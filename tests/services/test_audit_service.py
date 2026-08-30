"""AuditService — targeted refresh, copy via service."""

import shutil
import tempfile
from pathlib import Path

from audapack.config import AppConfig, save_config
from audapack.services.audit_service import AuditService


def test_refresh_and_copy():
    base = Path(tempfile.mkdtemp())
    audit_root = base / "AUDITING_IMPLEMENTATION"
    audit_root.mkdir()
    try:
        cfg = AppConfig()
        cfg.audits.root = str(audit_root)
        cfg.projects = []
        save_config(cfg, base)
        svc = AuditService(config=cfg, base_dir=base)
        # create project via its registry
        from audapack.projects import ProjectRegistry
        reg = ProjectRegistry(cfg, base_dir=base, transactional=True)
        proj, _ = reg.resolve_or_register_project("ProbeAudit")
        # no audit files yet -> snapshot empty
        snap = svc.get_snapshot(proj.id)
        assert snap is not None
        all_snaps = svc.refresh_all()
        assert proj.id in all_snaps
        ok, content, sha = svc.copy_all3(proj.id)
        assert not ok  # no ALL_3 yet
    finally:
        shutil.rmtree(base, ignore_errors=True)
