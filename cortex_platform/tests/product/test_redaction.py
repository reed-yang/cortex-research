"""⟦P5.6⟧ What a failure message may carry onto a status surface or a log."""

from __future__ import annotations

import secrets

from cortex_platform.product.redaction import DETAIL_LIMIT, redact

FAKE_TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"


def test_a_bot_token_never_survives() -> None:
    assert FAKE_TOKEN not in redact(f"HTTP 401 for {FAKE_TOKEN} at getUpdates")


def test_a_bot_api_path_segment_is_cut_out() -> None:
    text = redact(f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates failed")
    assert FAKE_TOKEN not in text
    assert "/bot[redacted]/getUpdates" in text


def test_a_url_query_string_is_cut_out() -> None:
    text = redact("GET https://host/path?token=abc&offset=1 timed out")
    assert "abc" not in text
    assert "offset" not in text
    assert "https://host/path?[redacted]" in text


def test_key_value_secrets_are_cut_out() -> None:
    text = redact("Authorization: Bearer xyz.123 api_key=sk-1234567890abcdef1234")
    assert "xyz.123" not in text
    assert "sk-1234567890abcdef1234" not in text


def test_the_result_is_one_bounded_line() -> None:
    text = redact("line one\nline\ttwo   " + "x" * 1000)
    assert "\n" not in text and "\t" not in text
    assert len(text) == DETAIL_LIMIT
    assert text.startswith("line one line two ")


def test_an_ordinary_message_is_left_alone() -> None:
    assert redact("worker response timed out") == "worker response timed out"
    assert redact("operation_conflict") == "operation_conflict"



# ⟦Batch F P56-OBS-8⟧ Three shapes this daemon actually holds, and the stray `]`.


def test_a_64_hex_secret_is_cut_and_a_shorter_hex_run_is_not() -> None:
    key = "3f2a91c0" * 8
    assert len(key) == 64
    assert redact(f"control token {key} rejected") == "control token [redacted] rejected"
    # 32 hex (a uuid hex, a window id's tail) is a diagnostic and survives.
    assert redact("window transport-activation_" + "ab" * 16) == (
        "window transport-activation_" + "ab" * 16
    )


def test_the_daemons_own_control_token_shape_is_cut() -> None:
    token = secrets.token_urlsafe(32)
    assert len(token) == 43
    assert redact(f"presented {token} to /api/v1/shutdown") == (
        "presented [redacted] to /api/v1/shutdown"
    )
    assert redact(f"X-Cortex-Control-Token: {token}") == "X-Cortex-Control-Token: [redacted]"


def test_url_userinfo_is_cut_and_the_host_survives() -> None:
    assert redact("connect https://cortex:s3cr3tpassw0rd@api.telegram.org/ failed") == (
        "connect https://[redacted]@api.telegram.org/ failed"
    )
    assert redact("proxy http://user@proxy.local:3128/") == "proxy http://[redacted]@proxy.local:3128/"


def test_a_bearer_credential_leaves_no_stray_bracket() -> None:
    line = "401 headers={'Authorization': 'Bearer sk-ant-abcdefghijklmnopqrstuv'}"
    assert redact(line) == "401 headers={'Authorization': 'Bearer [redacted]'}"
    assert redact("[Bearer abc]") == "[Bearer [redacted]"


def test_redaction_is_idempotent() -> None:
    samples = [
        "401 headers={'Authorization': 'Bearer sk-ant-abcdefghijklmnopqrstuv'}",
        "token=abc] secret: no api_key = xyz",
        "https://u:p@h/x?y=1 bot 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "control token " + "0" * 64 + " and " + secrets.token_urlsafe(32),
    ]
    for sample in samples:
        once = redact(sample)
        assert redact(once) == once, sample
