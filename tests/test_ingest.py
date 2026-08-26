"""Unit tests for audit text and clipboard ingestion."""

import tempfile
import unittest
from pathlib import Path

from audapack.config import AppConfig, AuditsConfig
from audapack.ingest import (
    clean_markdown_headers,
    detect_wave_type,
    extract_project_name_from_text,
    ingest_audit_text,
    split_multi_wave_text,
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
        res = ingest_audit_text(core_text, self.config)
        self.assertTrue(res.ok)
        self.assertEqual(res.project_name, "TEST_APP")
        self.assertEqual(res.saved_waves, ["core"])
        self.assertFalse(res.all3_generated)

    def test_ingest_all_3_waves_synthesizes_canonical(self):
        core = "PROJECT_NAME: PROJ_XYZ\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done\n"
        second = "PROJECT_NAME: PROJ_XYZ\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: done\n"
        perf = "PROJECT_NAME: PROJ_XYZ\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE\nTICKETS: 1\n[P1] [PERF-001] c.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nPERFORMANCE_DONE_WHEN: done\n"

        # 1. Ingest Core
        r1 = ingest_audit_text(core, self.config)
        self.assertTrue(r1.ok)
        self.assertFalse(r1.all3_generated)

        # 2. Ingest Second
        r2 = ingest_audit_text(second, self.config)
        self.assertTrue(r2.ok)
        self.assertFalse(r2.all3_generated)

        # 3. Ingest Performance -> triggers ALL_3
        r3 = ingest_audit_text(perf, self.config)
        self.assertTrue(r3.ok)
        self.assertTrue(r3.all3_generated)
        self.assertIsNotNone(r3.all3_path)
        self.assertTrue(r3.all3_path.exists())
        self.assertIn("00_AUDIT_ALL_3.md", r3.all3_path.name)
