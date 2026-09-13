"""The v2 worker loop, running inside the release under its own interpreter.

Two things make this module different from `cortex_platform.product`'s copy of
the same behaviour, and both are the point of S3.2's launch-contract change.

It is imported from the slot, not from the product: the launch contract is now
`-I <slot>/content/<worker_entrypoint> --v2-descriptor …`, so the interpreter is
the one the release carries and `cortex_platform` is not importable at all. And
it measures the interpreter it is *running from* by digesting that binary's
bytes, rather than fingerprinting a path and a version string — the term the
supervisor now compares for equality, which is what closes contract F6's "built
and unbound".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Sequence

from .approval import ApprovalChoiceError
from .confinement import confinement_for, install_write_confinement
from .digests import canonical_json, digest_document
from .ledger import LedgerUnavailable, OperationConflict, OperationLedger
from .protocol import (
    PROTOCOL_V2,
    ProtocolViolation,
    SlotInterpreterDescriptor,
    WorkerError,
    WorkerResponse,
    encode_response,
    parse_request,
)
from .runtime import (
    HERMES_HOME_FORBIDDEN,
    ForkRunner,
    HermesHomeUnsafe,
    RuntimeUnavailable,
    assert_hermes_home,
)
from . import telegram as telegram_transport
from .turn import HEARTBEAT_SECONDS, Turn, TurnEmitter

_SLOT_FIELDS = {
    "schema_version",
    "artifact_sha256",
    "manifest_sha256",
    "content_tree_sha256",
}


class IdentityMismatch(RuntimeError):
    """The worker's measured slot identity differs from its descriptor."""


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityMismatch("worker identity evidence is unavailable") from exc
    if not isinstance(raw, dict):
        raise IdentityMismatch("worker identity evidence must be an object")
    return raw


def _read_closed_json(path: Path, fields: set[str]) -> dict[str, object]:
    raw = _read_json_object(path)
    if set(raw) != fields:
        raise IdentityMismatch("worker identity evidence fields are invalid")
    return raw


def content_tree_digest(root: Path) -> str:
    """The updater's `_tree_digest`, replicated entry for entry.

    Replicated rather than imported, and replicated exactly: the whole value of
    a self-measured identity is that the worker and the updater compute the same
    number from the same bytes without sharing code, so any difference here —
    key order, mode masking, the directory entry's shape — turns a healthy slot
    into a permanent identity mismatch.
    """

    entries: list[dict[str, object]] = []
    if root.is_symlink() or not root.is_dir():
        raise IdentityMismatch("worker content tree is invalid")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise IdentityMismatch("worker content tree contains a symlink")
        relative = path.relative_to(root).as_posix()
        details = path.stat()
        if path.is_dir():
            entries.append({"path": relative, "kind": "directory"})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.S_IMODE(details.st_mode),
                    "size": details.st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            raise IdentityMismatch("worker content tree contains a special file")
    return hashlib.sha256(canonical_json(entries)).hexdigest()


def measure_interpreter() -> str:
    """Digest the interpreter binary this process is executing.

    The predecessor hashed `str(sys.executable) + sys.version`, which is a claim
    about a path and a banner rather than about code: two different binaries at
    the same path with the same version string produced the same value, and
    nothing compared it to anything. This reads the bytes, so the term means
    "the interpreter I am running is the one the release carried".
    """

    try:
        executable = Path(sys.executable).resolve(strict=True)
        payload = executable.read_bytes()
    except OSError as exc:
        raise IdentityMismatch("worker interpreter is unreadable") from exc
    return hashlib.sha256(payload).hexdigest()


def measure_identity(descriptor: SlotInterpreterDescriptor) -> dict[str, str]:
    slot = descriptor.slot_path
    metadata = _read_closed_json(slot / "slot.json", _SLOT_FIELDS)
    manifest = _read_json_object(slot / "manifest.json")
    content_digest = content_tree_digest(slot / "content")
    manifest_digest = digest_document(manifest)
    interpreter_digest = measure_interpreter()
    identity = {
        "worker_protocol": PROTOCOL_V2,
        "artifact_digest": slot.name,
        "content_tree_sha256": content_digest,
        "manifest_sha256": manifest_digest,
        "slot_id": descriptor.slot_id,
        "state_generation_id": descriptor.state_generation_id,
        "release_id": descriptor.release_id,
        "interpreter_sha256": interpreter_digest,
    }
    expected = (
        metadata.get("schema_version") == 1
        and slot.name == metadata.get("artifact_sha256")
        and slot.name == descriptor.expected_artifact_digest
        and content_digest == metadata.get("content_tree_sha256")
        and content_digest == descriptor.expected_content_tree_sha256
        and manifest_digest == metadata.get("manifest_sha256")
        and manifest_digest == descriptor.expected_manifest_sha256
        and manifest.get("release_id") == descriptor.release_id
        and manifest.get("worker_entrypoint") == descriptor.worker_entrypoint
        # The new term. Everything above binds the slot; this binds the
        # interpreter the slot is being run by, which nothing bound before.
        and interpreter_digest == descriptor.expected_interpreter_sha256
    )
    if not expected:
        raise IdentityMismatch("worker identity does not match descriptor")
    return identity


HERMES_HOME_NAME = "hermes-home"
STDOUT_LOG_NAME = "worker.stdout.log"


def _private_frame_writer(stream: BinaryIO, state_dir: Path) -> BinaryIO:
    """⟦AMD-5⟧ Take fd 1 away from the fork before the fork can reach it.

    The fork has 151 gated `print` sites, and the gate is `quiet_mode`, which
    is a keyword argument rather than a guarantee. A single un-gated line on
    fd 1 would land in the middle of a frame and the supervisor would close
    the channel on an unparseable line — a failure mode that would only ever
    appear against a real fork, under load, in production.

    So the frame stream moves to a private duplicate and fd 1 is pointed at a
    log. `sys.stdout` follows fd 1, so no code in the slot has to cooperate.
    A stream with no fd (a test's buffer) is handed back unchanged.
    """

    try:
        fileno = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream
    if fileno != 1:
        return stream
    private = os.dup(fileno)
    log = os.open(
        str(state_dir / STDOUT_LOG_NAME),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os.dup2(log, fileno)
    finally:
        os.close(log)
    return os.fdopen(private, "wb", buffering=0)


def _error(
    request_id: str,
    category: str,
    message: str,
    *,
    operation_id: str | None = None,
) -> bytes:
    return encode_response(
        WorkerResponse(
            request_id=request_id,
            error=WorkerError(category, message),
            operation_id=operation_id,
        )
    )


class _TransportCalls:
    """Run one transport call at a time, off the request loop.

    The loop must keep reading while a `telegram.poll` is parked in a 30 s
    long poll — otherwise a `shutdown` sent to close a window would sit in the
    pipe until the poll returned, and ⟦AMD-4⟧'s "disable ends the inbound
    loop" would be a promise the worker could not keep. One at a time because
    two concurrent `getUpdates` against one token is the 409 the whole window
    procedure exists to avoid.
    """

    def __init__(self, writer: "_FrameWriter") -> None:
        self._writer = writer
        self._lock = threading.Lock()
        self._busy = False
        self._threads: list[threading.Thread] = []

    def submit(
        self,
        request_id: str,
        call: Callable[[], Mapping[str, object]],
    ) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        thread = threading.Thread(
            target=self._run, args=(request_id, call), name="cortex-worker-transport"
        )
        thread.daemon = True
        with self._lock:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)
        thread.start()
        return True

    def _run(self, request_id: str, call: Callable[[], Mapping[str, object]]) -> None:
        try:
            result: object = dict(call())
            payload = encode_response(
                WorkerResponse(request_id=request_id, result=result)
            )
        except telegram_transport.TelegramTransportError:
            payload = _error(
                request_id, "protocol_violation", "transport call failed"
            )
        except Exception:  # noqa: BLE001 - provider detail never leaves the worker.
            payload = _error(
                request_id, "protocol_violation", "transport call failed"
            )
        finally:
            with self._lock:
                self._busy = False
        self._writer.write(payload)

    def join(self, timeout: float) -> None:
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)


class _FrameWriter:
    """Serialize every outbound frame onto one fd.

    Three producers share it — the request loop, the turn thread and the
    heartbeat timer — and a partially written line is an unparseable frame,
    so the lock is around the write, not around the decision to write.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, payload: bytes) -> None:
        with self._lock:
            self._stream.write(payload)
            self._stream.flush()


def serve(
    descriptor: SlotInterpreterDescriptor,
    *,
    stdin: BinaryIO,
    stdout: BinaryIO,
    runner: Callable[[Mapping[str, object], object], Mapping[str, object]] | None = None,
    heartbeat_interval: float = HEARTBEAT_SECONDS,
) -> int:
    """Read requests forever; run at most one turn at a time, off this thread.

    The loop must keep reading while a turn is open — that is the whole reason
    `turn.resolve` and `turn.cancel` can reach a turn that is parked inside an
    approval callback. Concurrency stays 1 by construction: the ledger's
    exclusive flock and the `(release_id, generation_id)` state dir make this
    process the single owner, and a second `turn.begin` while one is open is an
    `operation_conflict` rather than a queue.
    """

    token = os.environ.pop("CORTEX_WORKER_TOKEN", "")
    if len(token) < 32:
        return 2
    home = descriptor.state_dir / HERMES_HOME_NAME
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # ⟦AMD-2⟧ The fork resolves `HERMES_HOME` from the process environment
        # and falls back to `Path.home()/'.hermes'` when it is unset — a
        # directory inside the sole writable root that nothing guards. So the
        # value is written back here, unconditionally, before anything from the
        # slot can run: this is the one layer no caller can forget. A caller who
        # supplied a *different* home is refused rather than silently corrected,
        # because that disagreement means some other layer is guarding the wrong
        # directory.
        inherited = os.environ.get("HERMES_HOME")
        if inherited is not None and os.path.realpath(inherited) != os.path.realpath(
            home
        ):
            raise HermesHomeUnsafe("HERMES_HOME disagrees with the worker state dir")
        os.environ["HERMES_HOME"] = str(home)
        # The first of two content checks: refuse to start at all while the
        # directory the fork executes content from holds anything. The second is
        # inside `ForkRunner`, immediately before the import itself.
        assert_hermes_home(home)
    except HermesHomeUnsafe:
        return 5
    writer = _FrameWriter(_private_frame_writer(stdout, descriptor.state_dir))
    transport = _TransportCalls(writer)
    # ⟦S3.4/D3 layer 2⟧ The last thing installed before anything from the slot
    # can run. The stdout log above is opened first because the hook has no
    # uninstall, and the same four `HERMES_HOME` names the seatbelt denies are
    # denied here too — where the refusal is a typed Python error naming the
    # path rather than an EPERM the fork may swallow.
    install_write_confinement(
        confinement_for(str(descriptor.state_dir), str(home), HERMES_HOME_FORBIDDEN)
    )
    if runner is None:
        runner = ForkRunner(home)
    identity: dict[str, str] | None = None
    identity_error: IdentityMismatch | None = None
    ledger: OperationLedger | None = None
    measured = False
    turn: Turn | None = None
    try:
        identity = measure_identity(descriptor)
    except IdentityMismatch as exc:
        identity_error = exc
    if identity_error is None:
        try:
            ledger = OperationLedger.open(descriptor.state_dir)
        except LedgerUnavailable:
            ledger = None
    for line in stdin:
        request_id = _peek_request_id(line)
        operation_id: str | None = None
        stop = False
        try:
            request = parse_request(line, token=token)
            request_id = request.request_id
            params = request.params
            if "operation_id" in params:
                operation_id = str(params["operation_id"])
            if request.method == "shutdown":
                if turn is not None:
                    turn.cancel()
                    turn.join(2)
                transport.join(2)
                result: object = {"stopping": True}
                stop = True
            elif request.method == "health.check":
                result = {
                    "healthy": identity_error is None and ledger is not None,
                    "protocol": PROTOCOL_V2,
                    "ledger_open": ledger is not None,
                    "quarantined": ledger.quarantined if ledger is not None else False,
                }
            elif request.method == "identity.measure":
                if identity_error is not None:
                    raise identity_error
                try:
                    identity = measure_identity(descriptor)
                except IdentityMismatch as exc:
                    identity_error = exc
                    measured = False
                    raise
                result = identity
            else:
                if identity_error is not None:
                    raise identity_error
                if identity is None:
                    raise IdentityMismatch("worker identity is unavailable")
                if ledger is None:
                    raise LedgerUnavailable("operation ledger is unavailable")
                if not measured:
                    raise RuntimeError("not_measured")
                if request.method == "operation.begin":
                    state = ledger.begin(
                        str(params["operation_id"]),
                        str(params["kind"]),
                        str(params["request_digest"]),
                    )
                    result = {"state": state}
                elif request.method == "operation.finish":
                    ledger.finish(
                        str(params["operation_id"]),
                        str(params["outcome"]),
                        str(params["result_digest"]),
                    )
                    result = {"state": str(params["outcome"])}
                elif request.method == "operation.status":
                    status = ledger.status(str(params["operation_id"]))
                    result = {"state": status.state}
                    if status.request_digest is not None:
                        result["request_digest"] = status.request_digest
                    if status.result_digest is not None:
                        result["result_digest"] = status.result_digest
                elif request.method == "turn.begin":
                    turn, result = _begin_turn(
                        ledger=ledger,
                        writer=writer,
                        open_turn=turn,
                        params=params,
                        runner=runner,
                        heartbeat_interval=heartbeat_interval,
                    )
                elif request.method == "telegram.capabilities":
                    result = telegram_transport.capabilities()
                elif request.method in {"telegram.send", "telegram.poll"}:
                    if not _submit_transport(transport, request, params):
                        raise OperationConflict("a transport call is already open")
                    # The reply is written by the transport thread.
                    continue
                elif request.method == "turn.resolve":
                    # The bool the turn actually produced. Answering `True`
                    # unconditionally told the backend an approval had been
                    # delivered when it had reached nothing.
                    result = {
                        "delivered": _require_open(turn, params).resolve(
                            params["decision"]
                        )
                    }
                else:
                    _require_open(turn, params).cancel()
                    result = {"canceled": True}
            if request.method == "identity.measure" and identity_error is None:
                measured = True
            payload = encode_response(
                WorkerResponse(
                    request_id=request_id, result=result, operation_id=operation_id
                )
            )
        except (ProtocolViolation, ApprovalChoiceError):
            # ⟦S3.4⟧ A choice outside the closed approval set is a protocol
            # fault, not a decision: it is refused on the wire and never reaches
            # the parked callback, so the fork can never be handed a standing
            # grant this product does not offer.
            payload = _error(
                request_id,
                "protocol_violation",
                "request violates worker protocol",
                operation_id=operation_id,
            )
        except IdentityMismatch:
            payload = _error(
                request_id, "identity_mismatch", "worker identity mismatch",
                operation_id=operation_id,
            )
        except LedgerUnavailable:
            payload = _error(
                request_id, "ledger_unavailable", "operation ledger is unavailable",
                operation_id=operation_id,
            )
        except OperationConflict:
            payload = _error(
                request_id, "operation_conflict",
                "operation conflicts with durable state",
                operation_id=operation_id,
            )
        except HermesHomeUnsafe:
            payload = _error(
                request_id, "hermes_home_unsafe",
                "HERMES_HOME is not safe to import the runtime from",
                operation_id=operation_id,
            )
        except RuntimeUnavailable:
            payload = _error(
                request_id, "runtime_unavailable",
                "runtime is not importable from this slot",
                operation_id=operation_id,
            )
        except _TurnUnknown:
            payload = _error(
                request_id, "turn_unknown", "no such open turn",
                operation_id=operation_id,
            )
        except RuntimeError as exc:
            if str(exc) == "not_measured":
                payload = _error(
                    request_id,
                    "not_measured",
                    "worker identity has not been measured",
                    operation_id=operation_id,
                )
            else:
                payload = _error(
                    request_id, "protocol_violation", "request failed",
                    operation_id=operation_id,
                )
        writer.write(payload)
        if stop:
            return 0
    return 0


def _peek_request_id(line: bytes) -> str:
    """Recover the correlation id from a request the grammar rejected.

    ⟦S3.4⟧ Found by the invalid-choice test, and it is a channel defect rather
    than an approval one. `parse_request` validates the params before the loop
    has assigned `request_id`, so any params-level violation answered with the
    literal `"invalid"` — and a reply the supervisor is not waiting for is not a
    stray it ignores, it is `_fail("worker replied to an unknown request")` and
    the channel is gone. One malformed request would take the worker with it,
    mid-turn, with the turn left parked forever.

    Only the id is recovered, and only to address the refusal: everything else in
    a rejected request stays rejected. The supervisor still routes it exclusively
    to a request it actually issued, so echoing the id cannot create a
    correlation that did not already exist.
    """

    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return "invalid"
    if not isinstance(raw, dict):
        return "invalid"
    candidate = raw.get("request_id")
    if isinstance(candidate, str) and candidate:
        return candidate
    return "invalid"


def _submit_transport(
    transport: _TransportCalls,
    request: object,
    params: Mapping[str, object],
) -> bool:
    """Hand one transport method to the off-loop runner, deadline included."""

    method = getattr(request, "method")
    request_id = getattr(request, "request_id")
    if method == "telegram.send":
        frozen = dict(params)
        return transport.submit(
            request_id,
            lambda: telegram_transport.send(
                frozen, timeout=telegram_transport.poll_timeout(0)
            ),
        )
    frozen = dict(params)
    long_poll = int(frozen["timeout_seconds"])
    return transport.submit(
        request_id,
        lambda: telegram_transport.poll(
            frozen, timeout=telegram_transport.poll_timeout(long_poll)
        ),
    )


class _TurnUnknown(RuntimeError):
    """A turn method named an operation that is not the open turn."""


def _require_open(turn: Turn | None, params: Mapping[str, object]) -> Turn:
    if turn is None or turn.operation_id != str(params["operation_id"]):
        raise _TurnUnknown("no such open turn")
    return turn


def _begin_turn(
    *,
    ledger: OperationLedger,
    writer: _FrameWriter,
    open_turn: Turn | None,
    params: Mapping[str, object],
    runner: Callable[[Mapping[str, object], object], Mapping[str, object]],
    heartbeat_interval: float,
) -> tuple[Turn | None, dict[str, object]]:
    """Record the turn durably first, then start the thread that runs it.

    The ledger append is fsynced before `begin` returns, so the `accepted` the
    supervisor sees is already a durable fact — which is what makes the crash
    the dedup probe stages meaningful. A replay of the same operation with the
    same request digest is `duplicate` and starts nothing.
    """

    operation_id = str(params["operation_id"])
    if open_turn is not None and not open_turn.done.is_set():
        if open_turn.operation_id == operation_id:
            return open_turn, {"state": "duplicate"}
        raise OperationConflict("a turn is already open")
    state = ledger.begin(operation_id, "turn", str(params["request_digest"]))
    if state == "duplicate":
        return open_turn, {"state": "duplicate"}
    emitter = TurnEmitter(operation_id, writer.write)

    def finish(outcome: str, digest: str) -> None:
        ledger.finish(operation_id, outcome, digest)

    turn = Turn(
        operation_id=operation_id,
        request=params["request"],
        emitter=emitter,
        runner=runner,
        finish=finish,
        heartbeat_interval=heartbeat_interval,
    )
    turn.start()
    return turn, {"state": state}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-descriptor", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        descriptor = SlotInterpreterDescriptor.load(arguments.v2_descriptor)
    except ProtocolViolation:
        return 4
    return serve(descriptor, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer)
