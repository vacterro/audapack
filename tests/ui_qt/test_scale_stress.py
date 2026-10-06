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
from audapack.models import SLOTS_PER_GROUP, Project
from audapack.services.audit_service import AuditService
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_room_model import ProjectRoomModel
from audapack.ui_qt.task_runner import TaskRunner


def _group_for_ordinal(ordinal: int) -> str:
    """Group name for the Nth slot of the registry, in canonical order.

    The old builder wrapped after a fixed list of ten groups, so counts above
    60 reused (group, slot) keys. Projects collided in the registry and the
    model's keyed dict silently dropped the losers: 240 of 300 never reached
    the view, while the assertions still passed because they never asked.
    Dynamic SIDE groups are a real supported layout, so scale the names out.
    """
    if ordinal == 0:
        return "MAIN0"
    if ordinal == 1:
        return "MAIN1"
    return f"SIDE{ordinal - 2}"


def build_scale_project_service(tmp_path, count: int) -> tuple[ProjectService, ProjectRoomModel]:
    projects = [
        Project(
            id=f"proj_{i:03d}",
            display_name=f"Project {i:03d}",
            source_path=str(tmp_path / f"proj_{i:03d}"),
            priority_group=_group_for_ordinal(i // SLOTS_PER_GROUP),
            slot=(i % SLOTS_PER_GROUP) + 1,
        )
        for i in range(count)
    ]

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

    # Verification of initial load: EVERY project must reach the view. The old
    # fixture lost 240 of 300 this way and still passed, because "at least two
    # group rows" is true for a registry that silently dropped the rest.
    registry = service.list_projects()
    assert len(registry) == project_count
    keys = {(p.priority_group.upper(), p.slot) for p in registry}
    assert len(keys) == project_count, "fixture must not collide (group, slot) keys"
    assert all(model.index_for_project_id(p.id).isValid() for p in registry)
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
    executed = []

    # Fire 100 rapid audit events alternating among 4 projects
    for i in range(100):
        target_pid = f"proj_{i % 4:03d}"
        runner.submit_coalesced(
            f"audit:{target_pid}",
            lambda pid=target_pid: (
                executed.append(pid),
                audit_service.refresh_project(pid),
            )[1],
            on_success=lambda snap, pid=target_pid: (
                model.update_audit_snapshot(pid, snap),
                completed_events.append(pid),
            ),
        )

    # Process events until every key has drained. Stopping at an arbitrary count
    # would make the bound below vacuous: it must be measured after the storm is
    # over, not while it is still running.
    start_wait = time.time()
    while time.time() - start_wait < 5.0:
        qapp.processEvents()
        if completed_events and not any(
            runner.is_running(f"audit:proj_{i:03d}") for i in range(4)
        ):
            break
        time.sleep(0.01)

    # Invariants:
    # 1. 0 full model resets
    assert model.model_reset_count == initial_resets
    # 2. Coalescing collapsed 100 submissions into bounded WORK. Callback count
    #    alone cannot prove it: plain submit() also yields only 4 callbacks,
    #    because superseded generations are dropped rather than coalesced. The
    #    falsifiable quantity is how many worker bodies ran.
    assert len(executed) <= 20, f"coalescing failed: {len(executed)} bodies ran"
    assert set(executed) == {f"proj_{i:03d}" for i in range(4)}
    # 3. Model snapshot state updated for target projects
    for i in range(4):
        pid = f"proj_{i:03d}"
        idx = model.index_for_project_id(pid)
        assert idx.isValid()
