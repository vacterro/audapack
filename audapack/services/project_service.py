"""Application-level project operations, framework-neutral."""

from __future__ import annotations

from typing import Callable, Optional

from audapack.config import AppConfig, load_config
from audapack.models import SLOTS_PER_GROUP, Project
from audapack.projects import ProjectRegistry
from audapack.services.events import ProjectMoveResult


class ProjectService:
    """Owns list/move/swap/add/remove/rename/resolve operations.

    Uses the transactional ProjectRegistry so persistence stays cross-process
    safe. UI layers call these methods instead of coordinating registry +
    config saves manually.
    """

    def __init__(self, config: Optional[AppConfig] = None, base_dir=None):
        self.base_dir = base_dir
        self.config = config or load_config(base_dir)
        self.registry = ProjectRegistry(self.config, base_dir=base_dir, transactional=True)

    # ---------------------------------------------------------------- queries

    def list_projects(self) -> list[Project]:
        return list(self.registry.projects)

    def get_project(self, project_id: str) -> Optional[Project]:
        return self.registry.get_project_by_id(project_id)

    def get_slot_map(self):
        return self.registry.get_slot_map()

    def active_groups(self) -> list[str]:
        return self.registry.get_active_groups()

    def occupied_count(self, group: str) -> int:
        """Number of slots in ``group`` actually containing a project."""
        return sum(
            1
            for s in range(1, SLOTS_PER_GROUP + 1)
            if self.registry.get_slot_map().get((group.upper(), s)) is not None
        )

    def resolve_project(self, name: str) -> tuple[Project, bool]:
        proj, created = self.registry.resolve_or_register_project(name)
        return proj, created

    # ---------------------------------------------------------------- mutations

    def move_project(self, project_id: str, target_group: str, target_slot: int) -> ProjectMoveResult:
        """Move (or swap) one project; returns a specific targeted result."""
        current = self.registry.get_project_by_id(project_id)
        if not current:
            return ProjectMoveResult(project_id=project_id, ok=False)
        old_group = current.priority_group.upper()
        old_slot = current.slot

        swap_partner = None
        existing = self.registry.get_project_in_slot(target_group, target_slot)
        if existing and existing.id != project_id:
            swap_partner = existing

        ok = self.registry.move_project(project_id, target_group, target_slot)
        if not ok:
            return ProjectMoveResult(
                project_id=project_id, ok=False,
                old_group=old_group, old_slot=old_slot,
                new_group=target_group.upper(), new_slot=target_slot,
            )

        updated = self.registry.get_project_by_id(project_id)
        return ProjectMoveResult(
            project_id=project_id,
            ok=True,
            old_group=old_group,
            old_slot=old_slot,
            new_group=updated.priority_group.upper() if updated else target_group.upper(),
            new_slot=updated.slot if updated else target_slot,
            swapped_project_id=swap_partner.id if swap_partner else None,
        )

    def move_project_step(self, project_id: str, step: int) -> Optional[ProjectMoveResult]:
        """Move one slot up (-1) / down (+1); None when the step is illegal."""
        current = self.registry.get_project_by_id(project_id)
        if not current:
            return None
        old_group = current.priority_group.upper()
        old_slot = current.slot
        ok = self.registry.move_project_step(project_id, step)
        if not ok:
            return None
        updated = self.registry.get_project_by_id(project_id)
        return ProjectMoveResult(
            project_id=project_id,
            ok=True,
            old_group=old_group,
            old_slot=old_slot,
            new_group=updated.priority_group.upper() if updated else old_group,
            new_slot=updated.slot if updated else old_slot,
        )

    def add_project(
        self,
        display_name: str,
        source_path: str = "",
        enabled: bool = True,
        priority_group: Optional[str] = None,
        slot: Optional[int] = None,
        audit_project_name: Optional[str] = None,
    ) -> Project:
        return self.registry.add_project(
            display_name=display_name,
            source_path=source_path,
            enabled=enabled,
            priority_group=priority_group,
            slot=slot,
            audit_project_name=audit_project_name,
        )

    def remove_project(self, project_id: str) -> bool:
        return self.registry.remove_project(project_id)

    delete_project = remove_project

    def update_project(self, project_id: str, editor: Callable[[Project], None]) -> bool:
        """Apply arbitrary field edits inside the registry transaction."""
        return self.registry.edit_project(project_id, editor)

    def set_enabled(self, project_id: str, enabled: bool) -> bool:
        return self.update_project(project_id, lambda p: setattr(p, "enabled", enabled))

    def clear_project_marks(self) -> int:
        """Clear Done/archive-ignore/copy marks for all projects in one transaction."""
        return self.registry.clear_project_marks()
