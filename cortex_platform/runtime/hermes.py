"""Lazy Hermes adapter for the Cortex runtime port.

This is the only Cortex runtime module that knows Hermes module names or
callback shapes. The adapter can be imported and health-checked when Hermes is
not installed; native modules are loaded only when a port operation needs them.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from .compatibility import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    CompatibilityCheck,
    CompatibilityReport,
    SUPPORTED_HERMES_DISTRIBUTIONS,
    SUPPORTED_SESSION_DB_SCHEMA,
)
from .events import RuntimeEventFactory, translate_hermes_signal
from .models import (
    FAILURE_DETAIL_LIMIT,
    WORKER_REPORTED_FAILURE,
    ActionOutcomeQuery,
    ActionOutcomeStatus,
    AttemptRequest,
    ControlAction,
    ControlRequest,
    ControlResult,
    DecisionResolution,
    DecisionResult,
    EventDurability,
    HealthStatus,
    ReleasePin,
    RuntimeActionOutcome,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeHandshake,
    RuntimeHealth,
    RuntimeReleaseIdentity,
    SessionInspection,
    SessionOpenRequest,
    failure_detail,
)


class HermesUnavailableError(RuntimeError):
    """Hermes cannot be loaded in this installation."""


class HermesIncompatibleError(RuntimeError):
    """The installed Hermes surface does not match the compatibility matrix."""


class HermesSessionNotFoundError(LookupError):
    """The opaque Hermes session binding no longer exists."""


class HermesDuplicateAttemptError(RuntimeError):
    """A run/attempt key already has one active Hermes execution."""


class RuntimeOperationUncertain(RuntimeError):
    """A turn's outcome is unknown: the channel died with it in flight.

    Terminal for the attempt. Retrying it automatically is exactly the thing
    durable dedup exists to prevent, so the operator's retry is a new attempt
    id and therefore a new operation.
    """


class HermesOperationConflictError(RuntimeError):
    """One adapter operation identifier was reused for another effect."""


@dataclass(frozen=True)
class HermesSession:
    session_ref: str
    parent_session_ref: str | None = None


@dataclass(frozen=True)
class HermesInspection:
    exists: bool
    active: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HermesSignal:
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    stable_id: str | None = None


@dataclass(frozen=True)
class HermesRunInput:
    run_id: str
    attempt_id: str
    session_ref: str
    user_message: str
    system_message: str | None
    conversation_history: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]
    execution_token: object
    adapter_operation_id: str = ""


def hermes_turn_prompt(
    request: HermesRunInput, agent_options: Mapping[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    """Keep opted-in Cortex turn directives out of Hermes' cached prefix."""
    options = dict(agent_options)
    if request.metadata.get("cortex_research_prompt_mode") != "ephemeral_v1":
        return request.system_message, options
    if not isinstance(request.system_message, str) or not request.system_message.strip():
        raise ValueError("per-turn research prompt is missing")
    configured = options.get("ephemeral_system_prompt")
    if configured is not None and not isinstance(configured, str):
        raise ValueError("configured ephemeral system prompt must be text")
    options["ephemeral_system_prompt"] = (
        configured + "\n\n" + request.system_message if configured else request.system_message
    )
    return None, options


@dataclass(frozen=True)
class HermesRunResult:
    session_ref: str
    final_response: str | None
    canceled: bool = False
    failed: bool = False


class HermesBackend(Protocol):
    def compatibility(self) -> CompatibilityReport: ...

    def capabilities(self) -> RuntimeCapabilities: ...

    def runtime_identity(self) -> RuntimeReleaseIdentity | None: ...

    def open_session(
        self, metadata: Mapping[str, Any], adapter_operation_id: str
    ) -> HermesSession: ...

    def load_session(self, session_ref: str) -> HermesSession: ...

    def fork_session(
        self,
        session_ref: str,
        metadata: Mapping[str, Any],
        adapter_operation_id: str,
    ) -> HermesSession: ...

    def reserve_attempt(self, run_id: str, attempt_id: str) -> object | None: ...

    def release_attempt(
        self, run_id: str, attempt_id: str, execution_token: object
    ) -> None: ...

    def run(
        self, request: HermesRunInput, emit: Callable[[HermesSignal], None]
    ) -> HermesRunResult: ...

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str,
    ) -> RuntimeActionOutcome: ...

    def steer(
        self,
        run_id: str,
        attempt_id: str,
        text: str,
        adapter_operation_id: str,
        delivery_epoch: int,
        session_ref: str,
    ) -> RuntimeActionOutcome: ...

    def resolve_decision(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        choice: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome: ...

    def query_action_outcome(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome: ...

    def inspect(self, session_ref: str) -> HermesInspection: ...

    def recover(
        self,
        session_ref: str,
        checkpoint: RuntimeCheckpoint,
        adapter_operation_id: str,
    ) -> HermesSession: ...


@dataclass
class _PendingDecision:
    run_id: str
    attempt_id: str
    event: threading.Event = field(default_factory=threading.Event)
    choice: str | None = None


@dataclass
class _NativeExecution:
    token: object
    agent: Any = None
    started: bool = False
    cancel_requested: bool = False


@dataclass
class _AdapterExecution:
    session_ref: str
    backend: HermesBackend | None = None
    cancel_requested: bool = False
    accepted_decision_operation: tuple[str, int] | None = None


@dataclass
class _DecisionClaim:
    choice: str
    revision: int
    future: Future[DecisionResult] = field(default_factory=Future)


@dataclass(frozen=True)
class _DecisionSuccess:
    choice: str
    revision: int
    result: DecisionResult


@dataclass(frozen=True)
class _DecisionIdentity:
    run_id: str
    attempt_id: str
    revision: int


@dataclass(frozen=True)
class _SessionOperation:
    identity: tuple[str, ...]
    session: HermesSession


@dataclass(frozen=True)
class _ActionOperation:
    identity: tuple[str, ...]
    delivery_epoch: int
    outcome: RuntimeActionOutcome


_PRIVATE_REASONING_PROGRESS_EVENTS = frozenset(
    {
        "_thinking",
        "thinking",
        "reasoning.available",
        "delegate.task_thinking",
        "subagent.thinking",
        "delegateevent.task_thinking",
    }
)


def _call_shape_compatible(target: Any, /, *args: Any, **kwargs: Any) -> bool:
    if target is None:
        return False
    try:
        inspect.signature(target).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return False
    return True


def _interrupt_state_shape(agent_class: type) -> tuple[str, bool]:
    value = inspect.getattr_static(agent_class, "is_interrupted", None)
    if isinstance(value, property):
        return "property", True
    if callable(value):
        return (
            "callable",
            _call_shape_compatible(value, object()),
        )
    return "missing", False


def _agent_is_interrupted(agent: Any) -> bool:
    value = getattr(agent, "is_interrupted", False)
    return bool(value() if callable(value) else value)


class _NativeHermesBackend:
    """Thin binding to the verified v2026.5.28 Hermes Python surface."""

    def __init__(
        self,
        *,
        hermes_state: Any,
        agent_class: type,
        set_approval_callback: Callable[[Any], None],
        session_db_path: Path | None,
        agent_options: Mapping[str, Any],
        runtime_identity: RuntimeReleaseIdentity | None = None,
    ) -> None:
        self._hermes_state = hermes_state
        self._agent_class = agent_class
        self._set_approval_callback = set_approval_callback
        self._session_db_path = session_db_path
        self._agent_options = dict(agent_options)
        self._runtime_release_identity = runtime_identity
        self._db: Any = None
        self._lock = threading.RLock()
        self._executions: dict[tuple[str, str], _NativeExecution] = {}
        self._pending: dict[tuple[str, str], _PendingDecision] = {}
        self._resolved: dict[tuple[str, str], tuple[str, str, str]] = {}
        self._operation_lock = threading.Lock()
        self._session_operations: OrderedDict[str, _SessionOperation] = OrderedDict()
        self._action_operations: OrderedDict[str, _ActionOperation] = OrderedDict()
        self._operation_cache_limit = 1024

    @staticmethod
    def _remember_bounded(cache: OrderedDict[str, Any], key: str, value: Any) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > 1024:
            cache.popitem(last=False)

    def runtime_identity(self) -> RuntimeReleaseIdentity | None:
        return self._runtime_release_identity

    def _session_db(self) -> Any:
        with self._lock:
            if self._db is None:
                session_db = self._hermes_state.SessionDB
                self._db = (
                    session_db(self._session_db_path)
                    if self._session_db_path is not None
                    else session_db()
                )
            return self._db

    def compatibility(self) -> CompatibilityReport:
        try:
            runtime_version = metadata.version("hermes-agent")
        except metadata.PackageNotFoundError:
            runtime_version = None
        schema = getattr(self._hermes_state, "SCHEMA_VERSION", None)

        def callback(*args: Any, **kwargs: Any) -> None:
            return None

        constructor_options = dict(self._agent_options)
        constructor_options.update(
            {
                "session_id": "compat-session",
                "session_db": object(),
                "parent_session_id": None,
                "tool_progress_callback": callback,
                "tool_start_callback": callback,
                "tool_complete_callback": callback,
                "step_callback": callback,
            }
        )
        constructor_ok = _call_shape_compatible(
            self._agent_class, **constructor_options
        )
        session_db_class = getattr(self._hermes_state, "SessionDB", None)
        db_constructor_args = (
            (self._session_db_path,) if self._session_db_path is not None else ()
        )
        db_ok = _call_shape_compatible(session_db_class, *db_constructor_args)
        if db_ok:
            db_ok = all(
                (
                    _call_shape_compatible(
                        getattr(session_db_class, "create_session", None),
                        object(),
                        session_id="compat-session",
                        source="cortex",
                        model=self._agent_options.get("model"),
                        parent_session_id="compat-parent",
                    ),
                    _call_shape_compatible(
                        getattr(session_db_class, "get_session", None),
                        object(),
                        "compat-session",
                    ),
                    _call_shape_compatible(
                        getattr(
                            session_db_class,
                            "get_messages_as_conversation",
                            None,
                        ),
                        object(),
                        "compat-session",
                    ),
                    _call_shape_compatible(
                        getattr(session_db_class, "replace_messages", None),
                        object(),
                        "compat-session",
                        [],
                    ),
                )
            )
        interrupt_shape, interrupt_state_ok = _interrupt_state_shape(self._agent_class)
        control_ok = all(
            (
                _call_shape_compatible(
                    getattr(self._agent_class, "interrupt", None), object()
                ),
                _call_shape_compatible(
                    getattr(self._agent_class, "steer", None), object(), "text"
                ),
                _call_shape_compatible(
                    getattr(self._agent_class, "run_conversation", None),
                    object(),
                    "message",
                    system_message=None,
                    conversation_history=[],
                    task_id="attempt",
                    stream_callback=callback,
                ),
            )
        )
        approval_ok = _call_shape_compatible(self._set_approval_callback, callback)
        provider = str(self._agent_options.get("provider", "")).strip()
        model = str(self._agent_options.get("model", "")).strip()
        provider_models = {provider: (model,)} if provider and model else {}
        return CompatibilityReport(
            runtime_version=runtime_version,
            session_db_schema=schema if isinstance(schema, int) else None,
            checks=(
                CompatibilityCheck(
                    "distribution_version",
                    "|".join(SUPPORTED_HERMES_DISTRIBUTIONS),
                    runtime_version or "missing",
                    runtime_version in SUPPORTED_HERMES_DISTRIBUTIONS,
                ),
                CompatibilityCheck(
                    "session_db_schema",
                    f"{SUPPORTED_SESSION_DB_SCHEMA[0]}-{SUPPORTED_SESSION_DB_SCHEMA[1]}",
                    str(schema) if schema is not None else "missing",
                    isinstance(schema, int)
                    and SUPPORTED_SESSION_DB_SCHEMA[0]
                    <= schema
                    <= SUPPORTED_SESSION_DB_SCHEMA[1],
                ),
                CompatibilityCheck(
                    "agent_callbacks",
                    "v2026.5.28 constructor callback set",
                    "present" if constructor_ok else "missing",
                    constructor_ok,
                ),
                CompatibilityCheck(
                    "session_db_methods",
                    "create/load/copy",
                    "present" if db_ok else "missing",
                    db_ok,
                ),
                CompatibilityCheck(
                    "control_methods",
                    "run/interrupt/steer",
                    "present" if control_ok else "missing",
                    control_ok,
                ),
                CompatibilityCheck(
                    "interrupt_state",
                    "property|bound callable",
                    interrupt_shape,
                    interrupt_state_ok,
                ),
                CompatibilityCheck(
                    "approval_bridge",
                    "thread-local callback setter",
                    "present" if approval_ok else "missing",
                    approval_ok,
                ),
            ),
            provider_models=provider_models,
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
            steer=available,
            decisions=available,
            session_rebinding=available,
            checkpoint_recovery=available,
            action_outcome_query=available,
            durable_operation_deduplication=False,
            provider_models=report.provider_models,
        )

    @staticmethod
    def _new_session_ref() -> str:
        return f"cortex_{uuid.uuid4().hex}"

    def open_session(
        self, metadata: Mapping[str, Any], adapter_operation_id: str = ""
    ) -> HermesSession:
        _ = metadata
        adapter_operation_id = adapter_operation_id or f"legacy-open:{uuid.uuid4().hex}"
        identity = ("session.open",)
        with self._operation_lock:
            previous = self._session_operations.get(adapter_operation_id)
            if previous is not None:
                if previous.identity != identity:
                    raise HermesOperationConflictError("adapter_operation_conflict")
                return previous.session
            session_ref = self._new_session_ref()
            self._session_db().create_session(
                session_id=session_ref,
                source="cortex",
                model=self._agent_options.get("model"),
            )
            session = HermesSession(session_ref=session_ref)
            self._remember_bounded(
                self._session_operations,
                adapter_operation_id,
                _SessionOperation(identity, session),
            )
            return session

    def load_session(self, session_ref: str) -> HermesSession:
        row = self._session_db().get_session(session_ref)
        if row is None:
            raise HermesSessionNotFoundError(session_ref)
        parent = row.get("parent_session_id") if isinstance(row, Mapping) else None
        return HermesSession(session_ref=session_ref, parent_session_ref=parent)

    def fork_session(
        self,
        session_ref: str,
        metadata: Mapping[str, Any],
        adapter_operation_id: str = "",
    ) -> HermesSession:
        _ = metadata
        adapter_operation_id = adapter_operation_id or f"legacy-fork:{uuid.uuid4().hex}"
        identity = ("session.fork", session_ref)
        with self._operation_lock:
            previous = self._session_operations.get(adapter_operation_id)
            if previous is not None:
                if previous.identity != identity:
                    raise HermesOperationConflictError("adapter_operation_conflict")
                return previous.session
            self.load_session(session_ref)
            db = self._session_db()
            child_ref = self._new_session_ref()
            db.create_session(
                session_id=child_ref,
                source="cortex",
                model=self._agent_options.get("model"),
                parent_session_id=session_ref,
            )
            messages = db.get_messages_as_conversation(session_ref)
            if messages:
                db.replace_messages(child_ref, messages)
            session = HermesSession(child_ref, parent_session_ref=session_ref)
            self._remember_bounded(
                self._session_operations,
                adapter_operation_id,
                _SessionOperation(identity, session),
            )
            return session

    def reserve_attempt(self, run_id: str, attempt_id: str) -> object | None:
        key = (run_id, attempt_id)
        with self._lock:
            if key in self._executions:
                return None
            token = object()
            self._executions[key] = _NativeExecution(token=token)
            return token

    def release_attempt(
        self, run_id: str, attempt_id: str, execution_token: object
    ) -> None:
        key = (run_id, attempt_id)
        agent = None
        with self._lock:
            execution = self._executions.get(key)
            if execution is None or execution.token is not execution_token:
                return
            if execution.started:
                execution.cancel_requested = True
                agent = execution.agent
                self._deny_pending_locked(run_id, attempt_id)
            else:
                self._executions.pop(key, None)
        if agent is not None:
            agent.interrupt()

    def _deny_pending_locked(self, run_id: str, attempt_id: str) -> None:
        for pending in self._pending.values():
            if pending.run_id != run_id or pending.attempt_id != attempt_id:
                continue
            if pending.choice is None:
                pending.choice = "deny"
                pending.event.set()

    def _finish_execution(
        self, run_id: str, attempt_id: str, execution_token: object
    ) -> None:
        key = (run_id, attempt_id)
        with self._lock:
            execution = self._executions.get(key)
            if execution is None or execution.token is not execution_token:
                return
            self._deny_pending_locked(run_id, attempt_id)
            self._executions.pop(key, None)
            resolved_keys = [
                decision_key
                for decision_key, (
                    _,
                    resolved_run,
                    resolved_attempt,
                ) in self._resolved.items()
                if resolved_run == run_id and resolved_attempt == attempt_id
            ]
            for decision_key in resolved_keys:
                self._resolved.pop(decision_key, None)

    def run(
        self, request: HermesRunInput, emit: Callable[[HermesSignal], None]
    ) -> HermesRunResult:
        active_key = (request.run_id, request.attempt_id)
        with self._lock:
            execution = self._executions.get(active_key)
            if (
                execution is None
                or execution.token is not request.execution_token
                or execution.started
            ):
                raise HermesDuplicateAttemptError("duplicate_active_attempt")
            execution.started = True
            if execution.cancel_requested:
                self._executions.pop(active_key, None)
                return HermesRunResult(
                    session_ref=request.session_ref,
                    final_response=None,
                    canceled=True,
                )

        try:
            session = self.load_session(request.session_ref)
        except Exception:
            self._finish_execution(
                request.run_id, request.attempt_id, request.execution_token
            )
            raise
        with self._lock:
            execution = self._executions.get(active_key)
            canceled_before_setup = bool(
                execution is None
                or execution.token is not request.execution_token
                or execution.cancel_requested
            )
        if canceled_before_setup:
            self._finish_execution(
                request.run_id, request.attempt_id, request.execution_token
            )
            return HermesRunResult(
                session_ref=request.session_ref,
                final_response=None,
                canceled=True,
            )

        tool_completion_metadata: dict[str, deque[tuple[bool, int | None]]] = {}
        tool_completion_lock = threading.Lock()
        approval_index = 0

        def tool_progress(*args: Any, **kwargs: Any) -> None:
            event = str(args[0]).strip() if args else ""
            normalized_event = event.lower()
            if normalized_event == "tool.started":
                return
            if normalized_event == "tool.completed":
                tool_name = args[1] if len(args) > 1 else None
                if not isinstance(tool_name, str) or not tool_name:
                    return
                duration = kwargs.get("duration")
                duration_ms = (
                    round(duration * 1000)
                    if isinstance(duration, (int, float))
                    and not isinstance(duration, bool)
                    and math.isfinite(duration)
                    and duration >= 0
                    else None
                )
                metadata_item = (bool(kwargs.get("is_error", False)), duration_ms)
                with tool_completion_lock:
                    tool_completion_metadata.setdefault(tool_name, deque()).append(
                        metadata_item
                    )
                return
            if normalized_event in _PRIVATE_REASONING_PROGRESS_EVENTS:
                emit(HermesSignal("reasoning.available"))
                return
            if normalized_event == "tool.progress":
                emit(HermesSignal("tool.progress"))

        def tool_start(tool_call_id: str, tool_name: str, arguments: Any) -> None:
            emit(
                HermesSignal(
                    "tool.started",
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments
                        if isinstance(arguments, Mapping)
                        else {},
                    },
                    stable_id=f"tool:{tool_call_id}:started",
                )
            )

        def tool_complete(
            tool_call_id: str, tool_name: str, arguments: Any, result: Any
        ) -> None:
            _ = arguments
            with tool_completion_lock:
                pending_metadata = tool_completion_metadata.get(tool_name)
                completion_metadata = (
                    pending_metadata.popleft() if pending_metadata else None
                )
                if pending_metadata is not None and not pending_metadata:
                    tool_completion_metadata.pop(tool_name, None)
            if completion_metadata is None:
                is_error = isinstance(result, str) and result.lower().startswith(
                    "error"
                )
                duration_ms = None
            else:
                is_error, duration_ms = completion_metadata
            emit(
                HermesSignal(
                    "tool.completed",
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "is_error": is_error,
                        "duration_ms": duration_ms,
                    },
                    stable_id=f"tool:{tool_call_id}:completed",
                )
            )

        def step(iteration: int, previous_tools: Any) -> None:
            emit(HermesSignal("step.completed", {"iteration": iteration}))

        def approval(
            command: str,
            description: str,
            *,
            allow_permanent: bool = True,
        ) -> str:
            nonlocal approval_index
            _ = allow_permanent
            decision_digest = hashlib.sha256(
                f"{request.adapter_operation_id}\x1fapproval\x1f{approval_index}".encode(
                    "utf-8"
                )
            ).hexdigest()
            approval_index += 1
            decision_id = f"decision-{decision_digest}"
            key = (request.session_ref, decision_id)
            pending = _PendingDecision(request.run_id, request.attempt_id)
            with self._lock:
                current = self._executions.get(active_key)
                if (
                    current is None
                    or current.token is not request.execution_token
                    or current.cancel_requested
                ):
                    return "deny"
                self._pending[key] = pending
            emit(
                HermesSignal(
                    "decision.required",
                    {
                        "decision_id": decision_id,
                        "decision_kind": "approval",
                        "prompt": "Approve this runtime tool action?",
                        "command": command,
                        "description": description,
                    },
                    stable_id=f"decision:{decision_id}:required",
                )
            )
            pending.event.wait()
            with self._lock:
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)
                choice = pending.choice
                if choice is None:
                    raise RuntimeError("approval released without a control decision")
                self._resolved[key] = (
                    choice,
                    request.run_id,
                    request.attempt_id,
                )
            return choice

        def stream(text: Any) -> None:
            if isinstance(text, str) and text:
                emit(HermesSignal("token.delta", {"text": text}))

        system_message, options = hermes_turn_prompt(request, self._agent_options)
        options.update(
            {
                "session_id": request.session_ref,
                "session_db": self._session_db(),
                "parent_session_id": session.parent_session_ref,
                "tool_progress_callback": tool_progress,
                "tool_start_callback": tool_start,
                "tool_complete_callback": tool_complete,
                "step_callback": step,
            }
        )
        approval_install_attempted = False
        try:
            agent = self._agent_class(**options)
            with self._lock:
                execution = self._executions.get(active_key)
                if execution is None or execution.token is not request.execution_token:
                    return HermesRunResult(
                        session_ref=request.session_ref,
                        final_response=None,
                        canceled=True,
                    )
                execution.agent = agent
                canceled_before_registration = execution.cancel_requested
            if canceled_before_registration:
                agent.interrupt()
                return HermesRunResult(
                    session_ref=request.session_ref,
                    final_response=None,
                    canceled=True,
                )

            approval_install_attempted = True
            self._set_approval_callback(approval)
            with self._lock:
                execution = self._executions.get(active_key)
                canceled_before_run = bool(
                    execution is None
                    or execution.token is not request.execution_token
                    or execution.cancel_requested
                )
            if canceled_before_run:
                agent.interrupt()
                return HermesRunResult(
                    session_ref=request.session_ref,
                    final_response=None,
                    canceled=True,
                )

            result = agent.run_conversation(
                request.user_message,
                system_message=system_message,
                conversation_history=list(request.conversation_history),
                task_id=request.attempt_id,
                stream_callback=stream,
            )
            final_response = (
                result.get("final_response") if isinstance(result, Mapping) else None
            )
            canceled = bool(
                result.get("interrupted", False)
                if isinstance(result, Mapping)
                else False
            ) or _agent_is_interrupted(agent)
            failed = bool(
                isinstance(result, Mapping)
                and not canceled
                and (
                    result.get("failed") is True
                    or result.get("partial") is True
                    or ("completed" in result and result.get("completed") is False)
                )
            )
            return HermesRunResult(
                session_ref=str(getattr(agent, "session_id", request.session_ref)),
                final_response=(
                    str(final_response)
                    if final_response is not None and not canceled and not failed
                    else None
                ),
                canceled=canceled,
                failed=failed,
            )
        finally:
            try:
                if approval_install_attempted:
                    self._set_approval_callback(None)
            finally:
                self._finish_execution(
                    request.run_id,
                    request.attempt_id,
                    request.execution_token,
                )

    def _perform_action(
        self,
        *,
        identity: tuple[str, ...],
        adapter_operation_id: str,
        delivery_epoch: int,
        effect: Callable[[], bool],
        rejected_reason: str,
    ) -> RuntimeActionOutcome:
        with self._operation_lock:
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
            accepted = effect()
            outcome = RuntimeActionOutcome(
                adapter_operation_id,
                delivery_epoch,
                (
                    ActionOutcomeStatus.ACCEPTED
                    if accepted
                    else ActionOutcomeStatus.REJECTED
                ),
                None if accepted else rejected_reason,
            )
            self._remember_bounded(
                self._action_operations,
                adapter_operation_id,
                _ActionOperation(identity, delivery_epoch, outcome),
            )
            return outcome

    def cancel(
        self,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str = "",
        delivery_epoch: int = 0,
        session_ref: str = "",
    ) -> RuntimeActionOutcome:
        adapter_operation_id = (
            adapter_operation_id or f"legacy-cancel:{uuid.uuid4().hex}"
        )

        def effect() -> bool:
            key = (run_id, attempt_id)
            with self._lock:
                execution = self._executions.get(key)
                if execution is None:
                    return False
                execution.cancel_requested = True
                agent = execution.agent
                self._deny_pending_locked(run_id, attempt_id)
            if agent is not None:
                agent.interrupt()
            return True

        return self._perform_action(
            identity=("control.cancel", session_ref, run_id, attempt_id),
            adapter_operation_id=adapter_operation_id,
            delivery_epoch=delivery_epoch,
            effect=effect,
            rejected_reason="run_not_active",
        )

    def steer(
        self,
        run_id: str,
        attempt_id: str,
        text: str,
        adapter_operation_id: str = "",
        delivery_epoch: int = 0,
        session_ref: str = "",
    ) -> RuntimeActionOutcome:
        adapter_operation_id = (
            adapter_operation_id or f"legacy-steer:{uuid.uuid4().hex}"
        )

        def effect() -> bool:
            with self._lock:
                execution = self._executions.get((run_id, attempt_id))
                agent = execution.agent if execution is not None else None
                canceled = bool(execution is None or execution.cancel_requested)
            return bool(not canceled and agent is not None and agent.steer(text))

        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self._perform_action(
            identity=("control.steer", session_ref, run_id, attempt_id, text_digest),
            adapter_operation_id=adapter_operation_id,
            delivery_epoch=delivery_epoch,
            effect=effect,
            rejected_reason="run_not_active",
        )

    def resolve_decision(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        choice: str,
        adapter_operation_id: str = "",
        delivery_epoch: int = 0,
    ) -> RuntimeActionOutcome:
        adapter_operation_id = (
            adapter_operation_id or f"legacy-decision:{uuid.uuid4().hex}"
        )

        def effect() -> bool:
            key = (session_ref, decision_id)
            hermes_choice = "once" if choice == "approve_once" else "deny"
            with self._lock:
                resolved = self._resolved.get(key)
                if resolved is not None:
                    return bool(
                        resolved[1] == run_id
                        and resolved[2] == attempt_id
                        and resolved[0] == hermes_choice
                    )
                pending = self._pending.get(key)
                if pending is None:
                    return False
                if pending.run_id != run_id or pending.attempt_id != attempt_id:
                    return False
                if pending.choice is not None:
                    return pending.choice == hermes_choice
                pending.choice = hermes_choice
                pending.event.set()
                return True

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
            rejected_reason="pending_decision_missing",
        )

    def query_action_outcome(
        self,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome:
        with self._operation_lock:
            previous = self._action_operations.get(adapter_operation_id)
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

    def inspect(self, session_ref: str) -> HermesInspection:
        try:
            row = self._session_db().get_session(session_ref)
        except Exception:
            return HermesInspection(exists=False, active=False)
        if row is None:
            return HermesInspection(exists=False, active=False)
        active = any(
            execution.agent is not None
            and str(getattr(execution.agent, "session_id", "")) == session_ref
            for execution in self._executions.values()
        )
        return HermesInspection(exists=True, active=active)

    def recover(
        self,
        session_ref: str,
        checkpoint: RuntimeCheckpoint,
        adapter_operation_id: str = "",
    ) -> HermesSession:
        adapter_operation_id = adapter_operation_id or checkpoint.adapter_operation_id
        history_digest = hashlib.sha256(
            json.dumps(
                checkpoint.conversation_history,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        identity = (
            "session.recover",
            session_ref,
            checkpoint.checkpoint_ref,
            history_digest,
        )
        with self._operation_lock:
            previous = self._session_operations.get(adapter_operation_id)
            if previous is not None:
                if previous.identity != identity:
                    raise HermesOperationConflictError("adapter_operation_conflict")
                return previous.session
            self.load_session(session_ref)
            db = self._session_db()
            child_ref = self._new_session_ref()
            db.create_session(
                session_id=child_ref,
                source="cortex",
                model=self._agent_options.get("model"),
                parent_session_id=session_ref,
            )
            db.replace_messages(child_ref, list(checkpoint.conversation_history))
            recovered = HermesSession(child_ref, parent_session_ref=session_ref)
            self._remember_bounded(
                self._session_operations,
                adapter_operation_id,
                _SessionOperation(identity, recovered),
            )
            return recovered


def _load_native_backend(
    *,
    session_db_path: Path | None,
    agent_options: Mapping[str, Any],
    runtime_identity: RuntimeReleaseIdentity | None,
) -> HermesBackend:
    """Import Hermes only when the adapter is first exercised."""
    try:
        import hermes_state  # type: ignore[import-not-found]
        from run_agent import AIAgent  # type: ignore[import-not-found]
        from tools.terminal_tool import (  # type: ignore[import-not-found]
            set_approval_callback,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise HermesUnavailableError("hermes_not_installed") from exc
    return _NativeHermesBackend(
        hermes_state=hermes_state,
        agent_class=AIAgent,
        set_approval_callback=set_approval_callback,
        session_db_path=session_db_path,
        agent_options=agent_options,
        runtime_identity=runtime_identity,
    )


class HermesAdapter:
    """Cortex RuntimePort implementation backed by a lazy Hermes backend."""

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    def __init__(
        self,
        *,
        backend_loader: Callable[[], HermesBackend] | None = None,
        session_db_path: Path | None = None,
        agent_options: Mapping[str, Any] | None = None,
        runtime_identity: RuntimeReleaseIdentity | None = None,
        managed: bool = False,
    ) -> None:
        # ⟦AMD-3⟧ The trap this replaces: `backend_loader is None` meant the
        # identity refusal was switched off by the very thing that made a
        # backend managed — inject a loader and `_load_backend` stopped checking
        # that the backend could name its release at all. The pin-equality gate
        # at the orchestration boundary is the real invariant; this is the
        # fail-fast, and a fail-fast that inverts under its own trigger is worse
        # than none.
        self._managed_identity_required = managed
        self._backend_loader = backend_loader or (
            lambda: _load_native_backend(
                session_db_path=session_db_path,
                agent_options=agent_options or {},
                runtime_identity=runtime_identity,
            )
        )
        self._backend: HermesBackend | None = None
        self._load_lock = threading.Lock()
        self._decision_lock = threading.Lock()
        self._decision_inflight: dict[tuple[str, str, str, str], _DecisionClaim] = {}
        self._decision_intents: OrderedDict[
            tuple[str, str, str, str], tuple[str, int]
        ] = OrderedDict()
        self._decision_successes: OrderedDict[
            tuple[str, str, str, str], _DecisionSuccess
        ] = OrderedDict()
        self._decision_identities: OrderedDict[tuple[str, str], _DecisionIdentity] = (
            OrderedDict()
        )
        self._decision_cache_limit = 1024
        self._execution_lock = threading.RLock()
        self._active_executions: dict[tuple[str, str], _AdapterExecution] = {}
        self._action_lock = threading.Lock()
        self._action_outcomes: OrderedDict[str, _ActionOperation] = OrderedDict()
        self._action_cache_limit = 1024

    def _load_backend(self, *, require_compatible: bool = True) -> HermesBackend:
        with self._load_lock:
            if self._backend is None:
                self._backend = self._backend_loader()
            backend = self._backend
        if require_compatible and not backend.compatibility().compatible:
            raise HermesIncompatibleError("hermes_incompatible")
        if (
            require_compatible
            and self._managed_identity_required
            and backend.runtime_identity() is None
        ):
            raise HermesIncompatibleError("runtime_identity_unverified")
        return backend

    @staticmethod
    def _unavailable_capabilities() -> RuntimeCapabilities:
        return RuntimeCapabilities(adapter_id=ADAPTER_ID, available=False)

    async def capabilities(self) -> RuntimeCapabilities:
        try:
            backend = await asyncio.to_thread(
                self._load_backend, require_compatible=False
            )
            capabilities = await asyncio.to_thread(backend.capabilities)
            identity = await asyncio.to_thread(backend.runtime_identity)
            return (
                capabilities
                if identity is not None
                else self._unavailable_capabilities()
            )
        except Exception:
            return self._unavailable_capabilities()

    async def health(self) -> RuntimeHealth:
        try:
            backend = await asyncio.to_thread(
                self._load_backend, require_compatible=False
            )
            report = await asyncio.to_thread(backend.compatibility)
            capabilities = await asyncio.to_thread(backend.capabilities)
            runtime_identity = await asyncio.to_thread(backend.runtime_identity)
        except (HermesUnavailableError, ImportError, ModuleNotFoundError):
            return RuntimeHealth(
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                status=HealthStatus.UNAVAILABLE,
                reason_code="hermes_not_installed",
                capabilities=self._unavailable_capabilities(),
            )
        except Exception:
            return RuntimeHealth(
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                status=HealthStatus.DEGRADED,
                reason_code="hermes_probe_failed",
                capabilities=self._unavailable_capabilities(),
            )
        identity_verified = runtime_identity is not None
        status = (
            HealthStatus.HEALTHY
            if report.compatible and identity_verified
            else HealthStatus.INCOMPATIBLE
        )
        if not report.compatible:
            reason_code = "hermes_incompatible"
        elif not identity_verified:
            reason_code = "runtime_identity_unverified"
        else:
            reason_code = None
        return RuntimeHealth(
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            status=status,
            reason_code=reason_code,
            capabilities=(
                capabilities if identity_verified else self._unavailable_capabilities()
            ),
            compatibility=report.sanitized_summary(),
            runtime_identity=runtime_identity,
        )

    async def handshake(self, pin: ReleasePin) -> RuntimeHandshake:
        try:
            backend = await asyncio.to_thread(
                self._load_backend, require_compatible=False
            )
            report = await asyncio.to_thread(backend.compatibility)
            observed = await asyncio.to_thread(backend.runtime_identity)
        except Exception:
            return RuntimeHandshake(
                expected=pin.runtime_identity,
                observed=None,
                verified=False,
                reason_code="runtime_unavailable",
            )
        if not report.compatible:
            return RuntimeHandshake(
                expected=pin.runtime_identity,
                observed=observed,
                verified=False,
                reason_code="hermes_incompatible",
            )
        if observed is None:
            return RuntimeHandshake(
                expected=pin.runtime_identity,
                observed=None,
                verified=False,
                reason_code="runtime_identity_unverified",
            )
        return RuntimeHandshake(
            expected=pin.runtime_identity,
            observed=observed,
            verified=observed == pin.runtime_identity,
            reason_code=(
                None
                if observed == pin.runtime_identity
                else "runtime_identity_mismatch"
            ),
        )

    @staticmethod
    def _validate_binding(binding: RuntimeBinding) -> None:
        if binding.adapter_id != ADAPTER_ID:
            raise ValueError("binding belongs to a different runtime adapter")

    def _cached_action_outcome(
        self,
        *,
        identity: tuple[str, ...],
        adapter_operation_id: str,
        delivery_epoch: int,
    ) -> RuntimeActionOutcome | None:
        with self._action_lock:
            previous = self._action_outcomes.get(adapter_operation_id)
            if previous is None:
                return None
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
            if previous.outcome.status in {
                ActionOutcomeStatus.ACCEPTED,
                ActionOutcomeStatus.DEDUPLICATED,
            }:
                return RuntimeActionOutcome(
                    adapter_operation_id,
                    delivery_epoch,
                    ActionOutcomeStatus.DEDUPLICATED,
                )
            if delivery_epoch == previous.delivery_epoch:
                return previous.outcome
            return None

    def _remember_action_outcome(
        self,
        *,
        identity: tuple[str, ...],
        outcome: RuntimeActionOutcome,
    ) -> None:
        with self._action_lock:
            self._action_outcomes[outcome.adapter_operation_id] = _ActionOperation(
                identity=identity,
                delivery_epoch=outcome.delivery_epoch,
                outcome=outcome,
            )
            self._action_outcomes.move_to_end(outcome.adapter_operation_id)
            while len(self._action_outcomes) > self._action_cache_limit:
                self._action_outcomes.popitem(last=False)

    @staticmethod
    def _control_result(
        action: ControlAction, outcome: RuntimeActionOutcome
    ) -> ControlResult:
        return ControlResult(
            action=action,
            accepted=outcome.status
            in {ActionOutcomeStatus.ACCEPTED, ActionOutcomeStatus.DEDUPLICATED},
            reason_code=outcome.reason_code,
            adapter_operation_id=outcome.adapter_operation_id,
            delivery_epoch=outcome.delivery_epoch,
            outcome=outcome.status,
        )

    @staticmethod
    def _decision_result(
        decision_id: str, outcome: RuntimeActionOutcome
    ) -> DecisionResult:
        return DecisionResult(
            decision_id=decision_id,
            accepted=outcome.status
            in {ActionOutcomeStatus.ACCEPTED, ActionOutcomeStatus.DEDUPLICATED},
            reason_code=outcome.reason_code,
            adapter_operation_id=outcome.adapter_operation_id,
            delivery_epoch=outcome.delivery_epoch,
            outcome=outcome.status,
        )

    async def open_session(self, request: SessionOpenRequest) -> RuntimeBinding:
        backend = await asyncio.to_thread(self._load_backend)
        session = await asyncio.to_thread(
            backend.open_session,
            request.metadata,
            f"session.open:{request.adapter_operation_id}",
        )
        return RuntimeBinding(
            adapter_id=ADAPTER_ID,
            runtime_session_ref=session.session_ref,
            generation=0,
            adapter_version=ADAPTER_VERSION,
            parent_runtime_session_ref=session.parent_session_ref,
        )

    async def load_session(self, binding: RuntimeBinding) -> RuntimeBinding:
        self._validate_binding(binding)
        backend = await asyncio.to_thread(self._load_backend)
        session = await asyncio.to_thread(
            backend.load_session, binding.runtime_session_ref
        )
        return RuntimeBinding(
            adapter_id=ADAPTER_ID,
            runtime_session_ref=session.session_ref,
            generation=binding.generation,
            adapter_version=ADAPTER_VERSION,
            parent_runtime_session_ref=session.parent_session_ref,
        )

    async def fork_session(
        self, binding: RuntimeBinding, request: SessionOpenRequest
    ) -> RuntimeBinding:
        self._validate_binding(binding)
        backend = await asyncio.to_thread(self._load_backend)
        session = await asyncio.to_thread(
            backend.fork_session,
            binding.runtime_session_ref,
            request.metadata,
            f"session.fork:{request.adapter_operation_id}",
        )
        return RuntimeBinding(
            adapter_id=ADAPTER_ID,
            runtime_session_ref=session.session_ref,
            generation=binding.generation + 1,
            adapter_version=ADAPTER_VERSION,
            parent_runtime_session_ref=binding.runtime_session_ref,
        )

    async def _run_backend(
        self,
        backend: HermesBackend,
        execution_token: object,
        request: AttemptRequest,
        queue: asyncio.Queue[tuple[str, Any]],
    ) -> None:
        loop = asyncio.get_running_loop()

        def emit(signal: HermesSignal) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("signal", signal))

        run_input = HermesRunInput(
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            session_ref=request.binding.runtime_session_ref,
            user_message=request.user_message,
            system_message=request.system_message,
            conversation_history=request.conversation_history,
            metadata=request.metadata,
            adapter_operation_id=request.adapter_operation_id,
            execution_token=execution_token,
        )
        try:
            result = await asyncio.to_thread(backend.run, run_input, emit)
        except RuntimeOperationUncertain as exc:
            # Typed, not folded into `runtime_execution_failed`: "it failed" and
            # "we do not know whether it ran" are different facts, and only one
            # of them makes an automatic retry unsafe.
            await queue.put(
                ("error", ("runtime_operation_uncertain", failure_detail(exc)))
            )
        except Exception as exc:
            # ⟦P5.4d⟧ The category says which of four things happened; the
            # detail says what actually raised. Without it every product-side
            # fault in the whole managed path is one word.
            await queue.put(
                ("error", ("runtime_execution_failed", failure_detail(exc)))
            )
        else:
            await queue.put(("result", result))

    async def _take_event_causation(
        self,
        execution_key: tuple[str, str],
        execution: _AdapterExecution,
    ) -> tuple[str, int] | None:
        with self._decision_lock:
            pending = [
                claim.future
                for key, claim in self._decision_inflight.items()
                if key[1:3] == execution_key
            ]
        for future in pending:
            await asyncio.shield(asyncio.wrap_future(future))
        with self._execution_lock:
            if self._active_executions.get(execution_key) is not execution:
                return None
            cause = execution.accepted_decision_operation
            if cause is None:
                return None
            execution.accepted_decision_operation = None
        return cause

    async def _attach_event_causation(
        self,
        execution_key: tuple[str, str],
        execution: _AdapterExecution,
        event: RuntimeEvent,
    ) -> RuntimeEvent:
        if event.durability != EventDurability.DURABLE:
            return event
        cause = await self._take_event_causation(execution_key, execution)
        if cause is None:
            return event
        return replace(
            event,
            caused_by_adapter_operation_id=cause[0],
            caused_by_delivery_epoch=cause[1],
        )

    async def execute(self, request: AttemptRequest) -> AsyncIterator[RuntimeEvent]:
        self._validate_binding(request.binding)
        event_factory = RuntimeEventFactory(request)
        execution_key = (request.run_id, request.attempt_id)
        adapter_execution = _AdapterExecution(
            session_ref=request.binding.runtime_session_ref,
        )
        with self._execution_lock:
            if execution_key in self._active_executions:
                raise HermesDuplicateAttemptError("duplicate_active_attempt")
            self._active_executions[execution_key] = adapter_execution

        backend: HermesBackend | None = None
        backend_token: object | None = None
        worker: asyncio.Task[None] | None = None

        async def release_ownership(*, cancel_running: bool) -> None:
            nonlocal backend_token
            token = backend_token
            try:
                if backend is not None and token is not None:
                    if worker is not None:
                        if cancel_running and not worker.done():
                            try:
                                await asyncio.to_thread(
                                    backend.cancel,
                                    request.run_id,
                                    request.attempt_id,
                                    f"{request.adapter_operation_id}:cleanup-cancel",
                                    0,
                                    request.binding.runtime_session_ref,
                                )
                            except Exception:
                                pass
                        await worker
                    try:
                        await asyncio.to_thread(
                            backend.release_attempt,
                            request.run_id,
                            request.attempt_id,
                            token,
                        )
                    except Exception:
                        pass
            finally:
                backend_token = None
                with self._execution_lock:
                    if self._active_executions.get(execution_key) is adapter_execution:
                        self._active_executions.pop(execution_key, None)

        try:
            try:
                backend = await asyncio.to_thread(self._load_backend)
            except Exception:
                yield event_factory.event(
                    "runtime.run.failed",
                    EventDurability.DURABLE,
                    {"category": "runtime_unavailable", "retryable": False},
                )
                return

            backend_token = await asyncio.to_thread(
                backend.reserve_attempt, request.run_id, request.attempt_id
            )
            if backend_token is None:
                raise HermesDuplicateAttemptError("duplicate_active_attempt")
            with self._execution_lock:
                current = self._active_executions.get(execution_key)
                if current is not adapter_execution:
                    raise RuntimeError("adapter execution ownership lost")
                adapter_execution.backend = backend
                canceled_before_started = adapter_execution.cancel_requested
            if canceled_before_started:
                await asyncio.to_thread(
                    backend.cancel,
                    request.run_id,
                    request.attempt_id,
                    f"{request.adapter_operation_id}:pre-start-cancel",
                    0,
                    request.binding.runtime_session_ref,
                )

            yield await self._attach_event_causation(
                execution_key,
                adapter_execution,
                event_factory.event(
                    "runtime.run.started",
                    EventDurability.DURABLE,
                    {"binding_generation": request.binding.generation},
                ),
            )

            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            worker = asyncio.create_task(
                self._run_backend(backend, backend_token, request, queue)
            )
            terminal_seen = False
            while not terminal_seen:
                item_type, value = await queue.get()
                if item_type == "signal":
                    signal = value
                    if signal.kind == "decision.required":
                        decision_id = signal.payload.get("decision_id")
                        if isinstance(decision_id, str) and decision_id:
                            self._register_pending_decision(
                                session_ref=request.binding.runtime_session_ref,
                                run_id=request.run_id,
                                attempt_id=request.attempt_id,
                                decision_id=decision_id,
                                revision=0,
                            )
                    yield await self._attach_event_causation(
                        execution_key,
                        adapter_execution,
                        translate_hermes_signal(
                            event_factory,
                            signal.kind,
                            signal.payload,
                            stable_source_id=signal.stable_id,
                        ),
                    )
                    continue
                terminal_seen = True
                terminal_cause = await self._take_event_causation(
                    execution_key, adapter_execution
                )
                await release_ownership(cancel_running=False)

                def terminal_event(event: RuntimeEvent) -> RuntimeEvent:
                    nonlocal terminal_cause
                    cause = terminal_cause
                    if cause is None:
                        return event
                    terminal_cause = None
                    return replace(
                        event,
                        caused_by_adapter_operation_id=cause[0],
                        caused_by_delivery_epoch=cause[1],
                    )

                if item_type == "error":
                    category, detail = value
                    yield terminal_event(
                        event_factory.event(
                            "runtime.run.failed",
                            EventDurability.DURABLE,
                            {
                                "category": category,
                                # An uncertain outcome is not retryable by the
                                # system; it is retryable by an operator, as a
                                # new attempt with a new operation id.
                                "retryable": category != "runtime_operation_uncertain",
                                "detail": detail,
                            },
                        ),
                    )
                    continue
                result: HermesRunResult = value
                if result.session_ref != request.binding.runtime_session_ref:
                    yield terminal_event(
                        event_factory.event(
                            "runtime.session_rebound",
                            EventDurability.DURABLE,
                            {
                                "adapter_id": ADAPTER_ID,
                                "runtime_session_ref": result.session_ref,
                                "generation": request.binding.generation + 1,
                                "parent_runtime_session_ref": request.binding.runtime_session_ref,
                            },
                        ),
                    )
                if result.final_response and not result.canceled and not result.failed:
                    yield terminal_event(
                        event_factory.event(
                            "runtime.message.completed",
                            EventDurability.DURABLE,
                            {"role": "assistant", "content": result.final_response},
                        ),
                    )
                if result.canceled:
                    terminal_type = "runtime.run.canceled"
                    terminal_payload: Mapping[str, Any] = {}
                elif result.failed:
                    terminal_type = "runtime.run.failed"
                    terminal_payload = {
                        "category": "runtime_execution_failed",
                        "retryable": True,
                        # Not the same fact as a product-side exception, and an
                        # operator has to be able to tell them apart: this one
                        # means the turn ran and the worker refused to say why.
                        "detail": WORKER_REPORTED_FAILURE,
                    }
                else:
                    terminal_type = "runtime.run.completed"
                    terminal_payload = {}
                yield terminal_event(
                    event_factory.event(
                        terminal_type,
                        EventDurability.DURABLE,
                        terminal_payload,
                    ),
                )
        finally:
            await release_ownership(cancel_running=True)

    async def request_control(self, request: ControlRequest) -> ControlResult:
        self._validate_binding(request.binding)
        text_digest = (
            hashlib.sha256(request.text.encode("utf-8")).hexdigest()
            if request.text is not None
            else ""
        )
        identity = (
            f"control.{request.action.value}",
            request.binding.runtime_session_ref,
            request.run_id,
            request.attempt_id,
            text_digest,
        )
        cached = self._cached_action_outcome(
            identity=identity,
            adapter_operation_id=request.adapter_operation_id,
            delivery_epoch=request.delivery_epoch,
        )
        if cached is not None:
            return self._control_result(request.action, cached)
        if request.action == ControlAction.PAUSE:
            outcome = RuntimeActionOutcome(
                request.adapter_operation_id,
                request.delivery_epoch,
                ActionOutcomeStatus.REJECTED,
                "pause_requires_cortex_stage_boundary",
            )
            self._remember_action_outcome(identity=identity, outcome=outcome)
            return self._control_result(request.action, outcome)
        execution_key = (request.run_id, request.attempt_id)
        with self._execution_lock:
            execution = self._active_executions.get(execution_key)
            if execution is None:
                outcome = RuntimeActionOutcome(
                    request.adapter_operation_id,
                    request.delivery_epoch,
                    ActionOutcomeStatus.REJECTED,
                    "run_not_active",
                )
                self._remember_action_outcome(identity=identity, outcome=outcome)
                return self._control_result(request.action, outcome)
            if execution.session_ref != request.binding.runtime_session_ref:
                outcome = RuntimeActionOutcome(
                    request.adapter_operation_id,
                    request.delivery_epoch,
                    ActionOutcomeStatus.REJECTED,
                    "runtime_binding_mismatch",
                )
                self._remember_action_outcome(identity=identity, outcome=outcome)
                return self._control_result(request.action, outcome)
            if request.action == ControlAction.CANCEL:
                execution.cancel_requested = True
            backend = execution.backend

        if request.action == ControlAction.CANCEL:
            if backend is not None:
                try:
                    outcome = await asyncio.to_thread(
                        backend.cancel,
                        request.run_id,
                        request.attempt_id,
                        request.adapter_operation_id,
                        request.delivery_epoch,
                        request.binding.runtime_session_ref,
                    )
                except Exception:
                    outcome = RuntimeActionOutcome(
                        request.adapter_operation_id,
                        request.delivery_epoch,
                        ActionOutcomeStatus.UNKNOWN,
                        "runtime_outcome_unknown",
                    )
            else:
                outcome = RuntimeActionOutcome(
                    request.adapter_operation_id,
                    request.delivery_epoch,
                    ActionOutcomeStatus.ACCEPTED,
                )
        else:
            if not request.text:
                outcome = RuntimeActionOutcome(
                    request.adapter_operation_id,
                    request.delivery_epoch,
                    ActionOutcomeStatus.REJECTED,
                    "steer_text_required",
                )
                self._remember_action_outcome(identity=identity, outcome=outcome)
                return self._control_result(request.action, outcome)
            if backend is None:
                outcome = RuntimeActionOutcome(
                    request.adapter_operation_id,
                    request.delivery_epoch,
                    ActionOutcomeStatus.REJECTED,
                    "run_not_active",
                )
                self._remember_action_outcome(identity=identity, outcome=outcome)
                return self._control_result(request.action, outcome)
            try:
                outcome = await asyncio.to_thread(
                    backend.steer,
                    request.run_id,
                    request.attempt_id,
                    request.text,
                    request.adapter_operation_id,
                    request.delivery_epoch,
                )
            except Exception:
                outcome = RuntimeActionOutcome(
                    request.adapter_operation_id,
                    request.delivery_epoch,
                    ActionOutcomeStatus.UNKNOWN,
                    "runtime_outcome_unknown",
                )
        self._remember_action_outcome(identity=identity, outcome=outcome)
        return self._control_result(request.action, outcome)

    def _register_pending_decision(
        self,
        *,
        session_ref: str,
        run_id: str,
        attempt_id: str,
        decision_id: str,
        revision: int,
    ) -> None:
        key = (session_ref, decision_id)
        with self._decision_lock:
            self._decision_identities[key] = _DecisionIdentity(
                run_id=run_id,
                attempt_id=attempt_id,
                revision=revision,
            )
            self._decision_identities.move_to_end(key)
            while len(self._decision_identities) > self._decision_cache_limit:
                self._decision_identities.popitem(last=False)

    @staticmethod
    def _decision_action_identity(
        resolution: DecisionResolution,
    ) -> tuple[str, ...]:
        return (
            "decision.resolve",
            resolution.binding.runtime_session_ref,
            resolution.run_id,
            resolution.attempt_id,
            resolution.decision_id,
            resolution.choice,
            str(resolution.revision),
        )

    def _remember_decision_intent(
        self,
        key: tuple[str, str, str, str],
        choice: str,
        revision: int,
    ) -> None:
        self._decision_intents[key] = (choice, revision)
        self._decision_intents.move_to_end(key)
        while len(self._decision_intents) > self._decision_cache_limit:
            removable = next(
                (
                    candidate
                    for candidate in self._decision_intents
                    if candidate not in self._decision_inflight
                ),
                None,
            )
            if removable is None:
                break
            self._decision_intents.pop(removable, None)
            self._decision_successes.pop(removable, None)

    async def _complete_decision_claim(
        self,
        key: tuple[str, str, str, str],
        resolution: DecisionResolution,
        claim: _DecisionClaim,
    ) -> None:
        try:
            backend = await asyncio.to_thread(self._load_backend)
            outcome = await asyncio.to_thread(
                backend.resolve_decision,
                resolution.binding.runtime_session_ref,
                resolution.run_id,
                resolution.attempt_id,
                resolution.decision_id,
                resolution.choice,
                resolution.adapter_operation_id,
                resolution.delivery_epoch,
            )
            result = self._decision_result(resolution.decision_id, outcome)
        except Exception:
            result = self._decision_result(
                resolution.decision_id,
                RuntimeActionOutcome(
                    resolution.adapter_operation_id,
                    resolution.delivery_epoch,
                    ActionOutcomeStatus.UNKNOWN,
                    "runtime_outcome_unknown",
                ),
            )

        if (
            result.accepted
            and resolution.delivery_epoch >= 1
            and resolution.adapter_operation_id.startswith("runtime-action:")
        ):
            execution_key = (resolution.run_id, resolution.attempt_id)
            with self._execution_lock:
                execution = self._active_executions.get(execution_key)
                if (
                    execution is not None
                    and execution.session_ref
                    == resolution.binding.runtime_session_ref
                ):
                    execution.accepted_decision_operation = (
                        resolution.adapter_operation_id,
                        resolution.delivery_epoch,
                    )

        with self._decision_lock:
            if self._decision_inflight.get(key) is claim:
                self._decision_inflight.pop(key, None)
            if result.accepted:
                self._decision_successes[key] = _DecisionSuccess(
                    choice=resolution.choice,
                    revision=resolution.revision,
                    result=result,
                )
                self._decision_successes.move_to_end(key)
                while len(self._decision_successes) > self._decision_cache_limit:
                    removed, _ = self._decision_successes.popitem(last=False)
                    self._decision_intents.pop(removed, None)
            self._remember_decision_intent(key, resolution.choice, resolution.revision)
            if not claim.future.done():
                claim.future.set_result(result)
        self._remember_action_outcome(
            identity=self._decision_action_identity(resolution),
            outcome=RuntimeActionOutcome(
                resolution.adapter_operation_id,
                resolution.delivery_epoch,
                result.outcome or ActionOutcomeStatus.UNKNOWN,
                result.reason_code,
            ),
        )

    async def resolve_decision(self, resolution: DecisionResolution) -> DecisionResult:
        self._validate_binding(resolution.binding)
        action_identity = self._decision_action_identity(resolution)
        cached = self._cached_action_outcome(
            identity=action_identity,
            adapter_operation_id=resolution.adapter_operation_id,
            delivery_epoch=resolution.delivery_epoch,
        )
        if cached is not None:
            return self._decision_result(resolution.decision_id, cached)

        def rejected(reason_code: str) -> DecisionResult:
            outcome = RuntimeActionOutcome(
                resolution.adapter_operation_id,
                resolution.delivery_epoch,
                ActionOutcomeStatus.REJECTED,
                reason_code,
            )
            self._remember_action_outcome(
                identity=action_identity,
                outcome=outcome,
            )
            return self._decision_result(resolution.decision_id, outcome)

        if resolution.choice not in {"approve_once", "deny"}:
            return rejected("unsupported_decision_choice")
        key = (
            resolution.binding.runtime_session_ref,
            resolution.run_id,
            resolution.attempt_id,
            resolution.decision_id,
        )
        with self._decision_lock:
            identity = self._decision_identities.get(
                (
                    resolution.binding.runtime_session_ref,
                    resolution.decision_id,
                )
            )
            if identity is None:
                return rejected("pending_decision_missing")
            if (
                identity.run_id != resolution.run_id
                or identity.attempt_id != resolution.attempt_id
            ):
                return rejected("decision_identity_mismatch")
            if identity.revision != resolution.revision:
                return rejected("decision_revision_conflict")
            intent = self._decision_intents.get(key)
            if intent is not None:
                self._decision_intents.move_to_end(key)
                intent_choice, intent_revision = intent
                if intent_revision != resolution.revision:
                    return rejected("decision_revision_conflict")
                if intent_choice != resolution.choice:
                    return rejected("decision_conflict")
            success = self._decision_successes.get(key)
            if success is not None:
                self._decision_successes.move_to_end(key)
                if success.revision != resolution.revision:
                    return rejected("decision_revision_conflict")
                if success.choice != resolution.choice:
                    return rejected("decision_conflict")
                outcome = RuntimeActionOutcome(
                    resolution.adapter_operation_id,
                    resolution.delivery_epoch,
                    ActionOutcomeStatus.DEDUPLICATED,
                )
                self._remember_action_outcome(
                    identity=action_identity,
                    outcome=outcome,
                )
                return self._decision_result(resolution.decision_id, outcome)
            claim = self._decision_inflight.get(key)
            if claim is not None and claim.revision != resolution.revision:
                return rejected("decision_revision_conflict")
            if claim is not None and claim.choice != resolution.choice:
                return rejected("decision_conflict")
            owns_claim = claim is None
            if claim is None:
                claim = _DecisionClaim(
                    choice=resolution.choice,
                    revision=resolution.revision,
                )
                self._decision_inflight[key] = claim
                self._remember_decision_intent(
                    key, resolution.choice, resolution.revision
                )

        if owns_claim:
            asyncio.create_task(self._complete_decision_claim(key, resolution, claim))
        result = await asyncio.shield(asyncio.wrap_future(claim.future))
        if not owns_claim and result.accepted:
            outcome = RuntimeActionOutcome(
                resolution.adapter_operation_id,
                resolution.delivery_epoch,
                ActionOutcomeStatus.DEDUPLICATED,
            )
            self._remember_action_outcome(
                identity=action_identity,
                outcome=outcome,
            )
            return self._decision_result(resolution.decision_id, outcome)
        return result

    async def query_action_outcome(
        self, query: ActionOutcomeQuery
    ) -> RuntimeActionOutcome:
        self._validate_binding(query.binding)
        with self._action_lock:
            previous = self._action_outcomes.get(query.adapter_operation_id)
            if previous is not None:
                identity = previous.identity
                exact_target = len(identity) >= 4 and identity[1:4] == (
                    query.binding.runtime_session_ref,
                    query.run_id,
                    query.attempt_id,
                )
                if not exact_target:
                    return RuntimeActionOutcome(
                        query.adapter_operation_id,
                        query.delivery_epoch,
                        ActionOutcomeStatus.REJECTED,
                        "adapter_operation_conflict",
                    )
                if query.delivery_epoch < previous.delivery_epoch:
                    return RuntimeActionOutcome(
                        query.adapter_operation_id,
                        query.delivery_epoch,
                        ActionOutcomeStatus.REJECTED,
                        "stale_delivery_epoch",
                    )
                return previous.outcome
        try:
            backend = await asyncio.to_thread(self._load_backend)
            return await asyncio.to_thread(
                backend.query_action_outcome,
                query.binding.runtime_session_ref,
                query.run_id,
                query.attempt_id,
                query.adapter_operation_id,
                query.delivery_epoch,
            )
        except Exception:
            return RuntimeActionOutcome(
                query.adapter_operation_id,
                query.delivery_epoch,
                ActionOutcomeStatus.UNKNOWN,
                "operation_outcome_unknown",
            )

    async def inspect(self, binding: RuntimeBinding) -> SessionInspection:
        self._validate_binding(binding)
        backend = await asyncio.to_thread(self._load_backend)
        inspection = await asyncio.to_thread(
            backend.inspect, binding.runtime_session_ref
        )
        return SessionInspection(
            binding=binding,
            exists=inspection.exists,
            active=inspection.active,
            metadata=inspection.metadata,
        )

    async def recover(
        self, binding: RuntimeBinding, checkpoint: RuntimeCheckpoint
    ) -> RuntimeBinding:
        self._validate_binding(binding)
        backend = await asyncio.to_thread(self._load_backend)
        session = await asyncio.to_thread(
            backend.recover,
            binding.runtime_session_ref,
            checkpoint,
            f"session.recover:{checkpoint.adapter_operation_id}",
        )
        return RuntimeBinding(
            adapter_id=ADAPTER_ID,
            runtime_session_ref=session.session_ref,
            generation=binding.generation + 1,
            adapter_version=ADAPTER_VERSION,
            parent_runtime_session_ref=binding.runtime_session_ref,
        )
