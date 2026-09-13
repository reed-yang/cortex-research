"""Restart-durable append-only operation ledger for managed workers."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path


_LEDGER_NAME = "operations.ledger.jsonl"
_QUARANTINE_NAME = "operations.ledger.quarantine.json"
_LOCK_FILE = "worker.lock"
_RECORD_FIELDS = {
    "schema_version",
    "operation_id",
    "kind",
    "phase",
    "request_digest",
    "result_digest",
    "recorded_at_monotonic",
}
_QUARANTINE_FIELDS = {"schema_version", "offset", "length", "fragment_sha256"}
_PHASES = {"in_progress", "committed", "failed", "uncertain"}
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LedgerUnavailable(RuntimeError):
    """The persistent operation evidence cannot be opened safely."""


class OperationConflict(RuntimeError):
    """An operation identifier conflicts with durable ledger state."""


@dataclass(frozen=True)
class OperationStatus:
    state: str
    request_digest: str | None = None
    result_digest: str | None = None


@dataclass(frozen=True)
class _Entry:
    operation_id: str
    kind: str
    phase: str
    request_digest: str
    result_digest: str | None


@dataclass(frozen=True)
class _Quarantine:
    offset: int
    length: int
    fragment_sha256: str


class OperationLedger:
    """Append operation transitions only after they are durable on disk."""

    def __init__(
        self,
        path: Path,
        descriptor: int,
        lock_descriptor: int,
        directory_descriptor: int,
        entries: dict[str, _Entry],
        *,
        quarantined: bool,
        needs_newline: bool,
    ) -> None:
        self.path = path
        self._descriptor = descriptor
        self._lock_descriptor = lock_descriptor
        self._directory_descriptor = directory_descriptor
        self._entries = entries
        self.quarantined = quarantined
        self._needs_newline = needs_newline
        self._closed = False

    @staticmethod
    def checkpoint_hook(_phase: str) -> None:
        """Fault-injection seam called only after the matching append is durable."""

    @classmethod
    def open(cls, state_dir: Path) -> OperationLedger:
        state_dir = Path(state_dir)
        directory_descriptor = _prepare_state_dir(state_dir)
        try:
            _verify_directory_binding(state_dir, directory_descriptor)
            lock_descriptor = _open_lock(directory_descriptor)
        except BaseException:
            os.close(directory_descriptor)
            raise
        path = state_dir / _LEDGER_NAME
        try:
            descriptor, created = _open_ledger(directory_descriptor)
        except BaseException:
            os.close(lock_descriptor)
            os.close(directory_descriptor)
            raise
        try:
            if created:
                _fsync_directory(directory_descriptor)
            data = _read_all(descriptor)
            quarantine = _load_quarantine(directory_descriptor)
            replay_data = _exclude_quarantine(data, quarantine) if quarantine else data
            entries, torn, needs_newline = _replay(replay_data)
            if torn is not None:
                if quarantine is not None:
                    raise LedgerUnavailable("ledger contains a second torn fragment")
                offset, fragment = torn
                _write_quarantine(directory_descriptor, offset, fragment)
                _append_bytes(descriptor, b"\n")
                quarantine = _Quarantine(
                    offset=offset,
                    length=len(fragment),
                    fragment_sha256=hashlib.sha256(fragment).hexdigest(),
                )
                needs_newline = False
            ledger = cls(
                path,
                descriptor,
                lock_descriptor,
                directory_descriptor,
                entries,
                quarantined=quarantine is not None,
                needs_newline=needs_newline,
            )
            _verify_directory_binding(state_dir, directory_descriptor)
            for entry in tuple(entries.values()):
                if entry.phase == "in_progress":
                    ledger._append(
                        _Entry(
                            operation_id=entry.operation_id,
                            kind=entry.kind,
                            phase="uncertain",
                            request_digest=entry.request_digest,
                            result_digest=None,
                        ),
                        "uncertain_recorded",
                    )
            return ledger
        except BaseException:
            os.close(descriptor)
            os.close(lock_descriptor)
            os.close(directory_descriptor)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)
        os.close(self._lock_descriptor)
        os.close(self._directory_descriptor)

    def begin(self, operation_id: str, kind: str, request_digest: str) -> str:
        self._require_open()
        _validate_identity(operation_id, kind, request_digest)
        existing = self._entries.get(operation_id)
        if existing is not None:
            if existing.request_digest != request_digest:
                raise OperationConflict("operation identifier has a different request digest")
            return "duplicate"
        self._append(
            _Entry(operation_id, kind, "in_progress", request_digest, None),
            "begin_recorded",
        )
        return "accepted"

    def finish(self, operation_id: str, outcome: str, result_digest: str) -> None:
        self._require_open()
        if outcome not in {"committed", "failed"}:
            raise OperationConflict("operation outcome is invalid")
        if not _SHA256.fullmatch(result_digest):
            raise OperationConflict("operation result digest is invalid")
        existing = self._entries.get(operation_id)
        if existing is None or existing.phase not in {"in_progress", "uncertain"}:
            raise OperationConflict("operation is not finishable")
        self._append(
            _Entry(
                operation_id,
                existing.kind,
                outcome,
                existing.request_digest,
                result_digest,
            ),
            "finish_recorded",
        )

    def status(self, operation_id: str) -> OperationStatus:
        self._require_open()
        existing = self._entries.get(operation_id)
        if existing is None:
            return OperationStatus("unknown")
        return OperationStatus(
            existing.phase,
            request_digest=existing.request_digest,
            result_digest=existing.result_digest,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise LedgerUnavailable("operation ledger is closed")

    def _append(self, entry: _Entry, checkpoint: str) -> None:
        record = {
            "schema_version": 1,
            "operation_id": entry.operation_id,
            "kind": entry.kind,
            "phase": entry.phase,
            "request_digest": entry.request_digest,
            "result_digest": entry.result_digest,
            "recorded_at_monotonic": time.monotonic(),
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if self._needs_newline:
            payload = b"\n" + payload
        _append_bytes(self._descriptor, payload)
        self._needs_newline = False
        self._entries[entry.operation_id] = entry
        self.checkpoint_hook(checkpoint)


def _prepare_state_dir(state_dir: Path) -> int:
    try:
        state_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise LedgerUnavailable("ledger state directory is unavailable") from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(state_dir, flags)
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
            raise LedgerUnavailable("ledger state directory is unsafe")
    except BaseException as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        if isinstance(exc, LedgerUnavailable):
            raise
        raise LedgerUnavailable("ledger state directory is unavailable") from exc
    return descriptor


def _verify_directory_binding(state_dir: Path, directory_descriptor: int) -> None:
    try:
        path_details = os.lstat(state_dir)
        descriptor_details = os.fstat(directory_descriptor)
    except OSError as exc:
        raise LedgerUnavailable("ledger state directory is unavailable") from exc
    if (
        not stat.S_ISDIR(path_details.st_mode)
        or path_details.st_uid != os.geteuid()
        or (path_details.st_dev, path_details.st_ino)
        != (descriptor_details.st_dev, descriptor_details.st_ino)
    ):
        raise LedgerUnavailable("ledger state directory binding changed")


def _open_lock(directory_descriptor: int) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    existed = True
    try:
        os.stat(_LOCK_FILE, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        existed = False
    except OSError as exc:
        raise LedgerUnavailable("operation ledger lock is unsafe") from exc
    try:
        descriptor = os.open(_LOCK_FILE, flags, 0o600, dir_fd=directory_descriptor)
        os.fchmod(descriptor, 0o600)
        _validate_private_file(descriptor, "operation ledger lock")
        if not existed:
            _fsync_directory(directory_descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        if isinstance(exc, BlockingIOError) or exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
            raise LedgerUnavailable("operation ledger is owned by another process") from None
        raise LedgerUnavailable("operation ledger lock is unsafe") from exc
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    return descriptor


def _open_ledger(directory_descriptor: int) -> tuple[int, bool]:
    common = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(
            _LEDGER_NAME,
            os.O_RDWR | os.O_APPEND | common,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        try:
            descriptor = os.open(
                _LEDGER_NAME,
                os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | common,
                0o600,
                dir_fd=directory_descriptor,
            )
            created = True
        except OSError as exc:
            raise LedgerUnavailable("ledger file is unsafe") from exc
    except OSError as exc:
        raise LedgerUnavailable("ledger file is unsafe") from exc
    try:
        _validate_private_file(descriptor, "ledger file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, created


def _open_private_read(directory_descriptor: int, name: str) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LedgerUnavailable("quarantine sidecar is unsafe") from exc
    try:
        _validate_private_file(descriptor, "quarantine sidecar")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_private_file(descriptor: int, label: str) -> None:
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise LedgerUnavailable(f"{label} is unsafe")


def _load_quarantine(directory_descriptor: int) -> _Quarantine | None:
    descriptor = _open_private_read(directory_descriptor, _QUARANTINE_NAME)
    if descriptor is None:
        return None
    try:
        raw_bytes = _read_all(descriptor)
    finally:
        os.close(descriptor)
    try:
        raw = json.loads(raw_bytes, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LedgerUnavailable("quarantine sidecar is malformed") from exc
    if not isinstance(raw, dict) or set(raw) != _QUARANTINE_FIELDS:
        raise LedgerUnavailable("quarantine sidecar fields do not match schema")
    if raw["schema_version"] != 1 or type(raw["schema_version"]) is not int:
        raise LedgerUnavailable("quarantine sidecar schema version is invalid")
    offset = raw["offset"]
    length = raw["length"]
    digest = raw["fragment_sha256"]
    if type(offset) is not int or offset < 0 or type(length) is not int or length <= 0:
        raise LedgerUnavailable("quarantine sidecar range is invalid")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise LedgerUnavailable("quarantine sidecar digest is invalid")
    return _Quarantine(offset, length, digest)


def _exclude_quarantine(data: bytes, quarantine: _Quarantine) -> bytes:
    start = quarantine.offset
    end = start + quarantine.length
    if (
        end >= len(data)
        or (start > 0 and data[start - 1 : start] != b"\n")
        or data[end : end + 1] != b"\n"
        or hashlib.sha256(data[start:end]).hexdigest() != quarantine.fragment_sha256
    ):
        raise LedgerUnavailable("quarantine sidecar does not match ledger evidence")
    try:
        _parse_record(data[start:end])
    except LedgerUnavailable:
        pass
    else:
        raise LedgerUnavailable("quarantine sidecar covers a valid ledger record")
    return data[:start] + data[end + 1 :]


def _write_quarantine(directory_descriptor: int, offset: int, fragment: bytes) -> None:
    marker = {
        "schema_version": 1,
        "offset": offset,
        "length": len(fragment),
        "fragment_sha256": hashlib.sha256(fragment).hexdigest(),
    }
    payload = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            _QUARANTINE_NAME,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(directory_descriptor)
    except OSError as exc:
        raise LedgerUnavailable("quarantine sidecar cannot be recorded") from exc


def _append_bytes(descriptor: int, payload: bytes) -> None:
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError as exc:
        raise LedgerUnavailable("ledger append failed") from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("short write")
        written += count


def _fsync_directory(directory_descriptor: int) -> None:
    try:
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise LedgerUnavailable("ledger directory cannot be synchronized") from exc


def _read_all(descriptor: int) -> bytes:
    try:
        size = os.fstat(descriptor).st_size
        return os.pread(descriptor, size, 0)
    except OSError as exc:
        raise LedgerUnavailable("ledger evidence cannot be read") from exc


def _replay(data: bytes) -> tuple[dict[str, _Entry], tuple[int, bytes] | None, bool]:
    entries: dict[str, _Entry] = {}
    offset = 0
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        terminated = line.endswith(b"\n")
        payload = line[:-1] if terminated else line
        if not payload:
            raise LedgerUnavailable("ledger contains an empty record")
        try:
            entry = _parse_record(payload)
        except LedgerUnavailable:
            if index == len(lines) - 1 and not terminated:
                return entries, (offset, payload), False
            raise LedgerUnavailable("ledger contains a malformed non-quarantined line") from None
        entries[entry.operation_id] = entry
        offset += len(line)
    needs_newline = bool(lines and not lines[-1].endswith(b"\n"))
    return entries, None, needs_newline


def _parse_record(payload: bytes) -> _Entry:
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LedgerUnavailable("ledger record is malformed") from exc
    if not isinstance(raw, dict) or set(raw) != _RECORD_FIELDS:
        raise LedgerUnavailable("ledger record fields do not match schema")
    if raw["schema_version"] != 1 or type(raw["schema_version"]) is not int:
        raise LedgerUnavailable("ledger schema version is invalid")
    operation_id = raw["operation_id"]
    kind = raw["kind"]
    phase = raw["phase"]
    request_digest = raw["request_digest"]
    result_digest = raw["result_digest"]
    recorded = raw["recorded_at_monotonic"]
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise LedgerUnavailable("ledger operation identifier is invalid")
    if not isinstance(kind, str) or not kind or "\0" in kind:
        raise LedgerUnavailable("ledger operation kind is invalid")
    if phase not in _PHASES:
        raise LedgerUnavailable("ledger operation phase is invalid")
    if not isinstance(request_digest, str) or not _SHA256.fullmatch(request_digest):
        raise LedgerUnavailable("ledger request digest is invalid")
    if phase in {"committed", "failed"}:
        if not isinstance(result_digest, str) or not _SHA256.fullmatch(result_digest):
            raise LedgerUnavailable("ledger result digest is invalid")
    elif result_digest is not None:
        raise LedgerUnavailable("ledger result digest is invalid")
    if not isinstance(recorded, (int, float)) or isinstance(recorded, bool) or recorded < 0:
        raise LedgerUnavailable("ledger timestamp is invalid")
    return _Entry(operation_id, kind, phase, request_digest, result_digest)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _validate_identity(operation_id: str, kind: str, request_digest: str) -> None:
    if not _OPERATION_ID.fullmatch(operation_id):
        raise OperationConflict("operation identifier is invalid")
    if not kind or "\0" in kind:
        raise OperationConflict("operation kind is invalid")
    if not _SHA256.fullmatch(request_digest):
        raise OperationConflict("operation request digest is invalid")
