"""Main application window and project room for AUDAPACK."""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Optional

from audapack import __app_name__, __version__
from audapack.audits import AuditIndexer
from audapack.bridge.lifecycle import start_bridge_background
from audapack.bridge.server import set_audit_written_callback
from audapack.config import (
    AppConfig,
    app_dir,
    load_config,
)
from audapack.models import (
    SLOTS_PER_GROUP,
    AuditSnapshot,
    Project,
)
from audapack.packing import find_archive_for_project, human_mb, pack_single
from audapack.projects import ProjectRegistry
from audapack.saipen import get_saipen_info
from audapack.services.audit_service import AuditService
from audapack.services.project_service import ProjectService
from audapack.ui.clipboard_files import copy_file_to_clipboard
from audapack.ui.dialogs import ProjectEditDialog
from audapack.ui.i18n import (
    available_languages,
    get_language,
    language_display_name,
    register_reload_callback,
    t,
)
from audapack.ui.i18n import (
    set_language as i18n_set_language,
)
from audapack.ui.project_rows import EmptySlotRow, ProjectRow
from audapack.ui.settings import SettingsDialog
from audapack.ui.theme import FONT_FAMILY, PALETTE


class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config: AppConfig = load_config()
        # Sync UI language from config to i18n module BEFORE building widgets.
        i18n_set_language(self.config.ui.ui_language)
        # Disk is the canonical registry state: every mutation runs inside the
        # cross-process transaction (lock -> reload latest -> atomic save).
        self.registry = ProjectRegistry(self.config, transactional=True)
        self.indexer = AuditIndexer(self.config)
        # Framework-neutral application services (Wave K) — UI only presents,
        # services own the operations. No service imports tkinter/PySide6.
        self.project_service = ProjectService(self.config)
        self.audit_service = AuditService(self.config)

        self.snapshots: dict[str, AuditSnapshot] = {}
        self.saipen_cache: dict[str, Any] = {}
        self.cancel_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()
        self._saipen_queue: queue.Queue = queue.Queue()
        self.is_packing = False

        self._saipen_thread = threading.Thread(target=self._saipen_worker, daemon=True)
        self._saipen_thread.start()

        self._setup_window()
        self._build_ui()
        self._refresh_data()

        # Start bridge in background if autostart enabled
        if self.config.bridge.autostart:
            threading.Thread(target=start_bridge_background, args=(self.config,), daemon=True).start()

        # Register bridge write notification callback
        set_audit_written_callback(self._on_bridge_audit_received)

        # Register i18n reload callback so language switches retranslate in place
        register_reload_callback(self._on_language_changed)

        # Process UI queue periodically
        self.root.after(100, self._process_queue)

    def _setup_window(self):
        self.root.title(f"{__app_name__} v{__version__}")
        w, h = self.config.ui.window_size
        # Bump minimum width to fit new COPY ARCHIVE column on 100% scaling.
        w = max(w, 820)
        h = max(h, 600)
        self.root.minsize(820, 480)
        self.root.configure(bg=PALETTE["background"])

        # Position main window near mouse cursor, clamped to monitor area
        try:
            cur_x = self.root.winfo_pointerx()
            cur_y = self.root.winfo_pointery()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            pos_x = max(20, min(cur_x - w // 3, sw - w - 20))
            pos_y = max(20, min(cur_y - 80, sh - h - 40))
            self.root.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        except Exception:
            self.root.geometry(f"{w}x{h}")

        # Set application icon (Crisp Win95 Nearest-Neighbor / No AA)
        icon_ico = app_dir() / "resources" / "app_icon.ico"
        icon_16 = app_dir() / "resources" / "app_icon_16.png"
        icon_32 = app_dir() / "resources" / "app_icon_32.png"
        icon_png = app_dir() / "resources" / "app_icon.png"

        if icon_ico.exists():
            try:
                self.root.iconbitmap(default=str(icon_ico))
            except Exception:
                try:
                    self.root.iconbitmap(str(icon_ico))
                except Exception:
                    pass

        self.app_icons = []
        for p in [icon_16, icon_32, icon_png]:
            if p.exists():
                try:
                    self.app_icons.append(tk.PhotoImage(file=str(p)))
                except Exception:
                    pass

        if self.app_icons:
            try:
                self.root.iconphoto(True, *self.app_icons)
            except Exception:
                pass

        if sys.platform == "win32" and icon_ico.exists():
            try:
                import ctypes
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x00000010
                hicon_sm = ctypes.windll.user32.LoadImageW(None, str(icon_ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                hicon_bg = ctypes.windll.user32.LoadImageW(None, str(icon_ico), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)

                def _apply_win32_icons():
                    try:
                        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                        WM_SETICON = 0x0080
                        if hicon_sm:
                            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_sm)
                        if hicon_bg:
                            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_bg)
                    except Exception:
                        pass

                self.root.after(20, _apply_win32_icons)
            except Exception:
                pass

        # Save window size on exit
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Register loopback bridge live event callback
        try:
            from audapack.bridge.server import set_audit_written_callback
            set_audit_written_callback(lambda p, w: self.root.after(0, self._on_bridge_event, p, w))
        except Exception:
            pass

    def _on_close(self):
        try:
            from audapack.config import scoped_config_write
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            scoped_config_write(lambda cfg: (
                setattr(cfg.ui, "window_size", [w, h]),
                setattr(cfg.ui, "window_pos", [x, y]),
            ))
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        # 1. Top Action Toolbar
        self.top_bar = tk.Frame(self.root, bg=PALETTE["surfaceRaised"], bd=2, relief="raised", padx=6, pady=4)
        self.top_bar.pack(side="top", fill="x")

        self.btn_pack_all = tk.Button(
            self.top_bar,
            text=t("toolbar.pack_all"),
            bg=PALETTE["surfaceAlt"],
            fg=PALETTE["borderHighlight"],
            activebackground=PALETTE["surface"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 9, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=10,
            command=self._pack_all_enabled,
        )
        self.btn_pack_all.pack(side="left", padx=2)

        self.btn_refresh = tk.Button(
            self.top_bar,
            text=t("toolbar.refresh"),
            bg=PALETTE["surface"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=8,
            command=self._refresh_data,
        )
        self.btn_refresh.pack(side="left", padx=4)

        self.btn_paste_audit = tk.Button(
            self.top_bar,
            text=t("toolbar.paste_audit"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=8,
            command=self._on_paste_audit,
        )
        self.btn_paste_audit.pack(side="left", padx=2)

        self.root.bind("<Control-v>", lambda e: self._on_paste_audit())
        self.root.bind("<Control-V>", lambda e: self._on_paste_audit())

        self.btn_open_out = tk.Button(
            self.top_bar,
            text=t("toolbar.open_output"),
            bg=PALETTE["surface"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=8,
            command=self._open_output_dir,
        )
        self.btn_open_out.pack(side="left", padx=2)

        # Language switcher (segmented RU | EN) — sits between left buttons and settings.
        self._build_language_switcher(self.top_bar)

        self.btn_settings = tk.Button(
            self.top_bar,
            text=t("toolbar.settings"),
            bg=PALETTE["surface"],
            fg=PALETTE["borderHighlight"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=10,
            command=self._open_settings,
        )
        self.btn_settings.pack(side="right", padx=2)

        # 2. Table Column Header (Strict alignment with project rows)
        self.header_bar = tk.Frame(self.root, bg=PALETTE["surfaceAlt"], bd=1, relief="raised", padx=7, pady=3)
        self.header_bar.pack(side="top", fill="x", padx=4, pady=(2, 0))

        self.header_bar.columnconfigure(0, weight=0, minsize=48)
        self.header_bar.columnconfigure(1, weight=0, minsize=46)
        self.header_bar.columnconfigure(2, weight=1)
        self.header_bar.columnconfigure(3, weight=0, minsize=60)
        self.header_bar.columnconfigure(4, weight=0, minsize=88)
        self.header_bar.columnconfigure(5, weight=0, minsize=82)
        self.header_bar.columnconfigure(6, weight=0, minsize=50)
        self.header_bar.columnconfigure(7, weight=0, minsize=110)
        self.header_bar.columnconfigure(8, weight=0, minsize=32)

        self._build_header_labels()

        # 3. Main Scrollable Project Room (24 slots)
        container = tk.Frame(self.root, bg=PALETTE["background"])
        container.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        # Vintage Win95 scrollbar style
        style = ttk.Style()
        try:
            style.theme_use("classic")
        except Exception:
            pass
        style.configure(
            "Vertical.TScrollbar",
            background=PALETTE["surfaceRaised"],
            troughcolor=PALETTE["backgroundSoft"],
            arrowcolor=PALETTE["borderHighlight"],
            relief="raised",
            borderwidth=1,
            arrowsize=13,
            width=16,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", PALETTE["surfaceAlt"]), ("pressed", PALETTE["surface"])],
            arrowcolor=[("active", PALETTE["borderHighlight"]), ("pressed", PALETTE["borderHighlight"])],
        )

        self.canvas = tk.Canvas(container, bg=PALETTE["background"], highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview, style="Vertical.TScrollbar")
        self.scrollable_frame = tk.Frame(self.canvas, bg=PALETTE["background"])

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")

        def _on_canvas_configure(event):
            self.canvas.itemconfig(self.canvas_window, width=event.width)

        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Mousewheel scroll support
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # 4. Bottom Progress & Status Bar
        self.bottom_bar = tk.Frame(self.root, bg=PALETTE["surfaceRaised"], bd=2, relief="sunken", padx=6, pady=4)
        self.bottom_bar.pack(side="bottom", fill="x")

        self.lbl_status = tk.Label(
            self.bottom_bar,
            text=t("status.ready"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 8),
            anchor="w",
        )
        self.lbl_status.pack(side="left", fill="x", expand=True)

        self.btn_cancel = tk.Button(
            self.bottom_bar,
            text=t("dialog.cancel"),
            bg=PALETTE["danger"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=8,
            state="disabled",
            command=self._cancel_packing,
        )
        self.btn_cancel.pack(side="right", padx=4)

    def _build_header_labels(self):
        for child in self.header_bar.winfo_children():
            child.destroy()

        tk.Label(self.header_bar, text="✓ ⊘", bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(self.header_bar, text=t("header.slot"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=1, sticky="w")
        tk.Label(self.header_bar, text=t("header.project"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=2, sticky="w")
        tk.Label(self.header_bar, text=t("header.wave"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=3, sticky="e", padx=(0, 4))
        tk.Label(self.header_bar, text=t("header.temperature"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=4, sticky="e", padx=(0, 4))
        tk.Label(self.header_bar, text=t("header.handoff"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=5, sticky="e", padx=(0, 4))
        tk.Label(self.header_bar, text=t("header.pack"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=6, sticky="e", padx=(0, 4))
        tk.Label(self.header_bar, text=t("header.archive"), bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=7, sticky="e", padx=(0, 4))
        tk.Label(self.header_bar, text="···", bg=PALETTE["surfaceAlt"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 8, "bold")).grid(row=0, column=8, sticky="e")

    def _build_language_switcher(self, parent):
        """Segmented RU | EN toggle. Persists choice via config."""
        self.lang_frame = tk.Frame(parent, bg=PALETTE["surfaceRaised"], bd=2, relief="sunken", padx=2, pady=1)
        self.lang_frame.pack(side="left", padx=(8, 4))

        tk.Label(
            self.lang_frame,
            text=t("toolbar.lang") + ":",
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textSecondary"],
            font=(FONT_FAMILY, 7),
        ).pack(side="left", padx=(2, 4))

        self._lang_buttons: dict[str, tk.Button] = {}
        for code in available_languages():
            btn = tk.Button(
                self.lang_frame,
                text=language_display_name(code),
                bg=PALETTE["surfaceRaised"],
                fg=PALETTE["textSecondary"],
                activebackground=PALETTE["surfaceAlt"],
                activeforeground=PALETTE["borderHighlight"],
                font=(FONT_FAMILY, 8, "bold"),
                relief="raised",
                bd=1,
                highlightthickness=0,
                padx=6,
                command=lambda target=code: self._on_language_button(target),
            )
            btn.pack(side="left", padx=1)
            self._lang_buttons[code] = btn
        self._refresh_lang_switcher()

    def _refresh_lang_switcher(self):
        cur = get_language()
        for code, btn in self._lang_buttons.items():
            if code == cur:
                btn.configure(
                    bg=PALETTE["surfaceAlt"],
                    fg=PALETTE["borderHighlight"],
                    relief="sunken",
                )
            else:
                btn.configure(
                    bg=PALETTE["surfaceRaised"],
                    fg=PALETTE["textSecondary"],
                    relief="raised",
                )

    def _on_language_button(self, lang_code: str):
        """Handle a click on the RU/EN switcher."""
        applied = i18n_set_language(lang_code)
        if not applied:
            return
        try:
            from audapack.config import scoped_config_write
            ok = scoped_config_write(lambda cfg: setattr(cfg.ui, "ui_language", applied))
            if not ok:
                messagebox.showerror("Save Error", "Could not persist language preference.", parent=self.root)
                return
            latest = load_config()
            self.config = latest
        except Exception as exc:
            messagebox.showerror("Save Error", f"Language save failed: {exc}", parent=self.root)

    def _on_language_changed(self, new_lang: str):
        """Called by i18n when the active language changes. Retranslates in place."""
        # Toolbar
        self.btn_pack_all.configure(text=t("toolbar.pack_all"))
        self.btn_refresh.configure(text=t("toolbar.refresh"))
        self.btn_paste_audit.configure(text=t("toolbar.paste_audit"))
        self.btn_open_out.configure(text=t("toolbar.open_output"))
        self.btn_settings.configure(text=t("toolbar.settings"))
        self.btn_cancel.configure(text=t("dialog.cancel"))

        # Language switcher label + button highlight
        try:
            # Update the inline label inside lang_frame
            for child in self.lang_frame.winfo_children():
                if isinstance(child, tk.Label):
                    child.configure(text=t("toolbar.lang") + ":")
                    break
        except Exception:
            pass
        self._refresh_lang_switcher()

        # Status + headers
        try:
            self.lbl_status.configure(text=t("status.ready"))
        except Exception:
            pass
        self._build_header_labels()

        # Rebuild rows so all per-row strings retranslate
        self._rebuild_grid()

    # ----------------------------------------------------------------- data refresh

    def _on_bridge_event(self, project_name: str, wave: str):
        """Called when loopback bridge writes an audit or auto-registers a project."""
        from audapack.config import load_config
        self.config = load_config()
        self.registry.config = self.config
        self.indexer.config = self.config
        self.project_service = ProjectService(self.config)
        self.audit_service = AuditService(self.config)
        self._refresh_data()

    def _saipen_worker(self):
        """Background worker running SAIPEN detection & git inspections with 0ms UI lag."""
        while True:
            try:
                item = self._saipen_queue.get()
                if not item:
                    continue
                proj_id, src_path = item
                if src_path:
                    info = get_saipen_info(src_path)
                    self.ui_queue.put(("saipen_result", proj_id, info))
            except Exception:
                pass

    def _refresh_data(self):
        """Scans audits, queues async SAIPEN inspects, and rebuilds the slot grid."""
        self.snapshots = self.indexer.scan_all_projects()

        for p in self.registry.projects:
            if p.source_path and p.id not in self.saipen_cache:
                self._saipen_queue.put((p.id, p.source_path))

        self._rebuild_grid()

    def _rebuild_grid(self):
        # Clear existing rows
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        slot_map = self.registry.get_slot_map()
        active_groups = self.registry.get_active_groups()
        output_dir = Path(self.config.packing.output_dir or str(app_dir()))

        for group in active_groups:
            occupied = self.project_service.occupied_count(group)

            # Group Header Banner
            grp_frame = tk.Frame(self.scrollable_frame, bg=PALETTE["surfaceAlt"], bd=1, relief="ridge", padx=8, pady=3)
            grp_frame.pack(fill="x", pady=(8, 2), padx=2)

            lbl_grp = tk.Label(
                grp_frame,
                text=f"■ {group}",
                bg=PALETTE["surfaceAlt"],
                fg=PALETTE["borderHighlight"],
                font=(FONT_FAMILY, 9, "bold"),
            )
            lbl_grp.pack(side="left")

            lbl_count = tk.Label(
                grp_frame,
                text=t("status.group_projects_count_fmt", n=occupied, total=SLOTS_PER_GROUP),
                bg=PALETTE["surfaceAlt"],
                fg=PALETTE["textMuted"],
                font=(FONT_FAMILY, 8),
            )
            lbl_count.pack(side="left", padx=8)

            # 6 Slots for this group
            for s in range(1, SLOTS_PER_GROUP + 1):
                proj = slot_map.get((group, s))
                if proj:
                    snap = self.snapshots.get(proj.id)
                    s_info = self.saipen_cache.get(proj.id)
                    row = ProjectRow(
                        self.scrollable_frame,
                        project=proj,
                        snapshot=snap,
                        saipen_info=s_info,
                        on_toggle_enabled=self._on_toggle_enabled,
                        on_pack=self._pack_single_project,
                        on_copy_audit=self._on_copy_audit,
                        on_edit=self._on_edit_project,
                        on_move=self._on_move_project,
                        on_delete=self._on_delete_project,
                        on_move_step=self._on_move_step,
                        on_move_to_group=self._on_move_to_group,
                        on_drop_move=self._on_drop_move,
                        on_copy_archive=self._on_copy_archive,
                        on_reset_copied=self._on_reset_copied_audit,
                        on_toggle_ignored=self._on_toggle_ignored,
                        on_paste_audit=self._on_paste_audit,
                        output_dir=output_dir,
                        active_groups=active_groups,
                    )
                    row.pack(fill="x", pady=1, padx=2)
                else:
                    empty_row = EmptySlotRow(
                        self.scrollable_frame,
                        group=group,
                        slot=s,
                        on_add=self._on_add_project,
                    )
                    empty_row.pack(fill="x", pady=1, padx=2)

    def _on_drop_move(self, source_project: Project, target_group: str, target_slot: int):
        """Handles Drag & Drop movement/swap of project rows."""
        if source_project.priority_group == target_group and source_project.slot == target_slot:
            return
        # Wave K: canonical path goes through ProjectService, which returns a
        # specific targeted result (old/new group+slot, swapped id) instead of
        # asking the GUI to rebuild everything.
        result = self.project_service.move_project(source_project.id, target_group, target_slot)
        if result.ok:
            # Service mutated the canonical config on disk; refresh our view.
            self.config = self.project_service.config
            self.registry = self.project_service.registry
            self.audit_service = AuditService(self.config)
            self._refresh_data()

    def _on_move_step(self, project: Project, step: int):
        """Moves project up or down by 1 slot across the 24-slot grid."""
        if self.registry.move_project_step(project.id, step):
            self._refresh_data()

    def _on_move_to_group(self, project: Project, group: str):
        """Quickly moves a project into the target priority group."""
        free = self.registry.find_first_free_slot(group)
        if free:
            target_group, target_slot = free
        else:
            target_group, target_slot = group, 1
        if self.registry.move_project(project.id, target_group, target_slot):
            self._refresh_data()

    def _on_toggle_enabled(self, project: Project, enabled: bool):
        self.registry.edit_project(project.id, lambda p: setattr(p, "enabled", enabled))
        self._refresh_data()

    def _on_toggle_ignored(self, project: Project, ignored: bool):
        self.registry.edit_project(project.id, lambda p: setattr(p, "ignored", ignored))
        self._refresh_data()

    def _on_copy_audit(self, project: Project, snapshot: AuditSnapshot, button: tk.Button):
        """Copies exact ALL_3 content into clipboard and tracks content hash."""
        # Wave K: audit copy goes through AuditService (UI does not know file
        # layout or hash logic).
        ok, content, sha256 = self.audit_service.copy_all3(project.id)
        # Fallback to direct indexer when service has no snapshot yet (e.g.
        # snapshot object already provided by the caller).
        if not ok:
            ok, content, sha256 = self.indexer.read_exact_all3(snapshot)
        if not ok or not content:
            messagebox.showwarning(__app_name__, t("error.audit_not_ready"), parent=self.root)
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(content)

        target_p = snapshot.final_handoff_path or snapshot.all3_path
        copied_at = target_p.stat().st_mtime if (target_p and target_p.exists()) else ""
        self.registry.edit_project(
            project.id,
            lambda p: (setattr(p, "last_copied_audit_hash", sha256), setattr(p, "last_copied_at", copied_at)),
        )

        self._refresh_data()
        self.lbl_status.configure(text=t("status.copied_audit_fmt", name=project.display_name, n=len(content)))

    def _on_reset_copied_audit(self, project: Project):
        """Clears the last copied audit hash so the project returns to un-copied state."""
        self.registry.edit_project(
            project.id,
            lambda p: (setattr(p, "last_copied_audit_hash", ""), setattr(p, "last_copied_at", "")),
        )
        self._refresh_data()

    def _on_copy_archive(self, project: Project, button: tk.Button):
        """Copies the most recent ZIP archive to Windows clipboard as a real file drop."""
        output_dir = Path(self.config.packing.output_dir or str(app_dir()))
        latest = find_archive_for_project(project, output_dir)
        if not latest:
            self.lbl_status.configure(text=t("status.copy_archive_no_archive_fmt", name=project.display_name))
            return

        try:
            ok = copy_file_to_clipboard(latest)
        except Exception as exc:
            messagebox.showerror(__app_name__, t("error.copy_archive_failed_fmt", err=str(exc)), parent=self.root)
            return

        if not ok:
            self.lbl_status.configure(text=t("error.copy_archive_failed_fmt", err="clipboard refused"))
            return

        try:
            archive_path = str(latest.resolve())
            archive_at = str(latest.stat().st_mtime)
        except Exception:
            archive_path = str(latest)
            archive_at = ""
        self.registry.edit_project(
            project.id,
            lambda p: (setattr(p, "last_copied_archive_path", archive_path), setattr(p, "last_copied_archive_at", archive_at)),
        )

        self._refresh_data()
        self.lbl_status.configure(text=t("status.copied_archive_fmt", name=latest.name))

    def _on_paste_audit(self, project: Optional[Project] = None):
        """Pastes and ingests audit text from Windows clipboard."""
        try:
            content = self.root.clipboard_get()
        except Exception:
            content = ""

        if not content or not content.strip():
            self.lbl_status.configure(
                text=t("status.paste_audit_fail_fmt", err="Буфер обмена пуст или не содержит текст.")
            )
            return

        from audapack.ingest import ingest_audit_text

        project_hint = project.display_name if project else None
        res = ingest_audit_text(content, self.config, project_hint=project_hint)
        if res.ok:
            self._refresh_data()
            self.lbl_status.configure(text=t("status.paste_audit_ok_fmt", msg=res.message))
        else:
            self.lbl_status.configure(text=t("status.paste_audit_fail_fmt", err=res.error))

    def _on_add_project(self, group: str, slot: int):
        dlg = ProjectEditDialog(self.root, default_group=group, default_slot=slot, active_groups=self.registry.get_active_groups())
        self.root.wait_window(dlg)
        if dlg.result:
            try:
                self.registry.add_project(
                    display_name=dlg.result["display_name"],
                    source_path=dlg.result["source_path"],
                    priority_group=dlg.result["priority_group"],
                    slot=dlg.result.get("slot"),
                    audit_project_name=dlg.result.get("audit_project_name"),
                )
                self._refresh_data()
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=self.root)

    def _on_edit_project(self, project: Project):
        dlg = ProjectEditDialog(self.root, project=project, active_groups=self.registry.get_active_groups())
        self.root.wait_window(dlg)
        if dlg.result:
            result = dlg.result
            self.registry.edit_project(
                project.id,
                lambda p: (
                    setattr(p, "display_name", result["display_name"]),
                    setattr(p, "source_path", result["source_path"]),
                    setattr(p, "priority_group", result["priority_group"]),
                    setattr(p, "slot", result["slot"]),
                    setattr(p, "audit_project_name", result["audit_project_name"]),
                ),
            )
            self._refresh_data()

    def _on_move_project(self, project: Project):
        dlg = ProjectEditDialog(self.root, project=project)
        self.root.wait_window(dlg)
        if dlg.result:
            self.registry.move_project(project.id, dlg.result["priority_group"], dlg.result["slot"])
            self._refresh_data()

    def _on_delete_project(self, project: Project):
        if messagebox.askyesno(__app_name__, t("dialog.confirm_remove_fmt", name=project.display_name, group=project.priority_group, slot=project.slot), parent=self.root):
            self.registry.remove_project(project.id)
            self._refresh_data()

    def _open_output_dir(self):
        out_dir = Path(self.config.packing.output_dir or str(app_dir()))
        out_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(out_dir)

    def _open_settings(self):
        self.config = load_config()
        dlg = SettingsDialog(self.root, self.config, on_saved=self._on_settings_saved)
        self.root.wait_window(dlg)

    def _on_settings_saved(self):
        self.config = load_config()
        self.registry.config = self.config
        self.indexer.config = self.config
        self.project_service = ProjectService(self.config)
        self.audit_service = AuditService(self.config)
        self._refresh_data()

    def _on_bridge_audit_received(self, project: str, wave: str):
        """Called when loopback bridge writes an audit wave to disk."""
        self.root.after(0, self._refresh_data)

    def _pack_single_project(self, project: Project):
        if self.is_packing:
            return
        self._start_pack_worker([project])

    def _pack_all_enabled(self):
        if self.is_packing:
            return
        targets = [p for p in self.registry.projects if p.enabled]
        if not targets:
            messagebox.showinfo(__app_name__, t("status.no_enabled_projects"), parent=self.root)
            return
        self._start_pack_worker(targets)

    def _start_pack_worker(self, projects: list[Project]):
        self.is_packing = True
        self.cancel_event.clear()
        self.btn_cancel.configure(state="normal")
        self.lbl_status.configure(text=t("status.pack_starting"))

        threading.Thread(target=self._pack_worker, args=(projects,), daemon=True).start()

    def _pack_worker(self, projects: list[Project]):
        excludes = set(self.config.packing.excludes)
        output_dir = Path(self.config.packing.output_dir or str(app_dir()))
        total = len(projects)

        for idx, p in enumerate(projects, 1):
            if self.cancel_event.is_set():
                self.ui_queue.put(("status", t("status.pack_cancelled")))
                break

            self.ui_queue.put(("status", t("status.packing_fmt", i=idx, n=total, name=p.display_name)))

            extra_meta = {}
            if self.config.packing.manifest_enabled and p.source_path:
                saipen_info = get_saipen_info(p.source_path)
                extra_meta["saipen_detected"] = saipen_info.detected
                if saipen_info.detected:
                    extra_meta["git"] = {
                        "branch": saipen_info.git_branch,
                        "head": saipen_info.git_head,
                        "dirty": saipen_info.git_dirty,
                        "changed_files": saipen_info.git_changed_files,
                    }

            res = pack_single(
                source_path=p.source_path,
                output_dir=output_dir,
                archive_stem=p.archive_name or p.display_name,
                excludes=excludes,
                delete_old=self.config.packing.delete_old,
                include_timestamp=getattr(self.config.packing, 'include_timestamp', True),
                cancel_event=self.cancel_event,
                progress_callback=lambda a, b, cur: self.ui_queue.put(("progress", a, b, cur)),
                manifest_meta={"project_name": p.display_name, "extra_meta": extra_meta} if self.config.packing.manifest_enabled else None,
            )

            if res.success:
                archive_name = res.output_path.name if res.output_path else ""
                # Persist through the registry transaction; disk is canonical.
                self.registry.edit_project(
                    p.id, lambda pr, an=archive_name: setattr(pr, "last_pack_time", an)
                )
                self.ui_queue.put(("archive_ready", p.id))
                self.ui_queue.put(("status", t("status.pack_ok_fmt", name=p.display_name, files=res.files_added, size=human_mb(res.archive_bytes))))
            else:
                self.ui_queue.put(("status", t("status.pack_fail_fmt", name=p.display_name, err=res.error_message)))

        self.ui_queue.put(("done",))

    def _cancel_packing(self):
        self.cancel_event.set()
        self.lbl_status.configure(text=t("status.cancelling"))

    def _process_queue(self):
        while not self.ui_queue.empty():
            msg = self.ui_queue.get()
            tag = msg[0]
            if tag == "status":
                self.lbl_status.configure(text=msg[1])
            elif tag == "progress":
                added, bytes_w, cur = msg[1], msg[2], msg[3]
                self.lbl_status.configure(text=t("status.pack_progress_fmt", files=added, size=human_mb(bytes_w), file=Path(cur).name))
            elif tag == "archive_ready":
                # Refresh rows so the COPY ARCHIVE button flips from disabled to enabled.
                self._refresh_data()
            elif tag == "saipen_result":
                proj_id, s_info = msg[1], msg[2]
                old = self.saipen_cache.get(proj_id)
                self.saipen_cache[proj_id] = s_info
                if old != s_info:
                    self._rebuild_grid()
            elif tag == "done":
                self.is_packing = False
                self.btn_cancel.configure(state="disabled")
                self.lbl_status.configure(text=t("status.pack_finished"))

        self._gen_tick = getattr(self, "_gen_tick", 0) + 1
        if self._gen_tick >= 10:
            self._gen_tick = 0
            try:
                from audapack.bridge.state import get_audit_generation
                gen_data = get_audit_generation()
                cur_gen = int(gen_data.get("generation", 0))
                if cur_gen != getattr(self, "_last_generation", 0):
                    self._last_generation = cur_gen
                    self._refresh_data()
            except Exception:
                pass

        self.root.after(100, self._process_queue)


def run_gui() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            myappid = "vacterro.audapack.gui.1.0"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    root = tk.Tk()
    _app = MainWindow(root)
    root.mainloop()
    return 0
