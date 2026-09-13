from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.api.app import _ACCESS_IDENTITY_MAXIMUM
from cortex_platform.product.control import ControlStore

TOKEN = "x" * 48


def _api(tmp_path: Path) -> ControlAPI:
    store = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    return ControlAPI(
        store,
        access_token=TOKEN,
        allowed_origins=frozenset({"https://cortex.test"}),
    )


def _headers(*, key: str | None = None, origin: str | None = None) -> dict[str, str]:
    value = {"X-Cortex-Control-Token": TOKEN}
    if key:
        value["Idempotency-Key"] = key
    if origin:
        value["Origin"] = origin
    return value


def _post(
    api: ControlAPI,
    path: str,
    payload: dict,
    *,
    key: str,
    headers: dict[str, str] | None = None,
):
    request_headers = _headers(key=key) if headers is None else headers
    return api.handle(
        method="POST",
        target=path,
        headers=request_headers,
        body=json.dumps(payload).encode(),
    )


def test_health_is_public_but_all_state_requires_loopback_token(tmp_path: Path) -> None:
    api = _api(tmp_path)
    health = api.handle(method="GET", target="/api/v1/health", headers={})
    assert health.status == 200
    assert health.payload["capabilities"]["control_store"] is True

    missing = api.handle(method="GET", target="/api/v1/workspaces", headers={})
    assert missing.status == 403
    assert missing.payload["category"] == "authentication_required"

    remote = api.handle(
        method="GET",
        target="/api/v1/workspaces",
        headers=_headers(),
        client_host="192.0.2.4",
    )
    assert remote.status == 403
    assert remote.payload["category"] == "loopback_required"


def test_origin_is_exact_and_no_wildcard_is_accepted(tmp_path: Path) -> None:
    api = _api(tmp_path)
    accepted = api.handle(
        method="GET",
        target="/api/v1/workspaces",
        headers=_headers(origin="https://cortex.test"),
    )
    assert accepted.status == 200

    rejected = api.handle(
        method="GET",
        target="/api/v1/workspaces",
        headers=_headers(origin="https://evil.test"),
    )
    assert rejected.status == 403
    assert rejected.payload["category"] == "origin_rejected"


def test_workspace_thread_and_idempotency_replay(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    )
    assert workspace.status == 201
    replay = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    )
    assert replay.status == 201
    assert replay.headers == (("Idempotency-Replayed", "true"),)
    assert replay.payload == workspace.payload

    conflict = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Different"},
        key="workspace-command-0001",
    )
    assert conflict.status == 409
    assert conflict.payload["category"] == "idempotency_conflict"

    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace.payload['id']}/threads",
        {"title": "Echo", "expected_revision": 0},
        key="thread-command-000001",
    )
    assert thread.status == 201
    listed = api.handle(
        method="GET",
        target=f"/api/v1/threads?workspace_id={workspace.payload['id']}",
        headers=_headers(),
    )
    assert listed.payload["items"] == [thread.payload]


def test_revision_problem_is_sanitized_and_returns_current_resource(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    ).payload
    _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "First", "expected_revision": 0},
        key="thread-command-000001",
    )
    stale = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Second", "expected_revision": 0},
        key="thread-command-000002",
    )
    assert stale.status == 409
    assert stale.payload["category"] == "revision_conflict"
    assert stale.payload["current"]["revision"] == 1
    rendered = json.dumps(stale.payload)
    assert "sqlite" not in rendered.lower()
    assert str(tmp_path) not in rendered


def test_invalid_body_and_missing_key_fail_without_mutation(tmp_path: Path) -> None:
    api = _api(tmp_path)
    missing_key = api.handle(
        method="POST",
        target="/api/v1/workspaces",
        headers=_headers(),
        body=b'{"title":"Research"}',
    )
    assert missing_key.status == 400
    malformed = api.handle(
        method="POST",
        target="/api/v1/workspaces",
        headers=_headers(key="workspace-command-0001"),
        body=b"not-json",
    )
    assert malformed.status == 400
    listed = api.handle(
        method="GET", target="/api/v1/workspaces", headers=_headers()
    )
    assert listed.payload["items"] == []


def test_event_cursor_is_opaque_signed_and_replayable(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    ).payload
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Echo", "expected_revision": 0},
        key="thread-command-000001",
    ).payload
    # ⟦V-2⟧ A run is refused on a thread with nothing to answer, so the
    # thread gets the message a cockpit would have sent before the run.
    _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "Start here", "expected_revision": 0},
        key="message-command-00001",
    )
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": 1},
        key="run-command-0000001",
    )
    assert run.status == 201
    assert "dispatch_owner" not in run.payload["attempt"]
    assert "dispatch_expires_at" not in run.payload["attempt"]
    assert "state_generation_id" not in run.payload["attempt"]
    assert "runtime_identity_version" not in run.payload["attempt"]
    assert "runtime_slot_id" not in run.payload["attempt"]
    assert "runtime_artifact_digest" not in run.payload["attempt"]
    assert "runtime_worker_protocol" not in run.payload["attempt"]

    first = api.handle(
        method="GET", target="/api/v1/events", headers=_headers()
    )
    cursor = first.payload["next_cursor"]
    assert isinstance(cursor, str)
    assert cursor != "1"
    assert isinstance(first.payload["items"][0]["cursor"], str)

    replay = api.handle(
        method="GET",
        target=f"/api/v1/events?after_cursor={cursor}",
        headers=_headers(),
    )
    assert replay.status == 200
    assert replay.payload["items"] == []

    tampered = api.handle(
        method="GET",
        target=f"/api/v1/events?after_cursor=A{cursor[1:]}",
        headers=_headers(),
    )
    assert tampered.status == 400
    assert tampered.payload["category"] == "invalid_request"

    restarted_store = ControlStore(tmp_path / "control.db")
    restarted_store.initialize()
    restarted_api = ControlAPI(
        restarted_store,
        access_token="y" * 48,
        allowed_origins=frozenset({"https://cortex.test"}),
    )
    after_restart = restarted_api.handle(
        method="GET",
        target=f"/api/v1/events?after_cursor={cursor}",
        headers={"X-Cortex-Control-Token": "y" * 48},
    )
    assert after_restart.status == 200
    assert after_restart.payload["items"] == []


def test_event_stream_auth_replay_and_wire_format(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    ).payload
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Echo", "expected_revision": 0},
        key="thread-command-000001",
    ).payload
    # ⟦V-2⟧ A run is refused on a thread with nothing to answer, so the
    # thread gets the message a cockpit would have sent before the run.
    _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "Start here", "expected_revision": 0},
        key="message-command-00001",
    )
    _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": 1},
        key="run-command-0000001",
    )

    start = api.prepare_event_stream(
        target="/api/v1/events/stream",
        headers=_headers(),
        client_host="127.0.0.1",
    )
    assert not hasattr(start, "status")
    events, cursor = api.event_stream_batch(after_cursor=start.after_cursor)
    assert cursor > 0
    wire = b"".join(api.format_sse_event(event) for event in events)
    assert b"id: " in wire
    assert b"event: run.queued" in wire
    assert b"data: {" in wire
    assert TOKEN.encode() not in wire
    assert str(tmp_path).encode() not in wire

    last_id = events[-1]["cursor"]
    resumed = api.prepare_event_stream(
        target="/api/v1/events/stream",
        headers={**_headers(), "Last-Event-ID": last_id},
        client_host="127.0.0.1",
    )
    assert resumed.after_cursor == cursor
    assert api.event_stream_batch(after_cursor=resumed.after_cursor)[0] == []

    ambiguous = api.prepare_event_stream(
        target=f"/api/v1/events/stream?after_cursor={last_id}",
        headers={**_headers(), "Last-Event-ID": last_id},
        client_host="127.0.0.1",
    )
    assert ambiguous.status == 400
    unauthorized = api.prepare_event_stream(
        target="/api/v1/events/stream",
        headers={},
        client_host="127.0.0.1",
    )
    assert unauthorized.status == 403


def test_public_event_projection_is_closed_and_redacts_unsafe_decision_text(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    ).payload
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Echo", "expected_revision": 0},
        key="thread-command-000001",
    ).payload
    # ⟦V-2⟧ A run is refused on a thread with nothing to answer, so the
    # thread gets the message a cockpit would have sent before the run.
    _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "Start here", "expected_revision": 0},
        key="message-command-00001",
    )
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": 1},
        key="run-command-0000001",
    ).payload
    attempt_id = run["active_attempt_id"]
    baseline_cursor = api.store.list_events()[-1]["cursor"]
    with api.store._transaction() as conn:
        api.store._insert_event(
            conn,
            run_id=run["id"],
            attempt_id=attempt_id,
            event_type="decision.required",
            payload={
                "decision_id": "decision_public_1",
                "kind": "source_conflict",
                "prompt": (
                    "Read /Users/operator/.config/cortex and use "
                    "token=super-secret <thinking>private plan</thinking>"
                ),
                "options": [
                    {
                        "id": "keep_both",
                        "label": "Keep both",
                        "raw": "provider payload",
                        "credential": "super-secret",
                        "path": "/Users/operator/private",
                        "reasoning": "private plan",
                    },
                    {"id": "sk-supersecretvalue", "label": "Leaking option"},
                ],
                "runtime_decision_ref": "runtime-private",
            },
        )
        api.store._insert_event(
            conn,
            run_id=run["id"],
            attempt_id=attempt_id,
            event_type="runtime.raw_payload",
            payload={
                "token": "super-secret",
                "path": "/Users/operator/private",
                "reasoning": "private plan",
            },
        )
        api.store._insert_event(
            conn,
            run_id=run["id"],
            attempt_id=attempt_id,
            event_type="run.completed",
            payload={
                "from": "running",
                "state": "completed",
                "raw": "provider payload",
                "token": "super-secret",
                "artifact_path": "/Users/operator/private/result.md",
            },
        )
        api.store._insert_event(
            conn,
            run_id=run["id"],
            attempt_id=attempt_id,
            event_type="checkpoint.committed",
            payload={"checkpoint_uri": "cortex://artifacts/runs/checkpoint.json"},
        )
        api.store._insert_event(
            conn,
            run_id=run["id"],
            attempt_id=attempt_id,
            event_type="checkpoint.committed",
            payload={
                "checkpoint_uri": (
                    "cortex://artifacts/../../etc/passwd?token=super-secret"
                )
            },
        )

    events, _ = api.event_stream_batch(after_cursor=baseline_cursor)
    decision, unknown, completed, checkpoint, unsafe_checkpoint = events
    assert decision["payload"] == {
        "decision_id": "decision_public_1",
        "kind": "source_conflict",
        "prompt": "Decision details are unavailable in this client.",
        "options": [{"id": "keep_both", "label": "Keep both"}],
    }
    assert unknown["type"] == "event.redacted"
    assert unknown["payload"] == {"category": "unsupported_event_type"}
    assert completed["payload"] == {"from": "running", "state": "completed"}
    assert checkpoint["payload"] == {
        "checkpoint_uri": "cortex://artifacts/runs/checkpoint.json"
    }
    assert unsafe_checkpoint["payload"] == {}
    rendered = json.dumps(events)
    for forbidden in (
        "runtime.raw_payload",
        "super-secret",
        "/Users/operator",
        "provider payload",
        "private plan",
        "runtime-private",
        "reasoning",
        "credential",
    ):
        assert forbidden not in rendered


def test_public_decision_list_uses_minimum_sanitized_dto(tmp_path: Path) -> None:
    api = _api(tmp_path)
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-command-0001",
    ).payload
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Echo", "expected_revision": 0},
        key="thread-command-000001",
    ).payload
    # ⟦V-2⟧ A run is refused on a thread with nothing to answer, so the
    # thread gets the message a cockpit would have sent before the run.
    _post(
        api,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "Start here", "expected_revision": 0},
        key="message-command-00001",
    )
    run = _post(
        api,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": 1},
        key="run-command-0000001",
    ).payload
    with api.store._transaction() as conn:
        conn.execute(
            """INSERT INTO decisions
               (id, run_id, attempt_id, kind, prompt, options_json, state,
                revision, created_at, runtime_decision_ref,
                runtime_decision_revision)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (
                "decision_public_1",
                run["id"],
                run["active_attempt_id"],
                "source_conflict",
                "Use credential=super-secret from /Users/operator/private",
                json.dumps(
                    [
                        {
                            "id": "keep_both",
                            "label": "Keep both",
                            "description": "Import both canonical sources.",
                            "raw": "provider payload",
                            "token": "super-secret",
                        }
                    ]
                ),
                "2026-07-23T12:00:00Z",
                "runtime-private",
                9,
            ),
        )

    response = api.handle(
        method="GET",
        target="/api/v1/decisions?state=pending",
        headers=_headers(),
    )
    assert response.status == 200
    decision = response.payload["items"][0]
    assert set(decision) == {
        "id",
        "run_id",
        "attempt_id",
        "kind",
        "prompt",
        "options",
        "state",
        "resolution",
        "revision",
        "created_at",
        "resolved_at",
    }
    assert decision["prompt"] == "Decision details are unavailable in this client."
    assert decision["options"] == [
        {
            "id": "keep_both",
            "label": "Keep both",
            "description": "Import both canonical sources.",
        }
    ]
    rendered = json.dumps(response.payload)
    for forbidden in (
        "super-secret",
        "/Users/operator",
        "provider payload",
        "runtime-private",
        "runtime_decision_ref",
        "runtime_decision_revision",
    ):
        assert forbidden not in rendered

    for index, invalid_body in enumerate(
        (
            {"choice": "keep_both", "expected_revision": 0, "extra": True},
            {"expected_revision": 0},
        )
    ):
        invalid = _post(
            api,
            "/api/v1/decisions/decision_public_1/resolve",
            invalid_body,
            key=f"decision-invalid-body-{index}",
        )
        assert invalid.status == 400
    assert api.store.get_decision("decision_public_1")["revision"] == 0

    with api.store._transaction() as conn:
        conn.execute(
            """UPDATE decisions
               SET state = 'resolved', revision = 1,
                   resolution_json = ?, resolved_at = ?
               WHERE id = ?""",
            (
                json.dumps(
                    {
                        "choice": "keep_both",
                        "actor_id": "credential=super-secret /Users/operator",
                    }
                ),
                "2026-07-23T12:01:00Z",
                "decision_public_1",
            ),
        )
    conflict = _post(
        api,
        "/api/v1/decisions/decision_public_1/resolve",
        {"choice": "keep_both", "expected_revision": 0},
        key="decision-command-0001",
    )
    assert conflict.status == 409
    assert conflict.payload["current"]["resolution"] == {"choice": "keep_both"}
    assert "actor_id" not in json.dumps(conflict.payload)
    assert "super-secret" not in json.dumps(conflict.payload)


def _receipt_actors(tmp_path: Path) -> list[tuple[str, str]]:
    """Every command receipt, as (operation, actor) in commit order."""

    with sqlite3.connect(tmp_path / "control.db") as connection:
        rows = connection.execute(
            "SELECT operation, actor_id FROM idempotency_receipts"
            " ORDER BY created_at, operation"
        ).fetchall()
    return [(str(operation), str(actor)) for operation, actor in rows]


def _write_a_thread_and_a_message(
    api: ControlAPI, *, headers: dict[str, str], suffix: str
) -> dict[str, str]:
    workspace = _post(
        api,
        "/api/v1/workspaces",
        {"title": f"Research {suffix}"},
        key=f"workspace-{suffix}",
        headers={**headers, "Idempotency-Key": f"workspace-{suffix}"},
    )
    assert workspace.status == 201
    thread = _post(
        api,
        f"/api/v1/workspaces/{workspace.payload['id']}/threads",
        {"title": f"Echo {suffix}", "expected_revision": 0},
        key=f"thread-command-{suffix}",
        headers={**headers, "Idempotency-Key": f"thread-command-{suffix}"},
    )
    assert thread.status == 201
    message = _post(
        api,
        f"/api/v1/threads/{thread.payload['id']}/messages",
        {
            "role": "user",
            "content": "Summarize the paper",
            "expected_revision": thread.payload["revision"],
        },
        key=f"message-command-{suffix}",
        headers={**headers, "Idempotency-Key": f"message-command-{suffix}"},
    )
    assert message.status == 201
    return {"workspace": workspace.payload["id"], "thread": thread.payload["id"]}


def test_a_loopback_turn_is_still_the_local_operator(tmp_path: Path) -> None:
    api = _api(tmp_path)
    written = _write_a_thread_and_a_message(
        api, headers=_headers(), suffix="loopback"
    )

    # No identity header is the operator sitting at the machine: the actor P8
    # always recorded, unchanged.
    assert {actor for _, actor in _receipt_actors(tmp_path)} == {"local-operator"}
    store = ControlStore(tmp_path / "control.db")
    assert store.thread_creator(written["thread"]) == "local-operator"


def test_a_public_door_turn_is_recorded_as_the_verified_access_identity(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    identity = "operator@cortex.test"
    written = _write_a_thread_and_a_message(
        api,
        headers={**_headers(), "X-Cortex-Access-Identity": identity},
        suffix="public",
    )

    # ⟦P8-08⟧ Every receipt the turn filed names the person, not the door.
    assert {actor for _, actor in _receipt_actors(tmp_path)} == {
        f"access:{identity}"
    }
    store = ControlStore(tmp_path / "control.db")
    assert store.thread_creator(written["thread"]) == f"access:{identity}"
    # The thread is still nobody's but the operator's -- an Access identity is
    # not the engine, so the P9 ownership rules read it exactly as before.
    assert store.thread_is_machine(written["thread"]) is False

    # The header is read case-insensitively, like every other header here.
    current = api.handle(
        method="GET",
        target=f"/api/v1/threads/{written['thread']}",
        headers=_headers(),
    ).payload
    lowercased = _post(
        api,
        f"/api/v1/threads/{written['thread']}/messages",
        {
            "role": "user",
            "content": "And the limitations",
            "expected_revision": current["revision"],
        },
        key="message-public-2",
        headers={
            **_headers(key="message-public-2"),
            "x-cortex-access-identity": identity,
        },
    )
    assert lowercased.status == 201
    assert (
        "POST:/api/v1/threads/%s/messages" % written["thread"],
        f"access:{identity}",
    ) in _receipt_actors(tmp_path)


def test_an_identity_the_adapter_would_not_have_produced_is_refused(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    # `access:` plus the identity has to fit the 200-character `actor_id`
    # bound, and the shape has to be the one the adapter verifies. Neither is
    # quietly downgraded to `local-operator`: a request that carries a
    # malformed identity is not recorded at all.
    for rejected in (
        "not-an-email",
        "a@b.test, c@d.test",
        "operator@cortex.test\nX-Injected: 1",
        "%s@cortex.test" % ("o" * 200),
    ):
        refused = _post(
            api,
            "/api/v1/workspaces",
            {"title": "Research"},
            key="workspace-refused",
            headers={
                **_headers(key="workspace-refused"),
                "X-Cortex-Access-Identity": rejected,
            },
        )
        assert refused.status == 400, rejected
        assert refused.payload["category"] == "invalid_request"

    assert _receipt_actors(tmp_path) == []
    assert (
        api.handle(
            method="GET", target="/api/v1/workspaces", headers=_headers()
        ).payload["items"]
        == []
    )


def test_the_access_identity_bound_is_the_one_the_web_doors_import() -> None:
    # ⟦ADJ-H-1⟧ The web's `ACCESS_IDENTITY_MAX_LENGTH` lives in one module,
    # `apps/web/server/access-identity-bound.mjs`, which both the node adapter
    # and `apps/web/app/api/cortex/access-security.ts` import; the same file
    # is asserted to be 193 in `apps/web/tests/access-identity.test.mjs`.
    # Three surfaces, one number: widening the 200-character `actor_id` column
    # here without widening the door -- or the reverse -- has to fail a test
    # rather than silently admit an identity that then fails every write with
    # 400.
    assert _ACCESS_IDENTITY_MAXIMUM == 193


def test_the_two_doors_do_not_replay_each_other(tmp_path: Path) -> None:
    api = _api(tmp_path)
    identity = "operator@cortex.test"
    local = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-shared-key",
    )
    public = _post(
        api,
        "/api/v1/workspaces",
        {"title": "Research"},
        key="workspace-shared-key",
        headers={
            **_headers(key="workspace-shared-key"),
            "X-Cortex-Access-Identity": identity,
        },
    )

    # Receipts are keyed by actor, so the same Idempotency-Key through the
    # other door is a new command rather than a replay of someone else's.
    assert local.status == 201
    assert public.status == 201
    assert public.headers == ()
    assert public.payload["id"] != local.payload["id"]
    assert sorted(actor for _, actor in _receipt_actors(tmp_path)) == [
        f"access:{identity}",
        "local-operator",
    ]
