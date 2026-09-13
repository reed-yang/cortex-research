from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import CAPTURE_CONSUMER_ACTOR, ControlStore
from cortex_platform.product.engine.capture_consumer import RESEARCH_CAPTURE_WORKFLOW

TOKEN = "control-token-for-tests-000000000000"


def _key(name: str) -> str:
    """The store demands 16-128 URL-safe characters; keep the readable name."""

    return f"{name}-000000000000000"[:128]


def _api(tmp_path: Path) -> ControlAPI:
    store = ControlStore(tmp_path / "control.db", clock=lambda: datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
    store.initialize()
    return ControlAPI(store, access_token=TOKEN, allowed_origins=frozenset({"https://cortex.test"}))


def _headers(key: str | None = None) -> dict[str, str]:
    value = {"X-Cortex-Control-Token": TOKEN}
    if key:
        value["Idempotency-Key"] = key
    return value


def _post(api: ControlAPI, path: str, payload: dict, *, key: str):
    return api.handle(method="POST", target=path, headers=_headers(_key(key)), body=json.dumps(payload).encode())


def _engine_owned(api: ControlAPI) -> tuple[dict, dict]:
    """A workspace and a thread the capture consumer created.

    Both ownership predicates key on an `idempotency_receipts` row whose
    actor is the consumer, so writing the rows through the store under that
    actor is what makes them the engine's -- the same setup the turn-driver
    tests use.
    """

    store = api.store
    workspace = store.create_workspace(
        title="Capture consumer",
        actor_id=CAPTURE_CONSUMER_ACTOR,
        idempotency_key=_key("engine-workspace"),
    ).value
    thread = store.create_thread(
        workspace_id=str(workspace["id"]),
        title="capture cap_1",
        expected_revision=int(workspace["revision"]),
        actor_id=CAPTURE_CONSUMER_ACTOR,
        idempotency_key=_key("engine-thread"),
    ).value
    return workspace, thread


def _seed(api: ControlAPI) -> tuple[dict, dict]:
    workspace = _post(api, "/api/v1/workspaces", {"title": "Echo"}, key="ws-1").payload
    thread = _post(api, f"/api/v1/workspaces/{workspace['id']}/threads", {"title": "Q1", "expected_revision": 0}, key="t-1").payload
    return workspace, thread


def test_rename_routes_return_projections_with_archived_at(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace, thread = _seed(api)
    renamed = _post(api, f"/api/v1/workspaces/{workspace['id']}/rename", {"title": "Echo v2", "expected_revision": 1}, key="wr-1")
    assert renamed.status == 200 and renamed.payload["title"] == "Echo v2" and renamed.payload["engine_owned"] is False
    thread_renamed = _post(api, f"/api/v1/threads/{thread['id']}/rename", {"title": "Q1b", "expected_revision": 0}, key="tr-1")
    assert thread_renamed.status == 200 and thread_renamed.payload["title"] == "Q1b" and thread_renamed.payload["archived_at"] is None
    stale = _post(api, f"/api/v1/threads/{thread['id']}/rename", {"title": "Q1c", "expected_revision": 0}, key="tr-2")
    assert stale.status == 409 and stale.payload["category"] == "revision_conflict" and stale.payload["current"]["revision"] == 1


def test_archive_filters_the_list_unless_asked(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace, thread = _seed(api)
    archived = _post(api, f"/api/v1/threads/{thread['id']}/archive", {"expected_revision": 0}, key="a-1")
    assert archived.status == 200 and archived.payload["archived_at"] is not None
    default = api.handle(method="GET", target=f"/api/v1/threads?workspace_id={workspace['id']}", headers=_headers())
    assert default.payload["items"] == []
    included = api.handle(method="GET", target=f"/api/v1/threads?workspace_id={workspace['id']}&include_archived=true", headers=_headers())
    assert [item["id"] for item in included.payload["items"]] == [thread["id"]]
    single = api.handle(method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers())
    assert single.status == 200 and single.payload["archived_at"] is not None
    bad = api.handle(method="GET", target=f"/api/v1/threads?workspace_id={workspace['id']}&include_archived=maybe", headers=_headers())
    assert bad.status == 400


def test_archive_with_active_run_is_a_typed_409(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace, thread = _seed(api)
    _post(api, f"/api/v1/threads/{thread['id']}/messages", {"role": "user", "content": "hello", "expected_revision": 0}, key="m-1")
    run = _post(api, f"/api/v1/threads/{thread['id']}/runs", {"expected_revision": 1}, key="r-1")
    assert run.status == 201
    current = api.handle(method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()).payload
    refused = _post(api, f"/api/v1/threads/{thread['id']}/archive", {"expected_revision": current["revision"]}, key="a-1")
    assert refused.status == 409 and refused.payload["category"] == "thread_active_run"
    assert refused.payload["current"]["id"] == thread["id"]


def test_missing_idempotency_key_and_unknown_thread(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace, thread = _seed(api)
    missing = api.handle(method="POST", target=f"/api/v1/threads/{thread['id']}/archive", headers=_headers(), body=b"{\"expected_revision\": 0}")
    assert missing.status == 400
    unknown = _post(api, "/api/v1/threads/thread_nope/archive", {"expected_revision": 0}, key="a-x")
    assert unknown.status == 404
    unknown_workspace = _post(
        api, "/api/v1/workspaces/ws_nope/rename", {"title": "Nowhere", "expected_revision": 0}, key="w-x"
    )
    assert unknown_workspace.status == 404


def test_engine_owned_rows_refuse_every_new_action(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace, thread = _engine_owned(api)
    refused_workspace = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/rename",
        {"title": "Mine now", "expected_revision": int(workspace["revision"])},
        key="mw-1",
    )
    assert refused_workspace.status == 409
    assert refused_workspace.payload["category"] == "machine_workspace"

    bodies = {
        "rename": {"title": "Mine now", "expected_revision": int(thread["revision"])},
        "archive": {"expected_revision": int(thread["revision"])},
        "unarchive": {"expected_revision": int(thread["revision"])},
    }
    for action, body in bodies.items():
        refused = _post(api, f"/api/v1/threads/{thread['id']}/{action}", body, key=f"mt-{action}")
        assert refused.status == 409, action
        assert refused.payload["category"] == "machine_thread", action

    # Refused before anything durable: neither row moved.
    assert api.store.get_workspace(str(workspace["id"]))["title"] == "Capture consumer"
    current = api.store.get_thread(str(thread["id"]))
    assert current["title"] == "capture cap_1"
    assert current["archived_at"] is None
    assert current["revision"] == int(thread["revision"])


def test_a_committed_rename_is_replayed_even_after_the_thread_becomes_the_engines(
    tmp_path: Path,
) -> None:
    """The receipt is consulted before ownership, as `_turn_refusal` does.

    A refusal must never be given about a command that already committed and
    whose response was lost: the client retrying with the same key gets the
    same body back, not a 409 about a rule that became true afterwards.
    """

    api = _api(tmp_path)
    _workspace, thread = _seed(api)
    body = {"title": "Q1b", "expected_revision": 0}
    first = _post(api, f"/api/v1/threads/{thread['id']}/rename", body, key="tr-1")
    assert first.status == 200 and first.headers == ()

    run = api.store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=1,
        actor_id="local-operator",
        idempotency_key=_key("engine-run"),
    ).value
    api.store.install_workflow(run_id=str(run["id"]), definition=RESEARCH_CAPTURE_WORKFLOW)
    assert api.store.thread_is_machine(str(thread["id"])) is True

    replay = _post(api, f"/api/v1/threads/{thread['id']}/rename", body, key="tr-1")
    assert replay.status == 200
    assert ("Idempotency-Replayed", "true") in replay.headers
    # The receipt's own body, projected the way the route projects: only
    # `engine_owned` differs, because it is recomputed on every read and the
    # thread genuinely changed hands between the two calls.
    assert {k: v for k, v in replay.payload.items() if k != "engine_owned"} == {
        k: v for k, v in first.payload.items() if k != "engine_owned"
    }
    assert replay.payload["engine_owned"] is True

    # A NEW key on the same thread is the refusal the ownership rule owes.
    fresh = _post(api, f"/api/v1/threads/{thread['id']}/rename", {"title": "Q1c", "expected_revision": 1}, key="tr-9")
    assert fresh.status == 409 and fresh.payload["category"] == "machine_thread"


def test_a_replayed_rename_returns_the_same_receipt_body(tmp_path: Path) -> None:
    api = _api(tmp_path)
    _workspace, thread = _seed(api)
    body = {"title": "Q1b", "expected_revision": 0}
    first = _post(api, f"/api/v1/threads/{thread['id']}/rename", body, key="tr-1")
    assert first.status == 200 and first.headers == ()
    replay = _post(api, f"/api/v1/threads/{thread['id']}/rename", body, key="tr-1")
    assert replay.status == 200
    assert replay.payload == first.payload
    assert ("Idempotency-Replayed", "true") in replay.headers
    # One command, one revision: the replay wrote nothing.
    assert api.store.get_thread(str(thread["id"]))["revision"] == 1


def test_an_archived_thread_refuses_a_message_and_a_run_with_a_typed_409(tmp_path: Path) -> None:
    """The refusal Telegram and a direct POST meet, seen through the API.

    The receipt still comes first: a message that committed before the
    archive is replayed under its own key, never answered with a refusal
    about a rule that became true afterwards.
    """

    api = _api(tmp_path)
    _workspace, thread = _seed(api)
    saved = _post(api, f"/api/v1/threads/{thread['id']}/messages", {"role": "user", "content": "hello", "expected_revision": 0}, key="m-0")
    assert saved.status == 201
    archived = _post(api, f"/api/v1/threads/{thread['id']}/archive", {"expected_revision": 1}, key="a-1")
    assert archived.status == 200
    revision = int(archived.payload["revision"])

    refused = _post(api, f"/api/v1/threads/{thread['id']}/messages", {"role": "user", "content": "still there?", "expected_revision": revision}, key="m-1")
    assert refused.status == 409 and refused.payload["category"] == "thread_archived"
    assert refused.payload["current"]["id"] == thread["id"]
    refused_run = _post(api, f"/api/v1/threads/{thread['id']}/runs", {"expected_revision": revision}, key="r-1")
    assert refused_run.status == 409 and refused_run.payload["category"] == "thread_archived"

    # Refused before anything durable: one message, no run, no revision bump.
    messages = api.handle(method="GET", target=f"/api/v1/threads/{thread['id']}/messages", headers=_headers()).payload
    assert [item["content"] for item in messages["items"]] == ["hello"]
    current = api.handle(method="GET", target=f"/api/v1/threads/{thread['id']}", headers=_headers()).payload
    assert current["active_run_id"] is None and current["revision"] == revision

    replay = _post(api, f"/api/v1/threads/{thread['id']}/messages", {"role": "user", "content": "hello", "expected_revision": 0}, key="m-0")
    assert replay.status == 201 and replay.payload == saved.payload
    assert ("Idempotency-Replayed", "true") in replay.headers

    restored = _post(api, f"/api/v1/threads/{thread['id']}/unarchive", {"expected_revision": revision}, key="u-1")
    assert restored.status == 200
    started = _post(api, f"/api/v1/threads/{thread['id']}/runs", {"expected_revision": int(restored.payload["revision"])}, key="r-2")
    assert started.status == 201
