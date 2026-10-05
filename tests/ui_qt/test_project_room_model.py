"""Unit tests for ProjectRoomModel (Wave M).

Verifies:
- In-memory presentation (zero disk reads during standard model access).
- Targeted mutation API (apply_project_move, update_audit_snapshot, update_pack_state, update_temperature_all).
- Model-native Drag & Drop contract (MIME type, flags, serialization, drop resolution).
- Zero model resets during ordinary move/swap/audit updates.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QStyleOptionViewItem

from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditSnapshot, AuditTemperature, Project
from audapack.services.audit_run_service import AuditRunSnapshot
from audapack.services.events import ProjectMoveResult
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_delegate import ProjectItemDelegate
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


def test_composite_run_snapshot_is_targeted_and_preserves_wave_progress(model_fixture):
    model, _service, _config, _tmp_path = model_fixture
    before = model.model_reset_count
    run = AuditRunSnapshot(
        project_id="p1", project_name="Project 1", operator_state="AUDITING",
        summary="AUDIT 1/3", dispatch_id="dsp-1", dispatch_state="AUDITING",
        completed_waves=1, total_waves=3, campaign_run_id="run-1",
    )
    model.update_audit_run_snapshot("p1", run)
    index = model.index_for_project_id("p1")
    assert model.data(index, model.ROLES["audit_run_state"]) == "AUDITING"
    assert model.data(index, model.ROLES["audit_run_summary"]) == "AUDIT 1/3"
    assert model.data(index, model.ROLES["completed_waves"]) == 1
    assert model.data(index, model.ROLES["total_waves"]) == 3
    assert model.data(index, model.ROLES["dispatch_run_id"]) == "run-1"
    assert model.model_reset_count == before

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

    # A FAILED pack must drop the stale cached entry (the staged .part.{uuid} is
    # unlinked and the previous archive restored from .bak.{uuid}) so the next
    # paint recomputes.
    model.update_pack_state("p1", "PACKING")
    model.update_pack_state("p1", "FAILED", "Partial archive: 2 file(s) skipped")
    assert proj.id not in model._archive_fresh_cache

    # And the fresh entry is recomputed on the next read.
    model._get_archive_fresh(proj)
    assert proj.id in model._archive_fresh_cache


def test_failed_pack_exact_reason_reaches_info_and_hover(model_fixture):
    model, _service, _config, _tmp_path = model_fixture
    failure = (
        "SAIPEN audit-manifest precondition failed "
        "(AUDIT_MANIFEST_STALE_REGENERATION_FAILED): manifest is stale; "
        "regeneration failed: launcher discovery found no executable"
    )

    model.update_pack_state("p1", "FAILED", failure)
    index = model.index_for_project_id("p1")
    hover = model.data(index, model.ROLES["hover_info"])

    assert hover["pack_state"] == "FAILED"
    assert hover["pack_message"] == failure
    html = ProjectItemDelegate.build_tooltip(hover)
    assert "FAILED" in html
    assert failure in html


def test_compute_uses_pack_hot_proof_before_bounded_probe(model_fixture, monkeypatch):
    import audapack.ui_qt.models.project_room_model as module

    model, service, _config, tmp_path = model_fixture
    project = service.get_project("p1")
    source = tmp_path / "p1"
    source.mkdir()
    (source / "app.py").write_text("print('x')\n", encoding="utf-8")
    archive = tmp_path / "p1.zip"
    archive.write_bytes(b"zip")
    monkeypatch.setattr(module, "find_archive_for_project", lambda *_args: archive)
    monkeypatch.setattr(module.hot_freshness, "lookup", lambda *_args: object())

    def forbidden_probe(*_args, **_kwargs):
        raise AssertionError("hot proof must avoid the bounded source probe")

    monkeypatch.setattr(module, "probe_archive_freshness", forbidden_probe)
    entry = model._compute_archive_fresh(project)
    assert entry["archive_freshness"] == "FRESH"


def test_archive_freshness_cache_ttl(model_fixture, monkeypatch):
    model, service, config, tmp_path = model_fixture
    proj = service.get_project("p1")

    # No source dir -> entry says "no archive", no source probe
    entry = model._get_archive_fresh(proj)
    assert entry["exists"] is False
    assert entry["freshness_short"] == "none"
    assert entry["archive_freshness"] == "UNKNOWN"

    # Second read within TTL must reuse the cached entry (no recompute)
    model._archive_fresh_cache[proj.id]["computed_at"] = 0.0  # force expiry
    calls = []
    orig = model._compute_archive_fresh
    monkeypatch.setattr(model, "_compute_archive_fresh", lambda p, probe_source=True: (calls.append(p), orig(p))[1])
    # PERF-001: a TTL-expired read serves the stale entry WITHOUT a synchronous
    # filesystem walk on the paint path; recompute is deferred to the tick.
    model._get_archive_fresh(proj)
    assert len(calls) == 0, "expired read must not recompute on the paint path"
    model._get_archive_fresh(proj)  # still cached, still stale-served
    assert len(calls) == 0

    # The tick no longer walks the disk itself. PERF-001 moved the bounded
    # source walk off the paint path and into this tick -- which is still the
    # GUI thread, so with a 10 s TTL against a 60 s tick every project was
    # recomputed every time and the UI froze for about a second. The tick now
    # only CLAIMS the stale ids; the caller recomputes off-thread and publishes.
    model.update_temperature_all()
    assert len(calls) == 0, "the tick must not walk the filesystem"
    assert model.take_stale_archive_projects() == [proj.id]
    assert model.take_stale_archive_projects() == [], "a claim is consumed once"
    model.apply_archive_fresh(proj.id, model.compute_archive_fresh(proj.id))
    assert len(calls) == 1
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
        "archive_freshness": "UNKNOWN",
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


def test_excluded_weight_neither_marks_stale_nor_eats_the_probe_budget(model_fixture):
    """PERF-002 (audit/9.md): the old raw os.walk stat-ed material the packer
    excludes, so a newer node_modules/cache.js reported STALE and >1,000
    excluded files could consume the whole 1,000-entry budget and prevent any
    verdict. The canonical probe prunes excluded trees before stat-ing them."""
    import os as _os
    import time as _time

    model, service, _cfg, tmp_path = model_fixture
    proj = service.registry.get_project_by_id("p1")

    service.config.packing.output_dir = str(tmp_path)
    src_dir = tmp_path / "p1"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "app.py").write_text("print(1)", encoding="utf-8")

    from audapack.services.packing_service import PackingService

    packer = PackingService(config=service.config, base_dir=service.base_dir)
    packed = packer.pack_project("p1")
    assert packed.success, packed.error_message
    arc = Path(packed.output_path)

    # 1,200 files that the packer excludes, every one newer than the archive.
    noise = src_dir / "node_modules"
    noise.mkdir()
    stamp = arc.stat().st_mtime + 600
    for i in range(1200):
        f = noise / f"cache_{i}.js"
        f.write_text("x", encoding="utf-8")
        _os.utime(f, (stamp, stamp))
    _os.utime(src_dir, (arc.stat().st_mtime - 60, arc.stat().st_mtime - 60))
    _os.utime(noise, (arc.stat().st_mtime - 60, arc.stat().st_mtime - 60))

    model._archive_fresh_cache.clear()
    entry = model._compute_archive_fresh(proj)
    assert entry["exists"] is True, "archive must be found"
    assert entry["archive_freshness"] == "FRESH", (
        "excluded material must neither mark the archive stale nor consume the budget"
    )

    # An INCLUDED file newer than the archive is real staleness.
    included = src_dir / "app.py"
    _os.utime(included, (stamp, stamp))
    model._archive_fresh_cache.clear()
    entry = model._compute_archive_fresh(proj)
    assert entry["archive_freshness"] == "STALE"
    assert _time.time() > 0


def test_archive_age_reads_the_cached_mtime_and_a_live_clock(model_fixture):
    """Freshness says fresh/stale/old; age says by how much.

    The age must not be baked into the cache entry, or it would freeze at
    whatever it was when the TTL last expired and read "10m" for an hour.
    """
    import time

    model, _service, _config, _tmp = model_fixture
    g0 = model.index(0, 0, QModelIndex())
    idx = model.index(0, 0, g0)

    model._archive_fresh_cache["p1"] = {
        "computed_at": time.time(), "exists": True, "path": Path("x.zip"),
        "mtime": time.time() - 3600 * 3, "size_str": "1 MB", "created_str": "",
        "temperature": AuditTemperature.NONE, "sync_status": "SYNCED",
        "archive_freshness": "FRESH", "freshness_short": "stale",
    }
    assert model.data(idx, model.ROLES["archive_age_str"]) == "3h"

    model._archive_fresh_cache["p1"]["mtime"] = None
    assert model.data(idx, model.ROLES["archive_age_str"]) == ""


def test_reload_snapshots_inaudit_for_every_project(model_fixture, monkeypatch):
    """The IA badge must be populated for ALL projects after a reload.

    Regression: _reload() called _refresh_inaudit_snapshot() BEFORE repopulating
    self._projects, and the refresh iterates that dict -- so the snapshot was
    built over an empty room and every project's count stayed 0. The only other
    writer is refresh_inaudit(project_id), which fills exactly one project, so
    the count appeared only for a project whose INAUDIT inbox the user opened by
    hand.
    """
    import audapack.ui_qt.models.project_room_model as module

    model, _service, _config, _tmp_path = model_fixture
    monkeypatch.setattr(module, "list_inaudit_layers", lambda proj: ["1.md", "2.md"])
    monkeypatch.setattr(module, "get_inaudit_selected", lambda proj: 1)

    model._reload()

    assert set(model._inaudit_snapshot) == {"p1", "p2", "p3"}
    for project_id in ("p1", "p2", "p3"):
        index = model.index_for_project_id(project_id)
        assert model.data(index, model.ROLES["inaudit_count"]) == 2, project_id
        assert model.data(index, model.ROLES["inaudit_selected"]) == 1, project_id


def test_data_roles_never_touch_the_filesystem(model_fixture, monkeypatch):

    """PERF-001 (audit/4.md): data() is a pure in-memory read, all roles.

    inaudit_count / inaudit_label / hover_info called list_inaudit_layers()
    live, and the delegate asks for inaudit_label on EVERY painted row -- so
    ordinary painting enumerated every project's audit/ directory. Measured at
    300 projects x six layers: 41.31 ms per label scan, 103.07 ms with the
    selected-layer read, on the GUI thread.
    """
    import audapack.ui_qt.models.project_room_model as module

    model, _service, _config, _tmp_path = model_fixture
    model._reload(initial=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("data() touched the filesystem")

    monkeypatch.setattr(module, "list_inaudit_layers", forbidden)
    monkeypatch.setattr(module, "get_inaudit_selected", forbidden)

    for g_idx in range(model.rowCount(QModelIndex())):
        group = model.index(g_idx, 0, QModelIndex())
        for slot in range(model.rowCount(group)):
            slot_index = model.index(slot, 0, group)
            if not slot_index.isValid():
                continue
            for role_name in ("inaudit_count", "inaudit_selected", "inaudit_label", "hover_info"):
                model.data(slot_index, model.ROLES[role_name])


def test_the_paint_path_never_walks_a_source_tree(model_fixture, monkeypatch):
    """PERF-001: a cache miss must not run the 0.15 s bounded walk.

    First paint of 24 populated projects used to execute up to 0.15 s of source
    stats per project, on the GUI thread, inside data().
    """
    import audapack.ui_qt.models.project_room_model as module

    model, service, _config, _tmp_path = model_fixture
    proj = service.get_project("p1")
    model._archive_fresh_cache.clear()

    def forbidden_walk(*args, **kwargs):
        raise AssertionError("the paint path walked a source tree")

    # PERF-002 (audit/9.md): the walk is the canonical freshness probe now, so
    # the paint-path guard is proven against THAT symbol.
    monkeypatch.setattr(module, "probe_archive_freshness", forbidden_walk)

    # No source dir in this fixture, so use the compute guard directly: the
    # probe_source flag must skip the walk branch entirely.
    entry = model._compute_archive_fresh(proj, probe_source=False)
    assert entry["archive_freshness"] == "UNKNOWN"


def test_an_observed_newer_included_file_is_stale_evidence_from_a_prefix(model_fixture):
    """PERF-002 (audit/9.md): a newer INCLUDED file settles STALE without the
    walk having to finish. The predecessor discarded evidence it had already
    seen whenever its 1,000-entry budget was exhausted, so >1,000-file projects
    paid the scan every TTL cycle and never reached a verdict at all."""
    import os as _os

    model, service, _cfg, tmp_path = model_fixture
    proj = service.registry.get_project_by_id("p1")
    service.config.packing.output_dir = str(tmp_path)

    src_dir = tmp_path / "p1"
    src_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1200):
        (src_dir / f"f{i}.txt").write_text("x", encoding="utf-8")

    from audapack.services.packing_service import PackingService

    packer = PackingService(config=service.config, base_dir=service.base_dir)
    packed = packer.pack_project("p1")
    assert packed.success, packed.error_message
    arc = Path(packed.output_path)

    stamp = arc.stat().st_mtime + 600
    _os.utime(src_dir / "f1199.txt", (stamp, stamp))
    _os.utime(src_dir, (arc.stat().st_mtime - 60, arc.stat().st_mtime - 60))

    model._archive_fresh_cache.clear()
    entry = model._compute_archive_fresh(proj)
    assert entry["archive_freshness"] == "STALE", "observed newer include is sufficient evidence"

def test_optimistic_move_rolls_back_from_registry_without_a_model_reset(model_fixture,
                                                                       monkeypatch):
    """T-19: the drop is optimistic, the persist can fail, and the rollback to
    the authoritative registry is itself targeted (zero model resets)."""
    model, service, _config, _tmp_path = model_fixture
    p1 = model.project_by_id("p1")
    assert p1 is not None
    before = model.model_reset_count

    # Optimistic in-memory move p1 MAIN0/1 -> SIDE0/2 (nothing persisted).
    model.apply_project_move("MAIN0", 1, "SIDE0", 2, p1)
    assert model.project_at("SIDE0", 2).id == "p1"
    assert model.project_at("MAIN0", 1) is None
    assert model.model_reset_count == before, "optimistic move must not reset"

    # The persistence worker fails, so the registry still holds the old row.
    monkeypatch.setattr(service, "move_project",
                        lambda *_a, **_k: ProjectMoveResult(project_id="p1", ok=False))

    # Lane end: reconcile from the authoritative registry (CORE-004 E4/E5).
    model.reconcile_arrangement()

    assert model.project_at("MAIN0", 1).id == "p1"
    assert model.project_at("SIDE0", 2) is None
    assert model.model_reset_count == before, "rollback must not reset"

def test_single_project_audit_event_emits_one_row_data_changed(model_fixture):
    """T-21: an audit event for ONE project must repaint ONE row, in the right
    group, and must not touch the rest of the room."""
    model, _service, _config, _tmp_path = model_fixture
    g0 = model.index(0, 0, QModelIndex())
    assert model.data(g0, Qt.ItemDataRole.DisplayRole) == "MAIN0"

    seen = []
    model.dataChanged.connect(lambda tl, br, *_: seen.append((tl, br)))
    before = model.model_reset_count

    model.update_audit_snapshot("p3", AuditSnapshot(
        project_id="p3", project_name="Project 3", completed_waves=3,
        audit_timestamp=datetime.now(), temperature=AuditTemperature.HOT))

    assert len(seen) == 1, f"expected one dataChanged, got {len(seen)}"
    top_left, bottom_right = seen[0]
    assert top_left.row() == bottom_right.row(), "dataChanged range is not a single row"
    assert top_left.parent() == bottom_right.parent(), "dataChanged spans two groups"
    assert top_left.internalId() == model._groups.index("SIDE0") + 1  # internalId is group_index + 1
    assert model.data(top_left, model.ROLES["project_id"]) == "p3"
    # The untouched MAIN0 row keeps its own identity; nothing was repainted as p3.
    assert model.data(model.index(0, 0, g0), model.ROLES["project_id"]) == "p1"
    assert model.model_reset_count == before


def test_temperature_tick_reads_nothing_from_disk(model_fixture, monkeypatch):
    """T-21: a temperature tick is pure arithmetic on stored snapshots. Any
    filesystem call under the project root during the tick fails the test."""
    model, _service, _config, tmp_path = model_fixture
    model.update_audit_snapshot("p1", AuditSnapshot(
        project_id="p1", project_name="Project 1", completed_waves=3,
        audit_timestamp=datetime.now(), temperature=AuditTemperature.HOT))

    import io

    def _forbidden(where):
        def _raise(*args, **kwargs):
            raise AssertionError(f"temperature tick touched the filesystem via {where}: {args[:1]!r}")
        return _raise

    # pathlib goes through io.open, not builtins.open, so both are sealed. The
    # seal is scoped: pytest's tmp_path cleanup must still be able to scan.
    with monkeypatch.context() as seal:
        for name in ("stat", "scandir", "listdir", "open", "walk"):
            seal.setattr(os, name, _forbidden(f"os.{name}"))
        seal.setattr(io, "open", _forbidden("io.open"))
        model.update_temperature_all(now=datetime.now() + timedelta(hours=80))

    idx = model.index_for_slot("MAIN0", 1)
    assert model.data(idx, model.ROLES["audit_temperature"]) == AuditTemperature.COLD
