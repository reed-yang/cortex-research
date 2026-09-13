"""Loopback-only identity gateway between Tailscale Serve and Cortex Web."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import secrets
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import AccessConfig, ConfigError, config_fingerprint, load_config
from .policy import evaluate_request
from .secrets import SecretResolutionError, derive_bootstrap_token, resolve_secret

_MAX_REQUEST_BODY = 1024 * 1024
_RESPONSE_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_SECURITY_CRITICAL_HEADERS = {
    "content-length",
    "host",
    "origin",
    "sec-fetch-site",
    "tailscale-app-capabilities",
    "tailscale-user-login",
    "transfer-encoding",
    "x-cortex-web-client",
}

WebBoundaryProbe = Callable[[AccessConfig, str], bool]
_WEB_ATTESTATION_DOMAIN = "cortex-web-access-attestation-v1"
_WEB_ATTESTATION_PATH = "/api/cortex/access-boundary/health"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _probe_web_boundary(config: AccessConfig, bootstrap_token: str) -> bool:
    """Verify the Web-owned fixed-origin boundary over its loopback challenge."""
    challenge = secrets.token_urlsafe(32)
    origin_fingerprint = _base64url(
        hashlib.sha256(config.public_origin.encode("utf-8")).digest()
    )
    material = (
        f"{_WEB_ATTESTATION_DOMAIN}\0{challenge}\0{origin_fingerprint}"
    ).encode("utf-8")
    expected_attestation = _base64url(
        hmac.new(bootstrap_token.encode("ascii"), material, hashlib.sha256).digest()
    )
    connection = http.client.HTTPConnection(
        config.web_upstream.host,
        config.web_upstream.port,
        timeout=2,
    )
    try:
        connection.request(
            "GET",
            _WEB_ATTESTATION_PATH,
            headers={
                "Host": config.public_hostname,
                "Sec-Fetch-Site": "none",
                "X-Cortex-Access-Attestation-Challenge": challenge,
                "X-Cortex-Access-Bootstrap": bootstrap_token,
                "X-Forwarded-Host": config.public_hostname,
                "X-Forwarded-Proto": "https",
            },
        )
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Cache-Control") != "no-store":
            response.read(8_193)
            return False
        body = response.read(8_193)
        if len(body) > 8_192:
            return False
        payload = json.loads(body)
    except (OSError, UnicodeError, http.client.HTTPException, json.JSONDecodeError):
        return False
    finally:
        connection.close()
    expected = {
        "attestation",
        "challenge",
        "origin_fingerprint",
        "service",
        "status",
        "version",
        "web_access_boundary_verified",
    }
    return (
        isinstance(payload, dict)
        and set(payload) == expected
        and payload["challenge"] == challenge
        and payload["origin_fingerprint"] == origin_fingerprint
        and payload["service"] == "cortex-web-access-boundary"
        and payload["status"] == "ok"
        and type(payload["version"]) is int
        and payload["version"] == 1
        and payload["web_access_boundary_verified"] is True
        and isinstance(payload["attestation"], str)
        and hmac.compare_digest(payload["attestation"], expected_attestation)
    )


class AccessGatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        config: AccessConfig,
        *,
        bootstrap_secret: bytes,
        web_boundary_probe: WebBoundaryProbe | None = None,
    ) -> None:
        self.config = config
        self.bootstrap_token = derive_bootstrap_token(bootstrap_secret)
        super().__init__(
            (config.access_gateway.host, config.access_gateway.port),
            _handler(
                config,
                self.bootstrap_token,
                web_boundary_probe or _probe_web_boundary,
            ),
        )


def _request_headers(handler: BaseHTTPRequestHandler) -> Mapping[str, str] | None:
    result: dict[str, str] = {}
    for name in handler.headers:
        key = name.casefold()
        values = handler.headers.get_all(name, failobj=[])
        if key in _SECURITY_CRITICAL_HEADERS and len(values) != 1:
            return None
        if key in result:
            continue
        result[key] = ", ".join(values)
    return result


def _handler(
    config: AccessConfig,
    bootstrap_token: str,
    web_boundary_probe: WebBoundaryProbe,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "cortex-private-access"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def _problem(self, status: int, category: str) -> None:
            body = json.dumps(
                {
                    "category": category,
                    "owner": "cortex-private-access",
                    "retryable": status >= 500,
                    "status": status,
                    "title": "Private access request rejected",
                    "type": f"urn:cortex:access-problem:{category}",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/problem+json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _health(self) -> bool:
            if self.path != "/.well-known/cortex-private-access/health":
                return False
            hosts = self.headers.get_all("Host", failobj=[])
            if (
                self.client_address[0] not in {"127.0.0.1", "::1"}
                or hosts != [config.access_gateway.authority]
            ):
                self._problem(404, "not_found")
                return True
            try:
                boundary_verified = web_boundary_probe(config, bootstrap_token)
            except Exception:  # noqa: BLE001 - any probe failure is a false attestation
                boundary_verified = False
            body = json.dumps(
                {
                    "config_fingerprint": config_fingerprint(config),
                    "service": "cortex-private-access",
                    "status": "ok",
                    "web_access_boundary_verified": boundary_verified is True,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True

        def _body(self) -> bytes | None:
            if self.headers.get("Transfer-Encoding") is not None:
                return None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return b""
            try:
                length = int(raw_length)
            except ValueError:
                return None
            if length < 0 or length > _MAX_REQUEST_BODY:
                return None
            return self.rfile.read(length)

        def _proxy(self) -> None:
            self.connection.settimeout(15)
            if self.command in {"GET", "HEAD"} and self._health():
                return
            headers = _request_headers(self)
            if headers is None:
                self._problem(400, "duplicate_header")
                return
            decision = evaluate_request(
                method=self.command,
                target=self.path,
                headers=headers,
                client_host=self.client_address[0],
                config=config,
            )
            if not decision.allowed:
                self._problem(decision.status, decision.category)
                return
            try:
                body = self._body()
            except OSError:
                self._problem(408, "request_body_timeout")
                return
            if body is None:
                self._problem(400, "request_body_rejected")
                return

            upstream = http.client.HTTPConnection(
                config.web_upstream.host,
                config.web_upstream.port,
                timeout=30,
            )
            response_started = False
            try:
                upstream.putrequest(
                    self.command,
                    self.path,
                    skip_accept_encoding=True,
                    skip_host=True,
                )
                for name, value in decision.upstream_headers:
                    upstream.putheader(name, value)
                upstream.putheader("x-cortex-access-bootstrap", bootstrap_token)
                if body:
                    upstream.putheader("content-length", str(len(body)))
                upstream.endheaders(body or None)
                response = upstream.getresponse()
                self.send_response(response.status, response.reason)
                response_started = True
                connection_tokens: set[str] = set()
                for name, value in response.getheaders():
                    if name.casefold() == "connection":
                        connection_tokens.update(
                            item.strip().casefold()
                            for item in value.split(",")
                            if item.strip()
                        )
                for name, value in response.getheaders():
                    key = name.casefold()
                    if (
                        key in _RESPONSE_HOP_HEADERS
                        or key in connection_tokens
                        or key in {"strict-transport-security", "x-content-type-options"}
                    ):
                        continue
                    self.send_header(name, value)
                self.send_header("Strict-Transport-Security", "max-age=31536000")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := response.read(65_536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (OSError, http.client.HTTPException):
                if not response_started:
                    self._problem(502, "web_upstream_unavailable")
                elif not self.wfile.closed:
                    self.close_connection = True
            finally:
                upstream.close()

        do_DELETE = _proxy  # noqa: N815
        do_GET = _proxy  # noqa: N815
        do_HEAD = _proxy  # noqa: N815
        do_OPTIONS = _proxy  # noqa: N815
        do_PATCH = _proxy  # noqa: N815
        do_POST = _proxy  # noqa: N815
        do_PUT = _proxy  # noqa: N815

    return Handler


def serve(config: AccessConfig) -> None:
    bootstrap_secret = resolve_secret(config.session_bootstrap_secret_ref)
    server = AccessGatewayServer(config, bootstrap_secret=bootstrap_secret)
    stopped = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        serve(config)
    except (ConfigError, OSError, SecretResolutionError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
