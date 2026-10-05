"""Prove the live T-25 clause: a fresh launcher opens a REAL window.

    python scripts/probe_real_launcher.py

This cannot be asserted from pytest: it needs an interactive window station
that the test runner does not own, so a headless run can only report "could
not verify". Run it from a normal desktop session, the same way the operator
does (double-click AUDAPACK.vbs), and read the printed verdict.

It starts AUDAPACK as its OWN child process, waits for a window whose title
matches the production classifier, prints the verdict, and terminates that
child BY PID. It never touches any other process.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audapack.single_instance import SingleInstance  # noqa: E402

WINDOW_TIMEOUT_SECONDS = 90.0
POLL_SECONDS = 0.5


def main() -> int:
    # The production classifier itself: a probe with its own window matcher
    # would prove nothing about the predicate the launcher actually uses.
    # Constructing the guard does not acquire the mutex (that happens in
    # is_already_running), so this never joins the guard's namespace.
    probe = SingleInstance("AUDAPACK_GUI_PROBE")
    child = subprocess.Popen(
        [sys.executable, "-m", "audapack.app"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"launched AUDAPACK as PID {child.pid}")
    try:
        deadline = time.time() + WINDOW_TIMEOUT_SECONDS
        while time.time() < deadline:
            if child.poll() is not None:
                print(f"VERDICT: FAIL -- launcher exited with code {child.returncode} "
                      f"before a window appeared")
                return 1
            hwnd = probe._find_window_hwnd()
            if hwnd:
                print(f"VERDICT: PASS -- a real AUDAPACK window is visible (HWND {hwnd})")
                return 0
            time.sleep(POLL_SECONDS)
        print("VERDICT: FAIL -- no AUDAPACK window became visible within "
              f"{WINDOW_TIMEOUT_SECONDS:.0f}s")
        return 1
    finally:
        # Only the process this script started, by PID.
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


if __name__ == "__main__":
    raise SystemExit(main())
