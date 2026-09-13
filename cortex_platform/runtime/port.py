"""Cortex-owned runtime boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .models import (
    ActionOutcomeQuery,
    AttemptRequest,
    ControlRequest,
    ControlResult,
    DecisionResolution,
    DecisionResult,
    ReleasePin,
    RuntimeActionOutcome,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeHandshake,
    RuntimeHealth,
    SessionInspection,
    SessionOpenRequest,
)


class RuntimePort(Protocol):
    """Runtime operations over control-plane identities already committed by Cortex.

    The caller persists run/attempt and decision state before invoking the
    corresponding runtime operation. Recovery accepts only a checkpoint handle
    that the Cortex control store has already committed.
    """

    async def capabilities(self) -> RuntimeCapabilities: ...

    async def health(self) -> RuntimeHealth: ...

    async def handshake(self, pin: ReleasePin) -> RuntimeHandshake: ...

    async def open_session(self, request: SessionOpenRequest) -> RuntimeBinding: ...

    async def load_session(self, binding: RuntimeBinding) -> RuntimeBinding: ...

    async def fork_session(
        self, binding: RuntimeBinding, request: SessionOpenRequest
    ) -> RuntimeBinding: ...

    def execute(self, request: AttemptRequest) -> AsyncIterator[RuntimeEvent]: ...

    async def request_control(self, request: ControlRequest) -> ControlResult: ...

    async def resolve_decision(self, resolution: DecisionResolution) -> DecisionResult:
        """Forward a resolution only after Cortex commits its revision CAS."""
        ...

    async def query_action_outcome(
        self, query: ActionOutcomeQuery
    ) -> RuntimeActionOutcome:
        """Inspect an adapter operation without repeating its side effect."""
        ...

    async def inspect(self, binding: RuntimeBinding) -> SessionInspection: ...

    async def recover(
        self, binding: RuntimeBinding, checkpoint: RuntimeCheckpoint
    ) -> RuntimeBinding:
        """Recover from a checkpoint already committed by Cortex."""
        ...
