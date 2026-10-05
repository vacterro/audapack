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

#: API versions this Bridge build can talk. One canonical set shared by the
#: server advertisement and the client health probe (W2-004 / SRC-041:R008).
SUPPORTED_API_VERSIONS = (2, 3)
CLIENT_SUPPORTED_API_VERSIONS = frozenset(SUPPORTED_API_VERSIONS)


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


def _normalized_supported_api_versions(value: Any) -> set[int]:
    """Valid integer members of an advertised supported-version collection.

    W2-004 (SRC-041:R008): `bool(supported_api_versions)` accepted ANY truthy
    form as compatible -- the string "3", the dict {"3": True}, the bare int 99.
    Normalize to a set of real ints (bool excluded) and drop invalid members;
    the caller still requires a non-empty intersection.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    versions: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            versions.add(item)
    return versions


def check_bridge_health(host: str = "127.0.0.1", port: int = 17843, timeout: float = 1.2) -> tuple[bool, dict[str, Any]]:
    """
    Queries /health on loopback.
    Returns (is_healthy, payload_or_error).
    Healthy only when service == 'AUDAPACK Bridge' AND the primary api_version
    or an advertised supported_api_versions member is in the client set.
    """
    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                svc = data.get("service")
                if svc == "ACBBridge":
                    return False, {"status": "legacy_acbbridge", "raw": data}
                if svc != "AUDAPACK Bridge":
                    return False, {"status": "wrong_service", "raw": data}
                api_ver = data.get("api_version")
                primary_ok = (
                    not isinstance(api_ver, bool)
                    and isinstance(api_ver, int)
                    and api_ver in CLIENT_SUPPORTED_API_VERSIONS
                )
                advertised = _normalized_supported_api_versions(
                    data.get("supported_api_versions")
                )
                if primary_ok or (advertised & CLIENT_SUPPORTED_API_VERSIONS):
                    return True, data
                # A real Bridge with no shared protocol version is a version
                # skew, not the wrong service.
                return False, {"status": "incompatible_api_version", "raw": data}
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

    W2-003 (audit/6.md): capturing the identity is not enough -- it has to stay
    the IMMUTABLE subject of the whole operation. Two paths still rebound
    themselves to whoever currently held the endpoint: the offline branch
    deleted the PID record with no expected identity when no live Bridge had
    ever answered, and the fallback discarded the captured identity entirely,
    re-read bridge.pid plus /health and validated those two CURRENT values
    against EACH OTHER -- which a successor satisfies perfectly. Both now refuse
    rather than act on an unverified or changed identity.
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
            if target_pid is None and not target_nonce:
                # W2-003 (audit/6.md): this used to call remove_pid() with NO
                # expected identity, and remove_pid only compares the fields it
                # was actually given -- so "never saw a healthy Bridge"
                # authorized an unconditional unlink of whatever record was on
                # disk, including a successor's. Nothing was verified, so
                # nothing is deleted. A nonce WITHOUT a pid is still a verified
                # identity and is still bound below.
                return True, (
                    "Bridge is not answering; no live identity was verified, so its "
                    "ownership record was left untouched."
                )
            remove_pid(expected_pid=target_pid, expected_nonce=target_nonce or None)
            return True, "Bridge stopped successfully."

    # Fallback: force only the ORIGINAL target, and only while that target is
    # provably still the process on the endpoint.
    #
    # W2-003: this branch used to DISCARD the captured identity, re-read
    # bridge.pid and /health, and validate those two CURRENT identities against
    # each other -- which a successor satisfies perfectly. Measured: A(pid=111)
    # is asked to stop, B(pid=222) takes the endpoint over with no observable
    # offline interval, and the fallback called os.kill(222, 9). The captured
    # target is the immutable subject of this operation from here on.
    if target_pid is None or not target_nonce:
        return False, "Refusing fallback kill: no live Bridge identity was verified before shutdown."

    identity = read_pid()
    try:
        recorded_pid = int(identity.get("pid", 0) or 0)
    except (TypeError, ValueError):
        recorded_pid = 0
    recorded_nonce = str(identity.get("nonce", "") or "")
    if recorded_pid != target_pid or recorded_nonce != target_nonce:
        return False, (
            f"Refusing fallback kill: bridge.pid no longer names the shutdown target "
            f"(PID {target_pid}); the current owner was left untouched."
        )

    healthy, health = check_bridge_health(cfg.bridge.host, cfg.bridge.port, timeout=0.5)
    try:
        live_pid = int(health.get("pid") or 0) if healthy else 0
    except (TypeError, ValueError):
        live_pid = 0
    live_nonce = str(health.get("instance_nonce", "")) if healthy else ""
    if not healthy or live_pid != target_pid or live_nonce != target_nonce:
        return False, (
            f"Refusing fallback kill: PID {target_pid} is no longer the process answering "
            f"on {cfg.bridge.host}:{cfg.bridge.port}; the successor was left untouched."
        )

    try:
        if sys.platform == "win32":
            result = run_hidden(
                ["taskkill", "/PID", str(target_pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return False, (
                    f"Failed to stop bridge PID {target_pid}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        else:
            os.kill(target_pid, 9)
        for _ in range(15):
            time.sleep(0.1)
            if not is_bridge_healthy(cfg.bridge.host, cfg.bridge.port, timeout=0.3):
                remove_pid(expected_pid=target_pid, expected_nonce=target_nonce)
                return True, f"Bridge stopped (PID {target_pid})."
        return False, "Bridge process remained healthy after fallback stop."
    except Exception as exc:
        return False, f"Failed to stop bridge PID: {exc}"
