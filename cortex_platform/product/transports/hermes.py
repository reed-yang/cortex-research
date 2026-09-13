"""Private codecs for the dormant Hermes Telegram transport bridge."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..control import (
    TransportDeliveryChunkSendPermit,
    transport_delivery_chunk_wire_operation_id,
)
from .models import TelegramChunkSendOutcome, TelegramDestination
from .ports import ControlTransportChunkPort
from .worker_rpc import TelegramRefusedBeforeSend

_TELEGRAM_PROTOCOL = "cortex.telegram.transport/1"
_CAPABILITY_FIELDS = frozenset(
    {
        "protocol",
        "send_message",
        "topics",
        "inline_buttons",
        "markdown_v2",
        "provider_idempotency",
        "outcome_query",
    }
)
_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_REJECTION_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_RETRY_AFTER_MS = 3_600_000


class _HermesTransportRPC(Protocol):
    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> object: ...


class _HermesCapabilityUnavailable(RuntimeError):
    """The worker did not prove the exact closed Telegram capability."""

    def __init__(self) -> None:
        super().__init__("hermes_telegram_capability_unavailable")


class _HermesOutcomeUnknown(RuntimeError):
    """The worker result cannot prove whether Telegram accepted the request."""

    def __init__(self) -> None:
        super().__init__("hermes_telegram_outcome_unknown")


@dataclass(frozen=True)
class _HermesTelegramCapability:
    protocol: Literal["cortex.telegram.transport/1"]
    send_message: bool
    topics: bool
    inline_buttons: bool
    markdown_v2: bool
    provider_idempotency: bool
    outcome_query: bool


@dataclass(frozen=True, repr=False)
class _HermesFrozenSendRequest:
    operation_id: str
    chat_id: int
    topic_id: int | None
    text: str = field(repr=False)
    buttons: tuple[tuple[str, str], ...] = field(repr=False)
    parse_mode: Literal["MarkdownV2"] = "MarkdownV2"
    schema_version: Literal[1] = field(default=1, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or _OPERATION_ID_RE.fullmatch(self.operation_id) is None
        ):
            raise ValueError("invalid_hermes_send_request")
        if type(self.chat_id) is not int or self.chat_id == 0:
            raise ValueError("invalid_hermes_send_request")
        if self.topic_id is not None and (
            type(self.topic_id) is not int or self.topic_id < 1
        ):
            raise ValueError("invalid_hermes_send_request")
        if (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > 4_096
            or "\x00" in self.text
            or self.parse_mode != "MarkdownV2"
        ):
            raise ValueError("invalid_hermes_send_request")
        if (
            not isinstance(self.buttons, tuple)
            or len(self.buttons) > 6
            or any(not _valid_button(button) for button in self.buttons)
        ):
            raise ValueError("invalid_hermes_send_request")

    def __repr__(self) -> str:
        return "<_HermesFrozenSendRequest>"


@dataclass(frozen=True)
class _HermesSendResult:
    status: Literal[
        "accepted", "rate_limited", "retryable_before_send", "rejected"
    ]
    provider_message_ref: str | None = field(default=None, repr=False)
    retry_after_ms: int | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        if self.status == "accepted":
            valid = (
                isinstance(self.provider_message_ref, str)
                and 1 <= len(self.provider_message_ref.encode("utf-8")) <= 512
                and "\x00" not in self.provider_message_ref
                and self.retry_after_ms is None
                and self.category is None
            )
        elif self.status == "rate_limited":
            valid = (
                self.provider_message_ref is None
                and type(self.retry_after_ms) is int
                and 1 <= self.retry_after_ms <= _MAX_RETRY_AFTER_MS
                and self.category is None
            )
        elif self.status == "retryable_before_send":
            valid = (
                self.provider_message_ref is None
                and self.retry_after_ms is None
                and self.category is None
            )
        elif self.status == "rejected":
            valid = (
                self.provider_message_ref is None
                and self.retry_after_ms is None
                and isinstance(self.category, str)
                and _REJECTION_CATEGORY_RE.fullmatch(self.category) is not None
            )
        else:
            valid = False
        if not valid:
            raise ValueError("invalid_hermes_send_result")


class _HermesTelegramCapabilityProbe:
    """Probe the injected worker until it answers, and retain the answer.

    ⟦F-B2⟧ A FAILURE is not retained. It used to be: `_attempted` was set
    before the RPC, so the first probe to lose a race -- a worker still
    starting, a relaunch, one timed-out frame -- made `probe()` raise for the
    life of the client. `capability_binding_digest()` is called by
    `send_chunk` as well as by the freeze, so one cold-start race turned into a
    daemon-lifetime outbound outage with nothing on any surface naming it. A
    success is still retained: the capabilities of a byte-pinned release do not
    change under a running daemon, and re-asking would be a frame per send.
    """

    def __init__(self, rpc: _HermesTransportRPC, *, timeout: float = 2.0) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        self._rpc = rpc
        self._timeout = float(timeout)
        self._capability: _HermesTelegramCapability | None = None
        self._lock = threading.Lock()

    def probe(self) -> _HermesTelegramCapability:
        with self._lock:
            if self._capability is not None:
                return self._capability
            try:
                raw = self._rpc.request(
                    "telegram.capabilities", {}, timeout=self._timeout
                )
                capability = _parse_capability(raw)
            except Exception:  # noqa: BLE001 - all worker failures fail closed.
                raise _HermesCapabilityUnavailable from None
            self._capability = capability
            return capability


class HermesTelegramClient:
    """Permit-gated private Hermes Telegram RPC client."""

    def __init__(
        self,
        *,
        rpc: _HermesTransportRPC,
        state: ControlTransportChunkPort,
        bot_identity: str,
        probe_timeout: float = 2.0,
    ) -> None:
        if type(state) is not ControlTransportChunkPort:
            raise TypeError("ControlTransportChunkPort is required")
        if not isinstance(bot_identity, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,64}", bot_identity
        ):
            raise ValueError("bot_identity is invalid")
        self._rpc = rpc
        self._state = state
        self._bot_identity = bot_identity
        self._probe = _HermesTelegramCapabilityProbe(rpc, timeout=probe_timeout)

    @property
    def state(self) -> ControlTransportChunkPort:
        return self._state

    def capability_binding_digest(self) -> str:
        capability = self._probe.probe()
        payload = {
            "inline_buttons": capability.inline_buttons,
            "markdown_v2": capability.markdown_v2,
            "outcome_query": capability.outcome_query,
            "protocol": capability.protocol,
            "provider_idempotency": capability.provider_idempotency,
            "send_message": capability.send_message,
            "topics": capability.topics,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()

    def send_chunk(
        self,
        *,
        permit: TransportDeliveryChunkSendPermit,
        destination: TelegramDestination,
    ) -> TelegramChunkSendOutcome:
        if type(permit) is not TransportDeliveryChunkSendPermit:
            raise TypeError("Control send permit is required")
        if not self._state.consume_send_permit(permit):
            raise _HermesOutcomeUnknown from None
        if not self._state.matches_destination(
            projection=permit.projection,
            destination=destination,
            bot_identity=self._bot_identity,
        ):
            raise _HermesOutcomeUnknown from None
        if self.capability_binding_digest() != permit.projection.capability_binding_digest:
            raise _HermesOutcomeUnknown from None
        request = _HermesFrozenSendRequest(
            # ⟦P5.5⟧ The ledger name is `<delivery>:chunk:<n>`; the WIRE name
            # is the same id with its colons flattened, because the certified
            # worker validates the `operation_id` it echoes in an error against
            # a pattern that has no colon in it and dies when it cannot. The
            # success reply carries no `operation_id` at all
            # (`serve.py::_TransportCalls._run`), so this changes nothing that
            # is read on the way back -- only what can be named on the way out.
            operation_id=transport_delivery_chunk_wire_operation_id(
                permit.chunk.operation_id
            ),
            chat_id=destination.chat_id,
            topic_id=destination.topic_id,
            text=permit.chunk.text,
            parse_mode=permit.chunk.parse_mode,
            buttons=tuple(
                (button.label, button.callback_data) for button in permit.chunk.buttons
            ),
        )
        try:
            raw = self._rpc.request(
                "telegram.send",
                _serialize_send_request(request),
                timeout=float(permit.projection.rpc_timeout_seconds),
            )
            result = _parse_send_result(raw)
        except (_HermesOutcomeUnknown, TelegramRefusedBeforeSend):
            # ⟦P5-01⟧ The refusal keeps its own type all the way to the caller.
            # Flattening it into `_HermesOutcomeUnknown` is what turned "the
            # line was busy, nothing was written" into `manual_required`.
            raise
        except Exception:  # noqa: BLE001 - provider/worker detail stays private.
            raise _HermesOutcomeUnknown from None
        return TelegramChunkSendOutcome(
            status=result.status,
            provider_message_ref=result.provider_message_ref,
            retry_after_ms=result.retry_after_ms,
            category=result.category,
        )


def _parse_capability(raw: object) -> _HermesTelegramCapability:
    if not isinstance(raw, Mapping) or set(raw) != _CAPABILITY_FIELDS:
        raise _HermesCapabilityUnavailable
    if raw["protocol"] != _TELEGRAM_PROTOCOL:
        raise _HermesCapabilityUnavailable
    boolean_fields = _CAPABILITY_FIELDS - {"protocol"}
    if any(type(raw[name]) is not bool for name in boolean_fields):
        raise _HermesCapabilityUnavailable
    if not all(
        raw[name]
        for name in ("send_message", "topics", "inline_buttons", "markdown_v2")
    ):
        raise _HermesCapabilityUnavailable
    return _HermesTelegramCapability(
        protocol="cortex.telegram.transport/1",
        send_message=True,
        topics=True,
        inline_buttons=True,
        markdown_v2=True,
        provider_idempotency=raw["provider_idempotency"],
        outcome_query=raw["outcome_query"],
    )


def _serialize_send_request(request: _HermesFrozenSendRequest) -> dict[str, object]:
    if not isinstance(request, _HermesFrozenSendRequest):
        raise TypeError("invalid_hermes_send_request")
    return {
        "schema_version": request.schema_version,
        "operation_id": request.operation_id,
        "chat_id": request.chat_id,
        "topic_id": request.topic_id,
        "text": request.text,
        "parse_mode": request.parse_mode,
        "buttons": [
            {"label": label, "callback_data": callback_data}
            for label, callback_data in request.buttons
        ],
    }


def _parse_send_result(raw: object) -> _HermesSendResult:
    if not isinstance(raw, Mapping):
        raise _HermesOutcomeUnknown
    status = raw.get("status")
    if not isinstance(status, str):
        raise _HermesOutcomeUnknown
    expected_fields = {
        "accepted": {"status", "provider_message_ref"},
        "rate_limited": {"status", "retry_after_ms"},
        "retryable_before_send": {"status"},
        "rejected": {"status", "category"},
        "outcome_unknown": {"status"},
    }.get(status)
    if expected_fields is None or set(raw) != expected_fields:
        raise _HermesOutcomeUnknown
    if status == "outcome_unknown":
        raise _HermesOutcomeUnknown
    try:
        if status == "accepted":
            return _HermesSendResult(
                status="accepted", provider_message_ref=raw["provider_message_ref"]
            )
        if status == "rate_limited":
            return _HermesSendResult(
                status="rate_limited", retry_after_ms=raw["retry_after_ms"]
            )
        if status == "retryable_before_send":
            return _HermesSendResult(status="retryable_before_send")
        return _HermesSendResult(status="rejected", category=raw["category"])
    except (TypeError, ValueError):
        raise _HermesOutcomeUnknown from None


def _valid_button(value: object) -> bool:
    if not isinstance(value, tuple) or len(value) != 2:
        return False
    label, callback_data = value
    return (
        isinstance(label, str)
        and 1 <= len(label) <= 64
        and "\x00" not in label
        and isinstance(callback_data, str)
        and 1 <= len(callback_data.encode("utf-8")) <= 64
        and "\x00" not in callback_data
    )
