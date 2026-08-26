"""Project registry management for AUDAPACK.

Maintains the priority grid (MAIN0, MAIN1, SIDE0, SIDE1, and dynamic SIDE2+),
handles atomic auto-registration, slot assignment, movement, deletion, and discovery.

All mutating operations can run inside a cross-process transaction:
lock -> reload latest config from disk -> mutate latest -> atomic save (verified).
The transaction is active when ``transactional=True`` (or a ``base_dir`` is given);
legacy in-memory callers keep the old non-persisting behavior.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from audapack.config import (
    AppConfig,
    auto_heal_project_path,
    config_path,
    cross_process_lock,
    get_registry_lock_path,
    load_config,
    safe_slug,
    save_config,
)
from audapack.models import CANONICAL_GROUPS, SLOTS_PER_GROUP, PriorityGroup, Project


class RegistrySaveError(RuntimeError):
    """Atomic registry persistence failed; the mutation MUST NOT report success."""


_RE_NORM_PROJ = re.compile(r'[\s_\-]+')


def normalize_project_key(name: str) -> str:
    """Normalizes a project name for safe, deterministic matching (case-insensitive, collapsed spacing/underscores)."""
    if not name:
        return ""
    return _RE_NORM_PROJ.sub('', name.strip().lower())


class ProjectRegistry:
    def __init__(self, config: AppConfig, base_dir: Optional[Path] = None, transactional: Optional[bool] = None):
        self.config = config
        self.base_dir = base_dir
        # A custom base_dir implies disk-backed usage (Bridge/tests); plain UI/test
        # construction stays legacy in-memory unless explicitly opted in.
        self.transactional = bool(base_dir) if transactional is None else bool(transactional)

    @contextmanager
    def _mutate_latest(self, persist_legacy: bool = False):
        """Cross-process registry mutation transaction.

        Yields a tx object whose ``cfg`` MUST be mutated instead of self.config.
        Set ``tx.skip = True`` to abort without saving (no-op mutations).
        In transactional mode: lock -> reload latest from canonical disk ->
        caller mutates -> verified atomic save -> sync self.config in place.
        A save failure raises RegistrySaveError -- never a false success.
        Legacy in-memory mode keeps the historical behavior exactly: it only
        persists when ``persist_legacy`` is set (the old resolve path).
        """
        class _Tx:
            def __init__(self, cfg: AppConfig):
                self.cfg = cfg
                self.skip = False

        if not self.transactional:
            tx = _Tx(self.config)
            yield tx
            if not tx.skip and persist_legacy:
                if not save_config(self.config, base_dir=self.base_dir):
                    raise RegistrySaveError("atomic registry save failed")
            return

        lock_path = get_registry_lock_path(self.base_dir)
        with cross_process_lock(lock_path):
            cfg_file = config_path(self.base_dir)
            if cfg_file.exists():
                latest = load_config(self.base_dir)
            else:
                # First write: seed from the in-memory snapshot.
                latest = self.config
            tx = _Tx(latest)
            yield tx
            if tx.skip:
                return
            if not save_config(latest, base_dir=self.base_dir):
                raise RegistrySaveError("atomic registry save failed")
            # Sync the caller's snapshot in place so existing references observe
            # the committed state without replacing object identity.
            self.config.projects[:] = latest.projects

    @property
    def projects(self) -> list[Project]:
        return self.config.projects

    def get_active_groups(self) -> list[str]:
        """Returns ordered list of active groups: MAIN0, MAIN1, SIDE0, SIDE1, followed by any dynamic SIDE2, SIDE3, etc."""
        return self._active_groups_in(self.config)

    def get_slot_map(self) -> dict[tuple[str, int], Optional[Project]]:
        """Returns a mapping of (group, slot) -> Project or None for all active groups."""
        grid: dict[tuple[str, int], Optional[Project]] = {}
        active_groups = self.get_active_groups()
        for g in active_groups:
            for s in range(1, SLOTS_PER_GROUP + 1):
                grid[(g, s)] = None

        for p in self.config.projects:
            grp = p.priority_group.upper()
            if grp in active_groups and 1 <= p.slot <= SLOTS_PER_GROUP:
                grid[(grp, p.slot)] = p
        return grid

    def get_project_in_slot(self, group: str, slot: int) -> Optional[Project]:
        grp = group.upper()
        for p in self.config.projects:
            if p.priority_group.upper() == grp and p.slot == slot:
                return p
        return None

    def get_project_by_id(self, project_id: str) -> Optional[Project]:
        p_id = project_id.strip().lower()
        for p in self.config.projects:
            if p.id.lower() == p_id:
                return p
        return None

    def get_project_by_name(self, name: str) -> Optional[Project]:
        """Matches project by display name, audit name, slug or normalized identity."""
        if not name:
            return None
        target_raw = name.strip().lower()
        target_norm = normalize_project_key(name)
        target_slug = safe_slug(name)

        for p in self.config.projects:
            p_display = p.display_name.strip().lower()
            p_audit = (p.audit_project_name or "").strip().lower()
            if p_display == target_raw or p_audit == target_raw or p.id.lower() == target_slug:
                return p
            if normalize_project_key(p.display_name) == target_norm or normalize_project_key(p.audit_project_name) == target_norm:
                return p
        return None

    def get_project_by_path(self, path: str | Path) -> Optional[Project]:
        target = Path(path).resolve()
        for p in self.config.projects:
            if p.source_path:
                try:
                    if Path(p.source_path).resolve() == target:
                        return p
                except Exception:
                    pass
        return None

    def find_first_free_slot(self, preferred_group: Optional[str] = None) -> Optional[tuple[str, int]]:
        return self._find_first_free_slot_in(self.config, preferred_group)

    def _find_first_free_slot_in(self, cfg: AppConfig, preferred_group: Optional[str] = None) -> Optional[tuple[str, int]]:
        # CORE-009: snapshot-aware free-slot lookup. Always operate on the
        # supplied cfg (the latest locked snapshot inside a transaction) rather
        # than the stale self.config, otherwise a stale pre-lock snapshot can
        # return an already-occupied slot and persist duplicate ownership.
        occupied = {(p.priority_group.upper(), p.slot) for p in cfg.projects}
        active_groups = self._active_groups_in(cfg)
        groups_to_check = [preferred_group.upper()] if preferred_group and preferred_group.upper() in active_groups else list(active_groups)
        if preferred_group and preferred_group.upper() in active_groups:
            for g in active_groups:
                if g not in groups_to_check:
                    groups_to_check.append(g)

        for g in groups_to_check:
            for s in range(1, SLOTS_PER_GROUP + 1):
                if (g, s) not in occupied:
                    return g, s
        return None

    def find_first_free_side_slot(self) -> tuple[str, int]:
        """
        Finds the first free slot starting from SIDE1.
        If SIDE1 is full (slots 1..6), checks SIDE2 (slots 1..6), then SIDE3, etc.
        Creates a new dynamic SIDE group if all existing are full.
        """
        return self._first_free_side_slot_in(self.config)

    def resolve_or_register_project(self, raw_project_name: str) -> tuple[Project, bool]:
        """
        Canonical atomic project resolution for AUDAPACK Bridge:
        1. Search existing registry by identity / alias / normalized name.
        2. If FOUND -> returns (existing_project, False). NEVER re-arranges.
        3. If NOT FOUND -> automatically registers in the first free SIDE1+ slot (growing to SIDE2, SIDE3 as needed),
           saves config, and returns (new_project, True).

        Transactional mode: lock -> reload latest -> resolve against latest ->
        allocate slot against latest state -> atomic save (verified). A save
        failure raises RegistrySaveError instead of returning a false success.
        """
        clean_name = raw_project_name.strip()
        if not clean_name:
            clean_name = "UNNAMED_PROJECT"

        with self._mutate_latest(persist_legacy=True) as tx:
            latest = tx.cfg
            existing = self._find_in(latest, clean_name)
            if existing:
                tx.skip = True
                return existing, False

            # Not found: auto-register in SIDE1+ against LATEST state
            target_group, target_slot = self._first_free_side_slot_in(latest)
            p_id = safe_slug(clean_name)
            base_id = p_id
            counter = 1
            while any(p.id.lower() == p_id.lower() for p in latest.projects):
                p_id = f"{base_id}_{counter}"
                counter += 1

            source_path = auto_heal_project_path(clean_name, "")
            new_proj = Project(
                id=p_id,
                display_name=clean_name,
                source_path=source_path,
                enabled=True,
                priority_group=target_group,
                slot=target_slot,
                archive_name=clean_name,
                audit_project_name=clean_name,
            )
            latest.projects.append(new_proj)
            return new_proj, True

    # ------------------------------------------------------------------ helpers over an explicit config snapshot

    @staticmethod
    def _find_in(cfg: AppConfig, name: str) -> Optional[Project]:
        """Alias/normalized identity match against an explicit snapshot."""
        if not name:
            return None
        target_raw = name.strip().lower()
        target_norm = normalize_project_key(name)
        target_slug = safe_slug(name)
        for p in cfg.projects:
            p_display = p.display_name.strip().lower()
            p_audit = (p.audit_project_name or "").strip().lower()
            if p_display == target_raw or p_audit == target_raw or p.id.lower() == target_slug:
                return p
            if normalize_project_key(p.display_name) == target_norm or normalize_project_key(p.audit_project_name) == target_norm:
                return p
        return None

    @staticmethod
    def _find_by_id_in(cfg: AppConfig, project_id: str) -> Optional[Project]:
        p_id = project_id.strip().lower()
        for p in cfg.projects:
            if p.id.lower() == p_id:
                return p
        return None

    @staticmethod
    def _first_free_side_slot_in(cfg: AppConfig) -> tuple[str, int]:
        occupied = {(p.priority_group.upper(), p.slot) for p in cfg.projects}
        side_num = 1
        while True:
            group_name = f"SIDE{side_num}"
            for slot_num in range(1, SLOTS_PER_GROUP + 1):
                if (group_name, slot_num) not in occupied:
                    return group_name, slot_num
            side_num += 1

    def _active_groups_in(self, cfg: AppConfig) -> list[str]:
        groups = list(CANONICAL_GROUPS)
        side_extras = set()
        for p in cfg.projects:
            grp = p.priority_group.upper()
            if grp not in groups and grp.startswith("SIDE"):
                side_extras.add(grp)

        def _side_sort_key(g_name: str) -> int:
            m = re.match(r'SIDE(\d+)', g_name)
            return int(m.group(1)) if m else 999

        groups.extend(sorted(side_extras, key=_side_sort_key))
        return groups

    def add_project(
        self,
        display_name: str,
        source_path: str,
        enabled: bool = True,
        priority_group: Optional[str] = None,
        slot: Optional[int] = None,
        audit_project_name: Optional[str] = None,
    ) -> Project:
        with self._mutate_latest() as tx:
            latest = tx.cfg
            p_id = safe_slug(display_name)
            counter = 1
            base_id = p_id
            while any(p.id == p_id for p in latest.projects):
                p_id = f"{base_id}_{counter}"
                counter += 1

            active_groups = self._active_groups_in(latest)
            if priority_group and slot and priority_group.upper() in active_groups and 1 <= slot <= SLOTS_PER_GROUP:
                existing = next(
                    (p for p in latest.projects if p.priority_group.upper() == priority_group.upper() and p.slot == slot),
                    None,
                )
                if existing:
                    free = self._find_first_free_slot_in(latest, priority_group)
                    if not free:
                        grp, s = self._first_free_side_slot_in(latest)
                    else:
                        grp, s = free
                else:
                    grp, s = priority_group.upper(), slot
            else:
                free = self._find_first_free_slot_in(latest, priority_group)
                if not free:
                    grp, s = self._first_free_side_slot_in(latest)
                else:
                    grp, s = free

            healed_path = auto_heal_project_path(display_name, str(source_path).strip())
            new_proj = Project(
                id=p_id,
                display_name=display_name.strip(),
                source_path=healed_path.strip(),
                enabled=enabled,
                priority_group=grp,
                slot=s,
                archive_name=display_name.strip(),
                audit_project_name=(audit_project_name or display_name).strip(),
            )
            latest.projects.append(new_proj)
            return new_proj

    def move_project(self, project_id: str, target_group: str, target_slot: int) -> bool:
        grp = target_group.upper()
        if not (1 <= target_slot <= SLOTS_PER_GROUP):
            return False
        with self._mutate_latest() as tx:
            latest = tx.cfg
            proj = self._find_by_id_in(latest, project_id)
            if not proj:
                tx.skip = True
                return False
            existing = next(
                (p for p in latest.projects if p.priority_group.upper() == grp and p.slot == target_slot),
                None,
            )
            if existing and existing.id != proj.id:
                # Swap slots
                existing.priority_group = proj.priority_group
                existing.slot = proj.slot

            proj.priority_group = grp
            proj.slot = target_slot
            return True

    def move_project_step(self, project_id: str, step: int) -> bool:
        """Moves project up (step=-1) or down (step=+1) across all active slots, swapping if needed."""
        with self._mutate_latest() as tx:
            latest = tx.cfg
            proj = self._find_by_id_in(latest, project_id)
            if not proj:
                tx.skip = True
                return False

            active_groups = self._active_groups_in(latest)
            all_slots = [(g, s) for g in active_groups for s in range(1, SLOTS_PER_GROUP + 1)]
            cur_pos = (proj.priority_group.upper(), proj.slot)
            if cur_pos not in all_slots:
                tx.skip = True
                return False

            cur_idx = all_slots.index(cur_pos)
            target_idx = cur_idx + step
            if target_idx < 0 or target_idx >= len(all_slots):
                tx.skip = True
                return False

            target_group, target_slot = all_slots[target_idx]
            existing = next(
                (p for p in latest.projects if p.priority_group.upper() == target_group.upper() and p.slot == target_slot),
                None,
            )
            if existing and existing.id != proj.id:
                existing.priority_group = proj.priority_group
                existing.slot = proj.slot
            proj.priority_group = target_group.upper()
            proj.slot = target_slot
            return True

    def remove_project(self, project_id: str) -> bool:
        with self._mutate_latest() as tx:
            latest = tx.cfg
            proj = self._find_by_id_in(latest, project_id)
            if not proj:
                tx.skip = True
                return False
            latest.projects.remove(proj)
            return True

    def edit_project(self, project_id: str, editor: Callable[[Project], None]) -> bool:
        """Applies arbitrary field edits to a project inside the registry transaction."""
        with self._mutate_latest() as tx:
            proj = self._find_by_id_in(tx.cfg, project_id)
            if not proj:
                tx.skip = True
                return False
            editor(proj)
            return True

    def sync_from_audit_root(self, audit_root: Path) -> int:
        """
        Discovers project directories inside MAIN0, MAIN1, SIDE0, SIDE1, etc. under audit_root.
        Assigns found projects to corresponding groups/slots if not already registered.
        Returns count of newly registered projects.
        """
        if not audit_root.exists() or not audit_root.is_dir():
            return 0

        newly_added = 0
        active_groups = self.get_active_groups()
        for group_dir in sorted(audit_root.iterdir()):
            if not group_dir.is_dir() or group_dir.name.startswith(("_", ".")):
                continue

            group = group_dir.name.upper()
            for p_dir in sorted(group_dir.iterdir()):
                if not p_dir.is_dir() or p_dir.name.startswith(("_", ".")):
                    continue

                name = p_dir.name
                existing = self.get_project_by_name(name)
                if not existing:
                    free = self.find_first_free_slot(group)
                    if free:
                        grp, s = free
                    else:
                        grp, s = self.find_first_free_side_slot()

                    self.add_project(
                        display_name=name,
                        source_path="",
                        enabled=True,
                        priority_group=grp,
                        slot=s,
                        audit_project_name=name,
                    )
                    newly_added += 1

        return newly_added
