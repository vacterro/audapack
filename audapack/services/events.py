"""Small framework-neutral result/event types for application services.

Plain dataclasses on purpose: no event bus, no Qt signals, no Tk variables.
UI layers translate these into their own mechanisms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProjectMoveResult:
    """Specific, targeted outcome of one move/swap operation."""

    project_id: str
    ok: bool
    old_group: str = ""
    old_slot: int = 0
    new_group: str = ""
    new_slot: int = 0
    swapped_project_id: Optional[str] = None


@dataclass(frozen=True)
class ProjectAdded:
    project_id: str
    display_name: str
    group: str
    slot: int


@dataclass(frozen=True)
class ProjectRemoved:
    project_id: str


@dataclass(frozen=True)
class ProjectUpdated:
    project_id: str


@dataclass(frozen=True)
class ProjectMoved:
    project_id: str
    new_group: str
    new_slot: int


@dataclass(frozen=True)
class AuditChanged:
    project_id: str


@dataclass(frozen=True)
class AuditCopied:
    project_id: str
    sha256: str
    length: int


@dataclass(frozen=True)
class PackStarted:
    task_id: int
    project_id: str
    display_name: str


@dataclass(frozen=True)
class PackProgress:
    task_id: int
    project_id: str
    done: int
    total: int
    current_file: str


@dataclass(frozen=True)
class PackCompleted:
    task_id: int
    project_id: str
    output_path: str
    files_added: int


@dataclass(frozen=True)
class PackFailed:
    task_id: int
    project_id: str
    error_message: str


@dataclass(frozen=True)
class BridgeStateChanged:
    healthy: bool
    detail: dict = field(default_factory=dict)
