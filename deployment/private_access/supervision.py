"""User-scoped private-access plans and macOS service-descriptor staging."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import json
import os
import platform
import plistlib
import pwd
import re
import secrets as stdlib_secrets
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import AccessConfig, ConfigError, config_fingerprint, parse_config
from .secrets import SecretResolutionError, derive_bootstrap_token, resolve_secret

try:
    import fcntl
except ImportError:  # pragma: no cover - supported service host is macOS
    fcntl = None


GATEWAY_LABEL = "ai.cortex.private-access.gateway"
WEB_LABEL = "ai.cortex.web"
MANIFEST_VERSION = 1
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}-[0-9a-f]{16}$")
_SENSITIVE_ARGUMENT = re.compile(
    r"(?:api[-_]?key|credential|password|secret|token)", re.IGNORECASE
)
_WEB_ENVIRONMENT_ALLOWLIST = {
    "HOME",
    "LANG",
    "LC_ALL",
    "NODE_ENV",
    "PATH",
    "TMPDIR",
    "TZ",
}
_OPERATION_HISTORY = ".operation-history"
_UNINSTALL_TOMBSTONES = ".uninstall-tombstones"
_TOMBSTONE_ID = re.compile(r"^[0-9a-f]{32}$")
_OWNED_ROOT_ENTRIES = {
    ".cortex-private-access-service.json",
    ".service.lock",
    _OPERATION_HISTORY,
    _UNINSTALL_TOMBSTONES,
    "current.json",
    "logs",
    "previous.json",
    "transaction.json",
    "versions",
}


class SupervisionError(RuntimeError):
    """Raised when a service lifecycle operation cannot proceed safely."""


@dataclass(frozen=True)
class ServiceSpec:
    config_path: Path
    service_root: Path
    gateway_launcher: Path
    supervisor_launcher: Path
    web_working_directory: Path
    web_command: tuple[str, ...]
    release_id: str
    release_sequence: int


@dataclass(frozen=True)
class ServiceResult:
    action: str
    release_id: str
    release_sequence: int
    version: str
    activation_allowed: bool = False
    cleanup_deferred_to_p2_devrel: bool = True
    launchctl_invoked: bool = False
    validated_scope: str = "access_boundary_only"

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "activation_allowed": self.activation_allowed,
            "cleanup_deferred_to_p2_devrel": self.cleanup_deferred_to_p2_devrel,
            "launchctl_invoked": self.launchctl_invoked,
            "release_id": self.release_id,
            "release_sequence": self.release_sequence,
            "validated_scope": self.validated_scope,
            "version": self.version,
        }


def default_service_root(*, home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Application Support" / "Cortex" / "Private Access"


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _normalized_real_path(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(path.expanduser()))))


def _account_home() -> Path:
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def _stable_user_anchor(path: Path) -> Path:
    candidate = _normalized_real_path(path)
    while candidate.parent != candidate and os.access(candidate.parent, os.W_OK):
        candidate = candidate.parent
    return candidate


def _trusted_service_anchor(path: Path) -> Path:
    candidate = _normalized_real_path(path)
    anchors = (
        _stable_user_anchor(_account_home()),
        _stable_user_anchor(Path(tempfile.gettempdir())),
    )
    for anchor in anchors:
        try:
            candidate.relative_to(anchor)
        except ValueError:
            continue
        return anchor
    raise SupervisionError(
        "service root must be under the account or temporary root"
    )


def _open_service_directory(path: Path, *, label: str, private: bool) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SupervisionError(f"{label} is unsafe") from exc
    details = os.fstat(descriptor)
    forbidden_mode = 0o077 if private else 0o022
    allowed_owners = {os.geteuid()} if private else {0, os.geteuid()}
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid not in allowed_owners
        or stat.S_IMODE(details.st_mode) & forbidden_mode
    ):
        os.close(descriptor)
        raise SupervisionError(f"{label} is unsafe")
    return descriptor


def _verify_service_directory_binding(
    descriptor: int,
    path: Path,
    *,
    label: str,
    private: bool,
) -> None:
    opened = os.fstat(descriptor)
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise SupervisionError(f"{label} pathname changed") from exc
    forbidden_mode = 0o077 if private else 0o022
    allowed_owners = {os.geteuid()} if private else {0, os.geteuid()}
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid not in allowed_owners
        or stat.S_IMODE(opened.st_mode) & forbidden_mode
        or not stat.S_ISDIR(named.st_mode)
        or named.st_uid not in allowed_owners
        or stat.S_IMODE(named.st_mode) & forbidden_mode
        or named.st_dev != opened.st_dev
        or named.st_ino != opened.st_ino
    ):
        raise SupervisionError(f"{label} pathname changed")


def _release_service_flock(descriptor: int) -> None:
    assert fcntl is not None
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _verify_service_anchor_parent(path: Path) -> None:
    if path.parent == path or os.access(path.parent, os.W_OK):
        raise SupervisionError("service lock anchor parent is unsafe")


def _validate_service_root_path(path: Path) -> Path:
    candidate = _normalized_absolute(path)
    candidate_parts = tuple(part.casefold() for part in candidate.parts)
    homes = {_normalized_absolute(Path.home()), _normalized_absolute(_account_home())}
    for home in homes:
        launch_agents = home / "Library" / "LaunchAgents"
        launch_agents_parts = tuple(part.casefold() for part in launch_agents.parts)
        if candidate_parts[: len(launch_agents_parts)] == launch_agents_parts:
            raise SupervisionError("service root must not target ~/Library/LaunchAgents")

    for component in reversed((candidate, *candidate.parents)):
        try:
            details = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SupervisionError("service root ancestor is unreadable") from exc
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise SupervisionError("service root ancestor is unsafe")
        if details.st_uid not in {0, os.geteuid()} or stat.S_IMODE(
            details.st_mode
        ) & 0o022:
            raise SupervisionError("service root ancestor is unsafe")
    return candidate


def _ensure_private_service_root(path: Path) -> None:
    missing: list[Path] = []
    component = path
    while not _exists_no_follow(component):
        missing.append(component)
        if component.parent == component:
            raise SupervisionError("service root has no trusted ancestor")
        component = component.parent
    for component in reversed(missing):
        try:
            os.mkdir(component, 0o700)
        except FileExistsError:
            pass
        try:
            details = component.lstat()
        except OSError as exc:
            raise SupervisionError("service root ancestor is unreadable") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SupervisionError("new service root ancestor is unsafe")
        _sync_directory(component.parent)
    _validate_service_root_path(path)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _config_document(config: AccessConfig) -> dict[str, object]:
    return {
        "access_gateway": config.access_gateway.url,
        "daemon_upstream": config.daemon_upstream.url,
        "identity": {
            "allowed_capability_roles": list(
                config.identity.allowed_capability_roles
            ),
            "allowed_logins": list(config.identity.allowed_logins),
            "allowed_sources": list(config.identity.allowed_sources),
            "app_capability": config.identity.app_capability,
            "service_tag": config.identity.service_tag,
            "tag_owners": list(config.identity.tag_owners),
        },
        "provider": config.provider,
        "public_origin": config.public_origin,
        "schema_version": config.schema_version,
        "session_bootstrap_secret_ref": config.session_bootstrap_secret_ref,
        "web_upstream": config.web_upstream.url,
    }


def _absolute_regular_executable(path: Path, *, name: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise SupervisionError(f"{name} must be an absolute executable path")
    try:
        details = candidate.lstat()
    except OSError as exc:
        raise SupervisionError(f"{name} is unavailable") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or not os.access(candidate, os.X_OK)
    ):
        raise SupervisionError(f"{name} is unsafe")
    return candidate


def _absolute_directory(path: Path, *, name: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise SupervisionError(f"{name} must be an absolute directory")
    try:
        details = candidate.lstat()
    except OSError as exc:
        raise SupervisionError(f"{name} is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != os.geteuid()
    ):
        raise SupervisionError(f"{name} is unsafe")
    return candidate


def _private_config(path: Path) -> AccessConfig:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise SupervisionError("private-access config path must be absolute")
    try:
        descriptor = os.open(
            candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise SupervisionError("private-access config is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise SupervisionError(
                "private-access config must be an owner-only file"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisionError("private-access config could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return parse_config(raw)
    except ConfigError as exc:
        raise SupervisionError(str(exc)) from exc


def _validate_spec(spec: ServiceSpec) -> tuple[ServiceSpec, AccessConfig]:
    if platform.system() != "Darwin" and os.environ.get("CORTEX_TEST_SERVICE_HOST") != "Darwin":
        raise SupervisionError("LaunchAgent supervision is supported only on macOS")
    if not _RELEASE_ID.fullmatch(spec.release_id):
        raise SupervisionError("release ID is invalid")
    if type(spec.release_sequence) is not int or spec.release_sequence < 1:
        raise SupervisionError("release sequence must be a positive integer")
    config = _private_config(spec.config_path)
    _require_supervised_secret_reference(config)
    gateway_launcher = _absolute_regular_executable(
        spec.gateway_launcher, name="gateway launcher"
    )
    supervisor_launcher = _absolute_regular_executable(
        spec.supervisor_launcher, name="supervisor launcher"
    )
    working_directory = _absolute_directory(
        spec.web_working_directory, name="Web working directory"
    )
    if not spec.web_command:
        raise SupervisionError("Web command is required")
    web_executable = _absolute_regular_executable(
        Path(spec.web_command[0]), name="Web executable"
    )
    arguments = (str(web_executable), *spec.web_command[1:])
    for argument in arguments[1:]:
        if (
            not argument
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in argument)
            or _SENSITIVE_ARGUMENT.search(argument)
        ):
            raise SupervisionError("Web command contains an unsafe argument")
    service_root = _validate_service_root_path(spec.service_root)
    if not service_root.is_absolute():
        raise SupervisionError("service root must be absolute")
    return (
        ServiceSpec(
            config_path=spec.config_path.expanduser(),
            service_root=service_root,
            gateway_launcher=gateway_launcher,
            supervisor_launcher=supervisor_launcher,
            web_working_directory=working_directory,
            web_command=arguments,
            release_id=spec.release_id,
            release_sequence=spec.release_sequence,
        ),
        config,
    )


def _require_supervised_secret_reference(config: AccessConfig) -> None:
    if not config.session_bootstrap_secret_ref.startswith("keychain://"):
        raise SupervisionError(
            "supervised private access requires a Keychain secret reference"
        )


def _plist(
    *,
    label: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    working_directory: Path | None,
    log_directory: Path,
) -> bytes:
    document: dict[str, object] = {
        "EnvironmentVariables": dict(environment),
        "KeepAlive": True,
        "Label": label,
        "ProcessType": "Interactive",
        "ProgramArguments": list(arguments),
        "RunAtLoad": True,
        "StandardErrorPath": str(log_directory / f"{label}.stderr.log"),
        "StandardOutPath": str(log_directory / f"{label}.stdout.log"),
    }
    if working_directory is not None:
        document["WorkingDirectory"] = str(working_directory)
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def _generation(spec: ServiceSpec, config: AccessConfig) -> dict[str, bytes]:
    config_bytes = _canonical_json(_config_document(config))
    fingerprint = config_fingerprint(config)
    seed = _canonical_json(
        {
            "config_fingerprint": fingerprint,
            "gateway_launcher": str(spec.gateway_launcher),
            "release_id": spec.release_id,
            "release_sequence": spec.release_sequence,
            "supervisor_launcher": str(spec.supervisor_launcher),
            "web_command": spec.web_command,
            "web_working_directory": str(spec.web_working_directory),
        }
    )
    digest = _sha256(seed)
    version = f"{spec.release_id}-{digest[:16]}"
    version_directory = spec.service_root / "versions" / version
    managed_config = version_directory / "private-access.json"
    environment = {
        "CORTEX_ACCESS_BOOTSTRAP_SECRET_REF": config.session_bootstrap_secret_ref,
        "CORTEX_PRIVATE_ACCESS_CONFIG": str(managed_config),
        "CORTEX_PUBLIC_ORIGIN": config.public_origin,
    }
    logs = spec.service_root / "logs"
    gateway = _plist(
        label=GATEWAY_LABEL,
        arguments=(str(spec.gateway_launcher), "--config", str(managed_config)),
        environment=environment,
        working_directory=None,
        log_directory=logs,
    )
    web = _plist(
        label=WEB_LABEL,
        arguments=(
            str(spec.supervisor_launcher),
            "exec-web",
            "--config",
            str(managed_config),
            "--",
            *spec.web_command,
        ),
        environment=environment,
        working_directory=spec.web_working_directory,
        log_directory=logs,
    )
    files = {
        "private-access.json": config_bytes,
        f"{GATEWAY_LABEL}.plist": gateway,
        f"{WEB_LABEL}.plist": web,
    }
    manifest = {
        "config_fingerprint": fingerprint,
        "files": {name: _sha256(content) for name, content in sorted(files.items())},
        "release_id": spec.release_id,
        "release_sequence": spec.release_sequence,
        "schema_version": MANIFEST_VERSION,
        "version": version,
    }
    files["manifest.json"] = _canonical_json(manifest)
    return files


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SupervisionError("service pointer is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise SupervisionError("service pointer is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisionError("service pointer is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise SupervisionError("service pointer schema is invalid")
    return raw


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if platform.system() == "Darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)
    elif platform.system() == "Linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 0x00000001)
    else:  # pragma: no cover - supported service host is macOS
        raise SupervisionError("atomic no-replace tombstone isolation is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination)
        if error == errno.ENOENT:
            raise FileNotFoundError(error, os.strerror(error), source)
        raise OSError(error, os.strerror(error), source, destination)
    _sync_directory(source.parent)
    if source.parent != destination.parent:
        _sync_directory(destination.parent)


def _rename_at(
    source_descriptor: int,
    source: str,
    destination_descriptor: int,
    destination: str,
    *,
    exchange: bool = False,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    flag = 0x00000002 if exchange else 0x00000004
    if platform.system() == "Darwin":
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_descriptor,
            os.fsencode(source),
            destination_descriptor,
            os.fsencode(destination),
            flag,
        )
    elif platform.system() == "Linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_descriptor,
            os.fsencode(source),
            destination_descriptor,
            os.fsencode(destination),
            0x00000002 if exchange else 0x00000001,
        )
    else:  # pragma: no cover - supported service host is macOS
        raise SupervisionError("atomic service state transition is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination)
        if error == errno.ENOENT:
            raise FileNotFoundError(error, os.strerror(error), source)
        raise OSError(error, os.strerror(error), source, destination)
    os.fsync(source_descriptor)
    if source_descriptor != destination_descriptor:
        os.fsync(destination_descriptor)


def _exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SupervisionError("service path is unreadable") from exc
    return True


def _pointer(manifest: Mapping[str, object]) -> dict[str, object]:
    return {
        "config_fingerprint": manifest["config_fingerprint"],
        "release_id": manifest["release_id"],
        "release_sequence": manifest["release_sequence"],
        "schema_version": 1,
        "version": manifest["version"],
    }


def _validate_pointer(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SupervisionError("service pointer schema is invalid")
    if set(raw) != {
        "config_fingerprint",
        "release_id",
        "release_sequence",
        "schema_version",
        "version",
    }:
        raise SupervisionError("service pointer schema is invalid")
    if (
        raw["schema_version"] != 1
        or not isinstance(raw["config_fingerprint"], str)
        or re.fullmatch(r"[0-9a-f]{64}", raw["config_fingerprint"]) is None
        or not isinstance(raw["release_id"], str)
        or not _RELEASE_ID.fullmatch(raw["release_id"])
        or type(raw["release_sequence"]) is not int
        or raw["release_sequence"] < 1
        or not isinstance(raw["version"], str)
        or not _VERSION.fullmatch(raw["version"])
    ):
        raise SupervisionError("service pointer values are invalid")
    return raw


class ServiceManager:
    """Stage immutable access-only service generations without activation."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = _validate_service_root_path(root or default_service_root())

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise SupervisionError("service locking is unavailable")
        _validate_service_root_path(self.root)
        anchor_path = _trusted_service_anchor(self.root)
        parent_path = self.root.parent
        expected = {"owner": "cortex-private-access-service", "schema_version": 1}
        lock_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        with contextlib.ExitStack() as cleanup:
            anchor_descriptor = _open_service_directory(
                anchor_path, label="service lock anchor", private=False
            )
            cleanup.callback(os.close, anchor_descriptor)
            cleanup.callback(_release_service_flock, anchor_descriptor)
            _verify_service_anchor_parent(anchor_path)
            _verify_service_directory_binding(
                anchor_descriptor,
                anchor_path,
                label="service lock anchor",
                private=False,
            )
            fcntl.flock(anchor_descriptor, fcntl.LOCK_EX)
            _verify_service_directory_binding(
                anchor_descriptor,
                anchor_path,
                label="service lock anchor",
                private=False,
            )
            _verify_service_anchor_parent(anchor_path)
            _ensure_private_service_root(self.root)
            parent_descriptor = _open_service_directory(
                parent_path, label="service root parent", private=False
            )
            cleanup.callback(os.close, parent_descriptor)
            anchor_details = os.fstat(anchor_descriptor)
            parent_details = os.fstat(parent_descriptor)
            if (anchor_details.st_dev, anchor_details.st_ino) != (
                parent_details.st_dev,
                parent_details.st_ino,
            ):
                cleanup.callback(_release_service_flock, parent_descriptor)
                fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
            _verify_service_directory_binding(
                parent_descriptor,
                parent_path,
                label="service root parent",
                private=False,
            )
            root_descriptor = os.open(
                self.root.name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            cleanup.callback(os.close, root_descriptor)
            cleanup.callback(_release_service_flock, root_descriptor)
            self._directory_descriptor(root_descriptor, name="service root")
            self._verify_root_binding(parent_descriptor, root_descriptor)
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
            self._verify_root_binding(parent_descriptor, root_descriptor)
            marker_content = self._read_owned_file_at(
                root_descriptor, ".cortex-private-access-service.json"
            )
            if marker_content is None:
                if os.listdir(root_descriptor):
                    raise SupervisionError(
                        "service root is not empty and has no ownership marker"
                    )
                self._write_owned_file_at(
                    root_descriptor,
                    ".cortex-private-access-service.json",
                    _canonical_json(expected),
                )
            else:
                try:
                    existing = json.loads(marker_content)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise SupervisionError(
                        "service ownership marker is invalid"
                    ) from exc
                if existing != expected:
                    raise SupervisionError("service ownership marker is invalid")
            self._verify_namespace_bindings(
                anchor_descriptor,
                anchor_path,
                parent_descriptor,
                parent_path,
                root_descriptor,
            )
            created = False
            try:
                descriptor = os.open(
                    ".service.lock",
                    lock_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root_descriptor,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    ".service.lock", lock_flags, dir_fd=root_descriptor
                )
            cleanup.callback(os.close, descriptor)
            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(root_descriptor)
            self._verify_lock_binding(root_descriptor, descriptor)
            cleanup.callback(_release_service_flock, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._verify_namespace_bindings(
                anchor_descriptor,
                anchor_path,
                parent_descriptor,
                parent_path,
                root_descriptor,
            )
            self._verify_lock_binding(root_descriptor, descriptor)
            self._recover_transaction()
            try:
                self._verify_namespace_bindings(
                    anchor_descriptor,
                    anchor_path,
                    parent_descriptor,
                    parent_path,
                    root_descriptor,
                )
                yield
            finally:
                self._verify_namespace_bindings(
                    anchor_descriptor,
                    anchor_path,
                    parent_descriptor,
                    parent_path,
                    root_descriptor,
                )
                self._verify_lock_binding(root_descriptor, descriptor)

    def _verify_namespace_bindings(
        self,
        anchor_descriptor: int,
        anchor_path: Path,
        parent_descriptor: int,
        parent_path: Path,
        root_descriptor: int,
    ) -> None:
        _verify_service_directory_binding(
            anchor_descriptor,
            anchor_path,
            label="service lock anchor",
            private=False,
        )
        _verify_service_anchor_parent(anchor_path)
        _verify_service_directory_binding(
            parent_descriptor,
            parent_path,
            label="service root parent",
            private=False,
        )
        self._verify_root_binding(parent_descriptor, root_descriptor)

    def _verify_root_binding(
        self, parent_descriptor: int, root_descriptor: int
    ) -> None:
        opened = os.fstat(root_descriptor)
        try:
            named = os.stat(
                self.root.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SupervisionError("service root pathname changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or not stat.S_ISDIR(named.st_mode)
            or named.st_uid != os.geteuid()
            or stat.S_IMODE(named.st_mode) != 0o700
            or named.st_dev != opened.st_dev
            or named.st_ino != opened.st_ino
        ):
            raise SupervisionError("service root pathname changed")

    @staticmethod
    def _verify_lock_binding(root_descriptor: int, descriptor: int) -> None:
        opened = os.fstat(descriptor)
        try:
            named = os.stat(
                ".service.lock",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SupervisionError("service lock pathname changed") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not stat.S_ISREG(named.st_mode)
            or named.st_uid != os.geteuid()
            or named.st_nlink != 1
            or stat.S_IMODE(named.st_mode) != 0o600
        ):
            raise SupervisionError("service lock is unsafe")
        if named.st_dev != opened.st_dev or named.st_ino != opened.st_ino:
            raise SupervisionError("service lock pathname changed")

    @staticmethod
    def _validate_pointer_transaction(
        transaction: Mapping[str, object],
    ) -> tuple[
        dict[str, object] | None,
        dict[str, object] | None,
        dict[str, object],
        dict[str, object] | None,
        str,
    ]:
        if set(transaction) != {
            "current_after",
            "current_before",
            "operation",
            "previous_after",
            "previous_before",
            "schema_version",
            "transaction_id",
        } or transaction.get("schema_version") != 2 or transaction.get(
            "operation"
        ) not in {
            "install",
            "upgrade",
            "rollback",
        } or not isinstance(
            transaction.get("transaction_id"), str
        ) or _TOMBSTONE_ID.fullmatch(str(transaction["transaction_id"])) is None:
            raise SupervisionError("service transaction journal is invalid")
        current_before = _validate_pointer(transaction["current_before"])
        previous_before = _validate_pointer(transaction["previous_before"])
        current_after = _validate_pointer(transaction["current_after"])
        previous_after = _validate_pointer(transaction["previous_after"])
        if current_after is None:
            raise SupervisionError("service transaction journal is invalid")
        return (
            current_before,
            previous_before,
            current_after,
            previous_after,
            str(transaction["transaction_id"]),
        )

    def _recover_transaction(self) -> None:
        path = self.root / "transaction.json"
        transaction = _read_json(path)
        if transaction is None:
            self._validate_operation_history()
            self._validate_tombstone_store()
            return
        if transaction.get("operation") == "uninstall":
            self._validate_operation_history()
            self._complete_uninstall(transaction)
            return
        (
            current_before,
            previous_before,
            current_after,
            previous_after,
            transaction_id,
        ) = self._validate_pointer_transaction(transaction)
        self._validate_operation_history(pending_id=transaction_id)
        current_now = _validate_pointer(_read_json(self.root / "current.json"))
        previous_now = _validate_pointer(_read_json(self.root / "previous.json"))
        if current_now not in (current_before, current_after):
            raise SupervisionError(
                "current pointer does not belong to the service transaction"
            )
        if previous_now not in (previous_before, previous_after):
            raise SupervisionError(
                "previous pointer does not belong to the service transaction"
            )
        if current_now == current_before and previous_now == previous_before:
            for pointer in (current_before, previous_before):
                if pointer is not None:
                    self._pointer_generation(pointer)
            self._close_pointer_transaction(transaction, outcome="aborted")
            return
        if current_now == current_after and previous_now == previous_after:
            for pointer in (current_after, previous_after):
                if pointer is not None:
                    self._pointer_generation(pointer)
            self._close_pointer_transaction(transaction, outcome="committed")
            return
        raise SupervisionError(
            "service transaction has mixed pointer state; manual recovery is required"
        )

    def _begin_transaction(
        self,
        operation: str,
        *,
        current_before: dict[str, object] | None,
        previous_before: dict[str, object] | None,
        current_after: dict[str, object],
        previous_after: dict[str, object] | None,
    ) -> dict[str, object]:
        transaction = {
            "current_after": current_after,
            "current_before": current_before,
            "operation": operation,
            "previous_after": previous_after,
            "previous_before": previous_before,
            "schema_version": 2,
            "transaction_id": stdlib_secrets.token_hex(16),
        }
        self._write_root_file_no_replace(
            "transaction.json", _canonical_json(transaction)
        )
        return transaction

    @staticmethod
    def _directory_descriptor(descriptor: int, *, name: str) -> None:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SupervisionError(f"{name} directory is unsafe")

    @contextlib.contextmanager
    def _root_directory(self) -> Iterator[int]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            raise SupervisionError("service root is unsafe") from exc
        try:
            self._directory_descriptor(descriptor, name="service root")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _history_directory(self, *, create: bool = False) -> Iterator[int]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with self._root_directory() as root_descriptor:
            if create:
                try:
                    os.mkdir(_OPERATION_HISTORY, 0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise SupervisionError(
                        "operation history could not be created"
                    ) from exc
                else:
                    os.fsync(root_descriptor)
            try:
                descriptor = os.open(
                    _OPERATION_HISTORY, flags, dir_fd=root_descriptor
                )
            except OSError as exc:
                raise SupervisionError("operation history is unsafe") from exc
            try:
                self._directory_descriptor(descriptor, name="operation history")
                yield descriptor
            finally:
                os.close(descriptor)

    @contextlib.contextmanager
    def _transaction_history_directory(
        self, transaction_id: str, *, create: bool = False
    ) -> Iterator[int]:
        if _TOMBSTONE_ID.fullmatch(transaction_id) is None:
            raise SupervisionError("service transaction ID is invalid")
        name = f"transaction-{transaction_id}"
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with self._history_directory(create=create) as history_descriptor:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=history_descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise SupervisionError(
                        "transaction history could not be created"
                    ) from exc
                else:
                    os.fsync(history_descriptor)
            try:
                descriptor = os.open(name, flags, dir_fd=history_descriptor)
            except OSError as exc:
                raise SupervisionError("transaction history is unsafe") from exc
            try:
                self._directory_descriptor(descriptor, name="transaction history")
                yield descriptor
            finally:
                os.close(descriptor)

    @staticmethod
    def _read_owned_file_at(descriptor: int, name: str) -> bytes | None:
        try:
            file_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SupervisionError("owned service file is unreadable") from exc
        try:
            details = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                raise SupervisionError("owned service file is unsafe")
            with os.fdopen(file_descriptor, "rb") as handle:
                file_descriptor = -1
                return handle.read()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    @classmethod
    def _read_pointer_at(
        cls, descriptor: int, name: str
    ) -> dict[str, object] | None:
        content = cls._read_owned_file_at(descriptor, name)
        if content is None:
            return None
        try:
            raw = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SupervisionError("service pointer is unreadable") from exc
        return _validate_pointer(raw)

    @staticmethod
    def _write_owned_file_at(descriptor: int, name: str, content: bytes) -> None:
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=descriptor,
            )
            os.fchmod(file_descriptor, 0o600)
            pending = memoryview(content)
            while pending:
                written = os.write(file_descriptor, pending)
                if written <= 0:
                    raise OSError("owned file write made no progress")
                pending = pending[written:]
            os.fsync(file_descriptor)
            os.fsync(descriptor)
        except OSError as exc:
            raise SupervisionError("owned service file already exists or is unsafe") from exc
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    def _write_root_file_no_replace(self, name: str, content: bytes) -> None:
        with self._root_directory() as descriptor:
            self._write_owned_file_at(descriptor, name, content)

    @staticmethod
    def _pointer_slot_name(pointer_name: str) -> str:
        return f"{pointer_name.removesuffix('.json')}-swap.json"

    def _validate_pointer_history_slots(
        self,
        descriptor: int,
        transaction: Mapping[str, object],
        *,
        outcome: str,
        manifest_name: str | None = None,
    ) -> None:
        (
            current_before,
            previous_before,
            current_after,
            previous_after,
            _,
        ) = self._validate_pointer_transaction(transaction)
        names = set(os.listdir(descriptor))
        if manifest_name is not None:
            names.discard(manifest_name)
        allowed = {
            self._pointer_slot_name("current.json"),
            self._pointer_slot_name("previous.json"),
        }
        if not names.issubset(allowed):
            raise SupervisionError("transaction history contains foreign state")
        for pointer_name, before, after in (
            ("current.json", current_before, current_after),
            ("previous.json", previous_before, previous_after),
        ):
            slot = self._pointer_slot_name(pointer_name)
            retained = self._read_pointer_at(descriptor, slot)
            if before == after:
                if retained is not None:
                    raise SupervisionError(
                        "transaction history pointer state is invalid"
                    )
            elif outcome == "committed":
                if retained != before:
                    raise SupervisionError(
                        "transaction history pointer state is invalid"
                    )
            elif outcome == "aborted":
                if retained not in (None, after):
                    raise SupervisionError(
                        "transaction history pointer state is invalid"
                    )
            else:
                raise SupervisionError("transaction history outcome is invalid")

    def _validate_transaction_history_entry(
        self, descriptor: int, transaction_id: str
    ) -> None:
        names = set(os.listdir(descriptor))
        manifests = names & {
            "aborted-manifest.json",
            "committed-manifest.json",
        }
        if len(manifests) != 1:
            raise SupervisionError("transaction history manifest is unavailable")
        manifest_name = manifests.pop()
        content = self._read_owned_file_at(descriptor, manifest_name)
        if content is None:
            raise SupervisionError("transaction history manifest is unavailable")
        try:
            transaction = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SupervisionError("transaction history manifest is invalid") from exc
        if not isinstance(transaction, dict):
            raise SupervisionError("transaction history manifest is invalid")
        *_, recorded_id = self._validate_pointer_transaction(transaction)
        if recorded_id != transaction_id or content != _canonical_json(transaction):
            raise SupervisionError("transaction history manifest is invalid")
        outcome = manifest_name.removesuffix("-manifest.json")
        self._validate_pointer_history_slots(
            descriptor,
            transaction,
            outcome=outcome,
            manifest_name=manifest_name,
        )

    def _validate_operation_history(self, *, pending_id: str | None = None) -> None:
        history = self.root / _OPERATION_HISTORY
        if not _exists_no_follow(history):
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        generation_names = {
            "manifest.json",
            "private-access.json",
            f"{GATEWAY_LABEL}.plist",
            f"{WEB_LABEL}.plist",
        }
        with self._history_directory() as history_descriptor:
            for name in os.listdir(history_descriptor):
                candidate = re.fullmatch(r"candidate-([0-9a-f]{32})", name)
                transaction = re.fullmatch(r"transaction-([0-9a-f]{32})", name)
                if candidate is None and transaction is None:
                    raise SupervisionError("operation history contains foreign state")
                try:
                    descriptor = os.open(name, flags, dir_fd=history_descriptor)
                except OSError as exc:
                    raise SupervisionError("operation history entry is unsafe") from exc
                try:
                    self._directory_descriptor(
                        descriptor, name="operation history entry"
                    )
                    if candidate is not None:
                        names = set(os.listdir(descriptor))
                        if not names.issubset(generation_names):
                            raise SupervisionError(
                                "operation history contains foreign state"
                            )
                        for filename in names:
                            if self._read_owned_file_at(descriptor, filename) is None:
                                raise SupervisionError(
                                    "operation history entry is unsafe"
                                )
                    elif transaction is not None and transaction.group(1) != pending_id:
                        self._validate_transaction_history_entry(
                            descriptor, transaction.group(1)
                        )
                finally:
                    os.close(descriptor)

    def _advance_pointer_transaction(
        self,
        transaction: Mapping[str, object],
        pointer_name: str,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
    ) -> None:
        *_, transaction_id = self._validate_pointer_transaction(transaction)
        slot = self._pointer_slot_name(pointer_name)
        with self._root_directory() as root_descriptor:  # noqa: SIM117
            with self._transaction_history_directory(
                transaction_id, create=True
            ) as history_descriptor:
                live = self._read_pointer_at(root_descriptor, pointer_name)
                retained = self._read_pointer_at(history_descriptor, slot)
                if before == after:
                    if live != before or retained is not None:
                        raise SupervisionError(
                            "service pointer does not belong to the transaction"
                        )
                    return
                if after is None:
                    raise SupervisionError(
                        "pointer removal is not owned by ordinary transactions"
                    )
                if before is None:
                    if live == after and retained is None:
                        return
                    if live is not None:
                        raise SupervisionError(
                            "service pointer does not belong to the transaction"
                        )
                    if retained is None:
                        self._write_owned_file_at(
                            history_descriptor, slot, _canonical_json(after)
                        )
                    elif retained != after:
                        raise SupervisionError(
                            "transaction history pointer state is invalid"
                        )
                    try:
                        _rename_at(
                            history_descriptor,
                            slot,
                            root_descriptor,
                            pointer_name,
                        )
                    except OSError as exc:
                        raise SupervisionError(
                            "service pointer could not be published"
                        ) from exc
                    if (
                        self._read_pointer_at(root_descriptor, pointer_name) != after
                        or self._read_pointer_at(history_descriptor, slot) is not None
                    ):
                        raise SupervisionError(
                            "service pointer changed during publication"
                        )
                    return
                if live == after and retained == before:
                    return
                if live != before:
                    raise SupervisionError(
                        "service pointer does not belong to the transaction"
                    )
                if retained is None:
                    self._write_owned_file_at(
                        history_descriptor, slot, _canonical_json(after)
                    )
                elif retained != after:
                    raise SupervisionError(
                        "transaction history pointer state is invalid"
                    )
                try:
                    _rename_at(
                        history_descriptor,
                        slot,
                        root_descriptor,
                        pointer_name,
                        exchange=True,
                    )
                except OSError as exc:
                    raise SupervisionError(
                        "service pointer could not be exchanged"
                    ) from exc
                if (
                    self._read_pointer_at(root_descriptor, pointer_name) != after
                    or self._read_pointer_at(history_descriptor, slot) != before
                ):
                    raise SupervisionError(
                        "service pointer changed during exchange"
                    )

    def _close_pointer_transaction(
        self, transaction: Mapping[str, object], *, outcome: str
    ) -> None:
        *_, transaction_id = self._validate_pointer_transaction(transaction)
        manifest_name = f"{outcome}-manifest.json"
        with self._root_directory() as root_descriptor:  # noqa: SIM117
            with self._transaction_history_directory(
                transaction_id, create=True
            ) as history_descriptor:
                self._validate_pointer_history_slots(
                    history_descriptor, transaction, outcome=outcome
                )
                try:
                    _rename_at(
                        root_descriptor,
                        "transaction.json",
                        history_descriptor,
                        manifest_name,
                    )
                except OSError as exc:
                    raise SupervisionError(
                        "service transaction could not be retained"
                    ) from exc
                content = self._read_owned_file_at(
                    history_descriptor, manifest_name
                )
                if content != _canonical_json(transaction):
                    raise SupervisionError(
                        "retained service transaction manifest is invalid"
                    )
                self._validate_transaction_history_entry(
                    history_descriptor, transaction_id
                )

    @contextlib.contextmanager
    def _versions_directory(self, *, create: bool = False) -> Iterator[int]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        try:
            try:
                root_descriptor = os.open(self.root, flags)
                descriptors.append(root_descriptor)
                self._directory_descriptor(root_descriptor, name="service root")
                if create:
                    try:
                        os.mkdir("versions", 0o700, dir_fd=root_descriptor)
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(root_descriptor)
                versions_descriptor = os.open(
                    "versions", flags, dir_fd=root_descriptor
                )
                descriptors.append(versions_descriptor)
                self._directory_descriptor(versions_descriptor, name="versions")
            except OSError as exc:
                raise SupervisionError("versions directory is unsafe") from exc
            yield versions_descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextlib.contextmanager
    def _generation_directory(self, version: str) -> Iterator[int]:
        if not _VERSION.fullmatch(version):
            raise SupervisionError("service version is invalid")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with self._versions_directory() as versions_descriptor:
            try:
                generation_descriptor = os.open(
                    version, flags, dir_fd=versions_descriptor
                )
            except OSError as exc:
                raise SupervisionError("service generation is unavailable") from exc
            try:
                self._directory_descriptor(generation_descriptor, name="generation")
                yield generation_descriptor
            finally:
                os.close(generation_descriptor)

    @staticmethod
    def _write_generation_file(descriptor: int, name: str, content: bytes) -> None:
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=descriptor,
            )
            os.fchmod(file_descriptor, 0o600)
            pending = memoryview(content)
            while pending:
                written = os.write(file_descriptor, pending)
                if written <= 0:
                    raise OSError("generation file write made no progress")
                pending = pending[written:]
            os.fsync(file_descriptor)
        except OSError as exc:
            raise SupervisionError("service generation could not be staged") from exc
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    @staticmethod
    def _rename_generation_no_replace(
        descriptor: int, source: str, destination: str
    ) -> None:
        _rename_at(descriptor, source, descriptor, destination)

    def _publish_generation(
        self, files: Mapping[str, bytes], manifest: Mapping[str, object]
    ) -> None:
        version = str(manifest["version"])
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with self._versions_directory(create=True) as versions_descriptor:
            try:
                existing_descriptor = os.open(
                    version, flags, dir_fd=versions_descriptor
                )
            except FileNotFoundError:
                existing_descriptor = -1
            except OSError as exc:
                raise SupervisionError("service generation is unsafe") from exc
            if existing_descriptor >= 0:
                try:
                    self._directory_descriptor(
                        existing_descriptor, name="generation"
                    )
                finally:
                    os.close(existing_descriptor)
            else:
                candidate_id = stdlib_secrets.token_hex(16)
                candidate_name = f".candidate-{candidate_id}"
                retained_name = f"candidate-{candidate_id}"
                with self._history_directory(create=True) as history_descriptor:
                    os.mkdir(candidate_name, 0o700, dir_fd=versions_descriptor)
                    candidate_descriptor = -1
                    published = False
                    try:
                        candidate_descriptor = os.open(
                            candidate_name, flags, dir_fd=versions_descriptor
                        )
                        self._directory_descriptor(
                            candidate_descriptor, name="generation candidate"
                        )
                        for name, content in files.items():
                            self._write_generation_file(
                                candidate_descriptor, name, content
                            )
                        if set(os.listdir(candidate_descriptor)) != set(files):
                            raise SupervisionError(
                                "service generation contains undeclared files"
                            )
                        os.fsync(candidate_descriptor)
                        self._rename_generation_no_replace(
                            versions_descriptor,
                            candidate_name,
                            version,
                        )
                        published = True
                    finally:
                        if not published:
                            try:
                                _rename_at(
                                    versions_descriptor,
                                    candidate_name,
                                    history_descriptor,
                                    retained_name,
                                )
                            except OSError as exc:
                                raise SupervisionError(
                                    "failed generation candidate could not be retained"
                                ) from exc
                        if candidate_descriptor >= 0:
                            os.close(candidate_descriptor)
        installed = self._generation_manifest(version)
        if installed != manifest:
            raise SupervisionError("service generation does not match the release")

    @staticmethod
    def _generation_file(descriptor: int, name: str) -> bytes:
        try:
            file_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except OSError as exc:
            raise SupervisionError("service generation is unreadable") from exc
        try:
            details = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                raise SupervisionError("service generation integrity check failed")
            with os.fdopen(file_descriptor, "rb") as handle:
                file_descriptor = -1
                return handle.read()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    def _generation_manifest(self, version: str) -> dict[str, object]:
        with self._generation_directory(version) as directory_descriptor:
            manifest_bytes = self._generation_file(
                directory_descriptor, "manifest.json"
            )
            try:
                manifest = json.loads(manifest_bytes)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SupervisionError(
                    "service generation manifest is invalid"
                ) from exc
            if not isinstance(manifest, dict) or set(manifest) != {
                "config_fingerprint",
                "files",
                "release_id",
                "release_sequence",
                "schema_version",
                "version",
            }:
                raise SupervisionError("service generation manifest is invalid")
            if (
                manifest["schema_version"] != MANIFEST_VERSION
                or manifest["version"] != version
                or not isinstance(manifest["release_id"], str)
                or not _RELEASE_ID.fullmatch(manifest["release_id"])
                or type(manifest["release_sequence"]) is not int
                or manifest["release_sequence"] < 1
                or not isinstance(manifest["config_fingerprint"], str)
                or re.fullmatch(
                    r"[0-9a-f]{64}", manifest["config_fingerprint"]
                )
                is None
            ):
                raise SupervisionError("service generation manifest is invalid")
            checksums = manifest["files"]
            if not isinstance(checksums, dict) or set(checksums) != {
                "private-access.json",
                f"{GATEWAY_LABEL}.plist",
                f"{WEB_LABEL}.plist",
            }:
                raise SupervisionError("service generation manifest is invalid")
            if set(os.listdir(directory_descriptor)) != {*checksums, "manifest.json"}:
                raise SupervisionError("service generation contains undeclared files")
            for name, digest in checksums.items():
                if (
                    not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise SupervisionError("service generation manifest is invalid")
                content = self._generation_file(directory_descriptor, name)
                if _sha256(content) != digest:
                    raise SupervisionError("service generation integrity check failed")
        return manifest

    def _pointer_generation(
        self, pointer: Mapping[str, object]
    ) -> dict[str, object]:
        manifest = self._generation_manifest(str(pointer["version"]))
        if any(
            pointer[field] != manifest[field]
            for field in (
                "config_fingerprint",
                "release_id",
                "release_sequence",
                "version",
            )
        ):
            raise SupervisionError("service pointer does not match its generation")
        return manifest

    def _owned_generation_versions(self) -> tuple[str, ...]:
        versions = self.root / "versions"
        if not _exists_no_follow(versions):
            return ()
        with self._versions_directory() as versions_descriptor:
            names = sorted(os.listdir(versions_descriptor))
        result: list[str] = []
        for name in names:
            if not _VERSION.fullmatch(name):
                raise SupervisionError("versions directory contains foreign state")
            self._generation_manifest(name)
            result.append(name)
        return tuple(result)

    def _build_uninstall_transaction(self) -> dict[str, object] | None:
        unexpected = {path.name for path in self.root.iterdir()} - _OWNED_ROOT_ENTRIES
        if unexpected:
            raise SupervisionError("service root contains unowned files")
        self._validate_tombstone_store()
        current = _validate_pointer(_read_json(self.root / "current.json"))
        previous = _validate_pointer(_read_json(self.root / "previous.json"))
        versions = self._owned_generation_versions()
        version_set = set(versions)
        for pointer in (current, previous):
            if pointer is not None and pointer["version"] not in version_set:
                raise SupervisionError("service pointer generation is unavailable")
            if pointer is not None:
                self._pointer_generation(pointer)
        files: dict[str, dict[str, object]] = {}
        directories: dict[str, dict[str, object]] = {}
        for name in ("current.json", "previous.json"):
            path = self.root / name
            if _exists_no_follow(path):
                files[name] = self._file_claim(path)
        if versions:
            directories["versions"] = self._directory_claim(self.root / "versions")
        for version in versions:
            relative_directory = f"versions/{version}"
            directories[relative_directory] = self._directory_claim(
                self.root / relative_directory
            )
            for path in sorted((self.root / relative_directory).iterdir()):
                relative = f"{relative_directory}/{path.name}"
                files[relative] = self._file_claim(path)
        logs = self.root / "logs"
        if _exists_no_follow(logs):
            details = logs.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_uid != os.geteuid()
                or any(logs.iterdir())
            ):
                raise SupervisionError("service logs contain foreign state")
            directories["logs"] = self._directory_claim(logs)
        if not files and not directories:
            return None
        return {
            "cleanup_owner": "p2_devrel_compose",
            "directories": directories,
            "files": files,
            "isolation": "durable_tombstone",
            "operation": "uninstall",
            "schema_version": 3,
            "tombstone_id": stdlib_secrets.token_hex(16),
        }

    @staticmethod
    def _file_claim(path: Path) -> dict[str, object]:
        try:
            details = path.lstat()
            content = path.read_bytes()
        except OSError as exc:
            raise SupervisionError("uninstall file is unreadable") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise SupervisionError("uninstall file is unsafe")
        return {
            "device": details.st_dev,
            "inode": details.st_ino,
            "mode": 0o600,
            "sha256": _sha256(content),
        }

    @staticmethod
    def _directory_claim(path: Path) -> dict[str, object]:
        try:
            details = path.lstat()
        except OSError as exc:
            raise SupervisionError("uninstall directory is unreadable") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SupervisionError("uninstall directory is unsafe")
        return {
            "device": details.st_dev,
            "inode": details.st_ino,
            "mode": 0o700,
        }

    def _validate_uninstall_transaction(
        self, transaction: Mapping[str, object]
    ) -> tuple[
        dict[str, dict[str, object]], dict[str, dict[str, object]], str
    ]:
        if set(transaction) != {
            "cleanup_owner",
            "directories",
            "files",
            "isolation",
            "operation",
            "schema_version",
            "tombstone_id",
        } or transaction.get("schema_version") != 3 or transaction.get(
            "operation"
        ) != "uninstall" or transaction.get(
            "isolation"
        ) != "durable_tombstone" or transaction.get(
            "cleanup_owner"
        ) != "p2_devrel_compose" or not isinstance(
            transaction.get("tombstone_id"), str
        ) or _TOMBSTONE_ID.fullmatch(str(transaction["tombstone_id"])) is None:
            raise SupervisionError("uninstall transaction journal is invalid")
        raw_files = transaction.get("files")
        raw_directories = transaction.get("directories")
        if not isinstance(raw_files, dict) or not isinstance(raw_directories, dict):
            raise SupervisionError("uninstall transaction journal is invalid")
        files: dict[str, dict[str, object]] = {}
        for relative, claim in raw_files.items():
            if (
                not isinstance(relative, str)
                or not self._valid_file_claim(claim)
                or not self._valid_uninstall_file(relative)
            ):
                raise SupervisionError("uninstall transaction journal is invalid")
            files[relative] = dict(claim)
        directories: dict[str, dict[str, object]] = {}
        for relative, claim in raw_directories.items():
            if (
                not isinstance(relative, str)
                or not self._valid_directory_claim(claim)
                or not self._valid_uninstall_directory(relative)
            ):
                raise SupervisionError("uninstall transaction journal is invalid")
            directories[relative] = dict(claim)
        version_directories = {
            relative.removeprefix("versions/")
            for relative in directories
            if relative.startswith("versions/")
        }
        file_versions = {
            relative.split("/")[1]
            for relative in files
            if relative.startswith("versions/")
        }
        if version_directories != file_versions or (
            bool(version_directories) != ("versions" in directories)
        ):
            raise SupervisionError("uninstall transaction journal is invalid")
        for version in version_directories:
            expected = {
                f"versions/{version}/manifest.json",
                f"versions/{version}/private-access.json",
                f"versions/{version}/{GATEWAY_LABEL}.plist",
                f"versions/{version}/{WEB_LABEL}.plist",
            }
            if {path for path in files if path.startswith(f"versions/{version}/")} != expected:
                raise SupervisionError("uninstall transaction journal is invalid")
        return files, directories, str(transaction["tombstone_id"])

    @staticmethod
    def _valid_file_claim(value: object) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == {"device", "inode", "mode", "sha256"}
            and type(value["device"]) is int
            and value["device"] >= 0
            and type(value["inode"]) is int
            and value["inode"] > 0
            and value["mode"] == 0o600
            and isinstance(value["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        )

    @staticmethod
    def _valid_directory_claim(value: object) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == {"device", "inode", "mode"}
            and type(value["device"]) is int
            and value["device"] >= 0
            and type(value["inode"]) is int
            and value["inode"] > 0
            and value["mode"] == 0o700
        )

    @staticmethod
    def _valid_uninstall_file(relative: str) -> bool:
        if relative in {"current.json", "previous.json"}:
            return True
        parts = relative.split("/")
        return (
            len(parts) == 3
            and parts[0] == "versions"
            and _VERSION.fullmatch(parts[1]) is not None
            and parts[2]
            in {
                "manifest.json",
                "private-access.json",
                f"{GATEWAY_LABEL}.plist",
                f"{WEB_LABEL}.plist",
            }
        )

    @staticmethod
    def _valid_uninstall_directory(relative: str) -> bool:
        if relative in {"logs", "versions"}:
            return True
        parts = relative.split("/")
        return (
            len(parts) == 2
            and parts[0] == "versions"
            and _VERSION.fullmatch(parts[1]) is not None
        )

    @staticmethod
    def _tombstone_name(kind: str, relative: str) -> str:
        digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()
        return f"{kind}-{digest}"

    @staticmethod
    def _file_matches_claim(path: Path, claim: Mapping[str, object]) -> bool:
        try:
            details = path.lstat()
            content = path.read_bytes()
        except OSError:
            return False
        return (
            stat.S_ISREG(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and details.st_uid == os.geteuid()
            and details.st_nlink == 1
            and stat.S_IMODE(details.st_mode) == claim["mode"]
            and details.st_dev == claim["device"]
            and details.st_ino == claim["inode"]
            and _sha256(content) == claim["sha256"]
        )

    @staticmethod
    def _directory_matches_claim(path: Path, claim: Mapping[str, object]) -> bool:
        try:
            details = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and details.st_uid == os.geteuid()
            and stat.S_IMODE(details.st_mode) == claim["mode"]
            and details.st_dev == claim["device"]
            and details.st_ino == claim["inode"]
        )

    def _restore_isolated(self, isolated: Path, live: Path) -> None:
        if _exists_no_follow(live):
            return
        try:
            _rename_no_replace(isolated, live)
        except OSError:
            return

    def _validate_tombstone(
        self, path: Path, transaction: Mapping[str, object]
    ) -> None:
        files, directories, tombstone_id = self._validate_uninstall_transaction(
            transaction
        )
        details = path.lstat()
        if (
            path.name != tombstone_id
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SupervisionError("uninstall tombstone is unsafe")
        expected_files = {
            self._tombstone_name("file", relative): claim
            for relative, claim in files.items()
        }
        expected_directories = {
            self._tombstone_name("directory", relative): claim
            for relative, claim in directories.items()
        }
        expected_names = {*expected_files, *expected_directories, "manifest.json"}
        if {entry.name for entry in path.iterdir()} != expected_names:
            raise SupervisionError("uninstall tombstone contains foreign state")
        manifest_path = path / "manifest.json"
        if manifest_path.read_bytes() != _canonical_json(transaction):
            raise SupervisionError("uninstall tombstone manifest is invalid")
        for name, claim in expected_files.items():
            if not self._file_matches_claim(path / name, claim):
                raise SupervisionError("uninstall tombstone contains foreign state")
        for name, claim in expected_directories.items():
            candidate = path / name
            if not self._directory_matches_claim(candidate, claim) or any(
                candidate.iterdir()
            ):
                raise SupervisionError("uninstall tombstone contains foreign state")

    def _validate_tombstone_store(self, *, pending_id: str | None = None) -> None:
        store = self.root / _UNINSTALL_TOMBSTONES
        if not _exists_no_follow(store):
            return
        details = store.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SupervisionError("uninstall tombstone store is unsafe")
        for tombstone in store.iterdir():
            if _TOMBSTONE_ID.fullmatch(tombstone.name) is None:
                raise SupervisionError(
                    "uninstall tombstone store contains foreign state"
                )
            details = tombstone.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o700
            ):
                raise SupervisionError("uninstall tombstone is unsafe")
            if tombstone.name == pending_id:
                continue
            manifest = _read_json(tombstone / "manifest.json")
            if manifest is None:
                raise SupervisionError("uninstall tombstone manifest is unavailable")
            self._validate_tombstone(tombstone, manifest)

    def _complete_uninstall(self, transaction: Mapping[str, object]) -> None:
        files, directories, tombstone_id = self._validate_uninstall_transaction(
            transaction
        )
        unexpected = {path.name for path in self.root.iterdir()} - _OWNED_ROOT_ENTRIES
        if unexpected:
            raise SupervisionError("service root contains unowned files")
        store = self.root / _UNINSTALL_TOMBSTONES
        if not _exists_no_follow(store):
            os.mkdir(store, 0o700)
            _sync_directory(self.root)
        self._validate_tombstone_store(pending_id=tombstone_id)
        tombstone = store / tombstone_id
        if not _exists_no_follow(tombstone):
            os.mkdir(tombstone, 0o700)
            _sync_directory(store)
        details = tombstone.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SupervisionError("uninstall tombstone is unsafe")
        expected_tombstone_files = {
            self._tombstone_name("file", relative): (relative, claim)
            for relative, claim in files.items()
        }
        expected_tombstone_directories = {
            self._tombstone_name("directory", relative): (relative, claim)
            for relative, claim in directories.items()
        }
        expected_tombstone_names = {
            *expected_tombstone_files,
            *expected_tombstone_directories,
        }
        if {path.name for path in tombstone.iterdir()} - expected_tombstone_names:
            raise SupervisionError("uninstall tombstone contains foreign state")
        actual_files: set[str] = set()
        actual_directories: set[str] = set()
        for name in ("current.json", "previous.json"):
            if _exists_no_follow(self.root / name):
                actual_files.add(name)
        versions = self.root / "versions"
        if _exists_no_follow(versions):
            details = versions.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o700
            ):
                raise SupervisionError("uninstall target contains foreign state")
            actual_directories.add("versions")
            for version_path in versions.iterdir():
                version_relative = f"versions/{version_path.name}"
                details = version_path.lstat()
                if (
                    not stat.S_ISDIR(details.st_mode)
                    or stat.S_ISLNK(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) != 0o700
                ):
                    raise SupervisionError("uninstall target contains foreign state")
                actual_directories.add(version_relative)
                for path in version_path.iterdir():
                    relative = f"{version_relative}/{path.name}"
                    details = path.lstat()
                    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                        raise SupervisionError("uninstall target contains foreign state")
                    actual_files.add(relative)
        logs = self.root / "logs"
        if _exists_no_follow(logs):
            details = logs.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o700
                or any(logs.iterdir())
            ):
                raise SupervisionError("uninstall target contains foreign state")
            actual_directories.add("logs")
        if not actual_files.issubset(files) or not actual_directories.issubset(
            directories
        ):
            raise SupervisionError("uninstall target does not belong to the journal")
        for relative in sorted(actual_files):
            path = self.root / relative
            if not self._file_matches_claim(path, files[relative]):
                raise SupervisionError("uninstall target does not belong to the journal")
        for relative in sorted(actual_directories):
            if not self._directory_matches_claim(
                self.root / relative, directories[relative]
            ):
                raise SupervisionError("uninstall target does not belong to the journal")
        for name, (_, claim) in sorted(expected_tombstone_files.items()):
            path = tombstone / name
            if _exists_no_follow(path) and not self._file_matches_claim(path, claim):
                raise SupervisionError("uninstall tombstone contains foreign state")
        for name, (_, claim) in sorted(expected_tombstone_directories.items()):
            path = tombstone / name
            if _exists_no_follow(path) and (
                not self._directory_matches_claim(path, claim) or any(path.iterdir())
            ):
                raise SupervisionError("uninstall tombstone contains foreign state")

        for relative, claim in sorted(files.items()):
            live = self.root / relative
            isolated = tombstone / self._tombstone_name("file", relative)
            if _exists_no_follow(live) and _exists_no_follow(isolated):
                raise SupervisionError("uninstall target exists in two locations")
            if _exists_no_follow(live):
                try:
                    _rename_no_replace(live, isolated)
                except OSError as exc:
                    raise SupervisionError("uninstall target could not be isolated") from exc
                if not self._file_matches_claim(isolated, claim):
                    self._restore_isolated(isolated, live)
                    raise SupervisionError(
                        "uninstall target does not belong to the journal"
                    )
            elif _exists_no_follow(isolated) and not self._file_matches_claim(
                isolated, claim
            ):
                raise SupervisionError("uninstall tombstone contains foreign state")

        for relative, claim in sorted(
            directories.items(),
            key=lambda item: item[0].count("/"),
            reverse=True,
        ):
            live = self.root / relative
            isolated = tombstone / self._tombstone_name("directory", relative)
            if _exists_no_follow(live) and _exists_no_follow(isolated):
                raise SupervisionError("uninstall target exists in two locations")
            if _exists_no_follow(live):
                if any(live.iterdir()):
                    raise SupervisionError("uninstall target contains foreign state")
                try:
                    _rename_no_replace(live, isolated)
                except OSError as exc:
                    raise SupervisionError("uninstall target could not be isolated") from exc
                if (
                    not self._directory_matches_claim(isolated, claim)
                    or any(isolated.iterdir())
                ):
                    self._restore_isolated(isolated, live)
                    raise SupervisionError("uninstall target contains foreign state")
            elif _exists_no_follow(isolated) and (
                not self._directory_matches_claim(isolated, claim)
                or any(isolated.iterdir())
            ):
                raise SupervisionError("uninstall tombstone contains foreign state")

        for relative in files:
            if _exists_no_follow(self.root / relative):
                raise SupervisionError("uninstall target changed during isolation")
        for relative in directories:
            if _exists_no_follow(self.root / relative):
                raise SupervisionError("uninstall target changed during isolation")
        if {path.name for path in tombstone.iterdir()} != expected_tombstone_names:
            raise SupervisionError("uninstall tombstone contains foreign state")
        for name, (_, claim) in sorted(expected_tombstone_files.items()):
            if not self._file_matches_claim(tombstone / name, claim):
                raise SupervisionError("uninstall tombstone contains foreign state")
        for name, (_, claim) in sorted(expected_tombstone_directories.items()):
            path = tombstone / name
            if not self._directory_matches_claim(path, claim) or any(path.iterdir()):
                raise SupervisionError("uninstall tombstone contains foreign state")

        journal = self.root / "transaction.json"
        manifest_path = tombstone / "manifest.json"
        expected_manifest = _canonical_json(transaction)
        if not _exists_no_follow(journal) or journal.read_bytes() != expected_manifest:
            raise SupervisionError("uninstall transaction journal changed")
        try:
            _rename_no_replace(journal, manifest_path)
        except OSError as exc:
            raise SupervisionError("uninstall manifest could not be closed") from exc
        if manifest_path.read_bytes() != expected_manifest:
            self._restore_isolated(manifest_path, journal)
            raise SupervisionError("uninstall tombstone manifest is invalid")
        self._validate_tombstone(tombstone, transaction)

    def plan(self, spec: ServiceSpec) -> dict[str, object]:
        normalized, config = _validate_spec(spec)
        files = _generation(normalized, config)
        manifest = json.loads(files["manifest.json"])
        return {
            "activation": "forbidden_until_p2_devrel_compose",
            "activation_allowed": False,
            "cleanup_deferred_to_p2_devrel": True,
            "config_fingerprint": manifest["config_fingerprint"],
            "cortex_control_ready": False,
            "environment": {
                "CORTEX_ACCESS_BOOTSTRAP_SECRET_REF": "<external-reference>",
                "CORTEX_PRIVATE_ACCESS_CONFIG": "<managed-owner-only-config>",
                "CORTEX_PUBLIC_ORIGIN": config.public_origin,
            },
            "launch_agent_descriptors_staged": [
                f"{GATEWAY_LABEL}.plist",
                f"{WEB_LABEL}.plist",
            ],
            "launch_agents_published": False,
            "launchctl_invoked": False,
            "release_id": normalized.release_id,
            "release_sequence": normalized.release_sequence,
            "resolved_secret_persisted": False,
            "validated_scope": "access_boundary_only",
            "version": manifest["version"],
        }

    def install(self, spec: ServiceSpec, *, upgrade: bool = False, dry_run: bool = False) -> ServiceResult:
        normalized, config = _validate_spec(spec)
        if normalized.service_root.absolute() != self.root:
            raise SupervisionError("service manager roots do not match the install specification")
        files = _generation(normalized, config)
        manifest = json.loads(files["manifest.json"])
        result = ServiceResult(
            action="would-upgrade" if upgrade else "would-install",
            release_id=normalized.release_id,
            release_sequence=normalized.release_sequence,
            version=manifest["version"],
        )
        if dry_run:
            return result
        with self._locked():
            current = _validate_pointer(_read_json(self.root / "current.json"))
            previous = _validate_pointer(_read_json(self.root / "previous.json"))
            for pointer in (current, previous):
                if pointer is not None:
                    self._pointer_generation(pointer)
            if upgrade and current is None:
                raise SupervisionError("upgrade requires an installed service")
            if not upgrade and current is not None and current["version"] != manifest["version"]:
                raise SupervisionError("a different service is installed; use upgrade")
            if current is not None:
                if current["version"] == manifest["version"]:
                    return ServiceResult(
                        "unchanged",
                        normalized.release_id,
                        normalized.release_sequence,
                        str(manifest["version"]),
                    )
                if normalized.release_sequence <= int(current["release_sequence"]):
                    raise SupervisionError("upgrade release sequence must increase")
            self._publish_generation(files, manifest)

            target = _pointer(manifest)
            previous_after = current if current is not None else previous
            transaction = self._begin_transaction(
                "upgrade" if current else "install",
                current_before=current,
                previous_before=previous,
                current_after=target,
                previous_after=previous_after,
            )
            try:
                self._advance_pointer_transaction(
                    transaction, "previous.json", previous, previous_after
                )
                self._advance_pointer_transaction(
                    transaction, "current.json", current, target
                )
                self._close_pointer_transaction(
                    transaction, outcome="committed"
                )
            except BaseException:
                self._recover_transaction()
                raise
            return ServiceResult(
                "upgraded" if current else "installed",
                normalized.release_id,
                normalized.release_sequence,
                str(manifest["version"]),
            )

    def rollback(self, *, dry_run: bool = False) -> ServiceResult:
        current = _validate_pointer(_read_json(self.root / "current.json"))
        previous = _validate_pointer(_read_json(self.root / "previous.json"))
        if current is None or previous is None:
            raise SupervisionError("rollback generation is unavailable")
        self._pointer_generation(current)
        self._pointer_generation(previous)
        result = ServiceResult(
            "would-rollback" if dry_run else "rolled-back",
            str(previous["release_id"]),
            int(previous["release_sequence"]),
            str(previous["version"]),
        )
        if dry_run:
            return result
        with self._locked():
            current = _validate_pointer(_read_json(self.root / "current.json"))
            previous = _validate_pointer(_read_json(self.root / "previous.json"))
            if current is None or previous is None:
                raise SupervisionError("rollback generation is unavailable")
            self._pointer_generation(current)
            self._pointer_generation(previous)
            transaction = self._begin_transaction(
                "rollback",
                current_before=current,
                previous_before=previous,
                current_after=previous,
                previous_after=current,
            )
            try:
                self._advance_pointer_transaction(
                    transaction, "previous.json", previous, current
                )
                self._advance_pointer_transaction(
                    transaction, "current.json", current, previous
                )
                self._close_pointer_transaction(
                    transaction, outcome="committed"
                )
            except BaseException:
                self._recover_transaction()
                raise
            return result

    def uninstall(self, *, dry_run: bool = False) -> dict[str, object]:
        if dry_run:
            return {
                "action": "would-uninstall",
                "activation_allowed": False,
                "cleanup_deferred_to_p2_devrel": True,
                "launchctl_invoked": False,
                "validated_scope": "access_boundary_only",
            }
        with self._locked():
            transaction = self._build_uninstall_transaction()
            if transaction is None:
                action = "unchanged"
            else:
                self._write_root_file_no_replace(
                    "transaction.json", _canonical_json(transaction)
                )
                self._complete_uninstall(transaction)
                action = "uninstalled"
        return {
            "action": action,
            "activation_allowed": False,
            "cleanup_deferred_to_p2_devrel": True,
            "launchctl_invoked": False,
            "validated_scope": "access_boundary_only",
        }


def exec_web(
    config_path: Path,
    command: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    resolver: Callable[..., bytes] = resolve_secret,
    execute: Callable[[str, Sequence[str], Mapping[str, str]], object] = os.execve,
) -> object:
    """Resolve the external bootstrap reference in memory and replace this process."""
    if not command:
        raise SupervisionError("Web command is required")
    config = _private_config(config_path)
    _require_supervised_secret_reference(config)
    executable = _absolute_regular_executable(Path(command[0]), name="Web executable")
    for argument in command[1:]:
        if _SENSITIVE_ARGUMENT.search(argument):
            raise SupervisionError("Web command contains an unsafe argument")
    secret = resolver(config.session_bootstrap_secret_ref)
    source_environment = os.environ if environ is None else environ
    child_environment = {
        name: source_environment[name]
        for name in _WEB_ENVIRONMENT_ALLOWLIST
        if name in source_environment
    }
    child_environment.update(
        {
            "CORTEX_ACCESS_BOOTSTRAP_TOKEN": derive_bootstrap_token(secret),
            "CORTEX_PUBLIC_ORIGIN": config.public_origin,
        }
    )
    return execute(str(executable), (str(executable), *command[1:]), child_environment)


def _add_spec_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--service-root", type=Path, default=default_service_root())
    parser.add_argument("--gateway-launcher", type=Path, required=True)
    parser.add_argument("--supervisor-launcher", type=Path, required=True)
    parser.add_argument("--web-working-directory", type=Path, required=True)
    parser.add_argument("--web-executable", type=Path, required=True)
    parser.add_argument("--web-argument", action="append", default=[])
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-sequence", type=int, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex-private-access-supervisor")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "install", "upgrade"):
        command = commands.add_parser(name)
        _add_spec_arguments(command)
        if name != "plan":
            command.add_argument("--dry-run", action="store_true")
    for name in ("rollback", "uninstall"):
        command = commands.add_parser(name)
        command.add_argument("--service-root", type=Path, default=default_service_root())
        command.add_argument("--dry-run", action="store_true")
    web = commands.add_parser("exec-web")
    web.add_argument("--config", type=Path, required=True)
    web.add_argument("web_command", nargs=argparse.REMAINDER)
    return parser


def _spec(args: argparse.Namespace) -> ServiceSpec:
    return ServiceSpec(
        config_path=args.config,
        service_root=args.service_root,
        gateway_launcher=args.gateway_launcher,
        supervisor_launcher=args.supervisor_launcher,
        web_working_directory=args.web_working_directory,
        web_command=(str(args.web_executable), *args.web_argument),
        release_id=args.release_id,
        release_sequence=args.release_sequence,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "exec-web":
            command = args.web_command
            if command and command[0] == "--":
                command = command[1:]
            exec_web(args.config, command)
            return 0
        manager = ServiceManager(args.service_root)
        if args.command == "plan":
            payload: object = manager.plan(_spec(args))
        elif args.command in {"install", "upgrade"}:
            payload = manager.install(
                _spec(args), upgrade=args.command == "upgrade", dry_run=args.dry_run
            ).to_dict()
        elif args.command == "rollback":
            payload = manager.rollback(dry_run=args.dry_run).to_dict()
        else:
            payload = manager.uninstall(dry_run=args.dry_run)
    except (OSError, SecretResolutionError, SupervisionError) as exc:
        print(json.dumps({"error": str(exc), "ok": False}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "result": payload}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
