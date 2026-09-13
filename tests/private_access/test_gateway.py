from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from deployment.private_access.config import (
    AccessConfig,
    LoopbackEndpoint,
    config_fingerprint,
)
from deployment.private_access.gateway import AccessGatewayServer, _probe_web_boundary
from deployment.private_access.secrets import derive_bootstrap_token

_BOOTSTRAP_SECRET = b"test-only-private-access-secret-value"


class RecordingHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, str]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self.requests.append({name.casefold(): value for name, value in self.headers.items()})
        body = b"private cortex web"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AttestingHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, str]]] = []
    tamper = False
    cache_control = "no-store"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        headers = {name.casefold(): value for name, value in self.headers.items()}
        self.requests.append(headers)
        challenge = headers["x-cortex-access-attestation-challenge"]
        origin = f"https://{headers['host']}"
        fingerprint = base64.urlsafe_b64encode(
            hashlib.sha256(origin.encode()).digest()
        ).rstrip(b"=").decode()
        material = (
            f"cortex-web-access-attestation-v1\0{challenge}\0{fingerprint}"
        ).encode()
        attestation = base64.urlsafe_b64encode(
            hmac.new(
                headers["x-cortex-access-bootstrap"].encode(),
                material,
                hashlib.sha256,
            ).digest()
        ).rstrip(b"=").decode()
        if self.tamper:
            attestation = "A" * 43
        body = json.dumps(
            {
                "attestation": attestation,
                "challenge": challenge,
                "origin_fingerprint": fingerprint,
                "service": "cortex-web-access-boundary",
                "status": "ok",
                "version": 1,
                "web_access_boundary_verified": True,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", self.cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _identity_headers(config: AccessConfig) -> dict[str, str]:
    return {
        "Host": config.public_hostname,
        "Tailscale-User-Login": "owner@example.com",
        "Tailscale-App-Capabilities": json.dumps(
            {config.identity.app_capability: [{"role": "owner"}]}
        ),
        "Sec-Fetch-Site": "none",
        "X-Cortex-Access-Bootstrap": "attacker-controlled",
    }


def test_gateway_exposes_only_guarded_web_upstream(config: AccessConfig) -> None:
    RecordingHandler.requests.clear()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    protected = replace(
        config,
        access_gateway=LoopbackEndpoint("127.0.0.1", 0),
        web_upstream=LoopbackEndpoint("127.0.0.1", upstream.server_port),
    )
    gateway = AccessGatewayServer(protected, bootstrap_secret=_BOOTSTRAP_SECRET)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        health_connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        health_connection.request(
            "GET",
            "/.well-known/cortex-private-access/health",
            headers={"Host": protected.access_gateway.authority},
        )
        health_response = health_connection.getresponse()
        health = json.loads(health_response.read())
        health_connection.close()
        assert health_response.status == 200
        assert health == {
            "config_fingerprint": config_fingerprint(protected),
            "service": "cortex-private-access",
            "status": "ok",
            "web_access_boundary_verified": False,
        }
        assert protected.session_bootstrap_secret_ref not in json.dumps(health)

        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        headers = _identity_headers(protected)
        headers.update(
            {
                "Forwarded": "for=203.0.113.10;proto=http;host=attacker.example",
                "X-Forwarded-Host": "attacker.example",
                "X-Forwarded-Proto": "http",
                "X-Real-IP": "203.0.113.10",
            }
        )
        connection.request("GET", "/health", headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()

        assert response.status == 200
        assert body == b"private cortex web"
        forwarded = RecordingHandler.requests[-1]
        assert forwarded["host"] == protected.public_hostname
        assert forwarded["x-forwarded-host"] == protected.public_hostname
        assert forwarded["x-forwarded-proto"] == "https"
        assert "forwarded" not in forwarded
        assert "x-real-ip" not in forwarded
        assert "tailscale-user-login" not in forwarded
        assert "tailscale-app-capabilities" not in forwarded
        assert forwarded["x-cortex-access-bootstrap"] == derive_bootstrap_token(
            _BOOTSTRAP_SECRET
        )
        assert forwarded["x-cortex-access-bootstrap"] != "attacker-controlled"
    finally:
        gateway.shutdown()
        gateway.server_close()
        upstream.shutdown()
        upstream.server_close()
        gateway_thread.join(timeout=2)
        upstream_thread.join(timeout=2)


def _successful_probe(_config: AccessConfig, _token: str) -> bool:
    return True


def _failing_probe(_config: AccessConfig, _token: str) -> bool:
    raise OSError("test-only probe failure")


@pytest.mark.parametrize(
    ("probe", "expected"),
    [(_successful_probe, True), (_failing_probe, False)],
)
def test_gateway_health_uses_injected_web_boundary_probe_fail_closed(
    config: AccessConfig, probe, expected: bool
) -> None:
    protected = replace(config, access_gateway=LoopbackEndpoint("127.0.0.1", 0))
    gateway = AccessGatewayServer(
        protected,
        bootstrap_secret=_BOOTSTRAP_SECRET,
        web_boundary_probe=probe,
    )
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        connection.request(
            "GET",
            "/.well-known/cortex-private-access/health",
            headers={"Host": protected.access_gateway.authority},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 200
        assert payload["web_access_boundary_verified"] is expected
    finally:
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=2)


@pytest.mark.parametrize(
    ("tamper", "cache_control", "expected"),
    [
        (False, "no-store", True),
        (True, "no-store", False),
        (False, "public, max-age=60", False),
    ],
)
def test_web_boundary_probe_verifies_exact_challenge_hmac(
    config: AccessConfig, tamper: bool, cache_control: str, expected: bool
) -> None:
    AttestingHandler.requests.clear()
    AttestingHandler.tamper = tamper
    AttestingHandler.cache_control = cache_control
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), AttestingHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    protected = replace(
        config,
        web_upstream=LoopbackEndpoint("127.0.0.1", upstream.server_port),
    )
    try:
        assert (
            _probe_web_boundary(
                protected, derive_bootstrap_token(_BOOTSTRAP_SECRET)
            )
            is expected
        )
        request = AttestingHandler.requests[-1]
        assert request["host"] == protected.public_hostname
        assert request["x-forwarded-host"] == protected.public_hostname
        assert request["x-forwarded-proto"] == "https"
        assert request["sec-fetch-site"] == "none"
        assert len(request["x-cortex-access-attestation-challenge"]) == 43
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)


def test_gateway_rejects_wrong_host_before_web_upstream(config: AccessConfig) -> None:
    RecordingHandler.requests.clear()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    protected = replace(
        config,
        access_gateway=LoopbackEndpoint("127.0.0.1", 0),
        web_upstream=LoopbackEndpoint("127.0.0.1", upstream.server_port),
    )
    gateway = AccessGatewayServer(protected, bootstrap_secret=_BOOTSTRAP_SECRET)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        headers = _identity_headers(protected)
        headers["Host"] = "attacker.example"
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        connection.request("GET", "/", headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 421
        assert payload["category"] == "host_rejected"
        assert RecordingHandler.requests == []
    finally:
        gateway.shutdown()
        gateway.server_close()
        upstream.shutdown()
        upstream.server_close()
        gateway_thread.join(timeout=2)
        upstream_thread.join(timeout=2)


def test_gateway_rejects_duplicate_identity_header(config: AccessConfig) -> None:
    protected = replace(config, access_gateway=LoopbackEndpoint("127.0.0.1", 0))
    gateway = AccessGatewayServer(protected, bootstrap_secret=_BOOTSTRAP_SECRET)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", protected.public_hostname)
        connection.putheader("Tailscale-User-Login", "owner@example.com")
        connection.putheader("Tailscale-User-Login", "attacker@example.com")
        connection.putheader(
            "Tailscale-App-Capabilities",
            json.dumps(
                {protected.identity.app_capability: [{"role": "owner"}]}
            ),
        )
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 400
        assert payload["category"] == "duplicate_header"
    finally:
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=2)


def test_gateway_sanitizes_unavailable_web_upstream(config: AccessConfig) -> None:
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    unavailable_port = reserved.getsockname()[1]
    reserved.close()
    protected = replace(
        config,
        access_gateway=LoopbackEndpoint("127.0.0.1", 0),
        web_upstream=LoopbackEndpoint("127.0.0.1", unavailable_port),
    )
    gateway = AccessGatewayServer(protected, bootstrap_secret=_BOOTSTRAP_SECRET)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        connection.request("GET", "/", headers=_identity_headers(protected))
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 502
        assert payload["category"] == "web_upstream_unavailable"
        assert str(unavailable_port) not in json.dumps(payload)
    finally:
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=2)
