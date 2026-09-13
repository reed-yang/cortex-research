import json
from pathlib import Path


def _make_paper(root: Path, name: str, title: str, body: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "notes.md").write_text(
        f"# Notes: {title}\n\n### 2026-01-01\n\n## Paper Summary\n\n{title} summary text here, long enough.\n\n"
        f"### Keywords / 关键术语\n\n`alpha`, `beta`\n", encoding="utf-8")
    (d / "full_text.md").write_text(f"## Method\n\n{body}\n", encoding="utf-8")


def test_index_builds_papers_and_chunks(tmp_path, monkeypatch, research_db):
    papers = tmp_path / "agent-readings" / "papers"
    _make_paper(papers, "20260101-A", "Paper A", "Method A does sequence concatenation.")
    _make_paper(papers, "20260102-B", "Paper B", "Method B extends sequence concatenation.")
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "agent-readings"))

    from cortex_research.index_papers import build_index
    n = build_index(full=True)
    assert n == 2

    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(research_db))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    # each paper has a __catalog__ chunk + >=1 real chunk
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE section='__catalog__'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 4
    conn.close()


def test_reindex_full_no_orphan_embeddings(tmp_path, monkeypatch, research_db):
    papers = tmp_path / "agent-readings" / "papers"
    _make_paper(papers, "20260101-A", "Paper A", "Method A does sequence concatenation.")
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "agent-readings"))
    from cortex_research.index_papers import build_index
    build_index(full=True)
    build_index(full=True)  # reindex same paper -> must not leave orphan embeddings
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(research_db))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_emb = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    assert n_emb == n_chunks, f"orphan embeddings: {n_emb} emb vs {n_chunks} chunks"
    conn.close()
