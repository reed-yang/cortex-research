"""Narrow product CLI integration for managed runtime updates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Mapping

from ..control.schema import RUNTIME_ACTIVATION_MIGRATION
from ..control.errors import ControlStoreError
from ..control.store import ControlStore
from ..lifecycle import LifecycleError, daemon_health
from ..paths import PathRegistry
from .approval import ControlReleaseApprovals, ReleaseApprovalError
from .capability import (
    CAPABILITY_RECORD_RELATIVE,
    CapabilityAssertionError,
    assert_active_transport_capability,
    default_capability_record,
)
from .egress_probe import DEFAULT_CAP_SECONDS, EgressProbeError, run_egress_probe
from .models import ReleaseManifest
from .service import Candidate, DigestPinVerifier, RuntimeUpdateService
from .supervisor import WorkerSupervisor
from .worker_launch import WorkerLaunchError


def add_runtime_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    common: argparse.ArgumentParser,
) -> None:
    runtime = subparsers.add_parser("runtime", parents=[common])
    commands = runtime.add_subparsers(dest="runtime_command", required=True)
    commands.add_parser("status")
    commands.add_parser("freeze")
    commands.add_parser("thaw")

    check = commands.add_parser("check")
    check.add_argument("--catalog", type=Path, required=True)
    check.add_argument("--trusted-catalog-sha256", required=True)

    import_parser = commands.add_parser("import")
    import_parser.add_argument("--catalog", type=Path, required=True)
    import_parser.add_argument("--manifest", type=Path, required=True)
    import_parser.add_argument("--attestation", type=Path, required=True)
    import_parser.add_argument("--patch-ledger", type=Path)
    import_parser.add_argument("--artifact", type=Path, required=True)
    import_parser.add_argument("--trusted-catalog-sha256", required=True)
    import_parser.add_argument("--trusted-attestation-sha256", required=True)

    stage = commands.add_parser("stage")
    stage.add_argument("--release-id", required=True)
    stage.add_argument("--state-snapshot", type=Path)
    activate = commands.add_parser("activate")
    activate.add_argument("--release-id", required=True)
    commands.add_parser("rollback")

    # ⟦S3.4/D6⟧ Two decisions, two commands. `approve` says these bytes may run;
    # enabling dispatch is a different decision on a different table, and neither
    # command can reach the other's.
    approve = commands.add_parser("approve")
    approve.add_argument("--release-id", required=True)
    approve.add_argument("--manifest-sha256", required=True)
    approve.add_argument("--actor-id", default="operator")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--release-id", required=True)
    revoke.add_argument("--manifest-sha256", required=True)
    revoke.add_argument("--actor-id", default="operator")
    # ⟦P5.4c⟧ The third decision, and the one no command could make before.
    # `approve` says these bytes may run; `transport enable-window` says the
    # product holds the bot token; this says the orchestrator may dispatch a
    # turn at all (migration 12). Three tables, three commands, and the window
    # procedure has to name each one separately -- coupling any two of them in
    # code would let one grant imply another the operator never made.
    enable_dispatch = commands.add_parser("enable-dispatch")
    enable_dispatch.add_argument("--seconds", type=int)
    enable_dispatch.add_argument("--permanent", action="store_true")
    enable_dispatch.add_argument("--actor-id", default="operator")
    enable_dispatch.add_argument("--idempotency-key")
    disable_dispatch = commands.add_parser("disable-dispatch")
    disable_dispatch.add_argument("--actor-id", default="operator")
    disable_dispatch.add_argument("--idempotency-key")

    assertion = commands.add_parser("assert-transport-capability", parents=[common])
    assertion.add_argument(
        "--output",
        type=Path,
        help=(
            "where to write capability-assertion.json; defaults to "
            f"$HOME/{CAPABILITY_RECORD_RELATIVE}, the path "
            "deployment/cutover/lib.sh derives for CUTOVER_CAPABILITY_RECORD"
        ),
    )
    # ⟦P5.6⟧ The token-free network probe, under the same profile the worker
    # runs in. Beside `assert-transport-capability` because it needs the same
    # things: the ACTIVE release, its interpreter, and no window.
    egress = commands.add_parser("probe-egress", parents=[common])
    egress.add_argument(
        "--host",
        help="override the host to probe (default: the transport's endpoint)",
    )
    egress.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CAP_SECONDS,
        help="per-family cap in seconds (default %(default)s)",
    )
    egress.add_argument(
        "--unsandboxed",
        action="store_true",
        help="run the same script outside the seatbelt, for comparison only",
    )


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _document(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError("update document must be a regular file")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)


def _control_store(paths: PathRegistry) -> ControlStore:
    store = ControlStore(paths.control_database_file)
    store.initialize()
    return store


class _DeferredControlApprovals:
    """The real D6 gate, opening `control.db` only if it is actually consulted.

    ⟦MRG-D1⟧ `_run_runtime_command` builds one service before the command
    switch, and `_control_store` calls `ControlStore.initialize()` — which is
    `apply_migrations` plus a WAL pragma. Building the gate eagerly therefore
    made every `cortex runtime <cmd>` a WRITER of the control database,
    including the two `deployment/cutover/preflight.sh` runs as read-only
    preconditions (`status` is gate (c); `assert-transport-capability` produces
    gate (d)'s record) — ahead of that script's own WAL, backup-proof-coverage
    and snapshot gates. `deployment/cutover/rollback.sh` states the discipline
    in prose and `transport_cli.py` already encodes it for `transport status`.

    Deferred rather than `approvals=None` for the read-only commands: `None` is
    reserved by `require_release_approval` for "no approval authority is
    configured", which refuses, and it must keep meaning that rather than
    quietly becoming "this command did not need one". Every door that consults
    D6 gets the real authority; `status`, `check`, `freeze`, `thaw` and
    `assert-transport-capability` never reach a door, and `import` is
    deliberately ungated — a build has to be far enough onto the machine for an
    operator to read its manifest digest before it can be approved — so none of
    them opens the store at all.
    """

    def __init__(self, paths: PathRegistry) -> None:
        self._paths = paths
        self._gate: ControlReleaseApprovals | None = None

    def approved(self, release_id: str, manifest_sha256: str) -> bool:
        if self._gate is None:
            self._gate = ControlReleaseApprovals(_control_store(self._paths))
        return self._gate.approved(release_id, manifest_sha256)


def _service(
    paths: PathRegistry,
    *,
    catalog_digest: str = "0" * 64,
    attestation_digest: str = "0" * 64,
) -> RuntimeUpdateService:
    return RuntimeUpdateService(
        paths.runtime_update_root,
        catalog_verifier=DigestPinVerifier(catalog_digest),
        attestation_verifier=DigestPinVerifier(attestation_digest),
        # The real gate, wired here rather than defaulted inside the service: the
        # updater has never imported Control, and D6's decision belongs to the
        # product that owns `control.db`.
        approvals=_DeferredControlApprovals(paths),
    )


def _managed_worker(paths: PathRegistry) -> dict[str, object]:
    """What the running daemon reports about its binding, or that none answers."""

    try:
        health = daemon_health(paths)
    except LifecycleError:
        health = None
    if health is None:
        return {"state": "unknown", "reason": "daemon_not_running"}
    worker = health.get("managed_worker")
    return dict(worker) if isinstance(worker, dict) else {
        "state": "unknown",
        "reason": "daemon_does_not_report_a_worker",
    }


def _readonly_dispatch(database: Path) -> dict[str, object]:
    """The migration-12 decision in force, read without creating or migrating.

    `runtime status` is a precondition of the P6 preflight, which runs it
    against a STOPPED product and then reads `control.db` with
    `mode=ro&immutable=1`. `ControlStore.initialize()` is `apply_migrations`
    plus a WAL pragma, so asking the gate through it would make the one
    read-only command in this family a writer -- the exact failure
    `_DeferredControlApprovals` above exists to prevent, and the one
    `transport status` already encodes for the other gate.

    An absent database and a database below the migration that creates the
    table are the same answer: no decision has ever been recorded.
    """

    closed: dict[str, object] = {"enabled": False, "mode": None, "expires_at": None}
    if not database.is_file():
        return closed
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error:
        return closed
    finally:
        conn.close()
    version = 0 if row is None or row[0] is None else int(row[0])
    if version < RUNTIME_ACTIVATION_MIGRATION:
        return closed
    return ControlStore(database).runtime_activation_report()


def _dispatch_key(arguments: argparse.Namespace, prefix: str) -> str:
    """A bounded idempotency key, defaulted so the operator need not invent one."""

    supplied = getattr(arguments, "idempotency_key", None)
    if supplied:
        return str(supplied)
    return f"{prefix}-{uuid.uuid4().hex}"[:64]


def _replayed_dispatch(decision: str, record_id: str, state: dict[str, object]) -> int:
    """A reused key replayed a stored receipt: never a silent exit 0."""

    print(
        json.dumps(
            {
                "error": "replayed_decision",
                "message": (
                    "--idempotency-key was already used for an earlier "
                    f"{decision}; the receipt printed for it is a replay"
                ),
                "decision_id": record_id,
                "state": state,
            }
        ),
        file=sys.stderr,
    )
    return 1


def _decision_key(command: str, release_id: str, manifest_sha256: str) -> str:
    """A stable idempotency key, so re-running the same command is not a new row."""

    digest = hashlib.sha256(
        f"{command}\x1f{release_id}\x1f{manifest_sha256}".encode("utf-8")
    ).hexdigest()
    return f"runtime-{command}-{digest[:40]}"


def _slot_entrypoint(candidate: Candidate) -> str:
    """Read the attested entrypoint back out of the slot's own manifest.

    Two checks, not one. The manifest is re-validated through `ReleaseManifest`
    so the name obeys the same rule that let the release in, and its digest is
    re-derived and compared against the `manifest_sha256` that `slot.json`
    recorded at import. `_make_immutable` only sets mode bits, which the owner
    can undo, so the file on disk is not by itself evidence of what was
    attested — the same reason `measure_identity` re-derives it in the v2 path.
    """

    metadata = _document(candidate.slot_dir / "slot.json")
    if not isinstance(metadata, dict):
        raise ValueError("candidate slot metadata is invalid")
    manifest = _document(candidate.slot_dir / "manifest.json")
    release = ReleaseManifest.from_dict(manifest)
    if release.digest != metadata.get("manifest_sha256"):
        raise ValueError("candidate manifest does not match the attested digest")
    return release.worker_entrypoint


def _probe(candidate: Candidate) -> bool:
    """Honour the `Callable[[Candidate], bool]` contract the service relies on.

    A slot whose own evidence cannot be read is an unhealthy slot, and saying so
    is what lets both `activate` and `rollback` refuse with their own typed
    error. `rollback` in particular does not wrap its probe call the way
    `activate` does, so a probe that raises escapes it as an unrelated type.
    """

    try:
        entrypoint = _slot_entrypoint(candidate)
    except (OSError, ValueError):
        return False
    with WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate.slot_dir / "content",
        state_root=candidate.state_dir,
        worker_entrypoint=entrypoint,
    ) as worker:
        result = worker.request(
            "health", {"release_id": candidate.release_id}, timeout=5
        )
    return (
        isinstance(result, dict)
        and result.get("status") == "healthy"
        and result.get("release_id") == candidate.release_id
    )


def run_runtime_command(
    arguments: argparse.Namespace,
    paths: PathRegistry,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    try:
        return _run_runtime_command(arguments, paths, environ=environ)
    except ReleaseApprovalError as exc:
        # Caught here rather than left to the top level: this is an operator
        # decision that has not been made, not a fault, and `RuntimeUpdateError`
        # is not its family — `approval.py` cannot import `service.py`, which
        # imports it.
        print(f"error: {exc.reason_code}: {exc.release_id}", file=sys.stderr)
        return 1


def _run_runtime_command(
    arguments: argparse.Namespace,
    paths: PathRegistry,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    command = arguments.runtime_command
    service = _service(
        paths,
        catalog_digest=getattr(arguments, "trusted_catalog_sha256", "0" * 64),
        attestation_digest=getattr(
            arguments, "trusted_attestation_sha256", "0" * 64
        ),
    )
    if command == "status":
        # ⟦P5.4⟧ Two different questions in one answer. The updater's own status
        # says which release is ACTIVE on disk; `managed_worker` says which one
        # the RUNNING daemon actually bound -- and after an `activate` those two
        # disagree until the product is restarted, which is exactly the state an
        # operator needs to see before opening a window.
        print(
            json.dumps(
                service.status()
                | {
                    "managed_worker": _managed_worker(paths),
                    # ⟦P5.4c⟧ The gate `RunOrchestrator.dispatch` consults, beside
                    # the release it would run. An operator reading only
                    # `active` sees a runtime that is installed; these two say
                    # whether anything can actually use it.
                    "dispatch": _readonly_dispatch(paths.control_database_file),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif command == "freeze":
        service.freeze()
        print("frozen")
    elif command == "thaw":
        service.thaw()
        print("thawed")
    elif command == "check":
        catalog = service.check_catalog(_document(arguments.catalog))
        print(f"catalog valid: sequence {catalog.payload.sequence}")
    elif command == "import":
        service.import_release(
            catalog=_document(arguments.catalog),
            manifest=_document(arguments.manifest),
            attestation=_document(arguments.attestation),
            patch_ledger=(
                _document(arguments.patch_ledger)
                if arguments.patch_ledger is not None
                else None
            ),
            artifact=arguments.artifact,
        )
        print("imported")
    elif command == "stage":
        candidate = service.stage(
            arguments.release_id, source_state=arguments.state_snapshot
        )
        print(f"staged: {candidate.release_id}")
    elif command == "activate":
        candidate = service.activate(arguments.release_id, probe=_probe)
        print(f"active: {candidate.release_id}")
    elif command == "rollback":
        candidate = service.rollback(probe=_probe)
        print(f"active: {candidate.release_id}")
    elif command in {"approve", "revoke"}:
        store = _control_store(paths)
        decide = (
            store.approve_runtime_release
            if command == "approve"
            else store.revoke_runtime_release
        )
        record = decide(
            release_id=arguments.release_id,
            manifest_sha256=arguments.manifest_sha256,
            actor_id=arguments.actor_id,
            idempotency_key=_decision_key(
                command, arguments.release_id, arguments.manifest_sha256
            ),
        )
        print(f"{record.decision}d: {record.release_id} @ {record.manifest_sha256}")
    elif command in {"enable-dispatch", "disable-dispatch"}:
        return _run_dispatch_decision(arguments, paths, command)
    elif command == "assert-transport-capability":
        output = arguments.output
        if output is None:
            # The caller's environment, never `os.environ`: `main` already
            # resolved every other path from the mapping it was handed, and a
            # command that wrote into the real `$HOME` regardless would be
            # unrunnable under a test and untrustworthy under a rehearsal.
            home = (os.environ if environ is None else environ).get("HOME")
            if not home:
                print("HOME is required to locate the assertion record", file=sys.stderr)
                return 2
            output = default_capability_record(Path(home))
        try:
            exit_code, record = assert_active_transport_capability(
                service, output=output.resolve()
            )
        except (CapabilityAssertionError, WorkerLaunchError) as exc:
            # Typed, not a traceback: an operator running this the morning of a
            # window needs a sentence, and the P6 preflight needs a non-zero
            # exit rather than a Python stack on stderr.
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if record.get("result") != "ok":
            # The operator running this the morning of a window needs a
            # sentence, not only a JSON object -- but the record is written
            # either way, because the preflight reads the file and a stale
            # success from the previous release is the failure mode this
            # command exists to prevent.
            print(
                f"error: {record.get('reason')}: {record.get('detail')}",
                file=sys.stderr,
            )
        # One JSON object on stdout, like every other operator-facing transport
        # command, so a runbook step can be recorded verbatim.
        print(json.dumps(record | {"record": str(output)}, sort_keys=True))
        return exit_code
    elif command == "probe-egress":
        try:
            report = run_egress_probe(
                service,
                environ=os.environ if environ is None else environ,
                host=arguments.host,
                cap_seconds=arguments.timeout,
                sandboxed=not arguments.unsandboxed,
                descriptor_path=paths.state_dir / "managed-worker" / "descriptor.json",
            )
        except EgressProbeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # One JSON object on stdout; the one sentence an operator needs on
        # stderr, so a runbook step can read either.
        print(json.dumps(report, sort_keys=True))
        if report.get("advisory"):
            print(f"advisory: {report['advisory']}", file=sys.stderr)
        return 0
    else:  # pragma: no cover - argparse enforces the closed command set
        return 2
    return 0


def _run_dispatch_decision(
    arguments: argparse.Namespace, paths: PathRegistry, command: str
) -> int:
    """`cortex runtime enable-dispatch|disable-dispatch` -- the migration-12 gate.

    Mirrors `cortex transport` deliberately, including the replay discipline:
    one JSON object on stdout, a typed refusal with a category on stderr, and
    never a credential anywhere near either. The two gates stay two decisions;
    this command cannot reach the transport window and that one cannot reach
    this table.
    """

    store = _control_store(paths)
    try:
        if command == "enable-dispatch":
            permanent = bool(getattr(arguments, "permanent", False))
            seconds = getattr(arguments, "seconds", None)
            if permanent == (seconds is not None):
                # Never a default: a grant that happens because a flag was
                # forgotten is a grant nobody made.
                raise ValueError(
                    "exactly one of --permanent or --seconds is required"
                )
            # ⟦F-B9⟧ Read the head BEFORE the call, because the receipt this
            # command must refuse is the one that names a decision already in
            # force. Re-running `enable-dispatch` with the same key inside its
            # own grant replayed the first receipt and printed the FIRST
            # grant's `expires_at` at exit 0 -- so a script re-running step 4a
            # after a hiccup believed it had a fresh 1800 s and had whatever
            # was left of the original.
            before = store.runtime_activation()
            record = store.enable_runtime_activation(
                mode="permanent" if permanent else "window",
                window_seconds=None if permanent else seconds,
                actor_id=arguments.actor_id,
                idempotency_key=_dispatch_key(arguments, "cli-dispatch"),
            )
            state = store.runtime_activation_report()
            in_force = store.runtime_activation()
            if before is not None and before.id == record.id:
                # The decision this "grant" returned already existed. Enabling
                # is the dangerous direction, and `disable-dispatch` already
                # gets superseding-key treatment for the same class of mistake.
                return _replayed_dispatch("enable", record.id, state)
            if in_force is None or in_force.id != record.id:
                # A replayed receipt naming a decision that is no longer in
                # force. Enabling is the dangerous direction, so this refuses
                # rather than reporting authority the operator does not have.
                return _replayed_dispatch("enable", record.id, state)
            payload: dict[str, object] = {
                "decision": "enable",
                "decision_id": record.id,
                "mode": record.mode,
                "expires_at": (
                    None if record.expires_at is None else record.expires_at.isoformat()
                ),
                "state": state,
            }
        else:
            record = store.disable_runtime_activation(
                actor_id=arguments.actor_id,
                idempotency_key=_dispatch_key(arguments, "cli-dispatch-off"),
            )
            replayed = store.runtime_activation() is not None
            if replayed:
                # The disable request body is constant, so a reused key replays
                # the stored receipt and the gate stays open while the command
                # prints a decision and exits 0. The operator asked for it
                # closed, so it is closed -- under a key bound to the decision
                # being superseded, which cannot collide with the one they
                # reused.
                current = store.runtime_activation()
                record = store.disable_runtime_activation(
                    actor_id=arguments.actor_id,
                    idempotency_key=(
                        f"cli-dispatch-off-supersede-"
                        f"{'none' if current is None else current.id}"
                    )[:64],
                )
            state = store.runtime_activation_report()
            if replayed:
                return _replayed_dispatch("disable", record.id, state)
            payload = {
                "decision": "disable",
                "decision_id": record.id,
                "state": state,
            }
    except ControlStoreError as exc:
        print(
            json.dumps({"error": exc.category, "message": str(exc)}), file=sys.stderr
        )
        return 1
    except ValueError as exc:
        print(
            json.dumps({"error": "invalid_request", "message": str(exc)}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
