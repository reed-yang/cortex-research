"""Pure source identity and import boundary models."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_AUTHORITY_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_AUTHORITY_ID_RE = re.compile(r"[\x21-\x7e]{1,500}\Z")
_ARXIV_AUTHORITY_ID_RE = re.compile(
    r"(?:arxiv:)?(?P<work>[0-9]{4}\.[0-9]{4,5})"
    r"(?:v(?P<version>[1-9][0-9]*))?\Z",
    re.IGNORECASE,
)
_DOI_AUTHORITY_ID_RE = re.compile(
    r"10\.[0-9]{4,9}/[-._;()/:a-z0-9]+\Z", re.IGNORECASE
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.IGNORECASE)
_ENGINE_REF_RE = re.compile(
    r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._/-]{0,466}\Z"
)
_CANONICAL_AUTHORITIES = frozenset({"arxiv", "doi", "sha256"})
_CLAIM_KINDS = frozenset({"title", "url", "doi", "arxiv", "local_file"})


@dataclass(frozen=True)
class CandidateObservation:
    """One immutable resolver observation tied to one user claim."""

    claim_kind: str
    authority: str
    authority_id: str
    official_title: str
    version: int | None = None
    locator: str | None = None
    evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.claim_kind not in _CLAIM_KINDS:
            raise ValueError("candidate claim_kind is invalid")
        if _AUTHORITY_RE.fullmatch(self.authority) is None:
            raise ValueError("candidate authority is invalid")
        if self.authority not in _CANONICAL_AUTHORITIES:
            raise ValueError("candidate authority is not supported")
        if self.version is not None and (
            type(self.version) is not int or self.version < 1
        ):
            raise ValueError("candidate version must be a positive integer")
        if self.authority != "arxiv" and self.version is not None:
            raise ValueError("candidate version is valid only for arXiv")
        try:
            self.authority_id.encode("ascii")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise ValueError("authority identity must contain ASCII only") from exc
        if _AUTHORITY_ID_RE.fullmatch(self.authority_id) is None:
            raise ValueError("authority identity is invalid")
        if self.authority == "arxiv":
            match = _ARXIV_AUTHORITY_ID_RE.fullmatch(self.authority_id)
            if match is None:
                raise ValueError("arXiv authority identity is invalid")
            observed_version = (
                int(match.group("version")) if match.group("version") else None
            )
            if self.version is not None and self.version != observed_version:
                if observed_version is not None:
                    raise ValueError("arXiv version observation is inconsistent")
                observed_version = self.version
            object.__setattr__(self, "authority_id", match.group("work"))
            object.__setattr__(self, "version", observed_version)
        elif self.authority == "doi":
            if _DOI_AUTHORITY_ID_RE.fullmatch(self.authority_id) is None:
                raise ValueError("DOI authority identity is invalid")
            object.__setattr__(self, "authority_id", self.authority_id.lower())
        elif self.authority == "sha256":
            if _SHA256_RE.fullmatch(self.authority_id) is None:
                raise ValueError("SHA-256 authority identity is invalid")
            object.__setattr__(self, "authority_id", self.authority_id.lower())
        object.__setattr__(
            self,
            "official_title",
            normalize_source_text(
                self.official_title, "official_title", maximum=2_000
            ),
        )
        if self.locator is not None and (
            not isinstance(self.locator, str)
            or not self.locator
            or len(self.locator) > 2_000
        ):
            raise ValueError("candidate locator is invalid")
        if self.locator is not None:
            object.__setattr__(
                self,
                "locator",
                normalize_source_text(
                    self.locator, "candidate locator", maximum=2_000
                ),
            )
        if self.evidence is not None:
            normalized = _normalize_evidence(self.evidence)
            object.__setattr__(self, "evidence", normalized)

    @property
    def canonical_id(self) -> str:
        return f"{self.authority}:{self.authority_id}"

    def to_record(self) -> dict[str, Any]:
        return {
            "claim_kind": self.claim_kind,
            "authority": self.authority,
            "authority_id": self.authority_id,
            "canonical_id": self.canonical_id,
            "official_title": self.official_title.strip(),
            "version": self.version,
            "locator": self.locator,
            "evidence": dict(self.evidence or {}),
        }


@dataclass(frozen=True)
class CanonicalLocator:
    authority: str
    authority_id: str
    normalized_locator: str
    claim_kind: str
    version: int | None = None

    @property
    def canonical_id(self) -> str:
        return f"{self.authority}:{self.authority_id}"


@dataclass(frozen=True)
class ImportRequest:
    operation_id: str
    source_id: str
    canonical_id: str
    request_hash: str


@dataclass(frozen=True)
class ImportResult:
    operation_id: str
    request_hash: str
    engine_ref: str
    manifest: Mapping[str, int]
    replayed: bool = False


@dataclass(frozen=True)
class ImportDeliveryResult:
    status: str
    source_id: str
    engine_ref: str | None = None
    adapter_replayed: bool = False


def _normalize_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("candidate evidence is invalid")
    allowed = {"resolver", "confidence", "observation", "version", "rank"}
    if not set(value) <= allowed:
        raise ValueError("candidate evidence contains unsupported fields")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "confidence":
            if (
                type(item) not in {int, float}
                or not math.isfinite(item)
                or not 0 <= item <= 1
            ):
                raise ValueError("candidate evidence confidence is invalid")
        elif key == "rank":
            if type(item) is not int or not 0 <= item <= 1_000:
                raise ValueError("candidate evidence rank is invalid")
        else:
            maximum = 100 if key in {"resolver", "version"} else 500
            try:
                item = normalize_source_text(
                    item, f"candidate evidence {key}", maximum=maximum
                )
            except ValueError as exc:
                raise ValueError(f"candidate evidence {key} is invalid") from exc
        result[key] = item
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > 4_096:
        raise ValueError("candidate evidence is too large")
    return result


def normalize_source_text(value: Any, name: str, *, maximum: int) -> str:
    """Normalize public source text and reject every Unicode Other category."""

    if not isinstance(value, str):
        raise ValueError(f"{name} is invalid")
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{name} contains unsafe Unicode/control characters")
    normalized = normalized.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized


def validate_engine_ref(value: Any, *, namespace: str | None = None) -> str:
    """Validate one stable engine reference without rewriting its identity."""

    if not isinstance(value, str) or _ENGINE_REF_RE.fullmatch(value) is None:
        raise ValueError("engine_ref is invalid")
    if ".." in value or "//" in value:
        raise ValueError("engine_ref is invalid")
    if namespace is not None and (
        re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", namespace) is None
        or value.partition(":")[0] != namespace
    ):
        raise ValueError("engine_ref is invalid")
    return value
