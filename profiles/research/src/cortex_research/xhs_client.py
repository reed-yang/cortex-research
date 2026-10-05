"""TikHub XHS list/detail calls and the image CDN download.

Shapes follow the recorded 2026-10-05 probe: a list page answers at
`data.data.notes` with `has_more`, and the next cursor is the last note's
`cursor`; a detail answers at `data.data[0].note_list[0]`. Both require the
inner `data.success`, and a detail must return the requested note ID. An inner
failure or a mismatched ID is `upstream_error`: TikHub bills such a call.

Image URLs are signed and expire. They are returned for the private task
payload only; every `raw` document this module returns has them removed.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

from .provider_http import (
    Deadline,
    ProviderError,
    Resolver,
    client,
    request_json,
    safe_get,
)

TIKHUB_BASE = "https://api.tikhub.io"
LIST_PATH = "/api/v1/xiaohongshu/app_v2/get_user_posted_notes"
DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
# The variant order the spec fixes: the first non-empty http(s) URL wins.
IMAGE_VARIANTS = ("original", "url_size_large", "url")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_API_BYTES = 16 * 1024 * 1024
API_TIMEOUT_SECONDS = 60.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
DOWNLOAD_DEADLINE_SECONDS = 180.0

_HEX24 = re.compile(r"[0-9a-f]{24}\Z")
# Keys whose string values are user text, kept verbatim even when they look
# like a URL.
_TEXT_KEYS = frozenset({"desc", "title", "display_title", "nickname", "name"})


@dataclass(frozen=True)
class ImageRef:
    """One carousel entry at its original 1-based position."""

    ordinal: int
    fileid: str
    width: int | None
    height: int | None
    variant: str | None
    url: str | None  # signed; private

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "fileid": self.fileid,
            "width": self.width,
            "height": self.height,
            "variant": self.variant,
            "url": self.url,
        }


@dataclass(frozen=True)
class ListedNote:
    note_id: str
    user_id: str | None
    note_type: str
    sticky: bool
    title: str
    caption: str  # the list cuts captions at 100 characters
    published_at: str | None
    cursor: str
    images: tuple[ImageRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "user_id": self.user_id,
            "note_type": self.note_type,
            "sticky": self.sticky,
            "title": self.title,
            "caption": self.caption,
            "caption_complete": False,
            "published_at": self.published_at,
            "cursor": self.cursor,
            "images": [image.to_dict() for image in self.images],
        }


@dataclass(frozen=True)
class ListPage:
    notes: tuple[ListedNote, ...]
    has_more: bool
    next_cursor: str | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NoteDetail:
    note_id: str
    note_type: str
    title: str
    caption: str
    published_at: str | None
    user_id: str | None
    user_name: str | None
    images: tuple[ImageRef, ...]
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "note_type": self.note_type,
            "title": self.title,
            "caption": self.caption,
            "caption_complete": True,
            "published_at": self.published_at,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "images": [image.to_dict() for image in self.images],
        }


@dataclass(frozen=True)
class DownloadedImage:
    path: Path
    sha256: str
    byte_size: int
    media_type: str
    extension: str
    width: int | None
    height: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
            "extension": self.extension,
            "width": self.width,
            "height": self.height,
        }


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or _HEX24.fullmatch(value) is None:
        raise ValueError(f"{name} must be 24 lowercase hex characters")
    return value


def _invalid(message: str) -> ProviderError:
    return ProviderError("invalid_response", f"tikhub: {message}")


def _timestamp(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000 if value > 10**12 else value
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dimension(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _is_url(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(("https://", "http://"))
        and not any(character.isspace() for character in value)
    )


def strip_signed_urls(value: Any, key: str | None = None) -> Any:
    """Drop the query and fragment of every URL value, recursively.

    The XHS CDN signs with query parameters, so a URL without its query can be
    kept in a private raw file without remaining usable. User text is kept
    verbatim.
    """

    if isinstance(value, dict):
        return {name: strip_signed_urls(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [strip_signed_urls(item, key) for item in value]
    if _is_url(value) and key not in _TEXT_KEYS:
        return value.split("?", 1)[0].split("#", 1)[0]
    return value


def parse_images(entries: Any) -> tuple[ImageRef, ...]:
    """Each entry keeps its list position; a bad URL never shifts a later one."""

    if not isinstance(entries, list):
        raise _invalid("images_list is not a list")
    images: list[ImageRef] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise _invalid("an images_list entry is not an object")
        fileid = entry.get("fileid")
        if not isinstance(fileid, str) or not fileid:
            raise _invalid("an image has no fileid")
        variant = next((name for name in IMAGE_VARIANTS if _is_url(entry.get(name))), None)
        images.append(
            ImageRef(
                ordinal=position,
                fileid=fileid,
                width=_dimension(entry.get("width")),
                height=_dimension(entry.get("height")),
                variant=variant,
                url=entry.get(variant) if variant else None,
            )
        )
    return tuple(images)


def _inner(document: Any) -> Any:
    """The provider's inner payload after the inner success check."""

    if not isinstance(document, dict) or not isinstance(document.get("data"), dict):
        raise _invalid("answer has no data object")
    inner = document["data"]
    if inner.get("success") is not True:
        raise ProviderError("upstream_error", "tikhub: inner request did not succeed")
    return inner.get("data")


def _headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise ProviderError("auth", "tikhub: no credential is configured")
    return {"Authorization": f"Bearer {api_key}"}


def list_user_notes(
    user_id: str,
    cursor: str = "",
    *,
    api_key: str,
    base: str = TIKHUB_BASE,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = API_TIMEOUT_SECONDS,
) -> ListPage:
    """One page of a blogger's posted notes."""

    _require_id(user_id, "user_id")
    headers = _headers(api_key)
    deadline = Deadline(timeout_seconds)
    with client(transport) as http:
        document = request_json(
            http,
            "GET",
            base.rstrip("/") + LIST_PATH,
            provider="tikhub",
            headers=headers,
            params={"user_id": user_id, "cursor": cursor or ""},
            timeout=deadline.timeout(timeout_seconds, provider="tikhub"),
            max_bytes=MAX_API_BYTES,
            deadline=deadline,
        )
    data = _inner(document)
    if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
        raise _invalid("list answer has no notes")
    notes: list[ListedNote] = []
    for entry in data["notes"]:
        if not isinstance(entry, dict):
            raise _invalid("a listed note is not an object")
        note_id = entry.get("id")
        if not isinstance(note_id, str) or _HEX24.fullmatch(note_id) is None:
            raise _invalid("a listed note has no 24-hex id")
        user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
        owner = user.get("userid") if isinstance(user.get("userid"), str) else None
        if owner is not None and owner != user_id:
            raise ProviderError("upstream_error", "tikhub: a listed note belongs to another user")
        notes.append(
            ListedNote(
                note_id=note_id,
                user_id=owner,
                note_type=str(entry.get("type") or ""),
                sticky=entry.get("sticky") is True,
                title=str(entry.get("title") or entry.get("display_title") or ""),
                caption=str(entry.get("desc") or ""),
                published_at=_timestamp(entry.get("create_time")),
                cursor=str(entry.get("cursor") or ""),
                images=parse_images(entry.get("images_list") or []),
            )
        )
    has_more = data.get("has_more") is True
    next_cursor = notes[-1].cursor if notes and notes[-1].cursor else None
    if has_more and next_cursor is None:
        raise _invalid("list answer has more notes but no cursor")
    return ListPage(
        notes=tuple(notes),
        has_more=has_more,
        next_cursor=next_cursor,
        raw=strip_signed_urls(document),
    )


def note_detail(
    note_id: str,
    *,
    api_key: str,
    base: str = TIKHUB_BASE,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = API_TIMEOUT_SECONDS,
) -> NoteDetail:
    """One note's full caption, author, time and ordered carousel."""

    _require_id(note_id, "note_id")
    headers = _headers(api_key)
    deadline = Deadline(timeout_seconds)
    with client(transport) as http:
        document = request_json(
            http,
            "GET",
            base.rstrip("/") + DETAIL_PATH,
            provider="tikhub",
            headers=headers,
            params={"note_id": note_id},
            timeout=deadline.timeout(timeout_seconds, provider="tikhub"),
            max_bytes=MAX_API_BYTES,
            deadline=deadline,
        )
    data = _inner(document)
    try:
        note = data[0]["note_list"][0]
    except (IndexError, KeyError, TypeError) as error:
        raise _invalid("detail answer has no note") from error
    if not isinstance(note, dict):
        raise _invalid("detail note is not an object")
    if note.get("id") != note_id:
        raise ProviderError("upstream_error", "tikhub: detail returned another note")
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    owner = user.get("userid") or user.get("id")
    name = user.get("nickname") or user.get("name")
    return NoteDetail(
        note_id=note_id,
        note_type=str(note.get("type") or ""),
        title=str(note.get("title") or ""),
        caption=str(note.get("desc") or ""),
        published_at=_timestamp(note.get("time")),
        user_id=owner if isinstance(owner, str) and owner else None,
        user_name=name if isinstance(name, str) and name else None,
        images=parse_images(note.get("images_list") or []),
        raw=strip_signed_urls(document),
    )


# -- image download -----------------------------------------------------------


def _cdn_status(status: int) -> str | None:
    """A refused CDN read means the signed URL went stale, not a bad key."""

    if 200 <= status < 300:
        return None
    if status in (401, 403, 404, 410):
        return "url_expired"
    if status == 429:
        return "rate_limited"
    if status == 408 or status >= 500:
        return "transient"
    return "upstream_error"


def sniff_image(data: bytes) -> tuple[str, str, int | None, int | None]:
    """Media type, extension and decoded size, from the bytes themselves."""

    if data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_size(data)
        return "image/jpeg", "jpg", width, height
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 24 and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
            return "image/png", "png", width or None, height or None
        return "image/png", "png", None, None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return "image/gif", "gif", width or None, height or None
        return "image/gif", "gif", None, None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        return "image/webp", "webp", width, height
    raise ProviderError("invalid_response", "cdn: body is not a JPEG, PNG, WebP or GIF image")


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            index += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width or None, height or None
        index += 2 + length
    return None, None


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return (width & 0x3FFF) or None, (height & 0x3FFF) or None
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None, None


def download_image(
    url: str,
    destination: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
    max_bytes: int = MAX_IMAGE_BYTES,
    deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
) -> DownloadedImage:
    """GET one carousel image and store it under its own hash.

    No cookie and no header beyond the agent string; the URL's own address is
    held to the public-web policy, since it comes from provider data. The file
    lands as `<destination>/<sha256>.<ext>` through a same-directory rename, so
    a crash leaves at most a stray temporary file and never a torn image.
    """

    page = safe_get(
        url,
        provider="cdn",
        max_bytes=max_bytes,
        deadline=Deadline(deadline_seconds),
        request_timeout=DOWNLOAD_TIMEOUT_SECONDS,
        transport=transport,
        resolver=resolver,
        accept="image/*",
        status_map=_cdn_status,
    )
    if not page.body:
        raise ProviderError("invalid_response", "cdn: empty body")
    media_type, extension, width, height = sniff_image(page.body)
    digest = hashlib.sha256(page.body).hexdigest()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{digest}.{extension}"
    if not target.exists():
        handle, temporary = tempfile.mkstemp(prefix=".download-", dir=destination)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(page.body)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return DownloadedImage(
        path=target,
        sha256=digest,
        byte_size=len(page.body),
        media_type=media_type,
        extension=extension,
        width=width,
        height=height,
    )
