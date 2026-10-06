"""Conservative canonicalization for authority-backed source locators."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

from ..resources import parse_resource_uri
from .models import CanonicalLocator, normalize_source_text, normalize_xhs_id


_ARXIV_RE = re.compile(
    r"(?:arxiv:)?(?P<work>[0-9]{4}\.[0-9]{4,5})(?:v(?P<version>[1-9][0-9]*))?\Z",
    re.IGNORECASE,
)
_ARXIV_PATH_RE = re.compile(
    r"/(?P<view>abs|pdf)/(?P<work>[0-9]{4}\.[0-9]{4,5})"
    r"(?:v(?P<version>[1-9][0-9]*))?(?P<pdf>\.pdf)?\Z",
)
_DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:a-z0-9]+\Z", re.IGNORECASE)
# The Capture parser also admits the HTML view; `canonicalize_locator` keeps
# its narrower abs/pdf set.
_CAPTURE_PATH_RE = re.compile(
    r"/(?P<view>abs|pdf|html)/(?P<work>[0-9]{4}\.[0-9]{4,5})"
    r"(?:v(?P<version>[1-9][0-9]*))?(?P<pdf>\.pdf)?\Z",
)
_CAPTURE_HOSTS = frozenset({"arxiv.org", "www.arxiv.org"})
# A candidate token is a maximal run of ASCII characters other than whitespace
# and these brackets and quotes; any non-ASCII character also ends one.
_CAPTURE_RUN_RE = re.compile(r"""[^\s()\[\]<>"'\x80-\U0010ffff]+""")
# Sentence punctuation trimmed from both ends of a run; it stays in the note.
_CAPTURE_TRIM = ".,;:!?"
# urlsplit silently deletes leading C0 controls, so a token holding one is
# never an exact locator.
_CAPTURE_CONTROLS = frozenset(chr(code) for code in range(0x20))
# The number the ingest child takes as the paper from raw text
# (`cortex_research.arxiv_client._strip_version`). A Capture dispatched before
# this parser handed the child its raw payload, so any such number outside the
# locators must name their paper for a reread of that dispatch to agree.
_CAPTURE_ID_SHAPE_RE = re.compile(r"\d{4}\.\d{4,5}")


@dataclass(frozen=True)
class ArxivCapturePayload:
    """The one arXiv paper a Capture names, and the words around it.

    `note` is derived on read and never stored: the submitted payload stays
    the durable record.
    """

    work_id: str
    note: str

    @property
    def canonical_id(self) -> str:
        return f"arxiv:{self.work_id}"


def canonicalize_arxiv_id(value: str) -> CanonicalLocator:
    """Normalize one modern arXiv ID while retaining its version observation."""

    if not isinstance(value, str):
        raise ValueError("arXiv identifier is invalid")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("arXiv identifier must contain ASCII only") from exc
    match = _ARXIV_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("arXiv identifier is invalid")
    work = match.group("work")
    version = match.group("version")
    return CanonicalLocator(
        authority="arxiv",
        authority_id=work,
        normalized_locator=f"https://arxiv.org/abs/{work}",
        claim_kind="arxiv",
        version=int(version) if version is not None else None,
    )


def canonicalize_locator(value: str) -> CanonicalLocator:
    """Normalize only URL path forms whose arXiv equivalence is proven."""

    if not isinstance(value, str) or not value or len(value) > 2_000:
        raise ValueError("source locator is invalid")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("source locator must contain ASCII only") from exc
    split = urlsplit(value)
    if (
        split.scheme != "https"
        or split.hostname not in {"arxiv.org", "www.arxiv.org"}
        or split.username is not None
        or split.password is not None
        or split.port is not None
        or split.query
        or split.fragment
    ):
        raise ValueError("source locator is not a supported canonical form")
    if unquote(split.path) != split.path:
        raise ValueError("encoded source locator paths are not accepted")
    match = _ARXIV_PATH_RE.fullmatch(split.path)
    if match is None:
        raise ValueError("source locator is not a supported canonical form")
    if match.group("view") == "abs" and match.group("pdf") is not None:
        raise ValueError("source locator is not a supported canonical form")
    canonical = canonicalize_arxiv_id(
        match.group("work")
        + (f"v{match.group('version')}" if match.group("version") else "")
    )
    return replace(canonical, claim_kind="url")


def parse_arxiv_capture_payload(payload: str) -> ArxivCapturePayload:
    """Find the one arXiv paper a Capture payload names.

    A candidate token is a maximal run of ASCII characters other than
    whitespace and `()[]<>"'`, so CJK text and full-width punctuation also
    end one; `.,;:!?` is trimmed from its two ends. A locator is a token that
    is a modern ID, `arXiv:<id>`, or an http, https or scheme-less
    arxiv.org/www.arxiv.org abs, pdf or html path with an optional version.
    A token joined to earlier text that holds a `/` outside a locator, with
    no whitespace or non-ASCII character between them, continues that link
    and is not a locator.
    A URL query or fragment never names the paper and is ignored. Nothing is
    fetched. The same paper may appear more than once; two different papers
    are refused, including an ID-shaped number for another paper anywhere
    outside the locators. Every other character of the payload is the note,
    trimmed only at its two ends.
    """

    if not isinstance(payload, str):
        raise ValueError("capture payload is invalid")
    works: set[str] = set()
    spans: list[tuple[int, int]] = []
    # Whether the text since the last whitespace or non-ASCII character holds
    # a `/` outside a locator, so that a run here is still part of a link.
    joined_to_link = False
    cursor = 0
    for run in _CAPTURE_RUN_RE.finditer(payload):
        if any(
            char.isspace() or not char.isascii()
            for char in payload[cursor : run.start()]
        ):
            joined_to_link = False
        cursor = run.end()
        text = run.group()
        token = text.strip(_CAPTURE_TRIM)
        work = None if joined_to_link or not token else _capture_token_work(token)
        if work is None:
            joined_to_link = joined_to_link or "/" in text
            continue
        start = run.start() + len(text) - len(text.lstrip(_CAPTURE_TRIM))
        works.add(work)
        spans.append((start, start + len(token)))
    if not works:
        raise ValueError("capture payload names no arXiv paper")
    if len(works) > 1:
        raise ValueError("capture payload names more than one arXiv paper")
    work = works.pop()
    # Scan the raw payload, not the note: a full-width digit ends a token yet
    # is a digit to the child, so a number may run across a locator's edge.
    index = 0
    for shape in _CAPTURE_ID_SHAPE_RE.finditer(payload):
        while index < len(spans) and spans[index][1] <= shape.start():
            index += 1
        inside = (
            index < len(spans)
            and spans[index][0] <= shape.start()
            and shape.end() <= spans[index][1]
        )
        if not inside and shape.group() != work:
            raise ValueError("capture payload names more than one arXiv paper")
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(payload[cursor:start])
        cursor = end
    pieces.append(payload[cursor:])
    return ArxivCapturePayload(work_id=work, note="".join(pieces).strip())


def _capture_token_work(token: str) -> str | None:
    """The arXiv work one payload token names exactly, or None."""

    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not _CAPTURE_CONTROLS.isdisjoint(token):
        return None
    try:
        return canonicalize_arxiv_id(token).authority_id
    except ValueError:
        pass
    if token.lower().startswith(("arxiv.org/", "www.arxiv.org/")):
        token = f"https://{token}"
    try:
        split = urlsplit(token)
    except ValueError:
        return None
    # The whole authority must be the bare host: this one comparison refuses
    # userinfo, any port and a trailing dot.
    if (
        split.scheme not in {"http", "https"}
        or split.netloc.lower() not in _CAPTURE_HOSTS
        or unquote(split.path) != split.path
    ):
        return None
    match = _CAPTURE_PATH_RE.fullmatch(split.path)
    if match is None or (match.group("pdf") and match.group("view") != "pdf"):
        return None
    return canonicalize_arxiv_id(
        match.group("work")
        + (f"v{match.group('version')}" if match.group("version") else "")
    ).authority_id


def canonicalize_doi(value: str) -> CanonicalLocator:
    """Normalize a DOI token or an exact HTTPS doi.org path."""

    if not isinstance(value, str) or not value or len(value) > 1_000:
        raise ValueError("DOI is invalid")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("DOI must contain ASCII only") from exc
    candidate = value.strip()
    if candidate.lower().startswith("doi:"):
        candidate = candidate[4:]
    elif "://" in candidate:
        split = urlsplit(candidate)
        if (
            split.scheme != "https"
            or split.hostname != "doi.org"
            or split.username is not None
            or split.password is not None
            or split.port is not None
            or split.query
            or split.fragment
            or unquote(split.path) != split.path
            or not split.path.startswith("/")
        ):
            raise ValueError("DOI URL is not a supported canonical form")
        candidate = split.path[1:]
    if _DOI_RE.fullmatch(candidate) is None:
        raise ValueError("DOI is invalid")
    authority_id = candidate.lower()
    return CanonicalLocator(
        authority="doi",
        authority_id=authority_id,
        normalized_locator=f"https://doi.org/{authority_id}",
        claim_kind="url" if "://" in value else "doi",
    )


_URL_DEFAULT_PORTS = {"http": 80, "https": 443}
# Printable ASCII without space: an IRI must arrive percent-encoded.
_URL_CHARACTERS_RE = re.compile(r"[\x21-\x7e]{1,2000}\Z")


def normalize_url(value: str) -> str:
    """Normalize one http(s) URL conservatively for blog identity.

    The scheme and host are lowercased; the default port and the fragment are
    dropped and an absent path becomes `/`. Path case, the query and any
    trailing slash are kept as given. A URL with credentials, another scheme,
    no host or a malformed port is refused. Nothing is fetched or resolved.
    """

    if not isinstance(value, str):
        raise ValueError("URL is invalid")
    value = value.strip()
    if _URL_CHARACTERS_RE.fullmatch(value) is None:
        raise ValueError("URL is invalid")
    try:
        split = urlsplit(value)
        port = split.port
    except ValueError:
        raise ValueError("URL is invalid") from None
    scheme = split.scheme.lower()
    if scheme not in _URL_DEFAULT_PORTS:
        raise ValueError("URL scheme is unsupported")
    if split.username is not None or split.password is not None or "@" in split.netloc:
        raise ValueError("URL must not carry credentials")
    host = (split.hostname or "").lower()
    if not host or "%" in host:
        raise ValueError("URL host is invalid")
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _URL_DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    query = f"?{split.query}" if split.query else ""
    return f"{scheme}://{host}{split.path or '/'}{query}"


def blog_url_identity(value: str) -> tuple[str, str]:
    """The normalized URL and its blog `authority_id`, the hex SHA-256 of it."""

    normalized = normalize_url(value)
    return normalized, hashlib.sha256(normalized.encode("ascii")).hexdigest()


def xhs_note_permalink(note_id: str) -> str:
    """The public note page. Identity is the note ID, never this URL."""

    return f"https://www.xiaohongshu.com/explore/{normalize_xhs_id(note_id, 'note_id')}"


def canonicalize_source_locator(
    value: str, *, local_sha256: str | None = None
) -> CanonicalLocator:
    """Parse one supported top-level user locator into an exact claim identity."""

    value = normalize_source_text(value, "source locator", maximum=2_000)
    if value.startswith("cortex://"):
        resource = parse_resource_uri(value)
        for segment in resource.segments:
            normalize_source_text(segment, "resource segment", maximum=1_000)
        if not isinstance(local_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", local_sha256, re.IGNORECASE
        ) is None:
            raise ValueError("local Cortex source requires a SHA-256 identity")
        digest = local_sha256.lower()
        return CanonicalLocator(
            authority="sha256",
            authority_id=digest,
            normalized_locator=resource.value,
            claim_kind="local_file",
        )
    if local_sha256 is not None:
        raise ValueError("locator_sha256 is valid only for a Cortex local file")
    if "://" in value:
        split = urlsplit(value)
        if split.hostname in {"arxiv.org", "www.arxiv.org"}:
            return canonicalize_locator(value)
        if split.hostname == "doi.org":
            return replace(canonicalize_doi(value), claim_kind="url")
        raise ValueError("source locator URL authority is unsupported")
    if _ARXIV_RE.fullmatch(value) is not None:
        return canonicalize_arxiv_id(value)
    if value.lower().startswith("doi:") or _DOI_RE.fullmatch(value) is not None:
        return replace(canonicalize_doi(value), claim_kind="doi")
    raise ValueError("source locator is not a supported canonical form")
