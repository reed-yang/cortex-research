"""External bootstrap-secret resolution for the loopback access boundary."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit


_DERIVATION_DOMAIN = b"cortex-private-access-bootstrap-v1"


class SecretResolutionError(RuntimeError):
    """Raised when an external bootstrap secret cannot be resolved safely."""


@dataclass(frozen=True)
class SecretCommandResult:
    returncode: int
    stdout: str = ""


class SecretCommandRunner(Protocol):
    def run(self, arguments: Sequence[str]) -> SecretCommandResult: ...


class StandardSecretCommandRunner:
    def run(self, arguments: Sequence[str]) -> SecretCommandResult:
        try:
            completed = subprocess.run(
                list(arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return SecretCommandResult(124)
        return SecretCommandResult(completed.returncode, completed.stdout)


def resolve_secret(
    reference: str,
    *,
    environ: Mapping[str, str] | None = None,
    runner: SecretCommandRunner | None = None,
) -> bytes:
    parsed = urlsplit(reference)
    if parsed.scheme == "env":
        value = (environ or os.environ).get(parsed.netloc)
        if value is None:
            raise SecretResolutionError("bootstrap secret reference is unavailable")
    elif parsed.scheme == "keychain":
        account = parsed.path.removeprefix("/")
        result = (runner or StandardSecretCommandRunner()).run(
            (
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                parsed.netloc,
                "-a",
                account,
                "-w",
            )
        )
        if result.returncode != 0:
            raise SecretResolutionError("bootstrap secret reference is unavailable")
        value = result.stdout.rstrip("\r\n")
    else:
        raise SecretResolutionError("bootstrap secret reference is unsupported")
    encoded = value.encode("utf-8")
    if not 32 <= len(encoded) <= 1024 or any(
        byte < 0x20 or byte == 0x7F for byte in encoded
    ):
        raise SecretResolutionError("bootstrap secret does not meet strength requirements")
    return encoded


def derive_bootstrap_token(secret: bytes) -> str:
    if not 32 <= len(secret) <= 1024:
        raise SecretResolutionError("bootstrap secret does not meet strength requirements")
    digest = hmac.new(secret, _DERIVATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
