from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from cortex_platform.runtime.hermes import (
    HermesAdapter,
    HermesRunInput,
    _NativeHermesBackend,
    _PendingDecision,
)
from cortex_platform.runtime.models import (
    WORKER_REPORTED_FAILURE,
    ActionOutcomeStatus,
    AttemptRequest,
    ControlAction,
    ControlRequest,
    DecisionResolution,
    RuntimeCheckpoint,
    SessionOpenRequest,
)


class MemorySessionDB:
    def __init__(self, path=None) -> None:
        self.sessions = {}
        self.messages = {}
        self.replace_calls = []

    def create_session(self, session_id, source, **kwargs):
        self.sessions[session_id] = {
            "session_id": session_id,
            "source": source,
            **kwargs,
        }
        self.messages.setdefault(session_id, [])
        return session_id

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_messages_as_conversation(self, session_id):
        return list(self.messages.get(session_id, []))

    def replace_messages(self, session_id, messages):
        replacement = list(messages)
        self.replace_calls.append((session_id, replacement))
        self.messages[session_id] = replacement


class ApprovalSlot:
    def __init__(self) -> None:
        self._callbacks = {}
        self._lock = threading.Lock()

    def set(self, callback) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            if callback is None:
                self._callbacks.pop(thread_id, None)
            else:
                self._callbacks[thread_id] = callback

    @property
    def callback(self):
        with self._lock:
            return self._callbacks.get(threading.get_ident())

    @property
    def active_callbacks(self) -> int:
        with self._lock:
            return len(self._callbacks)


class FakeNativeAgent:
    def __init__(
        self,
        session_id=None,
        session_db=None,
        parent_session_id=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        step_callback=None,
        approval_slot=None,
        provider=None,
        model="",
    ) -> None:
        self.session_id = session_id
        self.session_db = session_db
        self.parent_session_id = parent_session_id
        self.tool_progress_callback = tool_progress_callback
        self.tool_start_callback = tool_start_callback
        self.tool_complete_callback = tool_complete_callback
        self.step_callback = step_callback
        self.approval_slot = approval_slot
        self.provider = provider
        self.model = model
        self.interrupted = False
        self.steers = []

    def run_conversation(
        self,
        user_message,
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
    ):
        self.tool_start_callback("tool-native", "paper_search", {"q": "memory"})
        stream_callback("native delta")
        choice = self.approval_slot.callback(
            "echo safe", "native approval", allow_permanent=False
        )
        self.tool_complete_callback(
            "tool-native", "paper_search", {"q": "memory"}, "ok"
        )
        self.step_callback(1, [])
        self.session_id = f"{self.session_id}-compressed"
        return {"final_response": f"approved:{choice}"}

    def interrupt(self, message=None) -> None:
        self.interrupted = True

    @property
    def is_interrupted(self) -> bool:
        return self.interrupted

    def steer(self, text) -> bool:
        self.steers.append(text)
        return bool(text)


def test_native_seam_matches_verified_hermes_surface(monkeypatch) -> None:
    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        state_module = SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        )
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=state_module,
            agent_class=FakeNativeAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={
                "provider": "fake-provider",
                "model": "fake-model",
                "approval_slot": approval_slot,
            },
        )
        report = backend.compatibility()
        assert report.compatible is True
        assert report.session_db_schema == 13
        assert report.provider_models == {"fake-provider": ("fake-model",)}

        adapter = HermesAdapter(backend_loader=lambda: backend)
        root = await adapter.open_session(SessionOpenRequest())
        child = await adapter.fork_session(root, SessionOpenRequest())
        assert child.parent_runtime_session_ref == root.runtime_session_ref

        request = AttemptRequest(
            run_id="native-run",
            attempt_id="native-attempt",
            binding=child,
            user_message="test",
        )
        events = []
        async for event in adapter.execute(request):
            events.append(event)
            if event.type == "runtime.decision.required":
                result = await adapter.resolve_decision(
                    DecisionResolution(
                        run_id=request.run_id,
                        attempt_id=request.attempt_id,
                        binding=child,
                        decision_id=str(event.payload["decision_id"]),
                        choice="approve_once",
                        revision=0,
                    )
                )
                assert result.accepted is True

        assert [event.type for event in events] == [
            "runtime.run.started",
            "runtime.tool.started",
            "runtime.token.delta",
            "runtime.decision.required",
            "runtime.tool.completed",
            "runtime.status",
            "runtime.session_rebound",
            "runtime.message.completed",
            "runtime.run.completed",
        ]
        completed = next(
            event for event in events if event.type == "runtime.message.completed"
        )
        assert completed.payload["content"] == "approved:once"
        assert approval_slot.active_callbacks == 0

    asyncio.run(scenario())


def test_native_cancel_denies_only_target_attempt_approvals(monkeypatch) -> None:
    approval_slot = ApprovalSlot()
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=FakeNativeAgent,
        set_approval_callback=approval_slot.set,
        session_db_path=None,
        agent_options={"approval_slot": approval_slot},
    )
    target = _PendingDecision("run-1", "attempt-1")
    unrelated = _PendingDecision("run-2", "attempt-2")
    agent = FakeNativeAgent(approval_slot=approval_slot)
    backend._pending[("session-1", "decision-1")] = target
    backend._pending[("session-2", "decision-2")] = unrelated
    token = backend.reserve_attempt("run-1", "attempt-1")
    assert token is not None
    backend._executions[("run-1", "attempt-1")].agent = agent
    backend._executions[("run-1", "attempt-1")].started = True

    assert backend.cancel("run-1", "attempt-1").status == ActionOutcomeStatus.ACCEPTED
    assert target.event.is_set() is True
    assert target.choice == "deny"
    assert unrelated.event.is_set() is False
    assert agent.interrupted is True


def test_native_approval_waits_for_explicit_cancel(monkeypatch) -> None:
    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=FakeNativeAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="native-wait-run",
            attempt_id="native-wait-attempt",
            binding=binding,
            user_message="wait for control",
        )
        stream = adapter.execute(request)

        observed = []
        while True:
            event = await anext(stream)
            observed.append(event)
            if event.type == "runtime.decision.required":
                break

        blocked_next = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)
        assert blocked_next.done() is False

        canceled = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert canceled.accepted is True
        observed.append(await blocked_next)
        observed.extend([event async for event in stream])

        assert backend._pending == {}
        assert approval_slot.active_callbacks == 0
        assert observed[-1].type == "runtime.run.canceled"

    asyncio.run(scenario())


def test_native_recovery_always_replaces_empty_nonempty_and_repeated(
    monkeypatch,
) -> None:
    approval_slot = ApprovalSlot()
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=FakeNativeAgent,
        set_approval_callback=approval_slot.set,
        session_db_path=None,
        agent_options={"approval_slot": approval_slot},
    )
    parent = backend.open_session({})
    db = backend._session_db()
    db.replace_messages(parent.session_ref, [{"role": "user", "content": "parent"}])

    nonempty = RuntimeCheckpoint(
        "checkpoint-nonempty",
        ({"role": "assistant", "content": "checkpoint"},),
    )
    first = backend.recover(parent.session_ref, nonempty)
    repeated = backend.recover(parent.session_ref, nonempty)
    empty = backend.recover(
        parent.session_ref, RuntimeCheckpoint("checkpoint-empty", ())
    )

    assert db.messages[first.session_ref] == list(nonempty.conversation_history)
    assert db.messages[repeated.session_ref] == list(nonempty.conversation_history)
    assert db.messages[empty.session_ref] == []
    assert db.messages[parent.session_ref] == [{"role": "user", "content": "parent"}]
    assert (empty.session_ref, []) in db.replace_calls


@pytest.mark.parametrize("callable_state", [False, True])
def test_native_probe_accepts_real_interrupt_state_shapes(
    monkeypatch, callable_state
) -> None:
    class CallableInterruptStateAgent(FakeNativeAgent):
        def is_interrupted(self) -> bool:
            return self.interrupted

    agent_class = CallableInterruptStateAgent if callable_state else FakeNativeAgent
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=agent_class,
        set_approval_callback=ApprovalSlot().set,
        session_db_path=None,
        agent_options={"approval_slot": ApprovalSlot()},
    )

    report = backend.compatibility()
    interrupt_state = next(
        check for check in report.checks if check.name == "interrupt_state"
    )
    assert interrupt_state.observed == ("callable" if callable_state else "property")
    assert interrupt_state.compatible is True
    assert report.compatible is True


def test_native_probe_rejects_missing_interrupt_state(monkeypatch) -> None:
    class MissingInterruptStateAgent(FakeNativeAgent):
        is_interrupted = None

    approval_slot = ApprovalSlot()
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=MissingInterruptStateAgent,
        set_approval_callback=approval_slot.set,
        session_db_path=None,
        agent_options={"approval_slot": approval_slot},
    )

    report = backend.compatibility()
    interrupt_state = next(
        check for check in report.checks if check.name == "interrupt_state"
    )
    assert interrupt_state.observed == "missing"
    assert interrupt_state.compatible is False
    assert report.compatible is False


@pytest.mark.parametrize(
    ("agent_class", "state_class", "setter", "failed_check"),
    [
        (
            type(
                "ExtraConstructorRequirement",
                (FakeNativeAgent,),
                {
                    "__init__": lambda self, *args, required_new, **kwargs: None,
                },
            ),
            MemorySessionDB,
            ApprovalSlot().set,
            "agent_callbacks",
        ),
        (
            FakeNativeAgent,
            type(
                "ExtraSessionRequirement",
                (MemorySessionDB,),
                {
                    "replace_messages": lambda self, session_id, messages, required_new: (
                        None
                    ),
                },
            ),
            ApprovalSlot().set,
            "session_db_methods",
        ),
        (
            type(
                "ExtraRunRequirement",
                (FakeNativeAgent,),
                {
                    "run_conversation": lambda self, user_message, *, required_new: {},
                },
            ),
            MemorySessionDB,
            ApprovalSlot().set,
            "control_methods",
        ),
        (
            FakeNativeAgent,
            MemorySessionDB,
            lambda callback, required_new: None,
            "approval_bridge",
        ),
    ],
)
def test_native_probe_rejects_extra_required_call_parameters(
    monkeypatch, agent_class, state_class, setter, failed_check
) -> None:
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(SCHEMA_VERSION=13, SessionDB=state_class),
        agent_class=agent_class,
        set_approval_callback=setter,
        session_db_path=None,
        agent_options={"approval_slot": ApprovalSlot()},
    )

    report = backend.compatibility()
    check = next(check for check in report.checks if check.name == failed_check)
    assert check.compatible is False
    assert report.compatible is False


def test_native_pre_cancel_is_consumed_before_agent_construction_and_retries(
    monkeypatch,
) -> None:
    class CountingAgent(FakeNativeAgent):
        constructions = 0

        def __init__(self, *args, **kwargs):
            type(self).constructions += 1
            super().__init__(*args, **kwargs)

    approval_slot = ApprovalSlot()
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=CountingAgent,
        set_approval_callback=approval_slot.set,
        session_db_path=None,
        agent_options={"approval_slot": approval_slot},
    )
    session = backend.open_session({})
    token = backend.reserve_attempt("run-pre-cancel", "attempt-pre-cancel")
    assert token is not None
    assert (
        backend.cancel(
            "run-pre-cancel", "attempt-pre-cancel", "native-pre-cancel", 0
        ).status
        == ActionOutcomeStatus.ACCEPTED
    )
    assert (
        backend.cancel(
            "run-pre-cancel", "attempt-pre-cancel", "native-pre-cancel", 0
        ).status
        == ActionOutcomeStatus.DEDUPLICATED
    )

    result = backend.run(
        HermesRunInput(
            run_id="run-pre-cancel",
            attempt_id="attempt-pre-cancel",
            session_ref=session.session_ref,
            user_message="must not start",
            system_message=None,
            conversation_history=(),
            metadata={},
            execution_token=token,
        ),
        lambda signal: None,
    )

    assert result.canceled is True
    assert CountingAgent.constructions == 0
    assert backend._executions == {}
    retry_token = backend.reserve_attempt("run-pre-cancel", "attempt-pre-cancel")
    assert retry_token is not None
    backend.release_attempt("run-pre-cancel", "attempt-pre-cancel", retry_token)


def test_native_callback_install_failure_cleans_execution_ownership(
    monkeypatch,
) -> None:
    approval_slot = ApprovalSlot()

    def broken_setter(callback):
        raise RuntimeError("callback install failed")

    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=FakeNativeAgent,
        set_approval_callback=broken_setter,
        session_db_path=None,
        agent_options={"approval_slot": approval_slot},
    )
    session = backend.open_session({})
    token = backend.reserve_attempt("run-install-fail", "attempt-install-fail")
    assert token is not None

    with pytest.raises(RuntimeError, match="callback install failed"):
        backend.run(
            HermesRunInput(
                run_id="run-install-fail",
                attempt_id="attempt-install-fail",
                session_ref=session.session_ref,
                user_message="fail callback",
                system_message=None,
                conversation_history=(),
                metadata={},
                execution_token=token,
            ),
            lambda signal: None,
        )

    assert backend._executions == {}
    assert backend._pending == {}


def test_cancel_during_agent_construction_prevents_conversation_side_effects(
    monkeypatch,
) -> None:
    class BlockingConstructorAgent(FakeNativeAgent):
        constructor_entered = threading.Event()
        release_constructor = threading.Event()
        conversation_calls = 0

        def __init__(self, *args, **kwargs):
            type(self).constructor_entered.set()
            type(self).release_constructor.wait(2)
            super().__init__(*args, **kwargs)

        def run_conversation(self, *args, **kwargs):
            type(self).conversation_calls += 1
            return super().run_conversation(*args, **kwargs)

    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=BlockingConstructorAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-constructor-race",
            attempt_id="attempt-constructor-race",
            binding=binding,
            user_message="cancel during construction",
        )
        stream = adapter.execute(request)
        assert (await anext(stream)).type == "runtime.run.started"
        terminal = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(
            BlockingConstructorAgent.constructor_entered.wait, 1
        )

        canceled = await adapter.request_control(
            ControlRequest(
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                binding=binding,
                action=ControlAction.CANCEL,
            )
        )
        assert canceled.accepted is True
        BlockingConstructorAgent.release_constructor.set()
        assert (await terminal).type == "runtime.run.canceled"
        await stream.aclose()

        assert BlockingConstructorAgent.conversation_calls == 0
        assert backend._executions == {}
        assert backend._pending == {}
        assert approval_slot.active_callbacks == 0

    asyncio.run(scenario())


def test_closing_stream_at_pending_decision_releases_waiter_and_callback(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=FakeNativeAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        stream = adapter.execute(
            AttemptRequest(
                run_id="run-close-pending",
                attempt_id="attempt-close-pending",
                binding=binding,
                user_message="close pending",
            )
        )
        while (await anext(stream)).type != "runtime.decision.required":
            pass

        await asyncio.wait_for(stream.aclose(), timeout=1)
        assert backend._executions == {}
        assert backend._pending == {}
        assert adapter._active_executions == {}
        assert approval_slot.active_callbacks == 0

    asyncio.run(scenario())


def test_callback_clear_failure_emits_failed_and_cleans_all_native_state(
    monkeypatch,
) -> None:
    class ClearFailSlot(ApprovalSlot):
        def set(self, callback) -> None:
            if callback is None:
                super().set(None)
                raise RuntimeError("callback clear failed")
            super().set(callback)

    async def scenario() -> None:
        approval_slot = ClearFailSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=FakeNativeAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        request = AttemptRequest(
            run_id="run-clear-fail",
            attempt_id="attempt-clear-fail",
            binding=binding,
            user_message="clear callback",
        )
        events = []
        async for event in adapter.execute(request):
            events.append(event)
            if event.type == "runtime.decision.required":
                result = await adapter.resolve_decision(
                    DecisionResolution(
                        run_id=request.run_id,
                        attempt_id=request.attempt_id,
                        binding=binding,
                        decision_id=str(event.payload["decision_id"]),
                        choice="deny",
                        revision=0,
                    )
                )
                assert result.accepted is True

        assert events[-1].type == "runtime.run.failed"
        assert backend._executions == {}
        assert backend._pending == {}
        assert adapter._active_executions == {}
        assert approval_slot.active_callbacks == 0

    asyncio.run(scenario())


def test_native_run_exception_clears_callback_pending_and_execution(
    monkeypatch,
) -> None:
    class RunFailAgent(FakeNativeAgent):
        def run_conversation(self, *args, **kwargs):
            raise RuntimeError("native run failed")

    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=RunFailAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        events = [
            event
            async for event in adapter.execute(
                AttemptRequest(
                    run_id="run-native-fail",
                    attempt_id="attempt-native-fail",
                    binding=binding,
                    user_message="fail",
                )
            )
        ]

        assert events[-1].type == "runtime.run.failed"
        assert backend._executions == {}
        assert backend._pending == {}
        assert adapter._active_executions == {}
        assert approval_slot.active_callbacks == 0

    asyncio.run(scenario())


def test_private_reasoning_and_raw_errors_never_enter_runtime_events(
    monkeypatch,
) -> None:
    secret = "SECRET-private-reasoning-provider-path"

    class PrivateSignalAgent(FakeNativeAgent):
        def run_conversation(self, *args, **kwargs):
            self.tool_progress_callback("_thinking", secret)
            self.tool_progress_callback(
                "reasoning.available", "_thinking", secret, {"raw": secret}
            )
            self.tool_progress_callback("delegate.task_thinking", secret)
            self.tool_progress_callback(
                "subagent.thinking", None, secret, {"raw": secret}
            )
            self.tool_progress_callback(f"unknown-{secret}")
            self.tool_progress_callback(
                "tool.completed",
                "private_tool",
                None,
                None,
                duration=0.125,
                is_error=True,
                result=f'{{"error":"{secret}"}}',
            )
            self.tool_complete_callback(
                "tool-private",
                "private_tool",
                {"raw": secret},
                f'{{"error":"{secret}"}}',
            )
            raise RuntimeError(secret)

    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=PrivateSignalAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        events = [
            event
            async for event in adapter.execute(
                AttemptRequest(
                    run_id="run-private-signals",
                    attempt_id="attempt-private-signals",
                    binding=binding,
                    user_message="private signals",
                )
            )
        ]

        assert events[-1].type == "runtime.run.failed"
        assert all(secret not in repr(event) for event in events)
        assert all(
            event.payload in ({"phase": "reasoning.available"}, {})
            for event in events
            if event.type == "runtime.status"
        )
        completed = next(
            event for event in events if event.type == "runtime.tool.completed"
        )
        assert completed.payload == {
            "tool_call_id": "tool-private",
            "tool_name": "private_tool",
            "is_error": True,
            "duration_ms": 125,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result_payload",
    [
        {
            "completed": False,
            "failed": True,
            "error": "SECRET-provider-exhausted",
            "final_response": "SECRET-partial-response",
        },
        {
            "partial": True,
            "error": "SECRET-partial-error",
            "final_response": "SECRET-partial-response",
        },
        {
            "completed": False,
            "error": "SECRET-incomplete-error",
            "final_response": "SECRET-partial-response",
        },
    ],
)
def test_native_failed_partial_and_incomplete_mappings_are_sanitized(
    monkeypatch, result_payload
) -> None:
    class MappingResultAgent(FakeNativeAgent):
        def run_conversation(self, *args, **kwargs):
            self.session_id = f"{self.session_id}-rebound"
            return dict(result_payload)

    async def scenario() -> None:
        approval_slot = ApprovalSlot()
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=MappingResultAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        events = [
            event
            async for event in adapter.execute(
                AttemptRequest(
                    run_id="run-mapping-failure",
                    attempt_id="attempt-mapping-failure",
                    binding=binding,
                    user_message="mapping failure",
                )
            )
        ]

        assert [event.type for event in events] == [
            "runtime.run.started",
            "runtime.session_rebound",
            "runtime.run.failed",
        ]
        assert events[-1].payload == {
            "category": "runtime_execution_failed",
            "retryable": True,
            # ⟦P5.4d⟧ The backend said the turn failed and said nothing else,
            # which is a different fact from an exception on this side. The
            # sanitisation this test is about is unchanged: the word is a
            # constant, and the result mapping's own text never appears.
            "detail": WORKER_REPORTED_FAILURE,
        }
        assert all("SECRET" not in repr(event) for event in events)

    asyncio.run(scenario())


def test_native_mapping_success_defaults_and_cancel_precedence(monkeypatch) -> None:
    async def run_mapping(result_payload, *, suffix):
        class MappingResultAgent(FakeNativeAgent):
            def run_conversation(self, *args, **kwargs):
                return dict(result_payload)

        approval_slot = ApprovalSlot()
        backend = _NativeHermesBackend(
            hermes_state=SimpleNamespace(
                SCHEMA_VERSION=13,
                SessionDB=MemorySessionDB,
            ),
            agent_class=MappingResultAgent,
            set_approval_callback=approval_slot.set,
            session_db_path=None,
            agent_options={"approval_slot": approval_slot},
        )
        adapter = HermesAdapter(backend_loader=lambda: backend)
        binding = await adapter.open_session(SessionOpenRequest())
        return [
            event
            async for event in adapter.execute(
                AttemptRequest(
                    run_id=f"run-mapping-{suffix}",
                    attempt_id=f"attempt-mapping-{suffix}",
                    binding=binding,
                    user_message="mapping result",
                )
            )
        ]

    async def scenario() -> None:
        monkeypatch.setattr(
            "cortex_platform.runtime.hermes.metadata.version",
            lambda distribution: "0.15.0",
        )
        success = await run_mapping(
            {"final_response": "visible success"}, suffix="success"
        )
        canceled = await run_mapping(
            {
                "completed": False,
                "failed": True,
                "partial": True,
                "interrupted": True,
                "error": "SECRET-canceled-error",
                "final_response": "SECRET-canceled-partial",
            },
            suffix="canceled",
        )

        assert [event.type for event in success] == [
            "runtime.run.started",
            "runtime.message.completed",
            "runtime.run.completed",
        ]
        assert [event.type for event in canceled] == [
            "runtime.run.started",
            "runtime.run.canceled",
        ]
        assert all("SECRET" not in repr(event) for event in canceled)

    asyncio.run(scenario())


def test_native_decision_resolution_requires_run_and_attempt_identity(
    monkeypatch,
) -> None:
    approval_slot = ApprovalSlot()
    monkeypatch.setattr(
        "cortex_platform.runtime.hermes.metadata.version",
        lambda distribution: "0.15.0",
    )
    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(
            SCHEMA_VERSION=13,
            SessionDB=MemorySessionDB,
        ),
        agent_class=FakeNativeAgent,
        set_approval_callback=approval_slot.set,
        session_db_path=None,
        agent_options={"approval_slot": approval_slot},
    )
    pending = _PendingDecision("run-identity", "attempt-identity")
    backend._pending[("session-identity", "decision-identity")] = pending

    assert (
        backend.resolve_decision(
            "session-identity",
            "wrong-run",
            "attempt-identity",
            "decision-identity",
            "deny",
        ).status
        == ActionOutcomeStatus.REJECTED
    )
    assert pending.choice is None
    assert (
        backend.resolve_decision(
            "session-identity",
            "run-identity",
            "wrong-attempt",
            "decision-identity",
            "deny",
        ).status
        == ActionOutcomeStatus.REJECTED
    )
    assert pending.choice is None
    assert (
        backend.resolve_decision(
            "session-identity",
            "run-identity",
            "attempt-identity",
            "decision-identity",
            "deny",
        ).status
        == ActionOutcomeStatus.ACCEPTED
    )
    assert pending.choice == "deny"


def test_a_failure_detail_carries_no_path_and_no_separator_bearing_secret() -> None:
    """⟦P54D-3⟧ The 20-character opaque-run heuristic let two classes through.

    `failure_detail` reaches the durable run event AND
    `turn_bridge.last_failure`, which is read off `/api/v1/health` -- answered
    before authentication, so anything there is world-readable on loopback.
    """

    from cortex_platform.runtime.models import failure_detail

    missing = FileNotFoundError(
        2,
        "No such file or directory: "
        "/Users/operator/Library/Application_Support/Cortex/state.db",
    )
    detail = failure_detail(missing)
    assert "/Users" not in detail
    assert "Cortex" not in detail
    assert detail.startswith("FileNotFoundError")

    keyed = RuntimeError("provider rejected key sk-proj-YWJjZGVmZ2hpams+/=")
    scrubbed = failure_detail(keyed)
    assert "YWJjZGVmZ2hpams" not in scrubbed
    assert "sk-proj" not in scrubbed
    assert scrubbed.startswith("RuntimeError")

    referenced = RuntimeError("keychain://cortex-provider/anthropic is empty")
    assert "cortex-provider" not in failure_detail(referenced)

    # A detail with nothing secret-shaped in it still says what happened.
    assert (
        failure_detail(TimeoutError("the provider did not answer in 30s"))
        == "TimeoutError: the provider did not answer in 30s"
    )
