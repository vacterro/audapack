"""Unit tests for Component Center and Widget metadata."""

import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
