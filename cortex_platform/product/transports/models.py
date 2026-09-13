"""Strict transport-facing DTOs and sanitized adapter results."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_UPDATE_BYTES = 1_000_000
MAX_TEXT_BYTES = 16_384
MAX_CALLBACK_BYTES = 64
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 5_000
MAX_MEDIA_ITEMS = 10

#: The namespace a Telegram COMMAND REPLY's delivery lives under in Control's
#: ledger. `transport_delivery_projections.event_id` is a free 1..500 character
#: column with no reference to `run_events` (⟦schema 18⟧), which is what lets a
#: reply be a first-class durable delivery without inventing a run event to
#: hang it on. The adapter mints it and the drain branches on it, so it lives
#: here rather than in either of them; no run event id can carry it.
COMMAND_REPLY_PREFIX = "cmd:"


class TransportProblem(Exception):
    """A stable transport error that never carries a raw provider exception."""

    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms


@dataclass(frozen=True)
class TelegramScope:
    """A private Telegram routing scope used only at the adapter boundary."""

    bot_identity: str
    chat_id: int
    topic_id: int | None

    def canonical(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "bot": self.bot_identity,
                "chat": self.chat_id,
                "topic": self.topic_id if self.topic_id is not None else "root",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class TelegramMedia:
    kind: str
    file_id: str
    file_unique_id: str | None
    file_name: str | None
    mime_type: str | None
    file_size: int | None


@dataclass(frozen=True)
class TelegramMessage:
    message_id: int
    sender_id: int
    chat_id: int
    chat_type: str
    topic_id: int | None
    text: str | None
    media: TelegramMedia | None


@dataclass(frozen=True)
class TelegramCallback:
    callback_query_id: str
    sender_id: int
    chat_id: int
    chat_type: str
    topic_id: int | None
    message_id: int
    data: str


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message: TelegramMessage | None = None
    callback: TelegramCallback | None = None

    @property
    def sender_id(self) -> int:
        item = self.message or self.callback
        if item is None:  # pragma: no cover - guarded by the parser
            raise TransportProblem("invalid_request")
        return item.sender_id

    @property
    def chat_id(self) -> int:
        item = self.message or self.callback
        if item is None:  # pragma: no cover - guarded by the parser
            raise TransportProblem("invalid_request")
        return item.chat_id

    @property
    def topic_id(self) -> int | None:
        item = self.message or self.callback
        if item is None:  # pragma: no cover - guarded by the parser
            raise TransportProblem("invalid_request")
        return item.topic_id

    @property
    def identity(self) -> str:
        if self.callback is not None:
            return f"callback:{self.callback.callback_query_id}"
        return f"update:{self.update_id}"

    @classmethod
    def parse(cls, payload: bytes | str | Mapping[str, Any]) -> TelegramUpdate:
        data = _load_payload(payload)
        update_id = _integer(data.get("update_id"), "update_id", minimum=0)
        has_message = "message" in data
        has_callback = "callback_query" in data
        if has_message == has_callback:
            raise TransportProblem("ambiguous_update")
        if has_message:
            return cls(update_id=update_id, message=_parse_message(data["message"]))
        return cls(
            update_id=update_id, callback=_parse_callback(data["callback_query"])
        )


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    category: str
    action: str
    response_text: str
    mutated: bool = False
    replayed: bool = False
    retryable: bool = False
    retry_after_ms: int | None = None
    state: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class TelegramButton:
    label: str
    callback_data: str


@dataclass(frozen=True)
class TelegramDestination:
    chat_id: int
    topic_id: int | None = None


@dataclass(frozen=True)
class OutboundMessage:
    destination: TelegramDestination
    text: str
    parse_mode: str
    buttons: tuple[TelegramButton, ...]
    idempotency_key: str


@dataclass(frozen=True)
class DeliveryResult:
    category: str
    delivered: bool
    duplicate: bool = False
    ignored: bool = False
    retryable: bool = False
    retry_after_ms: int | None = None
    chunks: int = 0


@dataclass(frozen=True)
class TelegramChunkSendOutcome:
    status: Literal["accepted", "rate_limited", "retryable_before_send", "rejected"]
    provider_message_ref: str | None = field(default=None, repr=False)
    retry_after_ms: int | None = None
    category: str | None = None


def _load_payload(payload: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_UPDATE_BYTES:
            raise TransportProblem("update_too_large")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportProblem("invalid_request") from exc
        data = _loads_strict(text)
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_UPDATE_BYTES:
            raise TransportProblem("update_too_large")
        data = _loads_strict(payload)
    elif isinstance(payload, Mapping):
        data = dict(payload)
        try:
            encoded = json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TransportProblem("invalid_request") from exc
        if len(encoded) > MAX_UPDATE_BYTES:
            raise TransportProblem("update_too_large")
    else:
        raise TransportProblem("invalid_request")
    if not isinstance(data, dict):
        raise TransportProblem("invalid_request")
    _validate_tree(data)
    return data


def _loads_strict(text: str) -> Any:
    def reject_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise TransportProblem("duplicate_json_key")
            value[key] = item
        return value

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                TransportProblem("invalid_request")
            ),
        )
    except TransportProblem:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TransportProblem("invalid_request") from exc


def _validate_tree(root: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise TransportProblem("update_too_complex")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TransportProblem("invalid_request")
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise TransportProblem("invalid_request")


def _parse_message(value: Any) -> TelegramMessage:
    item = _mapping(value, "message")
    sender = _mapping(item.get("from"), "from")
    chat = _mapping(item.get("chat"), "chat")
    text = _optional_string(
        item.get("text"), "text", maximum=MAX_TEXT_BYTES, multiline=True
    )
    caption = _optional_string(
        item.get("caption"), "caption", maximum=MAX_TEXT_BYTES, multiline=True
    )
    if text is not None and caption is not None:
        raise TransportProblem("ambiguous_update")
    media = _parse_media(item)
    if text is None and caption is None and media is None:
        raise TransportProblem("unsupported_update")
    return TelegramMessage(
        message_id=_integer(item.get("message_id"), "message_id", minimum=0),
        sender_id=_integer(sender.get("id"), "sender.id"),
        chat_id=_integer(chat.get("id"), "chat.id"),
        chat_type=_string(chat.get("type"), "chat.type", maximum=32),
        topic_id=_optional_integer(
            item.get("message_thread_id"), "message_thread_id", minimum=0
        ),
        text=text if text is not None else caption,
        media=media,
    )


def _parse_callback(value: Any) -> TelegramCallback:
    item = _mapping(value, "callback_query")
    if item.get("inline_message_id") is not None:
        raise TransportProblem("ambiguous_scope")
    sender = _mapping(item.get("from"), "from")
    message = _mapping(item.get("message"), "message")
    chat = _mapping(message.get("chat"), "chat")
    return TelegramCallback(
        callback_query_id=_string(item.get("id"), "callback_query.id", maximum=128),
        sender_id=_integer(sender.get("id"), "sender.id"),
        chat_id=_integer(chat.get("id"), "chat.id"),
        chat_type=_string(chat.get("type"), "chat.type", maximum=32),
        topic_id=_optional_integer(
            message.get("message_thread_id"), "message_thread_id", minimum=0
        ),
        message_id=_integer(message.get("message_id"), "message_id", minimum=0),
        data=_string(
            item.get("data"), "callback_query.data", maximum=MAX_CALLBACK_BYTES
        ),
    )


def _parse_media(item: Mapping[str, Any]) -> TelegramMedia | None:
    present = [name for name in ("document", "photo", "voice", "audio") if name in item]
    if len(present) > 1:
        raise TransportProblem("ambiguous_update")
    if not present:
        return None
    kind = present[0]
    raw = item[kind]
    if kind == "photo":
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MEDIA_ITEMS:
            raise TransportProblem("invalid_request")
        media = _mapping(raw[-1], "photo")
    else:
        media = _mapping(raw, kind)
    return TelegramMedia(
        kind=kind,
        file_id=_string(media.get("file_id"), "file_id", maximum=512),
        file_unique_id=_optional_string(
            media.get("file_unique_id"), "file_unique_id", maximum=512
        ),
        file_name=_optional_string(media.get("file_name"), "file_name", maximum=512),
        mime_type=_optional_string(media.get("mime_type"), "mime_type", maximum=200),
        file_size=_optional_integer(media.get("file_size"), "file_size", minimum=0),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TransportProblem("invalid_request")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise TransportProblem("invalid_request")
    if minimum is not None and value < minimum:
        raise TransportProblem("invalid_request")
    if not -(2**63) <= value < 2**63:
        raise TransportProblem("invalid_request")
    return value


def _optional_integer(
    value: Any, name: str, *, minimum: int | None = None
) -> int | None:
    if value is None:
        return None
    return _integer(value, name, minimum=minimum)


def _string(value: Any, name: str, *, maximum: int, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise TransportProblem("invalid_request")
    allowed_controls = "\n\t" if multiline else ""
    if any(ord(char) < 32 and char not in allowed_controls for char in value):
        raise TransportProblem("invalid_request")
    return value


def _optional_string(
    value: Any, name: str, *, maximum: int, multiline: bool = False
) -> str | None:
    if value is None:
        return None
    return _string(value, name, maximum=maximum, multiline=multiline)
