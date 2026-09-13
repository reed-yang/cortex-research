from __future__ import annotations

import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from dataclasses import asdict
from pathlib import Path

import pytest

import cortex_platform.product.lifecycle as lifecycle
from cortex_platform.product.cli import main as cli_main
from cortex_platform.product.config import ConfigError, initialize, load_config
from cortex_platform.product.diagnostics import doctor
from cortex_platform.product.lifecycle import (
    DaemonMetadata,
    LifecycleError,
    daemon_status,
    process_identity_matches,
    read_metadata,
    start_daemon,
    stop_daemon,
    write_metadata,
)
from cortex_platform.product.paths import PathRegistry, resolve_paths


_TOKEN = "t" * 43


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
        metadata = read_metadata(paths.daemon_metadata_file)
    except LifecycleError:
        metadata = None
    try:
        stop_daemon(paths, timeout=1.0)
    except LifecycleError:
        if metadata is not None and process_identity_matches(metadata):
            os.kill(metadata.pid, signal.SIGTERM)


def _get_json(port: int, path: str) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read())
    finally:
        connection.close()
    assert isinstance(payload, dict)
    return payload


def _post_shutdown(
    port: int,
    instance_id: str,
    control_token: str | None,
) -> tuple[int, dict[str, object]]:
    payload: dict[str, object] = {"instance_id": instance_id}
    headers = {
        "Content-Type": "application/json",
        "X-Cortex-Instance-ID": instance_id,
    }
    if control_token is not None:
        payload["control_token"] = control_token
        headers["X-Cortex-Control-Token"] = control_token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        connection.request(
            "POST",
            "/__cortex__/shutdown",
            body=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        return response.status, body
    finally:
        connection.close()


def test_control_token_is_independent_secret_and_never_leaks(
    installation: tuple[PathRegistry, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, environment = installation
    started = start_daemon(paths, environ=environment, timeout=8.0)
    assert started.pid is not None
    assert started.port is not None
    raw_metadata = json.loads(paths.daemon_metadata_file.read_text(encoding="utf-8"))
    assert paths.daemon_metadata_file.stat().st_mode & 0o777 == 0o600
    control_token = raw_metadata["control_token"]
    assert isinstance(control_token, str)
    assert len(control_token) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]+", control_token)

    health = _get_json(started.port, "/healthz")
    demo = _get_json(started.port, "/demo")
    assert health["instance_id"] == started.instance_id
    assert control_token not in json.dumps(health)
    assert control_token not in json.dumps(demo)
    assert "control_token" not in health
    assert "control_token" not in demo

    command = subprocess.run(
        ["ps", "-ww", "-p", str(started.pid), "-o", "command="],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert control_token not in command
    assert control_token not in paths.daemon_log_file.read_text(encoding="utf-8")

    assert cli_main(["status"], environ=environment, platform=sys.platform) == 0
    status_output = capsys.readouterr()
    assert control_token not in status_output.out
    assert control_token not in status_output.err
    report = doctor(paths, environ=environment).render()
    assert control_token not in report

    missing_status, _ = _post_shutdown(
        started.port, started.instance_id or "", None
    )
    wrong_status, _ = _post_shutdown(
        started.port, started.instance_id or "", "wrong-token"
    )
    assert missing_status == 409
    assert wrong_status == 409
    assert daemon_status(paths).state == "running"

    assert stop_daemon(paths, timeout=5.0).state == "stopped"


def _metadata(instance_id: str) -> DaemonMetadata:
    return DaemonMetadata(
        pid=os.getpid(),
        instance_id=instance_id,
        start_token="start-token",
        host="127.0.0.1",
        port=1234,
        control_token=_TOKEN,
    )


def _raw_replace_metadata(path: Path, metadata: DaemonMetadata) -> None:
    payload = {"schema_version": 1, **asdict(metadata)}
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def test_compare_remove_preserves_replacement_instance(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    old = _metadata("old-instance")
    replacement = _metadata("new-instance")
    write_metadata(paths.daemon_metadata_file, old)
    original_read = lifecycle._read_metadata_unlocked
    calls = 0

    def inject_replacement(path: Path):
        nonlocal calls
        result = original_read(path)
        calls += 1
        if calls == 1:
            _raw_replace_metadata(path, replacement)
        return result

    monkeypatch.setattr(lifecycle, "_read_metadata_unlocked", inject_replacement)

    lifecycle._remove_matching_metadata(paths.daemon_metadata_file, old.instance_id)

    current = read_metadata(paths.daemon_metadata_file)
    assert current is not None
    assert current.instance_id == replacement.instance_id


def test_invalid_cleanup_preserves_valid_replacement(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    paths.daemon_metadata_file.write_text("[]", encoding="utf-8")
    paths.daemon_metadata_file.chmod(0o600)
    replacement = _metadata("replacement-instance")
    original_read = lifecycle._read_metadata_unlocked
    calls = 0

    def inject_replacement(path: Path):
        nonlocal calls
        result = original_read(path)
        calls += 1
        if calls == 1:
            _raw_replace_metadata(path, replacement)
        return result

    monkeypatch.setattr(lifecycle, "_read_metadata_unlocked", inject_replacement)
    monkeypatch.setattr(lifecycle, "process_identity_matches", lambda value: True)
    monkeypatch.setattr(lifecycle, "_health_ready", lambda value: True)

    status = daemon_status(paths)

    assert status.state == "running"
    current = read_metadata(paths.daemon_metadata_file)
    assert current is not None
    assert current.instance_id == replacement.instance_id


def test_stale_identity_cleanup_discovers_replacement_instance(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    old = _metadata("stale-instance")
    replacement = _metadata("replacement-instance")
    write_metadata(paths.daemon_metadata_file, old)
    original_read = lifecycle._read_metadata_unlocked
    old_reads = 0

    def inject_replacement(path: Path):
        nonlocal old_reads
        result = original_read(path)
        if result is not None and result.instance_id == old.instance_id:
            old_reads += 1
            if old_reads == 2:
                _raw_replace_metadata(path, replacement)
        return result

    monkeypatch.setattr(lifecycle, "_read_metadata_unlocked", inject_replacement)
    monkeypatch.setattr(
        lifecycle,
        "process_identity_matches",
        lambda value: value.instance_id == replacement.instance_id,
    )
    monkeypatch.setattr(lifecycle, "_health_ready", lambda value: True)

    status = daemon_status(paths)

    assert status.state == "running"
    assert status.instance_id == replacement.instance_id
    current = read_metadata(paths.daemon_metadata_file)
    assert current is not None
    assert current.instance_id == replacement.instance_id


@pytest.mark.parametrize("lock_role", ["start", "lifetime", "metadata"])
def test_lock_symlinks_fail_closed_without_touching_target(
    installation: tuple[PathRegistry, dict[str, str]],
    lock_role: str,
) -> None:
    paths, environment = installation
    victim = paths.state_dir / f"{lock_role}-victim"
    victim.write_text("do-not-touch", encoding="utf-8")
    victim.chmod(0o600)
    lock_path = {
        "start": paths.daemon_start_lock_file,
        "lifetime": paths.daemon_lifetime_lock_file,
        "metadata": paths.daemon_metadata_lock_file,
    }[lock_role]
    lock_path.symlink_to(victim)

    with pytest.raises(LifecycleError, match="unsafe|lock"):
        if lock_role == "start":
            start_daemon(paths, environ=environment, timeout=0.2)
        elif lock_role == "lifetime":
            lifecycle.acquire_daemon_lifetime_lock(lock_path)
        else:
            write_metadata(paths.daemon_metadata_file, _metadata("metadata-owner"))

    assert victim.read_text(encoding="utf-8") == "do-not-touch"
    assert victim.stat().st_mode & 0o777 == 0o600
    assert not paths.daemon_metadata_file.exists()
    lock_path.unlink()


@pytest.mark.parametrize("lock_role", ["start", "lifetime", "metadata"])
def test_unsafe_lock_modes_fail_closed(
    installation: tuple[PathRegistry, dict[str, str]],
    lock_role: str,
) -> None:
    paths, environment = installation
    lock_path = {
        "start": paths.daemon_start_lock_file,
        "lifetime": paths.daemon_lifetime_lock_file,
        "metadata": paths.daemon_metadata_lock_file,
    }[lock_role]
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o644)

    with pytest.raises(LifecycleError, match="unsafe|mode|lock"):
        if lock_role == "start":
            start_daemon(paths, environ=environment, timeout=0.2)
        elif lock_role == "lifetime":
            lifecycle.acquire_daemon_lifetime_lock(lock_path)
        else:
            read_metadata(paths.daemon_metadata_file)

    assert lock_path.stat().st_mode & 0o777 == 0o644
    lock_path.unlink()


def test_foreign_owned_lock_semantics_fail_closed(
    installation: tuple[PathRegistry, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = installation
    real_fstat = os.fstat

    def foreign_owner(descriptor: int) -> SimpleNamespace:
        details = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=details.st_mode,
            st_uid=details.st_uid + 1,
            st_nlink=details.st_nlink,
        )

    monkeypatch.setattr(lifecycle.os, "fstat", foreign_owner)

    with pytest.raises(LifecycleError, match="unsafe|owner|lock"):
        lifecycle.acquire_daemon_lifetime_lock(paths.daemon_lifetime_lock_file)

    paths.daemon_lifetime_lock_file.unlink()


def test_daemon_log_symlink_is_never_followed(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, environment = installation
    victim = paths.log_dir / "log-victim"
    victim.write_text("do-not-append", encoding="utf-8")
    victim.chmod(0o600)
    paths.daemon_log_file.symlink_to(victim)

    with pytest.raises(LifecycleError, match="log|unsafe"):
        start_daemon(paths, environ=environment, timeout=0.2)

    assert victim.read_text(encoding="utf-8") == "do-not-append"
    assert daemon_status(paths).state == "stopped"


def test_metadata_symlink_is_not_followed(
    installation: tuple[PathRegistry, dict[str, str]],
) -> None:
    paths, _ = installation
    victim = paths.state_dir / "metadata-victim"
    _raw_replace_metadata(victim, _metadata("victim-instance"))
    paths.daemon_metadata_file.symlink_to(victim)

    assert read_metadata(paths.daemon_metadata_file) is None
    assert daemon_status(paths).state == "stale"
    assert victim.exists()
    assert read_metadata(victim) is not None


def test_boolean_config_version_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("config_version = true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="config_version"):
        load_config(config_file)
