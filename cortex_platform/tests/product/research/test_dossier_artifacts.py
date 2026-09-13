"""Dossier-only runs save real artifacts through the unchanged runtime boundary."""

import sqlite3

import pytest

from cortex_platform.product.api.app import ControlAPI
from cortex_platform.product.api.research import ResearchWorkflowProjector
from cortex_platform.product.artifacts.reader import ArtifactReader
from cortex_platform.product.artifacts.materializer import FilesystemMaterializer
from cortex_platform.product.control import InvalidTransition
from cortex_platform.product.research.documents import ROOT_ID
from cortex_platform.product.research.service import ResearchService
from cortex_platform.tests.product.api.test_research_selection_projection import decode
from cortex_platform.tests.product.research.test_dossier_execution import dossier, started
from cortex_platform.tests.product.research.test_execution import AnswerBackend, execute, next_run
from cortex_platform.tests.product.sources.test_adoption_reader import corpus, database
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge


def disable_documents(store):
    root = store.get_asset_root(ROOT_ID)
    store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                            max_bytes=root.max_bytes, enabled=False,
                            expected_revision=root.revision, actor_id="local",
                            idempotency_key="disable-dossier-root-0001")


def test_dossier_output_and_followup_reopen_with_v2_provenance(dossier):
    store, *_ = dossier
    run = started(store)
    backend = AnswerBackend("The dossier proposes a small ablation [D1].")
    first_documents = None
    for index in range(2):
        result, backend, releases, _ = execute(store, run, backend)
        assert result["state"] == "completed", store.list_run_events(run["id"])
        context = store.get_research_context(run["id"])
        version = store.get_research_result(run["id"], run["active_attempt_id"])
        assert version is not None
        assert version["source_ids"] == []
        assert version["provenance"]["schema_version"] == 2
        assert version["provenance"]["research_context_sha256"] == context["sha256"]
        assert version["provenance"]["document_version_ids"] == sorted(
            d["document_version_id"] for d in context["snapshot"]["documents"])
        content = ArtifactReader(store).read(version["id"]).content
        assert "[D1]" in content and '"documents":' in content
        projected = ResearchWorkflowProjector(store).project(run["id"])
        decoded = decode(projected)
        assert decoded["artifacts"][0]["versions"][0]["provenance"]["schema_version"] == 2
        assert ControlAPI._public_artifact_version(version) == projected["artifacts"][0]["versions"][0]
        assert ResearchService(store).persist_response(
            run["id"], run["active_attempt_id"])["artifact_version_id"] == version["id"]
        if index == 0:
            first_documents = context["snapshot"]["documents"]
            run = next_run(store, run["thread_id"], "Propose the smallest follow-up")
        else:
            assert context["snapshot"]["documents"] == first_documents
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM artifact_versions WHERE state='committed'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM artifact_version_sources").fetchone()[0] == 0
    disable_documents(store)
    with pytest.raises(InvalidTransition):
        ResearchWorkflowProjector(store).project(run["id"])


@pytest.mark.parametrize("stop", ["revoke", "cancel"])
def test_dossier_materialization_obeys_revocation_and_cancel(dossier, monkeypatch, stop):
    store, *_ = dossier
    run = started(store)
    def checkpoint(_self, point):
        if point != "after_final_link":
            return
        if stop == "revoke":
            disable_documents(store)
        else:
            current = store.get_run(run["id"])
            store.transition_run(run_id=run["id"], target_state="cancel_requested",
                                 expected_revision=current["revision"], actor_id="local",
                                 idempotency_key="cancel-dossier-output-001")
    monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", checkpoint)
    result, _, releases, _ = execute(store, run, AnswerBackend("Small ablation [D1]."))
    assert result["state"] == ("canceled" if stop == "cancel" else "failed")
    assert store.get_research_result(run["id"], run["active_attempt_id"]) is None
    assert releases.pins == releases.finished


def test_dossier_excerpt_offsets_refer_to_retained_crlf_bytes():
    from cortex_platform.product.research.service import document_excerpts
    text = "Header\r\n" + "padding line\r\n" * 600 + "important experiment\r\n"
    excerpt = document_excerpts(text, "experiment")[-1]
    offset = int(excerpt["locator"].rsplit(":", 1)[1])
    assert text.encode("utf-8")[offset:].decode("utf-8").replace("\r\n", "\n").startswith(excerpt["text"])
