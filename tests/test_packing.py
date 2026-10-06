"""Unit tests for AUDAPACK packing engine."""

import json
import os
import queue
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from audapack import packing
from audapack.config import PackingConfig
from audapack.fidelity import build_fidelity_plan
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
from audapack.source_inventory import CODE_RESERVED_ARCHIVE_NAME_CONFLICT


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
        stats = create_zip(
            self.source_dir,
            out_zip,
            excludes,
            manifest_meta={"project_name": "TestProj"},
        )
        self.assertTrue(out_zip.exists())
        self.assertFalse(out_zip.with_name(out_zip.name + ".part").exists())

        # Verify entry count (3 files + 1 manifest = 4)
        count = verify_zip(out_zip, stats.files_added)
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
        stats = create_zip(
            file_path,
            out_zip,
            excludes=set(),
        )
        self.assertEqual(stats.files_added, 1)
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
        predecessor over (or unlink) an archive written by another pack.

        T-190: the write path is now DISCOVER -> FREEZE -> stage -> verify ->
        commit, so the fault is injected at the frozen-inventory staging
        boundary (``stage_inventory_zip``) instead of the obsolete
        ``create_zip`` helper.
        """
        import time as _time

        # Seed a pre-existing OLD archive for the same stem.
        old_zip = self.output_dir / "Same.zip"
        with zipfile.ZipFile(old_zip, "w") as zf:
            zf.writestr("old.txt", "OLD")

        from audapack import packing as packing_mod

        real_stage = packing_mod.stage_inventory_zip
        a_entered = threading.Event()
        state = {"calls": 0}
        state_lock = threading.Lock()
        results = {}

        def flaky_stage_inventory_zip(source_dir, output_zip, inventory, **kwargs):
            with state_lock:
                state["calls"] += 1
                is_first = state["calls"] == 1
            if is_first:
                # Pack A: begin (backup done), then fail mid-staging.
                a_entered.set()
                _time.sleep(0.2)
                raise RuntimeError("simulated pack failure (A)")
            return real_stage(source_dir, output_zip, inventory, **kwargs)

        def pack_a():
            results["a"] = pack_single(
                source_path=self.source_dir,
                output_dir=self.output_dir,
                archive_stem="Same",
                excludes=set(),
                delete_old=True,
                include_timestamp=False,
            )

        with patch.object(packing_mod, "stage_inventory_zip", side_effect=flaky_stage_inventory_zip):
            ta = threading.Thread(target=pack_a)
            ta.start()
            self.assertTrue(a_entered.wait(timeout=5), "pack A never entered staging")
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


class TestProjectOwnedBinaryPacking(unittest.TestCase):
    """Generic .bin inputs are opaque source assets, not generated output."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.source = self.root / "project"
        self.source.mkdir()
        fixtures = self.source / "fixtures"
        fixtures.mkdir()
        (self.source / "host-patch.json").write_text(
            '{"anchor": "fixtures/anchor-853.bin", "replacement": "fixtures/replacement-853.bin"}',
            encoding="utf-8",
        )
        (fixtures / "anchor-853.bin").write_bytes(b"ANCHOR\\x00\\x853")
        (fixtures / "replacement-853.bin").write_bytes(b"REPLACEMENT\\x00\\x853")
        self.output = self.root / "output"
        self.output.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _pack(self, *, profile="standard", excludes=None, always_exclude=None):
        packing = PackingConfig(
            fidelity_profile=profile,
            include_timestamp=False,
            manifest_enabled=True,
            always_exclude=list(always_exclude or []),
        )
        return pack_single(
            self.source,
            self.output,
            f"Binary-{profile}",
            set(excludes or []),
            delete_old=True,
            include_timestamp=False,
            packing=packing,
            manifest_meta={"project_name": "Binary"},
        )

    def test_standard_keeps_small_project_owned_bin_fixtures(self):
        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as archive:
            names = set(archive.namelist())
        self.assertIn("fixtures/anchor-853.bin", names)
        self.assertIn("fixtures/replacement-853.bin", names)

    def test_full_keeps_ordinary_project_owned_bin_input(self):
        result = self._pack(profile="full")
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as archive:
            self.assertIn("fixtures/anchor-853.bin", archive.namelist())
            self.assertIn("fixtures/replacement-853.bin", archive.namelist())

    def test_explicit_configured_bin_exclusion_still_wins(self):
        result = self._pack(excludes=["*.bin"])
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as archive:
            names = set(archive.namelist())
        self.assertNotIn("fixtures/anchor-853.bin", names)
        self.assertNotIn("fixtures/replacement-853.bin", names)

    def test_explicit_always_exclude_bin_still_wins(self):
        result = self._pack(always_exclude=["*.bin"])
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as archive:
            names = set(archive.namelist())
        self.assertNotIn("fixtures/anchor-853.bin", names)
        self.assertNotIn("fixtures/replacement-853.bin", names)

    def test_large_opaque_bin_remains_subject_to_standard_size_budget(self):
        large = self.source / "large.bin"
        large.write_bytes(b"x" * (2 * 1024 * 1024))
        plan = build_fidelity_plan(self.source, set(), profile="standard", max_mb=1)
        self.assertFalse(plan.decisions["large.bin"].include)
        self.assertEqual(plan.decisions["large.bin"].reason, "size_limit")
        self.assertEqual(plan.discovered, plan.included + plan.excluded + plan.failed)
        self.assertEqual(plan.decisions["large.bin"].priority, 3)

    def test_manifest_accounting_reconciles_for_bin_inputs(self):
        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as archive:
            manifest = json.loads(archive.read(MANIFEST_FILENAME).decode("utf-8"))
        self.assertTrue(manifest["accounting_reconciled"])
        self.assertEqual(
            manifest["files_discovered"],
            manifest["files_included"] + manifest["files_excluded"] + manifest["files_failed"],
        )


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
    files. All of it is uploaded, and the model reads all of it before it can
    write a single ticket. Media and fonts left this layer with CORE-002
    (audit/6.md): audapack.fidelity owns them.
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
            "C:/p/vendor/bundle.tgz",
            "C:/p/state/index.zst",
            "C:/p/old/main.py.bak",
            "C:/p/.codebase-memory/graph.db2",
        ):
            self.assertTrue(self._excluded(path), path)

    def test_media_and_fonts_are_owned_by_the_fidelity_profile(self):
        """CORE-002 (audit/6.md): the pattern layer must not pre-empt fidelity.

        `build_fidelity_plan` matches configured excludes BEFORE
        `media_class_for`, so while these patterns sat in DEFAULT_EXCLUDES no
        profile could sample them and FULL could not preserve them -- while the
        manifest still declared full_snapshot.
        """
        for path in (
            "C:/p/sounds/alert.wav",
            "C:/p/clips/demo.mp4",
            "C:/p/assets/inter.woff2",
            "C:/p/assets/inter.ttf",
        ):
            self.assertFalse(self._excluded(path), path)

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
             patch("audapack.packing.stage_inventory_zip", side_effect=AssertionError("packed without rollback authority")), \
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
        # (intentional continuation below)

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
        """T-190: verification now runs at the inventory-parity boundary.

        A verification failure BEFORE the final commit must restore/preserve
        the previous known-good archive exactly -- proven here by injecting
        the fault through ``verify_inventory_archive`` (the new authority),
        not the obsolete ``verify_zip`` helper.
        """
        archive, before = self._seed_previous_archive()

        with patch(
            "audapack.packing.verify_inventory_archive",
            side_effect=packing.ArchiveVerifyError("simulated verify failure"),
        ):
            result = self._pack()

        self.assertFalse(result.success)
        self.assertTrue(archive.exists(), "the predecessor was not restored")
        self.assertEqual(archive.read_bytes(), before, "the restored predecessor is not byte-identical")

    def test_a_first_pack_with_no_predecessor_still_packs(self):
        """No previous archive means nothing to secure -- and nothing to lose."""
        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        self.assertTrue((self.output_dir / "Proj.zip").exists())


class TestFreshnessOnlyLooksAtPackableFiles(unittest.TestCase):
    """PERF-004 (audit/2.md): reuse must not traverse what packing excludes.

    `ensure_fresh_archive()` walked the tree with NO exclusion matcher and
    stat'ed every file below the project root, so deciding an archive could be
    reused traversed exactly the generated/cache/object trees the archive omits.
    Measured: 0.13 ms with nothing excluded, 38.27 ms at 5,000 excluded files,
    141.25 ms at 20,000 -- and the same archive was reused in every case.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.source = self.root / "proj"
        self.source.mkdir(parents=True)
        (self.source / "main.py").write_text("print('x')", encoding="utf-8")
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self):
        from audapack.config import AppConfig, PackingConfig
        from audapack.models import Project

        config = AppConfig(packing=PackingConfig(output_dir=str(self.output_dir), delete_old=True))
        config.projects = [Project(
            id="proj", display_name="proj", source_path=str(self.source), archive_name="proj",
        )]
        return config

    def _service(self):
        from audapack.services.packing_service import PackingService

        return PackingService(self._config(), base_dir=self.root)

    def _seed_archive(self, newer_by: float = 60.0, *, manifest: bool = True) -> Path:
        """A reusable predecessor: fresh AND built under the current policy.

        CORE-006: reuse is gated on the archive's own manifest, so a fixture that
        omits it is testing the legacy-archive path instead of PERF-004's
        traversal question. ``manifest=False`` seeds that legacy archive on
        purpose.
        """
        import json as _json
        import os as _os

        from audapack.fidelity import policy_fingerprint_from_config
        from audapack.packing import MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION

        config = self._config()
        archive = self.output_dir / "proj.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", "print('x')")
            if manifest:
                zf.writestr(MANIFEST_FILENAME, _json.dumps({
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "product": "AUDAPACK",
                    "source_path": str(self.source),
                    "fidelity_profile": config.packing.fidelity_profile,
                    "archive_semantics": "full_snapshot",
                    "policy_fingerprint": policy_fingerprint_from_config(
                        config.packing, set(config.packing.excludes)
                    ),
                }))
        newest = max(path.stat().st_mtime for path in self.source.rglob("*") if path.is_file())
        stamp = newest + newer_by
        _os.utime(archive, (stamp, stamp))
        return archive

    def test_excluded_weight_is_never_stat_ed(self):
        import os as _os

        heavy = self.source / "node_modules" / "pkg"
        heavy.mkdir(parents=True)
        for index in range(30):
            (heavy / f"chunk{index}.js").write_text("x", encoding="utf-8")
        archive = self._seed_archive()

        stats = []
        scans = []
        real_stat = Path.stat
        real_scandir = _os.scandir

        def counting_stat(self, *args, **kwargs):
            stats.append(str(self))
            return real_stat(self, *args, **kwargs)

        def counting_scandir(path, *args, **kwargs):
            scans.append(str(path))
            return real_scandir(path, *args, **kwargs)

        with patch.object(Path, "stat", autospec=True, side_effect=counting_stat), \
                patch.object(_os, "scandir", side_effect=counting_scandir):
            result = self._service().ensure_fresh_archive("proj")

        self.assertTrue(result.success)
        self.assertEqual(result.output_path, archive, "the fresh archive was not reused")
        # PERF-001: metadata now comes from the directory read, so the excluded
        # weight must be untouched by BOTH -- unstat'ed and unenumerated.
        touched = [path for path in stats + scans if "node_modules" in path]
        self.assertEqual(touched, [], f"excluded weight was inspected: {touched[:3]}")

    def test_a_changed_included_file_still_forces_a_repack(self):
        import os as _os

        self._seed_archive()
        included = self.source / "main.py"
        stamp = self.output_dir.joinpath("proj.zip").stat().st_mtime + 120
        _os.utime(included, (stamp, stamp))

        packed = []
        service = self._service()
        service.pack_project = lambda project_id, **kw: packed.append(project_id) or PackResult(
            project_id=project_id, name=project_id, source_path=str(self.source), success=True,
        )
        service.ensure_fresh_archive("proj")
        self.assertEqual(packed, ["proj"], "a newer included file did not invalidate the archive")

    def test_a_change_under_an_excluded_directory_never_repacks(self):
        import os as _os

        archive = self._seed_archive()
        heavy = self.source / ".venv" / "lib"
        heavy.mkdir(parents=True)
        stamp = archive.stat().st_mtime + 500
        target = heavy / "site.py"
        target.write_text("x", encoding="utf-8")
        _os.utime(target, (stamp, stamp))

        packed = []
        service = self._service()
        service.pack_project = lambda project_id, **kw: packed.append(project_id)
        result = service.ensure_fresh_archive("proj")

        self.assertEqual(packed, [], "an excluded file forced a repack it cannot affect")
        self.assertTrue(result.success)
        self.assertEqual(result.output_path, archive)

    def test_no_existing_archive_packs_without_a_freshness_walk(self):
        import os as _os

        walks = []
        real_walk = _os.walk
        real_scandir = _os.scandir

        def counting_walk(*args, **kwargs):
            walks.append(str(args[0]))
            return real_walk(*args, **kwargs)

        def counting_scandir(path, *args, **kwargs):
            # The plan walk is scandir-based now (PERF-001); the source tree is
            # the only traversal this test forbids, and the output directory is
            # scanned to find the archive.
            if str(self.source) in str(path):
                walks.append(str(path))
            return real_scandir(path, *args, **kwargs)

        packed = []
        service = self._service()
        service.pack_project = lambda project_id, **kw: packed.append(project_id)
        with patch.object(_os, "walk", side_effect=counting_walk), \
                patch.object(_os, "scandir", side_effect=counting_scandir):
            service.ensure_fresh_archive("proj")

        self.assertEqual(packed, ["proj"])
        self.assertEqual(walks, [], "a first pack walked the tree to prove nothing")

    def test_the_eligible_walk_matches_what_packing_would_include(self):
        from audapack.packing import eligible_source_files

        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "big.js").write_text("x", encoding="utf-8")
        (self.source / "debug.log").write_text("noise", encoding="utf-8")
        (self.source / "README.md").write_text("docs", encoding="utf-8")

        from audapack.config import DEFAULT_EXCLUDES

        names = sorted(
            path.name for path in eligible_source_files(self.source, set(DEFAULT_EXCLUDES))
        )
        self.assertIn("main.py", names)
        self.assertIn("README.md", names)
        self.assertNotIn("big.js", names)
        self.assertNotIn("debug.log", names)


class TestSingleFileSecretBoundary(unittest.TestCase):
    """CORE-003 (audit/3.md): the mandatory-secret rule was directory-only.

    `create_zip()` merges MANDATORY_EXCLUDES ("must never be packaged":
    token.txt, *.token, *.secret, *.secrets, secrets) and the directory walk
    applies them -- but the single-file branch checked cancellation and symlink
    status and then wrote the file straight in. `--pack <file>` and the Explorer
    context menu route there, so selecting `token.txt` produced success=True and
    an archive containing the secret. These ZIPs exist to be uploaded to an AI
    auditor, which makes it a disclosure path.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _pack_file(self, name: str, excludes=None):
        target = self.root / name
        target.write_text("SECRET", encoding="utf-8")
        return target, pack_single(
            source_path=target,
            output_dir=self.output_dir,
            archive_stem=target.stem,
            excludes=set(excludes or ()),
            delete_old=True,
            include_timestamp=False,
        )

    def test_a_mandatory_excluded_file_is_refused_not_packed(self):
        for name in ("token.txt", "deploy.token", "prod.secret", "app.secrets"):
            with self.subTest(name=name):
                _target, result = self._pack_file(name)
                self.assertFalse(result.success, f"{name} was packed")
                self.assertIn("exclud", (result.error_message or "").lower())
                produced = list(self.output_dir.glob("*.zip"))
                self.assertEqual(produced, [], f"{name} produced {produced}")

    def test_a_user_configured_exclusion_is_refused_too(self):
        _target, result = self._pack_file("private.env", excludes={"*.env"})
        self.assertFalse(result.success)
        self.assertEqual(list(self.output_dir.glob("*.zip")), [])

    def test_an_ordinary_single_file_still_packs(self):
        target, result = self._pack_file("notes.md")
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
        self.assertIn("notes.md", names)
        # T-190: reserved archive-control metadata is NOT source payload.
        payload = [n for n in names if n not in packing.RESERVED_ARCHIVE_NAMES]
        self.assertEqual(payload, ["notes.md"])
        self.assertTrue(target.exists())

    def test_the_same_secret_inside_a_directory_is_still_skipped(self):
        source = self.root / "proj"
        source.mkdir()
        (source / "main.py").write_text("print('x')", encoding="utf-8")
        (source / "token.txt").write_text("SECRET", encoding="utf-8")
        result = pack_single(
            source_path=source,
            output_dir=self.output_dir,
            archive_stem="proj",
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
        )
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
        # T-190: distinguish source payload from reserved archive metadata;
        # the canonical inventory manifest may be present, the secret must not.
        payload = [n for n in names if n not in packing.RESERVED_ARCHIVE_NAMES]
        self.assertEqual(payload, ["main.py"])
        self.assertNotIn("token.txt", names)
        self.assertIn(".audapack/manifest.json", names)

    def test_the_cli_pack_path_refuses_it_as_well(self):
        from audapack import app

        target = self.root / "token.txt"
        target.write_text("SECRET", encoding="utf-8")
        config = AppConfigForPack(self.output_dir)
        with patch.object(app, "load_config", return_value=config):
            code = app.run_pack_path(str(target))
        self.assertEqual(code, 1, "the documented CLI entry point packed a secret")
        self.assertEqual(list(self.output_dir.glob("*.zip")), [])


def AppConfigForPack(output_dir: Path):
    from audapack.config import AppConfig, PackingConfig

    config = AppConfig(packing=PackingConfig(output_dir=str(output_dir), manifest_enabled=False))
    config.projects = []
    return config


class TestCanonicalArchiveIdentity(unittest.TestCase):
    """CORE-005 (audit/3.md): identity is ordered, and blank is not an alias.

    Every alias went into ONE unordered set and blank values were passed through
    `safe_archive_stem()`, which maps "" to the literal fallback "Archive". So a
    project with no archive_name matched a stray generic `Archive.zip`, and a
    NEWER display-name archive beat the explicitly configured archive_name --
    both reproduced. This resolver feeds packing freshness and Bridge artifact
    ownership, so it decided those on the wrong ZIP.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.output_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _archive(self, name: str, age_offset: float) -> Path:
        import os as _os

        path = self.output_dir / f"{name}.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("x.txt", name)
        stamp = 1_700_000_000 + age_offset
        _os.utime(path, (stamp, stamp))
        return path

    def _project(self, **kwargs):
        from audapack.models import Project

        defaults = {"id": "proj", "display_name": "My Display", "source_path": "C:/x"}
        defaults.update(kwargs)
        return Project(**defaults)

    def test_the_configured_archive_name_wins_over_a_newer_display_archive(self):
        from audapack.packing import find_archive_for_project

        canonical = self._archive("CustomArchive", 0)
        self._archive("My Display", 500)  # newer, and must lose
        project = self._project(archive_name="CustomArchive")
        self.assertEqual(find_archive_for_project(project, self.output_dir), canonical)

    def test_a_blank_archive_name_never_matches_the_generic_fallback(self):
        from audapack.packing import find_archive_for_project

        expected = self._archive("MyProj", 0)
        self._archive("Archive", 900)  # newer generic file from another project
        project = self._project(archive_name="", display_name="MyProj")
        self.assertEqual(
            find_archive_for_project(project, self.output_dir), expected,
            "a blank archive_name invented the alias 'Archive'",
        )

    def test_the_display_name_is_still_the_fallback(self):
        from audapack.packing import find_archive_for_project

        expected = self._archive("My Display", 0)
        project = self._project(archive_name="")
        self.assertEqual(find_archive_for_project(project, self.output_dir), expected)

    def test_the_id_is_the_last_resort(self):
        from audapack.packing import find_archive_for_project

        expected = self._archive("proj", 0)
        project = self._project(archive_name="", display_name="")
        self.assertEqual(find_archive_for_project(project, self.output_dir), expected)

    def test_newest_still_wins_inside_one_alias_family(self):
        from audapack.packing import find_archive_for_project

        self._archive("Proj_01.01.26-T00-00-00", 0)
        newest = self._archive("Proj_02.01.26-T00-00-00", 400)
        project = self._project(archive_name="Proj")
        self.assertEqual(find_archive_for_project(project, self.output_dir), newest)


class TestPolicyIsPartOfArchiveIdentity(unittest.TestCase):
    """CORE-006 (audit/6.md): reuse was a timestamp property alone.

    Reproduced against HEAD: pack a project as COMPACT, leave the source
    untouched so the archive stays newer, switch `fidelity_profile` to FULL, and
    `ensure_fresh_archive` reported success and returned the COMPACT archive --
    whose own manifest still declared `fidelity_profile=compact`,
    `archive_semantics=audit_representation`. The returned PackResult carried
    blank policy fields, so nothing downstream could notice. An audit asking for
    a full snapshot silently consumed a sampled representation.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.source = self.root / "proj"
        self.source.mkdir(parents=True)
        (self.source / "main.py").write_text("print('x')", encoding="utf-8")
        (self.source / "README.md").write_text("docs", encoding="utf-8")
        assets = self.source / "assets"
        assets.mkdir()
        for index in range(4):
            (assets / f"clip{index}.png").write_bytes(b"\x89PNG" + b"z" * 4096)
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _service(self, **packing_kwargs):
        from audapack.config import AppConfig, PackingConfig
        from audapack.models import Project
        from audapack.services.packing_service import PackingService

        packing = PackingConfig(
            output_dir=str(self.output_dir),
            delete_old=True,
            include_timestamp=False,
            **packing_kwargs,
        )
        config = AppConfig(packing=packing)
        config.projects = [Project(
            id="proj", display_name="proj", source_path=str(self.source), archive_name="proj",
        )]
        return PackingService(config, base_dir=self.root)

    def _pack(self, **packing_kwargs) -> Path:
        """Pack for real, then make the archive unambiguously newer than the source."""
        import os as _os

        result = self._service(**packing_kwargs).pack_project("proj")
        self.assertTrue(result.success, result.error_message)
        archive = Path(result.output_path)
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        _os.utime(archive, (newest + 120, newest + 120))
        return archive

    def _reuses(self, archive: Path, **packing_kwargs):
        """(reused, result) for a freshness check that never repacks silently."""
        service = self._service(**packing_kwargs)
        repacked = []
        service.pack_project = lambda project_id, **kw: repacked.append(project_id) or PackResult(
            project_id=project_id, name=project_id, source_path=str(self.source), success=True,
        )
        result = service.ensure_fresh_archive("proj")
        return (not repacked and result.output_path == archive), result

    def test_an_unchanged_source_under_the_same_policy_is_reused(self):
        archive = self._pack(fidelity_profile="compact")
        reused, result = self._reuses(archive, fidelity_profile="compact")
        self.assertTrue(reused, "identical policy and unchanged source did not reuse")
        self.assertTrue(result.success)

    def test_a_profile_change_repacks_even_though_the_archive_is_newer(self):
        archive = self._pack(fidelity_profile="compact")
        reused, _ = self._reuses(archive, fidelity_profile="full")
        self.assertFalse(reused, "a COMPACT archive was reused for a FULL request")

    def test_every_content_affecting_policy_change_repacks(self):
        cases = {
            "excludes": {"excludes": ["*.md"]},
            "always_include": {"always_include": ["assets/clip0.png"]},
            "always_exclude": {"always_exclude": ["README.md"]},
            "size_override": {"fidelity_max_mb": 1},
            "media_samples": {"fidelity_media_samples": 1},
            "media_bytes": {"fidelity_media_bytes": 1024},
        }
        for label, override in cases.items():
            with self.subTest(policy=label):
                archive = self._pack(fidelity_profile="standard")
                reused, _ = self._reuses(archive, fidelity_profile="standard", **override)
                self.assertFalse(reused, f"a {label} change reused the old archive")

    def test_an_inert_override_is_the_same_policy(self):
        """CORE-004: FULL ignores the generic size override, so it cannot change content."""
        archive = self._pack(fidelity_profile="full")
        reused, _ = self._reuses(archive, fidelity_profile="full", fidelity_max_mb=1)
        self.assertTrue(reused, "an override FULL ignores forced a pointless repack")

    def test_pattern_order_and_case_are_not_policy(self):
        archive = self._pack(fidelity_profile="standard", excludes=["*.md", "*.log"])
        reused, _ = self._reuses(
            archive, fidelity_profile="standard", excludes=["*.LOG", "*.md", "*.md"]
        )
        self.assertTrue(reused, "reordered/recased identical patterns forced a repack")

    def test_a_reused_archive_reports_the_policy_from_its_own_manifest(self):
        archive = self._pack(fidelity_profile="deep")
        reused, result = self._reuses(archive, fidelity_profile="deep")
        self.assertTrue(reused)
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))
        self.assertEqual(result.fidelity_profile, manifest["fidelity_profile"])
        self.assertEqual(result.archive_semantics, manifest["archive_semantics"])
        self.assertEqual(result.fidelity_profile, "deep")

    def test_a_legacy_archive_without_a_fingerprint_repacks_once(self):
        import os as _os

        from audapack.packing import MANIFEST_SCHEMA_VERSION

        archive = self.output_dir / "proj.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", "print('x')")
            zf.writestr(MANIFEST_FILENAME, json.dumps({
                # Current schema on purpose: the absent fingerprint must be the
                # reason this archive is refused, not a schema mismatch.
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "product": "AUDAPACK", "source_path": str(self.source),
                "fidelity_profile": "standard", "archive_semantics": "audit_representation",
            }))
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        _os.utime(archive, (newest + 120, newest + 120))

        reused, _ = self._reuses(archive, fidelity_profile="standard")
        self.assertFalse(reused, "an archive that cannot prove its policy was trusted")

    def test_an_unreadable_or_foreign_archive_is_never_trusted(self):
        import os as _os

        from audapack.packing import MANIFEST_SCHEMA_VERSION

        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        payloads = {
            "no manifest": None,
            "corrupt json": b"{not json",
            "future schema": json.dumps({
                "schema_version": MANIFEST_SCHEMA_VERSION + 1, "product": "AUDAPACK",
                "source_path": str(self.source), "policy_fingerprint": "x" * 32,
            }).encode("utf-8"),
            # PERF-003 reshaped media_inventory from a per-asset ledger into
            # per-group aggregates and bumped the schema for it. An archive
            # written under any superseded schema carries every key this gate
            # reads and is still not evidence about the current shape.
            "superseded schema": json.dumps({
                "schema_version": MANIFEST_SCHEMA_VERSION - 1, "product": "AUDAPACK",
                "source_path": str(self.source), "policy_fingerprint": "x" * 32,
            }).encode("utf-8"),
            "foreign product": json.dumps({
                "schema_version": MANIFEST_SCHEMA_VERSION, "product": "SOMETHING_ELSE",
                "source_path": str(self.source), "policy_fingerprint": "x" * 32,
            }).encode("utf-8"),
        }
        for label, payload in payloads.items():
            with self.subTest(archive=label):
                archive = self.output_dir / "proj.zip"
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr("main.py", "print('x')")
                    if payload is not None:
                        zf.writestr(MANIFEST_FILENAME, payload)
                _os.utime(archive, (newest + 120, newest + 120))
                reused, _ = self._reuses(archive, fidelity_profile="standard")
                self.assertFalse(reused, f"{label} was accepted as reuse evidence")

    def test_an_archive_built_from_another_source_is_refused(self):
        from audapack.fidelity import policy_fingerprint_from_config
        from audapack.packing import MANIFEST_SCHEMA_VERSION

        service = self._service(fidelity_profile="standard")
        archive = self.output_dir / "proj.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", "print('x')")
            zf.writestr(MANIFEST_FILENAME, json.dumps({
                "schema_version": MANIFEST_SCHEMA_VERSION, "product": "AUDAPACK",
                "source_path": str(self.root / "other_project"),
                "fidelity_profile": "standard", "archive_semantics": "audit_representation",
                "policy_fingerprint": policy_fingerprint_from_config(
                    service.config.packing, set(service.config.packing.excludes)
                ),
            }))
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        import os as _os
        _os.utime(archive, (newest + 120, newest + 120))

        reused, _ = self._reuses(archive, fidelity_profile="standard")
        self.assertFalse(reused, "an archive of a different project was reused")


class TestFreshnessCostsOneTraversal(unittest.TestCase):
    """PERF-001 (audit/6.md): deciding reuse walked the tree three times.

    Measured against HEAD on an unchanged 100-file Python project:
    FRESHNESS_COUNTS {'walks': 3, 'reads': 100} -- `scan_asset_references`
    walked and read every source file, `build_fidelity_plan` walked again to
    classify, and `ensure_fresh_archive` then walked a third time via
    `eligible_source_files`. All of it to conclude the archive was fresh.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.source = self.root / "proj"
        self.source.mkdir(parents=True)
        for index in range(40):
            (self.source / f"mod{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
        heavy = self.source / "node_modules" / "pkg"
        heavy.mkdir(parents=True)
        for index in range(20):
            (heavy / f"chunk{index}.js").write_text("y", encoding="utf-8")
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _service(self):
        from audapack.config import AppConfig, PackingConfig
        from audapack.models import Project
        from audapack.services.packing_service import PackingService

        config = AppConfig(packing=PackingConfig(
            output_dir=str(self.output_dir), delete_old=True, include_timestamp=False,
        ))
        config.projects = [Project(
            id="proj", display_name="proj", source_path=str(self.source), archive_name="proj",
        )]
        return PackingService(config, base_dir=self.root)

    def _fresh_archive(self) -> Path:
        import os as _os

        result = self._service().pack_project("proj")
        self.assertTrue(result.success, result.error_message)
        archive = Path(result.output_path)
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        _os.utime(archive, (newest + 120, newest + 120))
        return archive

    def _measure(self):
        import os as _os

        archive = self._fresh_archive()
        walks, reads, stats = [], [], []
        real_walk, real_stat = _os.walk, Path.stat
        real_scandir = _os.scandir
        real_text, real_bytes = Path.read_text, Path.read_bytes

        def counting_walk(top, *args, **kwargs):
            walks.append(str(top))
            return real_walk(top, *args, **kwargs)

        def counting_scandir(path, *args, **kwargs):
            # PERF-001: the plan walk is scandir-based, so a traversal of the
            # source root is one scandir call on it. The output directory is
            # scanned to find the archive and is not a source traversal.
            if str(path) == str(self.source):
                walks.append(str(path))
            return real_scandir(path, *args, **kwargs)

        def counting_stat(self, *args, **kwargs):
            stats.append(str(self))
            return real_stat(self, *args, **kwargs)

        def counting_text(self, *args, **kwargs):
            reads.append(str(self))
            return real_text(self, *args, **kwargs)

        def counting_bytes(self, *args, **kwargs):
            reads.append(str(self))
            return real_bytes(self, *args, **kwargs)

        with patch.object(_os, "walk", side_effect=counting_walk), \
                patch.object(_os, "scandir", side_effect=counting_scandir), \
                patch.object(Path, "stat", autospec=True, side_effect=counting_stat), \
                patch.object(Path, "read_text", autospec=True, side_effect=counting_text), \
                patch.object(Path, "read_bytes", autospec=True, side_effect=counting_bytes):
            result = self._service().ensure_fresh_archive("proj")

        self.assertTrue(result.success)
        self.assertEqual(result.output_path, archive, "the fresh archive was not reused")
        return walks, reads, stats

    def test_one_traversal_no_source_reads_and_no_excluded_metadata(self):
        walks, reads, stats = self._measure()
        self.assertEqual(len(walks), 1, f"the tree was traversed {len(walks)} times: {walks}")
        source_reads = [path for path in reads if str(self.source) in path]
        self.assertEqual(source_reads, [], f"source text was parsed to decide reuse: {source_reads[:3]}")
        touched = [path for path in stats if "node_modules" in path]
        self.assertEqual(touched, [], f"excluded weight was stat'ed: {touched[:3]}")

    def test_a_files_metadata_comes_from_the_directory_read(self):
        """PERF-001: scandir already described every entry; re-stat'ing it is waste.

        Measured before this layer: classifying a 40-file tree cost 2 extra
        syscalls per entry -- ``is_symlink()`` then ``stat()`` on a freshly built
        ``Path`` -- because ``os.walk`` throws its ``DirEntry`` objects away.
        """
        _walks, _reads, stats = self._measure()
        per_file = [path for path in stats if path.endswith(".py") or path.endswith(".js")]
        self.assertEqual(
            per_file, [],
            f"per-file metadata was re-read after the directory listing: {per_file[:3]}",
        )

    def test_the_archive_manifest_is_read_once(self):
        """PERF-001: the policy gate and the reused result share one manifest read.

        CORE-006 made reuse ask the archive what policy built it, and the reused
        ``PackResult`` reports the same fields -- two independent
        ``read_archive_manifest`` calls parsing the same zip central directory,
        which profiling showed dominating a reuse decision on a small tree.
        """
        import zipfile as _zipfile

        archive = self._fresh_archive()
        opens = []
        real_zipfile = _zipfile.ZipFile

        def counting_zipfile(file, *args, **kwargs):
            opens.append(str(file))
            return real_zipfile(file, *args, **kwargs)

        with patch.object(_zipfile, "ZipFile", side_effect=counting_zipfile):
            result = self._service().ensure_fresh_archive("proj")

        self.assertTrue(result.success)
        self.assertEqual(result.output_path, archive, "the fresh archive was not reused")
        archive_opens = [path for path in opens if path == str(archive)]
        self.assertEqual(
            len(archive_opens), 1,
            f"the archive was opened {len(archive_opens)} times to read one manifest",
        )

    def test_a_changed_included_file_still_repacks(self):
        import os as _os

        archive = self._fresh_archive()
        target = self.source / "mod7.py"
        stamp = archive.stat().st_mtime + 300
        target.write_text("x = 999\n", encoding="utf-8")
        _os.utime(target, (stamp, stamp))

        service = self._service()
        repacked = []
        service.pack_project = lambda project_id, **kw: repacked.append(project_id) or PackResult(
            project_id=project_id, name=project_id, source_path=str(self.source), success=True,
        )
        service.ensure_fresh_archive("proj")
        self.assertEqual(repacked, ["proj"], "a changed included file did not invalidate the archive")

    def test_media_sampling_still_sees_referenced_assets(self):
        """PERF-001 deferred the reference scan; it must still run where media exists."""
        from audapack.fidelity import build_fidelity_plan

        assets = self.source / "assets"
        assets.mkdir()
        for index in range(6):
            (assets / f"img{index}.png").write_bytes(b"\x89PNG" + b"z" * 2048)
        (self.source / "app.py").write_text("ICON = 'assets/img5.png'\n", encoding="utf-8")

        plan = build_fidelity_plan(self.source, profile="compact", excludes=set())
        self.assertIn("assets/img5.png", plan.referenced_files)
        decision = plan.decisions.get("assets/img5.png")
        self.assertIsNotNone(decision, "the referenced asset has no decision")
        self.assertTrue(decision.include, "a by-name referenced asset was sampled out")

    def test_create_zip_resolves_decisions_by_exact_case_identity(self):
        """T-150: the packer must tell Asset.PNG from asset.png.

        The old lookup lowercased the walked path before asking the plan, so
        on a case-sensitive filesystem the two physical twins shared one
        decision. Requires a filesystem that can hold case-distinct siblings.
        """
        import subprocess
        import sys

        source = Path(self.temp_dir) / "casetree"
        source.mkdir()
        if sys.platform == "win32":
            proc = subprocess.run(
                ["fsutil", "file", "setCaseSensitiveInfo", str(source), "enable"],
                capture_output=True,
            )
            if proc.returncode != 0:
                self.skipTest("per-directory case sensitivity unavailable (fsutil refused)")
        try:
            (source / "Asset.PNG").write_bytes(b"A" * 300_000)
            (source / "asset.png").write_bytes(b"B" * 300_000)
        except OSError:
            self.skipTest("filesystem cannot hold case-distinct siblings")
        if not ((source / "Asset.PNG").is_file() and (source / "asset.png").is_file()):
            self.skipTest("filesystem collapsed case-distinct siblings into one file")
        (source / "main.py").write_text("print('x')", encoding="utf-8")

        plan = build_fidelity_plan(source, set(), profile="compact")
        self.assertEqual(len(plan.decisions), 3, "the twins collapsed into one decision")
        output_zip = Path(self.temp_dir) / "case.zip"
        stats = create_zip(source, output_zip, set(), plan=plan)
        with zipfile.ZipFile(output_zip) as zf:
            names = set(zf.namelist())
        included = "Asset.PNG" if plan.decision_for("Asset.PNG").include else "asset.png"
        excluded = "asset.png" if included == "Asset.PNG" else "Asset.PNG"
        self.assertIn(included, names)
        self.assertNotIn(excluded, names)
        self.assertIn("main.py", names)
        self.assertEqual(stats.files_included, plan.included)
        self.assertEqual(stats.files_excluded, plan.excluded)


class TestFreshnessProbeStopsEarly(unittest.TestCase):
    """T-155 (PERF-004 residue): the freshness preflight is a probe, not a walk.

    Three costs the probe used to pay for an answer it already had. First, it
    decided staleness only after traversing the whole tree, although the first
    priority-1 included file newer than the archive settles the verdict. Second,
    it never looked at the caller's cancel_event, so a cancelled operation kept
    paying for a full traversal. Third, a stale verdict made pack_project walk
    the tree a second time (a fresh plan build), although the probe's own plan
    already holds every decision the pack needs.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.source = self.root / "proj"
        self.source.mkdir(parents=True)
        for index in range(40):
            (self.source / f"mod{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
        heavy = self.source / "node_modules" / "pkg"
        heavy.mkdir(parents=True)
        for index in range(20):
            (heavy / f"chunk{index}.js").write_text("y", encoding="utf-8")
        self.deep = self.source / "deep" / "deeper"
        self.deep.mkdir(parents=True)
        for index in range(5):
            (self.deep / f"f{index}.py").write_text("d", encoding="utf-8")
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _service(self):
        from audapack.config import AppConfig, PackingConfig
        from audapack.models import Project
        from audapack.services.packing_service import PackingService

        config = AppConfig(packing=PackingConfig(
            output_dir=str(self.output_dir), delete_old=True, include_timestamp=False,
        ))
        config.projects = [Project(
            id="proj", display_name="proj", source_path=str(self.source), archive_name="proj",
        )]
        return PackingService(config, base_dir=self.root)

    def _fresh_archive(self) -> Path:
        import os as _os

        result = self._service().pack_project("proj")
        self.assertTrue(result.success, result.error_message)
        archive = Path(result.output_path)
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        _os.utime(archive, (newest + 120, newest + 120))
        return archive

    def _scandir_probe(self):
        """A counting scandir plus the paths it saw under the source root."""
        import os as _os

        scans = []
        real_scandir = _os.scandir

        def counting_scandir(path, *args, **kwargs):
            scans.append(str(path))
            return real_scandir(path, *args, **kwargs)

        return scans, counting_scandir

    def test_the_probe_stops_at_the_first_newer_priority_one_file(self):
        import os as _os

        self._fresh_archive()
        target = self.source / "mod3.py"
        stamp = self.output_dir.joinpath("proj.zip").stat().st_mtime + 300
        _os.utime(target, (stamp, stamp))

        scans, counting_scandir = self._scandir_probe()
        pre_pack_scans = []

        service = self._service()
        real_pack = service.pack_project

        def spy_pack(project_id, **kwargs):
            # Runs the moment the probe has its verdict -- whatever it walked
            # up to here is everything the probe paid for.
            pre_pack_scans.extend(scans)
            return real_pack(project_id, **kwargs)

        service.pack_project = spy_pack
        with patch.object(_os, "scandir", side_effect=counting_scandir):
            service.ensure_fresh_archive("proj")

        descended = [p for p in pre_pack_scans if str(self.deep) in p or "deep" in p]
        self.assertEqual(
            descended, [],
            "the probe kept walking after a priority-1 include newer than the archive "
            f"had already settled the verdict: {descended[:3]}",
        )

    def test_a_cancel_during_the_probe_returns_promptly(self):
        import os as _os

        self._fresh_archive()
        scans, counting_scandir = self._scandir_probe()

        cancel = threading.Event()

        def cancelling_scandir(path, *args, **kwargs):
            result = counting_scandir(path, *args, **kwargs)
            # The cancel lands while the probe is standing on the root: every
            # further traversal is work a cancelled operation never asked for.
            if str(path) == str(self.source):
                cancel.set()
            return result

        packed = []
        service = self._service()
        service.pack_project = lambda project_id, **kw: packed.append(project_id) or PackResult(
            project_id=project_id, name=project_id, source_path=str(self.source), success=True,
        )
        with patch.object(_os, "scandir", side_effect=cancelling_scandir):
            result = service.ensure_fresh_archive("proj", cancel_event=cancel)

        self.assertFalse(result.success, "a cancelled probe reported a usable archive")
        self.assertIn("cancel", result.error_message.lower())
        self.assertEqual(packed, [], "a cancelled probe started a pack")
        descended = [p for p in scans if p != str(self.source) and str(self.source) in p]
        self.assertEqual(
            descended, [],
            f"the probe kept traversing after the cancel landed: {descended[:3]}",
        )

    def test_a_stale_verdict_does_not_rewalk_the_tree(self):
        """The probe's complete stale plan IS the pack plan: one decision walk."""
        import os as _os

        assets = self.source / "assets"
        assets.mkdir()
        for index in range(6):
            (assets / f"img{index}.png").write_bytes(b"\x89PNG" + b"z" * 2048)
        (self.source / "app.py").write_text("ICON = 'assets/img5.png'\n", encoding="utf-8")

        service = self._service()
        packed = service.pack_project("proj")
        self.assertTrue(packed.success, packed.error_message)
        archive = Path(packed.output_path)
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        _os.utime(archive, (newest + 120, newest + 120))
        # A referenced (protected, priority-1) asset is now newer than the
        # archive: stale, but only decidable after the walk completes, so the
        # probe's plan is complete and reusable.
        stamp = archive.stat().st_mtime + 300
        _os.utime(assets / "img5.png", (stamp, stamp))

        plan_builds = []
        import audapack.fidelity as fidelity_mod
        import audapack.freshness as freshness_mod
        real_build = fidelity_mod.build_plan_from_config

        def counting_build(*args, **kwargs):
            plan_builds.append(args)
            return real_build(*args, **kwargs)

        # The canonical name is kept, so the repack is proven by the manifest's
        # own creation stamp, not the file mtime (include_timestamp=False).
        with zipfile.ZipFile(archive) as zf:
            seed_created = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))["created_at"]

        # Both bindings: PERF-002 (audit/9.md) moved the probe's binding into
        # the canonical freshness module, pack_single imports it straight from
        # fidelity -- counting only one would let the second decision walk hide.
        with patch.object(freshness_mod, "build_plan_from_config", side_effect=counting_build), \
                patch.object(fidelity_mod, "build_plan_from_config", side_effect=counting_build):
            result = service.ensure_fresh_archive("proj")

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(
            len(plan_builds), 1,
            f"the tree was walked {len(plan_builds)} times to decide and pack: "
            "a stale verdict must reuse the probe's plan",
        )
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))
        self.assertGreater(
            manifest["created_at"], seed_created,
            "a stale verdict did not repack",
        )
        pruned = {entry["rel"]: entry for entry in manifest["pruned_directories"]}
        node_modules = pruned.get("node_modules")
        self.assertIsNotNone(node_modules, "the manifest lost the node_modules prune")
        self.assertEqual(
            node_modules["files"], 20,
            "the census was not completed on the reused plan",
        )
        self.assertIn("assets/img5.png", names, "the newer referenced asset was not packed")


def _noop():
    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    return _C()


class TestCompressionIsChosenPerFileType(unittest.TestCase):
    """PERF-002 (audit/6.md): create_zip forced Deflate on precompressed bytes.

    Measured on a 32 MiB payload that models already-compressed media: DEFLATED
    write 0.698 s producing 33,564,786 bytes, against ZIP_STORED 0.029 s
    producing 33,554,546 bytes -- 23.9x slower for an archive 0.03% LARGER,
    with `testzip` clean either way. T-147 made media inclusion a profile
    decision, so a STANDARD/DEEP/FULL archive now routinely carries exactly the
    payloads Deflate cannot shrink.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.source = self.root / "proj"
        self.source.mkdir(parents=True)
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _methods(self, out_zip: Path) -> dict:
        with zipfile.ZipFile(out_zip, "r") as zf:
            return {info.filename: info.compress_type for info in zf.infolist()}

    def test_precompressed_media_is_stored_and_text_stays_deflated(self):
        # Incompressible-by-construction bytes: a real Deflate attempt on these
        # is pure CPU, which is the whole cost this policy removes.
        payload = os.urandom(64 * 1024)
        (self.source / "clip.mp4").write_bytes(payload)
        (self.source / "photo.jpg").write_bytes(payload)
        (self.source / "icon.png").write_bytes(payload)
        (self.source / "face.woff2").write_bytes(payload)
        (self.source / "main.py").write_text("print('x')\n" * 500, encoding="utf-8")
        (self.source / "data.json").write_text('{"a": 1}\n' * 500, encoding="utf-8")

        out_zip = self.output_dir / "proj.zip"
        stats = create_zip(self.source, out_zip, set(), manifest_meta={"project_name": "proj"})
        methods = self._methods(out_zip)

        for name in ("clip.mp4", "photo.jpg", "icon.png", "face.woff2"):
            self.assertEqual(
                methods[name], zipfile.ZIP_STORED,
                f"{name} was deflated: precompressed bytes paid for a pointless compression pass",
            )
        for name in ("main.py", "data.json", MANIFEST_FILENAME):
            self.assertEqual(
                methods[name], zipfile.ZIP_DEFLATED,
                f"{name} was stored: compressible text must still be compressed",
            )
        # GUARDRAIL: integrity is unchanged by the storage method.
        self.assertEqual(verify_zip(out_zip, stats.files_added), stats.files_added)

    def test_raw_audio_is_still_deflated(self):
        """GUARDRAIL: 'media' is not 'incompressible'. WAV/PCM gains from Deflate."""
        # Silent PCM: the pathological case for storing media blindly.
        (self.source / "tone.wav").write_bytes(b"RIFF" + b"\x00" * (64 * 1024))
        (self.source / "raw.aiff").write_bytes(b"FORM" + b"\x00" * (64 * 1024))

        out_zip = self.output_dir / "proj.zip"
        create_zip(self.source, out_zip, set())
        methods = self._methods(out_zip)

        for name in ("tone.wav", "raw.aiff"):
            self.assertEqual(
                methods[name], zipfile.ZIP_DEFLATED,
                f"{name} was stored, throwing away the compression raw audio actually gets",
            )
        with zipfile.ZipFile(out_zip, "r") as zf:
            info = zf.getinfo("tone.wav")
            self.assertLess(
                info.compress_size, info.file_size // 2,
                "silent PCM did not shrink, so Deflate was not applied to it",
            )

    def test_the_policy_is_one_deterministic_function(self):
        """PERF-002: centralized, so no writing branch can drift from another."""
        from audapack.packing import compress_type_for

        self.assertEqual(compress_type_for("a/b/CLIP.MP4"), zipfile.ZIP_STORED)
        self.assertEqual(compress_type_for("a\\b\\clip.mp4"), zipfile.ZIP_STORED)
        self.assertEqual(compress_type_for("tone.wav"), zipfile.ZIP_DEFLATED)
        # No extension, and a leading-dot name, are not extensions to match on.
        self.assertEqual(compress_type_for("Makefile"), zipfile.ZIP_DEFLATED)
        self.assertEqual(compress_type_for(".mp4"), zipfile.ZIP_DEFLATED)
        # An unknown type keeps the historical behaviour rather than guessing.
        self.assertEqual(compress_type_for("payload.bin"), zipfile.ZIP_DEFLATED)

    def test_a_single_file_pack_uses_the_same_policy(self):
        """The single-file branch is a separate writer; it must not drift."""
        clip = self.root / "solo.mp4"
        clip.write_bytes(os.urandom(64 * 1024))
        out_zip = self.output_dir / "solo.zip"
        create_zip(clip, out_zip, set())
        self.assertEqual(self._methods(out_zip)["solo.mp4"], zipfile.ZIP_STORED)

    def test_archive_semantics_do_not_change_with_the_storage_method(self):
        """GUARDRAIL: storage method is a CPU decision, never a fidelity claim."""
        (self.source / "main.py").write_text("print('x')\n", encoding="utf-8")
        (self.source / "clip.mp4").write_bytes(os.urandom(32 * 1024))

        out_zip = self.output_dir / "proj.zip"
        plan = build_fidelity_plan(self.source, profile="full", excludes=set())
        stats = create_zip(
            self.source, out_zip, set(), manifest_meta={"project_name": "proj"}, plan=plan,
        )
        with zipfile.ZipFile(out_zip, "r") as zf:
            manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))

        self.assertEqual(manifest["archive_semantics"], "full_snapshot")
        self.assertEqual(manifest["files_excluded"], 0)
        self.assertTrue(manifest["accounting_reconciled"], manifest.get("accounting_error"))
        self.assertEqual(self._methods(out_zip)["clip.mp4"], zipfile.ZIP_STORED)
        self.assertEqual(stats.files_failed, 0)



class ReservedControlInNonGitSources(unittest.TestCase):
    """SRC-100: a reserved control name must behave the same in every mode.

    Git mode refused (or superseded) a tracked ``.audapack/manifest.json``.
    The walk and plan modes used to include it as ordinary payload, and the
    writer then emitted its own manifest under the same name: zipfile allowed
    the duplicate with a UserWarning, and the pack died as FAILED_VERIFY with
    nothing naming the cause. A non-Git source must fail closed with the SAME
    named classification instead.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_dir = self.tmp / "out"
        self.output_dir.mkdir()
        self.source = self.tmp / "proj"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pack(self, payload):
        (self.source / ".audapack").mkdir(parents=True, exist_ok=True)
        (self.source / "app.py").write_text("print('x')", encoding="utf-8")
        (self.source / ".audapack" / "manifest.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return packing.pack_single(
            source_path=self.source,
            output_dir=self.output_dir,
            archive_stem="proj",
            excludes=set(),
            delete_old=False,
            include_timestamp=False,
        )

    def test_a_project_owned_reserved_name_refuses_with_a_named_code(self):
        result = self._pack({"mine": True})
        self.assertFalse(result.success)
        self.assertEqual(result.status, packing.PACK_STATUS_FAILED_INVENTORY)
        # SRC-100: the code no longer claims TRACKED for a source that has no
        # source control at all. The sentence below already said "Source path"
        # -- only the classification was still pointing an operator at a
        # repository that is not involved.
        self.assertEqual(
            result.error_code, CODE_RESERVED_ARCHIVE_NAME_CONFLICT, result.error_message
        )
        # Not the opaque FAILED_VERIFY this replaced, and the sentence must not
        # tell a filesystem-mode operator to touch source control.
        self.assertNotEqual(result.status, packing.PACK_STATUS_FAILED_VERIFY)
        self.assertNotIn("Tracked source path", result.error_message)
        self.assertIn("Source path", result.error_message)
        self.assertEqual(list(self.output_dir.glob("*.zip")), [])

    def test_an_unmarked_reserved_name_also_refuses(self):
        result = self._pack({"hello": "world"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, CODE_RESERVED_ARCHIVE_NAME_CONFLICT)

    def test_a_generated_control_is_superseded_and_never_duplicated(self):
        result = self._pack({"schema_version": 1, "kind": "audapack_source_inventory", "files": []})
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
        self.assertEqual(
            [n for n in names if n == ".audapack/manifest.json"].__len__(), 1,
            f"the archive carried the reserved member more than once: {names}",
        )
        self.assertIn("app.py", names)

    def test_no_duplicate_member_ever_reaches_the_archive(self):
        for payload in ({"mine": True}, {"hello": "world"},
                        {"schema_version": 1, "kind": "audapack_source_inventory", "files": []}):
            with self.subTest(payload=payload):
                result = self._pack(payload)
                if not result.success:
                    self.assertEqual(list(self.output_dir.glob("*.zip")), [])
                    continue
                with zipfile.ZipFile(result.output_path) as zf:
                    names = zf.namelist()
                self.assertEqual(len(names), len(set(names)), f"duplicate members in {names}")
