from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore
from cortex_platform.product.daemon import create_server


def _store_with_run(tmp_path: Path) -> tuple[ControlStore, dict, dict]:
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    workspace = store.create_workspace(
        title="Research",
        actor_id="local",
        idempotency_key="workspace-command-0001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Echo",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key="thread-command-000001",
    ).value
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key="run-command-0000001",
    ).value
    return store, run, run["attempt"]


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_loopback_daemon_streams_committed_events_as_sse(tmp_path: Path) -> None:
    store, _, _ = _store_with_run(tmp_path)
    token = "s" * 48
    stop = threading.Event()
    server = create_server(
        host="127.0.0.1",
        port=0,
        instance_id="sse-test",
        control_token=token,
        stop_requested=stop,
        control_api=ControlAPI(store, access_token=token),
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(server.server_address[1]), timeout=2
    )
    try:
        connection.request(
            "GET",
            "/api/v1/events/stream",
            headers={"X-Cortex-Control-Token": token},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == (
            "text/event-stream; charset=utf-8"
        )
        assert response.getheader("Cache-Control") == "no-store"
        assert response.readline() == b"retry: 1000\n"
        assert response.readline() == b"\n"
        event_id = response.readline()
        event_type = response.readline()
        data = response.readline()
        assert event_id.startswith(b"id: ")
        assert event_type == b"event: run.queued\n"
        payload = json.loads(data.removeprefix(b"data: "))
        assert payload["run_id"]
        assert token not in repr(payload)
        assert str(tmp_path) not in repr(payload)
    finally:
        stop.set()
        connection.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_event_stream_capacity_is_bounded_and_rejection_is_not_cached(
    tmp_path: Path,
) -> None:
    store, _, _ = _store_with_run(tmp_path)
    token = "s" * 48
    server = create_server(
        host="127.0.0.1",
        port=0,
        instance_id="sse-capacity-test",
        control_token=token,
        control_api=ControlAPI(store, access_token=token),
        max_event_streams=1,
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    first = http.client.HTTPConnection(
        "127.0.0.1", int(server.server_address[1]), timeout=2
    )
    second = http.client.HTTPConnection(
        "127.0.0.1", int(server.server_address[1]), timeout=2
    )
    try:
        first.request(
            "GET",
            "/api/v1/events/stream",
            headers={"X-Cortex-Control-Token": token},
        )
        first_response = first.getresponse()
        assert first_response.status == 200
        assert first_response.readline() == b"retry: 1000\n"
        assert _wait_until(lambda: server.active_event_stream_count == 1)

        second.request(
            "GET",
            "/api/v1/events/stream",
            headers={"X-Cortex-Control-Token": token},
        )
        second_response = second.getresponse()
        assert second_response.status == 429
        assert second_response.getheader("Cache-Control") == "no-store"
        assert second_response.getheader("Retry-After") == "1"
        assert json.loads(second_response.read())["category"] == "rate_limited"
        assert server.active_event_stream_count == 1
    finally:
        server.request_stop()
        first.close()
        second.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    assert server.active_event_stream_count == 0
    assert not serving.is_alive()


def test_stop_interrupts_slow_event_writer_and_all_handlers_exit(
    tmp_path: Path,
) -> None:
    store, run, attempt = _store_with_run(tmp_path)
    large_prompt = "Choose a safe research option. " + ("x" * 16_000)
    with store._transaction() as conn:
        for index in range(500):
            store._insert_event(
                conn,
                run_id=run["id"],
                attempt_id=attempt["id"],
                event_type="decision.required",
                payload={
                    "decision_id": f"decision_{index}",
                    "kind": "source_conflict",
                    "prompt": large_prompt,
                    "options": [{"id": "keep_both", "label": "Keep both"}],
                },
            )

    token = "s" * 48
    stop = threading.Event()
    server = create_server(
        host="127.0.0.1",
        port=0,
        instance_id="sse-slow-client-test",
        control_token=token,
        stop_requested=stop,
        control_api=ControlAPI(store, access_token=token),
        event_stream_write_timeout=0.2,
        event_stream_shutdown_timeout=2.0,
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    address = ("127.0.0.1", int(server.server_address[1]))
    client = socket.create_connection(address, timeout=2)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_024)
    try:
        client.sendall(
            (
                "GET /api/v1/events/stream HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"X-Cortex-Control-Token: {token}\r\n"
                "Connection: keep-alive\r\n\r\n"
            ).encode("ascii")
        )
        assert _wait_until(lambda: server.active_event_stream_count == 1)
        time.sleep(0.2)
        assert server.active_event_stream_count == 1
        stop.set()
        assert server.wait_for_event_streams(timeout=2.0)
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
    assert server.active_event_stream_count == 0
    assert not serving.is_alive()
    assert not server._event_stream_stop_monitor.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.1)
