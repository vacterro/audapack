"""The audit signature cache must actually skip the reads it claims to skip (T-22).

T-22 asked for a canonical audit-path index plus a lightweight snapshot cache,
with "repeated file reads removed" as its acceptance half. The implementation
exists (audapack/audits.py: _get_dir_signatures caches (st_size, st_mtime_ns)
per file and scan_project returns the cached snapshot when the signature map is
unchanged), but no test referenced either symbol, so the claim could not fail.
A gate that cannot fail is not a gate (VERIFY-ORACLE-01).

These tests count real content reads through pathlib, so an implementation that
re-reads every wave file on every temperature tick is caught rather than
believed.
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# Make the project importable when running this file directly.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from audapack.audits import AuditIndexer  # noqa: E402
from audapack.config import AppConfig  # noqa: E402
from audapack.models import Project  # noqa: E402

# Copied verbatim from tests/test_audits.py: the wave-completeness recognizer is
# format-sensitive, and a paraphrased fixture would silently assert nothing.
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


class TestAuditSignatureCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_root = Path(self.temp_dir) / "AUDITING_IMPLEMENTATION"
        self.proj_dir = self.audit_root / "MAIN0" / "FastPrompter"
        self.proj_dir.mkdir(parents=True)
        self.config = AppConfig()
        self.config.audits.root = str(self.audit_root)
        self.indexer = AuditIndexer(self.config)
        self.project = Project(
            id="fastprompter",
            display_name="FastPrompter",
            source_path=r"C:\FastPrompter",
            priority_group="MAIN0",
            slot=1,
            audit_project_name="FastPrompter",
        )
        self.now = datetime(2026, 8, 26, 3, 5, 0)
        self.core_file = self.proj_dir / "FastPrompter__01_AUDIT_CORE.md"
        self.second_file = self.proj_dir / "FastPrompter__02_AUDIT_SECOND_WAVE.md"
        self.core_file.write_text(SAMPLE_CORE, encoding="utf-8")
        self.second_file.write_text(SAMPLE_SECOND, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _count_content_reads(self, fn):
        """Run `fn`, returning its result and which audit-dir files it read."""
        real_read_text = Path.read_text
        reads = []
        # Bound here: inside the wrapper `self` is the Path being read, so
        # reaching for self.audit_root would raise AttributeError inside the
        # indexer's own `except Exception: pass` and silently zero the scan.
        audit_root = self.audit_root

        def counting_read_text(target, *args, **kwargs):
            try:
                target.resolve().relative_to(audit_root)
            except ValueError:
                pass
            else:
                reads.append(target.name)
            return real_read_text(target, *args, **kwargs)

        Path.read_text = counting_read_text
        try:
            result = fn()
        finally:
            Path.read_text = real_read_text
        return result, reads

    def test_first_scan_reads_the_wave_files(self):
        # Establishes that the fixture really exercises the read path, so a
        # later zero-read assertion means "skipped", never "read nothing".
        snapshot, reads = self._count_content_reads(
            lambda: self.indexer.scan_project(self.project, now=self.now)
        )
        self.assertEqual(snapshot.completed_waves, 2)
        self.assertIn(self.core_file.name, reads)
        self.assertIn(self.second_file.name, reads)

    def test_unchanged_signature_skips_every_content_read(self):
        self.indexer.scan_project(self.project, now=self.now)
        snapshot, reads = self._count_content_reads(
            lambda: self.indexer.scan_project(self.project, now=self.now)
        )
        self.assertEqual(reads, [], "an unchanged signature must read no audit file")
        self.assertEqual(snapshot.completed_waves, 2)

    def test_changed_signature_forces_a_rescan(self):
        first, reads = self._count_content_reads(
            lambda: self.indexer.scan_project(self.project, now=self.now)
        )
        self.assertEqual(first.completed_waves, 2)
        self.assertIn(self.second_file.name, reads)

        # A size+content change must move the signature and force a real rescan.
        self.second_file.write_text(
            SAMPLE_SECOND + "\n# Extra Finding\n", encoding="utf-8"
        )
        _, after_reads = self._count_content_reads(
            lambda: self.indexer.scan_project(self.project, now=self.now)
        )
        self.assertIn(self.second_file.name, after_reads)

        # A signature blind to membership would keep answering from the cache
        # forever, so dropping the wave file must lose a wave.
        self.second_file.unlink()
        dropped, _ = self._count_content_reads(
            lambda: self.indexer.scan_project(self.project, now=self.now)
        )
        self.assertEqual(dropped.completed_waves, 1)

    def test_the_cache_is_keyed_per_project(self):
        self.indexer.scan_project(self.project, now=self.now)
        other_dir = self.audit_root / "MAIN0" / "OtherProject"
        other_dir.mkdir(parents=True)
        (other_dir / "OtherProject__01_AUDIT_CORE.md").write_text(
            SAMPLE_CORE, encoding="utf-8"
        )
        other = Project(
            id="other",
            display_name="OtherProject",
            source_path=r"C:\OtherProject",
            priority_group="MAIN0",
            slot=2,
            audit_project_name="OtherProject",
        )
        snapshot = self.indexer.scan_project(other, now=self.now)
        self.assertEqual(snapshot.project_id, "other")
        self.assertEqual(snapshot.completed_waves, 1)


if __name__ == "__main__":
    unittest.main()
