"""Application controller — composes services, holds single config."""

from __future__ import annotations

from typing import Optional

from audapack.config import AppConfig, load_config
from audapack.services.audit_service import AuditService
from audapack.services.bridge_service import BridgeService
from audapack.services.packing_service import PackingService
from audapack.services.project_service import ProjectService


class AppController:
    def __init__(self, config: Optional[AppConfig] = None, base_dir=None):
        self.base_dir = base_dir
        self.config = config or load_config(base_dir)
        self.projects = ProjectService(self.config, base_dir=base_dir)
        self.audits = AuditService(self.config, base_dir=base_dir)
        self.packing = PackingService(self.config, base_dir=base_dir)
        self.bridge = BridgeService(self.config)
