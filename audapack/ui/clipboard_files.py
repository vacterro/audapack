"""Windows clipboard file-drop helper (CF_HDROP).

Places one or more file paths onto the Windows clipboard in the standard
CF_HDROP format used by Explorer when you select files and press ``Ctrl+C``.
Any application that accepts file drops (Explorer, 7-Zip, email clients,
chat apps) can then paste the files as real files, not as text.

This module is import-safe on non-Windows: all Win32 bindings live behind a
``sys.platform`` guard, so importing never touches ``ctypes.windll``. The
public copy helpers return ``False`` cleanly on non-Windows so callers can
degrade gracefully; the pure payload builder stays testable everywhere except
that it requires the Win32 layout (guarded identically).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

import ctypes
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# Win32 constants (portable: plain integers, no DLL binding)
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002


if IS_WINDOWS:
    class _Kernel32:
        GlobalAlloc = ctypes.windll.kernel32.GlobalAlloc
        GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        GlobalAlloc.restype = wintypes.HGLOBAL

        GlobalLock = ctypes.windll.kernel32.GlobalLock
        GlobalLock.argtypes = [wintypes.HGLOBAL]
        GlobalLock.restype = wintypes.LPVOID

        GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock
        GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        GlobalUnlock.restype = wintypes.BOOL

        GlobalFree = ctypes.windll.kernel32.GlobalFree
        GlobalFree.argtypes = [wintypes.HGLOBAL]
        GlobalFree.restype = wintypes.HGLOBAL

        GetLastError = ctypes.windll.kernel32.GetLastError
        GetLastError.argtypes = []
        GetLastError.restype = wintypes.DWORD

    class _User32:
        OpenClipboard = ctypes.windll.user32.OpenClipboard
        OpenClipboard.argtypes = [wintypes.HWND]
        OpenClipboard.restype = wintypes.BOOL

        CloseClipboard = ctypes.windll.user32.CloseClipboard
        CloseClipboard.argtypes = []
        CloseClipboard.restype = wintypes.BOOL

        EmptyClipboard = ctypes.windll.user32.EmptyClipboard
        EmptyClipboard.argtypes = []
        EmptyClipboard.restype = wintypes.BOOL

        SetClipboardData = ctypes.windll.user32.SetClipboardData
        SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        SetClipboardData.restype = wintypes.HANDLE

        GetLastError = ctypes.windll.kernel32.GetLastError
        GetLastError.argtypes = []
        GetLastError.restype = wintypes.DWORD


# DROPFILES layout — see https://learn.microsoft.com/windows/win32/api/shlobj_core/ns-shlobj_core-dropfiles
class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


def build_dropfiles_payload(file_paths: list[Path]) -> Optional[bytes]:
    """Build the raw DROPFILES payload as a single contiguous byte buffer.

    Pure logic: encodes paths as UTF-16LE, double-null terminated, behind the
    DROPFILES header. The header is constructed deterministically with
    ``struct.pack`` (little-endian 5 × 4-byte fields) so it is testable on any
    platform without requiring a running Windows or ctypes.windll.
    Returns ``None`` if any path is missing or empty.
    """
    import struct

    cleaned: list[str] = []
    for p in file_paths:
        try:
            resolved = str(Path(p).resolve(strict=False))
        except Exception:
            resolved = str(p)
        if not resolved:
            return None
        cleaned.append(resolved)

    # DROPFILES header: 5 × 4-byte little-endian fields (pFiles, pt_x, pt_y, fNC, fWide).
    # fNC = 0 (not a clipboard), fWide = 1 (wide-character paths).
    payload_offset = 20
    header = struct.pack("<5i", payload_offset, 0, 0, 0, 1)
    encoded = ("\0".join(cleaned) + "\0\0").encode("utf-16-le")
    return header + encoded


# Historical private alias kept so any existing internal callers keep working.
_build_dropfiles_payload = build_dropfiles_payload


def copy_files_to_clipboard(file_paths: Iterable[Path | str]) -> bool:
    """Place ``file_paths`` on the Windows clipboard as a CF_HDROP drop.

    Returns ``True`` on success, ``False`` on any failure (non-Windows, empty
    list, missing files, Win32 errors). Errors are swallowed silently because
    the caller treats clipboard state as best-effort — surface-level status
    text is the user's source of truth, not exception traces.
    """
    if not IS_WINDOWS or sys.platform != "win32":
        return False

    paths = [Path(p) for p in file_paths if p]
    if not paths:
        return False

    payload = build_dropfiles_payload(paths)
    if payload is None:
        return False

    hMem = _Kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not hMem:
        return False

    pMem = _Kernel32.GlobalLock(hMem)
    if not pMem:
        _Kernel32.GlobalFree(hMem)
        return False

    try:
        ctypes.memmove(pMem, payload, len(payload))
    finally:
        _Kernel32.GlobalUnlock(hMem)

    if not _User32.OpenClipboard(0):
        _Kernel32.GlobalFree(hMem)
        return False

    try:
        _User32.EmptyClipboard()
        result = _User32.SetClipboardData(CF_HDROP, hMem)
        if not result:
            _Kernel32.GlobalFree(hMem)
            return False
        # On success the clipboard owns the handle; do NOT free it.
        return True
    finally:
        _User32.CloseClipboard()


def copy_file_to_clipboard(file_path: Path | str) -> bool:
    """Convenience wrapper for the common single-file case."""
    return copy_files_to_clipboard([file_path])
