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

from audapack.services.audit_run_service import MAX_AUDIT_LANES, AuditRunSnapshot


class AuditRunsWidget(QWidget):
    """Shows six current lanes plus bounded recent history and safe actions."""

    start_requested = Signal(str)
    cancel_requested = Signal(str)
    open_requested = Signal(str)
    diagnostics_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runs: list[AuditRunSnapshot] = []
        self._lane_runs: list[AuditRunSnapshot] = []

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
        self.open_button = QPushButton("OPEN RESULT", self)
        self.details_button = QPushButton("COPY DETAILS", self)
        self.retry_button.clicked.connect(self._start_selected)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.open_button.clicked.connect(self._open_selected)
        self.details_button.clicked.connect(self._details_selected)
        for button in (self.retry_button, self.cancel_button, self.open_button, self.details_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        history_label = QLabel("RECENT", self)
        layout.addWidget(history_label)
        self.history = QTableWidget(0, 4, self)
        self.history.setHorizontalHeaderLabels(("PROJECT", "RESULT", "RUN", "FINISHED"))
        self.history.verticalHeader().setVisible(False)
        self.history.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.history.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
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
        active = sum(run.operator_state not in {"READY", "FAILED", "CANCELLED"} for run in latest)
        ready = sum(run.ready for run in latest)
        attention = sum(run.operator_state in {"FAILED", "BLOCKED_PRE_START", "BLOCKED_POST_START", "RECOVERY"} for run in latest)
        self.summary.setText(f"AUDIT RUNS · {active} active · {ready} ready · {attention} attention · max {MAX_AUDIT_LANES}")

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

        terminal = [
            run for run in self._runs
            if run.operator_state in {"READY", "FAILED", "CANCELLED", "BLOCKED_PRE_START", "BLOCKED_POST_START", "RECOVERY"}
        ][:12]
        self.history.setRowCount(len(terminal))
        for row, run in enumerate(terminal):
            values = (
                run.project_name,
                run.operator_state,
                run.campaign_run_id or run.dispatch_id or run.intent_id,
                self._time_label(run.completed_at or run.updated_at),
            )
            for column, value in enumerate(values):
                self.history.setItem(row, column, QTableWidgetItem(value))
        self._sync_actions()

    def _selected(self) -> AuditRunSnapshot | None:
        row = self.lanes.currentRow()
        if 0 <= row < len(self._lane_runs):
            return self._lane_runs[row]
        return None

    def _sync_actions(self) -> None:
        run = self._selected()
        actions = set(run.actions) if run else set()
        self.retry_button.setEnabled(bool(run and actions.intersection({"RETRY", "RECOVER"})))
        self.cancel_button.setEnabled(bool(run and "CANCEL" in actions and run.dispatch_id))
        self.open_button.setEnabled(bool(run and "OPEN" in actions and run.handoff_path))
        self.details_button.setEnabled(bool(run))

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
