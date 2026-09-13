"""The gate in front of the worker (P5.3 item 2, D-P5-2 and ⟦AMD-4⟧).

`_HermesTransportRPC` had no production implementation at all before this, so
the gate had nowhere to sit. It sits at the boundary every transport frame
crosses, which is the only place a future call site cannot forget it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.runtime_update.supervisor import WorkerProtocolError
from cortex_platform.product.transports.worker_rpc import (
    GatedWorkerTransportRPC,
    TelegramInboundPoller,
    TransportCapabilityUnavailable,
    TransportGateClosed,
    assert_transport_capability,
)

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class FakeWorker:
    """A worker that records frames and replays scripted answers."""

    def __init__(self, answers: dict[str, object] | None = None) -> None:
        self.frames: list[tuple[str, dict, float]] = []
        self.answers = answers or {}
        self.raises: dict[str, Exception] = {}

    def request(self, method, params, *, timeout=None):
        self.frames.append((method, dict(params), timeout))
        if method in self.raises:
            raise self.raises[method]
        answer = self.answers.get(method)
        if callable(answer):
            return answer(params)
        return answer


@pytest.fixture
def store(tmp_path: Path):
    clock = {"now": _NOW}
    value = ControlStore(tmp_path / "control.db", clock=lambda: clock["now"])
    value.initialize()
    value.clock_box = clock  # type: ignore[attr-defined]
    return value


def _open_window(store: ControlStore, seconds: int = 600):
    return store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=seconds,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )


class TestGate:
    def test_a_send_is_refused_while_the_gate_is_closed(
        self, store: ControlStore
    ) -> None:
        worker = FakeWorker()
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)

        with pytest.raises(TransportGateClosed):
            rpc.request("telegram.send", {"chat_id": 1}, timeout=5)
        # Refused before the frame is written, so nothing reached the socket.
        assert worker.frames == []

    def test_a_poll_is_refused_while_the_gate_is_closed(
        self, store: ControlStore
    ) -> None:
        worker = FakeWorker()
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        with pytest.raises(TransportGateClosed):
            rpc.request("telegram.poll", {"offset": None}, timeout=5)
        assert worker.frames == []

    def test_an_open_window_lets_both_frames_through(
        self, store: ControlStore
    ) -> None:
        _open_window(store)
        worker = FakeWorker({"telegram.send": {"status": "accepted"}})
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)

        assert rpc.request("telegram.send", {"chat_id": 1}, timeout=5) == {
            "status": "accepted"
        }
        rpc.request("telegram.poll", {"offset": None}, timeout=5)
        assert [frame[0] for frame in worker.frames] == [
            "telegram.send",
            "telegram.poll",
        ]

    def test_an_expired_window_refuses_again_without_a_new_decision(
        self, store: ControlStore
    ) -> None:
        _open_window(store, seconds=60)
        worker = FakeWorker({"telegram.send": {"status": "accepted"}})
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        rpc.request("telegram.send", {"chat_id": 1}, timeout=5)

        store.clock_box["now"] = _NOW + timedelta(seconds=61)  # type: ignore[attr-defined]
        with pytest.raises(TransportGateClosed):
            rpc.request("telegram.send", {"chat_id": 1}, timeout=5)

    def test_a_disable_refuses_again(self, store: ControlStore) -> None:
        _open_window(store)
        worker = FakeWorker({"telegram.send": {"status": "accepted"}})
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        rpc.request("telegram.send", {"chat_id": 1}, timeout=5)
        store.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="tg-disable-000000001",
        )
        with pytest.raises(TransportGateClosed):
            rpc.request("telegram.send", {"chat_id": 1}, timeout=5)

    def test_capabilities_are_not_gated(self, store: ControlStore) -> None:
        """The pre-window assertion runs outside any window, on purpose.

        A packaging gap has to be found before the operator's bot is taken
        down, not while the window is open.
        """

        worker = FakeWorker(
            {"telegram.capabilities": {"protocol": "cortex.telegram.transport/1"}}
        )
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        assert store.telegram_dispatch_enabled() is False
        assert rpc.request("telegram.capabilities", {}, timeout=5) == {
            "protocol": "cortex.telegram.transport/1"
        }


class TestPreWindowAssertion:
    def test_the_new_worker_answers_the_transport_protocol(self) -> None:
        from cortex_platform.product.runtime_update.worker_payload.cortex_worker import (
            telegram as worker_telegram,
        )

        worker = FakeWorker({"telegram.capabilities": worker_telegram.capabilities()})
        assert assert_transport_capability(worker)["protocol"] == (
            "cortex.telegram.transport/1"
        )

    def test_an_s33_only_worker_refuses_the_method(self) -> None:
        """An older release has no `telegram.capabilities` in its closed set.

        The worker raises `ProtocolViolation("worker method is invalid")`, the
        supervisor turns that into a `protocol_violation` reply, and the
        assertion fails outside the window rather than during it.
        """

        from cortex_platform.product.runtime_update.supervisor import (
            WorkerProtocolError,
        )

        worker = FakeWorker()
        worker.raises["telegram.capabilities"] = WorkerProtocolError(
            "protocol_violation"
        )
        with pytest.raises(TransportCapabilityUnavailable):
            assert_transport_capability(worker)

    def test_a_worker_answering_a_different_protocol_is_refused(self) -> None:
        worker = FakeWorker(
            {"telegram.capabilities": {"protocol": "cortex.telegram.transport/2"}}
        )
        with pytest.raises(TransportCapabilityUnavailable):
            assert_transport_capability(worker)


class TestInboundPoller:
    def _poller(self, store, worker, **kwargs):
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        seen: list = []
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=seen.append,
            long_poll_seconds=0,
            sleep=lambda _: None,
            **kwargs,
        )
        return poller, seen

    def test_a_closed_gate_ends_the_loop_before_any_frame(
        self, store: ControlStore
    ) -> None:
        worker = FakeWorker()
        poller, seen = self._poller(store, worker)
        assert poller.run(max_iterations=5) == "gate_closed"
        assert worker.frames == []
        assert seen == []

    def test_updates_are_handed_to_the_adapter_and_advance_the_offset(
        self, store: ControlStore
    ) -> None:
        _open_window(store)
        answers = iter(
            [
                {
                    "status": "ok",
                    "updates": [
                        {"update_id": 10, "message": {"text": "one"}},
                        {"update_id": 11, "callback_query": {"data": "x"}},
                    ],
                },
                {"status": "ok", "updates": []},
            ]
        )
        worker = FakeWorker({"telegram.poll": lambda _: next(answers)})
        poller, seen = self._poller(store, worker)

        assert poller.run(max_iterations=2) == "bounded"
        assert [update["update_id"] for update in seen] == [10, 11]
        assert poller.offset == 12
        assert worker.frames[1][1]["offset"] == 12

    def test_disabling_the_gate_mid_loop_ends_the_inbound_loop(
        self, store: ControlStore
    ) -> None:
        """⟦AMD-4⟧: a disable ends the inbound loop, it does not just fail sends."""

        _open_window(store)

        def answer(_params):
            store.disable_transport_activation(
                transport="telegram",
                actor_id="operator",
                idempotency_key="tg-disable-000000001",
            )
            return {"status": "ok", "updates": []}

        worker = FakeWorker({"telegram.poll": answer})
        poller, seen = self._poller(store, worker)

        assert poller.run(max_iterations=10) == "gate_closed"
        # Exactly one frame: the gate was checked again before the second.
        assert len(worker.frames) == 1

    def test_an_expiring_window_ends_the_inbound_loop(
        self, store: ControlStore
    ) -> None:
        _open_window(store, seconds=60)

        def answer(_params):
            store.clock_box["now"] = _NOW + timedelta(seconds=61)  # type: ignore[attr-defined]
            return {"status": "ok", "updates": []}

        worker = FakeWorker({"telegram.poll": answer})
        poller, _ = self._poller(store, worker)
        assert poller.run(max_iterations=10) == "gate_closed"
        assert len(worker.frames) == 1

    def test_an_unavailable_poll_is_not_an_error(self, store: ControlStore) -> None:
        """A persistent 409 is the old poller, not a fault the loop dies on."""

        _open_window(store)
        worker = FakeWorker({"telegram.poll": {"status": "unavailable", "updates": []}})
        poller, seen = self._poller(store, worker)
        assert poller.run(max_iterations=3) == "bounded"
        assert seen == []
        assert poller.polls == 3

    def test_stop_ends_the_loop(self, store: ControlStore) -> None:
        _open_window(store)
        worker = FakeWorker({"telegram.poll": {"status": "ok", "updates": []}})
        poller, _ = self._poller(store, worker)
        poller.stop()
        assert poller.run(max_iterations=3) == "stopped"
        assert worker.frames == []


class TestFrameDeadline:
    """P5-02: the product must not abandon a frame the worker still owns."""

    def test_the_product_deadline_strictly_outlives_the_workers_own(self) -> None:
        """Both sides derived 40.0 s for a 30 s long poll, so the product gave
        up at the same instant the worker did.

        A frame abandoned at the deadline is not a frame that failed: the
        worker answers a moment later, and that late reply used to be a
        correlation fault that failed the whole managed-worker channel.
        Whatever the margins are, the product's deadline has to be strictly
        larger by construction rather than by coincidence.
        """

        from cortex_platform.product.runtime_update.worker_payload.cortex_worker import (
            telegram as worker_telegram,
        )
        from cortex_platform.product.runtime_update.worker_protocol import (
            TELEGRAM_MAX_POLL_SECONDS,
        )
        from cortex_platform.product.transports.worker_rpc import (
            telegram_frame_deadline,
        )

        for long_poll in range(TELEGRAM_MAX_POLL_SECONDS + 1):
            assert telegram_frame_deadline(long_poll) > worker_telegram.poll_timeout(
                long_poll
            ), long_poll

    def test_the_poller_asks_for_the_deadline_it_computed(
        self, store: ControlStore
    ) -> None:
        from cortex_platform.product.transports.worker_rpc import (
            telegram_frame_deadline,
        )

        _open_window(store)
        worker = FakeWorker({"telegram.poll": {"status": "ok", "updates": []}})
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=lambda _update: None,
            long_poll_seconds=30,
            sleep=lambda _: None,
        )

        poller.run(max_iterations=1)

        assert worker.frames[0][2] == telegram_frame_deadline(30)


class TestInboundPollerResilience:
    """P5-05: the loop must not die silently with the gate still open."""

    def _poller(self, store, worker, handle_update, **kwargs):
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        return TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=handle_update,
            long_poll_seconds=0,
            sleep=lambda _: None,
            **kwargs,
        )

    def test_a_transient_rpc_error_is_retried_rather_than_fatal(
        self, store: ControlStore
    ) -> None:
        """`run()` caught only `TransportGateClosed`.

        Any `WorkerProtocolError`, `WorkerCrashed` or `_UnboundWorker`
        RuntimeError from `self._rpc.request` therefore terminated the loop
        permanently, with the gate still open, no retry, no log and no state
        that distinguishes `stopped` from `crashed`.
        """

        _open_window(store)
        attempts: list[int] = []

        def answer(_params):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("worker is not bound")
            return {"status": "ok", "updates": [{"update_id": 7, "message": {}}]}

        worker = FakeWorker({"telegram.poll": answer})
        seen: list = []
        poller = self._poller(store, worker, seen.append)

        assert poller.run(max_iterations=2) == "bounded"
        assert len(attempts) == 2
        assert [update["update_id"] for update in seen] == [7]
        assert poller.offset == 8

    def test_a_handler_that_raises_does_not_drop_its_update(
        self, store: ControlStore
    ) -> None:
        """The offset advanced BEFORE the handler ran, so a failed handler
        silently dropped the update it failed on."""

        _open_window(store)
        seen: list = []

        def handle(update):
            if len(seen) == 0:
                seen.append(update)
                raise ValueError("adapter blew up")
            seen.append(update)

        worker = FakeWorker(
            {
                "telegram.poll": lambda _p: {
                    "status": "ok",
                    "updates": [{"update_id": 11, "message": {}}],
                }
            }
        )
        poller = self._poller(store, worker, handle)

        assert poller.run(max_iterations=2) == "bounded"
        # The second poll asked for the SAME offset, so the update came back.
        assert worker.frames[0][1]["offset"] is None
        assert worker.frames[1][1]["offset"] is None
        assert [update["update_id"] for update in seen] == [11, 11]
        assert poller.offset == 12

    def test_a_loop_that_only_ever_fails_returns_a_terminal_reason(
        self, store: ControlStore
    ) -> None:
        """`stopped` and `crashed` used to be the same answer: none."""

        _open_window(store)
        worker = FakeWorker()
        worker.raises["telegram.poll"] = RuntimeError("worker is not bound")
        poller = self._poller(store, worker, lambda _u: None)

        outcome = poller.run(max_iterations=100)

        assert outcome == "failed"
        assert outcome not in {"stopped", "gate_closed", "bounded"}
        assert poller.failures == TelegramInboundPoller.MAX_CONSECUTIVE_FAILURES
        assert poller.last_error == "RuntimeError"

    def test_the_backoff_is_bounded_and_grows(self, store: ControlStore) -> None:
        _open_window(store)
        delays: list[float] = []
        worker = FakeWorker()
        worker.raises["telegram.poll"] = RuntimeError("worker is not bound")
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=lambda _u: None,
            long_poll_seconds=0,
            sleep=delays.append,
        )

        assert poller.run(max_iterations=100) == "failed"
        assert delays == sorted(delays)
        assert delays[0] == TelegramInboundPoller.BACKOFF_BASE_SECONDS
        assert max(delays) <= TelegramInboundPoller.BACKOFF_MAX_SECONDS


class TestPollerFailureDetail:
    """⟦P5.6⟧ The message behind the class, bounded and redacted.

    The sixth window on the mini cycled through `WorkerProtocolError` bursts
    for sixteen minutes and nothing on the machine said whether that meant
    "worker response timed out" (the product's own deadline) or the worker's
    `operation_conflict` (the previous poll still open inside it). The class
    name alone cannot tell a stalled IPv6 path from a wedged worker.
    """

    def _poller(self, store, worker, **kwargs):
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        return TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=lambda _u: None,
            long_poll_seconds=0,
            sleep=lambda _: None,
            **kwargs,
        )

    def test_the_error_text_is_kept_beside_the_class(self, store: ControlStore) -> None:
        from cortex_platform.product.runtime_update.supervisor import (
            WorkerProtocolError,
        )

        _open_window(store)
        worker = FakeWorker()
        worker.raises["telegram.poll"] = WorkerProtocolError("worker response timed out")
        poller = self._poller(store, worker)

        assert poller.run(max_iterations=1) == "bounded"
        assert poller.last_error == "WorkerProtocolError"
        assert poller.last_error_detail == "worker response timed out"

    def test_the_last_ten_failures_are_a_ring_with_timing(
        self, store: ControlStore
    ) -> None:
        from cortex_platform.product.transports.worker_rpc import (
            POLLER_FAILURE_LOG_LIMIT,
        )

        _open_window(store)
        clock = {"now": 100.0}
        attempts = {"count": 0}

        def failing(_params):
            attempts["count"] += 1
            clock["now"] += 0.25
            raise RuntimeError(f"failure {attempts['count']}")

        worker = FakeWorker({"telegram.poll": failing})
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=lambda _u: None,
            long_poll_seconds=0,
            sleep=lambda _: None,
            monotonic=lambda: clock["now"],
        )
        # MAX_CONSECUTIVE_FAILURES ends the loop; run it several times.
        for _ in range(4):
            poller.run(max_iterations=100)
        assert poller.failures == 4 * TelegramInboundPoller.MAX_CONSECUTIVE_FAILURES
        log = list(poller.failure_log)
        assert len(log) == POLLER_FAILURE_LOG_LIMIT
        assert set(log[0]) == {"at", "error", "detail", "poll_seconds", "elapsed_ms"}
        assert log[-1]["detail"] == f"failure {poller.failures}"
        assert log[-1]["error"] == "RuntimeError"
        assert log[-1]["poll_seconds"] == 0
        assert log[-1]["elapsed_ms"] == 250
        assert log[-1]["at"].endswith("Z")

    def test_a_secret_in_the_message_never_reaches_the_record(
        self, store: ControlStore
    ) -> None:
        _open_window(store)
        token = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"
        worker = FakeWorker()
        worker.raises["telegram.poll"] = RuntimeError(
            f"https://api.telegram.org/bot{token}/getUpdates?offset=1 refused"
        )
        poller = self._poller(store, worker)
        poller.run(max_iterations=1)
        assert token not in str(poller.last_error_detail)
        assert token not in str(list(poller.failure_log))
        assert "offset=1" not in str(poller.last_error_detail)

    def test_the_detail_is_bounded(self, store: ControlStore) -> None:
        from cortex_platform.product.redaction import DETAIL_LIMIT

        _open_window(store)
        worker = FakeWorker()
        worker.raises["telegram.poll"] = RuntimeError("x" * 5000)
        poller = self._poller(store, worker)
        poller.run(max_iterations=1)
        assert len(poller.last_error_detail or "") == DETAIL_LIMIT

    def test_a_handler_failure_is_recorded_with_its_text_too(
        self, store: ControlStore
    ) -> None:
        _open_window(store)
        worker = FakeWorker(
            {
                "telegram.poll": lambda _p: {
                    "status": "ok",
                    "updates": [{"update_id": 3, "message": {}}],
                }
            }
        )

        def handle(_update):
            raise ValueError("adapter refused the update")

        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=handle,
            long_poll_seconds=0,
            sleep=lambda _: None,
        )
        poller.run(max_iterations=1)
        assert poller.last_error == "ValueError"
        assert poller.last_error_detail == "adapter refused the update"

    def test_every_failure_is_handed_to_the_sink(self, store: ControlStore) -> None:
        """The supervisor's ring outlives a restarted poller; it is fed here."""

        _open_window(store)
        worker = FakeWorker()
        worker.raises["telegram.poll"] = RuntimeError("gone")
        seen: list[dict] = []
        poller = self._poller(store, worker, on_failure=seen.append)
        poller.run(max_iterations=2)
        assert [entry["detail"] for entry in seen] == ["gone", "gone"]

    def test_a_sink_that_raises_does_not_kill_the_loop(
        self, store: ControlStore
    ) -> None:
        _open_window(store)
        worker = FakeWorker()
        worker.raises["telegram.poll"] = RuntimeError("gone")

        def sink(_entry):
            raise RuntimeError("the sink is broken")

        poller = self._poller(store, worker, on_failure=sink)
        assert poller.run(max_iterations=2) == "bounded"
        assert poller.failures == 2


class TestLineBusyIsNotAFault:
    """⟦P5.6⟧ Batch D D-6: contention on the line is not a poller strike."""

    def test_a_busy_line_is_counted_and_never_a_strike(
        self, store: ControlStore
    ) -> None:
        import threading

        from cortex_platform.product.transports.worker_rpc import (
            TransportCallSerializer,
        )

        _open_window(store)
        serializer = TransportCallSerializer(poll_wait_seconds=0.01)
        worker = FakeWorker({"telegram.poll": {"status": "ok", "updates": []}})
        rpc = GatedWorkerTransportRPC(
            supervisor=worker, store=store, serializer=serializer
        )
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=lambda _u: None,
            long_poll_seconds=0,
            sleep=lambda _: None,
        )
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with serializer.hold("telegram.send"):
                held.set()
                release.wait(5.0)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5.0)
        try:
            assert poller.run(max_iterations=3) == "bounded"
        finally:
            release.set()
            holder.join(5.0)
        assert poller.line_busy == 3
        assert poller.failures == 0
        assert poller.last_error is None
        assert list(poller.failure_log) == []
        assert worker.frames == []

    def test_the_poll_wait_outlives_the_longest_send_hold(self) -> None:
        """D-6: equality raced at the boundary with no margin."""

        from cortex_platform.product.transports.worker_rpc import (
            TELEGRAM_FRAME_DEADLINE_SLACK_SECONDS,
            TRANSPORT_POLL_WAIT_SECONDS,
        )
        from cortex_platform.product.transports.telegram import (
            _HERMES_RPC_TIMEOUT_SECONDS,
        )

        assert TRANSPORT_POLL_WAIT_SECONDS > _HERMES_RPC_TIMEOUT_SECONDS
        assert (
            TRANSPORT_POLL_WAIT_SECONDS
            == _HERMES_RPC_TIMEOUT_SECONDS + TELEGRAM_FRAME_DEADLINE_SLACK_SECONDS
        )


class _HeldSlotWorker:
    """The sealed worker's single transport slot, in virtual time.

    Two behaviours, both read from source rather than guessed:

    * `worker_payload/cortex_worker/serve.py::_TransportCalls` runs one
      transport call at a time. `submit()` returns False while busy and the
      request loop answers `operation_conflict` BEFORE the call is entered, so
      no `getUpdates` is issued and no update is consumed.
    * `runtime_update/supervisor.py::WorkerSupervisorV2.request` gives up at its
      caller's deadline, drops its pending entry and raises
      `WorkerProtocolError("worker response timed out")`. The worker keeps
      running the call it was given.
    """

    def __init__(self, clock, *, durations: list[float]) -> None:
        self._clock = clock
        self._durations = list(durations)
        self._busy_until = -1.0
        self.frames: list[tuple[str, float, str]] = []

    def request(self, method, params, *, timeout=None):
        if self._clock.now < self._busy_until:
            self.frames.append((method, self._clock.now, "operation_conflict"))
            raise WorkerProtocolError("operation_conflict")
        duration = self._durations.pop(0) if self._durations else 0.0
        if duration > float(timeout):
            self._busy_until = self._clock.now + duration
            self.frames.append((method, self._clock.now, "abandoned"))
            self._clock.now += float(timeout)
            raise WorkerProtocolError("worker response timed out")
        self.frames.append((method, self._clock.now, "ok"))
        self._clock.now += duration
        return {"status": "ok", "updates": []}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)

    def monotonic(self) -> float:
        return self.now


class TestHeldWorkerSlotIsNotAFault:
    """⟦G-1⟧ An abandoned frame must not spend the "worker is gone" budget.

    Reproduced from the gen18 acceptance sequence: a `poll_seconds=30` frame
    outlived the product's 45 s deadline and every following poll answered
    `operation_conflict`. The conflicts are the echo of the product's own
    abandoned frame -- and they used to be five strikes in fifteen seconds,
    which ended the loop, which the window supervisor then tried to fix with
    five restarts that cannot free a slot the poller does not own.
    """

    def _poller(self, store, worker, clock, **kwargs):
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        return TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=lambda _u: None,
            long_poll_seconds=30,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            **kwargs,
        )

    def test_a_stall_outliving_the_frame_no_longer_ends_the_loop(
        self, store: ControlStore
    ) -> None:
        """The reproduction. A 90 s worker call used to be a dead poller."""

        _open_window(store)
        clock = _Clock()
        worker = _HeldSlotWorker(clock, durations=[90.0])
        poller = self._poller(store, worker, clock)

        assert poller.run(max_iterations=40) == "bounded"

        # The abandoned frame is a real failure and is recorded as one. The
        # conflicts that follow it are not, and the loop lives to poll again.
        assert poller.failures == 1
        assert poller.last_error_detail == "worker response timed out"
        assert poller.worker_busy > 0
        assert poller.polls > 0
        assert poller.worker_busy_seconds is None
        assert [verdict for _m, _t, verdict in worker.frames][0] == "abandoned"

    def test_a_held_slot_is_counted_and_backed_off_rather_than_struck(
        self, store: ControlStore
    ) -> None:
        _open_window(store)
        clock = _Clock()
        worker = _HeldSlotWorker(clock, durations=[])
        worker._busy_until = 1e9  # held for the whole test
        poller = self._poller(store, worker, clock)

        assert poller.run(max_iterations=3) == "bounded"

        assert poller.worker_busy == 3
        assert poller.worker_busy_seconds == 7.0
        assert poller.failures == 0
        assert poller.last_error is None
        assert list(poller.failure_log) == []
        # Counted apart from the line: `line_busy` reads "a send is slow".
        assert poller.line_busy == 0
        # Backed off, so a wedged slot is not hammered.
        assert clock.now == 1.0 + 2.0 + 4.0

    def test_a_slot_held_past_the_bound_becomes_an_ordinary_failure(
        self, store: ControlStore
    ) -> None:
        """The bound is what keeps this from being suppression."""

        _open_window(store)
        clock = _Clock()
        worker = _HeldSlotWorker(clock, durations=[])
        worker._busy_until = 1e9
        poller = self._poller(store, worker, clock)

        assert (
            poller.run(max_iterations=TelegramInboundPoller.MAX_CONSECUTIVE_WORKER_BUSY)
            == "bounded"
        )

        assert poller.failures == 1
        assert poller.last_error == "TransportWorkerBusy"
        assert poller.last_error_detail == "transport_worker_busy"

    def test_a_permanently_held_slot_still_ends_the_loop(
        self, store: ControlStore
    ) -> None:
        """A worker that never frees its slot is still a fault, just later."""

        _open_window(store)
        clock = _Clock()
        worker = _HeldSlotWorker(clock, durations=[])
        worker._busy_until = 1e9
        poller = self._poller(store, worker, clock)

        assert poller.run(max_iterations=1000) == "failed"
        assert poller.failures == TelegramInboundPoller.MAX_CONSECUTIVE_FAILURES
        assert clock.now == 770.0

    def test_a_refused_poll_never_moves_the_offset(
        self, store: ControlStore
    ) -> None:
        """`_submit_transport` refuses before `getUpdates`: nothing is consumed."""

        _open_window(store)
        clock = _Clock()
        worker = _HeldSlotWorker(clock, durations=[])
        handled: list[object] = []
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=store)
        poller = TelegramInboundPoller(
            rpc=rpc,
            store=store,
            handle_update=handled.append,
            long_poll_seconds=30,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        # One real update first, so there is an offset worth losing.
        worker.request = lambda method, params, *, timeout=None: {  # type: ignore[method-assign]
            "status": "ok",
            "updates": [{"update_id": 41, "message": {"text": "hi"}}],
        }
        assert poller.run(max_iterations=1) == "bounded"
        assert poller.offset == 42

        offsets: list[object] = []

        def conflicting(method, params, *, timeout=None):
            offsets.append(params["offset"])
            raise WorkerProtocolError("operation_conflict")

        worker.request = conflicting  # type: ignore[method-assign]
        assert poller.run(max_iterations=2) == "bounded"

        assert poller.offset == 42
        assert offsets == [42, 42]
        assert len(handled) == 1
