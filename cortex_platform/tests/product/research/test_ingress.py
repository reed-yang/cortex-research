"""Both real producers retain the shared opt-in marker, without network calls."""

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.research.context import mode_for
from cortex_platform.tests.product.api.test_app import TOKEN, _post
from cortex_platform.tests.product.transports.test_telegram import harness, _message


@pytest.mark.parametrize("command", ["research", "chat"])
def test_telegram_normalizes_marker_and_notifies_once(harness, command):
    told = []
    harness.adapter.bind_turn_sink(told.append)
    update = _message(910, f"/{command.upper()}@research_bot   decoding")
    first = harness.adapter.handle_update(update)
    assert first.ok and first.action == "capture_message"
    assert harness.adapter.handle_update(update).replayed
    messages = harness.store.list_messages(harness.thread["id"])
    assert len(messages) == 1 and messages[0]["content"] == f"/{command} decoding"
    assert bool(mode_for(messages)[0]) == (command == "research")
    assert told == [harness.thread["id"]]


@pytest.mark.parametrize("command", ["research", "chat"])
def test_web_message_api_retains_marker_and_replays(harness, command):
    api = ControlAPI(harness.store, access_token=TOKEN)
    thread_id = harness.thread["id"]
    payload = {"role": "user", "content": f"/{command} decoding", "expected_revision": harness.store.get_thread(thread_id)["revision"]}
    path = f"/api/v1/threads/{thread_id}/messages"
    first = _post(api, path, payload, key="web-research-message-001")
    assert first.status == 201
    assert _post(api, path, payload, key="web-research-message-001").status == 201
    messages = harness.store.list_messages(thread_id)
    assert len(messages) == 1 and messages[0]["content"] == payload["content"]
    assert bool(mode_for(messages)[0]) == (command == "research")


@pytest.mark.parametrize("command", ["/research", "/chat"])
def test_empty_telegram_command_is_rejected_without_message(harness, command):
    assert not harness.adapter.handle_update(_message(910, command)).ok
    assert not harness.store.list_messages(harness.thread["id"])
