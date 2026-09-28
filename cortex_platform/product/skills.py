"""Operator-registered skills: a capability the engine may run, and its identity.

A skill is an Agent Skills directory (`SKILL.md` plus whatever it ships) that
the operator installs outside the product; the bundle never carries one. The
product consumes a skill only through a named capability slot, and only after
the operator has accepted that exact package. Acceptance records a digest of
the package tree and every use recomputes it, so a package that changed since
it was accepted -- edited, pulled, or replaced by a sync tool -- is treated as
a different capability until the operator accepts it again. The engine fails
closed instead of running whatever now sits at the accepted path.

The declaration lives in the Agent Skills `metadata` map, which the
specification reserves for client-specific string values, so one `SKILL.md`
stays valid for Claude Code and Codex:

    metadata:
      cortex-capability: ocr
      cortex-entry: scripts/ingest_paper.py
      cortex-interpreter: .venv/bin/python

The product imports nothing outside the standard library, so this module reads
that one block with a line parser rather than a YAML library: `metadata:` at
column zero, then indented `key: value` lines holding single-line scalars.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

#: The capability slots some product consumer calls, and the contract a skill
#: declaring one has to honour. A skill naming any other capability is listed
#: by `status` and otherwise ignored.
KNOWN_CAPABILITIES: Mapping[str, str] = {
    "ocr": (
        "The paper-ingestion command line: `<entry> <pdf> --engine <name> "
        "--output-dir <dir> --image-format png`, one JSON object on stdout "
        "naming `markdown_path` and `paper_dir`."
    ),
}

READY = "ready"
UNCONFIGURED = "unconfigured"
MISSING = "missing"
AMBIGUOUS = "ambiguous"
INVALID = "invalid"
UNPREPARED = "unprepared"
NOT_ACCEPTED = "not_accepted"
CHANGED = "changed"
#: The states `accept` can move to `ready`. The others need the operator to
#: fix the package or the configuration first.
_ACCEPTABLE = frozenset({READY, NOT_ACCEPTED, CHANGED})

_CAPABILITY_KEY = "cortex-capability"
_ENTRY_KEY = "cortex-entry"
_INTERPRETER_KEY = "cortex-interpreter"
_METADATA_ENTRY = re.compile(r"^\s+(?P<key>[A-Za-z0-9_.-]+):\s*(?P<value>.*?)\s*$")

# Not part of a package's identity. Environments and caches are rebuilt from
# files the digest does cover (`pyproject.toml`, `uv.lock`), `.env` holds the
# operator's own credentials rather than the implementation, and sync-tool
# backups are that tool's output. The price is that the digest does not pin
# the prepared interpreter environment; `status` says so.
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".agent-sync-backups",
    }
)
_EXCLUDED_FILES = frozenset({".env", ".DS_Store"})
_EXCLUDED_SUFFIXES = (".pyc",)
_ACCEPTANCE_SCHEMA = 1


class SkillAcceptanceError(ValueError):
    """The capability cannot be accepted in its current state."""


@dataclass(frozen=True)
class SkillPackage:
    """One skill directory that declares a capability."""

    name: str
    path: Path
    capability: str
    entry: str
    interpreter: str
    # `invalid` or `unprepared` with its reason, or None when usable.
    problem: tuple[str, str] | None = None

    @property
    def entry_path(self) -> Path:
        return self.path / self.entry

    @property
    def interpreter_path(self) -> Path:
        return self.path / self.interpreter


@dataclass(frozen=True)
class CapabilityStatus:
    """Whether one capability slot may run, and why not when it may not.

    `reason` names packages but never paths: it travels into an effect's
    failure message, which is product state rather than an operator console.
    """

    capability: str
    state: str
    reason: str
    package: SkillPackage | None = None
    digest: str | None = None
    accepted_at: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == READY

    def binding_values(self) -> dict[str, str]:
        """The capability binding slots an effect child receives, if any."""

        if not self.ready or self.package is None:
            return {}
        return {
            f"{self.capability}.entry": str(self.package.entry_path),
            f"{self.capability}.interpreter": str(self.package.interpreter_path),
        }

    def to_dict(self) -> dict[str, Any]:
        package = self.package
        return {
            "capability": self.capability,
            "state": self.state,
            "reason": self.reason,
            "package": package.name if package else None,
            "path": str(package.path) if package else None,
            "entry": package.entry if package else None,
            "interpreter": package.interpreter if package else None,
            "digest": self.digest,
            "accepted_at": self.accepted_at,
        }


def configured_root(config: Mapping[str, Any]) -> Path | None:
    section = config.get("skills")
    if not section:
        return None
    return Path(str(dict(section)["root"]))


def acceptance_file(paths: Any) -> Path:
    return Path(paths.state_dir) / "skills" / "accepted.json"


def _scalar(raw: str) -> str:
    """One YAML scalar: plain, "double-quoted" (no escapes) or 'single-quoted'.

    Whatever follows the closing quote -- a `# comment` -- is dropped. An
    unclosed quote is returned as written, so it can only name a path that
    does not exist.
    """

    if raw[:1] == '"':
        end = raw.find('"', 1)
        return raw[1:end] if end > 0 else raw
    if raw[:1] == "'":
        value: list[str] = []
        index = 1
        while index < len(raw):
            if raw[index] == "'":
                if raw[index + 1 : index + 2] != "'":
                    return "".join(value)
                index += 1
            value.append(raw[index])
            index += 1
        return raw
    return raw.split(" #", 1)[0].strip()


def read_metadata(skill_md: Path) -> dict[str, str]:
    """The frontmatter `metadata` map of one SKILL.md, or {} without one."""

    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        # Unterminated frontmatter: nothing in it is a declaration.
        return {}
    metadata: dict[str, str] = {}
    inside = False
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.rstrip() == "metadata:"
            continue
        if inside and (match := _METADATA_ENTRY.fullmatch(line)):
            metadata[match["key"]] = _scalar(match["value"])
    return metadata


def _relative(value: str) -> PurePosixPath | None:
    """A declared path, if it is a plain relative path inside the package."""

    if not value or "\0" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate


def _problem(path: Path, entry: str, interpreter: str) -> tuple[str, str] | None:
    if _relative(entry) is None:
        return INVALID, f"{_ENTRY_KEY} must be a relative path inside the skill"
    if _relative(interpreter) is None:
        return INVALID, f"{_INTERPRETER_KEY} must be a relative path inside the skill"
    root = path.resolve()
    target = (path / entry).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return INVALID, f"{_ENTRY_KEY} does not name a file inside the skill"
    # Lexical containment only: a virtual environment's interpreter is a
    # symlink to a managed CPython elsewhere, and that is how it is meant to be.
    runner = path / interpreter
    if not runner.is_file() or not os.access(runner, os.X_OK):
        return UNPREPARED, (
            f"{_INTERPRETER_KEY} is not an executable file; prepare the skill's "
            "environment (for a uv project: uv sync --frozen)"
        )
    return None


def discover(root: Path) -> tuple[SkillPackage, ...]:
    """Every immediate child of `root` whose SKILL.md declares a capability."""

    packages: list[SkillPackage] = []
    for child in sorted(root.iterdir()):
        manifest = child / "SKILL.md"
        if not child.is_dir() or not manifest.is_file():
            continue
        try:
            metadata = read_metadata(manifest)
        except (OSError, UnicodeDecodeError):
            continue
        capability = metadata.get(_CAPABILITY_KEY)
        if not capability:
            continue
        entry = metadata.get(_ENTRY_KEY, "")
        interpreter = metadata.get(_INTERPRETER_KEY, "")
        packages.append(
            SkillPackage(
                name=child.name,
                path=child,
                capability=capability,
                entry=entry,
                interpreter=interpreter,
                problem=_problem(child, entry, interpreter),
            )
        )
    return tuple(packages)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def package_digest(path: Path) -> str:
    """sha256 over the package tree: relative paths, file bytes, link targets.

    Symlinks are recorded by their target text and never followed, so a link
    that is retargeted changes the digest and a link cannot pull an unrelated
    tree into it.
    """

    digest = hashlib.sha256()
    root = Path(path)
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        kept: list[str] = []
        for name in sorted(directories):
            if name in _EXCLUDED_DIRECTORIES:
                continue
            if (base / name).is_symlink():
                files.append(name)
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            if name in _EXCLUDED_FILES or name.endswith(_EXCLUDED_SUFFIXES):
                continue
            item = base / name
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                record = f"L\0{relative}\0{os.readlink(item)}"
            elif item.is_file():
                record = f"F\0{relative}\0{_file_digest(item)}"
            else:
                continue
            digest.update(record.encode("utf-8", "surrogateescape") + b"\n")
    return "sha256:" + digest.hexdigest()


def read_acceptances(paths: Any) -> dict[str, dict[str, str]]:
    """Accepted capabilities, or {} when nothing was ever accepted.

    An unreadable record accepts nothing: every capability reads as
    `not_accepted` until the operator accepts again, which rewrites it.
    """

    try:
        raw = json.loads(acceptance_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != _ACCEPTANCE_SCHEMA:
        return {}
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict):
        return {}
    return {
        name: {key: str(value) for key, value in record.items()}
        for name, record in capabilities.items()
        if isinstance(record, dict)
    }


def _write_acceptances(paths: Any, records: Mapping[str, Mapping[str, str]]) -> None:
    target = acceptance_file(paths)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = {"schema_version": _ACCEPTANCE_SCHEMA, "capabilities": dict(records)}
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def resolve_capability(
    config: Mapping[str, Any], paths: Any, capability: str
) -> CapabilityStatus:
    """The current state of one capability slot, recomputed from disk."""

    root = configured_root(config)
    if root is None:
        return CapabilityStatus(capability, UNCONFIGURED, "[skills] root is not configured")
    try:
        declared = [p for p in discover(root) if p.capability == capability]
    except OSError:
        return CapabilityStatus(
            capability, MISSING, "the configured skills root is not a readable directory"
        )
    if not declared:
        return CapabilityStatus(
            capability, MISSING, f"no skill declares {_CAPABILITY_KEY}: {capability}"
        )
    if len(declared) > 1:
        names = ", ".join(package.name for package in declared)
        return CapabilityStatus(
            capability, AMBIGUOUS, f"{names} all declare {capability}; keep exactly one"
        )
    package = declared[0]
    if package.problem is not None:
        state, reason = package.problem
        return CapabilityStatus(capability, state, f"{package.name}: {reason}", package)
    try:
        digest = package_digest(package.path)
    except OSError:
        return CapabilityStatus(
            capability, INVALID, f"{package.name} could not be read in full", package
        )
    record = read_acceptances(paths).get(capability)
    if record is None:
        return CapabilityStatus(
            capability,
            NOT_ACCEPTED,
            f"{package.name} has not been accepted; run cortex skills accept",
            package,
            digest,
        )
    accepted = (
        record.get("path") == str(package.path)
        and record.get("digest") == digest
        and record.get("entry") == package.entry
        and record.get("interpreter") == package.interpreter
    )
    if not accepted:
        return CapabilityStatus(
            capability,
            CHANGED,
            f"{package.name} changed since it was accepted; review it, then run "
            "cortex skills accept",
            package,
            digest,
            record.get("accepted_at"),
        )
    return CapabilityStatus(
        capability,
        READY,
        f"{package.name} matches its accepted digest",
        package,
        digest,
        record.get("accepted_at"),
    )


def resolve_all(config: Mapping[str, Any], paths: Any) -> dict[str, CapabilityStatus]:
    return {name: resolve_capability(config, paths, name) for name in KNOWN_CAPABILITIES}


def accept(
    config: Mapping[str, Any],
    paths: Any,
    capability: str,
    *,
    now: datetime | None = None,
) -> CapabilityStatus:
    """Record the package now serving `capability` as the accepted one."""

    if capability not in KNOWN_CAPABILITIES:
        raise SkillAcceptanceError(f"unknown capability: {capability}")
    status = resolve_capability(config, paths, capability)
    if status.state not in _ACCEPTABLE or status.package is None or status.digest is None:
        raise SkillAcceptanceError(f"{capability} is {status.state}: {status.reason}")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = read_acceptances(paths)
    records[capability] = {
        "package": status.package.name,
        "path": str(status.package.path),
        "entry": status.package.entry,
        "interpreter": status.package.interpreter,
        "digest": status.digest,
        "accepted_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_acceptances(paths, records)
    return resolve_capability(config, paths, capability)


def unknown_capabilities(config: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """(package, capability) for every declaration no product consumer reads."""

    root = configured_root(config)
    if root is None:
        return ()
    try:
        packages = discover(root)
    except OSError:
        return ()
    return tuple(
        (package.name, package.capability)
        for package in packages
        if package.capability not in KNOWN_CAPABILITIES
    )
