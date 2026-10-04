"""Control API surface for save-only idea fragments."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore

TOKEN = "c" * 48
FRAGMENT_KEYS = {
    "id",
    "text",
    "note",
    "origin",
    "thread_id",
    "context_item_id",
    "created_at",
}


class _Clock:
    def __init__(self) -> None:
        self.seconds = 0

    def __call__(self) -> datetime:
        self.seconds += 1
        return datetime(2026, 9, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=self.seconds)


def _api(tmp_path: Path) -> ControlAPI:
    store = ControlStore(tmp_path / "control.db", clock=_Clock())
    store.initialize()
    return ControlAPI(
        store,
        access_token=TOKEN,
        allowed_origins=frozenset({"https://cortex.test"}),
    )


def _headers(*, key: str | None = None) -> dict[str, str]:
    headers = {"X-Cortex-Control-Token": TOKEN}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _post(api: ControlAPI, body: object, *, key: str | None = "api-fragment-create-01"):
    return api.handle(
        method="POST",
        target="/api/v1/fragments",
        headers=_headers(key=key),
        body=json.dumps(body).encode(),
    )


def _get(api: ControlAPI, target: str):
    return api.handle(method="GET", target=target, headers=_headers())


def test_create_returns_the_exact_public_fragment(tmp_path: Path) -> None:
    api = _api(tmp_path)
    text = "  多行想法\n第二行 🎬  "

    created = _post(api, {"text": text, "note": "later"})

    assert created.status == 201
    assert set(created.payload) == FRAGMENT_KEYS
    assert created.payload["text"] == text
    assert created.payload["note"] == "later"
    assert created.payload["origin"] == "web"
    assert created.payload["thread_id"] is None
    assert created.payload["context_item_id"] is None
    assert "Idempotency-Replayed" not in dict(created.headers)


def test_a_retry_replays_the_same_body_with_the_replay_header(tmp_path: Path) -> None:
    api = _api(tmp_path)
    created = _post(api, {"text": "an idea", "note": ""})

    replay = _post(api, {"text": "an idea", "note": ""})

    assert replay.status == 201
    assert dict(replay.headers)["Idempotency-Replayed"] == "true"
    assert replay.payload == created.payload
    assert len(_get(api, "/api/v1/fragments").payload["items"]) == 1


def test_a_reused_key_with_different_text_is_a_conflict(tmp_path: Path) -> None:
    api = _api(tmp_path)
    _post(api, {"text": "an idea", "note": ""})

    conflict = _post(api, {"text": "another idea", "note": ""})

    assert conflict.status == 409
    assert conflict.payload["category"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "body",
    [
        {"text": "an idea"},
        {"note": ""},
        {"text": "an idea", "note": "", "origin": "telegram"},
        {"text": "an idea", "note": "", "thread_id": "thread-1"},
        {"text": 7, "note": ""},
        {"text": "an idea", "note": None},
        {"text": "", "note": ""},
        {"text": "t" * 16_385, "note": ""},
        {"text": "an idea", "note": "n" * 2_001},
    ],
)
def test_an_invalid_body_is_refused_and_writes_nothing(
    tmp_path: Path, body: object
) -> None:
    api = _api(tmp_path)

    refused = _post(api, body)

    assert refused.status == 400
    assert refused.payload["category"] == "invalid_request"
    assert _get(api, "/api/v1/fragments").payload["items"] == []


def test_a_missing_idempotency_key_is_refused(tmp_path: Path) -> None:
    api = _api(tmp_path)

    refused = _post(api, {"text": "an idea", "note": ""}, key=None)

    assert refused.status == 400
    assert _get(api, "/api/v1/fragments").payload["items"] == []


def test_list_is_newest_first_and_get_returns_one(tmp_path: Path) -> None:
    api = _api(tmp_path)
    first = _post(api, {"text": "first", "note": ""}, key="api-fragment-first-01")
    second = _post(api, {"text": "second", "note": ""}, key="api-fragment-second-1")

    listed = _get(api, "/api/v1/fragments")

    assert listed.status == 200
    assert listed.payload == {
        "items": [second.payload, first.payload],
        "next_cursor": None,
    }
    fetched = _get(api, f"/api/v1/fragments/{first.payload['id']}")
    assert fetched.status == 200 and fetched.payload == first.payload


def test_list_pages_with_limit_and_cursor(tmp_path: Path) -> None:
    api = _api(tmp_path)
    ids = [
        _post(api, {"text": f"idea {index}", "note": ""}, key=f"api-fragment-page-{index:02d}").payload["id"]
        for index in range(3)
    ]

    first = _get(api, "/api/v1/fragments?limit=2")
    second = _get(api, f"/api/v1/fragments?limit=2&cursor={first.payload['next_cursor']}")

    assert [item["id"] for item in first.payload["items"]] == [ids[2], ids[1]]
    assert [item["id"] for item in second.payload["items"]] == [ids[0]]
    assert second.payload["next_cursor"] is None


@pytest.mark.parametrize(
    "target",
    [
        "/api/v1/fragments?state=open",
        "/api/v1/fragments?limit=0",
        "/api/v1/fragments?cursor=fragment_missing",
    ],
)
def test_list_refuses_an_invalid_query(tmp_path: Path, target: str) -> None:
    refused = _get(_api(tmp_path), target)

    assert refused.status == 400


def test_get_refuses_a_query_and_a_missing_id_is_not_found(tmp_path: Path) -> None:
    api = _api(tmp_path)
    created = _post(api, {"text": "an idea", "note": ""})

    assert _get(api, f"/api/v1/fragments/{created.payload['id']}?x=1").status == 400
    missing = _get(api, "/api/v1/fragments/fragment_missing")
    assert missing.status == 404


def test_a_telegram_fragment_is_served_without_its_transport_data(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    store = api.store
    workspace = store.create_workspace(
        title="Ideas", actor_id="local", idempotency_key="api-fragment-ws-0001"
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Bound",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key="api-fragment-thread-1",
    ).value
    saved = store.create_fragment(
        text="from the bot",
        note="",
        origin="telegram",
        thread_id=thread["id"],
        origin_ref="tg-synthetic-update-ref",
        actor_id="telegram-actor-synthetic",
        idempotency_key="tg-synthetic-update-ref",
    ).value

    fetched = _get(api, f"/api/v1/fragments/{saved['id']}")

    assert set(fetched.payload) == FRAGMENT_KEYS
    assert fetched.payload["origin"] == "telegram"
    assert fetched.payload["thread_id"] == thread["id"]
    body = json.dumps(_get(api, "/api/v1/fragments").payload)
    assert "tg-synthetic-update-ref" not in body
    assert "telegram-actor-synthetic" not in body


def test_saving_through_the_api_starts_nothing(tmp_path: Path) -> None:
    api = _api(tmp_path)

    _post(api, {"text": "an idea", "note": ""})

    with sqlite3.connect(api.store.path) as conn:
        for table in ("messages", "runs", "captures"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
