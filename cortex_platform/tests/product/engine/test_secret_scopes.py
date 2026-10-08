"""Operation-scoped credentials: each child receives only its own operation's.

No store is read: the age lookup is replaced, and every value is a dummy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cortex_platform.product import secrets
from cortex_platform.product.engine.bindings import (
    ENGINE_BINDINGS,
    OPERATION_SECRET_SCOPES,
    PROVIDER_SECRET_BINDINGS,
    EngineRoots,
    effect_secret_aliases,
    engine_secret_aliases,
    operation_secret_scope,
    research_effect_environment,
)
from cortex_platform.product.engine.protocol import ARXIV_OPERATIONS, OPERATIONS
from cortex_platform.product.engine.service import _secret_provider, unusable_secret_aliases
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor
from cortex_platform.product.secrets import SecretNotFound, SecretValue
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected

from .conftest import ActivationGate

ENGINE_ALIASES = {"glm", "glm-app-id", "novita", "openrouter"}
STORED = {"GLM_API_KEY": "dummy-glm", "OPENROUTER_API_KEY": "dummy-embedding"}
CONFIG = {
    "secret_refs": {
        "glm": "age://cortex/GLM_API_KEY",
        "openrouter": "age://cortex/OPENROUTER_API_KEY",
        # Configured, and nothing stands behind any of them.
        "tikhub": "age://cortex/TIKHUB_API_KEY",
        "sub2api-gpt": "age://cortex/SUB2API_GPT_KEY",
        "jina": "age://cortex/JINA_API_KEY",
    }
}


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def lookup(store: str, name: str) -> str | None:
        asked.append(name)
        return STORED.get(name)

    monkeypatch.setattr(secrets, "_system_age_lookup", lookup)
    return asked


def test_every_operation_has_a_scope_and_arxiv_keeps_its_credentials() -> None:
    assert OPERATIONS <= set(OPERATION_SECRET_SCOPES)
    for operation in ARXIV_OPERATIONS:
        assert operation_secret_scope(operation).aliases == ENGINE_ALIASES
        assert not operation_secret_scope(operation).aliases & set(PROVIDER_SECRET_BINDINGS)
    expected = {
        "xhs_list_page": {"tikhub"},
        "xhs_note_detail": {"tikhub"},
        "xhs_download_image": set(),
        "xhs_ocr_image": {"novita", "glm", "glm-app-id"},
        "xhs_identify": {"sub2api-gpt"},
        "xhs_resolve_link": {"sub2api-gpt"},
        "blog_fetch": set(),
        "xhs_fallback_decide": {"sub2api-gpt"},
        "xhs_fallback_verify": set(),
    }
    for operation, aliases in expected.items():
        assert operation_secret_scope(operation).aliases == aliases
    assert operation_secret_scope("blog_fetch").optional == {"jina"}
    assert operation_secret_scope("xhs_ocr_image").optional == {"sub2api-gpt"}
    unknown = operation_secret_scope("not_an_operation")
    assert not unknown.aliases and not unknown.optional


def test_provider_credentials_are_outside_the_engine_table() -> None:
    assert set(engine_secret_aliases()) == ENGINE_ALIASES
    assert set(effect_secret_aliases()) == ENGINE_ALIASES | {"tikhub", "sub2api-gpt", "jina"}
    assert not set(PROVIDER_SECRET_BINDINGS.values()) & set(ENGINE_BINDINGS)


def test_a_provider_credential_lands_in_exactly_one_variable(roots: EngineRoots) -> None:
    environment = research_effect_environment(
        roots=roots,
        secrets={"tikhub": SecretValue("tikhub", "dummy-tikhub")},
        effect_marker="marker0",
    )
    assert [name for name, value in environment.items() if value == "dummy-tikhub"] == [
        "CORTEX_TIKHUB_API_KEY"
    ]
    plain = research_effect_environment(roots=roots, effect_marker="marker0")
    assert not set(PROVIDER_SECRET_BINDINGS.values()) & set(plain)


def test_a_missing_xhs_credential_never_reaches_an_arxiv_operation(stored: list[str]) -> None:
    provider, dropped = _secret_provider(CONFIG, keychain_only=True)
    assert dropped == ()
    for operation in ARXIV_OPERATIONS:
        stored.clear()
        assert set(provider(operation)) == {"glm", "openrouter"}
        assert set(stored) == {"GLM_API_KEY", "OPENROUTER_API_KEY"}
    stored.clear()
    with pytest.raises(SecretNotFound):
        provider("xhs_list_page")
    assert stored == ["TIKHUB_API_KEY"]
    with pytest.raises(SecretNotFound):
        provider("xhs_identify")
    # Optional: an unresolvable Jina reference is left out, not fatal.
    assert provider("blog_fetch") == {}
    assert provider("xhs_download_image") == {}
    stored.clear()
    assert set(provider("xhs_ocr_image")) == {"glm"}
    assert "OPENROUTER_API_KEY" not in stored
    assert provider("not_an_operation") == {}


def test_an_env_reference_for_a_provider_alias_is_reported_unusable() -> None:
    config = {"secret_refs": {"tikhub": "env://TIKHUB_API_KEY", "glm": "age://cortex/GLM_API_KEY"}}
    assert unusable_secret_aliases(config) == ("tikhub",)


def test_the_supervisor_hands_a_child_only_its_operations_credentials(
    roots: EngineRoots, research_db: Path
) -> None:
    asked: list[str] = []

    def provider(operation: str) -> dict[str, SecretValue]:
        # Over-supplies on purpose: the supervisor filters to the scope too.
        asked.append(operation)
        return {
            "openrouter": SecretValue("openrouter", "dummy-embedding"),
            "tikhub": SecretValue("tikhub", "dummy-tikhub"),
            "sub2api-gpt": SecretValue("sub2api-gpt", "dummy-gpt"),
        }

    supervisor = ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        secret_provider=provider,
    )
    arxiv = supervisor._environment("marker0", {}, "ingest_arxiv")
    assert arxiv["OPENROUTER_API_KEY"] == "dummy-embedding"
    assert not {"CORTEX_TIKHUB_API_KEY", "CORTEX_GPT_API_KEY"} & set(arxiv)
    listing = supervisor._environment("marker0", {}, "xhs_list_page")
    assert listing["CORTEX_TIKHUB_API_KEY"] == "dummy-tikhub"
    assert not {"OPENROUTER_API_KEY", "CORTEX_GPT_API_KEY"} & set(listing)
    assert asked == ["ingest_arxiv", "xhs_list_page"]


def test_a_real_arxiv_child_runs_while_xhs_credentials_are_missing(
    roots: EngineRoots, research_db: Path, stored: list[str]
) -> None:
    provider, _ = _secret_provider(CONFIG, keychain_only=True)
    supervisor = ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        secret_provider=provider,
    )
    execution = supervisor.run("checkpoint")
    assert execution.ok
    assert "TIKHUB_API_KEY" not in stored
    # The same missing reference refuses the operation that needs it.
    with pytest.raises(EffectPermanentlyRejected) as raised:
        supervisor._environment("marker0", {}, "xhs_note_detail")
    assert raised.value.category == "adapter_unavailable"
