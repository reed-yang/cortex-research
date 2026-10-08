"""The weekly XHS fallback's Telegram digest.

One message per completed run that left items to the operator, sent through
the ordinary delivery ledger under `xhs-fallback:<run_id>`: frozen before any
send, resumed after a restart, never re-sent after an unknown outcome, and
never sent by opening the gate. Synthetic identities only; the Hermes RPC is
scripted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.transports import TelegramAdapter, TelegramDestination
from cortex_platform.product.transports.drain import (
    DestinationDirectory,
    TransportDeliveryDrain,
)
from cortex_platform.product.transports.models import XHS_DIGEST_PREFIX
from cortex_platform.tests.product.control.test_xhs_fallback_store import (
    _decide,
    _rec,
    _saved,
    _start,
)
from cortex_platform.tests.product.control.test_xhs_store import NOTE, _follow

from .test_telegram import (  # noqa: F401 - `harness` is a fixture
    Harness,
    ScriptedHermesRPC,
    _hermes_adapter,
    _key,
    harness,
)

ORIGIN = "https://cortex.example.com"
OPERATOR = 7
ACCEPTED = {"status": "accepted", "provider_message_ref": "2001"}


def _adapter(
    harness: Harness,
    rpc: ScriptedHermesRPC,
    *,
    store: ControlStore | None = None,
    worker_id: str = "digest-worker",
    allowed: frozenset[int] = frozenset({OPERATOR}),
    mode: str = "active",
) -> TelegramAdapter:
    config = replace(
        harness.adapter.config,
        allowed_user_ids=allowed,
        mode=mode,  # type: ignore[arg-type]
    )
    return _hermes_adapter(
        store=harness.store if store is None else store,
        config=config,
        rpc=rpc,
        worker_id=worker_id,
        clock=harness.clock,
    )


def _drain(
    adapter: TelegramAdapter,
    *,
    store: ControlStore,
    web_origin: str | None = ORIGIN,
    known: frozenset[int] | None = None,
) -> TransportDeliveryDrain:
    return TransportDeliveryDrain(
        store=store,
        adapter=adapter,
        directory=DestinationDirectory(
            store=store,
            bot_identity="research-bot",
            allowed_user_ids=adapter.config.allowed_user_ids if known is None else known,
        ),
        web_origin=web_origin,
    )


def _bind_private(harness: Harness, user_id: int = OPERATOR) -> None:
    """The operator's own private chat, bound at its root."""

    harness.adapter.bind_destination(
        destination=TelegramDestination(chat_id=user_id, topic_id=None),
        thread_id=harness.thread["id"],
        idempotency_key=_key(f"bind-private-{user_id}"),
    )


def _completed_run(
    store: ControlStore, titles: list[str], *, excluded: int = 0
) -> dict[str, Any]:
    """A completed weekly run: `excluded` items excluded, the rest left to
    the operator, in the order of `titles`."""

    _follow(store)
    _saved(store)
    for index, title in enumerate(titles):
        _rec(store, f"blog:{index:03d}", "blog", title)
    run = _start(store)["run"]
    for index in range(len(titles)):
        item = _decide(store)
        decision = (
            {"action": "exclude", "reason_code": "not_a_blog", "reason": "A course."}
            if index < excluded
            else {"action": "needs_operator", "reason_code": "insufficient_evidence"}
        )
        store.apply_xhs_fallback_result(
            item["id"], expected_revision=item["revision"], decision=decision
        )
    return store.get_xhs_fallback_run(run["id"])


def _sent(rpc: ScriptedHermesRPC) -> list[dict[str, Any]]:
    return [dict(params) for method, params, _ in rpc.calls if method == "telegram.send"]


def _digest(store: ControlStore, run_id: str) -> tuple[str, str | None]:
    run = store.get_xhs_fallback_run(run_id)
    return run["digest_state"], run["digest_reason"]


def _ledger(store: ControlStore, run_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(store.path) as conn:
        return [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT event_id, state FROM transport_delivery_projections "
                "WHERE event_id = ?",
                (f"{XHS_DIGEST_PREFIX}{run_id}",),
            )
        ]


def _digest_events(store: ControlStore) -> list[str]:
    with sqlite3.connect(store.path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT payload_json FROM control_audit WHERE type = 'xhs.fallback.digest' "
                "ORDER BY cursor"
            )
        ]


def test_a_run_left_to_the_operator_sends_one_digest_through_the_ledger(
    harness: Harness,
) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks", "Efficient Streaming"])
    assert _digest(store, run["id"]) == ("pending", None)
    _bind_private(harness)
    rpc = ScriptedHermesRPC([ACCEPTED])
    drain = _drain(_adapter(harness, rpc), store=store)

    outcomes = drain.drain()

    assert [(o["event_id"], o["source"], o["category"]) for o in outcomes] == [
        (f"{XHS_DIGEST_PREFIX}{run['id']}", "xhs_digest", "delivered")
    ]
    sent = _sent(rpc)
    assert len(sent) == 1
    assert sent[0]["text"] == (
        "XHS recommendations: 2 need your decision\\.\n"
        "• Attention Sinks\n"
        "• Efficient Streaming\n"
        "Open: https://cortex\\.example\\.com/?view\\=inbox"
    )
    # The operator's private chat, at its root, with no button and no token.
    assert (sent[0]["chat_id"], sent[0]["topic_id"], sent[0]["buttons"]) == (
        OPERATOR, None, [],
    )
    assert "token=" not in sent[0]["text"]
    assert _digest(store, run["id"]) == ("sent", None)
    assert _ledger(store, run["id"]) == [(f"{XHS_DIGEST_PREFIX}{run['id']}", "delivered")]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_chunk_capabilities"
        ).fetchone()[0] == 0
    assert len(_digest_events(store)) == 1
    # Nothing is owed any more.
    assert drain.drain() == []
    assert len(_sent(rpc)) == 1


def test_many_items_fit_one_chunk_naming_three_short_titles(harness: Harness) -> None:
    store = harness.store
    titles = [f"Title {index:02d} " + "long words " * 12 for index in range(25)]
    run = _completed_run(store, titles)
    _bind_private(harness)
    rpc = ScriptedHermesRPC([ACCEPTED])

    _drain(_adapter(harness, rpc), store=store).drain()

    sent = _sent(rpc)
    assert len(sent) == 1
    lines = sent[0]["text"].split("\n")
    assert lines[0] == "XHS recommendations: 25 need your decision\\."
    bullets = [line for line in lines if line.startswith("• ")]
    assert len(bullets) == 3 and len(lines) == 5
    for bullet in bullets:
        title = bullet.removeprefix("• ")
        assert len(title) == 60 and title.endswith("…")
    assert _digest(store, run["id"]) == ("sent", None)


def test_titles_are_escaped_and_redacted(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(
        store,
        [
            "a_b*c [x](y) ~`>#+-=|{}.!",
            "Read it at https://blog.example/post",
            "api_key=abcdef0123456789",
        ],
    )
    _bind_private(harness)
    rpc = ScriptedHermesRPC([ACCEPTED])

    _drain(_adapter(harness, rpc), store=store).drain()

    text = _sent(rpc)[0]["text"]
    assert "• a\\_b\\*c \\[x\\]\\(y\\) \\~\\`\\>\\#\\+\\-\\=\\|\\{\\}\\.\\!\n" in text
    assert "• Read it at \\[redacted\\]\n" in text
    assert "blog\\.example" not in text and "abcdef0123456789" not in text
    assert _digest(store, run["id"]) == ("sent", None)


def test_no_item_left_to_the_operator_suppresses_the_digest(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(store, ["A course", "Another course"], excluded=2)
    assert _digest(store, run["id"]) == ("suppressed", None)
    _bind_private(harness)
    rpc = ScriptedHermesRPC([])

    assert _drain(_adapter(harness, rpc), store=store).drain() == []
    assert rpc.calls == [] and _ledger(store, run["id"]) == []


def test_items_the_operator_handled_before_the_send_are_not_announced(
    harness: Harness,
) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    recommendation = store.get_xhs_recommendation(
        store.list_xhs_fallback_items(run["id"])[0]["recommendation_id"]
    )
    store.exclude_xhs_recommendation(
        note_source_id=store.get_xhs_note(NOTE)["source_id"],
        recommendation_id=recommendation["id"],
        reason="Not worth reading", expected_revision=recommendation["revision"],
        actor_id="local-operator", idempotency_key="exclude-digest-0001",
    )
    _bind_private(harness)
    rpc = ScriptedHermesRPC([])

    assert _drain(_adapter(harness, rpc), store=store).drain() == []
    assert rpc.calls == []
    assert _digest(store, run["id"]) == ("suppressed", None)
    assert len(_digest_events(store)) == 1


def test_a_missing_web_origin_keeps_the_digest_pending(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    _bind_private(harness)
    rpc = ScriptedHermesRPC([ACCEPTED])
    adapter = _adapter(harness, rpc)

    assert _drain(adapter, store=store, web_origin=None).drain() == []
    assert rpc.calls == [] and _ledger(store, run["id"]) == []
    assert _digest(store, run["id"]) == ("pending", "web_origin_missing")

    _drain(adapter, store=store).drain()
    assert len(_sent(rpc)) == 1
    assert _digest(store, run["id"]) == ("sent", None)


def test_no_bound_operator_or_several_keep_the_digest_pending(harness: Harness) -> None:
    """The harness binds a group topic; a digest never goes there."""

    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    rpc = ScriptedHermesRPC([])

    assert _drain(_adapter(harness, rpc), store=store).drain() == []
    assert _digest(store, run["id"]) == ("pending", "recipient_unavailable")

    _bind_private(harness, OPERATOR)
    _bind_private(harness, 8)
    both = _adapter(harness, rpc, allowed=frozenset({OPERATOR, 8}))
    assert _drain(both, store=store).drain() == []
    assert _digest(store, run["id"]) == ("pending", "recipient_ambiguous")
    assert rpc.calls == [] and _ledger(store, run["id"]) == []
    assert _digest_events(store) == []


def test_shadow_mode_and_a_closed_gate_keep_the_digest_pending(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    _bind_private(harness)
    rpc = ScriptedHermesRPC([])
    drain = _drain(_adapter(harness, rpc, mode="shadow"), store=store)

    assert drain.drain() == []
    assert _digest(store, run["id"]) == ("pending", "shadow")

    revision = store.get_xhs_fallback_run(run["id"])["revision"]
    drain.gate_closed()
    assert _digest(store, run["id"]) == ("pending", "transport_disabled")
    # A second closed tick writes nothing, and no tick opens the gate.
    drain.gate_closed()
    assert store.get_xhs_fallback_run(run["id"])["revision"] == revision + 1
    assert store.transport_activation("telegram") is None
    assert rpc.calls == [] and _ledger(store, run["id"]) == []


def test_a_daemon_started_after_the_run_completed_still_sends_it(
    harness: Harness,
) -> None:
    """The run row is the work source: a new daemon's cursor starts at the
    event head, and the digest is found anyway."""

    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    _bind_private(harness)
    reopened = ControlStore(store.path, clock=harness.clock)
    reopened.initialize()
    rpc = ScriptedHermesRPC([ACCEPTED])

    _drain(_adapter(harness, rpc, store=reopened), store=reopened).drain()

    assert len(_sent(rpc)) == 1
    assert _digest(reopened, run["id"]) == ("sent", None)


def test_a_restart_after_the_freeze_sends_the_frozen_message(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks", "Efficient Streaming"])
    _bind_private(harness)
    refused = ScriptedHermesRPC([{"status": "retryable_before_send"}])

    first = _drain(_adapter(harness, refused, worker_id="before-restart"), store=store).drain()

    assert [o["category"] for o in first] == ["transport_unavailable"]
    assert _ledger(store, run["id"]) == [(f"{XHS_DIGEST_PREFIX}{run['id']}", "pending")]
    assert _digest(store, run["id"]) == ("pending", None)
    frozen_text = _sent(refused)[0]["text"]
    # What the digest would say now differs; the frozen bytes are what go out.
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE xhs_recommendations SET title = 'Renamed'")

    reopened = ControlStore(store.path, clock=harness.clock)
    reopened.initialize()
    rpc = ScriptedHermesRPC([ACCEPTED])
    _drain(_adapter(harness, rpc, store=reopened, worker_id="after-restart"),
           store=reopened).drain()

    assert [params["text"] for params in _sent(rpc)] == [frozen_text]
    assert "Renamed" not in frozen_text
    assert _digest(reopened, run["id"]) == ("sent", None)


def test_an_unknown_send_outcome_blocks_the_digest_for_good(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    _bind_private(harness)
    rpc = ScriptedHermesRPC([RuntimeError("the worker went away mid-send")])
    drain = _drain(_adapter(harness, rpc), store=store)

    stuck = drain.drain()

    assert [o["category"] for o in stuck] == ["manual_required"]
    assert _ledger(store, run["id"]) == [
        (f"{XHS_DIGEST_PREFIX}{run['id']}", "manual_required")
    ]
    assert _digest(store, run["id"]) == ("blocked", "outcome_unknown")
    assert len(_digest_events(store)) == 1
    assert drain.drain() == []
    assert len(_sent(rpc)) == 1


def test_a_frozen_digest_is_never_sent_to_another_recipient(harness: Harness) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    _bind_private(harness, OPERATOR)
    refused = ScriptedHermesRPC([{"status": "retryable_before_send"}])
    _drain(_adapter(harness, refused), store=store).drain()
    assert _ledger(store, run["id"]) == [(f"{XHS_DIGEST_PREFIX}{run['id']}", "pending")]

    # The allowlist now names someone else, who is bound too.
    _bind_private(harness, 8)
    rpc = ScriptedHermesRPC([ACCEPTED])
    other = _adapter(harness, rpc, allowed=frozenset({8}))

    assert _drain(other, store=store).drain() == []
    assert rpc.calls == []
    assert _digest(store, run["id"]) == ("pending", "recipient_unavailable")
    assert len(_ledger(store, run["id"])) == 1


@pytest.mark.parametrize("bad", ["https://cortex.example.com/", "http://cortex.example.com"])
def test_the_digest_link_is_only_ever_the_configured_https_origin(
    harness: Harness, bad: str
) -> None:
    store = harness.store
    run = _completed_run(store, ["Attention Sinks"])
    _bind_private(harness)
    rpc = ScriptedHermesRPC([])
    adapter = _adapter(harness, rpc)

    result = adapter.deliver_xhs_digest(
        run_id=run["id"], count=1, titles=["Attention Sinks"], web_origin=bad
    )

    assert (result.category, result.delivered) == ("projection_failure", False)
    assert _sent(rpc) == [] and _ledger(store, run["id"]) == []
