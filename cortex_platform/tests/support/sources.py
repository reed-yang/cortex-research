"""Temporary deterministic research adapter for P2 source contract tests."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cortex_platform.product.sources.models import ImportRequest, ImportResult


class InjectedImportCrash(RuntimeError):
    """Deterministic interruption at one cross-domain commit boundary."""


@dataclass(frozen=True)
class _PreparedStateFrame:
    last_valid_offset: int
    file_size: int
    prefix: bytes
    header: bytes
    payload: bytes
    checksum: bytes


@dataclass(frozen=True)
class _OpenManifestAnchor:
    target_fd: int
    manifest_fd: int
    anchor_fd: int
    manifest_name: str
    anchor_name: str
    encoded: bytes


class TemporaryResearchImportAdapter:
    """Materialize synthetic state below a capability-opened temporary root.

    The ``database`` argument names a deterministic JSON operation-state file,
    not SQLite. Every read and write uses its already-open descriptor so tests
    make no claim that Python's path-based SQLite API is capability-safe.
    """

    _FAULTS = frozenset(
        {
            "after_directory",
            "after_manifest_temp",
            "after_manifest",
            "after_db_commit",
            "after_state_frame_header",
            "after_state_frame_body",
            "after_state_checksum_partial",
        }
    )
    _LOG_MAGIC = b"CORTEX-SOURCE-OPLOG\x00\x01"
    _MAX_LOG_BYTES = 1_048_576
    _MAX_FRAME_BYTES = 262_144
    _MAX_STATE_ITEMS = 512
    _OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
    _CANONICAL_ID_RE = re.compile(
        r"(?:arxiv:[0-9]{4}\.[0-9]{4,5}"
        r"|doi:10\.[0-9]{4,9}/[-._;()/:a-z0-9]+"
        r"|sha256:[0-9a-f]{64})\Z"
    )
    _ENGINE_REF_RE = re.compile(
        r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._/-]{0,466}\Z"
    )

    def __init__(
        self,
        *,
        database: Path,
        root: Path,
        safety_root: Path,
        fault_at: str | None = None,
        after_capability_open: Callable[[], None] | None = None,
        after_manifest_validation: Callable[[], None] | None = None,
        manifest_race_payload: bytes | None = None,
    ) -> None:
        self.database = Path(os.path.abspath(database))
        self.root = Path(os.path.abspath(root))
        self.safety_root = Path(os.path.abspath(safety_root))
        if fault_at is not None and fault_at not in self._FAULTS:
            raise ValueError("fault_at is invalid")
        if manifest_race_payload is not None and (
            not isinstance(manifest_race_payload, bytes)
            or len(manifest_race_payload) > 16_384
        ):
            raise ValueError("manifest_race_payload is invalid")
        self.fault_at = fault_at
        self.after_capability_open = after_capability_open
        self.after_manifest_validation = after_manifest_validation
        self.manifest_race_payload = manifest_race_payload
        for candidate in (self.database, self.root):
            if candidate == self.safety_root or self.safety_root not in candidate.parents:
                raise ValueError("temporary import paths must stay below safety_root")

    def execute(self, request: ImportRequest) -> ImportResult:
        slug = hashlib.sha256(request.canonical_id.encode()).hexdigest()[:20]
        engine_ref = f"paper:{slug}"
        manifest = {"source_rows": 1, "chunks": 1, "directories": 1}
        response = {"engine_ref": engine_ref, "manifest": manifest}
        materialization = {
            "canonical_id": request.canonical_id,
            "engine_ref": engine_ref,
            "operation_id": request.operation_id,
            "request_hash": request.request_hash,
        }
        safety_fd = self._open_safe_directory(self.safety_root)
        database_parent_fd = -1
        database_fd = -1
        root_fd = -1
        safety_locked = False
        manifest_anchor: _OpenManifestAnchor | None = None
        try:
            if self.after_capability_open is not None:
                self.after_capability_open()
            fcntl.flock(safety_fd, fcntl.LOCK_EX)
            safety_locked = True
            database_parts = self.database.parent.relative_to(
                self.safety_root
            ).parts
            database_parent_fd = self._open_existing_directory_chain(
                safety_fd, database_parts
            )
            initial_prepared: _PreparedStateFrame | None = None
            if database_parent_fd < 0:
                initial_prepared = self._prepare_new_operation(
                    state=self._empty_operation_state(),
                    request=request,
                    response=response,
                    engine_ref=engine_ref,
                    last_valid_offset=0,
                    file_size=0,
                )
                database_parent_fd = self._open_directory_chain(
                    safety_fd, database_parts, create=True
                )
            database_fd = self._open_existing_database(
                database_parent_fd, self.database.name
            )
            if database_fd < 0:
                if initial_prepared is None:
                    initial_prepared = self._prepare_new_operation(
                        state=self._empty_operation_state(),
                        request=request,
                        response=response,
                        engine_ref=engine_ref,
                        last_valid_offset=0,
                        file_size=0,
                    )
                database_fd = self._open_database(
                    database_parent_fd, self.database.name
                )
            fcntl.flock(database_fd, fcntl.LOCK_EX)
            try:
                state, last_valid_offset, file_size = self._load_operation_state(
                    database_fd
                )
                previous = state["operations"].get(request.operation_id)
                if previous is not None:
                    if previous["request_hash"] != request.request_hash:
                        raise ValueError("import operation request hash changed")
                    if (
                        previous["canonical_id"] != request.canonical_id
                        or previous["response"] != response
                    ):
                        raise ValueError("import operation result identity changed")
                    root_fd = self._open_directory_chain(
                        safety_fd,
                        self.root.relative_to(self.safety_root).parts,
                        create=False,
                    )
                    manifest_anchor = self._ensure_materialization(
                        root_fd=root_fd,
                        slug=slug,
                        expected=materialization,
                        allow_create=False,
                    )
                    self._validate_open_manifest_anchor(
                        manifest_anchor, label="replay"
                    )
                    return ImportResult(
                        operation_id=request.operation_id,
                        request_hash=request.request_hash,
                        engine_ref=engine_ref,
                        manifest=manifest,
                        replayed=True,
                    )
                prepared = initial_prepared
                if (
                    prepared is None
                    or last_valid_offset != 0
                    or file_size != 0
                    or state != self._empty_operation_state()
                ):
                    prepared = self._prepare_new_operation(
                        state=state,
                        request=request,
                        response=response,
                        engine_ref=engine_ref,
                        last_valid_offset=last_valid_offset,
                        file_size=file_size,
                    )
                root_fd = self._open_directory_chain(
                    safety_fd,
                    self.root.relative_to(self.safety_root).parts,
                    create=True,
                )
                manifest_anchor = self._ensure_materialization(
                    root_fd=root_fd,
                    slug=slug,
                    expected=materialization,
                    allow_create=True,
                )
                if self.after_manifest_validation is not None:
                    self.after_manifest_validation()
                self._validate_open_manifest_anchor(
                    manifest_anchor, label="pre-commit"
                )
                self._append_operation_state(
                    database_fd, prepared, manifest_anchor
                )
                os.fsync(database_parent_fd)
                self._validate_open_manifest_anchor(
                    manifest_anchor, label="committed"
                )
                self._fault("after_db_commit")
                return ImportResult(
                    operation_id=request.operation_id,
                    request_hash=request.request_hash,
                    engine_ref=engine_ref,
                    manifest=manifest,
                )
            finally:
                fcntl.flock(database_fd, fcntl.LOCK_UN)
        finally:
            if manifest_anchor is not None:
                self._close_manifest_anchor(manifest_anchor)
            if root_fd >= 0:
                os.close(root_fd)
            if database_fd >= 0:
                os.close(database_fd)
            if database_parent_fd >= 0:
                os.close(database_parent_fd)
            if safety_locked:
                fcntl.flock(safety_fd, fcntl.LOCK_UN)
            os.close(safety_fd)

    @classmethod
    def _prepare_new_operation(
        cls,
        *,
        state: dict[str, Any],
        request: ImportRequest,
        response: dict[str, Any],
        engine_ref: str,
        last_valid_offset: int,
        file_size: int,
    ) -> _PreparedStateFrame:
        sources = dict(state["sources"])
        chunks = dict(state["chunks"])
        operations = dict(state["operations"])
        existing_source = sources.get(request.canonical_id)
        if existing_source is None:
            sources[request.canonical_id] = engine_ref
            chunks[request.canonical_id] = "synthetic deterministic chunk"
        elif existing_source != engine_ref:
            raise ValueError("canonical research source identity changed")
        operations[request.operation_id] = {
            "canonical_id": request.canonical_id,
            "request_hash": request.request_hash,
            "response": response,
        }
        return cls._prepare_operation_state(
            {
                "version": 1,
                "operations": operations,
                "sources": sources,
                "chunks": chunks,
            },
            last_valid_offset=last_valid_offset,
            file_size=file_size,
        )

    @classmethod
    def _empty_operation_state(cls) -> dict[str, Any]:
        return {
            "version": 1,
            "operations": {},
            "sources": {},
            "chunks": {},
        }

    @classmethod
    def _load_operation_state(
        cls, database_fd: int
    ) -> tuple[dict[str, Any], int, int]:
        status = os.fstat(database_fd)
        file_size = status.st_size
        if status.st_size == 0:
            return cls._empty_operation_state(), 0, 0
        if status.st_size > cls._MAX_LOG_BYTES:
            raise ValueError("temporary operation state is too large")
        contents = bytearray()
        offset = 0
        while offset < status.st_size:
            chunk = os.pread(database_fd, min(65_536, status.st_size - offset), offset)
            if not chunk:
                break
            contents.extend(chunk)
            offset += len(chunk)
        raw = bytes(contents)
        if len(raw) < len(cls._LOG_MAGIC):
            if cls._LOG_MAGIC.startswith(raw):
                return cls._empty_operation_state(), 0, file_size
            raise ValueError("temporary operation state header is invalid")
        if raw[: len(cls._LOG_MAGIC)] != cls._LOG_MAGIC:
            raise ValueError("temporary operation state header is invalid")
        state = cls._empty_operation_state()
        offset = len(cls._LOG_MAGIC)
        last_valid_offset = offset
        while offset < len(raw):
            frame_start = offset
            if len(raw) - offset < 4:
                return state, last_valid_offset, file_size
            frame_length = struct.unpack(">I", raw[offset : offset + 4])[0]
            offset += 4
            if frame_length < 2 or frame_length > cls._MAX_FRAME_BYTES:
                raise ValueError("temporary operation state frame length is invalid")
            frame_end = offset + frame_length
            checksum_end = frame_end + hashlib.sha256().digest_size
            if checksum_end > len(raw):
                return state, last_valid_offset, file_size
            payload = raw[offset:frame_end]
            checksum = raw[frame_end:checksum_end]
            if checksum != hashlib.sha256(payload).digest():
                raise ValueError("temporary operation state checksum is invalid")
            try:
                candidate = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("temporary operation state frame is invalid") from exc
            canonical = json.dumps(
                candidate, sort_keys=True, separators=(",", ":")
            ).encode()
            if canonical != payload:
                raise ValueError("temporary operation state frame is not canonical")
            state = cls._validate_operation_state(candidate)
            offset = checksum_end
            last_valid_offset = offset
            if last_valid_offset <= frame_start:
                raise ValueError("temporary operation state frame is invalid")
        return state, last_valid_offset, file_size

    @classmethod
    def _prepare_operation_state(
        cls,
        state: dict[str, Any],
        *,
        last_valid_offset: int,
        file_size: int,
    ) -> _PreparedStateFrame:
        state = cls._validate_operation_state(state)
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(payload) > cls._MAX_FRAME_BYTES:
            raise ValueError("temporary operation state frame is too large")
        header = struct.pack(">I", len(payload))
        checksum = hashlib.sha256(payload).digest()
        prefix = cls._LOG_MAGIC if last_valid_offset == 0 else b""
        projected_size = (
            last_valid_offset + len(prefix) + len(header) + len(payload) + len(checksum)
        )
        if projected_size > cls._MAX_LOG_BYTES:
            raise ValueError("temporary operation state log is too large")
        return _PreparedStateFrame(
            last_valid_offset=last_valid_offset,
            file_size=file_size,
            prefix=prefix,
            header=header,
            payload=payload,
            checksum=checksum,
        )

    def _append_operation_state(
        self,
        database_fd: int,
        prepared: _PreparedStateFrame,
        manifest_anchor: _OpenManifestAnchor,
    ) -> None:
        self._validate_open_manifest_anchor(
            manifest_anchor, label="append"
        )
        if os.fstat(database_fd).st_size != prepared.file_size:
            raise ValueError("temporary operation state changed before append")
        if prepared.file_size != prepared.last_valid_offset:
            os.ftruncate(database_fd, prepared.last_valid_offset)
        os.lseek(database_fd, prepared.last_valid_offset, os.SEEK_SET)
        if prepared.prefix:
            self._write_all(database_fd, prepared.prefix)
        self._write_all(database_fd, prepared.header)
        os.fsync(database_fd)
        self._fault("after_state_frame_header")
        self._write_all(database_fd, prepared.payload)
        os.fsync(database_fd)
        self._fault("after_state_frame_body")
        if self.fault_at == "after_state_checksum_partial":
            self._write_all(
                database_fd,
                prepared.checksum[: len(prepared.checksum) // 2],
            )
            os.fsync(database_fd)
            raise InjectedImportCrash("after_state_checksum_partial")
        self._write_all(database_fd, prepared.checksum)
        os.fsync(database_fd)
        self._validate_open_manifest_anchor(
            manifest_anchor, label="append-durable"
        )

    @classmethod
    def _validate_operation_state(cls, state: Any) -> dict[str, Any]:
        if (
            type(state) is not dict
            or set(state) != {"version", "operations", "sources", "chunks"}
            or type(state.get("version")) is not int
            or state["version"] != 1
        ):
            raise ValueError("temporary operation state schema is invalid")
        operations = state.get("operations")
        sources = state.get("sources")
        chunks = state.get("chunks")
        if any(type(value) is not dict for value in (operations, sources, chunks)):
            raise ValueError("temporary operation state schema is invalid")
        if any(
            len(value) > cls._MAX_STATE_ITEMS
            for value in (operations, sources, chunks)
        ):
            raise ValueError("temporary operation state contains too many items")

        source_ids: set[str] = set()
        engine_refs: set[str] = set()
        for canonical_id, engine_ref in sources.items():
            if (
                not isinstance(canonical_id, str)
                or len(canonical_id) > 1_000
                or cls._CANONICAL_ID_RE.fullmatch(canonical_id) is None
                or not isinstance(engine_ref, str)
                or cls._ENGINE_REF_RE.fullmatch(engine_ref) is None
                or ".." in engine_ref
                or "//" in engine_ref
            ):
                raise ValueError("temporary operation state source is invalid")
            source_ids.add(canonical_id)
            engine_refs.add(engine_ref)
        if len(engine_refs) != len(sources):
            raise ValueError("temporary operation state engine identity is duplicated")

        if set(chunks) != source_ids:
            raise ValueError("temporary operation state chunks are inconsistent")
        for canonical_id, content in chunks.items():
            if (
                not isinstance(canonical_id, str)
                or not isinstance(content, str)
                or not 1 <= len(content) <= 10_000
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in content
                )
            ):
                raise ValueError("temporary operation state chunk is invalid")

        operation_source_ids: set[str] = set()
        for operation_id, operation in operations.items():
            if (
                not isinstance(operation_id, str)
                or cls._OPERATION_ID_RE.fullmatch(operation_id) is None
                or type(operation) is not dict
                or set(operation)
                != {"canonical_id", "request_hash", "response"}
            ):
                raise ValueError("temporary operation state operation is invalid")
            canonical_id = operation.get("canonical_id")
            request_hash = operation.get("request_hash")
            response = operation.get("response")
            if (
                not isinstance(canonical_id, str)
                or canonical_id not in source_ids
                or not isinstance(request_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
                or type(response) is not dict
                or set(response) != {"engine_ref", "manifest"}
            ):
                raise ValueError("temporary operation state operation is invalid")
            engine_ref = response.get("engine_ref")
            manifest = response.get("manifest")
            if (
                engine_ref != sources[canonical_id]
                or type(manifest) is not dict
                or set(manifest) != {"source_rows", "chunks", "directories"}
                or any(
                    type(manifest[field]) is not int
                    or not 0 <= manifest[field] <= 1_000_000
                    for field in ("source_rows", "chunks", "directories")
                )
            ):
                raise ValueError("temporary operation state response is invalid")
            operation_source_ids.add(canonical_id)
        if operation_source_ids != source_ids:
            raise ValueError("temporary operation state sources are inconsistent")
        return state

    def _ensure_materialization(
        self,
        *,
        root_fd: int,
        slug: str,
        expected: dict[str, Any],
        allow_create: bool,
    ) -> _OpenManifestAnchor:
        encoded = json.dumps(
            expected, sort_keys=True, separators=(",", ":")
        ).encode()
        claim_digest = hashlib.sha256(encoded).hexdigest()[:16]
        claim_prefix = f".claim-{slug}-"
        claim_name = f"{claim_prefix}{claim_digest}"
        for entry in os.listdir(root_fd):
            if entry.startswith(claim_prefix) and entry != claim_name:
                raise ValueError("import operation claim does not match")
        manifest_anchor: _OpenManifestAnchor | None = None
        try:
            claim_fd = self._open_child_directory(
                root_fd, claim_name, create=allow_create
            )
            try:
                target_fd = self._open_child_directory(
                    root_fd, slug, create=allow_create
                )
                try:
                    self._fault("after_directory")
                    digest = hashlib.sha256(encoded).hexdigest()[:16]
                    temporary_name = f".manifest-{digest}.tmp"
                    allowed_names = {"manifest.json", temporary_name}
                    entries = set(os.listdir(target_fd))
                    unexpected = entries - allowed_names
                    if unexpected:
                        raise ValueError(
                            "unexpected import materialization entry"
                        )
                    if "manifest.json" in entries:
                        if temporary_name not in entries:
                            raise ValueError(
                                "completed import manifest anchor is missing"
                            )
                        manifest_anchor = self._open_manifest_anchor(
                            target_fd=target_fd,
                            temporary_name=temporary_name,
                            encoded=encoded,
                            label="completed",
                        )
                    elif not allow_create:
                        raise ValueError("completed import manifest is missing")
                    elif temporary_name in entries:
                        actual = self._read_regular_file(
                            target_fd, temporary_name, "temporary"
                        )
                        if actual != encoded:
                            raise ValueError(
                                "import manifest temporary file changed"
                            )
                        self._fault("after_manifest_temp")
                        manifest_anchor = self._publish_manifest_no_replace(
                            target_fd=target_fd,
                            temporary_name=temporary_name,
                            encoded=encoded,
                        )
                    else:
                        temporary_fd = os.open(
                            temporary_name,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=target_fd,
                        )
                        try:
                            self._write_all(temporary_fd, encoded)
                            os.fsync(temporary_fd)
                        finally:
                            os.close(temporary_fd)
                        self._fault("after_manifest_temp")
                        manifest_anchor = self._publish_manifest_no_replace(
                            target_fd=target_fd,
                            temporary_name=temporary_name,
                            encoded=encoded,
                        )
                    self._fault("after_manifest")
                    if manifest_anchor is None:
                        raise ValueError(
                            "completed import manifest anchor is unavailable"
                        )
                    return manifest_anchor
                finally:
                    os.close(target_fd)
            finally:
                os.close(claim_fd)
        except BaseException:
            if manifest_anchor is not None:
                self._close_manifest_anchor(manifest_anchor)
            raise

    def _publish_manifest_no_replace(
        self,
        *,
        target_fd: int,
        temporary_name: str,
        encoded: bytes,
    ) -> _OpenManifestAnchor:
        if self.manifest_race_payload is not None:
            race_fd = os.open(
                "manifest.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target_fd,
            )
            try:
                self._write_all(race_fd, self.manifest_race_payload)
                os.fsync(race_fd)
            finally:
                os.close(race_fd)
            self.manifest_race_payload = None
        raced = False
        try:
            os.link(
                temporary_name,
                "manifest.json",
                src_dir_fd=target_fd,
                dst_dir_fd=target_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raced = True
        try:
            manifest_anchor = self._open_manifest_anchor(
                target_fd=target_fd,
                temporary_name=temporary_name,
                encoded=encoded,
                label="publication",
            )
        except ValueError as exc:
            if raced:
                raise ValueError(
                    "manifest publication race did not retain the anchor inode"
                ) from exc
            raise
        # Retain the exact publication anchor. Deleting it by name after
        # validation would create a validation-to-unlink replacement window.
        try:
            os.fsync(target_fd)
        except BaseException:
            self._close_manifest_anchor(manifest_anchor)
            raise
        return manifest_anchor

    @classmethod
    def _open_manifest_anchor(
        cls,
        *,
        target_fd: int,
        temporary_name: str,
        encoded: bytes,
        label: str,
    ) -> _OpenManifestAnchor:
        if re.fullmatch(r"\.manifest-[0-9a-f]{16}\.tmp", temporary_name) is None:
            raise ValueError(f"import {label} manifest anchor name is unsafe")
        descriptors: list[int] = []
        try:
            target_descriptor = os.dup(target_fd)
            descriptors.append(target_descriptor)
            for name in ("manifest.json", temporary_name):
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=target_descriptor,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"import {label} manifest anchor is unavailable"
                    ) from exc
                descriptors.append(descriptor)
            manifest_anchor = _OpenManifestAnchor(
                target_fd=descriptors[0],
                manifest_fd=descriptors[1],
                anchor_fd=descriptors[2],
                manifest_name="manifest.json",
                anchor_name=temporary_name,
                encoded=encoded,
            )
            cls._validate_open_manifest_anchor(manifest_anchor, label=label)
            return manifest_anchor
        except BaseException:
            for descriptor in descriptors:
                os.close(descriptor)
            raise

    @classmethod
    def _validate_open_manifest_anchor(
        cls, manifest_anchor: _OpenManifestAnchor, *, label: str
    ) -> None:
        if (
            manifest_anchor.manifest_name != "manifest.json"
            or re.fullmatch(
                r"\.manifest-[0-9a-f]{16}\.tmp",
                manifest_anchor.anchor_name,
            )
            is None
        ):
            raise ValueError(f"import {label} manifest anchor name is unsafe")
        try:
            cls._verify_private_directory(manifest_anchor.target_fd)
        except PermissionError as exc:
            raise ValueError(
                f"import {label} manifest target directory is unsafe"
            ) from exc
        retained_descriptors = (
            manifest_anchor.manifest_fd,
            manifest_anchor.anchor_fd,
        )
        bound_descriptors: list[int] = []
        try:
            for name in (
                manifest_anchor.manifest_name,
                manifest_anchor.anchor_name,
            ):
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=manifest_anchor.target_fd,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"import {label} manifest entry is unavailable"
                    ) from exc
                bound_descriptors.append(descriptor)
            retained_statuses = tuple(
                os.fstat(descriptor) for descriptor in retained_descriptors
            )
            bound_statuses = tuple(
                os.fstat(descriptor) for descriptor in bound_descriptors
            )
            for status in (*retained_statuses, *bound_statuses):
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or stat.S_IMODE(status.st_mode) & 0o077
                    or status.st_nlink != 2
                    or status.st_size > 16_384
                ):
                    raise ValueError(
                        f"import {label} manifest anchor link is unsafe"
                    )
            manifest_status, anchor_status = retained_statuses
            bound_manifest_status, bound_anchor_status = bound_statuses
            if (
                manifest_status.st_dev != anchor_status.st_dev
                or manifest_status.st_ino != anchor_status.st_ino
                or bound_manifest_status.st_dev != bound_anchor_status.st_dev
                or bound_manifest_status.st_ino != bound_anchor_status.st_ino
                or manifest_status.st_dev != bound_manifest_status.st_dev
                or manifest_status.st_ino != bound_manifest_status.st_ino
                or anchor_status.st_dev != bound_anchor_status.st_dev
                or anchor_status.st_ino != bound_anchor_status.st_ino
            ):
                raise ValueError(
                    f"import {label} manifest anchor inode does not match"
                )
            if any(
                cls._read_descriptor(descriptor) != manifest_anchor.encoded
                for descriptor in (*retained_descriptors, *bound_descriptors)
            ):
                raise ValueError(
                    f"import {label} manifest anchor identity changed"
                )
        finally:
            for descriptor in bound_descriptors:
                os.close(descriptor)

    @staticmethod
    def _close_manifest_anchor(manifest_anchor: _OpenManifestAnchor) -> None:
        os.close(manifest_anchor.anchor_fd)
        os.close(manifest_anchor.manifest_fd)
        os.close(manifest_anchor.target_fd)

    @staticmethod
    def _verify_private_directory(descriptor: int) -> None:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) & 0o077
        ):
            raise PermissionError("temporary import directory must be private and safe")

    @classmethod
    def _open_safe_directory(cls, path: Path) -> int:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise PermissionError("temporary import safety root is unsafe or symlinked") from exc
        try:
            cls._verify_private_directory(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @classmethod
    def _open_child_directory(
        cls, parent_fd: int, name: str, *, create: bool
    ) -> int:
        if not name or name in {".", ".."} or "/" in name:
            raise ValueError("temporary import directory name is unsafe")
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise ValueError("completed import materialization is missing") from exc
        except OSError as exc:
            raise PermissionError("temporary import directory is unsafe or symlinked") from exc
        try:
            cls._verify_private_directory(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @classmethod
    def _open_directory_chain(
        cls, parent_fd: int, parts: tuple[str, ...], *, create: bool
    ) -> int:
        descriptor = os.dup(parent_fd)
        try:
            for part in parts:
                child = cls._open_child_directory(
                    descriptor, part, create=create
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @classmethod
    def _open_existing_directory_chain(
        cls, parent_fd: int, parts: tuple[str, ...]
    ) -> int:
        descriptor = os.dup(parent_fd)
        try:
            for part in parts:
                if not part or part in {".", ".."} or "/" in part:
                    raise ValueError("temporary import directory name is unsafe")
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    os.close(descriptor)
                    return -1
                except OSError as exc:
                    raise PermissionError(
                        "temporary import directory is unsafe or symlinked"
                    ) from exc
                try:
                    cls._verify_private_directory(child)
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _verify_private_database(descriptor: int) -> None:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) & 0o077
            or status.st_nlink != 1
        ):
            raise PermissionError(
                "temporary import database must be private and safe"
            )

    @classmethod
    def _open_existing_database(cls, parent_fd: int, name: str) -> int:
        if not name or name in {".", ".."} or "/" in name:
            raise ValueError("temporary import database name is unsafe")
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return -1
        except OSError as exc:
            raise PermissionError(
                "temporary import database is unsafe or symlinked"
            ) from exc
        try:
            cls._verify_private_database(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @classmethod
    def _open_database(
        cls, parent_fd: int, name: str, *, create: bool = True
    ) -> int:
        if not name or name in {".", ".."} or "/" in name:
            raise ValueError("temporary import database name is unsafe")
        descriptor: int | None = None
        try:
            if create:
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    pass
            if descriptor is None:
                descriptor = os.open(
                    name,
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
        except OSError as exc:
            raise PermissionError(
                "temporary import database is unsafe or symlinked"
            ) from exc
        try:
            cls._verify_private_database(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @classmethod
    def inspect_operation_state(
        cls,
        *,
        database: Path,
        safety_root: Path,
    ) -> dict[str, Any]:
        database = Path(os.path.abspath(database))
        safety_root = Path(os.path.abspath(safety_root))
        if database == safety_root or safety_root not in database.parents:
            raise ValueError("temporary operation state must stay below safety_root")
        safety_fd = cls._open_safe_directory(safety_root)
        try:
            parent_fd = cls._open_directory_chain(
                safety_fd,
                database.parent.relative_to(safety_root).parts,
                create=False,
            )
            try:
                database_fd = cls._open_database(
                    parent_fd, database.name, create=False
                )
                try:
                    fcntl.flock(database_fd, fcntl.LOCK_SH)
                    try:
                        state, _, _ = cls._load_operation_state(database_fd)
                        return state
                    finally:
                        fcntl.flock(database_fd, fcntl.LOCK_UN)
                finally:
                    os.close(database_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.close(safety_fd)

    @staticmethod
    def _read_regular_file(parent_fd: int, name: str, label: str) -> bytes:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        except OSError as exc:
            raise ValueError(f"import {label} file is unsafe or symlinked") from exc
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) & 0o077
                or not 1 <= status.st_nlink <= 2
                or status.st_size > 16_384
            ):
                raise ValueError(f"import {label} file is unsafe")
            return TemporaryResearchImportAdapter._read_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_descriptor(descriptor: int) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        while True:
            chunk = os.pread(descriptor, 4_096, offset)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            offset += len(chunk)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])

    def _fault(self, point: str) -> None:
        if self.fault_at == point:
            raise InjectedImportCrash(point)
