"""The Control-owned transport activation gate (P5.2, D-P5-2).

Migration 12 gates `RunOrchestrator.dispatch` and nothing else; Telegram
outbound has never had a durable authorization at all. This is the schema
half of the gate that gives it one, mirrored from migration 12 rather than
generalized out of it, because that table is already in production and its
rowid order is the load-bearing part of how a decision is read back.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
)
from cortex_platform.product.control import schema as control_schema

_DECIDED_AT = "2026-09-02T12:00:00.000000Z"
_EXPIRES_AT = "2026-09-02T12:30:00.000000Z"


def _create_control_db_at_version(path: Path, version: int) -> None:
    """Build a control database frozen at `version`.

    `ControlStore.initialize()` always lands on the newest schema, so the
    states the operator actually runs before an upgrade are unreachable
    through it. Replaying migrations 1..version reproduces them exactly.
    """

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        # Byte-identical to `apply_migrations`, so a replayed database and a
        # fresh one can be compared on shape rather than on indentation.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        # Walked off `migration_scripts()` rather than `_MIGRATION_{n}`: the
        # scripts are named after what they create, not after the number they
        # were given, and a renumber moves the number only.
        for applied, script in control_schema.migration_scripts():
            if applied > version:
                break
            control_schema._execute_script_in_transaction(conn, script)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (applied, "2026-01-01T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()
    # `ControlStore` refuses a database file it cannot prove is private, and
    # `sqlite3.connect` creates one with the process umask. The companion
    # binding key is part of an existing store's on-disk state too, so a
    # replayed one has to carry it or the store refuses to open at all.
    path.chmod(0o600)
    companion = path.with_name(f".{path.name}.transport.key")
    companion.write_bytes(b"0" * 32)
    companion.chmod(0o600)


def _schema_objects(path: Path) -> list[tuple[str, str, str]]:
    """Every table, index and trigger SQLite stores, with its exact DDL."""

    conn = sqlite3.connect(path)
    try:
        return [
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        ]
    finally:
        conn.close()


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: str = "transport-activation-1",
    transport: str = "telegram",
    decision: str = "enable",
    scope: str | None = "window",
    expires_at: str | None = _EXPIRES_AT,
) -> None:
    conn.execute(
        """INSERT INTO transport_activation_decisions
           (id, transport, decision, scope, expires_at, actor_id, decided_at)
           VALUES (?, ?, ?, ?, ?, 'operator', ?)""",
        (decision_id, transport, decision, scope, expires_at, _DECIDED_AT),
    )


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(tmp_path / "control.db")
    value.initialize()
    return value


def test_the_transport_gate_migration_is_part_of_a_contiguous_schema(
    store: ControlStore,
) -> None:
    """`install.py:1973` refuses a control store with a gap in its history."""

    assert control_schema.TRANSPORT_ACTIVATION_MIGRATION <= (
        control_schema.SCHEMA_VERSION
    )
    with sqlite3.connect(store.path) as conn:
        versions = [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert versions == list(range(1, control_schema.SCHEMA_VERSION + 1))
    assert control_schema.TRANSPORT_ACTIVATION_MIGRATION in versions


def test_the_transport_gate_migration_creates_the_activation_table(
    store: ControlStore,
) -> None:
    with sqlite3.connect(store.path) as conn:
        columns = {
            str(row[1]): (str(row[2]), int(row[3]))
            for row in conn.execute(
                "PRAGMA table_info(transport_activation_decisions)"
            )
        }
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'transport_activation_decisions'"
            )
        }
    assert set(columns) == {
        "id",
        "transport",
        "decision",
        "scope",
        "expires_at",
        "actor_id",
        "decided_at",
    }
    # The nullable columns are exactly the ones a `disable` leaves empty.
    assert {name for name, (_, notnull) in columns.items() if notnull == 0} == {
        "id",
        "scope",
        "expires_at",
    }
    assert triggers == {
        "transport_activation_decisions_immutable_guard",
        "transport_activation_decisions_delete_guard",
    }


def test_the_transport_gate_migration_pins_the_transport_column_to_telegram(
    store: ControlStore,
) -> None:
    """A second transport cannot ride in on the telegram gate's decisions."""

    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_decision(conn, transport="signal")


@pytest.mark.parametrize(
    ("decision", "scope", "expires_at"),
    [
        # An enable must say how long it lasts.
        ("enable", None, None),
        # A window without an expiry is an unbounded window.
        ("enable", "window", None),
        # A permanent enable with an expiry is two answers to one question.
        ("enable", "permanent", _EXPIRES_AT),
        # A disable is unconditional: it cannot carry a scope or an expiry.
        ("disable", "window", _EXPIRES_AT),
        ("disable", "permanent", None),
        ("disable", None, _EXPIRES_AT),
        # The vocabularies are closed.
        ("pause", "window", _EXPIRES_AT),
        ("enable", "forever", None),
    ],
)
def test_the_transport_gate_migration_pins_the_decision_and_scope_checks(
    store: ControlStore,
    decision: str,
    scope: str | None,
    expires_at: str | None,
) -> None:
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_decision(
                conn, decision=decision, scope=scope, expires_at=expires_at
            )


def test_the_transport_gate_migration_accepts_the_two_shapes_the_gate_uses(
    store: ControlStore,
) -> None:
    with sqlite3.connect(store.path) as conn:
        _insert_decision(conn, decision_id="d-window")
        _insert_decision(
            conn,
            decision_id="d-permanent",
            scope="permanent",
            expires_at=None,
        )
        _insert_decision(
            conn,
            decision_id="d-disable",
            decision="disable",
            scope=None,
            expires_at=None,
        )
        assert conn.execute(
            "SELECT count(*) FROM transport_activation_decisions"
        ).fetchone()[0] == 3


def test_the_transport_gate_migration_adds_a_write_once_window_id_to_chunks(
    store: ControlStore,
) -> None:
    """AMD-5 lands the window id on the outbound row, not in a JSON body.

    The outbound receipt is `provider_receipt_digest`, an HMAC; there is no
    `response_json` on the send path to carry the id, so it gets a column of
    its own. Nullable, because every chunk frozen before this migration and
    every chunk sent outside a window legitimately has none.
    """

    with sqlite3.connect(store.path) as conn:
        column = next(
            row
            for row in conn.execute("PRAGMA table_info(transport_delivery_chunks)")
            if str(row[1]) == "transport_window_id"
        )
        assert (str(column[2]), int(column[3]), column[4]) == ("TEXT", 0, None)
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'transport_delivery_chunks_window_immutable_guard'"
        ).fetchone()[0] == 1
        foreign_keys = {
            str(row[2]): str(row[3])
            for row in conn.execute(
                "PRAGMA foreign_key_list(transport_delivery_chunks)"
            )
        }
    assert foreign_keys["transport_activation_decisions"] == "transport_window_id"


def test_the_migrations_above_twelve_land_the_same_state_in_either_order(
    tmp_path: Path,
) -> None:
    """AMD-8: the operator's schema 12 skips 13, so everything above it applies together.

    The mini is on schema 12 and gen 8 ships 13; a database that upgrades
    across every later migration in one `initialize()` must end byte-identical
    to one that took 13 first and the rest later, or the gate's shape would
    depend on when the operator happened to run an installer.
    """

    from_twelve = tmp_path / "twelve/control.db"
    _create_control_db_at_version(from_twelve, 12)
    ControlStore(from_twelve).initialize()

    from_thirteen = tmp_path / "thirteen/control.db"
    _create_control_db_at_version(from_thirteen, 13)
    ControlStore(from_thirteen).initialize()

    fresh = tmp_path / "fresh/control.db"
    fresh.parent.mkdir(mode=0o700, parents=True)
    ControlStore(fresh).initialize()

    assert _schema_objects(from_twelve) == _schema_objects(from_thirteen)
    assert _schema_objects(from_twelve) == _schema_objects(fresh)
    for path in (from_twelve, from_thirteen, fresh):
        with sqlite3.connect(path) as conn:
            assert [
                int(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ] == list(range(1, control_schema.SCHEMA_VERSION + 1))


def test_a_schema_12_store_keeps_its_rows_across_both_migrations(
    tmp_path: Path,
) -> None:
    """Migration 12's own decisions are not disturbed by its mirror."""

    path = tmp_path / "control.db"
    _create_control_db_at_version(path, 12)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO runtime_activation_decisions
               (id, decision, mode, expires_at, actor_id, decided_at)
               VALUES ('runtime-1', 'enable', 'permanent', NULL, 'operator', ?)""",
            (_DECIDED_AT,),
        )
        conn.commit()
    finally:
        conn.close()

    ControlStore(path).initialize()

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT id, decision, mode FROM runtime_activation_decisions"
        ).fetchall() == [("runtime-1", "enable", "permanent")]
        assert conn.execute(
            "SELECT count(*) FROM transport_activation_decisions"
        ).fetchone()[0] == 0


def test_an_interrupted_transport_gate_migration_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """Every migration boundary in this store is pinned this way.

    A migration that half-applies is the worst failure mode a control store
    has: the next start sees a version it never recorded and a table it did
    not create.
    """

    path = tmp_path / "control.db"
    _create_control_db_at_version(
        path, control_schema.TRANSPORT_ACTIVATION_MIGRATION - 1
    )
    conn = sqlite3.connect(path)
    try:
        original = control_schema._MIGRATION_TRANSPORT_ACTIVATION
        control_schema._MIGRATION_TRANSPORT_ACTIVATION = (
            original + "\nCREATE TABLE interrupted_transport_migration(id TEXT);\n"
        )
        try:
            with pytest.raises(sqlite3.OperationalError):
                # A second CREATE of the same name inside the same script
                # aborts halfway through the migration.
                control_schema._MIGRATION_TRANSPORT_ACTIVATION = (
                    original
                    + "\nCREATE TABLE transport_activation_decisions(id TEXT);\n"
                )
                control_schema.apply_migrations(
                    conn, now="2026-09-02T12:00:00.000000Z"
                )
        finally:
            control_schema._MIGRATION_TRANSPORT_ACTIVATION = original
        assert [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == list(range(1, control_schema.TRANSPORT_ACTIVATION_MIGRATION))
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = "
            "'transport_activation_decisions'"
        ).fetchone()[0] == 0

        control_schema.apply_migrations(conn, now="2026-09-02T12:00:01.000000Z")
        assert [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == list(control_schema.MIGRATION_VERSIONS)
    finally:
        conn.close()


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            return f"{kind}-{self._counts[kind]}"


_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def gated(tmp_path: Path) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db",
        clock=lambda: _NOW,
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value


@pytest.fixture
def moving(tmp_path: Path) -> tuple[ControlStore, dict[str, datetime]]:
    clock = {"now": _NOW}
    value = ControlStore(
        tmp_path / "control.db",
        clock=lambda: clock["now"],
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value, clock


class TestDefaultOff:
    def test_a_fresh_store_does_not_authorize_telegram(
        self, gated: ControlStore
    ) -> None:
        assert gated.transport_activation("telegram") is None
        assert gated.telegram_dispatch_enabled() is False

    def test_the_runtime_gate_does_not_open_the_transport_gate(
        self, gated: ControlStore
    ) -> None:
        """The two gates guard different code paths and share no state.

        Migration 12's gate is what `RunOrchestrator.dispatch` consults; a
        product running dispatch has not thereby authorized a Telegram send.
        """

        gated.enable_runtime_activation(
            mode="permanent",
            actor_id="operator",
            idempotency_key="activate-key-000001",
        )
        assert gated.runtime_dispatch_enabled() is True
        assert gated.telegram_dispatch_enabled() is False

    def test_an_unknown_transport_is_refused(self, gated: ControlStore) -> None:
        for transport in ("signal", "", "TELEGRAM", None):
            with pytest.raises(ValueError, match="transport"):
                gated.transport_activation(transport)  # type: ignore[arg-type]


class TestDecisions:
    def test_a_window_authorizes_until_it_expires(
        self, moving: tuple[ControlStore, dict[str, datetime]]
    ) -> None:
        store, clock = moving
        record = store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        assert (record.transport, record.scope) == ("telegram", "window")
        assert record.expires_at == _NOW + timedelta(seconds=600)
        assert store.telegram_dispatch_enabled() is True

        # The boundary is inclusive on the closing side: at the expiry the
        # window is already over, exactly as migration 12's read decides it.
        clock["now"] = _NOW + timedelta(seconds=600)
        assert store.telegram_dispatch_enabled() is False

    def test_the_second_before_expiry_still_authorizes(
        self, moving: tuple[ControlStore, dict[str, datetime]]
    ) -> None:
        store, clock = moving
        store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        clock["now"] = _NOW + timedelta(seconds=599)
        assert store.telegram_dispatch_enabled() is True

    def test_an_expired_window_cannot_be_extended(
        self, moving: tuple[ControlStore, dict[str, datetime]]
    ) -> None:
        """Expiry is a stored fact; only a new decision gets past it."""

        store, clock = moving
        first = store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=60,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        clock["now"] = _NOW + timedelta(seconds=61)
        assert store.telegram_dispatch_enabled() is False
        # Replaying the same request returns the same expired decision rather
        # than minting a fresh window under the used key.
        replay = store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=60,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        assert replay == first
        assert store.telegram_dispatch_enabled() is False

        store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=60,
            actor_id="operator",
            idempotency_key="tg-window-000000002",
        )
        assert store.telegram_dispatch_enabled() is True

    def test_disable_ends_a_permanent_enable(self, gated: ControlStore) -> None:
        gated.enable_transport_activation(
            transport="telegram",
            scope="permanent",
            actor_id="operator",
            idempotency_key="tg-permanent-0000001",
        )
        assert gated.telegram_dispatch_enabled() is True
        gated.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="tg-disable-000000001",
        )
        assert gated.telegram_dispatch_enabled() is False

    def test_the_latest_decision_decides_within_one_clock_tick(
        self, gated: ControlStore
    ) -> None:
        """Every row here shares a `decided_at`; rowid still orders them.

        A real clock can produce two decisions in one tick too, and then an
        operator's disable could be silently outvoted by the enable it was
        meant to revoke.
        """

        for index in range(3):
            gated.enable_transport_activation(
                transport="telegram",
                scope="permanent",
                actor_id="operator",
                idempotency_key=f"tg-enable-{index:09d}",
            )
            assert gated.telegram_dispatch_enabled() is True
            gated.disable_transport_activation(
                transport="telegram",
                actor_id="operator",
                idempotency_key=f"tg-disable-{index:08d}",
            )
            assert gated.telegram_dispatch_enabled() is False

    def test_the_window_bound_is_half_an_hour(self, gated: ControlStore) -> None:
        """Migration 12 allows a week; this gate holds a shared token."""

        gated.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=1_800,
            actor_id="operator",
            idempotency_key="tg-window-bound-0001",
        )
        for seconds in (0, -1, 1_801, 604_800):
            with pytest.raises(ValueError, match="window_seconds"):
                gated.enable_transport_activation(
                    transport="telegram",
                    scope="window",
                    window_seconds=seconds,
                    actor_id="operator",
                    idempotency_key=f"tg-bad-{seconds:013d}",
                )

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"scope": "window"}, "window_seconds"),
            ({"scope": "permanent", "window_seconds": 60}, "window_seconds"),
            ({"scope": "forever"}, "scope"),
            ({"scope": "window", "window_seconds": True}, "window_seconds"),
        ],
    )
    def test_typed_refusals(
        self, gated: ControlStore, kwargs: dict[str, object], match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            gated.enable_transport_activation(
                transport="telegram",
                actor_id="operator",
                idempotency_key="tg-refusal-00000001",
                **kwargs,  # type: ignore[arg-type]
            )

    def test_a_different_request_under_one_key_is_refused(
        self, gated: ControlStore
    ) -> None:
        gated.enable_transport_activation(
            transport="telegram",
            scope="permanent",
            actor_id="operator",
            idempotency_key="tg-permanent-0000001",
        )
        with pytest.raises(IdempotencyConflict):
            gated.enable_transport_activation(
                transport="telegram",
                scope="window",
                window_seconds=60,
                actor_id="operator",
                idempotency_key="tg-permanent-0000001",
            )

    def test_the_decision_survives_a_restart(self, tmp_path: Path) -> None:
        store = ControlStore(
            tmp_path / "control.db",
            clock=lambda: _NOW,
            id_factory=DeterministicIds(),
        )
        store.initialize()
        store.enable_transport_activation(
            transport="telegram",
            scope="permanent",
            actor_id="operator",
            idempotency_key="tg-permanent-0000001",
        )
        assert ControlStore(store.path).telegram_dispatch_enabled() is True


class TestImmutability:
    def test_the_history_is_append_only(self, gated: ControlStore) -> None:
        gated.enable_transport_activation(
            transport="telegram",
            scope="permanent",
            actor_id="operator",
            idempotency_key="tg-permanent-0000001",
        )
        gated.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="tg-disable-000000001",
        )
        with sqlite3.connect(gated.path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM transport_activation_decisions"
            ).fetchone()[0] == 2
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("DELETE FROM transport_activation_decisions")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(
                    "UPDATE transport_activation_decisions SET decision = 'enable'"
                )
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(
                    "UPDATE transport_activation_decisions SET expires_at = NULL"
                )


class TestWindowEndings:
    def test_a_closed_window_records_its_release_proof(
        self, gated: ControlStore
    ) -> None:
        window = gated.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        gated.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="tg-disable-000000001",
        )
        gated.record_transport_window_closed(
            window_id=window.id,
            poller_stopped=True,
            proof="supervisor closed and process.poll() is not None",
            actor_id="operator",
        )
        with sqlite3.connect(gated.path) as conn:
            rows = conn.execute(
                "SELECT aggregate_id, type, payload_json FROM control_audit "
                "WHERE aggregate_type = 'transport_window'"
            ).fetchall()
        assert len(rows) == 1
        assert (rows[0][0], rows[0][1]) == (window.id, "transport_window_closed")
        payload = json.loads(rows[0][2])
        assert payload["poller_stopped"] is True
        assert payload["proof"].startswith("supervisor closed")

    def test_a_window_cannot_be_recorded_closed_while_it_is_open(
        self, gated: ControlStore
    ) -> None:
        """Step 5 disables by decision and only then records the close.

        Recording a close while the gate still authorizes a send would put a
        release proof in the audit trail for a token still being held.
        """

        window = gated.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        with pytest.raises(InvalidTransition):
            gated.record_transport_window_closed(
                window_id=window.id,
                poller_stopped=True,
                proof="supervisor closed",
                actor_id="operator",
            )

    def test_a_close_needs_a_proof_and_a_real_window(
        self, gated: ControlStore
    ) -> None:
        permanent = gated.enable_transport_activation(
            transport="telegram",
            scope="permanent",
            actor_id="operator",
            idempotency_key="tg-permanent-0000001",
        )
        gated.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="tg-disable-000000001",
        )
        # `poller_stopped` is derived at the call site, so the store makes the
        # caller say what it observed rather than accepting a bare boolean.
        with pytest.raises(ValueError, match="proof"):
            gated.record_transport_window_closed(
                window_id=permanent.id,
                poller_stopped=True,
                proof="   ",
                actor_id="operator",
            )
        with pytest.raises(InvalidTransition):
            gated.record_transport_window_closed(
                window_id=permanent.id,
                poller_stopped=True,
                proof="supervisor closed",
                actor_id="operator",
            )
        with pytest.raises(NotFound):
            gated.record_transport_window_closed(
                window_id="transport-activation-99",
                poller_stopped=False,
                proof="no window",
                actor_id="operator",
            )

    def test_an_open_window_is_aborted_and_closed_by_the_next_start(
        self, gated: ControlStore
    ) -> None:
        """The crash path: no close row, so the next start writes the abort."""

        window = gated.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        assert gated.telegram_dispatch_enabled() is True

        assert gated.abort_open_transport_windows(
            reason="product_start", actor_id="cortexd"
        ) == (window.id,)
        assert gated.telegram_dispatch_enabled() is False
        with sqlite3.connect(gated.path) as conn:
            rows = conn.execute(
                "SELECT aggregate_id, type FROM control_audit "
                "WHERE aggregate_type = 'transport_window'"
            ).fetchall()
        assert rows == [(window.id, "transport_window_aborted")]

        # The second start has nothing left to account for.
        assert gated.abort_open_transport_windows(
            reason="product_start", actor_id="cortexd"
        ) == ()

    def test_an_expired_unclosed_window_is_still_aborted(
        self, moving: tuple[ControlStore, dict[str, datetime]]
    ) -> None:
        """A window that expired while the product was down was never closed.

        Letting it disappear into its own expiry would leave the only record
        of a token the product held saying nothing about how it ended.
        """

        store, clock = moving
        window = store.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=60,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        clock["now"] = _NOW + timedelta(seconds=600)
        assert store.telegram_dispatch_enabled() is False
        assert store.abort_open_transport_windows(
            reason="product_start", actor_id="cortexd"
        ) == (window.id,)

    def test_a_properly_closed_window_is_never_aborted(
        self, gated: ControlStore
    ) -> None:
        window = gated.enable_transport_activation(
            transport="telegram",
            scope="window",
            window_seconds=600,
            actor_id="operator",
            idempotency_key="tg-window-000000001",
        )
        gated.disable_transport_activation(
            transport="telegram",
            actor_id="operator",
            idempotency_key="tg-disable-000000001",
        )
        gated.record_transport_window_closed(
            window_id=window.id,
            poller_stopped=True,
            proof="no api.telegram.org connection on the worker pid",
            actor_id="operator",
        )
        assert gated.abort_open_transport_windows(
            reason="product_start", actor_id="cortexd"
        ) == ()

    def test_a_permanent_enable_is_not_a_window_to_abort(
        self, gated: ControlStore
    ) -> None:
        gated.enable_transport_activation(
            transport="telegram",
            scope="permanent",
            actor_id="operator",
            idempotency_key="tg-permanent-0000001",
        )
        assert gated.abort_open_transport_windows(
            reason="product_start", actor_id="cortexd"
        ) == ()
        assert gated.telegram_dispatch_enabled() is True
