"""Regression tests for the launcher visibility fix (T-27).

The real production failure was a Qt MainWindow constructed with the correct
title and size but never receiving WS_VISIBLE: the .vbs launcher runs pythonw
with WindowStyle=0 (SW_HIDE), Qt's windows platform plugin honours the
inherited startup show state for the first top-level window, and the user
sees no window.

The fix in audapack.ui_qt.app._force_show_native ignores the inherited state
and forces SW_SHOWNORMAL + SWP_SHOWWINDOW on the native top-level HWND. These
tests pin the contract so a refactor cannot silently drop the native force.

The full "real .vbs produces a visible AUDAPACK window" verification lives in
scripts/test_real_launcher.py (run manually on a desktop session; the headless
agent cannot reliably produce WS_VISIBLE in a window-station it does not own).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


class TestForceShowNativeContract(unittest.TestCase):
    """_force_show_native MUST be a no-op on non-Windows and MUST call
    ShowWindow(SW_SHOWNORMAL) + SetWindowPos(SWP_SHOWWINDOW) on Windows.
    The latter is the cure: it flips WS_VISIBLE regardless of the inherited
    SW_HIDE startup show state from the .vbs launcher.
    """

    def test_non_windows_is_noop(self):
        with patch("sys.platform", "linux"):
            from audapack.ui_qt.app import _force_show_native
            self.assertFalse(_force_show_native(0xCAFE))

    def test_zero_hwnd_is_noop(self):
        from audapack.ui_qt.app import _force_show_native
        with patch("sys.platform", "win32"):
            self.assertFalse(_force_show_native(0))

    def test_windows_calls_showwindow_and_setwindowpos(self):
        from audapack.ui_qt.app import _force_show_native

        user32 = MagicMock()
        user32.IsIconic.return_value = False  # not minimised
        with patch("sys.platform", "win32"):
            with patch.dict(sys.modules, {"ctypes": MagicMock(windll=MagicMock(user32=user32))}):
                # Build a real-ish ctypes substitute by using the actual ctypes
                # and patching only user32. Simpler: call the function with a
                # real ctypes but assert via the imported ctypes.windll.user32.
                # We use a thin shim: re-import ctypes path inside the function
                # reads `ctypes.windll.user32`. Patch that attribute.
                pass
        # Direct path: import ctypes, patch ctypes.windll.user32 attribute by
        # wrapping IsIconic/ShowWindow/SetWindowPos with MagicMocks and assert.
        import ctypes
        original_user32 = ctypes.windll.user32
        mock_user32 = MagicMock()
        mock_user32.IsIconic.return_value = False
        try:
            ctypes.windll.user32 = mock_user32
            with patch("sys.platform", "win32"):
                ok = _force_show_native(0x1234)
        finally:
            ctypes.windll.user32 = original_user32

        self.assertTrue(ok, "force must succeed on Windows with a real ctypes")
        # ShowWindow called at least once; the SW_SHOWNORMAL (1) call is the cure.
        show_calls = mock_user32.ShowWindow.call_args_list
        self.assertTrue(show_calls, "ShowWindow must be called")
        sw_shownormal_used = any(args and args[0] == 0x1234 and args[1] == 1 for args, _ in show_calls)
        self.assertTrue(sw_shownormal_used, "ShowWindow(hwnd, SW_SHOWNORMAL=1) must be called")
        # SetWindowPos must be called with SWP_SHOWWINDOW (0x0040) in the flags.
        swp_calls = mock_user32.SetWindowPos.call_args_list
        self.assertTrue(swp_calls, "SetWindowPos must be called")
        flags_used = swp_calls[0][0][6]  # 7th positional: flags
        self.assertEqual(flags_used & 0x0040, 0x0040, "SWP_SHOWWINDOW (0x0040) must be in flags")
        # First arg is the hwnd.
        self.assertEqual(swp_calls[0][0][0], 0x1234)

    def test_minimised_window_is_restored_first(self):
        """If Windows started the window minimised (IsIconic=True), force a
        SW_RESTORE before the SW_SHOWNORMAL so the user gets a normal window
        rather than a minimised one."""
        from audapack.ui_qt.app import _force_show_native
        import ctypes
        original_user32 = ctypes.windll.user32
        mock_user32 = MagicMock()
        mock_user32.IsIconic.return_value = True
        try:
            ctypes.windll.user32 = mock_user32
            with patch("sys.platform", "win32"):
                _force_show_native(0x5678)
        finally:
            ctypes.windll.user32 = original_user32

        show_calls = mock_user32.ShowWindow.call_args_list
        # First ShowWindow should be SW_RESTORE (9), then SW_SHOWNORMAL (1).
        self.assertEqual(show_calls[0][0], (0x5678, 9), "minimised window must be restored first")
        self.assertEqual(show_calls[1][0], (0x5678, 1), "then forced to SW_SHOWNORMAL")


class TestRunQtGuiInvokesForceShow(unittest.TestCase):
    """run_qt_gui must construct the window, call show(), AND force the
    native top-level visible. A refactor that drops the native force must
    fail this test, because the whole point of the fix is that
    Qt's .show() alone is not enough under the .vbs launcher.
    """

    def test_run_qt_gui_calls_force_show_native(self):
        from audapack.ui_qt import app as qt_app_mod

        # Patch QApplication, MainWindow, and the force helper so the test
        # does not need a real Qt event loop.
        with patch.object(qt_app_mod, "_force_show_native") as mock_force:
            with patch("PySide6.QtCore.QTimer") as mock_timer:
                with patch("PySide6.QtWidgets.QApplication") as mock_qapp:
                    mock_app_instance = MagicMock()
                    mock_qapp.return_value = mock_app_instance
                    with patch("audapack.ui_qt.main_window.MainWindow") as mock_mw:
                        mock_window = MagicMock()
                        # winId() must return an int (real HWND in production).
                        mock_window.winId.return_value = 0xBEEF
                        mock_mw.return_value = mock_window
                        with patch("audapack.services.project_service.ProjectService") as mock_svc:
                            mock_svc.return_value = MagicMock()
                            # app.exec() must return immediately (no real loop).
                            mock_app_instance.exec.return_value = 0
                            rc = qt_app_mod.run_qt_gui()

        self.assertEqual(rc, 0)
        # The window's .show() was called.
        mock_window.show.assert_called_once()
        # _force_show_native was called with the window's hwnd at least once
        # (immediately). The deferred QTimer.singleShot calls are guarded by
        # the QTimer patch above; we assert the immediate force.
        force_calls = [c for c in mock_force.call_args_list if c[0] and c[0][0] == 0xBEEF]
        self.assertTrue(
            force_calls,
            "_force_show_native(winId) must be called by run_qt_gui -- this is the cure",
        )
        # And the window activation helpers were called (raise_/activateWindow).
        mock_window.raise_.assert_called_once()
        mock_window.activateWindow.assert_called_once()


if __name__ == "__main__":
    unittest.main()
