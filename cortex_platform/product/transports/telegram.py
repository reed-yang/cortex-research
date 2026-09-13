"""Cortex-owned Telegram command adapter and durable-event projection."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from ..control import (
    ControlStore,
    ControlStoreError,
    FrozenTransportDeliveryProjection,
    IdempotencyConflict,
    InvalidTransition,
    MachineRunRefused,
    NotFound,
    RevisionConflict,
    TransportDeliveryButton,
    TransportDeliveryCapability,
    TransportDeliveryChunkClaim,
    TransportDeliveryChunkProjection,
    TransportDeliveryKey,
    TransportOpaqueTarget,
    TransportOpaqueTargetRegistration,
    telegram_provider_receipt_digest,
    transport_delivery_chunk_hash,
    transport_delivery_chunk_operation_id,
    transport_delivery_projection_hash,
)
from ..redaction import redact
from .hermes import HermesTelegramClient, _HermesTransportRPC
from .models import (
    COMMAND_REPLY_PREFIX,
    AdapterResult,
    DeliveryResult,
    OutboundMessage,
    TelegramButton,
    TelegramDestination,
    TelegramMessage,
    TelegramScope,
    TelegramUpdate,
    TransportProblem,
)
from .ports import (
    ControlTransportChunkPort,
    ControlTransportStatePort,
    DeliveryKey,
    TelegramChunkClient,
    TelegramClient,
    TelegramClientFailure,
    TelegramPermanentFailure,
    TelegramRateLimited,
    TelegramTemporaryFailure,
    TransportReceiptPort,
)
from .security import (
    OpaqueTokenService,
    PreparedOpaqueToken,
    chunk_markdown_v2,
    sanitize_text,
    stable_digest,
)
from .worker_rpc import TelegramRefusedBeforeSend

_log = logging.getLogger(__name__)

TRANSPORT = "telegram"
PROJECTION_VERSION = 1
_FROZEN_CHUNK_MAXIMUM = 1_024
_HERMES_RPC_TIMEOUT_SECONDS = 30
_BOT_IDENTITY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
#: Command names may carry digits, `_` and `-`: `/research-item` is the spelling
#: an operator reads in the Research view, and Telegram's own `/research_item`
#: spelling has to reach the same handler. Widening the name class cannot admit
#: a new action -- an unknown name still falls through to `invalid_command`.
_COMMAND_RE = re.compile(
    r"^/([A-Za-z][A-Za-z0-9_-]{0,31})(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.DOTALL
)
#: Only exact alternative spellings, never a general `-`/`_` fold: folding would
#: quietly turn `/re-search` into `/research`.
_COMMAND_ALIASES = {"research-item": "research_item"}
_RESEARCH_ITEM_RE = re.compile(r"ri_[0-9a-f]{32}\Z")
_ALLOWED_CHAT_TYPES = frozenset({"private", "group", "supergroup"})
_NOTIFICATION_TYPES = frozenset(
    {
        "decision.required",
        "run.blocked",
        "run.failed",
        "run.milestone",
        "run.completed",
    }
)

#: The one successful action whose reply is deliberately NOT sent. An ordinary
#: research message is answered by its own `run.completed` notification, so a
#: "Captured in the bound Cortex thread." line per message would be the second
#: acknowledgement the release explicitly does not want.
_UNDELIVERED_ACTIONS = frozenset({"capture_message"})


@dataclass(frozen=True)
class TelegramAdapterConfig:
    bot_identity: str
    signing_key: bytes = field(repr=False)
    allowed_user_ids: frozenset[int]
    base_url: str
    mode: Literal["active", "shadow"] = "shadow"
    default_workspace_id: str | None = None
    inbound_limit: int = 30
    inbound_window_seconds: int = 60
    identity_key: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not _BOT_IDENTITY_RE.fullmatch(self.bot_identity):
            raise ValueError("bot_identity must be a public stable name")
        if len(self.signing_key) < 32:
            raise ValueError("signing_key must contain at least 32 bytes")
        if self.identity_key is not None and len(self.identity_key) < 32:
            raise ValueError("identity_key must contain at least 32 bytes")
        if self.mode not in {"active", "shadow"}:
            raise ValueError("mode is invalid")
        if any(type(item) is not int for item in self.allowed_user_ids):
            raise ValueError("allowed_user_ids must contain integers")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTPS URL without query or fragment")
        if len(self.base_url.encode("utf-8")) > 512:
            raise ValueError("base_url must contain at most 512 bytes")
        if self.inbound_limit < 1 or self.inbound_window_seconds < 1:
            raise ValueError("rate limit values must be positive")


class FixedWindowRateLimiter:
    """A private-keyed in-memory limiter for synthetic and shadow verification."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._items: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int | None:
        now = self._clock().timestamp()
        cutoff = now - self._window_seconds
        with self._lock:
            values = self._items.setdefault(key, deque())
            while values and values[0] <= cutoff:
                values.popleft()
            if len(values) >= self._limit:
                return max(1, int((values[0] + self._window_seconds - now) * 1_000))
            values.append(now)
        return None


class _CommandReply:
    """What one inbound update needs in order to answer durably.

    Assembled before the handler runs, because a refusal raised *inside* the
    handler still deserves an answer and by then the binding is out of reach.
    Holds no message body: the text is whatever `AdapterResult` the handler
    produced, read once at freeze time.
    """

    __slots__ = (
        "attempted",
        "binding",
        "destination",
        "enabled",
        "event_id",
        "prepared",
    )

    def __init__(self) -> None:
        self.binding: Mapping[str, Any] | None = None
        self.destination: TelegramDestination | None = None
        self.enabled = False
        self.event_id: str | None = None
        #: Deep-link tokens signed but NOT yet registered. A command reply's
        #: token must become durable in the same transaction that freezes the
        #: chunk quoting it, or the freeze collides with the already-present
        #: digest and the operator gets a link to nothing.
        self.prepared: list[PreparedOpaqueToken] = []
        #: One freeze attempt per update. The `ok` path raises on failure and
        #: the refusal it becomes must not try again.
        self.attempted = False

    def arm(
        self,
        *,
        command_key: str,
        binding: Mapping[str, Any] | None,
        destination: TelegramDestination,
        enabled: bool,
    ) -> None:
        self.event_id = f"{COMMAND_REPLY_PREFIX}{command_key}"
        self.binding = binding
        self.destination = destination
        self.enabled = enabled and binding is not None

    def deliverable(self, result: AdapterResult) -> bool:
        """Should this exact result reach the operator's chat?"""

        if not self.enabled:
            return False
        if result.ok:
            return result.action not in _UNDELIVERED_ACTIONS
        # A refusal is worth sending only when it is the terminal answer.
        # `rate_limited` is excluded because answering every throttled update
        # is how a throttle becomes an amplifier; anything else retryable has
        # no terminal answer yet to send.
        return not result.retryable and result.category != "rate_limited"


class TelegramAdapter:
    """Translate Telegram updates into canonical Cortex ControlStore commands."""

    def __init__(
        self,
        *,
        store: ControlStore,
        config: TelegramAdapterConfig,
        client: TelegramClient | None,
        receipts: TransportReceiptPort,
        tokens: OpaqueTokenService,
        chunk_state: ControlTransportChunkPort | None = None,
        chunk_client: TelegramChunkClient | None = None,
        clock: Callable[[], datetime] | None = None,
        rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> None:
        self._store = store
        self.config = config
        if (chunk_state is None) != (chunk_client is None):
            raise ValueError("chunk_state and chunk_client must be configured together")
        if (client is None) == (chunk_client is None):
            raise ValueError("exactly one Telegram delivery client is required")
        self._client = client
        self._receipts = receipts
        self._tokens = tokens
        self._chunk_state = chunk_state
        self._chunk_client = chunk_client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._rate_limiter = rate_limiter or FixedWindowRateLimiter(
            limit=config.inbound_limit,
            window_seconds=config.inbound_window_seconds,
            clock=self._clock,
        )
        self._turn_sink: Callable[[str], None] | None = None
        #: ⟦P9-3 BRK-4⟧ What the runtime can do, asked at command time.
        self._capability_probe: Callable[[str], bool | None] | None = None

    def bind_capability_probe(
        self, probe: Callable[[str], bool | None] | None
    ) -> None:
        """How to ask whether the runtime can perform a control action.

        Bound after construction for the same reason the turn sink is: the
        adapter is built from configuration alone, and the runtime it would be
        asking about only exists once the managed worker has resolved. An
        adapter with no probe behaves exactly as it did before.
        """

        self._capability_probe = probe

    def bind_turn_sink(self, sink: Callable[[str], None] | None) -> None:
        """⟦P5.4c⟧ Who to tell that a thread has a new message to answer.

        Bound after construction rather than taken as an argument: the adapter
        is built by `build_telegram_adapter` from configuration alone, and
        whether this daemon can run a turn depends on a binding that only
        exists once the managed worker has resolved. An adapter with no sink
        behaves exactly as it did before -- capture and stop -- which is also
        what a shadow-mode or unbound daemon must keep doing.
        """

        self._turn_sink = sink

    @classmethod
    def control_owned(
        cls,
        *,
        store: ControlStore,
        config: TelegramAdapterConfig,
        client: TelegramClient,
        worker_id: str,
        delivery_lease_seconds: int = 30,
        previous_signing_keys: tuple[bytes, ...] = (),
        clock: Callable[[], datetime] | None = None,
        rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> TelegramAdapter:
        """Build an adapter whose replay, delivery, and token state is durable."""
        if previous_signing_keys and config.identity_key is None:
            raise ValueError("identity_key is required during signing-key rotation")
        durable_state = ControlTransportStatePort(
            store=store,
            worker_id=worker_id,
            lease_seconds=delivery_lease_seconds,
        )
        token_service = OpaqueTokenService(
            signing_key=config.signing_key,
            verification_keys=previous_signing_keys,
            store=durable_state,
            clock=clock,
        )
        return cls(
            store=store,
            config=config,
            client=client,
            receipts=durable_state,
            tokens=token_service,
            clock=clock,
            rate_limiter=rate_limiter,
        )

    @classmethod
    def hermes_control_owned(
        cls,
        *,
        store: ControlStore,
        config: TelegramAdapterConfig,
        rpc: _HermesTransportRPC,
        worker_id: str,
        delivery_lease_seconds: int = 30,
        previous_signing_keys: tuple[bytes, ...] = (),
        clock: Callable[[], datetime] | None = None,
        rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> TelegramAdapter:
        """Build the permit-gated synthetic Hermes delivery path."""

        if config.identity_key is None:
            raise ValueError("identity_key is required for frozen delivery")
        command_state = ControlTransportStatePort(
            store=store,
            worker_id=worker_id,
            lease_seconds=delivery_lease_seconds,
        )
        chunk_state = ControlTransportChunkPort(
            store=store,
            worker_id=worker_id,
            lease_seconds=delivery_lease_seconds,
        )
        token_service = OpaqueTokenService(
            signing_key=config.signing_key,
            verification_keys=previous_signing_keys,
            store=command_state,
            clock=clock,
        )
        chunk_client = HermesTelegramClient(
            rpc=rpc,
            state=chunk_state,
            bot_identity=config.bot_identity,
        )
        return cls(
            store=store,
            config=config,
            client=None,
            receipts=command_state,
            tokens=token_service,
            chunk_state=chunk_state,
            chunk_client=chunk_client,
            clock=clock,
            rate_limiter=rate_limiter,
        )

    def bind_destination(
        self,
        *,
        destination: TelegramDestination,
        thread_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Bind an operator-selected destination without returning its raw scope."""
        scope = self._scope(destination.chat_id, destination.topic_id)
        result = self._store.bind_transport(
            transport=TRANSPORT,
            external_scope=scope.canonical(),
            thread_id=thread_id,
            actor_id=self._adapter_actor(),
            idempotency_key=idempotency_key,
        )
        return result.value

    def handle_update(self, payload: bytes | str | Mapping[str, Any]) -> AdapterResult:
        """Reduce, authorize, and apply one update with sanitized failures."""
        idempotency_key: str | None = None
        request_digest: str | None = None
        reply = _CommandReply()
        try:
            update = TelegramUpdate.parse(payload)
            self._authorize(update)
            scope = self._scope(update.chat_id, update.topic_id)
            private_scope = self._private_scope_digest(scope)
            idempotency_key = self._idempotency_key(update.identity)
            request_digest = self._request_digest(update, private_scope)
            if self.config.mode == "active":
                try:
                    replay = self._receipts.command_result(
                        idempotency_key, request_digest
                    )
                except ValueError as exc:
                    raise TransportProblem("idempotency_conflict") from exc
                if replay is not None:
                    return replay
            retry_after = self._rate_limiter.check(
                f"{private_scope}:{self._actor_id(update.sender_id)}"
            )
            if retry_after is not None:
                raise TransportProblem(
                    "rate_limited", retryable=True, retry_after_ms=retry_after
                )
            binding = self._store.resolve_transport(
                transport=TRANSPORT, external_scope=scope.canonical()
            )
            reply.arm(
                command_key=idempotency_key,
                binding=binding,
                destination=TelegramDestination(
                    chat_id=update.chat_id, topic_id=update.topic_id
                ),
                enabled=self._reply_delivery_available(),
            )
            if update.callback is not None:
                result = self._handle_callback(
                    update=update,
                    binding=binding,
                    idempotency_key=idempotency_key,
                )
            else:
                result = self._handle_message(
                    update=update,
                    binding=binding,
                    idempotency_key=idempotency_key,
                    reply=reply,
                )
            if self.config.mode == "active" and result.ok:
                # Before the receipt and never after it: once `record_command`
                # commits, this update replays that stored result for ever and
                # the handler never runs again, so a reply not frozen by now
                # can never be re-derived.
                if not self._freeze_command_reply(reply, result):
                    raise TransportProblem("reply_not_persisted", retryable=True)
                self._receipts.record_command(idempotency_key, request_digest, result)
            return result
        except TransportProblem as exc:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category=exc.category,
                    action="reject",
                    response_text=_problem_text(exc.category),
                    retryable=exc.retryable,
                    retry_after_ms=exc.retry_after_ms,
                ),
            )
        except RevisionConflict as exc:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category="revision_conflict",
                    action="conflict",
                    response_text="The state changed before this action was applied. Refresh and retry.",
                    state=_safe_state(exc.current),
                    revision=_safe_revision(exc.current),
                ),
            )
        except IdempotencyConflict:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category="idempotency_conflict",
                    action="conflict",
                    response_text="This update identity was already used for a different command.",
                ),
            )
        except InvalidTransition:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category="invalid_transition",
                    action="reject",
                    response_text="That action is not valid for the current run state.",
                ),
            )
        except MachineRunRefused as exc:
            # ⟦ADJ-4⟧ The store's own refusal: `_apply_run_action` writes
            # cancel/pause straight through `transition_run`, and a run the
            # research engine owns is not this surface's to end.
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category=exc.category,
                    action="reject",
                    response_text=(
                        "That run belongs to the research engine and is ended by "
                        "the engine only."
                    ),
                ),
            )
        except NotFound:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category="not_found",
                    action="reject",
                    response_text="The requested Cortex resource is no longer available.",
                ),
            )
        except ControlStoreError:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category="control_store_error",
                    action="reject",
                    response_text="Cortex could not apply the command safely.",
                ),
            )
        except ValueError:
            return self._refused(
                reply,
                AdapterResult(
                    ok=False,
                    category="invalid_request",
                    action="reject",
                    response_text="The command was rejected as invalid.",
                ),
            )
        except Exception:
            return AdapterResult(
                ok=False,
                category="internal_error",
                action="reject",
                response_text="Cortex could not process this update safely.",
                retryable=True,
            )

    # -- durable command replies ------------------------------------------

    def _reply_delivery_available(self) -> bool:
        """Can this adapter own a reply through the managed delivery ledger?"""

        return (
            self.config.mode == "active"
            and self._chunk_state is not None
            and self._chunk_client is not None
        )

    def _refused(
        self, reply: _CommandReply, result: AdapterResult
    ) -> AdapterResult:
        """Answer a refusal durably where we can, and always return it.

        A refusal writes no command receipt -- `record_command` runs only for
        `ok` -- so the delivery key derived from the update identity is the
        whole of its idempotence. The text is deterministic prose from
        `_problem_text`, so a redelivered update freezes a byte-identical
        projection and Control answers `replayed` rather than sending twice.
        """

        if reply.attempted:
            # The `ok` path already tried and failed; this result IS that
            # failure. Trying again would only fail again.
            return result
        if not self._freeze_command_reply(reply, result):
            return AdapterResult(
                ok=False,
                category="reply_not_persisted",
                action="reject",
                response_text=_problem_text("reply_not_persisted"),
                retryable=True,
            )
        return result

    def _freeze_command_reply(
        self, reply: _CommandReply, result: AdapterResult
    ) -> bool:
        """Make this reply Control's to deliver. False only if it is not."""

        if not reply.deliverable(result):
            return True
        reply.attempted = True
        try:
            self._freeze_reply_projection(reply=reply, text=result.response_text)
        except Exception as exc:  # noqa: BLE001 - the reply, not the command
            _log.warning(
                "telegram command reply was not persisted: %s: %s",
                type(exc).__name__,
                redact(str(exc)),
            )
            return False
        return True

    def _freeze_reply_projection(
        self, *, reply: _CommandReply, text: str
    ) -> None:
        state = self._chunk_state
        client = self._chunk_client
        binding = reply.binding
        destination = reply.destination
        event_id = reply.event_id
        if (
            state is None
            or client is None
            or binding is None
            or destination is None
            or event_id is None
        ):  # pragma: no cover - guarded by _CommandReply.deliverable
            raise RuntimeError("frozen Telegram delivery is unavailable")
        capability_digest = client.capability_binding_digest()
        projection, targets = self._build_command_reply_projection(
            event_id=event_id,
            text=text,
            binding=binding,
            destination=destination,
            capability_digest=capability_digest,
            prepared=tuple(reply.prepared),
        )
        request_hash = self._command_reply_request_hash(
            event_id=event_id,
            text=text,
            binding=binding,
            destination=destination,
            capability_digest=capability_digest,
            opaque_targets=targets,
        )
        try:
            state.freeze(
                projection=projection,
                opaque_targets=targets,
                request_hash=request_hash,
            )
        except IdempotencyConflict:
            self._already_frozen()
        except InvalidTransition as exc:
            if exc.source not in {"projection_conflict", "projection_drift"}:
                raise
            self._already_frozen()

    @staticmethod
    def _already_frozen() -> None:
        """An earlier attempt at this same update already froze its reply.

        Reachable only when the inputs differ from that attempt's, which for a
        reply means a re-minted link token -- the text is otherwise a pure
        function of the update. Control's stored copy wins, because re-freezing
        is the one thing that would put a second link in front of the operator.

        The consequence to know about: the token in the `AdapterResult` this
        call returns was NOT registered (the freeze that would have registered
        it is the one that just lost), so that text must never be sent as-is.
        Nothing sends it -- `response_text` is the receipt's replay value and
        the delivered bytes come from the frozen projection -- and any future
        caller that wants to send it has to read the projection instead.
        """

        _log.info("telegram command reply was already frozen for this update")

    def deliver_command_reply(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        destination: TelegramDestination,
    ) -> DeliveryResult:
        """Send one frozen command reply the ledger still owes the operator.

        Nothing is re-derived. The claim carries Control's own frozen
        projection, so a restart mid-delivery resends the exact bytes -- and
        the exact single-use link -- the first attempt froze, and can never
        mint a second one or start a run.
        """

        if self.config.mode == "shadow":
            return DeliveryResult(category="shadow", delivered=False)
        state = self._chunk_state
        client = self._chunk_client
        if state is None or client is None:
            return DeliveryResult(category="delivery_unavailable", delivered=False)
        first_claim: TransportDeliveryChunkClaim | None = None
        try:
            first_claim = state.claim(delivery_key, 0)
            projection = first_claim.projection
            if not state.matches_destination(
                projection=projection,
                destination=destination,
                bot_identity=self.config.bot_identity,
            ):
                raise TransportProblem("binding_mismatch")
            return self._send_frozen_chunks(
                state=state,
                client=client,
                delivery_key=delivery_key,
                projection=projection,
                destination=destination,
                first_claim=first_claim,
            )
        except NotFound:
            # The ledger row named a projection that is gone. Nothing to send
            # and nothing to retry; the drain counts it and moves on.
            return DeliveryResult(category="ignored", delivered=False, ignored=True)
        except TransportProblem as exc:
            self._release_unstarted(state, first_claim)
            return DeliveryResult(
                category=exc.category,
                delivered=False,
                retryable=exc.retryable,
                retry_after_ms=exc.retry_after_ms,
            )
        except ControlStoreError as exc:
            return DeliveryResult(
                category=exc.category,
                delivered=False,
                retryable=bool(exc.retryable),
            )
        except Exception:  # noqa: BLE001 - never leak a provider detail
            return DeliveryResult(
                category="projection_failure", delivered=False, retryable=False
            )

    @staticmethod
    def _release_unstarted(
        state: ControlTransportChunkPort,
        claim: TransportDeliveryChunkClaim | None,
    ) -> None:
        """Give back a claim this worker took and never sent under."""

        if (
            claim is None
            or claim.status != "claimed"
            or claim.claim_owner != state.worker_id
        ):
            return
        try:
            state.release(claim=claim, proof="rpc_not_started")
        except Exception:  # noqa: BLE001 - the lease expires on its own
            pass

    def deliver_event(
        self,
        *,
        event: Mapping[str, Any],
        destination: TelegramDestination,
    ) -> DeliveryResult:
        """Project one committed event into an idempotent Telegram delivery."""
        key: DeliveryKey | None = None
        try:
            normalized = _normalize_event(event)
            if normalized["type"] not in _NOTIFICATION_TYPES:
                return DeliveryResult(category="ignored", delivered=False, ignored=True)
            scope = self._scope(destination.chat_id, destination.topic_id)
            binding = self._store.resolve_transport(
                transport=TRANSPORT, external_scope=scope.canonical()
            )
            if binding is None:
                raise TransportProblem("binding_required")
            run = self._store.get_run(str(normalized["run_id"]))
            if run["thread_id"] != binding["thread_id"]:
                raise TransportProblem("binding_mismatch")
            if self.config.mode == "shadow":
                text, _ = self._project_event(
                    normalized,
                    run=run,
                    binding=binding,
                    issue_capabilities=False,
                )
                return DeliveryResult(
                    category="shadow",
                    delivered=False,
                    chunks=len(chunk_markdown_v2(text)),
                )
            if self._chunk_state is not None and self._chunk_client is not None:
                return self._deliver_frozen_event(
                    normalized=normalized,
                    destination=destination,
                    run=run,
                    binding=binding,
                )
            key = DeliveryKey(
                transport=TRANSPORT,
                destination_digest=str(binding["external_scope"]),
                event_id=str(normalized["id"]),
                projection_version=PROJECTION_VERSION,
            )
            if self._client is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("Telegram client is unavailable")
            reservation = self._receipts.reserve_delivery(key)
            if reservation == "delivered":
                return DeliveryResult(
                    category="duplicate", delivered=False, duplicate=True
                )
            if reservation == "in_flight":
                return DeliveryResult(
                    category="delivery_in_flight",
                    delivered=False,
                    duplicate=True,
                    retryable=True,
                )
            text, buttons = self._project_event(
                normalized,
                run=run,
                binding=binding,
                issue_capabilities=True,
            )
            chunks = chunk_markdown_v2(text)
            canonical_key = key.canonical()
            for index, chunk in enumerate(chunks):
                self._client.send(
                    OutboundMessage(
                        destination=destination,
                        text=chunk,
                        parse_mode="MarkdownV2",
                        buttons=buttons if index == len(chunks) - 1 else (),
                        idempotency_key=f"{canonical_key}:chunk:{index}",
                    )
                )
            self._receipts.complete_delivery(key)
            return DeliveryResult(
                category="delivered", delivered=True, chunks=len(chunks)
            )
        except TelegramRateLimited as exc:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(
                category="rate_limited",
                delivered=False,
                retryable=True,
                retry_after_ms=exc.retry_after_ms,
            )
        except TelegramTemporaryFailure:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(
                category="transport_unavailable", delivered=False, retryable=True
            )
        except TelegramPermanentFailure:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(category="delivery_rejected", delivered=False)
        except TelegramClientFailure as exc:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(
                category="transport_failure",
                delivered=False,
                retryable=bool(exc.retryable),
                retry_after_ms=exc.retry_after_ms,
            )
        except TransportProblem as exc:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(
                category=exc.category,
                delivered=False,
                retryable=exc.retryable,
                retry_after_ms=exc.retry_after_ms,
            )
        except ControlStoreError as exc:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(
                category=exc.category,
                delivered=False,
                retryable=bool(exc.retryable),
            )
        except Exception:
            if key is not None:
                self._receipts.release_delivery(key)
            return DeliveryResult(
                category="projection_failure", delivered=False, retryable=False
            )

    def _deliver_frozen_event(
        self,
        *,
        normalized: Mapping[str, Any],
        destination: TelegramDestination,
        run: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> DeliveryResult:
        state = self._chunk_state
        client = self._chunk_client
        if state is None or client is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("frozen Telegram delivery is unavailable")
        delivery_key = TransportDeliveryKey(
            transport=TRANSPORT,
            destination_digest=str(binding["external_scope"]),
            event_id=str(normalized["id"]),
            projection_version=PROJECTION_VERSION,
        )
        first_claim = None
        try:
            first_claim = state.claim(delivery_key, 0)
            try:
                opaque_targets = state.registered_targets(first_claim.projection)
                request_hash = self._frozen_delivery_request_hash(
                    normalized=normalized,
                    binding=binding,
                    destination=destination,
                    capability_digest=(
                        first_claim.projection.capability_binding_digest
                    ),
                    opaque_targets=opaque_targets,
                )
                replay = state.replay(
                    projection=first_claim.projection,
                    request_hash=request_hash,
                    opaque_targets=opaque_targets,
                )
            except Exception:
                if (
                    first_claim.status == "claimed"
                    and first_claim.claim_owner == state.worker_id
                ):
                    state.release(claim=first_claim, proof="rpc_not_started")
                raise
            projection = replay.projection
        except NotFound:
            try:
                capability_digest = client.capability_binding_digest()
            except Exception:  # noqa: BLE001 - capability detail stays private.
                return DeliveryResult(
                    category="transport_unavailable",
                    delivered=False,
                    retryable=True,
                )
            projection, targets = self._build_frozen_projection(
                normalized=normalized,
                run=run,
                binding=binding,
                destination=destination,
                capability_digest=capability_digest,
            )
            request_hash = self._frozen_delivery_request_hash(
                normalized=normalized,
                binding=binding,
                destination=destination,
                capability_digest=capability_digest,
                opaque_targets=targets,
            )
            try:
                frozen = state.freeze(
                    projection=projection,
                    opaque_targets=targets,
                    request_hash=request_hash,
                )
            except InvalidTransition as exc:
                if exc.source not in {"projection_conflict", "projection_drift"}:
                    raise
                first_claim = state.claim(delivery_key, 0)
                try:
                    frozen = state.replay(
                        projection=first_claim.projection,
                        request_hash=request_hash,
                    )
                except Exception:
                    if (
                        first_claim.status == "claimed"
                        and first_claim.claim_owner == state.worker_id
                    ):
                        state.release(claim=first_claim, proof="rpc_not_started")
                    raise
            if frozen.disposition == "already_delivered":
                return DeliveryResult(
                    category="duplicate", delivered=False, duplicate=True
                )
            if frozen.disposition == "legacy_delivery_uncertain":
                return DeliveryResult(
                    category="manual_required", delivered=False, duplicate=True
                )
            projection = frozen.projection
        if projection is None:  # pragma: no cover - closed Control disposition
            raise RuntimeError("frozen projection is unavailable")

        return self._send_frozen_chunks(
            state=state,
            client=client,
            delivery_key=delivery_key,
            projection=projection,
            destination=destination,
            first_claim=first_claim,
        )

    def _send_frozen_chunks(
        self,
        *,
        state: ControlTransportChunkPort,
        client: TelegramChunkClient,
        delivery_key: TransportDeliveryKey,
        projection: FrozenTransportDeliveryProjection,
        destination: TelegramDestination,
        first_claim: TransportDeliveryChunkClaim | None,
    ) -> DeliveryResult:
        """Drive one frozen projection's chunks to a provider receipt.

        Shared by event notifications and command replies. Once a projection
        is frozen the two are the same object, and ⟦P5-01⟧ -- only a refusal
        provably made before a socket existed may be re-entered -- has to hold
        for both, or the ledger keeps only half of its promise.
        """

        delivered_now = 0
        for chunk_index in range(len(projection.chunks)):
            claim = (
                first_claim
                if chunk_index == 0 and first_claim is not None
                else state.claim(delivery_key, chunk_index)
            )
            if claim.status == "delivered":
                continue
            if claim.status == "sending_unknown":
                return DeliveryResult(
                    category="manual_required",
                    delivered=False,
                    chunks=chunk_index,
                )
            if claim.status == "failed":
                return DeliveryResult(
                    category="delivery_rejected",
                    delivered=False,
                    chunks=chunk_index,
                )
            if claim.status == "deferred":
                return DeliveryResult(
                    category="rate_limited",
                    delivered=False,
                    retryable=True,
                    retry_after_ms=self._retry_after_ms(claim.retry_not_before),
                    chunks=chunk_index,
                )
            if claim.claim_owner != state.worker_id:
                return DeliveryResult(
                    category="delivery_in_flight",
                    delivered=False,
                    duplicate=True,
                    retryable=True,
                    chunks=chunk_index,
                )

            try:
                observed_digest = client.capability_binding_digest()
            except Exception:  # noqa: BLE001 - capability detail stays private.
                state.release(claim=claim, proof="rpc_not_started")
                return DeliveryResult(
                    category="transport_unavailable",
                    delivered=False,
                    retryable=True,
                    chunks=chunk_index,
                )
            if observed_digest != projection.capability_binding_digest:
                state.release(claim=claim, proof="rpc_not_started")
                return DeliveryResult(
                    category="capability_mismatch",
                    delivered=False,
                    chunks=chunk_index,
                )
            decision = state.begin(
                claim,
                observed_capability_binding_digest=observed_digest,
            )
            if decision.status != "send_permitted" or decision.permit is None:
                return DeliveryResult(
                    category=decision.status,
                    delivered=False,
                    chunks=chunk_index,
                )
            permit = decision.permit
            try:
                outcome = client.send_chunk(
                    permit=permit,
                    destination=destination,
                )
            except TelegramRefusedBeforeSend:
                # ⟦P5.5/P5-01⟧ The one post-permit failure that is NOT unknown:
                # the frame was refused before a socket existed -- the shared
                # transport line was busy, or the worker declined to open a
                # second transport call while its long poll was still parked.
                # Provable, so the permit is released and the chunk is sent on
                # a later tick instead of stranding the operator's answer in
                # `manual_required` for a message that was never written.
                try:
                    state.release(
                        permit=permit,
                        proof="provider_proved_before_send",
                    )
                except Exception:  # noqa: BLE001 - outcome is already uncertain.
                    return DeliveryResult(
                        category="manual_required",
                        delivered=False,
                        chunks=chunk_index,
                    )
                return DeliveryResult(
                    category="transport_refused_before_send",
                    delivered=False,
                    retryable=True,
                    chunks=chunk_index,
                )
            except Exception:  # noqa: BLE001 - every post-permit failure is unknown.
                return DeliveryResult(
                    category="manual_required",
                    delivered=False,
                    chunks=chunk_index,
                )
            if outcome.status == "accepted" and outcome.provider_message_ref is not None:
                try:
                    receipt_digest = telegram_provider_receipt_digest(
                        self._identity_key(), outcome.provider_message_ref
                    )
                    state.complete(
                        permit,
                        provider_receipt_digest=receipt_digest,
                    )
                except Exception:  # noqa: BLE001 - outcome is already uncertain.
                    return DeliveryResult(
                        category="manual_required",
                        delivered=False,
                        chunks=chunk_index,
                    )
                delivered_now += 1
                continue
            if outcome.status == "rate_limited" and outcome.retry_after_ms is not None:
                try:
                    state.release(
                        permit=permit,
                        proof="provider_proved_before_send",
                        retry_after_ms=outcome.retry_after_ms,
                    )
                except Exception:  # noqa: BLE001 - outcome is already uncertain.
                    return DeliveryResult(
                        category="manual_required",
                        delivered=False,
                        chunks=chunk_index,
                    )
                return DeliveryResult(
                    category="rate_limited",
                    delivered=False,
                    retryable=True,
                    retry_after_ms=outcome.retry_after_ms,
                    chunks=chunk_index,
                )
            if outcome.status == "retryable_before_send":
                try:
                    state.release(
                        permit=permit,
                        proof="provider_proved_before_send",
                    )
                except Exception:  # noqa: BLE001 - outcome is already uncertain.
                    return DeliveryResult(
                        category="manual_required",
                        delivered=False,
                        chunks=chunk_index,
                    )
                return DeliveryResult(
                    category="transport_unavailable",
                    delivered=False,
                    retryable=True,
                    chunks=chunk_index,
                )
            if outcome.status == "rejected" and outcome.category is not None:
                try:
                    state.fail(permit, category=outcome.category)
                except Exception:  # noqa: BLE001 - outcome is already uncertain.
                    return DeliveryResult(
                        category="manual_required",
                        delivered=False,
                        chunks=chunk_index,
                    )
                return DeliveryResult(
                    category="delivery_rejected",
                    delivered=False,
                    chunks=chunk_index,
                )
            return DeliveryResult(
                category="manual_required",
                delivered=False,
                chunks=chunk_index,
            )
        return DeliveryResult(
            category="delivered" if delivered_now else "duplicate",
            delivered=bool(delivered_now),
            duplicate=not delivered_now,
            chunks=len(projection.chunks),
        )

    def _build_frozen_projection(
        self,
        *,
        normalized: Mapping[str, Any],
        run: Mapping[str, Any],
        binding: Mapping[str, Any],
        destination: TelegramDestination,
        capability_digest: str,
    ) -> tuple[
        FrozenTransportDeliveryProjection,
        tuple[TransportOpaqueTargetRegistration, ...],
    ]:
        prepared: list[PreparedOpaqueToken] = []
        text, buttons = self._project_event(
            normalized,
            run=run,
            binding=binding,
            issue_capabilities=True,
            prepared_tokens=prepared,
        )
        link_placeholders: list[tuple[str, str]] = []
        deep_links = [item for item in prepared if item.target.namespace == "deep_link"]
        for item in deep_links:
            link = self._with_navigation_hints(
                f"{self.config.base_url}?token={item.token}",
                resource_kind=item.target.resource_kind,
                resource_id=item.target.resource_id,
            )
            phrase = f"Open in Cortex: {link}"
            if text.count(phrase) != 1:
                raise RuntimeError("frozen deep-link projection is inconsistent")
            placeholder = f"CortexDeepLink{item.token_digest}"
            text = text.replace(phrase, placeholder, 1)
            link_placeholders.append((placeholder, link))
        return self._frozen_projection(
            event_id=str(normalized["id"]),
            text=text,
            buttons=buttons,
            prepared=tuple(prepared),
            link_placeholders=tuple(link_placeholders),
            binding=binding,
            destination=destination,
            capability_digest=capability_digest,
        )

    def _build_command_reply_projection(
        self,
        *,
        event_id: str,
        text: str,
        binding: Mapping[str, Any],
        destination: TelegramDestination,
        capability_digest: str,
        prepared: tuple[PreparedOpaqueToken, ...],
    ) -> tuple[
        FrozenTransportDeliveryProjection,
        tuple[TransportOpaqueTargetRegistration, ...],
    ]:
        """Project one command reply, reusing the event projection's shape.

        A reply carries no buttons, so the only capability it can declare is
        a deep link `/open` prepared. The link is lifted out of the text by
        its own characters rather than by an "Open in Cortex: " sentence,
        because a reply writes the URL into whatever prose its command chose.
        """

        link_placeholders: list[tuple[str, str]] = []
        for item in prepared:
            if item.target.namespace != "deep_link":
                raise RuntimeError("a command reply carries no action capability")
            link = self._with_navigation_hints(
                f"{self.config.base_url}?token={item.token}",
                resource_kind=item.target.resource_kind,
                resource_id=item.target.resource_id,
            )
            if text.count(link) != 1:
                raise RuntimeError("frozen command reply link is inconsistent")
            placeholder = f"CortexDeepLink{item.token_digest}"
            text = text.replace(link, placeholder, 1)
            link_placeholders.append((placeholder, link))
        return self._frozen_projection(
            event_id=event_id,
            text=text,
            buttons=(),
            prepared=prepared,
            link_placeholders=tuple(link_placeholders),
            binding=binding,
            destination=destination,
            capability_digest=capability_digest,
        )

    def _frozen_projection(
        self,
        *,
        event_id: str,
        text: str,
        buttons: tuple[TelegramButton, ...],
        prepared: tuple[PreparedOpaqueToken, ...],
        link_placeholders: tuple[tuple[str, str], ...],
        binding: Mapping[str, Any],
        destination: TelegramDestination,
        capability_digest: str,
    ) -> tuple[
        FrozenTransportDeliveryProjection,
        tuple[TransportOpaqueTargetRegistration, ...],
    ]:
        """Chunk one already-placeholdered text into a hash-bound projection.

        Shared by the event and command-reply builders. Only the sentence a
        deep link is lifted out of differs between them, so that substitution
        stays with the caller and everything after it lives here once: the
        chunk split, the per-chunk capability spans the projection validator
        re-derives from the bytes, and the chunk and projection hashes.
        """

        chunks = list(chunk_markdown_v2(text, maximum=_FROZEN_CHUNK_MAXIMUM))
        for index, chunk in enumerate(chunks):
            for placeholder, link in link_placeholders:
                if placeholder in chunk:
                    chunks[index] = chunks[index].replace(
                        placeholder,
                        f"[Open in Cortex]({_markdown_link_target(link)})",
                    )
        if any(len(chunk) > _FROZEN_CHUNK_MAXIMUM for chunk in chunks):
            raise RuntimeError("frozen Telegram chunk exceeds the internal maximum")
        operation_id = "telegram.delivery." + hashlib.sha256(
            f"{binding['external_scope']}\0{event_id}\0{PROJECTION_VERSION}".encode()
        ).hexdigest()
        delivery_key = TransportDeliveryKey(
            transport=TRANSPORT,
            destination_digest=str(binding["external_scope"]),
            event_id=event_id,
            projection_version=PROJECTION_VERSION,
        )
        deep_links = [item for item in prepared if item.target.namespace == "deep_link"]
        projected_chunks: list[TransportDeliveryChunkProjection] = []
        for index, chunk_text in enumerate(chunks):
            projected_buttons: tuple[TransportDeliveryButton, ...] = ()
            capabilities: list[TransportDeliveryCapability] = []
            if index == len(chunks) - 1:
                action_tokens = [
                    item for item in prepared if item.target.namespace == "action"
                ]
                if len(action_tokens) != len(buttons):
                    raise RuntimeError("frozen action projection is inconsistent")
                projected_buttons = tuple(
                    TransportDeliveryButton(
                        label=button.label,
                        callback_data=button.callback_data,
                        token_digest=prepared_token.token_digest,
                    )
                    for button, prepared_token in zip(buttons, action_tokens, strict=True)
                )
                capabilities.extend(
                    TransportDeliveryCapability(
                        namespace="action",
                        token_digest=item.token_digest,
                        expires_at=item.target.expires_at,
                    )
                    for item in action_tokens
                )
            chunk_bytes = chunk_text.encode("utf-8")
            for item in deep_links:
                token_bytes = item.token.encode("ascii")
                start = chunk_bytes.find(token_bytes)
                if start >= 0:
                    capabilities.append(
                        TransportDeliveryCapability(
                            namespace="deep_link",
                            token_digest=item.token_digest,
                            expires_at=item.target.expires_at,
                            start_offset=start,
                            end_offset=start + len(token_bytes),
                        )
                    )
            chunk = TransportDeliveryChunkProjection(
                chunk_index=index,
                operation_id=transport_delivery_chunk_operation_id(
                    operation_id, index
                ),
                text=chunk_text,
                parse_mode="MarkdownV2",
                buttons=projected_buttons,
                capabilities=tuple(capabilities),
            )
            projected_chunks.append(
                replace(chunk, chunk_hash=transport_delivery_chunk_hash(chunk))
            )
        targets = tuple(_prepared_target_registration(item) for item in prepared)
        projection = FrozenTransportDeliveryProjection(
            delivery_key=delivery_key,
            operation_id=operation_id,
            destination_binding_digest=str(binding["external_scope"]),
            routing="topic" if destination.topic_id is not None else "root",
            capability_binding_digest=capability_digest,
            rpc_timeout_seconds=_HERMES_RPC_TIMEOUT_SECONDS,
            projection_hash="",
            chunks=tuple(projected_chunks),
        )
        return (
            replace(
                projection,
                projection_hash=transport_delivery_projection_hash(
                    projection,
                    opaque_targets=targets,
                ),
            ),
            targets,
        )

    def _frozen_delivery_request_hash(
        self,
        *,
        normalized: Mapping[str, Any],
        binding: Mapping[str, Any],
        destination: TelegramDestination,
        capability_digest: str,
        opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
    ) -> str:
        value = {
            "event": {
                "id": normalized["id"],
                "run_id": normalized["run_id"],
                "type": normalized["type"],
                "durability": normalized["durability"],
                "payload": _frozen_request_payload(normalized),
            },
            "destination_binding_digest": binding["external_scope"],
            "routing": "topic" if destination.topic_id is not None else "root",
            "projection_version": PROJECTION_VERSION,
            "capability_binding_digest": capability_digest,
            "base_url": self.config.base_url,
            "targets": _frozen_request_targets(opaque_targets),
        }
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()

    def _command_reply_request_hash(
        self,
        *,
        event_id: str,
        text: str,
        binding: Mapping[str, Any],
        destination: TelegramDestination,
        capability_digest: str,
        opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
    ) -> str:
        """Everything that decided this reply's bytes, in one digest.

        Control compares it on a re-freeze: identical inputs replay the stored
        projection, and different ones are an idempotency conflict rather than
        a second message. A refusal's prose is deterministic, so a redelivered
        update lands on the first branch.
        """

        value = {
            "reply": {"event_id": event_id, "text": text},
            "destination_binding_digest": binding["external_scope"],
            "routing": "topic" if destination.topic_id is not None else "root",
            "projection_version": PROJECTION_VERSION,
            "capability_binding_digest": capability_digest,
            "base_url": self.config.base_url,
            "targets": _frozen_request_targets(opaque_targets),
        }
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()

    def _retry_after_ms(self, retry_not_before: datetime | None) -> int:
        if retry_not_before is None:
            return 1
        remaining = int((retry_not_before - self._clock()).total_seconds() * 1_000)
        return max(1, remaining)

    def resolve_deep_link(self, token: str) -> tuple[str, str]:
        """Resolve navigation only; no Cortex mutation is performed."""
        target = self._tokens.resolve(
            token,
            namespace="deep_link",
            purpose="open",
            consume=True,
        )
        return target.resource_kind, target.resource_id

    def _handle_message(
        self,
        *,
        update: TelegramUpdate,
        binding: Mapping[str, Any] | None,
        idempotency_key: str,
        reply: _CommandReply,
    ) -> AdapterResult:
        message = update.message
        if message is None:  # pragma: no cover - guarded by dispatch
            raise TransportProblem("invalid_request")
        if message.media is not None:
            raise TransportProblem("source_staging_unavailable")
        text = (message.text or "").strip()
        match = _COMMAND_RE.fullmatch(text)
        command = match.group(1).lower() if match else None
        command = _COMMAND_ALIASES.get(command, command)
        argument = (match.group(2) or "").strip() if match else ""
        if text.startswith("/") and match is None:
            raise TransportProblem("invalid_command")
        if binding is None:
            return self._unbound_result(message, command)
        thread_id = str(binding["thread_id"])
        if command in {None, "capture", "research", "chat"}:
            content = argument if command == "capture" else text
            if command in {"research", "chat"}:
                if not argument:
                    raise TransportProblem("invalid_command")
                content = f"/{command} {argument}"
            if not content:
                raise TransportProblem("invalid_command")
            if self.config.mode == "shadow":
                return AdapterResult(
                    ok=True,
                    category="shadow",
                    action="capture_message",
                    response_text="Shadow mode: the message would be captured in Cortex.",
                )
            result = self._append_message_with_retry(
                thread_id=thread_id,
                content=content,
                actor_id=self._actor_id(update.sender_id),
                idempotency_key=idempotency_key,
            )
            if self._turn_sink is not None and not result.replayed:
                # A replayed append is the same message arriving twice, and the
                # run it belongs to has already been asked for. Told after the
                # append commits, so the turn's history contains the message it
                # is a turn about; never allowed to fail the capture, because a
                # message that reached Control is captured whether or not
                # anything is going to answer it.
                try:
                    self._turn_sink(str(thread_id))
                except Exception:  # noqa: BLE001 - capture is the commitment
                    pass
            return AdapterResult(
                ok=True,
                category="ok",
                action="capture_message",
                response_text="Captured in the bound Cortex thread.",
                mutated=True,
                replayed=result.replayed,
            )
        if command == "research_item":
            return self._select_research_item(
                thread_id=thread_id,
                argument=argument,
                actor_id=self._actor_id(update.sender_id),
                idempotency_key=idempotency_key,
            )
        if command in {"approve", "deny"}:
            raise TransportProblem("ambiguous_decision")
        if command in {"start", "help"}:
            return AdapterResult(
                ok=True,
                category="ok",
                action="help",
                response_text=(
                    "Available: /research, /research-item, /chat, /status, /capture, "
                    "/resume, /cancel, /retry, and /open. Decision actions use bound buttons."
                ),
            )
        if command == "status":
            return self._status(thread_id)
        if command == "open":
            # The only command that mints a capability. Its token has to be
            # registered by the freeze that quotes it, so it is prepared --
            # never issued -- whenever this reply is going to be frozen. Any
            # future token-minting command must opt in here the same way.
            return self._open_thread(
                thread_id, prepared_tokens=reply.prepared if reply.enabled else None
            )
        if command not in {"pause", "resume", "cancel", "retry"}:
            raise TransportProblem("invalid_command")
        thread = self._store.get_thread(thread_id)
        active_run_id = thread.get("active_run_id")
        if active_run_id is None:
            raise TransportProblem("run_target_unavailable")
        run = self._store.get_run(str(active_run_id))
        if self.config.mode == "shadow":
            return AdapterResult(
                ok=True,
                category="shadow",
                action=command,
                response_text=f"Shadow mode: /{command} would target the bound run.",
                state=str(run["state"]),
                revision=int(run["revision"]),
            )
        result = self._apply_run_action(
            action=command,
            run=run,
            actor_id=self._actor_id(update.sender_id),
            idempotency_key=idempotency_key,
            retry_reason=argument or "Requested from the Telegram control surface",
        )
        return AdapterResult(
            ok=True,
            category="ok",
            action=command,
            response_text=f"The /{command} command was committed in Cortex.",
            mutated=True,
            replayed=result.replayed,
            state=str(result.value["state"]),
            revision=int(result.value["revision"]),
        )

    def _handle_callback(
        self,
        *,
        update: TelegramUpdate,
        binding: Mapping[str, Any] | None,
        idempotency_key: str,
    ) -> AdapterResult:
        if binding is None:
            raise TransportProblem("binding_required")
        callback = update.callback
        if callback is None:  # pragma: no cover - guarded by dispatch
            raise TransportProblem("invalid_request")
        target = self._tokens.resolve(
            callback.data,
            namespace="action",
            purpose="control",
            scope_digest=str(binding["external_scope"]),
            consumer=idempotency_key,
            consume=self.config.mode == "active",
        )
        if target.expected_revision is None or target.choice is None:
            raise TransportProblem("invalid_token")
        actor_id = self._actor_id(update.sender_id)
        thread_id = str(binding["thread_id"])
        if target.resource_kind == "decision":
            decision = self._store.get_decision(target.resource_id)
            run = self._store.get_run(str(decision["run_id"]))
            if run["thread_id"] != thread_id:
                raise TransportProblem("binding_mismatch")
            if self.config.mode == "shadow":
                return AdapterResult(
                    ok=True,
                    category="shadow",
                    action="decision",
                    response_text="Shadow mode: the decision would be resolved in Cortex.",
                    state=str(decision["state"]),
                    revision=int(decision["revision"]),
                )
            result = self._store.resolve_decision(
                decision_id=target.resource_id,
                choice=target.choice,
                expected_revision=target.expected_revision,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
            return AdapterResult(
                ok=True,
                category="ok",
                action="decision",
                response_text="The decision was committed in Cortex.",
                mutated=True,
                replayed=result.replayed,
                state=str(result.value["state"]),
                revision=int(result.value["revision"]),
            )
        if target.resource_kind != "run":
            raise TransportProblem("invalid_token")
        run = self._store.get_run(target.resource_id)
        if run["thread_id"] != thread_id:
            raise TransportProblem("binding_mismatch")
        if self.config.mode == "shadow":
            return AdapterResult(
                ok=True,
                category="shadow",
                action=target.choice,
                response_text="Shadow mode: the run action would be committed in Cortex.",
                state=str(run["state"]),
                revision=int(run["revision"]),
            )
        if int(run["revision"]) != target.expected_revision:
            raise RevisionConflict(run)
        result = self._apply_run_action(
            action=target.choice,
            run=run,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            retry_reason="Requested from a Telegram failure notification",
        )
        return AdapterResult(
            ok=True,
            category="ok",
            action=target.choice,
            response_text="The run action was committed in Cortex.",
            mutated=True,
            replayed=result.replayed,
            state=str(result.value["state"]),
            revision=int(result.value["revision"]),
        )

    def _unbound_result(
        self, message: TelegramMessage, command: str | None
    ) -> AdapterResult:
        if message.topic_id is not None:
            raise TransportProblem("binding_required")
        if command not in {None, "start", "help", "status", "open"}:
            raise TransportProblem("binding_required")
        workspace_id = self.config.default_workspace_id
        if workspace_id is None:
            raise TransportProblem("binding_required")
        threads = self._store.list_threads(workspace_id=workspace_id)
        titles = [sanitize_text(item.get("title"), maximum=120) for item in threads[:8]]
        summary = "\n".join(f"• {title}" for title in titles if title)
        if not summary:
            summary = "No Cortex threads are available for binding."
        link = (
            self._projection_deep_link(
                prepared_tokens=None,
                base_url=self.config.base_url,
                resource_kind="workspace",
                resource_id=workspace_id,
            )
            if self.config.mode == "active"
            else "Open Cortex from the primary Web/PWA surface."
        )
        return AdapterResult(
            ok=True,
            category="binding_required",
            action="list_threads",
            response_text=f"This Telegram root is not bound.\n{summary}\n{link}",
        )

    def _append_message_with_retry(
        self,
        *,
        thread_id: str,
        content: str,
        actor_id: str,
        idempotency_key: str,
    ):
        last_error: RevisionConflict | None = None
        for _ in range(3):
            thread = self._store.get_thread(thread_id)
            try:
                return self._store.append_message(
                    thread_id=thread_id,
                    role="user",
                    content=content,
                    expected_revision=int(thread["revision"]),
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                )
            except RevisionConflict as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("message revision retry failed")  # pragma: no cover

    def _status(self, thread_id: str) -> AdapterResult:
        thread = self._store.get_thread(thread_id)
        title = sanitize_text(thread.get("title"), maximum=160) or "Untitled thread"
        active_run_id = thread.get("active_run_id")
        if active_run_id is None:
            state = str(thread["status"])
            text = f"{title}\nThread status: {state}\nNo active run."
            revision = int(thread["revision"])
        else:
            run = self._store.get_run(str(active_run_id))
            state = str(run["state"])
            stage = sanitize_text(run.get("stage"), maximum=120)
            suffix = f"\nStage: {stage}" if stage else ""
            text = f"{title}\nRun status: {state}{suffix}"
            revision = int(run["revision"])
        return AdapterResult(
            ok=True,
            category="ok",
            action="status",
            response_text=text,
            state=state,
            revision=revision,
        )

    def _select_research_item(
        self,
        *,
        thread_id: str,
        argument: str,
        actor_id: str,
        idempotency_key: str,
    ) -> AdapterResult:
        """Choose which adopted item this chat's thread is about.

        Selection only rewrites the thread's item mapping through the existing
        Control command: it appends no message, starts no run, mints no
        capability, and touches no legacy research state. The evidence packet
        of the *next* `/research` is what the choice actually changes.
        """

        if not _RESEARCH_ITEM_RE.fullmatch(argument):
            raise TransportProblem("invalid_command")
        if self.config.mode == "shadow":
            return AdapterResult(
                ok=True,
                category="shadow",
                action="select_research_item",
                response_text=(
                    "Shadow mode: the adopted research item would be selected in Cortex."
                ),
            )
        result = self._store.select_research_item(
            thread_id=thread_id,
            item_id=argument,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        selected = self._store.get_research_thread_item(thread_id)
        title = sanitize_text(selected.get("title"), maximum=160) or "Untitled item"
        kind = sanitize_text(selected.get("kind"), maximum=40) or "item"
        return AdapterResult(
            ok=True,
            category="ok",
            action="select_research_item",
            response_text=(
                f"Selected {kind}: {title}\n"
                f"Item: {argument}\n"
                "This thread's research now uses that item's adopted dossier. "
                "Send /research <question> to start a bounded discussion of it, "
                "or /chat <message> to leave research mode."
            ),
            mutated=True,
            replayed=result.replayed,
            revision=int(selected["selection_revision"]),
        )

    def _open_thread(
        self,
        thread_id: str,
        *,
        prepared_tokens: list[PreparedOpaqueToken] | None = None,
    ) -> AdapterResult:
        if self.config.mode == "shadow":
            return AdapterResult(
                ok=True,
                category="shadow",
                action="open",
                response_text="Shadow mode: Cortex would issue a secure thread link.",
            )
        link = self._projection_deep_link(
            prepared_tokens=prepared_tokens,
            base_url=self.config.base_url,
            resource_kind="thread",
            resource_id=thread_id,
        )
        return AdapterResult(
            ok=True,
            category="ok",
            action="open",
            response_text=f"Open the bound Cortex thread: {link}",
        )

    def _apply_run_action(
        self,
        *,
        action: str,
        run: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
        retry_reason: str,
    ):
        common = {
            "run_id": str(run["id"]),
            "expected_revision": int(run["revision"]),
            "actor_id": actor_id,
            "idempotency_key": idempotency_key,
        }
        if action == "pause":
            # ⟦P9-3 BRK-4⟧ Refused rather than committed-and-undone. The
            # Hermes adapter reports `pause=False`, so this write was rolled
            # back by the delivery it triggered and the answer was delivered
            # anyway -- while `/pause` had already told the operator it was
            # committed. `None` means nothing is known about the runtime yet,
            # which behaves exactly as before.
            probe = self._capability_probe
            if probe is not None and probe("pause") is False:
                raise TransportProblem("pause_unsupported")
            return self._store.transition_run(target_state="pause_requested", **common)
        if action == "cancel":
            return self._store.transition_run(target_state="cancel_requested", **common)
        if action == "resume":
            return self._store.resume_run(**common)
        if action == "retry":
            return self._store.retry_run(reason=retry_reason, **common)
        raise TransportProblem("invalid_command")

    def _project_event(
        self,
        event: Mapping[str, Any],
        *,
        run: Mapping[str, Any],
        binding: Mapping[str, Any],
        issue_capabilities: bool,
        prepared_tokens: list[PreparedOpaqueToken] | None = None,
    ) -> tuple[str, tuple[TelegramButton, ...]]:
        event_type = str(event["type"])
        payload = event["payload"]
        if not isinstance(payload, Mapping):
            raise TransportProblem("invalid_event")
        buttons: tuple[TelegramButton, ...] = ()
        if event_type == "decision.required":
            text = "Decision required"
            prompt = sanitize_text(payload.get("prompt"), maximum=1_200)
            if prompt:
                text += f"\n{prompt}"
            decision_id = payload.get("decision_id")
            if isinstance(decision_id, str):
                decision = self._store.get_decision(decision_id)
                if (
                    decision["run_id"] == run["id"]
                    and decision["state"] == "pending"
                    and issue_capabilities
                ):
                    generated: list[TelegramButton] = []
                    for option in decision["options"][:6]:
                        if not isinstance(option, Mapping) or option.get("id") is None:
                            continue
                        choice = str(option["id"])
                        label = sanitize_text(option.get("label") or choice, maximum=48)
                        if not label:
                            continue
                        token = self._issue_projection_token(
                            prepared_tokens=prepared_tokens,
                            namespace="action",
                            purpose="control",
                            resource_kind="decision",
                            resource_id=decision_id,
                            expected_revision=int(decision["revision"]),
                            choice=choice,
                            scope_digest=str(binding["external_scope"]),
                            ttl=timedelta(minutes=10),
                        )
                        generated.append(
                            TelegramButton(label=label, callback_data=token)
                        )
                    buttons = tuple(generated)
        elif event_type == "run.failed":
            text = "Research run failed"
            summary = sanitize_text(payload.get("summary"), maximum=900)
            if summary:
                text += f"\n{summary}"
            if run["state"] == "failed" and issue_capabilities:
                token = self._issue_projection_token(
                    prepared_tokens=prepared_tokens,
                    namespace="action",
                    purpose="control",
                    resource_kind="run",
                    resource_id=str(run["id"]),
                    expected_revision=int(run["revision"]),
                    choice="retry",
                    scope_digest=str(binding["external_scope"]),
                    ttl=timedelta(minutes=10),
                )
                buttons = (TelegramButton(label="Retry", callback_data=token),)
        elif event_type == "run.blocked":
            text = "Research run is blocked"
            summary = sanitize_text(
                payload.get("summary") or payload.get("reason"), maximum=900
            )
            if summary:
                text += f"\n{summary}"
        elif event_type == "run.milestone":
            name = sanitize_text(
                payload.get("name") or payload.get("stage"), maximum=160
            )
            summary = sanitize_text(payload.get("summary"), maximum=900)
            text = "Research milestone"
            if name:
                text += f": {name}"
            if summary:
                text += f"\n{summary}"
        elif event_type == "run.completed":
            text = "Research completed"
            summary = sanitize_text(payload.get("summary"), maximum=1_200)
            if summary:
                text += f"\n{summary}"
            citations = _citation_titles(payload.get("citations"))
            if citations:
                text += "\nSelected citations:\n" + "\n".join(
                    f"• {item}" for item in citations
                )
            if issue_capabilities:
                link = self._projection_deep_link(
                    prepared_tokens=prepared_tokens,
                    base_url=self.config.base_url,
                    resource_kind="run",
                    resource_id=str(run["id"]),
                )
                text += f"\nOpen in Cortex: {link}"
        else:  # pragma: no cover - filtered by the notification allowlist
            raise TransportProblem("unsupported_event")
        return text, buttons

    def _issue_projection_token(
        self,
        *,
        prepared_tokens: list[PreparedOpaqueToken] | None,
        namespace: str,
        purpose: str,
        resource_kind: str,
        resource_id: str,
        expected_revision: int | None = None,
        choice: str | None = None,
        scope_digest: str | None = None,
        ttl: timedelta,
    ) -> str:
        if prepared_tokens is None:
            return self._tokens.issue(
                namespace=namespace,
                purpose=purpose,
                resource_kind=resource_kind,
                resource_id=resource_id,
                expected_revision=expected_revision,
                choice=choice,
                scope_digest=scope_digest,
                ttl=ttl,
            )
        prepared = self._tokens.prepare(
            namespace=namespace,
            purpose=purpose,
            resource_kind=resource_kind,
            resource_id=resource_id,
            expected_revision=expected_revision,
            choice=choice,
            scope_digest=scope_digest,
            ttl=ttl,
        )
        prepared_tokens.append(prepared)
        return prepared.token

    def _projection_deep_link(
        self,
        *,
        prepared_tokens: list[PreparedOpaqueToken] | None,
        base_url: str,
        resource_kind: str,
        resource_id: str,
    ) -> str:
        if prepared_tokens is None:
            link = self._tokens.deep_link(
                base_url=base_url,
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
        else:
            link, prepared = self._tokens.prepare_deep_link(
                base_url=base_url,
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
            prepared_tokens.append(prepared)
        return self._with_navigation_hints(
            link,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )

    def _with_navigation_hints(
        self, link: str, *, resource_kind: str, resource_id: str
    ) -> str:
        # Public Web IDs select a view; authentication and token authority stay separate.
        if resource_kind == "workspace":
            hints = {"project": resource_id}
        else:
            thread_id = (
                str(self._store.get_run(resource_id)["thread_id"])
                if resource_kind == "run"
                else resource_id
            )
            thread = self._store.get_thread(thread_id)
            hints = {"project": str(thread["workspace_id"]), "thread": thread_id}
        return f"{link}&{urlencode(hints)}"

    def _authorize(self, update: TelegramUpdate) -> None:
        item = update.message or update.callback
        if item is None or item.chat_type not in _ALLOWED_CHAT_TYPES:
            raise TransportProblem("unauthorized")
        if update.sender_id not in self.config.allowed_user_ids:
            raise TransportProblem("unauthorized")

    def _scope(self, chat_id: int, topic_id: int | None) -> TelegramScope:
        if type(chat_id) is not int or type(topic_id) not in {int, type(None)}:
            raise TransportProblem("invalid_request")
        return TelegramScope(
            bot_identity=self.config.bot_identity,
            chat_id=chat_id,
            topic_id=topic_id,
        )

    def _private_scope_digest(self, scope: TelegramScope) -> str:
        return stable_digest(
            self._identity_key(), "telegram-scope", scope.canonical()
        )

    def _actor_id(self, sender_id: int) -> str:
        value = f"{self.config.bot_identity}:{sender_id}"
        return (
            f"telegram-actor-{stable_digest(self._identity_key(), 'actor', value)}"
        )

    def _adapter_actor(self) -> str:
        return f"telegram-adapter-{stable_digest(self._identity_key(), 'adapter', self.config.bot_identity)}"

    def _idempotency_key(self, update_identity: str) -> str:
        value = f"{self.config.bot_identity}:{update_identity}"
        return f"tg-{stable_digest(self._identity_key(), 'update', value)}"

    def _identity_key(self) -> bytes:
        return self.config.identity_key or self.config.signing_key

    def _request_digest(self, update: TelegramUpdate, private_scope: str) -> str:
        if update.message is not None:
            body: dict[str, Any] = {
                "kind": "message",
                "text": update.message.text,
                "media": (
                    {
                        "kind": update.message.media.kind,
                        "file_id": update.message.media.file_id,
                        "unique": update.message.media.file_unique_id,
                        "name": update.message.media.file_name,
                        "mime": update.message.media.mime_type,
                        "size": update.message.media.file_size,
                    }
                    if update.message.media is not None
                    else None
                ),
            }
        else:
            callback = update.callback
            body = {"kind": "callback", "data": callback.data if callback else None}
        canonical = json.dumps(
            {
                "identity": update.identity,
                "scope": private_scope,
                "actor": self._actor_id(update.sender_id),
                "body": body,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def _prepared_target_registration(
    prepared: PreparedOpaqueToken,
) -> TransportOpaqueTargetRegistration:
    target = prepared.target
    return TransportOpaqueTargetRegistration(
        token_digest=prepared.token_digest,
        expires_at=target.expires_at,
        target=TransportOpaqueTarget(
            namespace=target.namespace,
            purpose=target.purpose,
            resource_kind=target.resource_kind,
            resource_id=target.resource_id,
            expected_revision=target.expected_revision,
            choice=target.choice,
            scope_digest=target.scope_digest,
            expires_at=target.expires_at,
        ),
    )


def _markdown_link_target(value: str) -> str:
    return value.replace("\\", "\\\\").replace(")", "\\)")


def _frozen_request_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    if not isinstance(payload, Mapping):
        raise TransportProblem("invalid_event")
    event_type = str(event["type"])
    if event_type == "decision.required":
        options = []
        raw_options = payload.get("options")
        if isinstance(raw_options, list):
            for option in raw_options[:6]:
                if not isinstance(option, Mapping) or option.get("id") is None:
                    continue
                choice = str(option["id"])
                label = sanitize_text(option.get("label") or choice, maximum=48)
                if label:
                    options.append({"choice": choice, "label": label})
        return {
            "decision_id": payload.get("decision_id"),
            "kind": sanitize_text(payload.get("kind"), maximum=100),
            "prompt": sanitize_text(payload.get("prompt"), maximum=1_200),
            "options": options,
        }
    if event_type == "run.failed":
        return {"summary": sanitize_text(payload.get("summary"), maximum=900)}
    if event_type == "run.blocked":
        return {
            "summary": sanitize_text(
                payload.get("summary") or payload.get("reason"), maximum=900
            )
        }
    if event_type == "run.milestone":
        return {
            "name": sanitize_text(
                payload.get("name") or payload.get("stage"), maximum=160
            ),
            "summary": sanitize_text(payload.get("summary"), maximum=900),
        }
    if event_type == "run.completed":
        return {
            "summary": sanitize_text(payload.get("summary"), maximum=1_200),
            "citations": list(_citation_titles(payload.get("citations"))),
        }
    raise TransportProblem("unsupported_event")


def _frozen_request_targets(
    opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "namespace": registration.target.namespace,
            "purpose": registration.target.purpose,
            "resource_kind": registration.target.resource_kind,
            "resource_id": registration.target.resource_id,
            "expected_revision": registration.target.expected_revision,
            "choice": registration.target.choice,
            "scope_digest": registration.target.scope_digest,
        }
        for registration in opaque_targets
    ]


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    required = {"id", "run_id", "type", "durability", "payload"}
    if not required.issubset(event):
        raise TransportProblem("invalid_event")
    if event["durability"] != "durable":
        raise TransportProblem("invalid_event")
    for name in ("id", "run_id", "type"):
        if (
            not isinstance(event[name], str)
            or not event[name]
            or len(event[name]) > 500
        ):
            raise TransportProblem("invalid_event")
    if not isinstance(event["payload"], Mapping):
        raise TransportProblem("invalid_event")
    return dict(event)


def _citation_titles(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    titles: list[str] = []
    for item in value[:5]:
        raw = item.get("title") if isinstance(item, Mapping) else item
        title = sanitize_text(raw, maximum=180)
        if title:
            titles.append(title)
    return tuple(titles)


def _safe_state(value: Mapping[str, Any]) -> str | None:
    state = value.get("state")
    return str(state) if isinstance(state, str) else None


def _safe_revision(value: Mapping[str, Any]) -> int | None:
    revision = value.get("revision")
    return revision if type(revision) is int else None


def _problem_text(category: str) -> str:
    messages = {
        "ambiguous_decision": "Use the bound approve or deny button for this decision.",
        "binding_required": "Bind this Telegram chat or topic to a Cortex thread first.",
        "rate_limited": "Too many Telegram commands. Retry after the indicated delay.",
        "pause_unsupported": "This runtime cannot pause a turn. Use /cancel to stop it.",
        "reply_not_persisted": "Cortex could not take ownership of the reply to that command.",
        "run_target_unavailable": "No bound active run can accept that command.",
        "source_staging_unavailable": "Media staging is not enabled yet; no source was created.",
        "unauthorized": "This Telegram identity is not authorized for Cortex controls.",
    }
    return messages.get(category, "The Telegram update was rejected safely.")
