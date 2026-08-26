"""Tests for the transactional legacy ACBBridge takeover (WJ-003).

The takeover's destructive step (legacy Scheduled Task deletion) is gated on a
proven sequence. These tests exercise the gate logic with stubbed operations so
no real Scheduled Task is ever touched, plus live capability probes against a
real in-process bridge server.
"""

import json
import secrets
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from audapack.bridge.server import AudapackBridgeHandler
from audapack.components import migration
from audapack.config import AppConfig
from audapack.models import Project


class TestTakeoverGateOrdering(unittest.TestCase):
    """Any mandatory failure BEFORE the gate must leave the legacy task intact."""

    def _base_report(self) -> dict:
        return {
            "step": "init",
            "legacy_detected": True,
            "legacy_stopped": False,
            "audapack_started": False,
            "identity_verified": False,
            "capability_probes": False,
            "autostart_installed": False,
            "task_command_verified": False,
            "task_trigger_verified": False,
            "backup_done": False,
            "legacy_task_removed": False,
            "rollback": None,
            "errors": [],
        }

    def setUp(self):
        self.migration = migration
        self.calls = []

        self.cfg = AppConfig()
        self.cfg.projects = [
            Project(id="probe_existing", display_name="ProbeExisting", source_path="", priority_group="MAIN0", slot=1)
        ]

        # Stub every external effect; record the order it happened in.
        m = self.migration
        self._patches = [
            mock.patch.object(m, "detect_legacy_installation", return_value={
                "legacy_task_exists": True,
                "legacy_task_info": {},
                "legacy_dir_exists": False,
                "legacy_dir_path": "",
                "legacy_bridge_running_on_port": False,
                "port_health_info": {},
            }),
            mock.patch.object(m, "stop_verified_legacy_bridge", side_effect=lambda: (self.calls.append("stop_legacy"), (True, "stopped"))[1]),
            mock.patch.object(m, "start_bridge_background", side_effect=lambda cfg=None: (self.calls.append("start_new"), True)[1]),
            mock.patch.object(m, "check_bridge_health", side_effect=lambda *a, **k: (self.calls.append("health"), (True, {"service": "AUDAPACK Bridge", "api_version": 2}))[1]),
            mock.patch.object(m, "probe_authenticated_endpoints", return_value=(True, {"status": True, "registry": True, "write": True})),
            mock.patch.object(m, "get_user_runtime_dir", return_value=Path(__file__).parent / "_tmproot"),
            mock.patch.object(m, "get_legacy_appdata_dir", return_value=Path(__file__).parent / "_nolegacy"),
            mock.patch.object(m, "install_autostart", side_effect=lambda: (self.calls.append("install_autostart"), (True, "installed"))[1]),
            mock.patch.object(m, "query_task", side_effect=lambda name: (self.calls.append(f"query:{name}"), (True, {"Task To Run": __import__("audapack.components.autostart", fromlist=["get_canonical_autostart_command"]).get_canonical_autostart_command()}))[1]),
            mock.patch.object(m, "run_autostart_task", side_effect=lambda: (self.calls.append("run_task"), (True, "started"))[1]),
            mock.patch.object(m, "stop_bridge", side_effect=lambda cfg=None: (self.calls.append("stop_new"), (True, "stopped"))[1]),
        ]
        self._subprocess_patch = mock.patch.object(m.subprocess, "run")
        self._patches.append(self._subprocess_patch)

        def _record_subprocess(*args, **kwargs):
            self.calls.append(f"subprocess:{list(args[0][:2])}")
            return mock.Mock(returncode=0, stdout="", stderr="")

        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        # Assign only AFTER start(): before start(), the patch object discards it.
        m.subprocess.run.side_effect = _record_subprocess

    def test_full_success_removes_legacy_after_all_gates(self):
        ok, rep = self.migration.perform_bridge_takeover(self.cfg)
        self.assertTrue(ok, rep)
        self.assertTrue(rep["legacy_task_removed"])
        self.assertTrue(rep["task_command_verified"])
        self.assertTrue(rep["task_trigger_verified"])
        self.assertTrue(rep["backup_done"] or rep["errors"])

        delete_idx = next(i for i, c in enumerate(self.calls) if str(c).startswith("subprocess:['schtasks', '/delete'"))
        for gate_call in ("install_autostart", "run_task"):
            gate_idx = self.calls.index(gate_call)
            self.assertLess(gate_idx, delete_idx)

    def test_failed_autostart_install_never_deletes_legacy(self):
        m = self.migration
        m.install_autostart.side_effect = None
        m.install_autostart.return_value = (False, "schtasks blew up")

        ok, rep = m.perform_bridge_takeover(self.cfg)
        self.assertFalse(ok)
        self.assertFalse(rep["autostart_installed"])
        self.assertFalse(rep["legacy_task_removed"])  # HARD GATE held
        self.assertTrue(any("/delete" not in str(c) for c in self.calls))
        self.assertFalse(any(str(c).startswith("subprocess:['schtasks', '/delete'") for c in self.calls))
        # Rollback attempted: legacy stopped earlier and still present.
        self.assertIn("legacy ACBBridge task re-triggered after failed takeover", str(rep["rollback"]))

    def test_failed_trigger_proof_never_deletes_legacy(self):
        m = self.migration

        # Identity check (first health call) succeeds; every later poll is offline,
        # so the manual-trigger proof can never observe a healthy task bridge.
        health_calls = {"n": 0}

        def identity_only_health(*args, **kwargs):
            self.calls.append("health")
            health_calls["n"] += 1
            if health_calls["n"] == 1:
                return True, {"service": "AUDAPACK Bridge", "api_version": 2}
            return False, {"status": "offline"}

        m.check_bridge_health.side_effect = identity_only_health

        # Fast-forward the takeover polling loop instead of sleeping 20 real seconds.
        class _FakeTime:
            def __init__(self):
                self.t = 1000.0
            def time(self):
                self.t += 1.0
                return self.t
            def sleep(self, s):
                pass

        time_shim = mock.patch.object(m, "time", _FakeTime())
        time_shim.start()
        self.addCleanup(time_shim.stop)

        ok, rep = m.perform_bridge_takeover(self.cfg)
        self.assertFalse(ok)
        self.assertEqual(rep["step"], "trigger_verify")
        self.assertFalse(rep["legacy_task_removed"])
        self.assertFalse(any(str(c).startswith("subprocess:['schtasks', '/delete'") for c in self.calls))


class TestCapabilityProbes(unittest.TestCase):
    """Live probes against a real in-process AUDAPACK Bridge server."""

    def setUp(self):
        import tempfile as _tf
        self.temp_dir = Path(_tf.mkdtemp())
        self.config = AppConfig()
        self.config.audits.root = str(self.temp_dir / "AUDITING_IMPLEMENTATION")
        self.config.bridge.host = "127.0.0.1"
        self.config.bridge.port = 18943
        self.config.bridge.token = f"probe_token_{secrets.token_hex(8)}"
        self.config.projects = [
            Project(id="probeexisting", display_name="ProbeExisting", source_path="", priority_group="MAIN0", slot=1)
        ]

        class TestHandler(AudapackBridgeHandler):
            pass

        TestHandler.config = self.config
        self.server = ThreadingHTTPServer((self.config.bridge.host, self.config.bridge.port), TestHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        import shutil as _sh
        _sh.rmtree(self.temp_dir, ignore_errors=True)

    def test_probes_pass_with_valid_token_and_existing_project_write(self):
        ok, detail = migration.probe_authenticated_endpoints(
            self.config.bridge.host,
            self.config.bridge.port,
            self.config.bridge.token,
            write_probe_name="ProbeExisting",
        )
        self.assertTrue(ok, detail)
        self.assertTrue(detail["status"])
        self.assertTrue(detail["registry"])
        self.assertTrue(detail["write"])
        self.assertEqual(detail["write_status"], "existing")  # no mutation

    def test_probes_fail_with_invalid_token(self):
        req_token = "wrong_token_value_12345"
        ok, detail = migration.probe_authenticated_endpoints(
            self.config.bridge.host, self.config.bridge.port, req_token
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
