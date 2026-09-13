"""Lexical retrieval acceptance against the engine's actual FTS schema."""

import hashlib
import sqlite3

import pytest

from cortex_platform.product.sources.adoption import AdoptionEntry, build_manifest, encode_engine_ref
from cortex_platform.product.sources.reader import SourceContentUnavailable, SourceQueryInvalid
from cortex_platform.tests.product.sources.test_adoption_reader import _write, corpus, database
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge


def test_english_or_semantics_deterministic_order_and_evidence(knowledge):
    store, _, database, reader = knowledge
    result = reader.search("the decoding OR nonexistent")
    assert result["query"] == "the decoding OR nonexistent"
    assert result["retrieval_mode"] == "fts5_or"
    assert result == reader.search("the decoding OR nonexistent")
    assert len(result["results"]) == 2
    assert [r["source_id"] for r in result["results"]] == [s["id"] for s in store.list_sources()]
    connection = sqlite3.connect(database)
    try:
        for hit in result["results"]:
            chunk_id = int(hit["evidence_id"].split(":chunk:")[1].split(":")[0])
            text = connection.execute("SELECT text FROM chunks WHERE id = ?", (chunk_id,)).fetchone()[0]
            assert hit["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
            assert hit["content_sha256"] in hit["evidence_id"]
            assert hit["section"] == "Method"
            assert hit["excerpt"] == text
    finally:
        connection.close()


def test_chinese_and_mixed_queries_have_explicit_title_fallback(knowledge):
    _, _, _, reader = knowledge
    result = reader.search("推测解码")
    assert result["retrieval_mode"] == "unicode_title_fallback"
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["canonical_id"] == "arxiv:2609.00002"
    assert hit["section"] == "__title__"
    assert hit["excerpt"] == "机器人推测解码方法"
    assert hit["content_sha256"] == hashlib.sha256(hit["excerpt"].encode()).hexdigest()
    mixed = reader.search("推测解码 decoding", limit=1)
    assert mixed["retrieval_mode"] == "fts5_or+unicode_title_fallback"
    assert mixed["results"] == [hit]
    assert reader.search("不存在的主题")["results"] == []


@pytest.mark.parametrize("query", [None, 1, [], "", "   ", "the and OR", "!!!", "机器" * 200, "x\0y", "\ud800", " ".join(f"term{i}" for i in range(33))])
def test_query_validation(knowledge, query):
    with pytest.raises(SourceQueryInvalid):
        knowledge[3].search(query)


@pytest.mark.parametrize("limit", [0, -2, True, 51, 1.2, "10", None])
def test_result_limit_validation(knowledge, limit):
    with pytest.raises(SourceQueryInvalid):
        knowledge[3].search("decoding", limit=limit)


def test_missing_database_and_fts_fail_typed_without_paths(knowledge):
    _, _, database, reader = knowledge
    _write(database, "DROP TABLE chunks_fts")
    with pytest.raises(SourceContentUnavailable) as error:
        reader.search("decoding")
    assert str(database) not in str(error.value)
    assert error.value.category == "source_content_unavailable"
    database.unlink()
    with pytest.raises(SourceContentUnavailable):
        reader.search("推测解码")
    assert not database.exists()


def test_outstanding_corpus_wal_is_rejected_without_mutation(knowledge):
    _, _, path, reader = knowledge
    wal = path.with_name(path.name + "-wal")
    wal.write_bytes(b"outstanding log")
    before = path.read_bytes(), wal.read_bytes()
    with pytest.raises(SourceContentUnavailable, match="checkpointed"):
        reader.search("decoding")
    assert (path.read_bytes(), wal.read_bytes()) == before


def test_registered_but_unadopted_source_is_not_authorized(knowledge):
    store, _, _, reader = knowledge
    store.register_source(
        authority="arxiv", authority_id="2609.99999", source_kind="paper",
        official_title="机器人 Unadopted", engine_ref="paper:unadopted",
        actor_id="operator", idempotency_key="register-unadopted-001",
    )
    source_id = store.list_sources()[-1]["id"]
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)
    assert source_id not in [r["source_id"] for r in reader.search("decoding")["results"]]
    assert source_id not in [r["source_id"] for r in reader.search("机器人")["results"]]


def test_wrong_root_adoption_is_excluded(knowledge, tmp_path):
    store, _, _, reader = knowledge
    store.register_asset_root(root_id="other-corpus", private_path=tmp_path / "other",
                              max_bytes=1 << 30, enabled=True, actor_id="operator",
                              idempotency_key="other-root-00001")
    manifest = build_manifest([AdoptionEntry(
        paper_dir="unadopted", authority="arxiv", authority_id="2609.99999",
        official_title="机器人 Unadopted", content_digest="a" * 64,
    )])
    store.commit_adoption_manifest(manifest=manifest, corpus_root_id="other-corpus",
                                    actor_id="operator", idempotency_key="other-adopt-00001")
    source_id = store.list_sources()[-1]["id"]
    with pytest.raises(SourceContentUnavailable):
        reader.read(source_id)
    assert source_id not in [r["source_id"] for r in reader.search("decoding")["results"]]


def test_encoded_traversal_is_rejected(knowledge):
    store, _, _, reader = knowledge
    # Encoded engine refs can decode a backslash: the reader rejects path syntax
    # even when the adoption writer's generic identity codec permits it.
    manifest = build_manifest([AdoptionEntry(
        paper_dir="..\\outside", authority="arxiv", authority_id="2609.99998",
        official_title="Unsafe", content_digest="a" * 64,
    )])
    assert encode_engine_ref("..\\outside").startswith("paper:enc/")
    store.commit_adoption_manifest(manifest=manifest, corpus_root_id="research-corpus",
                                    actor_id="operator", idempotency_key="unsafe-adopt-001")
    with pytest.raises(SourceContentUnavailable):
        reader.read(store.list_sources()[-1]["id"])
    with pytest.raises(SourceContentUnavailable):
        reader.search("decoding")


def test_oversized_chunks_rejected_and_excerpts_bounded(knowledge):
    _, _, database, reader = knowledge
    text = "decoding " + "中" * 1000
    _write(database, "UPDATE chunks SET text = ? WHERE id = 1", (text,))
    hit = next(r for r in reader.search("decoding")["results"] if ":chunk:1:" in r["evidence_id"])
    assert len(hit["excerpt"].encode()) <= 2000
    assert hit["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    _write(database, "UPDATE chunks SET text = ? WHERE id = 1", ("decoding " * 10000,))
    with pytest.raises(SourceContentUnavailable, match="byte limit"):
        reader.search("decoding")


def test_missing_directory_is_excluded(knowledge):
    store, root, _, reader = knowledge
    (root / "20260906-English").rename(root / "removed")
    assert store.list_sources()[0]["id"] not in [r["source_id"] for r in reader.search("decoding")["results"]]


def test_stable_error_categories():
    assert SourceQueryInvalid.category == "source_query_invalid"
    assert SourceContentUnavailable.category == "source_content_unavailable"


def test_repeated_adoption_does_not_duplicate_evidence(knowledge):
    store, _, _, reader = knowledge
    before = reader.search("decoding")
    manifest = build_manifest([AdoptionEntry(
        paper_dir="20260906-English", authority="arxiv", authority_id="2609.00001",
        official_title="Speculative Decoding", content_digest="f" * 64,
    )])
    store.commit_adoption_manifest(manifest=manifest, corpus_root_id="research-corpus",
                                    actor_id="operator", idempotency_key="repeat-adopt-00001")
    assert reader.search("decoding") == before


def test_search_uses_fts_and_title_primary_key_not_embeddings(knowledge, monkeypatch):
    original = sqlite3.connect
    statements = []
    class Traced(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            statements.append(sql)
            assert "embedding" not in sql.lower()
            if "FROM chunks_fts" in sql:
                plan = super().execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
                assert any("VIRTUAL TABLE INDEX" in str(row[3]) for row in plan)
                assert not any("SCAN c" == str(row[3]) for row in plan)
            if "FROM papers WHERE paper_dir" in sql:
                plan = super().execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
                assert any("USING INDEX sqlite_autoindex_papers_1" in str(row[3]) for row in plan)
            return super().execute(sql, parameters)
    def connect(path, **kwargs):
        return original(path, factory=Traced, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    knowledge[3].search("推测解码 decoding")
    assert any("FROM chunks_fts" in sql for sql in statements)
    assert any("FROM papers WHERE paper_dir" in sql for sql in statements)
