from __future__ import annotations

import json

import pytest

from deployment.private_access.config import AccessConfig
from deployment.private_access.policy import evaluate_request


def _headers(config: AccessConfig) -> dict[str, str]:
    return {
        "Host": config.public_hostname,
        "Origin": config.public_origin,
        "Sec-Fetch-Site": "same-origin",
        "X-Cortex-Web-Client": "v1",
        "Tailscale-User-Login": "owner@example.com",
        "Tailscale-User-Name": "Owner",
        "Tailscale-App-Capabilities": json.dumps(
            {config.identity.app_capability: [{"role": "owner"}]}
        ),
        "Forwarded": "for=203.0.113.9;proto=http;host=attacker.example",
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "http",
        "X-Real-IP": "203.0.113.9",
        "Content-Length": "999",
    }


def test_authorizes_exact_user_and_rebuilds_forwarded_boundary(
    config: AccessConfig,
) -> None:
    decision = evaluate_request(
        method="POST",
        target="/api/cortex/threads",
        headers=_headers(config),
        client_host="127.0.0.1",
        config=config,
    )

    assert decision.allowed
    forwarded = dict(decision.upstream_headers)
    assert forwarded["host"] == config.public_hostname
    assert forwarded["x-forwarded-host"] == config.public_hostname
    assert forwarded["x-forwarded-proto"] == "https"
    assert "forwarded" not in forwarded
    assert "x-real-ip" not in forwarded
    assert "tailscale-user-login" not in forwarded
    assert "tailscale-app-capabilities" not in forwarded
    assert "content-length" not in forwarded


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"Host": "attacker.example"}, "host_rejected"),
        ({"Origin": "https://attacker.example"}, "origin_rejected"),
        ({"Sec-Fetch-Site": "cross-site"}, "fetch_site_rejected"),
        ({"X-Cortex-Web-Client": "v2"}, "csrf_rejected"),
        ({"Tailscale-User-Login": "other@example.com"}, "identity_rejected"),
    ],
)
def test_rejects_host_origin_csrf_and_identity_attacks(
    config: AccessConfig, mutation: dict[str, str], expected: str
) -> None:
    headers = _headers(config)
    headers.update(mutation)
    decision = evaluate_request(
        method="POST",
        target="/api/cortex/threads",
        headers=headers,
        client_host="127.0.0.1",
        config=config,
    )
    assert not decision.allowed
    assert decision.category == expected


def test_rejects_missing_or_overbroad_capability(config: AccessConfig) -> None:
    missing = _headers(config)
    missing.pop("Tailscale-App-Capabilities")
    wrong = _headers(config)
    wrong["Tailscale-App-Capabilities"] = json.dumps(
        {
            config.identity.app_capability: [{"role": "reader"}],
            "attacker.example/cap/admin": [{"role": "owner"}],
        }
    )

    for headers in (missing, wrong):
        decision = evaluate_request(
            method="GET",
            target="/",
            headers=headers,
            client_host="127.0.0.1",
            config=config,
        )
        assert decision.category == "identity_rejected"


def test_accepts_capability_only_for_explicit_tagged_source(
    config: AccessConfig,
) -> None:
    headers = _headers(config)
    headers.pop("Tailscale-User-Login")

    decision = evaluate_request(
        method="GET",
        target="/",
        headers=headers,
        client_host="::1",
        config=config,
    )

    assert decision.allowed


def test_rejects_direct_tailnet_or_lan_connection(config: AccessConfig) -> None:
    decision = evaluate_request(
        method="GET",
        target="/",
        headers=_headers(config),
        client_host="100.64.0.42",
        config=config,
    )
    assert decision.category == "non_loopback_proxy"


@pytest.mark.parametrize(
    "target",
    [
        "https://attacker.example/",
        "//attacker.example/",
        "/safe\r\nX-Evil: yes",
    ],
)
def test_rejects_non_origin_form_or_injected_targets(
    config: AccessConfig, target: str
) -> None:
    decision = evaluate_request(
        method="GET",
        target=target,
        headers=_headers(config),
        client_host="127.0.0.1",
        config=config,
    )
    assert decision.category == "target_rejected"


def test_safe_navigation_does_not_require_mutation_csrf_header(
    config: AccessConfig,
) -> None:
    headers = _headers(config)
    headers.pop("X-Cortex-Web-Client")
    headers.pop("Origin")
    headers["Sec-Fetch-Site"] = "none"

    decision = evaluate_request(
        method="GET",
        target="/",
        headers=headers,
        client_host="127.0.0.1",
        config=config,
    )

    assert decision.allowed
