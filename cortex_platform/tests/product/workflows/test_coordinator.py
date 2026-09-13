from __future__ import annotations

import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import InvalidTransition
from cortex_platform.product.workflows import (
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectOutcomeUnknown,
    EffectPermanentlyRejected,
    EffectReconciliationResult,
    EngineReference,
    LineageNode,
    LineageQueryRequest,
    LineageQueryResult,
    RuntimeStageRequest,
    RuntimeStageResult,
    SourceImportRequest,
    SourceImportResult,
    StageDefinition,
    WorkflowCoordinator,
    WorkflowDecisionPlan,
    WorkflowDefinition,
    WorkflowEffectPlan,
    stable_attempt_operation_id,
    stable_operation_id,
    stable_runtime_operation_id,
    workflow_input_hash,
)
from cortex_platform.tests.support.workflows import (
    DeterministicResearchEngine,
    DeterministicRuntime,
)


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}-{self._counts[kind]}"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class PermanentlyRejectingEngine(DeterministicResearchEngine):
    """Reject one source import without an ambiguous external outcome."""

    def import_source(self, request: SourceImportRequest) -> SourceImportResult:
        self.calls.append((request.operation_id, request.delivery_epoch))
        raise EffectPermanentlyRejected("invalid_source")


class AlwaysLostBeforeCommitEngine(DeterministicResearchEngine):
    """Lose every source-import reply before recording an external commit."""

    def import_source(self, request: SourceImportRequest) -> SourceImportResult:
        self.calls.append((request.operation_id, request.delivery_epoch))
        raise EffectOutcomeUnknown(request.operation_id)


class RecordingArtifactPort:
    def __init__(self, result: ArtifactWorkflowResult) -> None:
        self.result = result
        self.calls: list[tuple[str, int]] = []

    def execute_artifact(
        self, request: ArtifactWorkflowRequest
    ) -> ArtifactWorkflowResult:
        self.calls.append((request.operation_id, request.delivery_epoch))
        return replace(self.result, delivery_epoch=request.delivery_epoch)

    def reconcile_effect(self, request):
        raise AssertionError("reconciliation is not expected")


class RecoveringArtifactPort(RecordingArtifactPort):
    def __init__(self, result: ArtifactWorkflowResult) -> None:
        super().__init__(result)
        self.reconciliation_calls: list[int] = []

    def execute_artifact(
        self, request: ArtifactWorkflowRequest
    ) -> ArtifactWorkflowResult:
        self.calls.append((request.operation_id, request.delivery_epoch))
        raise EffectOutcomeUnknown(request.operation_id)

    def reconcile_effect(self, request):
        self.reconciliation_calls.append(request.delivery_epoch)
        return EffectReconciliationResult(
            domain=request.domain,
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=request.delivery_epoch,
            disposition="committed",
            result=self.result,
        )


def _store(tmp_path: Path) -> ControlStore:
    store = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        id_factory=DeterministicIds(),
    )
    store.initialize()
    return store


def _run(store: ControlStore, *, suffix: str = "001") -> dict:
    workspace = store.create_workspace(
        title="Research",
        actor_id="local",
        idempotency_key=f"coordinator-workspace-{suffix}",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Coordinator",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=f"coordinator-thread-{suffix}",
    ).value
    return store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=f"coordinator-run-{suffix}",
    ).value


def _running_run(store: ControlStore, *, suffix: str = "runtime") -> dict:
    run = _run(store, suffix=suffix)
    attempt_id = run["active_attempt_id"]
    release_id = f"hermes-{suffix}"
    generation_id = f"state-{suffix}"
    owner = f"worker-{suffix}"
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=attempt_id,
        dispatch_owner=owner,
        runtime_release_id=release_id,
        state_generation_id=generation_id,
        runtime_slot_id=f"slot-{suffix}",
        runtime_artifact_digest=f"artifact-{suffix}",
        runtime_worker_protocol=f"protocol-{suffix}",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-reserve-{suffix}",
    ).value
    binding = store.create_runtime_binding(
        thread_id=run["thread_id"],
        adapter_id="hermes",
        runtime_session_ref=f"private-{suffix}",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key=f"coordinator-binding-{suffix}",
    ).value
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=attempt_id,
        runtime_binding_id=binding["id"],
        runtime_release_id=release_id,
        state_generation_id=generation_id,
        dispatch_owner=owner,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-pin-{suffix}",
    ).value
    identities = {
        "_attempt_id": attempt_id,
        "_binding_id": binding["id"],
        "_release_id": release_id,
        "_generation_id": generation_id,
    }
    for target in ("starting", "running"):
        run = store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=attempt_id,
            runtime_binding_id=binding["id"],
            runtime_release_id=release_id,
            state_generation_id=generation_id,
            target_state=target,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=f"coordinator-{target}-{suffix}",
        ).value
    return {**run, **identities}


def _record_runtime_event(
    store: ControlStore,
    run: dict,
    *,
    suffix: str = "stage",
    adapter_event_id: str | None = None,
) -> str:
    adapter_event_id = adapter_event_id or f"runtime-stage-{suffix}"
    store.record_runtime_observation(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        event_type="runtime.tool.completed",
        payload={
            "tool_call_id": f"tool-{suffix}",
            "tool_name": "workflow_stage",
            "is_error": False,
            "duration_ms": 10,
        },
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-observation-{suffix}",
        adapter_event_id=adapter_event_id,
        adapter_event_sequence=0,
    )
    return next(
        event["id"]
        for event in store.list_run_events(run["id"])
        if event["type"] == "runtime.tool.completed"
        and event["causation_id"] == adapter_event_id
    )


def _fail_unbound_run(store: ControlStore, run: dict, *, key: str) -> dict:
    return store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        expected_revision=store.get_run(run["id"])["revision"],
        category="coordinator_test",
        actor_id="local",
        idempotency_key=f"coordinator-{key}-001",
    ).value


def _cancel_run(store: ControlStore, run: dict, *, key: str) -> dict:
    current = store.get_run(run["id"])
    return store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=current["revision"],
        actor_id="local",
        idempotency_key=f"coordinator-{key}-001",
    ).value


def _pause_run(store: ControlStore, run: dict, *, suffix: str) -> dict:
    pause_requested = store.transition_run(
        run_id=run["id"],
        target_state="pause_requested",
        expected_revision=store.get_run(run["id"])["revision"],
        actor_id="local",
        idempotency_key=f"coordinator-pause-requested-{suffix}",
    ).value
    checkpointed = store.commit_checkpoint(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        checkpoint_uri=f"cortex://artifacts/checkpoints/{suffix}.json",
        expected_revision=pause_requested["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-checkpoint-{suffix}",
    ).value
    return store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="paused",
        expected_revision=checkpointed["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-paused-{suffix}",
    ).value


def _start_resumed_run(store: ControlStore, resumed: dict, *, suffix: str) -> dict:
    attempt_id = resumed["active_attempt_id"]
    release_id = f"hermes-{suffix}"
    generation_id = f"state-{suffix}"
    owner = f"worker-{suffix}"
    current = store.reserve_attempt_dispatch(
        run_id=resumed["id"],
        attempt_id=attempt_id,
        dispatch_owner=owner,
        runtime_release_id=release_id,
        state_generation_id=generation_id,
        runtime_slot_id=f"slot-{suffix}",
        runtime_artifact_digest=f"artifact-{suffix}",
        runtime_worker_protocol=f"protocol-{suffix}",
        expected_revision=resumed["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-reserve-{suffix}",
    ).value
    binding = store.create_runtime_binding(
        thread_id=current["thread_id"],
        adapter_id=f"hermes-{suffix}",
        runtime_session_ref=f"private-{suffix}",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key=f"coordinator-binding-{suffix}",
    ).value
    current = store.pin_attempt_runtime(
        run_id=current["id"],
        attempt_id=attempt_id,
        runtime_binding_id=binding["id"],
        runtime_release_id=release_id,
        state_generation_id=generation_id,
        dispatch_owner=owner,
        expected_revision=current["revision"],
        actor_id="runtime",
        idempotency_key=f"coordinator-pin-{suffix}",
    ).value
    for target in ("starting", "running"):
        current = store.apply_runtime_transition(
            run_id=current["id"],
            attempt_id=attempt_id,
            runtime_binding_id=binding["id"],
            runtime_release_id=release_id,
            state_generation_id=generation_id,
            target_state=target,
            expected_revision=current["revision"],
            actor_id="runtime",
            idempotency_key=f"coordinator-{target}-{suffix}",
        ).value
    return {
        **current,
        "_attempt_id": attempt_id,
        "_binding_id": binding["id"],
        "_release_id": release_id,
        "_generation_id": generation_id,
    }


def test_query_dispatch_commits_before_call_and_advances_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-test",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
            StageDefinition("finish", "control", ("lineage",)),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="Helios memory",
    )
    result = LineageQueryResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        nodes=(
            LineageNode(
                "lineage-echo",
                "graduated",
                "Echo memory",
                1,
                EngineReference("lineage", "idea:echo-memory"),
            ),
        ),
    )

    def prove_store_transaction_is_closed() -> None:
        with sqlite3.connect(store.path, timeout=0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()

    engine = DeterministicResearchEngine(
        responses={operation_id: result}, before_call=prove_store_transaction_is_closed
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"source_ids": ["source-echo"]},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    coordinator.recover(workflow_id=workflow["id"])
    recovered = store.get_workflow(workflow["id"])

    assert engine.calls == [(operation_id, 1)]
    assert recovered["stages"][0]["state"] == "completed"
    assert recovered["stages"][1]["state"] == "ready"
    assert recovered["current_stage_key"] == "finish"

    coordinator.recover(workflow_id=workflow["id"])
    assert engine.calls == [(operation_id, 1)]


def test_artifact_effect_dispatches_through_dedicated_port(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="artifact-port")
    definition = WorkflowDefinition(
        "research.coordinator-artifact",
        1,
        (
            StageDefinition(
                "publish",
                "artifact",
                (),
                ("artifact_living",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_attempt_operation_id(
        workflow["id"], "publish", "living", run["active_attempt_id"]
    )
    input_value = {"output_id": "runtime-output-living"}
    request = ArtifactWorkflowRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="publish",
        effect_kind="artifact_living",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
        plan_id="golden.living",
        plan_hash="3" * 64,
    )
    result = ArtifactWorkflowResult(
        operation_id=request.operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        stage_key=request.stage_key,
        effect_kind=request.effect_kind,
        artifact_version_ids=("artifact-version-missing",),
    )
    artifacts = RecordingArtifactPort(result)
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=artifacts,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("living", request),),
    )

    with pytest.raises(InvalidTransition, match="artifact_version_missing"):
        coordinator.recover(workflow_id=workflow["id"])
    assert artifacts.calls == [(operation_id, 1)]


def test_artifact_effect_unknown_outcome_uses_artifact_reconciliation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="artifact-reconcile")
    definition = WorkflowDefinition(
        "research.coordinator-artifact-reconcile",
        1,
        (
            StageDefinition(
                "publish",
                "artifact",
                (),
                ("artifact_living",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_attempt_operation_id(
        workflow["id"], "publish", "living", run["active_attempt_id"]
    )
    input_value = {"output_id": "runtime-output-living"}
    request = ArtifactWorkflowRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="publish",
        effect_kind="artifact_living",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
        plan_id="golden.living",
        plan_hash="3" * 64,
    )
    result = ArtifactWorkflowResult(
        operation_id=request.operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        stage_key=request.stage_key,
        effect_kind=request.effect_kind,
        artifact_version_ids=("artifact-version-missing",),
    )
    artifacts = RecoveringArtifactPort(result)
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=artifacts,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("living", request),),
    )

    coordinator.recover(workflow_id=workflow["id"])
    with pytest.raises(InvalidTransition, match="artifact_version_missing"):
        coordinator.recover(workflow_id=workflow["id"])
    assert artifacts.calls == [(operation_id, 1)]
    assert artifacts.reconciliation_calls == [2]


def test_decision_plan_is_restart_idempotent_and_resumes_through_coordinator(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-decision",
        1,
        (StageDefinition("choose", "decision", ()),),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    plan = WorkflowDecisionPlan(
        kind="lineage_strategy",
        prompt="Choose the lineage strategy",
        options=(
            {
                "id": "keep_both",
                "label": "Keep both",
                "description": "Preserve both prior branches.",
            },
            {
                "id": "create_successor",
                "label": "Create successor",
                "description": "Create one successor branch.",
            },
        ),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker",
    )

    waiting = coordinator.prepare_stage(
        workflow_id=workflow["id"], input_value={"lineage_nodes": 2}, decision=plan
    )
    restarted = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker-restarted",
    )
    replayed = restarted.prepare_stage(
        workflow_id=workflow["id"], input_value={"lineage_nodes": 2}, decision=plan
    )

    decisions = store.list_decisions()
    assert waiting["state"] == "waiting"
    assert replayed["state"] == "waiting"
    assert len(decisions) == 1
    resolved = store.resolve_decision(
        decision_id=decisions[0]["id"],
        choice="create_successor",
        expected_revision=decisions[0]["revision"],
        actor_id="local",
        idempotency_key="coordinator-decision-resolve-001",
    ).value
    assert resolved["state"] == "resolved"

    recovered = restarted.recover(workflow_id=workflow["id"])
    assert recovered["state"] == "completed"
    assert recovered["stages"][0]["state"] == "completed"


def test_lost_mutation_reply_reconciles_after_restart_without_redelivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-mutation-test",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
                checkpoint=True,
            ),
            StageDefinition("finish", "control", ("import",)),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    result = SourceImportResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        source_id="source-lingbot",
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )
    engine = DeterministicResearchEngine(
        responses={operation_id: result}, lose_reply_once={operation_id}
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    coordinator.recover(workflow_id=workflow["id"])
    assert store.list_workflow_effects(workflow_id=workflow["id"])[0]["state"] == (
        "outcome_unknown"
    )

    reopened = ControlStore(
        store.path,
        clock=lambda: datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        id_factory=DeterministicIds(),
    )
    reopened.initialize()
    restarted = WorkflowCoordinator(
        store=reopened,
        engine=engine,
        runtime=None,
        worker_id="flow-worker-restarted",
    )
    restarted.recover(workflow_id=workflow["id"])

    recovered = reopened.get_workflow(workflow["id"])
    assert engine.calls == [(operation_id, 1)]
    assert engine.reconciliation_calls == [(operation_id, 2)]
    assert recovered["stages"][0]["state"] == "completed"
    assert recovered["stages"][1]["state"] == "ready"


def test_expired_query_claim_is_safely_redelivered_with_a_new_epoch(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    store.initialize()
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-query-retry",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="Echo memory",
    )
    result = LineageQueryResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        nodes=(),
    )
    engine = DeterministicResearchEngine(
        responses={operation_id: result}, lose_reply_once={operation_id}
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
        lease_seconds=10,
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"source_ids": ["source-echo"]},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    coordinator.recover(workflow_id=workflow["id"])
    clock.advance(seconds=11)
    coordinator.recover(workflow_id=workflow["id"])

    assert engine.calls == [(operation_id, 1), (operation_id, 2)]
    assert engine.reconciliation_calls == []
    assert store.get_workflow(workflow["id"])["state"] == "completed"


def test_runtime_stage_uses_durable_receipt_and_reconciliation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _running_run(store)
    definition = WorkflowDefinition(
        "research.coordinator-runtime",
        1,
        (
            StageDefinition(
                "research_evidence",
                "runtime",
                (),
                required_receipts=("runtime_evidence",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"lineage_result_id": "a" * 64}
    operation_id = stable_operation_id(
        workflow["id"], "research_evidence", "primary"
    )
    runtime_event_id = _record_runtime_event(
        store, run, adapter_event_id=operation_id
    )
    request = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research_evidence",
        effect_kind="runtime_evidence",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
    )
    result = RuntimeStageResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        stage_key="research_evidence",
        effect_kind="runtime_evidence",
        attempt_id=run["_attempt_id"],
        runtime_event_id=runtime_event_id,
        output_id="runtime-output-evidence",
        output_hash="b" * 64,
    )
    def prove_store_transaction_is_closed() -> None:
        with sqlite3.connect(store.path, timeout=0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()

    runtime = DeterministicRuntime(
        responses={operation_id: result},
        before_call=prove_store_transaction_is_closed,
        lose_reply_once={operation_id},
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=runtime,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", request),),
    )

    coordinator.recover(workflow_id=workflow["id"])
    coordinator.recover(workflow_id=workflow["id"])

    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert effect["effect_kind"] == "runtime_evidence"
    assert effect["receipt"]["output_hash"] == "b" * 64
    assert runtime.calls == [(operation_id, 1)]
    assert runtime.reconciliation_calls == [(operation_id, 2)]
    assert store.get_workflow(workflow["id"])["state"] == "completed"
    with sqlite3.connect(store.path) as connection:
        runtime_references = connection.execute(
            """SELECT reference_id FROM workflow_stage_references
               WHERE workflow_id = ? AND reference_kind = 'runtime_event'""",
            (workflow["id"],),
        ).fetchall()
    assert runtime_references == [(runtime_event_id,)]


@pytest.mark.parametrize("event_source", ["missing", "other_run", "missing_inbox"])
def test_runtime_receipt_requires_an_event_from_its_current_run_attempt(
    tmp_path: Path, event_source: str
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="runtime-current")
    other_run = None
    if event_source == "other_run":
        other_run = _running_run(store, suffix="runtime-other")
    definition = WorkflowDefinition(
        "research.coordinator-runtime-current-attempt",
        1,
        (
            StageDefinition(
                "research_evidence",
                "runtime",
                (),
                required_receipts=("runtime_evidence",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"lineage_result_id": "a" * 64}
    operation_id = stable_operation_id(
        workflow["id"], "research_evidence", "primary"
    )
    if event_source == "missing":
        runtime_event_id = "event-missing-runtime"
    else:
        runtime_event_id = _record_runtime_event(
            store,
            other_run or run,
            suffix=event_source,
            adapter_event_id=operation_id,
        )
        if event_source == "missing_inbox":
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    """DELETE FROM runtime_event_inbox
                       WHERE attempt_id = ? AND adapter_event_id = ?""",
                    (run["_attempt_id"], operation_id),
                )
    request = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research_evidence",
        effect_kind="runtime_evidence",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
    )
    result = RuntimeStageResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        attempt_id=run["_attempt_id"],
        runtime_event_id=runtime_event_id,
        stage_key="research_evidence",
        effect_kind="runtime_evidence",
        output_id="runtime-output-evidence",
        output_hash="b" * 64,
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={operation_id: result}),
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", request),),
    )

    with pytest.raises(InvalidTransition, match="runtime_event"):
        coordinator.recover(workflow_id=workflow["id"])

    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert effect["state"] == "claimed"
    assert effect["receipt"] is None


def test_runtime_stage_completion_rechecks_the_current_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="runtime-retry")
    definition = WorkflowDefinition(
        "research.coordinator-runtime-retry",
        1,
        (
            StageDefinition(
                "research_evidence",
                "runtime",
                (),
                required_receipts=("runtime_evidence",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"lineage_result_id": "a" * 64}
    operation_id = stable_operation_id(
        workflow["id"], "research_evidence", "primary"
    )
    runtime_event_id = _record_runtime_event(
        store,
        run,
        suffix="before-retry",
        adapter_event_id=operation_id,
    )
    request = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research_evidence",
        effect_kind="runtime_evidence",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
    )
    result = RuntimeStageResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        attempt_id=run["_attempt_id"],
        runtime_event_id=runtime_event_id,
        stage_key="research_evidence",
        effect_kind="runtime_evidence",
        output_id="runtime-output-evidence",
        output_hash="b" * 64,
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={operation_id: result}),
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="flow-worker", lease_seconds=30
    )
    store.complete_workflow_effect(
        effect_id=effect["id"],
        worker_id="flow-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=result,
    )
    current_run = store.get_run(run["id"])
    failed = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="failed",
        expected_revision=current_run["revision"],
        actor_id="runtime",
        idempotency_key="coordinator-runtime-failed-before-stage-complete",
    ).value
    store.retry_run(
        run_id=run["id"],
        expected_revision=failed["revision"],
        actor_id="local",
        idempotency_key="coordinator-runtime-retry-before-stage-complete",
        reason="retry after runtime failure",
    )
    workflow = store.get_workflow(workflow["id"])
    stage = workflow["stages"][0]

    with pytest.raises(InvalidTransition, match="runtime_event_attempt_mismatch"):
        store.complete_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="research_evidence",
            expected_workflow_revision=workflow["revision"],
            expected_stage_revision=stage["revision"],
            references=(
                {
                    "kind": "runtime_event",
                    "id": runtime_event_id,
                    "metadata": {"effect_id": effect["id"]},
                },
            ),
        )


def test_runtime_operations_cannot_reuse_one_durable_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="runtime-event-reuse")
    definition = WorkflowDefinition(
        "research.coordinator-runtime-event-reuse",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_architecture", "runtime_evidence"),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"lineage_result_id": "a" * 64}
    input_hash = workflow_input_hash(input_value)
    evidence_operation = stable_operation_id(workflow["id"], "research", "evidence")
    architecture_operation = stable_operation_id(
        workflow["id"], "research", "architecture"
    )
    runtime_event_id = _record_runtime_event(
        store,
        run,
        suffix="event-reuse",
        adapter_event_id=evidence_operation,
    )
    requests = (
        RuntimeStageRequest(
            operation_id=evidence_operation,
            delivery_epoch=1,
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            stage_key="research",
            effect_kind="runtime_evidence",
            stage_input_hash=input_hash,
            dependency_result_ids=(),
        ),
        RuntimeStageRequest(
            operation_id=architecture_operation,
            delivery_epoch=1,
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            stage_key="research",
            effect_kind="runtime_architecture",
            stage_input_hash=input_hash,
            dependency_result_ids=(),
        ),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={}),
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(
            WorkflowEffectPlan("evidence", requests[0]),
            WorkflowEffectPlan("architecture", requests[1]),
        ),
    )
    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    for index, effect in enumerate(effects):
        claim = store.claim_workflow_effect(
            effect_id=effect["id"], worker_id="flow-worker", lease_seconds=30
        )
        result = RuntimeStageResult(
            operation_id=requests[index].operation_id,
            delivery_epoch=claim["delivery_epoch"],
            request_hash=requests[index].request_hash,
            attempt_id=run["_attempt_id"],
            runtime_event_id=runtime_event_id,
            stage_key="research",
            effect_kind=requests[index].effect_kind,
            output_id=f"runtime-output-{index}",
            output_hash=f"{index + 1:x}" * 64,
        )
        if index == 0:
            store.complete_workflow_effect(
                effect_id=effect["id"],
                worker_id="flow-worker",
                claim_epoch=claim["claim_epoch"],
                delivery_epoch=claim["delivery_epoch"],
                result=result,
            )
        else:
            with pytest.raises(
                InvalidTransition, match="runtime_event_causation_mismatch"
            ):
                store.complete_workflow_effect(
                    effect_id=effect["id"],
                    worker_id="flow-worker",
                    claim_epoch=claim["claim_epoch"],
                    delivery_epoch=claim["delivery_epoch"],
                    result=result,
                )


@pytest.mark.parametrize(
    ("stage_key", "stage_input_hash", "error"),
    [
        ("research_architecture", "input", "request_stage_mismatch"),
        ("research_evidence", "b" * 64, "request_input_mismatch"),
    ],
)
def test_runtime_stage_request_is_bound_to_its_active_stage(
    tmp_path: Path, stage_key: str, stage_input_hash: str, error: str
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-runtime-binding",
        1,
        (
            StageDefinition(
                "research_evidence",
                "runtime",
                (),
                required_receipts=("runtime_evidence",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"lineage_result_id": "a" * 64}
    operation_id = stable_operation_id(
        workflow["id"], "research_evidence", "primary"
    )
    request = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key=stage_key,
        effect_kind="runtime_evidence",
        stage_input_hash=(
            workflow_input_hash(input_value)
            if stage_input_hash == "input"
            else stage_input_hash
        ),
        dependency_result_ids=("a" * 64,),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={}),
        worker_id="flow-worker",
    )

    with pytest.raises(InvalidTransition, match=error):
        coordinator.prepare_stage(
            workflow_id=workflow["id"],
            input_value=input_value,
            effects=(WorkflowEffectPlan("primary", request),),
        )


@pytest.mark.parametrize(
    ("stage_effect", "request_kind"),
    [
        ("engine_query", "source_import"),
        ("engine_mutation", "lineage_query"),
        ("runtime", "source_import"),
        ("artifact", "source_import"),
        ("artifact", "runtime"),
    ],
)
def test_store_enforces_the_workflow_effect_stage_matrix(
    tmp_path: Path, stage_effect: str, request_kind: str
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    effect_kind = {
        "source_import": "source_import",
        "lineage_query": "lineage_query",
        "runtime": "runtime_evidence",
    }[request_kind]
    definition = WorkflowDefinition(
        f"research.effect-matrix-{stage_effect}-{request_kind}",
        1,
        (
            StageDefinition(
                "effect",
                stage_effect,
                (),
                required_results=(effect_kind,) if stage_effect == "engine_query" else (),
                required_receipts=(
                    (effect_kind,)
                    if stage_effect in {"engine_mutation", "runtime", "artifact"}
                    else ()
                ),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="effect",
        input_value={"case": request_kind},
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=workflow["stages"][0]["revision"],
    )
    operation_id = stable_operation_id(workflow["id"], "effect", "primary")
    if request_kind == "source_import":
        request = SourceImportRequest(
            operation_id=operation_id,
            delivery_epoch=1,
            source_id="source-lingbot",
            canonical_id="arxiv:2607.07675",
        )
    elif request_kind == "lineage_query":
        request = LineageQueryRequest(
            operation_id=operation_id,
            delivery_epoch=1,
            run_id=run["id"],
            source_ids=("source-echo",),
            query="Echo memory",
        )
    else:
        request = RuntimeStageRequest(
            operation_id=operation_id,
            delivery_epoch=1,
            run_id=run["id"],
            attempt_id=run["active_attempt_id"],
            stage_key="effect",
            effect_kind="runtime_evidence",
            stage_input_hash=active["stage"]["input_hash"],
            dependency_result_ids=(),
        )

    with pytest.raises(InvalidTransition, match="request_effect_mismatch"):
        store.create_workflow_effect(
            workflow_id=workflow["id"],
            stage_key="effect",
            effect_key="primary",
            request=request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )


def test_store_requires_effect_kind_after_the_stage_matrix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.effect-requirement",
        1,
        (
            StageDefinition(
                "effect",
                "engine_mutation",
                (),
                required_receipts=("source_binding",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="effect",
        input_value={"source_id": "source-lingbot"},
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=workflow["stages"][0]["revision"],
    )
    request = SourceImportRequest(
        operation_id=stable_operation_id(workflow["id"], "effect", "primary"),
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )

    with pytest.raises(InvalidTransition, match="effect_not_required"):
        store.create_workflow_effect(
            workflow_id=workflow["id"],
            stage_key="effect",
            effect_key="primary",
            request=request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )


@pytest.mark.parametrize(
    "dependency_result_ids",
    [(), ("c" * 64,), ("c" * 64, "d" * 64)],
)
def test_runtime_request_requires_exact_direct_dependency_results(
    tmp_path: Path, dependency_result_ids: tuple[str, ...]
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.runtime-dependencies",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
            StageDefinition(
                "runtime",
                "runtime",
                ("lineage",),
                required_receipts=("runtime_evidence",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    lineage_operation = stable_operation_id(workflow["id"], "lineage", "primary")
    lineage_request = LineageQueryRequest(
        operation_id=lineage_operation,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="Echo memory",
    )
    lineage_result = LineageQueryResult(
        operation_id=lineage_operation,
        delivery_epoch=1,
        request_hash=lineage_request.request_hash,
        nodes=(),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(
            responses={lineage_operation: lineage_result}
        ),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"source_ids": ["source-echo"]},
        effects=(WorkflowEffectPlan("primary", lineage_request),),
    )
    coordinator.recover(workflow_id=workflow["id"])
    workflow = store.get_workflow(workflow["id"])
    runtime_input = {"lineage_result_id": lineage_result.result_identity}
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="runtime",
        input_value=runtime_input,
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=workflow["stages"][1]["revision"],
    )
    operation_id = stable_operation_id(workflow["id"], "runtime", "primary")
    request = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="runtime",
        effect_kind="runtime_evidence",
        stage_input_hash=active["stage"]["input_hash"],
        dependency_result_ids=dependency_result_ids,
    )

    with pytest.raises(InvalidTransition, match="dependency_result_mismatch"):
        store.create_workflow_effect(
            workflow_id=workflow["id"],
            stage_key="runtime",
            effect_key="primary",
            request=request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )

    correct = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="runtime",
        effect_kind="runtime_evidence",
        stage_input_hash=active["stage"]["input_hash"],
        dependency_result_ids=(lineage_result.result_identity,),
    )
    created = store.create_workflow_effect(
        workflow_id=workflow["id"],
        stage_key="runtime",
        effect_key="primary",
        request=correct,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    assert created["state"] == "pending"


def test_external_commit_before_local_receipt_reconciles_stale_result(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    store.initialize()
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-stale-result",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    result = SourceImportResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        source_id="source-lingbot",
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )
    engine = DeterministicResearchEngine(
        responses={operation_id: result},
        before_call=lambda: clock.advance(seconds=11),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
        lease_seconds=10,
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    with pytest.raises(InvalidTransition, match="stale_claim"):
        coordinator.recover(workflow_id=workflow["id"])

    stale = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert stale["state"] == "claimed"
    assert stale["receipt"] is None
    coordinator.recover(workflow_id=workflow["id"])

    recovered = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert recovered["state"] == "completed"
    assert engine.calls == [(operation_id, 1)]
    assert engine.reconciliation_calls == [(operation_id, 2)]


def test_effect_plan_rejects_a_preincremented_delivery_epoch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-initial-epoch",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=2,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="Echo memory",
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker",
    )

    with pytest.raises(ValueError, match="initial delivery_epoch"):
        coordinator.prepare_stage(
            workflow_id=workflow["id"],
            input_value={"source_ids": ["source-echo"]},
            effects=(WorkflowEffectPlan("primary", request),),
        )


def test_prepare_after_workflow_completion_uses_a_sanitized_transition(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-completed",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="Echo memory",
    )
    result = LineageQueryResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        nodes=(),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={operation_id: result}),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"source_ids": ["source-echo"]},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    coordinator.recover(workflow_id=workflow["id"])

    with pytest.raises(InvalidTransition, match="completed"):
        coordinator.prepare_stage(
            workflow_id=workflow["id"],
            input_value={"source_ids": ["source-echo"]},
        )


def test_not_found_mutation_reconciliation_returns_to_safe_redelivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-not-found",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    result = SourceImportResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        source_id="source-lingbot",
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )
    engine = DeterministicResearchEngine(responses={operation_id: result})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="lost-worker", lease_seconds=30
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=effect["id"],
        worker_id="lost-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
    )

    coordinator.recover(workflow_id=workflow["id"])
    pending = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert pending["state"] == "pending"
    assert engine.reconciliation_calls == [(operation_id, 2)]
    assert engine.calls == []

    coordinator.recover(workflow_id=workflow["id"])
    assert engine.calls == [(operation_id, 3)]
    assert store.get_workflow(workflow["id"])["state"] == "completed"


def test_unknown_mutation_reconciliation_requires_manual_recovery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        "research.coordinator-manual-recovery",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    result = SourceImportResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        source_id="source-lingbot",
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )
    engine = DeterministicResearchEngine(
        responses={operation_id: result},
        unknown_reconciliation={operation_id},
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="lost-worker", lease_seconds=30
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=effect["id"],
        worker_id="lost-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
    )

    coordinator.recover(workflow_id=workflow["id"])
    coordinator.recover(workflow_id=workflow["id"])

    manual = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert manual["state"] == "manual_required"
    assert manual["failure_category"] == "outcome_unknown"
    assert engine.calls == []
    assert engine.reconciliation_calls == [(operation_id, 2)]


@pytest.mark.parametrize("terminal_state", ["cancel_requested", "failed", "canceled"])
def test_recovery_preserves_completed_effect_audit_on_fenced_run(
    tmp_path: Path, terminal_state: str
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    definition = WorkflowDefinition(
        f"research.coordinator-fenced-{terminal_state.replace('_', '-')}",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    result = SourceImportResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        source_id="source-lingbot",
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )
    engine = DeterministicResearchEngine(responses={operation_id: result})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="in-flight-worker", lease_seconds=30
    )
    store.complete_workflow_effect(
        effect_id=effect["id"],
        worker_id="in-flight-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=result,
    )
    if terminal_state == "cancel_requested":
        _cancel_run(store, run, key="cancel")
    elif terminal_state == "failed":
        _fail_unbound_run(store, run, key="fail")
    else:
        _cancel_run(store, run, key="cancel")
        _fail_unbound_run(store, run, key="cancel-complete")

    coordinator.recover(workflow_id=workflow["id"])

    assert store.get_run(run["id"])["state"] == terminal_state
    assert store.get_workflow(workflow["id"])["stages"][0]["state"] == "active"
    assert engine.calls == []


def test_permanent_effect_rejection_terminates_the_owned_lifecycle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="permanent-rejection")
    definition = WorkflowDefinition(
        "research.coordinator-permanent-rejection",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    engine = PermanentlyRejectingEngine(responses={})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    settled = coordinator.recover(workflow_id=workflow["id"])
    coordinator.recover(workflow_id=workflow["id"])

    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    terminal_run = store.get_run(run["id"])
    assert effect["state"] == "failed"
    assert effect["failure_category"] == "invalid_source"
    assert settled["state"] == "failed"
    assert settled["stages"][0]["state"] == "failed"
    assert terminal_run["state"] == "failed"
    assert store.get_attempt(run["active_attempt_id"])["state"] == "failed"
    assert store.get_thread(run["thread_id"])["status"] == "failed"
    assert engine.calls == [(operation_id, 1)]


def test_permanent_effect_rejection_is_claim_fenced(tmp_path: Path) -> None:
    clock = MutableClock()
    store = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    store.initialize()
    run = _run(store, suffix="stale-rejection")
    definition = WorkflowDefinition(
        "research.coordinator-stale-rejection",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    request = SourceImportRequest(
        operation_id=stable_operation_id(workflow["id"], "import", "primary"),
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="stale-worker", lease_seconds=10
    )
    clock.advance(seconds=11)

    with pytest.raises(InvalidTransition, match="stale_claim"):
        store.reject_workflow_effect(
            effect_id=effect["id"],
            worker_id="stale-worker",
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            failure_category="invalid_source",
        )

    assert store.list_workflow_effects(workflow_id=workflow["id"])[0][
        "state"
    ] == "claimed"
    assert store.get_workflow(workflow["id"])["state"] == "running"
    assert store.get_run(run["id"])["state"] == "queued"


def test_repeated_not_found_reconciliation_stops_safe_redelivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="redelivery-exhausted")
    definition = WorkflowDefinition(
        "research.coordinator-redelivery-exhausted",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    engine = AlwaysLostBeforeCommitEngine(responses={})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
        max_mutation_deliveries=2,
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    for _ in range(5):
        coordinator.recover(workflow_id=workflow["id"])

    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert effect["state"] == "failed"
    assert effect["failure_category"] == "redelivery_exhausted"
    assert store.get_workflow(workflow["id"])["state"] == "failed"
    assert store.get_run(run["id"])["state"] == "failed"
    assert engine.calls == [(operation_id, 1), (operation_id, 3)]
    assert engine.reconciliation_calls == [(operation_id, 2), (operation_id, 4)]


def test_reconciliation_lease_retries_do_not_consume_dispatch_attempts(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    store.initialize()
    run = _run(store, suffix="reconciliation-leases")
    definition = WorkflowDefinition(
        "research.coordinator-reconciliation-leases",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    engine = DeterministicResearchEngine(responses={})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=engine,
        runtime=None,
        worker_id="flow-worker",
        lease_seconds=10,
        max_mutation_deliveries=2,
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    dispatch = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="lost-worker", lease_seconds=10
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=effect["id"],
        worker_id="lost-worker",
        claim_epoch=dispatch["claim_epoch"],
        delivery_epoch=dispatch["delivery_epoch"],
    )
    for index in range(3):
        store.claim_workflow_effect_reconciliation(
            effect_id=effect["id"],
            worker_id=f"reconcile-worker-{index}",
            lease_seconds=10,
        )
        clock.advance(seconds=11)

    coordinator.recover(workflow_id=workflow["id"])

    pending = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert pending["state"] == "pending"
    assert pending["dispatch_attempt_count"] == 1
    assert store.get_workflow(workflow["id"])["state"] == "running"
    assert store.get_run(run["id"])["state"] == "queued"
    assert engine.reconciliation_calls == [(operation_id, 5)]


def test_recovery_preserves_completed_run_fencing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="completed-fence")
    definition = WorkflowDefinition(
        "research.coordinator-completed-fence",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    result = SourceImportResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        source_id="source-lingbot",
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={operation_id: result}),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="in-flight-worker", lease_seconds=30
    )
    store.complete_workflow_effect(
        effect_id=effect["id"],
        worker_id="in-flight-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=result,
    )
    current_run = store.get_run(run["id"])
    store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="completed",
        expected_revision=current_run["revision"],
        actor_id="runtime",
        idempotency_key="coordinator-completed-fence",
    )

    coordinator.recover(workflow_id=workflow["id"])

    assert store.get_run(run["id"])["state"] == "completed"
    assert store.get_workflow(workflow["id"])["stages"][0]["state"] == "active"


@pytest.mark.parametrize(
    "terminal_state", ["cancel_requested", "canceled", "completed", "failed"]
)
def test_late_permanent_rejection_preserves_the_run_terminal_authority(
    tmp_path: Path, terminal_state: str
) -> None:
    store = _store(tmp_path)
    run = (
        _running_run(store, suffix="late-rejection-completed")
        if terminal_state == "completed"
        else _run(store, suffix=f"late-rejection-{terminal_state}")
    )
    definition = WorkflowDefinition(
        f"research.coordinator-late-rejection-{terminal_state.replace('_', '-')}",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    request = SourceImportRequest(
        operation_id=stable_operation_id(workflow["id"], "import", "primary"),
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="late-worker", lease_seconds=30
    )
    if terminal_state == "cancel_requested":
        _cancel_run(store, run, key="late-rejection-cancel-request")
    elif terminal_state == "canceled":
        _cancel_run(store, run, key="late-rejection-cancel-request")
        _fail_unbound_run(store, run, key="late-rejection-canceled")
    elif terminal_state == "failed":
        _fail_unbound_run(store, run, key="late-rejection-failed")
    else:
        current_run = store.get_run(run["id"])
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            target_state="completed",
            expected_revision=current_run["revision"],
            actor_id="runtime",
            idempotency_key="coordinator-late-rejection-completed",
        )

    rejected = store.reject_workflow_effect(
        effect_id=effect["id"],
        worker_id="late-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        failure_category="invalid_source",
    )

    preserved = store.get_workflow(workflow["id"])
    assert rejected["state"] == "failed"
    assert rejected["failure_category"] == "invalid_source"
    assert preserved["state"] == "running"
    assert preserved["stages"][0]["state"] == "active"
    assert store.get_run(run["id"])["state"] == terminal_state


def test_missing_runtime_fails_before_claiming_a_runtime_effect(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="missing-runtime")
    definition = WorkflowDefinition(
        "research.coordinator-missing-runtime",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    request = RuntimeStageRequest(
        operation_id=stable_operation_id(workflow["id"], "research", "primary"),
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=workflow_input_hash({"question": "memory"}),
        dependency_result_ids=(),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"question": "memory"},
        effects=(WorkflowEffectPlan("primary", request),),
    )

    with pytest.raises(RuntimeError, match="runtime-stage adapter"):
        coordinator.recover(workflow_id=workflow["id"])

    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert effect["state"] == "pending"
    assert effect["claim_epoch"] == 0
    assert effect["delivery_epoch"] == 0
    assert effect["dispatch_attempt_count"] == 0


def test_concurrent_recovery_treats_a_lost_dispatch_claim_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="concurrent-dispatch")
    definition = WorkflowDefinition(
        "research.coordinator-concurrent-dispatch",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="memory",
    )
    result = LineageQueryResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        nodes=(),
    )
    engine = DeterministicResearchEngine(responses={operation_id: result})
    first = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker-a"
    )
    second = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker-b"
    )
    first.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"query": "memory"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    barrier = Barrier(2)
    classify = store.classify_workflow_recovery

    def synchronized_classification() -> list[dict]:
        value = classify()
        barrier.wait()
        return value

    monkeypatch.setattr(store, "classify_workflow_recovery", synchronized_classification)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(coordinator.recover, workflow_id=workflow["id"])
            for coordinator in (first, second)
        ]
        values = [future.result() for future in futures]

    assert {value["state"] for value in values} == {"completed"}
    assert engine.calls == [(operation_id, 1)]


def test_concurrent_recovery_treats_a_lost_reconciliation_claim_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="concurrent-reconciliation")
    definition = WorkflowDefinition(
        "research.coordinator-concurrent-reconciliation",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "import", "primary")
    request = SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    engine = DeterministicResearchEngine(responses={})
    first = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker-a"
    )
    second = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker-b"
    )
    first.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="lost-worker", lease_seconds=30
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=effect["id"],
        worker_id="lost-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
    )
    barrier = Barrier(2)
    classify = store.classify_workflow_recovery

    def synchronized_classification() -> list[dict]:
        value = classify()
        barrier.wait()
        return value

    monkeypatch.setattr(store, "classify_workflow_recovery", synchronized_classification)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(coordinator.recover, workflow_id=workflow["id"])
            for coordinator in (first, second)
        ]
        for future in futures:
            future.result()

    pending = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert pending["state"] == "pending"
    assert engine.reconciliation_calls == [(operation_id, 2)]


def test_concurrent_recovery_treats_a_lost_stage_cas_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="concurrent-finalize")
    definition = WorkflowDefinition(
        "research.coordinator-concurrent-finalize",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="memory",
    )
    result = LineageQueryResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        nodes=(),
    )
    engine = DeterministicResearchEngine(responses={operation_id: result})
    first = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker-a"
    )
    second = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker-b"
    )
    first.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"query": "memory"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="setup-worker", lease_seconds=30
    )
    store.complete_workflow_effect(
        effect_id=effect["id"],
        worker_id="setup-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=result,
    )
    barrier = Barrier(2)
    complete_stage = store.complete_workflow_stage

    def synchronized_completion(**kwargs: object) -> dict:
        barrier.wait()
        return complete_stage(**kwargs)

    monkeypatch.setattr(store, "complete_workflow_stage", synchronized_completion)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(coordinator.recover, workflow_id=workflow["id"])
            for coordinator in (first, second)
        ]
        values = [future.result() for future in futures]

    assert {value["state"] for value in values} == {"completed"}


def test_late_rejection_is_deferred_while_paused_and_settled_after_resume(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="paused-rejection")
    definition = WorkflowDefinition(
        "research.coordinator-paused-rejection",
        1,
        (
            StageDefinition(
                "import",
                "engine_mutation",
                (),
                required_receipts=("source_import",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    request = SourceImportRequest(
        operation_id=stable_operation_id(workflow["id"], "import", "primary"),
        delivery_epoch=1,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"canonical_id": "arxiv:2607.07675"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=effect["id"], worker_id="late-worker", lease_seconds=30
    )
    paused = _pause_run(store, run, suffix="paused-rejection")

    store.reject_workflow_effect(
        effect_id=effect["id"],
        worker_id="late-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        failure_category="invalid_source",
    )

    deferred = store.get_workflow(workflow["id"])
    assert store.get_run(run["id"])["state"] == "paused"
    assert deferred["state"] == "running"
    assert deferred["stages"][0]["state"] == "active"

    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key="coordinator-resume-paused-rejection",
    ).value
    settled = coordinator.recover(workflow_id=workflow["id"])

    assert settled["state"] == "failed"
    assert settled["stages"][0]["state"] == "failed"
    assert store.get_run(run["id"])["state"] == "failed"
    assert store.get_attempt(resumed["active_attempt_id"])["state"] == "failed"


def test_runtime_effect_rebinds_to_a_new_attempt_after_fencing_old_pending_work(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="runtime-rebind")
    definition = WorkflowDefinition(
        "research.coordinator-runtime-rebind",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"question": "memory"}
    input_hash = workflow_input_hash(input_value)
    old_operation_id = stable_operation_id(workflow["id"], "research", "primary")
    old_request = RuntimeStageRequest(
        operation_id=old_operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
    )
    runtime = DeterministicRuntime(responses={})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=runtime,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", old_request),),
    )
    paused = _pause_run(store, run, suffix="runtime-rebind")
    coordinator.recover(workflow_id=workflow["id"])
    paused_effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert paused_effect["state"] == "pending"
    assert runtime.calls == []
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key="coordinator-resume-runtime-rebind",
    ).value
    new_attempt_id = resumed["active_attempt_id"]
    coordinator.recover(workflow_id=workflow["id"])
    stale_effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert stale_effect["failure_category"] == "runtime_attempt_superseded"
    assert runtime.calls == []
    new_operation_id = stable_runtime_operation_id(
        workflow["id"],
        "research",
        "primary",
        new_attempt_id,
    )
    new_request = RuntimeStageRequest(
        operation_id=new_operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=new_attempt_id,
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
    )

    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", new_request),),
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", new_request),),
    )

    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    assert len(effects) == 2
    assert [effect["request"].attempt_id for effect in effects] == [
        run["_attempt_id"],
        new_attempt_id,
    ]
    assert effects[0]["state"] == "failed"
    assert effects[0]["failure_category"] == "runtime_attempt_superseded"
    assert effects[0]["dispatch_attempt_count"] == 0
    assert effects[1]["state"] == "pending"


def test_runtime_effect_reconciles_an_unknown_old_attempt_before_rebinding(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="runtime-reconcile-rebind")
    definition = WorkflowDefinition(
        "research.coordinator-runtime-reconcile-rebind",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"question": "memory"}
    input_hash = workflow_input_hash(input_value)
    old_operation_id = stable_operation_id(workflow["id"], "research", "primary")
    old_request = RuntimeStageRequest(
        operation_id=old_operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
    )
    runtime = DeterministicRuntime(responses={})
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=runtime,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", old_request),),
    )
    old_effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    claim = store.claim_workflow_effect(
        effect_id=old_effect["id"], worker_id="lost-worker", lease_seconds=30
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=old_effect["id"],
        worker_id="lost-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
    )
    paused = _pause_run(store, run, suffix="runtime-reconcile-rebind")
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key="coordinator-resume-runtime-reconcile-rebind",
    ).value
    new_attempt_id = resumed["active_attempt_id"]
    new_operation_id = stable_runtime_operation_id(
        workflow["id"],
        "research",
        "primary",
        new_attempt_id,
    )
    new_request = RuntimeStageRequest(
        operation_id=new_operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=new_attempt_id,
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
    )

    with pytest.raises(InvalidTransition, match="runtime_effect_unsettled"):
        coordinator.prepare_stage(
            workflow_id=workflow["id"],
            input_value=input_value,
            effects=(WorkflowEffectPlan("primary", new_request),),
        )

    coordinator.recover(workflow_id=workflow["id"])
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", new_request),),
    )

    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    assert runtime.reconciliation_calls == [(old_operation_id, 2)]
    assert [effect["state"] for effect in effects] == ["failed", "pending"]


def test_artifact_effect_rebinds_to_resumed_attempt_without_dispatching_old(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="artifact-rebind")
    definition = WorkflowDefinition(
        "research.coordinator-artifact-rebind",
        1,
        (
            StageDefinition(
                "publish",
                "artifact",
                (),
                required_receipts=("artifact_living",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"output_id": "living-output"}
    input_hash = workflow_input_hash(input_value)
    old_request = ArtifactWorkflowRequest(
        operation_id=stable_operation_id(workflow["id"], "publish", "living"),
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="publish",
        effect_kind="artifact_living",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
        plan_id="golden.living",
        plan_hash="3" * 64,
    )
    artifacts = RecordingArtifactPort(
        ArtifactWorkflowResult(
            operation_id=old_request.operation_id,
            delivery_epoch=1,
            request_hash=old_request.request_hash,
            attempt_id=old_request.attempt_id,
            stage_key=old_request.stage_key,
            effect_kind=old_request.effect_kind,
            artifact_version_ids=("artifact-version-missing",),
        )
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=artifacts,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("living", old_request),),
    )
    paused = _pause_run(store, run, suffix="artifact-rebind")
    coordinator.recover(workflow_id=workflow["id"])
    assert artifacts.calls == []
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key="coordinator-resume-artifact-rebind",
    ).value
    new_attempt_id = resumed["active_attempt_id"]

    coordinator.recover(workflow_id=workflow["id"])
    stale_effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert stale_effect["failure_category"] == "artifact_attempt_superseded"
    assert artifacts.calls == []

    new_request = replace(
        old_request,
        operation_id=stable_attempt_operation_id(
            workflow["id"], "publish", "living", new_attempt_id
        ),
        attempt_id=new_attempt_id,
    )

    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("living", new_request),),
    )
    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    assert [effect["request"].attempt_id for effect in effects] == [
        run["_attempt_id"],
        new_attempt_id,
    ]
    assert effects[0]["failure_category"] == "artifact_attempt_superseded"
    assert effects[1]["state"] == "pending"


def test_runtime_stage_finalizes_only_the_active_attempt_envelope(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="runtime-active-envelope")
    definition = WorkflowDefinition(
        "research.coordinator-runtime-active-envelope",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"question": "memory"}
    input_hash = workflow_input_hash(input_value)
    old_operation_id = stable_operation_id(workflow["id"], "research", "primary")
    old_event_id = _record_runtime_event(
        store, run, suffix="old-envelope", adapter_event_id=old_operation_id
    )
    old_request = RuntimeStageRequest(
        operation_id=old_operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
    )
    setup = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={}),
        worker_id="flow-worker",
    )
    setup.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", old_request),),
    )
    old_effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    old_claim = store.claim_workflow_effect(
        effect_id=old_effect["id"], worker_id="old-worker", lease_seconds=30
    )
    store.complete_workflow_effect(
        effect_id=old_effect["id"],
        worker_id="old-worker",
        claim_epoch=old_claim["claim_epoch"],
        delivery_epoch=old_claim["delivery_epoch"],
        result=RuntimeStageResult(
            operation_id=old_operation_id,
            delivery_epoch=old_claim["delivery_epoch"],
            request_hash=old_request.request_hash,
            attempt_id=run["_attempt_id"],
            runtime_event_id=old_event_id,
            stage_key="research",
            effect_kind="runtime_research",
            output_id="old-output",
            output_hash="a" * 64,
        ),
    )
    paused = _pause_run(store, run, suffix="runtime-active-envelope")
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key="coordinator-resume-runtime-active-envelope",
    ).value
    new_attempt_id = resumed["active_attempt_id"]
    new_operation_id = stable_runtime_operation_id(
        workflow["id"],
        "research",
        "primary",
        new_attempt_id,
    )
    new_request = RuntimeStageRequest(
        operation_id=new_operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=new_attempt_id,
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=input_hash,
        dependency_result_ids=(),
    )
    setup.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", new_request),),
    )
    active_run = _start_resumed_run(
        store, store.get_run(run["id"]), suffix="runtime-active-envelope-resumed"
    )
    new_event_id = _record_runtime_event(
        store,
        active_run,
        suffix="new-envelope",
        adapter_event_id=new_operation_id,
    )
    new_result = RuntimeStageResult(
        operation_id=new_operation_id,
        delivery_epoch=1,
        request_hash=new_request.request_hash,
        attempt_id=new_attempt_id,
        runtime_event_id=new_event_id,
        stage_key="research",
        effect_kind="runtime_research",
        output_id="new-output",
        output_hash="b" * 64,
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={new_operation_id: new_result}),
        worker_id="flow-worker",
    )

    settled = coordinator.recover(workflow_id=workflow["id"])

    assert settled["state"] == "completed"
    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    assert [effect["state"] for effect in effects] == ["completed", "completed"]
    with sqlite3.connect(store.path) as connection:
        references = connection.execute(
            """SELECT reference_id FROM workflow_stage_references
               WHERE workflow_id = ? AND reference_kind = 'runtime_event'""",
            (workflow["id"],),
        ).fetchall()
    assert references == [(new_event_id,)]


def test_runtime_prepare_replays_a_legacy_same_attempt_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="legacy-runtime-replay")
    definition = WorkflowDefinition(
        "research.coordinator-legacy-runtime-replay",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"question": "memory"}
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="research",
        input_value=input_value,
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=workflow["stages"][0]["revision"],
    )
    request = RuntimeStageRequest(
        operation_id=stable_operation_id(workflow["id"], "research", "primary"),
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
    )
    store.create_workflow_effect(
        workflow_id=workflow["id"],
        stage_key="research",
        effect_key="primary",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={}),
        worker_id="flow-worker",
    )

    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", request),),
    )

    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    assert len(effects) == 1
    assert effects[0]["effect_key"] == "primary"
    assert effects[0]["request"] == request


def test_recovery_uses_a_snapshot_from_before_effect_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="classification-snapshot")
    definition = WorkflowDefinition(
        "research.coordinator-classification-snapshot",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    operation_id = stable_operation_id(workflow["id"], "lineage", "primary")
    request = LineageQueryRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-echo",),
        query="memory",
    )
    result = LineageQueryResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        nodes=(),
    )
    engine = DeterministicResearchEngine(responses={operation_id: result})
    coordinator = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker"
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value={"query": "memory"},
        effects=(WorkflowEffectPlan("primary", request),),
    )
    advanced = False
    classify = store.classify_workflow_recovery

    def observed_classification() -> list[dict]:
        nonlocal advanced
        value = classify()
        if not advanced:
            advanced = True
            effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
            claim = store.claim_workflow_effect(
                effect_id=effect["id"],
                worker_id="competing-worker",
                lease_seconds=30,
            )
            store.complete_workflow_effect(
                effect_id=effect["id"],
                worker_id="competing-worker",
                claim_epoch=claim["claim_epoch"],
                delivery_epoch=claim["delivery_epoch"],
                result=result,
            )
        return value

    monkeypatch.setattr(store, "classify_workflow_recovery", observed_classification)

    settled = coordinator.recover(workflow_id=workflow["id"])

    assert settled["state"] == "completed"
    assert engine.calls == []


@pytest.mark.parametrize(
    "fenced_state", ["cancel_requested", "canceled", "completed", "failed"]
)
def test_runtime_supersede_does_not_mutate_a_fenced_run(
    tmp_path: Path, fenced_state: str
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix=f"supersede-{fenced_state}")
    definition = WorkflowDefinition(
        f"research.coordinator-supersede-{fenced_state.replace('_', '-')}",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"question": "memory"}
    request = RuntimeStageRequest(
        operation_id=stable_operation_id(workflow["id"], "research", "primary"),
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=DeterministicRuntime(responses={}),
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", request),),
    )
    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    paused = _pause_run(store, run, suffix=f"supersede-{fenced_state}")
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key=f"coordinator-resume-supersede-{fenced_state}",
    ).value
    replacement_attempt_id = resumed["active_attempt_id"]
    if fenced_state in {"cancel_requested", "canceled"}:
        current = store.transition_run(
            run_id=run["id"],
            target_state="cancel_requested",
            expected_revision=resumed["revision"],
            actor_id="local",
            idempotency_key=f"coordinator-cancel-supersede-{fenced_state}",
        ).value
        if fenced_state == "canceled":
            _fail_unbound_run(store, current, key="supersede-canceled")
    else:
        current = _start_resumed_run(
            store, resumed, suffix=f"supersede-{fenced_state}-resumed"
        )
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=current["_attempt_id"],
            runtime_binding_id=current["_binding_id"],
            runtime_release_id=current["_release_id"],
            state_generation_id=current["_generation_id"],
            target_state=fenced_state,
            expected_revision=current["revision"],
            actor_id="runtime",
            idempotency_key=f"coordinator-{fenced_state}-supersede",
        )
    before = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    with sqlite3.connect(store.path) as connection:
        audit_count = connection.execute(
            """SELECT COUNT(*) FROM control_audit
               WHERE aggregate_type = 'workflow_effect' AND aggregate_id = ?""",
            (effect["id"],),
        ).fetchone()[0]

    with pytest.raises(InvalidTransition, match=fenced_state):
        store.supersede_workflow_runtime_effect(
            effect_id=effect["id"],
            replacement_attempt_id=replacement_attempt_id,
        )

    after = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    with sqlite3.connect(store.path) as connection:
        after_audit_count = connection.execute(
            """SELECT COUNT(*) FROM control_audit
               WHERE aggregate_type = 'workflow_effect' AND aggregate_id = ?""",
            (effect["id"],),
        ).fetchone()[0]
    assert after == before
    assert after_audit_count == audit_count


def test_old_runtime_commit_reconciles_after_resume_without_closing_new_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _running_run(store, suffix="old-runtime-commit")
    definition = WorkflowDefinition(
        "research.coordinator-old-runtime-commit",
        1,
        (
            StageDefinition(
                "research",
                "runtime",
                (),
                required_receipts=("runtime_research",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    input_value = {"question": "memory"}
    operation_id = stable_operation_id(workflow["id"], "research", "primary")
    runtime_event_id = _record_runtime_event(
        store, run, suffix="old-runtime-commit", adapter_event_id=operation_id
    )
    request = RuntimeStageRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        stage_key="research",
        effect_kind="runtime_research",
        stage_input_hash=workflow_input_hash(input_value),
        dependency_result_ids=(),
    )
    result = RuntimeStageResult(
        operation_id=operation_id,
        delivery_epoch=1,
        request_hash=request.request_hash,
        attempt_id=run["_attempt_id"],
        runtime_event_id=runtime_event_id,
        stage_key="research",
        effect_kind="runtime_research",
        output_id="old-runtime-output",
        output_hash="c" * 64,
    )
    runtime = DeterministicRuntime(
        responses={operation_id: result}, lose_reply_once={operation_id}
    )
    coordinator = WorkflowCoordinator(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=runtime,
        worker_id="flow-worker",
    )
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", request),),
    )
    coordinator.recover(workflow_id=workflow["id"])
    paused = _pause_run(store, run, suffix="old-runtime-commit")
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key="coordinator-resume-old-runtime-commit",
    ).value

    recovered = coordinator.recover(workflow_id=workflow["id"])

    effect = store.list_workflow_effects(workflow_id=workflow["id"])[0]
    assert effect["state"] == "completed"
    assert effect["receipt"]["attempt_id"] == run["_attempt_id"]
    assert runtime.reconciliation_calls == [(operation_id, 2)]
    assert store.get_run(run["id"])["active_attempt_id"] == resumed["active_attempt_id"]
    assert recovered["state"] == "running"
    assert recovered["stages"][0]["state"] == "active"
    with sqlite3.connect(store.path) as connection:
        references = connection.execute(
            """SELECT COUNT(*) FROM workflow_stage_references
               WHERE workflow_id = ? AND reference_kind = 'runtime_event'""",
            (workflow["id"],),
        ).fetchone()[0]
    assert references == 0


def test_recovery_defers_an_effect_created_after_its_baseline_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store, suffix="late-effect")
    definition = WorkflowDefinition(
        "research.coordinator-late-effect",
        1,
        (
            StageDefinition(
                "lineage",
                "engine_query",
                (),
                required_results=("lineage_query",),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    requests: dict[str, LineageQueryRequest] = {}
    responses: dict[str, LineageQueryResult] = {}
    for effect_key in ("primary", "secondary"):
        operation_id = stable_operation_id(workflow["id"], "lineage", effect_key)
        request = LineageQueryRequest(
            operation_id=operation_id,
            delivery_epoch=1,
            run_id=run["id"],
            source_ids=("source-echo",),
            query=f"memory {effect_key}",
        )
        requests[effect_key] = request
        responses[operation_id] = LineageQueryResult(
            operation_id=operation_id,
            delivery_epoch=1,
            request_hash=request.request_hash,
            nodes=(),
        )
    engine = DeterministicResearchEngine(responses=responses)
    coordinator = WorkflowCoordinator(
        store=store, engine=engine, runtime=None, worker_id="flow-worker"
    )
    input_value = {"query": "memory"}
    coordinator.prepare_stage(
        workflow_id=workflow["id"],
        input_value=input_value,
        effects=(WorkflowEffectPlan("primary", requests["primary"]),),
    )
    classify = store.classify_workflow_recovery
    added = False

    def add_effect_before_classification() -> list[dict]:
        nonlocal added
        if not added:
            added = True
            coordinator.prepare_stage(
                workflow_id=workflow["id"],
                input_value=input_value,
                effects=(WorkflowEffectPlan("secondary", requests["secondary"]),),
            )
        return classify()

    monkeypatch.setattr(
        store, "classify_workflow_recovery", add_effect_before_classification
    )

    first = coordinator.recover(workflow_id=workflow["id"])

    assert first["state"] == "running"
    assert engine.calls == [(requests["primary"].operation_id, 1)]
    effects = store.list_workflow_effects(workflow_id=workflow["id"])
    assert [effect["state"] for effect in effects] == ["completed", "pending"]

    second = coordinator.recover(workflow_id=workflow["id"])

    assert second["state"] == "completed"
    assert engine.calls == [
        (requests["primary"].operation_id, 1),
        (requests["secondary"].operation_id, 1),
    ]
