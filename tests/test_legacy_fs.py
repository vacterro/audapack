"""Regression for T-13: legacy raw-named vs fs-safe sanitized artifact paths."""

from audapack.audits import AuditIndexer
from audapack.bridge.storage import sanitize_project_name
from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project


def test_fs_safe_name_resolution_finds_sanitized_dir(tmp_path):
    audits_root = tmp_path / "audits"
    audits_root.mkdir()
    raw_name = "Test:Project*?Name"
    sanitized = sanitize_project_name(raw_name)
    grp = "SIDE1"
    grp_dir = audits_root / grp
    grp_dir.mkdir(parents=True)
    sanitized_dir = grp_dir / sanitized
    sanitized_dir.mkdir()
    (sanitized_dir / f"{sanitized}__01_AUDIT_CORE.md").write_text(
        "PROJECT_NAME: Test\nWAVE: AUDIT CORE\nSTATUS: AUDIT_CORE: COMPLETE\nTICKETS: 0\nCORE_DONE_WHEN: x\n",
        encoding="utf-8",
    )
    cfg = AppConfig(audits=AuditsConfig(root=str(audits_root)), projects=[])
    proj = Project(
        id="p1",
        display_name=raw_name,
        source_path=str(tmp_path / "p1"),
        priority_group=grp,
        slot=1,
        audit_project_name=raw_name,
        archive_name=raw_name,
    )
    indexer = AuditIndexer(cfg)
    found = indexer.find_project_audit_dir(proj)
    assert found is not None
    assert found.resolve() == sanitized_dir.resolve()


def test_fs_safe_batch_index_still_finds_normal_sanitized(tmp_path):
    audits_root = tmp_path / "audits"
    audits_root.mkdir()
    for raw in ["Alpha", "Beta:Gamma", "Delta*Epsilon"]:
        san = sanitize_project_name(raw)
        d = audits_root / "MAIN0" / san
        d.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(audits=AuditsConfig(root=str(audits_root)), projects=[])
    indexer = AuditIndexer(cfg)
    for raw in ["Alpha", "Beta:Gamma", "Delta*Epsilon"]:
        san = sanitize_project_name(raw)
        proj = Project(id=san.lower(), display_name=raw, source_path="", priority_group="MAIN0", slot=1)
        found = indexer.find_project_audit_dir(proj)
        assert found is not None
        assert found.name.lower() == san.lower()
