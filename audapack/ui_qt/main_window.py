"""Qt MainWindow (Wave M) — Golden Default chrome, responsive tree, async I/O, DnD."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional, Union

from PySide6.QtCore import QFileSystemWatcher, QModelIndex, QPoint, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from audapack.bridge.state import get_generation_file_path, get_generation_info
from audapack.config import app_dir, save_config
from audapack.inaudit import (
    get_active_inaudit_path,
    get_inaudit_selected,
    list_inaudit_layers,
    validate_inaudit_path,
)
from audapack.ingest import ingest_audit_text
from audapack.instances import InstanceMonitor
from audapack.models import Project
from audapack.packing import find_archive_for_project, human_mb, resolve_output_dir
from audapack.services.audit_service import AuditService
from audapack.services.bridge_service import BridgeService
from audapack.services.packing_service import PackingService
from audapack.services.project_service import ProjectService
from audapack.ui_qt.dialogs.inaudit_widget import InauditWidget
from audapack.ui_qt.dialogs.instance_manager import InstanceManagerWidget
from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget
from audapack.ui_qt.models.project_delegate import (
    ProjectItemDelegate,
    compute_info_button_rect,
    compute_row_button_rects,
)
from audapack.ui_qt.models.project_room_model import MIME_TYPE_PROJECT, ProjectRoomModel
from audapack.ui_qt.task_runner import TaskRunner
from audapack.ui_qt.theme.golden_default import PALETTE, GoldenDefault

logger = logging.getLogger(__name__)


class ProjectTreeView(QTreeView):
    """Custom QTreeView with bulletproof Drag & Drop for internal slot reordering and Explorer folder drops."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.viewport().setMouseTracking(True)

    def mousePressEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        idx = self.indexAt(pos)
        # Group header — collapsable bar
        if idx.isValid() and idx.internalId() == 0:
            if self.isExpanded(idx):
                self.collapse(idx)
            else:
                self.expand(idx)
            event.accept()
            return
        button_hit = False
        if idx.isValid() and idx.internalId() != 0:
            rect = self.visualRect(idx)
            main_win = self.window()
            group = idx.data(self.model().ROLES["group"])
            slot = idx.data(self.model().ROLES["slot"])
            proj = self.model().project_at(group, slot)

            # Check [ⓘ] info button first — deterministic, no hover guesswork
            if proj and not idx.data(self.model().ROLES["is_empty_slot"]):
                launchers = getattr(main_win._service.config, "launchers", None) if hasattr(main_win, "_service") else None
                launcher_buttons, gg_rect = compute_row_button_rects(rect, launchers)
                info_rect = compute_info_button_rect(rect, launcher_buttons, gg_rect)
                if info_rect.contains(pos):
                    hover_info = idx.data(self.model().ROLES["hover_info"])
                    if hasattr(main_win, "_show_project_info"):
                        main_win._show_project_info(hover_info or {"project": proj, "group": group, "slot": slot}, anchor_pos=pos)
                    else:
                        # Fallback: at least copy full tooltip text
                        from audapack.ui_qt.models.project_delegate import ProjectItemDelegate as _D
                        tip = _D.build_tooltip(hover_info) if hover_info else f"{proj.display_name} [{group} #{slot}]"
                        QApplication.clipboard().setText(tip.replace("<br>", "\n").replace("<b>", "").replace("</b>", ""))
                    button_hit = True

            # Check launcher buttons [1]..[N]
            if not button_hit and proj and hasattr(main_win, "_on_open_with_launcher"):
                launchers = getattr(main_win._service.config, "launchers", None)
                if launchers:
                    launcher_buttons, _gg = compute_row_button_rects(rect, launchers)
                    for launcher_cfg, btn_rect in launcher_buttons:
                        if btn_rect.contains(pos):
                            main_win._on_open_with_launcher(proj, launcher_cfg.id)
                            button_hit = True
                            break

            # Check Enabled [E] — leftmost 14px (ZIP packing gate)
            if not button_hit:
                en_rect = QRect(rect.left() + 2, rect.top(), 14, rect.height())
                if en_rect.contains(pos) and proj:
                    if hasattr(main_win, "_on_toggle_project_enabled"):
                        main_win._on_toggle_project_enabled(proj)
                    button_hit = True
            # Check Done checkbox [✓] — next 14px
            if not button_hit:
                done_rect = QRect(rect.left() + 16, rect.top(), 14, rect.height())
                if done_rect.contains(pos) and proj:
                    if hasattr(main_win, "_on_toggle_ignored"):
                        main_win._on_toggle_ignored(proj)
                    button_hit = True
            # Check Archive ignore [A] — next 14px
            if not button_hit:
                arch_rect = QRect(rect.left() + 30, rect.top(), 14, rect.height())
                if arch_rect.contains(pos) and proj:
                    if hasattr(main_win, "_on_toggle_archive_ignored"):
                        main_win._on_toggle_archive_ignored(proj)
                    button_hit = True

        if button_hit:
            # Button was clicked — consume event WITHOUT calling super().
            # Calling super() would record press position in Qt's internal state,
            # which allows accidental drag initiation on subsequent mouse move.
            event.accept()
            return

        # No button hit — call super() so Qt initializes internal drag state.
        # Without this, mouseMoveEvent cannot trigger startDrag.
        self._press_pos = pos
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def _deferred_manual_start_drag(self):
        """Deferred drag-start insurance: fires only when Qt's own state
        machine failed to enter DraggingState after a press+move and the left
        button is still held. No-op in the normal (healthy) drag path."""
        if self.state() == QAbstractItemView.State.DraggingState:
            return
        if not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            return
        idx = self.currentIndex()
        if not idx.isValid() or idx.internalId() == 0:
            return
        self.startDrag(Qt.DropAction.MoveAction)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        main_win = self.window()

        idx = self.currentIndex()
        proj = None
        if idx.isValid() and idx.internalId() != 0:
            group = idx.data(self.model().ROLES["group"])
            slot = idx.data(self.model().ROLES["slot"])
            proj = self.model().project_at(group, slot)

        # Enter / Return -> Open in Explorer
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if proj and hasattr(main_win, "_on_open_project_folder"):
                main_win._on_open_project_folder(proj)
                event.accept()
                return

        # Delete -> Remove Project
        if key == Qt.Key.Key_Delete:
            if proj and hasattr(main_win, "_on_delete_project"):
                main_win._on_delete_project(proj)
                event.accept()
                return

        # Space -> Toggle Enabled/Disabled
        if key == Qt.Key.Key_Space:
            if proj and hasattr(main_win, "_on_toggle_project_enabled"):
                main_win._on_toggle_project_enabled(proj)
                event.accept()
                return

        # F2 -> Change Project Folder
        if key == Qt.Key.Key_F2:
            if proj and hasattr(main_win, "_on_change_project_folder"):
                main_win._on_change_project_folder(proj)
                event.accept()
                return

        # Ctrl+Up / Alt+Up -> Move Slot Up (-1)
        if (modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)) and key == Qt.Key.Key_Up:
            if proj and hasattr(main_win, "_on_move_project_step"):
                main_win._on_move_project_step(proj, -1)
                event.accept()
                return

        # Ctrl+Down / Alt+Down -> Move Slot Down (+1)
        if (modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)) and key == Qt.Key.Key_Down:
            if proj and hasattr(main_win, "_on_move_project_step"):
                main_win._on_move_project_step(proj, 1)
                event.accept()
                return

        super().keyPressEvent(event)

    def _resolve_archive_for_drag(self, idx) -> Optional[Path]:
        """Resolves the .zip archive file for a project row index (for external file drag)."""
        if not idx.isValid() or idx.internalId() == 0:
            return None
        try:
            main_win = self.window()
            if not hasattr(main_win, "_service"):
                return None
            group = idx.data(self.model().ROLES["group"])
            slot = idx.data(self.model().ROLES["slot"])
            proj = self.model().project_at(group, slot)
            if not proj or not proj.source_path:
                return None
            out_dir = resolve_output_dir(
                proj.source_path, main_win._service.config.packing,
                fallback=app_dir(), group=proj.priority_group, project=proj,
            )
            arc = find_archive_for_project(proj, out_dir)
            if arc and arc.exists():
                return arc
        except Exception:
            pass
        return None

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        # Fallback: ensure at least the current index is included
        cur = self.currentIndex()
        if cur.isValid() and cur not in indexes:
            indexes = [cur] + indexes
        valid_indexes = [i for i in indexes if i.isValid() and i.internalId() != 0]
        if not valid_indexes:
            return
        mime_data = self.model().mimeData(valid_indexes)
        if not mime_data or not mime_data.hasFormat(MIME_TYPE_PROJECT):
            return

        # Attach archive file URL for external drag-and-drop (ChatGPT, Explorer, etc.)
        # text/uri-list is the standard MIME type for file drag from apps.
        archive_path = self._resolve_archive_for_drag(valid_indexes[0])
        has_archive = archive_path is not None
        if has_archive:
            mime_data.setUrls([QUrl.fromLocalFile(str(archive_path.resolve()))])

        drag = QDrag(self)
        drag.setMimeData(mime_data)

        # Generate a clean Win95 drag preview badge
        proj_name = mime_data.text() or "Project"
        badge_w = 200 if has_archive else 180
        pixmap = QPixmap(badge_w, 24)
        pixmap.fill(QColor(PALETTE["surfaceRaised"]))
        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor(PALETTE["borderGolden"]), 1))
        painter.drawRect(0, 0, badge_w - 1, 23)
        painter.setFont(QFont("Verdana", 9, QFont.Weight.Bold))
        painter.setPen(QColor(PALETTE["borderGolden"]))
        if has_archive:
            painter.drawText(8, 16, f"\U0001F4E4 {proj_name} \u2502 .zip")
        else:
            painter.drawText(8, 16, f"\u21C4 {proj_name}")
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(10, 12))

        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)

    def mouseMoveEvent(self, event):
        """No hover tooltip — info is on the [ⓘ] button. Keep only drag fallback."""
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if event.buttons() & Qt.MouseButton.LeftButton:
            press_pos = getattr(self, "_press_pos", None)
            if (
                press_pos is not None
                and self.state() != QAbstractItemView.State.DraggingState
                and (pos - press_pos).manhattanLength() > QApplication.startDragDistance()
            ):
                self._press_pos = None
                QTimer.singleShot(0, self._deferred_manual_start_drag)
            super().mouseMoveEvent(event)
            return
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        super().leaveEvent(event)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(MIME_TYPE_PROJECT) or md.hasUrls():
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(MIME_TYPE_PROJECT) or md.hasUrls():
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        target_idx = self.indexAt(pos)
        model = self.model()

        # 1. External folder drop from Windows Explorer (0 popup dialogs)
        if md.hasUrls():
            urls = md.urls()
            folders = [Path(u.toLocalFile()) for u in urls if u.isLocalFile() and Path(u.toLocalFile()).is_dir()]
            if folders:
                main_win = self.window()
                target_grp = "MAIN0"
                target_s = None
                if target_idx.isValid():
                    if target_idx.internalId() == 0:
                        target_grp = target_idx.data(model.ROLES["group"]) or "MAIN0"
                    else:
                        target_grp = target_idx.data(model.ROLES["group"]) or "MAIN0"
                        target_s = target_idx.data(model.ROLES["slot"])
                for folder in folders:
                    if hasattr(main_win, "_add_project_from_path"):
                        main_win._add_project_from_path(folder, default_group=target_grp, default_slot=target_s)
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                return

        # 2. Internal project slot move / swap
        if md.hasFormat(MIME_TYPE_PROJECT):
            try:
                payload = json.loads(bytes(md.data(MIME_TYPE_PROJECT)).decode("utf-8"))
                proj_id = payload.get("project_id")
                src_group = payload.get("source_group")
                src_slot = payload.get("source_slot")

                if proj_id and src_group and src_slot is not None:
                    target_group = src_group
                    target_slot = src_slot

                    if target_idx.isValid():
                        if target_idx.internalId() == 0:
                            # Dropped on group header
                            target_group = target_idx.data(model.ROLES["group"]) or src_group
                            target_slot = 1
                            for s in range(1, 7):
                                if model.project_at(target_group, s) is None:
                                    target_slot = s
                                    break
                        else:
                            # Dropped on slot row
                            target_group = target_idx.data(model.ROLES["group"]) or src_group
                            target_slot = target_idx.data(model.ROLES["slot"]) or 1
                    else:
                        # Dropped in blank space -> first available slot
                        target_group = src_group
                        for s in range(1, 7):
                            if model.project_at(target_group, s) is None:
                                target_slot = s
                                break

                    target_slot = max(1, min(6, target_slot))
                    if not (target_group == src_group and target_slot == src_slot):
                        model.project_dropped.emit(proj_id, target_group, target_slot, src_group, src_slot)
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
                return
            except Exception as e:
                logger.debug(f"Drop parse error: {e}")

        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, service: ProjectService, audit_service: Optional[AuditService] = None):
        super().__init__()
        self._service = service
        self._audit_service = audit_service or AuditService(service.config, base_dir=service.base_dir)
        self._packing = PackingService(service.config, base_dir=service.base_dir)
        self._bridge = BridgeService(service.config)
        self._active_project: Optional[Project] = None
        record_path = Path(service.base_dir) / "instances.json" if service.base_dir is not None else None
        self._instance_monitor = InstanceMonitor(record_path=record_path)
        self._instance_manager: Optional[InstanceManagerWidget] = None
        self._instance_monitor.refresh(service.list_projects(), service.config.launchers)

        self._move_generation: int = 0
        self._last_audit_generation: int = 0
        self._active_pack_queue = False
        self._notified_terminal: dict[str, str] = {}
        self._init_tray_icon()

        # Background Task Runner for async I/O
        self.task_runner = TaskRunner(max_threads=4, parent=self)

        self.setWindowTitle("AUDAPACK — Project Room")
        self.resize(*service.config.ui.window_size)
        self.setMinimumSize(280, 200)

        # Restore saved window position (if previously stored and on-screen)
        ui_cfg = service.config.ui
        if getattr(ui_cfg, "window_pos", None):
            try:
                pos = list(ui_cfg.window_pos)
                if len(pos) == 2:
                    self.move(pos[0], pos[1])
            except Exception:
                pass
        if getattr(ui_cfg, "window_maximized", False):
            self.showMaximized()

        # Set main orange app icon
        icon_path = app_dir() / "resources" / "app_icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Presentation model
        self.model = ProjectRoomModel(service, audit_service=self._audit_service, parent=self)
        self.model.project_dropped.connect(self._on_project_dropped)

        # Bottom Toolbar — streamlined: ADD/FOLDER moved to context menu + DnD;
        # PASTE AUDIT redundant when bridge auto-saves + Ctrl+V works everywhere.
        toolbar = QToolBar("Actions", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.BottomToolBarArea, toolbar)

        act_pack = toolbar.addAction("PACK", self._on_pack)
        act_pack.setToolTip("Pack selected project in background")

        act_send_audit = toolbar.addAction("START AUDIT", self._on_send_audit)
        act_send_audit.setToolTip("Queue selected project for a free Brave/ChatGPT audit worker")

        act_pack_all = toolbar.addAction("ALL", self._on_pack_all)
        act_pack_all.setToolTip("Pack all configured projects in background")

        act_copy = toolbar.addAction("AUDIT", self._on_copy_audit)
        act_copy.setToolTip("Copy verified audit to clipboard")

        act_copy_gg = toolbar.addAction("GG", self._on_copy_audit_file_path)
        act_copy_gg.setToolTip("Copy /saipen gg audit path to clipboard (Ctrl+C)")

        act_ia = toolbar.addAction("IA", self._on_ia_copy)
        act_ia.setToolTip("IA — Copy selected INAUDIT path\nShift: saipen gg \"path\"\nCtrl: saipen cc \"path\"")

        act_copy_arc = toolbar.addAction("ZIP", self._on_copy_archive)
        act_copy_arc.setToolTip("Copy packed .zip archive file to clipboard")

        toolbar.addAction("REFRESH", self._on_refresh_all)

        act_reset_marks = toolbar.addAction("RESET MARKS", self._on_reset_project_marks)
        act_reset_marks.setToolTip(
            "Clear all Done dimming, Ignore to archive marks, and audit copy counters; disabled projects stay disabled. Does NOT cancel active audits."
        )

        # Central Tabs: [Project Room] [Instances] [Settings]
        self.tabs = QTabWidget(self)

        # Tab 0: Project Room
        projects_tab = QWidget(self)
        p_layout = QVBoxLayout(projects_tab)
        p_layout.setContentsMargins(4, 4, 4, 4)

        self.tree = ProjectTreeView(projects_tab)
        self.tree.setModel(self.model)
        self.delegate = ProjectItemDelegate(self.tree, config=service.config)
        self.tree.setItemDelegate(self.delegate)
        # Project rows are two lines tall (name plus ZIP/status line), while
        # group headers are one line. Uniform heights clip the second line on
        # every row except the last visible one.
        self.tree.setUniformRowHeights(False)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)

        # Context Menu & Double Click
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.doubleClicked.connect(self._on_tree_double_clicked)

        self.tree.expandAll()
        self.tree.selectionModel().currentChanged.connect(self._on_current_changed)
        p_layout.addWidget(self.tree)

        # Initial selection: auto-select first registered project
        self._auto_select_first_project()

        # Tab 1: embedded instance management; never creates a separate window.
        self._instance_manager = InstanceManagerWidget(
            self._instance_monitor,
            self._service,
            self._active_project,
            parent=self.tabs,
        )

        # Tab 2: INAUDIT — project-local audit layers (audit/1.md ...)
        def _inaudit_changed(_proj):
            try:
                p = _proj or self._selected_project() or self._active_project
                if p:
                    self.model.refresh_inaudit(p.id)
                    if hasattr(self, "tree"):
                        self.tree.viewport().update()
            except Exception:
                pass
        self.inaudit_widget = InauditWidget(parent=self.tabs, on_changed=_inaudit_changed)
        self.inaudit_widget.set_project(self._active_project)

        # Tab 3: Settings
        self.settings_widget = SettingsWidget(service.config, self, on_saved=self._on_settings_saved)

        self.tabs.addTab(projects_tab, "Project Room")
        self.tabs.addTab(self.inaudit_widget, "INAUDIT")
        self.tabs.addTab(self._instance_manager, "Instances")
        self.tabs.addTab(self.settings_widget, "Settings")
        self.tabs.setCurrentIndex(0)

        self.setCentralWidget(self.tabs)
        self.setStyleSheet(GoldenDefault.qss())

        # Golden Default tooltip stylesheet — dark bg, golden text, beveled border.
        # Applied BOTH on this window (child tooltips) and globally via QToolTip so
        # tooltips shown with widget=None (screen-positioned) keep the golden skin
        # instead of reverting to the default white tooltip.
        _tooltip_qss = """
QToolTip {
    background: #232018;
    color: #D4C89A;
    border: 2px solid #5A5040;
    border-top-color: #75663D;
    border-left-color: #75663D;
    border-right-color: #100E08;
    border-bottom-color: #100E08;
    padding: 4px 6px;
    font-family: "Verdana";
    font-size: 10px;
    selection-background-color: #3D372A;
    selection-color: #F0D060;
}
QToolTip QLabel {
    background: transparent;
    color: #D4C89A;
}
"""
        self.setStyleSheet(self.styleSheet() + _tooltip_qss)
        try:
            QToolTip.setStyleSheet(_tooltip_qss)
        except Exception:
            pass
        self.statusBar().showMessage("AUDAPACK Ready")

        # Comprehensive Shortcuts
        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self.copy_shortcut.activated.connect(self._on_copy_audit_file_path)

        self.paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.paste_shortcut.activated.connect(self._on_paste_audit)

        self.terminal_shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        self.terminal_shortcut.activated.connect(self._on_open_terminal)

        self.open_folder_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        self.open_folder_shortcut.activated.connect(self._on_open_project_folder)

        self.pack_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.pack_shortcut.activated.connect(self._on_pack)

        self.pack_all_shortcut = QShortcut(QKeySequence("Ctrl+Shift+P"), self)
        self.pack_all_shortcut.activated.connect(self._on_pack_all)

        self.pack_all_a_shortcut = QShortcut(QKeySequence("Ctrl+A"), self)
        self.pack_all_a_shortcut.activated.connect(self._on_pack_all)

        self.quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        self.quit_shortcut.activated.connect(self.close)

        # Ctrl 1-6: Open with launcher 1-6 (ordered enabled agents)
        self._launcher_shortcuts = []
        for _idx in range(6):
            sc = QShortcut(QKeySequence(f"Ctrl+{_idx+1}"), self)
            # capture idx via default arg
            sc.activated.connect(lambda _checked=False, _i=_idx: self._on_open_with_launcher_index(_i))
            self._launcher_shortcuts.append(sc)

        self.refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        self.refresh_shortcut.activated.connect(self._on_refresh_all)

        self.folder_shortcut = QShortcut(QKeySequence("F2"), self)
        self.folder_shortcut.activated.connect(self._on_change_project_folder)

        # Temperature Timer: 60s in-memory recalculation (0 disk reads)
        self.temp_timer = QTimer(self)
        self.temp_timer.setInterval(60000)
        self.temp_timer.timeout.connect(self._on_temperature_tick)
        self.temp_timer.start()

        # Bridge generation watcher with slow polling fallback.
        self._generation_path = get_generation_file_path()
        self._generation_watcher = QFileSystemWatcher(self)
        self._generation_watcher.fileChanged.connect(self._on_generation_fs_event)
        self._generation_watcher.directoryChanged.connect(self._on_generation_fs_event)
        self._generation_debounce = QTimer(self)
        self._generation_debounce.setSingleShot(True)
        self._generation_debounce.setInterval(100)
        self._generation_debounce.timeout.connect(self._on_check_bridge_generation)
        self._generation_watch_paths()
        self.bridge_timer = QTimer(self)
        self.bridge_timer.setInterval(30000)
        self.bridge_timer.timeout.connect(self._on_check_bridge_generation)
        self.bridge_timer.start()

        # Pack progress flush timer: 250ms. Worker threads write to
        # ``_pack_progress_buffer``; the GUI thread consumes and pushes into the
        # model. This is the single chokepoint that guarantees the Qt model is
        # only ever touched from the GUI thread.
        self._pack_progress_buffer: dict[str, tuple[int, int, int, str]] = {}
        self._pack_progress_lock = threading.Lock()
        self.pack_progress_timer = QTimer(self)
        self.pack_progress_timer.setInterval(250)
        self.pack_progress_timer.timeout.connect(self._flush_pack_progress)
        self.pack_progress_timer.start()

        self.dispatch_status_timer = QTimer(self)
        self.dispatch_status_timer.setInterval(10000)
        self.dispatch_status_timer.timeout.connect(self._refresh_dispatch_status_async)
        self.dispatch_status_timer.start()

        # Async initial enrichment (time-to-interactive optimization)
        QTimer.singleShot(50, self._async_initial_enrichment)

    # ---------------------------------------------------------------- Startup Flow

    def closeEvent(self, event):
        """Persist window geometry (size, position, maximized state) on close.

        CORE-003: narrow merge transaction — reload the latest on-disk config
        under the registry lock and update ONLY the geometry fields. Never write
        the stale project registry or unrelated settings from self._service.config
        during close, or concurrent Bridge/CLI project mutations get silently lost.
        """
        try:
            from audapack.config import cross_process_lock, get_registry_lock_path, load_config
            geometry = {
                "window_size": [self.width(), self.height()],
                "window_pos": [self.x(), self.y()],
                "window_maximized": bool(self.isMaximized()),
            }
            base = getattr(self._service, "_base_dir", None)
            lock_path = get_registry_lock_path(base)
            with cross_process_lock(lock_path):
                latest = load_config(base)
                latest.ui.window_size = geometry["window_size"]
                latest.ui.window_pos = geometry["window_pos"]
                latest.ui.window_maximized = geometry["window_maximized"]
                save_config(latest, base)
        except Exception:
            pass
        super().closeEvent(event)

    def _async_initial_enrichment(self):
        """Enriches audit snapshots and Bridge status asynchronously without blocking startup."""
        def _scan_all():
            return self._audit_service.refresh_all()

        def _on_scanned(snaps):
            for p_id, snap in snaps.items():
                self.model.update_audit_snapshot(p_id, snap)

        self.task_runner.submit("audit:initial_enrichment", _scan_all, on_success=_on_scanned)

        def _check_bridge():
            return self._bridge.status()

        def _on_bridge_done(st):
            healthy = st.get("healthy", False)
            status_txt = "Bridge CONNECTED" if healthy else "Bridge OFFLINE"
            self.statusBar().showMessage(f"AUDAPACK Ready · {status_txt}")

        self.task_runner.submit("bridge:initial_check", _check_bridge, on_success=_on_bridge_done)
        self._refresh_dispatch_status_async()

    def _refresh_dispatch_status_async(self):
        """Refresh live browser dispatch state without blocking the Qt thread."""
        def _load():
            return self._bridge.browser_jobs(), self._bridge.runtime_status()

        def _apply(result):
            response, status = result
            if not response.get("ok"):
                return
            workers_by_id: dict[str, dict] = {}
            used_labels: set[str] = set()
            for w in (status.get("browser", {}) if isinstance(status, dict) else {}).get("workers", []):
                wid = str(w.get("worker_id", ""))
                if wid:
                    bn = str(w.get("browser_name", "") or "Browser")
                    widx = 1
                    label = f"{bn} #{widx}"
                    while label in used_labels:
                        widx += 1
                        label = f"{bn} #{widx}"
                    used_labels.add(label)
                    workers_by_id[wid] = {"browser_name": bn, "friendly_worker_label": label}
            active = {}
            for job in response.get("jobs", []):
                project_id = str(job.get("project_id") or "")
                if project_id:
                    wid = str(job.get("assigned_worker_id") or "")
                    if wid and wid in workers_by_id:
                        job["browser_name"] = workers_by_id[wid]["browser_name"]
                        job["friendly_worker_label"] = workers_by_id[wid]["friendly_worker_label"]
                    active[project_id] = job
                    self._notify_dispatch_terminal(
                        str(job.get("dispatch_id") or ""),
                        str(job.get("state") or ""),
                        str(job.get("project_name") or project_id),
                    )
            for proj in self._service.list_projects():
                self.model.update_dispatch_snapshot(proj.id, active.get(proj.id))
            browser = status.get("browser", {}) if isinstance(status, dict) else {}
            if browser:
                self.statusBar().showMessage(
                    f"BRIDGE ✓ | W {browser.get('active_workers', 0)}/{browser.get('max_workers', 6)}  "
                    f"CLEAN {browser.get('clean_workers', 0)}  "
                    f"BUSY {browser.get('busy_workers', 0)}  "
                    f"Q {browser.get('queued_jobs', 0)}  "
                    f"RUN {browser.get('active_jobs', 0)}  "
                    f"! {browser.get('blocked_jobs', 0)}"
                )

        self.task_runner.submit_coalesced("bridge:dispatch_status", _load, on_success=_apply)

    # ---------------------------------------------------------------- Selection

    def _auto_select_first_project(self):
        """Auto-selects the first registered project in the tree on startup."""
        for g_idx, group in enumerate(self.model._groups):
            for slot in range(1, 7):
                p = self.model.project_at(group, slot)
                if p:
                    grp_idx = self.model.index(g_idx, 0, QModelIndex())
                    slot_idx = self.model.index(slot - 1, 0, grp_idx)
                    if slot_idx.isValid():
                        self.tree.setCurrentIndex(slot_idx)
                        self._active_project = p
                        return

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex):
        if not current.isValid():
            self._active_project = None
            if hasattr(self, "inaudit_widget"):
                self.inaudit_widget.set_project(None)
            return
        node_type = self.model.data(current, self.model.ROLES["node_type"])
        if node_type != "slot":
            self._active_project = None
            if hasattr(self, "inaudit_widget"):
                self.inaudit_widget.set_project(None)
            return
        group = self.model.data(current, self.model.ROLES["group"])
        slot = self.model.data(current, self.model.ROLES["slot"])
        self._active_project = self.model.project_at(group, slot)
        if hasattr(self, "inaudit_widget"):
            self.inaudit_widget.set_project(self._active_project)

    def _selected_project(self) -> Optional[Project]:
        """Resolves the currently targeted project with multi-layered fallback."""
        # 1. Direct inspection of current tree index
        idx = self.tree.currentIndex()
        if idx.isValid():
            node_type = self.model.data(idx, self.model.ROLES["node_type"])
            if node_type == "slot":
                group = self.model.data(idx, self.model.ROLES["group"])
                slot = self.model.data(idx, self.model.ROLES["slot"])
                p = self.model.project_at(group, slot)
                if p:
                    self._active_project = p
                    return p

        # 2. Selection model inspection
        sel_indexes = self.tree.selectionModel().selectedIndexes()
        for sel_idx in sel_indexes:
            if sel_idx.isValid():
                node_type = self.model.data(sel_idx, self.model.ROLES["node_type"])
                if node_type == "slot":
                    group = self.model.data(sel_idx, self.model.ROLES["group"])
                    slot = self.model.data(sel_idx, self.model.ROLES["slot"])
                    p = self.model.project_at(group, slot)
                    if p:
                        self._active_project = p
                        return p

        # 3. Active cached project (verify it still exists in registry)
        if self._active_project is not None:
            if self._service.get_project(self._active_project.id) is not None:
                return self._active_project
            self._active_project = None

        # 4. Fallback: select and return the first project found in the model
        for g_idx, group in enumerate(self.model._groups):
            for slot in range(1, 7):
                p = self.model.project_at(group, slot)
                if p:
                    grp_idx = self.model.index(g_idx, 0, QModelIndex())
                    slot_idx = self.model.index(slot - 1, 0, grp_idx)
                    if slot_idx.isValid():
                        self.tree.setCurrentIndex(slot_idx)
                    self._active_project = p
                    return p

        self.statusBar().showMessage("No project selected. Use ADD to select a project folder.")
        return None

    # ---------------------------------------------------------------- Agent Instances

    def _refresh_instance_snapshot(self) -> None:
        self._instance_monitor.refresh(self._service.list_projects(), self._service.config.launchers)
        if hasattr(self, "tree"):
            self.tree.viewport().update()

    def _launcher_config(self, launcher_id: str):
        return next(
            (launcher for launcher in self._service.config.launchers if launcher.id == launcher_id),
            None,
        )

    def _launcher_block_reason(self, launcher_id: str) -> str:
        launcher = self._launcher_config(launcher_id)
        return self._instance_monitor.block_reason(launcher) if launcher is not None else ""

    def _register_agent_launch(self, process: Any, launcher_id: str, project: Project) -> None:
        if self._instance_monitor.track_launch(getattr(process, "pid", 0), launcher_id, project):
            self._refresh_instance_snapshot()
            QTimer.singleShot(600, self._refresh_instances_after_launch)

    def _refresh_instances_after_launch(self) -> None:
        self._refresh_instance_snapshot()
        if self._instance_manager is not None:
            self._instance_manager.refresh_instances()

    def _show_instance_manager(self, project: Optional[Project] = None) -> None:
        target = project if isinstance(project, Project) else self._selected_project()
        if target is None:
            self.statusBar().showMessage("Instance manager unavailable: no project selected.")
            return
        if self._instance_manager is None:
            self.statusBar().showMessage("Instance manager unavailable: tab was not initialized.")
            return
        self._instance_manager.set_project(target)
        self.tabs.setCurrentWidget(self._instance_manager)

    # ---------------------------------------------------------------- Drag & Drop Optimistic Flow

    def _on_project_dropped(self, project_id: str, target_group: str, target_slot: int, src_group: str, src_slot: int):
        """Optimistic UI update on drop + background async persistence with rollback."""
        proj = self._service.get_project(project_id)
        if not proj:
            return

        swap_proj = self.model.project_at(target_group, target_slot)

        # 1. Immediate visual mutation in model (0 model reset, 0 filesystem I/O)
        optimistic_proj = Project(
            id=proj.id,
            display_name=proj.display_name,
            source_path=proj.source_path,
            priority_group=target_group.upper(),
            slot=target_slot,
            archive_name=proj.archive_name,
            audit_project_name=proj.audit_project_name,
            enabled=proj.enabled,
            ignored=proj.ignored,
            last_copied_audit_hash=proj.last_copied_audit_hash,
            last_copied_at=proj.last_copied_at,
            last_copied_archive_path=proj.last_copied_archive_path,
            last_copied_archive_at=proj.last_copied_archive_at,
        )

        optimistic_swap = None
        if swap_proj and swap_proj.id != project_id:
            optimistic_swap = Project(
                id=swap_proj.id,
                display_name=swap_proj.display_name,
                source_path=swap_proj.source_path,
                priority_group=src_group.upper(),
                slot=src_slot,
                archive_name=swap_proj.archive_name,
                audit_project_name=swap_proj.audit_project_name,
                enabled=swap_proj.enabled,
                ignored=swap_proj.ignored,
                last_copied_audit_hash=swap_proj.last_copied_audit_hash,
                last_copied_at=swap_proj.last_copied_at,
                last_copied_archive_path=swap_proj.last_copied_archive_path,
                last_copied_archive_at=swap_proj.last_copied_archive_at,
            )

        self.model.apply_project_move(
            src_group,
            src_slot,
            target_group,
            target_slot,
            optimistic_proj,
            swapped_project=optimistic_swap,
        )
        # Belt-and-braces repaint: layoutChanged alone can miss a viewport
        # refresh on some Windows drivers when rows are custom-painted.
        self.tree.viewport().update()
        # Select the project at its new position so the user sees the selection follow
        new_idx = self.model.index_for_slot(target_group, target_slot)
        if new_idx.isValid():
            self.tree.setCurrentIndex(new_idx)
        self.statusBar().showMessage(f"Moved {proj.display_name} -> [{target_group} #{target_slot}]")

        # 2. Async persistence with generation tracking & rollback protection
        self._move_generation += 1
        gen = self._move_generation

        def _persist():
            return self._service.move_project(project_id, target_group, target_slot)

        def _on_success(res):
            if not res.ok and gen == self._move_generation:
                # Rollback on failed persistence
                self.model.apply_project_move(
                    target_group,
                    target_slot,
                    src_group,
                    src_slot,
                    proj,
                    swapped_project=swap_proj,
                )
                # Select reverted position
                revert_idx = self.model.index_for_slot(src_group, src_slot)
                if revert_idx.isValid():
                    self.tree.setCurrentIndex(revert_idx)
                self.tree.viewport().update()
                self.statusBar().showMessage(f"Move failed: reverting {proj.display_name}")
            elif res.ok and gen == self._move_generation:
                # Confirm: re-read the updated project from disk and emit targeted updates.
                updated = self._service.get_project(project_id)
                if updated:
                    self.model.update_project_metadata(updated)
                idx = self.model.index_for_project_id(project_id)
                if idx.isValid():
                    self.tree.setCurrentIndex(idx)

        def _on_error(err):
            if gen == self._move_generation:
                # Rollback on exception
                self.model.apply_project_move(
                    target_group,
                    target_slot,
                    src_group,
                    src_slot,
                    proj,
                    swapped_project=swap_proj,
                )
                revert_idx = self.model.index_for_slot(src_group, src_slot)
                if revert_idx.isValid():
                    self.tree.setCurrentIndex(revert_idx)
                self.statusBar().showMessage(f"Move error: {err}")

        self.task_runner.submit(f"registry:move:{gen}", _persist, on_success=_on_success, on_error=_on_error)

    # ---------------------------------------------------------------- Direct Project Actions (0 Popups)

    def _add_project_from_path(
        self,
        folder_path: Union[str, Path],
        default_group: str = "MAIN0",
        default_slot: Optional[int] = None,
    ) -> Optional[Project]:
        """Directly registers a folder as a project with 0 popup dialogs and auto-slot selection."""
        p_path = Path(folder_path).resolve()
        if not p_path.is_dir():
            self.statusBar().showMessage(f"Path is not a directory: {p_path}")
            return None

        # Check if project with this source_path already exists
        existing = next((p for p in self._service.list_projects() if Path(p.source_path).resolve() == p_path), None)
        if existing:
            self.statusBar().showMessage(f"Project '{existing.display_name}' already exists in [{existing.priority_group} #{existing.slot}]")
            idx = self.model.index_for_project_id(existing.id)
            if idx.isValid():
                self.tree.setCurrentIndex(idx)
            return existing

        display_name = p_path.name
        target_group = default_group.upper()

        # Resolve slot: if default_slot is given and free, use it; otherwise find first empty slot
        target_slot = default_slot
        if target_slot is None or self.model.project_at(target_group, target_slot) is not None:
            target_slot = None
            for s in range(1, 7):
                if self.model.project_at(target_group, s) is None:
                    target_slot = s
                    break
            if target_slot is None:
                for grp in self._service.active_groups():
                    for s in range(1, 7):
                        if self.model.project_at(grp, s) is None:
                            target_group = grp
                            target_slot = s
                            break
                    if target_slot is not None:
                        break
            if target_slot is None:
                target_slot = 1

        try:
            p = self._service.add_project(
                display_name=display_name,
                source_path=str(p_path),
                priority_group=target_group,
                slot=target_slot,
                enabled=True,
            )
            self.model.reload()
            self.tree.expandAll()
            idx = self.model.index_for_project_id(p.id)
            if idx.isValid():
                self.tree.setCurrentIndex(idx)
            self.statusBar().showMessage(f"✓ Added project {p.display_name} -> [{p.priority_group} #{p.slot}]")
            return p
        except Exception as e:
            self.statusBar().showMessage(f"Error adding project: {e}")
            return None

    def _on_add_project(self, default_group: str = "MAIN0", default_slot: Optional[int] = None):
        """Directly prompts for folder selection (0 popup dialogs) and registers project seamlessly."""
        self._flash_status("\u25B6 ADD: Select project folder...", "#D4A840")
        cur_dir = str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Select Project Folder", cur_dir)
        if selected:
            self._add_project_from_path(selected, default_group=default_group, default_slot=default_slot)

    def _on_change_project_folder(self, proj: Optional[Any] = None):
        """Directly prompts for a new folder (0 popup dialogs) and updates the project."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            self._flash_status("FOLDER: No project selected", "#D66464")
            return
        self._flash_status(f"\u25B6 FOLDER: {target.display_name} — select new folder...", "#D4A840")
        cur_dir = str(target.source_path) if target.source_path else str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, f"Select New Folder for '{target.display_name}'", cur_dir)
        if selected:
            p_path = Path(selected).resolve()
            new_name = p_path.name
            try:
                self._service.update_project(
                    target.id,
                    lambda p: (
                        setattr(p, "source_path", str(p_path)),
                        setattr(p, "display_name", new_name),
                    ),
                )
                self.model.reload()
                self.tree.expandAll()
                idx = self.model.index_for_project_id(target.id)
                if idx.isValid():
                    self.tree.setCurrentIndex(idx)
                self.statusBar().showMessage(f"✓ Updated {new_name} path -> {p_path}")
            except Exception as e:
                self.statusBar().showMessage(f"Error updating project: {e}")

    def _on_move_project_step(self, proj: Optional[Any] = None, step: int = -1):
        """Moves project up (-1) or down (+1) across slots without popups."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
        res = self._service.move_project_step(target.id, step)
        if res:
            self.model.reload()
            self.tree.expandAll()
            idx = self.model.index_for_project_id(target.id)
            if idx.isValid():
                self.tree.setCurrentIndex(idx)
            self.statusBar().showMessage(f"✓ Moved {target.display_name} -> [{res.new_group} #{res.new_slot}]")

    def _on_move_project_to_group(self, proj: Optional[Any] = None, target_group: str = "MAIN0"):
        """Moves project to first available slot in target group without popups."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
        grp = target_group.upper()
        target_slot = 1
        for s in range(1, 7):
            if self.model.project_at(grp, s) is None:
                target_slot = s
                break
        res = self._service.move_project(target.id, grp, target_slot)
        if res.ok:
            self.model.reload()
            self.tree.expandAll()
            idx = self.model.index_for_project_id(target.id)
            if idx.isValid():
                self.tree.setCurrentIndex(idx)
            self.statusBar().showMessage(f"✓ Moved {target.display_name} -> [{grp} #{target_slot}]")

    def _on_delete_project(self, proj: Optional[Any] = None):
        """Removes the project from registry immediately without modal confirmation popups."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
        if self._active_project and self._active_project.id == target.id:
            self._active_project = None
        try:
            self._service.delete_project(target.id)
            self.model.reload()
            self.tree.expandAll()
            self._auto_select_first_project()
            self.statusBar().showMessage(f"✓ Removed project {target.display_name}")
        except Exception as e:
            self.statusBar().showMessage(f"Error removing project: {e}")

    # ---------------------------------------------------------------- Async Actions

    def _flash_status(self, msg: str, color: str = "#4A7A20", duration_ms: int = 0):
        """Visual feedback flash on the status bar after button press.

        PERF-005: the reusable timer is created and connected EXACTLY once (on
        first use) to one stable instance handler. Each call only updates state
        and restarts the single-shot timer, so historical flashes never leave
        retained signal connections behind.
        """
        self._flash_gen = getattr(self, "_flash_gen", 0) + 1
        if not hasattr(self, "_flash_timer"):
            self._flash_timer = QTimer(self)
            self._flash_timer.setSingleShot(True)
            self._flash_timer.timeout.connect(self._flash_clear_stale)
        self.statusBar().showMessage(msg)
        self.statusBar().setStyleSheet(f"color: {color}; font-weight: bold; font-size: 11px;")
        if duration_ms <= 0:
            duration_ms = getattr(self._service.config.ui, "flash_duration_ms", 800)
        self._flash_timer.start(duration_ms)

    def _flash_clear_stale(self):
        """PERF-005: single stable timer handler. A timer event belonging to an
        earlier flash must never clear styling belonging to a later flash."""
        if hasattr(self, "_flash_timer"):
            self._flash_timer.stop()
        self.statusBar().setStyleSheet("")

    def _init_tray_icon(self) -> None:
        """W8: optional native tray notification. Best-effort only -- a headless
        or tray-less session must never crash the desktop app."""
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                self._tray_icon = None
                return
            icon_path = app_dir() / "resources" / "app_icon.png"
            icon = QIcon(str(icon_path)) if icon_path.exists() else self.windowIcon()
            self._tray_icon = QSystemTrayIcon(icon, self)
            self._tray_icon.setToolTip("AUDAPACK — Project Room")
            self._tray_icon.show()
        except Exception:
            self._tray_icon = None

    def _notify_dispatch_terminal(self, dispatch_id: str, state: str, project_name: str) -> None:
        """Native notification only when THIS running UI observes a transition
        from a non-terminal state to a terminal one.

        W9.5: a process-local exactly-once map is not enough — on AUDAPACK
        restart it is empty, so every historical terminal job would re-notify
        (the "audit complete" toast parade). We therefore record the last seen
        state per dispatch and only toast when the previous observation was
        non-terminal and the current one is terminal. Jobs that were already
        terminal before this window started stay silent.
        """
        tray = getattr(self, "_tray_icon", None)
        terminal = {"COMPLETE", "BLOCKED", "FAILED", "CANCELLED"}
        prev = self._notified_terminal.get(dispatch_id, "")
        self._notified_terminal[dispatch_id] = state
        if tray is None or state not in terminal:
            return
        if prev in terminal:
            return
        if state == "COMPLETE":
            tray.showMessage(
                "AUDAPACK — audit complete",
                f"{project_name}: audit saved successfully.",
                QSystemTrayIcon.Information,
                6000,
            )
        elif state == "BLOCKED":
            tray.showMessage(
                "AUDAPACK — audit needs attention",
                f"{project_name}: audit is blocked.",
                QSystemTrayIcon.Warning,
                8000,
            )
        elif state == "FAILED":
            tray.showMessage(
                "AUDAPACK — audit failed",
                f"{project_name}: audit failed.",
                QSystemTrayIcon.Critical,
                8000,
            )

    def _on_send_audit(self):
        proj = self._selected_project()
        if not proj:
            self._flash_status("SEND AUDIT: select a project first", "#D66464")
            return
        key = f"dispatch:{proj.id}"
        if self.task_runner.is_running(key):
            self._flash_status(f"START AUDIT already queued for {proj.display_name}", "#D4A840")
            return
        profile = getattr(self._service.config.audits, "profile", "quick3") or "quick3"
        self._flash_status(f"START AUDIT: preparing {proj.display_name}", "#D4A840")

        def _prepare():
            active = self._bridge.active_browser_job(proj.id)
            if active:
                return {"active": active}
            health = self._bridge.runtime_status()
            if not health.get("healthy"):
                started, _message = self._bridge.start()
                if not started or not self._bridge.runtime_status().get("healthy"):
                    raise RuntimeError("Bridge is not healthy")
            packed = self._packing.ensure_fresh_archive(proj.id)
            if not packed.success or not packed.output_path:
                raise RuntimeError(packed.error_message or "Packing failed")
            return {"archive": packed.output_path}

        def _done(prepared):
            if prepared.get("active"):
                active = prepared["active"]
                self._flash_status(
                    f"START AUDIT already active for {proj.display_name} ({active.get('state', 'QUEUED')})",
                    "#D4A840",
                )
                return
            response = self._bridge.submit_browser_audit(proj, prepared["archive"], profile)
            if response.get("ok"):
                dispatch = response.get("dispatch", {})
                self._flash_status(
                    f"START AUDIT: {proj.display_name} queued ({dispatch.get('dispatch_id', 'queued')})",
                    "#D4A840",
                )
            else:
                self._flash_status(f"START AUDIT failed: {response.get('error', 'Bridge unavailable')}", "#D66464")

        def _error(error):
            self._flash_status(f"START AUDIT failed: {error}", "#D66464")

        self.task_runner.submit(key, _prepare, on_success=_done, on_error=_error)

    def _on_cancel_browser_audit(self, proj, dispatch):
        """Async cancel via TaskRunner — Qt UI must never block on HTTP."""
        did = str(dispatch.get("dispatch_id") or "")
        if not did:
            self._flash_status("Cancel failed: no dispatch id", "#D66464")
            return
        self._flash_status(f"Cancelling audit for {proj.display_name}...", "#D4A840")
        key = f"dispatch-cancel:{did}"

        def _cancel():
            return self._bridge.cancel_browser_job(did)

        def _on_cancelled(response):
            if response.get("ok"):
                self._flash_status(f"Audit cancelled for {proj.display_name}", "#D4A840")
                self.model.update_dispatch_snapshot(proj.id, None)
            else:
                self._flash_status(f"Cancel failed: {response.get('error', 'Bridge error')}", "#D66464")

        def _on_cancel_error(err):
            self._flash_status(f"Cancel error: {err}", "#D66464")

        self.task_runner.submit(key, _cancel, on_success=_on_cancelled, on_error=_on_cancel_error)

    def _on_pack(self):
        """Async background packing — GUI thread remains 100% interactive."""
        proj = self._selected_project()
        if not proj:
            return

        if self.task_runner.is_running(f"pack:{proj.id}"):
            self._flash_status(f"Packing already in progress for {proj.display_name}", "#D66464")
            return

        self.model.update_pack_state(proj.id, "PACKING")
        self._flash_status(f"\u25B6 PACK: {proj.display_name}", "#D4A840")
        run_id = self.model.get_current_pack_run_id(proj.id)


        def _do_pack():
            return self._packing.pack_project(
                proj.id,
                progress_callback=self._make_pack_progress_callback(proj.id, run_id),
            )

        def _on_pack_done(res):
            if res.success:
                out_name = res.output_path.name if res.output_path else "archive.zip"
                self.model.update_pack_state(proj.id, "COMPLETE", out_name)
                self.statusBar().showMessage(f"✓ Packed {proj.display_name} -> {out_name}")
            else:
                self.model.update_pack_state(proj.id, "FAILED", res.error_message)
                self.statusBar().showMessage(f"PACK FAILED ({proj.display_name}): {res.error_message}")

        def _on_pack_error(err):
            self.model.update_pack_state(proj.id, "FAILED", str(err))
            self.statusBar().showMessage(f"PACK ERROR ({proj.display_name}): {err}")

        self.task_runner.submit(f"pack:{proj.id}", _do_pack, on_success=_on_pack_done, on_error=_on_pack_error)

    def _start_sequential_pack_queue(self, projects: list[Project], queue_label: str = "PACK ALL"):
        """Executes a sequential top-to-bottom packing queue."""
        if self._active_pack_queue:
            self.statusBar().showMessage(f"{queue_label}: another packing queue is already running")
            return False
        self._active_pack_queue = True
        total_count = len(projects)
        for p in projects:
            self.model.update_pack_state(p.id, "QUEUED")

        queue = list(projects)
        packed_count = [0]

        def _step_next():
            if not queue:
                self._active_pack_queue = False
                self.statusBar().showMessage(f"✓ {queue_label} COMPLETE: Packed {packed_count[0]}/{total_count} projects")
                return

            current_proj = queue.pop(0)
            p_id = current_proj.id
            p_name = current_proj.display_name
            curr_num = total_count - len(queue)

            self.model.update_pack_state(p_id, "PACKING")
            self.statusBar().showMessage(f"{queue_label} ({curr_num}/{total_count}): Packing {p_name}...")
            run_id = self.model.get_current_pack_run_id(p_id)

            def _do_pack():
                return self._packing.pack_project(
                    p_id,
                    progress_callback=self._make_pack_progress_callback(p_id, run_id),
                )

            def _on_pack_done(res):
                if res.success:
                    out_name = res.output_path.name if res.output_path else "archive.zip"
                    self.model.update_pack_state(p_id, "COMPLETE", out_name)
                    packed_count[0] += 1
                else:
                    self.model.update_pack_state(p_id, "FAILED", res.error_message)
                    self.statusBar().showMessage(f"PACK FAILED ({p_name}): {res.error_message}")
                QTimer.singleShot(10, _step_next)

            def _on_pack_error(err):
                self.model.update_pack_state(p_id, "FAILED", str(err))
                self.statusBar().showMessage(f"PACK ERROR ({p_name}): {err}")
                QTimer.singleShot(10, _step_next)

            self.task_runner.submit(f"pack:{p_id}", _do_pack, on_success=_on_pack_done, on_error=_on_pack_error)

        _step_next()
        return True

    def _on_pack_all(self):
        """Sequential background packing queue of all projects from top to bottom."""
        ordered_projects = []
        skipped_arch = 0
        for group in self.model._groups:
            for slot in range(1, 7):
                p = self.model.project_at(group, slot)
                if p and p.source_path and p.enabled:
                    if getattr(p, "ignore_archive", False):
                        skipped_arch += 1
                        continue
                    ordered_projects.append(p)

        if not ordered_projects:
            if skipped_arch:
                self._flash_status(f"PACK ALL: All {skipped_arch} active projects are 'Ignore to archive'.", "#D66464")
            else:
                self._flash_status("PACK ALL: No active projects configured.", "#D66464")
            return

        suffix = f" (+{skipped_arch} ignored to archive skipped)" if skipped_arch else ""
        self._flash_status(f"\u25B6 PACK ALL: {len(ordered_projects)} projects queued{suffix}", "#D4A840")
        self._start_sequential_pack_queue(ordered_projects, queue_label="PACK ALL")

    def _on_pack_specific(self, proj: Project):
        if not proj:
            return
        if self.task_runner.is_running(f"pack:{proj.id}"):
            self.statusBar().showMessage(f"Packing already in progress for {proj.display_name}")
            return

        self.model.update_pack_state(proj.id, "PACKING")
        self.statusBar().showMessage(f"Packing {proj.display_name} in background...")
        run_id = self.model.get_current_pack_run_id(proj.id)


        def _do_pack():
            return self._packing.pack_project(
                proj.id,
                progress_callback=self._make_pack_progress_callback(proj.id, run_id),
            )

        def _on_pack_done(res):
            if res.success:
                out_name = res.output_path.name if res.output_path else "archive.zip"
                self.model.update_pack_state(proj.id, "COMPLETE", out_name)
                self.statusBar().showMessage(f"✓ Packed {proj.display_name} -> {out_name}")
            else:
                self.model.update_pack_state(proj.id, "FAILED", res.error_message)
                self.statusBar().showMessage(f"PACK FAILED ({proj.display_name}): {res.error_message}")

        def _on_pack_error(err):
            self.model.update_pack_state(proj.id, "FAILED", str(err))
            self.statusBar().showMessage(f"PACK ERROR ({proj.display_name}): {err}")

        self.task_runner.submit(f"pack:{proj.id}", _do_pack, on_success=_on_pack_done, on_error=_on_pack_error)

    def _on_copy_audit(self):
        """Copies verified audit to clipboard for selected project."""
        proj = self._selected_project()
        if not proj:
            self._flash_status("COPY AUDIT: No project selected", "#D66464")
            return
        self._flash_status(f"\u25B6 COPY AUDIT: {proj.display_name}", "#D4A840")
        self._on_copy_audit_specific(proj)

    def _on_copy_audit_specific(self, proj: Project):
        """Copies verified audit to clipboard for a specific project."""
        if not proj:
            return

        ok, content, sha256 = self._audit_service.copy_latest_campaign(proj.id)
        if not ok or not content:
            self.statusBar().showMessage(f"COPY AUDIT: No complete audit handoff for {proj.display_name}")
            return

        QApplication.clipboard().setText(content)
        from datetime import datetime, timezone

        def _inc(p):
            p.last_copied_audit_hash = sha256
            p.last_copied_at = datetime.now(timezone.utc).isoformat()
            p.audit_copy_count = int(getattr(p, "audit_copy_count", 0) or 0) + 1

        self._service.update_project(proj.id, _inc)
        updated = self._service.get_project(proj.id)
        if updated:
            self.model.update_project_metadata(updated)
        cnt = getattr(updated, "audit_copy_count", 1) if updated else 1
        self.statusBar().showMessage(f"✓ COPIED AUDIT for {proj.display_name} ({len(content)} chars) ×{cnt}")

    def _on_copy_audit_file_path(self, proj: Optional[Any] = None):
        """Copies audit file path (prefixed with '/saipen gg ' if SAIPEN is detected) to clipboard."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return

        audit_path = self._audit_service.get_preferred_audit_file_path(target.id)
        if not audit_path or not audit_path.exists():
            self.statusBar().showMessage(f"No audit markdown file found for {target.display_name}")
            return

        resolved_path = str(audit_path.resolve())
        has_saipen = bool(target.source_path and (Path(target.source_path) / ".saipen").is_dir())

        if has_saipen:
            gg_tpl = getattr(self._service.config.ui, "gg_template", "")
            if gg_tpl and "{path}" in gg_tpl:
                text_to_copy = gg_tpl.replace("{path}", resolved_path)
            else:
                text_to_copy = f"/saipen gg {resolved_path}"
            short_name = Path(resolved_path).name
            self.statusBar().showMessage(f"✓ Copied GG command: {short_name}")
        else:
            text_to_copy = resolved_path
            self.statusBar().showMessage(f"✓ Copied audit file path: {resolved_path}")

        QApplication.clipboard().setText(text_to_copy)
        # Count GG/file-path copy as audit copy as well
        try:
            from datetime import datetime, timezone

            def _inc_gg(p):
                p.audit_copy_count = int(getattr(p, "audit_copy_count", 0) or 0) + 1
                p.last_copied_at = datetime.now(timezone.utc).isoformat()
                try:
                    p.last_copied_audit_hash = audit_path.read_text(encoding="utf-8", errors="ignore")[:64]
                except Exception:
                    pass

            self._service.update_project(target.id, _inc_gg)
            _upd = self._service.get_project(target.id)
            if _upd:
                self.model.update_project_metadata(_upd)
        except Exception:
            pass

        # Auto-mark as Done when copying audit path (GG command = audit is ready)
        if not getattr(target, "ignored", False):
            self._on_toggle_ignored(target)

    def _on_ia_copy(self, _checked: bool = False):
        proj = self._selected_project()
        if not proj:
            self._flash_status("IA: no project selected", "#D66464")
            return
        p = get_active_inaudit_path(proj)
        if p is None or not validate_inaudit_path(proj, p):
            self._flash_status(f"IA: no INAUDIT layer for {proj.display_name}", "#D66464")
            return
        mods = QApplication.keyboardModifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            cmd = f'saipen gg "{p}"'
            QApplication.clipboard().setText(cmd)
            self._flash_status(f"IA GG copied: {p.name}", "#D4A840")
            return
        if mods & Qt.KeyboardModifier.ControlModifier:
            cmd = f'saipen cc "{p}"'
            QApplication.clipboard().setText(cmd)
            self._flash_status(f"IA CC copied: {p.name}", "#D4A840")
            return
        QApplication.clipboard().setText(str(p))
        self._flash_status(f"IA copied: audit\\{p.name}", "#D4A840")

    def _on_ia_copy_gg(self, proj: Project | None = None):
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
        p = get_active_inaudit_path(target)
        if p is None or not validate_inaudit_path(target, p):
            self._flash_status("IA GG: no layer", "#D66464")
            return
        QApplication.clipboard().setText(f'saipen gg "{p}"')
        self._flash_status(f"IA GG copied: {p.name}", "#D4A840")

    def _on_ia_copy_cc(self, proj: Project | None = None):
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
        p = get_active_inaudit_path(target)
        if p is None or not validate_inaudit_path(target, p):
            self._flash_status("IA CC: no layer", "#D66464")
            return
        QApplication.clipboard().setText(f'saipen cc "{p}"')
        self._flash_status(f"IA CC copied: {p.name}", "#D4A840")

    def _on_inaudit_selection_changed(self, proj: Project | None = None):
        target = proj if isinstance(proj, Project) else self._selected_project()
        if target:
            try:
                self.model.refresh_inaudit(target.id)
                self.tree.viewport().update()
            except Exception:
                pass

    def _on_copy_archive(self, proj: Optional[Any] = None):
        """Copies the .zip archive file to clipboard."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            self._flash_status("COPY ZIP: No project selected", "#D66464")
            return
        self._flash_status(f"\u25B6 COPY ZIP: {target.display_name}", "#D4A840")
        out_dir = resolve_output_dir(target.source_path, self._service.config.packing, fallback=app_dir(), group=target.priority_group, project=target)
        arc = find_archive_for_project(target, out_dir)
        if not arc or not arc.exists():
            self.statusBar().showMessage(f"COPY ZIP: No archive found for {target.display_name}. Pack first.")
            return

        # Copy file to clipboard as real file drop (CF_HDROP)
        # PySide6 setMimeData with file:// URLs sets both file:// and CF_HDROP on Windows
        try:
            from PySide6.QtCore import QMimeData
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(arc.resolve()))])
            QApplication.clipboard().setMimeData(mime)
        except Exception:
            # Fallback: plain text path
            QApplication.clipboard().setText(str(arc.resolve()))

        self._service.update_project(target.id, lambda p: setattr(p, "last_copied_archive_mtime", arc.stat().st_mtime))
        self.model.update_project_metadata(target)
        size_str = human_mb(arc.stat().st_size)
        self.statusBar().showMessage(f"✓ COPIED ARCHIVE FILE: {arc.name} ({size_str}) to clipboard")

    def _on_copy_archive_path(self, proj: Optional[Any] = None):
        """Copies absolute archive path to clipboard."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        out_dir = resolve_output_dir(target.source_path, self._service.config.packing, fallback=app_dir(), group=target.priority_group, project=target)
        arc = find_archive_for_project(target, out_dir)
        if not arc or not arc.exists():
            self.statusBar().showMessage(f"No archive found for {target.display_name}")
            return
        QApplication.clipboard().setText(str(arc.resolve()))
        self.statusBar().showMessage(f"✓ Copied archive path: {arc.resolve()}")

    def _on_copy_project_path(self, proj: Optional[Any] = None):
        """Copies project source directory path to clipboard."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        QApplication.clipboard().setText(str(target.source_path))
        self.statusBar().showMessage(f"✓ Copied project path: {target.source_path}")

    def _on_open_project_folder(self, proj: Optional[Any] = None):
        """Reveals source project in Windows Explorer."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        p = Path(target.source_path)
        if p.exists():
            os.startfile(str(p))

    def _on_open_terminal(self, proj: Optional[Any] = None):
        """Launches an interactive PowerShell terminal console at the project root."""
        import subprocess
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        target_path = str(Path(target.source_path).resolve())
        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

        subprocess.Popen([
            "powershell.exe", "-NoExit", "-Command",
            f'[Console]::Title = "{target.display_name} | PowerShell | {target_path}"; '
            f'Set-Location -LiteralPath "{target_path}"'
        ], creationflags=create_console)
        self.statusBar().showMessage(f"✓ Opened Terminal for {target.display_name}")

    def _on_open_archive_folder(self, proj: Optional[Any] = None):
        """Reveals archive folder in Windows Explorer."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        out_dir = resolve_output_dir(target.source_path, self._service.config.packing, fallback=app_dir(), group=target.priority_group, project=target)
        if out_dir.exists():
            os.startfile(str(out_dir))

    def _on_open_audit_folder(self, proj: Optional[Any] = None):
        """Reveals project audit folder in Windows Explorer."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
        root_dir = Path(self._service.config.audits.root_dir)
        target_dir = root_dir / target.priority_group.upper() / (target.audit_project_name or target.id)
        if target_dir.exists():
            os.startfile(str(target_dir))
        elif (root_dir / target.priority_group.upper()).exists():
            os.startfile(str(root_dir / target.priority_group.upper()))
        elif root_dir.exists():
            os.startfile(str(root_dir))
        else:
            self.statusBar().showMessage(f"Audit folder does not exist: {target_dir}")

    def _on_open_with_launcher(self, proj: Optional[Any] = None, launcher_id: str = ""):
        """Generic launcher dispatch — routes launcher_id to the matching handler."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return

        # Capacity is global per launcher, not per row/project. Refresh before
        # every launch so a stale painted button can never bypass the real gate.
        self._refresh_instance_snapshot()
        block_reason = self._launcher_block_reason(launcher_id)
        if block_reason:
            self.statusBar().showMessage(f"Launch blocked: {block_reason}")
            self._show_instance_manager(target)
            return

        # Auto-copy GG command to clipboard when launching agent (if toggle is on)
        if getattr(self._service.config.ui, "auto_copy_gg_on_launch", True):
            audit_path = self._audit_service.get_preferred_audit_file_path(target.id)
            if audit_path and audit_path.exists():
                resolved_path = str(audit_path.resolve())
                gg_tpl = getattr(self._service.config.ui, "gg_template", "")
                if gg_tpl and "{path}" in gg_tpl:
                    gg_text = gg_tpl.replace("{path}", resolved_path)
                else:
                    gg_text = f"/saipen gg {resolved_path}"
                QApplication.clipboard().setText(gg_text)
                self.statusBar().showMessage(f"\u2714 GG copied: {Path(resolved_path).name}")

        # Built-in dispatch map
        _map = {
            "opencode": self._on_open_with_opencode,
            "freebuff": self._on_open_with_freebuff,
            "cline": self._on_open_with_cline,
            "main_codex": lambda p: self._on_open_with_codex(p, "main_codex"),
            "main_codex2": lambda p: self._on_open_with_codex(p, "main_codex2"),
            "main_codex3_free": lambda p: self._on_open_with_codex(p, "main_codex3_free"),
        }

        handler = _map.get(launcher_id)
        if handler:
            handler(target)
            return

        # Custom launcher with command_template — execute via PowerShell
        cfg = next((lc for lc in getattr(self._service.config, "launchers", []) if lc.id == launcher_id), None)
        if cfg and cfg.command_template:
            self._launch_custom(cfg, target)
            return

        self.statusBar().showMessage(f"Unknown launcher: {launcher_id}")

    def _launch_custom(self, cfg, proj: Project):
        """Launches a custom command_template launcher in a new console."""
        import subprocess
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        target_path = str(Path(target.source_path).resolve())
        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        tpl = (cfg.command_template or "").strip()
        tpl = tpl.replace("{workdir}", target_path).replace("{path}", target_path)
        tpl = tpl.replace("{project}", target.display_name)
        process = subprocess.Popen(
            ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", tpl],
            creationflags=create_console,
        )
        self._register_agent_launch(process, cfg.id, target)
        self.statusBar().showMessage(f"✓ Launched {cfg.name} for {target.display_name}")

    def _on_open_with_launcher_index(self, idx: int):
        """Ctrl 1-6 — open selected project with ordered enabled launcher idx."""
        proj = self._selected_project()
        if not proj:
            self._flash_status("Open with: No project selected", "#D66464")
            return
        enabled = [lc for lc in getattr(self._service.config, "launchers", []) if getattr(lc, "enabled", True)]
        if idx < 0 or idx >= len(enabled):
            self.statusBar().showMessage(f"No launcher at position {idx+1}")
            return
        self._on_open_with_launcher(proj, enabled[idx].id)

    def _on_open_with_opencode(self, proj: Optional[Any] = None):
        """Launches OpenCode YOLO for this project."""
        import subprocess
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        target_path = str(Path(target.source_path).resolve())
        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        launcher_ps1 = Path(r"V:\___VAC\__K\__CUSTOMIZATION\___CONTEXTMENU\Scripts\AI_AGENT_LAUNCHER.PS1")
        if launcher_ps1.exists():
            process = subprocess.Popen([
                "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
                "-File", str(launcher_ps1), "-Agent", "OpenCode", "-WorkDir", target_path
            ], creationflags=create_console)
        else:
            process = subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f'Set-Location -LiteralPath "{target_path}"; opencode.cmd . --auto'
            ], creationflags=create_console)
        self._register_agent_launch(process, "opencode", target)
        self.statusBar().showMessage(f"✓ Launched OpenCode for {target.display_name}")

    def _on_open_with_cline(self, proj: Optional[Any] = None):
        """Launches Cline YOLO for this project."""
        import subprocess
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        target_path = str(Path(target.source_path).resolve())
        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        launcher_ps1 = Path(r"V:\___VAC\__K\__CUSTOMIZATION\___CONTEXTMENU\Scripts\AI_AGENT_LAUNCHER.PS1")
        if launcher_ps1.exists():
            process = subprocess.Popen([
                "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
                "-File", str(launcher_ps1), "-Agent", "Cline", "-WorkDir", target_path
            ], creationflags=create_console)
        else:
            process = subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f'Set-Location -LiteralPath "{target_path}"; cline.cmd --cwd "{target_path}" --auto-approve true --tui'
            ], creationflags=create_console)
        self._register_agent_launch(process, "cline", target)
        self.statusBar().showMessage(f"✓ Launched Cline for {target.display_name}")

    def _on_open_with_freebuff(self, proj: Optional[Any] = None):
        """Launches FreeBuff interactive CLI for this project in a new terminal console."""
        import subprocess
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        target_path = str(Path(target.source_path).resolve())
        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        fb_exe = Path(r"C:\Users\vac34\freebuff.exe")
        exe_str = str(fb_exe) if fb_exe.exists() else "freebuff"

        process = subprocess.Popen([
            "powershell.exe", "-NoExit", "-Command",
            f'[Console]::Title = "{target.display_name} | FreeBuff | {target_path}"; '
            f'Set-Location -LiteralPath "{target_path}"; '
            f'& "{exe_str}" --cwd "{target_path}"'
        ], creationflags=create_console)
        self._register_agent_launch(process, "freebuff", target)
        self.statusBar().showMessage(f"✓ Launched FreeBuff for {target.display_name}")

    def _on_open_with_codex(self, proj: Optional[Any] = None, account: str = "main_codex"):
        """Launches Codex for this project with specific account profile."""
        import subprocess
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        target_path = str(Path(target.source_path).resolve())
        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        if account == "main_codex2":
            script = Path(r"V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\main_codex2\Start-Codex-Account2.ps1")
        elif account == "main_codex3_free":
            script = Path(r"V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\main_codex3_free\Start-Codex-Account3-Free.ps1")
        else:
            script = Path(r"V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\main_codex\Start-Codex-Main.ps1")

        if script.exists():
            process = subprocess.Popen([
                "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
                "-Command",
                f'[Console]::Title = "{target.display_name} | Codex ({account}) | {target_path}"; '
                f'& "{script}" -WorkDir "{target_path}"'
            ], creationflags=create_console)
        elif account in {"main_codex2", "main_codex3_free"}:
            self.statusBar().showMessage(f"Codex launcher script does not exist: {script}")
            return
        else:
            process = subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f'[Console]::Title = "{target.display_name} | Codex | {target_path}"; '
                f'Set-Location -LiteralPath "{target_path}"; codex'
            ], creationflags=create_console)
        self._register_agent_launch(process, account, target)
        self.statusBar().showMessage(f"✓ Launched Codex ({account}) for {target.display_name}")

    def _on_open_audits_root(self):
        """Reveals root audits folder in Windows Explorer."""
        root_dir = Path(self._service.config.audits.root_dir)
        if root_dir.exists():
            os.startfile(str(root_dir))
        else:
            self.statusBar().showMessage(f"Audits root folder does not exist: {root_dir}")

    def _on_toggle_ignored(self, proj: Project):
        """Toggles Done (ignored) state — dims the project row."""
        new_state = not getattr(proj, "ignored", False)
        self._service.update_project(proj.id, lambda p: setattr(p, "ignored", new_state))
        # Targeted update — no full reload
        updated = self._service.get_project(proj.id)
        if updated:
            self.model.update_project_metadata(updated)
        label = "Done" if new_state else "Active"
        self.statusBar().showMessage(f"✓ {proj.display_name} marked as {label}")

    def _on_toggle_archive_ignored(self, proj: Project):
        """Toggles Ignore to archive — excludes from PACK ALL / grouped pack."""
        new_state = not getattr(proj, "ignore_archive", False)
        self._service.update_project(proj.id, lambda p: setattr(p, "ignore_archive", new_state))
        updated = self._service.get_project(proj.id)
        if updated:
            self.model.update_project_metadata(updated)
        label = "Ignore to archive: ON" if new_state else "Ignore to archive: OFF"
        self.statusBar().showMessage(f"✓ {proj.display_name} — {label}")

    def _on_reset_copy_counter(self, proj: Project):
        """Manual reset of audit copy counter — also used on fresh audit auto-reset."""
        try:
            self._service.update_project(proj.id, lambda p: setattr(p, "audit_copy_count", 0))
            updated = self._service.get_project(proj.id)
            if updated:
                self.model.update_project_metadata(updated)
            self.statusBar().showMessage(f"✓ {proj.display_name} — copy counter reset")
        except Exception as exc:
            self.statusBar().showMessage(f"Reset failed: {exc}")

    def _on_reset_project_marks(self):
        """Clear all visual/workflow marks while preserving project enablement."""
        try:
            changed = self._service.clear_project_marks()
            if changed:
                self.model.reload()
                self.tree.expandAll()
                self._flash_status(f"RESET MARKS: cleared {changed} project(s)", PALETTE["borderHighlight"])
            else:
                self._flash_status("RESET MARKS: nothing to clear", PALETTE["textSecondary"])
        except Exception as exc:
            self._flash_status(f"RESET MARKS failed: {exc}", PALETTE["dangerText"])

    def _on_toggle_project_enabled(self, proj: Project):
        """Toggles project enabled state."""
        new_state = not proj.enabled
        self._service.update_project(proj.id, lambda p: setattr(p, "enabled", new_state))
        self.model.reload()
        self.tree.expandAll()
        st = "Enabled" if new_state else "Disabled"
        self.statusBar().showMessage(f"✓ {st} project {proj.display_name}")

    def _on_pack_group(self, group: str):
        """Sequential background packing queue of projects in a group."""
        ordered_projects = []
        skipped_arch = 0
        for slot in range(1, 7):
            p = self.model.project_at(group, slot)
            if p and p.source_path and p.enabled:
                if getattr(p, "ignore_archive", False):
                    skipped_arch += 1
                    continue
                ordered_projects.append(p)

        if not ordered_projects:
            if skipped_arch:
                self.statusBar().showMessage(f"No packable projects in [{group}] — {skipped_arch} ignored to archive")
            else:
                self.statusBar().showMessage(f"No enabled projects in group {group}")
            return

        self._start_sequential_pack_queue(ordered_projects, queue_label=f"PACK [{group}]")

    def _on_refresh_single_project(self, proj: Project):
        """Refreshes audit status for a single project without full tree reload."""
        self.statusBar().showMessage(f"Refreshing audit for {proj.display_name}...")
        self.task_runner.submit_coalesced(
            f"audit:{proj.id}",
            lambda: self._audit_service.refresh_project(proj.id),
            on_success=lambda snap: (
                self.model.update_audit_snapshot(proj.id, snap),
                self.statusBar().showMessage(f"✓ Refreshed audit for {proj.display_name}"),
            ),
        )

    def _on_tree_double_clicked(self, index: QModelIndex):
        """Double click handler: open instance manager on project row, add folder on empty slot, toggle on group."""
        if not index.isValid():
            return
        node_type = self.model.data(index, self.model.ROLES["node_type"])
        if node_type == "group":
            if self.tree.isExpanded(index):
                self.tree.collapse(index)
            else:
                self.tree.expand(index)
            return

        group = self.model.data(index, self.model.ROLES["group"])
        slot = self.model.data(index, self.model.ROLES["slot"])
        proj = self.model.project_at(group, slot)
        if proj:
            self._active_project = proj
            self._show_instance_manager(proj)
        else:
            self._on_add_project(default_group=group, default_slot=slot)

    def _on_tree_context_menu(self, pos):
        """Rich Win95 context menu on right-click."""
        index = self.tree.indexAt(pos)
        if not index.isValid():
            menu = QMenu(self)
            menu.addAction("Add Project...", self._on_add_project)
            menu.addAction("Paste Audit (PASTE AUDIT)", self._on_paste_audit)
            menu.addSeparator()
            menu.addAction("Pack All Projects (PACK ALL)", self._on_pack_all)
            menu.addAction("Refresh All (REFRESH)", self._on_refresh_all)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return

        self.tree.setCurrentIndex(index)
        node_type = self.model.data(index, self.model.ROLES["node_type"])

        if node_type == "group":
            group = self.model.data(index, self.model.ROLES["group"])
            menu = QMenu(self)
            is_expanded = self.tree.isExpanded(index)
            act_exp = menu.addAction("Collapse Group" if is_expanded else "Expand Group")
            act_exp.triggered.connect(lambda: self.tree.collapse(index) if is_expanded else self.tree.expand(index))
            menu.addSeparator()

            act_add_grp = menu.addAction(f"Add Project to [{group}]...")
            act_add_grp.triggered.connect(lambda: self._on_add_project(default_group=group))

            act_pack_grp = menu.addAction(f"Pack All Projects in [{group}]")
            act_pack_grp.triggered.connect(lambda: self._on_pack_group(group))
            menu.addSeparator()

            act_aud_root = menu.addAction("Open Audits Root Folder")
            act_aud_root.triggered.connect(self._on_open_audits_root)

            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return

        group = self.model.data(index, self.model.ROLES["group"])
        slot = self.model.data(index, self.model.ROLES["slot"])
        proj = self.model.project_at(group, slot)

        menu = QMenu(self)
        if proj:
            self._active_project = proj

            hover = index.data(self.model.ROLES["hover_info"])
            act_info = menu.addAction("Info \u24D8 — Project Details")
            act_info.triggered.connect(lambda: self._show_project_info(hover or {"project": proj, "group": group, "slot": slot}))
            act_instances = menu.addAction("Manage Agent Instances...")
            act_instances.triggered.connect(lambda: self._show_instance_manager(proj))
            menu.addSeparator()

            # Actions group 0: live dispatch recovery (P0-9)
            dispatch = (hover or {}).get("dispatch") or {}
            dispatch_state = str(dispatch.get("state") or "")
            if dispatch_state and dispatch_state not in {"COMPLETE", "CANCELLED", "FAILED"}:
                cancellable = {"QUEUED", "RETRYABLE", "LEASED", "ARTIFACT_FETCHED", "ATTACHED", "BLOCKED"}
                if dispatch_state in cancellable:
                    act_cancel = menu.addAction(f"Cancel Audit ({dispatch_state})")
                    act_cancel.triggered.connect(lambda _checked=False, p=proj, d=dispatch: self._on_cancel_browser_audit(p, d))
                    menu.addSeparator()

            # Actions group 1: Packing
            act_pack = menu.addAction("Pack Project (PACK)")
            act_pack.triggered.connect(lambda: self._on_pack_specific(proj))
            act_send_audit = menu.addAction("Start Audit on Free Browser Worker")
            act_send_audit.triggered.connect(self._on_send_audit)

            act_pack_all = menu.addAction("Pack All Projects (PACK ALL)")
            act_pack_all.triggered.connect(self._on_pack_all)
            menu.addSeparator()

            # Actions group 1b: INAUDIT
            try:
                _layers = list_inaudit_layers(proj)
                _sel = get_inaudit_selected(proj)
                _p = get_active_inaudit_path(proj)
                _has_ia = _p is not None and validate_inaudit_path(proj, _p)
            except Exception:
                _layers, _sel, _has_ia = [], None, False
            ia_label = f"INAUDIT  —  {len(_layers)} layer(s)" + (f"  [{_sel}.md]" if _sel else "") if _layers else "INAUDIT — no layers"
            act_ia_hdr = menu.addAction(ia_label)
            act_ia_hdr.setEnabled(False)
            act_ia_copy = menu.addAction("  Copy IA Path" + (f"  ({_p.name})" if _has_ia else ""))
            act_ia_copy.setEnabled(_has_ia)
            act_ia_copy.triggered.connect(lambda _c=False, p=proj: self._on_ia_copy())
            act_ia_gg = menu.addAction('  Copy GG Command')
            act_ia_gg.setEnabled(_has_ia)
            act_ia_gg.triggered.connect(lambda _c=False, p=proj: self._on_ia_copy_gg(p))
            act_ia_cc = menu.addAction('  Copy CC Command')
            act_ia_cc.setEnabled(_has_ia)
            act_ia_cc.triggered.connect(lambda _c=False, p=proj: self._on_ia_copy_cc(p))
            act_ia_open = menu.addAction("  Open INAUDIT Tab")
            act_ia_open.triggered.connect(lambda: self.tabs.setCurrentWidget(self.inaudit_widget))
            menu.addSeparator()

            # Actions group 2: Clipboard
            act_copy_aud = menu.addAction("Copy Audit Text (COPY AUDIT)")
            act_copy_aud.triggered.connect(lambda: self._on_copy_audit_specific(proj))

            has_saipen = bool(proj.source_path and (Path(proj.source_path) / ".saipen").is_dir())
            copy_aud_path_label = "Copy Audit File Path (GG)" if has_saipen else "Copy Audit File Path"
            act_copy_aud_path = menu.addAction(copy_aud_path_label)
            act_copy_aud_path.triggered.connect(lambda: self._on_copy_audit_file_path(proj))

            act_copy_arc = menu.addAction("Copy Archive File (COPY ZIP)")
            act_copy_arc.triggered.connect(lambda: self._on_copy_archive(proj))

            act_copy_path = menu.addAction("Copy Archive Path")
            act_copy_path.triggered.connect(lambda: self._on_copy_archive_path(proj))

            act_copy_proj_path = menu.addAction("Copy Project Source Path")
            act_copy_proj_path.triggered.connect(lambda: self._on_copy_project_path(proj))
            menu.addSeparator()

            # Actions group 3: Explorer Folders & External Tools
            act_open_proj = menu.addAction("Open Project Folder in Explorer (Enter / Ctrl+O)")
            act_open_proj.triggered.connect(lambda: self._on_open_project_folder(proj))

            act_open_term = menu.addAction("Open Terminal Here (Ctrl+T)")
            act_open_term.triggered.connect(lambda: self._on_open_terminal(proj))

            act_open_arc = menu.addAction("Open Archive Folder in Explorer")
            act_open_arc.triggered.connect(lambda: self._on_open_archive_folder(proj))

            act_open_aud = menu.addAction("Open Audit Folder in Explorer")
            act_open_aud.triggered.connect(lambda: self._on_open_audit_folder(proj))

            menu_open_with = menu.addMenu("Open with...")
            for _lc in self._service.config.launchers:
                if _lc.enabled:
                    _act = menu_open_with.addAction(f"[{_lc.short_label}] {_lc.name}")
                    _lid = _lc.id
                    _act.triggered.connect(lambda _checked=False, _p=proj, _id=_lid: self._on_open_with_launcher(_p, _id))
            menu.addSeparator()

            # Actions group 4: Project Management & Slot Reordering
            act_folder = menu.addAction("Change Project Folder... (FOLDER)")
            act_folder.triggered.connect(lambda: self._on_change_project_folder(proj))

            act_move_up = menu.addAction("Move Up (Slot -1)")
            act_move_up.triggered.connect(lambda: self._on_move_project_step(proj, -1))

            act_move_down = menu.addAction("Move Down (Slot +1)")
            act_move_down.triggered.connect(lambda: self._on_move_project_step(proj, 1))

            menu_move_grp = menu.addMenu("Move to Group...")
            for g in self._service.active_groups():
                act_g = menu_move_grp.addAction(f"Move to [{g}]")
                act_g.triggered.connect(lambda _, grp_name=g: self._on_move_project_to_group(proj, grp_name))

            toggle_txt = "Disable Project" if proj.enabled else "Enable Project"
            act_toggle = menu.addAction(toggle_txt)
            act_toggle.triggered.connect(lambda: self._on_toggle_project_enabled(proj))

            act_arch = menu.addAction("Ignore to archive")
            act_arch.setCheckable(True)
            act_arch.setChecked(bool(getattr(proj, "ignore_archive", False)))
            act_arch.triggered.connect(lambda: self._on_toggle_archive_ignored(proj))

            act_reset_copy = menu.addAction(f"Reset copy counter (×{getattr(proj, 'audit_copy_count', 0) or 0})")
            act_reset_copy.triggered.connect(lambda: self._on_reset_copy_counter(proj))

            act_del = menu.addAction("Remove Project from List")
            act_del.triggered.connect(lambda: self._on_delete_project(proj))
            menu.addSeparator()

            # Actions group 5: Audit Refresh
            act_ref_aud = menu.addAction(f"Refresh Audit ({proj.display_name})")
            act_ref_aud.triggered.connect(lambda: self._on_refresh_single_project(proj))
        else:
            act_add = menu.addAction(f"Add Project to [{group} #{slot}]...")
            act_add.triggered.connect(lambda: self._on_add_project(default_group=group, default_slot=slot))

            act_paste = menu.addAction("Paste Audit to Ingest (PASTE AUDIT)")
            act_paste.triggered.connect(self._on_paste_audit)
            menu.addSeparator()

            act_pack_all = menu.addAction("Pack All Projects (PACK ALL)")
            act_pack_all.triggered.connect(self._on_pack_all)

            act_ref_all = menu.addAction("Refresh All Audits (REFRESH)")
            act_ref_all.triggered.connect(self._on_refresh_all)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_paste_audit(self):
        """Pastes and ingests audit text from clipboard."""
        self._flash_status("\u25B6 PASTE AUDIT: Reading clipboard...", "#D4A840")
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text or not text.strip():
            self._flash_status("PASTE AUDIT: Clipboard is empty or contains no text.", "#D66464")
            return

        project_hint = self._active_project.display_name if self._active_project else None
        res = ingest_audit_text(text, self._service.config, project_hint=project_hint)

        if res.ok:
            if res.project_name:
                p = self._service.registry.get_project_by_name(res.project_name) or (
                    self._service.get_project(res.project_name)
                )
                if p:
                    # Invalidate and refresh only this single project
                    self.task_runner.submit_coalesced(
                        f"audit:{p.id}",
                        lambda: self._audit_service.refresh_project(p.id),
                        on_success=lambda snap: self.model.update_audit_snapshot(p.id, snap),
                    )
            self.statusBar().showMessage(f"✓ Ingested Audit: {res.message}")
        else:
            self.statusBar().showMessage(f"Audit Ingest Error: {res.error}")

    def _on_bridge_status(self):
        """Switches to the Settings tab -> Bridge sub-tab."""
        self.tabs.setCurrentWidget(self.settings_widget)
        self.settings_widget.sub_tabs.setCurrentIndex(3)

    def _generation_watch_paths(self):
        paths = [str(self._generation_path.parent)]
        if self._generation_path.exists():
            paths.append(str(self._generation_path))
        current = set(self._generation_watcher.files()) | set(self._generation_watcher.directories())
        add = [path for path in paths if path not in current]
        if add:
            self._generation_watcher.addPaths(add)

    def _on_generation_fs_event(self, _path=""):
        self._generation_watch_paths()
        self._generation_debounce.start()

    def _on_check_bridge_generation(self):
        """Polls lightweight cross-process generation signal and updates affected project.

        W2-003: when the generation carries a project_id, do a targeted refresh.
        For legacy name-only generations, fall back to the registry's name lookup
        (which actually exists). Advance _last_audit_generation only after target
        resolution and refresh scheduling succeed, so a processing failure never
        burns the generation.
        """
        try:
            info = get_generation_info()
            gen = info.get("generation", 0)
            if gen > self._last_audit_generation:
                project_id = info.get("project_id")
                if not project_id and info.get("last_project"):
                    p = self._service.registry.get_project_by_name(info["last_project"])
                    if p:
                        project_id = p.id
                if project_id:
                    self.task_runner.submit_coalesced(
                        f"audit:{project_id}",
                        lambda: self._audit_service.refresh_project(project_id),
                        on_success=lambda snap: self.model.update_audit_snapshot(project_id, snap),
                    )
                    self._last_audit_generation = gen
                else:
                    self._on_refresh_all()
                    self._last_audit_generation = gen
        except Exception:
            pass

    def _on_temperature_tick(self):
        """In-memory temperature tick (0 disk reads)."""
        self.model.update_temperature_all()

    def _make_pack_progress_callback(self, project_id: str, run_id: int):
        """Builds a thread-safe progress callback for one pack run.

        Worker threads call this with raw (files_added, bytes_written,
        current_path). The buffer is drained by ``_flush_pack_progress`` on the
        GUI thread, so the Qt model is never touched off-thread.
        """
        def _cb(files_added: int, bytes_written: int, current_path: str) -> None:
            try:
                with self._pack_progress_lock:
                    prev = self._pack_progress_buffer.get(project_id)
                    # Keep only the latest snapshot per project per tick to
                    # avoid buffer blowup when a fast pack emits 100s of
                    # progress events.
                    if prev is None or int(bytes_written) >= int(prev[1]):
                        self._pack_progress_buffer[project_id] = (
                            int(files_added),
                            int(bytes_written),
                            int(run_id),
                            str(current_path or ""),
                        )
            except Exception:
                pass
        return _cb

    def _flush_pack_progress(self) -> None:
        """Drains the worker-thread progress buffer into the model on the GUI thread."""
        snapshot: list[tuple[str, int, int, int, str]] = []
        try:
            with self._pack_progress_lock:
                if not self._pack_progress_buffer:
                    return
                snapshot = [
                    (pid, fa, bw, rid, path)
                    for pid, (fa, bw, rid, path) in self._pack_progress_buffer.items()
                ]
                self._pack_progress_buffer.clear()
        except Exception:
            return
        for pid, files_added, bytes_written, run_id, current_path in snapshot:
            try:
                self.model.update_pack_progress(
                    pid, files_added, bytes_written, current_path, run_id=run_id
                )
            except Exception:
                pass

    def _on_settings(self):
        """Switches to the Settings tab."""
        self.tabs.setCurrentWidget(self.settings_widget)

    def _on_settings_saved(self):
        """Applies updates when settings are saved in the Settings tab.

        PERF-002: only refresh the audit model when a persisted audit-relevant
        field actually changed, and route that single refresh through
        submit_coalesced so rapid Settings edits cannot queue N full scans. The
        fingerprint is computed from the config that was actually persisted (not
        the in-flight widget state), so a failed save never triggers a refresh.
        """
        self.statusBar().showMessage("✓ Settings saved successfully")
        self.tree.scheduleDelayedItemsLayout()
        self.tree.viewport().update()
        try:
            from audapack.config import load_config
            persisted = load_config()
            fingerprint = self._audit_config_fingerprint(persisted)
        except Exception:
            fingerprint = None
        if fingerprint is not None and fingerprint == getattr(self, "_last_audit_fingerprint", None):
            return
        self._last_audit_fingerprint = fingerprint
        self.task_runner.submit_coalesced(
            "audit:refresh_all",
            self._audit_service.refresh_all,
            on_success=lambda snaps: [
                self.model.update_audit_snapshot(pid, s) for pid, s in snaps.items()
            ],
        )

    @staticmethod
    def _audit_config_fingerprint(cfg) -> str:
        """PERF-002: stable hash of only the fields that change audit scanning."""
        import hashlib
        fields = [
            cfg.audits.root,
            cfg.audits.hot_seconds,
            cfg.audits.warm_seconds,
            cfg.audits.cool_seconds,
            cfg.audits.cold_seconds,
            cfg.audits.stale_seconds,
            cfg.packing.output_dir,
            cfg.packing.output_layout,
        ]
        payload = "|".join("" if f is None else str(f) for f in fields)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _show_project_info(self, hover_info: dict, anchor_pos: QPoint | None = None) -> None:
        """Popup near ⓘ button — replaces hover uncertainty with deterministic click."""
        try:
            html = ProjectItemDelegate.build_tooltip(hover_info or {})
        except Exception:
            html = "<b>Info unavailable</b>"
        # Anchor near the ⓘ button or cursor, offset so cursor doesn't cover it
        if anchor_pos is not None:
            try:
                global_pos = self.tree.viewport().mapToGlobal(anchor_pos) + QPoint(20, 12)
            except Exception:
                global_pos = self.cursor().pos() + QPoint(20, 12)
        else:
            global_pos = self.cursor().pos() + QPoint(20, 12)
        # Golden tooltip styling already applied globally via QToolTip.setStyleSheet
        # Use QToolTip popup — lightweight, no separate window, click elsewhere dismisses
        tooltip_duration = 15000
        try:
            tooltip_duration = int(getattr(self._service.config.ui, "tooltip_duration_ms", 15000))
        except Exception:
            pass
        QToolTip.showText(global_pos, html, self.tree, QRect(), tooltip_duration)

    def _on_refresh_all(self):
        """Explicit Refresh All action."""
        self._flash_status("\u25B6 REFRESH: Scanning all audits...", "#D4A840")
        self.task_runner.submit_coalesced(
            "audit:refresh_all",
            self._audit_service.refresh_all,
            on_success=lambda snaps: (
                [self.model.update_audit_snapshot(pid, s) for pid, s in snaps.items()],
                self.statusBar().showMessage("✓ Audits refreshed"),
            ),
        )

    def refresh(self):
        """Full model reload (used only when structural reload is explicitly requested)."""
        self.model.reload()
        self.tree.expandAll()
