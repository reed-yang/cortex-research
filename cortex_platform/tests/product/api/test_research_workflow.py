from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore
from cortex_platform.product.workflows import GoldenWorkflowFacade

from ..sources.fakes import create_golden_intent, make_run, make_store, register_echo
from ..workflows.test_golden_facade import _case

TOKEN = "r" * 48


@pytest.mark.parametrize(
    "secret",
    (
        "ghp_1234567890ABCDEF",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJabcdefgh.ijklmnop.qrstuvwx",
        "-----BEGIN PRIVATE KEY-----",
    ),
)
def test_research_projection_rejects_secret_bearing_text(secret: str) -> None:
    from cortex_platform.product.api.research import ResearchWorkflowProjector

    assert ResearchWorkflowProjector._optional_safe_text(secret) is None


def _insert_completed_run(
    store: ControlStore,
    *,
    run_id: str,
    thread_id: str,
    created_at: str,
) -> None:
    with store._transaction() as conn:
        conn.execute(
            """INSERT INTO runs
               (id, thread_id, state, active_attempt_id, stage,
                latest_sequence, revision, created_at, updated_at)
               VALUES (?, ?, 'completed', NULL, NULL, 0, 0, ?, ?)""",
            (run_id, thread_id, created_at, created_at),
        )


def test_run_history_selector_is_recent_first_bounded_and_stable(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    initial = make_run(store)
    thread_id = initial["thread_id"]
    with store._transaction() as conn:
        conn.execute(
            """UPDATE runs
               SET state = 'completed', active_attempt_id = NULL,
                   created_at = ?, updated_at = ? WHERE id = ?""",
            ("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", initial["id"]),
        )
    for index in range(100):
        _insert_completed_run(
            store,
            run_id=f"run-history-{index:03d}",
            thread_id=thread_id,
            created_at=(
                "2026-07-28T00:00:00Z"
                if index >= 98
                else f"2026-07-{index // 4 + 2:02d}T00:00:{index % 4:02d}Z"
            ),
        )

    workspace_id = store.get_thread(thread_id)["workspace_id"]
    foreign_thread = store.create_thread(
        workspace_id=workspace_id,
        title="Foreign",
        expected_revision=store.get_workspace(workspace_id)["revision"],
        actor_id="fixture",
        idempotency_key="foreign-thread-history",
    ).value
    _insert_completed_run(
        store,
        run_id="run-history-foreign",
        thread_id=foreign_thread["id"],
        created_at="2026-07-29T00:00:00Z",
    )

    first = store.list_thread_runs(thread_id=thread_id, limit=100)

    assert len(first) == 100
    assert [item["id"] for item in first[:2]] == [
        "run-history-099",
        "run-history-098",
    ]
    assert {item["thread_id"] for item in first} == {thread_id}
    assert first.next_cursor == first[-1]["id"]

    remaining = store.list_thread_runs(
        thread_id=thread_id, after_id=first.next_cursor, limit=100
    )
    assert [item["id"] for item in remaining] == [initial["id"]]
    assert remaining.next_cursor is None
    assert not {item["id"] for item in first} & {item["id"] for item in remaining}

    with pytest.raises(ValueError, match="after_id"):
        store.list_thread_runs(
            thread_id=thread_id, after_id="run-history-foreign", limit=10
        )


def test_run_history_selector_rejects_invalid_bounds_without_schema_mutation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)
    with store._connect() as conn:
        before = int(conn.execute("PRAGMA schema_version").fetchone()[0])

    for invalid in (0, 101, True):
        with pytest.raises(ValueError, match="limit"):
            store.list_thread_runs(thread_id=run["thread_id"], limit=invalid)

    with store._connect() as conn:
        after = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    assert after == before


def test_run_history_selector_orders_mixed_precision_timestamps_by_time(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    initial = make_run(store)
    with store._transaction() as conn:
        conn.execute(
            """UPDATE runs SET state = 'completed', active_attempt_id = NULL,
                   created_at = '2026-07-27T00:00:00Z',
                   updated_at = '2026-07-27T00:00:00Z' WHERE id = ?""",
            (initial["id"],),
        )
    _insert_completed_run(
        store,
        run_id="run-exact-second",
        thread_id=initial["thread_id"],
        created_at="2026-07-28T00:00:00Z",
    )
    _insert_completed_run(
        store,
        run_id="run-fractional-later",
        thread_id=initial["thread_id"],
        created_at="2026-07-28T00:00:00.500000Z",
    )

    first = store.list_thread_runs(thread_id=initial["thread_id"], limit=1)
    second = store.list_thread_runs(
        thread_id=initial["thread_id"], after_id=first.next_cursor, limit=1
    )

    assert [item["id"] for item in first] == ["run-fractional-later"]
    assert [item["id"] for item in second] == ["run-exact-second"]
    assert first.next_cursor == "run-fractional-later"


def test_g0_projection_is_closed_and_preserves_source_conflict(tmp_path: Path) -> None:
    from cortex_platform.product.api.research import ResearchWorkflowProjector

    store = make_store(tmp_path)
    run = make_run(store)
    register_echo(store)
    intent = create_golden_intent(store, run)
    blocked = GoldenWorkflowFacade(
        store=store,
        engine=None,
        runtime=None,
        artifacts=None,
        worker_id="projection-fixture",
        case=_case(intent["id"]),
    ).start(run["id"])

    value = ResearchWorkflowProjector(store).project(run["id"])

    assert value["schema_version"] == 1
    assert value["workflow"]["id"] == blocked.workflow_id
    assert value["workflow"]["current_stage_key"] == "await_source_decision"
    assert value["source_gates"][0]["title_observation"] == intent["title"]
    assert value["source_gates"][0]["locator_observation"] == intent["locator"]
    assert value["source_gates"][0]["decision"]["state"] == "pending"
    assert value["sources"] == []
    assert value["lineage"] == {
        "nodes": [],
        "links": [],
        "successor_node_id": None,
    }
    assert value["artifacts"] == []
    assert value["snapshots"] == []
    assert set(value["workflow"]["stages"][0]) == {
        "key",
        "position",
        "effect",
        "state",
        "revision",
        "attempt",
        "checkpoint_enabled",
        "started_at",
        "completed_at",
    }


def test_research_routes_enforce_closed_queries_and_relationships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex_platform.product.api.research import ResearchWorkflowProjector
    from cortex_platform.product.artifacts.reader import ArtifactReader

    store = make_store(tmp_path)
    run = make_run(store)
    api = ControlAPI(store, access_token=TOKEN)
    headers = {"X-Cortex-Control-Token": TOKEN}

    history = api.handle(
        method="GET",
        target=f"/api/v1/threads/{run['thread_id']}/runs?limit=100",
        headers=headers,
    )
    projection = api.handle(
        method="GET",
        target=f"/api/v1/runs/{run['id']}/research-workflow",
        headers=headers,
    )

    assert history.status == projection.status == 200
    # ⟦ADJ-A⟧ The run history carries whose each run is, exactly as the thread
    # list carries whose each thread is. One key added to the store row.
    assert history.payload == {
        "items": [{**store.get_run(run["id"]), "engine_owned": False}],
        "next_cursor": None,
    }
    # ⟦ADJ-G-1⟧ The research projection is a SECOND producer of the Run DTO,
    # built from a closed literal rather than from `_run_projection`, so a key
    # added to the DTO is silently missing here until something decodes it --
    # and the cockpit's `decodeRun` throws, taking the whole research panel
    # down for every run. The key set is pinned so the two producers cannot
    # drift again; keep it identical to `decodeRun`'s required fields.
    assert set(projection.payload["run"]) == {
        "active_attempt_id",
        "created_at",
        "engine_owned",
        "id",
        "latest_sequence",
        "revision",
        "stage",
        "state",
        "thread_id",
        "updated_at",
    }
    assert projection.payload["run"]["engine_owned"] is False
    assert projection.payload["workflow"] is None
    for target in (
        f"/api/v1/threads/{run['thread_id']}/runs?limit=0",
        f"/api/v1/threads/{run['thread_id']}/runs?limit=101",
        f"/api/v1/threads/{run['thread_id']}/runs?limit=1&limit=2",
        f"/api/v1/threads/{run['thread_id']}/runs?after_id=",
        f"/api/v1/threads/{run['thread_id']}/runs?unknown=1",
        f"/api/v1/runs/{run['id']}/research-workflow?expand=all",
    ):
        assert api.handle(method="GET", target=target, headers=headers).status == 400

    def reject_projection(self, run_id: str):
        del self, run_id
        raise AssertionError("invalid route reached projection")

    monkeypatch.setattr(ResearchWorkflowProjector, "project", reject_projection)

    def reject_history(**kwargs):
        del kwargs
        raise AssertionError("invalid route reached history selector")

    def reject_content(self, artifact_version_id: str):
        del self, artifact_version_id
        raise AssertionError("invalid route reached content reader")

    monkeypatch.setattr(store, "list_thread_runs", reject_history)
    monkeypatch.setattr(ArtifactReader, "read", reject_content)
    for invalid_id in ("bad.id", "%2Frun", "é"):
        for target in (
            f"/api/v1/threads/{invalid_id}/runs",
            f"/api/v1/runs/{invalid_id}/research-workflow",
            f"/api/v1/artifact-versions/{invalid_id}/content",
        ):
            assert (
                api.handle(method="GET", target=target, headers=headers).status == 404
            )


def test_g1_projection_closes_over_golden_sources_lineage_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex_platform.product.api.research import ResearchWorkflowProjector
    from cortex_platform.tests.product.workflows import test_golden_facade as golden

    captured: list[ControlStore] = []
    original_make_store = golden.make_store

    def capture_store(path: Path):
        store = original_make_store(path)
        captured.append(store)
        return store

    monkeypatch.setattr(golden, "make_store", capture_store)
    golden.test_g1_completes_exact_golden_manifest_across_restart_and_replay(tmp_path)
    store = captured[0]
    thread = store.list_threads(workspace_id=store.list_workspaces()[0]["id"])[0]
    run_id = store.list_thread_runs(thread_id=thread["id"])[0]["id"]

    value = ResearchWorkflowProjector(store).project(run_id)

    assert value["workflow"]["state"] == "completed"
    assert {stage["state"] for stage in value["workflow"]["stages"]} == {"completed"}
    assert sorted(item["disposition"] for item in value["sources"]) == [
        "imported",
        "reused",
    ]
    assert [
        (node["id"], node["status"], node["revision"])
        for node in value["lineage"]["nodes"]
    ] == [
        ("lineage-dormant", "dormant", 2),
        ("lineage-graduated", "graduated", 4),
        ("lineage-successor", "active", 1),
    ]
    assert value["lineage"]["successor_node_id"] == "lineage-successor"
    assert len(value["lineage"]["links"]) == 2
    assert len(value["artifacts"]) == 3
    assert all(len(artifact["versions"]) == 1 for artifact in value["artifacts"])
    assert len(value["snapshots"]) == 1
    assert len(value["snapshots"][0]["members"]) == 3


def test_projection_uses_one_precommit_sqlite_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex_platform.product.api.research import ResearchWorkflowProjector

    store = make_store(tmp_path)
    run = make_run(store)
    register_echo(store)
    intent = create_golden_intent(store, run)
    GoldenWorkflowFacade(
        store=store,
        engine=None,
        runtime=None,
        artifacts=None,
        worker_id="snapshot-fixture",
        case=_case(intent["id"]),
    ).start(run["id"])
    original_run = store._run
    first_read = threading.Event()
    continue_read = threading.Event()
    paused = False

    def pausing_run(conn, resource_id):
        nonlocal paused
        value = original_run(conn, resource_id)
        if not paused:
            paused = True
            first_read.set()
            assert continue_read.wait(timeout=5)
        return value

    monkeypatch.setattr(store, "_run", pausing_run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ResearchWorkflowProjector(store).project, run["id"])
        assert first_read.wait(timeout=5)
        with store._transaction() as conn:
            conn.execute(
                """UPDATE workflow_stage_instances
                   SET revision = revision + 1
                   WHERE workflow_id = (
                       SELECT id FROM workflow_instances WHERE run_id = ?
                   ) AND stage_key = 'await_source_decision'""",
                (run["id"],),
            )
        continue_read.set()
        before = future.result(timeout=5)

    monkeypatch.setattr(store, "_run", original_run)
    after = ResearchWorkflowProjector(store).project(run["id"])
    before_stage = next(
        stage
        for stage in before["workflow"]["stages"]
        if stage["key"] == "await_source_decision"
    )
    after_stage = next(
        stage
        for stage in after["workflow"]["stages"]
        if stage["key"] == "await_source_decision"
    )
    assert after_stage["revision"] == before_stage["revision"] + 1
