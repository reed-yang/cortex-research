"""D7 storage: one Control-owned table, and the primitives that keep it honest."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import InvalidTransition
from cortex_platform.product.control.schema import (
    MIGRATION_VERSIONS,
    RESEARCH_SCHEDULES_MIGRATION,
    SCHEMA_VERSION,
)


def _register(store: ControlStore, **overrides: object) -> dict:
    request = {
        "job_key": "capture-drain",
        "operation": "capture_drain",
        "enabled": True,
        "interval_seconds": 300,
        "cadence_source": "product",
        "legacy_schedule": None,
        "actor_id": "machine:p4-capture-consumer",
        "idempotency_key": "register-capture-drain001",
    }
    request.update(overrides)
    return store.register_research_schedule(**request).value


# -- the migration --------------------------------------------------------


def test_the_migration_number_lives_in_one_constant() -> None:
    """S3.4 holds 14 and P5.2 holds 15, so this slice takes 16.

    The slice no longer owns the highest number -- research contexts took 17 and
    thread archiving took 18 -- so what is asserted is that its number is a
    declared migration and that the declared set is still a well-formed
    ascending sequence, not that it is the last one.
    """

    # Contiguity is a runtime contract, not a style: state_safety refuses a
    # control database whose applied versions are not exactly 1..n, and
    # `SCHEMA_VERSION` is derivable from the tail of that set.
    assert RESEARCH_SCHEDULES_MIGRATION == 16
    assert RESEARCH_SCHEDULES_MIGRATION in MIGRATION_VERSIONS
    assert MIGRATION_VERSIONS[-1] == SCHEMA_VERSION
    assert max(MIGRATION_VERSIONS) == SCHEMA_VERSION
    assert list(MIGRATION_VERSIONS) == sorted(MIGRATION_VERSIONS)
    assert len(set(MIGRATION_VERSIONS)) == len(MIGRATION_VERSIONS)
    assert list(MIGRATION_VERSIONS) == list(range(1, SCHEMA_VERSION + 1))


def test_the_schedule_table_exists_after_migration(store: ControlStore) -> None:
    with store._connect() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(research_schedules)")
        }
    assert {
        "job_key",
        "operation",
        "enabled",
        "interval_seconds",
        "next_due_at",
        "cadence_source",
        "legacy_schedule",
        "revision",
    } <= columns


def test_a_schedule_row_is_durable_and_its_identity_immutable(
    store: ControlStore,
) -> None:
    _register(store)
    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            conn.execute(
                "DELETE FROM research_schedules WHERE job_key = ?", ("capture-drain",)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_schedules SET operation = 'legacy' WHERE job_key = ?",
                ("capture-drain",),
            )


def test_migrations_apply_in_order_and_land_this_version(tmp_path: Path) -> None:
    """The application-order shape: 1..n, this slice included, once each.

    `n` is the store's head, not this slice's number: the contiguity rule
    `state_safety` enforces is `1..SCHEMA_VERSION`, and sibling slices have
    landed above 16.
    """

    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    store.initialize()  # repeatable
    with store._connect() as conn:
        applied = [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert applied == list(range(1, SCHEMA_VERSION + 1))
    assert RESEARCH_SCHEDULES_MIGRATION in applied
    assert applied == sorted(applied)
    assert len(applied) == len(set(applied))


def test_an_older_snapshot_without_the_table_is_still_protected() -> None:
    """Protected always, required only from the version that creates it."""

    from cortex_platform.backup import default_database_specs

    control = next(
        spec for spec in default_database_specs() if spec.name == "cortex_control"
    )
    assert "research_schedules" in control.required_tables
    since = dict(control.table_since_schema_version)
    assert since["research_schedules"] == RESEARCH_SCHEDULES_MIGRATION




def test_a_legacy_row_may_be_recorded_but_never_armed(store: ControlStore) -> None:
    """A job with no product implementation is inventory, not a schedule."""

    _register(
        store,
        job_key="radar-scan",
        operation="legacy",
        enabled=False,
        cadence_source="unknown",
        idempotency_key="register-radar-scan00001",
    )
    radar = store.get_research_schedule("radar-scan")
    assert radar["enabled"] is False
    with pytest.raises(InvalidTransition):
        store.set_research_schedule_enabled(
            job_key="radar-scan",
            enabled=True,
            expected_revision=int(radar["revision"]),
            actor_id="local-operator",
            idempotency_key="arm-radar-scan-0000001",
        )
    with pytest.raises(ValueError, match="legacy"):
        _register(
            store,
            job_key="another-legacy",
            operation="legacy",
            enabled=True,
            cadence_source="unknown",
            idempotency_key="register-another-leg001",
        )


def test_an_outcome_re_arms_the_row_from_its_own_interval(store: ControlStore) -> None:
    value = _register(store, interval_seconds=300)
    before = store.get_research_schedule("capture-drain")["next_due_at"]
    store.record_research_schedule_outcome(
        job_key="capture-drain",
        expected_revision=int(value["revision"]),
        outcome="ran",
        started_at=store._now(),
        actor_id="machine:p4-capture-consumer",
        idempotency_key="outcome-capture-drain001",
    )
    after = store.get_research_schedule("capture-drain")
    assert after["next_due_at"] > before
    assert after["last_outcome"] == "ran"
    assert store.list_due_research_schedules() == []


def test_registration_replays_rather_than_duplicating(store: ControlStore) -> None:
    first = _register(store)
    second = _register(store)
    assert first["job_key"] == second["job_key"]
    assert len(store.list_research_schedules()) == 1
