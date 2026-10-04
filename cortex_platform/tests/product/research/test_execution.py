"""Research uses real Control, adopted FTS/files, and the existing runtime fake."""

import asyncio
import copy
import sqlite3

import pytest

from cortex_platform.product.control import ControlStore, InvalidTransition, RevisionConflict
from cortex_platform.product.control import schema
from cortex_platform.product.orchestration import RunOrchestrator
from cortex_platform.product.research.context import (
    ACTOR, MAX_SNAPSHOT_BYTES, canonical, digest, ResearchFailure, validate_snapshot,
)
from cortex_platform.product.research.service import DRAFT, ResearchService
from cortex_platform.product.artifacts.materializer import FilesystemMaterializer
from cortex_platform.product.sources.reader import SourceKnowledgeReader
from cortex_platform.runtime.hermes import HermesRunResult
from cortex_platform.tests.product.orchestration.test_service import HermesAdapter, Releases, _queued_run
from cortex_platform.runtime.tests.fakes import FakeHermesBackend
from cortex_platform.tests.product.sources.test_adoption_reader import _add_paper, _write, corpus, database
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge

ENGLISH, CHINESE = "20260906-English", "20260906-中文论文"


def add_chunks(database, paper_dir, texts):
    for index, text in enumerate(texts, 1):
        _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
               (paper_dir, "Method", index, text))


def adopt_papers(store, corpus, database, paper_dirs):
    from cortex_platform.product.sources.adoption import read_corpus_subset

    for index, paper_dir in enumerate(paper_dirs, 3):
        _add_paper(database, corpus, paper_dir=paper_dir, title=f"Synthetic paper {index}",
                   arxiv_id=f"2609.{index:05d}")
    manifest = read_corpus_subset(database=database, corpus_root=corpus, paper_dirs=paper_dirs).manifest
    store.commit_adoption_manifest(manifest=manifest, corpus_root_id="research-corpus",
                                    actor_id="operator", idempotency_key="adopt-extra-papers-01")


def failure(store, run):
    return next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.failed")["payload"]["category"]


def append(store, thread_id, text):
    thread = store.get_thread(thread_id)
    return store.append_message(
        thread_id=thread_id, role="user", content=text, expected_revision=thread["revision"],
        actor_id="local", idempotency_key=digest(f"message:{thread_id}:{thread['revision']}"),
    ).value


def queued(store, text="/research decoding"):
    run = _queued_run(store)
    append(store, run["thread_id"], text)
    return run


def next_run(store, thread_id, text):
    append(store, thread_id, text)
    thread = store.get_thread(thread_id)
    return store.create_run(thread_id=thread_id, expected_revision=thread["revision"],
                            actor_id="local", idempotency_key=digest(f"run:{thread['revision']}")).value


class AnswerBackend(FakeHermesBackend):
    def __init__(self, answer="Decoding improves inference [S1].", callback=None):
        super().__init__()
        self.answer = answer
        self.callback = callback
        self.requests = []

    def run(self, request, emit):
        self.run_calls += 1
        self.requests.append(request)
        if self.callback:
            self.callback()
        return HermesRunResult(request.session_ref, self.answer, canceled=False)


def execute(store, run, backend=None, service=None, releases=None):
    backend = backend or AnswerBackend()
    releases = releases or Releases()
    orchestrator = RunOrchestrator(store, HermesAdapter(backend_loader=lambda: backend), releases,
                                   research=service or ResearchService(store))
    result = asyncio.run(orchestrator.dispatch(run["id"]))
    return result, backend, releases, orchestrator


def assert_citation_outcome(store, run, drafted):
    """Message, run.completed and the saved Output agree; re-annotation is stable."""
    status = "unverified_draft" if drafted else "labels_valid_claims_unverified"
    message = store.list_messages(run["thread_id"])[-1]["content"]
    assert message.startswith(DRAFT) is drafted
    event = next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.completed")
    assert event["payload"]["citation_status"] == status
    assert event["payload"]["summary"].startswith(DRAFT) is drafted
    version = store.get_research_result(run["id"], run["active_attempt_id"])
    root = store.get_asset_root("research-artifacts")
    saved = (root.private_path / version["materialization_action"]["relative_path"]).read_text()
    assert saved.startswith(DRAFT) is drafted and not saved.startswith(DRAFT + DRAFT)
    assert f'"citation_status": "{status}"' in saved
    service = ResearchService(store)
    assert service.annotate(run["id"], message) == message
    # Joined attempt messages keep the status of their parts.
    joined = message + "\n\n" + message
    assert service.annotate(run["id"], joined) == joined


def context_request(store, run, context):
    return dict(run_id=run["id"], thread_id=run["thread_id"], attempt_id=run["active_attempt_id"],
                message_id=context["message_id"], query=context["query"], snapshot=context["snapshot"],
                sha256=context["sha256"], expected_revision=store.get_run(run["id"])["revision"],
                actor_id=ACTOR, idempotency_key=digest(f"context:{run['id']}"))


def test_schema_16_upgrade_is_narrow_atomic_and_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "v16.db"
    with sqlite3.connect(path) as conn:
        for version, script in schema.migration_scripts():
            if version == 17:
                break
            conn.executescript(script)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, "before"))
        conn.execute("INSERT INTO workspaces VALUES ('old', 'keep', 7, 'before', 'before')")
        conn.commit()
        original = schema._execute_script_in_transaction
        def crash(connection, script):
            original(connection, script)
            if "CREATE TABLE research_contexts" in script:
                raise RuntimeError("interrupted migration")
        monkeypatch.setattr(schema, "_execute_script_in_transaction", crash)
        with pytest.raises(RuntimeError):
            schema.apply_migrations(conn, now="after")
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 16
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_contexts'").fetchall()
        monkeypatch.setattr(schema, "_execute_script_in_transaction", original)
        schema.apply_migrations(conn, now="after")
        schema.apply_migrations(conn, now="again")
        assert conn.execute("SELECT revision FROM workspaces").fetchone()[0] == 7
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == list(
            range(1, schema.SCHEMA_VERSION + 1)
        )


def test_context_replay_survives_source_file_and_index_changes(knowledge):
    store, root, db, _ = knowledge
    run = queued(store)
    service = ResearchService(store)
    context = service.prepare(run, store.list_messages(run["thread_id"]))
    (root / "20260906-English" / "notes.md").write_text("changed bytes")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE chunks SET text = 'changed index'")
    rebuilt = ResearchService(ControlStore(store.path))
    assert rebuilt.prepare(store.get_run(run["id"]), store.list_messages(run["thread_id"])) == context
    assert store.record_research_context(**context_request(store, run, context)).replayed
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM research_contexts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM run_source_bindings").fetchone()[0] == 0
        for statement in ("UPDATE research_contexts SET query='replace'", "DELETE FROM research_contexts"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(statement)


@pytest.mark.parametrize("fault", ["message", "attempt", "thread", "actor", "revision", "hash", "text_hash", "replacement", "source", "disabled", "unadopted"])
def test_context_rejects_ownership_integrity_and_readiness(knowledge, fault):
    store, *_ = knowledge
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    request = copy.deepcopy(context_request(store, run, context))
    other = store.create_thread(workspace_id=store.get_thread(run["thread_id"])["workspace_id"],
                                title="Other", expected_revision=store.get_workspace(store.get_thread(run["thread_id"])["workspace_id"])["revision"],
                                actor_id="local", idempotency_key="other-thread-000001").value
    other_message = append(store, other["id"], "/research decoding")
    if fault == "message":
        request["message_id"] = store.list_messages(other["id"])[-1]["id"]
    elif fault == "thread":
        request["thread_id"] = other["id"]
    elif fault == "attempt":
        other_run = store.create_run(thread_id=other["id"], expected_revision=store.get_thread(other["id"])["revision"],
                                     actor_id="local", idempotency_key="other-run-00000001").value
        request["attempt_id"] = other_run["active_attempt_id"]
    elif fault == "actor":
        request["actor_id"] = "worker"
    elif fault == "revision":
        request["expected_revision"] -= 1
    elif fault == "hash":
        request["sha256"] = "0" * 64
    elif fault == "text_hash":
        request["snapshot"]["sources"][0]["evidence"][0]["text"] = "forged"
        request["sha256"] = digest(canonical(request["snapshot"]))
    elif fault == "replacement":
        evidence = request["snapshot"]["sources"][0]["evidence"][0]
        evidence["text"] = "replacement"
        evidence["retained_sha256"] = digest(evidence["text"])
        request["sha256"] = digest(canonical(request["snapshot"]))
    elif fault == "source":
        request["snapshot"]["sources"][0]["canonical_id"] = "arxiv:wrong"
        request["sha256"] = digest(canonical(request["snapshot"]))
    elif fault == "disabled":
        root = store.get_asset_root("research-corpus")
        store.update_asset_root(root_id=root.root_id, private_path=root.private_path, max_bytes=root.max_bytes,
                                enabled=False, expected_revision=root.revision, actor_id="local", idempotency_key="disable-root-000001")
    elif fault == "unadopted":
        unadopted = store.register_source(authority="arxiv", authority_id="2609.99999", source_kind="paper",
                                         official_title="Unadopted", engine_ref="paper:unadopted",
                                         actor_id="local", idempotency_key="unadopted-source-0001").value
        request["snapshot"]["sources"][0].update(source_id=unadopted["id"], canonical_id=unadopted["canonical_id"], engine_ref=unadopted["engine_ref"])
        request["sha256"] = digest(canonical(request["snapshot"]))
    with pytest.raises((InvalidTransition, RevisionConflict, ValueError)):
        store.record_research_context(**request)


@pytest.mark.parametrize("question,category", [("/research", "research_query_invalid"),
    ("/research the and OR", "research_query_invalid"), ("/research nonexistent", "research_no_evidence")])
def test_query_failures_do_not_execute_or_leak_pins(knowledge, question, category):
    store, *_ = knowledge
    run = queued(store, question)
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "failed"
    assert next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.failed")["payload"]["category"] == category
    assert backend.run_calls == 0
    assert releases.pins == releases.finished


@pytest.mark.parametrize("question", ["/research decoding", "/research 推测解码"])
def test_real_evidence_and_complete_cited_artifact(knowledge, question):
    store, *_ = knowledge
    run = queued(store, question)
    response = "Evidence [S1].\n" + "Complete retained response. " * 250
    result, backend, releases, _ = execute(store, run, AnswerBackend(response))
    assert result["state"] == "completed", store.list_run_events(run["id"])
    context = store.get_research_context(run["id"])
    assert all(e["kind"] != "title" for s in context["snapshot"]["sources"] for e in s["evidence"])
    assert "untrusted source DATA" in backend.requests[0].system_message
    assert backend.requests[0].user_message == question
    version = store.get_research_result(run["id"], run["active_attempt_id"])
    action = version["materialization_action"]
    root = store.get_asset_root("research-artifacts")
    saved = (root.private_path / action["relative_path"]).read_text()
    assert response.strip() in saved and context["sha256"] in saved
    assert "labels_valid_claims_unverified" in saved
    assert releases.pins == releases.finished


@pytest.mark.parametrize("response", ["No citations", "Wrong [S99]", "Some [S1] and wrong [S0]", "Malformed [S01]", "Valid [S1] and malformed [S 2]", "Valid [S1] and lowercase [s2]"])
def test_invalid_citations_are_visible_drafts(knowledge, response):
    store, *_ = knowledge
    run = queued(store)
    result, _, _, _ = execute(store, run, AnswerBackend(response))
    assert result["state"] == "completed"
    assert store.list_messages(run["thread_id"])[-1]["content"].startswith(DRAFT)
    event = next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.completed")
    assert event["payload"]["citation_status"] == "unverified_draft"
    assert event["payload"]["summary"].startswith(DRAFT)


@pytest.mark.parametrize("response,drafted", [
    # v1 bracket bodies stay indivisible, so groups and S-led prose draft.
    ("Grouped [S1, S2].", True),
    ("Quoted [sic] [S1].", True),
    ("See [Supplementary] [S1].", True),
    ("[Self Forcing](https://example.test/paper) [S1]", True),
    # v1 never parses D brackets, and a link with label text still cites.
    ("Adjacent [S1][D99].", False),
    ("[S1](https://example.test/paper)", False),
    ("Adjacent [S1][S2].", False),
])
def test_v1_citation_parser_compatibility(knowledge, response, drafted):
    store, *_ = knowledge
    run = queued(store)
    result, _, _, _ = execute(store, run, AnswerBackend(response))
    assert result["state"] == "completed", store.list_run_events(run["id"])
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["schema_version"] == 1
    assert [source["label"] for source in snapshot["sources"]] == ["S1", "S2"]
    assert_citation_outcome(store, run, drafted)


def test_annotation_preserves_no_context_and_existing_draft(knowledge):
    store, *_ = knowledge
    run = queued(store)
    service = ResearchService(store)
    assert store.get_research_context(run["id"]) is None
    for text in ("No citations", "Wrong [S99]", DRAFT + "Already drafted [S1]"):
        assert service.annotate(run["id"], text) == text
    result, _, _, _ = execute(store, run, AnswerBackend("Wrong [S99]"))
    assert result["state"] == "completed"
    drafted = store.list_messages(run["thread_id"])[-1]["content"]
    assert drafted == DRAFT + "Wrong [S99]"
    assert service.annotate(run["id"], drafted) == drafted
    # An existing prefix is never removed, even when later text cites validly.
    continued = drafted + "\n\nValid [S1][S2]."
    assert service.annotate(run["id"], continued) == continued


def test_revoked_followup_reports_research_refusal_without_executing(knowledge):
    store, *_ = knowledge
    run = queued(store)
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed"
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(
        root_id=root.root_id, private_path=root.private_path, max_bytes=root.max_bytes,
        enabled=False, expected_revision=root.revision, actor_id="operator",
        idempotency_key="revoke-followup-root-0001",
    )
    count = len(backend.requests)
    followup = next_run(store, run["thread_id"], "Continue the comparison")
    result, _, _, _ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "failed"
    assert len(backend.requests) == count
    failure = next(event for event in store.list_run_events(followup["id"])
                   if event["type"] == "run.failed")
    assert failure["payload"]["category"] == "research_source_not_ready"
    assert failure["payload"]["retryable"] is False
    assert store.get_research_result(followup["id"], followup["active_attempt_id"]) is None


def test_mode_continuation_exit_and_reentry_preserve_history(knowledge):
    store, root, *_ = knowledge
    run = queued(store)
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed"
    first = store.get_research_context(run["id"])
    (root / "20260906-English" / "notes.md").write_text("new content")
    followup = next_run(store, run["thread_id"], "请展开说明")
    result, backend, _, _ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "completed"
    continued = store.get_research_context(followup["id"])["snapshot"]
    for key in ("sources", "retrieval_query", "retrieval_mode"):
        assert continued[key] == first["snapshot"][key]
    assert len(backend.requests[-1].conversation_history) == 3
    for question in ("/chat hello", "another ordinary message"):
        chat = next_run(store, run["thread_id"], question)
        backend.answer = "Hello"
        result, backend, _, _ = execute(store, chat, backend, releases=releases)
        assert result["state"] == "completed"
        assert "Mode: chat." in backend.requests[-1].system_message
        assert backend.requests[-1].metadata["cortex_research_prompt_mode"] == "ephemeral_v1"
        assert store.get_research_context(chat["id"]) is None
        assert store.get_research_result(chat["id"], chat["active_attempt_id"]) is None
    fresh = next_run(store, run["thread_id"], "/research decoding")
    assert execute(store, fresh, backend, releases=releases)[0]["state"] == "completed"
    assert store.get_research_context(fresh["id"])["snapshot"]["sources"] != first["snapshot"]["sources"]


def test_old_chat_to_research_and_topic_refresh_deliver_current_identity(knowledge):
    store, *_ = knowledge
    chat = queued(store, "ordinary chat")
    result, backend, releases, _ = execute(store, chat)
    assert result["state"] == "completed"
    assert "Mode: chat." in backend.requests[-1].system_message
    session = backend.requests[-1].session_ref
    for topic in ("decoding", "robot", "speculative"):
        run = next_run(store, chat["thread_id"], "/research " + topic)
        assert execute(store, run, backend, releases=releases)[0]["state"] == "completed"
        request = backend.requests[-1]
        context = store.get_research_context(run["id"])
        assert request.session_ref == session
        assert request.metadata["cortex_research_prompt_mode"] == "ephemeral_v1"
        assert "Mode: research." in request.system_message
        assert context["sha256"] in request.system_message
        assert context["message_id"] in request.system_message
        assert run["id"] in request.system_message
        assert request.system_message.endswith(canonical(context["snapshot"]))


@pytest.mark.parametrize("state", ["cancel_requested", "pause_requested"])
@pytest.mark.parametrize("boundary", ["before_result", "after_final_link"])
def test_stop_racing_result_never_commits_and_releases_pin(knowledge, monkeypatch, state, boundary):
    store, *_ = knowledge
    run = queued(store)
    def stop():
        current = store.get_run(run["id"])
        store.transition_run(run_id=run["id"], target_state=state, expected_revision=current["revision"],
                             actor_id="local", idempotency_key="stop-result-0000001")
    if boundary == "after_final_link":
        def checkpoint(self, point):
            if point == boundary:
                stop()
        monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", checkpoint)
    backend = AnswerBackend(callback=stop if boundary == "before_result" else None)
    result, _, releases, _ = execute(store, run, backend)
    assert result["state"] == "canceled"
    assert store.get_research_result(run["id"], run["active_attempt_id"]) is None
    assert releases.pins == releases.finished


class Crash(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["after_stage_fsync", "after_final_link", "after_commit"])
def test_interrupted_materialization_replays_without_duplicate_version(knowledge, monkeypatch, boundary):
    store, *_ = knowledge
    run = queued(store)
    service = ResearchService(store)
    def checkpoint(self, point):
        if point == boundary:
            raise Crash()
    original_complete = store.complete_artifact_materialization
    def complete(**kwargs):
        value = original_complete(**kwargs)
        if boundary == "after_commit":
            raise Crash()
        return value
    monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", checkpoint)
    monkeypatch.setattr(store, "complete_artifact_materialization", complete)
    with pytest.raises(Crash):
        execute(store, run, service=service)
    assert store.get_run(run["id"])["state"] == "running"
    assert store.research_attempt_response(run["id"], run["active_attempt_id"])
    store._clock.advance(seconds=301)
    monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", lambda *args: None)
    monkeypatch.setattr(store, "complete_artifact_materialization", original_complete)
    rebuilt = ResearchService(store)
    first = rebuilt.persist_response(run["id"], run["active_attempt_id"])
    assert rebuilt.persist_response(run["id"], run["active_attempt_id"]) == first
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM artifact_versions").fetchone()[0] == 1


def test_terminal_event_replay_is_identical_after_restart(knowledge):
    store, *_ = knowledge
    run = queued(store)
    result, _, releases, orchestrator = execute(store, run)
    assert result["state"] == "completed"
    # A reconstructed service uses committed domain state, never a second version.
    rebuilt = ResearchService(ControlStore(store.path))
    first = rebuilt.persist_response(run["id"], run["active_attempt_id"])
    assert rebuilt.persist_response(run["id"], run["active_attempt_id"]) == first
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM artifact_versions").fetchone()[0] == 1
    assert releases.pins == releases.finished


def test_corpus_unavailable_does_not_call_worker(knowledge):
    store, _, db, _ = knowledge
    run = queued(store)
    db.unlink()
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "failed" and backend.run_calls == 0
    assert next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.failed")["payload"]["category"] == "research_corpus_unavailable"
    assert releases.pins == releases.finished


def test_snapshot_limits_and_title_only_packets_are_rejected(knowledge):
    store, *_ = knowledge
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    for field, value in (("kind", "title"), ("text", "x" * 6001)):
        request = copy.deepcopy(context_request(store, run, context))
        evidence = request["snapshot"]["sources"][0]["evidence"][0]
        evidence[field] = value
        evidence["retained_sha256"] = digest(evidence["text"])
        request["sha256"] = digest(canonical(request["snapshot"]))
        with pytest.raises(ValueError):
            store.record_research_context(**request)


def test_context_does_not_authorize_a_different_run_or_engine_ref(knowledge):
    from cortex_platform.tests.product.sources.fakes import make_named_run

    store, *_ = knowledge
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    other = make_named_run(store, "foreign")
    selected = tuple(sorted(s["source_id"] for s in context["snapshot"]["sources"]))
    with store._connect() as conn:
        store._validate_artifact_sources(conn, run_id=run["id"], source_ids=selected, research_engine_refs=())
        with pytest.raises(InvalidTransition, match="source_not_bound_to_run"):
            store._validate_artifact_sources(conn, run_id=other["id"], source_ids=selected, research_engine_refs=())
        with pytest.raises(InvalidTransition, match="engine_ref_not_owned"):
            store._validate_artifact_sources(conn, run_id=run["id"], source_ids=selected, research_engine_refs=("paper:foreign",))


def test_retry_reuses_context_and_rejects_old_attempt(knowledge):
    store, root, *_ = knowledge
    run = queued(store)
    backend = AnswerBackend(callback=lambda: (_ for _ in ()).throw(RuntimeError("failed worker")))
    result, backend, releases, _ = execute(store, run, backend)
    assert result["state"] == "failed"
    context = store.get_research_context(run["id"])
    (root / "20260906-English" / "notes.md").write_text("changed after failed attempt")
    current = store.get_run(run["id"])
    retried = store.retry_run(run_id=run["id"], expected_revision=current["revision"],
                              actor_id="local", idempotency_key="retry-research-000001", reason="Retry").value
    with pytest.raises(InvalidTransition):
        store.record_research_context(**context_request(store, run, context))
    backend.callback = None
    result, _, releases, _ = execute(store, retried, backend, releases=releases)
    assert result["state"] == "completed"
    assert store.get_research_context(run["id"]) == context
    assert releases.pins == releases.finished


@pytest.mark.parametrize("boundary", ["after_stage_fsync", "after_final_link", "after_commit"])
def test_startup_recovers_artifact_then_releases_pin_without_inventing_success(knowledge, monkeypatch, boundary):
    store, *_ = knowledge
    run = queued(store)
    backend, releases = AnswerBackend(), Releases()
    original_complete = store.complete_artifact_materialization
    def checkpoint(self, point):
        if point == boundary:
            raise Crash()
    def complete(**kwargs):
        value = original_complete(**kwargs)
        if boundary == "after_commit":
            raise Crash()
        return value
    monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", checkpoint)
    monkeypatch.setattr(store, "complete_artifact_materialization", complete)
    with pytest.raises(Crash):
        execute(store, run, backend, releases=releases)
    monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", lambda *args: None)
    monkeypatch.setattr(store, "complete_artifact_materialization", original_complete)
    restarted = RunOrchestrator(store, HermesAdapter(backend_loader=lambda: backend), releases,
                                research=ResearchService(store))
    if boundary != "after_commit":
        reports = asyncio.run(restarted.recover_startup())
        assert reports[0]["outcome"] == "research_materialization_pending"
        assert store.get_run(run["id"])["state"] == "running"
    store._clock.advance(seconds=301)
    asyncio.run(restarted.recover_startup())
    assert store.get_research_result(run["id"], run["active_attempt_id"]) is not None
    assert store.get_run(run["id"])["state"] == "failed"
    assert releases.pins == releases.finished
    assert backend.run_calls == 1


def test_repeated_actual_terminal_event_uses_stored_result(knowledge):
    store, *_ = knowledge
    run = queued(store)
    backend, releases = AnswerBackend(), Releases()
    runtime = HermesAdapter(backend_loader=lambda: backend)
    events = []
    original = runtime.execute
    async def capture(request):
        async for event in original(request):
            events.append(event)
            yield event
    runtime.execute = capture
    orchestrator = RunOrchestrator(store, runtime, releases, research=ResearchService(store))
    assert asyncio.run(orchestrator.dispatch(run["id"]))["state"] == "completed"
    terminal = next(e for e in events if e.type == "runtime.run.completed")
    attempt = store.get_attempt(run["active_attempt_id"])
    rebuilt = RunOrchestrator(store, runtime, releases, research=ResearchService(store), actor_id="restarted")
    result = rebuilt._apply_event(store.get_run(run["id"]), terminal,
                                  binding_row=store.get_runtime_binding(attempt["runtime_binding_id"]),
                                  release_id=attempt["runtime_release_id"],
                                  state_generation_id=attempt["state_generation_id"])
    assert result["state"] == "completed"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM artifact_versions").fetchone()[0] == 1


def test_title_fallback_without_readable_text_is_no_evidence(knowledge):
    store, root, *_ = knowledge
    for name in ("notes.md", "grounding.md", "full_text.md"):
        (root / "20260906-中文论文" / name).unlink(missing_ok=True)
    run = queued(store, "/research 推测解码")
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "failed"
    assert backend.run_calls == 0
    assert next(e for e in store.list_run_events(run["id"]) if e["type"] == "run.failed")["payload"]["category"] == "research_no_evidence"
    assert releases.pins == releases.finished


def test_source_disable_during_materialization_refuses_commit(knowledge, monkeypatch):
    store, *_ = knowledge
    run = queued(store)
    def checkpoint(self, point):
        if point == "after_final_link":
            root = store.get_asset_root("research-corpus")
            store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                                    max_bytes=root.max_bytes, enabled=False, expected_revision=root.revision,
                                    actor_id="operator", idempotency_key="disable-during-result-001")
    monkeypatch.setattr(FilesystemMaterializer, "_crash_checkpoint", checkpoint)
    result, _, releases, _ = execute(store, run)
    assert result["state"] == "failed"
    assert store.get_research_result(run["id"], run["active_attempt_id"]) is None
    assert releases.pins == releases.finished


def test_artifact_root_symlink_never_creates_outside_directory(knowledge, tmp_path):
    store, *_ = knowledge
    external = tmp_path / "outside"
    external.mkdir()
    (store.path.parent / "artifacts").symlink_to(external, target_is_directory=True)
    run = queued(store)
    result, _, releases, _ = execute(store, run)
    assert result["state"] == "failed"
    assert not list(external.iterdir())
    assert releases.pins == releases.finished


def test_multiline_question_searches_one_line_and_keeps_the_verbatim_query(knowledge):
    store, *_ = knowledge
    run = queued(store, "/research speculative\ndecoding")
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed", store.list_run_events(run["id"])
    snapshot = store.get_research_context(run["id"])["snapshot"]
    assert snapshot["query"] == "speculative\ndecoding"
    assert snapshot["retrieval_query"] == "speculative decoding"
    assert snapshot["sources"]
    message = backend.requests[0].system_message
    assert "retrieval_query records the search text used to select this packet" in message
    assert "more than one indexed passage" in message
    assert releases.pins == releases.finished


def test_labels_length_requirements_and_format_words_do_not_reach_search(knowledge):
    store, *_ = knowledge
    run = queued(store, "/research decoding in 500 words [S1]")
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    assert context["query"] == context["snapshot"]["query"] == "decoding in 500 words [S1]"
    assert context["snapshot"]["retrieval_query"] == "decoding"
    assert context["snapshot"]["retrieval_mode"] == "fts5_or"


def test_a_paper_carries_two_passages_before_file_prefixes(knowledge):
    store, root, database, _ = knowledge
    add_chunks(database, ENGLISH, [f"decoding decoding decoding passage {name}"
                                   for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")])
    (root / ENGLISH / "grounding.md").write_text("grounding evidence", encoding="utf-8")
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    sources = context["snapshot"]["sources"]
    assert [source["label"] for source in sources] == ["S1", "S2"]
    english, chinese = sources
    assert [e["kind"] for e in english["evidence"]] == ["indexed_passage", "indexed_passage", "grounding", "notes"]
    assert [e["text"] for e in english["evidence"][:2]] == [
        "decoding decoding decoding passage alpha", "decoding decoding decoding passage beta"]
    assert [e["kind"] for e in chinese["evidence"]] == ["indexed_passage", "notes", "full_text"]
    assert validate_snapshot(context["snapshot"], context["sha256"])


def test_title_matched_paper_without_readable_files_keeps_its_indexed_passage(knowledge):
    store, root, *_ = knowledge
    for name in ("notes.md", "grounding.md", "full_text.md"):
        (root / CHINESE / name).unlink(missing_ok=True)
    run = queued(store, "/research 推测解码 decoding")
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    snapshot = context["snapshot"]
    assert snapshot["retrieval_mode"] == "fts5_or+unicode_title_fallback"
    chinese, english = snapshot["sources"]
    assert chinese["canonical_id"] == "arxiv:2609.00002"
    assert [e["kind"] for e in chinese["evidence"]] == ["indexed_passage"]
    assert english["canonical_id"] == "arxiv:2609.00001"


def test_continuing_multiline_followup_reuses_the_frozen_selection(knowledge, monkeypatch):
    store, *_ = knowledge
    run = queued(store)
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed"
    first = store.get_research_context(run["id"])["snapshot"]
    calls = []
    original = SourceKnowledgeReader.search
    def spy(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SourceKnowledgeReader, "search", spy)
    followup = next_run(store, run["thread_id"], "Expand the comparison\nwith more detail")
    result, backend, _, _ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "completed", store.list_run_events(followup["id"])
    snapshot = store.get_research_context(followup["id"])["snapshot"]
    assert calls == []
    assert snapshot["query"] == "Expand the comparison\nwith more detail"
    for key in ("sources", "retrieval_query", "retrieval_mode", "authority"):
        assert snapshot[key] == first[key]


def test_historical_retrieval_query_is_reused_without_cleanup_claims(knowledge, monkeypatch):
    from cortex_platform.product.research import service as module

    store, *_ = knowledge
    question = "decoding in 500 words [S1]"
    # Contexts stored before the derived search text recorded the question itself.
    with monkeypatch.context() as patch:
        patch.setattr(module, "retrieval_query", lambda text, context=(): text)
        run = queued(store, "/research " + question)
        result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed"
    first = store.get_research_context(run["id"])["snapshot"]
    assert first["retrieval_query"] == first["query"] == question
    followup = next_run(store, run["thread_id"], "Continue\nthe comparison")
    result, backend, _, _ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "completed"
    snapshot = store.get_research_context(followup["id"])["snapshot"]
    assert snapshot["retrieval_query"] == question
    assert snapshot["retrieval_mode"] == first["retrieval_mode"]
    assert snapshot["sources"] == first["sources"]
    message = backend.requests[-1].system_message
    assert "may differ from query" in message
    assert "removed" not in message and "title added" not in message


def test_question_byte_limit_is_checked_before_query_derivation(knowledge, monkeypatch):
    from cortex_platform.product.research import service as module

    store, *_ = knowledge
    calls = []
    original = module.retrieval_query
    def spy(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(module, "retrieval_query", spy)
    run = queued(store)
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed" and len(calls) == 1
    for text in ("decoding " * 120, "/research " + "decoding " * 120):
        later = next_run(store, run["thread_id"], text)
        result, backend, _, _ = execute(store, later, backend, releases=releases)
        assert result["state"] == "failed"
        assert failure(store, later) == "research_query_invalid"
    assert len(calls) == 1 and backend.run_calls == 1
    assert releases.pins == releases.finished


def test_six_large_sources_are_trimmed_within_packet_bounds(knowledge):
    store, root, database, _ = knowledge
    earlier = queued(store)
    assert execute(store, earlier)[0]["state"] == "completed"
    stored = store.get_research_context(earlier["id"])
    extra = [f"20260907-Synthetic-{index}" for index in range(4)]
    adopt_papers(store, root, database, extra)
    # Quotes double under canonical JSON, so six full sources exceed the packet bound.
    heavy = '"' * 1_990
    for number, paper_dir in enumerate([ENGLISH, CHINESE, *extra]):
        add_chunks(database, paper_dir, [f"decoding passage p{number}x{index} {heavy}" for index in range(2)])
        for name in ("grounding.md", "notes.md", "full_text.md"):
            (root / paper_dir / name).write_text(f"{name} {heavy * 3}", encoding="utf-8")
    fresh = next_run(store, earlier["thread_id"], "/research decoding")
    context = ResearchService(store).prepare(fresh, store.list_messages(fresh["thread_id"]))
    snapshot = context["snapshot"]
    assert len(snapshot["sources"]) == 6
    assert validate_snapshot(snapshot, context["sha256"])
    assert len(canonical(snapshot).encode("utf-8")) <= MAX_SNAPSHOT_BYTES
    kinds = [[e["kind"] for e in source["evidence"]] for source in snapshot["sources"]]
    assert all(1 <= len(found) <= 4 and found.count("indexed_passage") <= 2 for found in kinds)
    assert kinds[0] == ["indexed_passage", "indexed_passage", "grounding", "notes"]
    assert sum(map(len, kinds)) < 24
    assert store.get_research_context(earlier["id"]) == stored
