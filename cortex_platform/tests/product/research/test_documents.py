"""Dossier adoption uses real files, Control transactions and immutable versions."""

import sqlite3

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.research_store import research_item_id
from cortex_platform.product.control import schema
from cortex_platform.product.research.documents import (
    ROOT_ID, ResearchDocumentAdopter, ResearchDocumentReader, ResearchDocumentUnavailable,
)


class Catalog:
    def __init__(self, path):
        self.path = path
        self.item = {"id": research_item_id("idea", "idea-original"), "kind": "idea",
                     "origin_id": "idea-original", "title": "Retained idea"}

    def list_items(self, **kwargs):
        return {"items": [self.item], "total": 1}

    def document_candidates(self, item_id):
        return [{"kind": "idea", "registered_path": str(self.path)}]


@pytest.fixture
def fixture(tmp_path):
    store = ControlStore(tmp_path / "control" / "control.db")
    store.initialize()
    source = tmp_path / "originals"
    source.mkdir()
    document = source / "idea.md"
    document.write_text("# Preserved idea\n\nA formula $x^2$.\n")
    catalog = Catalog(document)
    adopter = ResearchDocumentAdopter(store, catalog)
    return store, catalog, adopter, source, tmp_path / "adopted"


def apply(fixture):
    store, catalog, adopter, source, target = fixture
    preview = adopter.preview({"idea": source})
    result = adopter.apply(preview, destination=target)
    return preview, result["items"][0]["documents"][0]


def test_preview_and_repeat_adoption_keep_original_and_exact_content(fixture):
    store, catalog, adopter, source, target = fixture
    before = catalog.path.read_bytes()
    preview = adopter.preview({"idea": source})
    assert not target.exists()
    assert store.list_research_documents(catalog.item["id"]) == []
    first = adopter.apply(preview, destination=target)
    assert adopter.apply(preview, destination=target) == first
    doc = first["items"][0]["documents"][0]
    assert ResearchDocumentReader(store).read(doc["id"])["content"].encode() == before
    assert catalog.path.read_bytes() == before
    assert "relative_path" not in doc
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_document_versions").fetchone()[0] == 1


def test_new_version_keeps_prior_document_readable(fixture):
    store, catalog, adopter, source, target = fixture
    _, first = apply(fixture)
    original = ResearchDocumentReader(store).read(first["id"])["content"]
    catalog.path.write_text("# Revised dossier\n\nNew evidence.\n")
    _, second = apply(fixture)
    assert (first["version"], second["version"]) == (1, 2)
    assert first["document_id"] == second["document_id"]
    assert store.list_research_documents(catalog.item["id"]) == [second]
    assert ResearchDocumentReader(store).read(first["id"])["content"] == original


def test_changed_source_refuses_the_approved_preview(fixture):
    store, catalog, adopter, source, target = fixture
    preview = adopter.preview({"idea": source})
    catalog.path.write_text("changed")
    with pytest.raises(ValueError, match="source changed"):
        adopter.apply(preview, destination=target)
    assert store.list_research_documents(catalog.item["id"]) == []


def test_external_reference_is_not_adopted(fixture):
    store, catalog, adopter, source, target = fixture
    outside = source.parent / "outside.md"
    outside.write_text("outside allowed root")
    catalog.path.unlink()
    catalog.path.symlink_to(outside)
    preview = adopter.preview({"idea": source})
    assert preview["items"] == []
    assert preview["skipped"][0]["reason"] == "document_unavailable"


def test_reader_rejects_modified_content_and_disabled_root(fixture):
    store, catalog, adopter, source, target = fixture
    _, doc = apply(fixture)
    reference = store.get_research_document(doc["id"])
    path = target / reference["relative_path"]
    path.write_text("tampered")
    with pytest.raises(ResearchDocumentUnavailable):
        ResearchDocumentReader(store).read(doc["id"])
    with store._transaction() as conn:
        conn.execute("UPDATE asset_roots SET enabled=0 WHERE root_id=?", (ROOT_ID,))
    with pytest.raises(ResearchDocumentUnavailable):
        ResearchDocumentReader(store).read(doc["id"])


def test_failed_registration_is_retryable_without_a_partial_version(fixture, monkeypatch):
    store, catalog, adopter, source, target = fixture
    preview = adopter.preview({"idea": source})
    original = store._audit
    def fail(*args, **kwargs):
        raise RuntimeError("injected after insert")
    monkeypatch.setattr(store, "_audit", fail)
    with pytest.raises(RuntimeError, match="injected"):
        adopter.apply(preview, destination=target)
    assert store.list_research_documents(catalog.item["id"]) == []
    monkeypatch.setattr(store, "_audit", original)
    result = adopter.apply(preview, destination=target)
    assert len(result["items"][0]["documents"]) == 1


def test_open_thread_replays_and_reuses_association_without_a_run(fixture):
    store, catalog, *_ = fixture
    apply(fixture)
    workspace = store.create_workspace(title="Work", actor_id="local", idempotency_key="research-workspace-1").value
    args = dict(item_id=catalog.item["id"], workspace_id=workspace["id"], expected_revision=workspace["revision"], actor_id="local")
    first = store.open_research_thread(**args, idempotency_key="open-research-thread-1")
    assert store.open_research_thread(**args, idempotency_key="open-research-thread-1").replayed
    second = store.open_research_thread(**args, idempotency_key="open-research-thread-2")
    assert first.value == second.value
    assert store.get_research_thread_item(first.value["id"])["id"] == catalog.item["id"]
    assert store.list_thread_runs(thread_id=first.value["id"]) == []


def test_selected_item_changes_only_mapping_and_is_durable(fixture):
    store, catalog, *_ = fixture
    apply(fixture)
    ws = store.create_workspace(title="Bot", actor_id="local", idempotency_key="research-bot-workspace").value
    thread = store.create_thread(workspace_id=ws["id"], title="Bot", expected_revision=ws["revision"], actor_id="local", idempotency_key="research-bot-thread").value
    args = dict(thread_id=thread["id"], item_id=catalog.item["id"], actor_id="local", idempotency_key="research-bot-selection")
    store.select_research_item(**args)
    assert store.select_research_item(**args).replayed
    reopened = ControlStore(store.path)
    reopened.initialize()
    assert reopened.get_research_thread_item(thread["id"])["selection_revision"] == 1
    assert reopened.get_thread(thread["id"]) == thread


def test_schema18_upgrade_retains_rows_and_refuses_partial19(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        for version, script in schema.migration_scripts():
            if version == schema.RESEARCH_ITEMS_MIGRATION:
                break
            conn.executescript(script)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, "old"))
        conn.execute("INSERT INTO workspaces VALUES('old','retained',7,'old','old')")
        conn.commit()
        execute = schema._execute_script_in_transaction
        def interrupt(connection, script):
            execute(connection, script)
            if script == schema._MIGRATION_RESEARCH_ITEMS:
                raise KeyboardInterrupt()
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(KeyboardInterrupt):
            schema.apply_migrations(conn, now="new")
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 18
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='research_items'").fetchone() is None
        monkeypatch.setattr(schema, "_execute_script_in_transaction", execute)
        schema.apply_migrations(conn, now="new")
        schema.apply_migrations(conn, now="again")
        assert conn.execute("SELECT title,revision FROM workspaces WHERE id='old'").fetchone() == ("retained", 7)
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 19
