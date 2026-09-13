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


if __name__ == "__main__":
    unittest.main()
