"""The `[xhs]` section, its defaults, its secret aliases and its asset roots."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cortex_platform.product.config import (
    ConfigError,
    XhsSettings,
    _render_config,
    load_config,
    validate_config,
    write_config,
    xhs_asset_root_paths,
    xhs_settings,
)
from cortex_platform.product.paths import resolve_paths


def _config(**xhs: object) -> dict[str, object]:
    return {"config_version": 1, "xhs": xhs}


def test_an_absent_section_is_the_disabled_default() -> None:
    settings = xhs_settings(validate_config({"config_version": 1}))
    assert settings == XhsSettings()
    assert settings.enabled is False
    assert settings.tikhub_base == "https://api.tikhub.io"
    assert settings.gpt_base == ""
    assert (settings.gpt_model, settings.gpt_effort) == ("gpt-6-luna", "xhigh")
    assert (settings.max_list_pages, settings.drain_units_per_tick) == (3, 10)
    assert settings.daily_calls == {"tikhub": 100, "ocr": 1000, "gpt": 300}
    assert settings.fallback_enabled is False and settings.fallback_weekly_cap == 100
    assert (settings.fallback_model, settings.fallback_effort) == ("gpt-6.1-sol", "xhigh")


def test_given_values_override_defaults_one_by_one() -> None:
    settings = xhs_settings(validate_config(_config(
        enabled=True, gpt_base="https://gpt.example/v1", max_list_pages=13,
        daily_calls={"gpt": 50},
    )))
    assert settings.enabled is True and settings.gpt_base == "https://gpt.example/v1"
    assert settings.max_list_pages == 13 and settings.drain_units_per_tick == 10
    assert settings.daily_calls == {"tikhub": 100, "ocr": 1000, "gpt": 50}


@pytest.mark.parametrize(
    "xhs",
    [
        {"enabled": "yes"},
        {"enabled": 1},
        {"tikhub_base": "http://api.tikhub.io"},
        {"tikhub_base": "https://user:secret@api.tikhub.io"},
        {"tikhub_base": "https://api.tikhub.io?key=secret"},
        {"tikhub_base": ""},
        {"gpt_base": "ftp://gpt.example"},
        {"gpt_base": "https://gpt.example/#frag"},
        {"gpt_model": "gpt 6"},
        {"gpt_effort": "X-HIGH"},
        {"max_list_pages": 0},
        {"max_list_pages": True},
        {"max_list_pages": "3"},
        {"drain_units_per_tick": 101},
        {"daily_calls": {"tikhub": -1}},
        {"daily_calls": {"jina": 5}},
        {"daily_calls": {"gpt": 1.5}},
        {"daily_calls": 100},
        {"api_key": "plaintext"},
        {"fallback_enabled": "true"},
        {"fallback_enabled": 0},
        {"fallback_weekly_cap": 0},
        {"fallback_weekly_cap": 101},
        {"fallback_weekly_cap": True},
        {"fallback_model": "gpt 6.1"},
        {"fallback_model": ""},
        {"fallback_effort": "XHIGH"},
        {"fallback_effort": "x" * 17},
    ],
)
def test_invalid_values_are_refused(xhs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        validate_config(_config(**xhs))


def test_a_loopback_http_endpoint_is_allowed_for_acceptance() -> None:
    validated = validate_config(_config(tikhub_base="http://127.0.0.1:8123",
                                        gpt_base="http://localhost:9000/v1"))
    assert xhs_settings(validated).tikhub_base == "http://127.0.0.1:8123"


def test_the_section_round_trips_through_the_written_file(tmp_path: Path) -> None:
    config = validate_config(_config(
        enabled=False, gpt_base="", gpt_effort="high",
        daily_calls={"tikhub": 20, "ocr": 0, "gpt": 300},
    ))
    rendered = _render_config(config)
    assert validate_config(tomllib.loads(rendered)) == config
    path = tmp_path / "config.toml"
    write_config(path, config)
    assert load_config(path) == config
    assert xhs_settings(load_config(path)).daily_calls["ocr"] == 0


def test_the_fallback_keys_round_trip_and_override_defaults(tmp_path: Path) -> None:
    config = validate_config(_config(
        fallback_enabled=True, fallback_weekly_cap=25,
        fallback_model="gpt-6.1-sol", fallback_effort="high",
    ))
    rendered = _render_config(config)
    assert "\"fallback_enabled\" = true" in rendered
    assert validate_config(tomllib.loads(rendered)) == config
    path = tmp_path / "config.toml"
    write_config(path, config)
    settings = xhs_settings(load_config(path))
    assert settings.fallback_enabled is True and settings.fallback_weekly_cap == 25
    assert (settings.fallback_model, settings.fallback_effort) == ("gpt-6.1-sol", "high")
    # The other plugin settings keep their defaults.
    assert settings.enabled is False and settings.gpt_model == "gpt-6-luna"


def test_the_plugin_secret_aliases_are_accepted_as_references() -> None:
    validated = validate_config({
        "config_version": 1,
        "secret_refs": {
            "tikhub": "age://cortex/TIKHUB_API_KEY",
            "sub2api-gpt": "keychain://cortex-research/sub2api-gpt",
            "jina": "age://cortex/JINA_API_KEY",
        },
    })
    assert set(validated["secret_refs"]) == {"tikhub", "sub2api-gpt", "jina"}
    with pytest.raises(ConfigError):
        validate_config({"config_version": 1, "secret_refs": {"tikhub": "plain-value"}})


def test_default_roots_live_outside_the_research_corpus(tmp_path: Path) -> None:
    paths = resolve_paths(
        environ={
            "HOME": str(tmp_path / "home"),
            "CORTEX_DATA_DIR": str(tmp_path / "data"),
            "CORTEX_STATE_DIR": str(tmp_path / "state"),
        },
        platform="darwin",
    )
    roots = xhs_asset_root_paths(paths)
    assert set(roots) == {"xhs-notes", "blogs"}
    corpus = paths.data_dir / "research" / "corpus"
    for root in roots.values():
        assert root.is_relative_to(paths.data_dir)
        assert not root.is_relative_to(corpus) and not corpus.is_relative_to(root)
        assert not root.is_relative_to(paths.data_dir / "research")
