"""Verified candidate storage and crash-safe local runtime activation."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Protocol

from cortex_platform.runtime_staging import (
    PROFILES as PYTHON_RUNTIME_PROFILES,
    PythonRuntime,
    PythonRuntimeProfile,
    RuntimeStagingError,
    assert_staged_python_runtime,
    stage_python_runtime,
)

from .approval import ReleaseApprovalGate, require_release_approval
from .worker_payload import WORKER_PACKAGE
from .models import (
    CatalogEnvelope,
    PatchLedger,
    ReleaseManifest,
    ValidationError,
    WorkerRuntime,
    canonical_json,
    host_platform,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - product paths support POSIX only
    fcntl = None


class RuntimeUpdateError(RuntimeError):
    """Base class for sanitized managed-runtime failures."""


class VerificationError(RuntimeUpdateError):
    """Artifact, catalog, or provenance verification failed."""


class CatalogReplayError(VerificationError):
    """A catalog sequence would move the trusted high-water mark backward."""


class ActivationError(RuntimeUpdateError):
    """A local slot or activation invariant was not satisfied."""


class CrashInjected(BaseException):
    """Test-only abrupt process-loss marker that bypasses normal rollback."""


class DocumentVerifier(Protocol):
    def verify(self, document: bytes, *, key_id: str | None = None, signature: str | None = None) -> None: ...


class DigestPinVerifier:
    """Verify an exact document digest supplied through a trusted channel."""

    def __init__(self, expected_sha256: str) -> None:
        self.expected_sha256 = expected_sha256

    def verify(
        self,
        document: bytes,
        *,
        key_id: str | None = None,
        signature: str | None = None,
    ) -> None:
        observed = hashlib.sha256(document).hexdigest()
        if observed != self.expected_sha256:
            raise VerificationError("trusted document digest mismatch")


@dataclass(frozen=True)
class RuntimeUpdatePaths:
    root: Path

    @property
    def slots(self) -> Path:
        return self.root / "slots" / "sha256"

    @property
    def generations(self) -> Path:
        return self.root / "state-generations"

    @property
    def pointers(self) -> Path:
        return self.root / "pointers"

    @property
    def attempts(self) -> Path:
        return self.root / "attempts"

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    @property
    def manager(self) -> Path:
        return self.root / "manager.json"

    @property
    def journal(self) -> Path:
        return self.root / "activation-journal.json"

    @property
    def interpreters(self) -> Path:
        """Digest-keyed interpreter roots, beside the slots and never inside one.

        Not in a slot (import-immutable, and exec bits cannot ride a zip) and not
        in the state generation (`_copy_state` would inherit a stale interpreter
        across a state-continuing upgrade — the drift D1b forbids). A sibling
        root keyed by the archive digest is shared by every release carrying the
        same interpreter and is garbage-collected by refcount, like a slot.
        """

        return self.root / "interpreters"

    @property
    def lock(self) -> Path:
        return self.root / ".manager.lock"


@dataclass(frozen=True)
class Candidate:
    release_id: str
    release_sequence: int
    slot_digest: str
    generation_id: str
    slot_dir: Path
    state_dir: Path

    def pointer(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "release_id": self.release_id,
            "release_sequence": self.release_sequence,
            "slot_digest": self.slot_digest,
            "generation_id": self.generation_id,
        }


@dataclass(frozen=True)
class AttemptPin:
    attempt_id: str
    release_id: str
    slot_digest: str
    generation_id: str
    slot_id: str
    artifact_digest: str
    worker_protocol: str


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: object) -> object:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
            os.close(descriptor)
            raise ActivationError("managed metadata is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationError("managed metadata is unreadable") from exc


def _safe_name(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise ActivationError(f"unsafe {label}")
    return value


def _artifact_contents(path: Path) -> tuple[str, bytes]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise VerificationError("artifact is not a readable regular file") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise VerificationError("artifact must be a single regular file")
    digest = hashlib.sha256()
    contents = bytearray()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                contents.extend(chunk)
                if len(contents) > 512 * 1024 * 1024:
                    raise VerificationError("artifact size limit exceeded")
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise VerificationError("artifact is not a readable regular file") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise VerificationError("artifact changed during verification")
    return digest.hexdigest(), bytes(contents)


def _safe_archive_name(name: str) -> PurePosixPath:
    if "\\" in name or "\0" in name:
        raise VerificationError("unsafe archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise VerificationError("unsafe archive path")
    return path


def _extract_archive(artifact: bytes, destination: Path) -> None:
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
            members = archive.infolist()
            if len(members) > 10_000:
                raise VerificationError("archive member limit exceeded")
            for member in members:
                relative = _safe_archive_name(member.filename)
                normalized = relative.as_posix()
                if normalized in seen:
                    raise VerificationError("duplicate archive path")
                seen.add(normalized)
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise VerificationError("archive symlink is forbidden")
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise VerificationError("archive special file is forbidden")
                total += member.file_size
                if total > 512 * 1024 * 1024:
                    raise VerificationError("archive expansion limit exceeded")
                target = destination.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError("artifact archive is invalid") from exc


def _make_immutable(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _tree_digest(root: Path) -> str:
    entries: list[dict[str, object]] = []
    if root.is_symlink() or not root.is_dir():
        raise VerificationError("candidate content tree is invalid")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise VerificationError("candidate content tree contains a symlink")
        relative = path.relative_to(root).as_posix()
        details = path.stat()
        if path.is_dir():
            entries.append({"path": relative, "kind": "directory"})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.S_IMODE(details.st_mode),
                    "size": details.st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            raise VerificationError("candidate content tree contains a special file")
    return hashlib.sha256(canonical_json(entries)).hexdigest()


def _copy_state(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ActivationError("state source must be a regular directory without symlinks")
    destination.mkdir(parents=True, mode=0o700)
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ActivationError("state source contains a symlink")
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(mode=0o700)
        elif item.is_file():
            shutil.copy2(item, target, follow_symlinks=False)
        else:
            raise ActivationError("state source contains a special file")


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INTERPRETER_PIN_FIELDS = {"archive_sha256", "interpreter_sha256"}


@dataclass(frozen=True)
class PythonRuntimeStager:
    """The two staging-kernel calls `stage` makes, as one replaceable seam.

    The kernel is the wheel-shipped `cortex_platform.runtime_staging` (⟦AMD-2⟧),
    and these are its defaults. Tests that exercise the state machine — reuse,
    mismatch, refusal, pruning, containment — inject a stand-in rather than
    unpacking a 38 MB CPython per case; the test that proves the real expansion
    uses the defaults below and stages the vendored archive for real.
    """

    expand: Callable[..., PythonRuntime] = stage_python_runtime
    verify: Callable[..., None] = assert_staged_python_runtime


def _staging_profile(python_version: str) -> PythonRuntimeProfile:
    """Select the staging profile the release's CPython version names.

    A typed refusal, not a fallback to the product's own profile: staging a 3.11
    tree under the 3.14 profile would check the wrong interpreter path, the wrong
    library id and the wrong extension suffix, and the first three of those would
    pass on a tree that carries neither.
    """

    release = ".".join(python_version.split(".")[:2])
    try:
        major, minor = (int(part) for part in release.split("."))
    except ValueError as exc:  # pragma: no cover - the schema already refuses this
        raise ActivationError("release names an unusable Python version") from exc
    profile = PYTHON_RUNTIME_PROFILES.get(f"cp{major}{minor}")
    if profile is None:
        raise ActivationError("release names an unsupported Python runtime profile")
    return profile


def _contained(root: Path, relative: str, label: str) -> Path:
    """Resolve `relative` under `root` and refuse anything that leaves it.

    ⟦AMD-5⟧'s second, independent check. The manifest grammar already refused
    `..`, absolute paths and backslashes, but that decision was made about a
    string; this one is made about the filesystem, so a symlink planted inside
    `content/` cannot turn an accepted path into an escape.
    """

    resolved_root = root.resolve(strict=False)
    candidate = (root / relative).resolve(strict=False)
    if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
        raise ActivationError(f"{label} resolves outside its root")
    return candidate


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.rglob("*"):
        if child.is_symlink():
            raise ActivationError("managed tree contains a symlink")
    for child in sorted(path.rglob("*"), reverse=True):
        child.chmod(0o700 if child.is_dir() else 0o600)
    path.chmod(0o700)
    shutil.rmtree(path)


def _verify_worker_modules(
    content: Path,
    manifest: ReleaseManifest,
    error: type[RuntimeUpdateError],
) -> None:
    """Re-derive every declared worker module against the bytes beside it.

    ⟦S32-R-07⟧ `worker_modules` was a closed, absent-refusing, digest-carrying
    schema-3 field with no consumer: `models.py` parsed it and
    `package_hermes_release.py` produced it, and nothing anywhere compared a
    declared digest to `content/<path>`. A release could declare modules it did
    not carry, or carry modules it did not declare, and every gate passed.
    Contrast `worker_runtime.archive_sha256`, which stage re-derives.

    Verified rather than deleted, because the field is the only statement a
    release makes about *which* of its files are the worker:
    `content_tree_sha256` binds the whole tree without distinguishing the
    protocol implementation from the fork beside it.

    Called from two places on purpose. `import_release` refuses at the door,
    before the slot is sealed and its digest recorded; `_verify_candidate`
    re-asks, because a runtime-update root can be restored onto a machine that
    did not import it — the same reason the platform target is re-asked there.

    ⟦ADJ-20⟧ Both directions, because walking `worker_modules` closes only one.
    The rule is that `content/<WORKER_PACKAGE>/` holds exactly the declared
    modules and nothing else: every declared path is re-derived against the
    bytes beside it, and every regular file under the package that no
    declaration names is a refusal. The second half is what the first half
    cannot see, and it is the half that matters at run time — the entrypoint
    inserts its own resolved directory onto `sys.path`, so an undeclared file
    there is importable for the life of the release.

    No exemption for bytecode, deliberately. `__pycache__` and a bare `.pyc`
    are refused like any other undeclared file: the producer expands with
    `--no-compile` and copies sources only, slots are sealed `0o555`, and the
    entrypoint sets `sys.dont_write_bytecode` before its first import — so a
    compiled artifact in a slot is never something the honest pipeline put
    there, and a sourceless `cortex_worker/<name>.pyc` is importable, which is
    the same hole one file extension over. Directories themselves are not
    files and carry no code, so an empty one is not refused; anything inside
    one is reached by the walk.
    """

    root = content.resolve(strict=False)
    declared = {relative for relative, _ in manifest.worker_modules}
    for relative, digest in manifest.worker_modules:
        module = content / relative
        if not module.resolve(strict=False).is_relative_to(root):
            raise error("candidate worker module escapes the slot")
        try:
            if module.is_symlink() or not module.is_file():
                raise error("candidate worker module is missing")
            observed = hashlib.sha256(module.read_bytes()).hexdigest()
        except OSError as exc:
            raise error("candidate worker module is unreadable") from exc
        if observed != digest:
            raise error("candidate worker module digest mismatch")
    package = content / WORKER_PACKAGE
    if package.is_symlink() or (package.exists() and not package.is_dir()):
        # Neither shape can be the package the declarations describe, and a
        # symlink would make the walk below measure a tree outside the slot.
        raise error("candidate carries an undeclared worker module")
    if not package.is_dir():
        return
    for item in sorted(package.rglob("*")):
        if item.is_dir() and not item.is_symlink():
            continue
        if item.relative_to(content).as_posix() not in declared:
            raise error("candidate carries an undeclared worker module")


class RuntimeUpdateService:
    """Own verified runtime artifacts, generations, pins, and pointers."""

    def __init__(
        self,
        root: Path,
        *,
        catalog_verifier: DocumentVerifier,
        attestation_verifier: DocumentVerifier,
        now: Callable[[], datetime] | None = None,
        python_stager: PythonRuntimeStager | None = None,
        approvals: ReleaseApprovalGate | None = None,
    ) -> None:
        self.paths = RuntimeUpdatePaths(root.resolve(strict=False))
        self._catalog_verifier = catalog_verifier
        self._attestation_verifier = attestation_verifier
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._python_stager = python_stager or PythonRuntimeStager()
        # ⟦S3.4/D6⟧ The operator's approval authority. `None` refuses every door
        # that makes a release run, rather than permitting them: a deployment
        # that forgot to wire the gate must not silently lose the decision.
        # `import` and `stage` are deliberately not gated — neither runs code,
        # and an operator has to be able to bring a build far enough onto the
        # machine to read its manifest digest before approving it.
        self._approvals = approvals
        self._thread_lock = threading.RLock()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise ActivationError("runtime update locking requires POSIX")
        with self._thread_lock:
            self.paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_details = self.paths.root.lstat()
            if (
                not stat.S_ISDIR(root_details.st_mode)
                or root_details.st_uid != os.geteuid()
            ):
                raise ActivationError("runtime update root is unsafe")
            descriptor = os.open(
                self.paths.lock,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                lock_details = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(lock_details.st_mode)
                    or lock_details.st_uid != os.geteuid()
                    or lock_details.st_nlink != 1
                ):
                    raise ActivationError("runtime update lock is unsafe")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _managed_directory(self, path: Path) -> None:
        current = self.paths.root
        root_details = current.lstat()
        if not stat.S_ISDIR(root_details.st_mode) or root_details.st_uid != os.geteuid():
            raise ActivationError("managed directory is unsafe")
        for part in path.relative_to(self.paths.root).parts:
            current = current / part
            try:
                details = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                details = current.lstat()
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.geteuid()
            ):
                raise ActivationError("managed directory is unsafe")

    def _registry(self) -> dict[str, dict[str, object]]:
        raw = _read_json(self.paths.registry, {"schema_version": 1, "releases": {}})
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("releases"), dict):
            raise ActivationError("runtime registry schema is invalid")
        return raw

    def _manager(self) -> dict[str, object]:
        raw = _read_json(
            self.paths.manager,
            {"schema_version": 1, "frozen": False, "catalog_sequence": -1, "catalog_digest": None},
        )
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ActivationError("runtime manager schema is invalid")
        return raw

    def check_catalog(self, raw: object) -> CatalogEnvelope:
        catalog = CatalogEnvelope.from_dict(raw)
        payload = canonical_json(catalog.payload.to_dict())
        self._catalog_verifier.verify(payload, key_id=catalog.key_id, signature=catalog.signature)
        expires = datetime.fromisoformat(catalog.payload.expires_at.removesuffix("Z") + "+00:00")
        issued = datetime.fromisoformat(catalog.payload.issued_at.removesuffix("Z") + "+00:00")
        now = self._now()
        if now < issued or now >= expires:
            raise VerificationError("catalog is outside its validity window")
        return catalog

    def import_release(
        self,
        *,
        catalog: object,
        manifest: object,
        attestation: object,
        artifact: Path,
        patch_ledger: object | None = None,
    ) -> Path:
        envelope = self.check_catalog(catalog)
        release = ReleaseManifest.from_dict(manifest)
        entries = [item for item in envelope.payload.entries if item.release_id == release.release_id]
        if len(entries) != 1 or entries[0].status != "certified":
            raise VerificationError("release is not certified by the catalog")
        if entries[0].manifest_sha256 != release.digest:
            raise VerificationError("manifest hash does not match catalog")
        if not isinstance(attestation, dict) or set(attestation) != {
            "schema_version", "artifact_sha256", "repository", "tag", "commit", "publisher", "workflow"
        }:
            raise VerificationError("provenance schema is invalid")
        self._attestation_verifier.verify(canonical_json(attestation))
        expected_provenance = {
            "schema_version": 1,
            "artifact_sha256": release.artifact_sha256,
            "repository": release.upstream_repository,
            "tag": release.upstream_tag,
            "commit": release.upstream_commit,
            "publisher": release.publisher,
            "workflow": release.workflow,
        }
        if attestation != expected_provenance:
            raise VerificationError("provenance identity does not match manifest")
        # Refuse here rather than at stage or launch: this is the last point
        # where the reason is still "the release is for another host" instead of
        # a link error out of a native wheel.
        if not release.platform.satisfied_by(host_platform()):
            raise VerificationError("release platform is not supported by this host")
        ledger = PatchLedger.from_dict(
            patch_ledger
            if patch_ledger is not None
            else {
                "schema_version": 1,
                "release_id": release.release_id,
                "upstream_commit": release.upstream_commit,
                "patches": [],
            }
        )
        if (
            ledger.release_id != release.release_id
            or ledger.upstream_commit != release.upstream_commit
            or ledger.digest != release.patch_set_sha256
        ):
            raise VerificationError("patch ledger does not match manifest")
        observed, verified_artifact = _artifact_contents(artifact)
        if observed != release.artifact_sha256:
            raise VerificationError("artifact hash does not match manifest")

        with self._locked():
            manager = self._manager()
            accepted = manager.get("catalog_sequence", -1)
            if type(accepted) is not int:
                raise ActivationError("catalog high-water mark is invalid")
            if envelope.payload.sequence < accepted:
                raise CatalogReplayError("catalog replay rejected")
            catalog_digest = hashlib.sha256(canonical_json(envelope.payload.to_dict())).hexdigest()
            if envelope.payload.sequence == accepted and manager.get("catalog_digest") not in {None, catalog_digest}:
                raise CatalogReplayError("catalog sequence equivocation rejected")

            slot = self.paths.slots / release.artifact_sha256
            if not slot.exists():
                self._managed_directory(self.paths.slots)
                temporary = Path(tempfile.mkdtemp(prefix=".candidate.", dir=self.paths.slots))
                try:
                    content = temporary / "content"
                    content.mkdir(mode=0o700)
                    _extract_archive(verified_artifact, content)
                    if not (content / release.worker_entrypoint).is_file():
                        raise VerificationError("candidate worker entrypoint is missing")
                    _verify_worker_modules(content, release, VerificationError)
                    _make_immutable(content)
                    tree_digest = _tree_digest(content)
                    (temporary / "manifest.json").write_bytes(canonical_json(release.to_dict()) + b"\n")
                    (temporary / "attestation.json").write_bytes(canonical_json(attestation) + b"\n")
                    (temporary / "slot.json").write_bytes(
                        canonical_json(
                            {
                                "schema_version": 1,
                                "artifact_sha256": release.artifact_sha256,
                                "manifest_sha256": release.digest,
                                "content_tree_sha256": tree_digest,
                            }
                        )
                        + b"\n"
                    )
                    os.replace(temporary, slot)
                    _make_immutable(slot)
                except BaseException:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise
            else:
                slot_metadata = _read_json(slot / "slot.json", None)
                if not isinstance(slot_metadata, dict):
                    raise VerificationError("existing candidate slot is invalid")
                tree_digest = slot_metadata.get("content_tree_sha256")
                if tree_digest != _tree_digest(slot / "content"):
                    raise VerificationError("existing candidate slot tree mismatch")
            registry = self._registry()
            registry["releases"][release.release_id] = {
                "status": "verified",
                "release_sequence": release.release_sequence,
                "slot_digest": release.artifact_sha256,
                "manifest_digest": release.digest,
                "content_tree_digest": tree_digest,
                "candidate_generation": None,
            }
            manager.update(catalog_sequence=envelope.payload.sequence, catalog_digest=catalog_digest)
            _atomic_json(self.paths.registry, registry)
            _atomic_json(self.paths.manager, manager)
            return slot

    def _candidate(self, release_id: str, registry: Mapping[str, object] | None = None) -> Candidate:
        releases = (registry or self._registry())["releases"]
        record = releases.get(release_id)
        if not isinstance(record, dict):
            raise ActivationError("release is not imported")
        generation = record.get("candidate_generation")
        if not isinstance(generation, str):
            raise ActivationError("release is not staged")
        digest = record.get("slot_digest")
        sequence = record.get("release_sequence")
        if not isinstance(digest, str) or type(sequence) is not int:
            raise ActivationError("release registry record is invalid")
        return Candidate(
            release_id=release_id,
            release_sequence=sequence,
            slot_digest=digest,
            generation_id=generation,
            slot_dir=self.paths.slots / digest,
            state_dir=self.paths.generations / release_id / generation,
        )

    # --- the per-slot interpreter ------------------------------------------

    def _interpreter_root(self, archive_sha256: str) -> Path:
        return self.paths.interpreters / _safe_name(archive_sha256, "interpreter digest")

    def _interpreter_pin(self, archive_sha256: str) -> Path:
        name = _safe_name(archive_sha256, "interpreter digest")
        return self.paths.interpreters / f"{name}.pin.json"

    def _read_interpreter_pin(self, archive_sha256: str) -> dict[str, object] | None:
        raw = _read_json(self._interpreter_pin(archive_sha256), None)
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != _INTERPRETER_PIN_FIELDS:
            raise ActivationError("interpreter pin schema is invalid")
        return raw

    def _verify_interpreter_root(self, manifest: ReleaseManifest) -> None:
        """The check `_verify_candidate` owes at stage, activate, and rollback.

        Two different verdicts on purpose. A missing root is a refusal that
        `stage` repairs by construction — expansion is idempotent and keyed by
        the archive digest, so a crash-orphaned root is re-stageable rather than
        permanently poisoned (the R0-C sealed-leftover class, closed by
        construction). A root whose pin does not match the manifest is a refusal
        with no automatic repair: two different interpreters claiming one digest
        is not a state this service resolves for itself.
        """

        runtime = manifest.worker_runtime
        root = self._interpreter_root(runtime.archive_sha256)
        pin = self._read_interpreter_pin(runtime.archive_sha256)
        if pin is None or not root.is_dir():
            raise ActivationError("candidate interpreter is not staged")
        if pin["archive_sha256"] != runtime.archive_sha256:
            raise ActivationError("candidate interpreter pin does not match the manifest")
        _contained(root, runtime.interpreter_relative, "interpreter")

    def _provision_interpreter(
        self, candidate: Candidate, manifest: ReleaseManifest
    ) -> None:
        """Expand the release's own CPython into a digest-keyed root, once.

        Three-way, per ⟦AMD-4⟧: missing → stage; present and matching → re-run
        the cheap seal and content assertions and reuse; present and mismatched →
        refuse. The digest in the directory name is the archive's, so two
        releases carrying the same interpreter share one root and a re-import of
        the same release stages nothing.
        """

        runtime = manifest.worker_runtime
        profile = _staging_profile(runtime.python_version)
        self._sweep_interpreter_orphans()
        root = self._interpreter_root(runtime.archive_sha256)
        recorded = self._read_interpreter_pin(runtime.archive_sha256)
        if recorded is not None and root.is_dir():
            if recorded["archive_sha256"] != runtime.archive_sha256:
                raise ActivationError("staged interpreter does not match the manifest")
            try:
                self._python_stager.verify(root, profile=profile)
            except RuntimeStagingError as exc:
                raise ActivationError("staged interpreter verification failed") from exc
            _contained(root, runtime.interpreter_relative, "interpreter")
            return
        if recorded is not None or root.exists():
            # A pin without its tree, or a tree without its pin. Either half is
            # the residue of an interrupted run; remove both and re-stage rather
            # than reasoning about which half is authoritative.
            _remove_tree(root)
            self._interpreter_pin(runtime.archive_sha256).unlink(missing_ok=True)

        # Containment is asserted before the archive is opened, on the resolved
        # path rather than on the manifest string (⟦AMD-5⟧).
        content = candidate.slot_dir / "content"
        archive = _contained(content, runtime.archive, "release runtime archive")
        if archive.is_symlink() or not archive.is_file():
            raise ActivationError("release runtime archive is missing")
        self._managed_directory(self.paths.interpreters)
        try:
            staged = self._python_stager.expand(
                archive,
                root,
                stage_root=self.paths.interpreters,
                expected={
                    "sha256": runtime.archive_sha256,
                    # Re-stated from the bytes rather than declared: the manifest
                    # carries no size, the digest is the binding, and the slot's
                    # content-tree digest already covers this file's length.
                    "size": archive.stat().st_size,
                    "version": runtime.python_version,
                    "abi_tag": profile.name,
                },
                profile=profile,
            )
        except RuntimeStagingError as exc:
            raise ActivationError("release interpreter could not be staged") from exc
        interpreter_sha256 = getattr(staged, "interpreter_sha256", "")
        if not isinstance(interpreter_sha256, str) or not _SHA256.fullmatch(
            interpreter_sha256
        ):
            raise ActivationError("staged interpreter was not measured")
        _contained(root, runtime.interpreter_relative, "interpreter")
        _atomic_json(
            self._interpreter_pin(runtime.archive_sha256),
            {
                "archive_sha256": runtime.archive_sha256,
                "interpreter_sha256": interpreter_sha256,
            },
        )

    def _verify_candidate(
        self,
        candidate: Candidate,
        registry: Mapping[str, object],
        *,
        require_interpreter: bool = True,
    ) -> ReleaseManifest:
        releases = registry["releases"]
        record = releases.get(candidate.release_id)
        if not isinstance(record, dict):
            raise ActivationError("candidate registry record is missing")
        metadata = _read_json(candidate.slot_dir / "slot.json", None)
        expected = {
            "schema_version": 1,
            "artifact_sha256": candidate.slot_digest,
            "manifest_sha256": record.get("manifest_digest"),
            "content_tree_sha256": record.get("content_tree_digest"),
        }
        if metadata != expected:
            raise ActivationError("candidate slot metadata verification failed")
        try:
            observed = _tree_digest(candidate.slot_dir / "content")
        except VerificationError as exc:
            raise ActivationError("candidate slot content verification failed") from exc
        if observed != record.get("content_tree_digest"):
            raise ActivationError("candidate slot content verification failed")
        # Import is not the only door. A runtime-update root restored onto
        # different hardware carries slots verified against the host that
        # imported them, so every path that admits a candidate — stage,
        # activate, rollback — re-asks the question here rather than trusting
        # a check that ran on another machine.
        raw_manifest = _read_json(candidate.slot_dir / "manifest.json", None)
        try:
            manifest = ReleaseManifest.from_dict(raw_manifest)
        except ValidationError as exc:
            raise ActivationError("candidate manifest is invalid") from exc
        if not manifest.platform.satisfied_by(host_platform()):
            raise ActivationError("candidate platform is not supported by this host")
        # `stage` is the one caller that legitimately verifies before the
        # interpreter exists: it verifies the slot's bytes, expands the archive
        # those bytes carry, and then asks this question separately. Every other
        # door — activate, rollback — asks it here.
        self._verify_worker_modules(candidate, manifest)
        if require_interpreter:
            self._verify_interpreter_root(manifest)
        return manifest

    def _verify_worker_modules(
        self, candidate: Candidate, manifest: ReleaseManifest
    ) -> None:
        _verify_worker_modules(
            candidate.slot_dir / "content", manifest, ActivationError
        )

    def _require_approval(
        self, candidate: Candidate, registry: Mapping[str, object]
    ) -> None:
        """Refuse a release the operator has not approved by exact digest.

        Read from the registry record rather than from the slot's own
        `manifest.json`, because `_verify_candidate` has just proved the record
        and the slot agree — and the record is the value `import` wrote, which is
        the one an operator approving a release would have been shown.
        """

        record = registry["releases"][candidate.release_id]
        manifest_digest = record.get("manifest_digest")  # type: ignore[union-attr]
        if not isinstance(manifest_digest, str):
            raise ActivationError("candidate manifest digest is unavailable")
        require_release_approval(
            self._approvals, candidate.release_id, manifest_digest
        )

    def stage(self, release_id: str, *, source_state: Path | None = None) -> Candidate:
        _safe_name(release_id, "release identifier")
        with self._locked():
            registry = self._registry()
            releases = registry["releases"]
            record = releases.get(release_id)
            if not isinstance(record, dict) or record.get("status") not in {"verified", "staged", "quarantined"}:
                raise ActivationError("release is not eligible for staging")
            generation = uuid.uuid4().hex
            target_parent = self.paths.generations / release_id
            self._managed_directory(target_parent)
            temporary = target_parent / f".{generation}.tmp"
            target = target_parent / generation
            try:
                if source_state is None:
                    temporary.mkdir(mode=0o700)
                else:
                    _copy_state(source_state, temporary)
                os.replace(temporary, target)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            record["candidate_generation"] = generation
            record["status"] = "staged"
            candidate = self._candidate(release_id, registry)
            manifest = self._verify_candidate(
                candidate, registry, require_interpreter=False
            )
            try:
                self._provision_interpreter(candidate, manifest)
                self._verify_interpreter_root(manifest)
            except BaseException:
                # The state generation was created for a candidate that cannot
                # run. Leaving it behind would make the next stage of the same
                # release inherit a generation no interpreter backs.
                _remove_tree(target)
                raise
            _atomic_json(self.paths.registry, registry)
            return candidate

    def _read_pointer(self, name: str) -> dict[str, object] | None:
        raw = _read_json(self.paths.pointers / f"{name}.json", None)
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "release_id", "release_sequence", "slot_digest", "generation_id"
        } or raw.get("schema_version") != 1:
            raise ActivationError("runtime pointer schema is invalid")
        return raw

    def _write_pointer(self, name: str, pointer: Mapping[str, object] | None) -> None:
        path = self.paths.pointers / f"{name}.json"
        if pointer is None:
            path.unlink(missing_ok=True)
        else:
            self._managed_directory(self.paths.pointers)
            _atomic_json(path, dict(pointer))

    def _active_attempts(self) -> list[Path]:
        return list(self.paths.attempts.glob("*.json")) if self.paths.attempts.exists() else []

    @staticmethod
    def _inject(crash_at: str | None, point: str) -> None:
        if crash_at == point:
            raise CrashInjected(point)

    def activate(
        self,
        release_id: str,
        *,
        probe: Callable[[Candidate], bool],
        crash_at: str | None = None,
    ) -> Candidate:
        with self._locked():
            self._recover_activation_unlocked()
            manager = self._manager()
            if manager.get("frozen") is True:
                raise ActivationError("runtime activation is frozen")
            if self._active_attempts():
                raise ActivationError("runtime activation requires no active attempts")
            registry = self._registry()
            candidate = self._candidate(release_id, registry)
            self._verify_candidate(candidate, registry)
            self._require_approval(candidate, registry)
            active = self._read_pointer("active")
            if active is not None and active.get("release_id") == release_id:
                return Candidate(
                    release_id=release_id,
                    release_sequence=active["release_sequence"],
                    slot_digest=active["slot_digest"],
                    generation_id=active["generation_id"],
                    slot_dir=self.paths.slots / active["slot_digest"],
                    state_dir=self.paths.generations / release_id / active["generation_id"],
                )
            if active is not None and candidate.release_sequence < active["release_sequence"]:
                raise ActivationError("runtime downgrade is forbidden")
            journal = {
                "schema_version": 1,
                "phase": "prepared",
                "old_active": active,
                "candidate": candidate.pointer(),
            }
            _atomic_json(self.paths.journal, journal)
            self._inject(crash_at, "journal_prepared")
            try:
                if probe(candidate) is not True:
                    raise ActivationError("candidate health probe failed")
                registry["releases"][release_id]["status"] = "certified"
                _atomic_json(self.paths.registry, registry)
                self._inject(crash_at, "candidate_healthy")
                self._write_pointer("active", candidate.pointer())
                journal["phase"] = "active_pointer_switched"
                _atomic_json(self.paths.journal, journal)
                self._inject(crash_at, "active_pointer_switched")
                if probe(candidate) is not True:
                    raise ActivationError("candidate post-activation health probe failed")
                self._inject(crash_at, "post_health")
            except Exception as exc:
                self._write_pointer("active", active)
                registry["releases"][release_id]["status"] = "quarantined"
                _atomic_json(self.paths.registry, registry)
                self.paths.journal.unlink(missing_ok=True)
                if isinstance(exc, ActivationError):
                    raise
                raise ActivationError("candidate health probe failed") from exc
            if active is not None:
                self._write_pointer("last_known_good", active)
                old_record = registry["releases"].get(active["release_id"])
                if isinstance(old_record, dict):
                    old_record["status"] = "last_known_good"
            registry["releases"][release_id]["status"] = "active"
            _atomic_json(self.paths.registry, registry)
            self.paths.journal.unlink(missing_ok=True)
            return candidate

    def _recover_activation_unlocked(self) -> None:
        raw = _read_json(self.paths.journal, None)
        if raw is None:
            return
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ActivationError("activation journal schema is invalid")
        old = raw.get("old_active")
        candidate = raw.get("candidate")
        self._write_pointer("active", old if isinstance(old, dict) else None)
        registry = self._registry()
        if isinstance(candidate, dict):
            release_id = candidate.get("release_id")
            record = registry["releases"].get(release_id)
            if isinstance(record, dict):
                record["status"] = "quarantined"
                _atomic_json(self.paths.registry, registry)
        self.paths.journal.unlink(missing_ok=True)

    def recover_activation(self) -> None:
        with self._locked():
            self._recover_activation_unlocked()

    def rollback(self, *, probe: Callable[[Candidate], bool]) -> Candidate:
        with self._locked():
            self._recover_activation_unlocked()
            if self._active_attempts():
                raise ActivationError("runtime rollback requires no active attempts")
            pointer = self._read_pointer("last_known_good")
            if pointer is None:
                raise ActivationError("last-known-good runtime is unavailable")
            candidate = Candidate(
                release_id=pointer["release_id"],
                release_sequence=pointer["release_sequence"],
                slot_digest=pointer["slot_digest"],
                generation_id=pointer["generation_id"],
                slot_dir=self.paths.slots / pointer["slot_digest"],
                state_dir=self.paths.generations / pointer["release_id"] / pointer["generation_id"],
            )
            registry = self._registry()
            self._verify_candidate(candidate, registry)
            self._require_approval(candidate, registry)
            if not candidate.state_dir.is_dir() or probe(candidate) is not True:
                raise ActivationError("last-known-good health probe failed")
            current = self._read_pointer("active")
            self._write_pointer("active", pointer)
            self._write_pointer("last_known_good", current)
            restored_record = registry["releases"].get(candidate.release_id)
            if isinstance(restored_record, dict):
                restored_record["status"] = "active"
            if current is not None:
                previous_record = registry["releases"].get(current["release_id"])
                if isinstance(previous_record, dict):
                    previous_record["status"] = "last_known_good"
            _atomic_json(self.paths.registry, registry)
            return candidate

    def preview_attempt_pin(self, attempt_id: str) -> AttemptPin:
        """Resolve the active exact identity without creating an attempt pin."""

        _safe_name(attempt_id, "attempt identifier")
        with self._locked():
            active = self._read_pointer("active")
            if active is None:
                raise ActivationError("active runtime is unavailable")
            return self._attempt_pin_from_identity(
                attempt_id=attempt_id,
                release_id=active["release_id"],
                slot_digest=active["slot_digest"],
                generation_id=active["generation_id"],
            )

    def pin_attempt(
        self,
        attempt_id: str,
        expected_pin: AttemptPin | None = None,
    ) -> AttemptPin:
        _safe_name(attempt_id, "attempt identifier")
        with self._locked():
            active = self._read_pointer("active")
            if active is None:
                raise ActivationError("active runtime is unavailable")
            pin = self._attempt_pin_from_identity(
                attempt_id=attempt_id,
                release_id=active["release_id"],
                slot_digest=active["slot_digest"],
                generation_id=active["generation_id"],
            )
            if expected_pin is not None and pin != expected_pin:
                raise ActivationError("active runtime changed before pin commit")
            path = self.paths.attempts / f"{attempt_id}.json"
            self._managed_directory(self.paths.attempts)
            existing = self.attempt_pin(attempt_id)
            if existing is not None and existing != pin:
                raise ActivationError("attempt is already pinned to another runtime")
            _atomic_json(path, asdict(pin))
            return pin

    def attempt_pin(self, attempt_id: str) -> AttemptPin | None:
        attempt_id = _safe_name(attempt_id, "attempt identifier")
        raw = _read_json(self.paths.attempts / f"{attempt_id}.json", None)
        if raw is None:
            return None
        legacy_fields = {
            "attempt_id",
            "release_id",
            "slot_digest",
            "generation_id",
        }
        exact_fields = legacy_fields | {
            "slot_id",
            "artifact_digest",
            "worker_protocol",
        }
        if not isinstance(raw, dict):
            raise ActivationError("attempt pin schema is invalid")
        fields = set(raw)
        if fields != legacy_fields and fields != exact_fields:
            raise ActivationError("attempt pin schema is invalid")
        if raw.get("attempt_id") != attempt_id:
            raise ActivationError("attempt pin identity is invalid")
        release_id = raw.get("release_id")
        slot_digest = raw.get("slot_digest")
        generation_id = raw.get("generation_id")
        if not all(isinstance(value, str) and value for value in (
            release_id,
            slot_digest,
            generation_id,
        )):
            raise ActivationError("attempt pin identity is invalid")
        expected = self._attempt_pin_from_identity(
            attempt_id=attempt_id,
            release_id=release_id,
            slot_digest=slot_digest,
            generation_id=generation_id,
        )
        if fields == exact_fields and raw != asdict(expected):
            raise ActivationError("attempt pin identity is invalid")
        return expected

    def _attempt_pin_from_identity(
        self,
        *,
        attempt_id: str,
        release_id: object,
        slot_digest: object,
        generation_id: object,
    ) -> AttemptPin:
        if not all(
            isinstance(value, str) and value
            for value in (release_id, slot_digest, generation_id)
        ):
            raise ActivationError("attempt pin identity is invalid")
        raw_manifest = _read_json(
            self.paths.slots / slot_digest / "manifest.json", None
        )
        try:
            manifest = ReleaseManifest.from_dict(raw_manifest)
        except ValidationError as exc:
            raise ActivationError("attempt pin manifest is invalid") from exc
        if (
            manifest.release_id != release_id
            or manifest.artifact_sha256 != slot_digest
        ):
            raise ActivationError("attempt pin manifest identity mismatch")
        # ⟦S3.4/D6⟧ Dispatch's door. An attempt pin is what binds a run to a
        # slot, so a pin issued for an unapproved release is dispatch of an
        # unapproved release however the caller reached it.
        require_release_approval(self._approvals, release_id, manifest.digest)
        return AttemptPin(
            attempt_id=attempt_id,
            release_id=release_id,
            slot_digest=slot_digest,
            generation_id=generation_id,
            slot_id=slot_digest,
            artifact_digest=manifest.artifact_sha256,
            worker_protocol=manifest.adapter_protocol,
        )

    def finish_attempt(self, attempt_id: str, pin: AttemptPin) -> None:
        with self._locked():
            current = self.attempt_pin(attempt_id)
            if current is None:
                return
            if current != pin:
                raise ActivationError("attempt pin ownership mismatch")
            (self.paths.attempts / f"{attempt_id}.json").unlink(missing_ok=True)
            directory = os.open(self.paths.attempts, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def freeze(self) -> None:
        with self._locked():
            manager = self._manager()
            manager["frozen"] = True
            _atomic_json(self.paths.manager, manager)

    def thaw(self) -> None:
        with self._locked():
            manager = self._manager()
            manager["frozen"] = False
            _atomic_json(self.paths.manager, manager)

    def status(self) -> dict[str, object]:
        with self._locked():
            self._recover_activation_unlocked()
            manager = self._manager()
            registry = self._registry()
            return {
                "frozen": manager.get("frozen") is True,
                "catalog_sequence": manager.get("catalog_sequence"),
                "active": self._read_pointer("active"),
                "last_known_good": self._read_pointer("last_known_good"),
                "active_attempts": len(self._active_attempts()),
                "releases": registry["releases"],
            }

    def prune(self, *, retain_candidates: int = 1) -> tuple[str, ...]:
        """Remove unreferenced candidates while preserving active, LKG, and pins."""

        if type(retain_candidates) is not int or retain_candidates < 0:
            raise ActivationError("candidate retention must be non-negative")
        with self._locked():
            self._recover_activation_unlocked()
            registry = self._registry()
            releases = registry["releases"]
            protected: set[str] = set()
            for name in ("active", "last_known_good"):
                pointer = self._read_pointer(name)
                if pointer is not None:
                    protected.add(pointer["release_id"])
            for path in self._active_attempts():
                raw = _read_json(path, None)
                if isinstance(raw, dict) and isinstance(raw.get("release_id"), str):
                    protected.add(raw["release_id"])
            candidates = sorted(
                (
                    (release_id, record)
                    for release_id, record in releases.items()
                    if release_id not in protected and isinstance(record, dict)
                ),
                key=lambda item: item[1].get("release_sequence", -1),
                reverse=True,
            )
            protected.update(release_id for release_id, _ in candidates[:retain_candidates])
            removed = tuple(sorted(set(releases) - protected))
            kept_digests = {
                record.get("slot_digest")
                for release_id, record in releases.items()
                if release_id in protected and isinstance(record, dict)
            }
            for release_id in removed:
                record = releases.pop(release_id)
                _remove_tree(self.paths.generations / release_id)
                digest = record.get("slot_digest") if isinstance(record, dict) else None
                if isinstance(digest, str) and digest not in kept_digests:
                    _remove_tree(self.paths.slots / digest)
            self._prune_interpreters(releases)
            _atomic_json(self.paths.registry, registry)
            return removed

    #: What `runtime_staging` names its in-progress expansion:
    #: `.{archive_sha256}.{32 hex}`. Matched exactly, so an operator's own
    #: dot-prefixed entry is left alone the way `_prune_interpreters` leaves an
    #: unrecognized one alone.
    _INTERPRETER_ORPHAN = re.compile(r"^\.[0-9a-f]{64}\.[0-9a-f]{32}$")

    def _sweep_interpreter_orphans(self) -> None:
        """Reclaim expansion trees a crash left behind.

        ⟦S32-R-02⟧ `runtime_staging` creates `.{archive_sha256}.{32 hex}` and
        removes it only from its own `except BaseException` handler, so a
        SIGKILL — or a plain SIGTERM — mid-expansion leaves a sealed ~94 MB /
        2032-file tree. `_prune_interpreters` cannot see it: it skips every name
        that fails `_SHA256.fullmatch`, and a dot-prefixed name always does. The
        primitive predates this branch, but this is the first caller to point it
        at a persistent root rather than an ephemeral install staging directory,
        so the orphan is now permanent.

        Called from `_provision_interpreter`, which runs under `stage`'s
        exclusive flock, so there is no live sibling to reason about. Failures
        are swallowed: an unreclaimable orphan should degrade to a leak, never
        to a refusal to stage.
        """

        root = self.paths.interpreters
        if not root.is_dir():
            return
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return
        for entry in entries:
            if not self._INTERPRETER_ORPHAN.fullmatch(entry.name):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                _remove_tree(entry)
            except OSError:
                continue

    def _prune_interpreters(self, releases: Mapping[str, object]) -> None:
        """Drop every interpreter root no surviving release's manifest names.

        Refcount by scan, the same discipline slot pruning already uses: an
        interpreter root is shared by every release carrying the same archive, so
        the only safe question is whether any remaining manifest still names its
        digest. Read from the slots rather than from the registry — the registry
        records a slot digest, and only the manifest inside that slot says which
        interpreter the release needs.
        """

        if not self.paths.interpreters.is_dir():
            return
        referenced: set[str] = set()
        for record in releases.values():
            if not isinstance(record, dict):
                continue
            digest = record.get("slot_digest")
            if not isinstance(digest, str):
                continue
            try:
                # ⟦S32-R-01⟧ Inside the try, not above it. `_read_json` raises
                # `ActivationError("managed metadata is unsafe"/"is unreadable")`
                # of its own, and `prune()` has already run `releases.pop()` and
                # `_remove_tree` by the time this executes but has not yet
                # committed the registry — so an escape here leaves the registry
                # naming releases whose slots are gone, and every re-run hits
                # the same read and the same escape.
                raw = _read_json(self.paths.slots / digest / "manifest.json", None)
                referenced.add(ReleaseManifest.from_dict(raw).worker_runtime.archive_sha256)
            except (ValidationError, ActivationError):
                # ⟦S32-R-06⟧ A skip drops this release's digest from
                # `referenced`, so the cost is a DELETED interpreter, not a
                # retained one — the comment here used to claim the opposite.
                # Tolerable because a slot whose manifest cannot be read is
                # already refused by `_verify_candidate`, which parses the same
                # manifest before it ever reaches `_verify_interpreter_root`:
                # the release is unusable with or without its interpreter.
                continue
        for entry in sorted(self.paths.interpreters.iterdir()):
            name = entry.name
            digest = name.removesuffix(".pin.json")
            if not _SHA256.fullmatch(digest) or digest in referenced:
                continue
            if entry.is_symlink():
                raise ActivationError("interpreter root contains a symlink")
            if entry.is_dir():
                _remove_tree(entry)
            else:
                entry.unlink(missing_ok=True)
