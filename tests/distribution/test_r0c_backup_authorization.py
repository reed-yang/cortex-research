"""R0-C: the backup-backed coordinator, pinned against the real ControlStore.

Every test here builds state with the production writer. The installer's own
derivation is stdlib-only and independent by necessity -- it runs against a
foreign generation's database and cannot import `cortex_platform` -- so the
equality between the two is a contract that only a test can hold. Fabricating
the state with raw `sqlite3`, as the older distribution tests do, would test
the coordinator against a fixture of its own assumptions.
"""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product import control as control_module
from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.schema import SCHEMA_VERSION
from distribution.state_safety import (
    BackupBackedSafetyPort,
    StateSafetyError,
    UpgradeRequest,
)

# Derived, never a literal: these tests assert the RELATIONSHIP between the
# state's schema and a candidate's (older refuses, newer migrates forward), so
# pinning an absolute number here only makes a schema bump fail 22 tests
# opaquely instead of the one that explains the coupling.
CONTROL_SCHEMA = SCHEMA_VERSION


class _Installation:
    """A real, initialized Control state and the request that targets it."""

    def __init__(self, tmp_path: Path) -> None:
        # The store refuses a group- or world-readable parent, and the real
        # runtime creates this directory 0o700 for the same reason.
        self.data = tmp_path / "data"
        self.data.mkdir(mode=0o700)
        self.runtime = tmp_path / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.database = self.data / "control.db"
        self.companion = self.data / ".control.db.transport.key"
        self.store = ControlStore(self.database)
        self.store.initialize()
        self.roots: list[object] = []

    def register_root(self, root_id: str) -> object:
        root = self.store.register_asset_root(
            root_id=root_id,
            private_path=self.data / "assets" / root_id,
            max_bytes=10_000,
            enabled=True,
            actor_id="operator",
            idempotency_key=f"register-{root_id}".ljust(16, "x"),
        )
        self.roots.append(root)
        return root

    def manifest(self) -> object:
        identity = self.store.current_control_store_identity()
        return control_module.ProtectedSetManifest(
            schema_version=1,
            control_store=control_module.ControlStoreSnapshot(
                schema_version=identity.schema_version,
                schema_fingerprint_sha256=identity.schema_fingerprint_sha256,
                # The backup copy's own bytes. Deliberately not the live file:
                # recording this proof mutates the database it describes.
                database_sha256="b" * 64,
                database_byte_length=4_096,
                identity_companion_sha256=identity.identity_companion_sha256,
                identity_companion_byte_length=(
                    identity.identity_companion_byte_length
                ),
            ),
            logical_snapshots=(),
            asset_roots=tuple(
                control_module.ProtectedAssetRootSnapshot(
                    root_id=root.root_id,
                    revision=root.revision,
                    content_manifest_sha256="c" * 64,
                    file_count=3,
                    byte_length=1_024,
                )
                for root in self.roots
            ),
        )

    def record_proof(
        self,
        *,
        proof_id: str = "backup-proof-current",
        restore_age: timedelta = timedelta(hours=1),
        restored_database_count: int = 2,
    ) -> str:
        manifest = self.manifest()
        digest = control_module.protected_set_digest(manifest)
        now = datetime.now(UTC)
        restore_at = now - restore_age
        proof = control_module.PairedBackupProofInput(
            proof_id=proof_id,
            protected_set_manifest=manifest,
            primary=control_module.BackupCopyProof(
                digest, restore_at - timedelta(hours=2), 7, True
            ),
            independent=control_module.BackupCopyProof(
                digest, restore_at - timedelta(hours=1), 7, True
            ),
            restore=control_module.RestoreVerification(
                digest, restore_at, restored_database_count, 22, True
            ),
        )
        self.store.record_paired_backup_proof(
            proof=proof,
            actor_id="backup-adapter",
            idempotency_key=f"proof-{proof_id}".ljust(16, "x"),
        )
        return digest

    def request(self, *, candidate_control_schema: int = CONTROL_SCHEMA) -> UpgradeRequest:
        return UpgradeRequest(
            current_bundle_digest="1" * 64,
            candidate_bundle_digest="2" * 64,
            current_version="cortex-dev-1-1111111111111111",
            candidate_version="cortex-dev-2-2222222222222222",
            candidate_control_schema=candidate_control_schema,
            control_database=self.database,
            identity_companion=self.companion,
            runtime_root=self.runtime,
        )

    def state_files(self) -> set[str]:
        return {entry.name for entry in self.data.iterdir()}


@pytest.fixture
def installation(tmp_path: Path) -> _Installation:
    value = _Installation(tmp_path)
    value.register_root("artifacts")
    return value


def test_derived_binding_reproduces_the_real_control_store_identity(
    installation: _Installation,
) -> None:
    """The whole design rests on this equality; nothing else pins it.

    If `ControlStore._control_store_identity` ever changes its projection,
    this must fail rather than the coordinator silently refusing every real
    installation.
    """

    installation.record_proof()
    binding = BackupBackedSafetyPort().inspect(installation.request())
    identity = installation.store.current_control_store_identity()
    assert binding is not None
    assert (
        binding.schema_version,
        binding.schema_fingerprint_sha256,
        binding.identity_companion_sha256,
        binding.identity_companion_byte_length,
    ) == (
        identity.schema_version,
        identity.schema_fingerprint_sha256,
        identity.identity_companion_sha256,
        identity.identity_companion_byte_length,
    )
    assert binding.enabled_roots == (("artifacts", installation.roots[0].revision),)
    assert binding.proof_id == "backup-proof-current"


def test_a_real_paired_proof_authorizes_a_stateful_upgrade(
    installation: _Installation,
) -> None:
    installation.record_proof()
    port = BackupBackedSafetyPort()
    request = installation.request()
    authorization = port.authorize(request)
    port.consume(authorization, request)


def test_authorization_is_one_use(installation: _Installation) -> None:
    installation.record_proof()
    port = BackupBackedSafetyPort()
    request = installation.request()
    authorization = port.authorize(request)
    port.consume(authorization, request)
    with pytest.raises(StateSafetyError, match="not current"):
        port.consume(authorization, request)


def test_state_changing_between_authorize_and_consume_is_refused(
    installation: _Installation,
) -> None:
    """A different-but-still-valid binding at commit time is not the one issued."""

    installation.record_proof(proof_id="backup-proof-first")
    port = BackupBackedSafetyPort()
    request = installation.request()
    authorization = port.authorize(request)
    installation.record_proof(
        proof_id="backup-proof-second", restore_age=timedelta(minutes=1)
    )
    with pytest.raises(StateSafetyError, match="changed during authorization"):
        port.consume(authorization, request)


def test_state_becoming_unauthorizable_between_the_two_phases_reports_why(
    installation: _Installation,
) -> None:
    """When the re-check fails outright, the specific reason wins, not "changed"."""

    installation.record_proof()
    port = BackupBackedSafetyPort()
    request = installation.request()
    authorization = port.authorize(request)
    installation.register_root("second")
    with pytest.raises(StateSafetyError, match="does not cover the current"):
        port.consume(authorization, request)


def test_inspection_and_authorization_do_not_touch_the_data_directory(
    installation: _Installation,
) -> None:
    """A plain `mode=ro` open would leave `-wal` and `-shm` behind."""

    installation.record_proof()
    before = installation.state_files()
    port = BackupBackedSafetyPort()
    request = installation.request()
    port.consume(port.authorize(request), request)
    assert installation.state_files() == before
    assert not (installation.data / "control.db-wal").exists()
    assert not (installation.data / "control.db-shm").exists()


def test_state_without_any_proof_is_refused_with_the_documented_message(
    installation: _Installation,
) -> None:
    with pytest.raises(
        StateSafetyError, match="verified backup authorization is required"
    ):
        BackupBackedSafetyPort().authorize(installation.request())


def test_a_root_enabled_after_the_proof_breaks_coverage(
    installation: _Installation,
) -> None:
    installation.record_proof()
    installation.register_root("second")
    with pytest.raises(StateSafetyError, match="does not cover the current"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_a_replaced_identity_companion_breaks_coverage(
    installation: _Installation,
) -> None:
    installation.record_proof()
    installation.companion.write_bytes(b"z" * 32)
    installation.companion.chmod(0o600)
    with pytest.raises(StateSafetyError, match="does not cover the current"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_a_stale_restore_is_refused(installation: _Installation) -> None:
    installation.record_proof(restore_age=timedelta(hours=1))
    port = BackupBackedSafetyPort(
        clock=lambda: datetime.now(UTC) + timedelta(days=8)
    )
    with pytest.raises(StateSafetyError, match="verified restore is stale"):
        port.authorize(installation.request())


def test_the_age_bound_is_the_configured_one(installation: _Installation) -> None:
    installation.record_proof(restore_age=timedelta(hours=1))
    port = BackupBackedSafetyPort(max_restore_age=timedelta(minutes=30))
    with pytest.raises(StateSafetyError, match="verified restore is stale"):
        port.authorize(installation.request())


def test_a_candidate_older_than_the_state_cannot_migrate_forward(
    installation: _Installation,
) -> None:
    installation.record_proof()
    with pytest.raises(StateSafetyError, match="cannot read the current control schema"):
        BackupBackedSafetyPort().authorize(
            installation.request(candidate_control_schema=CONTROL_SCHEMA - 1)
        )


def test_a_newer_candidate_migrates_forward(installation: _Installation) -> None:
    installation.record_proof()
    request = installation.request(candidate_control_schema=CONTROL_SCHEMA + 1)
    port = BackupBackedSafetyPort()
    port.consume(port.authorize(request), request)


def test_a_same_schema_candidate_is_a_code_upgrade_and_is_admitted(
    installation: _Installation,
) -> None:
    """A code upgrade that migrates nothing is a supported transition.

    Named rather than left implicit in the default request: the gate's rule is
    "the candidate must be able to READ this state", so equality is admitted and
    only a candidate BELOW the state is refused. A cutover between two builds of
    the same Control schema depends on that, and it must not be mistaken for a
    migration or made to require one.
    """

    installation.record_proof()
    request = installation.request(candidate_control_schema=CONTROL_SCHEMA)
    port = BackupBackedSafetyPort()

    binding = port.authorize(request)
    port.consume(binding, request)


def test_an_uncheckpointed_write_ahead_log_is_refused(
    installation: _Installation,
) -> None:
    installation.record_proof()
    (installation.data / "control.db-wal").write_bytes(b"\x00" * 32)
    with pytest.raises(StateSafetyError, match="not checkpointed"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_an_empty_write_ahead_log_is_accepted(installation: _Installation) -> None:
    """A read-only reader legitimately leaves a zero-length `-wal` behind."""

    installation.record_proof()
    (installation.data / "control.db-wal").write_bytes(b"")
    BackupBackedSafetyPort().authorize(installation.request())


def test_a_running_lifecycle_is_refused(installation: _Installation) -> None:
    installation.record_proof()
    (installation.runtime / "lifecycle.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StateSafetyError, match="requires the lifecycle to be stopped"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_a_manifest_that_does_not_match_its_digest_is_refused(
    installation: _Installation,
) -> None:
    """The stored manifest is the digested bytes, so one altered byte shows."""

    installation.record_proof()
    connection = sqlite3.connect(installation.database)
    try:
        row = connection.execute(
            """SELECT backup_set_digest, protected_set_manifest_json,
                      primary_completed_at, primary_snapshot_count,
                      independent_completed_at, independent_snapshot_count,
                      restore_completed_at, restored_database_count,
                      verified_sample_count, created_at
               FROM paired_backup_proofs"""
        ).fetchone()
        # Still valid JSON, still covers the live identity -- only the bytes
        # the digest was taken over differ.
        tampered = row[1].replace('"' + "b" * 64 + '"', '"' + "a" * 64 + '"', 1)
        assert tampered != row[1]
        connection.execute(
            """INSERT INTO paired_backup_proofs
               (id, backup_set_digest, protected_set_manifest_json,
                primary_completed_at, primary_snapshot_count,
                independent_completed_at, independent_snapshot_count,
                restore_completed_at, restored_database_count,
                verified_sample_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("backup-proof-tampered", row[0], tampered, *row[2:]),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StateSafetyError, match="manifest is corrupt"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_a_deeply_nested_manifest_is_refused_rather_than_raising(
    installation: _Installation,
) -> None:
    """`json.loads` answers deep nesting with RecursionError, not ValueError.

    The table's own `json_valid` CHECK refuses to store this, so it is not
    reachable through the store's write path -- but a control database is a
    file, and the gate reads whatever bytes are in it. `doctor` promises never
    to raise, so this must come back as a refusal either way. The row is
    inserted with the CHECK suppressed, which is exactly the state a database
    written by something other than the store can be in.
    """

    import hashlib

    installation.record_proof()
    nested = "[" * 100_000 + "]" * 100_000
    assert len(nested.encode("utf-8")) < 1 << 20
    digest = hashlib.sha256(nested.encode("utf-8")).hexdigest()
    connection = sqlite3.connect(installation.database)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        row = connection.execute(
            """SELECT primary_completed_at, primary_snapshot_count,
                      independent_completed_at, independent_snapshot_count,
                      restore_completed_at, restored_database_count,
                      verified_sample_count, created_at
               FROM paired_backup_proofs"""
        ).fetchone()
        connection.execute(
            """INSERT INTO paired_backup_proofs
               (id, backup_set_digest, protected_set_manifest_json,
                primary_completed_at, primary_snapshot_count,
                independent_completed_at, independent_snapshot_count,
                restore_completed_at, restored_database_count,
                verified_sample_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("backup-proof-nested", digest, nested, *row),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StateSafetyError, match="manifest is invalid"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_the_identity_companion_is_exactly_the_length_the_store_writes(
    installation: _Installation,
) -> None:
    """Pins the coupling in both directions, so a store change fails loudly."""

    assert installation.companion.stat().st_size == 32
    installation.record_proof()
    installation.companion.write_bytes(b"z" * 33)
    installation.companion.chmod(0o600)
    with pytest.raises(StateSafetyError, match="control state is unsafe"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_an_unsafe_companion_mode_is_refused(installation: _Installation) -> None:
    installation.record_proof()
    installation.companion.chmod(0o644)
    with pytest.raises(StateSafetyError, match="control state is unsafe"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_half_present_state_is_incomplete(installation: _Installation) -> None:
    installation.record_proof()
    installation.database.unlink()
    with pytest.raises(StateSafetyError, match="control state is incomplete"):
        BackupBackedSafetyPort().authorize(installation.request())


def test_reading_the_rollback_schema_leaves_no_sidecars_behind(
    installation: _Installation,
) -> None:
    """`rollback` reads this database; it must not write two files doing so."""

    from distribution.install import DistributionInstaller

    before = installation.state_files()
    schema = DistributionInstaller._control_state_schema(installation.database)
    assert schema == CONTROL_SCHEMA
    assert installation.state_files() == before


def test_a_fresh_installation_still_authorizes(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    request = UpgradeRequest(
        current_bundle_digest="1" * 64,
        candidate_bundle_digest="2" * 64,
        current_version="cortex-dev-1-1111111111111111",
        candidate_version="cortex-dev-2-2222222222222222",
        candidate_control_schema=CONTROL_SCHEMA,
        control_database=tmp_path / "data" / "control.db",
        identity_companion=tmp_path / "data" / ".control.db.transport.key",
        runtime_root=runtime,
    )
    port = BackupBackedSafetyPort()
    assert port.inspect(request) is None
    port.consume(port.authorize(request), request)


def test_the_newest_restore_is_the_one_that_decides(
    installation: _Installation,
) -> None:
    """`list_paired_backup_proofs` orders by restore time; so must the port."""

    installation.record_proof(proof_id="backup-proof-old", restore_age=timedelta(days=6))
    installation.record_proof(proof_id="backup-proof-new", restore_age=timedelta(hours=1))
    binding = BackupBackedSafetyPort().inspect(installation.request())
    assert binding is not None
    assert binding.proof_id == "backup-proof-new"


def test_the_database_file_mode_is_the_one_the_store_created(
    installation: _Installation,
) -> None:
    """Pins the 0o600 assumption the coordinator's safety check relies on."""

    for path in (installation.database, installation.companion):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600


def test_a_proof_taken_before_a_migration_stays_readable(
    installation: _Installation,
) -> None:
    """A proof records the schema it was taken OF, not the build reading it.

    This is the defect the first real forward migration exposed. The version
    was originally a literal, so no proof could be recorded after a migration;
    deriving it from the live version instead made every PRE-migration proof
    unreadable, which strands an instance that has already migrated -- it
    cannot read its own history to authorize anything. Neither direction is
    right: coverage is the safety port's question, not the encoder's.
    """

    manifest = installation.manifest()
    older = control_module.ProtectedSetManifest(
        schema_version=1,
        control_store=control_module.ControlStoreSnapshot(
            schema_version=manifest.control_store.schema_version - 1,
            schema_fingerprint_sha256=(
                manifest.control_store.schema_fingerprint_sha256
            ),
            database_sha256=manifest.control_store.database_sha256,
            database_byte_length=manifest.control_store.database_byte_length,
            identity_companion_sha256=(
                manifest.control_store.identity_companion_sha256
            ),
            identity_companion_byte_length=(
                manifest.control_store.identity_companion_byte_length
            ),
        ),
        logical_snapshots=manifest.logical_snapshots,
        asset_roots=manifest.asset_roots,
    )
    encoded = control_module.canonical_protected_set_manifest(older)
    assert b'"schema_version":' in encoded
    # And it digests to something different from the current-schema manifest,
    # so the two can never be confused for one another.
    assert control_module.protected_set_digest(
        older
    ) != control_module.protected_set_digest(manifest)


def test_a_proof_from_a_different_schema_does_not_cover_this_state(
    installation: _Installation,
) -> None:
    """Readable is not the same as authorizing. The port still refuses."""

    installation.record_proof()
    port = BackupBackedSafetyPort()
    request = installation.request()
    port.authorize(request)  # the current-schema proof is fine

    # Change the store's shape so the recorded fingerprint no longer describes
    # it, exactly as a migration would.
    installation.register_root("second-root")
    with pytest.raises(StateSafetyError):
        port.authorize(request)
