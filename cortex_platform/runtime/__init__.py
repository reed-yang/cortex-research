"""Cortex-owned runtime abstractions.

Hermes is deliberately not imported from this module. Importing Cortex runtime
types remains safe when Hermes is missing or incompatible.
"""

from .compatibility import COMPATIBILITY_MATRIX
from .hermes import HermesAdapter, RuntimeOperationUncertain
from .managed_hermes import (
    DedupProbeFailed,
    ManagedHermesBackend,
    ManagedRuntimeUnavailable,
    ProbeProvenance,
    load_managed_backend,
)
from .models import (
    ActionOutcomeQuery,
    ActionOutcomeStatus,
    AttemptRequest,
    ControlAction,
    ControlRequest,
    ControlResult,
    DecisionResolution,
    DecisionResult,
    EventDurability,
    HealthStatus,
    ReleasePin,
    RuntimeActionOutcome,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeHandshake,
    RuntimeHealth,
    RuntimeReleaseIdentity,
    SessionInspection,
    SessionOpenRequest,
)
from .port import RuntimePort

__all__ = [
    "ActionOutcomeQuery",
    "ActionOutcomeStatus",
    "AttemptRequest",
    "COMPATIBILITY_MATRIX",
    "ControlAction",
    "ControlRequest",
    "ControlResult",
    "DecisionResolution",
    "DecisionResult",
    "EventDurability",
    "HealthStatus",
    "DedupProbeFailed",
    "HermesAdapter",
    "ManagedHermesBackend",
    "ManagedRuntimeUnavailable",
    "ProbeProvenance",
    "ReleasePin",
    "RuntimeActionOutcome",
    "RuntimeBinding",
    "RuntimeCapabilities",
    "RuntimeCheckpoint",
    "RuntimeEvent",
    "RuntimeHandshake",
    "RuntimeHealth",
    "RuntimeOperationUncertain",
    "RuntimeReleaseIdentity",
    "RuntimePort",
    "SessionInspection",
    "SessionOpenRequest",
    "load_managed_backend",
]
