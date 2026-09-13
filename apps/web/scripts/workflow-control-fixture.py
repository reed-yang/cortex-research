#!/usr/bin/env python3
"""Deterministic, temporary-only fixture driver for the Web workflow acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _install_network_guard() -> None:
    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("workflow fixture networking is disabled")

    class GuardedSocket(socket.socket):
        def __init__(
            self,
            family: int = socket.AF_INET,
            type: int = socket.SOCK_STREAM,
            proto: int = 0,
            fileno: int | None = None,
        ) -> None:
            if family in {socket.AF_INET, socket.AF_INET6}:
                deny_network()
            super().__init__(family, type, proto, fileno)

        def connect(self, _address: object) -> None:
            deny_network()

        def connect_ex(self, _address: object) -> int:
            deny_network()
            return 1

        def bind(self, _address: object) -> None:
            deny_network()

        def listen(self, _backlog: int = 0) -> None:
            deny_network()

        def accept(self) -> tuple[socket.socket, object]:
            deny_network()
            raise AssertionError("unreachable")

        def sendto(self, *_args: object, **_kwargs: object) -> int:
            deny_network()
            return 0

        def sendmsg(self, *_args: object, **_kwargs: object) -> int:
            deny_network()
            return 0

    socket.socket = GuardedSocket
    socket.create_connection = deny_network  # type: ignore[assignment]
    socket.create_server = deny_network  # type: ignore[assignment]
    socket.getaddrinfo = deny_network  # type: ignore[assignment]


_install_network_guard()


def _expect_network_denied(operation: Any) -> None:
    try:
        result = operation()
    except PermissionError:
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()
    raise AssertionError("workflow fixture network operation was not denied")


def _assert_network_guard() -> int:
    probes = 0
    for family in (socket.AF_INET, socket.AF_INET6):
        _expect_network_denied(lambda family=family: socket.socket(family, socket.SOCK_DGRAM))
        probes += 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as guarded:
        socket.socket.setblocking(guarded, False)
        for operation in (
            lambda: guarded.connect(""),
            lambda: guarded.connect_ex(""),
            lambda: guarded.bind(""),
            lambda: guarded.listen(),
            lambda: guarded.accept(),
            lambda: guarded.sendto(b"probe", ""),
            lambda: guarded.sendmsg([b"probe"], [], 0, ""),
        ):
            _expect_network_denied(operation)
            probes += 1
    for operation in (
        lambda: socket.create_connection(("127.0.0.1", 0), timeout=0),
        lambda: socket.create_server(("127.0.0.1", 0)),
        lambda: socket.getaddrinfo("127.0.0.1", 0),
    ):
        _expect_network_denied(operation)
        probes += 1
    return probes

from cortex_platform.product.artifacts import (
    ArtifactMaterializationService,
    AssetRoot,
    FilesystemMaterializer,
)
from cortex_platform.product.artifacts.reader import ArtifactReader
from cortex_platform.product.api.research import ResearchWorkflowProjector
from cortex_platform.product.control import ControlStore
from cortex_platform.product.sources import CandidateObservation, SourceImportDispatcher
from cortex_platform.tests.support.sources import TemporaryResearchImportAdapter
from cortex_platform.product.workflows import (
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectReconciliationRequest,
    EffectReconciliationResult,
    EngineReference,
    GOLDEN_RESEARCH_WORKFLOW,
    GoldenArtifactPlans,
    GoldenWorkflowCase,
    GoldenWorkflowFacade,
    LineageLink,
    LineageNode,
    LineageQueryRequest,
    LineageQueryResult,
    RuntimeStageRequest,
    RuntimeStageResult,
    SourceBindingRequest,
    SourceBindingResult,
    SourceImportRequest,
    SourceImportResult,
    SuccessorCreationRequest,
    SuccessorCreationResult,
    stable_attempt_operation_id,
    stable_operation_id,
)
from cortex_platform.tests.support.workflows import (
    DeterministicResearchEngine,
    DeterministicRuntime,
)

ECHO_TITLE = "Echo-Infinity: Learnable Evolving Memory for Real-Time Infinite Video Generation"
LINGBOT_TITLE = "Scaling Mixture-of-Experts Video Pretraining for Embodied Intelligence"
LINGBOT_URL = "https://arxiv.org/pdf/2607.07675"
ASSET_ROOT_ID = "workflow-assets"
FIXTURE_MARKER = ".cortex-workflow-fixture-v1"

LIVING_V1 = """# Living Brief v1

Echo-Infinity supplies the evolving-memory baseline for the Helios-14B study [Echo-Infinity, 2026].

- Preserve causal temporal updates.
- Measure memory drift before scaling context.
"""
LIVING_V2 = """# Living Brief v2

The successor combines online TTT updates with a bounded Helios-14B memory controller while retaining the v1 baseline [Echo-Infinity, 2026].

- Gate writes by novelty and reconstruction error.
- Compare recurrent memory against a frozen Wan2.2 cache.

wrap_probe_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789
"""
EVIDENCE = """# Evidence Matrix

| Claim | Evidence | Risk |
| --- | --- | --- |
| Evolving memory supports long video | Echo-Infinity [2026] | Distribution shift |
| TTT can adapt state online | TTT literature [2025] | Update instability |

The matrix separates observed evidence from the proposed Helios mechanism.
"""
TRAINING = """# Training Plan

1. Freeze Wan2.2 and train only the memory projector.
2. Add truncated online TTT updates with a replay guard.
3. Unfreeze the final temporal blocks after stability ablations.

Primary metrics: temporal consistency, memory retrieval precision, and update latency [Helios protocol, 2026].
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_root(value: str) -> Path:
    root = Path(value).resolve(strict=True)
    expected_root = os.environ.get("CORTEX_WORKFLOW_FIXTURE_ROOT")
    capability = os.environ.get("CORTEX_WORKFLOW_FIXTURE_CAPABILITY")
    if expected_root is None or Path(expected_root).resolve(strict=True) != root:
        raise ValueError("root is not bound to the verifier")
    if capability is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", capability) is None:
        raise ValueError("fixture capability is invalid")
    temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        not root.is_dir()
        or root.parent != temporary_parent
        or re.fullmatch(r"cortex-control-workflow-[A-Za-z0-9_-]{6,}", root.name) is None
    ):
        raise ValueError("root must be a verifier-created temporary directory")
    observed = root.stat()
    if observed.st_uid != os.geteuid() or observed.st_mode & 0o077:
        raise ValueError("root must be owner-private")
    marker = root / FIXTURE_MARKER
    marker_stat = marker.lstat()
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or marker_stat.st_uid != os.geteuid()
        or stat.S_IMODE(marker_stat.st_mode) != 0o600
        or marker_stat.st_nlink != 1
        or marker.read_text(encoding="ascii") != capability
    ):
        raise ValueError("fixture capability marker is invalid")
    return root


def _child(root: Path, *parts: str) -> Path:
    value = root.joinpath(*parts).resolve(strict=False)
    if value == root or root not in value.parents:
        raise ValueError("fixture path escaped the safety root")
    return value


def _database(root: Path) -> Path:
    return _child(root, "data", "control.db")


def _state_path(root: Path) -> Path:
    return _child(root, "fixture-state.json")


def _write_state(root: Path, value: dict[str, str]) -> None:
    target = _state_path(root)
    target.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    target.chmod(0o600)


def _read_state(root: Path) -> dict[str, str]:
    value = json.loads(_state_path(root).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError("fixture state is invalid")
    return value


def _case(intent_id: str) -> GoldenWorkflowCase:
    return GoldenWorkflowCase(
        source_intent_id=intent_id,
        lineage_query="Echo memory successors",
        successor_title="Helios-14B TTT successor",
        artifact_plans=GoldenArtifactPlans(
            evidence_sha256=_digest(EVIDENCE),
            living_sha256=_digest(LIVING_V2),
            training_sha256=_digest(TRAINING),
            snapshot_sha256=_digest("Helios immutable snapshot"),
        ),
    )


def _engine_result(store: ControlStore, request: object) -> Any:
    if isinstance(request, (SourceBindingRequest, SourceImportRequest)):
        source = store.get_source(request.source_id)
        reference = EngineReference("source", source["engine_ref"])
        if isinstance(request, SourceBindingRequest):
            return SourceBindingResult(
                request.operation_id,
                request.delivery_epoch,
                request.request_hash,
                request.source_id,
                reference,
            )
        return SourceImportResult(
            request.operation_id,
            request.delivery_epoch,
            request.request_hash,
            request.source_id,
            reference,
            {"source_rows": 1, "chunks": 1, "directories": 1},
        )
    if isinstance(request, LineageQueryRequest):
        return LineageQueryResult(
            request.operation_id,
            request.delivery_epoch,
            request.request_hash,
            (
                LineageNode("lineage-dormant", "dormant", "Dormant baseline", 2, EngineReference("lineage", "idea:dormant")),
                LineageNode("lineage-graduated", "graduated", "Graduated baseline", 4, EngineReference("lineage", "idea:graduated")),
            ),
        )
    if not isinstance(request, SuccessorCreationRequest):
        raise TypeError("unsupported deterministic engine request")
    node = LineageNode(
        "lineage-successor",
        "active",
        request.title,
        1,
        EngineReference("lineage", "idea:successor"),
    )
    return SuccessorCreationResult(
        request.operation_id,
        request.delivery_epoch,
        request.request_hash,
        node,
        request.parent_node_ids,
        tuple(
            LineageLink(f"lineage-link-{index}", node.node_id, parent, "successor_reuses")
            for index, parent in enumerate(request.parent_node_ids, 1)
        ),
    )


def _engine(store: ControlStore, workflow_id: str) -> DeterministicResearchEngine:
    operation_ids = {
        stable_operation_id(workflow_id, stage, effect_key)
        for stage, effect_key in (
            ("import_sources", "reused"),
            ("import_sources", "imported"),
            ("retrieve_lineage", "lineage"),
            ("create_successor", "successor"),
        )
    }
    return DeterministicResearchEngine(
        responses={operation_id: lambda request: _engine_result(store, request) for operation_id in operation_ids}
    )


def _runtime_identity(store: ControlStore, run: dict[str, Any]) -> dict[str, str]:
    attempt = store.get_attempt(str(run["active_attempt_id"]))
    binding_id = attempt.get("runtime_binding_id")
    if not isinstance(binding_id, str):
        raise ValueError("fixture runtime is not bound")
    return {
        "attempt_id": str(attempt["id"]),
        "runtime_binding_id": binding_id,
        "runtime_release_id": "workflow-release",
        "state_generation_id": "workflow-generation",
    }


def _start_runtime(store: ControlStore, run: dict[str, Any]) -> dict[str, str]:
    identity = {
        "attempt_id": str(run["active_attempt_id"]),
        "runtime_release_id": "workflow-release",
        "state_generation_id": "workflow-generation",
    }
    run = store.reserve_attempt_dispatch(
        run_id=run["id"],
        dispatch_owner="workflow-runtime",
        runtime_slot_id="workflow-slot",
        runtime_artifact_digest="workflow-runtime-artifact",
        runtime_worker_protocol="workflow-protocol",
        expected_revision=run["revision"],
        actor_id="fixture-runtime",
        idempotency_key="workflow-runtime-reserve",
        **identity,
    ).value
    binding = store.create_runtime_binding(
        thread_id=run["thread_id"],
        adapter_id="workflow-fixture",
        runtime_session_ref="workflow-session",
        generation=1,
        adapter_version="test",
        actor_id="fixture-runtime",
        idempotency_key="workflow-runtime-binding",
    ).value
    identity["runtime_binding_id"] = binding["id"]
    run = store.pin_attempt_runtime(
        run_id=run["id"],
        dispatch_owner="workflow-runtime",
        expected_revision=run["revision"],
        actor_id="fixture-runtime",
        idempotency_key="workflow-runtime-pin",
        **identity,
    ).value
    for state in ("starting", "running"):
        run = store.apply_runtime_transition(
            run_id=run["id"],
            target_state=state,
            expected_revision=run["revision"],
            actor_id="fixture-runtime",
            idempotency_key=f"workflow-runtime-{state}",
            **identity,
        ).value
    return identity


def _runtime(store: ControlStore, run_id: str, workflow_id: str) -> DeterministicRuntime:
    run = store.get_run(run_id)
    identity = _runtime_identity(store, run)
    sequences = {"research_evidence": 0, "research_architecture": 1, "research_training_plan": 2}

    def result(request: RuntimeStageRequest) -> RuntimeStageResult:
        store.record_runtime_observation(
            run_id=run_id,
            expected_revision=store.get_run(run_id)["revision"],
            event_type="runtime.tool.completed",
            payload={
                "tool_call_id": f"fixture-{request.stage_key}",
                "tool_name": "deterministic_research",
                "is_error": False,
                "duration_ms": 1,
            },
            actor_id="fixture-runtime",
            idempotency_key=f"workflow-observation-{request.stage_key}",
            adapter_event_id=request.operation_id,
            adapter_event_sequence=sequences[request.stage_key],
            **identity,
        )
        event = next(item for item in store.list_run_events(run_id) if item["causation_id"] == request.operation_id)
        return RuntimeStageResult(
            request.operation_id,
            request.delivery_epoch,
            request.request_hash,
            request.attempt_id,
            event["id"],
            request.stage_key,
            request.effect_kind,
            f"fixture-output-{request.stage_key}",
            hashlib.sha256(request.stage_key.encode("utf-8")).hexdigest(),
        )

    return DeterministicRuntime(
        responses={
            stable_attempt_operation_id(workflow_id, stage, effect_key, identity["attempt_id"]): result
            for stage, effect_key in (
                ("research_evidence", "evidence"),
                ("research_architecture", "architecture"),
                ("research_training_plan", "training"),
            )
        }
    )


@dataclass
class _GeneratedArtifacts:
    store: ControlStore
    materializer: ArtifactMaterializationService
    run_id: str

    def __post_init__(self) -> None:
        self._committed: dict[str, ArtifactWorkflowResult] = {}

    def execute_artifact(self, request: ArtifactWorkflowRequest) -> ArtifactWorkflowResult:
        if request.effect_kind == "artifact_snapshot":
            thread = self.store.get_thread(self.store.get_run(self.run_id)["thread_id"])
            snapshot = self.store.create_artifact_snapshot(
                workspace_id=thread["workspace_id"],
                run_id=self.run_id,
                attempt_id=request.attempt_id,
                name="Helios immutable snapshot",
                artifact_version_ids=request.snapshot_member_version_ids,
                actor_id="fixture-artifacts",
                idempotency_key="workflow-snapshot",
            ).value
            value = ArtifactWorkflowResult(
                request.operation_id,
                request.delivery_epoch,
                request.request_hash,
                request.attempt_id,
                request.stage_key,
                request.effect_kind,
                request.snapshot_member_version_ids,
                snapshot["id"],
            )
        else:
            value = self._materialize_versions(request)
        request.validate_result(value)
        self._committed[request.operation_id] = value
        return value

    def reconcile_effect(self, request: EffectReconciliationRequest) -> EffectReconciliationResult:
        value = self._committed.get(request.operation_id)
        return EffectReconciliationResult(
            domain=request.domain,
            operation_id=request.operation_id,
            request_hash=request.request_hash,
            delivery_epoch=request.delivery_epoch,
            disposition="committed" if value is not None else "not_found",
            result=value,
        )

    def _materialize_versions(self, request: ArtifactWorkflowRequest) -> ArtifactWorkflowResult:
        kind = request.effect_kind.removeprefix("artifact_")
        metadata = {
            "evidence": ("evidence-matrix", "Evidence Matrix", (EVIDENCE,)),
            "living": ("living-brief", "Living Brief", (LIVING_V1, LIVING_V2)),
            "training": ("training-plan", "Training Plan", (TRAINING,)),
        }[kind]
        run = self.store.get_run(self.run_id)
        thread = self.store.get_thread(run["thread_id"])
        artifact = self.store.create_artifact(
            workspace_id=thread["workspace_id"],
            thread_id=thread["id"],
            run_id=self.run_id,
            attempt_id=request.attempt_id,
            kind=metadata[0],
            title=metadata[1],
            actor_id="fixture-artifacts",
            idempotency_key=f"workflow-artifact-{kind}",
        ).value
        bindings = self.store.list_run_source_bindings(self.run_id)
        sources = [self.store.get_source(item["source_id"]) for item in bindings]
        versions: list[dict[str, Any]] = []
        for index, text in enumerate(metadata[2], 1):
            content = text.encode("utf-8")
            parents = () if not versions else ({"artifact_version_id": versions[-1]["id"], "sha256": versions[-1]["sha256"]},)
            reservation = self.store.request_artifact_version(
                artifact_id=artifact["id"],
                logical_version=index,
                run_id=self.run_id,
                attempt_id=request.attempt_id,
                source_ids=tuple(sorted(source["id"] for source in sources)),
                research_engine_refs=tuple(sorted(source["engine_ref"] for source in sources)),
                generator={"name": "workflow-research", "version": "1"},
                tool={"name": "workflow-writer", "version": "1"},
                parents=parents,
                root_id=ASSET_ROOT_ID,
                relative_path=f"versions/{kind}-v{index}.md",
                sha256=hashlib.sha256(content).hexdigest(),
                byte_length=len(content),
                media_type="text/markdown",
                advance_head=True,
                expected_head_revision=index - 1,
                actor_id="fixture-artifacts",
                idempotency_key=f"workflow-version-{kind}-{index}",
            ).value
            committed = self.materializer.materialize_action(
                action_id=reservation["materialization_action"]["id"],
                content=content,
                worker_id="fixture-artifacts",
                lease_seconds=30,
            )
            versions.append(committed)
        return ArtifactWorkflowResult(
            request.operation_id,
            request.delivery_epoch,
            request.request_hash,
            request.attempt_id,
            request.stage_key,
            request.effect_kind,
            tuple(sorted(str(version["id"]) for version in versions)),
        )


def _artifacts(root: Path, store: ControlStore, run_id: str) -> _GeneratedArtifacts:
    asset_root = _child(root, "assets")
    asset_root.mkdir(mode=0o700, exist_ok=True)
    store.register_asset_root(
        root_id=ASSET_ROOT_ID,
        private_path=asset_root,
        max_bytes=1_000_000,
        enabled=True,
        actor_id="fixture-artifacts",
        idempotency_key="workflow-asset-root",
    )
    materializer = ArtifactMaterializationService(
        store,
        FilesystemMaterializer((AssetRoot(ASSET_ROOT_ID, asset_root, 1_000_000),)),
    )
    return _GeneratedArtifacts(store, materializer, run_id)


def seed_g0(root: Path) -> dict[str, Any]:
    data = _child(root, "data")
    data.mkdir(mode=0o700, exist_ok=True)
    store = ControlStore(_database(root))
    store.initialize()
    workspace = store.create_workspace(
        title="Echo × Helios Lab",
        actor_id="fixture",
        idempotency_key="workflow-workspace",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Helios-14B memory plan",
        expected_revision=workspace["revision"],
        actor_id="fixture",
        idempotency_key="workflow-thread-0001",
    ).value
    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="fixture",
        idempotency_key="workflow-run-0001",
    ).value
    store.register_source(
        authority="arxiv",
        authority_id="2606.04527",
        source_kind="paper",
        official_title=ECHO_TITLE,
        engine_ref="paper:echo-existing",
        aliases=({"authority": "project", "value": "Echo-Infinity"},),
        actor_id="fixture",
        idempotency_key="workflow-register-echo",
    )
    candidates = (
        CandidateObservation(
            claim_kind="title",
            authority="arxiv",
            authority_id="2606.04527",
            official_title=ECHO_TITLE,
            locator="https://arxiv.org/abs/2606.04527",
        ),
        CandidateObservation(
            claim_kind="url",
            authority="arxiv",
            authority_id="2607.07675",
            official_title=LINGBOT_TITLE,
            locator="https://arxiv.org/abs/2607.07675",
        ),
    )
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title=ECHO_TITLE,
        locator=LINGBOT_URL,
        candidates=[candidate.to_record() for candidate in candidates],
        actor_id="fixture",
        idempotency_key="workflow-source-intent",
    ).value
    projection = GoldenWorkflowFacade(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=None,
        worker_id="workflow-fixture",
        case=_case(intent["id"]),
    ).start(run["id"])
    if projection.blocked_on != "source_decision":
        raise AssertionError("G0 did not stop at the Source decision")
    _write_state(root, {"workspace_id": workspace["id"], "thread_id": thread["id"], "run_id": run["id"], "intent_id": intent["id"]})
    return {"state": "g0", "source_gates": 1, "sources": 0}


def advance_source(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    store = ControlStore(_database(root))
    workflow_id = store.get_workflow_for_run(state["run_id"])["id"]
    facade = GoldenWorkflowFacade(
        store=store,
        engine=_engine(store, workflow_id),
        runtime=None,
        artifacts=None,
        worker_id="workflow-fixture",
        case=_case(state["intent_id"]),
    )
    blocked = facade.advance_until_blocked(state["run_id"])
    if blocked.blocked_on != "source_import":
        raise AssertionError("workflow did not stop at Source import")
    action = store.list_pending_source_imports()[0]
    SourceImportDispatcher(
        store=store,
        adapter=TemporaryResearchImportAdapter(
            database=_child(root, "research", "operations.state"),
            root=_child(root, "research", "imports"),
            safety_root=root,
        ),
    ).deliver(
        action_id=action["id"],
        worker_id="workflow-source-importer",
        claim_key="workflow-source-claim",
        completion_key="workflow-source-complete",
    )
    _start_runtime(store, store.get_run(state["run_id"]))
    blocked = facade.advance_until_blocked(state["run_id"])
    if blocked.blocked_on != "lineage_decision":
        raise AssertionError("workflow did not stop at lineage decision")
    return {"state": "lineage_decision", "sources": 2, "decisions": 1}


def advance_lineage(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    store = ControlStore(_database(root))
    run = store.get_run(state["run_id"])
    identity = _runtime_identity(store, run)
    if run["state"] in {"waiting_for_decision", "resuming"}:
        store.apply_runtime_transition(
            run_id=run["id"],
            target_state="running",
            expected_revision=run["revision"],
            actor_id="fixture-runtime",
            idempotency_key="workflow-lineage-runtime-resumed",
            **identity,
        )
    workflow_id = store.get_workflow_for_run(run["id"])["id"]
    completed = GoldenWorkflowFacade(
        store=store,
        engine=_engine(store, workflow_id),
        runtime=_runtime(store, run["id"], workflow_id),
        artifacts=_artifacts(root, store, run["id"]),
        worker_id="workflow-fixture",
        case=_case(state["intent_id"]),
    ).advance_until_blocked(run["id"])
    if completed.blocked_on != "terminal":
        raise AssertionError("workflow did not reach terminal state")
    current = store.get_run(run["id"])
    if current["state"] != "completed":
        store.apply_runtime_transition(
            run_id=run["id"],
            target_state="completed",
            expected_revision=current["revision"],
            actor_id="fixture-runtime",
            idempotency_key="workflow-runtime-completed",
            **identity,
        )
    return {"state": "completed", "artifacts": 3, "snapshots": 1}


def replay_completed(root: Path, count: int) -> dict[str, Any]:
    if not 1 <= count <= 20:
        raise ValueError("count must be between 1 and 20")
    state = _read_state(root)
    store = ControlStore(_database(root))
    workflow_id = store.get_workflow_for_run(state["run_id"])["id"]
    before = _manifest_counts(store, state["run_id"], state["thread_id"], state["workspace_id"])
    facade = GoldenWorkflowFacade(
        store=store,
        engine=DeterministicResearchEngine(responses={}),
        runtime=None,
        artifacts=None,
        worker_id="workflow-fixture",
        case=_case(state["intent_id"]),
    )
    for _ in range(count):
        if facade.recover(state["run_id"]).blocked_on != "terminal":
            raise AssertionError("completed workflow replay changed state")
    after = _manifest_counts(store, state["run_id"], state["thread_id"], state["workspace_id"])
    if before != after or store.get_workflow(workflow_id)["state"] != "completed":
        raise AssertionError("completed workflow replay was not idempotent")
    return {"state": "completed", "replays": count, "manifest": after}


def emit_redacted(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    store = ControlStore(_database(root))
    run = store.get_run(state["run_id"])
    event_type = "workflow.fixture.invalidated"
    existing = [event for event in store.list_run_events(run["id"]) if event["type"] == event_type]
    if not existing:
        with store._transaction() as connection:
            store._insert_event(
                connection,
                run_id=run["id"],
                attempt_id=run["active_attempt_id"],
                event_type=event_type,
                payload={"reason": "acceptance_replay_probe"},
            )
    return {"state": "completed", "redacted_events": 1}


def _manifest_counts(store: ControlStore, run_id: str, thread_id: str, workspace_id: str) -> dict[str, int]:
    projection = ResearchWorkflowProjector(store).project(run_id)
    return {
        "bindings": len(store.list_run_source_bindings(run_id)),
        "effects": len(store.list_workflow_effects(workflow_id=store.get_workflow_for_run(run_id)["id"])),
        "events": len(store.list_run_events(run_id)),
        "artifacts": len(store.list_artifacts(thread_id=thread_id)),
        "artifact_versions": sum(len(artifact["versions"]) for artifact in projection["artifacts"]),
        "lineage_nodes": len(projection["lineage"]["nodes"]),
        "lineage_links": len(projection["lineage"]["links"]),
        "snapshots": len(store.list_artifact_snapshots(workspace_id=workspace_id)),
        "snapshot_members": sum(len(snapshot["members"]) for snapshot in projection["snapshots"]),
    }


def assert_g0(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    store = ControlStore(_database(root))
    intent = store.get_source_intent(state["intent_id"])
    counts = _manifest_counts(store, state["run_id"], state["thread_id"], state["workspace_id"])
    if intent["state"] != "pending" or counts["bindings"] or counts["effects"] or counts["artifacts"] or counts["snapshots"]:
        raise AssertionError("G0 contains a research or materialization side effect")
    return {"state": "g0", "source_gates": 1, "sources": 0}


def assert_g1(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    store = ControlStore(_database(root))
    run_id = state["run_id"]
    workflow = store.get_workflow_for_run(run_id)
    bindings = store.list_run_source_bindings(run_id)
    research_snapshot = store.read_research_workflow_snapshot(run_id)
    artifacts = research_snapshot["artifacts"]
    snapshots = research_snapshot["snapshots"]
    if store.get_run(run_id)["state"] != "completed" or workflow["state"] != "completed" or sorted(item["disposition"] for item in bindings) != ["imported", "reused"]:
        raise AssertionError("G1 Source/workflow state is incomplete")
    if len(artifacts) != 3 or len(snapshots) != 1:
        raise AssertionError("G1 artifact manifest is incomplete")
    expected_stage_keys = [stage.key for stage in GOLDEN_RESEARCH_WORKFLOW.stages]
    if (
        workflow["current_stage_key"] is not None
        or [stage["stage_key"] for stage in workflow["stages"]] != expected_stage_keys
        or any(stage["state"] != "completed" for stage in workflow["stages"])
    ):
        raise AssertionError("G1 workflow stages are incomplete")
    by_kind = {item["kind"]: item for item in artifacts}
    living = by_kind["living-brief"]
    living_versions = living["versions"]
    if living["head_revision"] != 2 or [item["logical_version"] for item in living_versions] != [1, 2]:
        raise AssertionError("Living Brief head did not advance from v1 to v2")
    if living_versions[1]["parents"] != [{"artifact_version_id": living_versions[0]["id"], "sha256": living_versions[0]["sha256"]}]:
        raise AssertionError("Living Brief v2 is not bound to v1")
    expected = {"living-brief": (LIVING_V1, LIVING_V2), "evidence-matrix": (EVIDENCE,), "training-plan": (TRAINING,)}
    source_ids = sorted(binding["source_id"] for binding in bindings)
    head_version_ids = set()
    for kind, values in expected.items():
        artifact = by_kind[kind]
        versions = artifact["versions"]
        actual = tuple(ArtifactReader(store).read(item["id"]).content for item in versions)
        if actual != values:
            raise AssertionError(f"{kind} content mismatch")
        head_version_ids.add(artifact["head_artifact_version_id"])
        for version in versions:
            provenance = version["provenance"]
            if (
                version["state"] != "committed"
                or provenance is None
                or provenance["schema_version"] != 1
                or provenance["run_id"] != run_id
                or provenance["attempt_id"] != version["attempt_id"]
                or sorted(provenance["source_ids"]) != source_ids
                or provenance["generator"] != {"name": "workflow-research", "version": "1"}
                or provenance["tool"] != {"name": "workflow-writer", "version": "1"}
                or provenance["parents"] != version["parents"]
                or provenance["media_type"] != version["media_type"]
                or provenance["byte_length"] != version["byte_length"]
                or provenance["sha256"] != version["sha256"]
                or provenance["committed_at"] != version["committed_at"]
            ):
                raise AssertionError(f"{kind} provenance is incomplete")
    snapshot_ids = {member["artifact_version_id"] for member in snapshots[0]["members"]}
    version_by_id = {
        version["id"]: version
        for artifact in artifacts
        for version in artifact["versions"]
    }
    if snapshot_ids != head_version_ids or len(snapshots[0]["members"]) != 3 or living_versions[0]["id"] in snapshot_ids:
        raise AssertionError("snapshot did not bind each artifact head exclusively")
    for member in snapshots[0]["members"]:
        version = version_by_id[member["artifact_version_id"]]
        if (
            member["artifact_id"] != version["artifact_id"]
            or member["logical_version"] != version["logical_version"]
            or member["sha256"] != version["sha256"]
        ):
            raise AssertionError("snapshot member does not match its artifact version")
    event_types = Counter(item["type"] for item in store.list_run_events(run_id))
    if not event_types or len(store.list_workflow_effects(workflow_id=workflow["id"])) != 11:
        raise AssertionError("G1 durable effects are incomplete")
    counts = _manifest_counts(store, run_id, state["thread_id"], state["workspace_id"])
    if counts["artifact_versions"] != 4 or counts["lineage_nodes"] != 3 or counts["lineage_links"] != 2 or counts["snapshot_members"] != 3:
        raise AssertionError("G1 version, lineage, or snapshot counts are incomplete")
    return {"state": "completed", "sources": 2, "artifacts": 3, "snapshots": 1, "manifest": counts}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow-control-fixture")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("assert-network-guard", "seed-g0", "advance-source", "advance-lineage", "emit-redacted", "assert-g0", "assert-g1"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
    replay = commands.add_parser("replay-completed")
    replay.add_argument("--root", required=True)
    replay.add_argument("--count", required=True, type=int)
    return parser


def main() -> int:
    network_guard_probes = _assert_network_guard()
    arguments = _parser().parse_args()
    root = _safe_root(arguments.root)
    handlers = {
        "seed-g0": seed_g0,
        "advance-source": advance_source,
        "advance-lineage": advance_lineage,
        "emit-redacted": emit_redacted,
        "assert-g0": assert_g0,
        "assert-g1": assert_g1,
    }
    if arguments.command == "assert-network-guard":
        result = {"state": "network_guarded", "probes": network_guard_probes}
    elif arguments.command == "replay-completed":
        result = replay_completed(root, arguments.count)
    else:
        result = handlers[arguments.command](root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
