"""Visual acceptance suite for AUDAPACK metrics strip states A through H."""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

from PySide6.QtWidgets import QApplication

from audapack.config import AppConfig
from audapack.fidelity import policy_fingerprint_from_config
from audapack.models import Project
from audapack.packing import MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION
from audapack.runtime_identity import LOADED_PACKING_DIGEST
from audapack.services.project_service import ProjectService
from audapack.ui_qt.main_window import MainWindow
from audapack.ui_qt.metrics_strip import evaluate_project_metrics


def _make_archive(zip_path: Path, manifest: dict, padding: int = 1024) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(MANIFEST_FILENAME, json.dumps(manifest, indent=2))
        if padding > 0:
            zf.writestr("data.bin", b"0" * padding)


def run_visual_acceptance(output_dir: Path) -> dict[str, dict]:
    app = QApplication.instance() or QApplication(sys.argv)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src_root = root / "sources"
        arc_root = root / "archives"
        src_root.mkdir()
        arc_root.mkdir()

        cfg = AppConfig()
        cfg.packing.output_dir = str(arc_root)
        cfg.packing.fidelity_max_mb = 1  # 1MB budget for test
        fp = policy_fingerprint_from_config(cfg.packing, set(cfg.packing.excludes))

        results = {}

        # ---------------- State A: Healthy Fresh Archive ----------------
        src_a = src_root / "proj_a"
        src_a.mkdir()
        (src_a / "main.py").write_text("print('hello')", encoding="utf-8")
        manifest_a = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "product": "AUDAPACK",
            "policy_fingerprint": fp,
            "source_review_ready": True,
            "source_review": {"source_files_total": 10, "source_files_included": 10},
            "saipen_evidence": {"mandatory_declared": ["a", "b"], "mandatory_missing": []},
            "producer": {"packing_implementation_sha256": LOADED_PACKING_DIGEST},
            "budget_bytes": 1024 * 1024,
        }
        _make_archive(arc_root / "proj_a.zip", manifest_a, padding=10000)
        proj_a = Project(id="proj_a", display_name="ProjA_Healthy", source_path=str(src_a))
        snap_a = evaluate_project_metrics(proj_a, cfg, output_dir=arc_root, canonical_freshness="FRESH")
        results["State A (Healthy)"] = {
            "source": f"{snap_a.source.label} {snap_a.source.value} ({snap_a.source.state})",
            "evidence": f"{snap_a.evidence.label} {snap_a.evidence.value} ({snap_a.evidence.state})",
            "freshness": f"{snap_a.freshness.label} {snap_a.freshness.value} ({snap_a.freshness.state})",
            "size": f"{snap_a.size.label} {snap_a.size.value} ({snap_a.size.state})",
            "readiness": f"{snap_a.readiness.label} {snap_a.readiness.value} ({snap_a.readiness.state})",
            "activity": f"{snap_a.activity.label} {snap_a.activity.value} ({snap_a.activity.state})",
        }

        # ---------------- State B: Stale Archive ----------------
        src_b = src_root / "proj_b"
        src_b.mkdir()
        (src_b / "main.py").write_text("print('hello')", encoding="utf-8")
        manifest_b = dict(manifest_a)
        manifest_b["producer"] = {"packing_implementation_sha256": "stale_hash_00000000"}
        _make_archive(arc_root / "proj_b.zip", manifest_b, padding=10000)
        proj_b = Project(id="proj_b", display_name="ProjB_Stale", source_path=str(src_b))
        snap_b = evaluate_project_metrics(proj_b, cfg, output_dir=arc_root)
        results["State B (Stale)"] = {
            "freshness": f"{snap_b.freshness.label} {snap_b.freshness.value} ({snap_b.freshness.state})",
            "readiness": f"{snap_b.readiness.label} {snap_b.readiness.value} ({snap_b.readiness.state})",
        }

        # ---------------- State C: Source Incomplete ----------------
        src_c = src_root / "proj_c"
        src_c.mkdir()
        manifest_c = dict(manifest_a)
        manifest_c["source_review_ready"] = False
        manifest_c["source_review"] = {"source_files_total": 10, "source_files_included": 5}
        _make_archive(arc_root / "proj_c.zip", manifest_c, padding=10000)
        proj_c = Project(id="proj_c", display_name="ProjC_SrcIncomplete", source_path=str(src_c))
        snap_c = evaluate_project_metrics(proj_c, cfg, output_dir=arc_root)
        results["State C (Source Incomplete)"] = {
            "source": f"{snap_c.source.label} {snap_c.source.value} ({snap_c.source.state})",
            "readiness": f"{snap_c.readiness.label} {snap_c.readiness.value} ({snap_c.readiness.state})",
        }

        # ---------------- State D: Evidence Incomplete ----------------
        src_d = src_root / "proj_d"
        src_d.mkdir()
        manifest_d = dict(manifest_a)
        manifest_d["saipen_evidence"] = {
            "mandatory_declared": ["ev1", "ev2", "ev3", "ev4"],
            "mandatory_missing": ["ev4"],
        }
        _make_archive(arc_root / "proj_d.zip", manifest_d, padding=10000)
        proj_d = Project(id="proj_d", display_name="ProjD_EvidIncomplete", source_path=str(src_d))
        snap_d = evaluate_project_metrics(proj_d, cfg, output_dir=arc_root)
        results["State D (Evidence Incomplete)"] = {
            "evidence": f"{snap_d.evidence.label} {snap_d.evidence.value} ({snap_d.evidence.state})",
            "readiness": f"{snap_d.readiness.label} {snap_d.readiness.value} ({snap_d.readiness.state})",
        }

        # ---------------- State E: Archive Over Soft Budget ----------------
        src_e = src_root / "proj_e"
        src_e.mkdir()
        manifest_e = dict(manifest_a)
        manifest_e["budget_bytes"] = 1000  # Budget 1KB
        _make_archive(arc_root / "proj_e.zip", manifest_e, padding=5000)  # Actual 5KB
        proj_e = Project(id="proj_e", display_name="ProjE_OverBudget", source_path=str(src_e))
        snap_e = evaluate_project_metrics(proj_e, cfg, output_dir=arc_root)
        results["State E (Over Budget)"] = {
            "size": f"{snap_e.size.label} {snap_e.size.value} ({snap_e.size.state})",
            "readiness": f"{snap_e.readiness.label} {snap_e.readiness.value} ({snap_e.readiness.state})",
        }

        # ---------------- State F: Active Pack All ----------------
        proj_f = Project(id="proj_f", display_name="ProjF_ActiveBatch", source_path=str(src_a))
        snap_f = evaluate_project_metrics(
            proj_f, cfg, is_packing=True, is_batch_busy=True, current_pack_state=("PACKING", "batch active")
        )
        results["State F (Active Pack All)"] = {
            "readiness": f"{snap_f.readiness.label} {snap_f.readiness.value} ({snap_f.readiness.state})",
            "activity": f"{snap_f.activity.label} {snap_f.activity.value} ({snap_f.activity.state})",
        }

        # ---------------- State G: Packing Failure ----------------
        proj_g = Project(id="proj_g", display_name="ProjG_Failed", source_path=str(src_a))
        snap_g = evaluate_project_metrics(
            proj_g, cfg, current_pack_state=("FAILED", "fatal: not a git repo")
        )
        results["State G (Packing Failure)"] = {
            "readiness": f"{snap_g.readiness.label} {snap_g.readiness.value} ({snap_g.readiness.state})",
        }

        # ---------------- State H: Legacy Archive ----------------
        src_h = src_root / "proj_h"
        src_h.mkdir()
        manifest_h = {"schema_version": 1, "product": "AUDAPACK"}
        _make_archive(arc_root / "proj_h.zip", manifest_h, padding=2000)
        proj_h = Project(id="proj_h", display_name="ProjH_Legacy", source_path=str(src_h))
        snap_h = evaluate_project_metrics(proj_h, cfg, output_dir=arc_root)
        results["State H (Legacy Archive)"] = {
            "source": f"{snap_h.source.label} {snap_h.source.value} ({snap_h.source.state})",
            "freshness": f"{snap_h.freshness.label} {snap_h.freshness.value} ({snap_h.freshness.state})",
            "readiness": f"{snap_h.readiness.label} {snap_h.readiness.value} ({snap_h.readiness.state})",
        }

        # Now launch live MainWindow with proj_a to verify visual rendering and capture screenshots
        cfg.projects = [proj_a, proj_b, proj_c, proj_d, proj_e, proj_h]
        svc = ProjectService(cfg, base_dir=root)
        win = MainWindow(svc)
        win.resize(760, 680)
        win.show()

        # Let Qt process layout and render
        app.processEvents()

        # Capture live window and strip screenshots
        shot_main = win.grab()
        shot_main.save(str(output_dir / "live_acceptance_main_window.png"))

        strip_pix = win.metrics_strip.grab()
        strip_pix.save(str(output_dir / "live_acceptance_metrics_strip.png"))

        win.close()
        return results


if __name__ == "__main__":
    out_dir = Path("tests/screenshots_metrics_acceptance")
    res = run_visual_acceptance(out_dir)
    print(json.dumps(res, indent=2))
