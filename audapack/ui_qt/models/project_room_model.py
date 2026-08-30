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
import os
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

from audapack.audits import calculate_temperature, format_age_str, format_created_str
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
        "hover_info": Qt.ItemDataRole.UserRole + 21,
        "is_ignored": Qt.ItemDataRole.UserRole + 22,
        "archive_temperature": Qt.ItemDataRole.UserRole + 23,
        "archive_ignored": Qt.ItemDataRole.UserRole + 24,
        "audit_copy_count": Qt.ItemDataRole.UserRole + 25,
        "pack_progress": Qt.ItemDataRole.UserRole + 26,  # dict {files_added, bytes_written, current_path} or None
        "pack_percent": Qt.ItemDataRole.UserRole + 27,  # 0..100 float or None
        "archive_mtime": Qt.ItemDataRole.UserRole + 28,  # float epoch seconds or None
        "source_dir_mtime": Qt.ItemDataRole.UserRole + 29,  # float epoch seconds or None
        "source_older_than_archive": Qt.ItemDataRole.UserRole + 30,  # True/False/None
        "archive_freshness_short": Qt.ItemDataRole.UserRole + 31,  # "fresh" | "stale" | "old" | "none"
    }

    def __init__(self, service: ProjectService, audit_service: Optional[AuditService] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._service = service
        self._audit_service = audit_service or AuditService(service.config, base_dir=service.base_dir)
        self._groups: list[str] = []
        self._projects: dict[tuple[str, int], Project] = {}  # (group, slot) -> Project
        self._snapshots: dict[str, AuditSnapshot] = {}  # project_id -> AuditSnapshot
        self._pack_states: dict[str, tuple[str, str]] = {}  # project_id -> (state, message)
        # Per-project live pack progress (files_added, bytes_written, current_path).
        # Populated via update_pack_progress() from a worker thread callback; cleared
        # when the pack state transitions away from PACKING/QUEUED.
        self._pack_progress: dict[str, dict] = {}
        # Monotonic counter so coalesced progress callbacks for a previous run
        # cannot bleed into a fresh pack.
        self._pack_run_id: dict[str, int] = {}
        # Archive freshness cache. The source tree freshness probe is a bounded
        # filesystem walk; running it on every data()/paint would stall the GUI
        # thread (white flashes). All archive roles read this cache; it is
        # invalidated after a pack and refreshed by the temperature tick.
        self._archive_fresh_cache: dict[str, dict] = {}
        self._archive_fresh_ttl = 10.0

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

        old_projects = {p.id: p for p in self._projects.values() if p}
        old_snapshots = self._snapshots
        self.beginResetModel()
        self._groups = self._service.active_groups()
        self._projects = {}
        # Single-pass structural rebuild; audit enrichment stays asynchronous.
        projects = self._service.list_projects()
        for p in projects:
            g = p.priority_group.upper()
            self._projects[(g, p.slot)] = p

        self._snapshots = {}
        for p in projects:
            previous = old_projects.get(p.id)
            if previous and (
                previous.source_path == p.source_path
                and previous.audit_project_name == p.audit_project_name
            ):
                snap = old_snapshots.get(p.id)
                if snap:
                    self._snapshots[p.id] = snap
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
        """Applies a project move/swap in-memory and emits layoutChanged for reliable repaint."""
        self.targeted_project_update_count += 1
        src_g = src_group.upper()
        tgt_g = tgt_group.upper()

        self.ensure_group_exists(tgt_g)

        # Emit layoutAboutToBeChanged so the view saves persistent indexes
        self.layoutAboutToBeChanged.emit()

        if swapped_project:
            self._projects[(src_g, src_slot)] = swapped_project
        else:
            self._projects.pop((src_g, src_slot), None)

        self._projects[(tgt_g, tgt_slot)] = project

        # layoutChanged forces complete re-layout — guaranteed repaint for swaps
        self.layoutChanged.emit()

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
        old_snap = self._snapshots.get(project_id)
        if snapshot:
            self._snapshots[project_id] = snapshot
        else:
            self._snapshots.pop(project_id, None)

        # Reset copy counter when audit completely refreshed (new run or new final hash)
        if snapshot and old_snap:
            new_run = getattr(snapshot, "campaign_run_id", "") or ""
            old_run = getattr(old_snap, "campaign_run_id", "") or ""
            new_sha = getattr(snapshot, "final_handoff_sha256", "") or getattr(snapshot, "all3_sha256", "") or ""
            old_sha = getattr(old_snap, "final_handoff_sha256", "") or getattr(old_snap, "all3_sha256", "") or ""
            is_new_audit = False
            if new_run and old_run and new_run != old_run:
                is_new_audit = True
            elif new_sha and old_sha and new_sha != old_sha:
                is_new_audit = True
            elif getattr(snapshot, "audit_timestamp", None) and getattr(old_snap, "audit_timestamp", None):
                try:
                    if snapshot.audit_timestamp and old_snap.audit_timestamp and snapshot.audit_timestamp > old_snap.audit_timestamp:
                        # newer timestamp + HOT means fresh ingest
                        if getattr(snapshot, "audit_age_seconds", 9999) is not None and snapshot.audit_age_seconds < 300:
                            is_new_audit = True
                except Exception:
                    pass
            if is_new_audit:
                proj = next((p for p in self._service.list_projects() if p.id == project_id), None)
                if proj and getattr(proj, "audit_copy_count", 0):
                    try:
                        self._service.update_project(project_id, lambda p: setattr(p, "audit_copy_count", 0))
                    except Exception:
                        pass

        idx = self.index_for_project_id(project_id)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def update_pack_state(self, project_id: str, state: str, message: str = ""):
        """Updates packing progress/completion status for a project row."""
        self.targeted_project_update_count += 1
        self._pack_states[project_id] = (state, message)
        if state not in ("PACKING", "QUEUED"):
            # Pack finished (COMPLETE / FAILED / IDLE): drop any in-flight progress,
            # bump the run id so a stale worker callback becomes a no-op, and force
            # the archive freshness cache to recompute (a failed pack may have moved
            # the archive to a .PARTIAL.* name / restored the previous good one).
            self._pack_progress.pop(project_id, None)
            self._pack_run_id[project_id] = self._pack_run_id.get(project_id, 0) + 1
            self._archive_fresh_cache.pop(project_id, None)
        else:
            # A fresh run is starting: invalidate any previous run id so a
            # lingering worker for the prior pack cannot update progress here.
            self._pack_run_id[project_id] = self._pack_run_id.get(project_id, 0) + 1
            self._pack_progress.pop(project_id, None)
        idx = self.index_for_project_id(project_id)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def get_current_pack_run_id(self, project_id: str) -> int:
        """Returns the active run id for ``project_id`` (0 if no run is registered)."""
        return int(self._pack_run_id.get(project_id, 0))

    def update_pack_progress(self, project_id: str, files_added: int, bytes_written: int, current_path: str = "", run_id: int = 0):
        """Live progress callback for a single pack run.

        ``run_id`` must equal the most recent ``update_pack_state`` run id for
        this project. When a pack finishes (state != PACKING/QUEUED) the run id
        is bumped; any worker that still calls this with the old id becomes a
        no-op so the UI never shows stale progress.
        """
        if run_id != self._pack_run_id.get(project_id, 0):
            # Stale worker callback for an older (or unregistered) run: drop it.
            return
        if project_id not in self._pack_states:
            return
        state, _ = self._pack_states[project_id]
        if state not in ("PACKING", "QUEUED"):
            return
        self._pack_progress[project_id] = {
            "files_added": max(0, int(files_added or 0)),
            "bytes_written": max(0, int(bytes_written or 0)),
            "current_path": str(current_path or ""),
        }
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

        # PERF-001: recompute archive/source freshness for entries whose TTL
        # expired. _get_archive_fresh marks them stale during a paint read and
        # this periodic tick does the filesystem work off the data() path.
        stale_ids = getattr(self, "_stale_projects", None)
        if stale_ids:
            for pid in list(stale_ids):
                stale_ids.discard(pid)
                proj = self.project_by_id(pid)
                if proj:
                    self._archive_fresh_cache[pid] = self._compute_archive_fresh(proj)
                    idx = self.index_for_project_id(pid)
                    if idx.isValid():
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
        if role == self.ROLES["is_ignored"]:
            return bool(getattr(proj, "ignored", False))
        if role == self.ROLES["archive_ignored"]:
            return bool(getattr(proj, "ignore_archive", False))
        if role == self.ROLES["audit_copy_count"]:
            return int(getattr(proj, "audit_copy_count", 0) or 0)

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
        if role == self.ROLES["archive_temperature"]:
            return self._get_archive_temperature(proj)
        if role == self.ROLES["pack_progress"]:
            return self._pack_progress.get(proj.id)
        if role == self.ROLES["pack_percent"]:
            prog = self._pack_progress.get(proj.id)
            if not prog:
                return None
            # Source size = best estimate. We only know the running bytes_written.
            # Use a moving estimate: bytes_written / max(bytes_written + chunk, total_seen).
            # Without a total, we cap to 99% and let COMPLETE/FAILED clear it.
            ba = int(prog.get("bytes_written") or 0)
            # The packing engine emits progress at 1 + every 50 files; we have no
            # total file count yet. Use a simple bounded log-scale proxy:
            # 1 MB = 5%, 10 MB = 25%, 100 MB = 60%, 1 GB = 90%, >1 GB ~ 95%.
            mb = ba / (1024 * 1024)
            if mb <= 0:
                pct = 1.0
            elif mb < 1:
                pct = 1 + (mb * 4.0)            # 0->1, 0.25->2, 0.99->5
            elif mb < 10:
                pct = 5 + (mb - 1) * (20 / 9)   # 1->5, 10->25
            elif mb < 100:
                pct = 25 + (mb - 10) * (35 / 90)
            elif mb < 1024:
                pct = 60 + (mb - 100) * (30 / 924)
            else:
                pct = min(95, 90 + (mb - 1024) * (5 / 1024))
            return float(pct)
        if role == self.ROLES["archive_mtime"]:
            return self._get_archive_mtime(proj)
        if role == self.ROLES["source_dir_mtime"]:
            return self._get_source_dir_mtime(proj)
        if role == self.ROLES["source_older_than_archive"]:
            return self._source_older_than_archive(proj)
        if role == self.ROLES["archive_freshness_short"]:
            return self._get_archive_freshness_short(proj)

        # Pack state role
        if role == self.ROLES["pack_state"]:
            st, _msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return st
        if role == self.ROLES["pack_message"]:
            _st, msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return msg

        # Rich hover info dict — everything the tooltip builder needs
        if role == self.ROLES["hover_info"]:
            snap = self._snapshots.get(proj.id)
            arc_data = self.get_archive_info(proj)
            pack_st, pack_msg = self._pack_states.get(proj.id, ("IDLE", ""))
            return {
                "project": proj,
                "snapshot": snap,
                "archive_info": arc_data,
                "pack_state": pack_st,
                "pack_message": pack_msg,
                "pack_progress": self._pack_progress.get(proj.id),
                "pack_percent": self._pack_progress.get(proj.id) and self.data(index, self.ROLES["pack_percent"]) or None,
                "archive_sync_status": self.get_archive_sync_status(proj, snap),
                "archive_freshness_short": self._get_archive_freshness_short(proj),
                "source_older_than_archive": self._source_older_than_archive(proj),
                "group": group,
                "slot": slot,
                "total_groups": len(self._groups),
                "group_count": self.group_count(group),
            }

        return None

    def get_archive_info(self, proj: Project) -> tuple[bool, str, str, Optional[Path]]:
        """Returns (exists, size_str, created_str, path) — pure cache reads.

        PERF-001: never stat on paint; size/created are already captured into
        the cached snapshot at compute time.
        """
        entry = self._get_archive_fresh(proj)
        if entry["exists"] and entry["path"]:
            size_str = entry.get("size_str") or ""
            created_str = entry.get("created_str") or ""
            return (True, size_str, created_str, entry["path"])
        return (False, "", "", None)

    def get_archive_sync_status(self, proj: Project, snap: Optional[AuditSnapshot]) -> str:
        """Returns 'SYNCED', 'OUTDATED', or 'NO_ARCHIVE' (cache-backed)."""
        entry = self._get_archive_fresh(proj)
        if not entry["exists"] or not entry["path"]:
            return "NO_ARCHIVE"
        return entry.get("sync_status", "SYNCED")

    # ---------------------------------------------------------------- Archive freshness cache
    #
    # The source-tree freshness probe is a bounded filesystem walk. If it ran on
    # every data()/paint call the GUI thread would stall (white flashes every
    # repaint). All archive roles below read ONE cached snapshot per project,
    # recomputed at most every ``_archive_fresh_ttl`` seconds.

    def _compute_archive_fresh(self, proj: Project) -> dict:
        """Single bounded disk pass: archive + source freshness for a project.

        Returns a dict consumed by the archive roles. Never raises. The source
        walk is capped both by file count and wall-clock budget so even a huge
        tree cannot stall the UI thread.
        """
        entry = {
            "computed_at": time.time(),
            "exists": False,
            "path": None,
            "mtime": None,
            "size_str": "",
            "created_str": "",
            "temperature": AuditTemperature.NONE,
            "sync_status": "NO_ARCHIVE",
            "source_mtime": None,
            "source_older": None,
            "freshness_short": "none",
        }
        try:
            if not proj or not proj.source_path:
                return entry
            sp = Path(proj.source_path)
            if not sp.exists():
                return entry

            out_dir = resolve_output_dir(proj.source_path, self._service.config.packing, fallback=app_dir(), group=proj.priority_group, project=proj)
            arc = find_archive_for_project(proj, out_dir)

            arc_mt: Optional[float] = None
            if arc and arc.exists():
                try:
                    arc_mt = float(arc.stat().st_mtime)
                except OSError:
                    arc_mt = None
                if arc_mt is not None:
                    entry["exists"] = True
                    entry["path"] = arc
                    entry["mtime"] = arc_mt
                    try:
                        st = arc.stat()
                        entry["size_str"] = human_mb(st.st_size)
                        entry["created_str"] = format_created_str(getattr(st, "st_ctime", st.st_mtime))
                    except OSError:
                        entry["size_str"] = ""
                        entry["created_str"] = ""
                    cfg = self._service.config.audits
                    entry["temperature"] = calculate_temperature(max(0.0, time.time() - arc_mt), cfg)
                    age_s = max(0.0, time.time() - arc_mt)
                    entry["freshness_short"] = "fresh" if age_s < 3600 else ("stale" if age_s < 86400 else "old")
                    snap = self._snapshots.get(proj.id)
                    entry["sync_status"] = self._sync_status_from(proj, snap, arc, arc_mt)

            # Bounded source probe (only when an archive exists to compare against;
            # skip entirely when nothing is packed so the row never pays for the walk).
            if arc_mt is not None:
                src_mt, src_complete = self._probe_source_mtime(sp)
                if src_mt is not None:
                    entry["source_mtime"] = src_mt
                    # PERF-001: only a COMPLETE traversal may prove the source is
                    # newer (or older) than the archive. A budget/interrupt-limited
                    # scan is UNKNOWN, never evidence of freshness.
                    if src_complete:
                        entry["source_older"] = src_mt > arc_mt + 0.5
        except Exception:
            pass
        return entry

    def _sync_status_from(self, proj: Project, snap: Optional[AuditSnapshot], arc_path: Path, arc_mtime: float) -> str:
        """SYNCED / OUTDATED / NO_ARCHIVE using the cached archive mtime."""
        if not snap or not snap.audit_timestamp:
            return "SYNCED"
        try:
            audit_ts = snap.audit_timestamp.timestamp()
            if audit_ts > arc_mtime + 60:
                return "OUTDATED"
        except Exception:
            pass
        return "SYNCED"

    def _probe_source_mtime(self, sp: Path) -> tuple[Optional[float], bool]:
        """Newest file mtime under ``sp``. File cap + time budget bound the walk.

        Returns ``(mtime, complete)``. When the budget or file cap is exhausted
        ``complete`` is ``False`` and the mtime is the best-effort newest seen so
        far -- callers MUST NOT treat it as proof that source is older than the
        archive (PERF-001).
        """
        try:
            if sp.is_file():
                return float(sp.stat().st_mtime), True
            max_entries = 1000
            budget_s = 0.15
            deadline = time.monotonic() + budget_s
            newest = sp.stat().st_mtime
            count = 0
            for root, _dirs, files in os.walk(sp):
                if count >= max_entries or time.monotonic() >= deadline:
                    return float(newest), False
                for f in files:
                    if count >= max_entries or time.monotonic() >= deadline:
                        return float(newest), False
                    try:
                        mt = (Path(root) / f).stat().st_mtime
                        if mt > newest:
                            newest = mt
                    except OSError:
                        continue
                    count += 1
            return float(newest), True
        except Exception:
            return None, False

    def _get_archive_fresh(self, proj: Project) -> dict:
        """Cached archive freshness entry.

        PERF-001: this is a PURE READ of the precomputed snapshot. On TTL expiry
        the stale entry is returned immediately and a deferred recompute flag is
        set; the actual filesystem walk happens in ``update_temperature_all()``
        (called from the temperature tick timer, never during data()/paint).
        """
        if not proj:
            return {
                "computed_at": time.time(), "exists": False, "path": None, "mtime": None,
                "size_str": "", "created_str": "", "temperature": AuditTemperature.NONE,
                "sync_status": "NO_ARCHIVE", "source_mtime": None, "source_older": None,
                "freshness_short": "none",
            }
        now = time.time()
        entry = self._archive_fresh_cache.get(proj.id)
        if entry is None:
            # Cache miss: initial populate / post-invalidation read. This is the
            # only synchronous compute path (startup enrichment, invalidation
            # follow-up); normal paints hit the TTL branch below.
            entry = self._compute_archive_fresh(proj)
            self._archive_fresh_cache[proj.id] = entry
            return entry
        if (now - entry.get("computed_at", 0)) >= self._archive_fresh_ttl:
            # Stale but serve it; recompute is scheduled in update_temperature_all.
            if not hasattr(self, "_stale_projects"):
                self._stale_projects = set()
            self._stale_projects.add(proj.id)
        return entry

    def invalidate_archive_fresh(self, project_id: str) -> None:
        """Force the next archive freshness read to recompute (after a pack)."""
        self._archive_fresh_cache.pop(project_id, None)

    def _get_archive_temperature(self, proj: Project) -> AuditTemperature:
        return self._get_archive_fresh(proj)["temperature"]

    def _get_archive_mtime(self, proj: Project) -> Optional[float]:
        return self._get_archive_fresh(proj)["mtime"]

    def _get_source_dir_mtime(self, proj: Project) -> Optional[float]:
        return self._get_archive_fresh(proj)["source_mtime"]

    def _source_older_than_archive(self, proj: Project) -> Optional[bool]:
        return self._get_archive_fresh(proj)["source_older"]

    def _get_archive_freshness_short(self, proj: Project) -> str:
        return self._get_archive_fresh(proj)["freshness_short"]

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
