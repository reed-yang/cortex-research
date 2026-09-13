"""The staging kernel's runtime profile, frozen and exercised on two releases.

Two questions are asked here. First, whether the cp314 profile still spells
exactly what `distribution/product_manifest.py` spelled literally before the
extraction — those tables decide what the product's own installer accepts, so
they are FROZEN LITERALS below rather than values re-derived from the profile.
Second, whether the parameter is real: a cp311 tree must stage under the cp311
profile and be refused under the default one, which is the whole point of
S3.2's per-slot interpreter.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from cortex_platform.runtime_staging import (
    CP311_PROFILE,
    CP314_PROFILE,
    DEFAULT_PROFILE,
    PROFILES,
    RuntimeStagingError,
    _darwin_embedded_linkage,
    stage_python_runtime,
)

MACH_O = b"\xcf\xfa\xed\xfe"
PREFIX_MARKER = "@CORTEX_PYTHON_PREFIX@"
# The archive must clear the policy's 8 MiB floor, so one incompressible member
# is built once and reused by every fixture in this module.
PADDING = os.urandom(9 * 1024 * 1024)

# The exact tables `distribution/product_manifest.py` carried at :28-39 and
# :148-171 before the kernel was extracted. If one of these fails the question
# is whether the policy change was intended, not whether the constant is stale.
CP314_PATCH_SET = (
    "lib/python3.14/_sysconfigdata_*.py",
    "lib/python3.14/_sysconfig_vars_*.json",
    "lib/pkgconfig/*.pc",
    "lib/python3.14/config-3.14-darwin/Makefile",
)
CP314_REQUIRED_FILES = (
    "bin/python3.14",
    "lib/python3.14/LICENSE.txt",
    "lib/python3.14/ensurepip/__init__.py",
    "lib/python3.14/os.py",
    "lib/python3.14/venv/__init__.py",
    "share/licenses/python-build-standalone/LICENSE.openssl-3.txt",
    "share/licenses/python-build-standalone/python-licenses.rst",
)
CP314_REQUIRED_DIRECTORIES = ("lib/python3.14/config-3.14-darwin",)
CP314_FORBIDDEN_PATHS = (
    "lib/libtcl9.0.dylib",
    "lib/python3.14/idlelib",
    "lib/python3.14/tkinter",
)


def _members(release: str) -> dict[str, bytes]:
    """One minimally complete relocatable CPython tree for `release`."""

    return {
        f"bin/python{release}": MACH_O + b"interpreter\n",
        f"lib/libpython{release}.dylib": MACH_O + b"libpython\n",
        f"lib/python{release}/LICENSE.txt": b"PSF\n",
        f"lib/python{release}/_sysconfigdata__darwin_darwin.py": (
            f'build_time_vars = {{"BINDIR": "{PREFIX_MARKER}/bin"}}\n'.encode()
        ),
        f"lib/python{release}/config-{release}-darwin/Makefile": (
            f"prefix={PREFIX_MARKER}\n".encode()
        ),
        f"lib/python{release}/ensurepip/__init__.py": b"",
        f"lib/python{release}/os.py": b"sep = '/'\n",
        f"lib/python{release}/venv/__init__.py": b"",
        "share/licenses/python-build-standalone/LICENSE.openssl-3.txt": b"OpenSSL\n",
        "share/licenses/python-build-standalone/python-licenses.rst": b"licences\n",
        "share/padding.bin": PADDING,
    }


def _archive(destination: Path, release: str) -> Path:
    payloads = _members(release)
    executable = f"bin/python{release}"
    with destination.open("wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
            for name in sorted(payloads):
                data = payloads[name]
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if name == executable else 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return destination


def _expected(archive: Path, *, version: str, abi_tag: str) -> dict[str, object]:
    payload = archive.read_bytes()
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "version": version,
        "abi_tag": abi_tag,
    }


def _runner(destination: Path, *, release: str, version: str):
    major, minor = release.split(".")

    def run(interpreter: Path) -> tuple[int, str, str]:
        # The probe runs against the staging path, whose root is two levels up
        # from `bin/python<release>`; the destination is where it is published.
        tree = interpreter.parent.parent
        return (
            0,
            json.dumps(
                {
                    "executable": str(interpreter),
                    "prefix": str(tree),
                    "base_prefix": str(tree),
                    "version": version,
                    "implementation": "CPython",
                    "platform": "darwin",
                    "machine": "arm64",
                    "bindir": f"{destination}/bin",
                    "libdir": f"{destination}/lib",
                    "includepy": f"{destination}/include/python{release}",
                    "ext_suffix": f".cpython-{major}{minor}-darwin.so",
                    "modules": sorted(
                        [
                            "bz2",
                            "ctypes",
                            "ensurepip",
                            "hashlib",
                            "lzma",
                            "sqlite3",
                            "ssl",
                            "venv",
                            "zlib",
                        ]
                    ),
                }
            ),
            "",
        )

    return run


def _stage_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "stage"
    root.mkdir(mode=0o700, exist_ok=True)
    return root, root / "python-runtime"


def test_the_cp314_profile_is_the_pre_extraction_literal_set() -> None:
    assert CP314_PROFILE.name == "cp314"
    assert CP314_PROFILE.supported_release == (3, 14)
    assert CP314_PROFILE.interpreter_relative == "bin/python3.14"
    assert CP314_PROFILE.library_relative == "lib/libpython3.14.dylib"
    assert CP314_PROFILE.library_id == "@rpath/libpython3.14.dylib"
    assert CP314_PROFILE.expected_rpaths == frozenset({"@executable_path/../lib"})
    assert CP314_PROFILE.extension_suffix == ".cpython-314-darwin.so"
    assert CP314_PROFILE.patch_set == CP314_PATCH_SET
    assert CP314_PROFILE.required_files == CP314_REQUIRED_FILES
    assert CP314_PROFILE.required_directories == CP314_REQUIRED_DIRECTORIES
    assert CP314_PROFILE.forbidden_paths == CP314_FORBIDDEN_PATHS


def test_the_product_profile_is_the_default() -> None:
    assert DEFAULT_PROFILE is CP314_PROFILE
    assert PROFILES == {"cp314": CP314_PROFILE, "cp311": CP311_PROFILE}


def test_the_cp311_profile_derives_its_whole_path_policy_from_its_release() -> None:
    assert CP311_PROFILE.supported_release == (3, 11)
    assert CP311_PROFILE.interpreter_relative == "bin/python3.11"
    assert CP311_PROFILE.library_id == "@rpath/libpython3.11.dylib"
    assert CP311_PROFILE.extension_suffix == ".cpython-311-darwin.so"
    assert "lib/python3.11/os.py" in CP311_PROFILE.required_files
    assert CP311_PROFILE.required_directories == ("lib/python3.11/config-3.11-darwin",)
    assert "lib/python3.11/tkinter" in CP311_PROFILE.forbidden_paths


def test_a_cp311_archive_stages_under_the_cp311_profile(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "runtime.tar.gz", "3.11")
    stage_root, destination = _stage_root(tmp_path)
    expected = _expected(archive, version="3.11.14", abi_tag="cp311")

    runtime = stage_python_runtime(
        archive,
        destination,
        stage_root=stage_root,
        expected=expected,
        run=_runner(destination, release="3.11", version="3.11.14"),
        expected_system="Darwin",
        expected_machine="arm64",
        inspect_linkage=lambda _tree: None,
        profile=CP311_PROFILE,
    )

    assert runtime.version == "3.11.14"
    assert runtime.archive_sha256 == expected["sha256"]
    interpreter = destination / "bin/python3.11"
    assert interpreter.is_file()
    assert runtime.interpreter_sha256 == hashlib.sha256(interpreter.read_bytes()).hexdigest()
    # The staged tree is bound to where it was published, not to the build host.
    makefile = destination / "lib/python3.11/config-3.11-darwin/Makefile"
    assert makefile.read_text() == f"prefix={destination}\n"


def test_the_default_profile_refuses_a_cp311_tree(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "runtime.tar.gz", "3.11")
    stage_root, destination = _stage_root(tmp_path)

    with pytest.raises(RuntimeStagingError, match="content does not match its policy"):
        stage_python_runtime(
            archive,
            destination,
            stage_root=stage_root,
            expected=_expected(archive, version="3.11.14", abi_tag="cp311"),
            run=_runner(destination, release="3.11", version="3.11.14"),
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=lambda _tree: None,
        )
    assert not destination.exists()


def test_the_cp311_profile_refuses_a_tree_whose_probe_reports_another_release(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "runtime.tar.gz", "3.11")
    stage_root, destination = _stage_root(tmp_path)

    with pytest.raises(RuntimeStagingError, match="version is unsupported"):
        stage_python_runtime(
            archive,
            destination,
            stage_root=stage_root,
            expected=_expected(archive, version="3.12.0", abi_tag="cp312"),
            run=_runner(destination, release="3.11", version="3.12.0"),
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=lambda _tree: None,
            profile=CP311_PROFILE,
        )


def _otool(tree: Path, release: str):
    """A fake `otool` describing a well-formed relocatable tree for `release`."""

    def run(*command: str) -> str:
        target = Path(command[-1])
        if command[1] == "-l":
            return "      cmd LC_RPATH\n      path @executable_path/../lib (offset 12)\n"
        rows = ["/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)"]
        if target == tree / f"lib/libpython{release}.dylib":
            rows.insert(0, f"@rpath/libpython{release}.dylib (compatibility version 1.0.0, current version 1.0.0)")
        return f"{target}:\n" + "".join(f"    {row}\n" for row in rows)

    return run


def _unpacked(tmp_path: Path, release: str) -> Path:
    tree = tmp_path / f"tree-{release}"
    for name, data in _members(release).items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tree


def test_the_linkage_inspection_follows_the_profile(tmp_path: Path) -> None:
    tree = _unpacked(tmp_path, "3.11")

    _darwin_embedded_linkage(tree, run=_otool(tree, "3.11"), profile=CP311_PROFILE)

    # Under the default profile the tree's `libpython3.11` is not the profile's
    # own library, so its `LC_ID_DYLIB` row reads as a foreign dependency and
    # the inspection refuses it instead of passing vacuously.
    with pytest.raises(RuntimeStagingError, match="unsupported_dynamic_closure"):
        _darwin_embedded_linkage(tree, run=_otool(tree, "3.11"))
