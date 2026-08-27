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
import time
from datetime import datetime
from pathlib import Path
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

from audapack.audits import calculate_temperature, format_age_str
from audapack.config import app_dir
from audapack.models import AuditSnapshot, AuditTemperature, Project
from audapack.packing import find_archive_for_project, human_mb, resolve_output_dir
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
        "total_waves": Qt.ItemDataRole.UserRole + 15,
        "campaign_complete": Qt.ItemDataRole.UserRole + 16,
        "audit_age_str": Qt.ItemDataRole.UserRole + 17,
        "archive_info": Qt.ItemDataRole.UserRole + 18,
        "audit_profile_id": Qt.ItemDataRole.UserRole + 19,
        "archive_sync_status": Qt.ItemDataRole.UserRole + 20,
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
        # Single-pass load: reuse one list_projects() result and avoid double scan.
        projects = self._service.list_projects()
        for p in projects:
            g = p.priority_group.upper()
            self._projects[(g, p.slot)] = p

        # Snapshots: startup is now lazy (0 disk reads) — async enrichment populates.
        # Explicit reload (user Refresh All) still preloads synchronously for immediate feedback.
        self._snapshots = {}
        if not initial:
            for p in projects:
                try:
                    if hasattr(self._audit_service, "indexer"):
                        snap = self._audit_service.indexer.scan_project(p)
                    else:
                        snap = self._audit_service.get_snapshot(p.id)
                    if snap:
                        self._snapshots[p.id] = snap
                except Exception:
                    pass
        self.endResetModel()

    def reload(self):
        self._reload(initial=False)

    def project_at(self, group: str, slot: int) -> Optional[Project]:
        """Returns Project located at (group, slot), or None if empty."""
        return self._projects.get((group.upper(), slot))

    def project_by_id(self, project_id: str) -> Optional[Project]:
        """Finds Project by project ID."""
        for p in self._projects.values():
            if p and p.id == project_id:
                return p
        return None

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

        self.ensure_group_exists(tgt_g)

        if swapped_project:
            self._projects[(src_g, src_slot)] = swapped_project
        else:
            self._projects.pop((src_g, src_slot), None)

        self._projects[(tgt_g, tgt_slot)] = project

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
                if (snap.completed_waves > 0 or snap.final_handoff_ready or snap.all3_ready)
                else AuditTemperature.NONE
            )
            if new_temp != snap.temperature or int(new_age // 60) != int((snap.audit_age_seconds or 0) // 60):
                updated_snap = AuditSnapshot(
                    project_id=snap.project_id,
                    project_name=snap.project_name,
                    audit_profile_id=snap.audit_profile_id,
                    audit_profile_version=snap.audit_profile_version,
                    completed_waves=snap.completed_waves,
                    total_waves=snap.total_waves,
                    campaign_complete=snap.campaign_complete,
                    final_handoff_ready=snap.final_handoff_ready,
                    final_handoff_sha256=snap.final_handoff_sha256,
                    final_handoff_path=snap.final_handoff_path,
                    all_path=snap.all_path,
                    campaign_run_id=snap.campaign_run_id,
                    wave_files=snap.wave_files,
                    wave_statuses=snap.wave_statuses,
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
                    total_tickets=snap.total_tickets,
                )
                self._snapshots[proj.id] = updated_snap
                idx = self.index_for_slot(g, s)
                if idx.isValid():
                    changed_indexes.append(idx)

        for idx in changed_indexes:
            self.dataChanged.emit(idx, idx)

    def group_count(self, group: str) -> int:
        """Returns count of active projects in a group."""
        g_upper = group.upper()
        return sum(1 for (g, s), p in self._projects.items() if g == g_upper and p is not None)

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

    def index(self, row: int, column: int, parent: Optional[QModelIndex] = None) -> QModelIndex:
        p = parent if parent is not None else QModelIndex()
        if not self.hasIndex(row, column, p):
            return QModelIndex()
        if not p.isValid():
            return self.createIndex(row, column, 0)
        return self.createIndex(row, column, p.row() + 1)

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

    def rowCount(self, parent: Optional[QModelIndex] = None) -> int:
        p = parent if parent is not None else QModelIndex()
        if not p.isValid():
            return len(self._groups)
        if p.internalId() == 0:
            return 6  # SLOTS_PER_GROUP (1..6)
        return 0

    def columnCount(self, parent: Optional[QModelIndex] = None) -> int:
        return 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        if index.internalId() == 0:
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

        # Audit roles
        snap = self._snapshots.get(proj.id)
        if role == self.ROLES["audit_temperature"]:
            return getattr(snap, "temperature", AuditTemperature.NONE)
        if role == self.ROLES["audit_age_seconds"]:
            return getattr(snap, "audit_age_seconds", None)
        if role == self.ROLES["all_ready"]:
            return bool(getattr(snap, "final_handoff_ready", False) or getattr(snap, "all3_ready", False) or getattr(snap, "all_ready", False))
        if role == self.ROLES["completed_waves"]:
            return getattr(snap, "completed_waves", 0)
        if role == self.ROLES["total_waves"]:
            return getattr(snap, "total_waves", 3)
        if role == self.ROLES["campaign_complete"]:
            return bool(getattr(snap, "campaign_complete", False))
        if role == self.ROLES["audit_age_str"]:
            age_s = getattr(snap, "audit_age_seconds", None) if snap else None
            return format_age_str(age_s)
        if role == self.ROLES["archive_info"]:
            return self.get_archive_info(proj)
        if role == self.ROLES["audit_profile_id"]:
            prof = getattr(snap, "audit_profile_id", "quick3") if snap else "quick3"
            return "A10" if prof == "super10" else "A3"
        if role == self.ROLES["archive_sync_status"]:
            return self.get_archive_sync_status(proj, snap)

        # Pack state role
        if role == self.ROLES["pack_state"]:
            st, _msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return st
        if role == self.ROLES["pack_message"]:
            _st, msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return msg

        return None

    def get_archive_info(self, proj: Project) -> tuple[bool, str, str, Optional[Path]]:
        """Returns (exists, size_str, age_str, path)."""
        if not proj or not proj.source_path:
            return (False, "", "", None)
        try:
            out_dir = resolve_output_dir(proj.source_path, self._service.config.packing, fallback=app_dir(), group=proj.priority_group, project=proj)
            arc = find_archive_for_project(proj, out_dir)
            if arc and arc.exists():
                st = arc.stat()
                age_s = time.time() - st.st_mtime
                return (True, human_mb(st.st_size), format_age_str(age_s), arc)
        except Exception:
            pass
        return (False, "", "", None)

    def get_archive_sync_status(self, proj: Project, snap: Optional[AuditSnapshot]) -> str:
        """Returns 'SYNCED', 'OUTDATED', or 'NO_ARCHIVE'."""
        if not proj or not proj.source_path:
            return "NO_ARCHIVE"
        arc_info = self.get_archive_info(proj)
        arc_exists, _size, _age, arc_path = arc_info
        if not arc_exists or not arc_path:
            return "NO_ARCHIVE"
        if not snap or not snap.audit_timestamp:
            return "SYNCED"
        try:
            arc_mtime = arc_path.stat().st_mtime
            audit_ts = snap.audit_timestamp.timestamp()
            if audit_ts > arc_mtime + 60:
                return "OUTDATED"
        except Exception:
            pass
        return "SYNCED"

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled

        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        if index.internalId() == 0:
            return base | Qt.ItemFlag.ItemIsDropEnabled

        group_idx = int(index.internalId()) - 1
        group = self._groups[group_idx] if group_idx < len(self._groups) else ""
        slot = index.row() + 1
        proj = self._projects.get((group, slot))

        if proj is not None:
            return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDropEnabled

    def mimeTypes(self) -> list[str]:
        return [MIME_TYPE_PROJECT, "text/uri-list"]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        mime = QMimeData()
        if not indexes:
            return mime
        idx = indexes[0]
        if not idx.isValid() or idx.internalId() == 0:
            return mime

        group_idx = int(idx.internalId()) - 1
        if group_idx >= len(self._groups):
            return mime
        group = self._groups[group_idx]
        slot = idx.row() + 1
        proj = self._projects.get((group, slot))
        if not proj:
            return mime

        payload = {
            "project_id": proj.id,
            "display_name": proj.display_name,
            "source_group": group,
            "source_slot": slot,
        }
        data = QByteArray(json.dumps(payload).encode("utf-8"))
        mime.setData(MIME_TYPE_PROJECT, data)
        mime.setText(proj.display_name)
        return mime

    def canDropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ) -> bool:
        if data.hasFormat(MIME_TYPE_PROJECT) or data.hasUrls():
            return True
        return False

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ) -> bool:
        if not data.hasFormat(MIME_TYPE_PROJECT):
            return False

        try:
            payload = json.loads(bytes(data.data(MIME_TYPE_PROJECT)).decode("utf-8"))
        except Exception:
            return False

        proj_id = payload.get("project_id")
        src_group = payload.get("source_group")
        src_slot = payload.get("source_slot")

        if not proj_id or not src_group or src_slot is None:
            return False

        if not parent.isValid():
            target_group = src_group
            target_slot = 1
            for s in range(1, 7):
                if (target_group, s) not in self._projects:
                    target_slot = s
                    break
        elif parent.internalId() == 0:
            target_group = self._groups[parent.row()]
            if row >= 0:
                target_slot = row + 1
            else:
                target_slot = 1
                for s in range(1, 7):
                    if (target_group, s) not in self._projects:
                        target_slot = s
                        break
        else:
            group_idx = int(parent.internalId()) - 1
            if group_idx >= len(self._groups):
                return False
            target_group = self._groups[group_idx]
            target_slot = parent.row() + 1

        target_slot = max(1, min(6, target_slot))

        if target_group == src_group and target_slot == src_slot:
            return False

        self.project_dropped.emit(proj_id, target_group, target_slot, src_group, src_slot)
        return True
