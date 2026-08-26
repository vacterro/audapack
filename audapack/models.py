"""Data models for AUDAPACK."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class PriorityGroup(str, Enum):
    MAIN0 = "MAIN0"
    MAIN1 = "MAIN1"
    SIDE0 = "SIDE0"
    SIDE1 = "SIDE1"


CANONICAL_GROUPS = [
    PriorityGroup.MAIN0.value,
    PriorityGroup.MAIN1.value,
    PriorityGroup.SIDE0.value,
    PriorityGroup.SIDE1.value,
]

SLOTS_PER_GROUP = 6
TOTAL_SLOTS = len(CANONICAL_GROUPS) * SLOTS_PER_GROUP


class AuditTemperature(str, Enum):
    HOT = "HOT"        # 0–6h
    WARM = "WARM"      # >6–24h
    COOL = "COOL"      # >24–72h
    COLD = "COLD"      # >72h–7d
    STALE = "STALE"    # >7d
    NONE = "NONE"      # No audit found


@dataclass
class Project:
    id: str
    display_name: str
    source_path: str
    enabled: bool = True
    priority_group: str = PriorityGroup.MAIN0.value
    slot: int = 1
    archive_name: str = ""
    audit_project_name: str = ""
    last_pack_time: str = ""
    last_package_hash: str = ""
    last_copied_audit_hash: str = ""
    last_copied_at: str = ""
    last_copied_archive_path: str = ""
    last_copied_archive_at: str = ""
    ignored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source_path": self.source_path,
            "enabled": self.enabled,
            "priority_group": self.priority_group,
            "slot": self.slot,
            "archive_name": self.archive_name,
            "audit_project_name": self.audit_project_name,
            "last_pack_time": self.last_pack_time,
            "last_package_hash": self.last_package_hash,
            "last_copied_audit_hash": self.last_copied_audit_hash,
            "last_copied_at": self.last_copied_at,
            "last_copied_archive_path": self.last_copied_archive_path,
            "last_copied_archive_at": self.last_copied_archive_at,
            "ignored": self.ignored,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        return cls(
            id=str(data.get("id", "")).strip(),
            display_name=str(data.get("display_name") or data.get("name", "")).strip(),
            source_path=str(data.get("source_path") or data.get("path", "")).strip(),
            enabled=bool(data.get("enabled", True)),
            priority_group=str(data.get("priority_group") or data.get("priority", PriorityGroup.MAIN0.value)).strip().upper(),
            slot=int(data.get("slot", 1)),
            archive_name=str(data.get("archive_name", "")).strip(),
            audit_project_name=str(data.get("audit_project_name") or data.get("audit_name", "")).strip(),
            last_pack_time=str(data.get("last_pack_time", "")).strip(),
            last_package_hash=str(data.get("last_package_hash", "")).strip(),
            last_copied_audit_hash=str(data.get("last_copied_audit_hash", "")).strip(),
            last_copied_at=str(data.get("last_copied_at", "")).strip(),
            last_copied_archive_path=str(data.get("last_copied_archive_path", "")).strip(),
            last_copied_archive_at=str(data.get("last_copied_archive_at", "")).strip(),
            ignored=bool(data.get("ignored", False)),
        )


@dataclass
class AuditSnapshot:
    project_id: str
    project_name: str
    core_path: Optional[Path] = None
    core_complete: bool = False
    second_path: Optional[Path] = None
    second_complete: bool = False
    performance_path: Optional[Path] = None
    performance_complete: bool = False
    all3_path: Optional[Path] = None
    all3_ready: bool = False
    all3_sha256: str = ""
    audit_timestamp: Optional[datetime] = None
    audit_age_seconds: Optional[float] = None
    temperature: AuditTemperature = AuditTemperature.NONE
    completed_waves: int = 0
    total_tickets: int = 0
    raw_status_line: str = ""


@dataclass
class SaipenInfo:
    detected: bool = False
    root_path: Optional[Path] = None
    task: str = ""
    phase: str = ""
    next_action: str = ""
    updated: str = ""
    git_branch: str = ""
    git_head: str = ""
    git_dirty: bool = False
    git_changed_files: int = 0
    git_untracked_files: int = 0


@dataclass
class PackResult:
    project_id: str
    name: str
    source_path: str
    output_path: Optional[Path] = None
    success: bool = False
    files_added: int = 0
    raw_bytes: int = 0
    archive_bytes: int = 0
    skipped_files: int = 0
    walk_errors: int = 0
    error_message: str = ""
