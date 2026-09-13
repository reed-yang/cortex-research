"""D3: the binding table is total, and the child environment fully replaces."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cortex_platform.product.engine.bindings import (
    BOUND,
    DENIED,
    EFFECT_MARKER_VARIABLE,
    ENGINE_BINDINGS,
    EngineRoots,
    INERT,
    bindings_by_disposition,
    engine_secret_aliases,
    research_effect_environment,
)
from cortex_platform.product.paths import resolve_paths
from cortex_platform.product.secrets import SecretValue


@pytest.fixture()
def roots(tmp_path: Path) -> EngineRoots:
    registry = resolve_paths(
        environ={"HOME": str(tmp_path / "home")}, platform="darwin"
    )
    # Deliberately NOT named `papers`: the production root is `corpus`, and a
    # fixture ending in `/papers` makes the F4 split invisible.
    return EngineRoots.resolve(registry, corpus_root=tmp_path / "corpus")


def test_every_binding_carries_a_disposition_and_a_reason() -> None:
    for name, binding in ENGINE_BINDINGS.items():
        assert binding.name == name
        assert binding.disposition in {BOUND, DENIED, INERT}
        assert binding.reason
        assert (binding.source is not None) == (binding.disposition == BOUND)


def test_the_contract_carrying_rows_hold_their_dispositions() -> None:
    # AMD-9: HOME is BOUND and may never be DENIED.
    assert ENGINE_BINDINGS["HOME"].disposition == BOUND
    assert ENGINE_BINDINGS["CORTEX_BACKUP_STATE_ROOT"].disposition == DENIED
    # F4: bind one corpus root and not the other and the corpus splits.
    assert ENGINE_BINDINGS["CORTEX_AGENT_READINGS"].disposition == BOUND
    assert ENGINE_BINDINGS["CORTEX_PAPERS_DIR"].disposition == BOUND
    # F6/F10: the profile switch and every Telegram name fail closed.
    for name in (
        "HERMES_PROFILE",
        "HERMES_HOME",
        "CORTEX_HERMES_HOOKS_DIR",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN_RESEARCH",
        "TELEGRAM_HOME_CHANNEL",
        "CORTEX_RESEARCH_CHAT_ID",
        "CORTEX_UV_BIN",
        "CORTEX_VENV_PY",
        "CORTEX_PAPER_INGEST_SKILL",
        # Both OCR slot names now fail closed: the semaphore that read them
        # left with the excluded `ingest_slots` module, and a bound directory
        # nothing reads is an invitation for a later reader to acquire it.
        "OCR_GLOBAL_SLOTS",
        "OCR_SLOT_DIR",
    ):
        assert ENGINE_BINDINGS[name].disposition == DENIED, name


def test_the_engine_has_no_telegram_name_to_bind() -> None:
    """Stronger than the old `CORTEX_TELEGRAM_DISABLE=1` belt.

    The belt existed because `cortex_platform.telegram` was importable from the
    engine's closure. It is not shipped any more, so there is no sender to
    disable and no Telegram name may be BOUND at all.
    """

    telegram_names = [
        name for name in ENGINE_BINDINGS if "TELEGRAM" in name or "CHAT_ID" in name
    ]
    assert telegram_names
    for name in telegram_names:
        assert ENGINE_BINDINGS[name].disposition == DENIED, name


def test_environment_binds_both_corpus_roots_to_one_corpus(roots: EngineRoots) -> None:
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    assert environment["CORTEX_PAPERS_DIR"] == str(roots.corpus_root)
    assert environment["CORTEX_AGENT_READINGS"] == str(roots.readings_root)
    assert environment["CORTEX_RESEARCH_DB"] == str(roots.research_db)


def test_environment_carries_no_refused_name_and_no_gdrive_path(
    roots: EngineRoots,
) -> None:
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    refused = {
        name
        for name, binding in ENGINE_BINDINGS.items()
        if binding.disposition != BOUND
    }
    assert refused.isdisjoint(environment)
    assert not [value for value in environment.values() if "gdrive" in value]
    # Nothing outside the table (plus the product's own marker) may appear.
    allowed = {
        name for name, binding in ENGINE_BINDINGS.items() if binding.disposition == BOUND
    } | {EFFECT_MARKER_VARIABLE}
    assert set(environment) <= allowed


def test_environment_is_fully_replacing_not_inherited(
    roots: EngineRoots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_PROFILE", "investment")
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(Path.home() / "gdrive"))
    monkeypatch.setenv("SOMETHING_ENTIRELY_UNRELATED", "1")
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    assert "HERMES_PROFILE" not in environment
    assert "SOMETHING_ENTIRELY_UNRELATED" not in environment
    assert environment["CORTEX_AGENT_READINGS"] == str(roots.readings_root)


def test_home_is_bound_inside_product_state(roots: EngineRoots) -> None:
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    assert environment["HOME"] == str(roots.state / "home")
    assert Path(environment["HOME"]).is_relative_to(roots.state)


def test_secret_lands_in_exactly_one_allowlisted_variable(roots: EngineRoots) -> None:
    aliases = engine_secret_aliases()
    assert aliases["openrouter"] == "OPENROUTER_API_KEY"
    environment = research_effect_environment(
        roots=roots,
        secrets={"openrouter": SecretValue("openrouter", "fake-value-0")},
        effect_marker="marker0",
    )
    carrying = [name for name, value in environment.items() if value == "fake-value-0"]
    assert carrying == ["OPENROUTER_API_KEY"]


def test_an_unconfigured_secret_is_simply_absent(roots: EngineRoots) -> None:
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    assert "OPENROUTER_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


def test_an_unbound_alias_is_refused(roots: EngineRoots) -> None:
    with pytest.raises(ValueError, match="unbound aliases"):
        research_effect_environment(
            roots=roots,
            secrets={"not-a-binding": SecretValue("not-a-binding", "x")},
            effect_marker="marker0",
        )


def test_skip_embed_is_a_product_decision(roots: EngineRoots) -> None:
    assert (
        research_effect_environment(roots=roots, effect_marker="m0")["CORTEX_SKIP_EMBED"]
        == "0"
    )
    assert (
        research_effect_environment(
            roots=roots, effect_marker="m0", skip_embed=True
        )["CORTEX_SKIP_EMBED"]
        == "1"
    )


def test_prepare_creates_only_product_roots(roots: EngineRoots) -> None:
    roots.prepare()
    assert roots.state.is_dir()
    assert roots.home.is_dir()
    assert roots.research_db.parent.is_dir()
    assert not roots.research_db.exists()


def test_path_bindings_stay_under_the_product_roots(roots: EngineRoots) -> None:
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    for name, binding in ENGINE_BINDINGS.items():
        if binding.disposition != BOUND or not binding.source.startswith("path:"):
            continue
        value = Path(environment[name])
        assert any(
            value == root or value.is_relative_to(root)
            for root in roots.write_roots
        ), name


def test_the_effect_marker_is_required(roots: EngineRoots) -> None:
    with pytest.raises(ValueError, match="effect_marker"):
        research_effect_environment(roots=roots, effect_marker="")


def test_disposition_report_covers_the_whole_table() -> None:
    grouped = bindings_by_disposition()
    assert sum(len(names) for names in grouped.values()) == len(ENGINE_BINDINGS)
    assert set(grouped) == {BOUND, DENIED, INERT}


def test_platform_injected_names_are_named_not_hand_waved() -> None:
    """darwin adds these underneath a fully replacing environment."""

    from cortex_platform.product.engine.bindings import PLATFORM_INJECTED_VARIABLES

    assert "__CF_USER_TEXT_ENCODING" in PLATFORM_INJECTED_VARIABLES
    # LC_CTYPE is both injected and bound; the binding wins in the child.
    assert ENGINE_BINDINGS["LC_CTYPE"].disposition == BOUND


def test_path_is_pinned_and_not_the_operators(roots: EngineRoots) -> None:
    environment = research_effect_environment(roots=roots, effect_marker="marker0")
    assert environment["PATH"] == os.defpath


def test_both_corpus_names_answer_one_directory_at_s1s_frozen_shape(
    tmp_path: Path,
) -> None:
    """F4 at the shape production actually has: the root is named `corpus`.

    S1 registers `research-corpus` at `<data_dir>/research/corpus`, so deriving
    the readings root by path arithmetic (`corpus_root.parent`) made
    `CORTEX_AGENT_READINGS/papers` -- the directory `index_papers.py:16` builds
    and `paper_ingest.py:700` writes -- a sibling of the adopted corpus rather
    than the corpus itself.
    """

    registry = resolve_paths(
        environ={
            "HOME": str(tmp_path / "home"),
            "CORTEX_DATA_DIR": str(tmp_path / "data"),
            "CORTEX_STATE_DIR": str(tmp_path / "state"),
        },
        platform="darwin",
    )
    roots = EngineRoots.resolve(
        registry, corpus_root=registry.data_dir / "research" / "corpus"
    )
    assert roots.corpus_root.name == "corpus"
    roots.prepare()
    environment = research_effect_environment(roots=roots, effect_marker="marker0")

    written = Path(environment["CORTEX_AGENT_READINGS"]) / "papers"
    assert written.resolve() == roots.corpus_root.resolve()
    assert Path(environment["CORTEX_PAPERS_DIR"]).resolve() == roots.corpus_root.resolve()

    # The invariant the two bindings exist for: one ingest, one directory.
    (written / "20260101-A-Paper").mkdir(parents=True)
    assert (roots.corpus_root / "20260101-A-Paper").is_dir()
