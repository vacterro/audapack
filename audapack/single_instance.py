"""Single instance application guard for AUDAPACK."""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path
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

    def is_already_running(self) -> bool:
        if sys.platform == "win32":
            try:
                import ctypes
                ERROR_ALREADY_EXISTS = 183
                mutex_name = f"Local\\{self.name}_MUTEX"
                self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
                last_error = ctypes.windll.kernel32.GetLastError()
                if last_error == ERROR_ALREADY_EXISTS:
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

    def activate_existing_window(self, title_prefix: str = "AUDAPACK"):
        """Restores and brings existing AUDAPACK window to foreground on Windows."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            SW_RESTORE = 9
            EnumWindows = ctypes.windll.user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            ShowWindow = ctypes.windll.user32.ShowWindow
            SetForegroundWindow = ctypes.windll.user32.SetForegroundWindow

            target_hwnd = None

            def foreach_window(hwnd, lParam):
                nonlocal target_hwnd
                if IsWindowVisible(hwnd):
                    length = GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        GetWindowTextW(hwnd, buff, length + 1)
                        title = buff.value
                        if title_prefix.lower() in title.lower() and "widget" not in title.lower() and "brave" not in title.lower():
                            target_hwnd = hwnd
                            return False
                return True

            EnumWindows(EnumWindowsProc(foreach_window), 0)
            if target_hwnd:
                ShowWindow(target_hwnd, SW_RESTORE)
                SetForegroundWindow(target_hwnd)
        except Exception:
            pass
