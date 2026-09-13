"""P5.4b: the outbound half the daemon was missing.

P5.4a's real run recorded it plainly -- `TelegramAdapter.deliver_event` had no
production call site, so `cortexd` could receive and could not send. These pin
the loop that closes it, and the two properties that make it safe to run inside
an operator-present window: it never re-sends something whose outcome is
unknown, and a restart in the middle of a delivery resumes rather than
duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

import pytest

from cortex_platform.product.transports.drain import (
    DestinationDirectory,
    TransportDeliveryDrain,
)
from cortex_platform.product.transports.models import (
    TelegramDestination,
    TelegramScope,
)
from cortex_platform.product.transports.telegram import TelegramAdapterConfig

from .test_telegram import (  # noqa: F401 - `harness` is a fixture
    Harness,
    ScriptedHermesRPC,
    _hermes_adapter,
    _key,
    _running_run,
    _runtime_transition,
    harness,
)

DESTINATION = TelegramDestination(chat_id=-100, topic_id=41)


@dataclass(frozen=True)
class TelegramAdapterResult:
    """The two fields `learning_update_handler` reads off an adapter result."""

    ok: bool
    category: str | None


def _directory(harness: Harness, *, learn: bool = True) -> DestinationDirectory:
    directory = DestinationDirectory(
        store=harness.store, bot_identity="research-bot", allowed_user_ids=()
    )
    if learn:
        directory.learn(DESTINATION)
    return directory


def _completed(harness: Harness, summary: str = "a finished turn") -> dict:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(
        harness.store, run, "completed", payload={"summary": summary}
    )
    return harness.store.list_run_events(completed["id"])[-1]


def test_the_directory_confirms_a_candidate_against_control_never_guesses(
    harness: Harness,
) -> None:
    """Control stores an HMAC of the scope; the daemon proposes and it confirms."""

    directory = _directory(harness)
    scope = TelegramScope(bot_identity="research-bot", chat_id=-100, topic_id=41)
    binding = harness.store.resolve_transport(
        transport="telegram", external_scope=scope.canonical()
    )
    assert binding is not None
    resolved = directory.for_digest(binding["external_scope"])
    assert resolved is not None
    assert resolved[0] == DESTINATION
    assert resolved[1] == harness.thread["id"]
    assert directory.for_thread(harness.thread["id"]) == DESTINATION
    # A digest no candidate matches is unanswered rather than approximated.
    assert directory.for_digest("hmac-sha256:" + "0" * 64) is None


def test_the_directory_learns_a_scope_from_an_inbound_update(
    harness: Harness,
) -> None:
    directory = _directory(harness, learn=False)
    assert directory.for_thread(harness.thread["id"]) is None
    directory.learn_from_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 3,
                "message_thread_id": 41,
                "chat": {"id": -100, "type": "supergroup"},
            },
        }
    )
    assert directory.for_thread(harness.thread["id"]) == DESTINATION


def test_a_new_notification_event_is_delivered_once_and_then_deduped(
    harness: Harness,
) -> None:
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=harness.adapter, directory=_directory(harness)
    )
    event = _completed(harness)
    outcomes = drain.drain()
    assert [item["category"] for item in outcomes] == ["delivered"]
    assert outcomes[0]["event_id"] == event["id"]
    assert len(harness.client.messages) == 1
    assert drain.status()["delivered"] == 1
    # The cursor moved past it, and a second pass finds no work at all.
    assert drain.drain() == []
    assert len(harness.client.messages) == 1


def test_a_daemon_start_does_not_deliver_the_history_it_starts_after(
    harness: Harness,
) -> None:
    """A backlog dump into the operator's chat is the failure this prevents."""

    _completed(harness, "an event from before this daemon existed")
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=harness.adapter, directory=_directory(harness)
    )
    assert drain.drain() == []
    assert harness.client.messages == []
    assert drain.cursor == harness.store.latest_event_cursor()


def test_an_event_whose_thread_has_no_binding_is_passed_over(
    harness: Harness,
) -> None:
    drain = TransportDeliveryDrain(
        store=harness.store,
        adapter=harness.adapter,
        directory=_directory(harness, learn=False),
    )
    _completed(harness)
    assert drain.drain() == []
    assert harness.client.messages == []


def _hermes(harness: Harness, rpc: ScriptedHermesRPC, worker_id: str):
    return _hermes_adapter(
        store=harness.store,
        config=replace(
            TelegramAdapterConfig(
                bot_identity="research-bot",
                signing_key=b"a" * 32,
                allowed_user_ids=frozenset({7}),
                base_url="https://cortex.test/open",
                mode="active",
                default_workspace_id=harness.workspace["id"],
            ),
        ),
        rpc=rpc,
        worker_id=worker_id,
        clock=harness.clock,
    )


def test_a_restart_mid_delivery_resumes_from_the_ledger_without_duplicating(
    harness: Harness,
) -> None:
    """⟦b2⟧ The permits and receipts are the truth; the drain holds no queue."""

    sends: list[dict] = []
    first_rpc = ScriptedHermesRPC(
        send_outcomes=[
            {"status": "accepted", "provider_message_ref": "9001"},
            # The provider proved it did NOT act on the second chunk, so that
            # chunk goes back to `pending` and the parent stays `pending`: the
            # exact shape a daemon killed mid-delivery leaves behind.
            {"status": "rate_limited", "retry_after_ms": 1},
        ],
        on_send=sends.append,
    )
    first = _hermes(harness, first_rpc, "worker-one")
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=first, directory=_directory(harness)
    )
    event = _completed(harness, "y" * 1_200)  # two chunks: one sent, one not
    outcomes = drain.drain()
    assert outcomes and outcomes[0]["delivered"] is False
    assert len(sends) == 2
    pending = harness.store.pending_transport_deliveries(transport="telegram")
    assert [row["event_id"] for row in pending] == [event["id"]]

    # A brand new daemon: a fresh cursor at the head, a fresh adapter, a fresh
    # worker id. The only thing it inherits is the ledger.
    harness.clock.advance(timedelta(seconds=1))
    resumed_sends: list[dict] = []
    second_rpc = ScriptedHermesRPC(
        send_outcomes=[
            {"status": "accepted", "provider_message_ref": "9002"},
            {"status": "accepted", "provider_message_ref": "9003"},
        ],
        on_send=resumed_sends.append,
    )
    second = _hermes(harness, second_rpc, "worker-two")
    resumed = TransportDeliveryDrain(
        store=harness.store, adapter=second, directory=_directory(harness)
    )
    outcomes = resumed.drain()
    assert [item["source"] for item in outcomes] == ["resumed"]
    assert outcomes[0]["category"] == "delivered"
    # Exactly the chunk that had not been sent, and not the one that had.
    assert len(resumed_sends) == 1
    assert harness.store.pending_transport_deliveries(transport="telegram") == []
    # And a third pass has nothing left to do.
    assert resumed.drain() == []
    assert len(resumed_sends) == 1


def test_an_unknown_outcome_becomes_manual_required_and_is_never_re_sent(
    harness: Harness,
) -> None:
    """⟦b3, P5-01⟧ `urllib` cannot say whether bytes were written."""

    sends: list[dict] = []
    rpc = ScriptedHermesRPC(
        send_outcomes=[TimeoutError("provider did not answer")], on_send=sends.append
    )
    adapter = _hermes(harness, rpc, "worker-unknown")
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=adapter, directory=_directory(harness)
    )
    _completed(harness)
    outcomes = drain.drain()
    assert [item["category"] for item in outcomes] == ["manual_required"]
    assert len(sends) == 1
    # The parent left `pending` for `manual_required`, so the drain's own work
    # source no longer names it -- there is no path back to a second send.
    assert harness.store.pending_transport_deliveries(transport="telegram") == []
    assert drain.drain() == []
    assert len(sends) == 1
    assert drain.status()["categories"] == {"manual_required": 1}
    assert drain.status()["last_outcome"]["category"] == "manual_required"


def test_a_pending_delivery_no_candidate_matches_is_counted_not_guessed(
    harness: Harness,
) -> None:
    rpc = ScriptedHermesRPC(
        send_outcomes=[
            {"status": "accepted", "provider_message_ref": "9100"},
            {"status": "rate_limited", "retry_after_ms": 1},
        ]
    )
    adapter = _hermes(harness, rpc, "worker-lost")
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=adapter, directory=_directory(harness)
    )
    _completed(harness, "z" * 1_200)
    drain.drain()
    assert harness.store.pending_transport_deliveries(transport="telegram")

    blind = TransportDeliveryDrain(
        store=harness.store,
        adapter=adapter,
        directory=_directory(harness, learn=False),
    )
    assert blind.drain() == []
    assert blind.status()["unroutable"] == 1


def test_the_status_surface_carries_counts_and_never_a_message_body(
    harness: Harness,
) -> None:
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=harness.adapter, directory=_directory(harness)
    )
    _completed(harness, "the body of a message nobody should read here")
    drain.drain()
    rendered = repr(drain.status())
    assert "nobody should read here" not in rendered
    assert drain.status()["categories"] == {"delivered": 1}


# -- ⟦F-B2⟧ the cursor is a high-water mark, not a delivery receipt -----------


class _FailsFirst:
    """An adapter that cannot deliver until it can."""

    def __init__(self, inner, failures: int) -> None:
        self._inner = inner
        self.remaining = failures
        self.calls = 0

    def deliver_event(self, *, event, destination):
        self.calls += 1
        if self.remaining:
            self.remaining -= 1
            raise RuntimeError("the worker went away mid-pass")
        return self._inner.deliver_event(event=event, destination=destination)


def test_an_event_whose_delivery_raised_is_kept_for_the_next_pass(
    harness: Harness,
) -> None:
    """⟦F-B2⟧ The cursor moved past it and nothing ever looked at it again.

    `_drain_locked` swallows the exception, so before this the operator saw a
    turn answered, `drain.delivered` flat, and no number anywhere saying an
    event had been dropped.
    """

    adapter = _FailsFirst(harness.adapter, failures=1)
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=adapter, directory=_directory(harness)
    )
    _completed(harness, "the answer the operator is waiting for")

    assert drain.drain() == []
    assert drain.status()["retrying"] == 1
    assert drain.status()["categories"] == {"deliver_raised": 1}

    outcomes = drain.drain()
    assert [item["source"] for item in outcomes] == ["retried"]
    assert outcomes[0]["delivered"] is True
    assert drain.status()["retrying"] == 0
    assert drain.status()["retried"] == 1
    # And it is not delivered a third time.
    assert drain.drain() == []
    assert adapter.calls == 2


def test_an_event_no_scope_is_bound_to_is_counted_and_never_held(
    harness: Harness,
) -> None:
    """⟦F-B2⟧ Most runs are not bound to a chat, so holding here stalls forever.

    Counted instead, distinctly from `unroutable` (which is a pending ledger row
    whose digest no candidate matches) -- and the cursor keeps moving, so a
    newer event that IS routable is still delivered in the same pass.
    """

    drain = TransportDeliveryDrain(
        store=harness.store,
        adapter=harness.adapter,
        directory=_directory(harness, learn=False),
    )
    _completed(harness, "an answer with nowhere to go")
    assert drain.drain() == []
    assert drain.status()["undeliverable"] == 1
    assert drain.status()["retrying"] == 0

    # Re-reading the same event does not double count it.
    assert drain.drain() == []
    assert drain.status()["undeliverable"] == 1


def test_the_retry_set_is_bounded_and_says_when_it_dropped_something(
    harness: Harness,
) -> None:
    """A retry set that could grow without limit would be a queue."""

    from cortex_platform.product.transports import drain as drain_module

    drain = TransportDeliveryDrain(
        store=harness.store, adapter=harness.adapter, directory=_directory(harness)
    )
    original = drain_module.RETRY_LIMIT
    drain_module.RETRY_LIMIT = 2
    try:
        for index in range(4):
            drain._remember(f"event-{index}")  # noqa: SLF001
    finally:
        drain_module.RETRY_LIMIT = original

    status = drain.status()
    assert status["retrying"] == 2
    assert status["retries_dropped"] == 2
    # The oldest go first: a held-back event that four passes could not place
    # is less likely to matter than the one that arrived a second ago, and the
    # operator can see how many were let go.
    assert list(drain._retry) == ["event-2", "event-3"]  # noqa: SLF001


# -- ⟦F-B4⟧ what a pass costs, and who may teach it a destination -------------


def test_one_pass_asks_control_once_per_candidate_not_once_per_event(
    harness: Harness,
) -> None:
    """⟦F-B4⟧ `for_digest` and `for_thread` each walked every candidate.

    A 200-event pass over N candidates was 200xN indexed queries, and it ran on
    the same thread `close_window` needs: the refuter measured a 49.5 s pass
    blocking `stop_poller()` for 49.2 s.
    """

    directory = _directory(harness)
    for chat_id in range(-120, -100):
        directory.learn(TelegramDestination(chat_id=chat_id, topic_id=None))
    calls = 0
    inner = harness.store.resolve_transport

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return inner(**kwargs)

    harness.store.resolve_transport = counted  # type: ignore[method-assign]
    try:
        drain = TransportDeliveryDrain(
            store=harness.store, adapter=harness.adapter, directory=directory
        )
        _completed(harness, "an answer")
        outcomes = drain.drain()
    finally:
        harness.store.resolve_transport = inner  # type: ignore[method-assign]

    assert [item["delivered"] for item in outcomes] == [True]
    # One question per candidate for the whole pass, plus the adapter's own for
    # the delivery it made -- and not one per candidate per event.
    assert calls <= len(directory.candidates()) + len(outcomes)


def test_a_pass_that_runs_out_of_time_leaves_the_rest_for_the_next_tick(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain holds the lock `close_window` needs, so a pass is bounded."""

    from cortex_platform.product.transports import drain as drain_module

    drain = TransportDeliveryDrain(
        store=harness.store, adapter=harness.adapter, directory=_directory(harness)
    )
    _completed(harness, "an answer nobody waits five seconds for")
    monkeypatch.setattr(drain_module, "MAX_PASS_SECONDS", -1.0)

    assert drain.drain() == []
    assert drain.status()["deadline_stops"] >= 1
    assert drain.status()["delivered"] == 0

    monkeypatch.setattr(drain_module, "MAX_PASS_SECONDS", 5.0)
    assert [item["delivered"] for item in drain.drain()] == [True]


def test_only_an_accepted_update_teaches_the_directory_a_destination(
    harness: Harness,
) -> None:
    """⟦F-B4⟧ `_authorize` is the fence, and learning used to happen ahead of it."""

    from cortex_platform.product.transports.drain import learning_update_handler

    directory = DestinationDirectory(
        store=harness.store, bot_identity="research-bot", allowed_user_ids=()
    )
    seen: list[bool] = []

    def refuse(update):
        seen.append(False)
        return TelegramAdapterResult(ok=False, category="unauthorized")

    def accept(update):
        seen.append(True)
        return TelegramAdapterResult(ok=True, category=None)

    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": -100, "type": "supergroup"},
            "message_thread_id": 41,
        },
    }

    learning_update_handler(directory, refuse)(update)
    assert directory.candidates() == ()

    learning_update_handler(directory, accept)(update)
    assert directory.candidates() == (DESTINATION,)
    assert seen == [False, True]


def test_unroutable_counts_distinct_rows_rather_than_ticks(harness: Harness) -> None:
    """⟦F-B7⟧ A pass runs once a second, so one stuck row read ~3600/hour.

    The number an operator read during a window was a tick count wearing a
    delivery count's name.
    """

    rpc = ScriptedHermesRPC(
        send_outcomes=[
            {"status": "accepted", "provider_message_ref": "9200"},
            {"status": "rate_limited", "retry_after_ms": 1},
        ]
    )
    adapter = _hermes(harness, rpc, "worker-ticks")
    seeded = TransportDeliveryDrain(
        store=harness.store, adapter=adapter, directory=_directory(harness)
    )
    _completed(harness, "z" * 1_200)
    seeded.drain()
    assert harness.store.pending_transport_deliveries(transport="telegram")

    blind = TransportDeliveryDrain(
        store=harness.store,
        adapter=adapter,
        directory=_directory(harness, learn=False),
    )
    for _ in range(5):
        blind.drain()

    assert blind.status()["unroutable"] == 1


def test_the_status_snapshot_is_taken_under_the_drain_s_own_lock(
    harness: Harness,
) -> None:
    """⟦F-B10⟧ `self.counts[category] += 1` can resize the dict mid-iteration.

    `ControlAPI._transport_window_status` catches that and returns None, so a
    transient read error was indistinguishable from "the daemon lost its
    window" -- and it was likeliest exactly when the drain first recorded a
    category the operator cares about.
    """

    import threading as _threading

    drain = TransportDeliveryDrain(
        store=harness.store, adapter=harness.adapter, directory=_directory(harness)
    )

    def churn() -> None:
        for index in range(2_000):
            with drain._status_lock:  # noqa: SLF001
                drain.counts[f"category-{index}"] += 1

    writer = _threading.Thread(target=churn)
    writer.start()
    try:
        while writer.is_alive():
            assert isinstance(drain.status()["categories"], dict)
    finally:
        writer.join(timeout=10)

    # The snapshot is a copy, so nothing the reconcile thread does afterwards
    # can change what the API thread already answered with.
    snapshot = drain.status()["categories"]
    drain.counts["afterwards"] += 1
    assert "afterwards" not in snapshot


def test_the_drain_tells_the_transport_line_whether_it_still_owes_a_message(
    harness: Harness,
) -> None:
    """⟦P5.5⟧ How long the poller may park `getUpdates` is this loop's answer.

    Nothing else in the daemon knows whether Control still holds outbound work,
    and a 30 s poll opened while a reply is waiting is what stranded the fifth
    window's answer inside the certified worker.
    """

    from cortex_platform.product.transports.worker_rpc import TransportCallSerializer

    serializer = TransportCallSerializer()
    drain = TransportDeliveryDrain(
        store=harness.store,
        adapter=harness.adapter,
        directory=_directory(harness),
        serializer=serializer,
    )

    assert drain.drain() == []
    assert serializer.poll_seconds(30) == 30

    _completed(harness)
    assert [item["category"] for item in drain.drain()] == ["delivered"]
    # Still short for one more tick: claiming idle the instant a chunk lands
    # puts a 30 s poll back in front of the next one.
    assert serializer.poll_seconds(30) == 1

    assert drain.drain() == []
    assert serializer.poll_seconds(30) == 30



def test_a_delivery_that_raised_leaves_its_message_on_the_status_surface(
    harness: Harness,
) -> None:
    """⟦P5.6⟧ `deliver_raised: 1` said an exception happened, and nothing said which."""

    adapter = _FailsFirst(harness.adapter, failures=1)
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=adapter, directory=_directory(harness)
    )
    event = _completed(harness, "the answer the operator is waiting for")

    assert drain.drain() == []
    status = drain.status()
    assert status["failures"] == 1
    assert status["last_failure"]["error"] == "RuntimeError"
    assert status["last_failure"]["detail"] == "the worker went away mid-pass"
    assert status["last_failure"]["event_id"] == str(event["id"])
    assert status["last_failure"]["at"].endswith("Z")


def test_a_chunk_deferred_by_control_is_not_outbound_work_the_poller_waits_for(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.6⟧ Batch D D-3: a real 429 flood-wait latched `outbound_pending` True.

    Every pass re-read the deferred chunk, Control answered `deferred` again,
    and the drain reported work it could not act on for the whole
    `retry_not_before` interval -- so the poller short-polled at 1 s for an
    hour with nothing to send.
    """

    from cortex_platform.product.transports.models import DeliveryResult
    from cortex_platform.product.transports.worker_rpc import TransportCallSerializer

    class _Deferring:
        def __init__(self, inner) -> None:
            self._inner = inner
            self.deferrals = 0

        def deliver_event(self, *, event, destination):
            self.deferrals += 1
            return DeliveryResult(
                category="rate_limited",
                delivered=False,
                retryable=True,
                retry_after_ms=600_000,
            )

    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        "cortex_platform.product.transports.drain.time.monotonic",
        lambda: clock["now"],
    )
    serializer = TransportCallSerializer()
    adapter = _Deferring(harness.adapter)
    drain = TransportDeliveryDrain(
        store=harness.store,
        adapter=adapter,
        directory=_directory(harness),
        serializer=serializer,
    )
    _completed(harness)

    outcomes = drain.drain()
    assert [item["category"] for item in outcomes] == ["rate_limited"]
    assert outcomes[0]["retry_after_ms"] == 600_000
    # Held for the next pass, but not reported as work the line must yield for.
    assert drain.status()["retrying"] == 1
    assert serializer.poll_seconds(30) == 30
    assert serializer.status()["outbound_pending"] is False

    # ⟦Batch F P56-OBS-7⟧ The next passes do NOT re-attempt it: the deferral
    # governs the attempt as well as the poll length. It stays held, and the
    # poll stays long.
    for _ in range(3):
        drain.drain()
    assert adapter.deferrals == 1
    assert drain.status()["retrying"] == 1
    assert serializer.poll_seconds(30) == 30

    # Once the flood-wait has elapsed it is attempted again.
    clock["now"] += 600.0
    drain.drain()
    assert adapter.deferrals == 2
    assert drain.status()["retrying"] == 1


def test_a_pre_socket_refusal_is_still_outbound_work(harness: Harness) -> None:
    """The retryable outcome WITHOUT a delay keeps the poller short: it is the
    line's own refusal, and the next tick can act on it."""

    from cortex_platform.product.transports.models import DeliveryResult
    from cortex_platform.product.transports.worker_rpc import TransportCallSerializer

    class _Refusing:
        def deliver_event(self, *, event, destination):
            return DeliveryResult(
                category="transport_refused_before_send",
                delivered=False,
                retryable=True,
            )

    serializer = TransportCallSerializer()
    drain = TransportDeliveryDrain(
        store=harness.store,
        adapter=_Refusing(),
        directory=_directory(harness),
        serializer=serializer,
    )
    _completed(harness)
    drain.drain()
    assert drain.status()["retrying"] == 1
    assert serializer.poll_seconds(30) == 1


def test_a_pass_that_dies_before_its_report_tells_the_line_nothing_is_actionable(
    harness: Harness,
) -> None:
    """⟦P5.6⟧ Batch D D-3: `_drain_locked` swallows the exception, so a pass that
    raised before `outbound_pending` left the previous answer standing."""

    from cortex_platform.product.transports.worker_rpc import TransportCallSerializer

    class _BrokenDirectory:
        def resolved(self):
            raise RuntimeError("control is unreadable")

    serializer = TransportCallSerializer()
    serializer.outbound_pending(True)
    drain = TransportDeliveryDrain(
        store=harness.store,
        adapter=harness.adapter,
        directory=_BrokenDirectory(),
        serializer=serializer,
    )
    with pytest.raises(RuntimeError):
        drain.drain()
    assert serializer.status()["outbound_pending"] is False
