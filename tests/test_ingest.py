"""Unit tests for audit text and clipboard ingestion."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audapack import ingest
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
