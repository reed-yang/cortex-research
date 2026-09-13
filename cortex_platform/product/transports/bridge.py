"""The inbound → turn bridge: a message becomes a run, a run becomes a reply.

⟦P5.4c⟧ Everything on both sides of this module already existed and was proven.
`TelegramAdapter._handle_message` appends the operator's message to the bound
thread; `RunOrchestrator.dispatch` runs one attempt against a `RuntimePort`;
`TransportDeliveryDrain` sends whatever committed notification events the
ledger holds. What was missing was the sentence joining them, and it is a
product decision rather than a wire: which run a message belongs to, when a
turn may start at all, and what a turn that cannot finish is called.

Three rules, and none of them may be inferred from another:

1. **Two gates, two decisions.** The transport window (migration 15) says the
   product holds the bot token. The activation gate (migration 12) says the
   orchestrator may dispatch. An open window does not enable dispatch and never
   will: this module reads the second gate itself, before it creates anything,
   and refuses with the same category `RunOrchestrator` would have raised.
2. **One run per scope.** A Telegram scope binds one thread, and a thread has
   one active run; a message that arrives while that run is live is a follow-up
   to it, not a second run. The ledger enforces the same thing with a partial
   unique index, so disagreeing here would only move the error.
3. **A turn that cannot finish ends typed.** Approval vocabulary is a further
   slice, so a turn that parks on a decision is not answered by this bridge --
   it is recorded as `decision_required`, the decision card the projection
   already renders is what reaches the operator, and the turn is abandoned
   rather than held open until the worker's own liveness timeout.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ...runtime.models import failure_detail
from datetime import datetime, timedelta, timezone

from ..control import (
    CANCELED_BEFORE_BINDING,
    DISPATCHABLE_RUN_STATES,
    ControlStore,
    ControlStoreError,
)
from .managed_worker import (
    REFUSED_GATE_CLOSED,
    ManagedTransportWorker,
    ManagedWorkerUnavailable,
)

#: ⟦P9⟧ One INFO line per decision -- a refusal, a failed or cancelled turn,
#: a sweep or re-drive decision -- naming ids and categories only. The
#: daemon's root handler formats every record through `RedactingFormatter`.
_log = logging.getLogger(__name__)

#: The migration-12 refusal, spelled exactly as `RunOrchestrator` spells it.
#: One vocabulary for one decision, whether the refusal was reached here or
#: three layers down.
REFUSED_DISPATCH_DISABLED = "runtime_activation_disabled"
#: The thread a message was appended to has no transport binding any more, or
#: the run it would join cannot take one.
REFUSED_QUEUE_FULL = "turn_queue_full"
#: ⟦P8⟧ The thread's active run is not one this bridge may drive: it belongs to
#: the research engine (it owns a workflow instance) or its thread is neither
#: an operator's conversation nor a transport scope. `ControlStore.
#: run_is_conversation` is the one predicate, asked at every entry.
REFUSED_NOT_CONVERSATION = "not_a_conversation_run"
#: ⟦P8 V-1⟧ A handover -- the sweep's or the re-drive's -- named a run that is
#: no longer its thread's active run: the operator cancelled it, or a later
#: turn replaced it. A handover exists to drive an EXISTING run and never
#: starts one, so the thread is refused with this rather than given a new run.
REFUSED_RUN_NOT_ACTIVE = "run_not_active"
#: ⟦P5.4d / c4⟧ What an attempt a dead daemon left `running` is called when the
#: transport window is shut. Recovery deliberately does NOT launch the release
#: outside a window -- a worker acquired there holds no bot token and could not
#: deliver the turn's reply anyway -- so the attempt ends typed with this
#: category and the recovery is deferred until a window opens. `retryable` is
#: true on the event because that is exactly what happens next.
RECOVERY_DEFERRED = "recovery_deferred"
#: ⟦F-B5⟧ What a turn that hit `TURN_TIMEOUT_SECONDS` is called. The bridge is
#: the only place that knows the deadline was what ended it -- the cancelled
#: task raises `CancelledError`, which is a `BaseException` that
#: `RunOrchestrator.dispatch`'s `except Exception` does not see, so no terminal
#: event was ever committed and the run stayed `running` for the life of the
#: daemon with its scope wedged behind `turn_in_flight`.
TURN_TIMEOUT = "turn_timeout"
#: The floor under `_classify`. A failed turn that can name no category at all
#: is still a failure with a name, because `failed / None / None` is what an
#: operator reads as "the product has nothing to say".
UNTERMINATED = "no_terminal_event"
#: ⟦F-B6⟧ What a run this daemon cannot move any further is called when the
#: operator writes again. Approval vocabulary is a later slice, so a parked run
#: has no consumer: `/cancel` moves it to `cancel_requested`, the decision
#: button moves it to `resuming`, and NO restart converges either --
#: `DEFERRABLE_STATES` is `{running, starting}`, so a shut-gate restart records
#: "converged" having converged nothing. Without an exit, one parked turn ends
#: the window's inbound half for that scope permanently.
TURN_ABANDONED = "turn_abandoned"
#: The three states a run can be parked in with nothing coming to move it.
ABANDONABLE_STATES = frozenset(
    {"waiting_for_decision", "cancel_requested", "resuming"}
)
#: ⟦P54D-2⟧ Recovery outcomes that are waiting on an operator rather than on a
#: worker, and therefore do not spend the bounded retry budget.
_UNBUDGETED_RECOVERY_STATES = frozenset({"deferred", "refused"})
#: The states a run can be left in by a daemon that died inside a turn. Both
#: have a bound attempt, which is what makes them different from a run that was
#: only reserved: they end through the runtime transition, not `fail_unbound_run`.
DEFERRABLE_STATES = frozenset({"running", "starting"})
#: The action kinds a live turn's own loop delivers. `decision.resolve` is
#: deliberately not one of them: it belongs to the resume arc, which opens a
#: new turn, not to the turn that parked and was already abandoned here.
CONTROL_ACTION_KINDS = frozenset({"control.cancel", "control.pause"})
#: ⟦P9-4⟧ The action that answers a worker parked on an approval. The store
#: queues it in `resolve_decision`'s own transaction, exactly as it queues a
#: cancel; it had the same missing caller, with a worse consequence -- a
#: cancelled turn at least ends, while an APPROVED turn simply never runs.
RESUME_ACTION_KINDS = frozenset({"decision.resolve"})
#: Which kinds this loop is the right deliverer for, by the state the run is
#: in. `ControlStore.list_pending_runtime_actions` enforces the same pairing in
#: SQL and decides what is deliverable at all; this only says which of its
#: answers belong to the turn being driven here.
#:
#: ⟦P9-3 ADJ-6⟧ This replaced a `CONVERGING_STATES` set that had become
#: unreferenced. The sentence worth keeping from it: the store queues the
#: control action inside the stop transition's OWN transaction, and that
#: transition never reaches the worker -- delivering the queued action is what
#: does. Which is why the keys here are run states rather than action kinds.
DELIVERABLE_ACTIONS = {
    "cancel_requested": CONTROL_ACTION_KINDS,
    "pause_requested": CONTROL_ACTION_KINDS,
    "resuming": RESUME_ACTION_KINDS,
}
#: ⟦P9-3⟧ Where a bound run in each converging state ends when its worker owes
#: it a terminal event and never sends one.
#: ⟦P9-3⟧ What such a run is called. `retryable` is FALSE wherever this is
#: written: the operator asked for this turn to STOP, and `run.failed` with a
#: Retry button for a turn they cancelled is the exact dishonesty ADJ9-1 was
#: accepted to delete. Deliberately not `turn_timeout`, and deliberately not
#: reached by adding these states to `DEFERRABLE_STATES`, which would label it
#: `failed / turn_timeout / retryable: true` and reintroduce that.
CANCELED_RUNTIME_QUIET = "runtime_quiet_after_cancel"
PAUSED_RUNTIME_QUIET = "runtime_quiet_after_pause"
#: ⟦P9-4⟧ What a run stalled in `resuming` is called: the decision was
#: answered and the worker never acted on it. Distinct from the two above
#: because the operator wanted an ANSWER, not a stop.
QUIET_AFTER_RESUME = "runtime_quiet_after_resume"
#: ⟦P9-3 / P9-4⟧ What a run STALLED in a non-terminal state becomes, what it
#: is called, and whether the operator may retry it. Read as one table because
#: the difference between the rows is the whole design:
#:
#: `cancel_requested` and `pause_requested` are the operator asking the turn to
#: STOP. The run ends `canceled` and is NOT retryable, and `run.canceled` is
#: not a notification type -- telling them a turn they stopped had failed, with
#: a Retry button, is the dishonesty this family exists to remove. `paused` is
#: not an option for either: that target demands a checkpoint URI a
#: conversation turn never writes.
#:
#: `resuming` is the opposite request. The operator ANSWERED an approval and
#: asked the turn to continue, so a turn that then never ran is a failure in
#: the ordinary sense: it ends `failed`, it IS retryable, and `run.failed`
#: being a notification type is exactly right here -- they are owed the news
#: that the answer they authorised never came, and a Retry button that works.
#:
#: ⟦batchM 11⟧ Signed off by the coordinator 2026-09-05 as the contract, which
#: supersedes the batchL brief's non-retryable wording: a resume that went
#: quiet may legitimately be re-driven, where a cancel that went quiet may not,
#: because there the operator asked for the run to END.
STALLED_TERMINALS = {
    "cancel_requested": ("canceled", CANCELED_RUNTIME_QUIET, False),
    "pause_requested": ("canceled", PAUSED_RUNTIME_QUIET, False),
    "resuming": ("failed", QUIET_AFTER_RESUME, True),
}
#: The states this loop puts a floor under at all.
STALLED_STATES = frozenset(STALLED_TERMINALS)
#: ⟦P9-3 ADJ-2⟧ Where a run stops moving for good. A candidate that
#: reaches one of these leaves the live sweep's watch list.
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "canceled"})

OUTCOME_ANSWERED = "answered"
OUTCOME_DECISION_REQUIRED = "decision_required"
OUTCOME_IN_FLIGHT = "turn_in_flight"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"
#: ⟦P9⟧ The run ended `canceled` under this turn: the operator's decision, not
#: a failure, so it is counted apart from `failed` and never becomes
#: `last_failure`. The reason is the terminal event's own category.
OUTCOME_CANCELED = "canceled"


@dataclass(frozen=True)
class TurnOutcome:
    """What one submission became. No message body, ever."""

    thread_id: str
    run_id: str | None
    outcome: str
    reason: str | None = None
    #: ⟦P5.4d⟧ The failure's own words, as the terminal event recorded them:
    #: bounded, scrubbed and produced by the adapter, never invented here. The
    #: reason says which category; this says which failure.
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "detail": self.detail,
        }


class _NoRuntime:
    """The runtime port a pin-release-only orchestrator deliberately lacks.

    ⟦P9-3 C2⟧ `RunOrchestrator.deliver_pending_pin_releases`
    (orchestration/service.py:1332) reads the store, the release authority and
    its own actor id, and touches `self.runtime` nowhere -- which is what lets
    a sweep with no worker hand an attempt's pin back. Passing `None` would
    rest that on an invariant in another module with nothing to say so; this
    raises instead, naming the decision, so a delivery that ever grows a
    runtime call fails against this comment rather than against a NoneType.
    The raise is caught by `_release_pins`, so the cost of being wrong is the
    pre-C2 outcome -- the pin waits for the next dispatch -- and not a crash.
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"pin_release_orchestrator_has_no_runtime:{name}")


_NO_RUNTIME = _NoRuntime()


class InboundTurnBridge:
    """Turn inbound messages into Hermes turns on the daemon's own worker."""

    THREAD_NAME = "cortexd-turn-bridge"
    #: A ceiling, not a schedule. The worker has its own liveness timeout and
    #: the orchestrator its own terminal-event rule; this exists so a turn that
    #: satisfies neither cannot hold the bridge for the life of the daemon.
    TURN_TIMEOUT_SECONDS = 900.0
    #: How often the bridge asks Control what the run has become while a turn
    #: streams. Durable state is the only thing that can say "parked on a
    #: decision" before the runtime stream ends, because it never ends.
    STATE_POLL_SECONDS = 0.25
    #: Bounded on purpose: a burst the bridge cannot serve is refused with a
    #: category the operator can read, not queued into memory the daemon has
    #: no plan for.
    QUEUE_LIMIT = 64
    #: ⟦P5.4d⟧ Recovery runs before the loops, which is also before the window
    #: supervisor has reconciled and, after a restart, possibly before the
    #: previous daemon's worker has finished dying. A single try at that
    #: instant is how a daemon restarted mid-turn left its run `running` for
    #: ever: the worker was not launchable yet, `recover()` refused, and
    #: nothing ever asked again. Retried from the loop thread instead --
    #: bounded, so an installation whose gate is simply shut stops asking and
    #: says so rather than relaunching a refusal for the life of the daemon.
    RECOVERY_ATTEMPTS = 10
    RECOVERY_RETRY_SECONDS = 3.0
    #: ⟦P9-3⟧ How long a BOUND run may sit in a converging state owing a
    #: terminal event before this bridge writes one for it. Two orders of
    #: magnitude below `TURN_TIMEOUT_SECONDS` on purpose: what is being waited
    #: for is not an ANSWER -- the operator has refused the answer -- but a
    #: worker's acknowledgement that it has stopped, and a worker that has not
    #: acknowledged in half a minute is one whose turn must end without it.
    #: Configurable for the same reason the turn timeout is.
    #:
    #: ⟦P9-3 BRK-2⟧ Scope, stated exactly because the first version of this
    #: comment overclaimed: this bounds how long the RUN sits in a converging
    #: state, and `_run_turn` writes the terminal before the teardown so that
    #: is true. It does NOT bound the turn SLOT -- the teardown's `await task`
    #: joins the worker uninterruptibly, so a worker that never returns still
    #: holds the drain loop's single turn thread. That wait is pre-existing
    #: structure, byte-identical at 68c93dc, and is carried as a standing item
    #: rather than closed here.
    CANCEL_FLOOR_SECONDS = 30.0
    #: ⟦P9-4⟧ How long a turn is held open while the operator answers an
    #: approval. This is the one number that trades two costs against each
    #: other, so it is written down rather than inferred. Holding costs the
    #: drain loop: `_serve` runs one turn at a time, so every other thread's
    #: turn waits. NOT holding costs the turn itself -- the worker parked in
    #: `TurnContext.await_decision` is only reachable while its execution is
    #: registered, which lasts exactly as long as this dispatch, so a turn
    #: abandoned on the park can never be resumed and the operator's answer
    #: arrives for a worker that is already gone. Three minutes is sized for a
    #: human answering a push notification, and well under the 900 s turn
    #: timeout that still bounds the whole turn.
    #:
    #: ⟦P9-3 ADJ-4⟧ The cost of the holding side is now MEASURED rather than
    #: described, so the next author faces a number instead of re-deriving one.
    #: `_serve` pulls one item and runs `_run_turn` inline, so an unanswered
    #: approval occupies the only turn slot for this whole window: the review
    #: measured an ordinary message on a DIFFERENT thread waiting 6.44 s behind
    #: a 6.0 s hold, tracking it one for one. At the shipped constant that is
    #: up to three minutes for every other thread, and the loop's own cadence
    #: work -- the re-drive and, since ADJ-2, the stalled-state sweep -- waits
    #: with it. The fix shape, if this is taken up, is to hold the parked
    #: dispatch on a side task and give the slot back, waking it when the run
    #: leaves `waiting_for_decision`. Shortening this constant is NOT the fix:
    #: it widens the window ADJ-2 exists to close.
    DECISION_WAIT_SECONDS = 180.0

    def __init__(
        self,
        *,
        store: ControlStore,
        worker: ManagedTransportWorker,
        actor_id: str,
        orchestrator_factory: Callable[[], Any] | None = None,
        turn_timeout: float | None = None,
        cancel_floor: float | None = None,
        decision_wait: float | None = None,
        queue_limit: int | None = None,
    ) -> None:
        self._store = store
        self._worker = worker
        self._actor_id = actor_id
        # ⟦P8⟧ Two forms of the same orchestrator over the same worker. A TURN
        # needs no transport window -- a cockpit-created run answers into its
        # thread and speaks as no bot -- so its factory acquires the worker
        # without one. RECOVERY keeps the window gate (c4): it launches nothing
        # outside a window and defers instead. An injected factory is both,
        # which is what every test of the loop already relies on.
        self._orchestrator_factory = (
            orchestrator_factory
            if orchestrator_factory is not None
            else lambda: self._managed_orchestrator(window_required=False)
        )
        self._recovery_factory = (
            orchestrator_factory
            if orchestrator_factory is not None
            else lambda: self._managed_orchestrator(window_required=True)
        )
        self._turn_timeout = (
            self.TURN_TIMEOUT_SECONDS if turn_timeout is None else float(turn_timeout)
        )
        self._cancel_floor = (
            self.CANCEL_FLOOR_SECONDS if cancel_floor is None else float(cancel_floor)
        )
        self._decision_wait = (
            self.DECISION_WAIT_SECONDS
            if decision_wait is None
            else float(decision_wait)
        )
        #: ⟦P9-4⟧ How many parked workers this process has actually handed an
        #: operator's answer to. Counted apart from `turns` because it is the
        #: number that says the approval path works at all: before this it was
        #: structurally zero.
        self._resolutions = 0
        #: ⟦P9-3 ADJ-2⟧ Runs this loop's own turns left behind, watched by
        #: `_sweep_stalled`. Bounded by what this process actually drove,
        #: never a store-wide projection.
        #:
        #: ⟦P9-3 FV-4⟧ Exactly ONE thing takes a run out of this set: a state
        #: in `TERMINAL_RUN_STATES` (or a run the store no longer has). Not
        #: "until they are terminal", which reads as a promise that every
        #: candidate eventually leaves -- a run parked on an approval nobody
        #: answers has no terminal coming, stays here for the life of the
        #: process and costs one `get_run` per cadence tick. That cost is
        #: deliberate and it is the price of ADJ-2: `waiting_for_decision` is
        #: one operator action away from `resuming` or `cancel_requested`, the
        #: resolve route writes `resuming` and returns with no turn queued
        #: behind it (api/app.py:954-966), and this sweep is then the only
        #: writer that would converge the run. Dropping a candidate for being
        #: "not stalled" therefore un-fixes ADJ-2 rather than bounding the
        #: sweep -- applied literally it fails both live-loop ADJ-2 tests, so
        #: `test_the_watch_list_keeps_a_parked_run_until_it_is_terminal` in
        #: test_turn_bridge.py fences it at the unit level as well.
        self._stalling: set[str] = set()
        #: ⟦P9-3 BRK-4⟧ The last runtime capabilities a turn was told.
        self._runtime_capabilities: Any = None
        #: Each item is the thread to drive and, for a handover of a run that
        #: already exists (the sweep, the re-drive), that run's id: the turn
        #: then drives exactly that run or refuses, and never creates one.
        self._queue: queue.Queue[tuple[str, str | None] | None] = queue.Queue(
            maxsize=self.QUEUE_LIMIT if queue_limit is None else int(queue_limit)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._queued: set[str] = set()
        self._followups: set[str] = set()
        self._turns = 0
        self._outcomes: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()
        self._last: TurnOutcome | None = None
        self._last_failure: TurnOutcome | None = None
        #: What `recover()` did at daemon start, and why if it did nothing.
        self._recovery: dict[str, object] = {
            "state": "not_attempted",
            "reason": None,
            "converged": 0,
            "attempts": 0,
            # ⟦F-B9⟧ `attempts` is the BUDGET spent, and it sits at 0 through
            # an indefinite deferred poll -- which reads as "nothing has been
            # tried". This is how many times recovery was actually asked.
            "asks": 0,
            "deferred": 0,
            "resumed": 0,
        }
        #: Runs ended typed because the window was shut, waiting for one to
        #: open. Process-local on purpose: they are already terminal in the
        #: ledger, so a daemon that exits before the window opens leaves them
        #: typed rather than stranded -- the operator retries, or the next
        #: window's recovery does not.
        self._deferred: set[str] = set()
        self._resumed = 0
        #: ⟦F-B6⟧ Runs this process is dispatching right now. `attempts.
        #: dispatch_owner` is cleared the moment the attempt binds, so it
        #: cannot answer "is somebody driving this"; the bridge is the only
        #: authority that can, and it is the one that has to be asked before
        #: abandoning a run a decision may have resolved seconds ago.
        self._driving: set[str] = set()
        self._abandoned = 0
        self._recovery_asks = 0
        #: ⟦P8⟧ What the sweep of never-driven runs did: a run left `queued`
        #: with nobody driving it is handed to the loop, and one left
        #: `cancel_requested` with nothing to cancel is ended typed. Store-only
        #: and before any worker is asked for. `submitted` counts distinct
        #: runs; `resubmitted` counts the loop handing a run over AGAIN after
        #: the worker refused its turn; `error` says the store could not be
        #: asked, which is different from "nothing to sweep".
        self._swept: set[str] = set()
        self._submitted: set[str] = set()
        #: The runs whose turn the worker (not the gate) refused, by thread:
        #: re-driven BY RUN ID by the loop at the recovery cadence, each tick
        #: re-reading that run first and dropping the entry the moment it is
        #: not `queued`/`retrying`/`resuming`, not its thread's active run, or
        #: not a conversation run (an operator cancel is the case that made
        #: this necessary: the bridge never observes it). Only runs this loop
        #: already met -- never a store-wide sample -- and never the source
        #: of a new run. `forget` drops an entry from outside the loop.
        self._redrive: dict[str, str] = {}
        #: The dispatch gate as the loop last read it, so the closed -> open
        #: edge can run one sweep for the runs that were left standing under
        #: the closed gate (a transport-bound thread's run is left `queued`
        #: there on purpose, and nothing else would pick it up).
        self._gate_seen: bool | None = None
        self._undriven_counts: dict[str, object] = {
            "submitted": 0,
            "resubmitted": 0,
            "canceled": 0,
            # ⟦P9-4⟧ Runs the floor converged to a NON-canceled terminal --
            # today only a `resuming` run whose worker never got its answer.
            "stalled": 0,
            "error": None,
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name=self.THREAD_NAME, daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Ask the loop to end, and do not wait out a turn that is still live.

        A turn in flight is holding the managed worker, and the caller's next
        act is to release that worker -- which is what actually ends the turn,
        typed, through the runtime's own uncertain-outcome path. Blocking here
        until the turn finished would deadlock exactly the shutdown that is
        supposed to unblock it.
        """

        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)
        with self._lock:
            # Nothing re-drives after the loop; the next daemon's start sweep
            # re-finds what is still standing from the store, not from here.
            self._redrive.clear()

    # -- the inbound seam --------------------------------------------------

    def submit(self, thread_id: str) -> bool:
        """Called from the poller thread; never blocks and never raises.

        The poller's next `telegram.poll` must not wait on a turn, so this only
        hands over an identifier. A thread whose turn is already queued or
        running is remembered as a follow-up rather than queued twice: the run
        the second message belongs to is the one already in flight.

        ⟦P8⟧ Returns whether the identifier was taken -- queued, or remembered
        as a follow-up -- and False when the bounded queue refused it. The
        Telegram sink ignores the answer; the sweep does not.
        """

        return self._enqueue(str(thread_id), followup=True)

    def forget(self, run_id: str) -> None:
        """Drop a run from the re-drive now, rather than at its next tick.

        ⟦P8 V-1⟧ The API's cancel route calls this after the cancellation
        committed: the tick's own re-read would drop the entry anyway (the run
        is no longer dispatchable), but an entry that dies with the cancel
        never even asks. Never blocks, never raises, never touches the store.
        """

        with self._lock:
            self._redrive.pop(str(run_id), None)

    def _enqueue(
        self, identifier: str, *, followup: bool, run_id: str | None = None
    ) -> bool:
        """`submit` without the follow-up rule when `followup` is False.

        A message arriving during a live turn IS a follow-up: the thread is
        submitted again when the turn ends, and the message gets a run of its
        own. A sweep finding the same thread already queued is not: it found
        the run that is about to be driven, and re-submitting it would start a
        second turn over the same history the moment the first one ended.

        `run_id` makes the submission a handover of a run that already exists:
        the turn drives that run if it is still the thread's active run and
        refuses otherwise -- it never creates one.
        """

        with self._lock:
            if identifier in self._queued:
                if followup:
                    self._followups.add(identifier)
                return followup
            self._queued.add(identifier)
        try:
            self._queue.put_nowait((identifier, run_id))
        except queue.Full:
            with self._lock:
                self._queued.discard(identifier)
            self._record(
                TurnOutcome(identifier, None, OUTCOME_REFUSED, REFUSED_QUEUE_FULL)
            )
            return False
        return True

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, object]:
        """Counts and categories. No message body, no chat id, no credential."""

        with self._lock:
            last = self._last
            failure = self._last_failure
            return {
                "state": "running" if self._thread is not None else "stopped",
                "queued": len(self._queued),
                "turns": self._turns,
                "outcomes": dict(self._outcomes),
                "reasons": dict(self._reasons),
                "last": None if last is None else last.to_dict(),
                # ⟦P5.4d⟧ Sticky, unlike `last`: an operator asking why the
                # product stopped answering is asking about the failure, and a
                # later refusal or a later answer must not erase it.
                "last_failure": None if failure is None else failure.to_dict(),
                "recovery": dict(self._recovery),
                # ⟦F-B6⟧ How many scopes were freed from a park nothing in
                # this product could answer.
                "abandoned": self._abandoned,
                # ⟦P9-4⟧ How many parked workers were handed the operator's
                # decision and carried on with the turn.
                "resolutions": self._resolutions,
                # ⟦P8⟧ What the start-time sweep found left never driven.
                "undriven": dict(self._undriven_counts),
            }

    # -- the loop ----------------------------------------------------------

    def _serve(self) -> None:
        attempts = 0
        next_attempt = time.monotonic()
        next_redrive = time.monotonic() + self.RECOVERY_RETRY_SECONDS
        self._gate_seen = self._gate()
        while not self._stop.is_set():
            if time.monotonic() >= next_redrive:
                # ⟦P8⟧ At the recovery cadence and independent of the recovery
                # state: a turn the worker refused (`release_not_approved`, a
                # credential) leaves its run `queued`, and the operator's fix
                # is a decision re-read at the next launch -- so the run is
                # re-driven from here rather than waiting for a restart. Only
                # runs this loop already refused, never a store-wide sweep:
                # that runs at start, on recovery's own retries -- and once
                # when the dispatch gate goes from closed to open (⟦V-5⟧),
                # because a run left standing under the closed gate entered
                # no re-drive and nothing else would pick it up.
                next_redrive = time.monotonic() + self.RECOVERY_RETRY_SECONDS
                gate = self._gate()
                if gate and self._gate_seen is False:
                    with self._lock:
                        # The per-run guard was set by handovers the closed
                        # gate refused (write-free, on transport-bound
                        # threads); the open edge is exactly when those runs
                        # are due again. A thread already queued or a run
                        # being driven is still skipped by the sweep itself.
                        self._swept.clear()
                    self._sweep_undriven()
                if gate is not None:
                    self._gate_seen = gate
                self._redrive_refused()
                # ⟦P9-3 ADJ-2⟧ The same cadence, for the runs whose turn
                # has ENDED but whose run never got a terminal: an
                # approval answered after the hold window closed, or a
                # cancel on the run that leaves behind. Before this the
                # floor's writer was reachable only from `recover()` and
                # the gate's open edge, so under a live daemon nothing
                # converged them and the thread stayed held until the
                # operator wrote again or the process restarted.
                self._sweep_stalled()
            state = str(self._recovery.get("state"))
            if state != "converged" and time.monotonic() >= next_attempt:
                next_attempt = time.monotonic() + self.RECOVERY_RETRY_SECONDS
                if state in _UNBUDGETED_RECOVERY_STATES:
                    # Waiting for something only the operator can do, which may
                    # be hours away. The budget below is for a worker that is
                    # not ready YET; a gate nobody has opened is not that, and
                    # spending the budget on it would mean the window that
                    # finally opens resumes nothing.
                    #
                    # ⟦P54D-2⟧ `refused` belongs here for the same reason:
                    # every one of its reasons is an operator decision that is
                    # re-read at each launch and can change under a running
                    # daemon, so a `release_not_approved` refusal used to spend
                    # the whole budget in 30 seconds and a later `cortex runtime
                    # approve` never re-ran recovery at all.
                    self.recover(attempt=attempts)
                elif attempts < self.RECOVERY_ATTEMPTS:
                    attempts += 1
                    self.recover(attempt=attempts)
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            identifier, expected_run = item
            try:
                outcome = self._run_turn(identifier, expected_run=expected_run)
            except Exception as exc:  # pragma: no cover - defensive
                outcome = TurnOutcome(
                    identifier,
                    None,
                    OUTCOME_FAILED,
                    type(exc).__name__,
                    failure_detail(exc),
                )
            self._record(outcome)
            with self._lock:
                if outcome.run_id:
                    # ⟦P9-3 ADJ-2⟧ This loop drove it, so this loop owes it a
                    # terminal if it never gets one. Dropped again by
                    # `_sweep_stalled` as soon as the run is terminal, which
                    # for an ordinary answered turn is the very next tick.
                    self._stalling.add(str(outcome.run_id))
                self._queued.discard(identifier)
                followup = identifier in self._followups
                self._followups.discard(identifier)
                if outcome.run_id:
                    if (
                        outcome.outcome == OUTCOME_REFUSED
                        and outcome.reason
                        not in {REFUSED_DISPATCH_DISABLED, REFUSED_NOT_CONVERSATION}
                    ):
                        # The worker refused, not the gate: the run is still
                        # `queued` and the loop re-drives it at its cadence
                        # (and a recovery retry's sweep may hand it over
                        # again). A gate refusal keeps its guard -- the gate is
                        # the operator's decision, re-read when a message or a
                        # start next submits the thread, and a run on a
                        # transport-bound thread must not be refused every
                        # three seconds. A run that is not a conversation is
                        # never re-asked.
                        self._swept.discard(outcome.run_id)
                        self._redrive[outcome.run_id] = identifier
                    else:
                        self._redrive.pop(outcome.run_id, None)
                # ⟦V-1⟧ Whatever this turn produced, a remembered run on the
                # same thread that is NOT the one it produced is stale: the
                # thread moved on (a cancel, a later run), and the tick's
                # re-read would only confirm it.
                for stale in [
                    run_id
                    for run_id, thread in self._redrive.items()
                    if thread == identifier and run_id != outcome.run_id
                ]:
                    self._redrive.pop(stale, None)
            if followup and not self._stop.is_set():
                self.submit(identifier)

    def _gate(self) -> bool | None:
        """The dispatch gate now: one row, or None when the store cannot say."""

        try:
            return bool(self._store.runtime_dispatch_enabled())
        except ControlStoreError:
            return None

    def _record(self, outcome: TurnOutcome) -> None:
        with self._lock:
            self._turns += 1
            self._outcomes[outcome.outcome] += 1
            if outcome.reason:
                self._reasons[outcome.reason] += 1
            self._last = outcome
            if outcome.outcome == OUTCOME_FAILED:
                self._last_failure = outcome
        if outcome.outcome in {OUTCOME_REFUSED, OUTCOME_FAILED, OUTCOME_CANCELED}:
            _log.info(
                "turn %s: thread=%s run=%s reason=%s",
                outcome.outcome,
                outcome.thread_id,
                outcome.run_id,
                outcome.reason,
            )

    # -- one turn ----------------------------------------------------------

    def _run_turn(
        self, thread_id: str, *, expected_run: str | None = None
    ) -> TurnOutcome:
        # The gate FIRST, and before anything durable is written for a thread a
        # transport can deliver to. A run created under a closed gate would
        # fail on the orchestrator's own check and commit a `run.failed` event
        # -- which is in NOTIFICATION_TYPES, so the drain would deliver it and
        # "no turn" would have sent a message anyway. `runtime disable-dispatch`
        # (cutover step 5a) promises exactly that nothing more is sent.
        if not self._store.runtime_dispatch_enabled():
            # ⟦P8⟧ A run that ALREADY exists under the closed gate -- created
            # through the control API, or left by a previous daemon -- is
            # named on the refusal. On a thread NOTHING can deliver to it is
            # also ended typed with the gate's own word: no message can result,
            # and a run nothing will ever move must not sit `queued`. On a
            # transport-bound thread it is left untouched -- the next submit
            # after the gate opens drives it -- and a thread with no such run
            # is refused exactly as before, with nothing written.
            return TurnOutcome(
                thread_id,
                self._refuse_undriven(thread_id),
                OUTCOME_REFUSED,
                REFUSED_DISPATCH_DISABLED,
            )
        try:
            run = self._run_for(thread_id, expected_run)
        except ManagedWorkerUnavailable as exc:
            return TurnOutcome(thread_id, None, OUTCOME_REFUSED, exc.reason)
        except ControlStoreError as exc:
            return TurnOutcome(thread_id, None, OUTCOME_REFUSED, exc.category)
        if isinstance(run, str):
            return TurnOutcome(thread_id, None, OUTCOME_REFUSED, run)
        if (
            expected_run is None
            and str(run["state"]) in ABANDONABLE_STATES
            and self._abandon(run)
        ):
            # ⟦F-B6⟧ The scope was parked on a question nothing in this
            # product can answer. The operator writing again IS the escape:
            # the parked run ends typed and this message gets a fresh one.
            # A handover is not the operator writing: it drives the run it
            # names or refuses, and escapes nothing.
            try:
                run = self._run_for(thread_id)
            except ManagedWorkerUnavailable as exc:
                return TurnOutcome(thread_id, None, OUTCOME_REFUSED, exc.reason)
            except ControlStoreError as exc:
                return TurnOutcome(thread_id, None, OUTCOME_REFUSED, exc.category)
            if isinstance(run, str):
                return TurnOutcome(thread_id, None, OUTCOME_REFUSED, run)
        run_id = str(run["id"])
        if run["state"] == "waiting_for_decision":
            return TurnOutcome(
                thread_id, run_id, OUTCOME_DECISION_REQUIRED, "already_parked"
            )
        if run["state"] not in DISPATCHABLE_RUN_STATES:
            return TurnOutcome(thread_id, run_id, OUTCOME_IN_FLIGHT, run["state"])
        try:
            orchestrator = self._orchestrator_factory()
        except ManagedWorkerUnavailable as exc:
            return TurnOutcome(thread_id, run_id, OUTCOME_REFUSED, exc.reason)
        try:
            return self._drive(orchestrator, thread_id, run_id)
        finally:
            # ⟦P9-3 BRK-4⟧ Whatever the turn learned about the runtime, kept
            # for the control surfaces. In the `finally` because a turn that
            # refused or failed still asked, and the answer is just as true.
            self._remember_capabilities(orchestrator)

    def _run_for(
        self, thread_id: str, expected_run: str | None = None
    ) -> dict[str, Any] | str:
        """The run this message belongs to: the active one, or a new one.

        ⟦V-1⟧ With `expected_run` -- a handover of a run that already exists
        -- the answer is that run or a refusal, never a new one: a thread whose
        active run is not the one named has moved on (the operator cancelled
        it, a later turn replaced it), and a handover that created a run here
        would be a turn nobody asked for, every tick, for ever.
        """

        thread = self._store.get_thread(thread_id)
        active = thread.get("active_run_id")
        if expected_run is not None and str(active or "") != expected_run:
            return REFUSED_RUN_NOT_ACTIVE
        if active:
            # ⟦P8⟧ The same predicate the sweep and the API apply, here on the
            # drive path: a run the engine owns is never driven from any
            # entry, whichever thread a message reached it through.
            if not self._store.run_is_conversation(str(active)):
                return REFUSED_NOT_CONVERSATION
            return self._store.get_run(str(active))
        if self._store.thread_is_machine(thread_id):
            # ⟦V6-3⟧ Nothing this bridge does opens a run on the engine's own
            # thread: a run created here would be adopted by nobody and
            # driven by nobody, and would hold the thread's one active slot.
            return REFUSED_NOT_CONVERSATION
        return self._store.create_run(
            thread_id=thread_id,
            expected_revision=int(thread["revision"]),
            actor_id=self._actor_id,
            # Bound to the revision the run is created FROM, so a retry of this
            # same submission replays the receipt instead of racing a second
            # create against the ledger's one-active-run index.
            idempotency_key=f"bridge-run-{thread_id}-{thread['revision']}"[:64],
        ).value

    def _abandon(self, run: Mapping[str, Any]) -> bool:
        """End a run nothing is going to move, so the scope is usable again.

        ⟦F-B6⟧ Keyed off dispatch ownership rather than off state, so it cannot
        race a decision the operator resolved seconds earlier -- but not off
        `attempts.dispatch_owner`, which `bind_runtime` sets to NULL the moment
        the attempt binds and which is therefore NULL for every parked run. The
        authority that can answer is this bridge: it drives one turn at a time
        on one thread, and it knows which run that is.

        `run.failed` is in `NOTIFICATION_TYPES`, so this correctly sends the
        operator one message saying the turn was abandoned. Not a duplicate:
        the parked attempt produced no delivery of its own.

        ⟦P9-3 / ADJ14-3⟧ Correctly, EXCEPT for the one abandonable state the
        operator asked for. A decision that parks a run and a `/cancel` that
        lands afterwards leave it `cancel_requested` -- the other ordering
        from the A-2 arm, which only ever saw the cancel first -- and
        abandoning it as `failed / turn_abandoned / retryable: true` told the
        operator that a turn THEY cancelled had failed, with a Retry button,
        which is the property the whole cancel family exists to hold. So the
        target comes from the run's own state now: a converging run ends
        through `_end_converging`, the same writer and the same category the
        floor uses, and `run.canceled` is not a notification type. Every other
        abandonable state is unchanged.
        """

        run_id = str(run["id"])
        with self._lock:
            if run_id in self._driving:
                return False
        attempt_id = str(run.get("active_attempt_id") or "")
        if not attempt_id:
            return False
        try:
            record = self._store.get_attempt(attempt_id)
            if record.get("dispatch_owner"):
                # Reserved and not yet bound, so somebody holds a lease on it.
                return False
            binding_id = record.get("runtime_binding_id")
            if not binding_id:
                return False
            if str(run["state"]) in STALLED_TERMINALS:
                if not self._end_converging(
                    run,
                    attempt_id,
                    (
                        "abandoned for a new message while "
                        f"{run['state']}"
                    ),
                ):
                    return False
            else:
                self._store.apply_runtime_transition(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    runtime_binding_id=str(binding_id),
                    runtime_release_id=str(record["runtime_release_id"]),
                    state_generation_id=record.get("state_generation_id"),
                    target_state="failed",
                    expected_revision=int(run["revision"]),
                    actor_id=self._actor_id,
                    idempotency_key=(
                        f"bridge-abandon-{run_id}-{run['revision']}"[:64]
                    ),
                    payload={
                        "category": TURN_ABANDONED,
                        "retryable": True,
                        "detail": str(run["state"]),
                    },
                )
        except Exception:  # noqa: BLE001 - a run that moved under us stays put
            return False
        with self._lock:
            self._abandoned += 1
        return True

    def _drive(
        self, orchestrator: Any, thread_id: str, run_id: str
    ) -> TurnOutcome:
        with self._lock:
            self._driving.add(run_id)
        try:
            return asyncio.run(self._dispatch(orchestrator, thread_id, run_id))
        finally:
            with self._lock:
                self._driving.discard(run_id)

    async def _dispatch(
        self, orchestrator: Any, thread_id: str, run_id: str
    ) -> TurnOutcome:
        task = asyncio.ensure_future(orchestrator.dispatch(run_id))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._turn_timeout
        parked = False
        timed_out = False
        #: The control actions this turn has already handed over, by id.
        delivered: set[str] = set()
        #: When this run first entered a stalled state, on the loop's clock.
        stalled_since: float | None = None
        #: When it parked on a decision, which is not a stall -- it is a wait.
        parked_since: float | None = None
        quiet = False
        while True:
            done, _pending = await asyncio.wait({task}, timeout=self.STATE_POLL_SECONDS)
            if done:
                break
            # Durable state, not the stream. A turn parked on a decision has
            # already committed `decision.required` and moved the run to
            # `waiting_for_decision`; the stream that would tell us the same
            # thing never ends, because nothing is going to answer it.
            state = self._state(run_id)
            # ⟦P9-3 ADJ-1⟧ Recorded on the way past, not at the end of the
            # turn. `RunOrchestrator.dispatch` assigns `last_capabilities`
            # before the run ever reaches `running`, so by the first pass of
            # this poll the answer exists -- and `running` is exactly the
            # state a pause is offered from. Remembering it only in
            # `_run_turn`'s `finally` left `runtime_supports` answering None
            # for the whole of every live turn, which is the one window where
            # a pause is reachable, so the refusal it feeds could never fire
            # on a first turn and never fired at all while the dispatch gate
            # stayed shut.
            self._remember_capabilities(orchestrator)
            if state == "waiting_for_decision":
                # ⟦P9-4⟧ This used to break immediately, and that is why every
                # approval-gated turn died. Breaking cancels the dispatch,
                # whose `finally` deregisters the adapter execution and sends
                # the worker `turn.cancel` -- so by the time the operator
                # answered, `resolve_decision` had nothing to deliver to and
                # the run sat in `resuming` with its thread held. The turn is
                # HELD open instead, bounded, because the worker parked in
                # `await_decision` is reachable only while this dispatch
                # lives. Beyond the window it is abandoned exactly as before.
                # ⟦P9-3 FV-2⟧ What keeps the run honest afterwards is the
                # cadence sweep, NOT the floor below: this `break` leaves the
                # `while True:` that floor lives in, so it cannot run again for
                # this run. `_sweep_stalled` (bridge.py:1522, armed every tick
                # from `_serve`'s re-drive block at bridge.py:623) reaches
                # `_converge_stalled` (bridge.py:1439), which applies the same
                # `_cancel_floor` from outside the turn.
                if parked_since is None:
                    parked_since = loop.time()
                if loop.time() - parked_since >= self._decision_wait:
                    parked = True
                    break
            else:
                parked_since = None
                if state in STALLED_STATES:
                    # ⟦P9-3 / P9-4⟧ The operator asked this live turn to stop,
                    # or answered the question it parked on. The same re-read
                    # that spots a parked run is what spots either, and this is
                    # the only place in the product that can act: the adapter's
                    # execution for this attempt is live exactly here, on this
                    # thread.
                    if stalled_since is None:
                        stalled_since = loop.time()
                    if await self._deliver_runtime_actions(
                        orchestrator, run_id, delivered, DELIVERABLE_ACTIONS[state]
                    ):
                        # A delivery that landed moved the run on -- an acked
                        # `decision.resolve` puts it back in `running` inside
                        # the same transaction -- so the floor's clock starts
                        # again from whatever it does next.
                        stalled_since = None
                    elif loop.time() - stalled_since >= self._cancel_floor:
                        # Told, and still silent. The floor, not the turn
                        # timeout: a run that has been answered or asked to
                        # stop is not owed 900 seconds, and the rescue that
                        # fires at 900 refuses all three of these states.
                        quiet = True
                        break
                elif stalled_since is not None:
                    # It left the stalled state under us -- a pause that
                    # reached `paused`, a resume that reached `running` -- so
                    # the floor no longer applies to what the turn does next.
                    stalled_since = None
            if self._stop.is_set():
                # ⟦F-B5⟧ A shutdown is NOT a timeout. The caller's next act is
                # to release the worker, which ends the attempt through the
                # runtime's own uncertain-outcome path, and c4's
                # `recovery_deferred` covers what the restart finds.
                break
            if loop.time() >= deadline:
                timed_out = True
                break
        if not task.done():
            if quiet:
                # ⟦P9-3 BRK-2⟧ The terminal is written BEFORE the teardown, and
                # the order is the whole fix. `await task` below is unbounded:
                # it runs through the adapter's `release_ownership`, which
                # joins the worker over an uninterruptible `asyncio.to_thread`.
                # A worker that ignores both the delivered cancel and the
                # teardown held the run in `cancel_requested` for as long as it
                # liked -- the review measured 15.1 s under a 1.0 s floor -- so
                # the floor bounded the DECISION to give up and not the thing
                # it is named for: how long the RUN sits owing a terminal.
                #
                # Safe in this order for two reasons already in the code. The
                # writer re-reads the run and no-ops if it moved, so a worker
                # that finishes during the teardown still wins the race it
                # would have won anyway; and the late-event arms in
                # `RunOrchestrator._apply_durable_event` absorb a terminal
                # event that arrives afterwards, which is exactly what they
                # exist for.
                #
                # This bounds the RUN's state, not the turn slot: `await task`
                # still holds the drain loop's single turn thread, which is
                # pre-existing structure (byte-identical at 68c93dc) and
                # carried as a standing item.
                self._end_quiet_after_control(orchestrator, run_id)
            # Abandoning the turn is what tells the worker to stop: the runtime
            # adapter's generator closes, `turn_events` runs its `finally`, and
            # the worker is sent `turn.cancel` rather than left inside an
            # approval callback nobody is coming to answer.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            if timed_out:
                self._end_timed_out(run_id)
        else:
            exception = task.exception()
            if exception is not None and not isinstance(exception, Exception):
                raise exception
        return self._classify(thread_id, run_id, parked=parked)

    async def _deliver_runtime_actions(
        self,
        orchestrator: Any,
        run_id: str,
        delivered: set[str],
        kinds: frozenset[str],
    ) -> bool:
        """Hand the operator's queued action to the worker it is waiting on.

        True when one was acknowledged, which for a `decision.resolve` means
        the parked worker has the answer and the run is back in `running`.

        ⟦P9-3 / P9-4⟧ `ControlStore` queues a runtime action inside the very
        transaction that records the operator's command -- a `control.cancel`
        for a cancel, a `control.pause` for a pause, a `decision.resolve` for
        an answered approval -- and `RunOrchestrator.deliver_runtime_action`
        has always known how to hand one to the adapter, but nothing in
        production ever called it. The action sat
        `pending` until the attempt ended, so the shipped worker was never
        told, its `context.canceled` -- the ONLY escape from
        `TurnContext.await_decision`, which has no timeout -- never flipped,
        and a cancelled turn kept running, kept emitting and kept asking for
        approvals until the stream was abandoned at the end of the turn. Every
        late-event arm in `RunOrchestrator._apply_event_once` is a consequence
        of that silence rather than of the cancel itself.

        This loop is where the delivery belongs: it already re-reads durable
        state every `STATE_POLL_SECONDS` while the turn streams, it is the one
        thread driving this run, and the adapter's execution for this attempt
        is registered exactly for the lifetime of the dispatch it is racing.

        `list_pending_runtime_actions` decides what is deliverable and nothing
        here re-derives it: the query joins the run and answers only for an
        action whose kind matches the state the run is actually in and whose
        attempt is still the active one. Each action is offered once per turn
        -- the point of the offer is to unpark the worker, which one delivery
        does, and a second would only be a duplicate the adapter's outcome
        cache answers anyway.

        A delivery that raises is logged and left. It competes for the run's
        revision with the dispatch that is applying events (the store's own
        `_confirm_runtime_resume_from_event` expects either side to lose), the
        action stays `pending` or `outcome_unknown` for `recover`'s
        reconciliation sweep, and a turn must never fail because the
        operator's cancel could not be handed over.
        """

        try:
            actions = self._store.list_pending_runtime_actions()
        except ControlStoreError as exc:
            _log.warning(
                "action delivery: the store could not be asked (%s)", exc.category
            )
            return False
        acked = False
        for action in actions:
            action_id = str(action["id"])
            kind = str(action.get("kind"))
            if str(action.get("run_id")) != run_id:
                continue
            if kind not in kinds:
                continue
            if action_id in delivered:
                continue
            try:
                settled = await orchestrator.deliver_runtime_action(
                    action_id, worker_id=self._actor_id
                )
            except Exception as exc:  # noqa: BLE001 - see above
                # ⟦P9-3 BRK-3⟧ NOT marked delivered: the marking happens below,
                # after the hand-over returns. Marking before the attempt meant
                # one throw from the pre-`try` region of
                # `deliver_runtime_action` -- the claim and its three gets --
                # dropped the operator's cancel for the rest of the turn, so it
                # reached the worker at the floor instead of within a poll.
                # Re-offering is safe by construction: the adapter's cached
                # action outcome makes a repeat a no-op, which is the same
                # property that made the once-per-turn rule cheap in the first
                # place.
                _log.info(
                    "action delivery: run=%s action=%s not handed over (%s)",
                    run_id,
                    action_id,
                    failure_detail(exc),
                )
                continue
            delivered.add(action_id)
            state = (
                str(settled.get("state")) if isinstance(settled, Mapping) else ""
            )
            if state == "acked":
                acked = True
                if kind in RESUME_ACTION_KINDS:
                    with self._lock:
                        self._resolutions += 1
            _log.info(
                "action delivery: run=%s action=%s kind=%s %s",
                run_id,
                action_id,
                kind,
                state or "delivered",
            )
        return acked

    def _state(self, run_id: str) -> str | None:
        try:
            return str(self._store.get_run(run_id)["state"])
        except ControlStoreError:
            return None

    def _end_timed_out(self, run_id: str) -> None:
        """⟦F-B5⟧ Write the terminal transition the cancelled task cannot.

        `task.cancel()` raises `CancelledError` inside `RunOrchestrator.
        dispatch`, and `CancelledError` is a `BaseException`, so the `except
        Exception` that commits a failure never sees it: the attempt stayed
        bound, the run stayed `running`, and every later message to that
        Telegram scope answered `turn_in_flight` for the life of the daemon.

        `retryable` is true because it is: the operator's next message starts a
        fresh attempt on the same run, which is exactly what should happen.
        Keyed off the run's own state rather than off the deadline alone, so a
        turn that finished in the same instant is not failed after the fact.
        """

        try:
            run = self._store.get_run(run_id)
            if str(run["state"]) not in DEFERRABLE_STATES:
                return
            attempt_id = str(run.get("active_attempt_id") or "")
            if not attempt_id:
                return
            record = self._store.get_attempt(attempt_id)
            binding_id = record.get("runtime_binding_id")
            if not binding_id:
                # Reserved and never bound: `fail_unbound_run` is that shape's
                # transition, and `recover_startup` is what applies it.
                return
            self._store.apply_runtime_transition(
                run_id=run_id,
                attempt_id=attempt_id,
                runtime_binding_id=str(binding_id),
                runtime_release_id=str(record["runtime_release_id"]),
                state_generation_id=record.get("state_generation_id"),
                target_state="failed",
                expected_revision=int(run["revision"]),
                actor_id=self._actor_id,
                idempotency_key=f"bridge-timeout-{run_id}-{run['revision']}"[:64],
                payload={
                    "category": TURN_TIMEOUT,
                    "retryable": True,
                    "detail": f"no terminal event within {int(self._turn_timeout)}s",
                },
            )
        except Exception:  # noqa: BLE001 - a turn that raced us is not an error
            return

    def _end_quiet_after_control(self, orchestrator: Any, run_id: str) -> None:
        """⟦P9-3⟧ The terminal floor for a BOUND run the operator stopped.

        The hole the fix-verify review's standing item 6 names. A run in a
        converging state has a bound attempt and a worker that owes it a
        terminal event, and NOTHING in this product would write one if the
        worker simply stopped talking: `_end_timed_out` returns early outside
        `DEFERRABLE_STATES`, `_recoverable` filters to the same two states so a
        restart does not converge it, and `_undriven` answers None for a bound
        attempt so the start sweep could not either. The run stayed
        `cancel_requested` for ever, holding the thread's `active_run_id`, the
        attempt's pin and this loop's only turn slot, until the operator's
        next message ended it `failed / turn_abandoned / retryable: true` --
        and `run.failed` is in `NOTIFICATION_TYPES` where `run.canceled` is
        not, so they were actively notified of a failure for a turn they had
        cancelled.

        So it converges here, under its own category and never as a failure.
        Keyed off the run's own state re-read now rather than off the deadline
        alone, so a worker that answered in the same instant is not overridden
        after the fact, and the pin is handed back afterwards exactly as the
        orchestrator's own terminal arcs hand it back -- otherwise the run
        would be terminal while its attempt still held the updater pin.
        """

        try:
            run = self._store.get_run(run_id)
        except ControlStoreError:
            return
        if str(run["state"]) not in STALLED_STATES:
            return
        attempt_id = str(run.get("active_attempt_id") or "")
        if not attempt_id:
            return
        self._end_converging(
            run,
            attempt_id,
            (
                f"no terminal event within {_seconds(self._cancel_floor)} "
                f"of {run['state']}"
            ),
            orchestrator=orchestrator,
        )

    def _end_converging(
        self,
        run: Mapping[str, Any],
        attempt_id: str,
        detail: str,
        *,
        orchestrator: Any | None = None,
    ) -> bool:
        """Write the terminal transition a bound converging run never got.

        Shared by the live turn's floor, the abandon path and the start sweep,
        so the three can never disagree about what the run is called or
        whether it is retryable. A run that moved under us fails its own
        revision check and stays where the writer that moved it takes it.

        ⟦P9-3 C2 / batchK 6⟧ Including the pin. The terminal transition queues
        the attempt's pin release, and handing it back used to be the caller's
        job -- which exactly one of the three callers did, so a run converged
        by the abandon path or by either sweep was terminal while its attempt
        still held the updater pin until some later dispatch happened to
        deliver it. It is done here now, by the writer, so the three cannot
        disagree about that either. `orchestrator` is the caller's when it has
        one; the two that have none -- both sweeps, through `_converge_stalled`
        (bridge.py:1439), and `_abandon` (bridge.py:839), which `_run_turn`
        runs before it builds one -- must not acquire one, so they get
        `_pin_release_authority`.

        ⟦P9-3 BRK-5⟧ The category and the retryable flag are the shared
        decision and stay here; the DETAIL is the caller's, because only the
        caller knows what actually happened. This used to hard-code the
        floor's sentence, so a run ended by the operator's next message
        reported "no terminal event within 300s" for a wait of 0.01 s -- a
        durable payload asserting a wait that never happened.
        """

        state = str(run["state"])
        terminal = STALLED_TERMINALS.get(state)
        if terminal is None:
            return False
        target, category, retryable = terminal
        try:
            record = self._store.get_attempt(attempt_id)
            binding_id = record.get("runtime_binding_id")
            if not binding_id:
                # Reserved and never bound: `fail_unbound_run` is that shape's
                # transition, and the sweep's own arm already applies it.
                return False
            self._store.apply_runtime_transition(
                run_id=str(run["id"]),
                attempt_id=attempt_id,
                runtime_binding_id=str(binding_id),
                runtime_release_id=str(record["runtime_release_id"]),
                state_generation_id=record.get("state_generation_id"),
                target_state=target,
                expected_revision=int(run["revision"]),
                actor_id=self._actor_id,
                idempotency_key=f"bridge-quiet-{run['id']}-{run['revision']}"[:64],
                payload={
                    "category": category,
                    "retryable": retryable,
                    "detail": detail,
                },
            )
        except Exception:  # noqa: BLE001 - a run that moved under us stays put
            return False
        _log.info("run=%s ended %s from %s", run["id"], category, state)
        self._release_pins(orchestrator)
        return True

    def _remember_capabilities(self, orchestrator: Any) -> None:
        """Keep what the turn learned about the runtime. Never fatal."""

        capabilities = getattr(orchestrator, "last_capabilities", None)
        if capabilities is None:
            return
        with self._lock:
            self._runtime_capabilities = capabilities

    def runtime_supports(self, capability: str) -> bool | None:
        """Whether the runtime this bridge drives can do `capability`.

        ⟦P9-3 BRK-4⟧ Tri-state on purpose, and the third state is the point.
        `True`/`False` are answers this daemon was actually given by the
        adapter; `None` means no turn has reached the runtime yet, so nothing
        is known and a caller must behave exactly as it did before this
        existed. Refusing on an unknown would invent a capability report,
        which is the failure this was added to remove rather than to repeat.

        ⟦P9-3 ADJ-1⟧ The window in which the answer is unknown must not
        overlap the window in which a pause is reachable, and the first
        version of this got that exactly wrong: it recorded at turn END, so
        the answer was None for the whole of every LIVE turn -- the only time
        a run is `running` and therefore the only time a pause can be asked
        for. A first turn could stay `running` for minutes
        (`TURN_TIMEOUT_SECONDS` is 900, `DECISION_WAIT_SECONDS` 180), and a
        closed dispatch gate held the window open indefinitely, because
        `last_capabilities` is assigned inside `dispatch` and a refused turn
        never gets there.

        It is recorded from `_dispatch`'s durable re-read instead, which runs
        while the turn is live and after `dispatch` has already asked. So the
        answer is present from that loop's FIRST re-read, which is one
        `STATE_POLL_SECONDS` (bridge.py:251, 0.25 s) after the dispatch task
        is created -- and not one instant sooner.

        ⟦P9-3 FV-1⟧ That last clause is the qualification the first version
        of this sentence left out, and the fix-verify review measured the
        difference rather than arguing it. `RunOrchestrator.dispatch` assigns
        `last_capabilities` (orchestration/service.py:359) and drives the run
        to `running` well inside one poll, so a pause arriving between
        `running` and this bridge's first re-read still reads None here, still
        returns 200 and still takes the pre-fix path -- the review hit that
        window 6 trials out of 6 by spinning on the run row at 0.5 ms. A human
        cannot: they would have to watch the run turn `running` and press
        Pause inside a quarter second. That is why the prescribed site is this
        one and why this is a qualification rather than a re-gating. Closing
        the interval means asking the orchestrator itself when this answers
        None for a run already `running`; shortening `STATE_POLL_SECONDS` is
        NOT the fix, because it narrows the window without closing it and buys
        the narrowing with a durable read per turn.
        """

        with self._lock:
            capabilities = self._runtime_capabilities
        if capabilities is None:
            return None
        value = getattr(capabilities, capability, None)
        return bool(value) if isinstance(value, bool) else None

    def _release_pins(self, orchestrator: Any | None = None) -> None:
        """Hand back the attempt pin a run converged from here was holding.

        The same call `RunOrchestrator` makes after its own terminal writes,
        and best effort for the same reason: a pin that could not be handed
        back now is still pending for the next arc that asks -- every
        `dispatch` delivers the pending releases (orchestration/service.py:512
        and orchestration/service.py:644) and so does `recover_startup`, whose
        own delivery is at orchestration/service.py:1662, so the worst outcome
        of a failure here is the pre-C2 one rather than a lost pin.
        """

        try:
            if orchestrator is None:
                orchestrator = self._pin_release_authority()
            orchestrator.deliver_pending_pin_releases(worker_id=self._actor_id)
        except Exception:  # noqa: BLE001 - see above
            return

    def _pin_release_authority(self) -> Any:
        """An orchestrator that can hand a pin back and do nothing else.

        ⟦P9-3 C2⟧ Why the sweeps do not simply build the real thing. `recover()`
        runs `_sweep_undriven()` BEFORE `self._recovery_factory()`, deliberately
        (⟦P8⟧, at the call): what a dead daemon left never driven needs no
        worker to converge and must not wait for a window the installation may
        never open. `_sweep_stalled` runs on the loop's cadence tick, where
        launching a worker to hand a pin back would be far worse than the pin.
        So this builds the two things a release actually needs -- the store and
        `ManagedTransportWorker.releases` (managed_worker.py:806), the updater
        service the binding already resolved through, which is constructed with
        the worker and needs no `acquire()` -- and no runtime.
        """

        from ..orchestration import RunOrchestrator

        return RunOrchestrator(
            self._store,
            _NO_RUNTIME,  # type: ignore[arg-type] - see `_NoRuntime`
            self._worker.releases,
            actor_id=self._actor_id,
        )

    def _converge_stalled(self, run: Mapping[str, Any]) -> bool:
        """End one run left in a converging or resuming state with no driver.

        ⟦P9-3 ADJ-2⟧ Extracted from the store-wide start sweep so the LIVE loop
        can reach it too. It had exactly one caller -- `_sweep_undriven`, which
        runs at `recover()` and on the dispatch gate's open edge -- so under a
        running daemon nothing wrote the terminal at all: an approval answered
        after the 180 s hold moved the run to `resuming` and it stayed there,
        holding the thread's `active_run_id`, until the operator wrote again or
        the process restarted. A `/cancel` on that run had the same problem.

        ⟦P9-3 ADJ-3⟧ The sentence is the caller's, for the reason BRK-5
        established: this is reached from a restart AND from a live tick, so a
        payload asserting "at startup, with no daemon left to end it" was false
        on one of the two paths the moment this gained a second caller.
        """

        run_id = str(run["id"])
        state = str(run["state"])
        # The unbound arm stays cancel-only: `running` is the only state
        # `_RUN_TRANSITIONS` gives a `pause_requested` edge and
        # `resolve_decision` refuses an unbound attempt outright, so both of
        # those are always bound and `CANCELED_BEFORE_BINDING` would be a false
        # label on them.
        #
        # ⟦P9-4 / P9-3 ADJ-5⟧ `resuming` never reaches the dispatchable arm in
        # `_sweep_undriven` any more, which changes nothing for the shape a
        # conversation turn produces: `resolve_decision` only writes `resuming`
        # for a BOUND attempt, so `_undriven` answered None for it there too.
        # The second producer is `ControlStore.resume_run`, which is reachable
        # only from `paused` (its `source_state="paused"`,
        # control/store.py:6493), and
        # a conversation turn can never be `paused`: the transition into it
        # refuses an attempt with no `checkpoint_uri` (control/store.py:5420-5423)
        # and the resume out of it refuses the same shape
        # (control/store.py:6647-6648), both raising
        # `InvalidTransition("checkpoint_missing", ...)`.
        #
        # ⟦P9-3 FV-3⟧ And the load-bearing clause, "a Hermes turn writes none",
        # is checkable rather than asserted: `attempts.checkpoint_uri` has one
        # writer in the whole store, the `UPDATE attempts SET checkpoint_uri`
        # inside `ControlStore.commit_checkpoint` (control/store.py:6520, the
        # write at control/store.py:6590) -- the attempt INSERTs set only
        # `source_checkpoint_uri` (control/store.py:6649-6652) -- and that
        # method is defined once and called
        # from tests only, never from a turn. Which is also why
        # `pause_requested` converges to `canceled` rather than to `paused` at
        # all. So an UNBOUND `resuming` run is unreachable rather than merely
        # unhandled.
        undriven = self._undriven(run) if state == "cancel_requested" else None
        if undriven is not None:
            if self._end_undriven(
                run, *undriven, category=CANCELED_BEFORE_BINDING, key="cancel"
            ):
                with self._lock:
                    self._undriven_counts["canceled"] += 1  # type: ignore[operator]
                _log.info("sweep: run=%s ended %s", run_id, CANCELED_BEFORE_BINDING)
                return True
            return False
        # ⟦P9-3⟧ The BOUND shape `_undriven` refuses to answer for. No terminal
        # event is ever coming for it and no other sweep in this file would
        # converge it.
        stale = self._stale_converging(run)
        if stale is None:
            return False
        if not self._end_converging(
            run,
            stale,
            (
                f"still {state} with no turn driving it, "
                f"{_seconds(self._cancel_floor)} after it entered that state"
            ),
        ):
            return False
        # Counted by the terminal it actually wrote, so `canceled` never
        # tallies a run that ended `failed`.
        counter = (
            "canceled" if STALLED_TERMINALS[state][0] == "canceled" else "stalled"
        )
        with self._lock:
            self._undriven_counts[counter] += 1  # type: ignore[operator]
        return True

    def _sweep_stalled(self) -> None:
        """Converge the runs THIS loop left behind, once per cadence tick.

        ⟦P9-3 ADJ-2⟧ Deliberately NOT `_sweep_undriven`. That one reads the
        store-wide `list_recoverable_runs` projection, and
        `test_the_loop_does_not_sample_the_store_for_the_life_of_the_daemon`
        pins that the loop does not sample it per tick -- a daemon that answers
        no messages must not poll a growing projection for ever. This asks
        `get_run` for the handful of runs this process's own turns parked or
        abandoned, which is bounded by what the loop actually did and costs one
        cheap read each.

        The candidate leaves the set when -- and only when -- it is terminal,
        so a run that completes normally is asked about exactly once and a run
        that never reaches a terminal is asked about every tick for as long as
        this process lives. ⟦P9-3 FV-4⟧ The second half is the one worth
        stating: an approval nobody answers holds its run in
        `waiting_for_decision`, which is neither terminal nor stalled, so this
        sweep reads it and acts on nothing, once per tick, for ever. It is one
        indexed row read and there is no correctness consequence, and it is
        NOT safe to optimise away by discarding a candidate that is merely not
        stalled YET: the operator's answer or cancel arrives from a route that
        writes the state and queues no turn, so nothing would put the run back
        and the late-answer wedge ADJ-2 exists to close would reopen. See
        `self._stalling` above for the fence.

        WHAT FENCES THIS, stated exactly rather than as "nothing races a live
        turn". Within this process: `_stale_converging` refuses any run in
        `self._driving`, so the loop cannot converge the turn it is running.
        Across processes there is ONE fence and it is time -- the run's
        `updated_at` must be older than `_cancel_floor` -- plus the revision
        check inside `_end_converging`, which makes the write a no-op if
        anything moved the run first. Arming this every tick rather than only
        at restart therefore narrows the cross-process margin from "a restart
        happened" to "the floor elapsed": a SECOND cortexd on the same
        `control.db` whose live turn had not written its run row for longer
        than the floor could have that run converged under it. Single-daemon
        installs -- every supported deployment -- are unaffected, and the
        revision fence means the loser of such a race writes nothing rather
        than corrupting anything.
        """

        with self._lock:
            candidates = sorted(self._stalling)
        if not candidates:
            return
        for run_id in candidates:
            try:
                run = self._store.get_run(run_id)
            except ControlStoreError:
                with self._lock:
                    self._stalling.discard(run_id)
                continue
            state = str(run["state"])
            if state in TERMINAL_RUN_STATES:
                with self._lock:
                    self._stalling.discard(run_id)
                continue
            if state in STALLED_STATES:
                self._converge_stalled(run)

    def _stale_converging(self, run: Mapping[str, Any]) -> str | None:
        """The attempt of a BOUND converging run nobody is left to end, or None.

        The restart half of the floor above. `_undriven` deliberately answers
        None for a bound attempt, because its ending is `fail_unbound_run` and
        that transition does not apply here; this asks the opposite question,
        and answers only for a run that is bound, that this process is not
        driving, and whose converging state is older than the floor -- so a
        cancel that landed a moment ago, on a turn a live loop is about to
        converge itself, is left alone.
        """

        run_id = str(run["id"])
        with self._lock:
            if run_id in self._driving:
                return None
        attempt_id = str(run.get("active_attempt_id") or "")
        if not attempt_id:
            return None
        try:
            record = self._store.get_attempt(attempt_id)
        except ControlStoreError:
            return None
        if not record.get("runtime_binding_id"):
            return None
        if not _older_than(run.get("updated_at"), self._cancel_floor):
            return None
        return attempt_id

    def _classify(
        self, thread_id: str, run_id: str, *, parked: bool
    ) -> TurnOutcome:
        state = self._state(run_id)
        if state == "completed":
            return TurnOutcome(thread_id, run_id, OUTCOME_ANSWERED, None)
        if state == "waiting_for_decision" or (
            parked and state not in {"completed", "failed", "canceled"}
        ):
            # ⟦P9-4⟧ The reason names what the TURN did, not what the run has
            # become since. `reasons` is a cumulative tally for the life of
            # the daemon, and an answer landing in the instant between the
            # window expiring and this read used to add a permanent `resuming`
            # entry to it -- describing a state no turn ends in, and which the
            # floor now converges anyway. The turn parked; that is the whole
            # fact, and a run that has since reached a terminal is classified
            # by that terminal instead.
            return TurnOutcome(
                thread_id, run_id, OUTCOME_DECISION_REQUIRED, "waiting_for_decision"
            )
        if state == "canceled":
            category, _detail = self._failure(run_id)
            return TurnOutcome(thread_id, run_id, OUTCOME_CANCELED, category)
        category, detail = self._failure(run_id)
        if category is None:
            # ⟦F-B5⟧ Never `failed / None / None`. P5.4d's whole premise is
            # that a failure says which failure it was, and a run left with no
            # terminal event at all is the one case that could still answer
            # nothing -- so it answers this, and names the state it was in.
            category, detail = UNTERMINATED, str(state)
        return TurnOutcome(thread_id, run_id, OUTCOME_FAILED, category, detail)

    def _failure(self, run_id: str) -> tuple[str | None, str | None]:
        """The typed reason the terminal event already recorded, never invented.

        ⟦P5.4d⟧ Both halves, because one of them is useless alone: every
        managed turn that cannot reach a model is `runtime_execution_failed`,
        and the detail is the only thing that says which of the many ways it
        got there this was.
        """

        try:
            events = self._store.list_run_events(run_id)
        except ControlStoreError:
            return None, None
        for event in reversed(events):
            if str(event.get("type", "")).startswith("run."):
                payload = event.get("payload") or {}
                category = payload.get("category")
                if category:
                    detail = payload.get("detail")
                    return str(category), (str(detail) if detail else None)
        return None, None

    # -- the managed runtime ----------------------------------------------

    def _managed_orchestrator(self, *, window_required: bool) -> Any:
        """A `RunOrchestrator` over the worker the transport is already bound to.

        The same sandboxed process, the same seatbelt, the same certified slot
        and the same ledger flock -- a second `ManagedHermesBackend` would
        launch a second worker and the two would fight over one lock. The
        release authority is the updater service the binding already holds, so
        the pin an attempt commits is the pin the running worker measured.

        ⟦P8⟧ `window_required` is the one difference between the turn's form
        and recovery's: a turn may launch the worker outside a transport window
        (provider key, no bot token), recovery may not.
        """

        from ...runtime.hermes import HermesAdapter
        from ..orchestration import RunOrchestrator
        from ..research.service import ResearchService

        backend = self._worker.backend(window_required=window_required)
        return RunOrchestrator(
            self._store,
            HermesAdapter(managed=True, backend_loader=lambda: backend),
            self._worker.releases,
            actor_id=f"{self._actor_id}:{uuid.uuid4().hex[:8]}",
            research=ResearchService(self._store),
        )

    # -- restart recovery --------------------------------------------------

    def recover(self, *, attempt: int = 1) -> list[Mapping[str, object]]:
        """Converge attempts a previous daemon left mid-flight, once, at start.

        `RunOrchestrator.recover_startup` has existed since P2 and has never had
        a production caller, which is why a daemon that died inside a turn left
        the run in `running` for ever and the operator with no reply and no
        error. It replays no side effect: an attempt whose outcome is unknown
        becomes typed rather than dispatched again.
        """

        with self._lock:
            self._recovery_asks += 1
        # ⟦P8⟧ Before any worker is asked for: what a previous daemon left
        # never driven needs no worker to converge, and must not wait for a
        # window the installation may never open. The loop repeats the sweep
        # at its own cadence; this is the one that runs before the loop.
        self._sweep_undriven()
        try:
            orchestrator = self._recovery_factory()
        except ManagedWorkerUnavailable as exc:
            # Recovery must never be the reason a daemon fails to start. The
            # runs stay recoverable and the next start tries again -- but it
            # must not be SILENT either. ⟦P5.4d⟧ A daemon restarted mid-turn
            # left the run `running` for ever and scheduled no recovery command
            # at all, and this `except` is why nobody could say which of the
            # two halves had refused.
            if exc.reason == REFUSED_GATE_CLOSED:
                # ⟦c4, coordinator decision⟧ NOT a case for launching the
                # release anyway. A worker acquired outside a window holds no
                # bot token, and the reply a recovered turn produced could not
                # be delivered -- so the refusal stands and the attempt is
                # ended typed instead, with the reason on its own event. The
                # deferral is remembered; the window opening is what resumes it.
                self._defer_recoverable()
                self._record_recovery(
                    "deferred" if self._deferred else "converged",
                    REFUSED_GATE_CLOSED if self._deferred else None,
                    0,
                    attempt,
                )
                return []
            self._record_recovery("refused", exc.reason, 0, attempt)
            return []
        except Exception as exc:  # noqa: BLE001 - see above
            self._record_recovery("unavailable", failure_detail(exc), 0, attempt)
            return []
        self._reconcile_runtime_actions(orchestrator)
        try:
            reports = asyncio.run(orchestrator.recover_startup())
        except Exception as exc:  # noqa: BLE001 - see above
            from ..research.context import ResearchFailure

            if isinstance(exc, ResearchFailure) and exc.category == "research_materialization_pending":
                self._record_recovery("deferred", exc.category, 0, attempt)
                return []
            self._record_recovery("failed", failure_detail(exc), 0, attempt)
            return []
        self._resume_deferred()
        pending_research = any(report.get("outcome") == "research_materialization_pending" for report in reports)
        self._record_recovery("deferred" if pending_research else "converged",
                              "research_materialization_pending" if pending_research else None,
                              len(reports), attempt)
        return reports

    def _reconcile_runtime_actions(self, orchestrator: Any) -> None:
        """Settle actions a previous daemon delivered without hearing the answer.

        ⟦P9-3⟧ `reconcile_pending_runtime_actions` asks the adapter what became
        of every action left `outcome_unknown`, or holding a reconciliation
        lease that expired with the daemon that took it, and records the
        acknowledgement, the rejection or a further deferral. Like
        `recover_startup` before P5.4d it had no production caller at all, so
        a cancel whose outcome the delivering daemon never learned stayed
        unknown for the life of the installation and its action row was never
        settled by anything.

        Here rather than in the turn loop because it is the restart's
        question: the turn that delivered the action is the one that knows how
        it went, and the only actions left needing this are the ones whose
        turn is gone. Best effort and never fatal -- recovery's own
        convergence is what a daemon's start depends on, and an adapter that
        cannot answer yet is asked again at the next retry.
        """

        try:
            settled = asyncio.run(
                orchestrator.reconcile_pending_runtime_actions(
                    worker_id=self._actor_id
                )
            )
        except Exception as exc:  # noqa: BLE001 - see above
            _log.warning("action reconcile: refused (%s)", failure_detail(exc))
            return
        if settled:
            _log.info("action reconcile: settled %d runtime action(s)", len(settled))

    def _recoverable(self) -> list[Mapping[str, Any]]:
        try:
            return [
                run
                for run in self._store.list_recoverable_runs()
                if str(run["state"]) in DEFERRABLE_STATES
            ]
        except ControlStoreError:
            return []

    def _defer_recoverable(self) -> list[str]:
        """End each interrupted attempt typed, and remember it for the window.

        The transition is the audit row: `run.failed` carrying
        `category=recovery_deferred`, `detail=transport_gate_closed` and
        `retryable=true`. Without it the run stays `running` for the life of
        the installation, which is what c4 observed on a real restart.
        """

        deferred: list[str] = []
        for run in self._recoverable():
            run_id = str(run["id"])
            if run_id in self._deferred:
                continue
            attempt_id = str(run.get("active_attempt_id") or "")
            if not attempt_id:
                continue
            try:
                record = self._store.get_attempt(attempt_id)
                binding_id = record.get("runtime_binding_id")
                if not binding_id:
                    # Reserved but never bound: not this branch's shape, and
                    # `recover_startup` converges it when the window opens.
                    continue
                self._store.apply_runtime_transition(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    runtime_binding_id=str(binding_id),
                    runtime_release_id=str(record["runtime_release_id"]),
                    state_generation_id=record.get("state_generation_id"),
                    target_state="failed",
                    expected_revision=int(run["revision"]),
                    actor_id=self._actor_id,
                    idempotency_key=f"bridge-defer-{run_id}-{run['revision']}"[:64],
                    payload={
                        "category": RECOVERY_DEFERRED,
                        "retryable": True,
                        "detail": REFUSED_GATE_CLOSED,
                    },
                )
            except Exception:  # noqa: BLE001 - one run must not stop the rest
                continue
            self._deferred.add(run_id)
            deferred.append(run_id)
        return deferred

    def _resume_deferred(self) -> int:
        """A window opened: re-attempt what the shut one could not recover.

        `retry_run` is the operator's own vocabulary -- a new attempt on the
        same run -- and the thread is submitted so the bridge's loop dispatches
        it rather than waiting for another inbound message. A duplicate reply
        is impossible for a reason that is not a rule here: the interrupted
        attempt never produced a delivery, and the ledger is idempotent on the
        delivery key for the one this attempt will.
        """

        resumed = 0
        for run_id in sorted(self._deferred):
            self._deferred.discard(run_id)
            try:
                run = self._store.get_run(run_id)
                if str(run["state"]) != "failed":
                    continue
                self._store.retry_run(
                    run_id=run_id,
                    expected_revision=int(run["revision"]),
                    actor_id=self._actor_id,
                    idempotency_key=f"bridge-resume-{run_id}-{run['revision']}"[:64],
                    reason="recovery deferred while the transport window was shut",
                )
            except Exception:  # noqa: BLE001 - a run that cannot retry stays typed
                continue
            resumed += 1
            self.submit(str(run["thread_id"]))
        self._resumed += resumed
        return resumed

    def _record_recovery(
        self, state: str, reason: str | None, converged: int, attempt: int
    ) -> None:
        with self._lock:
            self._recovery = {
                "state": state,
                "reason": reason,
                "converged": converged,
                "attempts": attempt,
                "asks": self._recovery_asks,
                "deferred": len(self._deferred),
                "resumed": self._resumed,
            }

    # -- runs nothing is driving -------------------------------------------

    def _undriven(self, run: Mapping[str, Any]) -> tuple[str, str | None] | None:
        """The attempt of a run this bridge may drive and nobody is, or None.

        ⟦P8⟧ Two questions, and the first is whether it is a conversation run
        at all (`ControlStore.run_is_conversation`): the capture consumer's
        carrier run (`capture_consumer._carrier`) has exactly the never-
        reserved, never-bound shape below, and this bridge must neither drive
        it (a Hermes turn on a machine thread, launching the worker at daemon
        start) nor end it (a terminal run fences its whole workflow). What
        excludes it is durable on rows every generation wrote: the run owns a
        workflow instance (⟦V-3⟧ `create_run(workflow=...)` installs it in
        the transaction that creates a gen-13 carrier), OR its thread is a
        machine thread (⟦V6-2⟧ `ControlStore.thread_is_machine`: its
        `create_thread` receipt carries the capture consumer's actor, or a
        thread any run of which ever carried a workflow) -- the second is
        what covers a carrier a pre-V-3 consumer left between its two
        transactions with no workflow row at all. The thread's messages
        decide nothing here -- the
        API stores an operator's message on a `capture` thread (and declines
        to submit it), so "no user message at any moment" is not a fact about
        carrier threads and is not relied on. This bridge drives conversation
        runs and only those, from every entry.

        Then whether anybody is driving it, which is a durable fact with two
        halves plus a process-local one: the attempt is not bound
        (`runtime_binding_id` is NULL); it is not reserved by a LIVE lease
        (`dispatch_owner` is set by `reserve_attempt_dispatch` and cleared by
        `pin_attempt_runtime`, so it is NULL both before and after -- which is
        why the binding half is needed -- and a lease past `dispatch_expires_at`
        is a dead dispatch, which the store itself lets a new reservation
        replace); and it is not a run this loop is dispatching right now,
        between `dispatch()` starting and its reservation committing.

        Returns the attempt id and the expired owner if there was one, so an
        ending can be fenced on the exact lease it is retiring.
        """

        run_id = str(run["id"])
        with self._lock:
            if run_id in self._driving:
                return None
        try:
            if not self._store.run_is_conversation(run_id):
                return None
        except ControlStoreError:
            return None
        attempt_id = str(run.get("active_attempt_id") or "")
        if not attempt_id:
            return None
        try:
            record = self._store.get_attempt(attempt_id)
        except ControlStoreError:
            return None
        if record.get("runtime_binding_id"):
            return None
        owner = record.get("dispatch_owner")
        if owner and not _lease_expired(record.get("dispatch_expires_at")):
            return None
        return attempt_id, (str(owner) if owner else None)

    def _end_undriven(
        self,
        run: Mapping[str, Any],
        attempt_id: str,
        owner: str | None,
        *,
        category: str,
        key: str,
    ) -> bool:
        """`fail_unbound_run`, the transition for exactly this shape.

        The same primitive `RunOrchestrator._converge_unbound_cancellation`
        uses, fenced on the owner when there is an expired one to fence on. A
        run that moved under us (a dispatch that reserved it in the same
        instant) fails the revision or owner check and stays where that
        dispatch will take it.
        """

        try:
            self._store.fail_unbound_run(
                run_id=str(run["id"]),
                attempt_id=attempt_id,
                expected_revision=int(run["revision"]),
                category=category,
                actor_id=self._actor_id,
                idempotency_key=f"bridge-{key}-{run['id']}-{run['revision']}"[:64],
                dispatch_owner=owner,
            )
        except Exception:  # noqa: BLE001 - a run that moved under us stays put
            return False
        return True

    def _refuse_undriven(self, thread_id: str) -> str | None:
        """Under a closed gate: name the thread's never-driven run, and end it
        typed when nothing could deliver the ending.

        Returns the run id when there was one. On a thread with a transport
        binding nothing is written -- the `run.failed` would be delivered, and
        the gate's promise is that nothing more is sent -- so the run stays
        `queued` for the next submit after the gate opens. Elsewhere it ends
        with `retryable` true (that is `fail_unbound_run`'s rule for anything
        but a cancellation), which is exactly right: enabling dispatch and
        retrying is what happens next.
        """

        try:
            thread = self._store.get_thread(thread_id)
            active = thread.get("active_run_id")
            if not active:
                return None
            run = self._store.get_run(str(active))
        except ControlStoreError:
            return None
        if str(run["state"]) not in DISPATCHABLE_RUN_STATES:
            return None
        undriven = self._undriven(run)
        if undriven is None:
            return None
        run_id = str(run["id"])
        try:
            if self._store.thread_has_transport_binding(thread_id):
                return run_id
        except ControlStoreError:
            # Unknown is not "nothing can deliver": leave it standing.
            return run_id
        self._end_undriven(
            run, *undriven, category=REFUSED_DISPATCH_DISABLED, key="gate"
        )
        return run_id

    def _sweep_undriven(self) -> None:
        """What a previous daemon, or a refusing worker, left with nobody to move it.

        ⟦P8⟧ `recover_startup` converges attempts a daemon died INSIDE; a run
        it never started -- created through the control API and left `queued`,
        or cancelled before anything reserved it -- was not its shape, so a
        restart found it and left it for ever. Store-only and before any worker
        is asked for, because neither case needs one: a dispatchable run is
        handed to the loop (which ends it typed if the gate is shut and nothing
        could deliver), and a cancellation with nothing to cancel is `canceled`.

        Guarded per run, never per process: a run is handed over once, and
        again only after the worker refused its turn (the loop drops the guard
        then, and re-drives the run itself at its cadence) -- so a `cortex
        runtime approve` re-drives it without a restart. A thread already
        queued is skipped rather than remembered as a follow-up. The guard is
        set only for a run actually enqueued, and only after the store
        answered; a store that could not be asked is reported as such rather
        than counted as nothing to do. Runs the engine owns are excluded in
        the query itself (`conversations_only`), so they are never projected
        here, let alone re-queried.

        Runs at daemon start and on recovery's own retries; never sampled for
        the life of the daemon.
        """

        try:
            runs = self._store.list_recoverable_runs(conversations_only=True)
        except ControlStoreError as exc:
            with self._lock:
                self._undriven_counts["error"] = failure_detail(exc)
            _log.warning("start sweep: the store could not be asked (%s)", exc.category)
            return
        with self._lock:
            self._undriven_counts["error"] = None
            present = {str(run["id"]) for run in runs}
            self._swept &= present
            self._submitted &= present
        for run in runs:
            run_id = str(run["id"])
            state = str(run["state"])
            if state in STALLED_STATES:
                self._converge_stalled(run)
            elif state in DISPATCHABLE_RUN_STATES:
                with self._lock:
                    if run_id in self._swept:
                        continue
                if self._undriven(run) is None:
                    continue
                if self._enqueue(str(run["thread_id"]), followup=False, run_id=run_id):
                    with self._lock:
                        self._swept.add(run_id)
                        if run_id in self._submitted:
                            self._undriven_counts["resubmitted"] += 1  # type: ignore[operator]
                            decision = "resubmitted"
                        else:
                            self._submitted.add(run_id)
                            self._undriven_counts["submitted"] += 1  # type: ignore[operator]
                            decision = "submitted"
                    _log.info(
                        "start sweep: run=%s thread=%s %s to the loop",
                        run_id,
                        run["thread_id"],
                        decision,
                    )

    def _redrive_refused(self) -> None:
        """Hand the runs the worker refused over again -- the same runs, by id.

        ⟦V-1⟧ Each tick re-reads every remembered run first and drops the
        entry unless the run still exists, is still dispatchable, is still
        its thread's active run and is still a conversation run. ⟦N-4⟧ What
        that costs, per remembered run: the run and thread rows by primary
        key, then `run_is_conversation`, whose machine-thread half walks the
        whole `runs` table for that thread -- `runs.thread_id` carries only
        the partial one-active index, which SQLite cannot use for that
        probe, and the plain index that would serve it is a schema
        migration this program does not make. Measured at a few
        milliseconds up to tens of thousands of runs, on the database file
        the turn loop and the API share (a fresh connection per call,
        contending on the same write lock), once per 3 s tick per
        remembered run;
        never the `list_recoverable_runs` projection the sweep performs,
        and nothing here is written. An operator's
        cancel takes a run out of `queued` by a route this loop never
        observes; before this, the entry outlived the cancel and the thread
        -- now idle -- was handed over as a thread, so `_run_for` created a
        fresh run on every tick. What is handed over now is the run id, and
        a handover never creates a run.
        """

        with self._lock:
            pending = list(self._redrive.items())
        for run_id, thread_id in pending:
            try:
                run = self._store.get_run(run_id)
                still = (
                    str(run["state"]) in DISPATCHABLE_RUN_STATES
                    and str(run["thread_id"]) == thread_id
                    and str(self._store.get_thread(thread_id).get("active_run_id") or "")
                    == run_id
                    and self._store.run_is_conversation(run_id)
                )
            except ControlStoreError:
                still = False
            if not still:
                with self._lock:
                    self._redrive.pop(run_id, None)
                _log.info("re-drive dropped run=%s: no longer its thread's conversation run", run_id)
                continue
            if self._enqueue(thread_id, followup=False, run_id=run_id):
                with self._lock:
                    self._swept.add(run_id)
                    self._undriven_counts["resubmitted"] += 1  # type: ignore[operator]


def _seconds(value: float) -> str:
    """A duration for a durable payload a human reads back.

    ⟦P9-3 BRK-6⟧ `int()` turned every sub-second floor into "within 0s", which
    is not a duration anybody waited. `%g` keeps 30.0 as "30" and 0.5 as
    "0.5", so the sentence is true at both ends of the configurable range.
    """

    return f"{value:g}s"


def _older_than(value: object, seconds: float) -> bool:
    """Whether a store timestamp is further in the past than `seconds`.

    The store writes every one of them as `_format_time` does (ISO 8601, `Z`),
    which is what `_lease_expired` below reads too.
    """

    if not value:
        return False
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _lease_expired(value: object) -> bool:
    """Whether a dispatch lease's `dispatch_expires_at` is in the past.

    The store writes it as `_format_time` does (ISO 8601, `Z`) and lets a new
    reservation replace an owner whose lease is `<= now`; the same instant is
    the answer here.
    """

    if not value:
        return False
    try:
        expires = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)

__all__ = [
    "InboundTurnBridge",
    "RECOVERY_DEFERRED",
    "TURN_ABANDONED",
    "TURN_TIMEOUT",
    "UNTERMINATED",
    "TurnOutcome",
    "OUTCOME_ANSWERED",
    "OUTCOME_CANCELED",
    "OUTCOME_DECISION_REQUIRED",
    "OUTCOME_FAILED",
    "OUTCOME_IN_FLIGHT",
    "OUTCOME_REFUSED",
    "REFUSED_DISPATCH_DISABLED",
    "REFUSED_NOT_CONVERSATION",
    "REFUSED_QUEUE_FULL",
]
