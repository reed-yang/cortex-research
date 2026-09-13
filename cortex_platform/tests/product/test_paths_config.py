from __future__ import annotations

from pathlib import Path

import pytest

from cortex_platform.product.config import (
    ConfigError,
    initialize,
    load_config,
    telegram_mode,
    validate_config,
    write_config,
)
from cortex_platform.product.diagnostics import doctor
from cortex_platform.product.paths import resolve_paths


def _environment(home: Path, **overrides: str) -> dict[str, str]:
    environment = {"HOME": str(home)}
    environment.update(overrides)
    return environment


def test_macos_defaults_use_native_library_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"

    paths = resolve_paths(environ=_environment(home), platform="darwin")

    support = home / "Library" / "Application Support" / "Cortex"
    assert paths.config_dir == support
    assert paths.config_file == support / "config.toml"
    assert paths.data_dir == support / "Data"
    assert paths.state_dir == support / "State"
    assert paths.cache_dir == home / "Library" / "Caches" / "Cortex"
    assert paths.log_dir == home / "Library" / "Logs" / "Cortex"


def test_linux_defaults_follow_xdg_and_home_fallbacks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    environment = _environment(
        home,
        XDG_CONFIG_HOME=str(tmp_path / "xdg-config"),
        XDG_DATA_HOME=str(tmp_path / "xdg-data"),
        XDG_STATE_HOME=str(tmp_path / "xdg-state"),
        XDG_CACHE_HOME=str(tmp_path / "xdg-cache"),
    )

    paths = resolve_paths(environ=environment, platform="linux")

    assert paths.config_dir == tmp_path / "xdg-config" / "cortex"
    assert paths.data_dir == tmp_path / "xdg-data" / "cortex"
    assert paths.state_dir == tmp_path / "xdg-state" / "cortex"
    assert paths.cache_dir == tmp_path / "xdg-cache" / "cortex"
    assert paths.log_dir == tmp_path / "xdg-state" / "cortex" / "logs"


def test_path_precedence_is_cli_then_env_then_config_then_default(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_file = tmp_path / "settings" / "cortex.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        """\
config_version = 1

[paths]
data_dir = "config-data"
state_dir = "config-state"
cache_dir = "config-cache"
log_dir = "config-logs"
""",
        encoding="utf-8",
    )
    environment = _environment(
        home,
        CORTEX_CONFIG_FILE=str(config_file),
        CORTEX_DATA_DIR=str(tmp_path / "env-data"),
        CORTEX_STATE_DIR=str(tmp_path / "env-state"),
    )

    paths = resolve_paths(
        environ=environment,
        platform="linux",
        cli_overrides={"data_dir": tmp_path / "cli-data"},
    )

    assert paths.data_dir == tmp_path / "cli-data"
    assert paths.state_dir == tmp_path / "env-state"
    assert paths.cache_dir == config_file.parent / "config-cache"
    assert paths.log_dir == config_file.parent / "config-logs"
    assert paths.config_dir == home / ".config" / "cortex"


def test_config_dir_override_selects_default_config_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_dir = tmp_path / "configured"

    paths = resolve_paths(
        environ=_environment(home, CORTEX_CONFIG_DIR=str(config_dir)),
        platform="linux",
    )

    assert paths.config_dir == config_dir
    assert paths.config_file == config_dir / "config.toml"


def test_cortex_home_is_never_a_product_data_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "source-repository"

    paths = resolve_paths(
        environ=_environment(home, CORTEX_HOME=str(repository)),
        platform="linux",
    )

    for path in paths.directories():
        assert repository not in path.parents
        assert path != repository


def test_init_is_idempotent_and_legacy_adoption_never_moves_roots(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    legacy_config = home / ".config" / "cortex"
    legacy_state = home / ".local" / "state" / "cortex"
    legacy_config.mkdir(parents=True)
    legacy_state.mkdir(parents=True)
    config_marker = legacy_config / "legacy.yaml"
    state_marker = legacy_state / "keep.db"
    config_marker.write_text("legacy: true\n", encoding="utf-8")
    state_marker.write_bytes(b"do-not-move")
    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")

    first = initialize(paths, environ=environment, adopt_legacy=False)
    second = initialize(paths, environ=environment, adopt_legacy=True)
    third = initialize(paths, environ=environment, adopt_legacy=True)

    assert first.created_config is True
    assert second.created_config is False
    assert third.created_config is False
    assert second.updated_config is True
    assert third.updated_config is False
    assert second.adopted_roots == ("legacy-config", "legacy-state")
    assert third.adopted_roots == ()
    assert config_marker.read_text(encoding="utf-8") == "legacy: true\n"
    assert state_marker.read_bytes() == b"do-not-move"
    assert config_marker.parent == legacy_config
    assert state_marker.parent == legacy_state
    loaded = load_config(paths.config_file)
    assert loaded["asset_roots"] == {
        "legacy-config": str(legacy_config),
        "legacy-state": str(legacy_state),
    }
    rendered = paths.config_file.read_text(encoding="utf-8").lower()
    assert paths.config_file.stat().st_mode & 0o777 == 0o600
    assert "password" not in rendered
    assert "api_key" not in rendered
    assert "token" not in rendered


def test_plaintext_secret_fields_are_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'config_version = 1\napi_key = "plaintext"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="unsupported|secret"):
        load_config(config_file)


def test_secret_references_are_allowed_but_plaintext_secret_values_are_not(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """\
config_version = 1

[secret_refs]
provider = "keychain://cortex/provider"
""",
        encoding="utf-8",
    )
    assert load_config(config_file)["secret_refs"]["provider"].startswith(
        "keychain://"
    )

    config_file.write_text(
        """\
config_version = 1

[secret_refs]
provider = "plaintext-value"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="reference"):
        load_config(config_file)


@pytest.mark.parametrize(
    "reference",
    [
        "https://vault.example/secret",
        "file:///tmp/secret",
        "keychain://user:password@cortex/provider",
        "keychain://cortex/provider?token=embedded",
        "keychain://cortex/provider#password",
        "keychain://cortex/provider=value",
        "keychain://cortex",
        "env://lowercase_name",
        "env://CORTEX_TOKEN/extra",
        "env://CORTEX_TOKEN?value=embedded",
    ],
)
def test_secret_reference_rejects_unsafe_or_embedded_credentials(
    tmp_path: Path, reference: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'config_version = 1\n\n[secret_refs]\nprovider = "{reference}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reference|scheme|credential"):
        load_config(config_file)


@pytest.mark.parametrize(
    "reference", ["keychain://cortex/provider", "env://CORTEX_PROVIDER_TOKEN"]
)
def test_secret_reference_accepts_only_supported_identifier_syntax(
    tmp_path: Path, reference: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'config_version = 1\n\n[secret_refs]\nprovider = "{reference}"\n',
        encoding="utf-8",
    )

    assert load_config(config_file)["secret_refs"] == {"provider": reference}


@pytest.mark.parametrize("key", ["api_key", "token", "password", "secret"])
def test_secret_reference_rejects_credential_like_aliases(
    tmp_path: Path, key: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'config_version = 1\n\n[secret_refs]\n{key} = "env://CORTEX_TOKEN"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="alias"):
        load_config(config_file)


@pytest.mark.parametrize("key", ["api_key", "token", "password", "readings"])
def test_asset_roots_reject_unknown_or_credential_like_keys(
    tmp_path: Path, key: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'config_version = 1\n\n[asset_roots]\n"{key}" = "/tmp/root"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="asset|credential|unsupported"):
        load_config(config_file)


@pytest.mark.parametrize("value", ["relative/root", "~/legacy", "https://host/root"])
def test_asset_roots_require_absolute_filesystem_paths(
    tmp_path: Path, value: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'config_version = 1\n\n[asset_roots]\nlegacy-config = "{value}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="absolute|path"):
        load_config(config_file)


def test_doctor_default_output_contains_no_absolute_paths_or_credentials(
    tmp_path: Path,
) -> None:
    home = tmp_path / "private-user-home"
    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")
    initialize(paths, environ=environment)

    output = doctor(paths, environ=environment).render()

    assert str(home) not in output
    assert "private-user-home" not in output
    assert "secret" not in output.lower()
    assert "config: ok" in output


def test_config_accepts_a_transport_mode_and_never_a_token() -> None:
    """D-P5-4: there is no product config field for a raw credential.

    The mode is the only thing the transports section carries. A token would
    have to arrive through a `secret_refs` alias resolved in the parent
    process, which is why the alias validator already refuses credential-
    shaped names.
    """

    assert validate_config(
        {"config_version": 1, "transports": {"telegram_mode": "active"}}
    ) == {"config_version": 1, "transports": {"telegram_mode": "active"}}
    assert telegram_mode({"config_version": 1}) == "shadow"
    assert telegram_mode({"config_version": 1, "transports": {}}) == "shadow"
    assert (
        telegram_mode({"config_version": 1, "transports": {"telegram_mode": "active"}})
        == "active"
    )

    for field in ("bot_token", "token", "api_key", "telegram_bot_token"):
        with pytest.raises(ConfigError, match="unsupported transport fields"):
            validate_config({"config_version": 1, "transports": {field: "value"}})

    with pytest.raises(ConfigError, match="telegram_mode"):
        validate_config(
            {"config_version": 1, "transports": {"telegram_mode": "loud"}}
        )
    with pytest.raises(ConfigError, match="must be strings"):
        validate_config({"config_version": 1, "transports": {"telegram_mode": True}})


def test_a_transport_mode_survives_a_config_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path, {"config_version": 1, "transports": {"telegram_mode": "active"}}
    )
    assert telegram_mode(load_config(path)) == "active"


def test_an_unknown_top_level_section_is_tolerated_but_unknown_keys_are_not(
    tmp_path: Path,
) -> None:
    """DIV-2: forward compatibility, in one direction only.

    P5 widened the schema with `[transports]` at `config_version = 1`, so a
    config written by gen 9 is unreadable to gen 8 -- and
    `resolve_installed_product_paths` runs the INSTALLED generation's own
    interpreter and turns any config refusal into `InstalledProductPathsError`,
    so `cortex-dist start|stop|status|record-proof` would all fail with it,
    product-wide, on a downgrade. Bumping `CONFIG_VERSION` makes that worse
    (`_render_config` writes the new number on every rewrite, so even a config
    with no transports section becomes unreadable), and failing open in
    `paths.py` would silently point cortexd at a different control.db.

    Tolerating an unknown SECTION is the narrowest fix that stops the next
    widening being a downgrade trap. An unknown KEY inside a known section
    stays fatal -- that is where a typo silently changes behaviour -- and so
    does an unknown top-level scalar.
    """

    accepted = validate_config(
        {
            "config_version": 1,
            "paths": {"data_dir": str(tmp_path / "data")},
            # A section this build has never heard of, from a later generation.
            "observability": {"exporter": "otlp"},
        }
    )
    assert accepted["paths"] == {"data_dir": str(tmp_path / "data")}
    assert "observability" not in accepted

    with pytest.raises(ConfigError, match="unsupported path fields"):
        validate_config({"config_version": 1, "paths": {"data_directory": "/tmp"}})
    with pytest.raises(ConfigError, match="unsupported configuration fields"):
        validate_config({"config_version": 1, "telemetry_enabled": True})


def _telegram_config(home: Path, reference: str | None) -> None:
    """Write a product config with a bot named, and optionally an alias."""

    from cortex_platform.product.config import write_config

    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")
    initialize(paths, environ=environment)
    config: dict = {
        "config_version": 1,
        "transports": {
            "telegram_mode": "active",
            "telegram_bot_identity": "cortex_research_bot",
            "telegram_base_url": "https://t.me",
        },
    }
    if reference is not None:
        config["secret_refs"] = {"research_bot": reference}
    write_config(paths.config_file, config)


def test_doctor_says_when_the_bot_token_cannot_be_resolved_by_the_daemon(
    tmp_path: Path,
) -> None:
    """⟦P5.4b⟧ The supervised daemon is started with a replaced environment.

    An `env://` reference resolves to nothing there, so every send inside the
    window is refused -- and the window is the operator-present ceremony, which
    is the worst possible time to learn it.
    """

    home = tmp_path / "home-env"
    _telegram_config(home, "env://TELEGRAM_BOT_TOKEN_RESEARCH")
    environment = _environment(home)
    report = doctor(resolve_paths(environ=environment, platform="darwin"), environ=environment)
    named = dict(report.checks)
    assert "transport_secret_refs" in named
    assert "env://" in named["transport_secret_refs"]
    assert "keychain://" in named["transport_secret_refs"]
    # Advisory, never fatal: the foreground path resolves it perfectly well.
    assert report.healthy is True
    assert "TELEGRAM_BOT_TOKEN_RESEARCH" not in report.render()


def test_doctor_says_nothing_when_the_reference_is_keychain_backed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home-keychain"
    _telegram_config(home, "keychain://cortex/research-bot")
    environment = _environment(home)
    report = doctor(resolve_paths(environ=environment, platform="darwin"), environ=environment)
    assert "transport_secret_refs" not in dict(report.checks)


def test_doctor_says_when_a_configured_bot_has_no_alias_at_all(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home-missing"
    _telegram_config(home, None)
    environment = _environment(home)
    report = doctor(resolve_paths(environ=environment, platform="darwin"), environ=environment)
    named = dict(report.checks)
    assert "transport_credential_unavailable" in named["transport_secret_refs"]


def test_the_runtime_section_names_a_provider_without_carrying_a_credential() -> None:
    """⟦BLOCK-1⟧ The endpoint is configuration; the key is still an alias.

    The supervised daemon's environment is replaced with seven keys, so the
    allowlisted `*_BASE_URL` variables cannot reach it. Naming the endpoint here
    is the only route a deployed installation has -- and it is safe to write in
    a file precisely because it is not a secret.
    """

    validated = validate_config(
        {
            "config_version": 1,
            "runtime": {
                "model": "claude-fable-5",
                "provider": "anthropic",
                "base_url": "https://sub2api.example",
            },
        }
    )
    assert validated["runtime"]["base_url"] == "https://sub2api.example"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:pass@provider.example",
        "https://provider.example/v1?api-version=2024",
        "https://provider.example/v1#fragment",
        "http://provider.example/v1",
        "ftp://provider.example",
        "https://",
        "https://provider.example:not-a-port",
    ],
)
def test_a_runtime_endpoint_that_could_carry_a_credential_is_refused(
    base_url: str,
) -> None:
    with pytest.raises(ConfigError, match="runtime.base_url"):
        validate_config(
            {"config_version": 1, "runtime": {"base_url": base_url}}
        )


def test_a_loopback_runtime_endpoint_may_be_plaintext() -> None:
    """An acceptance stands its provider up on the one port the seatbelt permits."""

    validated = validate_config(
        {"config_version": 1, "runtime": {"base_url": "http://127.0.0.1:8123/v1"}}
    )
    assert validated["runtime"]["base_url"] == "http://127.0.0.1:8123/v1"


def test_an_unknown_runtime_key_is_still_fatal() -> None:
    """The forward-compatibility rule stops at the section boundary.

    Worth pinning here because it is the rollback hazard `cortex-product-deploy`
    §7 now records: a gen 9 product reading a gen 10 config refuses
    `[runtime] base_url` outright, so a snapshot-based reversal has to strip it.
    """

    with pytest.raises(ConfigError, match="unsupported runtime fields"):
        validate_config({"config_version": 1, "runtime": {"endpoint": "x"}})


def _provider_config(home: Path, *, runtime: dict, references: dict) -> dict:
    from cortex_platform.product.config import write_config

    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")
    initialize(paths, environ=environment)
    write_config(
        paths.config_file,
        {
            "config_version": 1,
            "secret_refs": references,
            "runtime": runtime,
        },
    )
    return environment


def test_doctor_says_when_a_provider_alias_has_no_endpoint(tmp_path: Path) -> None:
    """⟦BLOCK-1⟧ Beside `transport_secret_refs`, and for the sharper failure.

    Nothing refused this before the window: health read `bound`, the runbook's
    predicates all passed, and the first message of the ceremony came back
    `runtime_execution_failed`.
    """

    environment = _provider_config(
        tmp_path / "home-provider-endpoint",
        runtime={"model": "claude-fable-5"},
        references={"anthropic": "keychain://cortex/provider"},
    )
    report = doctor(
        resolve_paths(environ=environment, platform="darwin"), environ=environment
    )
    named = dict(report.checks)
    assert "provider_endpoint_missing" in named["runtime_provider"]
    assert report.healthy is True


def test_doctor_says_when_an_endpoint_has_no_provider_alias(tmp_path: Path) -> None:
    environment = _provider_config(
        tmp_path / "home-provider-alias",
        runtime={"model": "claude-fable-5", "base_url": "https://provider.example"},
        references={},
    )
    report = doctor(
        resolve_paths(environ=environment, platform="darwin"), environ=environment
    )
    assert "provider_credential_missing" in dict(report.checks)["runtime_provider"]


def test_doctor_says_when_a_provider_reference_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    """The third silent way to half-configure a provider."""

    environment = _provider_config(
        tmp_path / "home-provider-unresolvable",
        runtime={"model": "claude-fable-5", "base_url": "https://provider.example"},
        references={"anthropic": "env://CORTEX_TEST_UNSET_PROVIDER_KEY"},
    )
    report = doctor(
        resolve_paths(environ=environment, platform="darwin"), environ=environment
    )
    named = dict(report.checks)
    # `env://` is refused for the supervised daemon before the value is even
    # looked for, and the advisory says which scheme resolves there.
    assert "provider_credential_unavailable" in named["runtime_provider"]
    assert "keychain://" in named["runtime_provider"]
    assert report.healthy is True


def test_doctor_says_nothing_about_a_provider_nobody_configured(
    tmp_path: Path,
) -> None:
    environment = _provider_config(
        tmp_path / "home-provider-none", runtime={}, references={}
    )
    report = doctor(
        resolve_paths(environ=environment, platform="darwin"), environ=environment
    )
    assert "runtime_provider" not in dict(report.checks)


# --------------------------------------------------------------------- [web] --

PUBLIC_ORIGIN = "https://cortex.example.test"
ACCESS_ISSUER = "https://example-team.cloudflareaccess.com"
ACCESS_AUDIENCE = "b4" * 32


def _web_config(home: Path, web: dict) -> dict:
    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")
    initialize(paths, environ=environment)
    write_config(paths.config_file, {"config_version": 1, "web": web})
    return environment


def test_the_web_section_fixes_the_port_and_names_the_public_door() -> None:
    """⟦P7⟧ Every value is public; the section is the door's shape, not a key."""

    from cortex_platform.product.config import web_settings

    validated = validate_config(
        {
            "config_version": 1,
            "web": {
                "port": 8787,
                "public_origin": PUBLIC_ORIGIN,
                "access_issuer": ACCESS_ISSUER,
                "access_audience": ACCESS_AUDIENCE,
            },
        }
    )
    settings = web_settings(validated)
    assert settings.port == 8787
    assert settings.public_origin == PUBLIC_ORIGIN
    assert settings.access_issuer == ACCESS_ISSUER
    assert settings.access_audience == ACCESS_AUDIENCE
    assert settings.public_door is True

    absent = web_settings({"config_version": 1})
    assert absent == web_settings(validate_config({"config_version": 1, "web": {}}))
    assert absent.port is None and absent.public_door is False


def test_the_web_port_round_trips_as_a_toml_integer(tmp_path: Path) -> None:
    home = tmp_path / "home-web-port"
    environment = _web_config(home, {"port": 8787})
    paths = resolve_paths(environ=environment, platform="darwin")
    text = paths.config_file.read_text(encoding="utf-8")
    assert '"port" = 8787' in text
    assert load_config(paths.config_file)["web"] == {"port": 8787}


@pytest.mark.parametrize("port", [0, 80, 1023, 65_536, "8787", 8787.0, True])
def test_a_web_port_outside_the_unprivileged_range_is_refused(port: object) -> None:
    with pytest.raises(ConfigError, match="web.port"):
        validate_config({"config_version": 1, "web": {"port": port}})


def test_a_public_origin_without_an_identity_check_is_refused() -> None:
    """⟦P7⟧ Fail closed: an origin the boundary would trust with no lock on it."""

    with pytest.raises(ConfigError, match="requires both web.access_issuer"):
        validate_config(
            {"config_version": 1, "web": {"public_origin": PUBLIC_ORIGIN}}
        )
    with pytest.raises(ConfigError, match="requires both web.access_issuer"):
        validate_config(
            {
                "config_version": 1,
                "web": {"public_origin": PUBLIC_ORIGIN, "access_issuer": ACCESS_ISSUER},
            }
        )
    with pytest.raises(ConfigError, match="require web.public_origin"):
        validate_config(
            {
                "config_version": 1,
                "web": {"access_issuer": ACCESS_ISSUER, "access_audience": ACCESS_AUDIENCE},
            }
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://cortex.example.test",
        "https://cortex.example.test/",
        "https://cortex.example.test:8443",
        "https://cortex.example.test/app",
        "https://cortex.example.test?x=1",
        "https://user:pass@cortex.example.test",
        "https://Cortex.Example.Test",
        "https://localhost",
        "https://127.0.0.1",
        "https://cortex",
        "cortex.example.test",
    ],
)
def test_a_public_origin_that_is_not_exactly_https_fqdn_is_refused(origin: str) -> None:
    with pytest.raises(ConfigError, match="web.public_origin"):
        validate_config(
            {
                "config_version": 1,
                "web": {
                    "public_origin": origin,
                    "access_issuer": ACCESS_ISSUER,
                    "access_audience": ACCESS_AUDIENCE,
                },
            }
        )


@pytest.mark.parametrize(
    "issuer",
    [
        "http://example-team.cloudflareaccess.com",
        "https://example-team.cloudflareaccess.com/",
        "https://example-team.cloudflareaccess.com/cdn-cgi/access/certs",
        "https://cloudflareaccess.com",
        "https://example-team.example.com",
        "https://a.b.cloudflareaccess.com",
        "http://localhost:8123",
        "http://127.0.0.1",
    ],
)
def test_an_access_issuer_that_is_not_a_team_domain_is_refused(issuer: str) -> None:
    with pytest.raises(ConfigError, match="web.access_issuer"):
        validate_config(
            {
                "config_version": 1,
                "web": {
                    "public_origin": PUBLIC_ORIGIN,
                    "access_issuer": issuer,
                    "access_audience": ACCESS_AUDIENCE,
                },
            }
        )


def test_a_loopback_access_issuer_is_for_an_acceptance_only() -> None:
    validated = validate_config(
        {
            "config_version": 1,
            "web": {
                "public_origin": PUBLIC_ORIGIN,
                "access_issuer": "http://127.0.0.1:8123",
                "access_audience": ACCESS_AUDIENCE,
            },
        }
    )
    assert validated["web"]["access_issuer"] == "http://127.0.0.1:8123"


@pytest.mark.parametrize("audience", ["", "b4" * 31, "B4" * 32, "b4" * 32 + "0", "not-hex" * 9])
def test_an_access_audience_that_is_not_an_aud_tag_is_refused(audience: str) -> None:
    with pytest.raises(ConfigError, match="web.access_audience"):
        validate_config(
            {
                "config_version": 1,
                "web": {
                    "public_origin": PUBLIC_ORIGIN,
                    "access_issuer": ACCESS_ISSUER,
                    "access_audience": audience,
                },
            }
        )


def test_an_unknown_web_key_is_fatal_but_the_section_itself_is_forward_compatible() -> None:
    """The rule gen 11 applies to a gen 12 config: unknown SECTION ignored, unknown KEY fatal.

    `[web]` is a whole new section, so a generation that predates it drops it
    on read (ephemeral port, no public door) instead of refusing the file the
    way `[runtime] base_url` did across the gen 9/10 boundary.
    """

    with pytest.raises(ConfigError, match="unsupported web fields"):
        validate_config({"config_version": 1, "web": {"listen_port": 8787}})
    # What a pre-P7 generation sees: the section is a dict-valued unknown key.
    assert validate_config({"config_version": 1, "web_next": {"port": 1}}) == {
        "config_version": 1
    }


def test_doctor_prints_the_web_section_state(tmp_path: Path) -> None:
    ephemeral = _web_config(tmp_path / "home-web-none", {})
    report = doctor(
        resolve_paths(environ=ephemeral, platform="darwin"), environ=ephemeral
    )
    named = dict(report.checks)
    assert named["web"] == "configured: port ephemeral; public door none"
    assert "web_public_door" not in named
    assert report.healthy is True

    configured = _web_config(
        tmp_path / "home-web-door",
        {
            "port": 8787,
            "public_origin": PUBLIC_ORIGIN,
            "access_issuer": ACCESS_ISSUER,
            "access_audience": ACCESS_AUDIENCE,
        },
    )
    report = doctor(
        resolve_paths(environ=configured, platform="darwin"), environ=configured
    )
    named = dict(report.checks)
    assert named["web"] == (
        f"configured: port 8787 (fixed); public door {PUBLIC_ORIGIN} "
        f"(issuer {ACCESS_ISSUER}, audience {ACCESS_AUDIENCE[:8]}...)"
    )
    assert "web_public_door" not in named
    assert report.healthy is True


def test_doctor_does_not_describe_a_front_door_it_could_not_read(tmp_path: Path) -> None:
    home = tmp_path / "home-invalid-config"
    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")
    initialize(paths, environ=environment)
    paths.config_file.write_text(
        'config_version = 1\n\n[web]\n"public_origin" = "https://cortex.example.test"\n'
    )
    report = doctor(paths, environ=environment)
    named = dict(report.checks)
    assert named["config"] == "invalid"
    assert named["web"] == "unknown (configuration invalid)"
    assert "web_public_door" not in named


def test_doctor_advises_when_the_public_door_trusts_a_loopback_issuer(tmp_path: Path) -> None:
    """The loopback issuer form exists for an acceptance; `doctor` says so.

    Advisory, never fatal: the schema accepts the form, so a non-zero exit
    here would be a worse regression than the plaintext it warns about.
    """

    home = tmp_path / "home-loopback-issuer"
    environment = _environment(home)
    paths = resolve_paths(environ=environment, platform="darwin")
    initialize(paths, environ=environment)
    write_config(
        paths.config_file,
        {
            "config_version": 1,
            "web": {
                "port": 8787,
                "public_origin": PUBLIC_ORIGIN,
                "access_issuer": "http://127.0.0.1:8123",
                "access_audience": ACCESS_AUDIENCE,
            },
        },
    )
    report = doctor(paths, environ=environment)
    named = dict(report.checks)
    assert "plaintext HTTP" in named["web_public_door_insecure_issuer"]
    assert "web_public_door" not in named
    assert report.healthy is True

    write_config(
        paths.config_file,
        {
            "config_version": 1,
            "web": {
                "port": 8787,
                "public_origin": PUBLIC_ORIGIN,
                "access_issuer": ACCESS_ISSUER,
                "access_audience": ACCESS_AUDIENCE,
            },
        },
    )
    assert "web_public_door_insecure_issuer" not in dict(doctor(paths, environ=environment).checks)


def test_doctor_advises_when_the_public_door_has_no_fixed_port(tmp_path: Path) -> None:
    """A tunnel ingress needs a port that survives a restart; say so before the ceremony."""

    environment = _web_config(
        tmp_path / "home-web-moving",
        {
            "public_origin": PUBLIC_ORIGIN,
            "access_issuer": ACCESS_ISSUER,
            "access_audience": ACCESS_AUDIENCE,
        },
    )
    report = doctor(
        resolve_paths(environ=environment, platform="darwin"), environ=environment
    )
    named = dict(report.checks)
    assert named["web"].startswith("configured: port ephemeral; public door ")
    assert "set web.port" in named["web_public_door"]
    assert report.healthy is True
