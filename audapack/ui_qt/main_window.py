"""Qt MainWindow (Wave M) — Golden Default chrome, responsive tree, async I/O, DnD."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Union

from PySide6.QtCore import QModelIndex, QPoint, QRect, Qt, QTimer, QUrl
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
    QTabWidget,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from audapack.bridge.state import get_generation_info
from audapack.config import app_dir
from audapack.ingest import ingest_audit_text
from audapack.models import Project
from audapack.packing import find_archive_for_project, human_mb, resolve_output_dir
from audapack.services.audit_service import AuditService
from audapack.services.bridge_service import BridgeService
from audapack.services.packing_service import PackingService
from audapack.services.project_service import ProjectService
from audapack.ui.clipboard_files import copy_file_to_clipboard
from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget
from audapack.ui_qt.models.project_delegate import ProjectItemDelegate
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

    def mousePressEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        idx = self.indexAt(pos)
        if idx.isValid() and idx.internalId() != 0:
            rect = self.visualRect(idx)
            btn_rect = QRect(rect.right() - 36, rect.top() + 3, 32, 20)
            if btn_rect.contains(pos):
                group = idx.data(self.model().ROLES["group"])
                slot = idx.data(self.model().ROLES["slot"])
                proj = self.model().project_at(group, slot)
                if proj:
                    main_win = self.window()
                    if hasattr(main_win, "_on_copy_audit_file_path"):
                        main_win._on_copy_audit_file_path(proj)
                    event.accept()
                    return
        super().mousePressEvent(event)

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

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        if not indexes:
            idx = self.currentIndex()
            if idx.isValid():
                indexes = [idx]
        valid_indexes = [i for i in indexes if i.isValid() and i.internalId() != 0]
        if not valid_indexes:
            return
        mime_data = self.model().mimeData(valid_indexes)
        if not mime_data:
            return

        drag = QDrag(self)
        drag.setMimeData(mime_data)

        # Generate a clean Win95 drag preview badge
        proj_name = mime_data.text() or "Project"
        pixmap = QPixmap(180, 24)
        pixmap.fill(QColor(PALETTE["surfaceRaised"]))
        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor(PALETTE["borderGolden"]), 1))
        painter.drawRect(0, 0, 179, 23)
        painter.setFont(QFont("Verdana", 9, QFont.Weight.Bold))
        painter.setPen(QColor(PALETTE["borderGolden"]))
        painter.drawText(8, 16, f"⇄ {proj_name}")
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(10, 12))

        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)

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

        self._move_generation: int = 0
        self._last_audit_generation: int = 0

        # Background Task Runner for async I/O
        self.task_runner = TaskRunner(max_threads=4, parent=self)

        self.setWindowTitle("AUDAPACK — Project Room")
        self.resize(*service.config.ui.window_size)
        self.setMinimumSize(280, 200)

        # Set main orange app icon
        icon_path = app_dir() / "resources" / "app_icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Presentation model
        self.model = ProjectRoomModel(service, audit_service=self._audit_service, parent=self)
        self.model.project_dropped.connect(self._on_project_dropped)

        # Top Toolbar
        toolbar = QToolBar("Actions", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        act_add = toolbar.addAction("ADD", self._on_add_project)
        act_add.setToolTip("Add new project folder directly to room")

        act_folder = toolbar.addAction("FOLDER", self._on_change_project_folder)
        act_folder.setToolTip("Change source folder for selected project")

        act_pack = toolbar.addAction("PACK", self._on_pack)
        act_pack.setToolTip("Pack selected project in background")

        act_pack_all = toolbar.addAction("PACK ALL", self._on_pack_all)
        act_pack_all.setToolTip("Pack all configured projects in background")

        act_copy = toolbar.addAction("COPY AUDIT", self._on_copy_audit)
        act_copy.setToolTip("Copy verified audit to clipboard")

        act_copy_gg = toolbar.addAction("COPY GG", self._on_copy_audit_file_path)
        act_copy_gg.setToolTip("Copy /saipen gg audit path to clipboard (Ctrl+C)")

        act_copy_arc = toolbar.addAction("COPY ZIP", self._on_copy_archive)
        act_copy_arc.setToolTip("Copy packed .zip archive file to clipboard")

        act_paste = toolbar.addAction("PASTE AUDIT", self._on_paste_audit)
        act_paste.setToolTip("Paste and ingest audit markdown from clipboard (Ctrl+V)")

        toolbar.addAction("REFRESH", self._on_refresh_all)

        # Central Tabs: [Project Room] [Settings]
        self.tabs = QTabWidget(self)

        # Tab 0: Project Room
        projects_tab = QWidget(self)
        p_layout = QVBoxLayout(projects_tab)
        p_layout.setContentsMargins(4, 4, 4, 4)

        self.tree = ProjectTreeView(projects_tab)
        self.tree.setModel(self.model)
        self.delegate = ProjectItemDelegate(self.tree)
        self.tree.setItemDelegate(self.delegate)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)

        # Context Menu & Double Click
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.doubleClicked.connect(self._on_tree_double_clicked)

        self.tree.expandAll()
        self.tree.selectionModel().currentChanged.connect(self._on_current_changed)
        self.tree.clicked.connect(self._on_tree_clicked)
        p_layout.addWidget(self.tree)

        # Initial selection: auto-select first registered project
        self._auto_select_first_project()

        # Tab 1: Settings
        self.settings_widget = SettingsWidget(service.config, self, on_saved=self._on_settings_saved)

        self.tabs.addTab(projects_tab, "Project Room")
        self.tabs.addTab(self.settings_widget, "Settings")
        self.tabs.setCurrentIndex(0)

        self.setCentralWidget(self.tabs)
        self.setStyleSheet(GoldenDefault.qss())
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

        self.refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        self.refresh_shortcut.activated.connect(self._on_refresh_all)

        self.folder_shortcut = QShortcut(QKeySequence("F2"), self)
        self.folder_shortcut.activated.connect(self._on_change_project_folder)

        # Temperature Timer: 60s in-memory recalculation (0 disk reads)
        self.temp_timer = QTimer(self)
        self.temp_timer.setInterval(60000)
        self.temp_timer.timeout.connect(self._on_temperature_tick)
        self.temp_timer.start()

        # Bridge Generation Poller: 1500ms lightweight generation check
        self.bridge_timer = QTimer(self)
        self.bridge_timer.setInterval(1500)
        self.bridge_timer.timeout.connect(self._on_check_bridge_generation)
        self.bridge_timer.start()

        # Async initial enrichment (time-to-interactive optimization)
        QTimer.singleShot(50, self._async_initial_enrichment)

    # ---------------------------------------------------------------- Startup Flow

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

    def _on_tree_clicked(self, index: QModelIndex):
        if not index.isValid():
            return
        node_type = self.model.data(index, self.model.ROLES["node_type"])
        if node_type == "slot":
            group = self.model.data(index, self.model.ROLES["group"])
            slot = self.model.data(index, self.model.ROLES["slot"])
            self._active_project = self.model.project_at(group, slot)

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex):
        if not current.isValid():
            self._active_project = None
            return
        node_type = self.model.data(current, self.model.ROLES["node_type"])
        if node_type != "slot":
            self._active_project = None
            return
        group = self.model.data(current, self.model.ROLES["group"])
        slot = self.model.data(current, self.model.ROLES["slot"])
        self._active_project = self.model.project_at(group, slot)

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
            last_copied_archive_mtime=proj.last_copied_archive_mtime,
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
                last_copied_archive_mtime=swap_proj.last_copied_archive_mtime,
            )

        self.model.apply_project_move(
            src_group,
            src_slot,
            target_group,
            target_slot,
            optimistic_proj,
            swapped_project=optimistic_swap,
        )

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
                self.statusBar().showMessage(f"Move failed: reverting {proj.display_name}")
            elif res.ok and gen == self._move_generation:
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
        cur_dir = str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Select Project Folder", cur_dir)
        if selected:
            self._add_project_from_path(selected, default_group=default_group, default_slot=default_slot)

    def _on_change_project_folder(self, proj: Optional[Any] = None):
        """Directly prompts for a new folder (0 popup dialogs) and updates the project."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target:
            return
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

    def _on_pack(self):
        """Async background packing — GUI thread remains 100% interactive."""
        proj = self._selected_project()
        if not proj:
            return

        if self.task_runner.is_running(f"pack:{proj.id}"):
            self.statusBar().showMessage(f"Packing already in progress for {proj.display_name}")
            return

        self.model.update_pack_state(proj.id, "PACKING")
        self.statusBar().showMessage(f"Packing {proj.display_name} in background...")

        def _do_pack():
            return self._packing.pack_project(proj.id)

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
        total_count = len(projects)
        for p in projects:
            self.model.update_pack_state(p.id, "QUEUED")

        queue = list(projects)
        packed_count = [0]

        def _step_next():
            if not queue:
                self.statusBar().showMessage(f"✓ {queue_label} COMPLETE: Packed {packed_count[0]}/{total_count} projects")
                return

            current_proj = queue.pop(0)
            p_id = current_proj.id
            p_name = current_proj.display_name
            curr_num = total_count - len(queue)

            self.model.update_pack_state(p_id, "PACKING")
            self.statusBar().showMessage(f"{queue_label} ({curr_num}/{total_count}): Packing {p_name}...")

            def _do_pack():
                return self._packing.pack_project(p_id)

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

    def _on_pack_all(self):
        """Sequential background packing queue of all projects from top to bottom."""
        ordered_projects = []
        for group in self.model._groups:
            for slot in range(1, 7):
                p = self.model.project_at(group, slot)
                if p and p.source_path and p.enabled and not p.ignored:
                    ordered_projects.append(p)

        if not ordered_projects:
            self.statusBar().showMessage("PACK ALL: No active projects configured.")
            return

        self._start_sequential_pack_queue(ordered_projects, queue_label="PACK ALL")

    def _on_pack_specific(self, proj: Project):
        if not proj:
            return
        if self.task_runner.is_running(f"pack:{proj.id}"):
            self.statusBar().showMessage(f"Packing already in progress for {proj.display_name}")
            return

        self.model.update_pack_state(proj.id, "PACKING")
        self.statusBar().showMessage(f"Packing {proj.display_name} in background...")

        def _do_pack():
            return self._packing.pack_project(proj.id)

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
        if proj:
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
        self._service.update_project(proj.id, lambda p: setattr(p, "last_copied_audit_hash", sha256))
        self.model.update_project_metadata(proj)
        self.statusBar().showMessage(f"✓ COPIED AUDIT for {proj.display_name} ({len(content)} chars)")

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
            text_to_copy = f"/saipen gg {resolved_path}"
            self.statusBar().showMessage(f"✓ Copied SAIPEN command: /saipen gg {audit_path.name}")
        else:
            text_to_copy = resolved_path
            self.statusBar().showMessage(f"✓ Copied audit file path: {resolved_path}")

        QApplication.clipboard().setText(text_to_copy)

    def _on_copy_archive(self, proj: Optional[Any] = None):
        """Copies the .zip archive file to clipboard."""
        target = proj if isinstance(proj, Project) else self._selected_project()
        if not target or not target.source_path:
            return
        out_dir = resolve_output_dir(target.source_path, self._service.config.packing, fallback=app_dir(), group=target.priority_group, project=target)
        arc = find_archive_for_project(target, out_dir)
        if not arc or not arc.exists():
            self.statusBar().showMessage(f"COPY ZIP: No archive found for {target.display_name}. Pack first.")
            return

        # Native clipboard file drop + PySide mime
        copy_file_to_clipboard([arc])
        try:
            from PySide6.QtCore import QMimeData
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(arc.resolve()))])
            QApplication.clipboard().setMimeData(mime)
        except Exception:
            pass

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
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
                "-File", str(launcher_ps1), "-Agent", "OpenCode", "-WorkDir", target_path
            ], creationflags=create_console)
        else:
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f'Set-Location -LiteralPath "{target_path}"; opencode.cmd . --auto'
            ], creationflags=create_console)
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
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
                "-File", str(launcher_ps1), "-Agent", "Cline", "-WorkDir", target_path
            ], creationflags=create_console)
        else:
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f'Set-Location -LiteralPath "{target_path}"; cline.cmd --cwd "{target_path}" --auto-approve true --tui'
            ], creationflags=create_console)
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

        subprocess.Popen([
            "powershell.exe", "-NoExit", "-Command",
            f'[Console]::Title = "{target.display_name} | FreeBuff | {target_path}"; '
            f'Set-Location -LiteralPath "{target_path}"; '
            f'& "{exe_str}" --cwd "{target_path}"'
        ], creationflags=create_console)
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
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
                "-Command",
                f'[Console]::Title = "{target.display_name} | Codex ({account}) | {target_path}"; '
                f'& "{script}" -WorkDir "{target_path}"'
            ], creationflags=create_console)
        else:
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f'[Console]::Title = "{target.display_name} | Codex | {target_path}"; '
                f'Set-Location -LiteralPath "{target_path}"; codex'
            ], creationflags=create_console)
        self.statusBar().showMessage(f"✓ Launched Codex ({account}) for {target.display_name}")

    def _on_open_audits_root(self):
        """Reveals root audits folder in Windows Explorer."""
        root_dir = Path(self._service.config.audits.root_dir)
        if root_dir.exists():
            os.startfile(str(root_dir))
        else:
            self.statusBar().showMessage(f"Audits root folder does not exist: {root_dir}")

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
        for slot in range(1, 7):
            p = self.model.project_at(group, slot)
            if p and p.source_path and p.enabled and not p.ignored:
                ordered_projects.append(p)

        if not ordered_projects:
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
        """Double click handler: open folder on project row, add folder on empty slot, toggle on group."""
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
            self._on_open_project_folder(proj)
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

            # Actions group 1: Packing
            act_pack = menu.addAction("Pack Project (PACK)")
            act_pack.triggered.connect(lambda: self._on_pack_specific(proj))

            act_pack_all = menu.addAction("Pack All Projects (PACK ALL)")
            act_pack_all.triggered.connect(self._on_pack_all)
            menu.addSeparator()

            # Actions group 2: Clipboard
            act_copy_aud = menu.addAction("Copy Audit Text (COPY AUDIT)")
            act_copy_aud.triggered.connect(lambda: self._on_copy_audit_specific(proj))

            has_saipen = bool(proj.source_path and (Path(proj.source_path) / ".saipen").is_dir())
            copy_aud_path_label = "Copy Audit File Path (/saipen gg ...)" if has_saipen else "Copy Audit File Path"
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
            act_opencode = menu_open_with.addAction("OpenCode YOLO")
            act_opencode.triggered.connect(lambda: self._on_open_with_opencode(proj))

            act_cline = menu_open_with.addAction("Cline YOLO")
            act_cline.triggered.connect(lambda: self._on_open_with_cline(proj))

            act_freebuff = menu_open_with.addAction("FreeBuff")
            act_freebuff.triggered.connect(lambda: self._on_open_with_freebuff(proj))

            menu_codex = menu_open_with.addMenu("Codex")
            act_codex1 = menu_codex.addAction("main_codex")
            act_codex1.triggered.connect(lambda: self._on_open_with_codex(proj, "main_codex"))

            act_codex2 = menu_codex.addAction("main_codex2")
            act_codex2.triggered.connect(lambda: self._on_open_with_codex(proj, "main_codex2"))

            act_codex3 = menu_codex.addAction("main_codex3_free")
            act_codex3.triggered.connect(lambda: self._on_open_with_codex(proj, "main_codex3_free"))
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
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text or not text.strip():
            self.statusBar().showMessage("Paste Audit: Clipboard is empty or contains no text.")
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

    def _on_check_bridge_generation(self):
        """Polls lightweight cross-process generation signal and updates affected project."""
        try:
            info = get_generation_info()
            gen = info.get("generation", 0)
            if gen > self._last_audit_generation:
                self._last_audit_generation = gen
                project_id = info.get("project_id")
                if not project_id and info.get("last_project"):
                    p = self._service.find_by_name(info["last_project"])
                    if p:
                        project_id = p.id
                if project_id:
                    # Targeted refresh for single affected project only! Zero model reset!
                    self.task_runner.submit_coalesced(
                        f"audit:{project_id}",
                        lambda: self._audit_service.refresh_project(project_id),
                        on_success=lambda snap: self.model.update_audit_snapshot(project_id, snap),
                    )
                else:
                    # Fallback to full refresh
                    self._on_refresh_all()
        except Exception:
            pass

    def _on_temperature_tick(self):
        """In-memory temperature tick (0 disk reads)."""
        self.model.update_temperature_all()

    def _on_settings(self):
        """Switches to the Settings tab."""
        self.tabs.setCurrentWidget(self.settings_widget)

    def _on_settings_saved(self):
        """Applies updates when settings are saved in the Settings tab."""
        self.statusBar().showMessage("✓ Settings saved successfully")
        self.task_runner.submit(
            "audit:refresh_all",
            self._audit_service.refresh_all,
            on_success=lambda snaps: [
                self.model.update_audit_snapshot(pid, s) for pid, s in snaps.items()
            ],
        )

    def _on_refresh_all(self):
        """Explicit Refresh All action."""
        self.statusBar().showMessage("Refreshing audits...")
        self.task_runner.submit(
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
