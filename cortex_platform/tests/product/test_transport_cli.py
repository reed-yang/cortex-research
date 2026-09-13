"""`cortex transport …`: the window procedure as supported commands.

Every step of the window ceremony had to be driven by hand-written `python -c`
one-liners against `ControlStore` before this. A ceremony that can only be
performed by improvised code has steps nobody reviewed and evidence nobody can
reproduce.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_platform.product.cli import main
from cortex_platform.product.control import ControlStore


def _paths(root: Path) -> list[str]:
    return [
        "--config-dir", str(root / "config"),
        "--data-dir", str(root / "data"),
        "--state-dir", str(root / "state"),
        "--cache-dir", str(root / "cache"),
        "--log-dir", str(root / "log"),
    ]


def _run(root: Path, *args: str, capsys) -> tuple[int, dict]:
    code = main([*args, *_paths(root)], environ={"HOME": str(root)})
    out, err = capsys.readouterr()
    text = out.strip() or err.strip()
    return code, (json.loads(text) if text else {})


def _store(root: Path) -> ControlStore:
    return ControlStore(root / "data" / "control.db")


def test_status_reports_a_closed_gate_on_a_fresh_product(tmp_path, capsys) -> None:
    code, payload = _run(tmp_path, "transport", "status", capsys=capsys)
    assert code == 0
    assert payload["enabled"] is False
    assert payload["window_id"] is None
    assert payload["telegram_mode"] == "shadow"
    assert payload["adapter_configured"] is False


def test_a_window_can_be_opened_closed_and_observed(tmp_path, capsys) -> None:
    code, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    assert code == 0
    window_id = opened["window_id"]
    assert opened["state"]["enabled"] is True
    assert opened["state"]["window_id"] == window_id
    assert opened["expires_at"]

    code, status = _run(tmp_path, "transport", "status", capsys=capsys)
    assert (code, status["enabled"], status["window_id"]) == (0, True, window_id)

    code, disabled = _run(
        tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys
    )
    assert code == 0
    assert disabled["state"]["enabled"] is False

    code, closed = _run(
        tmp_path,
        "transport", "close-window",
        "--window", window_id,
        "--proof", "supervisor close() returned and process.poll() is not None",
        "--actor", "operator",
        "--poller-stopped", "true",
        capsys=capsys,
    )
    assert code == 0
    # The proof is echoed: the audit row is what this will be read for later,
    # so the operator sees what was written, not what they meant to write.
    assert closed["proof"].startswith("supervisor close()")
    assert closed["poller_stopped"] is True


@pytest.mark.parametrize("seconds", ["0", "-1", "1801", "604800"])
def test_a_window_longer_than_half_an_hour_is_refused(
    tmp_path, capsys, seconds: str
) -> None:
    code, payload = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", seconds, "--actor", "operator",
        capsys=capsys,
    )
    assert code == 1
    assert payload["error"] == "invalid_request"
    assert _store(tmp_path).telegram_dispatch_enabled() is False


def test_closing_a_window_that_is_still_open_is_a_typed_refusal(
    tmp_path, capsys
) -> None:
    """Step 5 disables by decision and only then records the close."""

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    code, payload = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--proof", "premature",
        "--actor", "operator",
        "--poller-stopped", "true",
        capsys=capsys,
    )
    assert code == 1
    assert payload["error"] == "invalid_transition"


def test_closing_an_unknown_window_is_a_typed_refusal(tmp_path, capsys) -> None:
    code, payload = _run(
        tmp_path,
        "transport", "close-window",
        "--window", "transport-activation-does-not-exist",
        "--proof", "none",
        "--actor", "operator",
        "--poller-stopped", "true",
        capsys=capsys,
    )
    assert code == 1
    assert payload["error"] == "not_found"


def test_no_command_reads_or_prints_a_credential(tmp_path, capsys) -> None:
    """D-P5-4: nothing in this surface accepts or echoes a token."""

    import cortex_platform.product.transport_cli as module

    source = Path(module.__file__).read_text()
    for forbidden in ("TELEGRAM_BOT_TOKEN", "bot_token", "--token", "reveal("):
        assert forbidden not in source, forbidden

    _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "60", "--actor", "operator",
        capsys=capsys,
    )
    code, status = _run(tmp_path, "transport", "status", capsys=capsys)
    assert code == 0
    assert "token" not in json.dumps(status).lower()


def test_close_window_refuses_to_default_the_derived_proof(tmp_path, capsys) -> None:
    """D-P5-5 forbids a literal here by name.

    `--poller-stopped` defaulted to `true` with help text that called it "the
    derived value". The only production writer of the derived proof therefore
    wrote a literal whenever the operator did not think to pass the flag, and
    the audit row could not be told apart from one that was actually observed.
    Since P5.4 it is optional again -- but only because the DAEMON derives it,
    and with no daemon running the operator must still supply both halves.
    """

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)

    code, refusal = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--proof", "nothing was derived",
        "--actor", "operator",
        capsys=capsys,
    )

    assert code == 1
    assert refusal["error"] == "invalid_request"
    assert "--poller-stopped" in refusal["message"]
    with sqlite3.connect(tmp_path / "data" / "control.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM control_audit WHERE aggregate_type = ?",
            ("transport_window",),
        ).fetchone()[0] == 0


def test_close_window_asks_a_running_daemon_for_the_derivation(
    tmp_path, capsys, monkeypatch
) -> None:
    """The P5.4 seam, from the operator's side.

    The pid and the supervisor handle live in `cortexd`; this process holds a
    `ControlStore` and nothing else. So when a daemon answers, the command is a
    request for a derivation rather than a place to type one.
    """

    import cortex_platform.product.transport_cli as module

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)

    requests: list[dict] = []
    monkeypatch.setattr(
        module, "daemon_status", lambda paths: SimpleNamespace(state="running")
    )

    def _request(paths, *, method, path, payload=None, idempotency_key=None, timeout=60.0):
        requests.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        return 200, {
            "recorded": "transport_window_closed",
            "window_id": opened["window_id"],
            "derivation": {
                "poller_stopped": True,
                "worker_pid": 4242,
                "proof": "close() returned; pid 4242 poll()=-15; lsof … empty",
            },
        }

    monkeypatch.setattr(module, "daemon_control_request", _request)

    code, closed = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--actor", "operator",
        capsys=capsys,
    )
    assert code == 0
    assert closed["derived_by"] == "cortexd"
    assert closed["derivation"]["poller_stopped"] is True
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == (
        f"/api/v1/transport/windows/{opened['window_id']}/close"
    )
    # Neither half of the derivation crosses the boundary.
    assert requests[0]["payload"] == {"actor_id": "operator"}


def test_a_literal_is_refused_while_a_daemon_is_reachable(
    tmp_path, capsys, monkeypatch
) -> None:
    """And refused BEFORE the request: a rejection that closed the window first
    would be a refusal in name only."""

    import cortex_platform.product.transport_cli as module

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)

    monkeypatch.setattr(
        module, "daemon_status", lambda paths: SimpleNamespace(state="running")
    )
    called: list[str] = []
    monkeypatch.setattr(
        module,
        "daemon_control_request",
        lambda *args, **kwargs: called.append("sent") or (200, {}),
    )

    for extra in (
        ["--poller-stopped", "true"],
        ["--poller-stopped", "false"],
        ["--proof", "i looked with lsof"],
    ):
        code, refusal = _run(
            tmp_path,
            "transport", "close-window",
            "--window", opened["window_id"],
            "--actor", "operator",
            *extra,
            capsys=capsys,
        )
        assert code == 1
        assert refusal["error"] == "daemon_derives_release_proof"
    assert called == []


def test_unreadable_daemon_state_refuses_rather_than_falling_back(
    tmp_path, capsys, monkeypatch
) -> None:
    import cortex_platform.product.transport_cli as module

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)

    def _raise(paths):
        raise module.LifecycleError("unsafe metadata file")

    monkeypatch.setattr(module, "daemon_status", _raise)
    code, refusal = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--actor", "operator",
        capsys=capsys,
    )
    assert code == 1
    assert refusal["error"] == "daemon_state_unreadable"


def test_close_window_records_a_false_proof_verbatim(tmp_path, capsys) -> None:
    """`false` is a real answer: a window that was not proven released."""

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)

    code, closed = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--proof", "lsof reported no sockets but close() was never called",
        "--actor", "operator",
        "--poller-stopped", "false",
        capsys=capsys,
    )

    assert code == 0
    assert closed["poller_stopped"] is False


def _schema_version(database: Path) -> int:
    conn = sqlite3.connect(database)
    try:
        return int(
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        )
    finally:
        conn.close()


def test_status_does_not_create_the_control_database(tmp_path, capsys) -> None:
    """`status` is the one purely informational `cortex transport` command.

    `run_transport_command` called `store.initialize()` before dispatching, and
    `ControlStore.initialize` runs `apply_migrations` -- so a read-only-looking
    command created the database and its binding key, and migrated an existing
    one. On the gen-8 mini that means running `transport status` from the gen-9
    tree before the ceremony migrates control.db outside the documented R0-C
    order, and can leave a non-empty `-wal` that `record-proof` then refuses.
    """

    code, payload = _run(tmp_path, "transport", "status", capsys=capsys)

    assert code == 0
    assert payload["enabled"] is False
    database = tmp_path / "data" / "control.db"
    assert not database.exists()
    # Not the binding key either: `initialize()` mints one.
    assert not (tmp_path / "data" / ".control.db.transport.key").exists()


def test_status_leaves_a_schema_13_control_database_unmigrated(
    tmp_path, capsys
) -> None:
    """The state gen 8 ships, read by the gen 9 command surface."""

    from cortex_platform.product.control import schema as control_schema

    database = tmp_path / "data" / "control.db"
    database.parent.mkdir(parents=True, mode=0o700)
    conn = sqlite3.connect(database)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
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
    before = _schema_version(database)

    code, payload = _run(tmp_path, "transport", "status", capsys=capsys)

    assert code == 0
    assert payload["enabled"] is False
    assert _schema_version(database) == before == control_schema.CAPTURES_MIGRATION


def test_a_reused_key_never_leaves_disable_a_silent_no_op(tmp_path, capsys) -> None:
    """P5-08: the disable request body is constant, so a reused key replays.

    The CLI printed `{"decision": "disable", ...}` with exit 0 while the gate
    stayed open -- and a runbook-pasted fixed key is the natural operator
    action. Two things are wrong with that and both are fixed: the operator
    asked for the gate to be closed, so it is closed; and the run is not a
    clean success, so it does not exit 0.
    """

    _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    code, _first = _run(
        tmp_path,
        "transport", "disable", "--actor", "operator",
        "--idempotency-key", "runbook-disable-key-01",
        capsys=capsys,
    )
    assert code == 0
    assert _store(tmp_path).telegram_dispatch_enabled() is False

    _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        "--idempotency-key", "runbook-window-key-002",
        capsys=capsys,
    )
    assert _store(tmp_path).telegram_dispatch_enabled() is True

    code, payload = _run(
        tmp_path,
        "transport", "disable", "--actor", "operator",
        "--idempotency-key", "runbook-disable-key-01",
        capsys=capsys,
    )

    assert code != 0
    assert payload["error"] == "replayed_decision"
    assert _store(tmp_path).telegram_dispatch_enabled() is False


def test_a_reused_key_on_enable_window_is_refused_without_reopening(
    tmp_path, capsys
) -> None:
    """The dangerous direction is refused, not retried.

    `disable` is the safe direction and the operator's intent is honoured.
    Opening a window is not: a replayed receipt that named an old window must
    never be turned into a fresh window nobody asked for.
    """

    _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        "--idempotency-key", "runbook-window-key-001",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)

    code, payload = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        "--idempotency-key", "runbook-window-key-001",
        capsys=capsys,
    )

    assert code != 0
    assert payload["error"] == "replayed_decision"
    assert _store(tmp_path).telegram_dispatch_enabled() is False


def test_transport_status_reports_the_running_loops_or_says_there_are_none(
    tmp_path, capsys, monkeypatch
) -> None:
    """⟦P5.4b⟧ Durable state cannot say whether the inbound loop is alive.

    An operator watching a window has to be able to tell "quiet because nothing
    happened" from "quiet because the loop died", and that is a property of a
    process rather than of `control.db`.
    """

    import cortex_platform.product.transport_cli as module

    code, payload = _run(tmp_path, "transport", "status", capsys=capsys)
    assert code == 0
    assert payload["live"] == {"daemon": "not_running"}

    monkeypatch.setattr(
        module,
        "daemon_health",
        lambda paths: {
            "managed_worker": {"state": "bound", "release_id": "hermes-0.15.0-gen9"},
            "transport_window": {
                "window_id": "transport-activation-1",
                "poller_state": "running",
                "poller_restarts": 1,
                "polls": 12,
                "handled": 3,
                "drain": {
                    "delivered": 2,
                    "categories": {"delivered": 2},
                    "last_outcome": {"category": "delivered"},
                },
            },
        },
    )
    code, payload = _run(tmp_path, "transport", "status", capsys=capsys)
    assert code == 0
    assert payload["live"]["daemon"] == "running"
    assert payload["live"]["window"]["poller_state"] == "running"
    assert payload["live"]["window"]["poller_restarts"] == 1
    assert payload["live"]["window"]["drain"]["delivered"] == 2
    assert payload["live"]["managed_worker"]["state"] == "bound"


def test_a_live_but_unhealthy_daemon_is_not_treated_as_no_daemon(
    tmp_path, capsys, monkeypatch
) -> None:
    """⟦P54A-7⟧ It still holds the worker and the token; only health is mute.

    Reachability was `state == "running"`, so a wedged-but-alive daemon fell
    through to the no-daemon path -- which then accepted the hand-typed
    `--proof` / `--poller-stopped` literals D-P5-5 forbids while a daemon owns
    the derivation.
    """

    import cortex_platform.product.transport_cli as module

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)
    monkeypatch.setattr(
        module,
        "daemon_status",
        lambda paths: SimpleNamespace(state="unhealthy", process_identity=True),
    )

    code, refusal = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--actor", "operator",
        "--proof", "close() returned; pid 1 poll()=0; lsof empty",
        "--poller-stopped", "true",
        capsys=capsys,
    )

    assert code == 1
    assert refusal["error"] == "daemon_not_answering"


def test_a_stale_metadata_file_is_still_the_no_daemon_path(
    tmp_path, capsys, monkeypatch
) -> None:
    """`stale` means the recorded pid is NOT this daemon, so nobody holds it."""

    import cortex_platform.product.transport_cli as module

    _, opened = _run(
        tmp_path,
        "transport", "enable-window", "--seconds", "600", "--actor", "operator",
        capsys=capsys,
    )
    _run(tmp_path, "transport", "disable", "--actor", "operator", capsys=capsys)
    monkeypatch.setattr(
        module,
        "daemon_status",
        lambda paths: SimpleNamespace(state="stale", process_identity=False),
    )

    code, payload = _run(
        tmp_path,
        "transport", "close-window",
        "--window", opened["window_id"],
        "--actor", "operator",
        "--proof", "close() returned; pid 1 poll()=0; lsof empty",
        "--poller-stopped", "true",
        capsys=capsys,
    )

    assert code == 0
    assert payload["derived_by"] == "operator"
