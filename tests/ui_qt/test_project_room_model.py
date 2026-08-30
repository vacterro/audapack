"""Unit tests for ProjectRoomModel (Wave M).

Verifies:
- In-memory presentation (zero disk reads during standard model access).
- Targeted mutation API (apply_project_move, update_audit_snapshot, update_pack_state, update_temperature_all).
- Model-native Drag & Drop contract (MIME type, flags, serialization, drop resolution).
- Zero model resets during ordinary move/swap/audit updates.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QStyleOptionViewItem

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


def test_project_tree_does_not_clip_two_line_zip_rows(model_fixture, qapp):
    from audapack.ui_qt.main_window import MainWindow

    _model, service, _config, _tmp_path = model_fixture
    window = MainWindow(service)
    try:
        assert window.tree.uniformRowHeights() is False
    finally:
        window.close()


def test_compact_project_rows_use_one_line_height(model_fixture, qapp):
    from audapack.ui_qt.main_window import MainWindow

    _model, service, config, _tmp_path = model_fixture
    config.ui.compact_rows = True
    window = MainWindow(service)
    try:
        index = window.model.index_for_slot("MAIN0", 1)
        assert window.delegate.sizeHint(QStyleOptionViewItem(), index).height() == 22
    finally:
        window.close()


def test_targeted_project_move_zero_model_reset(model_fixture):
    model, service, config, tmp_path = model_fixture
    initial_resets = model.model_reset_count

    p1 = service.get_project("p1")
    updated_p1 = Project(id="p1", display_name="Project 1", source_path=p1.source_path, priority_group="MAIN0", slot=3)

    # Signal monitor for layoutChanged (used by apply_project_move for swap reliability)
    layout_changed_count = []
    model.layoutChanged.connect(lambda: layout_changed_count.append(True))

    # Apply targeted move in memory (slot 1 -> slot 3)
    model.apply_project_move("MAIN0", 1, "MAIN0", 3, updated_p1)

    # Invariant: 0 model reset!
    assert model.model_reset_count == initial_resets
    assert model.targeted_project_update_count == 1

    # Verify layoutChanged emitted (ensures reliable repaint for swaps)
    assert len(layout_changed_count) == 1

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


def test_pack_progress_run_id_gate_and_lifecycle(model_fixture):
    model, service, config, tmp_path = model_fixture

    # Start a pack run
    model.update_pack_state("p1", "PACKING")
    run_id = model.get_current_pack_run_id("p1")
    assert run_id >= 1

    # Progress with a stale run_id is ignored (previous run's worker)
    model.update_pack_progress("p1", 10, 5000, "c:/x/file.py", run_id=run_id - 1)
    s1_idx = model.index_for_slot("MAIN0", 1)
    assert model.data(s1_idx, model.ROLES["pack_progress"]) is None

    # Progress with the current run_id lands in the model
    model.update_pack_progress("p1", 10, 5000, "c:/x/file.py", run_id=run_id)
    assert model.data(s1_idx, model.ROLES["pack_progress"]) == {
        "files_added": 10,
        "bytes_written": 5000,
        "current_path": "c:/x/file.py",
    }
    pct = model.data(s1_idx, model.ROLES["pack_percent"])
    assert isinstance(pct, float) and pct > 0

    # Progress without a run_id while no run registered is a no-op
    model.update_pack_progress("p2", 1, 1, "c:/y")
    s2_idx = model.index_for_slot("MAIN0", 2)
    assert model.data(s2_idx, model.ROLES["pack_progress"]) is None

    # Completing the pack clears progress and bumps the run id
    model.update_pack_state("p1", "COMPLETE", "out.zip")
    assert model.get_current_pack_run_id("p1") == run_id + 1
    assert model.data(s1_idx, model.ROLES["pack_progress"]) is None


def test_pack_failed_invalidates_archive_freshness(model_fixture, monkeypatch):
    model, service, config, tmp_path = model_fixture
    proj = service.get_project("p1")

    # Seed the archive freshness cache so we can prove invalidation clears it.
    model._get_archive_fresh(proj)
    assert proj.id in model._archive_fresh_cache

    # A FAILED pack must drop the stale cached entry (partial zip moved to
    # .PARTIAL.*; previous archive restored) so the next paint recomputes.
    model.update_pack_state("p1", "PACKING")
    model.update_pack_state("p1", "FAILED", "Partial archive: 2 file(s) skipped")
    assert proj.id not in model._archive_fresh_cache

    # And the fresh entry is recomputed on the next read.
    model._get_archive_fresh(proj)
    assert proj.id in model._archive_fresh_cache


def test_archive_freshness_cache_ttl(model_fixture, monkeypatch):
    model, service, config, tmp_path = model_fixture
    proj = service.get_project("p1")

    # No source dir -> entry says "no archive", no source probe
    entry = model._get_archive_fresh(proj)
    assert entry["exists"] is False
    assert entry["freshness_short"] == "none"
    assert entry["source_older"] is None

    # Second read within TTL must reuse the cached entry (no recompute)
    model._archive_fresh_cache[proj.id]["computed_at"] = 0.0  # force expiry
    calls = []
    orig = model._compute_archive_fresh
    monkeypatch.setattr(model, "_compute_archive_fresh", lambda p: (calls.append(p), orig(p))[1])
    # PERF-001: a TTL-expired read serves the stale entry WITHOUT a synchronous
    # filesystem walk on the paint path; recompute is deferred to the tick.
    model._get_archive_fresh(proj)
    assert len(calls) == 0, "expired read must not recompute on the paint path"
    model._get_archive_fresh(proj)  # still cached, still stale-served
    assert len(calls) == 0

    # The temperature tick performs the deferred recompute.
    model.update_temperature_all()
    assert len(calls) == 1, "tick must recompute stale archive entries"
    model._get_archive_fresh(proj)
    assert len(calls) == 1

    # Invalidation forces recompute on next read
    model.invalidate_archive_fresh(proj.id)
    model._get_archive_fresh(proj)
    assert len(calls) == 2


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


def test_get_archive_info_uses_cached_data_no_stat(model_fixture, monkeypatch):
    """PERF-001: get_archive_info must read size/created from the precomputed
    cache entry, never call Path.stat() on the archive during paint."""
    import time
    model, service, _cfg, tmp_path = model_fixture
    proj = service.registry.get_project_by_id("p1")

    # Prime the freshness cache with a synthetic entry.
    cached = {
        "computed_at": time.time(),
        "exists": True,
        "path": tmp_path / "p1.zip",
        "mtime": 1000000.0,
        "size_str": "1.5 MB",
        "created_str": "12.08.26",
        "temperature": "COLD",
        "sync_status": "SYNCED",
        "source_mtime": None,
        "source_older": None,
        "freshness_short": "stale",
    }
    model._archive_fresh_cache[proj.id] = cached

    def fail_stat(*args, **kwargs):
        raise AssertionError("Path.stat() must not be called during get_archive_info paint path")

    monkeypatch.setattr(Path, "stat", fail_stat)
    exists, size_str, created_str, path = model.get_archive_info(proj)
    assert exists is True
    assert size_str == "1.5 MB"
    assert created_str == "12.08.26"
    assert path == tmp_path / "p1.zip"


def test_probe_source_mtime_incomplete_yields_source_older_none(model_fixture, monkeypatch):
    """PERF-001: a budget-exhausted source traversal must not set source_older,
    preventing a false 'source is older' conclusion from an incomplete scan."""

    model, service, _cfg, tmp_path = model_fixture
    proj = service.registry.get_project_by_id("p1")

    # Set output_dir so the archive finder looks in tmp_path.
    service.config.packing.output_dir = str(tmp_path)
    arc = tmp_path / "p1.zip"
    arc.write_text("fake", encoding="utf-8")

    # Over 2000 files in source so the budget cap kicks in.
    src_dir = tmp_path / "p1"
    src_dir.mkdir(parents=True, exist_ok=True)
    for i in range(2000):
        (src_dir / f"file_{i}.txt").write_text("x", encoding="utf-8")

    entry = model._compute_archive_fresh(proj)
    assert entry["exists"] is True, "archive must be found"
    # With >1000 files the scan is incomplete -> source_older must be None.
    assert entry["source_older"] is None, "incomplete scan must not claim source is older"
