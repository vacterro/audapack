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

from audapack.config import (
    AppConfig,
    app_dir,
    cross_process_lock,
    get_bridge_runtime_dir,
    load_config,
    open_new_temp_file,
)
from audapack.procutil import run_hidden

PID_FILE_NAME = "bridge.pid"
INSTANCE_NONCE = uuid.uuid4().hex
#: Serializes PID publication against compare-and-delete (W2-004). Without it,
#: write_pid(B) landing between remove_pid(A)'s read and unlink erases B.
_PID_LOCK_NAME = "bridge_pid.lock"


def _pid_lock_path(base_dir: Optional[Path] = None) -> Path:
    return get_pid_file(base_dir).with_name(_PID_LOCK_NAME)


def get_pid_file(base_dir: Optional[Path] = None) -> Path:
    if base_dir:
        return base_dir / PID_FILE_NAME
    return get_bridge_runtime_dir() / PID_FILE_NAME


def write_pid(base_dir: Optional[Path] = None):
    """Publish this process's ownership record atomically (W2-004).

    `Path.write_text` straight onto bridge.pid made a transient unreadable or
    PARTIAL read possible during the write -- and remove_pid treated an
    unreadable identity as "no objection", authorizing deletion. Staged temp,
    fsync, replace, under the same lock deletion takes.
    """
    p_file = get_pid_file(base_dir)
    with cross_process_lock(_pid_lock_path(base_dir)):
        payload = {
            "pid": os.getpid(),
            "nonce": INSTANCE_NONCE,
            "executable": str(Path(sys.executable).resolve()),
            "started_at": time.time(),
        }
        fd, tmp_file = open_new_temp_file(p_file.parent, p_file.name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            tmp_file.replace(p_file)
        except Exception:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise


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
    """Compare-and-delete the PID record, failing CLOSED on unreadable identity.

    W2-004 (audit/4.md): the comparison only rejected mismatches when the
    current value was TRUTHY, so an absent, partial or unreadable identity
    authorized deletion. Combined with the old non-atomic write_pid, a transient
    partial read was a real window: measured, remove_pid(expected_pid=111)
    deleted a file whose reread returned `{}`. Deletion now requires every
    SUPPLIED expected field to be present and exactly equal; the read and the
    unlink happen under the same cross-process lock as publication, closing the
    read/replace/unlink TOCTOU with a starting successor.
    """
    p_file = get_pid_file(base_dir)
    with cross_process_lock(_pid_lock_path(base_dir)):
        if not p_file.exists():
            return
        current = read_pid(base_dir)
        try:
            cur_pid = int(current.get("pid", 0)) if current else 0
        except (TypeError, ValueError):
            cur_pid = 0
        cur_nonce = str(current.get("nonce", "")) if current else ""

        if expected_pid is not None:
            if not cur_pid or cur_pid != int(expected_pid):
                # Unreadable identity is not consent: fail closed, leave the
                # record alone.
                return
        if expected_nonce:
            if not cur_nonce or cur_nonce != str(expected_nonce):
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
    """Gracefully stops the AUDAPACK Bridge daemon via authenticated shutdown.

    W2-004: the target's identity is captured from the LIVE health response
    BEFORE the shutdown request, and that exact identity is what cleanup
    compares against. The old path read `bridge.pid` AFTER the endpoint went
    offline, so a successor Bridge that started in that gap had its valid
    ownership record deleted by the retiring controller.
    """
    cfg = config or load_config()

    # Capture the target's identity while it is still alive and answering.
    healthy, health = check_bridge_health(cfg.bridge.host, cfg.bridge.port, timeout=0.8)
    target_pid: Optional[int] = None
    target_nonce = ""
    if healthy:
        try:
            candidate = int(health.get("pid") or 0)
        except (TypeError, ValueError):
            candidate = 0
        if candidate:
            target_pid = candidate
        target_nonce = str(health.get("instance_nonce") or "")

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
            # Bound cleanup to the identity captured BEFORE shutdown. The PID
            # file on disk at this moment may already belong to a successor.
            if target_pid is not None:
                remove_pid(expected_pid=target_pid, expected_nonce=target_nonce or None)
            else:
                # Never saw a healthy Bridge: nothing to bind to. Only remove
                # when the on-disk record is provably empty of a DIFFERENT
                # owner, which remove_pid's fail-closed check enforces.
                remove_pid()
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
                result = run_hidden(
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
