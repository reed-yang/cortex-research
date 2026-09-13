from pathlib import Path

import pytest

from distribution.state_safety import (
    FreshStateSafetyPort,
    StateSafetyError,
    UpgradeRequest,
)


def _request(tmp_path: Path) -> UpgradeRequest:
    return UpgradeRequest(
        current_bundle_digest="1" * 64,
        candidate_bundle_digest="2" * 64,
        current_version="cortex-dev-1-1111111111111111",
        candidate_version="cortex-dev-2-2222222222222222",
        candidate_control_schema=10,
        control_database=tmp_path / "control.db",
        identity_companion=tmp_path / ".control.db.transport.key",
        runtime_root=tmp_path / "runtime",
    )


def test_fresh_authorization_is_exact_and_one_use(tmp_path: Path) -> None:
    port = FreshStateSafetyPort()
    request = _request(tmp_path)
    authorization = port.authorize(request)

    port.consume(authorization, request)
    with pytest.raises(StateSafetyError, match="not current"):
        port.consume(authorization, request)


def test_fresh_authorization_expires_if_state_appears_before_consumption(
    tmp_path: Path,
) -> None:
    port = FreshStateSafetyPort()
    request = _request(tmp_path)
    authorization = port.authorize(request)
    request.control_database.write_bytes(b"database")
    request.identity_companion.write_bytes(b"i" * 32)

    with pytest.raises(StateSafetyError, match="verified backup authorization"):
        port.consume(authorization, request)


def test_fresh_authorization_rejects_partial_and_existing_state(tmp_path: Path) -> None:
    port = FreshStateSafetyPort()
    request = _request(tmp_path)
    request.control_database.write_bytes(b"database")

    with pytest.raises(StateSafetyError, match="incomplete"):
        port.authorize(request)

    request.identity_companion.write_bytes(b"i" * 32)
    with pytest.raises(StateSafetyError, match="verified backup authorization"):
        port.authorize(request)


def test_fresh_authorization_treats_a_broken_state_link_as_present(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.control_database.symlink_to(tmp_path / "missing-control.db")

    with pytest.raises(StateSafetyError, match="incomplete"):
        FreshStateSafetyPort().authorize(request)
