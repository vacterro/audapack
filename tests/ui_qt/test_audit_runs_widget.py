from audapack.services.audit_run_service import AuditRunSnapshot
from audapack.ui_qt.dialogs.audit_runs_widget import AuditRunsWidget


def run(index: int, state="AUDITING", ready=False):
    return AuditRunSnapshot(
        project_id=f"p{index}",
        project_name=f"Project {index}",
        operator_state=state,
        summary=f"{'AUDIT READY ✓' if ready else 'AUDIT'} · {index % 3}/3",
        dispatch_id=f"dsp-{index:016d}",
        dispatch_state="COMPLETE" if ready else "AUDITING",
        completed_waves=3 if ready else index % 3,
        total_waves=3,
        ready=ready,
        handoff_path=f"C:/results/p{index}.md" if ready else "",
        actions=("OPEN", "COPY", "DETAILS") if ready else ("DETAILS",),
        updated_at=float(index),
        completed_at=float(index) if ready else 0.0,
    )


def test_panel_has_exactly_six_visible_lanes_and_ready_truth(qapp):
    widget = AuditRunsWidget()
    widget.set_runs([run(index, "READY", True) for index in range(1, 7)])
    assert widget.lanes.rowCount() == 6
    assert widget.summary.text() == "AUDIT RUNS · 0 active · 6 ready · 0 attention · max 6"
    assert widget.lanes.item(0, 2).text().startswith("AUDIT READY")
    assert widget.lanes.item(5, 3).text() == "3/3"


def test_panel_state_aware_actions_emit_run_identities(qapp):
    widget = AuditRunsWidget()
    retry = AuditRunSnapshot(
        project_id="p1", project_name="Project", operator_state="FAILED",
        summary="FAILED", dispatch_id="dsp-1", actions=("RETRY", "DETAILS"),
    )
    widget.set_runs([retry])
    widget.lanes.selectRow(0)
    started = []
    details = []
    widget.start_requested.connect(started.append)
    widget.diagnostics_requested.connect(details.append)
    assert widget.retry_button.isEnabled()
    assert not widget.cancel_button.isEnabled()
    widget.retry_button.click()
    widget.details_button.click()
    assert started == ["p1"]
    assert details == [retry]


def test_panel_recent_history_is_bounded_to_twelve(qapp):
    widget = AuditRunsWidget()
    widget.set_runs([run(index, "READY", True) for index in range(1, 20)])
    assert widget.history.rowCount() == 12
