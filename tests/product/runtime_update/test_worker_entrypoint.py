"""The attested worker entrypoint must be the one the worker executes.

`import_release` verifies that `content/<worker_entrypoint>` exists before a
release is accepted. If the worker then loads some other file, the manifest
field attests something with no causal role: the release says one thing and the
process does another. The immutable content digest keeps that from being an
injection path, but a field that cannot be wrong is not evidence.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.supervisor import (
    WorkerCrashed,
    WorkerProtocolError,
    WorkerSupervisor,
)
from cortex_platform.product.runtime_update.worker import _safe_entrypoint


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "nested/worker.py", "..", ".", "", "a\x00b"],
)
def test_the_validator_rejects_every_name_a_manifest_could_not_attest(
    name: str,
) -> None:
    """Unit-level, because two of these never reach it through a process.

    The end-to-end test below cannot prove the validator handles "" or a NUL
    byte: argparse's guard rejects the empty string in `main`, and `Popen`
    raises on a NUL while building argv in the parent. Both are refusals, but
    neither is this function's refusal, so its contract is pinned directly.
    """

    with pytest.raises(RuntimeError, match="worker_entrypoint_invalid"):
        _safe_entrypoint(name)


def test_the_validator_accepts_a_plain_file_name() -> None:
    assert _safe_entrypoint("runtime_worker.py") == "runtime_worker.py"


def test_a_backslash_is_a_file_name_here_not_a_separator() -> None:
    """Deliberate, and recorded because it reads like a bug.

    On macOS a backslash is an ordinary filename character, and
    `ReleaseManifest` accepts it for exactly that reason. Rejecting it here
    would make the validator stricter than the rule that admits releases —
    reintroducing the two-meanings problem this slice exists to remove. The
    guard covers `os.sep`/`os.altsep` so the rule stays right if it is ever
    evaluated on a platform where a backslash separates.
    """

    assert _safe_entrypoint("a\\b") == "a\\b"


def _staged(tmp_path: Path, release_factory, entrypoint: str = "hermes_worker.py"):
    from cortex_platform.product.runtime_update.models import canonical_json
    from cortex_platform.product.runtime_update.service import (
        DigestPinVerifier,
        RuntimeUpdateService,
    )

    artifact, manifest, catalog, attestation = release_factory(
        worker_entrypoint=entrypoint
    )
    service = RuntimeUpdateService(
        tmp_path / "updates",
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=FakePythonStager(),
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    return service, service.stage(manifest["release_id"]), manifest


def test_a_release_naming_an_unconventional_entrypoint_can_be_activated(
    tmp_path: Path, release_factory
) -> None:
    """The production path, end to end, through the real service and CLI probe.

    Before this slice, such a release could be imported — `import_release`
    verifies the attested file exists — and then never activated, because the
    worker looked for `runtime_worker.py` regardless. The attestation was not
    only uncorroborated, it was actively misleading about what would run.
    """

    from cortex_platform.product.runtime_update.cli import _probe, _slot_entrypoint

    entrypoint = "hermes_worker.py"
    _, candidate, _ = _staged(tmp_path, release_factory, entrypoint)

    assert not (candidate.slot_dir / "content" / "runtime_worker.py").exists()
    assert _slot_entrypoint(candidate) == entrypoint
    assert _probe(candidate) is True


def test_a_tampered_slot_manifest_cannot_redirect_the_entrypoint(
    tmp_path: Path, release_factory
) -> None:
    """`_make_immutable` sets mode bits; the owner can undo them.

    So the manifest on disk is not evidence by itself. Rewriting it to name a
    different file that is also present and digest-pinned inside `content/`
    must be caught by the recorded `manifest_sha256`, and the probe must report
    the slot unhealthy rather than run the substituted file.
    """

    from cortex_platform.product.runtime_update.cli import _probe, _slot_entrypoint

    service, candidate, manifest = _staged(tmp_path, release_factory)
    content = candidate.slot_dir / "content"
    content.chmod(0o755)
    (content / "substitute.py").write_text(
        _handler("substituted"), encoding="utf-8"
    )
    tampered = dict(manifest)
    tampered["worker_entrypoint"] = "substitute.py"
    manifest_path = candidate.slot_dir / "manifest.json"
    candidate.slot_dir.chmod(0o755)
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ValueError, match="attested digest"):
        _slot_entrypoint(candidate)
    assert _probe(candidate) is False


def test_an_unreadable_slot_reports_unhealthy_rather_than_raising(
    tmp_path: Path, release_factory
) -> None:
    """`rollback` does not wrap its probe the way `activate` does.

    A probe that raises would escape it as an unrelated exception type, so the
    probe answers False and lets the service raise its own typed refusal.
    """

    from cortex_platform.product.runtime_update.cli import _probe

    service, candidate, _ = _staged(tmp_path, release_factory)
    candidate.slot_dir.chmod(0o755)
    (candidate.slot_dir / "manifest.json").unlink()

    assert _probe(candidate) is False


def _handler(marker: str) -> str:
    return f"def handle(method, params):\n    return {{'loaded': {marker!r}}}\n"


def _slot(tmp_path: Path) -> tuple[Path, Path]:
    candidate = tmp_path / "content"
    state = tmp_path / "state"
    candidate.mkdir()
    state.mkdir()
    return candidate, state


def test_the_attested_entrypoint_is_the_one_that_runs(tmp_path: Path) -> None:
    """Both files exist; only the attested one may be loaded."""

    candidate, state = _slot(tmp_path)
    (candidate / "runtime_worker.py").write_text(_handler("default"), encoding="utf-8")
    (candidate / "alternate_worker.py").write_text(
        _handler("attested"), encoding="utf-8"
    )

    with WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint="alternate_worker.py",
    ) as worker:
        assert worker.request("health", {}, timeout=3) == {"loaded": "attested"}


def test_a_missing_attested_entrypoint_refuses_even_when_another_exists(
    tmp_path: Path,
) -> None:
    """The conventional name must not rescue a release that attests another."""

    candidate, state = _slot(tmp_path)
    (candidate / "runtime_worker.py").write_text(_handler("default"), encoding="utf-8")

    worker = WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint="absent_worker.py",
    )
    with pytest.raises((WorkerCrashed, WorkerProtocolError)):
        worker.start()
    worker.close(force=True)


@pytest.mark.parametrize(
    "entrypoint",
    ["../escape.py", "nested/worker.py", "..", "."],
)
def test_an_unsafe_entrypoint_never_escapes_the_candidate_tree(
    tmp_path: Path, entrypoint: str
) -> None:
    """End to end, across the process boundary, with a real escape target.

    This proves the outcome, not the mechanism, and the distinction is measured:
    bypassing `_safe_entrypoint` entirely leaves every case here still passing,
    because the audit-hook sandbox refuses to `open` a path outside the
    candidate tree — so `../escape.py` is stopped twice over. Two layers is the
    point; what this test must not be read as is evidence that the validator
    works. That is pinned directly against `_safe_entrypoint` above, where the
    empty and NUL cases also live, since they are refused before a worker even
    exists (argparse's guard, and `Popen` building argv).
    """

    candidate, state = _slot(tmp_path)
    (candidate / "runtime_worker.py").write_text(_handler("default"), encoding="utf-8")
    (tmp_path / "escape.py").write_text(_handler("escaped"), encoding="utf-8")
    # A real, loadable handler at the nested path, inside the sandbox roots.
    # Without it `nested/worker.py` is refused by `is_file()` alone and proves
    # nothing about the validator; with it, deleting the separator check makes
    # this case load successfully and the test go red.
    nested = candidate / "nested"
    nested.mkdir()
    (nested / "worker.py").write_text(_handler("nested"), encoding="utf-8")

    worker = WorkerSupervisor(
        python_executable=Path(sys.executable),
        candidate_root=candidate,
        state_root=state,
        worker_entrypoint=entrypoint,
    )
    with pytest.raises((WorkerCrashed, WorkerProtocolError, ValueError)):
        worker.start()
    worker.close(force=True)
