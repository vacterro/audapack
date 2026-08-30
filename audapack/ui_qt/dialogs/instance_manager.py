"""Compact embedded manager for native agent instances."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from audapack.instances import InstanceMonitor, WindowInstance
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.theme.golden_default import GoldenDefault


class InstanceManagerWidget(QWidget):
    """Embedded tab for project/global agent windows and explicit Win32 actions."""

    COLUMNS = ("State", "Launcher", "Project", "Window title", "PID", "Process")

    def __init__(
        self,
        monitor: InstanceMonitor,
        service: ProjectService,
        project: Optional[Project],
        parent=None,
    ):
        super().__init__(parent)
        self.monitor = monitor
        self.service = service
        self.project = project
        self._displayed: list[WindowInstance] = []
        self._select_project_on_refresh = False

        self.setMinimumSize(520, 230)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        top = QWidget(self)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)
        self.project_label = QLabel(top)
        title_font = QFont("Verdana", 10, QFont.Weight.Bold)
        title_font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.project_label.setFont(title_font)
        top_layout.addWidget(self.project_label, 1)
        root.addWidget(top)

        self.scope_tabs = QTabBar(self)
        self.scope_tabs.setExpanding(False)
        self.scope_tabs.setDrawBase(False)
        self.scope_tabs.addTab("All (0)")
        self.scope_tabs.addTab("Project (0)")
        self.scope_tabs.setTabToolTip(0, "Show every detected agent window")
        self.scope_tabs.currentChanged.connect(self.refresh_instances)
        root.addWidget(self.scope_tabs)

        self.capacity_label = QLabel(self)
        self.capacity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.capacity_label)

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(18)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self.focus_selected())
        root.addWidget(self.table, 1)

        self.activity_label = QLabel("Activity: select an instance.", self)
        self.activity_label.setFixedHeight(34)
        self.activity_label.setWordWrap(True)
        self.activity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.activity_label.setToolTip(
            "Shows native window state plus the last explicit SAIPEN state/log entry. "
            "Private model reasoning and terminal output are not exposed."
        )
        root.addWidget(self.activity_label)

        actions = QWidget(self)
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(2)
        self.refresh_button = QPushButton("Refresh", actions)
        self.refresh_button.clicked.connect(self.refresh_instances)
        self.focus_button = QPushButton("Focus", actions)
        self.focus_button.clicked.connect(self.focus_selected)
        self.close_button = QPushButton("Close", actions)
        self.close_button.clicked.connect(self.close_selected)
        self.cascade_button = QPushButton("Cascade", actions)
        self.cascade_button.clicked.connect(lambda: self.arrange_displayed("cascade"))
        self.tile_vertical_button = QPushButton("Tile columns", actions)
        self.tile_vertical_button.clicked.connect(lambda: self.arrange_displayed("tile_vertical"))
        self.tile_horizontal_button = QPushButton("Tile rows", actions)
        self.tile_horizontal_button.clicked.connect(lambda: self.arrange_displayed("tile_horizontal"))
        for button in (
            self.refresh_button,
            self.focus_button,
            self.close_button,
            self.cascade_button,
            self.tile_vertical_button,
            self.tile_horizontal_button,
        ):
            action_layout.addWidget(button)
        action_layout.addStretch(1)
        root.addWidget(actions)

        self.status_label = QLabel("Select a running window to focus or close it.", self)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status_label)

        self.setStyleSheet(
            GoldenDefault.qss()
            + f"""
QTableWidget {{
    background: {GoldenDefault.compareBack};
    color: {GoldenDefault.textPrimary};
    border-top: 2px solid {GoldenDefault.borderDark};
    border-left: 2px solid {GoldenDefault.borderDark};
    border-right: 2px solid {GoldenDefault.bevelLight};
    border-bottom: 2px solid {GoldenDefault.bevelLight};
    gridline-color: {GoldenDefault.borderMuted};
    font-family: "Verdana";
    font-size: 10px;
}}
QTableWidget::item:selected {{
    background: {GoldenDefault.selection};
    color: {GoldenDefault.borderHighlight};
}}
"""
        )
        self.set_project(project)

    def set_project(self, project: Optional[Project]) -> None:
        self.project = project
        if project is None:
            self.project_label.setText("No project selected")
            self.scope_tabs.setTabText(1, "Project (0)")
            self.scope_tabs.setTabToolTip(1, "Select a project in Project Room")
            self.scope_tabs.setTabEnabled(1, False)
        else:
            self.project_label.setText(f"{project.display_name} · {project.source_path}")
            self.scope_tabs.setTabText(1, f"{project.display_name} (0)")
            self.scope_tabs.setTabToolTip(1, f"Show only {project.display_name} agent windows")
            self.scope_tabs.setTabEnabled(1, True)
        self._select_project_on_refresh = project is not None
        self.scope_tabs.setCurrentIndex(0)
        self.refresh_instances()

    def showEvent(self, event) -> None:
        self.refresh_instances()
        super().showEvent(event)

    def _capacity_text(self) -> str:
        running = sum(item.state == "running" for item in self.monitor.instances)
        starting = len(self.monitor.instances) - running
        chunks = [f"Windows {len(self.monitor.instances)}", f"RUN {running}", f"START {starting}"]
        for launcher in self.service.config.launchers:
            if not getattr(launcher, "enabled", True):
                continue
            count = self.monitor.count_for_launcher(launcher.id)
            limit = max(0, int(getattr(launcher, "max_instances", 0) or 0))
            suffix = str(limit) if limit else "∞"
            state = " BLOCKED" if limit and count >= limit else ""
            chunks.append(f"{launcher.short_label} {count}/{suffix}{state}")
        return "  |  ".join(chunks)

    def refresh_instances(self, *_args) -> None:
        selected = self._selected_instance()
        selected_key = (selected.hwnd, selected.pid) if selected else None
        self.monitor.refresh(self.service.list_projects(), self.service.config.launchers)
        project_instances = self.monitor.for_project(self.project.id) if self.project is not None else []
        self.scope_tabs.setTabText(0, f"All ({len(self.monitor.instances)})")
        if self.project is not None:
            self.scope_tabs.setTabText(1, f"{self.project.display_name} ({len(project_instances)})")

        if self.scope_tabs.currentIndex() == 0:
            self._displayed = list(self.monitor.instances)
        elif self.project is None:
            self._displayed = []
        else:
            self._displayed = project_instances

        self.table.setRowCount(len(self._displayed))
        selected_row = -1
        for row, instance in enumerate(self._displayed):
            values = (
                "RUNNING" if instance.state == "running" else "STARTING",
                instance.launcher_name,
                instance.project_name or "Unknown project",
                instance.title,
                str(instance.pid),
                instance.process_name or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, instance)
                self.table.setItem(row, column, item)
            if self._select_project_on_refresh and instance.project_id == getattr(self.project, "id", ""):
                if selected_row < 0:
                    selected_row = row
            elif selected_key == (instance.hwnd, instance.pid) and selected_row < 0:
                selected_row = row

        self._select_project_on_refresh = False
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        elif self._displayed:
            self.table.selectRow(0)
        if self.monitor.last_error:
            self.capacity_label.setText(self.monitor.last_error)
            self.status_label.setText("Monitoring unavailable: retry Refresh list; no stale window data is shown.")
        elif not self._displayed:
            self.capacity_label.setText(self._capacity_text())
            if self.scope_tabs.currentIndex() == 0:
                self.status_label.setText("No agent windows detected. Launch one, then press Refresh.")
            elif self.project is None:
                self.status_label.setText("Project view unavailable: select a project in Project Room.")
            else:
                self.status_label.setText(
                    f"No agent windows for {self.project.display_name}. All tab still shows every detected window."
                )
        else:
            self.capacity_label.setText(self._capacity_text())
            if self.scope_tabs.currentIndex() == 0 and self.project is not None:
                self.status_label.setText(
                    f"Showing all {len(self._displayed)} instance(s); {len(project_instances)} belong to "
                    f"{self.project.display_name}. Double-click to focus."
                )
            else:
                self.status_label.setText(f"Showing {len(self._displayed)} instance(s). Double-click to focus.")
        self._update_actions()

    def _selected_instance(self) -> Optional[WindowInstance]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        instance = item.data(Qt.ItemDataRole.UserRole) if item else None
        return instance if isinstance(instance, WindowInstance) else None

    def _update_actions(self) -> None:
        selected = self._selected_instance()
        selectable = bool(selected and selected.selectable)
        self.focus_button.setEnabled(selectable)
        self.close_button.setEnabled(selectable)
        movable = any(item.selectable for item in self._displayed)
        self.cascade_button.setEnabled(movable)
        self.tile_vertical_button.setEnabled(movable)
        self.tile_horizontal_button.setEnabled(movable)
        self._update_activity(selected)

    def _update_activity(self, instance: Optional[WindowInstance]) -> None:
        if instance is None:
            self.activity_label.setText("Activity: select an instance. Last action: unavailable.")
            return
        current = instance.activity or f"{instance.state.upper()} · {instance.title}"
        full_last = instance.last_action or "No explicit SAIPEN action available; terminal output is not exposed."
        display_last = full_last.rsplit("] ", 1)[-1]
        if len(display_last) > 180:
            display_last = display_last[:179] + "…"
        text = f"Current: {current}\nLast logged: {display_last}"
        self.activity_label.setText(text)
        self.activity_label.setToolTip(
            f"Current: {current}\nLast logged: {full_last}\nPrivate model reasoning is not exposed."
        )

    def focus_selected(self) -> None:
        instance = self._selected_instance()
        if not instance or not instance.selectable:
            self.status_label.setText("Focus unavailable: select a RUNNING row with a native window handle.")
            return
        if self.monitor.focus(instance):
            self.status_label.setText(f"Focused: {instance.title}")
        else:
            self.status_label.setText(f"Focus failed for PID {instance.pid}; click its taskbar button once, then retry.")

    def close_selected(self) -> None:
        instance = self._selected_instance()
        if not instance or not instance.selectable:
            self.status_label.setText("Close unavailable: select a RUNNING row with a native window handle.")
            return
        box = QMessageBox(QMessageBox.Icon.Warning, "Close agent window", "", parent=self)
        box.setText(f"Close '{instance.title}' (PID {instance.pid})? Unsaved terminal work may be lost.")
        close_button = box.addButton("Close window", QMessageBox.ButtonRole.DestructiveRole)
        keep_button = box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_button)
        box.exec()
        if box.clickedButton() is not close_button:
            self.status_label.setText(f"Kept running: {instance.title}")
            return
        if self.monitor.close(instance):
            self.status_label.setText(f"Close requested: {instance.title}. Refresh list to confirm exit.")
        else:
            self.status_label.setText(f"Close failed for PID {instance.pid}; window may already be gone.")

    def arrange_displayed(self, mode: str) -> None:
        labels = {
            "cascade": "Cascade",
            "tile_vertical": "Tile columns",
            "tile_horizontal": "Tile rows",
        }
        moved = self.monitor.arrange(self._displayed, mode)
        if moved:
            self.status_label.setText(f"{labels.get(mode, mode)} arranged {moved} agent window(s); unrelated windows untouched.")
        else:
            self.status_label.setText(f"{labels.get(mode, mode)} unavailable: no movable agent windows in this view.")
