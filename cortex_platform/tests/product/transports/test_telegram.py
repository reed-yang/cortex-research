from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from cortex_platform.product import transports as transports_module
from cortex_platform.product.control import ControlStore, InvalidTransition
from cortex_platform.product.control.research_store import research_item_id
from cortex_platform.product.research.documents import ROOT_ID as RESEARCH_DOCUMENT_ROOT
from cortex_platform.product.transports import (
    InMemoryOpaqueTargetPort,
    InMemoryTransportReceiptPort,
    OpaqueTokenService,
    SyntheticTelegramClient,
    TelegramAdapter,
    TelegramAdapterConfig,
    TelegramDestination,
    TelegramPermanentFailure,
    TelegramRateLimited,
    TelegramTemporaryFailure,
    TelegramUpdate,
    TransportProblem,
    chunk_markdown_v2,
)
from cortex_platform.product.transports.models import TelegramScope
from cortex_platform.product.transports.ports import DeliveryKey
from cortex_platform.product.transports.worker_rpc import TelegramRefusedBeforeSend


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


class ScriptedHermesRPC:
    def __init__(
        self,
        send_outcomes: list[object],
        *,
        on_send: Callable[[Mapping[str, object]], None] | None = None,
        capability_outcome: object | None = None,
    ) -> None:
        self._send_outcomes = deque(send_outcomes)
        self._on_send = on_send
        self._capability_outcome = capability_outcome
        self.calls: list[tuple[str, Mapping[str, object], float]] = []

    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> object:
        self.calls.append((method, params, timeout))
        if method == "telegram.capabilities":
            outcome = self._capability_outcome or {
                "protocol": "cortex.telegram.transport/1",
                "send_message": True,
                "topics": True,
                "inline_buttons": True,
                "markdown_v2": True,
                "provider_idempotency": False,
                "outcome_query": False,
            }
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if method != "telegram.send" or not self._send_outcomes:
            raise AssertionError("unexpected Hermes RPC")
        if self._on_send is not None:
            self._on_send(params)
        outcome = self._send_outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@dataclass
class Harness:
    store: ControlStore
    adapter: TelegramAdapter
    client: SyntheticTelegramClient
    receipts: InMemoryTransportReceiptPort
    token_store: InMemoryOpaqueTargetPort
    tokens: OpaqueTokenService
    clock: MutableClock
    workspace: dict
    thread: dict


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    clock = MutableClock(datetime(2026, 7, 23, 12, 0, tzinfo=UTC))
    store = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    store.initialize()
    workspace = store.create_workspace(
        title="Research", actor_id="local", idempotency_key=_key("workspace")
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Echo / Helios",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key("thread"),
    ).value
    client = SyntheticTelegramClient()
    receipts = InMemoryTransportReceiptPort()
    token_store = InMemoryOpaqueTargetPort()
    tokens = OpaqueTokenService(
        signing_key=b"t" * 32,
        store=token_store,
        clock=clock,
    )
    adapter = _adapter(
        store=store,
        client=client,
        receipts=receipts,
        tokens=tokens,
        clock=clock,
        workspace_id=workspace["id"],
    )
    adapter.bind_destination(
        destination=TelegramDestination(chat_id=-100, topic_id=41),
        thread_id=thread["id"],
        idempotency_key=_key("bind-topic"),
    )
    return Harness(
        store=store,
        adapter=adapter,
        client=client,
        receipts=receipts,
        token_store=token_store,
        tokens=tokens,
        clock=clock,
        workspace=workspace,
        thread=thread,
    )


def _adapter(
    *,
    store: ControlStore,
    client: SyntheticTelegramClient,
    receipts: InMemoryTransportReceiptPort,
    tokens: OpaqueTokenService,
    clock: MutableClock,
    workspace_id: str,
    mode: str = "active",
    inbound_limit: int = 30,
) -> TelegramAdapter:
    return TelegramAdapter(
        store=store,
        config=TelegramAdapterConfig(
            bot_identity="research-bot",
            signing_key=b"a" * 32,
            allowed_user_ids=frozenset({7}),
            base_url="https://cortex.test/open",
            mode=mode,  # type: ignore[arg-type]
            default_workspace_id=workspace_id,
            inbound_limit=inbound_limit,
        ),
        client=client,
        receipts=receipts,
        tokens=tokens,
        clock=clock,
    )


def _hermes_adapter(
    *,
    store: ControlStore,
    config: TelegramAdapterConfig,
    rpc: ScriptedHermesRPC,
    worker_id: str,
    clock: MutableClock,
) -> TelegramAdapter:
    return TelegramAdapter.hermes_control_owned(
        store=store,
        config=replace(config, identity_key=b"i" * 32),
        rpc=rpc,
        worker_id=worker_id,
        clock=clock,
    )


def _key(value: str) -> str:
    return f"telegram-{value}-00000000"


def test_config_rejects_base_url_that_cannot_fit_a_frozen_chunk() -> None:
    with pytest.raises(ValueError, match="base_url must contain at most 512 bytes"):
        TelegramAdapterConfig(
            bot_identity="research-bot",
            signing_key=b"a" * 32,
            allowed_user_ids=frozenset({7}),
            base_url="https://cortex.test/" + "x" * 512,
        )


def _message(
    update_id: int,
    text: str | None,
    *,
    chat_id: int = -100,
    topic_id: int | None = 41,
    sender_id: int = 7,
    media: dict | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id + 100,
        "from": {"id": sender_id},
        "chat": {"id": chat_id, "type": "supergroup"},
    }
    if topic_id is not None:
        message["message_thread_id"] = topic_id
    if text is not None:
        message["text"] = text
    if media is not None:
        message.update(media)
    return {"update_id": update_id, "message": message}


def _callback(
    update_id: int,
    callback_id: str,
    data: str,
    *,
    chat_id: int = -100,
    topic_id: int | None = 41,
    sender_id: int = 7,
) -> dict:
    message: dict = {
        "message_id": 900,
        "chat": {"id": chat_id, "type": "supergroup"},
    }
    if topic_id is not None:
        message["message_thread_id"] = topic_id
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": sender_id},
            "message": message,
            "data": data,
        },
    }


def _running_run(store: ControlStore, thread: dict) -> dict:
    current_thread = store.get_thread(thread["id"])
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=current_thread["revision"],
        actor_id="local",
        idempotency_key=_key(f"run-{thread['id']}"),
    ).value
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        dispatch_owner="telegram-test-worker",
        runtime_release_id="synthetic-release",
        state_generation_id="synthetic-generation",
        runtime_slot_id="synthetic-slot",
        runtime_artifact_digest="synthetic-artifact",
        runtime_worker_protocol="synthetic-protocol",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key(f"reserve-{thread['id']}"),
    ).value
    binding = store.create_runtime_binding(
        thread_id=thread["id"],
        adapter_id="synthetic-hermes",
        runtime_session_ref=f"private-{thread['id']}",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key=_key(f"runtime-{thread['id']}"),
    ).value
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        runtime_binding_id=binding["id"],
        runtime_release_id="synthetic-release",
        state_generation_id="synthetic-generation",
        dispatch_owner="telegram-test-worker",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key(f"pin-{thread['id']}"),
    ).value
    identity = {
        "attempt_id": run["attempt"]["id"],
        "runtime_binding_id": binding["id"],
        "runtime_release_id": "synthetic-release",
        "state_generation_id": "synthetic-generation",
    }
    for state in ("starting", "running"):
        run = store.apply_runtime_transition(
            run_id=run["id"],
            target_state=state,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=_key(f"{state}-{thread['id']}"),
            **identity,
        ).value
    return {**run, **identity}


def _runtime_transition(
    store: ControlStore, run: dict, target_state: str, *, payload: dict | None = None
) -> dict:
    result = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["attempt_id"],
        runtime_binding_id=run["runtime_binding_id"],
        runtime_release_id=run["runtime_release_id"],
        state_generation_id=run["state_generation_id"],
        target_state=target_state,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key(f"{target_state}-{run['id']}"),
        payload=payload,
    ).value
    return {
        **result,
        "attempt_id": run["attempt_id"],
        "runtime_binding_id": run["runtime_binding_id"],
        "runtime_release_id": run["runtime_release_id"],
        "state_generation_id": run["state_generation_id"],
    }


def test_update_parser_rejects_ambiguous_duplicate_oversized_and_deep_json() -> None:
    with pytest.raises(TransportProblem, match="ambiguous_update"):
        TelegramUpdate.parse(
            json.dumps(
                {
                    **_message(1, "hello"),
                    "callback_query": _callback(1, "cb", "x")["callback_query"],
                }
            )
        )
    with pytest.raises(TransportProblem, match="duplicate_json_key"):
        TelegramUpdate.parse('{"update_id":1,"update_id":2,"message":{"message_id":1}}')
    with pytest.raises(TransportProblem, match="update_too_large"):
        TelegramUpdate.parse(b"{" + b" " * 1_000_000 + b"}")
    nested: object = "end"
    for _ in range(18):
        nested = {"next": nested}
    payload = _message(1, "hello")
    payload["extra"] = nested
    with pytest.raises(TransportProblem, match="update_too_complex"):
        TelegramUpdate.parse(payload)
    with pytest.raises(TransportProblem, match="invalid_request"):
        TelegramUpdate.parse(_callback(2, "bad\ncallback", "token"))
    with pytest.raises(TransportProblem, match="invalid_request"):
        TelegramUpdate.parse(_callback(3, "callback", "x" * 65))


def test_capture_replay_and_adapter_restart_append_once(harness: Harness) -> None:
    update = _message(1, "Research Echo-Infinity with TTT")
    first = harness.adapter.handle_update(update)
    replay = harness.adapter.handle_update(update)
    assert first.ok is True and first.mutated is True
    assert replay.ok is True and replay.replayed is True
    assert len(harness.store.list_messages(harness.thread["id"])) == 1

    rebuilt = _adapter(
        store=harness.store,
        client=harness.client,
        receipts=harness.receipts,
        tokens=harness.tokens,
        clock=harness.clock,
        workspace_id=harness.workspace["id"],
    )
    after_restart = rebuilt.handle_update(update)
    assert after_restart.replayed is True
    assert len(harness.store.list_messages(harness.thread["id"])) == 1

    drift = rebuilt.handle_update(_message(1, "different payload"))
    assert drift.category == "idempotency_conflict"
    assert len(harness.store.list_messages(harness.thread["id"])) == 1


def _adopted_item(
    store: ControlStore,
    root: Path,
    *,
    kind: str = "idea",
    origin_id: str = "idea-decoding",
    title: str = "Decoding idea",
) -> str:
    """One adopted research item, registered the way adoption registers one.

    `/research-item` never reads a dossier -- it only rewrites which adopted
    item the thread is about -- so the registered bytes need not exist here.
    """

    try:
        store.register_asset_root(
            root_id=RESEARCH_DOCUMENT_ROOT,
            private_path=root,
            max_bytes=1_048_576,
            enabled=True,
            actor_id="local",
            idempotency_key=_key("research-docs-root"),
        )
    except InvalidTransition:
        pass
    item_id = research_item_id(kind, origin_id)
    store.register_research_documents(
        item={"id": item_id, "kind": kind, "origin_id": origin_id, "title": title},
        documents=[
            {
                "title": title,
                "asset_root_id": RESEARCH_DOCUMENT_ROOT,
                "relative_path": f"{origin_id}/dossier.md",
                "origin_relative_path": f"{kind}/{origin_id}.md",
                "media_type": "text/markdown",
                "byte_length": 64,
                "sha256": "a" * 64,
            }
        ],
        actor_id="local",
        idempotency_key=_key(f"adopt-{origin_id}"),
    )
    return item_id


def test_research_item_selects_an_adopted_dossier_without_any_other_mutation(
    harness: Harness, tmp_path: Path
) -> None:
    item = _adopted_item(harness.store, tmp_path / "adopted")
    before = harness.store.get_thread(harness.thread["id"])

    result = harness.adapter.handle_update(_message(60, f"/research-item {item}"))

    assert result.ok is True and result.action == "select_research_item"
    assert result.mutated is True and result.replayed is False
    assert result.revision == 1
    assert item in result.response_text and "Decoding idea" in result.response_text
    # The operator is told what a selection actually changes, and how to use it.
    assert "/research" in result.response_text and "/chat" in result.response_text
    selected = harness.store.get_research_thread_item(harness.thread["id"])
    assert selected["id"] == item and selected["selection_revision"] == 1
    # Selection appends no message, starts no run and moves no thread revision.
    assert harness.store.list_messages(harness.thread["id"]) == []
    assert harness.store.get_thread(harness.thread["id"]) == before


def test_research_item_alias_replays_once_and_reselection_bumps_the_revision(
    harness: Harness, tmp_path: Path
) -> None:
    idea = _adopted_item(harness.store, tmp_path / "adopted")
    project = _adopted_item(
        harness.store,
        tmp_path / "adopted",
        kind="project",
        origin_id="project-robotics",
        title="Robotics project",
    )
    update = _message(61, f"/research_item {idea}")

    first = harness.adapter.handle_update(update)
    replay = harness.adapter.handle_update(update)

    assert first.ok is True and first.revision == 1
    assert replay.ok is True and replay.replayed is True
    assert harness.store.get_research_thread_item(harness.thread["id"])["id"] == idea

    switched = harness.adapter.handle_update(_message(62, f"/research-item {project}"))

    assert switched.ok is True and switched.revision == 2
    selected = harness.store.get_research_thread_item(harness.thread["id"])
    assert selected["id"] == project and selected["selection_revision"] == 2
    assert "Robotics project" in switched.response_text


def test_shadow_mode_answers_research_item_without_selecting_anything(
    harness: Harness, tmp_path: Path
) -> None:
    item = _adopted_item(harness.store, tmp_path / "adopted")
    shadow = _adapter(
        store=harness.store,
        client=SyntheticTelegramClient(),
        receipts=InMemoryTransportReceiptPort(),
        tokens=harness.tokens,
        clock=harness.clock,
        workspace_id=harness.workspace["id"],
        mode="shadow",
    )

    result = shadow.handle_update(_message(63, f"/research-item {item}"))

    assert result.category == "shadow" and result.mutated is False
    assert result.action == "select_research_item"
    assert harness.store.get_research_thread_item(harness.thread["id"]) is None


def test_an_unadopted_research_item_is_refused_and_selects_nothing(
    harness: Harness, tmp_path: Path
) -> None:
    _adopted_item(harness.store, tmp_path / "adopted")
    unknown = "ri_" + "b" * 32

    result = harness.adapter.handle_update(_message(64, f"/research-item {unknown}"))

    assert result.ok is False and result.category == "not_found"
    assert harness.store.get_research_thread_item(harness.thread["id"]) is None


def test_research_item_is_refused_while_a_run_of_the_thread_is_active(
    harness: Harness, tmp_path: Path
) -> None:
    """A packet is chosen per run; the item cannot change under a live one."""

    item = _adopted_item(harness.store, tmp_path / "adopted")
    _running_run(harness.store, harness.thread)

    result = harness.adapter.handle_update(_message(65, f"/research-item {item}"))

    assert result.ok is False and result.category == "control_store_error"
    assert harness.store.get_research_thread_item(harness.thread["id"]) is None


@pytest.mark.parametrize(
    "argument",
    [
        "",
        "idea-decoding",
        "ri_" + "b" * 31,
        "ri_" + "B" * 32,
        "ri_" + "b" * 32 + " and something else",
        "ri_" + "b" * 32 + "\n/research-item " + "ri_" + "c" * 32,
    ],
)
def test_research_item_needs_exactly_one_adopted_item_id(
    harness: Harness, argument: str
) -> None:
    result = harness.adapter.handle_update(
        _message(66, f"/research-item {argument}".strip())
    )

    assert result.ok is False and result.category == "invalid_command"
    assert harness.store.get_research_thread_item(harness.thread["id"]) is None


def test_the_widened_command_grammar_keeps_the_existing_commands(
    harness: Harness, tmp_path: Path
) -> None:
    """`-` and `_` are now legal in a command name, and nothing else changed."""

    item = _adopted_item(harness.store, tmp_path / "adopted")
    # A group chat addresses the bot by username; that suffix grammar is the
    # one this file already had, and the widened name still parses in front.
    mentioned = harness.adapter.handle_update(
        _message(67, f"/research-item@research_bot {item}")
    )
    assert mentioned.ok is True and mentioned.action == "select_research_item"

    helped = harness.adapter.handle_update(_message(68, "/help"))
    assert helped.action == "help" and "/research-item" in helped.response_text
    assert helped.mutated is False

    assert harness.adapter.handle_update(_message(69, "/status")).action == "status"
    assert harness.adapter.handle_update(_message(70, "/open")).action == "open"
    for unknown in ("/9lives", "/-dash", "/", "/research-item-extra x"):
        refused = harness.adapter.handle_update(_message(71, unknown))
        assert refused.ok is False and refused.category == "invalid_command", unknown
    captured = harness.adapter.handle_update(_message(72, "/chat plain question"))
    assert captured.ok is True and captured.action == "capture_message"


def test_control_owned_inbound_receipt_replays_sanitized_result_after_restart(
    harness: Harness,
) -> None:
    client = SyntheticTelegramClient()
    adapter = TelegramAdapter.control_owned(
        store=harness.store,
        config=harness.adapter.config,
        client=client,
        worker_id="telegram-worker-one",
        clock=harness.clock,
    )
    raw_text = "private inbound text that must not enter the receipt"
    update = _message(130, raw_text)
    first = adapter.handle_update(update)
    assert first.ok is True and first.mutated is True

    reopened_store = ControlStore(harness.store.path, clock=harness.clock)
    reopened_store.initialize()
    rebuilt = TelegramAdapter.control_owned(
        store=reopened_store,
        config=harness.adapter.config,
        client=SyntheticTelegramClient(),
        worker_id="telegram-worker-two",
        clock=harness.clock,
    )
    replay = rebuilt.handle_update(update)
    assert replay.ok is True and replay.replayed is True
    assert len(reopened_store.list_messages(harness.thread["id"])) == 1
    with sqlite3.connect(harness.store.path) as conn:
        persisted = conn.execute(
            "SELECT response_json FROM transport_command_receipts"
        ).fetchone()[0]
    assert raw_text not in persisted


def test_distinct_concurrent_captures_retry_revision_without_loss(
    harness: Harness,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                harness.adapter.handle_update,
                (_message(101, "first"), _message(102, "second")),
            )
        )
    assert all(item.ok for item in outcomes)
    assert {
        item["content"] for item in harness.store.list_messages(harness.thread["id"])
    } == {
        "first",
        "second",
    }


def test_topic_bindings_isolate_capture_and_status(harness: Harness) -> None:
    workspace = harness.store.get_workspace(harness.workspace["id"])
    other = harness.store.create_thread(
        workspace_id=workspace["id"],
        title="Other topic",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key("other-thread"),
    ).value
    harness.adapter.bind_destination(
        destination=TelegramDestination(chat_id=-100, topic_id=42),
        thread_id=other["id"],
        idempotency_key=_key("bind-other"),
    )

    captured = harness.adapter.handle_update(_message(2, "topic 42 only", topic_id=42))
    status = harness.adapter.handle_update(_message(3, "/status", topic_id=41))
    unbound = harness.adapter.handle_update(_message(4, "/status", topic_id=43))
    assert captured.ok is True
    assert harness.store.list_messages(harness.thread["id"]) == []
    assert [item["content"] for item in harness.store.list_messages(other["id"])] == [
        "topic 42 only"
    ]
    assert "Echo / Helios" in status.response_text
    assert unbound.category == "binding_required"
    assert "-100" not in unbound.response_text and "43" not in unbound.response_text


def test_status_pause_cancel_and_ambiguous_decision_text(harness: Harness) -> None:
    run = _running_run(harness.store, harness.thread)
    status = harness.adapter.handle_update(_message(5, "/status"))
    paused = harness.adapter.handle_update(_message(6, "/pause"))
    canceled = harness.adapter.handle_update(_message(7, "/cancel"))
    ambiguous = harness.adapter.handle_update(_message(8, "/approve"))
    assert status.state == "running"
    assert paused.state == "pause_requested"
    assert canceled.state == "cancel_requested"
    assert harness.store.get_run(run["id"])["state"] == "cancel_requested"
    assert ambiguous.category == "ambiguous_decision"


@pytest.mark.parametrize("command", ["/pause", "/cancel"])
def test_a_bound_engine_carrier_is_not_ended_from_telegram(
    harness: Harness, command: str
) -> None:
    """⟦ADJ-4⟧ `_apply_run_action` writes through the store, and the store refuses.

    No production path binds a transport to a capture carrier's thread, so
    the rows are written by hand: the bound thread's active run is the
    engine's carrier (created by `MACHINE_ACTOR`, owning the capture
    workflow). The command is refused typed (`machine_run`), the run is not
    moved and its revision is unchanged -- the N-1 invariant now holds for
    every writer, not only the control API's route.
    """

    from cortex_platform.product.engine.capture_consumer import (
        MACHINE_ACTOR,
        RESEARCH_CAPTURE_WORKFLOW,
    )

    thread = harness.store.get_thread(harness.thread["id"])
    carrier = harness.store.create_run(
        thread_id=thread["id"],
        expected_revision=int(thread["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key=_key("carrier-run"),
        workflow=RESEARCH_CAPTURE_WORKFLOW,
    ).value

    result = harness.adapter.handle_update(_message(9, command))

    assert result.ok is False
    assert result.category == "machine_run"
    assert "research engine" in result.response_text
    after = harness.store.get_run(carrier["id"])
    assert after["state"] == "queued"
    assert after["revision"] == carrier["revision"]
    assert harness.store.get_thread(thread["id"])["active_run_id"] == carrier["id"]


def test_resume_uses_canonical_checkpointed_run_transition(harness: Harness) -> None:
    run = _running_run(harness.store, harness.thread)
    requested = harness.adapter.handle_update(_message(110, "/pause"))
    checkpointed = harness.store.commit_checkpoint(
        run_id=run["id"],
        attempt_id=run["attempt_id"],
        runtime_binding_id=run["runtime_binding_id"],
        runtime_release_id=run["runtime_release_id"],
        state_generation_id=run["state_generation_id"],
        checkpoint_uri="cortex://artifacts/checkpoints/telegram-resume.json",
        expected_revision=requested.revision,
        actor_id="runtime",
        idempotency_key=_key("resume-checkpoint"),
    ).value
    harness.store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["attempt_id"],
        runtime_binding_id=run["runtime_binding_id"],
        runtime_release_id=run["runtime_release_id"],
        state_generation_id=run["state_generation_id"],
        target_state="paused",
        expected_revision=checkpointed["revision"],
        actor_id="runtime",
        idempotency_key=_key("resume-paused"),
    )
    resumed = harness.adapter.handle_update(_message(111, "/resume"))
    assert resumed.ok is True
    assert resumed.state == "resuming"
    assert harness.store.get_run(run["id"])["state"] == "resuming"


def test_media_is_untrusted_and_never_promoted_or_leaked(harness: Harness) -> None:
    file_id = "private-provider-file-reference"
    result = harness.adapter.handle_update(
        _message(
            9,
            None,
            media={
                "document": {
                    "file_id": file_id,
                    "file_unique_id": "stable-private-ref",
                    "file_name": "paper.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 12_345,
                }
            },
        )
    )
    assert result.category == "source_staging_unavailable"
    assert file_id not in result.response_text
    assert harness.store.list_messages(harness.thread["id"]) == []


def test_decision_buttons_use_control_store_cas_and_reject_forwarded_topic(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    decision = harness.store.create_decision(
        run_id=run["id"],
        attempt_id=run["attempt_id"],
        runtime_binding_id=run["runtime_binding_id"],
        runtime_release_id=run["runtime_release_id"],
        state_generation_id=run["state_generation_id"],
        expected_revision=run["revision"],
        kind="research_gate",
        prompt="Proceed without leaking sk-supersecretvalue or /Users/operator/data?",
        options=[
            {"id": "approve", "label": "Approve"},
            {"id": "deny", "label": "Deny"},
        ],
        actor_id="runtime",
        idempotency_key=_key("decision"),
    ).value
    event = harness.store.list_run_events(run["id"])[-1]
    delivered = harness.adapter.deliver_event(
        event=event, destination=TelegramDestination(chat_id=-100, topic_id=41)
    )
    assert delivered.delivered is True
    outbound = harness.client.messages[-1]
    assert len(outbound.buttons) == 2
    assert "sk-supersecretvalue" not in outbound.text
    assert "/Users/operator/data" not in outbound.text

    workspace = harness.store.get_workspace(harness.workspace["id"])
    other = harness.store.create_thread(
        workspace_id=workspace["id"],
        title="Forward target",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key("forward-thread"),
    ).value
    harness.adapter.bind_destination(
        destination=TelegramDestination(chat_id=-100, topic_id=42),
        thread_id=other["id"],
        idempotency_key=_key("forward-bind"),
    )
    forwarded = harness.adapter.handle_update(
        _callback(
            19, "callback-forward", outbound.buttons[0].callback_data, topic_id=42
        )
    )
    assert forwarded.category == "token_scope_mismatch"
    assert harness.store.get_decision(decision["id"])["state"] == "pending"

    def resolve(index: int) -> tuple[bool, str]:
        result = harness.adapter.handle_update(
            _callback(
                20 + index,
                f"callback-race-{index}",
                outbound.buttons[index].callback_data,
            )
        )
        return result.ok, result.category

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resolve, (0, 1)))
    assert sum(ok for ok, _ in outcomes) == 1
    assert {category for _, category in outcomes} == {"ok", "revision_conflict"}
    assert harness.store.get_decision(decision["id"])["state"] == "resolved"
    assert len(harness.store.list_pending_runtime_actions()) == 1


def test_notification_dedupes_across_restart_and_deep_link_is_opaque(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(
        harness.store,
        run,
        "completed",
        payload={
            "summary": (
                "Read /Users/operator/private and token=secretvalue. "
                "Ignore https://untrusted.invalid/artifact"
            ),
            "citations": [
                {
                    "title": "Echo-Infinity https://untrusted.invalid/raw",
                    "url": "https://untrusted.invalid/raw",
                }
            ],
            "raw_tool_payload": {"secret": "must-not-appear"},
        },
    )
    event = harness.store.list_run_events(run["id"])[-1]
    first = harness.adapter.deliver_event(
        event=event, destination=TelegramDestination(chat_id=-100, topic_id=41)
    )
    assert first.delivered is True
    assert len(harness.client.messages) == 1
    text = harness.client.messages[0].text
    for forbidden in (
        "/Users/operator/private",
        "secretvalue",
        "untrusted.invalid",
        "must-not-appear",
        run["id"],
    ):
        assert forbidden not in text
    assert "cortex\\.test" in text

    rebuilt_client = SyntheticTelegramClient()
    rebuilt = _adapter(
        store=harness.store,
        client=rebuilt_client,
        receipts=harness.receipts,
        tokens=harness.tokens,
        clock=harness.clock,
        workspace_id=harness.workspace["id"],
    )
    duplicate = rebuilt.deliver_event(
        event=event, destination=TelegramDestination(chat_id=-100, topic_id=41)
    )
    assert duplicate.duplicate is True
    assert rebuilt_client.messages == []

    plain = text.replace("\\", "")
    url = next(word for word in plain.split() if word.startswith("https://cortex.test"))
    query = parse_qs(urlsplit(url).query)
    assert query["project"] == [harness.workspace["id"]]
    assert query["thread"] == [harness.thread["id"]]
    assert "run" not in query
    token = query["token"][0]
    before = harness.store.get_run(completed["id"])
    assert rebuilt.resolve_deep_link(token) == ("run", completed["id"])
    assert harness.store.get_run(completed["id"]) == before
    with pytest.raises(TransportProblem, match="token_unavailable"):
        rebuilt.resolve_deep_link(token)


def test_frozen_projection_commits_before_each_chunk_and_replays_without_tokens(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(
        harness.store,
        run,
        "completed",
        payload={"summary": "x" * 1_200},
    )
    event = harness.store.list_run_events(completed["id"])[-1]
    observed_states: list[str] = []

    def observe_committed_state(params: Mapping[str, object]) -> None:
        # ⟦P5.5⟧ The frame carries the colon-free WIRE projection of the ledger
        # id, because the certified worker cannot echo a colon in an error
        # reply. Control still keys the chunk by `<delivery>:chunk:<n>`, so the
        # lookup applies the same mapping rather than expecting the two names
        # to be one -- and asserts, by matching at all, that they correspond.
        wire = str(params["operation_id"])
        assert ":" not in wire
        with sqlite3.connect(harness.store.path) as conn:
            row = conn.execute(
                "SELECT state FROM transport_delivery_chunks "
                "WHERE event_id = ? AND REPLACE(operation_id, ':', '.') = ?",
                (event["id"], wire),
            ).fetchone()
        assert row is not None
        observed_states.append(str(row[0]))

    rpc = ScriptedHermesRPC(
        [
            {"status": "accepted", "provider_message_ref": "provider-secret-1"},
            {"status": "accepted", "provider_message_ref": "provider-secret-2"},
        ],
        on_send=observe_committed_state,
    )
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="frozen-worker-one",
        clock=harness.clock,
    )

    delivered = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert delivered.delivered is True and delivered.chunks == 2
    assert observed_states == ["sending_unknown", "sending_unknown"]
    final_text = [
        params["text"] for method, params, _ in rpc.calls if method == "telegram.send"
    ][-1]
    assert isinstance(final_text, str)
    assert "[Open in Cortex](" in final_text
    assert "token=dl1." in final_text
    link = final_text.split("[Open in Cortex](", 1)[1].split(")", 1)[0]
    query = parse_qs(urlsplit(link).query)
    assert query["project"] == [harness.workspace["id"]]
    assert query["thread"] == [harness.thread["id"]]
    assert "run" not in query
    with sqlite3.connect(harness.store.path) as conn:
        counts_before = (
            conn.execute("SELECT COUNT(*) FROM transport_delivery_projections").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transport_delivery_chunks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transport_opaque_targets").fetchone()[0],
        )
        receipts = [
            str(row[0])
            for row in conn.execute(
                "SELECT provider_receipt_digest FROM transport_delivery_chunks"
            ).fetchall()
        ]
    assert counts_before == (1, 2, 1)
    assert all(value.startswith("hmac-sha256:") for value in receipts)
    assert b"provider-secret" not in harness.store.path.read_bytes()

    reopened = ControlStore(harness.store.path, clock=harness.clock)
    reopened.initialize()
    replay_rpc = ScriptedHermesRPC([])
    rebuilt = _hermes_adapter(
        store=reopened,
        config=harness.adapter.config,
        rpc=replay_rpc,
        worker_id="frozen-worker-two",
        clock=harness.clock,
    )
    duplicate = rebuilt.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert duplicate.duplicate is True
    assert replay_rpc.calls == []

    drifted = rebuilt.deliver_event(
        event={**event, "payload": {"summary": "different"}},
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert drifted.category == "idempotency_conflict"
    with sqlite3.connect(harness.store.path) as conn:
        counts_after = (
            conn.execute("SELECT COUNT(*) FROM transport_delivery_projections").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transport_delivery_chunks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transport_opaque_targets").fetchone()[0],
        )
    assert counts_after == counts_before


def test_frozen_replay_rejects_a_different_web_origin(harness: Harness) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    first_rpc = ScriptedHermesRPC(
        [{"status": "accepted", "provider_message_ref": "provider-origin"}]
    )
    first = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=first_rpc,
        worker_id="origin-first-worker",
        clock=harness.clock,
    )
    assert first.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    ).delivered is True

    changed_rpc = ScriptedHermesRPC([])
    changed = _hermes_adapter(
        store=harness.store,
        config=replace(
            harness.adapter.config,
            base_url="https://other-cortex.test/open",
        ),
        rpc=changed_rpc,
        worker_id="origin-changed-worker",
        clock=harness.clock,
    )
    replay = changed.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert replay.category == "idempotency_conflict"
    assert changed_rpc.calls == []


def test_frozen_request_hash_binds_actual_target_presence_and_revision(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    decision = harness.store.create_decision(
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
        idempotency_key=_key("frozen-target-hash-decision"),
    ).value
    event = harness.store.list_run_events(run["id"])[-1]
    current_run = harness.store.get_run(run["id"])
    binding = harness.store.resolve_transport(
        transport="telegram",
        external_scope=TelegramScope(
            bot_identity="research-bot", chat_id=-100, topic_id=41
        ).canonical(),
    )
    assert binding is not None
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=ScriptedHermesRPC([]),
        worker_id="target-hash-worker",
        clock=harness.clock,
    )
    _, targets = adapter._build_frozen_projection(
        normalized=event,
        run=current_run,
        binding=binding,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
        capability_digest="a" * 64,
    )
    assert len(targets) == 1
    assert targets[0].target.expected_revision == decision["revision"]
    original = adapter._frozen_delivery_request_hash(
        normalized=event,
        binding=binding,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
        capability_digest="a" * 64,
        opaque_targets=targets,
    )
    revised_target = replace(
        targets[0],
        target=replace(
            targets[0].target,
            expected_revision=targets[0].target.expected_revision + 1,
        ),
    )
    revised = adapter._frozen_delivery_request_hash(
        normalized=event,
        binding=binding,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
        capability_digest="a" * 64,
        opaque_targets=(revised_target,),
    )
    absent = adapter._frozen_delivery_request_hash(
        normalized=event,
        binding=binding,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
        capability_digest="a" * 64,
        opaque_targets=(),
    )
    assert original != revised
    assert original != absent


def test_frozen_decision_replays_after_the_run_revision_changes(
    harness: Harness,
) -> None:
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
        idempotency_key=_key("frozen-revision-decision"),
    )
    event = harness.store.list_run_events(run["id"])[-1]
    rpc = ScriptedHermesRPC(
        [{"status": "accepted", "provider_message_ref": "provider-decision"}]
    )
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="frozen-revision-worker",
        clock=harness.clock,
    )
    delivered = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert delivered.delivered is True
    send_params = next(params for method, params, _ in rpc.calls if method == "telegram.send")
    buttons = send_params["buttons"]
    assert isinstance(buttons, list)
    callback_data = buttons[0]["callback_data"]
    resolved = adapter.handle_update(
        _callback(170, "frozen-revision-callback", callback_data)
    )
    assert resolved.ok is True

    duplicate = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert duplicate.duplicate is True
    assert sum(method == "telegram.send" for method, _, _ in rpc.calls) == 1


def test_frozen_timeout_remains_sending_unknown_across_restart(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    rpc = ScriptedHermesRPC([TimeoutError("private worker detail")])
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="timeout-worker-one",
        clock=harness.clock,
    )

    unknown = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert unknown.category == "manual_required"
    with sqlite3.connect(harness.store.path) as conn:
        assert conn.execute(
            "SELECT state FROM transport_delivery_chunks WHERE event_id = ?",
            (event["id"],),
        ).fetchone()[0] == "sending_unknown"

    reopened = ControlStore(harness.store.path, clock=harness.clock)
    reopened.initialize()
    retry_rpc = ScriptedHermesRPC([])
    rebuilt = _hermes_adapter(
        store=reopened,
        config=harness.adapter.config,
        rpc=retry_rpc,
        worker_id="timeout-worker-two",
        clock=harness.clock,
    )
    still_unknown = rebuilt.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert still_unknown.category == "manual_required"
    assert retry_rpc.calls == []


def test_frozen_outcome_persistence_failure_is_manual_required(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    rpc = ScriptedHermesRPC(
        [{"status": "accepted", "provider_message_ref": "provider-persist-failure"}]
    )
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="outcome-persistence-worker",
        clock=harness.clock,
    )
    state = adapter._chunk_state
    assert state is not None

    def fail_complete(*args, **kwargs) -> None:
        raise RuntimeError("private persistence detail")

    monkeypatch.setattr(state, "complete", fail_complete)
    result = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert result.category == "manual_required"
    with sqlite3.connect(harness.store.path) as conn:
        assert conn.execute(
            "SELECT state FROM transport_delivery_chunks WHERE event_id = ?",
            (event["id"],),
        ).fetchone()[0] == "sending_unknown"


def test_frozen_delivery_preserves_legacy_delivered_and_uncertain_receipts(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    binding = harness.store.resolve_transport(
        transport="telegram",
        external_scope=TelegramScope(
            bot_identity="research-bot", chat_id=-100, topic_id=41
        ).canonical(),
    )
    assert binding is not None
    legacy = transports_module.ControlTransportStatePort(
        store=harness.store,
        worker_id="legacy-worker",
    )
    delivered_key = DeliveryKey(
        transport="telegram",
        destination_digest=str(binding["external_scope"]),
        event_id=event["id"],
        projection_version=1,
    )
    assert legacy.reserve_delivery(delivered_key) == "reserved"
    legacy.complete_delivery(delivered_key)

    uncertain_event = {**event, "id": "legacy-uncertain-event"}
    uncertain_key = replace(delivered_key, event_id=uncertain_event["id"])
    assert legacy.reserve_delivery(uncertain_key) == "reserved"
    rpc = ScriptedHermesRPC([])
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="frozen-legacy-guard",
        clock=harness.clock,
    )

    duplicate = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    uncertain = adapter.deliver_event(
        event=uncertain_event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert duplicate.duplicate is True
    assert uncertain.category == "manual_required"
    assert all(method != "telegram.send" for method, _, _ in rpc.calls)


def test_frozen_target_reconstruction_failure_releases_claim(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    first = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=ScriptedHermesRPC(
            [{"status": "rate_limited", "retry_after_ms": 1_000}]
        ),
        worker_id="target-reconstruction-first-worker",
        clock=harness.clock,
    )
    assert first.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    ).category == "rate_limited"
    harness.clock.advance(timedelta(seconds=1))

    replay = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=ScriptedHermesRPC([]),
        worker_id="target-reconstruction-replay-worker",
        clock=harness.clock,
    )
    state = replay._chunk_state
    assert state is not None

    def fail_target_reconstruction(*args, **kwargs) -> None:
        raise RuntimeError("private target detail")

    monkeypatch.setattr(state, "registered_targets", fail_target_reconstruction)
    result = replay.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert result.category == "projection_failure"
    with sqlite3.connect(harness.store.path) as conn:
        row = conn.execute(
            "SELECT state, claim_owner FROM transport_delivery_chunks "
            "WHERE event_id = ?",
            (event["id"],),
        ).fetchone()
    assert row == ("pending", None)


@pytest.mark.parametrize(
    ("capability_outcome", "expected_category"),
    [
        (TimeoutError("private capability detail"), "transport_unavailable"),
        (
            {
                "protocol": "cortex.telegram.transport/1",
                "send_message": True,
                "topics": True,
                "inline_buttons": True,
                "markdown_v2": True,
                "provider_idempotency": True,
                "outcome_query": False,
            },
            "capability_mismatch",
        ),
    ],
)
def test_frozen_preflight_failure_releases_claim_without_send(
    harness: Harness,
    capability_outcome: object,
    expected_category: str,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    first_rpc = ScriptedHermesRPC(
        [{"status": "rate_limited", "retry_after_ms": 1_000}]
    )
    first = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=first_rpc,
        worker_id="preflight-first-worker",
        clock=harness.clock,
    )
    limited = first.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert limited.category == "rate_limited"
    harness.clock.advance(timedelta(seconds=1))

    reopened = ControlStore(harness.store.path, clock=harness.clock)
    reopened.initialize()
    replay_rpc = ScriptedHermesRPC(
        [],
        capability_outcome=capability_outcome,
    )
    replay = _hermes_adapter(
        store=reopened,
        config=harness.adapter.config,
        rpc=replay_rpc,
        worker_id="preflight-replay-worker",
        clock=harness.clock,
    )
    result = replay.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert result.category == expected_category
    assert all(method != "telegram.send" for method, _, _ in replay_rpc.calls)
    with sqlite3.connect(harness.store.path) as conn:
        row = conn.execute(
            "SELECT state, claim_owner FROM transport_delivery_chunks "
            "WHERE event_id = ?",
            (event["id"],),
        ).fetchone()
    assert row == ("pending", None)


@pytest.mark.parametrize(
    ("capability_kind", "rate_limit_attempts"),
    [("callback", 2), ("deep_link", 1)],
)
def test_frozen_capability_expiry_after_rate_limit_performs_no_extra_send(
    harness: Harness,
    capability_kind: str,
    rate_limit_attempts: int,
) -> None:
    run = _running_run(harness.store, harness.thread)
    if capability_kind == "callback":
        harness.store.create_decision(
            run_id=run["id"],
            attempt_id=run["attempt_id"],
            runtime_binding_id=run["runtime_binding_id"],
            runtime_release_id=run["runtime_release_id"],
            state_generation_id=run["state_generation_id"],
            expected_revision=run["revision"],
            kind="research_gate",
            prompt="p" * 1_200,
            options=[{"id": "approve", "label": "Approve"}],
            actor_id="runtime",
            idempotency_key=_key("frozen-capability-decision"),
        )
        event = harness.store.list_run_events(run["id"])[-1]
    else:
        completed = _runtime_transition(
            harness.store,
            run,
            "completed",
            payload={"summary": "x" * 1_200},
        )
        event = harness.store.list_run_events(completed["id"])[-1]
    rpc = ScriptedHermesRPC(
        [{"status": "accepted", "provider_message_ref": "provider-first"}]
        + [
            {"status": "rate_limited", "retry_after_ms": 300_000}
            for _ in range(rate_limit_attempts)
        ]
    )
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id=f"expiry-{capability_kind}",
        clock=harness.clock,
    )

    for _ in range(rate_limit_attempts):
        limited = adapter.deliver_event(
            event=event,
            destination=TelegramDestination(chat_id=-100, topic_id=41),
        )
        assert limited.category == "rate_limited"
        assert limited.retry_after_ms == 300_000
        harness.clock.advance(timedelta(minutes=5))
    send_count = sum(method == "telegram.send" for method, _, _ in rpc.calls)
    with sqlite3.connect(harness.store.path) as conn:
        frozen_hash = conn.execute(
            "SELECT projection_hash FROM transport_delivery_projections"
        ).fetchone()[0]
        target_count = conn.execute(
            "SELECT COUNT(*) FROM transport_opaque_targets"
        ).fetchone()[0]

    expired = adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert expired.category == "capability_expired"
    assert sum(method == "telegram.send" for method, _, _ in rpc.calls) == send_count
    with sqlite3.connect(harness.store.path) as conn:
        assert conn.execute(
            "SELECT projection_hash FROM transport_delivery_projections"
        ).fetchone()[0] == frozen_hash
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_opaque_targets"
        ).fetchone()[0] == target_count


def test_control_owned_delivery_reclaims_expired_lease_after_restart(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    binding = harness.store.resolve_transport(
        transport="telegram",
        external_scope=TelegramScope(
            bot_identity="research-bot", chat_id=-100, topic_id=41
        ).canonical(),
    )
    assert binding is not None
    delivery_key = DeliveryKey(
        transport="telegram",
        destination_digest=binding["external_scope"],
        event_id=event["id"],
        projection_version=1,
    )
    crashed = transports_module.ControlTransportStatePort(
        store=harness.store,
        worker_id="crashed-worker",
        lease_seconds=30,
    )
    assert crashed.reserve_delivery(delivery_key) == "reserved"

    harness.clock.advance(timedelta(seconds=31))
    assert crashed.reserve_delivery(delivery_key) == "in_flight"
    reopened_store = ControlStore(harness.store.path, clock=harness.clock)
    reopened_store.initialize()
    client = SyntheticTelegramClient()
    recovered = TelegramAdapter.control_owned(
        store=reopened_store,
        config=harness.adapter.config,
        client=client,
        worker_id="recovery-worker",
        delivery_lease_seconds=30,
        clock=harness.clock,
    )
    delivered = recovered.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert delivered.delivered is True
    assert len(client.messages) == 1
    with pytest.raises(InvalidTransition, match="stale_claim"):
        crashed.complete_delivery(delivery_key)

    final_store = ControlStore(harness.store.path, clock=harness.clock)
    final_store.initialize()
    duplicate_client = SyntheticTelegramClient()
    duplicate_adapter = TelegramAdapter.control_owned(
        store=final_store,
        config=harness.adapter.config,
        client=duplicate_client,
        worker_id="final-worker",
        clock=harness.clock,
    )
    duplicate = duplicate_adapter.deliver_event(
        event=event,
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert duplicate.duplicate is True
    assert duplicate_client.messages == []


def test_control_owned_opaque_target_survives_key_rotation_and_keeps_purpose(
    harness: Harness,
) -> None:
    old_key = b"o" * 32
    new_key = b"n" * 32
    identity_key = b"i" * 32
    old_config = replace(
        harness.adapter.config,
        signing_key=old_key,
        identity_key=identity_key,
    )
    old_adapter = TelegramAdapter.control_owned(
        store=harness.store,
        config=old_config,
        client=SyntheticTelegramClient(),
        worker_id="identity-worker-old",
        clock=harness.clock,
    )
    inbound = _message(140, "stable across signing-key rotation")
    assert old_adapter.handle_update(inbound).mutated is True

    old_port = transports_module.ControlTransportStatePort(
        store=harness.store,
        worker_id="token-worker-old",
    )
    old_tokens = OpaqueTokenService(
        signing_key=old_key,
        store=old_port,
        clock=harness.clock,
    )
    token = old_tokens.issue(
        namespace="deep_link",
        purpose="open",
        resource_kind="thread",
        resource_id=harness.thread["id"],
    )
    expiring = old_tokens.issue(
        namespace="deep_link",
        purpose="open",
        resource_kind="thread",
        resource_id=harness.thread["id"],
    )

    reopened_store = ControlStore(harness.store.path, clock=harness.clock)
    reopened_store.initialize()
    with pytest.raises(ValueError, match="identity_key"):
        TelegramAdapter.control_owned(
            store=reopened_store,
            config=replace(harness.adapter.config, signing_key=new_key),
            client=SyntheticTelegramClient(),
            worker_id="unsafe-rotation-worker",
            previous_signing_keys=(old_key,),
            clock=harness.clock,
        )
    new_config = replace(old_config, signing_key=new_key)
    rotated_adapter = TelegramAdapter.control_owned(
        store=reopened_store,
        config=new_config,
        client=SyntheticTelegramClient(),
        worker_id="identity-worker-new",
        previous_signing_keys=(old_key,),
        clock=harness.clock,
    )
    replay = rotated_adapter.handle_update(inbound)
    assert replay.ok is True and replay.replayed is True
    assert len(reopened_store.list_messages(harness.thread["id"])) == 1

    new_port = transports_module.ControlTransportStatePort(
        store=reopened_store,
        worker_id="token-worker-new",
    )
    without_previous = OpaqueTokenService(
        signing_key=new_key,
        store=new_port,
        clock=harness.clock,
    )
    with pytest.raises(TransportProblem, match="invalid_token"):
        without_previous.resolve(token, namespace="deep_link", purpose="open")

    rotated = OpaqueTokenService(
        signing_key=new_key,
        verification_keys=(old_key,),
        store=new_port,
        clock=harness.clock,
    )
    with pytest.raises(TransportProblem, match="token_scope_mismatch"):
        rotated.resolve(token, namespace="deep_link", purpose="control")
    target = rotated.resolve(token, namespace="deep_link", purpose="open")
    assert target.resource_id == harness.thread["id"]
    with pytest.raises(TransportProblem, match="token_unavailable"):
        rotated.resolve(token, namespace="deep_link", purpose="open")

    harness.clock.advance(timedelta(minutes=6))
    with pytest.raises(TransportProblem, match="token_expired"):
        rotated.resolve(expiring, namespace="deep_link", purpose="open")


def test_failure_notification_retry_button_targets_terminal_run(
    harness: Harness,
) -> None:
    run = _running_run(harness.store, harness.thread)
    failed = _runtime_transition(
        harness.store, run, "failed", payload={"summary": "Provider exhausted"}
    )
    assert harness.store.get_thread(harness.thread["id"])["active_run_id"] is None
    event = harness.store.list_run_events(run["id"])[-1]
    delivered = harness.adapter.deliver_event(
        event=event, destination=TelegramDestination(chat_id=-100, topic_id=41)
    )
    assert delivered.delivered is True
    retry_button = harness.client.messages[-1].buttons[0]
    retried = harness.adapter.handle_update(
        _callback(120, "retry-terminal-run", retry_button.callback_data)
    )
    assert retried.ok is True and retried.action == "retry"
    assert retried.state == "retrying"
    current = harness.store.get_run(failed["id"])
    assert current["state"] == "retrying"


def test_deep_link_expiry_and_signature_tamper(harness: Harness) -> None:
    opened = harness.adapter.handle_update(_message(30, "/open"))
    url = next(
        word for word in opened.response_text.split() if word.startswith("https://")
    )
    query = parse_qs(urlsplit(url).query)
    assert query["project"] == [harness.workspace["id"]]
    assert query["thread"] == [harness.thread["id"]]
    token = query["token"][0]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(TransportProblem, match="invalid_token"):
        harness.adapter.resolve_deep_link(tampered)
    harness.clock.advance(timedelta(minutes=6))
    with pytest.raises(TransportProblem, match="token_expired"):
        harness.adapter.resolve_deep_link(token)


def test_unbound_root_link_navigates_to_project_only(harness: Harness) -> None:
    opened = harness.adapter.handle_update(_message(31, "/open", topic_id=None))
    assert opened.category == "binding_required"
    url = next(
        word for word in opened.response_text.split() if word.startswith("https://")
    )
    query = parse_qs(urlsplit(url).query)
    assert query["project"] == [harness.workspace["id"]]
    assert "thread" not in query
    assert harness.adapter.resolve_deep_link(query["token"][0]) == (
        "workspace", harness.workspace["id"]
    )


def test_markdown_chunking_is_bounded_and_never_splits_escape() -> None:
    chunks = chunk_markdown_v2("*unsafe* " * 2_000, maximum=128)
    assert len(chunks) > 2
    assert all(len(chunk) <= 128 for chunk in chunks)
    assert all(not chunk.endswith("\\") for chunk in chunks)
    assert all("*" not in chunk.replace("\\*", "") for chunk in chunks)


@pytest.mark.parametrize(
    ("failure", "category", "retryable"),
    [
        (TelegramRateLimited(2_500), "rate_limited", True),
        (TelegramTemporaryFailure(), "transport_unavailable", True),
        (TelegramPermanentFailure(), "delivery_rejected", False),
    ],
)
def test_delivery_failure_classification_releases_reservation(
    harness: Harness,
    failure: Exception,
    category: str,
    retryable: bool,
) -> None:
    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    harness.client.queue_failure(failure)  # type: ignore[arg-type]
    failed = harness.adapter.deliver_event(
        event=event, destination=TelegramDestination(chat_id=-100, topic_id=41)
    )
    assert failed.category == category
    assert failed.retryable is retryable
    retried = harness.adapter.deliver_event(
        event=event, destination=TelegramDestination(chat_id=-100, topic_id=41)
    )
    assert retried.delivered is True


def test_shadow_mode_performs_no_mutation_send_or_receipt(harness: Harness) -> None:
    shadow_client = SyntheticTelegramClient()
    shadow_receipts = InMemoryTransportReceiptPort()
    shadow = _adapter(
        store=harness.store,
        client=shadow_client,
        receipts=shadow_receipts,
        tokens=harness.tokens,
        clock=harness.clock,
        workspace_id=harness.workspace["id"],
        mode="shadow",
    )
    before = harness.store.get_thread(harness.thread["id"])
    result = shadow.handle_update(_message(40, "would capture"))
    assert result.category == "shadow" and result.mutated is False
    assert harness.store.get_thread(harness.thread["id"]) == before
    assert harness.store.list_messages(harness.thread["id"]) == []

    run = _running_run(harness.store, harness.thread)
    event = harness.store.list_run_events(run["id"])[-1]
    projected = shadow.deliver_event(
        event={**event, "type": "run.milestone", "payload": {"name": "Scout"}},
        destination=TelegramDestination(chat_id=-100, topic_id=41),
    )
    assert projected.category == "shadow"
    assert shadow_client.messages == []


def test_authorization_and_rate_limit_do_not_expose_raw_scope(harness: Harness) -> None:
    assert "aaaaaaaa" not in repr(harness.adapter.config)
    denied = harness.adapter.handle_update(_message(50, "/status", sender_id=999))
    assert denied.category == "unauthorized"
    assert "999" not in denied.response_text and "-100" not in denied.response_text

    limited = _adapter(
        store=harness.store,
        client=harness.client,
        receipts=InMemoryTransportReceiptPort(),
        tokens=harness.tokens,
        clock=harness.clock,
        workspace_id=harness.workspace["id"],
        inbound_limit=1,
    )
    assert limited.handle_update(_message(51, "/status")).ok is True
    blocked = limited.handle_update(_message(52, "/status"))
    assert blocked.category == "rate_limited"
    assert blocked.retry_after_ms is not None and blocked.retry_after_ms > 0


def test_a_captured_message_tells_the_turn_bridge_exactly_once(
    harness: Harness,
) -> None:
    """⟦P5.4c⟧ The seam that closes the loop's inbound end.

    Before this the adapter appended and stopped, which is why a message
    reached Control and nothing ever answered it. The sink is told after the
    append commits -- so the turn's history already contains the message it is
    a turn about -- and not on a replay, which is the same message arriving
    twice and the run for it already asked for.
    """

    told: list[str] = []
    harness.adapter.bind_turn_sink(told.append)

    first = harness.adapter.handle_update(_message(910, "what did it claim?"))
    assert first.action == "capture_message"
    assert told == [harness.thread["id"]]

    replay = harness.adapter.handle_update(_message(910, "what did it claim?"))
    assert replay.replayed is True or replay.ok
    assert told == [harness.thread["id"]]


def test_a_sink_that_raises_never_costs_the_operator_the_capture(
    harness: Harness,
) -> None:
    """A message that reached Control is captured, answered or not."""

    def refuse(_thread_id: str) -> None:
        raise RuntimeError("the bridge is gone")

    harness.adapter.bind_turn_sink(refuse)

    result = harness.adapter.handle_update(_message(911, "still a capture"))

    assert result.ok is True
    assert result.action == "capture_message"
    assert [
        message["content"]
        for message in harness.store.list_messages(harness.thread["id"])
    ] == ["still a capture"]


def test_shadow_mode_never_starts_a_turn(harness: Harness, tmp_path: Path) -> None:
    """Shadow is the mode that writes nothing; a turn is a write."""

    told: list[str] = []
    shadow = _adapter(
        store=harness.store,
        client=harness.client,
        receipts=harness.receipts,
        tokens=harness.tokens,
        clock=harness.clock,
        workspace_id=harness.workspace["id"],
        mode="shadow",
    )
    shadow.bind_turn_sink(told.append)

    result = shadow.handle_update(_message(912, "would be captured"))

    assert result.category == "shadow"
    assert told == []


def _chunk_states(store: ControlStore, event_id: str) -> list[str]:
    with sqlite3.connect(store.path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT state FROM transport_delivery_chunks "
                "WHERE event_id = ? ORDER BY chunk_index",
                (event_id,),
            )
        ]


def test_a_refusal_before_the_send_releases_the_permit_and_delivers_next_pass(
    harness: Harness,
) -> None:
    """⟦P5.5/P5-01⟧ The one post-permit failure that is not an unknown outcome.

    The worker refuses `telegram.send` with `operation_conflict` while its long
    poll is open, and the daemon refuses its own frame when the shared transport
    line is busy. Both prove nothing was written, so the chunk goes back to
    claimable instead of stranding the operator's answer in `manual_required` --
    which is exactly what the fifth window on the mini did with the only reply
    the product has ever computed for a real message.
    """

    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    rpc = ScriptedHermesRPC(
        [
            TelegramRefusedBeforeSend("operation_conflict"),
            {"status": "accepted", "provider_message_ref": "1001"},
        ]
    )
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="refused-before-send-worker",
        clock=harness.clock,
    )
    destination = TelegramDestination(chat_id=-100, topic_id=41)

    refused = adapter.deliver_event(event=event, destination=destination)

    assert refused.category == "transport_refused_before_send"
    assert refused.delivered is False and refused.retryable is True
    # Back to a state the SAME worker may claim again -- not `sending_unknown`.
    assert _chunk_states(harness.store, event["id"]) == ["pending"]

    delivered = adapter.deliver_event(event=event, destination=destination)

    assert delivered.delivered is True and delivered.duplicate is False
    assert _chunk_states(harness.store, event["id"]) == ["delivered"]
    # Two frames were offered; exactly one carried a message anywhere, because
    # the first was refused before the socket existed.
    assert [method for method, _, _ in rpc.calls].count("telegram.send") == 2

    again = adapter.deliver_event(event=event, destination=destination)

    assert again.delivered is False and again.duplicate is True
    assert [method for method, _, _ in rpc.calls].count("telegram.send") == 2


def test_a_worker_crash_mid_send_stays_manual_and_the_next_event_still_goes(
    harness: Harness,
) -> None:
    """⟦P5-01⟧ An unknown outcome is still unknown, and still stops that chunk.

    The relaunch a crash forces is the supervisor's business; what Control owes
    the operator is that the chunk nobody can speak for is never re-sent, and
    that it does not become a reason the NEXT delivery cannot happen.
    """

    run = _running_run(harness.store, harness.thread)
    completed = _runtime_transition(harness.store, run, "completed")
    event = harness.store.list_run_events(completed["id"])[-1]
    later = {**event, "id": "event-after-the-relaunch"}
    rpc = ScriptedHermesRPC(
        [
            RuntimeError("worker exited"),
            {"status": "accepted", "provider_message_ref": "2002"},
        ]
    )
    adapter = _hermes_adapter(
        store=harness.store,
        config=harness.adapter.config,
        rpc=rpc,
        worker_id="crashed-mid-send-worker",
        clock=harness.clock,
    )
    destination = TelegramDestination(chat_id=-100, topic_id=41)

    crashed = adapter.deliver_event(event=event, destination=destination)

    assert crashed.category == "manual_required"
    assert crashed.retryable is False
    assert _chunk_states(harness.store, event["id"]) == ["sending_unknown"]

    # Re-entered on the next tick, as the drain would: still refused, and still
    # without a second frame. `manual_required` is an operator decision.
    repeat = adapter.deliver_event(event=event, destination=destination)
    assert repeat.category == "manual_required"
    assert [method for method, _, _ in rpc.calls].count("telegram.send") == 1

    delivered = adapter.deliver_event(event=later, destination=destination)

    assert delivered.delivered is True
    assert _chunk_states(harness.store, event["id"]) == ["sending_unknown"]
    assert _chunk_states(harness.store, later["id"]) == ["delivered"]



def test_a_pause_the_runtime_cannot_perform_is_rejected_from_telegram(
    harness: Harness,
) -> None:
    """⟦P9-3 BRK-4⟧ Telegram told the operator the pause was committed too.

    `_apply_run_action` wrote `pause_requested` straight through the store, and
    the reply said "The /pause command was committed in Cortex." The Hermes
    adapter reports `pause=False`, so the delivered action was rejected and the
    store rolled the run back to `running` with the answer delivered anyway --
    and nothing told the operator any of that.

    The capability is a property of the bound runtime, so the answer is known
    before the write. `/cancel` is unaffected, which is the whole point of
    refusing narrowly: the operator still has a way to stop the turn, and the
    reply now says so.
    """

    run = _running_run(harness.store, harness.thread)
    harness.adapter.bind_capability_probe(lambda capability: False)

    rejected = harness.adapter.handle_update(_message(210, "/pause"))

    assert rejected.ok is False
    assert rejected.category == "pause_unsupported"
    assert "cancel" in rejected.response_text.lower()
    # Nothing was written.
    assert harness.store.get_run(run["id"])["state"] == "running"

    # The other stop still works, and still commits.
    canceled = harness.adapter.handle_update(_message(211, "/cancel"))
    assert canceled.ok is True
    assert harness.store.get_run(run["id"])["state"] == "cancel_requested"


def test_help_does_not_offer_a_pause_the_runtime_cannot_perform(
    harness: Harness,
) -> None:
    """⟦P9-3 BRK-4⟧ Advertising it is the first half of promising it."""

    helped = harness.adapter.handle_update(_message(212, "/help"))

    assert "/pause" not in helped.response_text, helped.response_text
    assert "/cancel" in helped.response_text


def test_an_unknown_pause_capability_still_commits_from_telegram(
    harness: Harness,
) -> None:
    """⟦P9-3 BRK-4⟧ Unknown is not refused.

    With no probe bound -- a shadow-mode or unbound daemon, or one whose worker
    has not run a turn yet -- the adapter behaves exactly as it did before this
    guard existed. Refusing on an unknown would invent a capability report,
    which is the failure being fixed rather than a fix.
    """

    run = _running_run(harness.store, harness.thread)

    paused = harness.adapter.handle_update(_message(213, "/pause"))

    assert paused.ok is True
    assert harness.store.get_run(run["id"])["state"] == "pause_requested"
