"""Migration 22: recommendation reviews, weekly fallback runs and their items."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control import schema
from cortex_platform.product.control.schema import (
    MIGRATION_VERSIONS,
    SCHEMA_VERSION,
    XHS_FALLBACK_MIGRATION,
    XHS_SOURCES_MIGRATION,
)
from cortex_platform.tests.product.control.test_fragments import (
    DeterministicIds,
    MovableClock,
)
from cortex_platform.tests.product.control.test_xhs_schema import (
    NOTE,
    OTHER_NOTE,
    STAMP,
    USER,
    _note,
    _recommendation,
    _source,
    _task,
    _through,
)

TABLES = {"xhs_recommendation_reviews", "xhs_fallback_runs", "xhs_fallback_items"}
SHA = "a" * 64


def _rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    names = [
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master WHERE type = 'table'
               AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'
               ORDER BY name"""
        )
    ]
    return {name: sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall()) for name in names}


def _objects(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): str(sql)
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }


def _seed(conn: sqlite3.Connection) -> None:
    """Schema-21 rows of every kind the new tables point at."""

    conn.execute(
        """INSERT INTO xhs_bloggers (user_id, role, followed, created_at, updated_at)
           VALUES (?, 'curator', 1, 'x', 'x')""",
        (USER,),
    )
    _source(conn, "note-source", "xhs_note", "xhs", NOTE)
    _note(conn, NOTE, "note-source")
    _note(conn, OTHER_NOTE, None)
    _recommendation(conn, "rec-1", NOTE)
    _recommendation(conn, "rec-2", NOTE)
    _recommendation(conn, "rec-other", OTHER_NOTE)
    _task(conn, "task-1", f"detail:{NOTE}")


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db", clock=MovableClock(), id_factory=DeterministicIds()
    )
    value.initialize()
    return value


@pytest.fixture
def conn(store: ControlStore):
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA foreign_keys = ON")
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


# -- the migration ------------------------------------------------------------


def test_migration_22_is_the_newest_and_follows_xhs_sources() -> None:
    assert XHS_FALLBACK_MIGRATION == XHS_SOURCES_MIGRATION + 1 == 22
    assert SCHEMA_VERSION == XHS_FALLBACK_MIGRATION == max(MIGRATION_VERSIONS)
    assert list(MIGRATION_VERSIONS) == list(range(1, SCHEMA_VERSION + 1))


def test_a_fresh_store_has_the_three_empty_tables(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as connection:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
        assert TABLES <= names
        assert "xhs_fallback_runs_running_idx" in names
        for table in TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migrating_from_21_keeps_every_row_and_object_and_adds_empty_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    _through(database, XHS_FALLBACK_MIGRATION - 1)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _seed(connection)
        rows_before = _rows(connection)
        objects_before = _objects(connection)
    with sqlite3.connect(database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        schema.apply_migrations(connection, now=STAMP)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        rows_after = _rows(connection)
        objects_after = _objects(connection)
        assert {name: rows for name, rows in rows_after.items() if name not in TABLES} == (
            rows_before
        )
        assert all(rows_after[name] == [] for name in TABLES)
        # No existing table, index or trigger was rebuilt or altered.
        assert {name: sql for name, sql in objects_after.items() if name in objects_before} == (
            objects_before
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_replaying_the_migrations_changes_nothing(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as connection:
        before = _objects(connection), connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
    store.initialize()
    with sqlite3.connect(store.path, isolation_level=None) as connection:
        schema.apply_migrations(connection, now="later")
        after = _objects(connection), connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert after == before


def test_an_interrupted_migration_22_leaves_schema_21_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    _through(database, XHS_FALLBACK_MIGRATION - 1)
    with sqlite3.connect(database) as connection:
        _seed(connection)
        rows_before = _rows(connection)
        objects_before = _objects(connection)
    original = schema._execute_script_in_transaction
    script_22 = dict(schema.migration_scripts())[XHS_FALLBACK_MIGRATION]

    def interrupt(connection: sqlite3.Connection, script: str) -> None:
        original(connection, script)
        if script == script_22:
            raise RuntimeError("simulated migration interruption")

    with sqlite3.connect(database, isolation_level=None) as connection:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(connection, now=STAMP)
        assert _rows(connection) == rows_before
        assert _objects(connection) == objects_before
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            21,
        )


# -- constraints ----------------------------------------------------------------


def _run(conn, run_id: str, state: str = "running") -> None:
    conn.execute(
        """INSERT INTO xhs_fallback_runs (id, state, trigger, started_at, finished_at,
               item_cap, model, effort, prompt_version, created_at, updated_at)
           VALUES (?, ?, 'schedule', 'x', ?, 100, 'gpt-6.1-sol', 'xhigh', 'p1', 'x', 'x')""",
        (run_id, state, None if state == "running" else "y"),
    )


def _review(conn, rec_id: str, state: str = "resolved_paper", **values) -> None:
    row = {
        "method": "rule", "reason_code": None, "reason": None,
        "corrected_fields": "[]", "duplicate_of": None, "run_id": None,
    } | values
    conn.execute(
        """INSERT INTO xhs_recommendation_reviews (recommendation_id, state, method,
               reason_code, reason, corrected_fields, duplicate_of, run_id,
               created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x', 'x')""",
        (rec_id, state, row["method"], row["reason_code"], row["reason"],
         row["corrected_fields"], row["duplicate_of"], row["run_id"]),
    )


def _item(conn, item_id: str, run_id: str, rec_id: str, ordinal: int, **values) -> None:
    row = {"state": "pending", "lease_until": None, "applied": None, "attempts": 0,
           "input_sha256": SHA} | values
    conn.execute(
        """INSERT INTO xhs_fallback_items (id, run_id, recommendation_id, ordinal,
               expected_revision, input_sha256, state, applied, attempts,
               next_attempt_at, lease_until, created_at, updated_at)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 'x', ?, 'x', 'x')""",
        (item_id, run_id, rec_id, ordinal, row["input_sha256"], row["state"],
         row["applied"], row["attempts"], row["lease_until"]),
    )


def test_one_run_at_a_time_and_a_finished_run_names_its_end(conn) -> None:
    _run(conn, "run-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _run(conn, "run-2")
    _run(conn, "run-0", state="completed")
    for statement in (
        "UPDATE xhs_fallback_runs SET finished_at = 'y' WHERE id = 'run-1'",
        "UPDATE xhs_fallback_runs SET digest_state = 'pending' WHERE id = 'run-1'",
        "UPDATE xhs_fallback_runs SET item_cap = 101 WHERE id = 'run-1'",
        "UPDATE xhs_fallback_runs SET trigger = 'force' WHERE id = 'run-1'",
        "UPDATE xhs_fallback_runs SET summary = '[]' WHERE id = 'run-1'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(statement)
    conn.execute(
        """UPDATE xhs_fallback_runs SET state = 'completed', finished_at = 'y',
               digest_state = 'pending' WHERE id = 'run-1'"""
    )
    _run(conn, "run-2")


def test_reviews_keep_their_reason_codes_fields_and_duplicates_consistent(conn) -> None:
    _review(conn, "rec-1", "excluded", reason_code="duplicate", duplicate_of="rec-2")
    refused = [
        # Excluded and needs_operator name why.
        ("rec-2", "excluded", {}),
        ("rec-2", "needs_operator", {"method": "model"}),
        ("rec-2", "resolved_paper", {"reason_code": "because"}),
        ("rec-2", "resolved_paper", {"corrected_fields": '["arxiv_id","kind"]'}),
        ("rec-2", "resolved_paper", {"corrected_fields": '["title"]'}),
        ("rec-2", "resolved_paper", {"reason": "x" * 501}),
        ("rec-2", "operator_owned", {"method": "operator", "reason": "kept"}),
        ("rec-2", "operator_owned", {"method": "model"}),
        # A duplicate is another row, and only a duplicate names one.
        ("rec-2", "excluded", {"reason_code": "duplicate", "duplicate_of": "rec-2"}),
        ("rec-2", "excluded", {"reason_code": "operator", "duplicate_of": "rec-1"}),
    ]
    for rec_id, state, values in refused:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _review(conn, rec_id, state, **values)
    with pytest.raises(sqlite3.IntegrityError, match="same note"):
        _review(conn, "rec-2", "excluded", reason_code="duplicate", duplicate_of="rec-other")
    with pytest.raises(sqlite3.IntegrityError, match="same note"):
        conn.execute(
            "UPDATE xhs_recommendation_reviews SET duplicate_of = 'rec-other' "
            "WHERE recommendation_id = 'rec-1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _review(conn, "rec-missing")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _review(conn, "rec-2", run_id="run-missing")
    _review(conn, "rec-2", "operator_owned", method="operator",
            corrected_fields='["kind","arxiv_id"]')


def test_items_hold_one_lease_per_stage_and_record_what_was_applied(conn) -> None:
    _run(conn, "run-1")
    _item(conn, "item-1", "run-1", "rec-1", 1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _item(conn, "item-2", "run-1", "rec-1", 2)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _item(conn, "item-2", "run-1", "rec-2", 1)
    refused = [
        {"ordinal": 101},
        {"input_sha256": "A" * 64},
        {"input_sha256": "a" * 63},
        {"state": "deciding"},
        {"state": "done"},
        {"applied": "excluded"},
        {"state": "done", "applied": "imported"},
        {"lease_until": "z"},
        {"attempts": 4},
    ]
    for values in refused:
        ordinal = values.pop("ordinal", 2)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _item(conn, "item-2", "run-1", "rec-2", ordinal, **values)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _item(conn, "item-2", "run-missing", "rec-2", 2)
    for statement in (
        # A pending item has made no call and holds no answer.
        "UPDATE xhs_fallback_items SET call_state = 'may_have_started' WHERE id = 'item-1'",
        "UPDATE xhs_fallback_items SET state = 'verifying' WHERE id = 'item-1'",
        "UPDATE xhs_fallback_items SET proposal = '{}' WHERE id = 'item-1'",
        "UPDATE xhs_fallback_items SET last_error = 'Bad error' WHERE id = 'item-1'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(statement)
    conn.execute(
        """UPDATE xhs_fallback_items SET state = 'deciding', lease_until = 'z',
               call_state = 'may_have_started' WHERE id = 'item-1'"""
    )
    conn.execute(
        """UPDATE xhs_fallback_items SET state = 'verifying', lease_until = NULL,
               call_state = 'finished', proposal = '{"outcome":"corrected_url"}'
           WHERE id = 'item-1'"""
    )
    conn.execute(
        """UPDATE xhs_fallback_items SET state = 'done', applied = 'blog_queued'
           WHERE id = 'item-1'"""
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
