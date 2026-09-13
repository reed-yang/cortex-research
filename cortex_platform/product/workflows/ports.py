"""Injected research-engine boundary for product workflow coordination."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectReconciliationRequest,
    EffectReconciliationResult,
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
)


class ArtifactWorkflowPort(Protocol):
    """Execute one durable, content-addressed artifact publication plan."""

    def execute_artifact(
        self, request: ArtifactWorkflowRequest
    ) -> ArtifactWorkflowResult: ...

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        """Recover the exact artifact plan after an uncertain delivery."""
        ...


class ResearchEnginePort(Protocol):
    """Typed engine operations over identities already owned by Cortex."""

    def bind_source(self, request: SourceBindingRequest) -> SourceBindingResult: ...

    def import_source(self, request: SourceImportRequest) -> SourceImportResult: ...

    def query_lineage(self, request: LineageQueryRequest) -> LineageQueryResult: ...

    def create_successor(
        self, request: SuccessorCreationRequest
    ) -> SuccessorCreationResult: ...

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        """Query a stable operation before any uncertain effect is redelivered."""
        ...


class RuntimeStagePort(Protocol):
    """Typed runtime-stage boundary owned by the workflow coordinator."""

    def execute_stage(self, request: RuntimeStageRequest) -> RuntimeStageResult: ...

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        """Query a stable runtime operation after an uncertain delivery."""
        ...
