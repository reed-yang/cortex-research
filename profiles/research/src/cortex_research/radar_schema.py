# profiles/research/src/cortex_research/radar_schema.py
"""M1f schema self-ensure helper. Adds radar_signals + radar_runs + papers.source column."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA_FILE = Path(__file__).parent / "radar_schema.sql"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = {r[1] for r in rows}  # row[1] = name
    return column in cols


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _radar_runs_status_check(conn: sqlite3.Connection) -> str:
    """Return the stored CREATE TABLE SQL for radar_runs (or '')."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='radar_runs'"
    ).fetchone()
    return (row[0] if row else "") or ""


def _radar_signals_create_sql(conn: sqlite3.Connection) -> str:
    """Return the stored CREATE TABLE SQL for radar_signals (or '')."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='radar_signals'"
    ).fetchone()
    return (row[0] if row else "") or ""


def _migrate_radar_runs(conn: sqlite3.Connection) -> None:
    """Idempotently bring an existing radar_runs table up to the current shape.

    Two additive migrations, both guarded so they no-op on an already-current
    schema (and on a fresh DB the CREATE TABLE IF NOT EXISTS already produced
    the new shape, so nothing fires here):

    1. Add the n_failed_queries column (cheap ALTER).
    2. Relax the status CHECK to allow 'partial'. CREATE TABLE IF NOT EXISTS
       cannot alter an existing table's CHECK, so when the old constraint is
       detected we rebuild the table in place (copy → drop → rename) inside a
       transaction. Data is preserved.
    3. Expand the source CHECK to allow 'xhs_pull' (same copy→drop→rename
       pattern, guarded on the stored SQL already containing 'xhs_pull').
    """
    if not _has_table(conn, "radar_runs"):
        return

    if not _has_column(conn, "radar_runs", "n_failed_queries"):
        conn.execute(
            "ALTER TABLE radar_runs ADD COLUMN n_failed_queries INTEGER DEFAULT 0"
        )
        conn.commit()

    # Relax the status CHECK only if the stored definition still lacks 'partial'.
    if "'partial'" not in _radar_runs_status_check(conn):
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE radar_runs__new (
                run_id        TEXT PRIMARY KEY,
                started_at    TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at  TEXT,
                status        TEXT NOT NULL DEFAULT 'running'
                              CHECK(status IN ('running','completed','partial','failed')),
                source        TEXT NOT NULL
                              CHECK(source IN ('cron','manual','forward')),
                n_queries     INTEGER DEFAULT 0,
                n_fetched     INTEGER DEFAULT 0,
                n_kept        INTEGER DEFAULT 0,
                n_indexed     INTEGER DEFAULT 0,
                n_pushed      INTEGER DEFAULT 0,
                n_spar_fired  INTEGER DEFAULT 0,
                n_failed_queries INTEGER DEFAULT 0,
                error         TEXT
            );
            INSERT INTO radar_runs__new
                (run_id, started_at, completed_at, status, source,
                 n_queries, n_fetched, n_kept, n_indexed, n_pushed,
                 n_spar_fired, n_failed_queries, error)
            SELECT run_id, started_at, completed_at, status, source,
                   n_queries, n_fetched, n_kept, n_indexed, n_pushed,
                   n_spar_fired, n_failed_queries, error
            FROM radar_runs;
            DROP TABLE radar_runs;
            ALTER TABLE radar_runs__new RENAME TO radar_runs;
            COMMIT;
            PRAGMA foreign_keys=ON;
            CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON radar_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_radar_runs_status ON radar_runs(status);
            """
        )
        conn.commit()

    # Expand the source CHECK to allow 'xhs_pull' if not already present.
    if "'xhs_pull'" not in _radar_runs_status_check(conn):
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE radar_runs__new (
                run_id        TEXT PRIMARY KEY,
                started_at    TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at  TEXT,
                status        TEXT NOT NULL DEFAULT 'running'
                              CHECK(status IN ('running','completed','partial','failed')),
                source        TEXT NOT NULL
                              CHECK(source IN ('cron','manual','forward','xhs_pull')),
                n_queries     INTEGER DEFAULT 0,
                n_fetched     INTEGER DEFAULT 0,
                n_kept        INTEGER DEFAULT 0,
                n_indexed     INTEGER DEFAULT 0,
                n_pushed      INTEGER DEFAULT 0,
                n_spar_fired  INTEGER DEFAULT 0,
                n_failed_queries INTEGER DEFAULT 0,
                error         TEXT
            );
            INSERT INTO radar_runs__new
                (run_id, started_at, completed_at, status, source,
                 n_queries, n_fetched, n_kept, n_indexed, n_pushed,
                 n_spar_fired, n_failed_queries, error)
            SELECT run_id, started_at, completed_at, status, source,
                   n_queries, n_fetched, n_kept, n_indexed, n_pushed,
                   n_spar_fired, n_failed_queries, error
            FROM radar_runs;
            DROP TABLE radar_runs;
            ALTER TABLE radar_runs__new RENAME TO radar_runs;
            COMMIT;
            PRAGMA foreign_keys=ON;
            CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON radar_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_radar_runs_status ON radar_runs(status);
            """
        )
        conn.commit()


def _migrate_radar_signals(conn: sqlite3.Connection) -> None:
    """Idempotently bring an existing radar_signals table up to the current shape.

    Migration is guarded by inspecting the stored CREATE TABLE SQL: if it already
    contains 'xhs_pull' AND a 'kind' column, the migration has already been applied
    and this function is a no-op (safe to call repeatedly).

    When migration is needed: copy→drop→rename (same pattern as _migrate_radar_runs)
    to expand the source CHECK to include 'xhs_pull' and add the nullable columns
    'kind' (TEXT) and 'source_url' (TEXT). All existing rows are preserved.
    """
    if not _has_table(conn, "radar_signals"):
        return

    stored_sql = _radar_signals_create_sql(conn)
    already_migrated = "'xhs_pull'" in stored_sql and "kind" in stored_sql
    if already_migrated:
        return

    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        BEGIN;
        CREATE TABLE radar_signals__new (
            signal_id            TEXT PRIMARY KEY,
            source               TEXT NOT NULL
                                 CHECK(source IN ('arxiv','s2','twitter','xhs','xhs_pull')),
            source_ref           TEXT NOT NULL,
            query_id             TEXT,
            title                TEXT NOT NULL,
            abstract             TEXT,
            authors              TEXT,
            published_at         TEXT,
            arxiv_id             TEXT,
            s2_paper_id          TEXT,
            importance           INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
            importance_breakdown TEXT NOT NULL,
            scoring_method       TEXT NOT NULL
                                 CHECK(scoring_method IN ('rule','rule+llm')),
            active_project_hits  TEXT,
            notes                TEXT,
            fetched_at           TEXT NOT NULL DEFAULT (datetime('now')),
            processed_status     TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(processed_status IN ('pending','indexed','skipped','failed')),
            paper_dir            TEXT,
            kind                 TEXT,
            source_url           TEXT,
            UNIQUE(source, source_ref)
        );
        INSERT INTO radar_signals__new
            (signal_id, source, source_ref, query_id, title, abstract,
             authors, published_at, arxiv_id, s2_paper_id, importance,
             importance_breakdown, scoring_method, active_project_hits,
             notes, fetched_at, processed_status, paper_dir)
        SELECT signal_id, source, source_ref, query_id, title, abstract,
               authors, published_at, arxiv_id, s2_paper_id, importance,
               importance_breakdown, scoring_method, active_project_hits,
               notes, fetched_at, processed_status, paper_dir
        FROM radar_signals;
        DROP TABLE radar_signals;
        ALTER TABLE radar_signals__new RENAME TO radar_signals;
        COMMIT;
        PRAGMA foreign_keys=ON;
        CREATE INDEX IF NOT EXISTS idx_radar_signals_importance ON radar_signals(importance);
        CREATE INDEX IF NOT EXISTS idx_radar_signals_fetched ON radar_signals(fetched_at);
        CREATE INDEX IF NOT EXISTS idx_radar_signals_paper ON radar_signals(paper_dir);
        CREATE INDEX IF NOT EXISTS idx_radar_signals_query ON radar_signals(query_id);
        """
    )
    conn.commit()


def _migrate_review_inbox(conn: sqlite3.Connection) -> None:
    """Idempotently add the dedupe_key UNIQUE column to an existing review_inbox table.

    Fresh DBs get the column from the CREATE TABLE IF NOT EXISTS in radar_schema.sql.
    Existing DBs need an explicit ALTER (SQLite cannot add UNIQUE via ALTER TABLE,
    so we use the copy-drop-rename pattern, preserving all rows).
    """
    if not _has_table(conn, "review_inbox"):
        return
    if _has_column(conn, "review_inbox", "dedupe_key"):
        return  # already migrated

    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        BEGIN;
        CREATE TABLE review_inbox__new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            payload     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            resolved    INTEGER NOT NULL DEFAULT 0,
            dedupe_key  TEXT UNIQUE
        );
        INSERT INTO review_inbox__new (id, kind, payload, created_at, resolved)
        SELECT id, kind, payload, created_at, resolved FROM review_inbox;
        DROP TABLE review_inbox;
        ALTER TABLE review_inbox__new RENAME TO review_inbox;
        COMMIT;
        PRAGMA foreign_keys=ON;
        """
    )
    conn.commit()


def ensure_radar_schema(conn: sqlite3.Connection) -> None:
    """Create radar tables + add papers.source column (idempotent).

    The papers.source ALTER is guarded on the M1a papers table existing.
    radar_scan opens the DB via cortex_research.db.connect() which ensures
    the M1a paper schema first, but a bare connection (tests / fresh DB) may
    not have it. Skip the ALTER gracefully rather than crash with
    "no such table: papers"; the column gets added on a later ensure once
    papers exists.
    """
    # Migrate BEFORE the executescript: the schema file's CREATE TABLE IF NOT
    # EXISTS is a no-op on an existing table, so the CHECK/column upgrades must
    # be applied explicitly against the pre-existing tables.
    _migrate_radar_runs(conn)
    _migrate_radar_signals(conn)
    _migrate_review_inbox(conn)
    sql = _SCHEMA_FILE.read_text(encoding="utf-8")
    conn.executescript(sql)
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "source"):
        conn.execute("ALTER TABLE papers ADD COLUMN source TEXT DEFAULT 'reader'")
        conn.commit()
    # arxiv_id: authoritative dedup key, decoupled from the paper_dir naming
    # convention (reader/agent = full-title, radar = arxiv:<id>). NULL for
    # non-arxiv (reader) papers. Mirrors the lazy source-column ALTER above.
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "arxiv_id"):
        conn.execute("ALTER TABLE papers ADD COLUMN arxiv_id TEXT")
        conn.commit()
    # published_at: arxiv publish date. The dir-prefix + `date` follow the reader
    # convention (= INGEST date); the publish date is kept separately here so it
    # is not lost. Lazy ALTER, mirrors arxiv_id/source above.
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "published_at"):
        conn.execute("ALTER TABLE papers ADD COLUMN published_at TEXT")
        conn.commit()
    # full_text_source: how the body was extracted ('html'|'ocr'|'pdf'). A 'pdf'
    # row is a plain-text (no-formula, no-image) dump — makes "why is this paper
    # plain text" answerable from the DB and lets a backfill target every pdf body.
    # NULL for reader/legacy rows. Lazy ALTER, mirrors arxiv_id/published_at above.
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "full_text_source"):
        conn.execute("ALTER TABLE papers ADD COLUMN full_text_source TEXT")
        conn.commit()
    # source_url: normalized origin URL for a NON-arxiv PDF ingest (ingest_pdf_url).
    # The dedup key so re-ingesting the same URL is a no-op/update. NULL for
    # arxiv/reader papers. Lazy ALTER, mirrors full_text_source above.
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "source_url"):
        conn.execute("ALTER TABLE papers ADD COLUMN source_url TEXT")
        conn.commit()
    # repo_urls: JSON array of resolved repo URLs (Spec 2 repo grounding) so the
    # radar/director can see a paper's repo without opening the sidecar. Additive,
    # column-exists-guarded, in the SAME applied-on-open path index_papers.index_paper
    # runs (connect + apply_schema + ensure_radar_schema). Lazy ALTER mirrors
    # published_at/arxiv_id/source above.
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "repo_urls"):
        conn.execute("ALTER TABLE papers ADD COLUMN repo_urls TEXT")
        conn.commit()
    # repo_urls_source: provenance of repo_urls ('human'|'agent'|'mined'; NULL legacy
    # ≈ mined). Pins (human/agent) survive automatic re-mines — the correction-
    # persistence half of the Helios no_repo false-negative fix. Lazy ALTER mirrors
    # repo_urls above.
    if _has_table(conn, "papers") and not _has_column(conn, "papers", "repo_urls_source"):
        conn.execute("ALTER TABLE papers ADD COLUMN repo_urls_source TEXT")
        conn.commit()


# repo_urls_source precedence (higher wins; NULL legacy reads as 'mined').
_REPO_SOURCE_RANK = {"mined": 0, "agent": 1, "human": 2}


def set_repo_urls(conn: sqlite3.Connection, paper_dir: str, urls: list[str],
                  source: str = "mined") -> str:
    """Store a paper's resolved repo URLs (JSON array) + provenance, honoring pins.

    Returns an outcome string (callers that promise persistence MUST check it —
    fail-loud, never assume the write landed):
      * 'written'       — row updated (urls + repo_urls_source committed).
      * 'skipped_pin'   — refused: an existing higher-precedence pin holds the row
                          (mined never overwrites human/agent; agent never
                          overwrites human; human always wins; equal rank rewrites).
      * 'skipped_empty' — refused: a mined [] (absence) must not erase a previously
                          stored non-empty list (absence never destroys knowledge).
      * 'no_row'        — UPDATE matched 0 rows (paper_dir not in papers).

    `source` is one of 'human'|'agent'|'mined' (unknown values rank as mined).
    Requires ensure_radar_schema (both columns present); a pre-migration DB raises
    the usual sqlite OperationalError. `conn` must use sqlite3.Row (db.connect()
    does) — the precedence read indexes columns by name."""
    new_rank = _REPO_SOURCE_RANK.get(source, 0)
    row = conn.execute(
        "SELECT repo_urls, repo_urls_source FROM papers WHERE paper_dir=? LIMIT 1",
        (paper_dir,),
    ).fetchone()
    if row is None:
        return "no_row"
    cur_source = row["repo_urls_source"] if row["repo_urls_source"] else "mined"
    cur_rank = _REPO_SOURCE_RANK.get(cur_source, 0)
    if new_rank < cur_rank:
        return "skipped_pin"
    urls = list(urls or [])
    if not urls and source == "mined":
        # A mined absence never erases previously stored URLs of any provenance.
        cur_urls = _parse_url_list(row["repo_urls"])
        if cur_urls:
            return "skipped_empty"
    payload = json.dumps(urls, ensure_ascii=False)
    cur = conn.execute(
        "UPDATE papers SET repo_urls=?, repo_urls_source=? WHERE paper_dir=?",
        (payload, source, paper_dir),
    )
    conn.commit()
    return "written" if cur.rowcount else "no_row"


def _parse_url_list(raw) -> list[str]:
    """Parse a stored repo_urls cell to a list; corrupt/NULL reads as []."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


def get_repo_pin(conn: sqlite3.Connection, paper_dir: str) -> dict:
    """Read repo_urls + provenance: {'urls': [...], 'source': 'human'|'agent'|'mined'|None}.

    source is None when the row is absent or never written. The resolver treats
    only human/agent sources as a resolution leg (mined values came from the
    resolver itself — the sidecar is their cache, not an input)."""
    row = conn.execute(
        "SELECT repo_urls, repo_urls_source FROM papers WHERE paper_dir=? LIMIT 1",
        (paper_dir,),
    ).fetchone()
    if row is None:
        return {"urls": [], "source": None}
    urls = _parse_url_list(row["repo_urls"])
    source = row["repo_urls_source"]
    if source is None and row["repo_urls"] is not None:
        source = "mined"  # legacy pre-provenance write
    return {"urls": urls, "source": source}


def get_repo_urls(conn: sqlite3.Connection, paper_dir: str) -> list[str]:
    """Read a paper's repo_urls JSON array back as a list.

    Returns [] when the row is absent, the column is NULL/empty, or the stored
    text is not a parseable JSON list (best-effort: a corrupt value reads as []
    rather than crashing a caller). The repo_urls column must exist (post
    ensure_radar_schema) — a pre-migration DB raises the usual OperationalError."""
    row = conn.execute(
        "SELECT repo_urls FROM papers WHERE paper_dir=? LIMIT 1", (paper_dir,)
    ).fetchone()
    if row is None:
        return []
    raw = row[0]
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []
