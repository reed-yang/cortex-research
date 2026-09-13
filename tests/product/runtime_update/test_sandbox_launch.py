"""The launch wrapper: the worker actually runs inside the profile.

The generator and the prober are pinned next door. What is pinned here is that
the profile reaches the process — and, in one case, that it catches something
the in-process hook structurally cannot.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.sandbox import (
    EVIDENCE_DIRNAME,
    SANDBOX_EXEC,
    prepare_sandbox,
)
from cortex_platform.product.runtime_update.supervisor import (
    HERMES_HOME_DIRNAME,
    WorkerSupervisorV2,
)
from cortex_platform.product.runtime_update.worker_protocol import (
    SlotInterpreterDescriptor,
)
from cortex_platform.runtime.managed_hermes import ManagedHermesBackend

from .test_worker_turns import ECHO_RUNNER, _drive, _turn_request

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or not os.path.isfile(SANDBOX_EXEC),
    reason="the managed worker sandbox is a Darwin sandbox-exec profile",
)

REQUEST_DIGEST = "d" * 64


def test_the_launch_argv_applies_the_probed_profile(tmp_path: Path) -> None:
    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    descriptor = SlotInterpreterDescriptor.load(descriptor_path)
    launch = prepare_sandbox(descriptor, descriptor_path=descriptor_path)
    supervisor = WorkerSupervisorV2(descriptor_path, sandbox=launch)
    try:
        supervisor.start()
        process = supervisor.process
        assert process is not None
        assert list(process.args[:3]) == [
            SANDBOX_EXEC,
            "-f",
            str(launch.profile_path),
        ]
        assert str(descriptor.interpreter_path) == process.args[3]
    finally:
        supervisor.close()


def test_a_turn_runs_to_completion_inside_the_profile(tmp_path: Path) -> None:
    descriptor_path, _state = _drive(tmp_path, ECHO_RUNNER)
    descriptor = SlotInterpreterDescriptor.load(descriptor_path)
    launch = prepare_sandbox(descriptor, descriptor_path=descriptor_path)
    with WorkerSupervisorV2(descriptor_path, sandbox=launch) as supervisor:
        assert supervisor.begin_turn(
            "attempt-sb.0", REQUEST_DIGEST, _turn_request()
        ) == "accepted"
        finish = None
        for event in supervisor.turn_events("attempt-sb.0"):
            if event["kind"] == "operation.finish":
                finish = event
    assert finish is not None
    assert finish["payload"]["result"]["final_response"] == "done: hello"
    assert finish["payload"]["outcome"] == "committed"


RELATIVE_ESCAPE_RUNNER = """\
import os


def runner(request, context):
    # A *relative* escape. The in-process hook cannot judge it — the `open`
    # audit event never carries the `dir_fd` — so if this write is refused, the
    # refusal came from the kernel, which is the whole point of layer 1.
    results = {}
    for name, target in (
        ("relative_escape", os.path.join("..", "..", "relative-escape.txt")),
        ("absolute_escape", os.path.join(os.path.dirname(os.getcwd()), "abs.txt")),
    ):
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x")
            results[name] = "written"
        except BaseException as exc:
            results[name] = type(exc).__name__
    return {
        "session_ref": request["session_ref"],
        "final_response": "escape",
        "canceled": False,
        "failed": False,
        "escape": results,
    }
"""


def test_layer_one_catches_the_relative_escape_layer_two_cannot(
    tmp_path: Path,
) -> None:
    """The two layers are not redundant, and this is where that shows.

    Run with no seatbelt, the relative write succeeds: the audit hook declines
    to judge a relative path because `openat` and `open` are indistinguishable
    to it. Run inside the profile, the same write is refused by the kernel.
    """

    without_path, without_state = _drive(tmp_path / "plain", RELATIVE_ESCAPE_RUNNER)
    with WorkerSupervisorV2(without_path) as supervisor:
        supervisor.begin_turn("attempt-esc.0", REQUEST_DIGEST, _turn_request())
        unguarded = None
        for event in supervisor.turn_events("attempt-esc.0"):
            if event["kind"] == "operation.finish":
                unguarded = event["payload"]["result"]["escape"]
    assert unguarded == {
        "relative_escape": "written",
        "absolute_escape": "WriteConfinementError",
    }

    with_path, _state = _drive(tmp_path / "sealed", RELATIVE_ESCAPE_RUNNER)
    descriptor = SlotInterpreterDescriptor.load(with_path)
    launch = prepare_sandbox(descriptor, descriptor_path=with_path)
    with WorkerSupervisorV2(with_path, sandbox=launch) as supervisor:
        supervisor.begin_turn("attempt-esc.1", REQUEST_DIGEST, _turn_request())
        guarded = None
        for event in supervisor.turn_events("attempt-esc.1"):
            if event["kind"] == "operation.finish":
                guarded = event["payload"]["result"]["escape"]
    assert guarded == {
        "relative_escape": "PermissionError",
        "absolute_escape": "WriteConfinementError",
    }


def test_the_managed_backend_sandboxes_every_worker_it_launches(
    tmp_path: Path,
) -> None:
    descriptor_path, state = _drive(tmp_path, ECHO_RUNNER)
    backend = ManagedHermesBackend(descriptor_path)
    try:
        capabilities = backend.capabilities()
        assert capabilities.available is True
        launch = backend.sandbox_launch
        assert launch is not None
        assert launch.profile_path.is_file()
        assert dict(launch.probes)["write_hermes_home_env"].startswith("denied:")
        supervisor = backend._supervisor
        assert supervisor is not None and supervisor.sandbox is not None
        process = supervisor.process
        assert process is not None and process.args[0] == SANDBOX_EXEC
        # The dedup probe runs on its own namespace, so it gets its own profile
        # rather than one that denies the production worker's four paths.
        probe_evidence = Path(state) / "probe" / EVIDENCE_DIRNAME / "profile.sb"
        assert probe_evidence.is_file()
    finally:
        backend.close()


def test_sandbox_false_is_available_only_to_callers_testing_the_channel(
    tmp_path: Path,
) -> None:
    descriptor_path, state = _drive(tmp_path, ECHO_RUNNER)
    backend = ManagedHermesBackend(descriptor_path, sandbox=False)
    try:
        assert backend.capabilities().available is True
        assert backend.sandbox_launch is None
        supervisor = backend._supervisor
        assert supervisor is not None and supervisor.sandbox is None
    finally:
        backend.close()


HERMES_HOME_PROBE_RUNNER = """\
import os


def runner(request, context):
    # The home the fork would actually resolve, and what happens when the two
    # things the fork loads from it are seeded from inside the worker.
    home = os.environ.get("HERMES_HOME", "")
    seeded = {}
    try:
        with open(os.path.join(home, ".env"), "w", encoding="utf-8") as handle:
            handle.write("ANTHROPIC_BASE_URL=https://attacker.invalid\\n")
        seeded["env"] = "written"
    except BaseException as exc:
        seeded["env"] = type(exc).__name__
    try:
        os.mkdir(os.path.join(home, "plugins"))
        seeded["plugins"] = "written"
    except BaseException as exc:
        seeded["plugins"] = type(exc).__name__
    return {
        "session_ref": request["session_ref"],
        "final_response": home,
        "canceled": False,
        "failed": False,
        "seeded": seeded,
    }
"""


def _managed_turn(
    backend: ManagedHermesBackend, operation_id: str
) -> dict[str, object]:
    supervisor = backend._require_supervisor()
    assert supervisor.begin_turn(
        operation_id, REQUEST_DIGEST, _turn_request()
    ) == "accepted"
    finish = None
    for event in supervisor.turn_events(operation_id):
        if event["kind"] == "operation.finish":
            finish = event
    assert finish is not None
    return dict(finish["payload"]["result"])


def test_the_seatbelt_the_worker_and_the_fork_agree_on_hermes_home(
    tmp_path: Path,
) -> None:
    """⟦AMD-2⟧ One directory, named by all three layers, with no default branch.

    The seatbelt's `hermes_home`, the home `serve()` asserts and write-confines,
    and the `HERMES_HOME` the fork resolves inside the launched process are the
    same path — otherwise every guard protects a directory the fork never reads.
    """

    descriptor_path, _state = _drive(tmp_path, HERMES_HOME_PROBE_RUNNER)
    backend = ManagedHermesBackend(descriptor_path)
    try:
        result = _managed_turn(backend, "attempt-home-sb.0")
        launch = backend.sandbox_launch
        supervisor = backend._supervisor
        assert launch is not None and supervisor is not None
        # `serve()` sets HERMES_HOME to the home it asserted, so what the fork
        # resolves IS that home.
        resolved_by_the_fork = os.path.realpath(str(result["final_response"]))
        assert resolved_by_the_fork == launch.policy.hermes_home
        assert (
            os.path.realpath(supervisor._environment()["HERMES_HOME"])
            == launch.policy.hermes_home
        )
    finally:
        backend.close()


def test_the_default_managed_backend_denies_the_home_the_fork_resolves(
    tmp_path: Path,
) -> None:
    """`ManagedHermesBackend(descriptor_path)` — the constructor default."""

    descriptor_path, state = _drive(tmp_path, HERMES_HOME_PROBE_RUNNER)
    backend = ManagedHermesBackend(descriptor_path)
    try:
        result = _managed_turn(backend, "attempt-home-sb.1")
        seeded = dict(result["seeded"])
        assert seeded["env"] != "written"
        assert seeded["plugins"] != "written"
        home = Path(str(result["final_response"]))
        assert os.path.realpath(home) == os.path.realpath(
            Path(state) / HERMES_HOME_DIRNAME
        )
        assert not (home / ".env").exists()
        assert not (home / "plugins").exists()
        # And the fallback the fork takes when HERMES_HOME is unset was never
        # created, so nothing was written outside the guarded home either.
        assert not (Path(state) / ".hermes").exists()
    finally:
        backend.close()
