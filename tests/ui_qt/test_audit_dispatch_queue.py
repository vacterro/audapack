"""START AUDIT presses are one serialized lane, not one task per press."""

from __future__ import annotations

import pytest

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService


@pytest.fixture
def window(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id=f"p{i}", display_name=f"Project {i}",
                    source_path=str(tmp_path / f"p{i}"), priority_group="MAIN0", slot=i)
            for i in range(1, 7)
        ],
    )
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    yield win
    win.close()


def test_rapid_presses_collapse_into_one_batch(window):
    """Six presses in a burst must dispatch as one batch of six."""
    batches: list[list[str]] = []
    window._audit_runs.start_batch = lambda ids, profile: (batches.append(list(ids)) or [])
    window.task_runner.submit = lambda key, fn, on_success=None, on_error=None: fn()

    for index in range(1, 7):
        window._start_audit_projects([f"p{index}"], f"Project {index}")
    assert batches == []  # nothing dispatched while the debounce is collecting

    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()
    assert batches == [["p1", "p2", "p3", "p4", "p5", "p6"]]


def test_a_press_while_a_batch_runs_is_queued_not_dropped(window):
    batches: list[list[str]] = []
    window._audit_runs.start_batch = lambda ids, profile: (batches.append(list(ids)) or [])
    # Simulate a batch that is still in flight: the runner never calls back.
    window.task_runner.submit = lambda key, fn, on_success=None, on_error=None: fn()
    window.task_runner.is_running = lambda key: bool(batches)

    window._start_audit_projects(["p1"], "Project 1")
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()
    assert batches == [["p1"]]

    window._start_audit_projects(["p2"], "Project 2")
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()
    # Lane busy: p2 waits in the pending set instead of racing p1.
    assert batches == [["p1"]]
    assert window._audit_start_pending == ["p2"]


def test_the_same_project_is_never_queued_twice(window):
    window._audit_runs.start_batch = lambda ids, profile: []
    window._start_audit_projects(["p1"], "Project 1")
    window._start_audit_projects(["p1"], "Project 1")
    assert window._audit_start_pending == ["p1"]


def test_batch_is_capped_at_six_lanes(window):
    batches: list[list[str]] = []
    window._audit_runs.start_batch = lambda ids, profile: (batches.append(list(ids)) or [])
    window.task_runner.submit = lambda key, fn, on_success=None, on_error=None: fn()

    window._start_audit_projects([f"p{i}" for i in range(1, 7)] + ["p1"], "MAIN0")
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()
    assert len(batches) == 1 and len(batches[0]) == 6


def test_stale_widget_pool_is_named_in_the_status_bar():
    """Six idle windows and a queue that never moves must not look healthy."""
    from audapack.ui_qt.main_window import bridge_status_text, bridge_status_warning

    healthy = {"active_workers": 6, "max_workers": 6, "clean_workers": 6,
               "busy_workers": 0, "queued_jobs": 0, "active_jobs": 0, "blocked_jobs": 0}
    assert "STALE" not in bridge_status_text(healthy)
    assert bridge_status_warning(healthy) == ""

    stale = {**healthy, "clean_workers": 0, "active_workers": 0,
             "stale_widget_workers": 6, "required_widget_build": "0.0.25", "queued_jobs": 3}
    text = bridge_status_text(stale)
    assert "STALE 6" in text
    warning = bridge_status_warning(stale)
    assert "OUTDATED widget" in warning
    assert "0.0.25" in warning
