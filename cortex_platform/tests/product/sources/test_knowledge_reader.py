"""K1 reads use real adoption commits, real schema, and real corpus files."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from cortex_platform.product.sources.adoption import read_corpus
from cortex_platform.product.sources.reader import (
    SourceContentUnavailable,
    SourceKnowledgeReader,
    SourceQueryInvalid,
)
from cortex_platform.tests.product.sources.fakes import make_store
from cortex_platform.tests.product.sources.test_adoption_reader import (
    _add_paper,
    _write,
    corpus,
    database,
)


@pytest.fixture
def knowledge(tmp_path, corpus, database):
    store = make_store(tmp_path)
    store.register_asset_root(
        root_id="research-corpus", private_path=corpus, max_bytes=1 << 30,
        enabled=True, actor_id="operator", idempotency_key="root-knowledge-001",
    )
    for index, (paper_dir, title) in enumerate([
        ("20260906-English", "Speculative Decoding"),
        ("20260906-中文论文", "机器人推测解码方法"),
    ], 1):
        _add_paper(database, corpus, paper_dir=paper_dir, title=title,
                   arxiv_id=f"2609.{index:05d}")
        _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
               (paper_dir, "Method", 0, "speculative decoding improves robot inference"))
    manifest = read_corpus(database=database, corpus_root=corpus).manifest
    store.commit_adoption_manifest(
        manifest=manifest, corpus_root_id="research-corpus", actor_id="operator",
        idempotency_key="adopt-knowledge-001",
    )
    _add_paper(database, corpus, paper_dir="unadopted", title="机器人 Unadopted",
               arxiv_id="2609.99999")
    _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
           ("unadopted", "Method", 0, "speculative speculative decoding"))
    return store, corpus, database, SourceKnowledgeReader(store)


def test_canonical_and_encoded_source_resolution(knowledge):
    store, root, _, reader = knowledge
    for source in store.list_sources():
        result = reader.read(source["id"])
        assert result["source_id"] == source["id"]
        assert result["canonical_id"] == source["canonical_id"]
        assert result["text"] == "notes for " + source["official_title"]
        assert result["content_sha256"] == hashlib.sha256(result["text"].encode()).hexdigest()
        assert result["start_line"] == result["end_line"] == 1
        assert result["next_cursor"] is None
        assert str(root) not in str(result)
    with pytest.raises(SourceContentUnavailable):
        reader.read("2609.00001")
    with pytest.raises(SourceContentUnavailable):
        reader.read("paper:unadopted")


def test_utf8_byte_pagination_is_lossless_and_version_bound(knowledge):
    store, root, _, reader = knowledge
    source = store.list_sources()[0]
    path = root / "20260906-English" / "notes.md"
    text = "第一行\nsecond line is long\n尾行"
    path.write_text(text, encoding="utf-8")
    cursor = None
    pages = []
    while True:
        page = reader.read(source["id"], cursor=cursor, limit=7)
        assert len(page["text"].encode()) <= 7
        assert page["start_line"] >= 1
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert "".join(p["text"] for p in pages) == text
    assert pages[0]["start_line"] == pages[0]["end_line"] == 1
    assert pages[-1]["end_line"] == 3
    cursor = pages[0]["next_cursor"]
    assert SourceKnowledgeReader(store).read(source["id"], cursor=cursor, limit=7) == pages[1]
    with pytest.raises(SourceContentUnavailable):
        reader.read(store.list_sources()[1]["id"], cursor=cursor)
    with pytest.raises(SourceContentUnavailable):
        reader.read(source["id"], kind="full_text", cursor=cursor)
    path.write_text("modified\n" + text, encoding="utf-8")
    with pytest.raises(SourceContentUnavailable, match="changed"):
        reader.read(source["id"], cursor=cursor)


@pytest.mark.parametrize("limit", [0, -1, True, 20001, 1.5, "20", None])
def test_read_limit_validation(knowledge, limit):
    with pytest.raises(SourceQueryInvalid):
        knowledge[3].read(knowledge[0].list_sources()[0]["id"], limit=limit)


@pytest.mark.parametrize("cursor", ["", "../notes.md", "a" * 98, "x" * 5000, 1, {}])
def test_invalid_cursor(knowledge, cursor):
    with pytest.raises(SourceQueryInvalid):
        knowledge[3].read(knowledge[0].list_sources()[0]["id"], cursor=cursor)


@pytest.mark.parametrize("kind", ["../notes", "/etc/passwd", "pdf", [], None])
def test_closed_content_kinds(knowledge, kind):
    with pytest.raises(SourceQueryInvalid):
        knowledge[3].read(knowledge[0].list_sources()[0]["id"], kind=kind)


def test_file_and_row_limits_missing_and_invalid_utf8(knowledge, monkeypatch):
    from cortex_platform.product.sources import reader as module

    store, root, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    path = root / "20260906-English" / "notes.md"
    path.unlink()
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)
    path.write_bytes(b"\xff")
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)
    path.write_bytes(b"x" * 11)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 10)
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)
    path.write_bytes(b"x\nx\nx\n")
    monkeypatch.setattr(module, "MAX_FILE_LINES", 2)
    with pytest.raises(SourceContentUnavailable, match="row limit"):
        reader.read(source_id)


def test_page_line_limit_and_empty_file(knowledge, monkeypatch):
    from cortex_platform.product.sources import reader as module

    store, root, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    path = root / "20260906-English" / "notes.md"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(module, "MAX_PAGE_LINES", 2)
    page = reader.read(source_id)
    assert (page["text"], page["start_line"], page["end_line"]) == ("a\nb\n", 1, 2)
    page = reader.read(source_id, cursor=page["next_cursor"])
    assert (page["text"], page["start_line"], page["end_line"]) == ("c\n", 3, 3)
    path.write_bytes(b"")
    assert reader.read(source_id)["start_line"] == reader.read(source_id)["end_line"] == 0


@pytest.mark.parametrize("target", ["file", "directory", "root", "database"])
def test_symlink_escape_is_refused(knowledge, tmp_path, target):
    store, root, db, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "file":
        path = root / "20260906-English" / "notes.md"
        (outside / "secret").write_text("private secret")
        path.unlink()
        path.symlink_to(outside / "secret")
    elif target == "directory":
        path = root / "20260906-English"
        path.rename(outside / "paper")
        path.symlink_to(outside / "paper", target_is_directory=True)
    elif target == "root":
        root.rename(outside / "corpus")
        root.symlink_to(outside / "corpus", target_is_directory=True)
    else:
        db.rename(outside / "research.db")
        db.symlink_to(outside / "research.db")
    if target != "database":
        with pytest.raises(SourceContentUnavailable) as error:
            reader.read(source_id)
        assert str(tmp_path) not in str(error.value)
    if target in {"root", "database"}:
        with pytest.raises(SourceContentUnavailable):
            reader.search("decoding")
    elif target == "directory":
        assert source_id not in [r["source_id"] for r in reader.search("decoding")["results"]]


def test_disabled_root_blocks_both_methods(knowledge):
    store, _, _, reader = knowledge
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(root_id=root.root_id, expected_revision=root.revision,
                            private_path=root.private_path, max_bytes=root.max_bytes,
                            enabled=False, actor_id="operator", idempotency_key="disable-root-001")
    with pytest.raises(SourceContentUnavailable):
        reader.read(store.list_sources()[0]["id"])
    with pytest.raises(SourceContentUnavailable):
        reader.search("机器人")


def test_missing_root_and_uninitialized_control_do_not_create_state(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(SourceContentUnavailable):
        SourceKnowledgeReader(store).search("robotics")
    store.path = tmp_path / "missing.db"
    with pytest.raises(SourceContentUnavailable):
        SourceKnowledgeReader(store).search("robotics")
    assert not store.path.exists()


def test_reads_and_searches_do_not_mutate_any_database_or_corpus(knowledge, tmp_path, monkeypatch):
    from cortex_research import db as legacy

    store, _, _, reader = knowledge
    def forbidden(*args, **kwargs):
        pytest.fail("legacy database initialization must not run")
    monkeypatch.setattr(legacy, "connect", forbidden)
    monkeypatch.setattr(legacy, "apply_schema", forbidden)
    def snapshot():
        return {str(p.relative_to(tmp_path)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in tmp_path.rglob("*") if p.is_file()
                and p.name not in {"control.db-wal", "control.db-shm"}}
    before = snapshot()
    reader.read(store.list_sources()[0]["id"])
    reader.search("speculative decoding")
    reader.search("推测解码")
    assert snapshot() == before
    # Live Control permits SHM coordination, but the reader cannot add WAL frames.
    wal = tmp_path / "control.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_database_connections_are_read_only_and_query_only(knowledge, monkeypatch):
    original = sqlite3.connect
    observed = []
    class Checked(sqlite3.Connection):
        def close(self):
            assert self.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                self.execute("CREATE TABLE forbidden_write (id INTEGER)")
            super().close()
    def connect(path, **kwargs):
        assert "mode=ro" in path
        assert kwargs["uri"] is True
        observed.append(path)
        return original(path, factory=Checked, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    knowledge[3].search("decoding")
    assert len(observed) == 2
    assert "immutable=1" in observed[1]


def test_root_byte_limit_applies_to_reads_and_search(knowledge):
    store, _, _, reader = knowledge
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                            max_bytes=2, enabled=True, expected_revision=root.revision,
                            actor_id="operator", idempotency_key="root-byte-limit-001")
    with pytest.raises(SourceContentUnavailable):
        reader.read(store.list_sources()[0]["id"])
    with pytest.raises(SourceContentUnavailable):
        reader.search("decoding")


def test_database_path_swap_during_connect_cannot_return_outside_data(knowledge, tmp_path, monkeypatch):
    _, _, database, reader = knowledge
    original = sqlite3.connect
    outside = tmp_path / "outside.db"
    connection = original(outside)
    connection.execute("CREATE TABLE private (secret TEXT)")
    connection.close()
    swapped_copy = False
    def swapped(path, **kwargs):
        nonlocal swapped_copy
        if "immutable=1" in path:
            assert path.startswith("file:/dev/fd/")
            swapped_copy = True
            database.rename(tmp_path / "original.db")
            database.symlink_to(outside)
        return original(path, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", swapped)
    with pytest.raises(SourceContentUnavailable) as error:
        reader.search("decoding")
    assert swapped_copy
    assert str(outside) not in str(error.value)
    assert "private" not in str(error.value)


def test_in_place_content_change_during_read_is_refused(knowledge, monkeypatch):
    from cortex_platform.product.sources import reader as module

    store, root, _, reader = knowledge
    original = module._read_regular
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "20260906-English" / "notes.md").write_bytes(b"x" * len(result))
        return result
    monkeypatch.setattr(module, "_read_regular", changed)
    with pytest.raises(SourceContentUnavailable, match="changed"):
        reader.read(store.list_sources()[0]["id"])


def test_cursor_tampering_and_small_unicode_limit(knowledge):
    store, root, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    (root / "20260906-English" / "notes.md").write_text("中文\ncontent", encoding="utf-8")
    with pytest.raises(SourceQueryInvalid):
        reader.read(source_id, limit=1)
    cursor = reader.read(source_id, limit=3)["next_cursor"]
    replacement = "A" if cursor[10] != "A" else "B"
    with pytest.raises(SourceQueryInvalid):
        reader.read(source_id, cursor=cursor[:10] + replacement + cursor[11:])


def test_source_row_bound(knowledge, monkeypatch):
    from cortex_platform.product.sources import reader as module

    monkeypatch.setattr(module, "MAX_SOURCES", 1)
    with pytest.raises(SourceContentUnavailable, match="row limit"):
        knowledge[3].search("decoding")


def test_document_kinds_and_hardlinks(knowledge):
    import os

    store, root, _, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    assert reader.read(source_id, kind="full_text")["text"] == "full text"
    path = root / "20260906-English" / "grounding.md"
    path.write_text("grounding evidence", encoding="utf-8")
    assert reader.read(source_id, kind="grounding")["text"] == "grounding evidence"
    os.link(path, root.parent / "hardlink")
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id, kind="grounding")
