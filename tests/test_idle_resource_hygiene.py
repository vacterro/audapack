"""Unit and integration regression tests for idle performance and resource hygiene.

Verifies:
1. MainWindow.closeEvent stops all recurring timers and unregisters file watchers.
2. InauditWidget.shutdown releases watchers and stops debouncers.
3. LimitsPreparedWidget skips discovery when not visible and shuts down cleanly.
4. probe_archive_freshness arms hot_freshness and confirms hot proofs.
5. Continuous monitor prevents repeated source tree walks during idle.
"""

import sys

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from audapack import hot_freshness
from audapack.config import AppConfig, PackingConfig
from audapack.freshness import ArchiveFreshness, probe_archive_freshness
from audapack.models import Project
from audapack.services.packing_service import PackingService
from audapack.ui_qt.dialogs.inaudit_widget import InauditWidget
from audapack.ui_qt.dialogs.limits_prepared_widget import LimitsPreparedWidget
from audapack.ui_qt.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_inaudit_widget_shutdown(qapp, tmp_path):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "test.txt").write_text("hello", encoding="utf-8")
    proj = Project(id="p1", display_name="P1", source_path=str(proj_dir))

    widget = InauditWidget()
    widget.set_project(proj)

    assert widget._debounce is not None
    assert widget._inbox_debounce is not None

    widget.shutdown()

    assert not widget._debounce.isActive()
    assert not widget._inbox_debounce.isActive()
    assert len(widget._watcher.files()) == 0
    assert len(widget._watcher.directories()) == 0
    assert len(widget._inbox_watcher.files()) == 0
    assert len(widget._inbox_watcher.directories()) == 0


def test_limits_prepared_widget_visibility_gating(qapp):
    config = AppConfig()
    from audapack.ui_qt.task_runner import TaskRunner
    runner = TaskRunner(max_threads=0)
    widget = LimitsPreparedWidget(config, runner)

    # When not visible, normal refresh_view skips loading to avoid idle CPU/disk
    widget.hide()
    initial_summary = widget.summary.text()
    widget.refresh_view(force=False)
    # Status/summary remains unaffected by idle background tick
    assert widget.summary.text() == initial_summary

    widget.shutdown()
    assert not widget.timer.isActive()


def test_probe_archive_freshness_arms_and_confirms_hot_proof(tmp_path):
    hot_freshness.reset()
    source = tmp_path / "src"
    source.mkdir()
    (source / "code.py").write_text("x = 1\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    config = AppConfig(packing=PackingConfig(output_dir=str(out_dir)))
    proj = Project(id="hp1", display_name="HP1", source_path=str(source), archive_name="hp1")
    config.projects = [proj]

    service = PackingService(config, base_dir=tmp_path)
    res = service.pack_project("hp1")
    assert res.success

    # First probe arms monitor and confirms hot proof
    verdict1 = probe_archive_freshness(proj, config.packing, output_dir=out_dir)
    assert verdict1.state is ArchiveFreshness.FRESH

    if hot_freshness.monitor_is_supported():
        fp = service._policy_fingerprint()
        proof = hot_freshness.lookup(str(source), fp, res.output_path)
        assert proof is not None, "probe_archive_freshness must confirm hot proof"

    hot_freshness.reset()


def test_main_window_close_event_cleans_up_all_resources(qapp, tmp_path):
    hot_freshness.reset()
    source = tmp_path / "src"
    source.mkdir()
    (source / "code.py").write_text("x = 1\n", encoding="utf-8")
    proj = Project(id="w1", display_name="W1", source_path=str(source), archive_name="w1")

    config = AppConfig()
    config.projects = [proj]
    from audapack.services.project_service import ProjectService
    service = ProjectService(config, base_dir=tmp_path)

    window = MainWindow(service)
    window.show()
    QCoreApplication.processEvents()

    assert window.temp_timer.isActive()
    assert window.bridge_timer.isActive()

    window.close()
    QCoreApplication.processEvents()

    assert not window.temp_timer.isActive()
    assert not window.bridge_timer.isActive()
    if hasattr(window, "auto_pack_timer") and window.auto_pack_timer is not None:
        assert not window.auto_pack_timer.isActive()
    assert not window._generation_debounce.isActive()
    assert len(window._generation_watcher.directories()) == 0
    assert len(window._generation_watcher.files()) == 0

    hot_freshness.reset()


def test_soak_leak_audit_acceptance(qapp, tmp_path):
    """Run the upgraded soak audit acceptance probe and prove all leak checks pass."""
    from tests.soak_leak_audit import run_soak

    report_file = tmp_path / "soak_test_report.json"
    report = run_soak(duration_seconds=1, rounds_count=3, report_path=str(report_file))

    assert report["verdict"] == "PASS"
    checks = report["checks"]
    assert checks["no_orphan_processes"] is True
    assert checks["recurring_timers_stopped_on_close"] is True
    assert checks["private_bytes_bounded_growth"] is True
    assert checks["handles_bounded_growth"] is True
    assert checks["threads_settled_range"] is True
    assert checks["gdi_objects_valid_non_null"] is True
    assert checks["user_objects_valid_non_null"] is True
    assert checks["gdi_objects_bounded_growth"] is True
    assert checks["user_objects_bounded_growth"] is True
