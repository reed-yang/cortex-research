"""⟦P5.5⟧ One transport call at a time, and a refusal that keeps its type.

The certified worker allows exactly ONE open transport call and the product had
no matching rule, so the poller's 30 s `telegram.poll` and the drain's
`telegram.send` met inside the worker on the operator's first real round trip.
The worker refused with `operation_conflict` -- and then died naming the chunk's
colon-bearing operation id in its own error reply, which its response validator
does not accept.

Neither half could be seen from a test before: the loopback Bot API answered
`getUpdates` instantly, so poll and send strictly alternated. These tests hold a
poll open on purpose.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    transport_delivery_chunk_operation_id,
    transport_delivery_chunk_wire_operation_id,
)
from cortex_platform.product.runtime_update.worker_protocol import (
    _OPERATION_ID as PRODUCT_MIRROR_OF_WORKER_OPERATION_ID,
)
from cortex_platform.product.runtime_update.supervisor import WorkerProtocolError
from cortex_platform.product.transports.worker_rpc import (
    OUTBOUND_EXPECTED_SECONDS,
    SHORT_POLL_SECONDS,
    GatedWorkerTransportRPC,
    TelegramInboundPoller,
    TelegramRefusedBeforeSend,
    TransportCallSerializer,
    TransportGateClosed,
    TransportLineBusy,
    TransportWorkerBusy,
)

_NOW = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
_JOIN_SECONDS = 10.0


class ConcurrentWorker:
    """Records what is open, so an overlap is an observation and not a guess."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.methods: list[str] = []
        self.open_calls = 0
        self.max_open_calls = 0
        self.entered: dict[str, threading.Event] = {}
        self.block: dict[str, threading.Event] = {}
        self.answers: dict[str, object] = {}

    def request(self, method, params, *, timeout=None):
        with self._lock:
            self.methods.append(method)
            self.open_calls += 1
            self.max_open_calls = max(self.max_open_calls, self.open_calls)
        try:
            entered = self.entered.get(method)
            if entered is not None:
                entered.set()
            gate = self.block.get(method)
            if gate is not None:
                assert gate.wait(_JOIN_SECONDS), "worker was never released"
            return self.answers.get(method)
        finally:
            with self._lock:
                self.open_calls -= 1


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(tmp_path / "control.db", clock=lambda: _NOW)
    value.initialize()
    value.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-p55000001",
    )
    return value


class TestTheSharedTransportLine:
    """A send and a poll can never be open at the same time."""

    def test_a_send_waits_for_an_open_poll_and_never_overlaps_it(
        self, store: ControlStore
    ) -> None:
        worker = ConcurrentWorker()
        worker.entered["telegram.poll"] = threading.Event()
        worker.block["telegram.poll"] = threading.Event()
        worker.answers["telegram.send"] = {"status": "accepted"}
        rpc = GatedWorkerTransportRPC(
            supervisor=worker,
            store=store,
            serializer=TransportCallSerializer(send_wait_seconds=_JOIN_SECONDS),
        )

        poll = threading.Thread(
            target=lambda: rpc.request(
                "telegram.poll", {"offset": None, "timeout_seconds": 30}, timeout=45
            )
        )
        poll.start()
        assert worker.entered["telegram.poll"].wait(_JOIN_SECONDS)

        sent: list[object] = []
        send = threading.Thread(
            target=lambda: sent.append(
                rpc.request("telegram.send", {"chat_id": 1}, timeout=20)
            )
        )
        send.start()
        # The send is waiting on the line, not on the worker: the frame has not
        # been written, which is the whole difference between a refusal the
        # product can prove and an `OperationConflict` inside the worker.
        threading.Event().wait(0.2)
        assert worker.methods == ["telegram.poll"]

        worker.block["telegram.poll"].set()
        send.join(_JOIN_SECONDS)
        poll.join(_JOIN_SECONDS)

        assert worker.methods == ["telegram.poll", "telegram.send"]
        assert worker.max_open_calls == 1
        assert sent == [{"status": "accepted"}]

    def test_a_send_that_cannot_take_the_line_is_refused_before_the_frame(
        self, store: ControlStore
    ) -> None:
        """⟦P5-01⟧ A refusal here is provable: nothing was written."""

        worker = ConcurrentWorker()
        worker.entered["telegram.poll"] = threading.Event()
        worker.block["telegram.poll"] = threading.Event()
        serializer = TransportCallSerializer(send_wait_seconds=0.2)
        rpc = GatedWorkerTransportRPC(
            supervisor=worker, store=store, serializer=serializer
        )
        poll = threading.Thread(
            target=lambda: rpc.request(
                "telegram.poll", {"offset": None, "timeout_seconds": 30}, timeout=45
            )
        )
        poll.start()
        assert worker.entered["telegram.poll"].wait(_JOIN_SECONDS)

        try:
            with pytest.raises(TelegramRefusedBeforeSend) as refusal:
                rpc.request("telegram.send", {"chat_id": 1}, timeout=20)
        finally:
            worker.block["telegram.poll"].set()
            poll.join(_JOIN_SECONDS)

        assert refusal.value.reason == "transport_line_busy"
        assert worker.methods == ["telegram.poll"]
        assert serializer.sends_refused == 1

    def test_a_refused_send_leaves_the_line_short_polling(
        self, store: ControlStore
    ) -> None:
        """The refusal IS the evidence that outbound work exists."""

        serializer = TransportCallSerializer(send_wait_seconds=0.05)
        held = threading.Event()
        holder = threading.Thread(target=_hold_line, args=(serializer, held))
        holder.start()
        try:
            assert held.wait(_JOIN_SECONDS)
            assert serializer.poll_seconds(30) == 30
            with pytest.raises(TelegramRefusedBeforeSend):
                with serializer.hold("telegram.send"):
                    pass
            assert serializer.poll_seconds(30) == SHORT_POLL_SECONDS
        finally:
            held.clear()
            holder.join(_JOIN_SECONDS)

    def test_a_poll_that_cannot_take_the_line_says_so_rather_than_hanging(
        self, store: ControlStore
    ) -> None:
        serializer = TransportCallSerializer(poll_wait_seconds=0.05)
        held = threading.Event()
        holder = threading.Thread(target=_hold_line, args=(serializer, held))
        holder.start()
        try:
            assert held.wait(_JOIN_SECONDS)
            with pytest.raises(TransportLineBusy):
                with serializer.hold("telegram.poll"):
                    pass
        finally:
            held.clear()
            holder.join(_JOIN_SECONDS)

    def test_capabilities_never_take_the_line(self, store: ControlStore) -> None:
        """`send_chunk` asks for capabilities on its way to taking the line.

        Serializing that frame would deadlock the send against itself, and the
        worker answers it on its request loop while a transport call is open --
        so the frame is correct as well as necessary.
        """

        serializer = TransportCallSerializer(send_wait_seconds=0.05)
        worker = ConcurrentWorker()
        worker.answers["telegram.capabilities"] = {"protocol": "x"}
        rpc = GatedWorkerTransportRPC(
            supervisor=worker, store=store, serializer=serializer
        )
        held = threading.Event()
        holder = threading.Thread(target=_hold_line, args=(serializer, held))
        holder.start()
        try:
            assert held.wait(_JOIN_SECONDS)
            assert rpc.request("telegram.capabilities", {}, timeout=5) == {
                "protocol": "x"
            }
        finally:
            held.clear()
            holder.join(_JOIN_SECONDS)

    def test_the_gate_is_still_checked_before_the_line(
        self, tmp_path: Path
    ) -> None:
        """⟦AMD-4⟧ A closed gate refuses without ever contending for anything."""

        closed = ControlStore(tmp_path / "closed.db", clock=lambda: _NOW)
        closed.initialize()
        worker = ConcurrentWorker()
        rpc = GatedWorkerTransportRPC(supervisor=worker, store=closed)
        with pytest.raises(TransportGateClosed):
            rpc.request("telegram.send", {"chat_id": 1}, timeout=5)
        assert worker.methods == []


def _hold_line(serializer: TransportCallSerializer, held: threading.Event) -> None:
    with serializer.hold("telegram.poll"):
        held.set()
        while held.is_set():
            threading.Event().wait(0.01)


class TestPollLength:
    """The long poll is the right frame only when there is nothing to send."""

    def _serializer(self, clock: list[float]) -> TransportCallSerializer:
        return TransportCallSerializer(monotonic=lambda: clock[0])

    def test_an_idle_ledger_gets_the_full_long_poll(self) -> None:
        assert TransportCallSerializer().poll_seconds(30) == 30

    def test_an_inbound_update_shortens_the_next_poll(self) -> None:
        clock = [0.0]
        serializer = self._serializer(clock)
        serializer.expect_send()
        assert serializer.poll_seconds(30) == SHORT_POLL_SECONDS

    def test_the_expectation_expires_back_to_the_long_poll(self) -> None:
        clock = [0.0]
        serializer = self._serializer(clock)
        serializer.expect_send()
        clock[0] = OUTBOUND_EXPECTED_SECONDS + 1.0
        assert serializer.poll_seconds(30) == 30

    def test_the_drain_reporting_work_shortens_the_poll(self) -> None:
        serializer = TransportCallSerializer()
        serializer.outbound_pending(True)
        assert serializer.poll_seconds(30) == SHORT_POLL_SECONDS
        serializer.outbound_pending(False)
        assert serializer.poll_seconds(30) == 30

    def test_the_short_poll_never_exceeds_the_configured_long_poll(self) -> None:
        serializer = TransportCallSerializer()
        serializer.outbound_pending(True)
        assert serializer.poll_seconds(0) == 0

    def test_the_poller_asks_for_the_short_frame_after_an_update(
        self, store: ControlStore
    ) -> None:
        """End to end through the poller: the frame itself carries the change."""

        frames: list[dict] = []

        class Recording:
            serializer = TransportCallSerializer()

            def request(self, method, params, *, timeout=None):
                frames.append(dict(params))
                if len(frames) == 1:
                    return {
                        "status": "ok",
                        "updates": [{"update_id": 1, "message": {}}],
                    }
                return {"status": "ok", "updates": []}

        poller = TelegramInboundPoller(
            rpc=Recording(),
            store=store,
            handle_update=lambda _update: None,
            sleep=lambda _seconds: None,
        )
        assert poller.run(max_iterations=2) == "bounded"
        assert frames[0]["timeout_seconds"] == 30
        assert frames[1]["timeout_seconds"] == SHORT_POLL_SECONDS

    def test_a_poller_without_a_line_keeps_the_fixed_long_poll(
        self, store: ControlStore
    ) -> None:
        frames: list[dict] = []

        class Bare:
            def request(self, method, params, *, timeout=None):
                frames.append(dict(params))
                return {"status": "ok", "updates": []}

        poller = TelegramInboundPoller(
            rpc=Bare(),
            store=store,
            handle_update=lambda _update: None,
            sleep=lambda _seconds: None,
        )
        assert poller.run(max_iterations=2) == "bounded"
        assert [frame["timeout_seconds"] for frame in frames] == [30, 30]


class TestTypedRefusal:
    """`operation_conflict` is a refusal, not an unknown outcome."""

    def test_an_operation_conflict_on_a_send_is_typed_as_pre_socket(
        self, store: ControlStore
    ) -> None:
        class Conflicting:
            def request(self, method, params, *, timeout=None):
                raise WorkerProtocolError("operation_conflict")

        rpc = GatedWorkerTransportRPC(supervisor=Conflicting(), store=store)
        with pytest.raises(TelegramRefusedBeforeSend) as refusal:
            rpc.request("telegram.send", {"chat_id": 1}, timeout=5)
        assert refusal.value.reason == "operation_conflict"

    def test_an_operation_conflict_on_a_poll_is_never_a_send_refusal(
        self, store: ControlStore
    ) -> None:
        """⟦G-1⟧ The same worker category, a different question.

        A send asks ⟦P5-01⟧'s question -- may this be written again? A poll asks
        nothing of the kind: `_submit_transport` refused before `getUpdates`, so
        the frame is simply not this poller's turn. The two must not share a
        type, because `TelegramRefusedBeforeSend` releases a delivery permit.
        """

        class Conflicting:
            def request(self, method, params, *, timeout=None):
                raise WorkerProtocolError("operation_conflict")

        rpc = GatedWorkerTransportRPC(supervisor=Conflicting(), store=store)
        with pytest.raises(TransportWorkerBusy):
            rpc.request(
                "telegram.poll", {"offset": None, "timeout_seconds": 0}, timeout=5
            )

    def test_every_other_worker_category_on_a_poll_stays_what_it_was(
        self, store: ControlStore
    ) -> None:
        """Only the one category the worker raises before starting is typed."""

        class Failing:
            def request(self, method, params, *, timeout=None):
                raise WorkerProtocolError("worker response timed out")

        rpc = GatedWorkerTransportRPC(supervisor=Failing(), store=store)
        with pytest.raises(WorkerProtocolError) as failure:
            rpc.request(
                "telegram.poll", {"offset": None, "timeout_seconds": 0}, timeout=5
            )
        assert not isinstance(failure.value, TransportWorkerBusy)

    def test_every_other_worker_category_stays_what_it_was(
        self, store: ControlStore
    ) -> None:
        """An unknown outcome must never be laundered into a retryable one."""

        class Failing:
            def request(self, method, params, *, timeout=None):
                raise WorkerProtocolError("protocol_violation")

        rpc = GatedWorkerTransportRPC(supervisor=Failing(), store=store)
        with pytest.raises(WorkerProtocolError):
            rpc.request("telegram.send", {"chat_id": 1}, timeout=5)


class TestWireOperationId:
    """What the ledger calls a chunk, and what the worker may echo."""

    def _chunk(self, index: int = 0) -> str:
        return transport_delivery_chunk_operation_id(
            "telegram.delivery." + "a" * 64, index
        )

    def test_the_wire_name_carries_no_colon(self) -> None:
        ledger = self._chunk()
        assert ledger == "telegram.delivery." + "a" * 64 + ":chunk:0"
        assert transport_delivery_chunk_wire_operation_id(ledger) == (
            "telegram.delivery." + "a" * 64 + ".chunk.0"
        )

    def test_the_wire_name_is_one_the_certified_worker_will_echo(self) -> None:
        """The exact pattern `WorkerResponse.__post_init__` validates against."""

        for index in range(100):
            wire = transport_delivery_chunk_wire_operation_id(self._chunk(index))
            assert PRODUCT_MIRROR_OF_WORKER_OPERATION_ID.fullmatch(wire) is not None

    def test_the_ledger_name_is_one_it_would_have_died_on(self) -> None:
        """The defect, stated as an assertion rather than as a story."""

        assert (
            PRODUCT_MIRROR_OF_WORKER_OPERATION_ID.fullmatch(self._chunk()) is None
        )

    def test_the_mapping_is_injective_over_every_chunk_of_one_delivery(
        self,
    ) -> None:
        wires = {
            transport_delivery_chunk_wire_operation_id(self._chunk(index))
            for index in range(100)
        }
        assert len(wires) == 100

    def test_a_name_that_cannot_be_mapped_is_refused_rather_than_truncated(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="wire operation_id is invalid"):
            transport_delivery_chunk_wire_operation_id("x" * 190 + ":chunk:0")
        with pytest.raises(ValueError, match="operation_id is invalid"):
            transport_delivery_chunk_wire_operation_id(":leading")

    def test_the_product_mirror_matches_the_pinned_worker_source(self) -> None:
        """Frozen from the byte-pinned source text, never from a paraphrase.

        Read as TEXT rather than imported: `worker_payload/` is byte-pinned into
        the gen 9 Hermes manifest and importing it is how a `__pycache__` entry
        gets written into a tree whose digest is the release's identity.
        """

        source = (
            Path(__file__).resolve().parents[3]
            / "product"
            / "runtime_update"
            / "worker_payload"
            / "cortex_worker"
            / "protocol.py"
        ).read_text(encoding="utf-8")
        pinned = re.search(r"^_OPERATION_ID = re\.compile\((.+)\)$", source, re.M)
        assert pinned is not None
        assert eval(pinned.group(1)) == PRODUCT_MIRROR_OF_WORKER_OPERATION_ID.pattern


class TestWindowReset:
    """⟦P5.6⟧ Batch D D-4: counters are per window, not per daemon."""

    def test_reset_window_zeroes_counts_and_the_expectation(self) -> None:
        serializer = TransportCallSerializer()
        serializer.sends_refused = 3
        serializer.polls_shortened = 9
        serializer.expect_send()
        serializer.outbound_pending(True)
        serializer.reset_window()
        assert serializer.status() == {
            "sends_refused": 0,
            "polls_shortened": 0,
            "outbound_pending": False,
            "sends_waiting": 0,
        }
        assert serializer.poll_seconds(30) == 30


class _RecordingBackend:
    """`ManagedHermesBackend`'s two verbs, over a worker that records frames."""

    instances: list["_RecordingBackend"] = []

    def __init__(
        self,
        descriptor_path,
        *,
        environment_factory,
        egress_port,
        agent_options_factory=None,
    ) -> None:
        from types import SimpleNamespace

        self.worker_object = ConcurrentWorker()
        self.worker_object.process = SimpleNamespace(pid=4242, poll=lambda: None)
        self._environment_factory = environment_factory
        _RecordingBackend.instances.append(self)

    def worker(self):
        self._environment_factory()
        return self.worker_object

    def close(self) -> None:
        return None


class TestTheGateDuringTheWait:
    """⟦P5.6⟧ Batch D D-8: the invariant, pinned at the PRODUCTION boundary.

    `test_the_gate_is_still_checked_before_the_line` uses a stub worker with no
    gate check of its own, so the property "a disable landing during the line
    wait never reaches the worker" was enforced by a collaborator and pinned by
    nothing. This one goes through `ManagedTransportWorker.acquire()`, which
    re-reads `telegram_dispatch_enabled()` after the line is granted.
    """

    def test_a_disable_landing_during_the_line_wait_never_reaches_the_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cortex_platform.product.transports.managed_worker import (
            REFUSED_GATE_CLOSED,
            ManagedTransportWorker,
            ManagedWorkerUnavailable,
        )
        from cortex_platform.product.transports.worker_rpc import (
            TELEGRAM_CREDENTIAL_KEY,
            TELEGRAM_SECRET_ALIAS,
        )

        from .test_managed_worker import (
            FAKE_TOKEN,
            _approve,
            _descriptor,
            _FakeService,
            _paths,
        )

        _RecordingBackend.instances = []
        (tmp_path / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
        store = ControlStore(tmp_path / "data" / "control.db", clock=lambda: _NOW)
        store.initialize()
        store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-d8-000001",
        )
        _approve(store)
        monkeypatch.setattr(
            "cortex_platform.product.transports.managed_worker.build_active_descriptor",
            lambda service: _descriptor(tmp_path),
        )
        managed = ManagedTransportWorker(
            store=store,
            paths=_paths(tmp_path),
            config={
                "secret_refs": {TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}"}
            },
            environ={TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN},
            backend_factory=_RecordingBackend,
            service=_FakeService(),
        )
        assert managed.bind().bound is True
        serializer = TransportCallSerializer(send_wait_seconds=5.0)
        rpc = GatedWorkerTransportRPC(
            supervisor=managed, store=store, serializer=serializer
        )
        # A poll, launched through the production path, holds the line.
        assert isinstance(
            rpc.request("telegram.poll", {"offset": None, "timeout_seconds": 0}, timeout=5),
            type(None),
        )
        worker = _RecordingBackend.instances[0].worker_object
        assert worker.methods == ["telegram.poll"]

        held = threading.Event()
        holder = threading.Thread(target=_hold_line, args=(serializer, held), daemon=True)
        holder.start()
        assert held.wait(_JOIN_SECONDS)
        result: dict[str, object] = {}

        def send() -> None:
            try:
                rpc.request("telegram.send", {"chat_id": 1, "text": "x"}, timeout=5)
            except Exception as exc:  # noqa: BLE001 - the type is the assertion
                result["exc"] = exc
            else:
                result["exc"] = None

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        deadline = time.monotonic() + _JOIN_SECONDS
        while serializer.status()["sends_waiting"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert serializer.status()["sends_waiting"] == 1
        # The operator disables while the send is parked on the line.
        store.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="disable-d8-00000001",
        )
        held.clear()
        sender.join(_JOIN_SECONDS)
        holder.join(_JOIN_SECONDS)

        assert isinstance(result["exc"], ManagedWorkerUnavailable)
        assert str(result["exc"]) == REFUSED_GATE_CLOSED
        assert worker.methods == ["telegram.poll"]
        assert serializer.status()["sends_refused"] == 0


class TestWireOperationIdPrecondition:
    """⟦P5.6⟧ Batch D D-10: injective by construction, not by today's caller."""

    def test_a_name_without_the_chunk_tail_is_refused(self) -> None:
        with pytest.raises(ValueError, match="delivery chunk"):
            transport_delivery_chunk_wire_operation_id("telegram.delivery." + "a" * 64)

    def test_a_base_with_its_own_colon_is_refused(self) -> None:
        with pytest.raises(ValueError, match="delivery chunk"):
            transport_delivery_chunk_wire_operation_id("run:7:chunk:0")

    def test_a_base_that_already_carries_a_chunk_segment_is_refused(self) -> None:
        """`x.chunk.0:chunk:1` and `x:chunk:0.chunk.1`-style bases would map onto
        each other's wire names."""

        with pytest.raises(ValueError, match="delivery chunk"):
            transport_delivery_chunk_wire_operation_id("x.chunk.0:chunk:1")

    def test_the_minted_shape_still_maps(self) -> None:
        minted = transport_delivery_chunk_operation_id("telegram.delivery." + "b" * 64, 3)
        assert transport_delivery_chunk_wire_operation_id(minted) == (
            "telegram.delivery." + "b" * 64 + ".chunk.3"
        )
