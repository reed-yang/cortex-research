"""Typed durable transport state owned by the Cortex control store."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
#: What the CERTIFIED worker will echo back in a typed error reply. Its
#: `WorkerResponse.__post_init__` validates `operation_id` against
#: `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` -- no colon, 128 characters -- while the
#: `telegram.send` REQUEST is validated against a looser pattern that does allow
#: one. A chunk id therefore travelled to the worker happily and killed the
#: process the moment the worker had to name it in a refusal. The worker is
#: byte-pinned into a certified release, so the bound belongs here.
_WIRE_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
#: The exact shape `transport_delivery_chunk_operation_id` mints, and the only
#: shape the wire projection accepts: a colon-free base and one `:chunk:<n>`.
_CHUNK_OPERATION_ID_RE = re.compile(
    r"(?P<base>[A-Za-z0-9][A-Za-z0-9._-]*):chunk:(?P<index>\d{1,2})\Z"
)
_TRANSPORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,49}\Z")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_DIGEST_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")
_TARGET_KIND_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_TARGET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,499}\Z")
_DEEP_LINK_TOKEN_RE = re.compile(
    rb"dl1\.[A-Za-z0-9_-]{1,128}\.[A-Za-z0-9_-]{1,128}\Z"
)
_EMBEDDED_DEEP_LINK_TOKEN_RE = re.compile(
    rb"(?<![A-Za-z0-9_-])dl1\.[A-Za-z0-9_-]{1,128}\."
    rb"[A-Za-z0-9_-]{1,128}(?![A-Za-z0-9_-])"
)
_MAX_CHUNK_TEXT = 4_096
_MAX_BUTTONS = 6
_MAX_CAPABILITIES_PER_CHUNK = 32


@dataclass(frozen=True)
class TransportCommandResponse:
    ok: bool
    category: str
    action: str
    response_text: str
    mutated: bool = False
    replayed: bool = False
    retryable: bool = False
    retry_after_ms: int | None = None
    state: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class TransportCommandReceipt:
    transport: str
    command_key: str
    request_hash: str
    response: TransportCommandResponse
    created_at: datetime


@dataclass(frozen=True)
class TransportDeliveryKey:
    transport: str
    destination_digest: str
    event_id: str
    projection_version: int


@dataclass(frozen=True)
class TransportDeliveryClaim:
    key: TransportDeliveryKey
    status: Literal["claimed", "in_flight", "delivered"]
    claim_owner: str | None
    claim_epoch: int
    claim_expires_at: datetime | None


@dataclass(frozen=True)
class TransportOpaqueTarget:
    namespace: Literal["action", "deep_link"]
    purpose: str
    resource_kind: str
    resource_id: str
    expected_revision: int | None
    choice: str | None
    scope_digest: str | None
    expires_at: datetime


@dataclass(frozen=True)
class TransportDeliveryButton:
    label: str
    callback_data: str = field(repr=False)
    token_digest: str


@dataclass(frozen=True)
class TransportDeliveryCapability:
    namespace: Literal["action", "deep_link"]
    token_digest: str
    expires_at: datetime
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(frozen=True)
class TransportOpaqueTargetRegistration:
    token_digest: str
    expires_at: datetime
    target: TransportOpaqueTarget = field(repr=False)


@dataclass(frozen=True)
class TransportDeliveryChunkProjection:
    chunk_index: int
    operation_id: str
    text: str = field(repr=False)
    parse_mode: Literal["MarkdownV2"] = "MarkdownV2"
    buttons: tuple[TransportDeliveryButton, ...] = ()
    capabilities: tuple[TransportDeliveryCapability, ...] = ()
    chunk_hash: str = ""


@dataclass(frozen=True)
class FrozenTransportDeliveryProjection:
    delivery_key: TransportDeliveryKey
    operation_id: str
    destination_binding_digest: str
    routing: Literal["root", "topic"]
    capability_binding_digest: str
    rpc_timeout_seconds: int
    projection_hash: str
    chunks: tuple[TransportDeliveryChunkProjection, ...]


@dataclass(frozen=True)
class TransportDeliveryChunkClaim:
    projection: FrozenTransportDeliveryProjection
    chunk: TransportDeliveryChunkProjection
    status: Literal[
        "pending",
        "deferred",
        "claimed",
        "sending_unknown",
        "delivered",
        "failed",
    ]
    revision: int
    claim_owner: str | None
    claim_epoch: int
    retry_not_before: datetime | None


@dataclass(frozen=True)
class TransportDeliveryChunkSendPermit:
    projection: FrozenTransportDeliveryProjection
    chunk: TransportDeliveryChunkProjection
    revision: int
    claim_epoch: int
    #: The window that AUTHORIZED this send, read in the same transaction that
    #: issued the permit. ⟦AMD-5⟧ asks for the window a send was made under,
    #: and the gate can change between the permit and the receipt: deriving the
    #: id at completion made a disable lose it and a NEW window steal it, which
    #: the write-once trigger then locked in permanently.
    transport_window_id: str | None = None


@dataclass(frozen=True)
class TransportDeliveryChunkSendDecision:
    status: Literal[
        "send_permitted", "capability_expired", "capability_mismatch"
    ]
    permit: TransportDeliveryChunkSendPermit | None


@dataclass(frozen=True)
class TransportDeliveryFreezeResult:
    disposition: Literal[
        "frozen", "replayed", "already_delivered", "legacy_delivery_uncertain"
    ]
    projection: FrozenTransportDeliveryProjection | None


def transport_delivery_chunk_operation_id(operation_id: str, chunk_index: int) -> str:
    """Derive the stable bounded operation identity for one frozen chunk."""

    _operation_id(operation_id)
    if type(chunk_index) is not int or not 0 <= chunk_index < 100:
        raise ValueError("chunk_index must be between 0 and 99")
    derived = f"{operation_id}:chunk:{chunk_index}"
    if _OPERATION_ID_RE.fullmatch(derived) is None:
        raise ValueError("derived operation_id exceeds the safe bound")
    return derived


def transport_delivery_chunk_wire_operation_id(chunk_operation_id: str) -> str:
    """The colon-free name one frozen chunk answers to ON THE WIRE.

    Control keeps `<delivery>:chunk:<n>` -- it is the ledger key, the permit
    reference and what an operator resolves a stuck chunk by, and none of that
    changes. This is the value the daemon puts in the `telegram.send` frame, so
    that a worker which has to REFUSE the frame can name the operation in a
    typed error instead of dying inside its own response validator.

    The mapping is `":" -> "."`, which is injective over the ids this product
    mints: a delivery operation id is `telegram.delivery.<64 hex>` and carries
    no colon of its own, so the only colons in the input are the two this
    module added. Nothing durable is keyed by the result -- the worker uses it
    to echo, never to look anything up -- so the wire name is a projection of
    the ledger name and never the other way round.
    """

    _operation_id(chunk_operation_id)
    # ⟦P5.6⟧ The injectivity is a checked precondition, not a property of the
    # one caller that exists today (batch D D-10): the input must be exactly
    # `<base>:chunk:<n>` with a colon-free base that carries no `.chunk.`
    # segment of its own, so no two ledger names can share a wire name.
    match = _CHUNK_OPERATION_ID_RE.fullmatch(chunk_operation_id)
    if match is None or ".chunk." in match.group("base"):
        raise ValueError("chunk operation_id is not a delivery chunk name")
    wire = chunk_operation_id.replace(":", ".")
    if _WIRE_OPERATION_ID_RE.fullmatch(wire) is None:
        raise ValueError("wire operation_id is invalid")
    return wire


def telegram_provider_receipt_digest(
    identity_key: bytes, provider_reference: str
) -> str:
    """Reduce a transient Telegram provider reference to a stable receipt digest."""

    if not isinstance(identity_key, bytes) or len(identity_key) < 32:
        raise ValueError("identity_key must contain at least 32 bytes")
    if (
        not isinstance(provider_reference, str)
        or not 1 <= len(provider_reference.encode("utf-8")) <= 512
        or "\x00" in provider_reference
    ):
        raise ValueError("provider_reference is invalid")
    digest = hmac.new(
        identity_key,
        b"telegram-provider-receipt\0" + provider_reference.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def transport_delivery_chunk_hash(chunk: TransportDeliveryChunkProjection) -> str:
    """Hash the exact immutable payload and declared capability locations."""

    value = _validated_chunk(chunk, expected_index=chunk.chunk_index)
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def transport_delivery_projection_hash(
    projection: FrozenTransportDeliveryProjection,
    *,
    opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
) -> str:
    """Hash one complete frozen projection, including opaque registrations."""

    projected, target_values = _validated_projection(
        projection,
        opaque_targets=opaque_targets,
        require_hashes=True,
    )
    return hashlib.sha256(
        _canonical_json({"projection": projected, "opaque_targets": target_values})
    ).hexdigest()


def validate_transport_delivery_projection(
    projection: FrozenTransportDeliveryProjection,
    *,
    opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
) -> FrozenTransportDeliveryProjection:
    """Fail closed unless the complete projection is canonical and hash-bound."""

    _digest(projection.projection_hash, "projection_hash")
    expected = transport_delivery_projection_hash(
        projection,
        opaque_targets=opaque_targets,
    )
    if not hmac.compare_digest(projection.projection_hash, expected):
        raise ValueError("projection_hash does not match the frozen projection")
    return projection


def _validated_projection(
    projection: FrozenTransportDeliveryProjection,
    *,
    opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
    require_hashes: bool,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if type(projection) is not FrozenTransportDeliveryProjection:
        raise TypeError("projection must be a FrozenTransportDeliveryProjection")
    key = _validated_delivery_key(projection.delivery_key)
    operation_id = _operation_id(projection.operation_id)
    destination_binding_digest = _binding_digest(
        projection.destination_binding_digest,
        "destination_binding_digest",
    )
    if destination_binding_digest != projection.delivery_key.destination_digest:
        raise ValueError("destination_binding_digest must match the delivery key")
    if projection.routing not in {"root", "topic"}:
        raise ValueError("routing is invalid")
    capability_binding_digest = _digest(
        projection.capability_binding_digest,
        "capability_binding_digest",
    )
    if (
        type(projection.rpc_timeout_seconds) is not int
        or not 1 <= projection.rpc_timeout_seconds <= 120
    ):
        raise ValueError("rpc_timeout_seconds must be between 1 and 120")
    if type(projection.chunks) is not tuple or not 1 <= len(projection.chunks) <= 100:
        raise ValueError("chunks must contain between 1 and 100 items")

    chunk_values: list[dict[str, object]] = []
    all_capabilities: dict[str, TransportDeliveryCapability] = {}
    for index, chunk in enumerate(projection.chunks):
        if chunk.operation_id != transport_delivery_chunk_operation_id(
            operation_id, index
        ):
            raise ValueError("chunk operation_id is not deterministic")
        value = _validated_chunk(chunk, expected_index=index)
        if index != len(projection.chunks) - 1 and chunk.buttons:
            raise ValueError("buttons are allowed only on the final chunk")
        if require_hashes:
            _digest(chunk.chunk_hash, "chunk_hash")
            expected_chunk_hash = hashlib.sha256(_canonical_json(value)).hexdigest()
            if not hmac.compare_digest(chunk.chunk_hash, expected_chunk_hash):
                raise ValueError("chunk_hash does not match the frozen chunk")
        for capability in chunk.capabilities:
            if capability.token_digest in all_capabilities:
                raise ValueError("capability token_digest must be unique")
            all_capabilities[capability.token_digest] = capability
        chunk_values.append(value)

    target_values = _validated_target_registrations(
        opaque_targets,
        capabilities=all_capabilities,
        destination_binding_digest=destination_binding_digest,
    )
    return (
        {
            "delivery_key": key,
            "operation_id": operation_id,
            "destination_binding_digest": destination_binding_digest,
            "routing": projection.routing,
            "capability_binding_digest": capability_binding_digest,
            "rpc_timeout_seconds": projection.rpc_timeout_seconds,
            "chunks": chunk_values,
        },
        target_values,
    )


def _validated_chunk(
    chunk: TransportDeliveryChunkProjection,
    *,
    expected_index: int,
) -> dict[str, object]:
    if type(chunk) is not TransportDeliveryChunkProjection:
        raise TypeError("chunks must contain TransportDeliveryChunkProjection values")
    if type(chunk.chunk_index) is not int or chunk.chunk_index != expected_index:
        raise ValueError("chunk_index must be contiguous from zero")
    operation_id = _operation_id(chunk.operation_id)
    if (
        not isinstance(chunk.text, str)
        or not chunk.text
        or len(chunk.text.encode("utf-8")) > _MAX_CHUNK_TEXT
        or unicodedata.normalize("NFC", chunk.text) != chunk.text
        or any(
            ord(character) < 32 and character not in "\n\t"
            for character in chunk.text
        )
    ):
        raise ValueError("chunk text is invalid")
    if chunk.parse_mode != "MarkdownV2":
        raise ValueError("parse_mode must be MarkdownV2")
    if type(chunk.buttons) is not tuple or len(chunk.buttons) > _MAX_BUTTONS:
        raise ValueError("buttons are invalid")
    if (
        type(chunk.capabilities) is not tuple
        or len(chunk.capabilities) > _MAX_CAPABILITIES_PER_CHUNK
    ):
        raise ValueError("capabilities are invalid")

    button_values: list[dict[str, str]] = []
    action_digests: list[str] = []
    for button in chunk.buttons:
        if type(button) is not TransportDeliveryButton:
            raise TypeError("buttons must contain TransportDeliveryButton values")
        if (
            not isinstance(button.label, str)
            or not 1 <= len(button.label) <= 64
            or "\x00" in button.label
            or not isinstance(button.callback_data, str)
            or not 1 <= len(button.callback_data.encode("utf-8")) <= 64
            or "\x00" in button.callback_data
        ):
            raise ValueError("button is invalid")
        token_digest = _digest(button.token_digest, "button token_digest")
        if hashlib.sha256(button.callback_data.encode("utf-8")).hexdigest() != token_digest:
            raise ValueError("button token_digest does not match callback_data")
        action_digests.append(token_digest)
        button_values.append(
            {
                "label": button.label,
                "callback_data": button.callback_data,
                "token_digest": token_digest,
            }
        )

    text_bytes = chunk.text.encode("utf-8")
    capability_values: list[dict[str, object]] = []
    declared_action_digests: list[str] = []
    declared_deep_link_spans: set[tuple[int, int, str]] = set()
    for capability in chunk.capabilities:
        if type(capability) is not TransportDeliveryCapability:
            raise TypeError(
                "capabilities must contain TransportDeliveryCapability values"
            )
        if capability.namespace not in {"action", "deep_link"}:
            raise ValueError("capability namespace is invalid")
        token_digest = _digest(capability.token_digest, "capability token_digest")
        expires_at = _canonical_time(capability.expires_at, "capability expires_at")
        if capability.namespace == "action":
            if capability.start_offset is not None or capability.end_offset is not None:
                raise ValueError("action capability offsets must be null")
            declared_action_digests.append(token_digest)
        else:
            if (
                type(capability.start_offset) is not int
                or type(capability.end_offset) is not int
                or not 0 <= capability.start_offset < capability.end_offset <= len(text_bytes)
            ):
                raise ValueError("deep_link capability span is invalid")
            token_bytes = text_bytes[capability.start_offset : capability.end_offset]
            try:
                token_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(
                    "deep_link capability span crosses a UTF-8 boundary"
                ) from None
            if len(token_bytes) > 128:
                raise ValueError("deep_link token must not exceed 128 bytes")
            if _DEEP_LINK_TOKEN_RE.fullmatch(token_bytes) is None:
                raise ValueError("deep_link capability span is not a token")
            if hashlib.sha256(token_bytes).hexdigest() != token_digest:
                raise ValueError("deep_link capability span does not match token_digest")
            declared_deep_link_spans.add(
                (capability.start_offset, capability.end_offset, token_digest)
            )
        capability_values.append(
            {
                "namespace": capability.namespace,
                "token_digest": token_digest,
                "expires_at": expires_at,
                "start_offset": capability.start_offset,
                "end_offset": capability.end_offset,
            }
        )
    if declared_action_digests != action_digests:
        raise ValueError("action capabilities must match buttons in order")
    embedded_deep_link_spans = {
        (match.start(), match.end(), hashlib.sha256(match.group()).hexdigest())
        for match in _EMBEDDED_DEEP_LINK_TOKEN_RE.finditer(text_bytes)
    }
    if embedded_deep_link_spans != declared_deep_link_spans:
        raise ValueError("chunk contains an undeclared deep_link token")
    return {
        "chunk_index": chunk.chunk_index,
        "operation_id": operation_id,
        "text": chunk.text,
        "parse_mode": chunk.parse_mode,
        "buttons": button_values,
        "capabilities": capability_values,
    }


def _validated_target_registrations(
    registrations: tuple[TransportOpaqueTargetRegistration, ...],
    *,
    capabilities: dict[str, TransportDeliveryCapability],
    destination_binding_digest: str,
) -> list[dict[str, object]]:
    if type(registrations) is not tuple or len(registrations) != len(capabilities):
        raise ValueError("opaque target registrations must match capabilities")
    seen: set[str] = set()
    values: list[dict[str, object]] = []
    for registration in registrations:
        if type(registration) is not TransportOpaqueTargetRegistration:
            raise TypeError(
                "opaque_targets must contain TransportOpaqueTargetRegistration values"
            )
        token_digest = _digest(registration.token_digest, "opaque target token_digest")
        if token_digest in seen or token_digest not in capabilities:
            raise ValueError("opaque target registrations must match capabilities")
        seen.add(token_digest)
        capability = capabilities[token_digest]
        expires_at = _canonical_time(registration.expires_at, "opaque target expires_at")
        if expires_at != _canonical_time(capability.expires_at, "capability expires_at"):
            raise ValueError("opaque target expiry must match capability expiry")
        target = registration.target
        if type(target) is not TransportOpaqueTarget:
            raise TypeError("opaque target registration target is invalid")
        if target.namespace != capability.namespace:
            raise ValueError("opaque target namespace must match capability namespace")
        if expires_at != _canonical_time(target.expires_at, "target expires_at"):
            raise ValueError("opaque target expiry must match target expiry")
        purpose = _bounded_text(target.purpose, "target purpose", maximum=100)
        resource_kind = _bounded_text(
            target.resource_kind, "target resource_kind", maximum=100
        )
        resource_id = _bounded_text(
            target.resource_id, "target resource_id", maximum=500
        )
        if _TARGET_KIND_RE.fullmatch(resource_kind) is None:
            raise ValueError("target resource_kind is invalid")
        if _TARGET_ID_RE.fullmatch(resource_id) is None:
            raise ValueError("target resource_id is invalid")
        if target.expected_revision is not None and (
            type(target.expected_revision) is not int or target.expected_revision < 0
        ):
            raise ValueError("target expected_revision is invalid")
        choice = (
            _bounded_text(target.choice, "target choice", maximum=200)
            if target.choice is not None
            else None
        )
        scope_digest = (
            _binding_digest(target.scope_digest, "target scope_digest")
            if target.scope_digest is not None
            else None
        )
        if capability.namespace == "action" and scope_digest != destination_binding_digest:
            raise ValueError("action target scope_digest must match destination binding")
        values.append(
            {
                "token_digest": token_digest,
                "expires_at": expires_at,
                "target": {
                    "namespace": target.namespace,
                    "purpose": purpose,
                    "resource_kind": resource_kind,
                    "resource_id": resource_id,
                    "expected_revision": target.expected_revision,
                    "choice": choice,
                    "scope_digest": scope_digest,
                },
            }
        )
    if seen != set(capabilities):
        raise ValueError("opaque target registrations must match capabilities")
    return values


def _validated_delivery_key(key: TransportDeliveryKey) -> dict[str, object]:
    if type(key) is not TransportDeliveryKey:
        raise TypeError("delivery_key must be a TransportDeliveryKey")
    if not isinstance(key.transport, str) or _TRANSPORT_RE.fullmatch(key.transport) is None:
        raise ValueError("delivery_key transport is invalid")
    destination_digest = _binding_digest(
        key.destination_digest, "delivery_key destination_digest"
    )
    event_id = _bounded_text(key.event_id, "delivery_key event_id", maximum=500)
    if type(key.projection_version) is not int or key.projection_version < 1:
        raise ValueError("delivery_key projection_version is invalid")
    return {
        "transport": key.transport,
        "destination_digest": destination_digest,
        "event_id": event_id,
        "projection_version": key.projection_version,
    }


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or _OPERATION_ID_RE.fullmatch(value) is None:
        raise ValueError("operation_id is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _binding_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _BINDING_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a Control binding digest")
    return value


def _canonical_time(value: object, name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
