"""A loopback stand-in for `api.telegram.org`, for tests and acceptance.

No test and no acceptance in this repository is allowed to reach the real Bot
API: a send is not reversible and the operator's research bot is a live
account. The worker's base URL is an allowlisted worker environment key the
product sets, so pointing it at this server is the only injection point, and
it is not reachable from any operator-facing configuration -- the production
default is the real `https://api.telegram.org` and `cortex_worker.telegram`
refuses plain HTTP for anything but loopback.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Obviously fake, and shaped like a token only so the URL path splits the same
#: way. Nothing here ever reaches a real Telegram account.
FAKE_TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"


#: The largest `timeout` this stand-in will honour, whatever a caller asks for.
#: The real Bot API caps `getUpdates` at 50 s and the worker at 30; this exists
#: so a unit test that mis-scripts a timeout stalls a suite for at most this
#: long instead of for ever.
MAX_LONG_POLL_SECONDS = 60.0


class FakeBotAPI:
    """Records every call and replays a scripted queue of responses.

    ⟦P5.5⟧ `getUpdates` LONG-POLLS: it holds the request for the `timeout` the
    caller asked for, or until an update is injected, whichever comes first --
    which is what the real API does and what every stand-in in this repository
    used to get wrong. Answering instantly made poll and send strictly
    alternate, so no acceptance could ever observe a send issued into an open
    poll, which is precisely the collision that killed the certified worker on
    the operator's first real round trip.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.send_responses: list[dict[str, Any]] = []
        #: Evidence about concurrency, not about content. `max_open_polls > 1`
        #: is the 409 the whole window procedure exists to avoid;
        #: `sends_during_open_poll > 0` is the defect P5.5 fixes.
        self.open_polls = 0
        self.max_open_polls = 0
        self.sends_during_open_poll = 0
        self.poll_durations: list[float] = []
        #: Scripted provider misbehaviour, one entry per call, consumed in
        #: order: a slow answer, a connection dropped without a reply, or a
        #: body the worker cannot use. These are the shapes that decide whether
        #: a send is `retryable_before_send` or `outcome_unknown`, and the
        #: acceptance had none of them until P5-01.
        self.behaviours: list[tuple[str, str, Any]] = []
        self.next_message_id = 1000
        self.poll_delay = 0.0
        #: A Condition, not a Lock: a parked `getUpdates` has to release the
        #: mutex while it waits or `queue_update` could never wake it.
        self._lock = threading.Condition()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> str:
        handler = _handler_for(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-bot-api", daemon=True
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeBotAPI":
        self.base_url = self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- scripting --------------------------------------------------------

    def queue_update(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.updates.append(update)
            # Wakes a parked long poll immediately, exactly as an update
            # arriving at Telegram ends a real `getUpdates`.
            self._lock.notify_all()

    def queue_send_response(self, response: dict[str, Any]) -> None:
        with self._lock:
            self.send_responses.append(response)

    def queue_hang(self, seconds: float, *, method: str = "sendMessage") -> None:
        """Accept the request, then answer only after `seconds`.

        The request reached the socket and Telegram may well have acted on it,
        so a caller that times out here knows nothing about the outcome.
        """

        with self._lock:
            self.behaviours.append(("hang", method, seconds))

    def queue_drop(self, *, method: str = "sendMessage") -> None:
        """Accept the request and close the connection without answering."""

        with self._lock:
            self.behaviours.append(("drop", method, None))

    def queue_raw_body(self, body: bytes, *, method: str = "sendMessage") -> None:
        """Answer 200 with bytes the worker cannot parse (or cannot hold)."""

        with self._lock:
            self.behaviours.append(("raw", method, body))

    def queue_stall(
        self, seconds: float, *, method: str = "getUpdates", interval: float = 3.0
    ) -> None:
        """⟦P5.6⟧ Answer 200 and then DRIP the response for `seconds`.

        The status line goes out at once, one header every `interval`, and
        the body only at the end. Every read the worker's `urllib` performs
        therefore returns inside its own socket timeout, so the worker's
        deadline never fires and the PRODUCT's frame deadline does -- the
        shape of a network path that stalls without ever quite dying, which
        is what the sixth window on the mini looked like: `worker response
        timed out` from the product, then `operation_conflict` from a worker
        still inside the first call.
        """

        with self._lock:
            self.behaviours.append(("stall", method, (float(seconds), float(interval))))

    def _take_behaviour(self, method: str) -> tuple[str, Any] | None:
        with self._lock:
            for index, (kind, target, value) in enumerate(self.behaviours):
                if target == method:
                    del self.behaviours[index]
                    return kind, value
        return None

    def sent(self) -> list[dict[str, Any]]:
        with self._lock:
            return [call for call in self.calls if call["method"] == "sendMessage"]

    def polled(self) -> list[dict[str, Any]]:
        with self._lock:
            return [call for call in self.calls if call["method"] == "getUpdates"]

    # -- handling ---------------------------------------------------------

    def handle(self, token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.calls.append({"token": token, "method": method, "payload": payload})
            if method == "sendMessage":
                if self.open_polls:
                    # The exact overlap the certified worker refuses. Recorded
                    # rather than refused here: this stand-in is the provider,
                    # and the product's own serialization is what the
                    # acceptance is measuring.
                    self.sends_during_open_poll += 1
                if self.send_responses:
                    return self.send_responses.pop(0)
                self.next_message_id += 1
                return {"ok": True, "result": {"message_id": self.next_message_id}}
            if method == "getUpdates":
                return self._long_poll(payload)
        return {"ok": False, "error_code": 404, "description": "unknown method"}

    def _long_poll(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hold the request open, under the lock's discipline. Caller holds it."""

        requested = payload.get("timeout")
        seconds = float(requested) if isinstance(requested, (int, float)) else 0.0
        seconds = max(0.0, min(seconds, MAX_LONG_POLL_SECONDS))
        started = time.monotonic()
        deadline = started + seconds
        self.open_polls += 1
        self.max_open_polls = max(self.max_open_polls, self.open_polls)
        try:
            while not self.updates:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
            drained = list(self.updates)
            self.updates = []
        finally:
            self.open_polls -= 1
            self.poll_durations.append(time.monotonic() - started)
        return {"ok": True, "result": drained}


def drip_response(
    handler: BaseHTTPRequestHandler,
    api: FakeBotAPI,
    document: dict[str, Any],
    seconds: float,
    interval: float,
) -> None:
    """Write a 200 reply one header at a time until `seconds` have passed.

    Shared with acceptance drivers that serve the Bot API on a handler of
    their own. The poll counts as open for the whole drip, so the
    `sends_during_open_poll` tripwire keeps its meaning while the worker is
    stuck inside the call.
    """

    body = json.dumps(document).encode("utf-8")
    started = time.monotonic()
    with api._lock:  # noqa: SLF001 - the stand-in's own accounting
        api.open_polls += 1
        api.max_open_polls = max(api.max_open_polls, api.open_polls)
    try:
        handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
        handler.wfile.flush()
        header = 0
        while time.monotonic() - started < seconds:
            time.sleep(min(interval, max(0.0, seconds - (time.monotonic() - started))))
            header += 1
            handler.wfile.write(f"X-Stall: {header}\r\n".encode("ascii"))
            handler.wfile.flush()
        handler.wfile.write(
            b"Content-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        handler.wfile.flush()
    except OSError:
        pass
    finally:
        with api._lock:  # noqa: SLF001
            api.open_polls -= 1
            api.poll_durations.append(time.monotonic() - started)
    handler.close_connection = True


def _handler_for(api: FakeBotAPI) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: object) -> None:  # keep the test output clean
            return

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or not parts[0].startswith("bot"):
                self.send_error(404)
                return
            token = parts[0][3:]
            behaviour = api._take_behaviour(parts[1])
            document = api.handle(token, parts[1], payload)
            if behaviour is not None:
                kind, value = behaviour
                if kind == "drop":
                    self.close_connection = True
                    return
                if kind == "raw":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(value)))
                    self.end_headers()
                    self.wfile.write(value)
                    return
                if kind == "hang":
                    # The caller has already given up by the time this returns.
                    time.sleep(value)
                    self.close_connection = True
                    return
                if kind == "stall":
                    drip_response(self, api, document, *value)
                    return
            body = json.dumps(document).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                # The client gave up first. Nothing here is a test failure --
                # the point of the scripted misbehaviour is that it does.
                self.close_connection = True

    return _Handler
