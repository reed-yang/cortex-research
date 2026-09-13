"""D3 layer 1: the generated profile, and the probes that make it evidence.

The whole point of this module is that a profile which parses is not a profile
which denies — V3 lost a run to exactly that. So most of what is pinned here is
behaviour of the real `sandbox-exec` against a real interpreter, not the text.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update import sandbox as sandbox_module
from cortex_platform.product.runtime_update.sandbox import (
    DENIED_HOME_NAMES,
    EVIDENCE_DIRNAME,
    PROFILE_FILENAME,
    PROFILE_SCHEMA_VERSION,
    RESOLVER_SOCKET,
    RESOLVER_SOCKET_PATH,
    SANDBOX_EXEC,
    SandboxError,
    SandboxLaunch,
    SandboxProbeFailed,
    build_policy,
    prepare_sandbox,
    probe_definitions,
    profile_digest,
    render_profile,
    resolved,
)

from .test_worker_v2 import _descriptor

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or not os.path.isfile(SANDBOX_EXEC),
    reason="the managed worker sandbox is a Darwin sandbox-exec profile",
)


def _policy(tmp_path: Path):
    descriptor_path, descriptor = _descriptor(tmp_path)
    return descriptor_path, descriptor, build_policy(
        descriptor, descriptor_path=descriptor_path
    )


def test_every_policy_path_is_realpath_resolved(tmp_path: Path) -> None:
    """V3's footgun, closed by construction rather than by review.

    A `(subpath "/tmp/...")` rule matches nothing on macOS because `/tmp` is a
    symlink to `/private/tmp`, and SBPL compares resolved paths — the rule does
    not error, the write simply succeeds.
    """

    real = tmp_path / "real"
    (real / "state").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    descriptor_path, descriptor = _descriptor(tmp_path)
    document = json.loads(descriptor_path.read_text(encoding="utf-8"))
    document["state_dir"] = str(link / "state")
    descriptor_path.write_text(json.dumps(document), encoding="utf-8")
    from cortex_platform.product.runtime_update.worker_protocol import (
        SlotInterpreterDescriptor,
    )

    descriptor = SlotInterpreterDescriptor.load(descriptor_path)
    policy = build_policy(descriptor, descriptor_path=descriptor_path)
    assert policy.state_root == str(real.resolve() / "state")
    assert "/link/" not in policy.state_root
    for value in (
        policy.interpreter_path,
        policy.slot_root,
        policy.content_root,
        policy.hermes_home,
        policy.descriptor_path,
        *policy.interpreter_roots,
    ):
        assert value == resolved(value)


def test_profile_is_deterministic_and_denies_after_it_allows(tmp_path: Path) -> None:
    _, _, policy = _policy(tmp_path)
    text = render_profile(policy)
    assert render_profile(policy) == text
    assert profile_digest(text) == profile_digest(render_profile(policy))
    allow_write = text.index("(allow file-write*")
    deny_write = text.index("(deny file-write*")
    # SBPL is last-match-wins: a deny before the allow it carves out of is a
    # deny that does nothing.
    assert allow_write < deny_write
    assert text.index("(allow file-read*") < text.index("(deny file-read*")
    assert "(deny default)" in text
    assert "(deny network*)" in text
    assert "(deny process-fork)" in text
    assert f'(allow network-outbound (remote tcp "*:{policy.egress_port}"))' in text
    assert f'(allow process-exec (literal "{policy.interpreter_path}"))' in text


def test_the_profile_allows_the_resolver_socket_in_its_resolved_form(
    tmp_path: Path,
) -> None:
    """ADJ-01: without this rule the worker cannot resolve a provider hostname.

    `getaddrinfo(3)` on Darwin reaches mDNSResponder over a UNIX socket, which
    is a `network-outbound` operation the port-scoped `remote tcp` term does not
    match. The literal must be the realpath form: `/var/run/mDNSResponder` is a
    symlink into `/private/var/run`, and the unresolved rule is accepted by the
    kernel while matching nothing — V3's exact failure, measured again here.
    """

    _, _, policy = _policy(tmp_path)
    text = render_profile(policy)
    literal = f'(allow network-outbound (literal "{RESOLVER_SOCKET}"))'
    assert RESOLVER_SOCKET == resolved(RESOLVER_SOCKET_PATH)
    assert RESOLVER_SOCKET.startswith("/private/var/run/")
    assert literal in text
    assert f'(literal "{RESOLVER_SOCKET_PATH}")' not in text
    # SBPL is last-match-wins, so the allow has to follow the blanket deny.
    assert text.index("(deny network*)") < text.index(literal)
    # And it must not widen the egress: the port scoping is the rest of D3.
    assert "(allow network-outbound)" not in text
    assert f'(allow network-outbound (remote tcp "*:{policy.egress_port}"))' in text


def test_the_four_hermes_home_paths_are_write_denied(tmp_path: Path) -> None:
    _, _, policy = _policy(tmp_path)
    text = render_profile(policy)
    for name in DENIED_HOME_NAMES:
        assert f'(subpath "{os.path.join(policy.hermes_home, name)}")' in text
    denied = text.split("(deny file-write*", 1)[1].split("\n", 1)[0]
    for name in DENIED_HOME_NAMES:
        assert os.path.join(policy.hermes_home, name) in denied


def test_profile_refuses_an_unknown_schema_or_port(tmp_path: Path) -> None:
    _, descriptor, policy = _policy(tmp_path)
    from dataclasses import replace

    with pytest.raises(SandboxError):
        render_profile(replace(policy, schema_version=PROFILE_SCHEMA_VERSION + 1))
    descriptor_path = tmp_path / "descriptor.json"
    with pytest.raises(SandboxError):
        build_policy(descriptor, descriptor_path=descriptor_path, egress_port=0)
    with pytest.raises(SandboxError):
        build_policy(descriptor, descriptor_path=descriptor_path, egress_port=70_000)
    with pytest.raises(SandboxError):
        build_policy(descriptor, descriptor_path=descriptor_path, egress_port=True)


def test_prepare_sandbox_probes_every_denial_and_seals_the_profile(
    tmp_path: Path,
) -> None:
    descriptor_path, descriptor, _ = _policy(tmp_path)
    launch = prepare_sandbox(descriptor, descriptor_path=descriptor_path)
    observed = dict(launch.probes)
    assert observed["write_inside_state"] == "allowed"
    # ADJ-01: the resolver socket is the second positive control. A profile that
    # denies it starts a worker that cannot resolve a single provider hostname.
    assert observed["connect_resolver_socket"] == "allowed"
    for name in (
        "write_outside_state",
        "write_hermes_home_env",
        "read_sealed_profile",
        "create_hermes_home_plugins",
        "create_hermes_home_skills",
        "write_slot_content",
        "spawn_shell",
        "connect_non_egress_port",
    ):
        assert observed[name].startswith("denied:"), (name, observed[name])
    evidence = Path(descriptor.state_dir) / EVIDENCE_DIRNAME
    assert launch.profile_path == evidence / PROFILE_FILENAME
    assert stat.S_IMODE(launch.profile_path.lstat().st_mode) == 0o600
    assert launch.profile_sha256 == profile_digest(
        launch.profile_path.read_text(encoding="utf-8")
    )
    record = json.loads((evidence / "launch.json").read_text(encoding="utf-8"))
    assert record["profile_sha256"] == launch.profile_sha256
    assert record["policy_sha256"] == launch.policy.digest
    assert {entry["probe"] for entry in record["probes"]} == set(observed)
    # The positive control cleans up after itself; the state dir the worker is
    # about to use must not carry the prober's leavings.
    assert not (Path(descriptor.state_dir) / ".cortex-sandbox-probe").exists()


def test_launch_refuses_when_a_removed_rule_lets_a_probe_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prober's own failure mode, exercised independently of the accept test.

    The profile is generated with the write deny removed — the exact shape of
    V3's silent failure — and the launch must refuse rather than proceed with a
    boundary that is not there.
    """

    descriptor_path, descriptor, _ = _policy(tmp_path)
    original = render_profile

    def without_write_deny(policy):
        return "\n".join(
            line
            for line in original(policy).splitlines()
            if not line.startswith("(deny file-write*")
        ) + "\n"

    monkeypatch.setattr(sandbox_module, "render_profile", without_write_deny)
    with pytest.raises(SandboxProbeFailed) as raised:
        prepare_sandbox(descriptor, descriptor_path=descriptor_path)
    assert raised.value.probe == "write_hermes_home_env"
    assert raised.value.observation == "allowed"


def test_launch_refuses_when_the_resolver_rule_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADJ-01's regression, in the direction that actually shipped.

    The rule was absent, every one of the eight probes passed, and the profile
    was recorded as certified. The ninth probe is what distinguishes the two
    profiles: it is `allowed` with the rule and `denied:PermissionError`
    without it, deterministically and with no external DNS dependency.
    """

    descriptor_path, descriptor, _ = _policy(tmp_path)
    original = render_profile

    def without_resolver(policy):
        return "\n".join(
            line
            for line in original(policy).splitlines()
            if "mDNSResponder" not in line
        ) + "\n"

    monkeypatch.setattr(sandbox_module, "render_profile", without_resolver)
    with pytest.raises(SandboxProbeFailed) as raised:
        prepare_sandbox(descriptor, descriptor_path=descriptor_path)
    assert raised.value.probe == "connect_resolver_socket"
    assert raised.value.observation == "denied:PermissionError"


def test_wrap_applies_the_profile_to_the_launch(tmp_path: Path) -> None:
    descriptor_path, descriptor, _ = _policy(tmp_path)
    launch = prepare_sandbox(descriptor, descriptor_path=descriptor_path)
    assert isinstance(launch, SandboxLaunch)
    assert launch.wrap(["/bin/true", "--flag"]) == [
        SANDBOX_EXEC,
        "-f",
        str(launch.profile_path),
        "/bin/true",
        "--flag",
    ]


def test_egress_is_port_scoped_not_host_scoped(tmp_path: Path) -> None:
    """The honest 443 proof: two local listeners, one allowed port.

    Binding 443 needs root, so the rule is exercised at a port this process can
    bind. Nothing about the rule differs — `render_profile` interpolates the
    port into the same `(remote tcp "*:<port>")` term the production profile
    carries — so what is demonstrated is the port-scoping D3 relies on.
    """

    descriptor_path, descriptor, _ = _policy(tmp_path)
    listeners = []
    try:
        for _ in range(2):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listeners.append(listener)
        allowed_port = listeners[0].getsockname()[1]
        refused_port = listeners[1].getsockname()[1]
        policy = build_policy(
            descriptor, descriptor_path=descriptor_path, egress_port=allowed_port
        )
        profile = tmp_path / "egress.sb"
        profile.write_text(render_profile(policy), encoding="utf-8")
        completed = subprocess.run(
            [
                SANDBOX_EXEC,
                "-f",
                str(profile),
                policy.interpreter_path,
                "-I",
                "-c",
                "import socket, sys\n"
                "for port in sys.argv[1:]:\n"
                "    try:\n"
                "        socket.create_connection(('127.0.0.1', int(port)), timeout=3).close()\n"
                "        print(port, 'connected')\n"
                "    except PermissionError:\n"
                "        print(port, 'denied')\n",
                str(allowed_port),
                str(refused_port),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.stdout.split() == [
            str(allowed_port),
            "connected",
            str(refused_port),
            "denied",
        ], completed
    finally:
        for listener in listeners:
            listener.close()


def test_probe_set_names_every_denial_the_contract_lists(tmp_path: Path) -> None:
    _, _, policy = _policy(tmp_path)
    names = {probe.name for probe in probe_definitions(policy)}
    assert {
        "write_outside_state",
        "write_hermes_home_env",
        "spawn_shell",
        "connect_non_egress_port",
        "connect_resolver_socket",
        "create_hermes_home_skills",
    } <= names
    # One process per probe, so a probe that kills the interpreter cannot hide
    # the ones that would have run after it.
    assert len({probe.name for probe in probe_definitions(policy)}) == len(
        probe_definitions(policy)
    )


def test_a_probe_that_failed_for_another_reason_is_not_a_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prober's own V3 hazard, pinned.

    A write to `<hermes_home>/.env` fails with ENOENT when the directory does
    not exist, whatever the profile says. If that counted as a denial the whole
    boundary could be absent and every probe would still pass.
    """

    descriptor_path, descriptor, policy = _policy(tmp_path)
    profile = tmp_path / "probe.sb"
    profile.write_text(render_profile(policy), encoding="utf-8")
    Path(policy.state_root).mkdir(parents=True, exist_ok=True)
    # No `hermes-home` directory: exactly the state the prober must not accept.
    assert not Path(policy.hermes_home).exists()
    with pytest.raises(SandboxProbeFailed) as raised:
        sandbox_module.run_probes(policy, profile)
    assert raised.value.probe == "write_hermes_home_env"
    assert raised.value.observation == "reached:FileNotFoundError"


def test_a_probe_that_gets_through_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prober's leavings, found by the real acceptance.

    With the write deny removed, `write_hermes_home_env` succeeds — and what it
    writes is a real `HERMES_HOME/.env`, which `assert_hermes_home` refuses to
    start beside. A prober that tidied up only after its positive control would
    turn one bad profile into a generation that can never launch again.
    """

    descriptor_path, descriptor, policy = _policy(tmp_path)
    Path(policy.state_root).mkdir(parents=True, exist_ok=True)
    Path(policy.hermes_home).mkdir(parents=True, exist_ok=True)
    original = render_profile

    def without_write_deny(policy):
        return "\n".join(
            line
            for line in original(policy).splitlines()
            if not line.startswith("(deny file-write*")
        ) + "\n"

    monkeypatch.setattr(sandbox_module, "render_profile", without_write_deny)
    with pytest.raises(SandboxProbeFailed):
        prepare_sandbox(descriptor, descriptor_path=descriptor_path)

    assert not (Path(policy.hermes_home) / ".env").exists()
    assert not (Path(policy.hermes_home) / "plugins").exists()
    assert not (Path(policy.state_root) / ".cortex-sandbox-probe").exists()
    assert not (Path(policy.state_root).parent / ".cortex-sandbox-probe").exists()


def test_a_probe_that_times_out_still_cleans_up_and_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADJ-11: a timeout left behind exactly the artifact cleanup exists to remove.

    `subprocess.run(..., timeout=...)` was followed by `_clean(probe)` with no
    `try/finally`, so `TimeoutExpired` propagated out of `run_probes` with the
    probe's `.env` still on disk — and `assert_hermes_home` then refuses every
    later launch of that generation.
    """

    descriptor_path, descriptor, policy = _policy(tmp_path)
    Path(policy.state_root).mkdir(parents=True, exist_ok=True)
    Path(policy.hermes_home).mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "timeout.sb"
    profile.write_text(render_profile(policy), encoding="utf-8")
    env_path = Path(policy.hermes_home) / ".env"
    only = tuple(
        probe
        for probe in probe_definitions(policy)
        if probe.name == "write_hermes_home_env"
    )
    # Just the one probe: the other eight would run a real `sandbox-exec` each,
    # and a loaded host would then decide which probe timed out first.
    monkeypatch.setattr(sandbox_module, "probe_definitions", lambda _policy: only)

    def timing_out(command, **kwargs):
        # The shape of the hazard: the probe got far enough to create the file
        # and then never returned.
        env_path.write_text("probe", encoding="utf-8")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30.0))

    monkeypatch.setattr(sandbox_module.subprocess, "run", timing_out)
    with pytest.raises(SandboxProbeFailed) as raised:
        sandbox_module.run_probes(policy, profile)
    assert raised.value.probe == "write_hermes_home_env"
    assert raised.value.observation == "timed out"
    assert not env_path.exists()
