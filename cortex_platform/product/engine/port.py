"""D2: the concrete `ResearchEnginePort` over the shipped engine.

`workflows/ports.py:39` has declared this boundary since P2 with exactly one
implementation, `DeterministicResearchEngine`, whose whole job is to return
predeclared answers in tests. This is the first one that runs the engine.

Scope is the non-agentic subset program-plan amendment A3 authorises -- ingest
now, radar and audit named and not implemented -- because the agent-SDK legs
handed `dict(os.environ)` to an unconfigured session, which made the agentic
steps a boundary rather than a backlog item. Those legs are no longer shipped:
the only model call the supported engine makes is the embedding request in
`cortex_research.embed`, whose credential is bound from `secret_refs` like every
other engine input.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from cortex_platform.product.control.errors import InvalidTransition
from cortex_platform.product.sources.adoption import (
    CorpusReadError,
    encode_engine_ref,
    read_corpus_subset,
)
from cortex_platform.product.workflows.coordinator import (
    EffectOutcomeUnknown,
    EffectPermanentlyRejected,
)
from cortex_platform.product.workflows.models import (
    EffectReconciliationRequest,
    EffectReconciliationResult,
    EngineReference,
    LineageQueryRequest,
    SourceBindingRequest,
    SourceImportRequest,
    SourceImportResult,
    SuccessorCreationRequest,
)

from .bindings import EngineRoots
from .supervisor import EffectExecution, ResearchEffectSupervisor

# The capture placeholder identity AMD-2 fixed: `capture-<id>` is never checked
# against `sources`, and the real identity appears only when the adoption
# manifest commits.
CAPTURE_SOURCE_PREFIX = "capture-"
# ⟦AMD-3⟧ fixes the adoption idempotency key to `capture:<id>`. The store
# refuses it verbatim: `_KEY_RE` (`control/store.py:118`) admits only 16 to 128
# URL-safe characters, so a colon is illegal and a short id is too. The prefix
# below is the same key in the shape the store accepts -- one commit per
# capture, derived from nothing but the capture id.
ADOPTION_KEY_PREFIX = "adoption-capture-"


@dataclass(frozen=True)
class EnginePayload:
    """What the operator actually captured, resolved for one import."""

    kind: str
    identifier: str


@dataclass(frozen=True)
class ImportOutcome:
    """Everything the consumer needs after one import, beyond the typed result."""

    manifest_id: str
    source_ids: tuple[str, ...]
    paper_dirs: tuple[str, ...]
    execution: EffectExecution


def capture_id_from_source(source_id: str) -> str | None:
    if not source_id.startswith(CAPTURE_SOURCE_PREFIX):
        return None
    return source_id[len(CAPTURE_SOURCE_PREFIX) :]


class ProductResearchEngine:
    """Run engine effects through the supervisor and commit what they produced."""

    def __init__(
        self,
        *,
        store: Any,
        supervisor: ResearchEffectSupervisor,
        roots: EngineRoots,
        corpus_root_id: str,
        actor_id: str,
        payload_resolver: Callable[[str], EnginePayload],
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._roots = roots
        self._corpus_root_id = corpus_root_id
        self._actor_id = actor_id
        self._payload_resolver = payload_resolver
        self.outcomes: dict[str, ImportOutcome] = {}

    # -- the subset A3 authorises ------------------------------------------

    def import_source(self, request: SourceImportRequest) -> SourceImportResult:
        payload = self._payload(request)
        if payload.kind != "arxiv":
            # V3 fixed the reachable path to arXiv. The non-arXiv entry points
            # this refusal used to fence off (`ingest_html_url`,
            # `ingest_pdf_url`) spawned detached grandchildren unconditionally,
            # so they could not satisfy the no-descendant assertion; they are no
            # longer part of the supported research surface at all, and this
            # refusal is what keeps the engine's four operations closed.
            raise EffectPermanentlyRejected("invalid_source")
        execution = self._supervisor.run(
            "ingest_arxiv", {"identifier": payload.identifier}
        )
        self._raise_for(execution)
        manifest_id, source_ids, entries, chunk_count = self._adopt(
            request, execution
        )
        self.outcomes[request.operation_id] = ImportOutcome(
            manifest_id=manifest_id,
            source_ids=source_ids,
            paper_dirs=execution.paper_dirs,
            execution=execution,
        )
        return SourceImportResult(
            operation_id=request.operation_id,
            delivery_epoch=request.delivery_epoch,
            request_hash=request.request_hash,
            source_id=request.source_id,
            engine_reference=EngineReference(
                kind="source", value=encode_engine_ref(entries[0].paper_dir)
            ),
            manifest={
                "source_rows": len(entries),
                "chunks": chunk_count,
                "directories": len(execution.paper_dirs),
            },
        )

    def reconcile_effect(
        self, request: EffectReconciliationRequest
    ) -> EffectReconciliationResult:
        """Ask the copy what happened, without opening it inside cortexd."""

        effect_request = request.effect_request
        if not isinstance(effect_request, SourceImportRequest):
            return EffectReconciliationResult(
                domain=request.domain,
                operation_id=request.operation_id,
                request_hash=request.request_hash,
                delivery_epoch=request.delivery_epoch,
                disposition="unknown",
            )
        payload = self._payload(effect_request)
        execution = self._supervisor.run(
            "reconcile_arxiv", {"identifier": payload.identifier}
        )
        if not execution.ok or not execution.paper_dirs:
            disposition = "not_found" if execution.ok else "unknown"
            return EffectReconciliationResult(
                domain=request.domain,
                operation_id=request.operation_id,
                request_hash=request.request_hash,
                delivery_epoch=request.delivery_epoch,
                disposition=disposition,
            )
        replayed = replace(effect_request, delivery_epoch=request.delivery_epoch)
        manifest_id, source_ids, entries, chunk_count = self._adopt(
            replayed, execution
        )
        self.outcomes[request.operation_id] = ImportOutcome(
            manifest_id=manifest_id,
            source_ids=source_ids,
            paper_dirs=execution.paper_dirs,
            execution=execution,
        )
        result = SourceImportResult(
            operation_id=effect_request.operation_id,
            delivery_epoch=request.delivery_epoch,
            request_hash=effect_request.request_hash,
            source_id=effect_request.source_id,
            engine_reference=EngineReference(
                kind="source", value=encode_engine_ref(entries[0].paper_dir)
            ),
            manifest={
                "source_rows": len(entries),
                "chunks": chunk_count,
                "directories": len(execution.paper_dirs),
            },
        )
        return EffectReconciliationResult(
            domain=request.domain,
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=request.delivery_epoch,
            disposition="committed",
            result=result,
        )

    def checkpoint(self) -> EffectExecution:
        """The first step after an operator reopens an uncertain capture.

        A copy stranded by a SIGKILL or by a surviving grandchild has a
        non-empty write-ahead log, and `read_corpus` is right to refuse it. It
        heals in a child, because cortexd never opens `research.db` for writing.
        """

        return self._supervisor.run("checkpoint")

    # -- the rest of the port, refused rather than faked --------------------

    def bind_source(self, request: SourceBindingRequest) -> Any:
        raise EffectPermanentlyRejected("adapter_unavailable")

    def query_lineage(self, request: LineageQueryRequest) -> Any:
        raise EffectPermanentlyRejected("adapter_unavailable")

    def create_successor(self, request: SuccessorCreationRequest) -> Any:
        raise EffectPermanentlyRejected("adapter_unavailable")

    # -- internals ----------------------------------------------------------

    def _payload(self, request: SourceImportRequest) -> EnginePayload:
        try:
            return self._payload_resolver(request.source_id)
        except LookupError as error:
            raise EffectPermanentlyRejected("invalid_source") from error

    @staticmethod
    def _raise_for(execution: EffectExecution) -> None:
        if execution.ok:
            return
        category = execution.failure_category or "outcome_unknown"
        if category == "outcome_unknown":
            # Never a failure: the child may have written before it died, and a
            # duplicate ingest mints a duplicate paper_dir.
            raise EffectOutcomeUnknown(execution.failure_message or category)
        raise EffectPermanentlyRejected(category)

    def _adopt(
        self, request: SourceImportRequest, execution: EffectExecution
    ) -> tuple[str, tuple[str, ...], tuple, int]:
        """Build the manifest over exactly what was ingested, and commit it."""

        paper_dirs = execution.paper_dirs
        if not paper_dirs:
            raise EffectPermanentlyRejected("materialization_failed")
        try:
            read = read_corpus_subset(
                database=self._roots.research_db,
                corpus_root=self._roots.corpus_root,
                paper_dirs=paper_dirs,
            )
        except CorpusReadError as error:
            # ⟦AMD-5⟧ the copy is in an unknown state; the payload is not the
            # problem, so this is never `invalid_source`.
            raise EffectOutcomeUnknown(str(error)) from error
        entries = read.manifest.entries
        if not entries:
            raise EffectPermanentlyRejected("materialization_failed")
        capture_id = capture_id_from_source(request.source_id)
        idempotency_key = (
            f"{ADOPTION_KEY_PREFIX}{capture_id}"
            if capture_id
            else f"adoption-import-{request.operation_id}"
        )
        try:
            record = self._store.commit_adoption_manifest(
                manifest=read.manifest,
                corpus_root_id=self._corpus_root_id,
                actor_id=self._actor_id,
                idempotency_key=idempotency_key,
            )
        except InvalidTransition as error:
            # S1 A1-1: a duplicate canonical id or a paper_dir already bound to
            # another source is the source being wrong, not the engine failing.
            raise EffectPermanentlyRejected("invalid_source") from error
        except ValueError as error:
            raise EffectPermanentlyRejected("invalid_source") from error
        canonical = {entry.canonical_id for entry in entries}
        source_ids = tuple(
            str(source["id"])
            for source in self._store.list_sources()
            if source["canonical_id"] in canonical
        )
        chunk_count = int((execution.engine or {}).get("chunk_count") or 0)
        return record.manifest_id, source_ids, entries, chunk_count
