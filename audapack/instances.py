"""Native agent-window discovery, launcher limits, and window operations."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

from audapack.config import get_state_dir


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

    @property
    def selectable(self) -> bool:
        return self.hwnd > 0 and self.state == "running"


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
        self._load_records()

    def _load_records(self) -> None:
        try:
            raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            self.records = {}
            return
        if not isinstance(raw, list):
            self.records = {}
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

    def _save_records(self) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.record_path.with_name(f".{self.record_path.name}.tmp.{os.getpid()}")
        payload = [asdict(record) for record in sorted(self.records.values(), key=lambda item: item.pid)]
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.record_path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def track_launch(self, pid: Any, launcher_id: str, project: Any) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int):
            return False
        numeric_pid = pid
        if numeric_pid <= 0:
            return False
        record = LaunchRecord(
            pid=numeric_pid,
            launcher_id=str(launcher_id),
            project_id=str(getattr(project, "id", "")),
            project_name=str(getattr(project, "display_name", "")),
            project_path=str(getattr(project, "source_path", "")),
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            process_token=0,
        )
        try:
            record.process_token = int(self.backend.process_token(numeric_pid) or 0)
        except Exception:
            record.process_token = 0
        self.records[numeric_pid] = record
        try:
            self._save_records()
        except OSError:
            return False
        return True

    @staticmethod
    def _launcher_from_title(title: str, launchers: Iterable[Any]) -> str:
        folded = str(title).casefold()
        for token, launcher_id in InstanceMonitor._KNOWN_TITLE_TOKENS:
            if token in folded:
                return launcher_id
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
        return ""

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

    @staticmethod
    def _project_from_command_line(command_line: str, projects: Iterable[Any]) -> Optional[Any]:
        """Prefer paths passed as working directories over launcher-script paths."""
        folded = str(command_line).casefold().replace("/", "\\")
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
        command_launcher = cls._launcher_from_title(window.command_line, launchers)
        if command_launcher and cls._same_launcher_family(command_launcher, launcher_id):
            return True
        process = window.process_name.casefold()
        direct_processes = {
            "opencode.exe": "opencode",
            "freebuff.exe": "freebuff",
            "cline.exe": "cline",
            "codex.exe": "main_codex",
        }
        process_launcher = direct_processes.get(process, "")
        return bool(process_launcher and cls._same_launcher_family(process_launcher, launcher_id))

    def _live_records(self) -> tuple[dict[int, LaunchRecord], bool]:
        live: dict[int, LaunchRecord] = {}
        changed = False
        for pid, record in self.records.items():
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
        self.last_error = ""
        project_list = list(projects)
        launcher_list = list(launchers)
        launcher_by_id = {str(getattr(item, "id", "")): item for item in launcher_list}
        project_by_id = {str(getattr(item, "id", "")): item for item in project_list}
        try:
            # Another AUDAPACK process may have launched or removed an agent
            # since our last scan. Read the shared launch journal before
            # checking process liveness; otherwise global capacity is a lie
            # until this process itself launches something.
            self._load_records()
            live_records, records_changed = self._live_records()
            raw_windows = self.backend.list_windows()
        except Exception as exc:
            self.instances = []
            self.last_error = f"Native window scan failed: {exc}"
            return []
        self.records = live_records

        pending_by_launcher: dict[str, list[LaunchRecord]] = {}
        for record in live_records.values():
            pending_by_launcher.setdefault(record.launcher_id, []).append(record)
        for records in pending_by_launcher.values():
            records.sort(key=lambda item: item.started_at)

        result: list[WindowInstance] = []
        consumed_records: set[int] = set()
        for raw in raw_windows:
            record = live_records.get(raw.pid)
            identity = f"{raw.title} | {raw.command_line}" if raw.command_line else raw.title
            launcher_id = record.launcher_id if record else self._launcher_from_title(identity, launcher_list)
            if not launcher_id:
                continue

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
                    and self._same_launcher_family(item.launcher_id, launcher_id)
                ]
                if len(project_records) == 1:
                    record = project_records[0]
                    launcher_id = record.launcher_id
                    consumed_records.add(record.pid)
            if project is None:
                candidates = [item for item in pending_by_launcher.get(launcher_id, []) if item.pid not in consumed_records]
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
                    launcher_name=str(getattr(launcher, "name", launcher_id)),
                    project_id=str(getattr(project, "id", record.project_id if record else "")),
                    project_name=str(getattr(project, "display_name", record.project_name if record else "Unknown project")),
                    project_path=str(getattr(project, "source_path", record.project_path if record else "")),
                    tracked=record is not None,
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
                    launcher_name=str(getattr(launcher, "name", record.launcher_id)),
                    project_id=record.project_id,
                    project_name=record.project_name,
                    project_path=record.project_path,
                    state="starting",
                    tracked=True,
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

        if records_changed:
            try:
                self._save_records()
            except OSError:
                pass
        self.instances = sorted(
            result,
            key=lambda item: (item.project_name.casefold(), item.launcher_name.casefold(), item.title.casefold()),
        )
        return list(self.instances)

    def for_project(self, project_id: str) -> list[WindowInstance]:
        return [item for item in self.instances if item.project_id == project_id]

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
