"""POSIX simulation: run the T-25 test files as if as on Linux.

On this Windows box we cannot install a real POSIX Python, so emulate the
facts that matter for portability:

1. sys.platform == "linux" (skip guards evaluate against it at import time);
2. ctypes has no `windll` / `WinDLL` / `oledll` (attribute access fails exactly
   as on POSIX);
3. `os.getuid`/`os.getgid` exist (real Linux has them; pytest's tmp_path
   naming calls them once the platform reads as POSIX);
4. `fcntl.flock` exists (real Linux has it; emulated here with Windows
   byte-range locks so the POSIX guard branch is exercised, not skipped).

Modules that branch on sys.platform at import time (asyncio, PySide6) are
imported BEFORE faking so their Windows code paths stay cached.

Usage: python _linux_sim.py [test files or node ids...]  (defaults to the
T-25 portability set)
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio  # noqa: F401  (pre-import before faking the platform)
from PySide6 import QtWidgets  # noqa: F401

import ctypes

for _attr in ("windll", "WinDLL", "oledll"):
    if hasattr(ctypes, _attr):
        delattr(ctypes, _attr)

os.getuid = lambda: 0  # type: ignore[attr-defined]
os.getgid = lambda: 0  # type: ignore[attr-defined]

# A minimal fcntl with real lock-conflict semantics (msvcrt byte-range locks
# conflict across handles and processes just like flock).
import msvcrt

fcntl = types.ModuleType("fcntl")
fcntl.LOCK_SH = 1
fcntl.LOCK_EX = 2
fcntl.LOCK_NB = 4
fcntl.LOCK_UN = 8


def _flock(fd, op):
    if not isinstance(fd, int):
        fd = fd.fileno()  # real fcntl.flock accepts a file object too
    if op & fcntl.LOCK_UN:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    elif op & fcntl.LOCK_NB:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


fcntl.flock = _flock
sys.modules["fcntl"] = fcntl

sys.platform = "linux"

import pytest

DEFAULT = [
    "tests/test_launcher_windowless_recovery.py",
    "tests/test_single_instance.py",
    "tests/test_qt_launcher_visibility.py",
    "tests/test_qt_app_icon.py",
    "tests/test_docs_consistency.py",
]
paths = [a for a in sys.argv[1:] if not a.startswith("-")]
ARGS = (paths or DEFAULT) + [a for a in sys.argv[1:] if a.startswith("-")] + ["-v", "--no-header"]

raise SystemExit(pytest.main(ARGS))
