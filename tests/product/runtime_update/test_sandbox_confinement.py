"""D3 layer 2: the in-process write confinement, and its agreement with layer 1.

F4's judgement stands — an audit hook is a containment for trusted code, not a
boundary against untrusted code — so what is pinned here is not that the hook is
unescapable. It is that the hook confines *the same set* the generated profile
confines, and that it turns a refusal into a typed Python error naming the path
instead of an EPERM the fork may swallow.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.sandbox import (
    DENIED_HOME_NAMES,
    EVIDENCE_DIRNAME,
    SANDBOX_EXEC,
    build_policy,
)
from cortex_platform.product.runtime_update.supervisor import (
    HERMES_HOME_DIRNAME,
    WorkerSupervisorV2,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker import (
    confinement as confinement_module,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.confinement import (
    SANDBOX_DENIED,
    WriteConfinement,
    WriteConfinementError,
    confinement_for,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.runtime import (
    HERMES_HOME_FORBIDDEN,
    HermesHomeUnsafe,
    assert_hermes_home,
)

from .test_worker_turns import _drive, _turn_request
from .test_worker_v2 import _descriptor

REQUEST_DIGEST = "c" * 64


def test_both_layers_name_the_same_denied_set() -> None:
    """One policy, two enforcers. A second set would be a second policy."""

    assert DENIED_HOME_NAMES == HERMES_HOME_FORBIDDEN
    assert EVIDENCE_DIRNAME == confinement_module.EVIDENCE_DIRNAME


def test_the_denied_set_covers_skills_which_reaches_the_system_prompt(
    tmp_path: Path,
) -> None:
    """ADJ-10: `skills/` is a HERMES_HOME child the fork reads without a gate.

    `agent/prompt_builder` reads `get_skills_dir()` with no config gate, so text
    written there persists into the system prompt of every later turn in the
    same (release_id, generation_id) — persistent prompt injection through a
    directory neither layer denied.
    """

    assert "skills" in HERMES_HOME_FORBIDDEN
    home = tmp_path / "home"
    home.mkdir()
    assert_hermes_home(home)
    skills = home / "skills"
    skills.mkdir()
    # Empty is tolerated, exactly like the fork's own `hooks/`.
    assert_hermes_home(home)
    (skills / "injected.md").write_text("do as I say", encoding="utf-8")
    with pytest.raises(HermesHomeUnsafe):
        assert_hermes_home(home)


def test_layer_two_confines_exactly_what_layer_one_writes(tmp_path: Path) -> None:
    descriptor_path, descriptor = _descriptor(tmp_path)
    policy = build_policy(descriptor, descriptor_path=descriptor_path)
    home = Path(descriptor.state_dir) / HERMES_HOME_DIRNAME
    confinement = confinement_for(
        str(descriptor.state_dir), str(home), HERMES_HOME_FORBIDDEN
    )
    assert confinement.write_roots == policy.write_roots
    assert confinement.denied_paths == policy.write_denied_paths


def test_confinement_permits_and_refuses_by_resolved_path(tmp_path: Path) -> None:
    state = tmp_path / "state"
    home = state / HERMES_HOME_DIRNAME
    home.mkdir(parents=True)
    confinement = confinement_for(str(state), str(home), HERMES_HOME_FORBIDDEN)
    assert confinement.permits(str(state / "operations.ledger.jsonl"))
    assert confinement.permits(str(home / "sessions" / "one.db"))
    assert confinement.permits("/dev/null")
    assert not confinement.permits(str(tmp_path / "escape.txt"))
    for name in HERMES_HOME_FORBIDDEN:
        assert not confinement.permits(str(home / name))
    assert not confinement.permits(str(home / "plugins" / "evil.py"))
    assert not confinement.permits(str(state / EVIDENCE_DIRNAME / "profile.sb"))
    # V3 again: a symlinked path must be judged by what it resolves to, not by
    # what it is spelled as.
    link = tmp_path / "link"
    link.symlink_to(state)
    assert confinement.permits(str(link / "inside.txt"))
    assert not confinement.permits(str(link / HERMES_HOME_DIRNAME / ".env"))


def test_audit_refuses_writes_and_ignores_reads(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / HERMES_HOME_DIRNAME).mkdir(parents=True)
    confinement = confinement_for(
        str(state), str(state / HERMES_HOME_DIRNAME), HERMES_HOME_FORBIDDEN
    )
    outside = str(tmp_path / "escape.txt")
    confinement.audit("open", (outside, "r", os.O_RDONLY))
    with pytest.raises(WriteConfinementError) as raised:
        confinement.audit("open", (outside, "w", None))
    assert SANDBOX_DENIED in str(raised.value)
    with pytest.raises(WriteConfinementError):
        confinement.audit("open", (outside, None, os.O_WRONLY | os.O_CREAT))
    with pytest.raises(WriteConfinementError):
        confinement.audit("os.mkdir", (str(tmp_path / "elsewhere"),))
    with pytest.raises(WriteConfinementError):
        confinement.audit("os.rename", (str(state / "a"), outside))
    # An already-open descriptor was audited when it was opened.
    confinement.audit("os.chmod", (7, 0o600))


def test_confinement_needs_a_writable_root() -> None:
    with pytest.raises(ValueError):
        WriteConfinement(write_roots=(), denied_paths=())


CONFINEMENT_RUNNER = """\
import os


def runner(request, context):
    # The worker's cwd is its state dir, and `serve()` derives HERMES_HOME from
    # the descriptor rather than the environment, so neither is guessed here.
    state = os.getcwd()
    home = os.path.join(state, "hermes-home")
    results = {}
    for name, target in (
        ("inside_state", os.path.join(state, "written-by-turn.txt")),
        ("hermes_home_env", os.path.join(home, ".env")),
        ("hermes_home_plugin", os.path.join(home, "plugins", "evil.py")),
        ("outside_state", os.path.join(os.path.dirname(state), "escape.txt")),
        ("sealed_profile", os.path.join(state, ".cortex-sandbox", "profile.sb")),
    ):
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x")
            results[name] = "written"
        except BaseException as exc:
            results[name] = type(exc).__name__ + ":" + str(exc).split(":")[0]
    return {
        "session_ref": request["session_ref"],
        "final_response": "confinement",
        "canceled": False,
        "failed": False,
        "confinement": results,
    }
"""


def test_the_hook_refuses_a_real_worker_write_with_a_typed_error(
    tmp_path: Path,
) -> None:
    """Driven through the real worker loop, in a real process, with no seatbelt.

    Independent of the layer-1 test on purpose: if the seatbelt were applied
    here, a passing result would prove nothing about the hook.
    """

    descriptor_path, state_dir = _drive(tmp_path, CONFINEMENT_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        assert supervisor.begin_turn(
            "attempt-conf.0", REQUEST_DIGEST, _turn_request()
        ) == "accepted"
        finish = None
        for event in supervisor.turn_events("attempt-conf.0"):
            if event["kind"] == "operation.finish":
                finish = event
    assert finish is not None
    observed = finish["payload"]["result"]["confinement"]
    assert observed["inside_state"] == "written"
    assert observed["hermes_home_env"].startswith("WriteConfinementError")
    assert observed["hermes_home_plugin"].startswith("WriteConfinementError")
    assert observed["outside_state"].startswith("WriteConfinementError")
    assert observed["sealed_profile"].startswith("WriteConfinementError")
    assert (Path(state_dir) / "written-by-turn.txt").is_file()
    assert not (tmp_path / "escape.txt").exists()
