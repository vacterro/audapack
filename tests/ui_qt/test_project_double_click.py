"""T-209 P1: the project-row double-click action is configurable.

Only a POPULATED PROJECT ROW changes behaviour. Group headers still
expand/collapse, empty slots still open Add Project, and every configured
action delegates to the canonical handler that already owns it -- including
"launcher", which reuses the ONE launch path so a double-click can never spawn
a duplicate console.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QModelIndex

from audapack.config import (
    PROJECT_DOUBLE_CLICK_DEFAULT,
    PROJECT_DOUBLE_CLICK_LAUNCHER_DEFAULT,
    AppConfig,
    AuditsConfig,
    create_default_launchers,
    normalize_double_click_action,
    save_config,
)
from audapack.config import (
    load_config as load_app_config,
)
from audapack.instances import InstanceMonitor, NativeWindow
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.title_guardian import NullTitleBackend, TitleGuardian
from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget
from audapack.ui_qt.main_window import MainWindow


def project(project_id: str, name: str, path: str, slot: int = 1) -> Project:
    return Project(
        id=project_id,
        display_name=name,
        source_path=path,
        priority_group="MAIN0",
        slot=slot,
    )


def make_window(tmp_path, projects, *, launchers=None, action="launcher", launcher_id="opencode"):
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=list(projects),
        launchers=list(launchers) if launchers is not None else create_default_launchers(),
    )
    config.ui.project_double_click_action = action
    config.ui.project_double_click_launcher_id = launcher_id
    # Keep the launch path free of clipboard/audit filesystem side effects.
    config.ui.auto_copy_gg_on_launch = False
    service = ProjectService(config, base_dir=tmp_path)
    win = MainWindow(service)
    win._instance_monitor = InstanceMonitor(
        backend=FakeWindowBackend(), record_path=tmp_path / "instances.json"
    )
    win._instance_manager.monitor = win._instance_monitor
    # Never install a real global WinEvent hook from a test process.
    win._title_guardian = TitleGuardian(backend=NullTitleBackend())
    win._title_guardian_timer = None
    return win


class FakeWindowBackend:
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


# ---------------------------------------------------------------------------
# 1-2. launcher action
# ---------------------------------------------------------------------------

def test_launcher_action_calls_the_exactly_selected_launcher(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="launcher", launcher_id="cline")
    try:
        with patch.object(win, "_on_open_with_launcher") as launch:
            win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        launch.assert_called_once()
        assert launch.call_args.args[0].id == "pa"
        assert launch.call_args.args[1] == "cline"
    finally:
        win.close()


def test_launcher_action_reuses_the_canonical_launch_path(tmp_path, qapp, monkeypatch):
    """No second launcher path: the click lands in _on_open_with_cline."""
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="launcher", launcher_id="cline")
    try:
        with patch.object(win, "_on_open_with_cline") as cline:
            win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        cline.assert_called_once()
        assert cline.call_args.args[0].id == "pa"
    finally:
        win.close()


def test_an_existing_instance_is_focused_instead_of_duplicated(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    win = make_window(tmp_path, [pa], action="launcher", launcher_id="opencode")
    win._instance_monitor = InstanceMonitor(
        backend=backend, record_path=tmp_path / "instances.json"
    )
    win._instance_manager.monitor = win._instance_monitor
    try:
        win._instance_monitor.track_launch(44, "opencode", pa)
        backend.windows = [
            NativeWindow(11, 44, r"Project A | OpenCode | V:\code\a", "powershell.exe")
        ]
        win._refresh_instance_snapshot()
        with patch.object(win, "_on_open_with_opencode") as launch:
            win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert launch.call_count == 0
        assert backend.focused == [11]
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 3-9. every other configured action
# ---------------------------------------------------------------------------

def test_project_folder_action_opens_source_path(tmp_path, qapp, monkeypatch):
    source = tmp_path / "proj A"
    source.mkdir()
    pa = project("pa", "Project A", str(source))
    win = make_window(tmp_path, [pa], action="project_folder")
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(str(path)), raising=False)
    try:
        win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert opened == [str(source)]
    finally:
        win.close()


def test_inaudit_action_still_works_when_selected(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="inaudit")
    try:
        win.tabs.setCurrentIndex(0)
        win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert win.tabs.currentWidget() is win.inaudit_widget
        assert win.inaudit_widget._project is pa
    finally:
        win.close()


def test_instances_action_opens_the_instance_manager(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="instances")
    try:
        win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert win.tabs.currentWidget() is win._instance_manager
        assert win._instance_manager.project is pa
    finally:
        win.close()


def test_terminal_action_opens_a_console_at_the_project_root(tmp_path, qapp, monkeypatch):
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="terminal")
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "audapack.ui_qt.main_window.subprocess.Popen",
        lambda args, **kwargs: launched.append(list(args)),
    )
    try:
        win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert len(launched) == 1
        assert "powershell.exe" in launched[0]
        assert r"V:\code\a" in " ".join(launched[0])
    finally:
        win.close()


def test_archive_folder_action_opens_the_archive_dir(tmp_path, qapp, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    pa = project("pa", "Project A", str(tmp_path / "src"))
    win = make_window(tmp_path, [pa], action="archive_folder")
    win._service.config.packing.output_dir = str(out)
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(str(path)), raising=False)
    try:
        win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert opened and Path(opened[0]) == out
    finally:
        win.close()


def test_audit_folder_action_opens_the_project_audit_dir(tmp_path, qapp, monkeypatch):
    root = tmp_path / "audits"
    target = root / "MAIN0" / "pa"
    target.mkdir(parents=True)
    pa = project("pa", "Project A", str(tmp_path / "src"))
    win = make_window(tmp_path, [pa], action="audit_folder")
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(str(path)), raising=False)
    try:
        win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert opened == [str(target)]
    finally:
        win.close()


def test_none_action_does_nothing(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="none")
    try:
        before = win.tabs.currentWidget()
        with (
            patch.object(win, "_on_open_with_launcher") as launch,
            patch.object(win, "_show_project_inbox") as inbox,
            patch.object(win, "_show_instance_manager") as manager,
            patch.object(win, "_on_open_project_folder") as folder,
        ):
            win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert launch.call_count == 0
        assert inbox.call_count == 0
        assert manager.call_count == 0
        assert folder.call_count == 0
        assert win.tabs.currentWidget() is before
        assert "Nothing" in win.statusBar().currentMessage()
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 10-11. behaviour that must NOT change
# ---------------------------------------------------------------------------

def test_empty_slot_still_opens_add_project(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a", slot=1)
    win = make_window(tmp_path, [pa], action="launcher", launcher_id="cline")
    try:
        empty_index = win.model.index_for_slot("MAIN0", 4)
        assert empty_index.isValid()
        assert win.model.project_at("MAIN0", 4) is None
        with patch.object(win, "_on_add_project") as add:
            win._on_tree_double_clicked(empty_index)
        add.assert_called_once()
        assert add.call_args.kwargs.get("default_group") == "MAIN0"
        assert add.call_args.kwargs.get("default_slot") == 4
    finally:
        win.close()


def test_group_row_still_expands_and_collapses(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    win = make_window(tmp_path, [pa], action="launcher", launcher_id="cline")
    try:
        group_index = win.model.index(0, 0, QModelIndex())
        assert win.model.data(group_index, win.model.ROLES["node_type"]) == "group"
        win.tree.expand(group_index)
        assert win.tree.isExpanded(group_index)
        win._on_tree_double_clicked(group_index)
        assert not win.tree.isExpanded(group_index)
        win._on_tree_double_clicked(group_index)
        assert win.tree.isExpanded(group_index)
    finally:
        win.close()


# ---------------------------------------------------------------------------
# 12-14. persistence
# ---------------------------------------------------------------------------

def settings_widget(tmp_path, config=None):
    cfg = config or AppConfig()
    widget = SettingsWidget(cfg)
    widget._base_dir = tmp_path
    return widget, cfg


def test_launcher_selection_survives_a_launcher_reorder(tmp_path, qapp):
    widget, cfg = settings_widget(tmp_path)
    widget.project_double_click_action.setCurrentIndex(
        widget.project_double_click_action.findData("launcher")
    )
    widget.project_double_click_launcher.setCurrentIndex(
        widget.project_double_click_launcher.findData("cline")
    )
    assert load_app_config(tmp_path).ui.project_double_click_launcher_id == "cline"

    # Reorder the launcher list underneath the selector.
    cfg.launchers.reverse()
    widget._refresh_launcher_list()
    widget._save()

    assert widget.project_double_click_launcher.currentData() == "cline"
    assert load_app_config(tmp_path).ui.project_double_click_launcher_id == "cline"


def test_the_launcher_selector_only_matters_for_the_launcher_action(tmp_path, qapp):
    widget, _cfg = settings_widget(tmp_path)
    widget.project_double_click_action.setCurrentIndex(
        widget.project_double_click_action.findData("instances")
    )
    assert widget.project_double_click_launcher.isEnabled() is False
    widget.project_double_click_action.setCurrentIndex(
        widget.project_double_click_action.findData("launcher")
    )
    assert widget.project_double_click_launcher.isEnabled() is True


def test_a_deleted_launcher_never_silently_launches_another(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    launchers = [lc for lc in create_default_launchers() if lc.id != "cline"]
    win = make_window(tmp_path, [pa], launchers=launchers, action="launcher", launcher_id="cline")
    try:
        with patch.object(win, "_on_open_with_launcher") as launch:
            win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert launch.call_count == 0
        assert "Double-click launcher unavailable: cline" in win.statusBar().currentMessage()
    finally:
        win.close()


def test_a_disabled_launcher_never_silently_launches_another(tmp_path, qapp):
    pa = project("pa", "Project A", r"V:\code\a")
    launchers = create_default_launchers()
    for launcher in launchers:
        launcher.enabled = launcher.id != "cline"
    win = make_window(tmp_path, [pa], launchers=launchers, action="launcher", launcher_id="cline")
    try:
        with patch.object(win, "_on_open_with_launcher") as launch:
            win._on_tree_double_clicked(win.model.index_for_project_id("pa"))
        assert launch.call_count == 0
        assert "Double-click launcher unavailable: cline" in win.statusBar().currentMessage()
    finally:
        win.close()


def test_settings_save_and_reload_preserve_action_and_launcher_id(tmp_path, qapp):
    widget, _cfg = settings_widget(tmp_path)
    widget.project_double_click_action.setCurrentIndex(
        widget.project_double_click_action.findData("audit_folder")
    )
    widget.project_double_click_launcher.setCurrentIndex(
        widget.project_double_click_launcher.findData("cline")
    )
    widget._save()

    reloaded = load_app_config(tmp_path)
    assert reloaded.ui.project_double_click_action == "audit_folder"
    assert reloaded.ui.project_double_click_launcher_id == "cline"

    # and a rebuilt dialog shows exactly what was stored
    rebuilt, _ = settings_widget(tmp_path, reloaded)
    assert rebuilt.project_double_click_action.currentData() == "audit_folder"
    assert rebuilt.project_double_click_launcher.currentData() == "cline"


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------

def test_defaults_are_launcher_and_opencode(tmp_path):
    loaded = load_app_config(tmp_path)
    assert loaded.ui.project_double_click_action == PROJECT_DOUBLE_CLICK_DEFAULT == "launcher"
    assert loaded.ui.project_double_click_launcher_id == PROJECT_DOUBLE_CLICK_LAUNCHER_DEFAULT == "opencode"


def test_a_config_may_explicitly_preserve_the_historical_inaudit_behaviour(tmp_path):
    cfg = AppConfig()
    cfg.ui.project_double_click_action = "inaudit"
    assert save_config(cfg, tmp_path)
    assert load_app_config(tmp_path).ui.project_double_click_action == "inaudit"


def test_an_absent_or_unknown_value_migrates_deterministically_to_launcher(tmp_path):
    cfg = AppConfig()
    assert save_config(cfg, tmp_path)
    path = tmp_path / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    data["ui"].pop("project_double_click_action", None)
    data["ui"].pop("project_double_click_launcher_id", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_app_config(tmp_path)
    assert loaded.ui.project_double_click_action == "launcher"
    assert loaded.ui.project_double_click_launcher_id == "opencode"

    data["ui"]["project_double_click_action"] = "banana"
    data["ui"]["project_double_click_launcher_id"] = "   "
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_app_config(tmp_path)
    assert loaded.ui.project_double_click_action == "launcher"
    assert loaded.ui.project_double_click_launcher_id == "opencode"


def test_unknown_action_is_never_encoded_as_a_translated_label(tmp_path):
    """Only the stable values are ever accepted, whatever the UI shows."""
    from audapack.config import PROJECT_DOUBLE_CLICK_ACTIONS

    assert "Open preferred launcher" not in PROJECT_DOUBLE_CLICK_ACTIONS
    assert normalize_double_click_action("Open preferred launcher") == "launcher"
    assert normalize_double_click_action("instances") == "instances"
