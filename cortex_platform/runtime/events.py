"""Translation from Hermes callback signals to Cortex runtime events."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Mapping

from .models import AttemptRequest, EventDurability, RuntimeEvent


class RuntimeEventFactory:
    """Create adapter events with durability-scoped sequence identities."""

    def __init__(self, request: AttemptRequest) -> None:
        self.request = request
        self._durable_sequence = 0
        self._transient_sequence = 0

    def event(
        self,
        event_type: str,
        durability: EventDurability,
        payload: Mapping[str, Any],
        *,
        stable_source_id: str | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
    ) -> RuntimeEvent:
        if durability == EventDurability.DURABLE:
            sequence = self._durable_sequence
            self._durable_sequence += 1
            source = stable_source_id or f"sequence:{sequence}"
            digest = hashlib.sha256(
                "\x1f".join(
                    (
                        self.request.adapter_operation_id,
                        event_type,
                        source,
                    )
                ).encode("utf-8")
            ).hexdigest()
            event_id = f"runtime-event-{digest}"
        else:
            sequence = self._transient_sequence
            self._transient_sequence += 1
            event_id = f"runtime-transient-{uuid.uuid4().hex}"
        return RuntimeEvent(
            run_id=self.request.run_id,
            attempt_id=self.request.attempt_id,
            type=event_type,
            durability=durability,
            payload=payload,
            event_id=event_id,
            event_sequence=sequence,
            caused_by_adapter_operation_id=caused_by_adapter_operation_id,
            caused_by_delivery_epoch=caused_by_delivery_epoch,
        )


def translate_hermes_signal(
    factory: RuntimeEventFactory,
    kind: str,
    payload: Mapping[str, Any],
    *,
    stable_source_id: str | None = None,
) -> RuntimeEvent:
    """Translate one Hermes-private callback without exposing hidden reasoning."""
    if kind == "token.delta":
        return factory.event(
            "runtime.token.delta",
            EventDurability.TRANSIENT,
            {"text": str(payload.get("text", ""))},
        )
    if kind == "tool.started":
        return factory.event(
            "runtime.tool.started",
            EventDurability.DURABLE,
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "arguments": payload.get("arguments", {}),
            },
            stable_source_id=stable_source_id,
        )
    if kind == "tool.progress":
        return factory.event(
            "runtime.tool.progress",
            EventDurability.TRANSIENT,
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "message": str(payload.get("message", ""))[:500],
            },
        )
    if kind == "tool.completed":
        return factory.event(
            "runtime.tool.completed",
            EventDurability.DURABLE,
            {
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "is_error": bool(payload.get("is_error", False)),
                "duration_ms": payload.get("duration_ms"),
            },
            stable_source_id=stable_source_id,
        )
    if kind == "decision.required":
        return factory.event(
            "runtime.decision.required",
            EventDurability.DURABLE,
            {
                "decision_id": payload.get("decision_id"),
                "kind": payload.get("decision_kind", "approval"),
                "prompt": payload.get("prompt", "Runtime approval required"),
                "command": payload.get("command"),
                "description": payload.get("description"),
                "options": ["approve_once", "deny"],
                "revision": 0,
            },
            stable_source_id=stable_source_id,
        )
    if kind in {
        "_thinking",
        "reasoning.available",
        "thinking",
        "delegate.task_thinking",
        "subagent.thinking",
    }:
        return factory.event(
            "runtime.status",
            EventDurability.TRANSIENT,
            {"phase": "reasoning.available"},
        )
    if kind in {"step.completed", "status"}:
        return factory.event(
            "runtime.status",
            EventDurability.TRANSIENT,
            {"phase": kind},
        )
    return factory.event(
        "runtime.hermes.unknown",
        EventDurability.TRANSIENT,
        {},
    )
