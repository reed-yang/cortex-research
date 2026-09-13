"""What a product start does before it serves anything.

The transport window is the only piece of control state whose correctness
depends on a process that may not have exited cleanly, so the start path is
where the product has to account for it (D-P5-5).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product import daemon as daemon_module
from cortex_platform.product.control import ControlStore


class _ImmediateServer:
    """A server that stops the loop the first time it is asked to serve."""

    def __init__(self, stop_requested) -> None:
        self.server_address = ("127.0.0.1", 51_234)
        self.timeout = 0.0
        self._stop_requested = stop_requested

    def handle_request(self) -> None:
        self._stop_requested.set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def server_close(self) -> None:
        return None


def _arguments(root: Path) -> list[str]:
    return [
        "--instance-id",
        "test-instance",
        "--config-dir",
        str(root / "config"),
        "--data-dir",
        str(root / "data"),
        "--state-dir",
        str(root / "state"),
        "--cache-dir",
        str(root / "cache"),
        "--log-dir",
        str(root / "log"),
    ]


def test_a_product_start_aborts_a_window_it_cannot_prove_was_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash path, exercised through `daemon.main` rather than described.

    A window that outlived the process that opened it leaves no
    `transport_window_closed` row. The next start writes the abort and takes
    the gate back down, so a restart the product cannot account for does not
    leave an open authorization behind it.
    """

    captured: dict[str, object] = {}

    def _server(**kwargs: object) -> _ImmediateServer:
        captured.update(kwargs)
        return _ImmediateServer(kwargs["stop_requested"])

    monkeypatch.setattr(daemon_module, "create_server", _server)

    arguments = _arguments(tmp_path)
    assert daemon_module.main(arguments) == 0

    control = tmp_path / "data" / "control.db"
    store = ControlStore(control)
    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    assert store.telegram_dispatch_enabled() is True

    assert daemon_module.main(arguments) == 0

    assert ControlStore(control).telegram_dispatch_enabled() is False
    with sqlite3.connect(control) as conn:
        rows = conn.execute(
            "SELECT type FROM control_audit WHERE aggregate_type = 'transport_window'"
        ).fetchall()
    assert rows == [("transport_window_aborted",)]


def _configure_telegram(root: Path, **extra: str) -> None:
    from cortex_platform.product.config import write_config

    config: dict = {
        "config_version": 1,
        "transports": {
            "telegram_bot_identity": "recursive_research_bot",
            "telegram_base_url": "https://cortex.example/open",
            "telegram_allowed_user_ids": "4242",
            **extra,
        },
    }
    write_config(root / "config" / "config.toml", config)


def _health(root: Path) -> dict:
    import http.client

    metadata = json.loads(
        (root / "state" / "cortexd.json").read_text()
        if (root / "state" / "cortexd.json").exists()
        else "{}"
    )
    connection = http.client.HTTPConnection(
        metadata["host"], int(metadata["port"]), timeout=10
    )
    try:
        connection.request("GET", "/api/v1/health")
        return json.loads(connection.getresponse().read())
    finally:
        connection.close()


def test_an_unconfigured_product_constructs_no_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1-8: false because there is nothing to report, not because of a literal."""

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        daemon_module,
        "create_server",
        lambda **kwargs: (captured.update(kwargs), _ImmediateServer(kwargs["stop_requested"]))[1],
    )
    assert daemon_module.main(_arguments(tmp_path)) == 0
    api = captured["control_api"]
    assert api._telegram_adapter is None
    assert api._telegram_mode == "shadow"


def test_a_configured_product_constructs_the_adapter_in_shadow_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex_platform.product.transports.telegram import TelegramAdapter

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    _configure_telegram(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        daemon_module,
        "create_server",
        lambda **kwargs: (captured.update(kwargs), _ImmediateServer(kwargs["stop_requested"]))[1],
    )
    assert daemon_module.main(_arguments(tmp_path)) == 0
    api = captured["control_api"]
    assert isinstance(api._telegram_adapter, TelegramAdapter)
    assert api._telegram_adapter.config.mode == "shadow"
    # Constructed, but the gate is shut, so health still says false.
    assert (
        api.handle(method="GET", target="/api/v1/health", headers={})
        .payload["capabilities"]["telegram_adapter"]
        is False
    )


def test_an_open_gate_over_no_active_release_does_not_claim_an_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1-8 closed, and P5.4 adds the third conjunct.

    This installation has activated no runtime, so the daemon binds no managed
    worker and every transport frame would be refused. Opening the gate on top
    of that must not make health claim the product can send -- the case the
    two-conjunct derivation reported as `true`.
    """

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    _configure_telegram(tmp_path, telegram_mode="active")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        daemon_module,
        "create_server",
        lambda **kwargs: (captured.update(kwargs), _ImmediateServer(kwargs["stop_requested"]))[1],
    )
    assert daemon_module.main(_arguments(tmp_path)) == 0
    api = captured["control_api"]
    store = ControlStore(tmp_path / "data" / "control.db")

    def reported() -> dict:
        return api.handle(
            method="GET", target="/api/v1/health", headers={}
        ).payload

    assert reported()["capabilities"]["telegram_adapter"] is False
    assert reported()["capabilities"]["telegram_mode"] == "active"
    assert reported()["managed_worker"] == {
        "state": "unbound",
        "reason": "no_active_release",
        "release_id": None,
        "slot_digest": None,
        "launched": False,
    }

    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    assert reported()["capabilities"]["telegram_adapter"] is False

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="tg-disable-000000001",
    )
    assert reported()["capabilities"]["telegram_adapter"] is False


def test_an_unbound_daemon_refuses_a_frame_rather_than_launching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_UnboundWorker` is still what an unbindable installation gets."""

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    _configure_telegram(tmp_path, telegram_mode="active")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        daemon_module,
        "create_server",
        lambda **kwargs: (captured.update(kwargs), _ImmediateServer(kwargs["stop_requested"]))[1],
    )
    assert daemon_module.main(_arguments(tmp_path)) == 0
    adapter = captured["control_api"]._telegram_adapter
    rpc = adapter._chunk_client._rpc
    assert isinstance(rpc._supervisor, daemon_module._UnboundWorker)
    with pytest.raises(RuntimeError, match="not bound"):
        rpc.request("telegram.capabilities", {}, timeout=1.0)


# ⟦P8⟧ ------------------------------------------ the API reaches the turn bridge


def _control_headers(token: str, *, key: str | None = None) -> dict[str, str]:
    headers = {"X-Cortex-Control-Token": token}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _post(api, token: str, path: str, payload: dict, *, key: str):
    return api.handle(
        method="POST",
        target=path,
        headers=_control_headers(token, key=key),
        body=json.dumps(payload).encode(),
    )


def _cockpit_thread(api, token: str) -> dict:
    """What the cockpit does before it asks for a run: a thread with a message."""

    workspace = _post(
        api, token, "/api/v1/workspaces", {"title": "Research"}, key="workspace-command-0001"
    ).payload
    thread = _post(
        api,
        token,
        f"/api/v1/workspaces/{workspace['id']}/threads",
        {"title": "Cockpit", "expected_revision": 0},
        key="thread-command-000001",
    ).payload
    _post(
        api,
        token,
        f"/api/v1/threads/{thread['id']}/messages",
        {"role": "user", "content": "Hello?", "expected_revision": thread["revision"]},
        key="message-command-00001",
    )
    return api.handle(
        method="GET",
        target=f"/api/v1/threads/{thread['id']}",
        headers=_control_headers(token),
    ).payload


def test_a_bound_daemon_hands_api_created_runs_to_its_turn_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P8⟧ One bridge, two callers: the poller's sink and the control API.

    The daemon builds the bridge over the bound worker and gives the API the
    same object it binds the Telegram adapter's turn sink to, so a run the
    cockpit creates is executed by the same orchestrator under the same
    dispatch gate as a run a Telegram message creates.
    """

    class _Health:
        bound = True
        reason = None

        def to_dict(self) -> dict:
            return {
                "state": "bound",
                "reason": None,
                "release_id": "hermes-0.15.0-test",
                "slot_digest": "b" * 64,
                "launched": False,
            }

    class _BoundWorker:
        instances: list = []

        def __init__(self, **kwargs) -> None:
            self.closed = 0
            _BoundWorker.instances.append(self)

        def bind(self) -> _Health:
            return _Health()

        @property
        def bound(self) -> bool:
            return True

        def health(self) -> _Health:
            return _Health()

        def close(self) -> None:
            self.closed += 1

    class _Bridge:
        instances: list = []

        def __init__(self, *, store, worker, actor_id) -> None:
            self.store = store
            self.worker = worker
            self.actor_id = actor_id
            self.calls: list[str] = []
            self.submitted: list[str] = []
            _Bridge.instances.append(self)

        def recover(self, *, attempt: int = 1) -> list:
            self.calls.append("recover")
            order.append("recover")
            return []

        def start(self) -> None:
            self.calls.append("start")

        def stop(self, *, timeout: float = 5.0) -> None:
            self.calls.append("stop")

        def submit(self, thread_id: str) -> None:
            self.submitted.append(str(thread_id))

        def status(self) -> dict:
            return {"state": "running", "queued": len(self.submitted)}

    captured: dict[str, object] = {}
    order: list[str] = []
    monkeypatch.setattr(daemon_module, "ManagedTransportWorker", _BoundWorker)
    monkeypatch.setattr(daemon_module, "InboundTurnBridge", _Bridge)
    # ⟦Batch G P8-01⟧ The engine's schedules start BEFORE the bridge recovers,
    # so the sweep meets the engine's carrier runs; the predicate that keeps
    # it off them is tested on the bridge, and the ordering is pinned here.
    real_schedules = daemon_module.start_research_schedules
    monkeypatch.setattr(
        daemon_module,
        "start_research_schedules",
        lambda **kwargs: (order.append("schedules"), real_schedules(**kwargs))[1],
    )
    monkeypatch.setattr(
        daemon_module,
        "create_server",
        lambda **kwargs: (captured.update(kwargs), _ImmediateServer(kwargs["stop_requested"]))[1],
    )
    assert daemon_module.main(_arguments(tmp_path)) == 0
    api = captured["control_api"]
    token = str(captured["control_token"])
    bridge = _Bridge.instances[0]
    assert api._turn_bridge is bridge
    assert bridge.worker is _BoundWorker.instances[0]
    assert bridge.actor_id == "cortexd-turn-test-instance"
    assert bridge.calls == ["recover", "start", "stop"]
    assert order == ["schedules", "recover"]
    assert _BoundWorker.instances[0].closed == 1

    store = ControlStore(tmp_path / "data" / "control.db")
    thread = _cockpit_thread(api, token)
    closed = _post(
        api,
        token,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )
    assert closed.status == 409
    assert closed.payload["category"] == "runtime_activation_disabled"
    assert bridge.submitted == []

    store.enable_runtime_activation(
        mode="permanent", actor_id="operator", idempotency_key="activation-000000001"
    )
    created = _post(
        api,
        token,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000002",
    )
    assert created.status == 201, created.payload
    assert bridge.submitted == [thread["id"]]
    health = api.handle(method="GET", target="/api/v1/health", headers={}).payload
    assert health["turn_bridge"] == {"state": "running", "queued": 1}


def test_an_unbound_daemon_refuses_an_api_run_rather_than_queueing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P8⟧ No active release: the run is refused with the worker's reason."""

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        daemon_module,
        "create_server",
        lambda **kwargs: (captured.update(kwargs), _ImmediateServer(kwargs["stop_requested"]))[1],
    )
    assert daemon_module.main(_arguments(tmp_path)) == 0
    api = captured["control_api"]
    token = str(captured["control_token"])
    assert api._turn_bridge is None
    thread = _cockpit_thread(api, token)

    refused = _post(
        api,
        token,
        f"/api/v1/threads/{thread['id']}/runs",
        {"expected_revision": thread["revision"]},
        key="run-command-0000001",
    )

    assert refused.status == 409
    assert refused.payload["category"] == "managed_worker_unavailable"
    assert "no_active_release" in refused.payload["title"]
    store = ControlStore(tmp_path / "data" / "control.db")
    assert store.get_thread(thread["id"])["active_run_id"] is None
