"""Hidden subprocess helpers (P0-1 pattern, shared).

AUDAPACK's GUI (``pythonw``) and the Bridge daemon run without a console.
Spawning a console utility (``powershell``, ``schtasks``, ``wmic``,
``taskkill``, ``git``, ...) from such a process makes Windows allocate a NEW
console: a black window that flashes on screen and steals focus. Every spawn
of an external tool must therefore opt into CREATE_NO_WINDOW plus
STARTF_USESHOWWINDOW/SW_HIDE, or it will flash -- exactly the symptom seen
when the Chromium worker launcher detected installed browsers.
"""

from __future__ import annotations

import subprocess
import sys


def hidden_spawn_kwargs(**kwargs) -> dict:
    """Return *kwargs* extended with the no-console-window flags on Windows."""
    if sys.platform != "win32":
        return dict(kwargs)
    si = subprocess.STARTUPINFO()
    si.dwFlags = subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    kwargs.setdefault("startupinfo", si)
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return kwargs


def run_hidden(args, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` that never flashes a console window."""
    return subprocess.run(args, **hidden_spawn_kwargs(**kwargs))


def popen_hidden(args, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` that never flashes a console window."""
    return subprocess.Popen(args, **hidden_spawn_kwargs(**kwargs))
