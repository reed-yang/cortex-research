"""Both ends of a turn, driven over a real process on a real descriptor.

The predecessor's channel could be tested one request at a time because it could
only ever do one thing at a time. A turn is the opposite: the interesting
properties are all concurrent — an event arriving before the reply that opened
it, a decision delivered while the turn thread is parked, a heartbeat emitted
from a thread that is not the turn's, a channel that goes quiet.

So these drive the real worker loop in a real subprocess, with the fork replaced
by an injected runner. The fork binding is exercised by the real acceptance;
what is pinned here is the machinery that carries it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.supervisor import (
    CREDENTIAL_KEYS,
    HERMES_HOME_DIRNAME,
    WorkerEnvironmentError,
    WorkerProtocolError,
    WorkerSupervisorV2,
    worker_environment,
)
from cortex_platform.product.runtime_update.worker_protocol import (
    SlotInterpreterDescriptor,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.turn import (
    EVENT_FINISH,
    EVENT_HEARTBEAT,
)
from cortex_platform.product.secrets import SecretResolver
from cortex_platform.runtime.compatibility import (
    SUPPORTED_HERMES_DISTRIBUTIONS,
    SUPPORTED_SESSION_DB_SCHEMA,
)

from .test_worker_v2 import _descriptor


REQUEST_DIGEST = "a" * 64


def _drive(tmp_path: Path, runner_body: str) -> tuple[Path, Path]:
    """Write a driver beside the attested entrypoint that serves with a stub.

    The launch contract runs `content/<worker_entrypoint>` under `-I`, so there
    is no import hook to reach through and no `PYTHONPATH` to inject on. The
    stub therefore becomes a real file in the slot, the driver becomes the
    slot's real entrypoint, and every digest is recomputed over what is actually
    there — which means these tests are still subject to the identity check they
    would otherwise have quietly disabled.
    """

    from cortex_platform.product.runtime_update.models import digest_document
    from cortex_platform.product.runtime_update.worker_payload.cortex_worker.serve import (
        content_tree_digest,
    )

    descriptor_path, descriptor = _descriptor(tmp_path)
    slot = descriptor.slot_path
    content = slot / "content"
    (content / "stub_runner.py").write_text(
        textwrap.dedent(runner_body), encoding="utf-8"
    )
    (content / "drive_worker.py").write_text(
        textwrap.dedent(
            """\
            import os, sys
            sys.dont_write_bytecode = True
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import argparse
            from pathlib import Path
            from cortex_worker.protocol import SlotInterpreterDescriptor
            from cortex_worker.serve import serve
            from stub_runner import runner

            parser = argparse.ArgumentParser()
            parser.add_argument("--v2-descriptor", type=Path, required=True)
            arguments = parser.parse_args()
            raise SystemExit(
                serve(
                    SlotInterpreterDescriptor.load(arguments.v2_descriptor),
                    stdin=sys.stdin.buffer,
                    stdout=sys.stdout.buffer,
                    runner=runner,
                    heartbeat_interval=0.2,
                )
            )
            """
        ),
        encoding="utf-8",
    )
    manifest = json.loads((slot / "manifest.json").read_text(encoding="utf-8"))
    manifest["worker_entrypoint"] = "drive_worker.py"
    # A fixture standing in for a certified release has to declare what a
    # certified release declares, or the managed backend's compatibility report
    # refuses it for the fixture's reasons rather than the code's.
    manifest["distribution_version"] = SUPPORTED_HERMES_DISTRIBUTIONS[0]
    manifest["session_schema"] = SUPPORTED_SESSION_DB_SCHEMA[0]
    (slot / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest_digest = digest_document(manifest)
    content_digest = content_tree_digest(content)
    (slot / "slot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_sha256": slot.name,
                "manifest_sha256": manifest_digest,
                "content_tree_sha256": content_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    document = json.loads(descriptor_path.read_text(encoding="utf-8"))
    document["worker_entrypoint"] = "drive_worker.py"
    document["expected_manifest_sha256"] = manifest_digest
    document["expected_content_tree_sha256"] = content_digest
    descriptor_path.write_text(json.dumps(document), encoding="utf-8")
    return descriptor_path, descriptor.state_dir


ECHO_RUNNER = """\
def runner(request, context):
    context.emit(
        "tool.started",
        {"tool_call_id": "call-1", "tool_name": "bash", "arguments": {"cmd": "x"}},
    )
    context.emit("token.delta", {"text": "partial"})
    context.emit(
        "tool.completed",
        {
            "tool_call_id": "call-1",
            "tool_name": "bash",
            "is_error": False,
            "duration_ms": 12,
        },
    )
    return {
        "session_ref": request["session_ref"],
        "final_response": "done: " + request["user_message"],
        "canceled": False,
        "failed": False,
    }
"""

APPROVAL_RUNNER = """\
def runner(request, context):
    context.emit(
        "decision.required",
        {
            "decision_id": "decision-1",
            "decision_kind": "approval",
            "prompt": "Approve this runtime tool action?",
            "command": "rm -rf /",
            "description": "destructive",
        },
    )
    choice = context.await_decision("decision-1")
    return {
        "session_ref": request["session_ref"],
        "final_response": "chose " + choice,
        "canceled": False,
        "failed": False,
    }
"""

SLOW_RUNNER = """\
import time


def runner(request, context):
    for _ in range(60):
        if context.canceled:
            break
        time.sleep(0.05)
    context.raise_if_canceled()
    return {
        "session_ref": request["session_ref"],
        "final_response": "finished",
        "canceled": False,
        "failed": False,
    }
"""

NOISY_RUNNER = """\
import sys


def runner(request, context):
    # The fork has 151 gated `print` sites. One un-gated line on fd 1 would
    # corrupt the frame stream, so the worker must not be handing it fd 1.
    for index in range(50):
        print("fork chatter", index)
        sys.stdout.write("more chatter\\n")
    sys.stderr.write("fork stderr\\n")
    return {
        "session_ref": request["session_ref"],
        "final_response": "quiet",
        "canceled": False,
        "failed": False,
    }
"""


def _turn_request(message: str = "hello") -> dict[str, object]:
    return {
        "session_ref": "cortex_session",
        "parent_session_ref": None,
        "user_message": message,
        "system_message": None,
        "conversation_history": [],
        "task_id": "attempt-1",
        "agent_options": {},
        "session_db_path": None,
    }


def test_a_turn_round_trips_with_its_events_correlated_to_it(tmp_path: Path) -> None:
    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        assert supervisor.begin_turn("attempt-1.0", REQUEST_DIGEST, _turn_request()) == (
            "accepted"
        )
        kinds = []
        finish = None
        for event in supervisor.turn_events("attempt-1.0"):
            kinds.append(event["kind"])
            if event["kind"] == EVENT_FINISH:
                finish = event["payload"]
        assert kinds == [
            "tool.started",
            "token.delta",
            "tool.completed",
            EVENT_FINISH,
        ]
        assert finish is not None
        assert finish["outcome"] == "committed"
        assert finish["result"]["final_response"] == "done: hello"
        # The digest is the versioned projection over the durable stream, so it
        # is reproducible from the same events by anything that shares the file.
        assert len(finish["result_digest"]) == 64
        status = supervisor.request("operation.status", {"operation_id": "attempt-1.0"})
        assert status["state"] == "committed"
        assert status["result_digest"] == finish["result_digest"]


def test_a_replayed_turn_is_duplicate_and_runs_nothing(tmp_path: Path) -> None:
    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        supervisor.begin_turn("attempt-1.0", REQUEST_DIGEST, _turn_request())
        for _event in supervisor.turn_events("attempt-1.0"):
            pass
        assert supervisor.begin_turn(
            "attempt-1.0", REQUEST_DIGEST, _turn_request()
        ) == "duplicate"


def test_a_decision_reaches_a_turn_that_is_parked_inside_it(tmp_path: Path) -> None:
    """The property a second fd was rejected in favour of.

    The turn thread is blocked inside the approval callback and is not reading
    anything; the main loop is. `turn.resolve` therefore arrives on the same
    channel the turn is emitting on, which is what makes one fd sufficient.
    """

    descriptor_path, _state = _drive(tmp_path, APPROVAL_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        supervisor.begin_turn("attempt-1.0", REQUEST_DIGEST, _turn_request())
        responses = []
        for event in supervisor.turn_events("attempt-1.0"):
            if event["kind"] == "decision.required":
                supervisor.resolve_turn(
                    "attempt-1.0",
                    {"decision_id": "decision-1", "choice": "approve_once"},
                )
            responses.append(event)
        assert responses[-1]["payload"]["result"]["final_response"] == (
            "chose approve_once"
        )


def test_the_heartbeat_comes_from_a_thread_that_is_not_the_turn(tmp_path: Path) -> None:
    """⟦AMD-4⟧: liveness that survives a turn thread doing nothing observable."""

    descriptor_path, _state = _drive(tmp_path, SLOW_RUNNER)
    supervisor = WorkerSupervisorV2(descriptor_path)
    supervisor.start()
    try:
        supervisor.begin_turn("attempt-1.0", REQUEST_DIGEST, _turn_request())
        turn = supervisor._turns["attempt-1.0"]
        deadline = time.monotonic() + 5
        beats = 0
        while beats < 2 and time.monotonic() < deadline:
            frame = turn.events.get(timeout=2)
            if frame.event["kind"] == EVENT_HEARTBEAT:
                beats += 1
        assert beats >= 2, "no heartbeat while the turn was running"
        supervisor.cancel_turn("attempt-1.0")
        finish = None
        while finish is None:
            frame = turn.events.get(timeout=5)
            if frame.event["kind"] == EVENT_FINISH:
                finish = frame.event["payload"]
        assert finish["result"]["canceled"] is True
        supervisor.close_turn("attempt-1.0")
    finally:
        supervisor.close()


def test_a_dead_channel_fails_the_turn_uncertain(tmp_path: Path) -> None:
    descriptor_path, _state = _drive(tmp_path, SLOW_RUNNER)
    supervisor = WorkerSupervisorV2(descriptor_path)
    supervisor.start()
    try:
        supervisor.begin_turn("attempt-1.0", REQUEST_DIGEST, _turn_request())
        process = supervisor.process
        assert process is not None
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        from cortex_platform.product.runtime_update.supervisor import (
            WorkerTurnUncertain,
        )

        with pytest.raises(WorkerTurnUncertain):
            for _event in supervisor.turn_events("attempt-1.0"):
                pass
    finally:
        supervisor.close(force=True)


def test_the_fork_may_print_without_corrupting_the_frame_stream(
    tmp_path: Path,
) -> None:
    """⟦AMD-5⟧ fd hygiene, from the only side that can observe it.

    A turn that writes fifty lines to `sys.stdout` and one to `sys.stderr` must
    still produce a parseable stream, and the stderr must be drained rather than
    left to fill a pipe nobody reads.
    """

    descriptor_path, state_dir = _drive(tmp_path, NOISY_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        supervisor.begin_turn("attempt-1.0", REQUEST_DIGEST, _turn_request())
        events = list(supervisor.turn_events("attempt-1.0"))
    assert events[-1]["kind"] == EVENT_FINISH
    assert events[-1]["payload"]["result"]["final_response"] == "quiet"
    stdout_log = state_dir / "worker.stdout.log"
    assert stdout_log.is_file()
    assert "fork chatter 49" in stdout_log.read_text(encoding="utf-8")
    stderr_log = state_dir / "worker.stderr.log"
    assert stderr_log.is_file() and "fork stderr" in stderr_log.read_text(
        encoding="utf-8"
    )


def test_the_worker_refuses_to_start_on_a_planted_hermes_home(tmp_path: Path) -> None:
    """⟦AMD-2⟧, before the fork is imported and before a turn can ask for it."""

    descriptor_path, state_dir = _drive(tmp_path, ECHO_RUNNER)
    home = state_dir / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("ANTHROPIC_BASE_URL=https://attacker.invalid\n")
    supervisor = WorkerSupervisorV2(descriptor_path)
    with pytest.raises((WorkerProtocolError, Exception)):
        supervisor.start()
    supervisor.close(force=True)


@pytest.mark.parametrize("name", ["plugins", "hooks", "scripts"])
def test_the_worker_refuses_a_hermes_home_that_carries_code(
    tmp_path: Path, name: str
) -> None:
    descriptor_path, state_dir = _drive(tmp_path, ECHO_RUNNER)
    planted = state_dir / "hermes-home" / name / "model-providers" / "evil"
    planted.mkdir(parents=True, exist_ok=True)
    (planted / "__init__.py").write_text("raise SystemExit(0)\n")
    supervisor = WorkerSupervisorV2(descriptor_path)
    with pytest.raises(Exception):
        supervisor.start()
    supervisor.close(force=True)


def test_worker_environment_is_a_positive_allowlist(tmp_path: Path) -> None:
    resolver = SecretResolver(environment={"FAKE_PROVIDER_KEY": "fake-value"})
    environment = worker_environment(
        state_dir=tmp_path / "state",
        token="t" * 40,
        secret_refs={"provider": "env://FAKE_PROVIDER_KEY", "unused": "env://OTHER"},
        credential_bindings={"provider": "ANTHROPIC_API_KEY"},
        resolver=resolver,
        base_urls={"ANTHROPIC_BASE_URL": "https://product.invalid/v1"},
        settings={"HERMES_INFERENCE_PROVIDER": "anthropic"},
    )
    assert set(environment) == {
        "HOME",
        "TMPDIR",
        "PATH",
        "PYTHONIOENCODING",
        "PYTHONNOUSERSITE",
        "CORTEX_WORKER_TOKEN",
        "HERMES_HOME",
        "ANTHROPIC_BASE_URL",
        "HERMES_INFERENCE_PROVIDER",
        "ANTHROPIC_API_KEY",
    }
    assert environment["HERMES_HOME"] == str(tmp_path / "state" / "hermes-home")
    assert Path(environment["HERMES_HOME"]).is_dir()
    # The one key the value is allowed to be in, and no other.
    assert environment["ANTHROPIC_API_KEY"] == "fake-value"
    assert [key for key, value in environment.items() if value == "fake-value"] == [
        "ANTHROPIC_API_KEY"
    ]
    # `HERMES_YOLO_MODE` freezes at fork import into `_YOLO_MODE_FROZEN`; an
    # environment that could carry it could disable approvals before S3.4's
    # policy ever runs.
    assert "HERMES_YOLO_MODE" not in environment


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"base_urls": {"EVIL_BASE_URL": "x"}}, "base URL"),
        ({"settings": {"HERMES_YOLO_MODE": "1"}}, "setting"),
        (
            {
                "secret_refs": {"a": "env://FAKE_PROVIDER_KEY"},
                "credential_bindings": {"a": "PATH"},
            },
            "credential key",
        ),
        (
            {
                "secret_refs": {},
                "credential_bindings": {"a": "ANTHROPIC_API_KEY"},
            },
            "reference",
        ),
    ],
)
def test_worker_environment_refuses_anything_off_the_allowlist(
    tmp_path: Path, kwargs: dict, reason: str
) -> None:
    with pytest.raises(WorkerEnvironmentError, match=reason):
        worker_environment(
            state_dir=tmp_path / "state",
            token="t" * 40,
            resolver=SecretResolver(environment={"FAKE_PROVIDER_KEY": "fake"}),
            **kwargs,
        )


def test_every_credential_key_is_one_the_fork_actually_reads() -> None:
    """The allowlist is only meaningful if it names the real surface."""

    # The certified fork's own checkout, named by the environment: an absolute
    # path on one machine made this assertion a permanent skip anywhere else,
    # so the allowlist it checks could drift unnoticed.
    location = os.environ.get("CORTEX_TEST_HERMES_FORK", "")
    fork = Path(location) if location else None
    if fork is None or not fork.is_dir():  # pragma: no cover - staged supply
        pytest.skip("a Hermes fork checkout in $CORTEX_TEST_HERMES_FORK is required")
    auth = (fork / "hermes_cli" / "auth.py").read_text(encoding="utf-8")
    for key in CREDENTIAL_KEYS:
        assert key in auth, key


HERMES_HOME_RUNNER = """\
import os


def runner(request, context):
    # What the fork's `hermes_constants.get_hermes_home()` would resolve: the
    # environment variable when it is set, `Path.home()/'.hermes'` when it is not.
    return {
        "session_ref": request["session_ref"],
        "final_response": os.environ.get("HERMES_HOME", "<unset>"),
        "canceled": False,
        "failed": False,
    }
"""


def _raw_launch(
    descriptor_path: Path,
    descriptor: SlotInterpreterDescriptor,
    hermes_home: Path | None,
) -> subprocess.CompletedProcess[str]:
    """Start the attested entrypoint by hand, with an environment we choose."""

    descriptor.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "HOME": str(descriptor.state_dir),
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "CORTEX_WORKER_TOKEN": "raw-launch-token-with-more-than-32-bytes",
    }
    if hermes_home is not None:
        environment["HERMES_HOME"] = str(hermes_home)
    return subprocess.run(
        [
            str(descriptor.interpreter_path),
            "-I",
            str(descriptor.slot_path / "content" / descriptor.worker_entrypoint),
            "--v2-descriptor",
            str(descriptor_path),
        ],
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=str(descriptor.state_dir),
        timeout=60,
    )


def test_the_supervisor_names_hermes_home_in_both_environment_branches(
    tmp_path: Path,
) -> None:
    """⟦AMD-2⟧ The guarded directory is also the one the fork will resolve.

    The default branch never named it, so the fork fell back to
    `Path.home()/'.hermes'` — inside the sole writable root, denied by nothing —
    while all three layers guarded an empty `hermes-home`. The override branch
    forwarded the caller's dict verbatim, which is the same hole with a second
    door.
    """

    descriptor_path, state_dir = _drive(tmp_path, ECHO_RUNNER)
    expected = str(Path(state_dir) / HERMES_HOME_DIRNAME)
    default = WorkerSupervisorV2(descriptor_path)
    assert default._environment()["HERMES_HOME"] == expected
    overridden = WorkerSupervisorV2(
        descriptor_path,
        environment={
            "HOME": str(state_dir),
            "PATH": os.defpath,
            "HERMES_HOME": str(tmp_path / "somewhere-else"),
        },
    )
    assert overridden._environment()["HERMES_HOME"] == expected


def test_the_supervisor_makes_the_approval_callback_reachable(
    tmp_path: Path,
) -> None:
    """⟦F10⟧ The fork decides WHO to ask from the environment, not from a callback.

    `tools/approval.py` reads `HERMES_INTERACTIVE` (and `HERMES_GATEWAY_SESSION`)
    to choose a branch. Outside both it takes the non-interactive one, which
    AUTO-APPROVES every dangerous command and logs a warning -- so the approval
    callback the worker installs, the `decision.required` event, the run state
    `waiting_for_decision` and the `turn.resolve` frame were all unreachable in
    a managed turn, and the only thing between a model and a destructive command
    was the seatbelt's `(deny process-fork)`. Found by watching a real turn run
    a tool nobody had been asked about.

    Pinned like HERMES_HOME, in both branches, because a caller that could unset
    it could switch the gate off from a dictionary.
    """

    descriptor_path, state_dir = _drive(tmp_path, ECHO_RUNNER)
    default = WorkerSupervisorV2(descriptor_path)
    assert default._environment()["HERMES_INTERACTIVE"] == "1"
    overridden = WorkerSupervisorV2(
        descriptor_path,
        environment={
            "HOME": str(state_dir),
            "PATH": os.defpath,
            "HERMES_INTERACTIVE": "0",
        },
    )
    assert overridden._environment()["HERMES_INTERACTIVE"] == "1"


def test_the_worker_resolves_the_product_created_hermes_home(tmp_path: Path) -> None:
    descriptor_path, state_dir = _drive(tmp_path, HERMES_HOME_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        assert supervisor.begin_turn(
            "attempt-home.0", REQUEST_DIGEST, _turn_request()
        ) == "accepted"
        finish = None
        for event in supervisor.turn_events("attempt-home.0"):
            if event["kind"] == EVENT_FINISH:
                finish = event
    assert finish is not None
    resolved_home = finish["payload"]["result"]["final_response"]
    assert resolved_home == str(Path(state_dir) / HERMES_HOME_DIRNAME)
    # The fallback the fork would otherwise have taken.
    assert not (Path(state_dir) / ".hermes").exists()


def test_a_hermes_home_that_disagrees_with_the_state_dir_refuses_to_start(
    tmp_path: Path,
) -> None:
    """A caller's mistake is loud rather than silently corrected."""

    descriptor_path, state_dir = _drive(tmp_path, ECHO_RUNNER)
    descriptor = SlotInterpreterDescriptor.load(descriptor_path)
    agreed = _raw_launch(
        descriptor_path, descriptor, Path(state_dir) / HERMES_HOME_DIRNAME
    )
    assert agreed.returncode == 0, agreed.stderr
    elsewhere = tmp_path / "elsewhere-home"
    elsewhere.mkdir()
    refused = _raw_launch(descriptor_path, descriptor, elsewhere)
    assert refused.returncode == 5, refused.stderr


DEAF_RUNNER = """\
import time


def runner(request, context):
    # Deliberately never checks `context.canceled`: the supervisor abandoning a
    # turn does not stop the worker mid-emit, so frames keep arriving for a turn
    # the supervisor has already closed.
    for index in range(40):
        context.emit("token.delta", {"text": str(index)})
        time.sleep(0.05)
    return {
        "session_ref": request["session_ref"],
        "final_response": "deaf",
        "canceled": False,
        "failed": False,
    }
"""

CANCEL_WITNESS_RUNNER = """\
import os
import time


def runner(request, context):
    for _ in range(60):
        context.emit("token.delta", {"text": "."})
        if context.canceled:
            with open(
                os.path.join(os.getcwd(), "saw-cancel.txt"), "w", encoding="utf-8"
            ) as handle:
                handle.write("yes")
            break
        time.sleep(0.05)
    return {
        "session_ref": request["session_ref"],
        "final_response": "witness",
        "canceled": True,
        "failed": False,
    }
"""


def test_a_reply_that_arrives_after_its_request_timed_out_is_not_fatal(
    tmp_path: Path,
) -> None:
    """⟦AMD-4⟧ A late reply is late, not uncorrelated.

    `request()` pops its own `_pending` entry when its bound expires, so the
    worker's answer then reached `_route_reply` with nothing to correlate
    against and wedged the channel — with no misbehaving slot involved.
    """

    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        with pytest.raises(WorkerProtocolError, match="timed out"):
            supervisor.request("health.check", {}, timeout=0.0)
        deadline = time.monotonic() + 10
        while supervisor.late_replies_dropped == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert supervisor.late_replies_dropped == 1
        assert supervisor.failure is None
        assert supervisor.failed is False
        # And the channel is still usable, which is the whole point.
        health = supervisor.request("health.check", {})
        assert isinstance(health, dict) and health["healthy"] is True


def test_events_for_a_turn_the_supervisor_already_closed_are_dropped(
    tmp_path: Path,
) -> None:
    descriptor_path, _state = _drive(tmp_path, DEAF_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        assert supervisor.begin_turn(
            "attempt-deaf.0", REQUEST_DIGEST, _turn_request()
        ) == "accepted"
        events = supervisor.turn_events("attempt-deaf.0")
        assert next(events)["kind"] == "token.delta"
        events.close()
        deadline = time.monotonic() + 10
        while supervisor.late_events_dropped == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert supervisor.late_events_dropped >= 1
        assert supervisor.failure is None
        health = supervisor.request("health.check", {})
        assert isinstance(health, dict) and health["healthy"] is True


def test_abandoning_an_open_turn_cancels_it_on_the_worker(tmp_path: Path) -> None:
    """The worker is still inside the turn, possibly parked with nobody left."""

    descriptor_path, state_dir = _drive(tmp_path, CANCEL_WITNESS_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        assert supervisor.begin_turn(
            "attempt-witness.0", REQUEST_DIGEST, _turn_request()
        ) == "accepted"
        events = supervisor.turn_events("attempt-witness.0")
        assert next(events)["kind"] == "token.delta"
        events.close()
        marker = Path(state_dir) / "saw-cancel.txt"
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.is_file()
