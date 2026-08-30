"""Windows Shell Context Menu integration for AUDAPACK.

Registers per-user Explorer context menu entries under HKCU:
- Directory\\shell\\AUDAPACK
- *\\shell\\AUDAPACK
Label: 'Упаковать через AUDAPACK'
Action: invokes pythonw AUDAPACK.pyw --pack "%1"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

CONTEXT_MENU_TITLE = "Упаковать через AUDAPACK"
REG_KEY_DIRECTORY = r"Software\Classes\Directory\shell\AUDAPACK"
REG_KEY_FILE = r"Software\Classes\*\shell\AUDAPACK"


def get_launcher_command(script_path: Optional[Path] = None) -> str:
    """Builds the exact command line string for context menu invocation."""
    script = script_path or (Path(__file__).resolve().parent.parent / "AUDAPACK.pyw")

    # Find pythonw.exe
    py_dir = Path(sys.executable).parent
    pythonw = py_dir / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable)

    return f'"{pythonw}" "{script}" --pack "%1"'


def is_context_menu_installed() -> bool:
    """Checks if AUDAPACK context menu is registered under HKCU."""
    if os.name != "nt":
        return False
    try:
        import winreg

        # Check command subkey first
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{REG_KEY_DIRECTORY}\command", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "")
                if val and ("AUDAPACK" in val or "python" in val.lower()):
                    return True
        except Exception:
            pass

        # Check shell root key
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_DIRECTORY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "")
            if val and (val == CONTEXT_MENU_TITLE or "AUDAPACK" in val or "Упаковать" in val or "Pack" in val):
                return True
    except Exception:
        pass
    return False


def install_context_menu(script_path: Optional[Path] = None) -> bool:
    """Installs context menu entries for folders and files in HKCU."""
    if os.name != "nt":
        return False
    try:
        import winreg

        cmd = get_launcher_command(script_path)
        created: list[str] = []

        for base_key in [REG_KEY_DIRECTORY, REG_KEY_FILE]:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, CONTEXT_MENU_TITLE)
            created.append(base_key)
            command_key = rf"{base_key}\command"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, command_key) as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)
            created.append(command_key)

        return True
    except Exception:
        for key_path in reversed(created):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                pass
        return False


def remove_context_menu() -> bool:
    """Removes context menu entries from HKCU.

    W2-010: only a genuinely absent key is treated as idempotent success.
    Permission/access failures on an existing key are recorded and reported as
    failure so the UI never claims removal that did not happen.
    """
    if os.name != "nt":
        return False
    try:
        import winreg
    except Exception:
        return False

    errors: list[str] = []
    for base_key in [REG_KEY_DIRECTORY, REG_KEY_FILE]:
        for key_path in (rf"{base_key}\command", base_key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError) or getattr(exc, "winerror", None) == 2:
                    continue
                errors.append(f"{key_path}: {exc}")
            except Exception as exc:
                errors.append(f"{key_path}: {exc}")

    return not errors
