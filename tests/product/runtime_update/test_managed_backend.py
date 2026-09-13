"""The managed backend, against a real worker process on a real descriptor.

The one thing that cannot be faked here is the probe: ⟦AMD-3⟧ asks for a real
crash, a real reap and a real relaunch, so that is what runs — a worker is
SIGKILLed mid-operation and the ledger it left behind is interrogated by its
successor. Everything a fake could prove about this was already provable, and
was already wrong: `durable_operation_deduplication` was a hardcoded literal for
four slices.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from cortex_platform.runtime.hermes import (
    HermesDuplicateAttemptError,
    HermesRunInput,
    HermesSignal,
    RuntimeOperationUncertain,
)
from cortex_platform.runtime.models import ActionOutcomeStatus
from cortex_platform.runtime.managed_hermes import (
    PROBE_NAMESPACE,
    DedupProbeFailed,
    ManagedHermesBackend,
    ManagedRuntimeUnavailable,
    _request_digest,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.digests import (
    PROJECTION_VERSION,
    result_digest,
    turn_durable_stream,
)

from .test_worker_turns import ECHO_RUNNER, APPROVAL_RUNNER, _drive


def _backend(tmp_path: Path, runner_body: str = ECHO_RUNNER) -> ManagedHermesBackend:
    descriptor_path, _state = _drive(tmp_path, runner_body)
    return ManagedHermesBackend(descriptor_path)


def _run_input(**overrides: object) -> HermesRunInput:
    values: dict[str, object] = {
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "session_ref": "cortex_session",
        "user_message": "hello",
        "system_message": None,
        "conversation_history": (),
        "metadata": {},
        "execution_token": object(),
        "adapter_operation_id": "attempt-digest",
    }
    values.update(overrides)
    return HermesRunInput(**values)  # type: ignore[arg-type]


def test_research_prompt_payload_preserves_options_and_digest_identity(tmp_path):
    backend = _backend(tmp_path)
    configured = {"ephemeral_system_prompt": "configured policy"}
    backend._agent_options_factory = lambda: configured
    try:
        request = _run_input(system_message="current packet A", metadata={
            "cortex_research_prompt_mode": "ephemeral_v1",
        })
        first = backend._turn_payload(request)
        assert first["system_message"] is None
        assert first["agent_options"]["ephemeral_system_prompt"] == "configured policy\n\ncurrent packet A"
        assert first == backend._turn_payload(request)
        changed = backend._turn_payload(_run_input(system_message="current packet B", metadata=request.metadata))
        assert _request_digest(first) != _request_digest(changed)
        generic = backend._turn_payload(_run_input(system_message="persistent generic"))
        assert generic["system_message"] == "persistent generic"
        assert generic["agent_options"]["ephemeral_system_prompt"] == "configured policy"
        assert configured == {"ephemeral_system_prompt": "configured policy"}
    finally:
        backend.close()


def test_the_probe_demonstrates_dedup_with_a_real_crash(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        capabilities = backend.capabilities()
        assert capabilities.durable_operation_deduplication is True
        provenance = backend.probe_provenance
        assert provenance is not None
        assert provenance.probe_operation_id.startswith("probe.")
        assert provenance.projection_version == PROJECTION_VERSION
        supervisor = backend._supervisor
        assert supervisor is not None and supervisor.process is not None
        assert provenance.worker_pid == supervisor.process.pid
        assert provenance.worker_launch_id == supervisor.worker_launch_id
        # The probe wrote where it said it would, and only there.
        probe_state = Path(supervisor.descriptor.state_dir) / PROBE_NAMESPACE
        assert (probe_state / "operations.log").is_file() or any(
            probe_state.iterdir()
        )
    finally:
        backend.close()


def test_capabilities_are_live_and_die_with_the_process(tmp_path: Path) -> None:
    """⟦AMD-3⟧ Nothing outlives the process it was proven on."""

    backend = _backend(tmp_path)
    try:
        assert backend.capabilities().durable_operation_deduplication is True
        supervisor = backend._supervisor
        assert supervisor is not None
        proven_launch = supervisor.worker_launch_id
        # Kill the worker the capability was proven against, without letting the
        # backend relaunch: what `capabilities()` says now is the whole question.
        process = supervisor.process
        assert process is not None
        process.kill()
        process.wait(timeout=5)
        assert backend._dedup_is_live() is False
        # A relaunch reruns the probe and rebinds provenance to a new process.
        assert backend.capabilities().durable_operation_deduplication is True
        assert backend._supervisor is not None
        assert backend._supervisor.worker_launch_id != proven_launch
    finally:
        backend.close()


def test_a_failed_probe_leaves_the_runtime_available_and_the_capability_false(
    tmp_path: Path,
) -> None:
    """The gate the operator sees must say which thing failed.

    Folding a failed probe into `runtime_unavailable` would tell an operator the
    runtime is down when it is serving; the orchestration gate reads
    `durable_operation_deduplication` and raises the typed refusal itself.
    """

    backend = _backend(tmp_path)

    def refuse(self: ManagedHermesBackend) -> str:
        raise DedupProbeFailed("replay_duplicate", "a replayed operation re-executed")

    backend._run_dedup_probe = refuse.__get__(backend)  # type: ignore[method-assign]
    try:
        capabilities = backend.capabilities()
        assert capabilities.available is True
        assert capabilities.durable_operation_deduplication is False
        assert backend.probe_failure_stage == "replay_duplicate"
    finally:
        backend.close()


def test_a_turn_streams_signals_and_records_a_reproducible_digest(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    signals: list[HermesSignal] = []
    try:
        result = backend.run(_run_input(), signals.append)
        assert result.final_response == "done: hello"
        assert result.canceled is False and result.failed is False
        kinds = [signal.kind for signal in signals]
        assert kinds == ["tool.started", "token.delta", "tool.completed"]
        # The stable ids the native backend minted are reconstructed here rather
        # than sent, because they are a pure function of the payload.
        assert signals[0].stable_id == "tool:call-1:started"
        assert signals[2].stable_id == "tool:call-1:completed"
        # ⟦AMD-1⟧ The digest the worker recorded is reproducible from the
        # durable stream this side observed, through the same source file.
        durable = turn_durable_stream(
            [(signal.kind, signal.payload) for signal in signals],
            result={
                "session_ref": result.session_ref,
                "final_response": result.final_response,
                "canceled": result.canceled,
                "failed": result.failed,
            },
            session_ref="cortex_session",
        )
        assert result_digest(durable) == backend.last_result_digest
    finally:
        backend.close()


def test_the_second_turn_of_an_attempt_is_a_new_operation(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        backend.run(_run_input(), lambda _signal: None)
        first = backend.last_result_digest
        backend.run(_run_input(), lambda _signal: None)
        assert backend.last_result_digest == first
        supervisor = backend._supervisor
        assert supervisor is not None
        assert supervisor.request(
            "operation.status", {"operation_id": "attempt-1.0"}
        )["state"] == "committed"
        assert supervisor.request(
            "operation.status", {"operation_id": "attempt-1.1"}
        )["state"] == "committed"
    finally:
        backend.close()


def test_a_decision_is_resolved_over_the_channel_mid_turn(tmp_path: Path) -> None:
    backend = _backend(tmp_path, APPROVAL_RUNNER)
    outcomes = []

    def emit(signal: HermesSignal) -> None:
        if signal.kind == "decision.required":
            outcomes.append(
                backend.resolve_decision(
                    "cortex_session",
                    "run-1",
                    "attempt-1",
                    "decision-1",
                    "approve_once",
                    "decision-op",
                    1,
                )
            )

    try:
        result = backend.run(_run_input(), emit)
        assert result.final_response == "chose approve_once"
        assert outcomes and outcomes[0].status.value == "accepted"
        # The recorded outcome is queryable without repeating the effect.
        replayed = backend.query_action_outcome(
            "cortex_session", "run-1", "attempt-1", "decision-op", 1
        )
        assert replayed.status.value == "accepted"
    finally:
        backend.close()


def test_an_uncertain_turn_is_terminal_rather_than_a_silent_retry(
    tmp_path: Path,
) -> None:
    """A `duplicate` whose record is not committed is not a result to return.

    This is the D4 restatement's teeth: the worker answers `duplicate` for an
    operation it already owns, and the backend's job is to distinguish "already
    finished, here is the digest" from "began and never finished" — the second
    of which is terminal for the attempt rather than something to re-run.
    """

    backend = _backend(tmp_path)
    try:
        supervisor = backend._launch()
        # Stage exactly the state a crash mid-turn leaves: begun, never
        # finished, with the digest the backend is about to replay.
        payload = backend._turn_payload(_run_input(attempt_id="attempt-2"))
        supervisor.request(
            "operation.begin",
            {
                "operation_id": "attempt-2.0",
                "kind": "turn",
                "request_digest": _request_digest(payload),
            },
        )
        with pytest.raises(RuntimeOperationUncertain):
            backend.run(_run_input(attempt_id="attempt-2"), lambda _signal: None)
    finally:
        backend.close()


def test_runtime_identity_is_the_five_key_projection_of_the_measurement(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        identity = backend.runtime_identity()
        assert identity is not None
        supervisor = backend._supervisor
        assert supervisor is not None
        measured = supervisor.identity
        assert identity.release_id == measured["release_id"]
        assert identity.artifact_digest == measured["artifact_digest"]
        assert identity.worker_protocol == measured["worker_protocol"]
        assert identity.slot_id == measured["slot_id"]
        assert identity.state_generation_id == measured["state_generation_id"]
    finally:
        backend.close()


def test_the_probe_descriptor_names_only_the_probe_namespace(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        path = backend._probe_descriptor_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        production = json.loads(
            backend._descriptor_path.read_text(encoding="utf-8")
        )
        assert document["state_dir"] == str(
            Path(production["state_dir"]) / PROBE_NAMESPACE
        )
        # Everything else is the production descriptor verbatim: the same slot,
        # the same interpreter, the same digests — otherwise the probe would be
        # demonstrating a mechanism that is not the one production uses.
        assert {
            key: value for key, value in document.items() if key != "state_dir"
        } == {key: value for key, value in production.items() if key != "state_dir"}
    finally:
        backend.close()


def test_orchestration_still_imports_only_the_port() -> None:
    """D-S3.3-3's fence, extended: the recompute side is the managed backend."""

    orchestration = (
        Path(__file__).parents[3]
        / "cortex_platform"
        / "product"
        / "orchestration"
    )
    forbidden = (
        "runtime_update.worker_protocol",
        "runtime_update.operation_ledger",
        "runtime_update.models",
        "cortex_worker",
    )
    violations = {
        str(path.relative_to(orchestration)): name
        for path in orchestration.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for module in ([f"{node.module or ''}.{alias.name}" for alias in node.names]
                       if isinstance(node, ast.ImportFrom) else
                       [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
        for name in forbidden
        if name in module
    }
    assert violations == {}


def test_the_digest_replays_out_of_a_real_control_db(tmp_path: Path) -> None:
    """⟦AMD-1⟧ end to end: the worker's digest == what Control actually stored.

    The two sides never exchange the digest's inputs. The worker computes it
    from the events it emitted; this recomputes it from `run_events` after
    Control has minted ids and sequences, dropped the tool arguments, re-minted
    the decision id and moved the message text out into `messages`. If the
    projection covered a field Control mints or drops, this is where it comes
    apart — which is the reason the projection exists rather than a digest over
    raw payloads.
    """

    import asyncio
    from dataclasses import dataclass

    from cortex_platform.product.control.store import ControlStore
    from cortex_platform.product.orchestration.service import RunOrchestrator
    from cortex_platform.runtime.hermes import HermesAdapter
    from cortex_platform.runtime.managed_hermes import (
        control_durable_stream,
        recompute_result_digest,
    )
    from cortex_platform.tests.product.orchestration.test_service import _queued_run

    backend = _backend(tmp_path)
    identity = backend.runtime_identity()
    assert identity is not None

    @dataclass
    class Pin:
        """The real slot's identity, so the handshake is a real comparison."""

        attempt_id: str
        release_id: str = identity.release_id
        generation_id: str = identity.state_generation_id
        slot_id: str = identity.slot_id
        artifact_digest: str = identity.artifact_digest
        worker_protocol: str = identity.worker_protocol

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
            assert expected_pin is None or expected_pin == pin
            self.pins.append(pin)
            return pin

        def finish_attempt(self, attempt_id: str, pin: Pin) -> None:
            assert attempt_id == pin.attempt_id
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

    async def scenario() -> str:
        store = ControlStore(tmp_path / "control.db")
        store.initialize()
        run = _queued_run(store)
        adapter = HermesAdapter(managed=True, backend_loader=lambda: backend)
        orchestrator = RunOrchestrator(store, adapter, Releases())
        completed = await asyncio.wait_for(orchestrator.dispatch(run["id"]), 60)
        assert completed["state"] == "completed", [
            (event["type"], event["payload"])
            for event in store.list_run_events(run["id"])
        ]
        attempt_id = str(completed["active_attempt_id"])
        events = store.list_run_events(run["id"])
        # Control stored the events, and stored them sanitized.
        started = next(
            event for event in events if event["type"] == "runtime.tool.started"
        )
        assert "arguments" not in started["payload"]
        messages = {
            message["id"]: message["content"]
            for message in store.list_messages(completed["thread_id"])
        }
        replayed = control_durable_stream(
            events,
            attempt_id=attempt_id,
            message_content=lambda identifier: messages[identifier],
        )
        assert [event_type for event_type, _payload in replayed] == [
            "runtime.run.started",
            "runtime.tool.started",
            "runtime.tool.completed",
            "runtime.message.completed",
            "runtime.run.completed",
        ]
        return recompute_result_digest(replayed)

    try:
        digest = asyncio.run(scenario())
        assert digest == backend.last_result_digest
    finally:
        backend.close()


def test_the_worker_predicts_the_adapter_s_durable_payloads_exactly() -> None:
    """The correspondence the real acceptance caught, pinned where it is cheap.

    The worker computes its digest before the adapter has translated anything,
    so `turn_durable_stream` has to predict the *event* payload rather than
    digest its own signal payload. `decision.required` is the one that actually
    changes shape — the adapter supplies the option set and the default prompt —
    and a worker that missed it produced a digest nothing could reproduce.
    """

    from cortex_platform.product.runtime_update.worker_payload.cortex_worker.digests import (
        project_event,
        signal_event_payload,
        signal_event_type,
    )
    from cortex_platform.runtime.events import RuntimeEventFactory, translate_hermes_signal
    from cortex_platform.runtime.models import AttemptRequest, RuntimeBinding

    request = AttemptRequest(
        run_id="run-1",
        attempt_id="attempt-1",
        binding=RuntimeBinding(
            adapter_id="hermes",
            runtime_session_ref="cortex_session",
            generation=0,
            adapter_version="0.2.0",
        ),
        user_message="hello",
    )
    signals = {
        "tool.started": {
            "tool_call_id": "call-1",
            "tool_name": "bash",
            "arguments": {"command": "echo"},
        },
        "tool.completed": {
            "tool_call_id": "call-1",
            "tool_name": "bash",
            "is_error": False,
            "duration_ms": 12,
        },
        "decision.required": {
            "decision_id": "decision-1",
            "decision_kind": "approval",
            "prompt": "Approve this runtime tool action?",
            "command": "echo",
            "description": "harmless",
        },
    }
    for kind, payload in signals.items():
        factory = RuntimeEventFactory(request)
        event = translate_hermes_signal(factory, kind, payload)
        assert signal_event_type(kind) == event.type
        # The projection of what the worker predicted must equal the projection
        # of what the adapter actually emitted, field for field.
        assert project_event(event.type, signal_event_payload(kind, payload)) == (
            project_event(event.type, event.payload)
        )


def test_a_failed_channel_is_relaunched_rather_than_handed_back(
    tmp_path: Path,
) -> None:
    """⟦AMD-4⟧ `alive` is `poll() is None`; a wedged channel passes it forever."""

    backend = _backend(tmp_path)
    try:
        first = backend._require_supervisor()
        process = first.process
        assert process is not None
        pid = process.pid
        first._fail("worker replied to an unknown request")
        assert first.failed is True
        assert first.alive is True
        second = backend._require_supervisor()
        assert second is not first
        assert second.process is not None and second.process.pid != pid
        # `close(force=True)` ran: the process is reaped and its ledger flock
        # released, which is what the relaunch needed.
        assert first.process is None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        backend.close()


def test_a_wedged_channel_is_reported_rather_than_silently_healed(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        supervisor = backend._require_supervisor()
        supervisor._fail("worker replied to an unknown request")
        report = backend.compatibility()
        check = next(
            item for item in report.checks if item.name == "worker_process"
        )
        assert check.compatible is False
        assert backend.capabilities().available is False
        # A status call is a read: the relaunch belongs to the next turn.
        assert backend._supervisor is supervisor
    finally:
        backend.close()


def test_relaunches_are_bounded_so_a_wedge_is_not_traded_for_a_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every relaunch reruns the full probe: two processes, a SIGKILL, a reap."""

    backend = _backend(tmp_path)
    monkeypatch.setattr(ManagedHermesBackend, "MAX_CHANNEL_RELAUNCHES", 2)
    try:
        for _ in range(2):
            backend._require_supervisor()._fail("wedged")
            backend._require_supervisor()
        backend._require_supervisor()._fail("wedged")
        with pytest.raises(ManagedRuntimeUnavailable, match="relaunch"):
            backend._require_supervisor()
        assert backend.capabilities().available is False
    finally:
        backend.close()


def test_resolving_a_decision_no_gate_registered_is_rejected_not_accepted(
    tmp_path: Path,
) -> None:
    """⟦S3.4⟧ `delivered` has to mean delivered.

    `TurnContext.resolve` already returned False when no gate exists for that
    id, and `Turn.resolve` threw the bool away, so the reply was an
    unconditional `{"delivered": True}` and the backend recorded ACCEPTED for a
    decision that reached nobody.
    """

    backend = _backend(tmp_path, APPROVAL_RUNNER)
    outcomes: list[object] = []

    def emit(signal: HermesSignal) -> None:
        if signal.kind == "decision.required":
            # A decision id no gate ever registered, on an open turn.
            outcomes.append(
                backend.resolve_decision(
                    "cortex_session",
                    "run-1",
                    "attempt-1",
                    "decision-never-asked-for",
                    "approve_once",
                    "decision-op-missing",
                    1,
                )
            )
            # Then the real one, so the turn can finish.
            backend.resolve_decision(
                "cortex_session",
                "run-1",
                "attempt-1",
                "decision-1",
                "approve_once",
                "decision-op-real",
                1,
            )

    try:
        result = backend.run(_run_input(), emit)
        assert result.final_response == "chose approve_once"
        assert len(outcomes) == 1
        missing = outcomes[0]
        assert missing.status == ActionOutcomeStatus.REJECTED
        assert missing.reason_code == "decision_not_pending"
    finally:
        backend.close()


def test_a_duplicate_turn_does_not_leave_its_inbox_open_forever(
    tmp_path: Path,
) -> None:
    """ADJ-14. `run()` returns via `_duplicate_result` without iterating events.

    `begin_turn` registers the turn's inbox before it asks, and only
    `turn_events`' `finally` ever removes it — which the duplicate path never
    reaches. The entry then stays in `self._turns` for the life of the process,
    so a later replay of the same operation id is `operation is already open`.
    """

    backend = _backend(tmp_path)
    try:
        supervisor = backend._launch()
        payload = backend._turn_payload(_run_input(attempt_id="attempt-dup"))
        digest = _request_digest(payload)
        # Exactly the state a committed turn leaves behind.
        supervisor.request(
            "operation.begin",
            {
                "operation_id": "attempt-dup.0",
                "kind": "turn",
                "request_digest": digest,
            },
        )
        supervisor.request(
            "operation.finish",
            {
                "operation_id": "attempt-dup.0",
                "outcome": "committed",
                "result_digest": "b" * 64,
            },
        )
        result = backend.run(
            _run_input(attempt_id="attempt-dup"), lambda _signal: None
        )
        assert result.final_response is None
        assert supervisor._turns == {}
    finally:
        backend.close()


SIDE_EFFECT_RUNNER = """\
import os


def runner(request, context):
    # The marker native's pre-start-cancel tests use: proof the turn body did
    # not run, rather than proof the result object says it did not.
    with open(
        os.path.join(os.getcwd(), "turn-side-effect.txt"), "w", encoding="utf-8"
    ) as handle:
        handle.write(request["user_message"])
    return {
        "session_ref": request["session_ref"],
        "final_response": "ran",
        "canceled": False,
        "failed": False,
    }
"""


def test_a_cancel_between_reserve_and_run_is_accepted_and_honoured(
    tmp_path: Path,
) -> None:
    """Mirrors `test_native_pre_cancel_is_consumed_before_agent_construction`.

    `_ManagedExecution.canceled` was declared and never written or read;
    `cancel()` collapsed "no execution" and "reserved but not started" into one
    `REJECTED/no_active_turn`; and `run()` overwrote the entry `reserve_attempt`
    created without ever looking at it.
    """

    backend = _backend(tmp_path, SIDE_EFFECT_RUNNER)
    marker = Path(backend._descriptor.state_dir) / "turn-side-effect.txt"
    try:
        token = backend.reserve_attempt("run-pre", "attempt-pre")
        assert token is not None
        outcome = backend.cancel(
            "run-pre", "attempt-pre", "managed-pre-cancel", 0, "cortex_session"
        )
        assert outcome.status == ActionOutcomeStatus.ACCEPTED
        assert outcome.reason_code is None
        result = backend.run(
            _run_input(
                run_id="run-pre", attempt_id="attempt-pre", execution_token=token
            ),
            lambda _signal: None,
        )
        assert result.canceled is True
        assert result.final_response is None
        assert not marker.exists()
        # The entry is popped, so `inspect()` does not keep reporting it active.
        assert backend.inspect("cortex_session").active is False
    finally:
        backend.close()


def test_a_cancel_that_lands_while_the_worker_launches_still_stops_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking only at entry misses the whole window.

    The launch and the dedup probe sit between the entry check and
    `begin_turn` — two extra processes, a SIGKILL and a reap — and a cancel
    delivered in there must not be overtaken by the turn it was meant to stop.
    """

    backend = _backend(tmp_path, SIDE_EFFECT_RUNNER)
    marker = Path(backend._descriptor.state_dir) / "turn-side-effect.txt"
    try:
        token = backend.reserve_attempt("run-mid", "attempt-mid")
        assert token is not None
        original = backend._turn_payload

        def payload_then_cancel(request: HermesRunInput) -> dict:
            document = original(request)
            outcome = backend.cancel(
                "run-mid", "attempt-mid", "managed-mid-cancel", 0, "cortex_session"
            )
            assert outcome.status == ActionOutcomeStatus.ACCEPTED
            return document

        monkeypatch.setattr(backend, "_turn_payload", payload_then_cancel)
        result = backend.run(
            _run_input(
                run_id="run-mid", attempt_id="attempt-mid", execution_token=token
            ),
            lambda _signal: None,
        )
        assert result.canceled is True
        assert not marker.exists()
    finally:
        backend.close()


def test_cancel_still_refuses_an_attempt_that_was_never_reserved(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        outcome = backend.cancel(
            "run-absent", "attempt-absent", "managed-absent", 0, "cortex_session"
        )
        assert outcome.status == ActionOutcomeStatus.REJECTED
        assert outcome.reason_code == "no_active_turn"
    finally:
        backend.close()


def test_run_refuses_an_execution_token_that_is_not_the_reserved_one(
    tmp_path: Path,
) -> None:
    """Native raises `HermesDuplicateAttemptError`; managed validated nothing."""

    backend = _backend(tmp_path)
    try:
        token = backend.reserve_attempt("run-token", "attempt-token")
        assert token is not None
        with pytest.raises(HermesDuplicateAttemptError):
            backend.run(
                _run_input(
                    run_id="run-token",
                    attempt_id="attempt-token",
                    execution_token=object(),
                ),
                lambda _signal: None,
            )
    finally:
        backend.close()


def test_a_redelivered_cancel_at_the_same_epoch_is_deduplicated(
    tmp_path: Path,
) -> None:
    """ADJ-12. The managed backend had no analogue of `_perform_action`."""

    backend = _backend(tmp_path)
    try:
        assert backend.reserve_attempt("run-dedup", "attempt-dedup") is not None
        first = backend.cancel(
            "run-dedup", "attempt-dedup", "cancel-op", 2, "cortex_session"
        )
        assert first.status == ActionOutcomeStatus.ACCEPTED
        again = backend.cancel(
            "run-dedup", "attempt-dedup", "cancel-op", 2, "cortex_session"
        )
        assert again.status == ActionOutcomeStatus.DEDUPLICATED
        assert again.reason_code is None
    finally:
        backend.close()


def test_a_redelivered_action_at_a_lower_epoch_is_stale(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        backend.reserve_attempt("run-stale", "attempt-stale")
        assert (
            backend.cancel(
                "run-stale", "attempt-stale", "cancel-op", 3, "cortex_session"
            ).status
            == ActionOutcomeStatus.ACCEPTED
        )
        stale = backend.cancel(
            "run-stale", "attempt-stale", "cancel-op", 1, "cortex_session"
        )
        assert stale.status == ActionOutcomeStatus.REJECTED
        assert stale.reason_code == "stale_delivery_epoch"
    finally:
        backend.close()


def test_one_adapter_operation_id_for_two_identities_is_a_conflict(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    try:
        backend.reserve_attempt("run-conflict", "attempt-a")
        backend.reserve_attempt("run-conflict", "attempt-b")
        assert (
            backend.cancel(
                "run-conflict", "attempt-a", "shared-op", 0, "cortex_session"
            ).status
            == ActionOutcomeStatus.ACCEPTED
        )
        conflict = backend.cancel(
            "run-conflict", "attempt-b", "shared-op", 0, "cortex_session"
        )
        assert conflict.status == ActionOutcomeStatus.REJECTED
        assert conflict.reason_code == "adapter_operation_conflict"
    finally:
        backend.close()


def test_a_query_at_a_higher_epoch_reads_the_outcome_that_was_applied(
    tmp_path: Path,
) -> None:
    """Exact-epoch matching read UNKNOWN for an action that had been applied."""

    backend = _backend(tmp_path)
    try:
        backend.reserve_attempt("run-query", "attempt-query")
        assert (
            backend.cancel(
                "run-query", "attempt-query", "query-op", 1, "cortex_session"
            ).status
            == ActionOutcomeStatus.ACCEPTED
        )
        later = backend.query_action_outcome(
            "cortex_session", "run-query", "attempt-query", "query-op", 2
        )
        assert later.status == ActionOutcomeStatus.ACCEPTED
        earlier = backend.query_action_outcome(
            "cortex_session", "run-query", "attempt-query", "query-op", 0
        )
        assert earlier.status == ActionOutcomeStatus.REJECTED
        assert earlier.reason_code == "stale_delivery_epoch"
        unknown = backend.query_action_outcome(
            "cortex_session", "run-query", "attempt-query", "never-seen", 1
        )
        assert unknown.status == ActionOutcomeStatus.UNKNOWN
        assert unknown.reason_code == "operation_outcome_unknown"
        wrong_identity = backend.query_action_outcome(
            "cortex_session", "run-query", "attempt-other", "query-op", 1
        )
        assert wrong_identity.status == ActionOutcomeStatus.REJECTED
        assert wrong_identity.reason_code == "adapter_operation_conflict"
    finally:
        backend.close()


def test_the_environment_is_rebuilt_for_every_launch(tmp_path: Path) -> None:
    """P5.4: the credential is scoped to the window, not to the backend object.

    A backend constructed once at daemon start, with a fixed environment, would
    have decided the token question before the operator ever opened a window --
    and a relaunch after the window closed would carry whatever that first
    decision was. The factory is consulted per launch, so both directions are
    answered by the gate at the moment the process starts.
    """

    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    builds: list[int] = []

    def environment() -> dict[str, str]:
        builds.append(len(builds))
        return {
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "CORTEX_LAUNCH_MARKER": f"launch-{len(builds)}",
        }

    backend = ManagedHermesBackend(
        descriptor_path, environment_factory=environment
    )
    try:
        first = backend._require_supervisor()
        # Three launches for one `_require_supervisor`: the probe's two
        # throwaway processes and the production worker.
        assert len(builds) == 3
        first._fail("wedged")
        backend._require_supervisor()
        assert len(builds) == 6
    finally:
        backend.close()


def test_an_environment_and_a_factory_cannot_both_be_given(tmp_path: Path) -> None:
    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    with pytest.raises(ValueError, match="environment"):
        ManagedHermesBackend(
            descriptor_path,
            environment={"PATH": os.defpath},
            environment_factory=lambda: {"PATH": os.defpath},
        )


def test_the_worker_accessor_launches_once_and_returns_the_live_process(
    tmp_path: Path,
) -> None:
    """The seam P5.4 binds a transport RPC to, rather than a private method."""

    backend = _backend(tmp_path)
    try:
        supervisor = backend.worker()
        assert supervisor.alive is True
        assert backend.worker() is supervisor
        assert supervisor.process is not None
    finally:
        backend.close()


def test_the_sandbox_egress_port_is_the_one_the_backend_was_given(
    tmp_path: Path,
) -> None:
    """The profile permits exactly the port the transport is configured to reach.

    443 is the production answer and the default. An acceptance that points the
    transport at a loopback stand-in has to be able to say so in one place, or
    the seatbelt silently denies the only socket the run is about.
    """

    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    backend = ManagedHermesBackend(descriptor_path, egress_port=8443)
    try:
        backend.worker()
        launch = backend.sandbox_launch
        assert launch is not None
        assert launch.policy.egress_port == 8443
        assert '(remote tcp "*:8443")' in launch.profile_path.read_text()
    finally:
        backend.close()


# ⟦P5.4d / F9⟧ ------------------------------- the payload the shipped fork gets


class _ForkSessionDB:
    """`hermes_state.SessionDB` with the certified fork's real signature.

    Two lines, both load-bearing, both copied from the gen 9 artifact's
    `hermes_state.py`::

        def __init__(self, db_path: Path = None):
            self.db_path = db_path or DEFAULT_DB_PATH
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    `db_path` is a `pathlib.Path` or nothing. Every test in this tree passed
    `session_db_path=None`, so the one producer of a real value — the managed
    backend — was never joined to the one consumer of it until a real turn ran.
    """

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or (Path(os.environ["HERMES_HOME"]) / "state.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


class _ForkAgent:
    def __init__(self, **options: object) -> None:
        self.options = options
        self.session_id = options.get("session_id")
        self.interrupted = False

    def run_conversation(self, message: str, **_kwargs: object) -> dict[str, object]:
        return {"final_response": f"echo:{message}", "completed": True}


class _Context:
    canceled = False

    def emit(self, kind: str, payload: object) -> None:
        pass

    def raise_if_canceled(self) -> None:
        pass

    def await_decision(self, decision_id: str) -> None:
        return None


def _fork_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fork on `sys.path` whose surface is the certified one's."""

    import sys
    from types import ModuleType

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = _ForkSessionDB  # type: ignore[attr-defined]
    hermes_state.SCHEMA_VERSION = 13  # type: ignore[attr-defined]
    run_agent = ModuleType("run_agent")
    run_agent.AIAgent = _ForkAgent  # type: ignore[attr-defined]
    tools = ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    terminal_tool = ModuleType("tools.terminal_tool")
    terminal_tool.set_approval_callback = lambda callback: None  # type: ignore[attr-defined]
    for name, module in (
        ("hermes_state", hermes_state),
        ("run_agent", run_agent),
        ("tools", tools),
        ("tools.terminal_tool", terminal_tool),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def test_the_turn_payload_is_one_the_shipped_fork_runner_can_consume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦F9⟧ The managed backend's payload, driven through the shipped runner.

    This is the hop nothing joined. `ManagedHermesBackend._turn_payload` is the
    only producer of a non-null `session_db_path`; `ForkRunner.__call__` is its
    only consumer; and JSON has no `Path`, so what crosses is a string. The
    shipped runner hands that string straight to `hermes_state.SessionDB`, whose
    very first statement is `self.db_path.parent.mkdir(...)` — so every managed
    turn died with `AttributeError: 'str' object has no attribute 'parent'`
    before `AIAgent` was constructed, before a single request left the sandbox,
    and with the exception thrown away by `turn.py` on purpose.

    The product cannot send a `Path` and the certified worker cannot convert a
    string, so the product names no path and the fork uses its own default —
    `HERMES_HOME/state.db`, inside the per-generation state dir the product
    already owns, already creates and already permits the seatbelt to write.
    """

    from cortex_platform.product.runtime_update.worker_payload.cortex_worker.runtime import (
        ForkRunner,
    )

    backend = _backend(tmp_path)
    try:
        payload = backend._turn_payload(_run_input())  # noqa: SLF001
    finally:
        backend.close()

    home = tmp_path / "fork-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _fork_modules(monkeypatch)

    result = ForkRunner(home)(payload, _Context())

    assert result["final_response"] == "echo:hello"
    assert result["failed"] is False
    # Where the session store actually landed: inside HERMES_HOME, which is
    # `<state_dir>/hermes-home` for a real launch.
    assert (home / "state.db").parent == home
