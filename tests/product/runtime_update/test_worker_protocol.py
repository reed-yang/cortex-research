from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.supervisor import (
    WorkerCrashed,
    WorkerProtocolError,
    WorkerSupervisor,
)


_ENTRYPOINT = "runtime_worker.py"


def _candidate(tmp_path: Path, body: str) -> tuple[Path, Path]:
    candidate = tmp_path / "candidate"
    state = tmp_path / "state"
    candidate.mkdir()
    state.mkdir()
    (candidate / _ENTRYPOINT).write_text(body, encoding="utf-8")
    return candidate, state


def test_stdio_worker_health_and_secret_home_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch environment replaces the parent's rather than extending it.

    S3.3 removes the `inherited_env` parameter this used to pass: it was
    stored and never merged, so it read like an env-injection point that had
    never been one. The property it was standing in for is real and is
    checked here directly — a credential in the parent's own environment
    does not reach the worker, because the worker's environment is built,
    not inherited.
    """

    monkeypatch.setenv("TEST_PROVIDER_TOKEN", "not-a-real-credential")
    monkeypatch.setenv("HOME", "/real/home")
    candidate, state = _candidate(
        tmp_path,
        """\
import os
def handle(method, params):
    return {
        "status": "healthy",
        "home": os.environ.get("HOME"),
        "secret_present": "TEST_PROVIDER_TOKEN" in os.environ,
    }
""",
    )
    with WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint=_ENTRYPOINT,
    ) as worker:
        result = worker.request("health", {}, timeout=2)
    assert result == {
        "status": "healthy",
        "home": str(state),
        "secret_present": False,
    }


def test_worker_denies_network_and_paths_outside_slot_and_state(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    candidate, state = _candidate(
        tmp_path,
        f"""\
import os
import socket
def handle(method, params):
    if method == "network":
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    if method == "outside":
        return {{"value": open({str(outside)!r}, encoding="utf-8").read()}}
    if method == "mutate_candidate":
        os.chmod(__file__, 0o600)
        open(__file__, "w", encoding="utf-8").write("tampered")
    if method == "write_state":
        open(os.path.join(os.environ["HOME"], "allowed.txt"), "w", encoding="utf-8").write("ok")
        return {{"status": "written"}}
    return {{"status": "healthy"}}
""",
    )
    with WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint=_ENTRYPOINT,
    ) as worker:
        with pytest.raises(WorkerProtocolError, match="sandbox_denied"):
            worker.request("network", {}, timeout=2)
        with pytest.raises(WorkerProtocolError, match="sandbox_denied"):
            worker.request("outside", {}, timeout=2)
        with pytest.raises(WorkerProtocolError, match="sandbox_denied"):
            worker.request("mutate_candidate", {}, timeout=2)
        assert worker.request("write_state", {}, timeout=2) == {"status": "written"}
        assert (state / "allowed.txt").read_text(encoding="utf-8") == "ok"


def test_worker_crash_and_protocol_corruption_are_contained(tmp_path: Path) -> None:
    candidate, state = _candidate(
        tmp_path,
        """\
import os
def handle(method, params):
    if method == "crash":
        os._exit(23)
    return {"status": "healthy"}
""",
    )
    worker = WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint=_ENTRYPOINT,
    )
    worker.start()
    with pytest.raises(WorkerCrashed):
        worker.request("crash", {}, timeout=2)
    worker.close()

    (candidate / _ENTRYPOINT).write_text(
        "def handle(method, params): return object()\n", encoding="utf-8"
    )
    with WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint=_ENTRYPOINT,
    ) as corrupt:
        with pytest.raises(WorkerProtocolError):
            corrupt.request("health", {}, timeout=2)
