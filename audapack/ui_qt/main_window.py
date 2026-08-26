"""Qt MainWindow (Wave M) — Golden Default chrome, responsive tree, async I/O, DnD."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QMainWindow,
    QMessageBox,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from audapack.bridge.state import get_generation_info
from audapack.ingest import ingest_audit_text
from audapack.models import Project
from audapack.services.audit_service import AuditService
from audapack.services.bridge_service import BridgeService
from audapack.services.packing_service import PackingService
from audapack.services.project_service import ProjectService
from audapack.ui_qt.dialogs.settings_dialog import SettingsDialog
from audapack.ui_qt.models.project_delegate import ProjectItemDelegate
from audapack.ui_qt.models.project_room_model import ProjectRoomModel
from audapack.ui_qt.task_runner import TaskRunner
from audapack.ui_qt.theme.golden_default import GoldenDefault

logger = logging.getLogger(__name__)


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

        # Presentation model
        self.model = ProjectRoomModel(service, audit_service=self._audit_service, parent=self)
        self.model.project_dropped.connect(self._on_project_dropped)

        # Top Toolbar
        toolbar = QToolBar("Actions", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        act_pack = toolbar.addAction("PACK", self._on_pack)
        act_pack.setToolTip("Pack selected project in background")

        act_copy = toolbar.addAction("COPY AUDIT", self._on_copy_audit)
        act_copy.setToolTip("Copy verified ALL_3 audit to clipboard")

        act_paste = toolbar.addAction("PASTE AUDIT", self._on_paste_audit)
        act_paste.setToolTip("Paste and ingest audit markdown from clipboard (Ctrl+V)")

        toolbar.addAction("REFRESH", self._on_refresh_all)
        toolbar.addAction("Bridge", self._on_bridge_status)
        toolbar.addAction("Settings", self._on_settings)

        # Central View
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.delegate = ProjectItemDelegate(self.tree)
        self.tree.setItemDelegate(self.delegate)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)

        # Enable Model-Native Drag & Drop
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)

        self.tree.expandAll()
        self.tree.selectionModel().currentChanged.connect(self._on_current_changed)
        layout.addWidget(self.tree)

        self.setCentralWidget(central)
        self.setStyleSheet(GoldenDefault.qss())
        self.statusBar().showMessage("AUDAPACK Ready")

        # Shortcuts
        self.paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.paste_shortcut.activated.connect(self._on_paste_audit)

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
        self._on_bridge_status()

    # ---------------------------------------------------------------- Selection

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
        if self._active_project is None:
            QMessageBox.information(self, "AUDAPACK", "Select a project row first.")
            return None
        return self._active_project

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

    def _on_copy_audit(self):
        """Copies verified ALL_3 audit to clipboard and updates row without model reset."""
        proj = self._selected_project()
        if not proj:
            return

        ok, content, sha256 = self._audit_service.copy_all3(proj.id)
        if not ok or not content:
            self.statusBar().showMessage(f"COPY AUDIT: No complete ALL_3 for {proj.display_name}")
            return

        QApplication.clipboard().setText(content)
        self._service.update_project(proj.id, lambda p: setattr(p, "last_copied_audit_hash", sha256))
        self.model.update_project_metadata(proj)
        self.statusBar().showMessage(f"✓ COPIED ALL_3 for {proj.display_name} ({len(content)} chars)")

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
        """Checks Bridge service status asynchronously."""
        def _check():
            return self._bridge.status()

        def _on_done(st):
            healthy = st.get("healthy", False)
            autostart = st.get("autostart", {}).get("status_text", "?")
            self.statusBar().showMessage(
                f"Bridge: {'CONNECTED' if healthy else 'OFFLINE'} · Autostart: {autostart}"
            )

        self.task_runner.submit("bridge:status", _check, on_success=_on_done)

    def _on_check_bridge_generation(self):
        """Polls lightweight cross-process generation signal and updates affected project."""
        try:
            info = get_generation_info()
            gen = info.get("generation", 0)
            if gen > self._last_audit_generation:
                self._last_audit_generation = gen
                project_id = info.get("project_id")
                if project_id:
                    # Targeted refresh for single affected project only! Zero model reset!
                    self.task_runner.submit_coalesced(
                        f"audit:{project_id}",
                        lambda: self._audit_service.refresh_project(project_id),
                        on_success=lambda snap: self.model.update_audit_snapshot(project_id, snap),
                    )
                else:
                    # Legacy fallback
                    self._on_refresh_all()
        except Exception:
            pass

    def _on_temperature_tick(self):
        """In-memory temperature tick (0 disk reads)."""
        self.model.update_temperature_all()

    def _on_settings(self):
        """Opens settings dialog and applies granular invalidation."""
        dlg = SettingsDialog(self._service.config, self)
        if dlg.exec():
            # If audit root or registry changed, refresh snapshots asynchronously
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
