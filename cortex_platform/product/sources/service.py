"""Durable source-import delivery over the Cortex control-store outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import ImportDeliveryResult, ImportRequest
from .ports import ResearchImportAdapter

if TYPE_CHECKING:
    from ..control import ControlStore


class SourceImportDispatcher:
    def __init__(
        self, *, store: ControlStore, adapter: ResearchImportAdapter
    ) -> None:
        self.store = store
        self.adapter = adapter

    def deliver(
        self,
        *,
        action_id: str,
        worker_id: str,
        claim_key: str,
        completion_key: str,
        simulate_lost_response: bool = False,
    ) -> ImportDeliveryResult:
        current = self.store.get_source_import_action(action_id)
        if current["state"] == "completed":
            return ImportDeliveryResult(
                status="already_completed",
                source_id=str(current["source_id"]),
                engine_ref=current.get("engine_ref"),
                adapter_replayed=True,
            )
        claim = self.store.claim_source_import(
            action_id=action_id,
            worker_id=worker_id,
            lease_seconds=300,
            actor_id="source-import-dispatcher",
            idempotency_key=claim_key,
        ).value
        result = self.adapter.execute(
            ImportRequest(
                operation_id=str(claim["operation_id"]),
                source_id=str(claim["source_id"]),
                canonical_id=str(claim["canonical_id"]),
                request_hash=str(claim["request_hash"]),
            )
        )
        expected_manifest = {"source_rows", "chunks", "directories"}
        if (
            result.operation_id != claim["operation_id"]
            or result.request_hash != claim["request_hash"]
            or not isinstance(result.manifest, dict)
            or set(result.manifest) != expected_manifest
            or any(
                type(result.manifest[key]) is not int
                or result.manifest[key] < 0
                for key in expected_manifest
            )
        ):
            raise ValueError("adapter import result violates the claimed operation")
        if simulate_lost_response:
            return ImportDeliveryResult(
                status="response_lost",
                source_id=str(claim["source_id"]),
                engine_ref=result.engine_ref,
                adapter_replayed=result.replayed,
            )
        completed = self.store.complete_source_import(
            action_id=action_id,
            claim_owner=worker_id,
            claim_epoch=int(claim["claim_epoch"]),
            request_hash=result.request_hash,
            engine_ref=result.engine_ref,
            result_manifest=result.manifest,
            actor_id="source-import-dispatcher",
            idempotency_key=completion_key,
        ).value
        return ImportDeliveryResult(
            status="completed",
            source_id=str(completed["source_id"]),
            engine_ref=str(completed["engine_ref"]),
            adapter_replayed=result.replayed,
        )
