"""SQLite helpers for research.db (paper index + ledger share one DB)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import sqlite_vec

_SCHEMAS = ["paper_index_schema.sql", "ledger_schema.sql", "crux_schema.sql", "teaching_schema.sql"]


def research_db_path() -> Path:
    return Path(
        os.environ.get(
            "CORTEX_RESEARCH_DB",
            str(Path.home() / ".local/state/cortex/research/research.db"),
        )
    )


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or research_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    # CORTEX: research.db is now written by BOTH the radar/distill crons AND the
    # on-demand agent ingest tool, so concurrent writers are expected. WAL lets a
    # reader proceed during a write and a writer wait rather than fail; the longer
    # busy_timeout absorbs a cron's multi-second write transaction (the old 5s
    # surfaced "database is locked" when an ingest raced a running radar scan).
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # e.g. read-only mount — fall back to default journaling
    conn.execute("PRAGMA busy_timeout = 30000")
    # CORTEX (Layer-A crux ledger): v_crux_current_edge is a VIEW on idea_seeds,
    # but idea_seeds is rebuilt (drop+recreate+rename) by m1d_schema.py's guarded
    # CHECK-widen migrations. SQLite 3.25+ revalidates every dependent view at the
    # `ALTER TABLE ... RENAME` step, so the momentarily-absent idea_seeds makes the
    # rename raise `error in view v_crux_current_edge: no such table: idea_seeds`.
    # legacy_alter_table restores pre-3.25 RENAME semantics (skip view/trigger
    # revalidation) — the SQLite-documented idiom for drop-rename migrations with
    # dependent views. It only changes RENAME behavior, not constraint enforcement.
    conn.execute("PRAGMA legacy_alter_table = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    here = Path(__file__).parent
    for name in _SCHEMAS:
        sql_path = here / name
        if sql_path.exists():
            conn.executescript(sql_path.read_text(encoding="utf-8"))
