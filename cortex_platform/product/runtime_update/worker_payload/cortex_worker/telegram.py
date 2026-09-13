"""The worker half of `cortex.telegram.transport/1` (D-P5-1, ruling (a)).

The socket lives here, in a product-owned module the slot's content-tree
digest attests, and it is stdlib `urllib` and nothing else. The fork's
`gateway/platforms/telegram.py` never enters the product path: it brings its
own poller lock, its own 200 s conflict-retry ladder, its own
`drop_pending_updates` policy and its own command registration, so every
upstream Hermes update would have to be re-aligned against decisions the
product has already made in `control.db`. Option (a) puts the socket behind
the capability-probed boundary ADR-0008 requires and leaves the permit state
machine in charge of when it may be used.

The credential is read from the environment once per call and never returned,
logged, or put in an error message. It reaches this process only through
`worker_environment`'s allowlisted key, and only while the product's transport
gate is `enable`.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Sequence

from .protocol import TELEGRAM_ALLOWED_UPDATES, TELEGRAM_MAX_POLL_SECONDS

#: The production default. `TELEGRAM_API_BASE_URL` exists so a test or an
#: acceptance can point the worker at a loopback stand-in; it is an allowlisted
#: worker environment key set by the product, not a field any operator-facing
#: configuration can reach.
DEFAULT_BASE_URL = "https://api.telegram.org"
BASE_URL_ENV = "TELEGRAM_API_BASE_URL"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN_RESEARCH"

#: What this worker can prove it does. The product hashes exactly these seven
#: fields into the capability binding digest a frozen projection is bound to,
#: so changing any of them invalidates every projection frozen against the old
#: answer rather than silently changing behaviour under one.
CAPABILITY = {
    "protocol": "cortex.telegram.transport/1",
    "send_message": True,
    "topics": True,
    "inline_buttons": True,
    "markdown_v2": True,
    # The product mints its own single-use operation id per chunk and the
    # permit is consumed before the send, so the transport does not claim
    # provider-side idempotency it cannot demonstrate.
    "provider_idempotency": False,
    # `getMessage` is not a Bot API method: once a send is in flight there is
    # no way to ask Telegram whether it landed. Claiming otherwise is what
    # would turn an `outcome_unknown` into a false `delivered`.
    "outcome_query": False,
}

_CONNECT_TIMEOUT_MARGIN = 10.0
_MAX_RESPONSE_BYTES = 1 << 20


class TelegramTransportError(RuntimeError):
    """A transport failure whose detail must not leave this module."""


class TelegramRefusedBeforeSend(TelegramTransportError):
    """A refusal that provably happened before any byte left this process.

    `urllib` cannot tell a caller whether a request was written, so the
    classification cannot be an enumeration of post-write failures -- one
    missing entry there turns an already-delivered message into a re-send. It
    is instead the other way round: only two shapes qualify as "nothing was
    sent", this module refusing to build the request at all (no base url, no
    credential) and a connect phase that never established a socket, and
    everything else falls through to `outcome_unknown`.
    """


def _base_url() -> str:
    value = os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TelegramRefusedBeforeSend("telegram base url is invalid")
    if parsed.query or parsed.fragment:
        raise TelegramRefusedBeforeSend("telegram base url is invalid")
    # Plain HTTP is only ever a loopback stand-in. Refusing it anywhere else
    # keeps the fake-server injection point from becoming a downgrade.
    host = parsed.hostname or ""
    if parsed.scheme == "http" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise TelegramRefusedBeforeSend("telegram base url must be https")
    return value.rstrip("/")


def _token() -> str:
    token = os.environ.get(TOKEN_ENV, "")
    if not token or "/" in token or "\n" in token:
        # The gate is closed, or a relaunch after the window closed did not
        # carry the key. No request was built, so nothing was sent.
        raise TelegramRefusedBeforeSend("telegram credential is unavailable")
    return token


def _call(method: str, payload: Mapping[str, object], *, timeout: float) -> object:
    """One Bot API call. The URL carries the token, so nothing here is logged."""

    url = f"{_base_url()}/bot{_token()}/{method}"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl.create_default_context() if url.startswith("https:") else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        except OSError:
            raise TelegramTransportError("telegram call failed") from None
    except urllib.error.URLError as exc:
        # A connect phase that never established a socket is the only network
        # failure that proves nothing was written. A timeout, a reset and a
        # short read all happen after the request may already have gone out.
        if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
            raise TelegramRefusedBeforeSend("telegram call was refused") from None
        raise TelegramTransportError("telegram call failed") from None
    except (ConnectionRefusedError, socket.gaierror):
        raise TelegramRefusedBeforeSend("telegram call was refused") from None
    except (OSError, ValueError):
        raise TelegramTransportError("telegram call failed") from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise TelegramTransportError("telegram response is oversized")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelegramTransportError("telegram response is not JSON") from None
    if not isinstance(document, dict):
        raise TelegramTransportError("telegram response is not an object")
    return document


def capabilities() -> dict[str, object]:
    return dict(CAPABILITY)


def _reply_markup(buttons: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    if not buttons:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": str(button["label"]),
                    "callback_data": str(button["callback_data"]),
                }
            ]
            for button in buttons
        ]
    }


def send(params: Mapping[str, object], *, timeout: float) -> dict[str, object]:
    """Send one frozen chunk and report an outcome the product can act on.

    The four statuses are the ones `_parse_send_result` accepts, and the
    distinction that matters is `outcome_unknown`: a request that reached the
    socket and did not come back with an answer must never be reported as
    anything else, because the product's only safe response is to stop rather
    than to retry a message the operator may already have received.
    """

    payload: dict[str, object] = {
        "chat_id": params["chat_id"],
        "text": params["text"],
        "parse_mode": params["parse_mode"],
    }
    topic_id = params["topic_id"]
    if topic_id is not None:
        payload["message_thread_id"] = topic_id
    markup = _reply_markup(list(params["buttons"]))  # type: ignore[arg-type]
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        document = _call("sendMessage", payload, timeout=timeout)
    except TelegramRefusedBeforeSend:
        # Provably nothing was written: the product may claim the chunk again.
        return {"status": "retryable_before_send"}
    except TelegramTransportError:
        # The request may have reached Telegram. A timeout, a reset, a body
        # too large to hold and a body that is not JSON are all indistinguish-
        # able from a delivered message whose receipt was lost, and there is
        # no `getMessage` to ask. The product's only safe answer is to stop.
        return {"status": "outcome_unknown"}
    assert isinstance(document, dict)
    if document.get("ok") is True:
        result = document.get("result")
        if not isinstance(result, dict) or not isinstance(
            result.get("message_id"), int
        ):
            return {"status": "outcome_unknown"}
        return {
            "status": "accepted",
            "provider_message_ref": str(result["message_id"]),
        }
    parameters = document.get("parameters")
    if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
        return {
            "status": "rate_limited",
            "retry_after_ms": int(parameters["retry_after"]) * 1000,
        }
    code = document.get("error_code")
    if isinstance(code, int) and 400 <= code < 500:
        # A 4xx is Telegram refusing the request itself; the category is the
        # code, never the description, which can echo the message back.
        return {"status": "rejected", "category": f"telegram_{code}"}
    return {"status": "outcome_unknown"}


def poll(params: Mapping[str, object], *, timeout: float) -> dict[str, object]:
    """One `getUpdates` round trip, `allowed_updates` pinned.

    Request/reply, not a stream: the product issues this only while
    `telegram_dispatch_enabled()` is true, so a `disable` ends the inbound
    loop at the next frame instead of leaving a long poll running against a
    token the product no longer holds (⟦AMD-4⟧).
    """

    long_poll = int(params["timeout_seconds"])
    if not 0 <= long_poll <= TELEGRAM_MAX_POLL_SECONDS:
        raise TelegramTransportError("telegram poll timeout is invalid")
    payload: dict[str, object] = {
        "timeout": long_poll,
        "allowed_updates": list(TELEGRAM_ALLOWED_UPDATES),
    }
    offset = params["offset"]
    if offset is not None:
        payload["offset"] = int(offset)
    try:
        document = _call("getUpdates", payload, timeout=timeout)
    except TelegramTransportError:
        return {"status": "unavailable", "updates": []}
    assert isinstance(document, dict)
    if document.get("ok") is not True:
        # A persistent 409 lands here. It is not fatal and not special-cased:
        # the product sees an empty poll and decides, because the decision to
        # keep holding the token belongs to the window, not to the socket.
        return {"status": "unavailable", "updates": []}
    result = document.get("result")
    if not isinstance(result, list):
        return {"status": "unavailable", "updates": []}
    updates: list[dict[str, object]] = []
    for item in result:
        if not isinstance(item, dict) or not isinstance(item.get("update_id"), int):
            continue
        if not any(kind in item for kind in TELEGRAM_ALLOWED_UPDATES):
            continue
        updates.append(item)
    return {"status": "ok", "updates": updates}


def poll_timeout(long_poll_seconds: int) -> float:
    """The frame deadline: the long poll plus a connect/read margin."""

    return float(long_poll_seconds) + _CONNECT_TIMEOUT_MARGIN
