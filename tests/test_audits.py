"""Unit tests for AUDAPACK Audit indexing, temperature, and copy state tracking."""

import hashlib
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from audapack.audits import (
    AuditIndexer,
    calculate_temperature,
    extract_audit_metadata_timestamp,
    format_age_str,
    is_all3_ready,
    is_wave_complete,
)
from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditSnapshot, AuditTemperature, Project

SAMPLE_CORE = """# FastPrompter — Audit Core
PROJECT_NAME: FastPrompter
DATE_TIME: 2026-08-26T01:00:00+03:00
WAVE: AUDIT CORE
TARGET: archive.zip
BASELINE: commit abc
GIT_CONTEXT: clean
SAIPEN_CONTEXT: active
AUDIT_SCOPE: core
TEST_STATUS: PASS
STATUS: AUDIT_CORE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P0] [CORE-001] Fix something
EVIDENCE: line 10
DEFECT: bug
REPAIR: fix
VERIFY: test

CORE_DONE_WHEN: CORE-001 fixed.
"""

SAMPLE_SECOND = """# FastPrompter — Audit Second Wave
PROJECT_NAME: FastPrompter
DATE_TIME: 2026-08-26T01:30:00+03:00
WAVE: AUDIT SECOND WAVE
TARGET: archive.zip
BASELINE: commit abc
GIT_CONTEXT: clean
SAIPEN_CONTEXT: active
AUDIT_SCOPE: second
TEST_STATUS: PASS
STATUS: SECOND_WAVE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P0] [W2-001] Fix second wave
EVIDENCE: line 20
DEFECT: bug2
REPAIR: fix2
VERIFY: test2

SECOND_WAVE_DONE_WHEN: W2-001 fixed.
"""

SAMPLE_PERF = """# FastPrompter — Audit Performance
PROJECT_NAME: FastPrompter
DATE_TIME: 2026-08-26T02:00:00+03:00
WAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS
TARGET: archive.zip
BASELINE: commit abc
GIT_CONTEXT: clean
SAIPEN_CONTEXT: active
AUDIT_SCOPE: performance
TEST_STATUS: PASS
STATUS: PERFORMANCE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [PERF-001] Optimize
EVIDENCE: line 30
ISSUE: slow
OPTIMIZE: fast
GUARDRAIL: safe
VERIFY: bench

PERFORMANCE_DONE_WHEN: PERF-001 optimized.
"""

SAMPLE_ALL_3 = """# FastPrompter — Audit Handoff

RUN_ID: run-123
GENERATED_AT: 2026-08-26T02:05:00
PROJECT_NAME: FastPrompter
TOTAL_TICKETS: 3

============================================================
01 — AUDIT CORE
============================================================
STATUS: AUDIT_CORE: COMPLETE

============================================================
02 — AUDIT SECOND WAVE
============================================================
STATUS: SECOND_WAVE: COMPLETE

============================================================
03 — AUDIT PERFORMANCE
============================================================
STATUS: PERFORMANCE: COMPLETE
"""


class TestAuditEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_root = Path(self.temp_dir) / "AUDITING_IMPLEMENTATION"
        self.audit_root.mkdir(parents=True)

        self.config = AppConfig()
        self.config.audits.root = str(self.audit_root)
        self.indexer = AuditIndexer(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_temperature_boundaries_with_injected_clock(self):
        cfg = AuditsConfig(
            hot_seconds=6 * 3600,
            warm_seconds=24 * 3600,
            cool_seconds=72 * 3600,
            cold_seconds=7 * 86400,
        )

        self.assertEqual(calculate_temperature(0, cfg), AuditTemperature.HOT)
        self.assertEqual(calculate_temperature(5 * 3600 + 59 * 60, cfg), AuditTemperature.HOT)
        self.assertEqual(calculate_temperature(6 * 3600, cfg), AuditTemperature.HOT)
        self.assertEqual(calculate_temperature(6 * 3600 + 1, cfg), AuditTemperature.WARM)
        self.assertEqual(calculate_temperature(24 * 3600, cfg), AuditTemperature.WARM)
        self.assertEqual(calculate_temperature(24 * 3600 + 1, cfg), AuditTemperature.COOL)
        self.assertEqual(calculate_temperature(72 * 3600, cfg), AuditTemperature.COOL)
        self.assertEqual(calculate_temperature(72 * 3600 + 1, cfg), AuditTemperature.COLD)
        self.assertEqual(calculate_temperature(7 * 86400, cfg), AuditTemperature.COLD)
        self.assertEqual(calculate_temperature(7 * 86400 + 1, cfg), AuditTemperature.STALE)
        self.assertEqual(calculate_temperature(None, cfg), AuditTemperature.NONE)

    def test_format_age_str(self):
        self.assertEqual(format_age_str(300), "5m")
        self.assertEqual(format_age_str(3600 * 3 + 12 * 60), "3h 12m")
        self.assertEqual(format_age_str(3600 * 17), "17h")
        self.assertEqual(format_age_str(86400 * 2 + 3600 * 3), "2d 3h")
        self.assertEqual(format_age_str(86400 * 11), "11d")

    def test_wave_validation(self):
        self.assertTrue(is_wave_complete(SAMPLE_CORE, "core"))
        self.assertTrue(is_wave_complete(SAMPLE_SECOND, "second"))
        self.assertTrue(is_wave_complete(SAMPLE_PERF, "performance"))

        # Broken / incomplete wave
        broken = SAMPLE_CORE.replace("STATUS: AUDIT_CORE: COMPLETE", "STATUS: BLOCKED")
        self.assertFalse(is_wave_complete(broken, "core"))

    def test_all3_validation(self):
        ready, tickets = is_all3_ready(SAMPLE_ALL_3)
        self.assertTrue(ready)
        self.assertEqual(tickets, 3)

        broken = SAMPLE_ALL_3.replace("03 — AUDIT PERFORMANCE", "")
        ready_broken, _ = is_all3_ready(broken)
        self.assertFalse(ready_broken)

    def test_scan_project_and_copy_state_flow(self):
        # Create project in MAIN0
        proj_dir = self.audit_root / "MAIN0" / "FastPrompter"
        proj_dir.mkdir(parents=True)

        (proj_dir / "FastPrompter__01_AUDIT_CORE.md").write_text(SAMPLE_CORE, encoding="utf-8")
        (proj_dir / "FastPrompter__02_AUDIT_SECOND_WAVE.md").write_text(SAMPLE_SECOND, encoding="utf-8")
        (proj_dir / "FastPrompter__03_AUDIT_PERFORMANCE.md").write_text(SAMPLE_PERF, encoding="utf-8")
        (proj_dir / "FastPrompter__00_AUDIT_ALL_3.md").write_text(SAMPLE_ALL_3, encoding="utf-8")

        project = Project(
            id="fastprompter",
            display_name="FastPrompter",
            source_path=r"C:\FastPrompter",
            priority_group="MAIN0",
            slot=1,
            audit_project_name="FastPrompter",
        )

        now = datetime(2026, 8, 26, 3, 5, 0)
        snapshot = self.indexer.scan_project(project, now=now)

        self.assertEqual(snapshot.completed_waves, 3)
        self.assertTrue(snapshot.all3_ready)
        self.assertEqual(snapshot.temperature, AuditTemperature.HOT)
        self.assertEqual(snapshot.total_tickets, 3)

        # Read exact ALL_3
        ok, content, sha256 = self.indexer.read_exact_all3(snapshot)
        self.assertTrue(ok)
        self.assertEqual(content, SAMPLE_ALL_3)
        expected_hash = hashlib.sha256(SAMPLE_ALL_3.encode("utf-8")).hexdigest()
        self.assertEqual(sha256, expected_hash)

        # Simulate COPY AUDIT: store hash
        project.last_copied_audit_hash = sha256
        project.last_copied_at = now.isoformat()

        # Check copied state: hash matches
        self.assertEqual(snapshot.all3_sha256, project.last_copied_audit_hash)

        # Now simulate new ALL_3 arriving
        NEW_ALL_3 = SAMPLE_ALL_3 + "\n# Extra Findings\n"
        (proj_dir / "FastPrompter__00_AUDIT_ALL_3.md").write_text(NEW_ALL_3, encoding="utf-8")

        snapshot2 = self.indexer.scan_project(project, now=now)
        self.assertTrue(snapshot2.all3_ready)
        self.assertNotEqual(snapshot2.all3_sha256, project.last_copied_audit_hash)
        # New hash proves copy state is invalidated / NEW


if __name__ == "__main__":
    unittest.main()
