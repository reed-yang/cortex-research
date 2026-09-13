"""Stable control-store errors without storage implementation details."""

from __future__ import annotations

from typing import Any


class ControlStoreError(Exception):
    """Base class for sanitized control-store failures."""

    category = "control_store_error"
    retryable = False


class NotFound(ControlStoreError):
    category = "not_found"

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(f"{resource} was not found")
        self.resource = resource
        self.resource_id = resource_id


class IdempotencyConflict(ControlStoreError):
    category = "idempotency_conflict"

    def __init__(self) -> None:
        super().__init__("idempotency key was already used for another request")


class RevisionConflict(ControlStoreError):
    category = "revision_conflict"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("resource revision does not match")
        self.current = current


class ThreadActiveRun(ControlStoreError):
    """A thread cannot be archived while a run of it is still active.

    ⟦Web shell 2026-09⟧ Archiving hides a thread from the default list; a
    thread whose run is still moving would keep producing events nobody is
    looking at. The caller is told the thread's current state so it can wait
    for the run to end and retry with the revision it just saw.
    """

    category = "thread_active_run"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("thread has an active run")
        self.current = current


class ThreadArchived(ControlStoreError):
    """An archived thread takes no new message and starts no run.

    ⟦Web shell 2026-09⟧ `ThreadActiveRun` settles only the instant of
    archiving. Afterwards the Web shell offers Unarchive and nothing else,
    but Telegram and a direct API call reach the same two writers, and a
    thread that started moving again would be answering somebody in a place
    nobody is looking at. The caller is told the thread's current state so
    it can unarchive and retry with the revision it just saw.
    """

    category = "thread_archived"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("thread is archived")
        self.current = current


class InvalidTransition(ControlStoreError):
    category = "invalid_transition"

    def __init__(self, source: str, target: str) -> None:
        super().__init__(f"invalid transition from {source} to {target}")
        self.source = source
        self.target = target


class MachineRunRefused(ControlStoreError):
    """A run the research engine owns is ended by the engine only.

    ⟦P8 N-1 / ADJ-4⟧ Raised by `ControlStore.transition_run` for a
    `cancel_requested` / `pause_requested` target on an engine-owned run
    when the actor is not the engine's own, so no writer -- the control
    API, the Telegram adapter, a script -- can fence a capture's workflow
    by ending its carrier. The control API answers the same problem
    (409 `machine_run`) from its own route-level check first.
    """

    category = "machine_run"

    def __init__(self, run_id: str, target: str) -> None:
        super().__init__(
            "The run belongs to the research engine; it is ended by the engine only"
        )
        self.run_id = run_id
        self.target = target


class CaptureConflict(ControlStoreError):
    category = "already_captured"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("payload is already captured and still open")
        self.current = current


class TransportBindingConflict(ControlStoreError):
    category = "transport_binding_conflict"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("transport scope is already bound to another thread")
        self.current = current
