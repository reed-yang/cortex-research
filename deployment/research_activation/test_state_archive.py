"""Real temporary SQLite/ditto recovery rehearsal; no installed product is used."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import state_archive as archive

TOOL = Path(archive.__file__).resolve()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cortex-archive-rehearsal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "home/Library/Application Support/Cortex"
        self.app.mkdir(parents=True)
        (self.app / "Data").mkdir()
        (self.app / "State/runtime-update").mkdir(parents=True)
        (self.app / "State/backups").mkdir()
        (self.app / "State/backups/old-proof").write_text("old proof")
        self.config = self.app / "config.toml"
        self.config.write_text('config_version=1\n[runtime]\nprovider="anthropic"\n')
        self.key = self.app / "Data/.control.db.transport.key"
        self.key.write_bytes(b"synthetic transport key")
        self.database = self.app / "Data/control.db"
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
CREATE TABLE schema_migrations(version INTEGER);
INSERT INTO schema_migrations VALUES(16);
CREATE TABLE asset_roots(root_id TEXT,private_path TEXT,revision INTEGER,enabled INTEGER);
CREATE TABLE runs(state TEXT);
CREATE TABLE runtime_pin_releases(state TEXT);
CREATE TABLE runtime_activation_decisions(decision TEXT);
INSERT INTO runtime_activation_decisions VALUES('disable');
CREATE TABLE transport_activation_decisions(decision TEXT);
INSERT INTO transport_activation_decisions VALUES('disable');
""")
        self.session = self.app / "State/runtime-update/session.db"
        with contextlib.closing(sqlite3.connect(self.session)) as db:
            db.execute("CREATE TABLE sessions(content TEXT)")
            db.execute("INSERT INTO sessions VALUES('before')")
            db.commit()
        self.external = self.root / "external-corpus"
        self.external.mkdir()
        (self.external / "notes.md").write_text("before")
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO asset_roots VALUES('corpus',?,1,1)", (str(self.external),))
            db.commit()
        self.pre, self.post = self.root / "pre", self.root / "post"

    def layout(self, nested):
        self.prefix = self.app / "Distribution" if nested else self.root / "prefix"
        self.prefix.mkdir()
        self.pointers(15, 14)
        (self.prefix / "keep-installed-bytes").write_text("generation bytes")

    def pointers(self, current, previous):
        for name, release in (("current.json", current), ("last-known-good.json", previous)):
            (self.prefix / name).write_text(json.dumps({"release_id": f"cortex-release-{release}", "version": str(release)}))

    def capture(self, path, schema, target=None):
        args = [str(TOOL), "capture", "--app", str(self.app), "--prefix", str(self.prefix),
                "--output", str(path), "--schema", str(schema), "--allow-external-root", str(self.external), "--apply"]
        if target is not None:
            args += ["--target-schema", str(target)]
        with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
            archive.main()

    def upgraded(self, source=16, target=17, declare_target=False):
        """Capture the baseline, move the store forward, capture the preservation."""

        self.capture(self.pre, source, target if declare_target else None)
        self.pointers(source, source - 1)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE schema_migrations SET version=?", (target,))
            db.commit()
        with contextlib.closing(sqlite3.connect(self.session)) as db:
            db.execute("UPDATE sessions SET content='after'")
            db.commit()
        (self.external / "notes.md").write_text("after")
        (self.app / "Data/new-artifact.md").write_text("new cited result")
        self.capture(self.post, target)

    def restore(self):
        return subprocess.run(["bash", str(self.pre / "restore-paths.sh")], capture_output=True, text=True,
                              env=dict(os.environ, POST_ARCHIVE=str(self.post), PREP_PYTHON=sys.executable,
                                       ARCHIVE_TOOL=str(TOOL)), timeout=30)

    def assert_recovery(self, nested):
        self.layout(nested)
        self.upgraded()
        pointers = {name: (self.prefix / name).read_bytes() for name in ("current.json", "last-known-good.json")}
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, value in pointers.items():
            self.assertEqual((self.prefix / name).read_bytes(), value)
        self.assertEqual(json.loads(pointers["current.json"])["release_id"], "cortex-release-16")
        self.assertEqual(json.loads(pointers["last-known-good.json"])["release_id"], "cortex-release-15")
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 16)
        with contextlib.closing(sqlite3.connect(self.session)) as db:
            self.assertEqual(db.execute("SELECT content FROM sessions").fetchone()[0], "before")
        with contextlib.closing(sqlite3.connect(self.app / "State.schema17-retained/runtime-update/session.db")) as db:
            self.assertEqual(db.execute("SELECT content FROM sessions").fetchone()[0], "after")
        self.assertEqual(self.key.read_bytes(), b"synthetic transport key")
        self.assertEqual((self.external / "notes.md").read_text(), "before")
        self.assertEqual(Path(str(self.external) + ".schema17-retained/notes.md").read_text(), "after")
        self.assertEqual((self.app / "Data.schema17-retained/new-artifact.md").read_text(), "new cited result")
        self.assertEqual((self.prefix / "keep-installed-bytes").read_text(), "generation bytes")
        self.assertNotIn("Distribution/", " ".join(archive.verify(self.pre)["copies"][0]["files"]))

    def test_mini_nested_distribution_restore_preserves_official_rollback_pair(self):
        self.assert_recovery(True)

    def test_dev_external_prefix_restore_preserves_official_rollback_pair(self):
        self.assert_recovery(False)

    def test_config_data_override_rejected_before_archiving_stale_default_db(self):
        self.layout(False)
        self.config.write_text(self.config.read_text() + '\n[paths]\ndata_dir="../../../real-data"\n')
        with self.assertRaisesRegex(ValueError, "config path override"):
            self.capture(self.pre, 16)
        self.assertFalse(self.pre.exists())

    def test_stale_post_preservation_prevents_any_restore(self):
        self.layout(True)
        self.upgraded()
        (self.app / "Data/late-result.md").write_text("not preserved")
        result = self.restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed since preservation", result.stderr)
        self.assertFalse((self.app / "Data.schema17-retained").exists())

    def test_wrong_official_rollback_pair_prevents_any_restore(self):
        self.layout(False)
        self.upgraded()
        self.pointers(16, 14)
        manifest = json.loads((self.post / "archive.json").read_text())
        manifest["control"]["last_known_good"] = json.loads((self.prefix / "last-known-good.json").read_text())
        (self.post / "archive.json").write_text(json.dumps(manifest))
        result = self.restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback target", result.stderr)

    def test_open_control_descriptor_prevents_capture(self):
        self.layout(False)
        with self.database.open("rb"), self.assertRaisesRegex(ValueError, "descriptor quiescence"):
            self.capture(self.pre, 16)

    def test_missing_explicit_disable_prevents_capture(self):
        self.layout(False)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("DELETE FROM transport_activation_decisions")
            db.commit()
        with self.assertRaisesRegex(ValueError, "explicitly disabled"):
            self.capture(self.pre, 16)

    def test_staged_corruption_refused_before_data_rename(self):
        self.layout(False)
        self.upgraded()
        manifest, _ = archive.restore_preflight(self.pre, self.post)
        for relative, target in archive.restoration_entries(manifest):
            subprocess.run(["/usr/bin/ditto", str(self.pre / relative), str(target) + ".schema16-stage"], check=True)
        (self.app / "Data.schema16-stage/control.db").write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "staged recovery content"):
            archive.restore_preflight(self.pre, self.post, staged=True)
        self.assertFalse((self.app / "Data.schema17-retained").exists())

    def test_current_write_during_staging_refused_before_any_rename(self):
        self.layout(True)
        self.upgraded()
        manifest, _ = archive.restore_preflight(self.pre, self.post)
        for relative, target in archive.restoration_entries(manifest):
            subprocess.run(["/usr/bin/ditto", str(self.pre / relative), str(target) + ".schema16-stage"], check=True)
        (self.external / "notes.md").write_text("late writer")
        with self.assertRaisesRegex(ValueError, "changed since preservation"):
            archive.restore_preflight(self.pre, self.post, staged=True)
        self.assertFalse((self.app / "Data.schema17-retained").exists())

    # -- the 17->18 pair ---------------------------------------------------

    def _seed_schema(self, version):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE schema_migrations SET version=?", (version,))
            db.commit()

    def test_a_declared_pair_recovers_schema17_from_a_schema18_preservation(self):
        """The same ceremony, one schema pair further along."""

        self.layout(True)
        self._seed_schema(17)
        self.pointers(16, 15)
        self.upgraded(source=17, target=18, declare_target=True)
        pointers = {name: (self.prefix / name).read_bytes() for name in ("current.json", "last-known-good.json")}

        result = self.restore()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, value in pointers.items():
            self.assertEqual((self.prefix / name).read_bytes(), value)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 17)
        with contextlib.closing(sqlite3.connect(self.session)) as db:
            self.assertEqual(db.execute("SELECT content FROM sessions").fetchone()[0], "before")
        # The displaced trees are named after the schema they preserve, so the
        # 17->18 pair does not collide with a retained 16->17 recovery.
        with contextlib.closing(sqlite3.connect(self.app / "State.schema18-retained/runtime-update/session.db")) as db:
            self.assertEqual(db.execute("SELECT content FROM sessions").fetchone()[0], "after")
        self.assertEqual((self.app / "Data.schema18-retained/new-artifact.md").read_text(), "new cited result")
        self.assertEqual(Path(str(self.external) + ".schema18-retained/notes.md").read_text(), "after")
        self.assertFalse((self.app / "Data.schema17-retained").exists())

    def test_schema19_preservation_recovers18_without_replacing_generations(self):
        self.layout(True)
        self._seed_schema(18)
        self.pointers(17, 16)
        self.upgraded(source=18, target=19, declare_target=True)
        pointers = {name: (self.prefix / name).read_bytes() for name in ("current.json", "last-known-good.json")}
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, value in pointers.items():
            self.assertEqual((self.prefix / name).read_bytes(), value)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 18)
        with contextlib.closing(sqlite3.connect(self.session)) as db:
            self.assertEqual(db.execute("SELECT content FROM sessions").fetchone()[0], "before")
        self.assertEqual((self.app / "Data.schema19-retained/new-artifact.md").read_text(), "new cited result")
        self.assertEqual(Path(str(self.external) + ".schema19-retained/notes.md").read_text(), "after")

    def test_the_staging_suffix_is_read_from_the_manifest_not_recomputed(self):
        """`restore-paths.sh` outlives the tool it calls back into."""

        self.layout(False)
        self._seed_schema(17)
        self.pointers(16, 15)
        self.capture(self.pre, 17, 18)

        manifest = archive.verify(self.pre)

        self.assertEqual(manifest["schema"], archive.ARCHIVE_SCHEMA)
        self.assertEqual(manifest["target_schema"], 18)
        self.assertEqual(manifest["staging"], {"stage": ".schema17-stage", "retained": ".schema18-retained"})
        script = (self.pre / "restore-paths.sh").read_text()
        self.assertIn(".schema17-stage", script)
        self.assertIn(".schema18-retained", script)
        self.assertNotIn(".schema16-stage", script)
        self.assertIn("Restore schema17 from a schema18 preservation", script)
        self.assertIn('"$ARCHIVE_TOOL" restore ', script)
        self.assertNotIn("restore-preflight", script)
        self.assertIn("then cortex-release-16 start", script)
        self.assertNotIn("then cortex-release-15 start", script)

    def test_a_preservation_capture_never_pretends_to_be_a_baseline(self):
        """The old silent trap: a non-16 baseline wrote no restore script."""

        self.layout(False)
        self._seed_schema(17)
        self.pointers(16, 15)

        self.capture(self.post, 17)
        self.assertFalse((self.post / "restore-paths.sh").exists())
        self.assertIsNone(archive.verify(self.post)["target_schema"])

        with self.assertRaisesRegex(ValueError, "declares no recovery target"):
            archive.restore_preflight(self.post, self.post)

    def test_a_target_at_or_below_the_baseline_is_refused(self):
        self.layout(False)
        self._seed_schema(17)
        self.pointers(16, 15)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.capture(self.pre, 17, 17)
        self.assertFalse(self.pre.exists())

    def test_a_legacy_format_archive_is_still_read_as_the_16_to_17_pair(self):
        """`/1` archives were only ever written for one pair; keep reading them."""

        self.layout(False)
        self.capture(self.pre, 16)
        manifest = json.loads((self.pre / "archive.json").read_text())
        del manifest["target_schema"], manifest["staging"]
        manifest["schema"] = archive.LEGACY_ARCHIVE_SCHEMA
        (self.pre / "archive.json").write_text(json.dumps(manifest))

        restored = archive.verify(self.pre)

        self.assertEqual(restored["target_schema"], 17)
        self.assertEqual(restored["staging"], {"stage": ".schema16-stage", "retained": ".schema17-retained"})

    def test_enabled_distribution_root_is_refused(self):
        self.layout(True)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO asset_roots VALUES('invalid',?,1,1)", (str(self.prefix),))
            db.commit()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.capture(self.pre, 16)
        self.assertFalse(self.pre.exists())


class SingleProcessRecoveryTests(ArchiveTests):
    """Run the same corruption and real SQLite recovery matrix on the fast path."""

    def restore(self):
        return subprocess.run(
            [sys.executable, str(TOOL), "restore", str(self.pre), str(self.post),
             "--app", str(self.app), "--prefix", str(self.prefix), "--apply"],
            capture_output=True, text=True, timeout=30,
        )

    def test_each_archive_is_scanned_once_during_restore(self):
        self.layout(True)
        self.upgraded()
        with patch.object(archive, "tree", wraps=archive.tree) as scan:
            archive.restore(self.pre, self.post, self.app, self.prefix, apply=True)
        for root in (self.pre, self.post):
            for child in ("app", "external-1"):
                calls = [call for call in scan.call_args_list if call.args[0] == root / child]
                self.assertEqual(len(calls), 1)

    def test_archive_corruption_during_staging_is_refused(self):
        self.layout(True)
        self.upgraded()
        original = subprocess.run

        def copy_after_corruption(command, **kwargs):
            if command[0] == "ditto":
                (self.pre / "external-1/notes.md").write_text("changed after verification")
            return original(command, **kwargs)

        with patch.object(archive.subprocess, "run", side_effect=copy_after_corruption):
            with self.assertRaisesRegex(ValueError, "staged recovery content"):
                archive.restore(self.pre, self.post, self.app, self.prefix, apply=True)
        self.assertFalse((self.app / "Data.schema17-retained").exists())

    def test_late_current_write_during_copy_is_refused(self):
        self.layout(True)
        self.upgraded()
        original = subprocess.run

        def copy_then_write(command, **kwargs):
            result = original(command, **kwargs)
            if command[0] == "ditto":
                (self.external / "notes.md").write_text("late writer")
            return result

        with patch.object(archive.subprocess, "run", side_effect=copy_then_write):
            with self.assertRaisesRegex(ValueError, "changed since preservation"):
                archive.restore(self.pre, self.post, self.app, self.prefix, apply=True)
        self.assertFalse((self.app / "Data.schema17-retained").exists())


class UnicodeArchivePathsTests(unittest.TestCase):
    def test_excluded_generations_are_not_traversed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            excluded = root / "Distribution"
            excluded.mkdir()
            (excluded / "irrelevant").write_text("installed bytes")
            (root / "notes.md").write_text("included")
            (root / "alias").symlink_to(excluded, target_is_directory=True)
            original = os.scandir

            def scan(path):
                self.assertNotEqual(Path(path), excluded)
                return original(path)

            with patch.object(archive.os, "scandir", side_effect=scan):
                result = archive.tree(root, (excluded,))
            self.assertEqual(set(result), {"notes.md", "alias"})
            self.assertEqual(result["alias"], {"link": str(excluded)})

    def test_equivalent_names_compare_without_rewriting_contents(self):
        self.assertEqual(
            archive.normalized_tree_paths({"SCHRÖDINGER/notes.md": {"sha256": "same"}}),
            archive.normalized_tree_paths({"SCHRO\u0308DINGER/notes.md": {"sha256": "same"}}),
        )
        self.assertNotEqual(
            archive.normalized_tree_paths({"SCHRÖDINGER/notes.md": {"sha256": "before"}}),
            archive.normalized_tree_paths({"SCHRO\u0308DINGER/notes.md": {"sha256": "after"}}),
        )

    def test_distinct_names_must_not_collapse_into_one_record(self):
        with self.assertRaisesRegex(ValueError, "collide"):
            archive.normalized_tree_paths({"é": {"size": 1}, "e\u0301": {"size": 2}})


    def test_legacy_archive_with_decomposed_names_stays_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            (root / "app" / "é.txt").write_text("original")
            files = archive.tree(root / "app")
            files["e\u0301.txt"] = files.pop("é.txt")
            (root / "archive.json").write_text(json.dumps({
                "schema": archive.LEGACY_ARCHIVE_SCHEMA,
                "control": {"schema": 16},
                "copies": [{"copy": "app", "files": files}],
            }))
            restored = archive.verify(root)
            self.assertIn("é.txt", restored["copies"][0]["files"])


if __name__ == "__main__":
    unittest.main()
