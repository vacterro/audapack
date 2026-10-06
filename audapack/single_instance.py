"""Single instance application guard for AUDAPACK."""

from __future__ import annotations

import atexit
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from audapack.config import get_state_dir


class GuardEstablishmentError(RuntimeError):
    """Raised when the single-instance guard cannot be reliably established.

    W2-007: a failed guard must not fail open (permit a second instance).
    The launcher must surface the error rather than silently starting.
    """


#: How long a launcher waits for a live owner to put its window up before it
#: stops believing the owner is merely slow. A Qt MainWindow reaches WS_VISIBLE
#: in well under a second; this only has to outlast a cold start on a loaded
#: machine, and it is the deadline AFTER WHICH a windowless owner is a brick.
OWNER_WINDOW_GRACE_SECONDS = 6.0

#: How long a non-recovering launcher waits for the launcher that IS recovering
#: to put a window up, before it gives up quietly rather than opening a second.
RECOVERY_PEER_WAIT_SECONDS = 30.0

#: How long to wait for a terminated owner to actually leave the process table.
OWNER_EXIT_WAIT_SECONDS = 10.0

#: How long a verified owner's own UI thread gets to put itself back on screen.
#: The request is posted asynchronously (ShowWindowAsync), so the launcher is
#: never blocked by the owner's message pump; this only bounds how long it will
#: wait to SEE the result. A thread that cannot service the request inside this
#: window is, by definition, the hung owner recovery exists to replace.
RESTORE_VISIBLE_POLL_SECONDS = 2.0
RESTORE_POLL_INTERVAL_SECONDS = 0.05

#: How long to wait for the primary mutex to become acquirable once its owner is
#: gone. The dying process may take a moment to release it.
PRIMARY_ACQUIRE_WAIT_SECONDS = 10.0

#: Version of the owner record on disk. Records written before identity fields
#: existed (pid + started_at only) are still READ, but they are explicitly NOT
#: an identity proof: see verify_owner_identity.
OWNER_RECORD_SCHEMA = 2

#: Windows access rights used by the identity probes.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102

_WIN32_API = None


def _win32_api():
    """Configured kernel32 entry points, or None off Windows.

    ctypes types every return value as c_int unless told otherwise, so a
    64-bit HANDLE is TRUNCATED on the way in. A truncated handle either reads
    back falsy -- which would make a live owner look dead and open a second GUI
    -- or aliases some unrelated object. Every call in this module that yields a
    HANDLE therefore declares its restype, once, here.
    """
    global _WIN32_API
    if _WIN32_API is not None:
        return _WIN32_API
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _WIN32_API = kernel32
    except Exception:
        return None
    return _WIN32_API


def _process_creation_time(pid: int) -> int:
    """Kernel creation time of `pid`, as an integer, or 0 when unknowable.

    This is the PID-reuse proof. Windows hands the same PID to unrelated
    processes all the time, so a PID alone is not an identity -- but two
    different processes never share a (pid, creation_time) pair. Zero means
    "could not prove", and zero is never treated as a match.
    """
    kernel32 = _win32_api()
    if kernel32 is None:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid) & 0xFFFFFFFF)
        if not handle:
            return 0
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return 0
            return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return 0


def _process_image_path(pid: int) -> str:
    """Full image path of `pid`, or "" when it cannot be read."""
    kernel32 = _win32_api()
    if kernel32 is None:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid) & 0xFFFFFFFF)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buff = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buff, ctypes.byref(size)):
                return ""
            return buff.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""

#: Names this PROCESS has already created+owned. A second CreateMutexW for the
#: same name in one process is a recursive acquisition (WAIT_OBJECT_0), not a
#: cross-process race, and must be reported as "already running" instead of
#: being misread as a dead/abandoned holder (which would grant a second writer).
_PROCESS_OWNED_MUTEXES: set[str] = set()
_PROCESS_OWNED_MUTEX_COUNTS: dict[str, int] = {}


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

            STILL_ACTIVE = 259
            kernel32 = _win32_api()
            if kernel32 is None:
                return True
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


def is_hidden_audapack_window_title(title: str) -> bool:
    """Stricter variant for windows that are NOT on screen.

    A hidden window is matched far tighter than a visible one: the title must
    START with a canonical marker. Recovery uses this before terminating, and
    an over-broad match there would mean restoring (or, worse, killing) some
    other process's window simply because its caption contains AUDAPACK.
    """
    stripped = str(title or "").lower().strip()
    return any(stripped.startswith(marker) for marker in AUDAPACK_WINDOW_MARKERS)


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
        #: W2-002: True when this instance OWNS the primary mutex and must
        #: ReleaseMutex it on shutdown (owned-mutex semantics).
        self._owns_primary_mutex = False
        self._process_mutex_registered = False
        #: Whoever holds the guard, when this launcher found it already held.
        self._owner_pid = 0
        #: Ties the on-disk record to the process that actually wrote it, so a
        #: launcher can never delete a LATER owner's record on its way out.
        self._owner_nonce = ""

    # ------------------------------------------------------------------ #
    # owner record: who holds this guard, and is that process still alive
    # ------------------------------------------------------------------ #

    def guard_name(self) -> str:
        """Canonical primary named object for this guard."""
        return f"Local\\{self.name}_MUTEX"

    def recovery_name(self) -> str:
        """Canonical recovery named object: at most one launcher may recover."""
        return f"Local\\{self.name}_RECOVERY_MUTEX"

    def _owner_record_path(self) -> Path:
        return get_state_dir() / f"{self.name.lower()}.owner.json"

    def _write_owner_record(self) -> None:
        """Record WHO owns this guard, atomically. Best effort; the kernel guard
        still rules whether a second instance may start.

        The record exists so a later launcher can prove an identity before it
        does anything irreversible. A bare PID cannot: Windows reuses PIDs, so
        a stale record would otherwise authorize killing a stranger's process.
        """
        try:
            path = self._owner_record_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            nonce = uuid.uuid4().hex
            record = {
                "schema": OWNER_RECORD_SCHEMA,
                "guard_name": self.guard_name(),
                "pid": os.getpid(),
                "process_creation_time": _process_creation_time(os.getpid()),
                "process_image": _process_image_path(os.getpid()),
                "sys_executable": sys.executable,
                "argv0": sys.argv[0] if sys.argv else "",
                "owner_nonce": nonce,
                "started_at": time.time(),
            }
            # Same-directory temp + replace: a concurrent launcher must never
            # read a half-written record and conclude "no identity, so unknown".
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._owner_nonce = nonce
        except OSError:
            pass

    def _read_owner_record(self) -> dict:
        """The owner record as a dict, or {} when absent/corrupt.

        An unreadable record is NOT proof the holder is dead; it is proof the
        holder cannot be IDENTIFIED, which is a different and much weaker thing.
        """
        try:
            data = json.loads(self._owner_record_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_owner_pid(self) -> int:
        try:
            return int(self._read_owner_record().get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    def read_owner_record(self) -> dict:
        """Public read of the owner record; the launcher re-reads it under the
        recovery guard so it verifies what is on disk NOW, not what it saw
        before another launcher had its turn."""
        return self._read_owner_record()

    def verify_owner_identity(self, record: Optional[dict] = None) -> tuple[bool, str]:
        """Is the recorded owner PROVABLY the same process that wrote the record?

        This is the gate in front of TerminateProcess. PID alone is not an
        identity, so the mandatory pair is (pid, kernel creation time); the
        recorded image path is compared too wherever one exists. Anything that
        cannot be proven returns False with the reason -- never a maybe.
        """
        rec = self._read_owner_record() if record is None else record
        if not isinstance(rec, dict) or not rec:
            return False, "no owner record on disk"
        try:
            pid = int(rec.get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            return False, "owner record carries an unreadable pid"
        if pid <= 0:
            return False, "owner record names no process"
        expected_guard = self.guard_name()
        recorded_guard = str(rec.get("guard_name") or "")
        if recorded_guard != expected_guard:
            return False, f"owner record guard {recorded_guard!r} is not {expected_guard!r}"
        try:
            recorded_creation = int(rec.get("process_creation_time") or 0)
        except (TypeError, ValueError, OverflowError):
            return False, "owner record carries an unreadable creation time"
        if not recorded_creation:
            # Schema 1 records (pid + started_at only) predate identity proof.
            # They name a process; they do not prove one.
            return False, "owner record carries no process creation time"
        if pid != os.getpid() and not _process_is_alive(pid):
            # _process_is_alive answers False for our OWN pid on purpose -- "our
            # own pid is not another owner". Here we are asking whether the
            # record names a live process, and we know we are one.
            return False, f"PID {pid} is not running"
        live_creation = _process_creation_time(pid)
        if not live_creation or live_creation != recorded_creation:
            return False, (
                f"PID {pid} creation time {live_creation} does not match "
                f"recorded {recorded_creation} (PID reuse)"
            )
        recorded_image = str(rec.get("process_image") or "")
        if recorded_image:
            live_image = _process_image_path(pid)
            if live_image and os.path.normcase(live_image) != os.path.normcase(recorded_image):
                return False, f"PID {pid} image {live_image!r} is not recorded {recorded_image!r}"
        return True, f"PID {pid} creation time {live_creation} verified"

    def _delete_owner_record_if_ours(self) -> None:
        """Remove the record only when it is still OURS.

        A launcher that was itself recovered from has its record replaced by the
        launcher that took over. Deleting on the way out would strip the new
        owner's identity proof, and the NEXT recovery would have nothing to
        verify -- which is how a recoverable brick turns into an unkillable one.
        """
        if not self._owner_nonce:
            return
        rec = self._read_owner_record()
        if str(rec.get("owner_nonce") or "") != self._owner_nonce:
            return
        if int(rec.get("pid") or 0) != os.getpid():
            return
        try:
            self._owner_record_path().unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def owner_pid(self) -> int:
        """PID of whoever holds this guard, or 0 when it cannot be named.

        Public because the launcher must NAME the holder to the operator. A
        stand-down the operator cannot attribute is not actionable, and the
        actionable step (ending a hung holder) is theirs alone to take.
        """
        return self._owner_pid or self._read_owner_pid()

    def owner_is_alive(self) -> bool:
        """True when the process holding this guard is demonstrably running.

        W2-005: this is the question `is_already_running` used to answer with
        "can I see a window", which a launcher racing a normal startup gets
        wrong. An unreadable/absent record answers False -- that is the pre-
        record behaviour, and the Win32 window check still gates takeover.
        """
        pid = self._owner_pid or self._read_owner_pid()
        return _process_is_alive(pid)

    def wait_for_owner_window(self, timeout: float = OWNER_WINDOW_GRACE_SECONDS,
                              pid: Optional[int] = None) -> Optional[int]:
        """Give a live owner that is still starting time to show its window.

        `pid` scopes the wait to the recorded holder, for the same reason
        activation does: another AUDAPACK-looking window is not this guard's
        owner, and waiting on it would consume the grace the real owner needs.
        """
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            hwnd = self._find_window_hwnd(pid=pid or None)
            if hwnd is not None:
                return hwnd
            if time.time() >= deadline:
                return None
            time.sleep(0.15)

    def _find_window_hwnd(self, title_prefix: str = "AUDAPACK", *, include_hidden: bool = False,
                          pid: Optional[int] = None) -> Optional[int]:
        """Return the first top-level HWND whose title identifies it as an
        AUDAPACK application window, or None if no such window exists.

        Identification is intentionally TIGHT: the bare "AUDAPACK" substring is
        not enough because editors/IDEs that happen to be open on the AUDAPACK
        project (e.g. OpenCode/VS Code) also show "AUDAPACK" in their title bar.
        We require either the distinctive em-dash marker that the MainWindow
        uses ("AUDAPACK \\u2014 Project Room") or the Settings dialog marker
        ("AUDAPACK Settings"), or a caller-supplied prefix. This avoids
        false-positive "already running" decisions caused by the IDE window.

        `include_hidden` and `pid` exist for one caller: recovery. A healthy
        AUDAPACK that is merely minimized or hidden behind the .vbs launcher's
        SW_HIDE startup state must be RESTORED, not killed -- but only when the
        window belongs to the very process whose identity was just verified.
        A hidden window with no owner PID filter would match any process on the
        box that ever opened AUDAPACK, which is exactly the kind of
        looks-right-acts-wrong match this function exists to prevent.

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
            GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
            captured = {"hwnd": None}

            # The em-dash "—" is the MainWindow's distinctive marker. Bare
            # "AUDAPACK" matches too many unrelated windows (the IDE itself,
            # file explorer breadcrumbs, Explorer windows showing the project
            # folder) so a title is never identified by the bare substring.

            def _matches(title_lower: str) -> bool:
                # No generic prefix fallback: `_AUDAPACK` Explorer / IDE titles
                # must not be mistaken for the application window. The caller
                # prefix parameter is retained for signature compatibility only.
                if include_hidden:
                    return is_hidden_audapack_window_title(title_lower)
                return is_audapack_window_title(title_lower)

            def foreach_window(hwnd, lParam):
                if include_hidden or IsWindowVisible(hwnd):
                    if pid is not None:
                        owner = wintypes.DWORD(0)
                        GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                        if int(owner.value) != int(pid):
                            return True
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

    def wait_for_recovered_window(self, timeout: float = RECOVERY_PEER_WAIT_SECONDS) -> Optional[int]:
        """Wait for the launcher that IS recovering to put a window up.

        A second impatient double-click does not start its own GUI and does not
        kill anything either: it waits for the recovery to land and then simply
        foregrounds the result. Exactly one AUDAPACK is ever produced."""
        return self.wait_for_owner_window(timeout=timeout)

    def is_already_running(self) -> bool:
        if sys.platform == "win32":
            try:
                kernel32 = _win32_api()
                if kernel32 is None:
                    raise GuardEstablishmentError("kernel32 is not reachable on this host")
                mutex_name = self.guard_name()
                # W2-002: request INITIAL OWNERSHIP so the kernel mutex itself is
                # the liveness authority, not a best-effort owner.json written
                # after the mutex becomes observable. A creator owns it at once;
                # an existing launcher probes with WaitForSingleObject. A live
                # holder (WAIT_TIMEOUT) makes this launcher stand down even when
                # owner.json is absent and no window exists yet.
                self._mutex = kernel32.CreateMutexW(None, True, mutex_name)
                if not self._mutex:
                    raise GuardEstablishmentError(
                        f"CreateMutexW for '{mutex_name}' returned a null handle"
                    )
                last_error = kernel32.GetLastError()
                if last_error != ERROR_ALREADY_EXISTS:
                    # We created and therefore own the primary guard.
                    self._primary_mutex = self._mutex
                    self._owns_primary_mutex = True
                    # Win32 mutexes are recursive for a caller that already
                    # owns them: a second CreateMutexW in this process returns
                    # WAIT_OBJECT_0 instead of WAIT_TIMEOUT. Keep a process
                    # local ownership marker so a same-process second guard is
                    # still rejected as an already-running instance.
                    _PROCESS_OWNED_MUTEXES.add(mutex_name)
                    _PROCESS_OWNED_MUTEX_COUNTS[mutex_name] = (
                        _PROCESS_OWNED_MUTEX_COUNTS.get(mutex_name, 0) + 1
                    )
                    self._process_mutex_registered = True
                    atexit.register(self.release)
                    self._write_owner_record()
                    return False

                if mutex_name in _PROCESS_OWNED_MUTEXES:
                    # A same-process CreateMutexW is recursive and therefore
                    # returns WAIT_OBJECT_0. Preserve the historical recovery
                    # contract: a visible owner is already running, while a
                    # windowless holder is treated as a recoverable zombie.
                    self._owner_pid = os.getpid()
                    owner_pid = self._read_owner_pid() or os.getpid()
                    if (self._find_window_hwnd() is not None or
                            _process_is_alive(owner_pid)):
                        try:
                            kernel32.CloseHandle(self._mutex)
                        except Exception:
                            pass
                        self._mutex = None
                        self._is_already_running = True
                        return True

                wait = kernel32.WaitForSingleObject(self._mutex, 0)
                if wait == WAIT_TIMEOUT:
                    # A live holder owns the mutex. Stand down: missing owner.json
                    # and an absent window are NOT proof the holder is dead.
                    self._owner_pid = self._read_owner_pid()
                    self._is_already_running = True
                    try:
                        kernel32.CloseHandle(self._mutex)
                    except Exception:
                        pass
                    self._mutex = None
                    return True

                # WAIT_OBJECT_0 (unowned existing object) or WAIT_ABANDONED (the
                # previous owner died holding it): this launcher now owns the
                # EXISTING primary object, so namespace continuity is automatic.
                if wait not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                    raise GuardEstablishmentError(
                        f"Unexpected WaitForSingleObject result {wait} on '{mutex_name}'"
                    )
                self._primary_mutex = self._mutex
                self._owns_primary_mutex = True
                _PROCESS_OWNED_MUTEXES.add(mutex_name)
                _PROCESS_OWNED_MUTEX_COUNTS[mutex_name] = (
                    _PROCESS_OWNED_MUTEX_COUNTS.get(mutex_name, 0) + 1
                )
                self._process_mutex_registered = True
                # Preserve the second-recovery-launcher exclusion (W2-004).
                recovery = self.acquire_recovery_guard()
                if not recovery[0]:
                    kernel32.ReleaseMutex(self._mutex)
                    self._process_mutex_registered = False
                    count = _PROCESS_OWNED_MUTEX_COUNTS.get(mutex_name, 0) - 1
                    if count > 0:
                        _PROCESS_OWNED_MUTEX_COUNTS[mutex_name] = count
                    else:
                        _PROCESS_OWNED_MUTEX_COUNTS.pop(mutex_name, None)
                        _PROCESS_OWNED_MUTEXES.discard(mutex_name)
                    raise GuardEstablishmentError(
                        "Another launcher is already recovering a windowless instance"
                    )
                atexit.register(self.release)
                self._write_owner_record()
                self._is_already_running = False
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

    # ------------------------------------------------------------------ #
    # windowless-owner recovery: one launcher, verified identity, same guard
    # ------------------------------------------------------------------ #

    def acquire_recovery_guard(self) -> tuple[bool, str]:
        """Take the one canonical recovery lock for this guard.

        Two impatient double-clicks must not both conclude "kill it and
        restart": that is how one broken owner becomes two racing recoveries
        and a pile of orphans. Exactly one launcher may recover; the other
        waits for the result and foregrounds whatever appears.
        """
        if self._recovery_mutex is not None:
            return True, "recovery guard already held by this launcher"
        kernel32 = _win32_api()
        if kernel32 is None:
            return False, "windowless-owner recovery needs the Win32 kernel API"
        try:
            name = self.recovery_name()
            handle = kernel32.CreateMutexW(None, True, name)
            if not handle:
                return False, f"CreateMutexW for recovery guard '{name}' returned a null handle"
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False, f"another launcher already holds '{name}'"
            self._recovery_mutex = handle
            return True, f"recovery guard '{name}' acquired"
        except Exception as exc:
            return False, f"recovery guard could not be acquired: {exc}"

    def release_recovery_guard(self) -> None:
        if self._recovery_mutex is None:
            return
        kernel32 = _win32_api()
        try:
            if kernel32 is not None:
                kernel32.ReleaseMutex(self._recovery_mutex)
                kernel32.CloseHandle(self._recovery_mutex)
        except Exception:
            pass
        self._recovery_mutex = None

    def wait_for_process_exit(self, pid: int, timeout: float = OWNER_EXIT_WAIT_SECONDS) -> bool:
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            if not _process_is_alive(pid):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def _restore_window_hwnd(self, hwnd: int,
                             poll_seconds: float = RESTORE_VISIBLE_POLL_SECONDS) -> bool:
        """Put an exact hidden HWND back on screen. True only if it GOT there.

        Three traps, all paid for here:

        * Show-state is not a message you hand-roll. The previous version sent
          `WM_SHOWWINDOW` with `SW_SHOWNORMAL` in both wParam and lParam: for
          that message wParam is a BOOL and lParam is the *reason*, so
          lParam=1 is SW_PARENTCLOSING, not a show state. It also cannot
          un-minimize an owner: a minimized-but-alive AUDAPACK stayed
          iconified forever and was then treated as restored.
          `ShowWindowAsync(hwnd, SW_RESTORE|SW_SHOW)` is the API for exactly
          this: it sets the show state and posts it to the OWNING thread.
        * Nothing synchronous touches the owner's thread. A hung owner is
          precisely the case T-25 exists for, so the launcher must never wait
          on that thread's message pump. ShowWindowAsync returns as soon as the
          request is posted; the launcher then polls `IsWindowVisible` for a
          bounded window and gives up if nothing appears. That is what keeps a
          hung window from becoming a hung launcher.
        * `SetForegroundWindow` returns FALSE for any process that does not
          currently hold the foreground, which is the recovering launcher most
          of the time. Treating its result as "restored" made the whole restore
          branch unreachable. It is best-effort; success means VISIBLE, so a
          refusal to take focus costs us nothing and a hidden window is never a
          brick.

        `ShowWindowAsync`'s own return value is NOT the answer: it only says the
        request was initiated (and, for the first call, whether the window was
        previously hidden). Visibility is polled, so acceptance is observed.
        """
        if not hwnd or sys.platform != "win32":
            return False
        try:
            import ctypes

            SW_SHOW = 5
            SW_RESTORE = 9
            user32 = ctypes.windll.user32
            try:
                # SW_RESTORE un-minimizes but also un-maximizes, so a maximized
                # owner would shrink on every second launch. Restore only what is
                # actually iconified; everything else just needs showing.
                show_cmd = SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW
                user32.ShowWindowAsync(hwnd, show_cmd)
            except Exception:
                return False
            deadline = time.time() + max(0.0, float(poll_seconds))
            while True:
                if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                    # Focus is a courtesy, never the criterion: a launcher that
                    # cannot take the foreground still restored the window.
                    try:
                        user32.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
                    return True
                if time.time() >= deadline:
                    return False
                time.sleep(RESTORE_POLL_INTERVAL_SECONDS)
        except Exception:
            return False

    def terminate_verified_owner(self, record: Optional[dict] = None,
                                  timeout: float = OWNER_EXIT_WAIT_SECONDS) -> tuple[str, str]:
        """Deal with a windowless owner: (state, detail).

        States, and what the launcher does with each:

        * ``gone``       -- it already exited. The abandoned mutex is simply
          taken over; nothing is terminated (T-25: a natural death must not
          cost a needless TerminateProcess against a reused PID).
        * ``restored``   -- it is healthy and only HIDDEN: its own MainWindow
          belongs to the verified PID and was put back on screen. Nothing dies.
        * ``terminated`` -- proven identity, still windowless after the grace,
          so it was ended and the primary guard is free.
        * ``refused``    -- identity not proven. Nothing is killed, ever; the
          launcher reports a real recovery failure instead.
        * ``failed``     -- verified, but Windows would not end it.
        """
        rec = self._read_owner_record() if record is None else record
        if not isinstance(rec, dict) or not rec:
            return "refused", "no owner record: the holder cannot be identified"
        try:
            pid = int(rec.get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            return "refused", "owner record carries an unreadable pid"
        if pid <= 0:
            return "refused", "owner record names no process"
        if not _process_is_alive(pid):
            return "gone", f"PID {pid} exited on its own; nothing to terminate"

        verified, why = self.verify_owner_identity(rec)
        if not verified:
            # "not running" was already ruled out above, so this is a genuine
            # identity failure -- PID reuse, wrong guard, or a schema-1 record.
            return "refused", why

        # Healthy-but-hidden is the cheapest correct answer, and it must be
        # tried BEFORE any termination: a window that can be restored is not a
        # brick. The PID filter means this can only ever touch the process just
        # proven to be the owner.
        hwnd = self._find_window_hwnd(include_hidden=True, pid=pid)
        if hwnd and self._restore_window_hwnd(hwnd):
            return "restored", f"PID {pid} owned a hidden AUDAPACK window (HWND {hwnd}); restored"

        kernel32 = _win32_api()
        if kernel32 is None:
            return "failed", "the Win32 kernel API is unreachable; no termination attempted"
        try:
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid & 0xFFFFFFFF)
        except Exception as exc:
            return "failed", f"OpenProcess(PROCESS_TERMINATE) failed for PID {pid}: {exc}"
        if not handle:
            return "failed", f"OpenProcess(PROCESS_TERMINATE) was denied for PID {pid}"
        try:
            if not kernel32.TerminateProcess(handle, 1):
                return "failed", f"TerminateProcess was refused for PID {pid}"
        finally:
            kernel32.CloseHandle(handle)
        if not self.wait_for_process_exit(pid, timeout):
            return "failed", f"PID {pid} survived TerminateProcess for {timeout:.0f}s"
        return "terminated", f"verified windowless owner PID {pid} terminated"

    def acquire_primary_after_recovery(self,
                                      timeout: float = PRIMARY_ACQUIRE_WAIT_SECONDS) -> tuple[bool, str]:
        """Take ownership of the SAME primary named object, not a new namespace.

        Recovery must not sidestep the guard: a fresh mutex under a fresh name
        would be a second independent guard, and the invariant the audit bought
        (one AUDAPACK per session) would silently evaporate. So this re-opens the
        canonical name and waits for it to become acquirable -- the dead owner
        may take a moment to let go -- then owns that object and writes its own
        identity record.
        """
        kernel32 = _win32_api()
        if kernel32 is None:
            return False, "the Win32 kernel API is unreachable; primary guard not acquired"
        name = self.guard_name()
        deadline = time.time() + max(0.0, float(timeout))
        handle = None
        while True:
            try:
                handle = kernel32.CreateMutexW(None, True, name)
            except Exception as exc:
                return False, f"CreateMutexW('{name}') failed: {exc}"
            if not handle:
                return False, f"CreateMutexW('{name}') returned a null handle"
            if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
                break
            try:
                wait = kernel32.WaitForSingleObject(handle, 250)
            except Exception as exc:
                kernel32.CloseHandle(handle)
                return False, f"WaitForSingleObject('{name}') failed: {exc}"
            kernel32.CloseHandle(handle)
            handle = None
            if wait in (WAIT_OBJECT_0, WAIT_ABANDONED):
                # The object existed but was free: reopen it to own it.
                continue
            if time.time() >= deadline:
                return False, (
                    f"the primary guard '{name}' was still held {timeout:.0f}s after its "
                    f"owner was verified dead; refusing to run outside the single-instance "
                    f"namespace"
                )
        self._primary_mutex = handle
        self._mutex = handle
        self._owns_primary_mutex = True
        _PROCESS_OWNED_MUTEXES.add(name)
        _PROCESS_OWNED_MUTEX_COUNTS[name] = _PROCESS_OWNED_MUTEX_COUNTS.get(name, 0) + 1
        self._process_mutex_registered = True
        atexit.register(self.release)
        self._write_owner_record()
        return True, f"primary guard '{name}' acquired after recovery"

    def release(self):
        if sys.platform == "win32":
            # W2-002: an owned primary mutex must be ReleaseMutex'd before its
            # handle closes, or the abandoned mutex would block (or require
            # recovery from) the next launcher. W2-004: primary and recovery are
            # tracked separately and each is closed exactly once.
            kernel32 = _win32_api()
            handles = []
            if self._primary_mutex:
                handles.append((self._primary_mutex, self._owns_primary_mutex))
            if self._recovery_mutex:
                handles.append((self._recovery_mutex, True))
            if self._mutex and self._mutex not in (self._primary_mutex, self._recovery_mutex):
                handles.append((self._mutex, False))
            for handle, owned in handles:
                try:
                    if kernel32 is not None and owned:
                        kernel32.ReleaseMutex(handle)
                    if kernel32 is not None:
                        kernel32.CloseHandle(handle)
                except Exception:
                    pass
            self._mutex = None
            self._primary_mutex = None
            self._recovery_mutex = None
            self._owns_primary_mutex = False
            if self._process_mutex_registered:
                mutex_name = self.guard_name()
                count = _PROCESS_OWNED_MUTEX_COUNTS.get(mutex_name, 0) - 1
                if count > 0:
                    _PROCESS_OWNED_MUTEX_COUNTS[mutex_name] = count
                else:
                    _PROCESS_OWNED_MUTEX_COUNTS.pop(mutex_name, None)
                    _PROCESS_OWNED_MUTEXES.discard(mutex_name)
                self._process_mutex_registered = False
            self._delete_owner_record_if_ours()
        elif self._file_handle:
            try:
                import fcntl

                fcntl.flock(self._file_handle, fcntl.LOCK_UN)
                self._file_handle.close()
                self._file_handle = None
            except Exception:
                pass
            self._delete_owner_record_if_ours()

    def activate_existing_window(self, title_prefix: str = "AUDAPACK",
                              pid: Optional[int] = None) -> bool:
        """Restores and brings existing AUDAPACK window to foreground on Windows.

        `pid` scopes the search to the process that actually holds THIS guard.
        Without it, any AUDAPACK-looking window on the desktop satisfies the
        check -- so a launcher holding a guard whose owner is a windowless brick
        would "succeed" at activation against somebody else's unrelated window
        and stand down forever, which is the very failure being fixed.

        Returns True if a matching window was found and foregrounded, False otherwise
        (caller can use this to detect a zombie mutex holder and recover)."""
        if sys.platform != "win32":
            return False
        try:
            return self._restore_window_hwnd(
                self._find_window_hwnd(title_prefix, pid=pid or None)
            )
        except Exception:
            return False
