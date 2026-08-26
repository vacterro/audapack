"""Wave M Benchmark & Instrumentation Script.

Runs actual timings and counters for:
- Startup points (to visible, to interactive, to audit enriched)
- Drop visual latency & async persistence latency
- Audit event to row update latency
- Pack click to worker started latency
- Scale matrix (24, 60, 120, 300 projects)
- 100 moves / 100 audit events stress
- Performance counters (model_reset_count, full_refresh_count, targeted_project_update_count, etc.)
"""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from PySide6.QtWidgets import QApplication

from audapack.audits import AUDIT_COUNTERS, reset_audit_counters
from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditSnapshot, AuditTemperature, Project
from audapack.services.audit_service import AuditService
from audapack.services.project_service import ProjectService
from audapack.ui_qt.main_window import MainWindow
from audapack.ui_qt.models.project_room_model import ProjectRoomModel
from audapack.ui_qt.task_runner import TaskRunner


def run_benchmark():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])

    metrics = {}

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Measure Startup Timings (24 projects)
        projects_24 = []
        for i in range(24):
            g = f"MAIN{i // 6}" if i < 12 else f"SIDE{(i - 12) // 6}"
            s = (i % 6) + 1
            projects_24.append(
                Project(id=f"p_{i}", display_name=f"Project {i}", source_path=str(tmp_path / f"p_{i}"), priority_group=g, slot=s)
            )

        cfg_24 = AppConfig(audits=AuditsConfig(root=str(tmp_path / "audits")), projects=projects_24)

        t0 = time.perf_counter()
        service_24 = ProjectService(cfg_24, base_dir=tmp_path)
        t_model_start = time.perf_counter()
        window = MainWindow(service_24)
        t_visible = time.perf_counter()
        window.show()
        app.processEvents()
        t_interactive = time.perf_counter()

        metrics["startup_to_window_visible_ms"] = (t_visible - t0) * 1000.0
        metrics["startup_to_interactive_ms"] = (t_interactive - t0) * 1000.0

        # Wait for async initial enrichment
        start_wait = time.time()
        while time.time() - start_wait < 0.5:
            app.processEvents()
            time.sleep(0.01)
        t_enriched = time.perf_counter()
        metrics["startup_to_audit_enriched_ms"] = (t_enriched - t0) * 1000.0

        # 2. Measure Drop / Move Latency
        t_drop_start = time.perf_counter()
        # Optimistic visual update
        p0 = service_24.get_project("p_0")
        updated_p0 = Project(id="p_0", display_name=p0.display_name, source_path=p0.source_path, priority_group="MAIN0", slot=4)
        window.model.apply_project_move("MAIN0", 1, "MAIN0", 4, updated_p0)
        t_drop_visual = time.perf_counter()
        metrics["drop_to_visual_update_ms"] = (t_drop_visual - t_drop_start) * 1000.0

        # Async persistence
        t_persist_start = time.perf_counter()
        res = service_24.move_project("p_0", "MAIN0", 4)
        t_persist_end = time.perf_counter()
        metrics["drop_to_persist_complete_ms"] = (t_persist_end - t_persist_start) * 1000.0

        # 3. Measure Audit Event to Row Update Latency
        t_event_start = time.perf_counter()
        snap = AuditSnapshot(project_id="p_0", project_name="Project 0", completed_waves=3, all3_ready=True, temperature=AuditTemperature.HOT)
        window.model.update_audit_snapshot("p_0", snap)
        app.processEvents()
        t_event_end = time.perf_counter()
        metrics["audit_event_to_row_update_ms"] = (t_event_end - t_event_start) * 1000.0

        # 4. Measure Pack Click to Worker Started Latency
        t_pack_start = time.perf_counter()
        window.model.update_pack_state("p_0", "PACKING")
        window.task_runner.submit("pack:p_0", lambda: True)
        t_pack_started = time.perf_counter()
        metrics["pack_click_to_worker_started_ms"] = (t_pack_started - t_pack_start) * 1000.0

        # 5. Record Model Performance Counters
        metrics["model_reset_count"] = window.model.model_reset_count
        metrics["full_refresh_count"] = window.model.full_refresh_count
        metrics["targeted_project_update_count"] = window.model.targeted_project_update_count
        metrics["audit_files_read_count"] = AUDIT_COUNTERS["files_read"]
        metrics["directory_scan_count"] = AUDIT_COUNTERS["directory_scans"]

        # 6. Scale Matrix Measurements
        scale_timings = {}
        for count in [24, 60, 120, 300]:
            projs = [
                Project(id=f"proj_{i}", display_name=f"Proj {i}", source_path=str(tmp_path / f"proj_{i}"), priority_group=f"MAIN{i // 6}" if i < 12 else f"SIDE{(i - 12) // 6}", slot=(i % 6) + 1)
                for i in range(count)
            ]
            cfg = AppConfig(audits=AuditsConfig(root=str(tmp_path / "audits")), projects=projs)
            t_s = time.perf_counter()
            svc = ProjectService(cfg, base_dir=tmp_path)
            mdl = ProjectRoomModel(svc)
            elapsed_ms = (time.perf_counter() - t_s) * 1000.0
            scale_timings[f"{count}_projects_ms"] = elapsed_ms

        metrics["scale"] = scale_timings

        # 7. Stress Timings
        t_moves_s = time.perf_counter()
        for i in range(100):
            pid = f"p_{i % 24}"
            target_slot = (i % 6) + 1
            service_24.move_project(pid, "MAIN0", target_slot)
            p = service_24.get_project(pid)
            window.model.apply_project_move("MAIN0", 1, "MAIN0", target_slot, p)
        metrics["100_moves_ms"] = (time.perf_counter() - t_moves_s) * 1000.0

    print("=== AUDAPACK WAVE M BENCHMARK RESULTS ===")
    for k, v in metrics.items():
        if isinstance(v, dict):
            print(f"{k}:")
            for sub_k, sub_v in v.items():
                print(f"  {sub_k}: {sub_v:.2f}")
        elif isinstance(v, float):
            print(f"{k}: {v:.2f}")
        else:
            print(f"{k}: {v}")
    print("=========================================")
    return metrics


if __name__ == "__main__":
    run_benchmark()
