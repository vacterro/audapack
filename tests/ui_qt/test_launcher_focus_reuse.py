"""T-179: a normal launcher click focuses the exact existing instance.

A normal click means "put that agent in front of me": it focuses exactly this
launcher for exactly this project and launches only when there is none.
Shift+click asks for another instance but still respects ``max_instances``.
C2 never focuses C1, Project A's OpenCode never answers for Project B, and a
live-but-unfocusable instance is reported instead of being silently duplicated.
"""

from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QRect
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStyleOptionViewItem

from audapack.config import AppConfig, AuditsConfig, create_default_launchers
from audapack.instances import InstanceMonitor, NativeWindow, WindowInstance
from audapack.models import Project
from audapack.opencode_launch import LaunchAdmissionError, OpenCodeAdmission
from audapack.services.project_service import ProjectService
from audapack.title_guardian import TitleGuardian
from audapack.ui_qt.dialogs.instance_manager import InstanceManagerWidget
from audapack.ui_qt.main_window import MainWindow
from audapack.ui_qt.models.project_delegate import (
    ProjectItemDelegate,
    compute_actions_left,
    compute_row_button_rects,
)
from audapack.ui_qt.theme.golden_default import PALETTE


class FakeWindowBackend:
    """In-memory native backend. No Win32 call ever leaves the process."""

    def __init__(self, windows=None):
        self.windows = list(windows or [])
        self.alive: dict[int, bool] = {}
        self.tokens: dict[int, int] = {}
        self.focused: list[int] = []
        self.closed: list[int] = []
        self.arranged: list[tuple[list[int], str]] = []
        self.focus_result = True
        self.on_focus = None
        self.scan_calls = 0

    def list_windows(self):
        self.scan_calls += 1
        return list(self.windows)

    def process_alive(self, pid):
        return self.alive.get(pid, False)

    def process_token(self, pid):
        return self.tokens.get(pid, 0)

    def focus_window(self, hwnd):
        self.focused.append(hwnd)
        if self.on_focus is not None:
            self.on_focus(hwnd)
        return self.focus_result

    def close_window(self, hwnd):
        self.closed.append(hwnd)
        return True

    def arrange_windows(self, hwnds, mode):
        values = list(hwnds)
        self.arranged.append((values, mode))
        return len(values)


def project(project_id: str, name: str, path: str, slot: int = 1) -> Project:
    return Project(
        id=project_id,
        display_name=name,
        source_path=path,
        priority_group="MAIN0",
        slot=slot,
    )


def tracked_window(hwnd: int, pid: int, title: str, launcher_id: str, proj: Project, monitor: InstanceMonitor):
    monitor.track_launch(pid, launcher_id, proj)
    return NativeWindow(hwnd, pid, title, "powershell.exe")


def make_window(
    tmp_path,
    projects,
    *,
    launchers=None,
    windows=None,
    backend=None,
):
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=list(projects),
        launchers=list(launchers) if launchers is not None else create_default_launchers(),
    )
    config.ui.compact_rows = True
    service = ProjectService(config, base_dir=tmp_path)
    win = MainWindow(service)
    backend = backend or FakeWindowBackend(windows)
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    win._instance_monitor = monitor
    win._instance_manager.monitor = monitor
    # T-205: never install a real global title hook from a test process; the
    # ownership contract is exercised through the in-memory backend instead.
    win._title_guardian = TitleGuardian(backend=FakeTitleBackend())
    win._title_guardian_timer = None
    return win, backend, monitor


# ---------------------------------------------------------------------------
# 1. first normal click
# ---------------------------------------------------------------------------

def test_first_normal_click_launches_exactly_once(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win, _backend, _monitor = make_window(tmp_path, [pa])
    try:
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 1
        assert launch.call_args.args[0] is pa
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 2. second normal click focuses, does not launch
# ---------------------------------------------------------------------------

def test_second_normal_click_focuses_and_never_launches(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor)]
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 0
        assert backend.focused == [11]
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 3. STARTING blocks the duplicate
# ---------------------------------------------------------------------------

def test_a_starting_instance_blocks_the_second_click(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[77] = True
    backend.tokens[77] = 3
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        monitor.track_launch(77, "opencode", pa)
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 0
        assert backend.focused == []
        assert "already starting" in win.statusBar().currentMessage()
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 4. focus failure, instance still live -> report, never spawn
# ---------------------------------------------------------------------------

def test_a_live_but_unfocusable_instance_is_reported_not_duplicated(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    backend.focus_result = False
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor)]
        win._refresh_instance_snapshot()
        with (
            patch.object(win, "_on_open_with_opencode") as launch,
            patch.object(win, "_show_instance_manager") as manager,
        ):
            win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 0
        assert manager.call_count == 1
        assert "would not come to the front" in win.statusBar().currentMessage()
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 5. focus failure, instance actually gone -> launch may proceed
# ---------------------------------------------------------------------------

def test_a_vanished_instance_releases_the_click_to_launch(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    backend.focus_result = False
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor)]

        def vanish(_hwnd):
            backend.windows = []
            backend.alive[44] = False

        backend.on_focus = vanish
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 1
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 6. Shift+click asks for another instance
# ---------------------------------------------------------------------------

def test_shift_click_launches_a_second_instance(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor)]
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pa, "opencode", force_new=True)
        assert launch.call_count == 1
        assert backend.focused == []
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 7. Shift still respects max_instances
# ---------------------------------------------------------------------------

def test_shift_click_still_respects_the_launcher_limit(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    launchers = create_default_launchers()
    # T-230: FreeBuff ships unlimited now; pin the generic capacity rule with an
    # explicit operator-set limit instead of the retired product default.
    next(lc for lc in launchers if lc.id == "freebuff").max_instances = 1
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], launchers=launchers, backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, r"Project A | FreeBuff | V:\code\a", "freebuff", pa, monitor)]
        win._refresh_instance_snapshot()
        with (
            patch.object(win, "_on_open_with_freebuff") as launch,
            patch.object(win, "_show_instance_manager") as manager,
        ):
            win._on_open_with_launcher(pa, "freebuff", force_new=True)
        assert launch.call_count == 0
        assert manager.call_count == 1
        assert "Launch blocked" in win.statusBar().currentMessage()
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 8. exact launcher: C2 must not focus C1
# ---------------------------------------------------------------------------

def test_a_sibling_launcher_never_focuses_another_codex_account(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [
            tracked_window(11, 44, r"Project A | Codex (main_codex) | V:\code\a", "main_codex", pa, monitor)
        ]
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_codex") as launch:
            win._on_open_with_launcher(pa, "main_codex2")
        assert launch.call_count == 1
        assert launch.call_args.args == (pa, "main_codex2")
        assert backend.focused == []
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 9. exact project: B's OpenCode must not focus A's
# ---------------------------------------------------------------------------

def test_another_projects_launcher_never_satisfies_this_click(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    pb = project("pb", "Project B", r"V:\code\b", slot=2)
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa, pb], backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor)]
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pb, "opencode")
        assert launch.call_count == 1
        assert backend.focused == []
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 10. multiple deliberate instances: deterministic focus, never a third
# ---------------------------------------------------------------------------

def test_two_deliberate_instances_are_focused_deterministically(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    backend.alive[55] = True
    backend.tokens[55] = 2
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [
            tracked_window(11, 44, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor),
            tracked_window(22, 55, r"Project A | OpenCode | V:\code\a", "opencode", pa, monitor),
        ]
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_open_with_launcher(pa, "opencode")
            win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 0
        assert backend.focused == [22, 22]
    finally:
        win.close()


# ---------------------------------------------------------------------------
# Launcher button state: highlight without geometry growth
# ---------------------------------------------------------------------------

def _running_instance(project_id: str, launcher_id: str) -> WindowInstance:
    return WindowInstance(
        hwnd=101, pid=1001, title="Project A | OpenCode", process_name="powershell.exe",
        launcher_id=launcher_id, launcher_name="OpenCode", project_id=project_id,
        project_name="Project A", project_path=r"V:\code\a", state="running", tracked=True,
    )


def _starting_instance(project_id: str, launcher_id: str) -> WindowInstance:
    return WindowInstance(
        hwnd=0, pid=1002, title="Starting", process_name="",
        launcher_id=launcher_id, launcher_name="OpenCode", project_id=project_id,
        project_name="Project A", project_path=r"V:\code\a", state="starting", tracked=True,
    )


def _paint_row(win, index, width: int = 620):
    pixmap = QPixmap(width, 22)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, width, 22)
        win.delegate.paint(painter, option, index)
    finally:
        painter.end()
    return pixmap


def _opencode_button(win, width: int = 620):
    rect = QRect(0, 0, width, 22)
    buttons, _gg = compute_row_button_rects(rect, win._service.config.launchers)
    return next(rect for launcher, rect in buttons if launcher.id == "opencode")


def test_launcher_state_never_moves_the_action_boundary(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win, _backend, monitor = make_window(tmp_path, [pa])
    try:
        index = win.model.index_for_project_id("pa")
        rect = QRect(0, 0, 620, 22)
        baseline_actions = compute_actions_left(rect, win._service.config.launchers)
        baseline_buttons = compute_row_button_rects(rect, win._service.config.launchers)[0]

        monitor.instances = [_running_instance("pa", "opencode")]
        assert compute_actions_left(rect, win._service.config.launchers) == baseline_actions
        assert compute_row_button_rects(rect, win._service.config.launchers)[0] == baseline_buttons

        monitor.instances = [_starting_instance("pa", "opencode")]
        assert compute_actions_left(rect, win._service.config.launchers) == baseline_actions
        assert compute_row_button_rects(rect, win._service.config.launchers)[0] == baseline_buttons

        _paint_row(win, index)
    finally:
        win.close()


def test_running_starting_and_none_paint_distinct_surfaces(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win, _backend, monitor = make_window(tmp_path, [pa])
    try:
        index = win.model.index_for_project_id("pa")
        button = _opencode_button(win)
        sample = (button.left() + 3, button.top() + 3)

        monitor.instances = []
        none_color = _paint_row(win, index).toImage().pixelColor(*sample)

        monitor.instances = [_running_instance("pa", "opencode")]
        running_color = _paint_row(win, index).toImage().pixelColor(*sample)

        monitor.instances = [_starting_instance("pa", "opencode")]
        starting_color = _paint_row(win, index).toImage().pixelColor(*sample)

        assert running_color.name() == PALETTE["accentTealDeep"].lower()
        assert starting_color.name() == PALETTE["warning"].lower()
        assert none_color.name() == PALETTE["surfaceRaised"].lower()
    finally:
        win.close()


def test_launcher_state_paint_performs_no_native_scan(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win, backend, monitor = make_window(tmp_path, [pa])
    try:
        index = win.model.index_for_project_id("pa")
        monitor.instances = [_running_instance("pa", "opencode")]

        def forbidden():
            raise AssertionError("delegate paint must not scan native windows")

        backend.list_windows = forbidden
        _paint_row(win, index)
    finally:
        win.close()


# ---------------------------------------------------------------------------
# Instances tab visibility
# ---------------------------------------------------------------------------

def test_instance_manager_shows_fallback_agents_with_their_real_project(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend(
        [
            NativeWindow(1, 10, r"Project A | OpenCode | V:\code\a", "powershell.exe"),
            NativeWindow(2, 20, r"Project A | Claude Code | V:\code\a", "powershell.exe"),
            NativeWindow(3, 30, r"Project A | ZCode | V:\code\a", "powershell.exe"),
        ]
    )
    service = ProjectService(
        AppConfig(projects=[pa], launchers=create_default_launchers()), base_dir=tmp_path
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.refresh([pa], service.config.launchers)

    widget = InstanceManagerWidget(monitor, service, pa)
    try:
        shown = {
            widget.table.item(row, 1).text(): widget.table.item(row, 2).text()
            for row in range(widget.table.rowCount())
        }
        assert shown == {"OpenCode": "Project A", "Claude Code": "Project A", "ZCode": "Project A"}
    finally:
        widget.close()


def test_tracked_and_external_instances_are_marked_truthfully(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend(
        [
            NativeWindow(1, 10, r"Project A | OpenCode | V:\code\a", "powershell.exe"),
            NativeWindow(2, 20, r"Project A | Claude Code | V:\code\a", "powershell.exe"),
        ]
    )
    backend.alive[10] = True
    backend.tokens[10] = 1
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.track_launch(10, "opencode", pa)

    instances = monitor.refresh([pa], create_default_launchers())

    tracked = next(item for item in instances if item.launcher_id == "opencode")
    external = next(item for item in instances if item.launcher_id == "claude")
    assert tracked.tracked is True
    assert external.tracked is False


# ---------------------------------------------------------------------------
# Project info agent section
# ---------------------------------------------------------------------------

def test_project_info_reports_agents_and_origin():
    pa = project("pa", "Project A", r"V:\code\a")
    hover_info = {
        "project": pa,
        "group": "MAIN0",
        "slot": 1,
        "group_count": 1,
        "launcher_instances": [
            {
                "launcher_id": "claude",
                "launcher_name": "Claude Code",
                "state": "running",
                "pid": 4242,
                "tracked": False,
            },
            {
                "launcher_id": "opencode",
                "launcher_name": "OpenCode",
                "state": "starting",
                "pid": 4343,
                "tracked": True,
            },
        ],
    }

    html = ProjectItemDelegate.build_tooltip(hover_info)

    assert "Claude Code: running" in html
    assert "PID 4242" in html
    assert "(external)" in html
    assert "OpenCode: starting" in html
    assert "(AUDAPACK)" in html


def test_launcher_states_is_an_in_memory_read(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win, backend, monitor = make_window(tmp_path, [pa])
    try:
        monitor.instances = [_running_instance("pa", "opencode")]

        def forbidden():
            raise AssertionError("launcher_states must not scan native windows")

        backend.list_windows = forbidden
        assert monitor.launcher_states("pa") == {"opencode": "running"}
    finally:
        win.close()


def _managed(*roots):
    """A SAIPEN-managed root: the only kind a click sends through Fleet preflight."""
    for root in roots:
        (Path(root) / ".saipen").mkdir(parents=True, exist_ok=True)
        (Path(root) / ".saipen" / "IDENTITY.md").write_text("id", encoding="utf-8")


def _settle(qapp, win, key, timeout=5.0):
    deadline = time.monotonic() + timeout
    qapp.processEvents()
    while win.task_runner.is_running(key) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def _click_and_settle(qapp, win, proj, launcher_id="opencode", **kwargs):
    """Click, then let the off-thread Fleet preflight deliver its continuation.

    T-216 TARGET H moved the managed preflight off the Qt GUI thread; the
    launch decision now arrives through the event loop, not inside the click.
    """
    win._on_open_with_launcher(proj, launcher_id, **kwargs)
    _settle(qapp, win, f"opencode:preflight:{proj.id}:{launcher_id}")


@contextmanager
def _policy_patch():
    """Stub the launch policy at the seam the window actually calls.

    Admission runs through ``admit_with_fallback``, which owns its own policy
    reference; patching only ``main_window.OpenCodeLaunchPolicy`` let the real
    policy run and fall back to a REAL degraded console launch.
    """
    with patch("audapack.ui_qt.main_window.OpenCodeLaunchPolicy") as policy:
        def admit(path, custom_template=None, known_display_name=""):
            return policy.return_value.admit(path, custom_template=custom_template)

        with patch("audapack.ui_qt.main_window.admit_with_fallback", admit):
            yield policy


def _bound_admission(root, lineage: str = "lineage-a"):
    return OpenCodeAdmission(
        True, root, ("python.exe", "bound/saipen.py", "--agent", "opencode", "launch", "opencode"),
        {"kind": "saipen-opencode-v1", "project_root": str(root), "entrypoint": "bound/saipen.py",
         "project_identity": str(root).replace("/", "\\").lower(), "project_lineage": lineage,
         "actor": "opencode"},
    )


def test_managed_preflight_refusal_happens_before_focus_or_spawn(tmp_path, qapp):
    _managed(tmp_path)
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, _monitor = make_window(tmp_path, [pa])
    messages: list[str] = []
    win.statusBar().messageChanged.connect(messages.append)
    try:
        # Startup schedules its own background scan; only the click is measured.
        _settle(qapp, win, "instances:refresh")
        scans_before = backend.scan_calls
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
            patch("audapack.ui_qt.main_window.QMessageBox.warning") as warning,
        ):
            policy.return_value.admit.side_effect = LaunchAdmissionError("bound CLI missing")
            _click_and_settle(qapp, win, pa)
        assert backend.focused == []
        assert backend.scan_calls == scans_before
        launch.assert_not_called()
        warning.assert_called_once()
        assert any("bound CLI missing" in m for m in messages) or "bound CLI missing" in win.statusBar().currentMessage()
    finally:
        win.close()


def test_recovery_admission_launches_without_modal_and_preserves_tracking(tmp_path, qapp):
    _managed(tmp_path)
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    backend.alive[500] = True
    backend.tokens[500] = 1
    base = _bound_admission(tmp_path)
    admission = OpenCodeAdmission(
        base.managed, base.cwd, base.command, base.binding,
        {
            "classification": "BOUND_RECOVERY_REQUIRED_SAFE",
            "reason_code": "BOARD_RECORD_OVERSIZE",
            "canonical_next_command": "saipen ticket",
        },
    )
    try:
        with (
            _policy_patch() as policy,
            patch("subprocess.Popen") as popen,
            patch("audapack.ui_qt.main_window.QMessageBox.warning") as warning,
        ):
            policy.return_value.admit.return_value = admission
            popen.return_value.pid = 500
            _click_and_settle(qapp, win, pa)
        warning.assert_not_called()
        assert "BOARD_RECORD_OVERSIZE" in win.statusBar().currentMessage()
        assert "saipen ticket" in win.statusBar().currentMessage()
        assert monitor.records[500].saipen_binding == admission.binding
        assert monitor.records[500].project_id == "pa"
        assert monitor.records[500].launcher_id == "opencode"
    finally:
        win.close()


def test_bound_spawn_script_sets_canonical_title_and_disables_terminal_title(tmp_path, qapp):
    """Every managed instance sets canonical title once and disables terminal title changes.

    OpenCode respects OPENCODE_DISABLE_TERMINAL_TITLE=true, so the PowerShell wrapper
    sets the canonical title once, passes the environment variable, avoids stderr
    redirection, and does not install a periodic title-fighting timer.
    """
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    backend.alive[500] = True
    backend.tokens[500] = 1
    admission = _bound_admission(tmp_path)
    try:
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = 500
            win._launch_bound_opencode(pa, admission)
        argv = popen.call_args.args[0]
        script = argv[-1]
        assert "OpenCode YOLO" in script
        assert str(tmp_path) in script
        assert "Project A" in script
        assert script.index("[Console]::Title") < script.index("& ")
        assert "OPENCODE_DISABLE_TERMINAL_TITLE" in script
        assert "titleGuard" not in script
        assert " 2> " not in script
        assert popen.call_args.kwargs["env"].get("OPENCODE_DISABLE_TERMINAL_TITLE") == "true"
        assert monitor.records[500].saipen_binding == admission.binding
        assert monitor.records[500].project_id == "pa"
        assert monitor.records[500].launcher_id == "opencode"
    finally:
        win.close()


def test_second_project_root_produces_distinguishable_canonical_title(tmp_path, qapp):
    """Two project roots never share one canonical managed console title."""
    pa = project("pa", "Project A", str(tmp_path / "a"))
    pb = project("pb", "Project B", str(tmp_path / "b"))
    win, _backend, _monitor = make_window(tmp_path, [pa, pb])
    admission_a = _bound_admission(tmp_path / "a")
    admission_b = _bound_admission(tmp_path / "b")
    scripts: list[str] = []
    try:
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = 500
            win._launch_bound_opencode(pa, admission_a)
            scripts.append(popen.call_args.args[0][-1])
            popen.return_value.pid = 501
            win._launch_bound_opencode(pb, admission_b)
            scripts.append(popen.call_args.args[0][-1])
        assert scripts[0] != scripts[1]
        assert str(tmp_path / "a") in scripts[0]
        assert str(tmp_path / "b") in scripts[1]
        assert "Project A" in scripts[0]
        assert "Project B" in scripts[1]
    finally:
        win.close()


def test_legacy_opencode_record_cannot_answer_managed_click(tmp_path, qapp):
    _managed(tmp_path)
    pa = project("pa", "Project A", str(tmp_path))
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        backend.windows = [tracked_window(11, 44, f"Project A | OpenCode | {tmp_path}", "opencode", pa, monitor)]
        admission = _bound_admission(tmp_path)
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = admission
            _click_and_settle(qapp, win, pa)
        assert backend.focused == []
        assert backend.closed == []
        launch.assert_called_once_with(pa, admission)
    finally:
        win.close()


def test_matching_bound_opencode_record_is_focused(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        admission = _bound_admission(tmp_path)
        monitor.track_launch(44, "opencode", pa, saipen_binding=admission.binding)
        backend.windows = [NativeWindow(11, 44, f"Project A | OpenCode | {tmp_path}", "powershell.exe")]
        # T-216: native discovery is a background lane; the harness installs the
        # snapshot explicitly instead of relying on a synchronous click scan.
        win._refresh_instance_snapshot()
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = admission
            win._on_open_with_launcher(pa, "opencode")
        assert backend.focused == [11]
        launch.assert_not_called()
    finally:
        win.close()


def test_bound_console_window_pid_can_differ_from_launch_record_pid(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        admission = _bound_admission(tmp_path)
        monitor.track_launch(44, "opencode", pa, saipen_binding=admission.binding)
        backend.windows = [NativeWindow(11, 99, f"Project A | OpenCode | {tmp_path}", "powershell.exe")]
        win._refresh_instance_snapshot()
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = admission
            win._on_open_with_launcher(pa, "opencode")
        assert backend.focused == [11]
        launch.assert_not_called()
    finally:
        win.close()


def test_changed_binding_and_second_project_cannot_reuse(tmp_path, qapp):
    _managed(tmp_path / "a", tmp_path / "b")
    pa = project("pa", "Project A", str(tmp_path / "a"))
    pb = project("pb", "Project B", str(tmp_path / "b"), slot=2)
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa, pb], backend=backend)
    try:
        old = _bound_admission(tmp_path / "a")
        monitor.track_launch(44, "opencode", pa, saipen_binding=old.binding)
        backend.windows = [NativeWindow(11, 44, f"Project A | OpenCode | {tmp_path / 'a'}", "powershell.exe")]
        changed = _bound_admission(tmp_path / "a", lineage="new-lineage")
        other = _bound_admission(tmp_path / "b")
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = changed
            _click_and_settle(qapp, win, pa)
            policy.return_value.admit.return_value = other
            _click_and_settle(qapp, win, pb)
        assert backend.focused == []
        assert launch.call_count == 2
        assert launch.call_args_list[0].args == (pa, changed)
        assert launch.call_args_list[1].args == (pb, other)
    finally:
        win.close()


def test_managed_shift_click_skips_compatible_focus_but_respects_limit(tmp_path, qapp):
    _managed(tmp_path)
    pa = project("pa", "Project A", str(tmp_path))
    launchers = create_default_launchers()
    next(lc for lc in launchers if lc.id == "opencode").max_instances = 2
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win, backend, monitor = make_window(tmp_path, [pa], launchers=launchers, backend=backend)
    messages: list[str] = []
    win.statusBar().messageChanged.connect(messages.append)
    try:
        admission = _bound_admission(tmp_path)
        monitor.track_launch(44, "opencode", pa, saipen_binding=admission.binding)
        backend.windows = [NativeWindow(11, 44, f"Project A | OpenCode | {tmp_path}", "powershell.exe")]
        win._refresh_instance_snapshot()
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = admission
            _click_and_settle(qapp, win, pa, force_new=True)
            assert launch.call_count == 1
            next(lc for lc in win._service.config.launchers if lc.id == "opencode").max_instances = 1
            _click_and_settle(qapp, win, pa, force_new=True)
            assert launch.call_count == 1
        assert backend.focused == []
        # Unrelated background status (Bridge health) may land after the click.
        assert any("limit 1" in message for message in messages), messages
    finally:
        win.close()


def test_managed_spawn_uses_bound_cli_not_external_launcher_or_direct_opencode(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    backend.alive[500] = True
    backend.tokens[500] = 1
    admission = _bound_admission(tmp_path)
    try:
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = 500
            win._launch_bound_opencode(pa, admission)
        argv = popen.call_args.args[0]
        script = argv[-1]
        assert argv[:4] == ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]
        assert popen.call_args.kwargs["cwd"] == str(tmp_path)
        assert "'bound/saipen.py' '--agent' 'opencode'" in script
        assert "AI_AGENT_LAUNCHER" not in script
        assert "opencode.cmd" not in script
        assert "-NoExit" not in argv
        assert monitor.records[500].saipen_binding == admission.binding
        assert monitor.records[500].project_id == "pa"
        assert monitor.records[500].launcher_id == "opencode"
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET C -- multi-instance window/launch correlation
# ---------------------------------------------------------------------------

def _two_bound_instances(tmp_path, pa):
    """The exact SRC-048 concurrency scene: two bound launches, two visible
    console windows whose PIDs differ from both recorded parent PIDs. Real
    bound consoles carry the launch correlation token in their title."""
    backend = FakeWindowBackend()
    backend.alive = {44: True, 55: True}
    backend.tokens = {44: 1, 55: 2}
    backend.windows = [
        NativeWindow(99, 99, f"Project A | OpenCode | {tmp_path} | OC-aaaa11", "powershell.exe"),
        NativeWindow(100, 100, f"Project A | OpenCode | {tmp_path} | OC-bbbb22", "powershell.exe"),
    ]
    monitor = InstanceMonitor(backend=backend, record_path=Path(tempfile.mkdtemp()) / "instances.json")
    first = _bound_admission(tmp_path, lineage="lineage-a")
    second = _bound_admission(tmp_path, lineage="lineage-a")
    monitor.track_launch(44, "opencode", pa, saipen_binding=first.binding, correlation_token="OC-aaaa11")
    monitor.track_launch(55, "opencode", pa, saipen_binding=second.binding, correlation_token="OC-bbbb22")
    return backend, monitor, first, second


def test_two_bound_windows_with_differing_pids_both_carry_binding(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    backend, monitor, _first, _second = _two_bound_instances(tmp_path, pa)
    try:
        instances = monitor.refresh([pa], create_default_launchers())
        windows = {item.pid: item for item in instances if item.hwnd}
        assert set(windows) == {99, 100}
        for item in windows.values():
            assert item.state == "running"
            assert item.tracked is True, f"PID {item.pid} lost binding evidence"
            assert item.saipen_binding is not None, f"PID {item.pid} lost binding evidence"
            assert item.project_id == "pa"
            assert item.launcher_id == "opencode"
        # no phantom "starting" rows for the still-live parents
        starting = [item for item in instances if item.state == "starting"]
        assert starting == []
    finally:
        pass


def test_normal_click_after_two_bound_instances_focuses_without_third_launch(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    backend = FakeWindowBackend()
    backend.alive = {44: True, 55: True}
    backend.tokens = {44: 1, 55: 2}
    backend.windows = [
        NativeWindow(99, 99, f"Project A | OpenCode | {tmp_path} | OC-aaaa11", "powershell.exe"),
        NativeWindow(100, 100, f"Project A | OpenCode | {tmp_path} | OC-bbbb22", "powershell.exe"),
    ]
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    try:
        first = _bound_admission(tmp_path, lineage="lineage-a")
        second = _bound_admission(tmp_path, lineage="lineage-a")
        monitor.track_launch(44, "opencode", pa, saipen_binding=first.binding, correlation_token="OC-aaaa11")
        monitor.track_launch(55, "opencode", pa, saipen_binding=second.binding, correlation_token="OC-bbbb22")
        monitor.refresh([pa], win._service.config.launchers)
        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = first
            for _ in range(4):
                win._on_open_with_launcher(pa, "opencode")
        assert launch.call_count == 0  # never a third process
        assert backend.focused and len(backend.focused) == 4
        assert len(set(backend.focused)) == 1  # deterministic: always the same window
    finally:
        win.close()


def test_legacy_unbound_window_cannot_inherit_binding_from_a_bound_record(tmp_path, qapp):
    """The dangerous mixed case: one old unbound OpenCode window beside one
    canonically bound one, same project/title family. Only the bound window
    may satisfy managed reuse."""
    pa = project("pa", "Project A", str(tmp_path))
    backend = FakeWindowBackend()
    backend.alive = {44: True, 80: True}
    backend.tokens = {44: 1}
    # PID 80: an old OpenCode console nobody launched from AUDAPACK (unbound,
    # no correlation token in its title); PID 44: the tracked bound launch,
    # window PID 99 differing from the parent and carrying the token.
    backend.windows = [
        NativeWindow(60, 80, f"Project A | OpenCode | {tmp_path}", "powershell.exe"),
        NativeWindow(99, 99, f"Project A | OpenCode | {tmp_path} | OC-aaaa11", "powershell.exe"),
    ]
    win, backend, monitor = make_window(tmp_path, [pa], backend=backend)
    admission = _bound_admission(tmp_path, lineage="lineage-a")
    monitor.track_launch(44, "opencode", pa, saipen_binding=admission.binding, correlation_token="OC-aaaa11")
    try:
        instances = monitor.refresh([pa], win._service.config.launchers)
        legacy = next(item for item in instances if item.pid == 80)
        assert legacy.saipen_binding is None, "unbound window inherited binding"

        with (
            _policy_patch() as policy,
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            policy.return_value.admit.return_value = admission
            win._on_open_with_launcher(pa, "opencode")
        # Only the bound window may answer; the legacy window must never be
        # focused as a managed reuse, and no third process spawns.
        assert launch.call_count == 0
        assert backend.focused == [99]
    finally:
        win.close()


# ---------------------------------------------------------------------------
# T-205 -- lifetime title ownership wiring
# ---------------------------------------------------------------------------

GENERIC_TITLE = "Administrator: Windows PowerShell"


class FakeTitleBackend:
    """In-memory title backend; no Win32 call leaves the process."""

    def __init__(self, *, event_driven: bool = True):
        self.event_driven = event_driven
        self.hook_callback = None
        self.hook_started = 0
        self.hook_stopped = 0
        self.windows: dict[int, dict] = {}

    def add_window(self, hwnd: int, pid: int, title: str) -> None:
        self.windows[hwnd] = {"pid": pid, "title": title}

    def resolve_hwnds(self, pid: int) -> list[int]:
        return [hwnd for hwnd, info in self.windows.items() if info["pid"] == pid]

    def window_pid(self, hwnd: int) -> int:
        info = self.windows.get(hwnd)
        return int(info["pid"]) if info else 0

    def get_title(self, hwnd: int) -> str:
        info = self.windows.get(hwnd)
        return str(info["title"]) if info else ""

    def set_title(self, hwnd: int, title: str) -> bool:
        if hwnd in self.windows:
            self.windows[hwnd]["title"] = title
        return True

    def process_alive(self, pid: int) -> bool:
        return True

    def process_token(self, pid: int) -> int:
        return 1

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
        self.windows[hwnd]["title"] = title
        if self.hook_callback is not None:
            self.hook_callback(hwnd)


def _install_fake_guardian(win, *, event_driven: bool = True):
    title_backend = FakeTitleBackend(event_driven=event_driven)
    title_backend.add_window(9100, 500, GENERIC_TITLE)
    guardian = TitleGuardian(backend=title_backend)
    win._title_guardian = guardian
    win._title_guardian_timer = None
    return guardian, title_backend


def test_bound_launch_owns_canonical_title_and_restores_external_drift(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, _monitor = make_window(tmp_path, [pa])
    backend.alive[500] = True
    backend.tokens[500] = 1
    guardian, title_backend = _install_fake_guardian(win)
    admission = _bound_admission(tmp_path)
    try:
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = 500
            win._launch_bound_opencode(pa, admission)
        registration = guardian.registration(500)
        assert registration is not None, "managed launch must register title ownership"
        assert "Project A | OpenCode YOLO" in registration.canonical_title
        assert str(tmp_path) in registration.canonical_title
        assert registration.correlation_token
        assert title_backend.get_title(9100) == registration.canonical_title

        # External drift (Ctrl+C, a TUI, the OS) is repaired without a respawn.
        title_backend.rename(9100, GENERIC_TITLE)
        assert title_backend.get_title(9100) == registration.canonical_title
        assert popen.call_count == 1
        assert guardian.active_count == 1
    finally:
        win.close()


def test_fallback_title_timer_runs_only_while_instances_exist(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, _monitor = make_window(tmp_path, [pa])
    backend.alive[500] = True
    backend.tokens[500] = 1
    guardian, title_backend = _install_fake_guardian(win, event_driven=False)
    admission = _bound_admission(tmp_path)
    try:
        # Zero managed instances: zero polling.
        assert win._title_guardian_timer is None
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = 500
            win._launch_bound_opencode(pa, admission)
        assert guardian.uses_fallback_polling is True
        assert win._title_guardian_timer is not None
        assert win._title_guardian_timer.interval() == 5000
        assert win._title_guardian_timer.isActive() is True

        # The sweep the timer performs repairs drift and is a no-op when stable.
        title_backend.rename(9100, GENERIC_TITLE)
        assert guardian.sweep() == 1
        assert title_backend.get_title(9100) == guardian.registration(500).canonical_title
        assert guardian.sweep() == 0

        with patch.object(win, "_report_opencode_launch_failure"):
            win._handle_opencode_exit(
                0, admission, tmp_path / "no-diag.json", tmp_path / "no-err.log", target_pid=500,
            )
        assert guardian.active_count == 0
        assert guardian.uses_fallback_polling is False
        assert win._title_guardian_timer is None, "the fallback timer stops with the last instance"
    finally:
        win.close()


def test_title_ownership_released_on_gui_close(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, _monitor = make_window(tmp_path, [pa])
    backend.alive[500] = True
    backend.tokens[500] = 1
    guardian, title_backend = _install_fake_guardian(win)
    admission = _bound_admission(tmp_path)
    with patch("subprocess.Popen") as popen:
        popen.return_value.pid = 500
        win._launch_bound_opencode(pa, admission)
    assert guardian.active_count == 1
    win.close()
    assert guardian.active_count == 0
    assert title_backend.hook_stopped >= 1, "closing AUDAPACK releases the title watcher"
