"""Closed R0 product composition manifest and host Node policy.

The Python side of the composition — the fd-bound unpack, the seal, the Darwin
linkage inspection and the probe that makes one staged CPython tree describe
itself — lives in `cortex_platform.runtime_staging`, because the installed
product must run it from the wheel and cannot import `distribution`. It is
imported back here under the names this module has always published, so every
caller, constant and error identity is unchanged; the profile parameter those
primitives now take defaults to the product's own cp314 runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# The staging kernel, re-exported under the names this module has always
# published. The names below are this module's public surface as much as the
# functions defined here are, so they are imported even where nothing in this
# file uses them.
from cortex_platform.runtime_staging import (  # noqa: F401 - published surface
    PYTHON_MAX_ARCHIVE_BYTES,
    PYTHON_MAX_EXTRACTED_BYTES,
    PYTHON_MAX_MEMBER_BYTES,
    PYTHON_MAX_MEMBER_DEPTH,
    PYTHON_MAX_MEMBERS,
    PYTHON_MIN_ARCHIVE_BYTES,
    PYTHON_PREFIX_MARKER,
    PYTHON_REQUIRED_MODULES,
    EmbeddedLinkageInspector,
    OtoolRunner,
    PythonRunner,
    PythonRuntime,
    PythonRuntimeProfile,
    _darwin_embedded_linkage,
    _descriptor_sha256,
    _is_system_library_path,
    _OTOOL_DEPENDENCY_ROW,
    _python_release,
    _r0_architecture,
    _reject_extended_acl,
    _SHA256,
    _validate_executable_parent_chain,
    _verify_bound_stage_root,
    _bind_private_stage_root,
    probe_python_runtime,
    stage_python_runtime,
)
from cortex_platform.runtime_staging import CP314_PROFILE as PRODUCT_PYTHON_PROFILE

# The kernel raises under the name this module has raised since it was written,
# so every `except ProductManifestError` and every test is unaffected.
from cortex_platform.runtime_staging import RuntimeStagingError as ProductManifestError

from .schema import ClosedSchemaError, canonical_json_bytes, exact_mapping

# The product's embedded runtime policy, published under the names this module
# has always exported. They are the cp314 profile's fields, not a second
# transcription of them.
PYTHON_SUPPORTED_RELEASE = PRODUCT_PYTHON_PROFILE.supported_release
PYTHON_INTERPRETER_RELATIVE = PRODUCT_PYTHON_PROFILE.interpreter_relative
PYTHON_LIBRARY_RELATIVE = PRODUCT_PYTHON_PROFILE.library_relative
PYTHON_LIBRARY_ID = PRODUCT_PYTHON_PROFILE.library_id
PYTHON_EXPECTED_RPATHS = PRODUCT_PYTHON_PROFILE.expected_rpaths

NODE_SUPPORTED_RELEASES = {22: (22, 13, 0), 24: (24, 0, 0), 26: (26, 0, 0)}
NODE_VERSION_RANGE = "22.13+ LTS, 24.x LTS, or 26.x current"
NODE_MIN_EXECUTABLE_BYTES = 32 * 1024
NODE_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
NODE_ADAPTER_VERSION = 1

PRODUCT_MANIFEST_SCHEMA = 3

_NODE_VERSION = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_NODE_REPORT_FIELDS = {
    "architecture",
    "execPath",
    "platform",
    "sharedObjects",
    "version",
}
_NODE_RUNTIME_FIELDS = {
    "architecture",
    "embedded",
    "execution_ownership",
    "executable_sha256",
    "platform",
    "policy",
    "runtime_mode",
    "version",
}
_NODE_POLICY_FIELDS = {
    "adapter_version",
    "allowed_runtime_imports",
    "dynamic_dependency_policy",
    "forbidden_installed_commands",
    "identity_claim",
    "identity_threat_model",
    "path_requirements",
    "supported_platform",
    "version_range",
}
_PYTHON_RUNTIME_FIELDS = {
    "archive_sha256",
    "architecture",
    "embedded",
    "execution_ownership",
    "interpreter_sha256",
    "platform",
    "policy",
    "runtime_mode",
    "venv_interpreter_sha256",
    "version",
}
_PYTHON_POLICY_FIELDS = {
    "dynamic_dependency_policy",
    "flavour",
    "host_interpreter_required",
    "identity_claim",
    "relocation",
    "runtime",
    "source_project",
    "supported_architecture",
    "supported_platform",
    "venv_mode",
}
_PRODUCT_FIELDS_V1 = {
    "capabilities",
    "listeners",
    "node_runtime",
    "processes",
    "release_class",
    "schema_version",
    "state_ownership",
}
_PRODUCT_FIELDS_V2 = _PRODUCT_FIELDS_V1 | {"python_runtime"}
#: Schema 3 has the same closed top-level field set as schema 2 -- it differs
#: only inside `capabilities`, which the contract comparison below pins exactly.
_PRODUCT_FIELDS_BY_SCHEMA = {
    1: _PRODUCT_FIELDS_V1,
    2: _PRODUCT_FIELDS_V2,
    3: _PRODUCT_FIELDS_V2,
}


@dataclass(frozen=True)
class NodeRuntime:
    """A compatible Node executable staged into an installer generation."""

    version: str
    platform: str
    architecture: str
    executable_sha256: str

    def as_manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "platform": self.platform,
            "architecture": self.architecture,
            "embedded": False,
            "runtime_mode": "staged-host-node",
            "execution_ownership": "installer-owned-generation",
            "executable_sha256": self.executable_sha256,
            "policy": {
                "version_range": NODE_VERSION_RANGE,
                "adapter_version": NODE_ADAPTER_VERSION,
                "allowed_runtime_imports": ["node-builtins", "relative-payload"],
                "dynamic_dependency_policy": "system-libraries-only",
                "forbidden_installed_commands": ["npm", "npx", "vinext", "wrangler"],
                "identity_claim": "opened-source-fd-to-immutable-generation-copy",
                "identity_threat_model": (
                    "same-user-stage-root-destination-replacement-and-in-place-source-"
                    "mutation-excluded-r0"
                ),
                "path_requirements": [
                    "opened-source-file-descriptor",
                    "installer-owned-private-generation-directory",
                    "atomic-no-clobber-generation-publish",
                    "staged-regular-single-link-not-writable-by-caller",
                ],
                "supported_platform": "darwin",
            },
        }


NodeRunner = Callable[[Path], tuple[int, str, str]]


def _default_node_runner(executable: Path) -> tuple[int, str, str]:
    program = (
        "const report=process.report.getReport();"
        "process.stdout.write(JSON.stringify({"
        "architecture:process.arch,execPath:process.execPath,"
        "platform:process.platform,sharedObjects:report.sharedObjects,"
        "version:process.version}))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "--no-addons", "--input-type=module", "-e", program],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={
                "HOME": "",
                "LANG": "C",
                "LC_ALL": "C",
                "NODE_OPTIONS": "",
                "NODE_PATH": "",
                "PATH": str(executable.parent),
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProductManifestError("Node runtime inspection failed") from exc
    return completed.returncode, completed.stdout, completed.stderr


def _open_regular_executable(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ProductManifestError("Node executable is unavailable or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ProductManifestError("Node executable is not a single regular file")
        if details.st_mode & 0o111 == 0 or details.st_mode & (
            stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
        ):
            raise ProductManifestError("Node executable is not executable")
        if not NODE_MIN_EXECUTABLE_BYTES <= details.st_size <= NODE_MAX_EXECUTABLE_BYTES:
            raise ProductManifestError("Node executable size is unsafe")
        if details.st_uid not in {0, os.geteuid()} or _mode_writable_by_effective_user(
            details
        ):
            raise ProductManifestError("Node executable ownership or mode is unsafe")
        _reject_extended_acl((path,), "Node executable")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _mode_writable_by_effective_user(details: os.stat_result) -> bool:
    effective_user = os.geteuid()
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return True
    return bool(
        details.st_mode & stat.S_IWUSR
        and (effective_user == 0 or details.st_uid == effective_user)
    )


def _source_descriptor(path: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ProductManifestError("Node source executable is unavailable or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_mode & 0o111 == 0
            or details.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            or not NODE_MIN_EXECUTABLE_BYTES
            <= details.st_size
            <= NODE_MAX_EXECUTABLE_BYTES
            or details.st_uid not in {0, os.geteuid()}
            or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ProductManifestError("Node source executable identity is unsafe")
        _reject_extended_acl((path,), "Node source executable")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, details


def _open_bound_staged_runtime(
    name: str,
    *,
    root_descriptor: int,
    expected: os.stat_result,
    expected_digest: str,
) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
    except OSError as exc:
        raise ProductManifestError("Node staged runtime identity changed") from exc
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o500
            or observed.st_nlink != 1
            or (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
            )
            != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
                expected.st_mtime_ns,
            )
            or _descriptor_sha256(descriptor) != expected_digest
        ):
            raise ProductManifestError("Node staged runtime identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


StageHook = Callable[[Path], None]
LinkageInspector = Callable[[Path], None]


def _darwin_system_linkage(executable: Path) -> None:
    try:
        completed = subprocess.run(
            ["/usr/bin/otool", "-L", str(executable)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProductManifestError("Node dynamic linkage inspection failed") from exc
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 1024 * 1024:
        raise ProductManifestError("Node dynamic linkage inspection failed")
    lines = completed.stdout.splitlines()
    if not lines or lines[0] != f"{executable}:" or len(lines) < 2:
        raise ProductManifestError("Node dynamic linkage report is invalid")
    dependencies: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _OTOOL_DEPENDENCY_ROW.fullmatch(line)
        if match is None:
            raise ProductManifestError("Node dynamic linkage report is invalid")
        dependency = match.group(1)
        if " (compatibility version " in dependency:
            raise ProductManifestError("Node dynamic linkage report is invalid")
        dependencies.append(dependency)
    if not dependencies or any(not _is_system_library_path(item) for item in dependencies):
        raise ProductManifestError("unsupported_dynamic_closure")


def stage_node_runtime(
    source: Path,
    destination: Path,
    *,
    stage_root: Path,
    run: NodeRunner = _default_node_runner,
    expected_system: str | None = None,
    expected_machine: str | None = None,
    after_source_open: StageHook | None = None,
    before_publish: StageHook | None = None,
    inspect_linkage: LinkageInspector = _darwin_system_linkage,
) -> NodeRuntime:
    """Copy one opened host executable into a private immutable generation."""

    _r0_architecture(expected_system=expected_system, expected_machine=expected_machine)
    try:
        resolved_source = source.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProductManifestError("Node source executable is unavailable or unsafe") from exc
    try:
        destination_value = os.fspath(destination.expanduser())
        if "\0" in destination_value:
            raise ValueError("embedded null character")
        destination = Path(os.path.abspath(destination_value))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProductManifestError("Node stage destination is invalid") from exc
    bound_root, root_descriptor, root_identity = _bind_private_stage_root(stage_root)
    if destination.parent != bound_root or destination.name in {"", ".", ".."}:
        os.close(root_descriptor)
        raise ProductManifestError("Node stage destination is outside its generation")
    try:
        os.stat(destination.name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        os.close(root_descriptor)
        raise ProductManifestError("Node stage destination is unsafe") from exc
    else:
        os.close(root_descriptor)
        raise ProductManifestError("Node stage destination already exists")
    try:
        source_descriptor, source_before = _source_descriptor(resolved_source)
    except BaseException:
        os.close(root_descriptor)
        raise
    temporary_name: str | None = None
    staged_descriptor: int | None = None
    destination_published = False
    try:
        if after_source_open is not None:
            after_source_open(resolved_source)
        temporary_name = f".{destination.name}.{secrets.token_hex(16)}"
        output_descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        try:
            digest = hashlib.sha256()
            copied_bytes = 0
            while chunk := os.read(source_descriptor, 1024 * 1024):
                digest.update(chunk)
                copied_bytes += len(chunk)
                if copied_bytes > NODE_MAX_EXECUTABLE_BYTES:
                    raise ProductManifestError("Node source executable size is unsafe")
                view = memoryview(chunk)
                while view:
                    written = os.write(output_descriptor, view)
                    if written <= 0:
                        raise ProductManifestError("Node stage copy did not make progress")
                    view = view[written:]
            os.fchmod(output_descriptor, 0o500)
            os.fsync(output_descriptor)
            expected_digest = digest.hexdigest()
            if expected_digest != _descriptor_sha256(output_descriptor):
                raise ProductManifestError("Node staged copy digest mismatch")
            staged_identity = os.fstat(output_descriptor)
        finally:
            os.close(output_descriptor)
        source_after = os.fstat(source_descriptor)
        if (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_mtime_ns,
        ) != (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_size,
            source_after.st_mtime_ns,
        ):
            raise ProductManifestError("Node source executable changed during staging")
        if copied_bytes != source_before.st_size:
            raise ProductManifestError("Node source executable size changed during staging")
        temporary_path = bound_root / temporary_name
        inspect_linkage(temporary_path)
        runtime = probe_node_runtime(
            temporary_path,
            run=run,
            expected_system=expected_system,
            expected_machine=expected_machine,
        )
        if runtime.executable_sha256 != expected_digest:
            raise ProductManifestError("Node staged runtime identity mismatch")
        _verify_bound_stage_root(bound_root, root_descriptor, root_identity)
        if before_publish is not None:
            before_publish(destination)
        _verify_bound_stage_root(bound_root, root_descriptor, root_identity)
        staged_descriptor = _open_bound_staged_runtime(
            temporary_name,
            root_descriptor=root_descriptor,
            expected=staged_identity,
            expected_digest=expected_digest,
        )
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ProductManifestError("Node stage destination already exists") from exc
        destination_published = True
        os.unlink(temporary_name, dir_fd=root_descriptor)
        temporary_name = None
        if os.fstat(staged_descriptor).st_nlink != 1:
            raise ProductManifestError("Node staged runtime identity changed")
        os.fsync(root_descriptor)
        return runtime
    except BaseException:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass
        if destination_published and staged_descriptor is not None:
            try:
                published = os.stat(
                    destination.name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                opened = os.fstat(staged_descriptor)
                if (published.st_dev, published.st_ino) == (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    os.unlink(destination.name, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass
        os.fsync(root_descriptor)
        raise
    finally:
        if staged_descriptor is not None:
            os.close(staged_descriptor)
        os.close(source_descriptor)
        os.close(root_descriptor)


def _node_report(executable: Path, run: NodeRunner) -> dict[str, object]:
    returncode, stdout, _stderr = run(executable)
    if returncode != 0 or len(stdout.encode("utf-8")) > 1024 * 1024:
        raise ProductManifestError("Node runtime inspection failed")
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProductManifestError("Node runtime report is invalid") from exc
    if not isinstance(report, dict) or set(report) != _NODE_REPORT_FIELDS:
        raise ProductManifestError("Node runtime report schema is invalid")
    try:
        reported_executable = Path(report["execPath"]).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise ProductManifestError("Node runtime executable identity is invalid") from exc
    if reported_executable != executable:
        raise ProductManifestError("Node runtime executable identity changed during inspection")
    shared_objects = report["sharedObjects"]
    if (
        not isinstance(shared_objects, list)
        or not shared_objects
        or len(shared_objects) > 4096
        or any(not isinstance(item, str) or len(item) > 4096 for item in shared_objects)
    ):
        raise ProductManifestError("Node shared-object report is invalid")
    allowed_self = str(executable)
    if allowed_self not in shared_objects or any(
        item != allowed_self and not _is_system_library_path(item)
        for item in shared_objects
    ):
        raise ProductManifestError("unsupported_dynamic_closure")
    return report


def verify_staged_node_runtime(
    executable: Path,
    expected: NodeRuntime,
    *,
    stage_root: Path,
    run: NodeRunner = _default_node_runner,
    expected_system: str | None = None,
    expected_machine: str | None = None,
) -> NodeRuntime:
    """Re-inspect an installed generation and require its exact manifest identity."""

    bound_root, root_descriptor, root_identity = _bind_private_stage_root(stage_root)
    executable_descriptor: int | None = None
    try:
        resolved_executable = Path(
            os.path.abspath(os.fspath(executable.expanduser()))
        )
        if (
            resolved_executable.parent != bound_root
            or resolved_executable.name in {"", ".", ".."}
        ):
            raise ProductManifestError("installed Node runtime is outside its generation")
        try:
            executable_descriptor = os.open(
                resolved_executable.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
        except OSError as exc:
            raise ProductManifestError("installed Node runtime is unsafe") from exc
        opened = os.fstat(executable_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o500
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
        ):
            raise ProductManifestError("installed Node runtime is unsafe")
        if not NODE_MIN_EXECUTABLE_BYTES <= opened.st_size <= NODE_MAX_EXECUTABLE_BYTES:
            raise ProductManifestError("installed Node runtime size is unsafe")
        _reject_extended_acl((resolved_executable,), "installed Node runtime")
        observed = probe_node_runtime(
            resolved_executable,
            run=run,
            expected_system=expected_system,
            expected_machine=expected_machine,
        )
        try:
            after = os.stat(
                resolved_executable.name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProductManifestError("installed Node runtime identity changed") from exc
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ):
            raise ProductManifestError("installed Node runtime identity changed")
        _verify_bound_stage_root(bound_root, root_descriptor, root_identity)
    finally:
        if executable_descriptor is not None:
            os.close(executable_descriptor)
        os.close(root_descriptor)
    if observed != expected:
        raise ProductManifestError("installed Node runtime does not match the manifest")
    return observed


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _NODE_VERSION.fullmatch(value)
    if match is None:
        raise ProductManifestError("Node reported an invalid version")
    major, minor, patch = (int(item) for item in match.groups())
    return major, minor, patch


def _version_supported(value: str) -> bool:
    version = _version_tuple(value)
    minimum = NODE_SUPPORTED_RELEASES.get(version[0])
    return minimum is not None and version >= minimum and version[0] == minimum[0]


def probe_node_runtime(
    executable: Path,
    *,
    run: NodeRunner = _default_node_runner,
    expected_system: str | None = None,
    expected_machine: str | None = None,
) -> NodeRuntime:
    """Inspect one installer-staged Node path before and after execution."""

    expected_architecture = _r0_architecture(
        expected_system=expected_system,
        expected_machine=expected_machine,
    )
    try:
        resolved = executable.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProductManifestError("Node executable is unavailable or unsafe") from exc
    _validate_executable_parent_chain(resolved)
    descriptor = _open_regular_executable(resolved)
    try:
        before = os.fstat(descriptor)
        report = _node_report(resolved, run)
        after = os.stat(resolved, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProductManifestError("Node runtime executable changed during inspection")
        version = str(report["version"])
        if not _version_supported(version):
            raise ProductManifestError(f"Node {version} is outside {NODE_VERSION_RANGE}")
        expected_platform = "darwin"
        if report["platform"] != expected_platform:
            raise ProductManifestError("Node runtime platform is incompatible")
        if report["architecture"] != expected_architecture:
            raise ProductManifestError("Node runtime architecture is incompatible")
        digest = _descriptor_sha256(descriptor)
    finally:
        os.close(descriptor)
    return NodeRuntime(
        version=version,
        platform=expected_platform,
        architecture=expected_architecture,
        executable_sha256=digest,
    )


def _product_contract(interpreter_runtime: str, schema_version: int) -> dict[str, object]:
    """Return the non-runtime half of one product-manifest schema version."""

    return {
        "schema_version": schema_version,
        "release_class": "unsigned-r0-developer-candidate",
        "processes": {
            "control": {
                "required": True,
                "enabled_by_default": True,
                "runtime": interpreter_runtime,
                "entrypoint": "runtime/bin/cortexd",
                "listener": "control-loopback",
            },
            "web": {
                "required": True,
                "enabled_by_default": True,
                "runtime": "staged-host-node",
                "runtime_entrypoint": "node-runtime/bin/node",
                "entrypoint": "web/server/node-adapter.mjs",
                "listener": "web-loopback",
            },
            "private_access": {
                "required": False,
                "enabled_by_default": False,
                "runtime": interpreter_runtime,
                "entrypoint": "runtime/bin/cortex-private-access-supervisor",
                "listener": "private-access-loopback",
            },
        },
        "listeners": {
            "control-loopback": {
                "address": "127.0.0.1",
                "port": "supervisor-allocated",
                "browser_visible": False,
            },
            "web-loopback": {
                "address": "127.0.0.1",
                "port": "supervisor-allocated",
                "browser_visible": True,
            },
            "private-access-loopback": {
                "address": "127.0.0.1",
                "port": "disabled",
                "browser_visible": False,
            },
        },
        "capabilities": {
            "control_api": "available",
            "web_pwa": "available",
            "runtime_update": "available-disabled",
            "runtime_dispatch": "disabled",
            "private_access": "disabled",
            "telegram": "disabled",
            "providers": "disabled",
            "ingestion": "disabled",
        },
        "state_ownership": {
            "installer_owned": [
                "code-generations",
                "launchers",
                "service-definitions",
                "version-pointers",
            ],
            "user_owned_preserved": [
                "config",
                "control-db",
                "identity-key",
                "artifact-roots",
                # The corpus asset roots, named after the legacy `~/gdrive/...`
                # default they replaced. Frozen: see the schema table below.
                "gdrive-roots",
                "hermes-slots",
                "logs",
            ],
        },
    }


# The non-runtime half of the manifest, FROZEN one entry per schema version.
#
# `validate_product_manifest` is not run only by the code that composed a
# generation: it is run by whatever `cortex-dist` executes NEXT — the next
# release's `doctor`, `upgrade` and rollback all validate the generation that is
# already installed. A literal that MOVED under a live schema would therefore
# make every previously installed generation fail validation under the newer
# tools (`installation_unhealthy`, and an upgrade that cannot run), and would
# also change that generation's identity-hash input. Schema 1 already carried
# that rule in prose; this table makes it structural.
#
# Adding, removing, renaming or re-valuing ANY entry below therefore requires a
# NEW schema version, leaving the older entries untouched forever. A generation
# is always validated against the entry for the schema it declares — the
# contract it was itself composed against — never against the newest one.
def _product_contract_v3() -> dict[str, object]:
    """Schema 3: schema 2 plus A1-8's `telegram_adapter` capability row.

    A new version rather than an edit, for the reason the verifier fix exists:
    a manifest composed by an earlier generation is measured against ITS
    version, so schema 2's bytes have to stay exactly what they were. Growing
    the capabilities map in place would have made every already-installed
    composed generation fail `load_generation` under a newer `cortex-dist`.

    `disabled`, not `available-disabled`: the vocabulary's "constructed but not
    enabled" is what a generation that builds a `TelegramAdapter` will say, and
    the R0 composition does not build one by default. The row exists so that
    wiring one up is a manifest change a reviewer sees rather than a health
    boolean quietly becoming true.
    """

    contract = copy.deepcopy(_product_contract("embedded-python", 3))
    capabilities = contract["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["telegram_adapter"] = "disabled"
    return contract


_PRODUCT_CONTRACT_BY_SCHEMA: dict[int, dict[str, object]] = {
    1: _product_contract("installed-python", 1),
    2: _product_contract("embedded-python", 2),
    3: _product_contract_v3(),
}


def _product_manifest_value(
    node_runtime: NodeRuntime,
    python_runtime: PythonRuntime | None = None,
    *,
    schema_version: int | None = None,
) -> dict[str, object]:
    # A generation whose Python came from the bundle names a different process
    # runtime, so its identity input differs from a legacy generation's.
    # `schema_version` selects the frozen contract explicitly, so a manifest
    # composed by an earlier generation is measured against ITS version rather
    # than against whatever the running code composes today.
    if schema_version is None:
        schema_version = PRODUCT_MANIFEST_SCHEMA if python_runtime is not None else 1
    contract = _PRODUCT_CONTRACT_BY_SCHEMA.get(schema_version)
    if contract is None:
        raise ProductManifestError("product manifest version is unsupported")
    manifest: dict[str, object] = copy.deepcopy(contract)
    manifest["node_runtime"] = node_runtime.as_manifest()
    if python_runtime is not None:
        manifest["python_runtime"] = python_runtime.as_manifest()
    return manifest


def build_product_manifest(
    node_runtime: NodeRuntime,
    python_runtime: PythonRuntime | None = None,
) -> dict[str, object]:
    """Return the closed, inactive-by-default R0 process and ownership contract."""

    manifest = _product_manifest_value(node_runtime, python_runtime)
    validate_product_manifest(manifest)
    return manifest


def _validated_python_runtime(value: object) -> PythonRuntime:
    try:
        python = exact_mapping(value, _PYTHON_RUNTIME_FIELDS, label="Python runtime")
        exact_mapping(python["policy"], _PYTHON_POLICY_FIELDS, label="Python policy")
    except ClosedSchemaError as exc:
        raise ProductManifestError(str(exc)) from exc
    if (
        python["embedded"] is not True
        or python["runtime_mode"] != "embedded-cpython"
        or python["execution_ownership"] != "installer-owned-generation"
        or python["platform"] != "darwin"
        or python["architecture"] != "arm64"
        or not _SHA256.fullmatch(str(python["interpreter_sha256"]))
        or not _SHA256.fullmatch(str(python["archive_sha256"]))
        or not _SHA256.fullmatch(str(python["venv_interpreter_sha256"]))
    ):
        raise ProductManifestError("Python runtime policy is invalid")
    runtime = PythonRuntime(
        version=str(python["version"]),
        platform=str(python["platform"]),
        architecture=str(python["architecture"]),
        interpreter_sha256=str(python["interpreter_sha256"]),
        archive_sha256=str(python["archive_sha256"]),
        venv_interpreter_sha256=str(python["venv_interpreter_sha256"]),
    )
    if _python_release(runtime.version) != PYTHON_SUPPORTED_RELEASE:
        raise ProductManifestError("Python runtime version is unsupported")
    if python["policy"] != runtime.as_manifest()["policy"]:
        raise ProductManifestError("Python runtime policy is invalid")
    return runtime


def validate_product_manifest(value: object) -> dict[str, object]:
    """Validate the complete closed R0 manifest and return a plain copy.

    The reference is the frozen contract for the schema the manifest DECLARES,
    so a generation installed by an earlier release keeps validating under a
    later `cortex-dist` even after the contract has grown a new version.
    """

    if not isinstance(value, Mapping) or value.get("schema_version") not in _PRODUCT_FIELDS_BY_SCHEMA:
        raise ProductManifestError("product manifest version is unsupported")
    schema_version = int(value["schema_version"])  # type: ignore[arg-type]
    try:
        manifest = exact_mapping(
            value, _PRODUCT_FIELDS_BY_SCHEMA[schema_version], label="product manifest"
        )
        node = exact_mapping(
            manifest["node_runtime"], _NODE_RUNTIME_FIELDS, label="Node runtime"
        )
        policy = exact_mapping(node["policy"], _NODE_POLICY_FIELDS, label="Node policy")
    except ClosedSchemaError as exc:
        raise ProductManifestError(str(exc)) from exc
    if manifest["release_class"] != "unsigned-r0-developer-candidate":
        raise ProductManifestError("product release class is invalid")
    if (
        node["embedded"] is not False
        or node["runtime_mode"] != "staged-host-node"
        or node["execution_ownership"] != "installer-owned-generation"
        or not _NODE_VERSION.fullmatch(str(node["version"]))
        or not _SHA256.fullmatch(str(node["executable_sha256"]))
        or node["platform"] != "darwin"
        or node["architecture"] != "arm64"
        or policy != NodeRuntime("v22.13.0", "darwin", "arm64", "0" * 64).as_manifest()["policy"]
    ):
        raise ProductManifestError("Node runtime policy is invalid")
    if not _version_supported(str(node["version"])):
        raise ProductManifestError("Node runtime version is unsupported")

    # Keep every non-runtime field literal and closed so installed code cannot
    # advertise capabilities that the R0 composition does not own.
    reference_runtime = NodeRuntime(
        version=str(node["version"]),
        platform=str(node["platform"]),
        architecture=str(node["architecture"]),
        executable_sha256=str(node["executable_sha256"]),
    )
    reference_python = (
        _validated_python_runtime(manifest["python_runtime"])
        if "python_runtime" in manifest
        else None
    )
    reference = _product_manifest_value(
        reference_runtime, reference_python, schema_version=schema_version
    )
    if canonical_json_bytes(manifest) != canonical_json_bytes(reference):
        raise ProductManifestError("product manifest contract is invalid")
    return json.loads(canonical_json_bytes(manifest))
