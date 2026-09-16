"""Age store parsing, durable references, and component credential boundaries."""
import subprocess
from types import SimpleNamespace

import pytest

from cortex_platform.product import secrets
from cortex_platform.product.config import ConfigError, _validate_secret_reference
from cortex_platform.product.engine.service import _secret_provider, unusable_secret_aliases
from cortex_platform.product.secrets import SecretNotFound, SecretResolutionError, SecretResolver
from tools.with_engine_secrets import command_environment


@pytest.mark.parametrize("reference", [
    "age://../GLM_API_KEY", "age:///GLM_API_KEY", "age://cortex/../KEY",
    "age://cortex/GLM_API_KEY?x=1", "age://user@cortex/KEY",
    "age://cortex:22/KEY", "age://cortex/%2e%2e", "age://cortex/key",
])
def test_invalid_references_never_reach_lookup(reference):
    def forbidden(*args):
        pytest.fail("invalid reference reached storage")
    with pytest.raises(ConfigError):
        _validate_secret_reference("glm", reference)
    with pytest.raises(SecretResolutionError):
        SecretResolver(environment={}, age_lookup=forbidden).resolve("glm", reference)


def test_durable_resolution_and_scoped_command(monkeypatch):
    monkeypatch.setattr(secrets, "_system_age_lookup", lambda store, name: {
        "GLM_API_ID": "dummy-id", "GLM_API_KEY": "dummy-key"
    }.get(name))
    config = {"secret_refs": {"glm": "age://cortex/GLM_API_KEY",
                              "glm-app-id": "age://cortex/GLM_API_ID",
                              "research_bot": "age://cortex/TELEGRAM_TOKEN"}}
    for alias, reference in config["secret_refs"].items():
        _validate_secret_reference(alias, reference)
    assert unusable_secret_aliases(config) == ()
    provider, dropped = _secret_provider(config, keychain_only=True)
    assert not dropped
    assert set(provider()) == {"glm", "glm-app-id"}
    child = command_environment(config, {})
    assert set(child) == {"GLM_API_ID", "GLM_API_KEY"}
    result = subprocess.run(
        [__import__('sys').executable, "-c",
         "import os; assert os.environ['GLM_API_KEY'] == 'dummy-key'; assert 'TELEGRAM_TOKEN' not in os.environ"],
        env=child, capture_output=True, check=False,
    )
    assert result.returncode == 0


@pytest.fixture
def age_store(tmp_path, monkeypatch):
    root = tmp_path / ".config/cortex"
    root.mkdir(parents=True)
    (root / "secrets.age").write_bytes(b"dummy encrypted store")
    monkeypatch.setattr(secrets.Path, "home", lambda: tmp_path)
    original = secrets.Path.is_file
    monkeypatch.setattr(secrets.Path, "is_file", lambda p: True if str(p) == "/opt/homebrew/bin/age" else original(p))
    return root


def test_parser_preserves_literals_without_shell_execution(age_store, monkeypatch):
    plaintext = "export GLM_API_KEY='literal $(touch /never-execute) `whoami` # spaces'\nOTHER=ignored\n"
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=plaintext)
    monkeypatch.setattr(secrets.subprocess, "run", run)
    value = SecretResolver(environment={}).resolve("glm", "age://cortex/GLM_API_KEY")
    assert value.reveal() == "literal $(touch /never-execute) `whoami` # spaces"
    assert value.reveal() not in repr(value)
    assert calls[0][0][0] == "/opt/homebrew/bin/age"
    assert "shell" not in calls[0][1]
    assert calls[0][1]["timeout"] == 10


@pytest.mark.parametrize("plaintext", [
    "GLM_API_KEY='unterminated", "GLM_API_KEY=a\nGLM_API_KEY=b",
    "GLM_API_KEY=", "OTHER=x", "GLM_API_KEY=a b", "GLM_API_KEY='a\\0b'".replace('\\0', '\0'),
])
def test_bad_or_missing_assignments_are_redacted(age_store, monkeypatch, plaintext):
    monkeypatch.setattr(secrets.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=plaintext))
    with pytest.raises(SecretNotFound) as caught:
        SecretResolver(environment={}).resolve("glm", "age://cortex/GLM_API_KEY")
    assert "unterminated" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_decryption_timeout_does_not_expose_plaintext(age_store, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("age", 10, output="SECRET", stderr="SECRET")
    monkeypatch.setattr(secrets.subprocess, "run", fail)
    with pytest.raises(SecretNotFound) as caught:
        SecretResolver(environment={}).resolve("glm", "age://cortex/GLM_API_KEY")
    assert "SECRET" not in str(caught.value)
    assert caught.value.__context__ is None


def test_rotation_is_observed_without_restart(age_store, monkeypatch):
    outputs = iter(["GLM_API_KEY=first", "GLM_API_KEY=second"])
    monkeypatch.setattr(secrets.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=next(outputs)))
    resolver = SecretResolver(environment={})
    assert resolver.resolve("glm", "age://cortex/GLM_API_KEY").reveal() == "first"
    assert resolver.resolve("glm", "age://cortex/GLM_API_KEY").reveal() == "second"


def test_doctor_accepts_durable_age_transport_and_provider(monkeypatch):
    from cortex_platform.product.diagnostics import _transport_secret_check, _runtime_provider_check
    monkeypatch.setattr(secrets, "_system_age_lookup", lambda *a: "dummy-provider-key")
    config = {"runtime": {"provider": "anthropic", "model": "test", "base_url": "https://api.anthropic.com"},
              "secret_refs": {"anthropic": "age://cortex/ANTHROPIC_API_KEY",
                              "research_bot": "age://cortex/TELEGRAM_TOKEN"}}
    assert _transport_secret_check(config) == []
    assert _runtime_provider_check(config, environ={}) == []
