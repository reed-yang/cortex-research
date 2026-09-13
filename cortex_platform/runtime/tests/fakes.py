"""Deterministic Hermes backend fake for runtime contract tests."""

from __future__ import annotations

import threading
from typing import Any, Mapping

from cortex_platform.runtime.compatibility import (
    ADAPTER_ID,
    CompatibilityCheck,
    CompatibilityReport,
)
from cortex_platform.runtime.hermes import (
    HermesInspection,
    HermesRunInput,
    HermesRunResult,
    HermesSession,
    HermesSignal,
)
from cortex_platform.runtime.models import RuntimeCapabilities, RuntimeCheckpoint
from cortex_platform.runtime.models import (
    ActionOutcomeStatus,
    RuntimeActionOutcome,
    RuntimeReleaseIdentity,
)


FAKE_RUNTIME_IDENTITY = RuntimeReleaseIdentity(
    release_id="hermes-test-0.15.0",
    state_generation_id="test-state-generation-1",
    slot_id="test-slot-1",
    artifact_digest=f"sha256:{'a' * 64}",
    worker_protocol="cortex-runtime-worker/1",
)


class FakeHermesBackend:
    def __init__(
        self,
        *,
        compatible: bool = True,
        mode: str = "complete",
        rotate_session: bool = False,
        block_resolution: bool = False,
        fail_resolutions: int = 0,
    ) -> None:
        self._compatible = compatible
        self.mode = mode
        self.rotate_session = rotate_session
        self.block_resolution = block_resolution
        self.fail_resolutions = fail_resolutions
        self.sessions: dict[str, str | None] = {}
        self.messages: dict[str, list[Mapping[str, Any]]] = {}
        self.active = threading.Event()
        self.canceled = threading.Event()
        self.pending = threading.Event()
        self.pending_identity: tuple[str, str] | None = None
        self.resolved = threading.Event()
        self.decision_choice: str | None = None
        self.resolve_calls = 0
        self.resolve_entered = threading.Event()
        self.release_resolution = threading.Event()
        self.cancel_calls = 0
        self.run_calls = 0
        self.steer_texts: list[str] = []
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str], object] = {}
        self._cancel_requested: set[tuple[str, str]] = set()
        self._operations_lock = threading.Lock()
        self._session_operations: dict[str, tuple[tuple[str, ...], HermesSession]] = {}
        self._action_operations: dict[
            str, tuple[tuple[str, ...], int, RuntimeActionOutcome]
        ] = {}

    def runtime_identity(self) -> RuntimeReleaseIdentity | None:
        return FAKE_RUNTIME_IDENTITY

    def reserve_attempt(self, run_id: str, attempt_id: str) -> object | None:
        key = (run_id, attempt_id)
        with self._lock:
            if key in self._leases:
                return None
            token = object()
            self._leases[key] = token
            return token

    def release_attempt(
        self, run_id: str, attempt_id: str, execution_token: object
    ) -> None:
        key = (run_id, attempt_id)
        with self._lock:
            if self._leases.get(key) is execution_token:
                self._leases.pop(key, None)
                self._cancel_requested.discard(key)

    def compatibility(self) -> CompatibilityReport:
        return CompatibilityReport(
            runtime_version="0.15.0",
            session_db_schema=13,
            checks=(
                CompatibilityCheck(
                    "fake_surface",
                    "compatible",
                    "compatible" if self._compatible else "incompatible",
                    self._compatible,
                ),
            ),
            provider_models={"fake": ("test-model",)},
        )

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            adapter_id=ADAPTER_ID,
            available=self._compatible,
            session_create=self._compatible,
            session_load=self._compatible,
            session_fork=self._compatible,
            run_stream=self._compatible,
            cancel=self._compatible,
            pause=False,
            steer=self._compatible,
            decisions=self._compatible,
            session_rebinding=self._compatible,
            checkpoint_recovery=self._compatible,
            action_outcome_query=self._compatible,
            durable_operation_deduplication=False,
            provider_models={"fake": ("test-model",)},
        )

    def open_session(
        self, metadata: Mapping[str, Any], adapter_operation_id: str = ""
    ) -> HermesSession:
        _ = metadata
        adapter_operation_id = adapter_operation_id or "legacy-session-open"
        identity = ("session.open",)
        with self._operations_lock:
            previous = self._session_operations.get(adapter_operation_id)
            if previous is not None:
                if previous[0] != identity:
                    raise RuntimeError("adapter_operation_conflict")
                return previous[1]
            session_ref = f"session-{len(self.sessions) + 1}"
            self.sessions[session_ref] = None
            self.messages[session_ref] = []
            session = HermesSession(session_ref)
            self._session_operations[adapter_operation_id] = (identity, session)
            return session

    def load_session(self, session_ref: str) -> HermesSession:
        if session_ref not in self.sessions:
            raise LookupError(session_ref)
        return HermesSession(session_ref, self.sessions[session_ref])

    def fork_session(
        self,
        session_ref: str,
        metadata: Mapping[str, Any],
        adapter_operation_id: str = "",
    ) -> HermesSession:
        _ = metadata
        adapter_operation_id = adapter_operation_id or f"legacy-fork:{session_ref}"
        identity = ("session.fork", session_ref)
        with self._operations_lock:
            previous = self._session_operations.get(adapter_operation_id)
            if previous is not None:
                if previous[0] != identity:
                    raise RuntimeError("adapter_operation_conflict")
                return previous[1]
            self.load_session(session_ref)
            child_ref = f"session-{len(self.sessions) + 1}"
            self.sessions[child_ref] = session_ref
            self.messages[child_ref] = list(self.messages[session_ref])
            session = HermesSession(child_ref, session_ref)
            self._session_operations[adapter_operation_id] = (identity, session)
            return session

    def run(self, request: HermesRunInput, emit) -> HermesRunResult:
        key = (request.run_id, request.attempt_id)
        with self._lock:
            if self._leases.get(key) is not request.execution_token:
                raise RuntimeError("execution lease mismatch")
            canceled_before_start = key in self._cancel_requested
        self.run_calls += 1
        if canceled_before_start:
            return HermesRunResult(
                request.session_ref, final_response=None, canceled=True
            )
        self.active.set()
        if self.mode == "run_error":
            raise RuntimeError("backend run failed")
        if self.mode == "cancel_wait":
            self.canceled.wait(2)
            return HermesRunResult(
                request.session_ref, final_response=None, canceled=True
            )

        emit(HermesSignal("token.delta", {"text": "Hel"}))
        emit(
            HermesSignal(
                "tool.started",
                {
                    "tool_call_id": "tool-1",
                    "tool_name": "paper_search",
                    "arguments": {"query": "memory"},
                },
                stable_id="tool:tool-1:started",
            )
        )
        emit(
            HermesSignal(
                "tool.progress",
                {
                    "tool_call_id": "tool-1",
                    "tool_name": "paper_search",
                    "message": "Searching",
                },
                stable_id="tool:tool-1:completed",
            )
        )
        emit(HermesSignal("reasoning.available", {"text": "hidden chain"}))
        emit(
            HermesSignal(
                "tool.completed",
                {
                    "tool_call_id": "tool-1",
                    "tool_name": "paper_search",
                    "result": "private raw result",
                    "duration_ms": 12,
                },
                stable_id="decision:decision-1:required",
            )
        )
        emit(
            HermesSignal(
                "decision.required",
                {
                    "decision_id": "decision-1",
                    "decision_kind": "approval",
                    "prompt": "Approve?",
                    "command": "safe command",
                    "description": "test approval",
                },
            )
        )
        self.arm_decision(request.run_id, request.attempt_id)
        self.resolved.wait(2)
        session_ref = (
            f"{request.session_ref}-rotated"
            if self.rotate_session
            else request.session_ref
        )
        return HermesRunResult(session_ref, "Hello", canceled=False)

    def _action(
        self,
        identity: tuple[str, ...],
        adapter_operation_id: str,
        delivery_epoch: int,
        effect,
        rejected_reason: str,
    ) -> RuntimeActionOutcome:
        with self._operations_lock:
            previous = self._action_operations.get(adapter_operation_id)
            if previous is not None:
                if previous[0] != identity:
                    return RuntimeActionOutcome(
                        adapter_operation_id,
                        delivery_epoch,
                        ActionOutcomeStatus.REJECTED,
                        "adapter_operation_conflict",
                    )
                if delivery_epoch < previous[1]:
                    return RuntimeActionOutcome(
                        adapter_operation_id,
                        delivery_epoch,
                        ActionOutcomeStatus.REJECTED,
                        "stale_delivery_epoch",
                    )
                if previous[2].status == ActionOutcomeStatus.ACCEPTED:
                    return RuntimeActionOutcome(
                        adapter_operation_id,
                        delivery_epoch,
                        ActionOutcomeStatus.DEDUPLICATED,
                    )
                if delivery_epoch == previous[1]:
                    return previous[2]
            accepted = effect()
            outcome = RuntimeActionOutcome(
                adapter_operation_id,
                delivery_epoch,
                ActionOutcomeStatus.ACCEPTED
                if accepted
                else ActionOutcomeStatus.REJECTED,
                None if accepted else rejected_reason,
            )
            self._action_operations[adapter_operation_id] = (
                identity,
                delivery_epoch,
                outcome,
            )
            return outcome

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str = "",
    ) -> RuntimeActionOutcome:
        def effect() -> bool:
            self.cancel_calls += 1
            key = (run_id, attempt_id)
            with self._lock:
                if key not in self._leases:
                    return False
                self._cancel_requested.add(key)
            self.canceled.set()
            self.resolved.set()
            return True

        return self._action(
            ("control.cancel", session_ref, run_id, attempt_id),
            adapter_operation_id,
            delivery_epoch,
            effect,
            "run_not_active",
        )

    def steer(
        self,
        run_id: str,
        attempt_id: str,
        text: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str = "",
    ) -> RuntimeActionOutcome:
        def effect() -> bool:
            if not self.active.is_set():
                return False
            self.steer_texts.append(text)
            return True

        return self._action(
            ("control.steer", session_ref, run_id, attempt_id, text),
            adapter_operation_id,
            delivery_epoch,
            effect,
            "run_not_active",
        )

    def arm_decision(self, run_id: str, attempt_id: str) -> None:
        self.pending_identity = (run_id, attempt_id)
        self.pending.set()

    def resolve_decision(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        choice: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome:
        def effect() -> bool:
            if (
                decision_id != "decision-1"
                or not self.pending.is_set()
                or self.pending_identity != (run_id, attempt_id)
            ):
                return False
            self.resolve_calls += 1
            self.resolve_entered.set()
            if self.block_resolution:
                self.release_resolution.wait(2)
            if self.fail_resolutions:
                self.fail_resolutions -= 1
                raise RuntimeError("transient backend failure")
            self.decision_choice = choice
            self.resolved.set()
            return True

        return self._action(
            (
                "decision.resolve",
                session_ref,
                run_id,
                attempt_id,
                decision_id,
                choice,
            ),
            adapter_operation_id,
            delivery_epoch,
            effect,
            "pending_decision_missing",
        )

    def query_action_outcome(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome:
        with self._operations_lock:
            previous = self._action_operations.get(adapter_operation_id)
            if previous is None:
                return RuntimeActionOutcome(
                    adapter_operation_id,
                    delivery_epoch,
                    ActionOutcomeStatus.UNKNOWN,
                    "operation_outcome_unknown",
                )
            if len(previous[0]) < 4 or previous[0][1:4] != (
                session_ref,
                run_id,
                attempt_id,
            ):
                return RuntimeActionOutcome(
                    adapter_operation_id,
                    delivery_epoch,
                    ActionOutcomeStatus.REJECTED,
                    "adapter_operation_conflict",
                )
            if delivery_epoch < previous[1]:
                return RuntimeActionOutcome(
                    adapter_operation_id,
                    delivery_epoch,
                    ActionOutcomeStatus.REJECTED,
                    "stale_delivery_epoch",
                )
            return previous[2]

    def inspect(self, session_ref: str) -> HermesInspection:
        return HermesInspection(
            exists=session_ref in self.sessions,
            active=self.active.is_set() and not self.canceled.is_set(),
            metadata={"source": "fake"},
        )

    def recover(
        self,
        session_ref: str,
        checkpoint: RuntimeCheckpoint,
        adapter_operation_id: str = "",
    ) -> HermesSession:
        adapter_operation_id = adapter_operation_id or checkpoint.adapter_operation_id
        identity = ("session.recover", session_ref, checkpoint.checkpoint_ref)
        with self._operations_lock:
            previous = self._session_operations.get(adapter_operation_id)
            if previous is not None:
                if previous[0] != identity:
                    raise RuntimeError("adapter_operation_conflict")
                return previous[1]
            self.load_session(session_ref)
            child_ref = f"session-{len(self.sessions) + 1}"
            self.sessions[child_ref] = session_ref
            self.messages[child_ref] = list(checkpoint.conversation_history)
            recovered = HermesSession(child_ref, session_ref)
            self._session_operations[adapter_operation_id] = (identity, recovered)
            return recovered
