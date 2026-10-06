"""Bounded, read-only knowledge access through adopted Control identities."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..artifacts.materializer import _open_directory, _read_regular
from .adoption import decode_engine_ref, encode_engine_ref

if TYPE_CHECKING:
    from ..control import ControlStore

ROOT_ID = "research-corpus"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 20_000
MAX_PAGE_LINES = 2_000
MAX_FILE_LINES = 200_000
MAX_SOURCES = 10_000
KINDS = {"notes": "notes.md", "full_text": "full_text.md", "grounding": "grounding.md"}
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_ASSET_PATH_BYTES = 512
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c|2e|00)", re.IGNORECASE)


class SourceContentUnavailable(RuntimeError):
    """Storage, adoption, or content version cannot satisfy the request."""

    category = "source_content_unavailable"


class SourceQueryInvalid(ValueError):
    """A public read/search request exceeds or violates the closed contract."""

    category = "source_query_invalid"


class SourceDocumentTooLarge(RuntimeError):
    """The retained document exceeds the whole-document read limit."""

    category = "source_document_too_large"


class SourceAssetInvalid(ValueError):
    """An asset reference is outside the closed relative-path grammar."""

    category = "source_asset_invalid"


class SourceAssetUnavailable(RuntimeError):
    """The source, its own assets directory, or the referenced file is unreadable."""

    category = "source_asset_unavailable"


class SourceAssetTooLarge(RuntimeError):
    """The referenced asset exceeds the root and asset byte limits."""

    category = "source_asset_too_large"


class SourceAssetUnsupported(RuntimeError):
    """The referenced asset is not a PNG, JPEG, GIF or WebP image."""

    category = "source_asset_unsupported"


def _integer_limit(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SourceQueryInvalid("limit is out of range")
    return value


def _open_path_directory(path: Path) -> int:
    # Reuse the artifact reader's descriptor-relative, no-follow traversal,
    # without requiring copied corpus directories to have artifact-only modes.
    if not path.is_absolute() or ".." in path.parts:
        raise SourceContentUnavailable("knowledge root is invalid")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = _open_directory(fd, part, create=False)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _directory(path: Path):
    fd = _open_path_directory(path)
    try:
        yield fd
        verified = _open_path_directory(path)
        try:
            before, after = os.fstat(fd), os.fstat(verified)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise SourceContentUnavailable("knowledge directory changed during read")
        finally:
            os.close(verified)
    finally:
        os.close(fd)


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_checked(directory: int, name: str, limit: int) -> bytes:
    # A regular single-link file, opened without following links, whose
    # identity did not change across the read.
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    raw = _read_regular(directory, name, limit=limit, require_private=False)
    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if _identity(before) != _identity(after):
        raise SourceContentUnavailable("source content changed during read")
    return raw


def _check_text(raw: bytes) -> None:
    raw.decode("utf-8", errors="strict")
    if raw.count(b"\n") + bool(raw and not raw.endswith(b"\n")) > MAX_FILE_LINES:
        raise SourceContentUnavailable("source content row limit exceeded")


def _asset_reference(path: object) -> tuple[str | None, tuple[str, ...]]:
    """Parse a stored image reference into (named paper directory, segments).

    Only `assets/...`, `./assets/...` and `papers/<dir>/assets/...` parse; the
    caller decides whether `<dir>` is the source's own binding directory.
    """
    if not isinstance(path, str) or not path:
        raise SourceAssetInvalid("asset path is invalid")
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        raise SourceAssetInvalid("asset path is invalid") from None
    if (
        size > MAX_ASSET_PATH_BYTES
        or "\\" in path
        or _ENCODED_SEPARATOR.search(path)
        or any(unicodedata.category(char).startswith("C") for char in path)
    ):
        raise SourceAssetInvalid("asset path is invalid")
    dotted = path.startswith("./")
    segments = tuple((path[2:] if dotted else path).split("/"))
    if any(segment in {"", ".", ".."} for segment in segments):
        raise SourceAssetInvalid("asset path is invalid")
    if segments[0] == "assets" and len(segments) > 1:
        return None, segments[1:]
    if not dotted and len(segments) > 3 and segments[0] == "papers" and segments[2] == "assets":
        return segments[1], segments[3:]
    raise SourceAssetInvalid("asset path is invalid")


def _image_media_type(raw: bytes) -> str | None:
    # Decided by signature only; the stored file name is never trusted.
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _begin_read(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    # Bound pathological FTS expressions and corrupt/unindexed schemas.
    budget = 2_000

    def progress() -> int:
        nonlocal budget
        budget -= 1
        return int(budget <= 0)

    connection.set_progress_handler(progress, 10_000)
    connection.execute("BEGIN")


@contextmanager
def _control_database(path: Path):
    """Read live Control WAL through SQLite, allowing only SHM coordination.

    Unlike the copied corpus, Control changes normally between requests. Pin
    its file and directory identity, not its size or timestamps. SQLite needs
    the real pathname to locate WAL; /dev/fd immutable aliases cannot do that.
    """
    with _directory(path.parent) as parent, ExitStack() as descriptors:
        def identity(info):
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SourceContentUnavailable("Control database is unavailable")
            return info.st_dev, info.st_ino, info.st_mode, info.st_uid

        before = identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        descriptors.callback(os.close, descriptor)

        def verify():
            verified = _open_path_directory(path.parent)
            try:
                old, current = os.fstat(parent), os.fstat(verified)
                if (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino):
                    raise SourceContentUnavailable("Control directory changed during read")
                if (
                    identity(os.fstat(descriptor)) != before
                    or identity(os.stat(path.name, dir_fd=verified, follow_symlinks=False)) != before
                ):
                    raise SourceContentUnavailable("Control database changed during read")
                for suffix in ("-wal", "-journal", "-shm"):
                    try:
                        sidecar = os.stat(path.name + suffix, dir_fd=verified, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    identity(sidecar)
            finally:
                os.close(verified)

        verify()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            verify()
            _begin_read(connection)
            yield connection
            verify()
        finally:
            connection.close()


@contextmanager
def _database(path: Path):
    """Open a copied research database without initialization or sidecars.

    The copy must be checkpointed. A normal mode=ro WAL connection can still
    create or update shared memory; immutable avoids that, and outstanding logs
    must be refused instead of silently returning stale rows.
    """
    with _directory(path.parent) as parent, ExitStack() as descriptors:
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SourceContentUnavailable("knowledge database is unavailable")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        descriptors.callback(os.close, descriptor)
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise SourceContentUnavailable("knowledge database changed during open")
        for suffix in ("-wal", "-journal", "-shm"):
            try:
                sidecar = os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(sidecar.st_mode) or (sidecar.st_size and suffix != "-shm"):
                raise SourceContentUnavailable("knowledge database is not checkpointed")
        # SQLite opens the already verified inode, never re-resolves an attacker
        # replaceable corpus path. The POSIX /dev/fd alias is private to this call.
        uri = f"file:/dev/fd/{descriptor}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            _begin_read(connection)
            yield connection
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _identity(before) != _identity(after) or _identity(before) != _identity(os.fstat(descriptor)):
                raise SourceContentUnavailable("knowledge database changed during read")
            for suffix in ("-wal", "-journal"):
                try:
                    info = os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size:
                    raise SourceContentUnavailable("knowledge database changed during read")
        finally:
            connection.close()


class SourceKnowledgeReader:
    """Read adopted source text and lexical evidence; never perform ingestion."""

    def __init__(self, store: ControlStore, *, redact_line: Callable[[str], bool] | None = None) -> None:
        self._store = store
        self._redact_line = redact_line

    @staticmethod
    def _registration(control, source_id):
        root = control.execute(
            "SELECT * FROM asset_roots WHERE root_id = ? AND enabled = 1", (ROOT_ID,)
        ).fetchone()
        if root is None:
            raise SourceContentUnavailable("knowledge root is unavailable")
        params: list = [ROOT_ID]
        condition = ""
        if source_id is not None:
            condition = " AND s.id = ?"
            params.append(source_id)
        rows = control.execute(
            """SELECT DISTINCT s.id, s.canonical_id, s.official_title,
                      s.engine_ref, s.revision, s.import_state, e.paper_dir
               FROM sources s
               JOIN adoption_entries e ON e.source_id = s.id
                    AND e.engine_ref = s.engine_ref
               JOIN adoption_manifests m ON m.manifest_id = e.manifest_id
               WHERE m.corpus_root_id = ? AND s.source_kind = 'paper'
                 AND s.import_state IN ('existing', 'imported')"""
            + condition + " ORDER BY s.canonical_id, s.id, e.paper_dir LIMIT ?",
            (*params, MAX_SOURCES + 1),
        ).fetchall()
        if len(rows) > MAX_SOURCES:
            raise SourceContentUnavailable("knowledge source row limit exceeded")
        sources = {}
        for row in rows:
            paper_dir = decode_engine_ref(row["engine_ref"])
            if (
                paper_dir != row["paper_dir"]
                or encode_engine_ref(paper_dir) != row["engine_ref"]
                or any(char in paper_dir for char in ("/", "\\", "\0"))
                or paper_dir in {".", ".."}
                or paper_dir in sources
            ):
                raise SourceContentUnavailable("source identity is inconsistent")
            sources[paper_dir] = dict(row)
        if source_id is not None and len(sources) != 1:
            raise SourceContentUnavailable("source is not adopted in this root")
        return dict(root), sources

    @contextmanager
    def _registered(self, source_id: str | None = None):
        # Existing ControlStore getters use write-capable connections and lack
        # this bounded adoption/root join. Do not initialize or migrate here.
        with _control_database(self._store.path.absolute()) as control:
            root, sources = self._registration(control, source_id)
            path = Path(root["private_path"])
            with _directory(path):
                yield root, path, sources
            # End the original snapshot: checking again inside it would miss a
            # revocation committed to WAL during file/index access. This fresh
            # authorization snapshot is the read's authorization linearization
            # point; later commits cannot recall an already delivered response.
            control.execute("ROLLBACK")
            control.execute("BEGIN")
            current_root, current_sources = self._registration(control, source_id)
            if current_root != root or any(
                current_sources.get(paper_dir) != source
                for paper_dir, source in sources.items()
            ):
                raise SourceContentUnavailable("source authorization changed during read")

    def _project_evidence(self, text: str) -> str:
        if self._redact_line is None:
            return text
        return "\n".join(
            "[redacted]" if self._redact_line(line) else line
            for line in text.split("\n")
        )

    def _project_page(self, raw: bytes, start: int, end: int) -> str:
        # Classify each COMPLETE original line before projecting its overlap
        # with a page, including cursors forged into the middle of that line.
        # Cursor offsets and UTF-8 boundaries always refer to original bytes.
        line_start = raw.rfind(b"\n", 0, start) + 1
        parts = []
        while line_start < end:
            line_end = raw.find(b"\n", line_start)
            if line_end < 0:
                line_end = len(raw)
            fragment = raw[max(start, line_start):min(end, line_end)]
            if self._redact_line(raw[line_start:line_end].decode("utf-8")):
                # A full marker may not fit a tiny legal page. Never expand
                # beyond the original fragment's byte budget or expose bytes.
                parts.append("[redacted]" if len(fragment) >= 10 else "*" * len(fragment))
            else:
                parts.append(fragment.decode("utf-8"))
            if line_end < end:
                parts.append("\n")
            line_start = line_end + 1
        return "".join(parts)

    def read(self, source_id, kind="notes", cursor=None, limit=20000) -> dict:
        limit = _integer_limit(limit, MAX_PAGE_BYTES)
        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            raise SourceQueryInvalid("source_id is invalid")
        if not isinstance(kind, str) or kind not in KINDS:
            raise SourceQueryInvalid("content kind is invalid")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) != 98):
            raise SourceQueryInvalid("cursor is invalid")
        try:
            with self._registered(source_id) as (root, path, sources):
                paper_dir, source = next(iter(sources.items()))
                with _directory(path / paper_dir) as directory:
                    raw = _read_checked(
                        directory, KINDS[kind], min(root["max_bytes"], MAX_FILE_BYTES)
                    )
                _check_text(raw)
                digest = hashlib.sha256(raw).hexdigest()
                binding = hashlib.sha256(
                    repr((source_id, source["canonical_id"], kind, digest, root["revision"])).encode()
                ).digest()
                start = _cursor_start(cursor, binding, len(raw))
                end = min(start + limit, len(raw))
                # Bound line count and preserve every byte, including long lines.
                lines = raw[start:end].splitlines(keepends=True)
                if len(lines) > MAX_PAGE_LINES:
                    end = start + sum(map(len, lines[:MAX_PAGE_LINES]))
                while end > start:
                    try:
                        text = raw[start:end].decode("utf-8")
                        break
                    except UnicodeDecodeError as error:
                        if error.start == 0 and end - start >= 4:
                            raise SourceQueryInvalid("cursor is not on a UTF-8 boundary") from None
                        end -= 1
                else:
                    if start < len(raw):
                        raise SourceQueryInvalid("limit cannot fit the next UTF-8 character")
                    text = ""
                start_line = raw[:start].count(b"\n") + 1 if raw else 0
                end_line = start_line + text.count("\n") - int(text.endswith("\n")) if text else 0
                if self._redact_line is not None:
                    text = self._project_page(raw, start, end)
                return {
                    "source_id": source["id"], "canonical_id": source["canonical_id"],
                    "kind": kind, "text": text, "content_sha256": digest,
                    "start_line": start_line, "end_line": end_line,
                    "next_cursor": _cursor(end, binding) if end < len(raw) else None,
                }
        except (SourceQueryInvalid, SourceContentUnavailable):
            raise
        except Exception:
            raise SourceContentUnavailable("source content is unavailable") from None

    def document(self, source_id, kind="notes") -> dict:
        """Read one whole retained document once, projected as pages are."""
        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            raise SourceQueryInvalid("source_id is invalid")
        if not isinstance(kind, str) or kind not in KINDS:
            raise SourceQueryInvalid("content kind is invalid")
        too_large = False
        try:
            with self._registered(source_id) as (root, path, sources):
                paper_dir, source = next(iter(sources.items()))
                retained_limit = min(root["max_bytes"], MAX_FILE_BYTES)
                with _directory(path / paper_dir) as directory:
                    info = os.stat(KINDS[kind], dir_fd=directory, follow_symlinks=False)
                    # Refuse by retained size before reading or decoding. A
                    # file over the root limit stays unavailable, as on pages.
                    too_large = (
                        stat.S_ISREG(info.st_mode)
                        and MAX_DOCUMENT_BYTES < info.st_size <= retained_limit
                    )
                    if not too_large:
                        raw = _read_checked(
                            directory, KINDS[kind], min(retained_limit, MAX_DOCUMENT_BYTES)
                        )
                if not too_large:
                    _check_text(raw)
                    text = raw.decode("utf-8")
                    redacted = self._redact_line is not None and any(
                        map(self._redact_line, text.split("\n"))
                    )
                    if redacted:
                        text = self._project_page(raw, 0, len(raw))
                    result = {
                        "source_id": source["id"], "canonical_id": source["canonical_id"],
                        "kind": kind, "text": text,
                        "content_sha256": hashlib.sha256(raw).hexdigest(),
                        "retained_bytes": len(raw), "redacted": redacted,
                    }
        except (SourceQueryInvalid, SourceContentUnavailable):
            raise
        except Exception:
            raise SourceContentUnavailable("source content is unavailable") from None
        # The size refusal describes the file, so it follows the authorization
        # recheck that ends the read.
        if too_large:
            raise SourceDocumentTooLarge("source document exceeds the whole-document limit")
        return result

    def asset(self, source_id, path) -> tuple[str, bytes]:
        """Read one image from the source's own `assets/` directory.

        The directory comes from the authorized adoption binding, never from
        the request; a `papers/<dir>/` prefix may only name that directory.
        """
        prefix, segments = _asset_reference(path)
        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            raise SourceAssetUnavailable("source asset is unavailable")
        refusal = None
        try:
            with self._registered(source_id) as (root, root_path, sources):
                paper_dir = next(iter(sources))
                if prefix is not None and prefix != paper_dir:
                    raise SourceAssetUnavailable("source asset is unavailable")
                limit = min(root["max_bytes"], MAX_ASSET_BYTES)
                with _directory(root_path.joinpath(paper_dir, "assets", *segments[:-1])) as directory:
                    info = os.stat(segments[-1], dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_size > limit:
                        refusal = SourceAssetTooLarge("source asset exceeds its size limit")
                    else:
                        raw = _read_checked(directory, segments[-1], limit)
        except SourceAssetUnavailable:
            raise
        except Exception:
            raise SourceAssetUnavailable("source asset is unavailable") from None
        # Size and type refusals describe the file, so they follow the
        # authorization recheck that ends the read.
        if refusal is not None:
            raise refusal
        media_type = _image_media_type(raw)
        if media_type is None:
            raise SourceAssetUnsupported("source asset is not a supported image")
        return media_type, raw

    def search(self, query, limit=10, *, per_source=None) -> dict:
        from .search import search_knowledge

        return search_knowledge(self, query, limit=limit, per_source=per_source)


def _cursor(offset: int, binding: bytes) -> str:
    payload = b"\x01" + offset.to_bytes(8, "big") + binding
    return base64.urlsafe_b64encode(payload + hashlib.sha256(payload).digest()).decode().rstrip("=")


def _cursor_start(cursor: str | None, binding: bytes, size: int) -> int:
    if cursor is None:
        return 0
    try:
        decoded = base64.b64decode(cursor + "==", altchars=b"-_", validate=True)
        offset = int.from_bytes(decoded[1:9], "big")
        if len(decoded) != 73 or decoded[0] != 1 or _cursor(offset, decoded[9:41]) != cursor:
            raise SourceQueryInvalid("cursor is invalid")
        if decoded[9:41] != binding:
            raise SourceContentUnavailable("source content or cursor binding changed")
        if not 0 < offset < size:
            raise SourceQueryInvalid("cursor offset is invalid")
        return offset
    except (ValueError, TypeError):
        raise SourceQueryInvalid("cursor is invalid") from None
