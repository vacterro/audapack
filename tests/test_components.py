"""Unit tests for Component Center and Widget metadata."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from audapack.components.manager import ComponentManager
from audapack.components.widget import get_bundled_widget_path, read_bundled_widget_metadata
from audapack.config import AppConfig


class TestComponents(unittest.TestCase):
    def test_bundled_widget_exists(self):
        w_path = get_bundled_widget_path()
        self.assertTrue(w_path.exists())
        self.assertTrue(w_path.is_file())

    def test_read_bundled_widget_metadata(self):
        meta = read_bundled_widget_metadata()
        self.assertTrue(meta["exists"])
        self.assertRegex(meta["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("AUDAPACK", meta["name"])

    def test_component_manager_status(self):
        cfg = AppConfig()
        mgr = ComponentManager(cfg)
        st = mgr.get_components_status()

        self.assertIn("context_menu", st)
        self.assertIn("bridge", st)
        self.assertIn("widget", st)
        self.assertEqual(st["widget"]["status"], "READY")

    def test_detect_installed_browsers_returns_list_of_dicts(self):
        from audapack.components.widget import detect_installed_browsers
        browsers = detect_installed_browsers()
        self.assertIsInstance(browsers, list)
        for b in browsers:
            self.assertIn("name", b)
            self.assertIn("exe", b)
            self.assertTrue(Path(b["exe"]).exists())

    def test_preferred_browser_config(self):
        from audapack.config import AppConfig, UIConfig
        cfg = AppConfig(ui=UIConfig(preferred_browser="C:\\fake\\browser.exe"))
        d = cfg.to_dict()
        self.assertEqual(d["ui"]["preferred_browser"], "C:\\fake\\browser.exe")

    def test_dedicated_chromium_command_is_isolated_and_unthrottled(self):
        from audapack.components.widget import dedicated_chromium_command

        profile = Path("C:/runtime/AUDAPACK/browser_worker/chromium_profile")
        cmd = dedicated_chromium_command("C:/Program Files/Google/Chrome/Application/chrome.exe", profile)
        self.assertIn(f"--user-data-dir={profile}", cmd)
        self.assertIn("--disable-background-timer-throttling", cmd)
        self.assertIn("--disable-backgrounding-occluded-windows", cmd)
        self.assertIn("--disable-renderer-backgrounding", cmd)
        self.assertIn("--profile-directory=Default", cmd)
        self.assertEqual(cmd[-1], "https://chatgpt.com/?audapack_worker=1")

    def test_dedicated_worker_rejects_firefox(self):
        from audapack.components.widget import dedicated_chromium_command

        with self.assertRaises(ValueError):
            dedicated_chromium_command("C:/Program Files/Mozilla Firefox/firefox.exe", Path("C:/profile"))

    @patch("audapack.components.manager.launch_dedicated_chromium_worker")
    def test_managed_worker_identity_reaches_launcher(self, launch):
        launch.return_value = (True, "launched")
        result = ComponentManager(AppConfig()).launch_browser_worker(
            managed_slot=3,
            managed_generation=7,
        )
        self.assertEqual(result, (True, "launched"))
        launch.assert_called_once_with(managed_slot=3, managed_generation=7)

    @patch("audapack.components.widget._launch_dedicated_chromium")
    def test_managed_worker_launch_marks_url(self, launch):
        from audapack.components.widget import launch_dedicated_chromium_worker

        launch.return_value = (True, "", "C:/chrome.exe", Path("C:/profile"))
        ok, _message = launch_dedicated_chromium_worker(managed_slot=2, managed_generation=5)
        self.assertTrue(ok)
        target = launch.call_args.args[0]
        query = parse_qs(urlsplit(target).query)
        self.assertEqual(query["audapack_worker"], ["1"])
        self.assertEqual(query["audapack_worker_slot"], ["2"])
        self.assertEqual(query["audapack_worker_generation"], ["5"])

    @patch("audapack.components.manager.open_widget_in_dedicated_chromium")
    @patch("audapack.components.manager.is_bridge_healthy", return_value=True)
    def test_widget_install_uses_dedicated_profile(self, _healthy, open_dedicated):
        open_dedicated.return_value = (True, "opened")
        cfg = AppConfig()
        cfg.bridge.host = "127.0.0.1"
        cfg.bridge.port = 18765

        # A cold profile makes trigger_widget_install warm the pool first, and
        # an unstubbed warm-up launches a REAL Chrome from the test run.
        manager = ComponentManager(cfg)
        manager.WIDGET_INSTALL_WARMUP_SECONDS = 0.0
        manager._worker_profile_is_live = lambda: False
        manager.launch_browser_worker = lambda **kw: (False, "stubbed")

        ok, message = manager.trigger_widget_install()

        self.assertTrue(ok)
        self.assertEqual(message, "opened")
        open_dedicated.assert_called_once_with(
            use_bridge=True,
            bridge_url="http://127.0.0.1:18765/widget.user.js",
            new_window=True,
        )


class TestWidgetInstallWarmsTheProfile(unittest.TestCase):
    """Tampermonkey's install page waits on the extension's MV3 service worker.

    A Chromium started only to open that URL is a cold start every time. When a
    window already exists in the profile, Chrome forwards the URL into that live
    process instead. So the installer warms the profile first when nothing is
    running in it -- and never opens a second window when something is.
    """

    def _manager(self, active_workers):
        from audapack.components.manager import ComponentManager
        from audapack.config import AppConfig

        manager = ComponentManager(AppConfig())
        manager.WIDGET_INSTALL_WARMUP_SECONDS = 0.0
        manager._worker_profile_is_live = lambda: bool(active_workers)
        return manager

    def test_a_cold_profile_is_warmed_before_the_installer_opens(self):
        from audapack.components import manager as mgr

        manager = self._manager(active_workers=0)
        launches = []
        manager.launch_browser_worker = lambda **kw: (launches.append(kw) or (True, "started"))
        with patch.object(mgr, "is_bridge_healthy", return_value=True), \
             patch.object(mgr, "open_widget_in_dedicated_chromium", return_value=(True, "opened")) as opener:
            ok, message = manager.trigger_widget_install()
        self.assertTrue(ok)
        self.assertEqual(len(launches), 1, "a cold profile needs one window")
        self.assertIn("warmed", message)
        self.assertTrue(opener.call_args.kwargs["use_bridge"])

    def test_a_live_profile_never_gets_a_second_window(self):
        from audapack.components import manager as mgr

        manager = self._manager(active_workers=3)
        launches = []
        manager.launch_browser_worker = lambda **kw: (launches.append(kw) or (True, "started"))
        with patch.object(mgr, "is_bridge_healthy", return_value=True), \
             patch.object(mgr, "open_widget_in_dedicated_chromium", return_value=(True, "opened")):
            ok, message = manager.trigger_widget_install()
        self.assertTrue(ok)
        self.assertEqual(launches, [], "Chrome forwards into the live process")
        self.assertIn("already live", message)

    def test_a_dead_bridge_still_opens_the_installer(self):
        from audapack.components import manager as mgr

        manager = self._manager(active_workers=0)
        launches = []
        manager.launch_browser_worker = lambda **kw: (launches.append(kw) or (True, "started"))
        with patch.object(mgr, "is_bridge_healthy", return_value=False), \
             patch.object(mgr, "open_widget_in_dedicated_chromium", return_value=(True, "opened")) as opener:
            ok, _message = manager.trigger_widget_install()
        self.assertTrue(ok)
        self.assertEqual(launches, [], "no Bridge means no pool to warm")
        self.assertFalse(opener.call_args.kwargs["use_bridge"])


class TestProfileLivenessIsAboutTheProfile(unittest.TestCase):
    """CORE-005 (audit/2.md): worker registration is not profile liveness.

    `_worker_profile_is_live()` answered `dispatch.active_workers > 0`, which is
    a different invariant and wrong in both directions. False positive:
    `dedicated_profile_only` is off by default, so an operator's own widget-
    carrying tab satisfied it while the dedicated profile was not running at
    all. False negative, the one that hurt: on a fresh profile the userscript is
    not installed yet, so the window CANNOT register -- the installer waited the
    full 25 s for an impossible condition and then opened a second window
    anyway, defeating its own purpose.
    """

    def _manager(self):
        from audapack.components.manager import ComponentManager
        from audapack.config import AppConfig

        return ComponentManager(AppConfig())

    @unittest.skipUnless(sys.platform == "win32", "window enumeration is Win32-only")
    def test_a_foreign_worker_does_not_make_the_dedicated_profile_live(self):
        from audapack.components import manager as mgr
        from audapack.services.bridge_service import BridgeService

        manager = self._manager()
        with patch("audapack.window_layout.find_profile_windows", return_value=[]), \
             patch.object(BridgeService, "browser_status", return_value={
                 "ok": True, "dispatch": {"active_workers": 4}}):
            self.assertFalse(
                manager._worker_profile_is_live(),
                "a foreign registered worker was read as the dedicated profile being up",
            )
        self.assertTrue(mgr is not None)

    @unittest.skipUnless(sys.platform == "win32", "window enumeration is Win32-only")
    def test_a_running_profile_with_no_registration_is_live(self):
        from audapack.services.bridge_service import BridgeService

        manager = self._manager()
        with patch("audapack.window_layout.find_profile_windows", return_value=[4242]), \
             patch.object(BridgeService, "browser_status", return_value={
                 "ok": True, "dispatch": {"active_workers": 0}}):
            self.assertTrue(
                manager._worker_profile_is_live(),
                "a fresh profile cannot register before the widget is installed",
            )

    @unittest.skipUnless(sys.platform == "win32", "window enumeration is Win32-only")
    def test_a_live_profile_costs_no_warmup_and_no_second_window(self):
        from audapack.components import manager as mgr
        from audapack.services.bridge_service import BridgeService

        manager = self._manager()
        launches = []
        manager.launch_browser_worker = lambda **kw: (launches.append(kw) or (True, "started"))
        with patch("audapack.window_layout.find_profile_windows", return_value=[99]), \
             patch.object(BridgeService, "browser_status", return_value={"ok": False}), \
             patch.object(mgr, "is_bridge_healthy", return_value=True), \
             patch.object(mgr, "open_widget_in_dedicated_chromium", return_value=(True, "opened")) as opener:
            ok, message = manager.trigger_widget_install()

        self.assertTrue(ok)
        self.assertEqual(launches, [], "the profile was already up")
        self.assertIn("already live", message)
        self.assertIs(opener.call_args.kwargs["new_window"], False)

    def test_the_method_never_consults_the_bridge_at_all(self):
        """T-152: profile liveness is a window-enumeration answer, not a Bridge one.

        The old fallback read `dispatch.active_workers` off Windows and on any
        enumeration failure, so a foreign registered widget worker could make
        the dedicated profile look alive. The method must not even ask.
        """
        from audapack.services.bridge_service import BridgeService

        manager = self._manager()
        with patch("sys.platform", "linux"), \
             patch.object(BridgeService, "browser_status", side_effect=AssertionError("Bridge consulted")):
            self.assertFalse(manager._worker_profile_is_live())

    @unittest.skipUnless(sys.platform == "win32", "window enumeration is Win32-only")
    def test_bridge_workers_cannot_make_the_profile_live_on_windows(self):
        from audapack.services.bridge_service import BridgeService

        manager = self._manager()
        with patch("audapack.window_layout.find_profile_windows", return_value=[]), \
             patch.object(BridgeService, "browser_status", side_effect=AssertionError("Bridge consulted")):
            self.assertFalse(manager._worker_profile_is_live())

    @unittest.skipUnless(sys.platform == "win32", "window enumeration is Win32-only")
    def test_an_enumeration_exception_fails_closed(self):
        manager = self._manager()
        with patch("audapack.window_layout.find_profile_windows", side_effect=OSError("injected")):
            self.assertFalse(
                manager._worker_profile_is_live(),
                "an enumeration error must never read as profile liveness",
            )


class TestInstallerDoesNotAddAWindow(unittest.TestCase):

    """One press had started opening two windows: a warmed one and the installer.

    A worker lane wants its own window. The installer does not -- when the
    profile is already live Chrome can put it in a tab of the window that is
    already there.
    """

    def test_the_command_can_omit_new_window(self):
        from audapack.components.widget import dedicated_chromium_command

        exe = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        with_window = dedicated_chromium_command(exe, Path("C:/profile"), "https://x/", True)
        as_tab = dedicated_chromium_command(exe, Path("C:/profile"), "https://x/", False)
        self.assertIn("--new-window", with_window)
        self.assertNotIn("--new-window", as_tab)
        self.assertEqual(as_tab[-1], "https://x/")

    def test_a_live_profile_gets_the_installer_as_a_tab(self):
        from audapack.components import manager as mgr
        from audapack.components.manager import ComponentManager
        from audapack.config import AppConfig

        manager = ComponentManager(AppConfig())
        manager._worker_profile_is_live = lambda: True
        with patch.object(mgr, "is_bridge_healthy", return_value=True), \
             patch.object(mgr, "open_widget_in_dedicated_chromium", return_value=(True, "opened")) as opener:
            manager.trigger_widget_install()
        self.assertFalse(opener.call_args.kwargs["new_window"])

    def test_a_cold_profile_still_gets_its_window(self):
        from audapack.components import manager as mgr
        from audapack.components.manager import ComponentManager
        from audapack.config import AppConfig

        manager = ComponentManager(AppConfig())
        manager.WIDGET_INSTALL_WARMUP_SECONDS = 0.0
        manager._worker_profile_is_live = lambda: False
        manager.launch_browser_worker = lambda **kw: (False, "no browser")
        with patch.object(mgr, "is_bridge_healthy", return_value=True), \
             patch.object(mgr, "open_widget_in_dedicated_chromium", return_value=(True, "opened")) as opener:
            manager.trigger_widget_install()
        self.assertTrue(opener.call_args.kwargs["new_window"])


class TestManualAuditWindow(unittest.TestCase):
    """NEW opens a window in the worker profile that no lane owns.

    The dispatcher knows a window only by the slot/generation in its URL, so a
    window opened without them can never be sent work -- which is the whole
    point: the operator drops an archive in and runs the audit by hand.
    """

    @patch("audapack.components.widget._launch_dedicated_chromium")
    def test_the_manual_window_carries_no_worker_slot(self, launch):
        from audapack.components.widget import open_manual_chromium_window

        launch.return_value = (True, "", "C:/chrome.exe", Path("C:/profile"))
        ok, message = open_manual_chromium_window()

        self.assertTrue(ok)
        target = launch.call_args.args[0]
        self.assertNotIn("audapack_worker", target)
        self.assertIn("chatgpt.com", target)
        self.assertIn("archive", message.lower())

    @patch("audapack.components.widget._launch_dedicated_chromium")
    def test_it_is_always_its_own_window(self, launch):
        """A tab added to a lane mid-audit is not a place to drop an archive."""
        from audapack.components.widget import open_manual_chromium_window

        launch.return_value = (True, "", "C:/chrome.exe", Path("C:/profile"))
        open_manual_chromium_window()
        self.assertIs(launch.call_args.kwargs["new_window"], True)

    @patch("audapack.components.widget._launch_dedicated_chromium")
    def test_a_refused_launch_is_reported_not_swallowed(self, launch):
        from audapack.components.widget import open_manual_chromium_window

        launch.return_value = (False, "No supported Chromium browser was found.", None, Path("C:/p"))
        ok, message = open_manual_chromium_window()
        self.assertFalse(ok)
        self.assertIn("Chromium", message)


class TestDirectDiscoveryRunsEveryClass(unittest.TestCase):
    """CORE-004 (audit/2.md): the guard sat in the MIDDLE of the module.

    `if __name__ == "__main__": unittest.main()` was at line 118, BEFORE three
    later test classes were even defined, so `python tests/test_components.py`
    started discovery against a half-built module and silently omitted them. A
    ship gate cannot say whether a change is verified if the runner can skip the
    tests that verify it.
    """

    def test_the_unittest_guard_is_the_last_statement(self):
        # Parsed, not string-searched: this module's own source mentions the
        # guard in prose and in assertions, and a substring count would trip
        # over those instead of over a real misplacement.
        import ast

        module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        guards = [
            index for index, node in enumerate(module.body)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ]
        self.assertEqual(len(guards), 1, "more than one entry guard")
        after = module.body[guards[0] + 1:]
        classes = [node.name for node in after if isinstance(node, ast.ClassDef)]
        self.assertEqual(
            classes, [],
            f"defined after the unittest.main() guard, so direct discovery skips them: {classes}",
        )


    def test_every_class_in_this_module_is_discoverable(self):
        loader = unittest.TestLoader()
        import tests.test_components as module

        names = {
            type(case).__name__
            for suite in loader.loadTestsFromModule(module)
            for case in suite
        }
        for expected in (
            "TestComponents",
            "TestWidgetInstallWarmsTheProfile",
            "TestInstallerDoesNotAddAWindow",
            "TestManualAuditWindow",
        ):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
