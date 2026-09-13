"""K1 HTTP boundary tests; the only reader double is installed by the test."""
from __future__ import annotations

import json
import sys
from types import ModuleType
from urllib.parse import urlencode

import pytest

from .test_sources import _api, _headers

MODULE = "cortex_platform.product.sources.reader"


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    api, _ = _api(tmp_path)
    module = ModuleType(MODULE)

    class SourceContentUnavailable(RuntimeError):
        pass

    class SourceQueryInvalid(ValueError):
        pass

    calls = []

    class Reader:
        def __init__(self, store, *, redact_line):
            assert store is api.store
            assert redact_line("/Users/private/paper")
            assert not redact_line("Safe evidence")

        def read(self, source_id, kind="notes", cursor=None, limit=20000):
            calls.append((source_id, kind, cursor, limit))
            if source_id == "missing":
                raise SourceContentUnavailable("/Users/private/papers/notes.md")
            if cursor == "bad":
                raise SourceQueryInvalid("/tmp/private/cursor")
            return {"source_id": source_id, "canonical_id": "arxiv:2401.12345", "kind": kind,
                        "text": f"{kind}\n[redacted]\nSafe evidence", "content_sha256": "a" * 64,
                        "start_line": 1, "end_line": 3, "next_cursor": "next", "private_path": "/tmp/root"}

        def search(self, query, limit=10):
            calls.append((query, limit))
            return {"query": query, "retrieval_mode": "fts5_or", "results": [{
                "source_id": "source_1", "canonical_id": "arxiv:2401.12345", "title": "A paper",
                "evidence_id": "source:source_1:chunk:1", "section": "Method", "excerpt": "Evidence\n/home/private/file",
                "content_sha256": "b" * 64, "private_path": "/tmp/root"}]}

    module.SourceKnowledgeReader = Reader
    module.SourceContentUnavailable = SourceContentUnavailable
    module.SourceQueryInvalid = SourceQueryInvalid
    monkeypatch.setitem(sys.modules, MODULE, module)
    return api, calls


def get(api, target, **kwargs):
    return api.handle(method="GET", target=target, headers=kwargs.pop("headers", _headers()), **kwargs)


@pytest.mark.parametrize("kind", ["notes", "full_text", "grounding"])
def test_read_frozen_interface_and_redacted_line_references(knowledge, kind):
    api, calls = knowledge
    response = get(api, f"/api/v1/sources/source_1/content?kind={kind}&limit=100&cursor=next")
    assert response.status == 200
    assert calls == [("source_1", kind, "next", 100)]
    assert response.payload["text"] == f"{kind}\n[redacted]\nSafe evidence"
    assert response.payload["start_line"] == 1 and response.payload["end_line"] == 3
    assert response.payload["content_sha256"] == "a" * 64
    assert response.headers == (("Cache-Control", "no-store"),)
    assert set(response.payload) == {"source_id", "canonical_id", "kind", "text", "content_sha256", "start_line", "end_line", "next_cursor"}
    assert "/Users/" not in json.dumps(response.payload)


@pytest.mark.parametrize("query", ["memory", "记忆", "a" * 1024])
def test_search_precedes_id_route_and_projects_only_contract_fields(knowledge, query):
    api, calls = knowledge
    response = get(api, "/api/v1/sources/search?" + urlencode({"q": query, "limit": 5}))
    assert response.status == 200
    assert calls == [(query, 5)]
    assert response.payload["results"][0]["excerpt"] == "Evidence\n[redacted]"
    assert "private" not in json.dumps(response.payload)
    assert "engine_ref" not in json.dumps(response.payload)


@pytest.mark.parametrize("suffix", [
    "search", "search?q=", "search?q=%20", "search?q=a&q=b", "search?q=memory&limit=0",
    "search?q=memory&limit=01", "search?q=memory&limit=51", "search?q=memory&limit=x", "search?q=memory&limit=1&limit=2",
    "search?q=" + "a" * 1025, "search?q=" + "%E4%B8%AD" * 342,
    "search?q=memory%00", "search?q=memory&path=/tmp/private", "source_1/content?path=/tmp/private",
    "source_1/content?kind=pdf", "source_1/content?kind=", "source_1/content?kind=notes&kind=grounding",
    "source_1/content?cursor=", "source_1/content?cursor=/tmp/private", "source_1/content?cursor=bad",
    "source_1/content?cursor=a&cursor=b", "source_1/content?limit=0", "source_1/content?limit=20001",
])
def test_query_and_cursor_errors_are_stable_without_paths(knowledge, suffix):
    api, _ = knowledge
    response = get(api, "/api/v1/sources/" + suffix)
    assert response.status == 400
    assert response.payload["category"] in {"source_query_invalid", "invalid_request"}
    assert "/tmp/" not in json.dumps(response.payload)


@pytest.mark.parametrize("suffix", ["search?q=memory", "source_1/content"])
@pytest.mark.parametrize("boundary", ["token", "origin", "host"])
def test_all_knowledge_requests_authenticate_before_reading(knowledge, suffix, boundary):
    api, calls = knowledge
    kwargs = {"headers": {}} if boundary == "token" else {"headers": {**_headers(), "Origin": "https://evil.test"}} if boundary == "origin" else {"client_host": "192.0.2.1"}
    assert get(api, "/api/v1/sources/" + suffix, **kwargs).status == 403
    assert calls == []


def test_missing_content_has_a_safe_recoverable_category(knowledge):
    api, _ = knowledge
    response = get(api, "/api/v1/sources/missing/content")
    assert response.status == 409
    assert response.payload["category"] == "source_content_unavailable"
    assert "/Users/" not in json.dumps(response.payload)


def test_absent_k1_does_not_break_existing_routes(tmp_path, monkeypatch):
    import builtins
    api, _ = _api(tmp_path)
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "sources.reader":
            raise ModuleNotFoundError("unavailable", name=MODULE)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    response = get(api, "/api/v1/sources/source_1/content")
    assert response.status == 503
    assert response.payload["category"] == "source_content_unavailable"
    assert get(api, "/api/v1/sources").status == 200


def test_real_k1_stored_documents_search_and_cursor(tmp_path):
    """Runs when K1 is integrated; parallel rehearsal can extend sources.__path__."""
    pytest.importorskip(MODULE)
    from cortex_research import db as research_db

    from cortex_platform.product.api import ControlAPI
    from cortex_platform.product.control import ControlStore
    from cortex_platform.product.sources.adoption import read_corpus
    from cortex_platform.tests.product.sources.test_adoption_reader import (
        _add_paper,
        _write,
    )

    from .test_sources import TOKEN

    root = tmp_path / "corpus"
    root.mkdir()
    database = tmp_path / "research.db"
    conn = research_db.connect(database)
    research_db.apply_schema(conn)
    conn.commit()
    conn.close()
    _add_paper(database, root, paper_dir="paper-memory", title="Memory 记忆", arxiv_id="2401.12345", body="Full text\nEvidence\n")
    _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
           ("paper-memory", "Method", 0, "Memory evidence from stored papers"))
    directory = root / "paper-memory"
    (directory / "notes.md").write_text("Notes\nEvidence\n", encoding="utf-8")
    (directory / "grounding.md").write_text("Grounding\nClaim\n", encoding="utf-8")
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    store.register_asset_root(root_id="research-corpus", private_path=root, max_bytes=1 << 30,
                              enabled=True, actor_id="fixture", idempotency_key="knowledge-root-0001")
    store.commit_adoption_manifest(manifest=read_corpus(database=database, corpus_root=root).manifest,
                                   corpus_root_id="research-corpus", actor_id="fixture", idempotency_key="knowledge-adopt-001")
    source_id = store.list_sources()[0]["id"]
    api = ControlAPI(store, access_token=TOKEN)
    before = store.path.read_bytes()
    for kind, expected in [("notes", "Notes\nEvidence\n"), ("full_text", "Full text\nEvidence\n"), ("grounding", "Grounding\nClaim\n")]:
        cursor = None
        text = ""
        while True:
            response = get(api, f"/api/v1/sources/{source_id}/content?" + urlencode({"kind": kind, "limit": 7, **({"cursor": cursor} if cursor else {})}))
            assert response.status == 200, response.payload
            text += response.payload["text"]
            cursor = response.payload["next_cursor"]
            if cursor is None:
                break
        assert text == expected
    assert get(api, f"/api/v1/sources/{source_id}/content?cursor=bad").status == 400
    missing = get(api, "/api/v1/sources/missing/content")
    assert missing.status == 409
    (directory / "grounding.md").unlink()
    assert get(api, f"/api/v1/sources/{source_id}/content?kind=grounding").status == 409
    search = get(api, "/api/v1/sources/search?" + urlencode({"q": "记忆"}))
    assert search.status == 200, search.payload
    assert search.payload["retrieval_mode"] == "unicode_title_fallback"
    assert search.payload["results"][0]["source_id"] == source_id
    english = get(api, "/api/v1/sources/search?q=memory")
    assert english.status == 200, english.payload
    assert english.payload["retrieval_mode"] == "fts5_or"
    assert english.payload["results"][0]["excerpt"] == "Memory evidence from stored papers"
    assert str(tmp_path) not in json.dumps(search.payload)
    assert store.path.read_bytes() == before
