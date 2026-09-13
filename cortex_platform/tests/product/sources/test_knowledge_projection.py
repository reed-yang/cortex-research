"""Exercise the real public API against complete private evidence and old cursors."""

import base64
import hashlib
from urllib.parse import urlencode

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.sources.reader import _cursor
from cortex_platform.tests.product.api.test_sources import TOKEN, _headers
from cortex_platform.tests.product.sources import (
    test_knowledge_reader as reader_fixtures,
)
from cortex_platform.tests.product.sources.test_adoption_reader import _write

corpus = reader_fixtures.corpus
database = reader_fixtures.database
knowledge = reader_fixtures.knowledge


@pytest.fixture
def public(knowledge):
    store, root, database, reader = knowledge
    source_id = store.list_sources()[0]["id"]
    api = ControlAPI(store, access_token=TOKEN)

    def get(params=None, *, search=None):
        path = "search" if search is not None else source_id + "/content"
        params = {"q": search} if search is not None else params or {}
        return api.handle(method="GET", target="/api/v1/sources/" + path + "?" + urlencode(params),
                          headers=_headers())

    return get, root / "20260906-English" / "notes.md", reader, source_id, database


@pytest.mark.parametrize("limit,raw", [
    (1, b"Before\napi_key=REVIEW_SYNTHETIC_VALUE\nAfter\n"),
    (7, b"token=REVIEW_SYNTHETIC_VALUE\nSafe next line"),
    (20_000, b"a" * 19_995 + b" api_key=REVIEW_SYNTHETIC_VALUE\nAfter\n"),
    (20_000, b"REVIEW_SYNTHETIC_VALUE " + b"a" * 20_000 + b" secret=hidden\nAfter\n"),
])
def test_public_pages_never_expose_any_sensitive_line_fragment(public, limit, raw):
    get, path, reader, source_id, _ = public
    path.write_bytes(raw)
    sensitive = {i + 1 for i, line in enumerate(raw.decode().split("\n")) if "=" in line}
    cursor = None
    previous_offset = 0
    collected = []
    while True:
        response = get({"limit": limit, **({"cursor": cursor} if cursor else {})})
        assert response.status == 200, response.payload
        page = response.payload
        internal = reader.read(source_id, limit=limit, cursor=cursor)
        assert page.keys() == internal.keys()
        assert {k: v for k, v in page.items() if k != "text"} == {k: v for k, v in internal.items() if k != "text"}
        assert len(page["text"].encode()) <= limit
        assert page["text"].count("\n") == internal["text"].count("\n")
        for offset, fragment in enumerate(page["text"].split("\n")):
            if page["start_line"] + offset in sensitive:
                assert fragment == "[redacted]" or set(fragment) <= {"*"}
        collected.append(page["text"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        position = int.from_bytes(base64.urlsafe_b64decode(cursor + "==")[1:9], "big")
        assert previous_offset < position < len(raw)
        previous_offset = position
    assert "REVIEW_SYNTHETIC_VALUE" not in "".join(collected)
    assert page["content_sha256"] == hashlib.sha256(raw).hexdigest()


def test_old_and_forged_cursors_inside_sensitive_line_remain_redacted(public):
    get, path, reader, source_id, _ = public
    raw = b"Safe\napi_key=REVIEW_SYNTHETIC_VALUE\nAfter\n"
    path.write_bytes(raw)
    old_cursor = reader.read(source_id, limit=13)["next_cursor"]
    binding = base64.urlsafe_b64decode(old_cursor + "==")[9:41]
    for position in [5, 8, 12, 13, 22, 31]:
        cursor = _cursor(position, binding)
        for limit in [1, 7, 20_000]:
            response = get({"cursor": cursor, "limit": limit})
            assert response.status == 200
            fragment = response.payload["text"].split("\n")[0]
            assert fragment == "[redacted]" or set(fragment) <= {"*"}
    assert get({"cursor": old_cursor}).status == 200
    path.write_bytes(raw + b"Changed")
    assert get({"cursor": old_cursor}).status == 409


def test_public_utf8_pages_keep_original_boundaries_and_coordinates(public):
    get, path, reader, source_id, _ = public
    raw = "中🙂e\u0301\r\napi_key=虚构值🙂\n末行\n".encode()
    path.write_bytes(raw)
    for limit in [4, 7, 13, 20_000]:
        cursor = None
        while True:
            response = get({"limit": limit, **({"cursor": cursor} if cursor else {})})
            assert response.status == 200
            page = response.payload
            internal = reader.read(source_id, cursor=cursor, limit=limit)
            assert len(page["text"].encode()) <= limit
            assert page["start_line"] == internal["start_line"]
            assert page["end_line"] == internal["end_line"]
            assert page["next_cursor"] == internal["next_cursor"]
            if page["start_line"] == page["end_line"] != 2:
                assert page["text"] == internal["text"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert get({"limit": 1}).status == 400
    first = reader.read(source_id, limit=3)["next_cursor"]
    binding = base64.urlsafe_b64decode(first + "==")[9:41]
    assert get({"cursor": _cursor(4, binding)}).status == 400


@pytest.mark.parametrize("text", [
    "decoding REVIEW_SYNTHETIC_VALUE " + "a" * 2_000 + " api_key=hidden\nSafe evidence",
    "decoding " + "a" * 1_987 + " api_key=REVIEW_SYNTHETIC_VALUE\nSafe evidence",
])
def test_search_classifies_complete_indexed_chunk_before_excerpt_truncation(public, text):
    get, path, reader, source_id, database = public
    _write(database, "UPDATE chunks SET text = ? WHERE paper_dir = ?", (text, "20260906-English"))
    before = next(h for h in reader.search("decoding")["results"] if h["source_id"] == source_id)
    result = get(search="decoding")
    assert result.status == 200
    hit = next(h for h in result.payload["results"] if h["source_id"] == source_id)
    assert hit["excerpt"] == "[redacted]\nSafe evidence"
    assert hit["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert hit["evidence_id"] == before["evidence_id"]
    path.write_text("Different current document", encoding="utf-8")
    assert get(search="decoding").payload == result.payload
    assert before["excerpt"] != hit["excerpt"]


def test_unicode_title_and_section_are_classified_before_truncation(public):
    get, _, _, source_id, database = public
    title = "机器人 REVIEW_SYNTHETIC_VALUE " + "x" * 2_000 + " secret=hidden"
    section = "REVIEW_SYNTHETIC_SECTION " + "x" * 1_000 + " api_key=hidden"
    _write(database, "UPDATE papers SET title = ? WHERE paper_dir = ?", (title, "20260906-English"))
    _write(database, "UPDATE chunks SET section = ? WHERE paper_dir = ?", (section, "20260906-English"))
    result = get(search="机器人")
    assert result.status == 200
    hit = next(h for h in result.payload["results"] if h["source_id"] == source_id)
    assert hit["excerpt"] == "[redacted]"
    assert hit["content_sha256"] == hashlib.sha256(title.encode()).hexdigest()
    result = get(search="decoding")
    assert result.status == 200
    hit = next(h for h in result.payload["results"] if h["source_id"] == source_id)
    assert hit["section"] == "[redacted]"
