"""Real temporary SQLite checks for the probe's preservation comparator.

The whole-row fingerprint the probe used to carry reported a preserved table as
mutated the moment a migration added a column, which is exactly the shape of
Control migration 18. These fix that behaviour in place; the end-to-end probe
still needs a real baseline database and two source checkouts.
"""
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import migration_probe as probe


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cortex-probe-inventory-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "control.db"
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
CREATE TABLE workspaces(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE threads(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                     title TEXT NOT NULL, created_at TEXT NOT NULL);
INSERT INTO workspaces VALUES('ws1','Trial','2026-09-07T00:00:00Z');
INSERT INTO threads VALUES('th0','ws1','First','2026-09-07T00:00:00Z');
INSERT INTO threads VALUES('th1','ws1','Second','2026-09-07T00:01:00Z');
""")
            db.commit()

    def migrated(self, script):
        copy = self.root / "migrated.db"
        copy.write_bytes(self.database.read_bytes())
        with contextlib.closing(sqlite3.connect(copy)) as db:
            db.executescript(script)
            db.commit()
        return copy

    def test_an_added_column_is_reported_not_mistaken_for_row_mutation(self):
        """This is Control migration 18's exact shape."""

        baseline = probe.inventory(self.database)
        after_database = self.migrated("ALTER TABLE threads ADD COLUMN archived_at TEXT;")

        projected = probe.inventory(after_database, probe.columns_of(baseline))
        observed = probe.inventory(after_database)

        self.assertEqual(projected["threads"]["sha256"], baseline["threads"]["sha256"])
        self.assertEqual(projected["threads"]["rows"], baseline["threads"]["rows"])
        self.assertEqual(projected["threads"]["added_columns"], ["archived_at"])
        self.assertEqual(baseline["threads"]["added_columns"], [])
        # The unprojected fingerprint is what the old comparator used, and it
        # differs -- which is why the projection exists.
        self.assertNotEqual(observed["threads"]["sha256"], baseline["threads"]["sha256"])
        self.assertEqual(sorted(set(observed) - set(baseline)), [])

    def test_a_real_row_change_still_fails_the_projected_comparison(self):
        """The projection must not become a way to hide data loss."""

        baseline = probe.inventory(self.database)
        after_database = self.migrated(
            "ALTER TABLE threads ADD COLUMN archived_at TEXT;"
            "UPDATE threads SET title='rewritten' WHERE id='th0';"
        )

        projected = probe.inventory(after_database, probe.columns_of(baseline))

        self.assertNotEqual(projected["threads"]["sha256"], baseline["threads"]["sha256"])

    def test_a_deleted_row_still_fails_the_projected_comparison(self):
        baseline = probe.inventory(self.database)
        after_database = self.migrated(
            "ALTER TABLE threads ADD COLUMN archived_at TEXT;"
            "DELETE FROM threads WHERE id='th1';"
        )

        projected = probe.inventory(after_database, probe.columns_of(baseline))

        self.assertEqual(projected["threads"]["rows"], 1)
        self.assertNotEqual(projected["threads"]["sha256"], baseline["threads"]["sha256"])

    def test_a_new_table_is_reported_separately_from_the_projection(self):
        """Migration 17's shape: a table, not a column."""

        baseline = probe.inventory(self.database)
        after_database = self.migrated("CREATE TABLE research_contexts(run_id TEXT PRIMARY KEY);")

        observed = probe.inventory(after_database)
        projected = probe.inventory(after_database, probe.columns_of(baseline))

        self.assertEqual(sorted(set(observed) - set(baseline)), ["research_contexts"])
        self.assertEqual(observed["research_contexts"]["rows"], 0)
        for table in baseline:
            self.assertEqual(projected[table]["sha256"], baseline[table]["sha256"])

    def test_thread_order_is_read_by_the_documented_listing_key(self):
        after_database = self.migrated(
            "ALTER TABLE threads ADD COLUMN archived_at TEXT;"
            "CREATE INDEX threads_workspace_archived_idx ON threads(workspace_id, archived_at, created_at, id);"
        )

        self.assertEqual(probe.thread_order(self.database), [("ws1", "th0"), ("ws1", "th1")])
        self.assertEqual(probe.thread_order(after_database), probe.thread_order(self.database))

    def test_a_store_without_threads_reports_no_order_rather_than_failing(self):
        empty = self.root / "empty.db"
        with contextlib.closing(sqlite3.connect(empty)) as db:
            db.execute("CREATE TABLE schema_migrations(version INTEGER)")
            db.commit()

        self.assertIsNone(probe.thread_order(empty))


class ControlMigration22Tests(unittest.TestCase):
    """The probe's comparator accepts 21->22 as it is: three new empty tables,
    no added column, every schema-21 row unchanged."""

    def setUp(self):
        from cortex_platform.product.control import schema

        self.schema = schema
        self.temp = tempfile.TemporaryDirectory(prefix="cortex-probe-22-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "control.db"
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            for version, script in schema.migration_scripts():
                if version > schema.XHS_SOURCES_MIGRATION:
                    break
                db.executescript(script)
                db.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                    (version,),
                )
            db.executescript("""
INSERT INTO xhs_bloggers (user_id, role, followed, created_at, updated_at)
VALUES ('fedcba9876543210fedcba98', 'curator', 1, 'x', 'x');
INSERT INTO xhs_notes (note_id, user_id, note_type, state, title, caption,
    caption_complete, content_version, created_at, updated_at)
VALUES ('0123456789abcdef01234567', 'fedcba9876543210fedcba98', 'normal',
    'identified', 'Note', 'caption', 1, 0, 'x', 'x');
INSERT INTO xhs_recommendations (id, note_id, item_key, kind, title, quote, url,
    url_state, origin, identify_run, created_at, updated_at)
VALUES ('rec-1', '0123456789abcdef01234567', 'title:a', 'blog', 'A blog', 'A blog',
    'https://arxiv.org/abs/2509.00001', 'from_text', 'model', 'run-1', 'x', 'x');
""")
            db.commit()

    def migrate(self, path, interrupt=False):
        schema = self.schema
        script_22 = dict(schema.migration_scripts())[schema.XHS_FALLBACK_MIGRATION]
        original = schema._execute_script_in_transaction

        def interrupted(connection, script):
            original(connection, script)
            if script is script_22:
                raise RuntimeError("migration_probe_interruption")

        with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            if not interrupt:
                schema.apply_migrations(db, now="migration-probe")
                return
            schema._execute_script_in_transaction = interrupted
            try:
                with self.assertRaises(RuntimeError):
                    schema.apply_migrations(db, now="migration-probe")
            finally:
                schema._execute_script_in_transaction = original

    def test_the_upgrade_preserves_every_table_and_adds_three_empty_ones(self):
        baseline = probe.inventory(self.database)
        self.assertEqual(probe.applied_versions(self.database), list(range(1, 22)))
        migrated = self.root / "migrated.db"
        migrated.write_bytes(self.database.read_bytes())
        self.migrate(migrated)

        after = probe.inventory(migrated, probe.columns_of(baseline))
        observed = probe.inventory(migrated)
        self.assertEqual(probe.applied_versions(migrated), list(range(1, 23)))
        for table in baseline:
            if table == "schema_migrations":
                continue
            self.assertEqual(after[table], baseline[table], table)
        self.assertEqual(
            sorted(set(observed) - set(baseline)),
            ["xhs_fallback_items", "xhs_fallback_runs", "xhs_recommendation_reviews"],
        )
        for table in set(observed) - set(baseline):
            self.assertEqual(observed[table]["rows"], 0)

    def test_an_interrupted_upgrade_leaves_the_baseline_inventory(self):
        baseline = probe.inventory(self.database)
        migrated = self.root / "interrupted.db"
        migrated.write_bytes(self.database.read_bytes())
        self.migrate(migrated, interrupt=True)

        self.assertEqual(probe.inventory(migrated), baseline)


if __name__ == "__main__":
    unittest.main()
