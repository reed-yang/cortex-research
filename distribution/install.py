"""Transactional user-level install, upgrade, doctor, rollback, and uninstall."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import quote

from .bundle import BundleVerificationError, VerifiedBundle, verify_bundle
from .capabilities import host_capabilities
from .lifecycle import (
    FRONT_DOOR_PROBE_TIMEOUT,
    LifecycleError,
    LifecycleManager,
    lifecycle_quiescence,
    load_generation,
)
from .product_manifest import (
    ProductManifestError,
    PythonRuntime,
    build_product_manifest,
    stage_node_runtime,
    stage_python_runtime,
)
from .product_paths import (
    InstalledProductPaths,
    InstalledProductPathsError,
    resolve_installed_product_paths,
)
from .wheel_closure import WheelClosureError, wheel_tags_compatible
from .state_safety import (
    BackupBackedSafetyPort,
    StateSafetyError,
    UpgradeRequest,
    UpgradeSafetyPort,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - supported product hosts are POSIX
    fcntl = None


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}-[0-9a-f]{16}$")
_POINTER_FIELDS = {"schema_version", "version", "release_id", "release_sequence", "bundle_digest"}
_TRANSACTION_FIELDS = {
    "schema_version",
    "operation",
    "phase",
    "current_before",
    "current_after",
    "lkg_before",
    "lkg_after",
    "snapshot",
    "candidate_owned",
    "launchers_owned",
}
_TRANSACTION_FILE = ".pointer-transaction.json"
_TRANSACTION_OPERATIONS = {"install", "upgrade", "rollback"}
_TRANSACTION_PHASES = {
    "prepared",
    "last_known_good_published",
    "current_published",
}
_UNINSTALL_FILE = ".uninstall-transaction.json"
_UNINSTALL_TRASH = ".uninstall-trash"
_UNINSTALL_FIELDS = {
    "schema_version",
    "operation",
    "phase",
    "current_before",
    "lkg_before",
}
_UNINSTALL_PHASES = {"prepared", "isolated", "committed"}
_SNAPSHOT = re.compile(
    r"^[1-9][0-9]{0,24}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}-[0-9a-f]{16}\.json$"
)
_MAX_TRANSACTION_BYTES = 64 * 1024
_OWNED_TOP_LEVEL = {
    ".cortex-distribution-root.json",
    ".distribution.lock",
    _TRANSACTION_FILE,
    _UNINSTALL_FILE,
    _UNINSTALL_TRASH,
    "bin",
    "current.json",
    "last-known-good.json",
    "snapshots",
    "versions",
}


class InstallError(RuntimeError):
    """A distribution transaction failed without exposing private state."""


@dataclass(frozen=True)
class InstallResult:
    action: str
    release_id: str
    version: str


def default_distribution_root(
    *,
    home: Path | None = None,
    system: str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    home = home or Path.home()
    system = system or platform.system()
    env = env or dict(os.environ)
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Cortex" / "Distribution"
    xdg = Path(env.get("XDG_DATA_HOME", str(home / ".local" / "share")))
    return xdg / "cortex" / "distribution"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for directory, directory_names, file_names in os.walk(
        root,
        topdown=False,
        followlinks=False,
    ):
        current = Path(directory)
        for name in file_names:
            path = current / name
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode):
                continue
            if not stat.S_ISREG(details.st_mode):
                raise InstallError("candidate generation contains a special file")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for name in directory_names:
            details = (current / name).lstat()
            if stat.S_ISLNK(details.st_mode):
                continue
            if not stat.S_ISDIR(details.st_mode):
                raise InstallError("candidate generation contains a special file")
        _fsync_directory(current)


# Every child the installer runs gets a closed environment. The generation must
# behave identically whatever the operator's shell carries.
_CLOSED_PYTHON_ENVIRONMENT = {
    "HOME": "",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": os.defpath,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONPATH": "",
}
_CLOSED_PIP_ENVIRONMENT = _CLOSED_PYTHON_ENVIRONMENT | {
    "PIP_CONFIG_FILE": os.devnull,
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_CACHE_DIR": "1",
    "PIP_NO_INDEX": "1",
    "PIP_NO_INPUT": "1",
}
_EMBEDDED_INTERPRETER_RELATIVE = Path("python-runtime/bin/python3.14")
_VENV_SURPLUS_INTERPRETERS = ("python3", "python3.14")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


_LAUNCHER_VERSION_PATTERN = (
    r's/.*[,{]"version":"\([A-Za-z0-9][A-Za-z0-9._-]\{0,127\}-[0-9a-f]\{16\}\)".*/\1/p'
)


def _launcher_stub(prelude: str) -> str:
    """Wrap the launcher's Python body in a generation-agnostic shell stub.

    The stub exists so an installed product stops needing a host Python: it
    provisionally reads the pointed-at version, and if that generation carries
    its own interpreter it uses it, falling back to `${PYTHON:-python3}` so a
    rollback into a legacy generation still launches. The choice is provisional
    and unprivileged — the Python prelude re-reads and fully re-validates the
    pointer and refuses to continue if the version it computes differs.
    """

    return (
        "#!/bin/sh\n"
        "set -eu\n"
        'root=$(cd "$(/usr/bin/dirname "$0")/.." && pwd -P)\n'
        'pointer="$root/current.json"\n'
        '[ -f "$pointer" ] || { echo "Cortex installation pointer is invalid" >&2; exit 1; }\n'
        f"version=$(/usr/bin/sed -n '{_LAUNCHER_VERSION_PATTERN}' \"$pointer\")\n"
        'case "${version:-}" in\n'
        '    "" | *[!A-Za-z0-9._-]* )\n'
        '        echo "Cortex installation pointer is invalid" >&2\n'
        "        exit 1\n"
        "        ;;\n"
        "esac\n"
        'embedded="$root/versions/$version/python-runtime/bin/python3.14"\n'
        'if [ -f "$embedded" ] && [ ! -L "$embedded" ] && [ -x "$embedded" ]; then\n'
        '    interpreter="$embedded"\n'
        "else\n"
        '    interpreter="${PYTHON:-python3}"\n'
        "fi\n"
        "prelude=$(/bin/cat <<'CORTEX_LAUNCH_PRELUDE'\n"
        f"{prelude}"
        "CORTEX_LAUNCH_PRELUDE\n"
        ")\n"
        'exec "$interpreter" -I -B -c "$prelude" "$root" "$version" "$@"\n'
    )


def _host_macos_major() -> int:
    release = platform.mac_ver()[0]
    parts = release.split(".")
    if not parts or not parts[0].isdigit():
        raise InstallError("host macOS version is unavailable")
    major = int(parts[0])
    if major < 11:
        # `SYSTEM_VERSION_COMPAT` makes macOS report 10.16. Every supported host
        # is 11 or newer, so a 10.x report means the environment is shimmed and
        # the platform-tag ceiling below cannot be trusted.
        raise InstallError("host macOS version is unavailable")
    return major


def _require_compatible_wheels(wheels: tuple[Path, ...], runtime: Mapping[str, object]) -> None:
    """Refuse an incompatible closure before pip is invoked at all.

    `verify_bundle` already proved every wheel's tags against the interpreter
    the manifest names; this re-applies the same rule against the interpreter
    that was actually probed, and adds the one host fact that legitimately
    belongs here — whether this macOS is new enough to load the wheels. That is
    a host *capability* question, not an ABI question.
    """

    version = str(runtime["version"])
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise InstallError("embedded interpreter version is invalid")
    major, minor = int(parts[0]), int(parts[1])
    host_major = _host_macos_major()
    for wheel in wheels:
        fields = wheel.name.removesuffix(".whl").rsplit("-", 4)
        if len(fields) != 5:
            raise InstallError("wheel ABI does not match the embedded interpreter")
        _name, _version, python_tag, abi_tag, platform_tag = fields
        try:
            compatible = wheel_tags_compatible(
                python_tag, abi_tag, platform_tag, major=major, minor=minor
            )
        except WheelClosureError as exc:
            raise InstallError("wheel ABI does not match the embedded interpreter") from exc
        if not compatible:
            raise InstallError("wheel ABI does not match the embedded interpreter")
        for element in platform_tag.split("."):
            match = re.fullmatch(r"macosx_(\d+)_\d+_(?:arm64|universal2)", element)
            # `SYSTEM_VERSION_COMPAT` makes `mac_ver` report 10.16, which fails
            # this comparison closed rather than admitting a newer wheel.
            if match is not None and int(match.group(1)) > host_major:
                raise InstallError("wheel platform tag is newer than this host")


def _purge_bytecode(runtime: Path) -> None:
    """Drop compiled caches that record where the environment was built.

    `pip` byte-compiles what it installs regardless of
    `PYTHONDONTWRITEBYTECODE`, and each cached code object embeds the absolute
    source path — the staging tree, which will not exist once the generation is
    published. The caches are pure derived data; removing them keeps the
    published generation free of staging paths and costs one recompilation.
    """

    for path in sorted(runtime.rglob("__pycache__"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


def _remove_generation_tree(path: Path, *, ignore_errors: bool) -> None:
    """Remove a staged or published generation, unsealing it first.

    `stage_python_runtime` seals the embedded interpreter 0o500/0o400 so the
    published tree cannot be edited in place. Unlinking a file needs the write
    bit on its *directory*, so `shutil.rmtree` cannot descend into that tree at
    all: with `ignore_errors=True` it removes everything else and says nothing,
    leaving an unreferenced partial generation that a later upgrade to the same
    version then tries to verify and fails on; with `ignore_errors=False` it
    raises out of a cleanup path, masking whatever failure sent it there.

    Neither is acceptable, so the directories are made writable first. Symlinks
    are skipped and never followed, and each `chmod` is best effort -- this
    runs on failure paths, where doing as much as possible beats doing nothing.
    """

    for parent, directories, _files in os.walk(path, topdown=False):
        for name in directories:
            entry = Path(parent) / name
            if not entry.is_symlink():
                with contextlib.suppress(OSError):
                    os.chmod(entry, 0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    shutil.rmtree(path, ignore_errors=ignore_errors)


def _create_embedded_virtualenv(interpreter: Path, runtime: Path) -> None:
    """Build the generation's virtual environment from its own interpreter.

    `venv.EnvBuilder` cannot be used: it always builds against the *running*
    interpreter, which is the host coupling this tier exists to remove. With
    `--copies` against a non-framework build, `venv` copies the base executable
    verbatim, so the venv's interpreter must be byte-identical to the staged
    one. If a future CPython stops doing that, this fails loudly rather than
    silently reintroducing the host.
    """

    completed = subprocess.run(
        [str(interpreter), "-I", "-B", "-m", "venv", "--copies", str(runtime)],
        check=False,
        capture_output=True,
        text=True,
        env=dict(_CLOSED_PYTHON_ENVIRONMENT),
        timeout=300,
    )
    if completed.returncode != 0:
        raise InstallError("embedded virtual environment creation failed")
    # `venv` copies its script templates with `shutil.copymode`, and the staged
    # runtime it copies them from is sealed read-only, so the fresh environment
    # would arrive unwritable and could never be relocated. The environment is
    # not the immutable artifact — the staged runtime is — so its modes are
    # normalized to the private ones every other generation directory uses.
    for path in (runtime, *runtime.rglob("*")):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            os.chmod(path, 0o700)
        elif stat.S_ISREG(details.st_mode):
            os.chmod(path, 0o700 if details.st_mode & 0o111 else 0o600)
    for name in _VENV_SURPLUS_INTERPRETERS:
        (runtime / "bin" / name).unlink(missing_ok=True)
    _purge_bytecode(runtime)
    python = runtime / "bin" / "python"
    if python.is_symlink() or not python.is_file():
        raise InstallError("embedded virtual environment interpreter is missing")
    if _file_digest(python) != _file_digest(interpreter):
        raise InstallError("virtual environment interpreter is not the staged interpreter")
    if any(path.is_symlink() for path in runtime.rglob("*")):
        raise InstallError("embedded virtual environment contains a symlink")


def _pyvenv_configuration(runtime: Path) -> dict[str, str]:
    try:
        text = (runtime / "pyvenv.cfg").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("virtual environment binding is invalid") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def _verify_relocated_venv(runtime: Path, *, destination_root: Path, version: str) -> None:
    """Assert the relocated environment names the generation, not the stage.

    `_relocate_venv` is a byte replacement and therefore semantically blind, so
    the binding it is supposed to produce is asserted separately here. Both the
    plain and the fully resolved destination are accepted because `venv` records
    one of its paths through `realpath` and the other verbatim.
    """

    roots = {
        str(destination_root),
        os.path.join(os.path.realpath(destination_root.parent), destination_root.name),
    }
    interpreter_roots = {f"{root}/python-runtime" for root in roots}
    values = _pyvenv_configuration(runtime)
    recorded_version = values.get("version") or values.get("version_info", "")
    if (
        values.get("home") not in {f"{root}/bin" for root in interpreter_roots}
        or values.get("executable")
        not in {f"{root}/bin/python3.14" for root in interpreter_roots}
        or values.get("include-system-site-packages") != "false"
        or recorded_version.split("+")[0] != version
    ):
        raise InstallError("virtual environment binding is invalid")
    # `venv` writes a `#!/bin/sh` re-exec shim instead of a plain shebang when
    # the interpreter path exceeds the kernel's limit, which a real generation
    # path easily does, so the requirement is that the header names this
    # generation's interpreter — not that it takes one particular form.
    expected_python = os.fsencode(str(destination_root / "runtime" / "bin" / "python"))
    for path in (runtime / "bin").iterdir():
        details = path.lstat()
        if path.is_symlink():
            raise InstallError("virtual environment binding is invalid")
        if not stat.S_ISREG(details.st_mode):
            continue
        head = path.read_bytes()[:4096]
        if head.startswith(b"#!") and expected_python not in head:
            raise InstallError("virtual environment binding is invalid")
    staging_marker = os.fsencode(str(runtime.parent))
    if staging_marker == os.fsencode(str(destination_root)):
        return
    for path in runtime.rglob("*"):
        if path.is_symlink():
            raise InstallError("virtual environment binding is invalid")
        if not path.is_file():
            continue
        # `direct_url.json` records where pip's own wheel was read from, which
        # is inside the staging tree. It is provenance, not a dependency, and
        # rewriting it would desynchronise its distribution's `RECORD` hashes.
        if path.name == "direct_url.json" and path.parent.name.endswith(".dist-info"):
            continue
        if staging_marker in path.read_bytes():
            raise InstallError("virtual environment binding is invalid")


def _relocate_venv(runtime: Path, *, source_root: Path, destination_root: Path) -> None:
    source = os.fsencode(source_root)
    destination = os.fsencode(destination_root)
    candidates = [runtime / "pyvenv.cfg"]
    candidates.extend((runtime / "bin").iterdir())
    activation_scripts = {"activate", "activate.csh", "activate.fish"}
    for path in candidates:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode):
            continue
        payload = path.read_bytes()
        if source not in payload:
            continue
        if (
            path.parent == runtime / "bin"
            and path.name not in activation_scripts
            and not payload.startswith(b"#!")
        ):
            raise InstallError("virtual environment launcher is unsafe")
        replaced = payload.replace(source, destination)
        if source in replaced:
            raise InstallError("virtual environment relocation failed")
        path.write_bytes(replaced)
        os.chmod(path, stat.S_IMODE(details.st_mode))


def _validate_pointer(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != _POINTER_FIELDS:
        raise InstallError("installation pointer schema is invalid")
    version = raw.get("version")
    release_id = raw.get("release_id")
    bundle_digest = raw.get("bundle_digest")
    if (
        type(raw.get("schema_version")) is not int
        or raw["schema_version"] != 1
        or not isinstance(version, str)
        or not _VERSION.fullmatch(version)
        or not isinstance(release_id, str)
        or not _IDENTIFIER.fullmatch(release_id)
        or not isinstance(bundle_digest, str)
        or not _SHA256.fullmatch(bundle_digest)
        or version != f"{release_id}-{bundle_digest[:16]}"
    ):
        raise InstallError("installation pointer values are invalid")
    sequence = raw.get("release_sequence")
    if type(sequence) is not int or sequence < 1:
        raise InstallError("installation pointer sequence is invalid")
    return raw


def _read_pointer(path: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError("installation pointer is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise InstallError("installation pointer is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("installation pointer is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _validate_pointer(raw)


def _unlink_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


_JOURNAL_ABSENT: object = object()


def _read_journal_document(path: Path, label: str) -> object:
    """Open a closed journal under the shared archive safety checks and parse it.

    Returns the decoded document, or the _JOURNAL_ABSENT sentinel when the file
    does not exist. A present-but-null journal parses to None (a valid JSON
    value) and is returned as None, which the caller rejects as an invalid
    schema — absence and a malformed null document must not be conflated. The
    archive contract shared by every journal is enforced here (O_NOFOLLOW open;
    a regular file owned by the caller at 0o600 with a single link and bounded
    size; duplicate-key rejection). The journal-specific schema is validated by
    the caller.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return _JOURNAL_ABSENT
    except OSError as exc:
        raise InstallError(f"{label} is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > _MAX_TRANSACTION_BYTES
        ):
            raise InstallError(f"{label} is unsafe")
        payload = os.read(descriptor, _MAX_TRANSACTION_BYTES + 1)
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"{label} is unreadable") from exc
    finally:
        os.close(descriptor)
    return raw


def _create_journal_document(
    path: Path,
    transaction: dict[str, object],
    label: str,
) -> None:
    """Create a closed journal via an exclusive O_NOFOLLOW open and fsync it.

    The exclusive create, private mode, file fsync, and parent-directory fsync
    are the shared durable-archive contract for every journal; on any failure
    the partial journal is durably removed.
    """
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise InstallError(f"{label} could not be created") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(transaction, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            _unlink_durable(path)
        raise


def _ensure_private_directory(path: Path, label: str) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise InstallError(f"{label} is a symlink")
    details = path.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise InstallError(f"{label} is unsafe")


def _validate_marker(path: Path) -> None:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid():
            raise InstallError("distribution ownership marker is unsafe")
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("distribution ownership marker is unreadable") from exc
    if raw != {"schema_version": 1, "owner": "cortex-distribution"}:
        raise InstallError("distribution ownership marker is invalid")


def _composition_reason(exc: BaseException) -> str:
    """A path-free reason for a composition failure, safe to report.

    Composition can fail for unrelated reasons — an unusable Node, a Web closure
    violation, a generation contract breach — and collapsing them all into one
    opaque message leaves an operator with nothing to act on. The project's own
    error types carry curated, path-free messages, so those are quoted verbatim.
    Anything else may embed a filesystem path, so only its type is named.
    """

    if isinstance(
        exc,
        (InstallError, BundleVerificationError, LifecycleError, ProductManifestError),
    ):
        return str(exc)
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"{type(exc).__name__}({errno.errorcode.get(exc.errno, exc.errno)})"
    return type(exc).__name__


def _owned_staged_node(version_dir: Path) -> Path | None:
    """The generation's own staged Node, or None when it is absent or unsafe.

    Held to the same ownership standard as every other installed component: a
    regular, unlinked file owned by this user. Anything else is treated as absent
    so the caller fails closed rather than executing a foreign binary.
    """

    staged = version_dir / "node-runtime" / "bin" / "node"
    try:
        # Every component, not just the leaf: a symlinked `node-runtime` would
        # otherwise redirect the interpreter that runs the closure analyser.
        for component in (version_dir / "node-runtime", staged.parent, staged):
            details = component.lstat()
            if details.st_uid != os.geteuid() or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None
            if component is staged:
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    return None
                if details.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    return None
            elif not stat.S_ISDIR(details.st_mode):
                return None
    except OSError:
        return None
    return staged


class DistributionInstaller:
    """Own only code versions and pointers below a selected distribution root."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        state_safety: UpgradeSafetyPort | None = None,
    ) -> None:
        self.root = (root or default_distribution_root()).expanduser().absolute()
        self.state_safety = (
            BackupBackedSafetyPort() if state_safety is None else state_safety
        )
        self._last_recovery_action = "none"
        self._last_recovery_operation: str | None = None

    def _read_pointer_transaction(self) -> dict[str, object] | None:
        raw = _read_journal_document(self.root / _TRANSACTION_FILE, "update journal")
        if raw is _JOURNAL_ABSENT:
            return None
        if not isinstance(raw, dict) or set(raw) != _TRANSACTION_FIELDS:
            raise InstallError("update journal schema is invalid")
        if (
            type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or raw.get("operation") not in _TRANSACTION_OPERATIONS
            or raw.get("phase") not in _TRANSACTION_PHASES
            or type(raw.get("candidate_owned")) is not bool
            or type(raw.get("launchers_owned")) is not bool
        ):
            raise InstallError("update journal values are invalid")
        try:
            for field in ("current_before", "current_after", "lkg_before", "lkg_after"):
                if raw[field] is not None:
                    raw[field] = _validate_pointer(raw[field])
        except InstallError as exc:
            raise InstallError("update journal values are invalid") from exc
        snapshot = raw["snapshot"]
        if snapshot is not None and (
            not isinstance(snapshot, str) or _SNAPSHOT.fullmatch(snapshot) is None
        ):
            raise InstallError("update journal values are invalid")
        operation = raw["operation"]
        before = raw["current_before"]
        after = raw["current_after"]
        lkg_before = raw["lkg_before"]
        lkg_after = raw["lkg_after"]
        valid = False
        if operation == "install":
            valid = (
                before is None
                and lkg_before is None
                and after is not None
                and lkg_after == after
                and snapshot is None
                and raw["candidate_owned"] is True
                and raw["launchers_owned"] is True
            )
        elif operation == "upgrade":
            valid = (
                before is not None
                and after is not None
                and before != after
                and lkg_after == before
                and snapshot is not None
                and raw["launchers_owned"] is False
            )
        elif operation == "rollback":
            valid = (
                before is not None
                and lkg_before is not None
                and before != lkg_before
                and after == lkg_before
                and lkg_after == before
                and snapshot is None
                and raw["candidate_owned"] is False
                and raw["launchers_owned"] is False
            )
        if not valid:
            raise InstallError("update journal values are invalid")
        return raw

    def _begin_pointer_transaction(self, transaction: dict[str, object]) -> None:
        _create_journal_document(
            self.root / _TRANSACTION_FILE,
            transaction,
            "update journal",
        )
        self._pointer_transaction_checkpoint("prepared")

    def _update_pointer_transaction_phase(
        self,
        transaction: dict[str, object],
        phase: str,
    ) -> None:
        transaction["phase"] = phase
        _atomic_json(self.root / _TRANSACTION_FILE, transaction)

    def _publish_pointer_transaction(self, transaction: dict[str, object]) -> None:
        self._write_pointer(self.root / "last-known-good.json", transaction["lkg_after"])
        self._update_pointer_transaction_phase(
            transaction,
            "last_known_good_published",
        )
        self._pointer_transaction_checkpoint("last_known_good_published")
        self._write_pointer(self.root / "current.json", transaction["current_after"])
        self._update_pointer_transaction_phase(transaction, "current_published")
        self._pointer_transaction_checkpoint("current_published")
        _unlink_durable(self.root / _TRANSACTION_FILE)

    @staticmethod
    def _pointer_transaction_checkpoint(_phase: str) -> None:
        pass

    def _read_uninstall_transaction(self) -> dict[str, object] | None:
        raw = _read_journal_document(self.root / _UNINSTALL_FILE, "uninstall journal")
        if raw is _JOURNAL_ABSENT:
            return None
        if not isinstance(raw, dict) or set(raw) != _UNINSTALL_FIELDS:
            raise InstallError("uninstall journal schema is invalid")
        if (
            type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or raw.get("operation") != "uninstall"
            or raw.get("phase") not in _UNINSTALL_PHASES
            or raw.get("current_before") is None
        ):
            raise InstallError("uninstall journal values are invalid")
        try:
            raw["current_before"] = _validate_pointer(raw["current_before"])
            if raw["lkg_before"] is not None:
                raw["lkg_before"] = _validate_pointer(raw["lkg_before"])
        except InstallError as exc:
            raise InstallError("uninstall journal values are invalid") from exc
        return raw

    def _begin_uninstall_transaction(self, transaction: dict[str, object]) -> None:
        _create_journal_document(
            self.root / _UNINSTALL_FILE,
            transaction,
            "uninstall journal",
        )

    def _update_uninstall_transaction_phase(
        self,
        transaction: dict[str, object],
        phase: str,
    ) -> None:
        transaction["phase"] = phase
        _atomic_json(self.root / _UNINSTALL_FILE, transaction)

    @staticmethod
    def _uninstall_checkpoint(_phase: str) -> None:
        pass

    def _recover_uninstall_locked(self) -> str:
        try:
            transaction = self._read_uninstall_transaction()
        except InstallError as exc:
            raise InstallError("manual distribution recovery is required") from exc
        trash = self.root / _UNINSTALL_TRASH
        trash_present = os.path.lexists(trash)
        if transaction is None:
            if trash_present:
                raise InstallError("manual distribution recovery is required")
            return "none"
        unexpected = {
            path.name for path in self.root.iterdir()
        } - _OWNED_TOP_LEVEL
        if unexpected:
            raise InstallError("manual distribution recovery is required")
        trash_names: set[str] = set()
        if trash_present:
            try:
                details = trash.lstat()
                if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
                    raise InstallError("manual distribution recovery is required")
                trash_names = {path.name for path in trash.iterdir()}
            except OSError as exc:
                raise InstallError("manual distribution recovery is required") from exc
            allowed = {
                "bin",
                "snapshots",
                "versions",
                "last-known-good.json",
                "current.json",
            }
            if trash_names - allowed:
                raise InstallError("manual distribution recovery is required")
        current_path = self.root / "current.json"
        if os.path.lexists(current_path):
            try:
                current = _read_pointer(current_path)
            except InstallError as exc:
                raise InstallError("manual distribution recovery is required") from exc
            if current != transaction["current_before"]:
                raise InstallError("manual distribution recovery is required")
            if "current.json" in trash_names:
                raise InstallError("manual distribution recovery is required")
            restore_names = (
                "last-known-good.json",
                "bin",
                "snapshots",
                "versions",
            )
            for name in restore_names:
                if name in trash_names and os.path.lexists(self.root / name):
                    raise InstallError("manual distribution recovery is required")
            if trash_names - set(restore_names):
                raise InstallError("manual distribution recovery is required")
            for name in restore_names:
                source = trash / name
                if name in trash_names:
                    os.rename(source, self.root / name)
            if trash_present:
                try:
                    trash.rmdir()
                except OSError as exc:
                    raise InstallError("manual distribution recovery is required") from exc
                _fsync_directory(self.root)
            self._verify_pointer_generation(transaction["current_before"])
            _unlink_durable(self.root / _UNINSTALL_FILE)
            return "aborted"
        self._finish_uninstall_locked(trash)
        return "completed"

    @staticmethod
    def _write_pointer(path: Path, pointer: object) -> None:
        if pointer is None:
            _unlink_durable(path)
        else:
            _atomic_json(path, pointer)

    def _verify_pointer_generation(self, pointer: object) -> None:
        if pointer is None:
            return
        assert isinstance(pointer, dict)
        self._verify_installed_version(
            pointer,
            self.root / "versions" / str(pointer["version"]),
        )

    def _remove_transaction_directory(self, path: Path, label: str) -> None:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InstallError(f"{label} is unreadable") from exc
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
            raise InstallError(f"{label} is unsafe")
        _remove_generation_tree(path, ignore_errors=False)
        _fsync_directory(path.parent)

    def _remove_transaction_snapshot(self, name: object) -> None:
        if name is None:
            return
        assert isinstance(name, str)
        path = self.root / "snapshots" / name
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InstallError("transaction snapshot is unreadable") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
        ):
            raise InstallError("transaction snapshot is unsafe")
        _unlink_durable(path)
        with contextlib.suppress(OSError):
            path.parent.rmdir()
            _fsync_directory(self.root)

    def _require_transaction_snapshot(
        self,
        name: object,
        expected: object,
    ) -> None:
        if name is None:
            return
        assert isinstance(name, str)
        if _read_pointer(self.root / "snapshots" / name) != expected:
            raise InstallError("transaction snapshot is unavailable")

    def _require_launchers(self) -> None:
        directory = self.root / "bin"
        try:
            directory_details = directory.lstat()
        except OSError as exc:
            raise InstallError("transaction launchers are unavailable") from exc
        if (
            not stat.S_ISDIR(directory_details.st_mode)
            or directory_details.st_uid != os.geteuid()
        ):
            raise InstallError("transaction launchers are unsafe")
        for name in ("cortex", "cortexd"):
            try:
                details = (directory / name).lstat()
            except OSError as exc:
                raise InstallError("transaction launchers are unavailable") from exc
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.geteuid()
                or not details.st_mode & stat.S_IXUSR
            ):
                raise InstallError("transaction launchers are unsafe")

    def _recover_transaction_locked(self) -> str:
        transaction = self._read_pointer_transaction()
        if transaction is None:
            self._last_recovery_operation = None
            return "none"
        self._last_recovery_operation = str(transaction["operation"])
        try:
            current = _read_pointer(self.root / "current.json")
            lkg = _read_pointer(self.root / "last-known-good.json")
        except InstallError as exc:
            raise InstallError("manual distribution recovery is required") from exc
        before = transaction["current_before"]
        after = transaction["current_after"]
        lkg_before = transaction["lkg_before"]
        lkg_after = transaction["lkg_after"]
        if current != before and current != after:
            raise InstallError("manual distribution recovery is required")
        if lkg != lkg_before and lkg != lkg_after:
            raise InstallError("manual distribution recovery is required")
        if current == before:
            self._verify_pointer_generation(before)
            self._verify_pointer_generation(lkg_before)
            self._write_pointer(self.root / "current.json", before)
            self._write_pointer(self.root / "last-known-good.json", lkg_before)
            self._remove_transaction_snapshot(transaction["snapshot"])
            if transaction["candidate_owned"] is True:
                before_references = (before, lkg_before)
                if all(reference != after for reference in before_references):
                    assert isinstance(after, dict)
                    self._remove_transaction_directory(
                        self.root / "versions" / str(after["version"]),
                        "transaction candidate",
                    )
                    versions = self.root / "versions"
                    with contextlib.suppress(OSError):
                        versions.rmdir()
                        _fsync_directory(self.root)
            if transaction["launchers_owned"] is True:
                self._remove_transaction_directory(
                    self.root / "bin",
                    "transaction launcher directory",
                )
            _unlink_durable(self.root / _TRANSACTION_FILE)
            return "aborted"
        self._verify_pointer_generation(after)
        self._verify_pointer_generation(lkg_after)
        if transaction["launchers_owned"] is True:
            self._require_launchers()
        self._require_transaction_snapshot(transaction["snapshot"], before)
        self._write_pointer(self.root / "last-known-good.json", lkg_after)
        self._write_pointer(self.root / "current.json", after)
        _unlink_durable(self.root / _TRANSACTION_FILE)
        return "committed"

    @contextlib.contextmanager
    def _locked(self, *, create_root: bool = True) -> Iterator[None]:
        if fcntl is None:
            raise InstallError("distribution locking is unavailable")
        if not create_root and not os.path.lexists(self.root):
            self._last_recovery_action = "none"
            self._last_recovery_operation = None
            yield
            return
        parent = self.root.parent
        _ensure_private_directory(parent, "distribution parent directory")
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
                raise InstallError("distribution parent directory is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if self.root.is_symlink():
                raise InstallError("distribution root is a symlink")
            self.root.mkdir(mode=0o700, exist_ok=True)
            root_details = self.root.stat()
            if not stat.S_ISDIR(root_details.st_mode) or root_details.st_uid != os.geteuid():
                raise InstallError("distribution root is unsafe")
            marker = self.root / ".cortex-distribution-root.json"
            if not marker.exists():
                entries = {path.name for path in self.root.iterdir()}
                uninstall_witnesses = {_UNINSTALL_FILE, _UNINSTALL_TRASH}
                if entries and not entries <= uninstall_witnesses:
                    raise InstallError("distribution root is not empty and has no ownership marker")
                if not entries:
                    _atomic_json(marker, {"schema_version": 1, "owner": "cortex-distribution"})
            else:
                _validate_marker(marker)
            pointer_pending = os.path.lexists(self.root / _TRANSACTION_FILE)
            uninstall_pending = any(
                os.path.lexists(self.root / name)
                for name in (_UNINSTALL_FILE, _UNINSTALL_TRASH)
            )
            if pointer_pending and uninstall_pending:
                raise InstallError("manual distribution recovery is required")
            if pointer_pending:
                self._last_recovery_action = self._recover_transaction_locked()
            elif uninstall_pending:
                self._last_recovery_operation = None
                self._last_recovery_action = self._recover_uninstall_locked()
            else:
                self._last_recovery_operation = None
                self._last_recovery_action = "none"
            if os.path.lexists(self.root):
                self._discard_stale_candidates_locked()
            yield
        finally:
            os.close(descriptor)

    def recover(self) -> str:
        with self._locked(create_root=False):
            return self._last_recovery_action

    def _discard_stale_candidates_locked(self) -> None:
        versions = self.root / "versions"
        if not versions.exists():
            return
        try:
            details = versions.lstat()
        except OSError as exc:
            raise InstallError("versions directory is unreadable") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
        ):
            raise InstallError("versions directory is unsafe")
        removed = False
        for candidate in versions.iterdir():
            if not candidate.name.startswith(".candidate-"):
                continue
            try:
                candidate_details = candidate.lstat()
            except OSError as exc:
                raise InstallError("stale candidate is unreadable") from exc
            if (
                not stat.S_ISDIR(candidate_details.st_mode)
                or candidate_details.st_uid != os.geteuid()
            ):
                raise InstallError("stale candidate is unsafe")
            _remove_generation_tree(candidate, ignore_errors=False)
            removed = True
        if removed:
            if any(versions.iterdir()):
                _fsync_directory(versions)
            else:
                versions.rmdir()
                _fsync_directory(self.root)

    @staticmethod
    def _pointer(bundle: VerifiedBundle) -> dict[str, object]:
        manifest = bundle.manifest
        version = f"{manifest['release_id']}-{bundle.digest[:16]}"
        return {
            "schema_version": 1,
            "version": version,
            "release_id": manifest["release_id"],
            "release_sequence": manifest["release_sequence"],
            "bundle_digest": bundle.digest,
        }

    def install(
        self,
        bundle_path: Path,
        *,
        node_executable: Path | None = None,
        allow_unsigned_developer: bool = False,
    ) -> InstallResult:
        try:
            bundle = verify_bundle(bundle_path, node_executable=node_executable)
        except BundleVerificationError as exc:
            raise InstallError(str(exc)) from exc
        if bundle.manifest["channel"] == "developer-unsigned" and not allow_unsigned_developer:
            raise InstallError("unsigned developer bundle requires explicit acknowledgement")
        pointer = self._pointer(bundle)
        with self._locked():
            if (
                self._last_recovery_action == "completed"
                and not os.path.lexists(self.root)
            ):
                try:
                    self.root.mkdir(mode=0o700)
                    root_details = self.root.stat()
                except OSError as exc:
                    raise InstallError(
                        "distribution root could not be recreated"
                    ) from exc
                if (
                    not stat.S_ISDIR(root_details.st_mode)
                    or root_details.st_uid != os.geteuid()
                ):
                    raise InstallError("distribution root is unsafe")
                _atomic_json(
                    self.root / ".cortex-distribution-root.json",
                    {"schema_version": 1, "owner": "cortex-distribution"},
                )
            current = _read_pointer(self.root / "current.json")
            if current and current["bundle_digest"] == bundle.digest:
                current_dir = self.root / "versions" / str(current["version"])
                self._verify_installed_version(current, current_dir)
                self._health_check(current_dir / "runtime")
                return InstallResult("unchanged", str(pointer["release_id"]), str(pointer["version"]))
            if current:
                raise InstallError("a different release requires explicit upgrade")
            existing = {
                path.name
                for path in self.root.iterdir()
                if path.name != ".cortex-distribution-root.json"
            }
            if existing:
                raise InstallError("existing installation is missing its current pointer")
            version_dir = self.root / "versions" / str(pointer["version"])
            transaction: dict[str, object] = {
                "schema_version": 1,
                "operation": "install",
                "phase": "prepared",
                "current_before": None,
                "current_after": pointer,
                "lkg_before": None,
                "lkg_after": pointer,
                "snapshot": None,
                "candidate_owned": True,
                "launchers_owned": True,
            }
            try:
                self._begin_pointer_transaction(transaction)
                if not version_dir.exists():
                    if bundle.manifest["schema_version"] >= 2:
                        self._stage_version(
                            bundle,
                            version_dir,
                            node_executable=node_executable,
                        )
                    else:
                        self._stage_version(bundle, version_dir)
                else:
                    self._verify_installed_version(pointer, version_dir)
                self._health_check(version_dir / "runtime")
                self._install_launchers()
                self._publish_pointer_transaction(transaction)
            except BaseException:
                try:
                    self._recover_transaction_locked()
                except BaseException as recovery_exc:
                    raise InstallError(
                        "installation failed and recovery was incomplete"
                    ) from recovery_exc
                raise
            return InstallResult("installed", str(pointer["release_id"]), str(pointer["version"]))

    def upgrade(
        self,
        bundle_path: Path,
        *,
        runtime_root: Path,
        home: Path,
        environment: Mapping[str, str] | None = None,
        node_executable: Path | None = None,
        allow_unsigned_developer: bool = False,
        timeout: float = 10,
    ) -> InstallResult:
        try:
            bundle = verify_bundle(
                bundle_path, node_executable=self._closure_node(node_executable)
            )
        except BundleVerificationError as exc:
            raise InstallError(str(exc)) from exc
        if bundle.manifest["channel"] == "developer-unsigned" and not allow_unsigned_developer:
            raise InstallError("unsigned developer bundle requires explicit acknowledgement")
        pointer = self._pointer(bundle)
        version_dir = self.root / "versions" / str(pointer["version"])
        created = False
        with self._locked():
            current = _read_pointer(self.root / "current.json")
            if current is None:
                raise InstallError("upgrade requires an existing installation")
            if current["bundle_digest"] == bundle.digest:
                self._verify_installed_version(current, version_dir)
                self._health_check(version_dir / "runtime")
                return InstallResult("unchanged", str(pointer["release_id"]), str(pointer["version"]))
            if int(pointer["release_sequence"]) < int(current["release_sequence"]):
                raise InstallError("downgrade is forbidden; use explicit rollback")
            current_dir = self.root / "versions" / str(current["version"])
            current_bundle = self._verify_installed_version(current, current_dir)
            self._health_check(current_dir / "runtime")
            try:
                paths = resolve_installed_product_paths(
                    current_dir / "runtime",
                    home=home,
                    environment=environment,
                )
            except InstalledProductPathsError as exc:
                raise InstallError(str(exc)) from exc
            selected_runtime = Path(os.path.abspath(os.fspath(runtime_root.expanduser())))
            if selected_runtime != paths.runtime_update_root:
                raise InstallError("upgrade requires the canonical lifecycle root")
            try:
                if not version_dir.exists():
                    if bundle.manifest["schema_version"] >= 2:
                        candidate_node = node_executable
                        if current_bundle.manifest["schema_version"] >= 2:
                            candidate_node = (
                                current_dir / "node-runtime" / "bin" / "node"
                            )
                        self._stage_version(
                            bundle,
                            version_dir,
                            node_executable=candidate_node,
                        )
                    else:
                        self._stage_version(bundle, version_dir)
                    created = True
                else:
                    self._verify_installed_version(pointer, version_dir)
                self._health_check(version_dir / "runtime")
                candidate_schema = self._control_schema(version_dir / "runtime")
            except BaseException:
                if created:
                    _remove_generation_tree(version_dir, ignore_errors=True)
                raise

        request = UpgradeRequest(
            current_bundle_digest=str(current["bundle_digest"]),
            candidate_bundle_digest=bundle.digest,
            current_version=str(current["version"]),
            candidate_version=str(pointer["version"]),
            candidate_control_schema=candidate_schema,
            control_database=paths.control_database_file,
            identity_companion=paths.control_database_file.with_name(
                ".control.db.transport.key"
            ),
            runtime_root=paths.runtime_update_root,
        )
        try:
            with lifecycle_quiescence(runtime_root, timeout=timeout):
                with self._locked():
                    current_at_commit = _read_pointer(self.root / "current.json")
                    if current_at_commit != current:
                        raise InstallError("installation changed during upgrade")
                    try:
                        paths_at_commit = resolve_installed_product_paths(
                            current_dir / "runtime",
                            home=home,
                            environment=environment,
                        )
                    except InstalledProductPathsError as exc:
                        raise InstallError(str(exc)) from exc
                    if paths_at_commit != paths:
                        raise InstallError("product paths changed during upgrade")
                    self._verify_installed_version(pointer, version_dir)
                    self._health_check(version_dir / "runtime")
                    if self._control_schema(version_dir / "runtime") != candidate_schema:
                        raise InstallError("candidate control schema changed during upgrade")
                    authorization = self.state_safety.authorize(request)
                    if _read_pointer(self.root / "current.json") != current:
                        raise InstallError("installation changed during upgrade authorization")
                    self.state_safety.consume(authorization, request)
                    snapshots = self.root / "snapshots"
                    _ensure_private_directory(snapshots, "snapshot directory")
                    snapshot_name = f"{time.time_ns()}-{current['version']}.json"
                    snapshot_path = snapshots / snapshot_name
                    previous_lkg = _read_pointer(self.root / "last-known-good.json")
                    transaction = {
                        "schema_version": 1,
                        "operation": "upgrade",
                        "phase": "prepared",
                        "current_before": current,
                        "current_after": pointer,
                        "lkg_before": previous_lkg,
                        "lkg_after": current,
                        "snapshot": snapshot_name,
                        "candidate_owned": created,
                        "launchers_owned": False,
                    }
                    try:
                        self._begin_pointer_transaction(transaction)
                        _atomic_json(snapshot_path, current)
                        self._publish_pointer_transaction(transaction)
                    except BaseException:
                        try:
                            self._recover_transaction_locked()
                        except BaseException as recovery_exc:
                            raise InstallError(
                                "upgrade failed and recovery was incomplete"
                            ) from recovery_exc
                        raise
        except (LifecycleError, StateSafetyError) as exc:
            self._discard_unreferenced_candidate(pointer, version_dir, created=created)
            raise InstallError(str(exc)) from exc
        except BaseException:
            self._discard_unreferenced_candidate(pointer, version_dir, created=created)
            raise
        return InstallResult("upgraded", str(pointer["release_id"]), str(pointer["version"]))

    def _discard_unreferenced_candidate(
        self,
        pointer: dict[str, object],
        version_dir: Path,
        *,
        created: bool,
    ) -> None:
        if not created:
            return
        with self._locked():
            references = (
                _read_pointer(self.root / "current.json"),
                _read_pointer(self.root / "last-known-good.json"),
            )
            if all(
                reference is None
                or reference["bundle_digest"] != pointer["bundle_digest"]
                for reference in references
            ):
                _remove_generation_tree(version_dir, ignore_errors=True)

    @staticmethod
    def _control_schema(runtime: Path) -> int:
        completed = subprocess.run(
            [
                str(runtime / "bin" / "python"),
                "-I",
                "-c",
                "from cortex_platform.product.control.schema import SCHEMA_VERSION; print(SCHEMA_VERSION)",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                "HOME": "",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": "",
            },
            timeout=30,
        )
        value = completed.stdout
        if completed.returncode != 0 or re.fullmatch(r"[1-9][0-9]*\n?", value) is None:
            raise InstallError("candidate control schema is unavailable")
        return int(value)

    def _stateful_upgrade_report(
        self,
        version_dir: Path,
        *,
        home: Path,
        environment: Mapping[str, str] | None,
    ) -> dict[str, object] | None:
        """Report whether an upgrade over existing Control state is authorized.

        Read-only, and never raises: `doctor` is a diagnosis, so a refusal is
        reported as its reason instead of as a failure. Absent entirely when a
        non-default safety port is installed, because the projection is a
        property of that coordinator rather than of the installation.

        The product paths are resolved here rather than taken from a lifecycle
        object so that this answers the same question `upgrade` will ask, from
        the same source, whatever the caller passed in.
        """

        port = self.state_safety
        if not isinstance(port, BackupBackedSafetyPort):
            return None
        try:
            candidate_schema = self._control_schema(version_dir / "runtime")
            paths = resolve_installed_product_paths(
                version_dir / "runtime",
                home=home,
                environment=environment,
            )
        except (
            InstallError,
            InstalledProductPathsError,
            OSError,
            subprocess.SubprocessError,
        ):
            return {
                "required": True,
                "authorized": False,
                "reason": "installed product state is unreadable",
                "proof_id": None,
            }
        try:
            binding = port.inspect_state(
                control_database=paths.control_database_file,
                identity_companion=paths.control_database_file.with_name(
                    ".control.db.transport.key"
                ),
                runtime_root=paths.runtime_update_root,
                candidate_control_schema=candidate_schema,
            )
        except StateSafetyError as exc:
            return {
                "required": True,
                "authorized": False,
                "reason": str(exc),
                "proof_id": None,
            }
        if binding is None:
            return {
                "required": False,
                "authorized": True,
                "reason": None,
                "proof_id": None,
            }
        return {
            "required": True,
            "authorized": True,
            "reason": None,
            "proof_id": binding.proof_id,
        }

    @staticmethod
    def _stage_embedded_runtime(
        bundle: VerifiedBundle,
        stage: Path,
        destination: Path,
    ) -> PythonRuntime:
        """Extract the bundle's own interpreter, then build the venv from it.

        The archive is re-opened and re-hashed from its own descriptor even
        though `verify_bundle` just checked it, and the interpreter is executed
        and made to describe itself before the environment is created. Both are
        deliberate: an unsigned bundle's recorded verdict is forgeable, so every
        stage re-derives from bytes.
        """

        described = bundle.manifest["python_runtime"]
        archive = stage / "bundle" / str(described["path"])
        runtime = stage_python_runtime(
            archive,
            stage / "python-runtime",
            stage_root=stage,
            expected=described,
            final_destination=destination / "python-runtime",
        )
        _create_embedded_virtualenv(stage / _EMBEDDED_INTERPRETER_RELATIVE, stage / "runtime")
        return dataclasses.replace(
            runtime,
            venv_interpreter_sha256=_file_digest(stage / "runtime" / "bin" / "python"),
        )

    def _stage_version(
        self,
        bundle: VerifiedBundle,
        destination: Path,
        *,
        node_executable: Path | None = None,
    ) -> None:
        schema_version = bundle.manifest["schema_version"]
        if schema_version >= 2 and node_executable is None:
            raise InstallError("Node executable is required for a composed bundle")
        _ensure_private_directory(destination.parent, "versions directory")
        stage = Path(tempfile.mkdtemp(prefix=".candidate-", dir=destination.parent))
        published = False
        try:
            shutil.copytree(bundle.path, stage / "bundle", symlinks=False)
            verify_bundle(stage / "bundle", node_executable=node_executable)
            runtime = stage / "runtime"
            python_runtime = None
            if schema_version == 3:
                # The generation's interpreter comes from the bundle, never from
                # a prior generation and never from the host, so there is
                # deliberately no `_closure_python` mirroring `_closure_node`.
                python_runtime = self._stage_embedded_runtime(bundle, stage, destination)
            else:
                # Retained byte-for-byte: this is the schema-1/2 fault point the
                # crash-injection tests inject into, and the path a rollback into
                # an already-published generation still takes.
                venv.EnvBuilder(with_pip=True, clear=False, symlinks=True).create(runtime)
            wheel_dir = stage / "bundle" / "artifacts" / "wheels"
            wheels = sorted(str(path) for path in wheel_dir.glob("*.whl"))
            if schema_version == 3:
                _require_compatible_wheels(
                    tuple(Path(path) for path in wheels),
                    bundle.manifest["python_runtime"],
                )
            completed = subprocess.run(
                [
                    str(runtime / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--no-deps",
                    *wheels,
                ],
                check=False,
                capture_output=True,
                text=True,
                # Closed, not inherited: a host `PIP_INDEX_URL` or
                # `PIP_CONFIG_FILE` would otherwise let an offline install reach
                # the network and install something the bundle does not carry.
                env=dict(_CLOSED_PIP_ENVIRONMENT),
                timeout=900,
            )
            if completed.returncode != 0:
                raise InstallError("local wheel installation failed")
            if schema_version >= 2:
                try:
                    shutil.copytree(
                        stage / "bundle" / "artifacts" / "web",
                        stage / "web",
                        symlinks=False,
                    )
                    node_root = stage / "node-runtime" / "bin"
                    _ensure_private_directory(node_root, "Node runtime directory")
                    node_runtime = stage_node_runtime(
                        node_executable,
                        node_root / "node",
                        stage_root=node_root,
                    )
                    _atomic_json(
                        stage / "product-manifest.json",
                        build_product_manifest(node_runtime, python_runtime),
                    )
                    installed = load_generation(stage)
                    if installed.bundle_digest != bundle.digest:
                        raise LifecycleError(
                            "installed generation bundle identity changed"
                        )
                except Exception as exc:
                    raise InstallError(
                        f"generation composition failed: {_composition_reason(exc)}"
                    ) from exc
            if schema_version == 3:
                # Before relocation, not after: relocation repoints `pyvenv.cfg`
                # at a generation directory that does not exist yet, so an
                # embedded venv cannot be executed again until it is published.
                # A legacy venv is unaffected — its `home` names a host
                # interpreter that exists either way.
                self._health_check(runtime, embedded=True)
                # Last, because every interpreter invocation above — the health
                # check included — recreates caches that record the staging path.
                _purge_bytecode(runtime)
            _relocate_venv(
                runtime,
                source_root=stage,
                destination_root=destination,
            )
            if schema_version == 3:
                _verify_relocated_venv(
                    runtime,
                    destination_root=destination,
                    version=str(bundle.manifest["python_runtime"]["version"]),
                )
            else:
                self._health_check(runtime)
            _fsync_tree(stage)
            os.replace(stage, destination)
            published = True
            _fsync_directory(destination.parent)
        except BaseException:
            if published:
                _remove_generation_tree(destination, ignore_errors=True)
                with contextlib.suppress(OSError):
                    _fsync_directory(destination.parent)
            else:
                _remove_generation_tree(stage, ignore_errors=True)
            raise

    def _health_check(self, runtime: Path, *, embedded: bool = False) -> None:
        python = runtime / "bin" / "python"
        # The same closed environment `_control_schema` already uses. Inheriting
        # `os.environ` here let a host `PYTHONHOME` or `VIRTUAL_ENV` reach the
        # generation's own interpreter, which is exactly what this check exists
        # to measure.
        completed = subprocess.run(
            [str(python), "-I", "-c", "import cortex_platform, cortex_research"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(_CLOSED_PYTHON_ENVIRONMENT),
            timeout=30,
        )
        if completed.returncode != 0:
            raise InstallError("installed wheel import health check failed")
        if not (runtime / "bin" / "cortex").is_file() or not (runtime / "bin" / "cortexd").is_file():
            raise InstallError("installed Cortex entry points are missing")
        if not embedded:
            return
        generation = runtime.parent
        report = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import sys;print(sys.prefix);print(sys.base_prefix);print(sys.executable)",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=dict(_CLOSED_PYTHON_ENVIRONMENT),
            timeout=30,
        )
        expected = (
            str(runtime),
            str(generation / "python-runtime"),
            str(python),
        )
        if report.returncode != 0 or tuple(report.stdout.splitlines()) != expected:
            raise InstallError("installed interpreter is not the generation's own")
        closure = subprocess.run(
            [str(python), "-I", "-m", "pip", "check"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(_CLOSED_PIP_ENVIRONMENT),
            timeout=300,
        )
        if closure.returncode != 0:
            raise InstallError("installed dependency closure is broken")

    def _install_launchers(self) -> None:
        directory = self.root / "bin"
        _ensure_private_directory(directory, "launcher directory")
        for executable in ("cortex", "cortexd"):
            script = directory / executable
            pointer_prelude = (
                "import json, os, re, sys\n"
                "from pathlib import Path\n"
                "root = Path(sys.argv[1])\n"
                "provisional = sys.argv[2]\n"
                "try:\n"
                "    pointer = json.loads((root / 'current.json').read_text())\n"
                "except (OSError, UnicodeDecodeError, json.JSONDecodeError):\n"
                "    raise SystemExit('Cortex installation pointer is invalid')\n"
                "fields = {'schema_version', 'version', 'release_id', 'release_sequence', 'bundle_digest'}\n"
                "version = pointer.get('version') if isinstance(pointer, dict) else None\n"
                "release_id = pointer.get('release_id') if isinstance(pointer, dict) else None\n"
                "digest = pointer.get('bundle_digest') if isinstance(pointer, dict) else None\n"
                "sequence = pointer.get('release_sequence') if isinstance(pointer, dict) else None\n"
                "if (not isinstance(pointer, dict) or set(pointer) != fields or pointer.get('schema_version') != 1 or isinstance(pointer.get('schema_version'), bool) or not isinstance(version, str) or not isinstance(release_id, str) or not isinstance(digest, str) or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1 or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', release_id) is None or re.fullmatch(r'[0-9a-f]{64}', digest) is None or version != f'{release_id}-{digest[:16]}'):\n"
                "    raise SystemExit('Cortex installation pointer is invalid')\n"
                # The shell stub below chose an interpreter from a provisionally
                # parsed version. That choice is unprivileged and is re-derived
                # here, under the full validation, before anything runs.
                "if version != provisional:\n"
                "    raise SystemExit('Cortex installation pointer changed during launch')\n"
                "generation = root / 'versions' / version\n"
            )
            if executable == "cortex":
                body = pointer_prelude + (
                    "python = generation / 'runtime' / 'bin' / 'python'\n"
                    "module_root = generation / 'bundle' / 'tools'\n"
                    "bootstrap = \"import sys;sys.path.insert(0,sys.argv[1]);from distribution.cli import main;raise SystemExit(main(sys.argv[2:]))\"\n"
                    "arguments = sys.argv[3:]\n"
                    "prefix_commands = {'doctor', 'install', 'record-proof', 'recover', 'rollback', 'start', 'status', 'stop', 'uninstall', 'upgrade'}\n"
                    "if arguments and arguments[0] in prefix_commands and not any(value == '--prefix' or value.startswith('--prefix=') for value in arguments[1:]):\n"
                    "    arguments = [arguments[0], '--prefix', str(root), *arguments[1:]]\n"
                    "os.execv(python, [str(python), '-I', '-B', '-c', bootstrap, str(module_root), *arguments])\n"
                )
            else:
                body = pointer_prelude + (
                    "target = generation / 'runtime' / 'bin' / 'cortexd'\n"
                    "os.execv(target, [str(target), *sys.argv[3:]])\n"
                )
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{executable}.", dir=directory)
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o700)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(_launcher_stub(body))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, script)
                _fsync_directory(directory)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    def rollback(
        self,
        *,
        runtime_root: Path,
        home: Path,
        environment: Mapping[str, str] | None = None,
        timeout: float = 10,
    ) -> InstallResult:
        with self._locked():
            current = _read_pointer(self.root / "current.json")
            previous = _read_pointer(self.root / "last-known-good.json")
            if current is None or previous is None:
                raise InstallError("rollback pointer is unavailable")
            recovered_rollback = (
                self._last_recovery_action == "committed"
                and self._last_recovery_operation == "rollback"
            )
            if recovered_rollback:
                current_dir = self.root / "versions" / str(current["version"])
                self._verify_installed_version(current, current_dir)
                self._health_check(current_dir / "runtime")
                try:
                    paths = resolve_installed_product_paths(
                        current_dir / "runtime",
                        home=home,
                        environment=environment,
                    )
                except InstalledProductPathsError as exc:
                    raise InstallError(str(exc)) from exc
                self._require_canonical_runtime(runtime_root, paths)
                return InstallResult(
                    "rolled-back",
                    str(current["release_id"]),
                    str(current["version"]),
                )
            if previous["bundle_digest"] == current["bundle_digest"]:
                raise InstallError("no prior version is available for rollback")
            current_dir = self.root / "versions" / str(current["version"])
            target_dir = self.root / "versions" / str(previous["version"])
            self._verify_installed_version(current, current_dir)
            self._verify_installed_version(previous, target_dir)
            self._health_check(current_dir / "runtime")
            self._health_check(target_dir / "runtime")
            try:
                paths = resolve_installed_product_paths(
                    current_dir / "runtime",
                    home=home,
                    environment=environment,
                )
            except InstalledProductPathsError as exc:
                raise InstallError(str(exc)) from exc
            self._require_canonical_runtime(runtime_root, paths)
            target_schema = self._control_schema(target_dir / "runtime")

        try:
            with lifecycle_quiescence(runtime_root, timeout=timeout):
                with self._locked():
                    if (
                        _read_pointer(self.root / "current.json") != current
                        or _read_pointer(self.root / "last-known-good.json") != previous
                    ):
                        raise InstallError("installation changed during rollback")
                    try:
                        paths_at_commit = resolve_installed_product_paths(
                            current_dir / "runtime",
                            home=home,
                            environment=environment,
                        )
                    except InstalledProductPathsError as exc:
                        raise InstallError(str(exc)) from exc
                    if paths_at_commit != paths:
                        raise InstallError("product paths changed during rollback")
                    self._verify_installed_version(previous, target_dir)
                    self._health_check(target_dir / "runtime")
                    if self._control_schema(target_dir / "runtime") != target_schema:
                        raise InstallError("rollback target schema changed")
                    state_schema = self._control_state_schema(paths.control_database_file)
                    if state_schema is not None and state_schema > target_schema:
                        raise InstallError(
                            "rollback target cannot read the current control schema"
                        )
                    transaction: dict[str, object] = {
                        "schema_version": 1,
                        "operation": "rollback",
                        "phase": "prepared",
                        "current_before": current,
                        "current_after": previous,
                        "lkg_before": previous,
                        "lkg_after": current,
                        "snapshot": None,
                        "candidate_owned": False,
                        "launchers_owned": False,
                    }
                    try:
                        self._begin_pointer_transaction(transaction)
                        self._publish_pointer_transaction(transaction)
                    except BaseException:
                        try:
                            self._recover_transaction_locked()
                        except BaseException as recovery_exc:
                            raise InstallError(
                                "rollback failed and recovery was incomplete"
                            ) from recovery_exc
                        raise
        except LifecycleError as exc:
            raise InstallError(str(exc)) from exc
        return InstallResult(
            "rolled-back",
            str(previous["release_id"]),
            str(previous["version"]),
        )

    @staticmethod
    def _require_canonical_runtime(
        runtime_root: Path,
        paths: InstalledProductPaths,
    ) -> None:
        selected_runtime = Path(os.path.abspath(os.fspath(runtime_root.expanduser())))
        if selected_runtime != paths.runtime_update_root:
            raise InstallError("operation requires the canonical lifecycle root")

    @staticmethod
    def _control_state_schema(database: Path) -> int | None:
        identity = database.with_name(".control.db.transport.key")
        database_exists = os.path.lexists(database)
        identity_exists = os.path.lexists(identity)
        if database_exists != identity_exists:
            raise InstallError("control state is incomplete")
        if not database_exists:
            return None
        for path in (database, identity):
            try:
                details = path.lstat()
            except OSError as exc:
                raise InstallError("control state is unreadable") from exc
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.geteuid()
            ):
                raise InstallError("control state is unsafe")
        # A plain read-only open of a WAL database creates `-shm` and `-wal` in
        # the user's data directory: reading the state to decide a rollback
        # must not leave two files behind. When the write-ahead log is absent
        # or empty the database file is the whole state, so it can be opened
        # `immutable=1`, which creates nothing. A non-empty log still needs the
        # ordinary open -- refusing there would be a new restriction on
        # rollback, which needs no backup and reads this correctly either way.
        log = database.with_name(database.name + "-wal")
        try:
            complete = not log.exists() or log.stat().st_size == 0
        except OSError:
            complete = False
        suffix = "&immutable=1" if complete else ""
        uri = f"file:{quote(str(database.resolve(strict=True)), safe='/')}?mode=ro{suffix}"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                connection.execute("PRAGMA query_only = ON")
                versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
            finally:
                connection.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise InstallError("control schema is unreadable") from exc
        if not versions or versions != list(range(1, versions[-1] + 1)):
            raise InstallError("control schema is invalid")
        return versions[-1]

    def _closure_node(self, node_executable: Path | None) -> Path | None:
        """Resolve the Node that runs the pinned Web closure analyser.

        An upgrade may reuse the Node already staged by the current generation,
        so fall back to that binary when the caller supplied none. This only
        selects which interpreter executes the pinned analyser; it never relaxes
        what the analyser accepts.
        """

        if node_executable is not None:
            return node_executable
        try:
            current = _read_pointer(self.root / "current.json")
        except InstallError:
            return None
        if current is None:
            return None
        return _owned_staged_node(
            self.root / "versions" / str(current["version"])
        )

    def _verify_installed_version(self, pointer: dict[str, object], version_dir: Path) -> VerifiedBundle:
        if not version_dir.is_dir() or version_dir.is_symlink():
            raise InstallError("installed version is missing or unsafe")
        # A composed generation stages its own Node; the Web closure analyser
        # runs under that exact binary, never under a PATH-discovered one.
        node_executable = _owned_staged_node(version_dir)
        # `pin_tools=False`: this generation was admitted, with its tools pinned,
        # by the `cortex-dist` that installed it. What identifies it now is the
        # pointer below plus the ledger `verify_bundle` re-derives over every
        # file it carries. Pinning it to THIS verifier's tools instead would
        # refuse every installed generation built from a different
        # `distribution/` — which is precisely the generation an upgrade runs
        # against.
        try:
            bundle = verify_bundle(
                version_dir / "bundle",
                node_executable=node_executable,
                pin_tools=False,
            )
        except BundleVerificationError as exc:
            raise InstallError(str(exc)) from exc
        manifest = bundle.manifest
        expected_version = f"{manifest['release_id']}-{bundle.digest[:16]}"
        if (
            bundle.digest != pointer["bundle_digest"]
            or manifest["release_id"] != pointer["release_id"]
            or manifest["release_sequence"] != pointer["release_sequence"]
            or pointer["version"] != expected_version
            or version_dir.name != expected_version
        ):
            raise InstallError("installed version identity does not match pointer")
        if manifest["schema_version"] >= 2:
            try:
                installed = load_generation(version_dir, pin_tools=False)
            except LifecycleError as exc:
                raise InstallError("installed composed generation is unavailable") from exc
            if installed.bundle_digest != bundle.digest:
                raise InstallError("installed generation identity does not match bundle")
        return bundle

    def current_generation(self) -> Path:
        """Return the verified current composed generation."""

        with self.current_generation_binding() as generation:
            return generation

    @contextlib.contextmanager
    def current_generation_binding(self) -> Iterator[Path]:
        """Hold the distribution lock while using the verified current generation."""

        with self._locked():
            current = _read_pointer(self.root / "current.json")
            if current is None:
                raise InstallError("current generation is unavailable")
            version_dir = self.root / "versions" / str(current["version"])
            bundle = self._verify_installed_version(current, version_dir)
            if bundle.manifest["schema_version"] not in {2, 3}:
                raise InstallError("current generation is not composed")
            yield version_dir

    def doctor(
        self,
        *,
        runtime_root: Path | None = None,
        home: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if any(
            os.path.lexists(self.root / name)
            for name in (_TRANSACTION_FILE, _UNINSTALL_FILE, _UNINSTALL_TRASH)
        ):
            return {
                "installed": True,
                "developer_usable": False,
                "ga_ready": False,
                "web_url": None,
                "category": "recovery_required",
            }
        try:
            current = _read_pointer(self.root / "current.json")
        except InstallError:
            return {
                "installed": True,
                "developer_usable": False,
                "ga_ready": False,
                "web_url": None,
                "category": "installation_unhealthy",
            }
        if current is None:
            return {
                "installed": False,
                "developer_usable": False,
                "ga_ready": False,
                "web_url": None,
                "category": "not_installed",
            }
        version_dir = self.root / "versions" / str(current["version"])
        try:
            bundle = self._verify_installed_version(current, version_dir)
        except InstallError:
            return {
                "installed": True,
                "developer_usable": False,
                "ga_ready": False,
                "web_url": None,
                "category": "installation_unhealthy",
            }
        trusted_release = {
            "release_id": bundle.manifest["release_id"],
            "version_digest": bundle.digest,
        }
        try:
            self._health_check(version_dir / "runtime")
            launchers = all((self.root / "bin" / name).is_file() for name in ("cortex", "cortexd"))
            host = host_capabilities(bundle=version_dir / "bundle")
            capabilities = bundle.manifest["capabilities"]
            ga_ready = bool(
                host["ga_ready"]
                and bundle.manifest["dependency_closure"] == "complete"
                and capabilities["sqlite_vec"]
                and capabilities["ocr"]
                and capabilities["hermes_slot"]
            )
            report: dict[str, object] = {
                "installed": True,
                "release_id": current["release_id"],
                "version_digest": current["bundle_digest"],
                "developer_usable": False,
                "ga_ready": ga_ready,
                "web_url": None,
                "category": "lifecycle_unavailable",
                "capabilities": capabilities,
                "host": host,
            }
            stateful_upgrade = self._stateful_upgrade_report(
                version_dir,
                home=home or Path.home(),
                environment=environment,
            )
            if stateful_upgrade is not None:
                report["stateful_upgrade"] = stateful_upgrade
        except (InstallError, OSError, subprocess.SubprocessError):
            return {
                "installed": True,
                **trusted_release,
                "developer_usable": False,
                "ga_ready": False,
                "web_url": None,
                "category": "installation_unhealthy",
            }
        if not launchers:
            report["category"] = "installation_unhealthy"
            return report
        if runtime_root is None or not runtime_root.expanduser().absolute().is_dir():
            report["category"] = "runtime_root_missing"
            return report
        # Same exemption, same reason: `_verify_installed_version` above already
        # bound this generation to the pointer, so the tools pin would only
        # re-measure a previous generation against this verifier's own tools.
        try:
            if environment is None:
                lifecycle = LifecycleManager(
                    version_dir,
                    runtime_root,
                    home=home or Path.home(),
                    pin_tools=False,
                )
            else:
                lifecycle = LifecycleManager(
                    version_dir,
                    runtime_root,
                    home=home or Path.home(),
                    environment=environment,
                    pin_tools=False,
                )
        except (LifecycleError, OSError):
            report["category"] = "generation_unhealthy"
            return report
        try:
            status = lifecycle.status()
        except (LifecycleError, OSError):
            report["category"] = "lifecycle_unhealthy"
            return report
        if (
            status.generation_identity is not None
            and status.generation_identity != lifecycle.generation.identity
        ):
            report["category"] = "wrong_generation"
            return report
        if status.state == "stopped":
            report["category"] = "stopped"
            return report
        if status.state == "stale":
            report["category"] = "lifecycle_unhealthy"
            return report
        if (
            status.state != "running"
            or status.generation_identity != lifecycle.generation.identity
            or type(status.control_port) is not int
            or type(status.web_port) is not int
        ):
            report["category"] = "lifecycle_unhealthy"
            return report
        expected_url = f"http://127.0.0.1:{status.web_port}"
        try:
            # Named rather than implicit: verifying the running generation
            # is a bundle re-hash plus the Web closure analysis, and the
            # old 1 s could not accommodate it on real hardware.
            health = lifecycle.probe_local_front_door(
                timeout=FRONT_DOOR_PROBE_TIMEOUT
            )
        except (LifecycleError, OSError):
            report["category"] = "front_door_unhealthy"
            return report
        failure_categories = {
            "stopped",
            "wrong_generation",
            "generation_unhealthy",
            "claims_unhealthy",
            "front_door_unhealthy",
        }
        if (
            not health.healthy
            or health.category != "healthy"
            or health.web_url != expected_url
        ):
            report["category"] = (
                health.category
                if health.category in failure_categories
                else "front_door_unhealthy"
            )
            return report
        try:
            current_after_probe = _read_pointer(self.root / "current.json")
        except InstallError:
            current_after_probe = None
        if current_after_probe != current:
            report["category"] = "wrong_generation"
            return report
        report["developer_usable"] = True
        report["web_url"] = expected_url
        report["category"] = "healthy"
        return report

    def _finish_uninstall_locked(self, trash: Path) -> None:
        self._remove_transaction_directory(trash, "uninstall trash directory")
        _fsync_directory(self.root)
        (self.root / ".distribution.lock").unlink(missing_ok=True)
        (self.root / ".cortex-distribution-root.json").unlink(missing_ok=True)
        _unlink_durable(self.root / _UNINSTALL_FILE)
        try:
            self.root.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InstallError("distribution root is not empty after uninstall") from exc
        _fsync_directory(self.root.parent)

    def uninstall(
        self,
        *,
        runtime_root: Path,
        home: Path,
        environment: Mapping[str, str] | None = None,
        timeout: float = 10,
    ) -> None:
        with self._locked():
            current = _read_pointer(self.root / "current.json")
            if current is None:
                raise InstallError("uninstall requires a complete installation")
            current_dir = self.root / "versions" / str(current["version"])
            self._verify_installed_version(current, current_dir)
            self._health_check(current_dir / "runtime")
            try:
                paths = resolve_installed_product_paths(
                    current_dir / "runtime",
                    home=home,
                    environment=environment,
                )
            except InstalledProductPathsError as exc:
                raise InstallError(str(exc)) from exc
            self._require_canonical_runtime(runtime_root, paths)

        try:
            with lifecycle_quiescence(runtime_root, timeout=timeout):
                with self._locked():
                    if _read_pointer(self.root / "current.json") != current:
                        raise InstallError("installation changed during uninstall")
                    try:
                        paths_at_commit = resolve_installed_product_paths(
                            current_dir / "runtime",
                            home=home,
                            environment=environment,
                        )
                    except InstalledProductPathsError as exc:
                        raise InstallError(str(exc)) from exc
                    if paths_at_commit != paths:
                        raise InstallError("product paths changed during uninstall")
                    unexpected = {
                        path.name for path in self.root.iterdir()
                    } - _OWNED_TOP_LEVEL
                    if unexpected:
                        raise InstallError("distribution root contains unowned files")
                    trash = self.root / _UNINSTALL_TRASH
                    journal_path = self.root / _UNINSTALL_FILE
                    if os.path.lexists(trash) or os.path.lexists(journal_path):
                        raise InstallError("manual distribution recovery is required")
                    for name in (
                        "bin",
                        "snapshots",
                        "versions",
                        "last-known-good.json",
                        "current.json",
                    ):
                        if (self.root / name).is_symlink():
                            raise InstallError("installer-owned path is a symlink")
                    transaction: dict[str, object] = {
                        "schema_version": 1,
                        "operation": "uninstall",
                        "phase": "prepared",
                        "current_before": current,
                        "lkg_before": _read_pointer(
                            self.root / "last-known-good.json"
                        ),
                    }
                    self._begin_uninstall_transaction(transaction)
                    self._uninstall_checkpoint("prepared")
                    trash.mkdir(mode=0o700)
                    for name in ("bin", "snapshots", "versions"):
                        path = self.root / name
                        if path.exists():
                            os.rename(path, trash / name)
                    _fsync_directory(self.root)
                    _fsync_directory(trash)
                    self._update_uninstall_transaction_phase(transaction, "isolated")
                    self._uninstall_checkpoint("isolated")
                    lkg_path = self.root / "last-known-good.json"
                    if lkg_path.exists():
                        os.rename(lkg_path, trash / lkg_path.name)
                    os.rename(
                        self.root / "current.json",
                        trash / "current.json",
                    )
                    _fsync_directory(self.root)
                    self._update_uninstall_transaction_phase(transaction, "committed")
                    self._uninstall_checkpoint("committed")
                    self._finish_uninstall_locked(trash)
        except LifecycleError as exc:
            raise InstallError(str(exc)) from exc
