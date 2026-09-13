"""Claim-fenced orchestration for durable artifact materialization actions."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, BinaryIO

from .materializer import (
    FilesystemMaterializer,
    IntegrityError,
    MaterializationConflict,
    MaterializationRequest,
    MaterializerError,
)

if TYPE_CHECKING:
    from ..control.store import ControlStore


class ArtifactMaterializationService:
    """Run the pure materializer outside the Control transaction and commit it."""

    def __init__(
        self,
        store: ControlStore,
        materializer: FilesystemMaterializer,
    ) -> None:
        self._store = store
        self._materializer = materializer

    def materialize_action(
        self,
        *,
        action_id: str,
        content: bytes | bytearray | memoryview | BinaryIO | None,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, object]:
        existing = self._store.get_artifact_materialization(action_id)
        if existing["state"] == "completed":
            return self._store.get_artifact_version(
                str(existing["artifact_version_id"])
            )
        claim = self._store.claim_artifact_materialization(
            action_id=action_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            actor_id="artifact-materializer",
            idempotency_key=self._key(
                "claim",
                action_id,
                worker_id,
                str(existing["claim_epoch"]),
            ),
        ).value
        version = self._store.get_artifact_version(
            str(claim["artifact_version_id"])
        )
        request = MaterializationRequest.from_dict(
            {
                "schema_version": 1,
                "operation_id": claim["operation_id"],
                "root_id": claim["root_id"],
                "relative_path": claim["relative_path"],
                "sha256": claim["sha256"],
                "byte_length": claim["byte_length"],
                "media_type": claim["media_type"],
                "parents": version["parents"],
            }
        )
        try:
            result = self._materializer.materialize(request, content)
        except MaterializerError as exc:
            self._store.fail_artifact_materialization(
                action_id=action_id,
                worker_id=worker_id,
                claim_epoch=int(claim["claim_epoch"]),
                failure_category=self._failure_category(exc),
                actor_id="artifact-materializer",
                idempotency_key=self._key(
                    "fail", action_id, worker_id, str(claim["claim_epoch"])
                ),
            )
            raise
        completed = self._store.complete_artifact_materialization(
            action_id=action_id,
            worker_id=worker_id,
            claim_epoch=int(claim["claim_epoch"]),
            materialized_result=result.to_dict(),
            actor_id="artifact-materializer",
            idempotency_key=self._key(
                "complete", action_id, worker_id, str(claim["claim_epoch"])
            ),
        )
        return completed.value

    @staticmethod
    def _failure_category(exc: MaterializerError) -> str:
        if isinstance(exc, IntegrityError):
            return "integrity_error"
        if isinstance(exc, MaterializationConflict):
            return "materialization_conflict"
        return "materializer_error"

    @staticmethod
    def _key(*parts: str) -> str:
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
