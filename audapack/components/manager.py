"""Component Center manager for AUDAPACK: Context Menu, Bridge, Autostart, and Widget."""

from __future__ import annotations

from typing import Any, Optional

from audapack.bridge.lifecycle import (
    check_bridge_health,
    is_bridge_healthy,
    start_bridge_background,
    stop_bridge,
)
from audapack.components.autostart import (
    get_autostart_status,
    install_autostart,
    remove_autostart,
    repair_autostart,
)
from audapack.components.migration import detect_legacy_installation, perform_bridge_takeover
from audapack.components.widget import (
    launch_dedicated_chromium_worker,
    open_widget_in_dedicated_chromium,
    read_bundled_widget_metadata,
)
from audapack.config import AppConfig, load_config
from audapack.context_menu import (
    install_context_menu,
    is_context_menu_installed,
    remove_context_menu,
)


class ComponentManager:
    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or load_config()

    def get_components_status(self) -> dict[str, Any]:
        healthy, health_info = check_bridge_health(self.config.bridge.host, self.config.bridge.port)
        auto_status = get_autostart_status()
        legacy_info = detect_legacy_installation()
        ctx_installed = is_context_menu_installed()
        widget_meta = read_bundled_widget_metadata()

        return {
            "context_menu": {
                "installed": ctx_installed,
                "status": "INSTALLED" if ctx_installed else "NOT INSTALLED",
            },
            "bridge": {
                "running": healthy,
                "status": "RUNNING" if healthy else ("LEGACY_RUNNING" if health_info.get("status") == "legacy_acbbridge" else "STOPPED"),
                "health_info": health_info,
                "host": self.config.bridge.host,
                "port": self.config.bridge.port,
                "token": self.config.bridge.token,
            },
            "autostart": auto_status,
            "legacy": legacy_info,
            "widget": {
                "installed": widget_meta["exists"],
                "version": widget_meta["version"],
                "status": "READY" if widget_meta["exists"] else "MISSING",
                "path": widget_meta["path"],
            },
        }

    def install_context_menu(self) -> tuple[bool, str]:
        ok = install_context_menu()
        return ok, "Context menu installed." if ok else "Failed to install context menu."

    def remove_context_menu(self) -> tuple[bool, str]:
        ok = remove_context_menu()
        return ok, "Context menu removed." if ok else "Failed to remove context menu."

    def start_bridge(self) -> tuple[bool, str]:
        ok = start_bridge_background(self.config)
        return ok, "AUDAPACK Bridge started." if ok else "Failed to start AUDAPACK Bridge."

    def stop_bridge(self) -> tuple[bool, str]:
        return stop_bridge(self.config)

    def restart_bridge(self) -> tuple[bool, str]:
        """W2-009: verify a real stop/start transition. Never report success
        when the old Bridge was not stopped or the new one did not come up."""
        stopped, stop_msg = stop_bridge(self.config)
        if not stopped:
            return False, f"Restart aborted: {stop_msg}"
        ok = start_bridge_background(self.config)
        if not ok:
            return False, "Failed to restart AUDAPACK Bridge."
        if not is_bridge_healthy(self.config.bridge.host, self.config.bridge.port, timeout=2.0):
            return False, "AUDAPACK Bridge did not become healthy after restart."
        return True, "AUDAPACK Bridge restarted."

    def install_autostart(self) -> tuple[bool, str]:
        return install_autostart()

    def remove_autostart(self) -> tuple[bool, str]:
        return remove_autostart()

    def repair_autostart(self) -> tuple[bool, str]:
        return repair_autostart()

    def takeover_legacy_bridge(self) -> tuple[bool, dict[str, Any]]:
        return perform_bridge_takeover(self.config)

    def get_bridge_token(self) -> str:
        return self.config.bridge.token

    def trigger_widget_install(self) -> tuple[bool, str]:
        bridge_healthy = is_bridge_healthy(self.config.bridge.host, self.config.bridge.port)
        return open_widget_in_dedicated_chromium(
            use_bridge=bridge_healthy,
            bridge_url=f"http://{self.config.bridge.host}:{self.config.bridge.port}/widget.user.js",
        )

    def launch_browser_worker(self) -> tuple[bool, str]:
        return launch_dedicated_chromium_worker()

    def repair_all(self) -> dict[str, dict[str, Any]]:
        results = {}
        # 1. Takeover / start bridge
        ok_takeover, takeover_rep = perform_bridge_takeover(self.config)
        results["bridge"] = {"ok": ok_takeover, "msg": "AUDAPACK Bridge verified and active." if ok_takeover else str(takeover_rep.get("errors"))}

        # 2. Autostart
        ok_auto, auto_msg = repair_autostart()
        results["autostart"] = {"ok": ok_auto, "msg": auto_msg}

        # 3. Context menu
        if is_context_menu_installed():
            ok_ctx, ctx_msg = self.install_context_menu()
            results["context_menu"] = {"ok": ok_ctx, "msg": ctx_msg}
        else:
            results["context_menu"] = {"ok": True, "msg": "Context menu not active"}

        return results
