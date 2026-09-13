"""Translate RuntimePort effects into transactional Cortex control commands."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from ...runtime.models import (
    FAILURE_DETAIL_LIMIT,
    ActionOutcomeQuery,
    ActionOutcomeStatus,
    AttemptRequest,
    ControlAction,
    ControlRequest,
    DecisionResolution,
    EventDurability,
    ReleasePin,
    RuntimeBinding,
    RuntimeCheckpoint,
    RuntimeEvent,
    SessionOpenRequest,
    failure_detail,
)
from ...runtime.port import RuntimePort
from ..control import (
    CANCELED_BEFORE_BINDING,
    CommandResult,
    ControlStore,
    InvalidTransition,
    RevisionConflict,
)


class ReleasePinPort(Protocol):
    def preview_attempt_pin(self, attempt_id: str) -> Any: ...

    def pin_attempt(self, attempt_id: str, expected_pin: Any) -> Any: ...

    def finish_attempt(self, attempt_id: str, pin: Any) -> None: ...


#: The projection caps a Telegram body at 1200 characters; the ledger keeps a
#: little more so a longer answer is truncated where it is rendered rather than
#: where it is recorded, and no turn can write an unbounded row.
SUMMARY_LIMIT = 4000

#: ⟦P9⟧ How many times one durable runtime event is offered to the ledger
#: before the dispatch gives up on it. A run's revision is a shared
#: optimistic-concurrency token and the streaming dispatch is not its only
#: legitimate writer -- `deliver_runtime_action` acknowledges the very
#: decision the run resumed from, and the store's own
#: `_confirm_runtime_resume_from_event` says either of them may be the one
#: that records it. Each competing writer is a bounded one-shot write, so
#: re-reading the run and offering the same event again converges; the
#: ceiling is what stops a pathological writer from spinning here instead.
EVENT_APPLY_ATTEMPTS = 4
#: ⟦P9⟧ What a run the operator cancelled mid-turn is called when the worker's
#: completion arrives after the cancel: converged to `canceled`, its late
#: answer discarded, rather than ended `failed / runtime_dispatch_failed` over
#: the transition the answer could no longer take.
CANCELED_AFTER_RUNTIME_COMPLETION = "runtime_completed_after_cancel"
#: ⟦P9⟧ What a run the operator cancelled mid-turn is called when the worker
#: asks for a DECISION after the cancel: the same operator decision, converged
#: the same way, but named apart from the completion because the worker is
#: still parked on the question when it happens and the request itself is then
#: refused rather than discarded.
CANCELED_AFTER_RUNTIME_DECISION = "runtime_decision_after_cancel"
#: ⟦P9 / ADJ9-1⟧ What a run the operator cancelled during the `starting`
#: window is called when the worker's durable start arrives afterwards. Its
#: own name because it is the only one of the three that ends the turn BEFORE
#: the worker is launched at all.
CANCELED_AFTER_RUNTIME_START = "runtime_started_after_cancel"
#: ⟦P9-2⟧ How many times a pin-release settlement is re-offered against the
#: run's current revision. `EVENT_APPLY_ATTEMPTS`' twin, for the same reason
#: and with the same ceiling: the settlement is fenced by
#: `(release_action_id, claim_owner, claim_epoch)`, so re-reading the run and
#: offering it again converges, and the bound is what stops a run whose
#: revision never settles from spinning here.
PIN_RELEASE_ATTEMPTS = 4
#: ⟦P9-3⟧ The pause siblings of the two arms a pause can actually reach. A
#: paused conversation turn converges to `canceled`, never to `paused` -- that
#: target demands a checkpoint the turn never wrote -- so the category is the
#: only thing that tells an operator their `/pause`, and not a `/cancel` and
#: not a failure, is why the turn stopped. There is no `started` sibling
#: because `_RUN_TRANSITIONS` gives `starting` no `pause_requested` edge: a run
#: can only be paused once it is already `running`.
CANCELED_AFTER_PAUSE_COMPLETION = "runtime_completed_after_pause"
CANCELED_AFTER_PAUSE_DECISION = "runtime_decision_after_pause"
#: ⟦P9-3 BRK-1⟧ What a run the operator stopped is called when the worker
#: reports FAILURE afterwards. Cancelling a live turn aborts arbitrary agent
#: code -- a provider stream, a tool subprocess -- and the shipped worker's
#: `except BaseException` arm reports that as `failed`, not as `TurnCanceled`
#: (`worker_payload/cortex_worker/turn.py`, the outcome branch). So the
#: ordinary consequence of a delivered cancel reached the failure path and
#: ended the run `failed / runtime_execution_failed / retryable: true` -- a
#: Telegram notification with a Retry button for a turn the operator stopped
#: on purpose.
CANCELED_AFTER_RUNTIME_FAILURE = "runtime_failed_after_cancel"
CANCELED_AFTER_PAUSE_FAILURE = "runtime_failed_after_pause"
#: ⟦P9-3⟧ The two states an operator's stop request leaves a LIVE run in.
#: `/pause` is reachable from Telegram and from the API and had none of the
#: discipline below, so a worker that asked for an approval after a pause
#: still ended the run `failed / runtime_dispatch_failed / retryable: true`.
_CONVERGING_STATES = frozenset({"cancel_requested", "pause_requested"})
#: What the operator asked for, for the detail sentence each arm writes.
_STOP_WORD = {"cancel_requested": "cancel", "pause_requested": "pause"}
#: Which category each arm writes, by the state the request left the run in.
_COMPLETED_AFTER_STOP = {
    "cancel_requested": CANCELED_AFTER_RUNTIME_COMPLETION,
    "pause_requested": CANCELED_AFTER_PAUSE_COMPLETION,
}
_FAILED_AFTER_STOP = {
    "cancel_requested": CANCELED_AFTER_RUNTIME_FAILURE,
    "pause_requested": CANCELED_AFTER_PAUSE_FAILURE,
}
_DECISION_AFTER_STOP = {
    "cancel_requested": CANCELED_AFTER_RUNTIME_DECISION,
    "pause_requested": CANCELED_AFTER_PAUSE_DECISION,
}
#: The durable non-terminal events a stopped run no longer applies.
#: Two rules rather than one, and the difference is worth writing down: a
#: discarded event terminates NOTHING (which is what the two arms below exist
#: for), so an event that can turn out to be the last one a run ever hears
#: does not belong in this set.
#:
#: `runtime.message.completed` and `runtime.session_rebound` are safe by
#: construction. The adapter emits them only from inside its terminal block --
#: `terminal_seen = True` at runtime/hermes.py:1742, then the rebound at 1781
#: and the message at 1794 -- so the run's terminal event follows in the same
#: iteration of that loop and converges the run.
#:
#: `runtime.tool.started` and `runtime.tool.completed` are NOT safe that way.
#: They are ordinary signals (runtime/hermes.py:748, 780) and either can be the
#: last thing a worker that then goes quiet ever sends. ⟦P9-3⟧ That hole --
#: standing item 6(d) -- is no longer standing: it is closed OUTSIDE this set,
#: exactly as the previous slice prescribed, by the turn bridge's bounded
#: terminal floor for bound runs in a converging state
#: (`_end_quiet_after_control`), which writes `canceled` under its own
#: category rather than widening the bridge's shared `DEFERRABLE_STATES` --
#: which its restart recovery reads too, and which would have labelled the
#: outcome `turn_timeout / retryable: true`: exactly the dishonesty that
#: slice removed. So this set stays as it is, and nothing is added to it to
#: compensate.
_DISCARDED_AFTER_STOP = frozenset(
    {
        "runtime.message.completed",
        "runtime.tool.started",
        "runtime.tool.completed",
        "runtime.session_rebound",
    }
)


def _key(namespace: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"{namespace}-{digest[:48]}"


def _binding_model(value: dict[str, Any]) -> RuntimeBinding:
    return RuntimeBinding(
        adapter_id=str(value["adapter_id"]),
        runtime_session_ref=str(value["runtime_session_ref"]),
        generation=int(value["generation"]),
        adapter_version=str(value["adapter_version"]),
        parent_runtime_session_ref=value.get("parent_ref"),
    )


def _managed_pin(attempt_id: str, value: Any) -> ReleasePin:
    state_generation_id = getattr(
        value,
        "state_generation_id",
        getattr(value, "generation_id", None),
    )
    slot_id = getattr(value, "slot_id", getattr(value, "slot_digest", None))
    artifact_digest = getattr(
        value,
        "artifact_digest",
        getattr(value, "slot_digest", None),
    )
    worker_protocol = getattr(value, "worker_protocol", None)
    if not all(
        (
            getattr(value, "release_id", None),
            state_generation_id,
            slot_id,
            artifact_digest,
            worker_protocol,
        )
    ):
        raise RuntimeError("runtime_pin_identity_incomplete")
    return ReleasePin(
        attempt_id=attempt_id,
        release_id=str(value.release_id),
        state_generation_id=str(state_generation_id),
        slot_id=str(slot_id),
        artifact_digest=str(artifact_digest),
        worker_protocol=str(worker_protocol),
    )


def _stored_pin(value: dict[str, Any]) -> ReleasePin:
    if value.get("runtime_identity_version") != 1:
        raise RuntimeError("legacy_runtime_identity")
    fields = {
        "release_id": value.get("runtime_release_id"),
        "state_generation_id": value.get("state_generation_id"),
        "slot_id": value.get("runtime_slot_id"),
        "artifact_digest": value.get("runtime_artifact_digest"),
        "worker_protocol": value.get("runtime_worker_protocol"),
    }
    if not all(isinstance(item, str) and item for item in fields.values()):
        raise RuntimeError("runtime_pin_identity_incomplete")
    return ReleasePin(
        attempt_id=str(value["attempt_id"] if "attempt_id" in value else value["id"]),
        release_id=str(fields["release_id"]),
        state_generation_id=str(fields["state_generation_id"]),
        slot_id=str(fields["slot_id"]),
        artifact_digest=str(fields["artifact_digest"]),
        worker_protocol=str(fields["worker_protocol"]),
    )


class RunOrchestrator:
    """Run one logical attempt while the control store remains authoritative."""

    def __init__(
        self,
        store: ControlStore,
        runtime: RuntimePort,
        releases: ReleasePinPort,
        *,
        actor_id: str = "runtime-orchestrator",
        research: Any = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.releases = releases
        self.actor_id = actor_id
        self.research = research
        #: ⟦P9-3 BRK-4⟧ The last capabilities this orchestrator was told, or
        #: None before its first dispatch. Read by the turn bridge so a
        #: control surface can refuse an action the runtime cannot perform.
        self.last_capabilities: Any = None

    def _checkpoint_source_pin(
        self, attempt: dict[str, Any]
    ) -> ReleasePin | None:
        if not attempt.get("source_checkpoint_uri"):
            return None
        source_attempt_id = attempt.get("source_attempt_id")
        visited: set[str] = set()
        while source_attempt_id is not None:
            source_attempt_id = str(source_attempt_id)
            if source_attempt_id in visited:
                raise RuntimeError("attempt_lineage_cycle")
            visited.add(source_attempt_id)
            source = self.store.get_attempt(source_attempt_id)
            if source["state"] == "paused" and source.get("checkpoint_uri"):
                expected = _stored_pin(source)
                load_pin = getattr(self.releases, "attempt_pin", None)
                if not callable(load_pin):
                    raise RuntimeError("pin_lookup_unavailable")
                managed = load_pin(source_attempt_id)
                if managed is None:
                    raise RuntimeError("checkpoint_source_pin_missing")
                if _managed_pin(source_attempt_id, managed) != expected:
                    raise RuntimeError("checkpoint_source_pin_mismatch")
                return expected
            source_attempt_id = source.get("source_attempt_id")
        raise RuntimeError("checkpoint_source_attempt_missing")

    def _converge_unbound_cancellation(
        self,
        *,
        run_id: str,
        attempt_id: str,
        dispatch_owner: str,
    ) -> dict[str, Any] | None:
        current = self.store.get_run(run_id)
        if current["state"] != "cancel_requested":
            return None
        return self.store.fail_unbound_run(
            run_id=run_id,
            attempt_id=attempt_id,
            expected_revision=int(current["revision"]),
            category="dispatch_canceled_before_binding",
            actor_id=self.actor_id,
            idempotency_key=_key(
                "unbound-canceled", run_id, attempt_id, dispatch_owner
            ),
            dispatch_owner=dispatch_owner,
        ).value

    async def dispatch(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["state"] not in {"queued", "retrying", "resuming"}:
            raise ValueError("run is not dispatchable")
        attempt_id = str(run["active_attempt_id"])
        dispatch_owner = f"{self.actor_id}:{uuid.uuid4().hex}"
        pin: Any | None = None
        runtime_pin: ReleasePin | None = None
        reserved = False
        bound = False
        terminal = False
        research_requested = False
        try:
            attempt = self.store.get_attempt(attempt_id)
            checkpoint_source_pin = self._checkpoint_source_pin(attempt)
            preview = self.releases.preview_attempt_pin(attempt_id)
            runtime_pin = _managed_pin(attempt_id, preview)
            if (
                checkpoint_source_pin is not None
                and runtime_pin.runtime_identity
                != checkpoint_source_pin.runtime_identity
            ):
                raise RuntimeError("checkpoint_runtime_identity_mismatch")
            release_id = runtime_pin.release_id
            state_generation_id = runtime_pin.state_generation_id
            try:
                run = self.store.reserve_attempt_dispatch(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dispatch_owner=dispatch_owner,
                    runtime_release_id=release_id,
                    state_generation_id=state_generation_id,
                    runtime_slot_id=runtime_pin.slot_id,
                    runtime_artifact_digest=runtime_pin.artifact_digest,
                    runtime_worker_protocol=runtime_pin.worker_protocol,
                    expected_revision=int(run["revision"]),
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "dispatch-reserve", run_id, attempt_id, dispatch_owner
                    ),
                ).value
                reserved = True
            except InvalidTransition as exc:
                if exc.source in {"already_reserved", "stale_dispatch_receipt"}:
                    return self.store.get_run(run_id)
                raise
            pin = self.releases.pin_attempt(attempt_id, preview)
            if _managed_pin(attempt_id, pin) != runtime_pin:
                raise RuntimeError("pin_identity_mismatch")
            attempt = self.store.get_attempt(attempt_id)
            # Before ANY runtime call. The health payload's `runtime_dispatch`
            # flag is a report nothing consumes, so this durable Control-owned
            # decision is the only thing standing between a certified runtime
            # and real effects. Checking it here means a disabled gate reaches
            # the runtime port zero times.
            if not self.store.runtime_dispatch_enabled():
                raise RuntimeError("runtime_activation_disabled")
            capabilities = await self.runtime.capabilities()
            # ⟦P9-3 BRK-4⟧ Kept so a surface can refuse a control action this
            # runtime cannot perform, instead of committing a request that
            # will be rolled back with nothing public to show for it. Recorded
            # where it is already asked for -- every dispatch asks -- so no
            # surface has to pay for a probe of its own.
            self.last_capabilities = capabilities
            canceled = self._converge_unbound_cancellation(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_owner=dispatch_owner,
            )
            if canceled is not None:
                return canceled
            if not capabilities.available:
                raise RuntimeError("runtime_unavailable")
            if not capabilities.durable_operation_deduplication:
                raise RuntimeError(
                    "runtime_durable_deduplication_unavailable"
                )
            health = await self.runtime.health()
            canceled = self._converge_unbound_cancellation(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_owner=dispatch_owner,
            )
            if canceled is not None:
                return canceled
            handshake = await self.runtime.handshake(runtime_pin)
            canceled = self._converge_unbound_cancellation(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_owner=dispatch_owner,
            )
            if canceled is not None:
                return canceled
            if not handshake.verified or health.runtime_identity != runtime_pin.runtime_identity:
                raise RuntimeError(handshake.reason_code or "runtime_identity_mismatch")

            binding_row = self.store.latest_runtime_binding(
                thread_id=str(run["thread_id"]),
                adapter_id=capabilities.adapter_id,
            )
            history = self.store.list_messages(str(run["thread_id"]))
            canceled = self._converge_unbound_cancellation(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_owner=dispatch_owner,
            )
            if canceled is not None:
                return canceled
            if binding_row is None:
                binding = await self.runtime.open_session(
                    SessionOpenRequest(
                        metadata={
                            "thread_id": run["thread_id"],
                            "attempt_id": attempt_id,
                            "dispatch_owner": dispatch_owner,
                        },
                        adapter_operation_id=_key(
                            "session-open", run_id, attempt_id
                        ),
                    )
                )
                canceled = self._converge_unbound_cancellation(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dispatch_owner=dispatch_owner,
                )
                if canceled is not None:
                    return canceled
                binding_row = self.store.create_runtime_binding(
                    thread_id=str(run["thread_id"]),
                    adapter_id=binding.adapter_id,
                    runtime_session_ref=binding.runtime_session_ref,
                    generation=binding.generation,
                    adapter_version=binding.adapter_version,
                    parent_ref=binding.parent_runtime_session_ref,
                    actor_id=self.actor_id,
                    idempotency_key=_key("binding", run_id, attempt_id),
                ).value
            else:
                binding = _binding_model(binding_row)
                source_checkpoint = attempt.get("source_checkpoint_uri")
                if source_checkpoint:
                    binding = await self.runtime.recover(
                        binding,
                        RuntimeCheckpoint(
                            checkpoint_ref=str(source_checkpoint),
                            conversation_history=tuple(history),
                            adapter_operation_id=_key(
                                "session-recover", run_id, attempt_id
                            ),
                        ),
                    )
                    canceled = self._converge_unbound_cancellation(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        dispatch_owner=dispatch_owner,
                    )
                    if canceled is not None:
                        return canceled
                    if (
                        binding.runtime_session_ref
                        != binding_row["runtime_session_ref"]
                        or binding.generation != binding_row["generation"]
                    ):
                        binding_row = self.store.create_runtime_binding(
                            thread_id=str(run["thread_id"]),
                            adapter_id=binding.adapter_id,
                            runtime_session_ref=binding.runtime_session_ref,
                            generation=binding.generation,
                            adapter_version=binding.adapter_version,
                            parent_ref=binding.parent_runtime_session_ref,
                            actor_id=self.actor_id,
                            idempotency_key=_key(
                                "recovery-binding", run_id, attempt_id
                            ),
                        ).value
                else:
                    binding = await self.runtime.load_session(binding)
                    canceled = self._converge_unbound_cancellation(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        dispatch_owner=dispatch_owner,
                    )
                    if canceled is not None:
                        return canceled
                    if (
                        binding.runtime_session_ref
                        != binding_row["runtime_session_ref"]
                        or binding.generation != binding_row["generation"]
                    ):
                        binding_row = self.store.create_runtime_binding(
                            thread_id=str(run["thread_id"]),
                            adapter_id=binding.adapter_id,
                            runtime_session_ref=binding.runtime_session_ref,
                            generation=binding.generation,
                            adapter_version=binding.adapter_version,
                            parent_ref=binding.parent_runtime_session_ref,
                            actor_id=self.actor_id,
                            idempotency_key=_key(
                                "loaded-binding", run_id, attempt_id
                            ),
                        ).value

            run = self.store.pin_attempt_runtime(
                run_id=run_id,
                attempt_id=attempt_id,
                runtime_binding_id=str(binding_row["id"]),
                runtime_release_id=release_id,
                state_generation_id=state_generation_id,
                expected_revision=int(run["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key("pin", run_id, attempt_id),
                dispatch_owner=dispatch_owner,
            ).value
            bound = True
            try:
                self.deliver_pending_pin_releases(worker_id=self.actor_id)
            except Exception:
                pass
            run = self.store.get_run(run_id)
            run = self.store.apply_runtime_transition(
                run_id=run_id,
                attempt_id=attempt_id,
                runtime_binding_id=str(binding_row["id"]),
                runtime_release_id=release_id,
                state_generation_id=state_generation_id,
                target_state="starting",
                expected_revision=int(run["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key("starting", run_id, attempt_id),
            ).value

            research_context = None
            if self.research is not None:
                from ..research.context import mode_for

                research_requested = (self.store.get_research_context(run_id) is not None
                                      or mode_for(history)[0] is not None)
                research_context = self.research.prepare(run, history)
                run = self.store.get_run(run_id)
                if run["state"] != "starting" or run["active_attempt_id"] != attempt_id:
                    raise RuntimeError("research_preparation_interrupted")
            user_messages = [item for item in history if item["role"] == "user"]
            if not user_messages:
                raise RuntimeError("run_has_no_user_message")
            latest_user = user_messages[-1]
            if research_context is not None:
                latest_user = next(item for item in user_messages if item["id"] == research_context["message_id"])
            request = AttemptRequest(
                run_id=run_id,
                attempt_id=attempt_id,
                binding=binding,
                user_message=str(latest_user["content"]),
                system_message=(self.research.system_message(research_context)
                                if self.research is not None else None),
                conversation_history=tuple(
                    {
                        "role": item["role"],
                        "content": item["content"],
                    }
                    for item in history
                    if item["id"] != latest_user["id"]
                ),
                metadata={
                    "runtime_release_id": release_id,
                    "state_generation_id": state_generation_id,
                    **({"cortex_research_prompt_mode": "ephemeral_v1"}
                       if self.research is not None else {}),
                },
                adapter_operation_id=_key(
                    "attempt-execute", run_id, attempt_id
                ),
            )
            reply: str | None = None
            async for event in self.runtime.execute(request):
                # ⟦P5.4c⟧ The turn's answer, remembered on its way past. The
                # runtime emits it as its own durable event and the terminal
                # event that follows carries no body, so a transport projecting
                # `run.completed` had nothing to render. Kept here rather than
                # read back out of the thread afterwards: the transition and
                # the message it describes then commit from the same fact.
                if (
                    event.durability == EventDurability.DURABLE
                    and event.type == "runtime.message.completed"
                    and str(event.payload.get("role", "assistant")) == "assistant"
                ):
                    content = str(event.payload.get("content", "")).strip()
                    if content:
                        reply = content
                run = self._apply_event(
                    run,
                    event,
                    binding_row=binding_row,
                    release_id=release_id,
                    state_generation_id=state_generation_id,
                    reply=reply,
                )
                if run["state"] in {"completed", "failed", "canceled"}:
                    terminal = True
            if not terminal and run["state"] != "waiting_for_decision":
                raise RuntimeError("runtime_stream_ended_without_terminal_event")
            return run
        except Exception as exc:
            current = self.store.get_run(run_id)
            if current["state"] not in {"completed", "failed", "canceled"}:
                category = (
                    str(exc)
                    if str(exc)
                    in {
                        "runtime_activation_disabled",
                        "runtime_unavailable",
                        "runtime_durable_deduplication_unavailable",
                        # The managed runtime's fourth typed refusal: the turn's
                        # outcome is unknown, which is terminal for the attempt
                        # and never an automatic retry.
                        "runtime_operation_uncertain",
                        "run_has_no_user_message",
                    }
                    else "runtime_dispatch_failed"
                )
                from ..research.context import ResearchFailure

                if isinstance(exc, ResearchFailure):
                    category = exc.category
                research_refusal = isinstance(exc, InvalidTransition) and exc.source in {
                    "research_source_not_ready", "research_authority_mismatch",
                    "research_message_not_owned", "research_message_not_current",
                    "research_context_integrity", "research_context_immutable",
                    "research_actor_not_owned",
                }
                if research_refusal:
                    category = exc.source
                stopped_research = research_requested and current["state"] in _CONVERGING_STATES
                if stopped_research:
                    category = _FAILED_AFTER_STOP[str(current["state"])]
                if bound:
                    attempt = self.store.get_attempt(attempt_id)
                    current = self.store.apply_runtime_transition(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        runtime_binding_id=str(attempt["runtime_binding_id"]),
                        runtime_release_id=str(attempt["runtime_release_id"]),
                        state_generation_id=attempt.get("state_generation_id"),
                        target_state="canceled" if stopped_research else "failed",
                        expected_revision=int(current["revision"]),
                        actor_id=self.actor_id,
                        idempotency_key=_key("dispatch-failed", run_id, attempt_id),
                        payload={
                            "category": category,
                            "retryable": not (stopped_research or research_refusal),
                            # ⟦P5.4d⟧ `runtime_dispatch_failed` is the catch-all
                            # of the whole dispatch arc; without the detail an
                            # operator cannot tell a ledger conflict from a
                            # runtime that never started.
                            "detail": failure_detail(exc),
                        },
                    ).value
                elif reserved:
                    try:
                        current = self.store.fail_unbound_run(
                            run_id=run_id,
                            attempt_id=attempt_id,
                            expected_revision=int(current["revision"]),
                            category=category,
                            retryable=not research_refusal,
                            actor_id=self.actor_id,
                            idempotency_key=_key(
                                "unbound-failed", run_id, attempt_id
                            ),
                            dispatch_owner=dispatch_owner,
                        ).value
                    except InvalidTransition as transition:
                        if transition.source != "dispatch_owner_mismatch":
                            raise
                        current = self.store.get_run(run_id)
                else:
                    return current
            return current
        finally:
            try:
                self.deliver_pending_pin_releases(worker_id=self.actor_id)
            except Exception:
                pass

    def _apply_event(
        self,
        run: dict[str, Any],
        event: RuntimeEvent,
        *,
        binding_row: dict[str, Any],
        release_id: str,
        state_generation_id: str,
        reply: str | None = None,
    ) -> dict[str, Any]:
        """Apply one durable runtime event, re-reading a revision it lost.

        ⟦P9⟧ `_apply_event_once` reads the run and then writes with the
        revision it read, so any other writer that commits inside that window
        takes the event's write down with `RevisionConflict` -- and the
        dispatch arc has no other answer for that than ending the run
        `runtime_dispatch_failed`, which throws a worker's finished answer
        away over a ledger race the store expects and handles. The competing
        writer is not a bug to remove: `_confirm_runtime_resume_from_event`
        exists so that EITHER the delivering caller's acknowledgement or the
        first event caused by it records that a decision was accepted, and
        `deliver_runtime_action` already converges when it is the side that
        loses (it re-reads the action and returns it acked).

        Offering the event again is safe by construction rather than by
        timing: `runtime_event_inbox` keys an event on
        `(attempt_id, adapter_event_id)` and hashes its request with
        `expected_revision` deliberately excluded, so a second offer of the
        same event is either the single write it always was or a replay --
        never a second assistant message, never a second transition. Nothing
        about optimistic concurrency is relaxed: every offer still carries a
        revision the store still checks, and an event the run has genuinely
        moved past still fails on its own transition rule rather than here.
        """

        remaining = EVENT_APPLY_ATTEMPTS
        while True:
            remaining -= 1
            try:
                return self._apply_event_once(
                    run,
                    event,
                    binding_row=binding_row,
                    release_id=release_id,
                    state_generation_id=state_generation_id,
                    reply=reply,
                )
            except RevisionConflict:
                # A run whose revision keeps moving under the dispatch is not
                # one this event can be applied to at all; the caller ends it
                # typed, exactly as it did before.
                if remaining <= 0:
                    raise

    def _apply_event_once(
        self,
        run: dict[str, Any],
        event: RuntimeEvent,
        *,
        binding_row: dict[str, Any],
        release_id: str,
        state_generation_id: str,
        reply: str | None = None,
    ) -> dict[str, Any]:
        run = self.store.get_run(str(run["id"]))
        attempt_id = str(run["active_attempt_id"])
        if event.run_id != run["id"] or event.attempt_id != attempt_id:
            raise RuntimeError("runtime_event_identity_mismatch")
        if event.durability != EventDurability.DURABLE:
            return run
        adapter_event_id = event.durable_idempotency_key
        if adapter_event_id is None:
            raise RuntimeError("durable_runtime_event_identity_missing")
        command_key = _key(
            "runtime-delivery", adapter_event_id, run["revision"]
        )
        common = {
            "run_id": str(run["id"]),
            "attempt_id": attempt_id,
            "runtime_binding_id": str(binding_row["id"]),
            "runtime_release_id": release_id,
            "state_generation_id": state_generation_id,
            "expected_revision": int(run["revision"]),
            "actor_id": self.actor_id,
            "idempotency_key": command_key,
            "adapter_event_id": adapter_event_id,
            "adapter_event_sequence": event.event_sequence,
            "caused_by_adapter_operation_id": (
                event.caused_by_adapter_operation_id
            ),
            "caused_by_delivery_epoch": event.caused_by_delivery_epoch,
        }
        if run["state"] in _CONVERGING_STATES:
            stopped = str(run["state"])
            stop_word = _STOP_WORD[stopped]
            # ⟦P9⟧ The operator stopped the run while the worker was still
            # answering (`running -> cancel_requested` is the API's own write,
            # on its own thread). ⟦P9-3⟧ Since this slice the worker IS told --
            # the queued control action is delivered by the turn loop -- but
            # the telling is not instantaneous and it is not guaranteed, so
            # every arm below remains the backstop for what arrives in the
            # window before it lands, or when it never does. A `/pause` is the
            # same shape and had none of this. ⟦P9-3 C1⟧ The arms below were
            # widened to `pause_requested` before the STORE was, so until this
            # fix a `/pause` plus one ordinary `runtime.tool.started` still
            # ended the turn `failed / runtime_dispatch_failed`: the discard
            # write below is the only observation a stopped run may take, and
            # its guard admitted `cancel_requested` alone. Both converging
            # states are admitted now -- `ControlStore.record_runtime_observation`,
            # the `run["state"] in {"cancel_requested", "pause_requested"}`
            # clause -- which is what makes the rest of this comment true for a
            # pause and not only for a cancel. What arrives afterwards
            # has no transition to take -- every non-terminal write requires
            # `running`, and a re-offer after the lost revision would end the
            # run `failed / runtime_dispatch_failed` over `InvalidTransition`
            # -- so a late answer, tool observation or rebound is DISCARDED:
            # recorded as `runtime.event.discarded` (type only, no body; the
            # inbox needs the attempt's event sequence contiguous), and the
            # completion that follows converges the run to `canceled` under
            # its own category, which is what the operator asked for.
            # ⟦P9-3 BRK-1⟧ A worker FAILURE reported after the stop converges
            # the same way, under its own category and with the worker's
            # detail carried through; this comment previously said the
            # opposite ("a worker failure still ends `failed`"), which is the
            # rule that put a Retry button on a turn the operator cancelled.
            # A worker cancel still ends `canceled`, unchanged.
            #
            # The two events that could be the LAST thing the run ever hears
            # -- the worker's start and its decision request -- are converged
            # terminally here instead and then left to be refused below, which
            # is what unwinds the stream and releases the worker.
            if event.type in _DISCARDED_AFTER_STOP:
                return self.store.record_runtime_observation(
                    **common,
                    event_type="runtime.event.discarded",
                    payload={"event_type": event.type},
                ).value
            if event.type == "runtime.run.completed":
                return self.store.apply_runtime_transition(
                    **common,
                    target_state="canceled",
                    payload={
                        "category": _COMPLETED_AFTER_STOP[stopped],
                        "retryable": False,
                        "detail": (
                            f"the worker completed after the operator's "
                            f"{stop_word}; its answer was discarded"
                        ),
                    },
                ).value
            if event.type == "runtime.run.failed":
                # ⟦P9-3 BRK-1⟧ The operator's stop wins over the worker's
                # outcome for a FAILURE exactly as it already does for a
                # completed answer, and this arm is the more important of the
                # two: a delivered cancel aborts whatever the agent was doing,
                # and the shipped worker reports an aborted provider stream or
                # tool subprocess through its `except BaseException` arm as
                # `failed` rather than as `TurnCanceled`. So failing here was
                # not the exotic case -- it was the ORDINARY consequence of
                # the cancel this slice now delivers, and it ended the turn
                # `failed / retryable: true`, which Telegram renders with a
                # Retry button (`run.failed` is a notification type;
                # `run.canceled` deliberately is not).
                #
                # The worker's own detail is carried through unchanged, so
                # nothing is hidden: the operator sees the run as canceled,
                # which is what they asked for, and can still read what the
                # worker said about why it stopped. Retryable is false for the
                # same reason it is false on the completed arm -- there is
                # nothing to retry when the turn ended because it was told to.
                payload = {
                    "category": _FAILED_AFTER_STOP[stopped],
                    "retryable": False,
                }
                detail = event.payload.get("detail")
                payload["detail"] = (
                    str(detail)[:FAILURE_DETAIL_LIMIT]
                    if detail
                    else (
                        f"the worker failed after the operator's {stop_word}"
                    )
                )
                return self.store.apply_runtime_transition(
                    **common,
                    target_state="canceled",
                    payload=payload,
                ).value
            if event.type == "runtime.run.started" and stopped == "cancel_requested":
                # ⟦P9 / ADJ9-1⟧ Cancel only, because `_RUN_TRANSITIONS` gives
                # `starting` no `pause_requested` edge: a run is only
                # pausable once it is already `running`, so a start can never
                # arrive under a pause.
                #
                # The operator cancelled while the run was
                # `starting` -- a legal edge the cockpit offers a button for,
                # and a window wide enough to hit rather than a millisecond
                # race: `starting` commits before `RuntimeAdapter.execute` is
                # called at all, and the adapter then loads its backend,
                # reserves the attempt and does the session handshake before
                # it yields `runtime.run.started`. Every bit of that latency,
                # a worker relaunch included, falls inside the window the
                # Cancel button is offered for.
                #
                # This event was briefly DISCARDED, which was wrong for the
                # same reason discarding a decision request is wrong, and
                # worse: a discarded event terminates nothing, and the start
                # is the last thing a worker that then goes quiet ever sends.
                # The run stayed `cancel_requested` for ever -- the bridge's
                # turn-timeout rescue, its restart recovery and its undriven
                # sweep all refuse a bound run outside {running, starting} --
                # holding the thread's `active_run_id`, the attempt's pin and
                # the drain loop's only turn slot, until the operator's next
                # message ended it `failed / turn_abandoned` and NOTIFIED them
                # of a failure for a turn they had cancelled.
                #
                # So it converges terminally here and falls through, exactly
                # as the decision arm below does. Falling through rather than
                # returning is load-bearing twice over: a terminal run dict
                # returned from here does NOT end the dispatch's `async for`
                # (the loop sets `terminal` and keeps reading), and the
                # adapter yields this event BEFORE it launches the worker, so
                # unwinding now means the worker is never started at all.
                self.store.apply_runtime_transition(
                    **common,
                    target_state="canceled",
                    payload={
                        "category": CANCELED_AFTER_RUNTIME_START,
                        "retryable": False,
                        "detail": (
                            "the worker started after the operator's cancel"
                        ),
                    },
                )
            if event.type == "runtime.decision.required":
                # ⟦P9 / A-2⟧ The worker asked for a tool approval AFTER the
                # cancel -- the ordinary shape of a tool-using turn, not a
                # corner: the shipped worker's approval callback emits this
                # unconditionally and then parks in `await_decision` with no
                # timeout, and nothing in production delivers the cancel that
                # would wake it. DISCARDING the event (the obvious symmetry
                # with the four above) is exactly what must not happen: the
                # run would stay `cancel_requested` with no terminal event
                # for ever -- the turn-timeout rescue refuses any state
                # outside {running, starting} -- the thread's `active_run_id`
                # would never clear, and the worker would stay parked.
                #
                # So the operator's cancel converges HERE, terminally, and
                # control then FALLS THROUGH to `create_decision` below on
                # purpose, where the write is refused. WHICH refusal fires is
                # worth naming exactly: the transition just above has already
                # consumed this `adapter_event_id` in the runtime event inbox,
                # so the second write of the same event is refused there --
                # `IdempotencyConflict`, on a different request hash -- before
                # `create_decision`'s `running`-only rule is ever reached.
                # That rule is the backstop for the interleaving where no
                # inbox row was written. Either way the exception is a
                # `ControlStoreError` that is not `RevisionConflict`, so it
                # escapes the retry above rather than being re-offered: the
                # dispatch's `async for` unwinds, the adapter generator's
                # `finally` sends `turn.cancel` and releases the parked
                # worker, and the dispatch's failure arm leaves an already
                # terminal run alone. The decision row is never created, which
                # is the point: there is nobody left to answer it.
                self.store.apply_runtime_transition(
                    **common,
                    target_state="canceled",
                    payload={
                        "category": _DECISION_AFTER_STOP[stopped],
                        "retryable": False,
                        "detail": (
                            f"the worker asked for a decision after the "
                            f"operator's {stop_word}"
                        ),
                    },
                )
        if event.type == "runtime.run.started":
            return self.store.apply_runtime_transition(
                **common,
                target_state="running",
                payload={"binding_generation": binding_row["generation"]},
            ).value
        if event.type in {"runtime.tool.started", "runtime.tool.completed"}:
            return self.store.record_runtime_observation(
                **common,
                event_type=event.type,
                payload=event.payload,
            ).value
        if event.type == "runtime.session_rebound":
            result = self.store.record_runtime_observation(
                **common,
                event_type=event.type,
                payload=event.payload,
            ).value
            rebound = self.store.get_runtime_binding(str(binding_row["id"]))
            binding_row.update(rebound)
            return result
        if event.type == "runtime.message.completed":
            content = str(event.payload.get("content", ""))
            if self.research is not None:
                content = self.research.annotate(str(run["id"]), content)
            return self.store.append_runtime_message(
                **common,
                content=content,
            ).value
        if event.type == "runtime.decision.required":
            options = event.payload.get("options", ())
            normalized_options = [
                {"id": str(option)} if not isinstance(option, dict) else option
                for option in options
            ]
            decision = self.store.create_decision(
                **common,
                kind=str(event.payload.get("kind", "approval")),
                prompt=str(
                    event.payload.get("prompt", "Runtime approval required")
                ),
                options=normalized_options,
                runtime_decision_ref=str(event.payload.get("decision_id", "")),
                runtime_decision_revision=int(event.payload.get("revision", 0)),
            )
            return self.store.get_run(str(decision.value["run_id"]))
        terminal = {
            "runtime.run.completed": "completed",
            "runtime.run.failed": "failed",
            "runtime.run.canceled": "canceled",
        }.get(event.type)
        if terminal is not None:
            research_result = None
            if terminal == "completed" and self.research is not None and run["state"] not in {"failed", "canceled"}:
                try:
                    research_result = self.research.persist_response(str(run["id"]), attempt_id)
                except InvalidTransition:
                    current = self.store.get_run(str(run["id"]))
                    if current["state"] in _CONVERGING_STATES:
                        return self._apply_event_once(
                            current, event, binding_row=binding_row, release_id=release_id,
                            state_generation_id=state_generation_id, reply=reply)
                    raise
                common["expected_revision"] = int(self.store.get_run(str(run["id"]))["revision"])
            payload = {
                "category": str(
                    event.payload.get("category", f"runtime_{terminal}")
                )[:100],
                "retryable": bool(event.payload.get("retryable", False)),
            }
            # ⟦P5.4d⟧ The category names the class of failure; the detail names
            # the failure. Carried through rather than derived, and bounded the
            # same way the category is: the adapter has already scrubbed it, and
            # this is a durable payload a status surface reads back.
            detail = event.payload.get("detail")
            if detail:
                payload["detail"] = str(detail)[:FAILURE_DETAIL_LIMIT]
            # Absent rather than empty when the turn said nothing: a projection
            # that renders `payload["summary"]` must be able to tell "no answer"
            # from "an answer that was blank".
            summary = event.payload.get("summary", reply)
            if summary:
                payload["summary"] = str(summary)[:SUMMARY_LIMIT]
            if research_result is not None:
                payload.update(research_result)
            return self.store.apply_runtime_transition(
                **common,
                target_state=terminal,
                payload=payload,
            ).value
        raise RuntimeError("unsupported_durable_runtime_event")

    async def deliver_runtime_action(
        self,
        action_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> dict[str, Any]:
        claimed = self.store.claim_runtime_action(
            action_id=action_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            actor_id=self.actor_id,
            idempotency_key=_key(
                "action-claim", action_id, worker_id, uuid.uuid4().hex
            ),
        ).value
        run = self.store.get_run(str(claimed["run_id"]))
        attempt = self.store.get_attempt(str(claimed["attempt_id"]))
        binding_row = self.store.get_runtime_binding(
            str(claimed["runtime_binding_id"])
        )
        claim_epoch = int(claimed["claim_epoch"])
        adapter_operation_id = f"runtime-action:{action_id}"
        invoked = False
        try:
            if claimed["kind"] == "decision.resolve":
                decision = self.store.get_decision(
                    str(claimed["payload"]["decision_id"])
                )
                invoked = True
                result = await self.runtime.resolve_decision(
                    DecisionResolution(
                        run_id=str(run["id"]),
                        attempt_id=str(attempt["id"]),
                        binding=_binding_model(binding_row),
                        decision_id=str(
                            claimed["payload"].get("runtime_decision_ref")
                            or decision["id"]
                        ),
                        choice=str(claimed["payload"]["choice"]),
                        revision=int(
                            claimed["payload"].get(
                                "runtime_decision_revision", 0
                            )
                        ),
                        adapter_operation_id=adapter_operation_id,
                        delivery_epoch=claim_epoch,
                    )
                )
            elif claimed["kind"] in {"control.pause", "control.cancel"}:
                action = (
                    ControlAction.PAUSE
                    if claimed["kind"] == "control.pause"
                    else ControlAction.CANCEL
                )
                invoked = True
                result = await self.runtime.request_control(
                    ControlRequest(
                        run_id=str(run["id"]),
                        attempt_id=str(attempt["id"]),
                        binding=_binding_model(binding_row),
                        action=action,
                        adapter_operation_id=adapter_operation_id,
                        delivery_epoch=claim_epoch,
                    )
                )
            else:
                raise RuntimeError("unsupported_runtime_action")
            settled = self.store.get_runtime_action(action_id)
            if settled["state"] == "acked":
                return settled
            if (
                result.adapter_operation_id != adapter_operation_id
                or result.delivery_epoch != claim_epoch
            ):
                current = self.store.get_run(str(run["id"]))
                return self.store.mark_runtime_action_outcome_unknown(
                    action_id=action_id,
                    claim_owner=worker_id,
                    claim_epoch=claim_epoch,
                    expected_revision=int(current["revision"]),
                    category="runtime_action_identity_unverified",
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "action-identity-unknown", action_id, claim_epoch
                    ),
                ).value
            outcome = result.outcome or (
                ActionOutcomeStatus.ACCEPTED
                if result.accepted
                else ActionOutcomeStatus.REJECTED
            )
            if outcome == ActionOutcomeStatus.UNKNOWN:
                current = self.store.get_run(str(run["id"]))
                return self.store.mark_runtime_action_outcome_unknown(
                    action_id=action_id,
                    claim_owner=worker_id,
                    claim_epoch=claim_epoch,
                    expected_revision=int(current["revision"]),
                    category=result.reason_code or "runtime_outcome_unknown",
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "action-unknown", action_id, claim_epoch
                    ),
                ).value
            if outcome == ActionOutcomeStatus.REJECTED or not result.accepted:
                current = self.store.get_run(str(run["id"]))
                return self.store.reject_runtime_action(
                    action_id=action_id,
                    claim_owner=worker_id,
                    claim_epoch=claim_epoch,
                    expected_revision=int(current["revision"]),
                    category=result.reason_code or "runtime_action_rejected",
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "action-rejected", action_id, claim_epoch
                    ),
                ).value
            current = self.store.get_run(str(run["id"]))
            return self.store.acknowledge_runtime_action(
                action_id=action_id,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                runtime_binding_id=str(binding_row["id"]),
                runtime_release_id=str(attempt["runtime_release_id"]),
                state_generation_id=attempt.get("state_generation_id"),
                claim_owner=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=int(current["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key("action-ack", action_id, claim_epoch),
            ).value
        except Exception:
            current = self.store.get_run(str(run["id"]))
            settled = self.store.get_runtime_action(action_id)
            if settled["state"] == "acked":
                return settled
            if current["state"] in {"completed", "failed", "canceled"}:
                return settled
            if invoked:
                return self.store.mark_runtime_action_outcome_unknown(
                    action_id=action_id,
                    claim_owner=worker_id,
                    claim_epoch=claim_epoch,
                    expected_revision=int(current["revision"]),
                    category="runtime_action_outcome_unknown",
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "action-exception-unknown", action_id, claim_epoch
                    ),
                ).value
            return self.store.fail_runtime_action(
                action_id=action_id,
                claim_owner=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=int(current["revision"]),
                category="runtime_action_failed",
                actor_id=self.actor_id,
                idempotency_key=_key("action-fail", action_id, claim_epoch),
            ).value

    async def reconcile_runtime_action(
        self,
        action_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> dict[str, Any]:
        claimed = self.store.claim_runtime_action_reconciliation(
            action_id=action_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            actor_id=self.actor_id,
            idempotency_key=_key(
                "action-reconcile", action_id, worker_id, uuid.uuid4().hex
            ),
        ).value
        run = self.store.get_run(str(claimed["run_id"]))
        attempt = self.store.get_attempt(str(claimed["attempt_id"]))
        binding_row = self.store.get_runtime_binding(
            str(claimed["runtime_binding_id"])
        )
        epoch = int(claimed["claim_epoch"])
        try:
            outcome = await self.runtime.query_action_outcome(
                ActionOutcomeQuery(
                    run_id=str(run["id"]),
                    attempt_id=str(attempt["id"]),
                    binding=_binding_model(binding_row),
                    adapter_operation_id=f"runtime-action:{action_id}",
                    delivery_epoch=epoch - 1,
                )
            )
        except Exception:
            outcome = None
        current = self.store.get_run(str(run["id"]))
        expected_operation_id = f"runtime-action:{action_id}"
        queried_delivery_epoch = epoch - 1
        identity_verified = outcome is not None and (
            outcome.adapter_operation_id == expected_operation_id
            and outcome.delivery_epoch <= queried_delivery_epoch
        )
        if outcome is not None and not identity_verified:
            return self.store.defer_runtime_action_reconciliation(
                action_id=action_id,
                claim_owner=worker_id,
                claim_epoch=epoch,
                expected_revision=int(current["revision"]),
                category="runtime_action_identity_unverified",
                actor_id=self.actor_id,
                idempotency_key=_key(
                    "action-reconcile-identity-unverified", action_id, epoch
                ),
            ).value
        if outcome is not None and outcome.status in {
            ActionOutcomeStatus.ACCEPTED,
            ActionOutcomeStatus.DEDUPLICATED,
        }:
            return self.store.acknowledge_runtime_action(
                action_id=action_id,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                runtime_binding_id=str(binding_row["id"]),
                runtime_release_id=str(attempt["runtime_release_id"]),
                state_generation_id=attempt.get("state_generation_id"),
                claim_owner=worker_id,
                claim_epoch=epoch,
                expected_revision=int(current["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key("action-reconcile-ack", action_id, epoch),
            ).value
        if outcome is not None and outcome.status == ActionOutcomeStatus.REJECTED:
            if outcome.reason_code in {
                "adapter_operation_conflict",
                "stale_delivery_epoch",
            }:
                return self.store.reject_runtime_action(
                    action_id=action_id,
                    claim_owner=worker_id,
                    claim_epoch=epoch,
                    expected_revision=int(current["revision"]),
                    category=str(outcome.reason_code),
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "action-reconcile-rejected", action_id, epoch
                    ),
                ).value
            return self.store.retry_runtime_action_delivery(
                action_id=action_id,
                claim_owner=worker_id,
                claim_epoch=epoch,
                expected_revision=int(current["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key("action-reconcile-retry", action_id, epoch),
            ).value
        return self.store.defer_runtime_action_reconciliation(
            action_id=action_id,
            claim_owner=worker_id,
            claim_epoch=epoch,
            expected_revision=int(current["revision"]),
            category="runtime_action_outcome_still_unknown",
            actor_id=self.actor_id,
            idempotency_key=_key("action-reconcile-defer", action_id, epoch),
        ).value

    async def reconcile_pending_runtime_actions(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for action in self.store.list_runtime_actions_requiring_reconciliation():
            results.append(
                await self.reconcile_runtime_action(
                    str(action["id"]),
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            )
        return results

    def deliver_pending_pin_releases(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for pending in self.store.list_pending_pin_releases():
            action_id = str(pending["id"])
            try:
                claimed = self.store.claim_pin_release(
                    release_action_id=action_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "pin-release-claim",
                        action_id,
                        worker_id,
                        uuid.uuid4().hex,
                    ),
                ).value
            except InvalidTransition:
                continue
            epoch = int(claimed["claim_epoch"])
            run_id = str(claimed["run_id"])
            try:
                expected = _stored_pin(claimed)
                load_pin = getattr(self.releases, "attempt_pin", None)
                if not callable(load_pin):
                    raise RuntimeError("pin_lookup_unavailable")
                pin = load_pin(str(claimed["attempt_id"]))
                if pin is not None:
                    identity = _managed_pin(str(claimed["attempt_id"]), pin)
                    if identity != expected:
                        raise RuntimeError("pin_identity_mismatch")
                    self.releases.finish_attempt(str(claimed["attempt_id"]), pin)
                result = self._settle_pin_release(
                    run_id,
                    lambda revision: self.store.acknowledge_pin_release(
                        release_action_id=action_id,
                        claim_owner=worker_id,
                        claim_epoch=epoch,
                        expected_revision=revision,
                        actor_id=self.actor_id,
                        idempotency_key=_key("pin-release-ack", action_id, epoch),
                    ),
                )
            except Exception as exc:
                category = (
                    str(exc)
                    if str(exc)
                    in {
                        "pin_lookup_unavailable",
                        "pin_identity_mismatch",
                        "legacy_runtime_identity",
                        "runtime_pin_identity_incomplete",
                    }
                    else "pin_release_failed"
                )
                result = self._settle_pin_release(
                    run_id,
                    lambda revision: self.store.fail_pin_release(
                        release_action_id=action_id,
                        claim_owner=worker_id,
                        claim_epoch=epoch,
                        expected_revision=revision,
                        category=category,
                        actor_id=self.actor_id,
                        idempotency_key=_key("pin-release-fail", action_id, epoch),
                    ),
                )
            results.append(result)
        return results

    def _settle_pin_release(
        self,
        run_id: str,
        write: Callable[[int], CommandResult],
    ) -> dict[str, Any]:
        """Offer a pin-release settlement against the run's revision NOW.

        ⟦P9-2⟧ The revision used to be read once, before the pin work that
        follows it does real I/O (`attempt_pin`, `finish_attempt`), and was
        then handed to the settlement. Anything that transitioned the run in
        that window -- and the whole point of a pin release is that the run is
        moving -- made the settlement lose a revision it had no quarrel with:
        the acknowledgement was recorded as `pin_release_failed` although the
        pin HAD been released, and the failure path's own read-then-write
        raised out of the loop and left every later pending release
        undelivered.

        So the revision is read inside the offer and the offer is bounded,
        exactly as `_apply_event` does for runtime events. It is safe for the
        same two reasons: the settlement's identity is the claim fence
        `(release_action_id, claim_owner, claim_epoch)`, which a re-offer does
        not change, and a conflicting attempt raises inside the store's
        transaction, so it commits no receipt for the next attempt to collide
        with. `expected_revision` here is a concurrency guard, not part of what
        the command means.

        ⟦ADJ-G-5⟧ What happens when the ceiling IS reached differs by caller,
        deliberately. Exhausting the attempts on the ACKNOWLEDGEMENT raises
        `RevisionConflict` into `deliver_pending_pin_releases`' own
        `except Exception`, which records the release `pin_release_failed` --
        the pre-fix outcome, now reached only after four losing offers instead
        of one. Exhausting them on the FAILURE path has no such catch: it
        propagates out of the loop and leaves later pending releases for the
        next sweep, which is the honest answer for a run whose revision will
        not settle at all -- retrying there would spin, and swallowing it would
        record a settlement that never committed.
        """

        remaining = PIN_RELEASE_ATTEMPTS
        while True:
            remaining -= 1
            revision = int(self.store.get_run(run_id)["revision"])
            try:
                return write(revision).value
            except RevisionConflict:
                if remaining <= 0:
                    raise

    async def recover_startup(self) -> list[dict[str, Any]]:
        """Converge interrupted attempts without replaying an unknown side effect."""

        reports: list[dict[str, Any]] = []
        try:
            self.deliver_pending_pin_releases(worker_id=self.actor_id)
        except Exception:
            pass
        for run in self.store.list_recoverable_runs():
            attempt_id = str(run["active_attempt_id"])
            if (self.research is not None and run["state"] == "running"
                    and self.store.research_materialization_leased(str(run["id"]), attempt_id)):
                reports.append({"run_id": run["id"], "attempt_id": attempt_id,
                                "outcome": "research_materialization_pending"})
                continue
            command = self.store.schedule_runtime_recovery(
                run_id=str(run["id"]),
                attempt_id=attempt_id,
                expected_revision=int(run["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key(
                    "recovery-schedule",
                    run["id"],
                    attempt_id,
                    run["revision"],
                ),
            ).value
            if command["state"] == "manual_required":
                reports.append(
                    {
                        "run_id": run["id"],
                        "attempt_id": attempt_id,
                        "outcome": "manual_recovery_required",
                        "inspection": "legacy_identity",
                        "release_pin_released": False,
                    }
                )
                continue

        for command in self.store.list_pending_runtime_recoveries():
            run = self.store.get_run(str(command["run_id"]))
            attempt_id = str(command["attempt_id"])
            if (self.research is not None and run["state"] == "running"
                    and self.store.research_materialization_leased(str(run["id"]), attempt_id)):
                continue
            attempt = self.store.get_attempt(attempt_id)
            try:
                claimed = self.store.claim_runtime_recovery(
                    recovery_command_id=str(command["id"]),
                    worker_id=self.actor_id,
                    lease_seconds=30,
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "recovery-claim", command["id"], uuid.uuid4().hex
                    ),
                ).value
            except InvalidTransition:
                continue
            epoch = int(claimed["claim_epoch"])
            current = self.store.get_run(str(run["id"]))
            if (
                current["state"] == "cancel_requested"
                and attempt["runtime_binding_id"] is None
            ):
                canceled = self.store.fail_unbound_run(
                    run_id=str(run["id"]),
                    attempt_id=attempt_id,
                    expected_revision=int(current["revision"]),
                    category=CANCELED_BEFORE_BINDING,
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "recovery-unbound-canceled", command["id"], epoch
                    ),
                    dispatch_owner=attempt.get("dispatch_owner"),
                ).value
                self.store.record_runtime_recovery_outcome(
                    recovery_command_id=str(command["id"]),
                    claim_owner=self.actor_id,
                    claim_epoch=epoch,
                    outcome="inactive",
                    expected_revision=int(canceled["revision"]),
                    details={"status": "canceled_before_binding"},
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "recovery-unbound-cancel-outcome", command["id"], epoch
                    ),
                )
                reports.append(
                    {
                        "run_id": run["id"],
                        "attempt_id": attempt_id,
                        "outcome": "terminal_converged",
                    }
                )
                continue
            if current["state"] in {"completed", "failed", "canceled"}:
                self.store.record_runtime_recovery_outcome(
                    recovery_command_id=str(command["id"]),
                    claim_owner=self.actor_id,
                    claim_epoch=epoch,
                    outcome="inactive",
                    expected_revision=int(current["revision"]),
                    details={"status": f"terminal_{current['state']}"},
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "recovery-terminal", command["id"], epoch
                    ),
                )
                reports.append(
                    {
                        "run_id": run["id"],
                        "attempt_id": attempt_id,
                        "outcome": "terminal_converged",
                    }
                )
                continue
            if attempt["runtime_binding_id"] is None:
                if attempt.get("runtime_identity_version") == 1:
                    try:
                        expected_pin = _stored_pin(attempt)
                        load_pin = getattr(self.releases, "attempt_pin", None)
                        preview_pin = getattr(
                            self.releases, "preview_attempt_pin", None
                        )
                        if not callable(load_pin) or not callable(preview_pin):
                            raise RuntimeError("pin_lookup_unavailable")
                        managed_pin = load_pin(attempt_id)
                        if managed_pin is None:
                            candidate_pin = preview_pin(attempt_id)
                            if _managed_pin(attempt_id, candidate_pin) != expected_pin:
                                raise RuntimeError("pin_identity_mismatch")
                            managed_pin = self.releases.pin_attempt(
                                attempt_id, candidate_pin
                            )
                        observed_pin = _managed_pin(attempt_id, managed_pin)
                        if observed_pin != expected_pin:
                            raise RuntimeError("pin_identity_mismatch")
                    except Exception:
                        self.store.record_runtime_recovery_outcome(
                            recovery_command_id=str(command["id"]),
                            claim_owner=self.actor_id,
                            claim_epoch=epoch,
                            outcome="manual_required",
                            expected_revision=int(current["revision"]),
                            details={"status": "identity_unverified"},
                            actor_id=self.actor_id,
                            idempotency_key=_key(
                                "recovery-unbound-manual",
                                command["id"],
                                epoch,
                            ),
                        )
                        reports.append(
                            {
                                "run_id": run["id"],
                                "attempt_id": attempt_id,
                                "outcome": "manual_recovery_required",
                                "inspection": "identity_unverified",
                                "release_pin_released": False,
                            }
                        )
                        continue
                self.store.record_runtime_recovery_outcome(
                    recovery_command_id=str(command["id"]),
                    claim_owner=self.actor_id,
                    claim_epoch=epoch,
                    outcome="dispatchable",
                    expected_revision=int(current["revision"]),
                    details={"status": "dispatchable"},
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "recovery-dispatchable", command["id"], epoch
                    ),
                )
                reports.append(
                    {
                        "run_id": run["id"],
                        "attempt_id": attempt_id,
                        "outcome": "dispatchable",
                    }
                )
                continue
            binding = self.store.get_runtime_binding(
                str(attempt["runtime_binding_id"])
            )
            inspection = "unavailable"
            try:
                expected_pin = _stored_pin(attempt)
                load_pin = getattr(self.releases, "attempt_pin", None)
                if not callable(load_pin):
                    raise RuntimeError("pin_lookup_unavailable")
                managed_pin = load_pin(attempt_id)
                if managed_pin is None or _managed_pin(
                    attempt_id, managed_pin
                ) != expected_pin:
                    raise RuntimeError("pin_identity_mismatch")
                handshake = await self.runtime.handshake(expected_pin)
                if not handshake.verified:
                    raise RuntimeError(
                        handshake.reason_code or "runtime_identity_mismatch"
                    )
                observed = await self.runtime.inspect(_binding_model(binding))
                inspection = (
                    "active" if observed.active else "inactive"
                ) if observed.exists else "missing"
            except Exception:
                inspection = "identity_unverified"
            if inspection in {"active", "unavailable", "identity_unverified"}:
                current = self.store.get_run(str(run["id"]))
                self.store.record_runtime_recovery_outcome(
                    recovery_command_id=str(command["id"]),
                    claim_owner=self.actor_id,
                    claim_epoch=epoch,
                    outcome="manual_required",
                    expected_revision=int(current["revision"]),
                    details={"status": inspection},
                    actor_id=self.actor_id,
                    idempotency_key=_key(
                        "recovery-manual", command["id"], epoch
                    ),
                )
                reports.append(
                    {
                        "run_id": run["id"],
                        "attempt_id": attempt_id,
                        "outcome": "manual_recovery_required",
                        "inspection": inspection,
                        "release_pin_released": False,
                    }
                )
                continue
            current = self.store.get_run(str(run["id"]))
            research_recovery_failure = None
            if self.research is not None and inspection in {"inactive", "missing"} and current["state"] == "running":
                # Retain an already recorded response before the existing recovery
                # policy closes an interrupted attempt. Never infer worker success.
                if self.store.research_attempt_response(str(run["id"]), attempt_id):
                    try:
                        self.research.persist_response(str(run["id"]), attempt_id)
                    except InvalidTransition as exc:
                        current = self.store.get_run(str(run["id"]))
                        if current["state"] not in _CONVERGING_STATES:
                            from ..research.context import ResearchFailure

                            if exc.source == "claimed":
                                raise ResearchFailure("research_materialization_pending") from None
                            research_recovery_failure = "research_result_recovery_failed"
                    except Exception:
                        research_recovery_failure = "research_result_recovery_failed"
                    current = self.store.get_run(str(run["id"]))
            research_stopped = (self.research is not None
                                and self.store.get_research_context(str(run["id"])) is not None
                                and current["state"] in _CONVERGING_STATES)
            recovery_category = (_FAILED_AFTER_STOP[str(current["state"])] if research_stopped
                                 else research_recovery_failure or "daemon_restart_recovery")
            failed = self.store.apply_runtime_transition(
                run_id=str(run["id"]),
                attempt_id=attempt_id,
                runtime_binding_id=str(attempt["runtime_binding_id"]),
                runtime_release_id=str(attempt["runtime_release_id"]),
                state_generation_id=attempt.get("state_generation_id"),
                target_state="canceled" if research_stopped else "failed",
                expected_revision=int(current["revision"]),
                actor_id=self.actor_id,
                idempotency_key=_key(
                    "startup-recovery", run["id"], attempt_id
                ),
                payload={
                    "category": recovery_category,
                    "retryable": not research_stopped,
                    "inspection": inspection,
                },
            ).value
            current = self.store.get_run(str(run["id"]))
            self.store.record_runtime_recovery_outcome(
                recovery_command_id=str(command["id"]),
                claim_owner=self.actor_id,
                claim_epoch=epoch,
                outcome="inactive" if research_stopped else "retryable_failed",
                expected_revision=int(current["revision"]),
                details={
                    "category": recovery_category,
                    "status": inspection,
                    "retryable": not research_stopped,
                },
                actor_id=self.actor_id,
                idempotency_key=_key(
                    "recovery-failed", command["id"], epoch
                ),
            )
            release_results = self.deliver_pending_pin_releases(
                worker_id=self.actor_id
            )
            pin_released = any(
                item.get("attempt_id") == attempt_id and item.get("state") == "acked"
                for item in release_results
            )
            reports.append(
                {
                    "run_id": failed["id"],
                    "attempt_id": attempt_id,
                    "outcome": "failed_retryable",
                    "inspection": inspection,
                    "release_pin_released": pin_released,
                }
            )
        try:
            self.deliver_pending_pin_releases(worker_id=self.actor_id)
        except Exception:
            pass
        return reports
