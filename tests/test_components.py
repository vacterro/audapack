"""Unit tests for Component Center and Widget metadata."""

import unittest
from pathlib import Path
from unittest.mock import patch

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

    @patch("audapack.components.manager.open_widget_in_dedicated_chromium")
    @patch("audapack.components.manager.is_bridge_healthy", return_value=True)
    def test_widget_install_uses_dedicated_profile(self, _healthy, open_dedicated):
        open_dedicated.return_value = (True, "opened")
        cfg = AppConfig()
        cfg.bridge.host = "127.0.0.1"
        cfg.bridge.port = 18765

        ok, message = ComponentManager(cfg).trigger_widget_install()

        self.assertTrue(ok)
        self.assertEqual(message, "opened")
        open_dedicated.assert_called_once_with(
            use_bridge=True,
            bridge_url="http://127.0.0.1:18765/widget.user.js",
        )


if __name__ == "__main__":
    unittest.main()
