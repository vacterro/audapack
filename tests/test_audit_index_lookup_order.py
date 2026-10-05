"""T-22: the canonical audit path is checked first, before any root scan.

``find_project_audit_dir`` has three tiers: the canonical name inside the
project's group directory, a cached batch index, and finally a full
``iterdir()`` walk of the group and of every sibling group. Only the last tier
increments ``AUDIT_COUNTERS["directory_scans"]``, so that counter is the
falsifiable signal for "checked first".
"""

import pytest

from audapack.audits import AUDIT_COUNTERS, AuditIndexer
from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project


def _indexer(tmp_path):
    cfg = AppConfig(audits=AuditsConfig(root=str(tmp_path / "audits")))
    return AuditIndexer(cfg)


def _project(**kw):
    base = dict(id="p1", display_name="Totally Different Name", source_path=str(kw.pop("src", "")),
                priority_group="MAIN0", slot=1)
    base.update(kw)
    return Project(**base)


def test_canonical_group_path_is_found_without_any_directory_scan(tmp_path):
    indexer = _indexer(tmp_path)
    canonical = tmp_path / "audits" / "MAIN0" / "CanonicalAuditName"
    canonical.mkdir(parents=True)
    (canonical / "WAVE1.md").write_text("x", encoding="utf-8")

    proj = _project(audit_project_name="CanonicalAuditName")
    before = AUDIT_COUNTERS["directory_scans"]
    found = indexer.find_project_audit_dir(proj)

    assert found is not None and found.resolve() == canonical.resolve()
    assert AUDIT_COUNTERS["directory_scans"] == before, (
        "canonical hit fell through to an iterdir() walk")


def test_only_the_walk_route_moves_the_scan_counter(tmp_path, monkeypatch):
    """Red control for the counter above.

    The audit lives in SIDE0 while the project claims MAIN0, and the batch index
    is stubbed empty. Tier 1 (group probe) and tier 2 (index) both miss, so the
    iterdir() walk is the only route left and it must be counted.
    """
    indexer = _indexer(tmp_path)
    elsewhere = tmp_path / "audits" / "SIDE0" / "canonical audit name"
    elsewhere.mkdir(parents=True)
    (elsewhere / "WAVE1.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(indexer, "_ensure_batch_index", lambda root: {})

    proj = _project(audit_project_name="canonical audit name", priority_group="MAIN0")
    before = AUDIT_COUNTERS["directory_scans"]
    found = indexer.find_project_audit_dir(proj)

    assert found is not None and found.resolve() == elsewhere.resolve()
    assert AUDIT_COUNTERS["directory_scans"] == before + 1


def test_a_non_canonical_spelling_is_still_served_from_the_batch_index(tmp_path):
    """The middle tier: the canonical directory spelling misses, the cached batch
    index answers case-insensitively, and no walk is needed."""
    indexer = _indexer(tmp_path)
    found_dir = tmp_path / "audits" / "MAIN0" / "totally different name"
    found_dir.mkdir(parents=True)
    (found_dir / "WAVE1.md").write_text("x", encoding="utf-8")

    proj = _project(audit_project_name="Totally Different Name")
    before = AUDIT_COUNTERS["directory_scans"]
    found = indexer.find_project_audit_dir(proj)

    assert found is not None and found.name.lower() == found_dir.name.lower()
    assert AUDIT_COUNTERS["directory_scans"] == before, "index tier should not scan"


def test_repeated_lookups_do_not_rescan(tmp_path):
    indexer = _indexer(tmp_path)
    canonical = tmp_path / "audits" / "MAIN0" / "CanonicalAuditName"
    canonical.mkdir(parents=True)
    (canonical / "WAVE1.md").write_text("x", encoding="utf-8")
    proj = _project(audit_project_name="CanonicalAuditName")

    before = AUDIT_COUNTERS["directory_scans"]
    first = indexer.find_project_audit_dir(proj)
    for _ in range(5):
        assert indexer.find_project_audit_dir(proj).resolve() == first.resolve()
    assert AUDIT_COUNTERS["directory_scans"] == before


@pytest.mark.parametrize("group", ["MAIN0", "SIDE0"])
def test_group_is_part_of_the_lookup_not_a_global_scan(tmp_path, group):
    indexer = _indexer(tmp_path)
    d = tmp_path / "audits" / group / "CanonicalAuditName"
    d.mkdir(parents=True)
    before = AUDIT_COUNTERS["directory_scans"]
    proj = _project(audit_project_name="CanonicalAuditName", priority_group=group)
    assert indexer.find_project_audit_dir(proj).resolve() == d.resolve()
    assert AUDIT_COUNTERS["directory_scans"] == before
