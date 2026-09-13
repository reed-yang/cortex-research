"""The production `_HermesTransportRPC`: the gate in front of the worker.

`HermesTelegramClient` has always talked to an injected RPC object and there
has never been a production one -- the Protocol had no implementation in the
tree at all. This is it, and it is not a bare forwarder: D-P5-2 requires the
durable transport gate to be consulted before any outbound send and, per
⟦AMD-4⟧, before every `telegram.poll`, so the gate check lives at the one
place every transport frame passes through rather than at each call site that
might forget it.

`telegram.capabilities` is deliberately NOT gated. The window procedure's
pre-window assertion asks the active slot whether it can speak
`cortex.telegram.transport/1` *before* a window is opened, precisely so a
packaging gap is found outside the window rather than during it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Protocol

from ..control import ControlStore
from ..redaction import redact
from ..runtime_update.supervisor import WorkerProtocolError
from ..runtime_update.worker_protocol import (
    TELEGRAM_MAX_POLL_SECONDS,
    TELEGRAM_POLL_TIMEOUT_MARGIN_SECONDS,
)

#: Only these two frames are refused when the gate is closed.
GATED_METHODS = frozenset({"telegram.send", "telegram.poll"})

#: How much longer than the worker the PRODUCT waits for one transport frame.
#: Without it both sides derived the same 40.0 s for a 30 s long poll, so the
#: product abandoned the frame at the instant the worker was still going to
#: answer it -- and the late reply used to fail the whole managed-worker
#: channel. The slack belongs here rather than in the worker's own margin: the
#: worker module is byte-pinned into a release, so shortening its deadline
#: would force a repackage and would make an ordinary empty long poll race its
#: own socket timeout.
TELEGRAM_FRAME_DEADLINE_SLACK_SECONDS = 5.0
#: What the pre-window assertion expects back from the active slot.
TELEGRAM_TRANSPORT_PROTOCOL = "cortex.telegram.transport/1"

#: How long a `telegram.send` may wait for the shared transport line before the
#: daemon refuses its own frame. Deliberately far shorter than one long poll:
#: the drain runs on the reconcile thread, which also owns `close_window`
#: (⟦F-B4⟧ measured a 49 s pass blocking `stop_poller` for 49 s), so a send that
#: waited out a 30 s poll would put that stall back. A refusal here is provably
#: pre-socket, the next tick retries it, and by then the poller is short-polling.
TRANSPORT_SEND_WAIT_SECONDS = 3.0
#: How long a `telegram.poll` may wait for the line. A send holds it for one
#: frame, and one frame is at most the send's own RPC deadline
#: (`rpc_timeout_seconds`, 30 s in every projection this product mints); this
#: only expires if the worker is wedged. ⟦P5.6⟧ Strictly above that hold by
#: the same slack the frame deadline carries over the worker's own -- at
#: equality the two expired on the same tick and a poll gave up on a line that
#: was about to be free (batch D D-6). An expiry here is `TransportLineBusy`,
#: which the poller counts on its own and never as a strike.
TRANSPORT_POLL_WAIT_SECONDS = 30.0 + TELEGRAM_FRAME_DEADLINE_SLACK_SECONDS
#: ⟦P5.6⟧ How many poller failures are remembered with their message. A ring,
#: not a list: a poller failing in bursts for an hour is exactly the case this
#: exists for, and a status surface must not grow with it.
POLLER_FAILURE_LOG_LIMIT = 10

_log = logging.getLogger(__name__)
#: The `timeout_seconds` a poll asks for while outbound work is expected or
#: waiting. Not zero: an empty short poll plus the poller's idle delay is
#: already a ~2 s cycle, which bounds a send's wait well inside
#: `TRANSPORT_SEND_WAIT_SECONDS` at half the `getUpdates` rate of a busy loop.
SHORT_POLL_SECONDS = 1
#: How long an inbound update keeps the poller yielding the line. An answer is
#: the expected consequence of a message and the turn that produces it takes
#: seconds to minutes. A turn that outlives this loses the fast path, not the
#: delivery: the first refused send re-arms the same signal.
OUTBOUND_EXPECTED_SECONDS = 120.0

#: Worker error categories that prove the frame never became a Bot API request.
#: `operation_conflict` is `serve.py::_submit_transport` declining to start a
#: second transport call -- raised BEFORE `cortex_worker.telegram.send` is
#: entered, so nothing was written and ⟦P5-01⟧ permits another attempt. ⟦G-1⟧
#: The same proof holds for a poll: no `getUpdates` was issued either, which is
#: why `request` types both gated methods from this one set.
REFUSED_BEFORE_SEND_CATEGORIES = frozenset({"operation_conflict"})


#: The logical alias the operator's config carries. `secret_refs` aliases are
#: logical names and `SecretResolver` refuses anything credential-shaped, so
#: `research_bot` is the name and the binding to a provider key is the
#: product's decision -- which is why the two maps stay separate (D-P5-4).
TELEGRAM_SECRET_ALIAS = "research_bot"
TELEGRAM_CREDENTIAL_KEY = "TELEGRAM_BOT_TOKEN_RESEARCH"


def telegram_frame_deadline(long_poll_seconds: int) -> float:
    """The product's deadline for one `telegram.poll` frame.

    Strictly greater than the worker's own `poll_timeout(long_poll_seconds)`
    by construction, so the product is never the first to give up.
    """

    return (
        float(long_poll_seconds)
        + TELEGRAM_POLL_TIMEOUT_MARGIN_SECONDS
        + TELEGRAM_FRAME_DEADLINE_SLACK_SECONDS
    )


def transport_credential_bindings(store: ControlStore) -> dict[str, str]:
    """Bind the bot token into the worker env only while the gate is `enable`.

    ⟦AMD-4⟧: the credential is scoped to the window, not to the process. A
    relaunch after the window closes builds its environment from this function
    again and gets an empty map, so the new worker cannot send even if
    something else in the product still thinks it may -- the token is simply
    not there.
    """

    if not store.telegram_dispatch_enabled():
        return {}
    return {TELEGRAM_SECRET_ALIAS: TELEGRAM_CREDENTIAL_KEY}


class TransportGateClosed(RuntimeError):
    """The durable transport gate does not authorize this frame."""

    def __init__(self) -> None:
        super().__init__("telegram_dispatch_disabled")


class TelegramRefusedBeforeSend(RuntimeError):
    """A send refused before one byte reached a socket.

    ⟦P5-01⟧ divides outbound failures into "provably nothing was written" and
    "unknown", and only the first may ever be re-sent. Both refusals this
    daemon can make itself are of the first kind -- the shared transport line
    was busy, or the worker answered `operation_conflict` because a poll was
    still open -- so they are one type, and `_deliver_frozen_event` releases the
    permit against it instead of taking the chunk to `manual_required`.

    Before this existed, `HermesTelegramClient.send_chunk` turned every RPC
    failure into `_HermesOutcomeUnknown`: a refusal the worker had proved was
    pre-socket arrived at Control as an unknown outcome, and the operator's
    answer needed a manual resolution to move at all.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TransportLineBusy(RuntimeError):
    """A poll could not take the shared transport line in time."""

    def __init__(self) -> None:
        super().__init__("transport_line_busy")


class TransportWorkerBusy(RuntimeError):
    """The worker's single transport slot is still held by an earlier frame.

    ⟦G-1⟧ The sibling of `TransportLineBusy`, one level down. `TransportLineBusy`
    is the PRODUCT's line held by a send; this is the WORKER's slot
    (`serve.py::_TransportCalls`) held by a call the product stopped waiting
    for. The two are the same kind of observation and neither is a fault:

    * `_submit_transport` refuses BEFORE `cortex_worker.telegram.poll` runs, so
      no `getUpdates` was issued, no update was consumed and no offset moved.
    * The refusal itself is proof the worker process, its pipe and its request
      loop are alive and answering -- the exact opposite of what the poller's
      consecutive-failure bound exists to detect.

    Reaching the poller as a bare `WorkerProtocolError` is how one abandoned
    frame used to spend the whole "the worker is gone" budget: five strikes in
    fifteen seconds, five restarts that cannot free a slot the poller does not
    own, and an `exhausted` window with no inbound loop until the product is
    restarted by hand.
    """

    def __init__(self) -> None:
        super().__init__("transport_worker_busy")


class TransportCallSerializer:
    """The one open transport call, shared by the poller and the drain.

    The certified worker allows exactly ONE transport call in flight
    (`serve.py::_submit_transport`) and the product had no matching rule. The
    poller thread parks a 30 s `telegram.poll`; the reconcile thread issues
    `telegram.send` for the reply straight into it. Against the loopback
    stand-in `getUpdates` answered instantly and the two never met -- against
    the real Bot API they met on the first reply this product ever tried to
    deliver, and the worker died naming the chunk in its refusal.

    Two mechanisms, one object. **Correctness is the line**: a send and a poll
    can never overlap, whatever the timing. **Latency is `poll_seconds`**: the
    poller asks for a SHORT poll while a send is waiting, while an inbound
    update is recent (an answer is what a message is for) and while the drain
    reports outbound work outstanding -- and for the full long poll only when
    there is nothing to send. The long poll is the right frame only when the
    line is otherwise idle.
    """

    def __init__(
        self,
        *,
        short_poll_seconds: int = SHORT_POLL_SECONDS,
        send_wait_seconds: float = TRANSPORT_SEND_WAIT_SECONDS,
        poll_wait_seconds: float = TRANSPORT_POLL_WAIT_SECONDS,
        outbound_expected_seconds: float = OUTBOUND_EXPECTED_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= int(short_poll_seconds) <= TELEGRAM_MAX_POLL_SECONDS:
            raise ValueError("short_poll_seconds is out of range")
        self._line = threading.Lock()
        self._state = threading.Lock()
        self._short = int(short_poll_seconds)
        self._send_wait = float(send_wait_seconds)
        self._poll_wait = float(poll_wait_seconds)
        self._expected = float(outbound_expected_seconds)
        self._monotonic = monotonic
        self._waiting = 0
        self._pending = False
        self._expect_until = 0.0
        #: Counts, never payloads -- what an operator reads to tell "the line is
        #: contended" from "the worker is gone".
        self.sends_refused = 0
        self.polls_shortened = 0

    # -- the latency half --------------------------------------------------

    def poll_seconds(self, long_poll_seconds: int) -> int:
        """The `timeout_seconds` the next poll should ask for."""

        with self._state:
            expecting = (
                self._waiting > 0
                or self._pending
                or self._monotonic() < self._expect_until
            )
            if not expecting:
                return int(long_poll_seconds)
            self.polls_shortened += 1
            return min(self._short, int(long_poll_seconds))

    def expect_send(self) -> None:
        """An inbound update arrived, so an outbound send is coming."""

        with self._state:
            self._expect_until = self._monotonic() + self._expected

    def outbound_pending(self, pending: bool) -> None:
        """The drain's own answer: is there still work in the ledger?"""

        with self._state:
            self._pending = bool(pending)

    def reset_window(self) -> None:
        """⟦P5.6⟧ A new window starts its counts at zero (batch D D-4).

        The serializer is built once with the RPC and outlives every window,
        and so does the drain it answers for (built once per daemon, never
        reset: its `delivered` and `categories` are daemon-lifetime ledger
        state). Without this the line's counters were daemon-lifetime too and
        carried a previous window's `sends_refused` into the next one; the
        runbook reads the line's counters from zero per window and the
        drain's as deltas. Counters and the outbound expectation are reset;
        `_waiting` is live state owned by a thread inside `_acquire_for_send`
        and is not.
        """

        with self._state:
            self.sends_refused = 0
            self.polls_shortened = 0
            self._pending = False
            self._expect_until = 0.0

    # -- the correctness half ----------------------------------------------

    @contextmanager
    def hold(self, method: str) -> Iterator[None]:
        """Hold the line for one transport frame, or refuse it."""

        if method == "telegram.send":
            self._acquire_for_send()
        elif not self._line.acquire(timeout=self._poll_wait):
            raise TransportLineBusy
        try:
            yield
        finally:
            self._line.release()

    def _acquire_for_send(self) -> None:
        with self._state:
            self._waiting += 1
        try:
            acquired = self._line.acquire(timeout=self._send_wait)
        finally:
            with self._state:
                self._waiting -= 1
        if acquired:
            return
        with self._state:
            self.sends_refused += 1
            # A busy line IS evidence of outbound work, so the poller yields
            # from here on rather than waiting to be told again by a drain pass
            # whose first attempt has just been refused.
            self._expect_until = self._monotonic() + self._expected
        raise TelegramRefusedBeforeSend("transport_line_busy")

    def status(self) -> dict[str, object]:
        with self._state:
            return {
                "sends_refused": self.sends_refused,
                "polls_shortened": self.polls_shortened,
                "outbound_pending": self._pending,
                "sends_waiting": self._waiting,
            }


class TransportCapabilityUnavailable(RuntimeError):
    """The active slot's worker cannot speak the Telegram transport protocol."""

    #: A worker failure message is a fixed protocol string, but it is bounded
    #: anyway: this value is written to an on-disk record an operator reads.
    DETAIL_LIMIT = 200

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail[: self.DETAIL_LIMIT]


class _WorkerRequest(Protocol):
    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float | None = None
    ) -> object: ...


class GatedWorkerTransportRPC:
    """Forward transport frames to one worker, gate-checked and serialized.

    Both fences are here for the same reason the gate check is: this is the one
    place every transport frame passes through, and a rule enforced at each
    call site is a rule one call site forgets. `telegram.capabilities` is
    deliberately outside BOTH -- the worker answers it inline on its request
    loop, never through its single open transport call, so it cannot contend
    with a poll and has nothing to wait for. The line itself is a plain
    non-reentrant lock: nothing called from inside a held frame may take it.
    """

    def __init__(
        self,
        *,
        supervisor: _WorkerRequest,
        store: ControlStore,
        serializer: TransportCallSerializer | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._store = store
        self.serializer = (
            TransportCallSerializer() if serializer is None else serializer
        )

    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> object:
        if method not in GATED_METHODS:
            return self._supervisor.request(method, dict(params), timeout=timeout)
        if not self._store.telegram_dispatch_enabled():
            # Fails closed and fails *before* the frame is written, so a
            # refused send is `rpc_not_started` rather than an unknown
            # outcome: nothing reached the socket.
            raise TransportGateClosed
        with self.serializer.hold(method):
            try:
                return self._supervisor.request(method, dict(params), timeout=timeout)
            except WorkerProtocolError as exc:
                if str(exc) in REFUSED_BEFORE_SEND_CATEGORIES:
                    # The worker declined to START the call. Typed here rather
                    # than in the client, because only this boundary knows the
                    # category came from a worker error frame at all. Both
                    # gated methods get a type of their own: ⟦P5-01⟧ needs the
                    # send's "provably nothing was written", and ⟦G-1⟧ needs the
                    # poll's "the worker is alive and its slot is still held".
                    # Every other category stays exactly what the worker said.
                    if method == "telegram.send":
                        raise TelegramRefusedBeforeSend(str(exc)) from None
                    raise TransportWorkerBusy from None
                raise


def assert_transport_capability(
    supervisor: _WorkerRequest, *, timeout: float = 5.0
) -> Mapping[str, object]:
    """The window procedure's pre-window assertion, as one callable.

    An S3.3-era worker does not have `telegram.capabilities` in its closed
    method set and answers `protocol_violation` ("worker method is invalid"),
    so a release that was never repackaged is caught here -- offline, outside
    any window -- instead of during the window with the operator's bot down.
    """

    try:
        raw = supervisor.request("telegram.capabilities", {}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - every worker failure is the same answer
        # The type is the category the caller branches on; the message is what
        # the operator needs, because "worker replied to an unknown request" and
        # "worker exited" are the same `WorkerProtocolError` and a very
        # different morning. An S3.3-era worker's refusal reaches the product
        # this way and nowhere else -- its `ProtocolViolation("worker method is
        # invalid")` is raised inside a process the product cannot see.
        raise TransportCapabilityUnavailable(
            type(exc).__name__, detail=str(exc)
        ) from None
    if not isinstance(raw, Mapping) or raw.get("protocol") != TELEGRAM_TRANSPORT_PROTOCOL:
        raise TransportCapabilityUnavailable(
            "protocol_mismatch",
            detail=str(raw.get("protocol")) if isinstance(raw, Mapping) else "",
        )
    return raw


class TelegramInboundPoller:
    """Drive `telegram.poll` while the gate is open, and stop when it closes.

    The loop checks the gate itself before every frame rather than relying on
    the RPC's refusal, so a `disable` ends the loop cleanly instead of turning
    into an exception per iteration. The RPC's check stays as the fence that
    catches any other caller.
    """

    #: Consecutive failures before the loop gives up and says so. A poller that
    #: retries an unrecoverable fault forever is as silent as one that dies.
    MAX_CONSECUTIVE_FAILURES = 5
    #: ⟦G-1⟧ How many consecutive `TransportWorkerBusy` answers are yielded to
    #: before the held slot is called a fault. Ten, with the backoff below, is
    #: about 150 s of yielding (1+2+4+8+16+30+30+30+30) -- comfortably longer
    #: than the frame the product just abandoned, and far shorter than a
    #: window. A bound is mandatory: without one a worker whose transport
    #: thread never returns would keep this loop "running" for ever with
    #: nothing in `last_error` for anyone to read.
    MAX_CONSECUTIVE_WORKER_BUSY = 10
    #: Doubling from the base, capped: a worker that is down must not be
    #: hammered, and a transient fault must not cost a whole minute.
    BACKOFF_BASE_SECONDS = 1.0
    BACKOFF_MAX_SECONDS = 30.0

    def __init__(
        self,
        *,
        rpc: GatedWorkerTransportRPC,
        store: ControlStore,
        handle_update: Callable[[Mapping[str, object]], object],
        long_poll_seconds: int = TELEGRAM_MAX_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        idle_delay: float = 1.0,
        serializer: TransportCallSerializer | None = None,
        on_failure: Callable[[Mapping[str, object]], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= long_poll_seconds <= TELEGRAM_MAX_POLL_SECONDS:
            raise ValueError("long_poll_seconds is out of range")
        self._rpc = rpc
        self._store = store
        self._handle_update = handle_update
        self._long_poll_seconds = long_poll_seconds
        # ⟦P5.6⟧ Whoever restarts this loop wants the failures of every
        # incarnation in one place; the supervisor passes its own ring here.
        self._on_failure = on_failure
        self._monotonic = monotonic
        # Taken from the RPC by default so the daemon wires nothing: the line
        # the poller must yield IS the line the RPC holds. An injected fake
        # without one keeps the fixed long poll, which is the old behaviour.
        self._serializer = (
            getattr(rpc, "serializer", None) if serializer is None else serializer
        )
        self._sleep = sleep
        self._idle_delay = idle_delay
        self._offset: int | None = None
        self._stop = threading.Event()
        self.polls = 0
        self.handled = 0
        #: Inspectable state, because "the loop is not running" used to be the
        #: same observation whether it stopped, the gate closed, or it died.
        self.failures = 0
        self.last_error: str | None = None
        #: ⟦P5.6⟧ The message behind `last_error`, bounded and redacted. On the
        #: mini a whole window of `WorkerProtocolError` bursts could not be told
        #: apart -- "worker response timed out" and `operation_conflict` are
        #: the same class and a very different network.
        self.last_error_detail: str | None = None
        self.failure_log: deque[dict[str, object]] = deque(
            maxlen=POLLER_FAILURE_LOG_LIMIT
        )
        #: ⟦P5.6⟧ Polls that could not take the shared line in time. Counted
        #: here and NOT as a failure (batch D D-6): a busy line is the outbound
        #: half holding it, which this branch introduced on purpose, and
        #: reading it as `poller_last_error` made cutover step 4b see a worker
        #: fault where the real state was "a send is slow".
        self.line_busy = 0
        #: ⟦G-1⟧ Polls the WORKER refused because its own transport slot was
        #: still held. Kept apart from `line_busy` on purpose: that counter
        #: reads "a send is slow" and the runbook treats it that way, while
        #: this one reads "a frame this product abandoned is still open inside
        #: the worker", which is the state an operator has to act on.
        self.worker_busy = 0
        self._worker_busy_since: float | None = None

    @property
    def worker_busy_seconds(self) -> float | None:
        """Elapsed time since the first unresolved worker-slot refusal."""

        since = self._worker_busy_since
        return None if since is None else max(0.0, self._monotonic() - since)

    @property
    def offset(self) -> int | None:
        return self._offset

    def _record_failure(
        self, exc: BaseException, *, poll_seconds: int, started: float
    ) -> None:
        entry: dict[str, object] = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": type(exc).__name__,
            "detail": redact(str(exc)),
            "poll_seconds": int(poll_seconds),
            "elapsed_ms": int((self._monotonic() - started) * 1000),
        }
        self.failures += 1
        self.last_error = str(entry["error"])
        self.last_error_detail = str(entry["detail"])
        self.failure_log.append(entry)
        _log.warning(
            "telegram poller failure: %s: %s (poll_seconds=%s elapsed_ms=%s)",
            entry["error"],
            entry["detail"],
            entry["poll_seconds"],
            entry["elapsed_ms"],
        )
        if self._on_failure is not None:
            try:
                self._on_failure(entry)
            except Exception:  # noqa: BLE001 - a sink must not kill the loop
                pass

    def stop(self) -> None:
        self._stop.set()

    def _backoff(self, consecutive: int) -> float:
        return min(
            self.BACKOFF_BASE_SECONDS * (2 ** (consecutive - 1)),
            self.BACKOFF_MAX_SECONDS,
        )

    def run(self, *, max_iterations: int | None = None) -> str:
        """Poll until the gate closes, the caller stops, or the bound is hit.

        Four terminal answers, and `failed` is a distinct one on purpose: the
        loop used to catch only `TransportGateClosed`, so a
        `WorkerProtocolError`, a `WorkerCrashed` or an unbound supervisor ended
        it permanently with the gate still open, no retry and nothing an
        operator could read to tell `stopped` from `crashed`.
        """

        iterations = 0
        consecutive = 0
        busy = 0
        while not self._stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                return "bounded"
            iterations += 1
            if not self._store.telegram_dispatch_enabled():
                return "gate_closed"
            # ⟦P5.5⟧ Asked once per iteration, never cached: the answer is
            # "does the daemon have anything to send", and a 30 s frame decided
            # 30 s ago is exactly how an answer computed in 8 s waited for a
            # poll that had nothing to report.
            seconds = self._poll_seconds()
            started = self._monotonic()
            try:
                raw = self._rpc.request(
                    "telegram.poll",
                    {
                        "offset": self._offset,
                        "timeout_seconds": seconds,
                    },
                    timeout=telegram_frame_deadline(seconds),
                )
            except TransportGateClosed:
                return "gate_closed"
            except TransportLineBusy:
                # ⟦P5.6⟧ Not a strike and not `last_error`: the line is held by
                # a send, which is the serialization working. Counted, and
                # asked again on the next iteration with a fresh `poll_seconds`.
                self.line_busy += 1
                _log.info(
                    "telegram poll yielded to a send holding the line (line_busy=%s)",
                    self.line_busy,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one bad frame is not fatal
                # ⟦G-1⟧ Branched here rather than in a clause of its own so
                # that the strike path below stays the only one: a held worker
                # slot is yielded to a bounded number of times and then becomes
                # an ordinary failure, with the same record and the same
                # escalation every other fault gets.
                if isinstance(exc, TransportWorkerBusy):
                    self.worker_busy += 1
                    if self._worker_busy_since is None:
                        self._worker_busy_since = started
                    busy += 1
                    if busy < self.MAX_CONSECUTIVE_WORKER_BUSY:
                        _log.info(
                            "telegram poll yielded to a held worker transport "
                            "slot (worker_busy=%s streak=%s)",
                            self.worker_busy,
                            busy,
                        )
                        self._sleep(self._backoff(busy))
                        continue
                    # The slot has been held for longer than any frame this
                    # poller issues. That is a fault, and it is recorded and
                    # escalated as one -- restarting the loop cannot free it,
                    # so the operator has to see it.
                    busy = 0
                consecutive += 1
                self._record_failure(exc, poll_seconds=seconds, started=started)
                if consecutive >= self.MAX_CONSECUTIVE_FAILURES:
                    return "failed"
                self._sleep(self._backoff(consecutive))
                continue
            busy = 0
            self._worker_busy_since = None
            self.polls += 1
            updates = _updates(raw)
            if updates and self._serializer is not None:
                # Before the handler runs, not after: the turn it starts can
                # finish and the drain can want the line while this loop is
                # still inside `handle_update`.
                self._serializer.expect_send()
            interrupted = False
            for update in updates:
                # Each update is handed to the adapter, which applies its own
                # `shadow` fence, its own allowlist and its own rate limit --
                # the poller decides nothing about an update it carries.
                try:
                    result = self._handle_update(update)
                    if getattr(result, "category", None) == "reply_not_persisted":
                        # Do not confirm an update until Control owns its reply.
                        # Reuse the bounded handler-failure retry/backoff path.
                        raise RuntimeError("telegram command reply was not persisted")
                except Exception as exc:  # noqa: BLE001 - see above
                    consecutive += 1
                    self._record_failure(exc, poll_seconds=seconds, started=started)
                    interrupted = True
                    break
                # Only now: the offset advanced BEFORE the handler ran, so a
                # handler that raised silently dropped the update it failed on
                # and the next poll never saw it again.
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._offset = update_id + 1
                self.handled += 1
            if interrupted:
                if consecutive >= self.MAX_CONSECUTIVE_FAILURES:
                    return "failed"
                self._sleep(self._backoff(consecutive))
                continue
            consecutive = 0
            if not updates:
                self._sleep(self._idle_delay)
        return "stopped"

    def _poll_seconds(self) -> int:
        if self._serializer is None:
            return self._long_poll_seconds
        return self._serializer.poll_seconds(self._long_poll_seconds)


def _updates(raw: object) -> list[Mapping[str, object]]:
    if not isinstance(raw, Mapping) or raw.get("status") != "ok":
        return []
    updates = raw.get("updates")
    if not isinstance(updates, list):
        return []
    return [item for item in updates if isinstance(item, Mapping)]


def build_telegram_adapter(
    *,
    store: ControlStore,
    config: Mapping[str, object],
    rpc: "GatedWorkerTransportRPC",
    worker_id: str,
):
    """Construct the adapter, or return None when it is not configured.

    A1-8's health boolean is `adapter constructed AND the gate is enabled`, so
    this function is the first half of that derivation and returning `None` is
    a real answer rather than a failure: an installation that has never named a
    bot has no adapter, and health says so.

    The mode defaults to `shadow`, which is the second fence behind the gate --
    `deliver_event` returns before it touches the client, so a misconfiguration
    fails towards not sending.
    """

    from ..config import (
        telegram_allowed_user_ids,
        telegram_mode,
        telegram_setting,
    )
    from .telegram import TelegramAdapter, TelegramAdapterConfig

    identity = telegram_setting(config, "telegram_bot_identity")
    base_url = telegram_setting(config, "telegram_base_url")
    if identity is None or base_url is None:
        return None
    adapter_config = TelegramAdapterConfig(
        bot_identity=identity,
        # Never configured, never a second key file to lose: both are derived,
        # domain-separated, from the binding key that already lives beside the
        # control database (D-P5-4 keeps secrets out of the files the product
        # writes).
        signing_key=store.transport_derived_key("telegram-signing"),
        allowed_user_ids=telegram_allowed_user_ids(config),
        base_url=base_url,
        mode=telegram_mode(config),  # type: ignore[arg-type]
        identity_key=store.transport_derived_key("telegram-identity"),
    )
    return TelegramAdapter.hermes_control_owned(
        store=store,
        config=adapter_config,
        rpc=rpc,
        worker_id=worker_id,
    )
