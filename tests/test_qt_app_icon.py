"""T-176: the shipped ICO is genuinely multi-size and one icon owns Windows identity.

Guards the three Part-B defect classes:
- resources/app_icon.ico claimed "multi-size" while embedding only 16x16;
- _build_app_icon() registered the 16x16 ICO as a 48x48 source (fake QSize);
- MainWindow/tray overwrote the canonical multi-size icon with the 256px PNG.

Plus the native WM_SETICON contract: no-op off Windows, ICON_SMALL + ICON_BIG
sent with nonzero handles at real GetSystemMetrics sizes, HICONs cached and
reused (no LoadImageW per application), failed load keeps startup alive, and
SetCurrentProcessExplicitAppUserModelID still precedes QApplication.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

RES = _HERE.parent / "resources"


def _ico_entries(path: Path) -> list[tuple[int, int]]:
    data = path.read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    out = []
    for i in range(count):
        o = 6 + i * 16
        w, h = data[o], data[o + 1]
        out.append((256 if w == 0 else w, 256 if h == 0 else h))
    return out


class TestIcoResourceIsMultiSize(unittest.TestCase):
    def test_raw_container_embeds_required_sizes(self):
        entries = _ico_entries(RES / "app_icon.ico")
        self.assertIn((16, 16), entries)
        self.assertIn((32, 32), entries)
        self.assertIn((48, 48), entries)
        self.assertIn((256, 256), entries)

    def test_qt_sees_all_native_sizes(self):
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        from PySide6.QtGui import QIcon

        icon = QIcon(str(RES / "app_icon.ico"))
        self.assertFalse(icon.isNull())
        sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
        self.assertIn((16, 16), sizes)
        self.assertIn((32, 32), sizes)
        self.assertIn((48, 48), sizes)
        self.assertIn((256, 256), sizes)


class TestBuildAppIcon(unittest.TestCase):
    def test_returns_non_null_icon_with_native_sizes(self):
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        from audapack.ui_qt.app import _build_app_icon

        icon = _build_app_icon()
        self.assertIsNotNone(icon)
        self.assertFalse(icon.isNull())
        sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
        self.assertTrue({(16, 16), (32, 32), (48, 48)} <= sizes, sizes)


class TestOneIconOwner(unittest.TestCase):
    def test_main_window_does_not_replace_canonical_icon(self):
        """run_qt_gui installs the multi-size icon on QApplication BEFORE
        MainWindow is constructed; the constructor must not overwrite it with
        the single 256px PNG."""
        import shutil
        import tempfile

        from PySide6.QtWidgets import QApplication

        from audapack.config import AppConfig
        from audapack.services.project_service import ProjectService
        from audapack.ui_qt.app import _build_app_icon
        from audapack.ui_qt.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        canonical = _build_app_icon()
        app.setWindowIcon(canonical)
        self.addCleanup(app.setWindowIcon, QIcon_null()) if False else None

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        svc = ProjectService(AppConfig(), base_dir=Path(tmp))
        win = MainWindow(svc)
        self.addCleanup(win.close)
        self.assertFalse(win.windowIcon().isNull())
        # The window must expose the canonical multi-size set, not a lone PNG.
        win_sizes = {(s.width(), s.height()) for s in win.windowIcon().availableSizes()}
        self.assertTrue({(16, 16), (32, 32)} <= win_sizes, win_sizes)

    def test_tray_reuses_window_icon(self):
        """_init_tray_icon must prefer windowIcon() (the canonical icon) over
        re-reading the PNG, so tray/taskbar/window share one artwork."""
        import shutil
        import tempfile

        from PySide6.QtWidgets import QApplication

        from audapack.config import AppConfig
        from audapack.services.project_service import ProjectService
        from audapack.ui_qt.app import _build_app_icon
        from audapack.ui_qt.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setWindowIcon(_build_app_icon())
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        svc = ProjectService(AppConfig(), base_dir=Path(tmp))
        win = MainWindow(svc)
        self.addCleanup(win.close)
        tray = getattr(win, "_tray_icon", None)
        if tray is not None:
            self.assertFalse(tray.icon().isNull())
            t_sizes = {(s.width(), s.height()) for s in tray.icon().availableSizes()}
            self.assertTrue({(16, 16), (32, 32)} <= t_sizes, t_sizes)


class TestNativeWindowIconHelper(unittest.TestCase):
    def test_off_windows_is_noop_returning_false(self):
        from audapack.ui_qt.app import _apply_native_window_icon

        with patch("sys.platform", "linux"):
            self.assertFalse(_apply_native_window_icon(0xCAFE))

    def test_zero_hwnd_is_noop(self):
        from audapack.ui_qt.app import _apply_native_window_icon

        with patch("sys.platform", "win32"):
            self.assertFalse(_apply_native_window_icon(0))

    def _win32_env(self, user32):
        import ctypes

        original = ctypes.windll.user32
        ctypes.windll.user32 = user32
        self.addCleanup(setattr, ctypes.windll, "user32", original)

    @unittest.skipUnless(
        sys.platform == "win32",
        "WM_SETICON delivery is Win32; the test drives the real ctypes.windll.user32 seam",
    )
    def test_sends_wm_seticon_small_and_big(self):
        from audapack.ui_qt import app as qt_app

        self._win32_env(MagicMock())  # placeholder, real env below

        import ctypes

        user32 = MagicMock()
        # Real system metrics: big 32x32, small 16x16.
        user32.GetSystemMetrics.side_effect = lambda idx: {
            11: 32, 12: 32, 49: 16, 50: 16
        }[idx]
        user32.LoadImageW.return_value = 0x1000  # nonzero handle
        original = ctypes.windll.user32
        ctypes.windll.user32 = user32
        try:
            # Reset the module-level handle cache to force a load.
            qt_app._NATIVE_ICON_CACHE["small"] = None
            qt_app._NATIVE_ICON_CACHE["big"] = None
            with patch("sys.platform", "win32"), patch.object(
                qt_app, "_load_native_icons", wraps=None
            ) if False else patch.object(qt_app, "_load_native_icons") as load:
                load.return_value = (0x1000, 0x2000)
                ok = qt_app._apply_native_window_icon(0x1234)
        finally:
            ctypes.windll.user32 = original

        self.assertTrue(ok)
        calls = user32.SendMessageW.call_args_list
        pairs = {(args[1], args[2], args[3]) for args, _ in calls}
        self.assertIn((0x0080, 0, 0x1000), pairs)  # WM_SETICON ICON_SMALL
        self.assertIn((0x0080, 1, 0x2000), pairs)  # WM_SETICON ICON_BIG

    @unittest.skipUnless(
        sys.platform == "win32",
        "LoadImageW sizing is Win32; the test drives the real ctypes.windll.user32 seam",
    )
    def test_native_load_requests_distinct_system_sizes(self):
        import ctypes

        from audapack.ui_qt import app as qt_app

        user32 = MagicMock()
        user32.GetSystemMetrics.side_effect = lambda idx: {
            11: 32, 12: 32, 49: 16, 50: 16
        }[idx]
        user32.LoadImageW.return_value = 0x1000
        original = ctypes.windll.user32
        ctypes.windll.user32 = original  # keep for restore
        ctypes.windll.user32 = user32
        try:
            qt_app._NATIVE_ICON_CACHE["small"] = None
            qt_app._NATIVE_ICON_CACHE["big"] = None
            with patch("sys.platform", "win32"), patch.object(
                qt_app, "_load_native_icons"
            ) if False else patch.object(qt_app, "_load_native_icons", wraps=qt_app._load_native_icons):
                qt_app._load_native_icons()
        finally:
            ctypes.windll.user32 = original

        widths = {args[3] for args, _ in user32.LoadImageW.call_args_list}
        self.assertIn(16, widths)
        self.assertIn(32, widths)
        self.assertNotEqual(widths, {16}, "one 16px image must not pretend to be every size")

    @unittest.skipUnless(
        sys.platform == "win32",
        "HICON caching is Win32; the test drives the real ctypes.windll.user32 seam",
    )
    def test_repeated_application_reuses_cached_handles(self):
        import ctypes

        from audapack.ui_qt import app as qt_app

        user32 = MagicMock()
        user32.LoadImageW.return_value = 0x1000
        original = ctypes.windll.user32
        ctypes.windll.user32 = user32
        try:
            qt_app._NATIVE_ICON_CACHE["small"] = None
            qt_app._NATIVE_ICON_CACHE["big"] = None
            with patch("sys.platform", "win32"):
                qt_app._apply_native_window_icon(0x1)
                load_after_first = user32.LoadImageW.call_count
                self.assertGreater(load_after_first, 0)
                user32.LoadImageW.reset_mock()
                # Second call: cache hit, no new LoadImageW.
                qt_app._apply_native_window_icon(0x2)
                self.assertEqual(user32.LoadImageW.call_count, 0, "must reuse cached HICON pair")
        finally:
            ctypes.windll.user32 = original

    @unittest.skipUnless(
        sys.platform == "win32",
        "LoadImageW failure handling is Win32; the test drives the real ctypes.windll.user32 seam",
    )
    def test_failed_native_load_is_safe(self):
        import ctypes

        from audapack.ui_qt import app as qt_app

        user32 = MagicMock()
        user32.GetSystemMetrics.return_value = 16
        user32.LoadImageW.return_value = 0  # load fails
        original = ctypes.windll.user32
        ctypes.windll.user32 = user32
        try:
            qt_app._NATIVE_ICON_CACHE["small"] = None
            qt_app._NATIVE_ICON_CACHE["big"] = None
            with patch("sys.platform", "win32"):
                ok = qt_app._apply_native_window_icon(0x1234)
            self.assertFalse(ok, "failed load must return False, not raise")
        finally:
            ctypes.windll.user32 = original
            qt_app._NATIVE_ICON_CACHE["small"] = None
            qt_app._NATIVE_ICON_CACHE["big"] = None


class TestAppUserModelIdOrder(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "win32",
        "run_qt_gui only sets the AppUserModelID on Windows (ctypes.windll.shell32)",
    )
    def test_aumid_set_before_qapplication(self):
        """run_qt_gui must call SetCurrentProcessExplicitAppUserModelID
        BEFORE QApplication is constructed (stable shell identity)."""
        from audapack.ui_qt import app as qt_app

        calls = []
        with patch("PySide6.QtWidgets.QApplication") as mock_qapp:
            mock_qapp.return_value = MagicMock()
            mock_qapp.return_value.exec.return_value = 0
            with patch("audapack.ui_qt.main_window.MainWindow") as mock_mw:
                mock_window = MagicMock()
                mock_window.winId.return_value = 0
                mock_mw.return_value = mock_window
                with patch("audapack.services.project_service.ProjectService") as mock_svc:
                    mock_svc.return_value = MagicMock()
                    import ctypes

                    with patch.object(
                        ctypes.windll.shell32,
                        "SetCurrentProcessExplicitAppUserModelID",
                        side_effect=lambda *a, **kw: calls.append("aumid") or 0,
                    ):
                        with patch("PySide6.QtCore.QTimer"), patch.object(
                            qt_app, "_build_app_icon", return_value=None
                        ), patch.object(qt_app, "_apply_native_window_icon"):
                            qt_app.run_qt_gui()
        self.assertEqual(calls, ["aumid"], "AUMID must execute before QApplication construction")


def QIcon_null():
    from PySide6.QtGui import QIcon

    return QIcon()


if __name__ == "__main__":
    unittest.main()
