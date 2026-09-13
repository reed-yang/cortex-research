"""papers.source_url column: the dedup key for a NON-arxiv PDF ingest
(ingest_pdf_url). Nullable, present on a fresh schema, lazily migrated on an
older DB, and persisted by index_paper. Mirrors test_full_text_source_column.
"""
import sqlite3
from pathlib import Path


def _mk_paper(dirpath: Path, title: str):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "notes.md").write_text(
        f"# Notes: {title}\n\n### 2026-06-03\n\n## 详细摘要\n\nabstract body\n",
        encoding="utf-8",
    )
    (dirpath / "full_text.md").write_text(
        f"# {title}\n\n## Method\n\nbody text " * 50, encoding="utf-8"
    )


def test_fresh_schema_has_source_url_column(research_db):
    conn = sqlite3.connect(research_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    conn.close()
    assert "source_url" in cols


def test_index_paper_persists_source_url(research_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    from cortex_research.index_papers import index_paper
    url = "https://raw.githubusercontent.com/jd-opensource/JoyAI-Echo/main/paper.pdf"
    pdir = tmp_path / "papers" / "20260603-Joy_Paper"
    _mk_paper(pdir, "Joy Paper")
    assert index_paper(pdir / "notes.md", source="agent",
                       full_text_source="ocr", source_url=url)
    conn = sqlite3.connect(research_db)
    row = conn.execute(
        "SELECT source_url, arxiv_id FROM papers WHERE paper_dir='20260603-Joy_Paper'"
    ).fetchone()
    conn.close()
    assert row[0] == url
    assert row[1] is None  # non-arxiv: arxiv_id stays NULL


def test_index_paper_source_url_defaults_null(research_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    from cortex_research.index_papers import index_paper
    pdir = tmp_path / "papers" / "20260203-Reader_Paper"
    _mk_paper(pdir, "Reader Paper")
    assert index_paper(pdir / "notes.md", source="reader")
    conn = sqlite3.connect(research_db)
    row = conn.execute(
        "SELECT source_url FROM papers WHERE paper_dir='20260203-Reader_Paper'"
    ).fetchone()
    conn.close()
    assert row[0] is None


def test_lazy_alter_adds_source_url_on_old_db(tmp_path, monkeypatch):
    """An older papers table without source_url gets the column via the
    PRAGMA-guarded ALTER (mirrors full_text_source/arxiv_id)."""
    db = tmp_path / "old.db"
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE papers (paper_dir TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "indexed_at TEXT NOT NULL)")
    conn.commit()
    from cortex_research.radar_schema import ensure_radar_schema
    ensure_radar_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    conn.close()
    assert "source_url" in cols
