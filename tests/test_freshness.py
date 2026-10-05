"""PERF-002 (audit/9.md): one canonical archive-freshness contract.

Project Room used to own a SECOND freshness algorithm -- a raw ``os.walk`` that
applied neither packing excludes nor fidelity inclusion decisions -- and carried
its answer in a boolean named ``source_older`` whose True the producer set for
"source is older" and the delegate rendered as "source changed since the pack".
These tests pin the replacement: an explicit FRESH/STALE/UNKNOWN tri-state
produced by the same policy the packer runs.
"""

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from audapack.config import AppConfig, PackingConfig
from audapack.fidelity import policy_fingerprint_from_config
from audapack.freshness import ArchiveFreshness, probe_archive_freshness
from audapack.models import Project
from audapack.packing import MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION


class ArchiveFreshnessContractTests(unittest.TestCase):
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

    # ------------------------------------------------------------------ fixtures
    def _config(self, **packing):
        config = AppConfig(packing=PackingConfig(
            output_dir=str(self.output_dir), delete_old=True, **packing,
        ))
        config.projects = [self._project()]
        return config

    def _project(self):
        return Project(
            id="proj", display_name="proj",
            source_path=str(self.source), archive_name="proj",
        )

    def _pack(self, config=None):
        from audapack.services.packing_service import PackingService

        config = config or self._config()
        result = PackingService(config, base_dir=self.root).pack_project("proj")
        self.assertTrue(result.success, result.error_message)
        return Path(result.output_path)

    def _probe(self, config=None, **kwargs):
        config = config or self._config()
        return probe_archive_freshness(
            self._project(), config.packing, fallback_dir=self.root, **kwargs,
        )

    def _touch(self, path: Path, when: float):
        os.utime(path, (when, when))

    def _freeze_root_older_than(self, archive: Path):
        """The source ROOT mtime shortcut must not decide these cases for us."""
        stamp = archive.stat().st_mtime - 120
        self._touch(self.source, stamp)

    # ------------------------------------------------------------------ FRESH
    def test_an_unchanged_tree_is_fresh(self):
        archive = self._pack()
        self._freeze_root_older_than(archive)
        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.FRESH)
        self.assertFalse(verdict.repack_required)
        self.assertEqual(verdict.archive_path, archive)

    def test_newer_node_modules_does_not_make_the_archive_stale(self):
        """The exact reproduction from the audit: node_modules/cache.js."""
        archive = self._pack()
        noise = self.source / "node_modules"
        noise.mkdir()
        cache = noise / "cache.js"
        cache.write_text("generated", encoding="utf-8")
        stamp = archive.stat().st_mtime + 600
        self._touch(cache, stamp)
        self._touch(noise, archive.stat().st_mtime - 60)
        self._freeze_root_older_than(archive)

        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.FRESH, verdict.reason)

    def test_newer_cache_and_venv_material_does_not_make_the_archive_stale(self):
        archive = self._pack()
        stamp = archive.stat().st_mtime + 600
        for name, child in (("__pycache__", "mod.pyc"), (".venv", "pyvenv.cfg")):
            directory = self.source / name
            directory.mkdir()
            member = directory / child
            member.write_text("x", encoding="utf-8")
            self._touch(member, stamp)
            self._touch(directory, archive.stat().st_mtime - 60)
        self._freeze_root_older_than(archive)

        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.FRESH, verdict.reason)

    def test_sampled_out_media_newer_than_the_archive_does_not_force_stale(self):
        """Fidelity sampling drops unreferenced media; dropped material cannot
        invalidate the archive it is not in."""
        media = self.source / "assets"
        media.mkdir()
        for index in range(8):
            (media / f"clip{index}.png").write_bytes(b"\x89PNG" + b"z" * 4096)
        config = self._config(fidelity_profile="STANDARD", fidelity_media_samples=1)
        archive = self._pack(config)

        with zipfile.ZipFile(archive) as zf:
            packed = {name for name in zf.namelist()}
        dropped = [
            path for path in sorted(media.glob("clip*.png"))
            if f"assets/{path.name}" not in packed
        ]
        self.assertTrue(dropped, "the fixture did not exercise media sampling")

        stamp = archive.stat().st_mtime + 600
        for path in dropped:
            self._touch(path, stamp)
        self._touch(media, archive.stat().st_mtime - 60)
        self._freeze_root_older_than(archive)

        verdict = probe_archive_freshness(
            self._project(), config.packing, fallback_dir=self.root,
        )
        self.assertIs(verdict.state, ArchiveFreshness.FRESH, verdict.reason)

    # ------------------------------------------------------------------ STALE
    def test_a_newer_included_file_is_stale(self):
        archive = self._pack()
        stamp = archive.stat().st_mtime + 600
        self._touch(self.source / "main.py", stamp)
        self._freeze_root_older_than(archive)

        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.STALE)
        self.assertTrue(verdict.repack_required)

    def test_a_changed_policy_fingerprint_is_stale(self):
        archive = self._pack()
        self._freeze_root_older_than(archive)
        self.assertIs(self._probe().state, ArchiveFreshness.FRESH)

        # Same bytes on disk, different active packing policy.
        changed = self._config(fidelity_profile="COMPACT")
        verdict = probe_archive_freshness(
            self._project(), changed.packing, fallback_dir=self.root,
        )
        self.assertIs(verdict.state, ArchiveFreshness.STALE)
        self.assertTrue(verdict.policy_mismatch)
        self.assertTrue(verdict.repack_required)
        self.assertTrue(archive.exists(), "a read-only probe must not touch the archive")

    def test_a_missing_archive_is_stale_not_unknown(self):
        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.STALE)
        self.assertEqual(verdict.reason, "no archive")
        self.assertTrue(verdict.repack_required)

    # ------------------------------------------------------------------ UNKNOWN
    def test_an_exhausted_budget_is_unknown_never_fresh(self):
        archive = self._pack()
        self._freeze_root_older_than(archive)
        verdict = self._probe(budget_s=0.0)
        self.assertIs(verdict.state, ArchiveFreshness.UNKNOWN)
        self.assertTrue(verdict.cancelled)
        self.assertFalse(verdict.repack_required, "a cancelled probe never orders a pack")

    def test_an_incomplete_traversal_is_unknown_never_fresh(self):
        archive = self._pack()
        sub = self.source / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("x", encoding="utf-8")
        self._touch(sub, archive.stat().st_mtime - 60)
        self._freeze_root_older_than(archive)

        real_scandir = os.scandir

        def failing_scandir(path, *args, **kwargs):
            if str(path).endswith("pkg"):
                raise OSError(13, "permission denied")
            return real_scandir(path, *args, **kwargs)

        import audapack.fidelity as fidelity_mod

        original = fidelity_mod.os.scandir
        fidelity_mod.os.scandir = failing_scandir
        try:
            verdict = self._probe()
        finally:
            fidelity_mod.os.scandir = original

        self.assertIs(verdict.state, ArchiveFreshness.UNKNOWN, verdict.reason)
        self.assertTrue(
            verdict.repack_required,
            "an archive that may be missing changed files must not be reused",
        )

    def test_a_missing_source_is_unknown(self):
        shutil.rmtree(self.source)
        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.UNKNOWN)

    # ------------------------------------------------------------------ read-only
    def test_the_probe_never_packs(self):
        archive = self._pack()
        before = archive.read_bytes()
        stamp = archive.stat().st_mtime + 600
        self._touch(self.source / "main.py", stamp)
        self._freeze_root_older_than(archive)

        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.STALE)
        self.assertEqual(
            sorted(path.name for path in self.output_dir.iterdir()), [archive.name],
            "the read-only probe created an archive",
        )
        self.assertEqual(archive.read_bytes(), before, "the read-only probe rewrote the archive")

    def test_the_packer_and_the_ui_probe_agree(self):
        """PERF-002 GUARDRAIL: one contract, so the two callers cannot drift."""
        from audapack.services.packing_service import PackingService

        config = self._config()
        archive = self._pack(config)
        self._freeze_root_older_than(archive)

        service = PackingService(config, base_dir=self.root)
        packed = []
        service.pack_project = lambda project_id, **kw: packed.append(project_id)

        # Fresh: ensure_fresh_archive reuses, the probe says FRESH.
        result = service.ensure_fresh_archive("proj")
        self.assertTrue(result.success)
        self.assertEqual(packed, [])
        self.assertIs(self._probe(config).state, ArchiveFreshness.FRESH)

        # Stale: ensure_fresh_archive packs, the probe says STALE.
        stamp = archive.stat().st_mtime + 600
        self._touch(self.source / "main.py", stamp)
        self._freeze_root_older_than(archive)
        service.ensure_fresh_archive("proj")
        self.assertEqual(packed, ["proj"])
        self.assertIs(self._probe(config).state, ArchiveFreshness.STALE)

    def test_a_legacy_archive_without_a_manifest_is_stale(self):
        """No manifest means no provable policy identity."""
        archive = self.output_dir / "proj.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", "print('x')")
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        self._touch(archive, newest + 600)

        verdict = self._probe()
        self.assertIs(verdict.state, ArchiveFreshness.STALE)
        self.assertTrue(verdict.policy_mismatch)

    def test_a_matching_manifest_keeps_the_reused_policy_metadata(self):
        config = self._config()
        archive = self.output_dir / "proj.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", "print('x')")
            zf.writestr(MANIFEST_FILENAME, json.dumps({
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "product": "AUDAPACK",
                "source_path": str(self.source),
                "fidelity_profile": config.packing.fidelity_profile,
                "archive_semantics": "full_snapshot",
                "policy_fingerprint": policy_fingerprint_from_config(
                    config.packing, set(config.packing.excludes)
                ),
            }))
        newest = max(p.stat().st_mtime for p in self.source.rglob("*") if p.is_file())
        self._touch(archive, newest + 600)
        self._freeze_root_older_than(archive)

        verdict = self._probe(config)
        self.assertIs(verdict.state, ArchiveFreshness.FRESH)
        self.assertEqual(verdict.manifest.get("archive_semantics"), "full_snapshot")


if __name__ == "__main__":
    unittest.main()
