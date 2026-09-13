"""M1f schema tests — radar_signals, radar_runs tables + papers.source migration."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_research.db import apply_schema, connect
from cortex_research.radar_schema import ensure_radar_schema


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = connect(db_path)  # loads sqlite_vec so vec0 virtual table works
    apply_schema(c)  # M1a/M1b/M1c base schema (creates papers table)
    yield c
    c.close()


def test_ensure_creates_radar_tables(conn):
    ensure_radar_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "radar_signals" in tables
    assert "radar_runs" in tables


def test_ensure_adds_papers_source_column(conn):
    ensure_radar_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    assert "source" in cols


def test_papers_source_default_reader(conn):
    ensure_radar_schema(conn)
    conn.execute(
        "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?, ?, ?)",
        ("test", "Title", "2026-05-27"),
    )
    row = conn.execute("SELECT source FROM papers WHERE paper_dir='test'").fetchone()
    assert row["source"] == "reader"


def test_ensure_idempotent(conn):
    ensure_radar_schema(conn)
    ensure_radar_schema(conn)
    ensure_radar_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    assert "source" in cols
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "radar_signals" in tables


def test_radar_signals_check_constraints(conn):
    ensure_radar_schema(conn)
    conn.execute(
        """INSERT INTO radar_signals
           (signal_id, source, source_ref, title, importance,
            importance_breakdown, scoring_method)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("sig1", "arxiv", "2403.12345", "Title", 3, "{}", "rule"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO radar_signals
               (signal_id, source, source_ref, title, importance,
                importance_breakdown, scoring_method)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("sig2", "invalid_source", "x", "T", 3, "{}", "rule"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO radar_signals
               (signal_id, source, source_ref, title, importance,
                importance_breakdown, scoring_method)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("sig3", "arxiv", "x", "T", 6, "{}", "rule"),
        )


def test_radar_signals_unique_source_ref(conn):
    ensure_radar_schema(conn)
    conn.execute(
        """INSERT INTO radar_signals
           (signal_id, source, source_ref, title, importance,
            importance_breakdown, scoring_method)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("sig1", "arxiv", "2403.12345", "T", 3, "{}", "rule"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO radar_signals
               (signal_id, source, source_ref, title, importance,
                importance_breakdown, scoring_method)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("sig2", "arxiv", "2403.12345", "T2", 3, "{}", "rule"),
        )


def test_radar_runs_check_constraints(conn):
    ensure_radar_schema(conn)
    conn.execute(
        "INSERT INTO radar_runs (run_id, source) VALUES (?, ?)",
        ("run1", "cron"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO radar_runs (run_id, source, status) VALUES (?, ?, ?)",
            ("run2", "cron", "bogus"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO radar_runs (run_id, source) VALUES (?, ?)",
            ("run3", "invalid_source"),
        )


def test_migration_existing_papers_get_reader_default(conn):
    """Existing papers from M1a/M1b should backfill to 'reader'."""
    conn.execute(
        "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?, ?, ?)",
        ("preexisting", "Old paper", "2026-05-01"),
    )
    ensure_radar_schema(conn)  # column add after data exists
    row = conn.execute("SELECT source FROM papers WHERE paper_dir='preexisting'").fetchone()
    assert row["source"] == "reader"


def test_ensure_on_papers_less_db_does_not_crash():
    """Guard: ensure_radar_schema must skip the papers.source ALTER (not crash
    with 'no such table: papers') when the M1a papers table is absent — e.g.
    a bare connection in tests or a fresh DB. Radar tables still get created."""
    bare = sqlite3.connect(":memory:")
    try:
        ensure_radar_schema(bare)  # must not raise
        tables = {r[0] for r in bare.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "radar_signals" in tables
        assert "radar_runs" in tables
        assert "papers" not in tables  # ALTER skipped gracefully
    finally:
        bare.close()


def test_xhs_pull_source_allowed_and_blogger_state_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research.db import connect, apply_schema
    from cortex_research.radar_schema import ensure_radar_schema
    c = connect(tmp_path / "r.db"); apply_schema(c); ensure_radar_schema(c)
    c.execute("""INSERT INTO radar_signals (signal_id, source, source_ref, title, importance,
                 importance_breakdown, scoring_method, processed_status, kind, source_url)
                 VALUES ('s1','xhs_pull','https://b.com/p','t',3,'{}','rule','pending','blog','https://b.com/p')""")
    c.execute("INSERT INTO radar_runs (run_id, source, status) VALUES ('r1','xhs_pull','running')")
    c.execute("INSERT INTO xhs_blogger_state (profile_id, nickname) VALUES ('abc','tabris')")
    c.commit()
    assert c.execute("SELECT kind FROM radar_signals WHERE signal_id='s1'").fetchone()[0] == "blog"
    # existing values still allowed
    c.execute("""INSERT INTO radar_signals (signal_id, source, source_ref, title, importance,
                 importance_breakdown, scoring_method, processed_status)
                 VALUES ('s2','arxiv','2605.1','t',1,'{}','rule','pending')""")
    c.commit()


def test_migration_idempotent_and_preserves_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research.db import connect, apply_schema
    from cortex_research.radar_schema import ensure_radar_schema
    c = connect(tmp_path / "r.db"); apply_schema(c); ensure_radar_schema(c)
    # seed a legacy-style arxiv row, then run ensure_radar_schema again (idempotent)
    c.execute("""INSERT INTO radar_signals (signal_id, source, source_ref, title, importance,
                 importance_breakdown, scoring_method, processed_status)
                 VALUES ('keep','arxiv','2605.2','t',2,'{}','rule','indexed')""")
    c.commit()
    ensure_radar_schema(c); ensure_radar_schema(c)  # 2x more — must not error or lose rows
    assert c.execute("SELECT COUNT(*) FROM radar_signals WHERE signal_id='keep'").fetchone()[0] == 1
    assert c.execute("SELECT importance FROM radar_signals WHERE signal_id='keep'").fetchone()[0] == 2
