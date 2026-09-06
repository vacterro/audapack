"""PERF-004: an unchanged audit-run poll must draw nothing.

The dashboard refresh publishes EVERY project's snapshot and rebuilds EVERY
AuditRunsWidget table on EVERY poll -- 4 s while a run is live, 30 s idle --
regardless of whether anything moved. At the 300-project scale target that is
300 row invalidations every four seconds (75/s averaged) plus ~100
QTableWidgetItem allocations, for a board nobody touched.

The guard is display equality, so a genuine transition still propagates: these
tests assert both halves, because a no-op that also swallows real news is the
worse defect.
"""

from __future__ import annotations

import dataclasses

import pytest

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.audit_run_service import AuditRunSnapshot
from audapack.services.project_service import ProjectService
from audapack.ui_qt.dialogs.audit_runs_widget import AuditRunsWidget
from audapack.ui_qt.models.project_room_model import ProjectRoomModel


@pytest.fixture
def model(tmp_path, qapp):
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id=f"p{index}", display_name=f"Project {index}",
                    source_path=str(tmp_path / f"p{index}"),
                    priority_group="MAIN0", slot=index)
            for index in range(1, 4)
        ],
    )
    return ProjectRoomModel(ProjectService(config, base_dir=tmp_path))


def run(index: int, state: str = "AUDITING", **overrides) -> AuditRunSnapshot:
    snapshot = AuditRunSnapshot(
        project_id=f"p{index}",
        project_name=f"Project {index}",
        operator_state=state,
        summary=f"AUDIT {index % 3}/3",
        dispatch_id=f"dsp-{index:04d}",
        dispatch_state=state,
        completed_waves=index % 3,
        total_waves=3,
        campaign_run_id=f"run-{index}",
        updated_at=float(index),
        actions=("DETAILS",),
    )
    return dataclasses.replace(snapshot, **overrides) if overrides else snapshot


# ------------------------------------------------------------------ the model


def test_an_identical_poll_emits_no_dataChanged(model):
    changed: list[object] = []
    model.dataChanged.connect(lambda *args: changed.append(args))

    model.update_audit_run_snapshot("p1", run(1))
    assert len(changed) == 1
    baseline = model.targeted_project_update_count

    for _ in range(10):
        model.update_audit_run_snapshot("p1", run(1))

    assert len(changed) == 1, "an unchanged snapshot is not news"
    assert model.targeted_project_update_count == baseline


def test_a_real_transition_still_repaints_that_row(model):
    changed: list[object] = []
    model.update_audit_run_snapshot("p1", run(1))
    model.dataChanged.connect(lambda *args: changed.append(args))

    model.update_audit_run_snapshot("p1", run(1, "FINALIZING"))

    assert len(changed) == 1
    index = model.index_for_project_id("p1")
    assert model.data(index, model.ROLES["audit_run_state"]) == "FINALIZING"


def test_an_unchanged_board_of_projects_costs_nothing(model):
    """The whole-board publication is the shape the dashboard actually uses."""
    board = {f"p{index}": run(index) for index in range(1, 4)}
    for project_id, snapshot in board.items():
        model.update_audit_run_snapshot(project_id, snapshot)

    changed: list[object] = []
    model.dataChanged.connect(lambda *args: changed.append(args))
    for _ in range(4):
        for project_id, snapshot in board.items():
            model.update_audit_run_snapshot(project_id, snapshot)

    assert changed == []

    # One project moves: exactly one row is invalidated, and it is that one.
    model.update_audit_run_snapshot("p2", run(2, "READY", ready=True))
    assert len(changed) == 1
    top_left = changed[0][0]
    assert model.data(top_left, model.ROLES["project_id"]) == "p2"


def test_clearing_a_project_that_has_no_snapshot_is_a_no_op(model):
    changed: list[object] = []
    model.dataChanged.connect(lambda *args: changed.append(args))

    model.update_audit_run_snapshot("p1", None)
    assert changed == []

    # A real clear still repaints: the row must lose its run state.
    model.update_audit_run_snapshot("p1", run(1))
    model.update_audit_run_snapshot("p1", None)
    assert len(changed) == 2
    index = model.index_for_project_id("p1")
    assert model.data(index, model.ROLES["audit_run_state"]) == ""


def test_a_live_transport_write_is_not_swallowed_by_the_guard(model):
    """update_dispatch_snapshot carries fields the run snapshot does not.

    Comparing only the snapshot would leave the panel showing a browser name
    and error code the composite refresh no longer agrees with.
    """
    snapshot = run(1)
    model.update_audit_run_snapshot("p1", snapshot)
    model.update_dispatch_snapshot("p1", {"dispatch_id": "dsp-0001", "state": "AUDITING",
                                          "browser_name": "Brave", "error": "transient"})
    index = model.index_for_project_id("p1")
    assert model.data(index, model.ROLES["dispatch_error"]) == "transient"

    changed: list[object] = []
    model.dataChanged.connect(lambda *args: changed.append(args))
    model.update_audit_run_snapshot("p1", snapshot)

    assert len(changed) == 1, "the composite refresh must reclaim the dispatch row"
    assert model.data(index, model.ROLES["dispatch_error"]) == ""


# ----------------------------------------------------------------- the widget


def test_an_identical_poll_rebuilds_no_table(qapp):
    widget = AuditRunsWidget()
    widget.set_runs([run(index) for index in range(1, 4)])
    rebuilds = widget.table_rebuild_count
    items = [widget.lanes.item(row, 1) for row in range(6)]

    for _ in range(10):
        widget.set_runs([run(index) for index in range(1, 4)])

    assert widget.table_rebuild_count == rebuilds
    assert [widget.lanes.item(row, 1) for row in range(6)] == items, (
        "the same cells must survive, not be reallocated"
    )


def test_the_first_empty_board_is_still_drawn(qapp):
    """The constructor leaves rows with no items; set_runs([]) must fill them."""
    widget = AuditRunsWidget()
    widget.set_runs([])
    assert widget.table_rebuild_count == 1
    assert widget.lanes.item(0, 1).text() == "[ EMPTY ]"


def test_every_genuine_change_still_rebuilds(qapp):
    widget = AuditRunsWidget()
    widget.set_runs([run(1)])
    rebuilds = widget.table_rebuild_count

    widget.set_runs([run(1, "FINALIZING")])
    assert widget.table_rebuild_count == rebuilds + 1
    assert widget.lanes.item(0, 2).text() == "AUDIT 1/3"

    # A queue reorder is a change even though every field of every run is the
    # same set of values -- order participates.
    queued = [run(7, "WAITING", queue_position=0), run(8, "WAITING", queue_position=1)]
    widget.set_runs(queued)
    rebuilds = widget.table_rebuild_count
    widget.set_runs(list(reversed(queued)))
    assert widget.table_rebuild_count == rebuilds + 1

    # A new agent verdict on an otherwise identical run is news too.
    widget.set_runs([run(1, "READY", ready=True)])
    rebuilds = widget.table_rebuild_count
    widget.set_runs([run(1, "READY", ready=True, agent_state="UNREAD")])
    assert widget.table_rebuild_count == rebuilds + 1
    assert "1 unread by agent" in widget.summary.text()


def test_the_selection_and_actions_survive_an_unchanged_poll(qapp):
    widget = AuditRunsWidget()
    board = [run(index, "FAILED", actions=("RETRY", "DETAILS")) for index in range(1, 4)]
    widget.set_runs(board)
    widget.lanes.selectRow(1)
    assert widget.retry_button.isEnabled()

    widget.set_runs(board)

    assert widget.lanes.currentRow() == 1
    selected = widget._selected()
    assert selected is not None and selected.project_id == "p2"
    assert widget.retry_button.isEnabled()


def test_a_skipped_rebuild_still_keeps_the_run_list_current(qapp):
    """RESET ALL and the diagnostics dialog read _runs, not the tables."""
    widget = AuditRunsWidget()
    board = [run(1, "AUDITING")]
    widget.set_runs(board)
    widget.set_runs(board)
    assert [snapshot.project_id for snapshot in widget._runs] == ["p1"]
    assert widget.reset_all_button.isEnabled() is True


# ----------------------------------------------------------- the scale matrix


@pytest.mark.parametrize("project_count", [24, 60, 120, 300])
def test_the_scale_matrix_polls_cost_nothing_when_nothing_moved(tmp_path, qapp, project_count):
    """The audit's own VERIFY: the existing 24/60/120/300 Qt scale matrix.

    300 projects x a 4 s poll was 75 row invalidations per second for a board
    nobody touched. The count asserted here is zero, not "fewer".
    """
    from tests.ui_qt.test_scale_stress import build_scale_project_service

    service, scale_model = build_scale_project_service(tmp_path, project_count)
    board = {
        project.id: run(index, project_id=project.id, project_name=project.display_name)
        for index, project in enumerate(service.list_projects(), start=1)
    }
    for project_id, snapshot in board.items():
        scale_model.update_audit_run_snapshot(project_id, snapshot)

    changed: list[object] = []
    scale_model.dataChanged.connect(lambda *args: changed.append(args))
    published = scale_model.targeted_project_update_count
    for _ in range(3):
        for project_id, snapshot in board.items():
            scale_model.update_audit_run_snapshot(project_id, snapshot)

    assert changed == []
    assert scale_model.targeted_project_update_count == published
