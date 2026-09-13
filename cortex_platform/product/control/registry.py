"""Private typed contracts for durable operations registry state."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_LOGICAL_ID_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_ROOT_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")


@dataclass(frozen=True)
class AssetRootRecord:
    root_id: str
    private_path: Path
    max_bytes: int
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AdoptionManifestRecord:
    """One committed bulk adoption: the resolution for a whole copied corpus."""

    manifest_id: str
    corpus_root_id: str
    actor_id: str
    entry_count: int
    adopted_count: int
    committed_at: datetime


@dataclass(frozen=True)
class RuntimeActivationRecord:
    """One durable decision about whether managed runtime dispatch may run."""

    id: str
    decision: str
    mode: str | None
    expires_at: datetime | None
    actor_id: str
    decided_at: datetime


@dataclass(frozen=True)
class RuntimeReleaseApprovalRecord:
    """One durable operator decision about one exact runtime release."""

    id: str
    decision: str
    release_id: str
    manifest_sha256: str
    actor_id: str
    decided_at: datetime


@dataclass(frozen=True)
class TransportActivationRecord:
    """One durable decision about whether a transport may send at all.

    A mirror of `RuntimeActivationRecord` with the transport it decides for,
    kept separate because the two gates guard different code paths and a
    single table would have made one audit log answer two questions.
    """

    id: str
    transport: str
    decision: str
    scope: str | None
    expires_at: datetime | None
    actor_id: str
    decided_at: datetime


# A transport window that ends without a proven release is the failure the
# audit trail exists to record, so both endings are named here rather than
# spelled out at the call sites that write them.
TRANSPORT_WINDOW_AGGREGATE = "transport_window"
TRANSPORT_WINDOW_CLOSED_EVENT = "transport_window_closed"
TRANSPORT_WINDOW_ABORTED_EVENT = "transport_window_aborted"


@dataclass(frozen=True)
class ConnectorRecord:
    id: str
    kind: str
    adapter_id: str
    display_name: str
    credential_alias: str | None
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ControlStoreIdentity:
    schema_version: int
    schema_fingerprint_sha256: str
    identity_companion_sha256: str
    identity_companion_byte_length: int


@dataclass(frozen=True)
class ControlStoreSnapshot:
    schema_version: int
    schema_fingerprint_sha256: str
    database_sha256: str
    database_byte_length: int
    identity_companion_sha256: str
    identity_companion_byte_length: int


@dataclass(frozen=True)
class ProtectedLogicalSnapshot:
    logical_id: str
    content_sha256: str
    byte_length: int


@dataclass(frozen=True)
class ProtectedAssetRootSnapshot:
    root_id: str
    revision: int
    content_manifest_sha256: str
    file_count: int
    byte_length: int


@dataclass(frozen=True)
class ProtectedSetManifest:
    schema_version: int
    control_store: ControlStoreSnapshot
    logical_snapshots: tuple[ProtectedLogicalSnapshot, ...]
    asset_roots: tuple[ProtectedAssetRootSnapshot, ...]


@dataclass(frozen=True)
class BackupCopyProof:
    backup_set_digest: str
    completed_at: datetime
    snapshot_count: int
    verified: bool


@dataclass(frozen=True)
class RestoreVerification:
    backup_set_digest: str
    completed_at: datetime
    database_count: int
    verified_sample_count: int
    verified: bool


@dataclass(frozen=True)
class PairedBackupProofInput:
    proof_id: str
    protected_set_manifest: ProtectedSetManifest
    primary: BackupCopyProof
    independent: BackupCopyProof
    restore: RestoreVerification


@dataclass(frozen=True)
class PairedBackupProofRecord:
    id: str
    backup_set_digest: str
    protected_set_manifest: ProtectedSetManifest
    primary_completed_at: datetime
    primary_snapshot_count: int
    independent_completed_at: datetime
    independent_snapshot_count: int
    restore_completed_at: datetime
    restored_database_count: int
    verified_sample_count: int
    created_at: datetime


@dataclass(frozen=True)
class HealthSubject:
    kind: Literal["asset_root", "connector", "backup_proof"]
    id: str


@dataclass(frozen=True)
class SystemHealthObservationInput:
    observation_id: str
    subject: HealthSubject
    status: Literal["ok", "degraded", "unavailable", "unknown"]
    category: str
    observed_at: datetime
    metrics: tuple[tuple[str, bool | int], ...]


@dataclass(frozen=True)
class SystemHealthObservationRecord:
    id: str
    subject: HealthSubject
    status: Literal["ok", "degraded", "unavailable", "unknown"]
    category: str
    observed_at: datetime
    metrics: tuple[tuple[str, bool | int], ...]
    created_at: datetime


def canonical_protected_set_manifest(manifest: ProtectedSetManifest) -> bytes:
    """Validate and encode one canonical logical protected-set manifest."""

    if type(manifest) is not ProtectedSetManifest:
        raise ValueError("manifest must be a ProtectedSetManifest")
    _require_integer(manifest.schema_version, name="schema_version", minimum=1)
    if manifest.schema_version != 1:
        raise ValueError("schema_version must be 1")

    control_store = manifest.control_store
    if type(control_store) is not ControlStoreSnapshot:
        raise ValueError("control_store must be a ControlStoreSnapshot")
    # The schema version describes the state this proof was taken OF, not the
    # build reading it. Requiring it to equal the current build's version
    # version-locks the format in both directions: pinned to a literal, no new
    # proof can be recorded after a migration; pinned to the live version, every
    # PRE-migration proof becomes unreadable -- which strands an instance that
    # has already migrated. Whether a proof still covers the current state is a
    # coverage question, and `BackupBackedSafetyPort` answers it by re-deriving
    # the live identity and comparing the fingerprint.
    _require_integer(
        control_store.schema_version,
        name="control_store.schema_version",
        minimum=1,
    )
    _require_digest(
        control_store.schema_fingerprint_sha256,
        name="schema_fingerprint_sha256",
    )
    _require_digest(control_store.database_sha256, name="database_sha256")
    _require_integer(
        control_store.database_byte_length,
        name="database_byte_length",
        minimum=1,
    )
    _require_digest(
        control_store.identity_companion_sha256,
        name="identity_companion_sha256",
    )
    _require_integer(
        control_store.identity_companion_byte_length,
        name="identity_companion_byte_length",
        minimum=1,
    )

    logical_snapshots = _validated_logical_snapshots(manifest.logical_snapshots)
    asset_roots = _validated_asset_roots(manifest.asset_roots)
    projected = {
        "schema_version": 1,
        "control_store": {
            "schema_version": control_store.schema_version,
            "schema_fingerprint_sha256": control_store.schema_fingerprint_sha256,
            "database_sha256": control_store.database_sha256,
            "database_byte_length": control_store.database_byte_length,
            "identity_companion_sha256": control_store.identity_companion_sha256,
            "identity_companion_byte_length": (
                control_store.identity_companion_byte_length
            ),
        },
        "logical_snapshots": logical_snapshots,
        "asset_roots": asset_roots,
    }
    return json.dumps(
        projected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def protected_set_digest(manifest: ProtectedSetManifest) -> str:
    """Return the SHA-256 digest of the canonical protected-set manifest."""

    return hashlib.sha256(canonical_protected_set_manifest(manifest)).hexdigest()


def _validated_logical_snapshots(
    values: tuple[ProtectedLogicalSnapshot, ...],
) -> list[dict[str, str | int]]:
    if type(values) is not tuple:
        raise ValueError("logical_snapshots must be a tuple")
    result: list[dict[str, str | int]] = []
    identifiers: list[str] = []
    for value in values:
        if type(value) is not ProtectedLogicalSnapshot:
            raise ValueError("logical_snapshots contains an invalid value")
        _require_identifier(
            value.logical_id,
            name="logical_id",
            pattern=_LOGICAL_ID_PATTERN,
        )
        _require_digest(value.content_sha256, name="content_sha256")
        _require_integer(value.byte_length, name="byte_length", minimum=1)
        identifiers.append(value.logical_id)
        result.append(
            {
                "logical_id": value.logical_id,
                "content_sha256": value.content_sha256,
                "byte_length": value.byte_length,
            }
        )
    _require_canonical_order(identifiers, name="logical_snapshots")
    return result


def _validated_asset_roots(
    values: tuple[ProtectedAssetRootSnapshot, ...],
) -> list[dict[str, str | int]]:
    if type(values) is not tuple:
        raise ValueError("asset_roots must be a tuple")
    result: list[dict[str, str | int]] = []
    identifiers: list[str] = []
    for value in values:
        if type(value) is not ProtectedAssetRootSnapshot:
            raise ValueError("asset_roots contains an invalid value")
        _require_identifier(value.root_id, name="root_id", pattern=_ROOT_ID_PATTERN)
        _require_integer(value.revision, name="revision", minimum=0)
        _require_digest(
            value.content_manifest_sha256,
            name="content_manifest_sha256",
        )
        _require_integer(value.file_count, name="file_count", minimum=0)
        _require_integer(value.byte_length, name="byte_length", minimum=0)
        identifiers.append(value.root_id)
        result.append(
            {
                "root_id": value.root_id,
                "revision": value.revision,
                "content_manifest_sha256": value.content_manifest_sha256,
                "file_count": value.file_count,
                "byte_length": value.byte_length,
            }
        )
    _require_canonical_order(identifiers, name="asset_roots")
    return result


def _require_identifier(value: object, *, name: str, pattern: re.Pattern[str]) -> None:
    if type(value) is not str or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{name} must be an NFC string")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid format")


def _require_digest(value: object, *, name: str) -> None:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _require_integer(value: object, *, name: str, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(
            f"{name} must be an integer greater than or equal to {minimum}"
        )


def _require_canonical_order(values: list[str], *, name: str) -> None:
    if values != sorted(values) or len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique and canonically ordered")
