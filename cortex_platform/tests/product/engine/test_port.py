"""D2: a real engine ingest, offline, through the product boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.engine.bindings import EngineRoots
from cortex_platform.product.engine.port import EnginePayload, ProductResearchEngine
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor
from cortex_platform.product.workflows.coordinator import (
    EffectOutcomeUnknown,
    EffectPermanentlyRejected,
)
from cortex_platform.product.workflows.models import (
    EffectReconciliationRequest,
    SourceImportRequest,
)

from .arxiv_fixture import ArxivFixtureServer
from .conftest import ActivationGate

HTML_PAPER = "2601.00042"
PDF_ONLY_PAPER = "2601.00043"
ACTOR = "machine:p4-capture-consumer"


@pytest.fixture
def arxiv() -> ArxivFixtureServer:
    with ArxivFixtureServer() as server:
        yield server


@pytest.fixture
def corpus_root_id(store: ControlStore, roots: EngineRoots) -> str:
    store.register_asset_root(
        root_id="research-corpus",
        private_path=roots.corpus_root,
        max_bytes=1 << 30,
        enabled=True,
        actor_id="local-operator",
        idempotency_key="engine-corpus-root0001",
    )
    return "research-corpus"


@pytest.fixture
def engine(
    store: ControlStore,
    roots: EngineRoots,
    research_db: Path,
    arxiv: ArxivFixtureServer,
    corpus_root_id: str,
) -> ProductResearchEngine:
    supervisor = ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        skip_embed=True,
        timeout_seconds=180,
        literal_overrides=arxiv.literal_overrides(),
    )
    payloads = {
        "capture-cap1": EnginePayload("arxiv", HTML_PAPER),
        "capture-cap2": EnginePayload("arxiv", PDF_ONLY_PAPER),
        "capture-cap3": EnginePayload("blog", "https://example.com/post"),
    }
    return ProductResearchEngine(
        store=store,
        supervisor=supervisor,
        roots=roots,
        corpus_root_id=corpus_root_id,
        actor_id=ACTOR,
        payload_resolver=lambda source_id: payloads[source_id],
    )


def _request(source_id: str, digest: str = "a" * 64) -> SourceImportRequest:
    return SourceImportRequest(
        operation_id=f"capture.import.{source_id}",
        delivery_epoch=1,
        source_id=source_id,
        canonical_id=f"sha256:{digest}",
    )


def test_a_real_arxiv_ingest_lands_in_the_copied_corpus(
    engine: ProductResearchEngine, roots: EngineRoots, store: ControlStore
) -> None:
    request = _request("capture-cap1")
    result = engine.import_source(request)

    assert result.source_id == "capture-cap1"
    assert result.engine_reference.kind == "source"
    assert result.manifest["directories"] == 1
    assert result.manifest["chunks"] >= 1

    outcome = engine.outcomes[request.operation_id]
    assert len(outcome.source_ids) == 1
    paper_dir = roots.corpus_root / outcome.paper_dirs[0]
    assert (paper_dir / "notes.md").is_file()
    assert (paper_dir / "full_text.md").is_file()
    # It landed under the product's data root, not gdrive.
    assert paper_dir.is_relative_to(roots.corpus_root)

    source = store.get_source(outcome.source_ids[0])
    assert source["canonical_id"] == f"arxiv:{HTML_PAPER}"
    manifest = store.get_adoption_manifest(outcome.manifest_id)
    assert manifest.actor_id == ACTOR
    assert manifest.entry_count == 1


def test_the_copy_is_checkpointed_so_the_reader_can_open_it(
    engine: ProductResearchEngine, roots: EngineRoots
) -> None:
    engine.import_source(_request("capture-cap1"))
    log = roots.research_db.with_name(roots.research_db.name + "-wal")
    assert not log.exists() or log.stat().st_size == 0


def test_a_pdf_only_paper_is_refused_before_anything_is_written(
    engine: ProductResearchEngine, roots: EngineRoots
) -> None:
    """⟦AMD-4⟧: refusing after `_write_paper_dir` would be unrecoverable."""

    before = sorted(path.name for path in roots.corpus_root.iterdir())
    with pytest.raises(EffectPermanentlyRejected) as raised:
        engine.import_source(_request("capture-cap2"))
    assert raised.value.category == "materialization_failed"
    assert sorted(path.name for path in roots.corpus_root.iterdir()) == before


def test_a_pdf_only_paper_leaves_no_papers_row_to_short_circuit(
    engine: ProductResearchEngine, roots: EngineRoots
) -> None:
    import sqlite3

    with pytest.raises(EffectPermanentlyRejected):
        engine.import_source(_request("capture-cap2"))
    connection = sqlite3.connect(str(roots.research_db))
    try:
        rows = connection.execute(
            "SELECT COUNT(*) FROM papers WHERE arxiv_id = ?", (PDF_ONLY_PAPER,)
        ).fetchone()
    finally:
        connection.close()
    assert rows[0] == 0


def test_the_blog_path_is_out_of_scope_not_silently_attempted(
    engine: ProductResearchEngine,
) -> None:
    with pytest.raises(EffectPermanentlyRejected) as raised:
        engine.import_source(_request("capture-cap3"))
    assert raised.value.category == "invalid_source"


def test_an_unknown_payload_is_an_invalid_source(
    engine: ProductResearchEngine,
) -> None:
    with pytest.raises(EffectPermanentlyRejected) as raised:
        engine.import_source(_request("capture-nope"))
    assert raised.value.category == "invalid_source"


def test_the_manifest_commit_is_idempotent_under_capture_id(
    engine: ProductResearchEngine, store: ControlStore
) -> None:
    """⟦AMD-3⟧ `capture:<id>` is what lets a resumed consumer replay safely."""

    request = _request("capture-cap1")
    engine.import_source(request)
    first = engine.outcomes[request.operation_id]

    reconciliation = EffectReconciliationRequest(effect_request=request, delivery_epoch=1)
    result = engine.reconcile_effect(reconciliation)
    assert result.disposition == "committed"
    second = engine.outcomes[reconciliation.operation_id]
    assert second.manifest_id == first.manifest_id
    assert second.source_ids == first.source_ids
    assert len(store.list_sources()) == 1


def test_reconciliation_reports_not_found_when_nothing_was_ingested(
    engine: ProductResearchEngine,
) -> None:
    request = _request("capture-cap1")
    reconciliation = EffectReconciliationRequest(effect_request=request, delivery_epoch=1)
    result = engine.reconcile_effect(reconciliation)
    assert result.disposition == "not_found"
    assert result.result is None


def test_an_uncheckpointed_copy_is_an_unknown_outcome(
    engine: ProductResearchEngine, roots: EngineRoots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦AMD-5⟧ the payload is not the problem; the copy is in an unknown state."""

    from cortex_platform.product.engine import port as port_module

    def refuse(**_: object):
        raise port_module.CorpusReadError("research database is not checkpointed")

    monkeypatch.setattr(port_module, "read_corpus_subset", refuse)
    with pytest.raises(EffectOutcomeUnknown):
        engine.import_source(_request("capture-cap1"))


def test_the_unimplemented_port_methods_refuse_rather_than_pretend(
    engine: ProductResearchEngine,
) -> None:
    for call in (engine.bind_source, engine.query_lineage, engine.create_successor):
        with pytest.raises(EffectPermanentlyRejected) as raised:
            call(None)
        assert raised.value.category == "adapter_unavailable"


def test_the_ingest_reached_only_the_fixture_server(
    engine: ProductResearchEngine, arxiv: ArxivFixtureServer
) -> None:
    engine.import_source(_request("capture-cap1"))
    assert any("/api/query" in path for path in arxiv.requests)
    assert any(f"/html/{HTML_PAPER}" in path for path in arxiv.requests)


def test_a_subset_whose_directory_vanished_is_a_typed_unknown_outcome(
    engine: ProductResearchEngine, roots: EngineRoots
) -> None:
    """The empty subset is the reader's refusal, not a bare `ValueError`.

    Reproduced through real code: `ingest_arxiv` short-circuits on a paper the
    database already names (`paper_ingest.py:1181`) and reports its recorded
    `paper_dir`, so a directory lost between the two runs leaves the subset
    reader with a named row and nothing to digest.
    """

    import shutil

    request = _request("capture-cap1")
    engine.import_source(request)
    paper_dir = engine.outcomes[request.operation_id].paper_dirs[0]
    shutil.rmtree(roots.corpus_root / paper_dir)

    with pytest.raises(EffectOutcomeUnknown):
        engine.import_source(_request("capture-cap1"))


def test_the_pdf_fallback_refuses_typed_when_pymupdf_is_absent() -> None:
    """⟦AMD-14⟧ the optional dependency is deferred, so the refusal is typed.

    `import fitz` sat bare inside `_pdf_to_text`, so on an installation without
    PyMuPDF -- which is every one of them, by decision -- the plain-PDF fallback
    raised `ImportError`. `child.py` maps an unrecognised exception to
    `materialization_failed` with the exception's own text, which is an accident
    reading as a product decision. `IngestError` is the engine's own refusal.
    """

    import builtins

    from cortex_research.paper_ingest import IngestError, _pdf_to_text

    real_import = builtins.__import__

    def without_fitz(name: str, *args: object, **kwargs: object):
        if name == "fitz":
            raise ImportError("No module named 'fitz'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = without_fitz  # type: ignore[assignment]
    try:
        with pytest.raises(IngestError) as raised:
            _pdf_to_text(b"%PDF-1.4\n")
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]
    assert "PyMuPDF" in str(raised.value)


def test_a_real_child_recovers_metadata_429_and_indexes_html(
    engine: ProductResearchEngine, roots: EngineRoots, arxiv: ArxivFixtureServer,
) -> None:
    """Exercise real retries, bound abs egress, parsing and corpus indexing."""
    arxiv.metadata_status = 429
    request = _request("capture-cap1")
    result = engine.import_source(request)
    assert result.manifest["chunks"] >= 1
    assert arxiv.requests.count(f"/api/query?id_list={HTML_PAPER}") == 4
    assert f"/abs/{HTML_PAPER}" in arxiv.requests
    assert f"/html/{HTML_PAPER}" in arxiv.requests
    outcome = engine.outcomes[request.operation_id]
    assert (roots.corpus_root / outcome.paper_dirs[0] / "full_text.md").is_file()


def test_permanent_metadata_failure_is_known_and_writes_nothing(
    engine: ProductResearchEngine, roots: EngineRoots, arxiv: ArxivFixtureServer,
) -> None:
    arxiv.metadata_status = 404
    before = sorted(roots.corpus_root.iterdir())
    with pytest.raises(EffectPermanentlyRejected) as raised:
        engine.import_source(_request("capture-cap1"))
    assert raised.value.category == "materialization_failed"
    assert sorted(roots.corpus_root.iterdir()) == before
    assert arxiv.requests == [f"/api/query?id_list={HTML_PAPER}"]
    receipts = list((roots.state / "effects").glob("*/result.json"))
    assert len(receipts) == 1
    failure = json.loads(receipts[0].read_text())["failure"]
    assert "arxiv metadata" in failure["message"]
    assert "404" in failure["message"]
