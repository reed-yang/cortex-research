from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cortex_platform.product.api import ControlAPI

from ..artifacts.test_control import (
    CONTENT_V1,
    _complete,
    _context,
    _request_version,
)

TOKEN = "a" * 48


def _get(api: ControlAPI, target: str):
    return api.handle(
        method="GET",
        target=target,
        headers={"X-Cortex-Control-Token": TOKEN},
    )


def test_artifact_metadata_routes_are_closed_paginated_and_redacted(
    tmp_path: Path,
) -> None:
    store, run, source, artifact = _context(tmp_path)
    committed = _complete(
        store, _request_version(store, run, source, artifact).value
    ).value
    snapshot = store.create_artifact_snapshot(
        workspace_id=artifact["workspace_id"],
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        name="see(/Users/operator/.ssh/id_ed25519)",
        artifact_version_ids=(committed["id"],),
        actor_id="api-fixture",
        idempotency_key="api-artifact-snapshot-001",
    ).value
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE artifacts SET title = 'path=/tmp/private/control.db' WHERE id = ?",
            (artifact["id"],),
        )
    api = ControlAPI(store, access_token=TOKEN)

    listed = _get(api, f"/api/v1/artifacts?thread_id={artifact['thread_id']}&limit=1")
    exact = _get(api, f"/api/v1/artifacts/{artifact['id']}")
    version = _get(api, f"/api/v1/artifact-versions/{committed['id']}")
    snapshots = _get(
        api, f"/api/v1/artifact-snapshots?workspace_id={artifact['workspace_id']}"
    )

    assert listed.status == exact.status == version.status == snapshots.status == 200
    assert listed.payload["items"] == [exact.payload]
    assert exact.payload["title"] == "Untitled artifact"
    # ⟦batchO⟧ One artifact asked for with `limit=1` is an exactly-full page
    # that is also the LAST page, so the list ends here. This assertion used
    # to read `== artifact["id"]`, which pinned the route's old guess (a full
    # page must have a successor) rather than the answer: that cursor's page
    # comes back empty. The store now proves the cursor by reading one row
    # further, and both cases are pinned below.
    assert listed.payload["next_cursor"] is None
    assert snapshots.payload["items"] == [
        {**snapshot, "name": "Unnamed snapshot"}
    ]
    assert set(version.payload) == {
        "id",
        "artifact_id",
        "logical_version",
        "resource_uri",
        "sha256",
        "byte_length",
        "media_type",
        "run_id",
        "attempt_id",
        "parents",
        "source_ids",
        "generator",
        "tool",
        "state",
        "provenance",
        "created_at",
        "committed_at",
    }
    rendered = json.dumps(
        {"listed": listed.payload, "version": version.payload, "snapshots": snapshots.payload}
    )
    for private in (
        "engine_ref",
        "research_engine_refs",
        "operation_id",
        "request_hash",
        "claim_owner",
        "claim_epoch",
        "root_id",
        "relative_path",
        str(tmp_path),
        "/Users/operator/.ssh/id_ed25519",
        "prompt",
        "secret",
    ):
        assert private not in rendered

    extra = _get(
        api,
        f"/api/v1/artifacts?thread_id={artifact['thread_id']}&absolute_path=/tmp/private",
    )
    too_large = _get(
        api, f"/api/v1/artifacts?thread_id={artifact['thread_id']}&limit=201"
    )
    assert extra.status == too_large.status == 400


def _walk(api: ControlAPI, base: str, limit: int) -> tuple[list[str], list[object]]:
    """Every id the paged route returns, and the cursor each page reported."""

    ids: list[str] = []
    cursors: list[object] = []
    cursor: str | None = None
    for _ in range(20):
        target = f"{base}&limit={limit}"
        if cursor is not None:
            target += f"&after_id={cursor}"
        response = _get(api, target)
        assert response.status == 200, response.payload
        assert len(response.payload["items"]) <= limit
        ids.extend(str(item["id"]) for item in response.payload["items"])
        cursor = response.payload["next_cursor"]
        cursors.append(cursor)
        if cursor is None:
            return ids, cursors
    raise AssertionError("the cursor did not terminate")


def test_the_artifact_list_ends_on_an_exactly_full_last_page(
    tmp_path: Path,
) -> None:
    """⟦batchO⟧ `next_cursor` is proven, not inferred from a full page.

    The route used to answer `items[-1]["id"] if len(items) == limit else
    None`. On a last page that happens to be exactly full that is a cursor
    whose page comes back empty -- and an empty page is, to a cockpit,
    indistinguishable from the end of the list, which is the one distinction
    a cursor exists to make. Both cases are asserted here: a page with more
    behind it must carry a cursor, and an exactly-full page with nothing
    behind it must not.
    """

    store, run, source, artifact = _context(tmp_path)
    thread_id = str(artifact["thread_id"])
    for index in range(3):
        store.create_artifact(
            workspace_id=artifact["workspace_id"],
            thread_id=thread_id,
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            kind="living-brief",
            title=f"Extra artifact {index}",
            actor_id="artifact-fixture",
            idempotency_key=f"batcho-artifact-create-{index:07d}",
        )
    api = ControlAPI(store, access_token=TOKEN)
    base = f"/api/v1/artifacts?thread_id={thread_id}"

    whole = _get(api, f"{base}&limit=200")
    assert whole.status == 200, whole.payload
    ordered = [str(item["id"]) for item in whole.payload["items"]]
    assert len(ordered) == 4
    # Four rows read four at a time: exactly full, and the last page.
    assert whole.payload["next_cursor"] is None
    exact = _get(api, f"{base}&limit=4")
    assert [str(item["id"]) for item in exact.payload["items"]] == ordered
    assert exact.payload["next_cursor"] is None

    # Two pages of two: the first has more behind it, the second is exactly
    # full and is the last.
    walked, cursors = _walk(api, base, 2)
    assert walked == ordered
    assert len(set(walked)) == len(ordered)
    assert cursors == [ordered[1], None]

    # And a page that is NOT full still ends the list.
    walked_three, cursors_three = _walk(api, base, 3)
    assert walked_three == ordered
    assert cursors_three == [ordered[2], None]


def test_the_artifact_snapshot_list_ends_on_an_exactly_full_last_page(
    tmp_path: Path,
) -> None:
    """⟦batchO⟧ The same proven cursor on `/artifact-snapshots`."""

    store, run, source, artifact = _context(tmp_path)
    committed = _complete(
        store, _request_version(store, run, source, artifact).value
    ).value
    workspace_id = str(artifact["workspace_id"])
    for index in range(4):
        store.create_artifact_snapshot(
            workspace_id=workspace_id,
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            name=f"snapshot {index}",
            artifact_version_ids=(committed["id"],),
            actor_id="api-fixture",
            idempotency_key=f"batcho-snapshot-{index:07d}",
        )
    api = ControlAPI(store, access_token=TOKEN)
    base = f"/api/v1/artifact-snapshots?workspace_id={workspace_id}"

    whole = _get(api, f"{base}&limit=200")
    assert whole.status == 200, whole.payload
    ordered = [str(item["id"]) for item in whole.payload["items"]]
    assert len(ordered) == 4
    assert whole.payload["next_cursor"] is None

    exact = _get(api, f"{base}&limit=4")
    assert [str(item["id"]) for item in exact.payload["items"]] == ordered
    assert exact.payload["next_cursor"] is None

    walked, cursors = _walk(api, base, 2)
    assert walked == ordered
    assert len(set(walked)) == len(ordered)
    assert cursors == [ordered[1], None]


def test_artifact_events_project_only_closed_metadata(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    committed = _complete(
        store, _request_version(store, run, source, artifact).value
    ).value
    api = ControlAPI(store, access_token=TOKEN)

    events, _ = api.event_stream_batch(after_cursor=0)
    artifact_events = [event for event in events if event["type"].startswith("artifact.")]

    assert [event["type"] for event in artifact_events] == [
        "artifact.created",
        "artifact.materialization_requested",
        "artifact.version_committed",
        "artifact.head_advanced",
    ]
    committed_event = artifact_events[2]
    assert committed_event["payload"] == {
        "artifact_id": artifact["id"],
        "artifact_version_id": committed["id"],
        "logical_version": 1,
        "resource_uri": committed["resource_uri"],
    }
    rendered = json.dumps(artifact_events)
    assert source["engine_ref"] not in rendered
    assert str(tmp_path) not in rendered
    assert "claim_" not in rendered


def test_artifact_content_route_is_authenticated_verified_and_never_cached(
    tmp_path: Path,
) -> None:
    store, run, source, artifact = _context(tmp_path)
    root = tmp_path / "content-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    store.register_asset_root(
        root_id="artifacts",
        private_path=root,
        max_bytes=1_048_576,
        enabled=True,
        actor_id="api-fixture",
        idempotency_key="api-content-root",
    )
    reservation = _request_version(store, run, source, artifact).value
    committed = _complete(store, reservation).value
    relative_path = reservation["materialization_action"]["relative_path"]
    target = root.joinpath(*relative_path.split("/"))
    target.parent.mkdir(mode=0o700, parents=True)
    target.parent.chmod(0o700)
    target.write_bytes(CONTENT_V1)
    target.chmod(0o600)
    api = ControlAPI(store, access_token=TOKEN)

    unauthorized = api.handle(
        method="GET",
        target=f"/api/v1/artifact-versions/{committed['id']}/content",
        headers={},
    )
    response = _get(
        api, f"/api/v1/artifact-versions/{committed['id']}/content"
    )

    assert unauthorized.status == 403
    assert response.status == 200
    assert response.headers == (("Cache-Control", "no-store"),)
    assert response.payload == {
        "artifact_version_id": committed["id"],
        "media_type": "text/markdown",
        "byte_length": len(CONTENT_V1),
        "sha256": committed["sha256"],
        "content": CONTENT_V1.decode(),
    }
    rendered = json.dumps(response.payload)
    assert relative_path not in rendered
    assert str(root) not in rendered
    assert committed["resource_uri"] not in rendered

    target.write_bytes(b"tampered")
    target.chmod(0o600)
    failed = _get(api, f"/api/v1/artifact-versions/{committed['id']}/content")
    assert failed.status == 409
    assert set(failed.payload) == {
        "category",
        "owner",
        "retryable",
        "status",
        "title",
        "type",
    }
    assert "tampered" not in json.dumps(failed.payload)
    assert str(root) not in json.dumps(failed.payload)
