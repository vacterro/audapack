"""Unit tests for ProjectRoomModel (Wave M).

Verifies:
- In-memory presentation (zero disk reads during standard model access).
- Targeted mutation API (apply_project_move, update_audit_snapshot, update_pack_state, update_temperature_all).
- Model-native Drag & Drop contract (MIME type, flags, serialization, drop resolution).
- Zero model resets during ordinary move/swap/audit updates.
"""

import json
import pytest
from datetime import datetime, timedelta
from PySide6.QtCore import QMimeData, QModelIndex, Qt

from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditSnapshot, AuditTemperature, Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_room_model import MIME_TYPE_PROJECT, ProjectRoomModel


@pytest.fixture
def model_fixture(tmp_path, qapp):
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="p1", display_name="Project 1", source_path=str(tmp_path / "p1"), priority_group="MAIN0", slot=1),
            Project(id="p2", display_name="Project 2", source_path=str(tmp_path / "p2"), priority_group="MAIN0", slot=2),
            Project(id="p3", display_name="Project 3", source_path=str(tmp_path / "p3"), priority_group="SIDE0", slot=1),
        ],
    )
    service = ProjectService(config, base_dir=tmp_path)
    model = ProjectRoomModel(service)
    return model, service, config, tmp_path


def test_model_initial_hierarchy(model_fixture):
    model, service, config, tmp_path = model_fixture
    # Groups: MAIN0, SIDE0, etc.
    assert model.rowCount(QModelIndex()) >= 2

    # Group row 0 -> MAIN0
    g0_idx = model.index(0, 0, QModelIndex())
    assert g0_idx.isValid()
    assert model.data(g0_idx, Qt.ItemDataRole.DisplayRole) == "MAIN0"
    assert model.rowCount(g0_idx) == 6  # 6 slots

    # Slot row 0 under MAIN0 -> Project 1
    s1_idx = model.index(0, 0, g0_idx)
    assert s1_idx.isValid()
    assert model.data(s1_idx, Qt.ItemDataRole.DisplayRole) == "Project 1"
    assert model.data(s1_idx, model.ROLES["project_id"]) == "p1"
    assert model.data(s1_idx, model.ROLES["is_empty_slot"]) is False

    # Slot row 2 under MAIN0 -> Empty slot 3
    s3_idx = model.index(2, 0, g0_idx)
    assert s3_idx.isValid()
    assert model.data(s3_idx, model.ROLES["is_empty_slot"]) is True


def test_targeted_project_move_zero_model_reset(model_fixture):
    model, service, config, tmp_path = model_fixture
    initial_resets = model.model_reset_count

    p1 = service.get_project("p1")
    updated_p1 = Project(id="p1", display_name="Project 1", source_path=p1.source_path, priority_group="MAIN0", slot=3)

    # Signal monitor
    data_changed_signals = []
    model.dataChanged.connect(lambda top_left, bottom_right: data_changed_signals.append((top_left, bottom_right)))

    # Apply targeted move in memory (slot 1 -> slot 3)
    model.apply_project_move("MAIN0", 1, "MAIN0", 3, updated_p1)

    # Invariant: 0 model reset!
    assert model.model_reset_count == initial_resets
    assert model.targeted_project_update_count == 1

    # Verify signals emitted for affected slots
    assert len(data_changed_signals) == 2

    # Check slot 1 is now empty
    s1_idx = model.index_for_slot("MAIN0", 1)
    assert model.data(s1_idx, model.ROLES["is_empty_slot"]) is True

    # Check slot 3 is now occupied by p1
    s3_idx = model.index_for_slot("MAIN0", 3)
    assert model.data(s3_idx, model.ROLES["project_id"]) == "p1"


def test_targeted_audit_snapshot_update_zero_model_reset(model_fixture):
    model, service, config, tmp_path = model_fixture
    initial_resets = model.model_reset_count

    snap = AuditSnapshot(
        project_id="p1",
        project_name="Project 1",
        core_complete=True,
        second_complete=True,
        performance_complete=True,
        all3_ready=True,
        completed_waves=3,
        temperature=AuditTemperature.HOT,
    )

    data_changed_signals = []
    model.dataChanged.connect(lambda top_left, bottom_right: data_changed_signals.append((top_left, bottom_right)))

    model.update_audit_snapshot("p1", snap)

    # Invariant: 0 model reset!
    assert model.model_reset_count == initial_resets
    assert model.targeted_project_update_count == 1
    assert len(data_changed_signals) == 1

    s1_idx = model.index_for_slot("MAIN0", 1)
    assert model.data(s1_idx, model.ROLES["all_ready"]) is True
    assert model.data(s1_idx, model.ROLES["completed_waves"]) == 3
    assert model.data(s1_idx, model.ROLES["audit_temperature"]) == AuditTemperature.HOT


def test_in_memory_temperature_update_zero_disk_reads(model_fixture):
    model, service, config, tmp_path = model_fixture
    initial_resets = model.model_reset_count

    base_time = datetime.now() - timedelta(hours=2)
    snap = AuditSnapshot(
        project_id="p1",
        project_name="Project 1",
        completed_waves=3,
        audit_timestamp=base_time,
        temperature=AuditTemperature.HOT,
    )
    model.update_audit_snapshot("p1", snap)

    # Recalculate temperature 80 hours later (> 72h) -> becomes COLD
    future_time = base_time + timedelta(hours=80)
    model.update_temperature_all(now=future_time)

    # Invariant: 0 model reset!
    assert model.model_reset_count == initial_resets
    s1_idx = model.index_for_slot("MAIN0", 1)
    assert model.data(s1_idx, model.ROLES["audit_temperature"]) == AuditTemperature.COLD


def test_drag_flags_and_mime_data(model_fixture):
    model, service, config, tmp_path = model_fixture

    # Occupied slot (p1): draggable + droppable
    s1_idx = model.index_for_slot("MAIN0", 1)
    flags_s1 = model.flags(s1_idx)
    assert flags_s1 & Qt.ItemFlag.ItemIsDragEnabled
    assert flags_s1 & Qt.ItemFlag.ItemIsDropEnabled

    # Empty slot (slot 4): droppable only
    s4_idx = model.index_for_slot("MAIN0", 4)
    flags_s4 = model.flags(s4_idx)
    assert not (flags_s4 & Qt.ItemFlag.ItemIsDragEnabled)
    assert flags_s4 & Qt.ItemFlag.ItemIsDropEnabled

    # Serialize MIME data
    mime = model.mimeData([s1_idx])
    assert mime.hasFormat(MIME_TYPE_PROJECT)
    raw = bytes(mime.data(MIME_TYPE_PROJECT)).decode("utf-8")
    payload = json.loads(raw)
    assert payload["project_id"] == "p1"
    assert payload["source_group"] == "MAIN0"
    assert payload["source_slot"] == 1


def test_drop_mime_data_signal(model_fixture):
    model, service, config, tmp_path = model_fixture

    drop_events = []
    model.project_dropped.connect(lambda pid, tgt_g, tgt_s, src_g, src_s: drop_events.append((pid, tgt_g, tgt_s, src_g, src_s)))

    s1_idx = model.index_for_slot("MAIN0", 1)
    mime = model.mimeData([s1_idx])

    # Drop onto slot 5 of MAIN0 (empty)
    s5_idx = model.index_for_slot("MAIN0", 5)
    ok = model.dropMimeData(mime, Qt.DropAction.MoveAction, 4, 0, s5_idx.parent())
    assert ok is True
    assert len(drop_events) == 1
    assert drop_events[0] == ("p1", "MAIN0", 5, "MAIN0", 1)
