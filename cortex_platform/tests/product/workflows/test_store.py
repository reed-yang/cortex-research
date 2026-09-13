from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    InvalidTransition,
    RevisionConflict,
)
from cortex_platform.product.sources import CandidateObservation
from cortex_platform.product.workflows import (
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectReconciliationResult,
    EngineReference,
    LineageQueryRequest,
    SourceBindingRequest,
    SourceImportRequest,
    SourceImportResult,
    StageDefinition,
    SuccessorCreationRequest,
    WorkflowDefinition,
)


class DeterministicIds:
    def __init__(self, *, fail_kind: str | None = None, fail_number: int = 0) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._fail_kind = fail_kind
        self._fail_number = fail_number

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        if kind == self._fail_kind and self._counts[kind] == self._fail_number:
            raise RuntimeError("injected workflow fault")
        return f"{kind}-{self._counts[kind]}"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db",
        clock=clock,
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value


def _key(value: str) -> str:
    return f"workflow-{value:0>8}"


def _run(store: ControlStore) -> dict:
    workspace = store.create_workspace(
        title="Research", actor_id="local", idempotency_key=_key("workspace")
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Durable workflow",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key("thread"),
    ).value
    return store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("run"),
    ).value


def _cancel_run(store: ControlStore, run: dict, *, key: str = "cancel") -> dict:
    return store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key=_key(key),
    ).value


def _definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "research.store-test",
        1,
        (
            StageDefinition("prepare", "control", ()),
            StageDefinition("choose", "decision", ("prepare",)),
            StageDefinition(
                "import",
                "engine_mutation",
                ("choose",),
                ("source_import",),
                checkpoint=True,
            ),
            StageDefinition(
                "lineage",
                "engine_query",
                ("import",),
                required_results=("lineage_query",),
            ),
        ),
    )


def _decision_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "research.decision-test",
        1,
        (StageDefinition("choose", "decision", ()),),
    )


def _effect_definition(*, query: bool = False) -> WorkflowDefinition:
    stage = (
        StageDefinition(
            "effect",
            "engine_query",
            (),
            required_results=("lineage_query",),
        )
        if query
        else StageDefinition(
            "effect", "engine_mutation", (), ("source_import",), checkpoint=True
        )
    )
    return WorkflowDefinition("research.effect-test", 1, (stage,))


def _advance_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "research.advance-test",
        1,
        (
            StageDefinition(
                "effect",
                "engine_mutation",
                (),
                ("source_import",),
                checkpoint=True,
            ),
            StageDefinition("finish", "control", ("effect",)),
        ),
    )


def _active_effect(store: ControlStore, *, query: bool = False) -> tuple[dict, dict]:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_effect_definition(query=query)
    )
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="effect",
        input_value={"run_id": run["id"]},
        expected_workflow_revision=0,
        expected_stage_revision=0,
    )
    return run, active


def _active_artifact_effect(
    store: ControlStore, *, effect_kind: str = "artifact_living"
) -> tuple[dict, dict]:
    run = _run(store)
    definition = WorkflowDefinition(
        "research.artifact-effect-test",
        1,
        (
            StageDefinition(
                "publish",
                "artifact",
                (),
                (effect_kind,),
            ),
        ),
    )
    workflow = store.install_workflow(run_id=run["id"], definition=definition)
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="publish",
        input_value={"run_id": run["id"]},
        expected_workflow_revision=0,
        expected_stage_revision=0,
    )
    return run, active


def _artifact_request(
    run: dict,
    active: dict,
    *,
    effect_kind: str = "artifact_living",
    snapshot_member_version_ids: tuple[str, ...] = (),
) -> ArtifactWorkflowRequest:
    return ArtifactWorkflowRequest(
        operation_id="operation-artifact-living",
        delivery_epoch=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        stage_key="publish",
        effect_kind=effect_kind,
        stage_input_hash=active["stage"]["input_hash"],
        dependency_result_ids=(),
        plan_id=f"golden.{effect_kind.removeprefix('artifact_')}",
        plan_hash="3" * 64,
        snapshot_member_version_ids=snapshot_member_version_ids,
    )


def _committed_artifact_version(store: ControlStore, run: dict) -> dict:
    source = store.register_source(
        authority="arxiv",
        authority_id="2606.04527",
        source_kind="paper",
        official_title="Echo-Infinity",
        engine_ref="paper:echo-existing",
        aliases=(),
        actor_id="fixture",
        idempotency_key=_key("register-source"),
    ).value
    candidate = CandidateObservation(
        claim_kind="title",
        authority="arxiv",
        authority_id="2606.04527",
        official_title="Echo-Infinity",
    )
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        title="Echo-Infinity",
        locator=None,
        candidates=(candidate.to_record(),),
        actor_id="fixture",
        idempotency_key=_key("source-intent"),
    ).value
    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="use_source",
        expected_revision=intent["revision"],
        actor_id="fixture",
        idempotency_key=_key("source-resolution"),
    )
    thread = store.get_thread(run["thread_id"])
    artifact = store.create_artifact(
        workspace_id=thread["workspace_id"],
        thread_id=thread["id"],
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        kind="living-brief",
        title="Echo living brief",
        actor_id="fixture",
        idempotency_key=_key("artifact-create"),
    ).value
    content = b"# Echo living brief\n"
    reservation = store.request_artifact_version(
        artifact_id=artifact["id"],
        logical_version=1,
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        source_ids=(source["id"],),
        research_engine_refs=("paper:echo-existing",),
        generator={"name": "cortex-research", "version": "0.1.0"},
        tool={"name": "artifact-writer", "version": "1.0.0"},
        parents=(),
        root_id="artifacts",
        relative_path="golden/living-brief.md",
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="text/markdown",
        advance_head=True,
        expected_head_revision=0,
        actor_id="fixture",
        idempotency_key=_key("artifact-version"),
    ).value
    action = reservation["materialization_action"]
    claim = store.claim_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-materializer",
        lease_seconds=30,
        actor_id="fixture",
        idempotency_key=_key("artifact-claim"),
    ).value
    return store.complete_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-materializer",
        claim_epoch=claim["claim_epoch"],
        materialized_result={
            "schema_version": 1,
            "operation_id": action["operation_id"],
            "root_id": action["root_id"],
            "relative_path": action["relative_path"],
            "sha256": reservation["sha256"],
            "byte_length": reservation["byte_length"],
            "media_type": reservation["media_type"],
            "parents": reservation["parents"],
            "replayed": False,
            "recovered_from": None,
        },
        actor_id="fixture",
        idempotency_key=_key("artifact-complete"),
    ).value


def _import_request(
    *, operation_id: str = "operation-import", source_id: str = "source-1"
) -> SourceImportRequest:
    return SourceImportRequest(
        operation_id=operation_id,
        delivery_epoch=1,
        source_id=source_id,
        canonical_id="arxiv:2607.07675",
    )


def _import_result(
    request: SourceImportRequest, *, source_id: str | None = None
) -> SourceImportResult:
    return SourceImportResult(
        operation_id=request.operation_id,
        delivery_epoch=request.delivery_epoch,
        request_hash=request.request_hash,
        source_id=source_id or request.source_id,
        engine_reference=EngineReference("source", "paper:lingbot-video"),
        manifest={"source_rows": 1, "chunks": 2, "directories": 1},
    )


def _hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def test_install_workflow_seals_definition_and_rolls_back_on_fault(
    tmp_path: Path, clock: MutableClock
) -> None:
    failing = ControlStore(
        tmp_path / "control.db",
        clock=clock,
        id_factory=DeterministicIds(fail_kind="workflow_stage", fail_number=2),
    )
    failing.initialize()
    run = _run(failing)

    with pytest.raises(RuntimeError, match="injected workflow fault"):
        failing.install_workflow(run_id=run["id"], definition=_definition())

    with sqlite3.connect(failing.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM workflow_instances").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM workflow_stage_instances").fetchone()[0]
            == 0
        )

    reopened = ControlStore(
        failing.path,
        clock=clock,
        id_factory=DeterministicIds(),
    )
    reopened.initialize()
    workflow = reopened.install_workflow(run_id=run["id"], definition=_definition())

    assert workflow["definition_sealed"] is True
    assert workflow["current_stage_key"] == "prepare"
    assert [stage["state"] for stage in workflow["stages"]] == [
        "ready",
        "pending",
        "pending",
        "pending",
    ]


def test_install_workflow_exactly_replays_and_rejects_definition_drift(
    store: ControlStore,
) -> None:
    run = _run(store)
    installed = store.install_workflow(run_id=run["id"], definition=_definition())

    assert (
        store.install_workflow(run_id=run["id"], definition=_definition()) == installed
    )
    with pytest.raises(InvalidTransition, match="definition_drift"):
        store.install_workflow(
            run_id=run["id"],
            definition=WorkflowDefinition(
                "research.store-test",
                2,
                (StageDefinition("prepare", "control", ()),),
            ),
        )


def test_cancel_requested_fences_first_workflow_install(store: ControlStore) -> None:
    run = _run(store)
    _cancel_run(store, run)

    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.install_workflow(run_id=run["id"], definition=_definition())


def test_reopen_reads_exact_installed_workflow(
    store: ControlStore, clock: MutableClock
) -> None:
    run = _run(store)
    installed = store.install_workflow(run_id=run["id"], definition=_definition())
    reopened = ControlStore(store.path, clock=clock, id_factory=DeterministicIds())
    reopened.initialize()

    assert reopened.get_workflow_for_run(run["id"]) == installed
    assert reopened.get_workflow(installed["id"]) == installed


def test_stage_activation_fences_dependencies_revisions_and_hashes_input(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(run_id=run["id"], definition=_definition())
    prepare, choose = workflow["stages"][:2]

    with pytest.raises(InvalidTransition, match="dependencies_incomplete"):
        store.activate_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="choose",
            input_value={"source": "early"},
            expected_workflow_revision=workflow["revision"],
            expected_stage_revision=choose["revision"],
        )

    activated = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="prepare",
        input_value={"query": "echo", "sources": ["source-1"]},
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=prepare["revision"],
    )

    assert activated["workflow"]["revision"] == workflow["revision"] + 1
    assert activated["stage"]["revision"] == prepare["revision"] + 1
    assert activated["stage"]["attempt"] == 1
    assert activated["stage"]["input_hash"] == _hash(
        {"query": "echo", "sources": ["source-1"]}
    )
    with pytest.raises(RevisionConflict):
        store.activate_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="prepare",
            input_value={"query": "echo", "sources": ["source-1"]},
            expected_workflow_revision=workflow["revision"],
            expected_stage_revision=prepare["revision"],
        )


def test_cancel_requested_fences_workflow_stage_activation(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(run_id=run["id"], definition=_definition())
    _cancel_run(store, run)

    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.activate_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="prepare",
            input_value={"query": "echo"},
            expected_workflow_revision=workflow["revision"],
            expected_stage_revision=workflow["stages"][0]["revision"],
        )


def test_workflow_decision_is_atomically_attached_without_runtime_binding(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_decision_definition()
    )
    activated = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="choose",
        input_value={"candidates": ["keep", "replace"]},
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=workflow["stages"][0]["revision"],
    )

    created = store.create_workflow_decision(
        workflow_id=workflow["id"],
        stage_key="choose",
        expected_workflow_revision=activated["workflow"]["revision"],
        expected_stage_revision=activated["stage"]["revision"],
        kind="source-selection",
        prompt="Choose source",
        options=({"id": "keep", "label": "Keep"}, {"id": "replace"}),
    )

    assert created["decision"]["state"] == "pending"
    assert created["decision_ref"]["state"] == "pending"
    assert created["decision_ref"]["decision_id"] == created["decision"]["id"]
    assert created["stage"]["state"] == "waiting"
    assert store.get_run(run["id"])["state"] == "waiting_for_decision"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_actions").fetchone()[0] == 0


def test_cancel_requested_fences_workflow_decision_creation(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_decision_definition()
    )
    activated = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="choose",
        input_value={"candidates": ["keep"]},
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=workflow["stages"][0]["revision"],
    )
    _cancel_run(store, run)

    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.create_workflow_decision(
            workflow_id=workflow["id"],
            stage_key="choose",
            expected_workflow_revision=activated["workflow"]["revision"],
            expected_stage_revision=activated["stage"]["revision"],
            kind="source-selection",
            prompt="Choose source",
            options=({"id": "keep"},),
        )

    assert store.get_run(run["id"])["state"] == "cancel_requested"


def test_public_resolution_recovers_workflow_without_runtime_action(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_decision_definition()
    )
    activated = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="choose",
        input_value={"candidates": ["keep"]},
        expected_workflow_revision=0,
        expected_stage_revision=0,
    )
    created = store.create_workflow_decision(
        workflow_id=workflow["id"],
        stage_key="choose",
        expected_workflow_revision=activated["workflow"]["revision"],
        expected_stage_revision=activated["stage"]["revision"],
        kind="source-selection",
        prompt="Choose source",
        options=({"id": "keep"},),
    )

    resolved = store.resolve_decision(
        decision_id=created["decision"]["id"],
        choice="keep",
        expected_revision=0,
        actor_id="local",
        idempotency_key=_key("resolve-workflow"),
    ).value

    assert resolved["state"] == "resolved"
    assert "runtime_action_id" not in resolved
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        decision_ref = conn.execute(
            "SELECT * FROM workflow_decision_refs WHERE decision_id = ?",
            (resolved["id"],),
        ).fetchone()
        attempt = conn.execute(
            "SELECT state FROM attempts WHERE id = ?", (run["attempt"]["id"],)
        ).fetchone()
        assert dict(decision_ref)["state"] == "resolved"
        assert dict(decision_ref)["selected_choice"] == "keep"
        assert attempt["state"] == "resuming"
        assert conn.execute("SELECT COUNT(*) FROM runtime_actions").fetchone()[0] == 0
    recovered = store.get_workflow(workflow["id"])
    assert recovered["state"] == "running"
    assert recovered["stages"][0]["state"] == "active"
    assert store.get_run(run["id"])["state"] == "resuming"


def test_cancel_requested_fences_workflow_decision_resolution(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_decision_definition()
    )
    activated = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="choose",
        input_value={"candidates": ["keep"]},
        expected_workflow_revision=0,
        expected_stage_revision=0,
    )
    created = store.create_workflow_decision(
        workflow_id=workflow["id"],
        stage_key="choose",
        expected_workflow_revision=activated["workflow"]["revision"],
        expected_stage_revision=activated["stage"]["revision"],
        kind="source-selection",
        prompt="Choose source",
        options=({"id": "keep"},),
    )
    waiting_run = store.get_run(run["id"])
    _cancel_run(store, waiting_run)

    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.resolve_decision(
            decision_id=created["decision"]["id"],
            choice="keep",
            expected_revision=created["decision"]["revision"],
            actor_id="local",
            idempotency_key=_key("resolve-canceled-workflow"),
        )

    assert store.get_workflow(workflow["id"])["state"] == "waiting"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state FROM workflow_decision_refs WHERE decision_id = ?",
            (created["decision"]["id"],),
        ).fetchone()[0] == "pending"


def test_resolution_repairs_resolved_decision_before_pending_workflow_ref(
    store: ControlStore,
) -> None:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_decision_definition()
    )
    activated = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="choose",
        input_value={"candidates": ["keep"]},
        expected_workflow_revision=0,
        expected_stage_revision=0,
    )
    created = store.create_workflow_decision(
        workflow_id=workflow["id"],
        stage_key="choose",
        expected_workflow_revision=activated["workflow"]["revision"],
        expected_stage_revision=activated["stage"]["revision"],
        kind="source-selection",
        prompt="Choose source",
        options=({"id": "keep"},),
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """UPDATE decisions SET state = 'resolved', resolution_json = ?,
                       revision = 1, resolved_at = ? WHERE id = ?""",
            (
                json.dumps({"choice": "keep", "actor_id": "local"}),
                "2026-07-27T12:00:00Z",
                created["decision"]["id"],
            ),
        )

    repaired = store.resolve_decision(
        decision_id=created["decision"]["id"],
        choice="keep",
        expected_revision=1,
        actor_id="local",
        idempotency_key=_key("repair-workflow"),
    ).value

    assert repaired["state"] == "resolved"
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute(
                "SELECT state FROM workflow_decision_refs WHERE decision_id = ?",
                (repaired["id"],),
            ).fetchone()[0]
            == "resolved"
        )


def test_effect_identity_is_stable_and_conflicting_replay_is_rejected(
    store: ControlStore,
) -> None:
    _, active = _active_effect(store)
    request = _import_request()
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )

    assert (
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="primary",
            request=request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )
        == command
    )
    with pytest.raises(InvalidTransition, match="effect_identity_drift"):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="primary",
            request=_import_request(operation_id="operation-conflict"),
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )
    with pytest.raises(InvalidTransition, match="effect_identity_drift"):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="alternate",
            request=request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )


def test_artifact_effect_is_persisted_but_requires_committed_owned_versions(
    store: ControlStore,
) -> None:
    run, active = _active_artifact_effect(store)
    request = _artifact_request(run, active)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="publish",
        effect_key="living",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    assert command["effect_kind"] == "artifact_living"
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="artifact-worker", lease_seconds=30
    )
    delivered = replace(request, delivery_epoch=claim["delivery_epoch"])

    with pytest.raises(InvalidTransition, match="artifact_version_missing"):
        store.complete_workflow_effect(
            effect_id=command["id"],
            worker_id="artifact-worker",
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            result=ArtifactWorkflowResult(
                operation_id=delivered.operation_id,
                delivery_epoch=delivered.delivery_epoch,
                request_hash=delivered.request_hash,
                attempt_id=delivered.attempt_id,
                stage_key=delivered.stage_key,
                effect_kind=delivered.effect_kind,
                artifact_version_ids=("artifact-version-missing",),
            ),
        )

    version = _committed_artifact_version(store, run)
    completed = store.complete_workflow_effect(
        effect_id=command["id"],
        worker_id="artifact-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=ArtifactWorkflowResult(
            operation_id=delivered.operation_id,
            delivery_epoch=delivered.delivery_epoch,
            request_hash=delivered.request_hash,
            attempt_id=delivered.attempt_id,
            stage_key=delivered.stage_key,
            effect_kind=delivered.effect_kind,
            artifact_version_ids=(version["id"],),
        ),
    )
    assert completed["receipt"]["artifact_version_ids"] == [version["id"]]

    with pytest.raises(InvalidTransition, match="references_extra"):
        store.complete_workflow_stage(
            workflow_id=active["workflow"]["id"],
            stage_key="publish",
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            references=(
                {
                    "kind": "artifact_version",
                    "id": version["id"],
                    "metadata": {"effect_id": command["id"]},
                },
                {
                    "kind": "snapshot",
                    "id": "snapshot-unrelated",
                    "metadata": {"context": True},
                },
            ),
        )
    unrelated_event_id = store.list_run_events(run["id"])[0]["id"]
    with pytest.raises(InvalidTransition, match="references_extra"):
        store.complete_workflow_stage(
            workflow_id=active["workflow"]["id"],
            stage_key="publish",
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            references=(
                {
                    "kind": "artifact_version",
                    "id": version["id"],
                    "metadata": {"effect_id": command["id"]},
                },
                {
                    "kind": "event",
                    "id": unrelated_event_id,
                    "metadata": {"context": True},
                },
            ),
        )

    advanced = store.complete_workflow_stage(
        workflow_id=active["workflow"]["id"],
        stage_key="publish",
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        references=(
            {
                "kind": "artifact_version",
                "id": version["id"],
                "metadata": {"effect_id": command["id"]},
            },
        ),
    )
    assert advanced["workflow"]["state"] == "completed"


def test_artifact_snapshot_receipt_is_bound_to_exact_committed_members(
    store: ControlStore,
) -> None:
    run, active = _active_artifact_effect(
        store, effect_kind="artifact_snapshot"
    )
    version = _committed_artifact_version(store, run)
    thread = store.get_thread(run["thread_id"])
    other_thread = store.create_thread(
        workspace_id=thread["workspace_id"],
        title="Other run",
        expected_revision=store.get_workspace(thread["workspace_id"])["revision"],
        actor_id="fixture",
        idempotency_key=_key("other-thread"),
    ).value
    other_run = store.create_run(
        thread_id=other_thread["id"],
        expected_revision=other_thread["revision"],
        actor_id="fixture",
        idempotency_key=_key("other-run"),
    ).value
    foreign_snapshot = store.create_artifact_snapshot(
        workspace_id=thread["workspace_id"],
        run_id=other_run["id"],
        attempt_id=other_run["active_attempt_id"],
        name="Foreign snapshot",
        artifact_version_ids=(version["id"],),
        actor_id="fixture",
        idempotency_key=_key("foreign-snapshot"),
    ).value
    snapshot = store.create_artifact_snapshot(
        workspace_id=thread["workspace_id"],
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        name="Golden snapshot",
        artifact_version_ids=(version["id"],),
        actor_id="fixture",
        idempotency_key=_key("artifact-snapshot"),
    ).value
    request = _artifact_request(
        run,
        active,
        effect_kind="artifact_snapshot",
        snapshot_member_version_ids=(version["id"],),
    )
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="publish",
        effect_key="snapshot",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="artifact-worker", lease_seconds=30
    )
    delivered = replace(request, delivery_epoch=claim["delivery_epoch"])
    result = ArtifactWorkflowResult(
        operation_id=delivered.operation_id,
        delivery_epoch=delivered.delivery_epoch,
        request_hash=delivered.request_hash,
        attempt_id=delivered.attempt_id,
        stage_key=delivered.stage_key,
        effect_kind=delivered.effect_kind,
        artifact_version_ids=(version["id"],),
        snapshot_id=snapshot["id"],
    )
    with pytest.raises(
        InvalidTransition, match="artifact_snapshot_identity_mismatch"
    ):
        store.complete_workflow_effect(
            effect_id=command["id"],
            worker_id="artifact-worker",
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            result=replace(result, snapshot_id=foreign_snapshot["id"]),
        )
    with pytest.raises(ValueError, match="requested snapshot members"):
        store.complete_workflow_effect(
            effect_id=command["id"],
            worker_id="artifact-worker",
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            result=replace(
                result,
                artifact_version_ids=("artifact-version-unrequested",),
            ),
        )
    store.complete_workflow_effect(
        effect_id=command["id"],
        worker_id="artifact-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=result,
    )
    completed = store.complete_workflow_stage(
        workflow_id=active["workflow"]["id"],
        stage_key="publish",
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        references=(
            {
                "kind": "artifact_version",
                "id": version["id"],
                "metadata": {"effect_id": command["id"]},
            },
            {
                "kind": "snapshot",
                "id": snapshot["id"],
                "metadata": {"effect_id": command["id"]},
            },
        ),
    )
    assert completed["workflow"]["state"] == "completed"


def test_artifact_effect_reconciliation_commits_only_durable_owned_outcome(
    store: ControlStore,
) -> None:
    run, active = _active_artifact_effect(store)
    request = _artifact_request(run, active)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="publish",
        effect_key="living",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="artifact-worker", lease_seconds=30
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=command["id"],
        worker_id="artifact-worker",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
    )
    version = _committed_artifact_version(store, run)
    reconciliation = store.claim_workflow_effect_reconciliation(
        effect_id=command["id"], worker_id="artifact-worker", lease_seconds=30
    )
    committed = ArtifactWorkflowResult(
        operation_id=request.operation_id,
        delivery_epoch=claim["delivery_epoch"],
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        stage_key=request.stage_key,
        effect_kind=request.effect_kind,
        artifact_version_ids=(version["id"],),
    )
    result = EffectReconciliationResult(
        domain=request.DOMAIN,
        operation_id=request.operation_id,
        request_hash=request.request_hash,
        delivery_epoch=reconciliation["delivery_epoch"],
        disposition="committed",
        result=committed,
    )
    completed = store.complete_workflow_effect_reconciliation(
        effect_id=command["id"],
        worker_id="artifact-worker",
        claim_epoch=reconciliation["claim_epoch"],
        delivery_epoch=reconciliation["delivery_epoch"],
        result=result,
    )
    assert completed["state"] == "completed"
    assert completed["receipt"]["artifact_version_ids"] == [version["id"]]


def test_effect_creation_fences_workflow_stage_and_input_cas(
    store: ControlStore,
) -> None:
    _, active = _active_effect(store)

    with pytest.raises(RevisionConflict):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="stale-workflow",
            request=_import_request(operation_id="operation-stale-workflow"),
            expected_workflow_revision=active["workflow"]["revision"] - 1,
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )
    with pytest.raises(RevisionConflict):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="stale-stage",
            request=_import_request(operation_id="operation-stale-stage"),
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"] - 1,
            expected_stage_input_hash=active["stage"]["input_hash"],
        )
    with pytest.raises(InvalidTransition, match="stage_input_drift"):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="stale-input",
            request=_import_request(operation_id="operation-stale-input"),
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash="0" * 64,
        )

    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_effect_commands"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "effect_request",
    (
        SourceBindingRequest(
            operation_id="bind-cross-run",
            delivery_epoch=1,
            run_id="run-other",
            source_id="source-1",
            canonical_id="arxiv:2607.07675",
            disposition="reused",
        ),
        LineageQueryRequest(
            operation_id="query-cross-run",
            delivery_epoch=1,
            run_id="run-other",
            source_ids=("source-1",),
            query="memory",
        ),
        SuccessorCreationRequest(
            operation_id="successor-cross-run",
            delivery_epoch=1,
            run_id="run-other",
            title="Successor",
            parent_node_ids=("lineage-parent",),
        ),
    ),
    ids=("source-binding", "lineage-query", "successor-creation"),
)
def test_effect_creation_rejects_request_scoped_to_another_run(
    store: ControlStore,
    effect_request: SourceBindingRequest
    | LineageQueryRequest
    | SuccessorCreationRequest,
) -> None:
    _, active = _active_effect(store, query=True)

    with pytest.raises(InvalidTransition, match="request_run_mismatch"):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="cross-run",
            request=effect_request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )


def test_effect_exact_replay_survives_run_fence_without_reopening_work(
    store: ControlStore,
) -> None:
    run, active = _active_effect(store)
    request = _import_request()
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    _cancel_run(store, run)

    assert store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=request,
        expected_workflow_revision=0,
        expected_stage_revision=0,
        expected_stage_input_hash="0" * 64,
    ) == command
    assert store.list_dispatchable_workflow_effects() == []


def test_effect_claim_fences_owner_epochs_and_mutation_expiry(
    store: ControlStore, clock: MutableClock
) -> None:
    _, active = _active_effect(store)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )

    claimed = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    assert claimed["claim_owner"] == "worker-a"
    assert claimed["claim_epoch"] == 1
    assert claimed["delivery_epoch"] == 1
    assert claimed["request"].delivery_epoch == 1
    with pytest.raises(InvalidTransition, match="stale_claim"):
        store.complete_workflow_effect(
            effect_id=command["id"],
            worker_id="worker-b",
            claim_epoch=claimed["claim_epoch"],
            delivery_epoch=claimed["delivery_epoch"],
            result=_import_result(claimed["request"]),
        )

    clock.advance(seconds=31)
    expired = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-b", lease_seconds=30
    )
    assert expired["state"] == "outcome_unknown"
    assert expired["delivery_epoch"] == 1
    assert store.list_dispatchable_workflow_effects() == []


def test_cancel_requested_fences_effect_creation_dispatch_and_recovery(
    store: ControlStore,
) -> None:
    run, active = _active_effect(store)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    _cancel_run(store, run)

    assert store.list_dispatchable_workflow_effects() == []
    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.claim_workflow_effect(
            effect_id=command["id"], worker_id="worker-a", lease_seconds=30
        )
    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key="secondary",
            request=_import_request(
                operation_id="operation-secondary", source_id="source-secondary"
            ),
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )

    assert store.classify_workflow_recovery() == [
        {
            "effect_id": command["id"],
            "workflow_id": command["workflow_id"],
            "stage_id": command["stage_id"],
            "state": "pending",
            "classification": "fenced",
        }
    ]


def test_cancel_requested_blocks_stage_completion_but_preserves_claimed_audit(
    store: ControlStore,
) -> None:
    run, active = _active_effect(store)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    _cancel_run(store, run)

    completed = store.complete_workflow_effect(
        effect_id=command["id"],
        worker_id="worker-a",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=_import_result(claim["request"]),
    )

    assert completed["state"] == "completed"
    assert completed["receipt"]["result_identity"] == completed["result_identity"]
    with pytest.raises(InvalidTransition, match="cancel_requested"):
        store.complete_workflow_stage(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            references=(
                {
                    "kind": "engine_source",
                    "id": completed["receipt"]["engine_reference"]["value"],
                    "metadata": {"effect_id": completed["id"]},
                },
            ),
            checkpoint_state={"cursor": 1},
        )


def test_cancel_requested_preserves_expired_mutation_reconciliation(
    store: ControlStore, clock: MutableClock
) -> None:
    run, active = _active_effect(store)
    request = _import_request()
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    _cancel_run(store, run)
    clock.advance(seconds=31)

    unknown = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-b", lease_seconds=30
    )
    assert unknown["state"] == "outcome_unknown"
    reconciliation = store.claim_workflow_effect_reconciliation(
        effect_id=command["id"], worker_id="reconciler", lease_seconds=30
    )
    settled = store.complete_workflow_effect_reconciliation(
        effect_id=command["id"],
        worker_id="reconciler",
        claim_epoch=reconciliation["claim_epoch"],
        delivery_epoch=reconciliation["delivery_epoch"],
        result=EffectReconciliationResult(
            domain="source_import",
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=reconciliation["delivery_epoch"],
            disposition="unknown",
        ),
    )

    assert settled["state"] == "manual_required"
    assert store.classify_workflow_recovery()[0]["classification"] == "terminal"


def test_lost_mutation_reply_requires_reconciliation_and_converges_three_outcomes(
    store: ControlStore,
) -> None:
    _, active = _active_effect(store)
    outcomes: dict[str, str] = {}
    for index, disposition in enumerate(("not_found", "committed", "unknown"), 1):
        request = _import_request(
            operation_id=f"operation-{disposition}", source_id=f"source-{index}"
        )
        command = store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key=disposition,
            request=request,
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )
        claim = store.claim_workflow_effect(
            effect_id=command["id"], worker_id="worker-a", lease_seconds=30
        )
        unknown = store.mark_workflow_effect_outcome_unknown(
            effect_id=command["id"],
            worker_id="worker-a",
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
        )
        assert unknown["state"] == "outcome_unknown"
        reconciliation = store.claim_workflow_effect_reconciliation(
            effect_id=command["id"], worker_id="reconciler", lease_seconds=30
        )
        result = EffectReconciliationResult(
            domain="source_import",
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=reconciliation["delivery_epoch"],
            disposition=disposition,
            result=(
                _import_result(claim["request"]) if disposition == "committed" else None
            ),
        )
        settled = store.complete_workflow_effect_reconciliation(
            effect_id=command["id"],
            worker_id="reconciler",
            claim_epoch=reconciliation["claim_epoch"],
            delivery_epoch=reconciliation["delivery_epoch"],
            result=result,
        )
        outcomes[disposition] = settled["state"]

    assert outcomes == {
        "not_found": "pending",
        "committed": "completed",
        "unknown": "manual_required",
    }


def test_typed_result_rejects_copied_hash_with_conflicting_business_identity(
    store: ControlStore,
) -> None:
    _, active = _active_effect(store)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    conflicting = _import_result(claim["request"], source_id="source-conflict")

    with pytest.raises(ValueError, match="does not match its request"):
        store.complete_workflow_effect(
            effect_id=command["id"],
            worker_id="worker-a",
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            result=conflicting,
        )


def test_expired_query_claim_can_be_safely_redelivered(
    store: ControlStore, clock: MutableClock
) -> None:
    run, active = _active_effect(store, query=True)
    request = LineageQueryRequest(
        operation_id="query-lineage",
        delivery_epoch=1,
        run_id=run["id"],
        source_ids=("source-1",),
        query="memory",
    )
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=request,
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    first = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    clock.advance(seconds=31)
    second = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-b", lease_seconds=30
    )

    assert (first["claim_epoch"], first["delivery_epoch"]) == (1, 1)
    assert (second["claim_epoch"], second["delivery_epoch"]) == (2, 2)


def test_stage_completion_requires_every_effect_and_exact_reference(
    store: ControlStore,
) -> None:
    run, active = _active_effect(store)
    workflow = active["workflow"]
    stage = active["stage"]

    with pytest.raises(InvalidTransition, match="effect_requirements_missing"):
        store.complete_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="effect",
            expected_workflow_revision=workflow["revision"],
            expected_stage_revision=stage["revision"],
            references=(),
            checkpoint_state={"cursor": 1},
        )

    command = store.create_workflow_effect(
        workflow_id=workflow["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    completed = store.complete_workflow_effect(
        effect_id=command["id"],
        worker_id="worker-a",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=_import_result(claim["request"]),
    )
    with pytest.raises(InvalidTransition, match="references_missing"):
        store.complete_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="effect",
            expected_workflow_revision=workflow["revision"],
            expected_stage_revision=stage["revision"],
            references=(),
            checkpoint_state={"cursor": 1},
        )

    result = store.complete_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="effect",
        expected_workflow_revision=workflow["revision"],
        expected_stage_revision=stage["revision"],
        references=(
            {
                "kind": "engine_source",
                "id": completed["receipt"]["engine_reference"]["value"],
                "metadata": {"effect_id": completed["id"]},
            },
            {
                "kind": "event",
                "id": store.list_run_events(run["id"])[0]["id"],
                "metadata": {"role": "stage-input"},
            },
        ),
        checkpoint_state={"cursor": 1},
    )

    assert result["stage"]["state"] == "completed"
    assert result["workflow"]["state"] == "completed"
    assert result["checkpoint"]["state_hash"] == _hash({"cursor": 1})
    assert {item["reference_kind"] for item in result["references"]} == {
        "engine_source",
        "event",
    }


@pytest.mark.parametrize(
    ("extra_kind", "extra_id"),
    (("unsupported", "unrelated"), ("engine_source", "paper:unrelated")),
)
def test_stage_completion_rejects_references_outside_the_exact_set(
    store: ControlStore, extra_kind: str, extra_id: str
) -> None:
    _, active = _active_effect(store)
    command = store.create_workflow_effect(
        workflow_id=active["workflow"]["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    completed = store.complete_workflow_effect(
        effect_id=command["id"],
        worker_id="worker-a",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=_import_result(claim["request"]),
    )

    with pytest.raises(InvalidTransition, match="references_extra"):
        store.complete_workflow_stage(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            references=(
                {
                    "kind": "engine_source",
                    "id": completed["receipt"]["engine_reference"]["value"],
                    "metadata": {"effect_id": completed["id"]},
                },
                {"kind": extra_kind, "id": extra_id, "metadata": {}},
            ),
            checkpoint_state={"cursor": 1},
        )


def test_checkpoint_reference_and_next_stage_advance_roll_back_together_on_fault(
    store: ControlStore, clock: MutableClock
) -> None:
    run = _run(store)
    workflow = store.install_workflow(
        run_id=run["id"], definition=_advance_definition()
    )
    active = store.activate_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="effect",
        input_value={"run_id": run["id"]},
        expected_workflow_revision=0,
        expected_stage_revision=0,
    )
    command = store.create_workflow_effect(
        workflow_id=workflow["id"],
        stage_key="effect",
        effect_key="primary",
        request=_import_request(),
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        expected_stage_input_hash=active["stage"]["input_hash"],
    )
    claim = store.claim_workflow_effect(
        effect_id=command["id"], worker_id="worker-a", lease_seconds=30
    )
    completed = store.complete_workflow_effect(
        effect_id=command["id"],
        worker_id="worker-a",
        claim_epoch=claim["claim_epoch"],
        delivery_epoch=claim["delivery_epoch"],
        result=_import_result(claim["request"]),
    )
    references = (
        {
            "kind": "engine_source",
            "id": completed["receipt"]["engine_reference"]["value"],
            "metadata": {"effect_id": completed["id"]},
        },
    )
    failing = ControlStore(
        store.path,
        clock=clock,
        id_factory=DeterministicIds(fail_kind="workflow_checkpoint", fail_number=1),
    )
    failing.initialize()

    with pytest.raises(RuntimeError, match="injected workflow fault"):
        failing.complete_workflow_stage(
            workflow_id=workflow["id"],
            stage_key="effect",
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            references=references,
            checkpoint_state={"cursor": 1},
        )

    after_fault = store.get_workflow(workflow["id"])
    assert [stage["state"] for stage in after_fault["stages"]] == [
        "active",
        "pending",
    ]
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM workflow_stage_references").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 0
        )

    advanced = store.complete_workflow_stage(
        workflow_id=workflow["id"],
        stage_key="effect",
        expected_workflow_revision=active["workflow"]["revision"],
        expected_stage_revision=active["stage"]["revision"],
        references=references,
        checkpoint_state={"cursor": 1},
    )
    assert [stage["state"] for stage in advanced["workflow"]["stages"]] == [
        "completed",
        "ready",
    ]
    assert advanced["workflow"]["current_stage_key"] == "finish"


def test_reopen_classifies_pending_claimed_unknown_and_terminal_without_mutation(
    store: ControlStore, clock: MutableClock
) -> None:
    _, active = _active_effect(store)
    effect_ids: dict[str, str] = {}
    for name in ("pending", "claimed", "unknown", "terminal"):
        command = store.create_workflow_effect(
            workflow_id=active["workflow"]["id"],
            stage_key="effect",
            effect_key=name,
            request=_import_request(
                operation_id=f"operation-{name}", source_id=f"source-{name}"
            ),
            expected_workflow_revision=active["workflow"]["revision"],
            expected_stage_revision=active["stage"]["revision"],
            expected_stage_input_hash=active["stage"]["input_hash"],
        )
        effect_ids[name] = command["id"]
    store.claim_workflow_effect(
        effect_id=effect_ids["claimed"], worker_id="worker-a", lease_seconds=30
    )
    unknown_claim = store.claim_workflow_effect(
        effect_id=effect_ids["unknown"], worker_id="worker-a", lease_seconds=30
    )
    store.mark_workflow_effect_outcome_unknown(
        effect_id=effect_ids["unknown"],
        worker_id="worker-a",
        claim_epoch=unknown_claim["claim_epoch"],
        delivery_epoch=unknown_claim["delivery_epoch"],
    )
    terminal_claim = store.claim_workflow_effect(
        effect_id=effect_ids["terminal"], worker_id="worker-a", lease_seconds=30
    )
    store.complete_workflow_effect(
        effect_id=effect_ids["terminal"],
        worker_id="worker-a",
        claim_epoch=terminal_claim["claim_epoch"],
        delivery_epoch=terminal_claim["delivery_epoch"],
        result=_import_result(terminal_claim["request"]),
    )
    reopened = ControlStore(store.path, clock=clock, id_factory=DeterministicIds())
    reopened.initialize()

    before = store.path.read_bytes()
    classifications = reopened.classify_workflow_recovery()
    after = store.path.read_bytes()

    assert {item["effect_id"]: item["classification"] for item in classifications} == {
        effect_ids["pending"]: "pending",
        effect_ids["claimed"]: "claimed",
        effect_ids["unknown"]: "unknown",
        effect_ids["terminal"]: "terminal",
    }
    assert after == before
