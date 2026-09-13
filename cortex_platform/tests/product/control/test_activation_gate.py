"""The Control-owned runtime activation gate (program plan A1-1).

`capabilities.runtime_dispatch` at `product/api/app.py` is health REPORTING of
what the BUILD can do -- drive a run the API created -- so it gates nothing and
never moves. This is the gate that does: durable, default-off, checked before
orchestration touches the runtime at all, and reported separately as the
tri-state `runtime_dispatch_enabled`.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
)

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


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
        clock=lambda: _NOW,
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value


class TestDefaultOff:
    def test_a_fresh_store_does_not_authorize_dispatch(
        self, store: ControlStore
    ) -> None:
        assert store.runtime_activation() is None
        assert store.runtime_dispatch_enabled() is False

    def test_the_projection_says_why(self, store: ControlStore) -> None:
        assert store.runtime_activation_report() == {
            "enabled": False,
            "mode": None,
            "expires_at": None,
        }


class TestPermanentEnable:
    def test_enabling_permanently_authorizes_dispatch(
        self, store: ControlStore
    ) -> None:
        record = store.enable_runtime_activation(
            mode="permanent",
            actor_id="operator",
            idempotency_key="activate-key-000001",
        )
        assert record.mode == "permanent"
        assert store.runtime_dispatch_enabled() is True

    def test_enabling_is_idempotent(self, store: ControlStore) -> None:
        first = store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        second = store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        assert first == second

    def test_a_different_request_under_one_key_is_refused(
        self, store: ControlStore
    ) -> None:
        store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        with pytest.raises(IdempotencyConflict):
            store.enable_runtime_activation(
                mode="window",
                window_seconds=60,
                actor_id="operator",
                idempotency_key="activate-key-000001",
            )

    def test_disabling_takes_it_away_again(self, store: ControlStore) -> None:
        store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        store.disable_runtime_activation(
            actor_id="operator", idempotency_key="deactivate-key-00001"
        )
        assert store.runtime_dispatch_enabled() is False


class TestBoundedWindow:
    def test_a_window_authorizes_until_it_expires(self, store: ControlStore) -> None:
        store.enable_runtime_activation(
            mode="window",
            window_seconds=300,
            actor_id="operator",
            idempotency_key="activate-window-0001",
        )
        assert store.runtime_dispatch_enabled() is True

    def test_an_expired_window_no_longer_authorizes(self, tmp_path: Path) -> None:
        clock = {"now": _NOW}
        store = ControlStore(
            tmp_path / "control.db",
            clock=lambda: clock["now"],
            id_factory=DeterministicIds(),
        )
        store.initialize()
        store.enable_runtime_activation(
            mode="window",
            window_seconds=60,
            actor_id="operator",
            idempotency_key="activate-window-0001",
        )
        assert store.runtime_dispatch_enabled() is True
        clock["now"] = _NOW + timedelta(seconds=61)
        # Expiry is a stored fact compared against the clock, never a timer:
        # a window must not survive a restart that outlasts it.
        assert store.runtime_dispatch_enabled() is False

    def test_a_window_is_refused_without_a_duration(self, store: ControlStore) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            store.enable_runtime_activation(
                mode="window", actor_id="operator", idempotency_key="activate-key-w000002"
            )

    def test_a_permanent_enable_is_refused_with_a_duration(
        self, store: ControlStore
    ) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            store.enable_runtime_activation(
                mode="permanent",
                window_seconds=60,
                actor_id="operator",
                idempotency_key="activate-key-w000003",
            )

    def test_an_unbounded_window_is_refused(self, store: ControlStore) -> None:
        for seconds in (0, -1, 60 * 60 * 24 * 8):
            with pytest.raises(ValueError):
                store.enable_runtime_activation(
                    mode="window",
                    window_seconds=seconds,
                    actor_id="operator",
                    idempotency_key=f"activate-key-w{seconds}".ljust(20, "x")[:24],
                )

    def test_an_unknown_mode_is_refused(self, store: ControlStore) -> None:
        with pytest.raises(ValueError):
            store.enable_runtime_activation(
                mode="forever", actor_id="operator", idempotency_key="activate-key-w000009"
            )


class TestDurability:
    def test_the_decision_survives_a_restart(self, store: ControlStore) -> None:
        store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        assert ControlStore(store.path).runtime_dispatch_enabled() is True

    def test_the_history_is_append_only(self, store: ControlStore) -> None:
        store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        store.disable_runtime_activation(
            actor_id="operator", idempotency_key="deactivate-key-00001"
        )
        with sqlite3.connect(store.path) as conn:
            assert conn.execute(
                "select count(*) from runtime_activation_decisions"
            ).fetchone()[0] == 2
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("delete from runtime_activation_decisions")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "update runtime_activation_decisions set mode = 'permanent'"
                )

    def test_the_latest_decision_decides(self, store: ControlStore) -> None:
        store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000001"
        )
        store.disable_runtime_activation(
            actor_id="operator", idempotency_key="deactivate-key-00001"
        )
        store.enable_runtime_activation(
            mode="permanent", actor_id="operator", idempotency_key="activate-key-000002"
        )
        assert store.runtime_dispatch_enabled() is True

    def test_decisions_in_one_clock_tick_still_order_correctly(
        self, store: ControlStore
    ) -> None:
        """Two decisions can share a timestamp; the later one must still win.

        The fixture clock is fixed, so every row here has an identical
        `decided_at`. Ordering by that column made this flaky -- and it is not
        only a test artifact: a real clock can produce two decisions in one
        tick too, and then an operator's disable could be silently outvoted by
        the enable it was meant to revoke.
        """

        for index in range(3):
            store.enable_runtime_activation(
                mode="permanent",
                actor_id="operator",
                idempotency_key=f"activate-key-{index:06d}".ljust(20, "x"),
            )
            assert store.runtime_dispatch_enabled() is True
            store.disable_runtime_activation(
                actor_id="operator",
                idempotency_key=f"deactivate-key-{index:06d}".ljust(20, "x"),
            )
            assert store.runtime_dispatch_enabled() is False
