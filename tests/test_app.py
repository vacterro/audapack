"""Unit tests for AUDAPACK CLI entry points and arguments."""

import io
import unittest
from unittest.mock import patch

from audapack import __version__
from audapack.app import main


class TestAppCLI(unittest.TestCase):
    def test_cli_status(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--status"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn(f"AUDAPACK v{__version__} Status", output)
        self.assertIn("Audit Root:", output)

    def test_gui_startup_instantiation(self):
        import tkinter as tk

        from audapack.ui.main_window import MainWindow

        try:
            root = tk.Tk()
            app = MainWindow(root)
            self.assertIsNotNone(app)
            self.assertIsNotNone(app.saipen_cache)
            root.destroy()
        except tk.TclError:
            pass  # headless environments without display


if __name__ == "__main__":
    unittest.main()
