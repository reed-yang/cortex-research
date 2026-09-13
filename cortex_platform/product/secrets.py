"""Resolving product secret references at the effect boundary.

`config.py` has always validated `secret_refs` and never resolved one, so no
provider credential has ever reached a running process. This resolves them,
under one rule: a resolved value is carried by an object that will not print
itself, and it is handed only to an effect process. It is never written to
`control.db`, never placed in the Web environment, and never interpolated into
a message, a log line, or an exception.
"""

from __future__ import annotations

import hashlib
import hmac
import subprocess
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from .config import (
    _CREDENTIAL_KEY_PATTERN,
    _ENVIRONMENT_NAME_PATTERN,
    _KEYCHAIN_IDENTIFIER_PATTERN,
    _SECRET_ALIAS_PATTERN,
)

_KEYCHAIN_TOOL = "/usr/bin/security"
_KEYCHAIN_TIMEOUT = 10.0

KeychainLookup = Callable[[str, str], str | None]


class SecretResolutionError(RuntimeError):
    """A secret reference could not be resolved. Never carries the value."""

    def __init__(self, alias: str, message: str) -> None:
        super().__init__(f"secret {alias!r}: {message}")
        self.alias = alias


class SecretNotFound(SecretResolutionError):
    """The reference was well formed, but nothing is stored behind it."""


class SecretValue:
    """One resolved credential that declines to print itself.

    Python cannot stop a caller from revealing a value it holds, but nearly
    every real leak is accidental -- an f-string in a log line, a dataclass
    repr, an exception message built from context. Requiring `reveal()` makes
    every intentional use greppable and every accidental one inert.
    """

    __slots__ = ("_alias", "_value")

    def __init__(self, alias: str, value: str) -> None:
        self._alias = alias
        self._value = value

    @property
    def alias(self) -> str:
        return self._alias

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"<SecretValue {self._alias!r} redacted>"

    __str__ = __repr__

    def __reduce__(self) -> tuple:
        # Redacting __repr__ is not enough on its own: a __slots__ class with
        # no state hook pickles its slots verbatim, so the default reduce puts
        # the plaintext on the wire. macOS spawns multiprocessing children,
        # which is exactly the path that would carry a credential to a worker,
        # so serializing one has to be an error rather than a silent leak.
        raise TypeError(
            f"secret {self._alias!r} cannot be serialized; "
            "pass the reference and resolve it in the destination process"
        )

    # copy/deepcopy consult __reduce_ex__ too, so they need their own hooks or
    # they would fail on a legitimate, non-escaping operation.
    def __copy__(self) -> SecretValue:
        return SecretValue(self._alias, self._value)

    def __deepcopy__(self, memo: dict) -> SecretValue:
        return SecretValue(self._alias, self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        # Constant-time, so comparing two secrets cannot be turned into an
        # oracle for one of them.
        return hmac.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash(hashlib.sha256(self._value.encode("utf-8")).digest())


def _system_keychain_lookup(service: str, account: str) -> str | None:
    """Read one generic password from the macOS keychain.

    The value arrives on stdout; stderr is discarded rather than surfaced,
    because a failure message from the tool can quote what it was asked for.
    """

    try:
        completed = subprocess.run(
            [
                _KEYCHAIN_TOOL,
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\n")


class SecretResolver:
    """Resolve validated `secret_refs` entries into revealable values."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        keychain_lookup: KeychainLookup | None = None,
    ) -> None:
        self._environment = environment
        self._keychain_lookup = keychain_lookup or _system_keychain_lookup

    def resolve(self, alias: str, reference: str) -> SecretValue:
        if (
            not isinstance(alias, str)
            or _SECRET_ALIAS_PATTERN.fullmatch(alias) is None
            or _CREDENTIAL_KEY_PATTERN.search(alias)
        ):
            raise SecretResolutionError(str(alias), "alias is not a logical name")
        if not isinstance(reference, str) or not reference:
            raise SecretResolutionError(alias, "reference is missing")
        try:
            parsed = urlsplit(reference)
            port = parsed.port
        except ValueError as exc:
            raise SecretResolutionError(alias, "reference is malformed") from exc
        if (
            parsed.scheme not in {"env", "keychain"}
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SecretResolutionError(alias, "reference scheme is not supported")
        if parsed.scheme == "env":
            return self._resolve_environment(alias, parsed.netloc, parsed.path)
        return self._resolve_keychain(alias, parsed.netloc, parsed.path)

    def resolve_all(self, references: Mapping[str, str]) -> dict[str, SecretValue]:
        return {
            alias: self.resolve(alias, reference)
            for alias, reference in sorted(references.items())
        }

    def _resolve_environment(self, alias: str, name: str, path: str) -> SecretValue:
        if path or _ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None:
            raise SecretResolutionError(
                alias, "reference must use env://VARIABLE_NAME"
            )
        value = self._environment.get(name)
        if not value:
            # An empty variable is a misconfiguration, not a credential: left
            # alone it surfaces much later as an authentication failure.
            raise SecretNotFound(alias, f"environment variable {name} is not set")
        return SecretValue(alias, value)

    def _resolve_keychain(self, alias: str, service: str, path: str) -> SecretValue:
        account = path.removeprefix("/")
        if (
            _KEYCHAIN_IDENTIFIER_PATTERN.fullmatch(service) is None
            or path != f"/{account}"
            or _KEYCHAIN_IDENTIFIER_PATTERN.fullmatch(account) is None
        ):
            raise SecretResolutionError(
                alias, "reference must use keychain://service/account"
            )
        value = self._keychain_lookup(service, account)
        if not value:
            raise SecretNotFound(
                alias, f"keychain item {service}/{account} is not available"
            )
        return SecretValue(alias, value)
