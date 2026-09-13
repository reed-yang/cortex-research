"""Real research writes must remain consumable by the actual Web decoder."""

import json
import copy
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from cortex_platform.product.api.research import ResearchWorkflowProjector
from cortex_platform.product.control import InvalidTransition
from cortex_platform.tests.product.research.test_execution import execute, next_run, queued
from cortex_platform.tests.product.sources.test_adoption_reader import corpus, database
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge

WEB = Path(__file__).resolve().parents[4] / "apps/web"


def decode(value, error=None):
    if shutil.which("node") is None or not (WEB / "node_modules/typescript").is_dir():
        pytest.skip("cross-language acceptance requires Web npm dependencies and Node")
    result = subprocess.run(
        ["node", str(WEB / "tests/research-projection-decoder.mjs")],
        input=json.dumps(value), text=True, capture_output=True, check=False,
    )
    assert result.stdout, result.stderr
    decoded = json.loads(result.stdout)
    if error is not None:
        assert result.returncode == 1 and error in decoded["error"], decoded
        return None
    assert result.returncode == 0, decoded
    return decoded["value"]


def test_completed_research_and_followup_decode_with_real_artifacts(knowledge):
    store, *_ = knowledge
    run = queued(store)
    backend = releases = None
    first_authority = None
    for index in range(2):
        result, backend, releases, _ = execute(store, run, backend, releases=releases)
        assert result["state"] == "completed", store.list_run_events(run["id"])
        projection = ResearchWorkflowProjector(store).project(run["id"])
        assert projection["artifacts"]
        assert decode(projection)["artifacts"]
        context = store.get_research_context(run["id"])
        assert projection == ResearchWorkflowProjector(store).project(run["id"])
        for projected, selected in zip(projection["sources"], context["snapshot"]["sources"]):
            assert projected["disposition"] == "research_selected"
            assert projected["source"]["id"] == selected["source_id"]
            assert projected["research_selection"] == {
                "kind": "research_context", "run_id": run["id"],
                "message_id": context["message_id"],
                "authority_message_id": context["snapshot"]["authority"]["message_id"],
                "context_sha256": context["sha256"], "label": selected["label"],
            }
        if first_authority is not None:
            assert context["snapshot"]["authority"]["message_id"] == first_authority
            assert context["message_id"] != first_authority
        if index == 0:
            first_authority = context["message_id"]
            run = next_run(store, run["thread_id"], "What experiment should follow?")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM run_source_bindings").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM source_resolutions").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["missing", "kind", "run", "label", "hash", "context", "private", "unbound"])
def test_web_rejects_forged_selection_and_keeps_artifact_source_closure(knowledge, fault):
    store, *_ = knowledge
    run = queued(store)
    assert execute(store, run)[0]["state"] == "completed"
    projection = copy.deepcopy(ResearchWorkflowProjector(store).project(run["id"]))
    selection = projection["sources"][0]["research_selection"]
    if fault == "missing":
        del projection["sources"][0]["research_selection"]
    elif fault == "kind":
        selection["kind"] = "source_resolution"
    elif fault == "run":
        selection["run_id"] = "another_run"
    elif fault == "label":
        selection["label"] = "S99"
    elif fault == "hash":
        selection["context_sha256"] = "invalid"
    elif fault == "context":
        selection["context_sha256"] = "0" * 64
    elif fault == "private":
        selection["snapshot"] = {"query": "private question"}
    else:
        projection["sources"] = []
    decode(projection, error="Invalid Cortex response")


@pytest.mark.parametrize("revocation", ["root", "source"])
def test_historical_research_projection_rechecks_source_authorization(knowledge, revocation):
    store, *_ = knowledge
    run = queued(store)
    assert execute(store, run)[0]["state"] == "completed"
    if revocation == "root":
        root = store.get_asset_root("research-corpus")
        store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                                max_bytes=root.max_bytes, enabled=False,
                                expected_revision=root.revision, actor_id="operator",
                                idempotency_key="projection-root-revoked")
    else:
        with store._transaction() as conn:
            conn.execute("UPDATE sources SET import_state = 'failed', engine_ref = NULL")
    with pytest.raises(InvalidTransition, match="research_source_not_ready"):
        ResearchWorkflowProjector(store).project(run["id"])
    assert store.get_research_result(run["id"], run["active_attempt_id"]) is not None


def test_selection_metadata_does_not_publish_snapshot_text_or_private_source_fields(knowledge):
    store, root, *_ = knowledge
    run = queued(store)
    assert execute(store, run)[0]["state"] == "completed"
    with store._transaction() as conn:
        conn.execute("UPDATE sources SET official_title = 'ghp_1234567890ABCDEF'")
    projection = ResearchWorkflowProjector(store).project(run["id"])
    encoded = json.dumps(projection)
    for private in (str(root), "ghp_1234567890ABCDEF", "snapshot_json", "retrieval_query", "engine_ref", "notes for"):
        assert private not in encoded
    assert all(item["source"]["official_title"] == "Untitled source" for item in projection["sources"])
    assert decode(projection)["sources"]


def test_selection_and_source_authorization_share_one_sqlite_snapshot(knowledge, monkeypatch):
    store, *_ = knowledge
    run = queued(store)
    assert execute(store, run)[0]["state"] == "completed"
    root = store.get_asset_root("research-corpus")
    original = store._validate_research_sources
    def revoke_after_validation(conn, snapshot):
        original(conn, snapshot)
        store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                                max_bytes=root.max_bytes, enabled=False,
                                expected_revision=root.revision, actor_id="operator",
                                idempotency_key="projection-snapshot-revoke")
    monkeypatch.setattr(store, "_validate_research_sources", revoke_after_validation)
    assert decode(ResearchWorkflowProjector(store).project(run["id"]))["sources"]
    monkeypatch.setattr(store, "_validate_research_sources", original)
    with pytest.raises(InvalidTransition, match="research_source_not_ready"):
        ResearchWorkflowProjector(store).project(run["id"])


def test_generic_chat_keeps_legacy_empty_projection(knowledge):
    store, *_ = knowledge
    run = queued(store, "/chat hello")
    assert execute(store, run)[0]["state"] == "completed"
    projection = ResearchWorkflowProjector(store).project(run["id"])
    assert projection["sources"] == projection["artifacts"] == []
    assert decode(projection)["sources"] == []


def test_resolution_bound_golden_workflow_keeps_legacy_dto(tmp_path, monkeypatch):
    from cortex_platform.tests.product.workflows import test_golden_facade as golden

    captured = []
    original = golden.make_store
    def capture_store(path):
        store = original(path)
        captured.append(store)
        return store
    monkeypatch.setattr(golden, "make_store", capture_store)
    golden.test_g1_completes_exact_golden_manifest_across_restart_and_replay(tmp_path)
    store = captured[0]
    thread = store.list_threads(workspace_id=store.list_workspaces()[0]["id"])[0]
    run = store.list_thread_runs(thread_id=thread["id"])[0]
    projection = ResearchWorkflowProjector(store).project(run["id"])
    assert {source["disposition"] for source in projection["sources"]} == {"imported", "reused"}
    assert all(set(source) == {"id", "disposition", "created_at", "source"} for source in projection["sources"])
    assert decode(projection)["artifacts"]
