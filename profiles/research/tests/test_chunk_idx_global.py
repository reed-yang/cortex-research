"""Task 1 (#1): chunk_idx must be a paper-GLOBAL sequential index in document order.

The chunker emits a per-SECTION sub-chunk index (single-sub-chunk sections all get
0), so `ORDER BY chunk_idx` was meaningless across a paper. index_papers now assigns
a global sequential idx at insert time, and `backfill_chunk_idx` rewrites existing
rows per-paper in `id` order (= insertion = document order).
"""
import sqlite3

import sqlite_vec

from cortex_research.index_papers import backfill_chunk_idx


def _seed_chunks(conn, paper_dir, n, chunk_idx_value=0):
    """Insert n chunks for paper_dir, all with the same (broken) chunk_idx."""
    for i in range(n):
        section = "__catalog__" if i == 0 else f"sec{i}"
        conn.execute(
            "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?,?,?,?)",
            (paper_dir, section, chunk_idx_value, f"{paper_dir} chunk {i}"),
        )


def test_backfill_rewrites_chunk_idx_in_id_order(research_db):
    conn = sqlite3.connect(str(research_db))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    # One paper, 4 chunks (catalog + 3 sections), all chunk_idx=0 (the bug).
    _seed_chunks(conn, "paperA", 4, chunk_idx_value=0)
    conn.commit()

    # Pre-fix state: all chunk_idx are 0 -> ORDER BY chunk_idx is meaningless.
    pre = [r["chunk_idx"] for r in conn.execute(
        "SELECT chunk_idx FROM chunks WHERE paper_dir='paperA' ORDER BY id")]
    assert pre == [0, 0, 0, 0]

    n = backfill_chunk_idx(conn)
    conn.commit()
    assert n == 4

    post = [(r["id"], r["chunk_idx"]) for r in conn.execute(
        "SELECT id, chunk_idx FROM chunks WHERE paper_dir='paperA' ORDER BY id")]
    # chunk_idx is now 0,1,2,3 in id (= document) order.
    assert [ci for (_id, ci) in post] == [0, 1, 2, 3]
    conn.close()


def test_backfill_is_per_paper_independent(research_db):
    conn = sqlite3.connect(str(research_db))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    # Two interleaved papers: ids alternate but each paper restarts at 0.
    conn.execute("INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES ('A','s',0,'a0')")
    conn.execute("INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES ('B','s',0,'b0')")
    conn.execute("INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES ('A','s',0,'a1')")
    conn.execute("INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES ('B','s',0,'b1')")
    conn.execute("INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES ('A','s',0,'a2')")
    conn.commit()

    backfill_chunk_idx(conn)
    conn.commit()

    a = [r["chunk_idx"] for r in conn.execute(
        "SELECT chunk_idx FROM chunks WHERE paper_dir='A' ORDER BY id")]
    b = [r["chunk_idx"] for r in conn.execute(
        "SELECT chunk_idx FROM chunks WHERE paper_dir='B' ORDER BY id")]
    assert a == [0, 1, 2]
    assert b == [0, 1]
    conn.close()


def test_backfill_idempotent(research_db):
    conn = sqlite3.connect(str(research_db))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    _seed_chunks(conn, "paperA", 3, chunk_idx_value=0)
    conn.commit()

    backfill_chunk_idx(conn)
    conn.commit()
    first = [r["chunk_idx"] for r in conn.execute(
        "SELECT chunk_idx FROM chunks WHERE paper_dir='paperA' ORDER BY id")]
    # Running again must produce the same result (no drift).
    backfill_chunk_idx(conn)
    conn.commit()
    second = [r["chunk_idx"] for r in conn.execute(
        "SELECT chunk_idx FROM chunks WHERE paper_dir='paperA' ORDER BY id")]
    assert first == second == [0, 1, 2]
    conn.close()


def test_index_loop_assigns_global_sequential_idx(tmp_path, monkeypatch, research_db):
    """A fresh index run assigns a global sequential chunk_idx (not per-section 0s)."""
    papers = tmp_path / "agent-readings" / "papers"
    d = papers / "20260101-A"
    d.mkdir(parents=True)
    (d / "notes.md").write_text(
        "# Notes: Paper A\n\n### 2026-01-01\n\n## Paper Summary\n\n"
        "Paper A summary text here, long enough.\n\n"
        "### Keywords / 关键术语\n\n`alpha`, `beta`\n",
        encoding="utf-8",
    )
    # Multiple sections, each a single sub-chunk -> chunker would give per-section 0s.
    (d / "full_text.md").write_text(
        "## Method\n\nMethod section body.\n\n"
        "## Results\n\nResults section body.\n\n"
        "## Discussion\n\nDiscussion section body.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "agent-readings"))

    from cortex_research.index_papers import build_index
    assert build_index(full=True) == 1

    conn = sqlite3.connect(str(research_db))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    idxs = [r["chunk_idx"] for r in conn.execute(
        "SELECT chunk_idx FROM chunks WHERE paper_dir='20260101-A' ORDER BY id")]
    # catalog + 3 sections = 4 chunks, sequentially 0..3 (NOT all 0).
    assert idxs == list(range(len(idxs)))
    assert idxs == [0, 1, 2, 3]
    conn.close()
