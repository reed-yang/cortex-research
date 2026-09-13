from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import cortex_platform.product.lifecycle as lifecycle
from cortex_platform.product.cli import main as cli_main
from cortex_platform.product.config import initialize
from cortex_platform.product.lifecycle import (
    DaemonMetadata,
    LifecycleError,
    daemon_status,
    read_metadata,
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
    try:
        stop_daemon(paths, timeout=1.0)
    except LifecycleError:
        paths.daemon_metadata_file.unlink(missing_ok=True)


def _direct_command(paths: PathRegistry, instance_id: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "cortex_platform.product.daemon",
        "--instance-id",
        instance_id,
        "--config-file",
        str(paths.config_file),
        "--config-dir",
        str(paths.config_dir),
        "--data-dir",
        str(paths.data_dir),
        "--state-dir",
        str(paths.state_dir),
        "--cache-dir",
        str(paths.cache_dir),
        "--log-dir",
        str(paths.log_dir),
    ]


def _start_direct(
    paths: PathRegistry, environment: dict[str, str], instance_id: str
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _direct_command(paths, instance_id),
        cwd=paths.data_dir,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_running(paths: PathRegistry, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    status = daemon_status(paths)
    while time.monotonic() < deadline and status.state != "running":
        time.sleep(0.05)
        status = daemon_status(paths)
    assert status.state == "running"
    return status


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait(timeout=2)
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def test_two_direct_daemons_leave_exactly_one_owner_and_listener(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation
    processes = [
        _start_direct(paths, environment, "direct-one"),
        _start_direct(paths, environment, "direct-two"),
    ]
    try:
        status = _wait_running(paths)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and sum(
            process.poll() is None for process in processes
        ) != 1:
            time.sleep(0.05)

        alive = [process for process in processes if process.poll() is None]
        assert len(alive) == 1
        assert status.pid == alive[0].pid
        metadata = read_metadata(paths.daemon_metadata_file)
        assert metadata is not None
        assert metadata.pid == alive[0].pid
        assert paths.daemon_lifetime_lock_file.stat().st_mode & 0o777 == 0o600
        assert len([process for process in processes if process.poll() is not None]) == 1

        assert stop_daemon(paths, timeout=5.0).state == "stopped"
        assert alive[0].wait(timeout=5) == 0
    finally:
        for process in processes:
            _terminate(process)


def test_direct_and_managed_start_race_resolves_to_one_daemon(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, environment = installation
    direct = _start_direct(paths, environment, "direct-racer")
    managed_processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        managed_processes.append(process)
        return process

    monkeypatch.setattr(lifecycle.subprocess, "Popen", tracked_popen)
    try:
        status = start_daemon(paths, environ=environment, timeout=8.0)
        deadline = time.monotonic() + 3.0
        all_processes = [direct, *managed_processes]
        while time.monotonic() < deadline and sum(
            process.poll() is None for process in all_processes
        ) != 1:
            time.sleep(0.05)

        alive = [process for process in all_processes if process.poll() is None]
        assert len(alive) == 1
        assert status.pid == alive[0].pid
        assert daemon_status(paths).pid == alive[0].pid
        assert stop_daemon(paths, timeout=5.0).state == "stopped"
        assert alive[0].wait(timeout=5) == 0
    finally:
        for process in [direct, *managed_processes]:
            _terminate(process)


def test_abnormal_exit_releases_lifetime_lock_and_restart_replaces_stale_metadata(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation
    first = _start_direct(paths, environment, "crash-first")
    second: subprocess.Popen[bytes] | None = None
    try:
        first_status = _wait_running(paths)
        assert first_status.pid == first.pid
        first.kill()
        first.wait(timeout=5)
        assert paths.daemon_metadata_file.exists()

        second = _start_direct(paths, environment, "crash-second")
        second_status = _wait_running(paths)
        assert second_status.pid == second.pid
        assert second_status.instance_id == "crash-second"
        assert stop_daemon(paths, timeout=5.0).state == "stopped"
        assert second.wait(timeout=5) == 0
    finally:
        _terminate(first)
        if second is not None:
            _terminate(second)


class _ProxyTrapHandler(BaseHTTPRequestHandler):
    requests = 0

    def _trap(self) -> None:
        type(self).requests += 1
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _trap
    do_POST = _trap

    def log_message(self, format: str, *args: object) -> None:
        return


def test_health_and_shutdown_ignore_environment_proxies(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, environment = installation
    trap = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyTrapHandler)
    trap_thread = threading.Thread(target=trap.serve_forever, daemon=True)
    trap_thread.start()
    proxy_url = f"http://127.0.0.1:{trap.server_address[1]}"
    _ProxyTrapHandler.requests = 0
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment[name] = proxy_url
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        environment.pop(name, None)
        monkeypatch.delenv(name, raising=False)

    try:
        assert start_daemon(paths, environ=environment, timeout=8.0).state == "running"
        assert daemon_status(paths).state == "running"
        assert stop_daemon(paths, timeout=5.0).state == "stopped"
        assert _ProxyTrapHandler.requests == 0
    finally:
        trap.shutdown()
        trap.server_close()
        trap_thread.join(timeout=2)


def test_cli_stop_uses_no_process_signals(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, environment = installation
    started = start_daemon(paths, environ=environment, timeout=8.0)
    assert started.pid is not None
    metadata = read_metadata(paths.daemon_metadata_file)
    assert metadata is not None
    real_kill = os.kill
    signals: list[tuple[int, int]] = []

    def record_kill(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(lifecycle.os, "kill", record_kill)
    try:
        assert stop_daemon(paths, timeout=1.0).state == "stopped"
        assert signals == []
    finally:
        if lifecycle.process_identity_matches(metadata):
            real_kill(started.pid, signal.SIGTERM)


class _ReplacementServiceHandler(BaseHTTPRequestHandler):
    instance_id = "expected-instance"

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "service": "cortexd",
                "status": "ok",
                "instance_id": self.instance_id,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "service": "cortexd",
                "status": "stopping",
                "instance_id": "replacement-instance",
            }
        ).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_port_replacement_or_control_identity_mismatch_never_signals_pid(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    replacement = ThreadingHTTPServer(("127.0.0.1", 0), _ReplacementServiceHandler)
    thread = threading.Thread(target=replacement.serve_forever, daemon=True)
    thread.start()
    metadata = DaemonMetadata(
        pid=os.getpid(),
        instance_id="expected-instance",
        start_token="semantic-before-race",
        host="127.0.0.1",
        port=int(replacement.server_address[1]),
        control_token="t" * 43,
    )
    write_metadata(paths.daemon_metadata_file, metadata)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(lifecycle, "process_identity_matches", lambda value: True)
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    try:
        with pytest.raises(LifecycleError, match="control|identity"):
            stop_daemon(paths, timeout=0.1)
        assert signals == []
        assert paths.daemon_metadata_file.exists()
    finally:
        paths.daemon_metadata_file.unlink(missing_ok=True)
        replacement.shutdown()
        replacement.server_close()
        thread.join(timeout=2)


def test_pid_semantics_changing_after_identity_check_never_signals(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    metadata = DaemonMetadata(
        pid=os.getpid(),
        instance_id="pid-race",
        start_token="before-reuse",
        host="127.0.0.1",
        port=1,
        control_token="t" * 43,
    )
    write_metadata(paths.daemon_metadata_file, metadata)
    race = {"changed": False}
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        lifecycle,
        "process_identity_matches",
        lambda value: not race["changed"],
    )

    def change_pid_semantics(value: DaemonMetadata) -> bool:
        race["changed"] = True
        return False

    monkeypatch.setattr(lifecycle, "_health_ready", change_pid_semantics)
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(LifecycleError, match="health identity"):
        stop_daemon(paths, timeout=0.1)
    assert race["changed"] is True
    assert signals == []
    assert paths.daemon_metadata_file.exists()
    paths.daemon_metadata_file.unlink()


def test_concurrent_stop_is_idempotent_across_repeated_real_daemons(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, environment = installation
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig))
    )

    for _ in range(30):
        started = start_daemon(paths, environ=environment, timeout=8.0)
        metadata = read_metadata(paths.daemon_metadata_file)
        assert metadata is not None
        barrier = threading.Barrier(2)

        def stop_together() -> str:
            barrier.wait(timeout=2.0)
            return stop_daemon(paths, timeout=5.0).state

        with ThreadPoolExecutor(max_workers=2) as executor:
            states = list(executor.map(lambda _: stop_together(), range(2)))

        assert states == ["stopped", "stopped"]
        assert not paths.daemon_metadata_file.exists()
        assert not lifecycle.process_identity_matches(metadata)
        assert started.instance_id == metadata.instance_id

    assert signals == []


@pytest.mark.parametrize("failed_handshake", ["health", "control"])
def test_stop_failure_returns_replacement_current_status_without_controlling_it(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    failed_handshake: str,
) -> None:
    paths, _ = installation
    old = DaemonMetadata(
        pid=os.getpid(),
        instance_id="stop-old",
        start_token="old-start",
        host="127.0.0.1",
        port=1234,
        control_token="o" * 43,
    )
    replacement = DaemonMetadata(
        pid=os.getpid(),
        instance_id="stop-replacement",
        start_token="replacement-start",
        host="127.0.0.1",
        port=4321,
        control_token="r" * 43,
    )
    write_metadata(paths.daemon_metadata_file, old)
    signals: list[tuple[int, int]] = []
    shutdown_instances: list[str] = []
    monkeypatch.setattr(lifecycle, "process_identity_matches", lambda value: True)
    monkeypatch.setattr(
        lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig))
    )

    def health(metadata: DaemonMetadata, *, timeout: float = 0.4) -> bool:
        if failed_handshake == "health" and metadata.instance_id == old.instance_id:
            write_metadata(paths.daemon_metadata_file, replacement)
            return False
        return True

    def shutdown(metadata: DaemonMetadata, *, timeout: float) -> bool:
        shutdown_instances.append(metadata.instance_id)
        if failed_handshake == "control":
            write_metadata(paths.daemon_metadata_file, replacement)
            return False
        raise AssertionError("shutdown must not follow a failed health handshake")

    monkeypatch.setattr(lifecycle, "_health_ready", health)
    monkeypatch.setattr(lifecycle, "_request_shutdown", shutdown)

    status = stop_daemon(paths, timeout=0.2)

    assert status.state == "running"
    assert status.instance_id == replacement.instance_id
    assert shutdown_instances == ([] if failed_handshake == "health" else ["stop-old"])
    current = read_metadata(paths.daemon_metadata_file)
    assert current == replacement
    assert signals == []


@pytest.mark.parametrize("failed_handshake", ["health", "control"])
def test_failed_handshake_is_idempotent_when_original_instance_disappears(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    failed_handshake: str,
) -> None:
    paths, _ = installation
    metadata = DaemonMetadata(
        pid=os.getpid(),
        instance_id="disappearing-instance",
        start_token="start",
        host="127.0.0.1",
        port=1234,
        control_token="t" * 43,
    )
    write_metadata(paths.daemon_metadata_file, metadata)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        lifecycle,
        "process_identity_matches",
        lambda value: paths.daemon_metadata_file.exists(),
    )
    monkeypatch.setattr(
        lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig))
    )

    def close_during_health(value: DaemonMetadata) -> bool:
        if failed_handshake == "health":
            assert lifecycle._remove_matching_metadata(
                paths.daemon_metadata_file, value.instance_id
            )
            return False
        return True

    def close_during_control(value: DaemonMetadata, *, timeout: float) -> bool:
        if failed_handshake == "control":
            assert lifecycle._remove_matching_metadata(
                paths.daemon_metadata_file, value.instance_id
            )
            return False
        raise AssertionError("control must not follow a failed health handshake")

    monkeypatch.setattr(lifecycle, "_health_ready", close_during_health)
    monkeypatch.setattr(lifecycle, "_request_shutdown", close_during_control)

    assert stop_daemon(paths, timeout=0.2).state == "stopped"
    assert signals == []


def test_status_discovers_replacement_written_after_successful_stale_removal(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    old = DaemonMetadata(
        pid=os.getpid(),
        instance_id="removed-stale",
        start_token="old-start",
        host="127.0.0.1",
        port=1234,
        control_token="o" * 43,
    )
    replacement = DaemonMetadata(
        pid=os.getpid(),
        instance_id="post-remove-replacement",
        start_token="new-start",
        host="127.0.0.1",
        port=4321,
        control_token="r" * 43,
    )
    write_metadata(paths.daemon_metadata_file, old)
    real_remove = lifecycle._remove_matching_metadata

    def remove_then_replace(path: Path, instance_id: str | None = None) -> bool:
        removed = real_remove(path, instance_id)
        assert removed
        write_metadata(path, replacement)
        return True

    monkeypatch.setattr(lifecycle, "_remove_matching_metadata", remove_then_replace)
    monkeypatch.setattr(
        lifecycle,
        "process_identity_matches",
        lambda value: value.instance_id == replacement.instance_id,
    )
    monkeypatch.setattr(lifecycle, "_health_ready", lambda value: True)

    status = daemon_status(paths)

    assert status.state == "running"
    assert status.instance_id == replacement.instance_id
    assert read_metadata(paths.daemon_metadata_file) == replacement


def test_status_rapid_replacement_converges_with_last_metadata_preserved(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    write_metadata(
        paths.daemon_metadata_file,
        DaemonMetadata(
            pid=os.getpid(),
            instance_id="rapid-0",
            start_token="start-0",
            host="127.0.0.1",
            port=1234,
            control_token="t" * 43,
        ),
    )
    real_remove = lifecycle._remove_matching_metadata
    replacements = 0

    def replace_after_remove(path: Path, instance_id: str | None = None) -> bool:
        nonlocal replacements
        removed = real_remove(path, instance_id)
        assert removed
        replacements += 1
        write_metadata(
            path,
            DaemonMetadata(
                pid=os.getpid(),
                instance_id=f"rapid-{replacements}",
                start_token=f"start-{replacements}",
                host="127.0.0.1",
                port=1234,
                control_token="t" * 43,
            ),
        )
        return True

    monkeypatch.setattr(lifecycle, "_remove_matching_metadata", replace_after_remove)
    monkeypatch.setattr(lifecycle, "process_identity_matches", lambda value: False)

    status = daemon_status(paths)

    assert status.state == "stale"
    assert replacements == lifecycle._STATUS_CONVERGENCE_LIMIT
    current = read_metadata(paths.daemon_metadata_file)
    assert current is not None
    assert current.instance_id == f"rapid-{replacements}"


def _metadata_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "pid": 123,
        "instance_id": "instance",
        "start_token": "token",
        "host": "127.0.0.1",
        "port": 1234,
        "control_token": "t" * 43,
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '"string"',
        "null",
        "{damaged-json",
        _metadata_json(unexpected=True),
        _metadata_json(schema_version="1"),
        _metadata_json(schema_version=True),
        _metadata_json(pid="123"),
        _metadata_json(pid=True),
        _metadata_json(instance_id=["instance"]),
        _metadata_json(start_token=123),
        _metadata_json(host=["127.0.0.1"]),
        _metadata_json(port=True),
        _metadata_json(port="1234"),
        _metadata_json(control_token=123),
        _metadata_json(control_token="too-short"),
        _metadata_json(control_token="!" * 43),
        json.dumps(
            {
                key: value
                for key, value in json.loads(_metadata_json()).items()
                if key != "port"
            }
        ),
        json.dumps(
            {
                key: value
                for key, value in json.loads(_metadata_json()).items()
                if key != "control_token"
            }
        ),
    ],
)
def test_invalid_metadata_is_stale_cleaned_and_never_traces_back(
    installation: tuple[PathRegistry, dict[str, str]],
    content: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, environment = installation
    paths.daemon_metadata_file.write_text(content, encoding="utf-8")

    assert read_metadata(paths.daemon_metadata_file) is None
    assert daemon_status(paths).state == "stale"
    assert not paths.daemon_metadata_file.exists()

    paths.daemon_metadata_file.write_text(content, encoding="utf-8")
    assert cli_main(["status"], environ=environment, platform=sys.platform) == 3
    captured = capsys.readouterr()
    assert captured.out.strip() == "stale"
    assert "Traceback" not in captured.err
    assert not paths.daemon_metadata_file.exists()


def test_process_identity_survives_the_closed_daemon_environment(
    tmp_path: Path,
) -> None:
    # The supervisor hands the daemon a closed environment whose PATH is only the
    # generation's own bin directory. Establishing process identity must not
    # depend on inheriting a system PATH: a bare `ps` is unresolvable there, and
    # the daemon exits before readiness with "could not establish daemon process
    # identity". Every suite double sidestepped this, so only a real start caught
    # it. Absolute system-tool paths are the house rule.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "sys.path[:0] = json.loads(os.environ['PROBE_PATH'])\n"
        "from cortex_platform.product.lifecycle import current_process_start_token\n"
        "print(json.dumps(current_process_start_token(os.getpid()) is not None))\n"
    )
    completed = subprocess.run(
        [sys.executable, str(probe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            # Deliberately closed, mirroring Generation.control_environment.
            "PATH": str(Path(sys.executable).parent),
            "HOME": "",
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONPATH": "",
            "PROBE_PATH": json.dumps([path for path in sys.path if path]),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) is True
