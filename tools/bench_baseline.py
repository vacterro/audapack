"""Dev-only performance baseline for Wave K — measures Tkinter-era costs.

Writes `.saipen/kitchen/bench_baseline.json`. No telemetry, no fabricated values.
"""

import sys
if __name__ == "__main__" and "." not in sys.path:
    sys.path.insert(0, ".")

import json, pathlib, tempfile, shutil, time
from audapack.config import AppConfig, save_config
from audapack.services.project_service import ProjectService
from audapack.services.audit_service import AuditService

def _synthetic_config(base, n):
    cfg = AppConfig()
    cfg.audits.root = str(base / "AUDITING_IMPLEMENTATION")
    pathlib.Path(cfg.audits.root).mkdir(parents=True, exist_ok=True)
    save_config(cfg, base)
    svc = ProjectService(base_dir=base)
    for i in range(n):
        svc.add_project(f"BenchProj{i:03d}", f"C:\\Bench{i:03d}")
    return base

def bench():
    out = {}
    # startup
    t0 = time.perf_counter()
    from audapack.services.app_controller import AppController
    base = pathlib.Path(tempfile.mkdtemp())
    try:
        _synthetic_config(base, 0)
        ctrl = AppController(base_dir=base)
        out["startup_ms"] = round((time.perf_counter()-t0)*1000, 2)
        # single move 24
        base24 = pathlib.Path(tempfile.mkdtemp())
        _synthetic_config(base24, 24)
        svc24 = ProjectService(base_dir=base24)
        projs = svc24.list_projects()
        pid = projs[0].id
        t0 = time.perf_counter()
        svc24.move_project(pid, "MAIN1", 6)
        out["single_move_24_ms"] = round((time.perf_counter()-t0)*1000, 2)
        # single move 60
        base60 = pathlib.Path(tempfile.mkdtemp())
        _synthetic_config(base60, 60)
        svc60 = ProjectService(base_dir=base60)
        pid60 = svc60.list_projects()[0].id
        t0 = time.perf_counter()
        svc60.move_project(pid60, "SIDE1", 2)
        out["single_move_60_ms"] = round((time.perf_counter()-t0)*1000, 2)
        # full refresh (scan_all) 24
        audit = AuditService(base_dir=base24)
        t0 = time.perf_counter()
        audit.refresh_all()
        out["full_refresh_24_ms"] = round((time.perf_counter()-t0)*1000, 2)
        # 60
        audit60 = AuditService(base_dir=base60)
        t0 = time.perf_counter()
        audit60.refresh_all()
        out["full_refresh_60_ms"] = round((time.perf_counter()-t0)*1000, 2)
        # room build synthetic (just list + slot_map)
        t0 = time.perf_counter()
        _ = svc60.get_slot_map()
        _ = svc60.active_groups()
        out["room_build_60_ms"] = round((time.perf_counter()-t0)*1000, 2)
        # 120-project stress baseline
        base120 = pathlib.Path(tempfile.mkdtemp())
        _synthetic_config(base120, 120)
        svc120 = ProjectService(base_dir=base120)
        pid120 = svc120.list_projects()[0].id
        t0 = time.perf_counter()
        svc120.move_project(pid120, "SIDE1", 2)
        out["single_move_120_ms"] = round((time.perf_counter()-t0)*1000, 2)
        audit120 = AuditService(base_dir=base120)
        t0 = time.perf_counter()
        audit120.refresh_all()
        out["full_refresh_120_ms"] = round((time.perf_counter()-t0)*1000, 2)
        shutil.rmtree(base120, ignore_errors=True)
        shutil.rmtree(base24, ignore_errors=True)
        shutil.rmtree(base60, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    out["note"] = "dev baseline only, no telemetry, Tkinter era"
    dest = pathlib.Path(".saipen/kitchen/bench_baseline.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    bench()
