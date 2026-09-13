from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore

from ..sources.fakes import ECHO_TITLE, LINGBOT_URL, GoldenResolver, make_run

TOKEN = "s" * 48


def _api(tmp_path: Path) -> tuple[ControlAPI, dict]:
    store = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    store.initialize()
    store.register_source(
        authority="arxiv",
        authority_id="2606.04527",
        source_kind="paper",
        official_title=ECHO_TITLE,
        engine_ref="paper:echo-existing",
        aliases=({"authority": "project", "value": "Echo-Infinity"},),
        actor_id="fixture",
        idempotency_key="register-echo-000001",
    )
    run = make_run(store)
    return (
        ControlAPI(
            store,
            access_token=TOKEN,
            allowed_origins=frozenset({"https://cortex.test"}),
            source_resolver=GoldenResolver(),
        ),
        run,
    )


def _headers(*, key: str | None = None) -> dict[str, str]:
    headers = {"X-Cortex-Control-Token": TOKEN}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_source_routes_expose_conflict_without_private_resolver_or_claim_fields(
    tmp_path: Path,
) -> None:
    api, run = _api(tmp_path)
    created = api.handle(
        method="POST",
        target=f"/api/v1/runs/{run['id']}/source-intents",
        headers=_headers(key="api-source-intent-001"),
        body=json.dumps(
            {
                "attempt_id": run["attempt"]["id"],
                "title": ECHO_TITLE,
                "locator": LINGBOT_URL,
            }
        ).encode(),
    )
    assert created.status == 201
    assert created.payload["state"] == "pending"
    rendered = json.dumps(created.payload)
    for private in (
        "claim_owner",
        "claim_epoch",
        "request_hash",
        "raw_payload",
        "resolver_secret",
        "engine_ref",
        str(tmp_path),
    ):
        assert private not in rendered

    fetched = api.handle(
        method="GET",
        target=f"/api/v1/source-intents/{created.payload['id']}",
        headers=_headers(),
    )
    assert fetched.status == 200
    assert fetched.payload == created.payload

    queried = api.handle(
        method="POST",
        target=(
            f"/api/v1/source-intents/{created.payload['id']}/resolve?unexpected=1"
        ),
        headers=_headers(key="api-source-query-rejected"),
        body=b'{"choice":"keep_both","expected_revision":0}',
    )
    assert queried.status == 400
    assert api.store.get_source_intent(created.payload["id"])["revision"] == 0

    for index, invalid_body in enumerate(
        (
            {"choice": "keep_both", "expected_revision": 0, "extra": True},
            {"choice": "keep_both"},
        )
    ):
        invalid = api.handle(
            method="POST",
            target=f"/api/v1/source-intents/{created.payload['id']}/resolve",
            headers=_headers(key=f"api-source-invalid-{index}"),
            body=json.dumps(invalid_body).encode(),
        )
        assert invalid.status == 400
    assert api.store.get_source_intent(created.payload["id"])["revision"] == 0

    resolved = api.handle(
        method="POST",
        target=f"/api/v1/source-intents/{created.payload['id']}/resolve",
        headers=_headers(key="api-source-resolve-01"),
        body=b'{"choice":"keep_both","expected_revision":0}',
    )
    assert resolved.status == 200
    assert resolved.payload["state"] == "resolved"
    assert set(resolved.payload) == {
        "id",
        "run_id",
        "attempt_id",
        "state",
        "revision",
        "created_at",
        "updated_at",
        "title_observation",
        "locator_observation",
        "candidates",
        "decision",
    }
    assert "engine_ref" not in json.dumps(resolved.payload)

    sources = api.handle(
        method="GET", target="/api/v1/sources", headers=_headers()
    )
    assert sources.status == 200
    assert [item["canonical_id"] for item in sources.payload["items"]] == [
        "arxiv:2606.04527",
        "arxiv:2607.07675",
    ]


def test_source_mutation_requires_injected_resolver_and_never_falls_back_to_network(
    tmp_path: Path,
) -> None:
    api, run = _api(tmp_path)
    api_without_resolver = ControlAPI(api.store, access_token="z" * 48)
    response = api_without_resolver.handle(
        method="POST",
        target=f"/api/v1/runs/{run['id']}/source-intents",
        headers={
            "X-Cortex-Control-Token": "z" * 48,
            "Idempotency-Key": "api-source-disabled-1",
        },
        body=json.dumps(
            {
                "attempt_id": run["attempt"]["id"],
                "title": ECHO_TITLE,
                "locator": LINGBOT_URL,
            }
        ).encode(),
    )
    assert response.status == 409
    assert response.payload["category"] == "source_resolution_disabled"


def test_source_intent_api_replay_does_not_call_resolver_again(tmp_path: Path) -> None:
    class CountingResolver(GoldenResolver):
        def __init__(self) -> None:
            self.calls = 0

        def resolve(
            self,
            *,
            title: str | None,
            locator: str | None,
            locator_sha256: str | None = None,
        ):
            self.calls += 1
            return super().resolve(
                title=title,
                locator=locator,
                locator_sha256=locator_sha256,
            )

    api, run = _api(tmp_path)
    resolver = CountingResolver()
    api._source_resolver = resolver
    payload = json.dumps(
        {
            "attempt_id": run["attempt"]["id"],
            "title": ECHO_TITLE,
            "locator": LINGBOT_URL,
        }
    ).encode()
    first = api.handle(
        method="POST",
        target=f"/api/v1/runs/{run['id']}/source-intents",
        headers=_headers(key="api-source-replay-001"),
        body=payload,
    )
    replay = api.handle(
        method="POST",
        target=f"/api/v1/runs/{run['id']}/source-intents",
        headers=_headers(key="api-source-replay-001"),
        body=payload,
    )

    assert first.status == replay.status == 201
    assert replay.payload == first.payload
    assert replay.headers == (("Idempotency-Replayed", "true"),)
    assert resolver.calls == 1


def test_public_source_projection_omits_unsafe_legacy_aliases(tmp_path: Path) -> None:
    api, _ = _api(tmp_path)
    source = api.store.list_sources()[0]
    with api.store._transaction() as conn:
        conn.execute(
            """INSERT INTO source_aliases
               (id, source_id, authority, normalized_value,
                display_value, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "legacy-unsafe-alias",
                source["id"],
                "project",
                "/private/operator/paper.pdf",
                "/private/operator/paper.pdf",
                "2026-07-23T12:00:00Z",
            ),
        )

    response = api.handle(
        method="GET", target="/api/v1/sources", headers=_headers()
    )
    rendered = json.dumps(response.payload)
    assert response.status == 200
    assert "/private/operator" not in rendered
    assert "legacy-unsafe-alias" not in rendered


def test_source_gate_projection_never_returns_raw_local_locator(
    tmp_path: Path,
) -> None:
    api, run = _api(tmp_path)
    created = api.handle(
        method="POST",
        target=f"/api/v1/runs/{run['id']}/source-intents",
        headers=_headers(key="api-source-private-gate"),
        body=json.dumps(
            {
                "attempt_id": run["attempt"]["id"],
                "title": ECHO_TITLE,
                "locator": LINGBOT_URL,
            }
        ).encode(),
    )
    with api.store._transaction() as conn:
        conn.execute(
            """UPDATE source_intents
               SET title_claim = ?, locator_claim = ? WHERE id = ?""",
            (
                "token=private-value /Users/operator/paper.pdf",
                "file:///Users/operator/paper.pdf",
                created.payload["id"],
            ),
        )
        conn.execute(
            """UPDATE source_candidates
               SET official_title = ?, locator = ?, evidence_json = ?
               WHERE intent_id = ?""",
            (
                "/Users/operator/private-title",
                "file:///Users/operator/paper.pdf",
                json.dumps({"token": "private-value"}),
                created.payload["id"],
            ),
        )

    response = api.handle(
        method="GET",
        target=f"/api/v1/source-intents/{created.payload['id']}",
        headers=_headers(),
    )
    rendered = json.dumps(response.payload)
    assert response.status == 200
    assert response.payload["title_observation"] is None
    assert response.payload["locator_observation"] is None
    assert response.payload["candidates"][0]["official_title"] == "Untitled source"
    assert "/Users/operator" not in rendered
    assert "private-value" not in rendered
    assert "file://" not in rendered


def test_source_events_have_closed_observable_sse_payloads(tmp_path: Path) -> None:
    api, run = _api(tmp_path)
    baseline = api.store.list_events()[-1]["cursor"]
    created = api.handle(
        method="POST",
        target=f"/api/v1/runs/{run['id']}/source-intents",
        headers=_headers(key="api-source-events-create-1"),
        body=json.dumps(
            {
                "attempt_id": run["attempt"]["id"],
                "title": ECHO_TITLE,
                "locator": LINGBOT_URL,
            }
        ).encode(),
    )
    api.handle(
        method="POST",
        target=f"/api/v1/source-intents/{created.payload['id']}/resolve",
        headers=_headers(key="api-source-events-resolve-1"),
        body=b'{"choice":"keep_both","expected_revision":0}',
    )

    events, _ = api.event_stream_batch(after_cursor=baseline)
    source_events = [event for event in events if event["type"].startswith("source.")]
    assert {event["type"] for event in source_events} == {
        "source.intent_received",
        "source.conflict_detected",
        "source.reused",
        "source.import_requested",
    }
    received = next(
        event for event in source_events if event["type"] == "source.intent_received"
    )
    assert received["payload"] == {
        "source_intent_id": created.payload["id"],
        "canonical_ids": ["arxiv:2606.04527", "arxiv:2607.07675"],
    }
    requested = next(
        event for event in source_events if event["type"] == "source.import_requested"
    )
    assert requested["payload"] == {
        "source_id": requested["payload"]["source_id"],
        "canonical_id": "arxiv:2607.07675",
    }
    wire = b"".join(api.format_sse_event(event) for event in source_events)
    assert b"event: source.import_requested" in wire
    rendered = wire.decode()
    for forbidden in (
        "engine_ref",
        "import_action_id",
        "import_waiter_id",
        "claim_owner",
        "/private/",
    ):
        assert forbidden not in rendered
