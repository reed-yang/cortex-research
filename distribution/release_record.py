"""The release descriptor and the build record generated from it.

One checked-in `release.toml` selects a composition; this module reconciles it
against the source tree and the real artifacts and emits the immutable record
the build already writes. Only independently chosen values are handwritten --
source commit, bundle digest, Web build identity and the whole certified worker
identity are read back out of what was actually produced, and a disagreement is
a refusal rather than a recorded value.

This describes a BUILD, not installed state. It never reads `current.json`,
never writes an installer pointer, and is not consulted at install, upgrade or
rollback time: those keep their existing admission points in `install.py` and
`state_safety.py`, which read the candidate runtime and the live database. The
public product version and the installer pointer's `version` field are different
things and are kept apart deliberately -- see `release_pointer_version`.

Standard library only, and no import of `cortex_platform`: this file is globbed
into every bundle's `tools/distribution/` by `_copy_distribution_tools`, where it
runs beside a foreign generation's code, and it reads a source tree that has no
runtime yet.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from .schema import ClosedSchemaError, canonical_json_bytes, exact_mapping


#: The format of `release.toml` itself. Not a database migration, not the bundle
#: manifest schema, and not the public product version.
DESCRIPTOR_FORMAT_VERSION = 1

#: The generated record's own format.
RECORD_SCHEMA = "cortex-release-record/1"

#: Legacy records carry no `schema` key at all; the gen-16 build wrote a bare
#: object. They stay readable so a predecessor can be named without rebuilding
#: it.
_LEGACY_RECORD_FIELDS = frozenset(
    {
        "source_commit",
        "release_id",
        "release_sequence",
        "control_schema",
        "bundle",
        "bundle_digest",
        "version",
        "web_build_id",
        "previous_bundle_digest",
    }
)

_DESCRIPTOR_FIELDS = {"format_version", "product", "control", "worker"}
_PRODUCT_FIELDS = {"version", "release_id", "release_sequence"}
_CONTROL_FIELDS = {"target_schema", "upgrade_from_schemas"}
_WORKER_FIELDS = {"release_id", "adapter_protocol"}

# Mirrors `install.py:_IDENTIFIER` -- the descriptor may not name a release the
# installer would refuse.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
#: A public product version: dotted numeric with an optional pre-release tail.
#: Deliberately NOT the installer pointer shape, which ends in a 16-hex digest.
_PRODUCT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?$")
_PROTOCOL = re.compile(r"^[a-z][a-z0-9-]*/[0-9]+$")

_SCHEMA_MODULE = "cortex_platform/product/control/schema.py"


class ReleaseRecordError(ValueError):
    """The descriptor, the source or an artifact disagree about the release."""


class ReleaseDescriptor:
    """The handwritten half of a release composition."""

    __slots__ = (
        "product_version",
        "release_id",
        "release_sequence",
        "target_schema",
        "upgrade_from_schemas",
        "worker_release_id",
        "adapter_protocol",
    )

    def __init__(
        self,
        *,
        product_version: str,
        release_id: str,
        release_sequence: int,
        target_schema: int,
        upgrade_from_schemas: tuple[int, ...],
        worker_release_id: str,
        adapter_protocol: str,
    ) -> None:
        self.product_version = product_version
        self.release_id = release_id
        self.release_sequence = release_sequence
        self.target_schema = target_schema
        self.upgrade_from_schemas = upgrade_from_schemas
        self.worker_release_id = worker_release_id
        self.adapter_protocol = adapter_protocol

    def as_dict(self) -> dict[str, object]:
        return {
            "format_version": DESCRIPTOR_FORMAT_VERSION,
            "product": {
                "version": self.product_version,
                "release_id": self.release_id,
                "release_sequence": self.release_sequence,
            },
            "control": {
                "target_schema": self.target_schema,
                "upgrade_from_schemas": list(self.upgrade_from_schemas),
            },
            "worker": {
                "release_id": self.worker_release_id,
                "adapter_protocol": self.adapter_protocol,
            },
        }


def _exact_int(value: object, *, label: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ReleaseRecordError(f"{label} is invalid")
    return value


def _exact_str(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ReleaseRecordError(f"{label} is invalid")
    return value


def load_descriptor(path: Path) -> ReleaseDescriptor:
    """Read `release.toml` under a closed schema at every level."""

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseRecordError("release descriptor is unreadable") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseRecordError("release descriptor is not valid TOML") from exc
    return parse_descriptor(raw)


def parse_descriptor(raw: object) -> ReleaseDescriptor:
    """Validate an already-decoded descriptor mapping."""

    try:
        document = exact_mapping(raw, _DESCRIPTOR_FIELDS, label="release descriptor")
        product = exact_mapping(
            document["product"], _PRODUCT_FIELDS, label="release descriptor product"
        )
        control = exact_mapping(
            document["control"], _CONTROL_FIELDS, label="release descriptor control"
        )
        worker = exact_mapping(
            document["worker"], _WORKER_FIELDS, label="release descriptor worker"
        )
    except ClosedSchemaError as exc:
        raise ReleaseRecordError(str(exc)) from exc

    if document["format_version"] != DESCRIPTOR_FORMAT_VERSION:
        raise ReleaseRecordError("release descriptor format is unsupported")

    target = _exact_int(control["target_schema"], label="target control schema")
    upgrades = control["upgrade_from_schemas"]
    if not isinstance(upgrades, list) or not upgrades:
        raise ReleaseRecordError("upgrade_from_schemas is invalid")
    supported: list[int] = []
    for entry in upgrades:
        value = _exact_int(entry, label="upgrade_from_schemas entry")
        if value >= target:
            raise ReleaseRecordError(
                "upgrade_from_schemas must name schemas below the target"
            )
        supported.append(value)
    if len(set(supported)) != len(supported):
        raise ReleaseRecordError("upgrade_from_schemas repeats a schema")

    return ReleaseDescriptor(
        product_version=_exact_str(
            product["version"], _PRODUCT_VERSION, label="public product version"
        ),
        release_id=_exact_str(
            product["release_id"], _IDENTIFIER, label="product release identifier"
        ),
        release_sequence=_exact_int(
            product["release_sequence"], label="product release sequence"
        ),
        target_schema=target,
        upgrade_from_schemas=tuple(sorted(supported)),
        worker_release_id=_exact_str(
            worker["release_id"], _IDENTIFIER, label="worker release identifier"
        ),
        adapter_protocol=_exact_str(
            worker["adapter_protocol"], _PROTOCOL, label="worker adapter protocol"
        ),
    )


def release_pointer_version(release_id: str, bundle_digest: str) -> str:
    """The installer's generation-directory identity, derived the one way.

    `DistributionInstaller._pointer` builds exactly this string and
    `_validate_pointer` refuses a pointer that is not it, so the record reports
    the same value rather than inventing a second spelling. The public product
    version never appears here.
    """

    _exact_str(release_id, _IDENTIFIER, label="product release identifier")
    _exact_str(bundle_digest, _SHA256, label="bundle digest")
    return f"{release_id}-{bundle_digest[:16]}"


def read_source_control_schema(repository: Path) -> tuple[int, tuple[int, ...]]:
    """Read `SCHEMA_VERSION` and the declared migrations without importing.

    A source checkout has no runtime to execute, and importing the product to
    learn its own schema would make the reconciliation depend on the very tree it
    is checking. The constants are plain assignments, including alias chains such
    as `SCHEMA_VERSION = THREAD_ARCHIVE_MIGRATION`, so they resolve statically.
    """

    path = repository / _SCHEMA_MODULE
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ReleaseRecordError("source control schema is unreadable") from exc

    constants: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and type(value.value) is int:
            constants[target.id] = value.value
        elif isinstance(value, ast.Name) and value.id in constants:
            constants[target.id] = constants[value.id]

    schema_version = constants.get("SCHEMA_VERSION")
    if type(schema_version) is not int:
        raise ReleaseRecordError("source control schema version is unavailable")

    declared = _declared_migrations(tree, constants)
    if not declared:
        raise ReleaseRecordError("source declares no control migrations")
    return schema_version, declared


def _declared_migrations(
    tree: ast.Module, constants: Mapping[str, int]
) -> tuple[int, ...]:
    """The version of every tuple `migration_scripts()` returns, in order."""

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "migration_scripts":
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Return):
                continue
            value = statement.value
            if not isinstance(value, ast.Tuple):
                continue
            versions: list[int] = []
            for element in value.elts:
                if not isinstance(element, ast.Tuple) or not element.elts:
                    return ()
                first = element.elts[0]
                if isinstance(first, ast.Constant) and type(first.value) is int:
                    versions.append(first.value)
                elif isinstance(first, ast.Name) and first.id in constants:
                    versions.append(constants[first.id])
                else:
                    return ()
            return tuple(versions)
    return ()


def read_worker_manifest(directory: Path) -> dict[str, object]:
    """Derive the certified worker identity from the Hermes release manifest."""

    path = directory / "manifest.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRecordError("worker manifest is unreadable") from exc
    if not isinstance(raw, dict):
        raise ReleaseRecordError("worker manifest is invalid")

    modules = raw.get("worker_modules")
    if not isinstance(modules, dict) or not modules:
        raise ReleaseRecordError("worker manifest declares no worker modules")
    for relative, digest in modules.items():
        if not isinstance(relative, str) or not relative:
            raise ReleaseRecordError("worker manifest module path is invalid")
        _exact_str(digest, _SHA256, label="worker module digest")

    return {
        "release_id": _exact_str(
            raw.get("release_id"), _IDENTIFIER, label="worker release identifier"
        ),
        "release_sequence": _exact_int(
            raw.get("release_sequence"), label="worker release sequence"
        ),
        "distribution_name": _exact_str(
            raw.get("distribution_name"), _IDENTIFIER, label="worker distribution name"
        ),
        "distribution_version": _exact_str(
            raw.get("distribution_version"),
            _PRODUCT_VERSION,
            label="worker distribution version",
        ),
        "adapter_protocol": _exact_str(
            raw.get("adapter_protocol"), _PROTOCOL, label="worker adapter protocol"
        ),
        "session_schema": _exact_int(
            raw.get("session_schema"), label="worker session schema"
        ),
        "artifact_sha256": _exact_str(
            raw.get("artifact_sha256"), _SHA256, label="worker artifact digest"
        ),
        "worker_modules": dict(sorted(modules.items())),
    }


def verify_worker_payload(
    repository: Path, commit: str, worker: Mapping[str, object]
) -> int:
    """Prove the source carries exactly the certified worker bytes.

    The payload is not changed by this lane, so the check is that the tree being
    released still matches the manifest that certified it. The expected count
    comes from the manifest, never from a literal.
    """

    import hashlib

    _exact_str(commit, _COMMIT, label="source commit")
    modules = worker["worker_modules"]
    assert isinstance(modules, dict)
    matched = 0
    for relative, expected in modules.items():
        path = (
            relative
            if relative.startswith("cortex_platform/")
            else f"cortex_platform/product/runtime_update/worker_payload/{relative}"
        )
        try:
            blob = subprocess.check_output(
                ["git", "-C", str(repository), "show", f"{commit}:{path}"]
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseRecordError(
                f"certified worker module is absent from the source: {path}"
            ) from exc
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ReleaseRecordError(
                f"certified worker module does not match the manifest: {path}"
            )
        matched += 1
    if matched != len(modules):
        raise ReleaseRecordError("certified worker payload is incomplete")
    return matched


def read_release_record(path: Path) -> dict[str, object]:
    """Read a generated or legacy release record."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRecordError("release record is unreadable") from exc
    if not isinstance(raw, dict):
        raise ReleaseRecordError("release record is invalid")
    schema = raw.get("schema")
    if schema is None:
        missing = _LEGACY_RECORD_FIELDS - set(raw)
        if missing:
            raise ReleaseRecordError("release record is invalid")
    elif schema != RECORD_SCHEMA:
        raise ReleaseRecordError("release record format is unsupported")
    _exact_str(
        raw.get("release_id"), _IDENTIFIER, label="release record identifier"
    )
    _exact_int(raw.get("release_sequence"), label="release record sequence")
    _exact_str(raw.get("bundle_digest"), _SHA256, label="release record digest")
    return raw


def find_release_records(search_roots: Iterable[Path]) -> list[tuple[Path, dict]]:
    """Every readable release record directly under the given roots.

    Build records, not installed state: this looks at what has been produced
    locally so an allocation collision is reported instead of guessed at.
    """

    found: list[tuple[Path, dict]] = []
    for root in search_roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("*/release.json")):
            try:
                found.append((candidate, read_release_record(candidate)))
            except ReleaseRecordError:
                continue
    return found


def check_sequence_available(
    descriptor: ReleaseDescriptor, records: Sequence[tuple[Path, Mapping[str, object]]]
) -> None:
    """Refuse a sequence or identifier already spent on a different release."""

    for path, record in records:
        same_sequence = record.get("release_sequence") == descriptor.release_sequence
        same_identity = record.get("release_id") == descriptor.release_id
        if same_sequence and not same_identity:
            raise ReleaseRecordError(
                "release sequence "
                f"{descriptor.release_sequence} is already allocated to "
                f"{record.get('release_id')!r} ({path})"
            )
        if same_identity and not same_sequence:
            raise ReleaseRecordError(
                f"release {descriptor.release_id!r} already exists at sequence "
                f"{record.get('release_sequence')} ({path})"
            )


def validate_predecessor(descriptor: ReleaseDescriptor, previous: Mapping[str, object]) -> None:
    """Check the supported predecessor before spending work on a build."""

    previous_sequence = previous.get("release_sequence")
    if type(previous_sequence) is not int:
        raise ReleaseRecordError("predecessor release sequence is invalid")
    if previous_sequence >= descriptor.release_sequence:
        raise ReleaseRecordError(
            "predecessor release sequence is not below this release"
        )
    previous_schema = previous.get("control_schema")
    if type(previous_schema) is not int or previous_schema > descriptor.target_schema:
        raise ReleaseRecordError(
            "predecessor control schema is not below this release"
        )
    # A code-only update needs no migration and must still retain its predecessor.
    if (previous_schema != descriptor.target_schema
            and previous_schema not in descriptor.upgrade_from_schemas):
        raise ReleaseRecordError(
            f"predecessor control schema {previous_schema} is not a declared "
            "upgrade path"
        )


def build_release_record(
    descriptor: ReleaseDescriptor,
    *,
    source_commit: str,
    source_schema_version: int,
    declared_migrations: Sequence[int],
    bundle_path: Path,
    bundle_manifest: Mapping[str, object],
    bundle_digest: str,
    web_build_id: str,
    worker: Mapping[str, object],
    previous: Mapping[str, object] | None,
) -> dict[str, object]:
    """Reconcile the descriptor with the source and artifacts, then record it.

    Every refusal below is a disagreement between what was chosen and what was
    actually produced. Nothing here decides installed state.
    """

    if source_schema_version != descriptor.target_schema:
        raise ReleaseRecordError(
            f"descriptor targets control schema {descriptor.target_schema} "
            f"but the source declares {source_schema_version}"
        )
    declared = tuple(declared_migrations)
    if not declared or max(declared) != descriptor.target_schema:
        raise ReleaseRecordError(
            "descriptor target schema is not the source's highest migration"
        )
    unknown = [
        version
        for version in descriptor.upgrade_from_schemas
        if version not in declared
    ]
    if unknown:
        raise ReleaseRecordError(
            f"upgrade_from_schemas names undeclared migrations: {unknown}"
        )

    if bundle_manifest.get("release_id") != descriptor.release_id:
        raise ReleaseRecordError(
            "bundle manifest release identifier does not match the descriptor"
        )
    if bundle_manifest.get("release_sequence") != descriptor.release_sequence:
        raise ReleaseRecordError(
            "bundle manifest release sequence does not match the descriptor"
        )
    source = bundle_manifest.get("source")
    if not isinstance(source, dict) or source.get("commit") != source_commit:
        raise ReleaseRecordError(
            "bundle manifest source commit does not match the built source"
        )

    if worker.get("release_id") != descriptor.worker_release_id:
        raise ReleaseRecordError(
            "worker manifest release identifier does not match the descriptor"
        )
    if worker.get("adapter_protocol") != descriptor.adapter_protocol:
        raise ReleaseRecordError(
            "worker manifest adapter protocol does not match the descriptor"
        )

    _exact_str(source_commit, _COMMIT, label="source commit")
    _exact_str(bundle_digest, _SHA256, label="bundle digest")

    record: dict[str, object] = {
        "schema": RECORD_SCHEMA,
        "descriptor_format_version": DESCRIPTOR_FORMAT_VERSION,
        "product_version": descriptor.product_version,
        "source_commit": source_commit,
        "release_id": descriptor.release_id,
        "release_sequence": descriptor.release_sequence,
        "control_schema": descriptor.target_schema,
        "upgrade_from_schemas": list(descriptor.upgrade_from_schemas),
        "bundle": str(bundle_path),
        "bundle_digest": bundle_digest,
        # The installer's own generation-directory identity, re-derived here so
        # the record reports it rather than a second spelling of it. This is NOT
        # `product_version`.
        "version": release_pointer_version(descriptor.release_id, bundle_digest),
        "web_build_id": web_build_id,
        "worker": {
            "release_id": worker["release_id"],
            "release_sequence": worker["release_sequence"],
            "distribution_name": worker["distribution_name"],
            "distribution_version": worker["distribution_version"],
            "adapter_protocol": worker["adapter_protocol"],
            "session_schema": worker["session_schema"],
            "artifact_sha256": worker["artifact_sha256"],
            "worker_module_count": len(worker["worker_modules"]),
        },
    }

    if previous is None:
        record["previous_bundle_digest"] = None
        record["previous_release_id"] = None
        record["previous_release_sequence"] = None
        record["previous_control_schema"] = None
        return record

    validate_predecessor(descriptor, previous)
    previous_sequence = previous["release_sequence"]
    previous_schema = previous["control_schema"]
    record["previous_bundle_digest"] = previous["bundle_digest"]
    record["previous_release_id"] = previous["release_id"]
    record["previous_release_sequence"] = previous_sequence
    record["previous_control_schema"] = previous_schema
    return record


def record_bytes(record: Mapping[str, object]) -> bytes:
    """The record as written: one deterministic serialization, newline ended."""

    return canonical_json_bytes(record) + b"\n"
