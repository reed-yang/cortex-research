"""Live Control WAL availability and fresh authorization using real stores."""

import os
import sqlite3

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.sources import reader as reader_module
from cortex_platform.product.sources import search as search_module
from cortex_platform.product.sources.reader import (
    SourceContentUnavailable,
    SourceKnowledgeReader,
)
from cortex_platform.tests.product.api.test_sources import TOKEN, _headers
from cortex_platform.tests.product.sources import (
    test_knowledge_reader as reader_fixtures,
)

corpus = reader_fixtures.corpus
database = reader_fixtures.database
knowledge = reader_fixtures.knowledge


def _response(store, operation):
    source_id = store.list_sources()[0]["id"]
    api = ControlAPI(store, access_token=TOKEN)
    target = "search?q=decoding" if operation == "search" else source_id + "/content"
    return api.handle(method="GET", target="/api/v1/sources/" + target, headers=_headers())


def _revoke(store, source_id, what):
    if what == "root":
        root = store.get_asset_root("research-corpus")
        store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                                max_bytes=root.max_bytes, enabled=False, expected_revision=root.revision,
                                actor_id="operator", idempotency_key="review-revoke-root-001")
    else:
        # No public command revokes an already adopted source. Use the real
        # transaction/schema to exercise the same state/identity authorization.
        with store._transaction() as connection:
            if what == "source":
                connection.execute("UPDATE sources SET import_state = 'failed', engine_ref = NULL, revision = revision + 1 WHERE id = ?", (source_id,))
            else:
                connection.execute("UPDATE sources SET engine_ref = 'paper:other', revision = revision + 1 WHERE id = ?", (source_id,))


@pytest.mark.parametrize("checkpoint", [False, True])
def test_real_control_wal_and_checkpointed_nontruncated_wal_allow_read_and_search(knowledge, checkpoint):
    store, _, _, _ = knowledge
    with store._connect() as keeper:
        keeper.execute("SELECT count(*) FROM sources").fetchone()
        store.create_workspace(title="Unrelated Control activity", actor_id="operator",
                               idempotency_key="review-unrelated-write-001")
        wal = store.path.with_name("control.db-wal")
        assert wal.stat().st_size > 0
        if checkpoint:
            busy, frames, copied = keeper.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            assert busy == 0 and frames == copied > 0
            assert wal.stat().st_size > 0
        before = store.path.read_bytes(), wal.read_bytes()
        for operation in ["read", "search"]:
            response = _response(store, operation)
            assert response.status == 200, response.payload
        assert (store.path.read_bytes(), wal.read_bytes()) == before


@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("change", ["unrelated", "root", "source", "engine_ref"])
def test_change_during_access_uses_fresh_authorization(knowledge, monkeypatch, operation, change):
    store, _, _, _ = knowledge
    source_id = store.list_sources()[0]["id"]
    module, name = (reader_module, "_read_regular") if operation == "read" else (search_module, "_result")
    original = getattr(module, name)
    changed = False

    def during(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            if change == "unrelated":
                store.create_workspace(title="Concurrent unrelated write", actor_id="operator",
                                       idempotency_key="review-during-access-001")
            else:
                _revoke(store, source_id, change)
        return result

    monkeypatch.setattr(module, name, during)
    with store._connect() as keeper:
        keeper.execute("SELECT count(*) FROM sources").fetchone()
        response = _response(store, operation)
        assert changed
        assert store.path.with_name("control.db-wal").stat().st_size > 0
        if change == "unrelated":
            assert response.status == 200, response.payload
        else:
            assert response.status == 409
            assert response.payload["category"] == "source_content_unavailable"


@pytest.mark.parametrize("what", ["root", "source", "engine_ref"])
def test_authorization_committed_only_in_wal_is_not_read_from_old_main_file(knowledge, what):
    store, _, _, _ = knowledge
    source_id = store.list_sources()[0]["id"]
    with store._connect() as keeper:
        keeper.execute("SELECT count(*) FROM sources").fetchone()
        before = store.path.read_bytes()
        _revoke(store, source_id, what)
        assert store.path.read_bytes() == before
        assert _response(store, "read").status == 409
        search = _response(store, "search")
        if what == "root":
            assert search.status == 409
        else:
            assert search.status == 200
            assert source_id not in [item["source_id"] for item in search.payload["results"]]


def test_control_queries_do_not_initialize_write_or_checkpoint(knowledge, monkeypatch):
    store, _, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    statements = []
    original = sqlite3.connect

    def connect(path, **kwargs):
        connection = original(path, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    reader.read(source_id)
    reader.search("decoding")
    assert statements
    assert any(statement == "ROLLBACK" for statement in statements)
    assert all(statement.startswith(("SELECT", "--", "BEGIN", "ROLLBACK", "PRAGMA query_only", "PRAGMA trusted_schema"))
               for statement in statements)


def test_real_uncheckpointed_research_wal_remains_immutable_and_refused(knowledge):
    _, _, database, reader = knowledge
    writer = sqlite3.connect(database)
    try:
        writer.execute("UPDATE chunks SET text = 'changed decoding'")
        writer.commit()
        wal = database.with_name("research.db-wal")
        assert wal.stat().st_size > 0
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in database.parent.glob("research.db*")}
        with pytest.raises(SourceContentUnavailable, match="checkpointed"):
            reader.search("decoding")
        assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in database.parent.glob("research.db*")}
    finally:
        writer.close()


@pytest.mark.parametrize("target", ["database", "parent", "wal", "shm"])
def test_control_symlink_paths_are_refused(knowledge, tmp_path, target):
    store, _, _, _ = knowledge
    source_id = store.list_sources()[0]["id"]
    if target == "database":
        original = store.path
        actual = tmp_path / "actual.db"
        original.rename(actual)
        original.symlink_to(actual)
    elif target == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        store.path = alias / "control.db"
    else:
        actual = tmp_path / "sidecar"
        actual.write_bytes(b"synthetic")
        store.path.with_name("control.db-" + target).symlink_to(actual)
    with pytest.raises(SourceContentUnavailable):
        SourceKnowledgeReader(store).read(source_id)


def test_control_hardlink_is_refused(knowledge, tmp_path):
    store, _, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    os.link(store.path, tmp_path / "linked.db")
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)


def test_control_path_swap_at_sqlite_open_is_refused(knowledge, tmp_path, monkeypatch):
    store, _, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    original = sqlite3.connect
    changed = False

    def connect(path, **kwargs):
        nonlocal changed
        if "immutable" not in path:
            changed = True
            store.path.rename(tmp_path / "original-control.db")
            replacement = original(store.path)
            replacement.execute("CREATE TABLE private (value TEXT)")
            replacement.close()
        return original(path, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)
    assert changed
