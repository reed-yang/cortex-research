#!/usr/bin/env python3
"""Exercise schema migration and backup recovery on an explicit private DB copy.

The source and target schema are inputs, not literals: the same probe rehearses
16->17, 17->18 and 16->18. What it proves is unchanged -- the migration set is
atomic, initialization is idempotent, existing rows survive, a previous reader
refuses the migrated store and reopens the restored baseline, and the source
copy is untouched.

Preservation is compared column-wise rather than by hashing `SELECT *`. An
additive migration such as 18 (`ALTER TABLE threads ADD COLUMN archived_at`)
changes every row tuple of the table it touches, so a whole-row fingerprint
reports a preserved table as mutated. Comparing the projection over the
baseline's own columns separates "the old data changed" from "a new column
appeared", and the new column is then checked to be uniformly unset on rows that
predate it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path


PROBE_SCHEMA = "cortex-research-migration-probe/2"


def _read_only(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def table_columns(connection, table):
    quoted = '"' + table.replace('"', '""') + '"'
    return [row[1] for row in connection.execute("PRAGMA table_info(" + quoted + ")")]


def inventory(path, columns_by_table=None):
    """Fingerprint every table, optionally projected onto known columns.

    `columns_by_table` pins the comparison to the baseline's shape. Without it
    the observed shape is recorded, which is what the baseline pass does.
    """

    with closing(_read_only(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        result = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            present = table_columns(connection, table)
            selected = present if columns_by_table is None else columns_by_table.get(table, present)
            missing = [column for column in selected if column not in present]
            assert not missing, (table, missing)
            projection = ", ".join('"' + column.replace('"', '""') + '"' for column in selected)
            rows = sorted(
                repr(tuple(row))
                for row in connection.execute("SELECT " + projection + " FROM " + quoted)
            )
            result[table] = {
                "rows": len(rows),
                "columns": list(selected),
                "added_columns": [column for column in present if column not in selected],
                "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
            }
        return result


def columns_of(inventoried):
    return {table: facts["columns"] for table, facts in inventoried.items()}


def thread_order(path):
    """The default listing order, which an added index must not disturb."""

    with closing(_read_only(path)) as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "threads" not in names:
            return None
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT workspace_id, id FROM threads ORDER BY workspace_id, created_at, id"
            )
        ]


def applied_versions(path):
    with closing(_read_only(path)) as connection:
        return sorted(
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        )


def copy_store(source, destination):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copy2(source, destination)
    shutil.copy2(source.with_name("." + source.name + ".transport.key"),
                 destination.with_name("." + destination.name + ".transport.key"))
    destination.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-source", type=Path, required=True)
    parser.add_argument("--candidate-source", type=Path, default=None,
                        help="checkout whose Control schema is the candidate; "
                             "default is the checkout containing this script")
    parser.add_argument("--from-schema", type=int, default=None,
                        help="expected baseline schema; default is the baseline's own head")
    parser.add_argument("--to-schema", type=int, default=None,
                        help="expected candidate schema; default is this source's SCHEMA_VERSION")
    args = parser.parse_args()
    source = args.baseline.resolve(strict=True)
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    baseline = inventory(source)
    baseline_columns = columns_of(baseline)
    baseline_order = thread_order(source)
    baseline_versions = applied_versions(source)
    # Which tree is the candidate has to be an input. Run as a script, sys.path[0]
    # is this file's directory, so `cortex_platform` would otherwise be resolved
    # by whatever a site .pth happens to point at -- silently probing a different
    # checkout than the one being released.
    candidate_source = (
        args.candidate_source.resolve(strict=True)
        if args.candidate_source is not None
        else Path(__file__).resolve().parents[2]
    )
    sys.path.insert(0, str(candidate_source))
    from cortex_platform.product.control import ControlStore
    from cortex_platform.product.control import schema

    resolved = Path(schema.__file__).resolve()
    assert resolved == candidate_source / "cortex_platform/product/control/schema.py", (
        "candidate schema resolved to %s, not the requested source %s"
        % (resolved, candidate_source)
    )

    from_schema = args.from_schema if args.from_schema is not None else baseline_versions[-1]
    to_schema = args.to_schema if args.to_schema is not None else schema.SCHEMA_VERSION
    assert schema.SCHEMA_VERSION == to_schema, (
        "candidate source is schema %d, not the requested target %d"
        % (schema.SCHEMA_VERSION, to_schema)
    )
    assert baseline_versions == list(range(1, from_schema + 1)), (
        "baseline is not a contiguous schema-%d store" % from_schema
    )
    assert from_schema < to_schema, "probe upgrades forward only"

    scripts = dict(schema.migration_scripts())
    pending = [version for version in sorted(scripts) if from_schema < version <= to_schema]
    assert pending, "no migration separates the baseline from the candidate"
    # Interrupt inside the LAST pending migration: the whole set shares one
    # exclusive transaction, so failing at the end is what proves the earlier
    # migrations roll back too. Keyed on the script itself, because an
    # ALTER-only migration carries no table name to match on.
    interrupt_at = scripts[pending[-1]]

    migrated = output / "migrated" / "control.db"
    copy_store(source, migrated)
    with closing(sqlite3.connect(migrated)) as connection:
        original = schema._execute_script_in_transaction

        def interrupted(connection, script):
            original(connection, script)
            if script is interrupt_at:
                raise RuntimeError("migration_probe_interruption")

        schema._execute_script_in_transaction = interrupted
        try:
            try:
                schema.apply_migrations(connection, now="migration-probe")
            except RuntimeError as error:
                assert str(error) == "migration_probe_interruption"
            else:
                raise AssertionError("interruption was not exercised")
        finally:
            schema._execute_script_in_transaction = original
    assert inventory(migrated) == baseline
    store = ControlStore(migrated)
    store.initialize()
    store.initialize()
    assert applied_versions(migrated) == list(range(1, to_schema + 1))
    after = inventory(migrated, baseline_columns)
    observed = inventory(migrated)
    new_tables = sorted(set(observed) - set(baseline))
    added_columns = {
        table: facts["added_columns"]
        for table, facts in after.items()
        if facts["added_columns"]
    }
    for table in baseline:
        if table == "schema_migrations":
            continue
        # Projected onto the baseline's columns: identical bytes, or the
        # migration touched data it was not supposed to.
        assert after[table] == {**baseline[table], "added_columns": after[table]["added_columns"]}, table
    for table, columns in added_columns.items():
        with closing(_read_only(migrated)) as connection:
            for column in columns:
                quoted_table = '"' + table.replace('"', '""') + '"'
                quoted_column = '"' + column.replace('"', '""') + '"'
                unset = connection.execute(
                    "SELECT count(*) FROM " + quoted_table + " WHERE " + quoted_column + " IS NOT NULL"
                ).fetchone()[0]
                assert unset == 0, (table, column, unset)
    for table in new_tables:
        assert observed[table]["rows"] == 0, table
    assert thread_order(migrated) == baseline_order

    old_program = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from cortex_platform.product.control import ControlStore; "
        "ControlStore(sys.argv[2]).initialize()"
    )
    refused = subprocess.run([sys.executable, "-c", old_program, str(args.previous_source.resolve()),
                              str(migrated)], capture_output=True, text=True)
    assert refused.returncode != 0 and "newer" in refused.stderr.lower(), "old schema reader did not refuse"
    restored = output / "restored" / "control.db"
    copy_store(source, restored)
    accepted = subprocess.run([sys.executable, "-c", old_program, str(args.previous_source.resolve()),
                               str(restored)], capture_output=True, text=True)
    assert accepted.returncode == 0, "old generation could not reopen restored baseline"
    assert inventory(restored) == baseline
    assert inventory(source) == baseline
    result = {"schema": PROBE_SCHEMA, "passed": True,
              "candidate_source": str(candidate_source),
              "from_schema": from_schema, "to_schema": to_schema,
              "applied_migrations": pending,
              "preserved_tables": len(baseline) - 1,
              "new_tables": new_tables,
              "added_columns": added_columns,
              "thread_order_preserved": thread_order(migrated) == baseline_order,
              "interrupted_migration_rolled_back": True, "initialization_idempotent": True,
              "previous_schema_reader_refused": True, "backup_restored_previous_reader": True,
              "source_copy_unchanged": True,
              "limitation": "Database-copy acceptance only; installed generation and asset-root recovery remain separate."}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
