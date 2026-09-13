"""Closed version-one contracts for Cortex research artifacts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from cortex_platform.product.resources import parse_resource_uri


_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA_TYPE_RE = re.compile(
    r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z"
)
_MAX_BYTE_LENGTH = (1 << 63) - 1
MAX_PARENT_INPUTS = 256


class ValidationError(ValueError):
    """An artifact document violates its closed v1 contract."""


def _closed(raw: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValidationError(f"{name} fields do not match schema")
    return raw


def _schema_v1(raw: Mapping[str, object], name: str) -> None:
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValidationError(f"unsupported {name} schema")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a canonical lowercase identifier")
    return value


def _display_text(value: object, name: str, *, maximum: int = 2_000) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValidationError(f"{name} must be non-empty canonical Unicode text")
    return value


def _producer_part(value: object, name: str) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a canonical producer identifier")
    return value


def _sha256(value: object, name: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _byte_length(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_BYTE_LENGTH:
        raise ValidationError("byte_length must be a bounded non-negative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _media_type(value: object) -> str:
    if not isinstance(value, str) or _MEDIA_TYPE_RE.fullmatch(value) is None:
        raise ValidationError(
            "media_type must be lowercase type/subtype without parameters"
        )
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{name} must be a canonical RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(
            f"{name} must be a canonical RFC3339 UTC timestamp"
        ) from exc
    if parsed.tzinfo != timezone.utc:
        raise ValidationError(f"{name} must be UTC")
    timespec = "microseconds" if parsed.microsecond else "seconds"
    canonical = parsed.isoformat(timespec=timespec).replace("+00:00", "Z")
    if value != canonical:
        raise ValidationError(f"{name} must use canonical RFC3339 formatting")
    return value


def _ordered_identifiers(
    value: object,
    name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a list")
    identifiers = tuple(_identifier(item, name) for item in value)
    if not allow_empty and not identifiers:
        raise ValidationError(f"{name} must not be empty")
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError(f"{name} entries must be unique")
    if list(identifiers) != sorted(identifiers):
        raise ValidationError(f"{name} entries must use canonical order")
    return identifiers


@dataclass(frozen=True)
class ProducerIdentity:
    """A redacted generator or tool identity, never a provider trace."""

    name: str
    version: str

    def __post_init__(self) -> None:
        _identifier(self.name, "producer name")
        _producer_part(self.version, "producer version")

    @classmethod
    def from_dict(cls, raw: object) -> ProducerIdentity:
        data = _closed(raw, {"name", "version"}, "producer identity")
        return cls(
            name=_identifier(data["name"], "producer name"),
            version=_producer_part(data["version"], "producer version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class ParentVersionInput:
    """An exact immutable parent version and its expected content hash."""

    artifact_version_id: str
    sha256: str

    def __post_init__(self) -> None:
        _identifier(self.artifact_version_id, "parent artifact_version_id")
        _sha256(self.sha256, "parent sha256")

    @classmethod
    def from_dict(cls, raw: object) -> ParentVersionInput:
        data = _closed(raw, {"artifact_version_id", "sha256"}, "parent input")
        return cls(
            artifact_version_id=_identifier(
                data["artifact_version_id"], "parent artifact_version_id"
            ),
            sha256=_sha256(data["sha256"], "parent sha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_version_id": self.artifact_version_id,
            "sha256": self.sha256,
        }


def parse_parent_inputs(value: object) -> tuple[ParentVersionInput, ...]:
    if not isinstance(value, list):
        raise ValidationError("parents must be a list")
    if len(value) > MAX_PARENT_INPUTS:
        raise ValidationError("parent inputs exceed the v1 parent limit")
    parents = tuple(ParentVersionInput.from_dict(item) for item in value)
    identities = [parent.artifact_version_id for parent in parents]
    if len(identities) != len(set(identities)):
        raise ValidationError("parent artifact versions must be unique")
    if identities != sorted(identities):
        raise ValidationError("parent inputs must use canonical order")
    return parents


@dataclass(frozen=True)
class Provenance:
    """Closed, redacted provenance for one immutable artifact version."""

    schema_version: int
    run_id: str
    attempt_id: str
    source_ids: tuple[str, ...]
    research_engine_refs: tuple[str, ...]
    generator: ProducerIdentity
    tool: ProducerIdentity
    parents: tuple[ParentVersionInput, ...]
    media_type: str
    byte_length: int
    sha256: str
    committed_at: str
    document_version_ids: tuple[str, ...] = ()
    research_context_sha256: str | None = None

    FIELDS = {
        "schema_version",
        "run_id",
        "attempt_id",
        "source_ids",
        "research_engine_refs",
        "generator",
        "tool",
        "parents",
        "media_type",
        "byte_length",
        "sha256",
        "committed_at",
    }

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ValidationError("unsupported provenance schema")
        _identifier(self.run_id, "run_id")
        _identifier(self.attempt_id, "attempt_id")
        _validate_identifier_tuple(self.source_ids, "source_ids", allow_empty=self.schema_version == 2)
        _validate_identifier_tuple(
            self.document_version_ids, "document_version_ids", allow_empty=self.schema_version == 1
        )
        if self.schema_version == 1:
            if self.document_version_ids or self.research_context_sha256 is not None:
                raise ValidationError("v1 provenance cannot carry document evidence")
        else:
            _sha256(self.research_context_sha256)
        _validate_identifier_tuple(
            self.research_engine_refs, "research_engine_refs", allow_empty=True
        )
        if not isinstance(self.generator, ProducerIdentity) or not isinstance(
            self.tool, ProducerIdentity
        ):
            raise ValidationError("generator and tool must be producer identities")
        _validate_parent_tuple(self.parents)
        _media_type(self.media_type)
        _byte_length(self.byte_length)
        _sha256(self.sha256)
        _timestamp(self.committed_at, "committed_at")

    @classmethod
    def from_dict(cls, raw: object) -> Provenance:
        version = raw.get("schema_version") if isinstance(raw, dict) else None
        fields = cls.FIELDS | {"document_version_ids", "research_context_sha256"} if version == 2 else cls.FIELDS
        data = _closed(raw, fields, "provenance")
        return cls(
            schema_version=data["schema_version"],
            run_id=_identifier(data["run_id"], "run_id"),
            attempt_id=_identifier(data["attempt_id"], "attempt_id"),
            source_ids=_ordered_identifiers(
                data["source_ids"], "source_ids", allow_empty=version == 2
            ),
            research_engine_refs=_ordered_identifiers(
                data["research_engine_refs"],
                "research_engine_refs",
                allow_empty=True,
            ),
            generator=ProducerIdentity.from_dict(data["generator"]),
            tool=ProducerIdentity.from_dict(data["tool"]),
            parents=parse_parent_inputs(data["parents"]),
            media_type=_media_type(data["media_type"]),
            byte_length=_byte_length(data["byte_length"]),
            sha256=_sha256(data["sha256"]),
            committed_at=_timestamp(data["committed_at"], "committed_at"),
            document_version_ids=(
                _ordered_identifiers(data["document_version_ids"], "document_version_ids", allow_empty=False)
                if version == 2 else ()
            ),
            research_context_sha256=data["research_context_sha256"] if version == 2 else None,
        )

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "source_ids": list(self.source_ids),
            "research_engine_refs": list(self.research_engine_refs),
            "generator": self.generator.to_dict(),
            "tool": self.tool.to_dict(),
            "parents": [parent.to_dict() for parent in self.parents],
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "committed_at": self.committed_at,
        }
        if self.schema_version == 2:
            result.update(document_version_ids=list(self.document_version_ids),
                          research_context_sha256=self.research_context_sha256)
        return result


def _validate_identifier_tuple(
    values: tuple[str, ...], name: str, *, allow_empty: bool
) -> None:
    if not isinstance(values, tuple):
        raise ValidationError(f"{name} must be an immutable tuple")
    validated = tuple(_identifier(value, name) for value in values)
    if not allow_empty and not validated:
        raise ValidationError(f"{name} must not be empty")
    if len(validated) != len(set(validated)):
        raise ValidationError(f"{name} entries must be unique")
    if list(validated) != sorted(validated):
        raise ValidationError(f"{name} entries must use canonical order")


def _validate_parent_tuple(values: tuple[ParentVersionInput, ...]) -> None:
    if not isinstance(values, tuple) or not all(
        isinstance(value, ParentVersionInput) for value in values
    ):
        raise ValidationError("parents must be immutable parent inputs")
    if len(values) > MAX_PARENT_INPUTS:
        raise ValidationError("parent inputs exceed the v1 parent limit")
    identities = [value.artifact_version_id for value in values]
    if len(identities) != len(set(identities)):
        raise ValidationError("parent artifact versions must be unique")
    if identities != sorted(identities):
        raise ValidationError("parent inputs must use canonical order")


@dataclass(frozen=True)
class Artifact:
    """A stable logical artifact identity without a mutable current pointer."""

    schema_version: int
    artifact_id: str
    workspace_id: str
    thread_id: str
    kind: str
    title: str

    FIELDS = {
        "schema_version",
        "artifact_id",
        "workspace_id",
        "thread_id",
        "kind",
        "title",
    }

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValidationError("unsupported artifact schema")
        _identifier(self.artifact_id, "artifact_id")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.thread_id, "thread_id")
        _identifier(self.kind, "kind")
        _display_text(self.title, "title")

    @classmethod
    def from_dict(cls, raw: object) -> Artifact:
        data = _closed(raw, cls.FIELDS, "artifact")
        _schema_v1(data, "artifact")
        return cls(
            schema_version=1,
            artifact_id=_identifier(data["artifact_id"], "artifact_id"),
            workspace_id=_identifier(data["workspace_id"], "workspace_id"),
            thread_id=_identifier(data["thread_id"], "thread_id"),
            kind=_identifier(data["kind"], "kind"),
            title=_display_text(data["title"], "title"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact_id": self.artifact_id,
            "workspace_id": self.workspace_id,
            "thread_id": self.thread_id,
            "kind": self.kind,
            "title": self.title,
        }


@dataclass(frozen=True)
class ArtifactVersion:
    """An immutable content-addressed version of a logical artifact."""

    schema_version: int
    artifact_version_id: str
    artifact_id: str
    logical_version: int
    resource_uri: str
    sha256: str
    byte_length: int
    media_type: str
    parents: tuple[ParentVersionInput, ...]
    provenance: Provenance
    committed_at: str

    FIELDS = {
        "schema_version",
        "artifact_version_id",
        "artifact_id",
        "logical_version",
        "resource_uri",
        "sha256",
        "byte_length",
        "media_type",
        "parents",
        "provenance",
        "committed_at",
    }

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValidationError("unsupported artifact version schema")
        _identifier(self.artifact_version_id, "artifact_version_id")
        _identifier(self.artifact_id, "artifact_id")
        _positive_int(self.logical_version, "logical_version")
        _validate_artifact_uri(
            self.resource_uri, self.artifact_id, self.artifact_version_id
        )
        _sha256(self.sha256)
        _byte_length(self.byte_length)
        _media_type(self.media_type)
        _validate_parent_tuple(self.parents)
        if any(
            parent.artifact_version_id == self.artifact_version_id
            for parent in self.parents
        ):
            raise ValidationError("an artifact version cannot parent itself")
        if not isinstance(self.provenance, Provenance):
            raise ValidationError("provenance must be a closed provenance document")
        _timestamp(self.committed_at, "committed_at")
        if (
            self.provenance.parents != self.parents
            or self.provenance.sha256 != self.sha256
            or self.provenance.byte_length != self.byte_length
            or self.provenance.media_type != self.media_type
            or self.provenance.committed_at != self.committed_at
        ):
            raise ValidationError("artifact version and provenance inputs disagree")

    @classmethod
    def from_dict(cls, raw: object) -> ArtifactVersion:
        data = _closed(raw, cls.FIELDS, "artifact version")
        _schema_v1(data, "artifact version")
        artifact_id = _identifier(data["artifact_id"], "artifact_id")
        return cls(
            schema_version=1,
            artifact_version_id=_identifier(
                data["artifact_version_id"], "artifact_version_id"
            ),
            artifact_id=artifact_id,
            logical_version=_positive_int(data["logical_version"], "logical_version"),
            resource_uri=_validate_artifact_uri(
                data["resource_uri"],
                artifact_id,
                _identifier(data["artifact_version_id"], "artifact_version_id"),
            ),
            sha256=_sha256(data["sha256"]),
            byte_length=_byte_length(data["byte_length"]),
            media_type=_media_type(data["media_type"]),
            parents=parse_parent_inputs(data["parents"]),
            provenance=Provenance.from_dict(data["provenance"]),
            committed_at=_timestamp(data["committed_at"], "committed_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact_version_id": self.artifact_version_id,
            "artifact_id": self.artifact_id,
            "logical_version": self.logical_version,
            "resource_uri": self.resource_uri,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "parents": [parent.to_dict() for parent in self.parents],
            "provenance": self.provenance.to_dict(),
            "committed_at": self.committed_at,
        }


def _validate_artifact_uri(
    value: object, artifact_id: str, artifact_version_id: str
) -> str:
    if not isinstance(value, str):
        raise ValidationError("resource_uri must be a canonical Cortex URI")
    try:
        parsed = parse_resource_uri(value)
    except ValueError as exc:
        raise ValidationError("resource_uri must be a canonical Cortex URI") from exc
    if parsed.root != "artifacts" or parsed.segments != (
        artifact_id,
        artifact_version_id,
    ):
        raise ValidationError(
            "resource_uri must identify this exact artifact version in artifacts"
        )
    return value


@dataclass(frozen=True)
class SnapshotMember:
    artifact_id: str
    artifact_version_id: str
    logical_version: int
    sha256: str

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, "snapshot artifact_id")
        _identifier(self.artifact_version_id, "snapshot artifact_version_id")
        _positive_int(self.logical_version, "snapshot logical_version")
        _sha256(self.sha256, "snapshot sha256")

    @classmethod
    def from_dict(cls, raw: object) -> SnapshotMember:
        data = _closed(
            raw,
            {"artifact_id", "artifact_version_id", "logical_version", "sha256"},
            "snapshot member",
        )
        return cls(
            artifact_id=_identifier(data["artifact_id"], "snapshot artifact_id"),
            artifact_version_id=_identifier(
                data["artifact_version_id"], "snapshot artifact_version_id"
            ),
            logical_version=_positive_int(
                data["logical_version"], "snapshot logical_version"
            ),
            sha256=_sha256(data["sha256"], "snapshot sha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_version_id": self.artifact_version_id,
            "logical_version": self.logical_version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Snapshot:
    """A frozen set of exact artifact version identities and hashes."""

    schema_version: int
    snapshot_id: str
    workspace_id: str
    name: str
    members: tuple[SnapshotMember, ...]
    created_at: str

    FIELDS = {
        "schema_version",
        "snapshot_id",
        "workspace_id",
        "name",
        "members",
        "created_at",
    }

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValidationError("unsupported snapshot schema")
        _identifier(self.snapshot_id, "snapshot_id")
        _identifier(self.workspace_id, "workspace_id")
        _display_text(self.name, "snapshot name")
        _validate_snapshot_members(self.members)
        _timestamp(self.created_at, "created_at")

    @classmethod
    def from_dict(cls, raw: object) -> Snapshot:
        data = _closed(raw, cls.FIELDS, "snapshot")
        _schema_v1(data, "snapshot")
        members_raw = data["members"]
        if not isinstance(members_raw, list):
            raise ValidationError("snapshot members must be a list")
        return cls(
            schema_version=1,
            snapshot_id=_identifier(data["snapshot_id"], "snapshot_id"),
            workspace_id=_identifier(data["workspace_id"], "workspace_id"),
            name=_display_text(data["name"], "snapshot name"),
            members=tuple(SnapshotMember.from_dict(item) for item in members_raw),
            created_at=_timestamp(data["created_at"], "created_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "members": [member.to_dict() for member in self.members],
            "created_at": self.created_at,
        }


def _validate_snapshot_members(members: tuple[SnapshotMember, ...]) -> None:
    if (
        not isinstance(members, tuple)
        or not members
        or not all(isinstance(member, SnapshotMember) for member in members)
    ):
        raise ValidationError("snapshot members must be a non-empty immutable tuple")
    artifact_ids = [member.artifact_id for member in members]
    version_ids = [member.artifact_version_id for member in members]
    if len(artifact_ids) != len(set(artifact_ids)) or len(version_ids) != len(
        set(version_ids)
    ):
        raise ValidationError("snapshot artifact and version identities must be unique")
    order = [(member.artifact_id, member.artifact_version_id) for member in members]
    if order != sorted(order):
        raise ValidationError("snapshot members must use canonical order")


def canonical_parent_dicts(
    parents: Sequence[ParentVersionInput],
) -> list[dict[str, object]]:
    """Return the canonical public shape for exact parent inputs."""

    values = tuple(parents)
    _validate_parent_tuple(values)
    return [parent.to_dict() for parent in values]
