"""Cortex asset inventory, SQLite snapshot, and restore verification tools.

The module is intentionally stdlib-only so recovery does not depend on the
research profile or its native extensions. Live SQLite databases are copied
with ``sqlite3.Connection.backup`` and failures are fatal; a raw file-copy
fallback would produce an unsafe snapshot for WAL databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote


MANIFEST_VERSION = 1
_SQLITE_HEADER = b"SQLite format 3\x00"
_BACKUP_EXCLUDED_DIRS = {
    ".pytest_cache",
    ".stversions",
    ".venv",
    "__pycache__",
    "backup-staging",
    "backups",
    "checkpoints",
    "node_modules",
}
_DATABASE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_RESTIC_EXCLUDES = (
    "**/.pytest_cache/**",
    "**/.venv/**",
    "**/__pycache__/**",
    "**/backups/**",
    "**/checkpoints/**",
    "**/node_modules/**",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.sqlite3-*",
)
_SENSITIVE_SAMPLE_NAMES = {
    ".env",
    "age-key.txt",
    "auth.json",
    "secrets.age",
}
_SAMPLE_EXCLUDED_DIRS = _BACKUP_EXCLUDED_DIRS | {
    ".claude",
    ".git",
    ".obsidian",
    "archive",
}


class BackupError(RuntimeError):
    """Raised when a backup cannot be proven safe."""


@dataclass(frozen=True)
class AssetRoot:
    name: str
    path: Path
    protection_class: str


@dataclass(frozen=True)
class DatabaseSpec:
    name: str
    path: Path
    required_tables: tuple[str, ...] = ()
    protection_class: str = "critical"
    # ``(table, first_schema_migrations_version)`` pairs for protected tables a
    # later migration introduces. A database that has not reached the version
    # yet legitimately lacks the table, so requiring it there would mark a
    # healthy older state invalid; the table stays protected from the version
    # that creates it onward.
    table_since_schema_version: tuple[tuple[str, int], ...] = ()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    encoded = quote(str(path.resolve()), safe="/")
    suffix = "?mode=ro"
    if immutable:
        suffix += "&immutable=1"
    return f"file:{encoded}{suffix}"


def _product_data_dir(home: Path) -> Path:
    configured = os.environ.get("CORTEX_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return home / "Library/Application Support/Cortex/Data"
    return Path(
        os.environ.get("XDG_DATA_HOME", home / ".local/share")
    ).expanduser() / "cortex"


def _configured_root(explicit: Path | None, variable: str) -> Path | None:
    """An optional asset root: the argument, else the variable, else absent.

    There is deliberately no fallback path. The three roots that use this --
    a source checkout and the two corpus directories -- are wherever the
    operator put them, and this module used to fall back to one machine's
    layout under ``$HOME``. A guess is worse than an absence here: an
    inventory that lists a path nobody has claims to have checked a directory
    it invented, and a restic pass over it silently backs up nothing.
    """

    if explicit is not None:
        return explicit.expanduser().resolve()
    value = os.environ.get(variable)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _hermes_home(home: Path) -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return home / ".hermes"


def default_asset_roots(
    *,
    home: Path | None = None,
    cortex_repo: Path | None = None,
    readings_root: Path | None = None,
    research_root: Path | None = None,
) -> list[AssetRoot]:
    """The product's own roots, plus whichever optional roots are configured.

    The first four are installation roles this product itself creates, so they
    are always listed: an absent one is a finding, which ``_path_metadata``
    records as ``exists: false``. ``hermes_home`` is among them because the
    managed worker's home is the product's to install and upgrade;
    ``HERMES_HOME`` names it when the operator moved it, exactly as the
    acceptance harness and the runtime read it.

    ``cortex_repo``, ``agent_readings`` and ``agent_research`` are not
    installation roles -- a source checkout and two corpus directories -- so
    they appear only when an argument or an environment variable says where
    they are.
    """

    home = (home or Path.home()).expanduser().resolve()
    roots = [
        AssetRoot("cortex_config", home / ".config/cortex", "critical"),
        AssetRoot("cortex_state", home / ".local/state/cortex", "critical"),
        AssetRoot("cortex_product_data", _product_data_dir(home), "critical"),
        AssetRoot("hermes_home", _hermes_home(home), "important"),
    ]
    optional = (
        ("cortex_repo", cortex_repo, "CORTEX_HOME", "important"),
        ("agent_readings", readings_root, "CORTEX_READINGS_ROOT", "critical"),
        ("agent_research", research_root, "CORTEX_RESEARCH_ROOT", "critical"),
    )
    for name, explicit, variable, protection in optional:
        path = _configured_root(explicit, variable)
        if path is not None:
            roots.append(AssetRoot(name, path, protection))
    return roots


def default_database_specs(*, home: Path | None = None) -> list[DatabaseSpec]:
    """The two databases this product owns: Control, and the research index.

    The inventory used to name seven more -- the investment cache, the eval
    store, three Hermes gateway/board databases and the shared user memory
    database. Every one of them belonged to a subsystem this product does not
    ship, so listing them made an inventory report describe an installation that
    does not exist and quietly promised a backup of files nothing creates.
    """

    from .product.control.schema import (
        CAPTURES_MIGRATION,
        RESEARCH_SCHEDULES_MIGRATION,
        RESEARCH_ITEMS_MIGRATION,
        RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,
        TRANSPORT_ACTIVATION_MIGRATION,
    )

    home = (home or Path.home()).expanduser().resolve()
    cortex_state = home / ".local/state/cortex"
    product_data = _product_data_dir(home)
    control_database = Path(
        os.environ.get("CORTEX_CONTROL_DB", product_data / "control.db")
    ).expanduser()
    return [
        DatabaseSpec(
            "cortex_control",
            control_database,
            (
                "schema_migrations",
                "workspaces",
                "threads",
                "runs",
                "run_events",
                "idempotency_receipts",
                "captures",
                # ⟦S3.4/D6⟧ The operator's release approvals are as protected as
                # the activation decisions they sit beside: losing them silently
                # would turn a revoked release back into an approved one at the
                # next restore.
                "runtime_release_approvals",
                "transport_activation_decisions",
                "research_schedules",
                "research_items",
                "research_document_versions",
                "research_thread_items",
            ),
            # Each table is required only from the version that creates it: an
            # older snapshot legitimately has none of them, and demanding one
            # would turn every earlier backup proof red.
            table_since_schema_version=(
                ("captures", CAPTURES_MIGRATION),
                (
                    "runtime_release_approvals",
                    RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,
                ),
                (
                    "transport_activation_decisions",
                    TRANSPORT_ACTIVATION_MIGRATION,
                ),
                ("research_schedules", RESEARCH_SCHEDULES_MIGRATION),
                ("research_items", RESEARCH_ITEMS_MIGRATION),
                ("research_document_versions", RESEARCH_ITEMS_MIGRATION),
                ("research_thread_items", RESEARCH_ITEMS_MIGRATION),
            ),
        ),
        DatabaseSpec(
            "research",
            Path(
                os.environ.get(
                    "CORTEX_RESEARCH_DB",
                    cortex_state / "research/research.db",
                )
            ).expanduser(),
            ("papers", "chunks", "idea_seeds"),
        ),
    ]


def _path_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
    }
    if not path.exists() and not path.is_symlink():
        return result

    info = path.lstat()
    result.update(
        {
            "mode": stat.filemode(info.st_mode),
            "mode_octal": oct(stat.S_IMODE(info.st_mode)),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns,
            "size_bytes": info.st_size,
            "kind": (
                "directory"
                if path.is_dir()
                else "file"
                if path.is_file()
                else "other"
            ),
        }
    )
    return result


def _walk_metrics(root: Path) -> dict[str, int]:
    if not root.exists():
        return {"file_count": 0, "directory_count": 0, "total_bytes": 0}
    if root.is_file():
        return {
            "file_count": 1,
            "directory_count": 0,
            "total_bytes": root.stat().st_size,
        }

    file_count = 0
    directory_count = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _BACKUP_EXCLUDED_DIRS
        )
        directory_count += len(dirnames)
        current = Path(dirpath)
        for name in filenames:
            candidate = current / name
            if candidate.is_symlink():
                continue
            try:
                total_bytes += candidate.stat().st_size
                file_count += 1
            except OSError:
                continue
    return {
        "file_count": file_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
    }


def _sqlite_header(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _schema_migrations_version(
    conn: sqlite3.Connection, tables: Sequence[str]
) -> int | None:
    """Return the highest applied migration, or None when it cannot be read."""

    if "schema_migrations" not in tables:
        return None
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _enforced_required_tables(
    required_tables: Sequence[str],
    table_since_schema_version: Sequence[tuple[str, int]],
    schema_version: int | None,
) -> list[str]:
    """Drop protected tables the database is too old to have yet."""

    gates = dict(table_since_schema_version)
    if not gates or schema_version is None:
        return list(required_tables)
    return [
        name
        for name in required_tables
        if name not in gates or schema_version >= gates[name]
    ]


def inspect_database(
    path: Path,
    *,
    required_tables: Sequence[str] = (),
    table_since_schema_version: Sequence[tuple[str, int]] = (),
    run_quick_check: bool = True,
    immutable: bool = False,
) -> dict[str, Any]:
    result = _path_metadata(path)
    result.update(
        {
            "sqlite_header": False,
            "schema_version": None,
            "required_tables": list(required_tables),
            "missing_required_tables": list(required_tables),
        }
    )
    if not path.exists() or not path.is_file():
        result["status"] = "missing"
        return result
    if path.stat().st_size == 0:
        result["status"] = "empty"
        return result

    result["sqlite_header"] = _sqlite_header(path)
    if not result["sqlite_header"]:
        result["status"] = "not_sqlite"
        return result

    try:
        conn = sqlite3.connect(_sqlite_uri(path, immutable=immutable), uri=True)
        try:
            schema_rows = conn.execute(
                "SELECT type, name, COALESCE(sql, '') "
                "FROM sqlite_schema ORDER BY type, name"
            ).fetchall()
            tables = sorted(
                row[1]
                for row in schema_rows
                if row[0] == "table" and not row[1].startswith("sqlite_")
            )
            schema_json = json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":"))
            result.update(
                {
                    "tables": tables,
                    "table_count": len(tables),
                    "schema_sha256": hashlib.sha256(
                        schema_json.encode("utf-8")
                    ).hexdigest(),
                    "user_version": int(
                        conn.execute("PRAGMA user_version").fetchone()[0]
                    ),
                    "journal_mode": str(
                        conn.execute("PRAGMA journal_mode").fetchone()[0]
                    ),
                }
            )
            schema_version = _schema_migrations_version(conn, tables)
            enforced = _enforced_required_tables(
                required_tables, table_since_schema_version, schema_version
            )
            result["schema_version"] = schema_version
            result["required_tables"] = enforced
            missing = sorted(set(enforced) - set(tables))
            result["missing_required_tables"] = missing
            if run_quick_check:
                check_rows = [
                    str(row[0]) for row in conn.execute("PRAGMA quick_check")
                ]
                result["quick_check"] = check_rows
                result["quick_check_ok"] = check_rows == ["ok"]
            result["status"] = (
                "ok"
                if not missing and result.get("quick_check_ok", True)
                else "invalid"
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["status"] = "open_error"
        result["error"] = str(exc)
    return result


def _discover_database_candidates(roots: Iterable[Path]) -> list[Path]:
    candidates: set[Path] = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames if name not in _BACKUP_EXCLUDED_DIRS
            )
            current = Path(dirpath)
            for name in filenames:
                if name.endswith(".db"):
                    candidates.add((current / name).resolve())
    return sorted(candidates)


def _run_command(
    args: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _git_info(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "is_repository": False}
    if not path.exists():
        return result
    probe = _run_command(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if probe.returncode != 0:
        return result
    result["is_repository"] = True
    result["toplevel"] = probe.stdout.strip()
    commands = {
        "head": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "branch", "--show-current"],
        "status_porcelain": ["git", "status", "--porcelain=v1"],
        "remotes": ["git", "remote", "-v"],
    }
    for key, command in commands.items():
        completed = _run_command(command, cwd=path)
        if completed.returncode == 0:
            result[key] = completed.stdout.rstrip()
        else:
            result[f"{key}_error"] = completed.stderr.strip()
    status_lines = result.get("status_porcelain", "").splitlines()
    result["dirty_entry_count"] = len(status_lines)
    return result


def _service_inventory() -> list[dict[str, Any]]:
    completed = _run_command(["launchctl", "list"])
    if completed.returncode != 0:
        return [{"status": "unavailable", "error": completed.stderr.strip()}]
    services: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, last_exit, label = parts
        if "cortex" not in label.lower() and "hermes" not in label.lower():
            continue
        services.append(
            {
                "label": label,
                "pid": None if pid == "-" else int(pid),
                "last_exit_status": None if last_exit == "-" else int(last_exit),
            }
        )
    return sorted(services, key=lambda item: item["label"])


def _sample_files(root: AssetRoot, *, limit: int = 3) -> list[dict[str, Any]]:
    if not root.path.exists() or not root.path.is_dir():
        return []
    preferred_names = {
        "CLAUDE.md",
        "PROJECT.md",
        "README.md",
        "full_text.md",
        "grounding.md",
        "notes.md",
    }
    candidates: list[Path] = []
    for candidate in sorted(root.path.iterdir()):
        if candidate.is_file() and candidate.name in preferred_names:
            candidates.append(candidate)
    for dirpath, dirnames, filenames in os.walk(root.path, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SAMPLE_EXCLUDED_DIRS
        )
        current = Path(dirpath)
        for name in sorted(filenames):
            if name in _SENSITIVE_SAMPLE_NAMES:
                continue
            candidate = current / name
            if candidate.is_symlink():
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if size == 0 or size > 10 * 1024 * 1024:
                continue
            if name in preferred_names:
                candidates.append(candidate)
        if len(candidates) >= limit * 4:
            break
    samples: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        samples.append(
            {
                "asset": root.name,
                "path": str(candidate),
                "relative_path": str(candidate.relative_to(root.path)),
                "size_bytes": candidate.stat().st_size,
                "sha256": _sha256(candidate),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _research_reference_inventory(
    database: Path,
    readings_root: Path,
    research_root: Path,
    *,
    limit: int = 3,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "database": str(database),
        "papers": [],
        "ideas": [],
        "explorations": [],
        "conversation_sessions": [],
        "file_samples": [],
    }
    if not database.is_file() or not _sqlite_header(database):
        result["status"] = "database_unavailable"
        return result

    try:
        conn = sqlite3.connect(_sqlite_uri(database), uri=True)
        conn.execute("PRAGMA query_only = ON")
        try:
            table_columns = {
                table_name: {
                    column[1]
                    for column in conn.execute(
                        "SELECT * FROM pragma_table_info(?)", (table_name,)
                    )
                }
                for table_name in (
                    "papers",
                    "idea_seeds",
                    "exploration_rounds",
                    "conversation_sessions",
                )
            }

            paper_columns = table_columns.get("papers", set())
            if {"paper_dir", "title", "indexed_at"} <= paper_columns:
                paper_rows = conn.execute(
                    "SELECT paper_dir, title FROM papers "
                    "ORDER BY indexed_at DESC LIMIT 2000"
                )
                for paper_dir, title in paper_rows:
                    paper_path = readings_root / "papers" / str(paper_dir)
                    if not paper_path.is_dir() or not _path_within(
                        paper_path, readings_root
                    ):
                        continue
                    files: list[dict[str, Any]] = []
                    for name in ("notes.md", "grounding.md", "full_text.md"):
                        path = paper_path / name
                        if not path.is_file():
                            continue
                        sample = {
                            "asset": "agent_readings",
                            "path": str(path),
                            "relative_path": str(path.relative_to(readings_root)),
                            "size_bytes": path.stat().st_size,
                            "sha256": _sha256(path),
                            "resource_type": "paper",
                            "resource_id": str(paper_dir),
                        }
                        files.append(sample)
                        result["file_samples"].append(sample)
                    result["papers"].append(
                        {
                            "paper_dir": str(paper_dir),
                            "title": str(title),
                            "path": str(paper_path),
                            "exists": True,
                            "files": [entry["path"] for entry in files],
                        }
                    )
                    if len(result["papers"]) >= limit:
                        break

            idea_columns = table_columns.get("idea_seeds", set())
            if {"idea_id", "status", "md_path", "updated_at"} <= idea_columns:
                idea_rows = conn.execute(
                    "SELECT idea_id, status, md_path FROM idea_seeds "
                    "ORDER BY updated_at DESC LIMIT 200"
                )
                for idea_id, status, md_path in idea_rows:
                    path = Path(str(md_path)).expanduser()
                    if not path.is_file() or not _path_within(path, research_root):
                        continue
                    sample = {
                        "asset": "agent_research",
                        "path": str(path),
                        "relative_path": str(
                            path.resolve().relative_to(research_root.resolve())
                        ),
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                        "resource_type": "idea",
                        "resource_id": str(idea_id),
                    }
                    result["file_samples"].append(sample)
                    result["ideas"].append(
                        {
                            "idea_id": str(idea_id),
                            "status": str(status),
                            "md_path": str(path),
                            "exists": True,
                        }
                    )
                    if len(result["ideas"]) >= limit:
                        break

            exploration_columns = table_columns.get("exploration_rounds", set())
            if {"exploration_id", "round_n"} <= exploration_columns:
                result["explorations"] = [
                    {
                        "exploration_id": str(row[0]),
                        "max_round": int(row[1]),
                        "round_count": int(row[2]),
                    }
                    for row in conn.execute(
                        "SELECT exploration_id, MAX(round_n), COUNT(*) "
                        "FROM exploration_rounds GROUP BY exploration_id "
                        "ORDER BY MAX(started_at) DESC LIMIT ?",
                        (limit,),
                    )
                ]

            session_columns = table_columns.get("conversation_sessions", set())
            if {
                "session_id",
                "platform",
                "n_turns_atomized",
                "n_atoms_extracted",
            } <= session_columns:
                result["conversation_sessions"] = [
                    {
                        "session_id": str(row[0]),
                        "platform": str(row[1]),
                        "n_turns_atomized": int(row[2]),
                        "n_atoms_extracted": int(row[3]),
                    }
                    for row in conn.execute(
                        "SELECT session_id, platform, n_turns_atomized, "
                        "n_atoms_extracted FROM conversation_sessions "
                        "ORDER BY started_at DESC LIMIT ?",
                        (limit,),
                    )
                ]
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        result["status"] = "query_error"
        result["error"] = str(exc)
        return result

    result["status"] = "ok"
    return result


def build_inventory(
    *,
    home: Path | None = None,
    cortex_repo: Path | None = None,
    readings_root: Path | None = None,
    research_root: Path | None = None,
    deep_database_check: bool = True,
) -> dict[str, Any]:
    home = (home or Path.home()).expanduser().resolve()
    assets = default_asset_roots(
        home=home,
        cortex_repo=cortex_repo,
        readings_root=readings_root,
        research_root=research_root,
    )
    asset_paths = {asset.name: asset.path for asset in assets}
    database_specs = default_database_specs(home=home)
    canonical_paths = {spec.path.expanduser().resolve() for spec in database_specs}

    asset_rows: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for asset in assets:
        row = asdict(asset)
        row["path"] = str(asset.path)
        row.update(_path_metadata(asset.path))
        row.update(_walk_metrics(asset.path))
        asset_rows.append(row)
        samples.extend(_sample_files(asset))

    database_rows: list[dict[str, Any]] = []
    for spec in database_specs:
        row = inspect_database(
            spec.path,
            required_tables=spec.required_tables,
            table_since_schema_version=spec.table_since_schema_version,
            run_quick_check=deep_database_check,
        )
        row.update(
            {
                "name": spec.name,
                "protection_class": spec.protection_class,
                "canonical": True,
            }
        )
        database_rows.append(row)

    candidate_rows: list[dict[str, Any]] = []
    candidate_roots = [
        home / ".local/state/cortex",
        _hermes_home(home) / "profiles",
    ]
    for path in _discover_database_candidates(candidate_roots):
        if path in canonical_paths:
            continue
        row = inspect_database(path, run_quick_check=False)
        row.update({"canonical": False, "name": "unclassified"})
        candidate_rows.append(row)

    git_roots = {
        asset.name: asset.path
        for asset in assets
        if asset.name in {"cortex_repo", "agent_readings", "agent_research"}
    }
    hermes_repo = _hermes_home(home) / "hermes-agent"
    git_roots["hermes_agent"] = hermes_repo

    research_database = next(
        spec.path for spec in database_specs if spec.name == "research"
    )
    # Both corpus roots are optional (`default_asset_roots`), and the reference
    # pass needs both: it reports each file BY its path relative to the root it
    # belongs to, and refuses one that escapes it. With neither configured
    # there is nothing to be relative to, so the pass says so rather than
    # resolving corpus paths against a directory it picked.
    if "agent_readings" in asset_paths and "agent_research" in asset_paths:
        resource_references = _research_reference_inventory(
            research_database,
            asset_paths["agent_readings"],
            asset_paths["agent_research"],
        )
    else:
        resource_references = {
            "database": str(research_database),
            "status": "corpus_roots_unconfigured",
            "papers": [],
            "ideas": [],
            "explorations": [],
            "conversation_sessions": [],
            "file_samples": [],
        }
    samples_by_key = {
        (sample["path"], sample["sha256"]): sample for sample in samples
    }
    for sample in resource_references.pop("file_samples", []):
        key = (sample["path"], sample["sha256"])
        if key in samples_by_key:
            samples_by_key[key].update(
                {
                    "resource_type": sample["resource_type"],
                    "resource_id": sample["resource_id"],
                }
            )
        else:
            samples.append(sample)
            samples_by_key[key] = sample

    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": _utc_now(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version,
        "home": str(home),
        "assets": asset_rows,
        "databases": database_rows,
        "database_candidates": candidate_rows,
        "git_repositories": {
            name: _git_info(path) for name, path in sorted(git_roots.items())
        },
        "services": _service_inventory(),
        "sample_files": samples,
        "resource_references": resource_references,
    }


def backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.exists() or not _sqlite_header(source):
        raise BackupError(f"Source is not a SQLite database: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise BackupError(f"Refusing to overwrite snapshot database: {destination}")

    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=30)
        destination_conn = sqlite3.connect(str(destination))
        source_conn.backup(destination_conn, pages=1024, sleep=0.01)
        destination_conn.commit()
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(f"SQLite backup failed for {source}: {exc}") from exc
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()

    destination.chmod(0o600)
    verification = inspect_database(destination, run_quick_check=True, immutable=True)
    if verification.get("status") != "ok":
        destination.unlink(missing_ok=True)
        raise BackupError(
            f"Snapshot verification failed for {source}: "
            f"{verification.get('quick_check') or verification.get('error')}"
        )
    verification["sha256"] = _sha256(destination)
    return verification


def _capture_git_state(
    repositories: dict[str, dict[str, Any]], destination: Path
) -> list[dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    for name, info in sorted(repositories.items()):
        if not info.get("is_repository"):
            continue
        repo = Path(info["toplevel"])
        metadata_path = destination / f"{name}.json"
        metadata_path.write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        metadata_path.chmod(0o600)
        entry: dict[str, Any] = {
            "name": name,
            "metadata": str(metadata_path),
            "metadata_sha256": _sha256(metadata_path),
        }
        for diff_name, command in (
            ("working", ["git", "diff", "--binary"]),
            ("staged", ["git", "diff", "--cached", "--binary"]),
        ):
            completed = _run_command(command, cwd=repo)
            if completed.returncode != 0:
                entry[f"{diff_name}_error"] = completed.stderr.strip()
                continue
            diff_path = destination / f"{name}.{diff_name}.patch"
            diff_path.write_text(completed.stdout, encoding="utf-8")
            diff_path.chmod(0o600)
            entry[f"{diff_name}_patch"] = str(diff_path)
            entry[f"{diff_name}_sha256"] = _sha256(diff_path)
        bundle_path = destination / f"{name}.bundle"
        completed = _run_command(
            ["git", "bundle", "create", str(bundle_path), "--all"], cwd=repo
        )
        if completed.returncode == 0:
            bundle_path.chmod(0o600)
            entry["bundle"] = str(bundle_path)
            entry["bundle_sha256"] = _sha256(bundle_path)
        else:
            bundle_path.unlink(missing_ok=True)
            entry["bundle_error"] = completed.stderr.strip()
        captured.append(entry)
    return captured


def _safe_relative_member(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise BackupError(f"Unsafe snapshot-relative path: {relative_path}")
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise BackupError(f"Snapshot-relative path escapes its root: {relative_path}") from exc
    return candidate


def _inspect_private_file(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"Private companion cannot be opened safely: {path}") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_nlink != 1
        ):
            raise BackupError(
                f"Private companion must be mode 0600, user-owned, and unlinked: {path}"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _copy_private_file(source: Path, destination: Path) -> dict[str, Any]:
    source_flags = os.O_RDONLY
    source_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, source_flags)
    except OSError as exc:
        raise BackupError(f"Private companion is missing or unsafe: {source}") from exc
    destination_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    destination_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        source_stat = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_uid != os.geteuid()
            or stat.S_IMODE(source_stat.st_mode) != 0o600
            or source_stat.st_nlink != 1
        ):
            raise BackupError(
                f"Private companion must be mode 0600, user-owned, and unlinked: {source}"
            )
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
        os.fsync(destination_descriptor)
    except BaseException:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
            destination_descriptor = -1
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_descriptor = os.open(destination.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def create_staged_snapshot(
    output_root: Path,
    *,
    home: Path | None = None,
    cortex_repo: Path | None = None,
    readings_root: Path | None = None,
    research_root: Path | None = None,
    snapshot_id: str | None = None,
) -> Path:
    output_root = output_root.expanduser().resolve()
    snapshot_id = snapshot_id or f"cortex-{_snapshot_stamp()}"
    snapshot_dir = output_root / snapshot_id
    if snapshot_dir.exists():
        raise BackupError(f"Snapshot already exists: {snapshot_dir}")
    snapshot_dir.mkdir(parents=True, mode=0o700)

    try:
        inventory = build_inventory(
            home=home,
            cortex_repo=cortex_repo,
            readings_root=readings_root,
            research_root=research_root,
            deep_database_check=True,
        )
        inventory_path = snapshot_dir / "inventory.json"
        inventory_path.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        inventory_path.chmod(0o600)

        database_dir = snapshot_dir / "databases"
        database_dir.mkdir(mode=0o700)
        database_snapshots: list[dict[str, Any]] = []
        specs_by_name = {
            spec.name: spec for spec in default_database_specs(home=home)
        }
        for database in inventory["databases"]:
            if database["status"] == "missing":
                continue
            if database["status"] != "ok":
                raise BackupError(
                    f"Canonical database is not healthy: {database['name']} "
                    f"({database['path']}, status={database['status']})"
                )
            spec = specs_by_name[database["name"]]
            logical_name = f"{spec.name}.sqlite3.snapshot"
            destination = database_dir / logical_name
            verification = backup_sqlite(spec.path, destination)
            database_snapshots.append(
                {
                    "name": spec.name,
                    "source_path": str(spec.path),
                    "snapshot_relative_path": str(
                        destination.relative_to(snapshot_dir)
                    ),
                    "protection_class": spec.protection_class,
                    "size_bytes": destination.stat().st_size,
                    "sha256": verification["sha256"],
                    "schema_sha256": verification["schema_sha256"],
                    "tables": verification["tables"],
                    "quick_check": verification["quick_check"],
                }
            )

        companion_files: list[dict[str, Any]] = []
        control_snapshot = next(
            (
                entry
                for entry in database_snapshots
                if entry["name"] == "cortex_control"
            ),
            None,
        )
        if control_snapshot is not None:
            control_spec = specs_by_name["cortex_control"]
            source = control_spec.path.with_name(
                f".{control_spec.path.name}.transport.key"
            )
            companion_dir = snapshot_dir / "companions"
            companion_dir.mkdir(mode=0o700)
            destination = companion_dir / "cortex-control-transport.key"
            copied = _copy_private_file(source, destination)
            companion_files.append(
                {
                    "name": "cortex_control_transport_key",
                    "source_path": str(source),
                    "snapshot_relative_path": str(
                        destination.relative_to(snapshot_dir)
                    ),
                    "restore_relative_path": (
                        "databases/.cortex_control.db.transport.key"
                    ),
                    "protection_class": "critical-secret",
                    "size_bytes": copied["size_bytes"],
                    "sha256": copied["sha256"],
                }
            )

        git_capture = _capture_git_state(
            inventory["git_repositories"], snapshot_dir / "git"
        )
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": _utc_now(),
            "hostname": inventory["hostname"],
            "mode": "online",
            "inventory_relative_path": "inventory.json",
            "inventory_sha256": _sha256(inventory_path),
            "database_snapshots": database_snapshots,
            "companion_files": companion_files,
            "sample_files": inventory["sample_files"],
            "git_capture": git_capture,
        }
        manifest_path = snapshot_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        verify_staged_snapshot(snapshot_dir)
        return snapshot_dir
    except Exception:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise


def verify_staged_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.expanduser().resolve()
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError(f"Snapshot manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise BackupError(
            f"Unsupported manifest version: {manifest.get('manifest_version')}"
        )

    errors: list[str] = []
    verified_databases: list[dict[str, Any]] = []
    inventory_path = snapshot_dir / manifest["inventory_relative_path"]
    if not inventory_path.is_file():
        errors.append(f"Missing inventory: {inventory_path}")
    elif _sha256(inventory_path) != manifest["inventory_sha256"]:
        errors.append("Inventory checksum mismatch")

    for entry in manifest.get("database_snapshots", []):
        path = snapshot_dir / entry["snapshot_relative_path"]
        if not path.is_file():
            errors.append(f"Missing database snapshot: {entry['name']}")
            continue
        checksum = _sha256(path)
        if checksum != entry["sha256"]:
            errors.append(f"Checksum mismatch: {entry['name']}")
            continue
        inspection = inspect_database(path, run_quick_check=True, immutable=True)
        if inspection.get("status") != "ok":
            errors.append(
                f"SQLite verification failed: {entry['name']} "
                f"({inspection.get('error') or inspection.get('quick_check')})"
            )
            continue
        if inspection.get("schema_sha256") != entry.get("schema_sha256"):
            errors.append(f"Schema fingerprint mismatch: {entry['name']}")
            continue
        verified_databases.append(
            {
                "name": entry["name"],
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": checksum,
                "quick_check": inspection["quick_check"],
            }
        )

    verified_companions: list[dict[str, Any]] = []
    companion_entries = manifest.get("companion_files", [])
    control_databases = [
        entry
        for entry in manifest.get("database_snapshots", [])
        if entry.get("name") == "cortex_control"
    ]
    control_keys = [
        entry
        for entry in companion_entries
        if entry.get("name") == "cortex_control_transport_key"
    ]
    if control_databases and len(control_keys) != 1:
        errors.append(
            "Cortex control database requires exactly one transport-key companion"
        )
    for entry in companion_entries:
        try:
            path = _safe_relative_member(
                snapshot_dir, entry["snapshot_relative_path"]
            )
            inspection = _inspect_private_file(path)
        except (BackupError, KeyError) as exc:
            errors.append(f"Invalid companion file {entry.get('name')}: {exc}")
            continue
        if inspection["sha256"] != entry.get("sha256"):
            errors.append(f"Checksum mismatch: {entry.get('name')}")
            continue
        if inspection["size_bytes"] != entry.get("size_bytes"):
            errors.append(f"Size mismatch: {entry.get('name')}")
            continue
        verified_companions.append(
            {
                "name": entry["name"],
                "path": str(path),
                **inspection,
            }
        )

    report = {
        "snapshot_id": manifest.get("snapshot_id"),
        "verified_at": _utc_now(),
        "ok": not errors,
        "errors": errors,
        "database_count": len(verified_databases),
        "databases": verified_databases,
        "companion_count": len(verified_companions),
        "companions": verified_companions,
    }
    if errors:
        raise BackupError("; ".join(errors))
    return report


def restore_staged_snapshot(
    snapshot_dir: Path, target_dir: Path
) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.expanduser().resolve()
    target_dir = target_dir.expanduser().resolve()
    verification = verify_staged_snapshot(snapshot_dir)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise BackupError(f"Restore target is not empty: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    restored_database_dir = target_dir / "databases"
    restored_database_dir.mkdir(mode=0o700)

    manifest = json.loads(
        (snapshot_dir / "manifest.json").read_text(encoding="utf-8")
    )
    restored: list[dict[str, Any]] = []
    for entry in manifest.get("database_snapshots", []):
        source = snapshot_dir / entry["snapshot_relative_path"]
        destination = restored_database_dir / f"{entry['name']}.db"
        shutil.copy2(source, destination)
        destination.chmod(0o600)
        inspection = inspect_database(
            destination, run_quick_check=True, immutable=True
        )
        checksum = _sha256(destination)
        if inspection.get("status") != "ok" or checksum != entry["sha256"]:
            raise BackupError(f"Restored database failed verification: {entry['name']}")
        restored.append(
            {
                "name": entry["name"],
                "path": str(destination),
                "sha256": checksum,
                "quick_check": inspection["quick_check"],
            }
        )

    restored_companions: list[dict[str, Any]] = []
    for entry in manifest.get("companion_files", []):
        source = _safe_relative_member(
            snapshot_dir, entry["snapshot_relative_path"]
        )
        destination = _safe_relative_member(
            target_dir, entry["restore_relative_path"]
        )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        copied = _copy_private_file(source, destination)
        if copied["sha256"] != entry["sha256"]:
            raise BackupError(
                f"Restored companion failed verification: {entry['name']}"
            )
        restored_companions.append(
            {
                "name": entry["name"],
                "path": str(destination),
                **copied,
            }
        )

    report = {
        "snapshot_id": verification["snapshot_id"],
        "restored_at": _utc_now(),
        "target": str(target_dir),
        "ok": True,
        "database_count": len(restored),
        "databases": restored,
        "companion_count": len(restored_companions),
        "companions": restored_companions,
    }
    report_path = target_dir / "restore-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.chmod(0o600)
    return report


def _restic_command(
    repository: Path, password_command: str, *args: str
) -> list[str]:
    executable = shutil.which("restic")
    if executable is None:
        raise BackupError("restic is not installed or is not on PATH")
    if not password_command.strip():
        raise BackupError("A non-empty restic password command is required")
    return [
        executable,
        "--repo",
        str(repository.expanduser().resolve()),
        "--password-command",
        password_command,
        *args,
    ]


def _run_restic(
    repository: Path, password_command: str, *args: str
) -> subprocess.CompletedProcess[str]:
    raw_timeout = os.environ.get("CORTEX_RESTIC_TIMEOUT_SECONDS", "3600")
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise BackupError(
            f"Invalid CORTEX_RESTIC_TIMEOUT_SECONDS: {raw_timeout}"
        ) from exc
    if timeout <= 0:
        raise BackupError("CORTEX_RESTIC_TIMEOUT_SECONDS must be positive")
    command = _restic_command(repository, password_command, *args)
    try:
        completed = _run_command(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"restic {' '.join(args[:2])} timed out after {timeout:g} seconds"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BackupError(f"restic {' '.join(args[:2])} failed: {detail}")
    return completed


def _restic_json_messages(output: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    return messages


def _archived_absolute_path(restore_root: Path, original: Path) -> Path:
    original = original.expanduser().resolve()
    if not original.is_absolute():  # pragma: no cover - resolve makes it absolute
        raise BackupError(f"Expected an absolute archived path: {original}")
    return restore_root.expanduser().resolve() / original.relative_to(original.anchor)


def create_restic_backup(
    repository: Path,
    password_command: str,
    staging_root: Path,
    receipt_root: Path,
    *,
    home: Path | None = None,
    cortex_repo: Path | None = None,
    readings_root: Path | None = None,
    research_root: Path | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Create and repository-check an encrypted restic backup.

    Raw SQLite files are excluded from the file pass. Their only authoritative
    copies are the verified logical snapshots created in ``staging_root``.
    """

    home = (home or Path.home()).expanduser().resolve()
    repository = repository.expanduser().resolve()
    staging_root = staging_root.expanduser().resolve()
    receipt_root = receipt_root.expanduser().resolve()
    repository.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    receipt_root.mkdir(parents=True, exist_ok=True)

    staged = create_staged_snapshot(
        staging_root,
        home=home,
        cortex_repo=cortex_repo,
        readings_root=readings_root,
        research_root=research_root,
        snapshot_id=snapshot_id,
    )
    stage_verification = verify_staged_snapshot(staged)
    inventory = json.loads((staged / "inventory.json").read_text(encoding="utf-8"))
    asset_entries = [
        (str(entry["name"]), Path(entry["path"]))
        for entry in inventory["assets"]
        if entry.get("exists")
    ]
    source_entries = [("logical_snapshot", staged), *asset_entries]
    sources = [path for _, path in source_entries]

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="cortex-restic-excludes-", delete=False
    ) as handle:
        exclude_path = Path(handle.name)
        handle.write("\n".join(_RESTIC_EXCLUDES) + "\n")
    restic_snapshots: list[dict[str, Any]] = []
    try:
        for source_name, source_path in source_entries:
            backup = _run_restic(
                repository,
                password_command,
                "backup",
                "--json",
                "--host",
                socket.gethostname(),
                "--tag",
                "cortex",
                "--tag",
                "online",
                "--tag",
                f"cortex-run:{stage_verification['snapshot_id']}",
                "--tag",
                f"cortex-source:{source_name}",
                "--exclude-file",
                str(exclude_path),
                str(source_path),
            )
            messages = _restic_json_messages(backup.stdout)
            summaries = [
                message
                for message in messages
                if message.get("message_type") == "summary"
            ]
            if not summaries or not summaries[-1].get("snapshot_id"):
                raise BackupError(
                    f"restic completed {source_name} without a verifiable snapshot ID"
                )
            restic_snapshots.append(
                {
                    "source_name": source_name,
                    "source_path": str(source_path),
                    "restic_snapshot_id": str(summaries[-1]["snapshot_id"]),
                    "restic_summary": summaries[-1],
                }
            )
    finally:
        exclude_path.unlink(missing_ok=True)

    check = _run_restic(repository, password_command, "check", "--json")
    check_messages = _restic_json_messages(check.stdout)
    restic_snapshot_ids = [
        entry["restic_snapshot_id"] for entry in restic_snapshots
    ]
    receipt = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": _utc_now(),
        "hostname": socket.gethostname(),
        "repository": str(repository),
        "cortex_snapshot_id": stage_verification["snapshot_id"],
        "restic_snapshot_id": restic_snapshot_ids[0],
        "restic_snapshot_ids": restic_snapshot_ids,
        "restic_snapshots": restic_snapshots,
        "staged_snapshot_path": str(staged),
        "sources": [str(path) for path in sources],
        "excluded_patterns": list(_RESTIC_EXCLUDES),
        "logical_database_count": stage_verification["database_count"],
        "sample_files": inventory.get("sample_files", []),
        "restic_summary": restic_snapshots[0]["restic_summary"],
        "repository_check": {
            "ok": True,
            "messages": check_messages,
            "stderr": check.stderr.strip(),
        },
    }
    receipt_path = receipt_root / f"{stage_verification['snapshot_id']}.json"
    if receipt_path.exists():
        raise BackupError(f"Refusing to overwrite backup receipt: {receipt_path}")
    try:
        shutil.rmtree(staged)
        receipt["local_staging_cleanup"] = {"ok": True}
    except OSError as exc:
        receipt["local_staging_cleanup"] = {
            "ok": False,
            "error": str(exc),
        }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def _receipt_snapshot_ids(receipt: dict[str, Any]) -> list[str]:
    values = receipt.get("restic_snapshot_ids")
    if values is None:
        values = [receipt.get("restic_snapshot_id")]
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and value for value in values)
    ):
        raise BackupError("Receipt does not contain valid restic snapshot IDs")
    return values


def restore_restic_backup(
    receipt_path: Path,
    password_command: str,
    target_dir: Path,
) -> dict[str, Any]:
    """Restore a restic snapshot and verify databases plus sampled files."""

    receipt_path = receipt_path.expanduser().resolve()
    target_dir = target_dir.expanduser().resolve()
    if target_dir.exists() and any(target_dir.iterdir()):
        raise BackupError(f"Restore target is not empty: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    repository = Path(receipt["repository"])
    restic_snapshot_ids = _receipt_snapshot_ids(receipt)

    for restic_snapshot_id in restic_snapshot_ids:
        _run_restic(
            repository,
            password_command,
            "restore",
            restic_snapshot_id,
            "--target",
            str(target_dir),
        )
    restored_stage = _archived_absolute_path(
        target_dir, Path(receipt["staged_snapshot_path"])
    )
    database_restore = restore_staged_snapshot(
        restored_stage, target_dir / "verified-databases"
    )

    sample_errors: list[str] = []
    verified_samples: list[dict[str, Any]] = []
    for sample in receipt.get("sample_files", []):
        restored_path = _archived_absolute_path(target_dir, Path(sample["path"]))
        if not restored_path.is_file():
            sample_errors.append(f"Missing sample file: {sample['path']}")
            continue
        checksum = _sha256(restored_path)
        if checksum != sample["sha256"]:
            sample_errors.append(f"Sample checksum mismatch: {sample['path']}")
            continue
        verified_samples.append(
            {
                "asset": sample["asset"],
                "original_path": sample["path"],
                "restored_path": str(restored_path),
                "sha256": checksum,
            }
        )
    if sample_errors:
        raise BackupError("; ".join(sample_errors))

    report = {
        "restored_at": _utc_now(),
        "ok": True,
        "repository": str(repository),
        "restic_snapshot_id": restic_snapshot_ids[0],
        "restic_snapshot_ids": restic_snapshot_ids,
        "cortex_snapshot_id": receipt["cortex_snapshot_id"],
        "target": str(target_dir),
        "database_restore": database_restore,
        "verified_sample_count": len(verified_samples),
        "verified_samples": verified_samples,
    }
    report_path = target_dir / "restic-restore-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.chmod(0o600)
    report["report_path"] = str(report_path)
    return report


def _write_json(value: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    output.chmod(0o600)
    print(output)


def _add_optional_root_arguments(parser: argparse.ArgumentParser) -> None:
    """The three roots that have no default; see ``default_asset_roots``."""

    parser.add_argument(
        "--cortex-repo",
        type=Path,
        help="Source checkout to inventory (default: $CORTEX_HOME, else absent)",
    )
    parser.add_argument(
        "--readings-root",
        type=Path,
        help=(
            "Readings corpus directory "
            "(default: $CORTEX_READINGS_ROOT, else absent)"
        ),
    )
    parser.add_argument(
        "--research-root",
        type=Path,
        help=(
            "Research corpus directory "
            "(default: $CORTEX_RESEARCH_ROOT, else absent)"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Inventory Cortex assets")
    inventory.add_argument("--home", type=Path)
    _add_optional_root_arguments(inventory)
    inventory.add_argument("--output", type=Path)
    inventory.add_argument(
        "--shallow-databases",
        action="store_true",
        help="Skip PRAGMA quick_check for canonical databases",
    )

    snapshot = subparsers.add_parser(
        "snapshot", help="Create verified logical SQLite snapshots"
    )
    snapshot.add_argument("--home", type=Path)
    _add_optional_root_arguments(snapshot)
    snapshot.add_argument("--output-root", type=Path, required=True)
    snapshot.add_argument("--snapshot-id")

    verify = subparsers.add_parser("verify", help="Verify a staged snapshot")
    verify.add_argument("snapshot_dir", type=Path)

    restore = subparsers.add_parser(
        "restore-drill", help="Restore logical snapshots into an isolated target"
    )
    restore.add_argument("snapshot_dir", type=Path)
    restore.add_argument("--target", type=Path, required=True)

    restic_backup = subparsers.add_parser(
        "restic-backup", help="Create an encrypted restic backup"
    )
    restic_backup.add_argument("--repository", type=Path, required=True)
    restic_backup.add_argument("--password-command", required=True)
    restic_backup.add_argument("--staging-root", type=Path, required=True)
    restic_backup.add_argument("--receipt-root", type=Path, required=True)
    restic_backup.add_argument("--home", type=Path)
    _add_optional_root_arguments(restic_backup)
    restic_backup.add_argument("--snapshot-id")

    restic_restore = subparsers.add_parser(
        "restic-restore-drill",
        help="Restore a restic backup into an isolated target and verify it",
    )
    restic_restore.add_argument("receipt", type=Path)
    restic_restore.add_argument("--password-command", required=True)
    restic_restore.add_argument("--target", type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inventory":
            inventory = build_inventory(
                home=args.home,
                cortex_repo=args.cortex_repo,
                readings_root=args.readings_root,
                research_root=args.research_root,
                deep_database_check=not args.shallow_databases,
            )
            _write_json(inventory, args.output)
        elif args.command == "snapshot":
            path = create_staged_snapshot(
                args.output_root,
                home=args.home,
                cortex_repo=args.cortex_repo,
                readings_root=args.readings_root,
                research_root=args.research_root,
                snapshot_id=args.snapshot_id,
            )
            _write_json(verify_staged_snapshot(path), None)
        elif args.command == "verify":
            _write_json(verify_staged_snapshot(args.snapshot_dir), None)
        elif args.command == "restore-drill":
            _write_json(
                restore_staged_snapshot(args.snapshot_dir, args.target), None
            )
        elif args.command == "restic-backup":
            _write_json(
                create_restic_backup(
                    args.repository,
                    args.password_command,
                    args.staging_root,
                    args.receipt_root,
                    home=args.home,
                    cortex_repo=args.cortex_repo,
                    readings_root=args.readings_root,
                    research_root=args.research_root,
                    snapshot_id=args.snapshot_id,
                ),
                None,
            )
        elif args.command == "restic-restore-drill":
            _write_json(
                restore_restic_backup(
                    args.receipt, args.password_command, args.target
                ),
                None,
            )
        else:  # pragma: no cover - argparse prevents this branch
            raise BackupError(f"Unknown command: {args.command}")
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
