"""Generation-bound foreground lifecycle for an installed Cortex release."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import math
import os
import plistlib
import queue
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping

from .bundle import BundleVerificationError, VerifiedBundle, verify_bundle
from .product_manifest import ProductManifestError, validate_product_manifest
from .product_paths import (
    InstalledProductPaths,
    InstalledProductPathsError,
    resolve_installed_product_paths,
)
from .schema import canonical_json_bytes
from .web_settings import (
    EPHEMERAL,
    InstalledWebSettings,
    InstalledWebSettingsError,
    resolve_installed_web_settings,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - R0 hosts are macOS and Linux
    fcntl = None


_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_CLAIM = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43,256}$")
_WEB_BOOTSTRAP_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_STATE_FIELDS = {
    "children",
    "generation_identity",
    "generation_root",
    "run_id",
    "schema_version",
    "supervisor",
}
_SUPERVISOR_FIELDS = {"claim", "pid", "start_token"}
_CONTROL_FIELDS = {"claim", "host", "instance_id", "pid", "port", "start_token"}
_WEB_FIELDS = {"build_id", "claim", "host", "pid", "port", "start_token"}
_SHUTDOWN_REQUEST_FIELDS = {"claim", "deadline", "operation", "schema_version"}
_SHUTDOWN_RESPONSE_FIELDS = {"accepted", "claim", "schema_version"}
_SUPERVISOR_CLEANUP_TIMEOUT = 5.0
_FOREGROUND_CLEANUP_TIMEOUT = 2.0
_STATUS_TIMEOUT = 2.0
_MACOS_SYSTEM_ANCHORS = {
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


class LifecycleError(RuntimeError):
    """A lifecycle operation could not prove ownership of its effects."""


def _normalize_macos_system_anchor(path: Path) -> Path:
    if sys.platform != "darwin":
        return path
    for alias, target in _MACOS_SYSTEM_ANCHORS.items():
        try:
            suffix = path.relative_to(alias)
        except ValueError:
            continue
        alias_details = alias.lstat()
        target_details = target.lstat()
        if (
            stat.S_ISLNK(alias_details.st_mode)
            and alias_details.st_uid == 0
            and stat.S_ISDIR(target_details.st_mode)
            and not stat.S_ISLNK(target_details.st_mode)
            and target_details.st_uid == 0
            and alias.resolve(strict=True) == target
        ):
            return target / suffix
    return path


def _normalized_absolute(path: Path) -> Path:
    try:
        value = os.fspath(path.expanduser())
        if "\0" in value:
            raise ValueError("embedded null character")
        absolute = Path(os.path.abspath(value))
        return _normalize_macos_system_anchor(absolute)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LifecycleError("generation path is invalid") from exc


def _validate_generation_root(path: Path) -> Path:
    root = _normalized_absolute(path)
    try:
        resolved = root.resolve(strict=True)
        details = root.lstat()
    except OSError as exc:
        raise LifecycleError("generation root is unavailable") from exc
    if (
        resolved != root
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise LifecycleError("generation root is unsafe")
    return root


def _safe_component(root: Path, relative: Path, *, directory: bool) -> Path:
    current = root
    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise LifecycleError("generation path is unsafe")
        current = current / component
        try:
            details = current.lstat()
        except OSError as exc:
            raise LifecycleError("generation path is unavailable") from exc
        expected = stat.S_ISDIR(details.st_mode) if current != root / relative else (
            stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)
        )
        if (
            not expected
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or (stat.S_ISDIR(details.st_mode) and stat.S_IMODE(details.st_mode) & 0o022)
        ):
            raise LifecycleError("generation path is unsafe")
    return current


def _opened_digest(path: Path, *, executable: bool = False) -> str:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise LifecycleError("generation file is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o022
            or (executable and details.st_mode & 0o111 == 0)
            or details.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        ):
            raise LifecycleError("generation file is unsafe")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _load_product_manifest(root: Path) -> tuple[dict[str, object], str]:
    path = _safe_component(root, Path("product-manifest.json"), directory=False)
    digest = _opened_digest(path)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise LifecycleError("product manifest is oversized")
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = validate_product_manifest(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProductManifestError) as exc:
        raise LifecycleError("product manifest is invalid") from exc
    return manifest, digest


def _verify_bundle(root: Path, *, pin_tools: bool = True) -> VerifiedBundle:
    bundle_root = _safe_component(root, Path("bundle"), directory=True)
    # The Web closure analyser runs under the generation's own staged Node.
    node_executable = _safe_component(root, Path("node-runtime/bin/node"), directory=False)
    try:
        bundle = verify_bundle(
            bundle_root, node_executable=node_executable, pin_tools=pin_tools
        )
    except BundleVerificationError as exc:
        raise LifecycleError(str(exc)) from exc
    if bundle.manifest.get("schema_version") not in {2, 3}:
        raise LifecycleError("installed generation requires a composed bundle")
    return bundle


def _relative_regular_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        for item in sorted(root.rglob("*")):
            details = item.lstat()
            if stat.S_ISLNK(details.st_mode):
                raise LifecycleError("Web payload copy is unsafe")
            if stat.S_ISDIR(details.st_mode):
                continue
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise LifecycleError("Web payload copy is unsafe")
            files[item.relative_to(root).as_posix()] = _opened_digest(item)
    except OSError as exc:
        raise LifecycleError("Web payload copy is unreadable") from exc
    return files


def _verify_web_copy(root: Path, bundle: VerifiedBundle) -> tuple[Path, str]:
    installed = _safe_component(root, Path("web"), directory=True)
    bundled = bundle.path / "artifacts" / "web"
    if _relative_regular_files(installed) != _relative_regular_files(bundled):
        raise LifecycleError("Web payload copy does not match the verified bundle")
    manifest = bundle.manifest["web_payload"]
    if not isinstance(manifest, dict) or not isinstance(manifest.get("release_build_id"), str):
        raise LifecycleError("verified bundle Web identity is invalid")
    return installed, manifest["release_build_id"]


@dataclass(frozen=True)
class InstalledGeneration:
    """Validated executable and payload paths for one immutable generation."""

    root: Path
    identity: str
    bundle_digest: str
    release_build_id: str
    control_executable: Path
    node_executable: Path
    web_adapter: Path
    web_root: Path
    private_access_enabled: bool

    def control_environment(self, *, home: Path) -> dict[str, str]:
        return {
            "HOME": str(_normalized_absolute(home)),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(self.control_executable.parent),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }

    def web_environment(
        self,
        *,
        control_port: int,
        control_token: str,
        bootstrap_token: str,
        web: InstalledWebSettings = EPHEMERAL,
    ) -> dict[str, str]:
        if (
            type(control_port) is not int
            or not 0 < control_port < 65_536
            or _CONTROL_TOKEN.fullmatch(control_token) is None
            or _WEB_BOOTSTRAP_TOKEN.fullmatch(bootstrap_token) is None
        ):
            raise LifecycleError("Web gateway configuration is invalid")
        environment = {
            "CORTEX_ACCESS_BOOTSTRAP_TOKEN": bootstrap_token,
            "CORTEX_CONTROL_API_URL": f"http://127.0.0.1:{control_port}",
            "CORTEX_CONTROL_TOKEN": control_token,
            "CORTEX_LOCAL_ACCESS_ENABLED": "1",
            "CORTEX_WEB_BUILD_ID": self.release_build_id,
            "CORTEX_WEB_LISTEN_HOST": "127.0.0.1",
            # ⟦P7⟧ `[web] port` fixes the loopback listener so a tunnel ingress
            # has a stable target; absent, the adapter picks one per start as
            # it always has. The public door travels as three public
            # identifiers the adapter re-validates; the signing keys it needs
            # come from the issuer, never from this environment.
            "CORTEX_WEB_LISTEN_PORT": "0" if web.port is None else str(web.port),
            "HOME": "",
            "LANG": "C",
            "LC_ALL": "C",
            "NODE_OPTIONS": "",
            "NODE_PATH": "",
            "PATH": str(self.node_executable.parent),
        }
        if web.public_door:
            environment["CORTEX_PUBLIC_ORIGIN"] = str(web.public_origin)
            environment["CORTEX_ACCESS_ISSUER"] = str(web.access_issuer)
            environment["CORTEX_ACCESS_AUDIENCE"] = str(web.access_audience)
        return environment


def load_generation(path: Path, *, pin_tools: bool = True) -> InstalledGeneration:
    """Verify one installed generation before any child execution.

    `pin_tools=False` is for a generation the caller has already anchored to a
    distribution pointer; an explicitly supplied generation path has no such
    anchor and keeps the default.
    """

    root = _validate_generation_root(path)
    manifest, manifest_digest = _load_product_manifest(root)
    bundle = _verify_bundle(root, pin_tools=pin_tools)
    web_root, release_build_id = _verify_web_copy(root, bundle)
    control = _safe_component(root, Path("runtime/bin/cortexd"), directory=False)
    node = _safe_component(root, Path("node-runtime/bin/node"), directory=False)
    adapter = _safe_component(root, Path("web/server/node-adapter.mjs"), directory=False)
    control_digest = _opened_digest(control, executable=True)
    node_digest = _opened_digest(node, executable=True)
    adapter_digest = _opened_digest(adapter)
    node_manifest = manifest["node_runtime"]
    if not isinstance(node_manifest, dict) or node_manifest.get("executable_sha256") != node_digest:
        raise LifecycleError("Node runtime digest does not match the product manifest")
    processes = manifest["processes"]
    if not isinstance(processes, dict) or not isinstance(processes.get("private_access"), dict):
        raise LifecycleError("product process manifest is invalid")
    identity_processes: dict[str, object] = {
        "control": control_digest,
        "node": node_digest,
        "web_adapter": adapter_digest,
    }
    if manifest["schema_version"] >= 2:
        # A generation that carries its own interpreter binds it exactly as the
        # Node runtime is bound, and additionally proves the virtual environment
        # runs that same interpreter rather than one borrowed from the host.
        # `>= 2`, not `== 2`: every composed schema carries a `python_runtime`,
        # and pinning the branch to one version number meant the next one
        # silently stopped binding the interpreter at all. Schema 1 is the only
        # version without one, so an already-published legacy generation still
        # keeps its identity byte for byte.
        python_manifest = manifest["python_runtime"]
        interpreter = _safe_component(
            root, Path("python-runtime/bin/python3.14"), directory=False
        )
        python_digest = _opened_digest(interpreter, executable=True)
        venv_interpreter = _safe_component(root, Path("runtime/bin/python"), directory=False)
        venv_digest = _opened_digest(venv_interpreter, executable=True)
        if (
            not isinstance(python_manifest, dict)
            or python_manifest.get("interpreter_sha256") != python_digest
            or python_manifest.get("venv_interpreter_sha256") != venv_digest
            or venv_digest != python_digest
        ):
            raise LifecycleError("Python runtime digest does not match the product manifest")
        identity_processes["python"] = python_digest
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_digest": bundle.digest,
                "product_manifest_sha256": manifest_digest,
                "processes": identity_processes,
            }
        )
    ).hexdigest()
    return InstalledGeneration(
        root=root,
        identity=identity,
        bundle_digest=bundle.digest,
        release_build_id=release_build_id,
        control_executable=control,
        node_executable=node,
        web_adapter=adapter,
        web_root=web_root,
        private_access_enabled=bool(processes["private_access"]["enabled_by_default"]),
    )


@dataclass(frozen=True)
class ProcessClaim:
    pid: int
    start_token: str
    claim: str

    def as_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "start_token": self.start_token,
            "claim": self.claim,
        }


@dataclass(frozen=True)
class ChildClaim(ProcessClaim):
    host: str
    port: int


@dataclass(frozen=True)
class ControlClaim(ChildClaim):
    instance_id: str

    def as_json(self) -> dict[str, object]:
        return {
            **super().as_json(),
            "host": self.host,
            "port": self.port,
            "instance_id": self.instance_id,
        }


@dataclass(frozen=True)
class ControlReady:
    claim: ControlClaim
    control_token: str = field(repr=False)


@dataclass(frozen=True)
class WebClaim(ChildClaim):
    build_id: str

    def as_json(self) -> dict[str, object]:
        return {
            **super().as_json(),
            "host": self.host,
            "port": self.port,
            "build_id": self.build_id,
        }


@dataclass(frozen=True)
class LifecycleRecord:
    generation_identity: str
    generation_root: str
    run_id: str
    supervisor: ProcessClaim
    control: ControlClaim
    web: WebClaim

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generation_identity": self.generation_identity,
            "generation_root": self.generation_root,
            "run_id": self.run_id,
            "supervisor": self.supervisor.as_json(),
            "children": {
                "control": self.control.as_json(),
                "web": self.web.as_json(),
            },
        }


@dataclass(frozen=True)
class LifecycleStatus:
    state: str
    generation_identity: str | None = None
    supervisor_pid: int | None = None
    control_port: int | None = None
    web_port: int | None = None
    #: ⟦P7⟧ Read from the installed generation's configuration, not from the
    #: lifecycle record, whose schema is unchanged across the upgrade moment.
    web_port_fixed: bool = False
    public_origin: str | None = None

    @property
    def web_url(self) -> str | None:
        if self.web_port is None:
            return None
        return f"http://127.0.0.1:{self.web_port}"


@dataclass(frozen=True)
class LocalFrontDoorHealth:
    healthy: bool
    web_url: str | None
    category: str


def _ensure_private_directory(path: Path, *, label: str) -> Path:
    candidate = _normalized_absolute(path)
    missing: list[Path] = []
    component = candidate
    try:
        while True:
            try:
                component.lstat()
                break
            except FileNotFoundError:
                missing.append(component)
                if component.parent == component:
                    raise LifecycleError(f"{label} has no existing ancestor")
                component = component.parent
        for existing in (component, *component.parents):
            details = existing.lstat()
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise LifecycleError(f"{label} has a symlink ancestor")
        for component in reversed(missing):
            try:
                component.mkdir(mode=0o700)
            except FileExistsError:
                pass
            details = component.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o700
            ):
                raise LifecycleError(f"{label} is unsafe")
        details = candidate.lstat()
    except OSError as exc:
        raise LifecycleError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise LifecycleError(f"{label} is unsafe")
    return candidate


def _open_private_file(
    path: Path,
    flags: int,
    *,
    label: str,
    missing_ok: bool = False,
) -> int | None:
    try:
        descriptor = os.open(
            path,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LifecycleError(f"{label} is unavailable") from None
    except OSError as exc:
        raise LifecycleError(f"{label} is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise LifecycleError(f"{label} is unsafe")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _bounded_flock(descriptor: int, *, deadline: float, label: str) -> None:
    if fcntl is None:
        raise LifecycleError(f"{label} requires macOS or Linux")
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LifecycleError(f"{label} timed out") from None
            time.sleep(min(0.02, remaining))


def _remaining_timeout(deadline: float) -> float:
    return max(0, deadline - time.monotonic())


@contextmanager
def _lifecycle_lock(runtime_root: Path, *, deadline: float) -> Iterator[None]:
    if fcntl is None:
        raise LifecycleError("lifecycle locking requires macOS or Linux")
    descriptor = _open_private_file(
        runtime_root / ".lifecycle.lock",
        os.O_CREAT | os.O_RDWR,
        label="lifecycle lock",
    )
    assert descriptor is not None
    try:
        _bounded_flock(descriptor, deadline=deadline, label="lifecycle lock")
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_private_json(path: Path, value: object) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _positive_integer(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise LifecycleError(f"lifecycle {label} is invalid")
    return value


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LifecycleError(f"lifecycle {label} is invalid")
    return value


def _process_claim(raw: object, *, fields: set[str], label: str) -> ProcessClaim:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise LifecycleError(f"lifecycle {label} schema is invalid")
    claim = _string(raw["claim"], label=f"{label} claim")
    start_token = _string(raw["start_token"], label=f"{label} start token")
    if label == "supervisor" and _CLAIM.fullmatch(claim) is None:
        raise LifecycleError("lifecycle supervisor claim is invalid")
    return ProcessClaim(
        pid=_positive_integer(raw["pid"], label=f"{label} pid"),
        start_token=start_token,
        claim=claim,
    )


def _parse_child(raw: object, *, role: str) -> ControlClaim | WebClaim:
    fields = _CONTROL_FIELDS if role == "control" else _WEB_FIELDS
    base = _process_claim(raw, fields=fields, label=role)
    assert isinstance(raw, dict)
    host = _string(raw["host"], label=f"{role} host")
    port = _positive_integer(raw["port"], label=f"{role} port")
    if host != "127.0.0.1" or port > 65535:
        raise LifecycleError(f"lifecycle {role} listener is invalid")
    if role == "control":
        return ControlClaim(
            **base.__dict__,
            host=host,
            port=port,
            instance_id=_string(raw["instance_id"], label="control instance"),
        )
    return WebClaim(
        **base.__dict__,
        host=host,
        port=port,
        build_id=_string(raw["build_id"], label="Web build"),
    )


def _read_record(path: Path) -> LifecycleRecord | None:
    descriptor = _open_private_file(
        path,
        os.O_RDONLY,
        label="lifecycle state",
        missing_ok=True,
    )
    if descriptor is None:
        return None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("lifecycle state is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(raw, dict)
        or set(raw) != _STATE_FIELDS
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
    ):
        raise LifecycleError("lifecycle state schema is invalid")
    generation_identity = _string(raw["generation_identity"], label="generation identity")
    run_id = _string(raw["run_id"], label="run id")
    if _GENERATION_ID.fullmatch(generation_identity) is None or _RUN_ID.fullmatch(run_id) is None:
        raise LifecycleError("lifecycle state identity is invalid")
    children = raw["children"]
    if not isinstance(children, dict) or set(children) != {"control", "web"}:
        raise LifecycleError("lifecycle child schema is invalid")
    supervisor = _process_claim(
        raw["supervisor"],
        fields=_SUPERVISOR_FIELDS,
        label="supervisor",
    )
    control = _parse_child(children["control"], role="control")
    web = _parse_child(children["web"], role="web")
    expected_prefix = f"cortex-{generation_identity[:16]}-{run_id}-{supervisor.claim[:16]}-"
    if (
        control.claim != f"{expected_prefix}control"
        or control.instance_id != control.claim
        or web.claim != f"{expected_prefix}web"
    ):
        raise LifecycleError("lifecycle child claim is invalid")
    return LifecycleRecord(
        generation_identity=generation_identity,
        generation_root=_string(raw["generation_root"], label="generation root"),
        run_id=run_id,
        supervisor=supervisor,
        control=control,
        web=web,
    )


def _process_details(pid: int, *, deadline: float | None = None) -> tuple[str, str] | None:
    if deadline is not None and time.monotonic() >= deadline:
        raise LifecycleError("process observation timed out")
    if sys.platform.startswith("linux"):
        try:
            fields = (
                Path(f"/proc/{pid}/stat")
                .read_text(encoding="utf-8")
                .rsplit(")", 1)[1]
                .strip()
                .split()
            )
            if fields[0] == "Z":
                return None
            start_token = fields[19]
            if deadline is not None and time.monotonic() >= deadline:
                raise LifecycleError("process observation timed out")
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
            return start_token, command
        except FileNotFoundError:
            return None
        except (OSError, IndexError, ValueError) as exc:
            raise LifecycleError("process observation failed") from exc
    timeout = 1.0
    if deadline is not None:
        timeout = min(timeout, _remaining_timeout(deadline))
        if timeout <= 0:
            raise LifecycleError("process observation timed out")
    try:
        completed = subprocess.run(
            [
                "/bin/ps",
                "-ww",
                "-p",
                str(pid),
                "-o",
                "stat=",
                "-o",
                "lstart=",
                "-o",
                "command=",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("process observation failed") from exc
    output = completed.stdout.strip()
    if completed.returncode == 1 and not output and not completed.stderr.strip():
        return None
    fields = output.split(None, 6)
    if completed.returncode != 0 or len(fields) != 7:
        raise LifecycleError("process observation failed")
    if fields[0].startswith("Z"):
        return None
    return " ".join(fields[1:6]), fields[6]


def _current_start_token(pid: int, *, deadline: float | None = None) -> str | None:
    details = _process_details(pid, deadline=deadline)
    return None if details is None else details[0]


def _identity_matches(process: ProcessClaim, *, deadline: float | None = None) -> bool:
    details = _process_details(process.pid, deadline=deadline)
    return bool(
        details is not None
        and details[0] == process.start_token
        and process.claim in details[1]
    )


class _DeadlineReader(io.RawIOBase):
    def __init__(self, raw: BinaryIO, transport: socket.socket, deadline: float) -> None:
        self._raw = raw
        self._transport = transport
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int | None:
        remaining = _remaining_timeout(self._deadline)
        if remaining <= 0:
            raise TimeoutError("HTTP response deadline exceeded")
        self._transport.settimeout(remaining)
        return self._raw.readinto(buffer)

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, transport: socket.socket, deadline: float) -> None:
        self._transport = transport
        self._deadline = deadline

    def makefile(self, mode: str) -> BinaryIO:
        if mode != "rb":
            raise ValueError("deadline socket supports binary reads only")
        raw = self._transport.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self._transport, self._deadline))

    def close(self) -> None:
        self._transport.close()


def _json_request(
    port: int,
    path: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    deadline: float | None = None,
) -> tuple[int, dict[str, object]] | None:
    request_deadline = time.monotonic() + timeout if deadline is None else deadline
    remaining = _remaining_timeout(request_deadline)
    if remaining <= 0:
        return None
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=remaining)
    try:
        remaining = _remaining_timeout(request_deadline)
        if remaining <= 0:
            return None
        connection.timeout = remaining
        connection.connect()
        transport = connection.sock
        if transport is None:
            return None
        remaining = _remaining_timeout(request_deadline)
        if remaining <= 0:
            return None
        transport.settimeout(remaining)
        connection.request(
            "GET",
            path,
            headers=headers if headers is not None else {"Accept": "application/json"},
        )
        remaining = _remaining_timeout(request_deadline)
        if remaining <= 0:
            return None
        transport.settimeout(remaining)
        connection.sock = _DeadlineSocket(transport, request_deadline)
        response = connection.getresponse()
        body = bytearray()
        while len(body) < 65_537:
            remaining = _remaining_timeout(request_deadline)
            if remaining <= 0:
                return None
            transport.settimeout(remaining)
            chunk = response.read1(min(8_192, 65_537 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if response.isclosed():
                break
        if len(body) > 65_536:
            return None
        payload = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    if not isinstance(payload, dict):
        return None
    return response.status, payload


def _linux_listener_owned(pid: int, port: int) -> bool:
    sockets: set[str] = set()
    try:
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                sockets.add(target[8:-1])
    except OSError:
        return False
    if not sockets:
        return False
    expected_port = f"{port:04X}"
    for table in (Path(f"/proc/{pid}/net/tcp"), Path(f"/proc/{pid}/net/tcp6")):
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A" or fields[9] not in sockets:
                continue
            address, separator, local_port = fields[1].partition(":")
            if separator and local_port == expected_port and address in {
                "0100007F",
                "0000000000000000FFFF00000100007F",
            }:
                return True
    return False


def _listener_owned(pid: int, port: int, *, deadline: float | None = None) -> bool:
    if deadline is not None and time.monotonic() >= deadline:
        return False
    if sys.platform.startswith("linux"):
        return _linux_listener_owned(pid, port)
    if sys.platform == "darwin":
        timeout = 1.0
        if deadline is not None:
            timeout = min(timeout, _remaining_timeout(deadline))
            if timeout <= 0:
                return False
        try:
            completed = subprocess.run(
                [
                    "/usr/sbin/lsof",
                    "-nP",
                    "-a",
                    "-p",
                    str(pid),
                    f"-iTCP@127.0.0.1:{port}",
                    "-sTCP:LISTEN",
                    "-Fn",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError):
            return False
        lines = set(completed.stdout.splitlines())
        return (
            completed.returncode == 0
            and f"p{pid}" in lines
            and f"n127.0.0.1:{port}" in lines
        )
    return False


def _control_healthy(control: ControlClaim, *, timeout: float = 0.4) -> bool:
    if not _listener_owned(control.pid, control.port):
        return False
    result = _json_request(control.port, "/healthz", timeout=timeout)
    return bool(
        result is not None
        and result[0] == 200
        and result[1].get("service") == "cortexd"
        and result[1].get("status") == "ok"
        and result[1].get("instance_id") == control.instance_id
        and _listener_owned(control.pid, control.port)
    )


def _web_health_matches(body: dict[str, object], build_id: str) -> bool:
    # ⟦P7⟧ An adapter with a public door adds one boolean, `public_door`; an
    # adapter without one (every generation before P7) sends the four keys.
    expected = {
        "adapter_version": 1,
        "build_id": build_id,
        "service": "cortex-web",
        "status": "ok",
    }
    public_door = body.get("public_door", False)
    if type(public_door) is not bool:
        return False
    return {name: value for name, value in body.items() if name != "public_door"} == expected


def _web_healthy(web: WebClaim, *, timeout: float = 0.4) -> bool:
    if not _listener_owned(web.pid, web.port):
        return False
    result = _json_request(web.port, "/_cortex/health", timeout=timeout)
    return bool(
        result is not None
        and result[0] == 200
        and _web_health_matches(result[1], web.build_id)
        and _listener_owned(web.pid, web.port)
    )


def _require_free_web_port(port: int) -> None:
    """Refuse a fixed listener port that something else already holds.

    ⟦P7⟧ Typed here, before the supervisor is spawned, because the adapter's
    own `EADDRINUSE` only reaches `web.log`; the parent would report "exited
    before readiness" and the operator would have to go and read why.
    `SO_REUSEADDR` matches what Node's listener sets, so a `TIME_WAIT` socket
    left by the previous instance does not read as a conflict, while a live
    listener still does. Two probes, because BSD lets a specific-address bind
    coexist with a wildcard listener under `SO_REUSEADDR`: a holder on
    `0.0.0.0:<port>` is invisible to the loopback probe and vice versa, and
    either one is the conflict the contract promises to name.
    """

    for address in ("127.0.0.1", "0.0.0.0"):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((address, port))
        except OSError as exc:
            raise LifecycleError(
                f"Web listen port {port} is already in use on 127.0.0.1"
            ) from exc
        finally:
            probe.close()


def _resolve_web_settings(
    generation: InstalledGeneration, *, home: Path, config_file: Path
) -> InstalledWebSettings:
    try:
        return resolve_installed_web_settings(
            generation.root / "runtime",
            config_file=config_file,
            home=home,
        )
    except InstalledWebSettingsError as exc:
        raise LifecycleError(str(exc)) from exc


def _status_for_record(
    record: LifecycleRecord,
    generation: InstalledGeneration,
    *,
    deadline: float | None = None,
) -> LifecycleStatus:
    identity = record.generation_identity
    if identity != generation.identity or record.generation_root != str(generation.root):
        return LifecycleStatus("stale", generation_identity=identity)
    if not _identity_matches(record.supervisor, deadline=deadline):
        return LifecycleStatus("stale", generation_identity=identity)
    if record.web.build_id != generation.release_build_id:
        return LifecycleStatus("unhealthy", generation_identity=identity)
    if not _identity_matches(record.control, deadline=deadline) or not _identity_matches(
        record.web, deadline=deadline
    ):
        return LifecycleStatus("unhealthy", generation_identity=identity)
    if not _control_healthy(record.control) or not _web_healthy(record.web):
        return LifecycleStatus("unhealthy", generation_identity=identity)
    return LifecycleStatus(
        "running",
        generation_identity=identity,
        supervisor_pid=record.supervisor.pid,
        control_port=record.control.port,
        web_port=record.web.port,
    )


def _open_log(path: Path) -> BinaryIO:
    descriptor = _open_private_file(
        path,
        os.O_CREAT | os.O_WRONLY | os.O_APPEND,
        label="lifecycle log",
    )
    assert descriptor is not None
    return os.fdopen(descriptor, "ab", buffering=0)


def _wait_for_start_token(
    pid: int,
    *,
    deadline: float,
    cancel: threading.Event | None = None,
) -> str:
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise LifecycleError("supervisor startup was cancelled")
        token = _current_start_token(pid, deadline=deadline)
        if token is not None:
            return token
        time.sleep(0.01)
    raise LifecycleError("child process identity was not established")


def _read_control_metadata(
    path: Path,
    *,
    process: subprocess.Popen[bytes],
    instance_id: str,
    start_token: str,
) -> ControlReady | None:
    descriptor = _open_private_file(
        path,
        os.O_RDONLY,
        label="Cortexd metadata",
        missing_ok=True,
    )
    if descriptor is None:
        return None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected = {
        "control_token",
        "host",
        "instance_id",
        "pid",
        "port",
        "schema_version",
        "start_token",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        return None
    token = raw["control_token"]
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or raw["pid"] != process.pid
        or raw["instance_id"] != instance_id
        or raw["start_token"] != start_token
        or raw["host"] != "127.0.0.1"
        or type(raw["port"]) is not int
        or not 0 < raw["port"] < 65536
        or not isinstance(token, str)
        or _CONTROL_TOKEN.fullmatch(token) is None
    ):
        return None
    return ControlReady(
        claim=ControlClaim(
            pid=process.pid,
            start_token=start_token,
            claim=instance_id,
            host="127.0.0.1",
            port=raw["port"],
            instance_id=instance_id,
        ),
        control_token=token,
    )


def _await_control(
    process: subprocess.Popen[bytes],
    *,
    metadata_path: Path,
    instance_id: str,
    start_token: str,
    deadline: float,
    cancel: threading.Event,
) -> ControlReady:
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise LifecycleError("supervisor startup was cancelled")
        if process.poll() is not None:
            raise LifecycleError("Cortexd exited before readiness")
        ready = _read_control_metadata(
            metadata_path,
            process=process,
            instance_id=instance_id,
            start_token=start_token,
        )
        if (
            ready is not None
            and _identity_matches(ready.claim, deadline=deadline)
            and _control_healthy(ready.claim, timeout=_remaining_timeout(deadline))
        ):
            return ready
        time.sleep(0.02)
    raise LifecycleError("Cortexd readiness timed out")


def _await_web(
    process: subprocess.Popen[bytes],
    *,
    claim: str,
    start_token: str,
    build_id: str,
    deadline: float,
    cancel: threading.Event,
    expected_port: int | None = None,
) -> WebClaim:
    if process.stdout is None:
        raise LifecycleError("Web readiness channel is unavailable")
    output: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def read_ready() -> None:
        output.put(process.stdout.readline(65_537))

    threading.Thread(target=read_ready, daemon=True).start()
    while True:
        if cancel.is_set():
            raise LifecycleError("supervisor startup was cancelled")
        if process.poll() is not None:
            raise LifecycleError("Web process exited before readiness")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LifecycleError("Web readiness timed out")
        try:
            line = output.get(timeout=min(0.05, remaining))
            break
        except queue.Empty:
            continue
    if len(line) > 65_536:
        raise LifecycleError("Web readiness record is oversized")
    try:
        raw = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("Web readiness record is invalid") from exc
    expected = {"adapter_version", "build_id", "event", "host", "port", "service"}
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or type(raw["adapter_version"]) is not int
        or raw["adapter_version"] != 1
        or raw["build_id"] != build_id
        or raw["event"] != "ready"
        or raw["host"] != "127.0.0.1"
        or type(raw["port"]) is not int
        or not 0 < raw["port"] < 65536
        or raw["service"] != "cortex-web"
    ):
        raise LifecycleError("Web readiness record is invalid")
    if expected_port is not None and raw["port"] != expected_port:
        raise LifecycleError(
            f"Web listener bound port {raw['port']} instead of the configured {expected_port}"
        )
    result = WebClaim(
        pid=process.pid,
        start_token=start_token,
        claim=claim,
        host="127.0.0.1",
        port=raw["port"],
        build_id=build_id,
    )
    if not _identity_matches(result, deadline=deadline) or not _web_healthy(
        result, timeout=_remaining_timeout(deadline)
    ):
        raise LifecycleError("Web listener ownership could not be verified")
    return result


def _signal_owned(
    process: ProcessClaim,
    signum: int,
    *,
    deadline: float | None = None,
) -> bool:
    pidfd: int | None = None
    try:
        if sys.platform.startswith("linux"):
            opener = getattr(os, "pidfd_open", None)
            sender = getattr(signal, "pidfd_send_signal", None)
            if opener is None or sender is None:
                return False
            pidfd = opener(process.pid, 0)
        first = _process_details(process.pid, deadline=deadline)
        if (
            first is None
            or first[0] != process.start_token
            or process.claim not in first[1]
        ):
            return False
        second = _process_details(process.pid, deadline=deadline)
        if second != first:
            return False
        if pidfd is not None:
            sender(pidfd, signum, None, 0)
        else:
            os.kill(process.pid, signum)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise LifecycleError("owned process could not be signaled") from exc
    finally:
        if pidfd is not None:
            os.close(pidfd)
    return True


def _wait_identity_exit(process: ProcessClaim, *, deadline: float) -> bool:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not _identity_matches(process, deadline=deadline):
            break
        time.sleep(min(0.02, remaining))
    try:
        os.waitpid(process.pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    return True


def _terminate_owned(process: ProcessClaim, *, timeout: float) -> None:
    started = time.monotonic()
    budget = max(timeout, 0)
    graceful_deadline = started + budget / 2
    cleanup_deadline = started + budget
    if not _signal_owned(process, signal.SIGTERM, deadline=cleanup_deadline):
        return
    if _wait_identity_exit(process, deadline=graceful_deadline):
        return
    if _signal_owned(process, signal.SIGKILL, deadline=cleanup_deadline):
        if not _wait_identity_exit(process, deadline=cleanup_deadline):
            raise LifecycleError("owned process did not exit within the cleanup bound")


def _terminate_spawned(
    process: subprocess.Popen[bytes],
    *,
    start_token: str | None,
    claim: str,
    timeout: float,
) -> None:
    del start_token, claim
    if process.poll() is not None:
        return
    started = time.monotonic()
    budget = max(timeout, 0)
    try:
        process.terminate()
        process.wait(timeout=budget / 2)
        return
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
        process.wait(timeout=max(0, started + budget - time.monotonic()))
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired as exc:
        raise LifecycleError("spawned process did not exit within the cleanup bound") from exc


def _shutdown_socket_path(runtime_root: Path) -> Path:
    digest = hashlib.sha256(str(runtime_root).encode("utf-8")).hexdigest()[:24]
    anchor = Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")
    return anchor / f"cortex-supervisor-{os.geteuid()}-{digest}.sock"


def _open_shutdown_listener(runtime_root: Path) -> tuple[socket.socket, Path, tuple[int, int]]:
    path = _shutdown_socket_path(runtime_root)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LifecycleError("supervisor shutdown channel is unavailable") from exc
    else:
        if (
            not stat.S_ISSOCK(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise LifecycleError("supervisor shutdown channel is unsafe")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600, follow_symlinks=False)
        listener.listen(1)
        listener.settimeout(0.05)
        details = path.lstat()
        if (
            not stat.S_ISSOCK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise LifecycleError("supervisor shutdown channel is unsafe")
        return listener, path, (details.st_dev, details.st_ino)
    except BaseException:
        listener.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _remove_shutdown_socket(path: Path, identity: tuple[int, int]) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LifecycleError("supervisor shutdown channel could not be removed") from exc
    if (
        stat.S_ISSOCK(details.st_mode)
        and details.st_uid == os.geteuid()
        and (details.st_dev, details.st_ino) == identity
    ):
        path.unlink()


def _read_shutdown_message(
    connection: socket.socket,
    *,
    deadline: float,
    limit: int = 4096,
) -> object:
    payload = bytearray()
    while not payload.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LifecycleError("supervisor shutdown message timed out")
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(min(1024, limit + 1 - len(payload)))
        except socket.timeout as exc:
            raise LifecycleError("supervisor shutdown message timed out") from exc
        if not chunk:
            raise LifecycleError("supervisor shutdown message is incomplete")
        payload.extend(chunk)
        if len(payload) > limit:
            raise LifecycleError("supervisor shutdown message is oversized")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("supervisor shutdown message is invalid") from exc


def _accept_shutdown_request(
    listener: socket.socket,
    *,
    claim: str,
    stop_requested: threading.Event,
    shutdown_deadline: list[float | None],
) -> None:
    try:
        connection, _address = listener.accept()
    except socket.timeout:
        return
    except OSError as exc:
        raise LifecycleError("supervisor shutdown channel failed") from exc
    with connection:
        read_deadline = time.monotonic() + 0.5
        try:
            raw = _read_shutdown_message(connection, deadline=read_deadline)
        except (LifecycleError, OSError):
            return
        if (
            not isinstance(raw, dict)
            or set(raw) != _SHUTDOWN_REQUEST_FIELDS
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or raw["operation"] != "stop"
            or not isinstance(raw["claim"], str)
            or not secrets.compare_digest(raw["claim"], claim)
            or type(raw["deadline"]) not in {int, float}
            or not math.isfinite(raw["deadline"])
            or not time.monotonic() < raw["deadline"] <= time.monotonic() + 60
        ):
            return
        response = {
            "schema_version": 1,
            "accepted": True,
            "claim": claim,
        }
        try:
            connection.sendall(canonical_json_bytes(response) + b"\n")
        except OSError:
            return
        shutdown_deadline[0] = float(raw["deadline"])
        stop_requested.set()


def _request_supervisor_shutdown(
    runtime_root: Path,
    record: LifecycleRecord,
    *,
    deadline: float,
) -> None:
    if not _identity_matches(record.supervisor, deadline=deadline):
        raise LifecycleError("supervisor shutdown identity does not match")
    path = _shutdown_socket_path(runtime_root)
    try:
        details = path.lstat()
    except OSError as exc:
        raise LifecycleError("supervisor shutdown channel is unavailable") from exc
    if (
        not stat.S_ISSOCK(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise LifecycleError("supervisor shutdown channel is unsafe")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LifecycleError("supervisor shutdown timed out")
    request = {
        "schema_version": 1,
        "operation": "stop",
        "claim": record.supervisor.claim,
        "deadline": deadline,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            remaining = _remaining_timeout(deadline)
            if remaining <= 0:
                raise LifecycleError("supervisor shutdown timed out")
            connection.settimeout(remaining)
            connection.connect(str(path))
            remaining = _remaining_timeout(deadline)
            if remaining <= 0:
                raise LifecycleError("supervisor shutdown timed out")
            connection.settimeout(remaining)
            connection.sendall(canonical_json_bytes(request) + b"\n")
            response = _read_shutdown_message(connection, deadline=deadline)
    except (OSError, LifecycleError) as exc:
        raise LifecycleError("supervisor shutdown was not acknowledged") from exc
    if (
        not isinstance(response, dict)
        or set(response) != _SHUTDOWN_RESPONSE_FIELDS
        or type(response["schema_version"]) is not int
        or response["schema_version"] != 1
        or response["accepted"] is not True
        or not isinstance(response["claim"], str)
        or not secrets.compare_digest(response["claim"], record.supervisor.claim)
    ):
        raise LifecycleError("supervisor shutdown acknowledgement is invalid")
    if not _wait_identity_exit(record.supervisor, deadline=deadline):
        raise LifecycleError("supervisor shutdown did not complete")


def _remove_matching_record(path: Path, expected_claim: str, *, deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise LifecycleError("lifecycle state cleanup timed out")
    try:
        current = _read_record(path)
    except LifecycleError:
        return
    if current is not None and current.supervisor.claim == expected_claim:
        path.unlink(missing_ok=True)


def _remove_matching_control_metadata(
    path: Path,
    expected: ControlClaim,
    *,
    deadline: float,
) -> None:
    if fcntl is None:
        return
    lock = _open_private_file(
        path.with_name(".cortexd.metadata.lock"),
        os.O_CREAT | os.O_RDWR,
        label="Cortexd metadata lock",
    )
    assert lock is not None
    try:
        _bounded_flock(lock, deadline=deadline, label="metadata lock")
        descriptor = _open_private_file(
            path,
            os.O_RDONLY,
            label="Cortexd metadata",
            missing_ok=True,
        )
        if descriptor is None:
            return
        try:
            opened = os.fstat(descriptor)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                raw = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            named = path.stat(follow_symlinks=False)
        except OSError:
            return
        if (
            not isinstance(raw, dict)
            or raw.get("pid") != expected.pid
            or raw.get("start_token") != expected.start_token
            or raw.get("instance_id") != expected.instance_id
            or raw.get("host") != expected.host
            or raw.get("port") != expected.port
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            return
        path.unlink(missing_ok=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _child_claim(generation: InstalledGeneration, run_id: str, claim: str, role: str) -> str:
    return f"cortex-{generation.identity[:16]}-{run_id}-{claim[:16]}-{role}"


def _acquire_supervisor_lock(runtime_root: Path, *, deadline: float) -> int:
    if fcntl is None:
        raise LifecycleError("supervisor locking requires macOS or Linux")
    descriptor = _open_private_file(
        runtime_root / ".supervisor.lock",
        os.O_CREAT | os.O_RDWR,
        label="supervisor lifetime lock",
    )
    assert descriptor is not None
    try:
        _bounded_flock(
            descriptor,
            deadline=deadline,
            label="supervisor lifetime lock",
        )
    except LifecycleError:
        os.close(descriptor)
        raise
    return descriptor


def _foreground_run(
    generation_path: Path,
    runtime_path: Path,
    home: Path,
    product_paths: InstalledProductPaths,
    *,
    run_id: str,
    claim: str,
    expected_generation: str,
    startup_timeout: float,
) -> int:
    if not math.isfinite(startup_timeout) or startup_timeout <= 0:
        raise LifecycleError("startup timeout must be positive and finite")
    generation = load_generation(generation_path)
    if generation.identity != expected_generation:
        raise LifecycleError("generation identity changed before supervisor start")
    runtime_root = _ensure_private_directory(runtime_path, label="lifecycle root")
    config = _ensure_private_directory(product_paths.config_dir, label="config directory")
    data = _ensure_private_directory(product_paths.data_dir, label="data directory")
    control_state = _ensure_private_directory(
        product_paths.state_dir, label="Cortexd state directory"
    )
    cache = _ensure_private_directory(product_paths.cache_dir, label="cache directory")
    logs = _ensure_private_directory(product_paths.log_dir, label="log directory")
    state_path = runtime_root / "lifecycle.json"
    deadline = time.monotonic() + startup_timeout
    supervisor_start = _current_start_token(os.getpid(), deadline=deadline)
    if supervisor_start is None:
        raise LifecycleError("supervisor process identity could not be established")
    supervisor = ProcessClaim(os.getpid(), supervisor_start, claim)
    control_marker = _child_claim(generation, run_id, claim, "control")
    web_marker = _child_claim(generation, run_id, claim, "web")
    stop_requested = threading.Event()
    shutdown_deadline: list[float | None] = [None]
    control_process: subprocess.Popen[bytes] | None = None
    web_process: subprocess.Popen[bytes] | None = None
    control_start: str | None = None
    web_start: str | None = None
    control_ready: ControlReady | None = None
    control_claim: ControlClaim | None = None
    web_claim: WebClaim | None = None
    control_log: BinaryIO | None = None
    web_log: BinaryIO | None = None
    shutdown_listener: socket.socket | None = None
    shutdown_path: Path | None = None
    shutdown_identity: tuple[int, int] | None = None
    previous_handlers: dict[int, object] = {}
    lifetime_lock = _acquire_supervisor_lock(runtime_root, deadline=deadline)

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        shutdown_listener, shutdown_path, shutdown_identity = _open_shutdown_listener(
            runtime_root
        )
        control_log = _open_log(logs / "cortexd.log")
        control_process = subprocess.Popen(
            [
                str(generation.control_executable),
                "--instance-id",
                control_marker,
                "--config-file",
                str(product_paths.config_file),
                "--config-dir",
                str(config),
                "--data-dir",
                str(data),
                "--state-dir",
                str(control_state),
                "--cache-dir",
                str(cache),
                "--log-dir",
                str(logs),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=control_log,
            stderr=subprocess.STDOUT,
            cwd=runtime_root,
            env=generation.control_environment(home=home),
            close_fds=True,
        )
        control_start = _wait_for_start_token(
            control_process.pid,
            deadline=deadline,
            cancel=stop_requested,
        )
        control_ready = _await_control(
            control_process,
            metadata_path=control_state / "cortexd.json",
            instance_id=control_marker,
            start_token=control_start,
            deadline=deadline,
            cancel=stop_requested,
        )
        control_claim = control_ready.claim
        # ⟦P7⟧ Read after `cortexd` is up: its `initialize` is what creates a
        # first `config.toml`, and the installed generation's own interpreter
        # is what says how that file shapes the front door.
        web_settings = _resolve_web_settings(
            generation, home=home, config_file=product_paths.config_file
        )
        if web_settings.port is not None:
            _require_free_web_port(web_settings.port)
        web_bootstrap = secrets.token_urlsafe(32)
        web_environment = generation.web_environment(
            control_port=control_claim.port,
            control_token=control_ready.control_token,
            bootstrap_token=web_bootstrap,
            web=web_settings,
        )
        web_log = _open_log(logs / "web.log")
        web_process = subprocess.Popen(
            [
                str(generation.node_executable),
                str(generation.web_adapter),
                f"--cortex-run-claim={web_marker}",
                f"--cortex-generation={generation.identity}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=web_log,
            cwd=generation.web_root,
            env=web_environment,
            close_fds=True,
        )
        control_ready = None
        del web_environment, web_bootstrap
        web_start = _wait_for_start_token(
            web_process.pid,
            deadline=deadline,
            cancel=stop_requested,
        )
        web_claim = _await_web(
            web_process,
            claim=web_marker,
            start_token=web_start,
            build_id=generation.release_build_id,
            deadline=deadline,
            cancel=stop_requested,
            expected_port=web_settings.port,
        )
        record = LifecycleRecord(
            generation_identity=generation.identity,
            generation_root=str(generation.root),
            run_id=run_id,
            supervisor=supervisor,
            control=control_claim,
            web=web_claim,
        )
        _atomic_private_json(state_path, record.as_json())

        while not stop_requested.is_set():
            if control_process.poll() is not None or web_process.poll() is not None:
                return 1
            _accept_shutdown_request(
                shutdown_listener,
                claim=claim,
                stop_requested=stop_requested,
                shutdown_deadline=shutdown_deadline,
            )
        return 0
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_deadline = shutdown_deadline[0]
        if cleanup_deadline is None:
            cleanup_deadline = time.monotonic() + _FOREGROUND_CLEANUP_TIMEOUT
        cleanup_errors: list[BaseException] = []

        def attempt(action: object, *arguments: object, **keywords: object) -> None:
            try:
                action(*arguments, **keywords)  # type: ignore[operator]
            except BaseException as exc:
                cleanup_errors.append(exc)

        if web_process is not None:
            attempt(
                _terminate_spawned,
                web_process,
                start_token=web_start,
                claim=web_marker,
                timeout=_remaining_timeout(cleanup_deadline),
            )
        pending_control_metadata: ControlClaim | None = None
        if control_claim is None and control_process is not None and control_start is not None:
            pending_control_ready = _read_control_metadata(
                control_state / "cortexd.json",
                process=control_process,
                instance_id=control_marker,
                start_token=control_start,
            )
            if pending_control_ready is not None:
                pending_control_metadata = pending_control_ready.claim
        if control_process is not None:
            attempt(
                _terminate_spawned,
                control_process,
                start_token=control_start,
                claim=control_marker,
                timeout=_remaining_timeout(cleanup_deadline),
            )
        if not cleanup_errors and control_claim is not None:
            attempt(
                _remove_matching_control_metadata,
                control_state / "cortexd.json",
                control_claim,
                deadline=cleanup_deadline,
            )
        elif not cleanup_errors and control_process is not None:
            if pending_control_metadata is not None:
                attempt(
                    _remove_matching_control_metadata,
                    control_state / "cortexd.json",
                    pending_control_metadata,
                    deadline=cleanup_deadline,
                )
        if web_process is not None and web_process.stdout is not None:
            attempt(web_process.stdout.close)
        if web_log is not None:
            attempt(web_log.close)
        if control_log is not None:
            attempt(control_log.close)
        if shutdown_listener is not None:
            attempt(shutdown_listener.close)
        if shutdown_path is not None and shutdown_identity is not None:
            attempt(_remove_shutdown_socket, shutdown_path, shutdown_identity)
        if fcntl is not None:
            attempt(fcntl.flock, lifetime_lock, fcntl.LOCK_UN)
        attempt(os.close, lifetime_lock)
        for signum, previous in previous_handlers.items():
            attempt(signal.signal, signum, previous)
        if cleanup_errors and not active_error:
            raise LifecycleError("supervisor cleanup did not complete") from cleanup_errors[0]


_FOREGROUND_BOOTSTRAP = (
    "import sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "from distribution.lifecycle import _foreground_entry;"
    "raise SystemExit(_foreground_entry(sys.argv[2:]))"
)

_SERVICE_BOOTSTRAP = (
    "import sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "from distribution.lifecycle import _service_entry;"
    "raise SystemExit(_service_entry(sys.argv[1:]))"
)


def _foreground_entry(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="cortex-dist foreground")
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--config-file", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--claim", required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--startup-timeout", type=float, default=10)
    arguments = parser.parse_args(argv)
    if _RUN_ID.fullmatch(arguments.run_id) is None or _CLAIM.fullmatch(arguments.claim) is None:
        raise LifecycleError("supervisor run identity is invalid")
    if not math.isfinite(arguments.startup_timeout) or arguments.startup_timeout <= 0:
        raise LifecycleError("startup timeout must be positive and finite")
    return _foreground_run(
        arguments.generation,
        arguments.runtime_root,
        arguments.home,
        InstalledProductPaths(
            config_file=arguments.config_file,
            config_dir=arguments.config_dir,
            data_dir=arguments.data_dir,
            state_dir=arguments.state_dir,
            cache_dir=arguments.cache_dir,
            log_dir=arguments.log_dir,
            control_database_file=arguments.data_dir / "control.db",
            runtime_update_root=arguments.runtime_root,
        ),
        run_id=arguments.run_id,
        claim=arguments.claim,
        expected_generation=arguments.expected_generation,
        startup_timeout=arguments.startup_timeout,
    )


def _service_entry(argv: list[str]) -> int:
    if not argv:
        raise LifecycleError("service module root is unavailable")
    product_arguments: list[str] = []
    if "--config-file" not in argv:
        import argparse

        parser = argparse.ArgumentParser(prog="cortex-dist service")
        parser.add_argument("--generation", type=Path, required=True)
        parser.add_argument("--runtime-root", type=Path, required=True)
        parser.add_argument("--home", type=Path, required=True)
        arguments, _unknown = parser.parse_known_args(argv[1:])
        try:
            product_paths = resolve_installed_product_paths(
                arguments.generation / "runtime",
                home=arguments.home,
                environment={},
            )
        except InstalledProductPathsError as exc:
            raise LifecycleError(str(exc)) from exc
        if _normalized_absolute(arguments.runtime_root) != _normalized_absolute(
            product_paths.runtime_update_root
        ):
            raise LifecycleError("operation requires the canonical lifecycle root")
        product_arguments = [
            "--config-file",
            str(product_paths.config_file),
            "--config-dir",
            str(product_paths.config_dir),
            "--data-dir",
            str(product_paths.data_dir),
            "--state-dir",
            str(product_paths.state_dir),
            "--cache-dir",
            str(product_paths.cache_dir),
            "--log-dir",
            str(product_paths.log_dir),
        ]
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _FOREGROUND_BOOTSTRAP,
        *argv,
        *product_arguments,
        "--run-id",
        secrets.token_hex(16),
        "--claim",
        secrets.token_hex(32),
    ]
    os.execv(sys.executable, command)
    return 1


@dataclass(frozen=True)
class ServiceDefinition:
    path: Path
    sha256: str
    generation_identity: str
    launchctl_invoked: bool = False


def _validate_service_staging(path: Path, *, home: Path) -> Path:
    candidate = _normalized_absolute(path)
    homes = {_normalized_absolute(home), _normalized_absolute(Path.home())}
    candidate_parts = tuple(part.casefold() for part in candidate.parts)
    for account_home in homes:
        launch_agents = account_home / "Library" / "LaunchAgents"
        launch_parts = tuple(part.casefold() for part in launch_agents.parts)
        if candidate_parts[: len(launch_parts)] == launch_parts:
            raise LifecycleError("service staging must not target ~/Library/LaunchAgents")
    missing: list[Path] = []
    component = candidate
    while True:
        try:
            details = component.lstat()
        except FileNotFoundError:
            missing.append(component)
            if component.parent == component:
                raise LifecycleError("service staging directory has no existing ancestor")
            component = component.parent
            continue
        except OSError as exc:
            raise LifecycleError("service staging directory is unavailable") from exc
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise LifecycleError("service staging directory has a symlink ancestor")
        break
    for component in reversed(missing):
        try:
            component.mkdir(mode=0o700)
        except FileExistsError:
            pass
        details = component.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise LifecycleError("service staging directory is unsafe")
    staging = _ensure_private_directory(candidate, label="service staging directory")
    try:
        if staging.resolve(strict=True) != staging:
            raise LifecycleError("service staging directory has a symlink ancestor")
    except OSError as exc:
        raise LifecycleError("service staging directory is unavailable") from exc
    return staging


def _atomic_private_bytes(path: Path, payload: bytes) -> None:
    try:
        directory = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise LifecycleError("service staging directory changed before writing") from exc
    temporary: str | None = None
    published = False
    try:
        opened_directory = os.fstat(directory)
        if (
            not stat.S_ISDIR(opened_directory.st_mode)
            or opened_directory.st_uid != os.geteuid()
            or stat.S_IMODE(opened_directory.st_mode) != 0o700
        ):
            raise LifecycleError("service staging directory is unsafe")
        try:
            existing = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise LifecycleError("staged service definition is unsafe") from exc
        if existing is not None:
            try:
                details = os.fstat(existing)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or details.st_nlink != 1
                    or stat.S_IMODE(details.st_mode) != 0o600
                ):
                    raise LifecycleError("staged service definition is unsafe")
            finally:
                os.close(existing)
        descriptor: int | None = None
        for _attempt in range(32):
            temporary = f".{path.name}.{secrets.token_hex(16)}"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory,
                )
                break
            except FileExistsError:
                continue
        if descriptor is None:
            raise LifecycleError("private service temporary file could not be allocated")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        temporary = None
        published = True
        os.fsync(directory)
        try:
            named_directory = path.parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise LifecycleError("service staging directory changed while writing") from exc
        if (named_directory.st_dev, named_directory.st_ino) != (
            opened_directory.st_dev,
            opened_directory.st_ino,
        ):
            os.unlink(path.name, dir_fd=directory)
            published = False
            raise LifecycleError("service staging directory changed while writing")
    except BaseException as exc:
        if published:
            try:
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise LifecycleError("service definition could not be staged safely") from exc
        raise
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def _service_python(generation: InstalledGeneration) -> Path:
    directory = _safe_component(
        generation.root,
        Path("runtime/bin"),
        directory=True,
    )
    python = directory / "python"
    try:
        link = python.lstat()
    except OSError as exc:
        raise LifecycleError("installed Python runtime is unavailable") from exc
    if stat.S_ISREG(link.st_mode):
        _opened_digest(python, executable=True)
        return python
    if not stat.S_ISLNK(link.st_mode) or link.st_uid != os.geteuid():
        raise LifecycleError("installed Python runtime is unsafe")
    try:
        target = python.resolve(strict=True)
        details = target.stat(follow_symlinks=False)
    except OSError as exc:
        raise LifecycleError("installed Python runtime is unavailable") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid not in {0, os.geteuid()}
        or details.st_mode & 0o111 == 0
        or details.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise LifecycleError("installed Python runtime is unsafe")
    return python


def _launch_agent_payload(
    generation: InstalledGeneration,
    runtime_root: Path,
    product_paths: InstalledProductPaths,
    *,
    home: Path,
) -> bytes:
    logs = product_paths.log_dir
    python = _service_python(generation)
    module_root = _safe_component(generation.root, Path("bundle/tools"), directory=True)
    definition = {
        "EnvironmentVariables": {
            "HOME": str(_normalized_absolute(home)),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(python.parent),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        },
        "KeepAlive": False,
        "Label": "ai.cortex.devrel.supervisor",
        "ProcessType": "Interactive",
        "ProgramArguments": [
            str(python),
            "-I",
            "-B",
            "-c",
            _SERVICE_BOOTSTRAP,
            str(module_root),
            "--generation",
            str(generation.root),
            "--runtime-root",
            str(runtime_root),
            "--home",
            str(_normalized_absolute(home)),
            "--config-file",
            str(product_paths.config_file),
            "--config-dir",
            str(product_paths.config_dir),
            "--data-dir",
            str(product_paths.data_dir),
            "--state-dir",
            str(product_paths.state_dir),
            "--cache-dir",
            str(product_paths.cache_dir),
            "--log-dir",
            str(product_paths.log_dir),
            "--expected-generation",
            generation.identity,
            "--startup-timeout",
            "10.0",
        ],
        "RunAtLoad": False,
        "StandardErrorPath": str(logs / "service-supervisor.log"),
        "StandardOutPath": str(logs / "service-supervisor.log"),
        "WorkingDirectory": str(generation.root),
    }
    return plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)


def render_launch_agent(
    generation_path: Path,
    runtime_path: Path,
    *,
    home: Path,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    """Render an inactive macOS user service definition without filesystem writes."""

    generation = load_generation(generation_path)
    try:
        product_paths = resolve_installed_product_paths(
            generation.root / "runtime",
            home=home,
            environment=environment,
        )
    except InstalledProductPathsError as exc:
        raise LifecycleError(str(exc)) from exc
    runtime_root = _normalized_absolute(runtime_path)
    if runtime_root != _normalized_absolute(product_paths.runtime_update_root):
        raise LifecycleError("operation requires the canonical lifecycle root")
    return _launch_agent_payload(
        generation,
        runtime_root,
        product_paths,
        home=home,
    )


def stage_launch_agent(
    generation_path: Path,
    runtime_path: Path,
    staging_path: Path,
    *,
    home: Path,
    environment: Mapping[str, str] | None = None,
) -> ServiceDefinition:
    """Stage an inactive macOS user service definition in an owner-private root."""

    generation = load_generation(generation_path)
    try:
        product_paths = resolve_installed_product_paths(
            generation.root / "runtime",
            home=home,
            environment=environment,
        )
    except InstalledProductPathsError as exc:
        raise LifecycleError(str(exc)) from exc
    if _normalized_absolute(runtime_path) != _normalized_absolute(
        product_paths.runtime_update_root
    ):
        raise LifecycleError("operation requires the canonical lifecycle root")
    runtime_root = _ensure_private_directory(runtime_path, label="lifecycle root")
    _ensure_private_directory(product_paths.log_dir, label="log directory")
    staging = _validate_service_staging(staging_path, home=home)
    payload = _launch_agent_payload(
        generation,
        runtime_root,
        product_paths,
        home=home,
    )
    destination = staging / "ai.cortex.devrel.supervisor.plist"
    _atomic_private_bytes(destination, payload)
    return ServiceDefinition(
        path=destination,
        sha256=hashlib.sha256(payload).hexdigest(),
        generation_identity=generation.identity,
    )


@contextmanager
def lifecycle_quiescence(runtime_path: Path, *, timeout: float = 10) -> Iterator[None]:
    """Hold the global lifecycle lock after proving an explicit stopped state."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise LifecycleError("quiescence timeout must be positive and finite")
    runtime_root = _ensure_private_directory(runtime_path, label="lifecycle root")
    deadline = time.monotonic() + timeout
    with _lifecycle_lock(runtime_root, deadline=deadline):
        if _read_record(runtime_root / "lifecycle.json") is not None:
            raise LifecycleError("operation requires the lifecycle to be stopped")
        yield


#: Verifying the running generation is a full `load_generation` -- a ledger
#: re-hash plus the pinned Web closure analysis -- which measures 0.65-1.17 s on
#: the production Mac mini. The old 1 s default therefore made
#: `cortex-dist doctor` structurally incapable of reporting a healthy product:
#: it blew the deadline inside its own verification and answered
#: `front_door_unhealthy` for a front door that was answering 200 in 15 ms.
FRONT_DOOR_PROBE_TIMEOUT = 10.0


class LifecycleManager:
    """Serialize start, status, and stop for one installed generation."""

    def __init__(
        self,
        generation: Path,
        runtime_root: Path | None = None,
        *,
        home: Path,
        environment: Mapping[str, str] | None = None,
        pin_tools: bool = True,
    ) -> None:
        self._pin_tools = pin_tools
        self.generation = load_generation(generation, pin_tools=pin_tools)
        self.home = _normalized_absolute(home)
        try:
            self.product_paths = resolve_installed_product_paths(
                self.generation.root / "runtime",
                home=self.home,
                environment=environment,
            )
        except InstalledProductPathsError as exc:
            raise LifecycleError(str(exc)) from exc
        canonical_runtime = _normalized_absolute(
            self.product_paths.runtime_update_root
        )
        selected_runtime = (
            canonical_runtime
            if runtime_root is None
            else _normalized_absolute(runtime_root)
        )
        if selected_runtime != canonical_runtime:
            raise LifecycleError("operation requires the canonical lifecycle root")
        self.runtime_root = _ensure_private_directory(
            canonical_runtime, label="lifecycle root"
        )
        self.web_settings = _resolve_web_settings(
            self.generation,
            home=self.home,
            config_file=self.product_paths.config_file,
        )
        self.supervisor_python = _service_python(self.generation)
        self.module_root = _safe_component(
            self.generation.root,
            Path("bundle/tools"),
            directory=True,
        )
        self.state_path = self.runtime_root / "lifecycle.json"

    def _current_generation(self) -> InstalledGeneration:
        current = load_generation(self.generation.root, pin_tools=self._pin_tools)
        if current.identity != self.generation.identity:
            raise LifecycleError("generation identity changed after lifecycle binding")
        return current

    def _require_owned_generation(self, record: LifecycleRecord) -> None:
        if (
            record.generation_identity != self.generation.identity
            or record.generation_root != str(self.generation.root)
        ):
            raise LifecycleError("lifecycle runtime is owned by another generation")

    @contextmanager
    def quiescence(self, *, timeout: float = 10) -> Iterator[None]:
        """Hold the lifecycle lock while the runtime is explicitly stopped."""

        with lifecycle_quiescence(self.runtime_root, timeout=timeout):
            yield

    def _describe_front_door(self, status: LifecycleStatus) -> LifecycleStatus:
        if status.state != "running":
            return status
        return replace(
            status,
            web_port_fixed=self.web_settings.port is not None,
            public_origin=self.web_settings.public_origin,
        )

    def status(self) -> LifecycleStatus:
        deadline = time.monotonic() + _STATUS_TIMEOUT
        with _lifecycle_lock(self.runtime_root, deadline=deadline):
            record = _read_record(self.state_path)
            if record is None:
                return LifecycleStatus("stopped")
            return self._describe_front_door(
                _status_for_record(
                    record,
                    self._current_generation(),
                    deadline=deadline,
                )
            )

    def probe_local_front_door(
        self, *, timeout: float = FRONT_DOOR_PROBE_TIMEOUT
    ) -> LocalFrontDoorHealth:
        if not math.isfinite(timeout) or timeout <= 0:
            raise LifecycleError("front-door timeout must be positive and finite")
        deadline = time.monotonic() + timeout
        with _lifecycle_lock(self.runtime_root, deadline=deadline):
            record = _read_record(self.state_path)
            if record is None:
                return LocalFrontDoorHealth(False, None, "stopped")
            try:
                generation = self._current_generation()
            except LifecycleError:
                return LocalFrontDoorHealth(False, None, "generation_unhealthy")
            if (
                record.generation_identity != generation.identity
                or record.generation_root != str(generation.root)
            ):
                return LocalFrontDoorHealth(False, None, "wrong_generation")
            if record.web.build_id != generation.release_build_id:
                return LocalFrontDoorHealth(False, None, "claims_unhealthy")
            claims = (record.supervisor, record.control, record.web)
            if (
                not all(_identity_matches(item, deadline=deadline) for item in claims)
                or not _listener_owned(
                    record.control.pid,
                    record.control.port,
                    deadline=deadline,
                )
                or not _listener_owned(
                    record.web.pid,
                    record.web.port,
                    deadline=deadline,
                )
            ):
                return LocalFrontDoorHealth(False, None, "claims_unhealthy")
            headers = {
                "Accept": "application/json",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            remaining = _remaining_timeout(deadline)
            result = None if remaining <= 0 else _json_request(
                record.web.port,
                "/api/cortex/workspaces",
                timeout=remaining,
                headers=headers,
                deadline=deadline,
            )
            if time.monotonic() >= deadline:
                return LocalFrontDoorHealth(False, None, "front_door_unhealthy")
            # The generation is verified ONCE per probe. `_current_generation`
            # is a full `load_generation` -- a bundle re-hash plus the pinned
            # Web closure analysis -- and running it twice doubled the probe's
            # dominant cost, which is what made the old 1 s deadline
            # unsatisfiable on real hardware.
            #
            # What the second call was for is catching a generation swapped
            # underneath the web request. A swap is an activation, and an
            # activation rewrites the lifecycle record: it is the record that
            # says which generation this runtime belongs to. So the record is
            # re-read and re-compared against the generation already verified,
            # and every invariant the probe compared before is still compared
            # here -- identity, root, build id, all three claims and both
            # listeners.
            current = _read_record(self.state_path)
            if current is None:
                return LocalFrontDoorHealth(False, None, "front_door_unhealthy")
            if (
                time.monotonic() >= deadline
                or current.generation_identity != record.generation_identity
                or current.generation_root != record.generation_root
                or current.web.build_id != record.web.build_id
                or current.web.port != record.web.port
                or current.control.port != record.control.port
                or record.generation_identity != generation.identity
                or record.generation_root != str(generation.root)
                or record.web.build_id != generation.release_build_id
                or result is None
                or result[0] != 200
                or set(result[1]) != {"items", "next_cursor"}
                or not isinstance(result[1]["items"], list)
                or (
                    result[1]["next_cursor"] is not None
                    and not isinstance(result[1]["next_cursor"], str)
                )
                or not all(_identity_matches(item, deadline=deadline) for item in claims)
                or not _listener_owned(
                    record.control.pid,
                    record.control.port,
                    deadline=deadline,
                )
                or not _listener_owned(
                    record.web.pid,
                    record.web.port,
                    deadline=deadline,
                )
            ):
                return LocalFrontDoorHealth(False, None, "front_door_unhealthy")
            return LocalFrontDoorHealth(
                True,
                f"http://127.0.0.1:{record.web.port}",
                "healthy",
            )

    def _supervisor_command(self, *, run_id: str, claim: str, timeout: float) -> list[str]:
        return [
            str(self.supervisor_python),
            "-I",
            "-B",
            "-c",
            _FOREGROUND_BOOTSTRAP,
            str(self.module_root),
            "--generation",
            str(self.generation.root),
            "--runtime-root",
            str(self.runtime_root),
            "--home",
            str(self.home),
            "--config-file",
            str(self.product_paths.config_file),
            "--config-dir",
            str(self.product_paths.config_dir),
            "--data-dir",
            str(self.product_paths.data_dir),
            "--state-dir",
            str(self.product_paths.state_dir),
            "--cache-dir",
            str(self.product_paths.cache_dir),
            "--log-dir",
            str(self.product_paths.log_dir),
            "--run-id",
            run_id,
            "--claim",
            claim,
            "--expected-generation",
            self.generation.identity,
            "--startup-timeout",
            str(timeout),
        ]

    def _cleanup_failed_start(
        self,
        process: subprocess.Popen[bytes],
        *,
        start_token: str | None,
        claim: str,
        deadline: float,
    ) -> None:
        _terminate_spawned(
            process,
            start_token=start_token,
            claim=claim,
            timeout=_remaining_timeout(deadline),
        )
        record = _read_record(self.state_path)
        if record is None or record.supervisor.claim != claim:
            return
        for child in (record.web, record.control):
            if _identity_matches(child, deadline=deadline):
                raise LifecycleError("failed supervisor start left an owned child")
        _remove_matching_control_metadata(
            self.product_paths.state_dir / "cortexd.json",
            record.control,
            deadline=deadline,
        )
        _remove_matching_record(
            self.state_path,
            record.supervisor.claim,
            deadline=deadline,
        )

    def start(self, *, timeout: float = 10) -> LifecycleStatus:
        if not math.isfinite(timeout) or timeout <= 0:
            raise LifecycleError("startup timeout must be positive and finite")
        operation_deadline = time.monotonic() + timeout
        with _lifecycle_lock(self.runtime_root, deadline=operation_deadline):
            generation = self._current_generation()
            existing = _read_record(self.state_path)
            if existing is not None:
                self._require_owned_generation(existing)
                status = _status_for_record(
                    existing,
                    generation,
                    deadline=operation_deadline,
                )
                if status.state == "running":
                    return self._describe_front_door(status)
                self._stop_record(existing, deadline=operation_deadline)
            # ⟦P7⟧ A fixed port that something else holds fails here, typed,
            # rather than as "foreground supervisor exited before readiness"
            # with the reason in `supervisor.log`.
            if self.web_settings.port is not None:
                _require_free_web_port(self.web_settings.port)
            run_id = secrets.token_hex(16)
            claim = secrets.token_hex(32)
            logs = _ensure_private_directory(
                self.product_paths.log_dir, label="log directory"
            )
            supervisor_log = _open_log(logs / "supervisor.log")
            process: subprocess.Popen[bytes] | None = None
            start_token: str | None = None
            try:
                try:
                    process = subprocess.Popen(
                        self._supervisor_command(
                            run_id=run_id,
                            claim=claim,
                            timeout=_remaining_timeout(operation_deadline),
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=supervisor_log,
                        stderr=subprocess.STDOUT,
                        cwd=self.runtime_root,
                        env={
                            "HOME": str(self.home),
                            "LANG": "C",
                            "LC_ALL": "C",
                            "PATH": os.defpath,
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONNOUSERSITE": "1",
                            "PYTHONPATH": "",
                        },
                        close_fds=True,
                        start_new_session=True,
                    )
                finally:
                    supervisor_log.close()
                start_token = _wait_for_start_token(
                    process.pid,
                    deadline=operation_deadline,
                )
                expected = ProcessClaim(process.pid, start_token, claim)
                while time.monotonic() < operation_deadline:
                    if process.poll() is not None:
                        raise LifecycleError(
                            "foreground supervisor exited before readiness"
                        )
                    record = _read_record(self.state_path)
                    if record is not None and record.supervisor == expected:
                        status = _status_for_record(
                            record,
                            generation,
                            deadline=operation_deadline,
                        )
                        if status.state == "running":
                            return self._describe_front_door(status)
                    time.sleep(0.02)
                raise LifecycleError("foreground supervisor readiness timed out")
            except BaseException:
                if process is None:
                    raise
                cleanup_deadline = time.monotonic() + _SUPERVISOR_CLEANUP_TIMEOUT
                try:
                    self._cleanup_failed_start(
                        process,
                        start_token=start_token,
                        claim=claim,
                        deadline=cleanup_deadline,
                    )
                except BaseException as cleanup_exc:
                    raise LifecycleError(
                        f"failed supervisor startup could not be cleaned: {cleanup_exc}"
                    ) from cleanup_exc
                raise

    def _stop_record(self, record: LifecycleRecord, *, deadline: float) -> None:
        if _identity_matches(record.supervisor, deadline=deadline):
            _request_supervisor_shutdown(
                self.runtime_root,
                record,
                deadline=deadline,
            )
        for process in (record.web, record.control):
            if _identity_matches(process, deadline=deadline):
                raise LifecycleError("supervisor shutdown left an owned child process")
        _remove_matching_control_metadata(
            self.product_paths.state_dir / "cortexd.json",
            record.control,
            deadline=deadline,
        )
        _remove_matching_record(
            self.state_path,
            record.supervisor.claim,
            deadline=deadline,
        )

    def stop(self, *, timeout: float = 10) -> LifecycleStatus:
        if not math.isfinite(timeout) or timeout < 0:
            raise LifecycleError("shutdown timeout must be finite and not negative")
        deadline = time.monotonic() + timeout
        with _lifecycle_lock(self.runtime_root, deadline=deadline):
            record = _read_record(self.state_path)
            if record is None:
                return LifecycleStatus("stopped")
            self._require_owned_generation(record)
            self._stop_record(record, deadline=deadline)
            return LifecycleStatus("stopped")
