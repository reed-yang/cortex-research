"""Declared Hermes compatibility matrix and probe results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..product.runtime_update.worker_payload.cortex_worker.approval import (
    APPROVAL_CHOICES,
    STANDING_CHOICES,
)


ADAPTER_ID = "hermes"
ADAPTER_VERSION = "0.2.0"
SUPPORTED_HERMES_DISTRIBUTIONS = ("0.15.0",)
SUPPORTED_SESSION_DB_SCHEMA = (13, 13)


COMPATIBILITY_MATRIX: Mapping[str, Any] = {
    "fork": "NousResearch/hermes-agent v2026.5.28 plus Cortex patches",
    "distribution_versions": SUPPORTED_HERMES_DISTRIBUTIONS,
    "session_db_schema": {
        "minimum": SUPPORTED_SESSION_DB_SCHEMA[0],
        "maximum": SUPPORTED_SESSION_DB_SCHEMA[1],
    },
    "callback_shapes": {
        "tool_progress_callback": "(event, tool_name, preview, args, **kwargs)",
        "tool_start_callback": "(tool_call_id, tool_name, args)",
        "tool_complete_callback": "(tool_call_id, tool_name, args, result)",
        "stream_callback": "(text_delta)",
        "step_callback": "(iteration, previous_tools)",
        "approval_callback": (
            "(command, description, *, allow_permanent=True) -> "
            + "|".join(APPROVAL_CHOICES)
        ),
        "interrupt_state": "AIAgent.is_interrupted property|bound callable",
    },
    "control": {
        "interrupt": "AIAgent.interrupt(message=None)",
        "steer": "AIAgent.steer(text) -> bool",
        "pause": "unsupported; never mapped to interrupt",
        "decision_wait": "explicit Cortex resolution or cancellation only",
    },
    "session_rotation": "AIAgent.session_id may rotate after compression",
    # ⟦S3.4⟧ Read from the closed set rather than restating it. This line was a
    # promise for four slices while `ForkRunner.approval` returned whatever
    # string arrived over the channel; now the promise and the enforcement are
    # the same tuple.
    "approval_bridge": (
        "thread-local callback; never returns " + "|".join(STANDING_CHOICES)
    ),
    "reasoning_privacy": (
        "_thinking|reasoning.available|delegate.task_thinking|"
        "subagent.thinking emit availability only; text and args are dropped"
    ),
    "tool_completion": (
        "tool.completed progress is authoritative for is_error|duration; "
        "structured completion supplies tool_call_id and raw result is dropped"
    ),
    "run_result": (
        "failed=true|partial=true|completed=false map to sanitized runtime failure; "
        "missing completion flags remain compatible success"
    ),
    "provider_models": "reported from configured backend capabilities",
    "event_identity": (
        "durable events use attempt-scoped stable IDs and durability-scoped "
        "monotonic sequences; transient events are never idempotency keys"
    ),
    "operation_identity": (
        "session/control/decision effects require stable adapter operation IDs; "
        "delivery epochs fence retries and action outcomes are queryable"
    ),
    "worker_identity": (
        "release/state-generation/slot/artifact/protocol handshake must match exactly"
    ),
    "durable_operation_deduplication": (
        "unsupported by the verified in-process Hermes seam; activation gate"
    ),
}


@dataclass(frozen=True)
class CompatibilityCheck:
    name: str
    expected: str
    observed: str
    compatible: bool
    required: bool = True


@dataclass(frozen=True)
class CompatibilityReport:
    runtime_version: str | None
    session_db_schema: int | None
    checks: tuple[CompatibilityCheck, ...]
    provider_models: Mapping[str, tuple[str, ...]]

    @property
    def compatible(self) -> bool:
        return all(check.compatible for check in self.checks if check.required)

    def sanitized_summary(self) -> Mapping[str, Any]:
        return {
            "runtime_version": self.runtime_version,
            "session_db_schema": self.session_db_schema,
            "checks": [
                {
                    "name": check.name,
                    "expected": check.expected,
                    "observed": check.observed,
                    "compatible": check.compatible,
                    "required": check.required,
                }
                for check in self.checks
            ],
            "provider_models": {
                provider: list(models)
                for provider, models in self.provider_models.items()
            },
        }
