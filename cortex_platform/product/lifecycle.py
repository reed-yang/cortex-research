"""Safe local lifecycle management for the Cortex demo daemon."""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping

from .paths import PathRegistry

if sys.platform == "darwin" or sys.platform.startswith("linux"):
    import fcntl
else:  # pragma: no cover - path resolution rejects unsupported platforms first
    fcntl = None


class LifecycleError(RuntimeError):
    """Raised when a daemon lifecycle transition cannot complete safely."""


@dataclass(frozen=True)
class DaemonMetadata:
    pid: int
    instance_id: str
    start_token: str
    host: str
    port: int
    control_token: str


@dataclass(frozen=True)
class DaemonStatus:
    state: str
    pid: int | None = None
    port: int | None = None
    instance_id: str | None = None
    process_identity: bool = False
    health_ready: bool = False


_CONTROL_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,256}$")
_SECURE_OPEN_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_STATUS_CONVERGENCE_LIMIT = 8


def _open_secure_owned_file(
    path: Path,
    flags: int,
    *,
    purpose: str,
    missing_ok: bool = False,
) -> int | None:
    try:
        descriptor = os.open(path, flags | _SECURE_OPEN_FLAGS, 0o600)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LifecycleError(f"unsafe {purpose} file") from None
    except OSError as exc:
        raise LifecycleError(f"unsafe {purpose} file") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise LifecycleError(f"unsafe {purpose} file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _metadata_lock_path(path: Path) -> Path:
    return path.with_name(".cortexd.metadata.lock")


@contextmanager
def _exclusive_file_lock(path: Path, *, purpose: str) -> Iterator[None]:
    if fcntl is None:
        raise LifecycleError("cortexd lifecycle requires macOS or Linux")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = _open_secure_owned_file(
        path, os.O_CREAT | os.O_RDWR, purpose=purpose
    )
    assert descriptor is not None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_metadata(path: Path, metadata: DaemonMetadata) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"schema_version": 1, **asdict(metadata)}
    with _exclusive_file_lock(
        _metadata_lock_path(path), purpose="metadata lock"
    ):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent, text=True
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _read_metadata_unlocked(path: Path) -> DaemonMetadata | None:
    try:
        descriptor = _open_secure_owned_file(
            path, os.O_RDONLY, purpose="metadata", missing_ok=True
        )
        if descriptor is None:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        expected_fields = {
            "schema_version",
            "pid",
            "instance_id",
            "start_token",
            "host",
            "port",
            "control_token",
        }
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            return None
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            return None
        metadata = DaemonMetadata(
            pid=raw["pid"],
            instance_id=raw["instance_id"],
            start_token=raw["start_token"],
            host=raw["host"],
            port=raw["port"],
            control_token=raw["control_token"],
        )
    except (LifecycleError, OSError, ValueError, KeyError, TypeError):
        return None
    if (
        type(metadata.pid) is not int
        or metadata.pid <= 0
        or type(metadata.instance_id) is not str
        or not metadata.instance_id
        or type(metadata.start_token) is not str
        or not metadata.start_token
        or type(metadata.host) is not str
        or metadata.host != "127.0.0.1"
        or type(metadata.port) is not int
        or not 0 < metadata.port < 65536
        or type(metadata.control_token) is not str
        or not _CONTROL_TOKEN_PATTERN.fullmatch(metadata.control_token)
    ):
        return None
    return metadata


def read_metadata(path: Path) -> DaemonMetadata | None:
    with _exclusive_file_lock(
        _metadata_lock_path(path), purpose="metadata lock"
    ):
        return _read_metadata_unlocked(path)


def _metadata_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return (
        details.st_dev,
        details.st_ino,
        details.st_mtime_ns,
        details.st_size,
    )


def _load_metadata(
    path: Path, *, clean_invalid: bool
) -> tuple[str, DaemonMetadata | None]:
    with _exclusive_file_lock(
        _metadata_lock_path(path), purpose="metadata lock"
    ):
        if _metadata_fingerprint(path) is None:
            return "missing", None
        first = _read_metadata_unlocked(path)
        if first is not None:
            return "valid", first
        fingerprint = _metadata_fingerprint(path)
        second = _read_metadata_unlocked(path)
        if second is not None:
            return "valid", second
        if fingerprint != _metadata_fingerprint(path):
            return "invalid", None
        if clean_invalid:
            path.unlink(missing_ok=True)
        return "invalid", None


def _process_details(pid: int) -> tuple[str, str] | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            stat = proc_stat.read_text(encoding="utf-8")
            remainder = stat.rsplit(")", 1)[1].strip().split()
            if remainder[0] == "Z":
                return None
            start_token = remainder[19]
            command_bytes = Path(f"/proc/{pid}/cmdline").read_bytes()
            command = command_bytes.replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
            return start_token, command
        except (OSError, IndexError, ValueError):
            return None

    try:
        result = subprocess.run(
            # Absolute path, as everywhere else a system tool is invoked: the
            # supervisor gives the daemon a closed environment whose PATH is the
            # generation's own bin directory, so a bare `ps` is unresolvable and
            # the daemon cannot establish its own process identity.
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return None
    fields = output.split(None, 7)
    if len(fields) < 8 or fields[0].startswith("Z"):
        return None
    start_token = " ".join(fields[1:6])
    command = fields[7]
    return start_token, command


def current_process_start_token(pid: int) -> str | None:
    details = _process_details(pid)
    return None if details is None else details[0]


def process_identity_matches(metadata: DaemonMetadata) -> bool:
    details = _process_details(metadata.pid)
    if details is None:
        return False
    start_token, command = details
    return start_token == metadata.start_token and metadata.instance_id in command


def acquire_daemon_lifetime_lock(path: Path) -> int | None:
    """Acquire the daemon-owned non-blocking lock for its entire lifetime."""

    if fcntl is None:
        raise LifecycleError("cortexd lifecycle requires macOS or Linux")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = _open_secure_owned_file(
        path, os.O_CREAT | os.O_RDWR, purpose="lifetime lock"
    )
    assert descriptor is not None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def release_daemon_lifetime_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _loopback_json_request(
    metadata: DaemonMetadata,
    *,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    timeout: float,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object]] | None:
    """Send a direct loopback request without consulting proxy settings."""

    if metadata.host != "127.0.0.1":
        return None
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["X-Cortex-Instance-ID"] = metadata.instance_id
        headers["X-Cortex-Control-Token"] = metadata.control_token
    headers.update(dict(extra_headers or {}))
    connection = http.client.HTTPConnection(
        "127.0.0.1", metadata.port, timeout=timeout
    )
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(65_537)
        if len(response_body) > 65_536:
            return None
        decoded = json.loads(response_body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    if not isinstance(decoded, dict):
        return None
    return response.status, decoded


def _health_ready(metadata: DaemonMetadata, *, timeout: float = 0.4) -> bool:
    result = _loopback_json_request(
        metadata, method="GET", path="/healthz", timeout=timeout
    )
    if result is None:
        return False
    status, payload = result
    return (
        status == 200
        and payload.get("service") == "cortexd"
        and payload.get("status") == "ok"
        and payload.get("instance_id") == metadata.instance_id
    )


def _request_shutdown(metadata: DaemonMetadata, *, timeout: float) -> bool:
    result = _loopback_json_request(
        metadata,
        method="POST",
        path="/__cortex__/shutdown",
        payload={
            "instance_id": metadata.instance_id,
            "control_token": metadata.control_token,
        },
        timeout=timeout,
    )
    if result is None:
        return False
    status, payload = result
    return (
        status == 202
        and payload.get("service") == "cortexd"
        and payload.get("status") == "stopping"
        and payload.get("instance_id") == metadata.instance_id
    )


def _remove_matching_metadata(path: Path, instance_id: str | None = None) -> bool:
    with _exclusive_file_lock(
        _metadata_lock_path(path), purpose="metadata lock"
    ):
        current = _read_metadata_unlocked(path)
        if current is None:
            return False
        if instance_id is not None and current.instance_id != instance_id:
            return False
        fingerprint = _metadata_fingerprint(path)
        confirmed = _read_metadata_unlocked(path)
        if confirmed != current or fingerprint != _metadata_fingerprint(path):
            return False
        path.unlink(missing_ok=True)
        return True


def _status_from_metadata(metadata: DaemonMetadata) -> DaemonStatus:
    if not process_identity_matches(metadata):
        return DaemonStatus(
            "stale",
            pid=metadata.pid,
            port=metadata.port,
            instance_id=metadata.instance_id,
        )
    ready = _health_ready(metadata)
    return DaemonStatus(
        "running" if ready else "unhealthy",
        pid=metadata.pid,
        port=metadata.port,
        instance_id=metadata.instance_id,
        process_identity=True,
        health_ready=ready,
    )


def daemon_status(paths: PathRegistry) -> DaemonStatus:
    metadata_path = paths.daemon_metadata_file
    state, metadata = _load_metadata(metadata_path, clean_invalid=True)
    last_stale: DaemonStatus | None = None
    for _ in range(_STATUS_CONVERGENCE_LIMIT):
        if state == "missing":
            return last_stale or DaemonStatus("stopped")
        if state == "invalid" or metadata is None:
            return DaemonStatus("stale")
        current = _status_from_metadata(metadata)
        if current.state != "stale":
            return current
        last_stale = current
        _remove_matching_metadata(metadata_path, metadata.instance_id)
        state, metadata = _load_metadata(metadata_path, clean_invalid=True)

    if state == "missing":
        return last_stale or DaemonStatus("stopped")
    if state == "invalid" or metadata is None:
        return DaemonStatus("stale")
    return _status_from_metadata(metadata)


def daemon_control_request(
    paths: PathRegistry,
    *,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, object]] | None:
    """One authenticated Control API call to the RUNNING daemon, or None.

    `None` means "no daemon answered", which is a different answer from any
    status the daemon might return: the P5.4 CLI has to be able to tell an
    installation with no running product -- where the operator derives the
    release proof by hand -- from one where the daemon owns the supervisor and
    is the only thing that can derive it.
    """

    status = daemon_status(paths)
    if status.state != "running":
        return None
    metadata = read_metadata(paths.daemon_metadata_file)
    if metadata is None or metadata.instance_id != status.instance_id:
        return None
    headers = {} if idempotency_key is None else {"Idempotency-Key": idempotency_key}
    return _loopback_json_request(
        metadata,
        method=method,
        path=path,
        payload=payload,
        timeout=timeout,
        extra_headers=headers,
    )


def daemon_health(paths: PathRegistry, *, timeout: float = 2.0) -> dict[str, object] | None:
    """The running daemon's `/api/v1/health` payload, or None if none answers."""

    result = daemon_control_request(
        paths, method="GET", path="/api/v1/health", timeout=timeout
    )
    if result is None:
        return None
    status, payload = result
    return payload if status == 200 else None


def _open_daemon_log(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = _open_secure_owned_file(
        path,
        os.O_CREAT | os.O_WRONLY | os.O_APPEND,
        purpose="daemon log",
    )
    assert descriptor is not None
    return os.fdopen(descriptor, "ab", buffering=0)


def _daemon_command(paths: PathRegistry, instance_id: str) -> list[str]:
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
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ]


def _start_daemon_unlocked(
    paths: PathRegistry,
    *,
    environ: Mapping[str, str],
    timeout: float = 10.0,
) -> DaemonStatus:
    existing = daemon_status(paths)
    if existing.state == "running":
        return existing
    if existing.state == "unhealthy":
        raise LifecycleError("cortexd exists but did not pass its health check")

    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance_id = uuid.uuid4().hex
    with _open_daemon_log(paths.daemon_log_file) as log_handle:
        process = subprocess.Popen(
            _daemon_command(paths, instance_id),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=paths.data_dir,
            env=dict(environ),
            close_fds=True,
            start_new_session=True,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            existing = daemon_status(paths)
            if existing.state == "running":
                return existing
            time.sleep(0.05)
            continue
        metadata = read_metadata(paths.daemon_metadata_file)
        if metadata is not None and metadata.instance_id == instance_id:
            status = daemon_status(paths)
            if status.state == "running":
                return status
        time.sleep(0.05)

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
    _remove_matching_metadata(paths.daemon_metadata_file, instance_id)
    raise LifecycleError("cortexd readiness timed out")


def start_daemon(
    paths: PathRegistry,
    *,
    environ: Mapping[str, str],
    timeout: float = 10.0,
) -> DaemonStatus:
    """Start exactly one daemon, serializing concurrent start requests."""

    if fcntl is None:
        raise LifecycleError("cortexd lifecycle requires macOS or Linux")
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = _open_secure_owned_file(
        paths.daemon_start_lock_file,
        os.O_CREAT | os.O_RDWR,
        purpose="start lock",
    )
    assert descriptor is not None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _start_daemon_unlocked(paths, environ=environ, timeout=timeout)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _reap_child(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _wait_for_expected_exit(metadata: DaemonMetadata, *, deadline: float) -> bool:
    while process_identity_matches(metadata):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))
    _reap_child(metadata.pid)
    return True


def _observe_stop_transition(
    metadata_path: Path,
    expected: DaemonMetadata,
    *,
    deadline: float,
    failure_message: str,
) -> DaemonStatus:
    replacements = 0
    while True:
        state, current = _load_metadata(metadata_path, clean_invalid=False)
        if state == "missing":
            if _wait_for_expected_exit(expected, deadline=deadline):
                return DaemonStatus("stopped")
            raise LifecycleError(failure_message)
        if state == "invalid" or current is None:
            return DaemonStatus("stale")
        if current.instance_id != expected.instance_id:
            status = _status_from_metadata(current)
            confirmed_state, confirmed = _load_metadata(
                metadata_path, clean_invalid=False
            )
            if confirmed_state == "missing":
                if _wait_for_expected_exit(expected, deadline=deadline):
                    return DaemonStatus("stopped")
                raise LifecycleError(failure_message)
            if confirmed_state == "invalid" or confirmed is None:
                return DaemonStatus("stale")
            if confirmed == current:
                return status
            replacements += 1
            if replacements >= _STATUS_CONVERGENCE_LIMIT:
                return _status_from_metadata(confirmed)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LifecycleError(failure_message)
        time.sleep(min(0.02, remaining))


def stop_daemon(paths: PathRegistry, *, timeout: float = 10.0) -> DaemonStatus:
    metadata_path = paths.daemon_metadata_file
    deadline = time.monotonic() + timeout
    state, metadata = _load_metadata(metadata_path, clean_invalid=True)
    if state == "missing":
        return DaemonStatus("stopped")
    if state == "invalid" or metadata is None:
        return DaemonStatus("stopped")
    if not process_identity_matches(metadata):
        _remove_matching_metadata(metadata_path, metadata.instance_id)
        return _observe_stop_transition(
            metadata_path,
            metadata,
            deadline=deadline,
            failure_message="cortexd process identity changed during stop",
        )

    if not _health_ready(metadata):
        return _observe_stop_transition(
            metadata_path,
            metadata,
            deadline=deadline,
            failure_message="cortexd health identity handshake failed",
        )
    control_timeout = min(max(timeout, 0.05), 1.0)
    if not _request_shutdown(metadata, timeout=control_timeout):
        return _observe_stop_transition(
            metadata_path,
            metadata,
            deadline=deadline,
            failure_message="cortexd control identity handshake failed",
        )

    while time.monotonic() < deadline:
        state, current = _load_metadata(metadata_path, clean_invalid=False)
        if state == "missing":
            if _wait_for_expected_exit(metadata, deadline=deadline):
                return DaemonStatus("stopped")
            break
        if state != "valid" or current is None:
            raise LifecycleError("cortexd control identity changed during shutdown")
        if current.instance_id != metadata.instance_id:
            return _observe_stop_transition(
                metadata_path,
                metadata,
                deadline=deadline,
                failure_message="cortexd control identity changed during shutdown",
            )
        if not process_identity_matches(metadata):
            _reap_child(metadata.pid)
            _remove_matching_metadata(metadata_path, metadata.instance_id)
            return _observe_stop_transition(
                metadata_path,
                metadata,
                deadline=deadline,
                failure_message="cortexd control identity changed during shutdown",
            )
        time.sleep(0.05)
    raise LifecycleError("cortexd shutdown confirmation timed out")
