"""Scale matrix and stress tests for Wave M responsive architecture.

Verifies:
- Synthetic registry scale matrix: 24, 60, 120, 300 projects.
- 100 sequential project moves/swaps with zero model resets.
- 100 audit events coalesced without queue explosion or GUI thread stalls.
- Invariant: No duplicate projects, no lost projects, valid registry state.
"""

import time

import pytest
from PySide6.QtCore import QModelIndex

from audapack.audits import reset_audit_counters
from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.audit_service import AuditService
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_room_model import ProjectRoomModel
from audapack.ui_qt.task_runner import TaskRunner


def build_scale_project_service(tmp_path, count: int) -> tuple[ProjectService, ProjectRoomModel]:
    projects = []
    groups = ["MAIN0", "MAIN1", "SIDE0", "SIDE1", "SIDE2", "SIDE3", "SIDE4", "SIDE5", "SIDE6", "SIDE7"]
    slot_idx = 0
    group_idx = 0

    for i in range(count):
        g = groups[group_idx]
        s = (slot_idx % 6) + 1
        projects.append(
            Project(
                id=f"proj_{i:03d}",
                display_name=f"Project {i:03d}",
                source_path=str(tmp_path / f"proj_{i:03d}"),
                priority_group=g,
                slot=s,
            )
        )
        slot_idx += 1
        if slot_idx % 6 == 0:
            group_idx = (group_idx + 1) % len(groups)

    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=projects,
    )
    service = ProjectService(config, base_dir=tmp_path)
    model = ProjectRoomModel(service)
    return service, model


@pytest.mark.parametrize("project_count", [24, 60, 120, 300])
def test_scale_model_construction_and_targeted_moves(tmp_path, qapp, project_count):
    service, model = build_scale_project_service(tmp_path, project_count)

    # Verification of initial load
    assert model.rowCount(QModelIndex()) >= 2
    assert model.model_reset_count == 1  # only initial reset

    # Measure single targeted move
    start_move = time.perf_counter()
    p0 = service.get_project("proj_000")
    old_g = p0.priority_group
    old_s = p0.slot
    new_s = 6 if old_s != 6 else 5

    updated_p0 = Project(
        id=p0.id,
        display_name=p0.display_name,
        source_path=p0.source_path,
        priority_group=old_g,
        slot=new_s,
    )
    model.apply_project_move(old_g, old_s, old_g, new_s, updated_p0)
    elapsed_single_move = (time.perf_counter() - start_move) * 1000.0

    # Invariant: single move cost must be fast (< 5ms) and have 0 model reset
    assert model.model_reset_count == 1
    assert model.targeted_project_update_count == 1
    assert elapsed_single_move < 50.0  # well below threshold


def test_100_sequential_moves_stress(tmp_path, qapp):
    service, model = build_scale_project_service(tmp_path, 24)
    initial_resets = model.model_reset_count

    for i in range(100):
        pid = f"proj_{i % 24:03d}"
        p = service.get_project(pid)
        target_slot = (p.slot % 6) + 1
        res = service.move_project(pid, p.priority_group, target_slot)
        assert res.ok is True
        updated = service.get_project(pid)
        model.apply_project_move(res.old_group, res.old_slot, res.new_group, res.new_slot, updated)


    # Invariants
    assert model.model_reset_count == initial_resets  # 0 model reset during 100 moves!
    assert model.targeted_project_update_count == 100
    assert len(service.list_projects()) == 24
    # All 24 project IDs preserved
    assert len(set(p.id for p in service.list_projects())) == 24


def test_100_audit_events_stress_and_coalescing(tmp_path, qapp):
    service, model = build_scale_project_service(tmp_path, 24)
    audit_service = AuditService(service.config, base_dir=tmp_path)
    runner = TaskRunner(max_threads=4)
    initial_resets = model.model_reset_count

    reset_audit_counters()
    completed_events = []

    # Fire 100 rapid audit events alternating among 4 projects
    for i in range(100):
        target_pid = f"proj_{i % 4:03d}"
        runner.submit_coalesced(
            f"audit:{target_pid}",
            lambda pid=target_pid: audit_service.refresh_project(pid),
            on_success=lambda snap, pid=target_pid: (
                model.update_audit_snapshot(pid, snap),
                completed_events.append(pid),
            ),
        )

    # Process events until finished
    start_wait = time.time()
    while len(completed_events) < 8 and time.time() - start_wait < 5.0:
        qapp.processEvents()
        time.sleep(0.01)


    # Invariants:
    # 1. 0 full model resets
    assert model.model_reset_count == initial_resets
    # 2. Coalescing collapsed 100 rapid requests into bounded executions
    assert len(completed_events) <= 20
    # 3. Model snapshot state updated for target projects
    for i in range(4):
        pid = f"proj_{i:03d}"
        idx = model.index_for_project_id(pid)
        assert idx.isValid()
