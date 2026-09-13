from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update import operation_ledger as operation_ledger_module
from cortex_platform.product.runtime_update.operation_ledger import (
    LedgerUnavailable,
    OperationConflict,
    OperationLedger,
)


REQUEST_DIGEST = "a" * 64
RESULT_DIGEST = "b" * 64


def _ledger_path(state_dir: Path) -> Path:
    return state_dir / "operations.ledger.jsonl"


def _quarantine_path(state_dir: Path) -> Path:
    return state_dir / "operations.ledger.quarantine.json"


def _lock_path(state_dir: Path) -> Path:
    return state_dir / "worker.lock"


def test_begin_is_durable_and_deduplicates_same_digest(tmp_path: Path) -> None:
    ledger = OperationLedger.open(tmp_path)
    assert ledger.begin("op-1", "run", REQUEST_DIGEST) == "accepted"
    recorded = _ledger_path(tmp_path).read_bytes()
    assert recorded.endswith(b"\n")
    assert json.loads(recorded) == {
        "schema_version": 1,
        "operation_id": "op-1",
        "kind": "run",
        "phase": "in_progress",
        "request_digest": REQUEST_DIGEST,
        "result_digest": None,
        "recorded_at_monotonic": pytest.approx(json.loads(recorded)["recorded_at_monotonic"]),
    }
    assert ledger.begin("op-1", "run", REQUEST_DIGEST) == "duplicate"
    assert _ledger_path(tmp_path).read_bytes() == recorded


def test_begin_conflict_does_not_write(tmp_path: Path) -> None:
    ledger = OperationLedger.open(tmp_path)
    ledger.begin("op-1", "run", REQUEST_DIGEST)
    recorded = _ledger_path(tmp_path).read_bytes()
    with pytest.raises(OperationConflict):
        ledger.begin("op-1", "run", "c" * 64)
    assert _ledger_path(tmp_path).read_bytes() == recorded


def test_finish_is_allowed_exactly_once(tmp_path: Path) -> None:
    ledger = OperationLedger.open(tmp_path)
    ledger.begin("op-1", "run", REQUEST_DIGEST)
    ledger.finish("op-1", "committed", RESULT_DIGEST)
    status = ledger.status("op-1")
    assert (status.state, status.request_digest, status.result_digest) == (
        "committed",
        REQUEST_DIGEST,
        RESULT_DIGEST,
    )
    recorded = _ledger_path(tmp_path).read_bytes()
    with pytest.raises(OperationConflict):
        ledger.finish("op-1", "failed", "c" * 64)
    assert _ledger_path(tmp_path).read_bytes() == recorded


def test_reopen_promotes_in_progress_to_uncertain_before_returning(tmp_path: Path) -> None:
    ledger = OperationLedger.open(tmp_path)
    ledger.begin("op-1", "run", REQUEST_DIGEST)
    ledger.close()

    reopened = OperationLedger.open(tmp_path)
    status = reopened.status("op-1")
    assert (status.state, status.request_digest, status.result_digest) == (
        "uncertain",
        REQUEST_DIGEST,
        None,
    )
    records = [json.loads(line) for line in _ledger_path(tmp_path).read_bytes().splitlines()]
    assert [record["phase"] for record in records] == ["in_progress", "uncertain"]
    reopened.finish("op-1", "failed", RESULT_DIGEST)
    assert reopened.status("op-1").state == "failed"


def test_status_distinguishes_unknown_from_interrupted(tmp_path: Path) -> None:
    ledger = OperationLedger.open(tmp_path)
    unknown = ledger.status("never-arrived")
    assert (unknown.state, unknown.request_digest, unknown.result_digest) == (
        "unknown",
        None,
        None,
    )
    ledger.begin("op-1", "run", REQUEST_DIGEST)
    ledger.close()
    assert OperationLedger.open(tmp_path).status("op-1").state == "uncertain"


def test_unparseable_torn_in_progress_is_quarantined_without_inferred_state(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    record = {
        "schema_version": 1,
        "operation_id": "op-torn",
        "kind": "run",
        "phase": "in_progress",
        "request_digest": REQUEST_DIGEST,
        "result_digest": None,
        "recorded_at_monotonic": 1.0,
    }
    torn = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()[:-1]
    path.write_bytes(torn)
    path.chmod(0o600)

    ledger = OperationLedger.open(tmp_path)
    assert path.read_bytes().startswith(torn)
    assert ledger.status("op-torn").state == "unknown"
    assert _quarantine_path(tmp_path).exists()
    assert ledger.begin("op-new", "run", "c" * 64) == "accepted"
    ledger.close()

    reopened = OperationLedger.open(tmp_path)
    assert reopened.status("op-torn").state == "unknown"
    assert reopened.status("op-new").state == "uncertain"


def test_unidentifiable_torn_tail_is_durably_quarantined_and_reopens(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    torn = b'{"unidentifiable":'
    path.write_bytes(torn)
    path.chmod(0o600)

    ledger = OperationLedger.open(tmp_path)
    sidecar = _quarantine_path(tmp_path)
    assert ledger.quarantined is True
    assert path.read_bytes() == torn + b"\n"
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    assert json.loads(sidecar.read_bytes()) == {
        "schema_version": 1,
        "offset": 0,
        "length": len(torn),
        "fragment_sha256": hashlib.sha256(torn).hexdigest(),
    }
    assert ledger.begin("op-new", "run", REQUEST_DIGEST) == "accepted"
    assert path.read_bytes().startswith(torn + b"\n{")
    ledger.close()

    reopened = OperationLedger.open(tmp_path)
    assert reopened.quarantined is True
    assert reopened.status("op-new").state == "uncertain"


def test_quarantine_sidecar_cannot_hide_a_parseable_record(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    record = {
        "schema_version": 1,
        "operation_id": "op-hidden",
        "kind": "run",
        "phase": "committed",
        "request_digest": REQUEST_DIGEST,
        "result_digest": RESULT_DIGEST,
        "recorded_at_monotonic": 1.0,
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload + b"\n")
    path.chmod(0o600)
    sidecar = _quarantine_path(tmp_path)
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "offset": 0,
                "length": len(payload),
                "fragment_sha256": hashlib.sha256(payload).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    sidecar.chmod(0o600)
    ledger_before = path.read_bytes()
    sidecar_before = sidecar.read_bytes()

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(tmp_path)
    assert path.read_bytes() == ledger_before
    assert sidecar.read_bytes() == sidecar_before


def test_quarantine_sidecar_hash_mismatch_is_refused_without_mutation(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    torn = b'{"unidentifiable":'
    path.write_bytes(torn)
    path.chmod(0o600)
    ledger = OperationLedger.open(tmp_path)
    before = path.read_bytes()
    sidecar = _quarantine_path(tmp_path)
    marker = json.loads(sidecar.read_bytes())
    marker["fragment_sha256"] = "0" * 64
    sidecar.write_text(json.dumps(marker), encoding="utf-8")
    ledger.close()

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(tmp_path)
    assert path.read_bytes() == before


def test_second_unparseable_fragment_after_quarantine_is_refused(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    path.write_bytes(b'{"first":')
    path.chmod(0o600)
    ledger = OperationLedger.open(tmp_path)
    ledger.begin("op-new", "run", REQUEST_DIGEST)
    with path.open("ab") as handle:
        handle.write(b'{"second":')
    before = path.read_bytes()
    ledger.close()

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(tmp_path)
    assert path.read_bytes() == before


def test_complete_record_without_final_newline_replays_normally(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    record = {
        "schema_version": 1,
        "operation_id": "op-complete",
        "kind": "run",
        "phase": "in_progress",
        "request_digest": REQUEST_DIGEST,
        "result_digest": None,
        "recorded_at_monotonic": 1.0,
    }
    path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)

    ledger = OperationLedger.open(tmp_path)
    assert ledger.quarantined is False
    assert not _quarantine_path(tmp_path).exists()
    assert ledger.status("op-complete").state == "uncertain"


def test_malformed_non_trailing_line_is_refused_without_mutation(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    original = b'{"broken":\n{"also":"evidence"}\n'
    path.write_bytes(original)
    path.chmod(0o600)
    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(tmp_path)
    assert path.read_bytes() == original


def test_ledger_file_is_private_and_symlinks_are_refused(tmp_path: Path) -> None:
    ledger = OperationLedger.open(tmp_path / "new")
    ledger.begin("op-1", "run", REQUEST_DIGEST)
    assert stat.S_IMODE(_ledger_path(tmp_path / "new").stat().st_mode) == 0o600

    target = tmp_path / "target"
    target.write_text("external", encoding="utf-8")
    state = tmp_path / "linked"
    state.mkdir()
    _ledger_path(state).symlink_to(target)
    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(state)
    assert target.read_text(encoding="utf-8") == "external"


def test_existing_non_private_ledger_is_refused(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    path.write_bytes(b"")
    path.chmod(0o644)
    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(tmp_path)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o644


def test_same_process_second_open_fails_until_owner_closes(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    owner = OperationLedger.open(state_dir)
    assert owner.begin("op-1", "run", REQUEST_DIGEST) == "accepted"

    with pytest.raises(
        LedgerUnavailable,
        match="operation ledger is owned by another process",
    ):
        OperationLedger.open(state_dir)

    assert owner.status("op-1").state == "in_progress"
    owner.close()
    reopened = OperationLedger.open(state_dir)
    assert reopened.status("op-1").state == "uncertain"


@pytest.mark.parametrize("method", ["begin", "finish", "status"])
def test_methods_fail_closed_after_close(tmp_path: Path, method: str) -> None:
    ledger = OperationLedger.open(tmp_path)
    ledger.close()

    with pytest.raises(LedgerUnavailable):
        if method == "begin":
            ledger.begin("op-1", "run", REQUEST_DIGEST)
        elif method == "finish":
            ledger.finish("op-1", "committed", RESULT_DIGEST)
        else:
            ledger.status("op-1")


def test_state_dir_is_created_privately_without_creating_parent(tmp_path: Path) -> None:
    parent = tmp_path / "generation"
    parent.mkdir()
    state_dir = parent / ".cortex-worker"

    ledger = OperationLedger.open(state_dir)

    assert stat.S_IMODE(os.lstat(state_dir).st_mode) == 0o700
    lock_details = os.lstat(_lock_path(state_dir))
    assert stat.S_ISREG(lock_details.st_mode)
    assert stat.S_IMODE(lock_details.st_mode) == 0o600
    ledger.close()

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(tmp_path / "missing-generation" / ".cortex-worker")
    assert not (tmp_path / "missing-generation").exists()


def test_symlinked_state_dir_is_refused_before_file_creation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    state_dir = tmp_path / "linked-state"
    state_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(state_dir)

    assert list(target.iterdir()) == []


def test_symlinked_worker_lock_is_refused_without_touching_target(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"external-lock-evidence")
    _lock_path(state_dir).symlink_to(target)

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(state_dir)

    assert target.read_bytes() == b"external-lock-evidence"
    assert not _ledger_path(state_dir).exists()


def test_state_dir_replacement_after_prepare_cannot_redirect_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    original = tmp_path / "original"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    prepare = operation_ledger_module._prepare_state_dir

    def replace_after_prepare(path: Path):
        result = prepare(path)
        path.rename(original)
        path.symlink_to(attacker, target_is_directory=True)
        return result

    monkeypatch.setattr(operation_ledger_module, "_prepare_state_dir", replace_after_prepare)

    with pytest.raises(LedgerUnavailable):
        OperationLedger.open(state_dir)

    assert list(attacker.iterdir()) == []
