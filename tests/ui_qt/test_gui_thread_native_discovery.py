"""T-216: the Qt GUI thread may never perform unbounded native discovery.

The escaped production regression: AUDAPACK Project Room went "Not Responding"
right after a successful managed OpenCode launch. Three synchronous native
paths ran inside the Qt event loop:

- ``MainWindow._on_title_heartbeat`` -> ``TitleGuardian.heartbeat`` ->
  ``_resolve_for`` -> ``resolve_hwnds`` + ``resolve_hwnds_by_token``, i.e.
  ~2*N full ``EnumWindows`` scans every 1000 ms;
- ``_on_title_heartbeat`` -> ``_refresh_instance_snapshot`` -> the FULL
  ``InstanceMonitor.refresh`` (launch-record disk read, process metadata,
  window enumeration, command lines, SAIPEN STATE/LOG reads);
- ``_on_open_with_launcher`` -> ``OpenCodeLaunchPolicy.admit`` -> the Fleet
  preflight subprocess (``timeout=30``).

Each of these now runs on the shared coalesced background lane
(``TaskRunner``), and the GUI callback only installs the completed immutable
snapshot. These tests pin the INVARIANT, not an implementation detail: a native
backend that blocks for seconds must not stall the Qt event loop, and 100
heartbeats with bound HWNDs must perform ZERO desktop scans.

TARGET M/N/O/Q/P live here.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QTimer

from audapack.config import AppConfig, AuditsConfig, create_default_launchers
from audapack.instances import InstanceMonitor, NativeWindow
from audapack.opencode_launch import OpenCodeAdmission
from audapack.services.project_service import ProjectService
from audapack.title_guardian import TitleGuardian
from audapack.ui_qt.main_window import MainWindow
from tests.ui_qt.test_launcher_focus_reuse import (
    FakeTitleBackend,
    FakeWindowBackend,
    project,
)


def _run_gui_for(qapp, seconds: float) -> list[float]:
    """Return the intervals observed by a 50 ms GUI sentinel timer.

    A stall of N seconds shows up as one interval >= N; a healthy loop keeps
    every interval near 50 ms. This is the REAL event-loop liveness signal the
    operator saw Windows use when the title bar said "Not Responding".
    """
    stamps: list[float] = [time.monotonic()]

    timer = QTimer()
    timer.setInterval(50)

    def _tick():
        stamps.append(time.monotonic())

    timer.timeout.connect(_tick)
    timer.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    timer.stop()
    return [b - a for a, b in zip(stamps, stamps[1:], strict=False)]


def _window(tmp_path, projects, backend=None, *, managed=False):
    if managed:
        for proj in projects:
            root = Path(proj.source_path)
            (root / ".saipen").mkdir(parents=True, exist_ok=True)
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=list(projects),
        launchers=create_default_launchers(),
    )
    config.ui.auto_copy_gg_on_launch = False
    service = ProjectService(config, base_dir=tmp_path)
    win = MainWindow(service)
    backend = backend or FakeWindowBackend()
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    win._instance_monitor = monitor
    win._instance_manager.monitor = monitor
    win._title_guardian = TitleGuardian(backend=FakeTitleBackend())
    win._title_guardian_timer = None
    return win, backend, monitor


# ---------------------------------------------------------------------------
# TARGET M -- slow window scan must not stall the Qt event loop
# ---------------------------------------------------------------------------

def test_slow_s_native_scan_never_stalls_the_gui_event_loop(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    win, backend, _monitor = _window(tmp_path, [pa])

    class SlowBackend(FakeWindowBackend):
        def list_windows(self):
            time.sleep(0.4)
            return super().list_windows()

    slow = SlowBackend()
    win._instance_monitor.backend = slow
    try:
        # The launch-like trigger: post-launch discovery is requested, and the
        # native backend blocks for 400 ms inside the worker.
        win.request_instance_refresh("test-slow-scan")
        intervals = _run_gui_for(qapp, 0.8)
        assert max(intervals) < 0.25, f"GUI stalled during a slow native scan: {max(intervals):.3f}s"
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET N -- slow Fleet preflight must not stall the Qt event loop
# ---------------------------------------------------------------------------

def test_slow_fleet_preflight_keeps_the_gui_responsive(tmp_path, qapp):
    root = tmp_path / "a"
    root.mkdir()
    (root / ".saipen").mkdir()
    (root / ".saipen" / "IDENTITY.md").write_text("id", encoding="utf-8")
    pa = project("pa", "Project A", str(root))
    win, backend, monitor = _window(tmp_path, [pa], managed=True)
    backend.alive[500] = True
    backend.tokens[500] = 1
    admission = OpenCodeAdmission(
        True, root, ("python.exe", "bound/saipen.py", "launch", "opencode"),
        {"kind": "saipen-opencode-v1", "project_root": str(root),
         "entrypoint": "bound/saipen.py", "project_identity": str(root).lower(),
         "project_lineage": "lineage-a", "actor": "buffy"},
    )

    seen: list[str] = []

    def _slow_admit(_self, _path, *, custom_template):  # noqa: ANN001
        seen.append("start")
        time.sleep(0.4)
        seen.append("end")
        return admission

    try:
        with (
            patch("audapack.ui_qt.main_window.OpenCodeLaunchPolicy.admit", _slow_admit),
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            win._on_open_with_launcher(pa, "opencode")
            assert "CHECK" in win.statusBar().currentMessage()
            intervals = _run_gui_for(qapp, 0.8)
        assert max(intervals) < 0.25, f"GUI stalled during Fleet preflight: {max(intervals):.3f}s"
        assert seen == ["start", "end"], "the preflight ran, off the GUI thread"
        assert launch.call_count == 1, "the result callback continued the launch path"
    finally:
        win.close()


def test_repeated_managed_clicks_never_run_two_preflights(tmp_path, qapp):
    root = tmp_path / "a"
    root.mkdir()
    (root / ".saipen").mkdir()
    (root / ".saipen" / "IDENTITY.md").write_text("id", encoding="utf-8")
    pa = project("pa", "Project A", str(root))
    win, backend, _monitor = _window(tmp_path, [pa], managed=True)
    backend.alive[500] = True
    backend.tokens[500] = 1
    admission = OpenCodeAdmission(
        True, root, ("python.exe", "bound/saipen.py", "launch", "opencode"),
        {"kind": "saipen-opencode-v1", "project_root": str(root),
         "entrypoint": "bound/saipen.py", "project_identity": str(root).lower(),
         "project_lineage": "lineage-a", "actor": "buffy"},
    )
    calls: list[int] = []

    def _admit(_self, _path, *, custom_template):  # noqa: ANN001
        calls.append(1)
        time.sleep(0.2)
        return admission

    try:
        with (
            patch("audapack.ui_qt.main_window.OpenCodeLaunchPolicy.admit", _admit),
            patch.object(win, "_launch_bound_opencode") as launch,
        ):
            for _ in range(6):
                win._on_open_with_launcher(pa, "opencode")
                qapp.processEvents()
            deadline = time.monotonic() + 3.0
            while launch.call_count == 0 and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
        assert len(calls) == 1, f"6 rapid clicks must be single-flight, saw {len(calls)}"
        assert launch.call_count == 1
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET P -- 100 heartbeats with bound HWNDs: zero desktop scans
# ---------------------------------------------------------------------------

def test_one_hundred_heartbeats_with_bound_hwnds_perform_zero_scans(tmp_path, qapp):
    root = tmp_path / "a"
    root.mkdir()
    (root / ".saipen").mkdir()
    (root / ".saipen" / "IDENTITY.md").write_text("id", encoding="utf-8")
    pa = project("pa", "Project A", str(root))
    win, backend, monitor = _window(tmp_path, [pa], managed=True)
    backend.alive[500] = True
    backend.tokens[500] = 1
    monitor.track_launch(500, "opencode", pa, correlation_token="OC-aaa111")
    win._title_guardian.register(
        500, f"Project A | OpenCode YOLO | {root} | OC-aaa111",
        correlation_token="OC-aaa111", launcher_id="opencode", project_id="pa",
    )
    backend.windows = [NativeWindow(11, 500, f"Project A | OpenCode | {root}", "powershell.exe")]
    win._refresh_instance_snapshot()
    assert win._title_guardian.binding_count == 1

    scans = 0
    original_list = backend.list_windows
    title_backend = win._title_guardian.backend
    title_scans = 0
    original_resolve = title_backend.resolve_hwnds

    def _count_scan():
        nonlocal scans
        scans += 1
        return original_list()

    def _count_resolve(pid):
        nonlocal title_scans
        title_scans += 1
        return original_resolve(pid)

    backend.list_windows = _count_scan
    title_backend.resolve_hwnds = _count_resolve
    try:
        # The heartbeat lane is a real background worker; drive it 100 times.
        for _ in range(100):
            win._run_title_heartbeat()
        assert title_scans == 0, f"heartbeat performed {title_scans} desktop scans"
        assert scans == 0, f"heartbeat performed {scans} instance scans"
        assert win._title_guardian.binding_count == 1
    finally:
        win.close()


# ---------------------------------------------------------------------------
# TARGET E/G -- the GUI installs a complete snapshot; the worker builds it
# ---------------------------------------------------------------------------

def test_scan_returns_an_immutable_snapshot_without_mutating_instances(tmp_path):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    backend = FakeWindowBackend(
        [NativeWindow(11, 44, r"Project A | OpenCode | V:\code\a", "powershell.exe")]
    )
    backend.alive[44] = True
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    monitor.track_launch(44, "opencode", pa)
    try:
        before = list(monitor.instances)
        snapshot = monitor.scan([pa], create_default_launchers())
        assert list(monitor.instances) == before, "scan must not mutate live state"
        assert isinstance(snapshot.instances, tuple)
        monitor.apply_snapshot(snapshot)
        assert [item.pid for item in monitor.instances] == [item.pid for item in snapshot.instances]
    finally:
        pass


def test_a_launch_tracked_during_a_scan_is_not_lost_by_the_commit(tmp_path):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    backend = FakeWindowBackend()
    backend.alive[44] = True
    backend.tokens[44] = 1
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    real_list = backend.list_windows

    def _track_midway():
        # Simulate the operator launching a second agent while the worker scans.
        monitor.track_launch(44, "opencode", pa)
        return real_list()

    backend.list_windows = _track_midway
    snapshot = monitor.scan([pa], create_default_launchers())
    monitor.apply_snapshot(snapshot)
    assert 44 in monitor.records, "a launch tracked during the scan must survive the commit"
