"""Single instance application guard for AUDAPACK."""

from __future__ import annotations

import atexit
import sys
from typing import Optional

from audapack.config import get_state_dir


class SingleInstance:
    """Enforces a single running instance of AUDAPACK per user session.

    On Windows: uses a named Win32 Mutex and activates the existing window.
    On POSIX: uses an exclusive advisory file lock.
    """

    def __init__(self, name: str = "AUDAPACK_GUI"):
        self.name = name
        self._mutex = None
        self._file_handle = None
        self._is_already_running = False

    def _find_window_hwnd(self, title_prefix: str = "AUDAPACK") -> Optional[int]:
        """Return the first visible top-level HWND whose title identifies it as an
        AUDAPACK application window, or None if no such window exists.

        Identification is intentionally TIGHT: the bare "AUDAPACK" substring is
        not enough because editors/IDEs that happen to be open on the AUDAPACK
        project (e.g. OpenCode/VS Code) also show "AUDAPACK" in their title bar.
        We require either the distinctive em-dash marker that the MainWindow
        uses ("AUDAPACK \\u2014 Project Room") or the Settings dialog marker
        ("AUDAPACK Settings"), or a caller-supplied prefix. This avoids
        false-positive "already running" decisions caused by the IDE window.

        Returns None on non-Windows or on error."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            EnumWindows = ctypes.windll.user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            captured = {"hwnd": None}

            # The em-dash "—" is the MainWindow's distinctive marker. Bare
            # "AUDAPACK" matches too many unrelated windows (the IDE itself,
            # file explorer breadcrumbs, etc.) so we treat it as a hint, not
            # as identification.
            app_markers = (
                "audapack \u2014 project room",  # MainWindow
                "audapack settings",  # Settings dialog
            )

            def _matches(title_lower: str) -> bool:
                if any(m in title_lower for m in app_markers):
                    return True
                # Caller-supplied prefix still works (kept for backwards compat
                # and explicit override), but exclude obvious IDE/editor noise.
                if title_prefix and title_prefix.lower() in title_lower:
                    if not any(
                        noise in title_lower for noise in (" | ", " - ", "—", "opencode", "code -", "visual studio")
                    ):
                        return True
                return False

            def foreach_window(hwnd, lParam):
                if IsWindowVisible(hwnd):
                    length = GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        GetWindowTextW(hwnd, buff, length + 1)
                        title = buff.value
                        if _matches(title.lower()):
                            captured["hwnd"] = hwnd
                            return False
                return True

            EnumWindows(EnumWindowsProc(foreach_window), 0)
            return captured["hwnd"]
        except Exception:
            return None

    def is_already_running(self) -> bool:
        if sys.platform == "win32":
            try:
                import ctypes

                ERROR_ALREADY_EXISTS = 183
                mutex_name = f"Local\\{self.name}_MUTEX"
                self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
                last_error = ctypes.windll.kernel32.GetLastError()
                if last_error == ERROR_ALREADY_EXISTS:
                    # Mutex is held by someone. Before treating this as "another AUDAPACK
                    # instance is running and we should yield", verify an actual AUDAPACK
                    # window is reachable. A windowless/wunged/stuck mutex holder
                    # (e.g. a zombie AUDAPACK.pyw hung before window.show) would
                    # otherwise permanently brick the launcher: every new launch would
                    # see is_already_running() == True, activate_existing_window()
                    # would find no window, and main() would silently return 0.
                    # Self-correct: if no window matches, release our failed-attempt
                    # handle and report False so a new instance can open.
                    hwnd = self._find_window_hwnd("AUDAPACK")
                    if hwnd is None:
                        try:
                            ctypes.windll.kernel32.CloseHandle(self._mutex)
                        except Exception:
                            pass
                        self._mutex = None
                        self._is_already_running = False
                        return False
                    self._is_already_running = True
                    return True
                atexit.register(self.release)
                return False
            except Exception:
                return False
        else:
            lock_file = get_state_dir() / f"{self.name.lower()}.lock"
            try:
                import fcntl

                self._file_handle = open(lock_file, "w")
                fcntl.flock(self._file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                atexit.register(self.release)
                return False
            except Exception:
                self._is_already_running = True
                return True

    def release(self):
        if sys.platform == "win32" and self._mutex:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._mutex)
                self._mutex = None
            except Exception:
                pass
        elif self._file_handle:
            try:
                import fcntl

                fcntl.flock(self._file_handle, fcntl.LOCK_UN)
                self._file_handle.close()
                self._file_handle = None
            except Exception:
                pass

    def activate_existing_window(self, title_prefix: str = "AUDAPACK") -> bool:
        """Restores and brings existing AUDAPACK window to foreground on Windows.

        Returns True if a matching window was found and foregrounded, False otherwise
        (caller can use this to detect a zombie mutex holder and recover)."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            SW_RESTORE = 9
            ShowWindow = ctypes.windll.user32.ShowWindow
            SetForegroundWindow = ctypes.windll.user32.SetForegroundWindow

            target_hwnd = self._find_window_hwnd(title_prefix)
            if target_hwnd:
                ShowWindow(target_hwnd, SW_RESTORE)
                SetForegroundWindow(target_hwnd)
                return True
            return False
        except Exception:
            return False
