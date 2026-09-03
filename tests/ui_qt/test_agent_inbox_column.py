"""READY is the station's verdict, not the agent's. The panel must say both.

An audit that finished and was never read is invisible work: the row says
READY, the operator presses START AUDIT again, and the same audit is produced
twice. The AGENT column answers the question that actually decides it.
"""

from __future__ import annotations

import pytest

from audapack import agent_inbox
from audapack.services.audit_run_service import AuditRunSnapshot
from audapack.ui_qt.dialogs.audit_runs_widget import AuditRunsWidget, agent_inbox_suffix


def run(project_id: str, **overrides) -> AuditRunSnapshot:
    base = dict(
        project_id=project_id,
        project_name=project_id.upper(),
        operator_state="READY",
        summary="AUDIT READY ✓ · 3/3",
        ready=True,
        campaign_run_id=f"run-{project_id}",
    )
    base.update(overrides)
    return AuditRunSnapshot(**base)


def test_a_run_with_no_inbox_adds_nothing_to_the_summary():
    assert agent_inbox_suffix([run("p1"), run("p2")]) == ""


def test_unread_audits_are_counted_once_per_project():
    runs = [
        run("p1", agent_state=agent_inbox.UNREAD),
        run("p1", agent_state=agent_inbox.UNREAD),
        run("p2", agent_state=agent_inbox.UNREAD),
        run("p3", agent_state=agent_inbox.CONSUMED),
    ]
    assert agent_inbox_suffix(runs) == " · 2 unread by agent"


def test_work_in_progress_and_residue_are_named_separately():
    runs = [
        run("p1", agent_state=agent_inbox.IN_WORK),
        run("p2", agent_state=agent_inbox.UNREAD, agent_residue=5),
    ]
    suffix = agent_inbox_suffix(runs)
    assert "1 unread by agent" in suffix
    assert "1 in agent" in suffix
    assert "1 with residue" in suffix


@pytest.fixture
def widget(qapp):
    return AuditRunsWidget()


def test_the_recent_table_carries_an_agent_column(widget):
    headers = [widget.history.horizontalHeaderItem(i).text() for i in range(widget.history.columnCount())]
    assert headers == ["PROJECT", "RESULT", "AGENT", "RUN", "FINISHED"]


def test_a_finished_run_shows_what_the_agent_did_with_it(widget):
    widget.set_runs([run(
        "p1",
        agent_state=agent_inbox.UNREAD,
        agent_summary="AGENT UNREAD · 1",
        agent_guidance="Delivered and never read.",
    )])
    assert widget.history.item(0, 1).text() == "READY"
    assert widget.history.item(0, 2).text() == "AGENT UNREAD · 1"
    assert widget.history.item(0, 2).toolTip() == "Delivered and never read."
    assert "1 unread by agent" in widget.summary.text()


def test_a_run_whose_project_has_no_inbox_shows_a_dash(widget):
    widget.set_runs([run("p1")])
    assert widget.history.item(0, 2).text() == "—"
