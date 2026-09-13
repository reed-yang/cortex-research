from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control.schema import MIGRATION_VERSIONS
from cortex_platform.product.control import ControlStore, schema
from cortex_platform.product.sources import validate_engine_ref

NOW = "2026-07-23T12:00:00Z"


def _create_v6_database(path: Path) -> None:
    _create_control_db_through(path, 6)


def _create_control_db_through(path: Path, last_version: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        # Walked off `migration_scripts()` rather than `_MIGRATION_{n}`: the
        # scripts are named after what they create, not after the number they
        # were given, and a renumber moves the number only.
        for version, script in schema.migration_scripts():
            if version > last_version:
                break
            schema._execute_script_in_transaction(conn, script)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, NOW),
            )
        conn.commit()


def _seed_run(conn: sqlite3.Connection, suffix: str) -> str:
    workspace_id = f"workspace-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    conn.execute(
        "INSERT INTO workspaces(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (workspace_id, suffix, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO threads(id, workspace_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (thread_id, workspace_id, suffix, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO runs(id, thread_id, state, created_at, updated_at)
        VALUES (?, ?, 'running', ?, ?)
        """,
        (run_id, thread_id, NOW, NOW),
    )
    return run_id


def _insert_workflow(conn: sqlite3.Connection, suffix: str) -> str:
    workflow_id = f"workflow-{suffix}"
    conn.execute(
        """
        INSERT INTO workflow_instances(
            id, run_id, definition_id, definition_version, state,
            created_at, updated_at
        ) VALUES (?, ?, 'research.golden', 1, 'running', ?, ?)
        """,
        (workflow_id, _seed_run(conn, suffix), NOW, NOW),
    )
    return workflow_id


def _insert_stage(
    conn: sqlite3.Connection,
    workflow_id: str,
    stage_id: str,
    stage_key: str,
    position: int,
    effect: str,
    *,
    state: str = "pending",
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_stage_instances(
            id, workflow_id, stage_key, position, effect,
            checkpoint_enabled, state
        ) VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (stage_id, workflow_id, stage_key, position, effect, state),
    )


def _insert_resolved_decision_ref(
    conn: sqlite3.Connection,
    suffix: str,
) -> tuple[str, str, str, str, str]:
    workflow_id = _insert_workflow(conn, suffix)
    run_id = conn.execute(
        "SELECT run_id FROM workflow_instances WHERE id = ?",
        (workflow_id,),
    ).fetchone()[0]
    attempt_id = f"attempt-{suffix}"
    conn.execute(
        """
        INSERT INTO attempts(id, run_id, number, state, started_at)
        VALUES (?, ?, 1, 'active', ?)
        """,
        (attempt_id, run_id, NOW),
    )
    first_stage = f"stage-decision-first-{suffix}"
    second_stage = f"stage-decision-second-{suffix}"
    _insert_stage(conn, workflow_id, first_stage, "decision-first", 0, "decision")
    _insert_stage(conn, workflow_id, second_stage, "decision-second", 1, "decision")
    first_decision = f"decision-first-{suffix}"
    second_decision = f"decision-second-{suffix}"
    for decision_id in (first_decision, second_decision):
        conn.execute(
            """
            INSERT INTO decisions(
                id, run_id, attempt_id, kind, prompt, options_json, state,
                resolution_json, revision, created_at, resolved_at
            ) VALUES (?, ?, ?, 'approval', 'Choose', '["approve"]',
                'resolved', '{"choice":"approve"}', 1, ?, ?)
            """,
            (decision_id, run_id, attempt_id, NOW, NOW),
        )
    conn.execute(
        """
        INSERT INTO workflow_decision_refs(
            id, workflow_id, stage_id, decision_id, expected_revision,
            resolved_revision, selected_choice, state, created_at, resolved_at
        ) VALUES ('decision-ref-original', ?, ?, ?, 0, 1, 'approve',
            'resolved', ?, ?)
        """,
        (workflow_id, first_stage, first_decision, NOW, NOW),
    )
    return (
        workflow_id,
        first_stage,
        second_stage,
        first_decision,
        second_decision,
    )


def test_v7_migrates_from_v6_and_is_repeatable(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    _create_v6_database(database)

    with sqlite3.connect(database) as conn:
        schema.apply_migrations(conn, now=NOW)
        schema.apply_migrations(conn, now=NOW)
        versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert versions == [(version,) for version in MIGRATION_VERSIONS]
    assert {
        "workflow_instances",
        "workflow_stage_instances",
        "workflow_stage_dependencies",
        "workflow_stage_receipt_requirements",
        "workflow_stage_result_requirements",
        "workflow_effect_commands",
        "workflow_stage_references",
        "workflow_checkpoints",
    } <= tables
    assert foreign_key_errors == []


def _apply_and_record(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[int], list[int]]:
    """Migrate `database`, returning (applied order, recorded versions).

    The applied order is read by watching `apply_migrations` execute, not from
    the table it writes. `schema_migrations.version` is `INTEGER PRIMARY KEY`,
    which SQLite makes an alias for the rowid, so the rows are STORED in
    version order however they were inserted: `ORDER BY rowid` returns exactly
    what `ORDER BY version` does and can no more see the applied order than the
    25 existing assertions that use it. Measured, not assumed -- an
    `ORDER BY rowid` version of this test stays green with the 14 and 15 blocks
    swapped.
    """

    scripts = schema.migration_scripts()
    by_script = {script: version for version, script in scripts}
    # A duplicated body would silently map two migrations to one version.
    assert len(by_script) == len(scripts)
    original = schema._execute_script_in_transaction
    applied: list[int] = []

    def record(conn: sqlite3.Connection, script: str) -> None:
        applied.append(by_script[script])
        original(conn, script)

    with monkeypatch.context() as patched:
        patched.setattr(schema, "_execute_script_in_transaction", record)
        with sqlite3.connect(database) as conn:
            schema.apply_migrations(conn, now=NOW)
            recorded = [
                int(version)
                for (version,) in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
    return applied, recorded


def test_the_migrations_are_applied_in_their_declared_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦MRG-D6⟧ The ordering claim three renumbering merges rest on, as a gate.

    Every assertion in the tree that could pin this reads `SELECT version FROM
    schema_migrations ORDER BY version` and compares against
    `MIGRATION_VERSIONS`, which is order-insensitive by construction: it
    re-sorts before comparing, so swapping two of `apply_migrations`'s if-blocks
    leaves all of them green. Those blocks are exactly what each of the three
    renumbering merges hand-edited, and the failure they can produce -- a
    migration running before the one whose table it alters -- shows up only on a
    real upgrade of an operator database, as a `no such table` that rolls the
    whole transaction back and leaves `cortex start` unable to migrate.

    Checked on a fresh database and on the schema-12 state the operator's own
    control store is in before the gen 9 upgrade.
    """

    fresh = tmp_path / "fresh.db"
    applied, recorded = _apply_and_record(fresh, monkeypatch)
    assert applied == list(MIGRATION_VERSIONS)
    assert recorded == list(MIGRATION_VERSIONS)

    upgraded = tmp_path / "schema-12.db"
    _create_control_db_through(upgraded, 12)
    applied, recorded = _apply_and_record(upgraded, monkeypatch)
    # Only the tail is applied, and these four are the ones the merges moved.
    assert applied == list(
        range(schema.CAPTURES_MIGRATION, schema.SCHEMA_VERSION + 1)
    )
    assert recorded == list(range(1, schema.SCHEMA_VERSION + 1))


def test_interrupted_v7_migration_rolls_back_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    _create_v6_database(database)
    original = schema._MIGRATION_7
    monkeypatch.setattr(
        schema,
        "_MIGRATION_7",
        original + "\nCREATE TABLE workflow_fault_injection(\n",
    )

    with sqlite3.connect(database) as conn:
        with pytest.raises(RuntimeError, match="incomplete control-store migration"):
            schema.apply_migrations(conn, now=NOW)
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,)]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'workflow_instances'"
        ).fetchone() is None

        monkeypatch.setattr(schema, "_MIGRATION_7", original)
        schema.apply_migrations(conn, now=NOW)
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]


def test_definition_guards_close_cross_workflow_and_effect_requirements(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        first = _insert_workflow(conn, "first")
        second = _insert_workflow(conn, "second")
        _insert_stage(conn, first, "first-control", "control", 0, "control", state="ready")
        _insert_stage(conn, first, "first-mutation", "mutation", 1, "engine_mutation")
        _insert_stage(conn, first, "first-query", "query", 2, "engine_query")
        _insert_stage(conn, second, "second-control", "control", 0, "control", state="ready")

        dependency_sql = """
            INSERT INTO workflow_stage_dependencies(
                workflow_id, stage_id, dependency_stage_id, position
            ) VALUES (?, ?, ?, 0)
        """
        with pytest.raises(sqlite3.IntegrityError, match="dependency is invalid"):
            conn.execute(dependency_sql, (first, "first-control", "first-mutation"))
        with pytest.raises(sqlite3.IntegrityError, match="dependency is invalid"):
            conn.execute(dependency_sql, (first, "first-mutation", "second-control"))
        conn.execute(dependency_sql, (first, "first-mutation", "first-control"))
        conn.execute(dependency_sql, (first, "first-query", "first-mutation"))

        receipt_sql = """
            INSERT INTO workflow_stage_receipt_requirements(
                workflow_id, stage_id, effect_kind, position
            ) VALUES (?, ?, ?, 0)
        """
        result_sql = """
            INSERT INTO workflow_stage_result_requirements(
                workflow_id, stage_id, result_kind, position
            ) VALUES (?, ?, ?, 0)
        """
        with pytest.raises(sqlite3.IntegrityError, match="receipt requirement is invalid"):
            conn.execute(receipt_sql, (first, "first-control", "source_import"))
        with pytest.raises(sqlite3.IntegrityError, match="result requirement is invalid"):
            conn.execute(result_sql, (first, "first-mutation", "lineage_query"))
        conn.execute(receipt_sql, (first, "first-mutation", "source_import"))
        conn.execute(result_sql, (first, "first-query", "lineage_query"))
        conn.execute(
            "UPDATE workflow_instances SET current_stage_key = 'control' WHERE id = ?",
            (first,),
        )
        conn.execute(
            "UPDATE workflow_instances SET definition_sealed = 1 WHERE id = ?",
            (first,),
        )

        with pytest.raises(sqlite3.IntegrityError, match="definition is sealed"):
            _insert_stage(conn, first, "first-extra", "extra", 3, "control")
        with pytest.raises(sqlite3.IntegrityError, match="definition is immutable"):
            conn.execute(
                "UPDATE workflow_stage_instances SET id = 'changed' WHERE id = ?",
                ("first-control",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE workflow_stage_dependencies SET position = 1 WHERE stage_id = ?",
                ("first-mutation",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM workflow_stage_result_requirements WHERE stage_id = ?",
                ("first-query",),
            )

        command_sql = """
            INSERT INTO workflow_effect_commands(
                id, operation_id, workflow_id, stage_id, effect_class,
                effect_kind, effect_key, request_hash, request_json,
                state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', 'pending', ?)
        """
        conn.execute(
            command_sql,
            (
                "command-mutation",
                "operation-mutation",
                first,
                "first-mutation",
                "mutation",
                "source_import",
                "source_import",
                "a" * 64,
                NOW,
            ),
        )
        conn.execute(
            command_sql,
            (
                "command-query",
                "operation-query",
                first,
                "first-query",
                "query",
                "lineage_query",
                "lineage_query",
                "b" * 64,
                NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="not required"):
            conn.execute(
                command_sql,
                (
                    "command-wrong",
                    "operation-wrong",
                    first,
                    "first-query",
                    "query",
                    "source_import",
                    "wrong",
                    "c" * 64,
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            conn.execute(
                "UPDATE workflow_effect_commands SET effect_key = 'changed' WHERE id = ?",
                ("command-query",),
            )
        conn.execute(
            """
            UPDATE workflow_effect_commands
            SET state = 'completed', result_identity = ?, receipt_json = '{}',
                completed_at = ?
            WHERE id = 'command-query'
            """,
            ("d" * 64, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="terminal workflow effect"):
            conn.execute(
                """
                UPDATE workflow_effect_commands
                SET result_identity = ?, receipt_json = '{"changed":true}'
                WHERE id = 'command-query'
                """,
                ("e" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="deletion is not supported"):
            conn.execute(
                "DELETE FROM workflow_effect_commands WHERE id = 'command-mutation'"
            )


def test_incomplete_definition_cannot_be_sealed(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, "incomplete")
        with pytest.raises(sqlite3.IntegrityError, match="definition is incomplete"):
            conn.execute(
                "UPDATE workflow_instances SET definition_sealed = 1 WHERE id = ?",
                (workflow_id,),
            )

        _insert_stage(
            conn,
            workflow_id,
            "incomplete-query",
            "query",
            0,
            "engine_query",
            state="ready",
        )
        conn.execute(
            "UPDATE workflow_instances SET current_stage_key = 'query' WHERE id = ?",
            (workflow_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="definition is incomplete"):
            conn.execute(
                "UPDATE workflow_instances SET definition_sealed = 1 WHERE id = ?",
                (workflow_id,),
            )
        conn.execute(
            """
            INSERT INTO workflow_stage_result_requirements(
                workflow_id, stage_id, result_kind, position
            ) VALUES (?, 'incomplete-query', 'lineage_query', 0)
            """,
            (workflow_id,),
        )
        conn.execute(
            "UPDATE workflow_instances SET definition_sealed = 1 WHERE id = ?",
            (workflow_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="irreversible"):
            conn.execute(
                "UPDATE workflow_instances SET definition_sealed = 0 WHERE id = ?",
                (workflow_id,),
            )


def test_workflow_cannot_be_inserted_with_a_sealed_definition(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        run_id = _seed_run(conn, "presealed")

        with pytest.raises(sqlite3.IntegrityError, match="must start unsealed"):
            conn.execute(
                """
                INSERT INTO workflow_instances(
                    id, run_id, definition_id, definition_version,
                    definition_sealed, state, created_at, updated_at
                ) VALUES (
                    'workflow-presealed', ?, 'research.golden', 1,
                    1, 'running', ?, ?
                )
                """,
                (run_id, NOW, NOW),
            )

        assert conn.execute(
            "SELECT 1 FROM workflow_instances WHERE id = 'workflow-presealed'"
        ).fetchone() is None


def test_workflow_update_replace_cannot_overwrite_an_unsealed_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        first = _insert_workflow(conn, "update-replace-first")
        second = _insert_workflow(conn, "update-replace-second")
        original = conn.execute(
            "SELECT id, run_id FROM workflow_instances ORDER BY id"
        ).fetchall()

        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            conn.execute(
                "UPDATE OR REPLACE workflow_instances SET id = ? WHERE id = ?",
                (second, first),
            )

        assert conn.execute(
            "SELECT id, run_id FROM workflow_instances ORDER BY id"
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["id", "run_id"])
def test_workflow_insert_replace_cannot_overwrite_an_unsealed_identity(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"insert-replace-{conflict}")
        run_id = conn.execute(
            "SELECT run_id FROM workflow_instances WHERE id = ?",
            (workflow_id,),
        ).fetchone()[0]
        replacement_id = workflow_id if conflict == "id" else "workflow-replacement"
        replacement_run_id = (
            _seed_run(conn, "insert-replace-new-run")
            if conflict == "id"
            else run_id
        )
        original = conn.execute("SELECT * FROM workflow_instances").fetchall()

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_instances(
                    id, run_id, definition_id, definition_version, state,
                    created_at, updated_at
                ) VALUES (?, ?, 'research.changed', 2, 'failed', ?, ?)
                """,
                (replacement_id, replacement_run_id, NOW, NOW),
            )

        assert conn.execute("SELECT * FROM workflow_instances").fetchall() == original


@pytest.mark.parametrize("conflict", ["id", "stage_key", "position"])
def test_workflow_stage_replace_cannot_overwrite_definition_row(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"stage-replace-{conflict}")
        _insert_stage(conn, workflow_id, "stage-original", "original", 0, "control")
        original = conn.execute("SELECT * FROM workflow_stage_instances").fetchall()
        replacement = {
            "id": "stage-replacement",
            "stage_key": "replacement",
            "position": 1,
        }
        if conflict == "id":
            replacement["id"] = "stage-original"
        elif conflict == "stage_key":
            replacement["stage_key"] = "original"
        else:
            replacement["position"] = 0

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_stage_instances(
                    id, workflow_id, stage_key, position, effect,
                    checkpoint_enabled, state
                ) VALUES (?, ?, ?, ?, 'control', 0, 'pending')
                """,
                (
                    replacement["id"],
                    workflow_id,
                    replacement["stage_key"],
                    replacement["position"],
                ),
            )

        assert conn.execute(
            "SELECT * FROM workflow_stage_instances"
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["dependency", "position"])
def test_workflow_dependency_replace_cannot_overwrite_definition_row(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"dependency-replace-{conflict}")
        _insert_stage(conn, workflow_id, "stage-zero", "zero", 0, "control")
        _insert_stage(conn, workflow_id, "stage-one", "one", 1, "control")
        _insert_stage(conn, workflow_id, "stage-two", "two", 2, "control")
        insert_sql = """
            INSERT OR REPLACE INTO workflow_stage_dependencies(
                workflow_id, stage_id, dependency_stage_id, position
            ) VALUES (?, 'stage-two', ?, ?)
        """
        conn.execute(insert_sql, (workflow_id, "stage-zero", 0))
        original = conn.execute("SELECT * FROM workflow_stage_dependencies").fetchall()
        dependency_id = "stage-zero" if conflict == "dependency" else "stage-one"
        position = 1 if conflict == "dependency" else 0

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(insert_sql, (workflow_id, dependency_id, position))

        assert conn.execute(
            "SELECT * FROM workflow_stage_dependencies"
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["effect_kind", "position"])
def test_workflow_receipt_requirement_replace_cannot_overwrite_definition_row(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"receipt-replace-{conflict}")
        stage_id = f"stage-receipt-replace-{conflict}"
        _insert_stage(conn, workflow_id, stage_id, "mutate", 0, "engine_mutation")
        insert_sql = """
            INSERT OR REPLACE INTO workflow_stage_receipt_requirements(
                workflow_id, stage_id, effect_kind, position
            ) VALUES (?, ?, ?, ?)
        """
        conn.execute(insert_sql, (workflow_id, stage_id, "source_import", 0))
        original = conn.execute(
            "SELECT * FROM workflow_stage_receipt_requirements"
        ).fetchall()
        effect_kind = "source_import" if conflict == "effect_kind" else "artifact"
        position = 1 if conflict == "effect_kind" else 0

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(insert_sql, (workflow_id, stage_id, effect_kind, position))

        assert conn.execute(
            "SELECT * FROM workflow_stage_receipt_requirements"
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["result_kind", "position"])
def test_workflow_result_requirement_replace_cannot_overwrite_definition_row(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"result-replace-{conflict}")
        stage_id = f"stage-result-replace-{conflict}"
        _insert_stage(conn, workflow_id, stage_id, "query", 0, "engine_query")
        insert_sql = """
            INSERT OR REPLACE INTO workflow_stage_result_requirements(
                workflow_id, stage_id, result_kind, position
            ) VALUES (?, ?, ?, ?)
        """
        conn.execute(insert_sql, (workflow_id, stage_id, "lineage_query", 0))
        original = conn.execute(
            "SELECT * FROM workflow_stage_result_requirements"
        ).fetchall()
        result_kind = "lineage_query" if conflict == "result_kind" else "source_query"
        position = 1 if conflict == "result_kind" else 0

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(insert_sql, (workflow_id, stage_id, result_kind, position))

        assert conn.execute(
            "SELECT * FROM workflow_stage_result_requirements"
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["id", "stage_id", "decision_id"])
def test_workflow_decision_ref_replace_cannot_overwrite_resolved_identity(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        (
            workflow_id,
            first_stage,
            second_stage,
            first_decision,
            second_decision,
        ) = _insert_resolved_decision_ref(conn, f"decision-replace-{conflict}")
        replacement_id = (
            "decision-ref-original" if conflict == "id" else "decision-ref-replacement"
        )
        stage_id = first_stage if conflict == "stage_id" else second_stage
        decision_id = first_decision if conflict == "decision_id" else second_decision
        original = conn.execute("SELECT * FROM workflow_decision_refs").fetchall()

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_decision_refs(
                    id, workflow_id, stage_id, decision_id, expected_revision,
                    state, created_at
                ) VALUES (?, ?, ?, ?, 9, 'pending', ?)
                """,
                (replacement_id, workflow_id, stage_id, decision_id, NOW),
            )

        assert conn.execute("SELECT * FROM workflow_decision_refs").fetchall() == original


def test_resolved_workflow_decision_ref_cannot_be_reopened_or_deleted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_resolved_decision_ref(conn, "decision-terminal")
        original = conn.execute("SELECT * FROM workflow_decision_refs").fetchall()

        with pytest.raises(sqlite3.IntegrityError, match="terminal.*immutable"):
            conn.execute(
                """
                UPDATE workflow_decision_refs
                SET state = 'pending', resolved_revision = NULL,
                    selected_choice = NULL, resolved_at = NULL
                WHERE id = 'decision-ref-original'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="deletion is not supported"):
            conn.execute(
                "DELETE FROM workflow_decision_refs WHERE id = 'decision-ref-original'"
            )

        assert conn.execute("SELECT * FROM workflow_decision_refs").fetchall() == original


@pytest.mark.parametrize("conflict", ["id", "operation_id", "effect_identity"])
def test_effect_command_replace_cannot_overwrite_terminal_identity(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, conflict)
        stage_id = f"stage-{conflict}"
        _insert_stage(
            conn,
            workflow_id,
            stage_id,
            "mutate",
            0,
            "engine_mutation",
            state="ready",
        )
        conn.execute(
            """
            INSERT INTO workflow_stage_receipt_requirements(
                workflow_id, stage_id, effect_kind, position
            ) VALUES (?, ?, 'source_import', 0)
            """,
            (workflow_id, stage_id),
        )
        conn.execute(
            "UPDATE workflow_instances SET current_stage_key = 'mutate' WHERE id = ?",
            (workflow_id,),
        )
        conn.execute(
            "UPDATE workflow_instances SET definition_sealed = 1 WHERE id = ?",
            (workflow_id,),
        )
        conn.execute(
            """
            INSERT INTO workflow_effect_commands(
                id, operation_id, workflow_id, stage_id, effect_class,
                effect_kind, effect_key, request_hash, request_json,
                state, created_at
            ) VALUES (
                'command-terminal', 'operation-terminal', ?, ?, 'mutation',
                'source_import', 'source_import', ?, '{"request":"original"}',
                'pending', ?
            )
            """,
            (workflow_id, stage_id, "a" * 64, NOW),
        )
        conn.execute(
            """
            UPDATE workflow_effect_commands
            SET state = 'completed', result_identity = ?, receipt_json = '{}',
                completed_at = ?
            WHERE id = 'command-terminal'
            """,
            ("b" * 64, NOW),
        )
        original = conn.execute(
            "SELECT * FROM workflow_effect_commands WHERE id = 'command-terminal'"
        ).fetchone()
        replacement = {
            "id": "command-replacement",
            "operation_id": "operation-replacement",
            "effect_key": "source_import-replacement",
        }
        if conflict == "id":
            replacement["id"] = "command-terminal"
        elif conflict == "operation_id":
            replacement["operation_id"] = "operation-terminal"
        else:
            replacement["effect_key"] = "source_import"

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_effect_commands(
                    id, operation_id, workflow_id, stage_id, effect_class,
                    effect_kind, effect_key, request_hash, request_json,
                    state, created_at
                ) VALUES (?, ?, ?, ?, 'mutation', 'source_import', ?, ?,
                    '{"request":"changed"}', 'pending', ?)
                """,
                (
                    replacement["id"],
                    replacement["operation_id"],
                    workflow_id,
                    stage_id,
                    replacement["effect_key"],
                    "c" * 64,
                    NOW,
                ),
            )

        assert conn.execute(
            "SELECT * FROM workflow_effect_commands WHERE id = 'command-terminal'"
        ).fetchone() == original
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_effect_commands"
        ).fetchone() == (1,)


def test_effect_command_update_replace_cannot_overwrite_terminal_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, "update-replace")
        stage_id = "stage-update-replace"
        _insert_stage(
            conn,
            workflow_id,
            stage_id,
            "mutate",
            0,
            "engine_mutation",
            state="ready",
        )
        conn.execute(
            """
            INSERT INTO workflow_stage_receipt_requirements(
                workflow_id, stage_id, effect_kind, position
            ) VALUES (?, ?, 'source_import', 0)
            """,
            (workflow_id, stage_id),
        )
        command_sql = """
            INSERT INTO workflow_effect_commands(
                id, operation_id, workflow_id, stage_id, effect_class,
                effect_kind, effect_key, request_hash, request_json,
                state, created_at
            ) VALUES (?, ?, ?, ?, 'mutation', 'source_import', ?, ?, '{}',
                'pending', ?)
        """
        conn.execute(
            command_sql,
            (
                "command-terminal",
                "operation-terminal",
                workflow_id,
                stage_id,
                "terminal",
                "a" * 64,
                NOW,
            ),
        )
        conn.execute(
            """
            UPDATE workflow_effect_commands
            SET state = 'completed', result_identity = ?, receipt_json = '{}',
                completed_at = ?
            WHERE id = 'command-terminal'
            """,
            ("b" * 64, NOW),
        )
        conn.execute(
            command_sql,
            (
                "command-pending",
                "operation-pending",
                workflow_id,
                stage_id,
                "pending",
                "c" * 64,
                NOW,
            ),
        )
        original = conn.execute(
            """
            SELECT id, operation_id, state, result_identity, receipt_json
            FROM workflow_effect_commands ORDER BY id
            """
        ).fetchall()

        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            conn.execute(
                """
                UPDATE OR REPLACE workflow_effect_commands
                SET id = 'command-terminal'
                WHERE id = 'command-pending'
                """
            )

        assert conn.execute(
            """
            SELECT id, operation_id, state, result_identity, receipt_json
            FROM workflow_effect_commands ORDER BY id
            """
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["id", "reference_identity"])
def test_workflow_reference_replace_cannot_overwrite_immutable_content(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"reference-replace-{conflict}")
        stage_id = f"stage-reference-replace-{conflict}"
        _insert_stage(conn, workflow_id, stage_id, "control", 0, "control")
        insert_sql = """
            INSERT OR REPLACE INTO workflow_stage_references(
                id, workflow_id, stage_id, reference_kind, reference_id,
                identity_hash, metadata_json, created_at
            ) VALUES (?, ?, ?, 'engine_source', ?, ?, ?, ?)
        """
        conn.execute(
            insert_sql,
            (
                "reference-original",
                workflow_id,
                stage_id,
                "paper:original",
                "a" * 64,
                '{}',
                NOW,
            ),
        )
        original = conn.execute(
            "SELECT * FROM workflow_stage_references"
        ).fetchall()
        replacement_id = (
            "reference-original" if conflict == "id" else "reference-replacement"
        )
        reference_id = (
            "paper:replacement"
            if conflict == "id"
            else "paper:original"
        )

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(
                insert_sql,
                (
                    replacement_id,
                    workflow_id,
                    stage_id,
                    reference_id,
                    "b" * 64,
                    '{"changed":true}',
                    NOW,
                ),
            )

        assert conn.execute(
            "SELECT * FROM workflow_stage_references"
        ).fetchall() == original


@pytest.mark.parametrize("conflict", ["id", "stage_id"])
def test_workflow_checkpoint_replace_cannot_overwrite_immutable_content(
    tmp_path: Path,
    conflict: str,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, f"checkpoint-replace-{conflict}")
        stage_id = f"stage-checkpoint-replace-{conflict}"
        conn.execute(
            """
            INSERT INTO workflow_stage_instances(
                id, workflow_id, stage_key, position, effect,
                checkpoint_enabled, input_hash, state, attempt, revision,
                started_at, completed_at
            ) VALUES (?, ?, 'checkpoint', 0, 'control', 1, ?, 'completed',
                1, 1, ?, ?)
            """,
            (stage_id, workflow_id, "a" * 64, NOW, NOW),
        )
        insert_sql = """
            INSERT OR REPLACE INTO workflow_checkpoints(
                id, workflow_id, stage_id, workflow_revision, state_hash,
                state_json, references_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn.execute(
            insert_sql,
            (
                "checkpoint-original",
                workflow_id,
                stage_id,
                1,
                "b" * 64,
                '{}',
                "c" * 64,
                NOW,
            ),
        )
        original = conn.execute("SELECT * FROM workflow_checkpoints").fetchall()
        replacement_id = (
            "checkpoint-original" if conflict == "id" else "checkpoint-replacement"
        )

        with pytest.raises(sqlite3.IntegrityError, match="replacement is not supported"):
            conn.execute(
                insert_sql,
                (
                    replacement_id,
                    workflow_id,
                    stage_id,
                    2,
                    "d" * 64,
                    '{"changed":true}',
                    "e" * 64,
                    NOW,
                ),
            )

        assert conn.execute("SELECT * FROM workflow_checkpoints").fetchall() == original


def test_raw_workflow_engine_references_match_shared_validator_boundaries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    ControlStore(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        workflow_id = _insert_workflow(conn, "engine-refs")
        _insert_stage(conn, workflow_id, "stage-engine-refs", "control", 0, "control")
        insert_sql = """
            INSERT INTO workflow_stage_references(
                id, workflow_id, stage_id, reference_kind, reference_id,
                identity_hash, metadata_json, created_at
            ) VALUES (?, ?, 'stage-engine-refs', ?, ?, ?, '{}', ?)
        """
        valid_references = (
            ("engine_source", "paper:A"),
            ("engine_source", f"paper:A{'a' * 466}"),
            ("lineage_node", "idea:0/path_with-safe.chars"),
            ("lineage_node", f"idea:Z{'z' * 466}"),
        )
        for index, (kind, reference_id) in enumerate(valid_references):
            assert validate_engine_ref(
                reference_id,
                namespace="paper" if kind == "engine_source" else "idea",
            ) == reference_id
            conn.execute(
                insert_sql,
                (
                    f"reference-valid-{index}",
                    workflow_id,
                    kind,
                    reference_id,
                    f"{index + 1:064x}",
                    NOW,
                ),
            )

        invalid_references = (
            ("engine_source", "paper:/private/research.db"),
            ("engine_source", "paper:../secret"),
            ("engine_source", "paper:a//b"),
            ("engine_source", "idea:wrong-namespace"),
            ("engine_source", "paper:a\u0000b"),
            ("engine_source", f"paper:A{'a' * 467}"),
            ("lineage_node", "idea:/private/research.db"),
            ("lineage_node", "idea:a..b"),
            ("lineage_node", "idea:a//b"),
            ("lineage_node", "paper:wrong-namespace"),
            ("lineage_node", "idea:\u202esecret"),
            ("lineage_node", f"idea:Z{'z' * 467}"),
        )
        for index, (kind, reference_id) in enumerate(invalid_references):
            with pytest.raises(ValueError, match="engine_ref"):
                validate_engine_ref(
                    reference_id,
                    namespace="paper" if kind == "engine_source" else "idea",
                )
            with pytest.raises(sqlite3.IntegrityError, match="engine reference is invalid"):
                conn.execute(
                    insert_sql,
                    (
                        f"reference-invalid-{index}",
                        workflow_id,
                        kind,
                        reference_id,
                        f"{index + 100:064x}",
                        NOW,
                    ),
                )
