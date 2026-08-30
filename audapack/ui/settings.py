"""Settings dialog and component manager interface for AUDAPACK."""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from audapack.bridge.lifecycle import is_bridge_healthy
from audapack.components.manager import ComponentManager
from audapack.config import AppConfig, get_user_runtime_dir, load_config
from audapack.ui.i18n import t
from audapack.ui.theme import FONT_FAMILY, PALETTE


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, config: Optional[AppConfig] = None, on_saved: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.title(t("settings.title"))
        self.geometry("720x640")
        self.configure(bg=PALETTE["background"])
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.config = config or load_config()
        self.on_saved = on_saved
        self.comp_mgr = ComponentManager(self.config)

        self._build_ui()
        self._refresh_components()

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        # TAB 1: General & Packing Settings
        tab_pack = tk.Frame(notebook, bg=PALETTE["surface"], padx=12, pady=12)
        notebook.add(tab_pack, text=t("settings.tab.packing"))

        # Output dir
        tk.Label(tab_pack, text=t("settings.output_dir"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(0, 2))
        row_out = tk.Frame(tab_pack, bg=PALETTE["surface"])
        row_out.pack(fill="x", pady=(0, 8))
        self.ent_out = tk.Entry(row_out, bg=PALETTE["backgroundSoft"], fg=PALETTE["textPrimary"], insertbackground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9), relief="sunken", bd=1, highlightthickness=0)
        self.ent_out.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=3)
        self.ent_out.insert(0, self.config.packing.output_dir)
        btn_browse_out = tk.Button(row_out, text=t("dialog.browse"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._browse_output)
        btn_browse_out.pack(side="right")

        # Audit root
        tk.Label(tab_pack, text=t("settings.audit_root"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(4, 2))
        row_aud = tk.Frame(tab_pack, bg=PALETTE["surface"])
        row_aud.pack(fill="x", pady=(0, 8))
        self.ent_aud = tk.Entry(row_aud, bg=PALETTE["backgroundSoft"], fg=PALETTE["textPrimary"], insertbackground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9), relief="sunken", bd=1, highlightthickness=0)
        self.ent_aud.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=3)
        self.ent_aud.insert(0, self.config.audits.root)
        btn_browse_aud = tk.Button(row_aud, text=t("dialog.browse"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._browse_audit_root)
        btn_browse_aud.pack(side="right")

        # Options
        self.del_old_val = self.config.packing.delete_old
        row_del = tk.Frame(tab_pack, bg=PALETTE["surface"])
        row_del.pack(fill="x", pady=(4, 4))
        self.btn_chk_del = tk.Button(
            row_del,
            text="✓" if self.del_old_val else "",
            bg=PALETTE["surfaceAlt"] if self.del_old_val else PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if self.del_old_val else PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="sunken" if self.del_old_val else "raised",
            bd=1,
            width=2,
            height=1,
            padx=0,
            pady=0,
            highlightthickness=0,
            command=self._toggle_del_old,
        )
        self.btn_chk_del.pack(side="left", padx=(0, 6))
        lbl_del = tk.Label(row_del, text=t("settings.delete_old"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9))
        lbl_del.pack(side="left")

        self.manifest_val = self.config.packing.manifest_enabled
        row_man = tk.Frame(tab_pack, bg=PALETTE["surface"])
        row_man.pack(fill="x", pady=(2, 8))
        self.btn_chk_man = tk.Button(
            row_man,
            text="✓" if self.manifest_val else "",
            bg=PALETTE["surfaceAlt"] if self.manifest_val else PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if self.manifest_val else PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="sunken" if self.manifest_val else "raised",
            bd=1,
            width=2,
            height=1,
            padx=0,
            pady=0,
            highlightthickness=0,
            command=self._toggle_manifest,
        )
        self.btn_chk_man.pack(side="left", padx=(0, 6))
        lbl_man = tk.Label(row_man, text=t("settings.manifest"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9))
        lbl_man.pack(side="left")

        # Excludes
        tk.Label(tab_pack, text=t("settings.excludes"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(4, 2))
        self.txt_excludes = tk.Text(tab_pack, bg=PALETTE["backgroundSoft"], fg=PALETTE["textPrimary"], insertbackground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), height=5, relief="sunken", bd=1, highlightthickness=0)
        self.txt_excludes.pack(fill="both", expand=True, pady=(0, 4))
        self.txt_excludes.insert("1.0", "\n".join(self.config.packing.excludes))

        # Restore defaults & Runtime isolation hint
        row_res = tk.Frame(tab_pack, bg=PALETTE["surface"])
        row_res.pack(fill="x", pady=(4, 2))
        btn_res_def = tk.Button(
            row_res,
            text=t("settings.restore_defaults"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="raised",
            bd=1,
            highlightthickness=0,
            padx=8,
            command=self._restore_defaults,
        )
        btn_res_def.pack(side="left")

        runtime_dir = get_user_runtime_dir()
        tk.Label(tab_pack, text=t("settings.runtime_dir_fmt", path=runtime_dir), bg=PALETTE["surface"], fg=PALETTE["textMuted"], font=(FONT_FAMILY, 7)).pack(anchor="w", pady=(2, 0))

        # TAB 2: Bridge, Autostart & Components
        tab_comp = tk.Frame(notebook, bg=PALETTE["surface"], padx=10, pady=10)
        notebook.add(tab_comp, text=t("settings.tab.bridge"))

        # Bridge Box
        box_br = tk.LabelFrame(tab_comp, text=t("settings.bridge_box"), bg=PALETTE["surface"], fg=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9, "bold"), padx=8, pady=6, bd=1, relief="ridge")
        box_br.pack(fill="x", pady=(0, 6))

        self.lbl_br_st = tk.Label(box_br, text=t("settings.br_status_checking"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9))
        self.lbl_br_st.pack(anchor="w", pady=(0, 4))

        row_br_actions = tk.Frame(box_br, bg=PALETTE["surface"])
        row_br_actions.pack(fill="x", pady=2)
        self.btn_br_start = tk.Button(row_br_actions, text=t("settings.br_start"), bg=PALETTE["surfaceRaised"], fg=PALETTE["successFg"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["successFg"], font=(FONT_FAMILY, 8, "bold"), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._start_bridge)
        self.btn_br_start.pack(side="left", padx=(0, 4))
        self.btn_br_stop = tk.Button(row_br_actions, text=t("settings.br_stop"), bg=PALETTE["surfaceRaised"], fg=PALETTE["dangerText"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["dangerText"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._stop_bridge)
        self.btn_br_stop.pack(side="left", padx=4)
        self.btn_br_restart = tk.Button(row_br_actions, text=t("settings.br_restart"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._restart_bridge)
        self.btn_br_restart.pack(side="left", padx=4)
        btn_copy_tok = tk.Button(row_br_actions, text=t("settings.br_copy_token"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._copy_token)
        btn_copy_tok.pack(side="left", padx=4)

        self.btn_legacy_takeover = tk.Button(row_br_actions, text=t("settings.br_takeover"), bg=PALETTE["surfaceRaised"], fg=PALETTE["copied"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["copied"], font=(FONT_FAMILY, 8, "bold"), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._takeover_legacy)
        self.btn_legacy_takeover.pack(side="right", padx=4)

        # Autostart Box
        box_auto = tk.LabelFrame(tab_comp, text=t("settings.autostart_box"), bg=PALETTE["surface"], fg=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9, "bold"), padx=8, pady=6, bd=1, relief="ridge")
        box_auto.pack(fill="x", pady=(0, 6))

        self.lbl_auto_st = tk.Label(box_auto, text=t("settings.auto_status_checking"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9))
        self.lbl_auto_st.pack(anchor="w", pady=(0, 4))

        row_auto_actions = tk.Frame(box_auto, bg=PALETTE["surface"])
        row_auto_actions.pack(fill="x", pady=2)
        self.btn_auto_install = tk.Button(row_auto_actions, text=t("settings.auto_install"), bg=PALETTE["surfaceRaised"], fg=PALETTE["borderHighlight"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8, "bold"), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._install_autostart)
        self.btn_auto_install.pack(side="left", padx=(0, 4))
        self.btn_auto_remove = tk.Button(row_auto_actions, text=t("settings.auto_remove"), bg=PALETTE["surfaceRaised"], fg=PALETTE["dangerText"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["dangerText"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._remove_autostart)
        self.btn_auto_remove.pack(side="left", padx=4)
        self.btn_auto_repair = tk.Button(row_auto_actions, text=t("settings.auto_repair"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._repair_autostart)
        self.btn_auto_repair.pack(side="left", padx=4)

        # Context Menu Box
        box_ctx = tk.LabelFrame(tab_comp, text=t("settings.context_menu_box"), bg=PALETTE["surface"], fg=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9, "bold"), padx=8, pady=6, bd=1, relief="ridge")
        box_ctx.pack(fill="x", pady=(0, 6))

        row_ctx_st = tk.Frame(box_ctx, bg=PALETTE["surface"])
        row_ctx_st.pack(fill="x")
        self.lbl_ctx_st = tk.Label(row_ctx_st, text=t("settings.br_status_checking"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9))
        self.lbl_ctx_st.pack(side="left")
        self.btn_ctx_install = tk.Button(row_ctx_st, text=t("settings.ctx_install"), bg=PALETTE["surfaceRaised"], fg=PALETTE["borderHighlight"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8, "bold"), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._install_ctx)
        self.btn_ctx_install.pack(side="right", padx=4)
        self.btn_ctx_remove = tk.Button(row_ctx_st, text=t("settings.ctx_remove"), bg=PALETTE["surfaceRaised"], fg=PALETTE["dangerText"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["dangerText"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._remove_ctx)
        self.btn_ctx_remove.pack(side="right", padx=4)

        # Widget Box
        box_wg = tk.LabelFrame(tab_comp, text=t("settings.widget_box"), bg=PALETTE["surface"], fg=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9, "bold"), padx=8, pady=6, bd=1, relief="ridge")
        box_wg.pack(fill="x", pady=(0, 4))

        row_wg_st = tk.Frame(box_wg, bg=PALETTE["surface"])
        row_wg_st.pack(fill="x")
        self.lbl_wg_st = tk.Label(row_wg_st, text=t("settings.widget_status_fmt", ver="0.0.01"), bg=PALETTE["surface"], fg=PALETTE["textPrimary"], font=(FONT_FAMILY, 9))
        self.lbl_wg_st.pack(side="left")
        btn_install_wg = tk.Button(row_wg_st, text=t("settings.widget_install"), bg=PALETTE["surfaceRaised"], fg=PALETTE["borderHighlight"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8, "bold"), relief="raised", bd=2, highlightthickness=0, padx=6, command=self._install_widget)
        btn_install_wg.pack(side="right", padx=4)

        # Bottom Buttons
        btn_bar = tk.Frame(self, bg=PALETTE["background"], padx=8, pady=8)
        btn_bar.pack(fill="x", side="bottom")

        btn_save = tk.Button(btn_bar, text=t("settings.save"), bg=PALETTE["surfaceRaised"], fg=PALETTE["borderHighlight"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 9, "bold"), relief="raised", bd=2, highlightthickness=0, padx=14, command=self._on_save)
        btn_save.pack(side="right", padx=4)

        btn_repair_all = tk.Button(btn_bar, text=t("settings.repair_all"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"], font=(FONT_FAMILY, 8), relief="raised", bd=2, highlightthickness=0, padx=8, command=self._repair_all)
        btn_repair_all.pack(side="left", padx=4)

        btn_cancel = tk.Button(btn_bar, text=t("settings.close"), bg=PALETTE["surfaceRaised"], fg=PALETTE["textMuted"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["textPrimary"], font=(FONT_FAMILY, 9), relief="raised", bd=2, highlightthickness=0, padx=8, command=self.destroy)
        btn_cancel.pack(side="right", padx=4)

    def _toggle_del_old(self):
        self.del_old_val = not self.del_old_val
        self.btn_chk_del.configure(
            text="✓" if self.del_old_val else "",
            bg=PALETTE["surfaceAlt"] if self.del_old_val else PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if self.del_old_val else PALETTE["textMuted"],
            relief="sunken" if self.del_old_val else "raised",
        )

    def _toggle_manifest(self):
        self.manifest_val = not self.manifest_val
        self.btn_chk_man.configure(
            text="✓" if self.manifest_val else "",
            bg=PALETTE["surfaceAlt"] if self.manifest_val else PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if self.manifest_val else PALETTE["textMuted"],
            relief="sunken" if self.manifest_val else "raised",
        )

    def _refresh_components(self):
        st = self.comp_mgr.get_components_status()

        # Bridge
        br = st["bridge"]
        if br["running"]:
            h_info = br.get("health_info", {})
            self.lbl_br_st.configure(
                text=t("settings.br_status_running_fmt", svc=h_info.get('service', 'AUDAPACK Bridge'), v=h_info.get('version', ''), api=h_info.get('api_version', ''), port=br['port']),
                fg=PALETTE["successFg"],
            )
            self.btn_br_start.configure(state="disabled")
            self.btn_br_stop.configure(state="normal")
            self.btn_legacy_takeover.pack_forget()
        elif br.get("status") == "LEGACY_RUNNING":
            self.lbl_br_st.configure(
                text=t("settings.br_status_legacy_fmt", port=br['port']),
                fg=PALETTE["dangerText"],
            )
            self.btn_br_start.configure(state="normal")
            self.btn_br_stop.configure(state="disabled")
            self.btn_legacy_takeover.pack(side="right", padx=4)
        else:
            self.lbl_br_st.configure(text=t("settings.br_status_stopped_fmt", port=br['port']), fg=PALETTE["textMuted"])
            self.btn_br_start.configure(state="normal")
            self.btn_br_stop.configure(state="disabled")
            if st.get("legacy", {}).get("legacy_task_exists"):
                self.btn_legacy_takeover.pack(side="right", padx=4)
            else:
                self.btn_legacy_takeover.pack_forget()

        # Autostart
        auto = st["autostart"]
        if auto["status_text"] == "INSTALLED":
            self.lbl_auto_st.configure(text=t("settings.auto_status_installed"), fg=PALETTE["successFg"])
            self.btn_auto_install.configure(state="disabled")
            self.btn_auto_remove.configure(state="normal")
        elif auto["status_text"] == "BROKEN":
            self.lbl_auto_st.configure(text=t("settings.auto_status_broken"), fg=PALETTE["dangerText"])
            self.btn_auto_install.configure(state="normal")
            self.btn_auto_remove.configure(state="normal")
        else:
            self.lbl_auto_st.configure(text=t("settings.auto_status_none"), fg=PALETTE["textMuted"])
            self.btn_auto_install.configure(state="normal")
            self.btn_auto_remove.configure(state="disabled")

        # Context menu
        if st["context_menu"]["installed"]:
            self.lbl_ctx_st.configure(text=t("settings.ctx_status_installed"), fg=PALETTE["successFg"])
        else:
            self.lbl_ctx_st.configure(text=t("settings.ctx_status_none"), fg=PALETTE["textMuted"])

    def _browse_output(self):
        sel = filedialog.askdirectory(parent=self, title=t("dialog.browse_dir_title"))
        if sel:
            from audapack.config import normalize_native_path
            sel = normalize_native_path(sel)
            self.ent_out.delete(0, tk.END)
            self.ent_out.insert(0, sel)

    def _browse_audit_root(self):
        sel = filedialog.askdirectory(parent=self, title=t("dialog.browse_dir_title"))
        if sel:
            from audapack.config import normalize_native_path
            sel = normalize_native_path(sel)
            self.ent_aud.delete(0, tk.END)
            self.ent_aud.insert(0, sel)

    def _install_ctx(self):
        ok, msg = self.comp_mgr.install_context_menu()
        if ok:
            messagebox.showinfo("Context Menu", msg, parent=self)
        else:
            messagebox.showerror("Context Menu Error", msg, parent=self)
        self._refresh_components()

    def _remove_ctx(self):
        ok, msg = self.comp_mgr.remove_context_menu()
        if ok:
            messagebox.showinfo("Context Menu", msg, parent=self)
        else:
            messagebox.showerror("Context Menu Error", msg, parent=self)
        self._refresh_components()

    def _start_bridge(self):
        ok, msg = self.comp_mgr.start_bridge()
        if ok:
            messagebox.showinfo("Bridge", msg, parent=self)
        else:
            messagebox.showerror("Bridge Error", msg, parent=self)
        self._refresh_components()

    def _stop_bridge(self):
        ok, msg = self.comp_mgr.stop_bridge()
        if ok:
            messagebox.showinfo("Bridge", msg, parent=self)
        else:
            messagebox.showerror("Bridge Error", msg, parent=self)
        self._refresh_components()

    def _restart_bridge(self):
        ok, msg = self.comp_mgr.restart_bridge()
        if ok:
            messagebox.showinfo("Bridge", msg, parent=self)
        else:
            messagebox.showerror("Bridge Error", msg, parent=self)
        self._refresh_components()

    def _takeover_legacy(self):
        if not messagebox.askyesno("Legacy Bridge Takeover", t("settings.takeover_confirm"), parent=self):
            return
        ok, rep = self.comp_mgr.takeover_legacy_bridge()
        if ok:
            messagebox.showinfo("Takeover Complete", t("settings.takeover_done"), parent=self)
        else:
            errs = "\n".join(rep.get("errors", ["Unknown error"]))
            messagebox.showerror("Takeover Error", t("settings.takeover_fail_fmt", errs=errs), parent=self)
        self._refresh_components()

    def _install_autostart(self):
        ok, msg = self.comp_mgr.install_autostart()
        if ok:
            messagebox.showinfo("Autostart", msg, parent=self)
        else:
            messagebox.showerror("Autostart Error", msg, parent=self)
        self._refresh_components()

    def _remove_autostart(self):
        ok, msg = self.comp_mgr.remove_autostart()
        if ok:
            messagebox.showinfo("Autostart", msg, parent=self)
        else:
            messagebox.showerror("Autostart Error", msg, parent=self)
        self._refresh_components()

    def _repair_autostart(self):
        ok, msg = self.comp_mgr.repair_autostart()
        if ok:
            messagebox.showinfo("Autostart Repaired", msg, parent=self)
        else:
            messagebox.showerror("Autostart Repair Error", msg, parent=self)
        self._refresh_components()

    def _copy_token(self):
        tok = self.comp_mgr.get_bridge_token()
        self.clipboard_clear()
        self.clipboard_append(tok)
        messagebox.showinfo("Token Copied", t("settings.token_copied"), parent=self)

    def _install_widget(self):
        from audapack.components.widget import detect_installed_browsers, open_widget_in_browser

        browsers = detect_installed_browsers()
        bridge_healthy = is_bridge_healthy(self.config.bridge.host, self.config.bridge.port)

        # Dialog for picking browser
        dlg = tk.Toplevel(self)
        dlg.title(t("settings.browser_select_title"))
        dlg.geometry("520x360")
        dlg.configure(bg=PALETTE["background"])
        dlg.transient(self)
        dlg.grab_set()

        # Prompt
        tk.Label(
            dlg,
            text=t("settings.browser_select_prompt"),
            bg=PALETTE["background"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 9, "bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 4))

        # List frame with scrollbar
        list_frame = tk.Frame(dlg, bg=PALETTE["surface"], bd=1, relief="sunken")
        list_frame.pack(fill="both", expand=True, padx=12, pady=4)

        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        lb = tk.Listbox(
            list_frame,
            font=(FONT_FAMILY, 8),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textPrimary"],
            selectbackground=PALETTE["surfaceAlt"],
            selectforeground=PALETTE["borderHighlight"],
            highlightthickness=0,
            bd=0,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=lb.yview)
        scrollbar.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True)

        # Populate list
        pref = getattr(self.config.ui, "preferred_browser", "").strip().lower()
        sel_idx = 0

        def _format_item(b):
            tag = f" {t('settings.browser_running')}" if b.get("running") else ""
            return f"{b['name']}{tag}  —  {b['exe']}"

        for idx, b in enumerate(browsers):
            lb.insert(tk.END, _format_item(b))
            if pref and str(b["exe"]).lower() == pref:
                sel_idx = idx

        # If custom preferred browser is not in detected list, prepend it
        if pref and not any(str(b["exe"]).lower() == pref for b in browsers):
            custom_b = {"name": "Custom Browser", "exe": self.config.ui.preferred_browser, "running": False}
            browsers.insert(0, custom_b)
            lb.insert(0, _format_item(custom_b))
            sel_idx = 0

        if browsers:
            lb.selection_set(sel_idx)
            lb.see(sel_idx)

        # Action row: Browse button & Remember checkbox
        opt_row = tk.Frame(dlg, bg=PALETTE["background"])
        opt_row.pack(fill="x", padx=12, pady=4)

        remember_var = tk.BooleanVar(value=True)
        chk_rem = tk.Checkbutton(
            opt_row,
            text=t("settings.browser_remember"),
            variable=remember_var,
            bg=PALETTE["background"],
            fg=PALETTE["textSecondary"],
            activebackground=PALETTE["background"],
            activeforeground=PALETTE["borderHighlight"],
            selectcolor=PALETTE["surfaceAlt"],
            font=(FONT_FAMILY, 8),
            highlightthickness=0,
            bd=0,
        )
        chk_rem.pack(side="left")

        def _on_browse_exe():
            picked = filedialog.askopenfilename(
                title="Select Browser Executable",
                filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")],
                parent=dlg,
            )
            if picked and os.path.exists(picked):
                custom = {"name": Path(picked).stem.title(), "exe": str(Path(picked).resolve()), "running": False}
                browsers.append(custom)
                lb.insert(tk.END, _format_item(custom))
                new_idx = lb.size() - 1
                lb.selection_clear(0, tk.END)
                lb.selection_set(new_idx)
                lb.see(new_idx)

        btn_browse = tk.Button(
            opt_row,
            text=t("settings.browser_browse"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8),
            relief="raised",
            bd=1,
            highlightthickness=0,
            padx=6,
            command=_on_browse_exe,
        )
        btn_browse.pack(side="right")

        # Bottom buttons
        btn_bar = tk.Frame(dlg, bg=PALETTE["background"])
        btn_bar.pack(fill="x", padx=12, pady=(4, 10))

        result = [None]

        def _do_open():
            sel = lb.curselection()
            if not sel and browsers:
                sel = (0,)
            if sel and sel[0] < len(browsers):
                b = browsers[sel[0]]
                chosen_exe = b["exe"]
                if remember_var.get():
                    try:
                        from audapack.config import scoped_config_write
                        ok = scoped_config_write(lambda cfg: setattr(cfg.ui, "preferred_browser", chosen_exe))
                        if not ok:
                            messagebox.showerror("Save Error", "Could not persist browser preference.", parent=dlg)
                            return
                    except Exception as exc:
                        messagebox.showerror("Save Error", f"Browser preference save failed: {exc}", parent=dlg)
                        return
                open_widget_in_browser(chosen_exe, use_bridge=bridge_healthy)
                result[0] = b["name"]
            dlg.destroy()

        lb.bind("<Double-Button-1>", lambda e: _do_open())

        btn_open = tk.Button(
            btn_bar,
            text=t("settings.btn_open_install"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 9, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=12,
            command=_do_open,
        )
        btn_open.pack(side="right", padx=(4, 0))

        btn_cancel = tk.Button(
            btn_bar,
            text=t("dialog.cancel"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 8),
            relief="raised",
            bd=1,
            highlightthickness=0,
            padx=8,
            command=dlg.destroy,
        )
        btn_cancel.pack(side="right")

        self.wait_window(dlg)
        if result[0]:
            messagebox.showinfo("Widget", t("settings.browser_opened_fmt", name=result[0]), parent=self)

    def _repair_all(self):
        res = self.comp_mgr.repair_all()

        def status(v):
            return t("settings.repair_ok") if v.get("ok") else t("settings.repair_failed")

        lines = [
            t(
                "settings.repair_summary_fmt",
                ctx=status(res.get("context_menu", {})),
                br=status(res.get("bridge", {})),
                auto=status(res.get("autostart", {})),
            )
        ]
        messagebox.showinfo("Repair Summary", "\n".join(lines), parent=self)
        self._refresh_components()

    def _restore_defaults(self):
        from audapack.config import create_default_projects, scoped_config_write
        if messagebox.askyesno(t("settings.restore_defaults"), t("settings.restore_defaults_confirm"), parent=self):
            ok = scoped_config_write(lambda cfg: setattr(cfg, "projects", create_default_projects()))
            if not ok:
                messagebox.showerror("Save Error", t("settings.save_error"), parent=self)
                return
            self.config = load_config()
            if self.on_saved:
                try:
                    self.on_saved()
                except Exception:
                    pass
            messagebox.showinfo(t("settings.restore_defaults"), t("settings.restore_defaults_done"), parent=self)

    def _on_save(self):
        from audapack.config import scoped_config_write
        out_dir = self.ent_out.get().strip()
        aud_root = self.ent_aud.get().strip()
        del_old = self.del_old_val
        manifest = self.manifest_val
        raw_excl = self.txt_excludes.get("1.0", tk.END).strip().splitlines()
        excludes = [line.strip() for line in raw_excl if line.strip()]

        def _mutate(cfg):
            cfg.packing.output_dir = out_dir
            cfg.audits.root = aud_root
            cfg.packing.delete_old = del_old
            cfg.packing.manifest_enabled = manifest
            cfg.packing.excludes = excludes

        if scoped_config_write(_mutate):
            self.config = load_config()
            if self.on_saved:
                try:
                    self.on_saved()
                except Exception:
                    pass
            self.destroy()
        else:
            messagebox.showerror("Save Error", t("settings.save_error"), parent=self)
