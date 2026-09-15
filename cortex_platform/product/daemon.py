"""Loopback-only stdlib HTTP daemon for Cortex product-shell demos."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from .api import APIResponse, ControlAPI, EventStreamStart
from .config import (
    initialize,
    load_config,
    telegram_allowed_user_ids,
    telegram_mode,
)
from .transports.bridge import InboundTurnBridge
from .transports.drain import (
    DestinationDirectory,
    TransportDeliveryDrain,
    learning_update_handler,
)
from .transports.managed_worker import (
    ManagedTransportWorker,
    TransportWindowSupervisor,
)
from .transports.worker_rpc import GatedWorkerTransportRPC, build_telegram_adapter
from .control import ControlStore
from .daemon_log import configure_daemon_logging, release_daemon_logging
from .engine.service import ResearchScheduleRunner, start_research_schedules
from .lifecycle import (
    DaemonMetadata,
    _remove_matching_metadata,
    acquire_daemon_lifetime_lock,
    current_process_start_token,
    release_daemon_lifetime_lock,
    write_metadata,
)
from .paths import PathRegistry, resolve_paths

#: A literal, not `__name__`: this module runs as `python -m
#: cortex_platform.product.daemon`, where `__name__` is `__main__` -- a logger
#: outside the `cortex_platform` tree the file handler collects. The first
#: real run under `cortex start` had every poller line and none of the
#: daemon's own; the in-process test could not see it because there the
#: module's name is its import path.
DAEMON_LOGGER = "cortex_platform.product.daemon"
_log = logging.getLogger(DAEMON_LOGGER)


class CortexHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        stop_requested: threading.Event,
        max_event_streams: int,
        event_stream_write_timeout: float,
        event_stream_shutdown_timeout: float,
    ) -> None:
        if not 1 <= max_event_streams <= 1_000:
            raise ValueError("max_event_streams must be between 1 and 1000")
        if not 0 < event_stream_write_timeout <= 60:
            raise ValueError("event_stream_write_timeout must be between 0 and 60")
        if not 0 < event_stream_shutdown_timeout <= 60:
            raise ValueError("event_stream_shutdown_timeout must be between 0 and 60")
        self.stop_requested = stop_requested
        self.max_event_streams = max_event_streams
        self.event_stream_write_timeout = event_stream_write_timeout
        self.event_stream_shutdown_timeout = event_stream_shutdown_timeout
        self._event_stream_condition = threading.Condition()
        self._event_stream_sockets: dict[socket.socket, int] = {}
        super().__init__(server_address, handler)
        self._event_stream_stop_monitor = threading.Thread(
            target=self._close_event_streams_when_stopped,
            name="cortexd-sse-stop-monitor",
            daemon=True,
        )
        self._event_stream_stop_monitor.start()

    def _close_event_streams_when_stopped(self) -> None:
        self.stop_requested.wait()
        self.close_active_event_streams()

    def begin_event_stream(self, connection: socket.socket) -> bool:
        """Register a handler before it writes any streaming response bytes."""

        handler_id = threading.get_ident()
        with self._event_stream_condition:
            if (
                self.stop_requested.is_set()
                or len(self._event_stream_sockets) >= self.max_event_streams
            ):
                return False
            self._event_stream_sockets[connection] = handler_id
            return True

    def end_event_stream(self, connection: socket.socket) -> None:
        with self._event_stream_condition:
            self._event_stream_sockets.pop(connection, None)
            self._event_stream_condition.notify_all()

    @property
    def active_event_stream_count(self) -> int:
        with self._event_stream_condition:
            return len(self._event_stream_sockets)

    def close_active_event_streams(self) -> None:
        """Interrupt every active writer without waiting on a slow client."""

        with self._event_stream_condition:
            active = tuple(self._event_stream_sockets)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def wait_for_event_streams(self, *, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (
            self.event_stream_shutdown_timeout if timeout is None else timeout
        )
        with self._event_stream_condition:
            while self._event_stream_sockets:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._event_stream_condition.wait(timeout=remaining)
            return True

    def request_stop(self) -> None:
        self.stop_requested.set()
        self.close_active_event_streams()

    def shutdown(self) -> None:
        self.request_stop()
        super().shutdown()
        self.wait_for_event_streams()

    def server_close(self) -> None:
        self.request_stop()
        self.wait_for_event_streams()
        self._event_stream_stop_monitor.join(timeout=1.0)
        super().server_close()


def _handler(
    instance_id: str,
    control_token: str,
    stop_requested: threading.Event,
    control_api: ControlAPI | None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "cortexd"
        sys_version = ""

        def _send(self, status: int, payload: dict[str, object], content_type: str) -> None:
            body = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_api(self, response: APIResponse) -> None:
            body = json.dumps(
                response.payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _api_body(self) -> bytes:
            value = self.headers.get("Content-Length", "")
            try:
                length = int(value)
            except ValueError:
                return b""
            if not 0 < length <= 1_048_576:
                return b""
            return self.rfile.read(length)

        def _send_event_stream(self, start: EventStreamStart) -> None:
            server = self.server
            if not isinstance(server, CortexHTTPServer):
                raise RuntimeError("event stream server is unavailable")
            if not server.begin_event_stream(self.connection):
                self._send_api(
                    APIResponse(
                        429,
                        {
                            "category": "rate_limited",
                            "owner": "cortexd",
                            "retryable": True,
                            "status": 429,
                            "title": "Event stream capacity is exhausted",
                            "type": "urn:cortex:problem:rate_limited",
                        },
                        "application/problem+json",
                        (("Retry-After", "1"),),
                    )
                )
                return
            cursor = start.after_cursor
            deadline = time.monotonic() + 25.0
            next_heartbeat = time.monotonic() + 10.0
            try:
                self.connection.settimeout(server.event_stream_write_timeout)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                while not stop_requested.is_set() and time.monotonic() < deadline:
                    events, cursor = control_api.event_stream_batch(
                        after_cursor=cursor
                    )  # type: ignore[union-attr]
                    if events:
                        for event in events:
                            self.wfile.write(
                                control_api.format_sse_event(event)  # type: ignore[union-attr]
                            )
                        self.wfile.flush()
                        next_heartbeat = time.monotonic() + 10.0
                        continue
                    if time.monotonic() >= next_heartbeat:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        next_heartbeat = time.monotonic() + 10.0
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.close_connection = True
                server.end_event_stream(self.connection)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send(
                    200,
                    {
                        "api_mode": "control" if control_api is not None else "demo",
                        "instance_id": instance_id,
                        "service": "cortexd",
                        "status": "ok",
                    },
                    "application/json",
                )
                return
            if (
                control_api is not None
                and urlsplit(self.path).path == "/api/v1/events/stream"
            ):
                prepared = control_api.prepare_event_stream(
                    target=self.path,
                    headers=self.headers,
                    client_host=self.client_address[0],
                )
                if isinstance(prepared, APIResponse):
                    self._send_api(prepared)
                else:
                    self._send_event_stream(prepared)
                return
            if self.path == "/demo":
                self._send(
                    200,
                    {
                        "demo_only": control_api is None,
                        "implemented": {
                            "control_store": control_api is not None,
                            "research_pipeline": False,
                            "runtime": False,
                        },
                    },
                    "application/json",
                )
                return
            if self.path.startswith("/api/v1/") and control_api is not None:
                self._send_api(
                    control_api.handle(
                        method="GET",
                        target=self.path,
                        headers=self.headers,
                        client_host=self.client_address[0],
                    )
                )
                return
            self._send(
                404,
                {
                    "category": "not_found",
                    "owner": "cortexd",
                    "retryable": False,
                    "status": 404,
                    "title": "Local demo endpoint not found",
                },
                "application/problem+json",
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/api/v1/") and control_api is not None:
                self._send_api(
                    control_api.handle(
                        method="POST",
                        target=self.path,
                        headers=self.headers,
                        body=self._api_body(),
                        client_host=self.client_address[0],
                    )
                )
                return
            if self.path != "/__cortex__/shutdown":
                self._send(
                    404,
                    {
                        "category": "not_found",
                        "owner": "cortexd",
                        "retryable": False,
                        "status": 404,
                        "title": "Local control endpoint not found",
                    },
                    "application/problem+json",
                )
                return
            try:
                content_length = int(self.headers.get("Content-Length", ""))
                if not 0 < content_length <= 4096:
                    raise ValueError
                payload = json.loads(self.rfile.read(content_length))
            except (OSError, ValueError):
                self._send(
                    400,
                    {"category": "invalid_control_request", "status": 400},
                    "application/problem+json",
                )
                return
            if (
                not isinstance(payload, dict)
                or self.client_address[0] != "127.0.0.1"
                or self.headers.get("X-Cortex-Instance-ID") != instance_id
                or not hmac.compare_digest(
                    self.headers.get("X-Cortex-Control-Token", ""),
                    control_token,
                )
                or payload.get("instance_id") != instance_id
                or not isinstance(payload.get("control_token"), str)
                or not hmac.compare_digest(payload["control_token"], control_token)
                or set(payload) != {"instance_id", "control_token"}
            ):
                self._send(
                    409,
                    {"category": "instance_identity_mismatch", "status": 409},
                    "application/problem+json",
                )
                return
            self._send(
                202,
                {
                    "instance_id": instance_id,
                    "service": "cortexd",
                    "status": "stopping",
                },
                "application/json",
            )
            server = self.server
            if isinstance(server, CortexHTTPServer):
                server.request_stop()
            else:
                stop_requested.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def create_server(
    *,
    host: str,
    port: int,
    instance_id: str,
    control_token: str,
    stop_requested: threading.Event | None = None,
    control_api: ControlAPI | None = None,
    max_event_streams: int = 8,
    event_stream_write_timeout: float = 1.0,
    event_stream_shutdown_timeout: float = 2.0,
) -> CortexHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("cortexd v0.1 requires the IPv4 loopback address")
    event = stop_requested if stop_requested is not None else threading.Event()
    return CortexHTTPServer(
        (host, port),
        _handler(instance_id, control_token, event, control_api),
        stop_requested=event,
        max_event_streams=max_event_streams,
        event_stream_write_timeout=event_stream_write_timeout,
        event_stream_shutdown_timeout=event_stream_shutdown_timeout,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortexd")
    parser.add_argument("--config-file")
    parser.add_argument("--config-dir")
    parser.add_argument("--data-dir")
    parser.add_argument("--state-dir")
    parser.add_argument("--cache-dir")
    parser.add_argument("--log-dir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--instance-id", help=argparse.SUPPRESS)
    return parser


def _registry(arguments: argparse.Namespace) -> PathRegistry:
    overrides = {
        name: getattr(arguments, name)
        for name in (
            "config_file",
            "config_dir",
            "data_dir",
            "state_dir",
            "cache_dir",
            "log_dir",
        )
        if getattr(arguments, name) is not None
    }
    return resolve_paths(cli_overrides=overrides)


def _remove_own_metadata(paths: PathRegistry, instance_id: str) -> None:
    _remove_matching_metadata(paths.daemon_metadata_file, instance_id)


class _UnboundWorker:
    """Stands in for a managed worker this installation cannot bind.

    P5.4 binds the real one whenever an ACTIVE release exists and the operator
    has approved its exact bytes. This is what is left when neither is true --
    nothing is activated, the activated release was never approved, or the
    updater's evidence cannot be read. The adapter is still constructed, so
    health derives a true answer and the shadow fence is live, and any frame
    that reaches this object fails closed rather than silently succeeding.
    """

    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float | None = None
    ) -> object:
        raise RuntimeError("managed worker transport is not bound")


def main(argv: Sequence[str] | None = None) -> int:
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    arguments = parser.parse_args(arguments_list)
    if arguments.instance_id is None:
        instance_id = uuid.uuid4().hex
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "cortex_platform.product.daemon",
                "--instance-id",
                instance_id,
                *arguments_list,
            ],
        )
        return 1

    paths = _registry(arguments)
    initialize(paths, environ=os.environ)
    lifetime_lock = acquire_daemon_lifetime_lock(paths.daemon_lifetime_lock_file)
    if lifetime_lock is None:
        print("cortexd is already running", file=sys.stderr)
        return 3
    # ⟦P5.6⟧ The daemon's own log, on the path the launchers already redirect
    # stdout/stderr into. Before this nothing in the process ever wrote a line
    # there, and a poller failing for sixteen minutes left no trace.
    log_handler = configure_daemon_logging(paths.daemon_log_file)
    _log.info(
        "cortexd starting instance=%s pid=%s", arguments.instance_id, os.getpid()
    )
    stop_requested = threading.Event()
    server: CortexHTTPServer | None = None
    schedule_runner: ResearchScheduleRunner | None = None
    readings_service = None
    window_supervisor: TransportWindowSupervisor | None = None
    managed_worker: ManagedTransportWorker | None = None
    turn_bridge: InboundTurnBridge | None = None
    previous_term = None
    previous_int = None
    try:
        control_token = secrets.token_urlsafe(32)
        control_store = ControlStore(paths.control_database_file)
        control_store.initialize()
        # A transport window that outlived the process that opened it has no
        # proof it was ever released (D-P5-5): the start that finds one says
        # so in the audit trail and takes the gate back down.
        control_store.abort_open_transport_windows(
            reason="product_start", actor_id="cortexd"
        )
        # Nothing constructs a `TelegramAdapter` yet (P5.3 does), so health
        # derives `telegram_adapter` to False through the adapter half rather
        # than through a literal. The mode travels now because it decides
        # whether the adapter, once built, may touch its client at all.
        product_config = load_config(paths.config_file)
        from .readings.service import build_readings_service

        # Validate publication before any workers start. The controller only
        # prepares private staging; all external writes run in the publisher.
        readings_service = build_readings_service(config=product_config, paths=paths, store=control_store)
        # P5.4: one managed worker per daemon, for the ACTIVE release only,
        # resolved through the attempt-free derivation and refused unless the
        # operator has approved its exact bytes (D6). Binding launches nothing
        # -- the process starts on the first frame a transport window needs --
        # so a product that never opens a window never runs the release's code.
        managed_worker = ManagedTransportWorker(
            store=control_store,
            paths=paths,
            config=product_config,
            environ=os.environ,
        )
        managed_worker.bind()
        # P5.3: the adapter is constructed only when the operator has named a
        # bot. Health's `telegram_adapter` is `adapter constructed AND a worker
        # is bound AND the gate is enabled`, so an installation that has never
        # configured one reports false because there is nothing to report, not
        # because of a literal. An unbound worker still gets an adapter whose
        # RPC refuses every frame, which is the correct behaviour rather than
        # an adapter that silently succeeds.
        worker_rpc = GatedWorkerTransportRPC(
            supervisor=(
                managed_worker if managed_worker.bound else _UnboundWorker()
            ),
            store=control_store,
        )
        telegram_adapter = build_telegram_adapter(
            store=control_store,
            config=product_config,
            rpc=worker_rpc,
            worker_id=f"cortexd-{arguments.instance_id}",
        )
        if managed_worker.bound:
            # ⟦P5.4c⟧ The inbound half of the round trip. Built whenever a
            # worker is bound, adapter or not: `recover` has to converge runs a
            # previous daemon left mid-turn even on an installation whose
            # transport is not configured, or a crash inside a turn strands the
            # run for ever.
            turn_bridge = InboundTurnBridge(
                store=control_store,
                worker=managed_worker,
                actor_id=f"cortexd-turn-{arguments.instance_id}",
            )
        if telegram_adapter is not None and turn_bridge is not None:
            # The poller thread calls this; it only enqueues. A turn takes
            # seconds to minutes and the next `telegram.poll` must not wait for
            # one.
            telegram_adapter.bind_turn_sink(turn_bridge.submit)
            # ⟦P9-3 BRK-4⟧ So `/pause` can be refused by a runtime that cannot
            # pause, rather than committed and silently rolled back.
            telegram_adapter.bind_capability_probe(turn_bridge.runtime_supports)
        if telegram_adapter is not None and managed_worker.bound:
            # P5.4b: the outbound half. Control's delivery ledger is the queue;
            # the directory is how a destination is recovered from the HMAC the
            # ledger stores, without the product ever holding a raw chat id.
            directory = DestinationDirectory(
                store=control_store,
                bot_identity=telegram_adapter.config.bot_identity,
                allowed_user_ids=telegram_allowed_user_ids(product_config),
            )

            # The scope of an update the operator actually sent is the best
            # candidate there is, and it costs one dict entry -- but ⟦F-B4⟧
            # only for an update the adapter's allowlist accepted.
            handle_update = learning_update_handler(
                directory, telegram_adapter.handle_update
            )

            # The gate is durable state another process writes, so the daemon
            # watches it: an `enable-window` starts the inbound poller here and
            # a `disable` stops it and releases the worker.
            window_supervisor = TransportWindowSupervisor(
                store=control_store,
                worker=managed_worker,
                rpc=worker_rpc,
                handle_update=handle_update,
                drain=TransportDeliveryDrain(
                    store=control_store,
                    adapter=telegram_adapter,
                    directory=directory,
                    # ⟦P5.5⟧ The same object the RPC serializes on, so the
                    # drain's "I still owe a message" and the poller's "how
                    # long may I park" are one decision and not two.
                    serializer=worker_rpc.serializer,
                ),
            )
        # ⟦P8⟧ The bridge the poller's sink is bound to is the bridge the API
        # drives: a run created through `POST /threads/{id}/runs` is submitted
        # to it, so the cockpit's turn and a Telegram message's turn are the
        # same orchestrator under the same dispatch gate. With no bridge (the
        # worker did not bind at start) the API refuses the run typed rather
        # than queueing what nothing here could drive.
        control_api = ControlAPI(
            control_store,
            access_token=control_token,
            telegram_adapter=telegram_adapter,
            telegram_mode=telegram_mode(product_config),
            managed_worker=managed_worker,
            transport_windows=window_supervisor,
            turn_bridge=turn_bridge,
            readings_service=readings_service,
        )
        server = create_server(
            host=arguments.host,
            port=arguments.port,
            instance_id=arguments.instance_id,
            control_token=control_token,
            stop_requested=stop_requested,
            control_api=control_api,
        )
        port = int(server.server_address[1])
        start_token = current_process_start_token(os.getpid())
        if start_token is None:
            raise RuntimeError("could not establish daemon process identity")
        metadata = DaemonMetadata(
            pid=os.getpid(),
            instance_id=arguments.instance_id,
            start_token=start_token,
            host="127.0.0.1",
            port=port,
            control_token=control_token,
        )
        write_metadata(paths.daemon_metadata_file, metadata)
        _log.info(
            "cortexd serving port=%s managed_worker=%s adapter=%s",
            port,
            "bound" if managed_worker.health().bound else "unbound",
            telegram_adapter is not None,
        )
        # P4.3: the whole of the daemon's engine wiring. Returns None on an
        # installation that has adopted no corpus, so cortexd starts exactly as
        # before rather than pointing the engine at a guessed directory.
        schedule_runner = start_research_schedules(store=control_store, paths=paths, config=product_config)
        if readings_service is not None:
            readings_service.start()
        if turn_bridge is not None:
            # Recovery BEFORE the loops: an attempt a previous daemon left in
            # flight has to be converged before a new message can be told the
            # thread already has an active run.
            turn_bridge.recover()
            turn_bridge.start()
        if window_supervisor is not None:
            window_supervisor.start()

        def request_stop(signum: int, frame: object) -> None:
            if server is not None:
                server.request_stop()
            else:
                stop_requested.set()

        previous_term = signal.signal(signal.SIGTERM, request_stop)
        previous_int = signal.signal(signal.SIGINT, request_stop)
        server.timeout = 0.2
        while not stop_requested.is_set():
            server.handle_request()
    finally:
        # First, and without waiting out a turn: the bridge is told to stop
        # accepting, and the worker release below is what actually ends a turn
        # still streaming -- typed, through the runtime's uncertain-outcome
        # path, rather than by a join that could never return.
        if turn_bridge is not None:
            turn_bridge.stop(timeout=1.0)
        # Before the server: a stop that left the worker running would leave
        # the process step 5 of the window procedure has to prove is gone.
        if window_supervisor is not None:
            window_supervisor.stop()
        elif managed_worker is not None:
            managed_worker.close()
        if schedule_runner is not None:
            schedule_runner.stop()
        if readings_service is not None:
            readings_service.stop()
        if server is not None:
            server.server_close()
        _remove_own_metadata(paths, arguments.instance_id)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_int is not None:
            signal.signal(signal.SIGINT, previous_int)
        release_daemon_lifetime_lock(lifetime_lock)
        _log.info("cortexd stopped instance=%s", arguments.instance_id)
        release_daemon_logging(log_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
