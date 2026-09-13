"""D3 layer 1: the OS boundary one managed worker launch runs inside.

Contract D3 splits the sandbox in two. This module owns the outer half — a
`sandbox-exec` profile generated per launch from the descriptor's own resolved
paths, applied by wrapping the launch argv, and *probed* before the worker is
allowed to start.

Three lessons are built in rather than documented.

**Every path is resolved before substitution.** V3 lost a run to a
`(deny file-write* (subpath "/tmp/..."))` rule that matched nothing, silently,
because SBPL compares resolved paths and `/tmp` is a symlink to `/private/tmp`.
`SandboxPolicy` can only hold `os.path.realpath` output, so an unresolved path
cannot reach the profile text.

**A syntactically valid profile is not an effective one.** The same V3 rule was
accepted by the kernel and did nothing. So `prepare_sandbox` runs one probe
process per denial under the generated profile and refuses the launch with
`SandboxProbeFailed` if any denial does not deny — the discipline
`apps/web/scripts/release-supply.mjs` calls "allow-and-deny-probed".

**The permitted set is measured, not guessed.** The read roots and the single
`process-exec` literal below are what CPython 3.11 needed to start under this
profile on Darwin 25.3, narrowed until removing any one of them aborted the
interpreter.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .supervisor import HERMES_HOME_DIRNAME
from .worker_protocol import SlotInterpreterDescriptor

#: The system profile applier. A literal, never resolved from `PATH`.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

PROFILE_SCHEMA_VERSION = 1

#: D3: "outbound TCP to 443, everything else denied". Port-scoped rather than
#: host-scoped because SBPL's `remote tcp` matches the address resolved at
#: connect time, which no per-provider hostname rule can survive against a CDN.
DEFAULT_EGRESS_PORT = 443

#: Measured: CPython 3.11.15 aborts at startup without a read rule for the root
#: directory itself. It is a `literal`, so it grants nothing below `/`.
SYSTEM_READ_PATHS = ("/",)
SYSTEM_READ_ROOTS = (
    "/System",
    "/usr/lib",
    "/usr/share",
    "/private/var/db",
    # `/etc/hosts` and `/etc/services`, which `getaddrinfo(3)` consults beside
    # the resolver socket below. Measured: without this root a name in
    # `/etc/hosts` and every `getservbyname` lookup fail under the profile.
    "/private/etc",
    "/dev",
)

#: The names the fork loads code, configuration or prompt text from inside
#: `HERMES_HOME`, in the order `cortex_worker.runtime.HERMES_HOME_FORBIDDEN`
#: lists them. None of them may ever be *written*: that is what stops the fork,
#: or anything it runs, creating a plugin, a hook, a script, a skill or a dotenv
#: after the worker's start-up assertion has already passed.
#:
#: It is deliberately NOT a claim to be the whole HERMES_HOME code and config
#: surface, which it is not: `config.yaml`, `cli-config.yaml` and
#: `shell-hooks-allowlist.json` are HERMES_HOME children this set does not name.
#: What contains them is that every loader that reads them calls the fork's
#: `ensure_hermes_home()` first, which mkdirs `hooks` — denied here — so the load
#: raises and the fork falls back to its defaults. `skills` had no such gate:
#: `agent/prompt_builder` reads it directly, so text written there would persist
#: into the system prompt of every later turn in the same generation.
DENIED_HOME_NAMES = (".env", "plugins", "hooks", "scripts", "skills")

#: `.env` additionally may not be *read*. It is the one of the four the fork
#: opens rather than executes — `run_agent` calls `load_dotenv(override=True)` at
#: import — and the worker's assertion only ever `lstat`s it, so denying the read
#: closes the path without costing the typed refusal.
#:
#: The three directories are deliberately NOT read-denied. Measured: a
#: `HERMES_HOME` that predates this profile already contains the empty `hooks/`
#: the fork creates at import, and `assert_hermes_home` lists each of them — so a
#: read-deny turns "empty and harmless" into an untyped `PermissionError` that
#: kills the worker instead of the typed `HermesHomeUnsafe` refusal. Write-deny
#: is sufficient on its own: content can never appear in them, and content that
#: appeared before the profile existed is refused by the worker-side assertion
#: before the fork is imported.
READ_DENIED_HOME_NAMES = (".env",)

#: Where the generated profile and its launch record are kept: inside the state
#: dir, so the evidence lives beside the worker it describes, and sealed by the
#: profile itself, so the worker cannot rewrite the profile of its next launch.
EVIDENCE_DIRNAME = ".cortex-sandbox"
PROFILE_FILENAME = "profile.sb"
LAUNCH_RECORD_FILENAME = "launch.json"

#: The name every probe writes, so a probe that unexpectedly succeeds leaves one
#: predictable path to clean up rather than an arbitrary one.
PROBE_BASENAME = ".cortex-sandbox-probe"


class SandboxError(RuntimeError):
    """The launch sandbox could not be established."""


class SandboxUnsupported(SandboxError):
    """This host has no `sandbox-exec` to apply a profile with."""


class SandboxProbeFailed(SandboxError):
    """A rule in the generated profile did not do what the profile says.

    Terminal for the launch. A profile whose denials do not deny is worse than
    no profile, because everything downstream would report a boundary that is
    not there.
    """

    def __init__(self, probe: str, observation: str) -> None:
        super().__init__(f"sandbox probe {probe} observed {observation}")
        self.probe = probe
        self.observation = observation


def resolved(path: object) -> str:
    """The one way a path becomes profile text."""

    return os.path.realpath(str(path))


#: The resolver's UNIX socket, and the reason there is a `network-outbound`
#: rule that is not a `remote tcp` term. `getaddrinfo(3)` on Darwin does not
#: speak DNS itself: it asks mDNSResponder over this socket, which the sandbox
#: classifies as `network-outbound` and which `(remote tcp "*:443")` does not
#: match. Without it `(deny network*)` denies name resolution outright, so a
#: worker under this profile can reach 443 at an address it can never learn —
#: the fork's own default `base_url` is a hostname.
#:
#: Resolved like every other path in this module, and for the same reason:
#: `/var/run` is a symlink to `/private/var/run`, and the unresolved literal is
#: accepted by the kernel while matching nothing. Measured under real
#: `sandbox-exec`: with `(literal "/var/run/mDNSResponder")` `getaddrinfo`
#: still fails; with the resolved form it succeeds, and `1.1.1.1:80` stays
#: EPERM, so the port scoping is untouched.
RESOLVER_SOCKET_PATH = "/var/run/mDNSResponder"
RESOLVER_SOCKET = resolved(RESOLVER_SOCKET_PATH)


@dataclass(frozen=True)
class SandboxPolicy:
    """What the boundary is, in resolved paths and one port.

    Deliberately a description rather than a rule list: the SBPL rules are
    derived from it in `render_profile`, and the probes are derived from it in
    `probe_definitions`, so the two can never disagree about which path is which.
    """

    schema_version: int
    interpreter_path: str
    interpreter_roots: tuple[str, ...]
    slot_root: str
    content_root: str
    state_root: str
    hermes_home: str
    descriptor_path: str
    evidence_root: str
    egress_port: int

    @property
    def read_paths(self) -> tuple[str, ...]:
        return tuple(sorted({*SYSTEM_READ_PATHS, self.descriptor_path}))

    @property
    def read_roots(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *SYSTEM_READ_ROOTS,
                    *self.interpreter_roots,
                    # The slot root, not just `content/`: `measure_identity`
                    # reads the slot's own `slot.json` and `manifest.json`
                    # beside it, and a worker that cannot read its evidence
                    # cannot answer the identity question at all.
                    self.slot_root,
                    self.state_root,
                }
            )
        )

    @property
    def write_roots(self) -> tuple[str, ...]:
        return (self.state_root,)

    @property
    def write_denied_paths(self) -> tuple[str, ...]:
        denied = {os.path.join(self.hermes_home, name) for name in DENIED_HOME_NAMES}
        # The profile of the *next* launch lives here. A worker that could
        # rewrite it would be choosing its own successor's boundary.
        denied.add(self.evidence_root)
        return tuple(sorted(denied))

    @property
    def read_denied_paths(self) -> tuple[str, ...]:
        denied = {
            os.path.join(self.hermes_home, name)
            for name in READ_DENIED_HOME_NAMES
        }
        # The worker has no business reading the profile that constrains it, and
        # this is also the one read-denied path the prober can prove: a denied
        # read of a path that does not exist reports ENOENT rather than EPERM
        # (measured), so `.env` — which must not exist for the worker to start at
        # all — cannot be probed without creating it. The evidence root is
        # rendered by the same rule, from the same tuple, and always exists.
        denied.add(self.evidence_root)
        return tuple(sorted(denied))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "interpreter_path": self.interpreter_path,
            "interpreter_roots": list(self.interpreter_roots),
            "slot_root": self.slot_root,
            "content_root": self.content_root,
            "state_root": self.state_root,
            "hermes_home": self.hermes_home,
            "descriptor_path": self.descriptor_path,
            "evidence_root": self.evidence_root,
            "egress_port": self.egress_port,
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _quote(value: str) -> str:
    """SBPL string literal. `json.dumps` escapes exactly what SBPL needs."""

    return json.dumps(value)


def _terms(kind: str, values: Iterable[str]) -> str:
    return " ".join(f"({kind} {_quote(value)})" for value in values)


def build_policy(
    descriptor: SlotInterpreterDescriptor,
    *,
    descriptor_path: Path,
    hermes_home: Path | None = None,
    egress_port: int = DEFAULT_EGRESS_PORT,
) -> SandboxPolicy:
    """Derive the boundary from the descriptor, resolving as it goes.

    `hermes_home` defaults to the directory `worker_environment` creates inside
    the state dir; it stays a parameter so the seam S3.3 left — "`worker_environment`
    returns the resolved HERMES_HOME so the seatbelt can deny-write its four
    paths" — is honoured with the value actually handed to the worker rather
    than a second guess at it.

    Two interpreter roots, not one, and both resolved. A release's interpreter is
    `interpreters/<archive_sha256>/bin/python3.11` and the two agree; a venv's
    `bin/python` is a symlink into a shared store, and the binary's own root and
    the directory it was launched from are then different trees, both of which
    CPython reads before it can run anything.
    """

    if type(egress_port) is not int:
        raise SandboxError("sandbox egress port must be an integer")
    if not 1 <= egress_port <= 65535:
        raise SandboxError("sandbox egress port is out of range")
    state_dir = Path(descriptor.state_dir)
    home = Path(hermes_home) if hermes_home is not None else state_dir / HERMES_HOME_DIRNAME
    interpreter = resolved(descriptor.interpreter_path)
    roots = {
        os.path.dirname(os.path.dirname(interpreter)),
        resolved(Path(descriptor.interpreter_path).parent.parent),
    }
    state_root = resolved(state_dir)
    return SandboxPolicy(
        schema_version=PROFILE_SCHEMA_VERSION,
        interpreter_path=interpreter,
        interpreter_roots=tuple(sorted(roots)),
        slot_root=resolved(descriptor.slot_path),
        content_root=resolved(Path(descriptor.slot_path) / "content"),
        state_root=state_root,
        hermes_home=resolved(home),
        descriptor_path=resolved(descriptor_path),
        evidence_root=os.path.join(state_root, EVIDENCE_DIRNAME),
        egress_port=egress_port,
    )


def render_profile(policy: SandboxPolicy) -> str:
    """Deterministic SBPL for one policy.

    Order matters twice over. SBPL is last-match-wins, so the denials come after
    the allow they carve out of; and the text is the thing whose digest is
    recorded, so the same policy must render the same bytes on every host.
    """

    if policy.schema_version != PROFILE_SCHEMA_VERSION:
        raise SandboxError("sandbox policy schema is unsupported")
    lines = [
        "(version 1)",
        "; Generated per launch by cortex_platform.product.runtime_update.sandbox.",
        "; Every path below is os.path.realpath output; see D3 and V3.",
        "(deny default)",
        "(deny network*)",
        # Name resolution, and only name resolution: a literal for the resolver
        # socket rather than a bare `(allow network-outbound)`, which would
        # dissolve the port scoping below.
        f"(allow network-outbound (literal {_quote(RESOLVER_SOCKET)}))",
        "(deny process-fork)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow signal (target self))",
        "(allow process-info* (target self))",
        # Metadata only. `stat` is not the boundary; opening is.
        "(allow file-read-metadata)",
        f"(allow file-read* {_terms('literal', policy.read_paths)} "
        f"{_terms('subpath', policy.read_roots)})",
        f"(allow file-write* (literal \"/dev/null\") "
        f"{_terms('subpath', policy.write_roots)})",
        # Last match wins: these sit inside a writable root on purpose.
        f"(deny file-write* {_terms('subpath', policy.write_denied_paths)})",
        f"(deny file-read* {_terms('subpath', policy.read_denied_paths)})",
        # The one executable this profile admits. `sandbox-exec` applies the
        # profile before it execs, so without this the interpreter never starts;
        # with `process-fork` denied, nothing after it can start either.
        f"(allow process-exec (literal {_quote(policy.interpreter_path)}))",
        f"(allow network-outbound (remote tcp \"*:{policy.egress_port}\"))",
        "",
    ]
    return "\n".join(lines)


def profile_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Probes. One process each, so no probe can mask another's result.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SandboxProbe:
    name: str
    expect: str
    source: str
    arguments: tuple[str, ...]
    #: Paths this probe creates if the profile lets it through. Removed after
    #: every run, pass or fail. A probe that unexpectedly SUCCEEDS is the one
    #: whose leavings matter most: `write_hermes_home_env` writing a real `.env`
    #: makes `assert_hermes_home` refuse every later launch, so a prober that
    #: only tidied up after its positive control would turn one bad profile into
    #: a permanently unstartable generation. Found by the real acceptance, whose
    #: removed-rule negative case did exactly that.
    cleanup: tuple[str, ...] = ()


#: Every probe distinguishes three answers, not two. "reached" is the V3 failure
#: reproduced inside the prober itself: an operation that failed for a reason the
#: profile had nothing to do with — a missing parent directory, an absent
#: listener — looks exactly like a denial unless it is named separately.
_WRITE_PROBE = """
import sys
try:
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write("probe")
    print("allowed")
except FileNotFoundError:
    print("reached:FileNotFoundError")
except BaseException as exc:
    print("denied:" + type(exc).__name__)
"""

_READ_PROBE = """
import sys
try:
    with open(sys.argv[1], "rb") as handle:
        handle.read(1)
    print("allowed")
except FileNotFoundError:
    # The profile is evaluated before the lookup, so a denied path reports
    # EPERM whether or not it exists. ENOENT means the deny did not match.
    print("reached:FileNotFoundError")
except BaseException as exc:
    print("denied:" + type(exc).__name__)
"""

_MKDIR_PROBE = """
import os, sys
try:
    os.mkdir(sys.argv[1])
    print("allowed")
except (FileNotFoundError, FileExistsError) as exc:
    print("reached:" + type(exc).__name__)
except BaseException as exc:
    print("denied:" + type(exc).__name__)
"""

_SPAWN_PROBE = """
import subprocess, sys
try:
    subprocess.run([sys.argv[1], "-c", "exit 0"], capture_output=True, timeout=10)
    print("allowed")
except BaseException as exc:
    print("denied:" + type(exc).__name__)
"""

_CONNECT_PROBE = """
import socket, sys
try:
    connection = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
    connection.close()
    print("allowed")
except PermissionError:
    print("denied:PermissionError")
except BaseException as exc:
    # ECONNREFUSED would mean the rule let the connect through and only the
    # absent listener stopped it, which proves nothing.
    print("reached:" + type(exc).__name__)
"""


_UNIX_CONNECT_PROBE = """
import socket, sys
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    connection.connect(sys.argv[1])
    print("allowed")
except PermissionError:
    print("denied:PermissionError")
except BaseException as exc:
    # An absent socket reports ENOENT and proves nothing about the rule.
    print("reached:" + type(exc).__name__)
finally:
    connection.close()
"""


def _non_egress_port(egress_port: int) -> int:
    """A port the profile must refuse. Never the one it allows."""

    return 1 if egress_port != 1 else 2


def probe_definitions(policy: SandboxPolicy) -> tuple[SandboxProbe, ...]:
    outside = os.path.join(os.path.dirname(policy.state_root), PROBE_BASENAME)
    return (
        # The positive control. A profile that denies this would fail the worker
        # later, in a way that looks like a worker bug rather than a policy one.
        SandboxProbe(
            "write_inside_state",
            "allowed",
            _WRITE_PROBE,
            (os.path.join(policy.state_root, PROBE_BASENAME),),
            (os.path.join(policy.state_root, PROBE_BASENAME),),
        ),
        SandboxProbe(
            "write_outside_state", "denied", _WRITE_PROBE, (outside,), (outside,)
        ),
        SandboxProbe(
            "write_hermes_home_env",
            "denied",
            _WRITE_PROBE,
            (os.path.join(policy.hermes_home, ".env"),),
            (os.path.join(policy.hermes_home, ".env"),),
        ),
        SandboxProbe(
            "read_sealed_profile",
            "denied",
            _READ_PROBE,
            (os.path.join(policy.evidence_root, PROFILE_FILENAME),),
        ),
        SandboxProbe(
            "create_hermes_home_plugins",
            "denied",
            _MKDIR_PROBE,
            (os.path.join(policy.hermes_home, "plugins"),),
            (os.path.join(policy.hermes_home, "plugins"),),
        ),
        SandboxProbe(
            "create_hermes_home_skills",
            "denied",
            _MKDIR_PROBE,
            (os.path.join(policy.hermes_home, "skills"),),
            (os.path.join(policy.hermes_home, "skills"),),
        ),
        SandboxProbe(
            "write_slot_content",
            "denied",
            _WRITE_PROBE,
            (os.path.join(policy.content_root, PROBE_BASENAME),),
            (os.path.join(policy.content_root, PROBE_BASENAME),),
        ),
        SandboxProbe("spawn_shell", "denied", _SPAWN_PROBE, ("/bin/sh",)),
        SandboxProbe(
            "connect_non_egress_port",
            "denied",
            _CONNECT_PROBE,
            (str(_non_egress_port(policy.egress_port)),),
        ),
        # The second positive control, and the only probe in the battery that
        # can see whether the worker will be able to resolve a hostname. It
        # connects to the resolver socket rather than resolving a real name on
        # purpose: a probe failure is terminal for the launch, so a real-DNS
        # probe would turn a network blip into a worker that will not start.
        SandboxProbe(
            "connect_resolver_socket",
            "allowed",
            _UNIX_CONNECT_PROBE,
            (RESOLVER_SOCKET,),
        ),
    )


def probe_command(
    policy: SandboxPolicy, profile_path: Path, probe: SandboxProbe
) -> list[str]:
    return [
        SANDBOX_EXEC,
        "-f",
        str(profile_path),
        policy.interpreter_path,
        "-I",
        "-c",
        probe.source,
        *probe.arguments,
    ]


def assert_available() -> None:
    if platform.system() != "Darwin" or not os.path.isfile(SANDBOX_EXEC):
        raise SandboxUnsupported(
            "the managed worker sandbox requires Darwin's sandbox-exec"
        )


def _clean(probe: SandboxProbe) -> None:
    """Remove whatever the probe managed to create, whatever the outcome."""

    for path in probe.cleanup:
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def run_probes(
    policy: SandboxPolicy, profile_path: Path, *, timeout: float = 30.0
) -> tuple[tuple[str, str], ...]:
    """Run every probe, and raise on the first one that misbehaves.

    Each probe is its own process. A shared one would let a probe that crashed
    the interpreter hide the results of the probes after it — the shape §7 of the
    contract refuses for tests, for the same reason.
    """

    assert_available()
    observations: list[tuple[str, str]] = []
    for probe in probe_definitions(policy):
        try:
            completed = subprocess.run(
                probe_command(policy, profile_path, probe),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # A probe that hung still created whatever it created. Cleaning up
            # only on the paths that returned left a real `HERMES_HOME/.env`
            # behind, which `assert_hermes_home` then refuses every later launch
            # beside — one loaded machine, permanently unstartable. The refusal
            # is typed like every other probe refusal so the caller sees a
            # sandbox failure rather than a raw subprocess exception.
            raise SandboxProbeFailed(probe.name, "timed out") from None
        finally:
            _clean(probe)
        observation = completed.stdout.strip().splitlines()[-1:] or [""]
        observed = observation[0]
        if probe.expect == "allowed":
            if observed != "allowed":
                raise SandboxProbeFailed(probe.name, observed or "no output")
        elif not observed.startswith("denied:"):
            raise SandboxProbeFailed(probe.name, observed or "no output")
        observations.append((probe.name, observed))
    return tuple(observations)


@dataclass(frozen=True)
class SandboxLaunch:
    """One probed profile, and the argv wrapper that applies it."""

    policy: SandboxPolicy
    profile_path: Path
    profile_sha256: str
    probes: tuple[tuple[str, str], ...]

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return [SANDBOX_EXEC, "-f", str(self.profile_path), *argv]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "policy": self.policy.to_dict(),
            "policy_sha256": self.policy.digest,
            "profile_path": str(self.profile_path),
            "profile_sha256": self.profile_sha256,
            "probes": [
                {"probe": name, "observation": observation}
                for name, observation in self.probes
            ],
        }


def prepare_sandbox(
    descriptor: SlotInterpreterDescriptor,
    *,
    descriptor_path: Path,
    hermes_home: Path | None = None,
    egress_port: int = DEFAULT_EGRESS_PORT,
) -> SandboxLaunch:
    """Generate, seal, probe and record the profile for one launch.

    The order is the point: the profile is written and probed *before* anything
    is launched inside it, and a probe that does not deny stops the launch.
    """

    assert_available()
    policy = build_policy(
        descriptor,
        descriptor_path=descriptor_path,
        hermes_home=hermes_home,
        egress_port=egress_port,
    )
    text = render_profile(policy)
    digest = profile_digest(text)
    state_dir = Path(descriptor.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The probes must be able to tell EPERM from ENOENT, so the directory whose
    # contents the profile denies has to exist before they run. `worker_environment`
    # creates it for the same launch; creating it here makes the prober's answer
    # independent of the order the two are called in.
    Path(policy.hermes_home).mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence = state_dir / EVIDENCE_DIRNAME
    # Rewritten every launch and read by nothing but `sandbox-exec`, which reads
    # it as this process, before the profile it describes is in force.
    shutil.rmtree(evidence, ignore_errors=True)
    evidence.mkdir(mode=0o700)
    profile_path = evidence / PROFILE_FILENAME
    profile_path.write_text(text, encoding="utf-8")
    os.chmod(profile_path, 0o600)
    probes = run_probes(policy, profile_path)
    launch = SandboxLaunch(
        policy=policy,
        profile_path=profile_path,
        profile_sha256=digest,
        probes=probes,
    )
    record = evidence / LAUNCH_RECORD_FILENAME
    record.write_text(
        json.dumps(launch.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(record, 0o600)
    return launch
