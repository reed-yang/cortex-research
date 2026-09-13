"""Auditable plan, apply, rollback, and diagnostic operations."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .config import AccessConfig, config_fingerprint

APPLY_APPROVAL = "APPLY_PRIVATE_TAILSCALE_SERVE"
ROLLBACK_APPROVAL = "ROLLBACK_PRIVATE_TAILSCALE_SERVE"
MANIFEST_VERSION = 1
_ROLLBACK_MANIFEST_NAME = "rollback-manifest.json"
_ROLLBACK_LOG_MAGIC = b"CORTEX-ACCESS-ROLLBACK\x00\x01"
_MAX_ROLLBACK_LOG_BYTES = 262_144
_MAX_ROLLBACK_FRAME_BYTES = 16_384
_ROLLBACK_STATES = frozenset(
    {"applying", "failed", "rollback_required", "applied", "rolled_back"}
)
_ROLLBACK_TRANSITIONS = {
    "applying": frozenset({"failed", "rollback_required", "applied", "rolled_back"}),
    "failed": frozenset({"rolled_back"}),
    "rollback_required": frozenset({"rolled_back"}),
    "applied": frozenset({"rolled_back"}),
    "rolled_back": frozenset(),
}
_ROLLBACK_IDENTITY_FIELDS = frozenset(
    {
        "manifest_version",
        "plan_id",
        "config_fingerprint",
        "before_serve_status_sha256",
        "before_was_empty",
        "apply_command",
        "rollback_command",
        "daemon_remote_exposed",
        "funnel_allowed",
    }
)
_ROLLBACK_RECORD_FIELDS = _ROLLBACK_IDENTITY_FIELDS | {"state"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class AccessOperationError(RuntimeError):
    """Raised when an access operation cannot safely proceed."""


class InjectedAccessOperationCrash(RuntimeError):
    """Deterministic interruption at a rollback-log commit boundary."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str]) -> CommandResult: ...


@dataclass(frozen=True)
class GatewayAttestation:
    config_fingerprint: str
    web_access_boundary_verified: bool


class StandardCommandRunner:
    """Run an explicit argument vector without a shell."""

    def run(self, arguments: Sequence[str]) -> CommandResult:
        try:
            completed = subprocess.run(
                list(arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class DoctorReport:
    status: str
    provider: str
    tailscale_available: bool
    tailnet_running: bool
    tailnet_identity_matches: bool
    tailnet_service_tag_matches: bool
    serve_private_https: bool
    funnel_disabled: bool | None
    access_gateway_loopback_reachable: bool
    web_access_boundary_verified: bool
    web_loopback_reachable: bool
    daemon_loopback_reachable: bool
    daemon_remote_exposed: bool | None
    cleanup_deferred_to_p2_devrel: bool
    local_browser_available: bool
    local_browser_status: str
    local_url: str | None
    iphone_pwa_url: str
    same_origin_required: str
    issues: tuple[str, ...]
    remaining_gates: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise AccessOperationError("private-access state directory is unsafe")
    if created:
        path.chmod(0o700)
        metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AccessOperationError("private-access state directory is not private")


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AccessOperationError("private-access directory is unsafe") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise AccessOperationError("private-access directory is unsafe")
    return descriptor


def _normalized_real_path(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(path.expanduser()))))


def _stable_user_anchor(path: Path) -> Path:
    candidate = _normalized_real_path(path)
    while candidate.parent != candidate and os.access(candidate.parent, os.W_OK):
        candidate = candidate.parent
    return candidate


def _reject_symlink_ancestors(path: Path) -> None:
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    for component in reversed((candidate, *candidate.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AccessOperationError(
                "private-access operation lock ancestor is unreadable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AccessOperationError(
                "private-access operation lock ancestor must not be a symlink"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise AccessOperationError(
                "private-access operation lock ancestor is unsafe"
            )


def _trusted_lock_anchor(path: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    anchors = (
        _stable_user_anchor(Path(pwd.getpwuid(os.geteuid()).pw_dir)),
        _stable_user_anchor(Path(tempfile.gettempdir())),
    )
    for anchor in anchors:
        try:
            candidate.relative_to(anchor)
        except ValueError:
            continue
        return anchor
    raise AccessOperationError(
        "private-access operation lock must be under the account or temporary root"
    )


def _open_trusted_anchor(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AccessOperationError("private-access lock anchor is unsafe") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or path.parent == path
        or os.access(path.parent, os.W_OK)
    ):
        os.close(descriptor)
        raise AccessOperationError("private-access lock anchor is unsafe")
    return descriptor


def _verify_directory_binding(
    descriptor: int,
    path: Path,
    *,
    label: str,
    private: bool,
) -> None:
    opened = os.fstat(descriptor)
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AccessOperationError(f"{label} pathname changed") from exc
    forbidden_mode = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & forbidden_mode
        or not stat.S_ISDIR(named.st_mode)
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) & forbidden_mode
        or named.st_dev != opened.st_dev
        or named.st_ino != opened.st_ino
        or (
            not private
            and (path.parent == path or os.access(path.parent, os.W_OK))
        )
    ):
        raise AccessOperationError(f"{label} pathname changed")


def _release_flock(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _verify_opened_directory_at(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    *,
    label: str,
    private: bool,
) -> None:
    opened = os.fstat(descriptor)
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise AccessOperationError(f"{label} pathname changed") from exc
    forbidden_mode = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(opened.st_mode) & forbidden_mode
        or not stat.S_ISDIR(named.st_mode)
        or named.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(named.st_mode) & forbidden_mode
        or named.st_dev != opened.st_dev
        or named.st_ino != opened.st_ino
    ):
        raise AccessOperationError(f"{label} pathname changed")


def _open_private_descendant_directory(
    anchor_descriptor: int,
    anchor_path: Path,
    target_path: Path,
) -> int:
    try:
        relative = target_path.relative_to(anchor_path)
    except ValueError as exc:
        raise AccessOperationError(
            "private-access operation lock parent escaped its anchor"
        ) from exc
    if not relative.parts:
        descriptor = os.dup(anchor_descriptor)
        try:
            _verify_directory_binding(
                descriptor,
                target_path,
                label="private-access operation lock parent",
                private=True,
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    current_descriptor = os.dup(anchor_descriptor)
    try:
        for index, name in enumerate(relative.parts):
            if not name or name in {".", ".."} or "/" in name:
                raise AccessOperationError(
                    "private-access operation lock parent is unsafe"
                )
            created = False
            try:
                next_descriptor = os.open(
                    name,
                    flags,
                    dir_fd=current_descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o700, dir_fd=current_descriptor)
                except OSError as exc:
                    raise AccessOperationError(
                        "private-access operation lock parent could not be created"
                    ) from exc
                os.fsync(current_descriptor)
                try:
                    next_descriptor = os.open(
                        name,
                        flags,
                        dir_fd=current_descriptor,
                    )
                except OSError as exc:
                    raise AccessOperationError(
                        "private-access operation lock parent could not be opened"
                    ) from exc
                created = True
            except OSError as exc:
                raise AccessOperationError(
                    "private-access operation lock parent is unsafe"
                ) from exc
            try:
                if created:
                    os.fchmod(next_descriptor, 0o700)
                    os.fsync(next_descriptor)
                    os.fsync(current_descriptor)
                _verify_opened_directory_at(
                    current_descriptor,
                    name,
                    next_descriptor,
                    label="private-access operation lock parent",
                    private=index == len(relative.parts) - 1,
                )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        _verify_directory_binding(
            current_descriptor,
            target_path,
            label="private-access operation lock parent",
            private=True,
        )
        descriptor = current_descriptor
        current_descriptor = -1
        return descriptor
    finally:
        if current_descriptor >= 0:
            os.close(current_descriptor)


def _verify_private_file(descriptor: int, *, label: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise AccessOperationError(f"{label} is unsafe")
    return metadata


def _verify_file_binding(
    directory_descriptor: int,
    name: str,
    descriptor: int,
    *,
    label: str,
) -> None:
    opened = _verify_private_file(descriptor, label=label)
    try:
        published = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
    except OSError as exc:
        raise AccessOperationError(f"{label} pathname changed") from exc
    if (
        published.st_dev != opened.st_dev
        or published.st_ino != opened.st_ino
        or not stat.S_ISREG(published.st_mode)
        or published.st_uid != os.geteuid()
        or published.st_nlink != 1
        or stat.S_IMODE(published.st_mode) != 0o600
    ):
        raise AccessOperationError(f"{label} pathname changed")


def _read_descriptor(descriptor: int, *, maximum: int, label: str) -> bytes:
    metadata = os.fstat(descriptor)
    if metadata.st_size > maximum:
        raise AccessOperationError(f"{label} is too large")
    contents = bytearray()
    offset = 0
    while offset < metadata.st_size:
        chunk = os.pread(descriptor, min(65_536, metadata.st_size - offset), offset)
        if not chunk:
            break
        contents.extend(chunk)
        offset += len(chunk)
    if len(contents) != metadata.st_size:
        raise AccessOperationError(f"{label} could not be read")
    return bytes(contents)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("private-access write did not make progress")
        offset += written


def _publish_private_json(
    directory_descriptor: int, name: str, value: object
) -> None:
    if not name or name in {".", ".."} or "/" in name:
        raise AccessOperationError("generated file name is unsafe")
    payload = _canonical(value) + b"\n"
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
        read_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            name, create_flags, 0o600, dir_fd=directory_descriptor
        )
    except FileExistsError:
        try:
            descriptor = os.open(name, read_flags, dir_fd=directory_descriptor)
        except OSError as exc:
            raise AccessOperationError("generated file is unsafe") from exc
        try:
            _verify_file_binding(
                directory_descriptor,
                name,
                descriptor,
                label="generated file",
            )
            if _read_descriptor(
                descriptor, maximum=65_536, label="generated file"
            ) != payload:
                raise AccessOperationError("generated file content has changed")
            _verify_file_binding(
                directory_descriptor,
                name,
                descriptor,
                label="generated file",
            )
            return
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AccessOperationError("generated file could not be created safely") from exc
    try:
        os.fchmod(descriptor, 0o600)
        _verify_private_file(descriptor, label="generated file")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        _verify_file_binding(
            directory_descriptor,
            name,
            descriptor,
            label="generated file",
        )
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise AccessOperationError("generated file could not be written safely") from exc
    finally:
        os.close(descriptor)


def _default_global_lock_path() -> Path:
    return _normalized_real_path(
        Path(tempfile.gettempdir())
    ) / f"cortex-private-access-{os.getuid()}.lock"


@contextmanager
def _operation_lock(state_directory: Path, global_lock_path: Path):
    _ensure_private_directory(state_directory)
    global_lock_path = Path(
        os.path.abspath(os.fspath(global_lock_path.expanduser()))
    )
    if not global_lock_path.name or global_lock_path.name in {".", ".."}:
        raise AccessOperationError("private-access operation lock path is unsafe")
    _reject_symlink_ancestors(global_lock_path.parent)
    anchor_path = _trusted_lock_anchor(global_lock_path.parent)
    parent_path = global_lock_path.parent
    create_flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    existing_flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
        existing_flags |= os.O_NOFOLLOW
    with ExitStack() as cleanup:
        anchor_descriptor = _open_trusted_anchor(anchor_path)
        cleanup.callback(os.close, anchor_descriptor)
        cleanup.callback(_release_flock, anchor_descriptor)
        _verify_directory_binding(
            anchor_descriptor,
            anchor_path,
            label="private-access lock anchor",
            private=False,
        )
        try:
            fcntl.flock(anchor_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AccessOperationError(
                "another private-access operation is in progress"
            ) from exc
        _verify_directory_binding(
            anchor_descriptor,
            anchor_path,
            label="private-access lock anchor",
            private=False,
        )
        parent_descriptor = _open_private_descendant_directory(
            anchor_descriptor,
            anchor_path,
            parent_path,
        )
        cleanup.callback(os.close, parent_descriptor)
        _verify_directory_binding(
            parent_descriptor,
            parent_path,
            label="private-access operation lock parent",
            private=True,
        )
        anchor_metadata = os.fstat(anchor_descriptor)
        parent_metadata = os.fstat(parent_descriptor)
        if (anchor_metadata.st_dev, anchor_metadata.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            cleanup.callback(_release_flock, parent_descriptor)
            try:
                fcntl.flock(parent_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AccessOperationError(
                    "another private-access operation is in progress"
                ) from exc
        _verify_directory_binding(
            parent_descriptor,
            parent_path,
            label="private-access operation lock parent",
            private=True,
        )
        created = False
        try:
            try:
                descriptor = os.open(
                    global_lock_path.name,
                    create_flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    global_lock_path.name,
                    existing_flags,
                    dir_fd=parent_descriptor,
                )
        except OSError as exc:
            raise AccessOperationError(
                "private-access operation lock is unsafe"
            ) from exc
        cleanup.callback(os.close, descriptor)
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(parent_descriptor)
        _verify_file_binding(
            parent_descriptor,
            global_lock_path.name,
            descriptor,
            label="private-access operation lock",
        )
        cleanup.callback(_release_flock, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AccessOperationError(
                "another private-access operation is in progress"
            ) from exc
        _verify_file_binding(
            parent_descriptor,
            global_lock_path.name,
            descriptor,
            label="private-access operation lock",
        )
        try:
            yield
        finally:
            _verify_directory_binding(
                anchor_descriptor,
                anchor_path,
                label="private-access lock anchor",
                private=False,
            )
            _verify_directory_binding(
                parent_descriptor,
                parent_path,
                label="private-access operation lock parent",
                private=True,
            )
            _verify_file_binding(
                parent_descriptor,
                global_lock_path.name,
                descriptor,
                label="private-access operation lock",
            )


def _validate_rollback_record(value: object) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != _ROLLBACK_RECORD_FIELDS
        or value.get("manifest_version") != MANIFEST_VERSION
        or value.get("state") not in _ROLLBACK_STATES
        or value.get("before_was_empty") is not True
        or value.get("daemon_remote_exposed") is not False
        or value.get("funnel_allowed") is not False
    ):
        raise AccessOperationError("rollback manifest record is invalid")
    for field in (
        "plan_id",
        "config_fingerprint",
        "before_serve_status_sha256",
    ):
        if not isinstance(value.get(field), str) or _SHA256_RE.fullmatch(
            str(value[field])
        ) is None:
            raise AccessOperationError("rollback manifest identity is invalid")
    apply_command = value.get("apply_command")
    rollback_command = value.get("rollback_command")
    if (
        type(apply_command) is not list
        or not 1 <= len(apply_command) <= 20
        or any(
            not isinstance(argument, str) or not argument or len(argument) > 2_000
            for argument in apply_command
        )
        or rollback_command != ["tailscale", "serve", "off"]
    ):
        raise AccessOperationError("rollback manifest command identity is invalid")
    return value


def _load_rollback_log(
    descriptor: int,
) -> tuple[dict[str, object] | None, int, int]:
    raw = _read_descriptor(
        descriptor,
        maximum=_MAX_ROLLBACK_LOG_BYTES,
        label="rollback manifest",
    )
    file_size = len(raw)
    if not raw:
        return None, 0, 0
    if len(raw) < len(_ROLLBACK_LOG_MAGIC):
        if _ROLLBACK_LOG_MAGIC.startswith(raw):
            return None, 0, file_size
        raise AccessOperationError("rollback manifest header is invalid")
    if raw[: len(_ROLLBACK_LOG_MAGIC)] != _ROLLBACK_LOG_MAGIC:
        raise AccessOperationError("rollback manifest header is invalid")
    current: dict[str, object] | None = None
    identity: dict[str, object] | None = None
    offset = len(_ROLLBACK_LOG_MAGIC)
    last_valid_offset = offset
    while offset < len(raw):
        if len(raw) - offset < 4:
            return current, last_valid_offset, file_size
        frame_length = struct.unpack(">I", raw[offset : offset + 4])[0]
        offset += 4
        if frame_length < 2 or frame_length > _MAX_ROLLBACK_FRAME_BYTES:
            raise AccessOperationError("rollback manifest frame length is invalid")
        frame_end = offset + frame_length
        checksum_end = frame_end + hashlib.sha256().digest_size
        if checksum_end > len(raw):
            return current, last_valid_offset, file_size
        payload = raw[offset:frame_end]
        checksum = raw[frame_end:checksum_end]
        if checksum != hashlib.sha256(payload).digest():
            raise AccessOperationError("rollback manifest checksum is invalid")
        try:
            candidate = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessOperationError("rollback manifest frame is invalid") from exc
        if _canonical(candidate) != payload:
            raise AccessOperationError("rollback manifest frame is not canonical")
        record = _validate_rollback_record(candidate)
        record_identity = {
            field: record[field] for field in _ROLLBACK_IDENTITY_FIELDS
        }
        if current is None:
            if record["state"] != "applying":
                raise AccessOperationError("rollback manifest initial state is invalid")
            identity = record_identity
        else:
            if record_identity != identity:
                raise AccessOperationError("rollback manifest identity changed")
            if record["state"] not in _ROLLBACK_TRANSITIONS[str(current["state"])]:
                raise AccessOperationError("rollback manifest transition is invalid")
        current = record
        offset = checksum_end
        last_valid_offset = offset
    return current, last_valid_offset, file_size


@dataclass
class _RollbackLog:
    directory_path: Path
    directory_descriptor: int
    descriptor: int
    current: dict[str, object] | None
    last_valid_offset: int
    file_size: int
    fault_at: str | None = None

    def verify_binding(self) -> None:
        _verify_directory_binding(
            self.directory_descriptor,
            self.directory_path,
            label="rollback state directory",
            private=True,
        )
        _verify_file_binding(
            self.directory_descriptor,
            _ROLLBACK_MANIFEST_NAME,
            self.descriptor,
            label="rollback manifest",
        )

    def append(self, record: dict[str, object]) -> dict[str, object]:
        record = _validate_rollback_record(record)
        if self.current is None:
            if record["state"] != "applying":
                raise AccessOperationError("rollback manifest initial state is invalid")
        else:
            current_identity = {
                field: self.current[field] for field in _ROLLBACK_IDENTITY_FIELDS
            }
            next_identity = {
                field: record[field] for field in _ROLLBACK_IDENTITY_FIELDS
            }
            if next_identity != current_identity:
                raise AccessOperationError("rollback manifest identity changed")
            if record["state"] not in _ROLLBACK_TRANSITIONS[str(self.current["state"])]:
                raise AccessOperationError("rollback manifest transition is invalid")
        payload = _canonical(record)
        if len(payload) > _MAX_ROLLBACK_FRAME_BYTES:
            raise AccessOperationError("rollback manifest frame is too large")
        prefix = _ROLLBACK_LOG_MAGIC if self.last_valid_offset == 0 else b""
        header = struct.pack(">I", len(payload))
        checksum = hashlib.sha256(payload).digest()
        projected_size = (
            self.last_valid_offset
            + len(prefix)
            + len(header)
            + len(payload)
            + len(checksum)
        )
        if projected_size > _MAX_ROLLBACK_LOG_BYTES:
            raise AccessOperationError("rollback manifest log is too large")
        self.verify_binding()
        if self.file_size != self.last_valid_offset:
            os.ftruncate(self.descriptor, self.last_valid_offset)
        os.lseek(self.descriptor, self.last_valid_offset, os.SEEK_SET)
        if prefix:
            _write_all(self.descriptor, prefix)
        _write_all(self.descriptor, header)
        os.fsync(self.descriptor)
        self._fault("after_rollback_frame_header")
        _write_all(self.descriptor, payload)
        os.fsync(self.descriptor)
        self._fault("after_rollback_frame_body")
        if self.fault_at == "after_rollback_checksum_partial":
            _write_all(self.descriptor, checksum[: len(checksum) // 2])
            os.fsync(self.descriptor)
            raise InjectedAccessOperationCrash("after_rollback_checksum_partial")
        _write_all(self.descriptor, checksum)
        os.fsync(self.descriptor)
        self.verify_binding()
        self.current = record
        self.last_valid_offset = projected_size
        self.file_size = projected_size
        return record

    def _fault(self, point: str) -> None:
        if self.fault_at == point:
            raise InjectedAccessOperationCrash(point)


@contextmanager
def _rollback_log(
    state_directory: Path,
    *,
    create: bool,
    fault_at: str | None = None,
):
    create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    existing_flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
        existing_flags |= os.O_NOFOLLOW
    with ExitStack() as cleanup:
        directory_descriptor = _open_private_directory(state_directory)
        cleanup.callback(os.close, directory_descriptor)
        _verify_directory_binding(
            directory_descriptor,
            state_directory,
            label="rollback state directory",
            private=True,
        )
        created = False
        try:
            if create:
                try:
                    descriptor = os.open(
                        _ROLLBACK_MANIFEST_NAME,
                        create_flags,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                    created = True
                except FileExistsError:
                    descriptor = os.open(
                        _ROLLBACK_MANIFEST_NAME,
                        existing_flags,
                        dir_fd=directory_descriptor,
                    )
            else:
                descriptor = os.open(
                    _ROLLBACK_MANIFEST_NAME,
                    existing_flags,
                    dir_fd=directory_descriptor,
                )
        except OSError as exc:
            raise AccessOperationError(
                "rollback manifest could not be opened safely"
            ) from exc
        cleanup.callback(os.close, descriptor)
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(directory_descriptor)
        _verify_file_binding(
            directory_descriptor,
            _ROLLBACK_MANIFEST_NAME,
            descriptor,
            label="rollback manifest",
        )
        cleanup.callback(_release_flock, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AccessOperationError("rollback manifest is already in use") from exc
        _verify_file_binding(
            directory_descriptor,
            _ROLLBACK_MANIFEST_NAME,
            descriptor,
            label="rollback manifest",
        )
        current, last_valid_offset, file_size = _load_rollback_log(descriptor)
        log = _RollbackLog(
            directory_path=state_directory,
            directory_descriptor=directory_descriptor,
            descriptor=descriptor,
            current=current,
            last_valid_offset=last_valid_offset,
            file_size=file_size,
            fault_at=fault_at,
        )
        try:
            yield log
        finally:
            log.verify_binding()


def _parse_json_output(result: CommandResult, *, operation: str) -> object:
    if result.returncode != 0:
        raise AccessOperationError(f"{operation} failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AccessOperationError(f"{operation} returned invalid JSON") from exc


def _is_empty_serve(value: object) -> bool:
    if value is None or value is False or value == "":
        return True
    if isinstance(value, dict):
        for key in ("TCP", "Web", "Services", "services"):
            configured = value.get(key)
            if isinstance(configured, dict) and configured:
                return False
        return all(_is_empty_serve(item) for item in value.values())
    if isinstance(value, list):
        return all(_is_empty_serve(item) for item in value)
    return False


def _funnel_enabled(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if "funnel" in str(key).casefold() and not _is_empty_serve(item):
                return True
            if _funnel_enabled(item):
                return True
    elif isinstance(value, list):
        return any(_funnel_enabled(item) for item in value)
    return False


def _serve_is_exact_private_gateway(value: object, config: AccessConfig) -> bool:
    if not isinstance(value, dict) or _funnel_enabled(value):
        return False
    if set(value) - {"TCP", "Web", "Services", "Foreground", "AllowFunnel"}:
        return False
    if any(not _is_empty_serve(value.get(key)) for key in ("Foreground", "Services")):
        return False
    tcp = value.get("TCP")
    if not isinstance(tcp, dict) or set(map(str, tcp)) != {"443"}:
        return False
    endpoint = tcp.get("443") or tcp.get(443)
    if not isinstance(endpoint, dict) or endpoint != {"HTTPS": True}:
        return False
    web = value.get("Web")
    expected_host_port = f"{config.public_hostname}:443"
    if not isinstance(web, dict) or set(web) != {expected_host_port}:
        return False
    web_config = web[expected_host_port]
    if not isinstance(web_config, dict) or set(web_config) != {"Handlers"}:
        return False
    handlers = web_config["Handlers"]
    if not isinstance(handlers, dict) or set(handlers) != {"/"}:
        return False
    handler = handlers["/"]
    if not isinstance(handler, dict):
        return False
    return handler == {
        "Proxy": config.access_gateway.url,
        "AcceptAppCaps": [config.identity.app_capability],
    }


def _daemon_target_state(value: object, config: AccessConfig) -> bool | None:
    unknown = False

    def visit(item: object, *, parent_key: str = "") -> bool:
        nonlocal unknown
        if isinstance(item, dict):
            return any(
                visit(child, parent_key=str(key).casefold())
                for key, child in item.items()
            )
        if isinstance(item, list):
            return any(visit(child, parent_key=parent_key) for child in item)
        if not isinstance(item, str) or parent_key not in {
            "proxy",
            "target",
            "tcpforward",
        }:
            return False
        candidate = item if "://" in item else f"tcp://{item}"
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError:
            unknown = True
            return False
        hostname = (parsed.hostname or "").casefold()
        if not hostname or port is None:
            unknown = True
            return False
        if port != config.daemon_upstream.port:
            return False
        if hostname.rstrip(".") == "localhost":
            return True
        ipv4_parts = hostname.split(".")
        if (
            2 <= len(ipv4_parts) <= 4
            and ipv4_parts[0] == "127"
            and all(part.isdigit() and 0 <= int(part) <= 255 for part in ipv4_parts)
        ):
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            unknown = True
            return False

    if visit(value):
        return True
    return None if unknown else False


def _tailnet_state(
    value: object, expected_hostname: str, service_tag: str
) -> tuple[bool, bool, bool]:
    if not isinstance(value, dict):
        return False, False, False
    running = value.get("BackendState") == "Running"
    self_status = value.get("Self")
    dns_name = self_status.get("DNSName") if isinstance(self_status, dict) else None
    identity_matches = (
        isinstance(dns_name, str)
        and dns_name.rstrip(".").casefold() == expected_hostname
    )
    tags = self_status.get("Tags") if isinstance(self_status, dict) else None
    tag_matches = isinstance(tags, list) and service_tag in tags
    return running, identity_matches, tag_matches


def _default_probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _default_gateway_probe(config: AccessConfig) -> GatewayAttestation | None:
    connection = http.client.HTTPConnection(
        config.access_gateway.host,
        config.access_gateway.port,
        timeout=1,
    )
    try:
        connection.request(
            "GET",
            "/.well-known/cortex-private-access/health",
            headers={"Host": config.access_gateway.authority},
        )
        response = connection.getresponse()
        body = response.read(65_537)
        if response.status != 200 or len(body) > 65_536:
            return None
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or payload.get("service") != "cortex-private-access"
            or payload.get("status") != "ok"
        ):
            return None
        fingerprint = payload.get("config_fingerprint")
        boundary = payload.get("web_access_boundary_verified")
        if not isinstance(fingerprint, str) or type(boundary) is not bool:
            return None
        return GatewayAttestation(fingerprint, boundary)
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        return None
    finally:
        connection.close()


class AccessManager:
    def __init__(
        self,
        config: AccessConfig,
        *,
        runner: CommandRunner | None = None,
        binary_available: Callable[[], bool] | None = None,
        probe: Callable[[str, int], bool] = _default_probe,
        gateway_probe: Callable[
            [AccessConfig], GatewayAttestation | None
        ] = _default_gateway_probe,
        composition_gate: Callable[[], bool] | None = None,
        operation_lock_path: Path | None = None,
        rollback_fault_at: str | None = None,
        after_rollback_log_open: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or StandardCommandRunner()
        self.binary_available = binary_available or (
            lambda: shutil.which("tailscale") is not None
        )
        self.probe = probe
        self.gateway_probe = gateway_probe
        self.composition_gate = composition_gate or (lambda: False)
        self.operation_lock_path = operation_lock_path or _default_global_lock_path()
        if rollback_fault_at not in {
            None,
            "after_rollback_frame_header",
            "after_rollback_frame_body",
            "after_rollback_checksum_partial",
        }:
            raise ValueError("rollback_fault_at is invalid")
        self.rollback_fault_at = rollback_fault_at
        self.after_rollback_log_open = after_rollback_log_open

    @property
    def serve_command(self) -> tuple[str, ...]:
        return (
            "tailscale",
            "serve",
            "--bg",
            "--https=443",
            f"--accept-app-caps={self.config.identity.app_capability}",
            self.config.access_gateway.url,
        )

    @property
    def rollback_command(self) -> tuple[str, ...]:
        return ("tailscale", "serve", "off")

    def policy_fragment(self) -> dict[str, object]:
        roles = [
            {"role": role}
            for role in self.config.identity.allowed_capability_roles
        ]
        return {
            "tagOwners": {
                self.config.identity.service_tag: list(self.config.identity.tag_owners)
            },
            "grants": [
                {
                    "src": list(self.config.identity.allowed_sources),
                    "dst": [self.config.identity.service_tag],
                    "ip": ["443"],
                    "app": {self.config.identity.app_capability: roles},
                }
            ],
            "tests": [
                {
                    "src": source,
                    "accept": [f"{self.config.identity.service_tag}:443"],
                }
                for source in self.config.identity.allowed_sources
            ],
        }

    def plan(self) -> dict[str, object]:
        material = {
            "manifest_version": MANIFEST_VERSION,
            "config_fingerprint": config_fingerprint(self.config),
            "provider": "tailscale-serve",
            "public_origin": self.config.public_origin,
            "iphone_pwa_url": self.config.public_origin,
            "same_origin_required": self.config.public_origin,
            "reviewed_policy_sha256": _sha256(self.policy_fragment()),
            "serve_target": self.config.access_gateway.url,
            "web_target": self.config.web_upstream.url,
            "daemon_remote_exposed": False,
            "funnel_allowed": False,
            "activation_allowed": False,
            "validated_scope": "access_boundary_only",
            "commands": {
                "inspect_tailnet": ["tailscale", "status", "--json"],
                "inspect_serve": ["tailscale", "serve", "status", "--json"],
                "apply": list(self.serve_command),
                "rollback": list(self.rollback_command),
            },
            "manual_gates": [
                "complete_p2_devrel_compose",
                "review_tailnet_policy_diff",
                "confirm_https_enabled_for_tailnet",
                "confirm_service_node_has_expected_tag",
                "start_loopback_web_and_access_gateway",
                "confirm_session_bootstrap_secret_reference_is_resolvable",
                "verify_web_fixed_origin_and_access_bootstrap_contract",
                "physical_iphone_safari_and_installed_pwa_acceptance",
            ],
        }
        return {"plan_id": _sha256(material), **material}

    def generate(self, output_directory: Path) -> tuple[Path, Path]:
        policy_path = output_directory / "tailnet-policy.fragment.json"
        plan_path = output_directory / "access-plan.json"
        _ensure_private_directory(output_directory)
        directory_descriptor = _open_private_directory(output_directory)
        try:
            _publish_private_json(
                directory_descriptor, policy_path.name, self.policy_fragment()
            )
            _publish_private_json(directory_descriptor, plan_path.name, self.plan())
        finally:
            os.close(directory_descriptor)
        return policy_path, plan_path

    def _run_with_rollback_binding(
        self,
        rollback_log: _RollbackLog | None,
        arguments: Sequence[str],
    ) -> CommandResult:
        if rollback_log is None:
            return self.runner.run(arguments)
        rollback_log.verify_binding()
        try:
            return self.runner.run(arguments)
        finally:
            rollback_log.verify_binding()

    def _tailnet_status(
        self, rollback_log: _RollbackLog | None = None
    ) -> tuple[object, bool, bool, bool]:
        status = _parse_json_output(
            self._run_with_rollback_binding(
                rollback_log, ("tailscale", "status", "--json")
            ),
            operation="tailscale status",
        )
        running, identity_matches, tag_matches = _tailnet_state(
            status,
            self.config.public_hostname,
            self.config.identity.service_tag,
        )
        return status, running, identity_matches, tag_matches

    def _serve_status(self, rollback_log: _RollbackLog | None = None) -> object:
        return _parse_json_output(
            self._run_with_rollback_binding(
                rollback_log, ("tailscale", "serve", "status", "--json")
            ),
            operation="tailscale serve status",
        )

    def doctor(self) -> DoctorReport:
        gateway_attestation = self.gateway_probe(self.config)
        access_reachable = (
            gateway_attestation is not None
            and gateway_attestation.config_fingerprint
            == config_fingerprint(self.config)
        )
        web_boundary_verified = (
            access_reachable
            and gateway_attestation is not None
            and gateway_attestation.web_access_boundary_verified
        )
        web_reachable = self.probe(
            self.config.web_upstream.host, self.config.web_upstream.port
        )
        daemon_reachable = self.probe(
            self.config.daemon_upstream.host, self.config.daemon_upstream.port
        )
        issues: list[str] = []
        available = self.binary_available()
        running = False
        identity_matches = False
        tag_matches = False
        serve_private = False
        serve_status_known = False
        funnel_disabled: bool | None = None
        daemon_remote_exposed: bool | None = None
        if not available:
            issues.append("tailscale_unavailable")
        else:
            try:
                _, running, identity_matches, tag_matches = self._tailnet_status()
                serve_status = self._serve_status()
                serve_status_known = True
                funnel_disabled = not _funnel_enabled(serve_status)
                daemon_remote_exposed = _daemon_target_state(
                    serve_status, self.config
                )
                serve_private = (
                    running
                    and identity_matches
                    and _serve_is_exact_private_gateway(serve_status, self.config)
                )
            except AccessOperationError:
                issues.append("tailscale_status_unavailable")
        if available and not running:
            issues.append("tailnet_not_running")
        if running and not identity_matches:
            issues.append("tailnet_https_identity_mismatch")
        if running and not tag_matches:
            issues.append("tailnet_service_tag_mismatch")
        if funnel_disabled is False:
            issues.append("funnel_must_be_disabled")
        if daemon_remote_exposed is True:
            issues.append("daemon_target_exposed")
        if serve_status_known and daemon_remote_exposed is None:
            issues.append("daemon_target_state_unknown")
        if not access_reachable:
            issues.append("access_gateway_not_verified")
        if not web_boundary_verified:
            issues.append("web_access_boundary_not_verified")
        if not web_reachable:
            issues.append("web_loopback_not_reachable")
        if not daemon_reachable:
            issues.append("daemon_loopback_not_reachable")
        if not self.composition_gate():
            issues.append("complete_cortex_composition_not_verified")
        issues.append("local_browser_front_door_not_composed")
        if available and running and not serve_private:
            issues.append("private_serve_not_verified")

        status = "ready" if not issues else "degraded"
        return DoctorReport(
            status=status,
            provider="tailscale-serve",
            tailscale_available=available,
            tailnet_running=running,
            tailnet_identity_matches=identity_matches,
            tailnet_service_tag_matches=tag_matches,
            serve_private_https=serve_private,
            funnel_disabled=funnel_disabled,
            access_gateway_loopback_reachable=access_reachable,
            web_access_boundary_verified=web_boundary_verified,
            web_loopback_reachable=web_reachable,
            daemon_loopback_reachable=daemon_reachable,
            daemon_remote_exposed=daemon_remote_exposed,
            cleanup_deferred_to_p2_devrel=True,
            local_browser_available=False,
            local_browser_status="blocked_until_p2_devrel_compose",
            local_url=None,
            iphone_pwa_url=self.config.public_origin,
            same_origin_required=self.config.public_origin,
            issues=tuple(dict.fromkeys(issues)),
            remaining_gates=(
                "p2_devrel_compose",
                "local_browser_front_door",
                "tailnet_policy_review",
                "session_secret_resolution",
                "web_fixed_origin_and_access_bootstrap",
                "physical_iphone_pwa_acceptance",
                "multi_user_auth_deferred",
            ),
        )

    def apply(
        self,
        state_directory: Path,
        *,
        approval: str,
        reviewed_policy_sha256: str,
    ) -> dict[str, object]:
        if approval != APPLY_APPROVAL:
            raise AccessOperationError("explicit private Serve approval is required")
        if reviewed_policy_sha256 != _sha256(self.policy_fragment()):
            raise AccessOperationError("reviewed tailnet policy hash does not match")
        with _operation_lock(state_directory, self.operation_lock_path):
            return self._apply_locked(state_directory)

    def _apply_locked(self, state_directory: Path) -> dict[str, object]:
        with _rollback_log(
            state_directory, create=True, fault_at=self.rollback_fault_at
        ) as log:
            if self.after_rollback_log_open is not None:
                self.after_rollback_log_open()
            log.verify_binding()
            manifest = log.current
            if manifest is not None:
                self._validate_manifest_identity(manifest)
                if manifest["state"] in {"failed", "rollback_required", "rolled_back"}:
                    raise AccessOperationError(
                        "an incompatible rollback manifest already exists"
                    )
            composition_ready = self.composition_gate()
            log.verify_binding()
            if not composition_ready:
                raise AccessOperationError(
                    "complete Cortex composition is not certified; apply is disabled"
                )
            gateway_attestation = self.gateway_probe(self.config)
            log.verify_binding()
            if (
                gateway_attestation is None
                or gateway_attestation.config_fingerprint
                != config_fingerprint(self.config)
                or not gateway_attestation.web_access_boundary_verified
            ):
                raise AccessOperationError(
                    "the loopback access gateway and Web security boundary is not ready"
                )
            available = self.binary_available()
            log.verify_binding()
            if not available:
                raise AccessOperationError(
                    "tailscale is unavailable; remote apply cannot proceed"
                )
            log.verify_binding()
            _, running, identity_matches, tag_matches = self._tailnet_status(log)
            if not running or not identity_matches or not tag_matches:
                raise AccessOperationError(
                    "tailnet HTTPS identity or service tag does not match the plan"
                )
            log.verify_binding()
            current = self._serve_status(log)
            if manifest is not None and manifest["state"] == "applied":
                if not _serve_is_exact_private_gateway(current, self.config):
                    raise AccessOperationError(
                        "rollback manifest and Serve state have diverged"
                    )
                return manifest
            if manifest is not None and manifest["state"] == "applying":
                if _serve_is_exact_private_gateway(current, self.config):
                    return log.append({**manifest, "state": "applied"})
                if not _is_empty_serve(current):
                    raise AccessOperationError(
                        "rollback manifest and Serve state have diverged"
                    )
            else:
                if _funnel_enabled(current):
                    raise AccessOperationError(
                        "Funnel is enabled and must be disabled manually"
                    )
                if not _is_empty_serve(current):
                    raise AccessOperationError(
                        "existing Serve configuration requires manual review"
                    )
                manifest = {
                    "manifest_version": MANIFEST_VERSION,
                    "state": "applying",
                    "plan_id": self.plan()["plan_id"],
                    "config_fingerprint": config_fingerprint(self.config),
                    "before_serve_status_sha256": _sha256(current),
                    "before_was_empty": True,
                    "apply_command": list(self.serve_command),
                    "rollback_command": list(self.rollback_command),
                    "daemon_remote_exposed": False,
                    "funnel_allowed": False,
                }
                manifest = log.append(manifest)

            log.verify_binding()
            result = self._run_with_rollback_binding(log, self.serve_command)
            if result.returncode != 0:
                log.append({**manifest, "state": "failed"})
                raise AccessOperationError("tailscale Serve apply failed")
            try:
                log.verify_binding()
                after = self._serve_status(log)
                if not _serve_is_exact_private_gateway(after, self.config):
                    raise AccessOperationError(
                        "applied Serve configuration failed verification"
                    )
            except AccessOperationError:
                log.append({**manifest, "state": "rollback_required"})
                raise
            return log.append({**manifest, "state": "applied"})

    def rollback(
        self, state_directory: Path, *, approval: str
    ) -> dict[str, object]:
        if approval != ROLLBACK_APPROVAL:
            raise AccessOperationError("explicit private Serve rollback approval is required")
        with _operation_lock(state_directory, self.operation_lock_path):
            return self._rollback_locked(state_directory)

    def _rollback_locked(self, state_directory: Path) -> dict[str, object]:
        with _rollback_log(
            state_directory, create=False, fault_at=self.rollback_fault_at
        ) as log:
            if self.after_rollback_log_open is not None:
                self.after_rollback_log_open()
            log.verify_binding()
            manifest = log.current
            if manifest is None:
                raise AccessOperationError("rollback manifest is incomplete")
            self._validate_manifest_identity(manifest)
            available = self.binary_available()
            log.verify_binding()
            if not available:
                raise AccessOperationError("rollback Serve state cannot be verified")
            log.verify_binding()
            current = self._serve_status(log)
            if manifest["state"] == "rolled_back":
                if not _is_empty_serve(current):
                    raise AccessOperationError(
                        "rollback manifest and Serve state have diverged"
                    )
                return manifest
            if manifest["state"] not in {
                "applied",
                "applying",
                "failed",
                "rollback_required",
            }:
                raise AccessOperationError("rollback manifest is not safe to execute")
            if _is_empty_serve(current):
                return log.append({**manifest, "state": "rolled_back"})
            if not _serve_is_exact_private_gateway(current, self.config):
                raise AccessOperationError(
                    "current Serve configuration differs; refusing destructive rollback"
                )
            log.verify_binding()
            result = self._run_with_rollback_binding(log, self.rollback_command)
            if result.returncode != 0:
                raise AccessOperationError("tailscale Serve rollback failed")
            log.verify_binding()
            after = self._serve_status(log)
            if not _is_empty_serve(after):
                raise AccessOperationError(
                    "tailscale Serve rollback could not be verified"
                )
            return log.append({**manifest, "state": "rolled_back"})

    def _validate_manifest_identity(self, manifest: dict[str, object]) -> None:
        if (
            manifest["config_fingerprint"] != config_fingerprint(self.config)
            or manifest["plan_id"] != self.plan()["plan_id"]
            or manifest["apply_command"] != list(self.serve_command)
            or manifest["rollback_command"] != list(self.rollback_command)
        ):
            raise AccessOperationError(
                "rollback manifest does not match this configuration"
            )

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, object]:
        with _rollback_log(path.parent, create=False) as log:
            if log.current is None:
                raise AccessOperationError("rollback manifest is incomplete")
            return log.current
