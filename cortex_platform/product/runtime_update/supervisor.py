"""Controller-side supervisor for the private runtime worker protocol.

⟦AMD-4⟧ replaces the v2 channel's `select`-then-`readline` with a reader
thread. The old shape could only ever be one-in-flight, and it could not
receive a frame it had not asked for — which a turn needs, twice over: it
emits events while it runs, and it has to be steerable while it is parked.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import select
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping

from ..secrets import SecretResolver
from .worker import PROTOCOL_VERSION
from .worker_payload.cortex_worker.turn import EVENT_FINISH, EVENT_HEARTBEAT
from .worker_protocol import (
    FRAME_REPLY,
    PROTOCOL_V2,
    ProtocolViolation,
    SlotInterpreterDescriptor,
    WorkerEvent,
    WorkerResponse,
    parse_frame,
)

if TYPE_CHECKING:  # pragma: no cover - `sandbox` imports this module at runtime
    from .sandbox import SandboxLaunch


class WorkerError(RuntimeError):
    """Base class for sanitized worker failures."""


class WorkerCrashed(WorkerError):
    """The candidate process exited before a valid response."""


class WorkerProtocolError(WorkerError):
    """The candidate violated the authenticated stdio protocol."""


class WorkerTurnUncertain(WorkerError):
    """The channel went silent while a turn was open.

    Terminal for the attempt. The operator's retry is a new attempt id and
    therefore a new operation, never a silent re-execution of this one.
    """


class WorkerSupervisor:
    """Run one candidate using a private authenticated newline-JSON channel."""

    def __init__(
        self,
        *,
        python_executable: Path,
        candidate_root: Path,
        state_root: Path,
        worker_entrypoint: str,
    ) -> None:
        self.python_executable = python_executable.resolve(strict=True)
        self.candidate_root = candidate_root.resolve(strict=True)
        self.state_root = state_root.resolve(strict=True)
        # The verifier tells the executor which file to run. Deriving it inside
        # the worker would let the slot choose its own entrypoint; the release
        # manifest is the only thing entitled to name it.
        self.worker_entrypoint = worker_entrypoint
        self._token = secrets.token_urlsafe(32)
        self._process: subprocess.Popen[str] | None = None

    def _environment(self) -> dict[str, str]:
        temporary = self.state_root / ".tmp"
        temporary.mkdir(exist_ok=True, mode=0o700)
        return {
            "HOME": str(self.state_root),
            "TMPDIR": str(temporary),
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "CORTEX_WORKER_TOKEN": self._token,
        }

    def start(self) -> None:
        if self._process is not None:
            return
        bootstrap = Path(__file__).with_name("worker.py")
        self._process = subprocess.Popen(
            [
                str(self.python_executable),
                "-I",
                str(bootstrap),
                str(self.candidate_root),
                str(self.state_root),
                "--worker-entrypoint",
                self.worker_entrypoint,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self._environment(),
            cwd=self.state_root,
            close_fds=True,
        )
        try:
            result = self.request("__hello__", {}, timeout=3)
            if result != {"protocol": PROTOCOL_VERSION}:
                raise WorkerProtocolError("worker handshake failed")
        except BaseException:
            self.close(force=True)
            raise

    def request(self, method: str, params: Mapping[str, object], *, timeout: float) -> object:
        process = self._process
        if process is None:
            raise WorkerCrashed("worker is not running")
        if process.poll() is not None:
            raise WorkerCrashed("worker exited")
        assert process.stdin is not None and process.stdout is not None
        request_id = uuid.uuid4().hex
        request = {
            "protocol": PROTOCOL_VERSION,
            "request_id": request_id,
            "token": self._token,
            "method": method,
            "params": dict(params),
        }
        try:
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerCrashed("worker exited") from exc
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            if process.poll() is not None:
                raise WorkerCrashed("worker exited")
            raise WorkerProtocolError("worker response timed out")
        line = process.stdout.readline()
        if not line:
            raise WorkerCrashed("worker exited")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerProtocolError("worker returned invalid JSON") from exc
        if (
            not isinstance(response, dict)
            or set(response) != {"protocol", "request_id", "ok", "result", "error"}
            or response.get("protocol") != PROTOCOL_VERSION
            or response.get("request_id") != request_id
            or type(response.get("ok")) is not bool
        ):
            raise WorkerProtocolError("worker response identity mismatch")
        if not response["ok"]:
            error = response.get("error")
            raise WorkerProtocolError(error if isinstance(error, str) else "worker_error")
        return response["result"]

    def close(self, *, force: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and not force:
            try:
                self._process = process
                self.request("__shutdown__", {}, timeout=1)
            except WorkerError:
                pass
            finally:
                self._process = None
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> WorkerSupervisor:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


@dataclass
class _Pending:
    """One request waiting for the reply the reader thread will hand it."""

    arrived: threading.Event = field(default_factory=threading.Event)
    frame: WorkerResponse | None = None


@dataclass
class _OpenTurn:
    """One turn's inbox, and the sequence the next event must carry."""

    operation_id: str
    events: "queue.Queue[WorkerEvent]" = field(default_factory=queue.Queue)
    expected_sequence: int = 0


class WorkerSupervisorV2:
    """Launch and authenticate one self-measuring worker v2 process."""

    #: Handshake methods keep S3.2's bound; a turn is allowed to take longer to
    #: be *accepted*, and no bound at all to run.
    HANDSHAKE_TIMEOUT = 3.0
    TURN_BEGIN_TIMEOUT = 30.0
    #: ⟦AMD-4⟧ This judges the channel, never the turn. The worker heartbeats
    #: every 5 s from a thread that is not the turn thread, so twelve missed
    #: heartbeats means the process or the pipe is gone — not that the model is
    #: slow. `turn.cancel` is the operator's bound on length.
    TURN_LIVENESS_TIMEOUT = 60.0
    #: The stderr drain is bounded because an unbounded one is a disk-fill
    #: primitive owned by the slot.
    STDERR_LOG_LIMIT = 1 << 20
    #: How many request ids and retired operation ids are remembered so a frame
    #: that arrives just after its deadline can be told from an uncorrelated
    #: one. Bounded because the slot chooses how many frames it sends.
    LATE_FRAME_MEMORY = 1024

    def __init__(
        self,
        descriptor_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
        sandbox: "SandboxLaunch | None" = None,
    ) -> None:
        self.descriptor_path = descriptor_path.resolve(strict=True)
        self.descriptor = SlotInterpreterDescriptor.load(self.descriptor_path)
        # ⟦S3.4/D3⟧ The single hook the OS boundary needs in this file: a probed
        # profile wraps the argv below. `None` means the caller has taken
        # responsibility for the boundary (the product path never does — see
        # `ManagedHermesBackend`), so the launch shape is otherwise unchanged.
        self.sandbox = sandbox
        self._token = secrets.token_urlsafe(32)
        self._process: subprocess.Popen[str] | None = None
        self.identity: dict[str, object] = {}
        # Set once per launch, so a capability proven against one process can be
        # refused the moment a different one is serving.
        self.worker_launch_id = uuid.uuid4().hex
        self._environment_override = dict(environment) if environment is not None else None
        self._state = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._turns: dict[str, _OpenTurn] = {}
        self._reader: threading.Thread | None = None
        self._drain: threading.Thread | None = None
        self._closed = threading.Event()
        self._failure: str | None = None
        self._last_frame = time.monotonic()
        # ⟦AMD-4⟧ Two kinds of frame the worker is entitled to send after this
        # side stopped waiting for them: the reply to a request whose bound
        # expired, and an event for a turn that has been closed. Both used to
        # end the channel. They are dropped and counted instead — the counters
        # are what makes the drop observable rather than invisible.
        self._timed_out: OrderedDict[str, None] = OrderedDict()
        self._retired_turns: OrderedDict[str, None] = OrderedDict()
        self._late_replies_dropped = 0
        self._late_events_dropped = 0

    @property
    def process(self) -> subprocess.Popen[str] | None:
        return self._process

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def failure(self) -> str | None:
        """The sticky reason this channel stopped serving, if it has.

        Public because `alive` is `poll() is None` and nothing else: a failed
        channel whose process is still up passes `alive` forever, so a caller
        deciding whether to reuse this object has to be able to ask.
        """

        with self._state:
            return self._failure

    @property
    def failed(self) -> bool:
        with self._state:
            return self._failure is not None

    @property
    def late_replies_dropped(self) -> int:
        with self._state:
            return self._late_replies_dropped

    @property
    def late_events_dropped(self) -> int:
        with self._state:
            return self._late_events_dropped

    def _remember_locked(self, memory: "OrderedDict[str, None]", key: str) -> None:
        memory[key] = None
        memory.move_to_end(key)
        while len(memory) > self.LATE_FRAME_MEMORY:
            memory.popitem(last=False)

    def _environment(self) -> dict[str, str]:
        self.descriptor.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.descriptor.state_dir / ".tmp"
        temporary.mkdir(exist_ok=True, mode=0o700)
        home = self.descriptor.state_dir / HERMES_HOME_DIRNAME
        home.mkdir(exist_ok=True, mode=0o700)
        # ⟦AMD-2⟧ HERMES_HOME is pinned by the same `|` merge as the token, in
        # BOTH branches. Unset is the case that reproduces by default: the fork
        # falls back to `Path.home()/'.hermes'`, which is inside the sole
        # writable root and denied by nothing, while all three layers guard an
        # empty `hermes-home`. A caller-supplied value is overridden here rather
        # than forwarded, and the worker refuses outright if it ever sees one
        # that disagrees — the product owns this directory.
        pinned = {
            "CORTEX_WORKER_TOKEN": self._token,
            "HERMES_HOME": str(home),
            # ⟦P5.4d / F10⟧ Without this the approval callback the product
            # installs is never called. `tools/approval.py` decides who to ask
            # from the environment: outside an interactive CLI and outside a
            # gateway session it takes the "non-interactive" branch, which
            # AUTO-APPROVES every dangerous command and logs a warning nobody
            # reads. So the whole S3.4 vocabulary -- `decision.required`,
            # `waiting_for_decision`, `turn.resolve` -- was unreachable in the
            # managed worker, and the only thing standing between a model and
            # `rm -rf` was the seatbelt's `(deny process-fork)`. Observed on a
            # real turn: the tool ran unasked and failed on the sandbox.
            #
            # `HERMES_INTERACTIVE` rather than `HERMES_GATEWAY_SESSION`: the
            # gateway branch submits to the fork's own pending-approval store,
            # which nothing in this product drains, and never reaches the
            # callback. Pinned rather than allowlisted for the same reason as
            # HERMES_HOME -- it is the product's decision about its own
            # boundary, not an operator setting, and a caller that could unset
            # it could turn the gate off.
            "HERMES_INTERACTIVE": "1",
        }
        if self._environment_override is not None:
            return dict(self._environment_override) | pinned
        return {
            "HOME": str(self.descriptor.state_dir),
            "TMPDIR": str(temporary),
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
        } | pinned

    def start(self) -> None:
        if self._process is not None:
            return
        environment = self._environment()
        stderr_log = self.descriptor.state_dir / "worker.stderr.log"
        # The attested entrypoint, exactly as S3.0's residual demanded: the file
        # the slot's `content_tree_sha256` witnesses is the file that runs.
        # `-m cortex_platform...` could not survive the move to a per-slot
        # interpreter anyway — the release's own CPython has no `cortex_platform`
        # to import, and `-I` removes the PYTHONPATH that would have papered
        # over it.
        argv = [
            str(self.descriptor.interpreter_path),
            "-I",
            str(
                self.descriptor.slot_path
                / "content"
                / self.descriptor.worker_entrypoint
            ),
            "--v2-descriptor",
            str(self.descriptor_path),
        ]
        if self.sandbox is not None:
            argv = self.sandbox.wrap(argv)
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # ⟦AMD-5⟧ An unread PIPE is a deadlock the slot controls: fill the
            # pipe buffer and the worker blocks forever inside a write nobody
            # will ever read. Drained, bounded, into the state dir.
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            cwd=self.descriptor.state_dir,
            close_fds=True,
        )
        self._closed.clear()
        self._last_frame = time.monotonic()
        # ⟦AMD-5⟧ The `Popen` travels as a thread argument rather than being
        # re-read from `self._process`: `close()` clears that attribute before
        # it joins these threads, and `start()`'s own `except BaseException:
        # close(force=True)` on an identity or health failure is exactly that
        # race. A thread that raised on the way in skipped its `finally`.
        process = self._process
        self._reader = threading.Thread(
            target=self._read_frames,
            args=(process,),
            name="cortex-worker-reader",
            daemon=True,
        )
        self._reader.start()
        self._drain = threading.Thread(
            target=self._drain_stderr,
            args=(process, stderr_log),
            name="cortex-worker-stderr",
            daemon=True,
        )
        self._drain.start()
        try:
            result = self.request("identity.measure", {})
            if not isinstance(result, dict) or not self._matches_expected_identity(result):
                raise WorkerProtocolError("worker identity_mismatch")
            self.identity = dict(result)
            health = self.request("health.check", {})
            if (
                not isinstance(health, dict)
                or set(health) != {"healthy", "protocol", "ledger_open", "quarantined"}
                or health.get("healthy") is not True
                or health.get("protocol") != PROTOCOL_V2
                or health.get("ledger_open") is not True
                or type(health.get("quarantined")) is not bool
            ):
                raise WorkerProtocolError("worker ledger_unavailable")
        except BaseException:
            self.close(force=True)
            raise

    def _read_frames(self, process: subprocess.Popen[str]) -> None:
        """Own the worker's stdout for the process's whole life.

        Everything the worker says arrives here first, is parsed by the closed
        frame grammar, and is then routed — replies by `request_id`, events by
        `operation_id`. A frame that does not parse, or an event whose sequence
        is not the next one, ends the channel rather than being skipped: a gap
        in a turn's stream is indistinguishable from a lost one.

        The whole body is inside the `try`, including the stream check, so the
        `finally` always releases whoever is waiting in `request()`. It was an
        `assert` before, which under `python -O` was an `AttributeError` on the
        next line instead.
        """

        try:
            if process.stdout is None:
                return
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    frame = parse_frame(line.encode("utf-8"))
                except ProtocolViolation as exc:
                    self._fail(f"worker frame rejected: {exc}")
                    return
                self._last_frame = time.monotonic()
                if isinstance(frame, WorkerResponse):
                    self._route_reply(frame)
                    continue
                if not self._route_event(frame):
                    return
        except (OSError, ValueError):
            pass
        finally:
            self._fail(self._failure or "worker exited")

    def _route_reply(self, frame: WorkerResponse) -> None:
        with self._state:
            pending = self._pending.pop(frame.request_id, None)
            late = False
            if pending is None and frame.request_id in self._timed_out:
                del self._timed_out[frame.request_id]
                self._late_replies_dropped += 1
                late = True
        if pending is None:
            if not late:
                # A reply to a request nobody ever issued is a protocol fault,
                # not a stray to ignore: correlation is the invariant. A reply
                # to one whose bound expired is merely late, and killing the
                # channel over it wedges a healthy worker.
                self._fail("worker replied to an unknown request")
            return
        pending.frame = frame
        pending.arrived.set()

    def _route_event(self, frame: WorkerEvent) -> bool:
        with self._state:
            turn = self._turns.get(frame.operation_id)
            if turn is None:
                if frame.operation_id in self._retired_turns:
                    # The turn is over on this side and the worker's last frames
                    # are still in flight. Dropping them loses nothing: nobody
                    # is reading that inbox.
                    self._late_events_dropped += 1
                    return True
                self._failure = "worker emitted an event for no open turn"
                return False
            if frame.sequence != turn.expected_sequence:
                self._failure = "worker event sequence is not contiguous"
                return False
            turn.expected_sequence += 1
        turn.events.put(frame)
        return True

    def _fail(self, reason: str) -> None:
        with self._state:
            if self._failure is None:
                self._failure = reason
            pending = list(self._pending.values())
            self._pending.clear()
        self._closed.set()
        for waiter in pending:
            waiter.arrived.set()

    def _drain_stderr(self, process: subprocess.Popen[str], path: Path) -> None:
        written = 0
        try:
            if process.stderr is None:
                return
            with open(path, "a", encoding="utf-8") as log:
                for line in process.stderr:
                    if written >= self.STDERR_LOG_LIMIT:
                        continue
                    written += len(line)
                    log.write(line)
                    log.flush()
        except (OSError, ValueError):
            pass

    def _matches_expected_identity(self, identity: Mapping[str, object]) -> bool:
        return (
            identity.get("worker_protocol") == PROTOCOL_V2
            and identity.get("artifact_digest") == self.descriptor.expected_artifact_digest
            and identity.get("content_tree_sha256")
            == self.descriptor.expected_content_tree_sha256
            and identity.get("manifest_sha256") == self.descriptor.expected_manifest_sha256
            and identity.get("slot_id") == self.descriptor.slot_id
            and identity.get("state_generation_id")
            == self.descriptor.state_generation_id
            and identity.get("release_id") == self.descriptor.release_id
            # Equality, not an isinstance check. The predecessor accepted any
            # string here, which is how a path-and-version fingerprint could be
            # reported for four slices without ever being compared.
            and identity.get("interpreter_sha256")
            == self.descriptor.expected_interpreter_sha256
        )

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> object:
        process = self._process
        if process is None or process.poll() is not None:
            raise WorkerCrashed("worker is not running")
        assert process.stdin is not None
        request_id = uuid.uuid4().hex
        pending = _Pending()
        with self._state:
            if self._failure is not None:
                raise WorkerProtocolError(self._failure)
            self._pending[request_id] = pending
        request = {
            "protocol": PROTOCOL_V2,
            "request_id": request_id,
            "token": self._token,
            "method": method,
            "params": dict(params),
        }
        try:
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            with self._state:
                self._pending.pop(request_id, None)
            raise WorkerCrashed("worker exited") from exc
        if not pending.arrived.wait(
            self.HANDSHAKE_TIMEOUT if timeout is None else timeout
        ):
            with self._state:
                self._pending.pop(request_id, None)
                # The worker may still answer this. Remembered so `_route_reply`
                # can tell a late reply from an uncorrelated one.
                self._remember_locked(self._timed_out, request_id)
            if process.poll() is not None:
                raise WorkerCrashed("worker exited")
            raise WorkerProtocolError("worker response timed out")
        frame = pending.frame
        if frame is None:
            if process.poll() is not None:
                raise WorkerCrashed("worker exited")
            raise WorkerProtocolError(self._failure or "worker channel closed")
        if frame.error is not None:
            raise WorkerProtocolError(frame.error.category)
        return frame.result

    # -- turns ------------------------------------------------------------

    def begin_turn(
        self,
        operation_id: str,
        request_digest: str,
        request: Mapping[str, object],
    ) -> str:
        """Open a turn and register its inbox before the first event can arrive.

        Registration happens first on purpose: the worker starts the turn thread
        inside `turn.begin`, so an event can be on the wire before the reply is.
        """

        with self._state:
            if operation_id in self._turns:
                raise WorkerProtocolError("operation is already open")
            self._turns[operation_id] = _OpenTurn(operation_id)
        try:
            result = self.request(
                "turn.begin",
                {
                    "operation_id": operation_id,
                    "request_digest": request_digest,
                    "request": dict(request),
                },
                timeout=self.TURN_BEGIN_TIMEOUT,
            )
        except BaseException:
            self._abandon_turn(operation_id)
            raise
        if not isinstance(result, dict) or result.get("state") not in {
            "accepted",
            "duplicate",
        }:
            self._abandon_turn(operation_id)
            raise WorkerProtocolError("worker returned an invalid turn state")
        return str(result["state"])

    def turn_events(self, operation_id: str) -> Iterator[Mapping[str, object]]:
        """Yield a turn's events until it finishes, or the channel goes quiet."""

        with self._state:
            turn = self._turns.get(operation_id)
        if turn is None:
            raise WorkerProtocolError("no such open turn")
        finished = False
        try:
            while True:
                try:
                    frame = turn.events.get(timeout=0.25)
                except queue.Empty:
                    if time.monotonic() - self._last_frame > self.TURN_LIVENESS_TIMEOUT:
                        raise WorkerTurnUncertain("worker channel went silent")
                    if self._closed.is_set() and turn.events.empty():
                        raise WorkerTurnUncertain(
                            self._failure or "worker exited during a turn"
                        )
                    continue
                event = frame.event
                kind = str(event.get("kind", ""))
                if kind == EVENT_HEARTBEAT:
                    continue
                # Set before the yield, not after: a consumer that breaks out of
                # the loop on the finish event has not abandoned anything, and
                # must not be charged a `turn.cancel` for it.
                finished = kind == EVENT_FINISH
                yield event
                if finished:
                    return
        finally:
            if finished:
                self.close_turn(operation_id)
            else:
                self._abandon_turn(operation_id)

    def resolve_turn(self, operation_id: str, decision: Mapping[str, object]) -> bool:
        """True when the worker parked, or is about to park, on this decision."""

        result = self.request(
            "turn.resolve",
            {"operation_id": operation_id, "decision": dict(decision)},
        )
        if not isinstance(result, dict) or type(result.get("delivered")) is not bool:
            raise WorkerProtocolError("worker returned an invalid resolve result")
        return result["delivered"]

    def cancel_turn(self, operation_id: str) -> None:
        self.request("turn.cancel", {"operation_id": operation_id})

    def close_turn(self, operation_id: str) -> None:
        with self._state:
            if self._turns.pop(operation_id, None) is not None:
                self._remember_locked(self._retired_turns, operation_id)

    def _abandon_turn(self, operation_id: str) -> None:
        """Walk away from a turn this side believes it opened.

        ⟦AMD-4⟧ The worker is still inside it, possibly parked in an approval
        callback nobody is left to answer, so it is told to stop before its
        inbox disappears. Best effort: the channel may already be gone, which
        is often exactly why the turn is being abandoned.
        """

        with self._state:
            open_turn = operation_id in self._turns
        if open_turn:
            try:
                self.cancel_turn(operation_id)
            except (WorkerError, OSError):
                pass
        self.close_turn(operation_id)

    # -- lifecycle ---------------------------------------------------------

    def close(self, *, force: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and not force:
            try:
                self._process = process
                self.request("shutdown", {}, timeout=1)
            except WorkerError:
                pass
            finally:
                self._process = None
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._closed.set()
        for thread in (self._reader, self._drain):
            if thread is not None:
                thread.join(timeout=2)
        self._reader = None
        self._drain = None
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        with self._state:
            self._turns.clear()
            self._pending.clear()

    def __enter__(self) -> WorkerSupervisorV2:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# ⟦AMD-2⟧ The worker's launch environment.
#
# The fork configures itself from process environment, which makes environment
# the zero-patch transport for provider config — and the reason this has to be a
# positive allowlist rather than a filter. A denylist over the parent's
# environment would ship every variable nobody thought to name; this ships
# exactly the variables named here and nothing else, the same discipline
# `distribution/lifecycle.py`'s `control_environment` and `web_environment` use.
# ---------------------------------------------------------------------------

#: The keys the fork reads a credential from. A resolved secret may land in one
#: of these and nowhere else.
CREDENTIAL_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
)
#: Pinned to product values in the same allowlist. Left unset these default
#: inside the fork; left inheritable they are a redirect primitive — a base URL
#: is where a credential gets sent.
BASE_URL_KEYS = (
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    # Read by `cortex_worker.telegram`, not by the fork. Its production default
    # is the real `https://api.telegram.org`; the key exists so a test or an
    # acceptance can point the transport at a loopback stand-in, and the worker
    # module refuses plain HTTP for anything but loopback. No operator-facing
    # configuration reaches it.
    "TELEGRAM_API_BASE_URL",
)
#: Credentials the product's OWN worker modules read. Kept apart from
#: `CREDENTIAL_KEYS`, whose invariant is that the fork's `auth.py` actually
#: reads every name in it -- `cortex_worker.telegram` is product code, and the
#: fork's own Telegram platform never enters this path (D-P5-1).
TRANSPORT_CREDENTIAL_KEYS = ("TELEGRAM_BOT_TOKEN_RESEARCH",)
#: Provider/iteration selection. Deliberately excludes `HERMES_YOLO_MODE`, which
#: `tools/approval.py` freezes at import into `_YOLO_MODE_FROZEN`: setting it
#: would disable the approval callback S3.4 is built on, from an environment
#: variable, before any policy could see it.
SETTING_KEYS = ("HERMES_INFERENCE_PROVIDER", "HERMES_MAX_ITERATIONS")

HERMES_HOME_DIRNAME = "hermes-home"


class WorkerEnvironmentError(RuntimeError):
    """The requested worker environment is not expressible in the allowlist."""


def worker_environment(
    *,
    state_dir: Path,
    token: str,
    secret_refs: Mapping[str, str] | None = None,
    credential_bindings: Mapping[str, str] | None = None,
    resolver: SecretResolver | None = None,
    base_urls: Mapping[str, str] | None = None,
    settings: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the worker's whole environment, resolving secrets in the parent.

    `secret_refs` is the config's `{alias: reference}` map; `credential_bindings`
    says which allowlisted key each alias lands in. Two maps rather than one
    because `SecretResolver` refuses an alias that looks like a credential key —
    aliases are logical names, and the binding from a logical name to a provider
    key is the product's decision, not the secret store's.

    Only aliases named in `credential_bindings` are resolved, so an unrelated
    `secret_refs` entry never reaches this process's memory, let alone the
    worker's.
    """

    state_dir = Path(state_dir)
    temporary = state_dir / ".tmp"
    home = state_dir / HERMES_HOME_DIRNAME
    for directory in (state_dir, temporary, home):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "HOME": str(state_dir),
        "TMPDIR": str(temporary),
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "CORTEX_WORKER_TOKEN": token,
        # Product-created, product-owned, and asserted inert by the worker
        # before it imports the fork. Persists across worker restarts within one
        # (release_id, generation_id) and never across an upgrade.
        "HERMES_HOME": str(home),
    }
    for key, value in sorted((base_urls or {}).items()):
        if key not in BASE_URL_KEYS:
            raise WorkerEnvironmentError(f"{key} is not an allowlisted base URL")
        environment[key] = str(value)
    for key, value in sorted((settings or {}).items()):
        if key not in SETTING_KEYS:
            raise WorkerEnvironmentError(f"{key} is not an allowlisted setting")
        environment[key] = str(value)
    bindings = dict(credential_bindings or {})
    if bindings:
        if resolver is None:
            raise WorkerEnvironmentError("a resolver is required to bind credentials")
        references = dict(secret_refs or {})
        used: set[str] = set()
        for alias, key in sorted(bindings.items()):
            if key not in CREDENTIAL_KEYS and key not in TRANSPORT_CREDENTIAL_KEYS:
                raise WorkerEnvironmentError(f"{key} is not an allowlisted credential key")
            if key in used:
                raise WorkerEnvironmentError(f"{key} is bound more than once")
            if alias not in references:
                raise WorkerEnvironmentError(f"secret {alias!r} has no reference")
            used.add(key)
            # `reveal()` is the single greppable disclosure point, and this is
            # the last place the value exists as a Python string before it is
            # handed to `Popen(env=...)`.
            environment[key] = resolver.resolve(alias, references[alias]).reveal()
    return environment


def spawn_v2(descriptor_path: Path) -> WorkerSupervisorV2:
    """Construct a worker v2 supervisor; the context manager starts it."""

    return WorkerSupervisorV2(descriptor_path)
