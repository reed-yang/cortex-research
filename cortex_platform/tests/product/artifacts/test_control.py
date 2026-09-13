from __future__ import annotations

import hashlib
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cortex_platform.product.control.schema import MIGRATION_VERSIONS
from cortex_platform.product.artifacts import AssetRoot, FilesystemMaterializer
from cortex_platform.product.artifacts.service import ArtifactMaterializationService
from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    RevisionConflict,
)
from cortex_platform.product.control.schema import (
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    _MIGRATION_4,
    _MIGRATION_5,
)

from ..sources.fakes import (
    MutableClock,
    make_named_run,
    make_store,
    make_run,
    register_echo,
)


CONTENT_V1 = b"# Echo / TTT\n"
DIGEST_V1 = hashlib.sha256(CONTENT_V1).hexdigest()
CONTENT_V2 = b"# Echo / TTT\n\nSecond version.\n"
DIGEST_V2 = hashlib.sha256(CONTENT_V2).hexdigest()


class SimulatedCrash(BaseException):
    pass


class ColonArtifactIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, kind: str) -> str:
        count = self.counts.get(kind, 0) + 1
        self.counts[kind] = count
        separator = ":" if kind in {"artifact", "artifact_version"} else "-"
        return f"{kind}{separator}{count}"


class InterruptingMaterializer(FilesystemMaterializer):
    def __init__(self, roots: list[AssetRoot], interrupt_at: str) -> None:
        super().__init__(roots)
        self.interrupt_at = interrupt_at
        self.interrupted = False

    def _crash_checkpoint(self, point: str) -> None:
        if point == self.interrupt_at and not self.interrupted:
            self.interrupted = True
            raise SimulatedCrash(point)


def _bind_source(store, run: dict) -> dict:
    source = register_echo(store)
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title="Echo-Infinity",
        locator=None,
        candidates=(
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2606.04527",
                "official_title": "Echo-Infinity",
            },
        ),
        actor_id="artifact-fixture",
        idempotency_key="artifact-source-intent-0001",
    ).value
    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="use_source",
        expected_revision=intent["revision"],
        actor_id="artifact-fixture",
        idempotency_key="artifact-source-resolve-001",
    )
    return source


def _context(tmp_path: Path, *, clock: MutableClock | None = None):
    store = make_store(tmp_path, clock=clock)
    run = make_run(store)
    source = _bind_source(store, run)
    thread = store.get_thread(run["thread_id"])
    artifact = store.create_artifact(
        workspace_id=thread["workspace_id"],
        thread_id=thread["id"],
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        kind="living-brief",
        title="Echo / Helios Living Brief",
        actor_id="artifact-fixture",
        idempotency_key="artifact-create-0000001",
    ).value
    return store, run, source, artifact


def _start_run(store, run: dict) -> dict:
    attempt_id = run["attempt"]["id"]
    run = {**store.get_run(run["id"]), "attempt": run["attempt"]}
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        dispatch_owner="artifact-runtime-worker",
        runtime_release_id="artifact-runtime-release",
        state_generation_id="artifact-state-generation",
        runtime_slot_id="artifact-runtime-slot",
        runtime_artifact_digest="artifact-runtime-digest",
        runtime_worker_protocol="artifact-runtime-protocol",
        expected_revision=run["revision"],
        actor_id="artifact-runtime",
        idempotency_key="artifact-runtime-reserve-0001",
    ).value
    binding = store.create_runtime_binding(
        thread_id=run["thread_id"],
        adapter_id="hermes",
        runtime_session_ref="artifact-private-session",
        generation=1,
        adapter_version="test",
        actor_id="artifact-runtime",
        idempotency_key="artifact-runtime-binding-0001",
    ).value
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        runtime_binding_id=binding["id"],
        runtime_release_id="artifact-runtime-release",
        state_generation_id="artifact-state-generation",
        dispatch_owner="artifact-runtime-worker",
        expected_revision=run["revision"],
        actor_id="artifact-runtime",
        idempotency_key="artifact-runtime-pin-000001",
    ).value
    identity = {
        "attempt_id": run["attempt"]["id"],
        "runtime_binding_id": binding["id"],
        "runtime_release_id": "artifact-runtime-release",
        "state_generation_id": "artifact-state-generation",
    }
    for state in ("starting", "running"):
        run = store.apply_runtime_transition(
            run_id=run["id"],
            target_state=state,
            expected_revision=run["revision"],
            actor_id="artifact-runtime",
            idempotency_key=f"artifact-runtime-{state}-0001",
            **identity,
        ).value
    return {**run, "attempt": {"id": attempt_id}, "runtime_identity": identity}


def _request_version(
    store,
    run: dict,
    source: dict,
    artifact: dict,
    *,
    logical_version: int = 1,
    content: bytes = CONTENT_V1,
    parents: tuple[dict, ...] = (),
    advance_head: bool = True,
    expected_head_revision: int | None = 0,
    key: str = "artifact-version-request-01",
):
    return store.request_artifact_version(
        artifact_id=artifact["id"],
        logical_version=logical_version,
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        source_ids=(source["id"],),
        research_engine_refs=(source["engine_ref"],),
        generator={"name": "cortex-research", "version": "0.1.0"},
        tool={"name": "artifact-writer", "version": "1.0.0"},
        parents=parents,
        root_id="artifacts",
        relative_path=f"versions/{artifact['id']}-v{logical_version}.md",
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="text/markdown",
        advance_head=advance_head,
        expected_head_revision=expected_head_revision,
        actor_id="artifact-fixture",
        idempotency_key=key,
    )


def _complete(store, reservation: dict, *, result: dict | None = None):
    action = reservation["materialization_action"]
    claim = store.claim_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker",
        lease_seconds=30,
        actor_id="artifact-dispatcher",
        idempotency_key=f"claim-{action['id']}-0001",
    ).value
    materialized = result or {
        "schema_version": 1,
        "operation_id": action["operation_id"],
        "root_id": action["root_id"],
        "relative_path": action["relative_path"],
        "sha256": reservation["sha256"],
        "byte_length": reservation["byte_length"],
        "media_type": reservation["media_type"],
        "parents": reservation["parents"],
        "replayed": False,
        "recovered_from": None,
    }
    return store.complete_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker",
        claim_epoch=claim["claim_epoch"],
        materialized_result=materialized,
        actor_id="artifact-dispatcher",
        idempotency_key=f"complete-{action['id']}-01",
    )


def test_v5_database_migrates_to_artifact_schema_without_mutating_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        for script in (
            _MIGRATION_1,
            _MIGRATION_2,
            _MIGRATION_3,
            _MIGRATION_4,
            _MIGRATION_5,
        ):
            conn.executescript(script)
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
            [(1,), (2,), (3,), (4,), (5,)],
        )
        conn.execute(
            """INSERT INTO workspaces
               (id, title, revision, created_at, updated_at)
               VALUES ('ws-old', 'Preserved', 7, 'old', 'old')"""
        )
    database.chmod(0o600)
    key = database.with_name(f".{database.name}.transport.key")
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    from cortex_platform.product.control import ControlStore

    store = ControlStore(database)
    store.initialize()

    assert store.get_workspace("ws-old")["revision"] == 7
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "artifacts",
        "artifact_versions",
        "artifact_version_parents",
        "artifact_version_sources",
        "artifact_version_engine_refs",
        "artifact_materialization_actions",
        "artifact_snapshots",
        "artifact_snapshot_members",
    } <= tables


def test_interrupted_v6_migration_rolls_back_and_retries_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex_platform.product.control import schema

    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        for script in (
            _MIGRATION_1,
            _MIGRATION_2,
            _MIGRATION_3,
            _MIGRATION_4,
            _MIGRATION_5,
        ):
            conn.executescript(script)
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
            [(1,), (2,), (3,), (4,), (5,)],
        )

    original = schema._execute_script_in_transaction

    def interrupt(conn: sqlite3.Connection, script: str) -> None:
        if script == schema._MIGRATION_6:
            conn.execute("CREATE TABLE interrupted_artifact_migration(id TEXT)")
            raise RuntimeError("simulated migration interruption")
        original(conn, script)

    with sqlite3.connect(database, isolation_level=None) as conn:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(conn, now="2026-07-23T12:00:00Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,)]
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type = 'table' AND name IN (
                   'artifacts', 'interrupted_artifact_migration'
               )"""
        ).fetchone()[0] == 0
        monkeypatch.setattr(schema, "_execute_script_in_transaction", original)
        schema.apply_migrations(conn, now="2026-07-23T12:00:01Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]


def test_populated_artifact_state_survives_sqlite_backup_and_restore(
    tmp_path: Path,
) -> None:
    from cortex_platform.product.control import ControlStore

    original_dir = tmp_path / "original"
    original_dir.mkdir(mode=0o700)
    original_dir.chmod(0o700)
    store, run, source, artifact = _context(original_dir)
    committed = _complete(
        store, _request_version(store, run, source, artifact).value
    ).value
    snapshot = store.create_artifact_snapshot(
        workspace_id=artifact["workspace_id"],
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        name="Backup snapshot",
        artifact_version_ids=(committed["id"],),
        actor_id="artifact-fixture",
        idempotency_key="artifact-backup-snapshot-1",
    ).value
    restored_dir = tmp_path / "restored"
    restored_dir.mkdir(mode=0o700)
    restored_database = restored_dir / "control.db"
    with sqlite3.connect(store.path) as source_db, sqlite3.connect(
        restored_database
    ) as target_db:
        source_db.backup(target_db)
    restored_database.chmod(0o600)
    original_key = store.path.with_name(f".{store.path.name}.transport.key")
    restored_key = restored_database.with_name(
        f".{restored_database.name}.transport.key"
    )
    shutil.copy2(original_key, restored_key)
    restored_key.chmod(0o600)

    restored = ControlStore(restored_database)
    restored.initialize()

    assert restored.get_artifact(artifact["id"])["head_artifact_version_id"] == (
        committed["id"]
    )
    assert restored.get_artifact_version(committed["id"])["provenance"] == (
        committed["provenance"]
    )
    assert restored.get_artifact_snapshot(snapshot["id"]) == snapshot


def test_pending_reservation_is_exact_idempotent_and_not_publicly_committed(
    tmp_path: Path,
) -> None:
    store, run, source, artifact = _context(tmp_path)

    first = _request_version(store, run, source, artifact)
    replay = _request_version(store, run, source, artifact)

    assert replay.replayed is True
    assert replay.value == first.value
    assert first.value["state"] == "pending_materialization"
    assert first.value["provenance"] is None
    assert first.value["committed_at"] is None
    assert first.value["parents"] == []
    assert first.value["source_ids"] == [source["id"]]
    assert first.value["materialization_action"]["state"] == "pending"
    with pytest.raises(IdempotencyConflict):
        _request_version(
            store,
            run,
            source,
            artifact,
            content=CONTENT_V2,
        )


def test_generated_artifact_uri_percent_encodes_valid_identifier_punctuation(
    tmp_path: Path,
) -> None:
    store = ControlStore(
        tmp_path / "control.db",
        clock=MutableClock(),
        id_factory=ColonArtifactIds(),
    )
    store.initialize()
    run = make_run(store)
    source = _bind_source(store, run)
    thread = store.get_thread(run["thread_id"])
    artifact = store.create_artifact(
        workspace_id=thread["workspace_id"],
        thread_id=thread["id"],
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        kind="living-brief",
        title="Canonical URI",
        actor_id="artifact-fixture",
        idempotency_key="artifact-colon-create-0001",
    ).value

    reservation = _request_version(store, run, source, artifact).value
    assert reservation["resource_uri"] == (
        "cortex://artifacts/artifact%3A1/artifact_version%3A1"
    )
    assert _complete(store, reservation).value["state"] == "committed"


def test_request_rejects_unbound_source_wrong_attempt_and_uncommitted_parent(
    tmp_path: Path,
) -> None:
    store, run, source, artifact = _context(tmp_path)
    other = make_named_run(store, "other")

    with pytest.raises(InvalidTransition, match="ownership"):
        store.request_artifact_version(
            artifact_id=artifact["id"],
            logical_version=1,
            run_id=other["id"],
            attempt_id=other["attempt"]["id"],
            source_ids=(source["id"],),
            research_engine_refs=(source["engine_ref"],),
            generator={"name": "cortex-research", "version": "0.1.0"},
            tool={"name": "artifact-writer", "version": "1.0.0"},
            parents=(),
            root_id="artifacts",
            relative_path="versions/unbound.md",
            sha256=DIGEST_V1,
            byte_length=len(CONTENT_V1),
            media_type="text/markdown",
            advance_head=False,
            expected_head_revision=None,
            actor_id="artifact-fixture",
            idempotency_key="artifact-unbound-source-1",
        )

    unbound = store.register_source(
        authority="arxiv",
        authority_id="2607.07675",
        source_kind="paper",
        official_title="LingBot-Video",
        engine_ref="paper:lingbot-video",
        actor_id="artifact-fixture",
        idempotency_key="artifact-register-unbound-1",
    ).value
    with pytest.raises(InvalidTransition, match="source"):
        store.request_artifact_version(
            artifact_id=artifact["id"],
            logical_version=1,
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            source_ids=(unbound["id"],),
            research_engine_refs=(unbound["engine_ref"],),
            generator={"name": "cortex-research", "version": "0.1.0"},
            tool={"name": "artifact-writer", "version": "1.0.0"},
            parents=(),
            root_id="artifacts",
            relative_path="versions/unbound.md",
            sha256=DIGEST_V1,
            byte_length=len(CONTENT_V1),
            media_type="text/markdown",
            advance_head=False,
            expected_head_revision=None,
            actor_id="artifact-fixture",
            idempotency_key="artifact-unbound-source-2",
        )

    pending = _request_version(store, run, source, artifact).value
    with pytest.raises(InvalidTransition, match="parent"):
        _request_version(
            store,
            run,
            source,
            artifact,
            logical_version=2,
            content=CONTENT_V2,
            parents=(
                {
                    "artifact_version_id": pending["id"],
                    "sha256": pending["sha256"],
                },
            ),
            key="artifact-uncommitted-parent-01",
        )


def test_claim_epoch_completion_and_lost_ack_are_exact(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    reservation = _request_version(store, run, source, artifact).value
    action = reservation["materialization_action"]
    first_claim = store.claim_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker",
        lease_seconds=30,
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-claim-first-0001",
    ).value

    with pytest.raises(InvalidTransition, match="claim"):
        store.complete_artifact_materialization(
            action_id=action["id"],
            worker_id="artifact-worker",
            claim_epoch=first_claim["claim_epoch"] + 1,
            materialized_result={
                "schema_version": 1,
                "operation_id": action["operation_id"],
                "root_id": action["root_id"],
                "relative_path": action["relative_path"],
                "sha256": reservation["sha256"],
                "byte_length": reservation["byte_length"],
                "media_type": reservation["media_type"],
                "parents": [],
                "replayed": False,
                "recovered_from": None,
            },
            actor_id="artifact-dispatcher",
            idempotency_key="artifact-complete-stale-1",
        )

    result = _complete_after_claim(store, reservation, first_claim)
    replay = _complete_after_claim(store, reservation, first_claim)
    assert replay.replayed is True
    assert replay.value == result.value
    assert result.value["state"] == "committed"
    assert result.value["committed_at"] == result.value["provenance"]["committed_at"]
    assert store.get_artifact(artifact["id"])["head_artifact_version_id"] == result.value["id"]
    assert store.get_artifact(artifact["id"])["head_revision"] == 1
    event_types = [event["type"] for event in store.list_run_events(run["id"])]
    assert event_types.count("artifact.version_committed") == 1
    assert event_types.count("artifact.head_advanced") == 1


def test_expired_claim_is_reclaimed_and_stale_worker_cannot_complete(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store, run, source, artifact = _context(tmp_path, clock=clock)
    reservation = _request_version(store, run, source, artifact).value
    action = reservation["materialization_action"]
    first = store.claim_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker-one",
        lease_seconds=30,
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-first-lease-0001",
    ).value
    clock.advance(seconds=31)
    second = store.claim_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker-two",
        lease_seconds=30,
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-second-lease-001",
    ).value
    result = {
        "schema_version": 1,
        "operation_id": action["operation_id"],
        "root_id": action["root_id"],
        "relative_path": action["relative_path"],
        "sha256": reservation["sha256"],
        "byte_length": reservation["byte_length"],
        "media_type": reservation["media_type"],
        "parents": [],
        "replayed": True,
        "recovered_from": "final",
    }
    with pytest.raises(InvalidTransition, match="stale_claim"):
        store.complete_artifact_materialization(
            action_id=action["id"],
            worker_id="artifact-worker-one",
            claim_epoch=first["claim_epoch"],
            materialized_result=result,
            actor_id="artifact-dispatcher",
            idempotency_key="artifact-stale-complete-01",
        )
    completed = store.complete_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker-two",
        claim_epoch=second["claim_epoch"],
        materialized_result=result,
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-current-complete-1",
    ).value
    assert completed["state"] == "committed"
    assert second["claim_epoch"] == first["claim_epoch"] + 1


def _complete_after_claim(store, reservation: dict, claim: dict):
    action = reservation["materialization_action"]
    return store.complete_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker",
        claim_epoch=claim["claim_epoch"],
        materialized_result={
            "schema_version": 1,
            "operation_id": action["operation_id"],
            "root_id": action["root_id"],
            "relative_path": action["relative_path"],
            "sha256": reservation["sha256"],
            "byte_length": reservation["byte_length"],
            "media_type": reservation["media_type"],
            "parents": reservation["parents"],
            "replayed": False,
            "recovered_from": None,
        },
        actor_id="artifact-dispatcher",
        idempotency_key=f"complete-{action['id']}-01",
    )


def test_publication_before_database_completion_is_adopted_on_replay(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store, run, source, artifact = _context(tmp_path, clock=clock)
    reservation = _request_version(store, run, source, artifact).value
    root = tmp_path / "assets"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    roots = [AssetRoot("artifacts", root, 10_000)]
    interrupted = ArtifactMaterializationService(
        store,
        InterruptingMaterializer(roots, "after_final_link"),
    )

    with pytest.raises(SimulatedCrash):
        interrupted.materialize_action(
            action_id=reservation["materialization_action"]["id"],
            content=CONTENT_V1,
            worker_id="artifact-worker-one",
            lease_seconds=30,
        )
    assert store.get_artifact_version(reservation["id"])["state"] == (
        "pending_materialization"
    )
    published = root / reservation["materialization_action"]["relative_path"]
    assert published.read_bytes() == CONTENT_V1

    clock.advance(seconds=31)
    recovered = ArtifactMaterializationService(
        store, FilesystemMaterializer(roots)
    ).materialize_action(
        action_id=reservation["materialization_action"]["id"],
        content=None,
        worker_id="artifact-worker-two",
        lease_seconds=30,
    )

    assert recovered["state"] == "committed"
    assert recovered["materialization_action"]["result"]["replayed"] is True
    assert recovered["materialization_action"]["result"]["recovered_from"] == (
        "dual-link"
    )


def test_failed_materialization_retries_same_version_and_action(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    reservation = _request_version(store, run, source, artifact).value
    action = reservation["materialization_action"]
    claim = store.claim_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker",
        lease_seconds=30,
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-claim-fail-0001",
    ).value
    failed = store.fail_artifact_materialization(
        action_id=action["id"],
        worker_id="artifact-worker",
        claim_epoch=claim["claim_epoch"],
        failure_category="integrity_error",
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-fail-00000001",
    ).value
    assert failed["state"] == "failed"
    assert store.get_artifact(artifact["id"])["head_revision"] == 0

    retried = store.retry_artifact_materialization(
        action_id=action["id"],
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-retry-000001",
    ).value
    assert retried["state"] == "pending"
    assert retried["artifact_version_id"] == reservation["id"]
    assert retried["operation_id"] == action["operation_id"]
    completed = _complete(store, store.get_artifact_version(reservation["id"]))
    assert completed.value["state"] == "committed"


def test_head_revision_cas_and_snapshot_members_are_immutable(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    first = _request_version(store, run, source, artifact).value
    first = _complete(store, first).value
    parent = ({"artifact_version_id": first["id"], "sha256": first["sha256"]},)
    second = _request_version(
        store,
        run,
        source,
        artifact,
        logical_version=2,
        content=CONTENT_V2,
        parents=parent,
        expected_head_revision=1,
        key="artifact-version-request-02",
    ).value
    second = _complete(store, second).value

    snapshot = store.create_artifact_snapshot(
        workspace_id=artifact["workspace_id"],
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        name="Echo baseline",
        artifact_version_ids=(first["id"],),
        actor_id="artifact-fixture",
        idempotency_key="artifact-snapshot-00001",
    ).value
    assert snapshot["members"] == [
        {
            "artifact_id": artifact["id"],
            "artifact_version_id": first["id"],
            "logical_version": 1,
            "sha256": first["sha256"],
        }
    ]
    assert store.get_artifact(artifact["id"])["head_artifact_version_id"] == second["id"]
    assert store.get_artifact_snapshot(snapshot["id"]) == snapshot

    with pytest.raises(RevisionConflict):
        store.advance_artifact_head(
            artifact_id=artifact["id"],
            artifact_version_id=first["id"],
            expected_head_revision=1,
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            actor_id="artifact-fixture",
            idempotency_key="artifact-stale-head-0001",
        )
    with sqlite3.connect(store.path) as conn, pytest.raises(
        sqlite3.IntegrityError, match="immutable"
    ):
        conn.execute(
            "UPDATE artifact_snapshot_members SET sha256 = ? WHERE snapshot_id = ?",
            ("f" * 64, snapshot["id"]),
        )


def test_concurrent_head_advances_have_one_cas_winner(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    first = _complete(
        store,
        _request_version(
            store,
            run,
            source,
            artifact,
            advance_head=False,
            expected_head_revision=None,
        ).value,
    ).value
    second = _complete(
        store,
        _request_version(
            store,
            run,
            source,
            artifact,
            logical_version=2,
            content=CONTENT_V2,
            parents=(
                {"artifact_version_id": first["id"], "sha256": first["sha256"]},
            ),
            advance_head=False,
            expected_head_revision=None,
            key="artifact-version-request-02",
        ).value,
    ).value

    def advance(version: dict, suffix: str):
        return store.advance_artifact_head(
            artifact_id=artifact["id"],
            artifact_version_id=version["id"],
            expected_head_revision=0,
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            actor_id="artifact-fixture",
            idempotency_key=f"artifact-concurrent-head-{suffix}",
        ).value

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(advance, first, "first-0001"),
            executor.submit(advance, second, "second-001"),
        ]
    outcomes: list[dict | BaseException] = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except BaseException as exc:
            outcomes.append(exc)
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, RevisionConflict) for item in outcomes) == 1
    current = store.get_artifact(artifact["id"])
    assert current["head_revision"] == 1
    assert current["head_artifact_version_id"] in {first["id"], second["id"]}


def test_materialization_commits_when_automatic_head_cas_loses(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    first = _request_version(store, run, source, artifact).value
    second = _request_version(
        store,
        run,
        source,
        artifact,
        logical_version=2,
        content=CONTENT_V2,
        expected_head_revision=0,
        key="artifact-version-request-02",
    ).value

    first = _complete(store, first).value
    second = _complete(store, second).value

    assert first["materialization_action"]["head_advanced"] is True
    assert first["materialization_action"]["observed_head_revision"] == 1
    assert second["state"] == "committed"
    assert second["materialization_action"]["state"] == "completed"
    assert second["materialization_action"]["head_advanced"] is False
    assert second["materialization_action"]["observed_head_revision"] == 1
    assert store.get_artifact(artifact["id"])["head_artifact_version_id"] == first["id"]
    assert store.list_pending_artifact_materializations() == []


def test_sql_cannot_bypass_version_commit_or_head_revision_cas(
    tmp_path: Path,
) -> None:
    store, run, source, artifact = _context(tmp_path)
    pending = _request_version(store, run, source, artifact).value
    store.claim_artifact_materialization(
        action_id=pending["materialization_action"]["id"],
        worker_id="artifact-worker",
        lease_seconds=30,
        actor_id="artifact-dispatcher",
        idempotency_key="artifact-sql-claim-000001",
    )
    with sqlite3.connect(store.path) as conn, pytest.raises(
        sqlite3.IntegrityError, match="state transition"
    ):
        conn.execute(
            """UPDATE artifact_versions
               SET state = 'committed', provenance_json = '{}', committed_at = 'now'
               WHERE id = ?""",
            (pending["id"],),
        )

    committed = _complete(
        store,
        _request_version(
            store,
            run,
            source,
            artifact,
            logical_version=2,
            content=CONTENT_V2,
            advance_head=False,
            expected_head_revision=None,
            key="artifact-version-request-02",
        ).value,
    ).value
    with sqlite3.connect(store.path) as conn, pytest.raises(
        sqlite3.IntegrityError, match="head must advance"
    ):
        conn.execute(
            "UPDATE artifacts SET head_artifact_version_id = ? WHERE id = ?",
            (committed["id"], artifact["id"]),
        )


def test_sql_cannot_insert_precommitted_version_or_snapshot(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    committed = _complete(
        store,
        _request_version(store, run, source, artifact).value,
    ).value

    with sqlite3.connect(store.path) as conn, pytest.raises(
        sqlite3.IntegrityError, match="ownership"
    ):
        conn.execute(
            """INSERT INTO artifact_versions
               (id, artifact_id, logical_version, resource_uri, sha256,
                byte_length, media_type, run_id, attempt_id, generator_name,
                generator_version, tool_name, tool_version, lineage_sealed,
                state, provenance_json, created_at, committed_at)
               SELECT 'artifact-version-raw', artifact_id, 99,
                      'cortex://artifacts/raw/version', sha256, byte_length,
                      media_type, run_id, attempt_id, generator_name,
                      generator_version, tool_name, tool_version, 1,
                      'committed', provenance_json, created_at, committed_at
               FROM artifact_versions WHERE id = ?""",
            (committed["id"],),
        )

    with sqlite3.connect(store.path) as conn, pytest.raises(
        sqlite3.IntegrityError, match="must start building"
    ):
        conn.execute(
            """INSERT INTO artifact_snapshots
               (id, workspace_id, name, member_count, state, created_at)
               VALUES ('artifact-snapshot-raw', ?, 'Raw snapshot', 1,
                       'committed', 'now')""",
            (artifact["workspace_id"],),
        )


def test_normalized_lineage_cannot_be_extended_after_reservation(
    tmp_path: Path,
) -> None:
    store, run, source, artifact = _context(tmp_path)
    reservation = _request_version(store, run, source, artifact).value

    with sqlite3.connect(store.path) as conn, pytest.raises(
        sqlite3.IntegrityError, match="sealed"
    ):
        conn.execute(
            """INSERT INTO artifact_version_engine_refs
               (artifact_version_id, engine_ref, position)
               VALUES (?, 'paper:late-injection', 1)""",
            (reservation["id"],),
        )


def test_terminal_run_cannot_claim_or_commit_materialization(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    reservation = _request_version(store, run, source, artifact).value
    current_run = store.get_run(run["id"])
    store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=current_run["revision"],
        actor_id="artifact-fixture",
        idempotency_key="artifact-run-cancel-0001",
    )

    action = store.get_artifact_materialization(
        reservation["materialization_action"]["id"]
    )
    assert action["state"] == "failed"
    assert action["failure_category"] == "run_terminated"
    assert store.get_artifact_version(reservation["id"])["state"] == "failed"

    with pytest.raises(InvalidTransition):
        store.claim_artifact_materialization(
            action_id=reservation["materialization_action"]["id"],
            worker_id="artifact-worker",
            lease_seconds=30,
            actor_id="artifact-dispatcher",
            idempotency_key="artifact-terminal-claim-1",
        )


def test_pause_fences_old_attempt_materialization_before_resume(tmp_path: Path) -> None:
    store, run, source, artifact = _context(tmp_path)
    run = _start_run(store, run)
    reservation = _request_version(store, run, source, artifact).value
    identity = run["runtime_identity"]

    pause_requested = store.transition_run(
        run_id=run["id"],
        target_state="pause_requested",
        expected_revision=run["revision"],
        actor_id="artifact-fixture",
        idempotency_key="artifact-pause-requested-01",
    ).value
    checkpointed = store.commit_checkpoint(
        run_id=run["id"],
        checkpoint_uri="cortex://artifacts/checkpoints/artifact-pause.json",
        expected_revision=pause_requested["revision"],
        actor_id="artifact-runtime",
        idempotency_key="artifact-pause-checkpoint-01",
        **identity,
    ).value
    paused = store.apply_runtime_transition(
        run_id=run["id"],
        target_state="paused",
        expected_revision=checkpointed["revision"],
        actor_id="artifact-runtime",
        idempotency_key="artifact-runtime-paused-0001",
        **identity,
    ).value

    action = store.get_artifact_materialization(
        reservation["materialization_action"]["id"]
    )
    assert action["state"] == "failed"
    assert action["failure_category"] == "attempt_superseded"
    assert store.get_artifact_version(reservation["id"])["state"] == "failed"
    assert store.list_pending_artifact_materializations() == []

    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="artifact-fixture",
        idempotency_key="artifact-resume-run-000001",
    ).value
    assert resumed["attempt"]["id"] != reservation["attempt_id"]
