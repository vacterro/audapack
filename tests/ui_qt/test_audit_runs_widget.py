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


def test_alternating_rows_stay_inside_the_golden_default_palette(qapp):
    """No Qt default white may leak into a dark golden panel.

    The Audit Runs tables are QTableWidgets and the theme styled only QTreeView,
    so every second lane rendered on Qt's default #f7f7f7 alternate base.
    """
    from audapack.ui_qt.theme.golden_default import GoldenDefault

    widget = AuditRunsWidget()
    widget.setStyleSheet(GoldenDefault.qss())
    widget.resize(640, 460)
    widget.set_runs([run(index) for index in range(1, 7)])
    widget.show()
    qapp.processEvents()

    image = widget.grab().toImage()
    viewport = widget.lanes.viewport()
    origin = viewport.mapTo(widget, viewport.rect().topLeft())

    allowed = {
        GoldenDefault.surface.upper(),
        GoldenDefault.surfaceRaised.upper(),
        GoldenDefault.selection.upper(),
    }

    # Sample the far right of each lane, past the text, so the check does not
    # depend on font metrics deciding where a glyph lands.
    seen = set()
    for row in range(6):
        rect = widget.lanes.visualRect(widget.lanes.model().index(row, 5))
        colour = image.pixelColor(
            origin.x() + rect.right() - 2,
            origin.y() + rect.center().y(),
        ).name().upper()
        seen.add(colour)
        assert colour in allowed, f"lane {row + 1} painted {colour}"

    # Alternating rows must still be visually distinguishable.
    assert len(seen) >= 2, seen

    # And no Qt default white may leak anywhere in the table, whatever the
    # column layout happens to be.
    viewport_rect = viewport.rect()
    for y in range(0, viewport_rect.height(), 3):
        for x in range(0, viewport_rect.width(), 3):
            pixel = image.pixelColor(origin.x() + x, origin.y() + y)
            assert not (pixel.red() > 200 and pixel.green() > 200 and pixel.blue() > 200), (
                f"light pixel {pixel.name()} at {x},{y}"
            )

    widget.hide()


def test_theme_styles_tables_and_lists_not_only_trees():
    from audapack.ui_qt.theme.golden_default import GoldenDefault

    qss = GoldenDefault.qss()
    assert "QTableView, QListView {" in qss
    assert f"alternate-background-color: {GoldenDefault.surfaceRaised};" in qss
    assert f"gridline-color: {GoldenDefault.borderMuted};" in qss


def test_run_polling_speeds_up_while_a_run_is_live(qapp, monkeypatch, tmp_path):
    """A flat 30 s poll made a working chain look dead."""
    from audapack.ui_qt.main_window import MainWindow

    tune = MainWindow._tune_bridge_poll_interval

    class FakeTimer:
        def __init__(self):
            self._interval = 30000

        def interval(self):
            return self._interval

        def setInterval(self, value):
            self._interval = int(value)

    class Stub:
        RUN_SETTLED_STATES = MainWindow.RUN_SETTLED_STATES
        BRIDGE_POLL_ACTIVE_MS = 4000
        BRIDGE_POLL_IDLE_MS = 30000

        def __init__(self):
            self.bridge_timer = FakeTimer()

    stub = Stub()
    assert tune(stub, [run(1, "AUDITING")]) == 4000
    assert stub.bridge_timer.interval() == 4000

    assert tune(stub, [run(1, "READY", True), run(2, "CANCELLED")]) == 30000
    assert stub.bridge_timer.interval() == 30000

    assert tune(stub, []) == 30000
    assert tune(stub, [run(3, "BLOCKED_PRE_START")]) == 30000
    assert tune(stub, [run(4, "WAITING")]) == 4000


def test_refresh_keeps_the_operator_selection(qapp):
    """A 4 s refresh must not repaint the selection out from under the cursor."""
    widget = AuditRunsWidget()
    widget.set_runs([run(index) for index in range(1, 5)])
    widget.lanes.selectRow(2)
    selected = widget._selected()
    assert selected is not None and selected.project_id == "p3"

    # Same board, refreshed: selection survives even though rows are rebuilt.
    widget.set_runs([run(index) for index in range(1, 5)])
    assert widget.lanes.currentRow() == 2
    still = widget._selected()
    assert still is not None and still.project_id == "p3"

    # The lane reorders: the selection follows the PROJECT, not the row number.
    widget.set_runs([run(index) for index in (3, 1, 2, 4)])
    assert widget.lanes.currentRow() == 0
    moved = widget._selected()
    assert moved is not None and moved.project_id == "p3"

    # The project disappears: no stale highlight is left behind.
    widget.set_runs([run(index) for index in (1, 2, 4)])
    assert widget._selected() is None


def test_reset_all_button_tracks_unfinished_work(qapp):
    widget = AuditRunsWidget()
    widget.set_runs([])
    assert widget.reset_all_button.isEnabled() is False

    widget.set_runs([run(1, "READY", True), run(2, "CANCELLED")])
    assert widget.reset_all_button.isEnabled() is False

    widget.set_runs([run(1, "READY", True), run(2, "AUDITING")])
    assert widget.reset_all_button.isEnabled() is True
