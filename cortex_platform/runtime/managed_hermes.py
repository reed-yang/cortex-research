"""The `HermesBackend` that speaks `cortex-worker/2` to a managed release.

Everything the native backend does in this process, this does across a pipe to
one worker on the slot's own interpreter. The interesting difference is not the
transport — it is what becomes provable. In-process, `durable_operation_
deduplication` was hardcoded `False` and correctly so: the native backend's
operation memory is an `OrderedDict` trimmed to 1024 entries, which forgets. The
worker's is an fsynced ledger under an exclusive flock, which does not — and
⟦AMD-3⟧ says the capability may only be claimed by *demonstrating* it, once per
worker process, with a real crash.

D-S3.3-6's fence: this lives in `cortex_platform/runtime/`, beside
`HermesAdapter`, and `product/orchestration/` keeps importing only the port.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..product.runtime_update.sandbox import (
    DEFAULT_EGRESS_PORT,
    SandboxLaunch,
    prepare_sandbox,
)
from ..product.runtime_update.supervisor import (
    WorkerCrashed,
    WorkerError as WorkerChannelError,
    WorkerProtocolError,
    WorkerSupervisorV2,
    WorkerTurnUncertain,
    worker_environment,
)
from ..product.runtime_update.worker_payload.cortex_worker.approval import (
    is_approval_choice,
)
from ..product.runtime_update.worker_payload.cortex_worker.digests import (
    PROJECTION_VERSION,
    result_digest,
)
from ..product.runtime_update.worker_payload.cortex_worker.turn import EVENT_FINISH
from ..product.runtime_update.worker_protocol import SlotInterpreterDescriptor
from .compatibility import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    SUPPORTED_HERMES_DISTRIBUTIONS,
    SUPPORTED_SESSION_DB_SCHEMA,
    CompatibilityCheck,
    CompatibilityReport,
)
from .hermes import (
    hermes_turn_prompt,
    HermesDuplicateAttemptError,
    HermesInspection,
    HermesRunInput,
    HermesRunResult,
    HermesSession,
    HermesSignal,
    HermesUnavailableError,
    RuntimeOperationUncertain,
)
from .models import (
    ActionOutcomeStatus,
    RuntimeActionOutcome,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeReleaseIdentity,
)

PROBE_NAMESPACE = "probe"
PROBE_KIND = "dedup-probe"
#: Distinguishable from any real request digest, and stable across relaunches so
#: the replayed `begin` is the same operation with the same digest.
PROBE_REQUEST_DIGEST = "cd" * 32

#: The stages ⟦AMD-3⟧ requires, in order. A failure names the stage it reached.
PROBE_STAGES = (
    "first_launch",
    "probe_begin",
    "kill",
    "reap",
    "relaunch",
    "remeasure",
    "status_uncertain",
    "replay_duplicate",
)


class ManagedRuntimeUnavailable(HermesUnavailableError):
    """The managed worker could not be launched or has stopped serving."""


class DedupProbeFailed(RuntimeError):
    """The durable-dedup demonstration did not complete. Names its stage."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage


@dataclass(frozen=True)
class ProbeProvenance:
    """What was proven, against which process. Pinned into the attempt."""

    probe_operation_id: str
    probed_at: str
    worker_pid: int
    worker_launch_id: str
    projection_version: int = PROJECTION_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "probe_operation_id": self.probe_operation_id,
            "probed_at": self.probed_at,
            "worker_pid": self.worker_pid,
            "worker_launch_id": self.worker_launch_id,
            "projection_version": self.projection_version,
        }


@dataclass(frozen=True)
class _ActionRecord:
    """What was decided for one `adapter_operation_id`, and on whose behalf."""

    identity: tuple[str, ...]
    delivery_epoch: int
    outcome: RuntimeActionOutcome


@dataclass
class _ManagedExecution:
    """One reserved attempt. Created by `reserve_attempt`, mutated by `run`.

    `operation_id` is empty between the reservation and the turn actually being
    opened — the window the launch and the dedup probe live in — which is what
    makes "reserved but not started" a distinguishable state rather than the
    same thing as "no such attempt".
    """

    operation_id: str
    session_ref: str
    token: object | None = None
    cancel_requested: bool = False


class ManagedHermesBackend:
    """One worker process per active generation, and one turn at a time in it."""

    #: ⟦AMD-4⟧ A relaunch is not cheap: every one reruns the full dedup probe —
    #: two extra processes, a SIGKILL, a reap and the ledger's fsyncs. Relaunching
    #: a failed channel unconditionally would trade one wedge for a launch storm
    #: whenever the failure source is persistent, so the healing is rate-limited
    #: rather than counted forever: a worker that fails once an hour is not the
    #: same thing as one that fails five times a second.
    MAX_CHANNEL_RELAUNCHES = 3
    RELAUNCH_WINDOW_SECONDS = 60.0

    def __init__(
        self,
        descriptor_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
        environment_factory: Callable[[], Mapping[str, str]] | None = None,
        supervisor_factory: Callable[..., WorkerSupervisorV2] | None = None,
        turn_timeout: float | None = None,
        sandbox: bool = True,
        egress_port: int = DEFAULT_EGRESS_PORT,
        agent_options_factory: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        if environment is not None and environment_factory is not None:
            raise ValueError("pass an environment or a factory, never both")
        self._descriptor_path = Path(descriptor_path).resolve(strict=True)
        self._descriptor = SlotInterpreterDescriptor.load(self._descriptor_path)
        self._environment = dict(environment) if environment is not None else None
        # ⟦P5.4⟧ A backend built once at daemon start outlives every transport
        # window it will serve, so an environment fixed at construction would
        # answer the credential question before the operator has decided it.
        # The factory is called for each launch, including the probe's and every
        # relaunch, so the gate in force at that instant is what the process gets.
        self._environment_factory = environment_factory
        # ⟦P5.4c⟧ Which model provider a TURN talks to. The fork resolves this
        # from its own ambient configuration when nobody says -- and inside the
        # seatbelt nobody can: `config.yaml` lives under a HERMES_HOME whose
        # `hooks` directory is deny-write, so `ensure_hermes_home()` fails and
        # every leg of the auto-detect chain comes back empty. The observed
        # result is `AIAgent.__init__` raising "No LLM provider configured"
        # before a single request leaves the sandbox, which the adapter reports
        # as `runtime_execution_failed` with nothing to read. Named per turn
        # rather than at construction for the same reason as the environment.
        self._agent_options_factory = agent_options_factory
        self._factory = supervisor_factory or WorkerSupervisorV2
        # ⟦S3.4/D3⟧ On by default, and refused rather than skipped where it
        # cannot be applied: this is the production seam, and a managed worker
        # running without the OS boundary would report a containment it does not
        # have. `sandbox=False` exists for callers who are testing the channel
        # rather than the boundary, and it is never the product's choice.
        self._sandboxed = bool(sandbox)
        # The one port `(allow network-outbound (remote tcp "*:<port>"))` names.
        # 443 in production, and the only reason to move it is an acceptance
        # whose provider stand-in is a loopback server on an unprivileged port.
        self._egress_port = int(egress_port)
        self.sandbox_launch: SandboxLaunch | None = None
        self._turn_timeout = turn_timeout
        self._lock = threading.RLock()
        self._supervisor: WorkerSupervisorV2 | None = None
        self._provenance: ProbeProvenance | None = None
        self._probe_failure: str | None = None
        self._sessions: dict[str, HermesSession] = {}
        self._executions: dict[tuple[str, str], _ManagedExecution] = {}
        self._turn_index: dict[str, int] = {}
        self._action_operations: OrderedDict[str, _ActionRecord] = OrderedDict()
        self._action_cache_limit = 1024
        self._identity: RuntimeReleaseIdentity | None = None
        self._relaunches: list[float] = []

    # -- lifecycle ---------------------------------------------------------

    def _launch_environment(self) -> Mapping[str, str] | None:
        if self._environment_factory is None:
            return self._environment
        return dict(self._environment_factory())

    def _new_supervisor(self, descriptor_path: Path) -> WorkerSupervisorV2:
        environment = self._launch_environment()
        if not self._sandboxed:
            return self._factory(descriptor_path, environment=environment)
        # Per descriptor, not per backend: the dedup probe runs on its own
        # `probe/` namespace, so its state root and its HERMES_HOME are not the
        # production worker's and a profile generated for one would deny the
        # wrong four paths for the other.
        descriptor = SlotInterpreterDescriptor.load(descriptor_path)
        # No `hermes_home=` override. `WorkerSupervisorV2._environment` now pins
        # HERMES_HOME to `<state_dir>/hermes-home` for the descriptor it is
        # launching, in both branches, and `build_policy` derives the same path
        # from the same descriptor — so re-parenting a caller's basename here
        # would be a second guess at a value that is no longer guessed.
        launch = prepare_sandbox(
            descriptor,
            descriptor_path=descriptor_path,
            egress_port=self._egress_port,
        )
        self.sandbox_launch = launch
        return self._factory(
            descriptor_path, environment=environment, sandbox=launch
        )

    def _launch(self) -> WorkerSupervisorV2:
        """Every launch, including an internal relaunch, pays for the probe.

        ⟦AMD-3⟧ names this the seam rather than `HermesAdapter._load_backend`,
        which memoizes the backend object once and never resets it: a capability
        proven there would outlive every process it was proven against.
        """

        with self._lock:
            supervisor = self._supervisor
            # `alive` is `poll() is None` and nothing else, so a channel that
            # `_fail`ed with its process still up passes it forever. Handing that
            # object back made the `close(force=True)` below unreachable for the
            # one state that needs it most.
            if supervisor is not None and supervisor.alive and not supervisor.failed:
                return supervisor
            relaunching = supervisor is not None
            if supervisor is not None:
                # Reaps the process and releases the ledger flock the relaunch
                # is about to need.
                supervisor.close(force=True)
                self._supervisor = None
            # Checked for a first launch too, so an exhausted budget stays
            # exhausted for its window: the refusal above leaves `_supervisor`
            # None, and a fresh launch must not be a way around it.
            if self._relaunch_budget_spent():
                raise ManagedRuntimeUnavailable(
                    "managed worker relaunch budget exhausted"
                )
            if relaunching:
                self._relaunches.append(time.monotonic())
            self._provenance = None
            self._probe_failure = None
            try:
                provenance_stub = self._run_dedup_probe()
            except DedupProbeFailed as exc:
                # Not fatal to the runtime: the worker may still serve. It is
                # fatal to the *capability*, which is what the gate reads.
                self._probe_failure = exc.stage
                provenance_stub = None
            supervisor = self._new_supervisor(self._descriptor_path)
            supervisor.start()
            self._supervisor = supervisor
            self._identity = self._read_identity(supervisor)
            if provenance_stub is not None:
                process = supervisor.process
                assert process is not None
                self._provenance = ProbeProvenance(
                    probe_operation_id=provenance_stub,
                    probed_at=datetime.now(timezone.utc).isoformat(),
                    worker_pid=process.pid,
                    worker_launch_id=supervisor.worker_launch_id,
                )
            return supervisor

    def _relaunch_budget_spent(self) -> bool:
        now = time.monotonic()
        self._relaunches = [
            stamp
            for stamp in self._relaunches
            if now - stamp < self.RELAUNCH_WINDOW_SECONDS
        ]
        return len(self._relaunches) >= self.MAX_CHANNEL_RELAUNCHES

    def _worker_is_serving(self) -> bool:
        """Is the channel serving now — without healing it as a side effect.

        A wedged channel (failed, process still up) is *reported*: `compatibility()`
        is a read, and ⟦AMD-4⟧'s relaunch belongs to the next turn. A worker whose
        process is gone is a different thing, and relaunching that one is the
        behaviour ⟦AMD-3⟧ pins — the capability has to be reproven against the
        process that will serve.
        """

        with self._lock:
            supervisor = self._supervisor
        if supervisor is not None and supervisor.failed and supervisor.alive:
            return False
        try:
            return self._require_supervisor().alive
        except (ManagedRuntimeUnavailable, WorkerChannelError, OSError):
            return False

    def _require_supervisor(self) -> WorkerSupervisorV2:
        supervisor = self._launch()
        if not supervisor.alive:
            raise ManagedRuntimeUnavailable("managed worker is not running")
        return supervisor

    def worker(self) -> WorkerSupervisorV2:
        """The live worker process, launched on first use.

        The public form of `_require_supervisor`, and the seam the daemon binds
        a transport RPC to: `GatedWorkerTransportRPC` needs an object it can
        send one frame to, and reaching into a private method for it would make
        the launch policy -- probe, relaunch budget, channel recovery -- a
        detail every caller had to know it was inheriting.
        """

        return self._require_supervisor()

    def close(self) -> None:
        with self._lock:
            supervisor = self._supervisor
            self._supervisor = None
            self._provenance = None
        if supervisor is not None:
            supervisor.close()

    # -- the dedup probe ---------------------------------------------------

    def _probe_descriptor_path(self) -> Path:
        """The same slot and interpreter, a `probe/` namespace of the state dir.

        A separate namespace because the probe's whole point is to leave an
        `uncertain` record behind, and the production operation namespace is not
        somewhere to leave one. The same `OperationLedger` implementation, so
        what is demonstrated is the mechanism production will use.
        """

        state_dir = self._descriptor.state_dir
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe_state = state_dir / PROBE_NAMESPACE
        probe_state.mkdir(exist_ok=True, mode=0o700)
        document = json.loads(self._descriptor_path.read_text(encoding="utf-8"))
        document["state_dir"] = str(probe_state)
        path = state_dir / "probe-descriptor.json"
        path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        return path

    def _run_dedup_probe(self) -> str:
        operation_id = f"probe.{uuid.uuid4().hex}"
        path = self._probe_descriptor_path()
        stage = PROBE_STAGES[0]
        first = self._new_supervisor(path)
        try:
            first.start()
            stage = "probe_begin"
            accepted = first.request(
                "operation.begin",
                {
                    "operation_id": operation_id,
                    "kind": PROBE_KIND,
                    "request_digest": PROBE_REQUEST_DIGEST,
                },
            )
            if not isinstance(accepted, dict) or accepted.get("state") != "accepted":
                raise DedupProbeFailed(stage, "probe operation was not accepted")
            process = first.process
            if process is None:
                raise DedupProbeFailed("kill", "probe worker is not running")
            stage = "kill"
            # SIGKILL, not SIGTERM: a graceful stop would let the worker finish
            # anything it was doing, which is the opposite of what is being
            # demonstrated. `close(force=True)` sends SIGTERM first, so the
            # signal is delivered here directly.
            os.kill(process.pid, signal.SIGKILL)
            stage = "reap"
            # The flock releases when the fd closes, and the fd closes when the
            # process is reaped — relaunching before the reap yields
            # `ledger_unavailable`, a false negative that looks like a failure.
            process.wait(timeout=10)
        except DedupProbeFailed:
            raise
        except (WorkerChannelError, OSError) as exc:
            raise DedupProbeFailed(stage, str(exc)) from exc
        finally:
            first.close(force=True)

        stage = "relaunch"
        second = self._new_supervisor(path)
        try:
            second.start()
            stage = "remeasure"
            identity = second.request("identity.measure", {})
            if not isinstance(identity, dict):
                raise DedupProbeFailed(stage, "identity was not remeasured")
            stage = "status_uncertain"
            status = second.request(
                "operation.status", {"operation_id": operation_id}
            )
            if not isinstance(status, dict) or status.get("state") != "uncertain":
                raise DedupProbeFailed(
                    stage, f"pre-crash record is {status!r}, not uncertain"
                )
            if status.get("request_digest") != PROBE_REQUEST_DIGEST:
                raise DedupProbeFailed(stage, "pre-crash request digest was lost")
            stage = "replay_duplicate"
            replay = second.request(
                "operation.begin",
                {
                    "operation_id": operation_id,
                    "kind": PROBE_KIND,
                    "request_digest": PROBE_REQUEST_DIGEST,
                },
            )
            if not isinstance(replay, dict) or replay.get("state") != "duplicate":
                raise DedupProbeFailed(stage, "a replayed operation re-executed")
        except DedupProbeFailed:
            raise
        except (WorkerChannelError, OSError) as exc:
            raise DedupProbeFailed(stage, str(exc)) from exc
        finally:
            second.close()
        return operation_id

    @property
    def probe_provenance(self) -> ProbeProvenance | None:
        return self._provenance

    @property
    def probe_failure_stage(self) -> str | None:
        return self._probe_failure

    def _dedup_is_live(self) -> bool:
        """⟦AMD-3⟧ Nothing outlives the process it was proven on."""

        with self._lock:
            supervisor = self._supervisor
            provenance = self._provenance
        if supervisor is None or provenance is None:
            return False
        if not supervisor.alive:
            return False
        if supervisor.worker_launch_id != provenance.worker_launch_id:
            return False
        try:
            health = supervisor.request("health.check", {})
        except WorkerChannelError:
            return False
        return (
            isinstance(health, dict)
            and health.get("ledger_open") is True
            and health.get("quarantined") is False
        )

    # -- HermesBackend -----------------------------------------------------

    def _read_identity(
        self, supervisor: WorkerSupervisorV2
    ) -> RuntimeReleaseIdentity | None:
        measured = supervisor.identity
        try:
            return RuntimeReleaseIdentity(
                release_id=str(measured["release_id"]),
                state_generation_id=str(measured["state_generation_id"]),
                slot_id=str(measured["slot_id"]),
                artifact_digest=str(measured["artifact_digest"]),
                worker_protocol=str(measured["worker_protocol"]),
            )
        except (KeyError, ValueError):
            return None

    def runtime_identity(self) -> RuntimeReleaseIdentity | None:
        try:
            self._require_supervisor()
        except (ManagedRuntimeUnavailable, WorkerChannelError, OSError):
            return None
        return self._identity

    def _manifest(self) -> Mapping[str, Any]:
        try:
            raw = json.loads(
                (self._descriptor.slot_path / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def compatibility(self) -> CompatibilityReport:
        """Answered from the release's own evidence, never by importing the fork.

        The whole point of a managed backend is that this process does not have
        the fork in it. What can be checked from here is what the release
        declares and what the worker reports about itself — and both are already
        digest-pinned by the descriptor the worker measured against.
        """

        manifest = self._manifest()
        version = manifest.get("distribution_version")
        schema = manifest.get("session_schema")
        alive = self._worker_is_serving()
        checks = (
            CompatibilityCheck(
                name="distribution_version",
                expected="|".join(SUPPORTED_HERMES_DISTRIBUTIONS),
                observed=str(version),
                compatible=version in SUPPORTED_HERMES_DISTRIBUTIONS,
            ),
            CompatibilityCheck(
                name="session_db_schema",
                expected=f"{SUPPORTED_SESSION_DB_SCHEMA[0]}-{SUPPORTED_SESSION_DB_SCHEMA[1]}",
                observed=str(schema),
                compatible=(
                    type(schema) is int
                    and SUPPORTED_SESSION_DB_SCHEMA[0]
                    <= schema
                    <= SUPPORTED_SESSION_DB_SCHEMA[1]
                ),
            ),
            CompatibilityCheck(
                name="worker_protocol",
                expected=self._descriptor.worker_protocol,
                observed=str(
                    (self._identity.worker_protocol if self._identity else None)
                ),
                compatible=(
                    self._identity is not None
                    and self._identity.worker_protocol
                    == self._descriptor.worker_protocol
                ),
            ),
            CompatibilityCheck(
                name="worker_process",
                expected="serving",
                observed="serving" if alive else "stopped",
                compatible=alive,
            ),
        )
        return CompatibilityReport(
            runtime_version=str(version) if version is not None else None,
            session_db_schema=schema if type(schema) is int else None,
            checks=checks,
            provider_models={},
        )

    def capabilities(self) -> RuntimeCapabilities:
        report = self.compatibility()
        available = report.compatible
        return RuntimeCapabilities(
            adapter_id=ADAPTER_ID,
            available=available,
            session_create=available,
            session_load=available,
            session_fork=available,
            run_stream=available,
            cancel=available,
            pause=False,
            steer=False,
            decisions=available,
            session_rebinding=available,
            checkpoint_recovery=available,
            action_outcome_query=available,
            # The only capability in this object that is a demonstration rather
            # than a declaration.
            durable_operation_deduplication=available and self._dedup_is_live(),
            provider_models=report.provider_models,
        )

    # -- sessions ----------------------------------------------------------

    @staticmethod
    def _new_session_ref() -> str:
        return f"cortex_{uuid.uuid4().hex}"

    def open_session(
        self, metadata: Mapping[str, Any], adapter_operation_id: str
    ) -> HermesSession:
        _ = metadata, adapter_operation_id
        session = HermesSession(session_ref=self._new_session_ref())
        with self._lock:
            self._sessions[session.session_ref] = session
        return session

    def load_session(self, session_ref: str) -> HermesSession:
        with self._lock:
            existing = self._sessions.get(session_ref)
            if existing is not None:
                return existing
            # A ref the fork owns: the worker resolves it when the agent is
            # constructed with `session_id`, which is the same resolution the
            # native backend relied on.
            session = HermesSession(session_ref=session_ref)
            self._sessions[session_ref] = session
            return session

    def fork_session(
        self,
        session_ref: str,
        metadata: Mapping[str, Any],
        adapter_operation_id: str,
    ) -> HermesSession:
        _ = metadata, adapter_operation_id
        parent = self.load_session(session_ref)
        session = HermesSession(
            session_ref=self._new_session_ref(),
            parent_session_ref=parent.session_ref,
        )
        with self._lock:
            self._sessions[session.session_ref] = session
        return session

    def inspect(self, session_ref: str) -> HermesInspection:
        with self._lock:
            known = session_ref in self._sessions
            active = any(
                execution.session_ref == session_ref
                for execution in self._executions.values()
            )
        return HermesInspection(exists=known, active=active)

    def recover(
        self,
        session_ref: str,
        checkpoint: RuntimeCheckpoint,
        adapter_operation_id: str,
    ) -> HermesSession:
        _ = adapter_operation_id
        parent = self.load_session(session_ref)
        session = HermesSession(
            session_ref=self._new_session_ref(),
            parent_session_ref=parent.session_ref,
        )
        with self._lock:
            self._sessions[session.session_ref] = session
        _ = checkpoint
        return session

    # -- attempts ----------------------------------------------------------

    def reserve_attempt(self, run_id: str, attempt_id: str) -> object | None:
        key = (run_id, attempt_id)
        token = object()
        with self._lock:
            if key in self._executions:
                return None
            self._executions[key] = _ManagedExecution(
                operation_id="", session_ref="", token=token
            )
        return token

    def release_attempt(
        self, run_id: str, attempt_id: str, execution_token: object
    ) -> None:
        key = (run_id, attempt_id)
        with self._lock:
            execution = self._executions.get(key)
            if execution is None or execution.token is not execution_token:
                return
            operation_id = execution.operation_id
            self._executions.pop(key, None)
        if operation_id:
            # The attempt is being released while the worker may still be inside
            # the turn it owned — parked in an approval callback nobody is left
            # to answer. `release_attempt` has no product-side pending registry
            # to deny, so the equivalent hardening is to tell the worker to stop.
            try:
                self._require_supervisor().cancel_turn(operation_id)
            except (WorkerChannelError, ManagedRuntimeUnavailable, OSError):
                pass

    def _operation_id(self, attempt_id: str) -> str:
        """⟦Design⟧ `f"{attempt_id}.{turn_index}"`.

        A retried attempt is a new attempt id and therefore a new operation, so
        a retry can never be mistaken for a replay of an `uncertain` one.
        """

        with self._lock:
            index = self._turn_index.get(attempt_id, 0)
            self._turn_index[attempt_id] = index + 1
        return f"{attempt_id}.{index}"

    def _turn_payload(self, request: HermesRunInput) -> dict[str, Any]:
        """The canonical turn request, and therefore the thing digested.

        `request_digest` is a digest of this document, so anything that changes
        it changes the operation's identity — which is the point: a replay is a
        replay only if it is asking for the same work.
        """

        system_message, options = hermes_turn_prompt(request, self._agent_options())
        return {
            "session_ref": request.session_ref,
            "parent_session_ref": self.load_session(
                request.session_ref
            ).parent_session_ref,
            "user_message": request.user_message,
            "system_message": system_message,
            "conversation_history": [
                dict(item) for item in request.conversation_history
            ],
            "task_id": request.attempt_id,
            "agent_options": options,
            "session_db_path": self._session_db_path(),
        }

    def _canceled_before_start(self, request: HermesRunInput) -> HermesRunResult:
        return HermesRunResult(
            session_ref=request.session_ref,
            final_response=None,
            canceled=True,
        )

    def run(
        self, request: HermesRunInput, emit: Callable[[HermesSignal], None]
    ) -> HermesRunResult:
        key = (request.run_id, request.attempt_id)
        # First of two checks. The native backend — which D-S3.3-6 names as the
        # specification — consumes a pre-start cancel before the agent is even
        # constructed, and here the thing being skipped is larger still: the
        # launch and the whole dedup probe.
        with self._lock:
            execution = self._executions.get(key)
            if execution is not None:
                if (
                    execution.token is not None
                    and execution.token is not request.execution_token
                ):
                    raise HermesDuplicateAttemptError("duplicate_active_attempt")
                if execution.cancel_requested:
                    self._executions.pop(key, None)
                    return self._canceled_before_start(request)
                # Mutated, not replaced: replacing it threw away the entry
                # `reserve_attempt` created and with it the cancel recorded on it.
                execution.session_ref = request.session_ref
            else:
                self._executions[key] = _ManagedExecution(
                    operation_id="",
                    session_ref=request.session_ref,
                    token=request.execution_token,
                )
        supervisor = self._require_supervisor()
        payload = self._turn_payload(request)
        digest = _request_digest(payload)
        # Second check, immediately before the turn is opened. Checking only at
        # entry misses the entire launch-and-probe window, which is where a
        # cancel is most likely to land.
        with self._lock:
            execution = self._executions.get(key)
            if execution is None or execution.cancel_requested:
                self._executions.pop(key, None)
                return self._canceled_before_start(request)
            operation_id = self._operation_id(request.attempt_id)
            execution.operation_id = operation_id
        try:
            state = supervisor.begin_turn(operation_id, digest, payload)
        except WorkerCrashed as exc:
            with self._lock:
                self._executions.pop(key, None)
            raise ManagedRuntimeUnavailable("managed worker exited") from exc
        if state == "duplicate":
            # The worker already has a durable record for this operation. The
            # attempt's outcome is whatever that record says, and re-running it
            # is exactly what durable dedup exists to prevent.
            with self._lock:
                self._executions.pop(key, None)
            return self._duplicate_result(supervisor, operation_id, request)
        finish: Mapping[str, Any] | None = None
        try:
            for event in supervisor.turn_events(operation_id):
                kind = str(event.get("kind", ""))
                fields = event.get("payload") or {}
                if kind == EVENT_FINISH:
                    finish = fields
                    break
                emit(HermesSignal(kind, fields, stable_id=_stable_id(kind, fields)))
        except WorkerTurnUncertain as exc:
            raise RuntimeOperationUncertain(str(exc)) from exc
        finally:
            with self._lock:
                self._executions.pop(key, None)
        if finish is None:
            raise RuntimeOperationUncertain("turn ended without an outcome")
        result = finish.get("result") or {}
        self._last_result_digest = str(finish.get("result_digest", ""))
        return HermesRunResult(
            session_ref=str(result.get("session_ref") or request.session_ref),
            final_response=(
                str(result["final_response"])
                if result.get("final_response") is not None
                else None
            ),
            canceled=bool(result.get("canceled", False)),
            failed=bool(result.get("failed", False)),
        )

    def _duplicate_result(
        self,
        supervisor: WorkerSupervisorV2,
        operation_id: str,
        request: HermesRunInput,
    ) -> HermesRunResult:
        # `begin_turn` registers the turn's inbox before it asks, and only
        # `turn_events`' `finally` ever removes it — a path this branch never
        # reaches, because there are no events to iterate. Left open, the entry
        # survives for the life of the process and every later replay of the
        # same operation id is refused as already open.
        supervisor.close_turn(operation_id)
        status = supervisor.request(
            "operation.status", {"operation_id": operation_id}
        )
        state = status.get("state") if isinstance(status, dict) else None
        self._last_result_digest = (
            str(status.get("result_digest", "")) if isinstance(status, dict) else ""
        )
        if state == "committed":
            return HermesRunResult(
                session_ref=request.session_ref, final_response=None
            )
        # `uncertain` is terminal for the attempt: orchestration fails it with
        # the typed category and the operator's retry is a new attempt id.
        raise RuntimeOperationUncertain("runtime_operation_uncertain")

    @property
    def last_result_digest(self) -> str:
        return getattr(self, "_last_result_digest", "")

    def _agent_options(self) -> dict[str, Any]:
        # ⟦AMD-5⟧ `agent_options`' first producer. `quiet_mode` is set on the
        # worker side too, because this side cannot enforce it.
        options: dict[str, Any] = {"quiet_mode": True}
        if self._agent_options_factory is not None:
            supplied = dict(self._agent_options_factory())
            # `quiet_mode` is not negotiable: fd 1 is the frame stream.
            supplied.pop("quiet_mode", None)
            options.update(supplied)
        return options

    def _session_db_path(self) -> str | None:
        """⟦P5.4d / F9⟧ Nothing, and that is the fix rather than an omission.

        The shipped worker hands this value straight to `hermes_state.SessionDB`,
        whose signature is `db_path: Path = None` and whose first statement is
        `self.db_path.parent.mkdir(...)`. What crosses the boundary is JSON, and
        JSON has no `Path` — so a named path was an `AttributeError` in the
        worker before `AIAgent` was constructed and before any request left the
        sandbox, reported as a bare `runtime_execution_failed` because `turn.py`
        discards the exception on purpose. Every managed turn, every time; found
        by running one for real, and invisible to a tree in which every other
        caller passes `session_db_path=None`.

        Naming nothing is not naming nowhere. The fork's own default is
        `HERMES_HOME/state.db`, and HERMES_HOME is `<state_dir>/hermes-home` —
        product-created, product-owned, pinned by `WorkerSupervisorV2._environment`
        in both branches, writable under the seatbelt and denied only for the
        five names the confinement forbids. The session store stays inside the
        per-generation state dir either way; what changes is that the product
        stops naming it in a type the worker cannot accept.

        The other half of the fix belongs to the next release: `ForkRunner._session_db`
        should read `SessionDB(Path(path))`. That file is byte-pinned in the
        release manifest, so correcting it there is a re-certification rather
        than a patch, and this side works with the certified worker as shipped.
        """

        return None

    # -- control -----------------------------------------------------------

    def _remember(
        self, identity: tuple[str, ...], outcome: RuntimeActionOutcome
    ) -> RuntimeActionOutcome:
        with self._lock:
            key = outcome.adapter_operation_id
            self._action_operations[key] = _ActionRecord(
                identity, outcome.delivery_epoch, outcome
            )
            self._action_operations.move_to_end(key)
            while len(self._action_operations) > self._action_cache_limit:
                self._action_operations.popitem(last=False)
        return outcome

    def _perform_action(
        self,
        *,
        identity: tuple[str, ...],
        adapter_operation_id: str,
        delivery_epoch: int,
        effect: Callable[[], RuntimeActionOutcome],
    ) -> RuntimeActionOutcome:
        """`_NativeHermesBackend._perform_action`'s guard, term for term.

        Mirrored rather than shared because `runtime/hermes.py` is outside this
        change's file scope — hoisting both onto one helper is the right shape
        and is deferred, not declined. What matters here is that the managed
        backend answers with the same closed `RuntimeActionOutcome` vocabulary
        as the native one: `adapter_operation_conflict` for a reused id under a
        different identity, `stale_delivery_epoch` for a regressed epoch,
        DEDUPLICATED for a redelivered accepted action.

        One difference is deliberate. Native holds its operation lock across the
        effect; here the effect is a blocking channel round trip, and holding
        the backend lock across it would stall `inspect()` and every other
        action for its duration. The guard is still atomic; two genuinely
        concurrent redeliveries of the same id could both pass it, which is the
        window `delivery_epoch` exists to make harmless.
        """

        with self._lock:
            previous = self._action_operations.get(adapter_operation_id)
            if previous is not None:
                if previous.identity != identity:
                    return RuntimeActionOutcome(
                        adapter_operation_id,
                        delivery_epoch,
                        ActionOutcomeStatus.REJECTED,
                        "adapter_operation_conflict",
                    )
                if delivery_epoch < previous.delivery_epoch:
                    return RuntimeActionOutcome(
                        adapter_operation_id,
                        delivery_epoch,
                        ActionOutcomeStatus.REJECTED,
                        "stale_delivery_epoch",
                    )
                if previous.outcome.status == ActionOutcomeStatus.ACCEPTED:
                    return RuntimeActionOutcome(
                        adapter_operation_id,
                        delivery_epoch,
                        ActionOutcomeStatus.DEDUPLICATED,
                    )
                if delivery_epoch == previous.delivery_epoch:
                    return previous.outcome
        return self._remember(identity, effect())

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str,
    ) -> RuntimeActionOutcome:
        def effect() -> RuntimeActionOutcome:
            with self._lock:
                execution = self._executions.get((run_id, attempt_id))
                if execution is None:
                    # No such attempt. Distinct from the one below: there is
                    # nothing here to record an intent on.
                    return RuntimeActionOutcome(
                        adapter_operation_id=adapter_operation_id,
                        delivery_epoch=delivery_epoch,
                        status=ActionOutcomeStatus.REJECTED,
                        reason_code="no_active_turn",
                    )
                operation_id = execution.operation_id
                if not operation_id:
                    # Reserved, not yet started. The native backend sets
                    # `cancel_requested` and answers ACCEPTED here, and `run()`
                    # returns `canceled=True` without executing; collapsing this
                    # into `no_active_turn` dropped the cancel entirely.
                    execution.cancel_requested = True
                    return RuntimeActionOutcome(
                        adapter_operation_id=adapter_operation_id,
                        delivery_epoch=delivery_epoch,
                        status=ActionOutcomeStatus.ACCEPTED,
                    )
            try:
                self._require_supervisor().cancel_turn(operation_id)
            except (WorkerChannelError, ManagedRuntimeUnavailable):
                return RuntimeActionOutcome(
                    adapter_operation_id=adapter_operation_id,
                    delivery_epoch=delivery_epoch,
                    status=ActionOutcomeStatus.UNKNOWN,
                    reason_code="worker_unavailable",
                )
            return RuntimeActionOutcome(
                adapter_operation_id=adapter_operation_id,
                delivery_epoch=delivery_epoch,
                status=ActionOutcomeStatus.ACCEPTED,
            )

        return self._perform_action(
            identity=("control.cancel", session_ref, run_id, attempt_id),
            adapter_operation_id=adapter_operation_id,
            delivery_epoch=delivery_epoch,
            effect=effect,
        )

    def steer(
        self,
        run_id: str,
        attempt_id: str,
        text: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str,
    ) -> RuntimeActionOutcome:
        # The closed method set grew by exactly the four turn methods the design
        # named. Steering is not one of them, and inventing a fifth here would
        # be S3.3 deciding a wire contract by implementation.
        def effect() -> RuntimeActionOutcome:
            return RuntimeActionOutcome(
                adapter_operation_id=adapter_operation_id,
                delivery_epoch=delivery_epoch,
                status=ActionOutcomeStatus.REJECTED,
                reason_code="steer_unsupported_by_managed_runtime",
            )

        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self._perform_action(
            identity=("control.steer", session_ref, run_id, attempt_id, text_digest),
            adapter_operation_id=adapter_operation_id,
            delivery_epoch=delivery_epoch,
            effect=effect,
        )

    def resolve_decision(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        choice: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome:
        def effect() -> RuntimeActionOutcome:
            # ⟦S3.4⟧ Validated before anything is written to the channel. A
            # choice outside the closed set is refused here and never becomes a
            # frame: the fork's `session` and `always` grant standing permission
            # across the rest of the session, and the only safe place to refuse
            # them is upstream of the wire.
            if not is_approval_choice(choice):
                return RuntimeActionOutcome(
                    adapter_operation_id=adapter_operation_id,
                    delivery_epoch=delivery_epoch,
                    status=ActionOutcomeStatus.REJECTED,
                    reason_code="invalid_approval_choice",
                )
            with self._lock:
                execution = self._executions.get((run_id, attempt_id))
                operation_id = execution.operation_id if execution else ""
            if not operation_id:
                return RuntimeActionOutcome(
                    adapter_operation_id=adapter_operation_id,
                    delivery_epoch=delivery_epoch,
                    status=ActionOutcomeStatus.REJECTED,
                    reason_code="no_active_turn",
                )
            try:
                delivered = self._require_supervisor().resolve_turn(
                    operation_id, {"decision_id": decision_id, "choice": choice}
                )
            except WorkerProtocolError as exc:
                return RuntimeActionOutcome(
                    adapter_operation_id=adapter_operation_id,
                    delivery_epoch=delivery_epoch,
                    status=ActionOutcomeStatus.REJECTED,
                    reason_code=str(exc),
                )
            except (WorkerChannelError, ManagedRuntimeUnavailable):
                return RuntimeActionOutcome(
                    adapter_operation_id=adapter_operation_id,
                    delivery_epoch=delivery_epoch,
                    status=ActionOutcomeStatus.UNKNOWN,
                    reason_code="worker_unavailable",
                )
            if not delivered:
                # The turn is open, but nothing on it ever asked about this
                # decision id. Recording ACCEPTED here would tell Control an
                # approval had been applied that reached no gate.
                return RuntimeActionOutcome(
                    adapter_operation_id=adapter_operation_id,
                    delivery_epoch=delivery_epoch,
                    status=ActionOutcomeStatus.REJECTED,
                    reason_code="decision_not_pending",
                )
            return RuntimeActionOutcome(
                adapter_operation_id=adapter_operation_id,
                delivery_epoch=delivery_epoch,
                status=ActionOutcomeStatus.ACCEPTED,
            )

        return self._perform_action(
            identity=(
                "decision.resolve",
                session_ref,
                run_id,
                attempt_id,
                decision_id,
                choice,
            ),
            adapter_operation_id=adapter_operation_id,
            delivery_epoch=delivery_epoch,
            effect=effect,
        )

    def query_action_outcome(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome:
        with self._lock:
            previous = self._action_operations.get(adapter_operation_id)
        # The native backend's query, term for term. Matching on an *exact*
        # `delivery_epoch` read UNKNOWN for an action that had been applied, the
        # moment a redelivery arrived at a higher epoch.
        if previous is None:
            return RuntimeActionOutcome(
                adapter_operation_id,
                delivery_epoch,
                ActionOutcomeStatus.UNKNOWN,
                "operation_outcome_unknown",
            )
        if len(previous.identity) < 4 or previous.identity[1:4] != (
            session_ref,
            run_id,
            attempt_id,
        ):
            return RuntimeActionOutcome(
                adapter_operation_id,
                delivery_epoch,
                ActionOutcomeStatus.REJECTED,
                "adapter_operation_conflict",
            )
        if delivery_epoch < previous.delivery_epoch:
            return RuntimeActionOutcome(
                adapter_operation_id,
                delivery_epoch,
                ActionOutcomeStatus.REJECTED,
                "stale_delivery_epoch",
            )
        return previous.outcome


def _stable_id(kind: str, payload: Mapping[str, Any]) -> str | None:
    """Reconstruct the native backend's stable event ids on this side.

    They are a pure function of the payload, so sending them across the wire
    would add a field whose only property is that it could disagree with the
    payload beside it.
    """

    if kind in {"tool.started", "tool.completed"}:
        suffix = "started" if kind.endswith("started") else "completed"
        return f"tool:{payload.get('tool_call_id')}:{suffix}"
    if kind == "decision.required":
        return f"decision:{payload.get('decision_id')}:required"
    return None


def _request_digest(payload: Mapping[str, Any]) -> str:
    from ..product.runtime_update.worker_payload.cortex_worker.digests import (
        digest_document,
    )

    return digest_document(payload)


def recompute_result_digest(events) -> str:
    """Recompute a turn's digest product-side, from the durable stream.

    The same source file the worker used, imported rather than reimplemented.
    Used by replay checks and by S3.5's harness; never by a per-turn gate.
    """

    return result_digest(events)


# ---------------------------------------------------------------------------
# Replaying the digest out of `control.db`.
#
# ⟦AMD-1⟧ says the durable event stream in Control IS the result, so the
# authoritative recompute reads the stored rows rather than the adapter's
# in-memory stream. Control does not store the RuntimeEvent type verbatim:
# transitions are typed `run.<state>` and a decision is typed `decision.required`
# — so the mapping is stated here, once, where it can be checked, rather than
# assumed by whoever writes the next replay check.
# ---------------------------------------------------------------------------

CONTROL_EVENT_TYPES: Mapping[str, str] = {
    "run.running": "runtime.run.started",
    "runtime.tool.started": "runtime.tool.started",
    "runtime.tool.completed": "runtime.tool.completed",
    "runtime.session_rebound": "runtime.session_rebound",
    "runtime.message.completed": "runtime.message.completed",
    "decision.required": "runtime.decision.required",
    "run.completed": "runtime.run.completed",
    "run.failed": "runtime.run.failed",
    "run.canceled": "runtime.run.canceled",
}


def control_durable_stream(
    events,
    *,
    attempt_id: str,
    message_content: Callable[[str], str],
) -> list[tuple[str, Mapping[str, Any]]]:
    """The turn's durable stream, read back out of `run_events`.

    `runtime.message.completed` is the one row whose projected field is not in
    the row: Control stores a minted `message_id` and puts the text in
    `messages`, so the caller supplies the join. Everything else is inline.
    """

    stream: list[tuple[str, Mapping[str, Any]]] = []
    for event in events:
        if str(event.get("attempt_id") or "") != attempt_id:
            continue
        projected_type = CONTROL_EVENT_TYPES.get(str(event.get("type")))
        if projected_type is None:
            continue
        payload = dict(event.get("payload") or {})
        if projected_type == "runtime.message.completed":
            payload = {"content": message_content(str(payload.get("message_id", "")))}
        stream.append((projected_type, payload))
    return stream


def load_managed_backend(
    descriptor_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> ManagedHermesBackend:
    return ManagedHermesBackend(descriptor_path, environment=environment)
