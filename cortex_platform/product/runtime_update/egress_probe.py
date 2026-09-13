"""A token-free egress probe under the managed worker's own seatbelt.

⟦P5.6⟧ During the sixth window the sandboxed worker's connections to
`api.telegram.org` alternated between IPv4 (fine) and IPv6 over the WARP
`utun0` (one sample stuck in `SYN_SENT`), and the poller failed in bursts
whose cause could not be told apart from a shell: `curl -4/-6` from ssh both
reached the host in 0.4 s, but the worker runs inside an SBPL profile that
denies everything but one port, a resolver socket and a handful of read
roots. The only honest measurement is one taken from inside that profile.

This runs a stdlib-only script -- `socket`, `ssl`, `json`, `time`, nothing
the worker does not already use -- with the release's own interpreter under
the profile `sandbox.build_policy` renders for the ACTIVE descriptor, and
asks, for IPv4 and IPv6 explicitly, how long a connect, a TLS handshake and
a first response byte take to the transport's host. No token is involved:
the request is `HEAD /` with no bot path, so the worst the probe can reveal
is that the host answers.

`cortex runtime probe-egress` is the command. It lives beside
`assert-transport-capability` rather than under `doctor` because it needs
what that command needs and `doctor` never does: an ACTIVE release, its
interpreter, its generated profile and outbound network -- all outside any
window.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from ..redaction import redact
from .sandbox import (
    EVIDENCE_DIRNAME,
    LAUNCH_RECORD_FILENAME,
    SANDBOX_EXEC,
    SandboxUnsupported,
    assert_available,
    build_policy,
    profile_digest,
    render_profile,
)
from .service import RuntimeUpdateService
from .worker_launch import (
    WorkerLaunchError,
    build_active_descriptor,
    descriptor_document,
)

#: The transport's production host and the one port the seatbelt admits.
DEFAULT_HOST = "api.telegram.org"
DEFAULT_SCHEME = "https"
DEFAULT_PORT = 443
#: The same override the worker honours (`cortex_worker.telegram.BASE_URL_ENV`),
#: so an acceptance points the probe and the worker at the same stand-in.
BASE_URL_ENV = "TELEGRAM_API_BASE_URL"
#: Per family. Long enough to see a SYN that never completes, short enough
#: that both families answer inside the minute an operator will wait.
DEFAULT_CAP_SECONDS = 15.0
_INSTANT = "%Y-%m-%dT%H:%M:%SZ"
_STDERR_LIMIT = 400

#: Runs under `python -I -B -c` on the release's interpreter (CPython 3.11),
#: so nothing here may need a newer syntax or a third-party module. It writes
#: exactly one JSON document to stdout and never a byte anywhere else.
PROBE_SCRIPT = r'''
import json, socket, ssl, sys, time

host, port, scheme, cap = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
NAMES = {socket.AF_INET: "ipv4", socket.AF_INET6: "ipv6"}


def ms(since):
    return int((time.monotonic() - since) * 1000)


def problem(exc):
    return {"type": type(exc).__name__, "detail": str(exc)[:200]}


def listing():
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return {"ok": False, "error": problem(exc), "addresses": []}
    seen, addresses = set(), []
    for family, _kind, _proto, _name, sockaddr in infos:
        entry = (NAMES.get(family, str(family)), sockaddr[0])
        if entry not in seen:
            seen.add(entry)
            addresses.append({"family": entry[0], "address": entry[1]})
    return {"ok": True, "addresses": addresses}


def probe(family):
    result = {
        "family": NAMES[family], "status": None, "address": None, "v4_mapped": False,
        "phase": None, "connect_ms": None, "tls_ms": None, "first_byte_ms": None,
        "status_line": None, "elapsed_ms": None, "error": None,
    }
    started = time.monotonic()
    deadline = started + cap

    def remaining():
        return max(0.05, deadline - time.monotonic())

    result["phase"] = "resolve"
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except OSError as exc:
        result.update(status="unresolved", error=problem(exc), elapsed_ms=ms(started))
        return result
    if not infos:
        result.update(status="unresolved", elapsed_ms=ms(started))
        return result
    sockaddr = infos[0][4]
    result["address"] = sockaddr[0]
    # Darwin answers an AF_INET6 lookup of a v4 literal with `::ffff:a.b.c.d`,
    # which travels over IPv4 -- an "ok" here says nothing about the v6 path.
    result["v4_mapped"] = family == socket.AF_INET6 and sockaddr[0].startswith("::ffff:")
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        result["phase"] = "connect"
        sock.settimeout(remaining())
        clock = time.monotonic()
        sock.connect(sockaddr)
        result["connect_ms"] = ms(clock)
        if scheme == "https":
            result["phase"] = "tls"
            sock.settimeout(remaining())
            clock = time.monotonic()
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            result["tls_ms"] = ms(clock)
        result["phase"] = "request"
        header_host = "[%s]" % host if ":" in host else host
        request = (
            "HEAD / HTTP/1.1\r\nHost: %s\r\nUser-Agent: cortex-egress-probe/1\r\n"
            "Connection: close\r\n\r\n" % header_host
        ).encode("ascii")
        sock.settimeout(remaining())
        clock = time.monotonic()
        sock.sendall(request)
        result["phase"] = "first_byte"
        data = sock.recv(512)
        result["first_byte_ms"] = ms(clock)
        result["status_line"] = data.split(b"\r\n", 1)[0].decode("latin-1")[:80]
        result["status"] = "ok" if data else "error"
        if not data:
            result["error"] = {"type": "EmptyResponse", "detail": "the peer closed without a byte"}
    except TimeoutError as exc:
        result.update(status="timed_out", error=problem(exc))
    except OSError as exc:
        result.update(status="error", error=problem(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    result["elapsed_ms"] = ms(started)
    return result


sys.stdout.write(json.dumps({
    "host": host, "port": port, "scheme": scheme, "cap_seconds": cap,
    "getaddrinfo": listing(),
    "families": {"ipv4": probe(socket.AF_INET), "ipv6": probe(socket.AF_INET6)},
}))
sys.stdout.write("\n")
sys.stdout.flush()
'''


class EgressProbeError(RuntimeError):
    """The probe could not be run at all (as opposed to a family failing)."""


def egress_target(
    environ: Mapping[str, str], *, host: str | None = None
) -> dict[str, object]:
    """Where the worker would connect: the override it honours, or production."""

    scheme, target_host, port = DEFAULT_SCHEME, DEFAULT_HOST, DEFAULT_PORT
    base = environ.get(BASE_URL_ENV)
    if base:
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise EgressProbeError(f"{BASE_URL_ENV} names no endpoint this can probe")
        scheme = parsed.scheme
        target_host = parsed.hostname
        try:
            port = parsed.port or (80 if scheme == "http" else 443)
        except ValueError as exc:
            raise EgressProbeError(f"{BASE_URL_ENV} port is not readable") from exc
    if host:
        target_host = host
    return {"scheme": scheme, "host": target_host, "port": int(port)}


def advisory(probe: Mapping[str, object]) -> str | None:
    """One sentence when one family stalls or fails while the other answers.

    `unresolved` is deliberately not a stall: a host with no AAAA record is
    the network's answer, not a path that swallowed a SYN.
    """

    families = probe.get("families")
    if not isinstance(families, Mapping):
        return None
    results = {
        name: families.get(name)
        for name in ("ipv4", "ipv6")
        if isinstance(families.get(name), Mapping)
    }
    if len(results) != 2:
        return None
    good = [name for name, item in results.items() if item.get("status") == "ok"]
    bad = [
        name
        for name, item in results.items()
        if item.get("status") in {"timed_out", "error"}
    ]
    if len(good) != 1 or len(bad) != 1:
        return None
    ok, failed = results[good[0]], results[bad[0]]
    error = failed.get("error") or {}
    return (
        f"{bad[0]} {failed.get('status')} at {failed.get('phase')} after "
        f"{failed.get('elapsed_ms')} ms ({error.get('type')}) while {good[0]} "
        f"answered in {ok.get('elapsed_ms')} ms via {ok.get('address')}; the "
        f"worker resolves both and takes whichever getaddrinfo lists first, so "
        f"prefer {good[0]} or fix the {bad[0]} path before the next window"
    )


def _matching_descriptor_path(
    candidate: Path | None, descriptor, directory: Path
) -> Path:
    """Reuse the daemon's descriptor path when it describes the same worker.

    The profile digest covers `descriptor_path`, so probing under a copy in a
    temporary directory would render a profile that differs from the worker's
    by exactly one literal. When the daemon's own document is byte-equal the
    real path is used and the digests can agree; otherwise a copy is written.
    """

    document = json.dumps(descriptor_document(descriptor), sort_keys=True)
    if candidate is not None and candidate.is_file() and not candidate.is_symlink():
        try:
            if candidate.read_text(encoding="utf-8") == document:
                return candidate
        except OSError:
            pass
    path = directory / "descriptor.json"
    path.write_text(document, encoding="utf-8")
    path.chmod(0o600)
    return path


def _last_launch_profile(state_dir: Path) -> str | None:
    record = state_dir / EVIDENCE_DIRNAME / LAUNCH_RECORD_FILENAME
    try:
        raw = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = raw.get("profile_sha256") if isinstance(raw, dict) else None
    return str(digest) if isinstance(digest, str) else None


def run_egress_probe(
    service: RuntimeUpdateService,
    *,
    environ: Mapping[str, str],
    host: str | None = None,
    cap_seconds: float = DEFAULT_CAP_SECONDS,
    sandboxed: bool = True,
    descriptor_path: Path | None = None,
    now=lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Probe both address families from inside the worker's profile.

    Returns a record; raises `EgressProbeError` only when nothing could be
    measured (no active release, no `sandbox-exec`, a script that did not
    produce its document). A family that fails is an answer, not an error.
    """

    if not 1.0 <= float(cap_seconds) <= 120.0:
        raise EgressProbeError("the per-family cap must be between 1 and 120 seconds")
    target = egress_target(environ, host=host)
    try:
        descriptor = build_active_descriptor(service)
    except WorkerLaunchError as exc:
        raise EgressProbeError(f"active runtime is unavailable: {exc}") from exc
    if sandboxed:
        try:
            assert_available()
        except SandboxUnsupported as exc:
            raise EgressProbeError(str(exc)) from exc
    directory = Path(tempfile.mkdtemp(prefix="cortex-egress-probe-"))
    try:
        path = _matching_descriptor_path(descriptor_path, descriptor, directory)
        policy = build_policy(
            descriptor, descriptor_path=path, egress_port=int(target["port"])
        )
        text = render_profile(policy)
        digest = profile_digest(text)
        profile = directory / "profile.sb"
        profile.write_text(text, encoding="utf-8")
        profile.chmod(0o600)
        argv = [
            policy.interpreter_path,
            "-I",
            "-B",
            "-c",
            PROBE_SCRIPT,
            str(target["host"]),
            str(target["port"]),
            str(target["scheme"]),
            str(float(cap_seconds)),
        ]
        if sandboxed:
            argv = [SANDBOX_EXEC, "-f", str(profile), *argv]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=2 * float(cap_seconds) + 15.0,
                check=False,
                # The state dir is the one place the profile lets a process
                # write; the probe writes nothing, but CPython may try a
                # cwd-relative read on start-up and must not be refused for it.
                cwd=str(descriptor.state_dir) if Path(descriptor.state_dir).is_dir() else None,
                env={"PATH": os.defpath},
            )
        except subprocess.TimeoutExpired as exc:
            raise EgressProbeError("the probe process did not finish") from exc
        try:
            probe = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise EgressProbeError(
                "the probe produced no document (exit "
                f"{completed.returncode}): {redact(completed.stderr, limit=_STDERR_LIMIT)}"
            ) from exc
    finally:
        for child in directory.iterdir():
            child.unlink(missing_ok=True)
        directory.rmdir()
    last = _last_launch_profile(Path(descriptor.state_dir))
    return {
        "schema_version": 1,
        "probed_at": now().strftime(_INSTANT),
        "release_id": descriptor.release_id,
        "slot": descriptor.slot_id,
        "target": target,
        "sandboxed": bool(sandboxed),
        "interpreter": policy.interpreter_path,
        "profile_sha256": digest,
        "policy_sha256": policy.digest,
        "last_launch_profile_sha256": last,
        "matches_last_launch": None if last is None else last == digest,
        "probe": probe,
        "advisory": advisory(probe),
    }
