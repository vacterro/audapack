"""Qt application entry (Wave L). Imports PySide6 only here."""

from __future__ import annotations

import sys


def _force_show_native(hwnd: int) -> bool:
    """Force a Win32 top-level window to become visible (WS_VISIBLE) and shown
    in its restored/normal state. Returns True on success.

    This is the production fix for the "app doesn't open" symptom observed
    when AUDAPACK is launched via ``AUDAPACK.vbs`` (which uses
    ``shell.Run "pythonw AUDAPACK.pyw", 0, False``) or directly via
    ``pythonw``: the STARTUPINFO inherited by the child has
    ``wShowWindow = SW_HIDE`` (window style 0), and Qt's windows platform
    plugin honours that startup show state for the first top-level window
    via ``ShowWindow(SW_SHOWDEFAULT)``. The result is a fully constructed
    MainWindow (correct title, correct size, valid HWND) that never has the
    ``WS_VISIBLE`` bit set -- ``IsWindowVisible`` returns False and the user
    sees no window, no taskbar entry, nothing.

    The cure is to ignore the inherited startup show state and explicitly
    request ``SW_SHOWNORMAL`` on the native HWND. We also nudge the window
    with ``SetWindowPos(SWP_SHOWWINDOW)`` and clear any minimise state, so a
    stuck minimised-from-startup case is covered too.

    Safe no-op on non-Windows: returns False without touching anything.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32

        SW_SHOWNORMAL = 1
        SW_RESTORE = 9
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_SHOWWINDOW = 0x0040
        SWP_FRAMECHANGED = 0x0020

        # 1) Clear any minimise/maximise and force the normal show state.
        #    IsIconic would be True if Windows decided the window starts
        #    minimised (a separate failure mode from SW_HIDE startup).
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        # 2) Belt-and-suspenders: SWP_SHOWWINDOW flips WS_VISIBLE on even
        #    if ShowWindow was a no-op (e.g. already in this state). The
        #    SWP_FRAMECHANGED bit forces a full style recompute.
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
        )
        return True
    except Exception:
        return False


def run_qt_gui(service=None) -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    if service is None:
        service = ProjectService()

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vacterro.audapack.app.1.0")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("AUDAPACK")

    # Set main orange application icon
    from PySide6.QtGui import QFont, QIcon

    from audapack.config import app_dir
    icon_path = app_dir() / "resources" / "app_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # UI.md Iron Law 1: Verdana, non-antialiased everywhere, !important.
    app_font = QFont("Verdana", 9)
    app_font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    app.setFont(app_font)

    window = MainWindow(service)
    # Ensure the window is not stuck in a minimised/maximised pre-state and
    # is a normal top-level. QMainWindow is, but be explicit.
    window.setWindowState(window.windowState() & ~(Qt.WindowMinimized | Qt.WindowMaximized | Qt.WindowFullScreen))

    window.show()

    # Force the native top-level to become visible right away, then again
    # once the event loop has had a chance to run the platform plugin's
    # initial mapping. Qt's SW_SHOWDEFAULT would otherwise honour an
    # inherited SW_HIDE from the launcher and leave WS_VISIBLE unset.
    try:
        hwnd = int(window.winId())
    except Exception:
        hwnd = 0
    if hwnd:
        _force_show_native(hwnd)
        # Re-enforce after the event loop starts, in case the platform
        # plugin's first-tick mapping reasserts the startup hide state.
        QTimer.singleShot(0, lambda: _force_show_native(int(window.winId())))
        QTimer.singleShot(250, lambda: _force_show_native(int(window.winId())))

    # Qt-side activation: raise + activate (the native force above already
    # set WS_VISIBLE; this is the user-facing bring-to-front).
    window.raise_()
    window.activateWindow()

    return app.exec()


main = run_qt_gui
run_qt_app = run_qt_gui

