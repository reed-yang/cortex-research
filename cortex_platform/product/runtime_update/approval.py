"""D6: the operator's approval of one exact release, as a Control decision.

The activation gate (control schema 12) answers "may the managed runtime
dispatch at all". This answers a different question — "is *this* release, with
*these* bytes, one the operator approved" — and D6 keeps them two decisions on
purpose: enabling dispatch must not imply approving whatever happens to be
staged, and approving a build must not turn dispatch on.

The identity is `(release_id, manifest_sha256)`, never `release_id` alone. A
release id is a name the packager chooses; the manifest digest is what
`slot.json`, the registry record and the worker's own `measure_identity` already
bind, so approving a name would approve whatever bytes later reused it.

`RuntimeUpdateService` takes a gate rather than a store: the updater has never
imported Control, the architectural fence in `test_worker_v2.py` exists to keep
boundaries like this one, and a protocol lets the product wire the real decision
while the packaging tests keep testing packaging.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ReleaseApprovalError(RuntimeError):
    """A release was asked to run without the operator's approval of its bytes.

    Typed and carrying a reason code because this is an operator-facing refusal,
    not a fault: the answer is a decision, not a retry.
    """

    def __init__(self, reason_code: str, release_id: str, manifest_sha256: str) -> None:
        super().__init__(
            f"{reason_code}: {release_id} @ {manifest_sha256}"
        )
        self.reason_code = reason_code
        self.release_id = release_id
        self.manifest_sha256 = manifest_sha256


@runtime_checkable
class ReleaseApprovalGate(Protocol):
    """Whatever can answer the approval question for an exact release."""

    def approved(self, release_id: str, manifest_sha256: str) -> bool: ...


class ControlReleaseApprovals:
    """The real gate: the append-only decision table in the control store."""

    def __init__(self, store: object) -> None:
        self._store = store

    def approved(self, release_id: str, manifest_sha256: str) -> bool:
        return bool(
            self._store.runtime_release_approved(  # type: ignore[attr-defined]
                release_id, manifest_sha256
            )
        )


def require_release_approval(
    gate: ReleaseApprovalGate | None, release_id: str, manifest_sha256: str
) -> None:
    """Refuse unless this exact release carries a standing `approve` decision.

    A missing gate refuses rather than permits. The alternative — treating "no
    approval authority configured" as "everything is approved" — would make the
    whole decision disappear on exactly the deployment that forgot to wire it.
    """

    if gate is None:
        raise ReleaseApprovalError(
            "release_approval_unavailable", release_id, manifest_sha256
        )
    if not gate.approved(release_id, manifest_sha256):
        raise ReleaseApprovalError(
            "release_not_approved", release_id, manifest_sha256
        )
