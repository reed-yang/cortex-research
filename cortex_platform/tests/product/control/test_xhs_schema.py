"""Migration 21: XHS plugin tables, content bindings, links and the schedule rebuild."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control import schema
from cortex_platform.product.control.schema import (
    IDEA_FRAGMENTS_MIGRATION,
    MIGRATION_VERSIONS,
    SCHEMA_VERSION,
    XHS_SOURCES_MIGRATION,
)
from cortex_platform.tests.product.control.test_fragments import (
    DeterministicIds,
    MovableClock,
)

NOTE = "0123456789abcdef01234567"
OTHER_NOTE = "89abcdef0123456789abcdef"
USER = "fedcba9876543210fedcba98"
TABLES = {
    "xhs_bloggers",
    "xhs_notes",
    "xhs_note_images",
    "xhs_recommendations",
    "source_content_bindings",
    "source_links",
    "xhs_tasks",
    "xhs_usage",
}
STAMP = "2026-09-01T12:00:00.000000Z"


def _through(database: Path, last: int) -> None:
    """A control database migrated exactly through `last`, as an older build left it."""

    with sqlite3.connect(database) as seed:
        for version, script in schema.migration_scripts():
            if version > last:
                break
            seed.executescript(script)
            seed.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )


def _schedule_rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT * FROM research_schedules ORDER BY job_key"
    ).fetchall()


def _schedule_objects(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): str(sql)
        for name, sql in conn.execute(
            """SELECT name, sql FROM sqlite_master
               WHERE tbl_name = 'research_schedules' AND sql IS NOT NULL
                 AND type IN ('index', 'trigger')"""
        )
    }


def _seed_schedules(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO research_schedules
           (job_key, operation, enabled, interval_seconds, next_due_at,
            last_started_at, last_finished_at, last_outcome, cadence_source,
            legacy_schedule, revision, created_at, updated_at)
           VALUES ('capture-drain', 'capture_drain', 1, 300, '2026-09-30T10:05:00Z',
                   '2026-09-30T10:00:00Z', '2026-09-30T10:00:01Z', 'ran', 'product',
                   NULL, 41, '2026-09-01T00:00:00Z', '2026-09-30T10:00:01Z')"""
    )
    conn.execute(
        """INSERT INTO research_schedules
           (job_key, operation, enabled, interval_seconds, next_due_at,
            cadence_source, legacy_schedule, revision, created_at, updated_at)
           VALUES ('xhs-pull-scan', 'legacy', 0, 86400, '2026-09-01T00:00:00Z',
                   'migrated', 'cron:0 8 * * *', 0, '2026-09-01T00:00:00Z',
                   '2026-09-01T00:00:00Z')"""
    )


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db", clock=MovableClock(), id_factory=DeterministicIds()
    )
    value.initialize()
    return value


# -- the migration ------------------------------------------------------------


def test_migration_21_is_the_newest_and_follows_fragments() -> None:
    assert XHS_SOURCES_MIGRATION == IDEA_FRAGMENTS_MIGRATION + 1 == 21
    assert SCHEMA_VERSION == XHS_SOURCES_MIGRATION == max(MIGRATION_VERSIONS)
    assert list(MIGRATION_VERSIONS) == list(range(1, SCHEMA_VERSION + 1))


def test_a_fresh_store_has_every_table_and_both_disabled_jobs(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        assert TABLES <= names
        jobs = conn.execute(
            """SELECT job_key, operation, enabled, interval_seconds, cadence_source,
                      legacy_schedule, last_outcome
               FROM research_schedules ORDER BY job_key"""
        ).fetchall()
    assert jobs == [
        ("xhs-drain", "xhs_drain", 0, 300, "product", None, None),
        ("xhs-pull", "xhs_pull", 0, 86400, "product", None, None),
    ]


def test_migrating_from_20_keeps_every_schedule_row_index_and_trigger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    _through(database, XHS_SOURCES_MIGRATION - 1)
    with sqlite3.connect(database) as conn:
        _seed_schedules(conn)
        rows_before = _schedule_rows(conn)
        objects_before = _schedule_objects(conn)
        assert set(objects_before) == {
            "research_schedules_due_idx",
            "research_schedules_delete_guard",
            "research_schedules_identity_guard",
        }
    with sqlite3.connect(database, isolation_level=None) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        schema.apply_migrations(conn, now=STAMP)
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        rows_after = _schedule_rows(conn)
        # Every old row is carried over column for column.
        assert [row for row in rows_after if row[0] not in {"xhs-pull", "xhs-drain"}] == (
            rows_before
        )
        assert {row[0] for row in rows_after} == {
            "capture-drain", "xhs-pull-scan", "xhs-pull", "xhs-drain",
        }
        assert _schedule_objects(conn) == objects_before
        assert conn.execute(
            "SELECT enabled FROM research_schedules WHERE job_key IN ('xhs-pull', 'xhs-drain')"
        ).fetchall() == [(0,), (0,)]
        assert {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")} >= TABLES


def test_the_rebuilt_schedule_table_enforces_its_check_and_guards(
    store: ControlStore,
) -> None:
    with sqlite3.connect(store.path) as conn:
        for operation in ("xhs_pull", "xhs_drain", "capture_drain", "legacy"):
            conn.execute(
                """INSERT INTO research_schedules
                   (job_key, operation, enabled, interval_seconds, next_due_at,
                    cadence_source, created_at, updated_at)
                   VALUES (?, ?, 0, 300, 'x', 'product', 'x', 'x')""",
                (f"probe-{operation}", operation),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                """INSERT INTO research_schedules
                   (job_key, operation, enabled, interval_seconds, next_due_at,
                    cadence_source, created_at, updated_at)
                   VALUES ('probe-bad', 'xhs_scan', 0, 300, 'x', 'product', 'x', 'x')"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            conn.execute("DELETE FROM research_schedules WHERE job_key = 'xhs-pull'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_schedules SET operation = 'capture_drain' WHERE job_key = 'xhs-pull'"
            )


def test_the_store_admits_the_plugin_operations_and_refuses_others(
    store: ControlStore,
) -> None:
    pull = store.get_research_schedule("xhs-pull")
    armed = store.set_research_schedule_enabled(
        job_key="xhs-pull", enabled=True, expected_revision=int(pull["revision"]),
        actor_id="local-operator", idempotency_key="enable-xhs-pull-0001",
    ).value
    assert armed["enabled"] is True and armed["operation"] == "xhs_pull"
    with pytest.raises(ValueError, match="unsupported"):
        store.register_research_schedule(
            job_key="other", operation="xhs_scan", enabled=False, interval_seconds=300,
            cadence_source="product", legacy_schedule=None, actor_id="local-operator",
            idempotency_key="register-other-00001",
        )


def test_an_interrupted_migration_21_leaves_schema_20_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    _through(database, XHS_SOURCES_MIGRATION - 1)
    with sqlite3.connect(database) as conn:
        _seed_schedules(conn)
        rows_before = _schedule_rows(conn)
        objects_before = _schedule_objects(conn)
    original = schema._execute_script_in_transaction
    script_21 = dict(schema.migration_scripts())[XHS_SOURCES_MIGRATION]

    def interrupt(conn: sqlite3.Connection, script: str) -> None:
        original(conn, script)
        if script == script_21:
            raise RuntimeError("simulated migration interruption")

    with sqlite3.connect(database, isolation_level=None) as conn:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(conn, now=STAMP)
        assert _schedule_rows(conn) == rows_before
        assert _schedule_objects(conn) == objects_before
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        assert not names & (TABLES | {"research_schedules_xhs"})
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (20,)


# -- constraints ----------------------------------------------------------------


def _source(conn, source_id: str, kind: str, authority: str, authority_id: str) -> None:
    conn.execute(
        """INSERT INTO sources (id, authority, authority_id, canonical_id, source_kind,
               official_title, engine_ref, import_state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'Title', ?, 'existing', 'x', 'x')""",
        (source_id, authority, authority_id, f"{authority}:{authority_id}", kind,
         f"ref-{source_id}:x"),
    )


def _note(conn, note_id: str, source_id: str | None) -> None:
    conn.execute(
        """INSERT INTO xhs_notes (note_id, user_id, note_type, state, title, caption,
               caption_complete, source_id, content_version, created_at, updated_at)
           VALUES (?, ?, 'normal', ?, '', 'caption', 1, ?, ?, 'x', 'x')""",
        (note_id, USER, "saved" if source_id else "discovered", source_id,
         1 if source_id else 0),
    )


def _recommendation(conn, rec_id: str, note_id: str) -> None:
    conn.execute(
        """INSERT INTO xhs_recommendations (id, note_id, item_key, kind, title, quote,
               origin, identify_run, created_at, updated_at)
           VALUES (?, ?, ?, 'blog', 'A blog', 'A blog', 'model', 'run-1', 'x', 'x')""",
        (rec_id, note_id, f"title:{rec_id}"),
    )


@pytest.fixture
def linked(store: ControlStore):
    conn = sqlite3.connect(store.path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """INSERT INTO xhs_bloggers (user_id, role, followed, created_at, updated_at)
           VALUES (?, 'curator', 1, 'x', 'x')""",
        (USER,),
    )
    _source(conn, "note-source", "xhs_note", "xhs", NOTE)
    _source(conn, "other-note-source", "xhs_note", "xhs", OTHER_NOTE)
    _source(conn, "blog-source", "blog", "url", "a" * 64)
    _source(conn, "paper-source", "paper", "arxiv", "2609.00001")
    _note(conn, NOTE, "note-source")
    _note(conn, OTHER_NOTE, "other-note-source")
    _recommendation(conn, "rec-1", NOTE)
    _recommendation(conn, "rec-2", NOTE)
    _recommendation(conn, "rec-other", OTHER_NOTE)
    conn.commit()
    yield conn
    conn.close()


def _link(conn, link_id: str, from_id: str, to_id: str, rec_id: str) -> None:
    conn.execute(
        """INSERT INTO source_links (id, from_source_id, to_source_id, relation,
               recommendation_id, created_at)
           VALUES (?, ?, ?, 'recommends', ?, 'x')""",
        (link_id, from_id, to_id, rec_id),
    )


def test_links_start_at_the_note_holding_the_recommendation(linked) -> None:
    conn = linked
    _link(conn, "link-1", "note-source", "blog-source", "rec-1")
    _link(conn, "link-other", "other-note-source", "blog-source", "rec-other")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _link(conn, "self", "note-source", "note-source", "rec-2")
    with pytest.raises(sqlite3.IntegrityError, match="starts at its XHS note"):
        _link(conn, "from-paper", "paper-source", "blog-source", "rec-2")
    with pytest.raises(sqlite3.IntegrityError, match="starts at its XHS note"):
        _link(conn, "wrong-note", "other-note-source", "blog-source", "rec-2")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _link(conn, "again", "note-source", "paper-source", "rec-1")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _link(conn, "dangling", "note-source", "missing-source", "rec-2")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE source_links SET to_source_id = 'paper-source'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM source_links")


def _binding(conn, source_id: str, version: int, directory: str, root: str = "xhs-notes"):
    conn.execute(
        """INSERT INTO source_content_bindings
           (source_id, version, root_id, directory, tree_sha256, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, '{}', 'x')""",
        (source_id, version, root, directory, "b" * 64),
    )


def test_binding_versions_are_contiguous_immutable_and_non_paper(linked, tmp_path) -> None:
    conn = linked
    for root_id in ("xhs-notes", "blogs"):
        conn.execute(
            """INSERT INTO asset_roots (root_id, private_path, max_bytes, enabled,
                   created_at, updated_at) VALUES (?, ?, 1024, 1, 'x', 'x')""",
            (root_id, str(tmp_path / root_id)),
        )
    _binding(conn, "note-source", 1, f"{NOTE}/v1")
    _binding(conn, "note-source", 2, f"{NOTE}/v2")
    with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        _binding(conn, "note-source", 4, f"{NOTE}/v4")
    with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        _binding(conn, "note-source", 2, f"{NOTE}/v2b")
    with pytest.raises(sqlite3.IntegrityError, match="non-paper"):
        _binding(conn, "paper-source", 1, "paper/v1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _binding(conn, "blog-source", 1, f"{NOTE}/v1")
    for directory in ("../escape/v1", "/abs/v1", "a//v1", "UPPER/v1"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _binding(conn, "blog-source", 1, directory, root="blogs")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _binding(conn, "blog-source", 1, "aaaaaaaaaaaaaaaa/v1", root="no-such-root")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE source_content_bindings SET tree_sha256 = ?", ("c" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM source_content_bindings")


def _task(conn, task_id: str, subject_key: str, kind: str = "detail") -> None:
    conn.execute(
        """INSERT INTO xhs_tasks (id, kind, subject_key, payload_json, state,
               next_attempt_at, created_at, updated_at)
           VALUES (?, ?, ?, '{}', 'pending', 'x', 'x', 'x')""",
        (task_id, kind, subject_key),
    )


def test_task_subject_keys_are_unique_and_tasks_durable(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        _task(conn, "task-1", f"detail:{NOTE}")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _task(conn, "task-2", f"detail:{NOTE}")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _task(conn, "task-3", "anything:1", kind="unknown")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("UPDATE xhs_tasks SET state = 'running' WHERE id = 'task-1'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE xhs_tasks SET subject_key = 'detail:x' WHERE id = 'task-1'")
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            conn.execute("DELETE FROM xhs_tasks")


def test_image_and_note_rows_keep_their_state_consistent(linked) -> None:
    conn = linked
    conn.execute(
        """INSERT INTO xhs_note_images (note_id, ordinal, created_at, updated_at)
           VALUES (?, 3, 'x', 'x')""",
        (NOTE,),
    )
    refused = [
        # A downloaded image names its bytes and asset.
        "UPDATE xhs_note_images SET download_state = 'ok' WHERE ordinal = 3",
        # A failure carries a category, and only a failure does.
        "UPDATE xhs_note_images SET download_state = 'failed' WHERE ordinal = 3",
        # No transcription before the bytes are retained.
        "UPDATE xhs_note_images SET ocr_state = 'failed', ocr_error = 'transient' WHERE ordinal = 3",
        "UPDATE xhs_note_images SET ordinal = 0 WHERE ordinal = 3",
        "UPDATE xhs_notes SET state = 'saved', source_id = NULL, content_version = 0",
        "UPDATE xhs_notes SET note_id = 'short' WHERE note_id = '" + NOTE + "'",
    ]
    for statement in refused:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
    conn.execute(
        """UPDATE xhs_note_images SET download_state = 'failed', download_error = 'url_expired'
           WHERE ordinal = 3"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            """INSERT INTO xhs_recommendations (id, note_id, item_key, image_ordinal, kind,
                   title, quote, origin, identify_run, created_at, updated_at)
               VALUES ('rec-image', ?, 'k', 9, 'other', 't', 'q', 'rule', 'r', 'x', 'x')""",
            (NOTE,),
        )
