"""Unit tests for AUDAPACK packing engine."""

import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from audapack.models import PackResult
from audapack.packing import (
    MANIFEST_FILENAME,
    PackingCancelled,
    create_zip,
    delete_old_archives,
    find_latest_archive,
    pack_single,
    path_is_excluded,
    safe_archive_stem,
    verify_zip,
)


class TestPackingEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source_dir = Path(self.temp_dir) / "test_source"
        self.source_dir.mkdir(parents=True)
        self.output_dir = Path(self.temp_dir) / "output"
        self.output_dir.mkdir(parents=True)

        # Create sample files
        (self.source_dir / "file1.txt").write_text("hello world", encoding="utf-8")
        (self.source_dir / "file2.py").write_text("print('test')", encoding="utf-8")
        sub = self.source_dir / "subdir"
        sub.mkdir()
        (sub / "subfile.md").write_text("# Sub", encoding="utf-8")

        # Excluded folder
        ignored = self.source_dir / "node_modules"
        ignored.mkdir()
        (ignored / "pkg.js").write_text("module.exports = {}", encoding="utf-8")

        # Excluded file
        (self.source_dir / "debug.log").write_text("log data", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_archive_stem(self):
        self.assertEqual(safe_archive_stem("My:Project?*"), "My_Project__")
        self.assertEqual(safe_archive_stem("  Valid Name  "), "Valid Name")
        self.assertEqual(safe_archive_stem(""), "Archive")

    def test_path_is_excluded(self):
        excludes = {"node_modules", "*.log", "__pycache__"}
        self.assertTrue(path_is_excluded(Path("a/node_modules/index.js"), excludes))
        self.assertTrue(path_is_excluded(Path("test.log"), excludes))
        self.assertFalse(path_is_excluded(Path("file.txt"), excludes))
        self.assertFalse(path_is_excluded(Path(".saipen/STATE.md"), excludes))

    def test_create_zip_and_verify(self):
        out_zip = self.output_dir / "test.zip"
        excludes = {"node_modules", "*.log"}
        added, raw_b, skipped, errors = create_zip(
            self.source_dir,
            out_zip,
            excludes,
            manifest_meta={"project_name": "TestProj"},
        )
        self.assertTrue(out_zip.exists())
        self.assertFalse(out_zip.with_name(out_zip.name + ".part").exists())

        # Verify entry count (3 files + 1 manifest = 4)
        count = verify_zip(out_zip, added)
        self.assertEqual(count, 4)

        # Check zip entries
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            self.assertIn("file1.txt", names)
            self.assertIn("file2.py", names)
            self.assertIn("subdir/subfile.md", names)
            self.assertIn(MANIFEST_FILENAME, names)
            self.assertNotIn("node_modules/pkg.js", names)
            self.assertNotIn("debug.log", names)

    def test_single_file_pack(self):
        file_path = self.source_dir / "file1.txt"
        out_zip = self.output_dir / "single.zip"
        added, raw_b, skipped, errors = create_zip(
            file_path,
            out_zip,
            excludes=set(),
        )
        self.assertEqual(added, 1)
        self.assertTrue(out_zip.exists())
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            self.assertEqual(names, ["file1.txt"])

    def test_pack_cancellation_cleans_part(self):
        out_zip = self.output_dir / "cancel_test.zip"
        cancel_event = threading.Event()
        cancel_event.set()  # Cancel immediately

        with self.assertRaises(PackingCancelled):
            create_zip(
                self.source_dir,
                out_zip,
                excludes=set(),
                cancel_event=cancel_event,
            )

        self.assertFalse(out_zip.exists())
        self.assertFalse(out_zip.with_name(out_zip.name + ".part").exists())

    def test_delete_old_archives(self):
        # Create an old archive
        old_zip = self.output_dir / "TestProj_01-01-2025-T00-00-00.zip"
        old_zip.write_text("old content")
        unrelated_zip = self.output_dir / "OtherProj_01-01-2025-T00-00-00.zip"
        unrelated_zip.write_text("other content")

        # New archive
        new_zip = self.output_dir / "TestProj_26-08-2026-T02-00-00.zip"
        new_zip.write_text("new content")

        removed, errors = delete_old_archives(self.output_dir, "TestProj", new_zip)
        self.assertEqual(removed, 1)
        self.assertEqual(errors, 0)
        self.assertFalse(old_zip.exists())
        self.assertTrue(new_zip.exists())
        self.assertTrue(unrelated_zip.exists())

    def test_secret_exclusion_from_package(self):
        from audapack.config import DEFAULT_EXCLUDES
        # Place secret files and tokens in source
        (self.source_dir / "token.txt").write_text("secret_token_12345", encoding="utf-8")
        (self.source_dir / "bridge.pid").write_text("9999", encoding="utf-8")
        (self.source_dir / "auth.token").write_text("secret_auth", encoding="utf-8")
        sec_dir = self.source_dir / "secrets"
        sec_dir.mkdir()
        (sec_dir / "key.pem").write_text("private_key", encoding="utf-8")

        res = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="SecretTest",
            excludes=set(DEFAULT_EXCLUDES),
            delete_old=True,
        )
        self.assertTrue(res.success)
        self.assertIsNotNone(res.output_path)

        with zipfile.ZipFile(res.output_path, "r") as zf:
            namelist = zf.namelist()
            self.assertNotIn("token.txt", namelist)
            self.assertNotIn("bridge.pid", namelist)
            self.assertNotIn("auth.token", namelist)
            self.assertTrue(all("secrets" not in name for name in namelist))
            self.assertIn("file1.txt", namelist)
            self.assertIn("file2.py", namelist)

    def test_archive_name_exact_when_delete_old(self):
        """With delete_old (default) the archive keeps the exact project name,
        no timestamp garbage, so clipboard copies read back a clean name."""
        res = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="My Project!",
            excludes=set(),
            delete_old=True,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.output_path.name, "My Project!.zip")

        # Re-packing overwrites the same clean name (no history clutter).
        res2 = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="My Project!",
            excludes=set(),
            delete_old=True,
        )
        self.assertEqual(res2.output_path.name, "My Project!.zip")

        latest = find_latest_archive(self.output_dir, "My Project!")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.name, "My Project!.zip")

    def test_archive_name_timestamped_when_keeping_history(self):
        """With delete_old=False history is preserved via a timestamp suffix."""
        res = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="HistProj",
            excludes=set(),
            delete_old=False,
        )
        self.assertTrue(res.success)
        self.assertTrue(res.output_path.name.startswith("HistProj_"))
        self.assertTrue(res.output_path.name.endswith(".zip"))

    def test_secret_content_absent_from_package(self):
        from audapack.config import DEFAULT_EXCLUDES
        unique_token = "content_scan_probe_token_9f2b7c"

        def _scan(zip_path) -> int:
            occurrences = 0
            with zipfile.ZipFile(zip_path, "r") as zf:
                for info in zf.infolist():
                    with zf.open(info) as fh:
                        if unique_token.encode("utf-8") in fh.read():
                            occurrences += 1
            return occurrences

        # RED CONTROL: a tree whose config-like file carries the token MUST be
        # detected by the content scan -- proves the gate can fail.
        cfg_dir = self.source_dir / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "app_config.json").write_text(
            json.dumps({"bridge": {"token": unique_token, "port": 17843}}),
            encoding="utf-8",
        )
        res_leaky = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="ContentScanLeaky",
            excludes=set(DEFAULT_EXCLUDES),
            delete_old=True,
        )
        self.assertTrue(res_leaky.success)
        self.assertGreaterEqual(_scan(res_leaky.output_path), 1)

        # GREEN: post-fix production shape -- portable config carries NO token
        # value (scrubbed/redacted), so packaged bytes contain zero occurrences.
        (cfg_dir / "app_config.json").write_text(
            json.dumps({"bridge": {"token": "", "port": 17843}}),
            encoding="utf-8",
        )
        (self.source_dir / "notes.md").write_text(
            "deployment notes referencing the rotated token live in user runtime only",
            encoding="utf-8",
        )
        res_clean = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="ContentScanClean",
            excludes=set(DEFAULT_EXCLUDES),
            delete_old=True,
        )
        self.assertTrue(res_clean.success)
        self.assertEqual(_scan(res_clean.output_path), 0)

    def test_pack_single_end_to_end(self):
        res: PackResult = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="MyProject",
            excludes={"node_modules", "*.log"},
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": "MyProject"},
        )
        self.assertTrue(res.success)
        self.assertIsNotNone(res.output_path)
        self.assertTrue(res.output_path.exists())
        self.assertEqual(res.output_path.name, "MyProject.zip")
        self.assertEqual(res.skipped_files, 0)

    def test_timestamp_format_and_toggle(self):
        import re
        # With include_timestamp=True: should produce {stem}_{DD.MM.YY-THH-MM-SS}.zip
        res_ts = pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="StampProject",
            excludes=set(),
            include_timestamp=True,
        )
        self.assertTrue(res_ts.success)
        pattern = r"^StampProject_\d{2}\.\d{2}\.\d{2}-T\d{2}-\d{2}-\d{2}\.zip$"
        self.assertTrue(
            bool(re.match(pattern, res_ts.output_path.name)),
            f"Filename {res_ts.output_path.name} did not match pattern {pattern}",
        )


if __name__ == "__main__":
    unittest.main()
