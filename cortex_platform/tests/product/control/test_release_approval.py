"""D6: the operator's approval of one exact release, as a durable decision.

Everything here is about the *decision*, not about what it gates. The gate's
refusals live beside the updater service that enforces them, and the accept and
refuse cases there share no state with each other or with these.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control import schema
from cortex_platform.product.control.schema import (
    MIGRATION_VERSIONS,
    RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    apply_migrations,
)
from cortex_platform.product.control.store import ControlStore

# The key the operator's command actually carries, imported rather than
# imitated: a hand-written key cannot reproduce what makes a reversal look like
# a retry to the store.
from cortex_platform.product.runtime_update.cli import _decision_key

RELEASE = "hermes-0.15.0"
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _store(tmp_path: Path) -> ControlStore:
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    return store


def _counts(tmp_path: Path) -> tuple[int, int]:
    """(decision rows, audit rows) — what the forensic record shows happened."""

    with sqlite3.connect(tmp_path / "control.db") as conn:
        decisions = conn.execute(
            "SELECT COUNT(*) FROM runtime_release_approvals"
        ).fetchone()[0]
        audits = conn.execute(
            "SELECT COUNT(*) FROM control_audit "
            "WHERE aggregate_type = 'runtime_release_approval'"
        ).fetchone()[0]
    return int(decisions), int(audits)


def test_the_migration_number_lives_in_exactly_one_place() -> None:
    """So renumbering this slice at merge stays a one-line edit.

    The slice no longer owns the highest number — sibling slices landed above
    it — so what is asserted is that its number is a declared migration and
    that the declared set is still a well-formed ascending sequence, not that
    it is the last one.
    """

    assert RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION in MIGRATION_VERSIONS
    assert max(MIGRATION_VERSIONS) == SCHEMA_VERSION
    assert MIGRATION_VERSIONS[-1] == SCHEMA_VERSION
    assert len(set(MIGRATION_VERSIONS)) == len(MIGRATION_VERSIONS)
    assert list(MIGRATION_VERSIONS) == sorted(MIGRATION_VERSIONS)
    declared = [version for version, _script in schema.migration_scripts()]
    assert declared == list(MIGRATION_VERSIONS)


def test_declared_migrations_are_the_migrations_actually_applied(
    tmp_path: Path,
) -> None:
    """Pins the declaration against the applier, which is still an `if` chain.

    The chain is left alone so a sibling branch inserting its own version is a
    clean textual addition; this is what stops the two drifting.
    """

    database = tmp_path / "control.db"
    with sqlite3.connect(database, isolation_level=None) as conn:
        apply_migrations(conn, now="2026-09-02T12:00:00.000000Z")
        recorded = [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert recorded == list(MIGRATION_VERSIONS)


@pytest.mark.parametrize("start_at", [None, *MIGRATION_VERSIONS[:-1]])
def test_a_database_at_any_earlier_version_reaches_the_approval_table(
    tmp_path: Path, start_at: int | None
) -> None:
    """The application-order shape, written so renumbering needs no test edits.

    A database that stopped at any recorded version — including the ones 13 and
    14 will occupy once they merge — must still arrive at the approval table
    when the applier runs again.
    """

    database = tmp_path / f"control-{start_at}.db"
    with sqlite3.connect(database, isolation_level=None) as conn:
        if start_at is not None:
            original = schema._execute_script_in_transaction

            def stop_after(connection, script):
                for version, source in schema.migration_scripts():
                    if source == script and version > start_at:
                        raise RuntimeError("stop")
                original(connection, script)

            schema._execute_script_in_transaction = stop_after  # type: ignore[assignment]
            try:
                with pytest.raises(RuntimeError, match="stop"):
                    apply_migrations(conn, now="2026-09-02T12:00:00.000000Z")
            finally:
                schema._execute_script_in_transaction = original  # type: ignore[assignment]
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='runtime_release_approvals'"
            ).fetchone() == (0,)
        apply_migrations(conn, now="2026-09-02T12:00:01.000000Z")
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='runtime_release_approvals'"
        ).fetchone() == (1,)


def test_approval_is_bound_to_the_exact_manifest_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.runtime_release_approved(RELEASE, DIGEST) is False
    record = store.approve_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key="approve-command-0001",
    )
    assert record.decision == "approve"
    assert store.runtime_release_approved(RELEASE, DIGEST) is True
    # A release id is a name the packager chose; the digest is what the slot,
    # the registry and `measure_identity` all bind.
    assert store.runtime_release_approved(RELEASE, OTHER_DIGEST) is False
    assert store.runtime_release_approved("other-release", DIGEST) is False


def test_a_later_revoke_wins_and_a_later_approve_wins_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index, decide in enumerate(
        (
            store.approve_runtime_release,
            store.revoke_runtime_release,
            store.approve_runtime_release,
        )
    ):
        decide(
            release_id=RELEASE,
            manifest_sha256=DIGEST,
            actor_id="operator",
            idempotency_key=f"decision-command-{index:04d}",
        )
    assert store.runtime_release_approved(RELEASE, DIGEST) is True
    store.revoke_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key="decision-command-0009",
    )
    assert store.runtime_release_approved(RELEASE, DIGEST) is False
    report = store.runtime_release_approval_report(RELEASE, DIGEST)
    assert report["approved"] is False and report["decision"] == "revoke"
    with sqlite3.connect(tmp_path / "control.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_release_approvals"
        ).fetchone() == (4,)


def test_the_decision_table_is_append_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.approve_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key="approve-command-0002",
    )
    with sqlite3.connect(tmp_path / "control.db") as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE runtime_release_approvals SET decision = 'revoke'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM runtime_release_approvals")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runtime_release_approvals VALUES "
                "('x', 'maybe', ?, ?, 'operator', '2026-09-02T00:00:00.000000Z')",
                (RELEASE, DIGEST),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runtime_release_approvals VALUES "
                "('y', 'approve', ?, 'NOT-A-DIGEST', 'operator', "
                "'2026-09-02T00:00:00.000000Z')",
                (RELEASE,),
            )


def test_approving_a_release_and_enabling_dispatch_are_two_decisions(
    tmp_path: Path,
) -> None:
    """D6's separation, pinned in both directions."""

    store = _store(tmp_path)
    store.approve_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key="approve-command-0003",
    )
    assert store.runtime_dispatch_enabled() is False

    store.enable_runtime_activation(
        mode="permanent",
        actor_id="operator",
        idempotency_key="activation-command-0001",
    )
    assert store.runtime_dispatch_enabled() is True
    store.revoke_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key="revoke-command-0001",
    )
    # Revoking the release did not touch the gate, and the gate never spoke for
    # the release.
    assert store.runtime_dispatch_enabled() is True
    assert store.runtime_release_approved(RELEASE, DIGEST) is False


def test_the_decision_is_idempotent_when_it_is_already_in_force(
    tmp_path: Path,
) -> None:
    """Re-scoped: a retry is a repeat of the decision that is already standing.

    It used to be scoped to the key alone, which pinned the bug as correct.
    `cli._decision_key` is a pure function of (command, release_id,
    manifest_sha256), so the key an honest retry carries is byte-identical to
    the key a genuine reversal-of-a-reversal carries — see
    `test_a_reversal_carrying_a_seen_key_is_still_a_decision`. Only the standing
    decision can tell the two apart, so only it may fold one.
    """

    store = _store(tmp_path)
    key = _decision_key("approve", RELEASE, DIGEST)
    first = store.approve_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key=key,
    )
    second = store.approve_runtime_release(
        release_id=RELEASE,
        manifest_sha256=DIGEST,
        actor_id="operator",
        idempotency_key=key,
    )
    assert first == second
    assert store.runtime_release_approved(RELEASE, DIGEST) is True
    assert _counts(tmp_path) == (1, 1)


def test_a_decision_already_in_force_folds_even_under_a_fresh_key(
    tmp_path: Path,
) -> None:
    """The standing decision, not the key, is what makes a repeat a repeat."""

    store = _store(tmp_path)
    for index in range(2):
        store.approve_runtime_release(
            release_id=RELEASE,
            manifest_sha256=DIGEST,
            actor_id="operator",
            idempotency_key=f"approve-command-100{index}",
        )
    assert store.runtime_release_approved(RELEASE, DIGEST) is True
    assert _counts(tmp_path) == (1, 1)


def test_a_reversal_carrying_a_seen_key_is_still_a_decision(
    tmp_path: Path,
) -> None:
    """Both directions, keyed exactly the way the CLI keys them.

    Two identities so the two sequences share no standing decision: on DIGEST
    the operator ends at approve, on OTHER_DIGEST at revoke. Under the receipt
    replay this failed both ways — the third command in each sequence returned
    the first command's record, wrote no row and left no audit trail, while its
    caller printed success.
    """

    store = _store(tmp_path)
    decide = {
        "approve": store.approve_runtime_release,
        "revoke": store.revoke_runtime_release,
    }
    for digest, sequence in (
        (DIGEST, ("approve", "revoke", "approve")),
        (OTHER_DIGEST, ("revoke", "approve", "revoke")),
    ):
        for command in sequence:
            decide[command](
                release_id=RELEASE,
                manifest_sha256=digest,
                actor_id="operator",
                idempotency_key=_decision_key(command, RELEASE, digest),
            )
    assert store.runtime_release_approved(RELEASE, DIGEST) is True
    assert store.runtime_release_approved(RELEASE, OTHER_DIGEST) is False
    # Six decisions, six of them effective: nothing was swallowed.
    assert _counts(tmp_path) == (6, 6)


def test_release_identity_is_validated_before_it_is_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="manifest_sha256"):
        store.approve_runtime_release(
            release_id=RELEASE,
            manifest_sha256="A" * 64,
            actor_id="operator",
            idempotency_key="approve-command-0005",
        )
    with pytest.raises(ValueError, match="release_id"):
        store.approve_runtime_release(
            release_id="",
            manifest_sha256=DIGEST,
            actor_id="operator",
            idempotency_key="approve-command-0006",
        )
