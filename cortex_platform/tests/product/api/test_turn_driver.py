"""⟦P8⟧ A run created through the control API is handed to the turn bridge.

`POST /api/v1/threads/{id}/runs` used to create a `queued` run that nothing
dispatched: the only seam that executes a turn is `InboundTurnBridge.submit`,
and only the Telegram adapter called it. The API now calls the same seam under
the same two rules the adapter follows -- after the command committed, never
on a replay -- and refuses, typed and before anything is written, when this
daemon could not drive the run at all.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import (
    CANCELED_BEFORE_BINDING,
    THREAD_PAGE_LIMIT,
    ControlStore,
    NotFound,
)
from cortex_platform.runtime.tests.fakes import FAKE_RUNTIME_IDENTITY

TOKEN = "x" * 48


class _Bridge:
    """The seam, recorded: `submit` is all the API may call."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.forgotten: list[str] = []

    def submit(self, thread_id: str) -> None:
        self.submitted.append(str(thread_id))

    def forget(self, run_id: str) -> None:
        self.forgotten.append(str(run_id))

    def status(self) -> dict:
        return {"state": "running", "queued": len(self.submitted)}


class _Worker:
    def __init__(self, *, bound: bool, reason: str | None = None) -> None:
        self.bound = bound
        self._reason = reason

    def health(self):
        payload = {
            "state": "bound" if self.bound else "unbound",
            "reason": self._reason,
            "release_id": "hermes-0.15.0-gen9" if self.bound else None,
            "slot_digest": "b" * 64 if self.bound else None,
            "launched": False,
        }
        return type("_Health", (), {"to_dict": lambda self: payload})()


def _store(tmp_path: Path) -> ControlStore:
    store = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    return store


def _driving_api(store: ControlStore) -> tuple[ControlAPI, _Bridge]:
    """What `daemon.main` builds when a managed worker was bound at start."""

    bridge = _Bridge()
    api = ControlAPI(
        store,
        access_token=TOKEN,
        managed_worker=_Worker(bound=True),
        turn_bridge=bridge,
    )
    return api, bridge


def _headers(key: str | None = None) -> dict[str, str]:
    value = {"X-Cortex-Control-Token": TOKEN}
    if key:
        value["Idempotency-Key"] = key
    return value


def _post(api: ControlAPI, path: str, payload: dict, *, key: str):
    return api.handle(
        method="POST",
        target=path,
        headers=_headers(key),
        body=json.dumps(payload).encode(),
    )


def _thread_with_message(api: ControlAPI) -> dict:
    workspace = _post(
        api, "/api/v1/workspaces", {"title": "Research"}, key="workspace-command-0001"
    ).payload
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Cockpit", "expected_revision": 0},
        key="thread-command-000001",
    ).payload
    _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "What did the paper claim?", "expected_revision": thread["revision"]},
        key="message-command-00001",
    )
    return api.handle(
        method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()
    ).payload


def _enable_dispatch(store: ControlStore) -> None:
    store.enable_runtime_activation(
        mode="permanent", actor_id="operator", idempotency_key="activation-000000001"
    )


def _run_events(store: ControlStore, run_id: str) -> list[dict]:
    return [dict(event) for event in store.list_run_events(run_id)]


def test_a_created_run_is_submitted_once_and_a_replay_is_not(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    body = {"expected_revision": thread["revision"]}

    created = _post(api, f"/api/v1/threads/{thread['id']}/runs", body, key="run-command-0000001")

    assert created.status == 201
    assert created.payload["state"] == "queued"
    assert bridge.submitted == [thread["id"]]

    replayed = _post(api, f"/api/v1/threads/{thread['id']}/runs", body, key="run-command-0000001")

    assert replayed.status == 201
    assert ("Idempotency-Replayed", "true") in replayed.headers
    assert bridge.submitted == [thread["id"]]


def test_a_closed_dispatch_gate_refuses_before_a_run_exists(tmp_path: Path) -> None:
    """The bridge's rule 1, at the API: the gate's own word, nothing written."""

    store = _store(tmp_path)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)

    refused = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )

    assert refused.status == 409
    assert refused.content_type == "application/problem+json"
    assert refused.payload["category"] == "runtime_activation_disabled"
    assert refused.payload["retryable"] is True
    assert bridge.submitted == []
    assert store.get_thread(thread["id"])["active_run_id"] is None
    assert store.list_thread_runs(thread_id=thread["id"], after_id=None, limit=10) == []


def test_an_unbound_worker_refuses_typed_and_names_its_reason(tmp_path: Path) -> None:
    """A daemon with a worker it could not bind owns no bridge: no queued run."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    api = ControlAPI(
        store,
        access_token=TOKEN,
        managed_worker=_Worker(bound=False, reason="release_not_approved"),
    )
    thread = _thread_with_message(api)

    refused = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )

    assert refused.status == 409
    assert refused.payload["category"] == "managed_worker_unavailable"
    assert refused.payload["retryable"] is True
    assert "release_not_approved" in refused.payload["title"]
    assert store.get_thread(thread["id"])["active_run_id"] is None


def test_a_process_with_no_worker_at_all_still_queues_a_run(tmp_path: Path) -> None:
    """Demo mode and the store-only tests: nothing here to refuse on behalf of."""

    store = _store(tmp_path)
    api = ControlAPI(store, access_token=TOKEN)
    thread = _thread_with_message(api)

    created = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )

    assert created.status == 201
    assert created.payload["state"] == "queued"


def test_a_retry_of_a_failed_run_is_submitted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload
    failed = store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        expected_revision=run["revision"],
        category="runtime_activation_disabled",
        actor_id="cortexd-turn-test",
        idempotency_key="bridge-gate-test-000001",
    ).value
    assert failed["state"] == "failed"

    retried = _post(
        api,
        f"/api/v1/runs/{run['id']}/retry",
        {"expected_revision": failed["revision"], "reason": "gate opened"},
        key="retry-command-000001",
    )

    assert retried.status == 200, retried.payload
    assert retried.payload["state"] == "retrying"
    assert bridge.submitted == [thread["id"], thread["id"]]


def test_a_retry_under_a_closed_gate_is_refused_before_it_is_written(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    api = ControlAPI(store, access_token=TOKEN)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload
    failed = store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        expected_revision=run["revision"],
        category="runtime_activation_disabled",
        actor_id="cortexd-turn-test",
        idempotency_key="bridge-gate-test-000001",
    ).value
    driving, bridge = _driving_api(store)

    refused = _post(
        driving,
        f"/api/v1/runs/{run['id']}/retry",
        {"expected_revision": failed["revision"], "reason": "too early"},
        key="retry-command-000001",
    )

    assert refused.status == 409
    assert refused.payload["category"] == "runtime_activation_disabled"
    assert store.get_run(run["id"])["state"] == "failed"
    assert bridge.submitted == []


def test_cancel_of_a_never_driven_run_converges_to_canceled(tmp_path: Path) -> None:
    """Deliverable 3: the store transition the orchestrator uses, without an owner."""

    store = _store(tmp_path)
    api = ControlAPI(store, access_token=TOKEN)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload

    canceled = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"]},
        key="cancel-command-00001",
    )

    assert canceled.status == 200
    assert canceled.payload["state"] == "canceled"
    assert "dispatch_owner" not in canceled.payload.get("attempt", {})
    events = _run_events(store, run["id"])
    assert [event["type"] for event in events][-1] == "run.canceled"
    assert events[-1]["payload"]["category"] == CANCELED_BEFORE_BINDING
    assert store.get_thread(thread["id"])["active_run_id"] is None
    # The thread is usable again: a new run may be created on it.
    fresh = store.get_thread(thread["id"])
    again = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": fresh["revision"]},
        key="run-command-0000002",
    )
    assert again.status == 201


def test_cancel_tells_the_bridge_to_forget_the_run(tmp_path: Path) -> None:
    """⟦V-1⟧ The re-drive entry dies with the cancel, not at its next tick."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload

    canceled = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"]},
        key="cancel-command-00001",
    )

    assert canceled.status == 200
    assert canceled.payload["state"] == "canceled"
    assert bridge.forgotten == [run["id"]]
    assert bridge.submitted == [thread["id"]]


def test_cancel_of_a_reserved_run_is_left_to_the_dispatch_that_owns_it(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    api = ControlAPI(store, access_token=TOKEN)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload
    reserved = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        dispatch_owner="cortexd-turn:0001",
        runtime_release_id=FAKE_RUNTIME_IDENTITY.release_id,
        state_generation_id=FAKE_RUNTIME_IDENTITY.state_generation_id,
        runtime_slot_id=FAKE_RUNTIME_IDENTITY.slot_id,
        runtime_artifact_digest=FAKE_RUNTIME_IDENTITY.artifact_digest,
        runtime_worker_protocol=FAKE_RUNTIME_IDENTITY.worker_protocol,
        expected_revision=run["revision"],
        actor_id="cortexd-turn",
        idempotency_key="reserve-command-000001",
    ).value

    requested = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": reserved["revision"]},
        key="cancel-command-00001",
    )

    assert requested.status == 200
    assert requested.payload["state"] == "cancel_requested"
    assert store.get_run(run["id"])["state"] == "cancel_requested"


# ⟦Batch G⟧ -------------------------------------------------------------------


def test_a_worker_that_lost_its_binding_after_start_refuses_the_run(
    tmp_path: Path,
) -> None:
    """⟦P8-1⟧ A bridge exists, but health says the worker cannot launch."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    bridge = _Bridge()
    api = ControlAPI(
        store,
        access_token=TOKEN,
        managed_worker=_Worker(bound=False, reason="release_not_approved"),
        turn_bridge=bridge,
    )
    thread = _thread_with_message(api)

    refused = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )

    assert refused.status == 409
    assert refused.payload["category"] == "managed_worker_unavailable"
    assert "release_not_approved" in refused.payload["title"]
    assert bridge.submitted == []
    assert store.get_thread(thread["id"])["active_run_id"] is None


def test_a_retried_request_is_replayed_before_it_is_refused(tmp_path: Path) -> None:
    """⟦P8-06⟧ The receipt outranks the gate: the run exists, so say so."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    body = {"expected_revision": thread["revision"]}
    created = _post(api, f"/api/v1/threads/{thread['id']}/runs", body, key="run-command-0000001")
    assert created.status == 201
    store.disable_runtime_activation(
        actor_id="operator", idempotency_key="deactivation-00000001"
    )

    replayed = _post(api, f"/api/v1/threads/{thread['id']}/runs", body, key="run-command-0000001")

    assert replayed.status == 201
    assert ("Idempotency-Replayed", "true") in replayed.headers
    assert replayed.payload["id"] == created.payload["id"]
    assert bridge.submitted == [thread["id"]]
    # A DIFFERENT key under the shut gate is still refused before anything is written.
    refused = _post(api, f"/api/v1/threads/{thread['id']}/runs", body, key="run-command-0000002")
    assert refused.status == 409
    assert refused.payload["category"] == "runtime_activation_disabled"


def test_a_retry_is_replayed_before_it_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload
    failed = store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        expected_revision=run["revision"],
        category="runtime_activation_disabled",
        actor_id="cortexd-turn-test",
        idempotency_key="bridge-gate-test-000001",
    ).value
    body = {"expected_revision": failed["revision"], "reason": "  gate opened  "}
    retried = _post(api, f"/api/v1/runs/{run['id']}/retry", body, key="retry-command-000001")
    assert retried.status == 200
    store.disable_runtime_activation(
        actor_id="operator", idempotency_key="deactivation-00000001"
    )

    replayed = _post(api, f"/api/v1/runs/{run['id']}/retry", body, key="retry-command-000001")

    assert replayed.status == 200
    assert ("Idempotency-Replayed", "true") in replayed.headers
    assert bridge.submitted == [thread["id"], thread["id"]]


def test_a_user_message_during_an_active_run_is_submitted_as_a_follow_up(
    tmp_path: Path,
) -> None:
    """⟦P8-4⟧ The adapter's rule at the API: a message joins the run in flight.

    The bridge remembers a queued or running thread as a follow-up and
    submits it again when the turn ends; the message route hands it the
    thread exactly as the Telegram adapter does, and only while a run is
    active -- on an idle thread the cockpit creates the run itself.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    idle = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "Still idle", "expected_revision": thread["revision"]},
        key="message-command-00002",
    )
    assert idle.status == 201
    assert bridge.submitted == []
    fresh = api.handle(
        method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()
    ).payload
    created = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": fresh["revision"]},
        key="run-command-0000001",
    )
    assert created.status == 201
    assert bridge.submitted == [thread["id"]]
    fresh = api.handle(
        method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()
    ).payload

    followup = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "And also this", "expected_revision": fresh["revision"]},
        key="message-command-00003",
    )

    assert followup.status == 201
    assert bridge.submitted == [thread["id"], thread["id"]]
    # A replay of the same append asks for nothing again.
    replayed = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "And also this", "expected_revision": fresh["revision"]},
        key="message-command-00003",
    )
    assert ("Idempotency-Replayed", "true") in replayed.headers
    assert bridge.submitted == [thread["id"], thread["id"]]
    # A system note is not a turn.
    fresh = api.handle(
        method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()
    ).payload
    note = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "system", "content": "operator note", "expected_revision": fresh["revision"]},
        key="message-command-00004",
    )
    assert note.status == 201
    assert bridge.submitted == [thread["id"], thread["id"]]


# ⟦Fix-verification round⟧ ------------------------------------------------


def test_a_message_under_a_closed_gate_leaves_the_queued_run_standing(
    tmp_path: Path,
) -> None:
    """⟦G-NEW-3⟧ No submit under a shut gate: the run is not ended under the
    message's own promise; the gate opening and the next message drive it."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    created = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )
    assert created.status == 201
    assert bridge.submitted == [thread["id"]]
    store.disable_runtime_activation(
        actor_id="operator", idempotency_key="deactivation-00000001"
    )
    fresh = api.handle(
        method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()
    ).payload

    appended = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "still there?", "expected_revision": fresh["revision"]},
        key="message-command-00003",
    )

    assert appended.status == 201
    assert bridge.submitted == [thread["id"]]
    assert store.get_run(created.payload["id"])["state"] == "queued"


def test_a_message_on_a_run_being_cancelled_is_stored_and_not_submitted(
    tmp_path: Path,
) -> None:
    """⟦G-R1⟧ A run waiting on the operator is never moved by a message."""

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload
    reserved = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        dispatch_owner="cortexd-turn:0001",
        runtime_release_id=FAKE_RUNTIME_IDENTITY.release_id,
        state_generation_id=FAKE_RUNTIME_IDENTITY.state_generation_id,
        runtime_slot_id=FAKE_RUNTIME_IDENTITY.slot_id,
        runtime_artifact_digest=FAKE_RUNTIME_IDENTITY.artifact_digest,
        runtime_worker_protocol=FAKE_RUNTIME_IDENTITY.worker_protocol,
        expected_revision=run["revision"],
        actor_id="cortexd-turn",
        idempotency_key="reserve-command-000001",
    ).value
    requested = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": reserved["revision"]},
        key="cancel-command-00001",
    )
    assert requested.payload["state"] == "cancel_requested"
    fresh = api.handle(
        method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()
    ).payload

    appended = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "never mind", "expected_revision": fresh["revision"]},
        key="message-command-00003",
    )

    assert appended.status == 201
    assert bridge.submitted == [thread["id"]]
    assert store.get_run(run["id"])["state"] == "cancel_requested"


def test_a_message_on_an_engine_carrier_thread_is_never_submitted(
    tmp_path: Path,
) -> None:
    """⟦G-NEW-1⟧ The API asks `run_is_conversation` before it hands over."""

    from cortex_platform.tests.product.transports.test_turn_bridge import _carrier_run

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    carrier = _carrier_run(store)
    thread = store.get_thread(carrier["thread_id"])

    appended = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "hello?", "expected_revision": thread["revision"]},
        key="message-command-00003",
    )

    assert appended.status == 201
    assert bridge.submitted == []
    assert store.get_run(carrier["run_id"])["state"] == "queued"


def test_a_machine_thread_never_gets_a_run_from_the_api(tmp_path: Path) -> None:
    """⟦V6-3⟧ "Start research run" on a `capture cap_*` thread is refused, typed.

    The rows are the consumer's own shape (a real capture, its workspace and
    `capture <id>` thread), the requests are the cockpit's (the real
    `ControlAPI` routes, with a bound worker and a bridge behind them). The
    message is stored -- real_run_check 5 -- and nothing is created or
    driven; the same refusal comes from a process with no worker at all,
    and from an ordinary thread the engine once wrote a workflow into.
    """

    from cortex_platform.product.control import (
        CAPTURE_CONSUMER_WORKSPACE_TITLE,
        CAPTURE_THREAD_TITLE_PREFIX,
    )
    from cortex_platform.product.engine.capture_consumer import (
        MACHINE_ACTOR,
        RESEARCH_CAPTURE_WORKFLOW,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    capture_id = str(
        store.create_capture(
            payload="https://example.test/paper",
            note="",
            actor_id="local-operator",
            idempotency_key="capture-create-0000001",
        ).value["id"]
    )
    workspace = store.create_workspace(
        title=CAPTURE_CONSUMER_WORKSPACE_TITLE,
        actor_id=MACHINE_ACTOR,
        idempotency_key="capture-workspace-000001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title=f"{CAPTURE_THREAD_TITLE_PREFIX}{capture_id}",
        expected_revision=workspace["revision"],
        actor_id=MACHINE_ACTOR,
        idempotency_key="capture-thread-0000001",
    ).value

    stored = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "start on this?", "expected_revision": thread["revision"]},
        key="message-command-000001",
    )
    assert stored.status == 201
    assert bridge.submitted == []

    fresh = store.get_thread(str(thread["id"]))
    refused = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": fresh["revision"]},
        key="run-command-0000001",
    )

    assert refused.status == 409
    assert refused.content_type == "application/problem+json"
    assert refused.payload["category"] == "machine_thread"
    assert refused.payload["retryable"] is False
    assert store.get_thread(str(thread["id"]))["active_run_id"] is None
    assert store.list_thread_runs(thread_id=str(thread["id"])) == []
    assert bridge.submitted == []

    # Any process shape: a demo-mode API with no worker refuses the same way.
    demo = ControlAPI(store, access_token=TOKEN)
    demo_refused = _post(
        demo,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": fresh["revision"]},
        key="run-command-0000002",
    )
    assert demo_refused.status == 409
    assert demo_refused.payload["category"] == "machine_thread"
    assert store.list_thread_runs(thread_id=str(thread["id"])) == []

    # An ordinary thread the engine once wrote a workflow into.
    ordinary = _thread_with_message(api)
    carrier = store.create_run(
        thread_id=ordinary["id"],
        expected_revision=ordinary["revision"],
        actor_id="machine:test-engine",
        idempotency_key="workflow-run-0000001",
        workflow=RESEARCH_CAPTURE_WORKFLOW,
    ).value
    # ⟦ADJ-4⟧ Ended the way the engine ends it: a run carrying the capture
    # workflow is the engine's to end, and the store refuses an operator.
    run = store.transition_run(
        run_id=carrier["id"],
        target_state="cancel_requested",
        expected_revision=carrier["revision"],
        actor_id=MACHINE_ACTOR,
        idempotency_key="cancel-command-000001",
    ).value
    store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        expected_revision=run["revision"],
        category=CANCELED_BEFORE_BINDING,
        actor_id=MACHINE_ACTOR,
        idempotency_key="cancel-converge-000001",
    )
    idle = store.get_thread(ordinary["id"])
    assert idle["active_run_id"] is None
    once_workflow = _post(
        api,
        f"/api/v1/threads/{ordinary['id']}/runs",
        {"expected_revision": idle["revision"]},
        key="run-command-0000003",
    )
    assert once_workflow.status == 409
    assert once_workflow.payload["category"] == "machine_thread"
    assert [item["id"] for item in store.list_thread_runs(thread_id=ordinary["id"])] == [carrier["id"]]
    assert bridge.submitted == []


def test_an_operator_thread_in_the_engine_workspace_still_gets_a_run(tmp_path: Path) -> None:
    """⟦V-R4⟧ The refusal keys on the consumer's receipt, not on a title."""

    from cortex_platform.product.control import CAPTURE_CONSUMER_WORKSPACE_TITLE
    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    workspace = store.create_workspace(
        title=CAPTURE_CONSUMER_WORKSPACE_TITLE,
        actor_id=MACHINE_ACTOR,
        idempotency_key="capture-workspace-000001",
    ).value
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "capture my thoughts", "expected_revision": workspace["revision"]},
        key="thread-command-00000001",
    ).payload
    message = _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "a note to self", "expected_revision": thread["revision"]},
        key="message-command-000001",
    )
    assert message.status == 201

    created = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": store.get_thread(thread["id"])["revision"]},
        key="run-command-0000001",
    )

    assert created.status == 201
    assert created.payload["state"] == "queued"
    assert bridge.submitted == [thread["id"]]


# ⟦N-1⟧ -----------------------------------------------------------------------


_REQUESTED = {"cancel": "cancel_requested", "pause": "pause_requested"}


def _transition_receipt(store: ControlStore, run: dict, action: str, *, key: str):
    """The receipt the store would replay for this cancel/pause, or None."""

    return store.replay_command(
        actor_id="local-operator",
        operation=store.transition_operation(run["id"], _REQUESTED[action]),
        idempotency_key=key,
        request=store.transition_request(
            target_state=_REQUESTED[action], expected_revision=int(run["revision"])
        ),
    )


def _assert_refused_machine_run(
    response, store: ControlStore, run: dict, thread_id: str, events_before: list[dict]
) -> None:
    assert response.status == 409
    assert response.content_type == "application/problem+json"
    assert response.payload["category"] == "machine_run"
    assert response.payload["retryable"] is False
    after = store.get_run(run["id"])
    assert after["state"] == "queued"
    assert after["revision"] == run["revision"]
    assert _run_events(store, run["id"]) == events_before
    assert store.get_thread(thread_id)["active_run_id"] == run["id"]


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_the_engine_carrier_is_not_ended_from_the_api(tmp_path: Path, action: str) -> None:
    """⟦N-1⟧ One cockpit click on a gen-13 carrier must not fence its capture.

    The carrier owns its workflow (`run_owns_workflow`); converging it to
    `canceled` in the same request would make `_expect_workflow_run_open`
    refuse that capture's workflow for ever. The real `ControlAPI` routes --
    with a bound worker and a bridge, then with no worker at all -- refuse
    409 `machine_run` before any transition, write nothing (no receipt under
    the refused key, no event, no revision), and the capture's effect can
    still be claimed afterwards.
    """

    from cortex_platform.tests.product.transports.test_turn_bridge import _carrier_run

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    carrier = _carrier_run(store)
    run = store.get_run(carrier["run_id"])
    assert store.run_owns_workflow(run["id"]) is True
    events_before = _run_events(store, run["id"])

    refused = _post(
        api,
        f"/api/v1/runs/{run['id']}/{action}",
        {"expected_revision": run["revision"]},
        key=f"{action}-command-00001",
    )

    _assert_refused_machine_run(refused, store, run, carrier["thread_id"], events_before)
    assert _transition_receipt(store, run, action, key=f"{action}-command-00001") is None
    assert bridge.forgotten == []
    # Any process shape: a demo-mode API with no worker refuses the same way.
    demo = ControlAPI(store, access_token=TOKEN)
    demo_refused = _post(
        demo,
        f"/api/v1/runs/{run['id']}/{action}",
        {"expected_revision": run["revision"]},
        key=f"{action}-command-00002",
    )
    _assert_refused_machine_run(demo_refused, store, run, carrier["thread_id"], events_before)
    # The harm prevented: the capture's workflow is not fenced.
    claimed = store.claim_workflow_effect(
        effect_id=str(carrier["effect"]["id"]),
        worker_id="p4-capture-consumer",
        lease_seconds=600,
    )
    assert claimed["state"] == "claimed"


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_a_legacy_gapped_carrier_is_not_ended_from_the_api(
    tmp_path: Path, action: str
) -> None:
    """⟦N-1⟧ No workflow row yet, but the run's own receipt names the consumer.

    The carrier a pre-V-3 consumer left between its two transactions, on a
    thread an operator has since typed into (rows a gen-12 store holds):
    `run_owns_workflow` is false and `run_creator` is the machine actor --
    the fact the consumer itself keys its repair on (V6-3). It is the
    engine's run, refused the same way.
    """

    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR
    from cortex_platform.tests.product.transports.test_turn_bridge import (
        _legacy_gapped_carrier,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    legacy = _legacy_gapped_carrier(store)
    run = store.get_run(legacy["run_id"])
    assert store.run_owns_workflow(run["id"]) is False
    assert store.run_creator(run["id"]) == MACHINE_ACTOR
    events_before = _run_events(store, run["id"])

    refused = _post(
        api,
        f"/api/v1/runs/{run['id']}/{action}",
        {"expected_revision": run["revision"]},
        key=f"{action}-command-00001",
    )

    _assert_refused_machine_run(refused, store, run, legacy["thread_id"], events_before)
    assert _transition_receipt(store, run, action, key=f"{action}-command-00001") is None
    assert bridge.forgotten == []


def _pre_fix_operator_cancel(store: ControlStore, run: dict, *, key: str) -> dict:
    """The rows a pre-ADJ-4 daemon left: an operator's cancel committed on a carrier.

    ⟦ADJ-4⟧ The store no longer writes that transition for an operator, so the
    rows are written the way a store an earlier generation wrote still holds
    them: the transition itself is the engine's own (the machine-actor
    bypass), and the operator's receipt under `key` is that receipt copied
    under the operator's actor -- byte-identical, because
    `transition_request` depends only on the target and the revision.
    """

    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    committed = store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=int(run["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key="engine-close-000001",
    ).value
    operation = store.transition_operation(run["id"], "cancel_requested")
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """INSERT INTO idempotency_receipts
               (actor_id, operation, idempotency_key, request_hash,
                status_code, response_json, created_at)
               SELECT 'local-operator', operation, ?, request_hash,
                      status_code, response_json, created_at
               FROM idempotency_receipts
               WHERE actor_id = ? AND operation = ?
                 AND idempotency_key = 'engine-close-000001'""",
            (key, MACHINE_ACTOR, operation),
        )
    return committed


@pytest.mark.parametrize("shape", ["carrier", "legacy"])
@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_the_store_itself_refuses_to_end_an_engine_owned_run(
    tmp_path: Path, shape: str, action: str
) -> None:
    """⟦ADJ-4⟧ N-1 is a STORE invariant, not a route policy.

    `ControlStore.transition_run` refuses `cancel_requested` / `pause_requested`
    on a run the engine owns -- the gen-13 carrier (owns the capture workflow,
    created by the consumer) and the legacy gapped carrier (no workflow row;
    its `create_run` receipt names the consumer) -- for any actor but the
    consumer's own, typed `MachineRunRefused` / `machine_run`, before the
    revision is checked (no revision will ever make it allowed) and before
    anything is written: no receipt, no event, no revision, the thread's
    active run kept. This is what closes the Telegram adapter's write path
    (`_apply_run_action`), which never went through the API's refusal.
    """

    from cortex_platform.product.control import MachineRunRefused
    from cortex_platform.tests.product.transports.test_turn_bridge import (
        _carrier_run,
        _legacy_gapped_carrier,
    )

    store = _store(tmp_path)
    built = _carrier_run(store) if shape == "carrier" else _legacy_gapped_carrier(store)
    run = store.get_run(built["run_id"])
    events_before = _run_events(store, run["id"])

    for revision in (int(run["revision"]), int(run["revision"]) + 1):
        with pytest.raises(MachineRunRefused) as refused:
            store.transition_run(
                run_id=run["id"],
                target_state=_REQUESTED[action],
                expected_revision=revision,
                actor_id="local-operator",
                idempotency_key=f"{action}-store-{revision:06d}",
            )
        assert refused.value.category == "machine_run"
        assert refused.value.retryable is False
        assert refused.value.run_id == run["id"]

    after = store.get_run(run["id"])
    assert after["state"] == "queued"
    assert after["revision"] == run["revision"]
    assert _run_events(store, run["id"]) == events_before
    assert store.get_thread(built["thread_id"])["active_run_id"] == run["id"]
    assert _transition_receipt(store, run, action, key=f"{action}-store-000000") is None


def test_the_engine_itself_still_ends_its_carrier(tmp_path: Path) -> None:
    """⟦ADJ-4⟧ The machine-actor bypass: `capture_consumer._close_run`'s shape.

    The consumer drives its own carrier terminal through the unbound path --
    `cancel_requested` under `MACHINE_ACTOR`, then `fail_unbound_run` -- and
    the store invariant is keyed on the actor so exactly that keeps working.
    An operator's workflow-less run on the same machine thread is not the
    engine's and stays cancellable (the `ForeignCarrierRun` recovery).
    """

    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR
    from cortex_platform.tests.product.transports.test_turn_bridge import _carrier_run

    store = _store(tmp_path)
    carrier = _carrier_run(store)
    run = store.get_run(carrier["run_id"])

    requested = store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=int(run["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key="engine-close-000001",
    ).value
    assert requested["state"] == "cancel_requested"
    settled = store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=str(requested["active_attempt_id"]),
        expected_revision=int(requested["revision"]),
        category="capture_effect_settled",
        actor_id=MACHINE_ACTOR,
        idempotency_key="engine-settle-00001",
    ).value
    assert settled["state"] == "canceled"

    # The thread is free again; an operator's own run there is theirs to end.
    thread = store.get_thread(carrier["thread_id"])
    foreign = store.create_run(
        thread_id=thread["id"],
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-run-on-capture-01",
    ).value
    assert store.thread_is_machine(thread["id"]) is True
    ended = store.transition_run(
        run_id=foreign["id"],
        target_state="cancel_requested",
        expected_revision=int(foreign["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-cancel-000001",
    ).value
    assert ended["state"] == "cancel_requested"


def test_a_cancel_that_committed_before_the_refusal_is_replayed_not_refused(
    tmp_path: Path,
) -> None:
    """⟦N-1⟧ Receipt before refusal, over the store's own request hash.

    A cancel a pre-fix daemon committed on a carrier under this key answers
    its receipt (200, replayed) rather than a 409 -- the refusal's "not
    ended" would be false about it -- and leaves the run as the receipt says.
    The request the API asks the receipt about is `transition_request`, the
    store's own: the same key with another revision is an idempotency
    conflict, not a replay and not a `machine_run`.
    """

    from cortex_platform.tests.product.transports.test_turn_bridge import _carrier_run

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    carrier = _carrier_run(store)
    run = store.get_run(carrier["run_id"])
    committed = _pre_fix_operator_cancel(store, run, key="cancel-command-00001")
    assert committed["state"] == "cancel_requested"

    replayed = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"]},
        key="cancel-command-00001",
    )

    assert replayed.status == 200
    assert replayed.headers == (("Idempotency-Replayed", "true"),)
    assert replayed.payload["state"] == "cancel_requested"
    assert store.get_run(run["id"])["state"] == "cancel_requested"
    assert bridge.forgotten == [run["id"]]
    conflict = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"] + 1},
        key="cancel-command-00001",
    )
    assert conflict.status == 409
    assert conflict.payload["category"] == "idempotency_conflict"
    assert store.get_run(run["id"])["state"] == "cancel_requested"


def test_an_operator_run_left_on_a_capture_thread_still_cancels(tmp_path: Path) -> None:
    """⟦N-1⟧ Ownership, never the thread: the ForeignCarrierRun recovery needs this.

    A workflow-less run somebody else opened on the consumer's own thread
    (rows a pre-V6-3 cockpit left) is the run the operator must END before
    the capture can be reopened (V6-3, `test_capture_consumer`). The thread
    is a machine thread; the run is not the engine's, so cancel converges it
    to `canceled` as for any operator run, and pause is answered by the
    store's own transition rule, not by `machine_run`.
    """

    from cortex_platform.product.control import (
        CAPTURE_CONSUMER_WORKSPACE_TITLE,
        CAPTURE_THREAD_TITLE_PREFIX,
    )
    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    capture_id = str(
        store.create_capture(
            payload="https://example.test/foreign",
            note="",
            actor_id="local-operator",
            idempotency_key="capture-create-0000001",
        ).value["id"]
    )
    workspace = store.create_workspace(
        title=CAPTURE_CONSUMER_WORKSPACE_TITLE,
        actor_id=MACHINE_ACTOR,
        idempotency_key="capture-workspace-000001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title=f"{CAPTURE_THREAD_TITLE_PREFIX}{capture_id}",
        expected_revision=workspace["revision"],
        actor_id=MACHINE_ACTOR,
        idempotency_key="capture-thread-0000001",
    ).value
    foreign = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-run-on-capture-0001",
    ).value
    assert store.thread_is_machine(str(thread["id"])) is True
    assert store.run_owns_workflow(foreign["id"]) is False
    assert store.run_creator(foreign["id"]) == "local-operator"

    paused = _post(
        api,
        f"/api/v1/runs/{foreign['id']}/pause",
        {"expected_revision": foreign["revision"]},
        key="pause-command-000001",
    )
    assert paused.status == 409
    assert paused.payload["category"] == "invalid_transition"
    assert store.get_run(foreign["id"])["state"] == "queued"

    canceled = _post(
        api,
        f"/api/v1/runs/{foreign['id']}/cancel",
        {"expected_revision": foreign["revision"]},
        key="cancel-command-00001",
    )

    assert canceled.status == 200
    assert canceled.payload["state"] == "canceled"
    assert store.get_run(foreign["id"])["state"] == "canceled"
    assert store.get_thread(str(thread["id"]))["active_run_id"] is None
    assert bridge.forgotten == [foreign["id"]]


def test_an_ordinary_run_is_paused_and_cancelled_as_before(tmp_path: Path) -> None:
    """⟦N-1⟧ Regression: the operator's own runs meet no new refusal.

    Pause of a queued run is the store's transition rule; cancel converges
    it and tells the bridge; the replay of that cancel answers the receipt
    and tells the bridge again, exactly as before the guard existed.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, bridge = _driving_api(store)
    thread = _thread_with_message(api)
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    ).payload
    assert store.run_owns_workflow(run["id"]) is False

    paused = _post(
        api,
        f"/api/v1/runs/{run['id']}/pause",
        {"expected_revision": run["revision"]},
        key="pause-command-000001",
    )
    assert paused.status == 409
    assert paused.payload["category"] == "invalid_transition"
    assert store.get_run(run["id"])["state"] == "queued"

    canceled = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"]},
        key="cancel-command-00001",
    )
    assert canceled.status == 200
    assert canceled.payload["state"] == "canceled"
    assert bridge.forgotten == [run["id"]]

    replayed = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"]},
        key="cancel-command-00001",
    )
    assert replayed.status == 200
    assert replayed.headers == (("Idempotency-Replayed", "true"),)
    assert store.get_run(run["id"])["state"] == "canceled"
    assert bridge.forgotten == [run["id"], run["id"]]


def test_the_thread_projection_says_whether_the_engine_owns_it(tmp_path: Path) -> None:
    """⟦ADJ-1⟧ `engine_owned` on every thread the API returns, computed, never stored.

    The composer has to branch on engine ownership BEFORE its active-run
    short-circuit, and the wire payload carried no signal for it. Each
    route that returns a thread -- create, get, list -- adds exactly one
    key, `engine_owned`, which is `ControlStore.thread_is_machine` at the
    time of the read: true for the gen-13 carrier's thread and for the
    legacy gapped carrier's, false for an operator's thread in the engine's
    workspace and for an ordinary thread. No column, no migration.
    """

    from cortex_platform.tests.product.transports.test_turn_bridge import (
        _carrier_run,
        _legacy_gapped_carrier,
    )

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, _bridge = _driving_api(store)

    def get(target: str):
        return api.handle(method="GET", target=target, headers=_headers())

    carrier = _carrier_run(store)
    carrier_thread = store.get_thread(carrier["thread_id"])
    got = get(f"/api/v1/threads/{carrier['thread_id']}")
    assert got.status == 200
    assert got.payload["engine_owned"] is True
    assert {key: value for key, value in got.payload.items() if key != "engine_owned"} == dict(
        carrier_thread
    )

    workspace_id = str(carrier_thread["workspace_id"])
    operator = _post(
        api,
        f"/api/v1/workspaces/{workspace_id}/threads",
        {
            "title": "capture my thoughts",
            "expected_revision": store.get_workspace(workspace_id)["revision"],
        },
        key="thread-command-000009",
    )
    assert operator.status == 201
    assert operator.payload["engine_owned"] is False
    listed = get(f"/api/v1/threads?workspace_id={workspace_id}")
    assert sorted(
        (item["id"], item["engine_owned"]) for item in listed.payload["items"]
    ) == sorted([(carrier["thread_id"], True), (operator.payload["id"], False)])

    ordinary = _thread_with_message(api)
    assert ordinary["engine_owned"] is False
    assert set(ordinary) == {
        "active_run_id",
        "archived_at",
        "created_at",
        "engine_owned",
        "id",
        "revision",
        "status",
        "title",
        "updated_at",
        "workspace_id",
    }

    legacy = _legacy_gapped_carrier(store)
    assert get(f"/api/v1/threads/{legacy['thread_id']}").payload["engine_owned"] is True


def test_the_engine_owned_ids_of_a_workspace_are_one_query_and_the_same_answer(
    tmp_path: Path,
) -> None:
    """⟦A-1⟧ `machine_thread_ids` is `thread_is_machine` over a workspace.

    The list route needs ownership for every thread it returns, so it asks
    once for the workspace instead of once per row. That is only allowed to
    be cheaper if it is the SAME answer, on every shape the predicate has to
    recognise: the gen-13 carrier (a machine receipt and a workflow), the
    gen-12 gapped carrier (a machine receipt and no workflow, on a thread an
    operator has since typed into), and an operator's own thread inside the
    engine's workspace, which must stay the engine's business and not become
    it. Asserted as set equality per workspace, not as a spot check.
    """

    from cortex_platform.tests.product.transports.test_turn_bridge import (
        _carrier_run,
        _legacy_gapped_carrier,
    )

    store = _store(tmp_path)
    carrier = _carrier_run(store)
    legacy = _legacy_gapped_carrier(store)
    api, _bridge = _driving_api(store)

    def workspace_of(thread_id: str) -> str:
        return str(store.get_thread(thread_id)["workspace_id"])

    operator_threads = []
    for index, machine_thread in enumerate((carrier["thread_id"], legacy["thread_id"])):
        workspace_id = workspace_of(machine_thread)
        created = _post(
            api,
            f"/api/v1/workspaces/{workspace_id}/threads",
            {
                "title": "capture my thoughts",
                "expected_revision": store.get_workspace(workspace_id)["revision"],
            },
            key=f"a1-operator-thread-{index:04d}",
        )
        assert created.status == 201
        operator_threads.append(str(created.payload["id"]))
    ordinary = _thread_with_message(api)

    for workspace in store.list_workspaces():
        workspace_id = str(workspace["id"])
        threads = [
            str(thread["id"])
            for thread in store.list_threads(workspace_id=workspace_id)
        ]
        assert store.machine_thread_ids(workspace_id) == {
            thread_id for thread_id in threads if store.thread_is_machine(thread_id)
        }

    # And not vacuously equal in either direction: each carrier workspace
    # holds exactly its own carrier, and the operator's threads hold none.
    assert store.machine_thread_ids(workspace_of(carrier["thread_id"])) == {
        carrier["thread_id"]
    }
    assert store.machine_thread_ids(workspace_of(legacy["thread_id"])) == {
        legacy["thread_id"]
    }
    assert store.machine_thread_ids(str(ordinary["workspace_id"])) == set()
    assert [store.thread_is_machine(item) for item in operator_threads] == [
        False,
        False,
    ]


def test_the_thread_list_asks_ownership_once_however_long_the_list_is(
    tmp_path: Path,
) -> None:
    """⟦A-1⟧ The cost of `GET /threads` no longer grows with the list.

    `engine_owned` used to be `thread_is_machine` per row, and that opens a
    FRESH connection per call -- three PRAGMAs, `synchronous = FULL` among
    them -- on a list that takes no limit, paginates to nothing, is never
    pruned, and gains a carrier thread per capture for ever. Measured rather
    than argued: the number of store connections the route opens is the same
    for a one-thread workspace and a six-thread one, and the per-thread
    predicate is not called at all. Both assertions fail on the old shape,
    where the count grows by exactly one per thread.
    """

    from cortex_platform.tests.product.transports.test_turn_bridge import _carrier_run

    store = _store(tmp_path)
    carrier = _carrier_run(store)
    api, _bridge = _driving_api(store)
    workspace_id = str(store.get_thread(carrier["thread_id"])["workspace_id"])

    connects: list[int] = []
    asked: list[str] = []
    connect = store._connect  # noqa: SLF001 - the cost under test
    predicate = store.thread_is_machine

    def counting_connect():
        connects.append(1)
        return connect()

    def counting_predicate(thread_id: str) -> bool:
        asked.append(thread_id)
        return predicate(thread_id)

    def list_threads():
        connects.clear()
        asked.clear()
        store._connect = counting_connect  # type: ignore[method-assign] # noqa: SLF001
        store.thread_is_machine = counting_predicate  # type: ignore[method-assign]
        try:
            return api.handle(
                method="GET",
                target=f"/api/v1/threads?workspace_id={workspace_id}",
                headers=_headers(),
            )
        finally:
            store._connect = connect  # type: ignore[method-assign] # noqa: SLF001
            store.thread_is_machine = predicate  # type: ignore[method-assign]

    one = list_threads()
    assert one.status == 200
    assert len(one.payload["items"]) == 1
    baseline = len(connects)
    assert asked == []

    for index in range(5):
        created = _post(
            api,
            f"/api/v1/workspaces/{workspace_id}/threads",
            {
                "title": f"thread {index}",
                "expected_revision": store.get_workspace(workspace_id)["revision"],
            },
            key=f"a1-list-thread-{index:04d}",
        )
        assert created.status == 201

    six = list_threads()
    assert six.status == 200
    assert len(six.payload["items"]) == 6
    assert len(connects) == baseline
    assert asked == []

    # Cheaper, and still the right answer: the carrier is the engine's, the
    # five threads an operator opened in the same workspace are not.
    assert {
        item["id"]: item["engine_owned"] for item in six.payload["items"]
    }[carrier["thread_id"]] is True
    assert sum(1 for item in six.payload["items"] if item["engine_owned"]) == 1

def test_the_workspace_projection_says_whether_the_engine_made_it(
    tmp_path: Path,
) -> None:
    """⟦P9-2⟧ `engine_owned` on every workspace the API returns.

    The cockpit's picker listed the engine's own `Capture consumer`
    workspace beside the operator's research, with nothing on the wire to
    tell them apart. The workspace routes -- create, get, list -- now add
    the one key, computed from the `create_workspace` receipt's actor: the
    same receipt-actor rule `thread_is_machine` reads, over the operation a
    workspace files. No column, no migration.

    Deliberately NOT "a workspace holding a machine thread": the consumer
    adopts a same-titled workspace by title and files no receipt when it
    does, so a workspace an operator made first and the consumer then used
    is still the operator's.
    """

    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)

    def get(target: str):
        return api.handle(method="GET", target=target, headers=_headers())

    engine = store.create_workspace(
        title="Capture consumer",
        actor_id=MACHINE_ACTOR,
        idempotency_key="p42-workspace-engine-0001",
    ).value
    operator = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Memory research"},
        key="workspace-command-0021",
    )
    assert operator.status == 201
    assert operator.payload["engine_owned"] is False

    assert store.workspace_is_machine(engine["id"]) is True
    assert store.workspace_creator(engine["id"]) == MACHINE_ACTOR
    assert store.workspace_creator(operator.payload["id"]) == "local-operator"

    got = get(f"/api/v1/workspaces/{engine['id']}")
    assert got.status == 200
    assert got.payload["engine_owned"] is True
    # Exactly one key added to what the route returned before.
    assert {
        key: value for key, value in got.payload.items() if key != "engine_owned"
    } == dict(engine)
    assert set(got.payload) == {
        "created_at",
        "engine_owned",
        "id",
        "revision",
        "title",
        "updated_at",
    }

    listed = get("/api/v1/workspaces")
    assert sorted(
        (item["title"], item["engine_owned"]) for item in listed.payload["items"]
    ) == [("Capture consumer", True), ("Memory research", False)]

    # A thread the consumer carries inside the operator's workspace makes the
    # THREAD the engine's and leaves the workspace the operator's.
    carrier = store.create_thread(
        workspace_id=operator.payload["id"],
        title="capture cap_7",
        expected_revision=operator.payload["revision"],
        actor_id=MACHINE_ACTOR,
        idempotency_key="p42-thread-carrier-0001",
    ).value
    assert store.thread_is_machine(carrier["id"]) is True
    assert get(f"/api/v1/workspaces/{operator.payload['id']}").payload["engine_owned"] is False


def test_a_run_is_refused_on_a_thread_with_nothing_to_answer(tmp_path: Path) -> None:
    """⟦V-2⟧ The undriven queued run, refused where it was created.

    "Start research run" on a thread nobody has written to committed a
    `queued` run that `run_is_conversation` excludes -- no user message and
    no transport binding -- so the bridge never picked it up, and it sat as
    the thread's active run until a message or a cancel cleared it. The API
    refuses it with its own category instead, before anything durable exists.

    The rule is the store's own `_drivable_thread_predicate`, spelled once and
    shared with `run_is_conversation`, so the API cannot refuse a run the
    bridge would have driven: a transport-bound thread has something to
    answer even with no message of its own.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, _bridge = _driving_api(store)

    workspace = _post(
        api, "/api/v1/workspaces", {"title": "Research"}, key="workspace-command-0031"
    ).payload
    empty = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Nothing said yet", "expected_revision": workspace["revision"]},
        key="thread-command-000031",
    ).payload

    refused = _post(
        api,
        f"/api/v1/threads/{empty['id']}/runs",
        {"expected_revision": empty["revision"]},
        key="run-command-0000031",
    )
    assert refused.status == 409
    assert refused.payload["category"] == "thread_has_no_user_message"
    assert refused.payload["retryable"] is False
    # Nothing was written: no run, and the thread is still idle rather than
    # holding an active run nobody will drive.
    assert store.list_thread_runs(
        thread_id=empty["id"], after_id=None, limit=10
    ) == []
    current = store.get_thread(empty["id"])
    assert current["active_run_id"] is None
    assert current["revision"] == empty["revision"]

    # The message the cockpit sends first is what makes the thread drivable.
    assert store.thread_can_carry_a_turn(empty["id"]) is False
    _post(
        api,
        f"/api/v1/threads/{empty['id']}/messages",
        {
            "role": "user",
            "content": "What did the paper claim?",
            "expected_revision": current["revision"],
        },
        key="message-command-00031",
    )
    assert store.thread_can_carry_a_turn(empty["id"]) is True
    started = _post(
        api,
        f"/api/v1/threads/{empty['id']}/runs",
        {"expected_revision": store.get_thread(empty["id"])["revision"]},
        key="run-command-0000032",
    )
    assert started.status == 201


def test_a_transport_bound_thread_needs_no_message_of_its_own(tmp_path: Path) -> None:
    """⟦V-2⟧ The refusal must not be narrower than what the bridge will drive.

    A thread a transport scope is bound to is answerable even with no user
    message stored through the API -- what is to be answered arrives as a
    delivery. `run_is_conversation` has always said so, and the refusal reads
    the same predicate, so the Telegram path is untouched.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, _bridge = _driving_api(store)

    workspace = _post(
        api, "/api/v1/workspaces", {"title": "Research"}, key="workspace-command-0041"
    ).payload
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Telegram", "expected_revision": workspace["revision"]},
        key="thread-command-000041",
    ).payload
    assert store.thread_can_carry_a_turn(thread["id"]) is False

    store.bind_transport(
        transport="telegram",
        external_scope="chat:4242",
        thread_id=thread["id"],
        actor_id="local-operator",
        idempotency_key="bind-command-0000041",
    )

    assert store.thread_can_carry_a_turn(thread["id"]) is True
    started = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": store.get_thread(thread["id"])["revision"]},
        key="run-command-0000041",
    )
    assert started.status == 201, started.payload


def test_the_workspace_list_asks_who_owns_them_once(tmp_path: Path) -> None:
    """⟦A-1⟧ One ownership query per list request, never one per row.

    `workspace_is_machine` opens a fresh connection per call and `_connect`
    runs three PRAGMAs on each, so asking it per row makes a list route cost
    N connection setups on a list nothing paginates. The review measured the
    thread equivalent at ~75x on 100 rows. The list asks once; the answers
    are identical to asking per row.
    """

    from cortex_platform.product.engine.capture_consumer import MACHINE_ACTOR

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)

    expected: dict[str, bool] = {}
    for index in range(6):
        engine_owned = index % 2 == 0
        workspace = store.create_workspace(
            title=f"Workspace {index}",
            actor_id=MACHINE_ACTOR if engine_owned else "local-operator",
            idempotency_key=f"workspace-listing-{index:04d}0000",
        ).value
        expected[str(workspace["id"])] = engine_owned

    per_row: list[str] = []
    real = store.workspace_is_machine
    store.workspace_is_machine = lambda workspace_id: (  # type: ignore[method-assign]
        per_row.append(workspace_id),
        real(workspace_id),
    )[1]
    listed = api.handle(
        method="GET", target="/api/v1/workspaces", headers=_headers()
    )

    assert listed.status == 200
    assert {
        str(item["id"]): item["engine_owned"] for item in listed.payload["items"]
    } == expected
    # The whole point: the list route asked nobody row by row.
    assert per_row == []

    # And the single-workspace route still answers for itself, per row, which
    # is one call either way.
    single = next(id for id, owned in expected.items() if owned)
    got = api.handle(
        method="GET", target=f"/api/v1/workspaces/{single}", headers=_headers()
    )
    assert got.payload["engine_owned"] is True
    assert per_row == [single]


def test_the_run_projection_says_whose_the_RUN_is_not_whose_the_thread_is(
    tmp_path: Path,
) -> None:
    """⟦ADJ-A⟧ `engine_owned` on every run the API returns, by RUN ownership.

    The cockpit hid every run button on an engine-owned THREAD, but the API
    refuses cancel and pause by RUN ownership -- deliberately, because a
    workflow-less run somebody else opened on a `capture` thread is theirs to
    end, and ending it is the whole of the `ForeignCarrierRun` recovery. The
    cockpit is the only surface that can send that cancel, so hiding the
    button removed the recovery. The DTO now carries the same pair
    `_machine_run_refusal` asks, so the cockpit hides exactly what the API
    would refuse.
    """

    from cortex_platform.product.control import CAPTURE_CONSUMER_ACTOR
    from cortex_platform.tests.product.transports.test_turn_bridge import _carrier_run

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)

    def get(target: str):
        return api.handle(method="GET", target=target, headers=_headers())

    carrier = _carrier_run(store)
    thread_id = str(carrier["thread_id"])

    # The engine's own carrier: the engine's run, on the engine's thread.
    got = get(f"/api/v1/runs/{carrier['run_id']}")
    assert got.status == 200
    assert got.payload["engine_owned"] is True
    assert {key: value for key, value in got.payload.items() if key != "engine_owned"} == dict(
        store.get_run(carrier["run_id"])
    )
    assert store.run_is_machine(carrier["run_id"]) is True

    # The foreign carrier run: an OPERATOR's run on the engine's thread. The
    # thread is the engine's, the run is not, and the API will let the
    # operator cancel it -- so the DTO must not say it is the engine's.
    store.transition_run(
        run_id=str(carrier["run_id"]),
        target_state="cancel_requested",
        expected_revision=int(store.get_run(carrier["run_id"])["revision"]),
        actor_id=CAPTURE_CONSUMER_ACTOR,
        idempotency_key="engine-cancel-00000001",
    )
    store.fail_unbound_run(
        run_id=str(carrier["run_id"]),
        attempt_id=str(store.get_run(carrier["run_id"])["active_attempt_id"]),
        expected_revision=int(store.get_run(carrier["run_id"])["revision"]),
        category="carrier_released",
        actor_id=CAPTURE_CONSUMER_ACTOR,
        idempotency_key="engine-release-0000001",
    )
    foreign = store.create_run(
        thread_id=thread_id,
        expected_revision=int(store.get_thread(thread_id)["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-foreign-run-0001",
    ).value

    assert store.thread_is_machine(thread_id) is True
    assert store.run_is_machine(foreign["id"]) is False
    assert get(f"/api/v1/runs/{foreign['id']}").payload["engine_owned"] is False

    # And the same answer in the run history, which asks once for the thread.
    history = get(f"/api/v1/threads/{thread_id}/runs?limit=100")
    assert {
        str(item["id"]): item["engine_owned"] for item in history.payload["items"]
    } == {str(carrier["run_id"]): True, str(foreign["id"]): False}

    # The set query is the same answer as asking run by run.
    assert store.machine_run_ids(thread_id) == {
        str(run["id"])
        for run in history.payload["items"]
        if store.run_is_machine(str(run["id"]))
    }

    # The refusal the flag predicts: the engine's run is refused, the
    # operator's own run on the same engine thread is cancellable.
    refused = _post(
        api,
        f"/api/v1/runs/{carrier['run_id']}/cancel",
        {"expected_revision": int(store.get_run(carrier["run_id"])["revision"])},
        key="operator-cancel-carrier01",
    )
    assert refused.status == 409
    assert refused.payload["category"] == "machine_run"
    allowed = _post(
        api,
        f"/api/v1/runs/{foreign['id']}/cancel",
        {"expected_revision": int(foreign["revision"])},
        key="operator-cancel-foreign01",
    )
    assert allowed.status == 200, allowed.payload
    assert allowed.payload["engine_owned"] is False


def test_a_run_on_an_unknown_thread_is_still_a_404(tmp_path: Path) -> None:
    """⟦ADJ-C⟧ The V-2 refusal must not answer for a thread that is not there.

    `thread_can_carry_a_turn` ran before `create_run`, which is what used to
    raise `NotFound`, and its `WHERE id = ? AND (<drivable>)` said False for a
    missing row -- so a run against an unknown thread answered 409
    `thread_has_no_user_message`, and the cockpit offered to send a message to
    a thread that does not exist.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, _bridge = _driving_api(store)

    missing = _post(
        api,
        "/api/v1/threads/thread_does_not_exist/runs",
        {"expected_revision": 0},
        key="run-command-0000051",
    )
    assert missing.status == 404
    assert missing.payload["category"] == "not_found"

    with pytest.raises(NotFound):
        store.thread_can_carry_a_turn("thread_does_not_exist")


def test_an_idempotent_retry_returns_a_run_the_cockpit_can_decode(
    tmp_path: Path,
) -> None:
    """⟦ADJ-G-2⟧ A replay is the same response, so it needs the same shape.

    Both refusal deciders short-circuit on a committed receipt -- a command
    that already ran is replayed, never refused -- and both returned the
    stored value unprojected. `engine_owned` is a REQUIRED field of the Run
    DTO, so the cockpit's `decodeRun` throws on the retry of a command whose
    first response it decoded fine: the network-hiccup path, which is exactly
    when a retry happens.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api, _bridge = _driving_api(store)
    thread = _thread_with_message(api)

    created = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000061",
    )
    assert created.status == 201
    assert created.payload["engine_owned"] is False

    replayed = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000061",
    )
    assert replayed.headers == (("Idempotency-Replayed", "true"),)
    assert "engine_owned" in replayed.payload
    assert replayed.payload == created.payload

    run_id = str(created.payload["id"])
    revision = int(store.get_run(run_id)["revision"])
    canceled = _post(
        api,
        f"/api/v1/runs/{run_id}/cancel",
        {"expected_revision": revision},
        key="run-cancel-00000061",
    )
    assert canceled.status == 200
    assert canceled.payload["engine_owned"] is False

    replayed_cancel = _post(
        api,
        f"/api/v1/runs/{run_id}/cancel",
        {"expected_revision": revision},
        key="run-cancel-00000061",
    )
    assert replayed_cancel.headers == (("Idempotency-Replayed", "true"),)
    assert "engine_owned" in replayed_cancel.payload
    assert replayed_cancel.payload["engine_owned"] is False
    assert set(replayed_cancel.payload) == set(canceled.payload)
    # NOT payload equality: the first cancel answers the CONVERGED run
    # (`_converge_cancellation` ends an unbound run `canceled` before it
    # replies) while the replay answers the receipt, which stored the
    # `cancel_requested` the command itself wrote. Pre-existing, and nothing
    # to do with the projection -- pinned here so the difference is on the
    # record rather than mistaken for one.
    assert canceled.payload["state"] == "canceled"
    assert replayed_cancel.payload["state"] == "cancel_requested"


class _PauselessBridge(_Bridge):
    """A bridge whose runtime told it, at dispatch, that it cannot pause."""

    def __init__(self, answer: bool | None = False) -> None:
        super().__init__()
        self.asked: list[str] = []
        self._answer = answer

    def runtime_supports(self, capability: str) -> bool | None:
        self.asked.append(capability)
        return self._answer


def _api_with(store: ControlStore, bridge: _Bridge) -> ControlAPI:
    return ControlAPI(
        store,
        access_token=TOKEN,
        managed_worker=_Worker(bound=True),
        turn_bridge=bridge,
    )


def _a_run(api: ControlAPI, *, suffix: str) -> dict:
    thread = _thread_with_message(api)
    return _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key=f"run-command-{suffix}",
    ).payload


def test_a_pause_the_runtime_cannot_perform_is_refused_not_committed(
    tmp_path: Path,
) -> None:
    """⟦P9-3 BRK-4⟧ The 200 that lied.

    `POST /runs/{id}/pause` returned 200 with the run in `pause_requested` and
    then quietly undid itself: the Hermes adapter reports `pause=False`, so the
    delivered action was rejected, the store rolled the run back to `running`,
    and the answer was delivered anyway. Nothing public said so -- the
    rejection is not a public event type, so the cockpit printed the literal
    string `event.redacted`, and the rollback writes no run event at all,
    leaving a timeline where `run.completed {from: running}` follows a
    `run.pause_requested` that nothing ever un-did.

    The outcome is knowable before the write -- `pause` is a property of the
    bound runtime, not of the request -- so the route refuses instead of
    committing something it will have to undo. Refused before the transition
    and after the receipt, the same shape and the same 409 as `machine_run`.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    bridge = _PauselessBridge()
    api = _api_with(store, bridge)
    run = _a_run(api, suffix="0000001")
    events_before = _run_events(store, run["id"])

    refused = _post(
        api,
        f"/api/v1/runs/{run['id']}/pause",
        {"expected_revision": run["revision"]},
        key="pause-unsupported-00001",
    )

    assert refused.status == 409, refused.payload
    assert refused.payload["category"] == "pause_unsupported", refused.payload
    assert bridge.asked == ["pause"]
    # Nothing was written: not the transition, not a receipt under the key.
    after = store.get_run(run["id"])
    assert after["state"] == run["state"]
    assert after["revision"] == run["revision"]
    assert _run_events(store, run["id"]) == events_before
    assert (
        _transition_receipt(store, run, "pause", key="pause-unsupported-00001")
        is None
    )


def test_only_the_pause_is_refused_and_only_on_a_definite_answer(
    tmp_path: Path,
) -> None:
    """⟦P9-3 BRK-4⟧ The guard is narrow in both directions.

    A runtime that cannot pause can still be CANCELLED, so the refusal must not
    generalise to the other control action. And `runtime_supports` answers None
    until a turn has told this daemon something: refusing on an unknown would
    invent a capability report, which is the failure being fixed rather than a
    fix, so an unknown behaves exactly as it did before this existed.
    """

    store = _store(tmp_path)
    _enable_dispatch(store)
    api = _api_with(store, _PauselessBridge())
    run = _a_run(api, suffix="0000002")

    canceled = _post(
        api,
        f"/api/v1/runs/{run['id']}/cancel",
        {"expected_revision": run["revision"]},
        key="cancel-still-works-00001",
    )
    assert canceled.status == 200, canceled.payload

    unknown_api = _api_with(store, _PauselessBridge(answer=None))
    other = _a_run(unknown_api, suffix="0000003")
    unknown = _post(
        unknown_api,
        f"/api/v1/runs/{other['id']}/pause",
        {"expected_revision": other["revision"]},
        key="pause-unknown-000001",
    )
    assert unknown.payload.get("category") != "pause_unsupported", unknown.payload


def _ticking_store(path: Path) -> ControlStore:
    """A store whose clock advances, so `created_at` orders the rows it writes.

    ⟦batchK-8⟧ `_store`'s clock is frozen, which is the HARDER case for a
    keyset cursor -- every thread shares one `created_at` and only the id
    tie-break separates them. Both are exercised, because the cursor has to
    be right on each branch of its own WHERE.
    """

    counter = itertools.count()
    store = ControlStore(
        path / "control.db",
        clock=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
        + timedelta(seconds=next(counter)),
    )
    store.initialize()
    return store


def _consumer_workspace(store: ControlStore) -> dict:
    from cortex_platform.product.control.store import CAPTURE_CONSUMER_ACTOR

    return dict(
        store.create_workspace(
            title="Capture consumer",
            actor_id=CAPTURE_CONSUMER_ACTOR,
            idempotency_key="batchk8-workspace-00001",
        ).value
    )


def _mixed_threads(store: ControlStore, workspace: dict, count: int) -> list[str]:
    """`count` threads in one workspace, every third one the engine's own.

    The engine's are made the way `_MACHINE_THREAD_PREDICATE`'s first half
    recognises them -- a `create_thread` receipt naming the capture consumer's
    actor -- and the rest by an operator in the same workspace, which is the
    pair the predicate has to keep apart.
    """

    from cortex_platform.product.control.store import CAPTURE_CONSUMER_ACTOR

    workspace_id = str(workspace["id"])
    created: list[str] = []
    for index in range(count):
        machine = index % 3 == 0
        thread = store.create_thread(
            workspace_id=workspace_id,
            title=(f"capture cap_{index:06d}" if machine else f"thread {index}"),
            expected_revision=int(
                store.get_workspace(workspace_id)["revision"]
            ),
            actor_id=CAPTURE_CONSUMER_ACTOR if machine else "operator:reed",
            idempotency_key=f"batchk8-thread-{index:06d}",
        ).value
        created.append(str(thread["id"]))
    return created


def _walk(api: ControlAPI, workspace_id: str, limit: int) -> tuple[list[str], int]:
    """Every thread id the paged route returns, and how many pages it took."""

    ids: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        target = f"/api/v1/threads?workspace_id={workspace_id}&limit={limit}"
        if cursor is not None:
            target += f"&after_id={cursor}"
        response = api.handle(method="GET", target=target, headers=_headers())
        assert response.status == 200, response.payload
        pages += 1
        assert len(response.payload["items"]) <= limit
        ids.extend(str(item["id"]) for item in response.payload["items"])
        cursor = response.payload["next_cursor"]
        if cursor is None:
            return ids, pages
        assert pages < 50, "the cursor did not terminate"


def test_the_thread_list_pages_to_exactly_the_list_it_used_to_return_whole(
    tmp_path: Path,
) -> None:
    """⟦batchK-8⟧ `GET /threads` pages, and no page changes the answer.

    The route took no `limit` and hard-coded `next_cursor` to null on a list
    that grows by one carrier thread per capture for ever: the only way to see
    a workspace was to read all of it. Now a `limit` bounds a page and the
    cursor walks the rest -- and the walk has to reproduce the unpaged list
    EXACTLY, same rows in the same order, with no row skipped and none
    returned twice.

    Both branches of the cursor's WHERE are exercised. With the frozen clock
    every thread shares one `created_at`, so only the `id` tie-break can
    separate them; with an advancing clock the `created_at` comparison does
    the work. A cursor that got either branch wrong would repeat or drop a row
    here, and the multiset comparison would fail.

    The default is unchanged, which is what keeps the cockpit working: no
    `limit` is still the whole list, still with a null cursor.
    """

    for maker in (_store, _ticking_store):
        store = maker(tmp_path / maker.__name__)
        api, _bridge = _driving_api(store)
        workspace = _consumer_workspace(store)
        workspace_id = str(workspace["id"])
        created = _mixed_threads(store, workspace, 10)

        whole = api.handle(
            method="GET",
            target=f"/api/v1/threads?workspace_id={workspace_id}",
            headers=_headers(),
        )
        assert whole.status == 200, whole.payload
        assert whole.payload["next_cursor"] is None
        unpaged = [str(item["id"]) for item in whole.payload["items"]]
        assert sorted(unpaged) == sorted(created)

        for limit in (1, 3, 4, 10, 11):
            walked, pages = _walk(api, workspace_id, limit)
            assert walked == unpaged, (maker.__name__, limit)
            assert len(walked) == len(set(walked)), (maker.__name__, limit)
            assert pages == max(1, -(-len(unpaged) // limit)) or limit >= len(unpaged)


def test_a_thread_created_during_a_walk_never_disturbs_the_pages_already_read(
    tmp_path: Path,
) -> None:
    """⟦batchK-8⟧ The cursor is a keyset, not an offset.

    A carrier thread is created for every capture, so a walk of a live
    workspace WILL race one. An offset cursor would shift every later page by
    one and return a row twice; a keyset one asks for what sorts after the
    row it names, so the pages already read are untouched and the new thread
    appears once, at the end, where its `created_at` puts it.
    """

    store = _ticking_store(tmp_path)
    api, _bridge = _driving_api(store)
    workspace = _consumer_workspace(store)
    workspace_id = str(workspace["id"])
    before = _mixed_threads(store, workspace, 6)

    first = api.handle(
        method="GET",
        target=f"/api/v1/threads?workspace_id={workspace_id}&limit=2",
        headers=_headers(),
    )
    assert first.status == 200, first.payload
    page_one = [str(item["id"]) for item in first.payload["items"]]
    assert page_one == before[:2]

    late = store.create_thread(
        workspace_id=workspace_id,
        title="capture cap_999999",
        expected_revision=int(store.get_workspace(workspace_id)["revision"]),
        actor_id="operator:reed",
        idempotency_key="batchk8-late-thread-01",
    ).value

    rest: list[str] = []
    cursor = first.payload["next_cursor"]
    assert cursor == page_one[-1]
    while cursor is not None:
        response = api.handle(
            method="GET",
            target=(
                f"/api/v1/threads?workspace_id={workspace_id}&limit=2"
                f"&after_id={cursor}"
            ),
            headers=_headers(),
        )
        assert response.status == 200, response.payload
        rest.extend(str(item["id"]) for item in response.payload["items"])
        cursor = response.payload["next_cursor"]

    assert page_one + rest == [*before, str(late["id"])]
    assert len(page_one + rest) == len(set(page_one + rest))


def test_every_page_of_the_thread_list_says_whose_each_thread_is_and_asks_for_no_more(
    tmp_path: Path,
) -> None:
    """⟦batchK-8 + A-1⟧ `engine_owned` on every item of every page, for the page.

    Two things at once, because they are the same change. The flag has to
    survive paging -- a cockpit that pages must be told whose each thread is
    on every page, not only the first -- and it must be asked for the PAGE:
    `_MACHINE_THREAD_PREDICATE`'s receipt half is a scan of
    `idempotency_receipts` for each thread it is asked about, so asking for a
    whole workspace to project three rows puts the cost back where the
    pagination just took it from.

    Asserted against `thread_is_machine` per row, which is the definition, and
    against the ids the route actually handed the store.

    ⟦batchO ADJ-1⟧ And the READ is asserted too, from the SQL sqlite actually
    executed. The ownership half above pins what the route asks ABOUT the
    page; without this the page itself rested on a docstring -- a
    `list_threads` that reads the whole workspace and slices the page out in
    Python answers every one of the assertions above identically, so the cost
    property batchK 8 exists to obtain was unpinned. The bound is `limit + 1`
    because the cursor is proven by reading one row further, never guessed
    from the page being full.
    """

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)
    workspace = _consumer_workspace(store)
    workspace_id = str(workspace["id"])
    _mixed_threads(store, workspace, 7)

    asked: list[list[str] | None] = []
    machine_thread_ids = store.machine_thread_ids

    def recording_machine_thread_ids(value, *, thread_ids=None):
        asked.append(None if thread_ids is None else [str(item) for item in thread_ids])
        return machine_thread_ids(value, thread_ids=thread_ids)

    # `set_trace_callback` reports the statement with its parameters already
    # substituted, so the LIMIT the store bound is readable rather than a `?`.
    #
    # ⟦batchQ A-3⟧ The bound is read out with a regex over the statement, not
    # matched against the statement's text. Matching the text made a REFORMAT
    # of the SQL -- a line break moved, a clause re-indented -- indistinguishable
    # from the bound disappearing: both turned this list empty and both went
    # red, so the test could not say which had happened and a green run proved
    # only that nobody had touched the formatting. `FROM threads` selects the
    # page read among the statements one request runs, and `LIMIT <n>` is the
    # property under test; a whole-workspace read sliced in Python carries no
    # numeric bound at all, so it drops out of this list rather than changing
    # its shape.
    traced: list[str] = []
    connect = store._connect

    @contextlib.contextmanager
    def tracing_connect():
        with connect() as conn:
            conn.set_trace_callback(traced.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    def page_reads() -> list[int]:
        bounds: list[int] = []
        for statement in traced:
            if "FROM threads" not in statement:
                continue
            match = re.search(r"LIMIT\s+(\d+)", statement)
            if match is not None:
                bounds.append(int(match.group(1)))
        return bounds

    store.machine_thread_ids = recording_machine_thread_ids  # type: ignore[method-assign]
    store._connect = tracing_connect  # type: ignore[method-assign]
    try:
        cursor: str | None = None
        seen: dict[str, bool] = {}
        while True:
            target = f"/api/v1/threads?workspace_id={workspace_id}&limit=3"
            if cursor is not None:
                target += f"&after_id={cursor}"
            response = api.handle(method="GET", target=target, headers=_headers())
            assert response.status == 200, response.payload
            for item in response.payload["items"]:
                assert "engine_owned" in item, item
                seen[str(item["id"])] = bool(item["engine_owned"])
            cursor = response.payload["next_cursor"]
            if cursor is None:
                break
    finally:
        store.machine_thread_ids = machine_thread_ids  # type: ignore[method-assign]
        store._connect = connect  # type: ignore[method-assign]

    assert len(seen) == 7
    assert seen == {
        thread_id: store.thread_is_machine(thread_id) for thread_id in seen
    }
    # Three of the seven are the engine's, so the flag is not vacuously one
    # value across the pages it had to survive.
    assert sum(seen.values()) == 3

    # One ownership query per page, scoped to that page and never to the
    # workspace: three pages of 3, 3 and 1.
    assert [len(item or []) for item in asked] == [3, 3, 1]
    assert None not in asked

    # ⟦batchO ADJ-1⟧ And the store read one bounded page per request, not the
    # workspace: three bounded reads, each `LIMIT 4` for a `limit` of 3 --
    # `limit + 1`, because the cursor is proven by reading one row further. A
    # read that took the whole workspace and sliced in Python binds `LIMIT -1`
    # or no LIMIT, and this list is empty either way.
    assert page_reads() == [4, 4, 4]

    # And the unpaged request keeps asking by workspace, which is the same
    # answer at the cost it always had: scoping it would bind every id of a
    # list that is not bounded into an `IN` clause to learn nothing new.
    asked.clear()
    store.machine_thread_ids = recording_machine_thread_ids  # type: ignore[method-assign]
    try:
        whole = api.handle(
            method="GET",
            target=f"/api/v1/threads?workspace_id={workspace_id}",
            headers=_headers(),
        )
    finally:
        store.machine_thread_ids = machine_thread_ids  # type: ignore[method-assign]
    assert whole.status == 200, whole.payload
    assert asked == [None]
    assert {
        str(item["id"]): bool(item["engine_owned"]) for item in whole.payload["items"]
    } == seen


def test_the_thread_list_refuses_a_limit_or_a_cursor_it_cannot_honour(
    tmp_path: Path,
) -> None:
    """⟦batchK-8⟧ The bound is real and the cursor is resolved, not trusted.

    A `limit` is only a bound if it cannot be raised past one, and a cursor is
    only opaque if handing back somebody else's is refused rather than
    answered with a silently empty page -- which would look, to a cockpit,
    exactly like the end of the list.
    """

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)
    workspace = _consumer_workspace(store)
    workspace_id = str(workspace["id"])
    threads = _mixed_threads(store, workspace, 3)
    other = _thread_with_message(api)

    def get(target: str):
        return api.handle(method="GET", target=target, headers=_headers())

    base = f"/api/v1/threads?workspace_id={workspace_id}"
    assert get(f"{base}&limit=0").status == 400
    assert get(f"{base}&limit=501").status == 400
    assert get(f"{base}&limit=-1").status == 400
    assert get(f"{base}&limit=many").status == 400
    assert get(f"{base}&limit=500").status == 200
    assert get(f"{base}&after_id={threads[0]}").status == 200

    # A cursor from another workspace is refused, not answered empty.
    foreign = get(f"{base}&after_id={other['id']}")
    assert foreign.status == 400, foreign.payload
    missing = get(f"{base}&after_id=thread_deadbeef")
    assert missing.status == 404, missing.payload


def test_a_cursor_without_a_limit_is_still_bounded_to_one_page(
    tmp_path: Path,
) -> None:
    """⟦batchO ADJ-3⟧ A cursor implies a page, so it implies the page's bound.

    `after_id` without `limit` was the one request shape with no ceiling: the
    store read every remaining row (`LIMIT -1`) and the route then bound all
    of them into one `IN (...)` list for the ownership query -- slower than
    the unpaged read it stood in for, and past sqlite's host-parameter
    ceiling not an answer at all but a 503 naming the store as unavailable
    while the store is fine.

    So the route implies `THREAD_PAGE_LIMIT` when a cursor arrives without
    one. The bound has to be real -- a workspace larger than one page, walked
    by cursor alone -- and the walk still has to reproduce the list exactly,
    because a bound that dropped or repeated a row would be a worse answer
    than the slow one it replaces.
    """

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)
    workspace = _consumer_workspace(store)
    workspace_id = str(workspace["id"])
    created = _mixed_threads(store, workspace, THREAD_PAGE_LIMIT + 2)
    base = f"/api/v1/threads?workspace_id={workspace_id}"

    def get(target: str):
        response = api.handle(method="GET", target=target, headers=_headers())
        assert response.status == 200, response.payload
        return [str(item["id"]) for item in response.payload["items"]], response.payload[
            "next_cursor"
        ]

    # The default -- no cursor and no limit -- is untouched: still the whole
    # list under a null cursor, which is what the cockpit asks for, and it is
    # the order every assertion below is measured against. The clock is
    # frozen, so that order is the `id` tie-break rather than creation order.
    whole, whole_cursor = get(base)
    assert whole_cursor is None
    assert sorted(whole) == sorted(created)

    head, cursor = get(f"{base}&limit=1")
    assert head == whole[:1]
    assert cursor == whole[0]

    # The shape under test: a cursor, and no `limit` at all.
    page, next_cursor = get(f"{base}&after_id={cursor}")
    assert len(page) == THREAD_PAGE_LIMIT
    assert page == whole[1 : 1 + THREAD_PAGE_LIMIT]
    # More remains, so the request that used to hand back the whole remainder
    # under a null cursor now hands back a page under a working one.
    assert next_cursor == page[-1]

    tail, tail_cursor = get(f"{base}&after_id={next_cursor}")
    assert tail_cursor is None
    assert head + page + tail == whole
    assert len(set(head + page + tail)) == len(created)


_OWNERSHIP_SHAPES = ("carrier", "other_workflow", "operator", "foreign_carrier")


def _ownership_shape(store: ControlStore, shape: str, *, suffix: str) -> dict:
    """One run of each shape the two ownership rules have to tell apart.

    `carrier` -- the engine's own: a thread and a run the capture consumer
    created, carrying the capture workflow. `other_workflow` -- an operator's
    run carrying SOME OTHER workflow, which is the only shape the two rules
    answer differently. `operator` -- an operator's plain run. And
    `foreign_carrier` -- an operator's workflow-less run on a thread the
    consumer created, the `ForeignCarrierRun` recovery, which is the shape
    that proves ownership is asked of the RUN and never of its thread.
    """

    from cortex_platform.product.control.store import CAPTURE_CONSUMER_ACTOR
    from cortex_platform.product.engine.capture_consumer import (
        RESEARCH_CAPTURE_WORKFLOW,
    )
    from cortex_platform.product.workflows import StageDefinition, WorkflowDefinition

    machine_thread = shape in {"carrier", "foreign_carrier"}
    machine_run = shape == "carrier"
    workspace = store.create_workspace(
        title="Capture consumer" if machine_thread else "Research",
        actor_id=CAPTURE_CONSUMER_ACTOR if machine_thread else "operator:reed",
        idempotency_key=f"batchk9-workspace-{suffix}",
    ).value
    thread = store.create_thread(
        workspace_id=str(workspace["id"]),
        title=f"capture cap_{suffix}" if machine_thread else f"thread {suffix}",
        expected_revision=int(workspace["revision"]),
        actor_id=CAPTURE_CONSUMER_ACTOR if machine_thread else "operator:reed",
        idempotency_key=f"batchk9-thread-{suffix}",
    ).value
    run = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id=CAPTURE_CONSUMER_ACTOR if machine_run else "operator:reed",
        idempotency_key=f"batchk9-run-{suffix}",
    ).value
    if shape == "carrier":
        store.install_workflow(
            run_id=str(run["id"]), definition=RESEARCH_CAPTURE_WORKFLOW
        )
    elif shape == "other_workflow":
        store.install_workflow(
            run_id=str(run["id"]),
            definition=WorkflowDefinition(
                "research.batchk9-other",
                1,
                (StageDefinition("prepare", "control", ()),),
            ),
        )
    return dict(store.get_run(str(run["id"])))


@pytest.mark.parametrize("shape", _OWNERSHIP_SHAPES)
def test_one_source_decides_who_owns_a_run_at_every_door(
    tmp_path: Path, shape: str
) -> None:
    """⟦batchK-9 / A-5⟧ The two ownership rules are one source, and pinned apart.

    `ControlStore._engine_owned_run` -- the guard EVERY writer passes,
    including the Telegram adapter's `_apply_run_action` -- was a second
    hand-written copy of what `ControlAPI._machine_run_refusal` asks, and a
    strict subset of it: the receipt half read "any receipt by the consumer"
    where the route's read "the FIRST by (created_at, actor_id)", and the
    workflow half read the capture definition where the route's read any
    workflow. Two spellings of one rule, free to drift, and drifted.

    Both are now generated by `_machine_run_predicate` from one string, and
    the API asks its own question through `run_is_machine` instead of
    reproducing the pair. This enumerates every shape the rules must tell
    apart and pins, per shape, that the four answers agree: the wire's
    `engine_owned`, `run_is_machine`, the route's 409, and the store guard --
    with the ONE documented disagreement asserted rather than left latent.

    That disagreement is `other_workflow`, and it is deliberate. The route
    refuses it because a terminal run fences whichever workflow it carries;
    the store does not, because the store's exemption is one named actor and
    widening it would leave a run carrying some other workflow endable by the
    capture consumer alone -- by nobody who has any business ending it. It is
    asserted here so a future author changes it on purpose.
    """

    from cortex_platform.product.control import MachineRunRefused

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)

    engine_owned = shape == "carrier"
    route_refuses = shape in {"carrier", "other_workflow"}

    # 1. The wire, and the store method the projection is built from.
    wired = _ownership_shape(store, shape, suffix=f"{shape[:6]}-wire")
    projected = api.handle(
        method="GET", target=f"/api/v1/runs/{wired['id']}", headers=_headers()
    )
    assert projected.status == 200, projected.payload
    assert projected.payload["engine_owned"] is route_refuses
    assert store.run_is_machine(str(wired["id"])) is route_refuses
    assert store.machine_run_ids(str(wired["thread_id"])) == (
        {str(wired["id"])} if route_refuses else set()
    )

    # 2. The route, asked for real.
    routed = _ownership_shape(store, shape, suffix=f"{shape[:6]}-route")
    canceled = _post(
        api,
        f"/api/v1/runs/{routed['id']}/cancel",
        {"expected_revision": int(routed["revision"])},
        key=f"batchk9-cancel-{shape[:6]}-r",
    )
    if route_refuses:
        assert canceled.status == 409, canceled.payload
        assert canceled.payload["category"] == "machine_run"
        assert store.get_run(str(routed["id"]))["state"] == "queued"
    else:
        assert canceled.status == 200, canceled.payload

    # 3. The store guard, which every other door passes through.
    guarded = _ownership_shape(store, shape, suffix=f"{shape[:6]}-store")
    with store._connect() as conn:  # noqa: SLF001 - the guard under test
        row = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (str(guarded["id"]),)
        ).fetchone()
        assert store._engine_owned_run(conn, dict(row)) is engine_owned  # noqa: SLF001
    if engine_owned:
        with pytest.raises(MachineRunRefused):
            store.transition_run(
                run_id=str(guarded["id"]),
                target_state="cancel_requested",
                expected_revision=int(guarded["revision"]),
                actor_id="operator:reed",
                idempotency_key=f"batchk9-guard-{shape[:6]}-s",
            )
        assert store.get_run(str(guarded["id"]))["state"] == "queued"
    else:
        assert (
            store.transition_run(
                run_id=str(guarded["id"]),
                target_state="cancel_requested",
                expected_revision=int(guarded["revision"]),
                actor_id="operator:reed",
                idempotency_key=f"batchk9-guard-{shape[:6]}-s",
            ).value["state"]
            == "cancel_requested"
        )

    # 4. And the two rules agree everywhere except the one clause that is
    # supposed to differ, which is exactly `other_workflow`.
    assert (engine_owned == route_refuses) is (shape != "other_workflow")


def test_the_store_guard_reads_the_creating_receipt_not_any_receipt(
    tmp_path: Path,
) -> None:
    """⟦batchK-9⟧ The receipt half is now `run_creator`'s, at both doors.

    The old `_engine_owned_run` matched ANY receipt naming the consumer;
    `_MACHINE_RUN_PREDICATE` matches the FIRST by `(created_at, actor_id)`,
    which is what `run_creator` returns and what the route refuses on. On a
    run an operator created, a later consumer receipt therefore no longer
    turns the operator's own run into the engine's behind the operator's
    back -- and, more to the point, the store and the route now say the same
    thing about it instead of opposite things.

    Built by writing the receipt rows directly, because no production path
    files two `create_run` receipts for one run; the rule is still worth
    pinning, since it is the one the two doors had spelled differently.
    """

    from cortex_platform.product.control.store import CAPTURE_CONSUMER_ACTOR

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)
    run = _ownership_shape(store, "operator", suffix="receipt-order")
    run_id = str(run["id"])
    assert store.run_is_machine(run_id) is False

    with store._connect() as conn:  # noqa: SLF001 - the receipt rows under test
        conn.execute(
            """INSERT INTO idempotency_receipts
               (actor_id, operation, idempotency_key, request_hash,
                status_code, response_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                CAPTURE_CONSUMER_ACTOR,
                f"POST:/api/v1/threads/{run['thread_id']}/runs",
                "batchk9-late-receipt-01",
                "hash-batchk9-late",
                201,
                json.dumps({"id": run_id}),
                "2999-01-01T00:00:00.000000Z",
            ),
        )
        conn.commit()

    assert store.run_is_machine(run_id) is False
    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert store._engine_owned_run(conn, dict(row)) is False  # noqa: SLF001
    projected = api.handle(
        method="GET", target=f"/api/v1/runs/{run_id}", headers=_headers()
    )
    assert projected.payload["engine_owned"] is False


def test_a_corrupt_receipt_row_never_takes_an_ownership_read_down(
    tmp_path: Path,
) -> None:
    """⟦batchO, carried⟧ A row that is not JSON contributes nothing, never a 503.

    Every ownership predicate asks which resource a receipt created by reading
    `json_extract(response_json, '$.id')`, and `json_extract` RAISES on a
    `response_json` that is not JSON. The store's own writer cannot produce
    such a row -- but a corrupted or hand-edited database can hold one, and
    one of them used to take down every read that consults it:
    `GET /runs/{id}`, `GET /threads/{id}/runs` and `GET /threads` all answered
    503 `control_store_unavailable`, a category naming the store as
    unavailable while the rest of it answered perfectly.

    The row is written past the store's writer, which is the only way to make
    it. Two are needed to reach all three routes, because they read different
    receipt operations: the run predicates read `create_run`, and
    `_MACHINE_THREAD_PREDICATE` reads `create_thread`.

    What is asserted is not only that the routes answer, but that they answer
    the SAME thing: the corrupt row must be invisible, not merely survivable.
    """

    store = _store(tmp_path)
    api, _bridge = _driving_api(store)
    from cortex_platform.product.control.store import CAPTURE_CONSUMER_ACTOR

    runs = {
        shape: _ownership_shape(store, shape, suffix=f"{shape[:6]}-corrupt")
        for shape in _OWNERSHIP_SHAPES
    }

    def answers() -> dict[str, object]:
        value: dict[str, object] = {}
        for shape, run in runs.items():
            thread_id = str(run["thread_id"])
            workspace_id = str(store.get_thread(thread_id)["workspace_id"])
            for name, target in (
                ("run", f"/api/v1/runs/{run['id']}"),
                ("thread_runs", f"/api/v1/threads/{thread_id}/runs"),
                ("threads", f"/api/v1/threads?workspace_id={workspace_id}"),
            ):
                response = api.handle(
                    method="GET", target=target, headers=_headers()
                )
                assert response.status == 200, (shape, name, response.payload)
                payload = response.payload
                value[f"{shape}.{name}"] = (
                    [bool(item["engine_owned"]) for item in payload["items"]]
                    if "items" in payload
                    else bool(payload["engine_owned"])
                )
        return value

    before = answers()
    # Not vacuous: the shapes disagree, so an answer that silently collapsed
    # to one value would be caught by the comparison below.
    assert len(set(map(str, before.values()))) > 1

    operator_run = runs["operator"]
    operator_thread = str(operator_run["thread_id"])
    operator_workspace = str(store.get_thread(operator_thread)["workspace_id"])
    with sqlite3.connect(store.path) as conn:
        for suffix, operation in (
            ("run", f"POST:/api/v1/threads/{operator_thread}/runs"),
            (
                "thr",
                f"POST:/api/v1/workspaces/{operator_workspace}/threads",
            ),
        ):
            conn.execute(
                """INSERT INTO idempotency_receipts
                   (actor_id, operation, idempotency_key, request_hash,
                    status_code, response_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    CAPTURE_CONSUMER_ACTOR,
                    operation,
                    f"batcho-corrupt-receipt-{suffix}",
                    "0" * 64,
                    201,
                    "{not json",
                    # Earlier than every real receipt, so it would win the
                    # `(created_at, actor_id)` tie-break if it were visible.
                    "2020-01-01T00:00:00.000000Z",
                ),
            )
        conn.commit()

    assert answers() == before

    # And the store methods the routes are built from answer too, rather than
    # raising past the route into a 503.
    assert store.run_is_machine(str(operator_run["id"])) is False
    assert store.thread_is_machine(operator_thread) is False
    assert store.workspace_is_machine(operator_workspace) is False
    assert store.run_creator(str(operator_run["id"])) == "operator:reed"
    assert store.thread_creator(operator_thread) == "operator:reed"
    assert store.workspace_creator(operator_workspace) == "operator:reed"
