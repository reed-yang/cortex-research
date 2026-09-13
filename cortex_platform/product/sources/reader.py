"""Bounded, read-only knowledge access through adopted Control identities."""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import stat
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


class SourceContentUnavailable(RuntimeError):
    """Storage, adoption, or content version cannot satisfy the request."""

    category = "source_content_unavailable"


class SourceQueryInvalid(ValueError):
    """A public read/search request exceeds or violates the closed contract."""

    category = "source_query_invalid"


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
                    before = os.stat(KINDS[kind], dir_fd=directory, follow_symlinks=False)
                    raw = _read_regular(
                        directory, KINDS[kind],
                        limit=min(root["max_bytes"], MAX_FILE_BYTES),
                        require_private=False,
                    )
                    after = os.stat(KINDS[kind], dir_fd=directory, follow_symlinks=False)
                    if _identity(before) != _identity(after):
                        raise SourceContentUnavailable("source content changed during read")
                raw.decode("utf-8", errors="strict")
                if raw.count(b"\n") + bool(raw and not raw.endswith(b"\n")) > MAX_FILE_LINES:
                    raise SourceContentUnavailable("source content row limit exceeded")
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

    def search(self, query, limit=10) -> dict:
        from .search import search_knowledge

        return search_knowledge(self, query, limit=limit)


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
