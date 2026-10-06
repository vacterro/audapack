"""CORE-002: cross-process registry mutations must become observable.

A writer (for example the Bridge auto-registering a project) commits to
canonical config, but a long-lived reader registry keeps the AppConfig snapshot
it was constructed with. One explicit in-place refresh primitive is the only
path that pulls the latest project collection into the shared AppConfig, so the
running GUI, ProjectService and AuditService all observe it without a restart.
"""
from __future__ import annotations

from audapack.config import AppConfig, load_config, save_config
from audapack.projects import ProjectRegistry
from audapack.services.audit_service import AuditService
from audapack.services.project_service import ProjectService


def _base(tmp_path):
    cfg = AppConfig()
    cfg.projects = []
    save_config(cfg, tmp_path)
    return tmp_path


def _writer(base):
    return ProjectRegistry(load_config(base), base_dir=base, transactional=True)


def test_reader_is_stale_until_explicit_refresh(tmp_path):
    base = _base(tmp_path)
    svc = ProjectService(base_dir=base)
    assert svc.list_projects() == []

    new, created = _writer(base).resolve_or_register_project("BridgeAdded")
    assert created
    assert any(p.id == new.id for p in load_config(base).projects)

    # The long-lived reader is still on its constructor snapshot.
    assert svc.list_projects() == []
    assert svc.get_project(new.id) is None

    assert svc.refresh_projects() is True

    assert [p.id for p in svc.list_projects()] == [new.id]
    got = svc.get_project(new.id)
    assert got is not None and got.slot == new.slot


def test_refresh_preserves_shared_appconfig_identity(tmp_path):
    base = _base(tmp_path)
    svc = ProjectService(base_dir=base)
    shared = svc.config
    _writer(base).resolve_or_register_project("P2")

    svc.refresh_projects()

    assert svc.config is shared, "refreshing replaced the AppConfig object identity"
    assert svc.registry.config is shared


def test_move_and_remove_visible_after_refresh(tmp_path):
    base = _base(tmp_path)
    svc = ProjectService(base_dir=base)
    p = svc.add_project("Movable", "", priority_group="MAIN0", slot=1)

    writer = _writer(base)
    assert writer.move_project(p.id, "SIDE1", 3) is True
    assert svc.get_project(p.id).priority_group.upper() == "MAIN0", "reader was not stale"

    svc.refresh_projects()
    moved = svc.get_project(p.id)
    assert moved.priority_group.upper() == "SIDE1" and moved.slot == 3

    assert writer.remove_project(p.id) is True
    svc.refresh_projects()
    assert svc.get_project(p.id) is None


def test_audit_service_shares_the_refreshed_collection(tmp_path):
    base = _base(tmp_path)
    svc = ProjectService(base_dir=base)
    audit = AuditService(svc.config, base_dir=base)
    new, _ = _writer(base).resolve_or_register_project("AuditVisible")

    assert audit.registry.get_project_by_id(new.id) is None, "audit service should start stale"

    svc.refresh_projects()

    got = audit.registry.get_project_by_id(new.id)
    assert got is not None and got.slot == new.slot
