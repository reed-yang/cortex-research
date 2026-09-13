"""⟦S3.4/D6⟧ A reversal is a decision, not a retry — driven through the CLI.

Every key here is the CLI's own `_decision_key`, never a hand-written one,
because the key is exactly what makes this reachable: it is a pure function of
(command, release_id, manifest_sha256), so the second `approve` of a release
that was revoked in between arrives carrying a key the store has already seen.
A store that answers that from its receipt leaves the release revoked while the
command prints success — in both directions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.cli import main
from cortex_platform.product.control.store import ControlStore
from cortex_platform.product.runtime_update.cli import _decision_key

RELEASE = "hermes-0.15.0"
DIGEST = "c" * 64


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "state", tmp_path / "data"


def _decide(command: str, roots: tuple[Path, Path], capsys) -> str:
    """Run the real `cortex runtime approve|revoke`, key and all."""

    state, data = roots
    assert (
        main(
            [
                "runtime",
                "--state-dir",
                str(state),
                "--data-dir",
                str(data),
                command,
                "--release-id",
                RELEASE,
                "--manifest-sha256",
                DIGEST,
            ],
            environ={"HOME": str(state.parent / "home")},
            platform="darwin",
        )
        == 0
    )
    return capsys.readouterr().out.strip()


def _approved(roots: tuple[Path, Path]) -> bool:
    _state, data = roots
    return ControlStore(data / "control.db").runtime_release_approved(RELEASE, DIGEST)


def _counts(roots: tuple[Path, Path]) -> tuple[int, int]:
    """(decision rows, audit rows) — the forensic record of what took effect."""

    _state, data = roots
    with sqlite3.connect(data / "control.db") as conn:
        decisions = conn.execute(
            "SELECT COUNT(*) FROM runtime_release_approvals"
        ).fetchone()[0]
        audits = conn.execute(
            "SELECT COUNT(*) FROM control_audit "
            "WHERE aggregate_type = 'runtime_release_approval'"
        ).fetchone()[0]
    return int(decisions), int(audits)


def test_the_command_key_is_the_same_for_every_run_of_one_command() -> None:
    """The premise, stated once so the cases below are not accidents."""

    assert _decision_key("approve", RELEASE, DIGEST) == _decision_key(
        "approve", RELEASE, DIGEST
    )
    assert _decision_key("approve", RELEASE, DIGEST) != _decision_key(
        "revoke", RELEASE, DIGEST
    )


def test_approving_again_after_a_revoke_really_approves(
    roots: tuple[Path, Path], capsys
) -> None:
    assert _decide("approve", roots, capsys).startswith("approved:")
    assert _approved(roots) is True
    assert _decide("revoke", roots, capsys).startswith("revoked:")
    assert _approved(roots) is False
    # The second `approve` reuses the first one's key. It is still a decision.
    assert _decide("approve", roots, capsys).startswith("approved:")
    assert _approved(roots) is True
    # Three decisions took effect, so three rows and three audit events.
    assert _counts(roots) == (3, 3)


def test_revoking_again_after_an_approve_really_revokes(
    roots: tuple[Path, Path], capsys
) -> None:
    """The other direction, which is the one that matters for containment."""

    assert _decide("revoke", roots, capsys).startswith("revoked:")
    assert _approved(roots) is False
    assert _decide("approve", roots, capsys).startswith("approved:")
    assert _approved(roots) is True
    # The second `revoke` reuses the first one's key, and a release that the
    # operator has revoked must not keep running because of a receipt.
    assert _decide("revoke", roots, capsys).startswith("revoked:")
    assert _approved(roots) is False
    assert _counts(roots) == (3, 3)


def test_repeating_a_decision_that_is_already_in_force_is_still_a_retry(
    roots: tuple[Path, Path], capsys
) -> None:
    """Retry idempotency is what the key is for, and it survives the fix."""

    assert _decide("approve", roots, capsys).startswith("approved:")
    assert _decide("approve", roots, capsys).startswith("approved:")
    assert _approved(roots) is True
    assert _counts(roots) == (1, 1)

    assert _decide("revoke", roots, capsys).startswith("revoked:")
    assert _decide("revoke", roots, capsys).startswith("revoked:")
    assert _approved(roots) is False
    # Four commands, two decisions: only the two that changed the standing
    # decision left a row and an audit event behind.
    assert _counts(roots) == (2, 2)
