"""D5: drain the gen-8 capture inbox through the engine boundary.

Two nested fences. The capture lease is the outer one and the
`workflow_effect_commands` row is the inner durable journal, and they are
independent clocks over independent transactions -- which is why ⟦AMD-3⟧ had to
relate them explicitly: the capture lease must outlive the effect lease plus
the child's hard timeout, or a capture expires into `uncertain` while its own
effect is still running.

⟦AMD-2⟧ settles what carries the effect. `workflow_effect_commands.workflow_id`
is NOT NULL and the dispatch query joins runs and filters terminal ones, so an
effect with no open run is invisible to the dispatcher. The carrier is a sealed
`research.capture` definition -- one `engine_mutation` stage, one
`source_import` receipt, and deliberately no `resolve_sources` /
`await_source_decision`, because a capture performs no resolution and the
run-scoped intent chain is exactly what gen 8's S1-V1 adjudication forbids for
one. The run is opened for the effect and driven terminal when it terminates.

Nothing is ever retried. A consumer that cannot prove it did not perform the
side effect records `uncertain` and waits for the operator, because a duplicate
ingest mints a duplicate `paper_dir`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from cortex_platform.product.control import (
    CAPTURE_CONSUMER_ACTOR,
    CAPTURE_CONSUMER_WORKSPACE_TITLE,
    CAPTURE_THREAD_TITLE_PREFIX,
    CAPTURE_WORKFLOW_DEFINITION_ID,
)
from cortex_platform.product.control.errors import InvalidTransition, NotFound
from cortex_platform.product.sources.adoption import decode_engine_ref
from cortex_platform.product.workflows.coordinator import (
    EffectOutcomeUnknown,
    EffectPermanentlyRejected,
    workflow_input_hash,
)
from cortex_platform.product.workflows.models import (
    EffectReconciliationRequest,
    SourceImportRequest,
    SourceImportResult,
    StageDefinition,
    WorkflowDefinition,
)

from .port import EnginePayload, ImportOutcome, ProductResearchEngine

# ⟦AMD-11⟧ One module constant, asserted by a unit test. gen 8 R-2: the consumer
# commits under its own machine actor and never impersonates `local-operator`.
# ⟦V-R4⟧ Spelled once, in the store: the same string is what
# `ControlStore._MACHINE_THREAD_PREDICATE` reads off this consumer's
# `create_thread` receipts to know a thread is its own.
MACHINE_ACTOR = CAPTURE_CONSUMER_ACTOR
WORKER_ID = "p4-capture-consumer"

#: ⟦V6-2⟧ One definition, in the store: the same two strings are what
#: `ControlStore._MACHINE_THREAD_PREDICATE` uses to recognise this consumer's
#: threads, so the consumer's notion of its own thread and the store's cannot
#: drift apart.
WORKSPACE_TITLE = CAPTURE_CONSUMER_WORKSPACE_TITLE
STAGE_KEY = "import_capture"

# ⟦AMD-2⟧ The sealed carrier: one engine_mutation stage requiring exactly one
# source_import receipt, and no resolution stage of any kind.
RESEARCH_CAPTURE_WORKFLOW = WorkflowDefinition(
    definition_id=CAPTURE_WORKFLOW_DEFINITION_ID,
    version=1,
    stages=(
        StageDefinition(
            key=STAGE_KEY,
            effect="engine_mutation",
            dependencies=(),
            required_receipts=("source_import",),
        ),
    ),
)

# The frozen store allowlist (`_CAPTURE_FAILURES`, `control/store.py:147`)
# plus the one translation
# ⟦AMD-7⟧ requires: the gate refusal has no capture category of its own, and
# gen 8 R-1 lets an `adapter_unavailable` capture be re-captured.
_CATEGORY_MAP: Mapping[str, str] = {
    "runtime_activation_disabled": "adapter_unavailable",
    "adapter_unavailable": "adapter_unavailable",
    "invalid_source": "invalid_source",
    "materialization_failed": "materialization_failed",
}
_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "canceled"})

_log = logging.getLogger(__name__)


class ForeignCarrierRun(RuntimeError):
    """The capture's thread carries an active run this consumer did not create.

    ⟦V6-3⟧ Installing the capture's workflow onto it would take that run away
    from whoever opened it (it stops being a conversation run, and nothing
    ends it), and the one-active-run-per-thread index forbids opening a
    second beside it. So the capture is set aside for the operator: closed
    `uncertain`, the one state `reopen_capture` accepts, so that once the
    foreign run has ended the operator's acknowledged reopen is what asks
    again. Not left claimed -- an abandoned claim also ends `uncertain`, but
    only at lease expiry, by a path that says the consumer was lost, and
    while blocking every capture behind it -- and not `failed`, which is
    terminal and says something was wrong with the capture. ⟦V-R3⟧ Under
    its own category, `carrier_thread_busy`, with the run named on the
    capture's audit row: a reopen that does not end the run gets the same
    typed answer, never the half-run-effect word `outcome_unknown`.
    """

    def __init__(self, capture_id: str, run_id: str) -> None:
        super().__init__(f"capture {capture_id}: foreign active run {run_id}")
        self.capture_id = capture_id
        self.run_id = run_id


class ForeignCarrierThread(RuntimeError):
    """The thread the capture's carrier would use was created by somebody else.

    ⟦ADJ-2⟧ `_thread` finds the carrier thread by title in the consumer's
    workspace, and a thread there whose `create_thread` receipt does not name
    this consumer is not its own: adopting it would make it a machine thread
    for ever (`thread_is_machine`'s second half, the moment a workflow is
    installed on a run there) and would put the consumer's captures on a
    thread whose receipt says an operator created it -- the universal the
    receipt half of that predicate rests on. So, like `ForeignCarrierRun`,
    the capture is set aside `uncertain` (reopenable, and dismissable) with
    its own category naming the thread. Reaching this takes a thread titled
    exactly `capture <capture id>` created by another actor in the engine's
    workspace; no rename path exists, so the operator's way out is to
    dismiss the capture and capture the payload again.
    """

    def __init__(self, capture_id: str, thread_id: str) -> None:
        super().__init__(f"capture {capture_id}: foreign carrier thread {thread_id}")
        self.capture_id = capture_id
        self.thread_id = thread_id


@dataclass(frozen=True)
class LeasePlan:
    """⟦AMD-3⟧ the deadline relation between the two independent clocks.

    One ingest has to finish inside one capture lease. Overrunning it means the
    child is killed and the outcome is unknown, so the outer lease is sized to
    outlast the inner one plus the whole hard timeout rather than to race it.
    """

    child_timeout_seconds: int = 600
    effect_lease_seconds: int = 900
    capture_lease_seconds: int = 1_800

    def __post_init__(self) -> None:
        for value in (
            self.child_timeout_seconds,
            self.effect_lease_seconds,
            self.capture_lease_seconds,
        ):
            if type(value) is not int or not 1 <= value <= 3_600:
                raise ValueError("every lease must be between 1 and 3600 seconds")
        if self.effect_lease_seconds <= self.child_timeout_seconds:
            raise ValueError("the effect lease must outlast the child hard timeout")
        if (
            self.capture_lease_seconds
            < self.effect_lease_seconds + self.child_timeout_seconds
        ):
            raise ValueError(
                "the capture lease must cover the effect lease plus the child margin"
            )


@dataclass(frozen=True)
class CaptureOutcome:
    """What one drain did, in the terms the inbox records."""

    capture_id: str
    state: str
    failure_category: str | None = None
    consumed_source_ids: tuple[str, ...] = ()
    effect_id: str | None = None
    manifest_id: str | None = None
    resumed: bool = False
    #: ⟦ADJ-2 / V-R3⟧ The run or thread that held the carrier when the
    #: capture was set aside, so the caller can name it without reading the
    #: audit trail.
    blocked_by: str | None = None
    healed: bool = False


def effect_key_for(capture_id: str) -> str:
    """⟦AMD-3⟧ the key a resumed consumer looks the effect up by."""

    return f"capture:{capture_id}"


def _idempotency_key(*parts: str) -> str:
    """A store-legal idempotency key derived from stable parts.

    `_KEY_RE` (`control/store.py:118`) admits 16 to 128 URL-safe characters, so
    a readable prefix is kept and anything that could carry an illegal
    character is folded into a digest rather than trusted.
    """

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"p42-{parts[0]}-{digest}"


class CaptureConsumer:
    """Claim one approved capture, ingest it, and record what happened."""

    def __init__(
        self,
        *,
        store: Any,
        engine: ProductResearchEngine,
        leases: LeasePlan | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._leases = leases or LeasePlan()

    # -- payload resolution -------------------------------------------------

    def payload_for(self, source_id: str) -> EnginePayload:
        """Resolve `capture-<id>` back to the raw payload the operator staged.

        The store never cross-checks the placeholder, so this is the only place
        it means anything -- and the payload is used exactly as captured, since
        capture performs no resolution.
        """

        from .port import capture_id_from_source

        capture_id = capture_id_from_source(source_id)
        if capture_id is None:
            raise LookupError(source_id)
        try:
            capture = self._store.get_capture(capture_id)
        except NotFound as error:
            raise LookupError(source_id) from error
        payload = str(capture["payload"]).strip()
        kind = "arxiv" if _looks_like_arxiv(payload) else str(capture["kind"])
        return EnginePayload(kind=kind, identifier=payload)

    # -- the loop -----------------------------------------------------------

    def run_once(self) -> CaptureOutcome | None:
        """Drain at most one approved capture. Returns None when there is none."""

        # ⟦AMD-7⟧ Read the gate BEFORE claiming. The per-effect check inside the
        # supervisor stays the authority; this only shrinks the window in which
        # a claim straddles an expiring window, and it means a capture is not
        # consumed by a dispatch that was never going to be allowed.
        if not self._store.runtime_dispatch_enabled():
            raise EffectPermanentlyRejected("runtime_activation_disabled")
        pending = self._store.list_pending_captures(limit=1)
        if not pending:
            return None
        capture = pending[0]
        capture_id = str(capture["id"])
        claim = self._store.claim_capture(
            capture_id=capture_id,
            worker_id=WORKER_ID,
            lease_seconds=self._leases.capture_lease_seconds,
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key(
                "claim", capture_id, str(capture["claim_epoch"])
            ),
        )
        epoch = int(claim.value["claim_epoch"])
        try:
            return self._consume(capture_id, epoch)
        except EffectPermanentlyRejected as rejection:
            return self._fail(capture_id, epoch, rejection.category)
        except EffectOutcomeUnknown:
            return self._uncertain(capture_id, epoch)
        except ForeignCarrierRun as foreign:
            # ⟦V6-3⟧ Nothing ran and nothing was installed. The capture is
            # closed `uncertain` -- reopenable once the operator has ended the
            # run that is holding its thread -- ⟦V-R3⟧ under its own category,
            # with the run named on the capture's audit row.
            _log.warning(
                "capture %s: thread holds foreign active run %s; not adopted, "
                "capture closed uncertain (carrier_thread_busy) until the "
                "operator ends that run and reopens the capture",
                foreign.capture_id,
                foreign.run_id,
            )
            return self._set_aside(
                capture_id,
                epoch,
                category="carrier_thread_busy",
                blocked_by=foreign.run_id,
            )
        except ForeignCarrierThread as foreign:
            # ⟦ADJ-2⟧ Nothing adopted, nothing created. The capture is closed
            # `uncertain` under its own category, naming the thread.
            _log.warning(
                "capture %s: carrier thread %s was created by another actor; "
                "not adopted, capture closed uncertain (carrier_thread_foreign)",
                foreign.capture_id,
                foreign.thread_id,
            )
            return self._set_aside(
                capture_id,
                epoch,
                category="carrier_thread_foreign",
                blocked_by=foreign.thread_id,
            )

    def heal(self) -> None:
        """⟦AMD-5⟧ the first step after an operator reopens an uncertain capture.

        A copy stranded by a killed child keeps a non-empty write-ahead log, and
        the adoption reader is right to refuse it. The repair is a checkpoint in
        a child; cortexd never opens `research.db` to write.
        """

        self._engine.checkpoint()

    # -- consuming one capture ---------------------------------------------

    def _consume(self, capture_id: str, epoch: int) -> CaptureOutcome:
        healed = False
        # ⟦AMD-3⟧ Resume before create, and look for the effect across every
        # carrier run this capture has had: a capture reopened after its first
        # run went terminal gets a second run, and the completed effect it must
        # replay hangs off the first one.
        found = self._existing_effect(capture_id)
        if found is None:
            workflow, stage = self._carrier(capture_id)
            effect = self._create_effect(workflow, stage, capture_id)
        else:
            workflow, stage, effect = found
        state = str(effect["state"])

        # A completed effect is replayed into the capture, never re-ingested.
        if state == "completed":
            return self._replay_completed(capture_id, epoch, effect, resumed=True)
        if state == "failed":
            return self._fail(
                capture_id, epoch, str(effect["failure_category"]), effect_id=effect["id"]
            )
        if state in {"outcome_unknown", "reconciling"}:
            self.heal()
            healed = True
            effect = self._reconcile(effect)
            state = str(effect["state"])
            if state == "completed":
                return self._replay_completed(
                    capture_id, epoch, effect, resumed=True, healed=True
                )
            if state == "manual_required":
                return self._uncertain(capture_id, epoch, effect_id=effect["id"])
            if state == "failed":
                return self._fail(
                    capture_id,
                    epoch,
                    str(effect["failure_category"]),
                    effect_id=effect["id"],
                )
        return self._dispatch(capture_id, epoch, workflow, stage, effect, healed=healed)

    def _dispatch(
        self,
        capture_id: str,
        epoch: int,
        workflow: Mapping[str, Any],
        stage: Mapping[str, Any],
        effect: Mapping[str, Any],
        *,
        healed: bool,
    ) -> CaptureOutcome:
        claim = self._store.claim_workflow_effect(
            effect_id=str(effect["id"]),
            worker_id=WORKER_ID,
            lease_seconds=self._leases.effect_lease_seconds,
        )
        if str(claim["state"]) != "claimed":
            # The lease had already expired and the store promoted the row to
            # `outcome_unknown`. It is never redelivered.
            return self._uncertain(capture_id, epoch, effect_id=str(effect["id"]))
        request = claim["request"]
        try:
            result = self._engine.import_source(request)
        except EffectPermanentlyRejected as rejection:
            self._store.reject_workflow_effect(
                effect_id=str(effect["id"]),
                worker_id=WORKER_ID,
                claim_epoch=int(claim["claim_epoch"]),
                delivery_epoch=int(claim["delivery_epoch"]),
                failure_category=rejection.category,
            )
            return self._fail(
                capture_id, epoch, rejection.category, effect_id=str(effect["id"])
            )
        except EffectOutcomeUnknown:
            self._store.mark_workflow_effect_outcome_unknown(
                effect_id=str(effect["id"]),
                worker_id=WORKER_ID,
                claim_epoch=int(claim["claim_epoch"]),
                delivery_epoch=int(claim["delivery_epoch"]),
            )
            return self._uncertain(capture_id, epoch, effect_id=str(effect["id"]))

        self._store.complete_workflow_effect(
            effect_id=str(effect["id"]),
            worker_id=WORKER_ID,
            claim_epoch=int(claim["claim_epoch"]),
            delivery_epoch=int(claim["delivery_epoch"]),
            result=result,
        )
        outcome = self._engine.outcomes[request.operation_id]
        self._record_watch_digests(str(effect["id"]), outcome)
        self._settle(workflow, stage, result)
        completion = self._complete(
            capture_id, epoch, outcome.source_ids, effect_id=str(effect["id"])
        )
        return CaptureOutcome(
            capture_id=capture_id,
            state=completion.state,
            consumed_source_ids=completion.consumed_source_ids,
            effect_id=str(effect["id"]),
            manifest_id=outcome.manifest_id,
            healed=healed,
        )

    def _record_watch_digests(self, effect_id: str, outcome: ImportOutcome) -> None:
        """⟦AMD-12⟧ Land D4(4)'s digest pair beside the effect that produced it.

        The pair rides home in `ImportOutcome.execution`, so `import_source` no
        longer drops it -- this is the one place that knows which effect command
        row it belongs to.
        """

        pair = outcome.execution.gdrive
        if not pair:
            return
        self._store.record_effect_watch_digests(effect_id=effect_id, digests=pair)

    def _reconcile(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        claim = self._store.claim_workflow_effect_reconciliation(
            effect_id=str(effect["id"]),
            worker_id=WORKER_ID,
            lease_seconds=self._leases.effect_lease_seconds,
        )
        request: EffectReconciliationRequest = claim["reconciliation_request"]
        result = self._engine.reconcile_effect(request)
        return self._store.complete_workflow_effect_reconciliation(
            effect_id=str(effect["id"]),
            worker_id=WORKER_ID,
            claim_epoch=int(claim["claim_epoch"]),
            delivery_epoch=int(claim["delivery_epoch"]),
            result=result,
        )

    def _replay_completed(
        self,
        capture_id: str,
        epoch: int,
        effect: Mapping[str, Any],
        *,
        resumed: bool,
        healed: bool = False,
    ) -> CaptureOutcome:
        """The split terminal state: the effect committed, the capture did not.

        Enumerated rather than discovered. The operator reopens the capture, the
        next claim finds a completed effect, and only `complete_capture` is
        replayed -- nothing is ingested a second time.
        """

        receipt = effect["receipt"] or {}
        result = SourceImportResult.from_dict(receipt)
        paper_dir = decode_engine_ref(result.engine_reference.value)
        source_ids = tuple(
            str(source["id"])
            for source in self._store.list_sources()
            if str(source["engine_ref"] or "") == result.engine_reference.value
        )
        if not source_ids:
            # The effect completed but no source carries its directory: the
            # adoption commit is missing, which is not something to guess about.
            return self._uncertain(capture_id, epoch, effect_id=str(effect["id"]))
        outcome = self._complete(
            capture_id, epoch, source_ids, effect_id=str(effect["id"])
        )
        return CaptureOutcome(
            capture_id=capture_id,
            state=outcome.state,
            consumed_source_ids=outcome.consumed_source_ids,
            effect_id=str(effect["id"]),
            resumed=resumed,
            healed=healed,
        )

    # -- terminal writes ----------------------------------------------------

    def _complete(
        self,
        capture_id: str,
        epoch: int,
        source_ids: Sequence[str],
        *,
        effect_id: str | None = None,
    ) -> CaptureOutcome:
        self._store.complete_capture(
            capture_id=capture_id,
            claim_owner=WORKER_ID,
            claim_epoch=epoch,
            outcome="consumed",
            consumed_source_ids=list(source_ids),
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("consumed", capture_id, str(epoch)),
        )
        return CaptureOutcome(
            capture_id=capture_id,
            state="consumed",
            consumed_source_ids=tuple(source_ids),
            effect_id=effect_id,
        )

    def _fail(
        self,
        capture_id: str,
        epoch: int,
        category: str,
        *,
        effect_id: str | None = None,
    ) -> CaptureOutcome:
        mapped = _CATEGORY_MAP.get(category, "materialization_failed")
        self._store.complete_capture(
            capture_id=capture_id,
            claim_owner=WORKER_ID,
            claim_epoch=epoch,
            outcome="failed",
            failure_category=mapped,
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("failed", capture_id, str(epoch)),
        )
        return CaptureOutcome(
            capture_id=capture_id,
            state="failed",
            failure_category=mapped,
            effect_id=effect_id,
        )

    def _uncertain(
        self, capture_id: str, epoch: int, *, effect_id: str | None = None
    ) -> CaptureOutcome:
        self._store.complete_capture(
            capture_id=capture_id,
            claim_owner=WORKER_ID,
            claim_epoch=epoch,
            outcome="uncertain",
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("uncertain", capture_id, str(epoch)),
        )
        return CaptureOutcome(
            capture_id=capture_id,
            state="uncertain",
            failure_category="outcome_unknown",
            effect_id=effect_id,
        )

    def _set_aside(
        self, capture_id: str, epoch: int, *, category: str, blocked_by: str
    ) -> CaptureOutcome:
        """Close the capture `uncertain` under a category that names its blocker.

        ⟦ADJ-2 / V-R3⟧ The same terminal `_uncertain` uses -- reopenable, the
        claim released -- but the row says WHY (the category) and the audit
        row says WHAT (`blocked_by`), so the cockpit does not show the
        half-run-effect word for a capture nothing ran for.
        """

        self._store.complete_capture(
            capture_id=capture_id,
            claim_owner=WORKER_ID,
            claim_epoch=epoch,
            outcome="uncertain",
            failure_category=category,
            detail={"blocked_by": blocked_by},
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("aside", capture_id, str(epoch)),
        )
        return CaptureOutcome(
            capture_id=capture_id,
            state="uncertain",
            failure_category=category,
            blocked_by=blocked_by,
        )

    # -- the carrier --------------------------------------------------------

    def _workspace(self) -> Mapping[str, Any]:
        for workspace in self._store.list_workspaces():
            if workspace["title"] == WORKSPACE_TITLE:
                return workspace
        return self._store.create_workspace(
            title=WORKSPACE_TITLE,
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("workspace", WORKSPACE_TITLE),
        ).value

    def _thread(self, capture_id: str) -> Mapping[str, Any]:
        workspace = self._workspace()
        title = f"{CAPTURE_THREAD_TITLE_PREFIX}{capture_id}"
        foreign: str | None = None
        for thread in self._store.list_threads(workspace_id=str(workspace["id"])):
            if thread["title"] != title:
                continue
            # ⟦ADJ-2⟧ Adopt only a thread THIS consumer created -- its
            # `create_thread` receipt names the machine actor -- which is the
            # thread a gen-12 consumer left. Any other thread under this
            # title is somebody else's (`ForeignCarrierThread`); no receipt
            # is filed for it, and none is created beside it.
            if self._store.thread_creator(str(thread["id"])) == MACHINE_ACTOR:
                return self._store.get_thread(str(thread["id"]))
            foreign = foreign or str(thread["id"])
        if foreign is not None:
            raise ForeignCarrierThread(capture_id, foreign)
        return self._store.create_thread(
            workspace_id=str(workspace["id"]),
            title=title,
            expected_revision=int(workspace["revision"]),
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("thread", capture_id),
        ).value

    def _carrier(
        self, capture_id: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """One workspace, one thread per capture, one run bound to the effect."""

        thread = self._thread(capture_id)
        run_id = thread.get("active_run_id")
        run = self._store.get_run(str(run_id)) if run_id else None
        if run is None or str(run["state"]) in _TERMINAL_RUN_STATES:
            run = self._store.create_run(
                thread_id=str(thread["id"]),
                expected_revision=int(thread["revision"]),
                actor_id=MACHINE_ACTOR,
                # The thread revision is part of the key: a capture that was
                # reopened after its first carrier run went terminal opens a
                # second one, and a receipt keyed only by capture id would
                # collide with the first request rather than replay it.
                idempotency_key=_idempotency_key(
                    "run", capture_id, str(thread["revision"])
                ),
                # ⟦P8 V-3⟧ One transaction, not two: the carrier owns its
                # workflow from the instant it exists, so there is no gap in
                # which the turn bridge could take it for a conversation run
                # (an operator may have typed into this thread; the API keeps
                # such a message) and drive a Hermes turn on a machine thread.
                workflow=RESEARCH_CAPTURE_WORKFLOW,
            ).value
        try:
            workflow = self._store.get_workflow_for_run(str(run["id"]))
        except NotFound:
            # ⟦V6-3⟧ A workflow-less active run on this thread is adopted only
            # when THIS consumer created it -- its `create_run` receipt carries
            # the machine actor -- which is the carrier a pre-V-3 consumer
            # left between its two transactions. Any other run (an operator's,
            # opened here before the API refused that) is foreign: adopting
            # it would take the run away from them, so the capture is set
            # aside instead (`ForeignCarrierRun`) until that run has ended.
            if self._store.run_creator(str(run["id"])) != MACHINE_ACTOR:
                raise ForeignCarrierRun(capture_id, str(run["id"]))
            workflow = self._store.install_workflow(
                run_id=str(run["id"]), definition=RESEARCH_CAPTURE_WORKFLOW
            )
        stage = next(
            item for item in workflow["stages"] if item["stage_key"] == STAGE_KEY
        )
        if str(stage["state"]) == "ready":
            activated = self._store.activate_workflow_stage(
                workflow_id=str(workflow["id"]),
                stage_key=STAGE_KEY,
                input_value={"capture_id": capture_id},
                expected_workflow_revision=int(workflow["revision"]),
                expected_stage_revision=int(stage["revision"]),
            )
            workflow, stage = activated["workflow"], activated["stage"]
        return workflow, stage

    def _existing_effect(
        self, capture_id: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]] | None:
        """Find this capture's effect on any carrier run it has ever had."""

        key = effect_key_for(capture_id)
        thread = self._thread(capture_id)
        for run in self._store.list_thread_runs(thread_id=str(thread["id"])):
            try:
                workflow = self._store.get_workflow_for_run(str(run["id"]))
            except NotFound:
                continue
            for effect in self._store.list_workflow_effects(
                workflow_id=str(workflow["id"]), stage_key=STAGE_KEY
            ):
                if str(effect["effect_key"]) != key:
                    continue
                stage = next(
                    item
                    for item in workflow["stages"]
                    if item["stage_key"] == STAGE_KEY
                )
                return workflow, stage, effect
        return None

    def _create_effect(
        self,
        workflow: Mapping[str, Any],
        stage: Mapping[str, Any],
        capture_id: str,
    ) -> Mapping[str, Any]:
        key = effect_key_for(capture_id)
        capture = self._store.get_capture(capture_id)
        digest = hashlib.sha256(str(capture["payload"]).encode("utf-8")).hexdigest()
        request = SourceImportRequest(
            operation_id=f"capture.import.{capture_id}",
            delivery_epoch=1,
            # ⟦AMD-2⟧ a placeholder the store never cross-checks against
            # `sources`, and a content digest of bytes already in hand -- not
            # the resolution §6 refuses. Real identity appears at the manifest.
            source_id=f"capture-{capture_id}",
            canonical_id=f"sha256:{digest}",
        )
        return self._store.create_workflow_effect(
            workflow_id=str(workflow["id"]),
            stage_key=STAGE_KEY,
            effect_key=key,
            request=request,
            expected_workflow_revision=int(workflow["revision"]),
            expected_stage_revision=int(stage["revision"]),
            expected_stage_input_hash=str(stage["input_hash"]),
        )

    def _settle(
        self,
        workflow: Mapping[str, Any],
        stage: Mapping[str, Any],
        result: SourceImportResult,
    ) -> None:
        """Complete the stage, then drive the carrier run terminal."""

        workflow = self._store.get_workflow(str(workflow["id"]))
        current = next(
            item for item in workflow["stages"] if item["stage_key"] == STAGE_KEY
        )
        self._store.complete_workflow_stage(
            workflow_id=str(workflow["id"]),
            stage_key=STAGE_KEY,
            expected_workflow_revision=int(workflow["revision"]),
            expected_stage_revision=int(current["revision"]),
            references=[
                {
                    "kind": "engine_source",
                    "id": result.engine_reference.value,
                    "metadata": {},
                }
            ],
        )
        self._close_run(str(workflow["run_id"]))

    def _close_run(self, run_id: str) -> None:
        """Drive an unbound carrier run terminal.

        A carrier run never acquires a runtime binding, and `transition_run`
        demands a runtime identity for every terminal target, so the only
        honest terminal available is the unbound one: request cancellation,
        then settle it. A rejected effect settles its own run
        (`_settle_workflow_failure`), so this is the success path only.
        """

        run = self._store.get_run(run_id)
        if str(run["state"]) in _TERMINAL_RUN_STATES:
            return
        run = self._store.transition_run(
            run_id=run_id,
            target_state="cancel_requested",
            expected_revision=int(run["revision"]),
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("close", run_id),
        ).value
        self._store.fail_unbound_run(
            run_id=run_id,
            attempt_id=str(run["active_attempt_id"]),
            expected_revision=int(run["revision"]),
            category="capture_effect_settled",
            actor_id=MACHINE_ACTOR,
            idempotency_key=_idempotency_key("settle", run_id),
        )


def _looks_like_arxiv(payload: str) -> bool:
    """Whether a captured payload names an arXiv paper.

    Deliberately syntactic. Deciding what a URL resolves to is resolution, and
    resolution happens after the operator's approval, not inside a dedup key.
    """

    import re

    text = payload.strip()
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", text):
        return True
    return bool(re.search(r"arxiv\.org/(abs|pdf|html)/\d{4}\.\d{4,5}", text))
