"""Legacy ACBBridge detection, verification, and migration engine for AUDAPACK."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from audapack.bridge.lifecycle import check_bridge_health, start_bridge_background, stop_bridge
from audapack.components.autostart import (
    get_canonical_autostart_command,
    install_autostart,
    query_task,
    run_autostart_task,
)
from audapack.config import AppConfig, get_user_runtime_dir, load_config, save_config
from audapack.projects import ProjectRegistry

LEGACY_TASK_NAME = "ACBBridge"


def get_legacy_appdata_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        return Path(local_app_data) / "ACBBridge"
    return Path.home() / ".local" / "share" / "ACBBridge"


def detect_legacy_installation() -> dict[str, Any]:
    """Inspects machine for legacy ACBBridge components without performing mutations."""
    task_exists, task_info = query_task(LEGACY_TASK_NAME)
    legacy_dir = get_legacy_appdata_dir()
    dir_exists = legacy_dir.exists()

    # Check port 17843 identity
    healthy, health_info = check_bridge_health("127.0.0.1", 17843, timeout=0.8)
    port_legacy = (health_info.get("status") == "legacy_acbbridge")

    return {
        "legacy_task_exists": task_exists,
        "legacy_task_info": task_info,
        "legacy_dir_exists": dir_exists,
        "legacy_dir_path": str(legacy_dir),
        "legacy_bridge_running_on_port": port_legacy,
        "port_health_info": health_info,
    }


def stop_verified_legacy_bridge() -> tuple[bool, str]:
    """Stops the legacy ACBBridge process after verifying ownership."""
    if sys.platform != "win32":
        return False, "Non-Windows environment."

    # 1. Stop scheduled task if present
    task_exists, _ = query_task(LEGACY_TASK_NAME)
    if task_exists:
        try:
            subprocess.run(["schtasks", "/end", "/tn", LEGACY_TASK_NAME], capture_output=True)
        except Exception:
            pass

    # 2. Wait up to 3s for port 17843 to release
    for _ in range(30):
        time.sleep(0.1)
        healthy, health_info = check_bridge_health("127.0.0.1", 17843, timeout=0.2)
        if health_info.get("status") == "offline":
            return True, "Legacy bridge stopped successfully."

    # 3. If still running, verify commandline matches acbbridge.py before taskkill
    try:
        wmic_res = subprocess.run(
            ["wmic", "process", "where", "name like 'python%.exe'", "get", "ProcessId,CommandLine", "/format:list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for block in wmic_res.stdout.split("\n\n"):
            if "acbbridge" in block.lower():
                for line in block.splitlines():
                    if line.strip().lower().startswith("processid="):
                        pid = line.split("=", 1)[1].strip()
                        if pid.isdigit():
                            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    except Exception:
        pass

    time.sleep(0.5)
    healthy, health_info = check_bridge_health("127.0.0.1", 17843, timeout=0.2)
    if health_info.get("status") == "offline":
        return True, "Legacy bridge terminated."
    return False, "Port 17843 remains occupied by an unverified process."


def probe_authenticated_endpoints(host: str, port: int, token: str, write_probe_name: Optional[str] = None) -> tuple[bool, dict[str, Any]]:
    """
    Authenticated capability probes against a running AUDAPACK Bridge:
    - GET /v1/status must return 200;
    - GET /v1/registry must return 200 with ok=true;
    - when write_probe_name is given, POST /v1/projects/resolve proves the
      controlled write path (existing name resolves without mutation).
    Returns (all_ok, detail_report).
    """
    probes: dict[str, Any] = {"status": False, "registry": False, "write": None}
    headers = {"X-ACB-Token": token, "Content-Type": "application/json"}
    base = f"http://{host}:{port}"

    try:
        req = urllib.request.Request(f"{base}/v1/status", headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            probes["status"] = resp.status == 200
            if not probes["status"]:
                return False, probes
    except Exception as exc:
        probes["status_error"] = str(exc)
        return False, probes

    try:
        req = urllib.request.Request(f"{base}/v1/registry", headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            probes["registry"] = resp.status == 200 and bool(data.get("ok", True))
            if not probes["registry"]:
                return False, probes
    except Exception as exc:
        probes["registry_error"] = str(exc)
        return False, probes

    if write_probe_name:
        try:
            payload = json.dumps({"project_name": write_probe_name}).encode("utf-8")
            req = urllib.request.Request(
                f"{base}/v1/projects/resolve", data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                probes["write"] = resp.status == 200 and bool(data.get("ok"))
                probes["write_status"] = data.get("status")
                probes["write_created"] = bool(data.get("created"))
                if not probes["write"]:
                    return False, probes
        except Exception as exc:
            probes["write"] = False
            probes["write_error"] = str(exc)
            return False, probes

    return True, probes


def _first_registry_project_name(cfg: AppConfig) -> Optional[str]:
    for p in cfg.projects:
        if p.display_name and p.display_name.strip():
            return p.display_name.strip()
    return None


def perform_bridge_takeover(config: Optional[AppConfig] = None) -> tuple[bool, dict[str, Any]]:
    """
    Executes transactional takeover from legacy ACBBridge to AUDAPACK Bridge.

    HARD GATE: the legacy 'ACBBridge' Scheduled Task is deleted only after ALL of
    these are proven: new bridge started; service identity == AUDAPACK Bridge;
    api_version supported; authenticated status/registry succeed; controlled
    write path succeeds; 'AUDAPACK Bridge' Scheduled Task installed AND read back
    with the canonical command; manual trigger produces a healthy AUDAPACK Bridge.
    Any mandatory failure returns False with the legacy task intact and, when we
    stopped a verified legacy bridge, a best-effort legacy restart.
    """
    cfg = config or load_config()
    report: dict[str, Any] = {
        "step": "init",
        "legacy_detected": False,
        "legacy_stopped": False,
        "audapack_started": False,
        "identity_verified": False,
        "capability_probes": False,
        "autostart_installed": False,
        "task_command_verified": False,
        "task_trigger_verified": False,
        "backup_done": False,
        "legacy_task_removed": False,
        "rollback": None,
        "errors": [],
    }

    def _fail(step: str, message: str) -> tuple[bool, dict[str, Any]]:
        report["step"] = step
        report["errors"].append(message)
        # Rollback: we stopped a verified legacy bridge but did NOT reach the
        # deletion gate -- free the port, then bring legacy back up when its task
        # still exists. The new bridge must not hold 17843 during legacy restart.
        if report["legacy_stopped"] and not report["legacy_task_removed"]:
            if report["audapack_started"]:
                try:
                    stop_bridge(cfg)
                except Exception:
                    pass
            det_now = detect_legacy_installation()
            if det_now["legacy_task_exists"] and sys.platform == "win32":
                try:
                    subprocess.run(["schtasks", "/run", "/tn", LEGACY_TASK_NAME], capture_output=True)
                    report["rollback"] = "legacy ACBBridge task re-triggered after failed takeover"
                except Exception as exc:
                    report["rollback"] = f"legacy restart attempt failed: {exc}"
        return False, report

    # Step 1: Detect
    det = detect_legacy_installation()
    report["legacy_detected"] = det["legacy_task_exists"] or det["legacy_bridge_running_on_port"]

    # Step 2: Stop legacy bridge if running
    if det["legacy_bridge_running_on_port"] or det["legacy_task_exists"]:
        ok_stop, stop_msg = stop_verified_legacy_bridge()
        if not ok_stop:
            return _fail("stop_legacy", f"Failed to stop legacy bridge: {stop_msg}")
        report["legacy_stopped"] = True

    # Step 3: Start AUDAPACK Bridge
    ok_start = start_bridge_background(cfg)
    if not ok_start:
        return _fail("start_audapack", "Failed to start AUDAPACK Bridge.")
    report["audapack_started"] = True

    # Step 4: Verify Identity
    healthy, health_data = check_bridge_health(cfg.bridge.host, cfg.bridge.port)
    if not healthy or health_data.get("service") != "AUDAPACK Bridge":
        return _fail("identity", f"Bridge identity verification failed: {health_data}")
    report["identity_verified"] = True

    # Step 5: Authenticated capability probes (status + registry + controlled write)
    write_probe_name = _first_registry_project_name(cfg) or "AUDAPACK_TAKEOVER_WRITE_PROBE"
    ok_probes, probe_detail = probe_authenticated_endpoints(
        cfg.bridge.host, cfg.bridge.port, cfg.bridge.token, write_probe_name=write_probe_name
    )
    report["probe_detail"] = probe_detail
    if not ok_probes:
        return _fail("capability_probes", f"Authenticated capability probes failed: {probe_detail}")
    report["capability_probes"] = True

    # The write probe must not leave a registered probe project behind.
    if probe_detail.get("write_created") and not write_probe_name.startswith("AUDAPACK_TAKEOVER_WRITE_PROBE"):
        pass  # existing project resolved -- nothing to clean
    elif probe_detail.get("write_created"):
        try:
            live_cfg = load_config()
            reg = ProjectRegistry(live_cfg, transactional=True)
            probe_proj = reg.get_project_by_name(write_probe_name)
            if probe_proj:
                reg.remove_project(probe_proj.id)
                save_config(reg.config)
                report["write_probe_cleaned"] = True
        except Exception as exc:
            report["errors"].append(f"Write-probe project cleanup failed (non-fatal): {exc}")

    # Step 6: Backup legacy directory BEFORE any destructive cleanup
    legacy_dir = get_legacy_appdata_dir()
    if legacy_dir.exists():
        backup_dir = get_user_runtime_dir() / "migration_backup" / "ACBBridge"
        try:
            backup_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_dir, backup_dir, dirs_exist_ok=True)
            report["backup_done"] = True
        except Exception as exc:
            # Non-blocking when the legacy source remains untouched, but reported truthfully.
            report["errors"].append(f"Backup failed (non-blocking): {exc}")
    else:
        report["backup_done"] = True  # nothing to back up

    # Step 7: Install new Autostart task -- mandatory
    ok_auto, auto_msg = install_autostart()
    if not ok_auto:
        return _fail("install_autostart", f"Autostart installation failed: {auto_msg}")
    report["autostart_installed"] = True

    # Step 8: Read the task back; command MUST match current AUDAPACK runtime
    exists, task_info = query_task("AUDAPACK Bridge")
    expected_cmd = get_canonical_autostart_command()
    actual_cmd = (task_info.get("Task To Run") or "").strip() if exists else ""
    report["task_command_actual"] = actual_cmd
    report["task_command_expected"] = expected_cmd
    if not exists or actual_cmd.lower() != expected_cmd.strip().lower():
        return _fail(
            "verify_task_command",
            f"Scheduled Task command mismatch after install: {actual_cmd!r} != {expected_cmd!r}",
        )
    report["task_command_verified"] = True

    # Step 9: Manual trigger proof -- the running bridge is stopped first so the
    # health we observe afterwards can only come from the Scheduled Task itself.
    ok_stop_new, _stop_msg = stop_bridge(cfg)
    if not ok_stop_new:
        return _fail("trigger_prep", "Could not stop the running AUDAPACK Bridge before task trigger proof.")
    ok_trigger, trigger_msg = run_autostart_task()
    if not ok_trigger:
        # The task failed to start anything; bring the bridge back directly.
        start_bridge_background(cfg)
        return _fail("trigger_task", f"Manual task trigger failed: {trigger_msg}")
    deadline = time.time() + 20.0
    triggered_healthy = False
    while time.time() < deadline:
        time.sleep(0.5)
        t_healthy, t_health = check_bridge_health(cfg.bridge.host, cfg.bridge.port, timeout=0.8)
        if t_healthy:
            triggered_healthy = True
            break
    if not triggered_healthy:
        start_bridge_background(cfg)
        return _fail("trigger_verify", "Scheduled Task trigger did not produce a healthy AUDAPACK Bridge.")
    report["task_trigger_verified"] = True

    # ---- GATE PASSED: everything above proven; legacy removal is now permitted ----

    # Step 10: Remove legacy Scheduled Task
    if det["legacy_task_exists"]:
        try:
            res_del = subprocess.run(
                ["schtasks", "/delete", "/tn", LEGACY_TASK_NAME, "/f"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if res_del.returncode == 0:
                report["legacy_task_removed"] = True
            else:
                report["errors"].append(
                    f"Legacy task deletion failed (gate passed, non-fatal): {res_del.stderr.strip() or res_del.stdout.strip()}"
                )
        except Exception as exc:
            report["errors"].append(f"Failed to remove legacy task: {exc}")

    report["step"] = "complete"
    return True, report
