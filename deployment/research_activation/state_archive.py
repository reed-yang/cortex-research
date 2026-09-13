#!/usr/bin/env python3
"""Capture or verify a stopped product archive; emit recovery commands only.

The before/after schema pair is an input, not a literal, so the same tool covers
16->17, 17->18 and 18->19. A capture that names a `--target-schema` is the BASELINE of
that pair and is what gets a `restore-paths.sh`; one that does not is a
preservation archive. Omitting it on `--schema 16` keeps the original 16->17
invocation working unchanged.

The staging suffixes are a cross-version on-disk contract: `restore-paths.sh`
bakes them in at generation time but calls back into a `state_archive.py` that
may since have been upgraded. They are therefore recorded in the manifest and
read back from it, never recomputed from a global literal.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tomllib
import unicodedata


ARCHIVE_SCHEMA = "cortex-offline-recovery-archive/2"
LEGACY_ARCHIVE_SCHEMA = "cortex-offline-recovery-archive/1"
#: The pair every `/1` archive was written for; it carried no target of its own.
LEGACY_PAIR = (16, 17)
SUPPORTED_SCHEMAS = (16, 17, 18, 19)


def staging_suffixes(source_schema, target_schema):
    return {"stage": f".schema{source_schema}-stage", "retained": f".schema{target_schema}-retained"}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_tree_paths(entries):
    """Compare equivalent filenames across APFS and HFS+ without merging files."""
    result = {}
    for path, value in entries.items():
        canonical = unicodedata.normalize("NFC", path)
        if canonical in result:
            raise ValueError("archive paths collide under Unicode normalization")
        result[canonical] = value
    return result


def tree(path, excluded=()):
    if not path.exists() or path.is_symlink():
        raise ValueError("archive/source root is missing or a symlink")
    if path.is_file():
        return {"": {"sha256": sha(path), "size": path.stat().st_size}}
    def walk_error(error):
        raise error

    result = {}
    for directory, directories, files in os.walk(path, followlinks=False, onerror=walk_error):
        parent = Path(directory)
        # Prune excluded generations/backups before descent, rather than walking
        # their tens of thousands of entries and filtering them afterward.
        directories[:] = sorted(name for name in directories if parent / name not in excluded)
        for name in sorted(directories + files):
            item = parent / name
            if item in excluded:
                continue
            if item.is_symlink():
                value = {"link": os.readlink(item)}
            elif item.is_dir():
                value = {"directory": True}
            elif item.is_file():
                value = {"sha256": sha(item), "size": item.stat().st_size}
            else:
                continue
            result[str(item.relative_to(path))] = value
    return normalized_tree_paths(result)


def validate_layout(app, prefix):
    if prefix == app or app.is_relative_to(prefix) or (prefix.is_relative_to(app) and prefix != app / "Distribution"):
        raise ValueError("prefix must be APP/Distribution or disjoint from APP")
    # A valid config can redirect state independently of environment overrides.
    config = tomllib.loads((app / "config.toml").read_text())
    defaults = {"config_dir": app, "data_dir": app / "Data", "state_dir": app / "State"}
    for name, value in config.get("paths", {}).items():
        if name not in defaults:
            continue
        text = str(value)
        if text == "~" or text.startswith("~/"):
            configured = app.parents[2] / text.removeprefix("~/") if text != "~" else app.parents[2]
        else:
            configured = Path(text)
            if not configured.is_absolute():
                configured = app / configured
        if configured.resolve() != defaults[name].resolve():
            raise ValueError("config path override is outside archive ownership: " + name)


def inspect(app, prefix):
    validate_layout(app, prefix)
    db = app / "Data/control.db"
    companion = app / "Data/.control.db.transport.key"
    for path in (db, companion, app / "config.toml"):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"required regular state file is missing: {path.name}")
    for path in (app / "State/runtime-update/lifecycle.json", app / "State/cortexd.json"):
        if path.exists():
            raise ValueError("official stop must remove daemon/lifecycle metadata first")
    wal = db.with_name("control.db-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Control WAL is nonempty; do not discard it")
    # This is an independent no-open-descriptor check, not a process killer.
    opened = subprocess.run(["/usr/sbin/lsof", "-t", str(db)], capture_output=True, text=True)
    if opened.returncode != 1 or opened.stdout.strip():
        raise ValueError("Control DB descriptor quiescence is unproven")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("database integrity check failed")
        schema = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        roots = [list(row) for row in connection.execute("SELECT root_id, private_path, revision FROM asset_roots WHERE enabled=1 ORDER BY root_id")]
        active = connection.execute("SELECT count(*) FROM runs WHERE state NOT IN ('completed','failed','canceled')").fetchone()[0]
        pending_pins = connection.execute("SELECT count(*) FROM runtime_pin_releases WHERE state != 'acked'").fetchone()[0]
        for table in ("runtime_activation_decisions", "transport_activation_decisions"):
            decision = connection.execute(f"SELECT decision FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            if not decision or decision[0] != "disable":
                raise ValueError("both gates must be explicitly disabled before archiving")
    return {"schema": schema, "active_runs": active, "pending_pin_releases": pending_pins, "roots": roots, "current": json.loads((prefix / "current.json").read_text()), "last_known_good": json.loads((prefix / "last-known-good.json").read_text())}


def verify(archive):
    manifest = json.loads((archive / "archive.json").read_text())
    if manifest["schema"] not in (ARCHIVE_SCHEMA, LEGACY_ARCHIVE_SCHEMA):
        raise ValueError("unknown archive format")
    if manifest["schema"] == LEGACY_ARCHIVE_SCHEMA:
        # A `/1` archive predates the declared pair. It was only ever written for
        # 16->17, so it is read as that pair rather than refused.
        source, target = LEGACY_PAIR
        manifest["target_schema"] = target if manifest["control"]["schema"] == source else None
        manifest["staging"] = staging_suffixes(source, target)
    for entry in manifest["copies"]:
        entry["files"] = normalized_tree_paths(entry["files"])
        if tree(archive / entry["copy"]) != entry["files"]:
            raise ValueError("archive content hash mismatch")
    return manifest


def restoration_entries(manifest):
    app, prefix = Path(manifest["app"]), Path(manifest["prefix"])
    result = []
    for entry in manifest["copies"]:
        source = Path(entry["source"])
        if source == app:
            children = sorted({Path(name).parts[0] for name in entry["files"]})
            for child in children:
                if app / child == prefix:
                    raise ValueError("archive unexpectedly includes distribution prefix")
                result.append((str(Path(entry["copy"]) / child), app / child))
        else:
            if source == prefix or source.is_relative_to(prefix) or prefix.is_relative_to(source):
                raise ValueError("archive root overlaps distribution prefix")
            result.append((entry["copy"], source))
    return result


def restore_preflight(pre, post, staged=False):
    before, after = verify(pre), verify(post)
    return _restore_preflight(before, after, staged)


def manifest_tree(manifest, relative):
    """Select one verified subtree without reading its archive bytes again."""
    relative = unicodedata.normalize("NFC", relative)
    for entry in manifest["copies"]:
        if relative == entry["copy"]:
            return entry["files"]
        if relative.startswith(entry["copy"] + "/"):
            child = relative[len(entry["copy"]) + 1:]
            record = entry["files"][child]
            if not record.get("directory"):
                return {"": record}
            prefix = child + "/"
            return {name[len(prefix):]: value for name, value in entry["files"].items() if name.startswith(prefix)}
    raise ValueError("recovery subtree is absent from verified manifest")


def _restore_preflight(before, after, staged=False):
    if before["app"] != after["app"] or before["prefix"] != after["prefix"]:
        raise ValueError("recovery archive ownership mismatch")
    declared = before.get("target_schema")
    if declared is None:
        raise ValueError("baseline archive declares no recovery target")
    if after["control"]["schema"] != declared:
        raise ValueError(f"recovery requires a schema{before['control']['schema']} baseline and schema{declared} preservation")
    if before["control"]["current"] != after["control"]["last_known_good"]:
        raise ValueError("baseline generation does not match current rollback target")
    app, prefix = Path(after["app"]), Path(after["prefix"])
    if inspect(app, prefix) != after["control"]:
        raise ValueError("current stopped state differs from preservation archive")
    entries = restoration_entries(before)
    suffixes = before["staging"]
    excluded = tuple(Path(value) for value in after["excluded"])
    if staged:
        excluded += tuple(Path(str(target) + suffixes["stage"]) for _, target in entries)
    for entry in after["copies"]:
        if tree(Path(entry["source"]), excluded) != entry["files"]:
            raise ValueError("current state changed since preservation archive")
    for relative, target in entries:
        stage = Path(str(target) + suffixes["stage"])
        displaced = Path(str(target) + suffixes["retained"])
        if (not staged and (stage.exists() or stage.is_symlink())) or displaced.exists() or displaced.is_symlink():
            raise ValueError("recovery staging or retained path already exists")
        if target.is_symlink() or not target.exists():
            raise ValueError("recovery target is missing or unsafe")
        if staged and tree(stage) != manifest_tree(before, relative):
            raise ValueError("staged recovery content mismatch")
    return before, after


def restore(pre, post, app, prefix, apply=False):
    """Verify archives once; recheck live and staged state before any rename.

    Manifests are retained only in this invocation, never trusted from a cached
    receipt. Archive changes during copying are caught against the verified
    inventory when staging is read back. Keep all external writers stopped.
    """
    before, after = verify(pre), verify(post)
    if before["app"] != str(app.absolute()) or before["prefix"] != str(prefix.absolute()):
        raise ValueError("archive app/prefix ownership mismatch")
    _restore_preflight(before, after)
    if not apply:
        return
    entries = restoration_entries(before)
    suffixes = before["staging"]
    for relative, target in entries:
        subprocess.run(["ditto", str(pre / relative), str(target) + suffixes["stage"]], check=True)
    _restore_preflight(before, after, staged=True)
    for _, target in entries:
        target.rename(Path(str(target) + suffixes["retained"]))
        Path(str(target) + suffixes["stage"]).rename(target)
    for filename, field in (("current.json", "current"), ("last-known-good.json", "last_known_good")):
        if json.loads((prefix / filename).read_text()) != after["control"][field]:
            raise ValueError("distribution pointer changed during state restoration")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--app", type=Path, required=True)
    capture.add_argument("--prefix", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--schema", type=int, choices=SUPPORTED_SCHEMAS, required=True)
    capture.add_argument("--target-schema", type=int, choices=SUPPORTED_SCHEMAS,
                         help="schema this baseline will be upgraded to; makes this the baseline of the pair")
    capture.add_argument("--preserve-unsettled", action="store_true")
    capture.add_argument("--allow-external-root", type=Path, action="append", default=[])
    capture.add_argument("--apply", action="store_true")
    check = sub.add_parser("verify")
    check.add_argument("archive", type=Path)
    check.add_argument("--schema", type=int, choices=SUPPORTED_SCHEMAS)
    check.add_argument("--app", type=Path)
    check.add_argument("--prefix", type=Path)
    recovery = sub.add_parser("restore-preflight")
    recovery.add_argument("pre", type=Path)
    recovery.add_argument("post", type=Path)
    recovery.add_argument("--staged", action="store_true")
    restore_command = sub.add_parser("restore")
    restore_command.add_argument("pre", type=Path)
    restore_command.add_argument("post", type=Path)
    restore_command.add_argument("--app", type=Path, required=True)
    restore_command.add_argument("--prefix", type=Path, required=True)
    restore_command.add_argument("--apply", action="store_true")
    staged = sub.add_parser("verify-staged")
    staged.add_argument("pre", type=Path)
    pointers = sub.add_parser("verify-pointers")
    pointers.add_argument("post", type=Path)
    layout = sub.add_parser("validate-layout")
    layout.add_argument("--app", type=Path, required=True)
    layout.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "restore":
        restore(args.pre, args.post, args.app, args.prefix, args.apply)
        print(json.dumps({"restore_ready": True, "applied": args.apply}))
        return
    if args.command == "validate-layout":
        validate_layout(args.app.absolute(), args.prefix.absolute())
        print(json.dumps({"layout_valid": True}))
        return
    if args.command == "restore-preflight":
        restore_preflight(args.pre, args.post, args.staged)
        print(json.dumps({"restore_preflight": True}))
        return
    if args.command == "verify-staged":
        manifest = verify(args.pre)
        stage_suffix = manifest["staging"]["stage"]
        for relative, target in restoration_entries(manifest):
            if manifest_tree(manifest, relative) != tree(Path(str(target) + stage_suffix)):
                raise ValueError("staged recovery content mismatch")
        print(json.dumps({"staged_verified": True}))
        return
    if args.command == "verify-pointers":
        manifest = verify(args.post)
        prefix = Path(manifest["prefix"])
        for filename, field in (("current.json", "current"), ("last-known-good.json", "last_known_good")):
            if json.loads((prefix / filename).read_text()) != manifest["control"][field]:
                raise ValueError("distribution pointer changed during state restoration")
        print(json.dumps({"distribution_pointers_preserved": True}))
        return
    if args.command == "verify":
        value = verify(args.archive.resolve(strict=True))
        if args.schema is not None and value["control"]["schema"] != args.schema:
            parser.error("archive schema mismatch")
        if args.app is not None and value["app"] != str(args.app.absolute()):
            parser.error("archive app ownership mismatch")
        if args.prefix is not None and value["prefix"] != str(args.prefix.absolute()):
            parser.error("archive prefix ownership mismatch")
        print(json.dumps({"verified": True, "control_schema": value["control"]["schema"], "copies": len(value["copies"])}))
        return
    app, prefix, output = args.app.absolute(), args.prefix.absolute(), args.output.absolute()
    if app.is_symlink() or prefix.is_symlink() or output.exists():
        parser.error("app/prefix must not be symlinks and output must be absent")
    facts = inspect(app, prefix)
    if facts["schema"] != args.schema:
        parser.error("current database differs from the expected schema")
    # A declared target makes this the baseline of a pair, which is what earns a
    # restore script. The legacy default reproduces the original 16->17 call.
    target_schema = args.target_schema if args.target_schema is not None else (LEGACY_PAIR[1] if args.schema == LEGACY_PAIR[0] else None)
    if target_schema is not None and target_schema <= args.schema:
        parser.error("--target-schema must be above --schema")
    if (facts["active_runs"] or facts["pending_pin_releases"]) and not args.preserve_unsettled:
        parser.error("settle active runs/pins first; only post-failure preservation may use --preserve-unsettled")
    roots = [("app", app)]
    external = []
    for root_id, raw, revision in facts["roots"]:
        root = Path(raw)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            parser.error(f"enabled root {root_id} is missing or unsafe")
        if root == prefix or root.is_relative_to(prefix) or prefix.is_relative_to(root):
            parser.error("an enabled asset root overlaps distribution ownership")
        if root == app or root.is_relative_to(app):
            continue
        if app.is_relative_to(root):
            parser.error("an enabled root contains the whole app; ownership needs review")
        if root not in args.allow_external_root:
            parser.error(f"external root {root_id} needs explicit --allow-external-root after single-writer review")
        external.append((root_id, root, revision))
    for root_id, root, _ in external:
        if any(root != other and root.is_relative_to(other) for _, other, _ in external):
            continue
        if root not in [v for _, v in roots]:
            roots.append((f"external-{len(roots)}", root))
    for _, root in roots:
        if output == root or output.is_relative_to(root) or root.is_relative_to(output):
            parser.error("archive and source roots must not overlap")
    print(json.dumps({"ready": True, "control_schema": facts["schema"], "target_schema": target_schema, "role": "baseline" if target_schema is not None else "preservation", "copy_count": len(roots), "apply": args.apply, "unsettled": bool(facts["active_runs"] or facts["pending_pin_releases"])}))
    if not args.apply:
        return
    output.mkdir(mode=0o700, parents=True)
    copies = []
    for name, root in roots:
        destination = output / name
        def ignored(directory, names):
            return [name for name in names if Path(directory) / name in (app / "State/backups", prefix)]
        shutil.copytree(root, destination, symlinks=True, ignore=ignored)
        excluded = (app / "State/backups", prefix) if root == app else ()
        copied_files = tree(destination)
        if tree(root, excluded) != copied_files:
            raise ValueError("source changed during archive copy")
        copies.append({"source": str(root), "copy": name, "files": copied_files})
    if inspect(app, prefix) != facts:
        raise ValueError("state changed during archive capture")
    manifest = {"schema": ARCHIVE_SCHEMA, "app": str(app), "prefix": str(prefix), "control": facts, "target_schema": target_schema, "staging": staging_suffixes(args.schema, target_schema) if target_schema is not None else None, "excluded": [str(app / "State/backups"), str(prefix)], "copies": copies}
    (output / "archive.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(output)
    # No restore is auto-executed: these commands preserve the current trees by
    # rename, and the coordinator must exclude all external writers first.
    # A preservation archive is not the baseline of a pair, so it gets no restore
    # script. This is decided by the declared target, never by a schema literal.
    if target_schema is None:
        return
    suffixes = staging_suffixes(args.schema, target_schema)
    ownership = f"--app {shlex.quote(str(app))} --prefix {shlex.quote(str(prefix))}"
    lines = [
        "#!/bin/bash", "set -euo pipefail", "umask 077",
        f"# Restore schema{args.schema} from a schema{target_schema} preservation archive.",
        f"# Staging: {suffixes['stage']}; retained: {suffixes['retained']}.",
        "# Keep launchd disabled throughout; no cortex/product Python call until rollback.",
        f': "${{POST_ARCHIVE:?path to verified schema-{target_schema} preservation archive is required}}"',
        ': "${PREP_PYTHON:?absolute Python 3.11+ interpreter is required}"',
        ': "${ARCHIVE_TOOL:?path to state_archive.py with the restore command is required}"',
        f'"$PREP_PYTHON" -B "$ARCHIVE_TOOL" restore {shlex.quote(str(output))} "$POST_ARCHIVE" {ownership} --apply',
    ]
    rollback_to = facts["current"].get("release_id", "the baseline generation")
    lines.append(f"# NEXT: official candidate cortex-dist rollback, then {rollback_to} start. Never start the schema{target_schema} candidate before rollback.")
    (output / "restore-paths.sh").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
