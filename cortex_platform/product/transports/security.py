"""Opaque token, text sanitization, and Telegram Markdown helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from .models import TransportProblem
from .ports import OpaqueTarget, OpaqueTargetPort

_MARKDOWN_V2_RESERVED = frozenset("_*[]()~`>#+-=|{}.!\\")
_UNSAFE_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|password)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?:file://)?/(?:Users|home|root|var|tmp|etc)/[^\s]*"),
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
)


@dataclass(frozen=True, repr=False)
class PreparedOpaqueToken:
    token: str
    token_digest: str
    target: OpaqueTarget


def stable_digest(key: bytes, domain: str, value: str) -> str:
    digest = hmac.new(key, f"{domain}\0{value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sanitize_text(value: object, *, maximum: int = 1_200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.replace("\x00", " ").split())
    for pattern in _UNSAFE_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned[:maximum].rstrip()


def escape_markdown_v2(value: str) -> str:
    return "".join(
        f"\\{char}" if char in _MARKDOWN_V2_RESERVED else char for char in value
    )


def chunk_markdown_v2(value: str, *, maximum: int = 4_096) -> tuple[str, ...]:
    if maximum < 2:
        raise ValueError("maximum must be at least two characters")
    escaped = escape_markdown_v2(value)
    if not escaped:
        return ("",)
    chunks: list[str] = []
    remaining = escaped
    while remaining:
        if len(remaining) <= maximum:
            chunks.append(remaining)
            break
        boundary = max(
            1,
            max(
                remaining.rfind("\n", 0, maximum + 1),
                remaining.rfind(" ", 0, maximum + 1),
            ),
        )
        if boundary < maximum // 2:
            boundary = maximum
        while boundary > 1 and _ends_with_odd_backslashes(remaining[:boundary]):
            boundary -= 1
        chunk = remaining[:boundary].rstrip()
        if not chunk:
            chunk = remaining[:boundary]
        chunks.append(chunk)
        remaining = remaining[boundary:].lstrip()
    return tuple(chunks)


def _ends_with_odd_backslashes(value: str) -> bool:
    count = 0
    for char in reversed(value):
        if char != "\\":
            break
        count += 1
    return count % 2 == 1


class OpaqueTokenService:
    """Issue signed random tokens whose complete targets remain server-side."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        store: OpaqueTargetPort,
        verification_keys: tuple[bytes, ...] = (),
        clock: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("signing_key must contain at least 32 bytes")
        if (
            not isinstance(verification_keys, tuple)
            or len(verification_keys) > 4
            or any(not isinstance(key, bytes) or len(key) < 32 for key in verification_keys)
        ):
            raise ValueError("verification_keys must contain at most four keys")
        self._key = bytes(signing_key)
        self._verification_keys = (self._key,) + tuple(
            bytes(key) for key in verification_keys if key != self._key
        )
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_bytes = random_bytes or secrets.token_bytes

    def issue(
        self,
        *,
        namespace: str,
        purpose: str,
        resource_kind: str,
        resource_id: str,
        expected_revision: int | None = None,
        choice: str | None = None,
        scope_digest: str | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> str:
        prepared = self.prepare(
            namespace=namespace,
            purpose=purpose,
            resource_kind=resource_kind,
            resource_id=resource_id,
            expected_revision=expected_revision,
            choice=choice,
            scope_digest=scope_digest,
            ttl=ttl,
        )
        self._store.put(prepared.token_digest, prepared.target)
        return prepared.token

    def prepare(
        self,
        *,
        namespace: str,
        purpose: str,
        resource_kind: str,
        resource_id: str,
        expected_revision: int | None = None,
        choice: str | None = None,
        scope_digest: str | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> PreparedOpaqueToken:
        """Prepare a signed target without persisting it before an atomic freeze."""

        if namespace not in {"action", "deep_link"}:
            raise ValueError("unsupported token namespace")
        if ttl <= timedelta(0) or ttl > timedelta(minutes=10):
            raise ValueError("token ttl must be between zero and ten minutes")
        random_id = _encode(self._random_bytes(16))
        prefix = "ac1" if namespace == "action" else "dl1"
        signature = _encode(
            hmac.new(
                self._key, f"{prefix}.{random_id}".encode(), hashlib.sha256
            ).digest()[:12]
        )
        token = f"{prefix}.{random_id}.{signature}"
        digest = hashlib.sha256(token.encode()).hexdigest()
        target = OpaqueTarget(
            namespace=namespace,  # type: ignore[arg-type]
            purpose=purpose,
            resource_kind=resource_kind,
            resource_id=resource_id,
            expected_revision=expected_revision,
            choice=choice,
            scope_digest=scope_digest,
            expires_at=self._clock() + ttl,
        )
        return PreparedOpaqueToken(token=token, token_digest=digest, target=target)

    def resolve(
        self,
        token: str,
        *,
        namespace: str,
        purpose: str,
        scope_digest: str | None = None,
        consumer: str | None = None,
        consume: bool = True,
    ) -> OpaqueTarget:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 128
            or not token.isascii()
        ):
            raise TransportProblem("invalid_token")
        prefix = "ac1" if namespace == "action" else "dl1"
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != prefix:
            raise TransportProblem("invalid_token")
        signed_value = f"{parts[0]}.{parts[1]}".encode()
        valid_signature = False
        for key in self._verification_keys:
            expected = _encode(
                hmac.new(key, signed_value, hashlib.sha256).digest()[:12]
            )
            valid_signature = hmac.compare_digest(expected, parts[2]) or valid_signature
        if not valid_signature:
            raise TransportProblem("invalid_token")
        digest = hashlib.sha256(token.encode()).hexdigest()
        target = self._store.claim(digest, consumer=consumer, consume=False)
        if target is None:
            raise TransportProblem("token_unavailable")
        now = self._clock()
        expires_at = target.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now >= expires_at:
            raise TransportProblem("token_expired")
        if target.namespace != namespace or target.purpose != purpose:
            raise TransportProblem("token_scope_mismatch")
        if scope_digest is not None and target.scope_digest != scope_digest:
            raise TransportProblem("token_scope_mismatch")
        if consume:
            claimed = self._store.claim(digest, consumer=consumer, consume=True)
            if claimed is None:
                raise TransportProblem("token_unavailable")
        return target

    def deep_link(
        self,
        *,
        base_url: str,
        resource_kind: str,
        resource_id: str,
        purpose: str = "open",
        ttl: timedelta = timedelta(minutes=5),
    ) -> str:
        token = self.issue(
            namespace="deep_link",
            purpose=purpose,
            resource_kind=resource_kind,
            resource_id=resource_id,
            ttl=ttl,
        )
        return f"{base_url}?{urlencode({'token': token})}"

    def prepare_deep_link(
        self,
        *,
        base_url: str,
        resource_kind: str,
        resource_id: str,
        purpose: str = "open",
        ttl: timedelta = timedelta(minutes=5),
    ) -> tuple[str, PreparedOpaqueToken]:
        prepared = self.prepare(
            namespace="deep_link",
            purpose=purpose,
            resource_kind=resource_kind,
            resource_id=resource_id,
            ttl=ttl,
        )
        return f"{base_url}?{urlencode({'token': prepared.token})}", prepared


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")
