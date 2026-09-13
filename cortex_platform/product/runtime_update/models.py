"""Strict machine-readable contracts for managed Hermes releases."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_STATUS = {"certified", "quarantined", "revoked"}


class ValidationError(ValueError):
    """A signed update document violates its closed schema."""


def canonical_json(value: object) -> bytes:
    """Encode a signed or hashed document deterministically."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_document(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _closed(raw: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValidationError(f"{name} fields do not match schema")
    return raw


def _text(raw: Mapping[str, object], name: str) -> str:
    value = raw[name]
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _identifier(raw: Mapping[str, object], name: str) -> str:
    value = _text(raw, name)
    if not _IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} must be a safe identifier")
    return value


def _digest(raw: Mapping[str, object], name: str) -> str:
    value = _text(raw, name)
    if not _SHA256.fullmatch(value):
        raise ValidationError(f"{name} must be a lowercase SHA-256")
    return value


_PYTHON_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _safe_relative_path(value: str, name: str) -> str:
    """One normalized POSIX path that cannot leave the root it is joined to.

    The AMD-5 grammar, and the same family as the `worker_entrypoint` rule one
    field above: `worker_entrypoint` is one file name because it names a file at
    the root of `content/`, while these paths may nest. Absolute paths, empty,
    `.` and `..` components, backslashes and NUL are refused here so that stage
    never has to reason about a path it has already accepted; the containment
    assertions at expansion time are the second, independent check.

    Backslash is refused rather than translated: a Windows-shaped path is not a
    POSIX path this product can join, and accepting it silently would make one
    document mean two different files on two hosts.
    """

    if not value or "\0" in value:
        raise ValidationError(f"{name} must be a non-empty string")
    if "\\" in value:
        raise ValidationError(f"{name} must not contain a backslash")
    if value.startswith("/"):
        raise ValidationError(f"{name} must be a relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValidationError(
            f"{name} must not contain an empty, current, or parent component"
        )
    if posixpath.normpath(value) != value:
        raise ValidationError(f"{name} must be a normalized POSIX path")
    return value


def _positive_int(raw: Mapping[str, object], name: str, *, zero: bool = False) -> int:
    value = raw[name]
    lower = 0 if zero else 1
    if type(value) is not int or value < lower:
        raise ValidationError(f"{name} must be an integer >= {lower}")
    return value


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{name} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{name} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValidationError(f"{name} must be UTC")
    return parsed


_OS_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")


def _os_version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _os_version_at_least(observed: str, required: str) -> bool:
    """Compare component by component, padding the shorter with zeros.

    Raw tuple comparison would make `(14,) < (14, 0)`, so a host reporting a
    bare major version would read as older than the floor it actually meets.
    """

    left = _os_version_tuple(observed)
    right = _os_version_tuple(required)
    width = max(len(left), len(right))
    pad = lambda parts: parts + (0,) * (width - len(parts))  # noqa: E731
    return pad(left) >= pad(right)


def host_platform() -> dict[str, object]:
    """The running host, in the shape a release manifest declares.

    `system` and `machine` mirror what `distribution/bundle.py` already
    compares for the product's own artifact, so the two stay consistent.
    """

    import platform as _platform

    release = _platform.mac_ver()[0] if _platform.system() == "Darwin" else ""
    return {
        "system": _platform.system(),
        "machine": _platform.machine(),
        "minimum_os_version": release or None,
    }


@dataclass(frozen=True)
class PlatformTarget:
    """The host a release's dependency closure was resolved for.

    Deliberately not a PEP 425 tag. Matching wheel tags properly is a job for
    `packaging`, and a hand-written version of it has already cost this project
    six defects, five of them silent wrong answers. This carries only what can
    be compared without a grammar.
    """

    system: str
    machine: str
    minimum_os_version: str | None

    @classmethod
    def from_dict(cls, raw: object) -> PlatformTarget:
        data = _closed(
            raw, {"system", "machine", "minimum_os_version"}, "platform target"
        )
        minimum = data["minimum_os_version"]
        if minimum is not None:
            if not isinstance(minimum, str) or _OS_VERSION.fullmatch(minimum) is None:
                raise ValidationError(
                    "platform minimum_os_version must be a dotted version or null"
                )
        return cls(
            system=_text(data, "system"),
            machine=_text(data, "machine"),
            minimum_os_version=minimum,
        )

    def satisfied_by(self, host: Mapping[str, object]) -> bool:
        if host.get("system") != self.system or host.get("machine") != self.machine:
            return False
        if self.minimum_os_version is None:
            return True
        observed = host.get("minimum_os_version")
        if not isinstance(observed, str) or _OS_VERSION.fullmatch(observed) is None:
            # The release demands a floor and the host cannot state its version.
            return False
        return _os_version_at_least(observed, self.minimum_os_version)


@dataclass(frozen=True)
class WorkerRuntime:
    """The interpreter one release carries for its own worker.

    Four keys, closed: the archive's place inside `content/`, the digest of the
    bytes that were packaged, the interpreter's place inside the expanded tree,
    and the CPython version that selects the staging profile. Every one of them
    is derived by the packaging tool from what it packaged — none is typed —
    and `archive_sha256` is checked a second time at stage from a freshly
    opened descriptor, because a recorded verdict is forgeable while the
    release is unsigned.

    There is deliberately no archive *size* field. The digest is the binding,
    the slot's content-tree digest already covers the archive's size and bytes,
    and a second self-consistent number would only look like a check.
    """

    archive: str
    archive_sha256: str
    interpreter_relative: str
    python_version: str

    FIELDS = {"archive", "archive_sha256", "interpreter_relative", "python_version"}

    @classmethod
    def from_dict(cls, raw: object) -> WorkerRuntime:
        data = _closed(raw, cls.FIELDS, "worker runtime")
        version = _text(data, "python_version")
        if not _PYTHON_VERSION.fullmatch(version):
            raise ValidationError("python_version must be a dotted CPython release")
        return cls(
            archive=_safe_relative_path(_text(data, "archive"), "archive"),
            archive_sha256=_digest(data, "archive_sha256"),
            interpreter_relative=_safe_relative_path(
                _text(data, "interpreter_relative"), "interpreter_relative"
            ),
            python_version=version,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _worker_modules(raw: object) -> tuple[tuple[str, str], ...]:
    """The stdlib-only worker modules the release carries beside its entrypoint.

    Empty in S3.2 and filled by S3.3: the launch contract changes here, the
    worker-side protocol implementation ships in the next slice. An empty map is
    therefore a valid release, but an *absent* key is not — the field is part of
    the closed schema so that a release which forgot to declare its modules
    cannot be mistaken for one that legitimately carries none.
    """

    if not isinstance(raw, dict):
        raise ValidationError("worker_modules must be an object")
    entries: list[tuple[str, str]] = []
    for key in sorted(raw):
        if not isinstance(key, str):
            raise ValidationError("worker_modules paths must be strings")
        path = _safe_relative_path(key, "worker_modules path")
        entries.append((path, _digest(raw, key)))
    return tuple(entries)


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    release_id: str
    release_sequence: int
    distribution_name: str
    distribution_version: str
    upstream_repository: str
    upstream_tag: str
    upstream_commit: str
    artifact_filename: str
    artifact_sha256: str
    publisher: str
    workflow: str
    python_range: str
    dependency_lock_sha256: str
    adapter_protocol: str
    session_schema: int
    patch_set_sha256: str
    evidence_sha256: str
    worker_entrypoint: str
    worker_runtime: WorkerRuntime
    worker_modules: tuple[tuple[str, str], ...]
    platform: PlatformTarget

    FIELDS = {
        "schema_version", "release_id", "release_sequence", "distribution_name",
        "distribution_version", "upstream_repository", "upstream_tag",
        "upstream_commit", "artifact_filename", "artifact_sha256", "publisher",
        "workflow", "python_range", "dependency_lock_sha256", "adapter_protocol",
        "session_schema", "patch_set_sha256", "evidence_sha256", "worker_entrypoint",
        "worker_runtime", "worker_modules", "platform",
    }

    @classmethod
    def from_dict(cls, raw: object) -> ReleaseManifest:
        data = _closed(raw, cls.FIELDS, "release manifest")
        if _positive_int(data, "schema_version") != 3:
            # Schema 2 is refused, not migrated. Production has imported zero
            # releases, so there is nothing to carry forward, and a migration
            # would have to invent the `worker_runtime` a schema-2 release never
            # packaged — the one thing this schema exists to make underivable.
            raise ValidationError("unsupported release manifest schema")
        artifact = _identifier(data, "artifact_filename")
        entrypoint = _text(data, "worker_entrypoint")
        if entrypoint.startswith("/") or entrypoint.split("/") != [entrypoint]:
            raise ValidationError("worker_entrypoint must be one safe file name")
        commit = _text(data, "upstream_commit")
        if not _COMMIT.fullmatch(commit):
            raise ValidationError("upstream_commit must be a full lowercase commit")
        result = cls(
            schema_version=3,
            release_id=_identifier(data, "release_id"),
            release_sequence=_positive_int(data, "release_sequence"),
            distribution_name=_identifier(data, "distribution_name"),
            distribution_version=_text(data, "distribution_version"),
            upstream_repository=_text(data, "upstream_repository"),
            upstream_tag=_identifier(data, "upstream_tag"),
            upstream_commit=commit,
            artifact_filename=artifact,
            artifact_sha256=_digest(data, "artifact_sha256"),
            publisher=_text(data, "publisher"),
            workflow=_identifier(data, "workflow"),
            python_range=_text(data, "python_range"),
            dependency_lock_sha256=_digest(data, "dependency_lock_sha256"),
            adapter_protocol=_text(data, "adapter_protocol"),
            session_schema=_positive_int(data, "session_schema"),
            patch_set_sha256=_digest(data, "patch_set_sha256"),
            evidence_sha256=_digest(data, "evidence_sha256"),
            worker_entrypoint=entrypoint,
            worker_runtime=WorkerRuntime.from_dict(data["worker_runtime"]),
            worker_modules=_worker_modules(data["worker_modules"]),
            platform=PlatformTarget.from_dict(data["platform"]),
        )
        if result.distribution_name != "hermes-agent":
            raise ValidationError("distribution_name must be hermes-agent")
        return result

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        # `asdict` renders the module map as the tuple of pairs the dataclass
        # stores (a frozen manifest cannot hold a mutable dict); the document
        # shape is an object, and it is the document that is digested.
        data["worker_modules"] = dict(self.worker_modules)
        return data

    @property
    def digest(self) -> str:
        return digest_document(self.to_dict())


@dataclass(frozen=True)
class CatalogEntry:
    release_id: str
    manifest_sha256: str
    status: str

    @classmethod
    def from_dict(cls, raw: object) -> CatalogEntry:
        data = _closed(raw, {"release_id", "manifest_sha256", "status"}, "catalog entry")
        status = _text(data, "status")
        if status not in _STATUS:
            raise ValidationError("unsupported catalog release status")
        return cls(
            release_id=_identifier(data, "release_id"),
            manifest_sha256=_digest(data, "manifest_sha256"),
            status=status,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogPayload:
    schema_version: int
    sequence: int
    issued_at: str
    expires_at: str
    entries: tuple[CatalogEntry, ...]

    @classmethod
    def from_dict(cls, raw: object) -> CatalogPayload:
        data = _closed(
            raw,
            {"schema_version", "sequence", "issued_at", "expires_at", "entries"},
            "catalog payload",
        )
        if _positive_int(data, "schema_version") != 1:
            raise ValidationError("unsupported catalog schema")
        issued = _timestamp(data["issued_at"], "issued_at")
        expires = _timestamp(data["expires_at"], "expires_at")
        if expires <= issued:
            raise ValidationError("catalog expiration must follow issuance")
        entries_raw = data["entries"]
        if not isinstance(entries_raw, list) or not entries_raw:
            raise ValidationError("catalog entries must be a non-empty list")
        entries = tuple(CatalogEntry.from_dict(item) for item in entries_raw)
        ids = [entry.release_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValidationError("catalog release identifiers must be unique")
        return cls(
            schema_version=1,
            sequence=_positive_int(data, "sequence", zero=True),
            issued_at=data["issued_at"],
            expires_at=data["expires_at"],
            entries=entries,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class CatalogEnvelope:
    payload: CatalogPayload
    key_id: str
    signature: str

    @classmethod
    def from_dict(cls, raw: object) -> CatalogEnvelope:
        data = _closed(raw, {"payload", "key_id", "signature"}, "catalog envelope")
        return cls(
            payload=CatalogPayload.from_dict(data["payload"]),
            key_id=_identifier(data, "key_id"),
            signature=_text(data, "signature"),
        )


@dataclass(frozen=True)
class PatchEntry:
    patch_id: str
    source_commit: str
    patch_sha256: str
    disposition: str

    @classmethod
    def from_dict(cls, raw: object) -> PatchEntry:
        data = _closed(
            raw,
            {"patch_id", "source_commit", "patch_sha256", "disposition"},
            "patch entry",
        )
        source = _text(data, "source_commit")
        if not _COMMIT.fullmatch(source):
            raise ValidationError("patch source_commit must be a full commit")
        disposition = _text(data, "disposition")
        if disposition not in {"required", "upstreamed", "dropped"}:
            raise ValidationError("unsupported patch disposition")
        return cls(
            patch_id=_identifier(data, "patch_id"),
            source_commit=source,
            patch_sha256=_digest(data, "patch_sha256"),
            disposition=disposition,
        )


@dataclass(frozen=True)
class PatchLedger:
    schema_version: int
    release_id: str
    upstream_commit: str
    patches: tuple[PatchEntry, ...]

    @classmethod
    def from_dict(cls, raw: object) -> PatchLedger:
        data = _closed(
            raw,
            {"schema_version", "release_id", "upstream_commit", "patches"},
            "patch ledger",
        )
        if _positive_int(data, "schema_version") != 1:
            raise ValidationError("unsupported patch ledger schema")
        upstream = _text(data, "upstream_commit")
        if not _COMMIT.fullmatch(upstream):
            raise ValidationError("patch ledger upstream_commit must be a full commit")
        patches_raw = data["patches"]
        if not isinstance(patches_raw, list):
            raise ValidationError("patches must be a list")
        patches = tuple(PatchEntry.from_dict(item) for item in patches_raw)
        return cls(
            schema_version=1,
            release_id=_identifier(data, "release_id"),
            upstream_commit=upstream,
            patches=patches,
        )

    @property
    def digest(self) -> str:
        return digest_document(
            {
                "schema_version": self.schema_version,
                "release_id": self.release_id,
                "upstream_commit": self.upstream_commit,
                "patches": [asdict(patch) for patch in self.patches],
            }
        )
