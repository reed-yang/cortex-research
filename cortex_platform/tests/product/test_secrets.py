"""P2a: resolving secret references, and proving where they may not go.

The product has always validated `secret_refs` and never resolved one. This
is the resolver, plus the property that matters more than the resolution: a
resolved value reaches an effect process and nothing else -- not the Web
environment, not `control.db`, not `doctor`, not a log line, not a traceback.
"""

from __future__ import annotations

import pytest

from cortex_platform.product.secrets import (
    SecretNotFound,
    SecretResolutionError,
    SecretResolver,
    SecretValue,
)

_ALIAS = "anthropic-primary"
_SECRET = "sk-ant-super-secret-value"


def _resolver(
    *,
    environment: dict[str, str] | None = None,
    keychain: dict[tuple[str, str], str] | None = None,
) -> SecretResolver:
    items = dict(keychain or {})

    def lookup(service: str, account: str) -> str | None:
        return items.get((service, account))

    return SecretResolver(environment=environment or {}, keychain_lookup=lookup)


class TestResolution:
    def test_an_environment_reference_resolves(self) -> None:
        resolver = _resolver(environment={"ANTHROPIC_API_KEY": _SECRET})
        value = resolver.resolve(_ALIAS, "env://ANTHROPIC_API_KEY")
        assert value.reveal() == _SECRET
        assert value.alias == _ALIAS

    def test_a_keychain_reference_resolves(self) -> None:
        resolver = _resolver(keychain={("cortex", "anthropic"): _SECRET})
        value = resolver.resolve(_ALIAS, "keychain://cortex/anthropic")
        assert value.reveal() == _SECRET

    def test_resolving_many_returns_one_mapping(self) -> None:
        resolver = _resolver(
            environment={"A_KEY": "a"}, keychain={("cortex", "b"): "b"}
        )
        values = resolver.resolve_all(
            {"first": "env://A_KEY", "second": "keychain://cortex/b"}
        )
        assert {alias: value.reveal() for alias, value in values.items()} == {
            "first": "a",
            "second": "b",
        }


class TestRedaction:
    def test_the_value_never_appears_in_its_repr(self) -> None:
        value = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        assert _SECRET not in repr(value)
        assert _SECRET not in str(value)
        assert _ALIAS in repr(value)

    def test_the_value_never_appears_in_an_f_string(self) -> None:
        value = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        assert _SECRET not in f"{value}"

    def test_the_value_never_appears_in_a_traceback(self) -> None:
        value = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        try:
            raise RuntimeError(f"failed while using {value}")
        except RuntimeError as error:
            assert _SECRET not in str(error)

    def test_the_value_cannot_be_serialized_out(self) -> None:
        # macOS spawns multiprocessing children, so a SecretValue handed to a
        # worker would be pickled -- putting the credential on a pipe in
        # plaintext, with none of the redaction above applying.
        import pickle

        value = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(value)

    def test_copying_does_not_produce_a_plain_object(self) -> None:
        import copy

        value = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        for clone in (copy.copy(value), copy.deepcopy(value)):
            assert isinstance(clone, SecretValue)
            assert _SECRET not in repr(clone)
            assert clone.reveal() == _SECRET

    def test_two_values_compare_without_revealing(self) -> None:
        first = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        second = _resolver(environment={"K": _SECRET}).resolve(_ALIAS, "env://K")
        assert first == second
        assert first != _resolver(environment={"K": "other"}).resolve(
            _ALIAS, "env://K"
        )


class TestRefusals:
    def test_a_missing_environment_variable_is_typed(self) -> None:
        with pytest.raises(SecretNotFound) as error:
            _resolver().resolve(_ALIAS, "env://ABSENT_KEY")
        assert error.value.alias == _ALIAS
        assert "ABSENT_KEY" in str(error.value)

    def test_an_empty_environment_variable_is_refused(self) -> None:
        # An empty credential is a misconfiguration that would otherwise
        # surface as an authentication failure much further downstream.
        with pytest.raises(SecretNotFound):
            _resolver(environment={"K": ""}).resolve(_ALIAS, "env://K")

    def test_a_missing_keychain_item_is_typed(self) -> None:
        with pytest.raises(SecretNotFound):
            _resolver().resolve(_ALIAS, "keychain://cortex/absent")

    def test_a_malformed_reference_is_refused(self) -> None:
        for reference in (
            "https://example.com/secret",
            "env://lowercase",
            "keychain://service",
            "env://GOOD/extra",
            "",
        ):
            with pytest.raises(SecretResolutionError):
                _resolver(environment={"GOOD": "x"}).resolve(_ALIAS, reference)

    def test_a_refusal_never_carries_the_value(self) -> None:
        resolver = _resolver(environment={"K": _SECRET})
        with pytest.raises(SecretResolutionError) as error:
            resolver.resolve("token", "env://K")  # alias looks like a credential
        assert _SECRET not in str(error.value)


class TestBoundary:
    def test_the_web_environment_cannot_carry_a_secret(self) -> None:
        """The browser-facing process is the one that must never hold one.

        `lifecycle` builds a fully-replacing environment for each child, so
        this asserts against the real builder rather than a description of it.
        """

        from distribution import lifecycle

        names = set(_web_environment_names(lifecycle))
        assert not any("SECRET" in name for name in names)
        assert not any(name.endswith("_API_KEY") for name in names)

    def test_secret_material_is_not_a_control_store_concept(self) -> None:
        from cortex_platform.product.control import schema

        # Read the declared migrations rather than a contiguous range or a
        # `dir(schema)` scan: the scripts are named after what they create, not
        # after the number they were given, so only the declaration knows the
        # full set.
        script = "".join(
            source for _version, source in schema.migration_scripts()
        ).lower()
        for banned in ("api_key", "secret_value", "credential_value"):
            assert banned not in script


def _web_environment_names(lifecycle_module) -> list[str]:
    import inspect

    source = inspect.getsource(lifecycle_module.InstalledGeneration.web_environment)
    import re

    return re.findall(r'"([A-Z][A-Z0-9_]+)"', source)
