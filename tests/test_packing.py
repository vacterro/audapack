"""Unit tests for AUDAPACK packing engine."""

import json
import queue
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from audapack.models import PackResult
from audapack.packing import (
    MANIFEST_FILENAME,
    PackingCancelled,
    create_zip,
    delete_old_archives,
    find_latest_archive,
    human_mb,
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

    def test_human_mb_uses_correct_unit_for_archive_size(self):
        self.assertEqual(human_mb(0), "0 B")
        self.assertEqual(human_mb(512), "512 B")
        self.assertEqual(human_mb(1536), "1.5 KB")
        self.assertEqual(human_mb(1024 * 1024), "1.0 MB")
        self.assertEqual(human_mb(2 * 1024**3), "2.0 GB")

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

    def test_concurrent_same_target_pack_preserves_successful_payload(self):
        """CORE-001: a failing same-target pack must never restore its stale
        predecessor over (or unlink) an archive written by another pack."""
        import time as _time

        # Seed a pre-existing OLD archive for the same stem.
        old_zip = self.output_dir / "Same.zip"
        with zipfile.ZipFile(old_zip, "w") as zf:
            zf.writestr("old.txt", "OLD")

        from audapack import packing as packing_mod

        real_create_zip = packing_mod.create_zip
        a_entered = threading.Event()
        state = {"calls": 0}
        state_lock = threading.Lock()
        results = {}

        def flaky_create_zip(*args, **kwargs):
            with state_lock:
                state["calls"] += 1
                is_first = state["calls"] == 1
            if is_first:
                # Pack A: begin (backup done), then fail mid-creation.
                a_entered.set()
                _time.sleep(0.2)
                raise RuntimeError("simulated pack failure (A)")
            return real_create_zip(*args, **kwargs)

        def pack_a():
            results["a"] = pack_single(
                source_path=self.source_dir,
                output_dir=self.output_dir,
                archive_stem="Same",
                excludes=set(),
                delete_old=True,
                include_timestamp=False,
            )

        with patch.object(packing_mod, "create_zip", side_effect=flaky_create_zip):
            ta = threading.Thread(target=pack_a)
            ta.start()
            self.assertTrue(a_entered.wait(timeout=5), "pack A never entered creation")
            # Pack B runs concurrently against the same target.
            results["b"] = pack_single(
                source_path=self.source_dir,
                output_dir=self.output_dir,
                archive_stem="Same",
                excludes=set(),
                delete_old=True,
                include_timestamp=False,
            )
            ta.join(timeout=30)

        self.assertFalse(results["a"].success, "pack A must have failed")
        self.assertTrue(results["b"].success, "pack B must have succeeded")
        final = self.output_dir / "Same.zip"
        self.assertTrue(final.exists(), "final archive missing")
        with zipfile.ZipFile(final) as zf:
            names = zf.namelist()
        self.assertIn("file1.txt", names, "final archive must contain B's payload")
        self.assertNotIn("old.txt", names, "A must not restore the stale predecessor over B")
        self.assertEqual(verify_zip(final, len(names)), len(names), "final archive must be byte-valid")

    def test_concurrent_timestamp_pack_unique_outputs_same_second(self):
        """CORE-001: same-second concurrent timestamp packs must produce unique,
        byte-valid archives (no silent overwrite/collision)."""
        results = {}
        barrier = threading.Barrier(2)

        def pack_stamped(idx: int):
            barrier.wait()
            results[idx] = pack_single(
                source_path=self.source_dir,
                output_dir=self.output_dir,
                archive_stem="StampProj",
                excludes=set(),
                include_timestamp=True,
                delete_old=False,
            )

        threads = [threading.Thread(target=pack_stamped, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        for i in range(2):
            self.assertTrue(results[i].success, f"stamped pack {i} must succeed")
        paths = {results[i].output_path for i in range(2)}
        self.assertEqual(len(paths), 2, "two concurrent stamped packs must not collide")
        for p in paths:
            self.assertTrue(Path(p).exists(), f"missing {p}")
            with zipfile.ZipFile(p) as zf:
                n = zf.namelist()
            self.assertEqual(verify_zip(Path(p), len(n)), len(n), f"{p} must be byte-valid")


class TestTkFallbackPackingOptions(unittest.TestCase):
    def test_worker_passes_independent_packing_options(self):
        from audapack.ui import main_window

        project = SimpleNamespace(
            id="project",
            display_name="Project",
            source_path="source",
            archive_name="Project",
        )
        for delete_old in (False, True):
            for include_timestamp in (False, True):
                window = main_window.MainWindow.__new__(main_window.MainWindow)
                window.config = SimpleNamespace(
                    packing=SimpleNamespace(
                        excludes=[],
                        output_dir="",
                        manifest_enabled=False,
                        delete_old=delete_old,
                        include_timestamp=include_timestamp,
                    )
                )
                window.cancel_event = threading.Event()
                window.ui_queue = queue.Queue()
                window.registry = Mock()
                result = SimpleNamespace(
                    success=False,
                    output_path=None,
                    files_added=0,
                    archive_bytes=0,
                    error_message="",
                )
                with patch.object(main_window, "pack_single", return_value=result) as pack_mock:
                    window._pack_worker([project])

                self.assertEqual(pack_mock.call_args.kwargs["delete_old"], delete_old)
                self.assertEqual(pack_mock.call_args.kwargs["include_timestamp"], include_timestamp)


if __name__ == "__main__":
    unittest.main()


class TestArchiveWeightExclusions(unittest.TestCase):
    """The auditor reads text. Everything else is upload time and nothing else.

    Measured on the operator's machine: __SAITULS packed to 346MB of which
    326MB was .exe and 14MB .dll; 9router to 210MB of which 150MB was git pack
    files and 16MB of web fonts; FastPrompter to 160MB with 89MB of .git and
    35MB of .wav. All of it is uploaded, and the model reads all of it before
    it can write a single ticket.
    """

    def setUp(self):
        from audapack.config import DEFAULT_EXCLUDES

        self.patterns = set(DEFAULT_EXCLUDES)

    def _excluded(self, path: str) -> bool:
        from audapack.packing import path_is_excluded

        return path_is_excluded(Path(path), self.patterns)

    def test_unreadable_weight_is_dropped(self):
        for path in (
            "C:/p/Bin/tool.exe",
            "C:/p/Bin/native.dll",
            "C:/p/lib/core.so",
            "C:/p/sounds/alert.wav",
            "C:/p/clips/demo.mp4",
            "C:/p/assets/inter.woff2",
            "C:/p/vendor/bundle.tgz",
            "C:/p/state/index.zst",
            "C:/p/old/main.py.bak",
            "C:/p/.codebase-memory/graph.db2",
        ):
            self.assertTrue(self._excluded(path), path)

    def test_source_and_docs_and_images_still_ship(self):
        for path in (
            "C:/p/src/main.py",
            "C:/p/README.md",
            "C:/p/pyproject.toml",
            "C:/p/assets/logo.png",
            "C:/p/assets/icon.svg",
            "C:/p/data/fixture.json",
        ):
            self.assertFalse(self._excluded(path), path)

    def test_git_objects_go_but_git_context_stays(self):
        """The audit's GIT_CONTEXT line needs refs, not pack files."""
        self.assertTrue(self._excluded("C:/p/.git/objects/pack/x.pack"))
        self.assertTrue(self._excluded("C:/p/.git/objects/ab/cdef123"))
        self.assertFalse(self._excluded("C:/p/.git/HEAD"))
        self.assertFalse(self._excluded("C:/p/.git/refs/heads/main"))
        self.assertFalse(self._excluded("C:/p/.git/config"))

    def test_a_path_pattern_never_matches_a_bare_directory_name(self):
        """".git/objects" must not take every directory called "objects"."""
        self.assertFalse(self._excluded("C:/p/src/objects/model.py"))
        self.assertFalse(self._excluded("C:/p/objects/README.md"))


class TestBackupIsRollbackAuthority(unittest.TestCase):
    """CORE-001 (audit/1.md): no backup, no canonical replacement.

    Moving the previous archive aside is the ONLY thing a failed pack can
    restore from, and a failure on that move used to be swallowed
    (`backup_path = None`) with the pack carrying on regardless. create_zip
    then atomically replaced the canonical archive and a later verify failure
    unlinked the replacement, so the operator's last good archive was gone --
    reported as an ordinary failed pack.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source_dir = Path(self.temp_dir) / "src"
        self.source_dir.mkdir(parents=True)
        (self.source_dir / "file1.txt").write_text("NEW PAYLOAD", encoding="utf-8")
        self.output_dir = Path(self.temp_dir) / "out"
        self.output_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_previous_archive(self) -> tuple[Path, bytes]:
        archive = self.output_dir / "Proj.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("old.txt", "OLD_GOOD_ARCHIVE")
        return archive, archive.read_bytes()

    def _pack(self):
        return pack_single(
            source_path=self.source_dir,
            output_dir=self.output_dir,
            archive_stem="Proj",
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
        )

    def test_a_failed_backup_aborts_before_the_canonical_replacement(self):
        archive, before = self._seed_previous_archive()
        real_replace = Path.replace

        def refuse_backup(self, destination):
            if ".bak." in Path(destination).name:
                raise PermissionError("simulated sharing violation")
            return real_replace(self, destination)

        with patch.object(Path, "replace", autospec=True, side_effect=refuse_backup), \
             patch("audapack.packing.create_zip", side_effect=AssertionError("packed without rollback authority")), \
             patch("time.sleep"):
            result = self._pack()

        self.assertFalse(result.success)
        self.assertIn("previous archive is untouched", result.error_message.lower())
        self.assertTrue(archive.exists(), "the previous archive was destroyed")
        self.assertEqual(archive.read_bytes(), before, "the previous archive changed")

    def test_a_transient_backup_failure_is_retried_not_fatal(self):
        archive, before = self._seed_previous_archive()
        real_replace = Path.replace
        attempts = {"n": 0}

        def flaky_backup(self, destination):
            if ".bak." in Path(destination).name:
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise PermissionError("simulated sharing violation")
            return real_replace(self, destination)

        with patch.object(Path, "replace", autospec=True, side_effect=flaky_backup), \
             patch("time.sleep"):
            result = self._pack()

        self.assertEqual(attempts["n"], 3)
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(archive) as zf:
            self.assertIn("file1.txt", zf.namelist())
        self.assertNotEqual(archive.read_bytes(), before)

    def test_a_post_commit_verify_failure_restores_the_predecessor_exactly(self):
        archive, before = self._seed_previous_archive()

        with patch("audapack.packing.verify_zip", side_effect=RuntimeError("simulated verify failure")):
            result = self._pack()

        self.assertFalse(result.success)
        self.assertTrue(archive.exists(), "the predecessor was not restored")
        self.assertEqual(archive.read_bytes(), before, "the restored predecessor is not byte-identical")

    def test_a_first_pack_with_no_predecessor_still_packs(self):
        """No previous archive means nothing to secure -- and nothing to lose."""
        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        self.assertTrue((self.output_dir / "Proj.zip").exists())
