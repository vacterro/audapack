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


def _build_app_icon():
    """Builds the canonical multi-size application icon from shipped resources.

    resources/app_icon.ico is a genuine multi-resolution Windows icon
    (16/32/48/256 embedded; regenerate with tools/build_app_icon.py). It is
    the canonical source: QIcon picks the native resolution per surface
    (title bar 16, taskbar 32/48, Alt-Tab/shell 256) instead of scaling one
    bitmap. The PNGs remain registered as explicit fallbacks so a broken or
    missing ICO still leaves the app with an icon. Returns None when no icon
    resource exists (app_dir layout drifted).
    """
    from pathlib import Path

    from PySide6.QtCore import QSize
    from PySide6.QtGui import QIcon

    from audapack.config import app_dir

    res = Path(app_dir()) / "resources"
    icon = QIcon()
    for name, size in (
        ("app_icon.ico", None),  # multi-size container; do not lie about one size
        ("app_icon.png", 256),
        ("app_icon_32.png", 32),
        ("app_icon_16.png", 16),
    ):
        p = res / name
        if p.exists():
            if size is None:
                icon.addFile(str(p))
            else:
                icon.addFile(str(p), QSize(size, size))
    return icon if not icon.isNull() else None


# --------------------------------------------------------------------------
# Native Windows window-icon binding (T-176 Part B).
#
# Qt's setWindowIcon alone is not always enough on Windows: the taskbar
# button can keep the process executable's default icon (e.g. pythonw.exe)
# when the shell already cached an identity for the HWND class. The legacy
# Tkinter UI sends WM_SETICON explicitly; the Qt path now does the same.
#
# HICON lifetime: handles are loaded ONCE into a module-level cache and are
# never destroyed while the process lives ( DestroyIcon on them while an HWND
# still references the icon corrupts the window). One set per process, no
# per-timer leak.
# --------------------------------------------------------------------------

_NATIVE_ICON_CACHE: dict = {"small": None, "big": None}


def _native_icon_dimensions():
    """Real system icon metrics, or (0, 0) sized pairs when unavailable."""
    import ctypes

    user32 = ctypes.windll.user32
    sm_cxicon = user32.GetSystemMetrics(11)  # SM_CXICON
    sm_cyicon = user32.GetSystemMetrics(12)  # SM_CYICON
    sm_cxsmicon = user32.GetSystemMetrics(49)  # SM_CXSMICON
    sm_cysmicon = user32.GetSystemMetrics(50)  # SM_CYSMICON
    return (sm_cxicon, sm_cyicon), (sm_cxsmicon, sm_cysmicon)


def _load_native_icons():
    """Loads the small + big HICON pair from the shipped ICO once per process.

    Returns (small_hicon, big_hicon); both nonzero on success. Reuses cached
    handles on every subsequent call (no LoadImageW storm, no leak).
    """
    import ctypes
    from pathlib import Path

    from audapack.config import app_dir

    if _NATIVE_ICON_CACHE["small"] and _NATIVE_ICON_CACHE["big"]:
        return _NATIVE_ICON_CACHE["small"], _NATIVE_ICON_CACHE["big"]

    ico = Path(app_dir()) / "resources" / "app_icon.ico"
    if not ico.exists():
        return 0, 0

    user32 = ctypes.windll.user32
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010

    (big_w, big_h), (small_w, small_h) = _native_icon_dimensions()
    small = user32.LoadImageW(
        None, str(ico), IMAGE_ICON, small_w or 16, small_h or 16, LR_LOADFROMFILE
    )
    big = user32.LoadImageW(
        None, str(ico), IMAGE_ICON, big_w or 32, big_h or 32, LR_LOADFROMFILE
    )
    _NATIVE_ICON_CACHE["small"] = int(small) if small else 0
    _NATIVE_ICON_CACHE["big"] = int(big) if big else 0
    return _NATIVE_ICON_CACHE["small"], _NATIVE_ICON_CACHE["big"]


def _apply_native_window_icon(hwnd: int) -> bool:
    """Sends WM_SETICON (ICON_SMALL + ICON_BIG) for a real top-level HWND.

    Windows-only helper; a safe no-op returning False off Windows, without
    hwnd, or when the native icon load fails (startup must never crash over
    an icon). Reuses the cached HICON pair (see module docstring above).
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        small, big = _load_native_icons()
        if not small and not big:
            return False
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        sent = False
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            sent = True
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            sent = True
        return sent
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

    # Set main orange application icon (multi-size, deterministic rendering)
    from PySide6.QtGui import QFont

    app_icon = _build_app_icon()
    if app_icon is not None:
        app.setWindowIcon(app_icon)

    # UI.md Iron Law 1: Verdana, non-antialiased everywhere, !important.
    app_font = QFont("Verdana", 9)
    app_font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    app.setFont(app_font)

    window = MainWindow(service)
    # Explicit window-level icon: taskbar/title-bar rendering on Windows
    # intermittently misses the inherited QApplication icon.
    if app_icon is not None:
        window.setWindowIcon(app_icon)
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
        # T-176: the shell may keep the pythonw taskbar identity unless the
        # HWND itself carries the icon. Bind small+big HICONs at the same
        # native-window boundary, and re-enforce on the EXISTING
        # first-event-loop shots (no independent timer storm, no new HICON
        # load per callback -- the handles are cached).
        _apply_native_window_icon(hwnd)
        # Re-enforce after the event loop starts, in case the platform
        # plugin's first-tick mapping reasserts the startup hide state.
        QTimer.singleShot(0, lambda: _force_show_native(int(window.winId())))
        QTimer.singleShot(250, lambda: _force_show_native(int(window.winId())))
        QTimer.singleShot(0, lambda: _apply_native_window_icon(int(window.winId())))
        QTimer.singleShot(250, lambda: _apply_native_window_icon(int(window.winId())))

    # Qt-side activation: raise + activate (the native force above already
    # set WS_VISIBLE; this is the user-facing bring-to-front).
    window.raise_()
    window.activateWindow()

    return app.exec()


main = run_qt_gui
run_qt_app = run_qt_gui

