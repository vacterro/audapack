"""Put the worker windows where the operator wants them, on the monitor they said.

Six Chromium windows opened wherever Windows felt like opening them: stacked on
top of each other, half of them on the wrong display, and dragged into place by
hand after every restart.

Nothing here is Qt: monitors and windows come from Win32, so an arrangement can
be computed and tested without a screen. Every entry point is a no-op that says
so on anything but Windows, rather than raising into a GUI callback.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from math import ceil, sqrt
from pathlib import Path
from typing import Any, Iterable, Optional

#: Grid tiles the windows edge to edge; cascade overlaps them by a fixed step,
#: each one still grabbable by its title bar.
LAYOUT_GRID = "grid"
LAYOUT_CASCADE = "cascade"
LAYOUT_SLOTS = "slots"
LAYOUTS = (LAYOUT_GRID, LAYOUT_CASCADE, LAYOUT_SLOTS)

#: The fixed lattice LAYOUT_SLOTS fills. Unlike the grid, these numbers do not
#: move with the window count: one window takes one sixth of the display and
#: the other five cells stay empty, so a window is in the same place whether
#: there are two of them or six.
SLOT_COLUMNS = 3
SLOT_ROWS = 2

#: Step between cascaded windows, and the share of the monitor one of them
#: takes. Both are in the units the monitor rect is in.
CASCADE_STEP = 32
CASCADE_SIZE_RATIO = 0.62


@dataclass(frozen=True)
class Monitor:
    """One display, in virtual-desktop coordinates."""

    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def label(self) -> str:
        star = " (primary)" if self.primary else ""
        return f"{self.index + 1}: {self.width}x{self.height} at {self.x},{self.y}{star}"


def list_monitors() -> list[Monitor]:
    """Every display, in the order Windows enumerates them.

    An empty list means the platform could not be asked, which callers must
    read as "arrange nothing" rather than as "there are no monitors".
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", ctypes.c_wchar * 32)]

    MONITORINFOF_PRIMARY = 0x1
    monitors: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
    )

    def collect(handle, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return True
        # The WORK area, not the whole display: a window sized to the full
        # monitor hides its bottom edge behind the taskbar in every column.
        work = info.rcWork
        monitors.append(Monitor(
            index=len(monitors),
            name=str(info.szDevice or f"display {len(monitors) + 1}"),
            x=int(work.left), y=int(work.top),
            width=int(work.right - work.left), height=int(work.bottom - work.top),
            primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
        ))
        return True

    try:
        user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    except Exception:
        return []
    return monitors


def resolve_monitor(monitors: Iterable[Monitor], wanted: int) -> Optional[Monitor]:
    """The configured monitor, falling back to the primary, then to the first.

    A display unplugged since the setting was saved must not send six windows
    to coordinates nobody can see.
    """
    available = list(monitors)
    if not available:
        return None
    for monitor in available:
        if monitor.index == int(wanted):
            return monitor
    for monitor in available:
        if monitor.primary:
            return monitor
    return available[0]


def grid_shape(count: int) -> tuple[int, int]:
    """(columns, rows) for ``count`` windows -- six becomes 3x2, which is the ask."""
    if count <= 0:
        return (0, 0)
    rows = max(1, int(sqrt(count)))
    return (ceil(count / rows), rows)


def tile_geometry(count: int, monitor: Monitor) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) per window, filling the monitor edge to edge."""
    if count <= 0:
        return []
    cols, rows = grid_shape(count)
    cell_w = monitor.width // cols
    cell_h = monitor.height // rows
    places = []
    for i in range(count):
        col, row = i % cols, i // cols
        # The last column and row absorb the rounding, so the tiling reaches
        # the edge instead of leaving a strip of desktop showing down the side.
        width = monitor.width - cell_w * col if col == cols - 1 else cell_w
        height = monitor.height - cell_h * row if row == rows - 1 else cell_h
        places.append((monitor.x + cell_w * col, monitor.y + cell_h * row, width, height))
    return places


def cascade_geometry(
    count: int, monitor: Monitor, step: int = CASCADE_STEP
) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) per window, each offset from the one before it."""
    if count <= 0:
        return []
    width = int(monitor.width * CASCADE_SIZE_RATIO)
    height = int(monitor.height * CASCADE_SIZE_RATIO)
    # The last window still has to land on the monitor, so the step shrinks
    # before the stack is allowed to walk off the bottom-right corner.
    span = max(1, count - 1)
    step = max(8, min(step, (monitor.width - width) // span, (monitor.height - height) // span))
    return [
        (monitor.x + step * i, monitor.y + step * i, width, height)
        for i in range(count)
    ]


def slot_geometry(
    count: int,
    monitor: Monitor,
    columns: int = SLOT_COLUMNS,
    rows: int = SLOT_ROWS,
) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) per window, into a lattice that never resizes.

    The grid divides the display by however many windows there are, so opening
    a second one halves the first. Here the cells are fixed: windows land in
    them one after another, starting at the BOTTOM-LEFT, right along the bottom
    row, then up to the top-left and right again.

    Past the lattice it wraps back to the bottom-left and overlaps rather than
    walking off the display -- a seventh window is a bug to see, not one to
    lose behind the edge of the screen.
    """
    if count <= 0:
        return []
    columns = max(1, int(columns))
    rows = max(1, int(rows))
    cell_w = monitor.width // columns
    cell_h = monitor.height // rows
    places = []
    for i in range(count):
        col = i % columns
        row_from_bottom = (i // columns) % rows
        row_from_top = rows - 1 - row_from_bottom
        # The last column and the bottom row absorb the rounding, so the
        # lattice reaches the right and bottom edges of the work area.
        width = monitor.width - cell_w * col if col == columns - 1 else cell_w
        height = monitor.height - cell_h * row_from_top if row_from_top == rows - 1 else cell_h
        places.append((monitor.x + cell_w * col, monitor.y + cell_h * row_from_top, width, height))
    return places


def layout_geometry(layout: str, count: int, monitor: Monitor) -> list[tuple[int, int, int, int]]:
    if str(layout) == LAYOUT_CASCADE:
        return cascade_geometry(count, monitor)
    if str(layout) == LAYOUT_SLOTS:
        return slot_geometry(count, monitor)
    return tile_geometry(count, monitor)


def find_profile_windows(profile_dir: Path, backend: Optional[Any] = None) -> list[int]:
    """Top-level window handles belonging to the dedicated worker profile.

    Matched on the browser's ``--user-data-dir``, which is the only thing that
    separates a worker window from the operator's own browsing. Every window in
    that profile counts, a hand-opened one included: that is what is actually on
    the display, and arranging half of them is worse than arranging none.
    """
    if backend is None:
        from audapack.instances import create_window_backend

        backend = create_window_backend()
    needle = str(profile_dir).rstrip("\\/").lower()
    if not needle:
        return []
    handles = []
    for window in backend.list_windows():
        command = str(getattr(window, "command_line", "") or "").lower()
        if needle in command:
            handles.append(int(window.hwnd))
    return handles


def arrange_windows(
    handles: Iterable[int],
    places: Iterable[tuple[int, int, int, int]],
    minimize: bool = False,
) -> int:
    """Move each window to its place, then optionally minimize it.

    Minimizing is safe for a worker: the dedicated profile is launched with
    occlusion detection and every backgrounding throttle switched off, which is
    what those flags are there for. The window keeps running its audit; the
    operator gets their desktop back and finds it where it was put.

    Returns how many windows were actually moved.
    """
    if sys.platform != "win32":
        return 0
    import ctypes

    user32 = ctypes.windll.user32
    SW_RESTORE, SW_SHOWMINNOACTIVE = 9, 7
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010

    moved = 0
    for hwnd, (x, y, width, height) in zip(handles, places, strict=False):
        try:
            # A maximized or minimized window ignores SetWindowPos, so it is
            # restored first or it never leaves where it already was.
            user32.ShowWindow(hwnd, SW_RESTORE)
            if not user32.SetWindowPos(
                hwnd, 0, int(x), int(y), int(width), int(height), SWP_NOZORDER | SWP_NOACTIVATE
            ):
                continue
            if minimize:
                user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
            moved += 1
        except Exception:
            continue
    return moved
