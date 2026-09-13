"""What the daemon may write down about a failure: bounded and secret-free.

⟦P5.6⟧ The sixth window on the mini failed its inbound poller in bursts and
the only record was `poller_last_error: WorkerProtocolError` -- a class name,
because the message was the one thing nobody had decided was safe to keep.
This module is that decision. Every failure message the daemon retains for an
operator (status surface or log line) passes through `redact` first, so the
policy lives in one place rather than at every `str(exc)`.

The patterns are deliberately generous: a bot token, a `/bot<token>/` path
segment, any URL query string, URL userinfo, any `key=value` whose key says
it is a secret, provider-key-shaped strings, a 64-hex secret and the daemon's
own control token shape. Redacting a harmless value costs a few characters
of diagnosis; keeping a secret costs the operator's bot. `redact` is
idempotent: a redacted line redacts to itself.
"""

from __future__ import annotations

import re

#: One line, and never a payload: the length a status field or a log line may
#: carry for one failure. The same bound `TransportCapabilityUnavailable`
#: already applies to a worker's message.
DETAIL_LIMIT = 200

_REDACTED = "[redacted]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A Bot API path segment carries the whole token: `/bot123456:ABC-.../`.
    (re.compile(r"/bot[^/\s\"']+"), "/bot" + _REDACTED),
    # A Telegram bot token on its own: numeric id, colon, secret.
    (re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}"), _REDACTED),
    # Provider keys of the `sk-...` family.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), _REDACTED),
    # URL userinfo: `scheme://user:pass@host` (a proxied base URL).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@"), r"\1" + _REDACTED + "@"),
    # Any URL query string: the one place a token travels in a GET.
    (re.compile(r"\?[^\s\"'<>]+"), "?" + _REDACTED),
    # A 64-hex secret: a hex-encoded 32-byte key or token. (A sha256 digest
    # has the same shape and is cut too -- recorded as a trade-off.)
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{64}(?![0-9A-Za-z])"), _REDACTED),
    # The daemon's own control token: `secrets.token_urlsafe(32)`, exactly 43
    # base64url characters with nothing of that alphabet on either side.
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"), _REDACTED),
    # `Authorization: Bearer <credential>` -- the credential is the word after.
    # The lookahead keeps an already-redacted value from being matched again
    # (which left a stray `]`); nothing is excluded from the class itself.
    (re.compile(r"(?i)\bbearer\s+(?!\[redacted\])\S+"), "Bearer " + _REDACTED),
    # `token=...`, `api_key: ...`, `password = ...`.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|authorization)"
            r"(\s*[=:]\s*)(?!\[redacted\])\S+"
        ),
        r"\1\2" + _REDACTED,
    ),
)


def redact(text: object, *, limit: int = DETAIL_LIMIT) -> str:
    """One bounded line with anything credential-shaped replaced."""

    collapsed = " ".join(str(text).split())
    for pattern, replacement in _PATTERNS:
        collapsed = pattern.sub(replacement, collapsed)
    return collapsed[:limit]
