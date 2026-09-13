"""One-use authorization boundary for distribution state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

# `ControlStore` writes exactly 32 bytes and refuses to open any other length
# (store.py `_load_or_create_binding_key`). A different length is a shape the
# product cannot produce, so it is refused rather than digested. The equality
# is pinned by test against the real store.
_IDENTITY_COMPANION_BYTES = 32
_MAX_MANIFEST_BYTES = 1 << 20
_DEFAULT_MAX_RESTORE_AGE = timedelta(days=7)
_FUTURE_TOLERANCE = timedelta(seconds=60)


class StateSafetyError(RuntimeError):
    """An upgrade could not prove that its state transition is safe."""


@dataclass(frozen=True)
class UpgradeRequest:
    """Exact release, schema, and state identity bound to one upgrade."""

    current_bundle_digest: str
    candidate_bundle_digest: str
    current_version: str
    candidate_version: str
    candidate_control_schema: int
    control_database: Path
    identity_companion: Path
    runtime_root: Path


class UpgradeAuthorization:
    """Opaque, process-local authorization consumed by the installer."""

    __slots__ = ()


class UpgradeSafetyPort(Protocol):
    """Issue and consume exact upgrade authorizations."""

    def authorize(self, request: UpgradeRequest) -> UpgradeAuthorization: ...

    def consume(
        self,
        authorization: UpgradeAuthorization,
        request: UpgradeRequest,
    ) -> None: ...


class FreshStateSafetyPort:
    """Authorize only installations that have never initialized Control state."""

    def __init__(self) -> None:
        self._issued: dict[int, tuple[UpgradeAuthorization, UpgradeRequest]] = {}

    @staticmethod
    def _require_fresh(request: UpgradeRequest) -> None:
        database_exists = os.path.lexists(request.control_database)
        identity_exists = os.path.lexists(request.identity_companion)
        if database_exists != identity_exists:
            raise StateSafetyError("control state is incomplete")
        if database_exists:
            raise StateSafetyError("verified backup authorization is required")

    def authorize(self, request: UpgradeRequest) -> UpgradeAuthorization:
        self._require_fresh(request)
        authorization = UpgradeAuthorization()
        self._issued[id(authorization)] = (authorization, request)
        return authorization

    def consume(
        self,
        authorization: UpgradeAuthorization,
        request: UpgradeRequest,
    ) -> None:
        issued = self._issued.pop(id(authorization), None)
        if issued is None or issued[0] is not authorization or issued[1] != request:
            raise StateSafetyError("upgrade authorization is not current")
        self._require_fresh(request)


@dataclass(frozen=True)
class ControlStateBinding:
    """Everything one look at the live Control state establishes about it.

    The first four fields are re-derived here, from the database file and its
    identity companion, using only the standard library. They must reproduce
    ``ControlStore.current_control_store_identity()`` exactly; the installer
    cannot import ``cortex_platform`` because it operates on a foreign
    generation's state, so that equality is pinned by test rather than by
    shared code.
    """

    schema_version: int
    schema_fingerprint_sha256: str
    identity_companion_sha256: str
    identity_companion_byte_length: int
    enabled_roots: tuple[tuple[str, int], ...]
    proof_id: str


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate manifest key")
        seen[key] = value
    return seen


class BackupBackedSafetyPort:
    """Authorize a state transition only against a verified paired backup proof.

    A fresh installation is authorized exactly as ``FreshStateSafetyPort``
    authorizes it. An installation that already owns Control state is
    authorized only when that state itself carries a paired backup proof which
    still covers it, whose restore leg was exercised recently, and whose schema
    the candidate build can read.

    The port never writes to the Control database, and it never restores state:
    "a rollback route is retained" means a verified restore is proven to exist
    and to be current, not that this code can perform one.
    """

    def __init__(
        self,
        *,
        max_restore_age: timedelta = _DEFAULT_MAX_RESTORE_AGE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(max_restore_age) is not timedelta or max_restore_age <= timedelta(0):
            raise ValueError("max_restore_age must be a positive timedelta")
        self._max_restore_age = max_restore_age
        self._clock = clock or (lambda: datetime.now(UTC))
        self._issued: dict[
            int,
            tuple[UpgradeAuthorization, UpgradeRequest, ControlStateBinding | None],
        ] = {}

    def authorize(self, request: UpgradeRequest) -> UpgradeAuthorization:
        binding = self.inspect(request)
        authorization = UpgradeAuthorization()
        self._issued[id(authorization)] = (authorization, request, binding)
        return authorization

    def consume(
        self,
        authorization: UpgradeAuthorization,
        request: UpgradeRequest,
    ) -> None:
        issued = self._issued.pop(id(authorization), None)
        if issued is None or issued[0] is not authorization or issued[1] != request:
            raise StateSafetyError("upgrade authorization is not current")
        if self.inspect(request) != issued[2]:
            raise StateSafetyError("control state changed during authorization")

    def inspect(self, request: UpgradeRequest) -> ControlStateBinding | None:
        """Prove the transition described by ``request`` is safe."""

        return self.inspect_state(
            control_database=request.control_database,
            identity_companion=request.identity_companion,
            runtime_root=request.runtime_root,
            candidate_control_schema=request.candidate_control_schema,
        )

    def inspect_state(
        self,
        *,
        control_database: Path,
        identity_companion: Path,
        runtime_root: Path,
        candidate_control_schema: int,
    ) -> ControlStateBinding | None:
        """Prove the transition is safe, or raise the first reason it is not.

        Returns ``None`` when the installation owns no Control state at all --
        the case the fresh port already allowed. Read-only, so ``doctor`` can
        report the outcome without mutating anything.
        """

        database = control_database
        companion = identity_companion
        database_exists = os.path.lexists(database)
        identity_exists = os.path.lexists(companion)
        if database_exists != identity_exists:
            raise StateSafetyError("control state is incomplete")
        if not database_exists:
            return None

        self._require_safe_state_file(database)
        self._require_safe_state_file(companion)
        self._require_checkpointed(database)
        if os.path.lexists(runtime_root / "lifecycle.json"):
            raise StateSafetyError("operation requires the lifecycle to be stopped")

        companion_bytes = self._read_companion(companion)
        schema_version, fingerprint, enabled_roots, proof = self._read_control_state(
            database
        )
        if proof is None:
            raise StateSafetyError("verified backup authorization is required")

        proof_id, manifest = self._validated_proof_manifest(proof)
        binding = ControlStateBinding(
            schema_version=schema_version,
            schema_fingerprint_sha256=fingerprint,
            identity_companion_sha256=hashlib.sha256(companion_bytes).hexdigest(),
            identity_companion_byte_length=len(companion_bytes),
            enabled_roots=enabled_roots,
            proof_id=proof_id,
        )
        self._require_coverage(manifest, binding)
        self._require_current_restore(proof)
        self._require_forward_migration(candidate_control_schema, schema_version)
        return binding

    @staticmethod
    def _require_safe_state_file(path: Path) -> None:
        try:
            details = path.lstat()
        except OSError as exc:
            raise StateSafetyError("control state is unreadable") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise StateSafetyError("control state is unsafe")

    @staticmethod
    def _require_checkpointed(database: Path) -> None:
        """Refuse state whose bytes are not all in the database file.

        A cleanly closed SQLite connection checkpoints and removes its write
        ahead log, so a non-empty ``-wal`` means the database file is not the
        whole state. Reading such a database would still be correct, but
        digesting or backing up the file alone would not be, and the read below
        opens it ``immutable=1``, which is sound only once this holds.
        """

        for suffix in ("-wal", "-journal"):
            sidecar = database.with_name(database.name + suffix)
            try:
                details = sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StateSafetyError("control state is unreadable") from exc
            if not stat.S_ISREG(details.st_mode) or details.st_size != 0:
                raise StateSafetyError("control state is not checkpointed")

    @staticmethod
    def _read_companion(path: Path) -> bytes:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise StateSafetyError("control state is unsafe") from exc
        try:
            payload = os.read(descriptor, _IDENTITY_COMPANION_BYTES + 1)
        except OSError as exc:
            raise StateSafetyError("control state is unreadable") from exc
        finally:
            os.close(descriptor)
        if len(payload) != _IDENTITY_COMPANION_BYTES:
            raise StateSafetyError("control state is unsafe")
        return payload

    @staticmethod
    def _read_control_state(
        database: Path,
    ) -> tuple[int, str, tuple[tuple[str, int], ...], tuple[object, ...] | None]:
        try:
            resolved = database.resolve(strict=True)
        except OSError as exc:
            raise StateSafetyError("control state is unreadable") from exc
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                connection.execute("PRAGMA query_only = ON")
                schema_rows = [
                    (str(row[0]), str(row[1]), str(row[2]))
                    for row in connection.execute(
                        """SELECT type, name, COALESCE(sql, '') FROM sqlite_schema
                           ORDER BY type, name"""
                    )
                ]
                present = {name for kind, name, _ in schema_rows if kind == "table"}
                if "schema_migrations" not in present:
                    raise StateSafetyError("control schema is invalid")
                versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                # A database that predates the operations registry carries no
                # proof at all. That is the missing authorization it is
                # reported as below, not a corruption -- and it can only ever
                # lead to a refusal. Enabled roots without a proofs table,
                # however, is a shape the store cannot produce.
                roots: tuple[tuple[str, int], ...] = ()
                proof: tuple[object, ...] | None = None
                if "paired_backup_proofs" in present:
                    if "asset_roots" not in present:
                        raise StateSafetyError("control schema is invalid")
                    roots = tuple(
                        (str(row[0]), int(row[1]))
                        for row in connection.execute(
                            """SELECT root_id, revision FROM asset_roots
                               WHERE enabled = 1 ORDER BY root_id"""
                        )
                    )
                    proof = connection.execute(
                        """SELECT id, backup_set_digest,
                                  protected_set_manifest_json,
                                  restore_completed_at, restored_database_count
                           FROM paired_backup_proofs
                           ORDER BY restore_completed_at DESC, id DESC LIMIT 1"""
                    ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise StateSafetyError("control state is unreadable") from exc
        if not versions or versions != list(range(1, versions[-1] + 1)):
            raise StateSafetyError("control schema is invalid")
        fingerprint = hashlib.sha256(
            json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return versions[-1], fingerprint, roots, proof

    @staticmethod
    def _validated_proof_manifest(
        proof: tuple[object, ...],
    ) -> tuple[str, dict[str, object]]:
        proof_id, digest, manifest_json = proof[0], proof[1], proof[2]
        if (
            type(proof_id) is not str
            or not proof_id
            or type(digest) is not str
            or type(manifest_json) is not str
        ):
            raise StateSafetyError("backup proof manifest is invalid")
        encoded = manifest_json.encode("utf-8")
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise StateSafetyError("backup proof manifest is invalid")
        # The store writes the canonical manifest bytes verbatim and digests
        # exactly those bytes, so this compares against the producer's own
        # encoding instead of reimplementing the canonical encoder here.
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise StateSafetyError("backup proof manifest is corrupt")
        try:
            manifest = json.loads(
                manifest_json, object_pairs_hook=_reject_duplicate_keys
            )
        except (ValueError, RecursionError) as exc:
            # A manifest nested ~100k deep parses to RecursionError, not
            # ValueError, well inside the size cap. `doctor` promises never to
            # raise, so this must become a refusal rather than a traceback.
            raise StateSafetyError("backup proof manifest is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise StateSafetyError("backup proof manifest is invalid")
        return proof_id, manifest

    @staticmethod
    def _require_coverage(
        manifest: dict[str, object],
        binding: ControlStateBinding,
    ) -> None:
        control_store = manifest.get("control_store")
        if not isinstance(control_store, dict):
            raise StateSafetyError("backup proof manifest is invalid")
        covered_identity = (
            control_store.get("schema_version"),
            control_store.get("schema_fingerprint_sha256"),
            control_store.get("identity_companion_sha256"),
            control_store.get("identity_companion_byte_length"),
        )
        if not (
            type(covered_identity[0]) is int
            and type(covered_identity[1]) is str
            and type(covered_identity[2]) is str
            and type(covered_identity[3]) is int
        ):
            raise StateSafetyError("backup proof manifest is invalid")

        raw_roots = manifest.get("asset_roots")
        if not isinstance(raw_roots, list):
            raise StateSafetyError("backup proof manifest is invalid")
        covered_roots: list[tuple[str, int]] = []
        for entry in raw_roots:
            if not isinstance(entry, dict):
                raise StateSafetyError("backup proof manifest is invalid")
            root_id = entry.get("root_id")
            revision = entry.get("revision")
            if type(root_id) is not str or type(revision) is not int:
                raise StateSafetyError("backup proof manifest is invalid")
            covered_roots.append((root_id, revision))

        live_identity = (
            binding.schema_version,
            binding.schema_fingerprint_sha256,
            binding.identity_companion_sha256,
            binding.identity_companion_byte_length,
        )
        if (
            covered_identity != live_identity
            or tuple(covered_roots) != binding.enabled_roots
        ):
            raise StateSafetyError(
                "backup proof does not cover the current control state"
            )

    def _require_current_restore(self, proof: tuple[object, ...]) -> None:
        restore_completed_at, restored_database_count = proof[3], proof[4]
        if type(restore_completed_at) is not str:
            raise StateSafetyError("backup proof timestamps are invalid")
        try:
            completed = datetime.fromisoformat(restore_completed_at)
        except ValueError as exc:
            raise StateSafetyError("backup proof timestamps are invalid") from exc
        now = self._clock()
        if completed.tzinfo is None or now.tzinfo is None:
            raise StateSafetyError("backup proof timestamps are invalid")
        if completed > now + _FUTURE_TOLERANCE:
            raise StateSafetyError("backup proof timestamps are invalid")
        if now - completed > self._max_restore_age:
            raise StateSafetyError("verified restore is stale")
        if type(restored_database_count) is not int or restored_database_count < 1:
            raise StateSafetyError("backup proof records no restored database")

    @staticmethod
    def _require_forward_migration(candidate: int, schema_version: int) -> None:
        if type(candidate) is not int or candidate < 1:
            raise StateSafetyError("candidate control schema is invalid")
        if schema_version > candidate:
            raise StateSafetyError("candidate cannot read the current control schema")


# ---------------------------------------------------------------------------
# Producing a proof (R0-C §2 exclusion 1's separate gate)
# ---------------------------------------------------------------------------
#
# The R0-C coordinator above consumes proofs and deliberately never creates
# one. That left the stateful-upgrade sequence -- `cortex stop`, record a paired
# backup proof through the generation's own `ControlStore`, `cortex-dist
# upgrade` -- with one step in the middle that no supported command performed,
# so the only way to satisfy the gate was to write the record by hand.
# Everything below is that command, and only that: it does the backup, verifies
# a restore of it, and hands the result to the generation's own `ControlStore`
# to record. It never authorizes anything; the gate above still decides.


class BackupProofError(RuntimeError):
    """A paired backup proof could not be produced."""


#: "Paired" is the whole name of the thing: two independent copies, both
#: restored, both read back. The store validates a primary and an independent
#: `BackupCopyProof` separately for the same reason.
_COPY_NAMES = ("primary", "independent")


#: R0-C §3.9's bound, restated where the producer can see it. A proof this
#: command records is fresh by construction, and the operator is told when it
#: stops being: `_DEFAULT_MAX_RESTORE_AGE` is the same value the coordinator
#: enforces, so the two cannot drift.
RESTORE_FRESHNESS_SECONDS = int(_DEFAULT_MAX_RESTORE_AGE.total_seconds())


@dataclass(frozen=True)
class RecordedBackupProof:
    proof_id: str
    #: The digest of the live `control.db` bytes that were copied. Named
    #: `source` rather than `backup` on purpose: the two are equal only because
    #: the copy is verified byte-for-byte, and that equality is the claim.
    source_digest: str
    backup_set_digest: str
    restore_completed_at: str
    backup_root: Path
    copied_snapshot_count: int
    restored_database_count: int
    verified_sample_count: int
    freshness_seconds: int = RESTORE_FRESHNESS_SECONDS


def record_backup_proof(
    *,
    database: Path,
    companion: Path,
    backup_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    actor_id: str = "operator",
    proof_id: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecordedBackupProof:
    """Back up the protected set, verify a restore of it, record the proof.

    The proof is recorded by the *generation's own* `ControlStore`, in a child
    process running the generation's own interpreter, because the proof has to
    be written by the same code that owns the schema it describes -- R0-C §7.4
    step 4, "not the checkout's". This module is stdlib-only and could not
    import `cortex_platform` even if it wanted to.

    Two passes over that child. The first reads the identity and the enabled
    asset roots; the parent then copies and measures exactly those; the second
    re-reads the identity at record time and hands the store a manifest built
    from both. Re-reading is not redundant: the store validates coverage against
    the rows as they are *now*, and a manifest built from a stale read would be
    refused by the very check that makes the proof mean something.
    """

    _require_state_ready(database, companion)
    # Before the first subprocess: an operator-supplied id that the store would
    # refuse must not cost a recorder run, let alone a copy.
    if proof_id is not None:
        _backup_set_root(backup_root, proof_id)
    inspection = _run_recorder(
        "inspect",
        {"database": str(database)},
        python_executable=python_executable,
        environment=environment,
    )
    started = now()
    source_digest = _digest_file(database)
    # `ControlStore` resource ids are lowercase, so the instant is too.
    stamp = started.strftime("%Y%m%dt%H%M%Sz")
    identifier = f"backup-proof-{stamp}" if proof_id is None else proof_id
    root = _backup_set_root(backup_root, identifier)
    # One backup set per proof, named by the proof. A collision here is a proof
    # id that already has a backup set, which the store would refuse a moment
    # later anyway -- refusing before anything is copied is the cheaper answer.
    if root.exists():
        raise BackupProofError("backup set already exists")
    roots = [
        (str(entry["root_id"]), Path(str(entry["private_path"])))
        for entry in inspection["asset_roots"]
    ]
    # Everything from the first copy to the recorded receipt writes a full copy
    # of control state to disk. A failure anywhere in between used to leave it
    # there, and no command prunes it. After the receipt the set is described by
    # a row, so it is kept even if the checkpoint below refuses.
    try:
        copies = [
            _write_backup_set(root / name, database, companion, roots)
            for name in _COPY_NAMES
        ]
        if len({copy["database_sha256"] for copy in copies}) != 1:
            raise BackupProofError("paired backup copies disagree")
        if copies[0]["database_sha256"] != source_digest:
            raise BackupProofError("backup copy does not match the source database")
        restored, samples = _verify_restore(root, _COPY_NAMES)
        completed = now()
        measurements = _measure_roots(
            root / _COPY_NAMES[0] / "assets",
            roots,
            frozenset(copies[0]["unreadable_roots"]),
        )
        receipt = _run_recorder(
            "record",
            {
                "database": str(database),
                "actor_id": actor_id,
                "proof_id": identifier,
                "database_sha256": source_digest,
                # R0-C F2: the two bytes fields describe the backup copy,
                # never the live file, which this very call is about to write to.
                "database_byte_length": (
                    root / "primary" / "control.db"
                ).stat().st_size,
                "snapshot_count": copies[0]["snapshot_count"],
                "primary_completed_at": _instant(started),
                "independent_completed_at": _instant(started),
                "restore_completed_at": _instant(completed),
                "restored_database_count": restored,
                "verified_sample_count": samples,
                "asset_roots": measurements,
            },
            python_executable=python_executable,
            environment=environment,
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    # Recording writes to the very database the proof describes, so the state is
    # only checkpointed again once the child's connection closed. Refusing here
    # is better than leaving an operator to discover it at `upgrade`.
    BackupBackedSafetyPort._require_checkpointed(database)
    return RecordedBackupProof(
        proof_id=str(receipt["proof_id"]),
        source_digest=source_digest,
        backup_set_digest=str(receipt["backup_set_digest"]),
        restore_completed_at=str(receipt["restore_completed_at"]),
        backup_root=root,
        copied_snapshot_count=int(copies[0]["snapshot_count"]),
        restored_database_count=restored,
        verified_sample_count=samples,
    )


#: The proof id class `ControlStore` applies to `proof_id`, written down a
#: second time because this module is stdlib-only and cannot import the store.
#: `tests/distribution/test_backup_proof.py` compares the two patterns.
_PROOF_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")

#: ⟦P5.6⟧ How many backup sets `record-proof` leaves behind by default: the
#: one it just wrote and its predecessor. Authorization reads only the newest
#: `paired_backup_proofs` row, so every older set is dead weight the moment a
#: new one lands -- and each set is three copies of control state plus every
#: enabled asset root (~12 GB on the mini). Five in one day filled the disk.
DEFAULT_KEEP_BACKUP_SETS = 2


@dataclass(frozen=True)
class BackupSetPrune:
    """What `prune_backup_sets` did, for the operator to read."""

    kept: tuple[Path, ...]
    pruned: tuple[Path, ...]
    #: Sets that could not be removed, with the error's type. A prune that
    #: fails does not un-record the proof, so it is reported rather than raised.
    failed: tuple[tuple[Path, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kept": [str(path) for path in self.kept],
            "pruned": [str(path) for path in self.pruned],
            "failed": [{"path": str(path), "error": error} for path, error in self.failed],
        }


def _is_backup_set(path: Path) -> bool:
    """The marker `record_backup_proof` leaves and nothing else under the root
    has: both copies as real directories and the primary's `control.db` a real
    file. Checked before anything is deleted, so a lowercase directory an
    operator parked beside the sets is neither removed nor a keep slot."""

    for name in _COPY_NAMES:
        copy = path / name
        if copy.is_symlink() or not copy.is_dir():
            return False
    database = path / _COPY_NAMES[0] / "control.db"
    return not database.is_symlink() and database.is_file()


def prune_backup_sets(
    backup_root: Path, *, keep: int, protect: Path | None = None
) -> BackupSetPrune:
    """Delete the backup sets under `backup_root` older than the newest `keep`.

    A candidate is a direct child directory of `backup_root` that carries the
    shape `record_backup_proof` writes: a name that is a proof id (operator
    `--proof-id` values are any lowercase id, so the name alone proves
    nothing) AND the backup-set marker -- every copy in `_COPY_NAMES` present
    as a non-symlink directory, with `primary/control.db` a non-symlink
    regular file. A directory without the marker is neither pruned nor
    counted toward `keep`; a regular file is never a candidate; a symlink is
    never followed, whether it is the child itself (skipped) or anything
    below it (`shutil.rmtree` unlinks a link rather than following it). The
    set named by `protect` -- the one this run just wrote -- is the newest by
    definition, counts as the first of the `keep`, and is never pruned
    whatever its mtime says (a restored copy, a clock that went backwards).
    After it, newest is decided by the set root's mtime, then by name --
    never by name alone, because operator-named ids carry no stamp.
    """

    if type(keep) is not int or keep < 1:
        raise BackupProofError("keep must be a positive integer")
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise BackupProofError("backup root is not a directory")
    protected = None if protect is None else Path(protect)
    candidates: list[Path] = []
    for child in backup_root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if _PROOF_ID_RE.fullmatch(child.name) is None:
            continue
        if not _is_backup_set(child):
            continue
        candidates.append(child)
    ordered = sorted(
        candidates,
        key=lambda path: (path == protected, path.stat().st_mtime, path.name),
        reverse=True,
    )
    kept: list[Path] = []
    pruned: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for index, child in enumerate(ordered):
        if index < keep or child == protected:
            kept.append(child)
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            failed.append((child, type(exc).__name__))
            continue
        pruned.append(child)
    return BackupSetPrune(tuple(kept), tuple(pruned), tuple(failed))


def _backup_set_root(backup_root: Path, identifier: str) -> Path:
    """Where this proof's backup set goes -- a direct child, or nothing.

    `--proof-id` is operator-supplied and arrives here before anything is
    copied. A value carrying `/` or `..` would otherwise place four copies of
    the control database, the identity companion and every enabled asset root
    outside `backup_root`, and only then be refused by the store.
    """

    if _PROOF_ID_RE.fullmatch(identifier) is None:
        raise BackupProofError("backup proof id is invalid")
    root = backup_root / identifier
    if root.parent != backup_root or root.name != identifier:
        raise BackupProofError("backup proof id is invalid")
    return root


def _require_state_ready(database: Path, companion: Path) -> None:
    try:
        BackupBackedSafetyPort._require_safe_state_file(database)
        BackupBackedSafetyPort._require_safe_state_file(companion)
        # The whole backup rests on this: a non-empty write-ahead log means the
        # database file is not the whole state, so copying it alone would
        # produce a proof about bytes that are not the state.
        BackupBackedSafetyPort._require_checkpointed(database)
    except StateSafetyError as exc:
        raise BackupProofError(str(exc)) from None


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


#: What an asset root's manifest says when its directory did not exist at proof
#: time. An absent directory is a LEGAL registration -- `register_asset_root`
#: performs no existence check -- so refusing it would make `record-proof`, and
#: therefore the whole R0-C gate, unreachable on an installation that has done
#: nothing wrong. But an absent root and an EMPTY root must not produce the same
#: manifest value, or the proof silently claims to have looked where it could
#: not. The digest is over this marker instead of over no entries at all.
_UNREADABLE_ROOT_MARKER = b"cortex.backup-proof/1 asset-root-unreadable"


def _measure_root(directory: Path) -> dict[str, object]:
    """A content manifest over one directory of the BACKUP SET.

    Measured from the copy, never from the live root: the manifest is then a
    claim about the bytes this proof holds by construction, rather than a claim
    about a directory the research agent may have written to since.
    """

    entries: list[str] = []
    total = 0
    if directory.is_dir() and not directory.is_symlink():
        for item in sorted(directory.rglob("*")):
            if item.is_symlink() or not item.is_file():
                continue
            size = item.stat().st_size
            total += size
            relative = item.relative_to(directory).as_posix()
            entries.append(f"{_digest_file(item)} {size} {relative}")
    payload = "\n".join(entries).encode("utf-8")
    return {
        "content_manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "file_count": len(entries),
        "byte_length": total,
    }


def _unreadable_root() -> dict[str, object]:
    return {
        "content_manifest_sha256": hashlib.sha256(
            _UNREADABLE_ROOT_MARKER
        ).hexdigest(),
        "file_count": 0,
        "byte_length": 0,
    }


def _measure_roots(
    assets: Path,
    roots: list[tuple[str, Path]],
    unreadable: frozenset[str],
) -> dict[str, dict[str, object]]:
    return {
        root_id: (
            _unreadable_root()
            if root_id in unreadable
            else _measure_root(assets / root_id)
        )
        for root_id, _private_path in roots
    }


def _write_backup_set(
    destination: Path,
    database: Path,
    companion: Path,
    roots: list[tuple[str, Path]],
) -> dict[str, object]:
    destination.mkdir(parents=True, mode=0o700)
    snapshots = 0
    for source, name in ((database, "control.db"), (companion, "identity-companion")):
        target = destination / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        snapshots += 1
    unreadable: list[str] = []
    for root_id, private_path in roots:
        target = destination / "assets" / root_id
        target.mkdir(parents=True, mode=0o700)
        if os.path.lexists(private_path) and (
            private_path.is_symlink() or not private_path.is_dir()
        ):
            # A symlink or a plain file is a state the store cannot have
            # produced. Skipping it silently wrote a green proof recording
            # `file_count: 0` for a root whose bytes this set never held, and
            # coverage compares only `(root_id, revision)`, so the upgrade gate
            # would have authorized it.
            raise BackupProofError(
                f"asset root {root_id} is not a directory this proof can back up"
            )
        if not private_path.is_dir():
            # Absent: legal, recorded as unreadable, and not counted as copied.
            unreadable.append(root_id)
            continue
        snapshots += 1
        for item in sorted(private_path.rglob("*")):
            if item.is_symlink() or not item.is_file():
                continue
            copy = target / item.relative_to(private_path)
            copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            copy.write_bytes(item.read_bytes())
            copy.chmod(0o600)
    return {
        "database_sha256": _digest_file(destination / "control.db"),
        "snapshot_count": snapshots,
        "unreadable_roots": tuple(unreadable),
    }


def _verify_restore(root: Path, names: tuple[str, ...]) -> tuple[int, int]:
    """Restore both copies and read them back, `immutable=1`, as the gate does.

    A backup nobody restored is a hope. Each copy is restored to its own
    directory and opened with the exact URI `_read_control_state` uses, which
    creates no companion files and therefore cannot leave the restored copy in a
    state its own digest no longer describes.
    """

    restored = 0
    samples = 0
    for index, name in enumerate(names):
        source = root / name / "control.db"
        target = root / "restore" / str(index) / "control.db"
        target.parent.mkdir(parents=True, mode=0o700)
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        if _digest_file(target) != _digest_file(source):
            raise BackupProofError("restored database does not match its backup copy")
        uri = f"file:{quote(str(target.resolve(strict=True)), safe='/')}?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                connection.execute("PRAGMA query_only = ON")
                if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                    raise BackupProofError("restored database failed integrity check")
                tables = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
                for table in tables:
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                samples += len(tables)
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BackupProofError("restored database is unreadable") from exc
        restored += 1
    if restored < 1 or samples < 1:
        raise BackupProofError("restore verified nothing")
    return restored, samples


def _run_recorder(
    mode: str,
    request: dict[str, object],
    *,
    python_executable: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    payload = json.dumps(request, sort_keys=True)
    completed = subprocess.run(
        [str(python_executable), "-I", "-c", _RECORDER_SOURCE, mode],
        input=payload,
        capture_output=True,
        text=True,
        # `PYTHONDONTWRITEBYTECODE` is already in `control_environment`, and it
        # matters here: this interpreter imports the installed wheel out of a
        # sealed generation, and a `__pycache__` written into one is a byte the
        # generation's own ledger never covered.
        env=dict(environment) | {"PYTHONDONTWRITEBYTECODE": "1"},
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise BackupProofError(
            f"control store {mode} failed: {detail[-1] if detail else 'no output'}"
        )
    raw = completed.stdout
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError) as exc:
        raise BackupProofError(f"control store {mode} produced invalid output") from exc
    if not isinstance(document, dict):
        raise BackupProofError(f"control store {mode} produced invalid output")
    return document


#: Runs under the INSTALLED generation's interpreter, never this one. Kept as a
#: source string rather than a module because it is the only code in
#: `distribution/` that imports `cortex_platform`, and a real import here would
#: be unresolvable in every context this file actually runs in: a checkout
#: verifying a bundle, and `tools/distribution/` inside a bundle.
_RECORDER_SOURCE = '''
import json, sys
from datetime import datetime
from pathlib import Path

from cortex_platform.product import control

mode = sys.argv[1]
request = json.loads(sys.stdin.read())
store = control.ControlStore(Path(request["database"]))
store.initialize()
identity = store.current_control_store_identity()
roots = [root for root in store.list_asset_roots() if root.enabled]
roots.sort(key=lambda root: root.root_id)

if mode == "inspect":
    print(json.dumps({
        "schema_version": identity.schema_version,
        "asset_roots": [
            {
                "root_id": root.root_id,
                "revision": root.revision,
                "private_path": str(root.private_path),
            }
            for root in roots
        ],
    }))
    raise SystemExit(0)

measured = request["asset_roots"]
manifest = control.ProtectedSetManifest(
    schema_version=1,
    control_store=control.ControlStoreSnapshot(
        schema_version=identity.schema_version,
        schema_fingerprint_sha256=identity.schema_fingerprint_sha256,
        database_sha256=request["database_sha256"],
        database_byte_length=request["database_byte_length"],
        identity_companion_sha256=identity.identity_companion_sha256,
        identity_companion_byte_length=identity.identity_companion_byte_length,
    ),
    logical_snapshots=(),
    asset_roots=tuple(
        control.ProtectedAssetRootSnapshot(
            root_id=root.root_id,
            revision=root.revision,
            content_manifest_sha256=measured[root.root_id]["content_manifest_sha256"],
            file_count=measured[root.root_id]["file_count"],
            byte_length=measured[root.root_id]["byte_length"],
        )
        for root in roots
    ),
)
digest = control.protected_set_digest(manifest)
record = store.record_paired_backup_proof(
    proof=control.PairedBackupProofInput(
        proof_id=request["proof_id"],
        protected_set_manifest=manifest,
        primary=control.BackupCopyProof(
            digest,
            datetime.fromisoformat(request["primary_completed_at"]),
            request["snapshot_count"],
            True,
        ),
        independent=control.BackupCopyProof(
            digest,
            datetime.fromisoformat(request["independent_completed_at"]),
            request["snapshot_count"],
            True,
        ),
        restore=control.RestoreVerification(
            digest,
            datetime.fromisoformat(request["restore_completed_at"]),
            request["restored_database_count"],
            request["verified_sample_count"],
            True,
        ),
    ),
    actor_id=request["actor_id"],
    idempotency_key=("record-proof-" + request["proof_id"])[:64].ljust(16, "x"),
)
print(json.dumps({
    "proof_id": record.id,
    "backup_set_digest": record.backup_set_digest,
    "restore_completed_at": record.restore_completed_at.isoformat(),
}))
'''
