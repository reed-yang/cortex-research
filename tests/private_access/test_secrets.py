from __future__ import annotations

import hashlib
from typing import Sequence

import pytest

from deployment.private_access.secrets import (
    SecretCommandResult,
    SecretResolutionError,
    derive_bootstrap_token,
    resolve_secret,
)


class FakeRunner:
    def __init__(self, result: SecretCommandResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: Sequence[str]) -> SecretCommandResult:
        self.calls.append(tuple(arguments))
        return self.result


def test_resolves_environment_reference_without_exposing_value() -> None:
    secret = "a" * 32
    assert resolve_secret("env://CORTEX_ACCESS_SESSION", environ={
        "CORTEX_ACCESS_SESSION": secret
    }) == secret.encode()


def test_resolves_keychain_reference_without_shell() -> None:
    runner = FakeRunner(SecretCommandResult(0, "b" * 32 + "\n"))

    value = resolve_secret(
        "keychain://cortex/private-access-session", runner=runner
    )

    assert value == b"b" * 32
    assert runner.calls == [
        (
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "cortex",
            "-a",
            "private-access-session",
            "-w",
        )
    ]


@pytest.mark.parametrize("value", ["short", "a" * 31, "a" * 32 + "\n"])
def test_rejects_weak_or_header_unsafe_secret(value: str) -> None:
    with pytest.raises(SecretResolutionError, match="strength"):
        resolve_secret("env://CORTEX_ACCESS_SESSION", environ={
            "CORTEX_ACCESS_SESSION": value
        })


def test_keychain_failure_is_sanitized() -> None:
    runner = FakeRunner(SecretCommandResult(44, "private diagnostic"))
    with pytest.raises(SecretResolutionError) as raised:
        resolve_secret(
            "keychain://cortex/private-access-session", runner=runner
        )
    assert "private diagnostic" not in str(raised.value)


def test_derived_token_is_domain_separated_and_does_not_reveal_secret() -> None:
    secret = b"correct horse battery staple value"
    token = derive_bootstrap_token(secret)

    assert len(token) == 43
    assert secret.decode() not in token
    assert token != hashlib.sha256(secret).hexdigest()
    assert token == derive_bootstrap_token(secret)
