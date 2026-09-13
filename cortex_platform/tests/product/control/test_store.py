from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import stat
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_type_hints

import pytest

from cortex_platform.product.control.schema import MIGRATION_VERSIONS
from cortex_platform.product import control as control_module
from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    RevisionConflict,
    TransportBindingConflict,
)
from cortex_platform.product.control.schema import (
    SCHEMA_VERSION,
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    _MIGRATION_4,
    _MIGRATION_5,
    _MIGRATION_6,
    _MIGRATION_7,
    _MIGRATION_8,
    _MIGRATION_9,
)
from cortex_platform.product.resources import parse_resource_uri

_EXACT_RUNTIME_IDENTITY = {
    "runtime_slot_id": "slot-test",
    "runtime_artifact_digest": "artifact-test",
    "runtime_worker_protocol": "protocol-test",
}


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value


def _key(value: str) -> str:
    return f"command-{value:0>8}"


def _frozen_delivery(
    *,
    event_id: str = "event-m10",
    digest_character: str = "d",
    operation_id: str = "telegram-delivery-m10",
    token_suffix: str = "signature",
    expires_at: datetime | None = None,
) -> tuple[
    control_module.FrozenTransportDeliveryProjection,
    tuple[control_module.TransportOpaqueTargetRegistration, ...],
]:
    destination_digest = "hmac-sha256:" + digest_character * 64
    expires_at = expires_at or datetime(2026, 7, 23, 12, 5, tzinfo=UTC)
    callback = f"ac1.callback.{token_suffix}"
    action_digest = hashlib.sha256(callback.encode()).hexdigest()
    deep_link_token = f"dl1.deep-link.{token_suffix}"
    text = f"Open https://cortex.example/open?token={deep_link_token}"
    token_bytes = deep_link_token.encode()
    start_offset = text.encode().index(token_bytes)
    first = control_module.TransportDeliveryChunkProjection(
        chunk_index=0,
        operation_id=f"{operation_id}:chunk:0",
        text="First chunk",
        parse_mode="MarkdownV2",
        buttons=(),
        capabilities=(),
        chunk_hash="0" * 64,
    )
    first = replace(
        first,
        chunk_hash=control_module.transport_delivery_chunk_hash(first),
    )
    final = control_module.TransportDeliveryChunkProjection(
        chunk_index=1,
        operation_id=f"{operation_id}:chunk:1",
        text=text,
        parse_mode="MarkdownV2",
        buttons=(
            control_module.TransportDeliveryButton(
                label="Retry",
                callback_data=callback,
                token_digest=action_digest,
            ),
        ),
        capabilities=(
            control_module.TransportDeliveryCapability(
                namespace="action",
                token_digest=action_digest,
                expires_at=expires_at,
            ),
            control_module.TransportDeliveryCapability(
                namespace="deep_link",
                token_digest=hashlib.sha256(token_bytes).hexdigest(),
                expires_at=expires_at,
                start_offset=start_offset,
                end_offset=start_offset + len(token_bytes),
            ),
        ),
        chunk_hash="0" * 64,
    )
    final = replace(
        final,
        chunk_hash=control_module.transport_delivery_chunk_hash(final),
    )
    projection = control_module.FrozenTransportDeliveryProjection(
        delivery_key=control_module.TransportDeliveryKey(
            transport="telegram",
            destination_digest=destination_digest,
            event_id=event_id,
            projection_version=2,
        ),
        operation_id=operation_id,
        destination_binding_digest=destination_digest,
        routing="topic",
        capability_binding_digest="a" * 64,
        rpc_timeout_seconds=30,
        projection_hash="0" * 64,
        chunks=(first, final),
    )
    targets = (
        control_module.TransportOpaqueTargetRegistration(
            token_digest=action_digest,
            expires_at=expires_at,
            target=control_module.TransportOpaqueTarget(
                namespace="action",
                purpose="control",
                resource_kind="run",
                resource_id="run-1",
                expected_revision=3,
                choice="retry",
                scope_digest=destination_digest,
                expires_at=expires_at,
            ),
        ),
        control_module.TransportOpaqueTargetRegistration(
            token_digest=hashlib.sha256(token_bytes).hexdigest(),
            expires_at=expires_at,
            target=control_module.TransportOpaqueTarget(
                namespace="deep_link",
                purpose="open",
                resource_kind="run",
                resource_id="run-1",
                expected_revision=None,
                choice=None,
                scope_digest=None,
                expires_at=expires_at,
            ),
        ),
    )
    return (
        replace(
            projection,
            projection_hash=control_module.transport_delivery_projection_hash(
                projection,
                opaque_targets=targets,
            ),
        ),
        targets,
    )


def _initialize_database(path: str) -> str:
    ControlStore(Path(path)).initialize()
    return "ok"


def _workspace(store: ControlStore) -> dict:
    return store.create_workspace(
        title="Research", actor_id="local", idempotency_key=_key("workspace")
    ).value


def _thread(store: ControlStore) -> dict:
    workspace = _workspace(store)
    return store.create_thread(
        workspace_id=workspace["id"],
        title="Echo / Helios",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key("thread"),
    ).value


def _running_run(store: ControlStore) -> dict:
    thread = _thread(store)
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("run"),
    ).value
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        dispatch_owner="worker-main",
        runtime_release_id="hermes-test",
        state_generation_id="state-test",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("runtime-reserve"),
    ).value
    binding = store.create_runtime_binding(
        thread_id=thread["id"],
        adapter_id="hermes",
        runtime_session_ref="private-session",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key=_key("runtime-binding"),
    ).value
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        runtime_binding_id=binding["id"],
        runtime_release_id="hermes-test",
        state_generation_id="state-test",
        dispatch_owner="worker-main",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("runtime-pin"),
    ).value
    identity = {
        "_attempt_id": run["attempt"]["id"],
        "_binding_id": binding["id"],
        "_release_id": "hermes-test",
        "_generation_id": "state-test",
    }
    run = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=identity["_attempt_id"],
        runtime_binding_id=identity["_binding_id"],
        runtime_release_id=identity["_release_id"],
        state_generation_id=identity["_generation_id"],
        target_state="starting",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("starting"),
    ).value
    run = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=identity["_attempt_id"],
        runtime_binding_id=identity["_binding_id"],
        runtime_release_id=identity["_release_id"],
        state_generation_id=identity["_generation_id"],
        target_state="running",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("running"),
    ).value
    return {**run, **identity}


def _runtime_transition(
    store: ControlStore, run: dict, target_state: str, key: str
) -> dict:
    value = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state=target_state,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key(key),
    ).value
    return {
        **value,
        "_attempt_id": run["_attempt_id"],
        "_binding_id": run["_binding_id"],
        "_release_id": run["_release_id"],
    }


def test_bound_source_cancel_uses_runtime_cancel_outbox_and_terminal_cleanup(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        title="Bound source",
        locator=None,
        candidates=(
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2607.07675",
                "official_title": "Bound source",
            },
        ),
        actor_id="local",
        idempotency_key=_key("bound-source-intent"),
    ).value

    resolved = store.resolve_source_intent(
        intent_id=intent["id"],
        choice="cancel",
        expected_revision=0,
        actor_id="local",
        idempotency_key=_key("bound-source-cancel"),
    ).value
    cancel_requested = store.get_run(run["id"])
    assert resolved["state"] == "canceled"
    assert cancel_requested["state"] == "cancel_requested"
    actions = store.list_pending_runtime_actions()
    assert [action["kind"] for action in actions] == ["control.cancel"]

    claim = store.claim_runtime_action(
        action_id=actions[0]["id"],
        worker_id="runtime-worker",
        lease_seconds=30,
        actor_id="dispatcher",
        idempotency_key=_key("claim-bound-cancel"),
    ).value
    store.acknowledge_runtime_action(
        action_id=actions[0]["id"],
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        claim_owner="runtime-worker",
        claim_epoch=claim["claim_epoch"],
        expected_revision=cancel_requested["revision"],
        actor_id="runtime",
        idempotency_key=_key("ack-bound-cancel"),
    )
    after_ack = store.get_run(run["id"])
    terminal = store.transition_run(
        run_id=run["id"],
        target_state="canceled",
        expected_revision=after_ack["revision"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        actor_id="runtime",
        idempotency_key=_key("terminal-bound-cancel"),
    ).value

    assert terminal["state"] == "canceled"
    assert store.get_source_intent(intent["id"])["state"] == "canceled"
    assert len(store.list_pending_pin_releases()) == 1


def test_migrations_are_repeatable_and_reject_newer_schema(store: ControlStore) -> None:
    store.initialize()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'future')"
        )
    with pytest.raises(RuntimeError, match="newer"):
        store.initialize()


def test_v1_database_migrates_additively_to_runtime_delivery_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(_MIGRATION_1)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'old')"
        )
        conn.execute(
            """INSERT INTO workspaces
               (id, title, revision, created_at, updated_at)
               VALUES ('ws-old', 'Preserved', 0, 'old', 'old')"""
        )
    database.chmod(0o600)
    key = database.with_name(f".{database.name}.transport.key")
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    migrated = ControlStore(database)
    migrated.initialize()

    assert migrated.get_workspace("ws-old")["title"] == "Preserved"
    with sqlite3.connect(database) as conn:
        versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(attempts)")
        }
        action_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(runtime_actions)")
        }
    assert versions == [(version,) for version in MIGRATION_VERSIONS]
    assert {
        "state_generation_id",
        "runtime_identity_version",
        "runtime_slot_id",
        "runtime_artifact_digest",
        "runtime_worker_protocol",
    } <= attempt_columns
    assert {
        "claim_owner",
        "claim_expires_at",
        "claim_epoch",
        "outcome_state",
        "failure_category",
    } <= action_columns


def test_v2_database_migrates_additively_to_durable_recovery_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(_MIGRATION_1)
        conn.executescript(_MIGRATION_2)
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
            [(1,), (2,)],
        )
        conn.execute(
            """INSERT INTO workspaces
               (id, title, revision, created_at, updated_at)
               VALUES ('ws-v2', 'Preserved v2', 0, 'old', 'old')"""
        )
    database.chmod(0o600)
    key = database.with_name(f".{database.name}.transport.key")
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    migrated = ControlStore(database)
    migrated.initialize()

    assert migrated.get_workspace("ws-v2")["title"] == "Preserved v2"
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
        "runtime_event_inbox",
        "runtime_pin_releases",
        "runtime_recovery_commands",
    } <= tables


def test_v8_database_migrates_additively_to_operations_registry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        for version, migration in enumerate(
            (
                _MIGRATION_1,
                _MIGRATION_2,
                _MIGRATION_3,
                _MIGRATION_4,
                _MIGRATION_5,
                _MIGRATION_6,
                _MIGRATION_7,
                _MIGRATION_8,
            ),
            start=1,
        ):
            conn.executescript(migration)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )
        conn.execute(
            """INSERT INTO workspaces
               (id, title, revision, created_at, updated_at)
               VALUES ('ws-v8', 'Preserved v8', 0, 'old', 'old')"""
        )
        conn.execute(
            """INSERT INTO transport_command_receipts
               (transport, command_key, request_hash, response_json, created_at)
               VALUES ('telegram', 'command-00000001', ?, '{"ok":true}', 'old')""",
            ("a" * 64,),
        )
    database.chmod(0o600)
    key = database.with_name(f".{database.name}.transport.key")
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    migrated = ControlStore(database)
    migrated.initialize()

    assert migrated.get_workspace("ws-v8")["title"] == "Preserved v8"
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
            "asset_roots",
            "connectors",
            "paired_backup_proofs",
            "system_health_observations",
        } <= tables
        assert "protected_set_manifest_json" in {
            row[1]
            for row in conn.execute("PRAGMA table_info(paired_backup_proofs)")
        }
        assert {
            row[1]
            for row in conn.execute("PRAGMA table_info(system_health_observations)")
        } == {
            "id",
            "asset_root_id",
            "connector_id",
            "backup_proof_id",
            "status",
            "category",
            "observed_at",
            "metrics_json",
            "created_at",
        }
        assert conn.execute(
            "SELECT response_json FROM transport_command_receipts "
            "WHERE transport = 'telegram' AND command_key = 'command-00000001'"
        ).fetchone() == ('{"ok":true}',)

    migrated.initialize()
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 9"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT title FROM workspaces WHERE id = 'ws-v8'"
        ).fetchone() == ("Preserved v8",)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_command_receipts "
            "WHERE transport = 'telegram' AND command_key = 'command-00000001'"
        ).fetchone() == (1,)


def test_v9_database_migrates_to_frozen_chunk_delivery_without_rewriting_legacy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    legacy_rows = (
        (
            "telegram",
            "hmac-sha256:" + "a" * 64,
            "legacy-delivered",
            1,
            "delivered",
            None,
            1,
            None,
            "old-created",
            "old-delivered",
        ),
        (
            "telegram",
            "hmac-sha256:" + "b" * 64,
            "legacy-pending",
            1,
            "pending",
            None,
            0,
            None,
            "old-created",
            None,
        ),
        (
            "telegram",
            "hmac-sha256:" + "c" * 64,
            "legacy-claimed",
            1,
            "claimed",
            "legacy-worker",
            2,
            "old-expiry",
            "old-created",
            None,
        ),
    )
    with sqlite3.connect(database) as conn:
        for version, migration in enumerate(
            (
                _MIGRATION_1,
                _MIGRATION_2,
                _MIGRATION_3,
                _MIGRATION_4,
                _MIGRATION_5,
                _MIGRATION_6,
                _MIGRATION_7,
                _MIGRATION_8,
                _MIGRATION_9,
            ),
            start=1,
        ):
            conn.executescript(migration)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )
        conn.executemany(
            """INSERT INTO transport_deliveries(
                   transport, destination_digest, event_id, projection_version,
                   state, claim_owner, claim_epoch, claim_expires_at,
                   created_at, delivered_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            legacy_rows,
        )
    database.chmod(0o600)
    key = database.with_name(f".{database.name}.transport.key")
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    migrated = ControlStore(
        database,
        clock=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )
    migrated.initialize()
    migrated.initialize()

    expected_tables = {
        "transport_delivery_projections",
        "transport_delivery_chunks",
        "transport_delivery_chunk_buttons",
        "transport_delivery_chunk_capabilities",
        "transport_delivery_chunk_resolutions",
    }
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        assert conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 10"
        ).fetchone() == ("2026-07-28T12:00:00.000000Z",)
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert expected_tables <= tables
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {
            "transport_delivery_projections_state_idx",
            "transport_delivery_chunks_claim_idx",
            "transport_delivery_chunk_capabilities_expiry_idx",
            "transport_delivery_chunk_resolutions_chunk_idx",
        } <= indexes
        assert len(
            conn.execute(
                "PRAGMA foreign_key_list(transport_delivery_chunks)"
            ).fetchall()
        ) >= 5
        assert conn.execute(
            """SELECT transport, destination_digest, event_id,
                      projection_version, state, claim_owner, claim_epoch,
                      claim_expires_at, created_at, delivered_at
               FROM transport_deliveries ORDER BY event_id"""
        ).fetchall() == sorted(legacy_rows, key=lambda row: row[2])
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            conn.execute(
                """INSERT INTO transport_delivery_projections(
                       transport, destination_digest, event_id,
                       projection_version, operation_id, request_hash,
                       projection_hash, destination_binding_digest, routing,
                       capability_binding_digest, rpc_timeout_seconds,
                       chunk_count, state, revision, created_at
                   ) VALUES ('telegram', ?, 'malformed-time', 1,
                             'malformed-time-operation', ?, ?, ?, 'root', ?,
                             30, 1, 'pending', 0,
                             'xxxx-xx-xxTxx:xx:xx.xxxxxxZ')""",
                (
                    "hmac-sha256:" + "d" * 64,
                    "a" * 64,
                    "b" * 64,
                    "hmac-sha256:" + "d" * 64,
                    "c" * 64,
                ),
            )


def test_interrupted_migration_10_rolls_back_all_chunk_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cortex_platform.product.control import schema

    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        for version, migration in enumerate(
            (
                _MIGRATION_1,
                _MIGRATION_2,
                _MIGRATION_3,
                _MIGRATION_4,
                _MIGRATION_5,
                _MIGRATION_6,
                _MIGRATION_7,
                _MIGRATION_8,
                _MIGRATION_9,
            ),
            start=1,
        ):
            conn.executescript(migration)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )

    original = schema._execute_script_in_transaction

    def interrupt(conn: sqlite3.Connection, script: str) -> None:
        if script == schema._MIGRATION_10:
            conn.execute("CREATE TABLE interrupted_transport_migration(id TEXT)")
            raise RuntimeError("simulated migration interruption")
        original(conn, script)

    with sqlite3.connect(database, isolation_level=None) as conn:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(conn, now="2026-07-28T12:00:00.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(index,) for index in range(1, 10)]
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type = 'table' AND name IN (
                   'transport_delivery_projections',
                   'interrupted_transport_migration'
               )"""
        ).fetchone() == (0,)

        monkeypatch.setattr(schema, "_execute_script_in_transaction", original)
        schema.apply_migrations(conn, now="2026-07-28T12:00:01.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]


def test_migration_9_rejects_raw_identity_replacement(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        conn.execute(
            """INSERT INTO asset_roots
               (root_id, private_path, max_bytes, enabled, revision,
                created_at, updated_at)
               VALUES ('artifacts', '/safe', 100, 1, 1, 'old', 'old')"""
        )
        conn.execute(
            """INSERT INTO connectors
               (id, kind, adapter_id, display_name, credential_alias, enabled,
                revision, created_at, updated_at)
               VALUES ('source-main', 'source', 'adapter-a', 'Original', NULL,
                       1, 1, 'old', 'old')"""
        )
        conn.execute(
            """INSERT INTO paired_backup_proofs
               (id, backup_set_digest, protected_set_manifest_json,
                primary_completed_at, primary_snapshot_count,
                independent_completed_at, independent_snapshot_count,
                restore_completed_at, restored_database_count,
                verified_sample_count, created_at)
               VALUES ('proof-1', ?, '{}', '2026-07-27T07:00:00Z', 1,
                       '2026-07-27T07:00:00Z', 1, '2026-07-27T07:01:00Z',
                       1, 0, '2026-07-27T07:01:00Z')""",
            ("a" * 64,),
        )
        conn.execute(
            """INSERT INTO system_health_observations
               (id, asset_root_id, connector_id, backup_proof_id, status,
                category, observed_at, metrics_json, created_at)
               VALUES ('health-1', 'artifacts', NULL, NULL, 'ok',
                       'storage.available', '2026-07-27T07:01:00Z', '{}',
                       '2026-07-27T07:01:00Z')"""
        )
        cases = (
            (
                """INSERT OR REPLACE INTO asset_roots
                   (root_id, private_path, max_bytes, enabled, revision,
                    created_at, updated_at)
                   VALUES ('artifacts', '/drift', 100, 1, 0, 'new', 'new')""",
                (),
                "SELECT private_path, revision FROM asset_roots WHERE root_id = 'artifacts'",
                ("/safe", 1),
            ),
            (
                """INSERT OR REPLACE INTO connectors
                   (id, kind, adapter_id, display_name, credential_alias,
                    enabled, revision, created_at, updated_at)
                   VALUES ('source-main', 'source', 'adapter-b', 'Drift', NULL,
                           1, 0, 'new', 'new')""",
                (),
                "SELECT adapter_id, revision FROM connectors WHERE id = 'source-main'",
                ("adapter-a", 1),
            ),
            (
                """INSERT OR REPLACE INTO paired_backup_proofs
                   (id, backup_set_digest, protected_set_manifest_json,
                    primary_completed_at, primary_snapshot_count,
                    independent_completed_at, independent_snapshot_count,
                    restore_completed_at, restored_database_count,
                    verified_sample_count, created_at)
                   VALUES ('proof-1', ?, '{}', '2026-07-27T08:00:00Z', 2,
                           '2026-07-27T08:00:00Z', 2,
                           '2026-07-27T08:01:00Z', 2, 1,
                           '2026-07-27T08:01:00Z')""",
                ("b" * 64,),
                "SELECT backup_set_digest, primary_snapshot_count "
                "FROM paired_backup_proofs WHERE id = 'proof-1'",
                ("a" * 64, 1),
            ),
            (
                """INSERT OR REPLACE INTO system_health_observations
                   (id, asset_root_id, connector_id, backup_proof_id, status,
                    category, observed_at, metrics_json, created_at)
                   VALUES ('health-1', 'artifacts', NULL, NULL, 'degraded',
                           'storage.drift', '2026-07-27T08:01:00Z', '{}',
                           '2026-07-27T08:01:00Z')""",
                (),
                "SELECT status, category FROM system_health_observations "
                "WHERE id = 'health-1'",
                ("ok", "storage.available"),
            ),
        )

        for statement, parameters, query, expected in cases:
            with pytest.raises(sqlite3.IntegrityError, match="replacement"):
                conn.execute(statement, parameters)
            assert conn.execute(query).fetchone() == expected


def test_migration_9_rejects_null_registry_identities(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """INSERT INTO asset_roots
               (root_id, private_path, max_bytes, enabled, revision,
                created_at, updated_at)
               VALUES ('artifacts', '/safe', 100, 1, 0, 'old', 'old')"""
        )
        cases = (
            (
                """INSERT INTO asset_roots
                   (root_id, private_path, max_bytes, enabled, revision,
                    created_at, updated_at)
                   VALUES (NULL, '/null-root', 100, 1, 0, 'old', 'old')""",
                (),
                "asset_roots",
                "root_id",
            ),
            (
                """INSERT INTO connectors
                   (id, kind, adapter_id, display_name, credential_alias,
                    enabled, revision, created_at, updated_at)
                   VALUES (NULL, 'source', 'adapter-a', 'Null', NULL,
                           1, 0, 'old', 'old')""",
                (),
                "connectors",
                "id",
            ),
            (
                """INSERT INTO paired_backup_proofs
                   (id, backup_set_digest, protected_set_manifest_json,
                    primary_completed_at, primary_snapshot_count,
                    independent_completed_at, independent_snapshot_count,
                    restore_completed_at, restored_database_count,
                    verified_sample_count, created_at)
                   VALUES (NULL, ?, '{}', '2026-07-27T07:00:00Z', 1,
                           '2026-07-27T07:00:00Z', 1,
                           '2026-07-27T07:01:00Z', 1, 0,
                           '2026-07-27T07:01:00Z')""",
                ("a" * 64,),
                "paired_backup_proofs",
                "id",
            ),
            (
                """INSERT INTO system_health_observations
                   (id, asset_root_id, connector_id, backup_proof_id, status,
                    category, observed_at, metrics_json, created_at)
                   VALUES (NULL, 'artifacts', NULL, NULL, 'ok',
                           'storage.available', '2026-07-27T07:01:00Z', '{}',
                           '2026-07-27T07:01:00Z')""",
                (),
                "system_health_observations",
                "id",
            ),
        )

        for statement, parameters, table, column in cases:
            with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
                conn.execute(statement, parameters)
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
            ).fetchone() == (0,)


def test_registry_contract_is_frozen_and_manifest_is_canonical() -> None:
    control_store = control_module.ControlStoreSnapshot(
        schema_version=SCHEMA_VERSION,
        schema_fingerprint_sha256="a" * 64,
        database_sha256="b" * 64,
        database_byte_length=1,
        identity_companion_sha256="c" * 64,
        identity_companion_byte_length=32,
    )
    manifest = control_module.ProtectedSetManifest(
        schema_version=1,
        control_store=control_store,
        logical_snapshots=(
            control_module.ProtectedLogicalSnapshot(
                logical_id="research.database",
                content_sha256="d" * 64,
                byte_length=1,
            ),
        ),
        asset_roots=(
            control_module.ProtectedAssetRootSnapshot(
                root_id="artifacts",
                revision=0,
                content_manifest_sha256="e" * 64,
                file_count=0,
                byte_length=0,
            ),
        ),
    )
    expected = (
        b'{"asset_roots":[{"byte_length":0,"content_manifest_sha256":"'
        + b"e" * 64
        + b'","file_count":0,"revision":0,"root_id":"artifacts"}],'
        + b'"control_store":{"database_byte_length":1,"database_sha256":"'
        + b"b" * 64
        + b'","identity_companion_byte_length":32,'
        + b'"identity_companion_sha256":"'
        + b"c" * 64
        + b'","schema_fingerprint_sha256":"'
        + b"a" * 64
        # The frozen property is the canonical ENCODING -- key order, separators,
        # no whitespace. The schema version is a value that legitimately moves
        # with the store, so it tracks the constant rather than pinning a
        # number that would make every migration look like a contract break.
        + b'","schema_version":'
        + str(SCHEMA_VERSION).encode("ascii")
        + b'},"logical_snapshots":['
        + b'{"byte_length":1,"content_sha256":"'
        + b"d" * 64
        + b'","logical_id":"research.database"}],"schema_version":1}'
    )

    assert control_module.canonical_protected_set_manifest(manifest) == expected
    assert control_module.protected_set_digest(manifest) == hashlib.sha256(
        expected
    ).hexdigest()
    with pytest.raises(FrozenInstanceError):
        manifest.schema_version = 2
    with pytest.raises(ValueError, match="database_sha256"):
        control_module.canonical_protected_set_manifest(
            replace(
                manifest,
                control_store=replace(control_store, database_sha256="B" * 64),
            )
        )


def test_system_health_observation_input_metrics_are_deeply_immutable() -> None:
    metrics = (("database_count", 8), ("independent_copy_ok", True))
    observation = control_module.SystemHealthObservationInput(
        observation_id="health-backup-20260727",
        subject=control_module.HealthSubject("backup_proof", "proof-20260727"),
        status="ok",
        category="backup.verified",
        observed_at=datetime(2026, 7, 27, 7, 1, tzinfo=UTC),
        metrics=metrics,
    )

    assert get_type_hints(control_module.SystemHealthObservationInput)[
        "metrics"
    ] == tuple[tuple[str, bool | int], ...]
    with pytest.raises(TypeError):
        observation.metrics[0] = ("database_count", 9)


def test_asset_root_lifecycle_is_durable_idempotent_and_revision_fenced(
    store: ControlStore, tmp_path: Path
) -> None:
    first = store.register_asset_root(
        root_id="artifacts",
        private_path=tmp_path / "assets-a",
        max_bytes=10_000,
        enabled=True,
        actor_id="operator",
        idempotency_key=_key("root-register"),
    )
    assert first.root_id == "artifacts"
    assert first.private_path == tmp_path / "assets-a"
    assert first.revision == 0

    reopened = ControlStore(store.path)
    reopened.initialize()
    assert reopened.get_asset_root("artifacts") == first
    assert reopened.register_asset_root(
        root_id="artifacts",
        private_path=tmp_path / "assets-a",
        max_bytes=10_000,
        enabled=True,
        actor_id="operator",
        idempotency_key=_key("root-register"),
    ) == first
    with pytest.raises(InvalidTransition, match="already_registered"):
        reopened.register_asset_root(
            root_id="artifacts",
            private_path=tmp_path / "assets-a",
            max_bytes=10_000,
            enabled=True,
            actor_id="operator",
            idempotency_key=_key("root-register-again"),
        )

    updated = reopened.update_asset_root(
        root_id="artifacts",
        private_path=tmp_path / "assets-b",
        max_bytes=20_000,
        enabled=False,
        expected_revision=0,
        actor_id="operator",
        idempotency_key=_key("root-relocate"),
    )
    assert updated.root_id == first.root_id
    assert updated.private_path == tmp_path / "assets-b"
    assert updated.revision == 1
    assert updated.enabled is False
    assert reopened.list_asset_roots() == (updated,)

    with pytest.raises(RevisionConflict) as error:
        reopened.update_asset_root(
            root_id="artifacts",
            private_path=tmp_path / "private-stale-path",
            max_bytes=30_000,
            enabled=True,
            expected_revision=0,
            actor_id="operator",
            idempotency_key=_key("root-stale"),
        )
    assert error.value.current == {"id": "artifacts", "revision": 1}
    assert "private-stale-path" not in str(error.value)
    assert reopened.get_asset_root("artifacts") == updated


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("root_id", "Artifacts"),
        ("root_id", "-artifacts"),
        ("private_path", Path("relative/assets")),
        ("private_path", Path("/")),
        ("private_path", Path("/tmp/assets\0hidden")),
        ("max_bytes", True),
        ("max_bytes", 0),
        ("enabled", 1),
    ],
)
def test_asset_root_validation_precedes_registry_and_receipt_writes(
    store: ControlStore, tmp_path: Path, field: str, invalid: object
) -> None:
    arguments: dict[str, object] = {
        "root_id": "artifacts",
        "private_path": tmp_path / "assets",
        "max_bytes": 10_000,
        "enabled": True,
        "actor_id": "operator",
        "idempotency_key": _key(f"invalid-root-{field}-{type(invalid).__name__}"),
    }
    arguments[field] = invalid

    with pytest.raises((TypeError, ValueError)):
        store.register_asset_root(**arguments)

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset_roots").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM idempotency_receipts "
            "WHERE actor_id = 'operator' AND operation LIKE 'INTERNAL:asset-roots/%'"
        ).fetchone() == (0,)


def test_connector_lifecycle_is_durable_idempotent_and_revision_fenced(
    store: ControlStore,
) -> None:
    first = store.register_connector(
        connector_id="hermes-runtime",
        kind="runtime",
        adapter_id="hermes",
        display_name="Hermes Runtime",
        credential_alias="runtime_provider",
        enabled=False,
        actor_id="operator",
        idempotency_key=_key("connector-register"),
    )
    assert first.revision == 0
    assert first.credential_alias == "runtime_provider"

    reopened = ControlStore(store.path)
    reopened.initialize()
    assert reopened.get_connector("hermes-runtime") == first
    assert reopened.register_connector(
        connector_id="hermes-runtime",
        kind="runtime",
        adapter_id="hermes",
        display_name="Hermes Runtime",
        credential_alias="runtime_provider",
        enabled=False,
        actor_id="operator",
        idempotency_key=_key("connector-register"),
    ) == first
    with pytest.raises(InvalidTransition, match="already_registered"):
        reopened.register_connector(
            connector_id="hermes-runtime",
            kind="runtime",
            adapter_id="hermes-v2",
            display_name="Hermes Runtime",
            credential_alias="runtime_provider",
            enabled=True,
            actor_id="operator",
            idempotency_key=_key("connector-register-again"),
        )

    updated = reopened.update_connector(
        connector_id="hermes-runtime",
        adapter_id="hermes-v2",
        display_name="Hermes Runtime",
        credential_alias="runtime_provider",
        enabled=True,
        expected_revision=0,
        actor_id="operator",
        idempotency_key=_key("connector-enable"),
    )
    assert updated.id == first.id
    assert updated.kind == first.kind
    assert updated.revision == 1
    assert updated.enabled is True
    assert reopened.list_connectors(kind="runtime") == (updated,)
    assert reopened.list_connectors(kind="notification") == ()

    with pytest.raises(RevisionConflict) as error:
        reopened.update_connector(
            connector_id="hermes-runtime",
            adapter_id="hermes-v3",
            display_name="Hermes Runtime",
            credential_alias="private_provider",
            enabled=False,
            expected_revision=0,
            actor_id="operator",
            idempotency_key=_key("connector-stale"),
        )
    assert error.value.current == {"id": "hermes-runtime", "revision": 1}
    assert "private_provider" not in str(error.value)
    assert reopened.get_connector("hermes-runtime") == updated


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("connector_id", "Hermes"),
        ("kind", "runtime/value"),
        ("adapter_id", "hermes v2"),
        ("display_name", "Hermes\nRuntime"),
        ("display_name", "Hermes Runtime\n"),
        ("display_name", "\tHermes Runtime"),
        ("display_name", "Hermes Runtime\u0085"),
        ("credential_alias", "token"),
        ("credential_alias", "runtime_secret"),
        ("credential_alias", "https://vault.example/credential"),
        ("credential_alias", "/private/provider"),
        ("credential_alias", "provider=value"),
        ("credential_alias", {"alias": "runtime_provider"}),
        ("enabled", 1),
    ],
)
def test_connector_validation_precedes_registry_and_receipt_writes(
    store: ControlStore, field: str, invalid: object
) -> None:
    arguments: dict[str, object] = {
        "connector_id": "hermes-runtime",
        "kind": "runtime",
        "adapter_id": "hermes",
        "display_name": "Hermes Runtime",
        "credential_alias": "runtime_provider",
        "enabled": False,
        "actor_id": "operator",
        "idempotency_key": _key(
            f"invalid-connector-{field}-{type(invalid).__name__}"
        ),
    }
    arguments[field] = invalid

    with pytest.raises((TypeError, ValueError)) as error:
        store.register_connector(**arguments)
    assert str(invalid) not in str(error.value)

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM connectors").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM idempotency_receipts "
            "WHERE actor_id = 'operator' AND operation LIKE 'INTERNAL:connectors/%'"
        ).fetchone() == (0,)


def _operations_proof(
    tmp_path: Path,
) -> tuple[
    ControlStore,
    control_module.AssetRootRecord,
    control_module.PairedBackupProofInput,
]:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    value = ControlStore(tmp_path / "control.db", clock=lambda: now)
    value.initialize()
    root = value.register_asset_root(
        root_id="artifacts",
        private_path=tmp_path / "assets",
        max_bytes=10_000,
        enabled=True,
        actor_id="operator",
        idempotency_key=_key("proof-root"),
    )
    identity = value.current_control_store_identity()
    manifest = control_module.ProtectedSetManifest(
        schema_version=1,
        control_store=control_module.ControlStoreSnapshot(
            schema_version=identity.schema_version,
            schema_fingerprint_sha256=identity.schema_fingerprint_sha256,
            database_sha256="b" * 64,
            database_byte_length=4_096,
            identity_companion_sha256=identity.identity_companion_sha256,
            identity_companion_byte_length=identity.identity_companion_byte_length,
        ),
        logical_snapshots=(),
        asset_roots=(
            control_module.ProtectedAssetRootSnapshot(
                root_id=root.root_id,
                revision=root.revision,
                content_manifest_sha256="c" * 64,
                file_count=3,
                byte_length=1_024,
            ),
        ),
    )
    digest = control_module.protected_set_digest(manifest)
    proof = control_module.PairedBackupProofInput(
        proof_id="backup-proof-20260727",
        protected_set_manifest=manifest,
        primary=control_module.BackupCopyProof(
            digest,
            datetime(2026, 7, 27, 3, 15, tzinfo=UTC),
            7,
            True,
        ),
        independent=control_module.BackupCopyProof(
            digest,
            datetime(2026, 7, 27, 6, 0, tzinfo=UTC),
            7,
            True,
        ),
        restore=control_module.RestoreVerification(
            digest,
            datetime(2026, 7, 27, 7, 0, tzinfo=UTC),
            8,
            22,
            True,
        ),
    )
    return value, root, proof


def test_paired_backup_proof_is_matched_durable_and_resource_idempotent(
    tmp_path: Path,
) -> None:
    store, _, proof = _operations_proof(tmp_path)
    recorded = store.record_paired_backup_proof(
        proof=proof,
        actor_id="backup-adapter",
        idempotency_key=_key("backup-proof"),
    )
    assert recorded.backup_set_digest == control_module.protected_set_digest(
        proof.protected_set_manifest
    )
    assert recorded.protected_set_manifest == proof.protected_set_manifest
    assert recorded.restored_database_count == 8

    reopened = ControlStore(
        store.path, clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    )
    reopened.initialize()
    assert reopened.get_paired_backup_proof(recorded.id) == recorded
    assert reopened.record_paired_backup_proof(
        proof=proof,
        actor_id="backup-adapter",
        idempotency_key=_key("backup-proof-second-key"),
    ) == recorded
    assert reopened.list_paired_backup_proofs() == (recorded,)

    later_proof = replace(
        proof,
        proof_id="backup-proof-later",
        restore=replace(
            proof.restore,
            completed_at=proof.restore.completed_at + timedelta(microseconds=1),
        ),
    )
    later = reopened.record_paired_backup_proof(
        proof=later_proof,
        actor_id="backup-adapter",
        idempotency_key=_key("backup-proof-later"),
    )
    assert reopened.list_paired_backup_proofs() == (later, recorded)

    drifted = replace(
        proof,
        independent=replace(
            proof.independent, snapshot_count=proof.independent.snapshot_count + 1
        ),
    )
    with pytest.raises(IdempotencyConflict):
        reopened.record_paired_backup_proof(
            proof=drifted,
            actor_id="backup-adapter",
            idempotency_key=_key("backup-proof-third-key"),
        )
    with pytest.raises(IdempotencyConflict):
        reopened.record_paired_backup_proof(
            proof=drifted,
            actor_id="backup-adapter",
            idempotency_key=_key("backup-proof"),
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paired_backup_proofs").fetchone() == (
            2,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM idempotency_receipts "
            "WHERE idempotency_key = ?",
            (_key("backup-proof-third-key"),),
        ).fetchone() == (0,)


def test_paired_backup_proof_rejects_stale_coverage_and_future_evidence(
    tmp_path: Path,
) -> None:
    store, root, proof = _operations_proof(tmp_path)
    future = replace(
        proof,
        proof_id="backup-proof-future",
        primary=replace(
            proof.primary,
            completed_at=datetime(2026, 7, 27, 8, 0, 0, 1, tzinfo=UTC),
        ),
        restore=replace(
            proof.restore,
            completed_at=datetime(2026, 7, 27, 8, 0, 0, 1, tzinfo=UTC),
        ),
    )
    with pytest.raises(ValueError, match="future"):
        store.record_paired_backup_proof(
            proof=future,
            actor_id="backup-adapter",
            idempotency_key=_key("backup-proof-future"),
        )

    store.update_asset_root(
        root_id=root.root_id,
        private_path=root.private_path,
        max_bytes=root.max_bytes,
        enabled=True,
        expected_revision=root.revision,
        actor_id="operator",
        idempotency_key=_key("proof-root-update"),
    )
    with pytest.raises(ValueError, match="coverage"):
        store.record_paired_backup_proof(
            proof=replace(proof, proof_id="backup-proof-stale-root"),
            actor_id="backup-adapter",
            idempotency_key=_key("backup-proof-stale-root"),
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paired_backup_proofs").fetchone() == (
            0,
        )


def test_system_health_observation_is_sanitized_and_resource_idempotent(
    tmp_path: Path,
) -> None:
    store, _, proof = _operations_proof(tmp_path)
    recorded = store.record_paired_backup_proof(
        proof=proof,
        actor_id="backup-adapter",
        idempotency_key=_key("backup-proof"),
    )
    observation = control_module.SystemHealthObservationInput(
        observation_id="health-backup-20260727",
        subject=control_module.HealthSubject("backup_proof", recorded.id),
        status="ok",
        category="backup.verified",
        observed_at=datetime(2026, 7, 27, 7, 1, tzinfo=UTC),
        metrics=(
            ("database_count", 8),
            ("independent_copy_ok", True),
            ("verified_sample_count", 22),
        ),
    )
    saved = store.record_system_health_observation(
        observation=observation,
        actor_id="backup-adapter",
        idempotency_key=_key("health-backup"),
    )
    assert store.latest_system_health_observation(subject=observation.subject) == saved
    assert store.record_system_health_observation(
        observation=observation,
        actor_id="backup-adapter",
        idempotency_key=_key("health-backup-second-key"),
    ) == saved

    later_observation = replace(
        observation,
        observation_id="health-backup-later",
        observed_at=observation.observed_at + timedelta(microseconds=1),
    )
    later = store.record_system_health_observation(
        observation=later_observation,
        actor_id="backup-adapter",
        idempotency_key=_key("health-backup-later"),
    )
    assert (
        store.latest_system_health_observation(subject=observation.subject) == later
    )

    with pytest.raises(IdempotencyConflict):
        store.record_system_health_observation(
            observation=replace(observation, status="degraded"),
            actor_id="backup-adapter",
            idempotency_key=_key("health-backup-third-key"),
        )
    with pytest.raises(ValueError, match="future"):
        store.record_system_health_observation(
            observation=replace(
                observation,
                observation_id="health-backup-future",
                observed_at=datetime(2026, 7, 27, 8, 0, 0, 1, tzinfo=UTC),
            ),
            actor_id="backup-adapter",
            idempotency_key=_key("health-backup-future"),
        )
    assert store.latest_system_health_observation(subject=observation.subject) == later
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM system_health_observations"
        ).fetchone() == (2,)
        assert conn.execute(
            """SELECT metrics_json FROM system_health_observations
               ORDER BY id LIMIT 1"""
        ).fetchone() == (
            (
                '{"database_count":8,"independent_copy_ok":true,'
                '"verified_sample_count":22}'
            ),
        )


@pytest.mark.parametrize(
    "metrics",
    [
        (("path_count", 1),),
        (("database_count", "/private/control.db"),),
        (("database_count", -1),),
        (("database_count", (8,)),),
        (("verified_sample_count", 22), ("database_count", 8)),
    ],
)
def test_system_health_observation_rejects_unsafe_metrics_before_write(
    tmp_path: Path, metrics: object
) -> None:
    store, _, proof = _operations_proof(tmp_path)
    recorded = store.record_paired_backup_proof(
        proof=proof,
        actor_id="backup-adapter",
        idempotency_key=_key("backup-proof"),
    )
    observation = control_module.SystemHealthObservationInput(
        observation_id="health-invalid",
        subject=control_module.HealthSubject("backup_proof", recorded.id),
        status="ok",
        category="backup.verified",
        observed_at=datetime(2026, 7, 27, 7, 1, tzinfo=UTC),
        metrics=metrics,
    )

    with pytest.raises((TypeError, ValueError)):
        store.record_system_health_observation(
            observation=observation,
            actor_id="backup-adapter",
            idempotency_key=_key("health-invalid"),
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM system_health_observations"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM idempotency_receipts "
            "WHERE idempotency_key = ?",
            (_key("health-invalid"),),
        ).fetchone() == (0,)


def test_workspace_idempotency_replays_and_rejects_payload_drift(store: ControlStore) -> None:
    first = store.create_workspace(
        title="Research", actor_id="local", idempotency_key=_key("workspace")
    )
    replay = store.create_workspace(
        title="Research", actor_id="local", idempotency_key=_key("workspace")
    )
    assert replay.replayed is True
    assert replay.value == first.value

    with pytest.raises(IdempotencyConflict):
        store.create_workspace(
            title="Different", actor_id="local", idempotency_key=_key("workspace")
        )


def test_thread_and_message_require_current_parent_revision(store: ControlStore) -> None:
    workspace = _workspace(store)
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Echo",
        expected_revision=0,
        actor_id="local",
        idempotency_key=_key("thread"),
    ).value
    assert store.get_workspace(workspace["id"])["revision"] == 1
    with pytest.raises(RevisionConflict):
        store.create_thread(
            workspace_id=workspace["id"],
            title="Stale",
            expected_revision=0,
            actor_id="local",
            idempotency_key=_key("stale-thread"),
        )

    first = store.append_message(
        thread_id=thread["id"],
        role="user",
        content="Research Echo-Infinity",
        expected_revision=0,
        actor_id="local",
        idempotency_key=_key("message-one"),
    ).value
    second = store.append_message(
        thread_id=thread["id"],
        role="assistant",
        content="Source conflict requires a decision.",
        expected_revision=1,
        actor_id="runtime",
        idempotency_key=_key("message-two"),
    ).value
    assert (first["position"], second["position"]) == (1, 2)
    assert [item["content"] for item in store.list_messages(thread["id"])] == [
        "Research Echo-Infinity",
        "Source conflict requires a decision.",
    ]


def test_run_transitions_commit_monotonic_events_and_release_thread(store: ControlStore) -> None:
    run = _running_run(store)
    completed = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="completed",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("completed"),
        payload={"artifact_id": "artifact-1"},
    ).value
    assert completed["latest_sequence"] == 6
    assert store.get_thread(completed["thread_id"])["active_run_id"] is None
    events = store.list_run_events(run["id"])
    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert [event["type"] for event in events] == [
        "run.queued",
        "runtime.dispatch.reserved",
        "runtime.bound",
        "run.starting",
        "run.running",
        "run.completed",
    ]
    assert [event["cursor"] for event in store.list_events()] == [1, 2, 3, 4, 5, 6]


def test_one_active_run_and_duplicate_concurrent_create(store: ControlStore) -> None:
    thread = _thread(store)

    def create() -> dict:
        return store.create_run(
            thread_id=thread["id"],
            expected_revision=0,
            actor_id="local",
            idempotency_key=_key("same-run"),
        ).value

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: create(), range(2)))
    assert values[0]["id"] == values[1]["id"]
    assert len(store.list_run_events(values[0]["id"])) == 1

    current_thread = store.get_thread(thread["id"])
    with pytest.raises(InvalidTransition):
        store.create_run(
            thread_id=thread["id"],
            expected_revision=current_thread["revision"],
            actor_id="local",
            idempotency_key=_key("another-run"),
        )


def test_decision_compare_and_set_has_one_winner(store: ControlStore) -> None:
    run = _running_run(store)
    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=run["revision"],
        kind="source_conflict",
        prompt="Keep both sources?",
        options=[{"id": "keep_both"}, {"id": "cancel"}],
        actor_id="runtime",
        idempotency_key=_key("decision"),
    ).value

    def resolve(choice: str) -> str:
        try:
            result = store.resolve_decision(
                decision_id=decision["id"],
                choice=choice,
                expected_revision=0,
                actor_id=choice,
                idempotency_key=_key(choice),
            )
            return str(result.value["resolution"]["choice"])
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = set(pool.map(resolve, ["keep_both", "cancel"]))
    assert "conflict" in outcomes
    assert len(outcomes) == 2
    committed = store.get_decision(decision["id"])
    assert committed["state"] == "resolved"
    assert committed["revision"] == 1
    assert store.get_run(run["id"])["state"] == "resuming"
    pending = store.list_pending_runtime_actions()
    assert len(pending) == 1
    assert pending[0]["payload"]["decision_id"] == decision["id"]
    assert [event["type"] for event in store.list_run_events(run["id"])][-2:] == [
        "decision.required",
        "decision.resolved",
    ]
    reopened = ControlStore(
        store.path, clock=lambda: datetime(2026, 7, 23, 12, 1, tzinfo=UTC)
    )
    reopened.initialize()
    assert reopened.list_pending_runtime_actions() == pending
    claimed = reopened.claim_runtime_action(
        action_id=pending[0]["id"],
        worker_id="decision-worker",
        lease_seconds=30,
        actor_id="runtime",
        idempotency_key=_key("decision-claim"),
    ).value
    current = reopened.get_run(run["id"])
    acked = reopened.acknowledge_runtime_action(
        action_id=pending[0]["id"],
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        expected_revision=current["revision"],
        actor_id="runtime",
        idempotency_key=_key("decision-ack"),
        state_generation_id=run["_generation_id"],
        claim_owner="decision-worker",
        claim_epoch=claimed["claim_epoch"],
    ).value
    assert acked["state"] == "acked"
    assert reopened.get_run(run["id"])["state"] == "running"
    assert reopened.list_pending_runtime_actions() == []


def test_runtime_action_claim_has_one_owner_and_ack_requires_it(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=run["revision"],
        kind="approval",
        prompt="Approve?",
        options=[{"id": "approve_once"}, {"id": "deny"}],
        actor_id="runtime",
        idempotency_key=_key("claim-decision"),
    ).value
    store.resolve_decision(
        decision_id=decision["id"],
        choice="approve_once",
        expected_revision=decision["revision"],
        actor_id="local",
        idempotency_key=_key("claim-resolve"),
    )
    action = store.list_pending_runtime_actions()[0]

    claimed_epochs: dict[str, int] = {}

    def claim(worker: str) -> str:
        try:
            value = store.claim_runtime_action(
                action_id=action["id"],
                worker_id=worker,
                lease_seconds=30,
                actor_id=worker,
                idempotency_key=_key(worker),
            ).value
            claimed_epochs[worker] = value["claim_epoch"]
            return str(value["claim_owner"])
        except InvalidTransition:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = set(pool.map(claim, ["worker-one", "worker-two"]))
    assert "conflict" in outcomes
    owner = next(value for value in outcomes if value != "conflict")
    current = store.get_run(run["id"])
    with pytest.raises(InvalidTransition, match="claim_fence_mismatch"):
        store.acknowledge_runtime_action(
            action_id=action["id"],
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            expected_revision=current["revision"],
            actor_id="runtime",
            idempotency_key=_key("wrong-claim-owner"),
            claim_owner="wrong-worker",
            claim_epoch=action["claim_epoch"] + 1,
        )
    acked = store.acknowledge_runtime_action(
        action_id=action["id"],
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=current["revision"],
        actor_id="runtime",
        idempotency_key=_key("right-claim-owner"),
        claim_owner=owner,
        claim_epoch=claimed_epochs[owner],
    ).value
    assert acked["state"] == "acked"


def test_terminal_failure_settles_pending_runtime_action(store: ControlStore) -> None:
    run = _running_run(store)
    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=run["revision"],
        kind="source_conflict",
        prompt="Keep both sources?",
        options=[{"id": "keep_both"}, {"id": "cancel"}],
        actor_id="runtime",
        idempotency_key=_key("orphan-decision"),
    ).value
    store.resolve_decision(
        decision_id=decision["id"],
        choice="keep_both",
        expected_revision=decision["revision"],
        actor_id="local",
        idempotency_key=_key("orphan-resolve"),
    )
    action = store.list_pending_runtime_actions()[0]
    current = store.get_run(run["id"])
    failed = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="failed",
        expected_revision=current["revision"],
        actor_id="runtime",
        idempotency_key=_key("orphan-failed"),
    ).value

    assert store.list_pending_runtime_actions() == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state FROM runtime_actions WHERE id = ?", (action["id"],)
        ).fetchone() == ("failed",)
    retry = store.retry_run(
        run_id=run["id"],
        expected_revision=failed["revision"],
        actor_id="local",
        idempotency_key=_key("orphan-retry"),
        reason="runtime action failed",
    ).value
    assert retry["attempt"]["number"] == 2


def test_event_failure_rolls_back_state_and_receipt(store: ControlStore) -> None:
    run = _running_run(store)
    original = store._insert_event

    def fail(*args, **kwargs):
        raise RuntimeError("injected event failure")

    store._insert_event = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected"):
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            target_state="completed",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=_key("atomic-failure"),
        )
    store._insert_event = original  # type: ignore[method-assign]
    assert store.get_run(run["id"])["state"] == "running"
    succeeded = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="completed",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("atomic-failure"),
    )
    assert succeeded.value["state"] == "completed"


def test_runtime_result_rejects_wrong_attempt_binding_or_release(store: ControlStore) -> None:
    run = _running_run(store)
    before = store.list_run_events(run["id"])
    with pytest.raises(InvalidTransition, match="runtime_binding_mismatch"):
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id="wrong-release",
            state_generation_id=run["_generation_id"],
            target_state="completed",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=_key("wrong-runtime-result"),
        )
    assert store.get_run(run["id"])["state"] == "running"
    assert store.list_run_events(run["id"]) == before


def test_managed_attempt_requires_exact_state_generation(store: ControlStore) -> None:
    thread = _thread(store)
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("managed-run"),
    ).value
    binding = store.create_runtime_binding(
        thread_id=thread["id"],
        adapter_id="hermes",
        runtime_session_ref="managed-session",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key=_key("managed-binding"),
    ).value
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        dispatch_owner="managed-worker",
        runtime_release_id="release-one",
        state_generation_id="generation-one",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("managed-reserve"),
    ).value
    pinned = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        runtime_binding_id=binding["id"],
        runtime_release_id="release-one",
        state_generation_id="generation-one",
        dispatch_owner="managed-worker",
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("managed-pin"),
    ).value

    with pytest.raises(InvalidTransition, match="runtime_binding_mismatch"):
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            runtime_binding_id=binding["id"],
            runtime_release_id="release-one",
            state_generation_id="wrong-generation",
            target_state="starting",
            expected_revision=pinned["revision"],
            actor_id="runtime",
            idempotency_key=_key("wrong-generation"),
        )
    started = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        runtime_binding_id=binding["id"],
        runtime_release_id="release-one",
        state_generation_id="generation-one",
        target_state="starting",
        expected_revision=pinned["revision"],
        actor_id="runtime",
        idempotency_key=_key("right-generation"),
    ).value
    assert started["state"] == "starting"


def test_resume_and_retry_create_new_attempts(store: ControlStore) -> None:
    run = _running_run(store)
    pause_requested = store.transition_run(
        run_id=run["id"],
        target_state="pause_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key=_key("pause-requested"),
    ).value
    checkpointed = store.commit_checkpoint(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        checkpoint_uri="cortex://artifacts/checkpoints/run-1.json",
        expected_revision=pause_requested["revision"],
        actor_id="runtime",
        idempotency_key=_key("checkpoint"),
    ).value
    paused = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="paused",
        expected_revision=checkpointed["revision"],
        actor_id="runtime",
        idempotency_key=_key("paused"),
    ).value
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key=_key("resume"),
    ).value
    assert resumed["attempt"]["number"] == 2
    assert resumed["attempt"]["runtime_release_id"] is None
    assert resumed["attempt"]["source_attempt_id"] == run["_attempt_id"]
    assert resumed["attempt"]["source_checkpoint_uri"].startswith("cortex://")

    resumed = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=resumed["attempt"]["id"],
        dispatch_owner="resume-worker",
        runtime_release_id=run["_release_id"],
        state_generation_id="state-resume",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=resumed["revision"],
        actor_id="runtime",
        idempotency_key=_key("resume-reserve"),
    ).value
    pinned = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=resumed["attempt"]["id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id="state-resume",
        dispatch_owner="resume-worker",
        expected_revision=resumed["revision"],
        actor_id="runtime",
        idempotency_key=_key("resume-pin"),
    ).value

    running = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=resumed["attempt"]["id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id="state-resume",
        target_state="running",
        expected_revision=pinned["revision"],
        actor_id="runtime",
        idempotency_key=_key("resumed-running"),
    ).value
    failed = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=resumed["attempt"]["id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id="state-resume",
        target_state="failed",
        expected_revision=running["revision"],
        actor_id="runtime",
        idempotency_key=_key("failed"),
    ).value
    retried = store.retry_run(
        run_id=run["id"],
        expected_revision=failed["revision"],
        actor_id="local",
        idempotency_key=_key("retry"),
        reason="provider exhausted",
    ).value
    assert retried["attempt"]["number"] == 3
    assert retried["attempt"]["source_checkpoint_uri"].startswith("cortex://")
    assert store.get_thread(run["thread_id"])["active_run_id"] == run["id"]


def test_retry_after_unbound_resume_releases_paused_ancestor_pin(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    pause_requested = store.transition_run(
        run_id=run["id"],
        target_state="pause_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key=_key("ancestor-pause-requested"),
    ).value
    checkpointed = store.commit_checkpoint(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        checkpoint_uri="cortex://artifacts/checkpoints/ancestor.json",
        expected_revision=pause_requested["revision"],
        actor_id="runtime",
        idempotency_key=_key("ancestor-checkpoint"),
    ).value
    paused = store.apply_runtime_transition(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        target_state="paused",
        expected_revision=checkpointed["revision"],
        actor_id="runtime",
        idempotency_key=_key("ancestor-paused"),
    ).value
    resumed = store.resume_run(
        run_id=run["id"],
        expected_revision=paused["revision"],
        actor_id="local",
        idempotency_key=_key("ancestor-resume"),
    ).value
    resumed_attempt_id = resumed["attempt"]["id"]
    resumed = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=resumed_attempt_id,
        dispatch_owner="crashed-resume-worker",
        runtime_release_id=run["_release_id"],
        state_generation_id="resume-generation",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=resumed["revision"],
        actor_id="runtime",
        idempotency_key=_key("ancestor-reserve-resume"),
    ).value
    failed = store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=resumed_attempt_id,
        expected_revision=resumed["revision"],
        category="runtime_unavailable",
        actor_id="runtime",
        idempotency_key=_key("ancestor-resume-failed"),
        dispatch_owner="crashed-resume-worker",
    ).value
    assert failed["state"] == "paused"
    assert failed["active_attempt_id"] == run["_attempt_id"]
    assert {
        release["attempt_id"] for release in store.list_pending_pin_releases()
    } == {resumed_attempt_id}

    retried = store.resume_run(
        run_id=run["id"],
        expected_revision=failed["revision"],
        actor_id="local",
        idempotency_key=_key("ancestor-resume-again"),
    ).value
    retry_attempt_id = retried["attempt"]["id"]
    assert retried["attempt"]["number"] == 3
    assert retried["attempt"]["source_checkpoint_uri"].endswith("ancestor.json")
    retried = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=retry_attempt_id,
        dispatch_owner="retry-worker",
        runtime_release_id=run["_release_id"],
        state_generation_id="retry-generation",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=retried["revision"],
        actor_id="runtime",
        idempotency_key=_key("ancestor-reserve-retry"),
    ).value
    store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=retry_attempt_id,
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id="retry-generation",
        dispatch_owner="retry-worker",
        expected_revision=retried["revision"],
        actor_id="runtime",
        idempotency_key=_key("ancestor-pin-retry"),
    )

    assert {
        release["attempt_id"] for release in store.list_pending_pin_releases()
    } == {run["_attempt_id"], resumed_attempt_id}


def test_transport_binding_uses_opaque_scope_and_survives_restart(store: ControlStore) -> None:
    thread = _thread(store)
    binding = store.bind_transport(
        transport="telegram",
        external_scope="bot:123:topic:456",
        thread_id=thread["id"],
        actor_id="adapter",
        idempotency_key=_key("binding"),
    ).value
    assert binding["external_scope"].startswith("hmac-sha256:")
    assert binding["external_scope"] != "bot:123:topic:456"
    assert store.resolve_transport(
        transport="telegram", external_scope="bot:123:topic:456"
    ) == binding

    reopened = ControlStore(store.path)
    reopened.initialize()
    assert reopened.resolve_transport(
        transport="telegram", external_scope="bot:123:topic:456"
    )["thread_id"] == thread["id"]

    repeated = reopened.bind_transport(
        transport="telegram",
        external_scope="bot:123:topic:456",
        thread_id=thread["id"],
        actor_id="adapter",
        idempotency_key=_key("binding-again"),
    )
    assert repeated.status_code == 200
    assert repeated.value == binding

    workspace = store.create_workspace(
        title="Other", actor_id="local", idempotency_key=_key("other-workspace")
    ).value
    other = store.create_thread(
        workspace_id=workspace["id"],
        title="Other",
        expected_revision=0,
        actor_id="local",
        idempotency_key=_key("other-thread"),
    ).value
    with pytest.raises(TransportBindingConflict):
        store.bind_transport(
            transport="telegram",
            external_scope="bot:123:topic:456",
            thread_id=other["id"],
            actor_id="adapter",
            idempotency_key=_key("binding-conflict"),
        )


def test_transport_command_receipt_replays_sanitized_result_after_restart(
    store: ControlStore,
) -> None:
    request_hash = "a" * 64
    response = control_module.TransportCommandResponse(
        ok=True,
        category="ok",
        action="capture_message",
        response_text="Captured in the bound Cortex thread.",
        mutated=True,
    )
    receipt = store.record_transport_command(
        transport="telegram",
        command_key=_key("tg-inbound"),
        request_hash=request_hash,
        response=response,
    )
    assert receipt.response == response
    with pytest.raises(TypeError, match="TransportCommandResponse"):
        store.record_transport_command(
            transport="telegram",
            command_key=_key("unsafe-result"),
            request_hash="d" * 64,
            response={"secret": "raw-provider-payload"},  # type: ignore[arg-type]
        )

    reopened = ControlStore(store.path)
    reopened.initialize()
    replay = reopened.get_transport_command(
        transport="telegram",
        command_key=_key("tg-inbound"),
        request_hash=request_hash,
    )
    assert replay == receipt
    with pytest.raises(IdempotencyConflict):
        reopened.get_transport_command(
            transport="telegram",
            command_key=_key("tg-inbound"),
            request_hash="b" * 64,
        )


def test_transport_delivery_expired_lease_reclaims_with_a_new_fence(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 23, 12, 0, tzinfo=UTC)]
    database = tmp_path / "control.db"
    store = ControlStore(database, clock=lambda: now[0])
    store.initialize()
    key = control_module.TransportDeliveryKey(
        transport="telegram",
        destination_digest="hmac-sha256:" + "d" * 64,
        event_id="event-1",
        projection_version=1,
    )
    with pytest.raises(ValueError, match="destination_digest"):
        store.claim_transport_delivery(
            key=control_module.TransportDeliveryKey(
                transport="telegram",
                destination_digest="raw-chat:-100:topic:41",
                event_id="event-unsafe",
                projection_version=1,
            ),
            worker_id="worker-one",
            lease_seconds=30,
        )
    first = store.claim_transport_delivery(
        key=key,
        worker_id="worker-one",
        lease_seconds=30,
    )
    assert first.status == "claimed" and first.claim_epoch == 1

    now[0] += timedelta(seconds=31)
    reopened = ControlStore(database, clock=lambda: now[0])
    reopened.initialize()
    second = reopened.claim_transport_delivery(
        key=key,
        worker_id="worker-two",
        lease_seconds=30,
    )
    assert second.status == "claimed" and second.claim_epoch == 2
    with pytest.raises(InvalidTransition, match="stale_claim"):
        reopened.complete_transport_delivery(
            key=key,
            worker_id="worker-one",
            claim_epoch=first.claim_epoch,
        )
    reopened.complete_transport_delivery(
        key=key,
        worker_id="worker-two",
        claim_epoch=second.claim_epoch,
    )
    delivered = reopened.claim_transport_delivery(
        key=key,
        worker_id="worker-three",
        lease_seconds=30,
    )
    assert delivered.status == "delivered"


def test_transport_delivery_projection_freezes_atomically_and_replays_exact_bytes(
    store: ControlStore,
) -> None:
    projection, targets = _frozen_delivery()

    frozen = store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )
    assert frozen == control_module.TransportDeliveryFreezeResult(
        disposition="frozen",
        projection=projection,
    )

    reopened = ControlStore(store.path)
    reopened.initialize()
    replay = reopened.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )
    assert replay == control_module.TransportDeliveryFreezeResult(
        disposition="replayed",
        projection=projection,
    )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_projections"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_chunks"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_chunk_buttons"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_chunk_capabilities"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_opaque_targets"
        ).fetchone() == (2,)


def test_transport_delivery_preserves_opaque_target_hash_order(
    store: ControlStore,
) -> None:
    projection, targets = _frozen_delivery()
    reversed_targets = tuple(reversed(targets))
    projection = replace(
        projection,
        projection_hash=control_module.transport_delivery_projection_hash(
            projection,
            opaque_targets=reversed_targets,
        ),
    )
    frozen = store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=reversed_targets,
        request_hash="f" * 64,
    )
    assert frozen.projection == projection

    reopened = ControlStore(store.path)
    reopened.initialize()
    replayed = reopened.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=reversed_targets,
        request_hash="f" * 64,
    )
    assert replayed.projection == projection
    claim = reopened.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=30,
    )
    decision = reopened.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        claim_epoch=claim.claim_epoch,
        expected_revision=claim.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert decision.status == "send_permitted"


def test_transport_delivery_projection_rejects_request_projection_and_target_drift(
    store: ControlStore,
) -> None:
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )

    with pytest.raises(IdempotencyConflict):
        store.freeze_transport_delivery_projection(
            projection=projection,
            opaque_targets=targets,
            request_hash="e" * 64,
        )
    changed_chunk = replace(
        projection.chunks[0],
        text="Changed first chunk",
    )
    changed_chunk = replace(
        changed_chunk,
        chunk_hash=control_module.transport_delivery_chunk_hash(changed_chunk),
    )
    changed_projection = replace(
        projection,
        chunks=(changed_chunk, projection.chunks[1]),
    )
    changed_projection = replace(
        changed_projection,
        projection_hash=control_module.transport_delivery_projection_hash(
            changed_projection,
            opaque_targets=targets,
        ),
    )
    with pytest.raises(InvalidTransition, match="projection_drift"):
        store.freeze_transport_delivery_projection(
            projection=changed_projection,
            opaque_targets=targets,
            request_hash="f" * 64,
        )

    other, other_targets = _frozen_delivery(
        event_id="event-target-collision",
        operation_id="telegram-delivery-target-collision",
        token_suffix="collision",
    )
    store.put_transport_opaque_target(
        token_digest=other_targets[1].token_digest,
        target=replace(other_targets[1].target, resource_id="other-run"),
    )
    with pytest.raises(InvalidTransition, match="token_collision"):
        store.freeze_transport_delivery_projection(
            projection=other,
            opaque_targets=other_targets,
            request_hash="c" * 64,
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_projections "
            "WHERE event_id = 'event-target-collision'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_opaque_targets"
        ).fetchone() == (3,)


def test_transport_delivery_frozen_records_reject_direct_sql_mutation(
    store: ControlStore,
) -> None:
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )

    immutable_cases = (
        (
            "UPDATE transport_delivery_projections "
            "SET projection_hash = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'",
            "immutable",
        ),
        ("DELETE FROM transport_delivery_projections", "deletion"),
        (
            "INSERT OR REPLACE INTO transport_delivery_projections "
            "SELECT * FROM transport_delivery_projections",
            "replacement",
        ),
        (
            """INSERT OR REPLACE INTO transport_delivery_projections
               SELECT transport, destination_digest, 'replacement-event',
                      projection_version, operation_id, request_hash,
                      projection_hash, destination_binding_digest, routing,
                      capability_binding_digest, rpc_timeout_seconds,
                      chunk_count, state, revision, created_at, completed_at
               FROM transport_delivery_projections""",
            "replacement",
        ),
        (
            "UPDATE transport_delivery_chunks SET text = 'tampered' "
            "WHERE chunk_index = 0",
            "immutable",
        ),
        ("DELETE FROM transport_delivery_chunks WHERE chunk_index = 0", "deletion"),
        (
            "INSERT OR REPLACE INTO transport_delivery_chunks "
            "SELECT * FROM transport_delivery_chunks WHERE chunk_index = 0",
            "replacement",
        ),
        (
            """INSERT OR REPLACE INTO transport_delivery_chunks
               SELECT transport, destination_digest, event_id,
                      projection_version, 2, operation_id, text, parse_mode,
                      chunk_hash, state, revision, claim_owner, claim_epoch,
                      claim_expires_at, retry_not_before,
                      provider_receipt_digest, failure_category,
                      manual_resolution_id, completed_at, transport_window_id
               FROM transport_delivery_chunks WHERE chunk_index = 0""",
            "replacement",
        ),
        (
            "UPDATE transport_delivery_chunk_buttons SET label = 'Changed'",
            "immutable",
        ),
        ("DELETE FROM transport_delivery_chunk_buttons", "deletion"),
        (
            "INSERT OR REPLACE INTO transport_delivery_chunk_buttons "
            "SELECT * FROM transport_delivery_chunk_buttons",
            "replacement",
        ),
        (
            "UPDATE transport_delivery_chunk_capabilities "
            "SET token_digest = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'",
            "immutable",
        ),
        ("DELETE FROM transport_delivery_chunk_capabilities", "deletion"),
        (
            "INSERT OR REPLACE INTO transport_delivery_chunk_capabilities "
            "SELECT * FROM transport_delivery_chunk_capabilities WHERE position = 0",
            "replacement",
        ),
        (
            """INSERT OR REPLACE INTO transport_delivery_chunk_capabilities
               SELECT transport, destination_digest, event_id,
                      projection_version, chunk_index, 2, target_position,
                      namespace, token_digest, expires_at, start_offset, end_offset
               FROM transport_delivery_chunk_capabilities WHERE position = 0""",
            "replacement",
        ),
        (
            """INSERT OR REPLACE INTO transport_delivery_chunk_capabilities
               SELECT transport, destination_digest, event_id,
                      projection_version, chunk_index, 2, target_position,
                      namespace,
                      'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                      expires_at, start_offset, end_offset
               FROM transport_delivery_chunk_capabilities WHERE position = 0""",
            "replacement",
        ),
    )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        for statement, message in immutable_cases:
            with pytest.raises(sqlite3.IntegrityError, match=message):
                conn.execute(statement)

    claim = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=30,
    )
    assert claim.status == "claimed"


@pytest.mark.parametrize(
    ("trigger_name", "tamper_statement"),
    [
        (
            "transport_delivery_chunks_frozen_update_guard",
            "UPDATE transport_delivery_chunks SET text = 'Tampered first chunk' "
            "WHERE chunk_index = 0",
        ),
        (
            "transport_delivery_projections_frozen_update_guard",
            "UPDATE transport_delivery_projections "
            "SET projection_hash = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'",
        ),
    ],
)
def test_transport_delivery_replay_and_send_fail_closed_on_frozen_tampering(
    store: ControlStore,
    trigger_name: str,
    tamper_statement: str,
) -> None:
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )
    claim = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=30,
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute(tamper_statement)

    with pytest.raises(InvalidTransition, match="projection_integrity"):
        store.freeze_transport_delivery_projection(
            projection=projection,
            opaque_targets=targets,
            request_hash="f" * 64,
        )
    with pytest.raises(InvalidTransition, match="projection_integrity"):
        store.begin_transport_delivery_chunk_send(
            delivery_key=projection.delivery_key,
            chunk_index=0,
            worker_id="worker-one",
            claim_epoch=claim.claim_epoch,
            expected_revision=claim.revision,
            observed_capability_binding_digest="a" * 64,
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state FROM transport_delivery_chunks WHERE chunk_index = 0"
        ).fetchone() == ("claimed",)


@pytest.mark.parametrize(
    ("legacy_state", "disposition"),
    [
        ("delivered", "already_delivered"),
        ("pending", "legacy_delivery_uncertain"),
        ("claimed", "legacy_delivery_uncertain"),
    ],
)
def test_transport_delivery_projection_never_adopts_legacy_rows(
    store: ControlStore,
    legacy_state: str,
    disposition: str,
) -> None:
    projection, targets = _frozen_delivery(
        event_id=f"legacy-{legacy_state}",
        digest_character={"delivered": "a", "pending": "b", "claimed": "c"}[
            legacy_state
        ],
        operation_id=f"telegram-delivery-legacy-{legacy_state}",
    )
    key = projection.delivery_key
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """INSERT INTO transport_deliveries(
                   transport, destination_digest, event_id, projection_version,
                   state, claim_owner, claim_epoch, claim_expires_at,
                   created_at, delivered_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                key.transport,
                key.destination_digest,
                key.event_id,
                key.projection_version,
                legacy_state,
                "legacy-worker" if legacy_state == "claimed" else None,
                1 if legacy_state != "pending" else 0,
                "future" if legacy_state == "claimed" else None,
                "old",
                "old" if legacy_state == "delivered" else None,
            ),
        )

    result = store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )
    assert result == control_module.TransportDeliveryFreezeResult(
        disposition=disposition,  # type: ignore[arg-type]
        projection=None,
    )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_delivery_projections"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_opaque_targets"
        ).fetchone() == (0,)


def test_legacy_delivery_claim_never_downgrades_a_frozen_projection(
    store: ControlStore,
) -> None:
    projection, targets = _frozen_delivery(
        event_id="m10-before-legacy",
        operation_id="telegram-delivery-m10-before-legacy",
    )
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )

    pending = store.claim_transport_delivery(
        key=projection.delivery_key,
        worker_id="legacy-worker",
        lease_seconds=30,
    )
    assert pending.status == "in_flight"
    for chunk_index in range(len(projection.chunks)):
        claim = store.claim_transport_delivery_chunk(
            delivery_key=projection.delivery_key,
            chunk_index=chunk_index,
            worker_id="m10-worker",
            lease_seconds=30,
        )
        decision = store.begin_transport_delivery_chunk_send(
            delivery_key=projection.delivery_key,
            chunk_index=chunk_index,
            worker_id="m10-worker",
            claim_epoch=claim.claim_epoch,
            expected_revision=claim.revision,
            observed_capability_binding_digest="a" * 64,
        )
        assert decision.permit is not None
        store.complete_transport_delivery_chunk(
            delivery_key=projection.delivery_key,
            chunk_index=chunk_index,
            worker_id="m10-worker",
            claim_epoch=decision.permit.claim_epoch,
            expected_revision=decision.permit.revision,
            provider_receipt_digest="hmac-sha256:" + str(chunk_index) * 64,
        )

    delivered = store.claim_transport_delivery(
        key=projection.delivery_key,
        worker_id="legacy-worker",
        lease_seconds=30,
    )
    assert delivered.status == "delivered"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transport_deliveries WHERE event_id = ?",
            (projection.delivery_key.event_id,),
        ).fetchone() == (0,)


def test_transport_delivery_chunk_claims_in_order_and_reclaims_expired_lease(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 23, 12, 0, tzinfo=UTC)]
    store = ControlStore(tmp_path / "control.db", clock=lambda: now[0])
    store.initialize()
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )

    with pytest.raises(InvalidTransition, match="chunk_order"):
        store.claim_transport_delivery_chunk(
            delivery_key=projection.delivery_key,
            chunk_index=1,
            worker_id="worker-two",
            lease_seconds=30,
        )
    first = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=30,
    )
    assert (first.status, first.claim_owner, first.claim_epoch, first.revision) == (
        "claimed",
        "worker-one",
        1,
        1,
    )
    in_flight = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        lease_seconds=30,
    )
    assert in_flight == first

    now[0] += timedelta(seconds=30)
    reclaimed = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        lease_seconds=30,
    )
    assert (
        reclaimed.status,
        reclaimed.claim_owner,
        reclaimed.claim_epoch,
        reclaimed.revision,
    ) == ("claimed", "worker-two", 2, 2)


def test_transport_delivery_begin_fails_before_permit_on_capability_drift_or_expiry(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    store = ControlStore(tmp_path / "control.db", clock=lambda: now)
    store.initialize()

    drifted, drifted_targets = _frozen_delivery(
        event_id="event-drift",
        operation_id="telegram-delivery-drift",
        token_suffix="drift",
    )
    store.freeze_transport_delivery_projection(
        projection=drifted,
        opaque_targets=drifted_targets,
        request_hash="d" * 64,
    )
    drifted_claim = store.claim_transport_delivery_chunk(
        delivery_key=drifted.delivery_key,
        chunk_index=0,
        worker_id="worker-drift",
        lease_seconds=30,
    )
    drift = store.begin_transport_delivery_chunk_send(
        delivery_key=drifted.delivery_key,
        chunk_index=0,
        worker_id="worker-drift",
        claim_epoch=drifted_claim.claim_epoch,
        expected_revision=drifted_claim.revision,
        observed_capability_binding_digest="b" * 64,
    )
    assert drift == control_module.TransportDeliveryChunkSendDecision(
        status="capability_mismatch",
        permit=None,
    )

    expiring, expiring_targets = _frozen_delivery(
        event_id="event-expiring",
        operation_id="telegram-delivery-expiring",
        token_suffix="expiring",
        expires_at=now + timedelta(seconds=30),
    )
    store.freeze_transport_delivery_projection(
        projection=expiring,
        opaque_targets=expiring_targets,
        request_hash="e" * 64,
    )
    first = store.claim_transport_delivery_chunk(
        delivery_key=expiring.delivery_key,
        chunk_index=0,
        worker_id="worker-expiring",
        lease_seconds=30,
    )
    first_decision = store.begin_transport_delivery_chunk_send(
        delivery_key=expiring.delivery_key,
        chunk_index=0,
        worker_id="worker-expiring",
        claim_epoch=first.claim_epoch,
        expected_revision=first.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert first_decision.status == "send_permitted"
    assert first_decision.permit is not None
    store.complete_transport_delivery_chunk(
        delivery_key=expiring.delivery_key,
        chunk_index=0,
        worker_id="worker-expiring",
        claim_epoch=first.claim_epoch,
        expected_revision=first_decision.permit.revision,
        provider_receipt_digest="hmac-sha256:" + "1" * 64,
    )
    final = store.claim_transport_delivery_chunk(
        delivery_key=expiring.delivery_key,
        chunk_index=1,
        worker_id="worker-expiring",
        lease_seconds=30,
    )
    expiry = store.begin_transport_delivery_chunk_send(
        delivery_key=expiring.delivery_key,
        chunk_index=1,
        worker_id="worker-expiring",
        claim_epoch=final.claim_epoch,
        expected_revision=final.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert expiry == control_module.TransportDeliveryChunkSendDecision(
        status="capability_expired",
        permit=None,
    )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state, failure_category FROM transport_delivery_chunks "
            "WHERE event_id = 'event-expiring' AND chunk_index = 1"
        ).fetchone() == ("failed", "capability_expired")


def test_transport_delivery_send_outcomes_persist_retry_and_complete_parent(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 23, 12, 0, tzinfo=UTC)]
    store = ControlStore(tmp_path / "control.db", clock=lambda: now[0])
    store.initialize()
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )

    first = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=30,
    )
    first_decision = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        claim_epoch=first.claim_epoch,
        expected_revision=first.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert first_decision.status == "send_permitted"
    assert first_decision.permit is not None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state, claim_expires_at FROM transport_delivery_chunks "
            "WHERE event_id = 'event-m10' AND chunk_index = 0"
        ).fetchone() == ("sending_unknown", None)
    with pytest.raises(InvalidTransition, match="stale_claim"):
        store.complete_transport_delivery_chunk(
            delivery_key=projection.delivery_key,
            chunk_index=0,
            worker_id="worker-one",
            claim_epoch=first.claim_epoch,
            expected_revision=first_decision.permit.revision - 1,
            provider_receipt_digest="hmac-sha256:" + "1" * 64,
        )
    released = store.release_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        claim_epoch=first.claim_epoch,
        expected_revision=first_decision.permit.revision,
        proof="provider_proved_before_send",
        retry_after_ms=1_000,
    )
    assert released.status == "pending"
    assert released.retry_not_before == now[0] + timedelta(seconds=1)
    deferred = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        lease_seconds=30,
    )
    assert deferred.status == "deferred"
    assert deferred.retry_not_before == released.retry_not_before

    now[0] += timedelta(seconds=1)
    retried = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        lease_seconds=30,
    )
    retried_decision = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        claim_epoch=retried.claim_epoch,
        expected_revision=retried.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert retried_decision.permit is not None
    store.complete_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        claim_epoch=retried.claim_epoch,
        expected_revision=retried_decision.permit.revision,
        provider_receipt_digest="hmac-sha256:" + "2" * 64,
    )

    final = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=1,
        worker_id="worker-two",
        lease_seconds=30,
    )
    final_decision = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=1,
        worker_id="worker-two",
        claim_epoch=final.claim_epoch,
        expected_revision=final.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert final_decision.permit is not None
    delivered = store.complete_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=1,
        worker_id="worker-two",
        claim_epoch=final.claim_epoch,
        expected_revision=final_decision.permit.revision,
        provider_receipt_digest="hmac-sha256:" + "3" * 64,
    )
    assert delivered.status == "delivered"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state, completed_at FROM transport_delivery_projections "
            "WHERE event_id = 'event-m10'"
        ).fetchone() == ("delivered", "2026-07-23T12:00:01.000000Z")


def test_transport_delivery_sending_unknown_requires_audited_manual_resolution(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 7, 23, 12, 0, tzinfo=UTC)]
    store = ControlStore(
        tmp_path / "control.db",
        clock=lambda: now[0],
        id_factory=DeterministicIds(),
    )
    store.initialize()
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )
    claim = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=1,
    )
    decision = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        claim_epoch=claim.claim_epoch,
        expected_revision=claim.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert decision.permit is not None
    now[0] += timedelta(days=1)
    unknown = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        lease_seconds=30,
    )
    assert unknown.status == "sending_unknown"
    assert unknown.claim_owner == "worker-one"

    retried = store.resolve_transport_delivery_chunk_sending_unknown(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        resolution="retry",
        expected_revision=unknown.revision,
        actor_id="operator-one",
        idempotency_key=_key("manual-retry"),
    )
    assert retried.status == "pending"
    assert store.resolve_transport_delivery_chunk_sending_unknown(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        resolution="retry",
        expected_revision=unknown.revision,
        actor_id="operator-one",
        idempotency_key=_key("manual-retry"),
    ) == retried
    with pytest.raises(IdempotencyConflict):
        store.resolve_transport_delivery_chunk_sending_unknown(
            delivery_key=projection.delivery_key,
            chunk_index=0,
            resolution="assume_delivered",
            expected_revision=unknown.revision,
            actor_id="operator-one",
            idempotency_key=_key("manual-retry"),
        )

    claim_again = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-three",
        lease_seconds=30,
    )
    decision_again = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-three",
        claim_epoch=claim_again.claim_epoch,
        expected_revision=claim_again.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert decision_again.permit is not None
    original_id_factory = store._id_factory
    store._id_factory = lambda kind: "resolution-1"
    with pytest.raises(InvalidTransition, match="resolution_conflict") as conflict:
        store.resolve_transport_delivery_chunk_sending_unknown(
            delivery_key=projection.delivery_key,
            chunk_index=0,
            resolution="assume_delivered",
            expected_revision=decision_again.permit.revision,
            actor_id="operator-one",
            idempotency_key=_key("manual-collision"),
        )
    assert conflict.value.__cause__ is None
    store._id_factory = original_id_factory
    assumed = store.resolve_transport_delivery_chunk_sending_unknown(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        resolution="assume_delivered",
        expected_revision=decision_again.permit.revision,
        actor_id="operator-one",
        idempotency_key=_key("manual-assume"),
    )
    assert assumed.status == "delivered"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            """SELECT resolution, prior_state, prior_revision,
                      result_state, result_revision
               FROM transport_delivery_chunk_resolutions ORDER BY created_at, id"""
        ).fetchall() == [
            ("retry", "sending_unknown", unknown.revision, "pending", retried.revision),
            (
                "assume_delivered",
                "sending_unknown",
                decision_again.permit.revision,
                "delivered",
                assumed.revision,
            ),
        ]
        immutable_cases = (
            (
                "UPDATE transport_delivery_chunk_resolutions "
                "SET actor_id = 'other-operator' WHERE id = 'resolution-1'",
                "immutable",
            ),
            (
                "DELETE FROM transport_delivery_chunk_resolutions "
                "WHERE id = 'resolution-1'",
                "immutable",
            ),
            (
                """INSERT INTO transport_delivery_chunk_resolutions
                   SELECT * FROM transport_delivery_chunk_resolutions
                   WHERE id = 'resolution-1'""",
                "replacement",
            ),
            (
                """INSERT OR REPLACE INTO transport_delivery_chunk_resolutions
                   SELECT 'replacement-resolution', transport, destination_digest,
                          event_id, projection_version, chunk_index, actor_id,
                          idempotency_key, request_hash, expected_revision,
                          resolution, prior_state, prior_revision,
                          prior_claim_epoch, result_state, result_revision, created_at
                   FROM transport_delivery_chunk_resolutions
                   WHERE id = 'resolution-1'""",
                "replacement",
            ),
        )
        assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        for statement, message in immutable_cases:
            with pytest.raises(sqlite3.IntegrityError, match=message):
                conn.execute(statement)
        assert conn.execute(
            "SELECT id FROM transport_delivery_chunk_resolutions "
            "ORDER BY created_at, id"
        ).fetchall() == [("resolution-1",), ("resolution-2",)]


def test_transport_delivery_definite_rejection_fails_parent_and_blocks_later_chunk(
    store: ControlStore,
) -> None:
    projection, targets = _frozen_delivery()
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="f" * 64,
    )
    claim = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        lease_seconds=30,
    )
    released = store.release_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-one",
        claim_epoch=claim.claim_epoch,
        expected_revision=claim.revision,
        proof="rpc_not_started",
    )
    assert released.status == "pending"
    reclaimed = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        lease_seconds=30,
    )
    decision = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        claim_epoch=reclaimed.claim_epoch,
        expected_revision=reclaimed.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert decision.permit is not None
    failed = store.fail_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-two",
        claim_epoch=reclaimed.claim_epoch,
        expected_revision=decision.permit.revision,
        category="provider_rejected",
    )
    assert failed.status == "failed"
    with pytest.raises(InvalidTransition, match="chunk_order"):
        store.claim_transport_delivery_chunk(
            delivery_key=projection.delivery_key,
            chunk_index=1,
            worker_id="worker-two",
            lease_seconds=30,
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state, completed_at FROM transport_delivery_projections "
            "WHERE event_id = 'event-m10'"
        ).fetchone() == ("failed", "2026-07-23T12:00:00.000000Z")


def test_transport_opaque_target_is_restart_safe_and_single_consumer(
    store: ControlStore,
) -> None:
    target = control_module.TransportOpaqueTarget(
        namespace="action",
        purpose="control",
        resource_kind="decision",
        resource_id="decision-1",
        expected_revision=4,
        choice="approve",
        scope_digest="hmac-sha256:" + "a" * 64,
        expires_at=datetime(2026, 7, 23, 12, 5, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="scope_digest"):
        store.put_transport_opaque_target(
            token_digest="e" * 64,
            target=control_module.TransportOpaqueTarget(
                namespace="action",
                purpose="control",
                resource_kind="decision",
                resource_id="decision-1",
                expected_revision=4,
                choice="approve",
                scope_digest="raw-chat:-100:topic:41",
                expires_at=datetime(2026, 7, 23, 12, 5, tzinfo=UTC),
            ),
        )
    store.put_transport_opaque_target(token_digest="c" * 64, target=target)

    reopened = ControlStore(store.path)
    reopened.initialize()
    assert reopened.claim_transport_opaque_target(
        token_digest="c" * 64,
        consumer=None,
        consume=False,
    ) == target
    assert reopened.claim_transport_opaque_target(
        token_digest="c" * 64,
        consumer="telegram-update-1",
        consume=True,
    ) == target
    assert reopened.claim_transport_opaque_target(
        token_digest="c" * 64,
        consumer="telegram-update-1",
        consume=True,
    ) == target
    assert reopened.claim_transport_opaque_target(
        token_digest="c" * 64,
        consumer="telegram-update-2",
        consume=True,
    ) is None


def test_existing_control_store_fails_closed_when_transport_key_is_missing(
    store: ControlStore,
) -> None:
    _workspace(store)
    key_path = store.path.with_name(f".{store.path.name}.transport.key")
    key_path.unlink()

    with pytest.raises(PermissionError, match="missing"):
        ControlStore(store.path).initialize()


@pytest.mark.parametrize(
    "checkpoint_uri",
    [
        "cortex://Artifacts/file",
        "cortex://artifacts",
        "cortex://artifacts//file",
        "cortex://artifacts/./file",
        "cortex://artifacts/%2e%2e/file",
        "cortex://artifacts/a%2Fb",
        "cortex://artifacts/a%5Cb",
        "cortex://user@artifacts/file",
        "cortex://artifacts:9/file",
        "cortex://artifacts/file?q=1",
        "cortex://artifacts/file#fragment",
        "cortex://artifacts/%ZZ",
        "cortex://artifacts/%C0%AF",
        "cortex://artifacts/e\u0301.md",
        "cortex://artifacts/%61",
        "cortex://artifacts/é.md",
        "cortex://artifacts/%c3%a9.md",
        "cortex://artifacts/a\nb",
        "cortex://artifacts/a\tb",
        "cortex://artifacts/a%0Ab",
        "cortex://artifacts/a%7Fb",
        "cortex://readings/checkpoints/run.json",
    ],
)
def test_checkpoint_uri_fails_closed(
    store: ControlStore, checkpoint_uri: str
) -> None:
    run = _running_run(store)
    with pytest.raises(ValueError, match="resource|checkpoint"):
        store.commit_checkpoint(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            checkpoint_uri=checkpoint_uri,
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=_key("invalid-checkpoint"),
        )


def test_resource_uri_accepts_one_canonical_unicode_spelling() -> None:
    parsed = parse_resource_uri("cortex://artifacts/checkpoints/%C3%A9.json")
    assert parsed.root == "artifacts"
    assert parsed.segments[-1] == "é.json"


def test_pause_requires_committed_checkpoint(store: ControlStore) -> None:
    run = _running_run(store)
    requested = store.transition_run(
        run_id=run["id"],
        target_state="pause_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key=_key("pause-no-checkpoint"),
    ).value
    with pytest.raises(InvalidTransition, match="checkpoint_missing"):
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            target_state="paused",
            expected_revision=requested["revision"],
            actor_id="runtime",
            idempotency_key=_key("bad-paused"),
        )


def test_invalid_idempotency_key_fails_before_mutation(store: ControlStore) -> None:
    with pytest.raises(ValueError, match="idempotency"):
        store.create_workspace(title="Research", actor_id="local", idempotency_key="short")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0


def test_control_store_path_is_private_and_rejects_symlink(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    database = private / "control.db"
    value = ControlStore(database)
    value.initialize()
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    target = private / "target.db"
    target.touch(mode=0o600)
    link = private / "linked.db"
    link.symlink_to(target)
    with pytest.raises(PermissionError, match="private"):
        ControlStore(link).initialize()


def test_fresh_database_initialization_is_safe_across_processes(tmp_path: Path) -> None:
    private = tmp_path / "concurrent"
    private.mkdir(mode=0o700)
    database = private / "control.db"
    context = multiprocessing.get_context("spawn")
    with context.Pool(8) as pool:
        results = pool.map(_initialize_database, [str(database)] * 8)
    assert results == ["ok"] * 8
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(version,) for version in MIGRATION_VERSIONS]


def test_attempt_dispatch_reservation_is_exclusive_and_owner_fenced(
    store: ControlStore,
) -> None:
    thread = _thread(store)
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("dispatch-run"),
    ).value
    attempt_id = run["attempt"]["id"]
    claimed = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=attempt_id,
        dispatch_owner="worker-one",
        runtime_release_id="release-claim",
        state_generation_id="generation-claim",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("dispatch-reserve-one"),
    ).value
    assert claimed["attempt"]["dispatch_owner"] == "worker-one"

    with pytest.raises(InvalidTransition, match="already_reserved"):
        store.reserve_attempt_dispatch(
            run_id=run["id"],
            attempt_id=attempt_id,
            dispatch_owner="worker-two",
            runtime_release_id="release-claim",
            state_generation_id="generation-claim",
            **_EXACT_RUNTIME_IDENTITY,
            expected_revision=claimed["revision"],
            actor_id="runtime",
            idempotency_key=_key("dispatch-reserve-two"),
        )
    with pytest.raises(InvalidTransition, match="dispatch_owner_mismatch"):
        store.fail_unbound_run(
            run_id=run["id"],
            attempt_id=attempt_id,
            expected_revision=claimed["revision"],
            category="runtime_dispatch_failed",
            actor_id="runtime",
            idempotency_key=_key("wrong-owner"),
            dispatch_owner="worker-two",
        )
    assert store.get_run(run["id"])["state"] == "queued"


def test_runtime_event_inbox_replays_same_hash_and_rejects_payload_drift(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    before = len(store.list_run_events(run["id"]))
    first = store.record_runtime_observation(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        event_type="runtime.tool.started",
        payload={"tool_call_id": "tool-1", "tool_name": "search"},
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("inbox-first"),
        adapter_event_id="adapter:event-1",
        adapter_event_sequence=0,
    )
    replay = store.record_runtime_observation(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        event_type="runtime.tool.started",
        payload={"tool_call_id": "tool-1", "tool_name": "search"},
        expected_revision=first.value["revision"],
        actor_id="runtime-restarted",
        idempotency_key=_key("inbox-replay"),
        adapter_event_id="adapter:event-1",
        adapter_event_sequence=0,
    )
    assert replay.replayed is True
    assert replay.value == first.value
    assert len(store.list_run_events(run["id"])) == before + 1

    with pytest.raises(IdempotencyConflict):
        store.record_runtime_observation(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            event_type="runtime.tool.started",
            payload={"tool_call_id": "tool-1", "tool_name": "different"},
            expected_revision=first.value["revision"],
            actor_id="runtime-restarted",
            idempotency_key=_key("inbox-conflict"),
            adapter_event_id="adapter:event-1",
            adapter_event_sequence=0,
        )


def test_runtime_event_inbox_rejects_sequence_gaps_and_collisions(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    common = {
        "run_id": run["id"],
        "attempt_id": run["_attempt_id"],
        "runtime_binding_id": run["_binding_id"],
        "runtime_release_id": run["_release_id"],
        "state_generation_id": run["_generation_id"],
        "event_type": "runtime.tool.started",
        "payload": {"tool_call_id": "tool-sequence", "tool_name": "search"},
        "expected_revision": run["revision"],
        "actor_id": "runtime",
    }
    with pytest.raises(InvalidTransition, match="runtime_event_sequence_gap"):
        store.record_runtime_observation(
            **common,
            idempotency_key=_key("sequence-gap"),
            adapter_event_id="adapter:sequence-1",
            adapter_event_sequence=1,
        )
    first = store.record_runtime_observation(
        **common,
        idempotency_key=_key("sequence-zero"),
        adapter_event_id="adapter:sequence-0",
        adapter_event_sequence=0,
    ).value
    with pytest.raises(IdempotencyConflict):
        store.record_runtime_observation(
            **{**common, "expected_revision": first["revision"]},
            idempotency_key=_key("sequence-collision"),
            adapter_event_id="adapter:sequence-collision",
            adapter_event_sequence=0,
        )
    second = store.record_runtime_observation(
        **{
            **common,
            "expected_revision": first["revision"],
            "payload": {
                "tool_call_id": "tool-sequence-2",
                "tool_name": "read",
            },
        },
        idempotency_key=_key("sequence-one"),
        adapter_event_id="adapter:sequence-next",
        adapter_event_sequence=1,
    ).value
    assert second["revision"] == first["revision"] + 1


@pytest.mark.parametrize("causation", ["missing", "forged"])
def test_unclaimed_decision_action_rejects_unverified_runtime_event_causation(
    store: ControlStore,
    causation: str,
) -> None:
    run = _running_run(store)
    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=run["revision"],
        kind="approval",
        prompt="Approve this action?",
        options=[{"id": "approve", "label": "Approve"}],
        actor_id="runtime",
        idempotency_key=_key(f"causation-decision-{causation}"),
    ).value
    store.resolve_decision(
        decision_id=decision["id"],
        choice="approve",
        expected_revision=decision["revision"],
        actor_id="local",
        idempotency_key=_key(f"causation-resolve-{causation}"),
    )
    action = store.list_pending_runtime_actions()[0]
    run = store.get_run(run["id"])
    causal_fields = (
        {
            "caused_by_adapter_operation_id": f"runtime-action:{action['id']}:forged",
            "caused_by_delivery_epoch": 1,
        }
        if causation == "forged"
        else {}
    )

    with pytest.raises(
        InvalidTransition,
        match="runtime_resume_causation_unverified",
    ) as error:
        store.append_runtime_message(
            run_id=run["id"],
            attempt_id=run["active_attempt_id"],
            runtime_binding_id=action["runtime_binding_id"],
            runtime_release_id=action["runtime_release_id"],
            state_generation_id=store.get_attempt(run["active_attempt_id"])[
                "state_generation_id"
            ],
            content="Buffered before decision delivery",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=_key(f"causation-event-{causation}"),
            adapter_event_id=f"adapter:causation:{causation}",
            adapter_event_sequence=0,
            **causal_fields,
        )

    assert error.value.source == "runtime_resume_causation_unverified"
    assert store.get_run(run["id"])["state"] == "resuming"
    unchanged_action = store.get_runtime_action(action["id"])
    assert unchanged_action["state"] == "pending"
    assert unchanged_action["claim_epoch"] == 0


def test_runtime_action_epochs_fence_stale_receipts_and_unknown_outcome(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=run["revision"],
        kind="approval",
        prompt="Approve this action?",
        options=[{"id": "approve", "label": "Approve"}],
        actor_id="runtime",
        idempotency_key=_key("epoch-decision"),
    ).value
    store.resolve_decision(
        decision_id=decision["id"],
        choice="approve",
        expected_revision=decision["revision"],
        actor_id="local",
        idempotency_key=_key("epoch-resolve"),
    )
    action = store.list_pending_runtime_actions()[0]
    first = store.claim_runtime_action(
        action_id=action["id"],
        worker_id="worker-one",
        lease_seconds=30,
        actor_id="worker-one",
        idempotency_key=_key("epoch-claim-one"),
    )
    replay = store.claim_runtime_action(
        action_id=action["id"],
        worker_id="worker-one",
        lease_seconds=30,
        actor_id="worker-one",
        idempotency_key=_key("epoch-claim-one"),
    )
    assert replay.replayed is True
    assert first.value["claim_epoch"] == 1

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE runtime_actions SET claim_expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (action["id"],),
        )
    second = store.claim_runtime_action(
        action_id=action["id"],
        worker_id="worker-two",
        lease_seconds=30,
        actor_id="worker-two",
        idempotency_key=_key("epoch-claim-two"),
    ).value
    assert second["claim_epoch"] == 2
    with pytest.raises(InvalidTransition, match="stale_claim_receipt"):
        store.claim_runtime_action(
            action_id=action["id"],
            worker_id="worker-one",
            lease_seconds=30,
            actor_id="worker-one",
            idempotency_key=_key("epoch-claim-one"),
        )
    with pytest.raises(InvalidTransition, match="claim_fence_mismatch"):
        store.acknowledge_runtime_action(
            action_id=action["id"],
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            expected_revision=store.get_run(run["id"])["revision"],
            actor_id="runtime",
            idempotency_key=_key("epoch-stale-ack"),
            claim_owner="worker-one",
            claim_epoch=1,
        )

    current = store.get_run(run["id"])
    unknown = store.mark_runtime_action_outcome_unknown(
        action_id=action["id"],
        claim_owner="worker-two",
        claim_epoch=second["claim_epoch"],
        expected_revision=current["revision"],
        category="transport_timeout",
        actor_id="runtime",
        idempotency_key=_key("epoch-unknown"),
    ).value
    assert unknown["outcome_state"] == "outcome_unknown"
    assert store.get_run(run["id"])["state"] == "resuming"
    assert store.list_pending_runtime_actions() == []

    reconcile = store.claim_runtime_action_reconciliation(
        action_id=action["id"],
        worker_id="reconciler",
        lease_seconds=30,
        actor_id="reconciler",
        idempotency_key=_key("epoch-reconcile"),
    ).value
    assert store.list_runtime_actions_requiring_reconciliation() == []
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE runtime_actions SET claim_expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (action["id"],),
        )
    assert [
        item["id"]
        for item in store.list_runtime_actions_requiring_reconciliation()
    ] == [action["id"]]
    reconcile = store.claim_runtime_action_reconciliation(
        action_id=action["id"],
        worker_id="reconciler-after-crash",
        lease_seconds=30,
        actor_id="reconciler-after-crash",
        idempotency_key=_key("epoch-reconcile-after-crash"),
    ).value
    deferred = store.defer_runtime_action_reconciliation(
        action_id=action["id"],
        claim_owner="reconciler-after-crash",
        claim_epoch=reconcile["claim_epoch"],
        expected_revision=store.get_run(run["id"])["revision"],
        category="operation_outcome_unknown",
        actor_id="reconciler-after-crash",
        idempotency_key=_key("epoch-reconcile-deferred"),
    ).value
    assert deferred["outcome_state"] == "outcome_unknown"
    reconcile = store.claim_runtime_action_reconciliation(
        action_id=action["id"],
        worker_id="reconciler-final",
        lease_seconds=30,
        actor_id="reconciler-final",
        idempotency_key=_key("epoch-reconcile-final"),
    ).value
    retried = store.retry_runtime_action_delivery(
        action_id=action["id"],
        claim_owner="reconciler-final",
        claim_epoch=reconcile["claim_epoch"],
        expected_revision=store.get_run(run["id"])["revision"],
        actor_id="reconciler",
        idempotency_key=_key("epoch-redeliver"),
    ).value
    assert retried["outcome_state"] == "ready"
    assert len(store.list_pending_runtime_actions()) == 1


def test_terminal_pin_release_is_durable_fenced_and_retryable(
    store: ControlStore,
) -> None:
    run = _running_run(store)
    completed = _runtime_transition(store, run, "completed", "release-complete")
    releases = store.list_pending_pin_releases()
    assert len(releases) == 1
    assert releases[0]["runtime_release_id"] == run["_release_id"]
    claimed = store.claim_pin_release(
        release_action_id=releases[0]["id"],
        worker_id="release-worker",
        lease_seconds=30,
        actor_id="runtime",
        idempotency_key=_key("release-claim"),
    ).value
    with pytest.raises(InvalidTransition, match="claim_fence_mismatch"):
        store.acknowledge_pin_release(
            release_action_id=claimed["id"],
            claim_owner="release-worker",
            claim_epoch=claimed["claim_epoch"] + 1,
            expected_revision=completed["revision"],
            actor_id="runtime",
            idempotency_key=_key("release-stale"),
        )
    failed = store.fail_pin_release(
        release_action_id=claimed["id"],
        claim_owner="release-worker",
        claim_epoch=claimed["claim_epoch"],
        expected_revision=completed["revision"],
        category="updater_busy",
        actor_id="runtime",
        idempotency_key=_key("release-failed"),
    ).value
    assert failed["state"] == "failed"
    assert store.get_run(run["id"])["state"] == "completed"
    retried = store.retry_pin_release(
        release_action_id=claimed["id"],
        expected_revision=store.get_run(run["id"])["revision"],
        actor_id="runtime",
        idempotency_key=_key("release-retry"),
    ).value
    reclaimed = store.claim_pin_release(
        release_action_id=retried["id"],
        worker_id="release-worker-two",
        lease_seconds=30,
        actor_id="runtime",
        idempotency_key=_key("release-reclaim"),
    ).value
    acked = store.acknowledge_pin_release(
        release_action_id=reclaimed["id"],
        claim_owner="release-worker-two",
        claim_epoch=reclaimed["claim_epoch"],
        expected_revision=store.get_run(run["id"])["revision"],
        actor_id="runtime",
        idempotency_key=_key("release-acked"),
    ).value
    assert acked["state"] == "acked"
    assert store.list_pending_pin_releases() == []
    assert [event["type"] for event in store.list_run_events(run["id"])][-3:] == [
        "runtime.pin_release.failed",
        "runtime.pin_release.retried",
        "runtime.pin_release.acked",
    ]


def test_dispatch_reservation_precedes_binding_and_fences_identity(
    store: ControlStore,
) -> None:
    thread = _thread(store)
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("reserve-run"),
    ).value
    reserved = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        dispatch_owner="dispatcher",
        runtime_release_id="release-exact",
        state_generation_id="generation-exact",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("reserve-exact"),
    ).value
    assert reserved["attempt"]["runtime_binding_id"] is None
    assert reserved["attempt"]["runtime_release_id"] == "release-exact"
    assert reserved["attempt"]["state_generation_id"] == "generation-exact"
    binding = store.create_runtime_binding(
        thread_id=thread["id"],
        adapter_id="hermes",
        runtime_session_ref="session-after-reservation",
        generation=1,
        adapter_version="test",
        actor_id="runtime",
        idempotency_key=_key("reserve-binding"),
    ).value
    with pytest.raises(InvalidTransition, match="dispatch_reservation_mismatch"):
        store.pin_attempt_runtime(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            runtime_binding_id=binding["id"],
            runtime_release_id="release-wrong",
            state_generation_id="generation-exact",
            dispatch_owner="dispatcher",
            expected_revision=reserved["revision"],
            actor_id="runtime",
            idempotency_key=_key("reserve-wrong-pin"),
        )


def test_legacy_null_generation_schedules_manual_recovery(store: ControlStore) -> None:
    run = _running_run(store)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE attempts SET state_generation_id = NULL WHERE id = ?",
            (run["_attempt_id"],),
        )
    with pytest.raises(InvalidTransition, match="legacy_runtime_identity"):
        store.apply_runtime_transition(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            target_state="completed",
            expected_revision=run["revision"],
            actor_id="runtime",
            idempotency_key=_key("legacy-result"),
        )
    command = store.schedule_runtime_recovery(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        expected_revision=run["revision"],
        actor_id="recovery",
        idempotency_key=_key("legacy-recovery"),
    ).value
    assert command["kind"] == "manual_recovery"
    assert command["state"] == "manual_required"
    assert store.list_pending_runtime_recoveries() == []
    assert store.list_run_events(run["id"])[-1]["type"] == (
        "runtime.recovery.manual_required"
    )


def test_unbound_dispatch_failure_creates_terminal_pin_release(
    store: ControlStore,
) -> None:
    thread = _thread(store)
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("unbound-run"),
    ).value
    reserved = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        dispatch_owner="unbound-worker",
        runtime_release_id="release-unbound",
        state_generation_id="generation-unbound",
        **_EXACT_RUNTIME_IDENTITY,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key("unbound-reserve"),
    ).value
    failed = store.fail_unbound_run(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        expected_revision=reserved["revision"],
        category="session_open_failed",
        actor_id="runtime",
        idempotency_key=_key("unbound-failed"),
        dispatch_owner="unbound-worker",
    ).value
    assert failed["state"] == "failed"
    releases = store.list_pending_pin_releases()
    assert len(releases) == 1
    assert releases[0]["runtime_release_id"] == "release-unbound"
    assert store.list_run_events(run["id"])[-1]["payload"][
        "pin_release_action_id"
    ] == releases[0]["id"]


def test_recovery_command_claim_and_outcome_are_replayable_events(
    store: ControlStore,
) -> None:
    thread = _thread(store)
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key("recovery-run"),
    ).value
    command = store.schedule_runtime_recovery(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        expected_revision=run["revision"],
        actor_id="recovery",
        idempotency_key=_key("recovery-schedule"),
    ).value
    assert command["kind"] == "dispatch"
    claimed = store.claim_runtime_recovery(
        recovery_command_id=command["id"],
        worker_id="recovery-worker",
        lease_seconds=30,
        actor_id="recovery",
        idempotency_key=_key("recovery-claim"),
    ).value
    completed = store.record_runtime_recovery_outcome(
        recovery_command_id=command["id"],
        claim_owner="recovery-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="dispatchable",
        expected_revision=store.get_run(run["id"])["revision"],
        details={"status": "ready", "retryable": True},
        actor_id="recovery",
        idempotency_key=_key("recovery-outcome"),
    ).value
    assert completed["state"] == "completed"
    assert completed["result"] == {"retryable": True, "status": "ready"}
    assert [event["type"] for event in store.list_run_events(run["id"])][-2:] == [
        "runtime.recovery.queued",
        "runtime.recovery.dispatchable",
    ]


@pytest.mark.parametrize(
    "option",
    [
        {"id": "approve", "secret": "token"},
        {"id": "approve", "path": "/private/data"},
        {"id": "approve", "raw_payload": {"hidden": True}},
        {"id": "approve", "reasoning": "hidden"},
        {"id": "approve", "unknown": "field"},
    ],
)
def test_decision_options_reject_private_or_unknown_fields(
    store: ControlStore, option: dict
) -> None:
    run = _running_run(store)
    with pytest.raises(ValueError, match="private fields"):
        store.create_decision(
            run_id=run["id"],
            attempt_id=run["_attempt_id"],
            runtime_binding_id=run["_binding_id"],
            runtime_release_id=run["_release_id"],
            state_generation_id=run["_generation_id"],
            expected_revision=run["revision"],
            kind="approval",
            prompt="Approve?",
            options=[option],
            actor_id="runtime",
            idempotency_key=_key(f"private-{len(option)}"),
        )
    assert store.get_run(run["id"])["state"] == "running"


def test_decision_options_are_bounded_public_dtos(store: ControlStore) -> None:
    run = _running_run(store)
    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=run["_attempt_id"],
        runtime_binding_id=run["_binding_id"],
        runtime_release_id=run["_release_id"],
        state_generation_id=run["_generation_id"],
        expected_revision=run["revision"],
        kind="approval",
        prompt="Approve?",
        options=[
            {"id": "approve", "label": "Approve", "description": "Run once"}
        ],
        actor_id="runtime",
        idempotency_key=_key("public-decision"),
    ).value
    assert decision["options"] == [
        {"id": "approve", "label": "Approve", "description": "Run once"}
    ]


def test_transport_delivery_chunk_records_the_window_that_authorized_it(
    store: ControlStore,
) -> None:
    """AMD-5: a send inside a window names the decision that permitted it.

    The outbound receipt is an HMAC digest with no room for the id, so it
    rides in its own column on the delivered row, and the column is
    write-once: a redelivery must not re-attribute a chunk to whichever
    window happened to be open later.
    """

    window = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    projection, targets = _frozen_delivery(
        event_id="event-window",
        operation_id="telegram-delivery-window",
        token_suffix="window",
    )
    store.freeze_transport_delivery_projection(
        projection=projection,
        opaque_targets=targets,
        request_hash="c" * 64,
    )
    claim = store.claim_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-window",
        lease_seconds=30,
    )
    decision = store.begin_transport_delivery_chunk_send(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-window",
        claim_epoch=claim.claim_epoch,
        expected_revision=claim.revision,
        observed_capability_binding_digest="a" * 64,
    )
    assert decision.permit is not None
    store.complete_transport_delivery_chunk(
        delivery_key=projection.delivery_key,
        chunk_index=0,
        worker_id="worker-window",
        claim_epoch=claim.claim_epoch,
        expected_revision=decision.permit.revision,
        provider_receipt_digest="hmac-sha256:" + "4" * 64,
        transport_window_id=window.id,
    )

    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT transport_window_id FROM transport_delivery_chunks "
            "WHERE event_id = 'event-window' AND chunk_index = 0"
        ).fetchone() == (window.id,)
        # The chunk that was never sent inside a window carries no id.
        assert conn.execute(
            "SELECT transport_window_id FROM transport_delivery_chunks "
            "WHERE event_id = 'event-window' AND chunk_index = 1"
        ).fetchone() == (None,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE transport_delivery_chunks SET transport_window_id = "
                "'transport-activation-99' WHERE chunk_index = 0"
            )
    # A window id that names no decision is refused by the foreign key. The
    # pragma is per-connection and cannot be set inside a transaction, so this
    # needs a connection that has not written yet.
    checked = sqlite3.connect(store.path)
    try:
        checked.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            checked.execute(
                "UPDATE transport_delivery_chunks SET transport_window_id = "
                "'transport-activation-99' WHERE chunk_index = 1"
            )
    finally:
        checked.close()


def test_the_chunk_port_stamps_the_window_that_authorized_the_send(
    store: ControlStore,
) -> None:
    """⟦AMD-5⟧, derived at the moment the receipt is written.

    A caller-supplied window id could name a window that had already been
    disabled by the time the chunk landed; reading the decision in force here
    is the version that cannot drift.
    """

    from cortex_platform.product.transports.ports import ControlTransportChunkPort

    window = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-port-00001",
    )
    projection, targets = _frozen_delivery(
        event_id="event-port",
        operation_id="telegram-delivery-port",
        token_suffix="port",
    )
    store.freeze_transport_delivery_projection(
        projection=projection, opaque_targets=targets, request_hash="b" * 64
    )
    port = ControlTransportChunkPort(store=store, worker_id="worker-port", lease_seconds=30)
    claim = port.claim(projection.delivery_key, 0)
    decision = port.begin(claim, observed_capability_binding_digest="a" * 64)
    assert decision.permit is not None
    port.complete(decision.permit, provider_receipt_digest="hmac-sha256:" + "5" * 64)

    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT transport_window_id FROM transport_delivery_chunks "
            "WHERE event_id = 'event-port' AND chunk_index = 0"
        ).fetchone() == (window.id,)

    # A permanent enable is not a window: it stamps nothing.
    store.enable_transport_activation(
        transport="telegram",
        scope="permanent",
        actor_id="operator",
        idempotency_key="tg-permanent-port-01",
    )
    claim = port.claim(projection.delivery_key, 1)
    decision = port.begin(claim, observed_capability_binding_digest="a" * 64)
    assert decision.permit is not None
    port.complete(decision.permit, provider_receipt_digest="hmac-sha256:" + "6" * 64)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT transport_window_id FROM transport_delivery_chunks "
            "WHERE event_id = 'event-port' AND chunk_index = 1"
        ).fetchone() == (None,)


def _window_stamp(store: ControlStore, event_id: str) -> object:
    with sqlite3.connect(store.path) as conn:
        return conn.execute(
            "SELECT transport_window_id FROM transport_delivery_chunks "
            "WHERE event_id = ? AND chunk_index = 0",
            (event_id,),
        ).fetchone()[0]


def _permitted_chunk(store: ControlStore, suffix: str):
    """Freeze a delivery, claim chunk 0 and hold an uncommitted send permit."""

    from cortex_platform.product.transports.ports import ControlTransportChunkPort

    projection, targets = _frozen_delivery(
        event_id=f"event-{suffix}",
        operation_id=f"telegram-delivery-{suffix}",
        token_suffix=suffix,
    )
    store.freeze_transport_delivery_projection(
        projection=projection, opaque_targets=targets, request_hash="b" * 64
    )
    port = ControlTransportChunkPort(
        store=store, worker_id=f"worker-{suffix}", lease_seconds=30
    )
    claim = port.claim(projection.delivery_key, 0)
    decision = port.begin(claim, observed_capability_binding_digest="a" * 64)
    assert decision.permit is not None
    return port, decision.permit


def test_a_permit_carries_the_window_that_authorized_it(store: ControlStore) -> None:
    """⟦AMD-5⟧: the AUTHORIZING window, not whatever is in force at the receipt.

    The id used to be read at completion, so the gate changing between the
    permit and the receipt decided what the audit said. `schema.py`'s
    write-once trigger then locked that answer permanently.
    """

    window = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-permit-0001",
    )
    _port, permit = _permitted_chunk(store, "permitwin")

    assert permit.transport_window_id == window.id


def test_a_disable_between_the_permit_and_the_receipt_keeps_the_window_id(
    store: ControlStore,
) -> None:
    """The send was authorized. Losing the id loses the audit answer."""

    window = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-disable-001",
    )
    port, permit = _permitted_chunk(store, "disablemid")

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="tg-disable-mid-00001",
    )
    port.complete(permit, provider_receipt_digest="hmac-sha256:" + "8" * 64)

    assert _window_stamp(store, "event-disablemid") == window.id


def test_an_expiry_between_the_permit_and_the_receipt_keeps_the_window_id(
    tmp_path: Path,
) -> None:
    clock = {"now": datetime(2026, 7, 23, 12, 0, tzinfo=UTC)}
    store = ControlStore(
        tmp_path / "expiring.db",
        clock=lambda: clock["now"],
        id_factory=DeterministicIds(),
    )
    store.initialize()
    window = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=60,
        actor_id="operator",
        idempotency_key="tg-window-expire-0001",
    )
    port, permit = _permitted_chunk(store, "expiremid")

    clock["now"] = clock["now"] + timedelta(seconds=61)
    port.complete(permit, provider_receipt_digest="hmac-sha256:" + "9" * 64)

    assert _window_stamp(store, "event-expiremid") == window.id


def test_a_new_window_between_the_permit_and_the_receipt_is_not_stamped(
    store: ControlStore,
) -> None:
    """The worse variant: the row was stamped with the WRONG window.

    A new window opening between the permit and the receipt made the receipt
    name a window that never authorized this send, and the write-once trigger
    made that permanent.
    """

    first = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-first-00001",
    )
    port, permit = _permitted_chunk(store, "newwindow")

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="tg-disable-first-0001",
    )
    second = store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-second-0001",
    )
    port.complete(permit, provider_receipt_digest="hmac-sha256:" + "a" * 64)

    stamped = _window_stamp(store, "event-newwindow")
    assert stamped == first.id
    assert stamped != second.id
