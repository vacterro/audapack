"""Qt model for the project room (Wave M).

Hierarchy: ROOT -> GROUP (MAIN0/SIDE1/...) -> PROJECT_SLOT (1..SLOTS_PER_GROUP).
Presentation-only in-memory model:
- Zero filesystem / disk / Bridge / Git calls in data(), rowCount(), flags(), mimeData(), canDropMimeData().
- Model-native Drag & Drop with MIME 'application/x-audapack-project'.
- Targeted mutation API (apply_project_move, update_audit_snapshot, update_pack_state, update_temperature_all).
- Structural group insertion (beginInsertRows) without full model resets.
- Performance counters: model_reset_count, full_refresh_count, targeted_project_update_count.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from PySide6.QtCore import (
    QAbstractItemModel,
    QByteArray,
    QMimeData,
    QModelIndex,
    QObject,
    Qt,
    Signal,
)

from audapack.audits import calculate_temperature
from audapack.models import AuditSnapshot, AuditTemperature, Project
from audapack.services.audit_service import AuditService
from audapack.services.project_service import ProjectService

MIME_TYPE_PROJECT = "application/x-audapack-project"


class ProjectRoomModel(QAbstractItemModel):
    # Signal emitted when a project is dropped onto a slot (optimistic drop flow)
    project_dropped = Signal(str, str, int, str, int)  # project_id, target_grp, target_slot, src_grp, src_slot

    ROLES = {
        "project_id": Qt.ItemDataRole.UserRole + 1,
        "display_name": Qt.ItemDataRole.UserRole + 2,
        "group": Qt.ItemDataRole.UserRole + 3,
        "slot": Qt.ItemDataRole.UserRole + 4,
        "enabled": Qt.ItemDataRole.UserRole + 5,
        "is_empty_slot": Qt.ItemDataRole.UserRole + 6,
        "node_type": Qt.ItemDataRole.UserRole + 7,  # "root" | "group" | "slot"
        "audit_temperature": Qt.ItemDataRole.UserRole + 8,
        "audit_age_seconds": Qt.ItemDataRole.UserRole + 9,
        "all_ready": Qt.ItemDataRole.UserRole + 10,
        "pack_state": Qt.ItemDataRole.UserRole + 11,
        "pack_message": Qt.ItemDataRole.UserRole + 12,
        "completed_waves": Qt.ItemDataRole.UserRole + 13,
        "source_path": Qt.ItemDataRole.UserRole + 14,
    }

    def __init__(self, service: ProjectService, audit_service: Optional[AuditService] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._service = service
        self._audit_service = audit_service or AuditService(service.config, base_dir=service.base_dir)
        self._groups: list[str] = []
        self._projects: dict[tuple[str, int], Project] = {}  # (group, slot) -> Project
        self._snapshots: dict[str, AuditSnapshot] = {}  # project_id -> AuditSnapshot
        self._pack_states: dict[str, tuple[str, str]] = {}  # project_id -> (state, message)

        # Performance counters for diagnostics & tests
        self.model_reset_count: int = 0
        self.full_refresh_count: int = 0
        self.targeted_project_update_count: int = 0

        self._reload(initial=True)

    def _reload(self, initial: bool = False):
        """Full model reload (used ONLY on startup, explicit Refresh All, or major settings change)."""
        self.model_reset_count += 1
        if not initial:
            self.full_refresh_count += 1

        self.beginResetModel()
        self._groups = self._service.active_groups()
        self._projects = {}
        for p in self._service.list_projects():
            g = p.priority_group.upper()
            self._projects[(g, p.slot)] = p

        # Pre-load snapshots into memory
        self._snapshots = {}
        for p in self._service.list_projects():
            try:
                snap = self._audit_service.get_snapshot(p.id)
                if snap:
                    self._snapshots[p.id] = snap
            except Exception:
                pass
        self.endResetModel()

    def reload(self):
        self._reload(initial=False)

    # ---------------------------------------------------------------- Targeted Mutation API

    def index_for_slot(self, group: str, slot: int) -> QModelIndex:
        """Returns QModelIndex for a specific (group, slot)."""
        g_upper = group.upper()
        if g_upper not in self._groups:
            return QModelIndex()
        g_row = self._groups.index(g_upper)
        g_idx = self.createIndex(g_row, 0, 0)
        s_row = slot - 1
        return self.index(s_row, 0, g_idx)

    def index_for_project_id(self, project_id: str) -> QModelIndex:
        """Finds QModelIndex for a project by ID."""
        for (g, s), p in self._projects.items():
            if p and p.id == project_id:
                return self.index_for_slot(g, s)
        return QModelIndex()

    def apply_project_move(
        self,
        src_group: str,
        src_slot: int,
        tgt_group: str,
        tgt_slot: int,
        project: Project,
        swapped_project: Optional[Project] = None,
    ):
        """Applies a project move/swap in-memory and emits targeted dataChanged signals. ZERO model reset."""
        self.targeted_project_update_count += 1
        src_g = src_group.upper()
        tgt_g = tgt_group.upper()

        # Ensure target group exists in model
        self.ensure_group_exists(tgt_g)

        # Update in-memory map
        if swapped_project:
            self._projects[(src_g, src_slot)] = swapped_project
        else:
            self._projects.pop((src_g, src_slot), None)

        self._projects[(tgt_g, tgt_slot)] = project

        # Targeted signal emissions
        src_idx = self.index_for_slot(src_g, src_slot)
        tgt_idx = self.index_for_slot(tgt_g, tgt_slot)

        if src_idx.isValid():
            self.dataChanged.emit(src_idx, src_idx)
        if tgt_idx.isValid():
            self.dataChanged.emit(tgt_idx, tgt_idx)

    def update_project_metadata(self, project: Project):
        """Updates in-memory project data and emits single-row dataChanged."""
        self.targeted_project_update_count += 1
        g = project.priority_group.upper()
        s = project.slot
        self._projects[(g, s)] = project
        idx = self.index_for_slot(g, s)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def update_audit_snapshot(self, project_id: str, snapshot: Optional[AuditSnapshot]):
        """Updates pre-loaded snapshot for a single project and notifies the view."""
        self.targeted_project_update_count += 1
        if snapshot:
            self._snapshots[project_id] = snapshot
        else:
            self._snapshots.pop(project_id, None)

        idx = self.index_for_project_id(project_id)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def update_pack_state(self, project_id: str, state: str, message: str = ""):
        """Updates packing progress/completion status for a project row."""
        self.targeted_project_update_count += 1
        self._pack_states[project_id] = (state, message)
        idx = self.index_for_project_id(project_id)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def update_temperature_all(self, now: Optional[datetime] = None):
        """Calculates temperature & age from stored timestamps in-memory with 0 disk reads."""
        current_time = now or datetime.now()
        cfg = self._service.config.audits
        changed_indexes: list[QModelIndex] = []

        for (g, s), proj in self._projects.items():
            if not proj:
                continue
            snap = self._snapshots.get(proj.id)
            if not snap or not snap.audit_timestamp:
                continue
            new_age = max(0.0, (current_time - snap.audit_timestamp).total_seconds())
            new_temp = (
                calculate_temperature(new_age, cfg)
                if (snap.completed_waves > 0 or snap.all3_ready)
                else AuditTemperature.NONE
            )
            if new_temp != snap.temperature or int(new_age // 60) != int((snap.audit_age_seconds or 0) // 60):
                # Update snapshot in memory
                updated_snap = AuditSnapshot(
                    project_id=snap.project_id,
                    project_name=snap.project_name,
                    core_path=snap.core_path,
                    core_complete=snap.core_complete,
                    second_path=snap.second_path,
                    second_complete=snap.second_complete,
                    performance_path=snap.performance_path,
                    performance_complete=snap.performance_complete,
                    all3_path=snap.all3_path,
                    all3_ready=snap.all3_ready,
                    all3_sha256=snap.all3_sha256,
                    audit_timestamp=snap.audit_timestamp,
                    audit_age_seconds=new_age,
                    temperature=new_temp,
                    completed_waves=snap.completed_waves,
                    total_tickets=snap.total_tickets,
                )
                self._snapshots[proj.id] = updated_snap
                idx = self.index_for_slot(g, s)
                if idx.isValid():
                    changed_indexes.append(idx)

        for idx in changed_indexes:
            self.dataChanged.emit(idx, idx)

    def ensure_group_exists(self, group: str) -> bool:
        """Dynamically inserts a new SIDE group into model hierarchy with beginInsertRows."""
        g_upper = group.upper()
        if g_upper in self._groups:
            return False
        pos = len(self._groups)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._groups.append(g_upper)
        self.endInsertRows()
        return True

    # ---------------------------------------------------------------- Qt Item Model API

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, 0)  # 0 = group row
        # slot row: internalId stores (parent_group_row + 1)
        return self.createIndex(row, column, parent.row() + 1)

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        pid = index.internalId()
        if pid == 0:
            return QModelIndex()
        parent_row = int(pid) - 1
        if 0 <= parent_row < len(self._groups):
            return self.createIndex(parent_row, 0, 0)
        return QModelIndex()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._groups)
        if parent.internalId() == 0:
            return 6  # SLOTS_PER_GROUP (1..6)
        return 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        if index.internalId() == 0:
            # Group row
            if index.row() >= len(self._groups):
                return None
            group = self._groups[index.row()]
            if role in (Qt.ItemDataRole.DisplayRole, self.ROLES["display_name"]):
                return group
            if role == self.ROLES["node_type"]:
                return "group"
            if role == self.ROLES["group"]:
                return group
            return None

        # Slot row
        group_idx = int(index.internalId()) - 1
        if group_idx >= len(self._groups):
            return None
        group = self._groups[group_idx]
        slot = index.row() + 1
        proj = self._projects.get((group, slot))

        if role == self.ROLES["node_type"]:
            return "slot"
        if role == self.ROLES["group"]:
            return group
        if role == self.ROLES["slot"]:
            return slot
        if role == self.ROLES["is_empty_slot"]:
            return proj is None

        if proj is None:
            if role == Qt.ItemDataRole.DisplayRole:
                return f"Slot {slot}"
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            return proj.display_name
        if role == self.ROLES["project_id"]:
            return proj.id
        if role == self.ROLES["display_name"]:
            return proj.display_name
        if role == self.ROLES["source_path"]:
            return str(proj.source_path)
        if role == self.ROLES["enabled"]:
            return bool(proj.enabled)

        # Audit roles (fast in-memory read)
        snap = self._snapshots.get(proj.id)
        if role == self.ROLES["audit_temperature"]:
            return getattr(snap, "temperature", AuditTemperature.NONE)
        if role == self.ROLES["audit_age_seconds"]:
            return getattr(snap, "audit_age_seconds", None)
        if role == self.ROLES["all_ready"]:
            return bool(getattr(snap, "all3_ready", getattr(snap, "all_ready", False)))
        if role == self.ROLES["completed_waves"]:
            return getattr(snap, "completed_waves", 0)

        # Pack state role
        if role == self.ROLES["pack_state"]:
            st, _msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return st
        if role == self.ROLES["pack_message"]:
            _st, msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return msg

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        if index.internalId() == 0:
            # Group row: not draggable, can accept drop to assign to first available slot
            return base | Qt.ItemFlag.ItemIsDropEnabled

        # Slot row
        group_idx = int(index.internalId()) - 1
        if group_idx < len(self._groups):
            group = self._groups[group_idx]
            slot = index.row() + 1
            proj = self._projects.get((group, slot))
            if proj is not None:
                # Occupied slot is both draggable and droppable (swap/replace)
                return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            else:
                # Empty slot is a drop target
                return base | Qt.ItemFlag.ItemIsDropEnabled

        return base

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return "Project Room"
        return None

    # ---------------------------------------------------------------- Drag & Drop Model API

    def mimeTypes(self) -> list[str]:
        return [MIME_TYPE_PROJECT]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        """Serializes selected project ID and source slot into MIME payload."""
        mime = QMimeData()
        for idx in indexes:
            if idx.isValid() and idx.internalId() != 0:
                p_id = self.data(idx, self.ROLES["project_id"])
                grp = self.data(idx, self.ROLES["group"])
                slot = self.data(idx, self.ROLES["slot"])
                if p_id:
                    payload = {
                        "project_id": p_id,
                        "source_group": grp,
                        "source_slot": slot,
                    }
                    data_bytes = json.dumps(payload).encode("utf-8")
                    mime.setData(MIME_TYPE_PROJECT, QByteArray(data_bytes))
                    return mime
        return mime

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def canDropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ) -> bool:
        """Zero I/O validation of drag format and destination."""
        if not data.hasFormat(MIME_TYPE_PROJECT):
            return False
        return True

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ) -> bool:
        """Receives drop and emits project_dropped signal for async persistence handling."""
        if not data.hasFormat(MIME_TYPE_PROJECT):
            return False

        try:
            raw = bytes(data.data(MIME_TYPE_PROJECT)).decode("utf-8")
            payload = json.loads(raw)
            project_id = payload.get("project_id")
            src_group = payload.get("source_group")
            src_slot = payload.get("source_slot")
        except Exception:
            return False

        if not project_id or not parent.isValid():
            return False

        # Determine target group & slot
        if parent.internalId() == 0:
            # Dropped directly on a slot item (whose parent is a group row)
            # Or dropped on a group row
            if row >= 0:
                target_group = self._groups[parent.row()]
                target_slot = row + 1
            else:
                # Dropped on a slot index directly
                target_group = self.data(parent, self.ROLES["group"])
                target_slot = self.data(parent, self.ROLES["slot"]) or 1
        else:
            target_group = self.data(parent, self.ROLES["group"])
            target_slot = self.data(parent, self.ROLES["slot"]) or 1

        if not target_group or not target_slot:
            return False

        # Emit drop signal for controller / MainWindow to handle with optimistic mutation
        self.project_dropped.emit(project_id, target_group, target_slot, src_group, src_slot)
        return True

    # ---------------------------------------------------------------- helpers

    def project_at(self, group: str, slot: int) -> Optional[Project]:
        return self._projects.get((group.upper(), slot))

    def group_count(self, group: str) -> int:
        g = group.upper()
        return sum(1 for (grp, _s), p in self._projects.items() if grp == g and p is not None)
