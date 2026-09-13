from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import (
    canonical_json,
    digest_document,
    host_platform,
)
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
    _tree_digest,
)
from cortex_platform.product.runtime_update.worker_launch import build_descriptor
from cortex_platform.product.runtime_update.supervisor import (
    WorkerProtocolError,
    WorkerSupervisorV2,
    spawn_v2,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.serve import (
    measure_identity,
)
from cortex_platform.product.runtime_update.worker_payload import (
    ENTRYPOINT_SOURCE,
    module_sources,
)
from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    SlotInterpreterDescriptor,
)


TOKEN = "v2-private-token-with-more-than-32-bytes"
REQUEST_DIGEST = "a" * 64
RESULT_DIGEST = "b" * 64


def _slot(tmp_path: Path) -> tuple[Path, dict[str, object], str, str, str]:
    artifact_digest = "d" * 64
    slot = tmp_path / "slots" / artifact_digest
    content = slot / "content"
    package = content / "package"
    package.mkdir(parents=True)
    # The launch contract now executes `content/<worker_entrypoint>`, so the slot
    # carries the real attested entrypoint and the real worker package beside it
    # — the same files `package_hermes_release.py` copies into a payload.
    (content / "runtime_worker.py").write_bytes(ENTRYPOINT_SOURCE.read_bytes())
    (content / "cortex_worker").mkdir()
    for relative, source in module_sources().items():
        (content / relative).write_bytes(source.read_bytes())
    (package / "data.txt").write_text("synthetic worker content\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "schema_version": 3,
        "release_id": "release-1",
        "distribution_name": "hermes-agent",
        "distribution_version": "synthetic",
        "worker_entrypoint": "runtime_worker.py",
        # Not validated by this path, but a fixture that names a schema which no
        # longer exists is a fixture that has started drifting.
        "platform": host_platform(),
    }
    manifest_digest = digest_document(manifest)
    content_digest = _tree_digest(content)
    (slot / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (slot / "attestation.json").write_text("{}\n", encoding="utf-8")
    (slot / "slot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_sha256": artifact_digest,
                "manifest_sha256": manifest_digest,
                "content_tree_sha256": content_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return slot, manifest, artifact_digest, manifest_digest, content_digest


def _interpreter_digest(executable: Path | None = None) -> str:
    """What the worker will measure: the bytes of the interpreter it runs from."""

    resolved = (executable or Path(sys.executable)).resolve(strict=True)
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _descriptor(
    tmp_path: Path,
    *,
    artifact_digest: str | None = None,
    content_digest: str | None = None,
    manifest_digest: str | None = None,
    interpreter_digest: str | None = None,
) -> tuple[Path, SlotInterpreterDescriptor]:
    slot, _, observed_artifact, observed_manifest, observed_content = _slot(tmp_path)
    value = {
        "schema_version": 1,
        "slot_path": str(slot),
        "slot_id": "primary",
        "state_generation_id": "generation-1",
        "release_id": "release-1",
        "expected_artifact_digest": artifact_digest or observed_artifact,
        "expected_manifest_sha256": manifest_digest or observed_manifest,
        "expected_content_tree_sha256": content_digest or observed_content,
        "expected_interpreter_sha256": interpreter_digest or _interpreter_digest(),
        "interpreter_path": sys.executable,
        "worker_entrypoint": "runtime_worker.py",
        "state_dir": str(tmp_path / "state"),
        "worker_protocol": PROTOCOL_V2,
    }
    path = tmp_path / "descriptor.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, SlotInterpreterDescriptor.load(path)


def _real_descriptor(
    tmp_path: Path, release_factory, vendored_worker_runtime
) -> tuple[Path, SlotInterpreterDescriptor]:
    """A descriptor built the way production builds one, end to end.

    Nothing is faked below the descriptor: the release carries the real vendored
    cp311 archive and the product's own worker payload, `stage` expands and
    probes that archive through the wheel-shipped kernel, and `build_descriptor`
    derives the interpreter path and its digest from the pin `stage` wrote. The
    supervisor then launches the slot's own `bin/python3.11`.
    """

    archive, _pin = vendored_worker_runtime
    artifact, manifest, catalog, attestation = release_factory(
        runtime_archive=archive.read_bytes(), real_worker_payload=True
    )
    manifest["adapter_protocol"] = PROTOCOL_V2
    catalog["payload"]["entries"][0]["manifest_sha256"] = digest_document(manifest)
    service = RuntimeUpdateService(
        tmp_path / "updates",
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda _: True)
    pin = service.pin_attempt("attempt-e2e")
    descriptor = build_descriptor(service, pin.attempt_id)
    path = tmp_path / "real-descriptor.json"
    path.write_text(json.dumps(asdict(descriptor), default=str), encoding="utf-8")
    return path, descriptor


def _identity_then_unhealthy(supervisor: WorkerSupervisorV2):
    """Stub request() that measures a matching identity but reports an unopened ledger."""
    descriptor = supervisor.descriptor

    def request(method, params, *, timeout=3):
        if method == "identity.measure":
            return {
                "worker_protocol": PROTOCOL_V2,
                "artifact_digest": descriptor.expected_artifact_digest,
                "content_tree_sha256": descriptor.expected_content_tree_sha256,
                "manifest_sha256": descriptor.expected_manifest_sha256,
                "slot_id": descriptor.slot_id,
                "state_generation_id": descriptor.state_generation_id,
                "release_id": descriptor.release_id,
                "interpreter_sha256": descriptor.expected_interpreter_sha256,
            }
        return {
            "healthy": False,
            "protocol": PROTOCOL_V2,
            "ledger_open": False,
            "quarantined": False,
        }

    return request


def _raw_worker(
    descriptor_path: Path, descriptor: SlotInterpreterDescriptor
) -> subprocess.Popen[str]:
    env = {
        "HOME": str(descriptor_path.parent),
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "CORTEX_WORKER_TOKEN": TOKEN,
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(descriptor.slot_path / "content" / descriptor.worker_entrypoint),
            "--v2-descriptor",
            str(descriptor_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=descriptor_path.parent,
        close_fds=True,
    )


def _request(process: subprocess.Popen[str], method: str, params: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(
        json.dumps(
            {
                "protocol": PROTOCOL_V2,
                "request_id": "raw-request",
                "token": TOKEN,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    return json.loads(process.stdout.readline())


def _stop_raw(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        _request(process, "shutdown", {})
    process.wait(timeout=3)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def test_worker_content_measurement_matches_updater_canonical_tree(tmp_path: Path) -> None:
    _, descriptor = _descriptor(tmp_path)
    identity = measure_identity(descriptor)
    assert identity["content_tree_sha256"] == _tree_digest(descriptor.slot_path / "content")
    assert identity["content_tree_sha256"] == descriptor.expected_content_tree_sha256
    assert identity["manifest_sha256"] == descriptor.expected_manifest_sha256
    assert identity["artifact_digest"] == descriptor.expected_artifact_digest


def test_raw_worker_rejects_operations_until_identity_is_measured(tmp_path: Path) -> None:
    path, descriptor = _descriptor(tmp_path)
    process = _raw_worker(path, descriptor)
    try:
        response = _request(process, "operation.status", {"operation_id": "op-1"})
        assert response["ok"] is False
        assert response["error"] == {
            "category": "not_measured",
            "message": "worker identity has not been measured",
        }
        identity = _request(process, "identity.measure", {})
        assert identity["ok"] is True
        health = _request(process, "health.check", {})
        assert health["result"] == {
            "healthy": True,
            "protocol": PROTOCOL_V2,
            "ledger_open": True,
            "quarantined": False,
        }
    finally:
        _stop_raw(process)


def test_identity_measure_rechecks_slot_after_worker_startup(tmp_path: Path) -> None:
    path, descriptor = _descriptor(tmp_path)
    process = _raw_worker(path, descriptor)
    try:
        health = _request(process, "health.check", {})
        assert health["result"]["healthy"] is True
        (descriptor.slot_path / "content" / "package" / "data.txt").write_text(
            "tampered after startup\n", encoding="utf-8"
        )
        measured = _request(process, "identity.measure", {})
        assert measured["error"]["category"] == "identity_mismatch"
    finally:
        _stop_raw(process)


def test_tampered_slot_reports_identity_mismatch_and_refuses_operations(tmp_path: Path) -> None:
    path, descriptor = _descriptor(tmp_path)
    (descriptor.slot_path / "content" / "package" / "data.txt").write_text(
        "tampered\n", encoding="utf-8"
    )
    process = _raw_worker(path, descriptor)
    try:
        measured = _request(process, "identity.measure", {})
        assert measured["error"]["category"] == "identity_mismatch"
        health = _request(process, "health.check", {})
        assert health["result"]["healthy"] is False
        refused = _request(process, "operation.status", {"operation_id": "op-1"})
        assert refused["error"]["category"] == "identity_mismatch"
    finally:
        _stop_raw(process)


def test_unknown_wire_method_is_a_sanitized_protocol_violation(tmp_path: Path) -> None:
    path, descriptor = _descriptor(tmp_path)
    process = _raw_worker(path, descriptor)
    try:
        response = _request(process, "arbitrary.call", {})
        assert response["ok"] is False
        assert response["error"]["category"] == "protocol_violation"
    finally:
        _stop_raw(process)


def test_spawn_v2_runs_operation_lifecycle_in_real_subprocess(tmp_path: Path) -> None:
    path, descriptor = _descriptor(tmp_path)
    with spawn_v2(path) as worker:
        identity = worker.identity
        assert identity["artifact_digest"] == descriptor.expected_artifact_digest
        assert worker.request(
            "operation.begin",
            {"operation_id": "op-1", "kind": "run", "request_digest": REQUEST_DIGEST},
        ) == {"state": "accepted"}
        assert worker.request("operation.status", {"operation_id": "op-1"}) == {
            "state": "in_progress",
            "request_digest": REQUEST_DIGEST,
        }
        assert worker.request(
            "operation.finish",
            {"operation_id": "op-1", "outcome": "committed", "result_digest": RESULT_DIGEST},
        ) == {"state": "committed"}
        assert worker.request("operation.status", {"operation_id": "op-1"}) == {
            "state": "committed",
            "request_digest": REQUEST_DIGEST,
            "result_digest": RESULT_DIGEST,
        }


def test_factory_descriptor_spawn_requires_exclusive_healthy_ledger(
    tmp_path: Path,
    release_factory,
    vendored_worker_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole S3.2 chain in one subprocess: the slot runs its own 3.11."""

    path, descriptor = _real_descriptor(
        tmp_path, release_factory, vendored_worker_runtime
    )
    assert descriptor.interpreter_path.name == "python3.11"
    requests: list[str] = []
    original_request = WorkerSupervisorV2.request

    def recording_request(self, method, params, *, timeout=3):
        requests.append(method)
        return original_request(self, method, params, timeout=timeout)

    monkeypatch.setattr(WorkerSupervisorV2, "request", recording_request)
    first = spawn_v2(path)
    first.start()
    assert requests[:2] == ["identity.measure", "health.check"]
    try:
        assert first.identity["state_generation_id"] == descriptor.state_generation_id
        assert first.request(
            "operation.begin",
            {"operation_id": "op-real", "kind": "run", "request_digest": REQUEST_DIGEST},
        ) == {"state": "accepted"}
        first.request(
            "operation.finish",
            {"operation_id": "op-real", "outcome": "committed", "result_digest": RESULT_DIGEST},
        )

        second = spawn_v2(path)
        with pytest.raises(WorkerProtocolError, match="ledger_unavailable"):
            second.start()
        assert second.process is None
        assert first.request("health.check", {})["ledger_open"] is True
        assert first.request("operation.status", {"operation_id": "op-real"})["state"] == "committed"
    finally:
        first.close()

    with spawn_v2(path) as reopened:
        assert reopened.request(
            "operation.begin",
            {"operation_id": "op-real", "kind": "run", "request_digest": REQUEST_DIGEST},
        ) == {"state": "duplicate"}


# ⟦AMD-5⟧ These two drive `start()` into `close(force=True)` while the
# reader and stderr threads are still coming up, which is the race that used
# to kill them before their `finally`. A thread that dies there only ever
# surfaced as a warning, so for these two it is an error.
@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_health_gate_preserves_sanitized_error_when_killed_worker_never_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _descriptor(tmp_path)
    supervisor = WorkerSupervisorV2(path)

    class StuckProcess:
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("worker", timeout)

    process = StuckProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(supervisor, "request", _identity_then_unhealthy(supervisor))

    with pytest.raises(WorkerProtocolError, match="ledger_unavailable"):
        supervisor.start()
    assert supervisor.process is None


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_health_gate_force_kills_worker_that_ignores_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _descriptor(tmp_path)
    supervisor = WorkerSupervisorV2(path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"

    monkeypatch.setattr(supervisor, "request", _identity_then_unhealthy(supervisor))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    try:
        with pytest.raises(WorkerProtocolError, match="ledger_unavailable"):
            supervisor.start()
        assert supervisor.process is None
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_supervisor_kills_worker_on_wrong_expected_digest(tmp_path: Path) -> None:
    path, _ = _descriptor(tmp_path, content_digest="f" * 64)
    supervisor = WorkerSupervisorV2(path)
    with pytest.raises(WorkerProtocolError, match="identity_mismatch"):
        supervisor.start()
    assert supervisor.process is None


def test_real_hermes_backend_does_not_claim_durable_operation_deduplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cortex_platform.runtime.hermes import _NativeHermesBackend

    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(SCHEMA_VERSION=13, SessionDB=object),
        agent_class=object,
        set_approval_callback=lambda callback: None,
        session_db_path=None,
        agent_options={},
    )
    assert backend.capabilities().durable_operation_deduplication is False


def test_orchestration_does_not_import_worker_v2_modules() -> None:
    orchestration = Path(__file__).parents[3] / "cortex_platform" / "product" / "orchestration"
    forbidden = ("runtime_update.worker_protocol", "runtime_update.operation_ledger")
    violations = {
        str(path.relative_to(orchestration)): name
        for path in orchestration.rglob("*.py")
        for name in forbidden
        if name in path.read_text(encoding="utf-8")
    }
    assert violations == {}


def test_a_late_reply_to_an_abandoned_request_keeps_the_channel_usable(
    tmp_path: Path,
) -> None:
    """P5-02: a slow provider must not poison the managed-worker channel.

    The product and the worker derived the same 40.0 s deadline for a
    `telegram.poll`, so the product abandoned the frame at the instant the
    worker was still going to answer it. The reply then arrived for a request
    id nobody was waiting for, and `_route_reply` failed the whole channel as a
    correlation fault -- taking down turns, the ledger and the transport with
    it, because a Bot API call was slow.

    A real worker, a real socket to a loopback Bot API that answers late, and a
    real abandoned deadline. Correlation is still enforced: only ids this side
    actually issued and gave up on are dropped.
    """

    import time

    from .fake_bot_api import FAKE_TOKEN, FakeBotAPI

    api = FakeBotAPI()
    base_url = api.start()
    api.queue_hang(1.5, method="getUpdates")
    path, _ = _descriptor(tmp_path)
    supervisor = WorkerSupervisorV2(
        path,
        environment={
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TELEGRAM_API_BASE_URL": base_url,
            "TELEGRAM_BOT_TOKEN_RESEARCH": FAKE_TOKEN,
        },
    )
    try:
        supervisor.start()
        with pytest.raises(WorkerProtocolError, match="worker response timed out"):
            supervisor.request(
                "telegram.poll",
                {"offset": None, "timeout_seconds": 0},
                timeout=0.3,
            )
        # Let the late reply land while nobody is waiting for it.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not api.polled():
            time.sleep(0.05)
        time.sleep(2.0)

        assert supervisor.alive
        capability = supervisor.request("telegram.capabilities", {}, timeout=10)
        assert capability["protocol"] == "cortex.telegram.transport/1"
    finally:
        supervisor.close(force=True)
        api.stop()


def test_a_reply_to_a_request_that_was_never_issued_still_fails_the_channel(
    tmp_path: Path,
) -> None:
    """The abandoned set narrows the rule; it does not remove it."""

    from cortex_platform.product.runtime_update.worker_protocol import WorkerResponse

    path, _ = _descriptor(tmp_path)
    supervisor = WorkerSupervisorV2(path)

    supervisor._route_reply(
        WorkerResponse(request_id="never-issued", result={}, error=None)
    )

    assert supervisor._failure == "worker replied to an unknown request"
