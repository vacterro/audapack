"""Bridge control — GUI-oriented facade, no PID/Task details leak."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from audapack.bridge.lifecycle import check_bridge_health, start_bridge_background, stop_bridge
from audapack.components.autostart import get_autostart_status, install_autostart, remove_autostart
from audapack.config import AppConfig, load_config
from audapack.packing import find_archive_for_project, resolve_output_dir


def _browser_bridge_request(config: AppConfig, method: str, path: str, payload: Optional[dict] = None) -> dict[str, Any]:
    host = str(config.bridge.host or "127.0.0.1")
    port = int(config.bridge.port)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return {"ok": False, "error": "Bridge host must be loopback"}
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-ACB-Token": str(config.bridge.token or ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def browser_worker_launch_need(dispatch: dict[str, Any]) -> str:
    """Classify whether a SEND AUDIT needs a worker window launched.

    Returns one of:
      'ready'   -- at least one clean worker is free; submit directly.
      'busy'    -- workers exist but all are busy; job will queue normally.
      'launch'  -- no worker is registered; launch the dedicated Chromium.
    Never raises on malformed status payloads.
    """
    if not isinstance(dispatch, dict):
        return "launch"
    active = int(dispatch.get("active_workers", 0) or 0)
    clean = int(dispatch.get("clean_workers", 0) or 0)
    if clean > 0:
        return "ready"
    if active > 0:
        return "busy"
    return "launch"

class BridgeService:
    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or load_config()

    def status(self) -> dict[str, Any]:
        healthy, info = check_bridge_health(self.config.bridge.host, self.config.bridge.port)
        autostart = get_autostart_status()
        browser = self.browser_status() if healthy else {"ok": False}
        return {"healthy": healthy, "health_info": info, "autostart": autostart, "is_healthy": healthy, "browser": browser.get("dispatch", {})}

    def runtime_status(self) -> dict[str, Any]:
        """Cheap periodic UI status: HTTP only, zero subprocess/schtasks.

        P0-1: the 5-second live-status timer must never spawn a Windows console
        (get_autostart_status -> schtasks /query). Health is probed exactly
        once per snapshot; the full OS/autostart status belongs on startup and
        in Settings only.
        """
        healthy, info = check_bridge_health(self.config.bridge.host, self.config.bridge.port)
        browser = self.browser_status() if healthy else {"ok": False}
        return {
            "healthy": healthy,
            "health_info": info,
            "is_healthy": healthy,
            "browser": browser.get("dispatch", {}),
        }

    def start(self) -> tuple[bool, str]:
        ok = start_bridge_background(self.config)
        return (ok, "Bridge started" if ok else "Failed to start bridge")

    def stop(self) -> tuple[bool, str]:
        return stop_bridge(self.config)

    def restart(self) -> tuple[bool, str]:
        stopped, stop_message = stop_bridge(self.config)
        if not stopped:
            return False, f"Restart failed: {stop_message}"
        ok = start_bridge_background(self.config)
        return (ok, "Bridge restarted" if ok else "Restart failed")

    def install_autostart(self) -> tuple[bool, str]:
        return install_autostart()

    def remove_autostart(self) -> tuple[bool, str]:
        return remove_autostart()

    def repair(self) -> tuple[bool, str]:
        from audapack.components.autostart import repair_autostart
        return repair_autostart()

    def submit_browser_audit(self, project, archive_path: Path, profile: str = "quick3") -> dict[str, Any]:
        """Queue packed archive for an existing free Widget worker.

        The browser receives only a dispatch id and later streams the server-owned
        archive through its leased artifact endpoint. It never receives a local
        filesystem path as an instruction and this method never creates windows.
        """
        archive = Path(archive_path)
        if not archive.is_file():
            return {"ok": False, "error": "Packed archive is missing"}
        try:
            output_dir = resolve_output_dir(
                project.source_path,
                self.config.packing,
                fallback=Path.cwd(),
                group=getattr(project, "priority_group", None),
                project=project,
            )
            canonical = find_archive_for_project(project, output_dir)
            if canonical is None or archive.resolve() != canonical.resolve():
                return {"ok": False, "error": "Archive is not the canonical current project pack artifact"}
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"Archive ownership check failed: {exc}"}
        return _browser_bridge_request(self.config, "POST", "/v1/browser/jobs", {
            "project_id": str(project.id),
            "project_name": str(project.display_name),
            "archive_filename": archive.name,
            "archive_path": str(archive.resolve()),
            "archive_size": archive.stat().st_size,
            "archive_sha256": _sha256_file(archive),
            "profile": profile,
        })

    def browser_jobs(self, project_id: str | None = None) -> dict[str, Any]:
        path = "/v1/browser/jobs"
        if project_id:
            from urllib.parse import quote
            path += f"?project_id={quote(str(project_id), safe='')}"
        return _browser_bridge_request(self.config, "GET", path)

    def active_browser_job(self, project_id: str) -> dict[str, Any] | None:
        response = self.browser_jobs(project_id)
        if not response.get("ok"):
            return None
        terminal = {"COMPLETE", "FAILED", "CANCELLED"}
        jobs = [job for job in response.get("jobs", []) if job.get("state") not in terminal]
        return jobs[-1] if jobs else None

    def browser_status(self) -> dict[str, Any]:
        return _browser_bridge_request(self.config, "GET", "/v1/browser/status")

    def cancel_browser_job(self, dispatch_id: str) -> dict[str, Any]:
        from urllib.parse import quote
        return _browser_bridge_request(self.config, "POST", f"/v1/browser/jobs/{quote(str(dispatch_id), safe='')}/cancel")
