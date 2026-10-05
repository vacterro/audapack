from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from audapack.instances import InstanceMonitor
from audapack.models import Project
from audapack.opencode_launch import OpenCodeAdmission
from audapack.ui_qt.main_window import MainWindow
from tests.ui_qt.test_launcher_focus_reuse import FakeWindowBackend, _settle
from tests.ui_qt.test_launcher_focus_reuse import make_window as _base_make_window


def make_window(tmp_path: Path, projects: list[Project], backend: FakeWindowBackend | None = None):
    win, be, monitor = _base_make_window(tmp_path, projects, backend=backend)
    win._opencode_startup_grace_sec = 0.05
    return win, be, monitor


def project(pid: str, name: str, source_path: str) -> Project:
    return Project(id=pid, display_name=name, source_path=source_path)


def _bound_admission(root: Path, actor: str = "buffy", lineage: str = "lineage-a") -> OpenCodeAdmission:
    return OpenCodeAdmission(
        True,
        root,
        ("python.exe", "bound/saipen.py", "--agent", actor, "--project-root", str(root), "launch", "opencode", "--", ".", "--auto"),
        {
            "kind": "saipen-opencode-v1",
            "project_root": str(root),
            "entrypoint": "bound/saipen.py",
            "project_identity": str(root).replace("/", "\\").lower(),
            "project_lineage": lineage,
            "actor": actor,
        },
    )


def test_1_wrapper_surviving_startup_grace_is_tracked_as_launched(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    admission = _bound_admission(tmp_path)
    backend.alive[601] = True
    backend.tokens[601] = 10

    mock_proc = MagicMock()
    mock_proc.pid = 601
    mock_proc.poll.return_value = None

    try:
        with patch("subprocess.Popen", return_value=mock_proc):
            win._launch_bound_opencode(pa, admission)

        assert 601 in monitor.records
        assert monitor.records[601].launcher_id == "opencode"
        assert monitor.records[601].project_id == "pa"
        assert "✓ Launched SAIPEN-bound OpenCode" in win.statusBar().currentMessage()
    finally:
        win.close()


def test_2_immediate_exit_nonzero_reported_as_launch_failure(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    admission = _bound_admission(tmp_path)

    mock_proc = MagicMock()
    mock_proc.pid = 602
    mock_proc.poll.return_value = 1

    try:
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("audapack.ui_qt.main_window.QMessageBox.warning") as mock_warn,
        ):
            win._launch_bound_opencode(pa, admission)

        mock_warn.assert_called_once()
        title, text = mock_warn.call_args.args[1], mock_warn.call_args.args[2]
        assert "OpenCode launch failed" in title
        assert "exit=1" in text
        assert "OpenCode launch failed (exit=1" in win.statusBar().currentMessage()
        assert 602 not in monitor.records
    finally:
        win.close()


def test_3_immediate_exit_zero_without_live_instance_reported_as_failure(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    admission = _bound_admission(tmp_path)

    mock_proc = MagicMock()
    mock_proc.pid = 603
    mock_proc.poll.return_value = 0

    try:
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("audapack.ui_qt.main_window.QMessageBox.warning") as mock_warn,
        ):
            win._launch_bound_opencode(pa, admission)

        mock_warn.assert_called_once()
        text = mock_warn.call_args.args[2]
        assert "exit=0" in text
        assert "without a live OpenCode instance" in text
        assert 603 not in monitor.records
    finally:
        win.close()


def test_4_failed_launch_creates_no_stale_instance_monitor_record(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    admission = _bound_admission(tmp_path)

    mock_proc = MagicMock()
    mock_proc.pid = 604
    mock_proc.poll.return_value = 2

    try:
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("audapack.ui_qt.main_window.QMessageBox.warning"),
        ):
            win._launch_bound_opencode(pa, admission)

        assert monitor.records == {}
        assert monitor.for_project(pa.id) == []
    finally:
        win.close()


def test_5_title_guard_setup_failure_does_not_prevent_canonical_host_execution(tmp_path):
    cmd_file = tmp_path / "host_marker.txt"
    title_bad = "TestTitle"
    script = (
        f"$managedTitle = '{title_bad}'; "
        "try { "
        "throw [System.InvalidOperationException]::new('Forced title failure'); "
        "} catch {}; "
        f"[System.IO.File]::WriteAllText('{str(cmd_file).replace(chr(92), chr(47))}', 'HOST_EXECUTED'); "
        "exit 0"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert cmd_file.is_file()
    assert cmd_file.read_text(encoding="utf-8") == "HOST_EXECUTED"


def test_6_normal_bound_valid_launch_remains_alive(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    backend.alive[606] = True
    backend.tokens[606] = 55
    admission = _bound_admission(tmp_path)

    mock_proc = MagicMock()
    mock_proc.pid = 606
    mock_proc.poll.return_value = None

    try:
        with patch("subprocess.Popen", return_value=mock_proc):
            win._launch_bound_opencode(pa, admission)

        rec = monitor.records.get(606)
        assert rec is not None
        assert rec.saipen_binding == admission.binding
        assert rec.project_id == "pa"
        assert rec.launcher_id == "opencode"
    finally:
        win.close()


def test_7_recovery_state_launch_remains_alive(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    backend.alive[607] = True
    backend.tokens[607] = 56
    base = _bound_admission(tmp_path)
    admission = OpenCodeAdmission(
        base.managed,
        base.cwd,
        base.command,
        base.binding,
        {
            "classification": "BOUND_RECOVERY_REQUIRED_SAFE",
            "reason_code": "BOARD_RECORD_OVERSIZE",
            "canonical_next_command": "saipen ticket",
        },
    )

    mock_proc = MagicMock()
    mock_proc.pid = 607
    mock_proc.poll.return_value = None

    try:
        with patch("subprocess.Popen", return_value=mock_proc):
            win._launch_bound_opencode(pa, admission)

        assert 607 in monitor.records
        assert "Recovery: BOARD_RECORD_OVERSIZE" in win.statusBar().currentMessage()
        assert "saipen ticket" in win.statusBar().currentMessage()
    finally:
        win.close()


def test_8_force_new_second_instance_remains_alive(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    backend.alive[608] = True
    backend.alive[609] = True
    backend.tokens[608] = 101
    backend.tokens[609] = 102
    admission = _bound_admission(tmp_path)

    mock_proc_1 = MagicMock()
    mock_proc_1.pid = 608
    mock_proc_1.poll.return_value = None

    mock_proc_2 = MagicMock()
    mock_proc_2.pid = 609
    mock_proc_2.poll.return_value = None

    try:
        with patch("subprocess.Popen", side_effect=[mock_proc_1, mock_proc_2]):
            win._launch_bound_opencode(pa, admission)
            win._launch_bound_opencode(pa, admission)

        assert 608 in monitor.records
        assert 609 in monitor.records
        assert len(monitor.for_project(pa.id)) == 2
    finally:
        win.close()


def test_9_two_live_instances_remain_independently_attributable(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path / "a"))
    pb = project("pb", "Project B", str(tmp_path / "b"))
    win, backend, monitor = make_window(tmp_path, [pa, pb])
    backend.alive[610] = True
    backend.alive[611] = True
    backend.tokens[610] = 201
    backend.tokens[611] = 202
    admission_a = _bound_admission(tmp_path / "a", actor="buffy")
    admission_b = _bound_admission(tmp_path / "b", actor="antigravity")

    mock_proc_a = MagicMock()
    mock_proc_a.pid = 610
    mock_proc_a.poll.return_value = None

    mock_proc_b = MagicMock()
    mock_proc_b.pid = 611
    mock_proc_b.poll.return_value = None

    try:
        with patch("subprocess.Popen", side_effect=[mock_proc_a, mock_proc_b]):
            win._launch_bound_opencode(pa, admission_a)
            win._launch_bound_opencode(pb, admission_b)

        rec_a = monitor.records[610]
        rec_b = monitor.records[611]
        assert rec_a.project_id == "pa"
        assert rec_b.project_id == "pb"
        assert rec_a.correlation_token != rec_b.correlation_token
        assert rec_a.saipen_binding["actor"] == "buffy"
        assert rec_b.saipen_binding["actor"] == "antigravity"
    finally:
        win.close()


def test_10_startup_diagnostic_is_bounded_and_does_not_dump_raw_secrets():
    raw_error = (
        "Fatal error in SAIPEN CLI: "
        "Authorization header bearer=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-kJ1 failed; "
        "API key secret=sk-ant-api03-abcdef1234567890; "
        "set SECRET_KEY=super_secret_environment_dump_value_here\n"
        + "Extra detailed stack information " * 20
    )
    sanitized = MainWindow._sanitize_diagnostic(raw_error, max_length=150)
    assert len(sanitized) <= 150
    assert "sk-ant-api03" not in sanitized
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in sanitized
    assert "super_secret_environment_dump_value_here" not in sanitized
    assert "[REDACTED]" in sanitized
    assert sanitized.endswith("...")


def test_11_clean_interactive_tty_no_stderr_redirection_and_title_env(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    admission = _bound_admission(tmp_path)
    backend.alive[620] = True
    backend.tokens[620] = 77

    mock_proc = MagicMock()
    mock_proc.pid = 620
    mock_proc.poll.return_value = None

    try:
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            win._launch_bound_opencode(pa, admission)

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        script = cmd[-1]
        env = mock_popen.call_args.kwargs.get("env", {})

        # Target B: No stderr redirection
        assert " 2> " not in script
        # Target D: OPENCODE_DISABLE_TERMINAL_TITLE set in script & popen env
        assert "$env:OPENCODE_DISABLE_TERMINAL_TITLE = 'true'" in script
        assert env.get("OPENCODE_DISABLE_TERMINAL_TITLE") == "true"
        # Target D: No titleGuard timer loop
        assert "titleGuard" not in script
        # Target E: Single Title setup
        assert script.count("[Console]::Title = $managedTitle") == 1
    finally:
        win.close()


def test_12_delayed_premature_exit_during_grace_clears_registration(tmp_path, qapp):
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    win._opencode_startup_grace_sec = 0.2
    admission = _bound_admission(tmp_path)
    backend.alive[621] = True
    backend.tokens[621] = 88

    # Start alive, then die on second check
    poll_results = [None, 1]
    def mock_poll():
        if poll_results:
            return poll_results.pop(0)
        return 1

    mock_proc = MagicMock()
    mock_proc.pid = 621
    mock_proc.poll.side_effect = mock_poll

    try:
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("audapack.ui_qt.main_window.QMessageBox.warning") as mock_warn,
        ):
            win._launch_bound_opencode(pa, admission)
            # Initially registered as launch
            assert 621 in monitor.records

            # Run timer / event loop for watcher to fire
            for _ in range(10):
                QApplication.processEvents()
                time.sleep(0.03)

            # Watcher should have caught the exit, cleared registration, and warned
            assert 621 not in monitor.records
            mock_warn.assert_called_once()
            assert "OpenCode launch failed" in mock_warn.call_args.args[1]
    finally:
        win.close()


def test_13_instance_monitor_untrack_launch(tmp_path):
    pa = project("pa", "Project A", str(tmp_path))
    backend = FakeWindowBackend()
    backend.alive[622] = True
    monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")

    assert monitor.track_launch(622, "opencode", pa)
    assert 622 in monitor.records

    # Untracking non-existent PID returns False
    assert not monitor.untrack_launch(9999)

    # Untracking valid PID removes record and persists
    assert monitor.untrack_launch(622)
    assert 622 not in monitor.records

    # Re-reading records confirms disk persistence
    fresh_monitor = InstanceMonitor(backend=backend, record_path=tmp_path / "instances.json")
    assert 622 not in fresh_monitor.records


def test_recovery_verdict_survives_a_routine_bridge_poll(tmp_path, qapp):
    """A BOUND_RECOVERY_REQUIRED_SAFE launch is deliberately silent -- no modal --
    so the status bar is the operator's ONLY channel for the reason code and the
    canonical next command. The 4s bridge poll used to overwrite it on its next
    pass, and under load that landed inside the click, erasing the verdict before
    it was ever read. An unchanged bridge state now yields to whoever wrote last.
    """
    pa = project("pa", "Project A", str(tmp_path))
    win, backend, monitor = make_window(tmp_path, [pa])
    base = _bound_admission(tmp_path)
    admission = OpenCodeAdmission(
        base.managed,
        base.cwd,
        base.command,
        base.binding,
        {
            "classification": "BOUND_RECOVERY_REQUIRED_SAFE",
            "reason_code": "BOARD_RECORD_OVERSIZE",
            "canonical_next_command": "saipen ticket",
        },
    )
    mock_proc = MagicMock()
    mock_proc.pid = 731
    mock_proc.poll.return_value = None
    try:
        # One ordinary poll first, so the ambient line is genuinely on screen and
        # the click below is the only thing that changes it.
        _settle(qapp, win, "instances:refresh")
        win._refresh_audit_runs_async()
        _settle(qapp, win, "audit-runs:refresh")
        ambient = win.statusBar().currentMessage()
        assert win._bridge_status_text == ambient

        with patch("subprocess.Popen", return_value=mock_proc):
            win._launch_bound_opencode(pa, admission)
        verdict = win.statusBar().currentMessage()
        assert "Recovery: BOARD_RECORD_OVERSIZE" in verdict
        assert "saipen ticket" in verdict

        # Same bridge answer as before the click: the poll must not clobber it.
        win._refresh_audit_runs_async()
        _settle(qapp, win, "audit-runs:refresh")
        assert "Recovery: BOARD_RECORD_OVERSIZE" in win.statusBar().currentMessage()
        assert "saipen ticket" in win.statusBar().currentMessage()

        # A genuinely NEW bridge answer must still win -- the yield is for an
        # unchanged answer, never a blanket suppression of the ambient line.
        win._bridge_status_text = "a stale answer nobody wrote"
        win._refresh_audit_runs_async()
        _settle(qapp, win, "audit-runs:refresh")
        assert win.statusBar().currentMessage() == ambient
    finally:
        win.close()
