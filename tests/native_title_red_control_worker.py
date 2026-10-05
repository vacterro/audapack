"""T-210 TARGET H: RED CONTROL for the escaped T-209 regression (Windows only).

This worker is a faithful copy of the PRE-T-210 resolution path and exists only
to prove, with real Win32 calls, that the causal chain is real. It is the
control: ``tests/native_title_worker.py`` runs the same kind of workload against
the fixed architecture and must show none of this damage.

The old production code (``Win32TitleBackend._console_hwnd``, T-209) was::

    kernel.AttachConsole(target_pid)
    hwnd = kernel.GetConsoleWindow()
    kernel.FreeConsole()

It ran inside the long-lived AUDAPACK GUI process, from ``register()``, the
1000 ms heartbeat, the WinEvent callback and the unknown-HWND recovery path.

``AttachConsole``/``FreeConsole`` act on the CALLING PROCESS, not on a thread:
attaching repopulates that process' standard handles with the target console's,
and freeing leaves those values behind as stale handles. The next
``STARTF_USESTDHANDLES`` ``CreateProcess`` -- exactly what
``subprocess.run(..., capture_output=True)`` does when the caller does not
specify ``stdin`` -- then fails at creation with
``[WinError 6] The handle is invalid``. That is precisely how the operator saw
``SAIPEN Fleet preflight failed: [WinError 6] The handle is invalid``.

Nothing here is mocked: a real ``CREATE_NEW_CONSOLE`` PowerShell console, real
``AttachConsole``/``GetConsoleWindow``/``FreeConsole``, real ``GetStdHandle``
reads and a real failing/failing-or-not ``CreateProcess``.

Two host shapes are measured:

``inherited_handles``
    the handles the worker was started with (valid, inherited DEVNULL handles);
``nulled_handles``
    every standard handle set to NULL first, which is the documented state of a
    real ``AUDAPACK.vbs -> pythonw`` host that has no console at all.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

GENERIC_TITLE = "Administrator: Windows PowerShell"
CANONICAL_TITLE = r"AUDAPACK | OpenCode YOLO | V:\code\audapack | OC-redcontrol"
CYCLES = 8

_STD_CODES = (("stdin", -10), ("stdout", -11), ("stderr", -12))


class LegacyAttachResolver:
    """Verbatim pre-T-210 resolution: attach, read, free. RED CONTROL ONLY."""

    def __init__(self) -> None:
        self._ctypes = ctypes
        self._wintypes = wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleWindow.restype = wintypes.HWND
        kernel.GetConsoleWindow.argtypes = []
        kernel.AttachConsole.restype = wintypes.BOOL
        kernel.AttachConsole.argtypes = [wintypes.DWORD]
        kernel.FreeConsole.restype = wintypes.BOOL
        kernel.FreeConsole.argtypes = []
        self._kernel = kernel
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetWindowTextW.restype = wintypes.BOOL
        user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user.GetWindowTextW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user = user

    def console_hwnd(self, pid: int) -> int:
        """The T-209 implementation, unchanged."""
        if not self._kernel.AttachConsole(self._wintypes.DWORD(int(pid))):
            return 0
        try:
            return int(self._kernel.GetConsoleWindow() or 0)
        finally:
            self._kernel.FreeConsole()

    def set_title(self, hwnd: int, title: str) -> bool:
        return bool(self._user.SetWindowTextW(self._wintypes.HWND(int(hwnd)), str(title)))

    def get_title(self, hwnd: int) -> str:
        buffer = self._ctypes.create_unicode_buffer(512)
        self._user.GetWindowTextW(self._wintypes.HWND(int(hwnd)), buffer, 512)
        return str(buffer.value or "")


def _handle_state() -> dict:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
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
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetStdHandle.restype = wintypes.BOOL
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    for _name, code in _STD_CODES:
        kernel.SetStdHandle(ctypes.c_uint(code & 0xFFFFFFFF), wintypes.HANDLE(0))


def _try_spawn(count: int = 5) -> list[dict]:
    """The exact Fleet preflight shape: capture_output, no explicit stdin."""
    results: list[dict] = []
    for index in range(count):
        try:
            result = subprocess.run(
                [sys.executable, "-c", "print('ok')"],
                capture_output=True, text=True, timeout=30,
            )
            results.append({"index": index, "rc": result.returncode, "stdout": result.stdout.strip()[:20]})
        except Exception as exc:  # noqa: BLE001 - the failure IS the evidence
            results.append({"index": index, "error": f"{type(exc).__name__}: {exc}"})
    return results


def _run_variant(label: str, resolver: LegacyAttachResolver, pid: int, hwnd: int) -> dict:
    """Real title-guardian workload driven through the legacy attach resolver."""
    handles_before = _handle_state()
    attach_cycles: list[dict] = []
    for index in range(CYCLES):
        resolved = resolver.console_hwnd(pid)
        if resolved:
            hwnd = resolved
        # title-guardian activity: real SetWindowTextW drift + restore, which is
        # what the heartbeat/WinEvent path did on every pass.
        if hwnd:
            resolver.set_title(hwnd, GENERIC_TITLE)
            resolver.set_title(hwnd, CANONICAL_TITLE)
        spawn = None
        try:
            spawn = subprocess.run(
                [sys.executable, "-c", "print('ok')"],
                capture_output=True, text=True, timeout=30,
            )
            spawn = {"rc": spawn.returncode}
        except Exception as exc:  # noqa: BLE001
            spawn = {"error": f"{type(exc).__name__}: {exc}"}
        attach_cycles.append({"cycle": index, "resolved_hwnd": resolved, "spawn": spawn})
        time.sleep(0.05)

    handles_after = _handle_state()
    spawns = _try_spawn()
    errors = [item["error"] for item in spawns if item.get("error")]
    return {
        "label": label,
        "handles_before": handles_before,
        "handles_after": handles_after,
        "handle_mutation": handles_before != handles_after,
        "stdin_after": handles_after.get("stdin", ""),
        "stdin_stale_not_null": handles_after.get("stdin", "") not in ("", "null"),
        "attach_cycles": attach_cycles,
        "resolved_hwnd": int(hwnd or 0),
        "spawns": spawns,
        "winerror6_reproduced": any("WinError 6" in error for error in errors),
        "spawn_errors": errors,
    }


def run(report_path: Path, _workdir: Path) -> dict:
    report: dict = {"ok": False, "error": "", "steps": []}
    process = None
    try:
        report["handles_inherited"] = _handle_state()

        create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        process = subprocess.Popen(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-Command",
                f"[Console]::Title = '{CANONICAL_TITLE}'; Start-Sleep 300",
            ],
            creationflags=create_console,
        )
        report["launch_pid"] = int(process.pid)
        time.sleep(2.5)
        report["steps"].append("managed_console_started")

        resolver = LegacyAttachResolver()

        # Variant A: whatever handles the worker inherited.
        variant_a = _run_variant("inherited_handles", resolver, process.pid, 0)

        # Variant B: the real console-less GUI host shape.
        _null_standard_handles()
        variant_b = _run_variant("nulled_handles", resolver, process.pid, variant_a["resolved_hwnd"])

        report["variants"] = {
            "inherited_handles": variant_a,
            "nulled_handles": variant_b,
        }
        report["legacy_resolved_hwnd"] = int(variant_b["resolved_hwnd"])
        report["steps"].append("legacy_resolution_reproduced")
        report["handles_final"] = _handle_state()
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        import traceback

        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    return report


def main() -> int:
    if len(sys.argv) < 3:
        return 2
    report_path = Path(sys.argv[1])
    workdir = Path(sys.argv[2])
    report_path.write_text(
        json.dumps(run(report_path, workdir), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
