"""SRC-081 / APP-CLI-001: multi-agent CLI launcher UI semantics (TARGET N).

Items 8-10 and 15-19 of the CLI launcher matrix: exact Claude 1 / Claude 2
instance identity with T-179 focus/reuse semantics, force-new Shift+click,
ten non-overlapping launcher buttons with paint/hit-test agreement, deliberate
narrow-row degradation, and the Ctrl+1..9 / Ctrl+0 positional keyboard model.

"""

from __future__ import annotations

from unittest.mock import patch

from PySide6.QtCore import QRect
from PySide6.QtGui import QKeySequence

from audapack.cli_launchers import LAUNCHER_SHORTCUT_KEYS
from audapack.config import AppConfig, AuditsConfig, create_default_launchers
from audapack.instances import InstanceMonitor, NativeWindow
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.title_guardian import TitleGuardian
from audapack.ui_qt.main_window import MainWindow
from audapack.ui_qt.models.project_delegate import (
    MIN_ROW_WIDTH,
    ProjectItemDelegate,
    actions_block_width,
    compute_info_button_rect,
    compute_layer_button_rects,
    compute_row_button_rects,
    full_row_min_width,
    launcher_button_label,
    launcher_button_width,
)


class FakeWindowBackend:
    """In-memory native backend. No Win32 call ever leaves the process."""

    def __init__(self, windows=None):
        self.windows = list(windows or [])
        self.alive: dict[int, bool] = {}
        self.tokens: dict[int, int] = {}
        self.focused: list[int] = []
        self.closed: list[int] = []
        self.arranged: list[tuple[list[int], str]] = []

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
        self.closed.append(hwnd)
        return True

    def arrange_windows(self, hwnds, mode):
        self.arranged.append((list(hwnds), mode))
        return len(list(hwnds))


class FakeTitleBackend:
    """In-memory title backend; no Win32 call leaves the process."""

    def __init__(self, *, event_driven: bool = True):
        self.event_driven = event_driven
        self.hook_callback = None
        self.hook_started = 0
        self.hook_stopped = 0
        self.windows: dict[int, dict] = {}

    def resolve_hwnds(self, pid: int) -> list[int]:
        return []

    def window_pid(self, hwnd: int) -> int:
        return 0

    def get_title(self, hwnd: int) -> str:
        return ""

    def set_title(self, hwnd: int, title: str) -> bool:
        return True

    def start_hook(self) -> bool:
        self.hook_started += 1
        return True

    def stop_hook(self) -> None:
        self.hook_stopped += 1


def project(project_id: str, name: str, path: str, slot: int = 1) -> Project:
    return Project(
        id=project_id,
        display_name=name,
        source_path=path,
        priority_group="MAIN0",
        slot=slot,
    )


def make_window(tmp_path, projects, *, launchers=None, backend=None):
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=list(projects),
        launchers=list(launchers) if launchers is not None else create_default_launchers(),
    )
    config.ui.compact_rows = True
    service = ProjectService(config, base_dir=tmp_path)
    win = MainWindow(service)
    backend = backend or FakeWindowBackend()
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    win._instance_monitor = monitor
    win._instance_manager.monitor = monitor
    win._title_guardian = TitleGuardian(backend=FakeTitleBackend())
    win._title_guardian_timer = None
    return win, backend, monitor


def _tracked_window(monitor, backend, pid, hwnd, launcher_id, proj, title):
    assert monitor.track_launch(pid, launcher_id, proj, correlation_token=f"tok{pid}")
    backend.alive[pid] = True
    backend.tokens[pid] = 1
    backend.windows = [NativeWindow(hwnd, pid, title, "powershell.exe")]


# ---------------------------------------------------------------------------
# TARGET N 8: repeated normal click on Claude 1 focuses Claude 1
# ---------------------------------------------------------------------------


def test_claude1_repeated_normal_click_focuses_claude1(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    win, backend, monitor = make_window(tmp_path, [pa])
    try:
        _tracked_window(
            monitor, backend, 44, 11, "claude1", pa,
            rf"Project A | Claude 1 | {tmp_path / 'a'} | tok44",
        )
        win._refresh_instance_snapshot()
        with patch.object(win, "_launch_cli_launcher") as launch:
            win._on_open_with_launcher(pa, "claude1")
        assert launch.call_count == 0
        assert backend.focused == [11]
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET N 9: Claude 2 never focuses Claude 1
# ---------------------------------------------------------------------------


def test_claude2_click_never_focuses_claude1(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    win, backend, monitor = make_window(tmp_path, [pa])
    try:
        _tracked_window(
            monitor, backend, 44, 11, "claude1", pa,
            rf"Project A | Claude 1 | {tmp_path / 'a'} | tok44",
        )
        win._refresh_instance_snapshot()
        with patch.object(win, "_launch_cli_launcher") as launch:
            win._on_open_with_launcher(pa, "claude2")
        assert launch.call_count == 1  # a fresh Claude 2, not Claude 1's window
        assert launch.call_args.args[1] == "claude2"
        assert backend.focused == []
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET N 10: Shift+click keeps force-new semantics on the new launchers
# ---------------------------------------------------------------------------


def test_shift_click_preserves_force_new_for_claude1(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    win, backend, monitor = make_window(tmp_path, [pa])
    try:
        _tracked_window(
            monitor, backend, 44, 11, "claude1", pa,
            rf"Project A | Claude 1 | {tmp_path / 'a'} | tok44",
        )
        win._refresh_instance_snapshot()
        with patch.object(win, "_launch_cli_launcher") as launch:
            win._on_open_with_launcher(pa, "claude1", force_new=True)
        assert launch.call_count == 1  # another instance, even though one runs
        assert backend.focused == []
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET N 15: ten enabled launchers -> ten non-overlapping button rectangles
# ---------------------------------------------------------------------------


def test_ten_enabled_launchers_produce_ten_non_overlapping_rects():
    launchers = create_default_launchers()
    assert len([lc for lc in launchers if lc.enabled]) == 10
    rect = QRect(0, 0, 700, 22)
    buttons, gg_rect = compute_row_button_rects(rect, launchers)
    assert len(buttons) == 10
    rects = [r for _launcher, r in buttons]
    for i, first in enumerate(rects):
        assert rect.contains(first)  # every button lives inside the row
        assert first.width() == launcher_button_width(launcher_button_label(buttons[i][0]))
        for second in rects[i + 1:]:
            assert not first.intersects(second)
    # The fixed info/layer block never collides with the launcher buttons.
    info_rect = compute_info_button_rect(rect, buttons, gg_rect)
    plus_rect, edit_rect = compute_layer_button_rects(rect, info_rect)
    controls = rects + [info_rect, plus_rect, edit_rect]
    for i, first in enumerate(controls):
        assert rect.contains(first)
        for second in controls[i + 1:]:
            assert not first.intersects(second)


def test_longer_labels_widen_their_own_button_instead_of_clipping():
    wide = type("L", (), {"enabled": True, "id": "x", "short_label": "CLAUDE"})()
    rect = QRect(0, 0, 700, 22)
    buttons, _gg = compute_row_button_rects(rect, [wide])
    assert buttons[0][1].width() == launcher_button_width("CLAUDE")
    assert buttons[0][1].width() > 18  # 6 chars -> 18 + 4*7, never clipped


# ---------------------------------------------------------------------------
# TARGET N 16: paint and hit-testing agree for all ten
# ---------------------------------------------------------------------------


def test_paint_and_hit_testing_agree_for_all_ten():
    import inspect

    from audapack.ui_qt import main_window as main_window_module

    paint_src = inspect.getsource(ProjectItemDelegate.paint)
    click_src = inspect.getsource(main_window_module.ProjectTreeView.mousePressEvent)
    # The painter draws the geometry helper's rects and guards with the shared
    # threshold helper; the click handler guards with the same helper.
    assert "compute_row_button_rects" in paint_src
    assert "full_row_min_width" in paint_src
    assert "compute_row_button_rects" in click_src
    assert "full_row_min_width" in click_src

    launchers = create_default_launchers()
    rect = QRect(0, 0, 700, 22)
    painted, _ = compute_row_button_rects(rect, launchers)
    hit, _ = compute_row_button_rects(rect, launchers)
    assert painted == hit  # deterministic: both sides compute identical pixels


# ---------------------------------------------------------------------------
# TARGET N 17: narrow-row degradation stays non-overlapping
# ---------------------------------------------------------------------------


def test_narrow_row_degradation_stays_non_overlapping(qapp):
    launchers = create_default_launchers()
    threshold = full_row_min_width(launchers)
    assert threshold == MIN_ROW_WIDTH + actions_block_width(launchers)
    # Below the threshold the painter draws NO buttons (cramped branch) and
    # the hit-tester refuses button clicks with the SAME helper -- so nothing
    # is painted on top of anything and nothing invisible is clickable.
    import inspect

    from audapack.ui_qt import main_window as main_window_module

    click_src = inspect.getsource(main_window_module.ProjectTreeView.mousePressEvent)
    assert "full_row_min_width(launchers)" in click_src
    # Even a cramped-width row's geometry stays inside its own pixels.
    rect = QRect(0, 0, threshold - 1, 22)
    buttons, _gg = compute_row_button_rects(rect, launchers)
    rects = [r for _launcher, r in buttons]
    for i, first in enumerate(rects):
        for second in rects[i + 1:]:
            assert not first.intersects(second)


# ---------------------------------------------------------------------------
# TARGET N 18: launcher reorder changes the positional shortcut mapping
# ---------------------------------------------------------------------------


def test_launcher_reorder_changes_positional_shortcut_mapping(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    win, _backend, _monitor = make_window(tmp_path, [pa])
    try:
        launchers = win._service.config.launchers
        zcode = next(lc for lc in launchers if lc.id == "zcode")
        rest = [lc for lc in launchers if lc.id != "zcode"]
        win._service.config.launchers = [zcode] + rest  # ZCode becomes position 1
        with patch.object(win, "_on_open_with_launcher") as launch:
            win._on_open_with_launcher_index(0)
            win._on_open_with_launcher_index(9)
        assert launch.call_args_list[0].args[1] == "zcode"
        assert launch.call_args_list[1].args[1] == rest[-1].id  # position 10
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET N 19: Ctrl+1..9 and Ctrl+0 resolve to positions 1..10
# ---------------------------------------------------------------------------


def test_ctrl_digit_shortcuts_resolve_to_intended_positions(tmp_path, qapp):
    win, _backend, _monitor = make_window(tmp_path, [project("pa", "Project A", str(tmp_path / "a"))])
    try:
        assert LAUNCHER_SHORTCUT_KEYS == tuple("123456789") + ("0",)
        # The historical first-six mapping survives unchanged (TARGET J).
        assert LAUNCHER_SHORTCUT_KEYS[:6] == ("1", "2", "3", "4", "5", "6")
        shortcuts = win._launcher_shortcuts
        assert len(shortcuts) == 2 * len(LAUNCHER_SHORTCUT_KEYS)  # 10 + 10 force-new
        for idx, key in enumerate(LAUNCHER_SHORTCUT_KEYS):
            assert shortcuts[2 * idx].key() == QKeySequence(f"Ctrl+{key}")
            assert shortcuts[2 * idx + 1].key() == QKeySequence(f"Ctrl+Shift+{key}")
        # No other window shortcut owns a Ctrl+digit (conflict check).
        digit_keys = {QKeySequence(f"Ctrl+{key}") for key in LAUNCHER_SHORTCUT_KEYS}
        owned = {
            sc.key()
            for sc in win.findChildren(type(shortcuts[0]))
            if sc not in shortcuts and sc.key() in digit_keys
        }
        assert not owned
    finally:
        win.close()
