from __future__ import annotations

from dataclasses import replace

import pytest

from cortex_platform.product import workflows
from cortex_platform.product.sources import validate_engine_ref
from cortex_platform.product.workflows import (
    GOLDEN_RESEARCH_WORKFLOW,
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectReconciliationRequest,
    EffectReconciliationResult,
    EngineReference,
    LineageLink,
    LineageNode,
    LineageQueryRequest,
    LineageQueryResult,
    SourceBindingRequest,
    SourceBindingResult,
    SourceImportRequest,
    SourceImportResult,
    StageDefinition,
    SuccessorCreationRequest,
    SuccessorCreationResult,
    WorkflowDefinition,
)


def test_artifact_workflow_effect_contract_is_public() -> None:
    assert hasattr(workflows, "ArtifactWorkflowRequest")
    assert hasattr(workflows, "ArtifactWorkflowResult")
    assert hasattr(workflows, "ArtifactWorkflowPort")


def _artifact_request(
    *,
    delivery_epoch: int = 1,
    effect_kind: str = "artifact_living",
    snapshot_member_version_ids: tuple[str, ...] = (),
) -> ArtifactWorkflowRequest:
    return ArtifactWorkflowRequest(
        operation_id="workflow-artifact-living-attempt-1",
        delivery_epoch=delivery_epoch,
        run_id="run-golden-001",
        attempt_id="attempt-golden-001-a2",
        stage_key="publish_living_artifacts",
        effect_kind=effect_kind,
        stage_input_hash="1" * 64,
        dependency_result_ids=("2" * 64,),
        plan_id="golden.living",
        plan_hash="3" * 64,
        snapshot_member_version_ids=snapshot_member_version_ids,
    )


def test_artifact_workflow_contract_round_trips_and_binds_result() -> None:
    request = _artifact_request()
    redelivery = _artifact_request(delivery_epoch=2)
    assert request.request_hash == redelivery.request_hash
    assert ArtifactWorkflowRequest.from_dict(request.to_dict()) == request

    result = ArtifactWorkflowResult(
        operation_id=request.operation_id,
        delivery_epoch=request.delivery_epoch,
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        stage_key=request.stage_key,
        effect_kind=request.effect_kind,
        artifact_version_ids=("artifact-version-1", "artifact-version-2"),
    )
    request.validate_result(result)
    assert ArtifactWorkflowResult.from_dict(result.to_dict()) == result

    with pytest.raises(ValueError, match="does not match"):
        request.validate_result(replace(result, attempt_id="attempt-other"))


@pytest.mark.parametrize(
    "effect_kind",
    ("artifact_delete", "artifact_publish", "artifact_snapshot_extra"),
)
def test_artifact_workflow_contract_rejects_unsealed_effect_kinds(
    effect_kind: str,
) -> None:
    with pytest.raises(ValueError, match="effect_kind is unsupported"):
        _artifact_request(effect_kind=effect_kind)


def test_artifact_snapshot_result_requires_exact_snapshot_shape() -> None:
    request = _artifact_request(
        effect_kind="artifact_snapshot",
        snapshot_member_version_ids=("artifact-version-1",),
    )
    result = ArtifactWorkflowResult(
        operation_id=request.operation_id,
        delivery_epoch=request.delivery_epoch,
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        stage_key=request.stage_key,
        effect_kind=request.effect_kind,
        artifact_version_ids=("artifact-version-1",),
        snapshot_id="artifact-snapshot-1",
    )
    request.validate_result(result)

    with pytest.raises(ValueError, match="snapshot_id"):
        replace(result, snapshot_id=None)
    with pytest.raises(ValueError, match="only artifact_snapshot"):
        replace(result, effect_kind="artifact_living")
    with pytest.raises(ValueError, match="requested snapshot members"):
        request.validate_result(
            replace(result, artifact_version_ids=("artifact-version-other",))
        )


def test_artifact_snapshot_request_binds_canonical_members() -> None:
    with pytest.raises(ValueError, match="snapshot members"):
        _artifact_request(effect_kind="artifact_snapshot")
    with pytest.raises(ValueError, match="snapshot members"):
        _artifact_request(
            effect_kind="artifact_living",
            snapshot_member_version_ids=("artifact-version-1",),
        )
    with pytest.raises(ValueError, match="canonical order"):
        _artifact_request(
            effect_kind="artifact_snapshot",
            snapshot_member_version_ids=(
                "artifact-version-2",
                "artifact-version-1",
            ),
        )


def test_artifact_workflow_result_round_trips_through_reconciliation() -> None:
    effect = _artifact_request(delivery_epoch=1)
    committed = ArtifactWorkflowResult(
        operation_id=effect.operation_id,
        delivery_epoch=1,
        request_hash=effect.request_hash,
        attempt_id=effect.attempt_id,
        stage_key=effect.stage_key,
        effect_kind=effect.effect_kind,
        artifact_version_ids=("artifact-version-1",),
    )
    request = EffectReconciliationRequest(effect, delivery_epoch=2)
    result = EffectReconciliationResult(
        domain=request.domain,
        operation_id=request.operation_id,
        request_hash=request.request_hash,
        delivery_epoch=request.delivery_epoch,
        disposition="committed",
        result=committed,
    )
    request.validate_result(result)
    assert EffectReconciliationRequest.from_dict(request.to_dict()) == request
    assert EffectReconciliationResult.from_dict(result.to_dict()) == result


def test_artifact_workflow_contract_rejects_open_or_unstable_inputs() -> None:
    request = _artifact_request()
    open_wire = request.to_dict()
    open_wire["path"] = "/private/research.md"
    with pytest.raises(ValueError, match="fields do not match"):
        ArtifactWorkflowRequest.from_dict(open_wire)

    with pytest.raises(ValueError, match="effect_kind is unsupported"):
        replace(request, effect_kind="runtime_evidence")
    with pytest.raises(ValueError, match="canonical.*order"):
        replace(request, dependency_result_ids=("f" * 64, "1" * 64))

    result = ArtifactWorkflowResult(
        operation_id=request.operation_id,
        delivery_epoch=request.delivery_epoch,
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        stage_key=request.stage_key,
        effect_kind=request.effect_kind,
        artifact_version_ids=("artifact-version-000",),
    )
    with pytest.raises(ValueError, match="version limit"):
        replace(
            result,
            artifact_version_ids=tuple(
                f"artifact-version-{index:03d}" for index in range(501)
            ),
        )


def _binding_request(*, delivery_epoch: int = 1) -> SourceBindingRequest:
    return SourceBindingRequest(
        operation_id="operation-source-echo",
        delivery_epoch=delivery_epoch,
        run_id="run-golden-001",
        source_id="source-echo",
        canonical_id="arxiv:2606.04527",
        disposition="reused",
    )


def _binding_result(request: SourceBindingRequest) -> SourceBindingResult:
    return SourceBindingResult(
        operation_id=request.operation_id,
        delivery_epoch=request.delivery_epoch,
        request_hash=request.request_hash,
        source_id=request.source_id,
        engine_reference=EngineReference("source", "paper:echo-existing"),
    )


def _import_request(*, delivery_epoch: int = 1) -> SourceImportRequest:
    return SourceImportRequest(
        operation_id="operation-import-lingbot",
        delivery_epoch=delivery_epoch,
        source_id="source-lingbot",
        canonical_id="arxiv:2607.07675",
    )


def _import_result(request: SourceImportRequest) -> SourceImportResult:
    return SourceImportResult(
        request.operation_id,
        request.delivery_epoch,
        request.request_hash,
        request.source_id,
        EngineReference("source", "paper:lingbot-video"),
        {"source_rows": 1, "chunks": 2, "directories": 1},
    )


def _nodes() -> tuple[LineageNode, ...]:
    return (
        LineageNode(
            "lineage-echo-memory-graduated",
            "graduated",
            "Echo memory architecture survey",
            7,
            EngineReference("lineage", "idea:echo-memory-graduated"),
        ),
        LineageNode(
            "lineage-helios-ttt-dormant",
            "dormant",
            "Helios-14B TTT memory feasibility",
            4,
            EngineReference("lineage", "idea:helios-ttt-dormant"),
        ),
    )


def _query_request(*, delivery_epoch: int = 1) -> LineageQueryRequest:
    return LineageQueryRequest(
        operation_id="query-lineage-golden",
        delivery_epoch=delivery_epoch,
        run_id="run-golden-001",
        source_ids=("source-echo", "source-lingbot"),
        query="Helios-14B memory",
    )


def _query_result(request: LineageQueryRequest) -> LineageQueryResult:
    return LineageQueryResult(
        request.operation_id,
        request.delivery_epoch,
        request.request_hash,
        _nodes(),
    )


def _successor_request(*, delivery_epoch: int = 1) -> SuccessorCreationRequest:
    return SuccessorCreationRequest(
        operation_id="operation-successor-golden",
        delivery_epoch=delivery_epoch,
        run_id="run-golden-001",
        title="Helios-14B on Wan2.2 hybrid memory successor",
        parent_node_ids=(
            "lineage-echo-memory-graduated",
            "lineage-helios-ttt-dormant",
        ),
    )


def _successor_result(
    request: SuccessorCreationRequest,
) -> SuccessorCreationResult:
    node = LineageNode(
        "lineage-helios-echo-successor",
        "active",
        request.title,
        1,
        EngineReference("lineage", "idea:helios-echo-successor"),
    )
    return SuccessorCreationResult(
        operation_id=request.operation_id,
        delivery_epoch=request.delivery_epoch,
        request_hash=request.request_hash,
        node=node,
        parent_node_ids=request.parent_node_ids,
        links=(
            LineageLink(
                "link-successor-dormant",
                node.node_id,
                "lineage-helios-ttt-dormant",
                "successor_reuses",
            ),
            LineageLink(
                "link-successor-graduated",
                node.node_id,
                "lineage-echo-memory-graduated",
                "successor_reuses",
            ),
        ),
    )


def test_golden_stage_graph_freezes_mutations_queries_and_checkpoints() -> None:
    assert GOLDEN_RESEARCH_WORKFLOW.identity == "research.golden@1"
    assert tuple(stage.key for stage in GOLDEN_RESEARCH_WORKFLOW.stages) == (
        "resolve_sources",
        "await_source_decision",
        "import_sources",
        "retrieve_lineage",
        "await_lineage_decision",
        "create_successor",
        "research_evidence",
        "research_architecture",
        "research_training_plan",
        "publish_living_artifacts",
        "freeze_snapshot",
    )
    assert GOLDEN_RESEARCH_WORKFLOW.stage("import_sources").required_receipts == (
        "source_binding",
        "source_import",
    )
    assert GOLDEN_RESEARCH_WORKFLOW.stage("retrieve_lineage").required_results == (
        "lineage_query",
    )
    assert GOLDEN_RESEARCH_WORKFLOW.stage("create_successor").checkpoint is True
    assert GOLDEN_RESEARCH_WORKFLOW.stage("freeze_snapshot").checkpoint is True


def test_stage_graph_defensively_copies_sequences_and_rejects_effect_gaps() -> None:
    dependencies: list[str] = []
    receipts = ["appendix"]
    stage = StageDefinition(
        "publish_appendix",
        "artifact",
        dependencies,
        receipts,
    )
    stages = [StageDefinition("start", "control", ()), stage]
    definition = WorkflowDefinition("research.extended", 2, stages)
    dependencies.append("late")
    receipts.append("mutated")
    stages.clear()

    assert definition.stages[1].dependencies == ()
    assert definition.stages[1].required_receipts == ("appendix",)
    with pytest.raises(ValueError, match="receipts only"):
        StageDefinition("mutate", "engine_mutation", ())
    with pytest.raises(ValueError, match="durable results only"):
        StageDefinition("query", "engine_query", ())
    with pytest.raises(ValueError, match="cannot require"):
        StageDefinition("choose", "decision", (), ("receipt",))


def test_workflow_graph_rejects_nonpreceding_and_duplicate_stages() -> None:
    with pytest.raises(ValueError, match="non-preceding"):
        WorkflowDefinition(
            "research.invalid",
            1,
            (StageDefinition("late", "control", ("missing",)),),
        )
    with pytest.raises(ValueError, match="unique"):
        WorkflowDefinition(
            "research.invalid",
            1,
            (
                StageDefinition("same", "control", ()),
                StageDefinition("same", "control", ()),
            ),
        )


def test_requests_compute_closed_hashes_and_reject_wire_tampering() -> None:
    request = _binding_request()
    redelivery = _binding_request(delivery_epoch=2)
    assert request.request_hash == redelivery.request_hash
    assert SourceBindingRequest.from_dict(request.to_dict()) == request

    tampered = request.to_dict()
    tampered["canonical_id"] = "arxiv:2607.07675"
    with pytest.raises(ValueError, match="hash does not match"):
        SourceBindingRequest.from_dict(tampered)
    open_wire = request.to_dict()
    open_wire["provider_path"] = "/private/research.db"
    with pytest.raises(ValueError, match="fields do not match"):
        SourceBindingRequest.from_dict(open_wire)


@pytest.mark.parametrize(
    "canonical_id",
    [
        "/Users/operator/private/research.db",
        r"C:\\Users\\reed\\research.db",
        "file:///private/research.db",
        "arxiv:2607.07675 ",
        "arxiv:2607.07675\u0000",
        "arxiv:2607.07675\ue000",
        "project:Echo-Infinity",
    ],
)
def test_source_requests_reject_paths_aliases_and_noncanonical_text(
    canonical_id: str,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        SourceImportRequest(
            operation_id="operation-import",
            delivery_epoch=1,
            source_id="source-lingbot",
            canonical_id=canonical_id,
        )


def test_source_requests_accept_only_canonical_p2_source_identities() -> None:
    assert _import_request().canonical_id == "arxiv:2607.07675"
    assert SourceImportRequest(
        "operation-doi",
        1,
        "source-doi",
        "doi:10.1234/example",
    ).canonical_id == "doi:10.1234/example"
    digest = "a" * 64
    assert SourceImportRequest(
        "operation-local",
        1,
        "source-local",
        f"sha256:{digest}",
    ).canonical_id == f"sha256:{digest}"


def test_shared_engine_reference_preserves_existing_namespaces_without_rewrite() -> None:
    assert validate_engine_ref("paper:echo-existing") == "paper:echo-existing"
    assert EngineReference("source", "paper:echo-existing").value == (
        "paper:echo-existing"
    )
    assert EngineReference("lineage", "idea:existing-42").value == "idea:existing-42"
    with pytest.raises(ValueError, match="namespace"):
        EngineReference("source", "idea:existing-42")
    with pytest.raises(ValueError, match="namespace"):
        EngineReference("lineage", "paper:echo-existing")
    for invalid in (
        "/private/paper",
        "paper ref",
        "paper:../secret",
        "paper:\u202esecret",
        "x" * 501,
    ):
        with pytest.raises(ValueError, match="engine_ref"):
            validate_engine_ref(invalid)


def test_binding_result_is_closed_content_addressed_and_request_bound() -> None:
    request = _binding_request()
    result = _binding_result(request)
    request.validate_result(result)
    assert SourceBindingResult.from_dict(result.to_dict()) == result

    tampered = result.to_dict()
    tampered["source_id"] = "source-other"
    with pytest.raises(ValueError, match="identity does not match"):
        SourceBindingResult.from_dict(tampered)
    with pytest.raises(ValueError, match="does not match its request"):
        request.validate_result(replace(result, request_hash="a" * 64))


@pytest.mark.parametrize(
    ("request_factory", "result_factory"),
    [
        (_binding_request, _binding_result),
        (_import_request, _import_result),
        (_query_request, _query_result),
        (_successor_request, _successor_result),
    ],
)
def test_effect_result_identity_is_stable_across_delivery_retries(
    request_factory,
    result_factory,
) -> None:
    first_request = request_factory(delivery_epoch=1)
    retry_request = request_factory(delivery_epoch=2)
    first = result_factory(first_request)
    retry = result_factory(retry_request)

    assert first_request.request_hash == retry_request.request_hash
    assert first.result_identity == retry.result_identity
    assert first.to_dict()["delivery_epoch"] == 1
    assert retry.to_dict()["delivery_epoch"] == 2


def test_import_result_defensively_copies_manifest_and_binds_content_identity() -> None:
    request = _import_request()
    manifest = {"source_rows": 1, "chunks": 2, "directories": 1}
    result = SourceImportResult(
        request.operation_id,
        request.delivery_epoch,
        request.request_hash,
        request.source_id,
        EngineReference("source", "paper:lingbot-video"),
        manifest,
    )
    identity = result.result_identity
    manifest["source_rows"] = -1
    assert result.manifest["source_rows"] == 1
    assert result.result_identity == identity
    with pytest.raises(TypeError):
        result.manifest["chunks"] = 99  # type: ignore[index]
    request.validate_result(result)
    assert SourceImportResult.from_dict(result.to_dict()) == result

    tampered = result.to_dict()
    tampered["manifest"]["chunks"] = 99
    with pytest.raises(ValueError, match="identity does not match"):
        SourceImportResult.from_dict(tampered)


def test_lineage_query_has_a_durable_result_identity_and_deep_copy() -> None:
    sources = ["source-echo", "source-lingbot"]
    request = LineageQueryRequest(
        "query-lineage-golden",
        1,
        "run-golden-001",
        sources,
        "Helios-14B memory",
    )
    sources.clear()
    assert request.source_ids == ("source-echo", "source-lingbot")
    result = LineageQueryResult(
        request.operation_id,
        request.delivery_epoch,
        request.request_hash,
        list(_nodes()),
    )
    request.validate_result(result)
    assert LineageQueryResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="canonically ordered"):
        replace(result, nodes=tuple(reversed(result.nodes)))


def test_successor_result_requires_exact_parent_targets_and_relation() -> None:
    request = _successor_request()
    result = _successor_result(request)
    request.validate_result(result)
    assert SuccessorCreationResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="exactly cover"):
        replace(
            result,
            parent_node_ids=("lineage-echo-memory-graduated",),
        )
    with pytest.raises(ValueError, match="exactly cover"):
        replace(
            result,
            parent_node_ids=(
                "lineage-echo-memory-graduated",
                "lineage-extra",
                "lineage-helios-ttt-dormant",
            ),
        )
    with pytest.raises(ValueError, match="reuse links"):
        replace(
            result,
            links=(
                replace(result.links[0], relation="related_to"),
                result.links[1],
            ),
        )


@pytest.mark.parametrize("disposition", ["not_found", "unknown"])
def test_reconciliation_models_safe_absence_and_unknown_outcomes(
    disposition: str,
) -> None:
    effect = _import_request(delivery_epoch=3)
    request = EffectReconciliationRequest(effect, effect.delivery_epoch)
    result = EffectReconciliationResult(
        request.domain,
        request.operation_id,
        request.request_hash,
        request.delivery_epoch,
        disposition,
    )
    request.validate_result(result)
    assert EffectReconciliationRequest.from_dict(request.to_dict()) == request
    assert EffectReconciliationResult.from_dict(result.to_dict()) == result


def test_committed_reconciliation_carries_and_validates_the_typed_result() -> None:
    effect = _binding_request(delivery_epoch=1)
    committed = _binding_result(effect)
    request = EffectReconciliationRequest(effect, 3)
    result = EffectReconciliationResult(
        request.domain,
        request.operation_id,
        request.request_hash,
        3,
        "committed",
        committed,
    )
    request.validate_result(result)
    assert EffectReconciliationResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="typed result"):
        replace(result, result=None)
    with pytest.raises(ValueError, match="identity does not match"):
        request.validate_result(replace(result, request_hash="b" * 64))

    conflicting = replace(committed, source_id="source-other")
    malicious = replace(result, result=conflicting)
    with pytest.raises(ValueError, match="does not match its request"):
        request.validate_result(malicious)


def test_reconciliation_rejects_stale_and_cross_domain_results() -> None:
    effect = _binding_request(delivery_epoch=2)
    request = EffectReconciliationRequest(effect, effect.delivery_epoch)
    stale = EffectReconciliationResult(
        request.domain,
        request.operation_id,
        request.request_hash,
        1,
        "unknown",
    )
    with pytest.raises(ValueError, match="does not match its request"):
        request.validate_result(stale)
    future = replace(stale, delivery_epoch=3)
    with pytest.raises(ValueError, match="does not match its request"):
        request.validate_result(future)
    with pytest.raises(ValueError, match="identity does not match"):
        EffectReconciliationResult(
            request.domain,
            request.operation_id,
            request.request_hash,
            2,
            "committed",
            replace(_binding_result(effect), operation_id="operation-other"),
        )
    with pytest.raises(ValueError, match="typed result"):
        EffectReconciliationResult(
            "source_import",
            request.operation_id,
            request.request_hash,
            2,
            "committed",
            _binding_result(effect),
        )
