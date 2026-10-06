"""Local release gate for `saipen ship` (`--gate core`).

This is the FAST half of CI, on purpose: the release gate runs once per ship,
so it must stay cheap and deterministic and offline. It covers the two checks
that can silently corrupt a shipped artifact -- lint drift, and a widget whose
served bytes no longer match the recorded release ledger.

The slow half (`python -m pytest`) is not duplicated here; it runs in CI and is
recorded per ticket as the VERIFY evidence.

Usage: python tools/validate.py --gate core
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(label: str, argv: list[str]) -> bool:
    result = subprocess.run(argv, cwd=str(ROOT))
    if result.returncode:
        print(f"GATE FAIL [{label}]: {' '.join(argv)} exited {result.returncode}")
        return False
    print(f"GATE PASS [{label}]")
    return True


def gate_core() -> bool:
    checks = [
        ("ruff", [sys.executable, "-m", "ruff", "check", "audapack", "tests"]),
        (
            "widget-release-ledger",
            [sys.executable, "scripts/update_widget_release.py", "--check"],
        ),
    ]
    return all(_run(label, argv) for label, argv in checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", default="core", choices=["core"])
    args = parser.parse_args(argv)
    return 0 if gate_core() else 1


if __name__ == "__main__":
    raise SystemExit(main())