"""Runtime-port data models owned by Cortex."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


#: How much of a failure's own words a terminal event may carry. Bounded
#: because the string is written into Control's durable payload and read back
#: by a status surface, and neither is a place to put an unbounded blob.
FAILURE_DETAIL_LIMIT = 200

#: ⟦P5.4d⟧ What a turn that failed INSIDE the worker is called. The worker maps
#: every exception to a bare `failed: True` on purpose -- an exception's text is
#: the one place a provider error could carry a credential back across the
#: boundary -- so this side has no text to report and says so with a word rather
#: than by leaving the field empty. The fork's own account of such a turn is in
#: `worker.stdout.log` and `worker.stderr.log` in the slot's state dir.
WORKER_REPORTED_FAILURE = "worker_reported_failure"

#: A run of this many characters with no separator in it is not a word; it is a
#: key, a token or a digest. Redacted wholesale rather than matched by scheme,
#: because a detail is only worth carrying if it can never be the thing that
#: leaks one.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9_\-]{20,}")
#: ⟦P54D-3⟧ An absolute filesystem path. `FileNotFoundError` quotes one
#: verbatim, and the durable payload and `turn_bridge.last_failure` are both
#: read off `/api/v1/health`, which is answered before authentication -- so a
#: path there is the slot layout and the operator's home directory, published
#: on loopback to anything that can reach the port.
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/[A-Za-z0-9_./\-]{2,}")
#: A run of 12 or more characters that DOES carry a separator, which is the
#: shape the opaque-run heuristic was blind to: base64 (`+/=`), a bearer token
#: with dots in it, a `service:account` pair. Twelve, because a scrubbed detail
#: that says slightly less is a better trade than one that says a secret.
_SEPARATOR_RUN = re.compile(r"[A-Za-z0-9_\-]*[+/=:.][A-Za-z0-9_\-+/=:.]{11,}")


def _redact_detail(text: str) -> str:
    """Scrub before truncation, and scrub the shapes a secret actually takes.

    ⟦P54D-3⟧ The 20-character opaque-run heuristic let two whole classes
    through: an absolute path (no run of 20 without a `/` in it) and any
    credential carrying a separator -- a base64 key, a token with dots, a
    `service:account` reference.
    """

    text = _ABSOLUTE_PATH.sub("<path>", text)
    text = _SEPARATOR_RUN.sub("<redacted>", text)
    return _OPAQUE_RUN.sub("<redacted>", text)


def failure_detail(exc: BaseException) -> str:
    """The exception's class and its first line, bounded and scrubbed.

    ⟦P5.4d⟧ `runtime_execution_failed` used to be the whole story a failed turn
    told, which cost one slice of real running to find out that the failure was
    an `AttributeError` in the worker's own session-db call rather than anything
    to do with a provider. The class name alone would have said so.

    Only ever produced for an exception raised in the PRODUCT process -- the
    channel, the payload, the protocol -- because the worker never sends its
    own. That is what makes carrying the text defensible; the scrub is what
    makes it safe when a message quotes something the worker said.
    """

    name = type(exc).__name__
    first = str(exc).splitlines()[0].strip() if str(exc) else ""
    if not first:
        return name
    return f"{name}: {_redact_detail(first)}"[:FAILURE_DETAIL_LIMIT]


class EventDurability(str, Enum):
    DURABLE = "durable"
    TRANSIENT = "transient"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"


class ControlAction(str, Enum):
    CANCEL = "cancel"
    PAUSE = "pause"
    STEER = "steer"


class ActionOutcomeStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    DEDUPLICATED = "deduplicated"


@dataclass(frozen=True)
class RuntimeReleaseIdentity:
    """Exact identity reported by one isolated runtime worker."""

    release_id: str
    state_generation_id: str
    slot_id: str
    artifact_digest: str
    worker_protocol: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.release_id,
                self.state_generation_id,
                self.slot_id,
                self.artifact_digest,
                self.worker_protocol,
            )
        ):
            raise ValueError("runtime release identity fields must be non-empty")


@dataclass(frozen=True)
class ReleasePin:
    """Exact managed-runtime identity pinned to one Cortex attempt."""

    attempt_id: str
    release_id: str
    state_generation_id: str
    slot_id: str
    artifact_digest: str
    worker_protocol: str

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        RuntimeReleaseIdentity(
            release_id=self.release_id,
            state_generation_id=self.state_generation_id,
            slot_id=self.slot_id,
            artifact_digest=self.artifact_digest,
            worker_protocol=self.worker_protocol,
        )

    @property
    def runtime_identity(self) -> RuntimeReleaseIdentity:
        return RuntimeReleaseIdentity(
            release_id=self.release_id,
            state_generation_id=self.state_generation_id,
            slot_id=self.slot_id,
            artifact_digest=self.artifact_digest,
            worker_protocol=self.worker_protocol,
        )


@dataclass(frozen=True)
class RuntimeHandshake:
    expected: RuntimeReleaseIdentity
    observed: RuntimeReleaseIdentity | None
    verified: bool
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.verified and self.expected != self.observed:
            raise ValueError("verified runtime handshake must match exactly")


@dataclass(frozen=True)
class RuntimeBinding:
    adapter_id: str
    runtime_session_ref: str
    generation: int
    adapter_version: str
    parent_runtime_session_ref: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.adapter_id
            or not self.runtime_session_ref
            or not self.adapter_version
        ):
            raise ValueError("runtime binding identifiers must be non-empty")
        if self.generation < 0:
            raise ValueError("runtime binding generation must be non-negative")


@dataclass(frozen=True)
class RuntimeCapabilities:
    adapter_id: str
    available: bool
    session_create: bool = False
    session_load: bool = False
    session_fork: bool = False
    run_stream: bool = False
    cancel: bool = False
    pause: bool = False
    steer: bool = False
    decisions: bool = False
    session_rebinding: bool = False
    checkpoint_recovery: bool = False
    action_outcome_query: bool = False
    durable_operation_deduplication: bool = False
    provider_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeHealth:
    adapter_id: str
    adapter_version: str
    status: HealthStatus
    reason_code: str | None
    capabilities: RuntimeCapabilities
    compatibility: Mapping[str, Any] = field(default_factory=dict)
    runtime_identity: RuntimeReleaseIdentity | None = None


@dataclass(frozen=True)
class SessionOpenRequest:
    metadata: Mapping[str, Any] = field(default_factory=dict)
    adapter_operation_id: str = ""

    def __post_init__(self) -> None:
        if not self.adapter_operation_id:
            digest = hashlib.sha256(
                json.dumps(
                    self.metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "adapter_operation_id", f"session-{digest}")


@dataclass(frozen=True)
class AttemptRequest:
    run_id: str
    attempt_id: str
    binding: RuntimeBinding
    user_message: str
    system_message: str | None = None
    conversation_history: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    adapter_operation_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id or not self.attempt_id:
            raise ValueError("run identifiers must be non-empty")
        if not self.adapter_operation_id:
            digest = hashlib.sha256(
                f"{self.run_id}\x1f{self.attempt_id}".encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "adapter_operation_id", f"attempt-{digest}")
        if not self.user_message:
            raise ValueError("user_message must be non-empty")


@dataclass(frozen=True)
class ControlRequest:
    run_id: str
    attempt_id: str
    binding: RuntimeBinding
    action: ControlAction
    text: str | None = None
    adapter_operation_id: str = ""
    delivery_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.run_id or not self.attempt_id:
            raise ValueError("control identifiers must be non-empty")
        if not self.adapter_operation_id:
            text_digest = hashlib.sha256((self.text or "").encode("utf-8")).hexdigest()
            digest = hashlib.sha256(
                "\x1f".join(
                    (
                        self.run_id,
                        self.attempt_id,
                        self.binding.runtime_session_ref,
                        self.action.value,
                        text_digest,
                    )
                ).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "adapter_operation_id", f"control-{digest}")
        if self.delivery_epoch < 0:
            raise ValueError("delivery_epoch must be non-negative")


@dataclass(frozen=True)
class ControlResult:
    action: ControlAction
    accepted: bool
    reason_code: str | None = None
    adapter_operation_id: str | None = None
    delivery_epoch: int | None = None
    outcome: ActionOutcomeStatus | None = None


@dataclass(frozen=True)
class DecisionResolution:
    """A resolution already committed through the Cortex revision CAS."""

    run_id: str
    attempt_id: str
    binding: RuntimeBinding
    decision_id: str
    choice: str
    revision: int
    adapter_operation_id: str = ""
    delivery_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.decision_id or not self.choice:
            raise ValueError("decision identifiers must be non-empty")
        if not self.adapter_operation_id:
            digest = hashlib.sha256(
                "\x1f".join(
                    (
                        self.run_id,
                        self.attempt_id,
                        self.binding.runtime_session_ref,
                        self.decision_id,
                        self.choice,
                        str(self.revision),
                    )
                ).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "adapter_operation_id", f"decision-{digest}")
        if self.revision < 0:
            raise ValueError("decision revision must be non-negative")
        if self.delivery_epoch < 0:
            raise ValueError("delivery_epoch must be non-negative")


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    accepted: bool
    reason_code: str | None = None
    adapter_operation_id: str | None = None
    delivery_epoch: int | None = None
    outcome: ActionOutcomeStatus | None = None


@dataclass(frozen=True)
class ActionOutcomeQuery:
    run_id: str
    attempt_id: str
    binding: RuntimeBinding
    adapter_operation_id: str
    delivery_epoch: int

    def __post_init__(self) -> None:
        if not self.run_id or not self.attempt_id or not self.adapter_operation_id:
            raise ValueError("action outcome identifiers must be non-empty")
        if self.delivery_epoch < 0:
            raise ValueError("delivery_epoch must be non-negative")


@dataclass(frozen=True)
class RuntimeActionOutcome:
    adapter_operation_id: str
    delivery_epoch: int
    status: ActionOutcomeStatus
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.adapter_operation_id:
            raise ValueError("adapter_operation_id must be non-empty")
        if self.delivery_epoch < 0:
            raise ValueError("delivery_epoch must be non-negative")
        if not isinstance(self.status, ActionOutcomeStatus):
            raise ValueError("status must be an ActionOutcomeStatus")


@dataclass(frozen=True)
class RuntimeCheckpoint:
    """A checkpoint handle whose referenced state is already committed."""

    checkpoint_ref: str
    conversation_history: tuple[Mapping[str, Any], ...]
    adapter_operation_id: str = ""

    def __post_init__(self) -> None:
        if not self.checkpoint_ref:
            raise ValueError("checkpoint identifier must be non-empty")
        if not self.adapter_operation_id:
            digest = hashlib.sha256(self.checkpoint_ref.encode("utf-8")).hexdigest()
            object.__setattr__(self, "adapter_operation_id", f"recovery-{digest}")


@dataclass(frozen=True)
class SessionInspection:
    binding: RuntimeBinding
    exists: bool
    active: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeEvent:
    run_id: str
    attempt_id: str
    type: str
    durability: EventDurability
    payload: Mapping[str, Any]
    event_id: str
    event_sequence: int
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    caused_by_adapter_operation_id: str | None = None
    caused_by_delivery_epoch: int | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.attempt_id or not self.type or not self.event_id:
            raise ValueError("runtime event identifiers must be non-empty")
        if self.event_sequence < 0:
            raise ValueError("event_sequence must be non-negative")
        causal_fields = (
            self.caused_by_adapter_operation_id,
            self.caused_by_delivery_epoch,
        )
        if (causal_fields[0] is None) != (causal_fields[1] is None):
            raise ValueError("runtime event causation must be complete")
        if causal_fields[0] is not None and not causal_fields[0]:
            raise ValueError("runtime event causation identifier must be non-empty")
        if causal_fields[1] is not None and (
            type(causal_fields[1]) is not int or causal_fields[1] < 1
        ):
            raise ValueError("runtime event causation epoch must be positive")

    @property
    def durable_idempotency_key(self) -> str | None:
        """Return an idempotency identity only for replayable durable events."""
        return self.event_id if self.durability == EventDurability.DURABLE else None
