"""Windows Scheduled Task autostart manager for AUDAPACK Bridge."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from audapack.config import app_dir

TASK_NAME = "AUDAPACK Bridge"


def get_pythonw_executable() -> Path:
    py_dir = Path(sys.executable).parent
    pythonw = py_dir / "pythonw.exe"
    if pythonw.exists():
        return pythonw
    return Path(sys.executable)


def get_canonical_autostart_command() -> str:
    pythonw = get_pythonw_executable()
    entrypoint = app_dir() / "AUDAPACK.pyw"
    return f'"{pythonw}" "{entrypoint}" --bridge'


def query_task(task_name: str = TASK_NAME) -> tuple[bool, dict[str, str]]:
    """Queries a Scheduled Task using schtasks /query /fo LIST /v."""
    if sys.platform != "win32":
        return False, {}

    try:
        res = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode != 0:
            return False, {}

        info: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                info[key.strip()] = val.strip()
        return True, info
    except Exception:
        return False, {}


def get_autostart_status() -> dict[str, Any]:
    """
    Returns full autostart health and status:
    - installed: bool
    - is_healthy: bool (command matches current repo path)
    - status_text: 'INSTALLED' | 'NOT_INSTALLED' | 'BROKEN'
    - actual_command: str
    - expected_command: str
    """
    expected_cmd = get_canonical_autostart_command()
    exists, info = query_task(TASK_NAME)

    if not exists:
        return {
            "installed": False,
            "is_healthy": False,
            "status_text": "NOT_INSTALLED",
            "task_name": TASK_NAME,
            "actual_command": "",
            "expected_command": expected_cmd,
            "task_state": "",
        }

    actual_cmd = info.get("Task To Run", "")
    task_state = info.get("Status", info.get("Scheduled Task State", ""))

    # Normalize paths for comparison (case-insensitive on Windows)
    is_matching = actual_cmd.strip().lower() == expected_cmd.strip().lower()

    return {
        "installed": True,
        "is_healthy": is_matching,
        "status_text": "INSTALLED" if is_matching else "BROKEN",
        "task_name": TASK_NAME,
        "actual_command": actual_cmd,
        "expected_command": expected_cmd,
        "task_state": task_state,
    }


def install_autostart() -> tuple[bool, str]:
    """Creates/Updates the Scheduled Task 'AUDAPACK Bridge' to start at user logon."""
    if sys.platform != "win32":
        return False, "Scheduled Tasks are only supported on Windows."

    cmd_str = get_canonical_autostart_command()
    try:
        res = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/tr", cmd_str, "/sc", "ONLOGON", "/f", "/rl", "LIMITED"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode == 0:
            return True, f"Autostart task '{TASK_NAME}' installed successfully."
        return False, f"schtasks error: {res.stderr.strip() or res.stdout.strip()}"
    except Exception as exc:
        return False, f"Failed to execute schtasks: {exc}"


def remove_autostart() -> tuple[bool, str]:
    """Deletes the Scheduled Task 'AUDAPACK Bridge'."""
    if sys.platform != "win32":
        return False, "Scheduled Tasks are only supported on Windows."

    try:
        res = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode == 0:
            return True, f"Autostart task '{TASK_NAME}' removed."
        return False, f"schtasks error: {res.stderr.strip() or res.stdout.strip()}"
    except Exception as exc:
        return False, f"Failed to remove task: {exc}"


def repair_autostart() -> tuple[bool, str]:
    """Repairs the autostart task by re-installing with the current canonical repo path."""
    return install_autostart()


def run_autostart_task() -> tuple[bool, str]:
    """Manually triggers the Scheduled Task."""
    if sys.platform != "win32":
        return False, "Scheduled Tasks are only supported on Windows."

    try:
        res = subprocess.run(
            ["schtasks", "/run", "/tn", TASK_NAME],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode == 0:
            return True, f"Task '{TASK_NAME}' started."
        return False, f"Failed to run task: {res.stderr.strip()}"
    except Exception as exc:
        return False, str(exc)


def stop_autostart_task() -> tuple[bool, str]:
    """Stops any running instance of the Scheduled Task."""
    if sys.platform != "win32":
        return False, "Scheduled Tasks are only supported on Windows."

    try:
        res = subprocess.run(
            ["schtasks", "/end", "/tn", TASK_NAME],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode == 0:
            return True, f"Task '{TASK_NAME}' stopped."
        return False, f"Failed to stop task: {res.stderr.strip()}"
    except Exception as exc:
        return False, str(exc)
