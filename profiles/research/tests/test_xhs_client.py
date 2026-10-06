"""TikHub list/detail parsing and the CDN download, against synthetic answers.

The documents are shaped like the recorded probe responses; every ID, caption
and URL here is invented, and every image is generated in the test.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from cortex_research import xhs_client
from cortex_research.provider_http import ProviderError

USER = "5f0e1d2c3b4a596877665544"
NOTE_A = "6a0b1c2d3e4f5a6b7c8d9e0f"
NOTE_B = "6a0b1c2d3e4f5a6b7c8d9e1f"
NOTE_C = "6a0b1c2d3e4f5a6b7c8d9e2f"
KEY = "test-tikhub-key"


def png(width: int = 3, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def jpeg(width: int = 5, height: int = 4) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp(width: int = 7, height: int = 6) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 8
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def gif(width: int = 9, height: int = 8) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def cdn(name: str) -> str:
    return f"https://sns-img.cdn.example/{name}?sign=deadbeef&t=1700000000"


def image(fileid: str, **variants: str) -> dict:
    entry = {"fileid": fileid, "width": 1080, "height": 1440, "trace_id": fileid,
             "original": "", "url_size_large": "", "url": ""}
    entry.update(variants)
    return entry


def listed(note_id: str, *, sticky: bool = False, cursor: str | None = None) -> dict:
    return {
        "id": note_id,
        "type": "normal",
        "sticky": sticky,
        "title": "Reading list",
        "display_title": "Reading list",
        "desc": "这周读的论文 papers I read this week " * 3,
        "create_time": 1_759_600_000,
        "cursor": cursor or note_id,
        "user": {"userid": USER, "nickname": "Synthetic Curator"},
        "images_list": [
            image("spectrum/one", url_size_large=cdn("one-large"), url=cdn("one")),
            image("spectrum/two", url=cdn("two")),
        ],
    }


def envelope(inner_data, *, success: bool = True) -> dict:
    return {
        "code": 200,
        "message": "Request successful.",
        "params": {},
        "data": {"code": 0 if success else -1, "success": success, "msg": "", "data": inner_data},
    }


def detail_document(note_id: str = NOTE_A, *, returned: str | None = None, images=None) -> dict:
    note = {
        "id": returned or note_id,
        "type": "normal",
        "title": "Reading list",
        "desc": "完整的说明 The complete caption, longer than the list's cut. " * 4,
        "time": 1_759_600_000,
        "user": {"userid": USER, "id": USER, "nickname": "Synthetic Curator"},
        "images_list": images
        if images is not None
        else [
            image("spectrum/one", original=cdn("one-orig"), url_size_large=cdn("one-large"), url=cdn("one")),
            image("spectrum/two", url_size_large="not a url", url=cdn("two")),
            image("spectrum/three"),
        ],
        "share_info": {"link": "https://www.xiaohongshu.com/discovery/item/x?xsec_token=secret"},
    }
    return envelope([{"note_list": [note], "comment_list": []}])


def transport_for(document=None, *, status: int = 200, exception: Exception | None = None,
                  seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if exception is not None:
            raise exception
        return httpx.Response(status, json=document if document is not None else {})

    return httpx.MockTransport(handler)


def test_a_list_page_parses_notes_cursor_and_has_more() -> None:
    seen: list[httpx.Request] = []
    document = envelope({"has_more": True, "notes": [listed(NOTE_A), listed(NOTE_B, sticky=True)]})
    page = xhs_client.list_user_notes(
        USER, "", api_key=KEY, transport=transport_for(document, seen=seen)
    )
    request = seen[0]
    assert request.url.path == xhs_client.LIST_PATH
    assert dict(request.url.params) == {"user_id": USER, "cursor": ""}
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert [note.note_id for note in page.notes] == [NOTE_A, NOTE_B]
    assert page.notes[1].sticky is True
    assert page.has_more is True and page.next_cursor == NOTE_B
    assert page.notes[0].published_at == "2025-10-04T17:46:40Z"
    assert page.notes[0].images[0].variant == "url_size_large"
    assert page.notes[0].to_dict()["caption_complete"] is False
    # The private raw copy keeps no signed query.
    assert "sign=" not in json.dumps(page.raw)
    assert KEY not in json.dumps(page.raw)


def test_a_last_page_has_no_cursor() -> None:
    document = envelope({"has_more": False, "notes": []})
    page = xhs_client.list_user_notes(USER, "c1", api_key=KEY, transport=transport_for(document))
    assert page.notes == () and page.has_more is False and page.next_cursor is None


def test_an_inner_failure_is_an_upstream_error_never_an_empty_page() -> None:
    document = envelope(None, success=False)
    with pytest.raises(ProviderError) as caught:
        xhs_client.list_user_notes(USER, "", api_key=KEY, transport=transport_for(document))
    assert caught.value.category == "upstream_error"


def test_a_list_without_notes_is_invalid() -> None:
    with pytest.raises(ProviderError) as caught:
        xhs_client.list_user_notes(
            USER, "", api_key=KEY, transport=transport_for(envelope({"has_more": False}))
        )
    assert caught.value.category == "invalid_response"


def test_a_note_of_another_user_is_refused() -> None:
    other = listed(NOTE_A)
    other["user"] = {"userid": "0" * 24}
    with pytest.raises(ProviderError) as caught:
        xhs_client.list_user_notes(
            USER, "", api_key=KEY,
            transport=transport_for(envelope({"has_more": False, "notes": [other]})),
        )
    assert caught.value.category == "upstream_error"


def test_detail_reads_the_full_caption_and_the_variant_order() -> None:
    seen: list[httpx.Request] = []
    detail = xhs_client.note_detail(
        NOTE_A, api_key=KEY, transport=transport_for(detail_document(), seen=seen)
    )
    assert seen[0].url.path == xhs_client.DETAIL_PATH
    assert dict(seen[0].url.params) == {"note_id": NOTE_A}
    assert detail.caption.startswith("完整的说明")
    assert detail.to_dict()["caption_complete"] is True
    assert detail.user_id == USER and detail.user_name == "Synthetic Curator"
    assert [(image.ordinal, image.variant) for image in detail.images] == [
        (1, "original"),
        (2, "url"),
        (3, None),
    ]
    assert detail.images[0].url == cdn("one-orig")
    assert detail.images[2].url is None  # kept at its ordinal, never compacted
    raw = json.dumps(detail.raw)
    assert "sign=" not in raw and "xsec_token" not in raw


def test_detail_of_another_note_is_an_upstream_error() -> None:
    with pytest.raises(ProviderError) as caught:
        xhs_client.note_detail(
            NOTE_A, api_key=KEY, transport=transport_for(detail_document(returned=NOTE_C))
        )
    assert caught.value.category == "upstream_error"


def test_detail_with_no_note_is_invalid() -> None:
    with pytest.raises(ProviderError) as caught:
        xhs_client.note_detail(NOTE_A, api_key=KEY, transport=transport_for(envelope([])))
    assert caught.value.category == "invalid_response"


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (402, "payment"), (429, "rate_limited"), (500, "transient"),
     (502, "transient"), (400, "upstream_error"), (404, "not_found")],
)
def test_http_failures_map_to_categories(status: int, category: str) -> None:
    with pytest.raises(ProviderError) as caught:
        xhs_client.note_detail(
            NOTE_A, api_key=KEY, transport=transport_for({"detail": "x"}, status=status)
        )
    assert caught.value.category == category
    assert KEY not in caught.value.message


def test_a_timeout_is_transient() -> None:
    error = httpx.ReadTimeout("slow")
    with pytest.raises(ProviderError) as caught:
        xhs_client.note_detail(NOTE_A, api_key=KEY, transport=transport_for(exception=error))
    assert caught.value.category == "transient"


def test_a_non_json_answer_is_invalid() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as caught:
        xhs_client.note_detail(NOTE_A, api_key=KEY, transport=transport)
    assert caught.value.category == "invalid_response"


def test_a_missing_key_is_auth_and_sends_nothing() -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(ProviderError) as caught:
        xhs_client.note_detail(NOTE_A, api_key="", transport=transport_for({}, seen=seen))
    assert caught.value.category == "auth" and seen == []


def test_ids_are_validated_before_any_call() -> None:
    with pytest.raises(ValueError):
        xhs_client.note_detail(NOTE_A[:8], api_key=KEY, transport=transport_for({}))


# -- download -----------------------------------------------------------------


def cdn_resolver(host: str, port: int, type: int = 0):  # noqa: A002
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.40", port))]


@pytest.mark.parametrize(
    ("data", "media_type", "extension", "size"),
    [
        (png(), "image/png", "png", (3, 2)),
        (jpeg(), "image/jpeg", "jpg", (5, 4)),
        (webp(), "image/webp", "webp", (7, 6)),
        (gif(), "image/gif", "gif", (9, 8)),
    ],
)
def test_a_download_is_typed_by_signature_and_stored_under_its_hash(
    tmp_path: Path, data: bytes, media_type: str, extension: str, size: tuple[int, int]
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # A wrong declared type must not matter: the signature decides.
        return httpx.Response(200, content=data, headers={"content-type": "text/plain"})

    result = xhs_client.download_image(
        cdn("one"), tmp_path / "staging", transport=httpx.MockTransport(handler),
        resolver=cdn_resolver,
    )
    digest = hashlib.sha256(data).hexdigest()
    assert result.path == tmp_path / "staging" / f"{digest}.{extension}"
    assert result.path.read_bytes() == data
    assert (result.media_type, result.extension) == (media_type, extension)
    assert (result.width, result.height) == size
    assert result.byte_size == len(data)
    assert "cookie" not in seen[0].headers and "authorization" not in seen[0].headers
    assert seen[0].url.params["sign"] == "deadbeef"
    assert [entry.name for entry in (tmp_path / "staging").iterdir()] == [result.path.name]


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_a_refused_cdn_read_means_the_url_expired(tmp_path: Path, status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    with pytest.raises(ProviderError) as caught:
        xhs_client.download_image(cdn("one"), tmp_path, transport=transport, resolver=cdn_resolver)
    assert caught.value.category == "url_expired"
    assert "sign" not in caught.value.message


def test_a_non_image_body_is_invalid(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html></html>"))
    with pytest.raises(ProviderError) as caught:
        xhs_client.download_image(cdn("one"), tmp_path, transport=transport, resolver=cdn_resolver)
    assert caught.value.category == "invalid_response"
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_image_is_refused(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=png() + b"\0" * 64))
    with pytest.raises(ProviderError) as caught:
        xhs_client.download_image(
            cdn("one"), tmp_path, transport=transport, resolver=cdn_resolver, max_bytes=32
        )
    assert caught.value.category == "invalid_response"
    assert list(tmp_path.iterdir()) == []


def test_a_cdn_url_on_a_private_address_is_refused(tmp_path: Path) -> None:
    def private(host: str, port: int, type: int = 0):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.2", port))]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=png()))
    with pytest.raises(ProviderError) as caught:
        xhs_client.download_image(cdn("one"), tmp_path, transport=transport, resolver=private)
    assert caught.value.category == "not_found"


def test_strip_signed_urls_keeps_user_text() -> None:
    value = {
        "desc": "https://example.org/a?b=c",
        "url": "https://cdn.example/x?sign=1",
        "nested": [{"original": "http://cdn.example/y?t=2#f"}],
    }
    assert xhs_client.strip_signed_urls(value) == {
        "desc": "https://example.org/a?b=c",
        "url": "https://cdn.example/x",
        "nested": [{"original": "http://cdn.example/y"}],
    }


def test_an_odd_video_cover_never_fails_the_page_but_a_broken_carousel_does() -> None:
    video = listed(NOTE_C)
    video["type"] = "video"
    video["images_list"] = [{"url": cdn("cover")}]  # no fileid
    page = xhs_client.list_user_notes(
        USER, "", api_key=KEY,
        transport=transport_for(envelope({"has_more": False, "notes": [listed(NOTE_A), video]})),
    )
    assert [(note.note_type, len(note.images)) for note in page.notes] == [("normal", 2), ("video", 0)]

    broken = listed(NOTE_A)
    broken["images_list"] = [{"url": cdn("x")}]
    with pytest.raises(ProviderError) as caught:
        xhs_client.list_user_notes(
            USER, "", api_key=KEY,
            transport=transport_for(envelope({"has_more": False, "notes": [broken]})),
        )
    assert caught.value.category == "invalid_response"
