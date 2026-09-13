"""Control API surface for the capture inbox.

Every route here stages or decides raw operator input, so the tests pin the
edge validation and the public projection as hard as the happy paths.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore

TOKEN = "c" * 48


def _api(tmp_path: Path) -> ControlAPI:
    store = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
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


def _create(
    api: ControlAPI,
    *,
    payload: str = "https://example.com/post",
    note: str = "read later",
    key: str = "api-capture-create-01",
):
    return api.handle(
        method="POST",
        target="/api/v1/captures",
        headers=_headers(key=key),
        body=json.dumps({"payload": payload, "note": note}).encode(),
    )


_LEASE_FIELDS = ("claim_owner", "claim_epoch", "claim_expires_at")


def test_capture_create_returns_the_public_row(tmp_path: Path) -> None:
    api = _api(tmp_path)
    created = _create(api)

    assert created.status == 201
    assert created.payload["state"] == "pending"
    assert created.payload["payload"] == "https://example.com/post"
    assert created.payload["note"] == "read later"
    assert created.payload["kind"] == "url"
    assert created.payload["revision"] == 0
    assert created.payload["failure_category"] is None
    # The whole public DTO, pinned: the lease columns are absent by name and
    # the only operator text is what the operator submitted.
    assert set(created.payload) == {
        "id",
        "capture_key",
        "payload",
        "kind",
        "note",
        "state",
        "known_source_id",
        "consumed_source_ids",
        "failure_category",
        "blocked_by",
        "revision",
        "created_at",
        "updated_at",
    }
    assert set(created.payload).isdisjoint(_LEASE_FIELDS)

    replay = _create(api)
    assert replay.status == 201
    assert dict(replay.headers)["Idempotency-Replayed"] == "true"
    assert replay.payload == created.payload

    fetched = api.handle(
        method="GET",
        target=f"/api/v1/captures/{created.payload['id']}",
        headers=_headers(),
    )
    assert fetched.status == 200
    assert fetched.payload == created.payload

    listed = api.handle(
        method="GET", target="/api/v1/captures", headers=_headers()
    )
    assert listed.status == 200
    assert listed.payload == {"items": [created.payload], "next_cursor": None}


def test_capture_create_takes_exactly_payload_and_note(tmp_path: Path) -> None:
    api = _api(tmp_path)

    for index, body in enumerate(
        (
            {"payload": "https://example.com", "note": "", "extra": True},
            {"payload": "https://example.com"},
            {"note": ""},
            {},
        )
    ):
        response = api.handle(
            method="POST",
            target="/api/v1/captures",
            headers=_headers(key=f"api-capture-body-{index:02d}"),
            body=json.dumps(body).encode(),
        )
        assert response.status == 400, body
        assert response.payload["category"] == "invalid_request"

    queried = api.handle(
        method="POST",
        target="/api/v1/captures?state=pending",
        headers=_headers(key="api-capture-query-01"),
        body=json.dumps({"payload": "https://example.com", "note": ""}).encode(),
    )
    assert queried.status == 400

    keyless = api.handle(
        method="POST",
        target="/api/v1/captures",
        headers=_headers(),
        body=json.dumps({"payload": "https://example.com", "note": ""}).encode(),
    )
    assert keyless.status == 400
    assert api.store.list_captures() == []


def test_capture_list_pages_with_limit_and_cursor(tmp_path: Path) -> None:
    """The inbox response is bounded and advertises where the next page starts."""

    api = _api(tmp_path)
    for index in range(3):
        _create(
            api,
            payload=f"https://example.com/{index}",
            key=f"api-capture-page-{index:02d}",
        )
    # The API clock is frozen, so all three rows share `created_at` and the id
    # is what makes the page boundary stable.
    ordered = [item["id"] for item in api.store.list_captures()]

    first = api.handle(
        method="GET", target="/api/v1/captures?limit=2", headers=_headers()
    )
    assert first.status == 200
    assert [item["id"] for item in first.payload["items"]] == ordered[:2]
    assert first.payload["next_cursor"] == ordered[1]

    second = api.handle(
        method="GET",
        target=f"/api/v1/captures?limit=2&cursor={first.payload['next_cursor']}",
        headers=_headers(),
    )
    assert [item["id"] for item in second.payload["items"]] == ordered[2:]
    assert second.payload["next_cursor"] is None

    for target in (
        "/api/v1/captures?limit=0",
        "/api/v1/captures?limit=1001",
        "/api/v1/captures?limit=abc",
        "/api/v1/captures?limit=2&limit=3",
        "/api/v1/captures?cursor=",
        "/api/v1/captures?cursor=capture-does-not-exist",
    ):
        response = api.handle(method="GET", target=target, headers=_headers())
        assert response.status == 400, target
        assert response.payload["category"] == "invalid_request"


def test_capture_list_ends_on_an_exactly_full_last_page(tmp_path: Path) -> None:
    """⟦batchO⟧ `next_cursor` is proven, not inferred from a full page.

    The route used to answer `items[-1]["id"] if len(items) == limit else
    None`, which is right except on the one case a cursor exists to settle: a
    last page that is exactly full. There it handed back a cursor whose page
    comes back empty, and an empty page is indistinguishable from the end of
    the list. The store now reads one row further and reports what it found.
    """

    api = _api(tmp_path)
    for index in range(4):
        _create(
            api,
            payload=f"https://example.com/full-{index}",
            key=f"api-capture-full-{index:02d}",
        )
    # The API clock is frozen, so all four rows share `created_at` and the id
    # tie-break is what orders them.
    ordered = [str(item["id"]) for item in api.store.list_captures()]
    assert len(ordered) == 4

    def get(target: str):
        response = api.handle(method="GET", target=target, headers=_headers())
        assert response.status == 200, response.payload
        return [str(item["id"]) for item in response.payload["items"]], response.payload[
            "next_cursor"
        ]

    # Exactly full AND last: the list ends here.
    exact, exact_cursor = get("/api/v1/captures?limit=4")
    assert exact == ordered
    assert exact_cursor is None

    # Exactly full with more behind it: a working cursor that yields the
    # remainder with no duplicate and no omission.
    first, first_cursor = get("/api/v1/captures?limit=2")
    assert first == ordered[:2]
    assert first_cursor == ordered[1]
    second, second_cursor = get(f"/api/v1/captures?limit=2&cursor={first_cursor}")
    assert second == ordered[2:]
    assert second_cursor is None
    assert first + second == ordered
    assert len(set(first + second)) == 4

    # The state filter takes the same bound: three pending rows read three at
    # a time is an exactly-full last page.
    approved = api.handle(
        method="POST",
        target=f"/api/v1/captures/{ordered[0]}/approve",
        headers=_headers(key="api-capture-full-approve-1"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    assert approved.status == 200, approved.payload
    pending, pending_cursor = get("/api/v1/captures?state=pending&limit=3")
    assert pending == ordered[1:]
    assert pending_cursor is None


def test_capture_list_query_is_fail_closed(tmp_path: Path) -> None:
    """Unlike the sources route, an unknown parameter is refused, not ignored."""

    api = _api(tmp_path)
    created = _create(api).payload
    other = _create(
        api, payload="https://example.com/b", key="api-capture-create-02"
    ).payload
    approved = api.handle(
        method="POST",
        target=f"/api/v1/captures/{other['id']}/approve",
        headers=_headers(key="api-capture-approve-01"),
        body=json.dumps({"expected_revision": other["revision"]}).encode(),
    ).payload

    filtered = api.handle(
        method="GET", target="/api/v1/captures?state=pending", headers=_headers()
    )
    assert filtered.status == 200
    assert [item["id"] for item in filtered.payload["items"]] == [created["id"]]

    approved_only = api.handle(
        method="GET", target="/api/v1/captures?state=approved", headers=_headers()
    )
    assert [item["id"] for item in approved_only.payload["items"]] == [
        approved["id"]
    ]

    for target in (
        "/api/v1/captures?unexpected=1",
        "/api/v1/captures?state=pending&unexpected=1",
        "/api/v1/captures?state=staged",
        "/api/v1/captures?state=",
        "/api/v1/captures?state=pending&state=approved",
    ):
        rejected = api.handle(method="GET", target=target, headers=_headers())
        assert rejected.status == 400, target
        assert rejected.payload["category"] == "invalid_request"

    detail = api.handle(
        method="GET",
        target=f"/api/v1/captures/{created['id']}?state=pending",
        headers=_headers(),
    )
    assert detail.status == 400


def test_capture_approve_and_dismiss_are_exact_body_cas(tmp_path: Path) -> None:
    api = _api(tmp_path)
    created = _create(api).payload

    for index, body in enumerate(
        (
            {"expected_revision": 0, "extra": True},
            {},
            {"expected_revision": "0"},
        )
    ):
        rejected = api.handle(
            method="POST",
            target=f"/api/v1/captures/{created['id']}/approve",
            headers=_headers(key=f"api-capture-badcas-{index:02d}"),
            body=json.dumps(body).encode(),
        )
        assert rejected.status == 400, body
    assert api.store.get_capture(created["id"])["revision"] == 0

    approved = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-01"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    assert approved.status == 200
    assert approved.payload["state"] == "approved"
    assert approved.payload["revision"] == 1

    stale = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/dismiss",
        headers=_headers(key="api-capture-dismiss-01"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    assert stale.status == 409
    assert stale.payload["category"] == "revision_conflict"
    assert stale.payload["current"]["state"] == "approved"
    assert set(stale.payload["current"]).isdisjoint(_LEASE_FIELDS)

    dismissed = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/dismiss",
        headers=_headers(key="api-capture-dismiss-02"),
        body=json.dumps({"expected_revision": 1}).encode(),
    )
    assert dismissed.status == 200
    assert dismissed.payload["state"] == "dismissed"

    again = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-02"),
        body=json.dumps({"expected_revision": 2}).encode(),
    )
    assert again.status == 409
    assert again.payload["category"] == "invalid_transition"


def test_open_duplicate_is_409_already_captured_with_the_current_row(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    created = _create(api).payload

    duplicate = _create(
        api, payload="HTTPS://Example.com/post", key="api-capture-create-02"
    )

    assert duplicate.status == 409
    assert duplicate.content_type == "application/problem+json"
    assert duplicate.payload["category"] == "already_captured"
    assert duplicate.payload["type"] == "urn:cortex:problem:already_captured"
    assert duplicate.payload["retryable"] is False
    assert duplicate.payload["current"] == created
    assert set(duplicate.payload["current"]).isdisjoint(_LEASE_FIELDS)


def test_reopen_requires_a_literal_acknowledged_true(tmp_path: Path) -> None:
    api = _api(tmp_path)
    created = _create(api).payload
    approved = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-01"),
        body=json.dumps({"expected_revision": 0}).encode(),
    ).payload
    claimed = api.store.claim_capture(
        capture_id=created["id"],
        worker_id="p4-worker",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key="api-capture-claim-0001",
    ).value
    uncertain = api.store.complete_capture(
        capture_id=created["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        actor_id="p4",
        idempotency_key="api-capture-unsure-001",
    ).value
    assert approved["state"] == "approved"

    for index, body in enumerate(
        (
            {"expected_revision": uncertain["revision"]},
            {"expected_revision": uncertain["revision"], "acknowledged": False},
            {"expected_revision": uncertain["revision"], "acknowledged": "true"},
            {"expected_revision": uncertain["revision"], "acknowledged": 1},
            {
                "expected_revision": uncertain["revision"],
                "acknowledged": True,
                "extra": True,
            },
        )
    ):
        rejected = api.handle(
            method="POST",
            target=f"/api/v1/captures/{created['id']}/reopen",
            headers=_headers(key=f"api-capture-reopen-{index:02d}"),
            body=json.dumps(body).encode(),
        )
        assert rejected.status == 400, body
    assert api.store.get_capture(created["id"])["state"] == "uncertain"

    reopened = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/reopen",
        headers=_headers(key="api-capture-reopen-ok1"),
        body=json.dumps(
            {"expected_revision": uncertain["revision"], "acknowledged": True}
        ).encode(),
    )
    assert reopened.status == 200
    assert reopened.payload["state"] == "approved"
    assert reopened.payload["failure_category"] is None
    assert set(reopened.payload).isdisjoint(_LEASE_FIELDS)


def test_claimed_capture_never_exposes_its_lease(tmp_path: Path) -> None:
    api = _api(tmp_path)
    created = _create(api).payload
    api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-01"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    api.store.claim_capture(
        capture_id=created["id"],
        worker_id="p4-worker-secret",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key="api-capture-claim-0001",
    )

    fetched = api.handle(
        method="GET",
        target=f"/api/v1/captures/{created['id']}",
        headers=_headers(),
    )

    assert fetched.payload["state"] == "claimed"
    rendered = json.dumps(fetched.payload)
    for private in (*_LEASE_FIELDS, "p4-worker-secret", str(tmp_path)):
        assert private not in rendered


def test_failure_text_never_reaches_the_public_dto(tmp_path: Path) -> None:
    """Only an allowlisted category is persistable, so nothing else can leak."""

    api = _api(tmp_path)
    created = _create(api).payload
    api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-01"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    claimed = api.store.claim_capture(
        capture_id=created["id"],
        worker_id="p4-worker",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key="api-capture-claim-0001",
    ).value
    raw = "OSError: cannot read /Users/operator/private/paper.pdf (token sk-abcdefgh)"

    with pytest.raises(ValueError):
        api.store.complete_capture(
            capture_id=created["id"],
            claim_owner="p4-worker",
            claim_epoch=claimed["claim_epoch"],
            outcome="failed",
            failure_category=raw,
            actor_id="p4",
            idempotency_key="api-capture-fail-0001",
        )
    api.store.complete_capture(
        capture_id=created["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="failed",
        failure_category="materialization_failed",
        actor_id="p4",
        idempotency_key="api-capture-fail-0002",
    )

    fetched = api.handle(
        method="GET",
        target=f"/api/v1/captures/{created['id']}",
        headers=_headers(),
    )

    assert fetched.payload["failure_category"] == "materialization_failed"
    rendered = json.dumps(fetched.payload)
    assert raw not in rendered
    for fragment in ("/Users/operator", "sk-abcdefgh", "OSError"):
        assert fragment not in rendered
    # failure_category is the only failure-shaped field the DTO carries.
    assert [
        name for name in fetched.payload if "fail" in name or "error" in name
    ] == ["failure_category"]


def test_unknown_capture_is_a_sanitized_404(tmp_path: Path) -> None:
    api = _api(tmp_path)

    missing = api.handle(
        method="GET", target="/api/v1/captures/capture-missing", headers=_headers()
    )

    assert missing.status == 404
    assert missing.content_type == "application/problem+json"
    assert missing.payload["category"] == "not_found"
    assert missing.payload["owner"] == "cortexd"
    assert str(tmp_path) not in json.dumps(missing.payload)


def test_a_blocked_capture_names_the_run_that_fences_it(tmp_path: Path) -> None:
    """⟦V-R3 / P9-3⟧ The blocking id reaches the DTO, and only while it is true.

    V-R3 put the id of the run still holding a capture's carrier thread on the
    capture's audit row, because the `captures` table has no detail column and
    no migration is made. That was the right place to RECORD it and the wrong
    place to leave it: the cockpit could tell an operator a capture was
    blocked and never which run to go and end. It is projected onto the DTO
    now -- computed on read, never stored -- on every route that returns a
    capture.

    The second half matters as much as the first. `blocked_by` is tied to the
    capture's CURRENT `failure_category`, so a reopened capture stops naming
    the run that blocked the attempt before it, and an `outcome_unknown`
    capture -- which names nothing -- reports nothing.
    """

    api = _api(tmp_path)
    created = _create(api).payload
    assert created["blocked_by"] is None

    api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-blocked"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    claimed = api.store.claim_capture(
        capture_id=created["id"],
        worker_id="p4-worker",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key="api-capture-claim-blocked",
    ).value
    api.store.complete_capture(
        capture_id=created["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        failure_category="carrier_thread_busy",
        detail={"blocked_by": "run_fencing_the_carrier"},
        actor_id="p4",
        idempotency_key="api-capture-uncertain-blocked",
    )

    fetched = api.handle(
        method="GET",
        target=f"/api/v1/captures/{created['id']}",
        headers=_headers(),
    ).payload
    assert fetched["state"] == "uncertain"
    assert fetched["failure_category"] == "carrier_thread_busy"
    assert fetched["blocked_by"] == "run_fencing_the_carrier"

    listed = api.handle(
        method="GET", target="/api/v1/captures", headers=_headers()
    ).payload["items"]
    assert [item["blocked_by"] for item in listed] == ["run_fencing_the_carrier"]
    # Exactly one key added, and the lease is still private.
    assert set(listed[0]) == set(fetched)
    assert not [name for name in fetched if name in _LEASE_FIELDS]

    # Reopening clears the category, so the id stops being true and stops
    # being reported -- the audit row it came from is still there.
    reopened = api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/reopen",
        headers=_headers(key="api-capture-reopen-blocked"),
        body=json.dumps(
            {"expected_revision": fetched["revision"], "acknowledged": True}
        ).encode(),
    ).payload
    assert reopened["state"] == "approved"
    assert reopened["failure_category"] is None
    assert reopened["blocked_by"] is None
    assert (
        api.store.capture_blocked_by([created["id"]])
        == {created["id"]: "run_fencing_the_carrier"}
    )


def _set_aside(
    api: ControlAPI,
    *,
    payload: str,
    key: str,
    category: str,
    blocked_by: str,
) -> dict:
    """One capture closed `uncertain` under a blocking category, as the consumer does."""

    created = _create(api, payload=payload, key=f"{key}-create").payload
    api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key=f"{key}-approve"),
        body=json.dumps({"expected_revision": created["revision"]}).encode(),
    )
    claimed = api.store.claim_capture(
        capture_id=created["id"],
        worker_id="p4-worker",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key=f"{key}-claim",
    ).value
    api.store.complete_capture(
        capture_id=created["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        failure_category=category,
        detail={"blocked_by": blocked_by},
        actor_id="p4",
        idempotency_key=f"{key}-uncertain",
    )
    return created


def test_a_dismissed_capture_stops_naming_a_blocker_and_keeps_its_category(
    tmp_path: Path,
) -> None:
    """⟦BRK-7⟧ A closed capture names no blocker; its category survives anyway.

    `reopen_capture` nulls `failure_category`, so a reopened capture already
    stopped reporting the run that fenced the attempt before it.
    `dismiss_capture` deliberately does NOT -- the category is the only in-row
    record of why the capture was set aside, and nulling it would erase the
    chip on the same card -- so a dismissed capture went on telling the
    operator to go and end a run for a capture they had just closed.

    The fix is on the read, not the write: the projection looks a blocker up
    only for a capture that is not in a terminal state. So the chip stays and
    the instruction goes, which is the asymmetry the two transitions were
    disagreeing about.
    """

    api = _api(tmp_path)
    blocked = _set_aside(
        api,
        payload="https://example.com/blocked",
        key="api-capture-brk7-blocked",
        category="carrier_thread_busy",
        blocked_by="run_fencing_the_carrier",
    )
    # A second one that stays uncertain, so the assertions below cannot pass
    # by the projection simply reporting nothing for anybody.
    waiting = _set_aside(
        api,
        payload="https://example.com/waiting",
        key="api-capture-brk7-waiting",
        category="carrier_thread_foreign",
        blocked_by="thread_holding_the_title",
    )

    def fetch(capture_id: str) -> dict:
        response = api.handle(
            method="GET",
            target=f"/api/v1/captures/{capture_id}",
            headers=_headers(),
        )
        assert response.status == 200, response.payload
        return response.payload

    before = fetch(blocked["id"])
    assert before["state"] == "uncertain"
    assert before["failure_category"] == "carrier_thread_busy"
    assert before["blocked_by"] == "run_fencing_the_carrier"

    dismissed = api.handle(
        method="POST",
        target=f"/api/v1/captures/{blocked['id']}/dismiss",
        headers=_headers(key="api-capture-brk7-dismiss"),
        body=json.dumps({"expected_revision": before["revision"]}).encode(),
    )
    assert dismissed.status == 200, dismissed.payload

    # The command route's own body, the single GET and the list all agree.
    listed = {
        str(item["id"]): item
        for item in api.handle(
            method="GET", target="/api/v1/captures", headers=_headers()
        ).payload["items"]
    }
    for projected in (dismissed.payload, fetch(blocked["id"]), listed[blocked["id"]]):
        assert projected["state"] == "dismissed"
        # The chip survives: this is the only in-row record of WHY.
        assert projected["failure_category"] == "carrier_thread_busy"
        # The instruction does not: nobody is waiting on that run any more.
        assert projected["blocked_by"] is None

    # And the capture still open still names its blocker, on the same page.
    assert listed[waiting["id"]]["state"] == "uncertain"
    assert listed[waiting["id"]]["failure_category"] == "carrier_thread_foreign"
    assert listed[waiting["id"]]["blocked_by"] == "thread_holding_the_title"

    # Nothing was erased: the audit row the id is computed from is untouched,
    # so the record of what fenced the capture survives the dismissal.
    assert api.store.capture_blocked_by([blocked["id"]]) == {
        blocked["id"]: "run_fencing_the_carrier"
    }


def test_an_uncertain_capture_that_names_nothing_reports_nothing(
    tmp_path: Path,
) -> None:
    """`outcome_unknown` is the category that points at no other row."""

    api = _api(tmp_path)
    created = _create(api).payload
    api.handle(
        method="POST",
        target=f"/api/v1/captures/{created['id']}/approve",
        headers=_headers(key="api-capture-approve-unknown"),
        body=json.dumps({"expected_revision": 0}).encode(),
    )
    claimed = api.store.claim_capture(
        capture_id=created["id"],
        worker_id="p4-worker",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key="api-capture-claim-unknown",
    ).value
    api.store.complete_capture(
        capture_id=created["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        actor_id="p4",
        idempotency_key="api-capture-uncertain-unknown",
    )

    fetched = api.handle(
        method="GET",
        target=f"/api/v1/captures/{created['id']}",
        headers=_headers(),
    ).payload
    assert fetched["failure_category"] == "outcome_unknown"
    assert fetched["blocked_by"] is None
