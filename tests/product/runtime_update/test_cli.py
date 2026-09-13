from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.cli import main
from cortex_platform.product.control import schema as control_schema
from cortex_platform.product.runtime_update.models import (
    canonical_json,
    digest_document,
)


def _environment(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


def test_runtime_status_freeze_thaw_are_sanitized(
    tmp_path: Path, capsys
) -> None:
    environment = _environment(tmp_path / "home")
    common = ["--state-dir", str(tmp_path / "state")]

    assert main(["runtime", *common, "status"], environ=environment, platform="darwin") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["frozen"] is False
    assert main(["runtime", *common, "freeze"], environ=environment, platform="darwin") == 0
    assert capsys.readouterr().out.strip() == "frozen"
    assert main(["runtime", *common, "status"], environ=environment, platform="darwin") == 0
    assert json.loads(capsys.readouterr().out)["frozen"] is True
    assert main(["runtime", *common, "thaw"], environ=environment, platform="darwin") == 0
    assert capsys.readouterr().out.strip() == "thawed"


def test_runtime_check_requires_exact_out_of_band_catalog_pin(
    tmp_path: Path, capsys
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    result = main(
        [
            "runtime",
            "--state-dir",
            str(tmp_path / "state"),
            "check",
            "--catalog",
            str(catalog),
            "--trusted-catalog-sha256",
            "0" * 64,
        ],
        environ=_environment(tmp_path / "home"),
        platform="darwin",
    )
    assert result == 1
    assert "error:" in capsys.readouterr().err


def test_runtime_offline_import_stage_activate_status_round_trip(
    tmp_path: Path, capsys, release_factory, vendored_worker_runtime
) -> None:
    """The CLI round trip stages a real interpreter, because `stage` now does.

    Deliberately not faked. `cortex runtime stage` builds its own service, so
    this is the one path where the wheel-shipped staging kernel runs unmocked —
    and a round trip that expanded a stand-in archive would prove exactly the
    thing this project has been burned by eight times: a green suite over code
    that has never executed.
    """

    archive, _pin = vendored_worker_runtime
    artifact, manifest, catalog, attestation = release_factory(
        runtime_archive=archive.read_bytes()
    )
    files = {}
    for name, document in {
        "catalog": catalog,
        "manifest": manifest,
        "attestation": attestation,
    }.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_json(document))
        files[name] = path
    state = tmp_path / "state"
    common = ["runtime", "--state-dir", str(state)]
    environment = _environment(tmp_path / "home")

    assert main(
        [
            *common,
            "import",
            "--catalog",
            str(files["catalog"]),
            "--manifest",
            str(files["manifest"]),
            "--attestation",
            str(files["attestation"]),
            "--artifact",
            str(artifact),
            "--trusted-catalog-sha256",
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest(),
            "--trusted-attestation-sha256",
            hashlib.sha256(canonical_json(attestation)).hexdigest(),
        ],
        environ=environment,
        platform="darwin",
    ) == 0
    assert capsys.readouterr().out.strip() == "imported"
    assert main(
        [*common, "stage", "--release-id", manifest["release_id"]],
        environ=environment,
        platform="darwin",
    ) == 0
    capsys.readouterr()
    # ⟦MRG-D1⟧ `import` is control-free by decision, and so is `stage`: neither
    # runs code, neither consults D6, and neither may open — and therefore
    # migrate — `control.db`. The first door that does is the refusal below.
    control_db = (
        tmp_path / "home" / "Library" / "Application Support" / "Cortex" / "Data"
        / "control.db"
    )
    assert not control_db.exists()
    # ⟦S3.4/D6⟧ Activation refuses first. The refusal test comes before the
    # accept test and shares none of its state: the approval that unblocks it
    # does not exist yet.
    assert main(
        [*common, "activate", "--release-id", manifest["release_id"]],
        environ=environment,
        platform="darwin",
    ) == 1
    assert "release_not_approved" in capsys.readouterr().err
    # The other half of the same decision: a door that consults D6 does open the
    # store, so the deferral is a deferral and not a silent loss of authority.
    assert control_db.exists()
    assert main(
        [
            *common,
            "approve",
            "--release-id",
            manifest["release_id"],
            "--manifest-sha256",
            digest_document(manifest),
        ],
        environ=environment,
        platform="darwin",
    ) == 0
    assert capsys.readouterr().out.strip().startswith("approved:")
    assert main(
        [*common, "activate", "--release-id", manifest["release_id"]],
        environ=environment,
        platform="darwin",
    ) == 0
    capsys.readouterr()
    assert main([*common, "status"], environ=environment, platform="darwin") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active"]["release_id"] == manifest["release_id"]

    # The interpreter the release carried is expanded, sealed, and pinned under
    # a root named by the archive digest — beside the slots, never inside one.
    digest = manifest["worker_runtime"]["archive_sha256"]
    root = state / "runtime-update" / "interpreters" / digest
    interpreter = root / manifest["worker_runtime"]["interpreter_relative"]
    assert interpreter.is_file()
    pin = json.loads((root.parent / f"{digest}.pin.json").read_text())
    assert pin == {
        "archive_sha256": digest,
        "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
    }


def test_assert_transport_capability_defaults_to_the_cutover_record_path(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The default is a contract with `deployment/cutover/lib.sh`, not a taste.

    The preflight refuses to proceed without a record at
    `$CUTOVER_RECORD_DIR/capability-assertion.json`, whose default is derived
    from `$HOME`. Both sides are overridable -- `--output` here, the two
    variables there -- so an operator who moved it passes the same path twice.
    """

    from cortex_platform.product.runtime_update import cli as runtime_cli

    home = tmp_path / "home"
    seen: dict[str, object] = {}

    def stub(service, *, output):
        seen["output"] = output
        return 0, {"result": "ok", "capability": "cortex.telegram.transport/1"}

    monkeypatch.setattr(runtime_cli, "assert_active_transport_capability", stub)

    result = main(
        ["runtime", "--state-dir", str(tmp_path / "state"), "assert-transport-capability"],
        environ=_environment(home),
        platform="darwin",
    )

    assert result == 0
    assert seen["output"] == (
        home / ".local/state/cortex/cutover/capability-assertion.json"
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["capability"] == "cortex.telegram.transport/1"
    assert printed["record"] == str(seen["output"])


def test_assert_transport_capability_propagates_a_refusal(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from cortex_platform.product.runtime_update import cli as runtime_cli

    record = tmp_path / "capability-assertion.json"
    monkeypatch.setattr(
        runtime_cli,
        "assert_active_transport_capability",
        lambda service, *, output: (
            1,
            {"result": "unavailable", "capability": None, "reason": "WorkerProtocolError"},
        ),
    )

    result = main(
        [
            "runtime",
            "--state-dir",
            str(tmp_path / "state"),
            "assert-transport-capability",
            "--output",
            str(record),
        ],
        environ=_environment(tmp_path / "home"),
        platform="darwin",
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out)["result"] == "unavailable"


def test_assert_transport_capability_refuses_without_an_active_runtime(
    tmp_path: Path, capsys
) -> None:
    """A typed refusal, not a traceback: the P6 preflight reads the exit code."""

    result = main(
        [
            "runtime",
            "--state-dir",
            str(tmp_path / "state"),
            "assert-transport-capability",
            "--output",
            str(tmp_path / "record.json"),
        ],
        environ=_environment(tmp_path / "home"),
        platform="darwin",
    )

    assert result == 1
    assert "active runtime is unavailable" in capsys.readouterr().err
    # DIV-4: written, not skipped. A failure that leaves no record leaves the
    # PREVIOUS release's success on disk, and that file is what the P6
    # preflight reads as the current answer.
    written = json.loads((tmp_path / "record.json").read_text())
    assert written["result"] == "unavailable"
    assert written["capability"] is None



def test_runtime_status_reports_what_the_daemon_actually_bound(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """⟦P5.4⟧ Two questions, and after `activate` they disagree until a restart.

    The updater's `active` pointer is what is on disk; `managed_worker` is what
    the RUNNING process bound at its own start. The runbook's "restart before
    the window" step exists because of that gap, and this is where an operator
    can see it.
    """

    import cortex_platform.product.runtime_update.cli as module

    environment = _environment(tmp_path / "home")
    common = ["--state-dir", str(tmp_path / "state")]

    assert main(["runtime", *common, "status"], environ=environment, platform="darwin") == 0
    assert json.loads(capsys.readouterr().out)["managed_worker"] == {
        "state": "unknown",
        "reason": "daemon_not_running",
    }

    monkeypatch.setattr(
        module,
        "daemon_health",
        lambda paths: {
            "managed_worker": {
                "state": "bound",
                "reason": None,
                "release_id": "hermes-0.15.0-gen9",
                "slot_digest": "b" * 64,
                "launched": False,
            }
        },
    )
    assert main(["runtime", *common, "status"], environ=environment, platform="darwin") == 0
    reported = json.loads(capsys.readouterr().out)["managed_worker"]
    assert reported["state"] == "bound"
    assert reported["release_id"] == "hermes-0.15.0-gen9"


def _control_db_at_captures(database: Path) -> None:
    """A control store frozen at the state gen 8 ships, on disk and no further."""

    database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(database)
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
            if version > control_schema.CAPTURES_MIGRATION:
                break
            control_schema._execute_script_in_transaction(conn, script)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-09-02T12:00:00.000000Z"),
            )
        conn.commit()
    finally:
        conn.close()
    # `ControlStore` refuses a database file it cannot prove is private, and
    # `sqlite3.connect` creates one with the process umask. The companion
    # binding key is part of an existing store's on-disk state too, so a
    # replayed one has to carry it or the store refuses to open at all — and a
    # store that refuses to open would make this test pass for the wrong
    # reason.
    database.chmod(0o600)
    companion = database.with_name(f".{database.name}.transport.key")
    companion.write_bytes(b"0" * 32)
    companion.chmod(0o600)


def _schema_version(database: Path) -> int:
    conn = sqlite3.connect(database)
    try:
        return int(
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "command",
    ["status", "check", "freeze", "thaw", "assert-transport-capability"],
)
def test_status_leaves_a_schema_13_control_database_unmigrated(
    tmp_path: Path, capsys, command: str
) -> None:
    """⟦MRG-D1⟧ The read-only half of the surface never migrates control.db.

    `deployment/cutover/preflight.sh` runs two of these as preconditions — gate
    (c) is `runtime status`, gate (d)'s producer is
    `assert-transport-capability` — inside a script whose header says every
    check is read-only, and BEFORE its own WAL, backup-proof-coverage and
    snapshot gates. A command surface that opens `ControlStore` to build an
    approvals gate it never consults applies migrations there instead, which is
    the same discipline `deployment/cutover/rollback.sh` states in prose and
    `test_transport_cli.py` pins for `transport status`.

    The state gen 8 ships, read by the gen 9 command surface: a database frozen
    at `CAPTURES_MIGRATION`, which every command here must leave alone.
    """

    database = tmp_path / "data" / "control.db"
    _control_db_at_captures(database)
    before = _schema_version(database)
    assert before == control_schema.CAPTURES_MIGRATION

    roots = [
        "--state-dir",
        str(tmp_path / "state"),
        "--data-dir",
        str(tmp_path / "data"),
    ]
    if command == "check":
        catalog = tmp_path / "catalog.json"
        catalog.write_text("{}", encoding="utf-8")
        tail = ["--catalog", str(catalog), "--trusted-catalog-sha256", "0" * 64]
    elif command == "assert-transport-capability":
        # The one runtime subparser declared with `parents=[common]`, so its own
        # copy of `--data-dir` defaults back to None and overrides whatever
        # preceded the subcommand. Repeated here, or this case would read a
        # database it never froze and pass for free.
        tail = [*roots, "--output", str(tmp_path / "record.json")]
    else:
        tail = []

    main(
        ["runtime", *roots, command, *tail],
        environ=_environment(tmp_path / "home"),
        platform="darwin",
    )
    capsys.readouterr()

    assert _schema_version(database) == before
    # No store was opened at all, not merely no migration applied: `initialize()`
    # is what leaves this behind.
    assert not (tmp_path / "data" / ".control.db.initialize.lock").exists()


def _dispatch_state(tmp_path: Path, capsys) -> dict:
    """`runtime status`'s answer about the migration-12 gate."""

    assert (
        main(
            ["runtime", "--state-dir", str(tmp_path / "state"), "status"],
            environ=_environment(tmp_path / "home"),
            platform="darwin",
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)["dispatch"]


def test_dispatch_gate_is_a_second_decision_the_operator_can_now_make(
    tmp_path: Path, capsys
) -> None:
    """⟦P5.4c⟧ The window is not the gate; two decisions, two commands.

    `cortex transport enable-window` opens migration 15. Nothing before this
    slice could open migration 12, so the run-turn path was unreachable by any
    supported command and the window procedure had no step that could name one.
    """

    common = ["--state-dir", str(tmp_path / "state")]
    environment = _environment(tmp_path / "home")

    assert _dispatch_state(tmp_path, capsys) == {
        "enabled": False,
        "mode": None,
        "expires_at": None,
    }

    assert (
        main(
            ["runtime", *common, "enable-dispatch", "--seconds", "1800", "--actor-id", "op"],
            environ=environment,
            platform="darwin",
        )
        == 0
    )
    opened = json.loads(capsys.readouterr().out)
    assert opened["decision"] == "enable"
    assert opened["mode"] == "window"
    assert opened["state"]["enabled"] is True
    assert opened["expires_at"] is not None

    assert _dispatch_state(tmp_path, capsys)["enabled"] is True

    assert (
        main(
            ["runtime", *common, "disable-dispatch", "--actor-id", "op"],
            environ=environment,
            platform="darwin",
        )
        == 0
    )
    closed = json.loads(capsys.readouterr().out)
    assert closed["decision"] == "disable"
    assert closed["state"]["enabled"] is False

    assert _dispatch_state(tmp_path, capsys)["enabled"] is False


def test_enable_dispatch_requires_exactly_one_of_seconds_or_permanent(
    tmp_path: Path, capsys
) -> None:
    """A grant of authority is never made by a default."""

    common = ["--state-dir", str(tmp_path / "state")]
    environment = _environment(tmp_path / "home")

    assert (
        main(
            ["runtime", *common, "enable-dispatch", "--actor-id", "op"],
            environ=environment,
            platform="darwin",
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_request"

    assert (
        main(
            [
                "runtime",
                *common,
                "enable-dispatch",
                "--seconds",
                "60",
                "--permanent",
                "--actor-id",
                "op",
            ],
            environ=environment,
            platform="darwin",
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_request"
    assert _dispatch_state(tmp_path, capsys)["enabled"] is False


def test_a_reused_key_never_prints_a_replay_as_a_fresh_grant(
    tmp_path: Path, capsys
) -> None:
    """Mirrors `cortex transport`: opening is the dangerous direction."""

    common = ["--state-dir", str(tmp_path / "state")]
    environment = _environment(tmp_path / "home")
    grant = [
        "runtime",
        *common,
        "enable-dispatch",
        "--seconds",
        "60",
        "--actor-id",
        "op",
        "--idempotency-key",
        "reused-operator-key-0001",
    ]

    assert main(grant, environ=environment, platform="darwin") == 0
    capsys.readouterr()
    assert (
        main(
            ["runtime", *common, "disable-dispatch", "--actor-id", "op"],
            environ=environment,
            platform="darwin",
        )
        == 0
    )
    capsys.readouterr()

    # The same key now replays a receipt naming a decision that is no longer in
    # force. Exiting 0 here would tell the operator dispatch is enabled when it
    # is not, which is the one direction that must never be guessed.
    assert main(grant, environ=environment, platform="darwin") == 1
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["error"] == "replayed_decision"
    assert refusal["state"]["enabled"] is False


def test_a_reused_disable_key_still_closes_the_gate(tmp_path: Path, capsys) -> None:
    """The operator asked for it closed, so it is closed -- under a fresh key."""

    common = ["--state-dir", str(tmp_path / "state")]
    environment = _environment(tmp_path / "home")
    revoke = [
        "runtime",
        *common,
        "disable-dispatch",
        "--actor-id",
        "op",
        "--idempotency-key",
        "reused-operator-key-0001",
    ]

    assert main(revoke, environ=environment, platform="darwin") == 0
    capsys.readouterr()
    assert (
        main(
            [
                "runtime",
                *common,
                "enable-dispatch",
                "--permanent",
                "--actor-id",
                "op",
            ],
            environ=environment,
            platform="darwin",
        )
        == 0
    )
    capsys.readouterr()

    assert main(revoke, environ=environment, platform="darwin") == 1
    assert json.loads(capsys.readouterr().err)["error"] == "replayed_decision"
    # Refused, and yet the gate is shut: the refusal is about the receipt, not
    # about the decision.
    assert _dispatch_state(tmp_path, capsys)["enabled"] is False


def test_runtime_status_still_neither_creates_nor_migrates_control_db(
    tmp_path: Path, capsys
) -> None:
    """⟦P5.4c⟧ Reporting the gate must not make a reader a writer.

    `deployment/cutover/preflight.sh` runs `runtime status` as a read-only
    precondition against a STOPPED product whose `control.db` is checked with
    `mode=ro&immutable=1`; the merged `_control_db_at_captures` test guards the
    same property for the other read-only commands.
    """

    state = tmp_path / "state"
    assert (
        main(
            ["runtime", "--state-dir", str(state), "status"],
            environ=_environment(tmp_path / "home"),
            platform="darwin",
        )
        == 0
    )
    reported = json.loads(capsys.readouterr().out)["dispatch"]
    assert reported == {"enabled": False, "mode": None, "expires_at": None}
    assert list(tmp_path.rglob("control.db")) == []


def test_a_reused_key_inside_its_own_grant_refuses_rather_than_replaying(
    tmp_path: Path, capsys
) -> None:
    """⟦F-B9⟧ The replay guard only caught a SUPERSEDED decision.

    Re-running `enable-dispatch` with the same key while that same decision is
    still in force replayed the receipt, passed the check, and printed
    `decision: enable` with the FIRST grant's `expires_at` at exit 0 -- so a
    script re-running cutover step 4a after a hiccup believed it had a fresh
    1800 s and had whatever was left of the original.
    """

    common = ["--state-dir", str(tmp_path / "state")]
    environment = _environment(tmp_path / "home")
    grant = [
        "runtime",
        *common,
        "enable-dispatch",
        "--seconds",
        "1800",
        "--actor-id",
        "op",
        "--idempotency-key",
        "reused-operator-key-0002",
    ]

    assert main(grant, environ=environment, platform="darwin") == 0
    first = json.loads(capsys.readouterr().out)

    assert main(grant, environ=environment, platform="darwin") == 1
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["error"] == "replayed_decision"
    assert refusal["decision_id"] == first["decision_id"]
    # Refused, and the gate the operator already had is untouched.
    assert refusal["state"]["enabled"] is True
