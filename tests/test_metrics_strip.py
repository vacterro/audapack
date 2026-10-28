"""Unit and visual regression tests for audapack.ui_qt.metrics_strip."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from audapack.config import AppConfig
from audapack.fidelity import policy_fingerprint_from_config
from audapack.freshness import ArchiveFreshness
from audapack.models import Project
from audapack.packing import MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION
from audapack.runtime_identity import LOADED_PACKING_DIGEST
from audapack.ui_qt.metrics_strip import (
    MetricItem,
    MetricsStripWidget,
    evaluate_project_metrics,
)


@pytest.fixture
def base_config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.packing.output_dir = str(tmp_path / "out")
    return cfg


def _make_archive(
    zip_path: Path,
    manifest_data: dict,
    size_padding_bytes: int = 1024,
) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(MANIFEST_FILENAME, json.dumps(manifest_data, indent=2))
        if size_padding_bytes > 0:
            zf.writestr("data.bin", b"0" * size_padding_bytes)
    return zip_path


def test_no_project_or_unconfigured_evaluates_cleanly(base_config):
    snap = evaluate_project_metrics(None, base_config)
    assert snap.source.value == "N/A"
    assert snap.evidence.value == "N/A"
    assert snap.freshness.value == "N/A"
    assert snap.size.value == "N/A"
    assert snap.readiness.value == "NONE"


def test_no_archive_reports_none_and_repack(base_config, tmp_path):
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(tmp_path / "src"))
    snap = evaluate_project_metrics(proj, base_config, output_dir=tmp_path / "out")
    assert snap.source.value == "NONE"
    assert snap.evidence.value == "N/A"
    assert snap.freshness.value == "REPACK"
    assert snap.freshness.state == "warning"
    assert snap.readiness.value == "NONE"


def test_state_a_healthy_fresh_archive(base_config, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    fp = policy_fingerprint_from_config(base_config.packing, set(base_config.packing.excludes))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "policy_fingerprint": fp,
        "source_review_ready": True,
        "source_review": {
            "source_review_ready": True,
            "included_files": 10,
            "components": [],
        },
        "saipen_detected": True,
        "saipen_snapshot": {
            "detected": True,
            "required_evidence_omitted": False,
            "mandatory_declared": [".saipen/STATE.md"],
            "mandatory_included": [".saipen/STATE.md"],
            "mandatory_missing": [],
        },
        "producer": {
            "packing_implementation_sha256": LOADED_PACKING_DIGEST,
        },
        "git_dirty": False,
        "budget_bytes": 30 * 1024 * 1024,
    }
    _make_archive(out / "TestProj.zip", manifest, size_padding_bytes=2048)

    proj = Project(id="test_proj", display_name="TestProj", source_path=str(src))
    snap = evaluate_project_metrics(
        proj, base_config, output_dir=out, canonical_freshness=ArchiveFreshness.FRESH
    )

    assert snap.source.value == "100%"
    assert snap.source.state == "healthy"
    assert snap.evidence.value == "100%"
    assert snap.evidence.state == "healthy"
    assert snap.freshness.value == "FRESH"
    assert snap.freshness.state == "healthy"
    assert snap.size.state == "healthy"
    assert snap.readiness.value == "READY"
    assert snap.readiness.state == "healthy"


def test_state_b_stale_policy_mismatch(base_config, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "policy_fingerprint": "different_old_fingerprint",
        "source_review_ready": True,
        "producer": {"packing_implementation_sha256": LOADED_PACKING_DIGEST},
    }
    _make_archive(out / "TestProj.zip", manifest)
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(src))
    snap = evaluate_project_metrics(proj, base_config, output_dir=out)

    assert snap.freshness.value == "STALE"
    assert snap.freshness.state == "warning"
    assert snap.readiness.value == "PARTIAL"


def test_state_c_source_incomplete_and_blocked(base_config, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    fp = policy_fingerprint_from_config(base_config.packing, set(base_config.packing.excludes))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "policy_fingerprint": fp,
        "source_review_ready": False,
        "source_review": {
            "source_review_ready": False,
            "components": [
                {"name": "subrepo", "included": False, "reason": ""},
                {"name": "core", "included": True, "reason": ""},
            ],
        },
        "producer": {"packing_implementation_sha256": LOADED_PACKING_DIGEST},
    }
    _make_archive(out / "TestProj.zip", manifest)
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(src))
    snap = evaluate_project_metrics(proj, base_config, output_dir=out)

    assert snap.source.value == "50%"
    assert snap.source.state == "warning"
    assert snap.readiness.value == "PARTIAL"


def test_state_d_evidence_incomplete(base_config, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    fp = policy_fingerprint_from_config(base_config.packing, set(base_config.packing.excludes))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "policy_fingerprint": fp,
        "source_review_ready": True,
        "saipen_detected": True,
        "saipen_snapshot": {
            "detected": True,
            "required_evidence_omitted": True,
            "mandatory_declared": ["a", "b", "c", "d"],
            "mandatory_missing": ["c"],
        },
        "producer": {"packing_implementation_sha256": LOADED_PACKING_DIGEST},
    }
    _make_archive(out / "TestProj.zip", manifest)
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(src))
    snap = evaluate_project_metrics(proj, base_config, output_dir=out)

    assert snap.evidence.value == "75%"
    assert snap.evidence.state == "warning"
    assert snap.readiness.value == "PARTIAL"


def test_state_e_archive_over_budget(base_config, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    fp = policy_fingerprint_from_config(base_config.packing, set(base_config.packing.excludes))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "policy_fingerprint": fp,
        "source_review_ready": True,
        "budget_bytes": 1000,
        "producer": {"packing_implementation_sha256": LOADED_PACKING_DIGEST},
    }
    # Create archive larger than 1000 bytes
    _make_archive(out / "TestProj.zip", manifest, size_padding_bytes=5000)
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(src))
    snap = evaluate_project_metrics(proj, base_config, output_dir=out)

    assert snap.size.state == "warning"


def test_state_f_active_pack_all_and_packing(base_config, tmp_path):
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(tmp_path / "src"))
    snap = evaluate_project_metrics(
        proj, base_config, is_packing=True, is_batch_busy=True, current_pack_state=("PACKING", "in progress")
    )
    assert snap.readiness.value == "PACKING"
    assert snap.readiness.state == "active"
    assert snap.activity.value == "BATCH"
    assert snap.activity.state == "active"


def test_state_g_packing_failure(base_config, tmp_path):
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(tmp_path / "src"))
    snap = evaluate_project_metrics(
        proj, base_config, current_pack_state=("FAILED", "git error")
    )
    assert snap.readiness.value == "ERROR"
    assert snap.readiness.state == "danger"


def test_state_h_legacy_archive_no_false_green(base_config, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    # Legacy archive without producer block or source review
    manifest = {
        "schema_version": 1,
        "product": "AUDAPACK",
    }
    _make_archive(out / "TestProj.zip", manifest)
    proj = Project(id="test_proj", display_name="TestProj", source_path=str(src))
    snap = evaluate_project_metrics(proj, base_config, output_dir=out)

    assert snap.source.value == "?"
    assert snap.source.state != "healthy"
    assert snap.freshness.value == "LEGACY"
    assert snap.freshness.state == "warning"
    assert snap.readiness.value != "READY"


def test_widget_renders_snapshot_without_error(qtbot, base_config):
    widget = MetricsStripWidget()
    qtbot.addWidget(widget)
    snap = evaluate_project_metrics(None, base_config)
    widget.update_snapshot(snap)
    assert widget.cell_source.item.value == "N/A"
    widget.set_compact(True)
    widget.set_compact(False)


def test_main_window_metrics_strip_integration(qtbot, tmp_path, base_config):
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    src = tmp_path / "src"
    src.mkdir()
    p = Project(id="p1", display_name="P1", source_path=str(src), priority_group="MAIN0", slot=1)
    base_config.projects = [p]
    svc = ProjectService(base_config, base_dir=tmp_path)
    win = MainWindow(svc)
    qtbot.addWidget(win)

    # 1. Metrics strip exists on MainWindow
    assert hasattr(win, "metrics_strip")
    assert hasattr(win, "_metrics_toolbar")
    assert not win._metrics_toolbar.isHidden()

    # 2. Toggle show_metrics_strip
    base_config.ui.show_metrics_strip = False
    win._apply_toolbar_visibility()
    assert win._metrics_toolbar.isHidden()

    base_config.ui.show_metrics_strip = True
    win._apply_toolbar_visibility()
    assert not win._metrics_toolbar.isHidden()

    # 3. Compact mode
    base_config.ui.compact_metrics = True
    win._refresh_metrics()
    assert win.metrics_strip.height() == 24

    base_config.ui.compact_metrics = False
    win._refresh_metrics()
    assert win.metrics_strip.height() == 26
    win.close()


def test_tooltips_concise(tmp_path, base_config):
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    svc = ProjectService(base_config, base_dir=tmp_path)
    win = MainWindow(svc)

    for key, act in win.toolbar_actions.items():
        tip = act.toolTip()
        assert tip, f"Action {key} has no tooltip"
        # Tooltip must be concise (max 80 chars, no multiple paragraphs)
        assert len(tip) <= 80, f"Tooltip for {key} too long: {tip}"
        assert "\n" not in tip, f"Tooltip for {key} contains newlines: {tip}"
    win.close()


def test_main_window_pack_batch_metric_integration(qtbot, tmp_path, base_config):
    """Prove MainWindow._refresh_metrics derives activity from real _pack_batch_runner."""
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    src = tmp_path / "src"
    src.mkdir()
    p = Project(id="p1", display_name="P1", source_path=str(src), priority_group="MAIN0", slot=1)
    base_config.projects = [p]
    svc = ProjectService(base_config, base_dir=tmp_path)
    win = MainWindow(svc)
    qtbot.addWidget(win)

    # 1. Initial idle state
    win._refresh_metrics()
    assert win.metrics_strip.cell_activity.item.value == "IDLE"
    assert win.metrics_strip.cell_activity.item.state == "neutral"

    # 2. Start batch via the canonical runner
    def _dummy_job(pid):
        import time
        time.sleep(0.05)
        return True

    batch = win._pack_batch_runner.start_batch(
        [p],
        label="TEST BATCH",
        job_fn=_dummy_job,
    )
    assert batch is not None
    assert win._pack_batch_runner.busy()

    # Refresh metrics while runner is busy: must report BATCH
    win._refresh_metrics()
    assert win.metrics_strip.cell_activity.item.value == "BATCH"
    assert win.metrics_strip.cell_activity.item.state == "active"

    # 3. Wait for batch completion and verify metric returns to IDLE
    qtbot.waitUntil(lambda: not win._pack_batch_runner.busy(), timeout=3000)
    assert not win._pack_batch_runner.busy()
    win._refresh_metrics()
    assert win.metrics_strip.cell_activity.item.value == "IDLE"
    assert win.metrics_strip.cell_activity.item.state == "neutral"
    win.close()


def test_nested_file_modification_not_false_green(tmp_path, base_config):
    """Prove modifying a nested source file cannot false-green FRESH."""
    import os
    import time

    from audapack.freshness import probe_archive_freshness

    src = tmp_path / "src"
    nested = src / "pkg" / "nested"
    nested.mkdir(parents=True)
    f = nested / "module.py"
    f.write_text("INITIAL_CONTENT = 1\n", encoding="utf-8")

    out = tmp_path / "out"
    out.mkdir()
    fp = policy_fingerprint_from_config(base_config.packing, set(base_config.packing.excludes or []))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "policy_fingerprint": fp,
        "source_review_ready": True,
        "producer": {"packing_implementation_sha256": LOADED_PACKING_DIGEST},
        "git_dirty": False,
        "budget_bytes": 30 * 1024 * 1024,
    }
    archive_path = out / "NestedProj.zip"
    _make_archive(archive_path, manifest, size_padding_bytes=1024)

    # Ensure archive mtime >= src root mtime
    arc_mtime = time.time() + 10.0
    os.utime(archive_path, (arc_mtime, arc_mtime))

    proj = Project(id="nested_proj", display_name="NestedProj", source_path=str(src))

    # Before modifying nested file, with canonical FRESH, reports FRESH
    snap_fresh = evaluate_project_metrics(
        proj, base_config, output_dir=out, canonical_freshness=ArchiveFreshness.FRESH
    )
    assert snap_fresh.freshness.value == "FRESH"
    assert snap_fresh.freshness.state == "healthy"

    # Now modify nested file in place: its mtime is newer than archive,
    # but root directory `src` mtime does NOT change.
    new_mtime = arc_mtime + 10.0
    f.write_text("MODIFIED_CONTENT = 2\n", encoding="utf-8")
    os.utime(f, (new_mtime, new_mtime))

    # Verify root mtime is still older than archive mtime
    assert src.stat().st_mtime < archive_path.stat().st_mtime

    # Under canonical freshness authority, probe reports STALE
    verdict = probe_archive_freshness(proj, base_config.packing, output_dir=out)
    assert verdict.state is ArchiveFreshness.STALE

    # Metrics strip with canonical STALE verdict:
    snap_stale = evaluate_project_metrics(
        proj, base_config, output_dir=out, canonical_freshness=verdict.state
    )
    assert snap_stale.freshness.value == "STALE"
    assert snap_stale.freshness.state == "warning"
    assert snap_stale.readiness.value == "PARTIAL"

    # When canonical verdict is UNKNOWN: never reports FRESH/READY
    snap_unknown = evaluate_project_metrics(
        proj, base_config, output_dir=out, canonical_freshness=ArchiveFreshness.UNKNOWN
    )
    assert snap_unknown.freshness.value == "UNKNOWN"
    assert snap_unknown.freshness.state == "warning"
    assert snap_unknown.readiness.value != "READY"

    # When canonical verdict is None and no hot proof: defaults to UNKNOWN, never FRESH
    snap_none = evaluate_project_metrics(proj, base_config, output_dir=out)
    assert snap_none.freshness.value == "UNKNOWN"
    assert snap_none.readiness.value != "READY"


def test_metric_cell_tooltips_settings_and_concise(qtbot, tmp_path, base_config):
    """Prove metric cell tooltips obey show_tooltips, tooltip_duration_ms, and <=80 char constraint."""
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    src = tmp_path / "src"
    src.mkdir()
    p = Project(id="p1", display_name="P1", source_path=str(src), priority_group="MAIN0", slot=1)
    base_config.projects = [p]
    svc = ProjectService(base_config, base_dir=tmp_path)
    win = MainWindow(svc)
    qtbot.addWidget(win)

    # 1. show_tooltips = False: all cell tooltips must be empty
    base_config.ui.show_tooltips = False
    win._refresh_metrics()
    for cell in (
        win.metrics_strip.cell_source,
        win.metrics_strip.cell_evidence,
        win.metrics_strip.cell_fresh,
        win.metrics_strip.cell_size,
        win.metrics_strip.cell_ready,
        win.metrics_strip.cell_activity,
    ):
        assert cell.toolTip() == "", f"Expected empty tooltip for {cell.item.label}"

    # 2. show_tooltips = True: tooltips present, concise (<= 80 chars, no \n), duration applied
    base_config.ui.show_tooltips = True
    base_config.ui.tooltip_duration_ms = 8500
    win._refresh_metrics()
    for cell in (
        win.metrics_strip.cell_source,
        win.metrics_strip.cell_evidence,
        win.metrics_strip.cell_fresh,
        win.metrics_strip.cell_size,
        win.metrics_strip.cell_ready,
        win.metrics_strip.cell_activity,
    ):
        tip = cell.toolTip()
        assert tip != "", f"Expected tooltip for {cell.item.label}"
        assert len(tip) <= 80, f"Tooltip exceeds 80 chars ({len(tip)}): {tip}"
        assert "\n" not in tip, f"Tooltip contains newline: {tip}"
        assert cell.toolTipDuration() == 8500

    # 3. Direct truncation on MetricCellWidget with an oversized tooltip
    long_tip = "This is an extremely long tooltip description that exceeds the eighty character limit by quite a lot."
    cell = win.metrics_strip.cell_fresh
    cell.set_metric(MetricItem("FRESH", "STALE", tooltip=long_tip))
    assert len(cell.toolTip()) <= 80
    assert cell.toolTip().endswith("...")
    win.close()

