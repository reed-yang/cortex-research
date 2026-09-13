from __future__ import annotations

import copy
import getpass
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_platform.runtime_staging import PythonRuntime
from distribution.product_manifest import (
    NODE_MAX_EXECUTABLE_BYTES,
    NODE_VERSION_RANGE,
    NodeRuntime,
    ProductManifestError,
    PythonRuntime,
    _darwin_system_linkage,
    _is_system_library_path,
    _mode_writable_by_effective_user,
    build_product_manifest,
    probe_node_runtime,
    stage_node_runtime,
    validate_product_manifest,
    verify_staged_node_runtime,
)


def _node(tmp_path: Path) -> Path:
    executable = tmp_path / "node"
    executable.write_bytes((b"fixture node executable\n" * 2048)[:48 * 1024])
    executable.chmod(0o500)
    return executable


def _runner(
    executable: Path,
    *,
    architecture: str = "arm64",
    version: str = "v26.0.0",
    exec_path: Path | None = None,
    platform: str = "darwin",
    shared_objects: list[str] | None = None,
) -> tuple[int, str, str]:
    reported_executable = exec_path or executable
    return (
        0,
        json.dumps(
            {
                "architecture": architecture,
                "execPath": str(reported_executable),
                "platform": platform,
                "sharedObjects": shared_objects
                or [str(reported_executable), "/usr/lib/libSystem.B.dylib"],
                "version": version,
            }
        ),
        "",
    )


def _system_linkage(_executable: Path) -> None:
    return None


def test_node_policy_binds_a_compatible_exact_executable(tmp_path: Path) -> None:
    executable = _node(tmp_path)
    runtime = probe_node_runtime(
        executable,
        run=_runner,
        expected_system="Darwin",
        expected_machine="arm64",
    )

    assert runtime.version == "v26.0.0"
    assert runtime.executable_sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = build_product_manifest(runtime)
    assert validate_product_manifest(manifest) == manifest
    assert manifest["node_runtime"]["policy"]["version_range"] == NODE_VERSION_RANGE
    assert manifest["node_runtime"]["policy"]["forbidden_installed_commands"] == [
        "npm",
        "npx",
        "vinext",
        "wrangler",
    ]
    assert manifest["node_runtime"]["runtime_mode"] == "staged-host-node"
    assert manifest["node_runtime"]["execution_ownership"] == (
        "installer-owned-generation"
    )
    assert manifest["node_runtime"]["policy"]["identity_claim"] == (
        "opened-source-fd-to-immutable-generation-copy"
    )


@pytest.mark.parametrize(
    "version",
    ["v22.12.9", "v23.11.1", "v25.8.0", "v27.0.0", "26.0.0", "v026.0.0"],
)
def test_node_policy_rejects_unsupported_or_noncanonical_versions(
    tmp_path: Path,
    version: str,
) -> None:
    executable = _node(tmp_path)
    with pytest.raises(ProductManifestError, match="outside|invalid"):
        probe_node_runtime(
            executable,
            run=lambda path: _runner(path, version=version),
            expected_system="Darwin",
            expected_machine="arm64",
        )


@pytest.mark.parametrize("version", ["v22.13.0", "v24.0.0", "v26.0.0"])
def test_node_policy_accepts_only_supported_release_lines(
    tmp_path: Path,
    version: str,
) -> None:
    executable = _node(tmp_path)
    assert probe_node_runtime(
        executable,
        run=lambda path: _runner(path, version=version),
        expected_system="Darwin",
        expected_machine="arm64",
    ).version == version


def test_node_policy_rejects_reported_executable_and_architecture_mismatch(
    tmp_path: Path,
) -> None:
    executable = _node(tmp_path)
    other = tmp_path / "other-node"
    other.write_bytes(b"other")
    other.chmod(0o500)
    with pytest.raises(ProductManifestError, match="identity changed"):
        probe_node_runtime(
            executable,
            run=lambda path: _runner(path, exec_path=other),
            expected_system="Darwin",
            expected_machine="arm64",
        )
    with pytest.raises(ProductManifestError, match="arm64"):
        probe_node_runtime(
            executable,
            run=_runner,
            expected_system="Darwin",
            expected_machine="riscv64",
        )
    with pytest.raises(ProductManifestError, match="architecture"):
        probe_node_runtime(
            executable,
            run=lambda path: _runner(path, architecture="x64"),
            expected_system="Darwin",
            expected_machine="arm64",
        )


def test_node_policy_rejects_non_darwin_before_execution(tmp_path: Path) -> None:
    executable = _node(tmp_path)
    calls = 0

    def runner(path: Path) -> tuple[int, str, str]:
        nonlocal calls
        calls += 1
        return _runner(path, platform="linux")

    with pytest.raises(ProductManifestError, match="Darwin only"):
        probe_node_runtime(
            executable,
            run=runner,
            expected_system="Linux",
            expected_machine="aarch64",
        )
    assert calls == 0


@pytest.mark.parametrize("mode", [0o4555, 0o2555, 0o1555])
def test_node_policy_rejects_privileged_executable_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    executable = _node(tmp_path)
    executable.chmod(mode)
    with pytest.raises(ProductManifestError, match="not executable"):
        probe_node_runtime(
            executable,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
        )
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    with pytest.raises(ProductManifestError, match="identity is unsafe"):
        stage_node_runtime(
            executable,
            generation / "node",
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )
    assert list(generation.iterdir()) == []


def test_node_staging_rejects_unreasonable_source_sizes(tmp_path: Path) -> None:
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for name, size in (("small", 1024), ("large", NODE_MAX_EXECUTABLE_BYTES + 1)):
        source = tmp_path / name
        with source.open("wb") as handle:
            handle.truncate(size)
        source.chmod(0o500)
        with pytest.raises(ProductManifestError, match="identity is unsafe"):
            stage_node_runtime(
                source,
                generation / f"node-{name}",
                stage_root=generation,
                run=_runner,
                expected_system="Darwin",
                expected_machine="arm64",
                inspect_linkage=_system_linkage,
            )
    assert list(generation.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL contract")
def test_node_policy_accepts_the_default_home_deny_acl(tmp_path: Path) -> None:
    # macOS gives every home directory a `group:everyone deny delete` entry, so a
    # rule that refused any extended ACL made the product unstageable from
    # anywhere under `~` — the real failure that blocked the Mac mini install. A
    # deny entry can only take permissions away, so it is accepted.
    executable = _node(tmp_path)
    subprocess.run(
        ["/bin/chmod", "+a", "group:everyone deny delete", str(tmp_path)],
        check=True,
    )
    try:
        runtime = probe_node_runtime(
            executable,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
        )
    finally:
        subprocess.run(["/bin/chmod", "-N", str(tmp_path)], check=True)

    assert runtime.version == "v26.0.0"


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL contract")
def test_node_policy_rejects_a_permissive_parent_acl(tmp_path: Path) -> None:
    # The boundary that matters: an entry granting someone else write access to a
    # parent directory still fails closed.
    user = getpass.getuser()
    executable = _node(tmp_path)
    subprocess.run(
        ["/bin/chmod", "+a", f"user:{user} allow add_file", str(tmp_path)],
        check=True,
    )
    try:
        with pytest.raises(ProductManifestError, match="permissive extended ACL"):
            probe_node_runtime(
                executable,
                run=_runner,
                expected_system="Darwin",
                expected_machine="arm64",
            )
    finally:
        subprocess.run(["/bin/chmod", "-N", str(tmp_path)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL contract")
def test_node_policy_rejects_extended_file_and_stage_directory_acls(
    tmp_path: Path,
) -> None:
    user = getpass.getuser()
    executable = _node(tmp_path)
    subprocess.run(
        ["/bin/chmod", "+a", f"user:{user} allow write", str(executable)],
        check=True,
    )
    try:
        with pytest.raises(ProductManifestError, match="extended ACL"):
            probe_node_runtime(
                executable,
                run=_runner,
                expected_system="Darwin",
                expected_machine="arm64",
            )
    finally:
        subprocess.run(["/bin/chmod", "-N", str(executable)], check=True)

    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    subprocess.run(
        ["/bin/chmod", "+a", f"user:{user} allow add_file", str(generation)],
        check=True,
    )
    try:
        with pytest.raises(ProductManifestError, match="extended ACL"):
            stage_node_runtime(
                executable,
                generation / "node",
                stage_root=generation,
                run=_runner,
                expected_system="Darwin",
                expected_machine="arm64",
                inspect_linkage=_system_linkage,
            )
    finally:
        subprocess.run(["/bin/chmod", "-N", str(generation)], check=True)


def test_node_policy_detects_path_replacement_during_inspection(tmp_path: Path) -> None:
    executable = _node(tmp_path)

    def replace(path: Path) -> tuple[int, str, str]:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"replacement node executable")
        replacement.chmod(0o500)
        replacement.replace(path)
        return _runner(path)

    with pytest.raises(ProductManifestError, match="changed during inspection"):
        probe_node_runtime(
            executable,
            run=replace,
            expected_system="Darwin",
            expected_machine="arm64",
        )


def test_node_policy_resolves_one_symlink_and_rejects_hardlinks(tmp_path: Path) -> None:
    executable = _node(tmp_path)
    alias = tmp_path / "node-alias"
    alias.symlink_to(executable)
    runtime = probe_node_runtime(
        alias,
        run=_runner,
        expected_system="Darwin",
        expected_machine="arm64",
    )
    assert runtime.executable_sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()

    hardlink = tmp_path / "node-hardlink"
    hardlink.hardlink_to(executable)
    with pytest.raises(ProductManifestError, match="single regular file"):
        probe_node_runtime(
            executable,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
        )


def test_node_policy_allows_root_owned_owner_writable_executable_for_nonroot() -> None:
    details = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
    if os.geteuid() == 0:
        assert _mode_writable_by_effective_user(details) is True
    else:
        assert _mode_writable_by_effective_user(details) is False

    assert _mode_writable_by_effective_user(
        SimpleNamespace(st_mode=stat.S_IFREG | 0o775, st_uid=0)
    ) is True
    assert _mode_writable_by_effective_user(
        SimpleNamespace(st_mode=stat.S_IFREG | 0o777, st_uid=0)
    ) is True


def test_node_policy_supports_a_private_directory_below_sticky_tmp() -> None:
    directory = Path(tempfile.mkdtemp(prefix="cortex-node-policy-", dir="/tmp"))
    try:
        directory.chmod(0o700)
        executable = _node(directory)
        runtime = probe_node_runtime(
            executable,
            run=_runner,
            expected_system="Darwin",
            expected_machine="aarch64",
        )
        assert runtime.platform == "darwin"
    finally:
        executable.unlink(missing_ok=True)
        directory.rmdir()


def test_node_policy_rejects_a_world_writable_descendant(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    executable = _node(unsafe)
    try:
        with pytest.raises(ProductManifestError, match="parent chain"):
            probe_node_runtime(
                executable,
                run=_runner,
                expected_system="Darwin",
                expected_machine="arm64",
            )
    finally:
        executable.unlink()
        unsafe.chmod(0o700)


def test_node_policy_rejects_an_owner_group_writable_descendant(tmp_path: Path) -> None:
    unsafe = tmp_path / "group-writable"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o770)
    executable = _node(unsafe)
    try:
        with pytest.raises(ProductManifestError, match="parent chain"):
            probe_node_runtime(
                executable,
                run=_runner,
                expected_system="Darwin",
                expected_machine="arm64",
            )
    finally:
        executable.unlink()
        unsafe.chmod(0o700)


def test_node_staging_rejects_symlinked_generation_and_cross_root_destination(
    tmp_path: Path,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    alias = tmp_path / "generation-alias"
    alias.symlink_to(generation, target_is_directory=True)
    with pytest.raises(ProductManifestError, match="symlink ancestor"):
        stage_node_runtime(
            source,
            alias / "node",
            stage_root=alias,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with pytest.raises(ProductManifestError, match="outside its generation"):
        stage_node_runtime(
            source,
            outside / "node",
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )
    assert list(generation.iterdir()) == []
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "destination_factory",
    [
        lambda generation: generation / "node\0invalid",
        lambda _generation: Path("~cortex-user-that-must-not-exist/node"),
    ],
)
def test_node_staging_destination_errors_do_not_leak_stage_root_descriptor(
    tmp_path: Path,
    destination_factory,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    before = len(tuple(Path("/dev/fd").iterdir()))

    with pytest.raises(ProductManifestError, match="destination is invalid"):
        stage_node_runtime(
            source,
            destination_factory(generation),
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )

    assert len(tuple(Path("/dev/fd").iterdir())) == before
    assert list(generation.iterdir()) == []


def test_node_staging_never_clobbers_a_destination_that_appears(
    tmp_path: Path,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    sentinel = b"concurrent owner sentinel"
    sentinel_inode: int | None = None

    def appear(path: Path) -> None:
        nonlocal sentinel_inode
        path.write_bytes(sentinel)
        path.chmod(0o500)
        sentinel_inode = path.stat().st_ino

    with pytest.raises(ProductManifestError, match="already exists"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            before_publish=appear,
            inspect_linkage=_system_linkage,
        )
    assert destination.read_bytes() == sentinel
    assert destination.stat().st_ino == sentinel_inode
    assert list(generation.iterdir()) == [destination]


def test_node_staging_rejects_temporary_inode_tamper_before_publish(
    tmp_path: Path,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"

    def tamper(_path: Path) -> None:
        temporary = next(generation.glob(".node.*"))
        temporary.chmod(0o700)
        temporary.write_bytes(b"x" * source.stat().st_size)
        temporary.chmod(0o500)

    with pytest.raises(ProductManifestError, match="staged runtime identity changed"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            before_publish=tamper,
            inspect_linkage=_system_linkage,
        )
    assert list(generation.iterdir()) == []


def test_node_staging_rejects_external_hardlink_before_publish(
    tmp_path: Path,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    external_alias = tmp_path / "external-node-alias"

    def hardlink(_path: Path) -> None:
        external_alias.hardlink_to(next(generation.glob(".node.*")))

    with pytest.raises(ProductManifestError, match="staged runtime identity changed"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            before_publish=hardlink,
            inspect_linkage=_system_linkage,
        )
    assert list(generation.iterdir()) == []
    assert external_alias.stat().st_nlink == 1


def test_node_staging_removes_published_runtime_when_hardlink_appears_during_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    external_alias = tmp_path / "external-node-alias"
    real_link = os.link

    def race_publish(
        source_name,
        destination_name,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
        follow_symlinks=True,
    ) -> None:
        real_link(
            generation / os.fspath(source_name),
            external_alias,
            follow_symlinks=False,
        )
        real_link(
            source_name,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", race_publish)
    with pytest.raises(ProductManifestError, match="staged runtime identity changed"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )

    assert not destination.exists()
    assert list(generation.iterdir()) == []
    assert external_alias.stat().st_nlink == 1


def test_node_cleanup_never_unlinks_a_replacement_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    sentinel = (b"replacement sentinel\n" * 2048)[:48 * 1024]
    replacement_inode: int | None = None
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_during_post_publish_cleanup(descriptor: int) -> None:
        nonlocal fsync_calls, replacement_inode
        fsync_calls += 1
        if fsync_calls == 2:
            destination.unlink()
            destination.write_bytes(sentinel)
            destination.chmod(0o500)
            replacement_inode = destination.stat().st_ino
            raise OSError("injected cleanup fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_during_post_publish_cleanup)
    with pytest.raises(OSError, match="injected cleanup fsync failure"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )
    assert destination.read_bytes() == sentinel
    assert destination.stat().st_ino == replacement_inode
    assert list(generation.iterdir()) == [destination]


@pytest.mark.parametrize(
    "value",
    [
        "/usr/lib/../private/libbad.dylib",
        "/usr/lib//libbad.dylib",
        "/usr/lib/./libbad.dylib",
        "/System/Library/../private/libbad.dylib",
        "@rpath/libbad.dylib",
        "relative/libbad.dylib",
    ],
)
def test_system_library_paths_must_be_absolute_and_lexically_canonical(
    value: str,
) -> None:
    assert _is_system_library_path(value) is False


@pytest.mark.parametrize(
    "dependency",
    [
        (
            "/usr/lib/allowed (marker)/../../private/evil.dylib "
            "(compatibility version 1.0.0, current version 1.0.0)"
        ),
        "/usr/lib/libSystem.B.dylib",
        (
            "/usr/lib/libSystem.B.dylib "
            "(compatibility version 1.0.0, current version 1.0.0) "
            "(compatibility version 1.0.0, current version 1.0.0)"
        ),
    ],
)
def test_darwin_linkage_parser_rejects_truncated_or_ambiguous_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
) -> None:
    executable = tmp_path / "node"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{executable}:\n\t{dependency}\n",
        ),
    )

    with pytest.raises(ProductManifestError, match="invalid|unsupported"):
        _darwin_system_linkage(executable)


def test_node_staging_uses_the_open_source_when_path_is_replaced(tmp_path: Path) -> None:
    source = _node(tmp_path)
    original = source.read_bytes()
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"

    def replace(path: Path) -> None:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"different replacement executable")
        replacement.chmod(0o500)
        replacement.replace(path)

    runtime = stage_node_runtime(
        source,
        destination,
        stage_root=generation,
        run=_runner,
        expected_system="Darwin",
        expected_machine="arm64",
        after_source_open=replace,
        inspect_linkage=_system_linkage,
    )

    assert destination.read_bytes() == original
    assert runtime.executable_sha256 == hashlib.sha256(original).hexdigest()
    assert destination.stat().st_mode & 0o777 == 0o500


def test_node_staging_rejects_source_tamper_and_cleans_partial_output(
    tmp_path: Path,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"

    def tamper(path: Path) -> None:
        path.chmod(0o700)
        path.write_bytes(b"tampered source executable with another size")
        path.chmod(0o500)

    with pytest.raises(ProductManifestError, match="changed during staging"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            after_source_open=tamper,
            inspect_linkage=_system_linkage,
        )
    assert not destination.exists()
    assert list(generation.iterdir()) == []


def test_installed_node_verification_rejects_copy_tamper(tmp_path: Path) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    runtime = stage_node_runtime(
        source,
        destination,
        stage_root=generation,
        run=_runner,
        expected_system="Darwin",
        expected_machine="arm64",
        inspect_linkage=_system_linkage,
    )
    verify_staged_node_runtime(
        destination,
        runtime,
        stage_root=generation,
        run=_runner,
        expected_system="Darwin",
        expected_machine="arm64",
    )

    destination.chmod(0o700)
    destination.write_bytes(b"tampered installed executable")
    destination.chmod(0o500)
    with pytest.raises(ProductManifestError, match="size|does not match"):
        verify_staged_node_runtime(
            destination,
            runtime,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
        )


def test_installed_node_verification_rejects_external_symlink(
    tmp_path: Path,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    runtime = stage_node_runtime(
        source,
        destination,
        stage_root=generation,
        run=_runner,
        expected_system="Darwin",
        expected_machine="arm64",
        inspect_linkage=_system_linkage,
    )
    external = tmp_path / "external-node"
    external.write_bytes(destination.read_bytes())
    external.chmod(0o500)
    destination.unlink()
    destination.symlink_to(external)

    with pytest.raises(ProductManifestError, match="installed Node runtime is unsafe"):
        verify_staged_node_runtime(
            destination,
            runtime,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
        )


def test_node_staging_cleans_copy_when_execution_probe_fails(tmp_path: Path) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    with pytest.raises(ProductManifestError, match="inspection failed"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=lambda _path: (1, "", "sentinel-secret-must-not-escape"),
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )
    assert not destination.exists()
    assert list(generation.iterdir()) == []


def test_partial_node_copy_failure_leaves_zero_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _node(tmp_path)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    real_write = os.write
    calls = 0

    def fail_after_partial_write(descriptor: int, value: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, value[: max(1, len(value) // 2)])
        raise OSError("injected partial write failure")

    monkeypatch.setattr(os, "write", fail_after_partial_write)
    with pytest.raises(OSError, match="injected partial write failure"):
        stage_node_runtime(
            source,
            destination,
            stage_root=generation,
            run=_runner,
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=_system_linkage,
        )
    assert not destination.exists()
    assert list(generation.iterdir()) == []


@pytest.mark.skipif(
    not Path("/opt/homebrew/bin/node").exists(),
    reason="Homebrew Node is a local macOS R0 rejection fixture",
)
def test_current_homebrew_node_is_rejected_without_residue(tmp_path: Path) -> None:
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    with pytest.raises(ProductManifestError, match="unsupported_dynamic_closure"):
        stage_node_runtime(
            Path("/opt/homebrew/bin/node"),
            destination,
            stage_root=generation,
            expected_system="Darwin",
            expected_machine="arm64",
        )
    assert not destination.exists()
    assert list(generation.iterdir()) == []


@pytest.mark.skipif(
    not Path("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node").exists(),
    reason="A system-library-only Node was not provided for local acceptance",
)
def test_explicit_system_library_only_node_executes_from_private_stage(
    tmp_path: Path,
) -> None:
    source = Path("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node")
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    destination = generation / "node"
    runtime = stage_node_runtime(
        source,
        destination,
        stage_root=generation,
        expected_system="Darwin",
        expected_machine="arm64",
    )
    manifest = build_product_manifest(runtime)
    serialized = json.dumps(manifest, sort_keys=True)

    assert validate_product_manifest(manifest) == manifest
    assert runtime.executable_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert "/Applications/" not in serialized
    assert str(tmp_path) not in serialized
    assert "sentinel-secret-must-not-escape" not in serialized


def test_product_manifest_is_closed_and_keeps_effects_disabled(tmp_path: Path) -> None:
    runtime = NodeRuntime("v26.0.0", "darwin", "arm64", "a" * 64)
    manifest = build_product_manifest(runtime)

    assert manifest["processes"]["private_access"]["enabled_by_default"] is False
    assert manifest["capabilities"]["runtime_dispatch"] == "disabled"
    assert manifest["capabilities"]["telegram"] == "disabled"
    assert manifest["capabilities"]["providers"] == "disabled"
    assert manifest["capabilities"]["ingestion"] == "disabled"
    assert "control-db" in manifest["state_ownership"]["user_owned_preserved"]
    assert "service-definitions" in manifest["state_ownership"]["installer_owned"]

    tampered = json.loads(json.dumps(manifest))
    tampered["capabilities"]["runtime_dispatch"] = "available"
    with pytest.raises(ProductManifestError, match="contract"):
        validate_product_manifest(tampered)

    # Schema 1 is the legacy host-Python generation whose manifest bytes are
    # frozen forever, and schema 2 is frozen the same way now that generations
    # have been installed under it. The A1-8 row belongs to schema 3 and cannot
    # be retrofitted onto either.
    assert "telegram_adapter" not in manifest["capabilities"]
    tampered = json.loads(json.dumps(manifest))
    tampered["capabilities"]["telegram_adapter"] = "disabled"
    with pytest.raises(ProductManifestError, match="contract"):
        validate_product_manifest(tampered)

    tampered = json.loads(json.dumps(manifest))
    tampered["unexpected"] = True
    with pytest.raises(ProductManifestError, match="schema"):
        validate_product_manifest(tampered)


def test_composed_manifest_pins_the_telegram_adapter_row(tmp_path: Path) -> None:
    """A1-8: the adapter row gets `runtime_dispatch`'s tamper discipline.

    `telegram_adapter` was a health literal with no persistent state behind
    it, so the step that eventually turns it true could have been an edit to
    one line. The manifest half of that fence is this row: an installed
    generation cannot advertise an adapter the composition does not build,
    and it cannot silently drop the row either.

    It rides on the composed manifest, not the legacy one. Schema 1's bytes
    are a published generation's identity input and are frozen permanently,
    and a host-Python generation is not the one that will ever carry the
    worker-side transport.
    """

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "a" * 64)
    python = PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
        venv_interpreter_sha256="2" * 64,
    )
    manifest = build_product_manifest(node, python_runtime=python)

    assert manifest["schema_version"] == 3
    assert manifest["capabilities"]["telegram_adapter"] == "disabled"
    assert validate_product_manifest(manifest) == manifest

    for claim in ("available", "available-disabled", "enabled", True):
        tampered = json.loads(json.dumps(manifest))
        tampered["capabilities"]["telegram_adapter"] = claim
        with pytest.raises(ProductManifestError, match="contract"):
            validate_product_manifest(tampered)

    tampered = json.loads(json.dumps(manifest))
    del tampered["capabilities"]["telegram_adapter"]
    with pytest.raises(ProductManifestError, match="contract"):
        validate_product_manifest(tampered)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), True),
        (("processes", "control", "required"), 1),
        (("listeners", "web-loopback", "browser_visible"), 1),
        (("node_runtime", "policy", "adapter_version"), True),
    ],
)
def test_product_manifest_rejects_bool_integer_type_confusion(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    manifest = build_product_manifest(
        NodeRuntime("v26.0.0", "darwin", "arm64", "a" * 64)
    )
    cursor: dict[str, object] = manifest
    for component in path[:-1]:
        child = cursor[component]
        assert isinstance(child, dict)
        cursor = child
    cursor[path[-1]] = replacement
    with pytest.raises(ProductManifestError, match="version|contract"):
        validate_product_manifest(manifest)


def _advance_product_contract(
    later: pytest.MonkeyPatch,
    manifest_module: object,
) -> int:
    """Simulate the NEXT generation's `product_manifest.py`.

    A later release grows the composed contract. It must do that by adding a
    schema version and leaving the frozen entry an already-installed generation
    was composed against alone.

    The row is a placeholder name rather than a real one: `telegram_adapter` is
    now the real schema-3 row, and simulating growth with a field the current
    schema already carries would test nothing.
    """

    current = manifest_module.PRODUCT_MANIFEST_SCHEMA
    later_schema = current + 1
    frozen = manifest_module._PRODUCT_CONTRACT_BY_SCHEMA
    grown = copy.deepcopy(frozen[current])
    grown["schema_version"] = later_schema
    capabilities = grown["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["placeholder_future_capability"] = "disabled"
    later.setattr(
        manifest_module,
        "_PRODUCT_CONTRACT_BY_SCHEMA",
        {**frozen, later_schema: grown},
    )
    fields = manifest_module._PRODUCT_FIELDS_BY_SCHEMA
    later.setattr(
        manifest_module,
        "_PRODUCT_FIELDS_BY_SCHEMA",
        {**fields, later_schema: fields[current]},
    )
    later.setattr(manifest_module, "PRODUCT_MANIFEST_SCHEMA", later_schema)
    return later_schema


def test_a_grown_product_contract_still_validates_a_previous_generation() -> None:
    """Cross-generation: a moving contract reference breaks installed generations.

    `validate_product_manifest` is run by whatever `cortex-dist` executes NEXT,
    against a `product-manifest.json` an EARLIER generation composed. Building
    the reference from literals that move would make every installed generation
    fail validation the moment the contract grew — `doctor` reporting
    `installation_unhealthy` and `upgrade` refusing to run, exactly as the
    bundled-tools pin did. The contract is frozen per schema version instead.
    """

    from distribution import product_manifest as manifest_module

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    python = PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
        venv_interpreter_sha256="2" * 64,
    )
    installed = build_product_manifest(node, python)
    composed_schema = installed["schema_version"]

    with pytest.MonkeyPatch.context() as later:
        later_schema = _advance_product_contract(later, manifest_module)
        grown = build_product_manifest(node, python)

        assert grown["schema_version"] == later_schema
        assert grown["capabilities"]["placeholder_future_capability"] == "disabled"
        # The previously installed generation still validates, byte for byte.
        assert validate_product_manifest(installed) == installed
        assert installed["schema_version"] == composed_schema
        assert "placeholder_future_capability" not in installed["capabilities"]


def test_a_grown_product_contract_is_not_retrofitted_onto_an_older_schema() -> None:
    """The freeze is a refusal too: an older schema may not carry newer fields."""

    from distribution import product_manifest as manifest_module

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    python = PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
        venv_interpreter_sha256="2" * 64,
    )
    forged = build_product_manifest(node, python)
    assert isinstance(forged["capabilities"], dict)
    forged["capabilities"]["telegram_adapter"] = "available"

    with pytest.MonkeyPatch.context() as later:
        _advance_product_contract(later, manifest_module)
        with pytest.raises(ProductManifestError, match="contract"):
            validate_product_manifest(forged)
