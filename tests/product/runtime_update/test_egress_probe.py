"""⟦P5.6⟧ The token-free egress probe, from inside the worker's own profile.

The sixth window's poller failed in bursts and the network question -- does
the sandboxed worker's IPv6 path stall while IPv4 works? -- could not be
answered from a shell, because a shell is not inside the seatbelt. These pin
the script, the report, the advisory and the sandboxed launch, all against a
loopback server; no request leaves the machine and no token exists.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.egress_probe import (
    BASE_URL_ENV,
    DEFAULT_HOST,
    PROBE_SCRIPT,
    EgressProbeError,
    advisory,
    egress_target,
    run_egress_probe,
)
from cortex_platform.product.runtime_update.sandbox import SANDBOX_EXEC
from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    SlotInterpreterDescriptor,
)

DIGEST = "a" * 64


class _Head(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def loopback():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Head)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class _QuietTLSServer(ThreadingHTTPServer):
    """The probe aborts the handshake on purpose (self-signed); keep the
    server's traceback for that off the test output."""

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        return


@pytest.fixture
def tls_loopback(tmp_path: Path):
    """⟦Batch F P56-05⟧ A loopback HTTPS server with a self-signed certificate
    for 127.0.0.1 -- the branch the runbook prescribes (`HEAD https://...`)
    had never executed anywhere."""

    openssl = "/usr/bin/openssl"
    if not os.path.isfile(openssl):
        pytest.skip("openssl is required to mint the self-signed certificate")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    command = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
    ]
    minted = subprocess.run(
        [*command, "-addext", "subjectAltName=IP:127.0.0.1"], capture_output=True
    )
    if minted.returncode != 0:
        subprocess.run(command, check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server = _QuietTLSServer(("127.0.0.1", 0), _Head)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _script(host: str, port: int, scheme: str = "http", cap: float = 5.0) -> dict:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", PROBE_SCRIPT, host, str(port), scheme, str(cap)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def test_the_script_reports_both_families_and_the_listing(loopback: int) -> None:
    report = _script("127.0.0.1", loopback)
    assert set(report["families"]) == {"ipv4", "ipv6"}
    ipv4 = report["families"]["ipv4"]
    assert ipv4["status"] == "ok"
    assert ipv4["address"] == "127.0.0.1"
    assert ipv4["status_line"].startswith("HTTP/1.0 204")
    assert ipv4["connect_ms"] is not None and ipv4["first_byte_ms"] is not None
    assert ipv4["tls_ms"] is None  # plain http: no handshake to time
    # A v4 literal either has no v6 address or is answered v4-mapped
    # (`::ffff:127.0.0.1`), which Darwin does; either way it is the network's
    # answer, not a stall, and a mapped address is flagged as not-really-v6.
    ipv6 = report["families"]["ipv6"]
    assert ipv6["status"] in {"unresolved", "error", "ok"}
    if ipv6["status"] == "ok":
        assert ipv6["v4_mapped"] is True and ipv6["address"] == "::ffff:127.0.0.1"
    assert report["getaddrinfo"]["ok"] is True
    assert {"family": "ipv4", "address": "127.0.0.1"} in report["getaddrinfo"]["addresses"]


def test_the_tls_path_attempts_the_handshake_and_reports_its_phase(
    tls_loopback: int,
) -> None:
    """Over `https` the probe connects, then wraps the socket with the default
    context; a self-signed peer fails verification, which is the proof that
    the handshake was attempted: phase `tls`, connect timed, tls not."""

    report = _script("127.0.0.1", tls_loopback, scheme="https")
    assert report["scheme"] == "https"
    ipv4 = report["families"]["ipv4"]
    assert ipv4["status"] == "error"
    assert ipv4["phase"] == "tls"
    assert isinstance(ipv4["connect_ms"], (int, float))
    assert ipv4["tls_ms"] is None
    assert ipv4["first_byte_ms"] is None
    assert "CERTIFICATE_VERIFY_FAILED" in json.dumps(ipv4["error"])


def test_a_peer_that_never_answers_is_a_timeout_at_first_byte() -> None:
    silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        report = _script("127.0.0.1", silent.getsockname()[1], cap=1.0)
    finally:
        silent.close()
    ipv4 = report["families"]["ipv4"]
    assert ipv4["status"] == "timed_out"
    assert ipv4["phase"] == "first_byte"
    assert ipv4["connect_ms"] is not None
    assert 900 <= ipv4["elapsed_ms"] <= 3000


def test_the_advisory_names_the_family_that_stalled_while_the_other_answered() -> None:
    probe = {
        "families": {
            "ipv4": {"status": "ok", "elapsed_ms": 412, "address": "149.154.166.110"},
            "ipv6": {
                "status": "timed_out",
                "phase": "connect",
                "elapsed_ms": 15000,
                "error": {"type": "TimeoutError", "detail": "timed out"},
            },
        }
    }
    text = advisory(probe)
    assert text is not None
    assert text.startswith("ipv6 timed_out at connect after 15000 ms (TimeoutError)")
    assert "ipv4 answered in 412 ms via 149.154.166.110" in text


def test_no_advisory_when_both_answer_or_one_is_merely_unresolved() -> None:
    both = {"families": {"ipv4": {"status": "ok"}, "ipv6": {"status": "ok"}}}
    assert advisory(both) is None
    unresolved = {"families": {"ipv4": {"status": "ok"}, "ipv6": {"status": "unresolved"}}}
    assert advisory(unresolved) is None
    neither = {"families": {"ipv4": {"status": "error"}, "ipv6": {"status": "timed_out"}}}
    assert advisory(neither) is None


def test_the_target_is_the_workers_own_override_or_production() -> None:
    assert egress_target({}) == {"scheme": "https", "host": DEFAULT_HOST, "port": 443}
    assert egress_target({BASE_URL_ENV: "http://127.0.0.1:5123"}) == {
        "scheme": "http",
        "host": "127.0.0.1",
        "port": 5123,
    }
    assert egress_target({BASE_URL_ENV: "https://example.test"}, host="localhost") == {
        "scheme": "https",
        "host": "localhost",
        "port": 443,
    }
    with pytest.raises(EgressProbeError):
        egress_target({BASE_URL_ENV: "ftp://nowhere"})


def _descriptor(tmp_path: Path) -> SlotInterpreterDescriptor:
    slot = tmp_path / "slots" / DIGEST
    (slot / "content").mkdir(parents=True)
    state = tmp_path / "worker-state"
    state.mkdir()
    return SlotInterpreterDescriptor(
        schema_version=1,
        slot_path=slot,
        slot_id="primary",
        state_generation_id="generation-1",
        release_id="hermes-0.15.0-test",
        expected_artifact_digest=DIGEST,
        expected_manifest_sha256="b" * 64,
        expected_content_tree_sha256="c" * 64,
        expected_interpreter_sha256="d" * 64,
        interpreter_path=Path(sys.executable),
        worker_entrypoint="runtime_worker.py",
        state_dir=state,
        worker_protocol=PROTOCOL_V2,
    )


def _patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SlotInterpreterDescriptor:
    descriptor = _descriptor(tmp_path)
    monkeypatch.setattr(
        "cortex_platform.product.runtime_update.egress_probe.build_active_descriptor",
        lambda service: descriptor,
    )
    return descriptor


def test_the_report_carries_the_profile_the_worker_would_run_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loopback: int
) -> None:
    from cortex_platform.product.runtime_update.sandbox import (
        build_policy,
        profile_digest,
        render_profile,
    )

    descriptor = _patched(monkeypatch, tmp_path)
    report = run_egress_probe(
        object(),  # type: ignore[arg-type]
        environ={BASE_URL_ENV: f"http://127.0.0.1:{loopback}"},
        cap_seconds=5.0,
        sandboxed=False,
    )
    assert report["target"] == {"scheme": "http", "host": "127.0.0.1", "port": loopback}
    assert report["sandboxed"] is False
    assert report["release_id"] == "hermes-0.15.0-test"
    assert report["probe"]["families"]["ipv4"]["status"] == "ok"
    assert report["advisory"] is None
    assert report["matches_last_launch"] is None
    # The daemon's own descriptor path, when it holds the same document, is
    # what the profile names -- and then the digest equals the worker's.
    from cortex_platform.product.runtime_update.worker_launch import descriptor_document

    daemon_path = tmp_path / "state" / "managed-worker" / "descriptor.json"
    daemon_path.parent.mkdir(parents=True)
    daemon_path.write_text(json.dumps(descriptor_document(descriptor), sort_keys=True))
    expected = profile_digest(
        render_profile(build_policy(descriptor, descriptor_path=daemon_path, egress_port=loopback))
    )
    launch = descriptor.state_dir / ".cortex-sandbox"
    launch.mkdir()
    (launch / "launch.json").write_text(json.dumps({"profile_sha256": expected}))
    again = run_egress_probe(
        object(),  # type: ignore[arg-type]
        environ={BASE_URL_ENV: f"http://127.0.0.1:{loopback}"},
        cap_seconds=5.0,
        sandboxed=False,
        descriptor_path=daemon_path,
    )
    assert again["profile_sha256"] == expected
    assert again["matches_last_launch"] is True


def test_localhost_yields_an_advisory_when_only_ipv4_listens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loopback: int
) -> None:
    _patched(monkeypatch, tmp_path)
    report = run_egress_probe(
        object(),  # type: ignore[arg-type]
        environ={BASE_URL_ENV: f"http://127.0.0.1:{loopback}"},
        host="localhost",
        cap_seconds=5.0,
        sandboxed=False,
    )
    families = report["probe"]["families"]
    assert families["ipv4"]["status"] == "ok"
    assert families["ipv6"]["status"] == "error"
    assert families["ipv6"]["address"] in {"::1", "fe80::1%lo0"}
    assert report["advisory"] is not None
    assert report["advisory"].startswith("ipv6 error at connect")


def test_no_active_release_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex_platform.product.runtime_update.worker_launch import WorkerLaunchError

    def unavailable(service):
        raise WorkerLaunchError("active runtime is unavailable")

    monkeypatch.setattr(
        "cortex_platform.product.runtime_update.egress_probe.build_active_descriptor",
        unavailable,
    )
    with pytest.raises(EgressProbeError, match="active runtime"):
        run_egress_probe(object(), environ={}, sandboxed=False)  # type: ignore[arg-type]


@pytest.mark.skipif(
    platform.system() != "Darwin" or not os.path.isfile(SANDBOX_EXEC),
    reason="the managed worker sandbox is a Darwin sandbox-exec profile",
)
def test_the_probe_runs_inside_the_real_seatbelt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loopback: int
) -> None:
    """The whole point: measured from inside the profile, with the port the
    profile admits. The profile is rendered for the target's port, so a second
    loopback on another port is admitted too; that the seatbelt denies a port
    it was NOT rendered for is the sandbox module's own contract, pinned in
    its tests, not here."""

    _patched(monkeypatch, tmp_path)
    report = run_egress_probe(
        object(),  # type: ignore[arg-type]
        environ={BASE_URL_ENV: f"http://127.0.0.1:{loopback}"},
        cap_seconds=5.0,
        sandboxed=True,
    )
    assert report["sandboxed"] is True
    assert report["probe"]["families"]["ipv4"]["status"] == "ok"

    # A second loopback on another port: the profile is rendered for the
    # target, so this one is admitted as well.
    other = ThreadingHTTPServer(("127.0.0.1", 0), _Head)
    other.daemon_threads = True
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        again = run_egress_probe(
            object(),  # type: ignore[arg-type]
            environ={BASE_URL_ENV: f"http://127.0.0.1:{loopback}"},
            host=None,
            cap_seconds=5.0,
            sandboxed=True,
        )
        assert again["probe"]["families"]["ipv4"]["status"] == "ok"
        other_report = run_egress_probe(
            object(),  # type: ignore[arg-type]
            environ={BASE_URL_ENV: f"http://127.0.0.1:{other.server_address[1]}"},
            cap_seconds=5.0,
            sandboxed=True,
        )
    finally:
        other.shutdown()
        other.server_close()
    # The port scoping follows the endpoint, exactly as the worker's does.
    assert other_report["probe"]["families"]["ipv4"]["status"] == "ok"
    assert other_report["target"]["port"] == other.server_address[1]


@pytest.mark.skipif(
    platform.system() != "Darwin" or not os.path.isfile(SANDBOX_EXEC),
    reason="the real seatbelt is Darwin's sandbox-exec",
)
def test_the_tls_handshake_is_attempted_from_inside_the_real_seatbelt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls_loopback: int
) -> None:
    """⟦Batch F P56-05⟧ The runbook prescribes `HEAD https://api.telegram.org/`
    from inside the worker's profile. Under the real seatbelt the default
    context is built, the socket is wrapped and the self-signed peer fails
    VERIFICATION -- not a read denial, not an import failure: the actual
    outcome of the TLS branch under the generated profile."""

    _patched(monkeypatch, tmp_path)
    report = run_egress_probe(
        object(),  # type: ignore[arg-type]
        environ={BASE_URL_ENV: f"https://127.0.0.1:{tls_loopback}"},
        cap_seconds=5.0,
        sandboxed=True,
    )
    assert report["sandboxed"] is True
    assert report["target"]["scheme"] == "https"
    ipv4 = report["probe"]["families"]["ipv4"]
    assert ipv4["phase"] == "tls"
    assert ipv4["status"] == "error"
    assert isinstance(ipv4["connect_ms"], (int, float))
    assert "CERTIFICATE_VERIFY_FAILED" in json.dumps(ipv4["error"]), ipv4["error"]


def test_the_cli_prints_the_report_and_the_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from cortex_platform.product.cli import main as cli_main

    seen: dict[str, object] = {}

    def fake(service, *, environ, host, cap_seconds, sandboxed, descriptor_path):
        seen.update(host=host, cap=cap_seconds, sandboxed=sandboxed, path=descriptor_path)
        return {"schema_version": 1, "advisory": "ipv6 timed_out at connect", "probe": {}}

    monkeypatch.setattr(
        "cortex_platform.product.runtime_update.cli.run_egress_probe", fake
    )
    home = tmp_path / "home"
    home.mkdir()
    code = cli_main(
        ["runtime", "probe-egress", "--host", "localhost", "--timeout", "3"],
        environ={"HOME": str(home)},
        platform="darwin",
    )
    out, err = capsys.readouterr()
    assert code == 0
    assert json.loads(out)["advisory"] == "ipv6 timed_out at connect"
    assert err.strip() == "advisory: ipv6 timed_out at connect"
    assert seen["host"] == "localhost" and seen["cap"] == 3.0 and seen["sandboxed"] is True
    assert str(seen["path"]).endswith("State/managed-worker/descriptor.json")


def test_the_cli_reports_a_probe_it_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from cortex_platform.product.cli import main as cli_main

    def fail(service, **kwargs):
        raise EgressProbeError("active runtime is unavailable: no active release")

    monkeypatch.setattr(
        "cortex_platform.product.runtime_update.cli.run_egress_probe", fail
    )
    home = tmp_path / "home"
    home.mkdir()
    code = cli_main(
        ["runtime", "probe-egress"], environ={"HOME": str(home)}, platform="darwin"
    )
    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert "active runtime is unavailable" in err
