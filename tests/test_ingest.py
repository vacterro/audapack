"""Unit tests for audit text and clipboard ingestion."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from audapack import campaign, ingest
from audapack.campaign import campaign_transaction_lock
from audapack.config import AppConfig, AuditsConfig
from audapack.ingest import (
    clean_markdown_headers,
    detect_wave_type,
    extract_project_name_from_text,
    ingest_audit_text,
)


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.config = AppConfig(audits=AuditsConfig(root=str(self.root)))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clean_markdown_headers(self):
        dirty = """```markdown
**PROJECT_NAME:** TEST_PROJ
**WAVE:** AUDIT CORE
**STATUS:** AUDIT_CORE: COMPLETE
**TICKETS:** 1
[P1] [CORE-001] some_file.py
EVIDENCE: ev
DEFECT: def
REPAIR: rep
VERIFY: ver
**CORE_DONE_WHEN:** done when ready
```"""
        cleaned = clean_markdown_headers(dirty)
        self.assertIn("PROJECT_NAME: TEST_PROJ", cleaned)
        self.assertIn("STATUS: AUDIT_CORE: COMPLETE", cleaned)
        self.assertIn("CORE_DONE_WHEN: done when ready", cleaned)
        self.assertNotIn("**PROJECT_NAME:**", cleaned)
        self.assertNotIn("```", cleaned)

    def test_detect_wave_type(self):
        self.assertEqual(detect_wave_type("WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE"), "core")
        self.assertEqual(detect_wave_type("WAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE"), "second")
        self.assertEqual(detect_wave_type("WAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE"), "performance")

    def test_extract_project_name(self):
        text = "**PROJECT_NAME:** `SAIPEN`\nWAVE: AUDIT CORE"
        self.assertEqual(extract_project_name_from_text(text), "SAIPEN")

    def test_ingest_single_wave(self):
        core_text = """
PROJECT_NAME: TEST_APP
WAVE: AUDIT CORE
STATUS: AUDIT_CORE: COMPLETE
TICKETS: 1
[P1] [CORE-001] main.py
EVIDENCE: ev
DEFECT: def
REPAIR: rep
VERIFY: ver
CORE_DONE_WHEN: done
"""
        res = ingest_audit_text(core_text, self.config, base_dir=self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.project_name, "TEST_APP")
        self.assertEqual(res.saved_waves, ["core"])
        self.assertFalse(res.all3_generated)

    def test_ingest_rolls_back_previous_bytes_when_wave_commit_fails(self):
        old_core = b"old core bytes\n"
        old_second = b"old second bytes\n"
        project_dir = self.root / "PROJ_ROLLBACK"
        project_dir.mkdir()
        core_path = project_dir / "PROJ_ROLLBACK__01_AUDIT_CORE.md"
        second_path = project_dir / "PROJ_ROLLBACK__02_AUDIT_SECOND_WAVE.md"
        core_path.write_bytes(old_core)
        second_path.write_bytes(old_second)

        content = (
            "PROJECT_NAME: PROJ_ROLLBACK\n"
            "WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n"
            "[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n\n"
            "PROJECT_NAME: PROJ_ROLLBACK\n"
            "WAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n"
            "[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        )
        original_atomic_write = ingest.atomic_write

        def fail_second(path, text):
            if Path(path).name.endswith("02_AUDIT_SECOND_WAVE.md"):
                raise OSError("injected second-wave write failure")
            return original_atomic_write(path, text)

        with patch.object(ingest, "atomic_write", side_effect=fail_second):
            result = ingest.ingest_audit_text(content, self.config, base_dir=self.root)

        self.assertFalse(result.ok)
        self.assertIn("rolled back", result.error)
        self.assertEqual(core_path.read_bytes(), old_core)
        self.assertEqual(second_path.read_bytes(), old_second)

    def test_ingest_surfaces_canonical_write_failure_and_rolls_back(self):
        core = "PROJECT_NAME: PROJ_CANON\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        second = "PROJECT_NAME: PROJ_CANON\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        perf = "PROJECT_NAME: PROJ_CANON\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE\nTICKETS: 1\n[P1] [PERF-001] c.py\nEVIDENCE: e\nISSUE: i\nOPTIMIZE: o\nGUARDRAIL: g\nVERIFY: v\nPERFORMANCE_DONE_WHEN: done\n"
        self.assertTrue(ingest.ingest_audit_text(core, self.config, base_dir=self.root).ok)
        self.assertTrue(ingest.ingest_audit_text(second, self.config, base_dir=self.root).ok)
        project_dir = next(self.root.rglob("PROJ_CANON__01_AUDIT_CORE.md")).parent
        old_all3 = b"previous all3\n"
        all3_path = project_dir / "PROJ_CANON__00_AUDIT_ALL_3.md"
        all3_path.write_bytes(old_all3)
        original_atomic_write = ingest.atomic_write

        def fail_all3(path, text):
            if Path(path).name.endswith("00_AUDIT_ALL_3.md"):
                raise OSError("injected canonical write failure")
            return original_atomic_write(path, text)

        with patch.object(ingest, "atomic_write", side_effect=fail_all3):
            result = ingest.ingest_audit_text(perf, self.config, base_dir=self.root)

        self.assertFalse(result.ok)
        self.assertIn("canonical campaign artifacts", result.error)
        self.assertEqual(all3_path.read_bytes(), old_all3)

    def test_ingest_surfaces_live_campaign_index_failure(self):
        core = "PROJECT_NAME: PROJ_INDEX\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        with patch.object(ingest, "save_live_campaign_index", side_effect=OSError("injected index write failure")):
            result = ingest.ingest_audit_text(core, self.config, base_dir=self.root)
        self.assertFalse(result.ok)
        self.assertIn("canonical campaign artifacts", result.error)
        self.assertIn("index write failure", result.error)
        self.assertFalse((self.root / "MAIN0" / "PROJ_INDEX" / "PROJ_INDEX__01_AUDIT_CORE.md").exists())

    def test_ingest_all_3_waves_synthesizes_canonical(self):
        core = "PROJECT_NAME: PROJ_XYZ\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        second = "PROJECT_NAME: PROJ_XYZ\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        perf = "PROJECT_NAME: PROJ_XYZ\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE\nTICKETS: 1\n[P1] [PERF-001] c.py\nEVIDENCE: e\nISSUE: i\nOPTIMIZE: o\nGUARDRAIL: g\nVERIFY: v\nPERFORMANCE_DONE_WHEN: done\n"

        # 1. Ingest Core
        r1 = ingest_audit_text(core, self.config, base_dir=self.root)
        self.assertTrue(r1.ok)
        self.assertFalse(r1.all3_generated)

        # 2. Ingest Second
        r2 = ingest_audit_text(second, self.config, base_dir=self.root)
        self.assertTrue(r2.ok)
        self.assertFalse(r2.all3_generated)

        # 3. Ingest Performance -> triggers ALL_3
        r3 = ingest_audit_text(perf, self.config, base_dir=self.root)
        self.assertTrue(r3.ok)
        self.assertTrue(r3.all3_generated)
        self.assertIsNotNone(r3.all3_path)

    def test_ingest_terminal_run_id_consistent_across_all_artifacts(self):
        """W2-006: a single terminal ingest must produce a consistent run_id
        across the final ALL_3 artifact and campaign.json — not a split identity
        from independently sampled timestamps."""
        core = "PROJECT_NAME: PROJ_RUNID\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        second = "PROJECT_NAME: PROJ_RUNID\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        perf = "PROJECT_NAME: PROJ_RUNID\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE\nTICKETS: 1\n[P1] [PERF-001] c.py\nEVIDENCE: e\nISSUE: i\nOPTIMIZE: o\nGUARDRAIL: g\nVERIFY: v\nPERFORMANCE_DONE_WHEN: done\n"
        for txt in [core, second]:
            self.assertTrue(ingest_audit_text(txt, self.config, base_dir=self.root).ok)
        r3 = ingest_audit_text(perf, self.config, base_dir=self.root)
        self.assertTrue(r3.ok)
        proj_dir = next(self.root.rglob("PROJ_RUNID__01_AUDIT_CORE.md")).parent
        campaign_json = proj_dir / "campaign.json"
        self.assertTrue(campaign_json.exists())
        import json as _j
        cj = _j.loads(campaign_json.read_text(encoding="utf-8"))
        all3_file = proj_dir / "PROJ_RUNID__00_AUDIT_ALL_3.md"
        all3_text = all3_file.read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r"(?:CAMPAIGN_RUN_ID|RUN_ID):\s*(\S+)", all3_text)
        self.assertIsNotNone(m, "final ALL_3 must contain a run-id header")
        all3_run = m.group(1)
        cj_run = cj.get("campaign_run_id", "")
        self.assertEqual(all3_run, cj_run, "run_id in ALL_3 must match campaign.json")

    def test_ingest_terminal_no_duplicate_all3_generation(self):
        """W2-005: the single terminal ingest call must emit at most one
        generation, never a duplicate 'all3'."""
        from audapack.bridge.state import get_audit_generation
        core = "PROJECT_NAME: PROJ_NOGEN\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        second = "PROJECT_NAME: PROJ_NOGEN\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        perf = "PROJECT_NAME: PROJ_NOGEN\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE\nTICKETS: 1\n[P1] [PERF-001] c.py\nEVIDENCE: e\nISSUE: i\nOPTIMIZE: o\nGUARDRAIL: g\nVERIFY: v\nPERFORMANCE_DONE_WHEN: done\n"
        for txt in [core, second]:
            self.assertTrue(ingest_audit_text(txt, self.config, base_dir=self.root).ok)
        old_gen = get_audit_generation().get("generation", 0)
        r3 = ingest_audit_text(perf, self.config, base_dir=self.root)
        self.assertTrue(r3.ok)
        new_gen = get_audit_generation().get("generation", 0)
        # The terminal ingest (which produces the ALL_3) must bump the generation
        # exactly once, not twice (pre-commit + post-commit duplicate).
        gen_diff = new_gen - old_gen
        self.assertEqual(gen_diff, 1, "terminal ingest must emit exactly one generation")
        self.assertTrue(r3.all3_path.exists())
        self.assertIn("00_AUDIT_ALL_3.md", r3.all3_path.name)

    def test_an_unknown_project_with_conflicting_run_ids_leaves_no_trace(self):
        """CORE-004 (audit/3.md): registration is COMMIT, not preparation.

        Cross-wave run-id equality is a pure function of the pasted text, yet it
        ran AFTER `resolve_or_register_project()` and `target_dir.mkdir()`. So an
        unknown project pasting two waves with different CAMPAIGN_RUN_IDs was
        refused -- saying "all ingest writes rolled back" -- having already
        written config.json, a durable registry entry ('new_bad', 'NEW_BAD') and
        an audits/SIDE1/NEW_BAD directory. Ghost projects from invalid text.
        """
        from audapack.config import config_path, load_config

        content = (
            "PROJECT_NAME: NEW_BAD\nCAMPAIGN_RUN_ID: run-A\n"
            "WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n"
            "[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n\n"
            "PROJECT_NAME: NEW_BAD\nCAMPAIGN_RUN_ID: run-B\n"
            "WAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n"
            "[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        )
        cfg_file = config_path(self.root)
        before = cfg_file.read_bytes() if cfg_file.exists() else None

        result = ingest_audit_text(content, self.config, base_dir=self.root)

        self.assertFalse(result.ok)
        self.assertIn("Multiple campaign run IDs", result.error)
        after = cfg_file.read_bytes() if cfg_file.exists() else None
        self.assertEqual(after, before, "the registry was mutated by a rejected ingest")
        if after is not None:
            names = [p.display_name for p in load_config(self.root).projects]
            self.assertNotIn("NEW_BAD", names, "a ghost project was registered")
        self.assertEqual(
            list(self.root.rglob("NEW_BAD")), [],
            "a ghost audit directory survived a rejected ingest",
        )

    def test_a_known_project_with_conflicting_run_ids_writes_nothing(self):
        content_ok = (
            "PROJECT_NAME: KNOWN_RUN\nCAMPAIGN_RUN_ID: run-one\n"
            "WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n"
            "[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        )
        self.assertTrue(ingest_audit_text(content_ok, self.config, base_dir=self.root).ok)
        project_dir = next(self.root.rglob("KNOWN_RUN__01_AUDIT_CORE.md")).parent
        before = sorted(p.name for p in project_dir.iterdir())

        conflicting = (
            "PROJECT_NAME: KNOWN_RUN\nCAMPAIGN_RUN_ID: run-two\n"
            "WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n"
            "[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n\n"
            "PROJECT_NAME: KNOWN_RUN\nCAMPAIGN_RUN_ID: run-three\n"
            "WAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n"
            "[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        )
        result = ingest_audit_text(conflicting, self.config, base_dir=self.root)
        self.assertFalse(result.ok)
        self.assertEqual(sorted(p.name for p in project_dir.iterdir()), before)

    def test_a_valid_unknown_project_still_registers_once(self):
        content = (
            "PROJECT_NAME: NEW_GOOD\nCAMPAIGN_RUN_ID: run-same\n"
            "WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n"
            "[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n\n"
            "PROJECT_NAME: NEW_GOOD\nCAMPAIGN_RUN_ID: run-same\n"
            "WAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n"
            "[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        )
        from audapack.config import load_config

        result = ingest_audit_text(content, self.config, base_dir=self.root)
        self.assertTrue(result.ok, result.error)
        names = [p.display_name for p in load_config(self.root).projects]
        self.assertEqual(names.count("NEW_GOOD"), 1)
        self.assertEqual(sorted(result.saved_waves), ["core", "second"])


def _lock_is_busy_elsewhere(campaign_root: Path) -> bool:
    """True when another thread cannot take this campaign root's lock.

    The reentrancy escape in ``campaign_transaction_lock`` is thread-local, so a
    probe from a second thread contends for real, exactly like a second process.
    """
    verdict: dict[str, bool] = {}

    def probe():
        try:
            with campaign_transaction_lock(campaign_root, timeout=0.05):
                verdict["busy"] = False
        except TimeoutError:
            verdict["busy"] = True

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(10)
    return verdict.get("busy", False)


class TestCampaignIngestTransactionLock(unittest.TestCase):
    """W2-001: one campaign root, one writer at a time.

    Before this, ingest captured its rollback snapshot and wrote waves,
    canonical finals and campaign.json with no cross-process lock, so a
    concurrent commit for the same project interleaved and the first rollback
    restored pre-transaction bytes over the other writer's committed campaign.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.config = AppConfig(audits=AuditsConfig(root=str(self.root)))
        self.core = (
            "PROJECT_NAME: PROJ_LOCK\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n"
            "[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_the_lock_is_held_while_ingest_writes_a_wave(self):
        observed: list[bool] = []
        original_atomic_write = ingest.atomic_write

        def probe_then_write(path, text):
            observed.append(_lock_is_busy_elsewhere(Path(path).parent))
            return original_atomic_write(path, text)

        with patch.object(ingest, "atomic_write", side_effect=probe_then_write):
            result = ingest.ingest_audit_text(self.core, self.config, base_dir=self.root)

        self.assertTrue(result.ok, result.error)
        self.assertTrue(observed and all(observed), "ingest wrote a wave without holding the campaign lock")

    def test_the_lock_is_released_once_the_transaction_ends(self):
        result = ingest.ingest_audit_text(self.core, self.config, base_dir=self.root)
        self.assertTrue(result.ok, result.error)
        campaign_root = next(self.root.rglob("PROJ_LOCK__01_AUDIT_CORE.md")).parent
        self.assertFalse(
            _lock_is_busy_elsewhere(campaign_root),
            "the campaign lock outlived the transaction that took it",
        )

    def test_publishing_the_index_takes_the_lock_even_outside_ingest(self):
        result = ingest.ingest_audit_text(self.core, self.config, base_dir=self.root)
        self.assertTrue(result.ok, result.error)
        campaign_root = next(self.root.rglob("PROJ_LOCK__01_AUDIT_CORE.md")).parent
        observed: list[bool] = []
        original_temp_file = campaign.open_new_temp_file

        def probe_then_open(directory, name):
            observed.append(_lock_is_busy_elsewhere(directory))
            return original_temp_file(directory, name)

        with patch.object(campaign, "open_new_temp_file", side_effect=probe_then_open):
            campaign.save_live_campaign_index(
                campaign_root=campaign_root,
                profile=campaign.get_profile("quick3"),
                run_id="run-lock",
                project_name="PROJ_LOCK",
                parsed_waves={},
                completed_waves=["core"],
                active_wave_id="second",
                status=campaign.STATUS_CAMPAIGN_READY_FOR_WAVE,
            )

        self.assertEqual(observed, [True], "campaign.json was published without the campaign lock")
