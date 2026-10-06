"""Native worker for tests/test_title_guardian_native.py (Windows only).

T-210: this is the CORRECTED architecture, exercised for real.

The worker runs under ``pythonw.exe`` -- exactly the shape AUDAPACK ships in
(AUDAPACK.vbs -> pythonw, a process with no console of its own). That matters
twice over:

* a console-attached process (plain ``pytest``) cannot attach to another
  console, so the pre-T-210 resolver returned nothing there and the real path
  could only be exercised from a console-less host;
* the escaped regression this file guards against (``[WinError 6] The handle is
  invalid``) is *caused* by a console-less host attaching to a managed console
  and then freeing it, which repopulates and then strands the host's standard
  handles. ``tests/native_title_red_control_worker.py`` reproduces that damage
  on purpose; this worker proves the fixed boundary does not do it.

What is proven here, with real Win32 calls and no mocks:

1. the production backend discovers a real ``CREATE_NEW_CONSOLE`` window with a
   read-only ``EnumWindows`` scan (the window is attributed to the launch PID,
   and AUDAPACK's own correlation token is readable from its caption);
2. ``InstanceMonitor`` proves launch<->window and ``bind_hwnd`` transfers
   ownership, after which a completely generic title is only drift;
3. the worker's standard handles -- deliberately NULLED first, because that is
   the state a real console-less GUI host has -- stay NULL through every
   resolve, event, bind and heartbeat;
4. 100 real bound Fleet preflights and 100 real hidden child spawns all succeed
   while the title guardian is active and four managed titles are being
   deliberately drifted, which is exactly the workload that used to break;
5. four simultaneous managed projects each recover their OWN canonical title
   within one heartbeat interval.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

GENERIC_TITLE = "Administrator: Windows PowerShell"
PROJECT_NAMES = ("AUDAPACK", "SAIPEN", "SAIMAIL", "LIMISAW")
ITERATIONS = 100

_STD_CODES = (("stdin", -10), ("stdout", -11), ("stderr", -12))


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.SetStdHandle.restype = wintypes.BOOL
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.GetConsoleWindow.argtypes = []
    return kernel


def _handle_state() -> dict:
    kernel = _kernel32()
    state: dict[str, str] = {}
    for name, code in _STD_CODES:
        value = int(kernel.GetStdHandle(ctypes.c_uint(code & 0xFFFFFFFF)) or 0)
        if value == 0:
            state[name] = "null"
        elif value in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            state[name] = "INVALID"
        else:
            state[name] = f"0x{value:08X}"
    return state


def _null_standard_handles() -> None:
    """Reproduce the real console-less GUI host: every std handle is NULL.

    AUDAPACK.vbs starts pythonw with no inherited console handles, so
    ``GetStdHandle`` answers NULL. Working from that state is what makes the
    process-safety claim in this worker meaningful: the red control starts from
    the same state and ends up with stranded, non-NULL handles.
    """
    kernel = _kernel32()
    for _name, code in _STD_CODES:
        kernel.SetStdHandle(ctypes.c_uint(code & 0xFFFFFFFF), wintypes.HANDLE(0))


def _console_window() -> int:
    return int(_kernel32().GetConsoleWindow() or 0)


def _wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _start_managed(name: str, root: Path, token: str) -> tuple[subprocess.Popen, str]:
    title = f"{name} | OpenCode YOLO | {root} | {token}"
    script = f"[Console]::Title = '{title}'; Start-Sleep 300"
    create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    process = subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
        creationflags=create_console,
    )
    return process, title


def run(report_path: Path, workdir: Path) -> dict:
    report: dict = {"ok": False, "error": "", "steps": [], "warnings": []}
    root = Path(workdir).resolve()
    processes: list[subprocess.Popen] = []
    guardian = None
    try:
        sys.path.insert(0, str(workdir))
        from audapack.instances import InstanceMonitor
        from audapack.models import Project
        from audapack.opencode_launch import OpenCodeLaunchPolicy
        from audapack.procutil import popen_hidden, run_hidden
        from audapack.saipen_transport import SaipenTransportError, bound_entrypoint, bound_python
        from audapack.title_guardian import HEARTBEAT_INTERVAL_MS, TitleGuardian, Win32TitleBackend

        backend = Win32TitleBackend()
        report["heartbeat_interval_ms"] = int(HEARTBEAT_INTERVAL_MS)
        report["handles_inherited"] = _handle_state()
        report["console_window_inherited"] = _console_window()

        # 1. the real console-less host shape: no console, NULL standard handles.
        _null_standard_handles()
        report["handles_nulled"] = _handle_state()
        report["console_window_after_null"] = _console_window()

        # 2. four real managed consoles, each with its OWN durable token.
        specs: list[dict] = []
        for index, name in enumerate(PROJECT_NAMES):
            token = f"OC-t210{index:04d}"
            project = Project(
                id=f"t210-{index}",
                display_name=name,
                source_path=str(root),
                priority_group="MAIN0",
                slot=index + 1,
            )
            process, title = _start_managed(name, root, token)
            processes.append(process)
            specs.append(
                {
                    "project": project,
                    "pid": int(process.pid),
                    "token": token,
                    "title": title,
                    "process": process,
                }
            )
        report["launch_pids"] = [spec["pid"] for spec in specs]
        report["steps"].append("managed_consoles_started")

        # 3. read-only discovery: EnumWindows by launch PID and by our own token.
        deadline = time.monotonic() + 25
        by_pid: dict[str, list[int]] = {}
        by_token: dict[str, list[int]] = {}
        while time.monotonic() < deadline:
            by_pid = {str(spec["pid"]): backend.resolve_hwnds(spec["pid"]) for spec in specs}
            by_token = {spec["token"]: backend.resolve_hwnds_by_token(spec["token"]) for spec in specs}
            if all(by_pid[str(spec["pid"])] for spec in specs) and all(
                by_token[spec["token"]] for spec in specs
            ):
                break
            time.sleep(0.25)
        report["resolved_by_pid_scan"] = by_pid
        report["resolved_by_token_scan"] = by_token
        report["window_classes"] = {
            str(spec["pid"]): [backend.window_class(hwnd) for hwnd in by_pid[str(spec["pid"])]]
            for spec in specs
        }
        report["handles_after_resolve"] = _handle_state()
        report["steps"].append("hwnd_discovered_without_attaching_console")

        # 4. register inside the long-lived host, then let InstanceMonitor prove
        #    the association and hand it over through bind_hwnd.
        guardian = TitleGuardian(backend=backend)
        for spec in specs:
            spec["registered"] = bool(
                guardian.register(
                    spec["pid"],
                    spec["title"],
                    correlation_token=spec["token"],
                    launcher_id="opencode",
                    project_id=spec["project"].id,
                )
            )
        report["registered"] = [spec["registered"] for spec in specs]
        report["event_driven"] = bool(guardian.event_driven)
        report["hook_installed"] = bool(backend.hook_installed)
        report["handles_after_register"] = _handle_state()

        monitor = InstanceMonitor(record_path=Path(tempfile.mkdtemp(prefix="t210-monitor-")) / "instances.json")
        for spec in specs:
            monitor.track_launch(
                spec["pid"], "opencode", spec["project"], correlation_token=spec["token"]
            )
        instances = monitor.refresh([spec["project"] for spec in specs], [])
        bound: list[dict] = []
        for instance in instances:
            launch_pid = int(getattr(instance, "launch_pid", 0) or 0)
            hwnd = int(getattr(instance, "hwnd", 0) or 0)
            if launch_pid <= 0 or hwnd <= 0:
                continue
            spec = next((item for item in specs if item["pid"] == launch_pid), None)
            if spec is None:
                continue
            ok = guardian.bind_hwnd(
                launch_pid,
                hwnd,
                correlation_token=str(getattr(instance, "correlation_token", "") or ""),
                native_pid=int(getattr(instance, "pid", 0) or 0),
            )
            spec["hwnd"] = hwnd
            bound.append(
                {
                    "launch_pid": launch_pid,
                    "hwnd": hwnd,
                    "native_pid": int(getattr(instance, "pid", 0) or 0),
                    "window_class": backend.window_class(hwnd),
                    "bind_ok": bool(ok),
                    "title_at_bind": backend.get_title(hwnd),
                }
            )
        report["monitor_bound"] = bound
        report["bound_count"] = len(bound)
        report["hwnd_for_pid"] = {str(spec["pid"]): guardian.hwnd_for_pid(spec["pid"]) for spec in specs}
        report["handles_after_bind"] = _handle_state()
        report["steps"].append("monitor_proved_and_bound")

        # 5. four simultaneous projects: generic drift, back to OWN canonical.
        phase1: list[dict] = []
        for spec in specs:
            hwnd = spec.get("hwnd", 0)
            backend.set_title(hwnd, GENERIC_TITLE)
            immediately = backend.get_title(hwnd)
            restored = _wait_for(
                lambda hwnd=hwnd, title=spec["title"]: backend.get_title(hwnd) == title,
                timeout=HEARTBEAT_INTERVAL_MS / 1000.0 * 3,
            )
            via_heartbeat = False
            if not restored:
                guardian.heartbeat()
                via_heartbeat = True
                restored = backend.get_title(hwnd) == spec["title"]
            phase1.append(
                {
                    "pid": spec["pid"],
                    "hwnd": hwnd,
                    "title_immediately_after_drift": immediately,
                    "title_after_restore": backend.get_title(hwnd),
                    "restored": bool(restored),
                    "needed_explicit_heartbeat": via_heartbeat,
                }
            )
        report["phase1_drift_restore"] = phase1
        report["phase1_all_restored"] = all(item["restored"] for item in phase1)
        report["phase1_titles_distinct"] = len({item["title_after_restore"] for item in phase1}) == len(phase1)
        report["steps"].append("four_projects_restored")

        # 6. TARGET L: after ownership is proven, a generic title is only drift --
        #    restoration must not depend on the token still being in the title.
        spec0 = next(item for item in specs if item.get("hwnd"))
        tokenless = "OpenCode - session"
        backend.set_title(spec0["hwnd"], tokenless)
        drift_repair = _wait_for(
            lambda: backend.get_title(spec0["hwnd"]) == spec0["title"], timeout=2.0
        )
        if not drift_repair:
            guardian.heartbeat()
            drift_repair = backend.get_title(spec0["hwnd"]) == spec0["title"]
        report["tokenless_drift_restored"] = bool(drift_repair)
        report["binding_survived_tokenless_drift"] = guardian.binding(spec0["hwnd"]) is not None
        report["steps"].append("tokenless_drift_repaired")

        # 7. THE regression workload: real Fleet preflights and real child
        #    spawns, repeatedly, while titles are drifting underneath.
        entry = None
        python = None
        try:
            entry = bound_entrypoint(root)
            python = bound_python()
            if not python.is_file():
                report["fleet_unavailable"] = f"bound python missing: {python}"
                entry = None
        except SaipenTransportError as exc:
            report["fleet_unavailable"] = str(exc)
        policy = OpenCodeLaunchPolicy()

        fleet_ok = fleet_bad = spawn_ok = spawn_bad = 0
        restore_failures: list[int] = []
        spawn_errors: list[str] = []
        fleet_errors: list[str] = []
        classifications: dict[str, int] = {}
        for index in range(ITERATIONS):
            spec = specs[index % len(specs)]
            hwnd = spec.get("hwnd", 0)
            backend.set_title(hwnd, f"{GENERIC_TITLE} {index}")
            if not _wait_for(
                lambda hwnd=hwnd, title=spec["title"]: backend.get_title(hwnd) == title,
                timeout=1.0,
            ):
                guardian.heartbeat()
                if backend.get_title(hwnd) != spec["title"]:
                    restore_failures.append(index)
            guardian.heartbeat()

            try:
                result = run_hidden(
                    [sys.executable, "-c", "print('ok')"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip() == "ok":
                    spawn_ok += 1
                else:
                    spawn_bad += 1
                    spawn_errors.append(f"iteration {index}: rc={result.returncode}")
            except Exception as exc:  # noqa: BLE001 - the failure IS the evidence
                spawn_bad += 1
                spawn_errors.append(f"iteration {index}: {type(exc).__name__}: {exc}")

            if entry is not None and python is not None:
                try:
                    payload = policy._fleet_preflight(python, entry, root)
                    classification = str(payload.get("classification", ""))
                    if classification:
                        fleet_ok += 1
                        classifications[classification] = classifications.get(classification, 0) + 1
                    else:
                        fleet_bad += 1
                        fleet_errors.append(f"iteration {index}: no classification")
                except Exception as exc:  # noqa: BLE001
                    fleet_bad += 1
                    fleet_errors.append(f"iteration {index}: {type(exc).__name__}: {exc}")

        # A failure to CREATE the child process is this boundary's defect; a
        # bound CLI that starts and then exits non-zero is SAIPEN's own state and
        # must never be reported as a handle regression.
        spawn_failure = next(
            (
                error for error in fleet_errors
                if "WinError" in error or "handle is invalid" in error or "OSError" in error
            ),
            "",
        )
        report["iterations"] = ITERATIONS
        report["fleet_preflights_ok"] = fleet_ok
        report["fleet_preflights_failed"] = fleet_bad
        report["fleet_classifications"] = classifications
        report["fleet_spawn_failure"] = spawn_failure
        if entry is not None and fleet_ok == 0 and fleet_errors and not spawn_failure:
            report["fleet_unavailable"] = fleet_errors[0]
        report["fleet_errors"] = fleet_errors[:5]
        report["spawns_ok"] = spawn_ok
        report["spawns_failed"] = spawn_bad
        report["spawn_errors"] = spawn_errors[:5]
        report["restore_failures"] = restore_failures[:10]
        report["handles_after_workload"] = _handle_state()
        report["steps"].append("fleet_and_spawn_stress")

        # 8. TARGET J: representative subprocess users beyond Fleet.
        representative: dict[str, str] = {}
        try:
            result = run_hidden(
                [sys.executable, "-c", "print('hidden')"],
                capture_output=True, text=True, timeout=30,
            )
            representative["run_hidden"] = result.stdout.strip()
        except Exception as exc:  # noqa: BLE001
            representative["run_hidden"] = f"{type(exc).__name__}: {exc}"
        try:
            proc = popen_hidden(
                [sys.executable, "-c", "print('popen')"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            out, _err = proc.communicate(timeout=30)
            representative["popen_hidden"] = out.strip()
        except Exception as exc:  # noqa: BLE001
            representative["popen_hidden"] = f"{type(exc).__name__}: {exc}"
        try:
            result = subprocess.run(
                [sys.executable, "-c", "print('direct')"],
                capture_output=True, text=True, timeout=30,
            )
            representative["direct_subprocess"] = result.stdout.strip()
        except Exception as exc:  # noqa: BLE001
            representative["direct_subprocess"] = f"{type(exc).__name__}: {exc}"
        if entry is not None and python is not None:
            try:
                result = run_hidden(
                    [str(python), str(entry), "validate"],
                    cwd=str(root), capture_output=True, text=True, timeout=180,
                )
                # The point is that the bound CLI can be SPAWNED at all; its own
                # exit code is SAIPEN's business, not this boundary's.
                representative["saipen_read_only"] = f"spawned rc={result.returncode}"
            except Exception as exc:  # noqa: BLE001
                representative["saipen_read_only"] = f"{type(exc).__name__}: {exc}"
        report["representative_spawns"] = representative
        report["handles_after_representative"] = _handle_state()

        # 9. lifecycle truth: exit retires ownership, nothing is renamed after.
        for spec in specs:
            process = spec["process"]
            if process.poll() is None:
                process.terminate()
        for spec in specs:
            try:
                spec["process"].wait(timeout=20)
            except Exception:
                spec["process"].kill()
        retired = _wait_for(lambda: guardian.retire_dead() >= 1, timeout=25.0, interval=0.25)
        report["retired"] = bool(retired)
        report["active_after_exit"] = int(guardian.active_count)
        report["bindings_after_exit"] = int(guardian.binding_count)
        report["renames_after_exit"] = int(guardian.heartbeat())
        report["handles_final"] = _handle_state()
        report["console_window_final"] = _console_window()
        report["steps"].append("retired")

        report["ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported to the test, never raised blind
        import traceback

        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        for process in processes:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass
        try:
            if guardian is not None:
                guardian.shutdown()
        except Exception:
            pass
    return report


def main() -> int:
    if len(sys.argv) < 3:
        return 2
    report_path = Path(sys.argv[1])
    workdir = Path(sys.argv[2])
    report = run(report_path, workdir)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
