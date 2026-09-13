"""papers.full_text_source column: persisted on index, threaded from
fetch_full_text's `src` ('html'|'ocr'|'pdf'), nullable, lazily migrated on an
older DB missing it. Answers 'why is this paper plain text?' from the DB and
lets a backfill target every source='pdf' body.
"""
import sqlite3
from pathlib import Path

import pytest


def _mk_paper(dirpath: Path, title: str):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "notes.md").write_text(
        f"# Notes: {title}\n\n### 2026-06-01\n\n## 详细摘要\n\nabstract body\n",
        encoding="utf-8",
    )
    (dirpath / "full_text.md").write_text(
        f"# {title}\n\n## Method\n\nbody text " * 50, encoding="utf-8"
    )


def test_fresh_schema_has_full_text_source_column(research_db):
    conn = sqlite3.connect(research_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    conn.close()
    assert "full_text_source" in cols


def test_index_paper_persists_full_text_source(research_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    from cortex_research.index_papers import index_paper
    for src, slug in (("html", "Html_Paper"), ("ocr", "Ocr_Paper"), ("pdf", "Pdf_Paper")):
        pdir = tmp_path / "papers" / f"20260601-{slug}"
        _mk_paper(pdir, slug)
        assert index_paper(pdir / "notes.md", source="agent", arxiv_id="2603.0000",
                           full_text_source=src)
        conn = sqlite3.connect(research_db)
        row = conn.execute(
            "SELECT full_text_source FROM papers WHERE paper_dir=?",
            (f"20260601-{slug}",)).fetchone()
        conn.close()
        assert row[0] == src


def test_index_paper_full_text_source_defaults_null(research_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    from cortex_research.index_papers import index_paper
    pdir = tmp_path / "papers" / "20260203-Reader_Paper"
    _mk_paper(pdir, "Reader Paper")
    assert index_paper(pdir / "notes.md", source="reader")
    conn = sqlite3.connect(research_db)
    row = conn.execute(
        "SELECT full_text_source FROM papers WHERE paper_dir='20260203-Reader_Paper'"
    ).fetchone()
    conn.close()
    assert row[0] is None


def test_lazy_alter_adds_column_on_old_db(tmp_path, monkeypatch):
    """An older papers table created WITHOUT full_text_source gets the column added
    by ensure_radar_schema's PRAGMA-guarded ALTER (mirrors arxiv_id/published_at)."""
    db = tmp_path / "old.db"
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(db))
    conn = sqlite3.connect(db)
    # Minimal legacy papers table — no full_text_source column.
    conn.execute(
        "CREATE TABLE papers (paper_dir TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "indexed_at TEXT NOT NULL)")
    conn.commit()
    from cortex_research.radar_schema import ensure_radar_schema
    ensure_radar_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    conn.close()
    assert "full_text_source" in cols


def test_ingest_arxiv_threads_source_html(research_db, tmp_path, monkeypatch):
    """End-to-end: ingest_arxiv with an HTML fetch persists full_text_source='html'."""
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research import paper_ingest
    monkeypatch.setattr(paper_ingest, "fetch_metadata",
                        lambda aid: {"title": "HTML Paper", "abstract": "abs",
                                     "published_at": "2026-05-01T00:00:00Z"})
    monkeypatch.setattr(paper_ingest, "fetch_full_text",
                        lambda aid, *, ocr_tmp=None, strict=False: ("body " * 500, "html", []))
    res = paper_ingest.ingest_arxiv("2606.03982", source="agent")
    assert res["full_text_source"] == "html"
    conn = sqlite3.connect(research_db)
    row = conn.execute(
        "SELECT full_text_source FROM papers WHERE arxiv_id='2606.03982'").fetchone()
    conn.close()
    assert row[0] == "html"
