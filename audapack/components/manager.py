"""Component Center manager for AUDAPACK: Context Menu, Bridge, Autostart, and Widget."""

from __future__ import annotations

import sys
from pathlib import Path
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
    open_manual_chromium_window,
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

    #: How long to wait for a freshly launched worker window to register before
    #: opening the installer into it anyway.
    WIDGET_INSTALL_WARMUP_SECONDS = 25.0

    def _worker_profile_is_live(self) -> bool:
        """True when a WINDOW exists in the dedicated worker profile.

        CORE-005 (audit/2.md): this used to answer `dispatch.active_workers > 0`,
        which is audit-lane registration -- a different invariant entirely, and
        wrong in both directions. False positive: `dedicated_profile_only` is off
        by default, so an operator's own Chromium/Brave tab carrying the widget
        satisfies it while the dedicated profile is not running at all. False
        negative, and the one that hurts: on a genuinely fresh profile the
        userscript is not installed yet, so the window CANNOT register as a
        worker -- the installer waited the full 25 s for a condition that was
        impossible by construction and then opened a second window anyway,
        defeating its own "warm it first" purpose.

        T-152: the Bridge fallback that survived this method reintroduced the
        exact false positive on non-Windows and on any enumeration failure --
        `active_workers` means "some browser widget worker is registered", never
        "the dedicated Chromium user-data-dir has a live window". The method now
        answers ONLY the dedicated-profile question and fails closed: Win32
        enumerates the profile windows, every other platform and every
        enumeration error answers False. The Bridge is never consulted here.
        """
        if sys.platform != "win32":
            return False
        try:
            from audapack.components.widget import get_dedicated_chromium_profile_dir
            from audapack.window_layout import find_profile_windows

            return bool(find_profile_windows(get_dedicated_chromium_profile_dir()))
        except Exception:
            return False

    def trigger_widget_install(self) -> tuple[bool, str]:
        """Open the userscript installer in the dedicated worker profile.

        Warms the profile first when nothing is running in it. Tampermonkey's
        install goes through an intermediate page that waits on the extension's
        MV3 service worker, and a Chromium started only to open that URL is a
        cold start every time -- measured as often 1-2 minutes as instant, with
        the Bridge serving all 840KB in 2ms and therefore not the cause. When a
        window already exists, Chrome forwards the URL into that live process
        and the worker is already awake.

        A HYPOTHESIS about the cold start, not a measurement: the message says
        which path was taken so the two can be told apart in use.
        """
        import time

        bridge_healthy = is_bridge_healthy(self.config.bridge.host, self.config.bridge.port)
        warmed = ""
        live = bridge_healthy and self._worker_profile_is_live()
        if bridge_healthy and not live:
            launched, _msg = self.launch_browser_worker()
            if launched:
                deadline = time.time() + self.WIDGET_INSTALL_WARMUP_SECONDS
                while time.time() < deadline:
                    if self._worker_profile_is_live():
                        live = True
                        break
                    time.sleep(1.0)
                warmed = " (profile was cold; warmed it first)"
        elif bridge_healthy:
            warmed = " (profile already live)"

        ok, message = open_widget_in_dedicated_chromium(
            use_bridge=bridge_healthy,
            bridge_url=f"http://{self.config.bridge.host}:{self.config.bridge.port}/widget.user.js",
            # A live profile takes the installer as a TAB. Opening a window for
            # it as well is how one press started producing two.
            new_window=not live,
        )
        return ok, (message + warmed) if ok else message

    #: How long to keep looking for windows that a just-issued launch has not
    #: put on screen yet. Chrome takes seconds to show one.
    ARRANGE_SETTLE_SECONDS = 6.0

    def arrange_worker_windows(
        self, *, force: bool = False, settle_seconds: float = 0.0
    ) -> tuple[bool, str]:
        """Put every window of the worker profile on the configured monitor.

        ``force`` runs the arrangement even when the setting is off, which is
        what an explicit "arrange now" press means. ``settle_seconds`` keeps
        looking while a launch is still opening windows, because a Chromium
        asked to open a window has not opened it by the time the call returns.
        """
        import time

        from audapack.components.widget import get_dedicated_chromium_profile_dir
        from audapack.window_layout import (
            arrange_windows,
            find_profile_windows,
            layout_geometry,
            list_monitors,
            resolve_monitor,
        )

        ui = self.config.ui
        if not force and not bool(getattr(ui, "arrange_worker_windows", True)):
            return False, "Worker window arrangement is switched off."

        monitor = resolve_monitor(list_monitors(), int(getattr(ui, "worker_window_monitor", -1)))
        if monitor is None:
            return False, "No display could be read; no window was moved."

        profile = get_dedicated_chromium_profile_dir()
        handles = find_profile_windows(profile)
        deadline = time.time() + max(0.0, float(settle_seconds))
        while not handles and time.time() < deadline:
            time.sleep(0.5)
            handles = find_profile_windows(profile)
        if not handles:
            return False, "No worker window is open, so there was nothing to arrange."

        layout = str(getattr(ui, "worker_window_layout", "grid"))
        minimized = bool(getattr(ui, "worker_windows_minimized", True))
        moved = arrange_windows(handles, layout_geometry(layout, len(handles), monitor), minimized)
        if not moved:
            return False, f"{len(handles)} worker window(s) found, none could be moved."
        tail = ", minimized" if minimized else ""
        return True, f"Arranged {moved} worker window(s) as {layout} on display {monitor.index + 1}{tail}."

    #: Windows the operator opened by hand with NEW. Automatic close must never
    #: touch one: it holds an audit they are running themselves, and nothing in
    #: the window itself distinguishes it from a worker at the Win32 level.
    MANUAL_WINDOW_REGISTRY = "manual_worker_windows.json"
    #: How long to watch for the window a NEW press just asked Chromium for.
    MANUAL_WINDOW_SETTLE_SECONDS = 8.0

    def _manual_window_file(self) -> Path:
        from audapack.config import get_state_dir

        return get_state_dir() / self.MANUAL_WINDOW_REGISTRY

    def _read_manual_windows(self) -> set[int]:
        import json

        path = self._manual_window_file()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(raw, list):
            return set()
        return {int(item) for item in raw if isinstance(item, (int, float, str)) and str(item).isdigit()}

    def _write_manual_windows(self, handles: set[int]) -> None:
        import json

        try:
            self._manual_window_file().write_text(
                json.dumps(sorted(handles)), encoding="utf-8"
            )
        except OSError:
            # Losing the registry means a hand-opened window could be closed by
            # the idle sweep. Bad, but not worth refusing to open the window.
            pass

    def open_manual_worker_window(self) -> tuple[bool, str]:
        """A window in the worker profile that no lane owns.

        The dispatcher only knows a window by its slot/generation query params,
        so one opened without them is the operator's to use by hand.

        Its handle is remembered, because the idle sweep closes worker windows
        and Win32 cannot tell this one from a worker: same profile, same
        executable, same title.
        """
        import time

        from audapack.components.widget import get_dedicated_chromium_profile_dir
        from audapack.window_layout import find_profile_windows

        profile = get_dedicated_chromium_profile_dir()
        before = set(find_profile_windows(profile))
        ok, message = open_manual_chromium_window()
        if not ok:
            return ok, message

        deadline = time.time() + self.MANUAL_WINDOW_SETTLE_SECONDS
        while time.time() < deadline:
            appeared = set(find_profile_windows(profile)) - before
            if appeared:
                known = self._read_manual_windows()
                # Prune handles that are no longer windows while we are here,
                # or the registry grows for the life of the install.
                alive = set(find_profile_windows(profile))
                self._write_manual_windows((known & alive) | appeared)
                break
            time.sleep(0.5)
        return ok, message

    def close_idle_worker_windows(self, *, force: bool = False) -> tuple[bool, str]:
        """Close the worker windows once the pool has nothing left to do.

        Idle means the Bridge reports nothing queued, nothing in flight,
        nothing blocked awaiting recovery and no busy worker. Anything less and
        this does nothing: a window holding a run is not ours to close.

        Windows the operator opened by hand with NEW are excluded by handle --
        at the Win32 level they are indistinguishable from a worker, so the
        registry written when NEW opened one is the only thing that knows.
        """
        from audapack.components.widget import get_dedicated_chromium_profile_dir
        from audapack.window_layout import close_windows, find_profile_windows

        if not force and not bool(getattr(self.config.ui, "close_idle_worker_windows", True)):
            return False, "Closing idle worker windows is switched off."

        busy = self._pool_busy_reason()
        if busy:
            return False, f"Worker windows left open: {busy}."

        profile = get_dedicated_chromium_profile_dir()
        handles = set(find_profile_windows(profile))
        manual = self._read_manual_windows() & handles
        closable = handles - manual
        if not closable:
            kept = f" ({len(manual)} opened by hand, left alone)" if manual else ""
            return False, f"No worker window to close{kept}."

        closed = close_windows(closable)
        self._write_manual_windows(manual)
        tail = f", {len(manual)} opened by hand left alone" if manual else ""
        return bool(closed), f"Closed {closed} idle worker window(s){tail}."

    def _pool_busy_reason(self) -> str:
        """Why the pool is not idle, or "" when it is. Unreadable counts as busy."""
        try:
            from audapack.services.bridge_service import BridgeService

            status = BridgeService(self.config).browser_status()
        except Exception as exc:
            return f"the Bridge could not be read ({exc})"
        if not status.get("ok"):
            return "the Bridge did not answer"
        dispatch = status.get("dispatch") or {}
        for key, label in (
            ("queued_jobs", "audits are queued"),
            ("active_jobs", "an audit is in flight"),
            ("blocked_jobs", "a blocked run is waiting for you"),
            ("busy_workers", "a worker is busy"),
        ):
            if int(dispatch.get(key, 0) or 0) > 0:
                return label
        return ""

    def launch_browser_worker(
        self,
        *,
        managed_slot: Optional[int] = None,
        managed_generation: Optional[int] = None,
    ) -> tuple[bool, str]:
        return launch_dedicated_chromium_worker(
            managed_slot=managed_slot,
            managed_generation=managed_generation,
        )

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
