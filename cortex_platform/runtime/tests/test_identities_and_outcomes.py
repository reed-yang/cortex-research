from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from cortex_platform.runtime.hermes import HermesAdapter, HermesIncompatibleError
from cortex_platform.runtime.models import (
    ActionOutcomeQuery,
    ActionOutcomeStatus,
    AttemptRequest,
    ControlAction,
    ControlRequest,
    DecisionResolution,
    EventDurability,
    ReleasePin,
    RuntimeCheckpoint,
    SessionOpenRequest,
)

from .fakes import FAKE_RUNTIME_IDENTITY, FakeHermesBackend


async def _complete_events() -> list:
    backend = FakeHermesBackend(rotate_session=True)
    adapter = HermesAdapter(backend_loader=lambda: backend)
    binding = await adapter.open_session(
        SessionOpenRequest(adapter_operation_id="open-thread-identity")
    )
    request = AttemptRequest(
        run_id="run-stable-events",
        attempt_id="attempt-stable-events",
        binding=binding,
        user_message="stable events",
        adapter_operation_id="execute-attempt-stable-events",
    )
    events = []
    decision_resolution = None
    async for event in adapter.execute(request):
        events.append(event)
        if event.type == "runtime.decision.required":
            decision_resolution = DecisionResolution(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                decision_id=str(event.payload["decision_id"]),
                choice="approve_once",
                revision=0,
                adapter_operation_id="resolve-stable-decision",
                delivery_epoch=4,
            )
            result = await adapter.resolve_decision(decision_resolution)
            assert result.outcome == ActionOutcomeStatus.ACCEPTED
    assert decision_resolution is not None
    duplicate = await adapter.resolve_decision(decision_resolution)
    assert duplicate.outcome == ActionOutcomeStatus.DEDUPLICATED
    return events


def test_durable_event_identity_reconstructs_across_adapter_restart() -> None:
    first = asyncio.run(_complete_events())
    second = asyncio.run(_complete_events())

    first_durable = [
        (event.type, event.event_id, event.event_sequence)
        for event in first
        if event.durability == EventDurability.DURABLE
    ]
    second_durable = [
        (event.type, event.event_id, event.event_sequence)
        for event in second
        if event.durability == EventDurability.DURABLE
    ]
    assert first_durable == second_durable
    assert [sequence for _, _, sequence in first_durable] == list(
        range(len(first_durable))
    )
    assert all(
        event.durable_idempotency_key == event.event_id
        for event in first
        if event.durability == EventDurability.DURABLE
    )

    first_transient = [
        event for event in first if event.durability == EventDurability.TRANSIENT
    ]
    second_transient = [
        event for event in second if event.durability == EventDurability.TRANSIENT
    ]
    assert all(event.durable_idempotency_key is None for event in first_transient)
    assert [event.event_id for event in first_transient] != [
        event.event_id for event in second_transient
    ]


def test_session_operations_are_process_local_and_deduplicated() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend()
        adapter = HermesAdapter(backend_loader=lambda: backend)
        request = SessionOpenRequest(adapter_operation_id="session-open-op")
        root = await adapter.open_session(request)
        duplicate_root = await adapter.open_session(request)
        assert duplicate_root == root
        assert len(backend.sessions) == 1

        fork_request = SessionOpenRequest(adapter_operation_id="session-fork-op")
        child = await adapter.fork_session(root, fork_request)
        duplicate_child = await adapter.fork_session(root, fork_request)
        assert duplicate_child == child
        assert len(backend.sessions) == 2

        checkpoint = RuntimeCheckpoint(
            checkpoint_ref="checkpoint-session-dedup",
            conversation_history=({"role": "user", "content": "resume"},),
            adapter_operation_id="session-recover-op",
        )
        recovered = await adapter.recover(child, checkpoint)
        duplicate_recovered = await adapter.recover(child, checkpoint)
        assert duplicate_recovered == recovered
        assert len(backend.sessions) == 3

        health = await adapter.health()
        assert health.runtime_identity == FAKE_RUNTIME_IDENTITY
        assert health.capabilities.action_outcome_query is True
        assert health.capabilities.durable_operation_deduplication is False

    asyncio.run(scenario())


def test_control_outcomes_are_fenced_deduplicated_and_queryable() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(
            SessionOpenRequest(adapter_operation_id="control-session-open")
        )
        attempt = AttemptRequest(
            run_id="run-control-outcome",
            attempt_id="attempt-control-outcome",
            binding=binding,
            user_message="wait",
            adapter_operation_id="execute-control-outcome",
        )
        stream = adapter.execute(attempt)
        assert (await anext(stream)).type == "runtime.run.started"
        terminal = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(backend.active.wait, 1)

        request = ControlRequest(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            binding=binding,
            action=ControlAction.CANCEL,
            adapter_operation_id="control-cancel-op",
            delivery_epoch=3,
        )
        accepted = await adapter.request_control(request)
        duplicate = await adapter.request_control(request)
        assert accepted.outcome == ActionOutcomeStatus.ACCEPTED
        assert duplicate.outcome == ActionOutcomeStatus.DEDUPLICATED
        assert backend.cancel_calls == 1

        queried = await adapter.query_action_outcome(
            ActionOutcomeQuery(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                binding=binding,
                adapter_operation_id=request.adapter_operation_id,
                delivery_epoch=request.delivery_epoch,
            )
        )
        assert queried.status == ActionOutcomeStatus.ACCEPTED
        stale = await adapter.query_action_outcome(
            ActionOutcomeQuery(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                binding=binding,
                adapter_operation_id=request.adapter_operation_id,
                delivery_epoch=2,
            )
        )
        assert stale.status == ActionOutcomeStatus.REJECTED
        assert stale.reason_code == "stale_delivery_epoch"
        stale_delivery = await adapter.request_control(
            replace(request, delivery_epoch=2)
        )
        assert stale_delivery.outcome == ActionOutcomeStatus.REJECTED
        assert stale_delivery.reason_code == "stale_delivery_epoch"
        assert (await terminal).type == "runtime.run.canceled"
        await stream.aclose()

    asyncio.run(scenario())


def test_unknown_control_outcome_requires_a_new_delivery_epoch_to_retry() -> None:
    class UncertainCancelBackend(FakeHermesBackend):
        def cancel(self, *args, **kwargs):
            raise RuntimeError("connection lost after write")

    async def scenario() -> None:
        backend = UncertainCancelBackend(mode="cancel_wait")
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(
            SessionOpenRequest(adapter_operation_id="unknown-session-open")
        )
        attempt = AttemptRequest(
            run_id="run-unknown-outcome",
            attempt_id="attempt-unknown-outcome",
            binding=binding,
            user_message="wait",
            adapter_operation_id="execute-unknown-outcome",
        )
        stream = adapter.execute(attempt)
        assert (await anext(stream)).type == "runtime.run.started"

        request = ControlRequest(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            binding=binding,
            action=ControlAction.CANCEL,
            adapter_operation_id="uncertain-cancel-op",
            delivery_epoch=8,
        )
        result = await adapter.request_control(request)
        assert result.outcome == ActionOutcomeStatus.UNKNOWN
        queried = await adapter.query_action_outcome(
            ActionOutcomeQuery(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                binding=binding,
                adapter_operation_id=request.adapter_operation_id,
                delivery_epoch=request.delivery_epoch,
            )
        )
        assert queried.status == ActionOutcomeStatus.UNKNOWN
        same_epoch = await adapter.request_control(request)
        assert same_epoch.outcome == ActionOutcomeStatus.UNKNOWN
        newer_epoch = await adapter.request_control(replace(request, delivery_epoch=9))
        assert newer_epoch.outcome == ActionOutcomeStatus.UNKNOWN
        await stream.aclose()

    asyncio.run(scenario())


def test_exact_runtime_handshake_and_missing_identity_fail_closed(
    monkeypatch,
) -> None:
    class IdentitylessBackend(FakeHermesBackend):
        def runtime_identity(self):
            return None

    async def scenario() -> None:
        adapter = HermesAdapter(backend_loader=lambda: FakeHermesBackend())
        exact_pin = ReleasePin(
            attempt_id="attempt-handshake",
            release_id=FAKE_RUNTIME_IDENTITY.release_id,
            state_generation_id=FAKE_RUNTIME_IDENTITY.state_generation_id,
            slot_id=FAKE_RUNTIME_IDENTITY.slot_id,
            artifact_digest=FAKE_RUNTIME_IDENTITY.artifact_digest,
            worker_protocol=FAKE_RUNTIME_IDENTITY.worker_protocol,
        )
        verified = await adapter.handshake(exact_pin)
        assert verified.verified is True
        assert verified.observed == FAKE_RUNTIME_IDENTITY

        mismatch = await adapter.handshake(
            replace(exact_pin, state_generation_id="wrong-generation")
        )
        assert mismatch.verified is False
        assert mismatch.reason_code == "runtime_identity_mismatch"

        identityless = HermesAdapter(backend_loader=lambda: IdentitylessBackend())
        health = await identityless.health()
        assert health.reason_code == "runtime_identity_unverified"
        assert health.capabilities.available is False
        failed = await identityless.handshake(exact_pin)
        assert failed.verified is False
        assert failed.reason_code == "runtime_identity_unverified"

        # ⟦AMD-3⟧ The trap, stated as a test. `_managed_identity_required` used
        # to be `backend_loader is None`, so this refusal fired for the native
        # in-process backend and was switched OFF by the very thing that makes a
        # backend managed. It is an explicit argument now, and both directions
        # are pinned: a managed adapter refuses a backend that cannot name its
        # release...
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes._load_native_backend",
            lambda **kwargs: IdentitylessBackend(),
        )
        managed_adapter = HermesAdapter(
            managed=True, backend_loader=lambda: IdentitylessBackend()
        )
        managed_health = await managed_adapter.health()
        assert managed_health.reason_code == "runtime_identity_unverified"
        with pytest.raises(
            HermesIncompatibleError, match="runtime_identity_unverified"
        ):
            await managed_adapter.open_session(
                SessionOpenRequest(adapter_operation_id="must-fail-closed")
            )
        # ...and the native adapter does not, because the refusal that governs
        # it is the `durable_operation_deduplication=False` capability gate.
        native_adapter = HermesAdapter()
        native_health = await native_adapter.health()
        assert native_health.reason_code == "runtime_identity_unverified"
        await native_adapter.open_session(
            SessionOpenRequest(adapter_operation_id="native-is-not-managed")
        )

    asyncio.run(scenario())
