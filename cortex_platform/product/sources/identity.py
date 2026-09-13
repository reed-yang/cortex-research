"""Conservative canonicalization for authority-backed source locators."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import unquote, urlsplit

from ..resources import parse_resource_uri
from .models import CanonicalLocator, normalize_source_text


_ARXIV_RE = re.compile(
    r"(?:arxiv:)?(?P<work>[0-9]{4}\.[0-9]{4,5})(?:v(?P<version>[1-9][0-9]*))?\Z",
    re.IGNORECASE,
)
_ARXIV_PATH_RE = re.compile(
    r"/(?P<view>abs|pdf)/(?P<work>[0-9]{4}\.[0-9]{4,5})"
    r"(?:v(?P<version>[1-9][0-9]*))?(?P<pdf>\.pdf)?\Z",
)
_DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:a-z0-9]+\Z", re.IGNORECASE)


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
