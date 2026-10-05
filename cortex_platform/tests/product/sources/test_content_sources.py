"""Blog and XHS note sources: identity, registration, and reads through content bindings.

Every file here is generated: invented captions and transcriptions, tiny images
built in code. Papers keep their adoption join; these tests add the non-paper
cases beside the paper fixtures rather than relaxing them.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.sources.identity import (
    blog_url_identity,
    normalize_url,
    parse_arxiv_capture_payload,
    xhs_note_permalink,
)
from cortex_platform.product.sources.reader import (
    SourceAssetInvalid,
    SourceAssetTooLarge,
    SourceAssetUnavailable,
    SourceContentUnavailable,
    SourceKnowledgeReader,
)
from cortex_platform.tests.product.api.test_sources import TOKEN, _headers
from cortex_platform.tests.product.sources.test_adoption_reader import (  # noqa: F401
    corpus,
    database,
)
from cortex_platform.tests.product.sources.test_knowledge_document import png
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge  # noqa: F401

NOTE = "66f1a2b3c4d5e6f708192a3b"
OTHER_NOTE = "66f1a2b3c4d5e6f708192a3c"
BLOG_URL = "https://Blog.Example:443/posts/Speculative-Decoding/?ref=xhs#comments"
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00\x2f\x00\x00\x00"
NOTE_MD = "# 本周论文 weekly papers\n\nBlogger: 研究笔记 Lab\n\n1. Speculative decoding for robots\n"
TRANSCRIPTION_MD = "## Image 1\n\narXiv:2609.01234 Speculative Decoding\n\n## Image 2\n\nOCR failed: url_expired\n"


# -- identity ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (BLOG_URL, "https://blog.example/posts/Speculative-Decoding/?ref=xhs"),
        ("HTTP://Example.COM:80", "http://example.com/"),
        ("https://example.com:8443/A/b", "https://example.com:8443/A/b"),
        ("https://example.com/post", "https://example.com/post"),
        ("https://example.com/post/", "https://example.com/post/"),
        ("https://example.com?q=Case", "https://example.com/?q=Case"),
        ("https://example.com/%E4%B8%AD/", "https://example.com/%E4%B8%AD/"),
        ("http://[2001:DB8::1]:8080/x", "http://[2001:db8::1]:8080/x"),
        ("  https://example.com/trim  ", "https://example.com/trim"),
    ],
)
def test_url_normalization_is_conservative(value: str, expected: str) -> None:
    assert normalize_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https://@example.com/",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "file:///etc/hosts",
        "https:///posts/1",
        "https://",
        "example.com/post",
        "https://example.com/a b",
        "https://例子.com/",
        "https://example.com:99999/",
        "https://example.com:port/",
        "",
        None,
    ],
)
def test_url_normalization_refuses_unsafe_or_hostless_urls(value) -> None:
    with pytest.raises(ValueError):
        normalize_url(value)


def test_blog_identity_is_the_digest_of_the_normalized_url() -> None:
    normalized, digest = blog_url_identity(BLOG_URL)
    assert digest == hashlib.sha256(normalized.encode()).hexdigest()
    assert blog_url_identity("https://blog.example/posts/Speculative-Decoding/?ref=xhs")[1] == digest
    assert blog_url_identity("https://blog.example/posts/speculative-decoding/?ref=xhs")[1] != digest
    assert blog_url_identity("https://blog.example/posts/Speculative-Decoding?ref=xhs")[1] != digest
    assert xhs_note_permalink(NOTE.upper()) == f"https://www.xiaohongshu.com/explore/{NOTE}"


def test_the_arxiv_capture_parser_still_refuses_blog_and_note_links() -> None:
    for payload in (BLOG_URL, xhs_note_permalink(NOTE)):
        with pytest.raises(ValueError, match="names no arXiv paper"):
            parse_arxiv_capture_payload(payload)


# -- registration --------------------------------------------------------------------


def _register(store, key: str, **overrides):
    _, digest = blog_url_identity(BLOG_URL)
    request = {
        "authority": "url",
        "authority_id": digest,
        "source_kind": "blog",
        "official_title": "Speculative Decoding, explained",
        "engine_ref": f"blog:{digest}",
        "aliases": (),
    }
    request.update(overrides)
    return store.register_source(actor_id="fixture", idempotency_key=key, **request)


def test_each_kind_registers_only_with_its_own_authority_and_engine_ref(knowledge) -> None:
    store = knowledge[0]
    _, digest = blog_url_identity(BLOG_URL)
    blog = _register(store, "register-blog-00001").value
    assert (blog["canonical_id"], blog["source_kind"]) == (f"url:{digest}", "blog")
    note = _register(
        store, "register-note-00001", authority="xhs", authority_id=NOTE.upper(),
        source_kind="xhs_note", engine_ref=f"xhs-note:{NOTE}",
        official_title="本周论文 weekly papers",
        aliases=({"authority": "xhs", "value": NOTE},),
    ).value
    assert note["canonical_id"] == f"xhs:{NOTE}"
    refused = [
        {"source_kind": "paper"},
        {"engine_ref": f"paper:{digest}"},
        {"engine_ref": f"blog:{'0' * 64}"},
        {"authority": "xhs", "authority_id": NOTE, "engine_ref": f"xhs-note:{NOTE}"},
        {"authority": "arxiv", "authority_id": "2609.01234", "engine_ref": "paper:x"},
        {"authority": "url", "authority_id": BLOG_URL},
        {"authority": "xhs", "authority_id": NOTE[:8], "source_kind": "xhs_note",
         "engine_ref": f"xhs-note:{NOTE[:8]}"},
    ]
    for index, overrides in enumerate(refused):
        with pytest.raises(ValueError):
            _register(store, f"register-refused-{index:04d}", **overrides)


def test_xhs_and_url_aliases_are_validated_and_projected(knowledge) -> None:
    store = knowledge[0]
    _, digest = blog_url_identity(BLOG_URL)
    blog = _register(
        store, "register-blog-00002", aliases=({"authority": "url", "value": digest},),
    ).value
    for alias in ({"authority": "url", "value": BLOG_URL},
                  {"authority": "xhs", "value": NOTE[:8]},
                  {"authority": "url", "value": "g" * 64}):
        with pytest.raises(ValueError):
            _register(store, "register-alias-0001", aliases=(alias,))
    projected = ControlAPI._public_source_aliases(
        blog["aliases"] + [
            {"id": "alias-x", "authority": "xhs", "value": NOTE,
             "created_at": "2026-09-01T12:00:00Z"},
            {"id": "alias-u", "authority": "url", "value": BLOG_URL,
             "created_at": "2026-09-01T12:00:00Z"},
        ]
    )
    assert [(item["authority"], item["value"]) for item in projected] == [
        ("url", digest), ("xhs", NOTE),
    ]


# -- reading --------------------------------------------------------------------------


def _write_version(root: Path, directory: str, files: dict[str, bytes]) -> str:
    base = root / directory
    for name, data in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()


@pytest.fixture
def library(knowledge, tmp_path):  # noqa: F811
    """The paper corpus fixture, plus one XHS note and one blog in their own roots."""

    store, corpus_root, _, reader = knowledge
    roots = {}
    for root_id in ("xhs-notes", "blogs"):
        roots[root_id] = tmp_path / root_id
        roots[root_id].mkdir()
        store.register_asset_root(
            root_id=root_id, private_path=roots[root_id], max_bytes=1 << 20,
            enabled=True, actor_id="operator", idempotency_key=f"root-{root_id}-content-0001",
        )
    image = png()
    asset = f"1-{hashlib.sha256(image).hexdigest()[:12]}.png"
    tree = _write_version(roots["xhs-notes"], f"{NOTE}/v1", {
        "note.md": NOTE_MD.encode(),
        "transcription.md": TRANSCRIPTION_MD.encode(),
        f"assets/{asset}": image,
        "raw/detail.json": b'{"private": true}',
    })
    note = store.bind_content_source_version(
        source_kind="xhs_note", authority_id=NOTE, official_title="本周论文 weekly papers",
        version=1, tree_sha256=tree, metadata={"permalink": xhs_note_permalink(NOTE)},
    )["source"]
    normalized, digest = blog_url_identity(BLOG_URL)
    tree = _write_version(roots["blogs"], f"{digest[:16]}/v1", {
        "article.md": b"# Speculative Decoding, explained\n\nBody text.\n",
        "notes.md": "Recommended in 本周论文 · image 1\n".encode(),
        "assets/1-aaaaaaaaaaaa.webp": WEBP,
    })
    blog = store.bind_content_source_version(
        source_kind="blog", authority_id=digest, official_title="Speculative Decoding, explained",
        version=1, tree_sha256=tree, metadata={"normalized_url": normalized},
    )["source"]
    return store, reader, roots, note, blog, asset


@pytest.mark.parametrize(
    ("which", "kind", "expected"),
    [
        ("note", "notes", NOTE_MD),
        ("note", "full_text", TRANSCRIPTION_MD),
        ("blog", "notes", "Recommended in 本周论文 · image 1\n"),
        ("blog", "full_text", "# Speculative Decoding, explained\n\nBody text.\n"),
    ],
)
def test_each_kind_maps_onto_its_own_files(library, which, kind, expected) -> None:
    store, reader, roots, note, blog, _ = library
    source = note if which == "note" else blog
    page = reader.read(source["id"], kind=kind)
    document = reader.document(source["id"], kind=kind)
    assert page["text"] == document["text"] == expected
    assert page["canonical_id"] == document["canonical_id"] == source["canonical_id"]
    assert document["content_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert str(roots["xhs-notes"]) not in str(document)
    with pytest.raises(SourceContentUnavailable):
        reader.document(source["id"], kind="grounding")
    with pytest.raises(SourceContentUnavailable):
        reader.read(source["id"], kind="grounding")


def test_the_reader_uses_the_highest_version(library) -> None:
    store, reader, roots, note, _, _ = library
    tree = _write_version(roots["xhs-notes"], f"{NOTE}/v2", {
        "note.md": b"# Version two\n", "transcription.md": b"## Image 1\n\nretried\n",
    })
    # Written but not yet bound: still version one.
    assert reader.document(note["id"])["text"] == NOTE_MD
    store.bind_content_source_version(
        source_kind="xhs_note", authority_id=NOTE, official_title="本周论文 weekly papers",
        version=2, tree_sha256=tree, metadata={},
    )
    assert reader.document(note["id"])["text"] == "# Version two\n"
    assert reader.read(note["id"], kind="full_text")["text"] == "## Image 1\n\nretried\n"


def test_assets_come_only_from_the_bound_directory(library) -> None:
    store, reader, roots, note, blog, asset = library
    assert reader.asset(note["id"], f"assets/{asset}") == ("image/png", png())
    assert reader.asset(note["id"], f"./assets/{asset}")[0] == "image/png"
    assert reader.asset(blog["id"], "assets/1-aaaaaaaaaaaa.webp") == ("image/webp", WEBP)
    # The other source's image is not reachable from this one.
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(blog["id"], f"assets/{asset}")
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(note["id"], f"papers/{NOTE}/assets/{asset}")
    for path in (f"../{NOTE}/v1/assets/{asset}", "raw/detail.json", f"assets/../../{asset}"):
        with pytest.raises(SourceAssetInvalid):
            reader.asset(note["id"], path)
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(note["id"], "assets/missing.png")


def test_a_note_screenshot_is_served_up_to_the_download_bound(library) -> None:
    store, reader, roots, note, _, _ = library
    root = store.get_asset_root("xhs-notes")
    store.update_asset_root(
        root_id="xhs-notes", private_path=root.private_path, max_bytes=20 * 1024 * 1024,
        enabled=True, expected_revision=root.revision, actor_id="operator",
        idempotency_key="root-notes-larger-0001",
    )
    large = png() + b"\0" * (9 * 1024 * 1024)
    (roots["xhs-notes"] / NOTE / "v1" / "assets" / "2-bbbbbbbbbbbb.png").write_bytes(large)
    assert reader.asset(note["id"], "assets/2-bbbbbbbbbbbb.png") == ("image/png", large)
    (roots["xhs-notes"] / NOTE / "v1" / "assets" / "3-cccccccccccc.png").write_bytes(
        large + b"\0" * (12 * 1024 * 1024)
    )
    with pytest.raises(SourceAssetTooLarge):
        reader.asset(note["id"], "assets/3-cccccccccccc.png")


def test_a_binding_that_names_another_sources_directory_is_refused(library) -> None:
    store, reader, roots, note, _, asset = library
    _write_version(roots["xhs-notes"], f"{OTHER_NOTE}/v1", {
        "note.md": b"another note\n", f"assets/{asset}": png(),
    })
    connection = sqlite3.connect(store.path)
    try:
        # Raw SQL, as a corrupted or hostile writer would: the schema admits
        # the row, and the reader refuses it because the directory is not the
        # one this source's identity and version name.
        connection.execute(
            """INSERT INTO source_content_bindings
               (source_id, version, root_id, directory, tree_sha256, metadata_json, created_at)
               VALUES (?, 2, 'xhs-notes', ?, ?, '{}', 'x')""",
            (note["id"], f"{OTHER_NOTE}/v1", "e" * 64),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SourceContentUnavailable):
        reader.document(note["id"])
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(note["id"], f"assets/{asset}")


def test_a_disabled_root_or_an_unbound_source_reads_nothing(library) -> None:
    store, reader, roots, note, blog, _ = library
    unbound = store.register_source(
        authority="xhs", authority_id=OTHER_NOTE, source_kind="xhs_note",
        official_title="Unbound", engine_ref=f"xhs-note:{OTHER_NOTE}", aliases=(),
        actor_id="fixture", idempotency_key="register-unbound-0001",
    ).value
    with pytest.raises(SourceContentUnavailable):
        reader.document(unbound["id"])
    root = store.get_asset_root("xhs-notes")
    store.update_asset_root(
        root_id=root.root_id, private_path=root.private_path, max_bytes=root.max_bytes,
        enabled=False, expected_revision=root.revision, actor_id="operator",
        idempotency_key="disable-xhs-notes-01",
    )
    with pytest.raises(SourceContentUnavailable):
        reader.document(note["id"])
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(note["id"], "assets/anything.png")
    assert reader.document(blog["id"], kind="full_text")["kind"] == "full_text"


def test_search_stays_papers_only(library) -> None:
    store, reader, _, note, blog, _ = library
    result = reader.search("speculative decoding")
    found = {item["source_id"] for item in result["results"]}
    assert found and not found & {note["id"], blog["id"]}
    papers = {s["id"] for s in store.list_sources() if s["source_kind"] == "paper"}
    assert found <= papers


# -- routes --------------------------------------------------------------------------


def _get(api, target):
    return api.handle(method="GET", target=target, headers=_headers())


def test_document_and_asset_routes_read_both_kinds(library) -> None:
    store, _, _, note, blog, asset = library
    api = ControlAPI(store, access_token=TOKEN, allowed_origins=frozenset({"https://cortex.test"}))
    for source, kind, text in ((note, "full_text", TRANSCRIPTION_MD),
                               (blog, "notes", "Recommended in 本周论文 · image 1\n")):
        response = _get(api, f"/api/v1/sources/{source['id']}/document?kind={kind}")
        assert response.status == 200, response.payload
        assert response.payload["text"] == text
        assert response.headers == (("Cache-Control", "no-store"),)
        grounding = _get(api, f"/api/v1/sources/{source['id']}/document?kind=grounding")
        assert grounding.status == 409
        assert grounding.payload["category"] == "source_content_unavailable"
    image = _get(api, f"/api/v1/sources/{note['id']}/asset?path=assets/{asset}")
    assert (image.status, image.content_type, image.body) == (200, "image/png", png())
    crossed = _get(api, f"/api/v1/sources/{blog['id']}/asset?path=assets/{asset}")
    assert crossed.status == 404
    private = _get(api, f"/api/v1/sources/{note['id']}/asset?path=raw/detail.json")
    assert private.status == 400
