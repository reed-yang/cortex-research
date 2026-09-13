"""Closed schemas for the managed worker v2 protocol.

S3.2 left a channel that carried exactly one reply per request and nothing
else. A turn has to emit events while it runs and accept a decision while it is
parked inside an approval, so ⟦AMD-4⟧ gives every frame a `frame`
discriminator, correlates every reply by `request_id`, and admits exactly one
unsolicited frame — `turn.event`, routed to a turn by `operation_id`.

A second fd was rejected: framing buys the same thing and leaves S3.4's
seatbelt profile with one less inherited descriptor to reason about.

Both sets stay closed. That is the whole reason the supervisor is entitled to
believe the worker's answers about itself: an open set would let a compromised
slot introduce a frame the supervisor has no opinion about.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PROTOCOL_V2 = "cortex-worker/2"

FRAME_REPLY = "reply"
FRAME_EVENT = "event"

_REQUEST_FIELDS = {"protocol", "request_id", "token", "method", "params"}
_DESCRIPTOR_FIELDS = {
    "schema_version",
    "slot_path",
    "slot_id",
    "state_generation_id",
    "release_id",
    "expected_artifact_digest",
    "expected_manifest_sha256",
    "expected_content_tree_sha256",
    "expected_interpreter_sha256",
    "interpreter_path",
    "worker_entrypoint",
    "state_dir",
    "worker_protocol",
}
_ERROR_CATEGORIES = {
    "protocol_violation",
    "identity_mismatch",
    "ledger_unavailable",
    "operation_conflict",
    "operation_unknown",
    "not_measured",
    # ⟦AMD-2⟧: the fork executes content out of `HERMES_HOME` at import time, so
    # the worker refuses to import it while that directory could contribute
    # code. A distinct category because it is the operator's misconfiguration,
    # not a protocol fault and not an identity fault.
    "hermes_home_unsafe",
    # The fork itself is unimportable from the slot's content root.
    "runtime_unavailable",
    # A turn method named an operation that is not the open turn.
    "turn_unknown",
}
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_METHOD_FIELDS = {
    "identity.measure": set(),
    "health.check": set(),
    "operation.begin": {"operation_id", "kind", "request_digest"},
    "operation.finish": {"operation_id", "outcome", "result_digest"},
    "operation.status": {"operation_id"},
    "shutdown": set(),
    # ⟦AMD-4⟧, the four turn methods. `turn.event` is in the closed set because
    # its params shape is validated on the way *out*; it is refused on the way
    # in by `_WORKER_TO_SUPERVISOR` below.
    "turn.begin": {"operation_id", "request_digest", "request"},
    "turn.event": {"operation_id", "sequence", "event"},
    "turn.resolve": {"operation_id", "decision"},
    "turn.cancel": {"operation_id"},
    # ⟦AMD-1⟧, the three transport methods. All three are product-initiated
    # request/reply frames, never a worker-driven stream: the product asks for
    # one poll at a time and only while its own transport gate is open, so a
    # `disable` ends the inbound loop at the next frame rather than leaving a
    # socket the product no longer authorizes.
    "telegram.capabilities": set(),
    # The send params are the frozen request `HermesTelegramClient` already
    # serializes: `operation_id` IS the permit reference (it is the chunk's
    # operation id, minted with the permit and single-use), `chat_id`/`topic_id`
    # are the destination, and the rest is the frozen chunk.
    "telegram.send": {
        "schema_version",
        "operation_id",
        "chat_id",
        "topic_id",
        "text",
        "parse_mode",
        "buttons",
    },
    "telegram.poll": {"offset", "timeout_seconds"},
}
# The predecessor's `serve_v2` treated any unmatched method as `operation.status`
# (an else-branch, not a dispatch table), so a slot could have answered a
# supervisor-only method by accident. Direction is now explicit.
_WORKER_TO_SUPERVISOR = {"turn.event"}
_EVENT_FIELDS = {"kind", "payload"}
_DECISION_FIELDS = {"decision_id", "choice"}
_REPLY_FIELDS = {"protocol", "frame", "request_id", "operation_id", "ok"}
_EVENT_FRAME_FIELDS = {"protocol", "frame", "operation_id", "sequence", "event"}


class ProtocolViolation(ValueError):
    """A request or descriptor violates the closed worker protocol."""


@dataclass(frozen=True)
class WorkerRequest:
    request_id: str
    method: str
    params: Mapping[str, object]


@dataclass(frozen=True)
class WorkerError:
    category: str
    message: str

    def __post_init__(self) -> None:
        if self.category not in _ERROR_CATEGORIES:
            raise ValueError("unknown worker error category")
        if not self.message:
            raise ValueError("worker error message must not be empty")


@dataclass(frozen=True)
class WorkerResponse:
    request_id: str
    result: object | None = None
    error: WorkerError | None = None
    # ⟦AMD-4⟧: present on every reply, `None` on the handshake methods, so the
    # supervisor's reader thread can attribute a reply to a turn without
    # consulting the request it is answering.
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("response request_id must not be empty")
        if self.error is not None and self.result is not None:
            raise ValueError("response cannot contain both result and error")
        if self.operation_id is not None and not _OPERATION_ID.fullmatch(
            self.operation_id
        ):
            raise ValueError("response operation_id is invalid")


@dataclass(frozen=True)
class WorkerEvent:
    """The one unsolicited frame: a turn reporting on itself while it runs."""

    operation_id: str
    sequence: int
    event: Mapping[str, object]

    def __post_init__(self) -> None:
        if not _OPERATION_ID.fullmatch(self.operation_id):
            raise ValueError("event operation_id is invalid")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("event sequence must be a non-negative integer")
        _validate_event(self.event)


@dataclass(frozen=True)
class SlotInterpreterDescriptor:
    schema_version: int
    slot_path: Path
    slot_id: str
    state_generation_id: str
    release_id: str
    expected_artifact_digest: str
    expected_manifest_sha256: str
    expected_content_tree_sha256: str
    # ⟦AMD-3⟧: two identities, named apart. The manifest's `archive_sha256` is an
    # *archive* digest and only ever names the interpreter root; this is the
    # digest of the interpreter binary itself, measured at stage by
    # `probe_python_runtime` out of the tree it executed. The worker compares it
    # against the bytes it is actually running from.
    expected_interpreter_sha256: str
    interpreter_path: Path
    worker_entrypoint: str
    state_dir: Path
    worker_protocol: str

    @classmethod
    def load(cls, path: Path) -> SlotInterpreterDescriptor:
        try:
            raw = json.loads(path.read_bytes(), object_pairs_hook=_closed_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolViolation("descriptor is invalid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != _DESCRIPTOR_FIELDS:
            raise ProtocolViolation("descriptor fields do not match schema")
        if raw["schema_version"] != 1 or type(raw["schema_version"]) is not int:
            raise ProtocolViolation("descriptor schema version is invalid")
        if raw["worker_protocol"] != PROTOCOL_V2:
            raise ProtocolViolation("descriptor protocol is invalid")
        for name in ("slot_id", "state_generation_id", "release_id", "worker_entrypoint"):
            _nonempty_text(raw, name, "descriptor")
        for name in (
            "expected_artifact_digest",
            "expected_manifest_sha256",
            "expected_content_tree_sha256",
            "expected_interpreter_sha256",
        ):
            if not _SHA256.fullmatch(_nonempty_text(raw, name, "descriptor")):
                raise ProtocolViolation(f"descriptor {name} digest is invalid")
        paths: dict[str, Path] = {}
        for name in ("slot_path", "interpreter_path", "state_dir"):
            value = Path(_nonempty_text(raw, name, "descriptor"))
            if not value.is_absolute():
                raise ProtocolViolation(f"descriptor {name} must be absolute")
            paths[name] = value
        slot = paths["slot_path"].resolve(strict=False)
        state = paths["state_dir"].resolve(strict=False)
        if state == slot or slot in state.parents:
            raise ProtocolViolation("descriptor state_dir must be outside slot_path")
        return cls(
            schema_version=1,
            slot_path=paths["slot_path"],
            slot_id=str(raw["slot_id"]),
            state_generation_id=str(raw["state_generation_id"]),
            release_id=str(raw["release_id"]),
            expected_artifact_digest=str(raw["expected_artifact_digest"]),
            expected_manifest_sha256=str(raw["expected_manifest_sha256"]),
            expected_content_tree_sha256=str(raw["expected_content_tree_sha256"]),
            expected_interpreter_sha256=str(raw["expected_interpreter_sha256"]),
            interpreter_path=paths["interpreter_path"],
            worker_entrypoint=str(raw["worker_entrypoint"]),
            state_dir=paths["state_dir"],
            worker_protocol=PROTOCOL_V2,
        )


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolViolation("JSON contains duplicate keys")
        value[key] = item
    return value


def _nonempty_text(raw: Mapping[str, object], name: str, context: str) -> str:
    value = raw[name]
    if not isinstance(value, str) or not value or "\0" in value:
        raise ProtocolViolation(f"{context} {name} must be non-empty text")
    return value


def _validate_event(event: object) -> Mapping[str, object]:
    if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
        raise ProtocolViolation("turn event does not match schema")
    _nonempty_text(event, "kind", "turn event")
    if not isinstance(event["payload"], dict):
        raise ProtocolViolation("turn event payload must be an object")
    return event


def _validate_decision(decision: object) -> Mapping[str, object]:
    if not isinstance(decision, dict) or set(decision) != _DECISION_FIELDS:
        raise ProtocolViolation("turn decision does not match schema")
    # `choice` stays opaque *here* on purpose, and stays that way after S3.4.
    # This module is byte-identical to the product's `worker_protocol.py` — a
    # test pins it — and a relative `from .approval import ...` would resolve to
    # two different modules on the two sides. The closed vocabulary is enforced
    # in `turn.py`, before the answer reaches the parked callback, and refused
    # by `serve.py` as a `protocol_violation`.
    for name in _DECISION_FIELDS:
        _nonempty_text(decision, name, "turn decision")
    return decision


def _validate_params(method: str, params: object) -> dict[str, object]:
    if not isinstance(params, dict) or set(params) != _METHOD_FIELDS[method]:
        raise ProtocolViolation(f"{method} params do not match schema")
    # A turn's operation id is a ledger key and matches `_OPERATION_ID`. A
    # transport send's is a Control chunk operation id -- `<delivery>:chunk:<n>`
    # -- which carries colons the ledger pattern does not admit, so it is
    # validated by `_validate_telegram_send` against the shape Control mints
    # rather than against a pattern that would refuse every real send.
    if "operation_id" in params and method != "telegram.send":
        operation_id = params["operation_id"]
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise ProtocolViolation("operation_id is invalid")
    if method == "operation.begin":
        _nonempty_text(params, "kind", "operation.begin")
        digest = params["request_digest"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ProtocolViolation("request digest is invalid")
    if method == "operation.finish":
        if params["outcome"] not in {"committed", "failed"}:
            raise ProtocolViolation("operation outcome is invalid")
        digest = params["result_digest"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ProtocolViolation("result digest is invalid")
    if method == "turn.begin":
        digest = params["request_digest"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ProtocolViolation("turn request digest is invalid")
        if not isinstance(params["request"], dict):
            raise ProtocolViolation("turn request must be an object")
    if method == "turn.event":
        sequence = params["sequence"]
        if type(sequence) is not int or sequence < 0:
            raise ProtocolViolation("turn event sequence is invalid")
        _validate_event(params["event"])
    if method == "turn.resolve":
        _validate_decision(params["decision"])
    if method == "telegram.send":
        _validate_telegram_send(params)
    if method == "telegram.poll":
        _validate_telegram_poll(params)
    return params


#: Pinned rather than negotiated: the adapter's parser accepts exactly these two
#: update kinds, so asking Telegram for anything else would either be discarded
#: or would grow the parser's surface without a decision.
TELEGRAM_ALLOWED_UPDATES = ("message", "callback_query")
#: A poll is a request/reply frame like any other, so its long-poll bound has to
#: be small enough that a `disable` is honoured promptly and far below the
#: fork's 200 s conflict-retry ladder.
TELEGRAM_MAX_POLL_SECONDS = 30
#: The margin the supervisor adds on top of the worker's own getUpdates timeout
#: before it gives up on the frame.
TELEGRAM_POLL_TIMEOUT_MARGIN_SECONDS = 10.0
#: Control mints `<delivery-operation-id>:chunk:<index>`; the colon is the
#: separator, which is why this is not the ledger's operation-id pattern.
_TELEGRAM_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_TELEGRAM_PARSE_MODES = {"MarkdownV2"}
_TELEGRAM_BUTTON_FIELDS = {"label", "callback_data"}


def _validate_telegram_send(params: Mapping[str, object]) -> None:
    if params["schema_version"] != 1 or type(params["schema_version"]) is not int:
        raise ProtocolViolation("telegram.send schema version is invalid")
    operation_id = params["operation_id"]
    if (
        not isinstance(operation_id, str)
        or _TELEGRAM_OPERATION_ID.fullmatch(operation_id) is None
    ):
        raise ProtocolViolation("telegram.send operation_id is invalid")
    chat_id = params["chat_id"]
    if type(chat_id) is not int:
        raise ProtocolViolation("telegram.send chat_id is invalid")
    topic_id = params["topic_id"]
    if topic_id is not None and (type(topic_id) is not int or topic_id < 1):
        raise ProtocolViolation("telegram.send topic_id is invalid")
    text = params["text"]
    if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 4096:
        raise ProtocolViolation("telegram.send text is invalid")
    if params["parse_mode"] not in _TELEGRAM_PARSE_MODES:
        raise ProtocolViolation("telegram.send parse_mode is invalid")
    buttons = params["buttons"]
    if not isinstance(buttons, list) or len(buttons) > 6:
        raise ProtocolViolation("telegram.send buttons are invalid")
    for button in buttons:
        if not isinstance(button, dict) or set(button) != _TELEGRAM_BUTTON_FIELDS:
            raise ProtocolViolation("telegram.send button does not match schema")
        label = button["label"]
        callback_data = button["callback_data"]
        if not isinstance(label, str) or not 1 <= len(label) <= 64:
            raise ProtocolViolation("telegram.send button label is invalid")
        if (
            not isinstance(callback_data, str)
            or not 1 <= len(callback_data.encode("utf-8")) <= 64
        ):
            raise ProtocolViolation("telegram.send button callback_data is invalid")


def _validate_telegram_poll(params: Mapping[str, object]) -> None:
    offset = params["offset"]
    if offset is not None and (type(offset) is not int or offset < 0):
        raise ProtocolViolation("telegram.poll offset is invalid")
    timeout_seconds = params["timeout_seconds"]
    if (
        type(timeout_seconds) is not int
        or not 0 <= timeout_seconds <= TELEGRAM_MAX_POLL_SECONDS
    ):
        raise ProtocolViolation("telegram.poll timeout is invalid")


def parse_request(line: bytes, *, token: str) -> WorkerRequest:
    try:
        raw = json.loads(line, object_pairs_hook=_closed_object)
    except ProtocolViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolViolation("request contains invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != _REQUEST_FIELDS:
        raise ProtocolViolation("closed request fields do not match schema")
    if raw["protocol"] != PROTOCOL_V2:
        raise ProtocolViolation("worker protocol is invalid")
    request_id = raw["request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolViolation("request_id is invalid")
    if raw["token"] != token:
        raise ProtocolViolation("worker token is invalid")
    method = raw["method"]
    if not isinstance(method, str) or method not in _METHOD_FIELDS:
        raise ProtocolViolation("worker method is invalid")
    if method in _WORKER_TO_SUPERVISOR:
        raise ProtocolViolation("worker method is not accepted inbound")
    params = _validate_params(method, raw["params"])
    return WorkerRequest(request_id=request_id, method=method, params=params)


def _encode(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def encode_response(response: WorkerResponse) -> bytes:
    if response.error is None:
        value: dict[str, object] = {
            "protocol": PROTOCOL_V2,
            "frame": FRAME_REPLY,
            "request_id": response.request_id,
            "operation_id": response.operation_id,
            "ok": True,
            "result": response.result,
        }
    else:
        value = {
            "protocol": PROTOCOL_V2,
            "frame": FRAME_REPLY,
            "request_id": response.request_id,
            "operation_id": response.operation_id,
            "ok": False,
            "error": {
                "category": response.error.category,
                "message": response.error.message,
            },
        }
    return _encode(value)


def encode_event(event: WorkerEvent) -> bytes:
    return _encode(
        {
            "protocol": PROTOCOL_V2,
            "frame": FRAME_EVENT,
            "operation_id": event.operation_id,
            "sequence": event.sequence,
            "event": dict(event.event),
        }
    )


def parse_frame(line: bytes) -> WorkerResponse | WorkerEvent:
    """Decode one outbound frame, discriminating on `frame` before anything else.

    The supervisor's reader thread runs this on every line the worker writes, so
    it is the single place a malformed or misdirected frame is rejected — before
    a `request_id` is looked up and before an `operation_id` is routed.
    """

    try:
        raw = json.loads(line, object_pairs_hook=_closed_object)
    except ProtocolViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolViolation("frame contains invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ProtocolViolation("frame must be an object")
    if raw.get("protocol") != PROTOCOL_V2:
        raise ProtocolViolation("frame protocol is invalid")
    frame = raw.get("frame")
    if frame == FRAME_REPLY:
        return _parse_reply(raw)
    if frame == FRAME_EVENT:
        return _parse_event_frame(raw)
    raise ProtocolViolation("frame discriminator is invalid")


def _parse_reply(raw: Mapping[str, object]) -> WorkerResponse:
    ok = raw.get("ok")
    if type(ok) is not bool:
        raise ProtocolViolation("reply ok flag is invalid")
    expected = _REPLY_FIELDS | {"result" if ok else "error"}
    if set(raw) != expected:
        raise ProtocolViolation("reply fields do not match schema")
    request_id = raw["request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolViolation("reply request_id is invalid")
    operation_id = raw["operation_id"]
    if operation_id is not None and (
        not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id)
    ):
        raise ProtocolViolation("reply operation_id is invalid")
    if ok:
        return WorkerResponse(
            request_id=request_id,
            result=raw["result"],
            operation_id=operation_id,
        )
    error = raw["error"]
    if not isinstance(error, dict) or set(error) != {"category", "message"}:
        raise ProtocolViolation("reply error does not match schema")
    category = error["category"]
    message = error["message"]
    if not isinstance(category, str) or category not in _ERROR_CATEGORIES:
        raise ProtocolViolation("reply error category is unknown")
    if not isinstance(message, str) or not message:
        raise ProtocolViolation("reply error message is invalid")
    return WorkerResponse(
        request_id=request_id,
        error=WorkerError(category, message),
        operation_id=operation_id,
    )


def _parse_event_frame(raw: Mapping[str, object]) -> WorkerEvent:
    if set(raw) != _EVENT_FRAME_FIELDS:
        raise ProtocolViolation("event fields do not match schema")
    operation_id = raw["operation_id"]
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ProtocolViolation("event operation_id is invalid")
    sequence = raw["sequence"]
    if type(sequence) is not int or sequence < 0:
        raise ProtocolViolation("event sequence is invalid")
    return WorkerEvent(
        operation_id=operation_id,
        sequence=sequence,
        event=_validate_event(raw["event"]),
    )
