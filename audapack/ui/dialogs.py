"""Dialogs for AUDAPACK: Project Add/Edit, Move, Confirmations, and Error displays."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Optional

from audapack.models import CANONICAL_GROUPS, Project
from audapack.ui.i18n import t
from audapack.ui.theme import FONT_FAMILY, PALETTE, SPACING


def center_window_on_parent(window: tk.Toplevel, parent: tk.Widget, width: Optional[int] = None, height: Optional[int] = None):
    """Centers a Toplevel dialog directly over its parent window, preventing top-left screen popping."""
    window.update_idletasks()
    p_top = parent.winfo_toplevel()
    p_top.update_idletasks()

    w = width or window.winfo_reqwidth()
    h = height or window.winfo_reqheight()
    if w <= 10:
        w = 520
    if h <= 10:
        h = 280

    px = p_top.winfo_rootx()
    py = p_top.winfo_rooty()
    pw = p_top.winfo_width()
    ph = p_top.winfo_height()

    x = px + (pw - w) // 2
    y = py + (ph - h) // 2

    # Clamp within screen dimensions
    sw = window.winfo_screenwidth()
    sh = window.winfo_screenheight()
    x = max(10, min(x, sw - w - 10))
    y = max(10, min(y, sh - h - 40))

    window.geometry(f"{w}x{h}+{x}+{y}")


class ProjectEditDialog(tk.Toplevel):
    """Simplified Win95 dark golden Add/Edit Project modal with auto-slot assignment."""

    def __init__(
        self,
        parent: tk.Widget,
        project: Optional[Project] = None,
        default_group: str = "MAIN0",
        default_slot: Optional[int] = None,
        active_groups: Optional[list[str]] = None,
    ):
        super().__init__(parent)
        self.result: Optional[dict] = None
        self.project = project
        self.default_slot = default_slot
        self.active_groups = active_groups or list(CANONICAL_GROUPS)

        self.title(t("dialog.edit_title") if project else t("dialog.add_title"))
        self.configure(bg=PALETTE["background"])
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Layout Container
        frame = tk.Frame(self, bg=PALETTE["surface"], bd=2, relief="raised", padx=14, pady=12)
        frame.pack(fill="both", expand=True, padx=8, pady=8)

        # 1. Display Name
        tk.Label(
            frame,
            text=t("dialog.field.display_name"),
            bg=PALETTE["surface"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=6)

        self.ent_name = tk.Entry(
            frame,
            bg=PALETTE["backgroundSoft"],
            fg=PALETTE["textPrimary"],
            insertbackground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 9),
            relief="sunken",
            bd=1,
            highlightthickness=0,
            width=42,
        )
        self.ent_name.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)
        if project:
            self.ent_name.insert(0, project.display_name)

        # 2. Source Path
        tk.Label(
            frame,
            text=t("dialog.field.source_path"),
            bg=PALETTE["surface"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=6)

        self.ent_path = tk.Entry(
            frame,
            bg=PALETTE["backgroundSoft"],
            fg=PALETTE["textPrimary"],
            insertbackground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 9),
            relief="sunken",
            bd=1,
            highlightthickness=0,
            width=32,
        )
        self.ent_path.grid(row=1, column=1, sticky="ew", pady=6)
        if project:
            self.ent_path.insert(0, project.source_path)

        btn_browse = tk.Button(
            frame,
            text=t("dialog.browse"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=8,
            command=self._on_browse,
        )
        btn_browse.grid(row=1, column=2, padx=(6, 0), pady=6)

        # 3. Priority Group (Segmented Buttons: MAIN0, MAIN1, SIDE0, SIDE1)
        tk.Label(
            frame,
            text=t("dialog.field.priority_group"),
            bg=PALETTE["surface"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(row=2, column=0, sticky="w", pady=6)

        self.grp_var = tk.StringVar(value=project.priority_group if project else default_group)
        grp_box = tk.Frame(frame, bg=PALETTE["surface"])
        grp_box.grid(row=2, column=1, columnspan=2, sticky="w", pady=6)

        self.grp_buttons: dict[str, tk.Button] = {}
        for g in self.active_groups:
            btn = tk.Button(
                grp_box,
                text=g,
                activebackground=PALETTE["surfaceAlt"],
                activeforeground=PALETTE["borderHighlight"],
                relief="raised",
                bd=2,
                highlightthickness=0,
                padx=10,
                pady=2,
                command=lambda target=g: self._set_group(target),
            )
            btn.pack(side="left", padx=(0, 6))
            self.grp_buttons[g] = btn

        # 4. Audit Project Name Override
        tk.Label(
            frame,
            text=t("dialog.field.audit_override"),
            bg=PALETTE["surface"],
            fg=PALETTE["textSecondary"],
            font=(FONT_FAMILY, 8),
        ).grid(row=3, column=0, sticky="w", pady=6)

        self.ent_audit = tk.Entry(
            frame,
            bg=PALETTE["backgroundSoft"],
            fg=PALETTE["textPrimary"],
            insertbackground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 9),
            relief="sunken",
            bd=1,
            highlightthickness=0,
            width=42,
        )
        self.ent_audit.grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)
        if project and project.audit_project_name:
            self.ent_audit.insert(0, project.audit_project_name)

        # 5. Action Buttons
        btn_box = tk.Frame(frame, bg=PALETTE["surface"])
        btn_box.grid(row=4, column=0, columnspan=3, pady=(16, 0), sticky="e")

        btn_save = tk.Button(
            btn_box,
            text=t("dialog.save_edit") if project else t("dialog.save_add"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 9, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=14,
            command=self._on_save,
        )
        btn_save.pack(side="right", padx=4)

        btn_cancel = tk.Button(
            btn_box,
            text=t("dialog.cancel"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 9),
            relief="raised",
            bd=2,
            highlightthickness=0,
            padx=8,
            command=self.destroy,
        )
        btn_cancel.pack(side="right", padx=4)

        self._update_button_styles()
        center_window_on_parent(self, parent, 540, 270)

    def _set_group(self, group: str):
        self.grp_var.set(group)
        self._update_button_styles()

    def _update_button_styles(self):
        cur_g = self.grp_var.get()
        for g, btn in self.grp_buttons.items():
            if g == cur_g:
                btn.configure(
                    bg=PALETTE["surfaceAlt"],
                    fg=PALETTE["borderHighlight"],
                    relief="sunken",
                    font=(FONT_FAMILY, 9, "bold"),
                )
            else:
                btn.configure(
                    bg=PALETTE["surfaceRaised"],
                    fg=PALETTE["textSecondary"],
                    relief="raised",
                    font=(FONT_FAMILY, 9),
                )

    def _on_browse(self):
        sel = filedialog.askdirectory(parent=self, title=t("dialog.browse_dir_title"))
        if sel:
            from audapack.config import normalize_native_path
            sel = normalize_native_path(sel)
            self.ent_path.delete(0, tk.END)
            self.ent_path.insert(0, sel)
            if not self.ent_name.get().strip():
                self.ent_name.insert(0, Path(sel).name)

    def _on_save(self):
        name = self.ent_name.get().strip()
        path = self.ent_path.get().strip()
        if not name:
            return
        self.result = {
            "display_name": name,
            "source_path": path,
            "priority_group": self.grp_var.get(),
            "slot": self.project.slot if self.project else self.default_slot,
            "audit_project_name": self.ent_audit.get().strip() or name,
        }
        self.destroy()
