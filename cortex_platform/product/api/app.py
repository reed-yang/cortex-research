"""Framework-independent routing for the loopback Cortex control API."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from ..artifacts.reader import ArtifactContentUnavailable, ArtifactReader
from ..research.documents import MAX_DOCUMENT_BYTES, ResearchDocumentReader, ResearchDocumentUnavailable
from ..config import DEFAULT_TELEGRAM_MODE
from ..control import (
    CANCELED_BEFORE_BINDING,
    CAPTURE_BLOCKING_CATEGORIES,
    CAPTURE_STATES,
    CAPTURE_TERMINAL_STATES,
    DISPATCHABLE_RUN_STATES,
    FOLLOWUP_RUN_STATES,
    THREAD_PAGE_LIMIT,
    CaptureConflict,
    ControlStore,
    ControlStoreError,
    IdempotencyConflict,
    InvalidTransition,
    MachineRunRefused,
    NotFound,
    RevisionConflict,
    ThreadActiveRun,
    ThreadArchived,
)
from ..sources import SourceResolver, canonicalize_arxiv_id, canonicalize_doi
from .events import _SENSITIVE_TEXT_PATTERNS, project_public_decision, project_public_event
from .research import ResearchWorkflowProjector

_PUBLIC_ID_PATTERN = r"[A-Za-z0-9_-]{1,200}"

#: ⟦P8-08⟧ The one header that names who acted through the public front door.
#: The web adapter strips it from every caller and sets it itself, only after
#: it verified the Cloudflare Access assertion (`access-security.ts`), so it is
#: trusted exactly as far as the control token is: this API is loopback-only
#: and the adapter is the token holder. Absent it, the caller is the operator
#: sitting at the machine.
_ACCESS_IDENTITY_HEADER = "X-Cortex-Access-Identity"
_LOCAL_OPERATOR_ACTOR = "local-operator"
_ACCESS_ACTOR_PREFIX = "access:"
#: `actor_id` columns are bounded to 200 characters, so the identity that fits
#: inside `access:<identity>` is bounded to 200 minus the prefix.
_ACCESS_IDENTITY_MAXIMUM = 200 - len(_ACCESS_ACTOR_PREFIX)
#: The same shape AND the same length bound as the adapter's
#: `ACCESS_IDENTITY_MAX_LENGTH` (`app/api/cortex/access-security.ts`) and its
#: node twin, restated here so a malformed identity is refused at the boundary
#: that records it rather than trusted for the length of an audit trail. ⟦ADJ-D⟧
#: The two must agree: an identity the door admits and this refuses would
#: authenticate and then fail every write with 400.
_ACCESS_IDENTITY_PATTERN = re.compile(
    r"[^\s@,;\"'<>()\[\]\\]{1,64}"
    r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class APIResponse:
    status: int
    payload: dict[str, Any]
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EventStreamStart:
    after_cursor: int


#: ⟦N-1⟧ The state each operator control asks for, shared by the route that
#: writes it and the refusal decided before it is written.
_REQUESTED_RUN_STATES = {"pause": "pause_requested", "cancel": "cancel_requested"}


class ControlAPI:
    """Map authenticated v1 HTTP requests to transactional store commands."""

    def __init__(
        self,
        store: ControlStore,
        *,
        access_token: str,
        allowed_origins: frozenset[str] = frozenset(),
        source_resolver: SourceResolver | None = None,
        telegram_adapter: object | None = None,
        telegram_mode: str = DEFAULT_TELEGRAM_MODE,
        managed_worker: object | None = None,
        transport_windows: object | None = None,
        turn_bridge: object | None = None,
        research_catalog: object | None = None,
        readings_service: object | None = None,
    ) -> None:
        if len(access_token) < 32:
            raise ValueError("API access token must contain at least 32 characters")
        if telegram_mode not in {"active", "shadow"}:
            raise ValueError("telegram_mode is invalid")
        self.store = store
        self._access_token = access_token
        self._cursor_key = store.cursor_signing_key()
        self._allowed_origins = allowed_origins
        self._source_resolver = source_resolver
        # The adapter is held as an opaque object on purpose: health reports
        # whether one was constructed, and nothing here should be able to
        # reach into it. Typing it would also import the transports package
        # into the request path, which the daemon deliberately does not.
        self._telegram_adapter = telegram_adapter
        self._telegram_mode = telegram_mode
        # Held opaquely for the same reason the adapter is: health asks it one
        # question and the close route asks it one more, and neither should be
        # able to reach further into the daemon's worker than that.
        self._managed_worker = managed_worker
        self._transport_windows = transport_windows
        self._turn_bridge = turn_bridge
        self._research_catalog = research_catalog
        self._readings_service = readings_service

    def handle(
        self,
        *,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
        client_host: str = "127.0.0.1",
    ) -> APIResponse:
        """Route one request, answering `GET /api/v1/health` unauthenticated.

        Two health fields talk about runtime dispatch and mean different
        things; they are read together or not at all.

        `capabilities.runtime_dispatch` is what this BUILD can do: drive a run
        the API created, through the turn bridge. It is a constant of the code,
        true since P8, and it does not move when the gate does. A cockpit reads
        it to know whether asking is meaningful at all.

        `runtime_dispatch_enabled` is whether dispatch is switched ON HERE, and
        it is a tri-state: `True` / `False` from the durable Control gate, and
        `None` when this daemon cannot answer -- no turn bridge, no bound
        managed worker, or the store could not be read. `None` is "not
        reported"; it is never to be read as open. That is the field the
        cockpit's badge and the composer's copy are bound to.

        So `runtime_dispatch True` with `runtime_dispatch_enabled False` is the
        ordinary shape of a healthy daemon with the gate closed: capable, off.
        Neither field is an activation or certification claim -- that lives in
        the distribution product manifest, where `runtime_dispatch` is still
        `disabled`.
        """

        try:
            split = urlsplit(target)
            if split.scheme or split.netloc or split.fragment:
                raise ValueError("request target is invalid")
            path = split.path
            if method == "GET" and path == "/api/v1/health":
                return APIResponse(
                    200,
                    {
                        "api_version": "v1",
                        "capabilities": {
                            "control_store": True,
                            "event_replay": True,
                            "event_stream": True,
                            "source_resolution": self._source_resolver is not None,
                            "artifact_metadata": True,
                            "research_pipeline": True,
                            # ⟦P9-2 / batchK-5⟧ A build capability, like
                            # `control_store` and `research_pipeline` beside
                            # it: this build knows how to drive a run it
                            # created itself. True since P8 wired
                            # `POST /threads/{id}/runs` to the turn bridge.
                            #
                            # Deliberately a literal, and deliberately NOT the
                            # tri-state gate. It answers "could asking ever
                            # mean anything here", which is a property of the
                            # code and cannot move while the daemon runs; the
                            # gate answers "is dispatch on right now", which
                            # can, and is `runtime_dispatch_enabled` below
                            # (`_runtime_dispatch_gate`, tri-state: `None` is
                            # "not reported", never "open").
                            #
                            # Nothing binds a surface to this key. The
                            # cockpit's rail badge follows the GATE:
                            # `apps/web/app/control/client.ts:130-136`
                            # (`getRuntimeDispatchGate`) reads
                            # `runtime_dispatch_enabled` and nothing else out
                            # of this payload, and
                            # `apps/web/app/control-workspace.tsx:278` prints
                            # `Runtime dispatch ${dispatchGateLabel(dispatchGate)}`
                            # from it. So making this key follow the gate
                            # would change no pixel and would lose the one
                            # question it answers.
                            #
                            # It is also not an activation or certification
                            # claim: that is the distribution manifest's own
                            # `runtime_dispatch` row
                            # (`distribution/product_manifest.py:758`), which
                            # stays `disabled`. Pinned as a build constant --
                            # the same value with the gate unreported, closed
                            # and open -- by the capability test in
                            # `tests/product/api/test_health_telegram.py`,
                            # which asserts the pair moves apart.
                            "runtime_dispatch": True,
                            # A1-8: derived, never a literal. Three halves have
                            # to hold since P5.4 -- an adapter exists, the
                            # durable transport gate authorizes it, AND a
                            # managed worker is bound to serve the frames --
                            # so flipping this to True takes an activated,
                            # approved release rather than an edit to this
                            # line. The adapter check is first so an
                            # unauthenticated health request does not read the
                            # control store while there is nothing to report.
                            "telegram_adapter": (
                                self._telegram_adapter is not None
                                and self._managed_worker_bound()
                                and self.store.telegram_dispatch_enabled()
                            ),
                            "telegram_mode": self._telegram_mode,
                        },
                        # P5.4: why the daemon does or does not own a worker,
                        # in the vocabulary `ManagedTransportWorker` refuses
                        # with. Identity only -- no paths, and no credential
                        # has any route to this payload.
                        "managed_worker": self._managed_worker_health(),
                        # P5.4b: what the inbound loop and the outbound drain
                        # are doing right now, in counts and categories. No
                        # message body, no chat id, no credential.
                        "transport_window": self._transport_window_status(),
                        # ⟦P5.4c⟧ Whether an inbound message becomes a turn, and
                        # why not when it does not -- `runtime_activation_disabled`
                        # here with an open window is the two-gates answer, and
                        # the one an operator watching a window most needs. Counts
                        # and categories only.
                        "turn_bridge": self._turn_bridge_status(),
                        # ⟦P8 V-5⟧ The dispatch gate itself, so the cockpit
                        # can say what a message on a queued run became: kept
                        # for a turn that runs when dispatch is enabled, or
                        # joined to one that runs now. One row; null when the
                        # store could not be asked.
                        "runtime_dispatch_enabled": self._runtime_dispatch_gate(),
                        "status": "ok",
                    },
                )
            self._authenticate(headers, client_host=client_host)
            if method == "GET" and path == "/api/v1/readings":
                if split.query:
                    raise ValueError("readings query parameters are not supported")
                return APIResponse(200, self._readings_service.status() if self._readings_service else {"enabled": False, "items": []})
            if method == "GET":
                return self._get(path, parse_qs(split.query, keep_blank_values=True))
            if method == "POST":
                if split.query:
                    raise ValueError("POST query parameters are not supported")
                return self._post(path, headers, self._parse_body(body))
            return self._problem(405, "invalid_request", "Method is not supported")
        except RevisionConflict as exc:
            return self._problem(
                409,
                exc.category,
                str(exc),
                current=self._public_conflict_resource(exc.current),
            )
        except (ThreadActiveRun, ThreadArchived) as exc:
            # Both are the thread's own state refusing a write, and both hand
            # back the row so the caller can act on what it just learned.
            return self._problem(
                409,
                exc.category,
                str(exc),
                current=self._public_conflict_resource(exc.current),
            )
        except CaptureConflict as exc:
            return self._problem(
                409,
                exc.category,
                str(exc),
                current=self._public_conflict_resource(exc.current),
            )
        except (IdempotencyConflict, InvalidTransition, MachineRunRefused) as exc:
            # ⟦ADJ-4⟧ `MachineRunRefused` is the store's own `machine_run`:
            # the same problem `_machine_run_refusal` answers first.
            return self._problem(409, exc.category, str(exc))
        except NotFound as exc:
            return self._problem(404, exc.category, str(exc))
        except ResearchDocumentUnavailable:
            return self._problem(409, "research_document_unavailable", "Research document could not be read")
        except ArtifactContentUnavailable:
            return self._problem(
                409,
                "artifact_content_unavailable",
                "Artifact content is unavailable or could not be verified",
            )
        except PermissionError as exc:
            category = str(exc) if str(exc) in {
                "authentication_required",
                "origin_rejected",
                "loopback_required",
            } else "authentication_required"
            return self._problem(403, category, "Request is not authorized")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return self._problem(400, "invalid_request", str(exc))
        except sqlite3.Error:
            return self._problem(
                503,
                "control_store_unavailable",
                "Control store is temporarily unavailable",
                retryable=True,
            )
        except ControlStoreError as exc:
            return self._problem(409, exc.category, str(exc), retryable=exc.retryable)
        except Exception:
            return self._problem(
                500,
                "internal_error",
                "Cortex could not complete the request",
                retryable=False,
            )

    def _catalog_call(self, method, *args, **kwargs):
        from ..research.catalog import ResearchCatalog, CatalogUnavailable, CatalogItemNotFound
        catalog = self._research_catalog
        if catalog is None:
            catalog = ResearchCatalog(self.store.path.parent / "research" / "research.db")
        try:
            return getattr(catalog, method)(*args, **kwargs)
        except CatalogItemNotFound:
            raise NotFound("research item", args[0] if args else "") from None
        except CatalogUnavailable:
            raise ResearchDocumentUnavailable("Research catalog is unavailable") from None

    def _research_public(self, value):
        if isinstance(value, str):
            return self._public_source_text(value)
        if isinstance(value, list):
            return [self._research_public(item) for item in value]
        if isinstance(value, dict):
            return {key: self._research_public(item) for key, item in value.items()}
        return value

    def _research_item_detail(self, item_id):
        item = self._catalog_call("get_item", item_id)
        documents = self.store.list_research_documents(item_id)
        roots = {root.root_id: root for root in self.store.list_asset_roots()}
        available = bool(documents) and all(
            (root := roots.get(self.store.get_research_document(doc["id"])["asset_root_id"])) is not None
            and root.enabled for doc in documents
        )
        return self._research_public({**item, "documents": documents,
            "thread_id": self.store.research_item_thread(item_id), "continuation_ready": available,
            "unavailable_reason": None if available else ("documents_not_adopted" if not documents else "document_root_unavailable"),
        })

    def _managed_worker_bound(self) -> bool:
        worker = self._managed_worker
        return worker is not None and bool(getattr(worker, "bound", False))

    def _managed_worker_health(self) -> dict[str, Any]:
        worker = self._managed_worker
        if worker is None:
            # Not "unbound because of a reason": there is no binding in this
            # process at all, which is what a demo-mode or pre-P5.4 daemon
            # looks like, and saying so is different from naming a reason.
            return {
                "state": "unbound",
                "reason": "managed_worker_unconfigured",
                "release_id": None,
                "slot_digest": None,
                "launched": False,
            }
        return dict(worker.health().to_dict())  # type: ignore[attr-defined]

    def _transport_window_status(self) -> dict[str, Any] | None:
        windows = self._transport_windows
        if windows is None:
            return None
        try:
            return dict(windows.status())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - health must answer, always
            return None

    def _runtime_dispatch_gate(self) -> bool | None:
        # ⟦V6-4 / N-5⟧ Ordered like the `telegram_adapter` capability: the
        # dispatch-gate row is read only when something could drive a run --
        # a bridge exists and its worker is bound. That is the whole
        # invariant; it is not "no row for an unauthenticated probe": the
        # `managed_worker` field of this same payload asks the worker's
        # health, which reads the release-approval row on any daemon that
        # owns a bound worker. `None` means "not reported", which the cockpit
        # treats as unknown, never as open.
        if self._turn_bridge is None or not self._managed_worker_bound():
            return None
        try:
            return bool(self.store.runtime_dispatch_enabled())
        except Exception:  # noqa: BLE001 - health must answer, always
            return None

    def _turn_bridge_status(self) -> dict[str, Any] | None:
        bridge = self._turn_bridge
        if bridge is None:
            return None
        try:
            return dict(bridge.status())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - health must answer, always
            return None

    def prepare_event_stream(
        self,
        *,
        target: str,
        headers: Mapping[str, str],
        client_host: str,
    ) -> EventStreamStart | APIResponse:
        try:
            split = urlsplit(target)
            if (
                split.scheme
                or split.netloc
                or split.fragment
                or split.path != "/api/v1/events/stream"
            ):
                raise ValueError("event stream target is invalid")
            self._authenticate(headers, client_host=client_host)
            query = parse_qs(split.query, keep_blank_values=True)
            if set(query) - {"after_cursor"}:
                raise ValueError("event stream query parameter is invalid")
            values = query.get("after_cursor")
            if values is not None and (len(values) != 1 or not values[0]):
                raise ValueError("after_cursor query parameter is invalid")
            cursor_value = values[0] if values is not None else None
            last_event_id = self._header(headers, "Last-Event-ID")
            if cursor_value and last_event_id:
                raise ValueError(
                    "after_cursor and Last-Event-ID cannot both be supplied"
                )
            encoded = cursor_value or last_event_id
            return EventStreamStart(
                after_cursor=self._decode_cursor(encoded) if encoded else 0
            )
        except PermissionError as exc:
            category = str(exc) if str(exc) in {
                "authentication_required",
                "origin_rejected",
                "loopback_required",
            } else "authentication_required"
            return self._problem(403, category, "Request is not authorized")
        except ValueError as exc:
            return self._problem(400, "invalid_request", str(exc))
        except Exception:
            return self._problem(
                500,
                "internal_error",
                "Cortex could not open the event stream",
                retryable=False,
            )

    def event_stream_batch(
        self, *, after_cursor: int, limit: int = 100
    ) -> tuple[list[dict[str, Any]], int]:
        events = self.store.list_events(after_cursor=after_cursor, limit=limit)
        next_cursor = int(events[-1]["cursor"]) if events else after_cursor
        return [self._public_event(event) for event in events], next_cursor

    @staticmethod
    def format_sse_event(event: Mapping[str, Any]) -> bytes:
        event_id = str(event["cursor"])
        event_type = str(event["type"]).replace("\r", "").replace("\n", "")
        data = json.dumps(
            dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode(
            "utf-8"
        )

    def _get(self, path: str, query: Mapping[str, list[str]]) -> APIResponse:
        if path == "/api/v1/workspaces":
            # ⟦A-1⟧ One ownership query for the whole list, never one store
            # call per row: `workspace_is_machine` opens a fresh connection
            # each time, so per-row is N connection setups on a list that
            # nothing paginates.
            machine = self.store.machine_workspace_ids()
            return APIResponse(
                200,
                {
                    "items": [
                        self._workspace_projection(workspace, machine_ids=machine)
                        for workspace in self.store.list_workspaces()
                    ],
                    "next_cursor": None,
                },
            )

        match = re.fullmatch(r"/api/v1/workspaces/([^/]+)", path)
        if match:
            return APIResponse(
                200, self._workspace_projection(self.store.get_workspace(match.group(1)))
            )

        if path == "/api/v1/research-items":
            self._require_query_fields(query, frozenset({"kind", "status", "limit", "offset"}))
            page = self._catalog_call("list_items",
                kind=self._optional_single_query(query, "kind"),
                status=self._optional_single_query(query, "status"),
                limit=self._integer_query(query, "limit", default=100),
                offset=self._integer_query(query, "offset", default=0),
            )
            return APIResponse(200, {**page, "items": [self._research_public(item) for item in page["items"]]})

        match = re.fullmatch(r"/api/v1/research-items/(ri_[a-f0-9]{32})", path)
        if match:
            self._require_query_fields(query, frozenset())
            return APIResponse(200, self._research_item_detail(match.group(1)))

        match = re.fullmatch(r"/api/v1/research-documents/(rdv_[a-f0-9]{32})/content", path)
        if match:
            self._require_query_fields(query, frozenset())
            content = ResearchDocumentReader(self.store).read(match.group(1))
            projected = self._public_source_text(content["content"])
            content["redacted"] = projected != content["content"]
            content["retained_byte_length"] = content["byte_length"]
            content["content"] = projected
            content["byte_length"] = len(projected.encode("utf-8"))
            if content["byte_length"] > MAX_DOCUMENT_BYTES:
                raise ResearchDocumentUnavailable("Projected research document exceeds the read limit")
            return APIResponse(200, content)

        if path == "/api/v1/threads":
            workspace_id = self._single_query(query, "workspace_id")
            # ⟦batchK-8⟧ Paged like `/threads/{id}/runs` and `/artifacts`, and
            # by default not paged at all: no `limit` is the whole list this
            # route has always returned, which is what the cockpit's
            # `listThreads` asks for. `next_cursor` is now the store's proven
            # one rather than a hard-coded null.
            after_id = self._optional_single_query(query, "after_id")
            limit = self._optional_integer_query(query, "limit")
            # ⟦Web shell 2026-09⟧ Archived threads are absent unless the
            # caller says otherwise, and anything but the two words is a bad
            # request rather than a silent "no".
            include_archived_raw = self._optional_single_query(query, "include_archived")
            if include_archived_raw not in (None, "true", "false"):
                raise ValueError("include_archived must be true or false")
            include_archived = include_archived_raw == "true"
            # ⟦batchO ADJ-3⟧ A cursor implies a page, so it implies the page's
            # bound. `after_id` without `limit` was the one request shape with
            # no ceiling: `list_threads(limit=None)` reads EVERY remaining row
            # (`LIMIT -1`, control/store.py:11805) and the ownership query below then
            # binds all of them into one `IN (...)` list -- measured at 900
            # threads as 81.3 ms against the unpaged read's 68.6 ms for one
            # row fewer, and past sqlite's `SQLITE_LIMIT_VARIABLE_NUMBER`
            # (32766 here) it stops answering at all: 503
            # `control_store_unavailable`, a category naming the store as
            # unavailable while the store is fine. Implying the bound rather
            # than narrowing `paged` below, because it also gives that request
            # a working `next_cursor` instead of the null it returned while
            # handing back the whole remainder. Unreachable by any shipped
            # client -- `client.ts` `listThreads` sends neither parameter --
            # so no surface changes; a hand-edited URL now gets a page.
            if limit is None and after_id is not None:
                limit = THREAD_PAGE_LIMIT
            threads = self.store.list_threads(
                workspace_id=workspace_id,
                after_id=after_id,
                limit=limit,
                include_archived=include_archived,
            )
            # ⟦A-1 + batchK-8⟧ Ownership for the PAGE in ONE query, not one
            # fresh connection per row and not once for the whole workspace:
            # same predicate, same answer, at a cost that is the page's rather
            # than that of a list which is never pruned. An UNPAGED request
            # still asks by workspace, because there the page is the whole
            # workspace: binding every id into an `IN` list to reach the same
            # answer is the one shape that would be slower than before it
            # paged (measured at 900 threads: 87 ms against 72 ms). After the
            # implied bound above, `limit is None` is exactly "no page was
            # asked for": `after_id` can no longer reach this line unbounded.
            paged = limit is not None
            machine = self.store.machine_thread_ids(
                workspace_id,
                thread_ids=(
                    [str(thread["id"]) for thread in threads] if paged else None
                ),
            )
            return APIResponse(
                200,
                {
                    "items": [
                        self._thread_projection(
                            thread, engine_owned=str(thread["id"]) in machine
                        )
                        for thread in threads
                    ],
                    "next_cursor": getattr(threads, "next_cursor", None),
                },
            )

        match = re.fullmatch(r"/api/v1/threads/([^/]+)", path)
        if match:
            return APIResponse(200, self._thread_projection(self.store.get_thread(match.group(1))))

        match = re.fullmatch(r"/api/v1/threads/([^/]+)/messages", path)
        if match:
            return APIResponse(
                200,
                {"items": self.store.list_messages(match.group(1)), "next_cursor": None},
            )

        match = re.fullmatch(
            rf"/api/v1/threads/({_PUBLIC_ID_PATTERN})/runs", path
        )
        if match:
            self._require_query_fields(query, frozenset({"after_id", "limit"}))
            after_id = self._optional_single_query(query, "after_id")
            limit = self._integer_query(query, "limit", default=100)
            items = self.store.list_thread_runs(
                thread_id=match.group(1), after_id=after_id, limit=limit
            )
            # ⟦ADJ-A + A-1⟧ One ownership query for the page, never two store
            # calls per row -- and ⟦ADJ-G-4⟧ for the PAGE, not the thread: the
            # route paginates but a carrier thread's run list does not stop
            # growing.
            machine = self.store.machine_run_ids(
                match.group(1), run_ids=[str(item["id"]) for item in items]
            )
            return APIResponse(
                200,
                {
                    "items": [
                        self._run_projection(
                            item, engine_owned=str(item["id"]) in machine
                        )
                        for item in items
                    ],
                    "next_cursor": getattr(items, "next_cursor", None),
                },
            )

        match = re.fullmatch(
            rf"/api/v1/runs/({_PUBLIC_ID_PATTERN})/research-workflow", path
        )
        if match:
            self._require_query_fields(query, frozenset())
            return APIResponse(
                200, ResearchWorkflowProjector(self.store).project(match.group(1))
            )

        match = re.fullmatch(r"/api/v1/runs/([^/]+)", path)
        if match:
            return APIResponse(
                200, self._run_projection(self.store.get_run(match.group(1)))
            )

        match = re.fullmatch(r"/api/v1/runs/([^/]+)/events", path)
        if match:
            after = self._integer_query(query, "after_sequence", default=0)
            events = self.store.list_run_events(match.group(1), after_sequence=after)
            return APIResponse(
                200,
                {
                    "items": [self._public_event(event) for event in events],
                    "next_cursor": None,
                },
            )

        if path == "/api/v1/events":
            after = self._cursor_query(query, "after_cursor")
            limit = self._integer_query(query, "limit", default=500)
            events = self.store.list_events(after_cursor=after, limit=limit)
            next_cursor = int(events[-1]["cursor"]) if events else after
            return APIResponse(
                200,
                {
                    "items": [self._public_event(event) for event in events],
                    "next_cursor": self._encode_cursor(next_cursor),
                },
            )

        if path == "/api/v1/decisions":
            state = self._single_query(query, "state", default="pending")
            return APIResponse(
                200,
                {
                    "items": [
                        project_public_decision(decision)
                        for decision in self.store.list_decisions(state=state)
                    ],
                    "next_cursor": None,
                },
            )

        match = re.fullmatch(r"/api/v1/source-intents/([^/]+)", path)
        if match:
            return APIResponse(
                200,
                ResearchWorkflowProjector.project_source_gate(
                    self.store.get_source_intent(match.group(1))
                ),
            )

        if path == "/api/v1/sources":
            return APIResponse(
                200,
                {
                    "items": self._public_value(self.store.list_sources()),
                    "next_cursor": None,
                },
            )

        if path == "/api/v1/sources/search":
            self._require_query_fields(query, frozenset({"q", "limit"}))
            return self._source_knowledge_response(query=query)

        match = re.fullmatch(
            rf"/api/v1/sources/({_PUBLIC_ID_PATTERN})/content", path
        )
        if match:
            self._require_query_fields(query, frozenset({"kind", "cursor", "limit"}))
            return self._source_knowledge_response(
                source_id=match.group(1), query=query
            )

        match = re.fullmatch(r"/api/v1/sources/([^/]+)", path)
        if match:
            return APIResponse(
                200, self._public_value(self.store.get_source(match.group(1)))
            )

        if path == "/api/v1/artifacts":
            self._require_query_fields(
                query, frozenset({"thread_id", "after_id", "limit"})
            )
            thread_id = self._single_query(query, "thread_id")
            after_id = self._optional_single_query(query, "after_id")
            limit = self._integer_query(query, "limit", default=100)
            # ⟦batchO⟧ The store's proven cursor, not a guess from the page
            # being full: `list_artifacts` reads `limit + 1` and reports
            # whether anything remains, so an exactly-full LAST page ends the
            # list instead of handing back a cursor that returns nothing.
            page = self.store.list_artifacts(
                thread_id=thread_id, after_id=after_id, limit=limit
            )
            return APIResponse(
                200,
                {
                    "items": [self._public_artifact(item) for item in page],
                    "next_cursor": page.next_cursor,
                },
            )

        match = re.fullmatch(r"/api/v1/artifacts/([^/]+)", path)
        if match:
            self._require_query_fields(query, frozenset())
            return APIResponse(
                200, self._public_artifact(self.store.get_artifact(match.group(1)))
            )

        match = re.fullmatch(r"/api/v1/artifact-versions/([^/]+)", path)
        if match:
            self._require_query_fields(query, frozenset())
            return APIResponse(
                200,
                self._public_artifact_version(
                    self.store.get_artifact_version(match.group(1))
                ),
            )

        match = re.fullmatch(
            rf"/api/v1/artifact-versions/({_PUBLIC_ID_PATTERN})/content", path
        )
        if match:
            self._require_query_fields(query, frozenset())
            return APIResponse(
                200,
                ArtifactReader(self.store).read(match.group(1)).to_dict(),
                headers=(("Cache-Control", "no-store"),),
            )

        if path == "/api/v1/artifact-snapshots":
            self._require_query_fields(
                query, frozenset({"workspace_id", "after_id", "limit"})
            )
            workspace_id = self._single_query(query, "workspace_id")
            after_id = self._optional_single_query(query, "after_id")
            limit = self._integer_query(query, "limit", default=100)
            # ⟦batchO⟧ The store's proven cursor -- see `/artifacts` above.
            page = self.store.list_artifact_snapshots(
                workspace_id=workspace_id, after_id=after_id, limit=limit
            )
            return APIResponse(
                200,
                {
                    "items": [self._public_artifact_snapshot(item) for item in page],
                    "next_cursor": page.next_cursor,
                },
            )
        if path == "/api/v1/captures":
            # Fail closed on the query, unlike the older list routes that
            # silently ignore unknown parameters.
            self._require_query_fields(
                query, frozenset({"state", "limit", "cursor"})
            )
            state = self._optional_single_query(query, "state")
            if state is not None and state not in CAPTURE_STATES:
                raise ValueError("state query parameter is invalid")
            limit = self._integer_query(query, "limit", default=500)
            cursor = self._optional_single_query(query, "cursor")
            # ⟦batchO⟧ The store's proven cursor -- see `/artifacts` above.
            # The page is held rather than projected in place, because
            # `_capture_projections` returns a plain list and the cursor rides
            # on the page.
            page = self.store.list_captures(state=state, limit=limit, cursor=cursor)
            return APIResponse(
                200,
                {
                    "items": self._capture_projections(page),
                    "next_cursor": page.next_cursor,
                },
            )

        match = re.fullmatch(rf"/api/v1/captures/({_PUBLIC_ID_PATTERN})", path)
        if match:
            self._require_query_fields(query, frozenset())
            return APIResponse(
                200, self._capture_projection(self.store.get_capture(match.group(1)))
            )

        raise NotFound("endpoint", path)

    def _post(
        self,
        path: str,
        headers: Mapping[str, str],
        body: dict[str, Any],
    ) -> APIResponse:
        key = self._header(headers, "Idempotency-Key")
        if not key:
            raise ValueError("Idempotency-Key is required")
        actor = self._actor(headers)

        if path == "/api/v1/workspaces":
            return self._command(
                self.store.create_workspace(
                    title=self._string(body, "title"),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=self._workspace_projection,
            )

        match = re.fullmatch(r"/api/v1/research-items/(ri_[a-f0-9]{32})/thread", path)
        if match:
            self._require_body_fields(body, frozenset({"workspace_id", "expected_revision"}))
            workspace_id = self._string(body, "workspace_id")
            expected = self._revision(body)
            replay = self.store.replay_command(
                actor_id=actor, operation=f"POST:{path}", idempotency_key=key,
                request={"workspace_id": workspace_id, "expected_revision": expected},
            )
            if replay is not None:
                return self._command(replay, projector=self._thread_projection)
            self.store.get_workspace(workspace_id)
            if self.store.workspace_is_machine(workspace_id):
                return self._problem(409, "machine_workspace", "The workspace belongs to the research engine")
            detail = self._research_item_detail(match.group(1))
            if not detail["continuation_ready"]:
                return self._problem(409, "research_documents_unavailable", "Adopt this item's documents before opening research")
            return self._command(self.store.open_research_thread(
                item_id=match.group(1), workspace_id=workspace_id, expected_revision=expected,
                actor_id=actor, idempotency_key=key,
            ), projector=self._thread_projection)

        match = re.fullmatch(r"/api/v1/runs/([^/]+)/source-intents", path)
        if match:
            title = self._optional_string(body, "title")
            locator = self._optional_string(body, "locator")
            locator_sha256 = self._optional_string(body, "locator_sha256")
            attempt_id = self._string(body, "attempt_id")
            replay = self.store.replay_source_intent(
                run_id=match.group(1),
                attempt_id=attempt_id,
                title=title,
                locator=locator,
                locator_sha256=locator_sha256,
                actor_id=actor,
                idempotency_key=key,
            )
            if replay is not None:
                return self._command(
                    replay,
                    projector=ResearchWorkflowProjector.project_source_gate,
                )
            if self._source_resolver is None:
                return self._problem(
                    409,
                    "source_resolution_disabled",
                    "Source resolution is not enabled",
                )
            candidates = self._source_resolver.resolve(
                title=title,
                locator=locator,
                locator_sha256=locator_sha256,
            )
            return self._command(
                self.store.create_source_intent(
                    run_id=match.group(1),
                    attempt_id=attempt_id,
                    title=title,
                    locator=locator,
                    candidates=[candidate.to_record() for candidate in candidates],
                    actor_id=actor,
                    idempotency_key=key,
                    locator_sha256=locator_sha256,
                ),
                projector=ResearchWorkflowProjector.project_source_gate,
            )

        match = re.fullmatch(r"/api/v1/source-intents/([^/]+)/resolve", path)
        if match:
            self._require_body_fields(body, frozenset({"choice", "expected_revision"}))
            return self._command(
                self.store.resolve_source_intent(
                    intent_id=match.group(1),
                    choice=self._string(body, "choice"),
                    expected_revision=self._revision(body),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=ResearchWorkflowProjector.project_source_gate,
            )

        match = re.fullmatch(r"/api/v1/workspaces/([^/]+)/threads", path)
        if match:
            return self._command(
                self.store.create_thread(
                    workspace_id=match.group(1),
                    title=self._string(body, "title"),
                    expected_revision=self._revision(body),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=self._thread_projection,
            )

        match = re.fullmatch(r"/api/v1/threads/([^/]+)/messages", path)
        if match:
            role = self._string(body, "role")
            result = self.store.append_message(
                thread_id=match.group(1),
                role=role,
                content=self._string(body, "content"),
                expected_revision=self._revision(body),
                actor_id=actor,
                idempotency_key=key,
            )
            if role == "user":
                self._drive_followup(match.group(1), result)
            return self._command(result)

        match = re.fullmatch(r"/api/v1/threads/([^/]+)/runs", path)
        if match:
            thread_id = match.group(1)
            expected = self._revision(body)
            # ⟦P8⟧ The gate first, before anything durable is written -- the
            # bridge's own rule, applied where the run is created. The receipt
            # first still: a refusal is never given about a run this same
            # request already created and whose response was lost.
            refusal = self._turn_refusal(
                actor,
                operation=f"POST:/api/v1/threads/{thread_id}/runs",
                key=key,
                request={"expected_revision": expected},
                thread_id=thread_id,
                creating=True,
            )
            if refusal is not None:
                return refusal
            result = self.store.create_run(
                thread_id=thread_id,
                expected_revision=expected,
                actor_id=actor,
                idempotency_key=key,
            )
            self._drive(result)
            return self._command(result, projector=self._run_projection)

        match = re.fullmatch(r"/api/v1/workspaces/([^/]+)/rename", path)
        if match:
            workspace_id = match.group(1)
            title = self._string(body, "title")
            expected = self._revision(body)
            # The receipt is consulted before the ownership rule, exactly as
            # `_turn_refusal` does it: a refusal must never be given about a
            # command that already committed and whose response was lost.
            # `_required_text` strips before the store hashes the request, so
            # the probe hashes the stripped title or a legitimate retry would
            # read as an idempotency conflict.
            replay = self.store.replay_command(
                actor_id=actor,
                operation=f"POST:/api/v1/workspaces/{workspace_id}/rename",
                idempotency_key=key,
                request={"title": title.strip(), "expected_revision": expected},
            )
            if replay is not None:
                return self._command(replay, projector=self._workspace_projection)
            # A workspace nobody has is a 404 before it is an ownership question.
            self.store.get_workspace(workspace_id)
            if self.store.workspace_is_machine(workspace_id):
                return self._problem(
                    409, "machine_workspace", "The workspace belongs to the research engine"
                )
            return self._command(
                self.store.rename_workspace(
                    workspace_id=workspace_id,
                    title=title,
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=self._workspace_projection,
            )

        match = re.fullmatch(r"/api/v1/threads/([^/]+)/(rename|archive|unarchive)", path)
        if match:
            thread_id, action = match.groups()
            expected = self._revision(body)
            thread_title = self._string(body, "title") if action == "rename" else None
            request: dict[str, Any] = {"expected_revision": expected}
            if thread_title is not None:
                request = {"title": thread_title.strip(), "expected_revision": expected}
            # Receipt first, for the reason spelled out on the rename above.
            replay = self.store.replay_command(
                actor_id=actor,
                operation=f"POST:/api/v1/threads/{thread_id}/{action}",
                idempotency_key=key,
                request=request,
            )
            if replay is not None:
                return self._command(replay, projector=self._thread_projection)
            # A thread nobody has is a 404 before it is an ownership question.
            self.store.get_thread(thread_id)
            if self.store.thread_is_machine(thread_id):
                return self._problem(
                    409, "machine_thread", "The thread belongs to the research engine"
                )
            if action == "rename":
                result = self.store.rename_thread(
                    thread_id=thread_id,
                    title=thread_title or "",
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                )
            elif action == "archive":
                result = self.store.archive_thread(
                    thread_id=thread_id,
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                )
            else:
                result = self.store.unarchive_thread(
                    thread_id=thread_id,
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                )
            return self._command(result, projector=self._thread_projection)

        match = re.fullmatch(r"/api/v1/runs/([^/]+)/(pause|cancel|resume|retry)", path)
        if match:
            run_id, action = match.groups()
            expected = self._revision(body)
            if action in {"cancel", "pause"}:
                # ⟦N-1⟧ The engine's run is the engine's to end: refused
                # before the transition is written, after the receipt.
                refusal = self._machine_run_refusal(
                    actor,
                    run_id=run_id,
                    target_state=_REQUESTED_RUN_STATES[action],
                    key=key,
                    expected=expected,
                )
                if refusal is not None:
                    return refusal
            if action == "pause":
                refusal = self._unsupported_pause_refusal(
                    actor, run_id=run_id, key=key, expected=expected
                )
                if refusal is not None:
                    return refusal
            if action in {"resume", "retry"}:
                # Both leave the run in a dispatchable state, which is a run
                # this daemon has to be able to drive or must not create. The
                # request the receipt was hashed over is the store's own
                # normalized one (`_restart_attempt`).
                reason = (
                    self._string(body, "reason").strip() if action == "retry" else None
                )
                refusal = self._turn_refusal(
                    actor,
                    operation=f"POST:/api/v1/runs/{run_id}/{action}",
                    key=key,
                    request={"expected_revision": expected, "reason": reason},
                    thread_id=str(self.store.get_run(run_id)["thread_id"]),
                )
                if refusal is not None:
                    return refusal
            if action == "resume":
                result = self.store.resume_run(
                    run_id=run_id,
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                )
                self._drive(result)
            elif action == "retry":
                result = self.store.retry_run(
                    run_id=run_id,
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                    reason=self._string(body, "reason"),
                )
                self._drive(result)
            else:
                result = self.store.transition_run(
                    run_id=run_id,
                    target_state=_REQUESTED_RUN_STATES[action],
                    expected_revision=expected,
                    actor_id=actor,
                    idempotency_key=key,
                )
                if action == "cancel":
                    result = self._converge_cancellation(result, actor=actor)
                    self._forget(run_id)
            return self._command(result, projector=self._run_projection)

        match = re.fullmatch(r"/api/v1/decisions/([^/]+)/resolve", path)
        if match:
            self._require_body_fields(body, frozenset({"choice", "expected_revision"}))
            return self._command(
                self.store.resolve_decision(
                    decision_id=match.group(1),
                    choice=self._string(body, "choice"),
                    expected_revision=self._revision(body),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=project_public_decision,
            )

        if path == "/api/v1/captures":
            self._require_body_fields(body, frozenset({"payload", "note"}))
            return self._command(
                self.store.create_capture(
                    payload=self._string(body, "payload"),
                    note=self._string(body, "note"),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=self._capture_projection,
            )

        match = re.fullmatch(
            rf"/api/v1/captures/({_PUBLIC_ID_PATTERN})/(approve|dismiss)", path
        )
        if match:
            capture_id, action = match.groups()
            self._require_body_fields(body, frozenset({"expected_revision"}))
            command = (
                self.store.approve_capture
                if action == "approve"
                else self.store.dismiss_capture
            )
            return self._command(
                command(
                    capture_id=capture_id,
                    expected_revision=self._revision(body),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=self._capture_projection,
            )

        match = re.fullmatch(
            rf"/api/v1/transport/windows/({_PUBLIC_ID_PATTERN})/close", path
        )
        if match:
            # ⟦AMD-4⟧ + the P5.4 seam: the operator asks the DAEMON to close the
            # window, because the derivation needs a supervisor handle and a
            # worker pid that live only here. No `poller_stopped` and no `proof`
            # cross this boundary -- a client that could supply either would be
            # writing the literal D-P5-5 forbids.
            self._require_body_fields(body, frozenset({"actor_id"}))
            if self._transport_windows is None:
                return self._problem(
                    409,
                    "managed_worker_unavailable",
                    "This daemon owns no transport window to close",
                )
            return APIResponse(
                200,
                dict(
                    self._transport_windows.close_window(  # type: ignore[attr-defined]
                        window_id=match.group(1),
                        actor_id=self._actor_id(body),
                    )
                ),
            )

        match = re.fullmatch(
            rf"/api/v1/captures/({_PUBLIC_ID_PATTERN})/reopen", path
        )
        if match:
            self._require_body_fields(
                body, frozenset({"expected_revision", "acknowledged"})
            )
            return self._command(
                self.store.reopen_capture(
                    capture_id=match.group(1),
                    expected_revision=self._revision(body),
                    acknowledged=self._acknowledgement(body),
                    actor_id=actor,
                    idempotency_key=key,
                ),
                projector=self._capture_projection,
            )

        raise NotFound("endpoint", path)

    def _authenticate(self, headers: Mapping[str, str], *, client_host: str) -> None:
        if client_host != "127.0.0.1":
            raise PermissionError("loopback_required")
        origin = self._header(headers, "Origin")
        if origin and origin not in self._allowed_origins:
            raise PermissionError("origin_rejected")
        provided = self._header(headers, "X-Cortex-Control-Token")
        if not provided or not hmac.compare_digest(provided, self._access_token):
            raise PermissionError("authentication_required")

    def _public_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return project_public_event(event, encode_cursor=self._encode_cursor)

    def _public_conflict_resource(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """A conflict body's `current`, projected the way its own route would.

        ⟦P9-3⟧ A capture reaches the cockpit from four places -- the list, the
        single GET, its command responses, and the `current` of a 409 -- and
        the web decoder pins one exact key set for all of them, so a capture
        that arrives here a field short is rejected outright rather than
        rendered without it. `already_captured` always carries one, and a
        stale-revision approve or dismiss carries one too.
        """

        if {"capture_key", "payload", "kind", "state"}.issubset(value):
            return self._capture_projection(value)
        return self._public_revision_resource(value)

    @classmethod
    def _public_revision_resource(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        if {"prompt", "options", "kind", "attempt_id"}.issubset(value):
            return project_public_decision(value)
        if {"candidates", "decision", "title", "locator"}.issubset(value):
            return ResearchWorkflowProjector.project_source_gate(value)
        return cls._public_value(dict(value))

    @classmethod
    def _public_value(cls, value: Any) -> Any:
        internal = {
            "claim_expires_at",
            "claim_epoch",
            "claim_owner",
            "dispatch_expires_at",
            "dispatch_owner",
            "runtime_decision_ref",
            "runtime_decision_revision",
            "runtime_identity_version",
            "runtime_slot_id",
            "runtime_artifact_digest",
            "runtime_worker_protocol",
            "state_generation_id",
            "request_hash",
            "operation_id",
            "resume_run_state",
            "resume_attempt_state",
            "engine_ref",
        }
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key in internal:
                    continue
                if key == "aliases":
                    result[key] = cls._public_source_aliases(item)
                else:
                    result[key] = cls._public_value(item)
            return result
        if isinstance(value, list):
            return [cls._public_value(item) for item in value]
        return value

    def _source_knowledge_response(
        self, *, query: Mapping[str, list[str]], source_id: str | None = None
    ) -> APIResponse:
        # Keep unrelated Control routes usable while K1 is integrated separately.
        try:
            raw_limit = self._optional_single_query(query, "limit")
            if raw_limit is not None and re.fullmatch(r"[1-9][0-9]{0,4}", raw_limit) is None:
                raise ValueError("invalid source limit")
            if source_id is None:
                search_query = self._single_query(query, "q").strip()
                limit = self._integer_query(query, "limit", default=10)
                if not 1 <= len(search_query.encode("utf-8")) <= 1_024 or not 1 <= limit <= 50:
                    raise ValueError("invalid source query")
                if any(ord(char) < 32 for char in search_query):
                    raise ValueError("invalid source query")
            else:
                kind = self._single_query(query, "kind", default="notes")
                cursor = self._optional_single_query(query, "cursor")
                limit = self._integer_query(query, "limit", default=20_000)
                if kind not in {"notes", "full_text", "grounding"} or not 1 <= limit <= 20_000:
                    raise ValueError("invalid source query")
                if cursor is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cursor) is None:
                    raise ValueError("invalid source cursor")
        except ValueError:
            return self._problem(400, "source_query_invalid", "Source query is invalid")
        try:
            from ..sources.reader import (
                SourceContentUnavailable,
                SourceKnowledgeReader,
                SourceQueryInvalid,
            )
        except ModuleNotFoundError as exc:
            if exc.name != "cortex_platform.product.sources.reader":
                raise
            return self._problem(
                503, "source_content_unavailable", "Source knowledge is unavailable",
                retryable=True,
            )
        try:
            reader = SourceKnowledgeReader(
                self.store,
                redact_line=lambda line: any(pattern.search(line) for pattern in _SENSITIVE_TEXT_PATTERNS),
            )
            if source_id is None:
                value = reader.search(search_query, limit=limit)
                result = {
                    "query": self._public_source_text(value["query"]),
                    "retrieval_mode": self._public_source_text(value["retrieval_mode"]),
                    "results": [
                        {key: self._public_source_text(item[key]) for key in (
                            "source_id", "canonical_id", "title", "evidence_id",
                            "section", "excerpt", "content_sha256",
                        )}
                        for item in value["results"]
                    ],
                }
            else:
                value = reader.read(source_id, kind=kind, cursor=cursor, limit=limit)
                result = {key: value[key] for key in (
                    "source_id", "canonical_id", "kind", "text", "content_sha256",
                    "start_line", "end_line", "next_cursor",
                )}
                # Text has already been classified with complete original-line
                # context by the reader. Its fragments retain the page budget.
                result = {key: item if key == "text" else self._public_source_text(item)
                          for key, item in result.items()}
            return APIResponse(200, result, headers=(("Cache-Control", "no-store"),))
        except SourceQueryInvalid:
            return self._problem(400, "source_query_invalid", "Source query or cursor is invalid")
        except SourceContentUnavailable:
            return self._problem(
                409, "source_content_unavailable",
                "Source content is unavailable or could not be verified",
            )
        except NotFound:
            return self._problem(404, "not_found", "Source was not found")

    @staticmethod
    def _public_source_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        # Redact per line to preserve K1 citation coordinates. The digest is
        # the stored document identity, not a checksum of this public projection.
        return "\n".join(
            "[redacted]" if any(pattern.search(line) for pattern in _SENSITIVE_TEXT_PATTERNS)
            else line for line in value.split("\n")
        )

    @staticmethod
    def _public_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "workspace_id": value["workspace_id"],
            "thread_id": value["thread_id"],
            "kind": value["kind"],
            "title": ControlAPI._public_metadata_text(
                value.get("title"), fallback="Untitled artifact"
            ),
            "head_artifact_version_id": value["head_artifact_version_id"],
            "head_revision": value["head_revision"],
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
        }

    @staticmethod
    def _public_artifact_version(value: Mapping[str, Any]) -> dict[str, Any]:
        return ResearchWorkflowProjector._artifact_version(value)

    @staticmethod
    def _public_artifact_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "workspace_id": value["workspace_id"],
            "name": ControlAPI._public_metadata_text(
                value.get("name"), fallback="Unnamed snapshot"
            ),
            "members": value["members"],
            "created_at": value["created_at"],
        }

    @staticmethod
    def _require_query_fields(
        query: Mapping[str, list[str]], allowed: frozenset[str]
    ) -> None:
        if set(query) - allowed:
            raise ValueError("query parameter is invalid")

    @staticmethod
    def _require_body_fields(
        body: Mapping[str, Any], expected: frozenset[str]
    ) -> None:
        if set(body) != expected:
            raise ValueError("JSON body fields are invalid")

    @staticmethod
    def _public_metadata_text(value: Any, *, fallback: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 2_000
            or any(ord(character) < 32 for character in value)
            or any(pattern.search(value) for pattern in _SENSITIVE_TEXT_PATTERNS)
        ):
            return fallback
        return value

    @staticmethod
    def _public_source_aliases(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for alias in value[:100]:
            if not isinstance(alias, Mapping):
                continue
            alias_id = alias.get("id")
            authority = alias.get("authority")
            display_value = alias.get("value")
            created_at = alias.get("created_at")
            if (
                not isinstance(alias_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", alias_id)
                is None
                or authority not in {"arxiv", "doi", "sha256", "project"}
                or not isinstance(display_value, str)
                or not isinstance(created_at, str)
                or re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
                    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
                    created_at,
                )
                is None
            ):
                continue
            try:
                if authority == "project":
                    valid = re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", display_value
                    ) is not None
                elif authority == "arxiv":
                    valid = "://" not in display_value
                    if valid:
                        canonicalize_arxiv_id(display_value)
                elif authority == "doi":
                    valid = "://" not in display_value
                    if valid:
                        canonicalize_doi(display_value)
                else:
                    valid = re.fullmatch(
                        r"[0-9a-f]{64}", display_value, re.IGNORECASE
                    ) is not None
            except ValueError:
                valid = False
            if not valid:
                continue
            result.append(
                {
                    "id": alias_id,
                    "authority": authority,
                    "value": display_value,
                    "created_at": created_at,
                }
            )
        return result

    def _encode_cursor(self, cursor: int) -> str:
        payload = f"v1:{cursor}".encode("ascii")
        tag = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()[:12]
        return base64.urlsafe_b64encode(payload + b"." + tag).decode("ascii").rstrip("=")

    def _decode_cursor(self, value: str) -> int:
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(decoded) < 14 or decoded[-13] != ord("."):
                raise ValueError
            payload, tag = decoded[:-13], decoded[-12:]
            expected = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()[:12]
            prefix, number = payload.decode("ascii").split(":", 1)
            if prefix != "v1" or not hmac.compare_digest(tag, expected):
                raise ValueError
            cursor = int(number)
            if cursor < 0:
                raise ValueError
            return cursor
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ValueError("after_cursor is invalid") from exc

    def _cursor_query(self, query: Mapping[str, list[str]], name: str) -> int:
        values = query.get(name)
        if values is None:
            return 0
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{name} query parameter is invalid")
        return self._decode_cursor(values[0])

    @staticmethod
    def _parse_body(body: bytes) -> dict[str, Any]:
        if not body or len(body) > 1_048_576:
            raise ValueError("JSON body is required and must not exceed 1 MiB")
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    # -- ⟦P8⟧ runs this daemon drives ----------------------------------------

    def _workspace_projection(
        self,
        workspace: Mapping[str, Any],
        *,
        machine_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """The workspace as the cockpit sees it, plus whose it is.

        ⟦P9-2⟧ `_thread_projection`'s twin: `engine_owned` is
        `ControlStore.workspace_is_machine` at the time of the read, on every
        route that returns a workspace (create, get, list), never stored. The
        cockpit's picker hides the engine's own `Capture consumer` workspace
        behind a toggle; the server decides whose it is, the web only filters.
        Note this is also the first projection these routes have had -- they
        returned the raw store row, which for a workspace is already the
        public shape.

        ⟦A-1⟧ `machine_ids` is the whole answer for a whole list, asked once;
        without it the single-workspace routes ask for their own id. Never one
        store call per row -- that is the shape the review measured at ~75x on
        100 rows for the thread projection.
        """

        value = dict(self._public_value(workspace))
        workspace_id = str(workspace["id"])
        value["engine_owned"] = (
            workspace_id in machine_ids
            if machine_ids is not None
            else self.store.workspace_is_machine(workspace_id)
        )
        return value

    def _run_projection(
        self, run: Mapping[str, Any], *, engine_owned: bool | None = None
    ) -> dict[str, Any]:
        """The run as the cockpit sees it, plus whose it is.

        ⟦ADJ-A⟧ `engine_owned` is the SAME pair `_machine_run_refusal` asks
        before it refuses a cancel or a pause -- the run owns a workflow, or
        its `create_run` receipt names the capture consumer -- so the cockpit
        can hide exactly the buttons the API would refuse and no others.

        RUN ownership, not the thread's, and the difference is the point: a
        workflow-less run an operator opened on a `capture` thread is theirs
        to end, and ending it is the whole of the `ForeignCarrierRun`
        recovery -- the cockpit is the only surface that can send that cancel.
        Creating a run is still refused by THREAD, which is a different
        question with a different answer.

        `engine_owned` is passed in by the list route, which asks once for the
        whole thread; a single-run route asks for its own id.
        """

        value = dict(self._public_value(run))
        value["engine_owned"] = (
            engine_owned
            if engine_owned is not None
            else self.store.run_is_machine(str(run["id"]))
        )
        return value

    def _capture_projections(
        self, captures: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """The captures as the cockpit sees them, plus what fences each one.

        ⟦V-R3 / P9-3⟧ The `carrier_thread_busy` audit row has named the run
        holding a capture's carrier thread since V-R3, and the DTO did not
        carry it -- so the cockpit could tell an operator that a capture was
        blocked but never which run to go and end. `blocked_by` is computed on
        read like `engine_owned` is, never stored, and it is ALWAYS present:
        `CAPTURE_FIELDS` is both the web decoder's allowlist and its
        required-key list, so a capture DTO that omitted it would be rejected
        outright.

        Applied on every route that returns a capture, including the command
        routes, so a replayed receipt written before this key existed is
        projected with it too rather than reaching the browser a field short.

        One audit read per page, and only for the rows whose CURRENT
        `failure_category` can have an answer -- usually none, and never one
        query per row.

        ⟦BRK-7⟧ And only while somebody is still waiting on the answer. A
        `dismiss` deliberately leaves `failure_category` alone -- it is the
        only in-row record of why the capture was set aside, and nulling it
        would erase the chip on the same card -- so a dismissed capture kept
        naming the run an operator was being told to go and end, for a capture
        the operator had just closed. `reopen_capture` clears the category
        (control/store.py:1181), which is why a reopened capture already stopped
        reporting one; a dismissed capture stops here instead, by state, with
        the category and its chip intact.
        """

        ids = [
            str(capture["id"])
            for capture in captures
            if str(capture.get("failure_category") or "")
            in CAPTURE_BLOCKING_CATEGORIES
            and str(capture.get("state") or "") not in CAPTURE_TERMINAL_STATES
        ]
        blocked = self.store.capture_blocked_by(ids) if ids else {}
        items: list[dict[str, Any]] = []
        for capture in captures:
            value = self._public_value(dict(capture))
            value["blocked_by"] = blocked.get(str(capture["id"]))
            items.append(value)
        return items

    def _capture_projection(self, capture: Mapping[str, Any]) -> dict[str, Any]:
        return self._capture_projections([capture])[0]

    def _thread_projection(
        self, thread: Mapping[str, Any], *, engine_owned: bool | None = None
    ) -> dict[str, Any]:
        """The thread as the cockpit sees it, plus whose it is.

        ⟦ADJ-1⟧ `engine_owned` is `ControlStore.thread_is_machine` at the
        time of the read -- computed on every route that returns a thread
        (create, get, list), never stored -- so the composer can say "this
        thread belongs to the research engine" BEFORE its active-run
        short-circuit, instead of promising an answer on a carrier thread
        whose run is the engine's. One key added; nothing else changes.

        ⟦A-1 / batchK-8⟧ A caller that already knows the answer passes it:
        the LIST route asks `machine_thread_ids` once for the PAGE it is
        about to project rather than opening a connection per row. The
        single-thread routes keep the per-thread predicate, which is one call
        for one thread either way.
        """

        value = dict(self._public_value(thread))
        value["engine_owned"] = (
            self.store.thread_is_machine(str(thread["id"]))
            if engine_owned is None
            else engine_owned
        )
        return value

    def _turn_refusal(
        self,
        actor: str,
        *,
        operation: str,
        key: str,
        request: Mapping[str, Any],
        thread_id: str,
        creating: bool = False,
    ) -> APIResponse | None:
        """Why this daemon could not drive a run, decided before one is written.

        ⟦V6-3⟧ First, whoever the daemon is: a machine thread
        (`ControlStore.thread_is_machine` -- the research engine's capture
        threads, and any thread a workflow was ever written into) never gets
        a run from this API, in any process shape. A run created there would
        be adopted by the capture consumer's carrier logic or left standing
        for ever, and either way it is not the operator's conversation. The
        message route still stores what the operator typed there.

        ⟦V-2⟧ Then, whether there is a turn to drive at all: creating a run on
        a thread with nothing to answer -- no operator message, no transport
        scope bound to it -- committed a `queued` run `run_is_conversation`
        excludes, so the bridge never picked it up and it sat as the thread's
        active run until a message or a cancel cleared it. `creating` scopes
        that refusal to the create route: `resume` and `retry` are about a run
        that already exists, and a run in that shape is exactly the one whose
        self-clearing behaviour must keep working.

        The receipt is consulted first: a command that committed under this
        key is replayed, never refused -- the refusal's "not created" would be
        false about it. Then three shapes of process, three answers. A daemon
        whose managed worker was bound at start owns a turn bridge: the run
        may be created if the worker is still bound (a revoke lands after the
        binding; health re-reads it) and the migration-12 dispatch gate is
        open, and is refused otherwise with the worker's reason or the gate's
        own word -- the same categories the bridge and the orchestrator use,
        reached here before anything durable exists. A daemon with a worker
        it could not bind owns no bridge, and a run it created would sit
        `queued` for the life of the installation, so it is refused with the
        worker's reason. A process with no worker at all (demo mode, the
        store-only tests) keeps creating queued runs: there is nothing here
        to refuse on behalf of.
        """

        replay = self.store.replay_command(
            actor_id=actor, operation=operation, idempotency_key=key, request=request
        )
        if replay is not None:
            # ⟦ADJ-G-2⟧ A replay is the SAME response the command gave, so it
            # needs the same projection: unprojected, the retried create-run
            # answers a Run with no `engine_owned` and the cockpit cannot
            # decode its own idempotent retry.
            return self._command(replay, projector=self._run_projection)
        if self.store.thread_is_machine(thread_id):
            return self._problem(
                409,
                "machine_thread",
                "The thread belongs to the research engine; its runs are "
                "created and driven by the engine only",
            )
        if creating and not self.store.thread_can_carry_a_turn(thread_id):
            return self._problem(
                409,
                "thread_has_no_user_message",
                "The thread has nothing to answer yet; send a message before "
                "starting a run",
            )
        if self._turn_bridge is None:
            if self._managed_worker is None:
                return None
            reason = self._managed_worker_health().get("reason") or "restart_required"
            return self._problem(
                409,
                "managed_worker_unavailable",
                f"No managed worker is bound to run this turn ({reason})",
                retryable=True,
            )
        if not self._managed_worker_bound():
            reason = self._managed_worker_health().get("reason") or "unbound"
            return self._problem(
                409,
                "managed_worker_unavailable",
                f"The managed worker cannot run this turn ({reason})",
                retryable=True,
            )
        if not self.store.runtime_dispatch_enabled():
            return self._problem(
                409,
                "runtime_activation_disabled",
                "Runtime dispatch is disabled; the turn was not created",
                retryable=True,
            )
        return None

    def _machine_run_refusal(
        self,
        actor: str,
        *,
        run_id: str,
        target_state: str,
        key: str,
        expected: int,
    ) -> APIResponse | None:
        """Why this API will not end a run the engine owns, decided before it is written.

        ⟦N-1⟧ A cancel or pause of the engine's carrier -- the run that
        carries a capture's workflow -- would be converged to `canceled` in
        this same request (`_converge_cancellation`), and a terminal carrier
        fences its workflow for ever (`_expect_workflow_run_open`): the
        capture could never be claimed again, and no reopen would help. So
        the run is refused by RUN ownership, never by its thread: it owns a
        workflow instance (every gen-13 carrier, from the transaction that
        creates it), or its `create_run` receipt names the capture consumer's
        own actor (the carrier a pre-V-3 consumer left without a workflow,
        the fact the consumer keys its own repair on) -- the pair
        `ControlStore._MACHINE_RUN_PREDICATE` spells, asked here in one query
        through `run_is_machine` rather than reproduced in two.
        A workflow-less run somebody else opened on a
        `capture` thread stays cancellable: the `ForeignCarrierRun` recovery
        needs the operator to end exactly that run. The receipt is consulted
        first, as in `_turn_refusal`: a command that committed under this
        key is replayed, never refused -- and the request it is asked about
        is the store's own (`transition_request`), so the replay cannot miss.

        ⟦ADJ-4 / batchK-9⟧ This is the route's answer, not the only guard:
        the store itself refuses the same two targets on an engine-owned run
        for any actor but the consumer's (`ControlStore.transition_run` ->
        `MachineRunRefused`, mapped to this same 409 `machine_run` in
        `handle`), which is what covers the Telegram adapter's
        `_apply_run_action` and any other writer. The two used to be
        different rules -- the store's was a strict subset -- so which answer
        an operator got depended on which door they knocked at.

        ⟦batchO ADJ-2⟧ They are now one SOURCE and two predicates, which is
        not the same thing as one predicate: this route asks
        `ControlStore._MACHINE_RUN_PREDICATE` (control/store.py:11242) through
        `run_is_machine` (control/store.py:11189), and the store asks the NARROWER
        `_ENGINE_OWNED_RUN_PREDICATE` (control/store.py:11270) through
        `_engine_owned_run` (control/store.py:11248) on the writing transaction's own
        connection. Both are generated by `ControlStore._machine_run_predicate`
        (control/store.py:461), whose docstring argues the one clause that still
        differs -- the store's workflow half is `research.capture` only
        (control/store.py:489-497) -- and names why it cannot be reached today. So a
        run carrying some OTHER workflow is the one shape on which the two
        doors still answer differently: this route refuses the cancel and the
        store commits it. That single intended disagreement is pinned by
        `test_one_source_decides_who_owns_a_run_at_every_door[other_workflow]`,
        which asserts it rather than leaving it implicit.

        It is also the same pair `_run_projection` puts on the wire as
        `engine_owned`, so the cockpit hides exactly the buttons THIS guard
        would refuse.
        """

        replay = self.store.replay_command(
            actor_id=actor,
            operation=self.store.transition_operation(run_id, target_state),
            idempotency_key=key,
            request=self.store.transition_request(
                target_state=target_state, expected_revision=expected
            ),
        )
        if replay is not None:
            if target_state == "cancel_requested":
                self._forget(run_id)
            # ⟦ADJ-G-2⟧ Same as the create path: the replayed cancel returns a
            # Run, so it returns a projected one.
            return self._command(replay, projector=self._run_projection)
        if self.store.run_is_machine(run_id):
            return self._problem(
                409,
                "machine_run",
                "The run belongs to the research engine; it is ended by the "
                "engine only",
            )
        return None

    def _unsupported_pause_refusal(
        self,
        actor: str,
        *,
        run_id: str,
        key: str,
        expected: int,
    ) -> APIResponse | None:
        """Why this API will not accept a pause the runtime cannot perform.

        ⟦P9-3 BRK-4⟧ `POST /runs/{id}/pause` used to return 200 with the run in
        `pause_requested` and then quietly undo itself: the Hermes adapter
        reports `pause=False`, so the delivered action is rejected and the
        store rolls the run back to `running` with the answer delivered
        anyway. Every surface said the pause committed, and none of them said
        it had been undone -- the rejection is not in `_PUBLIC_EVENT_TYPES`, so
        the cockpit rendered the literal string `event.redacted`, and the
        rollback writes no run event at all, leaving a public timeline whose
        `run.completed {from: running}` follows a `run.pause_requested` that
        nothing ever un-did.

        The outcome is knowable before the write -- the capability is a
        property of the bound runtime, not of the request -- so the honest
        answer is to refuse instead of committing something that will be
        undone. Same 409 shape as `machine_run`, and the receipt is consulted
        first for the same reason: a pause that DID commit under this key,
        against a runtime that supported it, is replayed rather than refused.

        Unknown is not refused. `runtime_supports` answers None until a turn
        has told this daemon something, and inventing a capability report is
        the failure being fixed, not a fix.
        """

        bridge = self._turn_bridge
        supports = getattr(bridge, "runtime_supports", None) if bridge else None
        if not callable(supports):
            return None
        try:
            supported = supports("pause")
        except Exception:  # noqa: BLE001 - a surface never fails on a probe
            return None
        if supported is not False:
            return None
        replay = self.store.replay_command(
            actor_id=actor,
            operation=self.store.transition_operation(run_id, "pause_requested"),
            idempotency_key=key,
            request=self.store.transition_request(
                target_state="pause_requested", expected_revision=expected
            ),
        )
        if replay is not None:
            return self._command(replay, projector=self._run_projection)
        return self._problem(
            409,
            "pause_unsupported",
            "The runtime running this turn cannot pause it; cancel it instead",
        )

    def _drive(self, result: Any) -> None:
        """Hand the run's thread to the turn bridge, after the command committed.

        The same seam the Telegram adapter uses (`bind_turn_sink`), with the
        same two rules: only for a command that committed rather than replayed
        -- a replay is the same request twice, and its run was already asked
        for -- and never allowed to fail the request, because the run is
        durable whether or not anything is going to drive it. `submit` itself
        never blocks and never raises; a bridge that cannot take more records
        `turn_queue_full` on its own status.
        """

        bridge = self._turn_bridge
        if bridge is None or result.replayed:
            return
        run = result.value
        if str(run.get("state")) not in DISPATCHABLE_RUN_STATES:
            return
        try:
            bridge.submit(str(run["thread_id"]))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the run is durable; the drive is not
            pass

    def _drive_followup(self, thread_id: str, result: Any) -> None:
        """A user message on a thread with a run in flight joins that run.

        ⟦P8⟧ The Telegram adapter submits the thread after every appended
        message; the bridge remembers a thread whose turn is queued or running
        as a follow-up and submits it again when the turn ends, so the message
        gets a turn of its own. The API does the same for a message appended
        while the thread has a run in flight -- and only then. On an idle
        thread the cockpit creates the run itself, from the revision the
        append produced, and a submit here would race it with a second run.
        A run parked on a decision (or paused, resuming, being cancelled) is
        waiting on the OPERATOR, whose answer this same API and the cockpit
        can give: submitting would make the bridge abandon it (F-B6's escape,
        written for a transport with no approval vocabulary) and expire the
        decision -- so the message is stored and the run left standing. A
        closed dispatch gate leaves the run standing too, rather than ending
        it typed under the message's own promise. And a run the engine owns
        is never handed over from here (`run_is_conversation`).
        """

        bridge = self._turn_bridge
        if bridge is None or result.replayed:
            return
        try:
            if not self.store.runtime_dispatch_enabled():
                return
            if self.store.thread_is_machine(thread_id):
                # ⟦V6-3⟧ Stored, never driven: the engine's thread.
                return
            active = self.store.get_thread(thread_id).get("active_run_id")
            if not active:
                return
            if str(self.store.get_run(str(active)).get("state")) not in FOLLOWUP_RUN_STATES:
                return
            if not self.store.run_is_conversation(str(active)):
                return
            bridge.submit(thread_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the message is durable; the drive is not
            pass

    def _forget(self, run_id: str) -> None:
        """Tell the bridge the operator cancelled this run, so its re-drive dies now.

        ⟦P8 V-1⟧ The bridge re-drives a run the worker refused by run id and
        re-reads it every tick, so a cancelled run would be dropped at the next
        tick anyway; this drops it before that tick asks. Never allowed to
        fail the request: the cancellation is durable whether or not a bridge
        is there to hear about it.
        """

        bridge = self._turn_bridge
        if bridge is None:
            return
        try:
            bridge.forget(str(run_id))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - see above
            pass

    def _converge_cancellation(self, result: Any, *, actor: str) -> Any:
        """A cancellation nothing has reserved is `canceled` now, not for ever.

        `RunOrchestrator._converge_unbound_cancellation` ends a
        `cancel_requested` run from inside a live dispatch, fenced on the
        dispatch owner it holds. A run cancelled before any dispatch reserved
        it has no owner and no dispatch coming, so the same store transition
        is applied here with nothing to fence on. If a dispatch reserved the
        attempt in the same instant, the revision check refuses this and that
        dispatch's own convergence finishes the cancellation.
        """

        run = result.value
        if result.replayed or str(run.get("state")) != "cancel_requested":
            return result
        attempt_id = run.get("active_attempt_id")
        if not attempt_id:
            return result
        attempt = self.store.get_attempt(str(attempt_id))
        if attempt.get("dispatch_owner") or attempt.get("runtime_binding_id"):
            return result
        try:
            return self.store.fail_unbound_run(
                run_id=str(run["id"]),
                attempt_id=str(attempt_id),
                expected_revision=int(run["revision"]),
                category=CANCELED_BEFORE_BINDING,
                actor_id=actor,
                idempotency_key=f"api-cancel-{run['id']}-{run['revision']}"[:64],
            )
        except (RevisionConflict, InvalidTransition):
            return result

    def _command(
        self,
        result: Any,
        *,
        projector: Any | None = None,
    ) -> APIResponse:
        headers = (("Idempotency-Replayed", "true"),) if result.replayed else ()
        return APIResponse(
            result.status_code,
            (projector or self._public_value)(result.value),
            headers=headers,
        )

    @staticmethod
    def _problem(
        status: int,
        category: str,
        title: str,
        *,
        retryable: bool = False,
        current: dict[str, Any] | None = None,
    ) -> APIResponse:
        payload: dict[str, Any] = {
            "category": category,
            "owner": "cortexd",
            "retryable": retryable,
            "status": status,
            "title": title,
            "type": f"urn:cortex:problem:{category}",
        }
        if current is not None:
            payload["current"] = current
        return APIResponse(status, payload, "application/problem+json")

    @classmethod
    def _actor(cls, headers: Mapping[str, str]) -> str:
        """Who a command is recorded as, from the door it arrived through.

        ⟦P8-08⟧ A loopback caller is the operator sitting at the machine, and
        stays `local-operator`. A request the web adapter forwarded from the
        public front door carries the Cloudflare Access identity that adapter
        verified; it is recorded as `access:<identity>` so the audit row and
        the receipt name the person who acted rather than the door they came
        through. The header is never accepted as given by a caller: the
        adapter strips it from every request and re-sets it only after the
        assertion verified, and this API is reachable on loopback with the
        control token only. An identity that could not have come from the
        adapter is refused rather than quietly recorded as the operator.
        """
        identity = cls._header(headers, _ACCESS_IDENTITY_HEADER)
        if not identity:
            return _LOCAL_OPERATOR_ACTOR
        if (
            len(identity) > _ACCESS_IDENTITY_MAXIMUM
            or not _ACCESS_IDENTITY_PATTERN.fullmatch(identity)
        ):
            raise ValueError("access identity is invalid")
        return f"{_ACCESS_ACTOR_PREFIX}{identity}"

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        lowered = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered:
                return value
        return ""

    @staticmethod
    def _actor_id(body: Mapping[str, Any]) -> str:
        """The operator name the audit row carries, bounded and printable."""

        value = body.get("actor_id")
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 200
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("actor_id is invalid")
        return value

    @staticmethod
    def _string(body: Mapping[str, Any], name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    @staticmethod
    def _optional_string(body: Mapping[str, Any], name: str) -> str | None:
        value = body.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _acknowledgement(body: Mapping[str, Any]) -> bool:
        # Re-opening an uncertain capture may cause a duplicate ingest, so the
        # operator's acknowledgement has to be literally true -- a truthy
        # string or 1 is a client bug, not consent.
        value = body.get("acknowledged")
        if type(value) is not bool or not value:
            raise ValueError("acknowledged must be true")
        return True

    @staticmethod
    def _revision(body: Mapping[str, Any]) -> int:
        value = body.get("expected_revision")
        if type(value) is not int or value < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        return value

    @staticmethod
    def _single_query(
        query: Mapping[str, list[str]], name: str, *, default: str | None = None
    ) -> str:
        values = query.get(name)
        if values is None and default is not None:
            return default
        if values is None or len(values) != 1 or not values[0]:
            raise ValueError(f"{name} query parameter is required exactly once")
        return values[0]

    @classmethod
    def _integer_query(
        cls,
        query: Mapping[str, list[str]],
        name: str,
        *,
        default: int,
    ) -> int:
        value = cls._single_query(query, name, default=str(default))
        if not value.isdigit():
            raise ValueError(f"{name} must be a non-negative integer")
        return int(value)

    @classmethod
    def _optional_integer_query(
        cls, query: Mapping[str, list[str]], name: str
    ) -> int | None:
        """`_integer_query` for a parameter whose absence is not a default.

        ⟦batchK-8⟧ `GET /threads` has no numeric default to fall back on: no
        `limit` means the whole list, which is a different answer from any
        number, so it cannot be spelled as `_integer_query(default=...)`.
        A `limit` that is present is validated exactly as everywhere else.
        """

        value = cls._optional_single_query(query, name)
        if value is None:
            return None
        if not value.isdigit():
            raise ValueError(f"{name} must be a non-negative integer")
        return int(value)

    @staticmethod
    def _optional_single_query(
        query: Mapping[str, list[str]], name: str
    ) -> str | None:
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{name} query parameter is invalid")
        return values[0]
