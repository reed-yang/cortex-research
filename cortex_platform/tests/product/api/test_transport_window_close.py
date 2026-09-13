"""The P5.4 seam: the operator asks the DAEMON to close a transport window.

`cortex transport close-window` is the RECORDING call site and has been since
P5.3. What it could not do is DERIVE `poller_stopped` -- that needs a supervisor
handle and a worker pid, which live in `cortexd` and nowhere else. This route is
how the CLI reaches them, and the shape of the request is the point: no
`poller_stopped` and no `proof` cross it, because a client that could supply
either would be writing the literal D-P5-5 forbids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore

TOKEN = "x" * 48
_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class _Windows:
    """A stand-in for `TransportWindowSupervisor`, with its one route verb."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._raises = raises

    def close_window(self, *, window_id: str, actor_id: str) -> dict:
        self.calls.append({"window_id": window_id, "actor_id": actor_id})
        if self._raises is not None:
            raise self._raises
        return {
            "recorded": "transport_window_closed",
            "window_id": window_id,
            "derivation": {
                "poller_stopped": True,
                "worker_launched": True,
                "worker_pid": 4242,
                "exit_status": -15,
                "established_sockets": [],
                "proof": "close() returned; pid 4242 poll()=-15; lsof … empty",
            },
        }


def _store(tmp_path: Path) -> ControlStore:
    value = ControlStore(tmp_path / "control.db", clock=lambda: _NOW)
    value.initialize()
    return value


def _headers(key: str = "close-000000000000001") -> dict[str, str]:
    return {"X-Cortex-Control-Token": TOKEN, "Idempotency-Key": key}


def _post(api: ControlAPI, window_id: str, body: bytes):
    return api.handle(
        method="POST",
        target=f"/api/v1/transport/windows/{window_id}/close",
        headers=_headers(),
        body=body,
    )


def test_the_route_returns_the_derivation_the_daemon_made(tmp_path: Path) -> None:
    windows = _Windows()
    api = ControlAPI(
        _store(tmp_path), access_token=TOKEN, transport_windows=windows
    )
    response = _post(api, "transport-activation-1", b'{"actor_id":"reed"}')
    assert response.status == 200
    assert response.payload["recorded"] == "transport_window_closed"
    assert response.payload["derivation"]["poller_stopped"] is True
    assert response.payload["derivation"]["worker_pid"] == 4242
    assert windows.calls == [
        {"window_id": "transport-activation-1", "actor_id": "reed"}
    ]


def test_a_client_cannot_supply_the_value_the_daemon_must_derive(
    tmp_path: Path,
) -> None:
    windows = _Windows()
    api = ControlAPI(
        _store(tmp_path), access_token=TOKEN, transport_windows=windows
    )
    for body in (
        b'{"actor_id":"reed","poller_stopped":true}',
        b'{"actor_id":"reed","proof":"i looked"}',
        b"{}",
    ):
        response = _post(api, "transport-activation-1", body)
        assert response.status == 400
        assert response.payload["category"] == "invalid_request"
    assert windows.calls == []


def test_an_actor_id_is_required_and_bounded(tmp_path: Path) -> None:
    windows = _Windows()
    api = ControlAPI(
        _store(tmp_path), access_token=TOKEN, transport_windows=windows
    )
    for value in (b'{"actor_id":""}', b'{"actor_id":123}', b'{"actor_id":"a\\u0000b"}'):
        assert _post(api, "transport-activation-1", value).status == 400
    assert windows.calls == []


def test_a_daemon_that_owns_no_worker_refuses_rather_than_recording(
    tmp_path: Path,
) -> None:
    """No supervisor handle, no derivation -- and therefore no audit row."""

    api = ControlAPI(_store(tmp_path), access_token=TOKEN)
    response = _post(api, "transport-activation-1", b'{"actor_id":"reed"}')
    assert response.status == 409
    assert response.payload["category"] == "managed_worker_unavailable"


def test_the_stores_refusal_of_an_open_window_reaches_the_operator(
    tmp_path: Path,
) -> None:
    from cortex_platform.product.control.errors import InvalidTransition

    windows = _Windows(raises=InvalidTransition("open", "closed"))
    api = ControlAPI(
        _store(tmp_path), access_token=TOKEN, transport_windows=windows
    )
    response = _post(api, "transport-activation-1", b'{"actor_id":"reed"}')
    assert response.status == 409
    assert response.payload["category"] == "invalid_transition"


def test_the_route_is_authenticated_like_every_other_command(
    tmp_path: Path,
) -> None:
    api = ControlAPI(
        _store(tmp_path), access_token=TOKEN, transport_windows=_Windows()
    )
    response = api.handle(
        method="POST",
        target="/api/v1/transport/windows/transport-activation-1/close",
        headers={"Idempotency-Key": "close-000000000000001"},
        body=b'{"actor_id":"reed"}',
    )
    assert response.status == 403
