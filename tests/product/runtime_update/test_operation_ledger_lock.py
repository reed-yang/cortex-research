from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import canonical_json
from cortex_platform.product.runtime_update.operation_ledger import (
    LedgerUnavailable,
    OperationConflict,
    OperationLedger,
)
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
)


REQUEST_DIGEST = "a" * 64
OTHER_DIGEST = "c" * 64
RESULT_DIGEST = "b" * 64


def _start_holder(state_dir: Path, *, begin: bool = False) -> subprocess.Popen[str]:
    script = r'''
import os
import sys
from pathlib import Path

from cortex_platform.product.runtime_update.operation_ledger import OperationLedger

state_dir = Path(sys.argv[1])
request_digest = sys.argv[2]
begin = sys.argv[3] == "begin"
ledger = OperationLedger.open(state_dir)
if begin:
    ledger.begin("op-shared", "run", request_digest)
print("ready", flush=True)
command = sys.stdin.readline().strip()
if command == "crash":
    os._exit(97)
ledger.close()
'''
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(state_dir),
            REQUEST_DIGEST,
            "begin" if begin else "hold",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        },
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def _stop_holder(process: subprocess.Popen[str], command: str = "stop") -> None:
    if process.poll() is None:
        assert process.stdin is not None
        process.stdin.write(command + "\n")
        process.stdin.flush()
    stdout, stderr = process.communicate(timeout=30)
    expected = 97 if command == "crash" else 0
    assert process.returncode == expected, (stdout, stderr)


def _copy_state(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for item in source.rglob("*"):
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(mode=0o700)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


def _service(
    root: Path,
    catalog: dict,
    attestation: dict,
    stager: FakePythonStager | None = None,
) -> RuntimeUpdateService:
    return RuntimeUpdateService(
        root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=stager or FakePythonStager(),
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )


def _seed_inheritable_ledger(state_dir: Path) -> None:
    worker_state = state_dir / ".cortex-worker"
    ledger = OperationLedger.open(worker_state)
    assert ledger.begin("op-committed", "run", REQUEST_DIGEST) == "accepted"
    ledger.finish("op-committed", "committed", RESULT_DIGEST)
    assert ledger.begin("op-interrupted", "run", OTHER_DIGEST) == "accepted"
    ledger.close()


def _assert_inherited_ledger(state_dir: Path) -> None:
    worker_state = state_dir / ".cortex-worker"
    ledger = OperationLedger.open(worker_state)
    assert ledger.begin("op-committed", "run", REQUEST_DIGEST) == "duplicate"
    assert ledger.status("op-interrupted").state == "uncertain"
    ledger.close()


def test_process_lock_releases_after_clean_child_exit(tmp_path: Path) -> None:
    state_dir = tmp_path / ".cortex-worker"
    child = _start_holder(state_dir)
    try:
        with pytest.raises(
            LedgerUnavailable,
            match="operation ledger is owned by another process",
        ):
            OperationLedger.open(state_dir)
    finally:
        _stop_holder(child)

    reopened = OperationLedger.open(state_dir)
    assert reopened.status("unknown").state == "unknown"
    reopened.close()


def test_process_lock_prevents_dual_accept_with_conflicting_digest(tmp_path: Path) -> None:
    state_dir = tmp_path / ".cortex-worker"
    child = _start_holder(state_dir, begin=True)
    ledger_path = state_dir / "operations.ledger.jsonl"
    before = ledger_path.read_bytes()
    try:
        with pytest.raises(LedgerUnavailable):
            parent = OperationLedger.open(state_dir)
            parent.begin("op-shared", "run", OTHER_DIGEST)
        assert ledger_path.read_bytes() == before
    finally:
        _stop_holder(child)

    reopened = OperationLedger.open(state_dir)
    assert reopened.status("op-shared").state == "uncertain"
    with pytest.raises(OperationConflict):
        reopened.begin("op-shared", "run", OTHER_DIGEST)
    reopened.close()


def test_process_lock_releases_after_abrupt_child_death(tmp_path: Path) -> None:
    state_dir = tmp_path / ".cortex-worker"
    child = _start_holder(state_dir, begin=True)
    _stop_holder(child, "crash")

    reopened = OperationLedger.open(state_dir)
    status = reopened.status("op-shared")
    assert (status.state, status.request_digest, status.result_digest) == (
        "uncertain",
        REQUEST_DIGEST,
        None,
    )
    reopened.close()


def test_copied_worker_namespace_inherits_dedup_state_and_not_lock(tmp_path: Path) -> None:
    source = tmp_path / "generation-a"
    source.mkdir()
    _seed_inheritable_ledger(source)
    destination = tmp_path / "generation-b"

    _copy_state(source, destination)

    assert (destination / ".cortex-worker" / "worker.lock").is_file()
    _assert_inherited_ledger(destination)


def test_real_stage_source_state_preserves_quarantine_sidecar(
    tmp_path: Path,
    release_factory,
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    first = service.stage(manifest["release_id"])
    worker_state = first.state_dir / ".cortex-worker"
    worker_state.mkdir()
    ledger_path = worker_state / "operations.ledger.jsonl"
    ledger_path.write_bytes(b'{"torn":')
    ledger_path.chmod(0o600)
    ledger = OperationLedger.open(worker_state)
    ledger.close()
    sidecar = worker_state / "operations.ledger.quarantine.json"
    ledger_evidence = ledger_path.read_bytes()
    sidecar_evidence = sidecar.read_bytes()

    successor = service.stage(manifest["release_id"], source_state=first.state_dir)
    inherited = successor.state_dir / ".cortex-worker"

    assert (inherited / ledger_path.name).read_bytes() == ledger_evidence
    assert (inherited / sidecar.name).read_bytes() == sidecar_evidence
    reopened = OperationLedger.open(inherited)
    assert reopened.quarantined is True
    reopened.close()


def test_real_stage_source_state_inherits_worker_namespace(
    tmp_path: Path,
    release_factory,
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    first = service.stage(manifest["release_id"])
    _seed_inheritable_ledger(first.state_dir)

    successor = service.stage(manifest["release_id"], source_state=first.state_dir)

    assert successor.state_dir != first.state_dir
    assert (successor.state_dir / ".cortex-worker" / "worker.lock").is_file()
    _assert_inherited_ledger(successor.state_dir)
