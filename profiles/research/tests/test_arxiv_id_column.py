"""papers.arxiv_id column: stored on index, defaults NULL, backfillable."""
import sqlite3
from pathlib import Path


def _mk_paper(dirpath: Path, title: str):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "notes.md").write_text(
        f"# Notes: {title}\n\n### 2026-06-01\n\n## 详细摘要\n\nabstract body\n",
        encoding="utf-8",
    )
    (dirpath / "full_text.md").write_text(
        f"# {title}\n\n## Method\n\nbody text " * 50, encoding="utf-8"
    )


def test_index_paper_stores_arxiv_id(research_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    from cortex_research.index_papers import index_paper
    pdir = tmp_path / "papers" / "20260601-Some_Title"
    _mk_paper(pdir, "Some Title")
    assert index_paper(pdir / "notes.md", source="agent", arxiv_id="2603.04379")
    conn = sqlite3.connect(research_db)
    row = conn.execute(
        "SELECT arxiv_id FROM papers WHERE paper_dir='20260601-Some_Title'"
    ).fetchone()
    conn.close()
    assert row[0] == "2603.04379"


def test_index_paper_arxiv_id_defaults_null(research_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    from cortex_research.index_papers import index_paper
    pdir = tmp_path / "papers" / "20260203-Reader_Paper"
    _mk_paper(pdir, "Reader Paper")
    assert index_paper(pdir / "notes.md", source="reader")
    conn = sqlite3.connect(research_db)
    row = conn.execute(
        "SELECT arxiv_id FROM papers WHERE paper_dir='20260203-Reader_Paper'"
    ).fetchone()
    conn.close()
    assert row[0] is None


def test_backfill_arxiv_id_from_paper_dir(research_db):
    from cortex_research.index_papers import backfill_arxiv_id
    conn = sqlite3.connect(research_db)
    for pd in ("arxiv-2603.04379", "arxiv:2605.10759",
               "20260301-2512.04332-Some_Idea_Paper", "20260203-Reader_No_Id"):
        conn.execute(
            "INSERT INTO papers (paper_dir,title,indexed_at) "
            "VALUES (?,?,datetime('now'))",
            (pd, pd),
        )
    conn.commit()
    n = backfill_arxiv_id(conn)
    got = dict(conn.execute("SELECT paper_dir, arxiv_id FROM papers").fetchall())
    conn.close()
    assert got["arxiv-2603.04379"] == "2603.04379"
    # radar COLON stub stays NULL — it is abstract-only, NOT a full-text key
    assert got["arxiv:2605.10759"] is None
    assert got["20260301-2512.04332-Some_Idea_Paper"] == "2512.04332"
    assert got["20260203-Reader_No_Id"] is None  # 8-digit date is not an arxiv id
    assert n == 2  # dash + idea-curate; colon stub + reader excluded
