"""Compact six-lane operator panel for transparent audit runs."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from audapack import agent_inbox
from audapack.services.audit_run_service import MAX_AUDIT_LANES, AuditRunSnapshot


def agent_inbox_suffix(runs) -> str:
    """What the agent still owes, counted once per project.

    An audit that reached READY and was never read is invisible work: the
    station looks finished and the same audit gets ordered again. Residue is
    counted too -- files the inbox will never read and never clean up, which is
    exactly what AUDAPACK used to deliver.
    """
    unread: set[str] = set()
    working: set[str] = set()
    residue: set[str] = set()
    for run in runs:
        project = str(getattr(run, "project_id", "") or "")
        if not project:
            continue
        state = str(getattr(run, "agent_state", "") or "")
        if state == agent_inbox.UNREAD:
            unread.add(project)
        elif state == agent_inbox.IN_WORK:
            working.add(project)
        if int(getattr(run, "agent_residue", 0) or 0):
            residue.add(project)
    parts = []
    if unread:
        parts.append(f"{len(unread)} unread by agent")
    if working:
        parts.append(f"{len(working)} in agent")
    if residue:
        parts.append(f"{len(residue)} with residue")
    return (" · " + " · ".join(parts)) if parts else ""


def _queue_position(run) -> int:
    """Place in the waiting line, or -1. Zero is a POSITION, not a missing one."""
    value = getattr(run, "queue_position", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class AuditRunsWidget(QWidget):
    """Shows six current lanes plus bounded recent history and safe actions."""

    start_requested = Signal(str)
    cancel_requested = Signal(str)
    abandon_requested = Signal(str)
    open_requested = Signal(str)
    diagnostics_requested = Signal(object)
    reset_all_requested = Signal()
    #: (dispatch_id, delta) -- move a waiting run up (-1) or down (+1).
    reorder_requested = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runs: list[AuditRunSnapshot] = []
        self._lane_runs: list[AuditRunSnapshot] = []
        self._queue_runs: list[AuditRunSnapshot] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.summary = QLabel("AUDIT RUNS · 0 active · 0 ready", self)
        self.summary.setObjectName("auditRunsSummary")
        layout.addWidget(self.summary)

        self.lanes = QTableWidget(MAX_AUDIT_LANES, 6, self)
        self.lanes.setHorizontalHeaderLabels(("LANE", "PROJECT", "STATE", "WAVES", "WORKER", "UPDATED"))
        self.lanes.verticalHeader().setVisible(False)
        self.lanes.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lanes.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.lanes.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.lanes.setAlternatingRowColors(True)
        self.lanes.setMinimumHeight(190)
        self.lanes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.lanes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.lanes.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.lanes.itemSelectionChanged.connect(self._sync_actions)
        layout.addWidget(self.lanes)

        actions = QHBoxLayout()
        actions.setSpacing(3)
        self.retry_button = QPushButton("START / RETRY", self)
        self.cancel_button = QPushButton("CANCEL", self)
        self.abandon_button = QPushButton("FORCE UNBLOCK", self)
        self.abandon_button.setToolTip("Mark a dead BLOCKED run FAILED so the project can start again")
        self.open_button = QPushButton("OPEN RESULT", self)
        self.details_button = QPushButton("COPY DETAILS", self)
        self.reset_all_button = QPushButton("RESET ALL", self)
        self.reset_all_button.setToolTip("Clear every unfinished lane in one action")
        self.retry_button.clicked.connect(self._start_selected)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.abandon_button.clicked.connect(self._abandon_selected)
        self.open_button.clicked.connect(self._open_selected)
        self.details_button.clicked.connect(self._details_selected)
        self.reset_all_button.clicked.connect(self.reset_all_requested.emit)
        self._pending_dispatch_count = 0
        for button in (self.retry_button, self.cancel_button, self.abandon_button, self.open_button, self.details_button, self.reset_all_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        # QUEUE: everyone waiting for a window. The lane table only ever shows
        # six, so a seventh queued project was invisible -- and an order you
        # cannot see is one you cannot decide.
        self.queue_label = QLabel("QUEUE — next window goes to the top row", self)
        layout.addWidget(self.queue_label)
        self.queue = QTableWidget(0, 4, self)
        self.queue.setHorizontalHeaderLabels(("#", "PROJECT", "PROFILE", "WAITING"))
        self.queue.verticalHeader().setVisible(False)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.queue.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.queue.setMaximumHeight(120)
        self.queue.itemSelectionChanged.connect(self._sync_actions)
        layout.addWidget(self.queue)

        self.queue_actions_row = QWidget(self)
        queue_actions = QHBoxLayout(self.queue_actions_row)
        queue_actions.setContentsMargins(0, 0, 0, 0)
        queue_actions.setSpacing(3)
        self.queue_up_button = QPushButton("▲ UP", self)
        self.queue_up_button.setToolTip("Move this project one place towards the next free window")
        self.queue_down_button = QPushButton("▼ DOWN", self)
        self.queue_down_button.setToolTip("Let the project below it take the next free window first")
        self.queue_up_button.clicked.connect(lambda: self._reorder_selected(-1))
        self.queue_down_button.clicked.connect(lambda: self._reorder_selected(+1))
        queue_actions.addWidget(self.queue_up_button)
        queue_actions.addWidget(self.queue_down_button)
        queue_actions.addStretch(1)
        layout.addWidget(self.queue_actions_row)
        # Nothing waiting means no line to show. An empty table with a header
        # is 120px of nothing between the lanes and the history.
        self._set_queue_visible(False)

        history_label = QLabel("RECENT", self)
        layout.addWidget(history_label)
        # AGENT: READY only says the station finished. Whether anyone READ the
        # result is a different fact, and it is the one that decides whether to
        # press START AUDIT again -- so it gets its own column beside RESULT.
        self.history = QTableWidget(0, 5, self)
        self.history.setHorizontalHeaderLabels(("PROJECT", "RESULT", "AGENT", "RUN", "FINISHED"))
        self.history.verticalHeader().setVisible(False)
        self.history.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.history.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.history.setMaximumHeight(150)
        layout.addWidget(self.history)
        self._sync_actions()

    @staticmethod
    def _time_label(value: float) -> str:
        if not value:
            return "—"
        try:
            return datetime.fromtimestamp(value).strftime("%d.%m %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return "—"

    def set_runs(self, runs: list[AuditRunSnapshot]) -> None:
        # A 4 s refresh that silently drops the operator's selection makes the
        # action buttons unusable: you aim at a lane and the panel repaints under
        # your cursor. Remember the selected project and restore it.
        selected_project = ""
        current_row = self.lanes.currentRow()
        if 0 <= current_row < len(self._lane_runs):
            selected_project = str(self._lane_runs[current_row].project_id)
        queue_selected = ""
        queue_row = self.queue.currentRow()
        if 0 <= queue_row < len(self._queue_runs):
            queue_selected = str(self._queue_runs[queue_row].dispatch_id)
        self._runs = list(runs)
        latest: list[AuditRunSnapshot] = []
        seen: set[str] = set()
        for run in self._runs:
            if run.project_id and run.project_id not in seen:
                latest.append(run)
                seen.add(run.project_id)
            if len(latest) == MAX_AUDIT_LANES:
                break
        self._lane_runs = latest
        active = sum(run.operator_state not in {"READY", "FAILED", "CANCELLED", "SUPERSEDED"} for run in latest)
        ready = sum(run.ready for run in latest)
        attention = sum(run.operator_state in {"FAILED", "BLOCKED_PRE_START", "BLOCKED_POST_START", "RECOVERY"} for run in latest)
        self.summary.setText(
            f"AUDIT RUNS · {active} active · {ready} ready · {attention} attention"
            f" · max {MAX_AUDIT_LANES}{agent_inbox_suffix(self._runs)}"
        )

        self.lanes.blockSignals(True)
        self.lanes.clearContents()
        for row in range(MAX_AUDIT_LANES):
            run = latest[row] if row < len(latest) else None
            values = (
                str(row + 1),
                run.project_name if run else "[ EMPTY ]",
                run.summary if run else "—",
                f"{run.completed_waves}/{run.total_waves}" if run else "—",
                run.worker_label or ("waiting" if run and run.operator_state == "WAITING" else "—") if run else "—",
                self._time_label(run.updated_at) if run else "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 3, 5}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.lanes.setItem(row, column, item)

        restored_row = next(
            (index for index, snapshot in enumerate(latest) if str(snapshot.project_id) == selected_project),
            -1,
        ) if selected_project else -1
        self.lanes.blockSignals(False)
        if restored_row >= 0:
            if self.lanes.currentRow() != restored_row:
                self.lanes.selectRow(restored_row)
        elif selected_project:
            # The lane genuinely disappeared; do not leave a stale highlight.
            self.lanes.clearSelection()

        # The waiting line, in the order the dispatcher will consume it. A run
        # is in it only while it has no window: queue_position goes to -1 the
        # moment one claims the job.
        # `or -1` here would drop position ZERO -- the front of the line, and
        # the one row that matters most.
        self._queue_runs = sorted(
            (run for run in self._runs if _queue_position(run) >= 0),
            key=_queue_position,
        )
        self.queue.blockSignals(True)
        self.queue.setRowCount(len(self._queue_runs))
        for row, run in enumerate(self._queue_runs):
            values = (
                str(row + 1),
                run.project_name,
                run.profile_id,
                self._time_label(run.created_at),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 3}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.queue.setItem(row, column, item)
        self.queue.blockSignals(False)
        self._set_queue_visible(bool(self._queue_runs))
        restored_queue_row = next(
            (index for index, snapshot in enumerate(self._queue_runs)
             if str(snapshot.dispatch_id) == queue_selected),
            -1,
        ) if queue_selected else -1
        if restored_queue_row >= 0:
            if self.queue.currentRow() != restored_queue_row:
                self.queue.selectRow(restored_queue_row)
        elif queue_selected:
            self.queue.clearSelection()

        terminal = [
            run for run in self._runs
            if run.operator_state in {"READY", "FAILED", "CANCELLED", "SUPERSEDED", "BLOCKED_PRE_START", "BLOCKED_POST_START", "RECOVERY"}
        ][:12]
        self.history.setRowCount(len(terminal))
        for row, run in enumerate(terminal):
            values = (
                run.project_name,
                run.operator_state,
                run.agent_summary or "—",
                run.campaign_run_id or run.dispatch_id or run.intent_id,
                self._time_label(run.completed_at or run.updated_at),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2 and run.agent_guidance:
                    item.setToolTip(run.agent_guidance)
                self.history.setItem(row, column, item)
        self._sync_actions()

    def _selected(self) -> AuditRunSnapshot | None:
        row = self.lanes.currentRow()
        if 0 <= row < len(self._lane_runs):
            return self._lane_runs[row]
        return None

    def _set_queue_visible(self, visible: bool) -> None:
        for part in (self.queue_label, self.queue, self.queue_actions_row):
            part.setVisible(bool(visible))

    def _selected_queued(self) -> AuditRunSnapshot | None:
        row = self.queue.currentRow()
        if 0 <= row < len(self._queue_runs):
            return self._queue_runs[row]
        return None

    def _reorder_selected(self, delta: int) -> None:
        run = self._selected_queued()
        if run and run.dispatch_id:
            self.reorder_requested.emit(run.dispatch_id, int(delta))

    def _sync_actions(self) -> None:
        run = self._selected()
        actions = set(run.actions) if run else set()
        self.retry_button.setEnabled(bool(run and actions.intersection({"RETRY", "RECOVER"})))
        self.cancel_button.setEnabled(bool(run and "CANCEL" in actions and run.dispatch_id))
        self.abandon_button.setEnabled(bool(run and "ABANDON" in actions and run.dispatch_id))
        self.open_button.setEnabled(bool(run and "OPEN" in actions and run.handoff_path))
        self.details_button.setEnabled(bool(run))
        # A row already at an end of the line has nowhere to go, and a button
        # that does nothing when pressed is worse than one that is greyed out.
        queued = self._selected_queued()
        queued_row = self.queue.currentRow()
        movable = bool(queued and queued.dispatch_id and "UP" in set(queued.actions))
        self.queue_up_button.setEnabled(movable and queued_row > 0)
        self.queue_down_button.setEnabled(movable and 0 <= queued_row < len(self._queue_runs) - 1)
        # RESET ALL never depends on a selection: it exists precisely for the
        # board state where nothing is usefully selectable. It also stays live
        # while presses are still queued in the desktop's dispatch debounce --
        # those have no run snapshot yet and are exactly what "I changed my
        # mind" needs to cancel.
        self.reset_all_button.setEnabled(
            any(
                snapshot.operator_state not in {"READY", "FAILED", "CANCELLED", "SUPERSEDED"}
                for snapshot in self._runs
            )
            or bool(self._pending_dispatch_count)
        )

    def set_pending_dispatch_count(self, count: int) -> None:
        """Presses queued in the desktop but not dispatched to the Bridge yet."""
        self._pending_dispatch_count = max(0, int(count or 0))
        self._sync_actions()

    def _abandon_selected(self) -> None:
        run = self._selected()
        if run and run.dispatch_id and "ABANDON" in set(run.actions):
            self.abandon_requested.emit(run.dispatch_id)

    def _start_selected(self) -> None:
        run = self._selected()
        if run:
            self.start_requested.emit(run.project_id)

    def _cancel_selected(self) -> None:
        run = self._selected()
        if run and run.dispatch_id:
            self.cancel_requested.emit(run.dispatch_id)

    def _open_selected(self) -> None:
        run = self._selected()
        if run and run.handoff_path:
            self.open_requested.emit(run.handoff_path)

    def _details_selected(self) -> None:
        run = self._selected()
        if run:
            self.diagnostics_requested.emit(run)
