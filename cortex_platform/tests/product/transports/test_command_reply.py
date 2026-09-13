"""0.1.18 lane B: a handled command must not silently discard its answer.

Before this, `TelegramAdapter.handle_update` computed `/open`'s link, signed a
single-use token for it, registered that token durably, wrote a receipt saying
the command succeeded -- and no byte left the process. The poller's only use of
the result was `getattr(result, "ok", False)`.

These pin the replacement, and the three properties that make it safe to run in
front of a real operator: the reply becomes Control's BEFORE the update is
acknowledged, a duplicate or a restart re-sends the exact bytes and the exact
link rather than minting a second one, and an outcome nobody can speak for is
still never re-sent.
"""

from __future__ import annotations

import sqlite3

import pytest
from dataclasses import replace
from datetime import timedelta

from cortex_platform.product.control import ControlStore, TransportDeliveryKey
from cortex_platform.product.transports import (
    TelegramAdapter,
    TelegramDestination,
    TelegramUpdate,
)
from cortex_platform.product.transports.drain import (
    DestinationDirectory,
    TransportDeliveryDrain,
)
from cortex_platform.product.transports.models import COMMAND_REPLY_PREFIX

from .test_telegram import (  # noqa: F401 - `harness` is a fixture
    Harness,
    ScriptedHermesRPC,
    _adopted_item,
    _callback,
    _hermes_adapter,
    _key,
    _message,
    _running_run,
    _runtime_transition,
    harness,
)

DESTINATION = TelegramDestination(chat_id=-100, topic_id=41)
ACCEPTED = {"status": "accepted", "provider_message_ref": "1001"}


def _adapter(
    harness: Harness,
    rpc: ScriptedHermesRPC,
    *,
    worker_id: str = "reply-worker",
    store: ControlStore | None = None,
    mode: str | None = None,
    inbound_limit: int | None = None,
) -> TelegramAdapter:
    config = harness.adapter.config
    if mode is not None:
        config = replace(config, mode=mode)  # type: ignore[arg-type]
    if inbound_limit is not None:
        config = replace(config, inbound_limit=inbound_limit)
    return _hermes_adapter(
        store=harness.store if store is None else store,
        config=config,
        rpc=rpc,
        worker_id=worker_id,
        clock=harness.clock,
    )


def _directory(harness: Harness) -> DestinationDirectory:
    directory = DestinationDirectory(
        store=harness.store, bot_identity="research-bot", allowed_user_ids=()
    )
    directory.learn(DESTINATION)
    return directory


def _drain(
    harness: Harness, adapter: TelegramAdapter, store: ControlStore | None = None
) -> TransportDeliveryDrain:
    return TransportDeliveryDrain(
        store=harness.store if store is None else store,
        adapter=adapter,
        directory=_directory(harness),
    )


def _replies(store: ControlStore) -> list[tuple[str, str]]:
    """Every command-reply projection in the ledger, with its state."""

    with sqlite3.connect(store.path) as conn:
        return [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT event_id, state FROM transport_delivery_projections "
                "WHERE event_id LIKE ? ORDER BY created_at, operation_id",
                (f"{COMMAND_REPLY_PREFIX}%",),
            )
        ]


def _chunks(store: ControlStore, event_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(store.path) as conn:
        return [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT text, state FROM transport_delivery_chunks "
                "WHERE event_id = ? ORDER BY chunk_index",
                (event_id,),
            )
        ]


def _sent(rpc: ScriptedHermesRPC) -> list[str]:
    return [
        str(params["text"])
        for method, params, _ in rpc.calls
        if method == "telegram.send"
    ]


def _link_of(text: str) -> str:
    return next(word for word in text.split() if word.startswith("https://"))


def _token_of(text: str) -> str:
    return _link_of(text).split("token=", 1)[1].split("&", 1)[0]


def test_open_freezes_its_answer_before_the_receipt_and_the_drain_sends_it(
    harness: Harness,
) -> None:
    """The whole defect, end to end: `/open` now reaches the chat.

    The freeze happens inside `handle_update`, so the delivery exists before
    the receipt that makes this update un-repeatable -- and it is the ordinary
    ledger, so the ordinary drain pass is what sends it. No second sender, no
    second poller, no run invented to carry it.
    """

    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)

    opened = adapter.handle_update(_message(30, "/open"))

    assert opened.ok is True and opened.action == "open"
    # Frozen and owed BEFORE anything was sent: the ledger, not the poller,
    # is what remembers the answer.
    assert _replies(harness.store) == [
        (f"{COMMAND_REPLY_PREFIX}{adapter._idempotency_key('update:30')}", "pending")
    ]
    assert _sent(rpc) == []

    outcomes = _drain(harness, adapter).drain()

    assert [item["category"] for item in outcomes] == ["delivered"]
    assert outcomes[0]["source"] == "command_reply"
    assert outcomes[0]["event_id"].startswith(COMMAND_REPLY_PREFIX)
    # Exactly one message, carrying the same single-use token the operator was
    # told about, rendered as a MarkdownV2 link.
    token = _token_of(opened.response_text)
    assert len(_sent(rpc)) == 1
    assert token in _sent(rpc)[0]
    assert "[Open in Cortex](" in _sent(rpc)[0]
    # Only the certified managed methods were used to do it.
    assert {method for method, _, _ in rpc.calls} == {
        "telegram.capabilities",
        "telegram.send",
    }
    # The token the message carries resolves -- the freeze registered it.
    assert adapter.resolve_deep_link(token) == ("thread", harness.thread["id"])


def test_a_delivered_reply_is_not_sent_again_by_a_later_pass(
    harness: Harness,
) -> None:
    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)
    adapter.handle_update(_message(31, "/status"))
    drain = _drain(harness, adapter)

    assert [item["category"] for item in drain.drain()] == ["delivered"]
    assert drain.status()["replies_delivered"] == 1

    assert drain.drain() == []
    assert len(_sent(rpc)) == 1
    assert [state for _, state in _replies(harness.store)] == ["delivered"]


def test_research_item_answers_through_the_same_ledger_and_no_other_sender(
    harness: Harness, tmp_path
) -> None:
    """R2a's new command owns no send path of its own.

    The selection is frozen as an ordinary command reply before the receipt,
    and the ordinary drain pass is what puts it in the chat -- no raw Bot API
    call, no second poller, and nothing sent twice.
    """

    item = _adopted_item(harness.store, tmp_path / "adopted")
    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)

    selected = adapter.handle_update(_message(34, f"/research-item {item}"))

    assert selected.ok is True and selected.action == "select_research_item"
    assert _replies(harness.store) == [
        (f"{COMMAND_REPLY_PREFIX}{adapter._idempotency_key('update:34')}", "pending")
    ]
    assert _sent(rpc) == []

    outcomes = _drain(harness, adapter).drain()

    assert [item["category"] for item in outcomes] == ["delivered"]
    assert outcomes[0]["source"] == "command_reply"
    assert len(_sent(rpc)) == 1 and "Decoding idea" in _sent(rpc)[0]
    assert {method for method, _, _ in rpc.calls} == {
        "telegram.capabilities",
        "telegram.send",
    }
    # A redelivered update replays the same answer and selects nothing again.
    assert adapter.handle_update(_message(34, f"/research-item {item}")).replayed
    assert _drain(harness, adapter).drain() == []
    assert len(_sent(rpc)) == 1
    assert harness.store.get_research_thread_item(harness.thread["id"])[
        "selection_revision"
    ] == 1


def test_a_duplicate_update_reuses_the_same_reply_and_the_same_link(
    harness: Harness,
) -> None:
    """⟦B-2⟧ A retry must not regenerate a different link.

    The delivery key is derived from the update identity, so the second arrival
    replays the receipt and never reaches the handler -- and even if it did,
    the frozen projection is what the drain sends.
    """

    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)

    first = adapter.handle_update(_message(32, "/open"))
    second = adapter.handle_update(_message(32, "/open"))

    assert second.replayed is True
    assert second.response_text == first.response_text
    assert len(_replies(harness.store)) == 1

    _drain(harness, adapter).drain()

    assert len(_sent(rpc)) == 1
    assert _token_of(first.response_text) in _sent(rpc)[0]


def test_a_reply_one_process_froze_is_delivered_by_the_next_one(
    harness: Harness,
) -> None:
    """⟦B-2⟧ A restart resends the frozen bytes; it invents nothing.

    Also the disable case: while no drain pass runs, the row simply waits. A
    closed window loses nothing, because the ledger is where the answer lives.
    """

    frozen_rpc = ScriptedHermesRPC([])
    froze = _adapter(harness, frozen_rpc, worker_id="worker-before-restart")
    opened = froze.handle_update(_message(33, "/open"))

    # No pass runs at all -- the window is closed, or the daemon died here.
    assert _replies(harness.store) == [
        (f"{COMMAND_REPLY_PREFIX}{froze._idempotency_key('update:33')}", "pending")
    ]
    assert _sent(frozen_rpc) == []

    reopened = ControlStore(harness.store.path, clock=harness.clock)
    reopened.initialize()
    rpc = ScriptedHermesRPC([ACCEPTED])
    recovered = _adapter(
        harness, rpc, worker_id="worker-after-restart", store=reopened
    )

    outcomes = _drain(harness, recovered, store=reopened).drain()

    assert [item["category"] for item in outcomes] == ["delivered"]
    # The exact link the first process told the operator about, not a new one.
    assert _token_of(opened.response_text) in _sent(rpc)[0]
    assert len(_sent(rpc)) == 1


def test_a_rate_limited_reply_is_deferred_and_then_delivered_once(
    harness: Harness,
) -> None:
    rpc = ScriptedHermesRPC(
        [{"status": "rate_limited", "retry_after_ms": 400}, ACCEPTED]
    )
    adapter = _adapter(harness, rpc)
    adapter.handle_update(_message(34, "/status"))
    drain = _drain(harness, adapter)

    limited = drain.drain()

    assert [item["category"] for item in limited] == ["rate_limited"]
    assert limited[0]["delivered"] is False and limited[0]["retryable"] is True
    # Provably before the socket, so the chunk is claimable again rather than
    # stranded -- and the parent is still owed.
    assert _chunks(harness.store, limited[0]["event_id"])[0][1] == "pending"
    assert [state for _, state in _replies(harness.store)] == ["pending"]

    harness.clock.advance(timedelta(seconds=1))
    delivered = drain.drain()

    assert [item["category"] for item in delivered] == ["delivered"]
    assert len(_sent(rpc)) == 2
    assert drain.status()["replies_delivered"] == 1


def test_an_unknown_send_outcome_leaves_the_reply_manual_and_never_resends(
    harness: Harness,
) -> None:
    """⟦P5-01⟧ holds for a reply exactly as it holds for a notification.

    `urllib` cannot say whether bytes were written, so a chunk whose outcome is
    unknown stops there. A reply the operator may already have read is not
    worth sending twice to make a counter tidy.
    """

    rpc = ScriptedHermesRPC([RuntimeError("the worker went away mid-send")])
    adapter = _adapter(harness, rpc)
    adapter.handle_update(_message(35, "/status"))
    drain = _drain(harness, adapter)

    stuck = drain.drain()

    assert [item["category"] for item in stuck] == ["manual_required"]
    assert _chunks(harness.store, stuck[0]["event_id"])[0][1] == "sending_unknown"
    assert [state for _, state in _replies(harness.store)] == ["manual_required"]

    # `manual_required` is excluded from the pending scan, so no later pass
    # re-enters it and no second frame is ever offered.
    assert drain.drain() == []
    assert len(_sent(rpc)) == 1
    assert drain.status()["replies_delivered"] == 0


def test_a_rejected_command_is_answered_once_and_stays_idempotent(
    harness: Harness,
) -> None:
    """A refusal has no receipt, so its delivery key is its whole idempotence.

    The text is deterministic prose, so a redelivered update freezes a
    byte-identical projection and Control replays it instead of answering the
    operator twice.
    """

    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)

    rejected = adapter.handle_update(_message(36, "/frobnicate"))
    again = adapter.handle_update(_message(36, "/frobnicate"))

    assert rejected.ok is False and rejected.category == "invalid_command"
    assert again.category == "invalid_command"
    assert len(_replies(harness.store)) == 1

    outcomes = _drain(harness, adapter).drain()

    assert [item["category"] for item in outcomes] == ["delivered"]
    assert len(_sent(rpc)) == 1
    assert "rejected safely" in _sent(rpc)[0]


def test_a_throttled_update_is_not_answered_at_all(harness: Harness) -> None:
    """Answering every rate-limited update is how a throttle becomes a megaphone."""

    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc, inbound_limit=1)

    assert adapter.handle_update(_message(37, "/status")).ok is True
    blocked = adapter.handle_update(_message(38, "/status"))

    assert blocked.category == "rate_limited"
    # One reply frozen -- the accepted command's -- and none for the refusal.
    assert len(_replies(harness.store)) == 1


def test_an_ordinary_research_message_freezes_no_second_acknowledgement(
    harness: Harness,
) -> None:
    """The answer to a research message is its run, not a "captured" line."""

    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc)

    captured = adapter.handle_update(_message(39, "what did the paper claim?"))

    assert captured.ok is True and captured.action == "capture_message"
    assert _replies(harness.store) == []
    assert _drain(harness, adapter).drain() == []
    assert _sent(rpc) == []


def test_a_notification_still_reaches_the_chat_beside_a_command_reply(
    harness: Harness,
) -> None:
    """Command replies are an addition to the run notification, not a swap."""

    rpc = ScriptedHermesRPC([ACCEPTED, ACCEPTED])
    adapter = _adapter(harness, rpc)
    # Built first: the cursor starts at the head, so the notification below is
    # new work for this pass rather than history.
    drain = _drain(harness, adapter)
    run = _running_run(harness.store, harness.thread)
    _runtime_transition(harness.store, run, "completed", payload={"summary": "done"})
    adapter.handle_update(_message(40, "/status"))

    outcomes = drain.drain()

    assert sorted(str(item["source"]) for item in outcomes) == ["command_reply", "new"]
    assert all(item["delivered"] for item in outcomes)
    assert len(_sent(rpc)) == 2


def test_an_unauthorized_sender_is_answered_by_nothing_at_all(
    harness: Harness,
) -> None:
    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc)

    denied = adapter.handle_update(_message(41, "/open", sender_id=999))

    assert denied.category == "unauthorized"
    assert "999" not in denied.response_text and "-100" not in denied.response_text
    # No delivery, no capability, nothing routable: a reply would itself be a
    # signal that this chat is bound to something.
    assert _replies(harness.store) == []
    assert _drain(harness, adapter).drain() == []


def test_shadow_mode_freezes_nothing(harness: Harness) -> None:
    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc, worker_id="shadow-worker", mode="shadow")

    shadowed = adapter.handle_update(_message(42, "/open"))

    assert shadowed.category == "shadow"
    assert _replies(harness.store) == []
    assert _sent(rpc) == []


def test_an_unbound_root_reply_is_not_frozen_and_keeps_its_issued_link(
    harness: Harness,
) -> None:
    """The documented gap, pinned rather than papered over.

    A delivery key needs a BINDING digest, and an unbound root has none, so
    this reply cannot ride the ledger. Today's behaviour is preserved exactly:
    the token is issued immediately and the link still resolves -- what is
    missing is the send, which is a coordinator contract question.
    """

    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc)

    unbound = adapter.handle_update(_message(43, "/open", topic_id=None))

    assert unbound.category == "binding_required"
    assert _replies(harness.store) == []
    assert adapter.resolve_deep_link(_token_of(unbound.response_text)) == (
        "workspace",
        harness.workspace["id"],
    )


def test_a_reply_whose_scope_this_process_cannot_name_waits_rather_than_dies(
    harness: Harness,
) -> None:
    """⟦F-B2⟧ An unroutable reply is counted and retried, never dropped.

    Control stores an HMAC of the routing scope, so a process that has not yet
    seen an update from that chat cannot propose the destination. The row stays
    pending and the next pass -- with a learned candidate -- delivers it.
    """

    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)
    adapter.handle_update(_message(44, "/status"))
    blind = DestinationDirectory(
        store=harness.store, bot_identity="research-bot", allowed_user_ids=()
    )
    drain = TransportDeliveryDrain(
        store=harness.store, adapter=adapter, directory=blind
    )

    assert drain.drain() == []
    assert drain.status()["unroutable"] == 1
    assert _sent(rpc) == []

    blind.learn(DESTINATION)

    assert [item["category"] for item in drain.drain()] == ["delivered"]
    assert len(_sent(rpc)) == 1


def test_a_reply_offered_to_the_wrong_chat_is_refused_before_any_send(
    harness: Harness,
) -> None:
    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc)
    adapter.handle_update(_message(45, "/status"))
    event_id, _ = _replies(harness.store)[0]
    binding = harness.store.resolve_transport(
        transport="telegram",
        external_scope=adapter._scope(-100, 41).canonical(),  # noqa: SLF001
    )
    assert binding is not None

    misrouted = adapter.deliver_command_reply(
        delivery_key=TransportDeliveryKey(
            transport="telegram",
            destination_digest=str(binding["external_scope"]),
            event_id=event_id,
            projection_version=1,
        ),
        destination=TelegramDestination(chat_id=-100, topic_id=None),
    )

    assert misrouted.category == "binding_mismatch"
    assert misrouted.delivered is False
    assert _sent(rpc) == []
    # The claim was given straight back, so the right destination still gets it.
    assert _chunks(harness.store, event_id)[0][1] == "pending"


def test_a_reply_that_cannot_be_frozen_is_refused_and_leaves_no_receipt(
    harness: Harness,
) -> None:
    """Fail loudly rather than acknowledge an update whose answer is lost.

    No receipt is written. The poller recognizes this typed failure and leaves
    the update unconfirmed so a later poll can retry persistence.
    """

    rpc = ScriptedHermesRPC(
        [], capability_outcome=RuntimeError("the worker cannot be asked")
    )
    adapter = _adapter(harness, rpc)

    refused = adapter.handle_update(_message(46, "/status"))

    assert refused.ok is False and refused.category == "reply_not_persisted"
    assert refused.retryable is True
    assert _replies(harness.store) == []
    # No receipt either: Control never claims to have handled a command whose
    # answer it does not own. The same update offered again is handled afresh
    # rather than replaying a stored "ok".
    update = TelegramUpdate.parse(_message(46, "/status"))
    assert (
        adapter._receipts.command_result(  # noqa: SLF001
            adapter._idempotency_key(update.identity),  # noqa: SLF001
            adapter._request_digest(  # noqa: SLF001
                update,
                adapter._private_scope_digest(  # noqa: SLF001
                    adapter._scope(-100, 41)  # noqa: SLF001
                ),
            ),
        )
        is None
    )


def test_help_is_answered_when_bound_and_only_computed_when_not(
    harness: Harness,
) -> None:
    """`/help` in a bound topic reaches the chat; at an unbound root it cannot.

    The second half is the documented gap, not an accident: an unbound scope
    has no `transport_bindings` row, and a delivery key's `destination_digest`
    must be a binding digest.
    """

    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)

    bound = adapter.handle_update(_message(48, "/help"))
    unbound = adapter.handle_update(_message(49, "/help", topic_id=None))

    assert bound.action == "help" and unbound.category == "binding_required"
    assert len(_replies(harness.store)) == 1

    assert [item["category"] for item in _drain(harness, adapter).drain()] == [
        "delivered"
    ]
    assert len(_sent(rpc)) == 1
    assert "/open" in _sent(rpc)[0]


def test_a_decision_button_confirmation_is_delivered_too(
    harness: Harness,
) -> None:
    """A callback is a command, and its confirmation travels the same way."""

    rpc = ScriptedHermesRPC([ACCEPTED, ACCEPTED])
    adapter = _adapter(harness, rpc, worker_id="callback-worker")
    run = _running_run(harness.store, harness.thread)
    harness.store.create_decision(
        run_id=run["id"],
        attempt_id=run["attempt_id"],
        runtime_binding_id=run["runtime_binding_id"],
        runtime_release_id=run["runtime_release_id"],
        state_generation_id=run["state_generation_id"],
        expected_revision=run["revision"],
        kind="research_gate",
        prompt="Choose the next research stage",
        options=[{"id": "approve", "label": "Approve"}],
        actor_id="runtime",
        idempotency_key=_key("reply-decision"),
    )
    event = harness.store.list_run_events(run["id"])[-1]
    assert adapter.deliver_event(event=event, destination=DESTINATION).delivered
    buttons = next(
        params["buttons"]
        for method, params, _ in rpc.calls
        if method == "telegram.send"
    )
    assert isinstance(buttons, list)

    resolved = adapter.handle_update(
        _callback(50, "reply-callback", str(buttons[0]["callback_data"]))
    )

    assert resolved.ok is True
    assert len(_replies(harness.store)) == 1

    outcomes = _drain(harness, adapter).drain()

    assert [item["category"] for item in outcomes] == ["delivered"]
    assert "committed in Cortex" in _sent(rpc)[1]


@pytest.mark.parametrize("text", ["/open", "/unknown-command"])
def test_poller_retries_a_reply_until_it_is_frozen(harness: Harness, text: str) -> None:
    from .test_worker_rpc import FakeWorker, _open_window
    from cortex_platform.product.transports.worker_rpc import TelegramInboundPoller

    _open_window(harness.store)
    rpc = ScriptedHermesRPC([ACCEPTED], capability_outcome=RuntimeError("temporarily unavailable"))
    adapter = _adapter(harness, rpc)
    worker = FakeWorker({"telegram.poll": {"status": "ok", "updates": [_message(80, text)]}})
    results = []

    def handle(update):
        result = adapter.handle_update(update)
        results.append(result)
        rpc._capability_outcome = None
        return result

    poller = TelegramInboundPoller(
        rpc=worker, store=harness.store, handle_update=handle,
        long_poll_seconds=0, sleep=lambda _: None,
    )
    assert poller.run(max_iterations=2) == "bounded"
    assert results[0].category == "reply_not_persisted"
    assert results[0].retryable is True
    assert results[1].category != "reply_not_persisted"
    assert [frame[1]["offset"] for frame in worker.frames] == [None, None]
    assert poller.offset == 81
    assert poller.handled == 1
    assert len(_replies(harness.store)) == 1
    assert _drain(harness, adapter).drain()[0]["delivered"] is True
    assert len(_sent(rpc)) == 1


def test_shadow_adapter_does_not_send_a_previously_frozen_active_reply(harness: Harness) -> None:
    rpc = ScriptedHermesRPC([ACCEPTED])
    active = _adapter(harness, rpc)
    assert active.handle_update(_message(81, "/open")).ok
    shadow = _adapter(harness, rpc, mode="shadow")
    outcome = _drain(harness, shadow).drain()[0]
    assert outcome["category"] == "shadow"
    assert outcome["delivered"] is False
    assert _sent(rpc) == []
    assert _replies(harness.store)[0][1] == "pending"
    assert _drain(harness, active).drain()[0]["delivered"] is True
