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


def test_short_model_suffixes_and_versions_remain_searchable(knowledge):
    from cortex_platform.product.sources.search import _terms
    assert _terms("LingBot VA 2.0") == (["lingbot", "va", "2.0"], [])
    _, _, database, reader = knowledge
    _write(database, "UPDATE chunks SET text = ? WHERE id = 1", ("LingBot VA 2.0 model",))
    for query in ("VA", "2.0", "LingBot VA 2.0"):
        result = reader.search(query)
        assert any(":chunk:1:" in hit["evidence_id"] for hit in result["results"])
    assert reader.search("3.0")["results"] == []


ENGLISH, CHINESE = "20260906-English", "20260906-中文论文"


def _chunks(database, paper_dir, texts, section="Method"):
    for index, text in enumerate(texts, 1):
        _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
               (paper_dir, section, index, text))


def _ids(store):
    by_canonical = {source["canonical_id"]: source["id"] for source in store.list_sources()}
    return {ENGLISH: by_canonical["arxiv:2609.00001"], CHINESE: by_canonical["arxiv:2609.00002"]}


def _chunk_id(hit):
    return int(hit["evidence_id"].split(":chunk:")[1].split(":")[0])


def test_per_source_returns_distinct_papers_with_contiguous_chunks(knowledge):
    store, _, database, reader = knowledge
    ids = _ids(store)
    _chunks(database, ENGLISH, [f"decoding decoding decoding variant {name}"
                                for name in ("one", "two", "three", "four", "five")])
    chunk_level = reader.search("decoding", limit=2)
    assert [hit["source_id"] for hit in chunk_level["results"]] == [ids[ENGLISH]] * 2
    paper_level = reader.search("decoding", limit=2, per_source=2)
    assert paper_level.keys() == chunk_level.keys()
    assert paper_level["retrieval_mode"] == "fts5_or"
    assert [hit["source_id"] for hit in paper_level["results"]] == [ids[ENGLISH]] * 2 + [ids[CHINESE]]
    assert paper_level["results"][:2] == chunk_level["results"]
    assert all(hit.keys() == chunk_level["results"][0].keys() for hit in paper_level["results"])


def test_per_source_caps_chunks_and_skips_duplicate_content(knowledge):
    store, _, database, reader = knowledge
    ids = _ids(store)
    _chunks(database, ENGLISH, ["decoding decoding decoding", "decoding decoding decoding",
                                "decoding decoding cache"])
    single = reader.search("decoding", limit=2, per_source=1)["results"]
    assert [hit["source_id"] for hit in single] == [ids[ENGLISH], ids[CHINESE]]
    english = [hit for hit in reader.search("decoding", limit=2, per_source=2)["results"]
               if hit["source_id"] == ids[ENGLISH]]
    # The second identical text does not take the second slot.
    assert [_chunk_id(hit) for hit in english] == [4, 6]
    assert len({hit["content_sha256"] for hit in english}) == 2


def test_per_source_selection_stays_inside_bounded_candidates(knowledge, monkeypatch):
    from cortex_platform.product.sources import search

    store, _, database, reader = knowledge
    ids = _ids(store)
    _chunks(database, ENGLISH, ["decoding decoding decoding a1", "decoding decoding decoding b2",
                                "decoding decoding decoding c3"])
    monkeypatch.setattr(search, "MAX_CANDIDATES", 3)
    results = reader.search("decoding", limit=6, per_source=2)["results"]
    assert [hit["source_id"] for hit in results] == [ids[ENGLISH]] * 2


@pytest.mark.parametrize("per_source", [0, 5, True, "2", 1.5, -1])
def test_per_source_validation(knowledge, per_source):
    with pytest.raises(SourceQueryInvalid):
        knowledge[3].search("decoding", per_source=per_source)


def test_per_source_none_is_the_default_chunk_level_call(knowledge):
    reader = knowledge[3]
    for query in ("decoding", "推测解码", "推测解码 decoding"):
        assert reader.search(query, limit=1, per_source=None) == reader.search(query, limit=1)


def test_mixed_query_selects_title_sources_first_and_keeps_their_low_ranked_chunks(knowledge):
    store, _, database, reader = knowledge
    ids = _ids(store)
    # English chunks outrank the only Chinese-paper chunk, which still lies
    # inside the bounded candidates.
    _chunks(database, ENGLISH, ["decoding decoding decoding x1", "decoding decoding decoding y2",
                                "decoding decoding decoding z3"])
    one = reader.search("推测解码 decoding", limit=1, per_source=2)
    assert one["retrieval_mode"] == "fts5_or+unicode_title_fallback"
    assert [(hit["source_id"], hit["section"]) for hit in one["results"]] == [
        (ids[CHINESE], "__title__"), (ids[CHINESE], "Method")]
    assert _chunk_id(one["results"][1]) == 2
    two = reader.search("推测解码 decoding", limit=2, per_source=2)["results"]
    assert two[:2] == one["results"]
    assert [hit["source_id"] for hit in two[2:]] == [ids[ENGLISH]] * 2
    assert [_chunk_id(hit) for hit in two[2:]] == [4, 5]


def test_per_source_keeps_adoption_directory_and_byte_limit_guards(knowledge, tmp_path):
    store, root, database, reader = knowledge
    ids = _ids(store)
    both = {ids[ENGLISH], ids[CHINESE]}
    assert {hit["source_id"] for hit in reader.search("speculative", limit=6, per_source=2)["results"]} == both
    store.register_asset_root(root_id="other-corpus", private_path=tmp_path / "other",
                              max_bytes=1 << 30, enabled=True, actor_id="operator",
                              idempotency_key="other-root-00002")
    store.commit_adoption_manifest(manifest=build_manifest([AdoptionEntry(
        paper_dir="unadopted", authority="arxiv", authority_id="2609.99999",
        official_title="机器人 Unadopted", content_digest="a" * 64,
    )]), corpus_root_id="other-corpus", actor_id="operator", idempotency_key="other-adopt-00002")
    assert {hit["source_id"] for hit in reader.search("speculative", limit=6, per_source=2)["results"]} == both
    # A long chunk with one weak match ranks last; it is refused only when attached.
    _chunks(database, ENGLISH, ["decoding decoding decoding strong", "speculative " * 10000 + "decoding"])
    assert [hit["source_id"] for hit in reader.search("decoding", limit=2, per_source=1)["results"]] == [
        ids[ENGLISH], ids[CHINESE]]
    with pytest.raises(SourceContentUnavailable, match="byte limit"):
        reader.search("decoding", limit=2, per_source=3)
    (root / ENGLISH).rename(root / "removed")
    assert [hit["source_id"] for hit in reader.search("decoding", limit=2, per_source=3)["results"]] == [
        ids[CHINESE]]


@pytest.mark.parametrize("query", ["decoding", "推测解码 decoding"])
def test_per_source_issues_the_same_single_fts_statement(knowledge, monkeypatch, query):
    original = sqlite3.connect
    statements = []
    class Traced(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            assert "embedding" not in sql.lower()
            statements.append((sql, tuple(parameters)))
            return super().execute(sql, parameters)
    def connect(path, **kwargs):
        return original(path, factory=Traced, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    knowledge[3].search(query, limit=2)
    chunk_level = [statement for statement in statements if "FROM chunks_fts" in statement[0]]
    statements.clear()
    knowledge[3].search(query, limit=2, per_source=2)
    paper_level = [statement for statement in statements if "FROM chunks_fts" in statement[0]]
    assert len(chunk_level) == 1
    assert paper_level == chunk_level


@pytest.mark.parametrize("query", [
    "LingBot VA 2.0", "the and OR", "", "x\0y", "推测解码 decoding", "a b c",
    " ".join(f"term{i}" for i in range(40)),
])
def test_query_terms_never_raises_and_matches_terms(query):
    from cortex_platform.product.sources.search import MAX_QUERY_TERMS, _terms, query_terms

    english, unicode = query_terms(query)
    try:
        expected = _terms(query)
    except SourceQueryInvalid:
        assert not (english or unicode) or len(english) + len(unicode) > MAX_QUERY_TERMS
    else:
        assert (english, unicode) == expected
