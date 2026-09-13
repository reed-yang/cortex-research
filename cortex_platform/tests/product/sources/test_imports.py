from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from cortex_platform.product.control import IdempotencyConflict, InvalidTransition
from cortex_platform.product.sources import ImportRequest, ImportResult, SourceImportDispatcher
from cortex_platform.tests.support.sources import (
    InjectedImportCrash,
    TemporaryResearchImportAdapter,
    _OpenManifestAnchor,
    _PreparedStateFrame,
)

from .fakes import (
    MutableClock,
    create_golden_intent,
    make_named_run,
    make_run,
    make_store,
    register_echo,
)


def _state_counts(path: Path) -> dict[str, int]:
    state = TemporaryResearchImportAdapter.inspect_operation_state(
        database=path,
        safety_root=path.parent,
    )
    return {
        "sources": len(state["sources"]),
        "chunks": len(state["chunks"]),
        "operations": len(state["operations"]),
    }


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    result: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        details = path.lstat()
        result.append(
            (
                str(path.relative_to(root)),
                details.st_dev,
                details.st_ino,
                details.st_mode,
                details.st_nlink,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return tuple(result)


def _state_frame(state: dict) -> bytes:
    payload = json.dumps(
        state, sort_keys=True, separators=(",", ":")
    ).encode()
    return (
        struct.pack(">I", len(payload))
        + payload
        + hashlib.sha256(payload).digest()
    )


def _resolved_case(tmp_path: Path):
    clock = MutableClock()
    store = make_store(tmp_path, clock=clock)
    register_echo(store)
    intent = create_golden_intent(store, make_run(store))
    resolved = store.resolve_source_intent(
        intent_id=intent["id"],
        choice="keep_both",
        expected_revision=0,
        actor_id="local",
        idempotency_key="resolve-keep-both-01",
    ).value
    action = store.list_pending_source_imports()[0]
    return clock, store, resolved, action


def test_keep_both_reuses_echo_and_imports_second_source_exactly_once_after_loss(
    tmp_path: Path,
) -> None:
    clock, store, resolved, action = _resolved_case(tmp_path)
    assert [binding["disposition"] for binding in resolved["bindings"]] == [
        "reused"
    ]
    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    dispatcher = SourceImportDispatcher(store=store, adapter=adapter)

    lost = dispatcher.deliver(
        action_id=action["id"],
        worker_id="import-worker",
        claim_key="claim-import-000001",
        completion_key="complete-import-001",
        simulate_lost_response=True,
    )
    assert lost.status == "response_lost"
    assert store.get_source_import_action(action["id"])["state"] == "pending"

    clock.advance(minutes=6)
    completed = dispatcher.deliver(
        action_id=action["id"],
        worker_id="import-worker-restart",
        claim_key="claim-import-000002",
        completion_key="complete-import-002",
    )
    replay = dispatcher.deliver(
        action_id=action["id"],
        worker_id="import-worker-restart",
        claim_key="claim-import-000003",
        completion_key="complete-import-003",
    )

    assert completed.status == "completed"
    assert completed.adapter_replayed is True
    assert replay.status == "already_completed"
    imported = store.get_source(completed.source_id)
    assert imported["canonical_id"] == "arxiv:2607.07675"
    assert imported["engine_ref"] == completed.engine_ref
    assert _state_counts(tmp_path / "research.db") == {
        "sources": 1,
        "chunks": 1,
        "operations": 1,
    }
    assert len(
        [
            entry
            for entry in (tmp_path / "imports").iterdir()
            if not entry.name.startswith(".")
        ]
    ) == 1
    assert len(store.list_run_source_bindings(resolved["run_id"])) == 2
    assert sum(
        event["type"] == "source.imported"
        for event in store.list_run_events(resolved["run_id"])
    ) == 1


def test_claim_epoch_request_hash_and_stale_completion_are_fenced(
    tmp_path: Path,
) -> None:
    clock, store, _, action = _resolved_case(tmp_path)
    first = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-a",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-source-000001",
    )
    replay = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-a",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-source-000001",
    )
    assert replay.replayed is True
    assert replay.value == first.value
    assert first.value["claim_epoch"] == 1

    with pytest.raises(IdempotencyConflict):
        store.complete_source_import(
            action_id=action["id"],
            claim_owner="worker-a",
            claim_epoch=1,
            request_hash="0" * 64,
            engine_ref="paper:bad",
            result_manifest={"source_rows": 1, "chunks": 1, "directories": 1},
            actor_id="dispatcher",
            idempotency_key="complete-source-001",
        )

    clock.advance(minutes=6)
    second = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-b",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-source-000002",
    )
    assert second.value["claim_epoch"] == 2
    with pytest.raises(InvalidTransition, match="claim"):
        store.complete_source_import(
            action_id=action["id"],
            claim_owner="worker-a",
            claim_epoch=1,
            request_hash=action["request_hash"],
            engine_ref="paper:stale",
            result_manifest={"source_rows": 1, "chunks": 1, "directories": 1},
            actor_id="dispatcher",
            idempotency_key="complete-source-002",
        )


@pytest.mark.parametrize(
    "fault_at,expected_replayed",
    [
        ("after_directory", False),
        ("after_manifest_temp", False),
        ("after_manifest", False),
        ("after_db_commit", True),
    ],
)
def test_import_restart_adopts_exact_partial_materialization(
    tmp_path: Path, fault_at: str, expected_replayed: bool
) -> None:
    clock, store, _, action = _resolved_case(tmp_path)
    crashing = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
        fault_at=fault_at,
    )
    dispatcher = SourceImportDispatcher(store=store, adapter=crashing)

    with pytest.raises(InjectedImportCrash, match=fault_at):
        dispatcher.deliver(
            action_id=action["id"],
            worker_id="worker-before-crash",
            claim_key=f"claim-{fault_at}-00001",
            completion_key=f"complete-{fault_at}-01",
        )

    clock.advance(minutes=6)
    restarted = SourceImportDispatcher(
        store=store,
        adapter=TemporaryResearchImportAdapter(
            database=tmp_path / "research.db",
            root=tmp_path / "imports",
            safety_root=tmp_path,
        ),
    )
    completed = restarted.deliver(
        action_id=action["id"],
        worker_id="worker-after-restart",
        claim_key=f"reclaim-{fault_at}-001",
        completion_key=f"recomplete-{fault_at}-01",
    )

    assert completed.status == "completed"
    assert completed.adapter_replayed is expected_replayed
    assert _state_counts(tmp_path / "research.db") == {
        "sources": 1,
        "chunks": 1,
        "operations": 1,
    }
    assert len(
        [
            entry
            for entry in (tmp_path / "imports").iterdir()
            if not entry.name.startswith(".")
        ]
    ) == 1
    current = store.get_source_import_action(action["id"])
    assert sum(
        event["type"] == "source.imported"
        for event in store.list_run_events(current["run_id"])
    ) == 1


class _MutatingAdapter:
    def __init__(self, field: str) -> None:
        self.field = field

    def execute(self, request: ImportRequest) -> ImportResult:
        result = ImportResult(
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            engine_ref="paper:result",
            manifest={"source_rows": 1, "chunks": 1, "directories": 1},
        )
        if self.field == "manifest":
            return replace(result, manifest={"source_rows": 1, "raw_path": 1})
        return replace(
            result,
            **{
                self.field: (
                    "source-import:different"
                    if self.field == "operation_id"
                    else "0" * 64
                )
            },
        )


@pytest.mark.parametrize("field", ["operation_id", "request_hash", "manifest"])
def test_dispatcher_rejects_adapter_identity_or_manifest_drift(
    tmp_path: Path, field: str
) -> None:
    case = tmp_path / field
    case.mkdir(mode=0o700)
    _, store, _, action = _resolved_case(case)
    dispatcher = SourceImportDispatcher(store=store, adapter=_MutatingAdapter(field))

    with pytest.raises(ValueError, match="adapter import result"):
        dispatcher.deliver(
            action_id=action["id"],
            worker_id="worker",
            claim_key=f"claim-drift-{field}-01",
            completion_key=f"complete-drift-{field}-01",
        )

    assert store.get_source_import_action(action["id"])["state"] == "pending"


def test_known_import_failure_is_fenced_sanitized_and_retryable(
    tmp_path: Path,
) -> None:
    _, store, _, action = _resolved_case(tmp_path)
    claim = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-a",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-before-failure-1",
    ).value
    failed = store.fail_source_import(
        action_id=action["id"],
        claim_owner="worker-a",
        claim_epoch=claim["claim_epoch"],
        category="adapter_unavailable",
        actor_id="dispatcher",
        idempotency_key="fail-source-import-01",
    ).value
    assert failed["state"] == "failed"
    assert failed["failure_category"] == "adapter_unavailable"
    assert store.get_source(failed["source_id"])["import_state"] == "failed"
    assert store.list_pending_source_imports() == []

    retried = store.retry_source_import(
        action_id=action["id"],
        actor_id="local",
        idempotency_key="retry-source-import-1",
    ).value
    assert retried["state"] == "pending"
    assert retried["operation_id"] == action["operation_id"]
    assert retried["request_hash"] == action["request_hash"]
    second_claim = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-b",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-after-failure-01",
    ).value
    assert second_claim["claim_epoch"] == claim["claim_epoch"] + 1

    with pytest.raises(ValueError, match="category"):
        store.fail_source_import(
            action_id=action["id"],
            claim_owner="worker-b",
            claim_epoch=second_claim["claim_epoch"],
            category="/private/secret-token",
            actor_id="dispatcher",
            idempotency_key="fail-source-private-1",
        )


def test_run_cancel_fences_claimed_import_and_stale_completion(tmp_path: Path) -> None:
    _, store, _, action = _resolved_case(tmp_path)
    claim = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-before-cancel",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-before-run-cancel-1",
    ).value
    run = store.get_run(action["run_id"])
    store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key="cancel-claimed-import-1",
    )

    assert store.get_source_import_action(action["id"])["state"] == "canceled"
    assert store.list_pending_source_imports() == []
    with pytest.raises(InvalidTransition, match="claim"):
        store.complete_source_import(
            action_id=action["id"],
            claim_owner="worker-before-cancel",
            claim_epoch=claim["claim_epoch"],
            request_hash=action["request_hash"],
            engine_ref="paper:stale-completion",
            result_manifest={"source_rows": 1, "chunks": 1, "directories": 1},
            actor_id="dispatcher",
            idempotency_key="complete-after-run-cancel-1",
        )


def test_source_decision_cancel_fences_prior_pending_import(tmp_path: Path) -> None:
    _, store, resolved, action = _resolved_case(tmp_path)
    run = store.get_run(resolved["run_id"])
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["active_attempt_id"],
        title="A second pending paper",
        locator=None,
        candidates=(
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2608.12345",
                "official_title": "A second pending paper",
            },
        ),
        actor_id="local",
        idempotency_key="second-pending-intent-001",
    ).value

    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="cancel",
        expected_revision=0,
        actor_id="local",
        idempotency_key="cancel-second-intent-001",
    )

    assert store.get_source_import_action(action["id"])["state"] == "canceled"
    assert store.list_source_import_waiters(action["id"])[0]["state"] == "canceled"
    assert store.list_pending_source_imports() == []


def test_failed_source_is_not_reused_and_keeps_one_action(tmp_path: Path) -> None:
    _, store, _, action = _resolved_case(tmp_path)
    claim = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-failed",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-shared-failure-01",
    ).value
    store.fail_source_import(
        action_id=action["id"],
        claim_owner="worker-failed",
        claim_epoch=claim["claim_epoch"],
        category="adapter_unavailable",
        actor_id="dispatcher",
        idempotency_key="fail-shared-action-001",
    )
    run = make_named_run(store, "failed-waiter")
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title="LingBot",
        locator=None,
        candidates=(
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2607.07675",
                "official_title": "LingBot",
            },
        ),
        actor_id="local",
        idempotency_key="failed-source-intent-1",
    ).value
    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="use_source",
        expected_revision=0,
        actor_id="local",
        idempotency_key="failed-source-resolve-1",
    )

    actions = store.list_all_source_imports()
    assert len(actions) == 1
    assert actions[0]["id"] == action["id"]
    assert actions[0]["state"] == "failed"
    assert any(
        waiter["run_id"] == run["id"] and waiter["state"] == "waiting"
        for waiter in store.list_source_import_waiters(action["id"])
    )
    assert not any(
        event["type"] == "source.reused"
        for event in store.list_run_events(run["id"])
    )


@pytest.mark.parametrize(
    "engine_ref",
    ["/private/paper", "paper ref", "paper:../secret", "paper:\u202esecret", "x" * 501],
)
def test_engine_reference_must_use_stable_safe_syntax(
    tmp_path: Path, engine_ref: str
) -> None:
    _, store, _, action = _resolved_case(tmp_path)
    claim = store.claim_source_import(
        action_id=action["id"],
        worker_id="worker-engine-ref",
        lease_seconds=300,
        actor_id="dispatcher",
        idempotency_key="claim-invalid-engine-01",
    ).value
    with pytest.raises(ValueError, match="engine_ref"):
        store.complete_source_import(
            action_id=action["id"],
            claim_owner="worker-engine-ref",
            claim_epoch=claim["claim_epoch"],
            request_hash=action["request_hash"],
            engine_ref=engine_ref,
            result_manifest={"source_rows": 1, "chunks": 1, "directories": 1},
            actor_id="dispatcher",
            idempotency_key="complete-invalid-engine-1",
        )


def _adapter_request(*, operation_id: str = "source-import:operation-a") -> ImportRequest:
    return ImportRequest(
        operation_id=operation_id,
        source_id="source-test",
        canonical_id="arxiv:2607.07675",
        request_hash=("a" if operation_id.endswith("a") else "b") * 64,
    )


def _materialization_bytes(request: ImportRequest) -> bytes:
    slug = hashlib.sha256(request.canonical_id.encode()).hexdigest()[:20]
    return json.dumps(
        {
            "canonical_id": request.canonical_id,
            "engine_ref": f"paper:{slug}",
            "operation_id": request.operation_id,
            "request_hash": request.request_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_operation_claim_precedes_directory_and_blocks_different_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=root,
        safety_root=tmp_path,
        fault_at="after_directory",
    )
    with pytest.raises(InjectedImportCrash):
        adapter.execute(_adapter_request())

    names = {entry.name for entry in root.iterdir()}
    assert any(name.startswith(".claim-") for name in names)
    assert any(not name.startswith(".") for name in names)
    with pytest.raises(ValueError, match="operation claim"):
        TemporaryResearchImportAdapter(
            database=tmp_path / "research.db",
            root=root,
            safety_root=tmp_path,
        ).execute(_adapter_request(operation_id="source-import:operation-b"))


def test_unknown_target_file_and_changed_temp_fail_closed_without_unlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    after_directory = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=root,
        safety_root=tmp_path,
        fault_at="after_directory",
    )
    with pytest.raises(InjectedImportCrash):
        after_directory.execute(_adapter_request())
    target = next(entry for entry in root.iterdir() if not entry.name.startswith("."))
    unknown = target / "unknown.bin"
    unknown.write_bytes(b"unknown")
    with pytest.raises(ValueError, match="unexpected"):
        TemporaryResearchImportAdapter(
            database=tmp_path / "research.db",
            root=root,
            safety_root=tmp_path,
        ).execute(_adapter_request())
    assert unknown.read_bytes() == b"unknown"

    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    second_root = second / "imports"
    after_temp = TemporaryResearchImportAdapter(
        database=second / "research.db",
        root=second_root,
        safety_root=second,
        fault_at="after_manifest_temp",
    )
    with pytest.raises(InjectedImportCrash):
        after_temp.execute(_adapter_request())
    second_target = next(
        entry for entry in second_root.iterdir() if not entry.name.startswith(".")
    )
    temporary = next(second_target.glob(".manifest-*.tmp"))
    temporary.write_bytes(b"changed")
    with pytest.raises(ValueError, match="temporary"):
        TemporaryResearchImportAdapter(
            database=second / "research.db",
            root=second_root,
            safety_root=second,
        ).execute(_adapter_request())
    assert temporary.read_bytes() == b"changed"


def test_database_parent_swap_cannot_redirect_capability_bound_state(
    tmp_path: Path,
) -> None:
    safety = tmp_path / "safety"
    safety.mkdir(mode=0o700)
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    parked = tmp_path / "parked"

    def swap_parent() -> None:
        os.rename(safety, parked)
        os.rename(attacker, safety)

    adapter = TemporaryResearchImportAdapter(
        database=safety / "research.db",
        root=safety / "imports",
        safety_root=safety,
        after_capability_open=swap_parent,
    )
    result = adapter.execute(_adapter_request())
    assert result.replayed is False
    assert not (safety / "research.db").exists()
    assert _state_counts(parked / "research.db") == {
        "sources": 1,
        "chunks": 1,
        "operations": 1,
    }


def test_manifest_publication_race_never_replaces_competing_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    competing = b'{"operation_id":"different"}'
    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=root,
        safety_root=tmp_path,
        manifest_race_payload=competing,
    )

    with pytest.raises(ValueError, match="publication race"):
        adapter.execute(_adapter_request())

    target = next(entry for entry in root.iterdir() if not entry.name.startswith("."))
    assert (target / "manifest.json").read_bytes() == competing


def test_exact_manifest_publication_race_requires_the_retained_anchor_inode(
    tmp_path: Path,
) -> None:
    request = _adapter_request()
    competing = _materialization_bytes(request)
    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
        manifest_race_payload=competing,
    )

    with pytest.raises(ValueError, match="publication race"):
        adapter.execute(request)

    target = next(
        entry
        for entry in (tmp_path / "imports").iterdir()
        if not entry.name.startswith(".")
    )
    anchor = next(target.glob(".manifest-*.tmp"))
    manifest = target / "manifest.json"
    assert manifest.read_bytes() == competing
    assert anchor.read_bytes() == competing
    assert anchor.stat().st_ino != manifest.stat().st_ino
    assert anchor.stat().st_nlink == 1
    assert manifest.stat().st_nlink == 1


def test_completed_manifest_retains_exact_anchor_without_path_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_unlink(*args: object, **kwargs: object) -> None:
        raise AssertionError("completed materialization must not unlink by name")

    monkeypatch.setattr("cortex_platform.tests.support.sources.os.unlink", reject_unlink)
    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    first = adapter.execute(_adapter_request())
    second = adapter.execute(_adapter_request())

    target = next(
        entry
        for entry in (tmp_path / "imports").iterdir()
        if not entry.name.startswith(".")
    )
    anchor = next(target.glob(".manifest-*.tmp"))
    manifest = target / "manifest.json"
    assert first.replayed is False
    assert second.replayed is True
    assert anchor.read_bytes() == manifest.read_bytes()
    assert anchor.stat().st_ino == manifest.stat().st_ino
    assert anchor.stat().st_nlink == 2


def test_replay_rejects_identical_rebuilt_manifest_anchor(tmp_path: Path) -> None:
    request = _adapter_request()
    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    adapter.execute(request)
    target = next(
        entry
        for entry in (tmp_path / "imports").iterdir()
        if not entry.name.startswith(".")
    )
    anchor = next(target.glob(".manifest-*.tmp"))
    manifest = target / "manifest.json"
    encoded = manifest.read_bytes()
    anchor.unlink()
    anchor.write_bytes(encoded)
    anchor.chmod(0o600)
    assert anchor.stat().st_ino != manifest.stat().st_ino

    with pytest.raises(ValueError, match="anchor|inode|link"):
        adapter.execute(request)


def test_manifest_pair_replacement_after_validation_fails_before_state_commit(
    tmp_path: Path,
) -> None:
    request = _adapter_request()

    def replace_linked_pair() -> None:
        target = next(
            entry
            for entry in (tmp_path / "imports").iterdir()
            if not entry.name.startswith(".")
        )
        anchor = next(target.glob(".manifest-*.tmp"))
        manifest = target / "manifest.json"
        encoded = manifest.read_bytes()
        anchor.unlink()
        manifest.unlink()
        anchor.write_bytes(encoded)
        anchor.chmod(0o600)
        os.link(anchor, manifest)

    adapter = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
        after_manifest_validation=replace_linked_pair,
    )

    with pytest.raises(ValueError, match="anchor|inode|link"):
        adapter.execute(request)

    assert (tmp_path / "research.db").read_bytes() == b""
    assert _state_counts(tmp_path / "research.db") == {
        "sources": 0,
        "chunks": 0,
        "operations": 0,
    }


@pytest.mark.parametrize("same_content", [True, False], ids=("same", "wrong"))
def test_append_entry_replacement_is_rejected_before_first_frame_byte(
    tmp_path: Path, same_content: bool
) -> None:
    class ReplacingBeforeAppendAdapter(TemporaryResearchImportAdapter):
        captured_anchor: _OpenManifestAnchor | None = None

        def _append_operation_state(
            self,
            database_fd: int,
            prepared: _PreparedStateFrame,
            manifest_anchor: _OpenManifestAnchor,
        ) -> None:
            self.captured_anchor = manifest_anchor
            for name in (
                manifest_anchor.manifest_name,
                manifest_anchor.anchor_name,
            ):
                os.unlink(name, dir_fd=manifest_anchor.target_fd)
            replacement = (
                manifest_anchor.encoded if same_content else b"wrong-content"
            )
            replacement_fd = os.open(
                manifest_anchor.anchor_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=manifest_anchor.target_fd,
            )
            try:
                os.write(replacement_fd, replacement)
                os.fsync(replacement_fd)
            finally:
                os.close(replacement_fd)
            os.link(
                manifest_anchor.anchor_name,
                manifest_anchor.manifest_name,
                src_dir_fd=manifest_anchor.target_fd,
                dst_dir_fd=manifest_anchor.target_fd,
                follow_symlinks=False,
            )
            os.fsync(manifest_anchor.target_fd)
            super()._append_operation_state(
                database_fd, prepared, manifest_anchor
            )

    database = tmp_path / "research.db"
    adapter = ReplacingBeforeAppendAdapter(
        database=database,
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )

    with pytest.raises(ValueError, match="append.*anchor"):
        adapter.execute(_adapter_request())

    assert adapter.captured_anchor is not None
    for descriptor in (
        adapter.captured_anchor.target_fd,
        adapter.captured_anchor.manifest_fd,
        adapter.captured_anchor.anchor_fd,
    ):
        with pytest.raises(OSError):
            os.fstat(descriptor)
    target = next(
        entry
        for entry in (tmp_path / "imports").iterdir()
        if not entry.name.startswith(".")
    )
    anchor = next(target.glob(".manifest-*.tmp"))
    manifest = target / "manifest.json"
    replacement = (
        adapter.captured_anchor.encoded if same_content else b"wrong-content"
    )
    assert anchor.read_bytes() == replacement
    assert manifest.read_bytes() == replacement
    assert anchor.stat().st_ino == manifest.stat().st_ino
    assert anchor.stat().st_nlink == 2
    assert database.read_bytes() == b""
    assert _state_counts(database) == {
        "sources": 0,
        "chunks": 0,
        "operations": 0,
    }


def test_success_closes_all_manifest_capability_descriptors(tmp_path: Path) -> None:
    class CapturingAdapter(TemporaryResearchImportAdapter):
        captured_anchor: _OpenManifestAnchor | None = None

        def _append_operation_state(
            self,
            database_fd: int,
            prepared: _PreparedStateFrame,
            manifest_anchor: _OpenManifestAnchor,
        ) -> None:
            self.captured_anchor = manifest_anchor
            super()._append_operation_state(
                database_fd, prepared, manifest_anchor
            )

    adapter = CapturingAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    adapter.execute(_adapter_request())

    assert adapter.captured_anchor is not None
    for descriptor in (
        adapter.captured_anchor.target_fd,
        adapter.captured_anchor.manifest_fd,
        adapter.captured_anchor.anchor_fd,
    ):
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_two_adapter_instances_serialize_without_lost_operation_update(
    tmp_path: Path,
) -> None:
    barrier = Barrier(2)

    def execute(index: int) -> ImportResult:
        adapter = TemporaryResearchImportAdapter(
            database=tmp_path / "research.db",
            root=tmp_path / "imports",
            safety_root=tmp_path,
            after_capability_open=lambda: barrier.wait(timeout=5),
        )
        return adapter.execute(
            ImportRequest(
                operation_id=f"source-import:parallel-{index}",
                source_id=f"source-{index}",
                canonical_id=f"arxiv:260{index}.07675",
                request_hash=str(index) * 64,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, (7, 8)))

    assert all(result.replayed is False for result in results)
    assert _state_counts(tmp_path / "research.db") == {
        "sources": 2,
        "chunks": 2,
        "operations": 2,
    }


@pytest.mark.parametrize(
    "tail",
    [
        b"\x00\x00",
        struct.pack(">I", 100) + b"{}",
        struct.pack(">I", 2) + b"{}" + hashlib.sha256(b"{}").digest()[:8],
    ],
    ids=("partial-header", "partial-body", "partial-checksum"),
)
def test_partial_log_tail_is_truncated_before_safe_append(
    tmp_path: Path, tail: bytes
) -> None:
    first = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    first.execute(_adapter_request())
    with (tmp_path / "research.db").open("ab") as stream:
        stream.write(tail)
    before = (tmp_path / "research.db").stat().st_size

    second = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    second.execute(
        ImportRequest(
            operation_id="source-import:after-partial",
            source_id="source-after-partial",
            canonical_id="arxiv:2608.07675",
            request_hash="c" * 64,
        )
    )

    assert (tmp_path / "research.db").stat().st_size > before - len(tail)
    assert _state_counts(tmp_path / "research.db")["operations"] == 2


@pytest.mark.parametrize(
    "fault_at",
    [
        "after_state_frame_header",
        "after_state_frame_body",
        "after_state_checksum_partial",
    ],
)
def test_interrupted_state_append_recovers_without_false_commit(
    tmp_path: Path, fault_at: str
) -> None:
    crashing = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
        fault_at=fault_at,
    )
    with pytest.raises(InjectedImportCrash, match=fault_at):
        crashing.execute(_adapter_request())

    recovered = TemporaryResearchImportAdapter(
        database=tmp_path / "research.db",
        root=tmp_path / "imports",
        safety_root=tmp_path,
    ).execute(_adapter_request())
    assert recovered.replayed is False
    assert _state_counts(tmp_path / "research.db")["operations"] == 1


@pytest.mark.parametrize(
    "state",
    [
        {"version": True, "operations": {}, "sources": {}, "chunks": {}},
        {"version": 1, "operations": {}, "sources": {}, "chunks": {}, "extra": 1},
        {
            "version": 1,
            "operations": {
                "source-import:bad": {
                    "canonical_id": "arxiv:2607.07675",
                    "request_hash": "a" * 64,
                    "response": {
                        "engine_ref": "paper:bad",
                        "manifest": {
                            "source_rows": True,
                            "chunks": 1,
                            "directories": 1,
                        },
                    },
                }
            },
            "sources": {"arxiv:2607.07675": "paper:bad"},
            "chunks": {"arxiv:2607.07675": "chunk"},
        },
        {
            "version": 1,
            "operations": {},
            "sources": {"file:///private/paper": "paper:bad"},
            "chunks": {"file:///private/paper": "chunk"},
        },
        {
            "version": 1,
            "operations": {},
            "sources": {"doi:10.48550/ARXIV.2607.07675": "paper:bad"},
            "chunks": {"doi:10.48550/ARXIV.2607.07675": "chunk"},
        },
        {
            "version": 1,
            "operations": {},
            "sources": {"arxiv:2607.07675": "paper:bad"},
            "chunks": {},
        },
    ],
    ids=(
        "bool-version",
        "extra",
        "bool-manifest",
        "bad-canonical",
        "non-normalized-canonical",
        "inconsistent",
    ),
)
def test_complete_malformed_nested_state_fails_closed(
    tmp_path: Path, state: dict
) -> None:
    path = tmp_path / "research.db"
    payload = TemporaryResearchImportAdapter._LOG_MAGIC + _state_frame(state)
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(ValueError, match="state"):
        TemporaryResearchImportAdapter(
            database=path,
            root=tmp_path / "imports",
            safety_root=tmp_path,
        ).execute(_adapter_request())
    assert path.read_bytes() == payload


def test_complete_bad_checksum_fails_closed_and_oversize_is_unchanged(
    tmp_path: Path,
) -> None:
    state = {"version": 1, "operations": {}, "sources": {}, "chunks": {}}
    bad = bytearray(TemporaryResearchImportAdapter._LOG_MAGIC + _state_frame(state))
    bad[-1] ^= 1
    path = tmp_path / "research.db"
    path.write_bytes(bad)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="checksum"):
        TemporaryResearchImportAdapter(
            database=path,
            root=tmp_path / "imports",
            safety_root=tmp_path,
        ).execute(_adapter_request())
    assert path.read_bytes() == bad

    oversized = b"x" * (TemporaryResearchImportAdapter._MAX_LOG_BYTES + 1)
    path.write_bytes(oversized)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="large"):
        TemporaryResearchImportAdapter(
            database=path,
            root=tmp_path / "imports",
            safety_root=tmp_path,
        ).execute(_adapter_request())
    assert path.read_bytes() == oversized


def test_operation_log_limits_are_checked_before_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "research.db"
    adapter = TemporaryResearchImportAdapter(
        database=path,
        root=tmp_path / "imports",
        safety_root=tmp_path,
    )
    adapter.execute(_adapter_request())
    before = path.read_bytes()
    before_root = _tree_snapshot(tmp_path / "imports")
    monkeypatch.setattr(
        TemporaryResearchImportAdapter,
        "_MAX_LOG_BYTES",
        len(before) + 1,
    )

    with pytest.raises(ValueError, match="log is too large"):
        adapter.execute(
            ImportRequest(
                operation_id="source-import:over-limit",
                source_id="source-over-limit",
                canonical_id="arxiv:2608.07675",
                request_hash="d" * 64,
            )
        )
    assert path.read_bytes() == before
    assert _tree_snapshot(tmp_path / "imports") == before_root

    monkeypatch.setattr(
        TemporaryResearchImportAdapter,
        "_MAX_LOG_BYTES",
        1_048_576,
    )
    empty_path = tmp_path / "empty-research.db"
    empty_path.touch(mode=0o600)
    monkeypatch.setattr(TemporaryResearchImportAdapter, "_MAX_FRAME_BYTES", 1)
    empty_adapter = TemporaryResearchImportAdapter(
        database=empty_path,
        root=tmp_path / "empty-imports",
        safety_root=tmp_path,
    )
    with pytest.raises(ValueError, match="frame is too large"):
        empty_adapter.execute(_adapter_request())
    assert empty_path.read_bytes() == b""
    assert not (tmp_path / "empty-imports").exists()


@pytest.mark.parametrize(
    ("limit_name", "message"),
    [
        ("_MAX_FRAME_BYTES", "frame is too large"),
        ("_MAX_LOG_BYTES", "log is too large"),
    ],
    ids=("frame", "total-log"),
)
def test_operation_log_preflight_limit_creates_no_database_or_import_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    message: str,
) -> None:
    database = tmp_path / "nested" / "research.db"
    root = tmp_path / "nested" / "imports"
    monkeypatch.setattr(TemporaryResearchImportAdapter, limit_name, 1)

    with pytest.raises(ValueError, match=message):
        TemporaryResearchImportAdapter(
            database=database,
            root=root,
            safety_root=tmp_path,
        ).execute(_adapter_request())

    assert not database.exists()
    assert not root.exists()
    assert not database.parent.exists()


def test_locked_repreflight_limit_leaves_no_outer_operation_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "nested" / "research.db"
    outer_root = tmp_path / "nested" / "outer-imports"
    inner_root = tmp_path / "nested" / "inner-imports"
    after_inner: tuple[tuple[object, ...], ...] | None = None

    def write_competing_first_frame() -> None:
        nonlocal after_inner
        assert not database.parent.exists()
        TemporaryResearchImportAdapter(
            database=database,
            root=inner_root,
            safety_root=tmp_path,
        ).execute(_adapter_request())
        after_inner = _tree_snapshot(tmp_path)

    monkeypatch.setattr(TemporaryResearchImportAdapter, "_MAX_LOG_BYTES", 470)
    outer = TemporaryResearchImportAdapter(
        database=database,
        root=outer_root,
        safety_root=tmp_path,
        after_capability_open=write_competing_first_frame,
    )

    with pytest.raises(ValueError, match="log is too large"):
        outer.execute(_adapter_request(operation_id="source-import:operation-b"))

    assert after_inner is not None
    assert _tree_snapshot(tmp_path) == after_inner
    assert not outer_root.exists()
    assert _state_counts(database) == {
        "sources": 1,
        "chunks": 1,
        "operations": 1,
    }


@pytest.mark.parametrize("target", ["safety_root", "root", "database"])
def test_temporary_adapter_rejects_symlinked_path_components(
    tmp_path: Path, target: str
) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    safety = tmp_path / "safety"
    safety.mkdir(mode=0o700)
    database = safety / "research.db"
    root = safety / "imports"
    chosen_safety = safety
    if target == "safety_root":
        link = tmp_path / "safety-link"
        link.symlink_to(safety, target_is_directory=True)
        chosen_safety = link
        database = link / "research.db"
        root = link / "imports"
    elif target == "root":
        root.symlink_to(real, target_is_directory=True)
    else:
        real_database = real / "research.db"
        real_database.touch(mode=0o600)
        database.symlink_to(real_database)

    with pytest.raises((PermissionError, ValueError), match="safe|symlink|private"):
        TemporaryResearchImportAdapter(
            database=database,
            root=root,
            safety_root=chosen_safety,
        ).execute(_adapter_request())
