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
    assert [pid for pid, _profile in window._audit_start_pending] == ["p2"]


def test_the_same_project_is_never_queued_twice(window):
    window._audit_runs.start_batch = lambda ids, profile: []
    window._start_audit_projects(["p1"], "Project 1")
    window._start_audit_projects(["p1"], "Project 1")
    assert [pid for pid, _profile in window._audit_start_pending] == ["p1"]


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


def test_reopen_closed_workers_only_touches_unregistered_slots(monkeypatch):
    """Closing a worker window by accident must be recoverable from the GUI.

    The Bridge only re-provisions while audits are queued, so an idle pool
    stayed one window short until the next START AUDIT.
    """
    from audapack.services.bridge_service import BridgeService

    service = BridgeService.__new__(BridgeService)
    relaunched: list[int] = []
    service.browser_slots = lambda: {
        "ok": True,
        "slots": [
            {"slot": 1, "registered": True},
            {"slot": 2, "registered": False},
            {"slot": 3, "registered": True},
            {"slot": 4, "registered": False},
        ],
    }
    service.relaunch_browser_slot = lambda slot: (
        relaunched.append(slot) or {"ok": True, "success": True, "slot": slot}
    )

    result = BridgeService.reopen_closed_browser_workers(service)
    assert result["ok"] is True
    assert relaunched == [2, 4]
    assert result["reopened"] == [2, 4]
    assert result["failed"] == []


def test_reopen_reports_a_slot_the_bridge_refuses():
    from audapack.services.bridge_service import BridgeService

    service = BridgeService.__new__(BridgeService)
    service.browser_slots = lambda: {"ok": True, "slots": [{"slot": 5, "registered": False}]}
    service.relaunch_browser_slot = lambda slot: {
        "ok": True, "success": False, "message": "slot already has a live worker",
    }

    result = BridgeService.reopen_closed_browser_workers(service)
    assert result["reopened"] == []
    assert result["failed"] == [{"slot": 5, "message": "slot already has a live worker"}]


def test_reopen_surfaces_an_unreachable_bridge():
    from audapack.services.bridge_service import BridgeService

    service = BridgeService.__new__(BridgeService)
    service.browser_slots = lambda: {"ok": False, "error": "connection refused"}
    result = BridgeService.reopen_closed_browser_workers(service)
    assert result["ok"] is False
    assert "connection refused" in result["error"]


def test_a_profile_button_starts_that_profile_in_one_press(window):
    """Selecting a profile and then pressing START AUDIT is two actions."""
    batches: list[tuple[list[str], str]] = []
    window._audit_runs.start_batch = lambda ids, profile: (batches.append((list(ids), profile)) or [])
    window.task_runner.submit = lambda key, fn, on_success=None, on_error=None: fn()
    window._selected_project = lambda: window._service.get_project("p1")

    window._on_launch_audit_profile("compress")
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()

    assert batches == [(["p1"], "compress")]
    assert window._service.config.audits.profile == "compress"
    assert window.profile_actions["compress"].isChecked() is True


def test_two_profiles_queued_together_never_share_one_batch(window):
    """start_batch applies ONE profile to everything it is handed."""
    batches: list[tuple[list[str], str]] = []
    window._audit_runs.start_batch = lambda ids, profile: (batches.append((list(ids), profile)) or [])
    window.task_runner.submit = lambda key, fn, on_success=None, on_error=None: fn()

    window._start_audit_projects(["p1", "p2"], "pair", profile_id="quick3")
    window._start_audit_projects(["p3"], "third", profile_id="compress")
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()

    assert batches == [(["p1", "p2"], "quick3")]
    assert [pid for pid, _profile in window._audit_start_pending] == ["p3"]

    window._pump_audit_start_queue()
    assert batches[-1] == (["p3"], "compress")


def test_a_profile_button_without_a_selection_says_so(window):
    window._selected_project = lambda: None
    window._audit_runs.start_batch = lambda ids, profile: []
    window._on_launch_audit_profile("super10")
    assert window._audit_start_pending == []
    # The choice still persists, so the next START AUDIT uses it.
    assert window._service.config.audits.profile == "super10"
