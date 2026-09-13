"""⟦P5.4c⟧ The inbound → turn bridge, against a real ControlStore."""

from __future__ import annotations

import asyncio
import logging
import json
import threading
import time

import pytest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cortex_platform.product.control import (
    CANCELED_BEFORE_BINDING,
    ControlStore,
    RevisionConflict,
)
from cortex_platform.product.orchestration import RunOrchestrator
from cortex_platform.product.transports.managed_worker import (
    ManagedWorkerUnavailable,
)
from cortex_platform.product.transports.bridge import (
    CANCELED_RUNTIME_QUIET,
    QUIET_AFTER_RESUME,
    PAUSED_RUNTIME_QUIET,
    RECOVERY_DEFERRED,
    TURN_TIMEOUT,
    OUTCOME_ANSWERED,
    OUTCOME_CANCELED,
    OUTCOME_DECISION_REQUIRED,
    OUTCOME_FAILED,
    OUTCOME_REFUSED,
    REFUSED_DISPATCH_DISABLED,
    REFUSED_NOT_CONVERSATION,
    REFUSED_QUEUE_FULL,
    REFUSED_RUN_NOT_ACTIVE,
    InboundTurnBridge,
)
from cortex_platform.runtime.hermes import HermesAdapter as BaseHermesAdapter
from cortex_platform.runtime.hermes import HermesRunResult, HermesSignal
from cortex_platform.runtime.models import (
    FAILURE_DETAIL_LIMIT,
    WORKER_REPORTED_FAILURE,
    failure_detail,
)
from cortex_platform.runtime.tests.fakes import (
    FAKE_RUNTIME_IDENTITY,
    FakeHermesBackend,
)


class HermesAdapter(BaseHermesAdapter):
    """A certified durable worker, which is what the orchestrator requires."""

    async def capabilities(self):
        capabilities = await super().capabilities()
        return replace(capabilities, durable_operation_deduplication=True)


@dataclass(frozen=True)
class Pin:
    attempt_id: str
    release_id: str = FAKE_RUNTIME_IDENTITY.release_id
    generation_id: str = FAKE_RUNTIME_IDENTITY.state_generation_id
    slot_id: str = FAKE_RUNTIME_IDENTITY.slot_id
    artifact_digest: str = FAKE_RUNTIME_IDENTITY.artifact_digest
    worker_protocol: str = FAKE_RUNTIME_IDENTITY.worker_protocol


class Releases:
    def __init__(self) -> None:
        self.pins: list[Pin] = []
        self.finished: list[Pin] = []

    def preview_attempt_pin(self, attempt_id: str) -> Pin:
        return self.attempt_pin(attempt_id) or Pin(attempt_id)

    def pin_attempt(self, attempt_id: str, expected_pin: Pin | None = None) -> Pin:
        existing = self.attempt_pin(attempt_id)
        if existing is not None:
            return existing
        pin = Pin(attempt_id)
        self.pins.append(pin)
        return pin

    def finish_attempt(self, attempt_id: str, pin: Pin) -> None:
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


def _store(tmp_path: Path) -> ControlStore:
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    return store


def _thread_with_message(store: ControlStore, *, index: int = 1) -> str:
    workspace = store.create_workspace(
        title="Research",
        actor_id="operator",
        idempotency_key=f"bridge-workspace-{index:06d}",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Telegram",
        expected_revision=workspace["revision"],
        actor_id="operator",
        idempotency_key=f"bridge-thread-{index:06d}",
    ).value
    store.append_message(
        thread_id=thread["id"],
        role="user",
        content="What did the paper claim?",
        expected_revision=thread["revision"],
        actor_id="telegram:operator",
        idempotency_key=f"bridge-message-{index:06d}",
    )
    return str(thread["id"])


def _enable_dispatch(store: ControlStore, *, index: int = 1) -> None:
    store.enable_runtime_activation(
        mode="permanent",
        actor_id="operator",
        idempotency_key=f"bridge-activation-{index:06d}",
    )


def _bridge(
    store: ControlStore,
    backend: FakeHermesBackend,
    built: list | None = None,
    **options,
) -> InboundTurnBridge:
    # ⟦P9-4⟧ A turn parked on a decision is now HELD open while the operator
    # answers, because the parked worker is only reachable while its dispatch
    # lives. Every test written before that is about a decision NOBODY
    # answers, and their subject is what happens once the window expires --
    # so the window is short here by default. The hold itself is exercised by
    # the tests that pass `decision_wait` explicitly.
    options.setdefault("decision_wait", 0.2)

    def factory():
        orchestrator = RunOrchestrator(
            store,
            HermesAdapter(backend_loader=lambda: backend),
            Releases(),
        )
        if built is not None:
            built.append(orchestrator)
        return orchestrator

    return InboundTurnBridge(
        store=store,
        worker=None,  # type: ignore[arg-type] - the factory replaces it
        actor_id="cortexd-turn-test",
        orchestrator_factory=factory,
        **options,
    )


def _drain_one(bridge: InboundTurnBridge, thread_id: str, *, seconds: float = 20.0):
    """Run exactly one submission on the caller's thread, like the loop would."""

    return bridge._run_turn(thread_id)  # noqa: SLF001 - the unit under test


def test_a_closed_dispatch_gate_refuses_before_a_run_exists(tmp_path: Path) -> None:
    """⟦c2⟧ Window open, dispatch gate closed: no turn, and nothing to send.

    Refusing here rather than letting `RunOrchestrator` refuse is the whole
    point. A run created under a closed gate fails on the orchestrator's own
    check and commits `run.failed` -- which the drain would then deliver, so
    "no turn" would have sent the operator a message anyway.
    """

    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend())

    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == REFUSED_DISPATCH_DISABLED
    assert outcome.run_id is None
    assert store.get_thread(thread_id)["active_run_id"] is None
    # Not one event, so the drain has nothing to project and nothing to send:
    # "no turn, no send" is a property of the ledger here, not of a rule the
    # outbound half would have to remember.
    assert store.list_events(after_cursor=0, limit=50) == []


def test_an_inbound_message_becomes_a_turn_and_a_sendable_reply(
    tmp_path: Path,
) -> None:
    """⟦c1⟧ The round trip's product half, end to end against real state."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    built: list = []
    bridge = _bridge(store, backend, built)

    resolver = threading.Thread(
        target=_resolve_when_asked, args=(store, backend, built)
    )
    resolver.start()
    try:
        outcome = _drain_one(bridge, thread_id)
    finally:
        resolver.join(timeout=20)

    assert outcome.outcome == OUTCOME_ANSWERED, outcome
    run = store.get_run(str(outcome.run_id))
    assert run["state"] == "completed"
    completed = next(
        event
        for event in store.list_run_events(str(outcome.run_id))
        if event["type"] == "run.completed"
    )
    # The projection reads exactly this field; without it the delivery the
    # drain sends has a heading and no body.
    assert completed["payload"]["summary"] == "Hello"


def _resolve_when_asked(
    store: ControlStore, backend: FakeHermesBackend, built: list
) -> None:
    """Stand in for the operator, so the fake's approval does not park the turn.

    The real operator's route is a Telegram callback button, which is the
    approval-vocabulary slice this one deliberately does not build; what is
    exercised here is only that a turn which IS answered reaches `completed`
    with the answer on the event a transport can project.
    """

    if not backend.pending.wait(20):
        return
    for _ in range(200):
        decisions = store.list_decisions(state="pending")
        if decisions and built:
            decision = decisions[0]
            store.resolve_decision(
                decision_id=decision["id"],
                choice="approve_once",
                expected_revision=decision["revision"],
                actor_id="operator",
                idempotency_key="bridge-decision-000001",
            )
            actions = store.list_pending_runtime_actions()
            if actions:
                asyncio.run(
                    built[0].deliver_runtime_action(
                        actions[0]["id"], worker_id="bridge-test"
                    )
                )
                return
        time.sleep(0.05)


def test_a_turn_answers_when_the_acknowledgement_wins_the_run_revision(
    tmp_path: Path,
) -> None:
    """⟦P9⟧ The c1 flake, forced rather than waited for.

    Two legitimate writers commit against the same run while it resumes from
    an approved decision: the dispatch applying the worker's
    `runtime.message.completed`, and `deliver_runtime_action` acknowledging
    the decision the run resumed from. Both read the run and then write with
    the revision they read, so one of them loses -- and the store means that
    to be survivable, which is why `_confirm_runtime_resume_from_event` lets
    EITHER side record the acceptance, why `deliver_runtime_action` already
    re-reads the action when it is the side that loses, and why
    `runtime_event_inbox` hashes an event's request with `expected_revision`
    deliberately excluded.

    Driven against `RunOrchestrator` rather than through `_run_turn`: the
    bridge abandons a parked run on its own schedule (⟦c3⟧), and this test is
    about the ledger race, not about that deadline. The acknowledgement is
    held until the dispatch has read the run, and the dispatch is held until
    the acknowledgement has committed, so the losing order is the one that
    runs every time rather than a quarter of the time.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run = store.create_run(
        thread_id=thread_id,
        expected_revision=int(store.get_thread(thread_id)["revision"]),
        actor_id="cortexd-turn-test",
        idempotency_key="bridge-revision-race-000001",
    ).value
    backend = FakeHermesBackend()
    orchestrator = RunOrchestrator(
        store, HermesAdapter(backend_loader=lambda: backend), Releases()
    )

    read_the_run = threading.Event()
    acknowledged = threading.Event()
    conflicts: list[RevisionConflict] = []
    append_runtime_message = store.append_runtime_message
    acknowledge_runtime_action = store.acknowledge_runtime_action

    def held_append(**request):
        # The dispatch has read the run and settled on its expected_revision;
        # hold it here until the acknowledgement has moved the run under it.
        # Only the first offer waits, so the retry cannot deadlock on this.
        if not read_the_run.is_set():
            read_the_run.set()
            assert acknowledged.wait(20), "the acknowledgement never committed"
        try:
            return append_runtime_message(**request)
        except RevisionConflict as conflict:
            conflicts.append(conflict)
            raise

    def held_acknowledge(**request):
        assert read_the_run.wait(20), "the dispatch never read the run"
        try:
            return acknowledge_runtime_action(**request)
        finally:
            acknowledged.set()

    store.append_runtime_message = held_append  # type: ignore[method-assign]
    store.acknowledge_runtime_action = held_acknowledge  # type: ignore[method-assign]

    resolver = threading.Thread(
        target=_resolve_when_asked, args=(store, backend, [orchestrator])
    )
    resolver.start()
    try:
        answered = asyncio.run(orchestrator.dispatch(str(run["id"])))
    finally:
        # Never leave either side parked on a barrier the other stopped short
        # of setting: a broken interleaving must fail, not hang.
        read_the_run.set()
        acknowledged.set()
        resolver.join(timeout=20)

    assert answered["state"] == "completed", [
        (event["type"], event["payload"])
        for event in store.list_run_events(str(run["id"]))
    ]
    # The interleaving really happened rather than being timed away: the
    # dispatch's write lost the run's revision to the acknowledgement once.
    assert len(conflicts) == 1, conflicts
    completed = next(
        event
        for event in store.list_run_events(str(run["id"]))
        if event["type"] == "run.completed"
    )
    assert completed["payload"]["summary"] == "Hello"
    # Offered twice, written once. The retry is idempotent because the event
    # inbox keys the write on the adapter's event identity, so a second offer
    # can never leave the operator holding the answer twice.
    assert [
        message["content"]
        for message in store.list_messages(thread_id)
        if message["role"] == "assistant"
    ] == ["Hello"]


def test_a_cancel_during_a_live_turn_ends_canceled_when_the_answer_arrives_late(
    tmp_path: Path,
) -> None:
    """⟦P9⟧ Real-run check 5a on gen 13, forced rather than waited for.

    The operator cancels a RUNNING cockpit run (`cancel_requested`, through
    the API's own store write) while the worker is still answering; the
    worker then delivers `runtime.message.completed` and
    `runtime.run.completed`. The dispatch used to end the run `failed /
    runtime_dispatch_failed` with detail `InvalidTransition: invalid
    transition from cancel_requested to message_completed` -- a cancellation
    recorded as a failure, and `reasons.runtime_dispatch_failed` climbing on
    health. Now the late message is discarded, the late completion converges
    the run to `canceled` under its own category, no assistant message
    lands, and the bridge counts the turn as canceled, never as a failure.

    Two barriers, as ⟦P9-1⟧'s test: the first message write is held until
    the cancel has committed (so it loses the revision it read and is
    re-offered against the cancelled run), and the cancel waits until the
    dispatch has read the run, so the losing order runs every time.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_RUNTIME_COMPLETION,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, _AnsweringBackend())

    read_the_run = threading.Event()
    canceled = threading.Event()
    conflicts: list[RevisionConflict] = []
    cancel_result: dict[str, dict] = {}
    append_runtime_message = store.append_runtime_message

    def held_append(**request):
        if not read_the_run.is_set():
            read_the_run.set()
            assert canceled.wait(20), "the cancel never committed"
        try:
            return append_runtime_message(**request)
        except RevisionConflict as conflict:
            conflicts.append(conflict)
            raise

    def cancel_when_running():
        assert read_the_run.wait(20), "the dispatch never read the run"
        try:
            run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
            assert run["state"] == "running", run["state"]
            cancel_result["value"] = store.transition_run(
                run_id=str(run["id"]),
                target_state="cancel_requested",
                expected_revision=int(run["revision"]),
                actor_id="local-operator",
                idempotency_key="operator-cancel-000001",
            ).value
        finally:
            canceled.set()

    store.append_runtime_message = held_append  # type: ignore[method-assign]
    canceller = threading.Thread(target=cancel_when_running)
    canceller.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        read_the_run.set()
        canceled.set()
        canceller.join(timeout=20)

    assert cancel_result["value"]["state"] == "cancel_requested"
    run = store.get_run(str(outcome.run_id))
    events = store.list_run_events(str(run["id"]))
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    # The interleaving really happened: the message write lost the revision
    # to the cancel once, and was re-offered against the cancelled run.
    assert len(conflicts) == 1, conflicts
    assert outcome.outcome == OUTCOME_CANCELED
    assert outcome.reason == CANCELED_AFTER_RUNTIME_COMPLETION
    types = [event["type"] for event in events]
    assert "run.failed" not in types
    # The late answer is recorded as discarded -- its type, never its body --
    # so the attempt's event sequence stays contiguous for the completion.
    discarded = [event for event in events if event["type"] == "runtime.event.discarded"]
    assert [event["payload"] for event in discarded] == [
        {"event_type": "runtime.message.completed"}
    ]
    assert "Hello" not in repr(events)
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_AFTER_RUNTIME_COMPLETION
    assert terminal["payload"]["retryable"] is False
    assert "summary" not in terminal["payload"]
    assert _assistant_messages(store, thread_id) == []
    assert store.get_thread(thread_id)["active_run_id"] is None
    status = bridge.status()
    assert status["outcomes"] == {OUTCOME_CANCELED: 1}
    assert status["reasons"] == {CANCELED_AFTER_RUNTIME_COMPLETION: 1}
    assert status["last_failure"] is None


class _ParkingApprovalBackend(FakeHermesBackend):
    """A tool-using turn: it asks for an approval and then parks on it.

    This is the SHIPPED worker's shape, which no other fake here has:
    `cortex_worker`'s approval callback emits `decision.required` and then
    calls `await_decision` with NO timeout, and its `context.canceled`
    short-circuit is dead code because nothing in production delivers the
    control action that would set it. `_AnsweringBackend` answers without
    asking, and the default fake's own park is bounded by two seconds, so
    neither can show what releases a worker that is genuinely stuck on a
    question. The only thing that ends this one is the adapter's generator
    closing and sending `turn.cancel`, which is what sets `canceled`.
    """

    def __init__(self) -> None:
        super().__init__()
        #: The operator's cancel has committed; the worker may now ask.
        self.proceed = threading.Event()
        self.asked = threading.Event()
        self.left = threading.Event()

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        assert self.proceed.wait(20), "the cancel never committed"
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
        self.asked.set()
        try:
            # Bounded here ONLY so a regression cannot hang the suite; the
            # worker this stands in for waits for ever.
            assert self.canceled.wait(30), "the parked worker was never released"
        finally:
            self.left.set()
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)


def test_a_decision_asked_for_after_a_cancel_ends_canceled_and_frees_the_worker(
    tmp_path: Path,
) -> None:
    """⟦P9 / A-2⟧ Real-run check 10: cancel first, THEN the approval request.

    The turn above converges the shape where the worker ANSWERS late. This is
    the ordinary shape of a tool-using turn: the operator cancels a running
    run and the worker, which was never told, asks for a tool approval
    afterwards. `runtime.decision.required` used to reach `create_decision`,
    whose `running`-only rule raised `InvalidTransition`, and the dispatch
    ended the run `failed / runtime_dispatch_failed / retryable: true` -- a
    cancellation recorded as a retryable failure, with a Retry button on it.

    It is also the shape where the obvious fix is the wrong one. Discarding
    the event like the four non-terminal events above would leave the run in
    `cancel_requested` with no terminal event ever: the worker is parked in
    its approval callback with nothing coming to answer it, and the bridge's
    turn-timeout rescue refuses any state outside {running, starting}. So the
    cancel converges here first and the request is then left to fail on its
    own rule -- which is exactly what unwinds the stream, closes the adapter's
    generator and sends the parked worker its `turn.cancel`.

    No barrier is needed: the cancel commits while the run is `running` and
    the backend only asks afterwards, which is the real order.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_RUNTIME_DECISION,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _ParkingApprovalBackend()
    bridge = _bridge(store, backend)

    cancel_result: dict[str, dict] = {}

    def cancel_while_running() -> None:
        try:
            assert backend.active.wait(20), "the turn never started"
            run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
            assert run["state"] == "running", run["state"]
            cancel_result["value"] = store.transition_run(
                run_id=str(run["id"]),
                target_state="cancel_requested",
                expected_revision=int(run["revision"]),
                actor_id="local-operator",
                idempotency_key="operator-cancel-000002",
            ).value
        finally:
            backend.proceed.set()

    canceller = threading.Thread(target=cancel_while_running)
    canceller.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        backend.proceed.set()
        canceller.join(timeout=20)

    assert cancel_result["value"]["state"] == "cancel_requested"
    run = store.get_run(str(outcome.run_id))
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    assert "run.failed" not in types
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_AFTER_RUNTIME_DECISION
    assert terminal["payload"]["retryable"] is False
    # Nobody is left to answer a question, so none is recorded and the run is
    # never parked `waiting_for_decision`.
    assert store.list_decisions(state="pending") == []
    assert "run.waiting_for_decision" not in types
    assert _assistant_messages(store, thread_id) == []
    assert store.get_thread(thread_id)["active_run_id"] is None
    # The worker was released rather than left in its approval callback: the
    # generator's `finally` sent it `turn.cancel` and its `run` returned.
    assert backend.asked.is_set()
    assert backend.left.wait(20), "the worker was never released"
    assert backend.cancel_calls == 1
    # A cancellation, not a failure -- and its own typed word, so telemetry
    # can tell it from the late-answer shape.
    assert outcome.outcome == OUTCOME_CANCELED
    assert outcome.reason == CANCELED_AFTER_RUNTIME_DECISION
    status = bridge.status()
    assert status["outcomes"] == {OUTCOME_CANCELED: 1}
    assert status["reasons"] == {CANCELED_AFTER_RUNTIME_DECISION: 1}
    assert status["last_failure"] is None


class _StartingCancelBackend(FakeHermesBackend):
    """Answers, but holds in `reserve_attempt` -- inside the `starting` window.

    `starting` commits before `RuntimeAdapter.execute` is called at all, and
    the adapter then loads its backend, reserves the attempt and does the
    session handshake before it yields `runtime.run.started`. Holding in the
    reserve is therefore not an invented barrier: it stands in for that whole
    stretch, which is the seconds the cockpit shows a `starting` run with a
    Cancel button on it. The turn afterwards answers, like `_AnsweringBackend`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reserving = threading.Event()
        self.proceed = threading.Event()

    def reserve_attempt(self, run_id: str, attempt_id: str) -> object | None:
        self.reserving.set()
        assert self.proceed.wait(20), "the cancel never committed"
        return super().reserve_attempt(run_id, attempt_id)

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        return HermesRunResult(request.session_ref, "Hello", canceled=False)


def test_a_cancel_while_the_run_is_starting_converges_when_the_worker_starts(
    tmp_path: Path,
) -> None:
    """⟦P9 / ADJ9-1⟧ Real-run check 9: the cancel lands before the worker starts.

    `starting -> cancel_requested` is a legal edge and the cockpit offers the
    button, so this is a supported gesture rather than a race to lose. The
    worker's durable `runtime.run.started` then arrived at a cancelled run and
    asked for `cancel_requested -> running`, which no transition allows, and
    the dispatch ended the run `failed / runtime_dispatch_failed / retryable:
    true` -- a Retry button on a run the operator had just cancelled.

    The start is NOT discarded (that was the first attempt at this fix, and it
    left the quiet-worker case below with nothing to terminate it). It
    converges the run terminally under its own category and is then refused,
    which unwinds the stream. Because the adapter yields the start BEFORE it
    launches the worker, the turn ends without the worker ever running: no
    tokens are spent on an answer the operator has already said they do not
    want, which is why `run_calls` is asserted at zero.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_RUNTIME_START,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _StartingCancelBackend()
    bridge = _bridge(store, backend)

    cancel_result: dict[str, dict] = {}

    def cancel_while_starting() -> None:
        try:
            assert backend.reserving.wait(20), "the dispatch never reserved"
            run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
            assert run["state"] == "starting", run["state"]
            cancel_result["value"] = store.transition_run(
                run_id=str(run["id"]),
                target_state="cancel_requested",
                expected_revision=int(run["revision"]),
                actor_id="local-operator",
                idempotency_key="operator-cancel-000003",
            ).value
        finally:
            backend.proceed.set()

    canceller = threading.Thread(target=cancel_while_starting)
    canceller.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        backend.proceed.set()
        canceller.join(timeout=20)

    assert cancel_result["value"]["state"] == "cancel_requested"
    run = store.get_run(str(outcome.run_id))
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    assert "run.failed" not in types
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_AFTER_RUNTIME_START
    assert terminal["payload"]["retryable"] is False
    # Nothing is discarded on this path: the start is the event that converges
    # the run, and the stream unwinds before anything else can arrive.
    assert "runtime.event.discarded" not in types
    # The answer this backend would have given was never even asked for.
    assert backend.run_calls == 0
    assert "Hello" not in repr(events)
    assert _assistant_messages(store, thread_id) == []
    assert store.get_thread(thread_id)["active_run_id"] is None
    assert outcome.outcome == OUTCOME_CANCELED
    assert outcome.reason == CANCELED_AFTER_RUNTIME_START
    status = bridge.status()
    assert status["outcomes"] == {OUTCOME_CANCELED: 1}
    assert status["reasons"] == {CANCELED_AFTER_RUNTIME_START: 1}
    assert status["last_failure"] is None


class _QuietAfterStartBackend(_StartingCancelBackend):
    """Holds in `reserve_attempt`, and then says nothing at all, for ever.

    A worker does not have to fail to stop talking: it can hold the stream
    open and emit nothing, which is the case `TURN_TIMEOUT_SECONDS = 900`
    exists for. On a `cancel_requested` run that rescue does not fire --
    `DEFERRABLE_STATES` is {running, starting} -- so if the run's LAST event
    is discarded rather than converged, nothing terminates the run at all.
    """

    def __init__(self) -> None:
        super().__init__()
        self.launched = threading.Event()

    def run(self, request, emit):
        self.run_calls += 1
        self.launched.set()
        self.active.set()
        # Emits nothing, and returns only when the adapter cancels it.
        self.canceled.wait(30)
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)


def test_a_cancel_while_starting_still_ends_the_run_when_the_worker_goes_quiet(
    tmp_path: Path,
) -> None:
    """⟦P9 / ADJ9-1⟧ The branch that discarding the start could not terminate.

    Same cancel as the test above, but the worker then emits NOTHING. While
    `runtime.run.started` was in the discard set this run stayed
    `cancel_requested` for ever: the start was recorded as discarded and no
    terminal event ever came, the bridge's turn-timeout rescue refuses any
    state outside {running, starting}, its restart recovery and undriven
    sweep refuse a bound run just the same, and so the thread's
    `active_run_id`, the attempt's pin and the drain loop's only turn slot
    were all held until the operator's next message ended the run `failed /
    turn_abandoned` -- notifying them of a failure for a turn they cancelled.

    Now the start converges the run before the worker is ever launched, so
    this case and the answering one end identically and immediately. The turn
    timeout here is two seconds, so a regression that leaves the run
    non-terminal ends this test as a failure rather than as a hang.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_RUNTIME_START,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _QuietAfterStartBackend()
    bridge = _bridge(store, backend, turn_timeout=2.0)

    def cancel_while_starting() -> None:
        try:
            assert backend.reserving.wait(20), "the dispatch never reserved"
            run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
            assert run["state"] == "starting", run["state"]
            store.transition_run(
                run_id=str(run["id"]),
                target_state="cancel_requested",
                expected_revision=int(run["revision"]),
                actor_id="local-operator",
                idempotency_key="operator-cancel-000004",
            )
        finally:
            backend.proceed.set()

    canceller = threading.Thread(target=cancel_while_starting)
    canceller.start()
    started = time.monotonic()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        backend.proceed.set()
        canceller.join(timeout=20)
    elapsed = time.monotonic() - started

    run = store.get_run(str(outcome.run_id))
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_AFTER_RUNTIME_START
    assert "run.failed" not in types
    assert outcome.reason != TURN_TIMEOUT
    # The attempt's pin is handed back, so the next turn can be bound.
    assert "runtime.pin_release.acked" in types
    # The worker was never launched, so there is nothing left holding the
    # slot: the turn ends at once rather than at the timeout. `run_calls` is
    # what carries that deterministically -- converging and RETURNING would
    # free the thread but still sit here for the worker's whole silence, and
    # would be caught here rather than by the clock. The clock only separates
    # "ended at once" from "ended at the timeout this test configures".
    assert backend.run_calls == 0
    assert not backend.launched.is_set()
    assert elapsed < 2.0, elapsed
    assert outcome.outcome == OUTCOME_CANCELED
    assert outcome.reason == CANCELED_AFTER_RUNTIME_START
    assert bridge.status()["last_failure"] is None
    # What the operator actually lost while the run hung: the thread would
    # not take a new one.
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None
    reopened = store.create_run(
        thread_id=thread_id,
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="reopen-after-cancel-000001",
    ).value
    assert reopened["state"] == "queued"


# ⟦P9-3⟧ ---------------------------- the cancel, delivered to the shipped worker


class _ForkGate:
    """The two seams a fork double needs, and what the shipped callback answered.

    `install` is `tools.terminal_tool.set_approval_callback`: the shipped
    `ForkRunner` hands its own approval closure to the fork through exactly that
    function, so this is where a test gets hold of the very callable that parks
    in `TurnContext.await_decision`.
    """

    def __init__(self) -> None:
        self.callback: Any = None
        #: The turn thread is inside `run_conversation`, before the approval.
        self.entered = threading.Event()
        #: The test lets it reach the approval -- after the cancel has been
        #: delivered, or before it, depending on the shape under test.
        self.proceed = threading.Event()
        #: The shipped approval closure was entered.
        self.asked = threading.Event()
        #: It returned. For a parked worker that means it left `await_decision`,
        #: the wait this slice exists to end.
        self.left = threading.Event()
        self.choice: Any = None

    def install(self, callback: Any) -> None:
        self.callback = callback


def _fork_double(monkeypatch: Any, home: Path, gate: _ForkGate) -> None:
    """Install a fork whose surface is the certified one's, wired to `gate`.

    The four modules `worker_payload/cortex_worker/runtime.py` imports in
    `load_runtime`, with the two seams this test needs made real:
    `set_approval_callback` KEEPS the callback instead of dropping it, and the
    agent calls it. Everything between those two points -- the approval
    closure, its `if context.canceled` short circuit, the `decision.required`
    emission, the park -- is the shipped runner's own code, unmodified.
    """

    import sys
    from types import ModuleType

    class _SessionDB:
        def __init__(self, db_path: Any = None) -> None:
            self.db_path = db_path or (home / "state.db")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    class _Agent:
        def __init__(self, **options: Any) -> None:
            self.options = options
            self.session_id = options.get("session_id")
            self.interrupted = False

        def run_conversation(self, message: str, **_kwargs: Any) -> dict[str, Any]:
            gate.entered.set()
            assert gate.proceed.wait(20), "the turn was never released"
            gate.asked.set()
            # The fork's own call shape: one dangerous command and its
            # description. What answers it is the shipped runner's closure.
            gate.choice = gate.callback("safe command", "a tool needing approval")
            gate.left.set()
            # The answer names the choice, so a test can see that a DENIED
            # tool still let the turn finish and produce a reply.
            return {
                "final_response": f"echo:{message} ({gate.choice})",
                "completed": True,
            }

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = _SessionDB  # type: ignore[attr-defined]
    hermes_state.SCHEMA_VERSION = 13  # type: ignore[attr-defined]
    run_agent = ModuleType("run_agent")
    run_agent.AIAgent = _Agent  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    terminal_tool = ModuleType("tools.terminal_tool")
    terminal_tool.set_approval_callback = gate.install  # type: ignore[attr-defined]
    for name, module in (
        ("hermes_state", hermes_state),
        ("run_agent", run_agent),
        ("tools", tools),
        ("tools.terminal_tool", terminal_tool),
    ):
        monkeypatch.setitem(sys.modules, name, module)


class _ShippedWorkerBackend(FakeHermesBackend):
    """The turn is run by the worker the product SHIPS, not by a stand-in.

    ⟦P9-3⟧ What this slice claims is about code the product ships byte for byte
    and never imports in production. `TurnContext.await_decision`
    (`worker_payload/cortex_worker/turn.py`) waits on a gate with NO timeout
    whose only escape is `Turn.cancel()`, and the approval closure that enters
    it -- including the `if context.canceled` short circuit that the batchI
    adjudication called dead code -- is built by the shipped `ForkRunner`
    (`worker_payload/cortex_worker/runtime.py`). A double that parked on an
    `Event` of its own would prove the bridge delivers something; it would not
    prove the shipped worker LEAVES, which is the whole claim, and a fake
    kinder than the shipped worker is exactly what a cancel path may not be
    tested against.

    So the worker side here is the real thing: the real `Turn`, its real
    `TurnContext`, the real `ForkRunner`, the real approval closure, and a
    `cancel` that IS the shipped `Turn.cancel` -- the method a `turn.cancel`
    frame reaches through `serve.py`'s dispatch, and the one
    `WorkerSupervisorV2.cancel_turn` sends. Only the pipe is replaced by a
    method call, because a real worker subprocess is a distribution test and
    this is a bridge test. The frames the turn writes are decoded with the
    shipped `parse_frame` -- the supervisor's own decoder -- so the result this
    backend reports is the one the supervisor would have read off the wire.
    """

    def __init__(self, home: Path, gate: _ForkGate) -> None:
        super().__init__()
        self._home = home
        self._gate = gate
        #: The shipped `Turn`, once the turn thread owns it.
        self.turn: Any = None
        #: Every frame the shipped emitter wrote, decoded as the supervisor
        #: decodes them.
        self.frames: list[Any] = []
        self.finished: dict[str, Any] = {}

    @property
    def signal_kinds(self) -> list[str]:
        return [str(frame.event["kind"]) for frame in self.frames]

    def _relay(self, emit: Any, kind: str, payload: dict[str, Any]) -> None:
        """What the supervisor forwards. Nothing, unless a subclass says so."""

    def _make_runner(self, fork_runner: Any) -> Any:
        """What the shipped `Turn` runs. The real `ForkRunner`, unless replaced.

        ⟦P9-3 BRK-1⟧ A subclass swaps in agent code that ABORTS, which is the
        only way to reach the shipped worker's `except BaseException` arm --
        and reaching it through the shipped `Turn` is the point, so the
        `failed` this backend reports is written by the worker's own outcome
        branch rather than asserted by the test.
        """

        return fork_runner

    def run(self, request: Any, emit: Any) -> HermesRunResult:
        from cortex_platform.product.runtime_update.worker_payload.cortex_worker.protocol import (  # noqa: E501
            parse_frame,
        )
        from cortex_platform.product.runtime_update.worker_payload.cortex_worker.runtime import (  # noqa: E501
            ForkRunner,
        )
        from cortex_platform.product.runtime_update.worker_payload.cortex_worker.turn import (  # noqa: E501
            EVENT_FINISH,
            Turn,
            TurnEmitter,
        )

        self.run_calls += 1
        self.active.set()

        def write(frame: bytes) -> None:
            event = parse_frame(frame)
            self.frames.append(event)
            kind = str(event.event["kind"])
            if kind == EVENT_FINISH:
                self.finished = dict(event.event["payload"])
            # The supervisor's side of the pipe: what it forwards onto the
            # adapter's signal stream. `emit` is `loop.call_soon_threadsafe`,
            # so calling it from the turn thread is what the real one does too.
            self._relay(emit, kind, dict(event.event["payload"]))

        emitter = TurnEmitter(f"{request.attempt_id}.0", write)
        turn = Turn(
            operation_id=emitter.operation_id,
            request={
                "session_ref": request.session_ref,
                "user_message": request.user_message,
                "task_id": request.attempt_id,
            },
            emitter=emitter,
            runner=self._make_runner(ForkRunner(self._home)),
            finish=lambda outcome, digest: None,
            # Long enough that no heartbeat lands inside these tests; the
            # heartbeat is not what is under test.
            heartbeat_interval=60.0,
        )
        self.turn = turn
        turn.start()
        # Bounded ONLY so a regression cannot hang the suite. The worker this
        # runs is the shipped one, and its park has no bound at all.
        assert turn.join(30), "the shipped turn never finished"
        result = dict(self.finished.get("result") or {})
        return HermesRunResult(
            str(result.get("session_ref") or request.session_ref),
            result.get("final_response"),
            canceled=bool(result.get("canceled")),
            failed=bool(result.get("failed")),
        )

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str = "",
    ) -> Any:
        outcome = super().cancel(
            run_id, attempt_id, adapter_operation_id, delivery_epoch, session_ref
        )
        turn = self.turn
        if turn is not None:
            # Exactly what a `turn.cancel` frame does on the worker's side of
            # the channel: `serve.py`'s dispatch calls `Turn.cancel()`.
            turn.cancel()
        return outcome


def _watch_parked(bridge: InboundTurnBridge) -> list[str]:
    """Record every time the loop reads `waiting_for_decision` off the run.

    ⟦P9-4⟧ Without this a decision test proves nothing about the hold: the
    operator's answer can land before the loop's first poll, so a bridge that
    abandons the park on sight still reaches `resuming` and still completes.
    Waiting for the SECOND observation is what makes the order deterministic
    -- the loop saw the park, did not break, and looked again.
    """

    seen: list[str] = []
    original = bridge._state  # noqa: SLF001

    def spy(run_id: str) -> str | None:
        state = original(run_id)
        if state == "waiting_for_decision":
            seen.append(run_id)
        return state

    bridge._state = spy  # type: ignore[method-assign]  # noqa: SLF001
    return seen


def _cancel_action(store: ControlStore, run_id: str) -> dict[str, Any] | None:
    """The control action the cancel's OWN transaction queued, found as the
    operator would: named on the `run.cancel_requested` event it was written
    beside."""

    for event in store.list_run_events(run_id):
        if event["type"] == "run.cancel_requested":
            action_id = (event.get("payload") or {}).get("runtime_action_id")
            if action_id:
                return store.get_runtime_action(str(action_id))
    return None


def test_a_cancel_is_delivered_to_the_shipped_worker_while_its_turn_still_runs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """⟦P9-3 / fix-verify standing item 5⟧ The queued action reaches the worker.

    `ControlStore._prepare_run_cancellation` has queued a `control.cancel`
    runtime action inside the cancel's own transaction since the store was
    written, and `RunOrchestrator.deliver_runtime_action` has always known how
    to hand one to the adapter -- but nothing in production ever called it, as
    three separate reviews re-checked. The shipped worker was therefore never
    told a cancel had happened, and `context.canceled` was unreachable code.

    The shape driven here is the one nothing else in the product can rescue.
    The run is `running` -- NOT `waiting_for_decision`, which is asserted,
    because a parked run is one the bridge already abandons within a poll --
    and the worker is inside a wait the platform cannot see. It got there
    through the shipped approval closure, so the wait is the real
    `TurnContext.await_decision`, which has no timeout; a worker inside a long
    tool call is durably indistinguishable and just as unreachable. Before
    this slice the only thing that ended it was `TURN_TIMEOUT_SECONDS = 900`,
    and even that only ended the TURN: `_end_timed_out` refuses a run outside
    `{running, starting}`, so the run stayed `cancel_requested` for ever.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    home = tmp_path / "fork-home"
    home.mkdir()
    gate = _ForkGate()
    _fork_double(monkeypatch, home, gate)
    backend = _ShippedWorkerBackend(home, gate)
    bridge = _bridge(store, backend, turn_timeout=6.0)
    run_id: dict[str, str] = {}

    def cancel_while_parked() -> None:
        # Let the shipped worker reach its approval and park before the
        # operator cancels: this test is about a worker that is ALREADY
        # waiting when the action is delivered.
        gate.proceed.set()
        # Raises if the worker never asks; the turn thread is what would
        # otherwise sit here for ever.
        _wait_until(
            lambda: "decision.required" in backend.signal_kinds, seconds=20.0
        )
        run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
        run_id["id"] = str(run["id"])
        # The state that makes this the unrescuable shape.
        assert run["state"] == "running", run["state"]
        store.transition_run(
            run_id=str(run["id"]),
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-000010",
        )

    canceller = threading.Thread(target=cancel_while_parked)
    canceller.start()
    started = time.monotonic()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        gate.proceed.set()
        canceller.join(timeout=30)
    elapsed = time.monotonic() - started

    # The worker really did park in the shipped wait, and really did leave it.
    assert gate.asked.is_set()
    assert gate.left.is_set(), "the shipped worker never left `await_decision`"
    assert backend.turn is not None
    assert backend.turn.context.canceled is True
    # `await_decision` answers a cancelled turn with the one safe default, and
    # the shipped mapping turns it into the fork's own word.
    assert gate.choice == "deny", gate.choice

    # The action the cancel queued was handed over and acknowledged.
    action = _cancel_action(store, run_id["id"])
    assert action is not None, "the cancel queued no runtime action"
    assert action["kind"] == "control.cancel"
    assert action["state"] == "acked", action
    assert backend.cancel_calls >= 1

    run = store.get_run(run_id["id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    # The run ends on the WORKER's own cancel, not on one of the late-event
    # arms that exist because the worker used to be told nothing: the fix at
    # the source makes those arms unnecessary for this shape rather than
    # merely correct after the fact.
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == "runtime_canceled"
    assert terminal["payload"]["retryable"] is False
    assert "run.failed" not in types
    assert "runtime.event.discarded" not in types
    assert outcome.reason != TURN_TIMEOUT
    # It ended because it was told to, not because the turn timed out.
    assert elapsed < 5.0, elapsed
    # The fork's answer is not delivered: the operator refused the turn.
    assert _assistant_messages(store, thread_id) == []
    assert outcome.outcome == OUTCOME_CANCELED
    assert bridge.status()["last_failure"] is None
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None


def test_a_cancelled_turn_never_asks_for_an_approval_the_operator_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """⟦P9-3⟧ Delivery collapses the A-2 family at its source.

    The batchI A-2 arm converges a `runtime.decision.required` that arrives
    after a cancel, because the shipped approval callback "emits this
    unconditionally". It does not: its FIRST statement is `if
    context.canceled: return fork_choice(None)`. That branch was simply
    unreachable, because nothing delivered the cancel that sets the flag.

    So with the action delivered before the worker reaches its approval, the
    question is never asked: no `runtime.decision.required` event, no decision
    row for an operator who has already walked away, and no
    `runtime_decision_after_cancel` convergence -- the run ends on the
    worker's own cancel. The A-2 arm stays where it is as the backstop for the
    interleaving where the ask beats the delivery; this is the ordinary path
    it no longer has to carry.
    """

    from cortex_platform.product.orchestration.service import (
        CANCELED_AFTER_RUNTIME_DECISION,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    home = tmp_path / "fork-home"
    home.mkdir()
    gate = _ForkGate()
    _fork_double(monkeypatch, home, gate)
    backend = _ShippedWorkerBackend(home, gate)
    bridge = _bridge(store, backend, turn_timeout=6.0)
    run_id: dict[str, str] = {}

    def cancel_before_the_ask() -> None:
        try:
            assert gate.entered.wait(20), "the shipped turn never started"
            run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
            run_id["id"] = str(run["id"])
            assert run["state"] == "running", run["state"]
            store.transition_run(
                run_id=str(run["id"]),
                target_state="cancel_requested",
                expected_revision=int(run["revision"]),
                actor_id="local-operator",
                idempotency_key="operator-cancel-000011",
            )
            # Hold the worker OUT of the approval until the bridge has handed
            # the action over. Not a convenience: it is the ordering under
            # test -- what a worker does once it KNOWS.
            _wait_until(
                lambda: (_cancel_action(store, run_id["id"]) or {}).get("state")
                == "acked",
                seconds=20.0,
            )
        finally:
            gate.proceed.set()

    canceller = threading.Thread(target=cancel_before_the_ask)
    canceller.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        gate.proceed.set()
        canceller.join(timeout=30)

    # The shipped closure was entered and short-circuited: it asked nobody.
    assert gate.asked.is_set()
    assert gate.left.is_set()
    assert "decision.required" not in backend.signal_kinds, backend.signal_kinds
    assert backend.turn is not None
    assert backend.turn.context.canceled is True

    run = store.get_run(run_id["id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    assert "runtime.decision.required" not in types
    assert "decision.required" not in types
    assert store.list_decisions(state="pending") == []
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == "runtime_canceled"
    assert terminal["payload"]["category"] != CANCELED_AFTER_RUNTIME_DECISION
    assert "run.failed" not in types
    assert outcome.outcome == OUTCOME_CANCELED
    assert bridge.status()["last_failure"] is None


class _QuietAfterCancelBackend(FakeHermesBackend):
    """Told to stop, and says nothing anyway.

    ⟦P9-3⟧ A worker does not have to FAIL to stop talking -- it can hold the
    stream open and emit nothing, which is what `TURN_TIMEOUT_SECONDS` exists
    for -- and the cancel it is handed changes that no more than the cancel it
    used to never be handed did. This one ignores the delivered
    `control.cancel` entirely and stops only when the stream it is attached to
    is torn down, so the ONLY thing that can make its run terminal is the
    floor.

    It answers the teardown cancel (`:cleanup-cancel`, the adapter's own
    `release_ownership`) so the suite stays bounded; a worker that ignored
    that one too would hang the adapter's `await worker`, which is a different
    and worse failure than the one under test here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def run(self, request: Any, emit: Any) -> HermesRunResult:
        self.run_calls += 1
        self.active.set()
        # Bounded ONLY so a regression cannot hang the suite.
        self.release.wait(30)
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str = "",
    ) -> Any:
        outcome = super().cancel(
            run_id, attempt_id, adapter_operation_id, delivery_epoch, session_ref
        )
        if adapter_operation_id.endswith(":cleanup-cancel"):
            self.release.set()
        return outcome


class _WedgedAfterCancelBackend(_QuietAfterCancelBackend):
    """Ignores the delivered cancel AND the adapter's teardown.

    ⟦P9-3 BRK-2⟧ Its sibling answers the `:cleanup-cancel` so the turn can
    finish; this one answers nothing, which is the shape the review measured
    at 15.1 s under a 1.0 s floor. The turn task therefore never returns while
    the assertions run, so `await task` cannot be what writes the terminal --
    only the hoisted write can be.

    Bounded by the TEST rather than by the worker: nothing releases it until
    the measurement is over, which is the point.
    """

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str = "",
    ) -> Any:
        # Deliberately not its parent's: the teardown is swallowed too.
        return FakeHermesBackend.cancel(
            self, run_id, attempt_id, adapter_operation_id, delivery_epoch, session_ref
        )


def test_the_floor_bounds_the_run_even_when_the_turn_task_never_returns(
    tmp_path: Path,
) -> None:
    """⟦P9-3 BRK-2⟧ The floor is a ceiling on the RUN, not on the decision.

    P9-3 reached its terminal writer only AFTER `task.cancel()` and an untimed
    `await task`, and that await runs through the adapter's
    `release_ownership`, which joins the worker over an uninterruptible
    `asyncio.to_thread`. So a worker that ignored both the delivered cancel and
    the teardown held the run in `cancel_requested` for as long as it pleased
    -- the review measured 15x the configured floor -- and what the floor
    actually bounded was the decision to give up, not the thing it is named
    for.

    The proof has to be made from OUTSIDE the turn, because the turn is
    precisely what is wedged: the drain call is still blocked when the run is
    already terminal, and that is asserted rather than assumed. A test that
    waited for the drain to return would measure the wedge, not the fix.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _WedgedAfterCancelBackend()
    # Floor well under the turn timeout, so a regression that only ends the run
    # at the timeout fails on the reason rather than passing on the outcome.
    bridge = _bridge(store, backend, cancel_floor=0.5, turn_timeout=20.0)
    done = threading.Event()

    def drain() -> None:
        try:
            _drain_one(bridge, thread_id)
        finally:
            done.set()

    turn = threading.Thread(target=drain)
    turn.start()
    try:
        assert backend.active.wait(10), "the turn never started"
        run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
        run_id = str(run["id"])
        store.transition_run(
            run_id=run_id,
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-brk2-0001",
        )
        started = time.monotonic()
        _wait_until(
            lambda: store.get_run(run_id)["state"] == "canceled", seconds=10.0
        )
        elapsed = time.monotonic() - started

        # The ceiling the constant promises. Generous against a slow machine
        # and still an order of magnitude under the turn timeout, so it can
        # only pass for the right reason.
        assert elapsed < 5.0, elapsed
        # The load-bearing half: the turn is STILL wedged. Had the terminal
        # been written after the teardown, this could not be true.
        assert not done.is_set(), "the turn returned; the wedge was not real"

        events = store.list_run_events(run_id)
        terminal = next(
            event for event in events if event["type"] == "run.canceled"
        )
        assert terminal["payload"]["category"] == CANCELED_RUNTIME_QUIET
        assert terminal["payload"]["retryable"] is False
        assert "run.failed" not in [event["type"] for event in events]
    finally:
        backend.release.set()
        turn.join(timeout=30)


class _CancelHonouringBackend(FakeHermesBackend):
    """Stops when it is told to, which is what makes the delivery observable."""

    def run(self, request: Any, emit: Any) -> HermesRunResult:
        self.run_calls += 1
        self.active.set()
        # Bounded ONLY so a regression cannot hang the suite.
        assert self.canceled.wait(30), "the worker was never told to stop"
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)


class _FlakyDelivery:
    """Throws once from the hand-over, then behaves.

    ⟦P9-3 BRK-3⟧ Models the one region of `deliver_runtime_action` that is not
    inside its own `except`: the claim and its three gets. Everything after
    that is absorbed and turned into an outcome, so this is the whole of the
    surface that can escape to the caller.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.attempts = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def deliver_runtime_action(self, action_id: str, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("the store was busy")
        return await self._inner.deliver_runtime_action(action_id, **kwargs)


def test_a_cancel_whose_first_hand_over_throws_is_offered_again(
    tmp_path: Path,
) -> None:
    """⟦P9-3 BRK-3⟧ One throw must not drop the operator's cancel for the turn.

    `delivered.add(action_id)` ran BEFORE the attempt, so an action was marked
    handed-over whether or not it had been. A single throw from the claim --
    the store is shared with the API's own writes -- meant the worker was never
    offered the cancel again, and the run degraded to exactly the pre-fix
    behaviour the floor exists to bound.

    The floor here is deliberately long: if it were short, this test could pass
    because the floor ended the run rather than because the cancel was
    re-offered, which is the wrong reason. The terminal asserted is the
    WORKER's own cancel.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _CancelHonouringBackend()
    flaky: dict[str, _FlakyDelivery] = {}

    def factory() -> Any:
        wrapper = _FlakyDelivery(
            RunOrchestrator(
                store,
                HermesAdapter(backend_loader=lambda: backend),
                Releases(),
            )
        )
        flaky["it"] = wrapper
        return wrapper

    bridge = InboundTurnBridge(
        store=store,
        worker=None,  # type: ignore[arg-type] - the factory replaces it
        actor_id="cortexd-turn-test",
        orchestrator_factory=factory,
        # Long enough that the floor cannot be what ends this run.
        cancel_floor=60.0,
        turn_timeout=30.0,
        decision_wait=0.2,
    )
    run_id: dict[str, str] = {}

    def cancel_once_running() -> None:
        assert backend.active.wait(10), "the turn never started"
        run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
        run_id["id"] = str(run["id"])
        store.transition_run(
            run_id=str(run["id"]),
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-brk3-0001",
        )

    canceller = threading.Thread(target=cancel_once_running)
    canceller.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        canceller.join(timeout=30)

    # It really did throw, and it really was asked again.
    assert flaky["it"].attempts >= 2, flaky["it"].attempts
    action = _cancel_action(store, run_id["id"])
    assert action is not None and action["state"] == "acked", action
    assert backend.cancel_calls >= 1

    run = store.get_run(run_id["id"])
    events = store.list_run_events(run_id["id"])
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    # Ended by the worker acting on the re-offered cancel, not by the floor.
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == "runtime_canceled", terminal["payload"]


def _run_state(store: ControlStore, thread_id: str) -> str:
    return str(store.get_run(str(store.get_thread(thread_id)["active_run_id"]))["state"])


def test_a_cancelled_run_ends_even_when_the_worker_that_was_told_stays_quiet(
    tmp_path: Path,
) -> None:
    """⟦P9-3 / fix-verify standing item 6⟧ The terminal floor for a BOUND run.

    Delivering the cancel is not a guarantee that the worker acts on it, and
    before this there was no floor under a bound run that did not: the
    turn-timeout rescue returns early outside `{running, starting}`, restart
    recovery filters to the same two states, and the start sweep answers None
    for a bound attempt. So the run stayed `cancel_requested` for ever,
    holding the thread's `active_run_id`, the attempt's pin and this loop's
    only turn slot -- until the operator's next message ended it `failed /
    turn_abandoned / retryable: true` and NOTIFIED them of a failure for a
    turn they had cancelled.

    The floor here is half a second so the test is quick; the turn timeout is
    twenty, so a regression that only ends this run at the timeout fails on
    the reason rather than passing on the outcome, and one that never ends it
    fails rather than hangs.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _QuietAfterCancelBackend()
    bridge = _bridge(store, backend, turn_timeout=20.0, cancel_floor=0.5)
    run_id: dict[str, str] = {}

    def cancel_while_running() -> None:
        assert backend.active.wait(20), "the turn never reached the worker"
        _wait_until(lambda: _run_state(store, thread_id) == "running", seconds=20.0)
        run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
        run_id["id"] = str(run["id"])
        store.transition_run(
            run_id=str(run["id"]),
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-000012",
        )

    canceller = threading.Thread(target=cancel_while_running)
    canceller.start()
    started = time.monotonic()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        backend.release.set()
        canceller.join(timeout=30)
    elapsed = time.monotonic() - started

    # The cancel WAS handed over: the floor is the backstop for a worker that
    # was told and did not answer, not a replacement for telling it.
    action = _cancel_action(store, run_id["id"])
    assert action is not None and action["state"] == "acked", action

    run = store.get_run(run_id["id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_RUNTIME_QUIET
    # Never a failure and never retryable: the operator asked for this.
    assert terminal["payload"]["retryable"] is False
    assert "run.failed" not in types
    assert outcome.reason != TURN_TIMEOUT
    assert outcome.outcome == OUTCOME_CANCELED
    assert outcome.reason == CANCELED_RUNTIME_QUIET
    assert bridge.status()["outcomes"] == {OUTCOME_CANCELED: 1}
    assert bridge.status()["last_failure"] is None
    # It ended at the floor, not at the twenty-second turn timeout.
    assert elapsed < 10.0, elapsed
    # The attempt's pin comes back, so the next turn can be bound at all.
    assert "runtime.pin_release.acked" in types
    # What the operator actually lost while the run hung: the thread would not
    # take a new one.
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None
    reopened = store.create_run(
        thread_id=thread_id,
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="reopen-after-quiet-000001",
    ).value
    assert reopened["state"] == "queued"


def test_the_start_sweep_converges_a_bound_run_a_dead_daemon_left_cancelled(
    tmp_path: Path,
) -> None:
    """⟦P9-3⟧ The restart half of the floor, and its window in both directions.

    A daemon cancelled mid-turn and then died leaves a BOUND run in
    `cancel_requested` whose worker died with it, so no terminal event is ever
    coming. `_sweep_undriven`'s existing arm converges only the UNBOUND shape
    (`_undriven` answers None for a bound attempt, deliberately, because
    `fail_unbound_run` is not that run's transition), and neither
    `_recoverable` nor `_end_timed_out` will look at this state at all -- so
    nothing converged it and the thread was dead for the life of the install.

    The sweep is asked twice, by two fresh bridges, because the window has to
    hold in both directions: a bridge whose floor has not yet elapsed must
    leave the run alone (a cancel that landed a moment ago belongs to the loop
    that is about to converge it itself), and one whose floor has must end it.
    Fresh bridges rather than the one that stranded the run, because a restart
    is exactly what this is: the `_driving` guard is process-local, and the
    dead daemon's is gone with it.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _StalledBackend()
    stranding = _bridge(store, backend)
    try:
        run_id = _stranded_running_run(store, stranding, backend, thread_id)
        run = store.get_run(run_id)
        store.transition_run(
            run_id=run_id,
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-000013",
        )
        # Bound, and left by a daemon that is not coming back.
        assert store.get_run(run_id)["state"] == "cancel_requested"
        attempt = store.get_attempt(str(store.get_run(run_id)["active_attempt_id"]))
        assert attempt["runtime_binding_id"] is not None

        # A floor that has not elapsed: the sweep must not touch it.
        too_early = _bridge(store, backend, cancel_floor=60.0)
        too_early._sweep_undriven()  # noqa: SLF001 - what `recover` calls first
        assert store.get_run(run_id)["state"] == "cancel_requested"
        assert too_early.status()["undriven"]["canceled"] == 0

        time.sleep(0.1)
        restarted = _bridge(store, backend, cancel_floor=0.05)
        restarted._sweep_undriven()  # noqa: SLF001 - what `recover` calls first
    finally:
        backend.release.set()

    run = store.get_run(run_id)
    events = store.list_run_events(run_id)
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_RUNTIME_QUIET
    assert terminal["payload"]["retryable"] is False
    assert "run.failed" not in types
    assert restarted.status()["undriven"]["canceled"] == 1
    # The unbound arm is untouched: this one never used `fail_unbound_run`.
    assert CANCELED_BEFORE_BINDING not in [
        (event.get("payload") or {}).get("category") for event in events
    ]
    # The thread is usable again, which is the whole point.
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None
    reopened = store.create_run(
        thread_id=thread_id,
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="reopen-after-sweep-000001",
    ).value
    assert reopened["state"] == "queued"


def test_the_start_sweep_converges_a_bound_run_a_dead_daemon_left_paused(
    tmp_path: Path,
) -> None:
    """⟦P9-3⟧ The quiet-worker half of the pause parity.

    A `/pause` that is DELIVERED resolves itself: the adapter refuses it and
    the store rolls the run back to `running`, which is new in this slice and
    is the ordinary outcome now. This is the run whose pause was never
    delivered -- the daemon died between the operator's command and the turn
    loop's next poll -- so nothing rejected it, nothing rolled it back, and
    the run sat `pause_requested` with a bound attempt and a worker that died
    with the daemon.

    Before this slice no sweep in the file would look at it: `_undriven`
    answers None for a bound attempt, `_recoverable` filters to
    `{running, starting}`, and `_end_timed_out` returns early outside the same
    pair. It converges under its OWN category, because an operator who asked
    to hold a turn should not be told it was cancelled.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _StalledBackend()
    stranding = _bridge(store, backend)
    try:
        run_id = _stranded_running_run(store, stranding, backend, thread_id)
        run = store.get_run(run_id)
        store.transition_run(
            run_id=run_id,
            target_state="pause_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-pause-000001",
        )
        assert store.get_run(run_id)["state"] == "pause_requested"
        attempt = store.get_attempt(str(store.get_run(run_id)["active_attempt_id"]))
        assert attempt["runtime_binding_id"] is not None
        time.sleep(0.1)
        restarted = _bridge(store, backend, cancel_floor=0.05)
        restarted._sweep_undriven()  # noqa: SLF001 - what `recover` calls first
    finally:
        backend.release.set()

    run = store.get_run(run_id)
    events = store.list_run_events(run_id)
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    # A pause, not a cancel, and never a failure.
    assert terminal["payload"]["category"] == PAUSED_RUNTIME_QUIET
    assert terminal["payload"]["retryable"] is False
    assert "run.failed" not in types
    assert restarted.status()["undriven"]["canceled"] == 1
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None


class _ToolThenQuietBackend(FakeHermesBackend):
    """Emits one tool observation after the cancel, and then nothing at all.

    ⟦P9-3 / fix-verify standing item 6(d)⟧ The one member of the family that
    56894cf made WORSE than its merge base. At 83595a5 a durable event
    arriving under `cancel_requested` ended the run `failed /
    runtime_dispatch_failed` in a twentieth of a second, freeing the thread --
    dishonest, but fast. Once tool observations became DISCARDED events they
    terminated nothing, so a worker that emitted one and then went silent held
    its thread for the whole 900 s turn timeout and was not terminal even
    then. Honest and never is not an improvement on dishonest and fast; the
    floor is what makes it honest and bounded.
    """

    def __init__(self) -> None:
        super().__init__()
        self.proceed = threading.Event()
        self.release = threading.Event()

    def run(self, request: Any, emit: Any) -> HermesRunResult:
        self.run_calls += 1
        self.active.set()
        assert self.proceed.wait(20), "the cancel never committed"
        emit(
            HermesSignal(
                "tool.started",
                {
                    "tool_call_id": "call-1",
                    "tool_name": "search",
                    "arguments": {},
                },
            )
        )
        # And then silence. Bounded ONLY so a regression cannot hang the suite.
        self.release.wait(30)
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str = "",
    ) -> Any:
        outcome = super().cancel(
            run_id, attempt_id, adapter_operation_id, delivery_epoch, session_ref
        )
        if adapter_operation_id.endswith(":cleanup-cancel"):
            self.release.set()
        return outcome


def test_a_run_wedged_behind_a_discarded_tool_event_still_ends(
    tmp_path: Path,
) -> None:
    """⟦P9-3 / standing item 6(d)⟧ A discarded event terminates nothing.

    The worker is cancelled at `running`, emits exactly one more durable
    discardable event, and then says nothing. The event is recorded as
    `runtime.event.discarded` -- correct, and the reason the run is not ended
    `failed / runtime_dispatch_failed` -- but a discarded event is not a
    terminal one, so before the floor this run had no ending at all: the
    turn-timeout rescue refuses `cancel_requested`, restart recovery filters
    to `{running, starting}`, and the start sweep answers None for a bound
    attempt.

    The floor is half a second here and the turn timeout twenty, so a
    regression that only ends this at the timeout fails on the reason rather
    than passing on the outcome.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _ToolThenQuietBackend()
    bridge = _bridge(store, backend, turn_timeout=20.0, cancel_floor=0.5)
    run_id: dict[str, str] = {}

    def cancel_while_running() -> None:
        try:
            assert backend.active.wait(20), "the turn never reached the worker"
            _wait_until(lambda: _run_state(store, thread_id) == "running", seconds=20.0)
            run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
            run_id["id"] = str(run["id"])
            store.transition_run(
                run_id=str(run["id"]),
                target_state="cancel_requested",
                expected_revision=int(run["revision"]),
                actor_id="local-operator",
                idempotency_key="operator-cancel-000014",
            )
        finally:
            backend.proceed.set()

    canceller = threading.Thread(target=cancel_while_running)
    canceller.start()
    started = time.monotonic()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        backend.proceed.set()
        backend.release.set()
        canceller.join(timeout=30)
    elapsed = time.monotonic() - started

    run = store.get_run(run_id["id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    # The wedge shape is really the one under test: the last thing the run
    # heard before the floor was a discarded event.
    discarded = [
        event["payload"]
        for event in events
        if event["type"] == "runtime.event.discarded"
    ]
    assert discarded == [{"event_type": "runtime.tool.started"}], discarded
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_RUNTIME_QUIET
    assert terminal["payload"]["retryable"] is False
    assert "run.failed" not in types
    assert outcome.reason != TURN_TIMEOUT
    assert outcome.outcome == OUTCOME_CANCELED
    assert bridge.status()["last_failure"] is None
    assert elapsed < 10.0, elapsed
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None


def test_each_terminal_says_what_actually_happened_to_the_run(
    tmp_path: Path,
) -> None:
    """⟦P9-3 BRK-5 / BRK-6⟧ The durable detail must not assert a wait nobody had.

    `_end_converging` hard-coded the FLOOR's sentence for all of its callers,
    so a run ended by the operator's next message -- which waits for nothing --
    recorded "no terminal event within 300s of cancel_requested" after 0.01 s.
    And `int()` on the floor rendered any sub-second configuration as "within
    0s", a duration nobody waited either.

    Both are durable payloads an operator reads back on a status surface, so
    both are pinned: the sentence names the writer, and the number survives a
    fractional floor.
    """

    # (a) the abandon path: a long floor it never waited for.
    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), cancel_floor=300.0)
    first = _drain_one(bridge, thread_id)
    parked = store.get_run(str(first.run_id))
    store.transition_run(
        run_id=str(parked["id"]),
        target_state="cancel_requested",
        expected_revision=int(parked["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-cancel-brk5-0001",
    )
    bridge._record(_drain_one(bridge, thread_id))  # noqa: SLF001 - the escape

    terminal = next(
        event
        for event in store.list_run_events(str(first.run_id))
        if event["type"] == "run.canceled"
    )
    detail = str(terminal["payload"]["detail"])
    assert "abandoned for a new message" in detail, detail
    # The defect: the floor's sentence, on a path with no floor in it.
    assert "300" not in detail, detail
    assert "no terminal event" not in detail, detail

    # (b) the floor path, configured below a second.
    other = _store(tmp_path / "second")
    _enable_dispatch(other)
    other_thread = _thread_with_message(other)
    backend = _QuietAfterCancelBackend()
    floored = _bridge(other, backend, cancel_floor=0.5, turn_timeout=20.0)
    run_id: dict[str, str] = {}

    def cancel_once_running() -> None:
        assert backend.active.wait(10), "the turn never started"
        run = other.get_run(str(other.get_thread(other_thread)["active_run_id"]))
        run_id["id"] = str(run["id"])
        other.transition_run(
            run_id=str(run["id"]),
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-brk6-0001",
        )

    canceller = threading.Thread(target=cancel_once_running)
    canceller.start()
    try:
        floored._record(_drain_one(floored, other_thread))  # noqa: SLF001
    finally:
        backend.release.set()
        canceller.join(timeout=30)

    floor_terminal = next(
        event
        for event in other.list_run_events(run_id["id"])
        if event["type"] == "run.canceled"
    )
    floor_detail = str(floor_terminal["payload"]["detail"])
    assert "within 0.5s" in floor_detail, floor_detail
    # The defect: `int(0.5)` is 0, and "within 0s" is not a wait.
    assert "within 0s" not in floor_detail, floor_detail


def test_a_cancel_that_lands_on_a_parked_run_ends_canceled_not_abandoned(
    tmp_path: Path,
) -> None:
    """⟦P9-3 / ADJ14-3⟧ The other ordering: park first, cancel second.

    The A-2 arm only ever saw one ordering -- the cancel, then the worker's
    decision request. In the other one the worker asks first, the run parks at
    `waiting_for_decision`, the bridge ends the turn, and the operator THEN
    presses Cancel. That leaves the run `cancel_requested` with the thread's
    `active_run_id` still held, and the escape is the operator's next message,
    which ran `_abandon` and wrote `failed / turn_abandoned / retryable: true`
    -- a NOTIFICATION_TYPE. So the operator was told that a turn they had
    cancelled had FAILED, and offered a Retry button for it, which is exactly
    the property the rest of this family exists to hold.

    `_abandon` now takes its target from the run's own state, through the same
    writer the floor uses, so a converging run ends `canceled` under its own
    category. `run.canceled` is not a notification type, so nothing is sent.
    Every other abandonable state keeps `turn_abandoned`, which the parked-run
    test above still pins.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=2.0)

    first = _drain_one(bridge, thread_id)
    assert first.outcome == OUTCOME_DECISION_REQUIRED
    parked = store.get_run(str(first.run_id))
    assert parked["state"] == "waiting_for_decision"

    # The operator presses Cancel on the parked run -- a legal edge the
    # cockpit offers a button for.
    store.transition_run(
        run_id=str(parked["id"]),
        target_state="cancel_requested",
        expected_revision=int(parked["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-cancel-000015",
    )
    assert store.get_thread(thread_id)["active_run_id"] == str(first.run_id)

    # Their next message is the escape.
    second = _drain_one(bridge, thread_id)
    bridge._record(second)  # noqa: SLF001 - what the loop does with it

    ended = store.get_run(str(first.run_id))
    events = store.list_run_events(str(first.run_id))
    types = [event["type"] for event in events]
    assert ended["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    # The whole point: `run.failed` is a notification type and `run.canceled`
    # is not, so nothing tells the operator their cancelled turn failed.
    assert "run.failed" not in types, types
    assert "turn_abandoned" not in [
        (event.get("payload") or {}).get("category") for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_RUNTIME_QUIET
    assert terminal["payload"]["retryable"] is False
    # The escape still works: the thread took a fresh run for the new message.
    assert second.run_id != first.run_id
    assert bridge.status()["abandoned"] == 1


# ⟦P9-3 BRK-1⟧ ------------- the worker that dies BECAUSE of the delivered stop


class _ShippedAbortBackend(_ShippedWorkerBackend):
    """The shipped worker whose agent code aborts when the cancel lands.

    ⟦P9-3 BRK-1⟧ The delivered cancel does not politely unwind arbitrary agent
    code. It aborts a provider stream or a tool subprocess, and what surfaces
    is whatever THAT raises -- not `TurnCanceled`. The shipped `Turn._body`
    routes anything other than `TurnCanceled` through its `except
    BaseException` arm and reports `{"canceled": False, "failed": True}`.

    So this is not an exotic worker: it is the ordinary consequence of the
    cancel P9-3 taught the product to deliver, and the reason a fix that only
    handles `runtime.run.completed` leaves the common path failing. The
    `failed` this reports is written by the shipped outcome branch, decoded
    off the shipped finish frame -- the test never asserts it into existence.
    """

    def __init__(self, home: Path, gate: _ForkGate) -> None:
        super().__init__(home, gate)
        #: The runner is inside the turn, before the cancel.
        self.running = threading.Event()
        #: The runner saw `context.canceled` and aborted.
        self.aborted = threading.Event()

    def _make_runner(self, fork_runner: Any) -> Any:
        def runner(request: Any, context: Any) -> dict[str, Any]:
            self.running.set()
            deadline = time.monotonic() + 30.0
            while not context.canceled:
                if time.monotonic() > deadline:
                    raise AssertionError("the cancel was never delivered")
                time.sleep(0.01)
            self.aborted.set()
            # Deliberately NOT `TurnCanceled`: that is the clean path the
            # shipped worker already maps to `canceled`, and it is not what a
            # torn-down provider stream raises.
            raise RuntimeError("provider stream aborted")

        return runner


def test_a_worker_that_dies_from_the_delivered_cancel_ends_canceled(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """⟦P9-3 BRK-1⟧ The operator's stop wins over the worker's outcome.

    P9-3 gave the converging block an arm for `runtime.run.completed` and none
    for `runtime.run.failed`, so the slice that set out to stop telling an
    operator their cancelled turn FAILED did exactly that on the most likely
    path of all: cancel delivered, agent code torn down, shipped worker
    reports `failed`, run ends `failed / runtime_execution_failed /
    retryable: true` -- and `run.failed` IS a Telegram notification type, so
    the operator gets a push with a Retry button for a turn they stopped.

    The run now converges to `canceled` under its own category, with the
    worker's detail carried through so nothing is hidden about why it stopped.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    home = tmp_path / "fork-home"
    home.mkdir()
    gate = _ForkGate()
    _fork_double(monkeypatch, home, gate)
    backend = _ShippedAbortBackend(home, gate)
    bridge = _bridge(store, backend, turn_timeout=30.0)
    run_id: dict[str, str] = {}

    def cancel_once_running() -> None:
        assert backend.running.wait(20), "the shipped turn never started"
        run = store.get_run(str(store.get_thread(thread_id)["active_run_id"]))
        run_id["id"] = str(run["id"])
        assert run["state"] == "running", run["state"]
        store.transition_run(
            run_id=str(run["id"]),
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancel-brk1-0001",
        )

    canceller = threading.Thread(target=cancel_once_running)
    canceller.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        canceller.join(timeout=30)

    # The shipped worker really did abort, and really did report failure.
    assert backend.aborted.is_set(), "the runner never saw the cancel"
    assert backend.finished.get("result", {}).get("failed") is True, backend.finished

    run = store.get_run(run_id["id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "canceled", [
        (event["type"], event["payload"]) for event in events
    ]
    # The defect, named so a regression is unambiguous: `run.failed` is a
    # notification type and `run.canceled` is not.
    assert "run.failed" not in types, types
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == "runtime_failed_after_cancel"
    assert terminal["payload"]["retryable"] is False
    # The worker's own account of why it stopped survives the conversion.
    assert terminal["payload"].get("detail"), terminal["payload"]
    assert store.get_thread(thread_id)["active_run_id"] is None


# ⟦P9-4⟧ ------------------------- the decision, delivered to the parked worker


class _ShippedDecisionBackend(_ShippedWorkerBackend):
    """The shipped worker parked on a real approval, answered the real way.

    ⟦P9-4⟧ Adds the two halves P9-3's cancel test deliberately left out. The
    `decision.required` frame is RELAYED onto the adapter's signal stream, so
    the platform creates the decision and the run genuinely reaches
    `waiting_for_decision` -- the state that used to end the turn. And
    `resolve_decision` is the shipped `Turn.resolve`, the method a
    `turn.resolve` frame reaches through `serve.py`'s dispatch, so the
    operator's answer travels to `TurnContext.await_decision` exactly as it
    would in production.
    """

    def __init__(self, home: Path, gate: _ForkGate) -> None:
        super().__init__(home, gate)
        self.resolve_calls = 0

    def _relay(self, emit: Any, kind: str, payload: dict[str, Any]) -> None:
        if kind == "decision.required":
            emit(HermesSignal(kind, payload))

    def resolve_decision(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        choice: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> Any:
        def effect() -> bool:
            self.resolve_calls += 1
            turn = self.turn
            if turn is None:
                return False
            # Exactly what a `turn.resolve` frame does on the worker's side:
            # `serve.py` calls `Turn.resolve`, which validates the choice
            # against the shipped vocabulary and releases the parked gate.
            return turn.resolve({"decision_id": decision_id, "choice": choice})

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


@pytest.mark.parametrize(
    "choice, fork_choice",
    [("approve_once", "once"), ("deny", "deny")],
)
def test_the_shipped_worker_parked_on_an_approval_is_handed_the_answer(
    tmp_path: Path, monkeypatch: Any, choice: str, fork_choice: str
) -> None:
    """⟦P9-4⟧ The defect a real gen-13 run found: an approved turn never runs.

    `ControlStore.resolve_decision` queues a `decision.resolve` runtime action
    in the transaction that records the operator's answer, and nothing
    delivered it -- the same missing caller as the cancel, with a worse
    consequence: a cancelled turn at least ends, while an APPROVED turn simply
    never ran. On the real install the run moved to `resuming` and stayed
    there, holding its thread, with the action `pending`.

    Delivering it is only possible from inside the turn, and that is the whole
    design. `HermesAdapter.resolve_decision` answers `pending_decision_missing`
    unless the decision is in `_decision_identities`, which is per-adapter
    state the bridge rebuilds every turn; the managed backend answers
    `no_active_turn` without a live execution; and `serve.py` answers `no such
    open turn` once the worker's `Turn` is cancelled. Abandoning the park --
    which is what the bridge did -- closed all three doors at once.

    Both answers are covered because they are different code paths in the
    shipped mapping: `approve_once` becomes the fork's `once` and the tool
    runs, `deny` becomes `deny` and the tool is refused -- and in both the
    turn CONTINUES and produces an answer, which is the point.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    home = tmp_path / "fork-home"
    home.mkdir()
    gate = _ForkGate()
    _fork_double(monkeypatch, home, gate)
    backend = _ShippedDecisionBackend(home, gate)
    bridge = _bridge(store, backend, turn_timeout=30.0, decision_wait=25.0)
    run_id: dict[str, str] = {}

    parked_reads = _watch_parked(bridge)

    def answer_when_asked() -> None:
        gate.proceed.set()
        _wait_until(
            lambda: bool(store.list_decisions(state="pending")), seconds=20.0
        )
        # The loop must have SEEN the park and kept the turn open before the
        # operator answers; otherwise this test would pass against a bridge
        # that abandons the park on sight.
        _wait_until(lambda: len(parked_reads) >= 2, seconds=20.0)
        decision = store.list_decisions(state="pending")[0]
        run_id["id"] = str(decision["run_id"])
        # The run really did park: this is the state that used to end the turn.
        assert store.get_run(run_id["id"])["state"] == "waiting_for_decision"
        store.resolve_decision(
            decision_id=str(decision["id"]),
            choice=choice,
            expected_revision=int(decision["revision"]),
            actor_id="local-operator",
            idempotency_key=f"operator-resolve-{choice}-01",
        )

    answering = threading.Thread(target=answer_when_asked)
    answering.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        gate.proceed.set()
        answering.join(timeout=30)

    # The shipped park returned the operator's answer, mapped by the shipped
    # vocabulary into the fork's own word.
    # The turn was HELD across the park: the loop read the parked state more
    # than once and did not abandon the worker.
    assert len(parked_reads) >= 2, parked_reads
    assert gate.asked.is_set()
    assert gate.left.is_set(), "the shipped worker never left `await_decision`"
    assert gate.choice == fork_choice, gate.choice
    assert backend.resolve_calls == 1
    assert backend.turn is not None
    assert backend.turn.context.canceled is False

    run = store.get_run(run_id["id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "completed", [
        (event["type"], event["payload"]) for event in events
    ]
    # The action was delivered and acknowledged, which is what put the run
    # back into `running` inside the same transaction.
    action = next(
        event for event in events if event["type"] == "runtime.action.acked"
    )
    assert action["payload"]["kind"] == "decision.resolve"
    assert "decision.resolved" in types
    assert "run.failed" not in types
    assert store.list_decisions(state="pending") == []
    # The turn continued and answered -- including on a denial.
    answered = _assistant_messages(store, thread_id)
    assert len(answered) == 1
    assert f"({fork_choice})" in answered[0]["content"]
    assert outcome.outcome == OUTCOME_ANSWERED
    assert bridge.status()["resolutions"] == 1
    assert bridge.status()["last_failure"] is None
    # The thread is free again, which is what the operator lost.
    assert store.get_thread(thread_id)["active_run_id"] is None


def test_a_decision_resolved_through_the_real_route_completes_the_turn(
    tmp_path: Path,
) -> None:
    """⟦P9-4⟧ End to end on the route the cockpit and Telegram actually call.

    The store-level tests above prove the worker is reached. This one proves
    the product is: the operator's answer goes through `ControlAPI`'s real
    `POST /api/v1/decisions/{id}/resolve`, with its token, its idempotency key
    and its compare-and-set body, while the bridge is driving the turn on
    another thread. Nothing is left in `resuming`, which is the state the real
    gen-13 run was stuck in.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    bridge = _bridge(store, backend, turn_timeout=30.0, decision_wait=25.0)
    api = _control_api(store, bridge)
    resolved: dict[str, Any] = {}

    parked_reads = _watch_parked(bridge)

    def resolve_through_the_api() -> None:
        _wait_until(
            lambda: bool(store.list_decisions(state="pending")), seconds=20.0
        )
        _wait_until(lambda: len(parked_reads) >= 2, seconds=20.0)
        decision = store.list_decisions(state="pending")[0]
        resolved["run_id"] = str(decision["run_id"])
        assert store.get_run(resolved["run_id"])["state"] == "waiting_for_decision"
        resolved["response"] = api.handle(
            method="POST",
            target=f"/api/v1/decisions/{decision['id']}/resolve",
            headers={
                "X-Cortex-Control-Token": "x" * 48,
                "Idempotency-Key": "operator-resolve-route-0001",
            },
            body=json.dumps(
                {
                    "choice": "approve_once",
                    "expected_revision": int(decision["revision"]),
                }
            ).encode(),
        )

    answering = threading.Thread(target=resolve_through_the_api)
    answering.start()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        answering.join(timeout=30)

    assert len(parked_reads) >= 2, parked_reads
    assert resolved["response"].status == 200, resolved["response"].payload
    run = store.get_run(resolved["run_id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "completed", [
        (event["type"], event["payload"]) for event in events
    ]
    # The state the real install was stuck in is not where this ends.
    assert run["state"] != "resuming"
    assert "runtime.action.acked" in types
    assert "run.failed" not in types
    assert outcome.outcome == OUTCOME_ANSWERED
    # ⟦P9-4⟧ The accounting item: a resolved park is counted as a resolution
    # and never as a `resuming` turn.
    status = bridge.status()
    assert status["resolutions"] == 1
    assert "resuming" not in status["reasons"], status["reasons"]
    assert status["outcomes"] == {OUTCOME_ANSWERED: 1}
    assert backend.decision_choice == "approve_once"
    assert store.get_thread(thread_id)["active_run_id"] is None
    assert store.list_pending_runtime_actions() == []


class _RejectingResolveBackend(FakeHermesBackend):
    """Refuses the delivered answer, the way a worker that lost it does.

    ⟦P9-4⟧ `HermesAdapter.resolve_decision` answers `pending_decision_missing`
    when its per-adapter `_decision_identities` no longer holds the decision,
    and `ManagedHermesBackend` answers `no_active_turn` when the worker's turn
    is gone. Neither moves the run: `reject_runtime_action` settles the action
    and leaves the state exactly where it was. So a refused delivery is the
    wedge again, one attempt later, and only a floor ends it.
    """

    def resolve_decision(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        choice: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> Any:
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
            lambda: False,
            "pending_decision_missing",
        )


def test_a_resume_the_worker_never_took_ends_instead_of_hanging(
    tmp_path: Path,
) -> None:
    """⟦P9-4⟧ The floor under `resuming`, and why it is a FAILURE.

    Delivering the answer is not a guarantee the worker takes it. A refused
    delivery leaves the run in `resuming` with its thread held, and nothing
    else in this file would end it: `_end_timed_out` returns early outside
    `{running, starting}`, `_recoverable` filters to the same pair, and
    `_undriven` answers None for a bound attempt.

    It ends `failed`, not `canceled`, and it IS retryable -- the opposite of
    the cancel floor, on purpose. The operator ANSWERED an approval and asked
    the turn to continue, so a turn that then never ran is a failure in the
    ordinary sense, and `run.failed` being a notification type is exactly
    right here: they are owed the news that the answer they authorised never
    took, and a Retry button that works.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _RejectingResolveBackend()
    bridge = _bridge(
        store, backend, turn_timeout=30.0, decision_wait=25.0, cancel_floor=0.3
    )
    parked_reads = _watch_parked(bridge)
    resolved: dict[str, str] = {}

    def answer_when_asked() -> None:
        _wait_until(
            lambda: bool(store.list_decisions(state="pending")), seconds=20.0
        )
        _wait_until(lambda: len(parked_reads) >= 2, seconds=20.0)
        decision = store.list_decisions(state="pending")[0]
        resolved["run_id"] = str(decision["run_id"])
        store.resolve_decision(
            decision_id=str(decision["id"]),
            choice="approve_once",
            expected_revision=int(decision["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-resolve-refused-1",
        )

    answering = threading.Thread(target=answer_when_asked)
    answering.start()
    started = time.monotonic()
    try:
        outcome = _drain_one(bridge, thread_id)
        bridge._record(outcome)  # noqa: SLF001 - what the loop does with it
    finally:
        answering.join(timeout=30)
    elapsed = time.monotonic() - started

    run = store.get_run(resolved["run_id"])
    events = store.list_run_events(str(run["id"]))
    types = [event["type"] for event in events]
    assert run["state"] == "failed", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.failed")
    assert terminal["payload"]["category"] == QUIET_AFTER_RESUME
    # Retryable, unlike the cancel floor: the operator still wants an answer.
    assert terminal["payload"]["retryable"] is True
    assert outcome.reason != TURN_TIMEOUT
    # It ended at the floor, not at the thirty-second turn timeout.
    assert elapsed < 10.0, elapsed
    # Nothing was resolved, so nothing is counted as resolved.
    assert bridge.status()["resolutions"] == 0
    # The thread is usable again, which is what the wedge took away.
    thread = store.get_thread(thread_id)
    assert thread["active_run_id"] is None
    reopened = store.create_run(
        thread_id=thread_id,
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="reopen-after-resume-00001",
    ).value
    assert reopened["state"] == "queued"


def test_the_start_sweep_converges_a_run_left_resuming_by_a_dead_daemon(
    tmp_path: Path,
) -> None:
    """⟦P9-4⟧ The restart half: the operator answered after the turn was gone.

    The window is finite, so an operator who answers late still finds the turn
    abandoned -- and their answer still moves the run to `resuming` with a
    `decision.resolve` nobody can deliver, because the worker it named died
    with the turn. That is the exact state the real gen 13 run was left in.

    The sweep converges it under the same category the in-turn floor uses, and
    counts it apart from `canceled`: a run that ended `failed` must never be
    tallied as one the operator cancelled.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    # A window that expires at once: the operator is slower than the turn.
    abandoning = _bridge(store, backend, turn_timeout=30.0, decision_wait=0.05)
    outcome = _drain_one(abandoning, thread_id)
    assert outcome.outcome == OUTCOME_DECISION_REQUIRED
    run_id = str(outcome.run_id)
    assert store.get_run(run_id)["state"] == "waiting_for_decision"

    decision = store.list_decisions(state="pending")[0]
    store.resolve_decision(
        decision_id=str(decision["id"]),
        choice="approve_once",
        expected_revision=int(decision["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-resolve-too-late",
    )
    # Exactly what the real install showed: `resuming`, thread held, action
    # pending, and nothing coming.
    assert store.get_run(run_id)["state"] == "resuming"
    assert store.get_thread(thread_id)["active_run_id"] == run_id
    assert [a["kind"] for a in store.list_pending_runtime_actions()] == [
        "decision.resolve"
    ]

    time.sleep(0.1)
    restarted = _bridge(store, backend, cancel_floor=0.05)
    restarted._sweep_undriven()  # noqa: SLF001 - what `recover` calls first

    run = store.get_run(run_id)
    events = store.list_run_events(run_id)
    assert run["state"] == "failed", [
        (event["type"], event["payload"]) for event in events
    ]
    terminal = next(event for event in events if event["type"] == "run.failed")
    assert terminal["payload"]["category"] == QUIET_AFTER_RESUME
    assert terminal["payload"]["retryable"] is True
    counts = restarted.status()["undriven"]
    assert counts["stalled"] == 1
    assert counts["canceled"] == 0, counts
    assert store.get_thread(thread_id)["active_run_id"] is None


def test_recover_reconciles_a_decision_action_whose_outcome_was_never_learned(
    tmp_path: Path,
) -> None:
    """⟦P9-4⟧ The restart owes a `decision.resolve` the same settlement.

    Reconciliation was wired for the cancel path, and it selects on outcome
    rather than on kind -- but "it should also cover decisions" is a claim
    about behaviour, not about a SQL predicate, so it is asserted here on the
    kind that carries an operator's answer.

    The shape: a daemon delivered the resolution and died before it heard back,
    so the action is fenced `outcome_unknown` -- the one state that is never
    retried blindly, because re-delivering an answer the worker may already
    have acted on is not idempotent. Nothing but reconciliation settles it, and
    before `recover` called it the row stayed unknown for the life of the
    installation.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    abandoning = _bridge(store, backend, turn_timeout=30.0, decision_wait=0.05)
    outcome = _drain_one(abandoning, thread_id)
    assert outcome.outcome == OUTCOME_DECISION_REQUIRED
    run_id = str(outcome.run_id)

    decision = store.list_decisions(state="pending")[0]
    store.resolve_decision(
        decision_id=str(decision["id"]),
        choice="approve_once",
        expected_revision=int(decision["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-resolve-then-crash",
    )
    action = store.list_pending_runtime_actions()[0]
    assert action["kind"] == "decision.resolve"
    action_id = str(action["id"])

    claim = store.claim_runtime_action(
        action_id=action_id,
        worker_id="cortexd-turn-test",
        lease_seconds=30,
        actor_id="cortexd-turn-test",
        idempotency_key="claim-the-resolution-before-the-crash",
    )
    store.mark_runtime_action_outcome_unknown(
        action_id=action_id,
        claim_owner="cortexd-turn-test",
        claim_epoch=int(claim.value["claim_epoch"]),
        expected_revision=int(store.get_run(run_id)["revision"]),
        category="delivered_then_daemon_died",
        actor_id="cortexd-turn-test",
        idempotency_key="fence-the-resolution-outcome-unknown",
    )
    assert store.get_runtime_action(action_id)["outcome_state"] == "outcome_unknown"
    # Kind-agnostic by construction; asserted so a later `kind = 'control.%'`
    # narrowing fails here rather than in an install.
    assert [
        str(candidate["id"])
        for candidate in store.list_runtime_actions_requiring_reconciliation()
    ] == [action_id]

    restarted = _bridge(store, backend)
    restarted.recover()

    reconciled = next(
        event
        for event in store.list_run_events(run_id)
        if event["type"] == "runtime.action.reconciliation_deferred"
    )
    assert reconciled["payload"]["kind"] == "decision.resolve"
    # The adapter that delivered it died with its outcome cache, and no
    # backend will attest to an operation it has no record of -- so the honest
    # answer is still-unknown, and the row stays fenced for inspection rather
    # than being re-delivered blind. What this pins is that the restart ASKED:
    # before the reconcile call the row was never looked at again at all.
    settled = store.get_runtime_action(action_id)
    assert settled["failure_category"] == "runtime_action_outcome_still_unknown"
    assert settled["acknowledged_at"] is None


def test_a_park_answered_at_the_last_instant_is_never_tallied_as_resuming(
    tmp_path: Path,
) -> None:
    """⟦P9-4⟧ `reasons` is cumulative for the life of the daemon.

    The real gen 13 health showed `reasons {resuming: 1}`. That entry comes
    from one place: the turn breaks on the park, and the operator's answer
    lands in the instant before `_classify` re-reads the run, so the reason is
    recorded as the state string `resuming`. It then sits in a counter that is
    never decremented and never cleared, describing a state no turn ever ENDS
    in, next to genuine refusal categories.

    A parked turn now reports what the turn did. A run that has reached a
    terminal in the same window is classified by that terminal instead, so
    nothing is lost -- the reason stops being a place transient run states can
    leak into.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    bridge = _bridge(store, backend, turn_timeout=30.0, decision_wait=0.05)

    outcome = _drain_one(bridge, thread_id)
    run_id = str(outcome.run_id)
    decision = store.list_decisions(state="pending")[0]
    store.resolve_decision(
        decision_id=str(decision["id"]),
        choice="approve_once",
        expected_revision=int(decision["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-resolve-late-0001",
    )
    assert store.get_run(run_id)["state"] == "resuming"

    # Exactly the interleaving that produced the real health entry: the turn
    # parked, and by the time it is classified the run has moved on.
    late = bridge._classify(thread_id, run_id, parked=True)  # noqa: SLF001
    bridge._record(late)  # noqa: SLF001 - what the loop does with it

    assert late.outcome == OUTCOME_DECISION_REQUIRED
    assert late.reason == "waiting_for_decision"
    reasons = bridge.status()["reasons"]
    assert "resuming" not in reasons, reasons

    # And nothing is lost: a parked turn whose run reached a terminal in the
    # same window is still classified by that terminal.
    store.transition_run(
        run_id=run_id,
        target_state="cancel_requested",
        expected_revision=int(store.get_run(run_id)["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-cancel-late-00001",
    )
    attempt = store.get_attempt(str(store.get_run(run_id)["active_attempt_id"]))
    current = store.get_run(run_id)
    store.apply_runtime_transition(
        run_id=run_id,
        attempt_id=str(attempt["id"]),
        runtime_binding_id=str(attempt["runtime_binding_id"]),
        runtime_release_id=str(attempt["runtime_release_id"]),
        state_generation_id=attempt.get("state_generation_id"),
        target_state="canceled",
        expected_revision=int(current["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-canceled-late-0001",
        payload={"category": "runtime_canceled", "retryable": False},
    )
    terminal = bridge._classify(thread_id, run_id, parked=True)  # noqa: SLF001
    assert terminal.outcome == OUTCOME_CANCELED
    assert terminal.reason == "runtime_canceled"


def test_a_turn_that_asks_for_a_decision_ends_typed_and_does_not_hang(
    tmp_path: Path,
) -> None:
    """⟦c3⟧ Approval vocabulary is a further slice; the turn still ends.

    The fake parks in its approval callback exactly as the real worker does.
    Nobody is going to answer it here, so the bridge must not wait for the
    runtime stream -- which never ends -- but read the durable state that says
    the run is parked, record it, and abandon the turn.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=30.0)

    started = time.monotonic()
    outcome = _drain_one(bridge, thread_id)
    elapsed = time.monotonic() - started

    assert outcome.outcome == OUTCOME_DECISION_REQUIRED, outcome
    # Bounded by the run reaching `waiting_for_decision`, not by the 30 s
    # ceiling: the ceiling exists for a turn that satisfies nothing at all.
    assert elapsed < 20.0
    assert store.get_run(str(outcome.run_id))["state"] == "waiting_for_decision"
    events = [
        event["type"] for event in store.list_run_events(str(outcome.run_id))
    ]
    assert "decision.required" in events
    # And the decision card is what the projection will render, so the reply
    # the operator receives says a decision is required.
    assert store.list_decisions(state="pending") != []


def test_a_message_during_a_live_turn_joins_the_run_it_is_a_follow_up_to(
    tmp_path: Path,
) -> None:
    """⟦rule 2⟧ One run per scope; the ledger's index says the same thing.

    Enforced by `submit`, which is the seam the poller actually uses: a thread
    whose turn is queued or running is remembered as a follow-up rather than
    queued twice, so `_run_turn` is never re-entered concurrently for it.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=2.0)

    bridge.submit(thread_id)
    bridge.submit(thread_id)

    assert bridge.status()["queued"] == 1
    assert bridge._followups == {thread_id}  # noqa: SLF001


def test_a_second_message_frees_a_scope_parked_on_an_unanswerable_decision(
    tmp_path: Path,
) -> None:
    """⟦F-B6⟧ The documented escape, because there was none.

    A parked run has no consumer in this product: `/cancel` moves it to
    `cancel_requested`, after which every message answers `turn_in_flight`; the
    decision button moves it to `resuming`, which nothing dispatches; and no
    restart converges either, because `DEFERRABLE_STATES` is
    `{running, starting}`. One parked turn ended the window's inbound half for
    that scope permanently.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=2.0)

    first = _drain_one(bridge, thread_id)
    assert first.outcome == OUTCOME_DECISION_REQUIRED

    second = _drain_one(bridge, thread_id)

    assert second.run_id != first.run_id
    parked = store.get_run(str(first.run_id))
    assert parked["state"] == "failed"
    abandoned = next(
        event
        for event in store.list_run_events(str(first.run_id))
        if event["type"] == "run.failed"
    )
    assert abandoned["payload"]["category"] == "turn_abandoned"
    assert abandoned["payload"]["detail"] == "waiting_for_decision"
    # `run.failed` is a notification type, so the operator is told once that
    # the parked turn was abandoned. Not a duplicate: it produced no delivery.
    assert bridge.status()["abandoned"] == 1


def test_a_run_this_process_is_driving_is_never_abandoned(tmp_path: Path) -> None:
    """⟦F-B6⟧ Keyed off dispatch ownership, so it cannot race a live turn."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=2.0)
    first = _drain_one(bridge, thread_id)
    assert first.outcome == OUTCOME_DECISION_REQUIRED

    bridge._driving.add(str(first.run_id))  # noqa: SLF001
    second = _drain_one(bridge, thread_id)

    assert second.run_id == first.run_id
    assert second.outcome == OUTCOME_DECISION_REQUIRED
    assert store.get_run(str(first.run_id))["state"] == "waiting_for_decision"
    assert second.reason == "already_parked"


def test_submit_never_blocks_the_poller_and_says_so_when_it_cannot_take_more(
    tmp_path: Path,
) -> None:
    """The poller's next frame must not wait on a turn, ever."""

    store = _store(tmp_path)
    bridge = _bridge(store, FakeHermesBackend(), queue_limit=1)

    bridge.submit("thread-one")
    # Same thread twice is a follow-up, not a second queue entry.
    bridge.submit("thread-one")
    assert bridge.status()["queued"] == 1

    bridge.submit("thread-two")
    status = bridge.status()
    assert status["reasons"].get(REFUSED_QUEUE_FULL) == 1
    assert status["queued"] == 1


def test_status_carries_categories_and_never_a_message_body(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend())

    bridge._record(_drain_one(bridge, thread_id))  # noqa: SLF001
    status = bridge.status()

    assert status["outcomes"] == {OUTCOME_REFUSED: 1}
    assert status["reasons"] == {REFUSED_DISPATCH_DISABLED: 1}
    assert set(status["last"]) == {
        "thread_id",
        "run_id",
        "outcome",
        "reason",
        "detail",
    }
    assert "What did the paper claim?" not in repr(status)


def test_refusals_and_sweep_decisions_are_logged_one_line_each_without_a_body(
    tmp_path: Path, caplog: "pytest.LogCaptureFixture"
) -> None:
    """⟦P9⟧ The bridge has a logger; decisions are one INFO line, ids and reasons only.

    Before this the bridge's refusals and its start sweep were observable
    only through `bridge.status()` counters and the control DB. Each is now
    one line on `cortex_platform.product.transports.bridge` (INFO; the
    daemon's root handler formats it through `RedactingFormatter`), naming
    the thread / run and the category -- never a message body, never a
    detail payload.
    """

    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend())
    logger = "cortex_platform.product.transports.bridge"

    with caplog.at_level(logging.INFO, logger=logger):
        bridge._record(_drain_one(bridge, thread_id))  # noqa: SLF001
    refusals = [record for record in caplog.records if record.name == logger]
    assert len(refusals) == 1
    assert refusals[0].levelno == logging.INFO
    assert thread_id in refusals[0].getMessage()
    assert REFUSED_DISPATCH_DISABLED in refusals[0].getMessage()
    assert "What did the paper claim?" not in refusals[0].getMessage()

    caplog.clear()
    _enable_dispatch(store)
    run_id = _api_created_run(store, thread_id)
    with caplog.at_level(logging.INFO, logger=logger):
        bridge._sweep_undriven()  # noqa: SLF001
    sweep = [record for record in caplog.records if record.name == logger]
    assert len(sweep) == 1
    assert run_id in sweep[0].getMessage()
    assert "What did the paper claim?" not in sweep[0].getMessage()
    assert bridge.status()["undriven"]["submitted"] == 1


# ⟦P5.4d⟧ ------------------------------------------------------- observability


class _FailingBackend(FakeHermesBackend):
    """A worker whose turn ran and failed, which is the thing with no text.

    Distinct from `mode="run_error"`: that raises in the PRODUCT process, where
    the exception is in hand. This one returns the shape the real worker returns
    after `turn.py` has deliberately thrown the exception away.
    """

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        return HermesRunResult(
            request.session_ref, final_response=None, failed=True
        )


def test_a_product_side_failure_names_the_exception_it_actually_was(
    tmp_path: Path,
) -> None:
    """⟦F9⟧ `runtime_execution_failed` alone cost a slice; the detail is why.

    Every managed turn that cannot reach a model ends in the same category, so
    the category is not a diagnosis. The detail is the exception's own class and
    first line, produced by the adapter and carried unchanged through Control to
    the surface an operator reads.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(mode="run_error"))

    outcome = _drain_one(bridge, thread_id)
    bridge._record(outcome)  # noqa: SLF001 - the loop's own bookkeeping

    assert outcome.outcome == OUTCOME_FAILED
    assert outcome.reason == "runtime_execution_failed"
    assert outcome.detail == "RuntimeError: backend run failed"
    failed = next(
        event
        for event in store.list_run_events(str(outcome.run_id))
        if event["type"] == "run.failed"
    )
    assert failed["payload"]["detail"] == "RuntimeError: backend run failed"
    # The sticky copy is what `transport status` and `/api/v1/health` read, and
    # it survives a later refusal or a later answer.
    status = bridge.status()
    assert status["last_failure"]["reason"] == "runtime_execution_failed"
    assert status["last_failure"]["detail"] == "RuntimeError: backend run failed"


def test_a_worker_side_failure_is_named_rather_than_left_blank(
    tmp_path: Path,
) -> None:
    """The worker throws its exception away on purpose, so this side says so.

    An empty detail would read as "nobody looked". `worker_reported_failure` is
    the honest answer, and it is what tells an operator the turn reached the
    fork at all -- which is exactly the distinction F9 spent a slice on.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, _FailingBackend())

    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_FAILED
    assert outcome.reason == "runtime_execution_failed"
    assert outcome.detail == WORKER_REPORTED_FAILURE


def test_the_detail_can_never_carry_a_credential_or_an_unbounded_blob() -> None:
    """⟦AMD-4⟧ A detail is only worth carrying if it cannot be the leak.

    The scrub is by shape, not by scheme: anything long enough to be a key,
    a token or a digest and with no separator in it is not a word.
    """

    token = "0000000000:AAH" + "x" * 30
    detail = failure_detail(RuntimeError(f"send refused for {token}"))
    assert token not in detail
    assert "<redacted>" in detail
    assert detail.startswith("RuntimeError: ")

    long = failure_detail(RuntimeError("word " * 400))
    assert len(long) <= FAILURE_DETAIL_LIMIT

    # Only the first line: a traceback pasted into a message is not a detail.
    assert failure_detail(RuntimeError("first\nsecond")) == "RuntimeError: first"
    assert failure_detail(RuntimeError()) == "RuntimeError"


def test_a_recovery_that_does_nothing_says_why(tmp_path: Path) -> None:
    """⟦P5.4d⟧ `recover()` must not fail a start, and must not be silent.

    A daemon restarted mid-turn left the run `running` for ever and wrote no
    recovery command at all; the `except Exception: return []` that keeps a
    start from failing is also what made the reason unavailable to health, to
    `transport status` and to the operator.
    """

    store = _store(tmp_path)

    def refuse():
        raise ManagedWorkerUnavailable("release_not_approved")

    bridge = InboundTurnBridge(
        store=store,
        worker=None,  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
        orchestrator_factory=refuse,
    )

    assert bridge.recover() == []
    recovery = bridge.status()["recovery"]
    assert recovery["state"] == "refused"
    assert recovery["reason"] == "release_not_approved"
    assert recovery["attempts"] == 1


def test_recovery_is_retried_from_the_loop_when_the_worker_was_not_ready(
    tmp_path: Path,
) -> None:
    """⟦P5.4d⟧ One try at daemon start is not a recovery policy.

    `recover()` runs before the window supervisor has reconciled and, after a
    restart, possibly before the previous daemon's worker has finished dying.
    A refusal at that instant used to be final, which is how a daemon restarted
    mid-turn left its run `running` with no recovery command ever written.
    """

    store = _store(tmp_path)
    calls: list[int] = []

    def factory():
        calls.append(1)
        if len(calls) < 3:
            # The previous daemon's worker still holds the slot's ledger, and
            # will not for much longer. ⟦P54D-2⟧ Every refusal is re-asked at
            # the 3 s cadence without spending `RECOVERY_ATTEMPTS`: each of
            # their reasons is an operator decision (or a process exiting) that
            # is re-read at every launch and can change under a running daemon.
            raise ManagedWorkerUnavailable("slot_ledger_held_elsewhere")
        return RunOrchestrator(
            store, HermesAdapter(backend_loader=lambda: FakeHermesBackend()), Releases()
        )

    bridge = InboundTurnBridge(
        store=store,
        worker=None,  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
        orchestrator_factory=factory,
    )
    bridge.RECOVERY_RETRY_SECONDS = 0.05  # type: ignore[misc]

    assert bridge.recover() == []
    assert bridge.status()["recovery"]["state"] == "refused"
    bridge.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if bridge.status()["recovery"]["state"] == "converged":
                break
            time.sleep(0.05)
    finally:
        bridge.stop(timeout=5.0)
    recovery = bridge.status()["recovery"]
    assert recovery["state"] == "converged", recovery
    # ⟦F-B9⟧ `attempts` is the BUDGET spent; `asks` is how many times recovery
    # was actually run, which is what "it was retried" means.
    assert int(recovery["asks"]) >= 3
    assert int(recovery["attempts"]) == 0


# ⟦P5.4d / c4⟧ ------------------------------------------- deferred recovery


class _StalledBackend(FakeHermesBackend):
    """A turn still inside the worker when the daemon that owned it died.

    The whole of c4 is this state: a run left `running` by a process that is
    gone. The fake enters `run` and never returns, so the run reaches `running`
    with a real binding and a real attempt and nothing driving it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, request, emit):
        self.run_calls += 1
        self.entered.set()
        self.release.wait(30)
        return HermesRunResult(request.session_ref, final_response="Hello")


def _bridge_with_factory(store: ControlStore, factory) -> InboundTurnBridge:
    return InboundTurnBridge(
        store=store,
        worker=None,  # type: ignore[arg-type] - the factory replaces it
        actor_id="cortexd-turn-test",
        orchestrator_factory=factory,
    )


def _stranded_running_run(
    store: ControlStore, bridge: InboundTurnBridge, backend: _StalledBackend, thread_id: str
) -> str:
    """Leave one run `running` with nobody driving it, the way a crash does."""

    def drive() -> None:
        try:
            bridge._run_turn(thread_id)  # noqa: SLF001
        except BaseException:  # noqa: BLE001 - the abandoned dispatch, by design
            pass

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    assert backend.entered.wait(20), "the turn never reached the backend"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        active = store.get_thread(thread_id).get("active_run_id")
        if active and store.get_run(str(active))["state"] == "running":
            return str(active)
        time.sleep(0.02)
    raise AssertionError("the run never reached `running`")


def test_a_closed_window_defers_recovery_and_ends_the_attempt_typed(
    tmp_path: Path,
) -> None:
    """⟦c4⟧ Recovery does not launch the release outside a transport window.

    A worker acquired outside a window would hold no bot token and could not
    deliver the turn's reply anyway, so `acquire()` refuses first -- which is
    correct and used to be the end of it: the run stayed `running` for ever and
    no recovery command was ever written. The attempt now ends typed, with the
    reason on the event, and the deferral is remembered and counted.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _StalledBackend()
    calls: list[int] = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            return RunOrchestrator(
                store, HermesAdapter(backend_loader=lambda: backend), Releases()
            )
        raise ManagedWorkerUnavailable("transport_gate_closed")

    bridge = _bridge_with_factory(store, factory)
    run_id = _stranded_running_run(store, bridge, backend, thread_id)
    try:
        assert bridge.recover() == []
    finally:
        backend.release.set()

    run = store.get_run(run_id)
    assert run["state"] == "failed", run
    failed = next(
        event
        for event in store.list_run_events(run_id)
        if event["type"] == "run.failed"
    )
    assert failed["payload"]["category"] == RECOVERY_DEFERRED
    assert failed["payload"]["detail"] == "transport_gate_closed"
    assert failed["payload"]["retryable"] is True
    recovery = bridge.status()["recovery"]
    assert recovery["state"] == "deferred"
    assert recovery["reason"] == "transport_gate_closed"
    assert recovery["deferred"] == 1


def test_a_deferred_run_is_retried_when_the_next_window_opens(
    tmp_path: Path,
) -> None:
    """⟦c4⟧ Deferred is not abandoned: the window opening is the trigger.

    The retry is a new attempt on the same run, submitted to the bridge's own
    loop, so the operator's message is answered rather than silently dropped.
    A duplicate reply is impossible for a different reason: the delivery
    ledger is idempotent on the delivery key, and the interrupted attempt
    never produced one.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _StalledBackend()
    calls: list[int] = []

    def factory():
        calls.append(1)
        if len(calls) == 2:
            raise ManagedWorkerUnavailable("transport_gate_closed")
        return RunOrchestrator(
            store, HermesAdapter(backend_loader=lambda: backend), Releases()
        )

    bridge = _bridge_with_factory(store, factory)
    run_id = _stranded_running_run(store, bridge, backend, thread_id)
    try:
        bridge.recover()
        assert store.get_run(run_id)["state"] == "failed"
        assert bridge.status()["recovery"]["state"] == "deferred"
        # The window opens: the factory answers, so recovery runs for real.
        bridge.recover(attempt=2)
    finally:
        backend.release.set()

    assert store.get_run(run_id)["state"] == "retrying", store.get_run(run_id)
    recovery = bridge.status()["recovery"]
    assert recovery["state"] == "converged"
    assert recovery["deferred"] == 0
    assert recovery["resumed"] == 1
    # Submitted, so the loop dispatches the new attempt rather than waiting for
    # another inbound message.
    assert thread_id in bridge._queued  # noqa: SLF001


def test_a_deferred_run_stays_typed_when_the_release_is_not_usable(
    tmp_path: Path,
) -> None:
    """A refusal that is not the gate leaves the run ended, never re-attempted.

    "End it typed if the release is no longer active or approved" is already
    true after the deferral -- the run is `failed` with a category. What must
    not happen is a retry into a runtime that cannot serve it.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _StalledBackend()
    calls: list[int] = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            return RunOrchestrator(
                store, HermesAdapter(backend_loader=lambda: backend), Releases()
            )
        if len(calls) == 2:
            raise ManagedWorkerUnavailable("transport_gate_closed")
        raise ManagedWorkerUnavailable("release_not_approved")

    bridge = _bridge_with_factory(store, factory)
    run_id = _stranded_running_run(store, bridge, backend, thread_id)
    try:
        bridge.recover()
        bridge.recover(attempt=2)
    finally:
        backend.release.set()

    assert store.get_run(run_id)["state"] == "failed"
    recovery = bridge.status()["recovery"]
    assert recovery["state"] == "refused"
    assert recovery["reason"] == "release_not_approved"
    assert recovery["deferred"] == 1


def test_a_run_parked_on_a_decision_is_not_a_recovery_candidate(
    tmp_path: Path,
) -> None:
    """A parked run is waiting for the operator, not for a recovery.

    `list_recoverable_runs` excluded only `completed|failed|canceled|paused`,
    so a run legitimately parked on a decision was a recovery candidate at the
    next daemon start -- and recovery would have converged away the very
    decision the operator was being asked to make.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=30.0)

    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_DECISION_REQUIRED
    assert store.get_run(str(outcome.run_id))["state"] == "waiting_for_decision"
    assert [run["id"] for run in store.list_recoverable_runs()] == []


# -- ⟦F-B5⟧ a turn that hits the deadline ends typed --------------------------


class NeverFinishes(FakeHermesBackend):
    """A worker that neither answers nor parks: the case the ceiling exists for."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, request, emit):  # type: ignore[override]
        self.entered.set()
        # Bounded: `asyncio.run` waits for its default executor at loop
        # shutdown, so this thread's exit is on the test's critical path.
        self.release.wait(3)
        return HermesRunResult(request.session_ref, final_response=None, canceled=True)


def test_a_turn_that_hits_the_deadline_ends_typed_and_frees_its_scope(
    tmp_path: Path,
) -> None:
    """⟦F-B5⟧ `task.cancel()` raises `CancelledError`, a `BaseException`.

    `RunOrchestrator.dispatch`'s `except Exception` never sees it, so no
    terminal event was committed: the attempt stayed bound, the run stayed
    `running`, and every later message to that Telegram scope answered
    `turn_in_flight` for the life of the daemon. The bridge is the only place
    that knows the deadline was what ended it, so the bridge writes it.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = NeverFinishes()
    bridge = _bridge(store, backend, turn_timeout=1.0)

    try:
        outcome = _drain_one(bridge, thread_id)
    finally:
        backend.release.set()

    assert outcome.outcome == OUTCOME_FAILED, outcome
    assert outcome.reason == TURN_TIMEOUT
    assert outcome.detail == "no terminal event within 1s"
    run = store.get_run(str(outcome.run_id))
    assert run["state"] == "failed"
    failure = next(
        event
        for event in store.list_run_events(str(outcome.run_id))
        if event["type"] == "run.failed"
    )
    assert failure["payload"]["category"] == TURN_TIMEOUT
    assert failure["payload"]["retryable"] is True
    # The scope is free: the operator's next message starts a fresh run rather
    # than being answered `turn_in_flight` for ever.
    assert store.get_thread(thread_id)["active_run_id"] is None


def test_a_shutdown_mid_turn_is_not_recorded_as_a_timeout(tmp_path: Path) -> None:
    """⟦F-B5⟧ The shutdown break must not borrow the deadline's vocabulary.

    `stop()` is followed by the caller releasing the worker, which ends the
    attempt through the runtime's own uncertain-outcome path; c4's
    `recovery_deferred` is what the restart records. Calling that a turn
    timeout would put a category on the durable event that names the wrong
    cause.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = NeverFinishes()
    bridge = _bridge(store, backend, turn_timeout=600.0)

    stopper = threading.Thread(
        target=lambda: (backend.entered.wait(20), bridge._stop.set())  # noqa: SLF001
    )
    stopper.start()
    try:
        outcome = _drain_one(bridge, thread_id)
    finally:
        backend.release.set()
        stopper.join(timeout=20)

    assert outcome.reason != TURN_TIMEOUT
    categories = [
        event["payload"].get("category")
        for event in store.list_run_events(str(outcome.run_id))
        if event["type"].startswith("run.")
    ]
    assert TURN_TIMEOUT not in categories


def test_a_failed_turn_always_names_a_category(tmp_path: Path) -> None:
    """⟦F-B5⟧ `failed / None / None` reads as "the product has nothing to say"."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend())
    run = store.create_run(
        thread_id=thread_id,
        expected_revision=store.get_thread(thread_id)["revision"],
        actor_id="operator",
        idempotency_key="bridge-bare-run-000001",
    ).value

    outcome = bridge._classify(thread_id, str(run["id"]), parked=False)  # noqa: SLF001

    assert outcome.outcome == OUTCOME_FAILED
    assert outcome.reason is not None
    assert outcome.detail is not None


# ⟦P8⟧ ------------------------------------ runs created elsewhere, driven here


def _api_created_run(store: ControlStore, thread_id: str, *, index: int = 1) -> str:
    """What `POST /api/v1/threads/{id}/runs` leaves: a `queued` run, nobody driving."""

    return str(
        store.create_run(
            thread_id=thread_id,
            expected_revision=int(store.get_thread(thread_id)["revision"]),
            actor_id="local-operator",
            idempotency_key=f"api-run-command-{index:06d}",
        ).value["id"]
    )


def _run_event(store: ControlStore, run_id: str, event_type: str) -> dict:
    return next(
        event for event in store.list_run_events(run_id) if event["type"] == event_type
    )


def _delivery_rows(tmp_path: Path) -> int:
    import sqlite3

    with sqlite3.connect(tmp_path / "control.db") as conn:
        return int(conn.execute("SELECT COUNT(*) FROM transport_deliveries").fetchone()[0])


class _AnsweringBackend(FakeHermesBackend):
    """A turn that answers without asking for a decision.

    The default fake parks in an approval callback and `_resolve_when_asked`
    stands in for the operator -- a race with the orchestrator's own event
    application that the ⟦c1⟧ test above tolerates. What this section proves
    is where the answer lands, so the turn simply answers.
    """

    def run(self, request, emit):
        self.run_calls += 1
        self.active.set()
        return HermesRunResult(request.session_ref, "Hello", canceled=False)


def test_a_run_created_through_the_api_is_the_run_the_bridge_drives(
    tmp_path: Path,
) -> None:
    """⟦P8⟧ Deliverables 1 and 2 on a thread with NO transport binding.

    The thread was never bound to a Telegram scope; the run exists before the
    bridge is told. `_run_for` returns that run rather than creating a second,
    the same orchestrator executes it, and the reply is an assistant message
    IN THE THREAD -- `append_runtime_message` writes it on
    `runtime.message.completed` -- readable by `GET /threads/{id}/messages`
    with no transport involved and no delivery-ledger row for a thread that
    has nowhere to deliver to.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge = _bridge(store, _AnsweringBackend())

    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_ANSWERED, outcome
    assert outcome.run_id == run_id
    assert store.get_run(run_id)["state"] == "completed"
    assert len(store.list_thread_runs(thread_id=thread_id, after_id=None, limit=10)) == 1
    messages = store.list_messages(thread_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "Hello"
    assert store.get_thread(thread_id)["active_run_id"] is None
    assert _delivery_rows(tmp_path) == 0


def test_a_run_created_under_a_closed_gate_ends_typed_rather_than_queued(
    tmp_path: Path,
) -> None:
    """⟦P8⟧ "Closed gate → typed", also for a run that already exists.

    The Telegram path never creates a run under a closed gate (the test above
    this section proves it); the control API can, and used to leave it
    `queued` for ever. The bridge's refusal now ends it with the gate's own
    word, retryable, and names the run it ended.
    """

    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge = _bridge(store, FakeHermesBackend())

    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == REFUSED_DISPATCH_DISABLED
    assert outcome.run_id == run_id
    assert store.get_run(run_id)["state"] == "failed"
    failed = _run_event(store, run_id, "run.failed")
    assert failed["payload"]["category"] == REFUSED_DISPATCH_DISABLED
    assert failed["payload"]["retryable"] is True
    assert store.get_thread(thread_id)["active_run_id"] is None


def test_the_start_sweep_hands_a_queued_run_to_the_loop_once(tmp_path: Path) -> None:
    """⟦P8⟧ A run a previous daemon left `queued` is driven, not found for ever."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge = _bridge(store, FakeHermesBackend())

    bridge.recover()
    bridge.recover(attempt=2)

    status = bridge.status()
    assert status["undriven"] == {"submitted": 1, "resubmitted": 0, "canceled": 0, "stalled": 0, "error": None}
    assert status["queued"] == 1
    # ⟦V-1⟧ A handover names the run it hands over; the turn never creates one.
    assert bridge._queue.get_nowait() == (thread_id, run_id)  # noqa: SLF001


def test_the_start_sweep_converges_a_cancellation_nothing_reserved(
    tmp_path: Path,
) -> None:
    """⟦P8⟧ Deliverable 3 at restart: `cancel_requested` with nothing to cancel.

    `RunOrchestrator._converge_unbound_cancellation` needs the dispatch owner
    of a live dispatch; a run cancelled before any dispatch reserved it has
    none, and `recover_startup` converges that shape only with a worker (and
    so only inside a window). The same store transition, the same word.
    """

    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    store.transition_run(
        run_id=run_id,
        target_state="cancel_requested",
        expected_revision=int(store.get_run(run_id)["revision"]),
        actor_id="local-operator",
        idempotency_key="api-cancel-command-0001",
    )
    bridge = _bridge(store, FakeHermesBackend())

    bridge.recover()

    assert store.get_run(run_id)["state"] == "canceled"
    canceled = _run_event(store, run_id, "run.canceled")
    assert canceled["payload"]["category"] == CANCELED_BEFORE_BINDING
    assert canceled["payload"]["retryable"] is False
    assert store.get_thread(thread_id)["active_run_id"] is None
    assert bridge.status()["undriven"] == {"submitted": 0, "resubmitted": 0, "canceled": 1, "stalled": 0, "error": None}
    assert bridge.status()["queued"] == 0


def test_the_sweep_leaves_a_reserved_or_driven_run_alone(tmp_path: Path) -> None:
    """A dispatch owner, or this loop's own `_driving`, is somebody driving."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    reserved_thread = _thread_with_message(store, index=1)
    reserved_run = _api_created_run(store, reserved_thread, index=1)
    run = store.get_run(reserved_run)
    store.reserve_attempt_dispatch(
        run_id=reserved_run,
        attempt_id=str(run["active_attempt_id"]),
        dispatch_owner="another-daemon:0001",
        runtime_release_id=FAKE_RUNTIME_IDENTITY.release_id,
        state_generation_id=FAKE_RUNTIME_IDENTITY.state_generation_id,
        runtime_slot_id=FAKE_RUNTIME_IDENTITY.slot_id,
        runtime_artifact_digest=FAKE_RUNTIME_IDENTITY.artifact_digest,
        runtime_worker_protocol=FAKE_RUNTIME_IDENTITY.worker_protocol,
        expected_revision=int(run["revision"]),
        actor_id="another-daemon",
        idempotency_key="reserve-command-000001",
    )
    driven_thread = _thread_with_message(store, index=2)
    driven_run = _api_created_run(store, driven_thread, index=2)
    bridge = _bridge(store, FakeHermesBackend())
    bridge._driving.add(driven_run)  # noqa: SLF001

    bridge.recover()

    assert bridge.status()["undriven"] == {"submitted": 0, "resubmitted": 0, "canceled": 0, "stalled": 0, "error": None}
    assert bridge.status()["queued"] == 0
    assert store.get_run(reserved_run)["state"] == "queued"
    assert store.get_run(driven_run)["state"] == "queued"


class _RecordingWorker:
    """The seam itself: which form of `backend` each path asks for."""

    def __init__(self) -> None:
        self.asked: list[bool] = []

    def backend(self, *, window_required: bool):
        self.asked.append(window_required)
        raise ManagedWorkerUnavailable("transport_gate_closed")


def test_a_turn_acquires_the_worker_without_a_window_and_recovery_with_one(
    tmp_path: Path,
) -> None:
    """⟦P8⟧ The two factories: no window for a turn, the c4 gate for recovery."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    worker = _RecordingWorker()
    bridge = InboundTurnBridge(
        store=store,
        worker=worker,  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
    )

    assert bridge.recover() == []
    outcome = _drain_one(bridge, thread_id)

    assert worker.asked == [True, False]
    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == "transport_gate_closed"


# ⟦Batch G⟧ -------------------------------------- runs this bridge may drive


def _carrier_run(store: ControlStore, capture_id: str = "cap_000001") -> dict:
    """The capture consumer's carrier, built the way `_carrier` builds it.

    A machine workspace, a `capture <id>` thread with no user message, a run
    created by MACHINE_ACTOR whose attempt is never reserved and never bound,
    the RESEARCH_CAPTURE_WORKFLOW installed on it, its stage activated and one
    pending effect -- the shape `_uncertain` leaves behind by design.
    """

    import hashlib

    from cortex_platform.product.engine.capture_consumer import (
        MACHINE_ACTOR,
        RESEARCH_CAPTURE_WORKFLOW,
        STAGE_KEY,
    )
    from cortex_platform.product.workflows.models import SourceImportRequest

    workspace = store.create_workspace(
        title="Capture consumer",
        actor_id=MACHINE_ACTOR,
        idempotency_key="carrier-workspace-000001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title=f"capture {capture_id}",
        expected_revision=workspace["revision"],
        actor_id=MACHINE_ACTOR,
        idempotency_key=f"carrier-thread-{capture_id}",
    ).value
    run = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key=f"carrier-run-{capture_id}-0",
    ).value
    workflow = store.install_workflow(
        run_id=str(run["id"]), definition=RESEARCH_CAPTURE_WORKFLOW
    )
    stage = next(item for item in workflow["stages"] if item["stage_key"] == STAGE_KEY)
    activated = store.activate_workflow_stage(
        workflow_id=str(workflow["id"]),
        stage_key=STAGE_KEY,
        input_value={"capture_id": capture_id},
        expected_workflow_revision=int(workflow["revision"]),
        expected_stage_revision=int(stage["revision"]),
    )
    workflow, stage = activated["workflow"], activated["stage"]
    digest = hashlib.sha256(capture_id.encode()).hexdigest()
    effect = store.create_workflow_effect(
        workflow_id=str(workflow["id"]),
        stage_key=STAGE_KEY,
        effect_key=f"capture:{capture_id}",
        request=SourceImportRequest(
            operation_id=f"capture.import.{capture_id}",
            delivery_epoch=1,
            source_id=f"capture-{capture_id}",
            canonical_id=f"sha256:{digest}",
        ),
        expected_workflow_revision=int(workflow["revision"]),
        expected_stage_revision=int(stage["revision"]),
        expected_stage_input_hash=str(stage["input_hash"]),
    )
    return {"thread_id": str(thread["id"]), "run_id": str(run["id"]), "effect": effect}


class _UnavailableWorker:
    """A worker that refuses every backend, and counts the asks."""

    def __init__(self, reason: str = "release_not_approved") -> None:
        self.reason = reason
        self.asked: list[bool] = []

    def backend(self, *, window_required: bool):
        self.asked.append(window_required)
        raise ManagedWorkerUnavailable(self.reason)


def test_the_engine_carrier_run_is_not_a_run_this_bridge_may_drive_gate_open(
    tmp_path: Path,
) -> None:
    """⟦P8-01⟧ With dispatch enabled a restart must not start a turn on it.

    The carrier owns a workflow instance; that ownership is the predicate. No
    submit, no backend asked for with the turn's form, no launch.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    carrier = _carrier_run(store)
    worker = _UnavailableWorker("transport_gate_closed")
    bridge = InboundTurnBridge(
        store=store,
        worker=worker,  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
    )

    bridge.recover()
    bridge.recover(attempt=2)

    assert bridge.status()["undriven"] == {"submitted": 0, "resubmitted": 0, "canceled": 0, "stalled": 0, "error": None}
    assert bridge.status()["queued"] == 0
    # Recovery's form only (the c4 gate), never the turn's.
    assert False not in worker.asked
    assert store.get_run(carrier["run_id"])["state"] == "queued"


def test_the_engine_carrier_run_is_left_alone_under_a_closed_gate(
    tmp_path: Path,
) -> None:
    """⟦P8-02⟧ With dispatch disabled a restart must not end it either.

    A terminal run fences its workflow (`_expect_workflow_run_open`), which
    would strand the capture: the effect could never be claimed again.
    """

    store = _store(tmp_path)
    carrier = _carrier_run(store)
    bridge = InboundTurnBridge(
        store=store,
        worker=_UnavailableWorker("transport_gate_closed"),  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
    )

    bridge.recover()
    # And a submit that reaches the thread anyway refuses without writing.
    outcome = _drain_one(bridge, carrier["thread_id"])

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.run_id is None
    assert store.get_run(carrier["run_id"])["state"] == "queued"
    assert bridge.status()["undriven"] == {"submitted": 0, "resubmitted": 0, "canceled": 0, "stalled": 0, "error": None}
    claimed = store.claim_workflow_effect(
        effect_id=str(carrier["effect"]["id"]),
        worker_id="p4-capture-consumer",
        lease_seconds=600,
    )
    assert claimed["state"] == "claimed"


def test_a_cancelled_engine_carrier_is_not_converged_by_the_sweep(
    tmp_path: Path,
) -> None:
    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    store = _store(tmp_path)
    carrier = _carrier_run(store)
    run = store.get_run(carrier["run_id"])
    # ⟦ADJ-4⟧ Only the engine can request this now (`_close_run`'s first
    # transition; a crash before its `fail_unbound_run` leaves this shape).
    store.transition_run(
        run_id=carrier["run_id"],
        target_state="cancel_requested",
        expected_revision=int(run["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key="carrier-cancel-command-01",
    )
    # No orchestrator: `recover_startup` converges a cancelled unbound run on
    # its own (pre-existing, and the cancel already fenced the workflow); what
    # is isolated here is the sweep, which must not touch the engine's run.
    bridge = InboundTurnBridge(
        store=store,
        worker=_UnavailableWorker("transport_gate_closed"),  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
    )

    bridge.recover()

    assert store.get_run(carrier["run_id"])["state"] == "cancel_requested"
    assert bridge.status()["undriven"]["canceled"] == 0


def test_a_closed_gate_writes_nothing_on_a_transport_bound_thread(
    tmp_path: Path,
) -> None:
    """⟦P8-05⟧ `run.failed` is a notification; a bound thread would receive it.

    `runtime disable-dispatch` (cutover 5a) promises nothing more is sent, so
    the run is named on the refusal and left `queued` for the next submit
    after the gate opens.
    """

    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    store.bind_transport(
        transport="telegram",
        external_scope="scope-000001",
        thread_id=thread_id,
        actor_id="cortexd",
        idempotency_key="bind-transport-000001",
    )
    run_id = _api_created_run(store, thread_id)
    before = [event["type"] for event in store.list_events(after_cursor=0, limit=50)]
    bridge = _bridge(store, FakeHermesBackend())

    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == REFUSED_DISPATCH_DISABLED
    assert outcome.run_id == run_id
    assert store.get_run(run_id)["state"] == "queued"
    after = [event["type"] for event in store.list_events(after_cursor=0, limit=50)]
    assert after == before
    assert "run.failed" not in after


def test_an_expired_dispatch_lease_is_nobody_driving(tmp_path: Path) -> None:
    """⟦P8-3⟧ A daemon that died between reserve and bind left a dead lease."""

    from datetime import UTC, datetime

    # The store's clock is in the past, so a one-second lease it writes is
    # already expired against the bridge's own clock.
    store = ControlStore(
        tmp_path / "control.db", clock=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    )
    store.initialize()
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    run = store.get_run(run_id)
    reserved = store.reserve_attempt_dispatch(
        run_id=run_id,
        attempt_id=str(run["active_attempt_id"]),
        dispatch_owner="dead-daemon:0001",
        runtime_release_id=FAKE_RUNTIME_IDENTITY.release_id,
        state_generation_id=FAKE_RUNTIME_IDENTITY.state_generation_id,
        runtime_slot_id=FAKE_RUNTIME_IDENTITY.slot_id,
        runtime_artifact_digest=FAKE_RUNTIME_IDENTITY.artifact_digest,
        runtime_worker_protocol=FAKE_RUNTIME_IDENTITY.worker_protocol,
        expected_revision=int(run["revision"]),
        actor_id="dead-daemon",
        idempotency_key="reserve-command-000001",
        lease_seconds=1,
    ).value
    store.transition_run(
        run_id=run_id,
        target_state="cancel_requested",
        expected_revision=int(reserved["revision"]),
        actor_id="local-operator",
        idempotency_key="api-cancel-command-0001",
    )
    bridge = _bridge(store, FakeHermesBackend())

    bridge.recover()

    assert store.get_run(run_id)["state"] == "canceled"
    assert _run_event(store, run_id, "run.canceled")["payload"]["category"] == (
        CANCELED_BEFORE_BINDING
    )
    assert bridge.status()["undriven"]["canceled"] == 1


def test_a_run_the_worker_refused_is_driven_again_by_the_loop(
    tmp_path: Path,
) -> None:
    """⟦P8-1/P8-2⟧ A refusal is a decision the operator lifts; the loop re-asks.

    The guard is per run and dropped when the worker (not the gate) refused,
    and the loop re-drives the runs it refused at the recovery cadence
    whatever recovery's own state is -- so `cortex runtime approve` re-drives
    the run without a restart. No store-wide sweep is sampled for that.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge = InboundTurnBridge(
        store=store,
        worker=_UnavailableWorker("release_not_approved"),  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
    )
    bridge.RECOVERY_RETRY_SECONDS = 0.05  # type: ignore[misc]

    bridge.recover()
    bridge.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if bridge.status()["reasons"].get("release_not_approved", 0) >= 3:
                break
            time.sleep(0.02)
    finally:
        bridge.stop(timeout=5.0)

    status = bridge.status()
    assert status["reasons"]["release_not_approved"] >= 3, status
    # ⟦G-NEW-4⟧ One distinct run, re-driven: `submitted` stays readable.
    assert status["undriven"]["submitted"] == 1
    assert status["undriven"]["resubmitted"] >= 2
    assert store.get_run(run_id)["state"] == "queued"


def test_the_sweep_never_records_a_follow_up(tmp_path: Path) -> None:
    """A thread already queued is the run about to be driven, not a second turn."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    _api_created_run(store, thread_id)
    bridge = _bridge(store, FakeHermesBackend())

    bridge.recover()
    bridge.recover(attempt=2)
    bridge._sweep_undriven()  # noqa: SLF001 - the loop's own cadence

    assert bridge.status()["undriven"]["submitted"] == 1
    assert bridge._followups == set()  # noqa: SLF001
    assert bridge._queue.qsize() == 1  # noqa: SLF001


def test_a_store_that_cannot_be_asked_is_reported_not_counted_as_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """⟦P8-7⟧ "could not look" is not `{submitted: 0, canceled: 0}`."""

    from cortex_platform.product.control import NotFound

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    _api_created_run(store, thread_id)
    bridge = _bridge(store, FakeHermesBackend())
    real = store.list_recoverable_runs
    monkeypatch.setattr(
        store,
        "list_recoverable_runs",
        lambda **kwargs: (_ for _ in ()).throw(NotFound("runs", "locked")),
    )

    bridge.recover()
    assert bridge.status()["undriven"]["error"]
    assert bridge.status()["undriven"]["submitted"] == 0

    monkeypatch.setattr(store, "list_recoverable_runs", real)
    bridge.recover(attempt=2)
    assert bridge.status()["undriven"] == {"submitted": 1, "resubmitted": 0, "canceled": 0, "stalled": 0, "error": None}


# ⟦Fix-verification round⟧ ------------------------------------------------


def _control_api(store: ControlStore, bridge: InboundTurnBridge):
    from cortex_platform.product.api import ControlAPI

    class _BoundWorker:
        bound = True

        def health(self):
            payload = {
                "state": "bound",
                "reason": None,
                "release_id": "hermes-0.15.0-test",
                "slot_digest": "b" * 64,
                "launched": False,
            }
            return type("_Health", (), {"to_dict": lambda self: payload})()

    return ControlAPI(
        store, access_token="x" * 48, managed_worker=_BoundWorker(), turn_bridge=bridge
    )


def _post_message(api, thread_id: str, content: str, *, key: str):
    import json

    store_thread = api.store.get_thread(thread_id)
    return api.handle(
        method="POST",
        target=f"/api/v1/threads/{thread_id}/messages",
        headers={"X-Cortex-Control-Token": "x" * 48, "Idempotency-Key": key},
        body=json.dumps(
            {"role": "user", "content": content, "expected_revision": int(store_thread["revision"])}
        ).encode(),
    )


def test_a_message_on_a_parked_run_keeps_the_decision_answerable(
    tmp_path: Path,
) -> None:
    """⟦G-R1⟧ Real store, real bridge, real API: typing is not answering.

    The turn parks on a decision the cockpit and `POST /decisions/{id}/resolve`
    can answer. A user message posted to that thread is stored and NOT handed
    to the bridge -- which would abandon the run (F-B6, written for a
    transport with no approval vocabulary) and expire the decision.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    bridge = _bridge(store, FakeHermesBackend(), turn_timeout=30.0)
    api = _control_api(store, bridge)
    parked = _drain_one(bridge, thread_id)
    assert parked.outcome == OUTCOME_DECISION_REQUIRED
    run_id = str(parked.run_id)
    pending = [decision["id"] for decision in store.list_decisions(state="pending")]
    assert pending

    appended = _post_message(api, thread_id, "why do you need that?", key="message-command-00009")

    assert appended.status == 201
    assert bridge.status()["queued"] == 0
    assert bridge._queue.qsize() == 0  # noqa: SLF001
    assert store.get_run(run_id)["state"] == "waiting_for_decision"
    assert [d["id"] for d in store.list_decisions(state="pending")] == pending
    assert bridge.status()["abandoned"] == 0
    assert [m["role"] for m in store.list_messages(thread_id)][-1] == "user"


def test_a_message_on_a_carrier_thread_never_drives_the_carrier(
    tmp_path: Path,
) -> None:
    """⟦G-NEW-1⟧ Carrier threads are ordinary threads to the cockpit.

    A user message posted to `capture cap_*` must neither be handed to the
    bridge by the API nor, reaching the bridge by any other entry, drive the
    carrier: `run_is_conversation` is asked on the drive path too.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    carrier = _carrier_run(store)
    bridge = _bridge(store, _AnsweringBackend())
    api = _control_api(store, bridge)

    appended = _post_message(api, carrier["thread_id"], "hello?", key="message-command-00010")
    assert appended.status == 201
    assert bridge._queue.qsize() == 0  # noqa: SLF001

    outcome = _drain_one(bridge, carrier["thread_id"])

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == REFUSED_NOT_CONVERSATION
    assert store.get_run(carrier["run_id"])["state"] == "queued"
    events = [event["type"] for event in store.list_run_events(carrier["run_id"])]
    assert "run.failed" not in events and "run.completed" not in events
    claimed = store.claim_workflow_effect(
        effect_id=str(carrier["effect"]["id"]),
        worker_id="p4-capture-consumer",
        lease_seconds=600,
    )
    assert claimed["state"] == "claimed"


def test_a_carrier_owns_its_workflow_from_the_transaction_that_creates_it(
    tmp_path: Path,
) -> None:
    """⟦V-3⟧ The exclusion is durable from the instant the run exists.

    Built through the consumer's own `_carrier`, on a `capture` thread that
    HOLDS an operator message (the API stores one and declines to submit it,
    so a carrier thread may well hold one). Before this, `create_run` and
    `install_workflow` were two transactions, and in the gap between them
    the carrier passed both halves of the predicate: the sweep handed it
    over and asked for a backend with the turn's form on a machine thread.
    Now `create_run(workflow=...)` installs the workflow in the transaction
    that creates the run: observed the instant `create_run` returns, the
    run already owns its workflow and is not a conversation run.
    """

    from cortex_platform.product.engine.capture_consumer import CaptureConsumer

    store = _store(tmp_path)
    consumer = CaptureConsumer(store=store, engine=None)  # type: ignore[arg-type]
    capture_id = "cap_v3_000001"
    thread = consumer._thread(capture_id)  # noqa: SLF001 - the producer's own shape
    store.append_message(
        thread_id=str(thread["id"]),
        role="user",
        content="hello?",
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-on-carrier-000001",
    )
    observed: list[tuple[bool, bool]] = []
    real_create_run = store.create_run

    def probing_create_run(**kwargs):
        result = real_create_run(**kwargs)
        run_id = str(result.value["id"])
        try:
            store.get_workflow_for_run(run_id)
            owned = True
        except Exception:  # noqa: BLE001 - the observation is the point
            owned = False
        observed.append((store.run_is_conversation(run_id), owned))
        return result

    store.create_run = probing_create_run  # type: ignore[method-assign]
    try:
        workflow, _stage = consumer._carrier(capture_id)  # noqa: SLF001
    finally:
        store.create_run = real_create_run  # type: ignore[method-assign]

    # The instant `create_run` returned: already a workflow's run, never a
    # conversation run -- on a thread with an operator message.
    assert observed == [(False, True)]
    run_id = str(workflow["run_id"])
    assert store.get_thread(str(thread["id"]))["active_run_id"] == run_id
    assert store.run_is_conversation(run_id) is False
    assert store.list_recoverable_runs(conversations_only=True) == []
    for gate_open in (False, True):
        if gate_open:
            _enable_dispatch(store)
        worker = _UnavailableWorker("transport_gate_closed")
        bridge = InboundTurnBridge(
            store=store,
            worker=worker,  # type: ignore[arg-type]
            actor_id="cortexd-turn-test",
        )

        bridge.recover()
        outcome = _drain_one(bridge, str(thread["id"]))

        assert bridge.status()["undriven"]["submitted"] == 0
        assert bridge.status()["queued"] == 0
        assert False not in worker.asked
        assert outcome.outcome == OUTCOME_REFUSED
        # The gate answers first while it is shut; open, the predicate does.
        assert outcome.reason == (
            REFUSED_NOT_CONVERSATION if gate_open else REFUSED_DISPATCH_DISABLED
        )
        assert store.get_run(run_id)["state"] == "queued"


def test_the_loop_does_not_sample_the_store_for_the_life_of_the_daemon(
    tmp_path: Path, monkeypatch
) -> None:
    """⟦G-R3/G-R4⟧ The sweep runs at start and on recovery retries only."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    _api_created_run(store, thread_id)
    bridge = _bridge(store, FakeHermesBackend())
    calls: list[int] = []
    real = store.list_recoverable_runs
    monkeypatch.setattr(
        store,
        "list_recoverable_runs",
        lambda **kwargs: (calls.append(1), real(**kwargs))[1],
    )
    bridge.RECOVERY_RETRY_SECONDS = 0.02  # type: ignore[misc]

    bridge.recover()
    swept_at_start = len(calls)
    assert bridge.status()["recovery"]["state"] == "converged"
    bridge._queue.get_nowait()  # noqa: SLF001 - the loop must find nothing to drive
    with bridge._lock:  # noqa: SLF001
        bridge._queued.clear()
    bridge.start()
    try:
        time.sleep(0.3)
    finally:
        bridge.stop(timeout=5.0)

    assert len(calls) == swept_at_start


# -- ⟦V-1⟧ the re-drive contract --------------------------------------------


def _switchable_bridge(
    store: ControlStore, backend: FakeHermesBackend
) -> tuple[InboundTurnBridge, "callable"]:
    """A bridge whose worker refuses `release_not_approved` until approved.

    The `cortex runtime approve` shape: the refusal is a decision re-read at
    every launch, and lifting it is what the re-drive exists for.
    """

    refusing = {"reason": "release_not_approved"}

    def factory():
        if refusing["reason"]:
            raise ManagedWorkerUnavailable(refusing["reason"])
        return RunOrchestrator(
            store, HermesAdapter(backend_loader=lambda: backend), Releases()
        )

    bridge = InboundTurnBridge(
        store=store,
        worker=None,  # type: ignore[arg-type] - the factory replaces it
        actor_id="cortexd-turn-test",
        orchestrator_factory=factory,
    )
    bridge.RECOVERY_RETRY_SECONDS = 0.01  # type: ignore[misc]

    def approve() -> None:
        refusing["reason"] = None

    return bridge, approve


def _wait_until(predicate, *, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def _assistant_messages(store: ControlStore, thread_id: str) -> list[dict]:
    return [
        message
        for message in store.list_messages(thread_id=thread_id)
        if message["role"] == "assistant"
    ]


def _cancel_through_api(api, run_id: str) -> object:
    import json

    run = api.store.get_run(run_id)
    return api.handle(
        method="POST",
        target=f"/api/v1/runs/{run_id}/cancel",
        headers={"X-Cortex-Control-Token": "x" * 48, "Idempotency-Key": "cancel-command-00001"},
        body=json.dumps({"expected_revision": int(run["revision"])}).encode(),
    )


def test_an_operator_cancel_ends_the_redrive_and_no_run_is_ever_created_from_it(
    tmp_path: Path,
) -> None:
    """⟦V-1⟧ The mini's live sequence, with an answering worker at the end.

    `release_not_approved` leaves the run in the re-drive; the operator
    cancels it through the API; the release is approved. Before this the
    entry outlived the cancel, the idle THREAD was handed over every tick,
    and `_run_for` created -- and the worker answered -- a fresh run per
    tick for the life of the daemon. Now: the same run count, no assistant
    message, and nothing left to re-drive.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge, approve = _switchable_bridge(store, _AnsweringBackend())
    api = _control_api(store, bridge)

    bridge.recover()
    bridge.start()
    try:
        _wait_until(
            lambda: bridge.status()["reasons"].get("release_not_approved", 0) >= 2
        )
        with bridge._lock:  # noqa: SLF001
            assert run_id in bridge._redrive  # noqa: SLF001
        canceled = _cancel_through_api(api, run_id)
        assert canceled.status == 200, canceled.payload
        assert canceled.payload["state"] == "canceled"
        approve()
        time.sleep(0.8)  # >= 60 re-drive ticks at the test cadence
    finally:
        bridge.stop(timeout=5.0)

    assert [run["id"] for run in store.list_thread_runs(thread_id=thread_id)] == [run_id]
    assert store.get_run(run_id)["state"] == "canceled"
    assert _assistant_messages(store, thread_id) == []
    status = bridge.status()
    assert status["outcomes"].get("answered", 0) == 0
    with bridge._lock:  # noqa: SLF001
        assert bridge._redrive == {}  # noqa: SLF001


def test_the_redrive_tick_drops_a_run_the_store_says_moved_on(tmp_path: Path) -> None:
    """⟦V-1⟧ The tick itself re-reads the run, independent of `forget`.

    A cancel that reached the store by any route -- here without the API --
    is found at the next tick: the entry is dropped, nothing is queued, and
    a handover that does reach the turn is refused rather than creating.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge = _bridge(store, _AnsweringBackend())
    with bridge._lock:  # noqa: SLF001
        bridge._redrive[run_id] = thread_id  # noqa: SLF001 - what a refused turn leaves
    run = store.get_run(run_id)
    store.transition_run(
        run_id=run_id,
        target_state="cancel_requested",
        expected_revision=int(run["revision"]),
        actor_id="local-operator",
        idempotency_key="cancel-command-000001",
    )
    run = store.get_run(run_id)
    store.fail_unbound_run(
        run_id=run_id,
        attempt_id=str(run["active_attempt_id"]),
        expected_revision=int(run["revision"]),
        category=CANCELED_BEFORE_BINDING,
        actor_id="local-operator",
        idempotency_key="cancel-converge-000001",
    )

    for _ in range(60):
        bridge._redrive_refused()  # noqa: SLF001

    with bridge._lock:  # noqa: SLF001
        assert bridge._redrive == {}  # noqa: SLF001
    assert bridge._queue.qsize() == 0  # noqa: SLF001
    assert bridge.status()["undriven"]["resubmitted"] == 0

    outcome = bridge._run_turn(thread_id, expected_run=run_id)  # noqa: SLF001

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == REFUSED_RUN_NOT_ACTIVE
    assert [run["id"] for run in store.list_thread_runs(thread_id=thread_id)] == [run_id]
    assert _assistant_messages(store, thread_id) == []


def test_the_redrive_answers_the_same_run_exactly_once_after_approval(
    tmp_path: Path,
) -> None:
    """⟦V-1 / P8-2⟧ The non-cancel path: the SAME run id, one answer, no restart."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    run_id = _api_created_run(store, thread_id)
    bridge, approve = _switchable_bridge(store, _AnsweringBackend())

    bridge.recover()
    bridge.start()
    try:
        _wait_until(
            lambda: bridge.status()["reasons"].get("release_not_approved", 0) >= 2
        )
        approve()
        _wait_until(lambda: store.get_run(run_id)["state"] == "completed")
        time.sleep(0.3)  # more ticks: nothing else may be driven
    finally:
        bridge.stop(timeout=5.0)

    assert [run["id"] for run in store.list_thread_runs(thread_id=thread_id)] == [run_id]
    assert len(_assistant_messages(store, thread_id)) == 1
    status = bridge.status()
    assert status["outcomes"]["answered"] == 1
    assert status["undriven"]["submitted"] == 1
    with bridge._lock:  # noqa: SLF001
        assert bridge._redrive == {}  # noqa: SLF001


def test_the_loop_sweeps_once_when_the_dispatch_gate_opens(
    tmp_path: Path, monkeypatch
) -> None:
    """⟦V-5⟧ A run left standing under a closed gate is driven when it opens.

    On a transport-bound thread the closed gate refuses write-free and leaves
    the run `queued` (P8-05); it entered no re-drive because no turn was
    attempted, and the start sweep is not repeated once recovery converged.
    The closed -> open edge is what hands it over -- once.
    """

    store = _store(tmp_path)
    thread_id = _thread_with_message(store)
    store.bind_transport(
        transport="telegram",
        external_scope="scope-000001",
        thread_id=thread_id,
        actor_id="cortexd",
        idempotency_key="bind-transport-000001",
    )
    run_id = _api_created_run(store, thread_id)
    bridge = _bridge(store, _AnsweringBackend())
    bridge.RECOVERY_RETRY_SECONDS = 0.01  # type: ignore[misc]
    calls: list[int] = []
    real = store.list_recoverable_runs
    monkeypatch.setattr(
        store,
        "list_recoverable_runs",
        lambda **kwargs: (calls.append(1), real(**kwargs))[1],
    )

    bridge.recover()
    assert bridge.status()["recovery"]["state"] == "converged"
    bridge.start()
    try:
        _wait_until(
            lambda: bridge.status()["reasons"].get(REFUSED_DISPATCH_DISABLED, 0) >= 1
        )
        time.sleep(0.1)
        assert store.get_run(run_id)["state"] == "queued"
        swept_closed = len(calls)
        _enable_dispatch(store)
        _wait_until(lambda: store.get_run(run_id)["state"] == "completed")
        time.sleep(0.3)  # more ticks: no further sweep, nothing else driven
    finally:
        bridge.stop(timeout=5.0)

    assert len(calls) == swept_closed + 1
    assert [run["id"] for run in store.list_thread_runs(thread_id=thread_id)] == [run_id]
    assert len(_assistant_messages(store, thread_id)) == 1
    status = bridge.status()
    assert status["reasons"][REFUSED_DISPATCH_DISABLED] == 1
    assert status["undriven"] == {"submitted": 1, "resubmitted": 1, "canceled": 0, "stalled": 0, "error": None}


# -- ⟦V6-2⟧ rows an earlier generation wrote ---------------------------------


def _legacy_gapped_carrier(store: ControlStore, *, index: int = 1) -> dict:
    """The carrier a pre-V-3 consumer left between its two transactions, as rows.

    A real capture; the consumer's own workspace and `capture <id>` thread
    (`CAPTURE_CONSUMER_WORKSPACE_TITLE` / `CAPTURE_THREAD_TITLE_PREFIX`, the
    strings `capture_consumer._thread` finds its thread by); a run
    `create_run` wrote with NO `workflow=` and NO `install_workflow` after it;
    and an operator message on the thread, which the API stores (G-NEW-1).
    No gen-13 code path writes this shape any more, so the rows are written
    by hand: it is what a store an earlier generation wrote still holds.
    """

    from cortex_platform.product.control import (
        CAPTURE_CONSUMER_WORKSPACE_TITLE,
        CAPTURE_THREAD_TITLE_PREFIX,
    )
    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    capture_id = str(
        store.create_capture(
            payload=f"https://example.test/legacy-{index}",
            note="",
            actor_id="local-operator",
            idempotency_key=f"legacy-capture-{index:06d}",
        ).value["id"]
    )
    workspace = store.create_workspace(
        title=CAPTURE_CONSUMER_WORKSPACE_TITLE,
        actor_id=MACHINE_ACTOR,
        idempotency_key=f"legacy-workspace-{index:06d}",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title=f"{CAPTURE_THREAD_TITLE_PREFIX}{capture_id}",
        expected_revision=workspace["revision"],
        actor_id=MACHINE_ACTOR,
        idempotency_key=f"legacy-thread-{index:06d}",
    ).value
    run = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key=f"legacy-run-{index:06d}",
    ).value
    thread = store.get_thread(str(thread["id"]))
    store.append_message(
        thread_id=str(thread["id"]),
        role="user",
        content="what is this?",
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key=f"legacy-message-{index:06d}",
    )
    return {"capture_id": capture_id, "thread_id": str(thread["id"]), "run_id": str(run["id"])}


def test_a_legacy_gapped_carrier_on_a_message_bearing_thread_is_never_swept_or_driven(
    tmp_path: Path,
) -> None:
    """⟦V6-2⟧ Rows gen 12 wrote are excluded by the thread, not by the workflow.

    The atomic carrier (V-3) says nothing about a run that already exists
    without its workflow row. On a `capture` thread that also holds an
    operator message that run passed the whole predicate, and the start
    sweep drove it to `completed` with a worker asked for outside a window.
    Now the thread itself is a machine thread (`thread_is_machine`), from
    the workspace and title rows the consumer finds its thread by.
    """

    store = _store(tmp_path)
    legacy = _legacy_gapped_carrier(store)
    assert store.thread_is_machine(legacy["thread_id"]) is True
    assert store.run_is_conversation(legacy["run_id"]) is False
    assert store.list_recoverable_runs(conversations_only=True) == []
    # `recover_startup` keeps the full set: the row is still a run.
    assert [run["id"] for run in store.list_recoverable_runs()] == [legacy["run_id"]]
    for gate_open in (False, True):
        if gate_open:
            _enable_dispatch(store)
        worker = _UnavailableWorker("transport_gate_closed")
        bridge = InboundTurnBridge(
            store=store,
            worker=worker,  # type: ignore[arg-type]
            actor_id="cortexd-turn-test",
        )

        bridge.recover()
        outcome = _drain_one(bridge, legacy["thread_id"])

        assert bridge.status()["undriven"]["submitted"] == 0
        assert bridge.status()["queued"] == 0
        assert False not in worker.asked
        assert outcome.outcome == OUTCOME_REFUSED
        assert outcome.reason == (
            REFUSED_NOT_CONVERSATION if gate_open else REFUSED_DISPATCH_DISABLED
        )
        assert store.get_run(legacy["run_id"])["state"] == "queued"
        events = [event["type"] for event in store.list_run_events(legacy["run_id"])]
        assert "run.failed" not in events and "run.completed" not in events
    # The adjudicator's own probe: an answering worker behind a running loop.
    bridge = _bridge(store, _AnsweringBackend())
    bridge.RECOVERY_RETRY_SECONDS = 0.02  # type: ignore[misc]
    bridge.recover()
    bridge.start()
    try:
        time.sleep(0.3)
    finally:
        bridge.stop(timeout=5.0)
    assert store.get_run(legacy["run_id"])["state"] == "queued"
    assert [m["role"] for m in store.list_messages(thread_id=legacy["thread_id"])] == ["user"]
    assert bridge.status()["outcomes"] == {}
    assert bridge.status()["undriven"]["submitted"] == 0


def test_a_thread_that_ever_carried_a_workflow_stays_a_machine_thread(
    tmp_path: Path,
) -> None:
    """⟦V6-2⟧ The second half of the predicate: workflow rows, any run, ever.

    Independent of workspace and title: a thread the engine wrote a workflow
    into is not an operator's conversation afterwards either -- so a run
    somebody still opens there (the store allows it; the API no longer
    does) is not a conversation run, and the bridge opens none itself.
    """

    from cortex_platform.product.engine.capture_consumer import (
        MACHINE_ACTOR,
        RESEARCH_CAPTURE_WORKFLOW,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    thread = store.get_thread(thread_id)
    carrier = store.create_run(
        thread_id=thread_id,
        expected_revision=int(thread["revision"]),
        actor_id="machine:test-engine",
        idempotency_key="workflow-run-000001",
        workflow=RESEARCH_CAPTURE_WORKFLOW,
    ).value
    # ⟦ADJ-4⟧ Ended the way the engine ends it: a run carrying the capture
    # workflow is the engine's to end, and the store refuses an operator.
    run = store.transition_run(
        run_id=str(carrier["id"]),
        target_state="cancel_requested",
        expected_revision=int(carrier["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key="cancel-command-000001",
    ).value
    store.fail_unbound_run(
        run_id=str(run["id"]),
        attempt_id=str(run["active_attempt_id"]),
        expected_revision=int(run["revision"]),
        category=CANCELED_BEFORE_BINDING,
        actor_id=MACHINE_ACTOR,
        idempotency_key="cancel-converge-000001",
    )
    assert store.get_thread(thread_id)["active_run_id"] is None
    assert store.thread_is_machine(thread_id) is True

    bridge = _bridge(store, _AnsweringBackend())
    outcome = _drain_one(bridge, thread_id)

    assert outcome.outcome == OUTCOME_REFUSED
    assert outcome.reason == REFUSED_NOT_CONVERSATION
    assert [run["id"] for run in store.list_thread_runs(thread_id=thread_id)] == [carrier["id"]]
    again = store.create_run(
        thread_id=thread_id,
        expected_revision=int(store.get_thread(thread_id)["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-run-000001",
    ).value
    assert store.run_is_conversation(str(again["id"])) is False
    assert store.list_recoverable_runs(conversations_only=True) == []


def test_an_operator_thread_in_the_engine_workspace_is_not_a_machine_thread(
    tmp_path: Path,
) -> None:
    """⟦V-R4⟧ Machine-ness is who created the thread, not what it is called.

    The legacy carrier thread (created by the consumer: its `create_thread`
    receipt carries the machine actor -- every generation's consumer wrote
    one, `git log` 3914b1c onward) stays a machine thread. A thread an
    operator opened in the same workspace, titled "capture my thoughts", is
    an ordinary conversation: not machine, its run a conversation run the
    sweep projects.
    """

    from cortex_platform.product.control import CAPTURE_CONSUMER_WORKSPACE_TITLE

    store = _store(tmp_path)
    legacy = _legacy_gapped_carrier(store)
    assert store.thread_is_machine(legacy["thread_id"]) is True
    workspace = next(
        item
        for item in store.list_workspaces()
        if item["title"] == CAPTURE_CONSUMER_WORKSPACE_TITLE
    )
    thread = store.create_thread(
        workspace_id=str(workspace["id"]),
        title="capture my thoughts",
        expected_revision=int(store.get_workspace(str(workspace["id"]))["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-thread-in-engine-ws-01",
    ).value
    store.append_message(
        thread_id=str(thread["id"]),
        role="user",
        content="a note to self",
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-message-in-engine-ws-01",
    )
    run = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(store.get_thread(str(thread["id"]))["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-run-in-engine-ws-0001",
    ).value

    assert store.thread_is_machine(str(thread["id"])) is False
    assert store.run_is_conversation(str(run["id"])) is True
    assert [item["id"] for item in store.list_recoverable_runs(conversations_only=True)] == [run["id"]]


# ⟦P9-3 ADJ-1⟧ ---------------- the pause refusal must hold on a LIVE first turn


class _LiveUntilReleasedBackend(FakeHermesBackend):
    """Holds its turn open so the run is `running` while the test asks."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def run(self, request: Any, emit: Any) -> HermesRunResult:
        self.run_calls += 1
        self.active.set()
        # Bounded ONLY so a regression cannot hang the suite.
        self.release.wait(30)
        return HermesRunResult(request.session_ref, final_response="Hello")


def _running_first_turn(
    store: ControlStore, bridge: InboundTurnBridge, backend: Any, thread_id: str
) -> tuple[threading.Thread, str]:
    """Start the process's FIRST turn and leave it live and `running`."""

    turn = threading.Thread(target=lambda: _drain_one(bridge, thread_id))
    turn.start()
    assert backend.active.wait(10), "the first turn never started"
    _wait_until(
        lambda: str(store.get_thread(thread_id)["active_run_id"] or "") != "",
        seconds=10.0,
    )
    run_id = str(store.get_thread(thread_id)["active_run_id"])
    _wait_until(lambda: store.get_run(run_id)["state"] == "running", seconds=10.0)
    return turn, run_id


def test_the_pause_refusal_holds_on_the_first_live_turn(tmp_path: Path) -> None:
    """⟦P9-3 ADJ-1⟧ The refusal was inert in the only window it is needed.

    `_remember_capabilities` had one caller -- `_run_turn`'s `finally` -- so
    `runtime_supports('pause')` answered None for the whole of any turn that
    had not yet ENDED, and both surfaces treat None as unchanged by design. A
    pause is only reachable while a run is `running`, which is exactly when the
    answer was missing, so on a process's first turn the refusal could not fire
    at all. The window was wider still: `last_capabilities` is assigned inside
    `dispatch`, so a closed dispatch gate -- the documented first step of a
    generation rollout -- held it open indefinitely.

    Worse than cosmetic, because of this chain's own pause parity: the
    un-refused pause reaches the C1 discard arm, so the worker's answer is
    thrown away and the run ends `canceled / retryable: false`. At the merge
    base the same pause lost the answer too, but ended `failed / retryable:
    true` -- so inside this window the chain REMOVED the retry affordance.

    The answer is now recorded from the turn loop's own durable re-read, which
    runs while the turn is live.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _LiveUntilReleasedBackend()
    bridge = _bridge(store, backend, turn_timeout=30.0)
    api = _control_api(store, bridge)

    # The precondition the defect needed: nothing has run yet.
    assert bridge.runtime_supports("pause") is None

    turn, run_id = _running_first_turn(store, bridge, backend, thread_id)
    try:
        # The answer is known DURING the turn now, which is the fix.
        _wait_until(
            lambda: bridge.runtime_supports("pause") is False, seconds=10.0
        )
        run = store.get_run(run_id)
        assert run["state"] == "running", run["state"]

        refused = api.handle(
            method="POST",
            target=f"/api/v1/runs/{run_id}/pause",
            headers={
                "X-Cortex-Control-Token": "x" * 48,
                "Idempotency-Key": "adj1-live-pause-000001",
            },
            body=json.dumps({"expected_revision": int(run["revision"])}).encode(),
        )

        assert refused.status == 409, refused.payload
        assert refused.payload["category"] == "pause_unsupported", refused.payload
        # The durable half: the defect wrote this event, and the answer died.
        types = [event["type"] for event in store.list_run_events(run_id)]
        assert "run.pause_requested" not in types, types
        assert store.get_run(run_id)["state"] == "running"
    finally:
        backend.release.set()
        turn.join(timeout=30)

    # The turn was allowed to finish, and its answer survived.
    assert store.get_run(run_id)["state"] == "completed"


def test_the_telegram_pause_refusal_holds_on_the_first_live_turn(
    tmp_path: Path,
) -> None:
    """⟦P9-3 ADJ-1⟧ The twin, because `/pause` is still routed from Telegram.

    Same window, same mechanism, different surface: the adapter's probe is the
    bridge's own `runtime_supports`, so an answer recorded only at turn end
    left the Telegram guard as inert as the route's.
    """

    from cortex_platform.product.transports.telegram import (
        TelegramAdapter,
        TransportProblem,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = _LiveUntilReleasedBackend()
    bridge = _bridge(store, backend, turn_timeout=30.0)

    assert bridge.runtime_supports("pause") is None
    turn, run_id = _running_first_turn(store, bridge, backend, thread_id)
    try:
        _wait_until(
            lambda: bridge.runtime_supports("pause") is False, seconds=10.0
        )
        # The real `_apply_run_action`, with exactly what `daemon.main` binds
        # beside `bind_turn_sink`. The pause branch refuses before it touches
        # the store, so this drives the production method rather than a stand-in
        # for it -- only the two attributes that branch reads are supplied.
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._capability_probe = bridge.runtime_supports  # noqa: SLF001
        adapter._store = store  # noqa: SLF001
        run = store.get_run(run_id)

        with pytest.raises(TransportProblem) as raised:
            adapter._apply_run_action(  # noqa: SLF001
                action="pause",
                run=run,
                actor_id="telegram:1",
                idempotency_key="adj1-telegram-pause-0001",
                retry_reason="",
            )

        assert raised.value.category == "pause_unsupported"
        # The durable half: nothing was written on the way to the refusal.
        types = [event["type"] for event in store.list_run_events(run_id)]
        assert "run.pause_requested" not in types, types
        assert store.get_run(run_id)["state"] == "running"
    finally:
        backend.release.set()
        turn.join(timeout=30)


# ⟦P9-3 ADJ-2⟧ ------------- the stalled floor needs a writer on a LIVE daemon


def _live_loop(bridge: InboundTurnBridge) -> None:
    """Boot the loop the way `daemon.main` does, with a fast cadence."""

    bridge.RECOVERY_RETRY_SECONDS = 0.05  # type: ignore[misc]
    bridge.recover()
    bridge.start()


def test_a_late_answer_is_converged_by_the_live_loop(tmp_path: Path) -> None:
    """⟦P9-3 ADJ-2⟧ The floor had no writer under a running daemon.

    `_converge_stalled` was reachable only from `_sweep_undriven`, which runs at
    `recover()` and on the dispatch gate's open edge. So an operator who
    answered an approval AFTER the hold window closed moved the run to
    `resuming` and it stayed there -- holding the thread's `active_run_id`,
    with a `decision.resolve` nobody could deliver -- until they wrote again or
    the process restarted. The reviewer measured 30x the floor on a live loop.

    Health made it worse rather than visible: `reasons` reads
    `waiting_for_decision`, so cortexd reported a turn that ASKED for a
    decision and never that a run wedged after being ANSWERED.

    The loop now sweeps the runs its own turns left behind on the cadence tick
    it already runs, so the run converges within one floor plus one tick with
    no second operator message.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    # A hold that expires at once: the operator is slower than the turn.
    bridge = _bridge(store, backend, turn_timeout=30.0, decision_wait=0.05,
                     cancel_floor=0.5)
    _live_loop(bridge)
    try:
        bridge.submit(thread_id)
        # The turn really ended before the answer lands -- the shape the
        # start-sweep test cannot reach, because there the daemon is dead.
        _wait_until(lambda: bridge.status()["turns"] >= 1, seconds=20.0)
        run_id = str(store.get_thread(thread_id)["active_run_id"])
        assert store.get_run(run_id)["state"] == "waiting_for_decision"

        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=str(decision["id"]),
            choice="approve_once",
            expected_revision=int(decision["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-answers-too-late-01",
        )
        assert store.get_run(run_id)["state"] == "resuming"

        # No second message, no restart: the live loop is the only writer.
        _wait_until(
            lambda: store.get_run(run_id)["state"] == "failed", seconds=20.0
        )
    finally:
        bridge.stop(timeout=10.0)

    events = store.list_run_events(run_id)
    terminal = next(event for event in events if event["type"] == "run.failed")
    assert terminal["payload"]["category"] == QUIET_AFTER_RESUME
    assert terminal["payload"]["retryable"] is True
    # ⟦P9-3 ADJ-3⟧ Written by a LIVE daemon, so it must not claim otherwise.
    assert "at startup" not in str(terminal["payload"]["detail"])
    assert store.get_thread(thread_id)["active_run_id"] is None
    assert bridge.status()["undriven"]["stalled"] == 1


def test_a_cancel_after_the_turn_ended_is_converged_by_the_live_loop(
    tmp_path: Path,
) -> None:
    """⟦P9-3 ADJ-2⟧ The second arm, which would regress silently on its own.

    The reviewer's escape B: the operator gives up on the wedged run and
    presses Cancel in the cockpit. That writes `cancel_requested` on a run
    whose turn is already gone, so there is nothing to deliver the cancel to
    and -- before this -- nothing to write its terminal either. It sat there
    exactly as `resuming` did.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    bridge = _bridge(store, backend, turn_timeout=30.0, decision_wait=0.05,
                     cancel_floor=0.5)
    _live_loop(bridge)
    try:
        bridge.submit(thread_id)
        _wait_until(lambda: bridge.status()["turns"] >= 1, seconds=20.0)
        run_id = str(store.get_thread(thread_id)["active_run_id"])
        parked = store.get_run(run_id)
        assert parked["state"] == "waiting_for_decision"

        store.transition_run(
            run_id=run_id,
            target_state="cancel_requested",
            expected_revision=int(parked["revision"]),
            actor_id="local-operator",
            idempotency_key="operator-cancels-the-wedge-1",
        )

        _wait_until(
            lambda: store.get_run(run_id)["state"] == "canceled", seconds=20.0
        )
    finally:
        bridge.stop(timeout=10.0)

    events = store.list_run_events(run_id)
    types = [event["type"] for event in events]
    terminal = next(event for event in events if event["type"] == "run.canceled")
    assert terminal["payload"]["category"] == CANCELED_RUNTIME_QUIET
    assert terminal["payload"]["retryable"] is False
    assert "at startup" not in str(terminal["payload"]["detail"])
    # `run.canceled` is not a notification type; `run.failed` is.
    assert "run.failed" not in types, types
    assert store.get_thread(thread_id)["active_run_id"] is None


def test_the_watch_list_keeps_a_parked_run_until_it_is_terminal(
    tmp_path: Path,
) -> None:
    """⟦P9-3 FV-4⟧ The sweep may not drop a candidate for being "not stalled".

    The fix-verify review proposed bounding `_sweep_stalled`'s per-tick work by
    discarding a watched run whose state is neither terminal nor in
    `STALLED_STATES` -- a run parked on an approval nobody answers is read once
    per tick for ever and the sweep acts on none of those reads, which is true
    and is the whole cost. The proposal is unsound, and this pins why rather
    than only that: `waiting_for_decision` is one operator action away from
    `resuming`, the resolve route writes that state and queues no turn behind
    it, and this sweep is then the only writer that can converge the run. A
    candidate dropped while parked is a candidate nothing ever puts back, so
    the late-answer wedge ADJ-2 closed would reopen for exactly the runs ADJ-2
    was written for.

    Applied literally the drop fails the two live-loop ADJ-2 tests above -- the
    late answer and the cancel after the turn ended -- both by timing out on a
    run that never converges, after tens of seconds each. This one says the
    same thing in about one second, at the set itself.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    bridge = _bridge(
        store, backend, turn_timeout=30.0, decision_wait=0.05, cancel_floor=0.3
    )
    _live_loop(bridge)
    try:
        bridge.submit(thread_id)
        _wait_until(lambda: bridge.status()["turns"] >= 1, seconds=20.0)
        run_id = str(store.get_thread(thread_id)["active_run_id"])
        assert store.get_run(run_id)["state"] == "waiting_for_decision"

        # Many cadence ticks with nobody answering. The candidate stays, and
        # the sweep converges nothing: `waiting_for_decision` is not a stall.
        time.sleep(0.6)
        with bridge._lock:  # noqa: SLF001 - the set under test
            watched = set(bridge._stalling)  # noqa: SLF001
        assert run_id in watched, watched
        assert store.get_run(run_id)["state"] == "waiting_for_decision"

        # The operator answers long after the turn ended. Nothing re-adds the
        # run -- no turn runs on this thread again -- so convergence depends on
        # the run having stayed watched.
        decision = store.list_decisions(state="pending")[0]
        store.resolve_decision(
            decision_id=str(decision["id"]),
            choice="approve_once",
            expected_revision=int(decision["revision"]),
            actor_id="local-operator",
            idempotency_key="fv4-answers-after-the-window",
        )
        assert store.get_run(run_id)["state"] == "resuming"

        _wait_until(
            lambda: store.get_run(run_id)["state"] == "failed", seconds=20.0
        )
        # And NOW it leaves, because now it is terminal.
        _wait_until(lambda: run_id not in bridge._stalling, seconds=10.0)
    finally:
        bridge.stop(timeout=10.0)

    terminal = next(
        event
        for event in store.list_run_events(run_id)
        if event["type"] == "run.failed"
    )
    assert terminal["payload"]["category"] == QUIET_AFTER_RESUME
    assert store.get_thread(thread_id)["active_run_id"] is None


class _ReleaseAuthorityOnly:
    """The one `ManagedTransportWorker` surface a pin release needs.

    ⟦P9-3 C2⟧ `_pin_release_authority` reads `worker.releases` and nothing
    else, which is the whole claim that a sweep can hand a pin back without
    acquiring a worker. A double that offers only that is what makes the claim
    testable: anything else the bridge tried to launch would raise here.
    """

    def __init__(self, releases: Releases) -> None:
        self.releases = releases


def test_a_swept_run_hands_its_attempt_pin_back(tmp_path: Path) -> None:
    """⟦P9-3 C2 / batchK 6⟧ The sweep's terminal used to leave the pin held.

    `_end_converging` is the one writer for all three converging terminals, but
    only ONE of its callers -- the live turn's floor, which has an orchestrator
    in scope -- handed the attempt's pin back afterwards. A run converged by
    the abandon path or by either sweep was terminal with its attempt still
    holding the updater pin, and it stayed that way until some later dispatch
    happened to deliver the pending release (`RunOrchestrator.dispatch` does,
    in its `finally`) -- which on a daemon that answers nothing more is never.

    Driven through the sweep on purpose, because the sweep is the caller that
    has no orchestrator and must not acquire one: the release is delivered by
    the store and the updater service alone.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    thread_id = _thread_with_message(store)
    backend = FakeHermesBackend()
    releases = Releases()

    def factory():
        return RunOrchestrator(
            store, HermesAdapter(backend_loader=lambda: backend), releases
        )

    bridge = InboundTurnBridge(
        store=store,
        worker=_ReleaseAuthorityOnly(releases),  # type: ignore[arg-type]
        actor_id="cortexd-turn-test",
        orchestrator_factory=factory,
        turn_timeout=30.0,
        decision_wait=0.05,
        cancel_floor=0.2,
    )

    outcome = _drain_one(bridge, thread_id)
    assert outcome.outcome == OUTCOME_DECISION_REQUIRED, outcome
    run_id = str(outcome.run_id)
    attempt_id = str(store.get_run(run_id)["active_attempt_id"])
    # The turn ended without a terminal, so nothing has queued a release yet
    # and the attempt still holds its pin.
    assert releases.attempt_pin(attempt_id) is not None

    decision = store.list_decisions(state="pending")[0]
    store.resolve_decision(
        decision_id=str(decision["id"]),
        choice="approve_once",
        expected_revision=int(decision["revision"]),
        actor_id="local-operator",
        idempotency_key="c2-answers-after-the-window",
    )
    assert store.get_run(run_id)["state"] == "resuming"
    # `_stale_converging` answers only past the floor, on this loop's clock.
    time.sleep(0.25)
    with bridge._lock:  # noqa: SLF001 - the loop's own watch list
        bridge._stalling.add(run_id)  # noqa: SLF001

    bridge._sweep_stalled()  # noqa: SLF001 - the caller with no orchestrator

    assert store.get_run(run_id)["state"] == "failed"
    assert releases.attempt_pin(attempt_id) is None, releases.finished
    assert [pin.attempt_id for pin in releases.finished] == [attempt_id]
    types = [event["type"] for event in store.list_run_events(run_id)]
    # Acked, not merely attempted: `runtime.pin_release.failed` is the shape
    # this used to reach when the release was delivered by nobody.
    assert "runtime.pin_release.acked" in types, types
