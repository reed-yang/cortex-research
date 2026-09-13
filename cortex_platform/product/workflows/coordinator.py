"""Restart-safe coordination for durable research workflow effects."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from cortex_platform.product.control.errors import InvalidTransition, RevisionConflict

if TYPE_CHECKING:
    from cortex_platform.product.control import ControlStore

from .models import (
    ArtifactWorkflowRequest,
    EffectReconciliationRequest,
    EffectRequest,
    LineageQueryRequest,
    RuntimeStageRequest,
    SourceBindingRequest,
    SourceImportRequest,
    SuccessorCreationRequest,
)
from .ports import ArtifactWorkflowPort, ResearchEnginePort, RuntimeStagePort

_FENCED_RUN_STATES = frozenset(
    {"cancel_requested", "canceled", "completed", "failed"}
)
_FAILURE_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_RUNTIME_ATTEMPT_MARKER = "::attempt::"


class EffectOutcomeUnknown(RuntimeError):
    """An adapter committed or may have committed but lost the reply."""


class EffectPermanentlyRejected(RuntimeError):
    """An adapter rejected an effect without an ambiguous external outcome."""

    def __init__(self, category: str) -> None:
        if (
            not isinstance(category, str)
            or _FAILURE_CATEGORY_RE.fullmatch(category) is None
            or category == "outcome_unknown"
        ):
            raise ValueError("permanent rejection category is invalid")
        super().__init__(category)
        self.category = category


def workflow_input_hash(value: object) -> str:
    """Return the canonical hash persisted for one workflow stage input."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_operation_id(workflow_id: str, stage_key: str, effect_key: str) -> str:
    """Return the stable external-operation identity for one planned effect."""
    identity = workflow_input_hash(
        {"workflow_id": workflow_id, "stage_key": stage_key, "effect_key": effect_key}
    )
    return f"workflow-effect:{identity}"


def stable_runtime_operation_id(
    workflow_id: str,
    stage_key: str,
    effect_key: str,
    attempt_id: str,
) -> str:
    """Return the dispatch identity for one logical effect on one attempt."""
    return stable_attempt_operation_id(
        workflow_id, stage_key, effect_key, attempt_id
    )


def stable_attempt_operation_id(
    workflow_id: str,
    stage_key: str,
    effect_key: str,
    attempt_id: str,
) -> str:
    """Return a stable identity for an attempt-bound workflow effect."""
    return stable_operation_id(
        workflow_id,
        stage_key,
        f"{effect_key}{_RUNTIME_ATTEMPT_MARKER}{attempt_id}",
    )


def _attempt_command_key(effect_key: str, attempt_id: str) -> str:
    if _RUNTIME_ATTEMPT_MARKER in effect_key:
        raise ValueError("attempt-bound effect_key uses a reserved marker")
    return f"{effect_key}{_RUNTIME_ATTEMPT_MARKER}{attempt_id}"


@dataclass(frozen=True)
class WorkflowEffectPlan:
    """One exact durable effect to create while activating a stage."""

    effect_key: str
    request: EffectRequest

    def __post_init__(self) -> None:
        if not isinstance(self.effect_key, str) or not self.effect_key:
            raise ValueError("effect_key must be non-empty text")
        if not isinstance(
            self.request,
            (
                SourceBindingRequest,
                SourceImportRequest,
                LineageQueryRequest,
                SuccessorCreationRequest,
                RuntimeStageRequest,
                ArtifactWorkflowRequest,
            ),
        ):
            raise TypeError("request must be a typed workflow effect request")
        if self.request.delivery_epoch != 1:
            raise ValueError("workflow effects must start at initial delivery_epoch 1")


@dataclass(frozen=True)
class WorkflowDecisionPlan:
    """One exact human decision owned by a workflow stage."""

    kind: str
    prompt: str
    options: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("decision kind must be non-empty text")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("decision prompt must be non-empty text")
        if isinstance(self.options, (str, bytes)) or not self.options:
            raise ValueError("decision options must be a non-empty sequence")
        copied: list[Mapping[str, Any]] = []
        for option in self.options:
            if not isinstance(option, Mapping):
                raise TypeError("decision options must be mappings")
            copied.append(MappingProxyType(dict(option)))
        object.__setattr__(self, "options", tuple(copied))


class WorkflowCoordinator:
    """Coordinate durable transitions without spanning external calls."""

    def __init__(
        self,
        *,
        store: ControlStore,
        engine: ResearchEnginePort,
        runtime: RuntimeStagePort | None,
        artifacts: ArtifactWorkflowPort | None = None,
        worker_id: str,
        lease_seconds: int = 30,
        max_mutation_deliveries: int = 2,
    ) -> None:
        if (
            type(max_mutation_deliveries) is not int
            or not 1 <= max_mutation_deliveries <= 100
        ):
            raise ValueError("max_mutation_deliveries must be between 1 and 100")
        self._store = store
        self._engine = engine
        self._runtime = runtime
        self._artifacts = artifacts
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_mutation_deliveries = max_mutation_deliveries

    def prepare_stage(
        self,
        *,
        workflow_id: str,
        input_value: Mapping[str, Any],
        effects: Sequence[WorkflowEffectPlan] = (),
        decision: WorkflowDecisionPlan | None = None,
    ) -> dict[str, Any]:
        """Activate a current stage and idempotently persist its exact effects."""
        workflow = self._store.get_workflow(workflow_id)
        current_stage_key = workflow["current_stage_key"]
        if current_stage_key is None:
            raise InvalidTransition(str(workflow["state"]), "active")
        stage_key = str(current_stage_key)
        stage = next(
            item for item in workflow["stages"] if item["stage_key"] == stage_key
        )
        input_hash = workflow_input_hash(input_value)
        if stage["state"] == "ready":
            active = self._store.activate_workflow_stage(
                workflow_id=workflow_id,
                stage_key=stage_key,
                input_value=input_value,
                expected_workflow_revision=workflow["revision"],
                expected_stage_revision=stage["revision"],
            )
            workflow = active["workflow"]
            stage = active["stage"]
        elif stage["state"] in {"active", "waiting"}:
            if stage["input_hash"] != input_hash:
                raise InvalidTransition("stage_input_drift", "stage_replay")
            if stage["state"] == "waiting" and decision is None:
                raise InvalidTransition("waiting", "decision_replay")
        else:
            raise InvalidTransition(str(stage["state"]), "active")

        if decision is not None:
            if stage["effect"] != "decision" or effects:
                raise ValueError("decision plans require a decision-only stage")
            created = self._store.create_workflow_decision(
                workflow_id=workflow_id,
                stage_key=stage_key,
                expected_workflow_revision=workflow["revision"],
                expected_stage_revision=stage["revision"],
                kind=decision.kind,
                prompt=decision.prompt,
                options=decision.options,
            )
            return created["workflow"]
        if stage["effect"] == "decision":
            raise ValueError("decision stages require a typed decision plan")

        for plan in effects:
            command_key = plan.effect_key
            if isinstance(
                plan.request, (RuntimeStageRequest, ArtifactWorkflowRequest)
            ):
                command_key = _attempt_command_key(
                    plan.effect_key, plan.request.attempt_id
                )
                prefix = f"{plan.effect_key}{_RUNTIME_ATTEMPT_MARKER}"
                matching = [
                    effect
                    for effect in self._store.list_workflow_effects(
                        workflow_id=workflow_id, stage_key=stage_key
                    )
                    if effect["effect_kind"] == plan.request.effect_kind
                    and (
                        effect["effect_key"] == plan.effect_key
                        or str(effect["effect_key"]).startswith(prefix)
                    )
                ]
                current = [
                    effect
                    for effect in matching
                    if isinstance(
                        effect["request"],
                        (RuntimeStageRequest, ArtifactWorkflowRequest),
                    )
                    and effect["request"].attempt_id == plan.request.attempt_id
                ]
                if len(current) > 1:
                    raise InvalidTransition(
                        "runtime_attempt_ambiguous", "effect_replay"
                    )
                if current:
                    command_key = str(current[0]["effect_key"])
                expected_operation_id = stable_attempt_operation_id(
                    workflow_id,
                    stage_key,
                    plan.effect_key,
                    plan.request.attempt_id,
                )
                legacy_operation_id = stable_operation_id(
                    workflow_id, stage_key, plan.effect_key
                )
                if plan.request.operation_id != expected_operation_id and not (
                    plan.request.operation_id == legacy_operation_id
                    and (not matching or current)
                ):
                    raise ValueError("workflow effect operation_id is not stable")
                for effect in matching:
                    stored_request = effect["request"]
                    if (
                        isinstance(
                            stored_request,
                            (RuntimeStageRequest, ArtifactWorkflowRequest),
                        )
                        and stored_request.attempt_id != plan.request.attempt_id
                    ):
                        self._store.supersede_workflow_attempt_effect(
                            effect_id=str(effect["id"]),
                            replacement_attempt_id=plan.request.attempt_id,
                        )
            else:
                expected_operation_id = stable_operation_id(
                    workflow_id, stage_key, plan.effect_key
                )
                if plan.request.operation_id != expected_operation_id:
                    raise ValueError("workflow effect operation_id is not stable")
            self._store.create_workflow_effect(
                workflow_id=workflow_id,
                stage_key=stage_key,
                effect_key=command_key,
                request=plan.request,
                expected_workflow_revision=workflow["revision"],
                expected_stage_revision=stage["revision"],
                expected_stage_input_hash=stage["input_hash"],
            )
        return self._store.get_workflow(workflow_id)

    def recover(self, *, workflow_id: str) -> dict[str, Any]:
        """Dispatch recoverable effects, then commit a satisfied active stage."""
        workflow = self._store.get_workflow(workflow_id)
        effects = {
            str(effect["id"]): effect
            for effect in self._store.list_workflow_effects(workflow_id=workflow_id)
        }
        classifications = [
            classification
            for classification in self._store.classify_workflow_recovery()
            if classification["workflow_id"] == workflow_id
            and str(classification["effect_id"]) in effects
        ]
        run = self._store.get_run(str(workflow["run_id"]))
        active_attempt_id = run["active_attempt_id"]
        retained_classifications: list[dict[str, Any]] = []
        for classification in classifications:
            effect_id = str(classification["effect_id"])
            request = effects[effect_id]["request"]
            if (
                classification["classification"] == "pending"
                and active_attempt_id is not None
                and isinstance(
                    request, (RuntimeStageRequest, ArtifactWorkflowRequest)
                )
                and request.attempt_id != active_attempt_id
            ):
                effects[effect_id] = self._store.supersede_workflow_attempt_effect(
                    effect_id=effect_id,
                    replacement_attempt_id=str(active_attempt_id),
                )
                continue
            retained_classifications.append(classification)
        classifications = retained_classifications
        if run["state"] == "paused":
            classifications = [
                classification
                for classification in classifications
                if not (
                    classification["classification"] == "pending"
                    and isinstance(
                        effects[str(classification["effect_id"])]["request"],
                        (RuntimeStageRequest, ArtifactWorkflowRequest),
                    )
                )
            ]
        if self._runtime is None and any(
            classification["classification"] in {"pending", "unknown"}
            and isinstance(
                effects[str(classification["effect_id"])]["request"],
                RuntimeStageRequest,
            )
            for classification in classifications
        ):
            raise RuntimeError("runtime-stage adapter is not configured")
        if self._artifacts is None and any(
            classification["classification"] in {"pending", "unknown"}
            and isinstance(
                effects[str(classification["effect_id"])]["request"],
                ArtifactWorkflowRequest,
            )
            for classification in classifications
        ):
            raise RuntimeError("artifact workflow adapter is not configured")
        for classification in classifications:
            effect_id = str(classification["effect_id"])
            if classification["classification"] == "pending":
                self._dispatch(
                    effect_id,
                    workflow_id=workflow_id,
                    previous=effects[effect_id],
                )
            elif classification["classification"] == "unknown":
                self._reconcile(
                    effect_id,
                    state=str(classification["state"]),
                    workflow_id=workflow_id,
                    previous=effects[effect_id],
                )
            elif (
                classification["classification"] == "terminal"
                and effects[effect_id]["state"] == "failed"
                and effects[effect_id]["failure_category"]
                not in {
                    "runtime_attempt_superseded",
                    "artifact_attempt_superseded",
                }
            ):
                self._store.settle_workflow_effect_failure(effect_id=effect_id)
        run = self._store.get_run(str(workflow["run_id"]))
        if run["state"] not in _FENCED_RUN_STATES:
            self._finish_satisfied_stage(workflow_id)
        return self._store.get_workflow(workflow_id)

    def _effect_progressed(
        self,
        *,
        workflow_id: str,
        effect_id: str,
        previous: Mapping[str, Any],
    ) -> bool:
        current = next(
            effect
            for effect in self._store.list_workflow_effects(
                workflow_id=workflow_id
            )
            if effect["id"] == effect_id
        )
        durable_fields = (
            "state",
            "claim_owner",
            "claim_epoch",
            "delivery_epoch",
            "result_identity",
            "failure_category",
        )
        if any(current[field] != previous[field] for field in durable_fields):
            return True
        workflow = self._store.get_workflow(workflow_id)
        run = self._store.get_run(str(workflow["run_id"]))
        return str(run["state"]) in _FENCED_RUN_STATES

    def _dispatch(
        self,
        effect_id: str,
        *,
        workflow_id: str,
        previous: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            claim = self._store.claim_workflow_effect(
                effect_id=effect_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
        except InvalidTransition:
            if self._effect_progressed(
                workflow_id=workflow_id,
                effect_id=effect_id,
                previous=previous,
            ):
                return dict(previous)
            raise
        request = claim["request"]
        try:
            if isinstance(request, SourceBindingRequest):
                result = self._engine.bind_source(request)
            elif isinstance(request, SourceImportRequest):
                result = self._engine.import_source(request)
            elif isinstance(request, LineageQueryRequest):
                result = self._engine.query_lineage(request)
            elif isinstance(request, SuccessorCreationRequest):
                result = self._engine.create_successor(request)
            elif isinstance(request, RuntimeStageRequest):
                if self._runtime is None:
                    raise RuntimeError("runtime-stage adapter is not configured")
                result = self._runtime.execute_stage(request)
            elif isinstance(request, ArtifactWorkflowRequest):
                if self._artifacts is None:
                    raise RuntimeError("artifact workflow adapter is not configured")
                result = self._artifacts.execute_artifact(request)
            else:
                raise TypeError("unsupported workflow effect request")
        except EffectPermanentlyRejected as exc:
            return self._store.reject_workflow_effect(
                effect_id=effect_id,
                worker_id=self._worker_id,
                claim_epoch=claim["claim_epoch"],
                delivery_epoch=claim["delivery_epoch"],
                failure_category=exc.category,
            )
        except EffectOutcomeUnknown:
            if claim["effect_class"] == "query":
                return claim
            return self._store.mark_workflow_effect_outcome_unknown(
                effect_id=effect_id,
                worker_id=self._worker_id,
                claim_epoch=claim["claim_epoch"],
                delivery_epoch=claim["delivery_epoch"],
            )
        return self._store.complete_workflow_effect(
            effect_id=effect_id,
            worker_id=self._worker_id,
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            result=result,
        )

    def _reconcile(
        self,
        effect_id: str,
        *,
        state: str,
        workflow_id: str,
        previous: Mapping[str, Any],
    ) -> dict[str, Any]:
        if state == "claimed":
            try:
                normalized = self._store.claim_workflow_effect(
                    effect_id=effect_id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except InvalidTransition:
                if self._effect_progressed(
                    workflow_id=workflow_id,
                    effect_id=effect_id,
                    previous=previous,
                ):
                    return dict(previous)
                raise
            if normalized["state"] != "outcome_unknown":
                return normalized
            previous = normalized
        try:
            claim = self._store.claim_workflow_effect_reconciliation(
                effect_id=effect_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
        except InvalidTransition:
            if self._effect_progressed(
                workflow_id=workflow_id,
                effect_id=effect_id,
                previous=previous,
            ):
                return dict(previous)
            raise
        request = claim["reconciliation_request"]
        if not isinstance(request, EffectReconciliationRequest):
            raise TypeError("reconciliation request must be typed")
        if isinstance(request.effect_request, RuntimeStageRequest):
            if self._runtime is None:
                raise RuntimeError("runtime-stage adapter is not configured")
            result = self._runtime.reconcile_effect(request)
        elif isinstance(request.effect_request, ArtifactWorkflowRequest):
            if self._artifacts is None:
                raise RuntimeError("artifact workflow adapter is not configured")
            result = self._artifacts.reconcile_effect(request)
        else:
            result = self._engine.reconcile_effect(request)
        if (
            result.disposition == "not_found"
            and claim["dispatch_attempt_count"]
            >= self._max_mutation_deliveries
        ):
            return self._store.reject_workflow_effect(
                effect_id=effect_id,
                worker_id=self._worker_id,
                claim_epoch=claim["claim_epoch"],
                delivery_epoch=claim["delivery_epoch"],
                failure_category="redelivery_exhausted",
                reconciliation_result=result,
            )
        return self._store.complete_workflow_effect_reconciliation(
            effect_id=effect_id,
            worker_id=self._worker_id,
            claim_epoch=claim["claim_epoch"],
            delivery_epoch=claim["delivery_epoch"],
            result=result,
        )

    def _finish_satisfied_stage(self, workflow_id: str) -> dict[str, Any] | None:
        workflow = self._store.get_workflow(workflow_id)
        stage_key = workflow["current_stage_key"]
        if stage_key is None:
            return None
        stage = next(
            item for item in workflow["stages"] if item["stage_key"] == stage_key
        )
        if stage["state"] != "active":
            return None
        effects = self._store.list_workflow_effects(
            workflow_id=workflow_id, stage_key=stage_key
        )
        run = self._store.get_run(str(workflow["run_id"]))
        attempt_effects = [
            effect
            for effect in effects
            if isinstance(
                effect["request"],
                (RuntimeStageRequest, ArtifactWorkflowRequest),
            )
        ]
        active_attempt_effects = [
            effect
            for effect in attempt_effects
            if effect["request"].attempt_id == run["active_attempt_id"]
        ]
        if attempt_effects and not active_attempt_effects:
            return None
        if attempt_effects:
            effects = active_attempt_effects
        required = set(stage["required_receipts"]) | set(stage["required_results"])
        completed = {
            str(effect["effect_kind"])
            for effect in effects
            if effect["state"] == "completed"
        }
        if required - completed or any(effect["state"] != "completed" for effect in effects):
            return None
        references: list[dict[str, Any]] = []
        for effect in effects:
            receipt = effect["receipt"] or {}
            if effect["effect_kind"] in {"source_binding", "source_import"}:
                references.append(
                    {
                        "kind": "engine_source",
                        "id": receipt["engine_reference"]["value"],
                        "metadata": {"effect_id": effect["id"]},
                    }
                )
            elif effect["effect_kind"] == "lineage_query":
                references.extend(
                    {
                        "kind": "lineage_node",
                        "id": node["engine_reference"]["value"],
                        "metadata": {"effect_id": effect["id"]},
                    }
                    for node in receipt["nodes"]
                )
            elif effect["effect_kind"] == "successor_creation":
                references.append(
                    {
                        "kind": "lineage_node",
                        "id": receipt["node"]["engine_reference"]["value"],
                        "metadata": {"effect_id": effect["id"]},
                    }
                )
            if isinstance(effect["request"], RuntimeStageRequest):
                references.append(
                    {
                        "kind": "runtime_event",
                        "id": receipt["runtime_event_id"],
                        "metadata": {"effect_id": effect["id"]},
                    }
                )
            elif isinstance(effect["request"], ArtifactWorkflowRequest):
                references.extend(
                    {
                        "kind": "artifact_version",
                        "id": version_id,
                        "metadata": {"effect_id": effect["id"]},
                    }
                    for version_id in receipt["artifact_version_ids"]
                )
                if receipt["snapshot_id"] is not None:
                    references.append(
                        {
                            "kind": "snapshot",
                            "id": receipt["snapshot_id"],
                            "metadata": {"effect_id": effect["id"]},
                        }
                    )
        checkpoint_state = None
        if stage["checkpoint_enabled"]:
            checkpoint_state = {
                "stage_key": stage_key,
                "input_hash": stage["input_hash"],
                "result_identities": sorted(
                    effect["result_identity"] for effect in effects
                ),
            }
        try:
            return self._store.complete_workflow_stage(
                workflow_id=workflow_id,
                stage_key=stage_key,
                expected_workflow_revision=workflow["revision"],
                expected_stage_revision=stage["revision"],
                references=references,
                checkpoint_state=checkpoint_state,
            )
        except (InvalidTransition, RevisionConflict):
            current = self._store.get_workflow(workflow_id)
            current_stage = next(
                item
                for item in current["stages"]
                if item["stage_key"] == stage_key
            )
            run = self._store.get_run(str(current["run_id"]))
            if (
                current_stage["state"] == "completed"
                or current["current_stage_key"] != stage_key
                or str(run["state"]) in _FENCED_RUN_STATES
            ):
                return None
            raise
