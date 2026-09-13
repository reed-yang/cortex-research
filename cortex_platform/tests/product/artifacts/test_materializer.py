from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from cortex_platform.product.artifacts import materializer as materializer_module
from cortex_platform.product.artifacts.materializer import (
    AssetRoot,
    FilesystemMaterializer,
    IntegrityError,
    MaterializationConflict,
    MaterializationRequest,
    MaterializerError,
)
from cortex_platform.product.artifacts.models import ValidationError

CONTENT = b"# Echo / TTT\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
OTHER_CONTENT = b"# Helios / TTT\n"
OTHER_DIGEST = hashlib.sha256(OTHER_CONTENT).hexdigest()


def _private_dir(path: Path, *, parents: bool = False) -> Path:
    path.mkdir(mode=0o700, parents=parents)
    path.chmod(0o700)
    return path


def _request(
    *,
    operation_id: str = "materialize-version-1",
    root_id: str = "artifacts",
    relative_path: str = "versions/living-brief-v1.md",
    sha256: str = DIGEST,
    byte_length: int = len(CONTENT),
) -> MaterializationRequest:
    return MaterializationRequest.from_dict(
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "root_id": root_id,
            "relative_path": relative_path,
            "sha256": sha256,
            "byte_length": byte_length,
            "media_type": "text/markdown",
            "parents": [],
        }
    )


def _materializer(root: Path, *, max_bytes: int = 1024) -> FilesystemMaterializer:
    return FilesystemMaterializer(
        [AssetRoot(root_id="artifacts", path=root, max_bytes=max_bytes)]
    )


class SimulatedCrash(BaseException):
    pass


class InterruptingMaterializer(FilesystemMaterializer):
    def __init__(self, roots: list[AssetRoot], interrupt_at: str) -> None:
        super().__init__(roots)
        self.interrupt_at = interrupt_at
        self.interrupted = False

    def _crash_checkpoint(self, point: str) -> None:
        if point == self.interrupt_at and not self.interrupted:
            self.interrupted = True
            raise SimulatedCrash(point)


class BarrierMaterializer(FilesystemMaterializer):
    def __init__(self, roots: list[AssetRoot], barrier: threading.Barrier) -> None:
        super().__init__(roots)
        self.barrier = barrier

    def _lock_checkpoint(self, point: str, request: MaterializationRequest) -> None:
        del request
        if point == "before_operation_lock":
            self.barrier.wait(timeout=5)


class IntrudingMaterializer(FilesystemMaterializer):
    def __init__(self, roots: list[AssetRoot], final_path: Path) -> None:
        super().__init__(roots)
        self.final_path = final_path

    def _crash_checkpoint(self, point: str) -> None:
        if point == "before_final_link":
            self.final_path.write_bytes(b"unowned")
            self.final_path.chmod(0o600)


def test_materialize_stages_verifies_fsyncs_and_atomically_publishes(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    materializer = _materializer(root)

    result = materializer.materialize(_request(), CONTENT)

    assert result.to_dict() == {
        "schema_version": 1,
        "operation_id": "materialize-version-1",
        "root_id": "artifacts",
        "relative_path": "versions/living-brief-v1.md",
        "sha256": DIGEST,
        "byte_length": len(CONTENT),
        "media_type": "text/markdown",
        "parents": [],
        "replayed": False,
        "recovered_from": None,
    }
    assert (root / "versions" / "living-brief-v1.md").read_bytes() == CONTENT
    assert not (root / ".cortex-artifacts-v1" / "staging" / "materialize-version-1.part").exists()


def test_materialization_request_v1_is_closed_and_content_canonical() -> None:
    raw = _request().to_dict()
    assert MaterializationRequest.from_dict(raw).to_dict() == raw

    for field, value in (
        ("schema_version", 2),
        ("sha256", DIGEST.upper()),
        ("byte_length", True),
        ("media_type", "text/markdown; charset=utf-8"),
        ("root_id", "Artifacts"),
    ):
        changed = dict(raw)
        changed[field] = value
        with pytest.raises(ValidationError):
            MaterializationRequest.from_dict(changed)

    extra = {**raw, "absolute_path": "/tmp/private"}
    with pytest.raises(ValidationError):
        MaterializationRequest.from_dict(extra)

    unordered = dict(raw)
    unordered["parents"] = [
        {"artifact_version_id": "version-z", "sha256": "b" * 64},
        {"artifact_version_id": "version-a", "sha256": "a" * 64},
    ]
    with pytest.raises(ValidationError, match="canonical order"):
        MaterializationRequest.from_dict(unordered)


def test_exact_operation_replay_adopts_only_matching_final_bytes(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    materializer = _materializer(root)
    request = _request()
    materializer.materialize(request, CONTENT)

    replay = materializer.materialize(request, None)

    assert replay.replayed is True
    assert replay.recovered_from == "final"

    (root / "versions" / "living-brief-v1.md").write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        materializer.materialize(request, None)


def test_same_operation_request_drift_and_different_operation_target_reuse_fail(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    materializer = _materializer(root)
    materializer.materialize(_request(), CONTENT)

    with pytest.raises(MaterializationConflict, match="operation"):
        materializer.materialize(
            _request(relative_path="request-drift/different.md"), CONTENT
        )
    assert not (root / "request-drift").exists()

    with pytest.raises(MaterializationConflict, match="target"):
        materializer.materialize(
            _request(operation_id="materialize-version-2"), CONTENT
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape.md",
        "/absolute.md",
        "versions/../../escape.md",
        "versions//brief.md",
        "versions/./brief.md",
        "versions\\brief.md",
        "versions/e\u0301.md",
        "versions/line\nbreak.md",
        ".cortex-artifacts-v1/operations/stolen.json",
    ],
)
def test_traversal_and_noncanonical_paths_are_rejected_before_writes(
    tmp_path: Path, relative_path: str
) -> None:
    root = tmp_path / "assets"
    _private_dir(root)

    with pytest.raises((ValueError, MaterializerError)):
        _materializer(root).materialize(_request(relative_path=relative_path), CONTENT)

    assert list(root.iterdir()) == []


def test_unknown_root_and_oversize_are_rejected_before_writes(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    materializer = _materializer(root, max_bytes=len(CONTENT) - 1)

    with pytest.raises(MaterializerError, match="unknown asset root"):
        materializer.materialize(_request(root_id="unknown"), CONTENT)
    with pytest.raises(MaterializerError, match="size limit"):
        materializer.materialize(_request(), CONTENT)

    assert list(root.iterdir()) == []


def test_directory_replacement_between_lstat_and_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_dir(tmp_path / "root")
    child = _private_dir(root / "child")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_open = materializer_module.os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "child" and not replaced:
            replaced = True
            # Keep the old inode allocated; unlink/recreate may reuse it on Linux.
            child.rename(root / "retained-child")
            _private_dir(child)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(materializer_module.os, "open", replacing_open)
    try:
        with pytest.raises(MaterializerError, match="changed during open"):
            materializer_module._open_directory(
                root_fd, "child", create=False, private=True
            )
    finally:
        os.close(root_fd)


def test_file_size_change_between_lstat_and_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_dir(tmp_path / "root")
    target = root / "content.md"
    target.write_bytes(b"short")
    target.chmod(0o600)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_open = materializer_module.os.open
    changed = False

    def changing_open(path, flags, *args, **kwargs):
        nonlocal changed
        if path == "content.md" and not changed:
            changed = True
            target.write_bytes(b"longer content")
            target.chmod(0o600)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(materializer_module.os, "open", changing_open)
    try:
        with pytest.raises(MaterializerError, match="changed during open"):
            materializer_module._read_regular(root_fd, "content.md", limit=100)
    finally:
        os.close(root_fd)


def test_symlinked_root_administration_parent_and_target_are_rejected(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    _private_dir(outside)

    symlink_root = tmp_path / "root-link"
    symlink_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MaterializerError, match="root"):
        _materializer(symlink_root).materialize(_request(), CONTENT)

    root = tmp_path / "assets"
    _private_dir(root)
    (root / ".cortex-artifacts-v1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MaterializerError, match="symlink"):
        _materializer(root).materialize(_request(), CONTENT)

    (root / ".cortex-artifacts-v1").unlink()
    (root / "versions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MaterializerError, match="symlink"):
        _materializer(root).materialize(_request(), CONTENT)

    (root / "versions").unlink()
    _private_dir(root / "versions")
    (root / "versions" / "living-brief-v1.md").symlink_to(outside / "escape")
    with pytest.raises(MaterializerError, match="symlink"):
        _materializer(root).materialize(_request(), CONTENT)


def test_case_and_unicode_directory_aliases_fail_closed(tmp_path: Path) -> None:
    case_root = tmp_path / "case-assets"
    _private_dir(case_root)
    _private_dir(case_root / "Versions")
    with pytest.raises(MaterializerError, match="alias"):
        _materializer(case_root).materialize(_request(), CONTENT)

    unicode_root = tmp_path / "unicode-assets"
    _private_dir(unicode_root)
    _private_dir(unicode_root / "e\u0301")
    with pytest.raises(MaterializerError, match="alias"):
        _materializer(unicode_root).materialize(
            _request(relative_path="é/brief.md"), CONTENT
        )

    final_root = tmp_path / "final-assets"
    _private_dir(final_root / "versions", parents=True)
    final_root.chmod(0o700)
    (final_root / "versions" / "Living-Brief-V1.md").write_bytes(CONTENT)
    with pytest.raises(MaterializerError, match="alias"):
        _materializer(final_root).materialize(_request(), CONTENT)


def test_preexisting_unowned_final_is_never_adopted_even_when_hash_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    _private_dir(root / "versions", parents=True)
    root.chmod(0o700)
    (root / "versions" / "living-brief-v1.md").write_bytes(CONTENT)

    with pytest.raises(MaterializationConflict, match="unowned"):
        _materializer(root).materialize(_request(), None)


@pytest.mark.parametrize(
    ("interrupt_at", "expected_recovery"),
    [
        ("after_stage_fsync", "staged"),
        ("after_publish_intent_fsync", "staged"),
        ("after_final_link", "dual-link"),
    ],
)
def test_crash_recovery_adopts_only_same_operation_matching_bytes(
    tmp_path: Path, interrupt_at: str, expected_recovery: str
) -> None:
    root = tmp_path / interrupt_at
    _private_dir(root)
    roots = [AssetRoot(root_id="artifacts", path=root, max_bytes=1024)]
    interrupted = InterruptingMaterializer(roots, interrupt_at)

    with pytest.raises(SimulatedCrash):
        interrupted.materialize(_request(), CONTENT)

    recovered = FilesystemMaterializer(roots).materialize(_request(), None)

    assert recovered.replayed is True
    assert recovered.recovered_from == expected_recovery
    assert (root / "versions" / "living-brief-v1.md").read_bytes() == CONTENT


def test_dual_link_recovery_requires_same_inode_and_finishes_publication(
    tmp_path: Path,
) -> None:
    root = _private_dir(tmp_path / "assets")
    roots = [AssetRoot(root_id="artifacts", path=root, max_bytes=1024)]
    interrupted = InterruptingMaterializer(roots, "after_final_link")
    with pytest.raises(SimulatedCrash):
        interrupted.materialize(_request(), CONTENT)

    stage = root / ".cortex-artifacts-v1" / "staging" / "materialize-version-1.part"
    final = root / "versions" / "living-brief-v1.md"
    assert stage.stat().st_ino == final.stat().st_ino
    assert stage.stat().st_nlink == final.stat().st_nlink == 2

    recovered = FilesystemMaterializer(roots).materialize(_request(), None)
    assert recovered.recovered_from == "dual-link"
    assert not stage.exists()
    assert final.stat().st_nlink == 1


def test_final_plus_unknown_extra_stage_fails_closed(tmp_path: Path) -> None:
    root = _private_dir(tmp_path / "assets")
    materializer = _materializer(root)
    request = _request()
    materializer.materialize(request, CONTENT)
    stage = root / ".cortex-artifacts-v1" / "staging" / "materialize-version-1.part"
    stage.write_bytes(CONTENT)
    stage.chmod(0o600)

    with pytest.raises(MaterializationConflict, match="stage|inode|link"):
        materializer.materialize(request, None)

    assert (root / "versions" / "living-brief-v1.md").read_bytes() == CONTENT


def test_atomic_publication_never_replaces_racing_unknown_final(tmp_path: Path) -> None:
    root = _private_dir(tmp_path / "assets")
    final = root / "versions" / "living-brief-v1.md"
    materializer = IntrudingMaterializer(
        [AssetRoot(root_id="artifacts", path=root, max_bytes=1024)], final
    )

    with pytest.raises(MaterializationConflict, match="final|unowned"):
        materializer.materialize(_request(), CONTENT)

    assert final.read_bytes() == b"unowned"


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [
        ("Versions/Living-Brief.md", "versions/living-brief.md"),
        ("Ångström/Brief.md", "ångström/brief.md"),
    ],
)
def test_alias_targets_share_one_lock_and_only_one_operation_can_publish(
    tmp_path: Path, first_path: str, second_path: str
) -> None:
    root = _private_dir(tmp_path / "assets")
    barrier = threading.Barrier(2)
    materializer = BarrierMaterializer(
        [AssetRoot(root_id="artifacts", path=root, max_bytes=1024)], barrier
    )
    first = _request(relative_path=first_path)
    second = _request(
        operation_id="materialize-version-2",
        relative_path=second_path,
        sha256=OTHER_DIGEST,
        byte_length=len(OTHER_CONTENT),
    )

    def invoke(
        request: MaterializationRequest, content: bytes
    ) -> object:
        try:
            return materializer.materialize(request, content)
        except Exception as exc:  # Test captures both race outcomes deterministically.
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(invoke, first, CONTENT),
            executor.submit(invoke, second, OTHER_CONTENT),
        ]
        resolved = [future.result(timeout=10) for future in outcomes]

    successes = [value for value in resolved if not isinstance(value, Exception)]
    failures = [value for value in resolved if isinstance(value, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], MaterializationConflict)
    winner = successes[0]
    expected = CONTENT if winner.operation_id == first.operation_id else OTHER_CONTENT
    assert (root / winner.relative_path).read_bytes() == expected
    assert sum(1 for path in root.rglob("*.md") if path.is_file()) == 1


def test_physical_root_capability_is_shared_across_registry_root_ids(
    tmp_path: Path,
) -> None:
    root = _private_dir(tmp_path / "assets")
    barrier = threading.Barrier(2)
    first_materializer = BarrierMaterializer(
        [AssetRoot(root_id="artifacts", path=root, max_bytes=1024)], barrier
    )
    second_materializer = BarrierMaterializer(
        [AssetRoot(root_id="other", path=root, max_bytes=1024)], barrier
    )
    first = _request()
    second = _request(
        operation_id="materialize-version-2",
        root_id="other",
        sha256=OTHER_DIGEST,
        byte_length=len(OTHER_CONTENT),
    )

    def invoke(
        materializer: FilesystemMaterializer,
        request: MaterializationRequest,
        content: bytes,
    ) -> object:
        try:
            return materializer.materialize(request, content)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(invoke, first_materializer, first, CONTENT),
            executor.submit(invoke, second_materializer, second, OTHER_CONTENT),
        ]
        outcomes = [future.result(timeout=10) for future in futures]

    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert sum(isinstance(value, MaterializationConflict) for value in outcomes) == 1
    final = root / "versions" / "living-brief-v1.md"
    assert final.read_bytes() in {CONTENT, OTHER_CONTENT}
    assert final.stat().st_nlink == 1


def test_crash_recovery_rejects_mismatched_staged_bytes(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    roots = [AssetRoot(root_id="artifacts", path=root, max_bytes=1024)]
    interrupted = InterruptingMaterializer(roots, "after_stage_fsync")
    with pytest.raises(SimulatedCrash):
        interrupted.materialize(_request(), CONTENT)

    staged = root / ".cortex-artifacts-v1" / "staging" / "materialize-version-1.part"
    staged.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        FilesystemMaterializer(roots).materialize(_request(), None)


def test_actual_stream_size_and_hash_are_verified_before_publication(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    materializer = _materializer(root)

    with pytest.raises(IntegrityError, match="length"):
        materializer.materialize(_request(byte_length=len(CONTENT) + 1), CONTENT)
    assert not (root / "versions" / "living-brief-v1.md").exists()

    other = CONTENT[:-2] + b"X\n"
    other_root = tmp_path / "other"
    _private_dir(other_root)
    with pytest.raises(IntegrityError, match="hash"):
        _materializer(other_root).materialize(_request(), other)


def test_asset_root_registry_rejects_aliases_and_invalid_limits(tmp_path: Path) -> None:
    root = _private_dir(tmp_path / "assets")

    with pytest.raises(ValueError):
        FilesystemMaterializer(
            [
                AssetRoot("artifacts", root, 100),
                AssetRoot("other", root, 100),
            ]
        )
    with pytest.raises(ValueError):
        AssetRoot("Artifacts", root, 100)
    with pytest.raises(ValueError):
        AssetRoot("artifacts", root, 0)


def test_registry_rejects_nested_roots_and_symlink_ancestors(tmp_path: Path) -> None:
    outer = _private_dir(tmp_path / "outer")
    inner = _private_dir(outer / "inner")
    with pytest.raises(ValueError, match="overlap"):
        FilesystemMaterializer(
            [
                AssetRoot("artifacts", outer, 100),
                AssetRoot("other", inner, 100),
            ]
        )

    real_parent = _private_dir(tmp_path / "real-parent")
    _private_dir(real_parent / "assets")
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(MaterializerError, match="ancestor|symlink"):
        FilesystemMaterializer(
            [AssetRoot("artifacts", alias_parent / "assets", 100)]
        )


def test_root_and_existing_administration_state_must_be_owner_private(
    tmp_path: Path,
) -> None:
    insecure_root = _private_dir(tmp_path / "insecure-root")
    insecure_root.chmod(0o755)
    with pytest.raises(MaterializerError, match="private|mode"):
        _materializer(insecure_root)

    for insecure_part in (".cortex-artifacts-v1", "locks", "operations"):
        root = _private_dir(tmp_path / f"assets-{insecure_part.replace('.', 'dot')}")
        admin = root / ".cortex-artifacts-v1"
        if insecure_part == ".cortex-artifacts-v1":
            _private_dir(admin)
            admin.chmod(0o755)
        else:
            _private_dir(admin)
            child = _private_dir(admin / insecure_part)
            child.chmod(0o755)
        with pytest.raises(MaterializerError, match="private|mode"):
            _materializer(root).materialize(_request(), CONTENT)

    root = _private_dir(tmp_path / "assets-lock-file")
    admin = _private_dir(root / ".cortex-artifacts-v1")
    for name in ("operations", "targets", "staging", "publish", "locks"):
        _private_dir(admin / name)
    lock = admin / "locks" / "operation-materialize-version-1.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    with pytest.raises(MaterializerError, match="private|mode"):
        _materializer(root).materialize(_request(), CONTENT)


def test_parent_and_manifest_limits_are_enforced_before_any_write(tmp_path: Path) -> None:
    root = _private_dir(tmp_path / "assets")
    raw = _request().to_dict()
    raw["parents"] = [
        {
            "artifact_version_id": f"v{index:03d}-" + "x" * 122,
            "sha256": f"{index:064x}",
        }
        for index in range(256)
    ]
    raw["relative_path"] = "/".join(["long-segment-" + "x" * 80] * 20)
    raw["media_type"] = "a" * 5_000 + "/markdown"
    oversized_manifest_request = MaterializationRequest.from_dict(raw)

    with pytest.raises(MaterializerError, match="manifest.*limit"):
        _materializer(root, max_bytes=10_000).materialize(
            oversized_manifest_request, CONTENT
        )
    assert list(root.iterdir()) == []

    too_many = dict(raw)
    assert isinstance(too_many["parents"], list)
    too_many["parents"] = too_many["parents"] + [
        {"artifact_version_id": "v999-extra", "sha256": "f" * 64}
    ]
    with pytest.raises(ValidationError, match="parent.*limit"):
        MaterializationRequest.from_dict(too_many)


def test_direct_materialization_request_rejects_boolean_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema"):
        replace(_request(), schema_version=True)


def test_final_file_is_regular_single_link_and_private_admin_files_are_private(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    _private_dir(root)
    _materializer(root).materialize(_request(), CONTENT)

    final = root / "versions" / "living-brief-v1.md"
    assert final.stat().st_nlink == 1
    admin = root / ".cortex-artifacts-v1"
    assert os.stat(admin).st_mode & 0o077 == 0
    for path in admin.rglob("*"):
        assert path.lstat().st_mode & 0o077 == 0
