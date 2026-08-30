"""Unit tests for SAIPEN read-only detection and Git awareness."""

import shutil
import tempfile
import unittest
from pathlib import Path

from audapack.saipen import get_saipen_info


class TestSaipenAwareness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.proj_dir = Path(self.temp_dir) / "TestProj"
        self.proj_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_saipen(self):
        info = get_saipen_info(self.proj_dir)
        self.assertFalse(info.detected)

    def test_saipen_detected_and_parsed(self):
        saipen_dir = self.proj_dir / ".saipen"
        saipen_dir.mkdir()

        state_content = """# State
task: T-042 Implement feature
phase: BUILD
next_action: Run tests
last_event: E-123
style_contract: ded-4ae736e4
"""
        (saipen_dir / "STATE.md").write_text(state_content, encoding="utf-8")

        # Snapshot before inspection
        mtime_before = (saipen_dir / "STATE.md").stat().st_mtime_ns

        info = get_saipen_info(self.proj_dir)
        self.assertTrue(info.detected)
        self.assertEqual(info.task, "T-042 Implement feature")
        self.assertEqual(info.phase, "BUILD")
        self.assertEqual(info.next_action, "Run tests")

        # Invariant: read-only check (mtime not modified)
        mtime_after = (saipen_dir / "STATE.md").stat().st_mtime_ns
        self.assertEqual(mtime_before, mtime_after)

    def test_malformed_state_does_not_crash(self):
        saipen_dir = self.proj_dir / ".saipen"
        saipen_dir.mkdir()
        (saipen_dir / "STATE.md").write_text("random broken content without keys", encoding="utf-8")

        info = get_saipen_info(self.proj_dir)
        self.assertTrue(info.detected)
        self.assertEqual(info.task, "")
        self.assertEqual(info.phase, "")


if __name__ == "__main__":
    unittest.main()
