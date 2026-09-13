from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from deployment.private_access.config import ConfigError, load_config, parse_config


def test_loads_canonical_private_configuration(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    path = tmp_path / "access.json"
    path.write_text(json.dumps(config_dict), encoding="utf-8")

    config = load_config(path)

    assert config.public_hostname == "cortex-host.example-tailnet.ts.net"
    assert config.access_gateway.url == "http://127.0.0.1:3340"
    assert config.web_upstream.url == "http://127.0.0.1:3000"
    assert config.daemon_upstream.url == "http://127.0.0.1:8791"


@pytest.mark.parametrize(
    "origin",
    [
        "http://cortex-host.example-tailnet.ts.net",
        "https://*.example-tailnet.ts.net",
        "https://cortex-host.example.com",
        "https://cortex-host.example-tailnet.ts.net:8443",
        "https://cortex-host.example-tailnet.ts.net/path",
        "https://cortex-host.example-tailnet.ts.net?token=value",
        "https://CORTEX-host.example-tailnet.ts.net",
        "https://cortex-host..example-tailnet.ts.net",
        "https://-cortex-host.example-tailnet.ts.net",
        "https://user@cortex-host.example-tailnet.ts.net",
    ],
)
def test_rejects_non_exact_tailnet_https_origins(
    config_dict: dict[str, object], origin: str
) -> None:
    config_dict["public_origin"] = origin
    with pytest.raises(ConfigError, match="public_origin"):
        parse_config(config_dict)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_gateway", "http://0.0.0.0:3340"),
        ("access_gateway", "http://localhost:3340"),
        ("access_gateway", "https://127.0.0.1:3340"),
        ("web_upstream", "http://192.168.1.2:3000"),
        ("daemon_upstream", "http://100.64.0.1:8791"),
        ("daemon_upstream", "http://127.0.0.1:8791/api"),
    ],
)
def test_rejects_non_loopback_or_ambiguous_endpoints(
    config_dict: dict[str, object], field: str, value: str
) -> None:
    config_dict[field] = value
    with pytest.raises(ConfigError):
        parse_config(config_dict)


def test_requires_three_distinct_loopback_endpoints(
    config_dict: dict[str, object],
) -> None:
    config_dict["daemon_upstream"] = config_dict["web_upstream"]
    with pytest.raises(ConfigError, match="distinct"):
        parse_config(config_dict)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_logins", ["*"]),
        ("allowed_logins", ["Owner@example.com"]),
        ("allowed_sources", ["autogroup:member"]),
        ("allowed_sources", ["*"]),
        ("service_tag", "tag:*"),
        ("tag_owners", ["autogroup:admin"]),
        ("tag_owners", ["tag:delegated-owner"]),
        ("app_capability", "*/cortex"),
        ("app_capability", "tailscale.com/cap/cortex"),
        ("app_capability", "tailscale.io/cap/cortex"),
        ("allowed_capability_roles", ["*"]),
    ],
)
def test_rejects_wildcard_or_ambiguous_identity_policy(
    config_dict: dict[str, object], field: str, value: object
) -> None:
    identity = copy.deepcopy(config_dict["identity"])
    assert isinstance(identity, dict)
    identity[field] = value
    config_dict["identity"] = identity
    with pytest.raises(ConfigError):
        parse_config(config_dict)


def test_requires_every_runtime_login_in_tailnet_sources(
    config_dict: dict[str, object],
) -> None:
    identity = copy.deepcopy(config_dict["identity"])
    assert isinstance(identity, dict)
    identity["allowed_sources"] = ["tag:cortex-client"]
    config_dict["identity"] = identity
    with pytest.raises(ConfigError, match="allowed login"):
        parse_config(config_dict)


@pytest.mark.parametrize(
    "reference",
    [
        "literal-secret",
        "https://vault.example/secret",
        "env://lowercase",
        "env://CORTEX_SESSION/path",
        "keychain://service/account?secret=value",
        "keychain://user:password@service/account",
    ],
)
def test_rejects_secret_values_and_unsafe_references(
    config_dict: dict[str, object], reference: str
) -> None:
    config_dict["session_bootstrap_secret_ref"] = reference
    with pytest.raises(ConfigError, match="secret"):
        parse_config(config_dict)


def test_accepts_environment_secret_reference(config_dict: dict[str, object]) -> None:
    config_dict["session_bootstrap_secret_ref"] = "env://CORTEX_ACCESS_SESSION"
    assert parse_config(config_dict).session_bootstrap_secret_ref.endswith(
        "CORTEX_ACCESS_SESSION"
    )


def test_rejects_unknown_fields(config_dict: dict[str, object]) -> None:
    config_dict["funnel"] = True
    with pytest.raises(ConfigError, match="unsupported"):
        parse_config(config_dict)


def test_the_shipped_example_configuration_parses_under_the_strict_parser() -> None:
    """`deployment/private_access/example.config.json` is a wheel member.

    `tests/packaging/test_wheels.py:42` asserts the file ships; nothing asserted
    it is still valid input. It is the first thing an operator copies, and the
    parser it has to satisfy refuses unknown fields, non-canonical origins and
    embedded secrets -- so an example that drifted out of the schema fails at
    the one moment there is nobody to ask. Read from the repository rather than
    from the installed wheel: this suite runs off the checkout, and the wheel
    member is pinned byte-for-byte against this same file.
    """

    example = (
        Path(__file__).resolve().parents[2]
        / "deployment/private_access/example.config.json"
    )

    config = load_config(example)

    assert config.public_origin.startswith("https://")
    assert config.identity.allowed_logins
    assert config.session_bootstrap_secret_ref.startswith(
        ("env://", "keychain://")
    )
