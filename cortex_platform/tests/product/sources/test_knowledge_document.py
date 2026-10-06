"""Whole-document and source-asset reads over real adoption commits and files."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import zlib
from contextlib import contextmanager

import pytest

from cortex_platform.product.sources.adoption import encode_engine_ref
from cortex_platform.product.sources.reader import (
    MAX_ASSET_BYTES,
    MAX_DOCUMENT_BYTES,
    SourceAssetInvalid,
    SourceAssetTooLarge,
    SourceAssetUnavailable,
    SourceAssetUnsupported,
    SourceContentUnavailable,
    SourceDocumentTooLarge,
    SourceKnowledgeReader,
    SourceQueryInvalid,
)
from cortex_platform.tests.product.sources.test_adoption_reader import (  # noqa: F401
    corpus,
    database,
)
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge  # noqa: F401

ENGLISH = "20260906-English"
CHINESE = "20260906-中文论文"


def png() -> bytes:
    """A real 1x1 PNG, built here so no binary fixture is committed."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data)
        )
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


SIGNATURES = [
    (png(), "image/png"),
    (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01", "image/jpeg"),
    (b"GIF87a\x01\x00\x01\x00\x00\x00\x00;", "image/gif"),
    (b"GIF89a\x01\x00\x01\x00\x00\x00\x00;", "image/gif"),
    (b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00\x2f\x00\x00\x00", "image/webp"),
]


def source_id(store, paper_dir: str) -> str:
    return next(item["id"] for item in store.list_sources()
                if item["engine_ref"] == encode_engine_ref(paper_dir))


def disable_root(store, key: str) -> None:
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                            max_bytes=root.max_bytes, enabled=False,
                            expected_revision=root.revision, actor_id="operator",
                            idempotency_key=key)


def set_root_bytes(store, max_bytes: int, key: str) -> None:
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(root_id=root.root_id, private_path=root.private_path,
                            max_bytes=max_bytes, enabled=True,
                            expected_revision=root.revision, actor_id="operator",
                            idempotency_key=key)


def revoke_after_inner_read(monkeypatch, store, root) -> None:
    """Disable the root once a directory below it closes, before the recheck."""
    from cortex_platform.product.sources import reader as module

    original = module._directory

    @contextmanager
    def revoking(path):
        with original(path) as fd:
            yield fd
        if path != root and store.get_asset_root("research-corpus").enabled:
            disable_root(store, "revoke-during-document-read")

    monkeypatch.setattr(module, "_directory", revoking)


@pytest.mark.parametrize("kind", ["notes", "full_text", "grounding"])
def test_document_reads_each_kind_whole_in_one_call(knowledge, kind):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    raw = ("第一行\n" + "x" * 30_000 + "\n" + kind + " tail").encode()
    (root / ENGLISH / f"{kind}.md").write_bytes(raw)
    result = reader.document(sid, kind=kind)
    assert result == {
        "source_id": sid, "canonical_id": result["canonical_id"], "kind": kind,
        "text": raw.decode(), "content_sha256": hashlib.sha256(raw).hexdigest(),
        "retained_bytes": len(raw), "redacted": False,
    }
    assert result["canonical_id"].startswith("arxiv:")
    assert str(root) not in str(result)
    # The same bytes and digest the paged citation view names.
    assert reader.read(sid, kind=kind)["content_sha256"] == result["content_sha256"]


def test_document_default_kind_and_closed_arguments(knowledge):
    store, _, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    assert reader.document(sid)["text"] == "notes for Speculative Decoding"
    for kind in ("../notes", "pdf", None, []):
        with pytest.raises(SourceQueryInvalid):
            reader.document(sid, kind=kind)
    for value in ("", "x" * 201, None):
        with pytest.raises(SourceQueryInvalid):
            reader.document(value)


def test_document_redaction_flag_keeps_page_projection(knowledge):
    store, root, _, _ = knowledge
    sid = source_id(store, ENGLISH)
    reader = SourceKnowledgeReader(store, redact_line=lambda line: "secret" in line)
    raw = "safe line\na long secret line here\nsecret\n\nlast safe".encode()
    (root / ENGLISH / "notes.md").write_bytes(raw)
    result = reader.document(sid)
    assert result["redacted"] is True
    assert result["text"] == "safe line\n[redacted]\n******\n\nlast safe"
    assert result["text"] == reader.read(sid)["text"]
    assert result["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["retained_bytes"] == len(raw)
    (root / ENGLISH / "notes.md").write_text("nothing to hide\n", encoding="utf-8")
    assert reader.document(sid)["redacted"] is False


def test_document_limit_is_two_mebibytes_checked_before_decoding(knowledge):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    path = root / ENGLISH / "full_text.md"
    line = b"y" * 1023 + b"\n"
    path.write_bytes(line * (MAX_DOCUMENT_BYTES // len(line)))
    assert MAX_DOCUMENT_BYTES == 2_097_152
    assert reader.document(sid, kind="full_text")["retained_bytes"] == MAX_DOCUMENT_BYTES
    path.write_bytes(line * (MAX_DOCUMENT_BYTES // len(line)) + b"z")
    with pytest.raises(SourceDocumentTooLarge):
        reader.document(sid, kind="full_text")
    # Invalid UTF-8 above the limit is refused by size, never decoded.
    path.write_bytes(b"\xff" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(SourceDocumentTooLarge):
        reader.document(sid, kind="full_text")
    # Pages remain the fallback for a large document.
    path.write_bytes(line * (MAX_DOCUMENT_BYTES // len(line)) + b"z")
    assert reader.read(sid, kind="full_text")["next_cursor"] is not None
    # A file above the root limit stays unavailable, as it is for pages.
    set_root_bytes(store, MAX_DOCUMENT_BYTES, "document-root-limit-001")
    with pytest.raises(SourceContentUnavailable):
        reader.document(sid, kind="full_text")


def test_document_refuses_unknown_unadopted_non_paper_and_disabled(knowledge, tmp_path):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    with pytest.raises(SourceContentUnavailable):
        reader.document("missing")
    unadopted = store.register_source(
        authority="arxiv", authority_id="2609.99999", source_kind="paper",
        official_title="Unadopted", engine_ref=encode_engine_ref("unadopted"),
        aliases=(), actor_id="fixture", idempotency_key="register-unadopted-001",
    ).value
    with pytest.raises(SourceContentUnavailable):
        reader.document(unadopted["id"])
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("UPDATE sources SET source_kind = 'blog' WHERE id = ?", (sid,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SourceContentUnavailable) as error:
        reader.document(sid)
    assert str(tmp_path) not in str(error.value)
    other = source_id(store, CHINESE)
    disable_root(store, "disable-document-root-001")
    with pytest.raises(SourceContentUnavailable):
        reader.document(other)


@pytest.mark.parametrize("size", [10, MAX_DOCUMENT_BYTES + 1])
def test_document_rechecks_authorization_after_the_read(knowledge, monkeypatch, size):
    store, root, _, reader = knowledge
    (root / ENGLISH / "notes.md").write_bytes(b"x" * size)
    revoke_after_inner_read(monkeypatch, store, root)
    # Neither the text nor the size refusal survives a revocation.
    with pytest.raises(SourceContentUnavailable):
        reader.document(source_id(store, ENGLISH))
    assert not store.get_asset_root("research-corpus").enabled


def write_asset(root, relative: str, data: bytes, paper_dir: str = ENGLISH):
    path = root / paper_dir / "assets" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_asset_accepted_forms_read_only_the_source_directory(knowledge):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    image = png()
    write_asset(root, "fig1.png", image)
    write_asset(root, "deep/图 2.gif", SIGNATURES[3][0])
    for path in ("assets/fig1.png", "./assets/fig1.png", f"papers/{ENGLISH}/assets/fig1.png"):
        assert reader.asset(sid, path) == ("image/png", image)
    assert reader.asset(sid, "assets/deep/图 2.gif") == ("image/gif", SIGNATURES[3][0])
    assert reader.asset(sid, f"papers/{ENGLISH}/assets/deep/图 2.gif")[0] == "image/gif"
    # The other adopted paper has its own figure; this source cannot name it.
    write_asset(root, "fig1.png", image, paper_dir=CHINESE)
    for path in (f"papers/{CHINESE}/assets/fig1.png", "papers/unadopted/assets/fig1.png"):
        with pytest.raises(SourceAssetUnavailable):
            reader.asset(sid, path)
    assert reader.asset(source_id(store, CHINESE), f"papers/{CHINESE}/assets/fig1.png")[1] == image


@pytest.mark.parametrize("path", [
    "", "assets", "assets/", "/assets/fig1.png", f"/papers/{ENGLISH}/assets/fig1.png",
    "assets//fig1.png", "assets/./fig1.png", "assets/../notes.md", "../assets/fig1.png",
    "assets/deep/../../notes.md", "./../assets/fig1.png", "././assets/fig1.png",
    "Assets/fig1.png", "notes.md", "fig1.png", f"./papers/{ENGLISH}/assets/fig1.png",
    f"papers/{ENGLISH}/fig1.png", f"papers/{ENGLISH}/assets", "papers/assets/fig1.png",
    f"papers/{ENGLISH}/assets/", "papers/../assets/fig1.png", "papers/./assets/fig1.png",
    "http://example.test/assets/fig1.png", "//example.test/assets/fig1.png",
    "data:image/png;base64,iVBORw0KGgo=", "file:///assets/fig1.png", "assets\\fig1.png",
    "assets/fig1.png\x00", "assets/fig\n1.png", "assets/fig​1.png", "assets/\x7f.png",
    "assets%2Ffig1.png", "assets%2ffig1.png", "assets/%2e%2e/notes.md", "assets/..%2Fnotes.md",
    "assets/fig1%5Cx.png", "assets/fig1.png%00", "assets/" + "a" * 506, "assets/\ud800.png",
    None, b"assets/fig1.png",
])
def test_asset_grammar_refuses_everything_else(knowledge, path):
    store, root, _, reader = knowledge
    write_asset(root, "fig1.png", png())
    with pytest.raises(SourceAssetInvalid) as error:
        reader.asset(source_id(store, ENGLISH), path)
    assert str(root) not in str(error.value)


def test_asset_path_byte_bound_counts_utf8(knowledge):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    # 7 + 4 * 101 + 101 bytes: exactly the bound, within file-name limits.
    name = "/".join(["d" * 100] * 4) + "/" + "a" * 97 + ".png"
    assert len(("assets/" + name).encode()) == 512
    write_asset(root, name, png())
    assert reader.asset(sid, "assets/" + name)[0] == "image/png"
    with pytest.raises(SourceAssetInvalid):
        reader.asset(sid, "assets/" + "图" * 169)


@pytest.mark.parametrize("target", [
    "file symlink", "assets symlink", "nested symlink", "hardlink", "fifo",
    "directory", "missing", "missing directory",
])
def test_asset_links_and_non_regular_files_are_unavailable(knowledge, tmp_path, target):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "fig1.png").write_bytes(png())
    assets = root / ENGLISH / "assets"
    assets.mkdir()
    path = "assets/fig1.png"
    if target == "file symlink":
        (assets / "fig1.png").symlink_to(outside / "fig1.png")
    elif target == "assets symlink":
        assets.rmdir()
        assets.symlink_to(outside, target_is_directory=True)
    elif target == "nested symlink":
        (assets / "deep").symlink_to(outside, target_is_directory=True)
        path = "assets/deep/fig1.png"
    elif target == "hardlink":
        os.link(outside / "fig1.png", assets / "fig1.png")
    elif target == "fifo":
        os.mkfifo(assets / "fig1.png")
    elif target == "directory":
        (assets / "fig1.png").mkdir()
    elif target == "missing directory":
        path = "assets/deep/fig1.png"
    with pytest.raises(SourceAssetUnavailable) as error:
        reader.asset(sid, path)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize(("data", "media_type"), SIGNATURES,
                         ids=["png", "jpeg", "gif87a", "gif89a", "webp"])
def test_asset_type_is_decided_by_signature(knowledge, data, media_type):
    store, root, _, reader = knowledge
    write_asset(root, "figure.svg", data)
    assert reader.asset(source_id(store, ENGLISH), "assets/figure.svg") == (media_type, data)


@pytest.mark.parametrize("data", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    b"<!doctype html><img src=x>", b"%PDF-1.7\n", b"", b"\x89PNG", b"GIF88a",
    b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\x00\x00\x00\x18ftypavif",
])
def test_asset_refuses_svg_html_xml_pdf_and_unknown(knowledge, data):
    store, root, _, reader = knowledge
    write_asset(root, "fig1.png", data)
    with pytest.raises(SourceAssetUnsupported):
        reader.asset(source_id(store, ENGLISH), "assets/fig1.png")


def test_asset_size_cap_is_root_limit_and_eight_mebibytes(knowledge):
    store, root, _, reader = knowledge
    sid = source_id(store, ENGLISH)
    header = png()
    path = write_asset(root, "large.png", header + b"\x00" * (MAX_ASSET_BYTES - len(header)))
    assert MAX_ASSET_BYTES == 8 * 1024 * 1024
    assert len(reader.asset(sid, "assets/large.png")[1]) == MAX_ASSET_BYTES
    with path.open("ab") as handle:
        handle.write(b"\x00")
    with pytest.raises(SourceAssetTooLarge):
        reader.asset(sid, "assets/large.png")
    write_asset(root, "small.png", header)
    set_root_bytes(store, len(header) - 1, "asset-root-limit-001")
    with pytest.raises(SourceAssetTooLarge):
        reader.asset(sid, "assets/small.png")


def test_asset_refuses_unknown_unadopted_and_disabled_sources(knowledge):
    store, root, _, reader = knowledge
    write_asset(root, "fig1.png", png())
    write_asset(root, "fig1.png", png(), paper_dir="unadopted")
    for value in ("missing", "", "x" * 201, None):
        with pytest.raises(SourceAssetUnavailable):
            reader.asset(value, "assets/fig1.png")
    unadopted = store.register_source(
        authority="arxiv", authority_id="2609.99999", source_kind="paper",
        official_title="Unadopted", engine_ref=encode_engine_ref("unadopted"),
        aliases=(), actor_id="fixture", idempotency_key="register-unadopted-002",
    ).value
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(unadopted["id"], "assets/fig1.png")
    disable_root(store, "disable-asset-root-001")
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(source_id(store, ENGLISH), "assets/fig1.png")


@pytest.mark.parametrize("content", ["image", "svg", "too large"])
def test_asset_rechecks_authorization_after_the_read(knowledge, monkeypatch, content):
    store, root, _, reader = knowledge
    data = {"image": png(), "svg": b"<svg/>"}.get(content) or png() + b"\x00" * MAX_ASSET_BYTES
    write_asset(root, "fig1.png", data)
    revoke_after_inner_read(monkeypatch, store, root)
    # Bytes, the type refusal and the size refusal all wait for the recheck.
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(source_id(store, ENGLISH), "assets/fig1.png")
    assert not store.get_asset_root("research-corpus").enabled


def test_asset_change_during_read_is_refused(knowledge, monkeypatch):
    from cortex_platform.product.sources import reader as module

    store, root, _, reader = knowledge
    path = write_asset(root, "fig1.png", png())
    original = module._read_regular

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(png() + b"\x00")
        return result

    monkeypatch.setattr(module, "_read_regular", changed)
    with pytest.raises(SourceAssetUnavailable):
        reader.asset(source_id(store, ENGLISH), "assets/fig1.png")
