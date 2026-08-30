"""Bridge control — GUI-oriented facade, no PID/Task details leak."""

from __future__ import annotations

from typing import Any, Optional

from audapack.bridge.lifecycle import check_bridge_health, is_bridge_healthy, start_bridge_background, stop_bridge
from audapack.components.autostart import get_autostart_status, install_autostart, remove_autostart
from audapack.config import AppConfig, load_config


class BridgeService:
    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or load_config()

    def status(self) -> dict[str, Any]:
        healthy, info = check_bridge_health(self.config.bridge.host, self.config.bridge.port)
        autostart = get_autostart_status()
        return {"healthy": healthy, "health_info": info, "autostart": autostart, "is_healthy": is_bridge_healthy(self.config.bridge.host, self.config.bridge.port)}

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
