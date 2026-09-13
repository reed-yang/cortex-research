from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from cortex_platform.runtime.hermes import (
    HermesAdapter,
    HermesDuplicateAttemptError,
    HermesUnavailableError,
)
from cortex_platform.runtime.models import (
    ActionOutcomeStatus,
    AttemptRequest,
    ControlAction,
    ControlRequest,
    DecisionResolution,
    EventDurability,
    RuntimeBinding,
    SessionOpenRequest,
)

from .fakes import FakeHermesBackend


def test_backend_does_not_start_before_started_event_can_be_committed() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-durable-first",
            attempt_id="attempt-durable-first",
            binding=binding,
            user_message="Durable first",
        )
        stream = adapter.execute(request)

        started = await anext(stream)
        assert started.type == "runtime.run.started"
        assert started.durability == EventDurability.DURABLE
        assert backend.active.is_set() is False

        next_event = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(backend.active.wait, 1)
        cancel = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert cancel.accepted is True
        terminal = await next_event
        assert terminal.type == "runtime.run.canceled"

    asyncio.run(scenario())


def test_cancel_between_started_and_backend_start_is_durable_and_idempotent() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-start-window",
            attempt_id="attempt-start-window",
            binding=binding,
            user_message="Cancel in start window",
        )
        stream = adapter.execute(request)

        started = await anext(stream)
        assert started.type == "runtime.run.started"
        assert backend.run_calls == 0

        first = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        repeated = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert first.accepted is True
        assert repeated.accepted is True

        remaining = [event async for event in stream]
        assert remaining[-1].type == "runtime.run.canceled"
        assert backend.run_calls == 1
        assert adapter._active_executions == {}

    asyncio.run(scenario())


def test_cancel_during_backend_initialization_returns_exact_accepted_outcome() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        backend.sessions["session-initializing"] = None
        loader_entered = threading.Event()
        release_loader = threading.Event()

        def load_backend() -> FakeHermesBackend:
            loader_entered.set()
            assert release_loader.wait(2)
            return backend

        adapter = HermesAdapter(backend_loader=load_backend)
        binding = RuntimeBinding(
            adapter_id="hermes",
            runtime_session_ref="session-initializing",
            generation=0,
            adapter_version="test",
        )
        request = AttemptRequest(
            run_id="run-initializing-cancel",
            attempt_id="attempt-initializing-cancel",
            binding=binding,
            user_message="must not run",
        )
        stream = adapter.execute(request)
        first_event = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(loader_entered.wait, 1)

        cancel = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
                adapter_operation_id="cancel-during-initialization",
                delivery_epoch=1,
            )
        )
        assert cancel.accepted is True
        assert cancel.outcome == ActionOutcomeStatus.ACCEPTED
        assert cancel.adapter_operation_id == "cancel-during-initialization"
        assert cancel.delivery_epoch == 1

        release_loader.set()
        assert (await asyncio.wait_for(first_event, 2)).type == (
            "runtime.run.started"
        )
        remaining = [event async for event in stream]
        assert remaining[-1].type == "runtime.run.canceled"
        assert backend.run_calls == 1

    asyncio.run(scenario())


def test_duplicate_execute_rejects_without_failure_event_and_preserves_owner() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-duplicate",
            attempt_id="attempt-duplicate",
            binding=binding,
            user_message="Long task",
        )

        owner_stream = adapter.execute(request)
        started = await anext(owner_stream)
        assert started.type == "runtime.run.started"

        with pytest.raises(HermesDuplicateAttemptError):
            _ = [event async for event in adapter.execute(request)]
        assert backend.run_calls == 0

        terminal_task = asyncio.create_task(anext(owner_stream))
        assert await asyncio.to_thread(backend.active.wait, 1)
        cancel = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert cancel.accepted is True
        assert (await terminal_task).type == "runtime.run.canceled"
        await owner_stream.aclose()
        assert backend.run_calls == 1
        assert adapter._active_executions == {}

    asyncio.run(scenario())


def test_control_rejects_wrong_binding_without_touching_active_backend() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        wrong_binding = replace(binding, runtime_session_ref="wrong-runtime-session")
        request = AttemptRequest(
            run_id="run-binding-control",
            attempt_id="attempt-binding-control",
            binding=binding,
            user_message="binding control",
        )
        stream = adapter.execute(request)
        assert (await anext(stream)).type == "runtime.run.started"

        wrong_cancel = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=wrong_binding,
                action=ControlAction.CANCEL,
            )
        )
        wrong_steer = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=wrong_binding,
                action=ControlAction.STEER,
                text="wrong target",
            )
        )
        assert wrong_cancel.accepted is False
        assert wrong_cancel.reason_code == "runtime_binding_mismatch"
        assert wrong_steer.accepted is False
        assert wrong_steer.reason_code == "runtime_binding_mismatch"
        assert backend.cancel_calls == 0
        assert backend.steer_texts == []

        terminal = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(backend.active.wait, 1)
        correct_steer = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.STEER,
                text="correct target",
            )
        )
        concurrent_wrong, correct_cancel = await asyncio.gather(
            adapter.request_control(
                ControlRequest(
                    run_id=request.run_id,
                    attempt_id=request.attempt_id,
                    binding=wrong_binding,
                    action=ControlAction.CANCEL,
                )
            ),
            adapter.request_control(
                ControlRequest(
                    run_id=request.run_id,
                    attempt_id=request.attempt_id,
                    binding=binding,
                    action=ControlAction.CANCEL,
                )
            ),
        )
        assert correct_steer.accepted is True
        assert concurrent_wrong.reason_code == "runtime_binding_mismatch"
        assert correct_cancel.accepted is True
        assert backend.cancel_calls == 1
        assert backend.steer_texts == ["correct target"]
        assert (await terminal).type == "runtime.run.canceled"
        await stream.aclose()

        after_terminal = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert after_terminal.outcome == ActionOutcomeStatus.DEDUPLICATED
        assert backend.cancel_calls == 1

    asyncio.run(scenario())


def test_run_stream_tool_decision_and_rebind_translation() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(rotate_session=True)
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-1",
            attempt_id="attempt-1",
            binding=binding,
            user_message="Research memory architectures",
        )

        events = []
        resolution = None
        async for event in adapter.execute(request):
            events.append(event)
            if event.type == "runtime.decision.required":
                resolution = await adapter.resolve_decision(
                    DecisionResolution(
                        run_id=request.run_id,
                        attempt_id=request.attempt_id,
                        binding=binding,
                        decision_id=str(event.payload["decision_id"]),
                        choice="approve_once",
                        revision=0,
                    )
                )

        assert resolution is not None and resolution.accepted is True
        assert backend.decision_choice == "approve_once"
        assert [event.type for event in events] == [
            "runtime.run.started",
            "runtime.token.delta",
            "runtime.tool.started",
            "runtime.tool.progress",
            "runtime.status",
            "runtime.tool.completed",
            "runtime.decision.required",
            "runtime.session_rebound",
            "runtime.message.completed",
            "runtime.run.completed",
        ]
        token = next(event for event in events if event.type == "runtime.token.delta")
        assert token.durability == EventDurability.TRANSIENT
        decision = next(
            event for event in events if event.type == "runtime.decision.required"
        )
        assert decision.payload["options"] == ["approve_once", "deny"]
        assert "session" not in decision.payload["options"]
        assert "always" not in decision.payload["options"]
        status = next(event for event in events if event.type == "runtime.status")
        assert "hidden chain" not in repr(status.payload)
        completed_tool = next(
            event for event in events if event.type == "runtime.tool.completed"
        )
        assert "private raw result" not in repr(completed_tool.payload)
        rebound = next(
            event for event in events if event.type == "runtime.session_rebound"
        )
        assert rebound.payload["generation"] == binding.generation + 1

        duplicate = await adapter.resolve_decision(
            DecisionResolution(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                decision_id="decision-1",
                choice="approve_once",
                revision=0,
            )
        )
        assert duplicate.accepted is True
        conflict = await adapter.resolve_decision(
            DecisionResolution(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                decision_id="decision-1",
                choice="deny",
                revision=0,
            )
        )
        assert conflict.accepted is False
        assert conflict.reason_code == "decision_conflict"

    asyncio.run(scenario())


def test_decision_requires_exact_registered_identity_before_forwarding() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend()
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-decision-identity",
            attempt_id="attempt-decision-identity",
            binding=binding,
            user_message="decision identity",
        )
        stream = adapter.execute(request)
        while True:
            event = await anext(stream)
            if event.type == "runtime.decision.required":
                break
        decision_id = str(event.payload["decision_id"])

        def resolution(
            *,
            run_id: str = request.run_id,
            attempt_id: str = request.attempt_id,
            target_binding=binding,
            choice: str = "approve_once",
            revision: int = 0,
        ) -> DecisionResolution:
            return DecisionResolution(
                run_id=run_id,
                attempt_id=attempt_id,
                binding=target_binding,
                decision_id=decision_id,
                choice=choice,
                revision=revision,
            )

        wrong_run = await adapter.resolve_decision(resolution(run_id="wrong-run"))
        wrong_attempt = await adapter.resolve_decision(
            resolution(attempt_id="wrong-attempt")
        )
        wrong_session = await adapter.resolve_decision(
            resolution(
                target_binding=replace(
                    binding, runtime_session_ref="wrong-runtime-session"
                )
            )
        )
        wrong_revision = await adapter.resolve_decision(resolution(revision=1))
        wrong_choice = await adapter.resolve_decision(resolution(choice="always"))

        assert wrong_run.reason_code == "decision_identity_mismatch"
        assert wrong_attempt.reason_code == "decision_identity_mismatch"
        assert wrong_session.reason_code == "pending_decision_missing"
        assert wrong_revision.reason_code == "decision_revision_conflict"
        assert wrong_choice.reason_code == "unsupported_decision_choice"
        assert backend.resolve_calls == 0

        accepted = await adapter.resolve_decision(resolution())
        assert accepted.accepted is True
        assert backend.resolve_calls == 1
        remaining = [event async for event in stream]
        assert remaining[-1].type == "runtime.run.completed"

    asyncio.run(scenario())


def test_concurrent_decision_cas_shares_equal_choice_and_rejects_conflicts() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(block_resolution=True)
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        backend.arm_decision("run-decision", "attempt-decision")
        adapter._register_pending_decision(
            session_ref=binding.runtime_session_ref,
            run_id="run-decision",
            attempt_id="attempt-decision",
            decision_id="decision-1",
            revision=0,
        )

        def resolution(choice: str, revision: int = 0) -> DecisionResolution:
            return DecisionResolution(
                run_id="run-decision",
                attempt_id="attempt-decision",
                binding=binding,
                decision_id="decision-1",
                choice=choice,
                revision=revision,
            )

        owner = asyncio.create_task(
            adapter.resolve_decision(resolution("approve_once"))
        )
        assert await asyncio.to_thread(backend.resolve_entered.wait, 1)
        same = asyncio.create_task(adapter.resolve_decision(resolution("approve_once")))
        await asyncio.sleep(0)
        assert same.done() is False

        opposite = await adapter.resolve_decision(resolution("deny"))
        assert opposite.accepted is False
        assert opposite.reason_code == "decision_conflict"
        revision = await adapter.resolve_decision(
            resolution("approve_once", revision=1)
        )
        assert revision.accepted is False
        assert revision.reason_code == "decision_revision_conflict"

        backend.release_resolution.set()
        owner_result, same_result = await asyncio.gather(owner, same)
        assert owner_result.accepted is True
        assert same_result.accepted is True
        assert backend.resolve_calls == 1

    asyncio.run(scenario())


def test_transient_decision_failure_is_shared_then_retryable() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(block_resolution=True, fail_resolutions=1)
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        backend.arm_decision("run-retry-decision", "attempt-retry-decision")
        adapter._register_pending_decision(
            session_ref=binding.runtime_session_ref,
            run_id="run-retry-decision",
            attempt_id="attempt-retry-decision",
            decision_id="decision-1",
            revision=0,
        )
        resolution = DecisionResolution(
            run_id="run-retry-decision",
            attempt_id="attempt-retry-decision",
            binding=binding,
            decision_id="decision-1",
            choice="approve_once",
            revision=0,
        )

        owner = asyncio.create_task(adapter.resolve_decision(resolution))
        assert await asyncio.to_thread(backend.resolve_entered.wait, 1)
        same_batch = asyncio.create_task(adapter.resolve_decision(resolution))
        await asyncio.sleep(0)
        assert same_batch.done() is False
        backend.release_resolution.set()
        failed_owner, failed_waiter = await asyncio.gather(owner, same_batch)
        assert failed_owner.accepted is False
        assert failed_waiter.accepted is False
        assert failed_owner.reason_code == "runtime_outcome_unknown"
        assert failed_waiter.reason_code == "runtime_outcome_unknown"
        assert backend.resolve_calls == 1
        assert adapter._decision_inflight == {}

        opposite_after_failure = await adapter.resolve_decision(
            DecisionResolution(
                run_id=resolution.run_id,
                attempt_id=resolution.attempt_id,
                binding=binding,
                decision_id=resolution.decision_id,
                choice="deny",
                revision=resolution.revision,
            )
        )
        assert opposite_after_failure.reason_code == "decision_conflict"
        revision_after_failure = await adapter.resolve_decision(
            DecisionResolution(
                run_id=resolution.run_id,
                attempt_id=resolution.attempt_id,
                binding=binding,
                decision_id=resolution.decision_id,
                choice=resolution.choice,
                revision=1,
            )
        )
        assert revision_after_failure.reason_code == "decision_revision_conflict"
        assert backend.resolve_calls == 1

        retried = await adapter.resolve_decision(replace(resolution, delivery_epoch=1))
        assert retried.accepted is True
        assert backend.resolve_calls == 2

    asyncio.run(scenario())


def test_canceling_first_decision_caller_does_not_orphan_other_waiters() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(block_resolution=True)
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        backend.arm_decision("run-caller-cancel", "attempt-caller-cancel")
        adapter._register_pending_decision(
            session_ref=binding.runtime_session_ref,
            run_id="run-caller-cancel",
            attempt_id="attempt-caller-cancel",
            decision_id="decision-1",
            revision=0,
        )
        resolution = DecisionResolution(
            run_id="run-caller-cancel",
            attempt_id="attempt-caller-cancel",
            binding=binding,
            decision_id="decision-1",
            choice="approve_once",
            revision=0,
        )

        first = asyncio.create_task(adapter.resolve_decision(resolution))
        assert await asyncio.to_thread(backend.resolve_entered.wait, 1)
        waiter = asyncio.create_task(adapter.resolve_decision(resolution))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        backend.release_resolution.set()
        result = await asyncio.wait_for(waiter, timeout=1)
        assert result.accepted is True
        assert backend.resolve_calls == 1
        assert adapter._decision_inflight == {}

    asyncio.run(scenario())


def test_two_adapters_sharing_native_backend_reject_the_second_lease() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        owner_adapter = HermesAdapter(backend_loader=lambda: backend)
        duplicate_adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await owner_adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-shared-backend",
            attempt_id="attempt-shared-backend",
            binding=binding,
            user_message="one owner",
        )

        owner_stream = owner_adapter.execute(request)
        assert (await anext(owner_stream)).type == "runtime.run.started"
        with pytest.raises(HermesDuplicateAttemptError):
            _ = [event async for event in duplicate_adapter.execute(request)]
        assert duplicate_adapter._active_executions == {}

        terminal = asyncio.create_task(anext(owner_stream))
        assert await asyncio.to_thread(backend.active.wait, 1)
        await owner_adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert (await terminal).type == "runtime.run.canceled"
        await owner_stream.aclose()

    asyncio.run(scenario())


def test_decision_success_cache_is_bounded() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend()
        adapter = HermesAdapter(backend_loader=lambda: backend)
        adapter._decision_cache_limit = 2
        binding = await adapter.open_session(SessionOpenRequest())

        for index in range(3):
            backend.arm_decision(f"run-{index}", f"attempt-{index}")
            adapter._register_pending_decision(
                session_ref=binding.runtime_session_ref,
                run_id=f"run-{index}",
                attempt_id=f"attempt-{index}",
                decision_id="decision-1",
                revision=0,
            )
            result = await adapter.resolve_decision(
                DecisionResolution(
                    run_id=f"run-{index}",
                    attempt_id=f"attempt-{index}",
                    binding=binding,
                    decision_id="decision-1",
                    choice="approve_once",
                    revision=0,
                )
            )
            assert result.accepted is True

        assert len(adapter._decision_successes) == 2

    asyncio.run(scenario())


def test_early_close_and_backend_load_failure_release_adapter_ownership() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-close-before-start",
            attempt_id="attempt-close-before-start",
            binding=binding,
            user_message="Close after durable start",
        )
        stream = adapter.execute(request)
        assert (await anext(stream)).type == "runtime.run.started"
        await stream.aclose()
        assert adapter._active_executions == {}
        assert backend.run_calls == 0
        assert backend._leases == {}

        def missing_backend():
            raise HermesUnavailableError("missing")

        unavailable = HermesAdapter(backend_loader=missing_backend)
        unavailable_stream = unavailable.execute(
            AttemptRequest(
                run_id="run-load-fail",
                attempt_id="attempt-load-fail",
                binding=binding,
                user_message="fail load",
            )
        )
        events = [event async for event in unavailable_stream]
        assert events[-1].type == "runtime.run.failed"
        assert unavailable._active_executions == {}

    asyncio.run(scenario())


def test_backend_run_exception_releases_adapter_and_backend_ownership() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="run_error")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        events = [
            event
            async for event in adapter.execute(
                AttemptRequest(
                    run_id="run-backend-error",
                    attempt_id="attempt-backend-error",
                    binding=binding,
                    user_message="raise",
                )
            )
        ]

        assert [event.type for event in events] == [
            "runtime.run.started",
            "runtime.run.failed",
        ]
        assert adapter._active_executions == {}
        assert backend._leases == {}

    asyncio.run(scenario())


def test_terminal_event_is_not_required_to_be_resumed_for_cleanup() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-terminal-cleanup",
            attempt_id="attempt-terminal-cleanup",
            binding=binding,
            user_message="terminal cleanup",
        )
        stream = adapter.execute(request)
        assert (await anext(stream)).type == "runtime.run.started"
        terminal = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(backend.active.wait, 1)
        await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )

        assert (await terminal).type == "runtime.run.canceled"
        assert adapter._active_executions == {}
        assert backend._leases == {}

    asyncio.run(scenario())


def test_cancel_is_cooperative_and_pause_never_maps_to_interrupt() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-cancel",
            attempt_id="attempt-cancel",
            binding=binding,
            user_message="Long task",
        )
        events = []

        async def consume() -> None:
            async for event in adapter.execute(request):
                events.append(event)

        consumer = asyncio.create_task(consume())
        active = await asyncio.to_thread(backend.active.wait, 1)
        assert active is True

        pause = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.PAUSE,
            )
        )
        assert pause.accepted is False
        assert pause.reason_code == "pause_requires_cortex_stage_boundary"
        assert backend.cancel_calls == 0

        cancel = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert cancel.accepted is True
        await consumer
        assert events[-1].type == "runtime.run.canceled"
        assert backend.cancel_calls == 1

    asyncio.run(scenario())


def test_steer_requires_text_and_targets_active_attempt() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-steer",
            attempt_id="attempt-steer",
            binding=binding,
            user_message="Long task",
        )

        async def consume() -> None:
            async for _ in adapter.execute(request):
                pass

        consumer = asyncio.create_task(consume())
        assert await asyncio.to_thread(backend.active.wait, 1)
        missing = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.STEER,
            )
        )
        assert missing.reason_code == "steer_text_required"
        steered = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.STEER,
                text="Focus on TTT",
            )
        )
        assert steered.accepted is True
        assert backend.steer_texts == ["Focus on TTT"]
        await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        await consumer

    asyncio.run(scenario())
