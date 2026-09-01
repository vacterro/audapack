"""P0-1 regression guard: console tools spawned from windowless processes
(the pythonw GUI, the Bridge daemon) must never flash a black console window.

The Chromium worker launcher used to run ``powershell`` bare from
``detect_installed_browsers()`` on every worker launch: the operator saw a
black window flash and steal focus each time a managed worker was started or
re-provisioned. These tests pin the CREATE_NO_WINDOW + SW_HIDE pattern on
every spawn site, mirroring ``test_autostart_subprocess_is_hidden_on_windows``.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

WIN32 = sys.platform == "win32"


@unittest.skipUnless(WIN32, "console-flash flags are Windows-only")
class TestHiddenSpawnSites(unittest.TestCase):
    def assert_hidden(self, call) -> None:
        self.assertIn("startupinfo", call.kwargs)
        si = call.kwargs["startupinfo"]
        self.assertEqual(si.dwFlags, subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(si.wShowWindow, subprocess.SW_HIDE)
        self.assertTrue(call.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_detect_installed_browsers_powershell_is_hidden(self):
        from audapack.components import widget as widget_mod

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as run_mock:
            widget_mod.detect_installed_browsers()
            # Assert on THIS spawn, not on the call count: other suites keep
            # background threads that spawn schtasks, and a stray call landing
            # inside the patch window is not a defect in this one.
            powershell_calls = [
                call for call in run_mock.call_args_list
                if call.args and call.args[0] and "powershell" in str(call.args[0][0]).lower()
            ]
            self.assertEqual(len(powershell_calls), 1, run_mock.call_args_list)
            self.assert_hidden(powershell_calls[0])

    def test_worker_launch_popen_is_hidden(self):
        from audapack.components import widget as widget_mod

        with patch.object(widget_mod, "select_dedicated_chromium", return_value="C:/x/chrome.exe"), \
             patch("subprocess.Popen") as popen_mock:
            ok, _msg = widget_mod.launch_dedicated_chromium_worker()
            self.assertTrue(ok)
            popen_mock.assert_called_once()
            self.assert_hidden(popen_mock.call_args)

    def test_migration_taskkill_is_hidden(self):
        from audapack.components import migration as migration_mod

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as run_mock, \
             patch.object(migration_mod, "query_task", return_value=(True, {})), \
             patch.object(migration_mod, "check_bridge_health", side_effect=[(True, {})] * 40), \
             patch.object(migration_mod.time, "sleep"):
            migration_mod.stop_verified_legacy_bridge()
            self.assertTrue(run_mock.call_args_list)
            for call in run_mock.call_args_list:
                self.assert_hidden(call)


class TestHiddenSpawnKwargs(unittest.TestCase):
    def test_flags_or_with_caller_creationflags(self):
        if not WIN32:
            self.skipTest("Windows-only check")
        from audapack.procutil import hidden_spawn_kwargs

        kwargs = hidden_spawn_kwargs(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_non_windows_is_passthrough(self):
        if WIN32:
            self.skipTest("Non-Windows check")
        from audapack.procutil import hidden_spawn_kwargs

        self.assertEqual(hidden_spawn_kwargs(creationflags=7)["creationflags"], 7)


if __name__ == "__main__":
    unittest.main()


class TestSpawnStormIsCached(unittest.TestCase):
    """/health and /v1/status are polled every few seconds while an audit runs.

    Recomputing the build identity there spawned two `git` processes per
    request -- four per poll cycle, roughly twice a second on Windows, each
    flashing a console window. Caching is the fix; hiding the window only
    hides the symptom.
    """

    def test_build_identity_is_computed_once_per_process(self):
        from audapack.bridge import server as server_mod

        server_mod._BUILD_IDENTITY = None
        with patch.object(server_mod, "_read_build_identity", return_value=("abc123", "abc123")) as read_mock:
            first = server_mod._get_build_identity()
            second = server_mod._get_build_identity()
            third = server_mod._get_build_identity()
        self.assertEqual(first, ("abc123", "abc123"))
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        read_mock.assert_called_once()

    def test_widget_bundle_info_is_not_rehashed_per_request(self):
        from audapack.bridge import server as server_mod

        server_mod._WIDGET_BUNDLE_CACHE.clear()
        first = server_mod._get_widget_bundle_info()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("re-read the 800KB bundle")):
            second = server_mod._get_widget_bundle_info()
        self.assertEqual(first, second)

    def test_a_changed_bundle_invalidates_the_cache(self):
        import tempfile

        from audapack.bridge import server as server_mod

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "AUDAPACK_WIDGET.user.js"
            bundle.write_text("// @version      1.0.0\n", encoding="utf-8")
            server_mod._WIDGET_BUNDLE_CACHE.clear()
            with patch.object(server_mod, "get_bundled_widget_path", return_value=bundle):
                self.assertEqual(server_mod._get_widget_bundle_info()[0], "1.0.0")
                bundle.write_text("// @version      2.0.0\n\n", encoding="utf-8")
                self.assertEqual(server_mod._get_widget_bundle_info()[0], "2.0.0")
