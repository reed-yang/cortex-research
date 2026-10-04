"""R2a: dossier-grounded research over real Control, real files and the runtime fake."""

import copy
import sqlite3

import pytest

from cortex_platform.product.control import ControlStore, InvalidTransition
from cortex_platform.product.control.research_store import research_item_id
from cortex_platform.product.research.context import (
    MAX_EXCERPT_BYTES, canonical, cited_labels, digest, validate_snapshot,
)
from cortex_platform.product.research.documents import ROOT_ID, ResearchDocumentAdopter
from cortex_platform.product.research.service import DRAFT, ResearchService, document_excerpts
from cortex_platform.tests.product.research.test_execution import (
    AnswerBackend, append, assert_citation_outcome, context_request, execute, next_run,
)
from cortex_platform.tests.product.sources.test_adoption_reader import corpus, database
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge

IDEA = research_item_id("idea", "idea-decoding")
PROJECT = research_item_id("project", "project-robotics")
DOSSIER = "\n".join(
    ["# Robotic decoding dossier", "", "Retained from the legacy research database.", ""]
    + [f"Filler line {index} of the preserved dossier body." for index in range(200)]
    + ["", "Experiment: speculative decoding for robot control.", "Outcome: paused on budget."]
) + "\n"


class Catalog:
    """The R1a catalog surface the adopter actually uses, over local originals."""

    def __init__(self, entries):
        self.entries = entries

    def list_items(self, limit=100, offset=0):
        items = [item for item, _ in self.entries.values()]
        return {"items": items[offset:offset + limit], "total": len(items)}

    def document_candidates(self, item_id):
        item, paths = self.entries[item_id]
        return [{"kind": item["kind"], "registered_path": str(path)} for path in paths]


@pytest.fixture
def dossier(knowledge, tmp_path):
    """The `knowledge` corpus plus two adopted items with retained documents."""
    store = knowledge[0]
    originals = tmp_path / "originals"
    (originals / "idea").mkdir(parents=True)
    (originals / "project").mkdir(parents=True)
    (originals / "idea" / "dossier.md").write_text(DOSSIER)
    (originals / "idea" / "notes.md").write_text("# Idea notes\n\nParked in 2026.\n")
    (originals / "project" / "plan.md").write_text("# Robotics plan\n\nMonitored project.\n")
    catalog = Catalog({
        IDEA: ({"id": IDEA, "kind": "idea", "origin_id": "idea-decoding", "title": "Decoding idea"},
               [originals / "idea" / "dossier.md", originals / "idea" / "notes.md"]),
        PROJECT: ({"id": PROJECT, "kind": "project", "origin_id": "project-robotics",
                   "title": "Robotics project"}, [originals / "project" / "plan.md"]),
    })
    adopter = ResearchDocumentAdopter(store, catalog)
    roots = {"idea": originals / "idea", "project": originals / "project"}
    adopter.apply(adopter.preview(roots), destination=tmp_path / "adopted")
    return store, adopter, roots, tmp_path / "adopted"


def prepared_thread(store, tag="000001"):
    """A thread with the activation gate open and no run yet, so selection is allowed."""
    store.enable_runtime_activation(
        mode="permanent", actor_id="test-operator", idempotency_key="activate-for-dispatch01")
    workspace = store.create_workspace(
        title="Research", actor_id="local", idempotency_key="workspace-command-0001").value
    return store.create_thread(
        workspace_id=workspace["id"], title="Echo",
        expected_revision=store.get_workspace(workspace["id"])["revision"],
        actor_id="local", idempotency_key=f"thread-command-{tag}").value


def select(store, thread_id, item_id, key="select-research-item-1"):
    return store.select_research_item(
        thread_id=thread_id, item_id=item_id, actor_id="local", idempotency_key=key).value


def started(store, item_id=IDEA, question="/research nonexistent", tag="000001"):
    thread = prepared_thread(store, tag)
    if item_id is not None:
        select(store, thread["id"], item_id, key=f"select-research-item-{tag}")
    return next_run(store, thread["id"], question)


def saved_artifact(store, run):
    version = store.get_research_result(run["id"], run["active_attempt_id"])
    root = store.get_asset_root("research-artifacts")
    return version, (root.private_path / version["materialization_action"]["relative_path"]).read_text()


def test_dossier_only_research_completes_without_a_paper_match(dossier):
    store, *_ = dossier
    run = started(store)
    answer = "The retained dossier proposes a bounded decoding experiment [D1]."
    result, backend, releases, _ = execute(store, run, AnswerBackend(answer))
    assert result["state"] == "completed", store.list_run_events(run["id"])
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["schema_version"] == 2 and snapshot["sources"] == []
    assert [document["label"] for document in snapshot["documents"]] == ["D1", "D2"]
    assert snapshot["item"] == {"id": IDEA, "kind": "idea", "origin_id": "idea-decoding",
                                "title": "Decoding idea", "selection_revision": 1}
    assert "[D1]" in backend.requests[0].system_message
    assert not store.list_messages(run["thread_id"])[-1]["content"].startswith(DRAFT)
    completed = next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.completed")
    assert completed["payload"]["citation_status"] == "labels_valid_claims_unverified"
    version = store.get_research_result(run["id"], run["active_attempt_id"])
    assert version["provenance"]["schema_version"] == 2
    assert version["source_ids"] == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM artifact_version_sources").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM run_source_bindings").fetchone()[0] == 0
    assert releases.pins == releases.finished


def test_mixed_paper_and_dossier_evidence_keeps_paper_authorization(dossier):
    store, *_ = dossier
    run = started(store, question="/research decoding")
    answer = "Papers report speedups [S1]; the dossier scopes the experiment [D1]."
    result, _, releases, _ = execute(store, run, AnswerBackend(answer))
    assert result["state"] == "completed", store.list_run_events(run["id"])
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["schema_version"] == 2 and snapshot["sources"] and snapshot["documents"]
    version, saved = saved_artifact(store, run)
    assert version["source_ids"] == sorted(s["source_id"] for s in snapshot["sources"])
    assert all(document["document_version_id"] in saved for document in snapshot["documents"])
    assert "unverified_draft" not in saved
    assert releases.pins == releases.finished


def test_thread_without_a_selected_item_keeps_the_v1_packet(dossier):
    store, *_ = dossier
    run = started(store, item_id=None, question="/research decoding")
    result, _, _, _ = execute(store, run, AnswerBackend("Decoding improves inference [S1]."))
    assert result["state"] == "completed"
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["schema_version"] == 1
    assert set(snapshot) == {"schema_version", "query", "retrieval_query", "retrieval_mode",
                             "authority", "sources"}
    assert "[D1]" not in ResearchService.system_message(store.get_research_context(run["id"]))


def test_followup_retains_the_frozen_dossier_and_fresh_research_reselects(dossier):
    store, adopter, roots, destination = dossier
    run = started(store)
    result, backend, releases, _ = execute(store, run, AnswerBackend("Bounded scope [D1]."))
    assert result["state"] == "completed"
    first = store.get_research_context(run["id"])["snapshot"]
    (roots["idea"] / "dossier.md").write_text(DOSSIER + "\nRevised: new experiment section.\n")
    adopter.apply(adopter.preview(roots), destination=destination)
    followup = next_run(store, run["thread_id"], "Expand the second option")
    result, backend, _, _ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "completed"
    assert store.get_research_context(followup["id"])["snapshot"]["documents"] == first["documents"]
    fresh = next_run(store, run["thread_id"], "/research nonexistent")
    assert execute(store, fresh, backend, releases=releases)[0]["state"] == "completed"
    refreshed = store.get_research_context(fresh["id"])["snapshot"]["documents"]
    assert [document["version"] for document in refreshed] == [2, 1]
    assert refreshed != first["documents"]
    assert releases.pins == releases.finished


def test_changed_selection_never_reuses_the_previous_item_packet(dossier):
    store, *_ = dossier
    run = started(store)
    result, backend, releases, _ = execute(store, run, AnswerBackend("Scoped [D1]."))
    assert result["state"] == "completed"
    first = store.get_research_context(run["id"])["snapshot"]
    select(store, run["thread_id"], PROJECT, key="select-research-item-2")
    followup = next_run(store, run["thread_id"], "Continue with this one")
    result, backend, _, _ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "completed"
    snapshot = store.get_research_context(followup["id"])["snapshot"]
    assert snapshot["item"] == {"id": PROJECT, "kind": "project", "origin_id": "project-robotics",
                                "title": "Robotics project", "selection_revision": 2}
    assert snapshot["documents"] != first["documents"]
    assert snapshot["query"] == "Continue with this one"
    assert store.get_research_thread_item(run["thread_id"])["selection_revision"] == 2
    # Reselecting the SAME item is still a new decision: the revision it moves
    # is part of the packet's identity, so the frozen one is not reused either.
    select(store, run["thread_id"], PROJECT, key="select-research-item-3")
    again = next_run(store, run["thread_id"], "And again with the same item")
    assert execute(store, again, backend, releases=releases)[0]["state"] == "completed"
    reselected = store.get_research_context(again["id"])["snapshot"]
    assert reselected["item"]["selection_revision"] == 3
    assert reselected["item"]["id"] == PROJECT
    assert reselected["query"] == "And again with the same item"
    assert releases.pins == releases.finished


def test_replay_after_restart_keeps_the_original_document_evidence(dossier):
    store, _, _, destination = dossier
    run = started(store, question="/research decoding")
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    for path in sorted(destination.rglob("*.md")):
        path.write_text("edited after the packet was frozen")
    rebuilt = ResearchService(ControlStore(store.path))
    # A restarted process reloads the frozen packet; it never rereads the files.
    assert rebuilt.prepare(store.get_run(run["id"]), store.list_messages(run["thread_id"])) == context
    assert store.record_research_context(**context_request(store, run, context)).replayed
    result, _, releases, _ = execute(store, run, AnswerBackend("Retained [S1] and [D1]."),
                                     service=rebuilt)
    assert result["state"] == "completed", store.list_run_events(run["id"])
    assert store.get_research_context(run["id"])["snapshot"] == context["snapshot"]
    first = rebuilt.persist_response(run["id"], run["active_attempt_id"])
    assert rebuilt.persist_response(run["id"], run["active_attempt_id"]) == first
    _, saved = saved_artifact(store, run)
    frozen = context["snapshot"]["documents"][0]
    assert frozen["sha256"] in saved and frozen["excerpts"][0]["retained_sha256"] in saved
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM artifact_versions").fetchone()[0] == 1
    assert releases.pins == releases.finished


def test_cancellation_wins_over_a_dossier_result(dossier):
    store, *_ = dossier
    run = started(store)

    def stop():
        current = store.get_run(run["id"])
        store.transition_run(run_id=run["id"], target_state="cancel_requested",
                             expected_revision=current["revision"], actor_id="local",
                             idempotency_key="stop-dossier-0000001")

    result, _, releases, _ = execute(store, run, AnswerBackend("Scoped [D1].", callback=stop))
    assert result["state"] == "canceled"
    assert store.get_research_result(run["id"], run["active_attempt_id"]) is None
    assert releases.pins == releases.finished


@pytest.mark.parametrize("fault", [
    "unknown_document", "foreign_document", "sha256", "version", "title",
    "selection_revision", "other_item", "disabled_root",
])
def test_admission_rejects_disowned_documents_and_stale_selection(dossier, fault):
    store, *_ = dossier
    donor = started(store)
    context = ResearchService(store).prepare(donor, store.list_messages(donor["thread_id"]))
    # A second thread with the same selection and no context yet: admitting the
    # forged packet is the transactional recheck under test, not an immutability
    # conflict with an already recorded one.
    run = started(store, tag="000002")
    request = copy.deepcopy(context_request(store, run, context))
    request["message_id"] = store.list_messages(run["thread_id"])[-1]["id"]
    request["snapshot"]["authority"]["message_id"] = request["message_id"]
    document = request["snapshot"]["documents"][0]
    if fault == "unknown_document":
        document["document_version_id"] = "rdv_" + "0" * 32
    elif fault == "foreign_document":
        other = store.list_research_documents(PROJECT)[0]
        document.update({key: other[key] for key in ("document_id", "title", "version",
                                                     "media_type", "byte_length", "sha256")},
                        document_version_id=other["id"])
    elif fault == "sha256":
        document["sha256"] = digest("forged")
    elif fault == "version":
        document["version"] = 9
    elif fault == "title":
        document["title"] = "renamed"
    elif fault == "selection_revision":
        request["snapshot"]["item"]["selection_revision"] = 2
    elif fault == "other_item":
        request["snapshot"]["item"] = {"id": PROJECT, "kind": "project",
                                       "origin_id": "project-robotics",
                                       "title": "Robotics project", "selection_revision": 1}
    elif fault == "disabled_root":
        root = store.get_asset_root(ROOT_ID)
        store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                                max_bytes=root.max_bytes, enabled=False,
                                expected_revision=root.revision, actor_id="operator",
                                idempotency_key="disable-dossier-root-01")
    request["sha256"] = digest(canonical(request["snapshot"]))
    with pytest.raises(InvalidTransition):
        store.record_research_context(**request)
    assert store.get_research_context(run["id"]) is None


def test_unreadable_registered_dossier_refuses_before_the_worker(dossier):
    store, _, _, destination = dossier
    run = started(store, question="/research decoding")
    for path in sorted(destination.rglob("*.md")):
        path.write_text("tampered")
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "failed" and backend.run_calls == 0
    failed = next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.failed")
    assert failed["payload"]["category"] == "research_document_unavailable"
    assert store.get_research_context(run["id"]) is None
    assert releases.pins == releases.finished


def test_an_item_without_document_evidence_is_stated_not_borrowed(dossier):
    """An empty dossier is sayable; it never becomes someone else's evidence."""
    store, *_ = dossier
    run = started(store, question="/research decoding")
    packet = copy.deepcopy(ResearchService(store).prepare(
        run, store.list_messages(run["thread_id"]))["snapshot"])
    packet["documents"] = []
    assert validate_snapshot(packet, digest(canonical(packet)))
    message = ResearchService.system_message(
        {"run_id": run["id"], "message_id": store.list_messages(run["thread_id"])[-1]["id"],
         "sha256": digest(canonical(packet)), "snapshot": packet})
    assert "documents list is empty" in message and "not a peer-reviewed paper" in message
    # Nothing is promoted to fill the gap: the sources stay exactly the adopted
    # papers, and a packet with neither kind of evidence is not a packet.
    assert packet["sources"] == ResearchService(store).prepare(
        run, store.list_messages(run["thread_id"]))["snapshot"]["sources"]
    packet["sources"] = []
    with pytest.raises(ValueError, match="invalid research snapshot"):
        validate_snapshot(packet, digest(canonical(packet)))


@pytest.mark.parametrize("answer,drafted", [
    ("Grounded in the dossier [D1] and [D2].", False),
    ("Grounded in the dossier [D1,D2].", False),
    ("Grounded in the dossier [D1 D2].", False),
    ("Grounded in the dossier [D1][D2].", False),
    ("Grounded [D1] and invented [D9].", True),
    ("Grounded [D1] and a paper that was not selected [S1].", True),
    ("No labels at all.", True),
])
def test_dossier_labels_are_accepted_and_unknown_labels_stay_drafts(dossier, answer, drafted):
    store, *_ = dossier
    run = started(store)
    result, _, _, _ = execute(store, run, AnswerBackend(answer))
    assert result["state"] == "completed"
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["sources"] == []
    assert [document["label"] for document in snapshot["documents"]] == ["D1", "D2"]
    assert_citation_outcome(store, run, drafted)


@pytest.mark.parametrize("answer,drafted", [
    # Complete groups: comma, whitespace, full-width separators and adjacency.
    ("Evidence [S1, S2].", False),
    ("Evidence [S1 S2].", False),
    ("Evidence [S1, D1].", False),
    ("Evidence [S1 D1].", False),
    ("[ S1 , D1 ][D2].", False),
    ("Evidence [S1，S2] and [S2、D1].", False),
    ("Evidence [S1] (with a parenthetical aside).", False),
    # Ordinary brackets and inline links neither invalidate nor cite.
    ("Evidence [S1] [sic], with [Supplementary] and [Data].", False),
    ("Evidence [D1] beside [Self Forcing](https://example.test/paper).", False),
    ("[S99](https://example.test/paper) [S1]", False),
    ("[S1](https://example.test/paper)", True),
    # Labels inside a link destination, title or text are part of the link.
    ("See [Data](https://example.test/[S1]).", True),
    ('See [Data](https://example.test/paper "[S1]").', True),
    ("Evidence [S1]; see [Data](https://example.test/[S99]).", False),
    ("Evidence [S1]; see [notes [S99]](https://example.test/paper).", False),
    ("Only prose brackets [sic] and 【S1】.", True),
    # Unknown, lowercase, zero and leading-zero tokens are never authorized.
    ("Unknown [S99].", True),
    ("Unknown [D99].", True),
    ("Grouped [S1,S99].", True),
    ("Grouped [D1 D99].", True),
    ("Adjacent [S1][D99].", True),
    ("Lowercase [S1, s2].", True),
    ("Zero [S1 S0].", True),
    ("Leading zero [D1, S01].", True),
    # Label-led brackets outside the grammar fail closed.
    ("Valid [S1] and range [S1-S99].", True),
    ("Valid [S1] and semicolon [S2; S99].", True),
    ("Valid [S1] and malformed [S 2].", True),
    ("Valid [S1] and suffix [S99a].", True),
    ("Valid [S1] and prose [S1, S2, and S99].", True),
    # A parenthetical that does not complete a link leaves the bracket a citation.
    ("Valid [S1] and range [S1-S99](see dossier).", True),
    ("Valid [S1] and unknown [S99](see dossier).", True),
    # Code spans are not excluded by this grammar.
    ("Quoted `[S99]` beside [S1].", True),
    # Valid labels do not verify the claim they accompany.
    ("A claim the cited source never makes [S1].", False),
])
def test_v2_mixed_citation_groups_and_markdown(dossier, answer, drafted):
    store, *_ = dossier
    run = started(store, question="/research decoding")
    result, _, _, _ = execute(store, run, AnswerBackend(answer))
    assert result["state"] == "completed", store.list_run_events(run["id"])
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["schema_version"] == 2
    assert [source["label"] for source in snapshot["sources"]] == ["S1", "S2"]
    assert [document["label"] for document in snapshot["documents"]] == ["D1", "D2"]
    assert_citation_outcome(store, run, drafted)


@pytest.mark.parametrize("text,cited", [
    ("[S1]", {"S1"}),
    ("[S1, S2]", {"S1", "S2"}),
    ("[S1 S2]", {"S1", "S2"}),
    ("[S1，S2]", {"S1", "S2"}),
    ("[S1、S2]", {"S1", "S2"}),
    ("[S1][D2]", {"S1", "D2"}),
    ("[ S1 ,\n D2\t]", {"S1", "D2"}),
    ("[S1\nS2] then [S2 , S1] and [S1]", {"S1", "S2"}),
    ("[s1, S0, S01, D99]", {"s1", "S0", "S01", "D99"}),
    ("[S1] (with a parenthetical aside)", {"S1"}),
    ("[sic], [Supplementary], [Data] and [Self Forcing]", set()),
    ("[Self Forcing](https://example.org) and [S9](https://example.org)", set()),
    ("[Data](https://example.test/[S1])", set()),
    ('[Data](https://example.test/paper "[S1]")', set()),
    ("[Data](https://example.test/paper '[S9]') [S1]", {"S1"}),
    ("[Data](https://example.test/paper ([S9])) [S1]", {"S1"}),
    ("[Data](<https://example.test/a b/[S9]>) [S1]", {"S1"}),
    ("[Data](https://example.test/(x)/[S1-S3]) [S2]", {"S2"}),
    ("[notes [S9]](https://example.test/paper) [S1]", {"S1"}),
    # Only an immediate ]( opens a link; a later parenthetical is still scanned.
    ("[S1] (see https://example.test/[S2])", {"S1", "S2"}),
    ("【S1】", set()),
    ("[S1-S3]", None),
    ("[S2; S9]", None),
    ("[S 2]", None),
    ("[S9a]", None),
    ("[S1, S2, and S9]", None),
    ("[S1,, S2]", None),
    ("[S1] [S1-S3]", None),
    # A label-led bracket whose ( does not complete an inline link is not a link.
    ("[S1-S3](see dossier)", None),
    ("[S2; S9](not a url) [S1]", None),
    ("[S1-S3](unclosed", None),
    ("[S9](see dossier) [S1]", {"S9", "S1"}),
    ("[S1-S3](notes.md) [S1]", {"S1"}),
])
def test_v2_citation_group_syntax(text, cited):
    """Grammar only: tokens are kept verbatim and authorization happens later."""
    packet = {"schema_version": 2, "sources": [], "documents": []}
    assert cited_labels(packet, text) == (None if cited is None else frozenset(cited))


def test_citation_grammar_is_legacy_only_for_v1_packets():
    text = "[S1, S2] [D9] [sic] [S1](https://example.org)"
    assert cited_labels({"schema_version": 1, "sources": []}, text) == {"S1, S2", "sic", "S1"}
    # Any later packet version reads the group grammar, not the v1 scanner.
    later = {"schema_version": 3, "sources": [], "documents": []}
    assert cited_labels(later, text) == {"S1", "S2", "D9"}


def test_document_bounds_and_forged_excerpts_are_refused(dossier):
    store, *_ = dossier
    run = started(store)
    packet = copy.deepcopy(ResearchService(store).prepare(
        run, store.list_messages(run["thread_id"]))["snapshot"])
    assert validate_snapshot(packet, digest(canonical(packet)))
    for mutate in (
        lambda p: p["documents"][0]["excerpts"][0].update(text="forged"),
        lambda p: p["documents"][0]["excerpts"].extend(p["documents"][0]["excerpts"] * 3),
        lambda p: p["documents"][0].update(excerpts=[]),
        lambda p: p["documents"].extend(copy.deepcopy(p["documents"]) * 6),
        lambda p: p["item"].update(origin_id="mismatched-origin"),
        lambda p: p["item"].update(selection_revision=0),
        lambda p: p.update(documents=[], sources=[]),
        lambda p: p.update(schema_version=1),
    ):
        forged = copy.deepcopy(packet)
        mutate(forged)
        with pytest.raises(ValueError, match="invalid research snapshot"):
            validate_snapshot(forged, digest(canonical(forged)))


def test_excerpts_are_bounded_windows_with_line_and_offset_locators():
    excerpts = document_excerpts(DOSSIER, "speculative decoding")
    assert 2 <= len(excerpts) <= 3
    assert excerpts[0]["kind"] == "document_prefix" and excerpts[0]["locator"].endswith(":offset:0")
    assert all(len(item["text"].encode("utf-8")) <= MAX_EXCERPT_BYTES for item in excerpts)
    assert all(item["retained_sha256"] == digest(item["text"]) for item in excerpts)
    match = excerpts[1]
    assert match["kind"] == "document_match" and "speculative decoding" in match["text"]
    offset = int(match["locator"].rsplit(":", 1)[1])
    assert DOSSIER.encode("utf-8")[offset:].decode("utf-8").startswith(match["text"])
    assert document_excerpts("", "anything") == []
