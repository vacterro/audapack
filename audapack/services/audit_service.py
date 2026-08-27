"""Audit operations — framework-neutral."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from audapack.audits import AuditIndexer, AuditSnapshot
from audapack.config import AppConfig, load_config
from audapack.projects import ProjectRegistry


class AuditService:
    """Presents audit state without exposing file layout or parsing details."""

    def __init__(self, config: Optional[AppConfig] = None, base_dir=None):
        self.base_dir = base_dir
        self.config = config or load_config(base_dir)
        self.registry = ProjectRegistry(self.config, base_dir=base_dir, transactional=True)
        self.indexer = AuditIndexer(self.config)

    def invalidate(self, project_id: Optional[str] = None):
        """Invalidates cache for a specific project or all projects."""
        self.indexer.invalidate(project_id)

    def get_snapshot(self, project_id: str, force_rescan: bool = False) -> Optional[AuditSnapshot]:
        if force_rescan:
            self.indexer.invalidate(project_id)
        proj = self.registry.get_project_by_id(project_id)
        if not proj:
            return None
        return self.indexer.scan_project(proj)

    def refresh_project(self, project_id: str) -> Optional[AuditSnapshot]:
        """Refreshes ONLY the single requested project (never all projects)."""
        return self.get_snapshot(project_id, force_rescan=True)

    def refresh_all(self) -> Dict[str, AuditSnapshot]:
        self.indexer.invalidate()
        return self.indexer.scan_all_projects()

    def get_preferred_audit_file_path(self, project_id: str) -> Optional[Path]:
        """Returns Path to the preferred audit file for the project."""
        snap = self.get_snapshot(project_id)
        if not snap:
            return None
        return self.indexer.get_preferred_audit_file_path(snap)

    def copy_latest_campaign(self, project_id: str) -> tuple[bool, str, str]:
        """Reads preferred final campaign handoff (SUPER_AUDIT_FINAL, SUPER_AUDIT_ALL, or ALL_3)."""
        snap = self.get_snapshot(project_id)
        if not snap:
            return False, "", ""
        return self.indexer.read_preferred_handoff(snap)

    def copy_all3(self, project_id: str) -> tuple[bool, str, str]:
        """Legacy alias: copies preferred handoff text."""
        return self.copy_latest_campaign(project_id)

