"""P0-1 regression: live status path never spawns schtasks/subprocess.

The 5-second dispatch timer MUST NOT call get_autostart_status() (which runs
schtasks /query and would flash a Windows console). Only the Settings Bridge
page and startup are allowed to query Scheduled Tasks.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch


class TestRuntimeStatusAvoidsAutostart(unittest.TestCase):
    def test_runtime_status_does_not_call_get_autostart_status(self):
        from audapack.services.bridge_service import BridgeService

        with patch("audapack.services.bridge_service.get_autostart_status") as autostart_mock, \
             patch("audapack.services.bridge_service.check_bridge_health", return_value=(True, {"service": "AUDAPACK Bridge", "api_version": 3})), \
             patch.object(BridgeService, "browser_status", return_value={"ok": True, "dispatch": {}}):
            BridgeService().runtime_status()
            autostart_mock.assert_not_called()

    def test_full_status_may_call_get_autostart_status(self):
        """Full status() is allowed to read autostart for Settings / startup.
        It is just NOT allowed on the 5-second timer."""
        from audapack.services.bridge_service import BridgeService

        with patch("audapack.services.bridge_service.get_autostart_status", return_value={"installed": False}) as autostart_mock, \
             patch("audapack.services.bridge_service.check_bridge_health", return_value=(True, {"service": "AUDAPACK Bridge", "api_version": 3})), \
             patch.object(BridgeService, "browser_status", return_value={"ok": True, "dispatch": {}}):
            result = BridgeService().status()
            self.assertIn("autostart", result)
            autostart_mock.assert_called_once()

    def test_runtime_status_is_cheap_no_subprocess_spawn(self):
        """P0-1: runtime_status() must perform HTTP only. Detect any subprocess
        invocation through the standard library gateway as the regression sentinel."""
        from audapack.services.bridge_service import BridgeService

        with patch("subprocess.Popen") as popen_mock, \
             patch("subprocess.run") as run_mock, \
             patch("audapack.services.bridge_service.check_bridge_health", return_value=(True, {"service": "AUDAPACK Bridge", "api_version": 3})), \
             patch.object(BridgeService, "browser_status", return_value={"ok": True, "dispatch": {}}):
            BridgeService().runtime_status()
            popen_mock.assert_not_called()
            run_mock.assert_not_called()

    def test_autostart_subprocess_is_hidden_on_windows(self):
        """Every autostart subprocess call must use CREATE_NO_WINDOW +
        STARTF_USESHOWWINDOW so no console flashes even for explicit settings ops."""
        if sys.platform != "win32":
            self.skipTest("Windows-only check")
        from audapack.components import autostart as autostart_mod

        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = ""
            run_mock.return_value.stderr = ""
            autostart_mod.query_task()
            run_mock.assert_called_once()
            call = run_mock.call_args
            self.assertIn("startupinfo", call.kwargs)
            si = call.kwargs["startupinfo"]
            self.assertEqual(si.dwFlags, subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(si.wShowWindow, subprocess.SW_HIDE)
            self.assertEqual(call.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
