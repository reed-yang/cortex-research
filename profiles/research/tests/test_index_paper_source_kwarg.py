"""M1a index_paper backward-compat: source kwarg defaults to 'reader'."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_research.db import apply_schema
from cortex_research.radar_schema import ensure_radar_schema


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(db_path))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research.db import connect
    c = connect()
    c.row_factory = sqlite3.Row
    apply_schema(c)
    ensure_radar_schema(c)
    c.close()
    yield db_path


def _seed_notes(papers_dir: Path, paper_dir: str, content: str) -> Path:
    d = papers_dir / paper_dir
    d.mkdir(parents=True, exist_ok=True)
    notes = d / "notes.md"
    notes.write_text(content, encoding="utf-8")
    (d / "full_text.md").write_text("Full text body.", encoding="utf-8")
    return notes


def test_index_paper_default_source_is_reader(conn, tmp_path, monkeypatch):
    papers_dir = tmp_path / "papers"
    notes = _seed_notes(
        papers_dir, "20260525-MoE",
        "---\ntitle: MoE Survey\ndate: 2026-05-25\nkeywords: [moe]\n---\n# notes\nSummary line.",
    )
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))

    from cortex_research.index_papers import index_paper
    index_paper(notes)  # no source arg -> 'reader'

    c = sqlite3.connect(str(conn))
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT source FROM papers WHERE paper_dir='20260525-MoE'").fetchone()
    c.close()
    assert row["source"] == "reader"


def test_index_paper_source_radar_explicit(conn, tmp_path, monkeypatch):
    papers_dir = tmp_path / "papers"
    _seed_notes(
        papers_dir, "arxiv-2403.12345",
        "---\ntitle: Test\ndate: 2026-05-25\n---\nSummary.",
    )
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))

    from cortex_research.index_papers import index_paper
    index_paper(papers_dir / "arxiv-2403.12345" / "notes.md", source="radar")

    c = sqlite3.connect(str(conn))
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT source FROM papers WHERE paper_dir='arxiv-2403.12345'").fetchone()
    c.close()
    assert row["source"] == "radar"
