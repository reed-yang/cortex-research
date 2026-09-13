"""Strict parsing for version 0.1 Cortex resource URIs."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urlsplit


_ROOT_RE = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_BAD_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


@dataclass(frozen=True)
class CortexResourceURI:
    """A validated, canonical Cortex resource identifier."""

    value: str
    root: str
    segments: tuple[str, ...]


def parse_resource_uri(value: str) -> CortexResourceURI:
    """Parse a canonical Cortex URI and reject ambiguous path spellings."""

    if not isinstance(value, str) or not value.startswith("cortex://"):
        raise ValueError("resource URI must use the lowercase cortex scheme")
    if _ENCODED_SEPARATOR_RE.search(value):
        raise ValueError("resource URI contains an encoded path separator")

    raw_authority, separator, _ = value[len("cortex://") :].partition("/")
    if not separator or not _ROOT_RE.fullmatch(raw_authority):
        raise ValueError("resource URI root is invalid")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("resource URI authority is invalid") from exc
    if (
        parsed.scheme != "cortex"
        or parsed.netloc != raw_authority
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname != raw_authority
    ):
        raise ValueError("resource URI contains unsupported authority or suffix data")

    raw_segments = parsed.path.split("/")[1:]
    if not raw_segments or not all(raw_segments):
        raise ValueError("resource URI path must contain non-empty segments")

    decoded_segments: list[str] = []
    for segment in raw_segments:
        if _BAD_ESCAPE_RE.search(segment):
            raise ValueError("resource URI contains an invalid percent escape")
        try:
            decoded = unquote_to_bytes(segment).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("resource URI path is not valid UTF-8") from exc
        if unicodedata.normalize("NFC", decoded) != decoded:
            raise ValueError("resource URI path must use NFC Unicode")
        if (
            decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or "\x00" in decoded
            or any(unicodedata.category(character) == "Cc" for character in decoded)
        ):
            raise ValueError("resource URI path segment is unsafe")
        decoded_segments.append(decoded)

    canonical = "cortex://" + raw_authority + "/" + "/".join(
        quote(segment, safe="-._~", encoding="utf-8", errors="strict")
        for segment in decoded_segments
    )
    if value != canonical:
        raise ValueError("resource URI is not in canonical percent-encoded form")

    return CortexResourceURI(value, raw_authority, tuple(decoded_segments))
