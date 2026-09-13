"""A loopback arXiv stand-in served from recorded files.

The engine's endpoints are bound (`CORTEX_ARXIV_*_BASE`), so a real ingest can
run against recorded bytes without reaching the network at all. This is the
whole reason those three bindings exist: an acceptance that mocks the engine
proves nothing, and an acceptance that calls arxiv.org is not reproducible.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "arxiv"


class ArxivFixtureServer:
    """Serve `/api/query`, `/html/<id>` and `/pdf/<id>` from recorded files."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or FIXTURE_ROOT)
        self.requests: list[str] = []
        # Held on the full-text response only, so a test can keep a real child
        # genuinely in flight instead of racing a sub-second ingest.
        self.stall_seconds = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:  # silence the default log
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                outer.requests.append(self.path)
                parts = urlsplit(self.path)
                if parts.path.endswith("/api/query"):
                    identifiers = parse_qs(parts.query).get("id_list", [""])
                    source = outer.root / f"{identifiers[0]}.atom.xml"
                    if not source.is_file():
                        self._send(404, b"not found", "text/plain")
                        return
                    self._send(200, source.read_bytes(), "application/atom+xml")
                    return
                kind, _, identifier = parts.path.strip("/").partition("/")
                if kind == "html":
                    if outer.stall_seconds:
                        time.sleep(outer.stall_seconds)
                    source = outer.root / f"{identifier}.html"
                    if not source.is_file():
                        # No LaTeXML HTML: exactly the arXiv-without-HTML case
                        # AMD-4's strict refusal exists for.
                        self._send(404, b"no html", "text/plain")
                        return
                    self._send(200, source.read_bytes(), "text/html; charset=utf-8")
                    return
                if kind == "pdf":
                    source = outer.root / f"{identifier}.pdf"
                    if not source.is_file():
                        self._send(404, b"no pdf", "text/plain")
                        return
                    self._send(200, source.read_bytes(), "application/pdf")
                    return
                self._send(404, b"not found", "text/plain")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> ArxivFixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def literal_overrides(self) -> Mapping[str, str]:
        """The binding slots that point the engine at this server."""

        return {
            "arxiv_api_base": f"{self.base}/api/query",
            "arxiv_html_base": f"{self.base}/html",
            "arxiv_pdf_base": f"{self.base}/pdf",
            "arxiv_interval": "0",
        }
