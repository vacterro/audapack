"""Framework-neutral application services.

Services own application-level operations (move/copy/pack/bridge control) so
UI frameworks (Tkinter today, Qt tomorrow) only present state and forward user
intent. Hard rule: nothing under ``audapack/services`` may import a GUI
framework.
"""

from audapack.services.app_controller import AppController
from audapack.services.audit_service import AuditService
from audapack.services.bridge_service import BridgeService
from audapack.services.events import (
    AuditChanged,
    AuditCopied,
    BridgeStateChanged,
    PackCompleted,
    PackFailed,
    PackProgress,
    PackStarted,
    ProjectAdded,
    ProjectMoved,
    ProjectMoveResult,
    ProjectRemoved,
    ProjectUpdated,
)
from audapack.services.packing_service import PackingService
from audapack.services.project_service import ProjectService

__all__ = [
    "AppController",
    "AuditService",
    "BridgeService",
    "PackingService",
    "ProjectService",
    "ProjectMoveResult",
    "ProjectMoved",
    "ProjectAdded",
    "ProjectRemoved",
    "ProjectUpdated",
    "AuditChanged",
    "AuditCopied",
    "PackStarted",
    "PackProgress",
    "PackCompleted",
    "PackFailed",
    "BridgeStateChanged",
]
