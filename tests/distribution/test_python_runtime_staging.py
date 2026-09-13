from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from distribution.product_manifest import (
    PRODUCT_MANIFEST_SCHEMA,
    PYTHON_MAX_ARCHIVE_BYTES,
    PYTHON_MIN_ARCHIVE_BYTES,
    NodeRuntime,
    ProductManifestError,
    PythonRuntime,
    _darwin_embedded_linkage,
    _product_manifest_value,
    build_product_manifest,
    stage_python_runtime,
    validate_product_manifest,
)
from distribution.schema import canonical_json_bytes

PREFIX_MARKER = "@CORTEX_PYTHON_PREFIX@"
MACH_O = b"\xcf\xfa\xed\xfe"
# The archive must clear the policy's 8 MiB floor, so one incompressible member
# is built once and reused by every fixture in this module.
PADDING = os.urandom(9 * 1024 * 1024)

BASE_MEMBERS: dict[str, bytes] = {
    "bin/python3.14": MACH_O + b"interpreter\n",
    "lib/libpython3.14.dylib": MACH_O + b"libpython\n",
    "lib/python3.14/LICENSE.txt": b"PSF\n",
    "lib/python3.14/_sysconfigdata__darwin_darwin.py": (
        f'build_time_vars = {{"BINDIR": "{PREFIX_MARKER}/bin"}}\n'.encode()
    ),
    "lib/python3.14/config-3.14-darwin/Makefile": f"prefix={PREFIX_MARKER}\n".encode(),
    "lib/python3.14/ensurepip/__init__.py": b"",
    "lib/python3.14/os.py": b"sep = '/'\n",
    "lib/python3.14/venv/__init__.py": b"",
    "share/licenses/python-build-standalone/LICENSE.openssl-3.txt": b"OpenSSL\n",
    "share/licenses/python-build-standalone/python-licenses.rst": b"licences\n",
    "share/padding.bin": PADDING,
}
EXECUTABLE_MEMBERS = frozenset({"bin/python3.14"})


def _archive(
    destination: Path,
    *,
    members: dict[str, bytes] | None = None,
    extra: list[tarfile.TarInfo] | None = None,
) -> Path:
    payloads = BASE_MEMBERS if members is None else members
    with destination.open("wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
            for info in extra or ():
                archive.addfile(info)
            for name in sorted(payloads):
                data = payloads[name]
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if name in EXECUTABLE_MEMBERS else 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return destination


def _expected(archive: Path) -> dict[str, object]:
    payload = archive.read_bytes()
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "version": "3.14.6",
        "abi_tag": "cp314",
    }


def _report(
    interpreter: Path,
    *,
    tree: Path,
    destination: Path,
    version: str = "3.14.6",
    implementation: str = "CPython",
    prefix: Path | None = None,
    bindir: str | None = None,
    machine: str = "arm64",
) -> tuple[int, str, str]:
    root = str(prefix or tree)
    return (
        0,
        json.dumps(
            {
                "executable": str(interpreter),
                "prefix": root,
                "base_prefix": root,
                "version": version,
                "implementation": implementation,
                "platform": "darwin",
                "machine": machine,
                "bindir": bindir if bindir is not None else f"{destination}/bin",
                "libdir": f"{destination}/lib",
                "includepy": f"{destination}/include/python3.14",
                "ext_suffix": ".cpython-314-darwin.so",
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


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "stage"
    root.mkdir(mode=0o700, exist_ok=True)
    return root, root / "python-runtime"


def _runner(tree: Path, destination: Path, **overrides: object):
    def run(interpreter: Path) -> tuple[int, str, str]:
        return _report(interpreter, tree=tree, destination=destination, **overrides)

    return run


def _stage_python(
    tmp_path: Path,
    *,
    archive: Path | None = None,
    expected: dict[str, object] | None = None,
    inspect_linkage=lambda _tree: None,
    **overrides: object,
) -> PythonRuntime:
    stage_root, destination = _stage(tmp_path)
    archive = archive or _archive(tmp_path / "runtime.tar.gz")
    return stage_python_runtime(
        archive,
        destination,
        stage_root=stage_root,
        expected=expected or _expected(archive),
        run=_probing_runner(destination, **overrides),
        expected_system="Darwin",
        expected_machine="arm64",
        inspect_linkage=inspect_linkage,
    )


def _probing_runner(destination: Path, **overrides: object):
    def run(interpreter: Path) -> tuple[int, str, str]:
        # The probe runs against the staging path, whose root is two levels up
        # from `bin/python3.14`; the destination is where it will be published.
        return _report(
            interpreter,
            tree=interpreter.parent.parent,
            destination=destination,
            **overrides,
        )

    return run


def test_python_runtime_stages_from_a_digest_bound_archive(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "runtime.tar.gz")
    stage_root, destination = _stage(tmp_path)
    expected = _expected(archive)

    runtime = stage_python_runtime(
        archive,
        destination,
        stage_root=stage_root,
        expected=expected,
        run=_probing_runner(destination),
        expected_system="Darwin",
        expected_machine="arm64",
        inspect_linkage=lambda _tree: None,
    )

    assert runtime.version == "3.14.6"
    assert runtime.platform == "darwin"
    assert runtime.architecture == "arm64"
    assert runtime.archive_sha256 == expected["sha256"]
    interpreter = destination / "bin/python3.14"
    assert interpreter.is_file()
    assert runtime.interpreter_sha256 == hashlib.sha256(interpreter.read_bytes()).hexdigest()


def test_staged_tree_is_sealed_read_only_and_carries_no_symlink(tmp_path: Path) -> None:
    _stage_python(tmp_path)

    destination = tmp_path / "stage/python-runtime"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    for path in destination.rglob("*"):
        assert not path.is_symlink()
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            assert mode == 0o500
        elif path.name == "python3.14":
            assert mode == 0o500
        else:
            assert mode == 0o400
        assert path.stat().st_uid == os.geteuid()


def test_staging_substitutes_the_prefix_marker_with_the_destination(tmp_path: Path) -> None:
    _stage_python(tmp_path)

    destination = tmp_path / "stage/python-runtime"
    data = (destination / "lib/python3.14/_sysconfigdata__darwin_darwin.py").read_text()
    assert PREFIX_MARKER not in data
    assert f'"BINDIR": "{destination}/bin"' in data
    makefile = (destination / "lib/python3.14/config-3.14-darwin/Makefile").read_text()
    assert makefile == f"prefix={destination}\n"


def test_python_staging_rejects_an_archive_whose_digest_is_not_the_manifest(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "runtime.tar.gz")
    expected = _expected(archive) | {"sha256": "0" * 64}

    with pytest.raises(ProductManifestError, match="does not match the manifest"):
        _stage_python(tmp_path, archive=archive, expected=expected)


def test_python_staging_rejects_an_archive_whose_size_is_not_the_manifest(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "runtime.tar.gz")
    expected = _expected(archive) | {"size": 12}

    with pytest.raises(ProductManifestError, match="does not match the manifest"):
        _stage_python(tmp_path, archive=archive, expected=expected)


def test_python_staging_rejects_an_undersized_archive(tmp_path: Path) -> None:
    members = {name: value for name, value in BASE_MEMBERS.items() if name != "share/padding.bin"}
    archive = _archive(tmp_path / "runtime.tar.gz", members=members)

    with pytest.raises(ProductManifestError, match="size is unsafe"):
        _stage_python(tmp_path, archive=archive)


@pytest.mark.parametrize(
    "size",
    [PYTHON_MIN_ARCHIVE_BYTES - 1, PYTHON_MAX_ARCHIVE_BYTES + 1],
    ids=["below-floor", "above-ceiling"],
)
def test_python_staging_rejects_an_archive_outside_the_size_policy(
    tmp_path: Path, size: int
) -> None:
    """Both ends of one combined comparison, so neither half can be lost.

    The policy is a single `MIN <= st_size <= MAX` expression and nothing
    exercised either end, so a regression that dropped or inverted half of it
    was invisible. The file is sparse and its `expected` values are deliberate
    nonsense: the check reads `st_size` from the freshly opened descriptor,
    before a single byte is read or any digest re-derived.
    """

    archive = tmp_path / "runtime.tar.gz"
    with archive.open("wb") as handle:
        handle.truncate(size)

    with pytest.raises(ProductManifestError, match="archive size is unsafe"):
        _stage_python(
            tmp_path,
            archive=archive,
            expected={"sha256": "0" * 64, "size": size, "version": "3.14.6", "abi_tag": "cp314"},
        )


def test_python_staging_rejects_a_symlink_member(tmp_path: Path) -> None:
    info = tarfile.TarInfo("bin/python")
    info.type = tarfile.SYMTYPE
    info.linkname = "python3.14"
    info.mtime = 0
    archive = _archive(tmp_path / "runtime.tar.gz", extra=[info])

    with pytest.raises(ProductManifestError, match="unsupported member"):
        _stage_python(tmp_path, archive=archive)


def test_python_staging_rejects_a_hardlink_member(tmp_path: Path) -> None:
    info = tarfile.TarInfo("bin/python")
    info.type = tarfile.LNKTYPE
    info.linkname = "bin/python3.14"
    info.mtime = 0
    archive = _archive(tmp_path / "runtime.tar.gz", extra=[info])

    with pytest.raises(ProductManifestError, match="unsupported member"):
        _stage_python(tmp_path, archive=archive)


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/absolute.txt", "lib/../../escape.txt", "lib/./same.txt"],
)
def test_python_staging_rejects_a_traversing_member(tmp_path: Path, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.size = 0
    info.mtime = 0
    archive = _archive(tmp_path / "runtime.tar.gz", extra=[info])

    with pytest.raises(ProductManifestError, match="unsafe"):
        _stage_python(tmp_path, archive=archive)


def test_python_staging_rejects_a_duplicate_member(tmp_path: Path) -> None:
    info = tarfile.TarInfo("lib/python3.14/os.py")
    info.size = 0
    info.mtime = 0
    archive = _archive(tmp_path / "runtime.tar.gz", extra=[info])

    with pytest.raises(ProductManifestError, match="unsafe"):
        _stage_python(tmp_path, archive=archive)


def test_python_staging_rejects_a_residual_build_host_path(tmp_path: Path) -> None:
    members = dict(BASE_MEMBERS)
    members["lib/python3.14/_sysconfigdata__darwin_darwin.py"] = (
        b'build_time_vars = {"BINDIR": "/install/bin"}\n'
    )
    archive = _archive(tmp_path / "runtime.tar.gz", members=members)

    with pytest.raises(ProductManifestError, match="build-host path"):
        _stage_python(tmp_path, archive=archive)


def test_python_staging_rejects_content_that_does_not_match_the_policy(tmp_path: Path) -> None:
    members = {name: value for name, value in BASE_MEMBERS.items() if "ensurepip" not in name}
    archive = _archive(tmp_path / "runtime.tar.gz", members=members)

    with pytest.raises(ProductManifestError, match="content does not match its policy"):
        _stage_python(tmp_path, archive=archive)


def test_python_staging_rejects_a_pruned_path_that_came_back(tmp_path: Path) -> None:
    members = dict(BASE_MEMBERS)
    members["lib/python3.14/tkinter/__init__.py"] = b""
    archive = _archive(tmp_path / "runtime.tar.gz", members=members)

    with pytest.raises(ProductManifestError, match="content does not match its policy"):
        _stage_python(tmp_path, archive=archive)


def test_python_staging_rejects_a_probe_whose_prefix_is_not_the_generation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProductManifestError, match="prefix"):
        _stage_python(tmp_path, prefix=Path("/opt/homebrew"))


def test_python_staging_rejects_a_probe_whose_config_paths_are_not_the_destination(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProductManifestError, match="configuration"):
        _stage_python(tmp_path, bindir="/install/bin")


def test_python_staging_rejects_a_probe_whose_version_is_not_the_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProductManifestError, match="version"):
        _stage_python(tmp_path, version="3.13.0")


def test_python_staging_rejects_a_probe_that_is_not_cpython(tmp_path: Path) -> None:
    with pytest.raises(ProductManifestError, match="implementation"):
        _stage_python(tmp_path, implementation="PyPy")


def test_python_staging_rejects_an_existing_destination(tmp_path: Path) -> None:
    stage_root, destination = _stage(tmp_path)
    destination.mkdir()
    archive = _archive(tmp_path / "runtime.tar.gz")

    with pytest.raises(ProductManifestError, match="already exists"):
        stage_python_runtime(
            archive,
            destination,
            stage_root=stage_root,
            expected=_expected(archive),
            run=_probing_runner(destination),
            expected_system="Darwin",
            expected_machine="arm64",
            inspect_linkage=lambda _tree: None,
        )


def test_python_staging_publishes_nothing_when_the_probe_fails(tmp_path: Path) -> None:
    stage_root, destination = _stage(tmp_path)

    with pytest.raises(ProductManifestError):
        _stage_python(tmp_path, implementation="PyPy")

    assert not destination.exists()
    assert list(stage_root.iterdir()) == []


def _otool(rows: dict[str, list[str]], rpaths: list[str]):
    def run(*command: str) -> str:
        target = Path(command[-1])
        if command[1] == "-l":
            return "".join(
                f"          cmd LC_RPATH\n      cmdsize 40\n         path {value} (offset 12)\n"
                for value in rpaths
            )
        key = target.name
        listed = rows.get(key, ["/usr/lib/libSystem.B.dylib"])
        body = "".join(
            f"\t{value} (compatibility version 1.0.0, current version 1.0.0)\n" for value in listed
        )
        return f"{target}:\n{body}"

    return run


def _linkage_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "python-runtime"
    (tree / "bin").mkdir(parents=True)
    (tree / "lib").mkdir(parents=True)
    (tree / "bin/python3.14").write_bytes(MACH_O + b"interpreter\n")
    (tree / "lib/libpython3.14.dylib").write_bytes(MACH_O + b"libpython\n")
    (tree / "lib/python3.14").mkdir(parents=True)
    (tree / "lib/python3.14/os.py").write_bytes(b"sep = '/'\n")
    return tree


def test_embedded_linkage_accepts_the_measured_shape(tmp_path: Path) -> None:
    tree = _linkage_tree(tmp_path)
    run = _otool(
        {
            "python3.14": [
                "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
                "/usr/lib/libSystem.B.dylib",
            ],
            "libpython3.14.dylib": [
                "@rpath/libpython3.14.dylib",
                "/usr/lib/libSystem.B.dylib",
            ],
        },
        ["@executable_path/../lib"],
    )

    _darwin_embedded_linkage(tree, run=run)


def test_python_staging_rejects_a_non_system_dynamic_dependency(tmp_path: Path) -> None:
    tree = _linkage_tree(tmp_path)
    run = _otool(
        {"python3.14": ["/opt/homebrew/lib/libintl.8.dylib"]},
        ["@executable_path/../lib"],
    )

    with pytest.raises(ProductManifestError, match="unsupported_dynamic_closure"):
        _darwin_embedded_linkage(tree, run=run)


def test_python_staging_rejects_a_libpython_whose_id_is_absolute(tmp_path: Path) -> None:
    tree = _linkage_tree(tmp_path)
    run = _otool(
        {"libpython3.14.dylib": ["/opt/homebrew/lib/libpython3.14.dylib"]},
        ["@executable_path/../lib"],
    )

    with pytest.raises(ProductManifestError, match="unsupported_dynamic_closure"):
        _darwin_embedded_linkage(tree, run=run)


def test_python_staging_rejects_an_absolute_rpath(tmp_path: Path) -> None:
    tree = _linkage_tree(tmp_path)
    run = _otool(
        {"libpython3.14.dylib": ["@rpath/libpython3.14.dylib"]},
        ["@executable_path/../lib", "/opt/homebrew/lib"],
    )

    with pytest.raises(ProductManifestError, match="unsupported_dynamic_closure"):
        _darwin_embedded_linkage(tree, run=run)


def test_python_staging_rejects_an_unreadable_linkage_report(tmp_path: Path) -> None:
    tree = _linkage_tree(tmp_path)

    def run(*command: str) -> str:
        if command[1] == "-l":
            return "         path @executable_path/../lib (offset 12)\n"
        return "not an otool report\n"

    with pytest.raises(ProductManifestError, match="linkage report is invalid"):
        _darwin_embedded_linkage(tree, run=run)


def test_the_vendored_runtime_stages_probes_and_relocates_for_real(
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    tmp_path: Path,
) -> None:
    """The whole staging policy, exercised against the real pinned artifact.

    Synthetic archives cannot tell us whether the normalization actually
    relocates CPython; only running the shipped interpreter can.
    """

    stage_root, destination = _stage(tmp_path)
    archive = embedded_python_pin["archive"]
    expected = {
        "sha256": archive["sha256"],
        "size": archive["size"],
        "version": embedded_python_pin["version"],
        "abi_tag": embedded_python_pin["abi_tag"],
    }

    runtime = stage_python_runtime(
        embedded_python_runtime,
        destination,
        stage_root=stage_root,
        expected=expected,
        expected_system="Darwin",
        expected_machine="arm64",
    )

    assert runtime.version == "3.14.6"
    assert runtime.archive_sha256 == archive["sha256"]
    interpreter = destination / "bin/python3.14"
    assert runtime.interpreter_sha256 == hashlib.sha256(interpreter.read_bytes()).hexdigest()
    data = (destination / "lib/python3.14/_sysconfigdata__darwin_darwin.py").read_text()
    assert PREFIX_MARKER not in data
    assert f'"BINDIR": "{destination}/bin"' in data
    assert not any(path.is_symlink() for path in destination.rglob("*"))


# The canonical bytes a schema-1 product manifest has always had, for the fixed
# node input below. This is a FROZEN LITERAL and not a value to regenerate: those
# bytes are a legacy generation's identity-hash input, so any change to them —
# a new defaulted field, a renamed key, a different canonical ordering — silently
# stops an already-installed generation matching its recorded identity. If this
# fails, the question is whether the manifest change was intended, not whether
# the constant needs refreshing.
SCHEMA1_MANIFEST_SHA256 = "435dfb73a6646f846fffc5be8bdcae2cb806160fe2884f36f471d8da65c9724a"
SCHEMA1_MANIFEST_BYTES = 2301


def test_product_manifest_schema1_bytes_are_unchanged(tmp_path: Path) -> None:
    """A legacy generation's manifest must keep its exact identity input."""

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)

    manifest = build_product_manifest(node)

    assert manifest["schema_version"] == 1
    assert "python_runtime" not in manifest
    assert manifest["processes"]["control"]["runtime"] == "installed-python"
    assert validate_product_manifest(manifest) == manifest
    raw = canonical_json_bytes(manifest)
    assert len(raw) == SCHEMA1_MANIFEST_BYTES
    assert hashlib.sha256(raw).hexdigest() == SCHEMA1_MANIFEST_SHA256


# The canonical bytes a schema-2 product manifest has always had, for the fixed
# inputs below. FROZEN for the same reason as the schema-1 constant above, and
# for one more: `validate_product_manifest` builds its reference from the schema
# a manifest DECLARES, and the next release's `cortex-dist` validates the
# generation this release installed. A literal that moved under schema 2 would
# make every installed schema-2 generation fail under the newer tools. Grow the
# contract by adding a schema version, never by editing this one.
SCHEMA2_MANIFEST_SHA256 = "48e08645c199eedf194250214eff1d1c9eb898b7b72822ec374f8e5bdfbd9f18"
SCHEMA2_MANIFEST_BYTES = 3172


#: Schema 3 is schema 2 plus A1-8's `telegram_adapter` capability row. Frozen on
#: the same terms the moment it exists: the generation this release installs is
#: validated by the NEXT release's `cortex-dist` against the schema it declares.
SCHEMA3_MANIFEST_SHA256 = "63813ba3f477aa65c0fd50285982dfe9c73376b4d974ce11f6bca5b4a5664e12"
SCHEMA3_MANIFEST_BYTES = 3202


def _reference_python() -> PythonRuntime:
    return PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
        venv_interpreter_sha256="2" * 64,
    )


def test_product_manifest_schema2_bytes_are_unchanged() -> None:
    """A composed generation's manifest must keep its exact identity input.

    Schema 2 is no longer what `build_product_manifest` composes -- schema 3
    is -- but every generation already installed under schema 2 is still
    validated against this contract, so the bytes are asked for by version
    rather than by "whatever the current code builds".
    """

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)

    raw = canonical_json_bytes(
        _product_manifest_value(node, _reference_python(), schema_version=2)
    )

    assert len(raw) == SCHEMA2_MANIFEST_BYTES
    assert hashlib.sha256(raw).hexdigest() == SCHEMA2_MANIFEST_SHA256
    assert validate_product_manifest(json.loads(raw)) == json.loads(raw)


def test_product_manifest_schema3_bytes_are_pinned_from_the_start() -> None:
    """The version A1-8's capability row landed in, frozen the day it landed."""

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    manifest = build_product_manifest(node, python_runtime=_reference_python())
    raw = canonical_json_bytes(manifest)

    assert manifest["schema_version"] == PRODUCT_MANIFEST_SCHEMA == 3
    assert manifest["capabilities"]["telegram_adapter"] == "disabled"
    assert len(raw) == SCHEMA3_MANIFEST_BYTES
    assert hashlib.sha256(raw).hexdigest() == SCHEMA3_MANIFEST_SHA256


def test_the_capability_row_exists_in_schema_3_and_in_no_earlier_schema() -> None:
    """Growing the contract meant a new version, not an edit to an old one.

    Editing schema 2 in place would have made every composed generation
    already installed fail `load_generation` under the newer tools -- the same
    cross-generation failure the per-schema freeze exists to prevent.
    """

    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    python = _reference_python()
    for version, expected in ((1, False), (2, False), (3, True)):
        runtime = None if version == 1 else python
        manifest = _product_manifest_value(node, runtime, schema_version=version)
        capabilities = manifest["capabilities"]
        assert ("telegram_adapter" in capabilities) is expected, version


def test_product_manifest_schema2_binds_the_embedded_runtime(tmp_path: Path) -> None:
    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    python = PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
        venv_interpreter_sha256="2" * 64,
    )

    manifest = build_product_manifest(node, python_runtime=python)

    assert manifest["schema_version"] == PRODUCT_MANIFEST_SCHEMA == 3
    assert manifest["python_runtime"]["embedded"] is True
    assert manifest["python_runtime"]["runtime_mode"] == "embedded-cpython"
    assert manifest["python_runtime"]["interpreter_sha256"] == "2" * 64
    assert manifest["python_runtime"]["venv_interpreter_sha256"] == "2" * 64
    assert manifest["python_runtime"]["policy"]["host_interpreter_required"] is False
    assert manifest["processes"]["control"]["runtime"] == "embedded-python"
    assert manifest["processes"]["private_access"]["runtime"] == "embedded-python"
    assert validate_product_manifest(manifest) == manifest


def test_product_manifest_schema2_rejects_an_unmeasured_venv_interpreter() -> None:
    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    python = PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
    )

    with pytest.raises(ProductManifestError, match="virtual environment interpreter"):
        build_product_manifest(node, python_runtime=python)


def test_product_manifest_schema2_rejects_a_tampered_python_runtime() -> None:
    node = NodeRuntime("v26.0.0", "darwin", "arm64", "1" * 64)
    python = PythonRuntime(
        version="3.14.6",
        platform="darwin",
        architecture="arm64",
        interpreter_sha256="2" * 64,
        archive_sha256="3" * 64,
        venv_interpreter_sha256="2" * 64,
    )
    manifest = build_product_manifest(node, python_runtime=python)
    manifest["python_runtime"]["policy"]["host_interpreter_required"] = True

    with pytest.raises(ProductManifestError, match="Python runtime policy is invalid"):
        validate_product_manifest(manifest)
