"""Unit tests for AUDAPACK Audit indexing, temperature, and copy state tracking."""

import hashlib
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from audapack.audits import (
    AuditIndexer,
    calculate_temperature,
    format_age_str,
    is_all3_ready,
    is_wave_complete,
)
from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditTemperature, Project

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

    def test_scan_project_live_index_authority_over_stale_waves(self):
        """CORE-002: an old run's canonical wave/final files must never be
        counted as members of a new run. campaign.json is the authority."""
        import json

        proj_dir = self.audit_root / "MAIN0" / "StaleRun"
        proj_dir.mkdir(parents=True)

        # Old run A: three waves + ALL_3 all carry run A identity.
        core_a = SAMPLE_CORE + "\nCAMPAIGN_RUN_ID: run-A\n"
        second_a = SAMPLE_SECOND + "\nCAMPAIGN_RUN_ID: run-A\n"
        perf_a = SAMPLE_PERF + "\nCAMPAIGN_RUN_ID: run-A\n"
        all3_a = SAMPLE_ALL_3 + "\nCAMPAIGN_RUN_ID: run-A\n"
        (proj_dir / "StaleRun__01_AUDIT_CORE.md").write_text(core_a, encoding="utf-8")
        (proj_dir / "StaleRun__02_AUDIT_SECOND_WAVE.md").write_text(second_a, encoding="utf-8")
        (proj_dir / "StaleRun__03_AUDIT_PERFORMANCE.md").write_text(perf_a, encoding="utf-8")
        (proj_dir / "StaleRun__00_AUDIT_ALL_3.md").write_text(all3_a, encoding="utf-8")
        index_a = {
            "schema_version": 1,
            "campaign_run_id": "run-A",
            "campaign_profile": "quick3",
            "campaign_status": "CAMPAIGN_COMPLETE",
            "completed_count": 3,
            "completed_waves": ["core", "second", "performance"],
            "wave_count": 3,
            "final_handoff": "StaleRun__00_AUDIT_ALL_3.md",
        }
        (proj_dir / "campaign.json").write_text(json.dumps(index_a), encoding="utf-8")

        project = Project(
            id="stalrun",
            display_name="StaleRun",
            source_path=r"C:\StaleRun",
            priority_group="MAIN0",
            slot=1,
            audit_project_name="StaleRun",
        )
        now = datetime(2026, 8, 29, 6, 0, 0)
        snap_a = self.indexer.scan_project(project, now=now)
        self.assertEqual(snap_a.completed_waves, 3)
        self.assertTrue(snap_a.campaign_complete)
        self.assertTrue(snap_a.final_handoff_ready)

        # New run B: only Core posted. campaign.json records 1/3 ready-for-wave.
        core_b = SAMPLE_CORE + "\nCAMPAIGN_RUN_ID: run-B\n"
        (proj_dir / "StaleRun__01_AUDIT_CORE.md").write_text(core_b, encoding="utf-8")
        index_b = {
            "schema_version": 1,
            "campaign_run_id": "run-B",
            "campaign_profile": "quick3",
            "campaign_status": "CAMPAIGN_READY_FOR_WAVE",
            "completed_count": 1,
            "completed_waves": ["core"],
            "wave_count": 3,
            "final_handoff": "",
        }
        (proj_dir / "campaign.json").write_text(json.dumps(index_b), encoding="utf-8")

        snap_b = self.indexer.scan_project(project, now=now)
        # Old run-A waves and ALL_3 remain on disk but must NOT count for run B.
        self.assertEqual(snap_b.completed_waves, 1, "new run must report 1/3")
        self.assertFalse(snap_b.campaign_complete, "new run must not be complete")
        self.assertFalse(snap_b.final_handoff_ready, "old ALL_3 must not be final")
        self.assertNotEqual(snap_b.campaign_run_id, "run-A")

    def test_scan_project_parses_each_wave_exactly_once(self):
        """PERF-003: a single uncached scan of a complete Quick3 must invoke
        parse_wave exactly 3 times (one per wave) regardless of synthesis."""
        from unittest.mock import patch

        from audapack import audits as audits_mod

        proj_dir = self.audit_root / "MAIN0" / "OncePerScan"
        proj_dir.mkdir(parents=True)
        (proj_dir / "OncePerScan__01_AUDIT_CORE.md").write_text(SAMPLE_CORE, encoding="utf-8")
        (proj_dir / "OncePerScan__02_AUDIT_SECOND_WAVE.md").write_text(SAMPLE_SECOND, encoding="utf-8")
        (proj_dir / "OncePerScan__03_AUDIT_PERFORMANCE.md").write_text(SAMPLE_PERF, encoding="utf-8")
        (proj_dir / "OncePerScan__00_AUDIT_ALL_3.md").write_text(SAMPLE_ALL_3, encoding="utf-8")

        project = Project(
            id="onceperscan",
            display_name="OncePerScan",
            source_path=r"C:\OncePerScan",
            priority_group="MAIN0",
            slot=1,
            audit_project_name="OncePerScan",
        )
        now = datetime(2026, 8, 29, 7, 0, 0)
        with patch.object(audits_mod, "parse_wave", wraps=audits_mod.parse_wave) as spy:
            snap = self.indexer.scan_project(project, now=now)
        # Three waves parsed once each = 3 calls. No extra synthesis reparse.
        self.assertEqual(spy.call_count, 3, f"expected 3 parse_wave calls, got {spy.call_count}")
        self.assertEqual(snap.completed_waves, 3)

    def test_live_campaign_index_authority_over_stale_canonical_files(self):
        """CORE-002: old run wave files at canonical names must not be counted
        as members of a new run; the live campaign.json is the authority."""
        import json
        proj_dir = self.audit_root / "MAIN0" / "Scorecard"
        proj_dir.mkdir(parents=True)

        # Old run A: complete 3 waves + ALL_3 + campaign.json
        core_a = SAMPLE_CORE + "\nCAMPAIGN_RUN_ID: old-run-a\n"
        second_a = SAMPLE_SECOND + "\nCAMPAIGN_RUN_ID: old-run-a\n"
        perf_a = SAMPLE_PERF + "\nCAMPAIGN_RUN_ID: old-run-a\n"
        all3_a = SAMPLE_ALL_3 + "\nCAMPAIGN_RUN_ID: old-run-a\n"
        (proj_dir / "Scorecard__01_AUDIT_CORE.md").write_text(core_a, encoding="utf-8")
        (proj_dir / "Scorecard__02_AUDIT_SECOND_WAVE.md").write_text(second_a, encoding="utf-8")
        (proj_dir / "Scorecard__03_AUDIT_PERFORMANCE.md").write_text(perf_a, encoding="utf-8")
        (proj_dir / "Scorecard__00_AUDIT_ALL_3.md").write_text(all3_a, encoding="utf-8")
        old_index = {
            "schema_version": 1, "campaign_run_id": "old-run-a",
            "campaign_profile": "quick3", "campaign_status": "CAMPAIGN_COMPLETE",
            "completed_count": 3, "completed_waves": ["core", "second", "performance"],
            "final_handoff": "Scorecard__00_AUDIT_ALL_3.md",
            "wave_count": 3, "project_name": "Scorecard",
        }
        (proj_dir / "campaign.json").write_text(json.dumps(old_index), encoding="utf-8")

        project = Project(
            id="scorecard", display_name="Scorecard",
            source_path=r"C:\Scorecard", priority_group="MAIN0", slot=1,
            audit_project_name="Scorecard",
        )
        now = datetime(2026, 8, 29, 1, 0, 0)

        # Precondition: old run shows 3/3 complete.
        snap_old = self.indexer.scan_project(project, now=now)
        self.assertEqual(snap_old.completed_waves, 3)
        self.assertTrue(snap_old.campaign_complete)
        self.assertTrue(snap_old.final_handoff_ready)

        # New run B: only Core posted, campaign.json says 1/3.
        core_b = SAMPLE_CORE + "\nCAMPAIGN_RUN_ID: new-run-b\n"
        (proj_dir / "Scorecard__01_AUDIT_CORE.md").write_text(core_b, encoding="utf-8")
        new_index = {
            "schema_version": 1, "campaign_run_id": "new-run-b",
            "campaign_profile": "quick3", "campaign_status": "CAMPAIGN_READY_FOR_WAVE",
            "completed_count": 1, "completed_waves": ["core"],
            "final_handoff": "",
            "wave_count": 3, "project_name": "Scorecard",
        }
        (proj_dir / "campaign.json").write_text(json.dumps(new_index), encoding="utf-8")

        snap_new = self.indexer.scan_project(project, now=now)
        self.assertEqual(snap_new.completed_waves, 1, "new run must report 1/3, not 3")
        self.assertFalse(snap_new.campaign_complete, "new run must not be complete")
        self.assertFalse(snap_new.final_handoff_ready, "old ALL_3 must not be preferred")
        # The old ALL_3 still exists on disk; verify it's not exposed.
        if snap_new.final_handoff_path:
            self.assertNotIn("run-a", str(snap_new.final_handoff_path), "old final handoff must not be preferred")
        # After completing the new run, final_handoff_ready becomes true.
        new_index["campaign_status"] = "CAMPAIGN_COMPLETE"
        new_index["completed_count"] = 3
        new_index["completed_waves"] = ["core", "second", "performance"]
        new_index["final_handoff"] = "Scorecard__00_AUDIT_ALL_3.md"
        (proj_dir / "campaign.json").write_text(json.dumps(new_index), encoding="utf-8")
        snap_final = self.indexer.scan_project(project, now=now)
        self.assertTrue(snap_final.campaign_complete)
        self.assertTrue(snap_final.final_handoff_ready)


if __name__ == "__main__":
    unittest.main()
