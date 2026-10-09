"""Automated idle soak and resource leak measurement script.

Measures:
- CPU idle %
- Working Set (RSS)
- Private Bytes
- Thread count
- Process Handle count
- Windows GDI object count (explicit ctypes signatures)
- Windows USER object count (explicit ctypes signatures)

Profiles across:
1. Startup baseline after settling
2. Idle soak period
3. Multiple operation rounds (10x project switches, 3x packs, 1x pack all)
4. Post-operation idle stabilization
5. Child process / orphan check
6. Monotonic growth analysis and explicit PASS/FAIL verdict
"""

from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.windll.kernel32
    _u32 = ctypes.windll.user32
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.argtypes = []
    _u32.GetGuiResources.restype = wintypes.DWORD
    _u32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]

import psutil
from PySide6.QtWidgets import QApplication

from audapack import hot_freshness
from audapack.config import AppConfig, PackingConfig, save_config
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.main_window import MainWindow


class ResourceSnapshot(NamedTuple):
    timestamp: float
    cpu_percent: float
    rss_mb: float
    private_mb: float
    threads: int
    handles: int
    gdi_objects: Optional[int]
    user_objects: Optional[int]
    gui_resources_error: Optional[str] = None

    def to_dict(self):
        return {
            "timestamp": round(self.timestamp, 2),
            "cpu_percent": round(self.cpu_percent, 2),
            "rss_mb": round(self.rss_mb, 2),
            "private_mb": round(self.private_mb, 2),
            "threads": self.threads,
            "handles": self.handles,
            "gdi_objects": self.gdi_objects,
            "user_objects": self.user_objects,
            "gui_resources_error": self.gui_resources_error,
        }


def sample_resources(process: psutil.Process, interval: float = 0.5) -> ResourceSnapshot:
    cpu = process.cpu_percent(interval=interval)
    mem = process.memory_info()
    threads = process.num_threads()
    handles = process.num_handles()

    gdi: Optional[int] = None
    user: Optional[int] = None
    gui_err: Optional[str] = None

    if sys.platform == "win32":
        try:
            h = _k32.GetCurrentProcess()
            gdi_val = _u32.GetGuiResources(h, 0)  # GR_GDIOBJECTS
            user_val = _u32.GetGuiResources(h, 1)  # GR_USEROBJECTS
            err = ctypes.GetLastError()
            if gdi_val == 0 and err != 0:
                gui_err = f"GetGuiResources failed with winerror {err}"
            else:
                gdi = int(gdi_val)
                user = int(user_val)
        except Exception as exc:
            gui_err = str(exc)
    else:
        gui_err = "platform_not_windows"

    return ResourceSnapshot(
        timestamp=time.time(),
        cpu_percent=cpu,
        rss_mb=mem.rss / (1024 * 1024),
        private_mb=getattr(mem, "private", mem.rss) / (1024 * 1024),
        threads=threads,
        handles=handles,
        gdi_objects=gdi,
        user_objects=user,
        gui_resources_error=gui_err,
    )


def run_soak(duration_seconds: int = 300, rounds_count: int = 3, report_path: Optional[str] = None) -> dict:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    tmp_dir = tempfile.mkdtemp(prefix="audapack_soak_")
    tmp_path = Path(tmp_dir)

    projects = []
    for i in range(1, 5):
        p_src = tmp_path / f"proj_{i}"
        p_src.mkdir(parents=True)
        for f in range(10):
            (p_src / f"file_{f}.py").write_text(f"# Project {i} file {f}\nprint({f})\n", encoding="utf-8")
        projects.append(
            Project(
                id=f"p_{i}",
                display_name=f"Project {i}",
                source_path=str(p_src),
                priority_group="A",
                slot=i,
                archive_name=f"proj_{i}",
            )
        )

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    cfg = AppConfig(packing=PackingConfig(output_dir=str(out_dir)))
    cfg.projects = projects
    save_config(cfg, base_dir=tmp_path)

    service = ProjectService(cfg, base_dir=tmp_path)
    window = MainWindow(service)
    window.show()

    proc = psutil.Process()

    # Step 1: Let UI settle
    print("Settling UI...")
    start_time = time.monotonic()
    while time.monotonic() - start_time < 3.0:
        app.processEvents()
        time.sleep(0.05)

    baseline = sample_resources(proc, interval=1.0)
    print(
        f"BASELINE: RSS={baseline.rss_mb:.1f}MB, Private={baseline.private_mb:.1f}MB, "
        f"Threads={baseline.threads}, Handles={baseline.handles}, "
        f"GDI={baseline.gdi_objects}, USER={baseline.user_objects}"
    )

    # Step 2: Idle soak
    print(f"Starting idle soak ({duration_seconds}s)...")
    soak_samples = []
    soak_start = time.monotonic()
    sample_interval = 10.0 if duration_seconds >= 30 else max(1.0, duration_seconds / 3.0)
    next_sample = soak_start + sample_interval

    while time.monotonic() - soak_start < duration_seconds:
        app.processEvents()
        time.sleep(0.05)
        now = time.monotonic()
        if now >= next_sample:
            s = sample_resources(proc, interval=0.2)
            soak_samples.append(s)
            print(
                f"  [{int(now - soak_start):3d}s] CPU={s.cpu_percent:.1f}%, RSS={s.rss_mb:.1f}MB, "
                f"Threads={s.threads}, Handles={s.handles}, GDI={s.gdi_objects}, USER={s.user_objects}"
            )
            next_sample = now + sample_interval

    post_idle = sample_resources(proc, interval=1.0)
    print(
        f"POST-IDLE: RSS={post_idle.rss_mb:.1f}MB, Private={post_idle.private_mb:.1f}MB, "
        f"Threads={post_idle.threads}, Handles={post_idle.handles}"
    )

    # Step 3: Multiple operation rounds
    round_snapshots = []
    for rnd in range(1, rounds_count + 1):
        print(f"--- Operation Round {rnd}/{rounds_count} ---")
        # 10x project selection switching cycles
        for _ in range(10):
            for i in range(1, 5):
                idx = window.model.index(0, 0)
                slot_idx = window.model.index(i - 1, 0, idx)
                window.tree.setCurrentIndex(slot_idx)
                app.processEvents()
                time.sleep(0.02)

        # 3x Single Pack operations
        for _pack_idx in range(3):
            res = window._packing.pack_project("p_1")
            assert res.success
            app.processEvents()
            time.sleep(0.05)

        # 1x Pack All operation
        window._on_pack_all()
        pack_all_deadline = time.monotonic() + 30.0
        while time.monotonic() < pack_all_deadline:
            app.processEvents()
            time.sleep(0.05)
            if not window._pack_batch_runner.busy():
                break

        # Settle round
        round_settle_start = time.monotonic()
        while time.monotonic() - round_settle_start < 2.0:
            app.processEvents()
            time.sleep(0.05)

        snap = sample_resources(proc, interval=0.5)
        round_snapshots.append(snap)
        print(
            f"  ROUND {rnd} END: RSS={snap.rss_mb:.1f}MB, Private={snap.private_mb:.1f}MB, "
            f"Threads={snap.threads}, Handles={snap.handles}, GDI={snap.gdi_objects}, USER={snap.user_objects}"
        )

    # Step 4: Post-operation idle stabilization
    print("Observing post-operation idle stabilization (5s)...")
    stab_start = time.monotonic()
    while time.monotonic() - stab_start < 5.0:
        app.processEvents()
        time.sleep(0.05)

    final_stabilized = sample_resources(proc, interval=1.0)
    print(
        f"FINAL-STABILIZED: RSS={final_stabilized.rss_mb:.1f}MB, Private={final_stabilized.private_mb:.1f}MB, "
        f"Threads={final_stabilized.threads}, Handles={final_stabilized.handles}, "
        f"GDI={final_stabilized.gdi_objects}, USER={final_stabilized.user_objects}"
    )

    # Step 5: Check for orphan processes
    orphans = []
    for p in proc.children(recursive=True):
        orphans.append(f"{p.name()} (pid={p.pid})")
    print(f"ORPHAN PROCESSES: {orphans or 'None'}")

    # Step 6: Verify recurring timers and close
    window.close()
    app.processEvents()
    timers_stopped_after_close = (
        (not hasattr(window, "auto_pack_timer") or window.auto_pack_timer is None or not window.auto_pack_timer.isActive())
        and (not hasattr(window, "temp_timer") or window.temp_timer is None or not window.temp_timer.isActive())
        and (not hasattr(window, "bridge_timer") or window.bridge_timer is None or not window.bridge_timer.isActive())
        and (not hasattr(window, "_pack_batch_runner") or window._pack_batch_runner is None or not window._pack_batch_runner.busy())
    )
    hot_freshness.reset()

    # Step 7: Leak verdict & explicit checks
    private_growth_monotonic = False
    handles_growth_monotonic = False
    gdi_growth_monotonic = False
    user_growth_monotonic = False

    if len(round_snapshots) >= 3:
        r1, r2, r3 = round_snapshots[-3], round_snapshots[-2], round_snapshots[-1]
        private_growth_monotonic = (r3.private_mb > r2.private_mb > r1.private_mb) and (
            (r3.private_mb - r1.private_mb) > 10.0
        )
        handles_growth_monotonic = (r3.handles > r2.handles > r1.handles) and (
            (r3.handles - r1.handles) > 30
        )
        if (
            sys.platform == "win32"
            and r1.gdi_objects is not None
            and r2.gdi_objects is not None
            and r3.gdi_objects is not None
        ):
            gdi_growth_monotonic = (r3.gdi_objects > r2.gdi_objects > r1.gdi_objects) and (
                (r3.gdi_objects - r1.gdi_objects) > 15
            )
        if (
            sys.platform == "win32"
            and r1.user_objects is not None
            and r2.user_objects is not None
            and r3.user_objects is not None
        ):
            user_growth_monotonic = (r3.user_objects > r2.user_objects > r1.user_objects) and (
                (r3.user_objects - r1.user_objects) > 15
            )

    checks = {
        "no_orphan_processes": len(orphans) == 0,
        "recurring_timers_stopped_on_close": timers_stopped_after_close,
        "private_bytes_bounded_growth": not private_growth_monotonic,
        "handles_bounded_growth": not handles_growth_monotonic,
        "threads_settled_range": final_stabilized.threads <= baseline.threads + 4,
        "gdi_objects_valid_non_null": (
            (baseline.gdi_objects is not None and baseline.gdi_objects > 0)
            if sys.platform == "win32"
            else True
        ),
        "user_objects_valid_non_null": (
            (baseline.user_objects is not None and baseline.user_objects > 0)
            if sys.platform == "win32"
            else True
        ),
        "gdi_objects_bounded_growth": not gdi_growth_monotonic,
        "user_objects_bounded_growth": not user_growth_monotonic,
    }
    verdict = "PASS" if all(checks.values()) else "FAIL"

    report = {
        "verdict": verdict,
        "checks": checks,
        "baseline": baseline.to_dict(),
        "post_idle": post_idle.to_dict(),
        "operation_rounds": [s.to_dict() for s in round_snapshots],
        "final_stabilized": final_stabilized.to_dict(),
        "deltas": {
            "rss_mb_delta": round(final_stabilized.rss_mb - baseline.rss_mb, 2),
            "private_mb_delta": round(final_stabilized.private_mb - baseline.private_mb, 2),
            "threads_delta": final_stabilized.threads - baseline.threads,
            "handles_delta": final_stabilized.handles - baseline.handles,
            "gdi_objects_delta": (
                (final_stabilized.gdi_objects - baseline.gdi_objects)
                if (final_stabilized.gdi_objects is not None and baseline.gdi_objects is not None)
                else None
            ),
            "user_objects_delta": (
                (final_stabilized.user_objects - baseline.user_objects)
                if (final_stabilized.user_objects is not None and baseline.user_objects is not None)
                else None
            ),
        },
        "orphan_processes": orphans,
        "soak_samples": [s.to_dict() for s in soak_samples],
    }

    out_file = Path(report_path or "tests/soak_audit_report.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSOAK AUDIT VERDICT: {verdict}")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"Report written to {out_file.resolve()}\n")
    return report


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    run_soak(duration_seconds=duration, rounds_count=rounds)
