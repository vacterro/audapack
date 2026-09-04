"""Single instance application guard for AUDAPACK."""

from __future__ import annotations

import atexit
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from audapack.config import get_state_dir


class GuardEstablishmentError(RuntimeError):
    """Raised when the single-instance guard cannot be reliably established.

    W2-007: a failed guard must not fail open (permit a second instance).
    The launcher must surface the error rather than silently starting.
    """


#: How long a launcher waits for a live owner to put its window up before it
#: reports "already running" without having activated anything. A Qt MainWindow
#: reaches WS_VISIBLE in well under a second; this only has to outlast that.
OWNER_WINDOW_GRACE_SECONDS = 6.0


def _process_is_alive(pid: int) -> bool:
    """Is this PID a live process right now? Total over every parsed integer.

    W2-005 (audit/4.md): `os.kill(pid, 0)` raises OverflowError for a value
    beyond the platform's signed-int range -- measured with 4,000,000,000 on
    POSIX -- and nothing caught it, so a corrupt owner record could crash the
    launcher's second-instance handling instead of producing a decision. An
    unqueryable PID cannot be proven dead, and "dead" is the only answer that
    ever authorizes a second GUI.
    """
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError, OverflowError):
        return True
    if pid_int <= 0 or pid_int == os.getpid():
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int & 0xFFFFFFFF)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    # Cannot prove it is gone, so it is not treated as gone.
                    return True
                return int(code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # An unanswerable question is never answered "dead": that is the
            # answer that authorizes a second GUI.
            return True
    try:
        os.kill(pid_int, 0)
    except (ProcessLookupError, OverflowError):
        # OverflowError: no signal can be sent to a value this large, which is
        # proof enough that it is not one of OUR processes (PIDs are bounded far
        # below it), and definitely not a live owner.
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# The em-dash "—" is the MainWindow's distinctive marker. Bare "AUDAPACK"
# matches too many unrelated windows (the IDE itself, file explorer breadcrumbs,
# Explorer windows titled with the project folder name), so a title is ONLY
# identified as the application by these distinctive production markers — never
# by an arbitrary "AUDAPACK" substring.
AUDAPACK_WINDOW_MARKERS = (
    "audapack \u2014 project room",  # MainWindow
    "audapack settings",  # Settings dialog
)


def is_audapack_window_title(title: str) -> bool:
    """Pure title classifier: True only for distinctive AUDAPACK app windows.

    Extracted so tests exercise the exact production predicate. A generic
    "AUDAPACK" substring (Explorer folder windows, IDE breadcrumbs, editors
    open on the project) must return False — that is the whole point.
    """
    title_lower = str(title or "").lower()
    return any(marker in title_lower for marker in AUDAPACK_WINDOW_MARKERS)


class SingleInstance:
    """Enforces a single running instance of AUDAPACK per user session.

    On Windows: uses a named Win32 Mutex and activates the existing window.
    On POSIX: uses an exclusive advisory file lock.
    """

    def __init__(self, name: str = "AUDAPACK_GUI"):
        self.name = name
        self._mutex = None
        #: The handle to the PRIMARY named object, kept for this instance's whole
        #: life so the namespace survives the zombie it recovered from (W2-004).
        self._primary_mutex = None
        self._recovery_mutex = None
        self._file_handle = None
        self._is_already_running = False
        #: Whoever holds the guard, when this launcher found it already held.
        self._owner_pid = 0

    # ------------------------------------------------------------------ #
    # owner record: who holds this guard, and is that process still alive
    # ------------------------------------------------------------------ #

    def _owner_record_path(self) -> Path:
        return get_state_dir() / f"{self.name.lower()}.owner.json"

    def _write_owner_record(self) -> None:
        """Record the owning PID, best effort: the guard itself still rules."""
        try:
            path = self._owner_record_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"pid": os.getpid(), "started_at": time.time()}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _read_owner_pid(self) -> int:
        try:
            data = json.loads(self._owner_record_path().read_text(encoding="utf-8"))
            return int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def owner_is_alive(self) -> bool:
        """True when the process holding this guard is demonstrably running.

        W2-005: this is the question `is_already_running` used to answer with
        "can I see a window", which a launcher racing a normal startup gets
        wrong. An unreadable/absent record answers False -- that is the pre-
        record behaviour, and the Win32 window check still gates takeover.
        """
        pid = self._owner_pid or self._read_owner_pid()
        return _process_is_alive(pid)

    def wait_for_owner_window(self, timeout: float = OWNER_WINDOW_GRACE_SECONDS) -> Optional[int]:
        """Give a live owner that is still starting time to show its window."""
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            hwnd = self._find_window_hwnd()
            if hwnd is not None:
                return hwnd
            if time.time() >= deadline:
                return None
            time.sleep(0.15)

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
            # file explorer breadcrumbs, Explorer windows showing the project
            # folder) so a title is never identified by the bare substring.

            def _matches(title_lower: str) -> bool:
                # No generic prefix fallback: `_AUDAPACK` Explorer / IDE titles
                # must not be mistaken for the application window. The caller
                # prefix parameter is retained for signature compatibility only.
                return is_audapack_window_title(title_lower)

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
                if not self._mutex:
                    raise GuardEstablishmentError(
                        f"CreateMutexW for '{mutex_name}' returned a null handle"
                    )
                last_error = ctypes.windll.kernel32.GetLastError()
                if last_error == ERROR_ALREADY_EXISTS:
                    # W2-005 (audit/1.md): a live owner keeps the guard, window
                    # or no window. A process sitting between CreateMutexW and
                    # window.show() is observationally identical to the "zombie"
                    # this branch was written for, so a rapid second launch used
                    # to open a second GUI beside a perfectly healthy first one
                    # -- and two GUIs is the multiple-writer condition CORE-002
                    # is about. Takeover now needs the recorded owner to be
                    # provably gone.
                    self._owner_pid = self._read_owner_pid()
                    if self.owner_is_alive():
                        self._is_already_running = True
                        return True
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
                        # W2-004 (audit/2.md): the primary handle is KEPT.
                        # Closing it made the recovered GUI stop holding the
                        # primary namespace, so once the zombie finally exited
                        # the named object disappeared with it -- a third
                        # launcher then saw no primary mutex at all, created a
                        # fresh one, never consulted the recovery mutex, and was
                        # admitted as a second full GUI. Our handle to the
                        # EXISTING object keeps that object alive for exactly as
                        # long as this instance lives, which is what continuity
                        # means; the recovery mutex only serializes competing
                        # recovery launchers.
                        self._primary_mutex = self._mutex
                        recovery_name = f"Local\\{self.name}_RECOVERY_MUTEX"
                        recovery = ctypes.windll.kernel32.CreateMutexW(None, False, recovery_name)
                        if not recovery:
                            raise GuardEstablishmentError(
                                f"CreateMutexW for recovery guard '{recovery_name}' returned a null handle"
                            )
                        recovery_error = ctypes.windll.kernel32.GetLastError()
                        if recovery_error == ERROR_ALREADY_EXISTS:
                            ctypes.windll.kernel32.CloseHandle(recovery)
                            raise GuardEstablishmentError(
                                "Another launcher is already recovering a windowless instance"
                            )
                        self._recovery_mutex = recovery
                        atexit.register(self.release)
                        self._write_owner_record()
                        self._is_already_running = False
                        return False
                    self._is_already_running = True
                    return True
                self._primary_mutex = self._mutex
                atexit.register(self.release)
                self._write_owner_record()
                return False
            except GuardEstablishmentError:
                raise
            except Exception as exc:
                raise GuardEstablishmentError(
                    f"Single-instance guard establishment failed on Win32: {exc}"
                ) from exc
        else:
            lock_file = get_state_dir() / f"{self.name.lower()}.lock"
            try:
                import fcntl
            except ImportError as exc:
                # No advisory locking available: the guard cannot be established
                # at all, which is a failure to report, never permission to
                # start a second writer.
                raise GuardEstablishmentError(
                    f"Single-instance guard needs advisory file locking: {exc}"
                ) from exc
            try:
                self._file_handle = open(lock_file, "w")
                fcntl.flock(self._file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                atexit.register(self.release)
                self._write_owner_record()
                return False
            except OSError:
                # The lock is held (BlockingIOError) or unreachable. Either way
                # this launcher does not own the guard, so it does not start a
                # GUI: on POSIX `activate_existing_window` can never succeed, and
                # app.main() used to read that failure as "leftover lock, open
                # another one" -- making every genuine second instance on POSIX
                # fail open by construction (W2-005).
                self._owner_pid = self._read_owner_pid()
                self._is_already_running = True
                return True

    def release(self):
        if sys.platform == "win32":
            # W2-004: primary and recovery are tracked separately and each is
            # closed exactly once. `_mutex` is an alias for whichever handle
            # this instance created first, so it is never closed twice.
            handles = []
            for handle in (self._primary_mutex, self._recovery_mutex):
                if handle and handle not in handles:
                    handles.append(handle)
            for handle in handles:
                try:
                    import ctypes

                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
            self._mutex = None
            self._primary_mutex = None
            self._recovery_mutex = None
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
