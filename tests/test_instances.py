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


def test_freebuff_default_is_unlimited_across_projects(tmp_path):
    """T-230: FreeBuff no longer carries a product-owned single-instance cap.

    Three live FreeBuff windows across three projects must not block each other
    and must not produce a global-capacity refusal.
    """
    p1 = project("p1", "Project One", r"V:\code\one")
    p2 = project("p2", "Project Two", r"V:\code\two", slot=2)
    p3 = project("p3", "Project Three", r"V:\code\three", slot=3)
    backend = FakeWindowBackend(
        [
            NativeWindow(11, 44, r"Project One | FreeBuff | V:\code\one", "powershell.exe"),
            NativeWindow(12, 45, r"Project Two | FreeBuff | V:\code\two", "powershell.exe"),
            NativeWindow(13, 46, r"Project Three | FreeBuff | V:\code\three", "powershell.exe"),
        ]
    )
    launchers = create_default_launchers()
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.refresh([p1, p2, p3], launchers)

    freebuff = next(item for item in launchers if item.id == "freebuff")
    assert freebuff.max_instances == 0
    assert monitor.count_for_launcher("freebuff") == 3
    assert monitor.block_reason(freebuff) == ""


def test_same_project_force_new_freebuff_instances_stay_independent(tmp_path):
    """T-230 TARGET G: force-new FreeBuff launches on one project never collapse."""
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend()
    for pid, token in ((44, 1), (55, 2), (66, 3)):
        backend.alive[pid] = True
        backend.tokens[pid] = token
    record_path = tmp_path / "instances.json"
    monitor = InstanceMonitor(backend=backend, record_path=record_path)
    for pid in (44, 55, 66):
        assert monitor.track_launch(pid, "freebuff", pa)
    backend.windows = [
        NativeWindow(11, 44, r"Project A | FreeBuff | V:\code\a", "powershell.exe"),
        NativeWindow(22, 55, r"Project A | FreeBuff | V:\code\a", "powershell.exe"),
        NativeWindow(33, 66, r"Project A | FreeBuff | V:\code\a", "powershell.exe"),
    ]

    instances = monitor.refresh([pa], create_default_launchers())

    freebuff = [item for item in instances if item.launcher_id == "freebuff"]
    assert len(freebuff) == 3
    assert {(item.hwnd, item.pid, item.launch_pid) for item in freebuff} == {
        (11, 44, 44),
        (22, 55, 55),
        (33, 66, 66),
    }
    assert all(item.project_id == "pa" and item.launcher_id == "freebuff" for item in freebuff)
    assert monitor.count_for_launcher("freebuff") == 3


def test_explicit_launcher_capacity_blocks_globally(tmp_path):
    """T-230 TARGET H: the generic max_instances engine is untouched.

    An explicit cap must still refuse a further launch, whoever owns the window.
    """
    p1 = project("p1", "Project One", r"V:\code\one")
    p2 = project("p2", "Project Two", r"V:\code\two", slot=2)
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, r"Project One | FreeBuff | V:\code\one", "powershell.exe")]
    )
    launchers = create_default_launchers()
    next(item for item in launchers if item.id == "freebuff").max_instances = 1
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.refresh([p1, p2], launchers)

    freebuff = next(item for item in launchers if item.id == "freebuff")
    reason = monitor.block_reason(freebuff)
    assert monitor.count_for_launcher("freebuff") == 1
    assert "limit 1" in reason
    assert "Project One" in reason


def test_explicit_capacity_two_allows_two_and_blocks_the_third(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    p2 = project("p2", "Project Two", r"V:\code\two", slot=2)
    p3 = project("p3", "Project Three", r"V:\code\three", slot=3)
    backend = FakeWindowBackend(
        [
            NativeWindow(11, 44, r"Project One | FreeBuff | V:\code\one", "powershell.exe"),
            NativeWindow(12, 45, r"Project Two | FreeBuff | V:\code\two", "powershell.exe"),
            NativeWindow(13, 46, r"Project Three | FreeBuff | V:\code\three", "powershell.exe"),
        ]
    )
    launchers = create_default_launchers()
    next(item for item in launchers if item.id == "freebuff").max_instances = 2
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.refresh([p1, p2, p3], launchers)

    freebuff = next(item for item in launchers if item.id == "freebuff")
    assert monitor.count_for_launcher("freebuff") == 3
    assert "limit 2" in monitor.block_reason(freebuff)



def test_pending_launch_blocks_before_window_appears(tmp_path):
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend()
    backend.alive[77] = True
    backend.tokens[77] = 1234
    launchers = create_default_launchers()
    # T-230: pin the block with an explicit cap; FreeBuff ships unlimited now.
    next(lc for lc in launchers if lc.id == "freebuff").max_instances = 1
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


def test_focus_candidate_breaks_a_same_tick_timestamp_tie_by_launch_order(tmp_path):
    """Two launches inside one clock tick must still focus the NEWER window.

    Windows resolves datetime.now() to roughly the system clock tick, so two
    consoles started back to back share `started_at` exactly. With only that
    key the sort was a tie and the stable lowest-PID pass decided, so clicking
    a launcher brought the OLDER console to the front. The monotonic launch
    sequence makes the order total.
    """
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend()
    for pid, token in ((44, 1), (55, 2)):
        backend.alive[pid] = True
        backend.tokens[pid] = token
    record_path = tmp_path / "instances.json"
    monitor = InstanceMonitor(backend=backend, record_path=record_path)

    assert monitor.track_launch(44, "opencode", p1)
    assert monitor.track_launch(55, "opencode", p1)

    # Force the exact collision the real clock produces intermittently.
    stamp = "2026-09-09T14:31:13.335000Z"
    for pid in (44, 55):
        monitor.records[pid].started_at = stamp
    assert monitor.records[44].sequence < monitor.records[55].sequence

    backend.windows = [
        NativeWindow(11, 44, r"Project One | OpenCode | V:\code\one", "powershell.exe"),
        NativeWindow(22, 55, r"Project One | OpenCode | V:\code\one", "powershell.exe"),
    ]
    monitor._save_records()
    monitor.refresh([p1], create_default_launchers())
    for pid in (44, 55):
        monitor.records[pid].started_at = stamp

    candidate = monitor.focus_candidate("p1", "opencode")
    assert candidate is not None
    assert candidate.pid == 55, "the most recently launched window wins the tie"
    assert candidate.hwnd == 22

    # And the order survives a reload by another GUI process.
    reader = InstanceMonitor(backend=backend, record_path=record_path)
    reader.refresh([p1], create_default_launchers())
    assert reader.records[55].sequence > reader.records[44].sequence


def test_correlated_visible_pids_keep_launch_recency_and_focus_newest(tmp_path):
    """A conhost/TUI PID is presentation identity, not LaunchRecord identity."""
    p1 = project("p1", "Project One", r"V:\code\one")
    binding = {
        "kind": "saipen-opencode-v1",
        "project_root": r"V:\code\one",
        "entrypoint": r"V:\saipen\tools\saipen.py",
        "project_identity": r"v:\code\one",
        "project_lineage": "lineage-1",
        "actor": "buffy",
    }
    backend = FakeWindowBackend()
    backend.alive.update({44: True, 55: True})
    backend.tokens.update({44: 101, 55: 102})
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.track_launch(44, "opencode", p1, saipen_binding=binding, correlation_token="OC-one")
    monitor.track_launch(55, "opencode", p1, saipen_binding=binding, correlation_token="OC-two")

    backend.windows = [
        NativeWindow(199, 99, "Project One | OpenCode YOLO | OC-one", "powershell.exe"),
        NativeWindow(200, 100, "Project One | OpenCode YOLO | OC-two", "powershell.exe"),
    ]
    instances = monitor.refresh([p1], create_default_launchers())

    compatible = [item for item in instances if item.tracked and item.saipen_binding == binding]
    assert {(item.hwnd, item.pid, item.launch_pid) for item in compatible} == {
        (199, 99, 44),
        (200, 100, 55),
    }
    candidate = monitor.focus_candidate("p1", "opencode", saipen_binding=binding)
    assert candidate is not None
    assert (candidate.hwnd, candidate.pid, candidate.launch_pid) == (200, 100, 55)
    assert monitor.focus(candidate)
    assert backend.focused == [200]

    repeated = monitor.focus_candidate("p1", "opencode", saipen_binding=binding)
    assert repeated is not None
    assert (repeated.hwnd, repeated.pid) == (200, 100)
    assert monitor.focus(repeated)
    assert backend.focused == [200, 200]


def test_legacy_launch_records_without_a_sequence_still_load(tmp_path):
    import json

    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend()
    backend.alive[77] = True
    backend.tokens[77] = 5
    record_path = tmp_path / "instances.json"
    record_path.write_text(json.dumps([{
        "pid": 77,
        "launcher_id": "opencode",
        "project_id": "p1",
        "project_name": "Project One",
        "project_path": r"V:\code\one",
        "started_at": "2026-01-01T00:00:00Z",
        "process_token": 5,
    }]), encoding="utf-8")

    monitor = InstanceMonitor(backend=backend, record_path=record_path)
    assert monitor.records[77].sequence == 0
    # A new launch must not reuse the legacy 0 slot.
    backend.alive[88] = True
    backend.tokens[88] = 6
    assert monitor.track_launch(88, "opencode", p1)
    assert monitor.records[88].sequence == 1


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


def test_launcher_config_parses_capacity_generically_and_roundtrips():
    """T-230: no launcher id gets an implicit capacity any more."""
    legacy_freebuff = LauncherConfig.from_dict(
        {"id": "freebuff", "name": "FreeBuff", "short_label": "FB", "enabled": True}
    )
    legacy_opencode = LauncherConfig.from_dict(
        {"id": "opencode", "name": "OpenCode", "short_label": "OC", "enabled": True}
    )
    explicit = LauncherConfig.from_dict(
        {"id": "freebuff", "name": "FreeBuff", "short_label": "FB", "max_instances": 1}
    )
    custom = LauncherConfig.from_dict(
        {"id": "custom", "name": "Custom", "short_label": "CU", "max_instances": "3"}
    )
    invalid = LauncherConfig.from_dict(
        {"id": "freebuff", "name": "FreeBuff", "short_label": "FB", "max_instances": "garbage"}
    )

    assert legacy_freebuff.max_instances == 0, "FreeBuff no longer defaults to a cap"
    assert legacy_opencode.max_instances == 0
    assert explicit.max_instances == 1, "an explicit persisted cap is preserved"
    assert custom.max_instances == 3
    assert invalid.max_instances == 0, "an unparseable value falls back to unlimited"
    assert LauncherConfig.from_dict(custom.to_dict()) == custom
    assert LauncherConfig.from_dict(explicit.to_dict()) == explicit



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
        assert "FB 0/∞" in dialog.capacity_label.text()
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
    # T-230: FreeBuff no longer ships a cap; this pins the explicit generic
    # capacity rule, so the click must be refused by the operator-set limit.
    next(lc for lc in service.config.launchers if lc.id == "freebuff").max_instances = 1
    window = MainWindow(service)
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, r"Project One | FreeBuff | V:\code\one", "powershell.exe")]
    )
    window._instance_monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    window._instance_manager.monitor = window._instance_monitor
    # T-216: the launcher click no longer scans native windows on the GUI
    # thread; the harness installs the snapshot explicitly, exactly like every
    # other capacity regression in this repository.
    window._refresh_instance_snapshot()
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
        assert window.tabs.count() >= 2
        window.tabs.setCurrentIndex(0)
        # T-209: the default double-click action is now "launcher"; this test
        # pins the INAUDIT action path, which is one of the selectable values.
        window._service.config.ui.project_double_click_action = "inaudit"
        window._on_tree_double_clicked(index)
        # T-143: a double-click on a project opens ITS audit inbox. Instances
        # answers a different question -- how many windows this project has --
        # and is still reachable from the context menu and the toolbar.
        assert window.tabs.currentWidget() is window.inaudit_widget
        assert window.inaudit_widget._project is p2

        index = window.model.index_for_project_id(p1.id)
        with patch.object(window, "_show_instance_manager") as show_manager:
            index_before = window.tabs.currentWidget()
            window.tree.clicked.emit(index)
            show_manager.assert_not_called()
            assert window.tabs.currentWidget() is index_before

        window._show_instance_manager(p2)
        assert window.tabs.currentWidget() is window._instance_manager
        assert window._instance_manager.window() is window
        assert window._instance_manager.project is p2
    finally:
        window.close()


def _pending_record(path=r"V:\___VAC\__K\__CODE\__SAITULS"):
    from audapack.instances import LaunchRecord

    return LaunchRecord(
        pid=1, launcher_id="opencode", project_id="saituls",
        project_name="__SAITULS", project_path=path, started_at="",
    )


def test_a_window_rooted_elsewhere_is_not_adopted_by_a_pending_launch():
    """A window that already names a directory has told you where it lives.

    A window with no project match is adopted by the only pending launch
    record for its launcher, on the theory that a just-started console has not
    titled itself yet. Observed live: an OpenCode window under
    __STORE/_PERSONAL/_9router was adopted as __SAITULS -- whose source is
    __CODE/__SAITULS -- purely because it was the only pending OpenCode
    launch, and the Instances tab then showed __SAITULS twice.
    """
    identity = r"_9router | OpenCode YOLO | V:\___VAC\__K\__STORE\_PERSONAL\_9router"
    assert InstanceMonitor._window_names_another_root(identity, _pending_record()) is True


def test_the_projects_own_window_is_still_adopted():
    identity = r"__SAITULS | OpenCode YOLO | V:\___VAC\__K\__CODE\__SAITULS"
    assert InstanceMonitor._window_names_another_root(identity, _pending_record()) is False


def test_a_subdirectory_of_the_project_is_still_adopted():
    identity = r"x | OpenCode YOLO | V:\___VAC\__K\__CODE\__SAITULS	ools"
    assert InstanceMonitor._window_names_another_root(identity, _pending_record()) is False


def test_a_window_naming_no_path_is_still_adoptable():
    """The case the adoption exists for: a console that has not titled itself."""
    assert InstanceMonitor._window_names_another_root("OpenCode YOLO", _pending_record()) is False


def test_a_record_without_a_project_path_never_refuses():
    identity = r"_9router | OpenCode YOLO | V:\somewhere\else"
    assert InstanceMonitor._window_names_another_root(identity, _pending_record(path="")) is False


def test_a_foreign_window_is_not_counted_as_a_second_window_of_the_pending_project(tmp_path):
    """End to end: the Instances tab must not show one project twice.

    A pending OpenCode launch for __SAITULS plus an unrelated OpenCode window
    rooted under __STORE/_PERSONAL/_9router used to produce two __SAITULS rows,
    because the foreign window matched no project and was adopted as the only
    pending candidate for its launcher.
    """
    saituls = project("saituls", "__SAITULS", r"V:\___VAC\__K\__CODE\__SAITULS")
    # The foreign window is enumerated FIRST: that is what let it take the
    # pending record before the project's own window was ever considered.
    backend = FakeWindowBackend(
        [
            NativeWindow(
                202, 2002,
                r"_9router | OpenCode YOLO | V:\___VAC\__K\__STORE\_PERSONAL\_9router",
                "powershell.exe",
            ),
            NativeWindow(
                101, 1001,
                r"__SAITULS | OpenCode YOLO | V:\___VAC\__K\__CODE\__SAITULS",
                "powershell.exe",
            ),
        ]
    )
    backend.alive[1001] = True
    backend.tokens[1001] = 77
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(1001, "opencode", saituls)

    instances = monitor.refresh([saituls], create_default_launchers())

    owned = [item for item in instances if item.project_id == "saituls"]
    assert len(owned) == 1, [f"{item.project_id}:{item.title}" for item in instances]
    assert owned[0].hwnd == 101


LAUNCHER_CMD = (
    r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile '
    r'-ExecutionPolicy Bypass -File "V:\___VAC\__K\__CODE\__SAITULS\Scripts\AI_AGENT_LAUNCHER.PS1" '
    r'-Agent OpenCode -WorkDir "{workdir}"'
)


def test_the_declared_working_directory_is_read_from_the_command_line():
    cmd = LAUNCHER_CMD.format(workdir=r"V:\___VAC\__K\__STORE\_PERSONAL\_9router")
    assert InstanceMonitor._declared_workdir(cmd) == r"v:\___vac\__k\__store\_personal\_9router"
    assert InstanceMonitor._declared_workdir("opencode.exe --serve") == ""


def test_a_launcher_script_inside_one_project_does_not_claim_another_projects_window():
    """AI_AGENT_LAUNCHER.PS1 lives in __SAITULS and is run for other directories.

    Matching the script path attributed every such console to __SAITULS, which
    is how the Instances tab came to show that project twice with one row that
    was never its window.
    """
    saituls = project("saituls", "__SAITULS", r"V:\___VAC\__K\__CODE\__SAITULS")
    cmd = LAUNCHER_CMD.format(workdir=r"V:\___VAC\__K\__STORE\_PERSONAL\_9router")
    assert InstanceMonitor._project_from_command_line(cmd, [saituls]) is None


def test_a_console_launched_into_its_own_project_still_matches():
    saituls = project("saituls", "__SAITULS", r"V:\___VAC\__K\__CODE\__SAITULS")
    cmd = LAUNCHER_CMD.format(workdir=r"V:\___VAC\__K\__CODE\__SAITULS")
    assert InstanceMonitor._project_from_command_line(cmd, [saituls]) is saituls


def test_a_subdirectory_workdir_still_matches_its_project():
    saituls = project("saituls", "__SAITULS", r"V:\___VAC\__K\__CODE\__SAITULS")
    cmd = LAUNCHER_CMD.format(workdir=r"V:\___VAC\__K\__CODE\__SAITULS\tools")
    assert InstanceMonitor._project_from_command_line(cmd, [saituls]) is saituls


def test_without_a_declared_workdir_the_old_path_matching_still_applies():
    saituls = project("saituls", "__SAITULS", r"V:\___VAC\__K\__CODE\__SAITULS")
    cmd = r'opencode.exe --project "V:\___VAC\__K\__CODE\__SAITULS"'
    assert InstanceMonitor._project_from_command_line(cmd, [saituls]) is saituls


# --------------------------------------------------------------------------
# T-179 defect 1: a browser tab is content, not an agent console
# --------------------------------------------------------------------------

def test_an_untracked_browser_titled_with_a_project_and_claude_is_rejected(tmp_path):
    """chrome.exe "AUDAPACK | Claude Code" is a page, not a console.

    The project name in the title resolved first, so the denylist -- consulted
    only for project-less windows -- never ran and the tab was promoted.
    """
    p1 = project("audapack", "AUDAPACK", r"V:\code\AUDAPACK")
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, "AUDAPACK | Claude Code", "chrome.exe")]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.refresh([p1], create_default_launchers()) == []


def test_untracked_browsers_naming_agents_and_projects_are_all_rejected(tmp_path):
    p1 = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend(
        [
            NativeWindow(1, 10, "Project A | ZCode", "brave.exe"),
            NativeWindow(2, 20, "Project A | OpenCode", "msedge.exe"),
            NativeWindow(3, 30, "Project A | Claude Code", "discord.exe"),
            NativeWindow(4, 40, "Project A | Codex", "firefox.exe"),
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.refresh([p1], create_default_launchers()) == []


def test_a_terminal_agent_window_naming_a_project_and_claude_is_still_detected(tmp_path):
    """The trust boundary is the PROCESS, not the words in the title."""
    p1 = project("audapack", "AUDAPACK", r"V:\code\AUDAPACK")
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, "AUDAPACK | Claude Code", "powershell.exe")]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    instances = monitor.refresh([p1], create_default_launchers())

    assert [(item.launcher_id, item.project_id) for item in instances] == [("claude", "audapack")]


def test_a_tracked_browser_launch_record_is_still_honoured(tmp_path):
    """A launcher that deliberately starts a browser keeps its record."""
    p1 = project("p1", "Project One", r"V:\code\one")
    backend = FakeWindowBackend([NativeWindow(11, 44, "Project One | OpenCode", "chrome.exe")])
    backend.alive[44] = True
    backend.tokens[44] = 7
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(44, "opencode", p1)

    instances = monitor.refresh([p1], create_default_launchers())

    assert [(item.launcher_id, item.project_id, item.tracked) for item in instances] == [
        ("opencode", "p1", True)
    ]


# --------------------------------------------------------------------------
# T-179 defect 2: configured launcher identity outranks built-in tokens
# --------------------------------------------------------------------------

def test_a_configured_launcher_beats_a_generic_builtin_token():
    my_open = LauncherConfig(id="my_open", name="OpenCode Special", short_label="MO")
    launchers = [my_open] + create_default_launchers()

    assert InstanceMonitor._launcher_from_title("Project | OpenCode Special", launchers) == "my_open"


def test_configured_claude_and_zcode_names_keep_their_own_ids():
    my_claude = LauncherConfig(id="my_claude", name="Claude Code Pro", short_label="MC")
    my_zcode = LauncherConfig(id="my_zcode", name="ZCode Work", short_label="MZ")
    launchers = [my_claude, my_zcode] + create_default_launchers()

    assert InstanceMonitor._launcher_from_title("Project | Claude Code Pro", launchers) == "my_claude"
    assert InstanceMonitor._launcher_from_title("Project | ZCode Work", launchers) == "my_zcode"


def test_the_default_configured_launchers_still_resolve_to_their_own_ids():
    launchers = create_default_launchers()

    assert InstanceMonitor._launcher_from_title("Project | OpenCode", launchers) == "opencode"
    assert InstanceMonitor._launcher_from_title("Project | Codex (main_codex)", launchers) == "main_codex"
    assert InstanceMonitor._launcher_from_title("Project | Codex (main_codex2)", launchers) == "main_codex2"
    assert (
        InstanceMonitor._launcher_from_title("Project | Codex (main_codex3_free)", launchers)
        == "main_codex3_free"
    )


def test_unconfigured_claude_and_zcode_fall_back_to_their_fallback_ids():
    launchers = create_default_launchers()

    assert InstanceMonitor._launcher_from_title("Project | Claude Code", launchers) == "claude"
    assert InstanceMonitor._launcher_from_title("Project | ZCode", launchers) == "zcode"


# --------------------------------------------------------------------------
# T-179 project attribution matrix
# --------------------------------------------------------------------------

def test_attribution_matrix_maps_each_launcher_to_its_real_project(tmp_path):
    pa = project("pa", "Project A", r"V:\code\a")
    pb = project("pb", "Project B", r"V:\code\b", slot=2)
    backend = FakeWindowBackend(
        [
            NativeWindow(1, 10, r"Project A | OpenCode | V:\code\a", "powershell.exe"),
            NativeWindow(2, 20, r"Project B | ZCode | V:\code\b", "powershell.exe"),
            NativeWindow(3, 30, r"Project A | Claude Code | V:\code\a", "powershell.exe"),
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    instances = monitor.refresh([pa, pb], create_default_launchers())

    assert {(item.launcher_id, item.project_id) for item in instances} == {
        ("opencode", "pa"),
        ("zcode", "pb"),
        ("claude", "pa"),
    }


def test_a_tracked_audapack_record_wins_over_a_conflicting_title(tmp_path):
    pa = project("pa", "Project A", r"V:\code\a")
    pb = project("pb", "Project B", r"V:\code\b", slot=2)
    backend = FakeWindowBackend([NativeWindow(1, 10, "Project A | OpenCode", "powershell.exe")])
    backend.alive[10] = True
    backend.tokens[10] = 5
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(10, "opencode", pb)

    instances = monitor.refresh([pa, pb], create_default_launchers())

    assert [(item.launcher_id, item.project_id, item.tracked) for item in instances] == [
        ("opencode", "pb", True)
    ]


def test_an_explicit_title_identity_wins_over_an_ambiguous_pending_adoption(tmp_path):
    """A window that names Project A is not adopted into Project B's pending launch."""
    pa = project("pa", "Project A", r"V:\code\a")
    pb = project("pb", "Project B", r"V:\code\b", slot=2)
    backend = FakeWindowBackend(
        [NativeWindow(1, 10, r"Project A | OpenCode | V:\code\a", "powershell.exe")]
    )
    backend.alive[20] = True
    backend.tokens[20] = 9
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(20, "opencode", pb)

    instances = monitor.refresh([pa, pb], create_default_launchers())

    assert {(item.project_id, item.tracked, item.state) for item in instances} == {
        ("pa", False, "running"),
        ("pb", True, "starting"),
    }


# ---------------------------------------------------------------------------
# SRC-081 / APP-CLI-001 TARGET L: fallback window recognition stays truthful
# ---------------------------------------------------------------------------


def test_generic_external_claude_stays_generic_not_claude1_or_claude2(tmp_path):
    """A generic 'Claude Code' window is fallback `claude`, never a guess of
    Claude 1 / Claude 2; a managed Claude 1 console keeps its exact id."""
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend(
        [
            NativeWindow(11, 41, r"Project A | Claude Code | V:\code\a", "claude.exe"),
            NativeWindow(12, 42, r"Project A | Claude 1 | V:\code\a | tok42", "powershell.exe"),
        ]
    )
    backend.alive[41] = True
    backend.alive[42] = True
    backend.tokens[42] = 1
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(42, "claude1", pa, correlation_token="tok42")

    instances = monitor.refresh([pa], create_default_launchers())

    generic = next(item for item in instances if item.pid == 41)
    managed = next(item for item in instances if item.pid == 42)
    assert generic.launcher_id == "claude"  # generic fallback identity
    assert generic.launcher_id not in ("claude1", "claude2")
    assert generic.tracked is False
    assert managed.launcher_id == "claude1"  # exact registered identity
    assert managed.tracked is True


def test_generic_zcode_fallback_and_configured_zcode_share_one_identity(tmp_path):
    """External ZCode windows and AUDAPACK-launched ones are ONE identity --
    the fallback detector never mints a duplicate/conflicting id (TARGET E/L)."""
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend(
        [
            NativeWindow(21, 51, "ZCode — Project A", "ZCode.exe"),
            NativeWindow(22, 52, r"Project A | ZCode | V:\code\a | tok52", "powershell.exe"),
        ]
    )
    backend.alive[51] = True
    backend.alive[52] = True
    backend.tokens[52] = 1
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert monitor.track_launch(52, "zcode", pa, correlation_token="tok52")

    instances = monitor.refresh([pa], create_default_launchers())

    generic = next(item for item in instances if item.pid == 51)
    managed = next(item for item in instances if item.pid == 52)
    assert generic.launcher_id == managed.launcher_id == "zcode"
    ids = {item.launcher_id for item in instances}
    assert ids == {"zcode"}  # no "ZCode"/"zcode-desktop"/fallback twin identities


def test_browser_windows_with_provider_names_are_never_agent_consoles(tmp_path):
    """Claude 1 / Antigravity / ZCode words in a browser title are content."""
    pa = project("pa", "Project A", r"V:\code\a")
    backend = FakeWindowBackend(
        [
            NativeWindow(31, 61, "Claude 1 — Project A — ChatGPT", "chrome.exe"),
            NativeWindow(32, 62, "Antigravity docs — Project A", "brave.exe"),
            NativeWindow(33, 63, "ZCode pricing — Project A", "msedge.exe"),
            NativeWindow(34, 64, "Antigravity — Project A", "firefox.exe"),
        ]
    )
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    instances = monitor.refresh([pa], create_default_launchers())

    assert instances == []
