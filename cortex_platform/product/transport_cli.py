"""`cortex transport …` — the operator's supported commands over the gate.

The window procedure is written for an operator who is present, and until now
every step of it had to be driven by hand-written `python -c` one-liners
against `ControlStore`: the P6 cutover tooling found there was no supported
command to open a window, close one, or even ask whether one was open. A
ceremony that can only be performed by improvised code is a ceremony whose
steps are not reviewable and whose evidence is not reproducible.

Everything prints one JSON object on stdout so a runbook step can be recorded
verbatim. A typed refusal is a non-zero exit with the category on stderr, and
no command anywhere here reads, accepts or prints a credential -- the token
lives in `secret_refs` and reaches only a worker environment (D-P5-4).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from .config import load_config, telegram_mode, telegram_setting
from .control import ControlStore, ControlStoreError
from .control.schema import TRANSPORT_ACTIVATION_MIGRATION
from .lifecycle import (
    LifecycleError,
    daemon_control_request,
    daemon_health,
    daemon_status,
)
from .paths import PathRegistry

_MAX_WINDOW_SECONDS = 1_800


def add_transport_parser(subparsers: Any, *, common: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser("transport", parents=[common])
    actions = parser.add_subparsers(dest="transport_command", required=True)

    actions.add_parser("status", parents=[common])

    enable = actions.add_parser("enable-window", parents=[common])
    enable.add_argument("--seconds", type=int, required=True)
    enable.add_argument("--actor", required=True)
    enable.add_argument("--idempotency-key")

    disable = actions.add_parser("disable", parents=[common])
    disable.add_argument("--actor", required=True)
    disable.add_argument("--idempotency-key")

    close = actions.add_parser("close-window", parents=[common])
    close.add_argument("--window", required=True)
    close.add_argument("--actor", required=True)
    # ⟦P5.4⟧ Both are now the NO-DAEMON form of this command. When `cortexd` is
    # running it owns the supervisor and the worker pid, so it -- and only it --
    # can derive `poller_stopped` and write the sentence behind it; passing
    # either by hand in that situation is refused rather than silently
    # preferred, because the whole point of D-P5-5 is that the value is not
    # something anybody types.
    close.add_argument(
        "--proof",
        help="required only when no daemon is running; otherwise cortexd derives it",
    )
    close.add_argument(
        "--poller-stopped",
        choices=("true", "false"),
        # D-P5-5 forbids a literal here by name. Defaulting it made the only
        # production writer of the derived proof write `true` whenever the
        # operator did not think to pass the flag, and an audit row that says
        # the poller was proven released is worth nothing if it says so
        # because nobody looked.
        help="the DERIVED value, and only without a daemon; `false` records a window that was not proven released",
    )


def _key(arguments: argparse.Namespace, prefix: str) -> str:
    """A bounded idempotency key, defaulted so the operator need not invent one."""

    supplied = getattr(arguments, "idempotency_key", None)
    if supplied:
        return str(supplied)
    import uuid

    return f"{prefix}-{uuid.uuid4().hex}"[:64]


def _supersede_key(prefix: str, store: ControlStore) -> str:
    """An idempotency key bound to the decision it is meant to supersede."""

    record = store.transport_activation("telegram")
    return f"{prefix}-supersede-{'none' if record is None else record.id}"[:64]


def _replayed(decision: str, decision_id: str, state: dict) -> int:
    """A reused key replayed a stored receipt: never a silent exit 0."""

    print(
        json.dumps(
            {
                "error": "replayed_decision",
                "message": (
                    "--idempotency-key was already used for an earlier "
                    f"{decision}; the receipt printed for it is a replay"
                ),
                "decision_id": decision_id,
                "state": state,
            }
        ),
        file=sys.stderr,
    )
    return 1


def _readonly_activation(database):
    """The decision in force, read without migrating and without creating.

    `status` is the one purely informational `cortex transport` command, and it
    used to run `apply_migrations` before dispatching: a read-only-looking
    command created the database and its binding key, and migrated an existing
    one. On the gen-8 mini that migrates control.db outside the documented
    R0-C order and can leave a `-wal` that `record-proof` then refuses.

    An absent database and a database below the migration that creates the gate
    table are the same answer -- no decision has ever been recorded -- and
    neither is a reason to write anything.
    """

    if not database.is_file():
        return None
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    version = 0 if row is None or row[0] is None else int(row[0])
    if version < TRANSPORT_ACTIVATION_MIGRATION:
        return None
    return ControlStore(database).transport_activation("telegram")


def _live(paths: PathRegistry) -> dict:
    """What the RUNNING daemon reports about its loops, or that none answers.

    `status` reads durable state, and durable state cannot say whether the
    inbound poller is alive or how many deliveries the drain has made -- those
    are properties of a process. The window procedure needs both: an operator
    watching a window has to be able to tell "quiet because nothing happened"
    from "quiet because the loop died".
    """

    try:
        health = daemon_health(paths)
    except LifecycleError:
        health = None
    if health is None:
        return {"daemon": "not_running"}
    window = health.get("transport_window")
    bridge = health.get("turn_bridge")
    return {
        "daemon": "running",
        "managed_worker": health.get("managed_worker"),
        "window": window if isinstance(window, dict) else None,
        # ⟦P5.4c⟧ The other gate's answer. A window that polls, drains and
        # answers nothing reads `runtime_activation_disabled` here, which is a
        # decision the operator has not made rather than a fault.
        "turn_bridge": bridge if isinstance(bridge, dict) else None,
    }


def _status(record, config: dict) -> dict:
    return {
        "transport": "telegram",
        "enabled": record is not None,
        "scope": None if record is None else record.scope,
        "window_id": None if record is None or record.scope != "window" else record.id,
        "expires_at": (
            None
            if record is None or record.expires_at is None
            else record.expires_at.isoformat()
        ),
        "telegram_mode": telegram_mode(config),
        # "Configured" is the honest word: the daemon builds an adapter when a
        # bot is named, and health reports whether one exists in the RUNNING
        # process. This command reads durable state, not that process.
        "adapter_configured": telegram_setting(config, "telegram_bot_identity")
        is not None
        and telegram_setting(config, "telegram_base_url") is not None,
    }


def _close_through_daemon(
    arguments: argparse.Namespace, paths: PathRegistry
) -> dict | None:
    """Ask the running daemon to derive and record, or return None if there is none.

    The P5.4 seam. `poller_stopped` is `close()` returned AND the worker process
    is gone AND `lsof -nP -a -p <pid> -i TCP -s TCP:ESTABLISHED` is empty -- and
    every one of those three needs a supervisor handle and a pid that exist only
    inside `cortexd`. This process holds a `ControlStore` and nothing else, which
    is exactly why the flag it used to require was a literal.
    """

    try:
        status = daemon_status(paths)
    except LifecycleError:
        # An unreadable or unsafe metadata file is not "no daemon": falling
        # through to the hand-derived path would let this command write a value
        # nobody derived, so it refuses instead.
        return {
            "error": "daemon_state_unreadable",
            "message": "cortexd lifecycle state could not be read",
        }
    if status.state != "running" and status.process_identity:
        # ⟦P54A-7⟧ A live-but-unhealthy daemon is not "no daemon". Its process
        # identity matches the metadata, so it is still holding the worker and
        # the bot token; only `/api/v1/health` did not answer. Falling through
        # would let the hand-typed `--proof` / `--poller-stopped` literals
        # through -- exactly what D-P5-5's refusal exists to prevent.
        return {
            "error": "daemon_not_answering",
            "message": (
                f"cortexd is {status.state} and still owns the worker, so the "
                "release proof cannot be derived or typed; stop it first"
            ),
        }
    if status.state != "running":
        return None
    # Refused BEFORE the request, never after: a rejection that had already
    # closed the window would be a refusal in name only.
    if arguments.proof is not None or arguments.poller_stopped is not None:
        return {
            "error": "daemon_derives_release_proof",
            "message": (
                "cortexd is running and owns the worker, so it derives "
                "--poller-stopped and --proof; re-run without them"
            ),
        }
    result = daemon_control_request(
        paths,
        method="POST",
        path=f"/api/v1/transport/windows/{arguments.window}/close",
        payload={"actor_id": arguments.actor},
        idempotency_key=_key(arguments, "cli-close"),
    )
    if result is None:
        return {
            "error": "daemon_unreachable",
            "message": "cortexd stopped answering before the window was closed",
        }
    status, payload = result
    if status != 200:
        return {
            "error": str(payload.get("category") or "close_window_failed"),
            "message": str(payload.get("title") or payload.get("detail") or ""),
        }
    return dict(payload) | {"derived_by": "cortexd"}


def run_transport_command(arguments: argparse.Namespace, paths: PathRegistry) -> int:
    config = load_config(paths.config_file) if paths.config_file.is_file() else {}
    command = arguments.transport_command
    database = paths.control_database_file
    try:
        if command == "status":
            # No store, and therefore no `initialize()`: this command reads.
            payload = _status(_readonly_activation(database), config) | {
                "live": _live(paths)
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        store = ControlStore(database)
        store.initialize()
        if command == "enable-window":
            if (
                type(arguments.seconds) is not int
                or not 1 <= arguments.seconds <= _MAX_WINDOW_SECONDS
            ):
                raise ValueError(
                    f"--seconds must be between 1 and {_MAX_WINDOW_SECONDS}"
                )
            record = store.enable_transport_activation(
                transport="telegram",
                scope="window",
                window_seconds=arguments.seconds,
                actor_id=arguments.actor,
                idempotency_key=_key(arguments, "cli-window"),
            )
            in_force = store.transport_activation("telegram")
            if in_force is None or in_force.id != record.id:
                # A replayed receipt naming a window that is no longer in
                # force. Opening a window is the dangerous direction, so this
                # refuses rather than minting one nobody asked for.
                return _replayed(
                    "enable", record.id, _status(in_force, config)
                )
            payload = {
                "decision": "enable",
                "scope": "window",
                "window_id": record.id,
                "expires_at": record.expires_at.isoformat()
                if record.expires_at
                else None,
                "state": _status(in_force, config),
            }
        elif command == "disable":
            record = store.disable_transport_activation(
                transport="telegram",
                actor_id=arguments.actor,
                idempotency_key=_key(arguments, "cli-disable"),
            )
            replayed = store.transport_activation("telegram") is not None
            if replayed:
                # The disable request body is constant (`{transport,
                # decision}`), so a reused key replays the stored receipt and
                # the gate stays open while the CLI prints a decision and exits
                # 0. The operator asked for the gate to be closed, so it is
                # closed -- under a key bound to the decision being superseded,
                # which cannot collide with the one they reused.
                record = store.disable_transport_activation(
                    transport="telegram",
                    actor_id=arguments.actor,
                    idempotency_key=_supersede_key("cli-disable", store),
                )
            in_force = store.transport_activation("telegram")
            if replayed:
                return _replayed("disable", record.id, _status(in_force, config))
            payload = {
                "decision": "disable",
                "decision_id": record.id,
                "state": _status(in_force, config),
            }
        else:
            derived = _close_through_daemon(arguments, paths)
            if derived is not None:
                if "error" in derived:
                    print(json.dumps(derived), file=sys.stderr)
                    return 1
                payload = derived | {
                    "state": _status(store.transport_activation("telegram"), config)
                }
            else:
                if arguments.proof is None or arguments.poller_stopped is None:
                    raise ValueError(
                        "no daemon is running, so --proof and --poller-stopped "
                        "are required and you must derive them yourself"
                    )
                store.record_transport_window_closed(
                    window_id=arguments.window,
                    poller_stopped=arguments.poller_stopped == "true",
                    proof=arguments.proof,
                    actor_id=arguments.actor,
                )
                payload = {
                    "recorded": "transport_window_closed",
                    "window_id": arguments.window,
                    # Printed back deliberately: the proof is what the audit row
                    # will be read for later, so the operator sees exactly what
                    # was written rather than what they meant to write.
                    "poller_stopped": arguments.poller_stopped == "true",
                    "proof": arguments.proof,
                    "derived_by": "operator",
                    "state": _status(store.transport_activation("telegram"), config),
                }
    except ControlStoreError as exc:
        print(
            json.dumps({"error": exc.category, "message": str(exc)}),
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(json.dumps({"error": "invalid_request", "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
