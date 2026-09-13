"""Deterministic workflow adapters for temporary-store acceptance tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from cortex_platform.product.workflows.coordinator import EffectOutcomeUnknown
from cortex_platform.product.workflows.models import (
    EffectReconciliationRequest,
    EffectReconciliationResult,
    EffectResult,
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

_Response = EffectResult | Callable[[object], EffectResult]


class DeterministicResearchEngine:
    """Return predeclared typed results and record deterministic deliveries."""

    def __init__(
        self,
        *,
        responses: Mapping[str, _Response],
        before_call: Callable[[], None] | None = None,
        lose_reply_once: set[str] | frozenset[str] = frozenset(),
        unknown_reconciliation: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self._responses = dict(responses)
        self._before_call = before_call
        self._lose_reply_once = set(lose_reply_once)
        self._unknown_reconciliation = set(unknown_reconciliation)
        self._committed: dict[str, EffectResult] = {}
        self.calls: list[tuple[str, int]] = []
        self.reconciliation_calls: list[tuple[str, int]] = []

    def _result(self, request: object) -> EffectResult:
        if self._before_call is not None:
            self._before_call()
        operation_id = request.operation_id
        delivery_epoch = request.delivery_epoch
        self.calls.append((operation_id, delivery_epoch))
        configured = self._responses[operation_id]
        result = configured(request) if callable(configured) else configured
        result = replace(result, delivery_epoch=delivery_epoch)
        request.validate_result(result)
        self._committed[operation_id] = result
        if operation_id in self._lose_reply_once:
            self._lose_reply_once.remove(operation_id)
            raise EffectOutcomeUnknown(operation_id)
        return result

    def bind_source(self, request: SourceBindingRequest) -> SourceBindingResult:
        result = self._result(request)
        assert isinstance(result, SourceBindingResult)
        return result

    def import_source(self, request: SourceImportRequest) -> SourceImportResult:
        result = self._result(request)
        assert isinstance(result, SourceImportResult)
        return result

    def query_lineage(self, request: LineageQueryRequest) -> LineageQueryResult:
        result = self._result(request)
        assert isinstance(result, LineageQueryResult)
        return result

    def create_successor(
        self, request: SuccessorCreationRequest
    ) -> SuccessorCreationResult:
        result = self._result(request)
        assert isinstance(result, SuccessorCreationResult)
        return result

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        self.reconciliation_calls.append((request.operation_id, request.delivery_epoch))
        result = self._committed.get(request.operation_id)
        if request.operation_id in self._unknown_reconciliation:
            result = None
            disposition = "unknown"
        else:
            disposition = "committed" if result is not None else "not_found"
        return EffectReconciliationResult(
            domain=request.domain,
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=request.delivery_epoch,
            disposition=disposition,
            result=result,
        )


class DeterministicRuntime:
    """Return predeclared runtime results and reconcile committed operations."""

    def __init__(
        self,
        *,
        responses: Mapping[
            str,
            RuntimeStageResult | Callable[[RuntimeStageRequest], RuntimeStageResult],
        ],
        before_call: Callable[[], None] | None = None,
        lose_reply_once: set[str] | frozenset[str] = frozenset(),
        unknown_reconciliation: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self._responses = dict(responses)
        self._before_call = before_call
        self._lose_reply_once = set(lose_reply_once)
        self._unknown_reconciliation = set(unknown_reconciliation)
        self._committed: dict[str, RuntimeStageResult] = {}
        self.calls: list[tuple[str, int]] = []
        self.reconciliation_calls: list[tuple[str, int]] = []

    def execute_stage(self, request: RuntimeStageRequest) -> RuntimeStageResult:
        if self._before_call is not None:
            self._before_call()
        self.calls.append((request.operation_id, request.delivery_epoch))
        configured = self._responses[request.operation_id]
        result = configured(request) if callable(configured) else configured
        result = replace(result, delivery_epoch=request.delivery_epoch)
        request.validate_result(result)
        self._committed[request.operation_id] = result
        if request.operation_id in self._lose_reply_once:
            self._lose_reply_once.remove(request.operation_id)
            raise EffectOutcomeUnknown(request.operation_id)
        return result

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        self.reconciliation_calls.append((request.operation_id, request.delivery_epoch))
        result = self._committed.get(request.operation_id)
        if request.operation_id in self._unknown_reconciliation:
            result = None
            disposition = "unknown"
        else:
            disposition = "committed" if result is not None else "not_found"
        return EffectReconciliationResult(
            domain=request.domain,
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=request.delivery_epoch,
            disposition=disposition,
            result=result,
        )
