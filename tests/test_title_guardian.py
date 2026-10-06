"""T-205/T-209/T-210: AUDAPACK owns the canonical console title for the instance lifetime.

The launcher's one-shot ``[Console]::Title`` is best-effort; real production
evidence showed concurrently running managed OpenCode instances degrading to
``Administrator: Windows PowerShell`` after ordinary Ctrl / Ctrl+C / Ctrl+V
interaction and losing project correlation. These tests pin the ownership
contract on the platform boundary. T-210 adds the invariant that made the
T-209 attempt an escaped regression: the main AUDAPACK process may NEVER
mutate its own process-global console state to discover a window -- that
stranded its standard handles and broke every later ``capture_output`` spawn
with ``[WinError 6] The handle is invalid``:

- canonical title assigned at launch;
- exact HWND associated with the managed instance;
- arbitrary/generic/OpenCode dynamic drift restored, including the
  Ctrl+C-equivalent PowerShell transition;
- own-restore events are idempotent (paste/control-key churn);
- an event for an HWND nobody mapped yet self-heals through the bounded
  slow path over active registrations (conhost window born after register);
- the integrity heartbeat repairs drift even when EVERY event is missed, so
  the title is never permanently wrong;
- 100 forced drift cycles all restore with no accumulation of bindings/hooks;
- the heartbeat interval is 0 and the hook is released with zero instances;
- a monitor-proven ``bind_hwnd`` association restores a window the OS resolver
  cannot see, and cannot be stolen by another launch;
- exit retires ownership; HWND/PID reuse cannot rename an unrelated window;
- repeated launcher click still focuses the exact instance after drift;
- same-project instances stay distinguishable by correlation token;
- T-210: the production backend contains NO ``AttachConsole``/``FreeConsole``/
  ``GetConsoleWindow`` call, so no title path can damage AUDAPACK's handles;
- T-210: a correlation token adopts a window the PID scan cannot see, and the
  heartbeat asks InstanceMonitor for authority only while a binding is missing.

Every native call goes through an in-process fake here; the REAL Win32
backend is exercised in tests/test_title_guardian_native.py (Windows-only).
"""

from __future__ import annotations

import ast
from pathlib import Path

from audapack.instances import InstanceMonitor, NativeWindow
from audapack.models import Project
from audapack.title_guardian import (
    FALLBACK_INTERVAL_MS,
    HEARTBEAT_INTERVAL_MS,
    NullTitleBackend,
    TitleGuardian,
)

GENERIC_TITLE = "Administrator: Windows PowerShell"
CANONICAL_A = r"AUDAPACK | OpenCode YOLO | V:\code\audapack | OC-aaa111"
CANONICAL_B = r"AUDAPACK | OpenCode YOLO | V:\code\audapack | OC-bbb222"


class FakeTitleBackend:
    """In-memory platform boundary with a controllable event hook."""

    def __init__(self, *, event_driven: bool = True):
        self.event_driven = event_driven
        self.hook_callback = None
        self.hook_started = 0
        self.hook_stopped = 0
        self.windows: dict[int, dict] = {}
        self.alive: dict[int, bool] = {}
        self.tokens: dict[int, int] = {}
        self.set_calls: list[tuple[int, str]] = []
        #: T-216 TARGET B/C: every desktop discovery is counted, so a
        #: regression that scans from the heartbeat or the WinEvent callback
        #: fails a hard assertion instead of a timing guess.
        self.resolve_calls = 0

    def add_window(
        self,
        hwnd: int,
        pid: int,
        title: str,
        *,
        token: int = 1111,
        native_pid: int | None = None,
        resolvable: bool = True,
    ) -> None:
        """Add a window.

        ``pid`` is the durable launch PID the window belongs to; ``native_pid``
        is the PID that actually owns the window (conhost.exe for a real
        console). ``resolvable`` models whether the OS resolver can see it at
        all -- False is the initial-discovery race and the conhost case.
        """
        self.windows[hwnd] = {
            "pid": pid,
            "title": title,
            "native_pid": pid if native_pid is None else native_pid,
            "resolvable": bool(resolvable),
        }
        self.alive[pid] = True
        self.tokens[pid] = token

    def resolve_hwnds(self, pid: int) -> list[int]:
        self.resolve_calls += 1
        return [
            hwnd
            for hwnd, info in self.windows.items()
            if info["pid"] == pid and info.get("resolvable", True)
        ]

    def resolve_hwnds_by_token(self, token: str) -> list[int]:
        """T-210: the correlation token is mechanical identity, never a guess."""
        needle = str(token or "")
        if not needle:
            return []
        self.resolve_calls += 1
        return [
            hwnd
            for hwnd, info in self.windows.items()
            if needle in str(info.get("title", ""))
        ]

    def window_pid(self, hwnd: int) -> int:
        info = self.windows.get(hwnd)
        if not info:
            return 0
        return int(info.get("native_pid", info.get("pid", 0)) or 0)

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def get_title(self, hwnd: int) -> str:
        info = self.windows.get(hwnd)
        return str(info["title"]) if info else ""

    def set_title(self, hwnd: int, title: str) -> bool:
        if hwnd in self.windows:
            self.windows[hwnd]["title"] = title
        self.set_calls.append((hwnd, title))
        return True

    def process_alive(self, pid: int) -> bool:
        return bool(self.alive.get(pid, False))

    def process_token(self, pid: int) -> int:
        return int(self.tokens.get(pid, 0))

    def start(self, on_name_change) -> bool:
        self.hook_started += 1
        if not self.event_driven:
            return False
        self.hook_callback = on_name_change
        return True

    def stop(self) -> None:
        self.hook_stopped += 1
        self.hook_callback = None

    def rename(self, hwnd: int, title: str) -> None:
        """External drift: the OS posts a name change for a managed window."""
        self.windows[hwnd]["title"] = title
        if self.hook_callback is not None:
            self.hook_callback(hwnd)


def guardian_with_instance(
    *,
    pid: int = 100,
    hwnd: int = 5001,
    canonical: str = CANONICAL_A,
    token: str = "OC-aaa111",
    initial: str = GENERIC_TITLE,
    event_driven: bool = True,
):
    backend = FakeTitleBackend(event_driven=event_driven)
    backend.add_window(hwnd, pid, initial)
    guardian = TitleGuardian(backend=backend)
    assert guardian.register(
        pid, canonical, correlation_token=token, launcher_id="opencode", project_id="audapack"
    )
    return guardian, backend, hwnd


# ---------------------------------------------------------------------------
# 1-2. launch assignment and exact HWND association
# ---------------------------------------------------------------------------

def test_canonical_title_is_assigned_at_launch():
    guardian, backend, hwnd = guardian_with_instance()
    assert backend.get_title(hwnd) == CANONICAL_A
    assert guardian.restores == 1
    assert guardian.registration(100).correlation_token == "OC-aaa111"
    assert guardian.active_count == 1


def test_exact_hwnd_is_associated_with_the_managed_instance():
    guardian, backend, hwnd = guardian_with_instance()
    assert guardian.hwnd_for_pid(100) == hwnd
    # An unrelated window (another process, other titles) is never touched.
    backend.add_window(9001, 900, "Administrator: Windows PowerShell")
    assert guardian.hwnd_for_pid(900) == 0
    backend.rename(9001, "still unrelated")
    assert backend.get_title(9001) == "still unrelated"
    assert all(call[0] != 9001 for call in backend.set_calls)


# ---------------------------------------------------------------------------
# 3-5. drift restoration: arbitrary, generic PowerShell, OpenCode dynamic
# ---------------------------------------------------------------------------

def test_arbitrary_external_title_drift_is_restored():
    guardian, backend, hwnd = guardian_with_instance()
    backend.rename(hwnd, "Some Other App - untitled")
    assert backend.get_title(hwnd) == CANONICAL_A
    assert guardian.active_count == 1


def test_generic_powershell_title_is_restored():
    guardian, backend, hwnd = guardian_with_instance()
    backend.rename(hwnd, GENERIC_TITLE)
    assert backend.get_title(hwnd) == CANONICAL_A
    backend.rename(hwnd, "Administrator: Windows PowerShell - OpenCode")
    assert backend.get_title(hwnd) == CANONICAL_A


def test_opencode_dynamic_title_is_restored():
    guardian, backend, hwnd = guardian_with_instance()
    backend.rename(hwnd, "OC | refactor the launcher")
    assert backend.get_title(hwnd) == CANONICAL_A
    backend.rename(hwnd, "OpenCode")
    assert backend.get_title(hwnd) == CANONICAL_A


# ---------------------------------------------------------------------------
# 6-7. Ctrl+C / Ctrl+V interactive churn
# ---------------------------------------------------------------------------

def test_ctrl_c_equivalent_title_transition_is_not_permanent():
    guardian, backend, hwnd = guardian_with_instance()
    # The PowerShell host rewrites the title on interrupt/console transitions.
    backend.rename(hwnd, GENERIC_TITLE)
    assert backend.get_title(hwnd) == CANONICAL_A
    backend.rename(hwnd, r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\powershell.exe")
    assert backend.get_title(hwnd) == CANONICAL_A
    backend.rename(hwnd, GENERIC_TITLE)
    assert backend.get_title(hwnd) == CANONICAL_A


def test_paste_path_does_not_break_ownership():
    guardian, backend, hwnd = guardian_with_instance()
    # Our own restoration emits a name-change event whose title already
    # matches canonical -- it must be a no-op, never a rewrite loop.
    calls_before = len(backend.set_calls)
    backend.rename(hwnd, CANONICAL_A)
    assert len(backend.set_calls) == calls_before
    # Ctrl+V / control-key churn does not break the association.
    backend.rename(hwnd, GENERIC_TITLE)
    assert backend.get_title(hwnd) == CANONICAL_A
    assert guardian.hwnd_for_pid(100) == hwnd
    assert guardian.active_count == 1


# ---------------------------------------------------------------------------
# 8. focus/reuse after drift
# ---------------------------------------------------------------------------

class FakeMonitorBackend:
    def __init__(self, windows=None):
        self.windows = list(windows or [])
        self.alive: dict[int, bool] = {}
        self.tokens: dict[int, int] = {}
        self.focused: list[int] = []

    def list_windows(self):
        return list(self.windows)

    def process_alive(self, pid):
        return self.alive.get(pid, False)

    def process_token(self, pid):
        return self.tokens.get(pid, 0)

    def focus_window(self, hwnd):
        self.focused.append(hwnd)
        return True

    def close_window(self, hwnd):
        return True

    def arrange_windows(self, hwnds, mode):
        return len(list(hwnds))


def test_repeated_click_focuses_exact_instance_after_title_drift(tmp_path: Path):
    proj = Project(id="pa", display_name="Project A", source_path=r"V:\code\a", priority_group="MAIN0", slot=1)
    record_pid = 4242
    hwnd = 777
    # The visible console belongs to conhost (different PID) and its title has
    # already drifted; the correlation token lives in the command line.
    window = NativeWindow(
        hwnd, 9999, GENERIC_TITLE, "conhost.exe",
        r'powershell.exe -Command "$managedTitle = AUDAPACK | OpenCode YOLO | V:\code\a | OC-aaa111"',
    )
    backend = FakeMonitorBackend([window])
    backend.alive[record_pid] = True
    backend.tokens[record_pid] = 1111
    monitor = InstanceMonitor(backend=backend, record_path=Path(tmp_path) / "instances.json")
    monitor.track_launch(record_pid, "opencode", proj, correlation_token="OC-aaa111")
    monitor.refresh([proj], [])
    candidate = monitor.focus_candidate("pa", "opencode")
    assert candidate is not None
    assert candidate.hwnd == hwnd
    assert monitor.focus(candidate) is True
    assert backend.focused == [hwnd]
    # A second normal click focuses the same instance; no duplicate is born.
    again = monitor.focus_candidate("pa", "opencode")
    assert again is not None and again.hwnd == hwnd


def test_same_pid_drifted_title_still_focuses_exact_instance(tmp_path: Path):
    proj = Project(id="pa", display_name="Project A", source_path=r"V:\code\a", priority_group="MAIN0", slot=1)
    hwnd = 888
    window = NativeWindow(hwnd, 4242, GENERIC_TITLE, "powershell.exe")
    backend = FakeMonitorBackend([window])
    backend.alive[4242] = True
    backend.tokens[4242] = 1111
    monitor = InstanceMonitor(backend=backend, record_path=Path(tmp_path) / "instances.json")
    monitor.track_launch(4242, "opencode", proj, correlation_token="OC-aaa111")
    monitor.refresh([proj], [])
    candidate = monitor.focus_candidate("pa", "opencode")
    assert candidate is not None and candidate.hwnd == hwnd


# ---------------------------------------------------------------------------
# 9-11. multi-instance identity
# ---------------------------------------------------------------------------

def test_shift_click_second_instance_has_independent_title_and_restore():
    backend = FakeTitleBackend()
    backend.add_window(5001, 100, GENERIC_TITLE)
    backend.add_window(5002, 101, GENERIC_TITLE)
    guardian = TitleGuardian(backend=backend)
    guardian.register(100, CANONICAL_A, correlation_token="OC-aaa111", launcher_id="opencode")
    guardian.register(101, CANONICAL_B, correlation_token="OC-bbb222", launcher_id="opencode")
    assert backend.get_title(5001) == CANONICAL_A
    assert backend.get_title(5002) == CANONICAL_B
    backend.rename(5001, GENERIC_TITLE)
    assert backend.get_title(5001) == CANONICAL_A
    assert backend.get_title(5002) == CANONICAL_B
    backend.rename(5002, GENERIC_TITLE)
    assert backend.get_title(5002) == CANONICAL_B


def test_two_same_project_instances_remain_distinguishable_by_token():
    guardian, backend, hwnd_a = guardian_with_instance(canonical=CANONICAL_A, token="OC-aaa111")
    backend.add_window(5002, 101, GENERIC_TITLE)
    guardian.register(101, CANONICAL_B, correlation_token="OC-bbb222", launcher_id="opencode")
    assert "OC-aaa111" in backend.get_title(hwnd_a)
    assert "OC-bbb222" in backend.get_title(5002)
    titles = {backend.get_title(hwnd_a), backend.get_title(5002)}
    assert len(titles) == 2


def test_five_projects_remain_distinguishable():
    backend = FakeTitleBackend()
    guardian = TitleGuardian(backend=backend)
    titles = {}
    for index, name in enumerate(["AUDAPACK", "SAIPEN", "SAIMAIL", "LIMISAW", "FastPrompter"]):
        pid = 100 + index
        hwnd = 5001 + index
        canonical = f"{name} | OpenCode YOLO | V:\\code\\{name.lower()} | OC-{index:06d}"
        backend.add_window(hwnd, pid, GENERIC_TITLE, token=2000 + index)
        guardian.register(pid, canonical, correlation_token=f"OC-{index:06d}", launcher_id="opencode")
        titles[hwnd] = canonical
    assert len(set(titles.values())) == 5
    # Every window drifts to the generic host title at once; each is restored
    # to ITS OWN canonical identity, never to a shared one.
    for hwnd in titles:
        backend.rename(hwnd, GENERIC_TITLE)
    for hwnd, canonical in titles.items():
        assert backend.get_title(hwnd) == canonical
    assert guardian.restores == 10
    assert guardian.active_count == 5


# ---------------------------------------------------------------------------
# 12-13. lifecycle cleanup and reuse safety
# ---------------------------------------------------------------------------

def test_instance_exit_removes_title_ownership():
    guardian, backend, hwnd = guardian_with_instance()
    backend.alive[100] = False
    assert guardian.retire_dead() == 1
    assert guardian.active_count == 0
    assert guardian.hwnd_for_pid(100) == 0
    backend.rename(hwnd, "recycled console")
    assert backend.get_title(hwnd) == "recycled console"


def test_hwnd_pid_reuse_cannot_rename_unrelated_future_window():
    guardian, backend, hwnd = guardian_with_instance()
    # The old instance exits; Windows reuses its PID for an unrelated process
    # and its HWND for an unrelated window within the stale registration's life.
    backend.alive[100] = False
    backend.tokens[100] = 9999
    backend.windows[hwnd] = {"pid": 100, "title": "unrelated future window"}
    backend.windows[6000] = {"pid": 100, "title": "another future window"}
    # Late events for the reused handles are refused: the process creation
    # token no longer matches the generation AUDAPACK registered.
    backend.hook_callback(hwnd)
    backend.hook_callback(6000)
    assert backend.get_title(hwnd) == "unrelated future window"
    assert backend.get_title(6000) == "another future window"
    assert guardian.retire_dead() == 1
    assert guardian.active_count == 0


def test_registration_generation_never_revives_a_retired_instance():
    guardian, backend, hwnd = guardian_with_instance()
    assert guardian.unregister(100) is True
    assert guardian.register(
        100, CANONICAL_B, correlation_token="OC-bbb222", launcher_id="opencode",
    ) is True
    assert backend.get_title(hwnd) == CANONICAL_B
    # A late callback from the retired generation resolves to the CURRENT
    # registration only after process identity is re-verified; the restored
    # title is the current canonical one, never a stale generation's.
    backend.rename(hwnd, GENERIC_TITLE)
    assert backend.get_title(hwnd) == CANONICAL_B


# ---------------------------------------------------------------------------
# 14. no polling while zero instances; bounded fallback only when needed
# ---------------------------------------------------------------------------

def test_event_hook_is_lazy_and_stops_when_the_last_instance_exits():
    backend = FakeTitleBackend()
    guardian = TitleGuardian(backend=backend)
    assert backend.hook_started == 0, "no hook thread before any managed instance"
    assert guardian.uses_fallback_polling is False
    guardian.register(100, CANONICAL_A, correlation_token="OC-aaa111")
    assert backend.hook_started == 1
    assert guardian.event_driven is True
    assert guardian.fallback_interval_ms == 0
    assert guardian.uses_fallback_polling is False
    guardian.unregister(100)
    assert backend.hook_stopped == 1, "the hook is released when the last instance exits"
    assert guardian.uses_fallback_polling is False


def test_backend_start_failure_uses_only_the_bounded_fallback():
    backend = FakeTitleBackend(event_driven=False)
    backend.add_window(5001, 100, GENERIC_TITLE)
    guardian = TitleGuardian(backend=backend)
    guardian.register(100, CANONICAL_A, correlation_token="OC-aaa111")
    assert guardian.uses_fallback_polling is True
    assert guardian.fallback_interval_ms == FALLBACK_INTERVAL_MS >= 5000
    # Nothing is renamed by the failed start; the sweep restores actual drift
    # and is a no-op when titles are stable.
    backend.rename(5001, GENERIC_TITLE)
    assert backend.get_title(5001) == GENERIC_TITLE
    assert guardian.sweep() == 1
    assert backend.get_title(5001) == CANONICAL_A
    assert guardian.sweep() == 0
    guardian.unregister(100)
    assert guardian.uses_fallback_polling is False


def test_null_backend_is_a_truthful_noop():
    guardian = TitleGuardian(backend=NullTitleBackend())
    assert guardian.register(100, CANONICAL_A, correlation_token="OC-aaa111") is True
    assert guardian.active_count == 1
    assert guardian.uses_fallback_polling is True
    assert guardian.sweep() == 0  # no window can be witnessed or renamed
    assert guardian.active_count == 0  # liveness retired the unwitnessed instance
    guardian.shutdown()


def test_shutdown_releases_the_hook_and_registrations():
    guardian, backend, _hwnd = guardian_with_instance()
    guardian.shutdown()
    assert guardian.active_count == 0
    assert backend.hook_stopped == 1
    assert guardian.event_driven is False


# ---------------------------------------------------------------------------
# 15-20. T-209 escaped regression: lifetime truth after real Ctrl/Ctrl+V/Ctrl+C
# ---------------------------------------------------------------------------

def test_unknown_hwnd_event_only_requests_authority_never_scans():
    """T-216 TARGET C/Q: an unmapped name-change event is CHEAP.

    The pre-T-216 guardian answered an unknown HWND by scanning the desktop
    from inside the WinEvent callback (`_adopt_unknown_hwnd` ->
    `_resolve_for` -> `EnumWindows`) while holding guardian synchronization.
    That is exactly what made the GUI freeze. Now the callback only marks the
    authority request and records the observed handle; InstanceMonitor resolves
    ownership asynchronously and feeds the proof back through `bind_hwnd`.
    """
    backend = FakeTitleBackend()
    guardian = TitleGuardian(backend=backend)
    assert guardian.register(
        100, CANONICAL_A, correlation_token="OC-aaa111", launcher_id="opencode"
    )
    assert guardian.hwnd_for_pid(100) == 0, "no window existed at registration"
    assert guardian.event_driven is True

    # 100 unknown-HWND events: zero desktop scans, zero title writes, one
    # coalesced authority request.
    before = backend.resolve_calls
    for _ in range(100):
        backend.hook_callback(7001)
    assert backend.resolve_calls == before, "an unknown event must not scan the desktop"
    assert guardian.binding(7001) is None, "an unmapped event grants no ownership"
    assert guardian.take_hwnd_authority_request() is True
    assert guardian.take_hwnd_authority_request() is False, "the request coalesces"
    assert guardian.observed_hwnds() == (7001,)

    # The console window appears; InstanceMonitor proves it; the guardian adopts
    # the proven association and restores its title.
    backend.add_window(7001, 100, GENERIC_TITLE, native_pid=900)
    assert guardian.bind_hwnd(100, 7001, correlation_token="OC-aaa111", native_pid=900) is True
    assert backend.get_title(7001) == CANONICAL_A
    assert guardian.hwnd_for_pid(100) == 7001
    assert guardian.binding(7001) is not None

    # Once learned, the same event is the O(1) fast path.
    backend.rename(7001, GENERIC_TITLE)
    assert backend.get_title(7001) == CANONICAL_A
    assert guardian.binding_count == 1


def test_heartbeat_repairs_drift_when_every_event_is_missed():
    """Even with a zero-event hook, the title repairs within one interval."""
    guardian, backend, hwnd = guardian_with_instance()
    backend.windows[hwnd]["title"] = GENERIC_TITLE  # silent drift: no callback
    assert backend.get_title(hwnd) == GENERIC_TITLE
    assert guardian.heartbeat() == 1
    assert backend.get_title(hwnd) == CANONICAL_A
    assert guardian.heartbeat() == 0, "a stable title is never rewritten"


def test_heartbeat_is_armed_only_while_instances_exist():
    backend = FakeTitleBackend()
    guardian = TitleGuardian(backend=backend)
    assert guardian.heartbeat_interval_ms == 0
    assert backend.hook_started == 0
    guardian.register(100, CANONICAL_A, correlation_token="OC-aaa111")
    assert guardian.heartbeat_interval_ms == HEARTBEAT_INTERVAL_MS == 1000
    guardian.unregister(100)
    assert guardian.heartbeat_interval_ms == 0, "zero instances means zero wakeups"
    assert backend.hook_stopped == 1
    assert guardian.binding_count == 0


def test_one_hundred_repeated_drift_cycles_always_restore_without_accumulation():
    """The required stress scenario: 100 forced drifts, 100 restorations."""
    guardian, backend, hwnd = guardian_with_instance()
    for index in range(100):
        backend.windows[hwnd]["title"] = f"Administrator: Windows PowerShell {index}"
        assert guardian.heartbeat() == 1
        assert backend.get_title(hwnd) == CANONICAL_A
    assert guardian.restores == 101  # the launch assignment plus 100 repairs
    assert guardian.active_count == 1
    assert guardian.binding_count == 1
    assert backend.hook_started == 1, "no timer/hook accumulation"
    assert backend.hook_stopped == 0


def test_ctrl_modifier_churn_repeatedly_recovers_on_several_projects():
    """Ctrl, Ctrl+V, Ctrl+C and OpenCode redraws, repeatedly, per project."""
    backend = FakeTitleBackend()
    guardian = TitleGuardian(backend=backend)
    canonical: dict[int, str] = {}
    for index, name in enumerate(["AUDAPACK", "SAIPEN", "SAIMAIL", "LIMISAW"]):
        pid = 100 + index
        hwnd = 5001 + index
        title = f"{name} | OpenCode YOLO | V:\\code\\{name.lower()} | OC-{index:06d}"
        backend.add_window(hwnd, pid, GENERIC_TITLE, native_pid=900 + index, token=2000 + index)
        guardian.register(pid, title, correlation_token=f"OC-{index:06d}", launcher_id="opencode")
        canonical[hwnd] = title
    for _round in range(3):
        for hwnd in canonical:
            backend.rename(hwnd, GENERIC_TITLE)
            backend.rename(hwnd, "OC | session")
            backend.rename(hwnd, r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\powershell.exe")
        for hwnd, title in canonical.items():
            assert backend.get_title(hwnd) == title
    assert guardian.active_count == 4


def test_bind_hwnd_reuses_the_monitor_proof_and_rejects_a_foreign_launch():
    """InstanceMonitor already proved launch+hwnd+token; the guardian consumes it."""
    backend = FakeTitleBackend()
    # The OS resolver cannot see this console at all; only the monitor knows.
    backend.add_window(7002, 100, GENERIC_TITLE, native_pid=901, resolvable=False)
    guardian = TitleGuardian(backend=backend)
    assert guardian.register(
        100, CANONICAL_A, correlation_token="OC-aaa111", launcher_id="opencode"
    )
    assert guardian.hwnd_for_pid(100) == 0
    assert backend.get_title(7002) == GENERIC_TITLE

    assert guardian.bind_hwnd(
        100, 7002, correlation_token="OC-aaa111", native_pid=901
    ) is True
    assert backend.get_title(7002) == CANONICAL_A
    assert guardian.binding(7002).native_pid == 901

    # A different launch can never steal the proven window.
    backend.add_window(5003, 101, GENERIC_TITLE)
    guardian.register(101, CANONICAL_B, correlation_token="OC-bbb222", launcher_id="opencode")
    assert guardian.bind_hwnd(101, 7002, correlation_token="OC-bbb222") is False
    assert guardian.binding(7002).launch_pid == 100

    # Drift is repaired through both the event path and the heartbeat even
    # though the OS resolver still cannot witness the window.
    backend.rename(7002, GENERIC_TITLE)
    assert backend.get_title(7002) == CANONICAL_A
    backend.windows[7002]["title"] = GENERIC_TITLE
    assert guardian.heartbeat() == 1
    assert backend.get_title(7002) == CANONICAL_A


def test_retired_generation_never_renames_a_reused_console_hwnd():
    guardian, backend, hwnd = guardian_with_instance()
    backend.alive[100] = False
    backend.rename(hwnd, "unrelated future window")
    assert backend.get_title(hwnd) == "unrelated future window"
    assert guardian.heartbeat() == 0
    assert guardian.active_count == 0
    assert guardian.binding(hwnd) is None, "ownership is dropped with the launcher"


# ---------------------------------------------------------------------------
# 21-24. T-210 process safety: the host's own console state is off limits
# ---------------------------------------------------------------------------

#: APIs that mutate the CALLING PROCESS' console association and standard
#: handles. AttachConsole repopulates them; FreeConsole leaves them stranded, so
#: the next capture_output CreateProcess fails with [WinError 6].
FORBIDDEN_CONSOLE_APIS = frozenset({"AttachConsole", "FreeConsole", "GetConsoleWindow"})


def _process_global_console_references(source: str) -> list[str]:
    """AST-walk the module for real references, ignoring its own documentation."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CONSOLE_APIS:
            hits.append(f"line {node.lineno}: attribute {node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CONSOLE_APIS:
            hits.append(f"line {node.lineno}: name {node.id}")
    return hits


def test_production_title_backend_never_calls_process_global_console_apis():
    """T-210 TARGET A as a static proof, not a promise.

    The module may document these APIs (that history is worth keeping), but no
    executable reference may remain. A regression that reintroduces one fails
    here instead of in production, where it costs a real launch.
    """
    source = (
        Path(__file__).resolve().parents[1] / "audapack" / "title_guardian.py"
    ).read_text(encoding="utf-8")
    assert _process_global_console_references(source) == []
    # And the guard itself is honest about which side of the boundary it is on.
    from audapack.title_guardian import Win32TitleBackend

    assert Win32TitleBackend.PROCESS_SAFE_WINDOW_DISCOVERY is True


def test_token_correlation_adopts_a_window_the_pid_scan_cannot_see():
    """A console hosted by another process is still adoptable, safely.

    This is the shape the old code needed AttachConsole for. AUDAPACK's own
    unguessable token in the caption is mechanical identity (the same proof
    InstanceMonitor uses), and the scan is read-only.
    """
    backend = FakeTitleBackend()
    backend.add_window(
        7100,
        100,
        "AUDAPACK | OpenCode YOLO | V:\\code\\audapack | OC-aaa111",
        native_pid=900,
        resolvable=False,
    )
    guardian = TitleGuardian(backend=backend)
    assert guardian.register(
        100, CANONICAL_A, correlation_token="OC-aaa111", launcher_id="opencode"
    )
    assert guardian.hwnd_for_pid(100) == 7100
    assert backend.get_title(7100) == CANONICAL_A
    assert guardian.binding(7100).native_pid == 900


def test_no_registration_is_adopted_from_a_foreign_token():
    """Only OUR token adopts; another project's console is never taken over."""
    backend = FakeTitleBackend()
    backend.add_window(
        7101, 101, "SAIPEN | OpenCode YOLO | V:\\code\\saipen | OC-bbb222",
        native_pid=901, resolvable=False,
    )
    guardian = TitleGuardian(backend=backend)
    assert guardian.register(
        100, CANONICAL_A, correlation_token="OC-aaa111", launcher_id="opencode"
    )
    assert guardian.hwnd_for_pid(100) == 0
    assert backend.get_title(7101).startswith("SAIPEN")
    assert guardian.binding_count == 0


def test_needs_hwnd_authority_only_while_a_live_binding_is_missing():
    """The InstanceMonitor is asked for authority only when it is needed."""
    backend = FakeTitleBackend()
    backend.add_window(7002, 100, GENERIC_TITLE, native_pid=901, resolvable=False)
    guardian = TitleGuardian(backend=backend)
    guardian.register(100, CANONICAL_A, correlation_token="OC-aaa111", launcher_id="opencode")
    assert guardian.needs_hwnd_authority is True

    assert guardian.bind_hwnd(100, 7002, native_pid=901) is True
    assert guardian.needs_hwnd_authority is False

    backend.windows.pop(7002)
    assert guardian.needs_hwnd_authority is True

    guardian.unregister(100)
    assert guardian.needs_hwnd_authority is False
