"""CORE-002 C5 regression: managed delivery cannot wedge or re-enqueue
because tracker persistence failed.

The durable SAIPEN assignment is the PRIMARY commit. Once the assigned layer
is read back, the draft completes: editor mutable, pending cleared, draft
identity dropped, and the tracker attempt becomes a warning + durable repair
intent. A tracker failure must never escape TaskRunner on_success and leave
managed_draft_delivery_pending=True / editor readOnly=True.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import audapack.inaudit as inaudit  # noqa: E402
from audapack.config import AppConfig, AuditsConfig  # noqa: E402
from audapack.models import Project  # noqa: E402
from audapack.services.project_service import ProjectService  # noqa: E402
from audapack.ui_qt.main_window import MainWindow  # noqa: E402
from tests.test_inaudit_txn import _swap  # noqa: E402


def _wait_runner(window, qapp, key="inaudit:assign"):
    deadline = time.monotonic() + 6
    while window.inaudit_widget._task_runner.is_running(key) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    for _ in range(60):
        qapp.processEvents()
        time.sleep(0.01)


@pytest.fixture
def managed_window(tmp_path, qapp, monkeypatch):
    from tests.test_saipen_transport import _bind, _fake_cli

    home, _enqueue_calls = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
    managed_src = tmp_path / "managed_src"
    managed_src.mkdir(parents=True, exist_ok=True)
    _bind(managed_src, home)
    cfg = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[Project(id="managed", display_name="_AUDAPACK", source_path=str(managed_src),
                          priority_group="MAIN0", slot=1)],
    )
    svc = ProjectService(cfg, base_dir=tmp_path / "service")
    window = MainWindow(svc)
    window.inaudit_widget.set_project(svc.get_project("managed"))
    yield window
    try:
        window.close()
    except Exception:
        pass


def _wait_runner_bind(window, qapp):
    deadline = time.monotonic() + 6
    while window.inaudit_widget._task_runner.is_running("inaudit:bind") and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def test_tracker_failure_after_delivery_never_wedges_or_reenqueues(managed_window, qapp):
    window = managed_window
    widget = window.inaudit_widget
    store = widget._capture_store
    managed_project = window._service.get_project("managed")
    calls = {"n": 0}
    assigned_target = Path(managed_project.source_path) / "audit" / "40.md"

    real_assign = store.assign

    def _assign(capture_id, project_id, projects, action="", after_assign=None):
        calls["n"] += 1
        return real_assign(capture_id, project_id, projects, action=action)

    store.assign = _assign

    widget.create_and_focus_layer()
    widget.editor.setPlainText("durable managed body")
    QApplication.processEvents()

    def _broken_tracker(path, numbers):
        raise OSError("simulated tracker persistence failure")

    original_tracker = inaudit._save_user_layers
    inaudit._save_user_layers = _broken_tracker
    try:
        widget._on_save()
        _wait_runner(window, qapp)
    finally:
        inaudit._save_user_layers = original_tracker

    # SAIPEN called exactly once; assigned layer exists exactly once.
    assert calls["n"] == 1
    assert assigned_target.is_file()
    # Callback completed: editor writable, pending false, draft cleared.
    assert widget.managed_draft_delivery_pending is False
    assert widget.managed_draft_capture_id is None
    assert not widget.editor.isReadOnly()
    assert widget._editor_path == assigned_target.resolve()
    assert not widget._dirty
    # Tracker repair pending warning is surfaced (not a failure).
    assert "repair" in widget.status.text().lower()
    # The durable repair intent exists and later converges the tracker.
    queue = inaudit._repair_queue_path(managed_project)
    assert queue.is_file()
    assert inaudit.replay_tracker_repairs(managed_project) == ""
    assert inaudit.last_user_layer(managed_project) == 40  # exact SAIPEN-returned layer


def test_tracker_and_queue_failure_surfaces_truthful_warning(managed_window, qapp):
    """CORE-002 B4: when BOTH the tracker and the repair queue are unwritable,
    managed delivery still completes and the operator is told the truth."""
    window = managed_window
    widget = window.inaudit_widget
    store = widget._capture_store
    calls = {"n": 0}

    real_assign = store.assign

    def _assign(capture_id, project_id, projects, action="", after_assign=None):
        calls["n"] += 1
        return real_assign(capture_id, project_id, projects, action=action)

    store.assign = _assign

    widget.create_and_focus_layer()
    widget.editor.setPlainText("unpersistable tracker body")
    QApplication.processEvents()

    from audapack.ui_qt.dialogs import inaudit_widget as iw

    with _swap(iw, "record_user_layer", lambda project, number: inaudit.TRACKING_REPAIR_UNPERSISTED):
        widget._on_save()
        _wait_runner(window, qapp)

    assert calls["n"] == 1  # SAIPEN enqueued exactly once
    assert widget.managed_draft_delivery_pending is False
    assert not widget.editor.isReadOnly()
    text = widget.status.text().lower()
    assert "warning" in text
    assert "repair pending" not in text


def test_tracker_exception_inside_callback_still_unwedges(managed_window, qapp):
    """Even an unexpected exception in the completion callback must restore
    the draft state (never leave readOnly + pending after delivery)."""
    window = managed_window
    widget = window.inaudit_widget

    widget.create_and_focus_layer()
    widget.editor.setPlainText("callback boom body")
    QApplication.processEvents()

    def _boom(*_a, **_k):
        raise RuntimeError("simulated callback exception")

    with _swap(widget, "_managed_delivery_finish_impl", _boom):
        widget._on_save()
        _wait_runner(window, qapp)

    assert widget.managed_draft_delivery_pending is False
    assert widget.managed_draft_capture_id is None
    assert not widget.editor.isReadOnly()
