"""A1-8: the health payload's Telegram fields are derived, not asserted.

`telegram_adapter` was a hardcoded `False` with nothing behind it, so the
step that eventually turns it true could have been an edit to one line while
no adapter existed. It now derives from three facts that all have to hold --
an adapter was constructed, a managed worker is bound to serve its frames, and
the durable gate authorizes them -- and none of them lives in this file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore

TOKEN = "x" * 48


class _BoundWorker:
    """A stand-in for `ManagedTransportWorker`, with the two things health asks."""

    def __init__(self, *, bound: bool = True, reason: str | None = None) -> None:
        self.bound = bound
        self._reason = reason

    def health(self):
        payload = {
            "state": "bound" if self.bound else "unbound",
            "reason": self._reason,
            "release_id": "hermes-0.15.0-gen9" if self.bound else None,
            "slot_digest": "b" * 64 if self.bound else None,
            "launched": False,
        }
        return type("_Health", (), {"to_dict": lambda self: payload})()


def _store(tmp_path: Path, clock) -> ControlStore:
    value = ControlStore(tmp_path / "control.db", clock=clock)
    value.initialize()
    return value


def _capabilities(api: ControlAPI) -> dict:
    response = api.handle(method="GET", target="/api/v1/health", headers={})
    assert response.status == 200
    return dict(response.payload["capabilities"])


def test_the_adapter_boolean_is_false_while_nothing_constructs_one(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)
    api = ControlAPI(store, access_token=TOKEN)

    assert _capabilities(api)["telegram_adapter"] is False

    # Opening the gate is not enough, and this is the whole point of the
    # derivation: authorizing a transport that has no adapter must not make
    # health claim the product can send.
    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    assert store.telegram_dispatch_enabled() is True
    assert _capabilities(api)["telegram_adapter"] is False


def test_the_adapter_boolean_follows_the_gate_once_an_adapter_exists(
    tmp_path: Path,
) -> None:
    """The P5.3 shape, proven now with a stand-in for the adapter.

    Health only reports that an adapter was constructed; it never reaches
    into one. Any object is therefore a faithful stand-in for the half of
    the derivation P5.3 fills in.
    """

    clock = {"now": datetime(2026, 9, 2, 12, 0, tzinfo=UTC)}
    store = _store(tmp_path, lambda: clock["now"])
    api = ControlAPI(
        store,
        access_token=TOKEN,
        telegram_adapter=object(),
        managed_worker=_BoundWorker(),
    )

    assert _capabilities(api)["telegram_adapter"] is False

    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    assert _capabilities(api)["telegram_adapter"] is True

    clock["now"] += timedelta(seconds=600)
    assert _capabilities(api)["telegram_adapter"] is False

    store.enable_transport_activation(
        transport="telegram",
        scope="permanent",
        actor_id="operator",
        idempotency_key="tg-permanent-0000001",
    )
    assert _capabilities(api)["telegram_adapter"] is True

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="tg-disable-000000001",
    )
    assert _capabilities(api)["telegram_adapter"] is False


def test_the_mode_is_reported_beside_the_boolean_and_defaults_to_shadow(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)

    assert _capabilities(ControlAPI(store, access_token=TOKEN))[
        "telegram_mode"
    ] == "shadow"
    assert _capabilities(
        ControlAPI(store, access_token=TOKEN, telegram_mode="active")
    )["telegram_mode"] == "active"

    with pytest.raises(ValueError, match="telegram_mode"):
        ControlAPI(store, access_token=TOKEN, telegram_mode="loud")


def test_health_does_not_read_the_store_before_an_adapter_exists(
    tmp_path: Path,
) -> None:
    """Health is served before authentication, so it stays cheap.

    The store read is the LAST conjunct, so the unauthenticated endpoint opens
    no database connection while there is no adapter and no bound worker to
    report on.
    """

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)

    reads: list[str] = []
    original = ControlStore.telegram_dispatch_enabled

    def _counted(self: ControlStore) -> bool:
        reads.append("read")
        return original(self)

    ControlStore.telegram_dispatch_enabled = _counted  # type: ignore[method-assign]
    try:
        _capabilities(ControlAPI(store, access_token=TOKEN))
        assert reads == []
        _capabilities(
            ControlAPI(store, access_token=TOKEN, telegram_adapter=object())
        )
        assert reads == []
        _capabilities(
            ControlAPI(
                store,
                access_token=TOKEN,
                telegram_adapter=object(),
                managed_worker=_BoundWorker(),
            )
        )
        assert reads == ["read"]
    finally:
        ControlStore.telegram_dispatch_enabled = original  # type: ignore[method-assign]


def test_an_unbound_worker_keeps_the_boolean_false_and_names_the_reason(
    tmp_path: Path,
) -> None:
    """P5.4: an open gate over a release nobody approved is not a live adapter."""

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)
    api = ControlAPI(
        store,
        access_token=TOKEN,
        telegram_adapter=object(),
        managed_worker=_BoundWorker(bound=False, reason="release_not_approved"),
    )
    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    assert _capabilities(api)["telegram_adapter"] is False
    payload = api.handle(
        method="GET", target="/api/v1/health", headers={}
    ).payload["managed_worker"]
    assert payload["state"] == "unbound"
    assert payload["reason"] == "release_not_approved"
    assert payload["launched"] is False


def test_a_daemon_with_no_binding_at_all_says_so_rather_than_naming_a_reason(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)
    payload = ControlAPI(store, access_token=TOKEN).handle(
        method="GET", target="/api/v1/health", headers={}
    ).payload["managed_worker"]
    assert payload == {
        "state": "unbound",
        "reason": "managed_worker_unconfigured",
        "release_id": None,
        "slot_digest": None,
        "launched": False,
    }


def test_the_bound_release_identity_is_reported_without_a_path(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)
    payload = ControlAPI(
        store, access_token=TOKEN, managed_worker=_BoundWorker()
    ).handle(method="GET", target="/api/v1/health", headers={}).payload[
        "managed_worker"
    ]
    assert payload["state"] == "bound"
    assert payload["release_id"] == "hermes-0.15.0-gen9"
    assert payload["slot_digest"] == "b" * 64
    assert "/" not in json.dumps(payload)


def test_the_dispatch_gate_is_read_only_when_a_bridge_can_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦V6-4⟧ Ordered like `telegram_adapter`: the gate row is read only with a bridge.

    The health branch answers before authentication, so a store read there
    is a read any caller can cause. With no bridge, or a worker the daemon
    could not bind, the field is `null` and the dispatch-gate row is not
    read; with both, it is the gate. ⟦N-5⟧ Only that row is pinned here: the
    `managed_worker` field of the same payload asks the worker's health,
    which on the real worker reads the release-approval row -- a read the
    `_BoundWorker` stub below cannot show.
    """

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)
    reads: list[int] = []
    real = store.runtime_dispatch_enabled
    monkeypatch.setattr(
        store, "runtime_dispatch_enabled", lambda: (reads.append(1), real())[1]
    )

    def health(api: ControlAPI) -> dict:
        response = api.handle(method="GET", target="/api/v1/health", headers={})
        assert response.status == 200
        return dict(response.payload)

    assert health(ControlAPI(store, access_token=TOKEN))["runtime_dispatch_enabled"] is None
    unbound = ControlAPI(
        store,
        access_token=TOKEN,
        managed_worker=_BoundWorker(bound=False, reason="release_not_approved"),
    )
    assert health(unbound)["runtime_dispatch_enabled"] is None
    assert reads == []

    class _Bridge:
        def status(self) -> dict:
            return {"state": "running", "queued": 0}

    api = ControlAPI(
        store, access_token=TOKEN, managed_worker=_BoundWorker(), turn_bridge=_Bridge()
    )
    assert health(api)["runtime_dispatch_enabled"] is False
    assert reads == [1]
    store.enable_runtime_activation(
        mode="permanent", actor_id="operator", idempotency_key="activation-000000001"
    )
    assert health(api)["runtime_dispatch_enabled"] is True


def test_the_dispatch_capability_says_what_the_build_can_do_not_whether_it_is_on(
    tmp_path: Path,
) -> None:
    """⟦P9-2⟧ `capabilities.runtime_dispatch` and `runtime_dispatch_enabled`.

    The capability is what this build knows how to do -- drive a run the API
    created -- and it does not move when the gate does. Before P9-2 it was a
    literal `False` left over from P8's "not done" marker, so the honest
    reading of the pair was impossible: the capability said no while the
    daemon was answering turns.
    """

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    store = _store(tmp_path, lambda: now)

    def health(api: ControlAPI) -> dict:
        response = api.handle(method="GET", target="/api/v1/health", headers={})
        assert response.status == 200
        return dict(response.payload)

    class _Bridge:
        def status(self) -> dict:
            return {"state": "running", "queued": 0}

    # Nothing wired: the gate cannot be reported at all, and the capability is
    # still true -- the build can drive, this daemon is not set up to.
    bare = health(ControlAPI(store, access_token=TOKEN))
    assert bare["capabilities"]["runtime_dispatch"] is True
    assert bare["runtime_dispatch_enabled"] is None

    api = ControlAPI(
        store, access_token=TOKEN, managed_worker=_BoundWorker(), turn_bridge=_Bridge()
    )
    # Wired, gate closed: capable and off, the ordinary shape.
    closed = health(api)
    assert closed["capabilities"]["runtime_dispatch"] is True
    assert closed["runtime_dispatch_enabled"] is False

    store.enable_runtime_activation(
        mode="permanent", actor_id="operator", idempotency_key="activation-000000002"
    )
    opened = health(api)
    assert opened["capabilities"]["runtime_dispatch"] is True
    assert opened["runtime_dispatch_enabled"] is True

    # The capability is a build constant, so it is the one value that is the
    # same in all three readings above.
    assert (
        len({
            reading["capabilities"]["runtime_dispatch"]
            for reading in (bare, closed, opened)
        })
        == 1
    )
