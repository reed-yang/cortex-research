"""Runtime profiles for the vendoring tool (S3.2 design D-S3.2-1, AMD-1).

The tool used to be single-version by construction. Its version-shaped policy is
now profile data, so a second CPython release can be vendored with the same
attestation and pin discipline. The cp314 profile is the default and must keep
producing exactly what it produced before the extraction: the vendored archive,
its committed pin, and every distribution test that consumes them depend on it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tools import vendor_python_runtime as vendor

REPOSITORY = Path(__file__).resolve().parents[2]

# The exact tables the tool carried before the profile extraction. FROZEN
# LITERALS, not values to regenerate: they decide what an operator vendors, and
# the committed pin was produced by them. If one of these fails, the question is
# whether the policy change was intended, not whether the constant is stale.
CP314_PRUNE_POLICY = (
    "lib/python3.14/idlelib/",
    "lib/python3.14/tkinter/",
    "lib/python3.14/lib-dynload/_tkinter*.so",
    "lib/python3.14/lib-dynload/_dbm*.so",
    "lib/libtcl9*.dylib",
    "lib/libtcl9tk9.0.dylib",
    "lib/tcl9*/",
    "lib/tk9*/",
    "lib/itcl*/",
    "lib/thread*/",
    "**/__pycache__/",
    "**/*.a",
)
CP314_PATCH_SET = (
    "lib/python3.14/_sysconfigdata_*.py",
    "lib/python3.14/_sysconfig_vars_*.json",
    "lib/pkgconfig/*.pc",
    "lib/python3.14/config-3.14-darwin/Makefile",
)
CP314_RESIDUAL_BUILD_PATH_ALLOWLIST = frozenset(
    {
        "lib/python3.14/config-3.14-darwin/python.o",
        "lib/python3.14/site-packages/pip-26.1.2.dist-info/direct_url.json",
    }
)
CP314_REQUIRED_FILES = (
    "bin/python3.14",
    "lib/libpython3.14.dylib",
    "lib/python3.14/LICENSE.txt",
    "lib/python3.14/ensurepip/__init__.py",
    "lib/python3.14/os.py",
    "lib/python3.14/venv/__init__.py",
    "share/licenses/python-build-standalone/LICENSE.openssl-3.txt",
    "share/licenses/python-build-standalone/python-licenses.rst",
)
CP314_REQUIRED_DIRECTORIES = (
    "include",
    "lib/pkgconfig",
    "lib/python3.14/config-3.14-darwin",
    "lib/python3.14/site-packages/pip",
)
CP314_FORBIDDEN_PATHS = (
    "lib/libtcl9.0.dylib",
    "lib/python3.14/idlelib",
    "lib/python3.14/tkinter",
)

# The canonical bytes of the pin document the cp314 profile builds, measured
# from the tool as it stood before the extraction. Also a frozen literal.
CP314_PIN_SHA256 = "7f40e04395f68e2a668093dd2bbc103811235a8b754f059fb4805b6e76f45d41"


def test_the_default_profile_is_cp314() -> None:
    assert vendor.DEFAULT_PROFILE is vendor.CP314_PROFILE
    assert vendor.PROFILES["cp314"] is vendor.CP314_PROFILE
    assert vendor.DEFAULT_PROFILE.name == "cp314"


def test_the_cp314_profile_reproduces_the_frozen_names() -> None:
    profile = vendor.CP314_PROFILE

    assert profile.release == "3.14"
    assert profile.version == "3.14.6"
    assert profile.abi_tag == "cp314"
    assert profile.platform_tag == "macosx_11_0_arm64"
    assert profile.interpreter_relative == "bin/python3.14"
    assert profile.library_relative == "lib/libpython3.14.dylib"
    assert profile.library_id == "@rpath/libpython3.14.dylib"
    assert profile.archive_name == "cpython-3.14.6-cp314-macosx_11_0_arm64.tar.gz"
    assert profile.pin_name == "cpython-3.14.6-cp314-macosx_11_0_arm64.pin.json"


def test_the_cp314_profile_reproduces_the_frozen_upstream_pin() -> None:
    upstream = vendor.CP314_PROFILE.upstream

    assert upstream is not None
    assert upstream.release_tag == "20260728"
    assert upstream.name == (
        "cpython-3.14.6+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz"
    )
    assert upstream.sha256 == (
        "f4b47659e2da4b97f38cefdf5ad19f0042946099d843cde60de308708e5b1ac5"
    )
    assert upstream.size == 26022203


def test_the_cp314_profile_reproduces_the_frozen_policy_tables() -> None:
    profile = vendor.CP314_PROFILE

    assert profile.prune_policy == CP314_PRUNE_POLICY
    assert profile.patch_set == CP314_PATCH_SET
    assert profile.residual_build_path_allowlist == CP314_RESIDUAL_BUILD_PATH_ALLOWLIST
    assert profile.required_files == CP314_REQUIRED_FILES
    assert profile.required_directories == CP314_REQUIRED_DIRECTORIES
    assert profile.forbidden_paths == CP314_FORBIDDEN_PATHS


def test_the_module_level_constants_are_the_default_profile() -> None:
    """The tool's existing surface keeps naming the default profile's values."""

    profile = vendor.DEFAULT_PROFILE

    assert vendor.PYTHON_VERSION == profile.version
    assert vendor.PYTHON_RELEASE == profile.release
    assert vendor.ABI_TAG == profile.abi_tag
    assert vendor.PLATFORM_TAG == profile.platform_tag
    assert vendor.INTERPRETER_RELATIVE == profile.interpreter_relative
    assert vendor.ARCHIVE_NAME == profile.archive_name
    assert vendor.PIN_NAME == profile.pin_name
    assert vendor.PRUNE_POLICY == profile.prune_policy
    assert vendor.PATCH_SET == profile.patch_set
    assert vendor.REQUIRED_FILES == profile.required_files
    assert vendor.REQUIRED_DIRECTORIES == profile.required_directories
    assert vendor.FORBIDDEN_PATHS == profile.forbidden_paths
    assert vendor.LIBPYTHON_RELATIVE == profile.library_relative
    assert vendor.LIBPYTHON_ID == profile.library_id
    assert vendor.UPSTREAM_RELEASE_TAG == profile.upstream.release_tag
    assert vendor.UPSTREAM_ASSET == profile.upstream.name
    assert vendor.UPSTREAM_ASSET_SHA256 == profile.upstream.sha256
    assert vendor.UPSTREAM_ASSET_SIZE == profile.upstream.size


def test_the_cp314_pin_document_is_byte_identical_to_the_pre_extraction_tool() -> None:
    described = {
        "name": vendor.CP314_PROFILE.archive_name,
        "size": 26022203,
        "sha256": "a" * 64,
    }

    pin = vendor.build_pin(archive=described, attestation_sha256="e" * 64)

    assert pin == vendor.build_pin(
        archive=described,
        attestation_sha256="e" * 64,
        profile=vendor.CP314_PROFILE,
    )
    raw = json.dumps(pin, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == CP314_PIN_SHA256


def test_the_committed_pin_was_produced_by_the_cp314_profile() -> None:
    """The profile still names the artifact the repository actually carries."""

    committed = json.loads(
        (
            REPOSITORY / "vendor/python-runtime" / vendor.CP314_PROFILE.pin_name
        ).read_text(encoding="utf-8")
    )

    assert committed["version"] == vendor.CP314_PROFILE.version
    assert committed["abi_tag"] == vendor.CP314_PROFILE.abi_tag
    assert committed["platform_tag"] == vendor.CP314_PROFILE.platform_tag
    assert committed["interpreter_path"] == vendor.CP314_PROFILE.interpreter_relative
    assert committed["archive"]["name"] == vendor.CP314_PROFILE.archive_name
    assert committed["normalization"]["pruned"] == list(vendor.CP314_PROFILE.prune_policy)


def test_the_cp311_profile_names_the_3_11_runtime() -> None:
    profile = vendor.PROFILES["cp311"]

    assert profile.name == "cp311"
    assert profile.release == "3.11"
    assert profile.abi_tag == "cp311"
    assert profile.platform_tag == "macosx_11_0_arm64"
    assert profile.interpreter_relative == "bin/python3.11"
    assert profile.library_relative == "lib/libpython3.11.dylib"
    assert profile.library_id == "@rpath/libpython3.11.dylib"


def test_the_cp311_profile_carries_no_3_14_path_anywhere() -> None:
    profile = vendor.PROFILES["cp311"]
    tables = (
        *profile.prune_policy,
        *profile.patch_set,
        *sorted(profile.residual_build_path_allowlist),
        *profile.required_files,
        *profile.required_directories,
        *profile.forbidden_paths,
        profile.interpreter_relative,
        profile.library_relative,
        profile.library_id,
    )

    assert not any("3.14" in value or "314" in value for value in tables)
    assert "lib/python3.11/idlelib/" in profile.prune_policy
    assert "lib/python3.11/config-3.11-darwin/Makefile" in profile.patch_set
    assert "bin/python3.11" in profile.required_files
    assert "lib/python3.11/config-3.11-darwin" in profile.required_directories
    assert "lib/python3.11/tkinter" in profile.forbidden_paths


def test_the_cp311_profile_reproduces_the_measured_upstream_pin() -> None:
    """Measured on the 2026-09-01 acquisition run, from the release SHA256SUMS.

    Same release tag as cp314, same `install_only_stripped` aarch64 variant, so
    the two runtimes come from one upstream build and one attestation policy.
    """

    upstream = vendor.CP311_PROFILE.upstream

    assert upstream is not None
    assert upstream.release_tag == vendor.CP314_PROFILE.pinned_upstream().release_tag
    assert upstream.name == (
        "cpython-3.11.15+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz"
    )
    assert upstream.sha256 == (
        "3f1839e06c8a0800ac31b35d1a633323dda11d34ad8f2ed9d70cc51c56793028"
    )
    assert upstream.size == 27113247
    assert upstream.version == "3.11.15"
    assert vendor.CP311_PROFILE.archive_name == (
        "cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz"
    )


def test_the_cp311_residual_allowlist_is_what_the_vendor_run_measured() -> None:
    """Two holders, both `direct_url.json`, both refused until measured.

    The vendor run aborts on any unlisted build-host path, so this set is a
    transcript of the acquisition, not a policy choice.
    """

    assert vendor.CP311_PROFILE.residual_build_path_allowlist == frozenset(
        {
            "lib/python3.11/config-3.11-darwin/python.o",
            "lib/python3.11/site-packages/pip-26.1.2.dist-info/direct_url.json",
            "lib/python3.11/site-packages/setuptools-82.0.1.dist-info/direct_url.json",
        }
    )


def _write(path: Path, contents: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    return path


def _cp311_tree(root: Path) -> Path:
    """The minimum a 3.11 runtime must carry to satisfy its own profile."""

    tree = root / "python"
    _write(tree / "bin/python3.11", b"\xcf\xfa\xed\xfe" + b"interpreter\n")
    _write(tree / "lib/libpython3.11.dylib", b"\xcf\xfa\xed\xfe" + b"libpython\n")
    _write(tree / "lib/python3.11/LICENSE.txt", "PSF\n")
    _write(tree / "lib/python3.11/os.py", "sep = '/'\n")
    _write(tree / "lib/python3.11/ensurepip/__init__.py", "")
    _write(tree / "lib/python3.11/venv/__init__.py", "")
    _write(tree / "lib/python3.11/config-3.11-darwin/Makefile", "prefix=\t\t/install\n")
    _write(tree / "lib/python3.11/site-packages/pip/__init__.py", "")
    _write(tree / "lib/pkgconfig/python-3.11.pc", "prefix=/install\n")
    _write(tree / "include/python3.11/Python.h", "/* header */\n")
    for name in vendor.LICENCE_DOCUMENTS:
        _write(tree / vendor.LICENCE_RELATIVE / name, f"{name} text\n")
    # Removed by the profile's own prune policy.
    _write(tree / "lib/python3.11/idlelib/idle.py", "")
    _write(tree / "lib/python3.11/tkinter/__init__.py", "")
    return tree


def test_the_profile_threads_through_the_whole_normalization(tmp_path: Path) -> None:
    tree = _cp311_tree(tmp_path)
    profile = vendor.PROFILES["cp311"]

    removed = vendor.prune_tree(tree, profile=profile)
    patched = vendor.patch_build_host_paths(tree, profile=profile)
    vendor.assert_normalized_content(tree, profile=profile)

    assert "lib/python3.11/idlelib" in removed
    assert set(patched) == {
        "lib/pkgconfig/python-3.11.pc",
        "lib/python3.11/config-3.11-darwin/Makefile",
    }
    assert not (tree / "lib/python3.11/tkinter").exists()


def test_the_default_profile_refuses_a_3_11_tree(tmp_path: Path) -> None:
    """Proof the profile is consulted rather than carried: cp314 rejects it."""

    tree = _cp311_tree(tmp_path)

    with pytest.raises(vendor.VendorError, match="content is missing bin/python3.14"):
        vendor.assert_normalized_content(tree)


def test_a_3_11_dylib_id_is_checked_against_the_3_11_library(tmp_path: Path) -> None:
    tree = _cp311_tree(tmp_path)
    profile = vendor.PROFILES["cp311"]
    calls: list[tuple[str, ...]] = []

    def run(*command: str) -> str:
        calls.append(command)
        return "@rpath/libpython3.11.dylib"

    vendor.normalize_dylib_id(tree, run=run, profile=profile)

    assert calls == [("/usr/bin/otool", "-D", str(tree / "lib/libpython3.11.dylib"))]


def test_vendoring_refuses_a_profile_with_no_pinned_asset(tmp_path: Path) -> None:
    """Both shipped profiles are pinned now, so the refusal is proved on a copy."""

    unpinned = replace(vendor.CP311_PROFILE, upstream=None)

    def download(_url: str) -> bytes:  # pragma: no cover - must never be reached
        raise AssertionError("an unpinned profile must not reach the network")

    with pytest.raises(vendor.VendorError, match="no pinned upstream asset"):
        _ = unpinned.version
    with pytest.raises(vendor.VendorError, match="no pinned upstream asset"):
        vendor.build_pin(
            archive={"name": "x", "size": 1, "sha256": "a" * 64},
            attestation_sha256="e" * 64,
            profile=unpinned,
        )
    with pytest.raises(vendor.VendorError, match="no pinned upstream asset"):
        vendor.vendor(
            vendor_root=tmp_path / "vendored",
            download=download,
            profile=unpinned,
        )

    assert not (tmp_path / "vendored").exists()
