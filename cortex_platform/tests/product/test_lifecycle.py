from __future__ import annotations

import json
import os
import signal
import socket
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cortex_platform.product.config import initialize
from cortex_platform.product.daemon import create_server
from cortex_platform.product.lifecycle import (
    DaemonMetadata,
    daemon_status,
    start_daemon,
    stop_daemon,
    write_metadata,
)
from cortex_platform.product.paths import PathRegistry, resolve_paths


def _environment(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg" / "config"),
        "XDG_DATA_HOME": str(home / "xdg" / "data"),
        "XDG_STATE_HOME": str(home / "xdg" / "state"),
        "XDG_CACHE_HOME": str(home / "xdg" / "cache"),
    }


@pytest.fixture
def installation(tmp_path: Path) -> tuple[PathRegistry, dict[str, str]]:
    environment = _environment(tmp_path / "home")
    paths = resolve_paths(environ=environment, platform=sys.platform)
    initialize(paths, environ=environment)
    yield paths, environment
    stop_daemon(paths, timeout=3.0)


def _get_json(port: int, path: str) -> dict[str, object]:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=2.0
    ) as response:
        return json.load(response)


def test_start_waits_for_readiness_and_status_checks_health_and_identity(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation

    started = start_daemon(paths, environ=environment, timeout=8.0)
    repeated = start_daemon(paths, environ=environment, timeout=8.0)
    status = daemon_status(paths)

    assert started.state == "running"
    assert started.pid is not None
    assert started.port is not None
    assert repeated.pid == started.pid
    assert repeated.instance_id == started.instance_id
    assert status.state == "running"
    assert status.process_identity is True
    assert status.health_ready is True
    health = _get_json(started.port, "/healthz")
    assert health == {
        "api_mode": "control",
        "instance_id": started.instance_id,
        "service": "cortexd",
        "status": "ok",
    }
    demo = _get_json(started.port, "/demo")
    assert demo["implemented"] == {
        "control_store": True,
        "research_pipeline": False,
        "runtime": False,
    }


def test_stop_is_idempotent_and_restart_preserves_config_without_state_duplication(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation
    original_config = paths.config_file.read_bytes()

    first = start_daemon(paths, environ=environment, timeout=8.0)
    stopped = stop_daemon(paths, timeout=5.0)
    stopped_again = stop_daemon(paths, timeout=5.0)
    second = start_daemon(paths, environ=environment, timeout=8.0)
    stopped_second = stop_daemon(paths, timeout=5.0)

    assert stopped.state == "stopped"
    assert stopped_again.state == "stopped"
    assert stopped_second.state == "stopped"
    assert first.instance_id != second.instance_id
    assert paths.config_file.read_bytes() == original_config
    assert not paths.daemon_metadata_file.exists()
    assert sorted(path.name for path in paths.state_dir.iterdir()) == [
        ".cortexd.instance.lock",
        ".cortexd.metadata.lock",
        ".cortexd.start.lock",
    ]


def test_concurrent_start_requests_resolve_to_one_daemon(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation

    with ThreadPoolExecutor(max_workers=3) as executor:
        statuses = list(
            executor.map(
                lambda _: start_daemon(paths, environ=environment, timeout=8.0),
                range(3),
            )
        )

    assert len({status.pid for status in statuses}) == 1
    assert len({status.instance_id for status in statuses}) == 1
    assert daemon_status(paths).state == "running"


def test_real_socket_is_loopback_only_and_non_loopback_bind_is_rejected(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation
    started = start_daemon(paths, environ=environment, timeout=8.0)
    assert started.port is not None

    with socket.create_connection(("127.0.0.1", started.port), timeout=2.0) as client:
        assert client.getpeername()[0] == "127.0.0.1"
    metadata = json.loads(paths.daemon_metadata_file.read_text(encoding="utf-8"))
    assert metadata["host"] == "127.0.0.1"
    assert paths.daemon_metadata_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="loopback"):
        create_server(
            host="0.0.0.0",
            port=0,
            instance_id="rejected",
            control_token="t" * 43,
        )


def test_stale_pid_metadata_is_removed_without_signaling(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    write_metadata(
        paths.daemon_metadata_file,
        DaemonMetadata(
            pid=99_999_999,
            instance_id="stale",
            start_token="missing",
            host="127.0.0.1",
            port=1,
            control_token="t" * 43,
        ),
    )
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        "cortex_platform.product.lifecycle.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    status = daemon_status(paths)
    stopped = stop_daemon(paths)

    assert status.state == "stale"
    assert stopped.state == "stopped"
    assert signals == []
    assert not paths.daemon_metadata_file.exists()


def test_pid_reuse_identity_mismatch_never_terminates_unrelated_process(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    write_metadata(
        paths.daemon_metadata_file,
        DaemonMetadata(
            pid=os.getpid(),
            instance_id="not-this-process",
            start_token="reused-pid-token",
            host="127.0.0.1",
            port=1,
            control_token="t" * 43,
        ),
    )
    dangerous_signals: list[int] = []
    real_kill = os.kill

    def record_kill(pid: int, sig: int) -> None:
        if sig in {signal.SIGTERM, signal.SIGKILL}:
            dangerous_signals.append(sig)
            return
        real_kill(pid, sig)

    monkeypatch.setattr("cortex_platform.product.lifecycle.os.kill", record_kill)

    status = daemon_status(paths)
    stopped = stop_daemon(paths)

    assert status.state == "stale"
    assert stopped.state == "stopped"
    assert dangerous_signals == []
    assert not paths.daemon_metadata_file.exists()


def test_health_identity_mismatch_is_not_reported_as_running(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation
    started = start_daemon(paths, environ=environment, timeout=8.0)
    metadata = json.loads(paths.daemon_metadata_file.read_text(encoding="utf-8"))
    metadata["instance_id"] = "tampered"
    paths.daemon_metadata_file.write_text(json.dumps(metadata), encoding="utf-8")

    status = daemon_status(paths)

    assert status.state == "stale"
    assert status.process_identity is False
    assert status.health_ready is False
    assert started.pid is not None
    os.kill(started.pid, signal.SIGTERM)
