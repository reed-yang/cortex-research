from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from cortex_platform.product.artifacts import (
    ArtifactMaterializationService,
    AssetRoot,
    FilesystemMaterializer,
)
from cortex_platform.product.control import ControlStore, InvalidTransition
from cortex_platform.product.sources import SourceImportDispatcher
from cortex_platform.tests.support.sources import TemporaryResearchImportAdapter
from cortex_platform.product.workflows import (
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectReconciliationRequest,
    EffectReconciliationResult,
    EngineReference,
    GoldenArtifactPlans,
    GoldenWorkflowCase,
    GoldenWorkflowFacade,
    LineageLink,
    LineageNode,
    LineageQueryRequest,
    LineageQueryResult,
    RuntimeStageRequest,
    RuntimeStageResult,
    SourceBindingRequest,
    SourceBindingResult,
    SourceImportRequest,
    SourceImportResult,
    SuccessorCreationRequest,
    SuccessorCreationResult,
    stable_attempt_operation_id,
    stable_operation_id,
)
from cortex_platform.tests.support.workflows import (
    DeterministicResearchEngine,
    DeterministicRuntime,
)
from cortex_platform.tests.product.sources.fakes import (
    create_golden_intent,
    make_run,
    make_store,
    register_echo,
)


def _case(intent_id: str) -> GoldenWorkflowCase:
    return GoldenWorkflowCase(
        source_intent_id=intent_id,
        lineage_query="Echo memory successors",
        successor_title="Echo successor",
        artifact_plans=GoldenArtifactPlans(
            evidence_sha256="1" * 64,
            living_sha256="2" * 64,
            training_sha256="3" * 64,
            snapshot_sha256="4" * 64,
        ),
    )


class _GeneratedArtifacts:
    def __init__(self, factory) -> None:
        self._factory = factory
        self._committed: dict[str, ArtifactWorkflowResult] = {}
        self.calls: list[str] = []

    def execute_artifact(
        self, request: ArtifactWorkflowRequest
    ) -> ArtifactWorkflowResult:
        self.calls.append(request.operation_id)
        result = self._factory(request)
        request.validate_result(result)
        self._committed[request.operation_id] = result
        return result

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        result = self._committed.get(request.operation_id)
        return EffectReconciliationResult(
            domain=request.domain,
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=request.delivery_epoch,
            disposition="committed" if result is not None else "not_found",
            result=result,
        )


def _start_runtime(store: ControlStore, run: dict) -> dict[str, str]:
    attempt_id = run["active_attempt_id"]
    identity = {
        "attempt_id": attempt_id,
        "runtime_release_id": "golden-release",
        "state_generation_id": "golden-generation",
    }
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        dispatch_owner="golden-runtime",
        runtime_slot_id="golden-slot",
        runtime_artifact_digest="golden-runtime-artifact",
        runtime_worker_protocol="golden-protocol",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key="golden-runtime-reserve",
        **identity,
    ).value
    binding = store.create_runtime_binding(
        thread_id=run["thread_id"],
        adapter_id="golden-fake",
        runtime_session_ref="golden-session",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key="golden-runtime-binding",
    ).value
    identity["runtime_binding_id"] = binding["id"]
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        dispatch_owner="golden-runtime",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key="golden-runtime-pin",
        **identity,
    ).value
    for state in ("starting", "running"):
        run = store.apply_runtime_transition(
            run_id=run["id"],
            target_state=state,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=f"golden-runtime-{state}",
            **identity,
        ).value
    return identity


def _engine_result(store: ControlStore, request):
    if isinstance(request, (SourceBindingRequest, SourceImportRequest)):
        reference = EngineReference(
            "source", store.get_source(request.source_id)["engine_ref"]
        )
        if isinstance(request, SourceBindingRequest):
            return SourceBindingResult(
                request.operation_id,
                1,
                request.request_hash,
                request.source_id,
                reference,
            )
        return SourceImportResult(
            request.operation_id,
            1,
            request.request_hash,
            request.source_id,
            reference,
            {"source_rows": 1, "chunks": 1, "directories": 1},
        )
    if isinstance(request, LineageQueryRequest):
        return LineageQueryResult(
            request.operation_id,
            1,
            request.request_hash,
            (
                LineageNode(
                    "lineage-dormant",
                    "dormant",
                    "Dormant baseline",
                    2,
                    EngineReference("lineage", "idea:dormant"),
                ),
                LineageNode(
                    "lineage-graduated",
                    "graduated",
                    "Graduated baseline",
                    4,
                    EngineReference("lineage", "idea:graduated"),
                ),
            ),
        )
    assert isinstance(request, SuccessorCreationRequest)
    node = LineageNode(
        "lineage-successor",
        "active",
        request.title,
        1,
        EngineReference("lineage", "idea:successor"),
    )
    return SuccessorCreationResult(
        request.operation_id,
        1,
        request.request_hash,
        node,
        request.parent_node_ids,
        tuple(
            LineageLink(f"link-{index}", node.node_id, parent, "successor_reuses")
            for index, parent in enumerate(request.parent_node_ids, 1)
        ),
    )


def test_g0_stops_at_source_decision_without_external_mutation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)
    register_echo(store)
    intent = create_golden_intent(store, run)
    engine = DeterministicResearchEngine(responses={})
    asset_root = tmp_path / "assets"

    projection = GoldenWorkflowFacade(
        store=store,
        engine=engine,
        runtime=None,
        artifacts=None,
        worker_id="golden-flow",
        case=_case(intent["id"]),
    ).start(run["id"])

    assert projection.blocked_on == "source_decision"
    assert projection.current_stage_key == "await_source_decision"
    assert projection.decision_ids == (intent["decision"]["id"],)
    assert engine.calls == []
    assert store.list_workflow_effects(workflow_id=projection.workflow_id) == []
    assert store.list_all_source_imports() == []
    assert not asset_root.exists()

    with pytest.raises(InvalidTransition, match="case.*drift"):
        GoldenWorkflowFacade(
            store=ControlStore(store.path),
            engine=engine,
            runtime=None,
            artifacts=None,
            worker_id="golden-flow",
            case=replace(_case(intent["id"]), lineage_query="different query"),
        ).advance_until_blocked(run["id"])

    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="replace_url_with_echo",
        expected_revision=intent["revision"],
        actor_id="local",
        idempotency_key="g0-replace-source-0001",
    )
    with pytest.raises(ValueError, match="one reused and one imported"):
        GoldenWorkflowFacade(
            store=ControlStore(store.path),
            engine=engine,
            runtime=None,
            artifacts=None,
            worker_id="golden-flow",
            case=_case(intent["id"]),
        ).advance_until_blocked(run["id"])


def test_g1_completes_exact_golden_manifest_across_restart_and_replay(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)
    register_echo(store)
    intent = create_golden_intent(store, run)
    case = _case(intent["id"])
    import_root = tmp_path / "research-imports"
    asset_root = tmp_path / "assets"
    asset_root.mkdir(mode=0o700)
    materializer = ArtifactMaterializationService(
        store,
        FilesystemMaterializer((AssetRoot("golden-assets", asset_root, 10_000),)),
    )
    workflow = GoldenWorkflowFacade(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=None,
        worker_id="golden-flow",
        case=case,
    ).start(run["id"])
    assert workflow.blocked_on == "source_decision"

    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="keep_both",
        expected_revision=intent["revision"],
        actor_id="local",
        idempotency_key="golden-source-resolution",
    )
    import_block = GoldenWorkflowFacade(
        store=ControlStore(store.path),
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=None,
        worker_id="golden-flow",
        case=case,
    ).advance_until_blocked(run["id"])
    assert import_block.blocked_on == "source_import"
    action = store.list_pending_source_imports()[0]
    delivery = SourceImportDispatcher(
        store=store,
        adapter=TemporaryResearchImportAdapter(
            database=tmp_path / "research.state",
            root=import_root,
            safety_root=tmp_path,
        ),
    ).deliver(
        action_id=action["id"],
        worker_id="source-importer",
        claim_key="golden-source-claim",
        completion_key="golden-source-complete",
    )
    assert delivery.status == "completed"
    runtime_identity = _start_runtime(store, store.get_run(run["id"]))
    workflow_id = workflow.workflow_id
    operation_ids = {
        effect_key: stable_operation_id(workflow_id, stage, effect_key)
        for stage, effect_key in (
            ("import_sources", "reused"),
            ("import_sources", "imported"),
            ("retrieve_lineage", "lineage"),
            ("create_successor", "successor"),
        )
    }
    engine = DeterministicResearchEngine(
        responses={
            operation_id: lambda request: _engine_result(store, request)
            for operation_id in operation_ids.values()
        }
    )

    runtime_sequences = {
        "research_evidence": 0,
        "research_architecture": 1,
        "research_training_plan": 2,
    }

    def runtime_result(request: RuntimeStageRequest) -> RuntimeStageResult:
        store.record_runtime_observation(
            run_id=run["id"],
            expected_revision=store.get_run(run["id"])["revision"],
            event_type="runtime.tool.completed",
            payload={
                "tool_call_id": f"tool-{request.stage_key}",
                "tool_name": "golden_stage",
                "is_error": False,
                "duration_ms": 1,
            },
            actor_id="runtime",
            idempotency_key=f"observe-{request.stage_key}",
            adapter_event_id=request.operation_id,
            adapter_event_sequence=runtime_sequences[request.stage_key],
            **runtime_identity,
        )
        event = next(
            item
            for item in store.list_run_events(run["id"])
            if item["causation_id"] == request.operation_id
        )
        return RuntimeStageResult(
            request.operation_id,
            1,
            request.request_hash,
            request.attempt_id,
            event["id"],
            request.stage_key,
            request.effect_kind,
            f"output-{request.stage_key}",
            hashlib.sha256(request.stage_key.encode()).hexdigest(),
        )

    runtime = DeterministicRuntime(
        responses={
            stable_attempt_operation_id(
                workflow_id,
                stage,
                effect_key,
                runtime_identity["attempt_id"],
            ): runtime_result
            for stage, effect_key in (
                ("research_evidence", "evidence"),
                ("research_architecture", "architecture"),
                ("research_training_plan", "training"),
            )
        }
    )

    def artifact_result(request: ArtifactWorkflowRequest) -> ArtifactWorkflowResult:
        if request.effect_kind == "artifact_snapshot":
            thread = store.get_thread(run["thread_id"])
            snapshot = store.create_artifact_snapshot(
                workspace_id=thread["workspace_id"],
                run_id=run["id"],
                attempt_id=request.attempt_id,
                name="Golden research snapshot",
                artifact_version_ids=request.snapshot_member_version_ids,
                actor_id="golden-artifacts",
                idempotency_key="golden-snapshot-0001",
            ).value
            return ArtifactWorkflowResult(
                request.operation_id,
                1,
                request.request_hash,
                request.attempt_id,
                request.stage_key,
                request.effect_kind,
                request.snapshot_member_version_ids,
                snapshot["id"],
            )
        kind = request.effect_kind.removeprefix("artifact_")
        content = f"# Golden {kind}\n".encode()
        thread = store.get_thread(run["thread_id"])
        artifact = store.create_artifact(
            workspace_id=thread["workspace_id"],
            thread_id=thread["id"],
            run_id=run["id"],
            attempt_id=request.attempt_id,
            kind={
                "evidence": "evidence-matrix",
                "living": "living-brief",
                "training": "training-plan",
            }[kind],
            title=f"Golden {kind}",
            actor_id="golden-artifacts",
            idempotency_key=f"golden-artifact-{kind}",
        ).value
        bindings = store.list_run_source_bindings(run["id"])
        sources = [store.get_source(binding["source_id"]) for binding in bindings]
        reservation = store.request_artifact_version(
            artifact_id=artifact["id"],
            logical_version=1,
            run_id=run["id"],
            attempt_id=request.attempt_id,
            source_ids=tuple(sorted(source["id"] for source in sources)),
            research_engine_refs=tuple(
                sorted(source["engine_ref"] for source in sources)
            ),
            generator={"name": "golden-research", "version": "1"},
            tool={"name": "golden-writer", "version": "1"},
            parents=(),
            root_id="golden-assets",
            relative_path=f"versions/{kind}.md",
            sha256=hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
            media_type="text/markdown",
            advance_head=True,
            expected_head_revision=0,
            actor_id="golden-artifacts",
            idempotency_key=f"golden-version-{kind}",
        ).value
        committed = materializer.materialize_action(
            action_id=reservation["materialization_action"]["id"],
            content=content,
            worker_id="golden-artifacts",
            lease_seconds=30,
        )
        return ArtifactWorkflowResult(
            request.operation_id,
            1,
            request.request_hash,
            request.attempt_id,
            request.stage_key,
            request.effect_kind,
            (committed["id"],),
        )

    artifacts = _GeneratedArtifacts(artifact_result)

    def restarted() -> GoldenWorkflowFacade:
        return GoldenWorkflowFacade(
            store=ControlStore(store.path),
            engine=engine,
            runtime=runtime,
            artifacts=artifacts,
            worker_id="golden-flow",
            case=case,
        )

    lineage_block = restarted().advance_until_blocked(run["id"])
    assert lineage_block.blocked_on == "lineage_decision"
    lineage_decision = store.get_decision(lineage_block.decision_ids[0])
    store.resolve_decision(
        decision_id=lineage_decision["id"],
        choice="proceed",
        expected_revision=lineage_decision["revision"],
        actor_id="local",
        idempotency_key="golden-lineage-resolution",
    )
    store.apply_runtime_transition(
        run_id=run["id"],
        target_state="running",
        expected_revision=store.get_run(run["id"])["revision"],
        actor_id="runtime",
        idempotency_key="golden-lineage-runtime-resumed",
        **runtime_identity,
    )
    completed = restarted().advance_until_blocked(run["id"])
    assert completed.blocked_on == "terminal"

    bindings = store.list_run_source_bindings(run["id"])
    effects = store.list_workflow_effects(workflow_id=workflow_id)
    artifacts_manifest = store.list_artifacts(thread_id=run["thread_id"])
    snapshot = store.list_artifact_snapshots(
        workspace_id=store.get_thread(run["thread_id"])["workspace_id"]
    )[0]
    manifest = {
        "sources": sorted(
            (
                store.get_source(item["source_id"])["canonical_id"],
                item["disposition"],
                store.get_source(item["source_id"])["import_state"],
                store.get_source(item["source_id"])["engine_ref"],
            )
            for item in bindings
        ),
        "lineage": [
            (
                node["node_id"],
                node["status"],
                node["revision"],
                node["engine_reference"]["value"],
            )
            for node in next(
                effect["receipt"]
                for effect in effects
                if effect["effect_kind"] == "lineage_query"
            )["nodes"]
        ],
        "effect_kinds": [effect["effect_kind"] for effect in effects],
        "artifacts": sorted(
            (
                item["kind"],
                item["head_revision"],
                store.get_artifact_version(item["head_artifact_version_id"])[
                    "logical_version"
                ],
                store.get_artifact_version(item["head_artifact_version_id"])["sha256"],
            )
            for item in artifacts_manifest
        ),
        "snapshot_members": sorted(
            (member["artifact_id"], member["logical_version"])
            for member in snapshot["members"]
        ),
        "events": Counter(item["type"] for item in store.list_run_events(run["id"])),
        "files": sorted(
            str(path.relative_to(tmp_path))
            for root in (import_root, asset_root)
            for path in root.rglob("*")
            if path.is_file() and ".cortex-" not in str(path)
        ),
    }
    calls = (list(engine.calls), list(runtime.calls), list(artifacts.calls))

    assert manifest["sources"] == [
        ("arxiv:2606.04527", "reused", "existing", "paper:echo-existing"),
        (
            "arxiv:2607.07675",
            "imported",
            "imported",
            "paper:d7a755b4af3a65c5d6b6",
        ),
    ]
    assert manifest["lineage"] == [
        ("lineage-dormant", "dormant", 2, "idea:dormant"),
        ("lineage-graduated", "graduated", 4, "idea:graduated"),
    ]
    assert manifest["effect_kinds"] == [
        "source_binding",
        "source_import",
        "lineage_query",
        "successor_creation",
        "runtime_evidence",
        "runtime_architecture",
        "runtime_training_plan",
        "artifact_evidence",
        "artifact_living",
        "artifact_training",
        "artifact_snapshot",
    ]
    assert manifest["artifacts"] == [
        (
            "evidence-matrix",
            1,
            1,
            hashlib.sha256(b"# Golden evidence\n").hexdigest(),
        ),
        (
            "living-brief",
            1,
            1,
            hashlib.sha256(b"# Golden living\n").hexdigest(),
        ),
        (
            "training-plan",
            1,
            1,
            hashlib.sha256(b"# Golden training\n").hexdigest(),
        ),
    ]
    assert len(manifest["snapshot_members"]) == 3
    assert {member["artifact_version_id"] for member in snapshot["members"]} == {
        artifact["head_artifact_version_id"] for artifact in artifacts_manifest
    }
    successor = next(
        effect["receipt"]
        for effect in effects
        if effect["effect_kind"] == "successor_creation"
    )
    assert (
        successor["node"]["node_id"],
        successor["node"]["status"],
        successor["parent_node_ids"],
        sorted(link["to_node_id"] for link in successor["links"]),
    ) == (
        "lineage-successor",
        "active",
        ["lineage-dormant", "lineage-graduated"],
        ["lineage-dormant", "lineage-graduated"],
    )
    assert manifest["events"] == Counter(
        {
            "run.queued": 1,
            "source.intent_received": 1,
            "source.conflict_detected": 1,
            "decision.required": 3,
            "decision.resolved": 3,
            "source.reused": 1,
            "source.import_requested": 1,
            "source.imported": 1,
            "runtime.dispatch.reserved": 1,
            "runtime.bound": 1,
            "run.starting": 1,
            "run.running": 2,
            "runtime.tool.completed": 3,
            "artifact.created": 3,
            "artifact.materialization_requested": 3,
            "artifact.version_committed": 3,
            "artifact.head_advanced": 3,
            "artifact.snapshot_created": 1,
        }
    )
    assert manifest["files"] == [
        "assets/versions/evidence.md",
        "assets/versions/living.md",
        "assets/versions/training.md",
        "research-imports/d7a755b4af3a65c5d6b6/.manifest-ed162390d149b084.tmp",
        "research-imports/d7a755b4af3a65c5d6b6/manifest.json",
    ]
    assert len(store.list_all_source_imports()) == 1

    for _ in range(2):
        assert restarted().recover(run["id"]) == completed
    assert (engine.calls, runtime.calls, artifacts.calls) == calls
    assert len(store.list_workflow_effects(workflow_id=workflow_id)) == 11
    assert (
        Counter(item["type"] for item in store.list_run_events(run["id"]))
        == manifest["events"]
    )
    assert len(store.list_artifacts(thread_id=run["thread_id"])) == 3
    assert (
        len(
            store.list_artifact_snapshots(
                workspace_id=store.get_thread(run["thread_id"])["workspace_id"]
            )
        )
        == 1
    )
