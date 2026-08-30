"""Lifecycle management, PID tracking, and process health for AUDAPACK Bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from audapack.config import AppConfig, app_dir, get_bridge_runtime_dir, load_config

PID_FILE_NAME = "bridge.pid"
INSTANCE_NONCE = uuid.uuid4().hex


def get_pid_file(base_dir: Optional[Path] = None) -> Path:
    if base_dir:
        return base_dir / PID_FILE_NAME
    return get_bridge_runtime_dir() / PID_FILE_NAME


def write_pid(base_dir: Optional[Path] = None):
    p_file = get_pid_file(base_dir)
    p_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "nonce": INSTANCE_NONCE,
        "executable": str(Path(sys.executable).resolve()),
        "started_at": time.time(),
    }
    p_file.write_text(json.dumps(payload), encoding="utf-8")


def read_pid(base_dir: Optional[Path] = None) -> dict[str, Any]:
    p_file = get_pid_file(base_dir)
    try:
        data = json.loads(p_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        return {"pid": int(p_file.read_text(encoding="utf-8").strip()), "nonce": ""}
    except Exception:
        return {}


def remove_pid(base_dir: Optional[Path] = None, expected_pid: Optional[int] = None, expected_nonce: Optional[str] = None):
    """Compare-and-delete the PID file.

    W2-010: PID creation records ownership, but deletion MUST also enforce it.
    Accepts an optional expected pid and/or nonce; rereads the file immediately
    before unlink, and only removes the record when it still belongs to the
    caller. A server `finally` or a controller stopping a Bridge must pass its
    own identity so it can never unlink another live instance's PID record.
    """
    p_file = get_pid_file(base_dir)
    if not p_file.exists():
        return
    current = read_pid(base_dir)
    try:
        cur_pid = int(current.get("pid", 0)) if current else 0
    except (TypeError, ValueError):
        cur_pid = 0
    cur_nonce = str(current.get("nonce", "")) if current else ""

    if expected_pid is not None and cur_pid and cur_pid != int(expected_pid):
        return
    if expected_nonce and cur_nonce and cur_nonce != expected_nonce:
        return
    try:
        p_file.unlink()
    except OSError:
        pass


def check_bridge_health(host: str = "127.0.0.1", port: int = 17843, timeout: float = 1.2) -> tuple[bool, dict[str, Any]]:
    """
    Queries /health on loopback.
    Returns (is_healthy, payload_or_error).
    Ensures service == 'AUDAPACK Bridge' and api_version in (2, 3).
    """
    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                svc = data.get("service")
                api_ver = data.get("api_version")
                if svc == "AUDAPACK Bridge" and (api_ver in (2, 3) or bool(data.get("supported_api_versions"))):
                    return True, data
                elif svc == "ACBBridge":
                    return False, {"status": "legacy_acbbridge", "raw": data}
                else:
                    return False, {"status": "wrong_service", "raw": data}
            return False, {"status": f"http_{resp.status}"}
    except Exception as exc:
        return False, {"status": "offline", "error": str(exc)}


def is_bridge_healthy(host: str = "127.0.0.1", port: int = 17843, timeout: float = 1.2) -> bool:
    healthy, _ = check_bridge_health(host, port, timeout)
    return healthy


def start_bridge_background(config: Optional[AppConfig] = None) -> bool:
    """Starts AUDAPACK Bridge in a silent background process."""
    cfg = config or load_config()
    if is_bridge_healthy(cfg.bridge.host, cfg.bridge.port):
        return True

    python_exe = sys.executable
    py_dir = Path(python_exe).parent
    pythonw = py_dir / "pythonw.exe"
    runner = str(pythonw) if (pythonw.exists() and sys.platform == "win32") else str(python_exe)

    entry_pyw = app_dir() / "AUDAPACK.pyw"
    if entry_pyw.exists():
        cmd = [runner, str(entry_pyw), "--bridge"]
    else:
        cmd = [runner, "-m", "audapack.app", "--bridge"]

    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        subprocess.Popen(
            cmd,
            cwd=str(app_dir()),
            creationflags=creation_flags,
            close_fds=True,
        )
    except Exception:
        return False

    # Poll for health
    for _ in range(30):
        time.sleep(0.1)
        if is_bridge_healthy(cfg.bridge.host, cfg.bridge.port):
            return True
    return False


def stop_bridge(config: Optional[AppConfig] = None) -> tuple[bool, str]:
    """Gracefully stops the AUDAPACK Bridge daemon via authenticated shutdown."""
    cfg = config or load_config()
    url = f"http://{cfg.bridge.host}:{cfg.bridge.port}/v1/shutdown"
    token = cfg.bridge.token

    try:
        req = urllib.request.Request(url, data=b"{}", headers={"X-ACB-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5):
            pass
    except Exception:
        pass

    # Wait for process to exit
    for _ in range(15):
        time.sleep(0.1)
        if not is_bridge_healthy(cfg.bridge.host, cfg.bridge.port, timeout=0.3):
            identity = read_pid()
            remove_pid(expected_pid=identity.get("pid"), expected_nonce=identity.get("nonce"))
            return True, "Bridge stopped successfully."

    # Fallback: only kill a process whose recorded identity matches the live Bridge.
    identity = read_pid()
    try:
        pid = int(identity.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if pid:
        healthy, health = check_bridge_health(cfg.bridge.host, cfg.bridge.port, timeout=0.5)
        recorded_nonce = str(identity.get("nonce", ""))
        live_nonce = str(health.get("instance_nonce", "")) if healthy else ""
        if not recorded_nonce or not live_nonce or recorded_nonce != live_nonce:
            return False, "Refusing fallback kill: PID/Bridge identity cannot be verified."
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return False, f"Failed to stop bridge PID {pid}: {result.stderr.strip() or result.stdout.strip()}"
            else:
                os.kill(pid, 9)
            for _ in range(15):
                time.sleep(0.1)
                if not is_bridge_healthy(cfg.bridge.host, cfg.bridge.port, timeout=0.3):
                    remove_pid(expected_pid=pid, expected_nonce=recorded_nonce)
                    return True, f"Bridge stopped (PID {pid})."
            return False, "Bridge process remained healthy after fallback stop."
        except Exception as exc:
            return False, f"Failed to stop bridge PID: {exc}"

    if not is_bridge_healthy(cfg.bridge.host, cfg.bridge.port, timeout=0.3):
        return True, "Bridge is not running."

    return False, "Failed to stop bridge gracefully."
