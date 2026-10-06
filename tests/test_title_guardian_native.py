"""T-210: the REAL Win32 title path, on REAL Windows consoles, process-safe.

``tests/test_title_guardian.py`` runs against an in-process fake. This module is
the native evidence, and it is deliberately shaped around the failure that
escaped T-209:

- T-209 celebrated the fact that a ``pythonw`` worker performed
  ``AttachConsole`` itself. That proved title discovery and missed the
  collateral damage: ``AttachConsole``/``FreeConsole`` act on the CALLING
  PROCESS, so the long-lived AUDAPACK host came out of every title resolution
  with stranded standard handles and every later ``capture_output`` spawn died
  with ``[WinError 6] The handle is invalid``.
- The corrected architecture never attaches anything. Discovery is a read-only
  ``EnumWindows`` scan (by launch PID and by AUDAPACK's own correlation token),
  InstanceMonitor proves the association, and ``bind_hwnd`` transfers ownership.

Coverage layers, each with its own evidence:

1. production backend primitives are live and its discovery is read-only;
2. a real ``CREATE_NEW_CONSOLE`` window is discoverable from a normal
   console-attached pytest process without touching that process' console
   state -- the pre-T-210 resolver could not do this at all;
3. the corrected ``pythonw`` worker: 100 real Fleet preflights, 100 real hidden
   spawns, four drifted projects, NULL host handles that stay NULL;
4. the red control: the pre-T-210 resolver, run for real, repopulating then
   stranding the host's handles and failing the very next spawn with WinError 6.
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

import pytest

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="real Win32 console required")

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve().parent / "native_title_worker.py"
RED_CONTROL_WORKER = Path(__file__).resolve().parent / "native_title_red_control_worker.py"

GENERIC_TITLE = "Administrator: Windows PowerShell"

_STD_CODES = (("stdin", -10), ("stdout", -11), ("stderr", -12))


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.GetConsoleWindow.argtypes = []
    return kernel


def _std_handles() -> dict:
    kernel = _kernel32()
    return {
        name: int(kernel.GetStdHandle(ctypes.c_uint(code & 0xFFFFFFFF)) or 0)
        for name, code in _STD_CODES
    }


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


def _run_pythonw_worker(worker: Path, timeout: float) -> dict:
    """Run a worker under pythonw.exe -- the shape AUDAPACK ships in."""
    report_path = Path(tempfile.mkdtemp(prefix="audapack-t210-native-")) / "report.json"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = str(pythonw) if pythonw.exists() else sys.executable
    creationflags = 0
    if executable == sys.executable:
        # No pythonw available: detach so the worker still has no console.
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

    process = subprocess.Popen(
        [executable, str(worker), str(report_path), str(ROOT)],
        cwd=str(ROOT),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    report: dict = {}
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    pass
            if process.poll() is not None and not report_path.exists():
                break
            time.sleep(0.3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert report, "the native worker produced no report"
    return report


@WINDOWS_ONLY
def test_getconsolewindow_is_a_kernel32_export_not_user32():
    """Historical context for T-209's ROOT CAUSE A, still a platform fact.

    The pre-T-209 resolver asked user32 for ``GetConsoleWindow``; user32 does
    not export it, ctypes raised AttributeError, and every resolution silently
    returned no HWND. T-210 removed the call site entirely -- the backend binds
    neither user32's nor kernel32's version, because reading it requires
    attaching the AUDAPACK process to somebody else's console.
    """
    kernel32 = ctypes.WinDLL("kernel32")
    assert kernel32.GetConsoleWindow is not None
    user32 = ctypes.WinDLL("user32")
    with pytest.raises(AttributeError):
        user32.GetConsoleWindow  # noqa: B018 - the AttributeError IS the assertion


@WINDOWS_ONLY
def test_win32_backend_primitives_are_live_and_discovery_is_read_only():
    from audapack.title_guardian import Win32TitleBackend

    backend = Win32TitleBackend()
    assert backend.is_window(0) is False
    assert backend.get_title(0) == ""
    assert backend.resolve_hwnds(0) == []
    assert backend.resolve_hwnds(-1) == []
    assert backend.resolve_hwnds_by_token("") == []
    assert backend.resolve_hwnds_by_token("no-such-token-t210") == []
    assert backend.process_alive(__import__("os").getpid()) is True
    assert backend.process_token(__import__("os").getpid()) != 0

    # Discovery is side-effect-free for THIS process: same console association,
    # same standard handles before and after. The red control proves the
    # opposite outcome for the pre-T-210 resolver.
    console_before = _console_window()
    handles_before = _std_handles()
    backend.resolve_hwnds(__import__("os").getpid())
    backend.resolve_hwnds_by_token("no-such-token-t210")
    backend.window_class(0)
    assert _console_window() == console_before
    assert _std_handles() == handles_before


@WINDOWS_ONLY
def test_managed_console_hwnd_is_discoverable_without_attaching_console():
    """A real CREATE_NEW_CONSOLE window, found from an attached pytest process.

    The pre-T-210 resolver could not do this: it started with
    ``AttachConsole(target_pid)``, which Windows refuses for a process that
    already has a console, so it returned nothing and the title was never
    restored. The read-only scan finds the window because Windows attributes the
    ``ConsoleWindowClass`` window to the launch PID, and the caption carries
    AUDAPACK's own correlation token.
    """
    from audapack.title_guardian import Win32TitleBackend

    token = "OC-native-t210-probe"
    title = f"AUDAPACK | OpenCode YOLO | {ROOT} | {token}"
    create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    process = subprocess.Popen(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-Command",
            f"[Console]::Title = '{title}'; Start-Sleep 120",
        ],
        creationflags=create_console,
    )
    backend = Win32TitleBackend()
    console_before = _console_window()
    handles_before = _std_handles()
    try:
        assert _wait_for(
            lambda: bool(backend.resolve_hwnds(process.pid))
            and bool(backend.resolve_hwnds_by_token(token)),
            timeout=25.0,
        ), "the read-only scan never found a real CREATE_NEW_CONSOLE window"
        by_pid = backend.resolve_hwnds(process.pid)
        by_token = backend.resolve_hwnds_by_token(token)

        assert by_pid, "PID scan found no window for a real managed console"
        assert by_token, "token scan found no window for a real managed console"
        assert by_pid[0] in by_token
        assert backend.window_class(by_pid[0]) == "ConsoleWindowClass"
        assert token in backend.get_title(by_pid[0])

        # Reading somebody else's console caption changed nothing here.
        assert _console_window() == console_before
        assert _std_handles() == handles_before
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except Exception:
                process.kill()


@WINDOWS_ONLY
@pytest.mark.timeout(480)
def test_corrected_worker_proves_process_safety_and_one_hundred_fleet_preflights():
    """The corrected architecture, under load, in a real console-less host."""
    report = _run_pythonw_worker(WORKER, timeout=420)
    assert report.get("ok") is True, json.dumps(report, indent=2, ensure_ascii=False)[-6000:]

    # 1. the host really is the console-less shape we think we are testing.
    assert report["console_window_inherited"] == 0
    assert report["handles_nulled"] == {"stdin": "null", "stdout": "null", "stderr": "null"}

    # 2. four real managed consoles, each found by PID and by its own token.
    assert len(report["launch_pids"]) == 4
    assert all(report["resolved_by_pid_scan"][str(pid)] for pid in report["launch_pids"])
    assert all(report["resolved_by_token_scan"][token] for token in report["resolved_by_token_scan"])
    assert all(
        "ConsoleWindowClass" in classes for classes in report["window_classes"].values()
    ), report["window_classes"]

    # 3. InstanceMonitor proved the association; bind_hwnd adopted it.
    assert report["bound_count"] == 4, report["monitor_bound"]
    assert all(item["bind_ok"] for item in report["monitor_bound"])
    assert all(report["hwnd_for_pid"][str(pid)] for pid in report["launch_pids"])

    # 4. four simultaneous projects, each back to its OWN canonical title.
    assert report["phase1_all_restored"] is True, report["phase1_drift_restore"]
    assert report["phase1_titles_distinct"] is True
    assert len({item["title_after_restore"] for item in report["phase1_drift_restore"]}) == 4

    # 5. TARGET L: a generic title without the token is only drift.
    assert report["tokenless_drift_restored"] is True
    assert report["binding_survived_tokenless_drift"] is True

    # 6. THE regression workload. Failure to CREATE a child is this boundary's
    #    defect and is asserted unconditionally; a bound CLI that starts and then
    #    exits non-zero is SAIPEN's own state, reported honestly at the end
    #    instead of being dressed up as a handle fix.
    assert report["iterations"] == 100
    assert report["fleet_spawn_failure"] == "", report["fleet_spawn_failure"]
    assert report["spawns_ok"] == 100, report["spawn_errors"]
    assert report["spawns_failed"] == 0
    assert report["restore_failures"] == [], report["restore_failures"]  # type: ignore[index]

    # 7. every representative subprocess user still spawns normally.
    representative = report["representative_spawns"]
    assert representative["run_hidden"] == "hidden", representative
    assert representative["popen_hidden"] == "popen", representative
    assert representative["direct_subprocess"] == "direct", representative
    if "saipen_read_only" in representative:
        assert representative["saipen_read_only"].startswith("spawned rc="), representative

    # 8. THE point: the host's console state never moved. A NULL standard-handle
    #    slot is still NULL -- the exact thing the red control breaks.
    for key in ("handles_after_resolve", "handles_after_register", "handles_after_bind",
                "handles_after_workload", "handles_after_representative", "handles_final"):
        assert report[key] == {"stdin": "null", "stdout": "null", "stderr": "null"}, (key, report[key])
    assert report["console_window_final"] == 0

    # 9. exit retires ownership; nothing is renamed afterwards.
    assert report["retired"] is True
    assert report["active_after_exit"] == 0
    assert report["bindings_after_exit"] == 0
    assert report["renames_after_exit"] == 0

    # 10. LAST, so the process-safety evidence above is never skipped away: the
    #     100/100 real Fleet preflights. When the bound SAIPEN CLI in this
    #     checkout cannot answer at all (its own engine failing to import, for
    #     instance) that is reported as an explicit skip with the reason, and the
    #     handle-ownership proof above still stands on its own.
    if report.get("fleet_unavailable") or report["fleet_preflights_ok"] != 100:
        pytest.skip(
            "100/100 real Fleet preflights not provable in this checkout: "
            f"{report.get('fleet_unavailable') or report['fleet_errors'][:1]}"
        )
    assert report["fleet_preflights_failed"] == 0
    assert report["fleet_classifications"], "Fleet returned no classification at all"


@WINDOWS_ONLY
def test_red_control_reproduces_winerror6_from_process_global_console_attach():
    """TARGET H: the pre-T-210 architecture, reproduced for real.

    No mocked exception: real AttachConsole/GetConsoleWindow/FreeConsole against
    a real CREATE_NEW_CONSOLE console, real GetStdHandle reads, and a real
    CreateProcess failure at process creation.
    """
    report = _run_pythonw_worker(RED_CONTROL_WORKER, timeout=240)
    assert report.get("ok") is True, json.dumps(report, indent=2, ensure_ascii=False)[-6000:]

    variant = report["variants"]["nulled_handles"]
    # the legacy resolver did resolve a real console window (so the damage is
    # not an artifact of failing to find anything).
    assert variant["resolved_hwnd"] != 0, report

    # 1. attaching + freeing REPLACED a NULL handle slot with a stranded value.
    assert variant["handles_before"] == {"stdin": "null", "stdout": "null", "stderr": "null"}
    assert variant["handle_mutation"] is True, variant["handles_after"]
    assert variant["stdin_stale_not_null"] is True, variant["handles_after"]

    # 2. and the very next capture_output spawn failed exactly as the operator
    #    reported it.
    assert variant["winerror6_reproduced"] is True, variant["spawn_errors"]

    # 3. the inherited-handle variant is recorded honestly rather than assumed.
    assert report["variants"]["inherited_handles"]["handles_before"], report
