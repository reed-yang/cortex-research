from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.orchestration import RunOrchestrator
from cortex_platform.runtime import ActionOutcomeStatus, RuntimeActionOutcome
from cortex_platform.runtime.hermes import HermesAdapter as BaseHermesAdapter
from cortex_platform.runtime.hermes import HermesRunResult, HermesSignal
from cortex_platform.runtime.tests.fakes import (
    FAKE_RUNTIME_IDENTITY,
    FakeHermesBackend,
)


class HermesAdapter(BaseHermesAdapter):
    """Test adapter that models a certified durable worker."""

    async def capabilities(self):
        capabilities = await super().capabilities()
        return replace(
            capabilities,
            durable_operation_deduplication=True,
        )


@dataclass(frozen=True)
class Pin:
    attempt_id: str
    release_id: str = FAKE_RUNTIME_IDENTITY.release_id
    generation_id: str = FAKE_RUNTIME_IDENTITY.state_generation_id
    slot_id: str = FAKE_RUNTIME_IDENTITY.slot_id
    artifact_digest: str = FAKE_RUNTIME_IDENTITY.artifact_digest
    worker_protocol: str = FAKE_RUNTIME_IDENTITY.worker_protocol


class Releases:
    def __init__(self, *, fail_finish: int = 0) -> None:
        self.pins: list[Pin] = []
        self.finished: list[Pin] = []
        self.fail_finish = fail_finish

    def preview_attempt_pin(self, attempt_id: str) -> Pin:
        existing = self.attempt_pin(attempt_id)
        if existing is not None:
            return existing
        return Pin(attempt_id)

    def pin_attempt(
        self, attempt_id: str, expected_pin: Pin | None = None
    ) -> Pin:
        existing = self.attempt_pin(attempt_id)
        if existing is not None:
            return existing
        pin = Pin(attempt_id)
        assert expected_pin is None or expected_pin == pin
        self.pins.append(pin)
        return pin

    def finish_attempt(self, attempt_id: str, pin: Pin) -> None:
        assert attempt_id == pin.attempt_id
        if self.fail_finish:
            self.fail_finish -= 1
            raise RuntimeError("injected pin release failure")
        self.finished.append(pin)

    def attempt_pin(self, attempt_id: str) -> Pin | None:
        return next(
            (
                pin
                for pin in self.pins
                if pin.attempt_id == attempt_id and pin not in self.finished
            ),
            None,
        )


class GatedRuntime:
    def __init__(self, inner: HermesAdapter) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def capabilities(self):
        self.entered.set()
        await self.release.wait()
        return await self.inner.capabilities()

    def __getattr__(self, name):
        return getattr(self.inner, name)


class LostOutcomeRuntime:
    def __init__(
        self,
        inner: HermesAdapter,
        *,
        unknown_queries: int = 0,
        unknown_reason: str = "injected_outcome_unknown",
    ) -> None:
        self.inner = inner
        self.unknown_queries = unknown_queries
        self.unknown_reason = unknown_reason

    async def request_control(self, request):
        await self.inner.request_control(request)
        raise RuntimeError("injected lost response")

    async def query_action_outcome(self, query):
        if self.unknown_queries:
            self.unknown_queries -= 1
            return RuntimeActionOutcome(
                query.adapter_operation_id,
                query.delivery_epoch,
                ActionOutcomeStatus.UNKNOWN,
                self.unknown_reason,
            )
        return await self.inner.query_action_outcome(query)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class InvalidOutcomeIdentityRuntime:
    def __init__(self, inner: HermesAdapter, *, fault: str) -> None:
        self.inner = inner
        self.fault = fault

    async def request_control(self, request):
        await self.inner.request_control(request)
        raise RuntimeError("injected lost response")

    async def query_action_outcome(self, query):
        operation_id = query.adapter_operation_id
        delivery_epoch = query.delivery_epoch
        if self.fault == "operation_id":
            operation_id = f"{operation_id}:wrong"
        elif self.fault == "future_epoch":
            delivery_epoch += 1
        return RuntimeActionOutcome(
            operation_id,
            delivery_epoch,
            ActionOutcomeStatus.ACCEPTED,
        )

    def __getattr__(self, name):
        return getattr(self.inner, name)


class DelayedActionResponseRuntime:
    def __init__(self, inner: HermesAdapter) -> None:
        self.inner = inner
        self.effect_committed = asyncio.Event()
        self.release_response = asyncio.Event()

    async def request_control(self, request):
        result = await self.inner.request_control(request)
        self.effect_committed.set()
        await self.release_response.wait()
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)


class DelayedDecisionResponseRuntime:
    def __init__(
        self, inner: HermesAdapter, store: ControlStore, run_id: str
    ) -> None:
        self.inner = inner
        self.store = store
        self.run_id = run_id

    async def resolve_decision(self, resolution):
        result = await self.inner.resolve_decision(resolution)
        while self.store.get_run(self.run_id)["state"] == "resuming":
            await asyncio.sleep(0)
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)


class DeferredCancelBackend(FakeHermesBackend):
    def cancel(
        self,
        run_id,
        attempt_id,
        adapter_operation_id,
        delivery_epoch,
        session_ref="",
    ):
        def effect() -> bool:
            self.cancel_calls += 1
            with self._lock:
                return (run_id, attempt_id) in self._leases

        return self._action(
            ("control.cancel", session_ref, run_id, attempt_id),
            adapter_operation_id,
            delivery_epoch,
            effect,
            "run_not_active",
        )


def _queued_run(store: ControlStore) -> dict:
    # These tests exercise dispatch mechanics, so they need the activation
    # gate open. The gate itself is default-off and covered by its own suite
    # (`control/test_activation_gate.py`) plus the refusal test below, which
    # proves a closed gate reaches the runtime port zero times.
    store.enable_runtime_activation(
        mode="permanent",
        actor_id="test-operator",
        idempotency_key="activate-for-dispatch01",
    )
    workspace = store.create_workspace(
        title="Research",
        actor_id="local",
        idempotency_key="workspace-command-0001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Echo",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key="thread-command-000001",
    ).value
    store.append_message(
        thread_id=thread["id"],
        role="user",
        content="Research memory architectures",
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key="message-command-0001",
    )
    current_thread = store.get_thread(thread["id"])
    return store.create_run(
        thread_id=thread["id"],
        expected_revision=current_thread["revision"],
        actor_id="local",
        idempotency_key="run-command-0000001",
    ).value


def _paused_run_with_pin(
    store: ControlStore,
    releases: Releases,
    backend: FakeHermesBackend,
) -> tuple[dict, Pin]:
    run = _queued_run(store)
    attempt_id = str(run["attempt"]["id"])
    pin = releases.pin_attempt(attempt_id)
    session = backend.open_session({})
    binding = store.create_runtime_binding(
        thread_id=run["thread_id"],
        adapter_id="hermes",
        runtime_session_ref=session.session_ref,
        generation=0,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key="paused-source-binding-0001",
    ).value
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=attempt_id,
        dispatch_owner="paused-source-setup",
        runtime_release_id=pin.release_id,
        state_generation_id=pin.generation_id,
        runtime_slot_id=pin.slot_id,
        runtime_artifact_digest=pin.artifact_digest,
        runtime_worker_protocol=pin.worker_protocol,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key="paused-source-reserve-0001",
    ).value
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=attempt_id,
        runtime_binding_id=binding["id"],
        runtime_release_id=pin.release_id,
        state_generation_id=pin.generation_id,
        dispatch_owner="paused-source-setup",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key="paused-source-pin-0001",
    ).value
    for state in ("starting", "running"):
        run = store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=attempt_id,
            runtime_binding_id=binding["id"],
            runtime_release_id=pin.release_id,
            state_generation_id=pin.generation_id,
            target_state=state,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=f"paused-source-{state}-0001",
        ).value
    run = store.transition_run(
        run_id=run["id"],
        target_state="pause_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key="paused-source-request-0001",
    ).value
    run = store.commit_checkpoint(
        run_id=run["id"],
        attempt_id=attempt_id,
        runtime_binding_id=binding["id"],
        runtime_release_id=pin.release_id,
        state_generation_id=pin.generation_id,
        checkpoint_uri="cortex://artifacts/checkpoints/source.json",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key="paused-source-checkpoint-0001",
    ).value
    run = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=attempt_id,
        runtime_binding_id=binding["id"],
        runtime_release_id=pin.release_id,
        state_generation_id=pin.generation_id,
        target_state="paused",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key="paused-source-paused-0001",
    ).value
    return run, pin


def test_dispatch_commits_runtime_events_and_delivers_decision(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        runtime = HermesAdapter(backend_loader=lambda: backend)
        releases = Releases()
        orchestrator = RunOrchestrator(store, runtime, releases)

        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="decision-command-001",
        )
        action = store.list_pending_runtime_actions()[0]
        delivered = await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-one"
        )
        assert delivered["state"] == "acked"

        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "completed", [
            (event["type"], event["payload"])
            for event in store.list_run_events(run["id"])
        ]
        assert releases.finished == releases.pins
        messages = store.list_messages(completed["thread_id"])
        assert messages[-1]["content"] == "Hello"
        events = store.list_run_events(run["id"])
        event_types = [event["type"] for event in events]
        assert "run.completed" in event_types
        assert event_types[-1] == "runtime.pin_release.acked"
        assert "runtime.message.completed" in event_types
        tool_started = next(
            event for event in events if event["type"] == "runtime.tool.started"
        )
        assert "arguments" not in tool_started["payload"]
        assert "private raw result" not in repr(events)
        attempt = store.get_attempt(str(completed["active_attempt_id"]))
        assert attempt["runtime_release_id"] == FAKE_RUNTIME_IDENTITY.release_id
        assert (
            attempt["state_generation_id"]
            == FAKE_RUNTIME_IDENTITY.state_generation_id
        )
        assert backend.resolve_calls == 1

    asyncio.run(scenario())


def test_unavailable_runtime_fails_without_orphaning_release_pin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend(compatible=False)
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )

        failed = await orchestrator.dispatch(run["id"])
        assert failed["state"] == "failed"
        assert releases.finished == releases.pins
        assert store.get_thread(run["thread_id"])["active_run_id"] is None
        failed_event = next(
            event
            for event in store.list_run_events(run["id"])
            if event["type"] == "run.failed"
        )
        assert failed_event["payload"] == {
            "category": "runtime_unavailable",
            "from": "queued",
            "pin_release_action_id": failed_event["payload"][
                "pin_release_action_id"
            ],
            "retryable": True,
            "state": "failed",
        }

    asyncio.run(scenario())


def test_available_runtime_without_durable_deduplication_fails_before_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend(mode="run_error")
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            BaseHermesAdapter(backend_loader=lambda: backend),
            releases,
        )

        failed = await orchestrator.dispatch(run["id"])

        assert failed["state"] == "failed"
        assert releases.finished == releases.pins
        assert backend.sessions == {}
        assert backend.run_calls == 0
        failed_event = next(
            event
            for event in store.list_run_events(run["id"])
            if event["type"] == "run.failed"
        )
        assert (
            failed_event["payload"]["category"]
            == "runtime_durable_deduplication_unavailable"
        )

    asyncio.run(scenario())


def test_startup_recovery_leaves_a_run_parked_on_a_decision_alone(
    tmp_path: Path,
) -> None:
    """⟦P5.4d⟧ A parked run waits for the OPERATOR, not for a recovery.

    This test used to assert the opposite: that a restart failed the attempt
    and expired the decision. That was safe only while nothing could park --
    the fork never called the worker's approval callback, so
    `waiting_for_decision` was unreachable in a managed turn. ⟦F10⟧ made it
    reachable, and converging it at the next daemon start would answer away
    the very question the operator is being asked. The decision card the
    transport has already rendered stays answerable.

    The trade-off is recorded rather than hidden: the runtime session that
    parked does not survive the restart, so RESOLVING the decision has to
    re-dispatch. That belongs to the approval-vocabulary slice, which is the
    one that gives `/approve` and `/deny` a consumer at all.
    """

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        runtime = HermesAdapter(backend_loader=lambda: backend)
        session = backend.open_session({})
        binding = store.create_runtime_binding(
            thread_id=run["thread_id"],
            adapter_id="hermes",
            runtime_session_ref=session.session_ref,
            generation=0,
            adapter_version="test",
            actor_id="runtime",
            idempotency_key="binding-command-0001",
        ).value
        releases = Releases()
        pin = releases.pin_attempt(run["attempt"]["id"])
        dispatch_owner = "recovery-setup-owner"
        run = store.reserve_attempt_dispatch(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            dispatch_owner=dispatch_owner,
            runtime_release_id=pin.release_id,
            state_generation_id=pin.generation_id,
            runtime_slot_id=pin.slot_id,
            runtime_artifact_digest=pin.artifact_digest,
            runtime_worker_protocol=pin.worker_protocol,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key="runtime-reserve-command-1",
        ).value
        run = store.pin_attempt_runtime(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            runtime_binding_id=binding["id"],
            runtime_release_id=pin.release_id,
            state_generation_id=pin.generation_id,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key="runtime-pin-command-1",
            dispatch_owner=dispatch_owner,
        ).value
        for state in ("starting", "running"):
            run = store.apply_runtime_transition(
                run_id=run["id"],
                attempt_id=run["active_attempt_id"],
                runtime_binding_id=binding["id"],
                runtime_release_id=pin.release_id,
                state_generation_id=pin.generation_id,
                target_state=state,
                expected_revision=run["revision"],
                actor_id="runtime",
                idempotency_key=f"runtime-state-{state}",
            ).value
        decision = store.create_decision(
            run_id=run["id"],
            attempt_id=run["active_attempt_id"],
            runtime_binding_id=binding["id"],
            runtime_release_id=pin.release_id,
            state_generation_id=pin.generation_id,
            expected_revision=run["revision"],
            kind="approval",
            prompt="Approve?",
            options=[{"id": "approve_once"}, {"id": "deny"}],
            actor_id="runtime",
            idempotency_key="recovery-decision-01",
            runtime_decision_ref="runtime-decision-1",
            runtime_decision_revision=0,
        ).value

        reports = await RunOrchestrator(
            store, runtime, releases
        ).recover_startup()
        assert reports == []
        assert store.get_run(run["id"])["state"] == "waiting_for_decision"
        assert store.get_decision(decision["id"])["state"] == "pending"
        assert store.list_recoverable_runs() == []

    asyncio.run(scenario())


def test_cancel_command_uses_runtime_action_outbox(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend(mode="cancel_wait")
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 2)
        current = store.get_run(run["id"])
        requested = store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="cancel-command-0001",
        ).value
        action = store.list_pending_runtime_actions()[0]
        assert action["kind"] == "control.cancel"
        acked = await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-cancel"
        )
        assert acked["state"] == "acked"
        completed = await asyncio.wait_for(dispatch, 2)
        assert requested["state"] == "cancel_requested"
        assert completed["state"] == "canceled"
        assert backend.cancel_calls == 1
        assert releases.finished == releases.pins

    asyncio.run(scenario())


def test_pause_rejection_keeps_execution_and_release_pin_active(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend(mode="cancel_wait")
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 2)

        current = store.get_run(run["id"])
        store.transition_run(
            run_id=run["id"],
            target_state="pause_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="pause-rejected-command-0001",
        )
        pause_action = store.list_pending_runtime_actions()[0]
        rejected = await orchestrator.deliver_runtime_action(
            pause_action["id"], worker_id="worker-pause-rejected"
        )

        assert rejected["state"] == "failed"
        assert rejected["failure_category"] == (
            "pause_requires_cortex_stage_boundary"
        )
        assert store.get_run(run["id"])["state"] == "running"
        assert not dispatch.done()
        assert releases.finished == []
        assert releases.attempt_pin(run["attempt"]["id"]) is not None

        current = store.get_run(run["id"])
        store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="pause-rejected-cleanup-0001",
        )
        cancel_action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(
            cancel_action["id"], worker_id="worker-pause-cleanup"
        )
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "canceled"
        assert releases.finished == releases.pins

    asyncio.run(scenario())


def test_cancel_between_reservation_and_binding_converges_without_runtime_start(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        runtime = GatedRuntime(HermesAdapter(backend_loader=lambda: backend))
        releases = Releases()
        orchestrator = RunOrchestrator(store, runtime, releases)

        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        await asyncio.wait_for(runtime.entered.wait(), 2)
        reserved = store.get_run(run["id"])
        assert len(releases.pins) == 1
        store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=reserved["revision"],
            actor_id="local",
            idempotency_key="cancel-before-bind-0001",
        )

        runtime.release.set()
        canceled = await asyncio.wait_for(dispatch, 2)
        assert canceled["state"] == "canceled"
        assert backend.sessions == {}
        assert backend.run_calls == 0
        assert store.list_pending_runtime_actions() == []
        assert releases.finished == releases.pins
        assert await orchestrator.recover_startup() == []

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing", "mismatch"])
def test_resume_requires_exact_physical_checkpoint_source_pin(
    tmp_path: Path, fault: str
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / fault / "control.db")
        store.initialize()
        backend = FakeHermesBackend()
        releases = Releases()
        paused, source_pin = _paused_run_with_pin(store, releases, backend)
        resumed = store.resume_run(
            run_id=paused["id"],
            expected_revision=paused["revision"],
            actor_id="local",
            idempotency_key=f"resume-invalid-source-pin-{fault}",
        ).value
        if fault == "missing":
            releases.finish_attempt(source_pin.attempt_id, source_pin)
        else:
            releases.pins[0] = Pin(
                source_pin.attempt_id,
                release_id="hermes-test-0.16.0",
            )
        sessions_before = dict(backend.sessions)

        result = await RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        ).dispatch(resumed["id"])

        assert result["state"] == "resuming"
        assert result["active_attempt_id"] == resumed["attempt"]["id"]
        assert backend.sessions == sessions_before
        assert backend.run_calls == 0
        assert releases.attempt_pin(resumed["attempt"]["id"]) is None

    asyncio.run(scenario())


def test_duplicate_dispatch_is_fenced_before_runtime_side_effects(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        runtime = GatedRuntime(HermesAdapter(backend_loader=lambda: backend))
        releases = Releases()
        orchestrator = RunOrchestrator(store, runtime, releases)

        first = asyncio.create_task(orchestrator.dispatch(run["id"]))
        await asyncio.wait_for(runtime.entered.wait(), 2)
        duplicate = await orchestrator.dispatch(run["id"])
        assert duplicate["state"] == "queued"
        assert len(releases.pins) == 1
        assert backend.sessions == {}

        runtime.release.set()
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="decision-command-duplicate-01",
        )
        action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-duplicate"
        )
        completed = await asyncio.wait_for(first, 2)
        assert completed["state"] == "completed"
        assert backend.run_calls == 1
        assert len(backend.sessions) == 1

    asyncio.run(scenario())


def test_lost_action_response_reconciles_without_repeating_effect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = DeferredCancelBackend(mode="cancel_wait")
        runtime = LostOutcomeRuntime(
            HermesAdapter(backend_loader=lambda: backend),
            unknown_queries=1,
            unknown_reason="untrusted-worker-reason-" * 20,
        )
        releases = Releases()
        orchestrator = RunOrchestrator(store, runtime, releases)
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 2)
        current = store.get_run(run["id"])
        store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="cancel-lost-response-0001",
        )
        action = store.list_pending_runtime_actions()[0]
        unknown = await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-lost"
        )
        assert unknown["outcome_state"] == "outcome_unknown"
        assert backend.cancel_calls == 1

        deferred = await orchestrator.reconcile_runtime_action(
            action["id"], worker_id="worker-reconcile"
        )
        assert deferred["outcome_state"] == "outcome_unknown"
        assert deferred["failure_category"] == (
            "runtime_action_outcome_still_unknown"
        )
        assert backend.cancel_calls == 1
        assert [
            item["id"]
            for item in store.list_runtime_actions_requiring_reconciliation()
        ] == [action["id"]]

        acked = await orchestrator.reconcile_runtime_action(
            action["id"], worker_id="worker-reconcile-again"
        )
        assert acked["state"] == "acked"
        assert backend.cancel_calls == 1
        backend.canceled.set()
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "canceled"

    asyncio.run(scenario())


def test_reconciliation_rejects_unverified_outcome_identity(
    tmp_path: Path,
) -> None:
    async def scenario(fault: str) -> None:
        store = ControlStore(tmp_path / fault / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = DeferredCancelBackend(mode="cancel_wait")
        runtime = InvalidOutcomeIdentityRuntime(
            HermesAdapter(backend_loader=lambda: backend), fault=fault
        )
        orchestrator = RunOrchestrator(store, runtime, Releases())
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 2)
        current = store.get_run(run["id"])
        store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key=f"cancel-invalid-outcome-{fault}",
        )
        action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(
            action["id"], worker_id=f"worker-deliver-{fault}"
        )

        deferred = await orchestrator.reconcile_runtime_action(
            action["id"], worker_id=f"worker-reconcile-{fault}"
        )
        assert deferred["state"] == "pending"
        assert deferred["outcome_state"] == "outcome_unknown"
        assert deferred["failure_category"] == "runtime_action_identity_unverified"
        assert backend.cancel_calls == 1

        backend.canceled.set()
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "canceled"

    async def all_faults() -> None:
        for fault in ("operation_id", "future_epoch"):
            await scenario(fault)

    asyncio.run(all_faults())


def test_failed_pin_release_remains_durable_and_retryable(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        releases = Releases(fail_finish=1)
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="decision-pin-release-0001",
        )
        action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-pin-release"
        )
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "completed"
        assert releases.finished == []
        failed_event = next(
            event
            for event in store.list_run_events(run["id"])
            if event["type"] == "runtime.pin_release.failed"
        )
        action_id = failed_event["payload"]["pin_release_action_id"]
        store.retry_pin_release(
            release_action_id=action_id,
            expected_revision=store.get_run(run["id"])["revision"],
            actor_id="runtime",
            idempotency_key="retry-pin-release-0001",
        )
        delivered = orchestrator.deliver_pending_pin_releases(
            worker_id="worker-pin-release-retry"
        )
        assert delivered[-1]["state"] == "acked"
        assert releases.finished == releases.pins

    asyncio.run(scenario())


def test_terminal_event_racing_action_response_does_not_repeat_effect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend(mode="cancel_wait")
        runtime = DelayedActionResponseRuntime(
            HermesAdapter(backend_loader=lambda: backend)
        )
        orchestrator = RunOrchestrator(store, runtime, Releases())
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 2)
        current = store.get_run(run["id"])
        store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="cancel-response-race-0001",
        )
        action = store.list_pending_runtime_actions()[0]
        delivery = asyncio.create_task(
            orchestrator.deliver_runtime_action(
                action["id"], worker_id="worker-race"
            )
        )
        await asyncio.wait_for(runtime.effect_committed.wait(), 2)
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "canceled"
        runtime.release_response.set()
        settled_action = await asyncio.wait_for(delivery, 2)
        assert settled_action["state"] == "failed"
        assert backend.cancel_calls == 1

    asyncio.run(scenario())


def test_post_decision_event_causally_acks_delayed_action_response(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        runtime = DelayedDecisionResponseRuntime(
            HermesAdapter(backend_loader=lambda: backend), store, run["id"]
        )
        orchestrator = RunOrchestrator(store, runtime, Releases())
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="decision-response-race-0001",
        )
        action = store.list_pending_runtime_actions()[0]
        delivered = await asyncio.wait_for(
            orchestrator.deliver_runtime_action(
                action["id"], worker_id="worker-decision-race"
            ),
            2,
        )
        completed = await asyncio.wait_for(dispatch, 2)

        assert delivered["state"] == "acked"
        assert completed["state"] == "completed"
        assert store.list_messages(completed["thread_id"])[-1]["content"] == "Hello"
        assert backend.resolve_calls == 1
        assert not any(
            event["type"] == "run.failed"
            for event in store.list_run_events(run["id"])
        )

    asyncio.run(scenario())


def test_pause_resume_dispatch_completes_and_releases_both_attempt_pins(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        runtime = HermesAdapter(backend_loader=lambda: backend)
        releases = Releases()
        original_attempt_id = run["attempt"]["id"]
        original_pin = releases.pin_attempt(original_attempt_id)
        session = backend.open_session({})
        binding = store.create_runtime_binding(
            thread_id=run["thread_id"],
            adapter_id="hermes",
            runtime_session_ref=session.session_ref,
            generation=0,
            adapter_version="test",
            actor_id="runtime",
            idempotency_key="resume-binding-0000001",
        ).value
        run = store.reserve_attempt_dispatch(
            run_id=run["id"],
            attempt_id=original_attempt_id,
            dispatch_owner="resume-setup",
            runtime_release_id=original_pin.release_id,
            state_generation_id=original_pin.generation_id,
            runtime_slot_id=original_pin.slot_id,
            runtime_artifact_digest=original_pin.artifact_digest,
            runtime_worker_protocol=original_pin.worker_protocol,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key="resume-reserve-0000001",
        ).value
        run = store.pin_attempt_runtime(
            run_id=run["id"],
            attempt_id=original_attempt_id,
            runtime_binding_id=binding["id"],
            runtime_release_id=original_pin.release_id,
            state_generation_id=original_pin.generation_id,
            dispatch_owner="resume-setup",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key="resume-pin-0000000001",
        ).value
        for state in ("starting", "running", "pause_requested"):
            run = store.apply_runtime_transition(
                run_id=run["id"],
                attempt_id=original_attempt_id,
                runtime_binding_id=binding["id"],
                runtime_release_id=original_pin.release_id,
                state_generation_id=original_pin.generation_id,
                target_state=state,
                expected_revision=run["revision"],
                actor_id="runtime",
                idempotency_key=f"resume-state-{state}",
            ).value
        run = store.commit_checkpoint(
            run_id=run["id"],
            attempt_id=original_attempt_id,
            runtime_binding_id=binding["id"],
            runtime_release_id=original_pin.release_id,
            state_generation_id=original_pin.generation_id,
            checkpoint_uri="cortex://artifacts/checkpoints/resume.json",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key="resume-checkpoint-0001",
        ).value
        run = store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=original_attempt_id,
            runtime_binding_id=binding["id"],
            runtime_release_id=original_pin.release_id,
            state_generation_id=original_pin.generation_id,
            target_state="paused",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key="resume-paused-00000001",
        ).value
        run = store.resume_run(
            run_id=run["id"],
            expected_revision=run["revision"],
            actor_id="local",
            idempotency_key="resume-command-0000001",
        ).value
        resumed_attempt_id = run["attempt"]["id"]
        orchestrator = RunOrchestrator(store, runtime, releases)

        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="resume-decision-000001",
        )
        action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-resume"
        )
        completed = await asyncio.wait_for(dispatch, 2)

        assert completed["state"] == "completed"
        assert {pin.attempt_id for pin in releases.pins} == {
            original_attempt_id,
            resumed_attempt_id,
        }
        assert {pin.attempt_id for pin in releases.finished} == {
            original_attempt_id,
            resumed_attempt_id,
        }
        assert store.list_pending_pin_releases() == []

    asyncio.run(scenario())


def test_startup_reuses_recovery_scheduled_before_crash(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        releases = Releases()
        preview = releases.preview_attempt_pin(run["attempt"]["id"])
        run = store.reserve_attempt_dispatch(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            dispatch_owner="crashed-worker",
            runtime_release_id=preview.release_id,
            state_generation_id=preview.generation_id,
            runtime_slot_id=preview.slot_id,
            runtime_artifact_digest=preview.artifact_digest,
            runtime_worker_protocol=preview.worker_protocol,
            expected_revision=run["revision"],
            actor_id="crashed-worker",
            idempotency_key="reserve-before-crash-0001",
        ).value
        store.schedule_runtime_recovery(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            expected_revision=run["revision"],
            actor_id="crashed-worker",
            idempotency_key="schedule-before-crash-0001",
        )
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: FakeHermesBackend()),
            releases,
        )

        assert await orchestrator.recover_startup() == [
            {
                "run_id": run["id"],
                "attempt_id": run["attempt"]["id"],
                "outcome": "dispatchable",
            }
        ]
        assert store.list_pending_runtime_recoveries() == []
        assert releases.attempt_pin(run["attempt"]["id"]) == preview

    asyncio.run(scenario())


def test_startup_converges_terminal_recovery_and_release_after_crash(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        current = store.get_run(run["id"])
        attempt = store.get_attempt(str(current["active_attempt_id"]))
        command = store.schedule_runtime_recovery(
            run_id=run["id"],
            attempt_id=attempt["id"],
            expected_revision=current["revision"],
            actor_id="crashed-recovery",
            idempotency_key="terminal-crash-schedule-0001",
        ).value
        claimed = store.claim_runtime_recovery(
            recovery_command_id=command["id"],
            worker_id="crashed-recovery",
            lease_seconds=30,
            actor_id="crashed-recovery",
            idempotency_key="terminal-crash-claim-00001",
        ).value
        current = store.get_run(run["id"])
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=attempt["id"],
            runtime_binding_id=attempt["runtime_binding_id"],
            runtime_release_id=attempt["runtime_release_id"],
            state_generation_id=attempt["state_generation_id"],
            target_state="failed",
            expected_revision=current["revision"],
            actor_id="crashed-recovery",
            idempotency_key="terminal-crash-transition-01",
        )
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "UPDATE runtime_recovery_commands SET claim_expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (claimed["id"],),
            )

        reports = await orchestrator.recover_startup()
        assert reports == [
            {
                "run_id": run["id"],
                "attempt_id": attempt["id"],
                "outcome": "terminal_converged",
            }
        ]
        assert store.list_pending_runtime_recoveries() == []
        assert store.list_pending_pin_releases() == []
        assert releases.finished == releases.pins
        dispatch.cancel()
        try:
            await dispatch
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_a_closed_activation_gate_never_reaches_the_runtime(tmp_path: Path) -> None:
    """The gate's whole point: the runtime is not consulted when it is closed.

    A runtime that explodes on contact is the unambiguous instrument. Asserting
    only that dispatch failed would pass on a gate checked AFTER capabilities
    had already been requested -- which is where the pre-existing checks live,
    and is too late to be a safety property.
    """

    class ExplodingRuntime:
        def __getattr__(self, name):
            def refuse(*args, **kwargs):
                raise AssertionError(f"runtime.{name} was called through a closed gate")

            return refuse

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        store.disable_runtime_activation(
            actor_id="test-operator",
            idempotency_key="deactivate-for-refusal1",
        )

        orchestrator = RunOrchestrator(store, ExplodingRuntime(), Releases())
        await orchestrator.dispatch(run["id"])

        current = store.get_run(run["id"])
        assert current["state"] == "failed"
        # The ExplodingRuntime above is the assertion that matters: had the
        # gate been checked one line later, it would have raised instead.

    asyncio.run(scenario())


def test_the_turn_s_answer_rides_the_completion_event(tmp_path: Path) -> None:
    """⟦P5.4c⟧ A completion nobody can read is not an answer.

    The Telegram projection renders `run.completed` from `payload["summary"]`
    (`transports/telegram.py` `_project_event`), and until now the orchestrator
    wrote only `category` and `retryable` there -- so a real turn produced a
    delivery whose body was empty. The reply the runtime already emitted as
    `runtime.message.completed` is carried into the transition that ends the
    run, which is the same fact told twice on purpose: once as a thread message
    for the operator's history, once on the event a transport can project.
    """

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        orchestrator = RunOrchestrator(
            store, HermesAdapter(backend_loader=lambda: backend), Releases()
        )

        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="summary-decision-0001",
        )
        action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(action["id"], worker_id="worker-one")
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "completed"

        event = next(
            item
            for item in store.list_run_events(run["id"])
            if item["type"] == "run.completed"
        )
        assert event["payload"]["summary"] == "Hello"
        # The thread history still carries it too; neither replaces the other.
        assert store.list_messages(completed["thread_id"])[-1]["content"] == "Hello"

    asyncio.run(scenario())


def test_a_run_with_no_assistant_reply_carries_no_summary(tmp_path: Path) -> None:
    """An absent answer is absent, never an empty string a projection renders."""

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend(mode="run_error")
        orchestrator = RunOrchestrator(
            store, HermesAdapter(backend_loader=lambda: backend), Releases()
        )

        failed = await orchestrator.dispatch(run["id"])
        assert failed["state"] == "failed"
        event = next(
            item
            for item in store.list_run_events(run["id"])
            if item["type"] == "run.failed"
        )
        assert "summary" not in event["payload"]

    asyncio.run(scenario())


class _RacingReleases(Releases):
    """Releases whose `finish_attempt` lets another writer move the run.

    ⟦P9-2⟧ The concurrency the P9-1 report flagged, made deterministic. The
    settlement used to read the run's revision BEFORE this call, and
    `finish_attempt` is where a pin release does its real work -- so anything
    that transitioned the run while it ran stole the revision the settlement
    was about to use. The bump here is a bare `revision + 1` on purpose: that
    increment is the whole of what `_expect_revision` can see, so it stands
    for every concurrent writer without pinning the test to one of them.
    """

    def __init__(self, database: Path, run_id: str) -> None:
        super().__init__()
        self._database = database
        self._run_id = run_id
        self.races = 0

    def finish_attempt(self, attempt_id: str, pin) -> None:
        self.races += 1
        with sqlite3.connect(self._database) as connection:
            connection.execute(
                "UPDATE runs SET revision = revision + 1 WHERE id = ?",
                (self._run_id,),
            )
        super().finish_attempt(attempt_id, pin)


def test_a_pin_release_settles_against_the_revision_it_writes_at(
    tmp_path: Path,
) -> None:
    """⟦P9-2⟧ A run that moves under the pin work no longer fails the release.

    The acknowledgement was written with a revision read before the pin work,
    so a concurrent transition made it lose `RevisionConflict`, which the
    catch-all recorded as `pin_release_failed` -- although the pin HAD been
    released, and nothing about it had failed. The settlement now reads the
    revision inside a bounded re-offer, the same shape ⟦P9-1⟧ gave runtime
    events.
    """

    async def scenario() -> None:
        database = tmp_path / "control.db"
        store = ControlStore(database)
        store.initialize()
        run = _queued_run(store)
        backend = FakeHermesBackend()
        releases = _RacingReleases(database, str(run["id"]))
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.pending.wait, 2)
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=decision["id"],
            choice="approve_once",
            expected_revision=decision["revision"],
            actor_id="local",
            idempotency_key="decision-pin-race-00001",
        )
        action = store.list_pending_runtime_actions()[0]
        await orchestrator.deliver_runtime_action(
            action["id"], worker_id="worker-pin-race"
        )
        completed = await asyncio.wait_for(dispatch, 2)
        assert completed["state"] == "completed"

        # The race really happened, on the delivery the dispatch itself runs.
        assert releases.races >= 1
        assert releases.finished == releases.pins
        # And the release is recorded as what it is: acked, not failed.
        types = [event["type"] for event in store.list_run_events(run["id"])]
        assert "runtime.pin_release.failed" not in types, types
        assert "runtime.pin_release.acked" in types, types
        # Nothing is left pending or failed for a later sweep to pick up, and a
        # second sweep has nothing to do.
        assert store.list_pending_pin_releases() == []
        assert orchestrator.deliver_pending_pin_releases(
            worker_id="worker-pin-race-deliver"
        ) == []

    asyncio.run(scenario())


class _PauseThenAskBackend(FakeHermesBackend):
    """Asks for an approval inside the window a `/pause` opens.

    ⟦P9-3⟧ Driven without a turn bridge on purpose. Since this slice the
    bridge DELIVERS the queued `control.pause`, the adapter refuses it
    (`pause_requires_cortex_stage_boundary`) and the store rolls the run back
    to `running` -- so the window under test here is the one before that
    lands, and putting a deliverer in the test would race it shut.
    """

    def __init__(self) -> None:
        super().__init__()
        self.proceed = threading.Event()
        self.left = threading.Event()

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        assert self.proceed.wait(20), "the pause never committed"
        emit(
            HermesSignal(
                "decision.required",
                {
                    "decision_id": "decision-pause-1",
                    "decision_kind": "approval",
                    "prompt": "Approve?",
                    "command": "safe command",
                    "description": "test approval",
                },
            )
        )
        try:
            # Bounded ONLY so a regression cannot hang the suite; the worker
            # this stands in for waits for ever.
            assert self.canceled.wait(30), "the parked worker was never released"
        finally:
            self.left.set()
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)


def test_a_decision_asked_for_after_a_pause_converges_instead_of_failing(
    tmp_path: Path,
) -> None:
    """⟦P9-3 / fix-verify standing item 9⟧ `/pause` gets the cancel discipline.

    The A-2 arm was keyed on `cancel_requested` alone, and `/pause` is
    reachable from Telegram and from the API. So a worker that asked for an
    approval after a pause still reached `create_decision`, still failed its
    `running`-only rule, and still ended the run `failed /
    runtime_dispatch_failed / retryable: true` -- a failure notification, with
    a Retry button, for a turn the operator merely wanted held.

    It now converges the way a cancelled one does, under its own category so
    the operator can tell a pause from a cancel, and then falls through so the
    decision write is refused and the parked worker is released. `canceled`
    rather than `paused` because `paused` is unreachable: it demands the
    attempt carry a checkpoint URI and a Hermes turn writes none, which is
    also why `_RUN_TRANSITIONS['pause_requested']` had to gain the edge.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_PAUSE_DECISION,
    )

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = _PauseThenAskBackend()
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 10)

        current = store.get_run(run["id"])
        assert current["state"] == "running", current["state"]
        store.transition_run(
            run_id=run["id"],
            target_state="pause_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="pause-then-ask-000001",
        )
        backend.proceed.set()
        final = await asyncio.wait_for(dispatch, 20)

        assert final["state"] == "canceled", final
        events = store.list_run_events(run["id"])
        types = [event["type"] for event in events]
        terminal = next(
            event for event in events if event["type"] == "run.canceled"
        )
        assert terminal["payload"]["category"] == CANCELED_AFTER_PAUSE_DECISION
        assert terminal["payload"]["retryable"] is False
        # The defect this replaces, named so a regression is unambiguous.
        assert "run.failed" not in types, types
        assert "runtime_dispatch_failed" not in [
            (event.get("payload") or {}).get("category") for event in events
        ]
        # Nobody is left to answer, so nothing was asked.
        assert store.list_decisions(state="pending") == []
        assert "decision.required" not in types
        # The worker was released rather than left parked in its approval.
        assert await asyncio.to_thread(backend.left.wait, 10)
        assert backend.cancel_calls >= 1
        assert releases.finished == releases.pins

    asyncio.run(scenario())


class _PauseThenEmitBackend(FakeHermesBackend):
    """Emits one ordinary durable event inside the window a `/pause` opens.

    ⟦P9-3 C1⟧ The `_PauseThenAskBackend` sibling covers the event that ENDS a
    turn (a decision request). This one covers the events that do not end
    anything and that every real turn emits by the dozen -- which is why the
    guard rejecting them was the arm an operator actually hit.
    """

    def __init__(self, *, emit_tool: bool, final_response: str | None) -> None:
        super().__init__()
        self.proceed = threading.Event()
        self._emit_tool = emit_tool
        self._final_response = final_response

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        assert self.proceed.wait(20), "the pause never committed"
        if self._emit_tool:
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
        return HermesRunResult(
            request.session_ref, final_response=self._final_response
        )


@pytest.mark.parametrize(
    ("emit_tool", "final_response", "discarded"),
    [
        (True, None, "runtime.tool.started"),
        (False, "Hello", "runtime.message.completed"),
    ],
    ids=["tool-started", "message-completed"],
)
def test_a_pause_plus_one_ordinary_durable_event_still_converges(
    tmp_path: Path,
    emit_tool: bool,
    final_response: str | None,
    discarded: str,
) -> None:
    """⟦P9-3 C1⟧ The arms were widened to `pause_requested`; the store was not.

    So the slice that set out to give `/pause` the cancel discipline covered
    the two events that terminate a turn and left the four that do not --
    and those are the ones a live worker actually emits. One `/pause` plus one
    `runtime.tool.started`, the most ordinary event there is, still ended the
    run `failed / runtime_dispatch_failed / retryable: true`, because
    `record_runtime_observation`'s discard guard admitted `cancel_requested`
    alone and raised `InvalidTransition` for a merely-paused run.

    The window is not exotic: the review measured it at one `STATE_POLL_SECONDS`
    plus the adapter round-trip, and every tool call in a turn emits two events
    into it.

    Both members are driven separately so a regression in one is unambiguous.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_PAUSE_COMPLETION,
    )

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = _PauseThenEmitBackend(
            emit_tool=emit_tool, final_response=final_response
        )
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 10)

        current = store.get_run(run["id"])
        assert current["state"] == "running", current["state"]
        store.transition_run(
            run_id=run["id"],
            target_state="pause_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key="pause-then-emit-000001",
        )
        backend.proceed.set()
        final = await asyncio.wait_for(dispatch, 20)

        events = store.list_run_events(run["id"])
        types = [event["type"] for event in events]
        assert final["state"] == "canceled", (final["state"], types)
        terminal = next(
            event for event in events if event["type"] == "run.canceled"
        )
        assert terminal["payload"]["category"] == CANCELED_AFTER_PAUSE_COMPLETION
        assert terminal["payload"]["retryable"] is False
        # The event was recorded rather than applied, by TYPE and with no body.
        discards = [
            event["payload"]["event_type"]
            for event in events
            if event["type"] == "runtime.event.discarded"
        ]
        assert discarded in discards, (discarded, discards)
        # The defect this replaces, named so a regression is unambiguous.
        assert "run.failed" not in types, types
        assert "runtime_dispatch_failed" not in [
            (event.get("payload") or {}).get("category") for event in events
        ]
        assert releases.finished == releases.pins

    asyncio.run(scenario())


class _StopThenFailBackend(FakeHermesBackend):
    """Reports the shipped worker's failure result once the stop has landed.

    ⟦P9-3 BRK-1⟧ The result shape is the shipped one, not an invention:
    `worker_payload/cortex_worker/turn.py`'s `except BaseException` arm builds
    `{"canceled": False, "failed": True}` for anything that is not
    `TurnCanceled`, and a torn-down provider stream or tool subprocess is
    exactly that. `test_a_worker_that_dies_from_the_delivered_cancel_ends_canceled`
    reaches that arm through the real `Turn`; this drives the same contract for
    `pause_requested`, which no bridge path can reach.
    """

    def __init__(self) -> None:
        super().__init__()
        self.proceed = threading.Event()

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        assert self.proceed.wait(20), "the stop never committed"
        return HermesRunResult(
            request.session_ref,
            final_response=None,
            canceled=False,
            failed=True,
        )


@pytest.mark.parametrize(
    ("stopped", "category"),
    [
        ("cancel_requested", "runtime_failed_after_cancel"),
        ("pause_requested", "runtime_failed_after_pause"),
    ],
)
def test_a_worker_failure_after_a_stop_converges_instead_of_notifying(
    tmp_path: Path, stopped: str, category: str
) -> None:
    """⟦P9-3 BRK-1⟧ A failure reported after the stop is still the stop's run.

    The converging block had an arm for a worker that COMPLETES after the
    operator's stop and none for a worker that FAILS -- so the run ended
    `failed / runtime_execution_failed / retryable: true`, which Telegram
    renders as a notification with a Retry button. For a turn the operator
    cancelled or paused on purpose.

    Both converging states are driven because `pause_requested` reaches this
    arm too and no bridge path can produce it.
    """

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = _StopThenFailBackend()
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 10)

        current = store.get_run(run["id"])
        assert current["state"] == "running", current["state"]
        store.transition_run(
            run_id=run["id"],
            target_state=stopped,
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key=f"stop-then-fail-{stopped}",
        )
        backend.proceed.set()
        final = await asyncio.wait_for(dispatch, 20)

        events = store.list_run_events(run["id"])
        types = [event["type"] for event in events]
        assert final["state"] == "canceled", (final["state"], types)
        assert "run.failed" not in types, types
        terminal = next(
            event for event in events if event["type"] == "run.canceled"
        )
        assert terminal["payload"]["category"] == category
        assert terminal["payload"]["retryable"] is False
        # The worker's account of why it stopped is still legible.
        assert terminal["payload"].get("detail"), terminal["payload"]
        assert releases.finished == releases.pins

    asyncio.run(scenario())


# ⟦batchK 10 / batchM 5⟧ ------ the discard set, pinned by what it claims ----


class _StopThenEmitOneBackend(FakeHermesBackend):
    """Emits exactly ONE durable non-terminal event after the operator's stop.

    One member of `_DISCARDED_AFTER_STOP` per instance, produced the way a real
    worker produces it -- a signal for the two tool events, and the terminal
    block's own shape for the other two: `runtime.message.completed` follows a
    `final_response`, `runtime.session_rebound` follows a result whose session
    ref differs from the binding's (hermes.py:1763 and :1776).
    """

    def __init__(
        self,
        *,
        signal: HermesSignal | None = None,
        final_response: str | None = None,
        rebind_to: str | None = None,
    ) -> None:
        super().__init__()
        self.proceed = threading.Event()
        self._signal = signal
        self._final_response = final_response
        self._rebind_to = rebind_to

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        assert self.proceed.wait(20), "the stop never committed"
        if self._signal is not None:
            emit(self._signal)
        return HermesRunResult(
            self._rebind_to or request.session_ref,
            final_response=self._final_response,
        )


#: One producer per member of the set under test. Keyed BY the member, so a
#: member added to the set with no way to produce it fails this file rather
#: than passing silently.
_DISCARD_ARMS = {
    "runtime.tool.started": lambda: _StopThenEmitOneBackend(
        signal=HermesSignal(
            "tool.started",
            {
                "tool_call_id": "tool-1",
                "tool_name": "paper_search",
                "arguments": {"query": "memory"},
            },
            stable_id="tool:tool-1:started",
        )
    ),
    "runtime.tool.completed": lambda: _StopThenEmitOneBackend(
        signal=HermesSignal(
            "tool.completed",
            {
                "tool_call_id": "tool-1",
                "tool_name": "paper_search",
                "is_error": False,
                "duration_ms": 12,
            },
            stable_id="tool:tool-1:completed",
        )
    ),
    "runtime.message.completed": lambda: _StopThenEmitOneBackend(
        final_response="Hello"
    ),
    "runtime.session_rebound": lambda: _StopThenEmitOneBackend(
        rebind_to="hermes-session-rebound-0001"
    ),
}


def test_the_discard_set_holds_only_events_that_terminate_nothing() -> None:
    """⟦batchK 10⟧ Nothing pinned this set's membership, and it was renamed.

    The comment states one rule for belonging: a discarded event terminates
    NOTHING, so an event that can turn out to be the LAST one a run ever hears
    does not belong here -- it needs an arm of its own that converges the run.
    Five event types are excluded by that rule, and the exclusion is the half
    with teeth: the two terminals a stopped run re-labels
    (`runtime.run.completed`, `runtime.run.failed`), the worker cancel that
    still ends `canceled` (`runtime.run.canceled`), and the two the block
    below converges terminally and then refuses -- `runtime.run.started` and
    `runtime.decision.required`, both of which WERE discarded once and both of
    which wedged a run in `cancel_requested` for ever when they were.

    Membership is pinned literally as well as by rule, because a rename is
    exactly the condition under which an unpinned set drifts: the chain that
    produced this test renamed it.
    """

    from cortex_platform.product.orchestration.service import (
        _DISCARDED_AFTER_STOP,
    )

    # The rule, as an exclusion. Each of these can be the last event of a run.
    # ⟦batchN ADJ-3⟧ FIRST, before the literal set below, and the order is the
    # only thing that makes it worth asserting: the literal equality fails for
    # any drift at all, so behind it this could never fail on its own and a
    # member promoted from the excluded five reported a set difference instead
    # of the rule it broke.
    assert _DISCARDED_AFTER_STOP.isdisjoint(
        {
            "runtime.run.completed",
            "runtime.run.failed",
            "runtime.run.canceled",
            "runtime.run.started",
            "runtime.decision.required",
        }
    )
    assert _DISCARDED_AFTER_STOP == frozenset(
        {
            "runtime.message.completed",
            "runtime.tool.started",
            "runtime.tool.completed",
            "runtime.session_rebound",
        }
    )
    # Every member has a producer below, so the behavioural arm covers the set
    # rather than a snapshot of it.
    assert _DISCARDED_AFTER_STOP == frozenset(_DISCARD_ARMS)
    # The comment's escape clause for the two tool events -- which CAN be the
    # last thing a quiet worker sends -- is that the hole is closed outside
    # this set, by the turn bridge's floor for a bound run in a converging
    # state. That floor has to cover both stopped states for the clause to
    # hold, so the claim is checked here rather than trusted.
    from cortex_platform.product.transports.bridge import STALLED_STATES

    assert {"cancel_requested", "pause_requested"} <= STALLED_STATES


@pytest.mark.parametrize("discarded", sorted(_DISCARD_ARMS))
def test_a_discarded_event_after_a_stop_is_recorded_and_terminates_nothing(
    tmp_path: Path, discarded: str
) -> None:
    """⟦batchK 10 / batchM 5⟧ The property each member has to have, per member.

    Not that the set contains four strings, but that each of those four is a
    durable event a stopped run may legitimately drop: the store ACCEPTS it as
    an observation while the run is `cancel_requested` (no `InvalidTransition`,
    which is the C1 defect), it is recorded by TYPE and never applied, and the
    run's terminal comes from the stop's own arm afterwards rather than from
    the event. A member that terminated something, or that the store refused,
    fails here.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_RUNTIME_COMPLETION,
    )

    async def scenario() -> None:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        backend = _DISCARD_ARMS[discarded]()
        releases = Releases()
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            releases,
        )
        dispatch = asyncio.create_task(orchestrator.dispatch(run["id"]))
        assert await asyncio.to_thread(backend.active.wait, 10)

        current = store.get_run(run["id"])
        assert current["state"] == "running", current["state"]
        store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key=f"stop-then-emit-{discarded.replace('.', '-')}",
        )
        backend.proceed.set()
        final = await asyncio.wait_for(dispatch, 20)

        events = store.list_run_events(run["id"])
        types = [event["type"] for event in events]
        # Recorded, by type and with no body: the run took the observation.
        discards = [
            event["payload"]["event_type"]
            for event in events
            if event["type"] == "runtime.event.discarded"
        ]
        assert discarded in discards, (discarded, types)
        # And never applied. The event's own type is absent from the ledger,
        # so nothing downstream can read it as something that happened.
        assert discarded not in types, types
        # It terminated nothing: the run reached its terminal through the
        # stop's own completion arm, under the stop's category.
        assert final["state"] == "canceled", (final["state"], types)
        terminal = next(
            event for event in events if event["type"] == "run.canceled"
        )
        assert terminal["payload"]["category"] == CANCELED_AFTER_RUNTIME_COMPLETION
        assert terminal["payload"]["retryable"] is False
        # ⟦P9-3 C1⟧ The store refusing the observation is what ended a stopped
        # turn `failed / runtime_dispatch_failed` with a Retry button.
        assert "run.failed" not in types, types
        assert "runtime_dispatch_failed" not in [
            (event.get("payload") or {}).get("category") for event in events
        ]
        assert releases.finished == releases.pins

    asyncio.run(scenario())
