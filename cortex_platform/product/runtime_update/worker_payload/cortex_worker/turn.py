"""One open turn: a thread that runs it, and a timer that proves it is alive.

⟦AMD-4⟧ splits two jobs the predecessor's single loop could not do at once. The
turn thread is inside the fork for as long as the model takes, and it can park
indefinitely inside an approval callback waiting for a decision that arrives on
the same channel it is not reading. So the main loop keeps reading — that is
what makes `turn.resolve` and `turn.cancel` deliverable mid-turn — and a
separate timer thread emits the heartbeat, including while the turn thread is
parked. A heartbeat emitted by the turn thread would have gone silent exactly
when liveness matters most.

The supervisor's bound is therefore about the channel, never about the turn: it
fails a turn `uncertain` only after a window with no frame at all. A long turn
is not a dead one, and `turn.cancel` is the operator's bound on length.
"""

from __future__ import annotations

import threading
from typing import Callable, Mapping

from .approval import CHOICE_DENY, validate_approval_choice
from .protocol import WorkerEvent, encode_event

HEARTBEAT_SECONDS = 5.0

EVENT_HEARTBEAT = "heartbeat"
# The turn's final frame. The design's words: completion is the existing
# `operation.finish`, sent by the worker — the worker owns the ledger, so the
# worker is the side that records the outcome, and this frame reports what it
# recorded rather than asking the supervisor to record it.
EVENT_FINISH = "operation.finish"
#: The event that announces a decision. The only kind whose id a later
#: `turn.resolve` is allowed to name.
EVENT_DECISION_REQUIRED = "decision.required"

#: Kept as a name for the one answer this module produces on its own — a
#: cancelled turn, or a decision that never arrived.
DECISION_DENY = CHOICE_DENY


class TurnCanceled(RuntimeError):
    """`turn.cancel` arrived; the turn unwinds and reports itself canceled."""


class TurnEmitter:
    """One sequence counter and one lock, shared by the turn thread and the timer.

    Contiguity per turn is a property the supervisor checks, so the sequence is
    minted where the frame is written rather than where the event is decided —
    otherwise the heartbeat timer and the turn thread could interleave two
    frames whose numbers disagree with the order they reached the pipe.

    ⟦AMD-5⟧ The same lock seals the turn. `EVENT_FINISH` is the last frame an
    operation is allowed to produce, and "last" has to be a property of the
    writer rather than of the timing: stopping the heartbeat cannot retract an
    iteration that has already returned from its wait, so the seal is what
    makes the ordering deterministic instead of merely likely.
    """

    def __init__(self, operation_id: str, write: Callable[[bytes], None]) -> None:
        self._operation_id = operation_id
        self._write = write
        self._lock = threading.Lock()
        self._sequence = 0
        self._sealed = False

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    def emit(self, kind: str, payload: Mapping[str, object]) -> bool:
        """Write one event frame. False means the turn was already sealed.

        Refused rather than raised: the only callers that can arrive late are
        the heartbeat timer and a fork callback unwinding after the turn ended,
        and neither has anywhere to report an exception to.
        """

        with self._lock:
            if self._sealed:
                return False
            frame = encode_event(
                WorkerEvent(
                    operation_id=self._operation_id,
                    sequence=self._sequence,
                    event={"kind": kind, "payload": dict(payload)},
                )
            )
            self._sequence += 1
            # Sealed before the write, inside the lock, so no frame can slip
            # between the decision and the byte.
            if kind == EVENT_FINISH:
                self._sealed = True
            self._write(frame)
            return True


class Heartbeat:
    """A timer thread that outlives nothing: it stops with the turn it reports on."""

    JOIN_TIMEOUT = 2.0

    def __init__(self, emitter: TurnEmitter, *, interval: float = HEARTBEAT_SECONDS) -> None:
        self._emitter = emitter
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cortex-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Ask the timer to leave, and wait — briefly — until it has.

        Bounded on purpose. An unbounded join would let a `_write` blocked on a
        full pipe hang the turn thread forever, which is the failure the drained
        stderr and the private frame fd exist to prevent. The bound leaves a
        window; `TurnEmitter`'s seal is what closes it.
        """

        self._stop.set()
        self.join(self.JOIN_TIMEOUT)

    def join(self, timeout: float = JOIN_TIMEOUT) -> None:
        self._thread.join(timeout)

    @property
    def joined(self) -> bool:
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._emitter.emit(EVENT_HEARTBEAT, {})
            except Exception:
                # A dead pipe is the supervisor's problem to observe, not this
                # thread's to escalate; it would only race the turn's own error.
                return


class TurnContext:
    """What a runner is handed: a way to speak, and a way to wait for a decision."""

    def __init__(self, emitter: TurnEmitter, cancel: threading.Event) -> None:
        self._emitter = emitter
        self._cancel = cancel
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Event] = {}
        self._choices: dict[str, str] = {}
        #: Every decision id this turn has announced. A gate is registered only
        #: once the callback parks, and the fork emits `decision.required`
        #: *before* it calls `await_decision` — so "no gate yet" and "nobody
        #: ever asked" are different facts, and only the second is a refusal.
        self._announced: set[str] = set()
        self.signals: list[tuple[str, dict[str, object]]] = []

    @property
    def canceled(self) -> bool:
        return self._cancel.is_set()

    def raise_if_canceled(self) -> None:
        if self._cancel.is_set():
            raise TurnCanceled("turn was canceled")

    def emit(self, kind: str, payload: Mapping[str, object] | None = None) -> bool:
        """Emit one Hermes callback signal, and remember the durable ones.

        The remembered stream is what the turn's `result_digest` is taken over,
        so it is recorded here — at the single point every signal passes
        through — rather than reconstructed afterwards from anything.
        """

        fields = dict(payload or {})
        if kind == EVENT_DECISION_REQUIRED:
            decision_id = fields.get("decision_id")
            if isinstance(decision_id, str) and decision_id:
                with self._lock:
                    self._announced.add(decision_id)
        self.signals.append((kind, fields))
        return self._emitter.emit(kind, fields)

    def await_decision(self, decision_id: str, *, timeout: float | None = None) -> str:
        """Park until `turn.resolve` names this decision, or the turn is canceled.

        This replaces the native backend's in-process `Event.wait()`: the same
        park, with the release travelling over the channel instead of through a
        shared dict in one process.
        """

        gate = threading.Event()
        with self._lock:
            existing = self._choices.pop(decision_id, None)
            if existing is not None:
                return existing
            self._gates[decision_id] = gate
        try:
            while not gate.wait(0.05):
                if self._cancel.is_set():
                    return DECISION_DENY
                if timeout is not None:
                    timeout -= 0.05
                    if timeout <= 0:
                        return DECISION_DENY
        finally:
            with self._lock:
                self._gates.pop(decision_id, None)
        with self._lock:
            return self._choices.pop(decision_id, DECISION_DENY)

    def resolve(self, decision_id: str, choice: str) -> bool:
        """True when the answer reached, or will reach, something that asked.

        A decision that arrives before the callback parks is kept, not lost: the
        fork emits `decision.required` and only then calls `await_decision`, so
        the supervisor answering the instant it reads the event is the normal
        path rather than an error. A decision id nothing on this turn ever
        announced is the other thing entirely — it is delivered to nobody, and
        saying otherwise is what let the backend record ACCEPTED for an approval
        that reached no gate.
        """

        with self._lock:
            gate = self._gates.get(decision_id)
            if gate is None and decision_id not in self._announced:
                return False
            self._choices[decision_id] = choice
        if gate is not None:
            gate.set()
        return True

    def release_all(self) -> None:
        with self._lock:
            gates = list(self._gates.values())
        for gate in gates:
            gate.set()


class Turn:
    """The lifecycle of one operation id, from `turn.begin` to `operation.finish`."""

    def __init__(
        self,
        *,
        operation_id: str,
        request: Mapping[str, object],
        emitter: TurnEmitter,
        runner: Callable[[Mapping[str, object], "TurnContext"], Mapping[str, object]],
        finish: Callable[[str, str], None],
        heartbeat_interval: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.operation_id = operation_id
        self._request = dict(request)
        self._emitter = emitter
        self._runner = runner
        self._finish = finish
        self._cancel = threading.Event()
        self.context = TurnContext(emitter, self._cancel)
        self._heartbeat = Heartbeat(emitter, interval=heartbeat_interval)
        self._thread = threading.Thread(
            target=self._body, name=f"cortex-turn-{operation_id}", daemon=True
        )
        self.done = threading.Event()

    def start(self) -> None:
        self._heartbeat.start()
        self._thread.start()

    def resolve(self, decision: Mapping[str, object]) -> bool:
        # Validated again here rather than trusted from the grammar: this is the
        # object that hands the answer to the parked approval callback, and it is
        # reachable from any caller that can build a mapping.
        return self.context.resolve(
            str(decision["decision_id"]),
            validate_approval_choice(decision["choice"]),
        )

    def cancel(self) -> None:
        self._cancel.set()
        self.context.release_all()

    def join(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def _body(self) -> None:
        from .digests import result_digest, turn_durable_stream

        session_ref = str(self._request.get("session_ref", ""))
        result: dict[str, object]
        outcome = "committed"
        try:
            result = dict(self._runner(self._request, self.context))
        except TurnCanceled:
            result = {"session_ref": session_ref, "final_response": None,
                      "canceled": True, "failed": False}
        except BaseException:
            # The category is fixed rather than derived from the exception: an
            # exception's text is the one place a provider error message could
            # carry a credential back across the boundary.
            result = {"session_ref": session_ref, "final_response": None,
                      "canceled": False, "failed": True}
            outcome = "failed"
        else:
            if self._cancel.is_set():
                result["canceled"] = True
            if result.get("failed"):
                outcome = "failed"
        finally:
            self._heartbeat.stop()
        stream = turn_durable_stream(
            self.context.signals, result=result, session_ref=session_ref
        )
        digest = result_digest(stream)
        finish_payload: dict[str, object] = {
            "outcome": outcome,
            "result_digest": digest,
            "result": result,
        }
        try:
            self._finish(outcome, digest)
        except Exception as exc:
            finish_payload["ledger_error"] = type(exc).__name__
        try:
            self._emitter.emit(EVENT_FINISH, finish_payload)
        finally:
            self.done.set()
