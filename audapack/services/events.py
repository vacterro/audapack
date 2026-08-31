"""Small framework-neutral result type for application services.

Plain dataclass on purpose: no event bus, no Qt signals, no Tk variables.
UI layers translate this into their own mechanisms.
"""

from __future__ import annotations

from dataclasses import dataclass
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
