"""The acceptance's worker entrypoint: the real fork, an echo instead of a model.

Everything below the model call is production code. This file bootstraps exactly
as `runtime_worker.py` does (⟦AMD-6⟧: its own resolved directory, never the cwd,
never PYTHONPATH), imports the release's own `cortex_worker` package, and serves
with the shipped `serve()` — so the framed channel, the turn thread, the
heartbeat timer, the fd hygiene, the ledger and the projection digest are all the
bytes the release was attested with.

What it replaces is one line: `agent.run_conversation(...)`. It still performs
the real fork import through `cortex_worker.runtime.load_runtime` (which runs the
⟦AMD-2⟧ HERMES_HOME assertion immediately before `import run_agent`), and it
still drives an approval through the fork's own `tools.terminal_tool.
set_approval_callback`, so the approval bridge that `turn.resolve` releases is
the real one. It simply answers the model itself, because a provider call is a
network call and this acceptance makes none.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _bootstrap() -> None:
    sys.dont_write_bytecode = True
    directory = os.path.dirname(os.path.realpath(__file__))
    if directory not in sys.path:
        sys.path.insert(0, directory)


def handle(method, params):
    if method == "health":
        return {
            "status": "healthy",
            "release_id": params.get("release_id") if isinstance(params, dict) else None,
        }
    if method == "identity":
        return {"entrypoint": os.path.basename(os.path.realpath(__file__))}
    return {"method": method, "params": params}


def _denial_probes(home):
    """Run D3's denials from inside the real worker, under the real profile.

    Not the pre-launch prober: this is the fork's own process, after the fork
    has been imported, doing the things the seatbelt says it cannot.

    Every filesystem denial is attempted TWICE, and both answers are recorded
    (ADJ-16). Layer 2 — the in-process audit hook — answers first for an
    absolute path and raises `WriteConfinementError` before the syscall is ever
    issued, so an absolute-path attempt is evidence about the hook, not about
    the seatbelt. D3 declares layer 1 the boundary and layer 2 "a containment
    for trusted code, not a boundary against untrusted code", so the record has
    to be able to tell them apart: if a future change stopped installing or
    matching the profile, an absolute-path-only record would still show green
    "denials from inside the real worker".

    The layer-1 answer is taken by naming the same target RELATIVELY after a
    chdir. `WriteConfinement.check` declines relative paths on purpose (an audit
    hook cannot see the `dir_fd` an `openat` used), so the hook steps aside and
    the kernel answers with EPERM — no uninstalling, no second process.
    """

    import socket
    import subprocess

    state = os.path.dirname(home)
    content = os.path.dirname(os.path.realpath(__file__))
    results = {}
    layer1 = {}
    layer2 = {}

    def attempt(name, action, into=None):
        """Record one answer. `into` selects the layer bucket it belongs to."""

        try:
            action()
            observation = "allowed"
        except BaseException as exc:  # noqa: BLE001 - the refusal is the evidence
            observation = "denied:" + type(exc).__name__
        results[name] = observation
        if into is not None:
            into[name] = observation
        return observation

    def relative(name, directory, action):
        """The same denial, asked so that layer 1 is the one that answers."""

        try:
            previous = os.getcwd()
        except BaseException:  # noqa: BLE001
            # The launch cwd may be outside every read root, in which case
            # `getcwd` itself is EPERM. The state dir always is inside one.
            previous = state
        try:
            os.chdir(directory)
        except BaseException as exc:  # noqa: BLE001
            layer1[name] = "unreachable:" + type(exc).__name__
            return
        try:
            try:
                action()
                observation = "allowed"
            except BaseException as exc:  # noqa: BLE001
                observation = "denied:" + type(exc).__name__
        finally:
            os.chdir(previous)
        results[name + "__layer1"] = observation
        layer1[name] = observation

    attempt("write_inside_state", lambda: open(
        os.path.join(state, "acceptance-write.txt"), "w").write("x"))
    attempt("write_outside_state", lambda: open(
        os.path.join(os.path.dirname(state), "escape.txt"), "w").write("x"), layer2)
    relative("write_outside_state", state,
             lambda: open(os.path.join("..", "escape-relative.txt"), "w").write("x"))
    attempt("write_hermes_home_env", lambda: open(
        os.path.join(home, ".env"), "w").write("x"), layer2)
    relative("write_hermes_home_env", home,
             lambda: open(".env", "w").write("x"))
    attempt("create_hermes_home_plugins", lambda: os.mkdir(
        os.path.join(home, "plugins")), layer2)
    relative("create_hermes_home_plugins", home, lambda: os.mkdir("plugins"))
    # ADJ-10: `skills/` reaches the fork's system prompt with no config gate.
    attempt("create_hermes_home_skills", lambda: os.mkdir(
        os.path.join(home, "skills")), layer2)
    relative("create_hermes_home_skills", home, lambda: os.mkdir("skills"))
    attempt("relative_escape", lambda: open(
        os.path.join("..", "..", "relative-escape.txt"), "w").write("x"), layer1)
    attempt("write_slot_content", lambda: open(
        os.path.join(content, "evil.py"),
        "w").write("x"), layer2)
    relative("write_slot_content", content, lambda: open("evil-relative.py", "w").write("x"))
    attempt("read_slot_content", lambda: open(
        os.path.realpath(__file__), "rb").read(16))
    attempt("spawn_shell", lambda: subprocess.run(
        ["/bin/sh", "-c", "exit 0"], capture_output=True, timeout=10), layer1)
    attempt("fork", lambda: os.waitpid(os.fork(), 0), layer1)
    # ADJ-01: the profile denied name resolution outright, and nothing in the
    # stack could see it because every egress check uses a numeric address.
    # This is the fork's own process resolving a public name through the
    # resolver socket the profile now allows.
    attempt(
        "resolve_public_hostname",
        lambda: results.__setitem__(
            "resolved_address",
            socket.getaddrinfo("example.com", 443, socket.AF_INET,
                               socket.SOCK_STREAM)[0][4][0],
        ),
    )
    attempt(
        "connect_resolver_socket",
        lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(
            os.path.realpath("/var/run/mDNSResponder")),
    )

    ports = [int(value) for value in os.environ.get(
        "CORTEX_ACCEPTANCE_PORTS", "").split(",") if value.strip()]
    for index, port in enumerate(ports):
        attempt(
            "connect_port_" + str(index),
            lambda port=port: socket.create_connection(
                ("127.0.0.1", port), timeout=3).close(),
        )
    results["ports"] = ports
    results["layer1"] = layer1
    results["layer2"] = layer2

    try:
        # The fork's real tool entry, not a guess at one. It must fail closed
        # under the profile: `process-fork` is denied, so nothing it shells out
        # to can start.
        from tools.terminal_tool import terminal_tool  # type: ignore

        results["terminal_tool"] = str(terminal_tool("echo pwned", timeout=10))[:300]
    except BaseException as exc:  # noqa: BLE001
        results["terminal_tool"] = "raised:" + type(exc).__name__ + ":" + str(exc)[:200]
    layer1["terminal_tool"] = results["terminal_tool"]
    return results


def runner(request, context):
    from cortex_worker.approval import fork_choice
    from cortex_worker.runtime import load_runtime

    home = Path(os.environ["HERMES_HOME"])
    # The real import, on the real slot, under the real cp311 — and the real
    # HERMES_HOME assertion immediately before it.
    hermes_state, agent_class, set_approval_callback = load_runtime(home)
    context.emit(
        "tool.started",
        {
            "tool_call_id": "call-acceptance-1",
            "tool_name": "bash",
            "arguments": {"command": "echo hello"},
        },
    )

    decision_id = "decision-acceptance-1"
    approvals: list[str] = []

    def approval(command, description, *, allow_permanent=True):
        _ = allow_permanent
        context.emit(
            "decision.required",
            {
                "decision_id": decision_id,
                "decision_kind": "approval",
                "prompt": "Approve this runtime tool action?",
                "command": command,
                "description": description,
            },
        )
        wire = context.await_decision(decision_id)
        # The production mapping point, exercised rather than bypassed: the wire
        # vocabulary is Cortex's `approve_once|deny`, the fork's is `once|deny`,
        # and `session` or `always` must not survive the crossing.
        mapped = fork_choice(wire)
        approvals.append({"wire": wire, "fork": mapped})
        return mapped

    # The fork's own thread-local approval bridge, installed and invoked. This
    # is the park that `turn.resolve` releases across the channel.
    set_approval_callback(approval)
    try:
        from tools.terminal_tool import _get_approval_callback  # type: ignore

        installed = _get_approval_callback()
    except Exception:
        installed = None
    try:
        choice = approval("echo hello", "acceptance approval")
    finally:
        set_approval_callback(None)

    denials = (
        _denial_probes(str(home))
        if str(request.get("user_message", "")).startswith("probe-denials")
        else None
    )

    context.emit(
        "tool.completed",
        {
            "tool_call_id": "call-acceptance-1",
            "tool_name": "bash",
            "is_error": choice != "once",
            "duration_ms": 7,
        },
    )
    context.emit("token.delta", {"text": "echo: "})
    context.raise_if_canceled()
    return {
        "session_ref": request["session_ref"],
        "final_response": "echo: " + str(request.get("user_message", "")),
        "canceled": False,
        "failed": False,
        # Evidence carried back as ordinary result fields: the fork really
        # imported, and the approval really went through its bridge.
        "fork_modules": sorted(
            name
            for name in ("hermes_state", "run_agent", "tools.terminal_tool")
            if name.split(".")[0] in sys.modules
        ),
        "session_db_class": getattr(hermes_state.SessionDB, "__name__", None),
        "agent_class": getattr(agent_class, "__name__", None),
        "approval_bridge_installed": installed is not None,
        "approvals": approvals,
        "denials": denials,
    }


if __name__ == "__main__":
    _bootstrap()
    from cortex_worker.protocol import SlotInterpreterDescriptor
    from cortex_worker.serve import serve

    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-descriptor", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(
        serve(
            SlotInterpreterDescriptor.load(arguments.v2_descriptor),
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            runner=runner,
            heartbeat_interval=1.0,
        )
    )
