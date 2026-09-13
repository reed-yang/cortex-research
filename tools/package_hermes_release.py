"""Package one Hermes build into the document set `cortex runtime import` reads.

Operator-run, once per certified Hermes build. Like `vendor_wheelhouse.py` this
tool lives outside `distribution/` and is never copied into a bundle: it is a
producer, and the product only ever consumes what it emits.

S3.1a gave `import_release` the right to refuse a release the host cannot run.
This is the other half — the producer that fills the field the refusal reads.

The rule that shapes the module is that **`minimum_os_version` is derived, never
accepted**. `vendor_wheelhouse.py` hands pip four platform tags at once, so a
closure can legitimately mix an `11_0` wheel with a `14_0` one, and its true
floor is the maximum over the wheels pip actually left behind. Before this tool
the floor was whatever a packager typed, which made S3.1a's check a lock with a
hand-filled key: declare `None` over a `macosx_14_0` closure, import cleanly on
macOS 12, and the failure reappears at launch as a native-wheel link error —
the exact failure the platform target exists to prevent.

Deriving it does not breach the contract's no-PEP-425 rule. Reading
`macosx_<major>_<minor>` out of the platform field of a filename pip itself
produced is lexical extraction, not tag matching; nothing here decides whether
a tag is *compatible*, only what the highest declared floor is.

S3.2 adds the second derived field of the same kind. The release carries its own
CPython, so `worker_runtime` is filled from the archive the tool packaged and
from the vendor tool's committed pin — never from an argument. `--python-range`
remains a typed string because it describes the *upstream distribution's*
declared support, not the interpreter this release ships with; the two are
different claims and only the second is derivable here.

Two fail-closed behaviours are documented rather than fixed, both inherited
from `PlatformTarget.satisfied_by`: a non-Darwin host reports no OS version and
therefore can never satisfy a release that declares a floor, and a host running
under `SYSTEM_VERSION_COMPAT=1` reports `10.16`, which under-reports and
therefore refuses. Both err toward refusal.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from cortex_platform.product.runtime_update.worker_payload import (
    WORKER_PACKAGE,
    module_sources,
)
from cortex_platform.product.runtime_update.models import (
    CatalogEnvelope,
    PatchEntry,
    PatchLedger,
    ReleaseManifest,
    ValidationError,
    canonical_json,
    host_platform,
)
from distribution.wheel_closure import (
    TARGET_ENVIRONMENT,
    WheelClosureError,
    evaluate_marker,
    normalize_name,
    split_requirement,
)
from tools.vendor_wheelhouse import SIDE_RECORD_NAME

REQUIREMENTS_NAME = "closure.requirements.txt"

# Where the vendored interpreter rides inside the payload, and therefore inside
# `content/` once `import_release` extracts the artifact. A directory of its own
# so the interpreter can never be mistaken for a Hermes module.
RUNTIME_DIRECTORY = "runtime"
RUNTIME_PIN_NAME = "pin.json"

# One macOS platform tag. Deliberately a local copy of the grammar in
# `distribution/wheel_closure._MACOS_TAG` rather than an import of a private
# name; `test_package_hermes_release.py` pins the two to accept the same
# strings, so drift is a test failure rather than a silent divergence.
_MACOS_TAG = re.compile(r"^macosx_(?P<major>[0-9]+)_(?P<minor>[0-9]+)_(?P<arch>[A-Za-z0-9]+)$")

# A zip whose entries carry the current clock produces a different
# `artifact_sha256` on every run, and that digest is pinned into the manifest,
# the catalog entry and the attestation. Freeze it at the format minimum.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_REGULAR_FILE = 0o100644 << 16

CommandRunner = Callable[[Sequence[str]], None]


class PackagingError(RuntimeError):
    """The release could not be packaged into an importable document set."""


@dataclass(frozen=True)
class ReleaseIdentity:
    """Everything about a release that packaging cannot compute for itself.

    `evidence_sha256` is an input rather than an output because the
    certification harness (S3.5) produces the evidence, and the manifest digest
    is bound into the catalog — so it cannot be filled in afterwards without
    rebuilding every document. Packaging carries it; it does not invent it.

    There is deliberately no `minimum_os_version` field. See the module
    docstring.
    """

    release_id: str
    release_sequence: int
    distribution_version: str
    upstream_repository: str
    upstream_tag: str
    upstream_commit: str
    publisher: str
    workflow: str
    python_range: str
    adapter_protocol: str
    session_schema: int
    evidence_sha256: str


@dataclass(frozen=True)
class PackagedRelease:
    artifact: Path
    manifest: dict[str, object]
    catalog: dict[str, object]
    attestation: dict[str, object]
    patch_ledger: dict[str, object]
    pins: dict[str, str]
    paths: dict[str, Path] = field(default_factory=dict)


# --- the derivation --------------------------------------------------------


def _platform_field(wheel: str) -> str:
    """The last `-`-separated field of a wheel name is its platform tag set.

    Restricting the scan to that field costs one `rsplit` and removes a whole
    class of wrong answers: a distribution *named* `macosx_99_0_helper` cannot
    raise the floor of the closure it belongs to.
    """

    return wheel.removesuffix(".whl").rsplit("-", 1)[-1]


def derive_minimum_os_version(wheels: Iterable[str]) -> str | None:
    """The highest macOS floor any wheel in the closure declares.

    Returns `None` for a closure that declares no macOS platform at all — a
    pure-Python closure runs anywhere, and a floor would only refuse hosts.

    Comparison is numeric, not lexical: `macosx_10_0` sorts below `macosx_9_0`
    as text, and both orderings occur in a real closure because pip is offered
    `macosx_11_0` through `macosx_26_0` while older PyPI wheels still carry
    `10_x`.
    """

    floors: list[tuple[int, int]] = []
    for wheel in wheels:
        # One wheel may declare several platforms in a dot-separated field.
        for tag in _platform_field(wheel).split("."):
            match = _MACOS_TAG.fullmatch(tag)
            if match is not None:
                floors.append((int(match.group("major")), int(match.group("minor"))))
    if not floors:
        return None
    major, minor = max(floors)
    return f"{major}.{minor}"


# --- resolving and expanding the closure -----------------------------------


def _default_runner(command: Sequence[str]) -> None:
    try:
        completed = subprocess.run(
            list(command), check=False, capture_output=True, text=True
        )
    except OSError as exc:
        # A mistyped `--python-executable` never reaches pip: `subprocess.run`
        # raises `FileNotFoundError` first, and it escaped this module as a
        # standard-library traceback rather than as a packaging refusal naming
        # the interpreter the operator asked for.
        raise PackagingError(
            f"closure expansion could not start: {command[0]}: {exc}"
        ) from exc
    if completed.stderr.strip():
        # pip reports real problems at exit 0 too — skipping an existing
        # target directory is a stderr warning, not a failure. Never swallow it.
        print(completed.stderr.strip(), file=sys.stderr)
    if completed.returncode != 0:
        raise PackagingError(
            f"closure expansion failed: {' '.join(command[:3])}…\n{completed.stderr.strip()}"
        )


def _sdist_wheel_digests(closure: Path) -> dict[str, str]:
    """The recorded digests of wheels `vendor_wheelhouse` built from sdists.

    The lock pins the *sdist* for those distributions, so the wheel's own
    digest exists only in this record — `verify_acquisition` already treats it
    as the binding for exactly these bytes.
    """

    record = closure / SIDE_RECORD_NAME
    if not record.is_file():
        return {}
    try:
        raw = json.loads(record.read_text(encoding="utf-8"))
        return {str(name): str(entry["wheel_sha256"]) for name, entry in raw.items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PackagingError(f"{SIDE_RECORD_NAME} is invalid: {exc}") from exc


def resolve_requirements(requirements: Path) -> list[str]:
    """The pinned requirements that apply to the target, markers intact.

    "Markers intact" means evaluated, not stripped: a requirement gated on
    `sys_platform == "win32"` is not part of this closure and must not be
    installed, but the decision belongs to `TARGET_ENVIRONMENT` — the host
    doing the packaging is not necessarily the host being packaged for, and
    pip would evaluate against the running interpreter.

    `--hash=` pins are kept deliberately (they put pip into --require-hashes
    mode, which is a real binding, not an accident of parsing) — but a wheel
    `vendor_wheelhouse` legitimately built from a pinned sdist has a digest
    the lock does not know, so its recorded `wheel_sha256` is appended or pip
    would refuse the closure's own blessed wheel.
    """

    built = _sdist_wheel_digests(requirements.parent)
    resolved: list[str] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("--"):
            continue
        # Split off the hash pins first, exactly as `filter_for_target` does
        # when it writes this file; left inline they ride inside the specifier.
        head, *hashes = text.split("--hash=")
        try:
            name, extras, specifier, marker = split_requirement(head.strip())
        except WheelClosureError as exc:
            raise PackagingError(f"closure requirement is unsupported: {text!r}") from exc
        if marker and not evaluate_marker(marker, TARGET_ENVIRONMENT):
            continue
        suffix = f"[{','.join(extras)}]" if extras else ""
        digests = [f"--hash={value.strip()}" for value in hashes]
        sdist_wheel = built.get(normalize_name(name))
        if digests and sdist_wheel is not None:
            digests.append(f"--hash=sha256:{sdist_wheel}")
        resolved.append(" ".join([f"{name}{suffix}{specifier}", *digests]).strip())
    if not resolved:
        raise PackagingError("no closure requirement applies to the target environment")
    return resolved


#: The document names this producer writes, as `<name>.json`. Named because the
#: certification harness reads them back by name from a directory and had, until
#: the lanes merged, been looking for a hyphenated `patch-ledger.json` that this
#: producer has never written. `tests/packaging` pins the two together.
MANIFEST_DOCUMENT = "manifest"
CATALOG_DOCUMENT = "catalog"
ATTESTATION_DOCUMENT = "attestation"
PATCH_LEDGER_DOCUMENT = "patch_ledger"
PINS_DOCUMENT = "pins"


def load_patch_entries(document: Path) -> list[dict[str, object]]:
    """Read the ledger entries `--patches` names, in the consumer's own shape.

    The fork is a checkout this tool never sees, so what the release diverges by
    is an input like the closure is: one entry per commit the fork carries over
    `--upstream-commit`, each naming the digest of that commit's patch and the
    disposition A2 drives to `upstreamed` or `dropped`.

    Both shapes a real operator has on disk are accepted — a bare list, and a
    whole `patch_ledger.json` as a previous run emitted it, so a re-package can
    be handed the ledger of the release it supersedes.

    Empty is refused rather than packaged. `package_release` defaults `patches`
    to `()` and certification clause §5.9 passes only on a ledger that names at
    least one patch, so an empty ledger produces a release that can never be
    certified and therefore never approved — a build worth refusing at the
    argument, not four documents later.
    """

    try:
        raw = json.loads(document.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PackagingError(f"--patches is not readable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PackagingError(f"--patches is not a JSON document: {exc}") from exc
    if isinstance(raw, Mapping):
        raw = raw.get("patches")
    if not isinstance(raw, list):
        raise PackagingError(
            "--patches must hold a list of patch entries, or a patch ledger carrying one"
        )
    entries: list[dict[str, object]] = []
    for item in raw:
        # Validated through the consumer's own model for the same reason
        # `package_release` validates the manifest and the catalog through
        # theirs: a producer that can emit a document its consumer rejects is
        # the defect this tool exists to remove.
        try:
            PatchEntry.from_dict(item)
        except ValidationError as exc:
            raise PackagingError(f"--patches carries an invalid patch entry: {exc}") from exc
        entries.append(dict(item))  # type: ignore[arg-type]
    if not entries:
        raise PackagingError(
            "--patches carries no entries; a release with an empty patch ledger "
            "fails certification clause 5.9 and can never be certified"
        )
    return entries


def expand_closure(
    *,
    closure: Path,
    destination: Path,
    extra_files: Mapping[str, Path] = {},
    runner: CommandRunner = _default_runner,
    python_executable: Path | None = None,
) -> Path:
    """Install the pinned closure into a tree, then place the extras beside it.

    `--no-deps` is not an optimisation: the closure is already complete and
    already pinned, so letting pip re-resolve would silently replace the thing
    that was certified. `--no-index` keeps the step offline.
    """

    requirements = closure / REQUIREMENTS_NAME
    if not requirements.is_file():
        raise PackagingError(f"dependency closure has no {REQUIREMENTS_NAME}")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        # pip does not replace what a previous run left behind: it skips an
        # existing directory with a stderr warning and exit 0, so expanding
        # into a reused tree would attest the new build's identity over the
        # old build's bytes — self-consistently, and therefore undetectably
        # by any consumer check. Same posture as the bundle composer's
        # "bundle output already exists".
        raise PackagingError(
            "payload destination is not empty; expand into a fresh --output"
        )
    destination.mkdir(parents=True, exist_ok=True)
    filtered = destination.parent / "resolved.requirements.txt"
    filtered.write_text("\n".join(resolve_requirements(requirements)) + "\n", encoding="utf-8")
    runner(
        [
            str(python_executable or Path(sys.executable)),
            "-m", "pip", "install",
            "--no-index",
            "--no-deps",
            # pip byte-compiles into --target regardless of
            # PYTHONDONTWRITEBYTECODE, and each cached code object embeds the
            # absolute staging path — so two runs of an identical closure
            # digest differently and the reproducibility the manifest pins is
            # lost. Same lesson as the installer's _purge_bytecode.
            "--no-compile",
            "--find-links", str(closure),
            "--target", str(destination),
            "--requirement", str(filtered),
        ]
    )
    for name, source in extra_files.items():
        if not source.is_file():
            raise PackagingError(f"payload extra is not a regular file: {source}")
        (destination / name).write_bytes(source.read_bytes())
    return destination


def _shebang_version(pinned_version: str) -> str:
    """`major.minor` of the interpreter the *release* pins, as the shebang wants.

    ⟦ADJ-19⟧ The predecessor probed the builder instead — `sys.version_info`
    when no interpreter was passed, a `subprocess.run` on the passed one
    otherwise — and the shebang is payload, so it is `artifact_sha256`, so it is
    `slot_id`. One pinned closure and one vendored cp311 archive packaged on a
    3.12 host and a 3.13 host were therefore two different releases. The pin is
    the only statement about the interpreter that belongs to the release.
    """

    major, _, remainder = pinned_version.partition(".")
    minor, _, _ = remainder.partition(".")
    if not major.isdigit() or not minor.isdigit():
        raise PackagingError(f"runtime pin declares no usable version: {pinned_version!r}")
    return f"{major}.{minor}"


# --- packaging -------------------------------------------------------------


def _closure_wheels(closure: Path) -> list[str]:
    if not closure.is_dir():
        raise PackagingError("dependency closure directory does not exist")
    wheels = sorted(item.name for item in closure.iterdir() if item.suffix == ".whl")
    if not wheels:
        # A derived `None` over an empty wheelhouse is indistinguishable from a
        # derived `None` over a portable one, and only one of them is a release.
        raise PackagingError("dependency closure contains no wheels")
    return wheels


#: The generic shebang a wheel carries before installation. `pip install
#: --target` rewrites it to the absolute path of the interpreter that ran pip,
#: which makes the payload — and therefore `artifact_sha256`, and therefore
#: `slot_id` — a function of the build directory rather than of the release.
_GENERIC_SHEBANG_PREFIX = "#!python"

#: `RECORD` lines are `path,sha256=<urlsafe-b64, unpadded>,<size>`.
_RECORD_HASH = "sha256="


def _record_digest(payload: bytes) -> str:
    return (
        _RECORD_HASH
        + base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
        .decode("ascii")
        .rstrip("=")
    )


def normalize_console_scripts(destination: Path, *, python_version: str) -> list[str]:
    """Strip the build root out of console-script shebangs, and re-record them.

    ⟦S32-05⟧ Measured on the real acceptance artifact: 14 members under `bin/`
    began `#!/tmp/s32-real-acceptance/cp311/bin/python3.11`, and that zip's
    digest is exactly the descriptor's `expected_artifact_digest` and `slot_id`.
    Two identical closures expanded from two directories are therefore two
    different releases, which defeats the reproducibility the manifest pins.

    Rewritten rather than deleted. Deleting `bin/` would fix reproducibility too
    — nothing execs those scripts, and S3.4's profile denies `process-fork`
    outright — but it would leave every `RECORD` naming files the payload does
    not carry. Rewriting keeps the tree pip produced, and the `RECORD` entries
    are updated with it so each dist-info stays self-consistent about its own
    bytes.

    The replacement is the generic form a wheel carries before installation. No
    absolute path could be right: the interpreter's real location is
    `interpreters/<archive_sha256>/bin/python3.11`, which is not known until
    `stage` runs on the target machine.

    `python_version` is `major.minor` of the interpreter the release *pins*, and
    the caller is `package_release` for that reason — see `_shebang_version`.
    """

    scripts = destination / "bin"
    if not scripts.is_dir():
        return []
    replacement = f"{_GENERIC_SHEBANG_PREFIX}{python_version}"
    rewritten: dict[str, tuple[str, int]] = {}
    for item in sorted(scripts.iterdir()):
        if item.is_symlink() or not item.is_file():
            continue
        original = item.read_bytes()
        if not original.startswith(b"#!"):
            continue
        first, separator, rest = original.partition(b"\n")
        if not separator:
            continue
        updated = replacement.encode("utf-8") + separator + rest
        if updated == original:
            continue
        item.write_bytes(updated)
        rewritten[item.name] = (_record_digest(updated), len(updated))
    if rewritten:
        _rewrite_records(destination, rewritten)
    return sorted(rewritten)


def _rewrite_records(destination: Path, rewritten: Mapping[str, tuple[str, int]]) -> None:
    """Point every dist-info RECORD at the bytes the payload now carries."""

    for record in sorted(destination.glob("*.dist-info/RECORD")):
        lines = record.read_text(encoding="utf-8").splitlines()
        changed = False
        updated_lines = []
        for line in lines:
            path, _, remainder = line.partition(",")
            name = PurePosixPath(path).name
            if path.endswith(f"bin/{name}") and name in rewritten and remainder:
                digest, size = rewritten[name]
                line = f"{path},{digest},{size}"
                changed = True
            updated_lines.append(line)
        if changed:
            record.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def _payload_files(payload: Path) -> list[tuple[str, Path]]:
    if payload.is_symlink() or not payload.is_dir():
        raise PackagingError("payload directory does not exist")
    files: list[tuple[str, Path]] = []
    for item in sorted(payload.rglob("*")):
        relative = item.relative_to(payload)
        if item.is_symlink():
            # `_extract_archive` refuses archived symlinks outright. Refusing
            # here turns a remote "artifact archive is invalid" into a local
            # message naming the file.
            raise PackagingError(f"payload contains a symlink: {relative}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise PackagingError(f"payload contains a special file: {relative}")
        files.append((relative.as_posix(), item))
    if not files:
        raise PackagingError("payload directory is empty")
    return files


def _write_artifact(destination: Path, files: Sequence[tuple[str, Path]]) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, source in files:
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.external_attr = _REGULAR_FILE
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes())
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _runtime_pin(pin: Path, archive: Path) -> dict[str, str]:
    """Read the vendor tool's committed pin and bind it to these archive bytes.

    The pin is the only place the interpreter's relative path and CPython version
    exist as measurements rather than as something a packager could type, so it
    is the source for both. It is also checked against the archive it is shipped
    beside: a pin naming other bytes would let a release declare one interpreter
    and carry another, and the manifest's own `archive_sha256` — derived from the
    packaged bytes below — would agree with the archive rather than catch it.
    """

    try:
        document = json.loads(pin.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackagingError(f"runtime pin is unreadable: {pin}") from exc
    if not isinstance(document, dict):
        raise PackagingError("runtime pin is not an object")
    try:
        interpreter = document["interpreter_path"]
        version = document["version"]
        described = document["archive"]
        name = described["name"]
        digest = described["sha256"]
    except (KeyError, TypeError) as exc:
        raise PackagingError("runtime pin does not describe an archive") from exc
    if not all(isinstance(value, str) for value in (interpreter, version, name, digest)):
        raise PackagingError("runtime pin fields are not strings")
    if name != archive.name:
        raise PackagingError("runtime pin does not name the archive it ships with")
    observed = hashlib.sha256(archive.read_bytes()).hexdigest()
    if observed != digest:
        raise PackagingError("runtime archive does not match its pin")
    return {"interpreter_relative": interpreter, "python_version": version}


def _place_runtime(payload: Path, archive: Path, pin: Path) -> str:
    """Copy the archive and its pin into the payload as ordinary files.

    Ordinary is the point: `_write_artifact` then digests them like any other
    payload bytes, `import_release` extracts them into `content/`, and the slot's
    content-tree digest binds the interpreter for free — no second schema, no
    second trust root.
    """

    for source in (archive, pin):
        if source.is_symlink() or not source.is_file():
            raise PackagingError(f"runtime input is not a regular file: {source}")
    destination = payload / RUNTIME_DIRECTORY
    if destination.exists():
        raise PackagingError(f"payload already carries a {RUNTIME_DIRECTORY}/ directory")
    destination.mkdir(parents=True)
    shutil.copyfile(archive, destination / archive.name)
    shutil.copyfile(pin, destination / RUNTIME_PIN_NAME)
    return f"{RUNTIME_DIRECTORY}/{archive.name}"


def _place_worker_modules(payload: Path) -> dict[str, Path]:
    """Copy the product's own worker-side modules in beside the entrypoint.

    Product-owned, never fork-owned: the worker protocol is the product's
    contract, and a fork that could rewrite it could answer its own identity
    questions. They ride as ordinary payload files, so the slot's content-tree
    digest attests them exactly like the entrypoint that imports them.
    """

    destination = payload / WORKER_PACKAGE
    if destination.exists():
        raise PackagingError(f"payload already carries a {WORKER_PACKAGE}/ directory")
    destination.mkdir(parents=True)
    placed: dict[str, Path] = {}
    for relative, source in module_sources().items():
        if source.is_symlink() or not source.is_file():
            raise PackagingError(f"worker module is not a regular file: {source}")
        target = payload / relative
        shutil.copyfile(source, target)
        placed[relative] = target
    if not placed:
        raise PackagingError("the product carries no worker modules to package")
    return placed


def _packaged_member_sha256(artifact: Path, relative: str) -> str:
    """Digest the member as it exists in the artifact, not the file it came from.

    "Derived from the bytes packaged" is meant literally: this reads back out of
    the zip that was just written, so no copy step between the source archive and
    the artifact can leave the manifest describing bytes the release does not
    carry.
    """

    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(artifact) as archive:
            with archive.open(relative) as member:
                while chunk := member.read(1024 * 1024):
                    digest.update(chunk)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise PackagingError("packaged runtime archive could not be re-read") from exc
    return digest.hexdigest()


def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise PackagingError("catalog timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _target_platform(wheels: Sequence[str]) -> dict[str, object]:
    """The host that resolved the closure, carrying the closure's own OS floor.

    `system` and `machine` come from `host_platform()` so producer and consumer
    cannot drift on how a host is named. `minimum_os_version` deliberately does
    not: that field of `host_platform()` is the packaging machine's *current*
    OS version, which bounds nothing useful — packaging on macOS 26 would then
    refuse every host below 26 for a closure that runs happily on 14.
    """

    target = dict(host_platform())
    target["minimum_os_version"] = derive_minimum_os_version(wheels)
    if target["minimum_os_version"] is not None and target["system"] != "Darwin":
        # A macOS floor on a non-Darwin target is satisfiable by no host at all,
        # so the release would be unimportable everywhere. Fail here, where the
        # reason is still "these wheels and this host disagree".
        raise PackagingError(
            "closure carries macOS wheels but the packaging host is not Darwin"
        )
    return target


def package_release(
    *,
    identity: ReleaseIdentity,
    closure: Path,
    payload: Path,
    output: Path,
    catalog_sequence: int,
    issued_at: datetime,
    expires_at: datetime,
    python_runtime: Path,
    python_runtime_pin: Path,
    worker_entrypoint: str = "runtime_worker.py",
    patches: Sequence[Mapping[str, object]] = (),
    key_id: str = "developer-unsigned",
) -> PackagedRelease:
    """Package one Hermes release into the four documents `import` consumes.

    `closure` is a wheelhouse as `vendor_wheelhouse.py` leaves it; `payload` is
    the expanded tree that becomes the artifact — the Hermes distribution with
    the worker entrypoint beside it, not nested, because `import_release`
    extracts the archive at `content/` and then looks for `content/<entrypoint>`.

    `python_runtime` and `python_runtime_pin` are the vendor tool's outputs for
    the interpreter this release runs its worker under. They are required, not
    optional: a schema-3 manifest has no way to say "no interpreter", and a
    release that cannot name the CPython it needs is the failure S3.2 exists to
    remove.
    """

    wheels = _closure_wheels(closure)
    requirements = closure / REQUIREMENTS_NAME
    if not requirements.is_file():
        raise PackagingError(f"dependency closure has no {REQUIREMENTS_NAME}")

    described_runtime = _runtime_pin(python_runtime_pin, python_runtime)
    # ⟦ADJ-19⟧ Here rather than in `expand_closure`, because this is the only
    # place the release's own interpreter version exists as a measurement. An
    # expansion step cannot know it without being handed a version to type, and
    # this module's rule is that such fields are derived, never accepted.
    normalize_console_scripts(
        payload, python_version=_shebang_version(described_runtime["python_version"])
    )
    runtime_relative = _place_runtime(payload, python_runtime, python_runtime_pin)
    worker_modules = _place_worker_modules(payload)
    files = _payload_files(payload)
    if worker_entrypoint not in {name for name, _ in files}:
        raise PackagingError(
            f"payload does not carry the worker entrypoint {worker_entrypoint!r} at its root"
        )

    output.mkdir(parents=True, exist_ok=True)
    artifact_filename = f"{identity.release_id}.zip"
    artifact = output / artifact_filename
    artifact_sha256 = _write_artifact(artifact, files)
    worker_runtime = {
        "archive": runtime_relative,
        "archive_sha256": _packaged_member_sha256(artifact, runtime_relative),
        **described_runtime,
    }
    # Derived from the packaged bytes for the same reason `archive_sha256` is:
    # the manifest describes what the artifact carries, not what a directory on
    # the packaging host happened to hold when the copy started.
    packaged_modules = {
        relative: _packaged_member_sha256(artifact, relative)
        for relative in sorted(worker_modules)
    }

    patch_ledger: dict[str, object] = {
        "schema_version": 1,
        "release_id": identity.release_id,
        "upstream_commit": identity.upstream_commit,
        "patches": [dict(patch) for patch in patches],
    }

    manifest: dict[str, object] = {
        "schema_version": 3,
        "release_id": identity.release_id,
        "release_sequence": identity.release_sequence,
        "distribution_name": "hermes-agent",
        "distribution_version": identity.distribution_version,
        "upstream_repository": identity.upstream_repository,
        "upstream_tag": identity.upstream_tag,
        "upstream_commit": identity.upstream_commit,
        "artifact_filename": artifact_filename,
        "artifact_sha256": artifact_sha256,
        "publisher": identity.publisher,
        "workflow": identity.workflow,
        "python_range": identity.python_range,
        "dependency_lock_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        "adapter_protocol": identity.adapter_protocol,
        "session_schema": identity.session_schema,
        "patch_set_sha256": "",
        "evidence_sha256": identity.evidence_sha256,
        "worker_entrypoint": worker_entrypoint,
        "worker_runtime": worker_runtime,
        "worker_modules": packaged_modules,
        "platform": _target_platform(wheels),
    }

    # Validate through the consumer's own models rather than trusting the
    # literals above. A producer that can emit a document its consumer rejects
    # is the defect this slice exists to remove, one layer up.
    try:
        manifest["patch_set_sha256"] = PatchLedger.from_dict(patch_ledger).digest
        release = ReleaseManifest.from_dict(manifest)
    except ValidationError as exc:
        raise PackagingError(f"packaged release manifest is invalid: {exc}") from exc

    attestation: dict[str, object] = {
        "schema_version": 1,
        "artifact_sha256": artifact_sha256,
        "repository": identity.upstream_repository,
        "tag": identity.upstream_tag,
        "commit": identity.upstream_commit,
        "publisher": identity.publisher,
        "workflow": identity.workflow,
    }

    catalog_payload: dict[str, object] = {
        "schema_version": 1,
        "sequence": catalog_sequence,
        "issued_at": _rfc3339(issued_at),
        "expires_at": _rfc3339(expires_at),
        "entries": [
            {
                "release_id": identity.release_id,
                "manifest_sha256": release.digest,
                "status": "certified",
            }
        ],
    }
    payload_bytes = canonical_json(catalog_payload)
    attestation_bytes = canonical_json(attestation)
    catalog: dict[str, object] = {
        "payload": catalog_payload,
        # Unsigned developer channel: `key_id`/`signature` record the digest
        # they stand in for rather than pretending to be a signature. The
        # pins.json written below is an operator convenience — it saves
        # hand-deriving the digests — not an out-of-band anchor: shipped in
        # the same directory it pins, it authenticates nothing. The digest
        # must reach `import` by a path this directory does not control
        # (contract S3.0 residual 3 records the real trust position).
        "key_id": key_id,
        "signature": f"unsigned:{hashlib.sha256(payload_bytes).hexdigest()}",
    }
    try:
        # The catalog is validated through the consumer's own model like the
        # manifest and the ledger above; without this, `--valid-days 0` emits
        # a document set whose only possible consumer verdict is refusal.
        CatalogEnvelope.from_dict(catalog)
    except ValidationError as exc:
        raise PackagingError(f"packaged catalog is invalid: {exc}") from exc

    pins = {
        "catalog_payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "attestation_sha256": hashlib.sha256(attestation_bytes).hexdigest(),
        "manifest_sha256": release.digest,
        "artifact_sha256": artifact_sha256,
    }

    written = {"artifact": artifact}
    for name, document in (
        (MANIFEST_DOCUMENT, manifest),
        (CATALOG_DOCUMENT, catalog),
        (ATTESTATION_DOCUMENT, attestation),
        (PATCH_LEDGER_DOCUMENT, patch_ledger),
        (PINS_DOCUMENT, pins),
    ):
        target = output / f"{name}.json"
        target.write_bytes(canonical_json(document) + b"\n")
        written[name] = target

    return PackagedRelease(
        artifact=artifact,
        manifest=manifest,
        catalog=catalog,
        attestation=attestation,
        patch_ledger=patch_ledger,
        pins=pins,
        paths=written,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closure", type=Path, required=True)
    # `hermes-agent` is a distribution in the pinned closure, so expanding the
    # closure produces it; there is no separate source to point at.
    parser.add_argument("--worker-entrypoint-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-sequence", type=int, required=True)
    parser.add_argument("--distribution-version", required=True)
    parser.add_argument("--upstream-repository", required=True)
    parser.add_argument("--upstream-tag", required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--python-range", required=True)
    parser.add_argument("--adapter-protocol", required=True)
    parser.add_argument("--session-schema", type=int, required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--catalog-sequence", type=int, required=True)
    parser.add_argument("--valid-days", type=int, default=30)
    parser.add_argument("--worker-entrypoint", default="runtime_worker.py")
    parser.add_argument(
        "--python-runtime",
        type=Path,
        required=True,
        help="vendored CPython archive for the worker interpreter",
    )
    parser.add_argument(
        "--python-runtime-pin",
        type=Path,
        required=True,
        help="the pin document `vendor_python_runtime.py` emitted beside it",
    )
    # A1 finding 1. The closure is expanded by whatever pip the packaging host
    # happens to run unless the operator says otherwise, and a cp311 closure
    # expanded by a cp314 pip resolves the wrong environment markers and lands
    # the wrong native wheels. That — which wheels pip selects — is all this
    # flag decides now: ⟦ADJ-19⟧ moved the console-script shebang onto
    # `--python-runtime-pin`, so the artifact digest no longer moves with the
    # builder whether this is passed or not. It stays an operator choice because
    # the vendored runtime ships as a tarball, not as an installed interpreter,
    # and a producer that unpacked 38 MB to find a pip would be doing the
    # product's staging job in the wrong place.
    parser.add_argument(
        "--python-executable",
        type=Path,
        default=None,
        help="interpreter whose pip expands the closure (default: this one)",
    )
    # Required rather than defaulted to empty. `package_release` has always
    # taken `patches`, but no flag reached it, so every release built through
    # this command carried an empty ledger and failed §5.9 — silently, because
    # packaging still exits 0 and the gap only appears when someone tries to
    # certify. The fork this product ships always diverges; a build that cannot
    # say how is a build that cannot be approved.
    parser.add_argument(
        "--patches",
        type=Path,
        required=True,
        help=(
            "JSON list of patch-ledger entries, or a patch_ledger.json carrying "
            "one, describing what the fork adds over --upstream-commit"
        ),
    )
    # No --minimum-os-version. It is derived; see the module docstring.
    arguments = parser.parse_args(argv)

    # Before the expansion, which runs pip over the whole closure: a ledger the
    # consumer would refuse should cost a parse, not several minutes.
    patches = load_patch_entries(arguments.patches)

    issued = datetime.now(timezone.utc)
    payload = expand_closure(
        closure=arguments.closure,
        destination=arguments.output / "payload",
        extra_files={arguments.worker_entrypoint: arguments.worker_entrypoint_source},
        python_executable=arguments.python_executable,
    )
    result = package_release(
        identity=ReleaseIdentity(
            release_id=arguments.release_id,
            release_sequence=arguments.release_sequence,
            distribution_version=arguments.distribution_version,
            upstream_repository=arguments.upstream_repository,
            upstream_tag=arguments.upstream_tag,
            upstream_commit=arguments.upstream_commit,
            publisher=arguments.publisher,
            workflow=arguments.workflow,
            python_range=arguments.python_range,
            adapter_protocol=arguments.adapter_protocol,
            session_schema=arguments.session_schema,
            evidence_sha256=arguments.evidence_sha256,
        ),
        closure=arguments.closure,
        payload=payload,
        output=arguments.output,
        catalog_sequence=arguments.catalog_sequence,
        issued_at=issued,
        expires_at=issued + timedelta(days=arguments.valid_days),
        python_runtime=arguments.python_runtime,
        python_runtime_pin=arguments.python_runtime_pin,
        worker_entrypoint=arguments.worker_entrypoint,
        patches=patches,
    )
    print(canonical_json(result.pins).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
