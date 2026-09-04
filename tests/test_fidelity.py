"""T-147: audit-fidelity profiles -- engine, sampling, invariant, overrides, manifest.

The ticket's core demand: an archive is either an honest *audit representation*
(COMPACT/STANDARD/DEEP) or a *full snapshot* (FULL). No file ever silently
disappears: ``discovered == included + excluded + failed`` must hold, every
exclusion carries a reason category, and the manifest declares the profile and
archive semantics so an auditor can tell the two apart at a glance.
"""

import json
import shutil
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from audapack import fidelity
from audapack.config import PackingConfig
from audapack.fidelity import (
    PROFILE_COMPACT,
    PROFILE_DEEP,
    PROFILE_FULL,
    PROFILE_STANDARD,
    REASON_CONFIGURED_IGNORE,
    REASON_MEDIA_BUDGET,
    REASON_SECRET_POLICY,
    REASON_SIZE_LIMIT,
    REASON_UNSUPPORTED,
    _priority_for,
    archive_semantics_for,
    build_fidelity_plan,
    build_plan_from_config,
    normalize_fidelity_profile,
    profile_budget_bytes,
)
from audapack.packing import MANIFEST_FILENAME, pack_single


class FidelityBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source = Path(self.temp_dir) / "proj"
        self.source.mkdir(parents=True)
        self.output_dir = Path(self.temp_dir) / "out"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write(self, rel: str, size: int = 16, prefix: bytes = b"x"):
        p = self.source / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((prefix * (size // len(prefix) + 1))[:size])
        return p

    def _media_dir(self, name: str, count: int, size: int = 700_000, ext: str = "wav"):
        d = self.source / name
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (d / f"{name}_{i}.{ext}").write_bytes(b"\x00" * size)
        return d

    def _included_media(self, plan, ext: str) -> list:
        return [
            rel for rel, d in plan.decisions.items()
            if d.include and rel.endswith("." + ext)
        ]

    def _assert_invariant(self, plan):
        self.assertEqual(
            plan.discovered,
            plan.included + plan.excluded + plan.failed,
            "discovered == included + excluded + failed must hold",
        )


class TestProfileSemantics(FidelityBase):
    def test_normalize_and_semantics(self):
        self.assertEqual(normalize_fidelity_profile("STANDARD"), PROFILE_STANDARD)
        self.assertEqual(normalize_fidelity_profile("bogus"), PROFILE_STANDARD)
        self.assertEqual(normalize_fidelity_profile(""), PROFILE_STANDARD)
        self.assertEqual(normalize_fidelity_profile(None), PROFILE_STANDARD)
        self.assertEqual(normalize_fidelity_profile("FULL"), PROFILE_FULL)
        self.assertEqual(archive_semantics_for(PROFILE_FULL), "full_snapshot")
        for p in (PROFILE_COMPACT, PROFILE_STANDARD, PROFILE_DEEP):
            self.assertEqual(archive_semantics_for(p), "audit_representation")

    def test_budget_order(self):
        compact = profile_budget_bytes(PROFILE_COMPACT)
        standard = profile_budget_bytes(PROFILE_STANDARD)
        deep = profile_budget_bytes(PROFILE_DEEP)
        self.assertLessEqual(compact, 10 * 1024 * 1024)
        self.assertLessEqual(standard, 30 * 1024 * 1024)
        self.assertLessEqual(deep, 100 * 1024 * 1024)
        self.assertEqual(profile_budget_bytes(PROFILE_FULL), 0)
        # override wins over profile default
        self.assertEqual(profile_budget_bytes(PROFILE_STANDARD, override_mb=15), 15 * 1024 * 1024)


class TestMediaSampling(FidelityBase):
    def test_standard_samples_media_dir_but_inventory_sees_every_file(self):
        self._write("main.py", 64)
        self._write("README.md", 64)
        self._media_dir("Sounds", 6, size=700_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        # every discovered file accounted
        self.assertEqual(plan.discovered, 8)  # 2 text + 6 wav
        # STANDARD keeps up to ~3 representative files per media dir (the byte
        # budget can extend it, but 700KB x 6 exceeds the 2MB/dir cap after 3).
        kept = self._included_media(plan, "wav")
        self.assertGreaterEqual(len(kept), 1)
        self.assertLessEqual(len(kept), 3)
        # inventory records ALL 6 with included flags
        inv = plan.media_inventory.get("Sounds")
        self.assertIsNotNone(inv)
        self.assertEqual(inv["total"], 6)
        self.assertEqual(len(inv["files"]), 6)
        self.assertEqual(inv["included"], len(kept))
        # excluded media carry the media_budget reason
        budgeted = [d for d in plan.decisions.values() if d.reason == REASON_MEDIA_BUDGET]
        self.assertEqual(len(budgeted), 6 - len(kept))
        # no silent omission: excluded rels are in extra_excluded_rel too
        self.assertEqual(len(plan.extra_excluded_rel), len(budgeted))

    def test_deep_keeps_every_media_file(self):
        self._write("main.py", 64)
        self._media_dir("Video", 4, size=300_000, ext="mp4")
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_DEEP)
        self._assert_invariant(plan)
        self.assertEqual(plan.excluded, 0)
        self.assertEqual(plan.included, 5)

    def test_full_keeps_everything_no_sampling(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 4)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_FULL)
        self._assert_invariant(plan)
        self.assertEqual(plan.excluded, 0)
        self.assertEqual(plan.included, 5)

    def test_referenced_media_outranks_the_sample_cap(self):
        # build.ps1 references Sounds/*.wav -- those must be prioritized above
        # arbitrary unreferenced media even under the sampling cap.
        self._write("main.py", 64)
        self._media_dir("Sounds", 8, size=700_000)
        (self.source / "build.ps1").write_text(
            "Copy-Item Sounds/sounds_0.wav $out", encoding="utf-8"
        )
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        # decisions keys are lowercased; referenced_files keeps original case
        ref_decision = plan.decisions.get("sounds/sounds_0.wav")
        self.assertIsNotNone(ref_decision)
        self.assertTrue(ref_decision.include, "referenced wav must be included")
        self.assertIn("Sounds/sounds_0.wav", plan.referenced_files)

    def test_compact_samples_more_aggressively(self):
        self._write("main.py", 64)
        self._media_dir("Images", 5, size=700_000, ext="png")
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT)
        self._assert_invariant(plan)
        kept = self._included_media(plan, "png")
        self.assertEqual(len(kept), 1)  # COMPACT keeps ~1 per dir


class TestPriorityLadder(FidelityBase):
    """T-147: only code/config/tests are untrimmable; prose is second, bulk last.

    Measured on FastPrompter before the ladder existed: 45.70 MB of the 62.64 MB
    "priority-1" material was ``.saipen/`` agent memory (content-addressed
    milestone blobs, settled recovery records, nested sub-agent kitchens), which
    made COMPACT (10 MB target) byte-identical to FULL.
    """

    def test_saipen_decision_docs_are_p1_and_bulk_is_p3(self):
        self.assertEqual(_priority_for(".saipen/state.md", "state.md"), 1)
        self.assertEqual(_priority_for(".saipen/board.md", "board.md"), 1)
        self.assertEqual(_priority_for(".saipen/tickets/t-1.md", "t-1.md"), 2)
        self.assertEqual(
            _priority_for(".saipen/milestones/blobs/17e66acaab68", "17e66acaab68"), 3
        )
        self.assertEqual(
            _priority_for(".saipen/extensions/subs/saiui/kitchen/pen/main.py", "main.py"),
            3,
        )
        self.assertEqual(
            _priority_for(".saipen/recovery/settled/r-1/operation.json", "operation.json"),
            3,
        )

    def test_code_is_p1_prose_is_p2_bulk_is_p3(self):
        self.assertEqual(_priority_for("src/app.py", "app.py"), 1)
        self.assertEqual(_priority_for("pyproject.toml", "pyproject.toml"), 1)
        self.assertEqual(_priority_for("tests/test_x.py", "test_x.py"), 1)
        self.assertEqual(_priority_for("readme.md", "readme.md"), 1)
        self.assertEqual(_priority_for("docs/manual.md", "manual.md"), 2)
        self.assertEqual(_priority_for("notes/thoughts.txt", "thoughts.txt"), 2)
        self.assertEqual(_priority_for("data/files/blob.bin", "blob.bin"), 3)


class TestAccountingIdentityUnderFailure(FidelityBase):
    """The identity must survive the paths that never reach a clean stat().

    Every counted exclusion or failure needs a matching ``discovered``. When one
    side moves alone, ``discovered == included + excluded + failed`` reads false
    for a tree that is merely unusual -- a symlink, an unreadable file, a
    directory that cannot be traversed -- and the manifest presents an
    incompletely walked project as fully accounted for.
    """

    def test_a_symlinked_file_is_discovered_and_excluded(self):
        self._write("main.py", 64)
        self._write("link.txt", 64)
        real_is_symlink = Path.is_symlink

        def fake(self):
            return self.name == "link.txt" or real_is_symlink(self)

        with unittest.mock.patch.object(Path, "is_symlink", fake):
            plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(plan.reason_stats[REASON_UNSUPPORTED]["count"], 1)

    def test_an_unstattable_file_is_discovered_and_failed(self):
        self._write("main.py", 64)
        self._write("ghost.txt", 64)
        real_stat = Path.stat

        def fake(self, *args, **kwargs):
            if self.name == "ghost.txt":
                raise OSError(13, "denied")
            return real_stat(self, *args, **kwargs)

        with unittest.mock.patch.object(Path, "stat", fake):
            plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(plan.failed, 1)
        # One unreadable file must not cost the rest of the tree: the symlink
        # probe is a stat too, so before the shared guard an EACCES file ended
        # the walk and the plan reported an empty project as a valid audit.
        self.assertTrue(plan.decisions["main.py"].include)

    def test_an_untraversable_directory_is_discovered_and_failed(self):
        self._write("main.py", 64)
        real_walk = fidelity.os.walk

        def fake(top, onerror=None, **kwargs):
            if onerror is not None:
                onerror(OSError(13, "denied"))
            yield from real_walk(top, onerror=onerror, **kwargs)

        with unittest.mock.patch.object(fidelity.os, "walk", fake):
            plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(plan.failed, 1)
        self.assertTrue(
            plan.walk_incomplete,
            "a partly enumerated tree may not read as complete accounting",
        )

    def test_a_complete_walk_is_not_flagged_incomplete(self):
        self._write("main.py", 64)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertFalse(plan.walk_incomplete)


class TestSoftBudgetTrim(FidelityBase):
    def test_never_trims_priority_one_material(self):
        # P1 source material must survive the budget; ordinary assets trim first.
        self._write("main.py", 64)
        self._write("src/app.py", 64)
        self._write("tests/test_x.py", 64)
        self._write("docs/manual.txt", 1024)
        self._write("data/blob.bin", 2_000_000)  # P3 ordinary asset
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT, max_mb=1)
        self._assert_invariant(plan)
        # P1 files must all be included
        for rel in ("main.py", "src/app.py", "tests/test_x.py"):
            self.assertTrue(
                plan.decisions[rel].include, f"{rel} is mandatory audit material"
            )
        # the big P3 blob is trimmed by size_limit (not media budget)
        blob = plan.decisions.get("data/blob.bin")
        self.assertIsNotNone(blob)
        self.assertFalse(blob.include)
        self.assertEqual(blob.reason, REASON_SIZE_LIMIT)
        self.assertEqual(plan.reason_stats[REASON_SIZE_LIMIT]["count"], 1)
        # prose is tier 2: dropping the one P3 asset already met the budget, so
        # the doc survives untouched.
        self.assertTrue(plan.decisions["docs/manual.txt"].include)

    def test_prose_trims_only_after_every_asset_is_gone(self):
        self._write("main.py", 64)
        self._write("docs/a.md", 600_000)
        self._write("docs/b.md", 600_000)
        self._write("data/blob.bin", 600_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT, max_mb=1)
        self._assert_invariant(plan)
        # the asset must be gone before any doc is touched
        self.assertFalse(plan.decisions["data/blob.bin"].include)
        docs_kept = [
            rel for rel in ("docs/a.md", "docs/b.md") if plan.decisions[rel].include
        ]
        self.assertEqual(len(docs_kept), 1, "exactly one doc fits under 1 MB")
        self.assertTrue(plan.decisions["main.py"].include)

    def test_budget_can_be_missed_rather_than_sacrifice_source(self):
        # P1 alone exceeds the budget: the plan overshoots and says so.
        self._write("main.py", 2_000_000)
        self._write("src/app.py", 2_000_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT, max_mb=1)
        self._assert_invariant(plan)
        self.assertEqual(plan.excluded, 0, "nothing may be trimmed to reach it")
        self.assertGreater(plan.included_bytes, plan.budget_bytes)

    def test_always_include_is_never_trimmed(self):
        self._write("main.py", 64)
        self._write("data/huge.bin", 3_000_000)
        packing = PackingConfig(
            output_dir=str(self.output_dir),
            fidelity_profile=PROFILE_COMPACT,
            fidelity_max_mb=1,
            always_include=["data/huge.bin"],
        )
        plan = build_plan_from_config(self.source, packing, set())
        self._assert_invariant(plan)
        self.assertTrue(
            plan.decisions["data/huge.bin"].include,
            "an explicitly named file outranks the soft budget",
        )


class TestOverrides(FidelityBase):
    def _packing(self, **kw) -> PackingConfig:
        base = dict(output_dir=str(self.output_dir), fidelity_profile=PROFILE_STANDARD)
        base.update(kw)
        return PackingConfig(**base)

    def test_always_include_beats_sampling(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 5)
        self._write("fixtures/special.wav", 300_000)
        packing = self._packing(always_include=["fixtures/special.wav"])
        plan = build_plan_from_config(self.source, packing, set())
        self._assert_invariant(plan)
        self.assertTrue(plan.decisions["fixtures/special.wav"].include)

    def test_always_exclude_beats_profile(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 3)
        self._write("References/raw/heavy.dat", 2_000_000)
        packing = self._packing(always_exclude=["References/raw/**"])
        plan = build_plan_from_config(self.source, packing, set())
        self._assert_invariant(plan)
        # "References/raw/**" is a 3-segment pattern; the dir itself is not
        # whole-pruned, but every file below it is excluded at file level.
        dec = plan.decisions.get("references/raw/heavy.dat")
        self.assertIsNotNone(dec)
        self.assertFalse(dec.include)
        self.assertEqual(dec.reason, REASON_CONFIGURED_IGNORE)

    def test_safety_policy_beats_always_include(self):
        # a mandatory secret must never be included even via always_include
        self._write("main.py", 64)
        self._write("token.txt", 64)
        packing = self._packing(always_include=["token.txt"])
        plan = build_plan_from_config(self.source, packing, set())
        self._assert_invariant(plan)
        dec = plan.decisions.get("token.txt")
        self.assertIsNotNone(dec)
        self.assertFalse(dec.include, "safety policy must beat always_include")
        self.assertEqual(dec.reason, REASON_SECRET_POLICY)


class TestPackingIntegration(FidelityBase):
    def _packing(self, **kw) -> PackingConfig:
        base = dict(output_dir=str(self.output_dir), fidelity_profile=PROFILE_STANDARD)
        base.update(kw)
        return PackingConfig(**base)

    def _pack(self, packing: PackingConfig, stem="Proj"):
        return pack_single(
            source_path=self.source,
            output_dir=self.output_dir,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            packing=packing,
            manifest_meta={"project_name": stem},
        )

    def _manifest(self, zip_path: Path) -> dict:
        with zipfile.ZipFile(zip_path) as zf:
            return json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))

    def test_standard_pack_manifest_is_truthful(self):
        self._write("main.py", 64)
        self._write("README.md", 64)
        self._media_dir("Sounds", 5, size=600_000)
        packing = self._packing(fidelity_profile=PROFILE_STANDARD, manifest_enabled=True)
        res = self._pack(packing)
        self.assertTrue(res.success, res.error_message)
        # PackResult carries truthful accounting
        self.assertEqual(res.fidelity_profile, PROFILE_STANDARD)
        self.assertEqual(res.archive_semantics, "audit_representation")
        self.assertEqual(
            res.files_discovered, res.files_included + res.files_excluded + res.files_failed,
        )
        self.assertGreater(res.files_excluded, 0, "media sampling must exclude some wavs")
        # manifest carries profile + semantics + byte totals
        m = self._manifest(res.output_path)
        self.assertEqual(m["fidelity_profile"], PROFILE_STANDARD)
        self.assertEqual(m["archive_semantics"], "audit_representation")
        self.assertEqual(m["files_discovered"], res.files_discovered)
        self.assertEqual(m["files_included"], res.files_included)
        self.assertEqual(m["files_excluded"], res.files_excluded)
        self.assertEqual(m["files_failed"], res.files_failed)
        # source bytes = included + excluded (manifest bytes are written after
        # the manifest data is generated, so they are not part of source_bytes)
        self.assertEqual(m["source_bytes"], m["included_bytes"] + m["excluded_bytes"])
        self.assertGreaterEqual(m["archive_bytes"], 0)
        self.assertIn("exclusions", m)
        self.assertIn(REASON_MEDIA_BUDGET, m["exclusions"])
        self.assertIn("media_inventory", m)

    def test_full_pack_claims_full_snapshot(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 3, size=100_000)
        packing = self._packing(fidelity_profile=PROFILE_FULL, manifest_enabled=True)
        res = self._pack(packing)
        self.assertTrue(res.success, res.error_message)
        m = self._manifest(res.output_path)
        self.assertEqual(m["archive_semantics"], "full_snapshot")
        self.assertEqual(m["files_excluded"], 0, "FULL excludes nothing")
        self.assertEqual(res.files_excluded, 0)

    def test_manifest_admits_a_missed_budget(self):
        # Mandatory material alone exceeds COMPACT's budget. Nothing is trimmed
        # to reach it, so the manifest must NOT imply the target was met.
        self._write("main.py", 2_000_000)
        self._write("src/app.py", 2_000_000)
        packing = self._packing(
            fidelity_profile=PROFILE_COMPACT, fidelity_max_mb=1, manifest_enabled=True
        )
        res = self._pack(packing)
        self.assertTrue(res.success, res.error_message)
        m = self._manifest(res.output_path)
        self.assertEqual(m["budget_bytes"], 1024 * 1024)
        self.assertFalse(m["budget_met"])
        self.assertEqual(m["files_excluded"], 0)

    def test_manifest_reports_a_met_budget(self):
        self._write("main.py", 64)
        packing = self._packing(fidelity_profile=PROFILE_STANDARD, manifest_enabled=True)
        res = self._pack(packing)
        self.assertTrue(res.success, res.error_message)
        m = self._manifest(res.output_path)
        self.assertTrue(m["budget_met"])

    def test_legacy_pack_without_packing_config_reports_full(self):
        # calling pack_single without `packing` keeps the legacy behaviour and
        # the manifest honestly reports FULL semantics -- nothing was trimmed.
        self._write("main.py", 64)
        res = pack_single(
            source_path=self.source,
            output_dir=self.output_dir,
            archive_stem="Legacy",
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": "Legacy"},
        )
        self.assertTrue(res.success, res.error_message)
        m = self._manifest(res.output_path)
        self.assertEqual(m["fidelity_profile"], PROFILE_FULL)
        self.assertEqual(m["archive_semantics"], "full_snapshot")
        self.assertEqual(m["files_discovered"], 1)


if __name__ == "__main__":
    unittest.main()
