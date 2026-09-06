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
    profile_media_bytes,
    profile_media_samples,
)
from audapack.packing import MANIFEST_FILENAME, pack_single


class _FakeEntry:
    """An ``os.DirEntry`` that can present one unusual answer.

    PERF-001: the plan walk reads every entry's metadata from the directory read
    that produced it, so a test stages a symlink or an EACCES file by handing the
    walk the entry object it consumes -- patching ``Path.stat`` no longer
    intercepts anything.
    """

    def __init__(self, entry, *, symlink: bool = False, stat_error: bool = False):
        self._entry = entry
        self._symlink = symlink
        self._stat_error = stat_error

    @property
    def name(self):
        return self._entry.name

    @property
    def path(self):
        return self._entry.path

    def is_dir(self, **kwargs):
        return self._entry.is_dir(**kwargs)

    def is_symlink(self):
        return self._symlink or self._entry.is_symlink()

    def stat(self, **kwargs):
        if self._stat_error:
            raise OSError(13, "denied")
        return self._entry.stat(**kwargs)


class _FakeScandir:
    """``os.scandir``'s contract: an iterator that is also a context manager."""

    def __init__(self, entries):
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _staged_scandir(*, symlinks=(), stat_fails=(), unreadable_dirs=()):
    real_scandir = fidelity.os.scandir

    def fake(path):
        if Path(path).name in unreadable_dirs:
            raise OSError(13, "denied")
        with real_scandir(path) as it:
            entries = list(it)
        return _FakeScandir([
            _FakeEntry(
                entry,
                symlink=entry.name in symlinks,
                stat_error=entry.name in stat_fails,
            )
            for entry in entries
        ])

    return unittest.mock.patch.object(fidelity.os, "scandir", fake)


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
        # CORE-003: the identity is only half the accounting. Reason totals must
        # reconcile with the excluded terminals and known bytes with the sides
        # that claim them, or the manifest can still add up while lying.
        self.assertIsNone(
            fidelity.plan_accounting_error(plan),
            fidelity.plan_accounting_error(plan),
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
        # PERF-003: the inventory accounts for ALL 6 in exact aggregates. The
        # filename lists are bounded evidence, not a per-asset ledger.
        inv = plan.media_inventory.get("Sounds#audio")
        self.assertIsNotNone(inv)
        self.assertEqual(inv["total"], 6)
        self.assertEqual(inv["included"], len(kept))
        self.assertEqual(inv["excluded"], 6 - len(kept))
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


class TestCore005MediaSampling(FidelityBase):
    """CORE-005 (SRC-037:R005): sampling is a JOINT count-and-byte cap.

    Measured before this layer: six 400 KB files under STANDARD (3 samples,
    2 MB/dir) kept five, because remaining bytes were an alternative to the
    count instead of a second constraint; and three arbitrary 40 MB videos
    under DEEP (samples=0 = keep everything, media pinned at priority 2 above
    ordinary assets) produced ~120 MB with zero exclusions inside a declared
    100 MB tier.
    """

    def _kept_excluded(self, plan, ext: str):
        kept = sorted(
            rel for rel, d in plan.decisions.items()
            if d.include and rel.endswith("." + ext)
        )
        dropped = sorted(
            rel for rel, d in plan.decisions.items()
            if not d.include and rel.endswith("." + ext)
        )
        return kept, dropped

    def test_standard_count_cap_holds_while_bytes_remain(self):
        # The exact audit reproduction: 6 x ~400 KB = ~2.4 MB against a 2 MB
        # byte cap and a 3-sample count cap. Bytes alone would allow five.
        self._write("main.py", 64)
        self._media_dir("Sounds", 6, size=400_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        kept, dropped = self._kept_excluded(plan, "wav")
        self.assertEqual(len(kept), 3, "the sample count is a cap, not a fallback")
        self.assertEqual(len(dropped), 3)
        for rel in dropped:
            self.assertEqual(plan.decisions[rel].reason, REASON_MEDIA_BUDGET)
        # nothing silently omitted: every drop is also visible to the freshness
        # walk (extra_excluded_rel keeps the original case, decisions lowercase)
        self.assertEqual(
            {rel.lower() for rel in plan.extra_excluded_rel} & set(dropped),
            set(dropped),
        )

    def test_standard_byte_cap_holds_while_samples_remain(self):
        # Mirror case: the count would allow three, the bytes do not.
        self._write("main.py", 64)
        self._media_dir("Sounds", 4, size=1_500_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        kept, dropped = self._kept_excluded(plan, "wav")
        self.assertEqual(len(kept), 1, "2 MB/dir fits exactly one 1.5 MB file")
        self.assertEqual(len(dropped), 3)

    def test_deep_bounds_arbitrary_media_near_its_target(self):
        # The audit reproduction: three unreferenced ~40 MB videos under DEEP.
        self._write("main.py", 64)
        self._media_dir("Video", 3, size=40 * 1024 * 1024, ext="mp4")
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_DEEP)
        self._assert_invariant(plan)
        kept, dropped = self._kept_excluded(plan, "mp4")
        self.assertTrue(dropped, "DEEP must bound arbitrary media, not keep all")
        self.assertLessEqual(
            plan.included_bytes, plan.budget_bytes,
            "DEEP converges on its declared ~100 MB tier",
        )
        self.assertEqual(len(kept), 1)  # 50 MB/dir cap fits one 40 MB video

    def test_referenced_media_survives_before_arbitrary_samples(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 6, size=700_000)
        (self.source / "build.ps1").write_text(
            "Copy-Item Sounds/Sounds_5.wav $out", encoding="utf-8"
        )
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT)
        self._assert_invariant(plan)
        ref = plan.decisions["sounds/sounds_5.wav"]
        self.assertTrue(ref.include, "a named asset outranks every arbitrary sample")
        self.assertEqual(ref.priority, 1, "referenced media is mandatory material")

    def test_sampled_media_is_reachable_by_the_global_soft_budget(self):
        # Sampled-in arbitrary media is an ordinary asset (priority 3), so the
        # soft budget can still trim it. At priority 2 it outranked plain assets
        # and the global budget could never correct an over-large media tier.
        self._write("main.py", 64)
        self._media_dir("Sounds", 2, size=700_000)
        plan = build_fidelity_plan(
            self.source, set(), profile=PROFILE_STANDARD, max_mb=1
        )
        self._assert_invariant(plan)
        trimmed = [d for d in plan.decisions.values() if d.reason == REASON_SIZE_LIMIT]
        self.assertTrue(trimmed, "the global budget must reach sampled media")
        self.assertLessEqual(plan.included_bytes, plan.budget_bytes)

    def test_always_include_and_full_are_never_sampled(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 6, size=700_000)
        full = build_fidelity_plan(self.source, set(), profile=PROFILE_FULL)
        self._assert_invariant(full)
        self.assertEqual(len(self._included_media(full, "wav")), 6)
        self.assertEqual(full.excluded, 0)
        packing = PackingConfig(
            output_dir=str(self.output_dir),
            fidelity_profile=PROFILE_COMPACT,
            always_include=["Sounds/Sounds_4.wav"],
        )
        pinned = build_plan_from_config(self.source, packing, set())
        self._assert_invariant(pinned)
        self.assertTrue(pinned.decisions["sounds/sounds_4.wav"].include)

    def test_planning_the_same_tree_twice_selects_identically(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 7, size=700_000)
        self._media_dir("Images", 5, size=100_000, ext="png")
        first = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        second = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self.assertEqual(
            {rel: (d.include, d.reason) for rel, d in first.decisions.items()},
            {rel: (d.include, d.reason) for rel, d in second.decisions.items()},
        )
        self.assertEqual(first.largest_omitted, second.largest_omitted)


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
        with _staged_scandir(symlinks={"link.txt"}):
            plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(plan.reason_stats[REASON_UNSUPPORTED]["count"], 1)

    def test_an_unstattable_file_is_discovered_and_failed(self):
        self._write("main.py", 64)
        self._write("ghost.txt", 64)
        with _staged_scandir(stat_fails={"ghost.txt"}):
            plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(plan.failed, 1)
        # One unreadable file must not cost the rest of the tree: the symlink
        # probe is metadata work too, so before the shared guard an EACCES file
        # ended the walk and the plan reported an empty project as a valid audit.
        self.assertTrue(plan.decisions["main.py"].include)

    def test_an_untraversable_directory_is_discovered_and_failed(self):
        self._write("main.py", 64)
        (self.source / "locked").mkdir()
        with _staged_scandir(unreadable_dirs={"locked"}):
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


class TestCore003Accounting(FidelityBase):
    """CORE-003 (SRC-037:R003): the accounting must be true, not merely balanced.

    Measured before this layer: a tree of three physical files under a pruned
    ``node_modules`` reported ``discovered=1``, because a whole-directory prune
    recorded only its name -- the omitted material vanished from every counter
    while ``discovered == included + excluded + failed`` still read true. The
    identity was also never checked anywhere: the plan's totals were copied into
    the manifest unverified, and a failure had no category vocabulary at all, so
    an I/O error was indistinguishable from a policy decision.
    """

    def test_a_pruned_directory_reports_the_material_it_removed(self):
        self._write("main.py", 64)
        heavy = self.source / "node_modules" / "pkg"
        heavy.mkdir(parents=True)
        for index in range(3):
            (heavy / f"chunk{index}.js").write_bytes(b"y" * 100)
        plan = build_fidelity_plan(
            self.source, {"node_modules"}, profile=PROFILE_STANDARD
        )
        self._assert_invariant(plan)
        self.assertEqual(plan.discovered, 4, "the pruned tree may not vanish")
        self.assertEqual(plan.excluded, 3)
        self.assertEqual(plan.excluded_bytes, 300)
        self.assertEqual(plan.source_bytes, 364)
        info = plan.pruned_dirs_rel["node_modules"]
        self.assertEqual(info["files"], 3)
        self.assertEqual(info["bytes"], 300)
        self.assertEqual(
            fidelity.pruned_census_totals(plan), (3, 300),
            "the manifest must be able to total the omitted weight",
        )

    def test_a_failure_carries_its_own_category(self):
        self._write("main.py", 64)
        self._write("ghost.txt", 64)
        with _staged_scandir(stat_fails={"ghost.txt"}):
            plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(plan.failure_stats, {fidelity.FAILURE_STAT: 1})
        self.assertEqual(
            plan.unknown_size_entries, 1,
            "a size that could not be read is unknown, never a known zero",
        )
        self.assertNotIn(
            REASON_UNSUPPORTED, plan.reason_stats,
            "a failure is not an exclusion anybody decided",
        )

    def test_the_oracle_rejects_a_plan_that_only_balances(self):
        # Each defect below leaves discovered == included + excluded + failed
        # intact, which is exactly why the identity alone was insufficient.
        balanced = fidelity.FidelityPlan(profile=PROFILE_STANDARD)
        balanced.discovered = balanced.excluded = 1
        balanced.reason_stats[REASON_MEDIA_BUDGET] = {"count": 1, "bytes": 10}
        balanced.excluded_bytes = 10
        balanced.source_bytes = 10
        self.assertIsNone(fidelity.plan_accounting_error(balanced))

        wrong_reason_total = fidelity.FidelityPlan(profile=PROFILE_STANDARD)
        wrong_reason_total.discovered = wrong_reason_total.excluded = 2
        wrong_reason_total.reason_stats[REASON_MEDIA_BUDGET] = {"count": 1, "bytes": 0}
        self.assertIn("reason totals", fidelity.plan_accounting_error(wrong_reason_total))

        wrong_bytes = fidelity.FidelityPlan(profile=PROFILE_STANDARD)
        wrong_bytes.discovered = wrong_bytes.included = 1
        wrong_bytes.included_bytes = 10
        wrong_bytes.source_bytes = 99
        self.assertIn("source_bytes", fidelity.plan_accounting_error(wrong_bytes))

        unnamed_failure = fidelity.FidelityPlan(profile=PROFILE_STANDARD)
        unnamed_failure.discovered = unnamed_failure.failed = 1
        self.assertIn("failure totals", fidelity.plan_accounting_error(unnamed_failure))

        foreign = fidelity.FidelityPlan(profile=PROFILE_STANDARD)
        foreign.discovered = foreign.failed = 1
        foreign.failure_stats["invented_category"] = 1
        self.assertIn("unknown failure", fidelity.plan_accounting_error(foreign))

    def test_a_plan_that_cannot_account_for_itself_is_not_a_snapshot(self):
        plan = fidelity.FidelityPlan(profile=PROFILE_FULL)
        plan.accounting_error = "reason totals 2 != excluded 1"
        self.assertEqual(fidelity.semantics_for_plan(plan), "audit_representation")

    def test_a_built_plan_records_that_it_reconciles(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 5, size=600_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self.assertIsNone(plan.accounting_error)

    def test_a_freshness_probe_takes_no_pruned_census(self):
        # PERF-004: a reuse decision consumes none of the census, so proving
        # freshness must not enumerate the excluded weight it describes.
        self._write("main.py", 64)
        heavy = self.source / "node_modules" / "pkg"
        heavy.mkdir(parents=True)
        for index in range(3):
            (heavy / f"chunk{index}.js").write_bytes(b"y" * 100)
        probe = build_fidelity_plan(
            self.source, {"node_modules"}, profile=PROFILE_STANDARD,
            newer_than_mtime=0.0, census_pruned=False,
        )
        self._assert_invariant(probe)
        self.assertFalse(probe.pruned_census_taken)
        self.assertEqual(probe.discovered, 1, "only the traversed tree is counted")
        self.assertEqual(probe.pruned_dirs_rel["node_modules"]["files"], 0)

    def test_the_manifest_declares_its_own_reconciliation(self):
        from audapack.packing import ZipStats, generate_manifest_data, stats_accounting_error

        stats = ZipStats(
            files_discovered=2, files_included=1, files_excluded=1,
            source_bytes=110, included_bytes=100, excluded_bytes=10,
        )
        fid = {
            "fidelity_profile": PROFILE_STANDARD,
            "archive_semantics": "audit_representation",
            "exclusions": {REASON_MEDIA_BUDGET: {"count": 1, "bytes": 10}},
        }
        self.assertIsNone(stats_accounting_error(stats, fid))
        good = generate_manifest_data("P", str(self.source), "folder", stats, fidelity=fid)
        self.assertTrue(good["accounting_reconciled"])
        self.assertNotIn("accounting_error", good)

        # A reason total that disagrees with the terminals is named, not shipped
        # as silent metadata.
        fid["exclusions"] = {REASON_MEDIA_BUDGET: {"count": 5, "bytes": 10}}
        bad = generate_manifest_data("P", str(self.source), "folder", stats, fidelity=fid)
        self.assertFalse(bad["accounting_reconciled"])
        self.assertIn("reason totals", bad["accounting_error"])

    def test_an_unreadable_file_keeps_the_byte_identity_satisfiable(self):
        from audapack.packing import ZipStats, stats_accounting_error

        # A file that could not be read belongs to neither column, so without a
        # failed side the byte identity is unsatisfiable the moment one fails.
        stats = ZipStats(
            files_discovered=2, files_included=1, files_failed=1,
            source_bytes=110, included_bytes=100, failed_bytes=10,
        )
        self.assertIsNone(stats_accounting_error(stats))


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


class TestFullIsStructurallyUnbounded(FidelityBase):
    """CORE-004 (audit/6.md): FULL may not be trimmed, nor lie when it is.

    Two independent guarantees. First, FULL ignores the generic profile
    overrides, so ``fidelity_max_mb=1`` can no longer trim it. Second, as a
    defensive invariant, ``full_snapshot`` is earned from the finished plan
    rather than read off the profile name -- if any fidelity-policy omission
    happened anyway, the archive declares itself an audit representation.
    """

    def _packing(self, **kw) -> PackingConfig:
        base = dict(output_dir=str(self.output_dir), fidelity_profile=PROFILE_FULL)
        base.update(kw)
        return PackingConfig(**base)

    def test_full_ignores_every_generic_override(self):
        self.assertEqual(profile_budget_bytes(PROFILE_FULL, override_mb=1), 0)
        self.assertEqual(profile_budget_bytes(PROFILE_FULL, override_mb=10_000), 0)
        self.assertEqual(profile_media_samples(PROFILE_FULL, override=1), 0)
        self.assertEqual(profile_media_bytes(PROFILE_FULL, override=1024), 0)
        # The overrides still govern every audit profile.
        self.assertEqual(profile_budget_bytes(PROFILE_STANDARD, override_mb=15), 15 * 1024 * 1024)
        self.assertEqual(profile_media_samples(PROFILE_STANDARD, override=1), 1)
        self.assertEqual(profile_media_bytes(PROFILE_STANDARD, override=1024), 1024)

    def test_a_tiny_max_mb_cannot_trim_full(self):
        self._write("main.py", 64)
        self._write("data/payload.dat", 2 * 1024 * 1024)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_FULL, max_mb=1)
        self._assert_invariant(plan)
        self.assertEqual(plan.budget_bytes, 0, "FULL is structurally unbounded")
        self.assertEqual(plan.excluded, 0)
        self.assertNotIn(REASON_SIZE_LIMIT, plan.reason_stats)
        self.assertTrue(plan.decisions["data/payload.dat"].include)
        self.assertEqual(plan.archive_semantics, "full_snapshot")

    def test_media_overrides_cannot_sample_full(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 5, size=700_000)
        plan = build_fidelity_plan(
            self.source, set(), profile=PROFILE_FULL, media_samples=1, media_bytes=1024
        )
        self._assert_invariant(plan)
        self.assertEqual(len(self._included_media(plan, "wav")), 5)
        self.assertNotIn(REASON_MEDIA_BUDGET, plan.reason_stats)
        self.assertEqual(plan.archive_semantics, "full_snapshot")

    def test_a_policy_omission_downgrades_the_claim(self):
        # Defensive invariant: whatever route produced the omission, a plan that
        # lost material to fidelity policy may not claim a snapshot.
        for reason in (REASON_SIZE_LIMIT, REASON_MEDIA_BUDGET):
            plan = fidelity.FidelityPlan(profile=PROFILE_FULL)
            plan.reason_stats[reason] = {"count": 1, "bytes": 10}
            self.assertEqual(
                fidelity.semantics_for_plan(plan), "audit_representation", reason
            )
        # Operator-owned and safety exclusions are not policy loss: a snapshot
        # minus its secrets is still a snapshot by contract.
        for reason in (REASON_CONFIGURED_IGNORE, REASON_SECRET_POLICY):
            plan = fidelity.FidelityPlan(profile=PROFILE_FULL)
            plan.reason_stats[reason] = {"count": 1, "bytes": 10}
            self.assertEqual(fidelity.semantics_for_plan(plan), "full_snapshot", reason)

    def test_an_incomplete_or_unreadable_walk_downgrades_the_claim(self):
        truncated = fidelity.FidelityPlan(profile=PROFILE_FULL)
        truncated.walk_incomplete = True
        self.assertEqual(fidelity.semantics_for_plan(truncated), "audit_representation")
        unreadable = fidelity.FidelityPlan(profile=PROFILE_FULL)
        unreadable.failed = 1
        self.assertEqual(fidelity.semantics_for_plan(unreadable), "audit_representation")

    def test_an_audit_profile_is_never_promoted(self):
        plan = fidelity.FidelityPlan(profile=PROFILE_STANDARD)
        self.assertEqual(fidelity.semantics_for_plan(plan), "audit_representation")

    def test_a_full_pack_with_an_override_still_holds_everything(self):
        self._write("main.py", 64)
        self._write("data/payload.dat", 2 * 1024 * 1024)
        self._media_dir("Sounds", 4, size=300_000)
        res = pack_single(
            source_path=self.source,
            output_dir=self.output_dir,
            archive_stem="Full",
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            packing=self._packing(
                fidelity_max_mb=1, fidelity_media_samples=1, fidelity_media_bytes=1024,
                manifest_enabled=True,
            ),
            manifest_meta={"project_name": "Full"},
        )
        self.assertTrue(res.success, res.error_message)
        self.assertEqual(res.files_excluded, 0)
        self.assertEqual(res.archive_semantics, "full_snapshot")
        with zipfile.ZipFile(res.output_path) as zf:
            names = set(zf.namelist())
        self.assertIn("data/payload.dat", names)
        m = json.loads(
            zipfile.ZipFile(res.output_path).read(MANIFEST_FILENAME).decode("utf-8")
        )
        self.assertEqual(m["archive_semantics"], "full_snapshot")
        self.assertEqual(m["budget_bytes"], 0)
        self.assertEqual(m["files_excluded"], 0)


class TestPerf003BoundedMediaEvidence(FidelityBase):
    """PERF-003 (SRC-037:R014): the manifest carried one record per media asset.

    Measured before this layer: 2,000 media files produced a 0.11 MiB fidelity
    payload, 10,000 produced 0.56 MiB and 20,000 produced 1.13 MiB -- one
    ``{rel, size, included}`` dict per asset, serialized into every archive,
    with no reader anywhere that consumed it. The aggregates an auditor actually
    needs are exact and constant-sized; only the filename evidence is sampled.
    """

    def _inventory_bytes(self, plan) -> int:
        return len(json.dumps(plan.media_inventory, sort_keys=True))

    def _group_of(self, rel: str) -> str:
        directory = rel.rsplit("/", 1)[0] if "/" in rel else "."
        return f"{directory}#{fidelity.media_class_for(rel.rsplit('/', 1)[-1])}"

    def test_the_media_payload_is_bounded_by_group_not_by_file_count(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 40, size=1024)
        small = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._media_dir("Sounds", 2000, size=1024)
        large = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(small)
        self._assert_invariant(large)
        # The counts stay exact: 50x the files, same one group.
        self.assertEqual(small.media_inventory["Sounds#audio"]["total"], 40)
        self.assertEqual(large.media_inventory["Sounds#audio"]["total"], 2000)
        self.assertEqual(len(large.media_inventory), 1)
        growth = self._inventory_bytes(large) - self._inventory_bytes(small)
        self.assertLess(
            growth, 64,
            f"the media payload grew {growth} bytes for 1,960 more files",
        )
        self.assertLess(
            self._inventory_bytes(large), 1024,
            "a 2,000-file media group must not serialize a per-file ledger",
        )
        for inv in large.media_inventory.values():
            self.assertLessEqual(len(inv["included_sample"]), fidelity.MEDIA_SAMPLE_LIMIT)
            self.assertLessEqual(len(inv["omitted_sample"]), fidelity.MEDIA_SAMPLE_LIMIT)

    def test_group_aggregates_are_exact_and_reconcile_with_the_decisions(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 9, size=300_000)
        self._media_dir("Images", 4, size=200_000, ext="png")
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        self.assertEqual(set(plan.media_inventory), {"Sounds#audio", "Images#image"})
        for key, inv in plan.media_inventory.items():
            members = [d for d in plan.decisions.values() if self._group_of(d.rel) == key]
            kept = [d for d in members if d.include]
            dropped = [d for d in members if not d.include]
            self.assertEqual(inv["total"], len(members), key)
            self.assertEqual(inv["total_bytes"], sum(d.size for d in members), key)
            self.assertEqual(inv["included"], len(kept), key)
            self.assertEqual(inv["included_bytes"], sum(d.size for d in kept), key)
            self.assertEqual(inv["excluded"], len(dropped), key)
            self.assertEqual(inv["excluded_bytes"], sum(d.size for d in dropped), key)
            self.assertEqual(inv["total"], inv["included"] + inv["excluded"], key)
            self.assertEqual(
                inv["total_bytes"], inv["included_bytes"] + inv["excluded_bytes"], key
            )
            # The bounded evidence is real evidence: every sampled name is a
            # decision of the claimed side, not a summary the plan invented.
            for entry in inv["included_sample"]:
                self.assertTrue(plan.decisions[entry["rel"].lower()].include, entry)
            for entry in inv["omitted_sample"]:
                self.assertFalse(plan.decisions[entry["rel"].lower()].include, entry)

    def test_two_media_classes_in_one_directory_keep_separate_aggregates(self):
        """Keying the inventory by directory alone lost a whole group.

        A directory holding audio and images wrote both groups to the same key,
        so the second overwrote the first and the manifest under-reported the
        media it had seen -- while the file counts elsewhere still added up.
        """
        self._write("main.py", 64)
        assets = self.source / "Assets"
        assets.mkdir()
        for index in range(4):
            (assets / f"clip_{index}.wav").write_bytes(b"\x00" * 300_000)
        for index in range(3):
            (assets / f"pic_{index}.png").write_bytes(b"\x00" * 200_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self._assert_invariant(plan)
        audio = plan.media_inventory.get("Assets#audio")
        image = plan.media_inventory.get("Assets#image")
        self.assertIsNotNone(audio, "the audio group is missing from the inventory")
        self.assertIsNotNone(image, "the image group is missing from the inventory")
        self.assertEqual(audio["total"], 4)
        self.assertEqual(image["total"], 3)
        self.assertEqual(
            sum(int(inv["total"]) for inv in plan.media_inventory.values()), 7,
            "every discovered media file belongs to exactly one reported group",
        )

    def test_the_top_omitted_report_matches_a_full_sort_including_ties(self):
        """The bounded selection must be the sort's answer, not an approximation.

        Equal-sized omissions make the lexicographic tie-break decide the order,
        which is exactly where a heap-based top-N can diverge from a full sort.
        The groups are named so planning order (media class first: audio before
        image) is the REVERSE of path order, so a selection that silently leans
        on heap stability instead of the documented key reports Zed before Alpha.
        """
        self._write("main.py", 64)
        self._media_dir("Zed", 8, size=500_000)
        self._media_dir("Alpha", 8, size=500_000, ext="png")
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT)
        self._assert_invariant(plan)
        omitted = [
            (d.rel, d.size) for d in plan.decisions.values()
            if not d.include and d.size > 0
        ]
        self.assertEqual(
            len({size for _rel, size in omitted}), 1,
            "the fixture must omit only equal-sized files so ties decide the order",
        )
        reference = sorted(
            omitted, key=lambda item: (-item[1], item[0].lower())
        )[:fidelity.LARGEST_OMITTED_LIMIT]
        self.assertEqual(plan.largest_omitted, reference)
        self.assertEqual(len(plan.largest_omitted), fidelity.LARGEST_OMITTED_LIMIT)
        self.assertTrue(
            plan.largest_omitted[0][0].lower().startswith("alpha/"),
            f"ties must break on path, not on planning order: {plan.largest_omitted[:2]}",
        )

    def test_planning_twice_produces_identical_bounded_evidence(self):
        self._write("main.py", 64)
        self._media_dir("Sounds", 15, size=400_000)
        first = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        second = build_fidelity_plan(self.source, set(), profile=PROFILE_STANDARD)
        self.assertEqual(first.media_inventory, second.media_inventory)
        self.assertEqual(first.largest_omitted, second.largest_omitted)

    def test_the_soft_budget_trim_is_reflected_in_the_group_aggregates(self):
        """The sampler is not the last word on a media file.

        The soft budget runs AFTER sampling and reverses media decisions the
        sampler made (sampled-in media is priority 3, the first tier the trim
        touches). Aggregating during sampling reported files as included that
        the finished plan excludes: 20 groups of 2 x 1 MB under COMPACT put
        10 MB of samples inside a 10 MB budget, and 10 groups then claimed
        ``included=1`` for material the decisions record as omitted.
        """
        self._write("main.py", 64)
        for index in range(20):
            self._media_dir(f"Sounds{index:02d}", 2, size=1_000_000)
        plan = build_fidelity_plan(self.source, set(), profile=PROFILE_COMPACT)
        self._assert_invariant(plan)
        trimmed = [
            d for d in plan.decisions.values() if d.reason == fidelity.REASON_SIZE_LIMIT
        ]
        self.assertTrue(trimmed, "the fixture must actually engage the soft-budget trim")
        for key, inv in plan.media_inventory.items():
            members = [d for d in plan.decisions.values() if self._group_of(d.rel) == key]
            kept = [d for d in members if d.include]
            dropped = [d for d in members if not d.include]
            self.assertEqual(inv["included"], len(kept), key)
            self.assertEqual(inv["included_bytes"], sum(d.size for d in kept), key)
            self.assertEqual(inv["excluded"], len(dropped), key)
            self.assertEqual(inv["excluded_bytes"], sum(d.size for d in dropped), key)
            for entry in inv["included_sample"]:
                self.assertTrue(
                    plan.decisions[entry["rel"].lower()].include,
                    f"{entry['rel']} is sampled as included but the plan omits it",
                )
            for entry in inv["omitted_sample"]:
                self.assertFalse(
                    plan.decisions[entry["rel"].lower()].include,
                    f"{entry['rel']} is sampled as omitted but the plan includes it",
                )
        self.assertEqual(
            sum(int(inv["included"]) for inv in plan.media_inventory.values()),
            sum(
                1 for d in plan.decisions.values()
                if d.include and self._group_of(d.rel) in plan.media_inventory
            ),
        )


if __name__ == "__main__":
    unittest.main()
