"""Profiled staging kernel for a vendored, relocatable CPython runtime.

The primitive that expands one digest-bound CPython archive into a sealed,
self-describing tree is shared: the installer stages the product's own embedded
runtime with it, and `cortex runtime stage` must expand a slot's worker
interpreter with exactly the same fd-bound unpack, seal, linkage and probe. The
installed product cannot import `distribution` — the wheel packages only
`cortex_platform` and `deployment` — so the kernel lives here and
`distribution/product_manifest.py` imports it back.

Nothing in this module may import from `distribution`: that direction is the
whole point, and `tests/distribution/test_python_runtime_staging.py` plus the
layering guard pin it.

The 3.14 literals this kernel used to spell are `PythonRuntimeProfile` data,
with the product's cp314 profile as the default, so the behaviour every existing
caller sees is unchanged.

Contract: `docs/plans/2026-09-01-s32-per-slot-interpreter-design.md` D-S3.2-1
(AMD-1, second half) and D-S3.2-2 (AMD-2).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path


class RuntimeStagingError(RuntimeError):
    """A runtime tree could not be staged, sealed, probed, or composed safely.

    `distribution` re-exports this class as `ProductManifestError`, the name it
    has always raised under, so every existing caller and test is unaffected.
    """


PYTHON_MIN_ARCHIVE_BYTES = 8 * 1024 * 1024
PYTHON_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
PYTHON_MAX_MEMBERS = 20000
PYTHON_MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
PYTHON_MAX_MEMBER_BYTES = 96 * 1024 * 1024
PYTHON_MAX_MEMBER_DEPTH = 32
PYTHON_PREFIX_MARKER = "@CORTEX_PYTHON_PREFIX@"

PYTHON_REQUIRED_MODULES = (
    "bz2",
    "ctypes",
    "ensurepip",
    "hashlib",
    "lzma",
    "sqlite3",
    "ssl",
    "venv",
    "zlib",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACL_ENTRY = re.compile(r"^\s*[0-9]+:.*$", re.MULTILINE)
# `<n>: <user|group>:<principal> <allow|deny> <permissions>` as printed by
# `ls -lde`. A deny entry can only take permissions away, so it cannot make a
# path more reachable; only an allow entry can. macOS gives every home directory
# a default `group:everyone deny delete`, so rejecting deny entries would make
# the product unstageable from anywhere under `~`.
_ACL_ACE = re.compile(r"^\s*[0-9]+:\s+(?:user|group):.+?\s+(?P<action>allow|deny)\s")
_OTOOL_DEPENDENCY_ROW = re.compile(
    r"^[ \t]+(\S.*) \(compatibility version [0-9]+(?:\.[0-9]+){0,2}, "
    r"current version [0-9]+(?:\.[0-9]+){0,2}\)$"
)

_PYTHON_REPORT_FIELDS = {
    "base_prefix",
    "bindir",
    "executable",
    "ext_suffix",
    "implementation",
    "includepy",
    "libdir",
    "machine",
    "modules",
    "platform",
    "prefix",
    "version",
}
_MACH_O_MAGIC = frozenset(
    {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
)
_PYTHON_INSTALL_PATH = re.compile(r"/install(?![A-Za-z0-9_.-])")


@dataclass(frozen=True)
class PythonRuntimeProfile:
    """Everything one CPython release contributes to the staging policy.

    The staging primitive used to be single-version by construction: the
    supported release, the interpreter and library paths, the patched surfaces
    and the content tables all spelled 3.14 literally, so a slot carrying its
    own 3.11 interpreter was refused at four independent gates. The profile is
    that construction made explicit. Every table is derived from `release`,
    which makes a second release a declaration rather than a transcription.
    """

    name: str
    release: str
    supported_release: tuple[int, int]
    interpreter_relative: str
    library_relative: str
    library_id: str
    expected_rpaths: frozenset[str]
    extension_suffix: str
    patch_set: tuple[str, ...]
    required_files: tuple[str, ...]
    required_directories: tuple[str, ...]
    forbidden_paths: tuple[str, ...]


def _darwin_staging_profile(release: str) -> PythonRuntimeProfile:
    """Derive one profile's whole path policy from its CPython release."""

    major, minor = (int(part) for part in release.split(".", 1))
    return PythonRuntimeProfile(
        name=f"cp{major}{minor}",
        release=release,
        supported_release=(major, minor),
        interpreter_relative=f"bin/python{release}",
        library_relative=f"lib/libpython{release}.dylib",
        library_id=f"@rpath/libpython{release}.dylib",
        # A staged tree must resolve its library from its own root and nowhere
        # else, so the interpreter's rpath set is exactly this one entry.
        expected_rpaths=frozenset({"@executable_path/../lib"}),
        extension_suffix=f".cpython-{major}{minor}-darwin.so",
        # The surfaces from which the runtime, or a build against it, derives
        # paths. Kept identical to `tools/vendor_python_runtime.py`'s patch set.
        patch_set=(
            f"lib/python{release}/_sysconfigdata_*.py",
            f"lib/python{release}/_sysconfig_vars_*.json",
            "lib/pkgconfig/*.pc",
            f"lib/python{release}/config-{release}-darwin/Makefile",
        ),
        required_files=(
            f"bin/python{release}",
            f"lib/python{release}/LICENSE.txt",
            f"lib/python{release}/ensurepip/__init__.py",
            f"lib/python{release}/os.py",
            f"lib/python{release}/venv/__init__.py",
            "share/licenses/python-build-standalone/LICENSE.openssl-3.txt",
            "share/licenses/python-build-standalone/python-licenses.rst",
        ),
        required_directories=(f"lib/python{release}/config-{release}-darwin",),
        forbidden_paths=(
            "lib/libtcl9.0.dylib",
            f"lib/python{release}/idlelib",
            f"lib/python{release}/tkinter",
        ),
    )


# The product's own embedded runtime. Its tables are exactly the literals this
# kernel carried before the extraction, so every distribution caller is
# byte-for-byte unchanged.
CP314_PROFILE = _darwin_staging_profile("3.14")
# The per-slot worker interpreter of the S3.2 contract, paired with the cp311
# profile of `tools/vendor_python_runtime.py`.
CP311_PROFILE = _darwin_staging_profile("3.11")
DEFAULT_PROFILE = CP314_PROFILE
PROFILES: Mapping[str, PythonRuntimeProfile] = {
    profile.name: profile for profile in (CP314_PROFILE, CP311_PROFILE)
}


@dataclass(frozen=True)
class PythonRuntime:
    """A relocatable CPython tree extracted into an installer generation.

    `venv_interpreter_sha256` is measured after the generation's virtual
    environment is created, so it is empty on the value `stage_python_runtime`
    returns and is filled in by the installer before the manifest is built.
    """

    version: str
    platform: str
    architecture: str
    interpreter_sha256: str
    archive_sha256: str
    venv_interpreter_sha256: str = ""

    def as_manifest(self) -> dict[str, object]:
        if not _SHA256.fullmatch(self.venv_interpreter_sha256):
            raise RuntimeStagingError(
                "the generation's virtual environment interpreter has not been measured"
            )
        return {
            "version": self.version,
            "platform": self.platform,
            "architecture": self.architecture,
            "embedded": True,
            "runtime_mode": "embedded-cpython",
            "execution_ownership": "installer-owned-generation",
            "interpreter_sha256": self.interpreter_sha256,
            "archive_sha256": self.archive_sha256,
            "venv_interpreter_sha256": self.venv_interpreter_sha256,
            "policy": {
                "runtime": "embedded-cpython",
                "source_project": "astral-sh/python-build-standalone",
                "flavour": "install_only_stripped",
                "dynamic_dependency_policy": "system-libraries-only",
                "relocation": "executable-relative-rpath",
                "venv_mode": "copies-no-symlinks",
                "supported_platform": "darwin",
                "supported_architecture": "arm64",
                "identity_claim": (
                    "digest-bound-archive-extracted-into-immutable-generation"
                ),
                "host_interpreter_required": False,
            },
        }


PythonRunner = Callable[[Path], tuple[int, str, str]]


def _reject_extended_acl(paths: tuple[Path, ...], label: str) -> None:
    if not paths:
        return
    try:
        completed = subprocess.run(
            ["/bin/ls", "-lde", *(str(path) for path in paths)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeStagingError(f"{label} ACL inspection failed") from exc
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 1024 * 1024:
        raise RuntimeStagingError(f"{label} ACL inspection failed")
    for line in _ACL_ENTRY.findall(completed.stdout):
        ace = _ACL_ACE.match(line)
        # Anything that cannot be positively identified as a deny entry — an
        # allow entry, or a line this parser does not recognise — is refused.
        if ace is None or ace.group("action") != "deny":
            raise RuntimeStagingError(f"{label} has a permissive extended ACL")


def _validate_executable_parent_chain(path: Path) -> None:
    private_descendants = True
    parents = tuple(path.parents)
    _reject_extended_acl(parents, "Node executable parent chain")
    for index, parent in enumerate(parents):
        try:
            details = parent.lstat()
        except OSError as exc:
            raise RuntimeStagingError("Node executable parent chain is unavailable") from exc
        sticky_anchor = bool(
            details.st_uid == 0
            and details.st_mode & stat.S_ISVTX
            and details.st_mode & stat.S_IWOTH
        )
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid not in {0, os.geteuid()}
            or (details.st_mode & stat.S_IWOTH and not sticky_anchor)
            or (details.st_mode & stat.S_IWGRP and not sticky_anchor)
            or (sticky_anchor and (index == 0 or not private_descendants))
        ):
            raise RuntimeStagingError("Node executable parent chain is unsafe")
        if not sticky_anchor:
            private_descendants = bool(
                private_descendants
                and details.st_uid == os.geteuid()
                and details.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
            )


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _private_stage_directory(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise RuntimeStagingError("Node stage directory is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise RuntimeStagingError("Node stage directory is unsafe")
    _reject_extended_acl((path,), "Node stage directory")


def _bind_private_stage_root(path: Path) -> tuple[Path, int, os.stat_result]:
    requested = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise RuntimeStagingError("Node stage directory is unavailable") from exc
    if resolved != requested:
        raise RuntimeStagingError("Node stage directory contains a symlink ancestor")
    _private_stage_directory(resolved)
    _validate_executable_parent_chain(resolved / ".cortex-node-stage-boundary")
    descriptor = os.open(
        resolved,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        observed = resolved.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
            raise RuntimeStagingError("Node stage directory identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return resolved, descriptor, opened


def _verify_bound_stage_root(
    path: Path,
    descriptor: int,
    expected: os.stat_result,
) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeStagingError("Node stage directory identity changed") from exc
    opened = os.fstat(descriptor)
    if (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_uid,
    ) != (
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
        expected.st_uid,
    ) or (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeStagingError("Node stage directory identity changed")


def _is_system_library_path(value: str) -> bool:
    if (
        not value.startswith("/")
        or posixpath.normpath(value) != value
        or any(component in {"", ".", ".."} for component in value.split("/")[1:])
    ):
        return False
    return value.startswith(("/usr/lib/", "/System/Library/"))


def _r0_architecture(
    *,
    expected_system: str | None,
    expected_machine: str | None,
) -> str:
    if (expected_system or platform.system()) != "Darwin":
        raise RuntimeStagingError("the R0 Node policy supports Darwin only")
    machine = (expected_machine or platform.machine()).lower()
    if machine not in {"arm64", "aarch64"}:
        raise RuntimeStagingError("the R0 Node policy supports Darwin arm64 only")
    return "arm64"


def _default_python_runner(interpreter: Path) -> tuple[int, str, str]:
    program = (
        "import json,platform,sys,sysconfig\n"
        f"names={list(PYTHON_REQUIRED_MODULES)!r}\n"
        "for name in names:\n"
        "    __import__(name)\n"
        "print(json.dumps({"
        "'executable':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'version':'.'.join(str(part) for part in sys.version_info[:3]),"
        "'implementation':platform.python_implementation(),'platform':sys.platform,"
        "'machine':platform.machine(),"
        "'bindir':sysconfig.get_config_var('BINDIR'),"
        "'libdir':sysconfig.get_config_var('LIBDIR'),"
        "'includepy':sysconfig.get_config_var('INCLUDEPY'),"
        "'ext_suffix':sysconfig.get_config_var('EXT_SUFFIX'),"
        "'modules':sorted(names)}))"
    )
    try:
        completed = subprocess.run(
            [str(interpreter), "-I", "-S", "-B", "-c", program],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeStagingError("Python runtime inspection failed") from exc
    return completed.returncode, completed.stdout, completed.stderr


def _default_otool(*command: str) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeStagingError("Python dynamic linkage inspection failed") from exc
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 1024 * 1024:
        raise RuntimeStagingError("Python dynamic linkage inspection failed")
    return completed.stdout


OtoolRunner = Callable[..., str]
EmbeddedLinkageInspector = Callable[[Path], None]


def _mach_o_files(tree: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for path in sorted(tree.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(4) in _MACH_O_MAGIC:
                found.append(path)
    return tuple(found)


def _otool_dependencies(path: Path, *, run: OtoolRunner) -> list[str]:
    lines = run("/usr/bin/otool", "-L", str(path)).splitlines()
    if not lines or lines[0] != f"{path}:" or len(lines) < 2:
        raise RuntimeStagingError("Python dynamic linkage report is invalid")
    dependencies: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _OTOOL_DEPENDENCY_ROW.fullmatch(line)
        if match is None:
            raise RuntimeStagingError("Python dynamic linkage report is invalid")
        dependency = match.group(1)
        if " (compatibility version " in dependency:
            raise RuntimeStagingError("Python dynamic linkage report is invalid")
        dependencies.append(dependency)
    if not dependencies:
        raise RuntimeStagingError("Python dynamic linkage report is invalid")
    return dependencies


def _python_rpaths(interpreter: Path, *, run: OtoolRunner) -> set[str]:
    values: set[str] = set()
    for line in run("/usr/bin/otool", "-l", str(interpreter)).splitlines():
        stripped = line.strip()
        if not stripped.startswith("path "):
            continue
        value = stripped[len("path ") :]
        marker = value.rfind(" (offset ")
        if marker == -1:
            raise RuntimeStagingError("Python dynamic linkage report is invalid")
        values.add(value[:marker])
    return values


def _darwin_embedded_linkage(
    tree: Path,
    *,
    run: OtoolRunner = _default_otool,
    profile: PythonRuntimeProfile = DEFAULT_PROFILE,
) -> None:
    """Require every Mach-O in the staged tree to bind system libraries only.

    The one exception is `libpython`'s own `LC_ID_DYLIB`, which `otool -L`
    prints as the first dependency row of a dylib. The interpreter's rpath set
    must be exactly the executable-relative one, so a staged tree can never
    resolve a library from outside its own generation.
    """

    for path in _mach_o_files(tree):
        relative = path.relative_to(tree).as_posix()
        dependencies = _otool_dependencies(path, run=run)
        if relative == profile.library_relative:
            if dependencies[0] != profile.library_id:
                raise RuntimeStagingError("unsupported_dynamic_closure")
            dependencies = dependencies[1:]
        if any(not _is_system_library_path(item) for item in dependencies):
            raise RuntimeStagingError("unsupported_dynamic_closure")
    interpreter = tree / profile.interpreter_relative
    if _python_rpaths(interpreter, run=run) != set(profile.expected_rpaths):
        raise RuntimeStagingError("unsupported_dynamic_closure")


def _python_report(interpreter: Path, run: PythonRunner) -> dict[str, object]:
    returncode, stdout, _stderr = run(interpreter)
    if returncode != 0 or len(stdout.encode("utf-8")) > 1024 * 1024:
        raise RuntimeStagingError("Python runtime inspection failed")
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeStagingError("Python runtime report is invalid") from exc
    if not isinstance(report, dict) or set(report) != _PYTHON_REPORT_FIELDS:
        raise RuntimeStagingError("Python runtime report schema is invalid")
    return report


def probe_python_runtime(
    interpreter: Path,
    *,
    run: PythonRunner = _default_python_runner,
    expected: Mapping[str, object],
    destination_root: Path,
    archive_sha256: str = "",
    expected_system: str | None = None,
    expected_machine: str | None = None,
    profile: PythonRuntimeProfile = DEFAULT_PROFILE,
) -> PythonRuntime:
    """Execute one staged interpreter and require it to describe itself exactly.

    Two different roots are checked because they answer two different
    questions: `sys.prefix` must be the tree the interpreter was executed from,
    which proves relocation works, while the sysconfig paths must name the
    *destination*, which proves the staged prefix substitution landed. On an
    unpatched tree the latter reports the build host's `/install` prefix.
    """

    expected_architecture = _r0_architecture(
        expected_system=expected_system,
        expected_machine=expected_machine,
    )
    try:
        resolved = interpreter.expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeStagingError("Python interpreter is unavailable or unsafe") from exc
    tree = resolved.parent.parent
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        report = _python_report(resolved, run)
        after = os.stat(resolved, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeStagingError("Python runtime executable changed during inspection")
        if report["executable"] != str(resolved):
            raise RuntimeStagingError("Python runtime executable identity changed")
        if report["prefix"] != str(tree) or report["base_prefix"] != str(tree):
            raise RuntimeStagingError("Python runtime prefix is not the staged generation")
        if report["implementation"] != "CPython":
            raise RuntimeStagingError("Python runtime implementation is unsupported")
        version = str(report["version"])
        if version != str(expected["version"]):
            raise RuntimeStagingError("Python runtime version does not match the manifest")
        if _python_release(version) != profile.supported_release:
            raise RuntimeStagingError("Python runtime version is unsupported")
        if report["platform"] != "darwin":
            raise RuntimeStagingError("Python runtime platform is incompatible")
        if report["machine"] != expected_architecture:
            raise RuntimeStagingError("Python runtime architecture is incompatible")
        root = f"{destination_root}{os.sep}"
        if any(
            not isinstance(report[field], str) or not str(report[field]).startswith(root)
            for field in ("bindir", "libdir", "includepy")
        ):
            raise RuntimeStagingError(
                "Python runtime configuration paths are not inside the generation"
            )
        major, minor = _python_release(version)
        if report["ext_suffix"] != profile.extension_suffix:
            raise RuntimeStagingError("Python runtime extension suffix is unsupported")
        if f"cp{major}{minor}" != str(expected["abi_tag"]):
            raise RuntimeStagingError("Python runtime ABI does not match the manifest")
        if report["modules"] != sorted(PYTHON_REQUIRED_MODULES):
            raise RuntimeStagingError("Python runtime standard library is incomplete")
        digest = _descriptor_sha256(descriptor)
    finally:
        os.close(descriptor)
    return PythonRuntime(
        version=version,
        platform="darwin",
        architecture=expected_architecture,
        interpreter_sha256=digest,
        archive_sha256=archive_sha256,
    )


def _python_release(value: str) -> tuple[int, int]:
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeStagingError("Python reported an invalid version")
    return int(parts[0]), int(parts[1])


def _safe_python_member(info: tarfile.TarInfo) -> str:
    name = info.name
    if not name or "\0" in name or name.startswith("/"):
        raise RuntimeStagingError("Python runtime archive is unsafe")
    if posixpath.normpath(name) != name or len(name) > 1024:
        raise RuntimeStagingError("Python runtime archive is unsafe")
    components = name.split("/")
    if (
        len(components) > PYTHON_MAX_MEMBER_DEPTH
        or any(component in {"", ".", ".."} for component in components)
        or any(len(component) > 255 for component in components)
    ):
        raise RuntimeStagingError("Python runtime archive is unsafe")
    return name


def _extract_python_archive(descriptor: int, tree_descriptor: int) -> frozenset[str]:
    """Extract the digest-bound archive under the caller's own limits.

    Nothing from the archive is trusted: ownership, modes, and timestamps are
    discarded, only regular files and directories are created, and every path
    is created relative to an already-opened directory descriptor so no
    intermediate component can be replaced mid-extraction.
    """

    os.lseek(descriptor, 0, os.SEEK_SET)
    stream = open(descriptor, "rb", closefd=False)
    executables: set[str] = set()
    seen: set[str] = set()
    members = 0
    extracted = 0
    try:
        with tarfile.open(fileobj=stream, mode="r:gz") as archive:
            while True:
                try:
                    info = archive.next()
                except tarfile.TarError as exc:
                    raise RuntimeStagingError("Python runtime archive is unsafe") from exc
                if info is None:
                    break
                members += 1
                if members > PYTHON_MAX_MEMBERS:
                    raise RuntimeStagingError("Python runtime archive is unsafe")
                if set(info.pax_headers) - {"path", "size"}:
                    raise RuntimeStagingError(
                        "Python runtime archive contains an unsupported member"
                    )
                if not info.isdir() and not info.isreg():
                    raise RuntimeStagingError(
                        "Python runtime archive contains an unsupported member"
                    )
                if info.issparse():
                    raise RuntimeStagingError(
                        "Python runtime archive contains an unsupported member"
                    )
                name = _safe_python_member(info)
                if name in seen:
                    raise RuntimeStagingError("Python runtime archive is unsafe")
                seen.add(name)
                if info.isdir():
                    _make_python_directory(name, tree_descriptor)
                    continue
                if info.size > PYTHON_MAX_MEMBER_BYTES:
                    raise RuntimeStagingError("Python runtime archive is unsafe")
                extracted += info.size
                if extracted > PYTHON_MAX_EXTRACTED_BYTES:
                    raise RuntimeStagingError("Python runtime archive is unsafe")
                parent = name.rsplit("/", 1)[0] if "/" in name else ""
                if parent:
                    _make_python_directory(parent, tree_descriptor)
                source = archive.extractfile(info)
                if source is None:
                    raise RuntimeStagingError("Python runtime archive is unsafe")
                output = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=tree_descriptor,
                )
                try:
                    written = 0
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            count = os.write(output, view)
                            if count <= 0:
                                raise RuntimeStagingError("Python runtime archive is unsafe")
                            view = view[count:]
                    if written != info.size:
                        raise RuntimeStagingError("Python runtime archive is unsafe")
                finally:
                    os.close(output)
                if info.mode & 0o100:
                    executables.add(name)
    finally:
        stream.close()
    return frozenset(executables)


def _make_python_directory(name: str, tree_descriptor: int) -> None:
    parts = name.split("/")
    for index in range(1, len(parts) + 1):
        current = "/".join(parts[:index])
        try:
            os.mkdir(current, 0o700, dir_fd=tree_descriptor)
        except FileExistsError:
            details = os.stat(current, dir_fd=tree_descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(details.st_mode):
                raise RuntimeStagingError("Python runtime archive is unsafe") from None
        except OSError as exc:
            raise RuntimeStagingError("Python runtime archive is unsafe") from exc


def _substitute_python_prefix(
    tree: Path,
    destination: Path,
    *,
    profile: PythonRuntimeProfile = DEFAULT_PROFILE,
) -> None:
    """Bind the staged tree's recorded paths to where it will be published.

    Runs before the seal, because a sealed tree is deliberately unwritable. The
    assertion is scoped to the patched surface: the interpreter binary embeds
    the build prefix as a frozen compile-time constant that is inert after
    relocation and cannot be removed (contract §10 A1).
    """

    for pattern in profile.patch_set:
        for path in sorted(tree.glob(pattern)):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RuntimeStagingError(
                    "staged Python runtime retains a build-host path"
                ) from exc
            path.write_text(text.replace(PYTHON_PREFIX_MARKER, str(destination)), encoding="utf-8")
    for pattern in profile.patch_set:
        for path in sorted(tree.glob(pattern)):
            if path.is_symlink() or not path.is_file():
                continue
            # The destination is removed before scanning because a generation
            # may legitimately live under a path this scan would otherwise
            # mistake for the build host's temporary root.
            text = path.read_text(encoding="utf-8").replace(str(destination), "")
            if (
                PYTHON_PREFIX_MARKER in text
                or _PYTHON_INSTALL_PATH.search(text)
                or "/var/folders/" in text
            ):
                raise RuntimeStagingError("staged Python runtime retains a build-host path")


def _seal_python_tree(tree: Path, executables: frozenset[str]) -> None:
    """Make the extracted tree read-only, all but its own root.

    Renaming a directory updates its `..` entry, so the tree root must stay
    writable by its owner until it is published; the caller seals it through
    the bound stage descriptor immediately afterwards.
    """

    entries = sorted(tree.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in entries:
        relative = path.relative_to(tree).as_posix()
        if path.is_symlink():
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
        if path.is_dir():
            os.chmod(path, 0o500)
        elif path.is_file():
            os.chmod(path, 0o500 if relative in executables else 0o400)
        else:
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
    os.chmod(tree, 0o700)
    for path in (tree, *tree.rglob("*")):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            pass
        elif stat.S_ISREG(details.st_mode):
            if details.st_nlink != 1:
                raise RuntimeStagingError("staged Python runtime tree is unsafe")
        else:
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
        if (
            details.st_uid != os.geteuid()
            or details.st_mode & 0o022
            or details.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        ):
            raise RuntimeStagingError("staged Python runtime tree is unsafe")


def _assert_python_content(
    tree: Path, *, profile: PythonRuntimeProfile = DEFAULT_PROFILE
) -> None:
    for relative in profile.required_files:
        path = tree / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeStagingError("staged Python runtime content does not match its policy")
    for relative in profile.required_directories:
        if not (tree / relative).is_dir():
            raise RuntimeStagingError("staged Python runtime content does not match its policy")
    for relative in profile.forbidden_paths:
        if (tree / relative).exists():
            raise RuntimeStagingError("staged Python runtime content does not match its policy")


def assert_staged_python_runtime(
    tree: Path, *, profile: PythonRuntimeProfile = DEFAULT_PROFILE
) -> None:
    """Re-check a published tree cheaply, before it is reused.

    This is corruption detection, not tamper-proofing: an attacker who can write
    into the tree can also fix these modes, and the honest trust position is
    already recorded — the trust root is locally writable state, not a
    cryptographic anchor. What it does catch is the real failure class here, a
    tree left half-written or half-deleted by an interrupted run, before an
    interpreter is executed out of it.

    Deliberately not a re-run of the probe. Reuse must be cheap enough that
    `stage` can afford it on every call; anything that executes the interpreter
    belongs in the expansion path, which runs once per archive digest.
    """

    if tree.is_symlink() or not tree.is_dir():
        raise RuntimeStagingError("staged Python runtime tree is unavailable")
    root = tree.lstat()
    if stat.S_IMODE(root.st_mode) != 0o500 or root.st_uid != os.geteuid():
        raise RuntimeStagingError("staged Python runtime tree is unsafe")
    for path in tree.rglob("*"):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            if stat.S_IMODE(details.st_mode) != 0o500:
                raise RuntimeStagingError("staged Python runtime tree is unsafe")
        elif stat.S_ISREG(details.st_mode):
            if stat.S_IMODE(details.st_mode) not in {0o400, 0o500} or details.st_nlink != 1:
                raise RuntimeStagingError("staged Python runtime tree is unsafe")
        else:
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
        if (
            details.st_uid != os.geteuid()
            or details.st_mode & 0o022
            or details.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        ):
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
    _assert_python_content(tree, profile=profile)
    interpreter = tree / profile.interpreter_relative
    if interpreter.is_symlink() or not interpreter.is_file():
        raise RuntimeStagingError("staged Python runtime interpreter is missing")


def _remove_python_tree(path: Path) -> None:
    if not path.exists():
        return
    for current, directories, _files in os.walk(path, topdown=True):
        for name in (current, *(os.path.join(current, item) for item in directories)):
            try:
                os.chmod(name, 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def stage_python_runtime(
    archive: Path,
    destination: Path,
    *,
    stage_root: Path,
    expected: Mapping[str, object],
    final_destination: Path | None = None,
    run: PythonRunner = _default_python_runner,
    expected_system: str | None = None,
    expected_machine: str | None = None,
    inspect_linkage: EmbeddedLinkageInspector | None = None,
    profile: PythonRuntimeProfile = DEFAULT_PROFILE,
) -> PythonRuntime:
    """Extract one digest-bound CPython archive into a private generation.

    Every check is re-derived from bytes: `verify_bundle` already validated this
    archive, and it is validated again here from a freshly opened descriptor,
    because a recorded verdict is forgeable while the bundle is unsigned.

    `inspect_linkage` defaults to the profiled Darwin inspector rather than
    naming it in the signature, because the default has to be bound to the
    profile this call stages under, not to the profile that was current when
    the module was imported. An explicitly supplied inspector is called as
    given — the profile is NOT forwarded to it — so a non-default inspector
    must already be bound to the same profile this call stages under, or the
    linkage gate passes on a tree that profile never checked.
    """

    _r0_architecture(expected_system=expected_system, expected_machine=expected_machine)
    try:
        if final_destination is not None:
            final_value = os.fspath(final_destination.expanduser())
            if "\0" in final_value:
                raise ValueError("embedded null character")
            final_destination = Path(os.path.abspath(final_value))
        destination_value = os.fspath(destination.expanduser())
        if "\0" in destination_value:
            raise ValueError("embedded null character")
        destination = Path(os.path.abspath(destination_value))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeStagingError("Python stage destination is invalid") from exc
    bound_root, root_descriptor, root_identity = _bind_private_stage_root(stage_root)
    temporary_name: str | None = None
    archive_descriptor: int | None = None
    try:
        if destination.parent != bound_root or destination.name in {"", ".", ".."}:
            raise RuntimeStagingError("Python stage destination is outside its generation")
        try:
            os.stat(destination.name, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeStagingError("Python stage destination is unsafe") from exc
        else:
            raise RuntimeStagingError("Python stage destination already exists")

        archive_descriptor = _open_python_archive(archive)
        digest = _descriptor_sha256(archive_descriptor)
        details = os.fstat(archive_descriptor)
        if digest != str(expected["sha256"]) or details.st_size != int(expected["size"]):  # type: ignore[arg-type]
            raise RuntimeStagingError(
                "Python runtime archive identity does not match the manifest"
            )

        temporary_name = f".{destination.name}.{secrets.token_hex(16)}"
        os.mkdir(temporary_name, 0o700, dir_fd=root_descriptor)
        temporary_path = bound_root / temporary_name
        tree_descriptor = os.open(
            temporary_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            executables = _extract_python_archive(archive_descriptor, tree_descriptor)
        finally:
            os.close(tree_descriptor)

        # The tree is bound to where it will finally live, which is not the
        # publish path when the whole staging directory is itself renamed into
        # the generation afterwards.
        published_root = final_destination or destination
        _substitute_python_prefix(temporary_path, published_root, profile=profile)
        _seal_python_tree(temporary_path, executables)
        inspector = inspect_linkage or partial(_darwin_embedded_linkage, profile=profile)
        inspector(temporary_path)
        _assert_python_content(temporary_path, profile=profile)
        runtime = probe_python_runtime(
            temporary_path / profile.interpreter_relative,
            run=run,
            expected=expected,
            destination_root=published_root,
            archive_sha256=digest,
            expected_system=expected_system,
            expected_machine=expected_machine,
            profile=profile,
        )
        _verify_bound_stage_root(bound_root, root_descriptor, root_identity)
        os.rename(
            temporary_name,
            destination.name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        temporary_name = None
        os.chmod(destination.name, 0o500, dir_fd=root_descriptor, follow_symlinks=False)
        published = os.stat(destination.name, dir_fd=root_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(published.st_mode) or stat.S_IMODE(published.st_mode) != 0o500:
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
        os.fsync(root_descriptor)
        return runtime
    except BaseException:
        if temporary_name is not None:
            _remove_python_tree(bound_root / temporary_name)
        try:
            os.fsync(root_descriptor)
        except OSError:
            pass
        raise
    finally:
        if archive_descriptor is not None:
            os.close(archive_descriptor)
        os.close(root_descriptor)


def _open_python_archive(archive: Path) -> int:
    try:
        resolved = archive.expanduser()
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeStagingError("Python runtime archive is unavailable or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise RuntimeStagingError("Python runtime archive is not a single regular file")
        if details.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise RuntimeStagingError("Python runtime archive mode is unsafe")
        if details.st_uid not in {0, os.geteuid()} or details.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            raise RuntimeStagingError("Python runtime archive ownership or mode is unsafe")
        if not PYTHON_MIN_ARCHIVE_BYTES <= details.st_size <= PYTHON_MAX_ARCHIVE_BYTES:
            raise RuntimeStagingError("Python runtime archive size is unsafe")
        _reject_extended_acl((resolved,), "Python runtime archive")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
