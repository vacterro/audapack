"""Unit and integration tests for AUDAPACK Bridge HTTP server and storage."""

import json
import os
import secrets
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from audapack.bridge.server import AudapackBridgeHandler
from audapack.bridge.state import get_run_state
from audapack.bridge.storage import (
    InvalidProjectPathError,
    ensure_contained,
    parse_wave,
    resolve_project_audit_dir,
    sanitize_project_name,
)
from audapack.config import AppConfig, save_config
from audapack.models import Project


class TestAudapackBridge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_root = Path(self.temp_dir) / "AUDITING_IMPLEMENTATION"
        self.audit_root.mkdir(parents=True)

        self.config = AppConfig()
        self.config.audits.root = str(self.audit_root)
        self.config.bridge.host = "127.0.0.1"
        self.config.bridge.port = 18942  # Test port
        self.config.bridge.token = "test_secret_token_123456789"
        self.config.projects = [
            Project(
                id="saipen",
                display_name="SAIPEN",
                source_path=str(Path(self.temp_dir) / "SAIPEN"),
                priority_group="MAIN0",
                slot=1,
            )
        ]

        class TestHandler(AudapackBridgeHandler):
            pass

        TestHandler.config = self.config
        TestHandler.test_base_dir = self.temp_dir
        save_config(self.config, base_dir=self.temp_dir)
        self.server = ThreadingHTTPServer((self.config.bridge.host, self.config.bridge.port), TestHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _url(self, path: str) -> str:
        return f"http://{self.config.bridge.host}:{self.config.bridge.port}{path}"

    def test_health_endpoint(self):
        req = urllib.request.Request(self._url("/health"), method="GET")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("service"), "AUDAPACK Bridge")

    def test_status_endpoint_auth(self):
        # 1. No token -> 403
        req_no_auth = urllib.request.Request(self._url("/v1/status"), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_no_auth)
        self.assertEqual(ctx.exception.code, 403)

        # 2. Valid token -> 200
        req_auth = urllib.request.Request(
            self._url("/v1/status"),
            method="GET",
            headers={"X-ACB-Token": self.config.bridge.token},
        )
        with urllib.request.urlopen(req_auth) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("output_exists"))

    def test_audit_submission_flow_and_all3(self):
        run_id = f"test_run_{secrets.token_hex(4)}"
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }

        # Wave 1: Core
        core_content = """PROJECT_NAME: SAIPEN
DATE_TIME: 2026-08-26T00:00:00
WAVE: AUDIT CORE
TARGET: SAIPEN repo
BASELINE: v1.0
STATUS: AUDIT_CORE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [CORE-001] audapack/core.py
EVIDENCE: broken loop
DEFECT: off by one
REPAIR: fix index
VERIFY: unit test

CORE_DONE_WHEN: tests pass"""

        payload_core = {
            "run_id": run_id,
            "project": "SAIPEN",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_001",
            "content": core_content,
        }

        req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_core).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertFalse(data.get("duplicate"))
            self.assertFalse(data.get("all3_ready"))

        # Verify disk write to priority group MAIN0 / SAIPEN
        proj_dir = self.audit_root / "MAIN0" / "SAIPEN"
        self.assertTrue(proj_dir.exists())
        self.assertTrue((proj_dir / "SAIPEN__01_AUDIT_CORE.md").exists())

        # Test duplicate receipt (idempotency)
        req_dup = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_core).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req_dup) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("duplicate"))

        # Test receipt conflict (same receipt, different content)
        conflict_payload = dict(payload_core)
        conflict_payload["content"] = core_content + "\n# Modified"
        req_conf = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(conflict_payload).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_conf)
        self.assertEqual(ctx.exception.code, 409)

        # Wave 2: Second Wave
        second_content = """PROJECT_NAME: SAIPEN
DATE_TIME: 2026-08-26T00:00:00
WAVE: AUDIT SECOND WAVE
TARGET: SAIPEN repo
BASELINE: v1.0
CORE_BASELINE: v1.0
STATUS: SECOND_WAVE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P2] [W2-001] audapack/second.py
EVIDENCE: missing null check
DEFECT: crash on null
REPAIR: add guard
VERIFY: test null input

SECOND_WAVE_DONE_WHEN: tests pass"""

        payload_second = {
            "run_id": run_id,
            "project": "SAIPEN",
            "wave": "second",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_002",
            "content": second_content,
        }
        req2 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_second).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req2) as resp:
            self.assertEqual(resp.status, 200)

        # Wave 3: Performance -> triggers ALL_3 generation
        perf_content = """PROJECT_NAME: SAIPEN
DATE_TIME: 2026-08-26T00:00:00
WAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS
TARGET: SAIPEN repo
BASELINE: v1.0
PREVIOUS_BASELINE: v1.0
STATUS: PERFORMANCE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P2] [PERF-001] LOW-RISK SIMPLIFICATION audapack/perf.py
EVIDENCE: quadratic lookup
ISSUE: slow scan
OPTIMIZE: use set
GUARDRAIL: preserve ordering
VERIFY: benchmark

PERFORMANCE_DONE_WHEN: benchmark passes"""

        payload_perf = {
            "run_id": run_id,
            "project": "SAIPEN",
            "wave": "performance",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_003",
            "content": perf_content,
        }
        req3 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_perf).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("all3_ready"))

        # Verify canonical ALL_3 exists
        all3_file = proj_dir / "SAIPEN__00_AUDIT_ALL_3.md"
        self.assertTrue(all3_file.exists())
        all3_text = all3_file.read_text(encoding="utf-8")
        self.assertIn("TOTAL_TICKETS: 3", all3_text)
        self.assertIn("## 01 — AUDIT CORE", all3_text)
        self.assertIn("## 02 — AUDIT SECOND WAVE", all3_text)
        self.assertIn("## 03 — AUDIT PERFORMANCE", all3_text)

    def test_registry_endpoint(self):
        headers = {"X-ACB-Token": self.config.bridge.token}
        req = urllib.request.Request(self._url("/v1/registry"), headers=headers)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertIn("projects", data)
            self.assertIn("groups", data)
            projects = data["projects"]
            self.assertTrue(any(p["display_name"] == "SAIPEN" and p["group"] == "MAIN0" for p in projects))

    def test_resolve_and_auto_registration_side1_and_side2_growth(self):
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }

        # 1. Existing project -> matched in MAIN0, created=False
        req_match = urllib.request.Request(
            self._url("/v1/projects/resolve"),
            data=json.dumps({"project_name": "SAIPEN"}).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req_match) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["group"], "MAIN0")
            self.assertEqual(data["slot"], 1)
            self.assertFalse(data["created"])

        # 2. New project 1 -> auto-registered into SIDE1 slot 1
        req_new1 = urllib.request.Request(
            self._url("/v1/projects/resolve"),
            data=json.dumps({"project_name": "NewTool1"}).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req_new1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["group"], "SIDE1")
            self.assertEqual(data["slot"], 1)
            self.assertTrue(data["created"])

        # 3. Fill remaining slots 2..6 in SIDE1
        for s in range(2, 7):
            req_fill = urllib.request.Request(
                self._url("/v1/projects/resolve"),
                data=json.dumps({"project_name": f"Side1_Fill_{s}"}).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req_fill) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["group"], "SIDE1")
                self.assertEqual(data["slot"], s)

        # 4. Next project must auto-grow into SIDE2 slot 1!
        req_side2 = urllib.request.Request(
            self._url("/v1/projects/resolve"),
            data=json.dumps({"project_name": "OverflowProject"}).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req_side2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["group"], "SIDE2")
            self.assertEqual(data["slot"], 1)
            self.assertTrue(data["created"])

    def test_run_project_mismatch_409(self):
        run_id = f"test_run_mismatch_{secrets.token_hex(4)}"
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }

        # Wave 1 for SAIPEN
        payload1 = {
            "run_id": run_id,
            "project": "SAIPEN",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_m1",
            "content": "PROJECT_NAME: SAIPEN\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] test.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: tests pass",
        }
        req1 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload1).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req1) as resp:
            self.assertEqual(resp.status, 200)

        # Wave 2 with different project -> must reject 409
        payload2 = {
            "run_id": run_id,
            "project": "FastPrompter",
            "wave": "second",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_m2",
            "content": "PROJECT_NAME: FastPrompter\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\nTICKETS: 1\n[P1] [W2-001] test.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nSECOND_WAVE_DONE_WHEN: tests pass",
        }
        req2 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload2).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req2)
        self.assertEqual(ctx.exception.code, 409)

    def test_receipt_duplicate_and_conflict(self):
        run_id = f"test_run_rcpt_{secrets.token_hex(4)}"
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        content = "PROJECT_NAME: SAIPEN\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] test.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: tests pass"
        payload = {
            "run_id": run_id,
            "project": "SAIPEN",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_dup_1",
            "content": content,
        }

        # 1. Initial write -> 200
        req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertFalse(data["duplicate"])

        # 2. Duplicate with identical content -> 200 duplicate=True
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertTrue(data["duplicate"])

        # 3. Same receipt with different content -> 409 conflict
        payload_conf = dict(payload)
        payload_conf["content"] = content + "\nMODIFIED: different content"
        req_conf = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_conf).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_conf)
        self.assertEqual(ctx.exception.code, 409)

    def test_single_history_directory_per_run(self):
        run_id = f"test_run_hist_{secrets.token_hex(4)}"
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }

        for w_name, w_hdr, w_done, w_pfx, w_fields in [
            ("core", "WAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE", "CORE_DONE_WHEN: ok", "CORE-001",
             "EVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v"),
            ("second", "WAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE", "SECOND_WAVE_DONE_WHEN: ok", "W2-001",
             "EVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v"),
            ("performance", "WAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nSTATUS: PERFORMANCE: COMPLETE", "PERFORMANCE_DONE_WHEN: ok", "PERF-001",
             "EVIDENCE: e\nISSUE: i\nOPTIMIZE: o\nGUARDRAIL: g\nVERIFY: v"),
        ]:
            p = {
                "run_id": run_id,
                "project": "SAIPEN",
                "wave": w_name,
                "status": "complete",
            "api_version": 2,
                "receipt": f"rcpt_{w_name}",
                "content": f"PROJECT_NAME: SAIPEN\n{w_hdr}\nTICKETS: 1\n[P1] [{w_pfx}] test.py\n{w_fields}\n{w_done}",
            }
            req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(p).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)

        # Inspect history folders
        hist_root = self.audit_root / "MAIN0" / "SAIPEN" / "_history"
        self.assertTrue(hist_root.exists())
        subdirs = [d for d in hist_root.iterdir() if d.is_dir()]
        self.assertEqual(len(subdirs), 1, "All waves of a single run must reside in the exact same history directory")
        hist_files = [f.name for f in subdirs[0].iterdir() if f.is_file()]
        self.assertTrue(any("01_AUDIT_CORE" in f for f in hist_files))
        self.assertTrue(any("02_AUDIT_SECOND_WAVE" in f for f in hist_files))
        self.assertTrue(any("03_AUDIT_PERFORMANCE" in f for f in hist_files))
        self.assertTrue(any("00_AUDIT_ALL_3" in f for f in hist_files))

    def test_queued_audit_after_project_move(self):
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        # 1. Register project BananaTool -> auto goes to SIDE1
        req_reg = urllib.request.Request(self._url("/v1/projects/resolve"), data=json.dumps({"project_name": "BananaTool"}).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req_reg) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["group"], "SIDE1")

        # 2. Simulate User moving BananaTool from SIDE1 to MAIN1 via the registry
        #    transaction (disk is the canonical registry state).
        from audapack.projects import ProjectRegistry
        registry = ProjectRegistry(self.config, base_dir=self.temp_dir)
        banana_proj = self.config.get_project_by_name_or_audit("BananaTool")
        self.assertIsNotNone(banana_proj)
        self.assertTrue(registry.move_project(banana_proj.id, "MAIN1", 5))

        # 3. Queued audit arriving now must be written to MAIN1\BananaTool!
        run_id = f"test_run_move_{secrets.token_hex(4)}"
        payload = {
            "run_id": run_id,
            "project_name": "BananaTool",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_move_1",
            "content": "PROJECT_NAME: BananaTool\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n[P1] [CORE-001] banana.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\nCORE_DONE_WHEN: done",
        }
        req_audit = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req_audit) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["group"], "MAIN1")

        # Verify disk path
        target_file = self.audit_root / "MAIN1" / "BananaTool" / "BananaTool__01_AUDIT_CORE.md"
        self.assertTrue(target_file.exists())

    def test_strict_wave_validation_rejects_incomplete(self):
        run_id = f"test_run_inval_{secrets.token_hex(4)}"
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }

        # Missing DONE_WHEN marker -> reject 400
        payload_bad = {
            "run_id": run_id,
            "project": "SAIPEN",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_bad",
            "content": "PROJECT_NAME: SAIPEN\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 1\n(Missing done marker)",
        }
        req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_bad).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_traversal_audit_delivery_writes_inside_root(self):
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        payload = {
            "run_id": f"test_run_trav_{secrets.token_hex(4)}",
            "project": "../../escape",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_trav_1",
            "content": (
                "PROJECT_NAME: ../../escape\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
                "TICKETS: 1\n[P1] [CORE-001] x.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
                "CORE_DONE_WHEN: done"
            ),
        }
        req = urllib.request.Request(
            self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data["ok"])

        resolved_root = self.audit_root.resolve()
        written = None
        for candidate in resolved_root.rglob("*"):
            if candidate.is_file() and "escape" in candidate.name:
                written = candidate
                break
        self.assertIsNotNone(written)
        self.assertTrue(written.resolve().is_relative_to(resolved_root))
        # Nothing but the registry's own config.json / lock file may appear beside
        # the audit root.
        allowed_extras = {"config.json", "registry.lock", "token.txt"}
        outside = [
            p
            for p in Path(self.temp_dir).iterdir()
            if p != self.audit_root and p.name not in allowed_extras
        ]
        self.assertEqual(outside, [])

    def test_autostart_command_and_task_detection(self):
        from audapack.components.autostart import get_autostart_status, get_canonical_autostart_command
        cmd = get_canonical_autostart_command()
        self.assertIn("AUDAPACK.pyw", cmd)
        self.assertIn("--bridge", cmd)
        self.assertNotIn("_AICHATBUTTONS", cmd)

        st = get_autostart_status()
        self.assertIn("installed", st)
        self.assertIn("status_text", st)

    def test_wrong_api_version_rejected_permanently(self):
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        payload = {
            "run_id": f"test_run_apiver_{secrets.token_hex(4)}",
            "project": "SAIPEN",
            "wave": "core",
            "status": "complete",
            "api_version": 1,  # legacy widget
            "receipt": "rcpt_apiv1",
            "content": (
                "PROJECT_NAME: SAIPEN\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
                "TICKETS: 1\n[P1] [CORE-001] x.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
                "CORE_DONE_WHEN: done"
            ),
        }
        req = urllib.request.Request(
            self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"]["code"], "unsupported_api_version")
        self.assertFalse(body["error"]["retriable"])

        # Missing version is equally rejected (never guessed).
        del payload["api_version"]
        payload["receipt"] = "rcpt_apivnone"
        req2 = urllib.request.Request(
            self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            urllib.request.urlopen(req2)
        body2 = json.loads(ctx2.exception.read().decode("utf-8"))
        self.assertEqual(body2["error"]["code"], "unsupported_api_version")

    def test_run_bound_to_project_id_and_alias_accepted(self):
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        run_id = f"test_run_bind_{secrets.token_hex(4)}"

        def _payload(wave, receipt, name):
            w = {"core": ("WAVE: AUDIT CORE", "STATUS: AUDIT_CORE: COMPLETE", "[CORE-001]", "CORE_DONE_WHEN: ok"),
                 "second": ("WAVE: AUDIT SECOND WAVE", "STATUS: SECOND_WAVE: COMPLETE", "[W2-001]", "SECOND_WAVE_DONE_WHEN: ok")}[
                wave
            ]
            return {
                "run_id": run_id,
                "project": name,
                "wave": wave,
                "status": "complete",
            "api_version": 2,
                "receipt": receipt,
                "content": (
                    f"PROJECT_NAME: {name}\n{w[0]}\n{w[1]}\nTICKETS: 1\n[P1] {w[2]} x.py\n"
                    f"EVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n{w[3]}"
                ),
            }

        # Wave 1 binds the run to SAIPEN (canonical project_id).
        req1 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(_payload("core", "r_b1", "SAIPEN")).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data["ok"])

        # Formatting variant of the SAME identity is accepted (no false mismatch).
        req2 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(_payload("second", "r_b2", "s a i p e n")).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req2) as resp:
            data2 = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data2["ok"])
        self.assertEqual(data2["project_id"], data["project_id"])

        # A different canonical identity on the same run -> 409, no write.
        payload_conflict = _payload("second", "r_b3", "BananaTool")
        req3 = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload_conflict).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req3)
        self.assertEqual(ctx.exception.code, 409)

    def test_payload_project_id_vs_handoff_name_conflict_409_no_write(self):
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        # Register BananaTool so it has a canonical id.
        req_reg = urllib.request.Request(
            self._url("/v1/projects/resolve"),
            data=json.dumps({"project_name": "BananaTool"}).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req_reg) as resp:
            reg_data = json.loads(resp.read().decode("utf-8"))
        banana_id = reg_data["project_id"]

        run_id = f"test_run_conf_{secrets.token_hex(4)}"
        payload = {
            "run_id": run_id,
            "project": "SAIPEN",
            "project_id": banana_id,  # conflicts with handoff PROJECT_NAME
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_conf_1",
            "content": (
                "PROJECT_NAME: SAIPEN\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
                "TICKETS: 1\n[P1] [CORE-001] x.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
                "CORE_DONE_WHEN: done"
            ),
        }
        req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 409)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"]["code"], "project_identity_conflict")

        # No physical write for the conflicted run.
        self.assertFalse(any(run_id in f.name for f in self.audit_root.rglob("*")))

    def test_payload_project_name_vs_handoff_name_conflict_409_no_write(self):
        """CORE-005: first-wave payload project and handoff PROJECT_NAME are two
        explicit identities; a real conflict must be rejected with 409 before any
        registration/file/state mutation — even without a project_id."""
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        # Register ALPHA and BETA as existing projects.
        for name in ("ALPHA", "BETA"):
            req_reg = urllib.request.Request(
                self._url("/v1/projects/resolve"),
                data=json.dumps({"project_name": name}).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req_reg) as resp:
                json.loads(resp.read().decode("utf-8"))

        run_id = f"test_run_name_conf_{secrets.token_hex(4)}"
        payload = {
            "run_id": run_id,
            "project": "ALPHA",
            # No project_id supplied.
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_name_conf_1",
            "content": (
                "PROJECT_NAME: BETA\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
                "TICKETS: 1\n[P1] [CORE-001] x.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
                "CORE_DONE_WHEN: done"
            ),
        }
        req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 409)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"]["code"], "project_identity_conflict")

        # No physical write for the conflicted run, no BETA artifact.
        self.assertFalse(any(run_id in f.name for f in self.audit_root.rglob("*")))
        beta_audit_dir = self.audit_root / "MAIN0" / "BETA"
        self.assertFalse(beta_audit_dir.exists(), "BETA must not receive the wave")

    def test_payload_unknown_name_vs_handoff_existing_conflict_409(self):
        """CORE-005: a payload that names nothing known must not let handoff
        metadata silently reroute/register an existing project."""
        headers = {
            "Content-Type": "application/json",
            "X-ACB-Token": self.config.bridge.token,
        }
        req_reg = urllib.request.Request(
            self._url("/v1/projects/resolve"),
            data=json.dumps({"project_name": "GAMMA"}).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req_reg) as resp:
            json.loads(resp.read().decode("utf-8"))

        run_id = f"test_run_unknown_{secrets.token_hex(4)}"
        payload = {
            "run_id": run_id,
            "project": "NoSuchProject",
            "wave": "core",
            "status": "complete",
            "api_version": 2,
            "receipt": "rcpt_unknown_1",
            "content": (
                "PROJECT_NAME: GAMMA\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
                "TICKETS: 1\n[P1] [CORE-001] x.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
                "CORE_DONE_WHEN: done"
            ),
        }
        req = urllib.request.Request(self._url("/v1/audits"), data=json.dumps(payload).encode("utf-8"), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 409)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"]["code"], "project_identity_conflict")
        self.assertFalse(any(run_id in f.name for f in self.audit_root.rglob("*")))


class TestBridgeAuthHygiene(unittest.TestCase):
    """WJ-002: auth surface must be canonical and migration-scoped, never user-hard-coded."""

    def test_no_hard_coded_user_paths_in_server(self):
        source = Path(AudapackBridgeHandler.__module__.replace(".", "/")).with_suffix(".py")
        server_src = (
            Path(__file__).resolve().parent.parent / "audapack" / "bridge" / "server.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("vac34", server_src)
        self.assertNotIn("C:\\Users\\", server_src)
        del source

    def test_legacy_candidates_env_based_and_revocable(self):
        import tempfile as _tempfile
        from unittest import mock


        handler = AudapackBridgeHandler.__new__(AudapackBridgeHandler)
        fake_local = _tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": fake_local}):
                candidates = handler._legacy_token_candidates()
                self.assertEqual(
                    [c.name for c in candidates],
                    ["token.txt", "token.txt"],
                )
                self.assertTrue(all("LOCALAPPDATA" not in str(c) for c in candidates))
                self.assertTrue(all(fake_local in str(c) for c in candidates))

                # Revocation marker lives under the runtime secrets dir; patch its base too.
                with mock.patch(
                    "audapack.bridge.server.legacy_token_acceptance_revoked", return_value=True
                ):
                    self.assertEqual(handler._legacy_token_candidates(), [])
        finally:
            shutil.rmtree(fake_local, ignore_errors=True)


class TestAuditPathContainment(unittest.TestCase):
    """WJ-001: no project identity may resolve an audit destination outside the audit root."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_root = Path(self.temp_dir) / "AUDITING_IMPLEMENTATION"
        self.audit_root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _fresh_config(self) -> AppConfig:
        config = AppConfig()
        config.audits.root = str(self.audit_root)
        return config

    def test_filesystem_name_matrix(self):
        cases = {
            "../../escape": "_.._escape",
            "..\\..\\escape": "_.._escape",
            "C:\\Temp\\Foo": "C__Temp_Foo",
            "\\\\server\\share": "__server_share",
            ".": "UNKNOWN_PROJECT",
            "..": "UNKNOWN_PROJECT",
            "CON": "CON_",
            "name.": "name",
            "name<bad>": "name_bad_",
            "foo/bar": "foo_bar",
            "foo\\bar": "foo_bar",
            "Проект Аудит": "Проект Аудит",
            "Normal Project": "Normal Project",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_project_name(raw), expected)

    def test_resolved_destination_stays_inside_audit_root(self):
        hostile_and_valid_names = [
            "../../escape",
            "..\\..\\escape",
            "C:\\Temp\\Foo",
            "\\\\server\\share",
            ".",
            "..",
            "CON",
            "name.",
            "name<bad>",
            "foo/bar",
            "foo\\bar",
            "Проект Аудит",
            "Normal Project",
        ]
        resolved_root = self.audit_root.resolve()
        with tempfile.TemporaryDirectory() as base_tmp:
            base_dir = Path(base_tmp)
            for raw in hostile_and_valid_names:
                with self.subTest(raw=raw):
                    target_dir, _resolved_name, _proj, _created = resolve_project_audit_dir(
                        self._fresh_config(), raw, base_dir=base_dir
                    )
                    resolved_target = target_dir.resolve()
                    self.assertTrue(
                        resolved_target.is_relative_to(resolved_root),
                        f"{raw!r} resolved to {resolved_target}, outside {resolved_root}",
                    )
            # No write escaped the root for any case.
            outside = [p for p in Path(self.temp_dir).iterdir() if p != self.audit_root]
            self.assertEqual(outside, [])

    def test_ensure_contained_rejects_escape(self):
        inside = ensure_contained(self.audit_root / "MAIN0" / "Proj", self.audit_root)
        self.assertTrue(inside.is_relative_to(self.audit_root.resolve()))

        with self.assertRaises(InvalidProjectPathError):
            ensure_contained(self.audit_root.parent / "escape", self.audit_root)
        with self.assertRaises(InvalidProjectPathError):
            ensure_contained(Path(self.temp_dir), self.audit_root)

    def test_symlinked_project_directory_cannot_escape(self):
        escape_target = Path(self.temp_dir) / "outside"
        escape_target.mkdir()
        link_dir = self.audit_root / "SIDE1" / "EvilLink"
        link_dir.parent.mkdir(parents=True)
        try:
            link_dir.symlink_to(escape_target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")

        config = self._fresh_config()
        config.projects = [
            Project(
                id="evillink",
                display_name="EvilLink",
                source_path="",
                priority_group="SIDE1",
                slot=1,
            )
        ]
        with self.assertRaises(InvalidProjectPathError):
            resolve_project_audit_dir(config, "EvilLink", project_id="evillink")
        # The gate rejected before any canonical write; escape target untouched.
        self.assertEqual(list(escape_target.iterdir()), [])


class TestStrictWaveValidation(unittest.TestCase):
    """WJ-005: only exact structurally complete waves pass parse_wave."""

    def setUp(self):
        self.parse = parse_wave

        self.core_ok = (
            "PROJECT_NAME: X\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
            "TICKETS: 1\n[P1] [CORE-001] a.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
            "CORE_DONE_WHEN: done"
        )
        self.second_ok = (
            "PROJECT_NAME: X\nWAVE: AUDIT SECOND WAVE\nSTATUS: SECOND_WAVE: COMPLETE\n"
            "TICKETS: 0\nNO NEW VERIFIED SECOND-WAVE DEFECTS.\nSECOND_WAVE_DONE_WHEN: nothing to do"
        )
        self.perf_ok = (
            "PROJECT_NAME: X\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\n"
            "STATUS: PERFORMANCE: COMPLETE\nTICKETS: 2\n"
            "[P2] [PERF-001] a.py\nEVIDENCE: e1\nISSUE: i1\nOPTIMIZE: o1\nGUARDRAIL: g1\nVERIFY: v1\n"
            "[P3] [PERF-002] b.py\nEVIDENCE: e2\nISSUE: i2\nOPTIMIZE: o2\nGUARDRAIL: g2\nVERIFY: v2\n"
            "PERFORMANCE_DONE_WHEN: done"
        )

    def _reject(self, content, wave, fragment):
        ok, meta, err = self.parse(content, wave)
        self.assertFalse(ok, f"expected rejection ({fragment}): {err}")
        self.assertIn(fragment, err)

    def test_accepts_structurally_correct_waves(self):
        for wave, content in (("core", self.core_ok), ("second", self.second_ok), ("performance", self.perf_ok)):
            with self.subTest(wave=wave):
                ok, meta, err = self.parse(content, wave)
                self.assertTrue(ok, err)
                self.assertIsNotNone(meta)

    def test_rejects_core_without_status(self):
        broken = self.core_ok.replace("STATUS: AUDIT_CORE: COMPLETE\n", "")
        self._reject(broken, "core", "Missing terminal STATUS")

    def test_rejects_performance_text_as_core(self):
        self._reject(self.perf_ok, "core", "Wrong WAVE header")

    def test_rejects_second_text_as_performance(self):
        self._reject(self.second_ok, "performance", "Wrong WAVE header")

    def test_rejects_wrong_wave_header(self):
        broken = self.core_ok.replace("WAVE: AUDIT CORE", "WAVE: AUDIT CORES")
        self._reject(broken, "core", "Wrong WAVE header")

    def test_rejects_wrong_terminal_status(self):
        broken = self.core_ok.replace("STATUS: AUDIT_CORE: COMPLETE", "STATUS: AUDIT_CORE: IN_PROGRESS")
        self._reject(broken, "core", "Wrong terminal STATUS")

    def test_rejects_wrong_done_marker(self):
        broken = self.core_ok.replace("CORE_DONE_WHEN:", "SECOND_WAVE_DONE_WHEN:")
        self._reject(broken, "core", "Missing CORE_DONE_WHEN:")

    def test_rejects_empty_done_marker_value(self):
        broken = self.core_ok.replace("CORE_DONE_WHEN: done", "CORE_DONE_WHEN:")
        self._reject(broken, "core", "Missing CORE_DONE_WHEN:")

    def test_rejects_negative_tickets(self):
        broken = self.core_ok.replace("TICKETS: 1", "TICKETS: -1")
        self._reject(broken, "core", "Negative TICKETS")

    def test_rejects_ticket_count_mismatch(self):
        broken = self.perf_ok.replace("[P3] [PERF-002] b.py\n", "").replace("TICKETS: 2", "TICKETS: 2 ")
        # count says 2, unique PERF id blocks now 1
        ok, _meta, err = self.parse(broken.rstrip(), "performance")
        self.assertFalse(ok)
        self.assertIn("Expected 2 unique PERF- ticket blocks", err)

    def test_rejects_other_wave_tickets_as_core_evidence(self):
        foreign = (
            "PROJECT_NAME: X\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\n"
            "TICKETS: 1\n[P1] [W2-001] b.py\nEVIDENCE: e\nDEFECT: d\nREPAIR: r\nVERIFY: v\n"
            "CORE_DONE_WHEN: done"
        )
        self._reject(foreign, "core", "Expected 1 unique CORE- ticket blocks")

    def test_zero_ticket_terminal_form_accepted(self):
        ok, meta, err = self.parse(self.second_ok, "second")
        self.assertTrue(ok, err)
        self.assertEqual(meta["tickets"], 0)


class TestRunIdentity(unittest.TestCase):
    """WJ-006: hashed run-state identity + immutable project_id binding."""

    def test_colliding_sanitized_run_ids_remain_independent(self):
        from audapack.bridge.state import canonical_run_key, save_run_state
        # Both truncate to the SAME legacy sanitized form ("r" x 64).
        run_a = "r" * 70 + "A"
        run_b = "r" * 70 + "B"
        from audapack.bridge.state import sanitize_run_id
        self.assertEqual(sanitize_run_id(run_a), sanitize_run_id(run_b))
        self.assertNotEqual(canonical_run_key(run_a), canonical_run_key(run_b))

        base = Path(tempfile.mkdtemp())
        try:
            save_run_state(run_a, {"run_id": run_a, "marker": "A"}, base)
            save_run_state(run_b, {"run_id": run_b, "marker": "B"}, base)

            sa = get_run_state(run_a, base)
            sb = get_run_state(run_b, base)
            self.assertEqual(sa["marker"], "A")
            self.assertEqual(sb["marker"], "B")
            files = list((base / "runs").glob("run_*.json"))
            self.assertEqual(len(files), 2)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_legacy_sanitized_state_file_migrates_forward(self):
        from audapack.bridge.state import canonical_run_key
        base = Path(tempfile.mkdtemp())
        try:
            runs = base / "runs"
            runs.mkdir(parents=True)
            run_id = "legacy/run:id"
            (runs / "legacy_run_id.json").write_text(
                json.dumps({"run_id": run_id, "project": "OLD"}), encoding="utf-8"
            )
            state = get_run_state(run_id, base)
            self.assertEqual(state["project"], "OLD")
            hashed = runs / f"run_{canonical_run_key(run_id)}.json"
            self.assertTrue(hashed.exists())  # copied forward
            self.assertTrue((runs / "legacy_run_id.json").exists())  # original preserved
        finally:
            shutil.rmtree(base, ignore_errors=True)


    def test_concurrent_generation_increments_never_lost(self):
        """WJ-007: N concurrent increments must yield exactly +N (monotonic, none lost)."""
        from audapack.bridge.state import get_audit_generation, increment_audit_generation

        base = Path(tempfile.mkdtemp())
        try:
            workers = 12
            barrier = threading.Barrier(workers)

            def worker(i):
                barrier.wait()
                increment_audit_generation(f"Proj{i}", "core", base)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            final = get_audit_generation(base)
            self.assertEqual(final["generation"], workers)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestWidgetBranding(unittest.TestCase):
    """WJ-011: single production surface; user-facing wording is AUDAPACK."""

    def _widget_source(self) -> str:
        return (
            Path(__file__).resolve().parent.parent / "resources" / "AUDAPACK_WIDGET.user.js"
        ).read_text(encoding="utf-8")

    def test_retired_user_facing_phrases_absent(self):
        src = self._widget_source()
        for phrase in (
            "Use ACBBridge",
            "ACBBridge connected",
            "ACBBridge token",
            "Audit Disk Bridge",
            "queued for ACBBridge",
            "through ACBBridge",
            "Paste the ACBBridge token",
        ):
            self.assertNotIn(phrase, src)

    def test_legacy_service_identity_check_intentionally_retained(self):
        src = self._widget_source()
        # The wrong-service handshake must still recognize the legacy service name.
        self.assertIn("ACBBridge", src)

    def test_api_version_is_3(self):
        src = self._widget_source()
        self.assertIn("BRIDGE_API_VERSION = 3", src)

    def test_widget_is_top_level_only(self):
        src = self._widget_source()
        self.assertIn("// @noframes", src)
        self.assertIn("if (window.top !== window.self) return;", src)

    def test_canonical_vbs_launchers(self):
        root = Path(__file__).resolve().parent.parent
        gui_vbs = (root / "AUDAPACK.vbs").read_text(encoding="utf-8", errors="replace")
        self.assertIn("AUDAPACK.pyw", gui_vbs)

        silent_vbs = (root / "PACK_ALL_SILENT.vbs").read_text(encoding="utf-8", errors="replace")
        self.assertIn("AUDAPACK.pyw", silent_vbs)
        self.assertIn("--silent", silent_vbs)


class BrowserWorkerLaunchNeedTests(unittest.TestCase):
    def test_ready_when_clean_worker_exists(self):
        from audapack.services.bridge_service import browser_worker_launch_need
        self.assertEqual(browser_worker_launch_need({"active_workers": 3, "clean_workers": 1}), "ready")

    def test_busy_when_workers_exist_but_none_clean(self):
        from audapack.services.bridge_service import browser_worker_launch_need
        self.assertEqual(browser_worker_launch_need({"active_workers": 3, "clean_workers": 0}), "busy")

    def test_launch_when_no_worker_registered(self):
        from audapack.services.bridge_service import browser_worker_launch_need
        self.assertEqual(browser_worker_launch_need({"active_workers": 0, "clean_workers": 0}), "launch")

    def test_launch_on_malformed_status(self):
        from audapack.services.bridge_service import browser_worker_launch_need
        self.assertEqual(browser_worker_launch_need(None), "launch")
        self.assertEqual(browser_worker_launch_need({}), "launch")

    def test_qt_start_audit_copy_never_requests_a_second_click(self):
        import inspect

        from audapack.ui_qt.main_window import MainWindow

        source = inspect.getsource(MainWindow._on_send_audit)
        source += inspect.getsource(MainWindow._ensure_free_browser_worker)
        self.assertNotIn("SEND AUDIT", source)
        self.assertNotIn("click START AUDIT again", source)
        self.assertNotIn("click SEND AUDIT again", source)

    def test_worker_preparation_does_not_update_qt_from_background_thread(self):
        import inspect

        from audapack.ui_qt.main_window import MainWindow

        source = inspect.getsource(MainWindow._ensure_free_browser_worker)
        self.assertNotIn("_flash_status", source)

    def test_historical_terminal_run_does_not_renotify_after_gui_restart(self):
        from types import SimpleNamespace

        from audapack.ui_qt.main_window import MainWindow

        messages = []
        tray = SimpleNamespace(showMessage=lambda *args: messages.append(args))
        window = SimpleNamespace(_notified_terminal={}, _tray_icon=tray)
        MainWindow._notify_dispatch_terminal(window, "dsp-history", "COMPLETE", "Old")
        assert messages == []
        MainWindow._notify_dispatch_terminal(window, "dsp-live", "AUDITING", "Live")
        MainWindow._notify_dispatch_terminal(window, "dsp-live", "COMPLETE", "Live")
        assert len(messages) == 1


if __name__ == "__main__":
    unittest.main()


def test_a_bridge_that_cannot_bind_never_touches_dispatch_state(monkeypatch):
    """A losing second Bridge process must not rewrite jobs.json on its way out.

    BrowserDispatcher's constructor blocks every live post-START job with
    "Bridge restarted after START_PREPARED". Building it before claiming the
    port meant a spare process -- one spawned by a single timed-out /health
    probe -- destroyed the running instance's in-flight audits and then exited
    with nothing to show for it.
    """
    import socket

    from audapack.bridge import server as server_module
    from audapack.config import AppConfig

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]

    def _forbidden(*args, **kwargs):
        raise AssertionError("a Bridge that cannot bind must not open dispatch state")

    monkeypatch.setattr(server_module, "BrowserDispatcher", _forbidden)
    config = AppConfig()
    config.bridge.host = "127.0.0.1"
    config.bridge.port = port
    try:
        assert server_module.run_bridge_server(config) == 1
    finally:
        holder.close()


def test_the_widget_declares_an_auto_update_path_at_the_bridge():
    """Without @updateURL/@downloadURL a version bump needs a manual install.

    That install goes through tampermonkey.net/script_installation, which waits
    on the extension's MV3 service worker to wake -- observed taking 1-2 minutes
    as often as it was instant. With the headers, a bumped @version is picked up
    by Tampermonkey's own interval check and no click is needed. They must point
    at the same route the Bridge actually serves.
    """
    from audapack.components.widget import get_bundled_widget_path

    header = get_bundled_widget_path().read_text(encoding="utf-8")[:6000]
    assert "// @updateURL" in header
    assert "// @downloadURL" in header
    for line in header.splitlines():
        if line.startswith("// @updateURL") or line.startswith("// @downloadURL"):
            assert line.rstrip().endswith("/widget.user.js"), line


def test_exactly_one_update_and_one_download_directive():
    """CORE-001 (audit/2.md): two copies of a directive drift.

    The metadata block carried the pair TWICE, so a later edit could move one
    and leave the other -- and Tampermonkey persists whichever it read last.
    """
    from audapack.components.widget import get_bundled_widget_path

    header = get_bundled_widget_path().read_text(encoding="utf-8")[:6000]
    block = header.split("==/UserScript==")[0]
    assert block.count("// @updateURL") == 1, "more than one @updateURL to drift apart"
    assert block.count("// @downloadURL") == 1, "more than one @downloadURL to drift apart"


def test_the_served_widget_points_its_update_check_at_this_bridge():
    """CORE-001: the bundled port is a default, not the contract.

    BridgeConfig.port is operator-configurable and Settings exposes it as an
    editable field, but the bundled directives hardcode 17843 -- so an operator
    who moved the port installed through the configured URL and then silently
    lost auto-update forever, because Tampermonkey kept checking 17843. That is
    the manual-install outage these headers exist to remove.
    """
    from audapack.bridge.server import _widget_source_for_endpoint
    from audapack.components.widget import get_bundled_widget_path

    source = get_bundled_widget_path().read_bytes()
    config = AppConfig()
    config.bridge.host = "127.0.0.1"
    config.bridge.port = 18765

    served = _widget_source_for_endpoint(source, "127.0.0.1:18765", config).decode("utf-8")
    directives = [
        line.strip() for line in served.split("==/UserScript==")[0].splitlines()
        if line.strip().startswith(("// @updateURL", "// @downloadURL"))
    ]
    assert len(directives) == 2, directives
    for line in directives:
        assert line.endswith("http://127.0.0.1:18765/widget.user.js"), line
    assert "17843" not in "".join(directives)


def test_a_foreign_host_header_never_reaches_the_served_script():
    """The endpoint is loopback by contract; a Host claiming otherwise is not it."""
    from audapack.bridge.server import _widget_source_for_endpoint
    from audapack.components.widget import get_bundled_widget_path

    source = get_bundled_widget_path().read_bytes()
    config = AppConfig()
    config.bridge.host = "127.0.0.1"
    config.bridge.port = 19999

    served = _widget_source_for_endpoint(source, "evil.example.com", config).decode("utf-8")
    assert "evil.example.com" not in served.split("==/UserScript==")[0]
    assert "http://127.0.0.1:19999/widget.user.js" in served


def test_the_default_port_is_unchanged_by_the_rewrite():
    from audapack.bridge.server import _widget_source_for_endpoint
    from audapack.components.widget import get_bundled_widget_path

    source = get_bundled_widget_path().read_bytes()
    config = AppConfig()
    config.bridge.host = "127.0.0.1"
    config.bridge.port = 17843

    served = _widget_source_for_endpoint(source, "127.0.0.1:17843", config)
    assert served == source, "the default endpoint must not rewrite anything"


def test_authentication_never_rebuilds_the_whole_config(monkeypatch, tmp_path):
    """PERF-002 (audit/2.md): comparing a bearer token is not a config load.

    `check_auth()` called `load_config()` on every authenticated request just to
    get a second token candidate -- and that parses the config, migrates it,
    calls ensure_token() and walks every registered project through source-path
    healing, which stats project paths. Measured: 0.327 ms/load at 12 projects,
    0.972 ms at 60, 8.124 ms at 300, paid on every worker heartbeat and every
    4-second UI cycle.
    """
    from audapack.bridge import server as server_module

    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path))
    token_file = server_module.get_token_file_path()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("canonical-token-value-0001\n", encoding="utf-8")

    loads = {"count": 0}
    real_load = server_module.load_config

    def counting_load(*args, **kwargs):
        loads["count"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(server_module, "load_config", counting_load)

    handler = server_module.AudapackBridgeHandler.__new__(server_module.AudapackBridgeHandler)
    handler.test_base_dir = None
    handler.config = AppConfig()
    handler.config.bridge.token = "in-memory-token-value-0002"
    handler.headers = {"X-ACB-Token": "canonical-token-value-0001"}
    monkeypatch.setattr(
        server_module.AudapackBridgeHandler, "_legacy_token_candidates", lambda self: []
    )

    for _ in range(25):
        assert handler.check_auth() is True

    assert loads["count"] == 0, f"{loads['count']} full config loads for 25 authenticated requests"


def test_a_rotated_token_takes_effect_with_no_restart(monkeypatch, tmp_path):
    """Caching a credential must not outlive the credential."""
    from audapack.bridge import server as server_module

    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path))
    token_file = server_module.get_token_file_path()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("first-token-value-000000001\n", encoding="utf-8")

    handler = server_module.AudapackBridgeHandler.__new__(server_module.AudapackBridgeHandler)
    handler.test_base_dir = None
    handler.config = AppConfig()
    handler.config.bridge.token = ""
    monkeypatch.setattr(
        server_module.AudapackBridgeHandler, "_legacy_token_candidates", lambda self: []
    )
    refusals = []
    monkeypatch.setattr(
        server_module.AudapackBridgeHandler, "send_json",
        lambda self, status, payload: refusals.append(status),
    )

    handler.headers = {"X-ACB-Token": "first-token-value-000000001"}
    assert handler.check_auth() is True

    token_file.write_text("second-token-value-00000002\n", encoding="utf-8")
    handler.headers = {"X-ACB-Token": "second-token-value-00000002"}
    assert handler.check_auth() is True, "a rotated token was refused without a restart"

    handler.headers = {"X-ACB-Token": "first-token-value-000000001"}
    assert handler.check_auth() is False, "the retired token still authenticates"
    assert refusals and refusals[-1] == 403


def test_a_revocation_marker_written_at_runtime_stops_legacy_credentials(monkeypatch, tmp_path):
    from audapack.bridge import server as server_module
    from audapack.config import revoke_legacy_token_acceptance

    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path))
    legacy_root = tmp_path / "legacy"
    (legacy_root / "ACBBridge").mkdir(parents=True)
    (legacy_root / "ACBBridge" / "token.txt").write_text("legacy-token-value-00001\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(legacy_root))

    handler = server_module.AudapackBridgeHandler.__new__(server_module.AudapackBridgeHandler)
    assert [path.name for path in handler._legacy_token_candidates()] == ["token.txt", "token.txt"]

    assert revoke_legacy_token_acceptance() is True
    assert handler._legacy_token_candidates() == [], "the marker did not take effect immediately"


def test_a_same_length_rotation_inside_one_timestamp_tick_is_seen(monkeypatch, tmp_path):
    """A credential read must not be cached on a coarse signature.

    The first attempt at PERF-002 keyed the token on (mtime_ns, size) like the
    widget bundle. A rotation to a same-length value inside one filesystem
    timestamp tick was then invisible -- a stale credential, which is the one
    thing an auth path must never serve. The 40-byte read is not the cost;
    `load_config()` was.
    """
    from audapack.bridge import server as server_module

    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path))
    token_file = server_module.get_token_file_path()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    first = "aaaaaaaaaaaaaaaaaaaaaaaa"
    second = "bbbbbbbbbbbbbbbbbbbbbbbb"
    assert len(first) == len(second)

    token_file.write_text(first + "\n", encoding="utf-8")
    stat = token_file.stat()
    assert server_module._live_bridge_token() == first

    token_file.write_text(second + "\n", encoding="utf-8")
    import os as _os

    # Force the exact adversarial case: identical size AND identical mtime.
    _os.utime(token_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert token_file.stat().st_size == stat.st_size
    assert token_file.stat().st_mtime_ns == stat.st_mtime_ns

    assert server_module._live_bridge_token() == second, "a rotated token was served stale"


class TestPidOwnershipIsAtomicAndBound(unittest.TestCase):
    """W2-004 (audit/4.md): bridge.pid is an ownership token, not a diary.

    write_pid wrote straight onto the file (a transient partial read was
    possible mid-write), and remove_pid only rejected mismatches when the
    current value was TRUTHY -- so an unreadable identity AUTHORIZED deletion.
    Measured: remove_pid(expected_pid=111) deleted a file whose reread returned
    `{}`. And stop_bridge read the PID file AFTER the endpoint went offline, so
    a successor Bridge starting in that gap had its record deleted by the
    retiring controller.
    """

    def _lifecycle(self):
        import audapack.bridge.lifecycle as lc

        return lc

    def test_an_unreadable_pid_file_fails_closed(self):
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pid_file = base / "bridge.pid"
            pid_file.write_text("{ not json", encoding="utf-8")
            lc.remove_pid(base, expected_pid=111, expected_nonce="n-1")
            self.assertTrue(pid_file.exists(), "an unreadable identity authorized deletion")
            pid_file.write_text(json.dumps({"pid": "", "nonce": ""}), encoding="utf-8")
            lc.remove_pid(base, expected_pid=111, expected_nonce="n-1")
            self.assertTrue(pid_file.exists(), "an empty identity authorized deletion")

    def test_a_matching_identity_still_removes_the_record(self):
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pid_file = base / "bridge.pid"
            pid_file.write_text(json.dumps({"pid": 111, "nonce": "n-1"}), encoding="utf-8")
            lc.remove_pid(base, expected_pid=111, expected_nonce="n-1")
            self.assertFalse(pid_file.exists())

    def test_a_foreign_identity_is_never_removed(self):
        lc = self._lifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pid_file = base / "bridge.pid"
            pid_file.write_text(json.dumps({"pid": 222, "nonce": "n-2"}), encoding="utf-8")
            lc.remove_pid(base, expected_pid=111, expected_nonce="n-1")
            self.assertTrue(pid_file.exists(), "A's cleanup erased B's record")
            self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["pid"], 222)

    def test_a_concurrent_publication_survives_a_concurrent_delete(self):
        """write_pid(B) landing between remove_pid(A)'s read and unlink must
        not erase B: publication and deletion share one cross-process lock."""
        import threading

        import audapack.bridge.lifecycle as lc

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pid_file = base / "bridge.pid"
            pid_file.write_text(json.dumps({"pid": 111, "nonce": "n-A"}), encoding="utf-8")

            opened = threading.Event()
            real_open = lc.open_new_temp_file

            def gate(directory, basename):
                handle, path = real_open(directory, basename)
                if "bridge.pid" in basename:
                    opened.set()
                    import time as _time

                    _time.sleep(0.4)
                return handle, path

            from unittest.mock import patch as _patch
            with _patch.object(lc, "open_new_temp_file", side_effect=gate):
                writer = threading.Thread(
                    target=lambda: (opened.wait(timeout=5), lc.write_pid(base)),
                )
                writer.start()
                opened.wait(timeout=5)
                lc.remove_pid(base, expected_pid=111, expected_nonce="n-A")
                writer.join(timeout=10)


            current = lc.read_pid(base)
            self.assertNotEqual(
                int(current.get("pid") or 0), 111,
                "A's compare-and-delete ran against B's freshly published record",
            )

    def test_write_pid_is_atomic_and_durable(self):
        import os as _os

        import audapack.bridge.lifecycle as lc

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lc.write_pid(base)
            current = lc.read_pid(base)
            self.assertEqual(int(current.get("pid") or 0), _os.getpid())
            self.assertEqual(current.get("nonce"), lc.INSTANCE_NONCE)
            self.assertFalse(list(base.glob(".bridge.pid.tmp.*")), "an orphan temp survived")


if __name__ == "__main__":
    unittest.main()
