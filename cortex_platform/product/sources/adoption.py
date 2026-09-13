"""Bulk adoption of a copied research corpus into product source identity.

A copied corpus is already present on disk, so adoption never fetches
anything: it establishes identity. One content-addressed manifest enumerates
every corpus directory with the authority that names it, and committing that
manifest is the single explicit resolution for every source it carries.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .identity import canonicalize_arxiv_id, canonicalize_doi
from .models import normalize_source_text, validate_engine_ref

_ENGINE_NAMESPACE = "paper"
_ENCODED_PREFIX = "enc/"
# One corpus directory embeds verbatim only when it already satisfies the
# engine_ref grammar's post-colon charset. Everything else is encoded, because
# real paper_dir values are neither ASCII-only (Unicode slugs) nor
# colon-free (radar stubs are 'arxiv:<id>').
_VERBATIM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ENCODED_RE = re.compile(r"[a-z2-7]+\Z")
# base32 emits 8 characters per 5 input bytes, so after stripping padding a
# real payload's length mod 8 is only ever one of these. Accepting the others
# would let `decode` answer for strings `encode` can never produce.
_ENCODED_LENGTHS = frozenset({0, 2, 4, 5, 7})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ENGINE_REF_BODY = 467
_MAX_ENTRIES = 100_000
# The body the engine extracted, preferred over anything derived from it, as
# the identity of a source that carries no external authority.
_PRIMARY_ARTIFACTS = ("full_text.md", "notes.md")
_READ_CHUNK = 1 << 20
# radar_index.py writes an index-only stub as 'arxiv:<id>' with no directory.
_RADAR_STUB_PREFIX = "arxiv:"


class CorpusReadError(RuntimeError):
    """A copied corpus could not be read as a complete, consistent whole."""


def encode_engine_ref(paper_dir: str) -> str:
    """Map one corpus directory name onto a stable, reversible engine_ref."""

    if not isinstance(paper_dir, str) or not paper_dir:
        raise ValueError("paper_dir is invalid")
    if "/" in paper_dir or paper_dir in {".", ".."}:
        raise ValueError("paper_dir is invalid")
    if _VERBATIM_RE.fullmatch(paper_dir) is not None and ".." not in paper_dir:
        body = paper_dir
    else:
        encoded = base64.b32encode(paper_dir.encode("utf-8")).decode("ascii")
        body = _ENCODED_PREFIX + encoded.rstrip("=").lower()
    if len(body) > _MAX_ENGINE_REF_BODY:
        raise ValueError("paper_dir is too long to encode as an engine_ref")
    return validate_engine_ref(f"{_ENGINE_NAMESPACE}:{body}", namespace=_ENGINE_NAMESPACE)


def decode_engine_ref(engine_ref: str) -> str:
    """Recover the corpus directory name from an engine_ref."""

    validate_engine_ref(engine_ref, namespace=_ENGINE_NAMESPACE)
    body = engine_ref.partition(":")[2]
    if not body.startswith(_ENCODED_PREFIX):
        return body
    payload = body[len(_ENCODED_PREFIX) :]
    if (
        _ENCODED_RE.fullmatch(payload) is None
        or len(payload) % 8 not in _ENCODED_LENGTHS
    ):
        raise ValueError("engine_ref is invalid")
    padded = payload.upper() + "=" * (-len(payload) % 8)
    try:
        return base64.b32decode(padded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("engine_ref is invalid") from exc


@dataclass(frozen=True)
class AdoptionEntry:
    """One corpus directory, with the identity the product will record."""

    paper_dir: str
    authority: str
    authority_id: str
    official_title: str
    content_digest: str

    def __post_init__(self) -> None:
        encode_engine_ref(self.paper_dir)  # refuse an unencodable directory here
        if self.authority == "arxiv":
            canonical = canonicalize_arxiv_id(self.authority_id)
        elif self.authority == "doi":
            canonical = canonicalize_doi(self.authority_id)
        elif self.authority == "sha256":
            if _SHA256_RE.fullmatch(str(self.authority_id).lower()) is None:
                raise ValueError("authority_id is invalid")
            object.__setattr__(self, "authority_id", str(self.authority_id).lower())
            canonical = None
        else:
            raise ValueError("authority is not supported")
        if canonical is not None:
            object.__setattr__(self, "authority_id", canonical.authority_id)
        object.__setattr__(
            self,
            "official_title",
            normalize_source_text(self.official_title, "official_title", maximum=2_000),
        )
        if _SHA256_RE.fullmatch(str(self.content_digest).lower()) is None:
            raise ValueError("content_digest is invalid")
        object.__setattr__(self, "content_digest", str(self.content_digest).lower())

    @property
    def canonical_id(self) -> str:
        return f"{self.authority}:{self.authority_id}"

    @property
    def engine_ref(self) -> str:
        return encode_engine_ref(self.paper_dir)

    def to_document(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "authority_id": self.authority_id,
            "content_digest": self.content_digest,
            "engine_ref": self.engine_ref,
            "official_title": self.official_title,
            "paper_dir": self.paper_dir,
        }


@dataclass(frozen=True)
class AdoptionManifest:
    """One immutable, content-addressed enumeration of what to adopt."""

    entries: tuple[AdoptionEntry, ...]

    @property
    def manifest_id(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.to_document(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def to_document(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_document() for entry in self.entries],
            "schema": 1,
        }


def build_manifest(entries: Iterable[AdoptionEntry]) -> AdoptionManifest:
    """Order the entries deterministically and refuse every identity clash."""

    ordered = sorted(entries, key=lambda entry: entry.paper_dir)
    if not ordered:
        raise ValueError("adoption manifest is empty")
    if len(ordered) > _MAX_ENTRIES:
        raise ValueError("adoption manifest is too large")
    seen_dirs: dict[str, AdoptionEntry] = {}
    seen_canonical: dict[str, AdoptionEntry] = {}
    for entry in ordered:
        if entry.paper_dir in seen_dirs:
            raise ValueError(f"duplicate paper_dir in manifest: {entry.paper_dir}")
        # sources.canonical_id is UNIQUE, so two directories claiming one paper
        # must be named here rather than colliding inside the commit.
        clash = seen_canonical.get(entry.canonical_id)
        if clash is not None:
            raise ValueError(
                "duplicate canonical source "
                f"{entry.canonical_id} claimed by {clash.paper_dir} "
                f"and {entry.paper_dir}"
            )
        seen_dirs[entry.paper_dir] = entry
        seen_canonical[entry.canonical_id] = entry
    return AdoptionManifest(entries=tuple(ordered))


@dataclass(frozen=True)
class CorpusReadResult:
    """What a corpus read adopted, and what it deliberately did not."""

    manifest: AdoptionManifest
    skipped: tuple[tuple[str, str], ...]

    @property
    def skipped_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.skipped:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def read_corpus(*, database: Path, corpus_root: Path) -> CorpusReadResult:
    """Enumerate a copied research database and corpus into one manifest.

    Read-only in the strict sense: the database is opened `immutable`, so it
    creates no sidecars even though the engine writes it in WAL mode. That is
    sound only when the file is the whole state, which a checkpointed copy is
    and a copy taken mid-write is not -- so an un-checkpointed log is refused
    rather than silently read as a shorter database.
    """

    rows = _read_paper_rows(database)
    entries: list[AdoptionEntry] = []
    skipped: list[tuple[str, str]] = []
    for row in rows:
        paper_dir = str(row["paper_dir"])
        if not (corpus_root / paper_dir).is_dir():
            # A radar stub is an index-only signal: the engine noticed a paper
            # and never ingested it, so there is no directory BY DESIGN and
            # nothing to adopt -- claiming it as an existing source would
            # promise content the engine does not have. Any OTHER row without
            # a directory was ingested once and lost it, which is a corpus
            # anomaly the operator has to see rather than a normal skip.
            reason = (
                "radar_stub"
                if paper_dir.startswith(_RADAR_STUB_PREFIX)
                else "missing_directory"
            )
            skipped.append((paper_dir, reason))
            continue
        entries.append(_read_entry(row, corpus_root=corpus_root))
    return CorpusReadResult(
        manifest=build_manifest(entries), skipped=tuple(skipped)
    )


def _read_paper_rows(database: Path) -> list[sqlite3.Row]:
    """Open a copied corpus database read-only, refusing an un-checkpointed one.

    The precondition is deliberately strict and stays strict: an `immutable=1`
    read of a database with an outstanding write-ahead log silently returns the
    pre-commit row set, so relaxing it would turn a crashed ingest into a
    manifest that looks complete.
    """

    if not database.is_file():
        raise CorpusReadError(f"research database is missing: {database}")
    log = database.with_name(database.name + "-wal")
    try:
        checkpointed = not log.exists() or log.stat().st_size == 0
    except OSError:
        checkpointed = False
    if not checkpointed:
        raise CorpusReadError("research database is not checkpointed")
    uri = f"file:{quote(str(database.resolve(strict=True)), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT paper_dir, title, arxiv_id FROM papers ORDER BY paper_dir"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CorpusReadError(f"research database is unreadable: {exc}") from exc
    finally:
        connection.close()


def read_corpus_subset(
    *, database: Path, corpus_root: Path, paper_dirs: Iterable[str]
) -> CorpusReadResult:
    """Enumerate exactly the named corpus directories into one manifest.

    A capture adopts what one ingest produced, not the whole corpus: reading all
    of it would re-digest thousands of directories on every capture and would
    refuse the commit whenever some unrelated pair of directories collided. The
    reader precondition and the identity refusals are the same ones
    `read_corpus` applies -- only the row set is narrower.
    """

    wanted = list(dict.fromkeys(paper_dirs))
    if not wanted:
        raise ValueError("a corpus subset must name at least one directory")
    rows = {str(row["paper_dir"]): row for row in _read_paper_rows(database)}
    entries: list[AdoptionEntry] = []
    skipped: list[tuple[str, str]] = []
    for paper_dir in wanted:
        row = rows.get(paper_dir)
        if row is None:
            # The engine reported a directory the database does not name. That
            # is an incomplete ingest, not a normal skip.
            raise CorpusReadError(f"corpus database does not name: {paper_dir}")
        if not (corpus_root / paper_dir).is_dir():
            reason = (
                "radar_stub"
                if paper_dir.startswith(_RADAR_STUB_PREFIX)
                else "missing_directory"
            )
            skipped.append((paper_dir, reason))
            continue
        entries.append(_read_entry(row, corpus_root=corpus_root))
    if not entries:
        # Every named directory was skipped, so the copy is not in the state the
        # ingest reported it was. `build_manifest` refuses an empty list with a
        # bare `ValueError`, which escapes `import_source`, `run_once` and the
        # tick untyped; ⟦AMD-5⟧ makes this the reader's own refusal instead.
        # `build_manifest` itself is left alone: `read_corpus` shares it under
        # S1's whole-corpus contract, where an empty corpus is a different fact.
        raise CorpusReadError(
            f"corpus subset named {len(wanted)} directories and adopted none"
        )
    return CorpusReadResult(manifest=build_manifest(entries), skipped=tuple(skipped))


def _read_entry(row: sqlite3.Row, *, corpus_root: Path) -> AdoptionEntry:
    paper_dir = str(row["paper_dir"])
    files = _directory_digests(corpus_root / paper_dir)
    if not files:
        raise CorpusReadError(f"corpus directory is empty: {paper_dir}")
    arxiv_id = row["arxiv_id"]
    if arxiv_id:
        authority, authority_id = "arxiv", str(arxiv_id)
    else:
        # No external authority names this source, so its own content does.
        authority, authority_id = "sha256", _primary_digest(files, paper_dir)
    return AdoptionEntry(
        paper_dir=paper_dir,
        authority=authority,
        authority_id=authority_id,
        official_title=str(row["title"]),
        content_digest=_tree_digest(files),
    )


def _directory_digests(directory: Path) -> dict[str, str]:
    """Digest every file under one corpus directory, by relative path."""

    result: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        result[path.relative_to(directory).as_posix()] = _file_digest(path)
    return result


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _primary_digest(files: dict[str, str], paper_dir: str) -> str:
    for name in _PRIMARY_ARTIFACTS:
        if name in files:
            return files[name]
    raise CorpusReadError(f"corpus directory has no primary artifact: {paper_dir}")


def _tree_digest(files: dict[str, str]) -> str:
    """Digest the whole directory, so a partial copy cannot look complete."""

    return hashlib.sha256(
        json.dumps(files, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        .encode("utf-8")
    ).hexdigest()
