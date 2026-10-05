"""A group press must not queue an audit a pending INAUDIT layer already asks for.

Running an audit on a project whose canonical INAUDIT layer is still waiting
re-burns a browser window on work that is already queued: the layer is the
request, and it is consumed by being read, not by starting a second audit. The
dedupe is a GROUP policy only -- an operator pressing START on one project is an
explicit override and still starts it.
"""

from __future__ import annotations

import pytest

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService


@pytest.fixture
def window(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    projects = []
    for index in range(1, 7):
        root = tmp_path / f"p{index}"
        (root / "audit").mkdir(parents=True)
        projects.append(Project(
            id=f"p{index}", display_name=f"Project {index}",
            source_path=str(root), priority_group="MAIN0", slot=index,
        ))
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=projects,
    )
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    yield win
    win.close()


def _layer(window, project_id: str, number: int = 1):
    """Create one canonical pending layer for a project."""
    from pathlib import Path

    project = window._service.get_project(project_id)
    path = Path(project.source_path) / "audit" / f"{number}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# audit request\n", encoding="utf-8")
    return path


def _drain(window) -> list[list[str]]:
    """Run the queue synchronously, including the report the operator reads."""
    from types import SimpleNamespace

    batches: list[list[str]] = []

    def _start_batch(ids, profile, should_abort=None):
        batches.append(list(ids))
        return [
            SimpleNamespace(ok=True, duplicate=False, project_id=pid, message="")
            for pid in ids
        ]

    window._audit_runs.start_batch = _start_batch
    window._refresh_audit_runs_async = lambda: None
    window._arrange_worker_windows_async = lambda *a, **k: None

    def _submit(key, fn, on_success=None, on_error=None):
        try:
            result = fn()
        except Exception as exc:  # pragma: no cover - the runner's own path
            if on_error is not None:
                on_error(exc)
            return
        if on_success is not None:
            on_success(result)

    window.task_runner.submit = _submit
    return batches


def test_a_group_press_skips_a_project_that_already_has_a_pending_layer(window):
    _layer(window, "p2")
    window._selected_project = lambda: window._service.get_project("p1")
    _drain(window)

    window._on_start_audit_group()

    queued = [pid for pid, _profile in window._audit_start_pending]
    assert "p2" not in queued
    assert "p1" in queued


def test_the_skip_is_reported_visibly_with_its_count_and_reason(window):
    _layer(window, "p2")
    _layer(window, "p3")
    window._selected_project = lambda: window._service.get_project("p1")
    batches = _drain(window)
    statuses: list[str] = []
    window._flash_status = lambda text, *_a, **_k: statuses.append(str(text))

    window._on_start_audit_group()
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()

    assert batches == [["p1", "p4", "p5", "p6"]]
    report = [text for text in statuses if "skipped" in text]
    assert report, "a skipped project must be visible, never a silent hole"
    assert "2 skipped" in report[-1]
    assert "INAUDIT pending" in report[-1]


def test_a_group_press_never_claims_a_project_already_queued_or_in_flight(window):
    """The pre-existing suppression still holds alongside the new skip."""
    _layer(window, "p6")
    window._selected_project = lambda: window._service.get_project("p1")
    batches = _drain(window)

    window._on_start_audit_group()
    queued = [pid for pid, _profile in window._audit_start_pending]
    window._on_start_audit_group()
    assert [pid for pid, _profile in window._audit_start_pending] == queued
    assert "p6" not in queued

    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()
    assert batches == [["p1", "p2", "p3", "p4", "p5"]]


def test_a_manual_single_project_start_still_overrides_the_policy(window):
    """The operator pressing START on one project is an explicit override."""
    _layer(window, "p2")
    window._selected_project = lambda: window._service.get_project("p2")
    batches = _drain(window)

    window._on_send_audit()
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()

    assert batches == [["p2"]]


def test_consuming_the_layer_makes_the_project_eligible_again(window):
    path = _layer(window, "p2")
    window._selected_project = lambda: window._service.get_project("p1")
    batches = _drain(window)

    window._on_start_audit_group()
    assert "p2" not in [pid for pid, _profile in window._audit_start_pending]

    path.unlink()
    window._on_start_audit_group()
    assert "p2" in [pid for pid, _profile in window._audit_start_pending]
    assert batches == []


def test_a_group_where_every_project_is_skipped_dispatches_nothing(window):
    """No queued work means no browser capacity provisioned, and says why."""
    for index in range(1, 7):
        _layer(window, f"p{index}")
    window._selected_project = lambda: window._service.get_project("p1")
    batches = _drain(window)
    provisioned: list[str] = []
    window._ensure_free_browser_worker = lambda: (provisioned.append("worker") or {"state": "ready"})
    statuses: list[str] = []
    window._flash_status = lambda text, *_a, **_k: statuses.append(str(text))

    window._on_start_audit_group()
    window._audit_start_debounce.stop()
    window._pump_audit_start_queue()

    assert batches == []
    assert window._audit_start_pending == []
    assert provisioned == []
    assert any("6 skipped" in text and "INAUDIT pending" in text for text in statuses)
