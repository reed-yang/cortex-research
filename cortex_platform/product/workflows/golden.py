"""Public facade for the sealed golden research workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from cortex_platform.product.control import ControlStore

from cortex_platform.product.control.errors import InvalidTransition

from .coordinator import (
    WorkflowCoordinator,
    WorkflowDecisionPlan,
    WorkflowEffectPlan,
    stable_attempt_operation_id,
    stable_operation_id,
    workflow_input_hash,
)
from .models import (
    GOLDEN_RESEARCH_WORKFLOW,
    ArtifactWorkflowRequest,
    LineageQueryRequest,
    RuntimeStageRequest,
    SourceBindingRequest,
    SourceImportRequest,
    SuccessorCreationRequest,
)
from .ports import ArtifactWorkflowPort, ResearchEnginePort, RuntimeStagePort

GoldenBlock = Literal[
    "source_decision",
    "source_import",
    "lineage_decision",
    "paused",
    "manual_recovery",
    "terminal",
]

_TERMINAL_RUN_STATES = frozenset(
    {"cancel_requested", "canceled", "completed", "failed"}
)


def _sha256(value: str, field: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class GoldenArtifactPlans:
    """Exact plan identities for the four frozen artifact operations."""

    evidence_sha256: str
    living_sha256: str
    training_sha256: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "evidence_sha256",
            "living_sha256",
            "training_sha256",
            "snapshot_sha256",
        ):
            _sha256(getattr(self, field), field)


@dataclass(frozen=True)
class GoldenWorkflowCase:
    """Immutable invocation data for one golden workflow run."""

    source_intent_id: str
    lineage_query: str
    successor_title: str
    artifact_plans: GoldenArtifactPlans

    def __post_init__(self) -> None:
        for field in ("source_intent_id", "lineage_query", "successor_title"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be non-empty text")

    @property
    def identity(self) -> str:
        value = {
            "artifact_plans": {
                "evidence": self.artifact_plans.evidence_sha256,
                "living": self.artifact_plans.living_sha256,
                "snapshot": self.artifact_plans.snapshot_sha256,
                "training": self.artifact_plans.training_sha256,
            },
            "lineage_query": self.lineage_query,
            "source_intent_id": self.source_intent_id,
            "successor_title": self.successor_title,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class GoldenWorkflowProjection:
    """Small durable-state projection returned at a workflow boundary."""

    run_id: str
    attempt_id: str | None
    workflow_id: str
    workflow_state: str
    current_stage_key: str | None
    blocked_on: GoldenBlock | None
    decision_ids: tuple[str, ...] = ()


class GoldenWorkflowFacade:
    """Map the sealed golden graph onto ``WorkflowCoordinator`` requests."""

    def __init__(
        self,
        *,
        store: ControlStore,
        engine: ResearchEnginePort,
        runtime: RuntimeStagePort | None,
        artifacts: ArtifactWorkflowPort | None,
        worker_id: str,
        case: GoldenWorkflowCase,
    ) -> None:
        self._store = store
        self._case = case
        self._coordinator = WorkflowCoordinator(
            store=store,
            engine=engine,
            runtime=runtime,
            artifacts=artifacts,
            worker_id=worker_id,
        )

    def start(self, run_id: str) -> GoldenWorkflowProjection:
        """Install the sealed graph idempotently and advance to its first block."""
        self._store.install_workflow(
            run_id=run_id,
            definition=GOLDEN_RESEARCH_WORKFLOW,
        )
        return self.advance_until_blocked(run_id)

    def advance_until_blocked(self, run_id: str) -> GoldenWorkflowProjection:
        """Advance from durable Control state until external input is required."""
        while True:
            workflow = self._store.get_workflow_for_run(run_id)
            run = self._store.get_run(run_id)
            self._validate_case(workflow)
            if run["state"] == "paused":
                return self._projection(run, workflow, "paused")
            if workflow["state"] == "manual_recovery" or self._manual_required(
                workflow
            ):
                return self._projection(run, workflow, "manual_recovery")
            if workflow["state"] == "completed" or run["state"] in _TERMINAL_RUN_STATES:
                return self._projection(run, workflow, "terminal")

            stage_key = workflow["current_stage_key"]
            if stage_key == "resolve_sources":
                self._coordinator.prepare_stage(
                    workflow_id=workflow["id"],
                    input_value={
                        "case_identity": self._case.identity,
                        "source_intent_id": self._case.source_intent_id,
                    },
                )
                self._coordinator.recover(workflow_id=workflow["id"])
                continue
            if stage_key == "await_source_decision":
                intent = self._store.get_source_intent(self._case.source_intent_id)
                if intent["run_id"] != run_id:
                    raise ValueError("source intent does not belong to the run")
                decision = intent["decision"]
                if intent["state"] == "pending":
                    return self._projection(
                        run,
                        workflow,
                        "source_decision",
                        (str(decision["id"]),),
                    )
                self._advance_source_decision(workflow, intent)
                continue
            if stage_key == "import_sources":
                if not self._advance_source_imports(run, workflow):
                    return self._projection(run, workflow, "source_import")
                continue
            if stage_key == "retrieve_lineage":
                self._advance_lineage_query(run, workflow)
                continue
            if stage_key == "await_lineage_decision":
                decision_ids = self._advance_lineage_decision(workflow)
                if decision_ids:
                    return self._projection(
                        run, workflow, "lineage_decision", decision_ids
                    )
                continue
            if stage_key == "create_successor":
                self._advance_successor(run, workflow)
                continue
            if stage_key in {
                "research_evidence",
                "research_architecture",
                "research_training_plan",
            }:
                self._advance_runtime(run, workflow, str(stage_key))
                continue
            if stage_key == "publish_living_artifacts":
                self._advance_publication(run, workflow)
                continue
            if stage_key == "freeze_snapshot":
                self._advance_snapshot(run, workflow)
                continue
            raise NotImplementedError(f"golden stage is not mapped: {stage_key}")

    def recover(self, run_id: str) -> GoldenWorkflowProjection:
        """Recover the durable current stage, then continue through the mapper."""
        workflow = self._store.get_workflow_for_run(run_id)
        run = self._store.get_run(run_id)
        if (
            run["state"] != "paused"
            and workflow["state"] != "manual_recovery"
            and not self._manual_required(workflow)
            and workflow["state"] != "completed"
            and run["state"] not in _TERMINAL_RUN_STATES
        ):
            self._coordinator.recover(workflow_id=workflow["id"])
        return self.advance_until_blocked(run_id)

    def _advance_source_decision(
        self, workflow: Mapping[str, Any], intent: Mapping[str, Any]
    ) -> None:
        decision = intent["decision"]
        resolution = decision.get("resolution") or {}
        choice = resolution.get("choice")
        if not isinstance(choice, str) or not choice:
            raise ValueError("resolved source intent has no durable choice")
        input_value = self._input(
            source_decision_id=decision["id"],
            source_choice=choice,
        )
        resolved = self._decisions(
            workflow["run_id"], "source_selection_committed", "resolved"
        )
        if resolved:
            self._coordinator.recover(workflow_id=str(workflow["id"]))
            return
        self._coordinator.prepare_stage(
            workflow_id=str(workflow["id"]),
            input_value=input_value,
            decision=WorkflowDecisionPlan(
                kind="source_selection_committed",
                prompt="Record the durable Source identity choice for this workflow.",
                options=tuple(decision["options"]),
            ),
        )
        pending = self._decisions(
            workflow["run_id"], "source_selection_committed", "pending"
        )
        if len(pending) != 1:
            raise ValueError("source workflow decision is ambiguous")
        value = pending[0]
        self._store.resolve_decision(
            decision_id=str(value["id"]),
            choice=choice,
            expected_revision=int(value["revision"]),
            actor_id="golden-workflow",
            idempotency_key=f"golden-source-choice-{workflow['id']}",
        )
        self._coordinator.recover(workflow_id=str(workflow["id"]))

    def _advance_source_imports(
        self, run: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> bool:
        bindings = self._store.list_run_source_bindings(str(run["id"]))
        intent = self._store.get_source_intent(self._case.source_intent_id)
        choice = ((intent.get("decision") or {}).get("resolution") or {}).get("choice")
        if choice != "keep_both":
            raise ValueError(
                "golden workflow requires one reused and one imported source"
            )
        if len(bindings) < 2:
            bound_canonical_ids = {
                str(self._store.get_source(str(binding["source_id"]))["canonical_id"])
                for binding in bindings
            }
            missing_canonical_ids = {
                str(candidate["canonical_id"])
                for candidate in intent["candidates"]
                if str(candidate["canonical_id"]) not in bound_canonical_ids
            }
            actions = [
                action
                for action in self._store.list_all_source_imports()
                if action["canonical_id"] in missing_canonical_ids
            ]
            if any(action["state"] == "pending" for action in actions):
                return False
            raise ValueError(
                "golden workflow requires one reused and one imported source"
            )
        if len(bindings) != 2 or sorted(
            str(binding["disposition"]) for binding in bindings
        ) != ["imported", "reused"]:
            raise ValueError(
                "golden workflow requires one reused and one imported source"
            )
        sources = [
            self._store.get_source(str(binding["source_id"])) for binding in bindings
        ]
        if any(
            source["import_state"] not in {"existing", "imported"}
            or not source.get("engine_ref")
            for source in sources
        ):
            return False
        manifest = sorted(
            (
                str(binding["disposition"]),
                str(source["id"]),
                str(source["canonical_id"]),
                str(source["engine_ref"]),
            )
            for binding, source in zip(bindings, sources, strict=True)
        )
        input_value = self._input(source_manifest=manifest)
        plans: list[WorkflowEffectPlan] = []
        for binding, source in zip(bindings, sources, strict=True):
            disposition = str(binding["disposition"])
            effect_key = disposition
            operation_id = stable_operation_id(
                str(workflow["id"]), "import_sources", effect_key
            )
            request = (
                SourceBindingRequest(
                    operation_id=operation_id,
                    delivery_epoch=1,
                    run_id=str(run["id"]),
                    source_id=str(source["id"]),
                    canonical_id=str(source["canonical_id"]),
                    disposition="reused",
                )
                if disposition == "reused"
                else SourceImportRequest(
                    operation_id=operation_id,
                    delivery_epoch=1,
                    source_id=str(source["id"]),
                    canonical_id=str(source["canonical_id"]),
                )
            )
            plans.append(WorkflowEffectPlan(effect_key, request))
        plans.sort(key=lambda plan: plan.effect_key, reverse=True)
        self._prepare_recover(workflow, input_value, tuple(plans))
        return True

    def _advance_lineage_query(
        self, run: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> None:
        source_ids = tuple(
            sorted(
                str(binding["source_id"])
                for binding in self._store.list_run_source_bindings(str(run["id"]))
            )
        )
        input_value = self._input(
            source_ids=source_ids,
            lineage_query=self._case.lineage_query,
        )
        request = LineageQueryRequest(
            operation_id=stable_operation_id(
                str(workflow["id"]), "retrieve_lineage", "lineage"
            ),
            delivery_epoch=1,
            run_id=str(run["id"]),
            source_ids=source_ids,
            query=self._case.lineage_query,
        )
        self._prepare_recover(
            workflow,
            input_value,
            (WorkflowEffectPlan("lineage", request),),
        )

    def _advance_lineage_decision(self, workflow: Mapping[str, Any]) -> tuple[str, ...]:
        receipt = self._only_receipt(str(workflow["id"]), "retrieve_lineage")
        nodes = receipt.get("nodes") or []
        node_manifest = [
            (node["node_id"], node["status"], node["revision"]) for node in nodes
        ]
        input_value = self._input(lineage_nodes=node_manifest)
        resolved = self._decisions(
            workflow["run_id"], "golden_lineage_selection", "resolved"
        )
        if resolved:
            if (
                len(resolved) != 1
                or (resolved[0].get("resolution") or {}).get("choice") != "proceed"
            ):
                raise ValueError("golden lineage decision did not authorize successor")
            self._coordinator.recover(workflow_id=str(workflow["id"]))
            return ()
        self._coordinator.prepare_stage(
            workflow_id=str(workflow["id"]),
            input_value=input_value,
            decision=WorkflowDecisionPlan(
                kind="golden_lineage_selection",
                prompt="Create one successor from the exact retrieved lineage?",
                options=(
                    {"id": "proceed", "label": "Create successor"},
                    {"id": "cancel", "label": "Stop"},
                ),
            ),
        )
        pending = self._decisions(
            workflow["run_id"], "golden_lineage_selection", "pending"
        )
        if len(pending) != 1:
            raise ValueError("lineage workflow decision is ambiguous")
        return (str(pending[0]["id"]),)

    def _advance_successor(
        self, run: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> None:
        receipt = self._only_receipt(str(workflow["id"]), "retrieve_lineage")
        parent_ids = tuple(sorted(str(node["node_id"]) for node in receipt["nodes"]))
        input_value = self._input(
            parent_node_ids=parent_ids,
            successor_title=self._case.successor_title,
        )
        request = SuccessorCreationRequest(
            operation_id=stable_operation_id(
                str(workflow["id"]), "create_successor", "successor"
            ),
            delivery_epoch=1,
            run_id=str(run["id"]),
            title=self._case.successor_title,
            parent_node_ids=parent_ids,
        )
        self._prepare_recover(
            workflow,
            input_value,
            (WorkflowEffectPlan("successor", request),),
        )

    def _advance_runtime(
        self,
        run: Mapping[str, Any],
        workflow: Mapping[str, Any],
        stage_key: str,
    ) -> None:
        effect_key, effect_kind = {
            "research_evidence": ("evidence", "runtime_evidence"),
            "research_architecture": ("architecture", "runtime_architecture"),
            "research_training_plan": ("training", "runtime_training_plan"),
        }[stage_key]
        attempt_id = self._active_attempt(run)
        dependency_ids = self._dependency_result_ids(workflow, stage_key)
        input_value = self._input(
            stage_key=stage_key,
            dependency_result_ids=dependency_ids,
        )
        request = RuntimeStageRequest(
            operation_id=stable_attempt_operation_id(
                str(workflow["id"]), stage_key, effect_key, attempt_id
            ),
            delivery_epoch=1,
            run_id=str(run["id"]),
            attempt_id=attempt_id,
            stage_key=stage_key,
            effect_kind=effect_kind,
            stage_input_hash=workflow_input_hash(input_value),
            dependency_result_ids=dependency_ids,
        )
        self._prepare_recover(
            workflow,
            input_value,
            (WorkflowEffectPlan(effect_key, request),),
        )

    def _advance_publication(
        self, run: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> None:
        stage_key = "publish_living_artifacts"
        attempt_id = self._active_attempt(run)
        dependency_ids = self._dependency_result_ids(workflow, stage_key)
        input_value = self._input(dependency_result_ids=dependency_ids)
        configured = (
            (
                "evidence",
                "artifact_evidence",
                self._case.artifact_plans.evidence_sha256,
            ),
            ("living", "artifact_living", self._case.artifact_plans.living_sha256),
            (
                "training",
                "artifact_training",
                self._case.artifact_plans.training_sha256,
            ),
        )
        plans = tuple(
            WorkflowEffectPlan(
                effect_key,
                ArtifactWorkflowRequest(
                    operation_id=stable_attempt_operation_id(
                        str(workflow["id"]), stage_key, effect_key, attempt_id
                    ),
                    delivery_epoch=1,
                    run_id=str(run["id"]),
                    attempt_id=attempt_id,
                    stage_key=stage_key,
                    effect_kind=effect_kind,
                    stage_input_hash=workflow_input_hash(input_value),
                    dependency_result_ids=dependency_ids,
                    plan_id=f"golden.{effect_key}",
                    plan_hash=plan_hash,
                ),
            )
            for effect_key, effect_kind, plan_hash in configured
        )
        self._prepare_recover(workflow, input_value, plans)

    def _advance_snapshot(
        self, run: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> None:
        stage_key = "freeze_snapshot"
        attempt_id = self._active_attempt(run)
        dependency_ids = self._dependency_result_ids(workflow, stage_key)
        members = self._publication_members(str(workflow["id"]), attempt_id)
        input_value = self._input(
            dependency_result_ids=dependency_ids,
            snapshot_member_version_ids=members,
        )
        request = ArtifactWorkflowRequest(
            operation_id=stable_attempt_operation_id(
                str(workflow["id"]), stage_key, "snapshot", attempt_id
            ),
            delivery_epoch=1,
            run_id=str(run["id"]),
            attempt_id=attempt_id,
            stage_key=stage_key,
            effect_kind="artifact_snapshot",
            stage_input_hash=workflow_input_hash(input_value),
            dependency_result_ids=dependency_ids,
            plan_id="golden.snapshot",
            plan_hash=self._case.artifact_plans.snapshot_sha256,
            snapshot_member_version_ids=members,
        )
        self._prepare_recover(
            workflow,
            input_value,
            (WorkflowEffectPlan("snapshot", request),),
        )

    def _prepare_recover(
        self,
        workflow: Mapping[str, Any],
        input_value: Mapping[str, Any],
        effects: tuple[WorkflowEffectPlan, ...],
    ) -> None:
        self._coordinator.prepare_stage(
            workflow_id=str(workflow["id"]),
            input_value=input_value,
            effects=effects,
        )
        self._coordinator.recover(workflow_id=str(workflow["id"]))

    def _dependency_result_ids(
        self, workflow: Mapping[str, Any], stage_key: str
    ) -> tuple[str, ...]:
        stage = next(
            value for value in workflow["stages"] if value["stage_key"] == stage_key
        )
        identities = [
            str(effect["result_identity"])
            for dependency in stage["dependencies"]
            for effect in self._store.list_workflow_effects(
                workflow_id=str(workflow["id"]), stage_key=str(dependency)
            )
            if effect["state"] == "completed"
        ]
        return tuple(sorted(identities))

    def _only_receipt(self, workflow_id: str, stage_key: str) -> dict[str, Any]:
        effects = self._store.list_workflow_effects(
            workflow_id=workflow_id, stage_key=stage_key
        )
        receipts = [
            effect["receipt"] for effect in effects if effect["state"] == "completed"
        ]
        if len(receipts) != 1 or not isinstance(receipts[0], dict):
            raise ValueError(f"golden {stage_key} receipt is missing or ambiguous")
        return receipts[0]

    def _publication_members(
        self, workflow_id: str, attempt_id: str
    ) -> tuple[str, ...]:
        expected = {
            "artifact_evidence": "evidence-matrix",
            "artifact_living": "living-brief",
            "artifact_training": "training-plan",
        }
        selected: dict[str, tuple[int, str]] = {}
        owners: dict[str, str] = {}
        effects = self._store.list_workflow_effects(
            workflow_id=workflow_id, stage_key="publish_living_artifacts"
        )
        for effect in effects:
            if effect["state"] != "completed" or effect["effect_kind"] not in expected:
                continue
            request = effect["request"]
            if request.attempt_id != attempt_id:
                continue
            for version_id in effect["receipt"]["artifact_version_ids"]:
                version = self._store.get_artifact_version(str(version_id))
                artifact = self._store.get_artifact(str(version["artifact_id"]))
                kind = str(artifact["kind"])
                if kind != expected[str(effect["effect_kind"])] or (
                    version["state"] != "committed"
                    or version["attempt_id"] != attempt_id
                ):
                    raise ValueError("publication artifact identity is invalid")
                artifact_id = str(artifact["id"])
                if kind in owners and owners[kind] != artifact_id:
                    raise ValueError("publication artifact kind is ambiguous")
                owners[kind] = artifact_id
                candidate = (int(version["logical_version"]), str(version["id"]))
                selected[kind] = max(selected.get(kind, candidate), candidate)
        if set(selected) != set(expected.values()):
            raise ValueError("golden publication members are incomplete")
        return tuple(sorted(version_id for _, version_id in selected.values()))

    def _decisions(self, run_id: str, kind: str, state: str) -> list[dict]:
        return [
            decision
            for decision in self._store.list_decisions(state=state)
            if decision["run_id"] == run_id and decision["kind"] == kind
        ]

    def _manual_required(self, workflow: Mapping[str, Any]) -> bool:
        return any(
            effect["state"] == "manual_required"
            for effect in self._store.list_workflow_effects(
                workflow_id=str(workflow["id"])
            )
        )

    def _validate_case(self, workflow: Mapping[str, Any]) -> None:
        stage = next(
            value
            for value in workflow["stages"]
            if value["stage_key"] == "resolve_sources"
        )
        if stage["state"] == "ready":
            return
        expected = workflow_input_hash(
            self._input(source_intent_id=self._case.source_intent_id)
        )
        if stage["input_hash"] != expected:
            raise InvalidTransition("golden_case_drift", "workflow_replay")

    def _input(self, **values: Any) -> dict[str, Any]:
        return {"case_identity": self._case.identity, **values}

    @staticmethod
    def _active_attempt(run: Mapping[str, Any]) -> str:
        attempt_id = run.get("active_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("golden workflow has no active attempt")
        return attempt_id

    @staticmethod
    def _projection(
        run: dict,
        workflow: dict,
        blocked_on: GoldenBlock | None,
        decision_ids: tuple[str, ...] = (),
    ) -> GoldenWorkflowProjection:
        return GoldenWorkflowProjection(
            run_id=str(run["id"]),
            attempt_id=(
                str(run["active_attempt_id"])
                if run["active_attempt_id"] is not None
                else None
            ),
            workflow_id=str(workflow["id"]),
            workflow_state=str(workflow["state"]),
            current_stage_key=(
                str(workflow["current_stage_key"])
                if workflow["current_stage_key"] is not None
                else None
            ),
            blocked_on=blocked_on,
            decision_ids=decision_ids,
        )
