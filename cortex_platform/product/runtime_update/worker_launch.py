"""Supervisor-side descriptor construction from persisted updater evidence."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Mapping

from .models import ValidationError, WorkerRuntime, digest_document
from .service import AttemptPin, RuntimeUpdatePaths, RuntimeUpdateService
from .worker_protocol import PROTOCOL_V2, SlotInterpreterDescriptor


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_FIELDS = {"schema_version", "releases"}
_RELEASE_FIELDS = {
    "status",
    "release_sequence",
    "slot_digest",
    "manifest_digest",
    "content_tree_digest",
    "candidate_generation",
}


class WorkerLaunchError(RuntimeError):
    """Pinned worker launch evidence is unavailable or inconsistent."""


def derive_worker_state_dir(
    paths: RuntimeUpdatePaths,
    release_id: str,
    generation_id: str,
) -> Path:
    """Compose the worker namespace within one validated state generation."""

    _identifier(release_id, "release identifier")
    _identifier(generation_id, "generation identifier")
    return paths.generations / release_id / generation_id / ".cortex-worker"


def build_descriptor(
    service: RuntimeUpdateService,
    attempt_id: str,
) -> SlotInterpreterDescriptor:
    """Build a worker descriptor using validating reads of persisted evidence.

    The interpreter used to be the one caller-supplied, unverified parameter of
    this function: everything else was re-derived from the pin, the registry and
    the slot's own manifest, and the path the worker would execute was whatever
    the caller passed. S3.2 removes the parameter. The release says which
    interpreter it needs, `stage` expanded exactly that archive into a root named
    by its digest, and both the path and the expected interpreter digest are read
    back out of that durable evidence — so a descriptor cannot name an
    interpreter the release never carried.
    """

    try:
        pin = service.attempt_pin(attempt_id)
    except Exception as exc:
        raise WorkerLaunchError("attempt pin is unavailable") from exc
    if pin is None:
        raise WorkerLaunchError("attempt pin is unavailable")
    return _descriptor_from_pin(service, pin)


#: The attempt id `build_active_descriptor` reports. It names no attempt row and
#: never reaches the filesystem; it exists because `AttemptPin` carries the field.
ACTIVE_PIN_ID = "active"


def build_active_descriptor(
    service: RuntimeUpdateService,
) -> SlotInterpreterDescriptor:
    """The active slot's descriptor, with no attempt row and no ledger write.

    `build_descriptor` is attempt-pinned: it reads a pin some caller committed,
    which is right for a run that must be answerable for which runtime served
    it. A pre-window capability assertion is the opposite situation — it happens
    outside any run, must leave the updater's evidence exactly as it found it,
    and would otherwise have to invent an attempt id and then remember to
    `finish_attempt` it on every failure path.

    `preview_attempt_pin` already resolves the ACTIVE pointer's identity and
    re-derives `slot_id`, `artifact_digest` and `worker_protocol` from the
    slot's own manifest without writing anything, so the only difference from
    the attempt-pinned path is where the pin came from. Everything below —
    registry, manifest digest, interpreter pin, state generation — is the same
    code, and because `derive_worker_state_dir` depends only on the release and
    the generation, the descriptor is identical to the one any attempt pinned to
    this same active runtime would produce.
    """

    try:
        pin = service.preview_attempt_pin(ACTIVE_PIN_ID)
    except Exception as exc:
        raise WorkerLaunchError("active runtime is unavailable") from exc
    return _descriptor_from_pin(service, pin)


def descriptor_document(descriptor: SlotInterpreterDescriptor) -> dict[str, object]:
    """The exact JSON `SlotInterpreterDescriptor.load` accepts back.

    A supervisor takes a descriptor *path*, so anything that launches a worker
    has to serialize one first. Until now every caller wrote that dict by hand,
    which is how a field gets forgotten in a place `load` will only refuse at
    launch time.
    """

    return {
        "schema_version": descriptor.schema_version,
        "slot_path": str(descriptor.slot_path),
        "slot_id": descriptor.slot_id,
        "state_generation_id": descriptor.state_generation_id,
        "release_id": descriptor.release_id,
        "expected_artifact_digest": descriptor.expected_artifact_digest,
        "expected_manifest_sha256": descriptor.expected_manifest_sha256,
        "expected_content_tree_sha256": descriptor.expected_content_tree_sha256,
        "expected_interpreter_sha256": descriptor.expected_interpreter_sha256,
        "interpreter_path": str(descriptor.interpreter_path),
        "worker_entrypoint": descriptor.worker_entrypoint,
        "state_dir": str(descriptor.state_dir),
        "worker_protocol": descriptor.worker_protocol,
    }


def _descriptor_from_pin(
    service: RuntimeUpdateService,
    pin: AttemptPin,
) -> SlotInterpreterDescriptor:
    registry = _read_json(service.paths.registry, "runtime registry")
    if not isinstance(registry, dict) or set(registry) != _REGISTRY_FIELDS:
        raise WorkerLaunchError("runtime registry schema is invalid")
    if registry.get("schema_version") != 1 or type(registry.get("schema_version")) is not int:
        raise WorkerLaunchError("runtime registry schema is invalid")
    releases = registry.get("releases")
    if not isinstance(releases, dict):
        raise WorkerLaunchError("runtime registry schema is invalid")
    record = releases.get(pin.release_id)
    if not isinstance(record, dict) or set(record) != _RELEASE_FIELDS:
        raise WorkerLaunchError("runtime registry release record is invalid")

    slot_digest = _digest(record, "slot_digest")
    manifest_digest = _digest(record, "manifest_digest")
    content_tree_digest = _digest(record, "content_tree_digest")
    if slot_digest != pin.slot_digest or slot_digest != pin.artifact_digest:
        raise WorkerLaunchError("runtime registry slot digest mismatch")

    slot_path = service.paths.slots / pin.slot_digest
    manifest = _read_json(slot_path / "manifest.json", "slot manifest")
    if digest_document(manifest) != manifest_digest:
        raise WorkerLaunchError("slot manifest digest mismatch")
    if not isinstance(manifest, dict):
        raise WorkerLaunchError("slot manifest is invalid")
    worker_entrypoint = manifest.get("worker_entrypoint")
    if not isinstance(worker_entrypoint, str) or not worker_entrypoint or "\0" in worker_entrypoint:
        raise WorkerLaunchError("slot manifest worker entrypoint is invalid")
    try:
        runtime = WorkerRuntime.from_dict(manifest.get("worker_runtime"))
    except ValidationError as exc:
        raise WorkerLaunchError("slot manifest worker runtime is invalid") from exc
    interpreter_path, expected_interpreter_sha256 = _staged_interpreter(service, runtime)

    if pin.worker_protocol != PROTOCOL_V2:
        raise WorkerLaunchError("worker_protocol_unsupported")

    state_dir = derive_worker_state_dir(
        service.paths,
        pin.release_id,
        pin.generation_id,
    )
    _validate_generation(state_dir.parent)

    return SlotInterpreterDescriptor(
        schema_version=1,
        slot_path=slot_path,
        slot_id=pin.slot_id,
        state_generation_id=pin.generation_id,
        release_id=pin.release_id,
        expected_artifact_digest=pin.artifact_digest,
        expected_manifest_sha256=manifest_digest,
        expected_content_tree_sha256=content_tree_digest,
        expected_interpreter_sha256=expected_interpreter_sha256,
        interpreter_path=interpreter_path,
        worker_entrypoint=worker_entrypoint,
        state_dir=state_dir,
        worker_protocol=PROTOCOL_V2,
    )


def _staged_interpreter(
    service: RuntimeUpdateService, runtime: WorkerRuntime
) -> tuple[Path, str]:
    """The interpreter `stage` expanded for this release, and its measured digest.

    Two independent reads for two different questions. The path comes from the
    manifest — `interpreters/<archive_sha256>/<interpreter_relative>` — and is
    asserted to resolve strictly inside that root (⟦AMD-5⟧), so a manifest the
    grammar accepted still cannot compose a path out of it. The digest comes from
    the pin `stage` wrote beside the root, which is where the value
    `probe_python_runtime` measured lives; it is never read from the manifest,
    because the manifest is the thing being checked.
    """

    root = service.paths.interpreters / runtime.archive_sha256
    resolved_root = root.resolve(strict=False)
    interpreter = (root / runtime.interpreter_relative).resolve(strict=False)
    if interpreter == resolved_root or not interpreter.is_relative_to(resolved_root):
        raise WorkerLaunchError("slot interpreter resolves outside its root")
    try:
        details = interpreter.lstat()
    except OSError as exc:
        raise WorkerLaunchError("slot interpreter is unavailable") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
        raise WorkerLaunchError("slot interpreter is unsafe")
    pin = _read_json(
        service.paths.interpreters / f"{runtime.archive_sha256}.pin.json",
        "slot interpreter pin",
    )
    if not isinstance(pin, dict) or set(pin) != {"archive_sha256", "interpreter_sha256"}:
        raise WorkerLaunchError("slot interpreter pin schema is invalid")
    if pin.get("archive_sha256") != runtime.archive_sha256:
        raise WorkerLaunchError("slot interpreter pin does not match the manifest")
    measured = pin.get("interpreter_sha256")
    if not isinstance(measured, str) or not _SHA256.fullmatch(measured):
        raise WorkerLaunchError("slot interpreter pin digest is invalid")
    return interpreter, measured


def _read_json(path: Path, label: str) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
        ):
            raise WorkerLaunchError(f"{label} is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return json.load(handle, object_pairs_hook=_unique_object)
    except WorkerLaunchError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerLaunchError(f"{label} is unreadable") from exc
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise WorkerLaunchError(f"{label} is invalid")
    return value


def _digest(record: Mapping[str, object], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise WorkerLaunchError(f"runtime registry {name} is invalid")
    return value


def _validate_generation(path: Path) -> None:
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise WorkerLaunchError("state generation is unavailable") from exc
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise WorkerLaunchError("state generation is unsafe")
