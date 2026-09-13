"""Spec 2 Task 6 — papers.repo_urls additive column.

The repo_urls column (JSON array of resolved repo URLs) is added by the SAME
applied-on-open papers-additive-ALTER path that carries published_at/arxiv_id/
source: db.apply_schema (fresh-DB CREATE TABLE) + ensure_radar_schema (the
guarded ALTER for a legacy DB that predates the column). index_papers.index_paper
runs exactly this connect()+apply_schema()+ensure_radar_schema() sequence on every
write, so the column is live wherever a paper is indexed.

Tests (real db.connect against a temp CORTEX_RESEARCH_DB, CORTEX_SKIP_EMBED=1):
 - a FRESH DB has the column;
 - a LEGACY DB (papers WITHOUT the column) gains it on the real open path;
 - running the migration twice is idempotent (no error);
 - set_repo_urls / get_repo_urls round-trip a JSON list.
"""
from __future__ import annotations

import sqlite3

import pytest

from cortex_research.db import apply_schema, connect
from cortex_research.radar_schema import (
    ensure_radar_schema,
    get_repo_urls,
    set_repo_urls,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")


def _papers_cols(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}


def test_fresh_db_has_repo_urls_column(tmp_path):
    """A freshly-created DB (CREATE TABLE path) has repo_urls."""
    db = tmp_path / "research.db"
    conn = connect(db)
    try:
        apply_schema(conn)
        ensure_radar_schema(conn)
        assert "repo_urls" in _papers_cols(conn)
    finally:
        conn.close()


def test_legacy_db_gains_column_on_open(tmp_path):
    """A legacy papers table created WITHOUT repo_urls gains it on the real
    connect()+apply_schema()+ensure_radar_schema() open path."""
    db = tmp_path / "research.db"
    # Build a legacy papers table by hand (no repo_urls column), simulating a DB
    # that predates this migration. Use the same minimal columns ensure_radar_schema
    # guards on (it backfills source/arxiv_id/published_at/repo_urls lazily).
    legacy = sqlite3.connect(str(db))
    try:
        legacy.execute(
            "CREATE TABLE papers ("
            " paper_dir TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " date TEXT, keywords TEXT, summary TEXT, projects TEXT,"
            " indexed_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?,?,?)",
            ("legacy-paper", "Old", "2026-05-01"),
        )
        legacy.commit()
    finally:
        legacy.close()

    conn = connect(db)
    try:
        assert "repo_urls" not in _papers_cols(conn), "precondition: legacy DB lacks it"
        apply_schema(conn)         # CREATE TABLE IF NOT EXISTS is a no-op here
        ensure_radar_schema(conn)  # the additive ALTER fires
        assert "repo_urls" in _papers_cols(conn)
        # legacy row survives the migration
        row = conn.execute(
            "SELECT repo_urls FROM papers WHERE paper_dir='legacy-paper'"
        ).fetchone()
        assert row["repo_urls"] is None  # backfilled to NULL, not dropped
    finally:
        conn.close()


def test_migration_idempotent(tmp_path):
    """Running the migration repeatedly never raises (column-exists guard)."""
    db = tmp_path / "research.db"
    conn = connect(db)
    try:
        apply_schema(conn)
        ensure_radar_schema(conn)
        ensure_radar_schema(conn)
        ensure_radar_schema(conn)  # 3x — must not error
        assert "repo_urls" in _papers_cols(conn)
    finally:
        conn.close()


def test_set_get_repo_urls_round_trips_json_list(tmp_path):
    """set_repo_urls writes a JSON array; get_repo_urls reads it back as a list."""
    db = tmp_path / "research.db"
    conn = connect(db)
    try:
        apply_schema(conn)
        ensure_radar_schema(conn)
        conn.execute(
            "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?,?,?)",
            ("p1", "T", "2026-06-03"),
        )
        conn.commit()

        urls = ["https://github.com/foo/bar", "https://github.com/other/repo"]
        set_repo_urls(conn, "p1", urls)
        assert get_repo_urls(conn, "p1") == urls

        # raw column holds JSON text
        raw = conn.execute(
            "SELECT repo_urls FROM papers WHERE paper_dir='p1'"
        ).fetchone()[0]
        assert raw.startswith("[") and "github.com/foo/bar" in raw

        # a mined [] (absence) must NOT erase previously stored urls — absence
        # never destroys knowledge (Helios false-negative lesson).
        assert set_repo_urls(conn, "p1", []) == "skipped_empty"
        assert get_repo_urls(conn, "p1") == urls
        # an explicit human clear IS allowed
        assert set_repo_urls(conn, "p1", [], source="human") == "written"
        assert get_repo_urls(conn, "p1") == []
    finally:
        conn.close()


def test_get_repo_urls_missing_or_null_returns_empty(tmp_path):
    """A row with no repo_urls (NULL) or an absent paper_dir reads as []."""
    db = tmp_path / "research.db"
    conn = connect(db)
    try:
        apply_schema(conn)
        ensure_radar_schema(conn)
        conn.execute(
            "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?,?,?)",
            ("p2", "T", "2026-06-03"),
        )
        conn.commit()
        assert get_repo_urls(conn, "p2") == []      # NULL column
        assert get_repo_urls(conn, "absent") == []  # no such row
    finally:
        conn.close()


# --- provenance (repo_urls_source) -------------------------------------------

def _mk_db(tmp_path, name="prov.db"):
    db = tmp_path / name
    conn = connect(db)
    apply_schema(conn)
    ensure_radar_schema(conn)
    conn.execute(
        "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?,?,?)",
        ("p1", "T", "2026-06-09"),
    )
    conn.commit()
    return conn


def test_fresh_and_legacy_db_gain_repo_urls_source(tmp_path):
    """Both schema paths carry the provenance column."""
    conn = _mk_db(tmp_path)
    try:
        assert "repo_urls_source" in _papers_cols(conn)
    finally:
        conn.close()
    # legacy: papers WITHOUT either column gains both on the open path
    db = tmp_path / "legacy2.db"
    legacy = sqlite3.connect(str(db))
    try:
        legacy.execute(
            "CREATE TABLE papers ("
            " paper_dir TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " date TEXT, keywords TEXT, summary TEXT, projects TEXT,"
            " indexed_at TEXT NOT NULL)"
        )
        legacy.commit()
    finally:
        legacy.close()
    conn = connect(db)
    try:
        apply_schema(conn)
        ensure_radar_schema(conn)
        assert {"repo_urls", "repo_urls_source"} <= _papers_cols(conn)
    finally:
        conn.close()


def test_mined_never_overwrites_human_or_agent_pin(tmp_path):
    """The correction-persistence invariant: an automatic re-mine cannot clobber
    a human/agent pin (the Helios recurrence killer)."""
    from cortex_research.radar_schema import get_repo_pin

    conn = _mk_db(tmp_path)
    try:
        assert set_repo_urls(conn, "p1", ["https://github.com/a/b"],
                             source="human") == "written"
        # mined (non-empty AND empty) both refused
        assert set_repo_urls(conn, "p1", ["https://github.com/x/y"]) == "skipped_pin"
        assert set_repo_urls(conn, "p1", []) == "skipped_pin"
        # agent refused over human
        assert set_repo_urls(conn, "p1", ["https://github.com/x/y"],
                             source="agent") == "skipped_pin"
        # the pin survives, with provenance
        assert get_repo_pin(conn, "p1") == {
            "urls": ["https://github.com/a/b"], "source": "human"}
        # human re-write wins (rank-equal rewrites allowed)
        assert set_repo_urls(conn, "p1", ["https://github.com/a/b2"],
                             source="human") == "written"
        assert get_repo_pin(conn, "p1")["urls"] == ["https://github.com/a/b2"]
    finally:
        conn.close()


def test_agent_pin_beats_mined_loses_to_human(tmp_path):
    from cortex_research.radar_schema import get_repo_pin

    conn = _mk_db(tmp_path)
    try:
        assert set_repo_urls(conn, "p1", ["https://github.com/m/m"]) == "written"
        assert get_repo_pin(conn, "p1")["source"] == "mined"
        assert set_repo_urls(conn, "p1", ["https://github.com/a/a"],
                             source="agent") == "written"   # agent over mined
        assert set_repo_urls(conn, "p1", ["https://github.com/m/m2"]) == "skipped_pin"
        assert set_repo_urls(conn, "p1", ["https://github.com/h/h"],
                             source="human") == "written"   # human over agent
        assert get_repo_pin(conn, "p1") == {
            "urls": ["https://github.com/h/h"], "source": "human"}
    finally:
        conn.close()


def test_set_repo_urls_no_row_is_loud(tmp_path):
    """UPDATE matching 0 rows must be reported, never silently swallowed."""
    conn = _mk_db(tmp_path)
    try:
        assert set_repo_urls(conn, "absent-paper", ["https://github.com/a/b"],
                             source="human") == "no_row"
    finally:
        conn.close()


def test_legacy_null_source_reads_as_mined_and_is_overwritable(tmp_path):
    """A pre-provenance row (repo_urls set, source NULL) ranks as mined: mined may
    rewrite it, and get_repo_pin reports source='mined'."""
    from cortex_research.radar_schema import get_repo_pin

    conn = _mk_db(tmp_path)
    try:
        # simulate a legacy write (bypasses the new writer)
        conn.execute("UPDATE papers SET repo_urls=? WHERE paper_dir='p1'",
                     ('["https://github.com/old/legacy"]',))
        conn.commit()
        pin = get_repo_pin(conn, "p1")
        assert pin == {"urls": ["https://github.com/old/legacy"], "source": "mined"}
        assert set_repo_urls(conn, "p1", ["https://github.com/new/mined"]) == "written"
        assert get_repo_pin(conn, "p1")["urls"] == ["https://github.com/new/mined"]
    finally:
        conn.close()
