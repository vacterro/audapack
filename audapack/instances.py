"""Native agent-window discovery, launcher limits, and window operations."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

from audapack.config import get_state_dir, open_new_temp_file


@dataclass(frozen=True)
class NativeWindow:
    """Small platform-neutral projection of a visible top-level window."""

    hwnd: int
    pid: int
    title: str
    process_name: str = ""
    command_line: str = ""


@dataclass(frozen=True)
class WindowInstance:
    """Agent window associated with an AUDAPACK launcher and project."""

    hwnd: int
    pid: int
    title: str
    process_name: str
    launcher_id: str
    launcher_name: str
    project_id: str
    project_name: str
    project_path: str
    state: str = "running"
    tracked: bool = False
    activity: str = ""
    last_action: str = ""
    saipen_binding: dict[str, str] | None = None
    # Native window PID may differ from the process PID recorded at launch
    # (conhost/TUI path). Keep record identity without lying about ``pid``.
    launch_pid: int = 0
    #: Durable launch correlation token of the owning record, when the window
    #: was proven to belong to it. TitleGuardian.bind_hwnd consumes exactly
    #: this proven association instead of rediscovering the console window.
    correlation_token: str = ""

    @property
    def selectable(self) -> bool:
        return self.hwnd > 0 and self.state == "running"


@dataclass(frozen=True)
class InstanceSnapshot:
    """One complete, immutable instance picture produced OFF the GUI thread.

    T-216 TARGET G: the GUI-facing readers (``for_project``, ``focus_candidate``,
    ``launcher_states``, ``block_reason``) must consume one complete snapshot,
    never half-mutated worker state. ``scan`` builds it without touching the
    live ``instances`` list; the GUI installs it atomically with
    ``apply_snapshot``.
    """

    records: dict[int, "LaunchRecord"]
    instances: tuple["WindowInstance", ...]
    last_error: str = ""
    records_changed: bool = False
    #: Shared-record-file generation observed when the scan started, so a GUI
    #: commit can preserve a launch tracked while the scan was in flight.
    records_version: int = 0


@dataclass
class LaunchRecord:
    """Durable association created when AUDAPACK starts an agent process."""

    pid: int
    launcher_id: str
    project_id: str
    project_name: str
    project_path: str
    started_at: str
    process_token: int = 0
    # T-185: `started_at` alone cannot order two launches. Windows resolves
    # datetime.now() to roughly the clock tick (~1-16 ms), so two consoles
    # started back to back routinely share a timestamp -- and "focus the most
    # recent instance" then fell through to the lowest-PID tie-break and put
    # the OLDER window in front. `sequence` is a monotonic per-record-file
    # counter that breaks that tie in launch order. 0 means a legacy record
    # written before this field existed.
    sequence: int = 0
    saipen_binding: dict[str, str] | None = None
    # T-192: durable launch correlation token. AUDAPACK generates one per
    # bound launch, embeds it in the spawned console's title, and stores it
    # here. A window that carries the token is mechanically attributable to
    # this record even when its visible PID differs from the parent PID and
    # even when several bound instances of one project are live at once --
    # the old single-pending-record fallback cannot do that.
    correlation_token: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Optional[LaunchRecord]:
        try:
            pid = int(raw.get("pid", 0))
            if pid <= 0:
                return None
            return cls(
                pid=pid,
                launcher_id=str(raw.get("launcher_id", "")).strip(),
                project_id=str(raw.get("project_id", "")).strip(),
                project_name=str(raw.get("project_name", "")).strip(),
                project_path=str(raw.get("project_path", "")).strip(),
                started_at=str(raw.get("started_at", "")).strip(),
                process_token=int(raw.get("process_token", 0) or 0),
                sequence=int(raw.get("sequence", 0) or 0),
                saipen_binding=(raw.get("saipen_binding")
                                if isinstance(raw.get("saipen_binding"), dict) else None),
                correlation_token=str(raw.get("correlation_token", "") or ""),
            )
        except (TypeError, ValueError):
            return None


class WindowBackend(Protocol):
    def list_windows(self) -> list[NativeWindow]: ...

    def process_alive(self, pid: int) -> bool: ...

    def process_token(self, pid: int) -> int: ...

    def focus_window(self, hwnd: int) -> bool: ...

    def close_window(self, hwnd: int) -> bool: ...

    def arrange_windows(self, hwnds: Sequence[int], mode: str) -> int: ...


class NullWindowBackend:
    """Non-Windows implementation: truthful empty monitoring, no fake actions."""

    def list_windows(self) -> list[NativeWindow]:
        return []

    def process_alive(self, pid: int) -> bool:
        return False

    def process_token(self, pid: int) -> int:
        return 0

    def focus_window(self, hwnd: int) -> bool:
        return False

    def close_window(self, hwnd: int) -> bool:
        return False

    def arrange_windows(self, hwnds: Sequence[int], mode: str) -> int:
        return 0


class Win32WindowBackend:
    """Minimal stdlib-only adapter around safe top-level Win32 operations."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    SW_RESTORE = 9
    WM_CLOSE = 0x0010

    def _process_handle(self, pid: int):
        import ctypes

        return ctypes.windll.kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )

    def _process_name(self, pid: int) -> str:
        import ctypes
        from ctypes import wintypes

        handle = self._process_handle(pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).name
            return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def _process_command_line(self, pid: int) -> str:
        """Read the original command line even when a TUI rewrites its title."""
        import ctypes
        from ctypes import wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p),
            ]

        handle = self._process_handle(pid)
        if not handle:
            return ""
        try:
            ntdll = ctypes.WinDLL("ntdll")
            query = ntdll.NtQueryInformationProcess
            query.argtypes = [
                wintypes.HANDLE,
                wintypes.ULONG,
                ctypes.c_void_p,
                wintypes.ULONG,
                ctypes.POINTER(wintypes.ULONG),
            ]
            query.restype = ctypes.c_long
            needed = wintypes.ULONG()
            query(handle, 60, None, 0, ctypes.byref(needed))
            if needed.value <= ctypes.sizeof(UnicodeString):
                return ""
            buffer = ctypes.create_string_buffer(needed.value)
            status = query(handle, 60, buffer, len(buffer), ctypes.byref(needed))
            if status < 0:
                return ""
            value = ctypes.cast(buffer, ctypes.POINTER(UnicodeString)).contents
            if not value.Buffer or not value.Length:
                return ""
            return ctypes.wstring_at(value.Buffer, value.Length // ctypes.sizeof(ctypes.c_wchar))
        except (AttributeError, OSError, ValueError):
            return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def list_windows(self) -> list[NativeWindow]:
        import ctypes
        from ctypes import wintypes

        windows: list[NativeWindow] = []
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                windows.append(
                    NativeWindow(
                        hwnd=int(hwnd),
                        pid=int(pid.value),
                        title=title,
                        process_name=self._process_name(int(pid.value)),
                        command_line=self._process_command_line(int(pid.value)),
                    )
                )
            return True

        callback = callback_type(collect)
        user32.EnumWindows(callback, 0)
        return windows

    def process_alive(self, pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        handle = self._process_handle(pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == self.STILL_ACTIVE
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def process_token(self, pid: int) -> int:
        import ctypes
        from ctypes import wintypes

        handle = self._process_handle(pid)
        if not handle:
            return 0
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return 0
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def focus_window(self, hwnd: int) -> bool:
        import ctypes

        user32 = ctypes.windll.user32
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        user32.ShowWindow(hwnd, self.SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))

    def close_window(self, hwnd: int) -> bool:
        import ctypes

        user32 = ctypes.windll.user32
        return bool(hwnd and user32.IsWindow(hwnd) and user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))

    @staticmethod
    def _work_area(hwnd: int) -> tuple[int, int, int, int]:
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromWindow(hwnd, 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            return rect.left, rect.top, rect.right, rect.bottom

        rect = wintypes.RECT()
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, 1280, 720

    def arrange_windows(self, hwnds: Sequence[int], mode: str) -> int:
        import ctypes

        user32 = ctypes.windll.user32
        valid = [int(hwnd) for hwnd in dict.fromkeys(hwnds) if hwnd and user32.IsWindow(int(hwnd))]
        if not valid or mode not in {"cascade", "tile_horizontal", "tile_vertical"}:
            return 0

        left, top, right, bottom = self._work_area(valid[0])
        area_w = max(320, right - left)
        area_h = max(240, bottom - top)
        count = len(valid)

        if mode == "cascade":
            steps = min(count, 8)
            offset = 28
            width = max(320, area_w - offset * max(2, steps))
            height = max(240, area_h - offset * max(2, steps))
            rects = [
                (left + (idx % steps) * offset, top + (idx % steps) * offset, width, height)
                for idx in range(count)
            ]
        elif mode == "tile_horizontal":
            height = max(160, area_h // count)
            rects = [
                (left, top + idx * height, area_w, area_h - idx * height if idx == count - 1 else height)
                for idx in range(count)
            ]
        else:
            width = max(240, area_w // count)
            rects = [
                (left + idx * width, top, area_w - idx * width if idx == count - 1 else width, area_h)
                for idx in range(count)
            ]

        moved = 0
        for hwnd, (x, y, width, height) in zip(valid, rects, strict=True):
            user32.ShowWindow(hwnd, self.SW_RESTORE)
            if user32.MoveWindow(hwnd, x, y, width, height, True):
                moved += 1
        return moved


def create_window_backend() -> WindowBackend:
    return Win32WindowBackend() if sys.platform == "win32" else NullWindowBackend()


def _read_saipen_activity(project_path: str) -> tuple[str, str]:
    """Read explicit SAIPEN state; never pretend private reasoning is observable."""
    if not project_path:
        return "", ""
    memory_dir = Path(project_path) / ".saipen"
    state_path = memory_dir / "STATE.md"
    values: dict[str, str] = {}
    try:
        for line in state_path.read_text(encoding="utf-8").splitlines():
            if ":" not in line or line.startswith(("---", "#", " ", "\t")):
                continue
            key, value = line.split(":", 1)
            if key in {"phase", "task", "next_action"}:
                values[key] = value.strip().strip('"\'')
    except OSError:
        pass

    activity = " · ".join(value for value in (values.get("phase"), values.get("task"), values.get("next_action")) if value)
    last_action = ""
    try:
        with (memory_dir / "LOG.md").open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 32768))
            tail = handle.read().decode("utf-8", errors="replace")
        last_action = next((line.strip() for line in reversed(tail.splitlines()) if line.strip()), "")
    except OSError:
        pass
    return activity, last_action


class InstanceMonitor:
    """Discovers agent windows and keeps launch-to-project associations truthful."""

    _KNOWN_TITLE_TOKENS: tuple[tuple[str, str], ...] = (
        ("codex (main_codex3_free)", "main_codex3_free"),
        ("codex (main_codex2)", "main_codex2"),
        ("codex (main_codex)", "main_codex"),
        ("codex free", "main_codex3_free"),
        ("codex c2", "main_codex2"),
        ("opencode", "opencode"),
        ("freebuff", "freebuff"),
        ("cline", "cline"),
        ("openai codex", "main_codex"),
    )

    #: T-179: identities for agents nobody configured a launcher for. Consulted
    #: only AFTER the operator's own launchers, so a custom ``my_claude``
    #: launcher keeps its own id instead of being overwritten by a synthetic
    #: one. The Claude token is "claude code", never bare "claude": a browser
    #: tab titled "Chat with Claude" is not an agent console, and a window that
    #: resolves to one of these without a project still has to pass
    #: ``_is_probable_agent_window``.
    _FALLBACK_TITLE_TOKENS: tuple[tuple[str, str], ...] = (
        ("claude code", "claude"),
        ("zcode", "zcode"),
        # SRC-081 TARGET L: added because the product name is specific
        # evidence on its own (and the non-agent-process rejection above
        # already bars browsers/chat clients); an arbitrary chat window can
        # never satisfy this the way a bare "ai" or "agent" token would.
        ("antigravity", "antigravity"),
    )

    #: Human-facing names for the fallback identities above. A configured
    #: launcher's own name always wins over these.
    _FALLBACK_LAUNCHER_NAMES: dict[str, str] = {
        "claude": "Claude Code",
        "zcode": "ZCode",
        "antigravity": "Antigravity",
    }

    #: Processes that render other people's text. A launcher word in one of
    #: their titles is content, not evidence of an agent console.
    _NON_AGENT_PROCESSES: frozenset[str] = frozenset({
        "chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "opera.exe",
        "vivaldi.exe", "iexplore.exe", "discord.exe", "slack.exe", "teams.exe",
        "telegram.exe", "whatsapp.exe", "thunderbird.exe", "outlook.exe",
    })

    def __init__(
        self,
        *,
        backend: Optional[WindowBackend] = None,
        record_path: Optional[Path] = None,
    ):
        self.backend = backend or create_window_backend()
        self.record_path = Path(record_path) if record_path is not None else get_state_dir() / "instances.json"
        self.records: dict[int, LaunchRecord] = {}
        self.instances: list[WindowInstance] = []
        self.last_error = ""
        # T-216 TARGET F/G: one serialized owner for the shared records file and
        # the live snapshot. A background scan and a GUI install never interleave.
        self._lock = threading.RLock()
        #: Monotonic counter of in-process record mutations, so a GUI commit can
        #: tell whether a launch was tracked while its background scan ran.
        self._records_version = 0
        self._load_records()

    def _load_records(self) -> None:
        try:
            raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            self.records = {}
            self._records_version += 1
            return
        if not isinstance(raw, list):
            self.records = {}
            self._records_version += 1
            return
        loaded: dict[int, LaunchRecord] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            record = LaunchRecord.from_dict(item)
            if record:
                loaded[record.pid] = record
        # The record file is shared by every AUDAPACK GUI process. Replace the
        # snapshot atomically so a second GUI sees launches made by the first
        # one, and so deleted/exited records cannot survive in memory forever.
        self.records = loaded
        self._records_version += 1

    def _save_records(self) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = open_new_temp_file(self.record_path.parent, self.record_path.name)
        payload = [asdict(record) for record in sorted(self.records.values(), key=lambda item: item.pid)]
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            os.replace(temp, self.record_path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def track_launch(
        self, pid: Any, launcher_id: str, project: Any,
        *, saipen_binding: dict[str, str] | None = None,
        correlation_token: str = "",
    ) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int):
            return False
        numeric_pid = pid
        if numeric_pid <= 0:
            return False
        with self._lock:
            record = LaunchRecord(
                pid=numeric_pid,
                launcher_id=str(launcher_id),
                project_id=str(getattr(project, "id", "")),
                project_name=str(getattr(project, "display_name", "")),
                project_path=str(getattr(project, "source_path", "")),
                started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                process_token=0,
                # Derived from the shared record file, so a second GUI process
                # continues the same order instead of restarting it.
                sequence=self._next_launch_sequence(),
                saipen_binding=saipen_binding,
                correlation_token=str(correlation_token or ""),
            )
            try:
                record.process_token = int(self.backend.process_token(numeric_pid) or 0)
            except Exception:
                record.process_token = 0
            self.records[numeric_pid] = record
            self._records_version += 1
            # T-216 TARGET J: the launch record alone is enough to render
            # STARTING immediately -- no desktop scan. The next background scan
            # replaces this provisional row with the proven one.
            self._install_starting_instance(record)
            try:
                self._save_records()
            except OSError:
                return False
            return True

    def _install_starting_instance(self, record: LaunchRecord) -> None:
        if any(getattr(item, "launch_pid", 0) == record.pid for item in self.instances):
            return
        self.instances = self.instances + [
            WindowInstance(
                hwnd=0,
                pid=record.pid,
                title="Starting — window not visible yet",
                process_name="",
                launcher_id=record.launcher_id,
                launcher_name=record.launcher_id,
                project_id=record.project_id,
                project_name=record.project_name,
                project_path=record.project_path,
                state="starting",
                tracked=True,
                saipen_binding=record.saipen_binding,
                launch_pid=record.pid,
                correlation_token=str(getattr(record, "correlation_token", "") or ""),
            )
        ]

    def untrack_launch(self, pid: Any) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int):
            return False
        numeric_pid = pid
        with self._lock:
            if numeric_pid not in self.records:
                return False
            del self.records[numeric_pid]
            self.instances = [
                item for item in self.instances
                if int(getattr(item, "launch_pid", 0) or 0) != numeric_pid
            ]
            self._records_version += 1
            try:
                self._save_records()
            except OSError:
                return False
            return True

    @staticmethod
    def _launcher_from_title(title: str, launchers: Iterable[Any]) -> str:
        folded = str(title).casefold()
        # T-179: the operator's configured launchers own their names. A custom
        # "OpenCode Special" must not be rewritten into the built-in "opencode"
        # merely because its name contains that generic word, and a configured
        # "Claude Code Pro" must stay its own id instead of collapsing into the
        # fallback "claude". Longest token first, so the most specific
        # configured identity wins; exact ids remain exact, never families.
        candidates: list[tuple[int, str, str]] = []
        for launcher in launchers:
            launcher_id = str(getattr(launcher, "id", "")).strip()
            launcher_name = str(getattr(launcher, "name", "")).strip()
            for token in (launcher_name, launcher_id.replace("_", " ")):
                if len(token) >= 4:
                    candidates.append((len(token), token.casefold(), launcher_id))
        for _length, token, launcher_id in sorted(candidates, reverse=True):
            if token in folded:
                return launcher_id
        # Only with no configured launcher claiming this window may a shipped
        # canonical identity answer for it.
        for token, launcher_id in InstanceMonitor._KNOWN_TITLE_TOKENS:
            if token in folded:
                return launcher_id
        # T-179: last, identities for agents nobody configured a launcher for.
        for token, launcher_id in InstanceMonitor._FALLBACK_TITLE_TOKENS:
            if token in folded:
                return launcher_id
        return ""

    @classmethod
    def _launcher_display_name(cls, launcher_id: str, launcher: Any) -> str:
        """The operator's own launcher name, else a known fallback, else the id."""
        configured = str(getattr(launcher, "name", "") or "").strip()
        if configured:
            return configured
        return cls._FALLBACK_LAUNCHER_NAMES.get(launcher_id, launcher_id)

    @staticmethod
    def _same_launcher_family(first: str, second: str) -> bool:
        if first == second:
            return True
        return first.startswith("main_codex") and second.startswith("main_codex")

    @staticmethod
    def _project_from_title(title: str, projects: Iterable[Any]) -> Optional[Any]:
        folded = str(title).casefold().replace("/", "\\")
        segments = {segment.strip() for segment in folded.split("|") if segment.strip()}
        path_matches: list[tuple[int, Any]] = []
        name_matches: list[tuple[int, Any]] = []
        for project in projects:
            source = str(getattr(project, "source_path", "")).strip().casefold().replace("/", "\\")
            name = str(getattr(project, "display_name", "")).strip().casefold()
            project_id = str(getattr(project, "id", "")).strip().casefold()
            if source and source in folded:
                path_matches.append((len(source), project))
            elif name and (name in segments or folded.startswith(name + " ") or folded.startswith(name + "|")):
                name_matches.append((len(name), project))
            elif project_id and project_id in segments:
                name_matches.append((len(project_id), project))
        matches = path_matches or name_matches
        return max(matches, key=lambda item: item[0])[1] if matches else None

    _ROOTED_PATH_RE = re.compile(r"[a-z]:\\[^|\"']+", re.IGNORECASE)

    _WORKDIR_RE = re.compile(
        r'(?:--?)(?:workdir|cwd|literalpath)\s+"?([a-z]:\\[^"]*?)"?(?=\s|$)',
        re.IGNORECASE,
    )

    @classmethod
    def _declared_workdir(cls, text: str) -> str:
        """The working directory a launcher was explicitly told to use.

        AI_AGENT_LAUNCHER.PS1 lives inside one project and is invoked with
        -WorkDir pointing at another. The script path is not where the console
        works; the workdir is, and when one is declared it settles the question
        outright.
        """
        found = cls._WORKDIR_RE.findall(str(text).replace("/", "\\"))
        return found[-1].strip().casefold().rstrip("\\") if found else ""

    @classmethod
    def _window_names_another_root(cls, identity: str, record: LaunchRecord) -> bool:
        """True when the window plainly says it lives somewhere else.

        A window with no project match is adopted by the only pending launch
        record for its launcher, on the theory that a just-started console has
        not titled itself yet. But a window that already names a concrete
        directory has titled itself, and that directory is the answer.

        Observed live: an OpenCode window under __STORE/_PERSONAL/_9router was
        adopted as __SAITULS -- whose source is __CODE/__SAITULS -- because it
        happened to be the only pending OpenCode launch. The Instances tab
        then showed __SAITULS twice, one of them a window belonging to
        something else entirely.
        """
        root = str(getattr(record, "project_path", "") or "").strip().casefold().replace("/", "\\").rstrip("\\")
        if not root:
            return False
        workdir = cls._declared_workdir(identity)
        if workdir:
            return not workdir.startswith(root)
        found = [match.group(0).casefold().replace("/", "\\") for match in cls._ROOTED_PATH_RE.finditer(str(identity))]
        if not found:
            return False
        return not any(path.startswith(root) for path in found)

    @staticmethod
    def _project_from_command_line(command_line: str, projects: Iterable[Any]) -> Optional[Any]:
        """Prefer paths passed as working directories over launcher-script paths."""
        folded = str(command_line).casefold().replace("/", "\\")
        # An explicit working directory is the answer, full stop. Without this
        # a launcher script living inside project A but invoked with
        # -WorkDir pointing at B was attributed to A, and A appeared twice in
        # the Instances tab with one row that was never its window.
        declared = InstanceMonitor._declared_workdir(command_line)
        if declared:
            rooted = [
                project for project in projects
                if str(getattr(project, "source_path", "")).strip()
                and declared.startswith(
                    str(getattr(project, "source_path", "")).strip().casefold().replace("/", "\\").rstrip("\\")
                )
            ]
            if not rooted:
                return None
            return max(rooted, key=lambda item: len(str(getattr(item, "source_path", ""))))
        matches: list[tuple[int, int, Any]] = []
        workdir_tokens = ("-workdir", "--workdir", "--cwd", "-cwd", "-literalpath")
        for project in projects:
            source = str(getattr(project, "source_path", "")).strip().casefold().replace("/", "\\")
            if not source:
                continue
            offset = 0
            while True:
                index = folded.find(source, offset)
                if index < 0:
                    break
                prefix = folded[max(0, index - 48) : index]
                explicit_workdir = int(any(token in prefix for token in workdir_tokens))
                matches.append((explicit_workdir, len(source), project))
                offset = index + len(source)
        if not matches:
            return None
        explicit = [item for item in matches if item[0]]
        candidates = explicit or matches
        return max(candidates, key=lambda item: item[:2])[2]

    @classmethod
    def _is_probable_agent_window(cls, window: NativeWindow, launcher_id: str, launchers: Iterable[Any]) -> bool:
        """Accept unregistered agent consoles without trusting arbitrary title words."""
        # T-179: a browser or chat client showing the words "Claude Code" is
        # displaying somebody's page, not hosting an agent. No title and no
        # command line inside these processes may promote a window to an agent
        # console.
        if window.process_name.casefold() in cls._NON_AGENT_PROCESSES:
            return False
        command_launcher = cls._launcher_from_title(window.command_line, launchers)
        if command_launcher and cls._same_launcher_family(command_launcher, launcher_id):
            return True
        process = window.process_name.casefold()
        direct_processes = {
            "opencode.exe": "opencode",
            "freebuff.exe": "freebuff",
            "cline.exe": "cline",
            "codex.exe": "main_codex",
            # SRC-081 TARGET L: a specific product process is evidence on its
            # own. A bare ``claude.exe`` keeps the GENERIC fallback identity
            # "claude" -- it is never guessed as Claude 1 or Claude 2 without
            # a record, a correlation token or a managed title saying so.
            "claude.exe": "claude",
            "zcode.exe": "zcode",
            "antigravity.exe": "antigravity",
            "antigravity ide.exe": "antigravity",
        }
        process_launcher = direct_processes.get(process, "")
        return bool(process_launcher and cls._same_launcher_family(process_launcher, launcher_id))

    def _live_records(
        self, records: dict[int, LaunchRecord] | None = None
    ) -> tuple[dict[int, LaunchRecord], bool]:
        source = self.records if records is None else records
        live: dict[int, LaunchRecord] = {}
        changed = False
        for pid, record in source.items():
            if not self.backend.process_alive(pid):
                changed = True
                continue
            current_token = int(self.backend.process_token(pid) or 0)
            if record.process_token and current_token and current_token != record.process_token:
                changed = True
                continue
            live[pid] = record
        return live, changed

    def refresh(self, projects: Iterable[Any], launchers: Iterable[Any]) -> list[WindowInstance]:
        """Synchronous scan+install convenience for non-GUI callers/tests.

        The Qt GUI must NOT call this on its event loop; it requests the
        background lane instead (T-216 TARGET E/F). Kept for the CLI, the
        native worker and the existing test contract.
        """
        snapshot = self.scan(projects, launchers)
        self.apply_snapshot(snapshot)
        return list(self.instances)

    def scan(self, projects: Iterable[Any], launchers: Iterable[Any]) -> InstanceSnapshot:
        """Build ONE immutable :class:`InstanceSnapshot` (worker-thread safe).

        Performs every native/disk operation -- launch-record read, window
        enumeration, process metadata, correlation, SAIPEN activity reads -- and
        returns a complete picture WITHOUT mutating the live ``instances`` list,
        so the GUI never observes half-mutated worker state (T-216 TARGET G).
        """
        project_list = list(projects)
        launcher_list = list(launchers)
        launcher_by_id = {str(getattr(item, "id", "")): item for item in launcher_list}
        project_by_id = {str(getattr(item, "id", "")): item for item in project_list}
        with self._lock:
            self._load_records()
            base_records = dict(self.records)
            base_version = self._records_version
        try:
            live_records, records_changed = self._live_records(base_records)
            raw_windows = self.backend.list_windows()
        except Exception as exc:
            return InstanceSnapshot({}, (), f"Native window scan failed: {exc}", False, base_version)

        # T-192: windows that carry a launch correlation token are attributable
        # to their record mechanically -- no title heuristics, no "only pending
        # record" fallback. Tokens must be unique among live records. This
        # pre-pass runs before any fallback so a legacy unbound window can
        # never steal a bound record merely by being scanned first.
        token_map: dict[str, LaunchRecord] = {}
        for record in live_records.values():
            token = str(getattr(record, "correlation_token", "") or "")
            if token:
                token_map.setdefault(token, record)

        preassigned: dict[int, LaunchRecord] = {}
        claimed_records: set[int] = set()
        for raw_window in raw_windows:
            if live_records.get(raw_window.pid) is not None or raw_window.pid in preassigned:
                continue
            window_identity = (
                f"{raw_window.title} | {raw_window.command_line}"
                if raw_window.command_line else raw_window.title
            )
            for token, candidate in token_map.items():
                if candidate.pid in claimed_records:
                    continue
                if token in window_identity and not self._window_names_another_root(window_identity, candidate):
                    preassigned[raw_window.pid] = candidate
                    claimed_records.add(candidate.pid)
                    break

        pending_by_launcher: dict[str, list[LaunchRecord]] = {}
        for record in live_records.values():
            pending_by_launcher.setdefault(record.launcher_id, []).append(record)
        for records in pending_by_launcher.values():
            # Same total order as focus_candidate: a bare timestamp ties for
            # launches inside one clock tick (T-185).
            records.sort(key=lambda item: (item.started_at, int(item.sequence or 0)))

        result: list[WindowInstance] = []
        consumed_records: set[int] = set()
        for raw in raw_windows:
            record = live_records.get(raw.pid)
            # T-179: a native host that only renders other people's text can
            # never become an agent from its title -- not even when the title
            # also names a project. A trusted AUDAPACK launch record still
            # identifies its own process, so the rejection applies to untracked
            # windows only and legitimate tracked browser launches survive.
            if record is None and raw.process_name.casefold() in self._NON_AGENT_PROCESSES:
                continue
            identity = f"{raw.title} | {raw.command_line}" if raw.command_line else raw.title
            launcher_id = record.launcher_id if record else self._launcher_from_title(identity, launcher_list)
            if not launcher_id:
                continue

            # A launch record says where this process STARTED. The window
            # title says where it is now, and a console the operator pointed at
            # another directory is not a second window of the project that
            # launched it: __SAITULS was listed twice, its own console plus an
            # OpenCode window titled _9router and rooted under
            # __STORE/_PERSONAL. When the window names a root the record
            # cannot contain, the window wins and the stale association drops.
            if record and self._window_names_another_root(identity, record):
                record = None

            # T-192: a console titled by this record is provably its window,
            # whichever PID it shows and however many other pending records
            # exist. This outranks every heuristic fallback below.
            if record is None and raw.pid in preassigned:
                record = preassigned[raw.pid]
                launcher_id = record.launcher_id
                consumed_records.add(record.pid)

            if record:
                project = project_by_id.get(record.project_id)
            else:
                project = self._project_from_title(raw.title, project_list)
                if project is None and raw.command_line:
                    project = self._project_from_command_line(raw.command_line, project_list)
            if record is None and project is not None:
                project_records = [
                    item
                    for item in live_records.values()
                    if item.project_id == str(getattr(project, "id", ""))
                    and item.pid not in consumed_records
                    # T-192: a token-bearing bound launch is attributable only
                    # through its token or its own PID -- a legacy window with
                    # the same title family must never inherit its binding.
                    and not str(getattr(item, "correlation_token", "") or "")
                    and self._same_launcher_family(item.launcher_id, launcher_id)
                ]
                if len(project_records) == 1:
                    record = project_records[0]
                    launcher_id = record.launcher_id
                    consumed_records.add(record.pid)
            if project is None:
                candidates = [
                    item for item in pending_by_launcher.get(launcher_id, [])
                    if item.pid not in consumed_records
                    and not str(getattr(item, "correlation_token", "") or "")
                    and not self._window_names_another_root(identity, item)
                ]
                if len(candidates) == 1:
                    record = candidates[0]
                    consumed_records.add(record.pid)
                    project = project_by_id.get(record.project_id)
            elif record:
                consumed_records.add(record.pid)

            # A launcher word can occur in unrelated browser/chat titles. Keep
            # project-less windows only when the native process/command line
            # independently proves this is an agent console.
            if project is None and record is None and not self._is_probable_agent_window(
                raw, launcher_id, launcher_list
            ):
                continue

            launcher = launcher_by_id.get(launcher_id)
            result.append(
                WindowInstance(
                    hwnd=raw.hwnd,
                    pid=raw.pid,
                    title=raw.title,
                    process_name=raw.process_name,
                    launcher_id=launcher_id,
                    launcher_name=self._launcher_display_name(launcher_id, launcher),
                    project_id=str(getattr(project, "id", record.project_id if record else "")),
                    project_name=str(getattr(project, "display_name", record.project_name if record else "Unknown project")),
                    project_path=str(getattr(project, "source_path", record.project_path if record else "")),
                    tracked=record is not None,
                    saipen_binding=record.saipen_binding if record else None,
                    launch_pid=record.pid if record else 0,
                    correlation_token=str(getattr(record, "correlation_token", "") or "") if record else "",
                )
            )

        visible_record_pids = {item.pid for item in result if item.tracked}
        for record in live_records.values():
            if record.pid in visible_record_pids or record.pid in consumed_records:
                continue
            launcher = launcher_by_id.get(record.launcher_id)
            result.append(
                WindowInstance(
                    hwnd=0,
                    pid=record.pid,
                    title="Starting — window not visible yet",
                    process_name="",
                    launcher_id=record.launcher_id,
                    launcher_name=self._launcher_display_name(record.launcher_id, launcher),
                    project_id=record.project_id,
                    project_name=record.project_name,
                    project_path=record.project_path,
                    state="starting",
                    tracked=True,
                    saipen_binding=record.saipen_binding,
                    launch_pid=record.pid,
                    correlation_token=str(getattr(record, "correlation_token", "") or ""),
                )
            )

        activity_cache: dict[str, tuple[str, str]] = {}
        enriched: list[WindowInstance] = []
        for item in result:
            if item.project_path not in activity_cache:
                activity_cache[item.project_path] = _read_saipen_activity(item.project_path)
            activity, last_action = activity_cache[item.project_path]
            enriched.append(replace(item, activity=activity, last_action=last_action))
        result = enriched

        ordered = tuple(sorted(
            result,
            key=lambda item: (item.project_name.casefold(), item.launcher_name.casefold(), item.title.casefold()),
        ))
        return InstanceSnapshot(live_records, ordered, "", bool(records_changed), base_version)

    def apply_snapshot(self, snapshot: InstanceSnapshot) -> None:
        """Install a completed snapshot atomically, on the GUI thread.

        Runs under the same lock as ``track_launch``/``untrack_launch`` and
        ``scan``'s base read, so a launch tracked while a scan was in flight is
        preserved (its record is re-merged) and is never clobbered by the older
        scan (T-216 TARGET F/G).
        """
        with self._lock:
            if self._records_version != snapshot.records_version:
                merged = dict(snapshot.records)
                for pid, record in self.records.items():
                    merged.setdefault(pid, record)
                self.records = merged
                if snapshot.records_changed:
                    try:
                        self._save_records()
                    except OSError:
                        pass
            else:
                self.records = dict(snapshot.records)
                if snapshot.records_changed:
                    try:
                        self._save_records()
                    except OSError:
                        pass
            self.instances = list(snapshot.instances)
            self.last_error = snapshot.last_error

    def instance_alive(self, instance: WindowInstance) -> bool:
        """Bounded O(1) liveness probe for ONE instance, no desktop scan.

        T-216: the GUI must not run a full native scan to learn whether an
        instance that refused focus is still running. This reads the already
        known launch PID and asks the backend once.
        """
        pid = int(getattr(instance, "launch_pid", 0) or getattr(instance, "pid", 0) or 0)
        if pid <= 0:
            return False
        try:
            return bool(self.backend.process_alive(pid))
        except Exception:
            return False

    def for_project(self, project_id: str) -> list[WindowInstance]:
        return [item for item in self.instances if item.project_id == project_id]

    def for_project_launcher(self, project_id: str, launcher_id: str) -> list[WindowInstance]:
        """Instances of EXACTLY this launcher for EXACTLY this project.

        T-179: deliberately NOT ``_same_launcher_family``. main_codex,
        main_codex2 and main_codex3_free are three separate buttons that open
        three separate accounts; a click on C2 that focused C1 would be the
        wrong window, silently. Project identity is exact for the same reason:
        Project A's OpenCode is not Project B's.
        """
        target_project = str(project_id or "")
        target_launcher = str(launcher_id or "")
        if not target_project or not target_launcher:
            return []
        return [
            item for item in self.instances
            if item.project_id == target_project and item.launcher_id == target_launcher
        ]

    def _next_launch_sequence(self) -> int:
        highest = 0
        for record in self.records.values():
            highest = max(highest, int(getattr(record, "sequence", 0) or 0))
        return highest + 1

    def _record_recency_key(self, pid: int) -> tuple[str, int]:
        """Launch recency for one pid: timestamp first, launch order second.

        The sequence is what makes this total. Two launches inside one clock
        tick carry the same `started_at`, and without a second component the
        sort was a tie that the stable lowest-PID pass then decided -- so
        clicking a launcher focused the older console.
        """
        record = self.records.get(int(pid or 0))
        return (
            str(getattr(record, "started_at", "") or ""),
            int(getattr(record, "sequence", 0) or 0),
        )

    def focus_candidate(
        self, project_id: str, launcher_id: str,
        *, saipen_binding: dict[str, str] | None = None,
    ) -> Optional[WindowInstance]:
        """The one existing window a normal click should focus, or None.

        Deterministic so repeated clicks do not alternate between windows:
        AUDAPACK-tracked first, then the most recently launched, then the lowest
        PID as a stable tie-break. Composed as stable sorts, least significant
        key first.
        """
        running = [item for item in self.for_project_launcher(project_id, launcher_id) if item.selectable]
        if saipen_binding is not None:
            running = [item for item in running if item.tracked and
                       item.saipen_binding == saipen_binding]
        if not running:
            return None
        running.sort(key=lambda item: item.pid)
        running.sort(
            key=lambda item: self._record_recency_key(item.launch_pid or item.pid),
            reverse=True,
        )
        running.sort(key=lambda item: 0 if item.tracked else 1)
        return running[0]

    def starting_for_project_launcher(
        self, project_id: str, launcher_id: str,
        *, saipen_binding: dict[str, str] | None = None,
    ) -> list[WindowInstance]:
        """Matching launches that have not shown a window yet."""
        starting = [
            item for item in self.for_project_launcher(project_id, launcher_id)
            if item.state == "starting" or item.hwnd <= 0
        ]
        if saipen_binding is not None:
            starting = [item for item in starting if item.tracked and
                        item.saipen_binding == saipen_binding]
        return starting

    def launcher_states(self, project_id: str) -> dict[str, str]:
        """``{launcher_id: "running" | "starting"}`` for one project.

        Pure in-memory read of the last refresh, so the Project Room delegate
        can paint launcher state without any filesystem or native call on the
        paint path. "running" outranks "starting" for the same launcher.
        """
        states: dict[str, str] = {}
        for item in self.instances:
            if item.project_id != str(project_id or ""):
                continue
            state = "running" if item.selectable else "starting"
            if states.get(item.launcher_id) == "running":
                continue
            states[item.launcher_id] = state
        return states

    def count_for_launcher(self, launcher_id: str) -> int:
        return sum(1 for item in self.instances if item.launcher_id == launcher_id)

    def block_reason(self, launcher: Any) -> str:
        limit = max(0, int(getattr(launcher, "max_instances", 0) or 0))
        if limit <= 0:
            return ""
        matching = [item for item in self.instances if item.launcher_id == str(getattr(launcher, "id", ""))]
        if len(matching) < limit:
            return ""
        owner = matching[0]
        location = owner.project_name or owner.title or f"PID {owner.pid}"
        return f"{getattr(launcher, 'name', launcher.id)} limit {limit} reached by {location} (PID {owner.pid})"

    def focus(self, instance: WindowInstance) -> bool:
        if not instance.selectable:
            return False
        try:
            return self.backend.focus_window(instance.hwnd)
        except Exception as exc:
            self.last_error = f"Native focus failed: {exc}"
            return False

    def close(self, instance: WindowInstance) -> bool:
        if not instance.selectable:
            return False
        try:
            return self.backend.close_window(instance.hwnd)
        except Exception as exc:
            self.last_error = f"Native close failed: {exc}"
            return False

    def arrange(self, instances: Iterable[WindowInstance], mode: str) -> int:
        hwnds = [item.hwnd for item in instances if item.selectable]
        try:
            return self.backend.arrange_windows(hwnds, mode)
        except Exception as exc:
            self.last_error = f"Native layout failed: {exc}"
            return 0
