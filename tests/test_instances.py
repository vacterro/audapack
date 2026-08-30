"""Instance monitoring, launcher capacity, and Qt manager regressions."""

from __future__ import annotations

from unittest.mock import patch

from audapack.config import AppConfig, LauncherConfig, create_default_launchers
from audapack.instances import InstanceMonitor, NativeWindow
from audapack.models import Project
from audapack.services.project_service import ProjectService


class FakeWindowBackend:
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


def test_monitor_discovers_titles_and_uses_tracked_freebuff_project(tmp_path):
    p1 = project("audapack", "AUDAPACK", r"V:\code\AUDAPACK")
    p2 = project("saipen", "SAIPEN", r"V:\code\SAIPEN", slot=2)
    backend = FakeWindowBackend(
        [
            NativeWindow(101, 1001, r"AUDAPACK | OpenCode YOLO | V:\code\AUDAPACK", "powershell.exe"),
            NativeWindow(202, 2002, "Freebuff: ccc", "powershell.exe"),
            NativeWindow(303, 3003, "Unrelated browser", "browser.exe"),
        ]
    )
    backend.alive[2222] = True
    backend.tokens[2222] = 91
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(2222, "freebuff", p2)

    instances = monitor.refresh([p1, p2], create_default_launchers())

    assert len(instances) == 2
    opencode = next(item for item in instances if item.launcher_id == "opencode")
    freebuff = next(item for item in instances if item.launcher_id == "freebuff")
    assert (opencode.project_id, opencode.hwnd, opencode.tracked) == ("audapack", 101, False)
    assert (freebuff.project_id, freebuff.project_name, freebuff.hwnd, freebuff.tracked) == (
        "saipen",
        "SAIPEN",
        202,
        True,
    )


def test_freebuff_global_limit_blocks_every_project(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    p2 = project("p2", "Project Two", r"V:\code\two", slot=2)
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, r"Project One | FreeBuff | V:\code\one", "powershell.exe")]
    )
    launchers = create_default_launchers()
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.refresh([p1, p2], launchers)

    freebuff = next(item for item in launchers if item.id == "freebuff")
    reason = monitor.block_reason(freebuff)
    assert monitor.count_for_launcher("freebuff") == 1
    assert "limit 1" in reason
    assert "Project One" in reason


def test_pending_launch_blocks_before_window_appears(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend()
    backend.alive[77] = True
    backend.tokens[77] = 1234
    launchers = create_default_launchers()
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.track_launch(77, "freebuff", p1)

    instances = monitor.refresh([p1], launchers)

    assert [(item.state, item.hwnd, item.pid) for item in instances] == [("starting", 0, 77)]
    freebuff = next(item for item in launchers if item.id == "freebuff")
    assert monitor.block_reason(freebuff)


def test_tracked_window_survives_title_change_and_scan_failure_is_explicit(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend([NativeWindow(90, 77, "session renamed itself", "powershell.exe")])
    backend.alive[77] = True
    backend.tokens[77] = 1234
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.track_launch(77, "freebuff", p1)

    instances = monitor.refresh([p1], create_default_launchers())
    assert [(item.launcher_id, item.project_id, item.title) for item in instances] == [
        ("freebuff", "p1", "session renamed itself")
    ]

    def broken_scan():
        raise OSError("EnumWindows denied")

    backend.list_windows = broken_scan
    assert monitor.refresh([p1], create_default_launchers()) == []
    assert monitor.last_error == "Native window scan failed: EnumWindows denied"


def test_reused_pid_drops_stale_launch_record(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend()
    backend.alive[77] = True
    backend.tokens[77] = 10
    record_path = tmp_path / "instances.json"
    monitor = InstanceMonitor(backend=backend, record_path=record_path)
    monitor.track_launch(77, "freebuff", p1)
    backend.tokens[77] = 11

    assert monitor.refresh([p1], create_default_launchers()) == []
    assert monitor.records == {}
    assert record_path.read_text(encoding="utf-8").strip() == "[]"


def test_monitors_reload_shared_launch_records_across_gui_processes(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend()
    backend.alive[77] = True
    backend.tokens[77] = 1234
    record_path = tmp_path / "instances.json"
    writer = InstanceMonitor(backend=backend, record_path=record_path)
    reader = InstanceMonitor(backend=backend, record_path=record_path)

    assert writer.track_launch(77, "freebuff", p1)
    assert [(item.state, item.pid, item.project_id) for item in reader.refresh([p1], create_default_launchers())] == [
        ("starting", 77, "p1")
    ]

    backend.alive[77] = False
    writer.refresh([p1], create_default_launchers())
    assert reader.refresh([p1], create_default_launchers()) == []


def test_monitor_actions_only_forward_known_native_windows(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend(
        [
            NativeWindow(11, 44, r"Project One | OpenCode | V:\code\one", "powershell.exe"),
            NativeWindow(12, 45, r"Project One | Cline | V:\code\one", "powershell.exe"),
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    instances = monitor.refresh([p1], create_default_launchers())

    assert monitor.focus(instances[0])
    assert monitor.close(instances[1])
    assert monitor.arrange(instances, "cascade") == 2
    assert backend.focused == [instances[0].hwnd]
    assert backend.closed == [instances[1].hwnd]
    assert backend.arranged == [([item.hwnd for item in instances], "cascade")]


def test_monitor_uses_command_line_after_tui_rewrites_window_title(tmp_path):
    p1 = project("saipen", "SAIPEN", r"V:\code\SAIPEN")
    backend = FakeWindowBackend(
        [
            NativeWindow(
                11,
                44,
                "⠹ _SAIPEN",
                "powershell.exe",
                r'powershell -Command "SAIPEN | Codex (main_codex) | V:\code\SAIPEN"',
            )
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    instances = monitor.refresh([p1], create_default_launchers())

    assert [(item.project_id, item.launcher_id, item.title) for item in instances] == [
        ("saipen", "main_codex", "⠹ _SAIPEN")
    ]


def test_monitor_prefers_command_workdir_and_rejects_unrelated_launcher_word(tmp_path):
    launcher_project = project("launcher", "Launcher", r"V:\very-long\launcher-script-folder")
    target = project("target", "Target", r"V:\code\target", slot=2)
    backend = FakeWindowBackend(
        [
            NativeWindow(
                11,
                44,
                "⠹ Target",
                "powershell.exe",
                r'powershell -File V:\very-long\launcher-script-folder\start.ps1 -Agent OpenCode -WorkDir V:\code\target',
            ),
            NativeWindow(
                13,
                46,
                "Unregistered | OpenCode",
                "powershell.exe",
                r"powershell -File agent.ps1 -Agent OpenCode -WorkDir V:\code\unregistered",
            ),
            NativeWindow(12, 45, "#general | Freebuff - Discord", "Discord.exe"),
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    instances = monitor.refresh([launcher_project, target], create_default_launchers())

    assert {(item.project_id, item.launcher_id) for item in instances} == {
        ("", "opencode"),
        ("target", "opencode"),
    }
    assert next(item for item in instances if not item.project_id).project_name == "Unknown project"


def test_monitor_reads_explicit_saipen_activity_without_claiming_terminal_output(tmp_path):
    root = tmp_path / "project"
    memory = root / ".saipen"
    memory.mkdir(parents=True)
    (memory / "STATE.md").write_text(
        '---\nphase: BUILD\ntask: T-77\nnext_action: "PHASE BUILD T-77"\n---\n',
        encoding="utf-8",
    )
    (memory / "LOG.md").write_text(
        "- 30.08.26 00:01 [E-001] [T-77] RUN: inspect windows -> PASS\n",
        encoding="utf-8",
    )
    p1 = project("p1", "Project One", str(root))
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, f"Project One | OpenCode | {root}", "powershell.exe")]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    instance = monitor.refresh([p1], create_default_launchers())[0]

    assert instance.activity == "BUILD · T-77 · PHASE BUILD T-77"
    assert instance.last_action.endswith("RUN: inspect windows -> PASS")


def test_launcher_config_migrates_freebuff_limit_and_roundtrips():
    legacy = LauncherConfig.from_dict(
        {"id": "freebuff", "name": "FreeBuff", "short_label": "FB", "enabled": True}
    )
    custom = LauncherConfig.from_dict(
        {"id": "custom", "name": "Custom", "short_label": "CU", "max_instances": "3"}
    )

    assert legacy.max_instances == 1
    assert custom.max_instances == 3
    assert LauncherConfig.from_dict(custom.to_dict()) == custom


def test_instance_manager_lists_project_and_global_windows(tmp_path, qapp):
    from audapack.ui_qt.dialogs.instance_manager import InstanceManagerWidget

    p1 = project("p1", "Project One", r"V:\code\one")
    p2 = project("p2", "Project Two", r"V:\code\two", slot=2)
    config = AppConfig(projects=[p1, p2])
    service = ProjectService(config, base_dir=tmp_path)
    backend = FakeWindowBackend(
        [
            NativeWindow(11, 44, r"Project One | OpenCode | V:\code\one", "powershell.exe"),
            NativeWindow(12, 45, r"Project Two | Cline | V:\code\two", "powershell.exe"),
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    dialog = InstanceManagerWidget(monitor, service, p1)
    try:
        assert dialog.table.rowCount() == 2
        assert dialog.scope_tabs.tabText(0) == "All (2)"
        assert dialog.scope_tabs.tabText(1) == "Project One (1)"
        assert "Windows 2" in dialog.capacity_label.text()
        assert "FB 0/1" in dialog.capacity_label.text()
        assert dialog._selected_instance().project_id == "p1"
        assert dialog.activity_label.text().startswith("Current: RUNNING")
        dialog.scope_tabs.setCurrentIndex(1)
        assert dialog.table.rowCount() == 1
        assert dialog.focus_button.isEnabled()
    finally:
        dialog.close()


def test_main_window_enforces_limit_and_project_click_opens_manager(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    p1 = project("p1", "Project One", r"V:\code\one")
    p2 = project("p2", "Project Two", r"V:\code\two", slot=2)
    service = ProjectService(AppConfig(projects=[p1, p2]), base_dir=tmp_path)
    window = MainWindow(service)
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, r"Project One | FreeBuff | V:\code\one", "powershell.exe")]
    )
    window._instance_monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    window._instance_manager.monitor = window._instance_monitor
    try:
        with (
            patch.object(window, "_on_open_with_freebuff") as launch,
            patch.object(window, "_show_instance_manager") as show_manager,
        ):
            window._on_open_with_launcher(p2, "freebuff")
            launch.assert_not_called()
            show_manager.assert_called_once_with(p2)
            assert "Launch blocked" in window.statusBar().currentMessage()

        index = window.model.index_for_project_id(p2.id)
        with patch.object(window, "_show_instance_manager") as show_manager:
            window._on_tree_double_clicked(index)
            show_manager.assert_called_once_with(p2)

        with patch.object(window, "_show_instance_manager") as show_manager:
            window.tree.clicked.emit(index)
            show_manager.assert_not_called()

        window._show_instance_manager(p2)
        assert window.tabs.currentWidget() is window._instance_manager
        assert window._instance_manager.window() is window
        assert window._instance_manager.project is p2
    finally:
        window.close()
