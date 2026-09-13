from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from cortex_platform.backup import (
    _RESTIC_EXCLUDES,
    _run_restic,
    BackupError,
    build_inventory,
    create_staged_snapshot,
    default_asset_roots,
    default_database_specs,
    inspect_database,
    restore_restic_backup,
    restore_staged_snapshot,
    verify_staged_snapshot,
)
from cortex_platform.product.control import ControlStore
from cortex_platform.product.control import schema as control_schema
from cortex_platform.product.control.schema import (
    RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,
)


#: A recorded pre-gen-8 control database, staged by whoever runs this gate --
#: real operator state, so it is never in the checkout. It used to be an
#: absolute path on one machine, which made the assertion a permanent skip
#: anywhere else; the variable makes the requirement nameable instead.
_PRE_GEN7_VARIABLE = "CORTEX_TEST_PRE_GEN7_CONTROL_DB"


def _create_schema_12_control_db(path: Path) -> None:
    """Build a control database frozen at migration 12.

    ``ControlStore.initialize()`` always lands on the newest schema, so the
    state the operator actually runs before the gen 8 upgrade is unreachable
    through it. Replaying migrations 1..12 reproduces that state exactly.
    """

    _create_control_db_through(path, 12)


def _create_control_db_through(path: Path, last_version: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "    version INTEGER PRIMARY KEY,"
            "    applied_at TEXT NOT NULL"
            ")"
        )
        # Walked off `migration_scripts()` rather than `_MIGRATION_{n}`: the
        # scripts are named after what they create, not after the number they
        # were given, and a renumber moves the number only.
        for version, script in control_schema.migration_scripts():
            if version > last_version:
                break
            control_schema._execute_script_in_transaction(conn, script)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-01-01T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def _create_schema_13_control_db(path: Path) -> None:
    """Build a control database frozen at migration 13 -- what gen 8 ships."""

    _create_control_db_through(path, 13)


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def _write_transport_key_companion(control: Path) -> None:
    companion = control.with_name(f".{control.name}.transport.key")
    companion.write_bytes(b"0" * 32)
    companion.chmod(0o600)


def _create_research_db(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE papers (
            paper_dir TEXT PRIMARY KEY,
            title TEXT,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, paper_dir TEXT, body TEXT);
        CREATE TABLE idea_seeds (
            idea_id TEXT PRIMARY KEY,
            seed_text TEXT,
            status TEXT NOT NULL DEFAULT 'incubating',
            md_path TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE exploration_rounds (
            id INTEGER PRIMARY KEY,
            exploration_id TEXT NOT NULL,
            round_n INTEGER NOT NULL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE conversation_sessions (
            session_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            n_turns_atomized INTEGER NOT NULL,
            n_atoms_extracted INTEGER NOT NULL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO papers(paper_dir, title) VALUES ('paper-a', 'A paper');
        INSERT INTO chunks(paper_dir, body) VALUES ('paper-a', 'evidence');
        INSERT INTO idea_seeds(idea_id, seed_text) VALUES ('idea-a', 'test idea');
        INSERT INTO exploration_rounds(exploration_id, round_n)
        VALUES ('idea-a', 1);
        INSERT INTO conversation_sessions(
            session_id, platform, n_turns_atomized, n_atoms_extracted
        ) VALUES ('session-a', 'telegram', 2, 1);
        """
    )
    conn.commit()
    return conn


def test_optional_asset_roots_are_absent_until_something_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No guessed corpus or checkout path, in either direction.

    The four installation roles this product creates are always listed; the
    source checkout and the two corpus directories are listed only when an
    argument or an environment variable says where they are. An inventory that
    invents them reports having checked a directory nobody has.
    """

    for variable in ("CORTEX_HOME", "CORTEX_READINGS_ROOT", "CORTEX_RESEARCH_ROOT"):
        monkeypatch.delenv(variable, raising=False)

    names = [root.name for root in default_asset_roots(home=tmp_path / "home")]

    assert names == [
        "cortex_config",
        "cortex_state",
        "cortex_product_data",
        "hermes_home",
    ]

    monkeypatch.setenv("CORTEX_READINGS_ROOT", str(tmp_path / "from-environment"))
    named = {
        root.name: root.path
        for root in default_asset_roots(
            home=tmp_path / "home",
            cortex_repo=tmp_path / "checkout",
            research_root=tmp_path / "corpus",
        )
    }

    assert named["cortex_repo"] == (tmp_path / "checkout").resolve()
    assert named["agent_research"] == (tmp_path / "corpus").resolve()
    assert named["agent_readings"] == (tmp_path / "from-environment").resolve()


def test_inventory_distinguishes_canonical_db_from_empty_lookalike(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    canonical = home / ".local/state/cortex/research/research.db"
    _create_research_db(canonical).close()
    lookalike = home / ".local/state/cortex/research/data/research.db"
    lookalike.parent.mkdir(parents=True)
    lookalike.touch()

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")

    research = next(row for row in inventory["databases"] if row["name"] == "research")
    assert research["canonical"] is True
    assert research["status"] == "ok"
    assert research["missing_required_tables"] == []
    candidate = next(
        row
        for row in inventory["database_candidates"]
        if row["path"] == str(lookalike.resolve())
    )
    assert candidate["canonical"] is False
    assert candidate["status"] == "empty"


def test_control_store_protects_the_capture_inbox(tmp_path: Path) -> None:
    """A capture is durable operator intent that has not been imported yet.

    Losing it silently loses the only record that the operator asked for
    something, so the table joins the protected set rather than relying on
    the whole-file copy being complete.
    """

    spec = next(
        item
        for item in default_database_specs(home=tmp_path)
        if item.name == "cortex_control"
    )
    assert "captures" in spec.required_tables
    assert dict(spec.table_since_schema_version)["captures"] == 13


def test_snapshot_accepts_a_control_store_still_on_schema_12(
    tmp_path: Path,
) -> None:
    """The pre-upgrade state must stay snapshottable.

    Migration 13 introduces ``captures``; a control database that has not
    reached that version yet legitimately lacks the table. Requiring it there
    would mark the operator's live database invalid and refuse both the
    nightly backup and the pre-upgrade snapshot that is the only reversal
    path for the migration.
    """

    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    _create_schema_12_control_db(control)
    _write_transport_key_companion(control)

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")
    row = next(
        item for item in inventory["databases"] if item["name"] == "cortex_control"
    )
    assert "captures" not in row["tables"]
    assert row["missing_required_tables"] == []
    assert row["status"] == "ok"

    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="schema-12",
    )
    assert verify_staged_snapshot(snapshot)["ok"] is True


def test_snapshot_accepts_the_recorded_pre_gen7_control_snapshot(
    tmp_path: Path,
) -> None:
    """The same guarantee against the operator's recorded schema-12 database."""

    location = os.environ.get(_PRE_GEN7_VARIABLE, "")
    snapshot_source = Path(location) if location else None
    if snapshot_source is None or not snapshot_source.exists():
        pytest.skip(f"a pre-gen-8 control database in ${_PRE_GEN7_VARIABLE} is required")

    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    control.parent.mkdir(mode=0o700, parents=True)
    shutil.copyfile(snapshot_source, control)
    _write_transport_key_companion(control)

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")
    row = next(
        item for item in inventory["databases"] if item["name"] == "cortex_control"
    )
    assert row["missing_required_tables"] == []
    assert row["status"] == "ok"

    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="pre-gen7",
    )
    assert verify_staged_snapshot(snapshot)["ok"] is True


def test_control_store_protects_the_transport_activation_gate(
    tmp_path: Path,
) -> None:
    """The gate is the only record that the operator authorized a send.

    Losing it silently loses the audit answer to "who let the product hold
    the research token", so it joins the protected set on the same terms the
    capture inbox did.
    """

    spec = next(
        item
        for item in default_database_specs(home=tmp_path)
        if item.name == "cortex_control"
    )
    assert "transport_activation_decisions" in spec.required_tables
    gates = dict(spec.table_since_schema_version)
    assert (
        gates["transport_activation_decisions"]
        == control_schema.TRANSPORT_ACTIVATION_MIGRATION
    )
    assert gates["captures"] == control_schema.CAPTURES_MIGRATION


def test_every_gated_table_arrives_at_exactly_the_version_its_gate_names(
    tmp_path: Path,
) -> None:
    """The gate and the migration are the same number, proved against each other.

    A gate that names a version the migration does not create the table at
    makes `inspect_database` report `missing_required_tables` on a healthy
    database: `status` becomes `invalid`, `create_backup` raises, and
    `cortex-dist record-proof` cannot produce the R0-C proof the stateful
    upgrade needs. Nothing here is a literal, so a renumber that moves one
    side without the other fails here instead of at the ceremony.
    """

    spec = next(
        item
        for item in default_database_specs(home=tmp_path)
        if item.name == "cortex_control"
    )
    assert dict(spec.table_since_schema_version) == {
        "captures": control_schema.CAPTURES_MIGRATION,
        "runtime_release_approvals": (
            control_schema.RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION
        ),
        "transport_activation_decisions": (
            control_schema.TRANSPORT_ACTIVATION_MIGRATION
        ),
        "research_schedules": control_schema.RESEARCH_SCHEDULES_MIGRATION,
        "research_items": control_schema.RESEARCH_ITEMS_MIGRATION,
        "research_document_versions": control_schema.RESEARCH_ITEMS_MIGRATION,
        "research_thread_items": control_schema.RESEARCH_ITEMS_MIGRATION,
    }
    for table, version in spec.table_since_schema_version:
        before = tmp_path / f"before-{table}/control.db"
        _create_control_db_through(before, version - 1)
        assert table not in _table_names(before)
        after = tmp_path / f"after-{table}/control.db"
        _create_control_db_through(after, version)
        assert table in _table_names(after)


def test_snapshot_accepts_a_control_store_still_on_schema_13(
    tmp_path: Path,
) -> None:
    """The state gen 8 ships is snapshottable before either gate migration runs.

    Gen 8 ships schema 13; release approvals arrive at 14 and the transport
    activation gate at 15. Requiring either table at schema 13 would refuse the
    pre-upgrade snapshot that is the only reversal path for those migrations,
    in exactly the window that needs it -- the mistake `4fa5fae` fixed for
    `captures`.
    """

    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    _create_schema_13_control_db(control)
    _write_transport_key_companion(control)

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")
    row = next(
        item for item in inventory["databases"] if item["name"] == "cortex_control"
    )
    assert "captures" in row["tables"]
    assert "runtime_release_approvals" not in row["tables"]
    assert "transport_activation_decisions" not in row["tables"]
    assert row["missing_required_tables"] == []
    assert row["status"] == "ok"

    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="schema-13",
    )
    assert verify_staged_snapshot(snapshot)["ok"] is True


def test_snapshot_refuses_a_migrated_control_store_that_lost_the_gate(
    tmp_path: Path,
) -> None:
    """Once the gate migration has run the table is required, not optional."""

    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    control.parent.mkdir(mode=0o700, parents=True)
    ControlStore(control).initialize()
    _write_transport_key_companion(control)

    conn = sqlite3.connect(control)
    try:
        conn.execute("DROP TABLE transport_activation_decisions")
        conn.commit()
    finally:
        conn.close()

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")
    row = next(
        item for item in inventory["databases"] if item["name"] == "cortex_control"
    )
    assert row["missing_required_tables"] == ["transport_activation_decisions"]
    assert row["status"] == "invalid"

    with pytest.raises(BackupError, match="not healthy"):
        create_staged_snapshot(
            tmp_path / "snapshots",
            home=home,
            cortex_repo=tmp_path / "repo",
            snapshot_id="gate-damaged",
        )


def test_snapshot_refuses_a_schema_13_control_store_that_lost_captures(
    tmp_path: Path,
) -> None:
    """Once the migration has run the table is required, not optional."""

    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    control.parent.mkdir(mode=0o700, parents=True)
    ControlStore(control).initialize()
    _write_transport_key_companion(control)

    conn = sqlite3.connect(control)
    try:
        conn.execute("DROP TABLE captures")
        conn.commit()
    finally:
        conn.close()

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")
    row = next(
        item for item in inventory["databases"] if item["name"] == "cortex_control"
    )
    assert row["missing_required_tables"] == ["captures"]
    assert row["status"] == "invalid"

    with pytest.raises(BackupError, match="not healthy"):
        create_staged_snapshot(
            tmp_path / "snapshots",
            home=home,
            cortex_repo=tmp_path / "repo",
            snapshot_id="schema-13-damaged",
        )


def test_inventory_and_snapshot_include_native_product_control_store(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    control.parent.mkdir(mode=0o700, parents=True)
    store = ControlStore(control)
    store.initialize()
    workspace = store.create_workspace(
        title="Research",
        actor_id="local",
        idempotency_key="workspace-command-0001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Echo",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key="thread-command-000001",
    ).value
    binding = store.bind_transport(
        transport="telegram",
        external_scope="bot:123:topic:456",
        thread_id=thread["id"],
        actor_id="adapter",
        idempotency_key="binding-command-0001",
    ).value

    inventory = build_inventory(home=home, cortex_repo=tmp_path / "repo")
    row = next(
        item for item in inventory["databases"] if item["name"] == "cortex_control"
    )
    assert row["status"] == "ok"
    assert row["missing_required_tables"] == []

    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="control-store",
    )
    verification = verify_staged_snapshot(snapshot)
    assert verification["ok"] is True
    assert verification["database_count"] == 1
    assert verification["companion_count"] == 1

    restore_target = tmp_path / "restore-control"
    report = restore_staged_snapshot(snapshot, restore_target)
    assert report["companion_count"] == 1
    restored = ControlStore(restore_target / "databases/cortex_control.db")
    restored.initialize()
    assert restored.resolve_transport(
        transport="telegram", external_scope="bot:123:topic:456"
    ) == binding

    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["companion_files"] = []
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="transport-key companion"):
        verify_staged_snapshot(snapshot)


def test_inventory_verifies_research_resource_files(tmp_path: Path) -> None:
    """The corpus roots are arguments now, so this test states where they are.

    They used to default to one machine's layout under `$HOME`, which let this
    test build its corpus at that default and never name it. With the guess
    gone the roots are passed explicitly -- which is also what the reference
    pass needs to be able to say a file is INSIDE its root.
    """

    home = tmp_path / "home"
    database = home / ".local/state/cortex/research/research.db"
    conn = _create_research_db(database)
    readings_root = tmp_path / "corpus/readings"
    research_root = tmp_path / "corpus/research"
    paper_path = readings_root / "papers/paper-a"
    paper_path.mkdir(parents=True)
    (paper_path / "notes.md").write_text("paper note")
    idea_path = research_root / "ideas/idea-a.md"
    idea_path.parent.mkdir(parents=True)
    idea_path.write_text("idea artifact")
    conn.execute(
        "UPDATE idea_seeds SET md_path = ? WHERE idea_id = 'idea-a'",
        (str(idea_path),),
    )
    conn.commit()
    conn.close()

    inventory = build_inventory(
        home=home,
        cortex_repo=tmp_path / "repo",
        readings_root=readings_root,
        research_root=research_root,
    )

    references = inventory["resource_references"]
    assert references["status"] == "ok"
    assert references["papers"][0]["paper_dir"] == "paper-a"
    assert references["ideas"][0]["idea_id"] == "idea-a"
    assert references["explorations"][0]["max_round"] == 1
    assert references["conversation_sessions"][0]["session_id"] == "session-a"
    resource_types = {
        sample.get("resource_type") for sample in inventory["sample_files"]
    }
    assert {"paper", "idea"} <= resource_types


def test_snapshot_uses_sqlite_backup_and_restores_wal_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    canonical = home / ".local/state/cortex/research/research.db"
    writer = _create_research_db(canonical, wal=True)
    writer.execute(
        "INSERT INTO papers(paper_dir, title) "
        "VALUES ('paper-b', 'Committed while writer remains open')"
    )
    writer.commit()

    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="test-snapshot",
    )
    writer.close()

    verification = verify_staged_snapshot(snapshot)
    assert verification["ok"] is True
    assert verification["database_count"] == 1

    restore_target = tmp_path / "restore"
    report = restore_staged_snapshot(snapshot, restore_target)
    assert report["ok"] is True
    restored = sqlite3.connect(restore_target / "databases/research.db")
    try:
        titles = {
            row[0] for row in restored.execute("SELECT title FROM papers ORDER BY title")
        }
    finally:
        restored.close()
    assert titles == {"A paper", "Committed while writer remains open"}


def test_verify_rejects_tampered_snapshot(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _create_research_db(
        home / ".local/state/cortex/research/research.db"
    ).close()
    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="tamper-test",
    )
    manifest = json.loads((snapshot / "manifest.json").read_text())
    database_path = snapshot / manifest["database_snapshots"][0][
        "snapshot_relative_path"
    ]
    with database_path.open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(BackupError, match="Checksum mismatch"):
        verify_staged_snapshot(snapshot)


def test_inspect_database_rejects_missing_required_tables(tmp_path: Path) -> None:
    database = tmp_path / "wrong.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.close()

    inspection = inspect_database(database, required_tables=("papers",))

    assert inspection["status"] == "invalid"
    assert inspection["missing_required_tables"] == ["papers"]


def test_snapshot_fails_closed_for_invalid_canonical_database(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database = home / ".local/state/cortex/research/research.db"
    database.parent.mkdir(parents=True)
    database.write_text("not a database")

    with pytest.raises(BackupError, match="Canonical database is not healthy"):
        create_staged_snapshot(
            tmp_path / "snapshots",
            home=home,
            cortex_repo=tmp_path / "repo",
            snapshot_id="invalid-db",
        )

    assert not (tmp_path / "snapshots/invalid-db").exists()


def test_restic_rules_do_not_exclude_explicit_logical_snapshot() -> None:
    assert "**/backup-staging/**" not in _RESTIC_EXCLUDES
    assert "*.sqlite3.snapshot" not in _RESTIC_EXCLUDES
    assert "*.db" in _RESTIC_EXCLUDES


def test_restic_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def timeout_command(
        args: list[str], *, cwd: Path | None = None, timeout: float | None = None
    ) -> None:
        assert cwd is None
        assert timeout == 15
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setenv("CORTEX_RESTIC_TIMEOUT_SECONDS", "15")
    monkeypatch.setattr("cortex_platform.backup._run_command", timeout_command)

    with pytest.raises(BackupError, match="timed out after 15 seconds"):
        _run_restic(tmp_path / "repo", "true", "backup", "/tmp/source")


def test_restore_supports_grouped_snapshot_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "repository": str(tmp_path / "repo"),
                "restic_snapshot_id": "snapshot-one",
                "restic_snapshot_ids": ["snapshot-one", "snapshot-two"],
                "cortex_snapshot_id": "cortex-test",
                "staged_snapshot_path": "/staging/cortex-test",
                "sample_files": [],
            }
        )
    )
    restored_snapshot_ids: list[str] = []

    def record_restic(
        repository: Path, password_command: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        assert repository == tmp_path / "repo"
        assert password_command == "password-command"
        restored_snapshot_ids.append(args[1])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("cortex_platform.backup._run_restic", record_restic)
    monkeypatch.setattr(
        "cortex_platform.backup.restore_staged_snapshot",
        lambda staged, target: {"ok": True, "database_count": 0},
    )

    report = restore_restic_backup(
        receipt, "password-command", tmp_path / "restore"
    )

    assert restored_snapshot_ids == ["snapshot-one", "snapshot-two"]
    assert report["restic_snapshot_id"] == "snapshot-one"
    assert report["restic_snapshot_ids"] == ["snapshot-one", "snapshot-two"]


def _control_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    control = home / "Library/Application Support/Cortex/Data/control.db"
    control.parent.mkdir(mode=0o700, parents=True)
    return home, control


def test_release_approvals_are_protected_and_required_from_their_own_version(
    tmp_path: Path,
) -> None:
    """⟦S3.4/D6⟧ Protected always, required only once the schema can have it.

    The commit that taught this gate its lesson (`4fa5fae`) is the reason the
    membership and the requirement are two different things: requiring a table a
    migration has not created yet marks every older database invalid and kills
    the snapshot that is the only reversal path for the migration itself.
    """

    home, control = _control_home(tmp_path)
    store = ControlStore(control)
    store.initialize()
    spec = next(
        item
        for item in default_database_specs(home=home)
        if item.name == "cortex_control"
    )
    assert "runtime_release_approvals" in spec.required_tables
    # Membership, not equality: the gate tuple is a union across slices, and
    # pinning its whole shape here would fail every time a sibling table lands.
    assert (
        "runtime_release_approvals",
        RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,
    ) in spec.table_since_schema_version
    inspection = inspect_database(
        control,
        required_tables=spec.required_tables,
        table_since_schema_version=spec.table_since_schema_version,
    )
    assert inspection["schema_version"] >= RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION
    assert "runtime_release_approvals" in inspection["required_tables"]
    assert inspection["missing_required_tables"] == []


def test_an_older_schema_database_still_snapshots(tmp_path: Path) -> None:
    """The regression `4fa5fae` fixed, re-run against this slice's table.

    A control store that has not reached this migration legitimately has no
    approvals table, and must stay healthy — otherwise the pre-upgrade snapshot
    refuses in exactly the window that needs it.

    "Has not reached this migration" is the whole claim, so the fixture is a
    real schema-13 store: every table above 13 has to be physically absent, or
    the gates for the sibling slices are what keeps this green.
    """

    home, control = _control_home(tmp_path)
    # Replayed, not rewound. Deleting every `schema_migrations` row from this
    # migration up while dropping only `runtime_release_approvals` left
    # `transport_activation_decisions` (15) and `research_schedules` (16) on
    # disk under a claimed schema_version of 13 -- a state `apply_migrations`
    # can neither produce nor repair (it dies on "table
    # transport_activation_decisions already exists"), and one where the two
    # sibling tables were absent from `required_tables` because of their own
    # gates rather than because of the property under test.
    _create_control_db_through(control, control_schema.CAPTURES_MIGRATION)
    _write_transport_key_companion(control)
    tables = _table_names(control)
    assert "runtime_release_approvals" not in tables
    assert "transport_activation_decisions" not in tables
    assert "research_schedules" not in tables
    spec = next(
        item
        for item in default_database_specs(home=home)
        if item.name == "cortex_control"
    )
    inspection = inspect_database(
        control,
        required_tables=spec.required_tables,
        table_since_schema_version=spec.table_since_schema_version,
    )
    assert inspection["schema_version"] < RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION
    assert "runtime_release_approvals" not in inspection["required_tables"]
    assert inspection["missing_required_tables"] == []
    assert inspection["status"] == "ok"

    row = next(
        item
        for item in build_inventory(home=home, cortex_repo=tmp_path / "repo")[
            "databases"
        ]
        if item["name"] == "cortex_control"
    )
    assert row["status"] == "ok"
    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="older-schema",
    )
    assert verify_staged_snapshot(snapshot)["ok"] is True


def test_the_approvals_survive_a_snapshot_and_restore(tmp_path: Path) -> None:
    home, control = _control_home(tmp_path)
    store = ControlStore(control)
    store.initialize()
    store.approve_runtime_release(
        release_id="hermes-0.15.0",
        manifest_sha256="c" * 64,
        actor_id="operator",
        idempotency_key="approve-command-0007",
    )
    snapshot = create_staged_snapshot(
        tmp_path / "snapshots",
        home=home,
        cortex_repo=tmp_path / "repo",
        snapshot_id="approvals",
    )
    restore_target = tmp_path / "restore-approvals"
    restore_staged_snapshot(snapshot, restore_target)
    restored = ControlStore(restore_target / "databases/cortex_control.db")
    restored.initialize()
    assert restored.runtime_release_approved("hermes-0.15.0", "c" * 64) is True


def test_the_default_inventory_names_only_the_databases_this_product_owns(
    tmp_path: Path,
) -> None:
    """An inventory row is a promise that something creates the file.

    The list used to name the investment cache, the eval store, three Hermes
    gateway/board databases and the shared user-memory database. None of those
    subsystems is shipped, so every one of those rows made a backup report
    describe an installation that does not exist -- and a missing file there is
    indistinguishable from a lost one.
    """

    names = [spec.name for spec in default_database_specs(home=tmp_path)]
    assert names == ["cortex_control", "research"]
