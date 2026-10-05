"""AUDAPACK-owned console title for the full managed instance lifetime.

The launcher's one-shot ``[Console]::Title`` is best-effort at startup: any
later TUI/OS action (Ctrl, Ctrl+C, Ctrl+V, an OpenCode redraw) can overwrite
it, and a console labeled "Administrator: Windows PowerShell" carries zero
project identity. This module keeps the association between a managed launch
and its console window and restores the canonical title for as long as the
launch process itself lives.

Invariants (T-209, escaped regression of T-205/T-198, T-210):

- Ownership is keyed on process identity -- spawned PID, process creation
  token, launch correlation token, guardian generation -- never on the
  window's current title. A window whose title already drifted is still
  attributable and repairable (TARGET C).
- The main AUDAPACK process NEVER mutates its own process-global console state.
  The pre-T-210 resolver attached AUDAPACK to the target console and freed it
  again (``kernel32!AttachConsole`` -> ``GetConsoleWindow`` -> ``FreeConsole``)
  to read the console HWND. Those calls act on the CALLING PROCESS, not on a
  thread: ``AttachConsole`` repopulated AUDAPACK's standard handles with the
  target console's, and ``FreeConsole`` left those values stale, so the very
  next ``subprocess.run(..., capture_output=True)`` raised
  ``[WinError 6] The handle is invalid`` at process creation -- permanently, so
  every Project Room -> OpenCode -> Fleet preflight failed afterwards. No code
  path here may call ``AttachConsole``/``FreeConsole``; discovery is a
  read-only ``EnumWindows`` scan (T-210 TARGET A).
- Discovery is therefore two read-only scans, both already mechanical: the
  launch PID's own visible top-level windows (Windows attributes a
  ``CREATE_NEW_CONSOLE`` ``ConsoleWindowClass`` window to the launch PID, proven
  live), and AUDAPACK's own unguessable launch correlation token in a caption.
  The authoritative association still comes from InstanceMonitor through
  :meth:`TitleGuardian.bind_hwnd`; the scans only ADOPT, they never re-identify
  a window from generic title text (T-210 TARGET B/C).
- Restoration is primarily event-driven (WinEvent ``EVENT_OBJECT_NAMECHANGE``,
  ``WINEVENT_OUTOFCONTEXT``), but an event is an optimization. A low-frequency
  integrity heartbeat runs while at least one managed instance exists, so a
  missed event, a window created after registration, a rehosted HWND or an
  event whose owner cannot be mapped still repairs within one heartbeat
  (TARGET A/B). The heartbeat stops completely when the last instance exits.
- An event for an unknown HWND is not discarded: it triggers a bounded
  slow-path re-resolve over the active registrations only (never a system-wide
  scan). Once learned, the HWND is handled in O(1) afterwards.
- Every bound HWND carries the launch PID, process creation token, guardian
  generation, correlation token and (where known) the native window PID, so a
  stale callback can never rename an unrelated window that later reuses the
  same HWND or PID (TARGET E).
- A generic title is only drift. Once ``LaunchRecord <-> native HWND`` has been
  mechanically established the ownership is in memory until the launch process
  dies, its creation token changes, the HWND becomes invalid, or the launch is
  explicitly unregistered (TARGET L).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, replace
from typing import Callable, Protocol

#: Title-integrity heartbeat. Runs ONLY while at least one managed titled
#: instance exists; a missed WinEvent repairs within this bound.
HEARTBEAT_INTERVAL_MS = 1000

#: Legacy bounded fallback, kept for the case where the WinEvent hook cannot
#: be installed at all. It is never the primary mechanism anymore.
FALLBACK_INTERVAL_MS = 5000

#: Bounded number of windows resolved per managed process.
MAX_HWNDS_PER_PID = 8


@dataclass(frozen=True)
class TitleBinding:
    """Proven ownership of one HWND by one managed launch.

    This is the reuse-safety record: restoring a title is only allowed while
    the launch PID is alive, its creation token still matches the registered
    token, the binding's generation is the registration's current generation,
    and the HWND is still a real window.
    """

    hwnd: int
    launch_pid: int
    generation: int = 0
    correlation_token: str = ""
    #: PID that actually owns the window, which is not always the durable launch
    #: PID (a console window can be hosted by another process). Recorded from the
    #: monitor's proven association, never assumed (TARGET D).
    native_pid: int = 0


@dataclass(frozen=True)
class TitleRegistration:
    """One managed instance whose console title AUDAPACK owns."""

    pid: int
    canonical_title: str
    correlation_token: str = ""
    launcher_id: str = ""
    project_id: str = ""
    process_token: int = 0
    #: Monotonic per-guardian generation; a retired generation can never be
    #: revived by a late event (TARGET E).
    generation: int = 0
    #: Resolved console windows at registration/refresh time, for readability.
    hwnds: tuple[int, ...] = ()


class TitleBackend(Protocol):
    """Platform boundary; every method is side-effect-faithful."""

    def resolve_hwnds(self, pid: int) -> list[int]: ...

    def resolve_hwnds_by_token(self, token: str) -> list[int]: ...

    def window_pid(self, hwnd: int) -> int: ...

    def is_window(self, hwnd: int) -> bool: ...

    def get_title(self, hwnd: int) -> str: ...

    def set_title(self, hwnd: int, title: str) -> bool: ...

    def process_alive(self, pid: int) -> bool: ...

    def process_token(self, pid: int) -> int: ...

    def start(self, on_name_change: Callable[[int], None]) -> bool: ...

    def stop(self) -> None: ...


class NullTitleBackend:
    """Non-Windows: truthful no-op, never a fake restore."""

    def resolve_hwnds(self, pid: int) -> list[int]:
        return []

    def resolve_hwnds_by_token(self, token: str) -> list[int]:
        return []

    def window_pid(self, hwnd: int) -> int:
        return 0

    def is_window(self, hwnd: int) -> bool:
        return False

    def get_title(self, hwnd: int) -> str:
        return ""

    def set_title(self, hwnd: int, title: str) -> bool:
        return False

    def process_alive(self, pid: int) -> bool:
        return False

    def process_token(self, pid: int) -> int:
        return 0

    def start(self, on_name_change: Callable[[int], None]) -> bool:
        return False

    def stop(self) -> None:
        return None


class Win32TitleBackend:
    """SetWinEventHook name-change watcher plus window/title primitives.

    Every native entry point is resolved ONCE, with an explicit restype and
    argtypes, instead of relying on ctypes' implicit int marshalling. There is
    deliberately no ``AttachConsole``/``FreeConsole``/``GetConsoleWindow``
    binding here: those APIs mutate the CALLING process' console association
    and standard handles, and doing so inside the long-lived GUI process is
    exactly the T-210 regression. Window discovery is a read-only
    ``EnumWindows`` scan.
    """

    #: Not a setting: a truthful declaration of the boundary this module must
    #: keep. The red control in tests/native_title_red_control_worker.py proves
    #: what breaks when it is violated (T-210).
    PROCESS_SAFE_WINDOW_DISCOVERY = True

    EVENT_OBJECT_NAMECHANGE = 0x800C
    WINEVENT_OUTOFCONTEXT = 0x0000
    WM_QUIT = 0x0012
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    def __init__(self) -> None:
        self._hook: int | None = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._callback_ref = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.hook_installed = False
        self._configure_native()

    # -- native binding -----------------------------------------------------

    def _configure_native(self) -> None:
        """Declare every signature we call; no API is looked up implicitly."""
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel = self._kernel32
        user = self._user32

        # NOTE (T-210): AttachConsole / FreeConsole / GetConsoleWindow are NOT
        # bound here on purpose. They act on the calling PROCESS (console
        # association + standard handles), so using them for discovery corrupted
        # AUDAPACK's inherited handles and broke every later capture_output
        # spawn with [WinError 6]. Discovery below is EnumWindows-only.
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        kernel.GetCurrentThreadId.argtypes = []
        # PostThreadMessageW is a USER32 export (the previous kernel32 lookup
        # raised AttributeError inside start(), which silently disabled the
        # whole event hook).
        user.PostThreadMessageW.restype = wintypes.BOOL
        user.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        ]

        user.SetWindowTextW.restype = wintypes.BOOL
        user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user.GetWindowTextW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user.IsWindow.restype = wintypes.BOOL
        user.IsWindow.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetClassNameW.restype = ctypes.c_int
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.EnumWindows.restype = wintypes.BOOL
        user.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.SetWinEventHook.restype = ctypes.c_void_p
        user.UnhookWinEvent.restype = wintypes.BOOL
        user.UnhookWinEvent.argtypes = [ctypes.c_void_p]
        user.GetMessageW.restype = ctypes.c_int
        user.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.TranslateMessage.restype = wintypes.BOOL
        user.TranslateMessage.argtypes = [ctypes.c_void_p]
        user.DispatchMessageW.restype = wintypes.BOOL
        user.DispatchMessageW.argtypes = [ctypes.c_void_p]

    # -- title/handle primitives -------------------------------------------

    def _process_handle(self, pid: int):
        return self._kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )

    def process_alive(self, pid: int) -> bool:
        handle = self._process_handle(int(pid or 0))
        if not handle:
            return False
        try:
            exit_code = self._wintypes.DWORD()
            return bool(
                self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(exit_code))
            ) and exit_code.value == self.STILL_ACTIVE
        finally:
            self._kernel32.CloseHandle(handle)

    def process_token(self, pid: int) -> int:
        handle = self._process_handle(int(pid or 0))
        if not handle:
            return 0
        try:
            wintypes = self._wintypes
            ctypes = self._ctypes
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not self._kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                return 0
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            self._kernel32.CloseHandle(handle)

    def window_pid(self, hwnd: int) -> int:
        pid = self._wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(
            self._wintypes.HWND(int(hwnd)), self._ctypes.byref(pid)
        )
        return int(pid.value)

    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(self._wintypes.HWND(int(hwnd))))

    def get_title(self, hwnd: int) -> str:
        handle = self._wintypes.HWND(int(hwnd))
        length = int(self._user32.GetWindowTextLengthW(handle))
        if length <= 0:
            return ""
        buffer = self._ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def set_title(self, hwnd: int, title: str) -> bool:
        return bool(self._user32.SetWindowTextW(self._wintypes.HWND(int(hwnd)), str(title)))

    # -- read-only window discovery (T-210 TARGET A) ------------------------

    def _collect_visible_windows(self, match) -> list[int]:
        """Visible top-level windows ``match`` accepts.

        Read-only by construction: ``EnumWindows``/``GetWindowTextW``/
        ``GetWindowThreadProcessId`` never touch this process' console
        association or standard handles. That is the property the whole
        module depends on (T-210): a title-integrity feature must never be able
        to break an unrelated subprocess spawn.
        """
        user32 = self._user32
        ctypes = self._ctypes
        wintypes = self._wintypes
        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd, _lparam):
            try:
                if user32.IsWindowVisible(hwnd) and match(hwnd):
                    found.append(int(hwnd))
            except Exception:
                return True
            return True

        user32.EnumWindows(ctypes.cast(callback_type(collect), ctypes.c_void_p), 0)
        return found

    def _top_level_hwnds(self, pid: int) -> list[int]:
        """Visible top-level windows owned by ``pid``.

        Windows attributes a ``CREATE_NEW_CONSOLE`` ``ConsoleWindowClass``
        window to the launch PID that owns the console, so the console window is
        found by PID without attaching to anything (verified live on this
        platform in tests/test_title_guardian_native.py). This is a plain scan,
        not a guess: the owner PID is the identity.
        """
        user32 = self._user32
        ctypes = self._ctypes
        wintypes = self._wintypes

        def owned(hwnd) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            return int(owner.value) == int(pid)

        return self._collect_visible_windows(owned)

    def resolve_hwnds(self, pid: int) -> list[int]:
        """PID-scoped discovery. Never attaches to a console (T-210)."""
        pid = int(pid or 0)
        if pid <= 0:
            return []
        return self._top_level_hwnds(pid)

    def resolve_hwnds_by_token(self, token: str) -> list[int]:
        """Visible top-level windows whose caption carries AUDAPACK's token.

        The launch correlation token is a durable, unguessable per-launch value
        AUDAPACK writes into the managed console title and stores on the
        LaunchRecord (T-192). Matching it is mechanical identity -- exactly the
        same proof InstanceMonitor uses -- never a guess from generic title
        words. It exists to ADOPT a window this process cannot see by PID (a
        console window owned by another host process); once adopted, ownership
        lives in the in-memory binding and a drifted title is only drift.
        """
        needle = str(token or "").strip()
        if not needle:
            return []
        user32 = self._user32
        ctypes = self._ctypes

        def carries_token(hwnd) -> bool:
            length = int(user32.GetWindowTextLengthW(hwnd))
            if length <= 0:
                return False
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            return needle in str(buffer.value or "")

        return self._collect_visible_windows(carries_token)

    def window_class(self, hwnd: int) -> str:
        """Window class name, for truthful diagnostics only."""
        buffer = self._ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(
            self._wintypes.HWND(int(hwnd)), buffer, 256
        )
        return str(buffer.value or "")

    # -- event hook ---------------------------------------------------------

    def start(self, on_name_change: Callable[[int], None]) -> bool:
        if self._thread is not None:
            return bool(self.hook_installed)
        ctypes = self._ctypes
        wintypes = self._wintypes
        user32 = self._user32
        kernel32 = self._kernel32
        callback_type = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
        )

        def _dispatch(_hook, _event, hwnd, _id_object, _id_child, _thread, _time):
            try:
                on_name_change(int(hwnd or 0))
            except Exception:
                pass

        self._callback_ref = callback_type(_dispatch)
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, callback_type,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ]
        user32.SetWinEventHook.restype = ctypes.c_void_p

        def _run():
            self._thread_id = int(kernel32.GetCurrentThreadId())
            hook = user32.SetWinEventHook(
                self.EVENT_OBJECT_NAMECHANGE, self.EVENT_OBJECT_NAMECHANGE,
                0, self._callback_ref, 0, 0, self.WINEVENT_OUTOFCONTEXT,
            )
            self._hook = hook
            self.hook_installed = bool(hook)
            self._ready.set()
            if not hook:
                return
            msg = wintypes.MSG()
            while not self._stop.is_set() and user32.GetMessageW(
                ctypes.byref(msg), 0, 0, 0
            ) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                user32.UnhookWinEvent(hook)
            except Exception:
                pass
            self._hook = None
            self.hook_installed = False

        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=_run, name="audapack-title-hook", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return bool(self.hook_installed)

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0
        self._hook = None
        self.hook_installed = False


def create_title_backend() -> TitleBackend:
    return Win32TitleBackend() if sys.platform == "win32" else NullTitleBackend()


class TitleGuardian:
    """Owns canonical console titles for managed instances; restores drift.

    Thread-safety: the WinEvent callback arrives on the hook thread while the
    GUI thread registers/unregisters and runs the heartbeat, so every registry
    mutation and restore is serialized by one lock.
    """

    def __init__(self, backend: TitleBackend | None = None) -> None:
        self.backend = backend if backend is not None else create_title_backend()
        self._lock = threading.RLock()
        self._registrations: dict[int, TitleRegistration] = {}
        self._hwnds: dict[int, list[int]] = {}
        self._bindings: dict[int, TitleBinding] = {}
        self._generation = 0
        self._started = False
        self._event_driven = False
        self.restores = 0
        self.last_error = ""
        #: T-216 TARGET B/C: set when a bound window is missing or an event
        #: arrives for an HWND nobody has proven. It is a REQUEST for the
        #: InstanceMonitor authority, never permission to scan the desktop from
        #: the heartbeat or from the WinEvent callback.
        self._authority_needed = False
        #: Bounded record of HWNDs observed on unmapped name-change events, so
        #: the diagnostic is truthful without growing without limit.
        self._observed_hwnds: list[int] = []

        def _on_name_change(hwnd: int) -> None:
            try:
                self._handle_name_change(hwnd)
            except Exception as exc:  # never let a callback kill the hook thread
                self.last_error = f"title restore failed: {exc}"

        self._callback = _on_name_change

    # -- status -------------------------------------------------------------

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._registrations)

    @property
    def event_driven(self) -> bool:
        return self._event_driven

    @property
    def uses_fallback_polling(self) -> bool:
        """True only after an event-hook start attempt failed while owning work."""
        return self._started and not self._event_driven

    @property
    def fallback_interval_ms(self) -> int:
        return 0 if self._event_driven else FALLBACK_INTERVAL_MS

    @property
    def heartbeat_interval_ms(self) -> int:
        """Heartbeat period while managed instances exist; 0 when none do."""
        with self._lock:
            return HEARTBEAT_INTERVAL_MS if self._registrations else 0

    def registration(self, pid: int) -> TitleRegistration | None:
        with self._lock:
            return self._registrations.get(int(pid or 0))

    def binding(self, hwnd: int) -> TitleBinding | None:
        with self._lock:
            return self._bindings.get(int(hwnd or 0))

    def bound_hwnds(self, pid: int) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._hwnds.get(int(pid or 0)) or ())

    @property
    def binding_count(self) -> int:
        with self._lock:
            return len(self._bindings)

    def hwnd_for_pid(self, pid: int) -> int:
        with self._lock:
            hwnds = self._hwnds.get(int(pid or 0)) or []
            return hwnds[0] if hwnds else 0

    # -- lifecycle ----------------------------------------------------------

    def register(
        self,
        pid: int,
        canonical_title: str,
        *,
        correlation_token: str = "",
        launcher_id: str = "",
        project_id: str = "",
    ) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        title = str(canonical_title or "").strip()
        if not title:
            return False
        with self._lock:
            self._ensure_started()
            self._generation += 1
            registration = TitleRegistration(
                pid=pid,
                canonical_title=title,
                correlation_token=str(correlation_token or ""),
                launcher_id=str(launcher_id or ""),
                project_id=str(project_id or ""),
                process_token=self._process_token(pid),
                generation=self._generation,
                hwnds=(),
            )
            self._registrations[pid] = registration
            self._rebind_hwnds(pid, self._resolve_for(registration))
            current = self._registrations[pid]
            self._registrations[pid] = replace(current, hwnds=tuple(self._hwnds.get(pid) or ()))
            self._apply(self._registrations[pid])
            return True

    def bind_hwnd(
        self,
        launch_pid: int,
        hwnd: int,
        *,
        correlation_token: str = "",
        native_pid: int = 0,
        canonical_title: str = "",
    ) -> bool:
        """Adopt an HWND already proven by the InstanceMonitor correlation.

        The window that hosts a managed launch is not always owned by the
        durable launch PID. InstanceMonitor has already proven
        ``launch_pid + hwnd + correlation token`` by reading real top-level
        windows; feeding that proven association here means the guardian does
        not have to rediscover the window, and never has to identify the
        instance from the (already corrupted) title.
        """
        launch_pid = int(launch_pid or 0)
        hwnd = int(hwnd or 0)
        if launch_pid <= 0 or hwnd <= 0:
            return False
        with self._lock:
            registration = self._registrations.get(launch_pid)
            if registration is None:
                # A registration is what carries the canonical title; without
                # one there is nothing to restore and nothing to bind.
                return False
            token = str(correlation_token or "")
            if registration.correlation_token and token and registration.correlation_token != token:
                return False
            if not self._process_identity_ok(registration):
                return False
            bound = self._bindings.get(hwnd)
            if (
                bound is not None
                and bound.launch_pid != launch_pid
                and bound.launch_pid in self._registrations
            ):
                return False
            hwnds = list(self._hwnds.get(launch_pid) or [])
            if hwnd not in hwnds:
                hwnds.append(hwnd)
            self._rebind_hwnds(launch_pid, hwnds, native_pids={hwnd: int(native_pid or 0)})
            self._apply(self._registrations[launch_pid])
            return True

    def unregister(self, pid: int) -> bool:
        with self._lock:
            pid = int(pid or 0)
            removed = self._registrations.pop(pid, None)
            self._hwnds.pop(pid, None)
            for hwnd, binding in list(self._bindings.items()):
                if binding.launch_pid == pid:
                    del self._bindings[hwnd]
            if removed is not None and not self._registrations:
                self._stop_backend()
            return removed is not None

    def shutdown(self) -> None:
        with self._lock:
            self._registrations.clear()
            self._hwnds.clear()
            self._bindings.clear()
            self._stop_backend()

    # -- refresh/restore ----------------------------------------------------

    def retire_dead(self) -> int:
        """Drop registrations whose instance has exited or been replaced."""
        retired = 0
        with self._lock:
            for pid, registration in list(self._registrations.items()):
                if self._process_identity_ok(registration):
                    continue
                self._registrations.pop(pid, None)
                self._hwnds.pop(pid, None)
                for hwnd, binding in list(self._bindings.items()):
                    if binding.launch_pid == pid:
                        del self._bindings[hwnd]
                retired += 1
            if retired and not self._registrations:
                self._stop_backend()
        return retired

    @property
    def needs_hwnd_authority(self) -> bool:
        """True while a managed launch has no live window binding yet.

        InstanceMonitor is the HWND authority; the GUI asks it for a fresh
        proven correlation only while this is True, so the 1000 ms heartbeat
        stays a cheap read in the steady state instead of a desktop scan per
        tick (T-210 TARGET B/D). T-216: the liveness probe runs OUTSIDE the
        registry lock and is O(bound HWNDs), never a discovery scan.
        """
        with self._lock:
            snapshot = [
                (pid, tuple(self._hwnds.get(pid) or ()))
                for pid in self._registrations
            ]
        for _pid, hwnds in snapshot:
            if not hwnds:
                return True
            if not any(self._window_alive(hwnd) for hwnd in hwnds):
                return True
        return False

    def request_hwnd_authority(self) -> None:
        """Mark that InstanceMonitor must re-prove an HWND (T-216 TARGET C)."""
        with self._lock:
            self._authority_needed = True

    def take_hwnd_authority_request(self) -> bool:
        """Consume the coalesced authority request exactly once."""
        with self._lock:
            requested = self._authority_needed
            self._authority_needed = False
            return requested

    def observed_hwnds(self) -> tuple[int, ...]:
        """Bounded HWNDs seen on unmapped events, diagnostic only."""
        with self._lock:
            return tuple(self._observed_hwnds)

    def heartbeat(self) -> int:
        """Title-integrity heartbeat over ALREADY-PROVEN bindings only.

        T-216 TARGET B: the heartbeat NEVER performs window discovery
        (``_resolve_for``/``resolve_hwnds``/``resolve_hwnds_by_token``/
        ``EnumWindows``). Window discovery belongs to InstanceMonitor;
        TitleGuardian owns HWNDs the monitor already proved. A registration
        with no live binding only marks an authority request and returns, so
        the 1000 ms caller stays a cheap O(bound HWNDs) read instead of 2*N
        desktop scans. TARGET D: native title/liveness calls run OUTSIDE the
        registry lock; only the immutable snapshot and the commit are locked.
        """
        self.retire_dead()
        restored = 0
        with self._lock:
            snapshot = [
                (registration, tuple(self._hwnds.get(registration.pid) or ()))
                for registration in self._registrations.values()
            ]
        for registration, bound in snapshot:
            if not bound:
                self.request_hwnd_authority()
                continue
            current = self._current_registration(registration)
            if current is None:
                continue
            for hwnd in bound:
                restored += self._heartbeat_hwnd(current, hwnd)
        return restored

    def _heartbeat_hwnd(self, registration: TitleRegistration, hwnd: int) -> int:
        if not self._window_alive(hwnd):
            with self._lock:
                current = self._current_registration(registration)
                if current is None:
                    return 0
                remaining = [
                    h for h in (self._hwnds.get(registration.pid) or ()) if h != hwnd
                ]
                self._rebind_hwnds(registration.pid, remaining)
                if not self._hwnds.get(registration.pid):
                    self._authority_needed = True
            return 0
        if not self._process_identity_ok(registration):
            return 0
        with self._lock:
            current = self._current_registration(registration)
        if current is None:
            return 0
        return self._apply_to_hwnd(current, hwnd)

    def sweep(self) -> int:
        """Backwards-compatible alias for :meth:`heartbeat`."""
        return self.heartbeat()

    def _handle_name_change(self, hwnd: int) -> None:
        hwnd = int(hwnd or 0)
        if hwnd <= 0:
            return
        registration = self._registration_for_hwnd(hwnd)
        if registration is None:
            # T-216 TARGET C: an unmapped HWND event is CHEAP. It only marks
            # that InstanceMonitor must re-prove ownership and records the
            # observed handle; NO desktop scan runs inside the WinEvent
            # callback, and no guardian synchronization is held across one.
            with self._lock:
                self._authority_needed = True
                if hwnd not in self._observed_hwnds:
                    self._observed_hwnds.append(hwnd)
                    del self._observed_hwnds[:-MAX_HWNDS_PER_PID]
            return
        if not self._still_owns(registration, hwnd):
            return
        self._apply_to_hwnd(registration, hwnd)

    def _registration_for_hwnd(self, hwnd: int) -> TitleRegistration | None:
        hwnd = int(hwnd or 0)
        with self._lock:
            binding = self._bindings.get(hwnd)
            if binding is not None:
                registration = self._registrations.get(binding.launch_pid)
                if registration is not None:
                    return registration
            for pid, hwnds in self._hwnds.items():
                if hwnd in hwnds:
                    return self._registrations.get(pid)
        # O(1) owner lookup, outside the lock; no desktop enumeration.
        try:
            pid = int(self.backend.window_pid(hwnd) or 0)
        except Exception:
            pid = 0
        if not pid:
            return None
        with self._lock:
            return self._registrations.get(pid)

    def _current_registration(
        self, registration: TitleRegistration
    ) -> TitleRegistration | None:
        """Caller must hold the lock; None when retired or replaced."""
        current = self._registrations.get(registration.pid)
        if current is None or current.generation != registration.generation:
            return None
        return current

    def _process_token(self, pid: int) -> int:
        try:
            return int(self.backend.process_token(pid) or 0)
        except Exception:
            return 0

    def _process_identity_ok(self, registration: TitleRegistration) -> bool:
        """Launch process alive and not replaced (creation-token match)."""
        try:
            if not self.backend.process_alive(registration.pid):
                return False
        except Exception:
            return False
        current_token = self._process_token(registration.pid)
        if (
            registration.process_token
            and current_token
            and current_token != registration.process_token
        ):
            return False
        return True

    def _window_alive(self, hwnd: int) -> bool:
        probe = getattr(self.backend, "is_window", None)
        if probe is None:
            return True
        try:
            return bool(probe(int(hwnd)))
        except Exception:
            return False

    def _still_owns(self, registration: TitleRegistration, hwnd: int) -> bool:
        """Re-verify identity before any restore (HWND/PID reuse safety)."""
        if not self._process_identity_ok(registration):
            return False
        binding = self._bindings.get(int(hwnd or 0))
        if binding is not None and binding.launch_pid == registration.pid:
            if binding.generation != registration.generation:
                return False
            return self._window_alive(hwnd)
        return hwnd in self._resolve_for(registration)

    def _resolve(self, pid: int) -> list[int]:
        """PID-scoped, read-only resolution. Never attaches to a console."""
        try:
            hwnds = [int(h) for h in self.backend.resolve_hwnds(int(pid)) if h]
        except Exception as exc:
            self.last_error = f"window resolve failed: {exc}"
            return []
        return list(dict.fromkeys(hwnds))[:MAX_HWNDS_PER_PID]

    def _resolve_by_token(self, token: str) -> list[int]:
        """Correlation-token resolution. Read-only, process-safe."""
        token = str(token or "")
        if not token:
            return []
        resolver = getattr(self.backend, "resolve_hwnds_by_token", None)
        if resolver is None:
            return []
        try:
            hwnds = [int(h) for h in resolver(token) if h]
        except Exception as exc:
            self.last_error = f"token resolve failed: {exc}"
            return []
        return list(dict.fromkeys(hwnds))[:MAX_HWNDS_PER_PID]

    def _resolve_for(self, registration: TitleRegistration) -> list[int]:
        """Everything this process may safely learn about one launch's windows.

        The launch PID's own windows first, then this launch's own unguessable
        correlation token. Both are read-only ``EnumWindows`` scans; neither can
        change AUDAPACK's console association or standard handles (T-210
        TARGET A), and neither identifies an instance from generic title text.
        """
        merged = list(self._resolve(registration.pid))
        merged.extend(self._resolve_by_token(registration.correlation_token))
        return list(dict.fromkeys(h for h in merged if h))[:MAX_HWNDS_PER_PID]

    def _rebind_hwnds(
        self,
        pid: int,
        hwnds: list[int],
        *,
        native_pids: dict[int, int] | None = None,
    ) -> None:
        """Replace one registration's HWND ownership atomically.

        Old bindings for this launch that are no longer resolved are dropped so
        a reused HWND cannot keep being renamed through a stale ownership
        record. A binding already owned by ANOTHER live registration is never
        stolen.
        """
        registration = self._registrations.get(pid)
        if registration is None:
            return
        wanted = list(dict.fromkeys(int(h) for h in hwnds if h))
        for hwnd in list(self._bindings):
            binding = self._bindings[hwnd]
            if binding.launch_pid == pid and hwnd not in wanted:
                del self._bindings[hwnd]
        native_pids = native_pids or {}
        kept: list[int] = []
        for hwnd in wanted:
            existing = self._bindings.get(hwnd)
            if existing is not None and existing.launch_pid != pid and existing.launch_pid in self._registrations:
                continue
            native_pid = int(native_pids.get(hwnd, 0) or 0)
            if not native_pid:
                if existing is not None and existing.launch_pid == pid:
                    native_pid = existing.native_pid
                else:
                    native_pid = self._window_pid(hwnd)
            self._bindings[hwnd] = TitleBinding(
                hwnd=hwnd,
                launch_pid=pid,
                generation=registration.generation,
                correlation_token=registration.correlation_token,
                native_pid=native_pid,
            )
            kept.append(hwnd)
        self._hwnds[pid] = kept

    def _window_pid(self, hwnd: int) -> int:
        try:
            return int(self.backend.window_pid(hwnd) or 0)
        except Exception:
            return 0

    def _apply(self, registration: TitleRegistration) -> int:
        restored = 0
        for hwnd in list(self._hwnds.get(registration.pid, [])):
            restored += self._apply_to_hwnd(registration, hwnd)
        return restored

    def _apply_to_hwnd(self, registration: TitleRegistration, hwnd: int) -> int:
        binding = self._bindings.get(int(hwnd or 0))
        if binding is not None and binding.launch_pid == registration.pid and not self._window_alive(hwnd):
            # The window is gone; drop ownership instead of renaming a future
            # window that reuses this HWND.
            self._rebind_hwnds(
                registration.pid,
                [h for h in (self._hwnds.get(registration.pid) or []) if h != hwnd],
            )
            return 0
        try:
            current = str(self.backend.get_title(hwnd) or "")
        except Exception:
            return 0
        # Our own restoration generates a name-change event; the title already
        # matching canonical is the idempotence guard that stops the loop.
        if current == registration.canonical_title:
            return 0
        try:
            if not self.backend.set_title(hwnd, registration.canonical_title):
                return 0
        except Exception as exc:
            self.last_error = f"title restore failed: {exc}"
            return 0
        self.restores += 1
        return 1

    # -- backend lifecycle --------------------------------------------------

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._event_driven = bool(self.backend.start(self._callback))
        except Exception as exc:
            self._event_driven = False
            self.last_error = f"title hook unavailable: {exc}"

    def _stop_backend(self) -> None:
        if not self._started:
            return
        try:
            self.backend.stop()
        except Exception as exc:
            self.last_error = f"title hook stop failed: {exc}"
        self._started = False
        self._event_driven = False
