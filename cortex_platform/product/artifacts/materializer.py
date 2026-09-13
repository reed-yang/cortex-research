"""Fail-closed filesystem materialization for immutable artifact bytes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .models import (
    ParentVersionInput,
    ValidationError,
    _byte_length,
    _closed,
    _identifier,
    _media_type,
    _schema_v1,
    _sha256,
    canonical_parent_dicts,
    parse_parent_inputs,
)

_ROOT_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_ADMIN_DIRECTORY = ".cortex-artifacts-v1"
_MAX_RELATIVE_PATH = 2_000
_MANIFEST_LIMIT = 64 * 1024
_READ_CHUNK = 64 * 1024


class MaterializerError(RuntimeError):
    """The requested filesystem materialization cannot safely proceed."""


class MaterializationConflict(MaterializerError):
    """Durable operation or target state conflicts with the exact request."""


class IntegrityError(MaterializerError):
    """Staged or published bytes do not match their immutable metadata."""


@dataclass(frozen=True)
class AssetRoot:
    """Server-side root capability; its absolute path is never a public DTO."""

    root_id: str
    path: Path
    max_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.root_id, str) or _ROOT_ID_RE.fullmatch(self.root_id) is None:
            raise ValueError("asset root_id is invalid")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("asset root path must be an absolute Path")
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("asset root max_bytes must be a positive integer")


@dataclass(frozen=True)
class _RegisteredRoot:
    config: AssetRoot
    device: int
    inode: int
    real_path: Path
    capability_id: str


@dataclass(frozen=True)
class MaterializationRequest:
    """One exact idempotent operation bound to content and one relative target."""

    schema_version: int
    operation_id: str
    root_id: str
    relative_path: str
    sha256: str
    byte_length: int
    media_type: str
    parents: tuple[ParentVersionInput, ...]

    FIELDS = {
        "schema_version",
        "operation_id",
        "root_id",
        "relative_path",
        "sha256",
        "byte_length",
        "media_type",
        "parents",
    }

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValidationError("unsupported materialization request schema")
        _identifier(self.operation_id, "operation_id")
        if _ROOT_ID_RE.fullmatch(self.root_id) is None:
            raise ValidationError("root_id is invalid")
        _relative_segments(self.relative_path)
        _sha256(self.sha256)
        _byte_length(self.byte_length)
        _media_type(self.media_type)
        canonical_parent_dicts(self.parents)

    @classmethod
    def from_dict(cls, raw: object) -> MaterializationRequest:
        data = _closed(raw, cls.FIELDS, "materialization request")
        _schema_v1(data, "materialization request")
        root_id = data["root_id"]
        if not isinstance(root_id, str) or _ROOT_ID_RE.fullmatch(root_id) is None:
            raise ValidationError("root_id is invalid")
        relative_path = data["relative_path"]
        if not isinstance(relative_path, str):
            raise ValidationError("relative_path must be text")
        _relative_segments(relative_path)
        return cls(
            schema_version=1,
            operation_id=_identifier(data["operation_id"], "operation_id"),
            root_id=root_id,
            relative_path=relative_path,
            sha256=_sha256(data["sha256"]),
            byte_length=_byte_length(data["byte_length"]),
            media_type=_media_type(data["media_type"]),
            parents=parse_parent_inputs(data["parents"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "root_id": self.root_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "parents": canonical_parent_dicts(self.parents),
        }


@dataclass(frozen=True)
class MaterializedAsset:
    """A path-redacted result suitable for persistence by a later store slice."""

    operation_id: str
    root_id: str
    relative_path: str
    sha256: str
    byte_length: int
    media_type: str
    parents: tuple[ParentVersionInput, ...]
    replayed: bool
    recovered_from: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "root_id": self.root_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "parents": canonical_parent_dicts(self.parents),
            "replayed": self.replayed,
            "recovered_from": self.recovered_from,
        }


def _relative_segments(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_RELATIVE_PATH
        or value.startswith("/")
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValidationError("relative_path is not canonical")
    segments = tuple(value.split("/"))
    if not segments or any(not segment or segment in {".", ".."} for segment in segments):
        raise ValidationError("relative_path contains an unsafe segment")
    if segments[0].casefold() == _ADMIN_DIRECTORY.casefold():
        raise ValidationError("relative_path enters the private administration area")
    for segment in segments:
        if (
            segment in {".", ".."}
            or "\x00" in segment
            or unicodedata.normalize("NFC", segment) != segment
            or any(
                unicodedata.category(character).startswith("C")
                for character in segment
            )
        ):
            raise ValidationError("relative_path contains an unsafe segment")
    return segments


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _request_manifest(request: MaterializationRequest) -> bytes:
    return _canonical_json({"schema_version": 1, "request": request.to_dict()})


def _target_manifest(
    request: MaterializationRequest,
    root: _RegisteredRoot,
    target_identity: str,
) -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "operation_id": request.operation_id,
            "root_id": request.root_id,
            "root_capability": root.capability_id,
            "relative_path": request.relative_path,
            "target_identity": target_identity,
            "request_sha256": hashlib.sha256(_request_manifest(request)).hexdigest(),
        }
    )


def _target_identity(request: MaterializationRequest, root: _RegisteredRoot) -> str:
    alias_path = unicodedata.normalize(
        "NFC", unicodedata.normalize("NFC", request.relative_path).casefold()
    )
    return hashlib.sha256(
        _canonical_json(
            {
                "root_capability": root.capability_id,
                "alias_path": alias_path,
            }
        )
    ).hexdigest()


def _alias_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _entry_exists(directory_fd: int, name: str) -> bool:
    wanted = _alias_key(name)
    entries = os.listdir(directory_fd)
    aliases = [entry for entry in entries if _alias_key(entry) == wanted]
    if any(entry != name for entry in aliases):
        raise MaterializerError(f"filesystem alias conflicts with {name!r}")
    return name in aliases


def _entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    if not _entry_exists(directory_fd, name):
        return None
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise MaterializerError("filesystem entry changed during validation") from None


def _require_owner_private(observed: os.stat_result, label: str) -> None:
    if observed.st_uid != os.geteuid() or observed.st_mode & 0o077:
        raise MaterializerError(f"{label} must be owner-private")


def _open_directory(
    directory_fd: int,
    name: str,
    *,
    create: bool,
    private: bool = False,
) -> int:
    observed = _entry_stat(directory_fd, name)
    if observed is None:
        if not create:
            raise MaterializerError(f"required directory {name!r} is missing")
        try:
            os.mkdir(name, mode=0o700, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileExistsError:
            observed = _entry_stat(directory_fd, name)
        else:
            observed = _entry_stat(directory_fd, name)
    if observed is None:
        raise MaterializerError(f"directory {name!r} disappeared")
    if stat.S_ISLNK(observed.st_mode):
        raise MaterializerError(f"directory {name!r} is a symlink")
    if not stat.S_ISDIR(observed.st_mode):
        raise MaterializerError(f"directory {name!r} is not a directory")
    if private:
        _require_owner_private(observed, f"directory {name!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        opened_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MaterializerError(f"directory {name!r} is not safely openable") from exc
    try:
        opened = os.fstat(opened_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise MaterializerError(f"directory {name!r} changed during open")
        if private:
            _require_owner_private(opened, f"directory {name!r}")
        return opened_fd
    except BaseException:
        os.close(opened_fd)
        raise


def _open_secure_root(path: Path) -> tuple[int, os.stat_result, Path]:
    if not path.is_absolute() or path == Path(path.anchor):
        raise MaterializerError("asset root path is invalid")
    parts = path.parts
    if any(part in {".", ".."} for part in parts[1:]):
        raise MaterializerError("asset root path contains an unsafe ancestor")
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    current_fd = os.open(path.anchor, flags)
    try:
        for index, part in enumerate(parts[1:]):
            observed = _entry_stat(current_fd, part)
            if observed is None:
                raise MaterializerError("asset root ancestor is missing")
            if stat.S_ISLNK(observed.st_mode):
                raise MaterializerError("asset root has a symlink ancestor")
            if not stat.S_ISDIR(observed.st_mode):
                raise MaterializerError("asset root ancestor is not a directory")
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise MaterializerError(
                    "asset root ancestor is not safely openable"
                ) from exc
            opened = os.fstat(next_fd)
            if (opened.st_dev, opened.st_ino) != (
                observed.st_dev,
                observed.st_ino,
            ):
                os.close(next_fd)
                raise MaterializerError("asset root changed during traversal")
            os.close(current_fd)
            current_fd = next_fd
            if index == len(parts[1:]) - 1:
                _require_owner_private(opened, "asset root mode")
        final = os.fstat(current_fd)
        canonical_path = Path(path.anchor).joinpath(*parts[1:])
        return current_fd, final, canonical_path
    except BaseException:
        os.close(current_fd)
        raise


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written < 1:
            raise MaterializerError("filesystem write made no progress")
        view = view[written:]


def _read_regular(
    directory_fd: int,
    name: str,
    *,
    limit: int,
    require_single_link: bool = True,
    require_private: bool = True,
) -> bytes:
    observed = _entry_stat(directory_fd, name)
    if observed is None:
        raise FileNotFoundError(name)
    if stat.S_ISLNK(observed.st_mode):
        raise MaterializerError(f"file {name!r} is a symlink")
    if not stat.S_ISREG(observed.st_mode):
        raise MaterializerError(f"file {name!r} is not regular")
    if require_single_link and observed.st_nlink != 1:
        raise MaterializerError(f"file {name!r} has an unsafe link count")
    if require_private:
        _require_owner_private(observed, f"file {name!r}")
    if observed.st_size > limit:
        raise IntegrityError(f"file {name!r} exceeds its size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (require_single_link and opened.st_nlink != 1)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or opened.st_size != observed.st_size
        ):
            raise MaterializerError(f"file {name!r} changed during open")
        if require_private:
            _require_owner_private(opened, f"file {name!r}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise IntegrityError(f"file {name!r} exceeds its size limit")
        closed = os.fstat(fd)
        if (
            (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mode,
                opened.st_uid,
            )
            != (
                closed.st_dev,
                closed.st_ino,
                closed.st_size,
                closed.st_mode,
                closed.st_uid,
            )
            or (require_single_link and closed.st_nlink != 1)
        ):
            raise MaterializerError(f"file {name!r} changed during verification")
        if require_private:
            _require_owner_private(closed, f"file {name!r}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _ensure_manifest(
    directory_fd: int,
    name: str,
    expected: bytes,
    *,
    label: str,
) -> bool:
    if len(expected) > _MANIFEST_LIMIT:
        raise MaterializerError(f"{label} manifest exceeds the size limit")
    temporary = name + ".new"
    final_exists = _entry_exists(directory_fd, name)
    temporary_exists = _entry_exists(directory_fd, temporary)
    existed = final_exists or temporary_exists

    if final_exists:
        observed = _read_regular(
            directory_fd,
            name,
            limit=_MANIFEST_LIMIT,
            require_single_link=False,
        )
        if observed != expected:
            raise MaterializationConflict(f"{label} manifest conflicts with request")
        if temporary_exists:
            pending = _read_regular(
                directory_fd,
                temporary,
                limit=_MANIFEST_LIMIT,
                require_single_link=False,
            )
            final_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            pending_stat = os.stat(
                temporary, dir_fd=directory_fd, follow_symlinks=False
            )
            if pending != expected or (final_stat.st_dev, final_stat.st_ino) != (
                pending_stat.st_dev,
                pending_stat.st_ino,
            ):
                raise MaterializationConflict(
                    f"{label} manifest has an unknown extra file"
                )
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        _read_regular(directory_fd, name, limit=_MANIFEST_LIMIT)
        return True

    if temporary_exists:
        pending = _read_regular(directory_fd, temporary, limit=_MANIFEST_LIMIT)
        if pending != expected:
            raise MaterializationConflict(f"{label} manifest conflicts with request")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            _write_all(fd, expected)
            os.fsync(fd)
        finally:
            os.close(fd)

    try:
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        observed = _read_regular(
            directory_fd,
            name,
            limit=_MANIFEST_LIMIT,
            require_single_link=False,
        )
        if observed != expected:
            raise MaterializationConflict(f"{label} manifest conflicts with request")
    os.fsync(directory_fd)
    final_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    pending_stat = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
    if (final_stat.st_dev, final_stat.st_ino) != (
        pending_stat.st_dev,
        pending_stat.st_ino,
    ):
        raise MaterializationConflict(f"{label} manifest was replaced concurrently")
    os.unlink(temporary, dir_fd=directory_fd)
    os.fsync(directory_fd)
    _read_regular(directory_fd, name, limit=_MANIFEST_LIMIT)
    return existed


def _require_manifest(
    directory_fd: int,
    name: str,
    expected: bytes,
    *,
    label: str,
) -> None:
    if not _entry_exists(directory_fd, name):
        raise MaterializationConflict("published target is unowned by this operation")
    observed = _read_regular(directory_fd, name, limit=_MANIFEST_LIMIT)
    if observed != expected:
        raise MaterializationConflict(f"{label} manifest conflicts with request")


def _open_lock(directory_fd: int, name: str) -> int:
    _entry_exists(directory_fd, name)
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
    except FileExistsError:
        try:
            fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise MaterializerError("operation lock is not safely openable") from exc
    except OSError as exc:
        raise MaterializerError("operation lock is not safely openable") from exc
    observed = os.fstat(fd)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        os.close(fd)
        raise MaterializerError("operation lock is not a single regular file")
    try:
        _require_owner_private(observed, "operation lock")
    except MaterializerError:
        os.close(fd)
        raise
    os.fsync(directory_fd)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _verify_content(
    directory_fd: int,
    name: str,
    request: MaterializationRequest,
    *,
    require_single_link: bool = True,
) -> None:
    value = _read_regular(
        directory_fd,
        name,
        limit=request.byte_length,
        require_single_link=require_single_link,
    )
    if len(value) != request.byte_length:
        raise IntegrityError("materialized byte length does not match request")
    if hashlib.sha256(value).hexdigest() != request.sha256:
        raise IntegrityError("materialized hash does not match request")


def _verify_dual_link(
    stage_fd: int,
    stage_name: str,
    parent_fd: int,
    final_name: str,
    request: MaterializationRequest,
) -> None:
    stage_stat = _entry_stat(stage_fd, stage_name)
    final_stat = _entry_stat(parent_fd, final_name)
    if stage_stat is None or final_stat is None:
        raise MaterializationConflict("known dual-link publication is incomplete")
    if (
        not stat.S_ISREG(stage_stat.st_mode)
        or not stat.S_ISREG(final_stat.st_mode)
        or stat.S_ISLNK(stage_stat.st_mode)
        or stat.S_ISLNK(final_stat.st_mode)
    ):
        raise MaterializationConflict("known dual-link publication is not regular")
    _require_owner_private(stage_stat, "staged artifact")
    _require_owner_private(final_stat, "final artifact")
    if (
        (stage_stat.st_dev, stage_stat.st_ino)
        != (final_stat.st_dev, final_stat.st_ino)
        or stage_stat.st_nlink != 2
        or final_stat.st_nlink != 2
    ):
        raise MaterializationConflict(
            "final and staged artifacts are not one known dual-link inode"
        )
    _verify_content(
        stage_fd, stage_name, request, require_single_link=False
    )
    _verify_content(
        parent_fd, final_name, request, require_single_link=False
    )


def _write_stage(
    stage_fd: int,
    stage_name: str,
    content: bytes | bytearray | memoryview | BinaryIO,
    request: MaterializationRequest,
    max_bytes: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(stage_name, flags, 0o600, dir_fd=stage_fd)
    hasher = hashlib.sha256()
    total = 0
    try:
        if isinstance(content, (bytes, bytearray, memoryview)):
            iterator: Iterable[bytes] = (bytes(content),)
        elif hasattr(content, "read"):
            iterator = _stream_chunks(content)
        else:
            raise TypeError("content must be bytes or a binary stream")
        for chunk in iterator:
            total += len(chunk)
            if total > max_bytes or total > request.byte_length:
                raise IntegrityError("materialized byte length exceeds request")
            _write_all(fd, chunk)
            hasher.update(chunk)
        if total != request.byte_length:
            raise IntegrityError("materialized byte length does not match request")
        if hasher.hexdigest() != request.sha256:
            raise IntegrityError("materialized hash does not match request")
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(stage_name, dir_fd=stage_fd)
            os.fsync(stage_fd)
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(fd)
        os.fsync(stage_fd)


def _stream_chunks(stream: BinaryIO) -> Iterable[bytes]:
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            return
        if not isinstance(chunk, bytes):
            raise TypeError("binary stream returned non-bytes content")
        yield chunk


class FilesystemMaterializer:
    """Publish immutable bytes under explicitly configured local asset roots."""

    def __init__(self, roots: Iterable[AssetRoot]) -> None:
        roots_by_id: dict[str, _RegisteredRoot] = {}
        root_inodes: set[tuple[int, int]] = set()
        for root in roots:
            if not isinstance(root, AssetRoot):
                raise ValueError("asset roots must be AssetRoot values")
            if root.root_id in roots_by_id:
                raise ValueError("asset root identifiers must be unique")
            root_fd, observed, real_path = _open_secure_root(root.path)
            os.close(root_fd)
            inode = (observed.st_dev, observed.st_ino)
            if inode in root_inodes:
                raise ValueError("asset root paths must not alias")
            for registered in roots_by_id.values():
                if (
                    real_path == registered.real_path
                    or real_path in registered.real_path.parents
                    or registered.real_path in real_path.parents
                ):
                    raise ValueError("asset root paths must not overlap")
            root_inodes.add(inode)
            capability_id = hashlib.sha256(
                _canonical_json(
                    {
                        "device": observed.st_dev,
                        "inode": observed.st_ino,
                    }
                )
            ).hexdigest()
            roots_by_id[root.root_id] = _RegisteredRoot(
                config=root,
                device=observed.st_dev,
                inode=observed.st_ino,
                real_path=real_path,
                capability_id=capability_id,
            )
        if not roots_by_id:
            raise ValueError("at least one asset root is required")
        self._roots = roots_by_id

    def materialize(
        self,
        request: MaterializationRequest,
        content: bytes | bytearray | memoryview | BinaryIO | None,
    ) -> MaterializedAsset:
        if not isinstance(request, MaterializationRequest):
            raise TypeError("request must be a MaterializationRequest")
        registered_root = self._roots.get(request.root_id)
        if registered_root is None:
            raise MaterializerError("unknown asset root")
        root = registered_root.config
        if request.byte_length > root.max_bytes:
            raise MaterializerError("request exceeds the asset root size limit")
        segments = _relative_segments(request.relative_path)
        operation_manifest = _request_manifest(request)
        target_identity = _target_identity(request, registered_root)
        target_manifest = _target_manifest(
            request, registered_root, target_identity
        )
        if (
            len(operation_manifest) > _MANIFEST_LIMIT
            or len(target_manifest) > _MANIFEST_LIMIT
        ):
            raise MaterializerError("canonical manifest exceeds the size limit")

        root_fd, opened, real_path = _open_secure_root(root.path)

        with ExitStack() as stack:
            stack.callback(os.close, root_fd)
            if (
                (opened.st_dev, opened.st_ino)
                != (registered_root.device, registered_root.inode)
                or real_path != registered_root.real_path
            ):
                raise MaterializerError("asset root changed during validation")

            admin_fd = _open_directory(
                root_fd, _ADMIN_DIRECTORY, create=True, private=True
            )
            stack.callback(os.close, admin_fd)
            directories: dict[str, int] = {}
            for name in ("operations", "targets", "staging", "publish", "locks"):
                directory_fd = _open_directory(
                    admin_fd, name, create=True, private=True
                )
                stack.callback(os.close, directory_fd)
                directories[name] = directory_fd

            self._lock_checkpoint("before_operation_lock", request)
            operation_lock_fd = _open_lock(
                directories["locks"],
                "operation-" + request.operation_id + ".lock",
            )
            stack.callback(os.close, operation_lock_fd)
            target_lock_fd = _open_lock(
                directories["locks"], "target-" + target_identity + ".lock"
            )
            stack.callback(os.close, target_lock_fd)
            try:
                operation_existed = _ensure_manifest(
                    directories["operations"],
                    request.operation_id + ".json",
                    operation_manifest,
                    label="operation",
                )
                target_existed = _ensure_manifest(
                    directories["targets"],
                    target_identity + ".json",
                    target_manifest,
                    label="target",
                )
                return self._materialize_with_parent(
                    root,
                    root_fd,
                    directories,
                    request,
                    content,
                    segments,
                    operation_existed=operation_existed,
                    target_existed=target_existed,
                    operation_manifest=operation_manifest,
                    target_manifest=target_manifest,
                    target_identity=target_identity,
                )
            finally:
                fcntl.flock(target_lock_fd, fcntl.LOCK_UN)
                fcntl.flock(operation_lock_fd, fcntl.LOCK_UN)

    def _materialize_with_parent(
        self,
        root: AssetRoot,
        root_fd: int,
        directories: dict[str, int],
        request: MaterializationRequest,
        content: bytes | bytearray | memoryview | BinaryIO | None,
        segments: tuple[str, ...],
        *,
        operation_existed: bool,
        target_existed: bool,
        operation_manifest: bytes,
        target_manifest: bytes,
        target_identity: str,
    ) -> MaterializedAsset:
        parent_fd = os.dup(root_fd)
        try:
            for segment in segments[:-1]:
                next_fd = _open_directory(
                    parent_fd, segment, create=True, private=True
                )
                os.close(parent_fd)
                parent_fd = next_fd
            return self._materialize_under_lock(
                root,
                parent_fd,
                directories,
                request,
                content,
                segments[-1],
                operation_existed=operation_existed,
                target_existed=target_existed,
                operation_manifest=operation_manifest,
                target_manifest=target_manifest,
                target_identity=target_identity,
            )
        finally:
            os.close(parent_fd)

    def _materialize_under_lock(
        self,
        root: AssetRoot,
        parent_fd: int,
        directories: dict[str, int],
        request: MaterializationRequest,
        content: bytes | bytearray | memoryview | BinaryIO | None,
        final_name: str,
        *,
        operation_existed: bool,
        target_existed: bool,
        operation_manifest: bytes,
        target_manifest: bytes,
        target_identity: str,
    ) -> MaterializedAsset:
        operation_name = request.operation_id + ".json"
        target_name = target_identity + ".json"
        stage_name = request.operation_id + ".part"
        publish_name = request.operation_id + ".json"
        final_stat = _entry_stat(parent_fd, final_name)
        if final_stat is not None and stat.S_ISLNK(final_stat.st_mode):
            raise MaterializerError("final target is a symlink")
        if final_stat is not None and not stat.S_ISREG(final_stat.st_mode):
            raise MaterializerError("final target is not a regular file")

        if final_stat is not None:
            return self._settle_existing_final(
                parent_fd,
                directories,
                request,
                final_name,
                stage_name,
                operation_name,
                target_name,
                publish_name,
                operation_manifest,
                target_manifest,
                operation_existed=operation_existed,
                target_existed=target_existed,
            )

        stage_existed = _entry_exists(directories["staging"], stage_name)
        if stage_existed:
            if not operation_existed or not target_existed:
                raise MaterializationConflict(
                    "staged bytes are unknown to this durable operation"
                )
            _verify_content(directories["staging"], stage_name, request)
        else:
            if content is None:
                raise MaterializerError("matching staged bytes are unavailable")
            _write_stage(
                directories["staging"],
                stage_name,
                content,
                request,
                root.max_bytes,
            )
            _verify_content(directories["staging"], stage_name, request)
            self._crash_checkpoint("after_stage_fsync")

        publish_existed = _ensure_manifest(
            directories["publish"],
            publish_name,
            operation_manifest,
            label="publish intent",
        )
        if not publish_existed:
            self._crash_checkpoint("after_publish_intent_fsync")

        self._crash_checkpoint("before_final_link")
        try:
            os.link(
                stage_name,
                final_name,
                src_dir_fd=directories["staging"],
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return self._settle_existing_final(
                parent_fd,
                directories,
                request,
                final_name,
                stage_name,
                operation_name,
                target_name,
                publish_name,
                operation_manifest,
                target_manifest,
                operation_existed=operation_existed,
                target_existed=target_existed,
            )
        except OSError as exc:
            raise MaterializerError(
                "atomic no-replace publication link failed"
            ) from exc
        _verify_dual_link(
            directories["staging"],
            stage_name,
            parent_fd,
            final_name,
            request,
        )
        os.fsync(parent_fd)
        self._crash_checkpoint("after_final_link")
        os.unlink(stage_name, dir_fd=directories["staging"])
        os.fsync(directories["staging"])
        _verify_content(parent_fd, final_name, request)
        replayed = operation_existed or target_existed or stage_existed or publish_existed
        recovered_from = "staged" if stage_existed else None
        return self._result(
            request, replayed=replayed, recovered_from=recovered_from
        )

    def _settle_existing_final(
        self,
        parent_fd: int,
        directories: dict[str, int],
        request: MaterializationRequest,
        final_name: str,
        stage_name: str,
        operation_name: str,
        target_name: str,
        publish_name: str,
        operation_manifest: bytes,
        target_manifest: bytes,
        *,
        operation_existed: bool,
        target_existed: bool,
    ) -> MaterializedAsset:
        if not operation_existed or not target_existed:
            raise MaterializationConflict(
                "published final is unowned by this durable operation"
            )
        _require_manifest(
            directories["targets"],
            target_name,
            target_manifest,
            label="target",
        )
        _require_manifest(
            directories["operations"],
            operation_name,
            operation_manifest,
            label="operation",
        )
        _require_manifest(
            directories["publish"],
            publish_name,
            operation_manifest,
            label="publish intent",
        )
        if _entry_exists(directories["staging"], stage_name):
            _verify_dual_link(
                directories["staging"],
                stage_name,
                parent_fd,
                final_name,
                request,
            )
            os.fsync(parent_fd)
            os.unlink(stage_name, dir_fd=directories["staging"])
            os.fsync(directories["staging"])
            _verify_content(parent_fd, final_name, request)
            return self._result(
                request, replayed=True, recovered_from="dual-link"
            )
        _verify_content(parent_fd, final_name, request)
        return self._result(request, replayed=True, recovered_from="final")

    @staticmethod
    def _result(
        request: MaterializationRequest,
        *,
        replayed: bool,
        recovered_from: str | None,
    ) -> MaterializedAsset:
        return MaterializedAsset(
            operation_id=request.operation_id,
            root_id=request.root_id,
            relative_path=request.relative_path,
            sha256=request.sha256,
            byte_length=request.byte_length,
            media_type=request.media_type,
            parents=request.parents,
            replayed=replayed,
            recovered_from=recovered_from,
        )

    def _crash_checkpoint(self, point: str) -> None:
        """Test-only subclass hook; production materializers leave it inert."""

        del point

    def _lock_checkpoint(
        self, point: str, request: MaterializationRequest
    ) -> None:
        """Test-only barrier hook before the fixed lock acquisition order."""

        del point, request
