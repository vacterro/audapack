"""Project row UI widgets with strict grid-based column alignment, Drag & Drop, and vintage controls."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from audapack.audits import format_age_str
from audapack.models import CANONICAL_GROUPS, AuditSnapshot, AuditTemperature, Project, SaipenInfo
from audapack.packing import find_archive_for_project, human_mb
from audapack.ui.i18n import t
from audapack.ui.theme import FONT_FAMILY, PALETTE, SPACING


class ProjectRow(tk.Frame):
    """Row widget representing a single registered project with Drag & Drop and grid alignment."""

    def __init__(
        self,
        parent: tk.Widget,
        project: Project,
        snapshot: Optional[AuditSnapshot],
        saipen_info: Optional[SaipenInfo],
        on_toggle_enabled: Callable[[Project, bool], None],
        on_pack: Callable[[Project], None],
        on_copy_audit: Callable[[Project, AuditSnapshot, tk.Button], None],
        on_edit: Callable[[Project], None],
        on_move: Callable[[Project], None],
        on_delete: Optional[Callable[[Project], None]] = None,
        on_move_step: Optional[Callable[[Project, int], None]] = None,
        on_move_to_group: Optional[Callable[[Project, str], None]] = None,
        on_drop_move: Optional[Callable[[Project, str, int], None]] = None,
        on_copy_archive: Optional[Callable[[Project, tk.Button], None]] = None,
        on_reset_copied: Optional[Callable[[Project], None]] = None,
        on_toggle_ignored: Optional[Callable[[Project, bool], None]] = None,
        on_paste_audit: Optional[Callable[[Project], None]] = None,
        output_dir: Optional[Path] = None,
        active_groups: Optional[list[str]] = None,
    ):
        self.project = project
        self.group = project.priority_group
        self.slot = project.slot
        self.snapshot = snapshot
        self.saipen_info = saipen_info
        self.on_copy_audit = on_copy_audit
        self.on_copy_archive = on_copy_archive or (lambda p, b: None)
        self.on_reset_copied = on_reset_copied or (lambda p: None)
        self.on_paste_audit = on_paste_audit or (lambda p: None)
        self.on_toggle_enabled = on_toggle_enabled
        self.on_toggle_ignored = on_toggle_ignored or (lambda p, ign: None)
        self.on_delete = on_delete or (lambda p: None)
        self.on_move_step = on_move_step or (lambda p, s: None)
        self.on_move_to_group = on_move_to_group or (lambda p, g: None)
        self.on_drop_move = on_drop_move or (lambda p, g, s: None)
        self._output_dir = output_dir
        self._drag_data: Optional[dict] = None

        self.is_enabled = bool(project.enabled)
        self.is_ignored = bool(getattr(project, "ignored", False))

        row_bg = PALETTE["backgroundSoft"] if self.is_ignored else PALETTE["surface"]
        row_relief = "groove" if self.is_ignored else "raised"
        super().__init__(parent, bg=row_bg, bd=1, relief=row_relief, padx=3, pady=2)

        # Configure symmetric grid columns
        self.columnconfigure(0, weight=0, minsize=48)   # Checkboxes (Enable + Dim)
        self.columnconfigure(1, weight=0, minsize=46)   # Slot & ▲/▼ spinner / Drag handle
        self.columnconfigure(2, weight=1)              # Project Name & Path (expands)
        self.columnconfigure(3, weight=0, minsize=60)   # Audit Wave Status
        self.columnconfigure(4, weight=0, minsize=88)   # Freshness Tag
        self.columnconfigure(5, weight=0, minsize=82)   # AUDIT copy button
        self.columnconfigure(6, weight=0, minsize=50)   # PACK button
        self.columnconfigure(7, weight=0, minsize=110)  # ARCHIVE copy button
        self.columnconfigure(8, weight=0, minsize=32)   # Menu button

        # 0. Custom Golden Toggle Checkbox & Mute/Dim Checkbox
        chk_frame = tk.Frame(self, bg=row_bg)
        chk_frame.grid(row=0, column=0, sticky="w", padx=(2, 2))

        self.btn_chk = tk.Button(
            chk_frame,
            text="✓" if self.is_enabled else "",
            bg=PALETTE["surfaceAlt"] if self.is_enabled else PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if self.is_enabled else PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="sunken" if self.is_enabled else "raised",
            bd=1,
            width=2,
            height=1,
            padx=0,
            pady=0,
            highlightthickness=0,
            command=self._on_toggle,
        )
        self.btn_chk.pack(side="left", padx=(0, 2))

        self.btn_ign = tk.Button(
            chk_frame,
            text="⊘" if self.is_ignored else "",
            bg=PALETTE["surfaceAlt"] if self.is_ignored else PALETTE["surfaceRaised"],
            fg=PALETTE["warningFg"] if self.is_ignored else PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["warningFg"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="sunken" if self.is_ignored else "raised",
            bd=1,
            width=2,
            height=1,
            padx=0,
            pady=0,
            highlightthickness=0,
            command=self._on_toggle_ignored,
        )
        self.btn_ign.pack(side="left")

        # 1. Slot Box with Stacked Vertical Up/Down buttons (Drag Handle)
        slot_frame = tk.Frame(self, bg=PALETTE["surfaceRaised"], bd=1, relief="sunken", cursor="fleur")
        slot_frame.grid(row=0, column=1, sticky="w", padx=(0, 6))

        lbl_slot = tk.Label(
            slot_frame,
            text=f"#{project.slot}",
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textPrimary"],
            font=(FONT_FAMILY, 8, "bold"),
            width=3,
            cursor="fleur",
        )
        lbl_slot.pack(side="left", padx=1)

        spin_box = tk.Frame(slot_frame, bg=PALETTE["surfaceRaised"])
        spin_box.pack(side="left")

        btn_up = tk.Button(
            spin_box,
            text="▲",
            bg=PALETTE["surfaceAlt"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surface"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 5),
            relief="raised",
            bd=1,
            highlightthickness=0,
            padx=2,
            pady=0,
            command=lambda: self.on_move_step(project, -1),
        )
        btn_up.pack(side="top", fill="x")

        btn_down = tk.Button(
            spin_box,
            text="▼",
            bg=PALETTE["surfaceAlt"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surface"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 5),
            relief="raised",
            bd=1,
            highlightthickness=0,
            padx=2,
            pady=0,
            command=lambda: self.on_move_step(project, 1),
        )
        btn_down.pack(side="bottom", fill="x")

        # Determine audit readiness and copied state early for strikethrough styling
        completed_waves = snapshot.completed_waves if snapshot else 0
        all3_ready = snapshot.all3_ready if snapshot else False
        is_copied = bool(
            all3_ready
            and snapshot
            and snapshot.all3_sha256
            and project.last_copied_audit_hash == snapshot.all3_sha256
        )

        # 2. Project Name, Badges & Path Info (Flexible Expanding Column)
        info_frame = tk.Frame(self, bg=PALETTE["surface"], cursor="fleur")
        info_frame.grid(row=0, column=2, sticky="ew", padx=(0, 8))

        path_exists = project.source_path and Path(project.source_path).exists()
        if is_copied:
            name_font = (FONT_FAMILY, 9, "bold overstrike")
            name_color = PALETTE["copied"]
        else:
            name_font = (FONT_FAMILY, 9, "bold")
            name_color = PALETTE["textPrimary"] if path_exists else PALETTE["dangerText"]

        title_box = tk.Frame(info_frame, bg=PALETTE["surface"], cursor="fleur")
        title_box.pack(side="top", anchor="w", fill="x")

        lbl_name = tk.Label(
            title_box,
            text=project.display_name,
            bg=PALETTE["surface"],
            fg=name_color,
            font=name_font,
            anchor="w",
            cursor="fleur",
        )
        lbl_name.pack(side="left")
        lbl_name.bind("<Button-3>", lambda e: self.on_reset_copied(self.project))

        if self.is_ignored:
            lbl_muted_badge = tk.Label(
                title_box,
                text=t("row.muted_badge"),
                bg=PALETTE["surfaceAlt"],
                fg=PALETTE["textMuted"],
                font=(FONT_FAMILY, 7, "bold"),
                relief="sunken",
                bd=1,
                padx=3,
                cursor="hand2",
            )
            lbl_muted_badge.pack(side="left", padx=(4, 2))
            lbl_muted_badge.bind("<Button-1>", lambda e: self._on_toggle_ignored())

        if not path_exists:
            lbl_missing = tk.Label(
                title_box,
                text=t("row.missing_path"),
                bg=PALETTE["surface"],
                fg=PALETTE["dangerText"],
                font=(FONT_FAMILY, 7, "bold"),
            )
            lbl_missing.pack(side="left", padx=(4, 0))

        if saipen_info and saipen_info.detected:
            lbl_saipen = tk.Label(
                title_box,
                text=t("row.saipen"),
                bg=PALETTE["surfaceAlt"],
                fg=PALETTE["textSecondary"],
                font=(FONT_FAMILY, 7, "bold"),
                relief="ridge",
                bd=1,
                padx=2,
            )
            lbl_saipen.pack(side="left", padx=(6, 2))

            if saipen_info.git_dirty:
                git_txt = t("row.dirty_fmt", n=saipen_info.git_changed_files)
                git_fg = PALETTE["warningFg"]
            else:
                git_txt = t("row.clean")
                git_fg = PALETTE["successFg"]
            lbl_git = tk.Label(
                title_box,
                text=git_txt,
                bg=PALETTE["surface"],
                fg=git_fg,
                font=(FONT_FAMILY, 7),
            )
            lbl_git.pack(side="left", padx=(0, 4))

        lbl_path = tk.Label(
            info_frame,
            text=project.source_path or "No source path assigned",
            bg=PALETTE["surface"],
            fg=PALETTE["textMuted"],
            font=(FONT_FAMILY, 7),
            anchor="w",
            cursor="fleur",
        )
        lbl_path.pack(side="bottom", anchor="w")

        # 3. Audit Wave Status Tag (Column 3)
        if all3_ready:
            ready_txt = "✓ 3/3"
            ready_bg = PALETTE["success"]
            ready_fg = PALETTE["copied"] if is_copied else PALETTE["successFg"]
        elif completed_waves > 0:
            ready_txt = f"{completed_waves}/3"
            ready_bg = PALETTE["surfaceAlt"]
            ready_fg = PALETTE["warningFg"]
        else:
            ready_txt = "0/3"
            ready_bg = PALETTE["surfaceRaised"]
            ready_fg = PALETTE["textMuted"]

        self.lbl_ready = tk.Label(
            self,
            text=ready_txt,
            bg=ready_bg,
            fg=ready_fg,
            font=(FONT_FAMILY, 8, "bold"),
            relief="sunken",
            bd=1,
            width=6,
        )
        self.lbl_ready.grid(row=0, column=3, sticky="e", padx=(0, 4))

        # 4. Temperature / Freshness Tag (Column 4)
        temp = snapshot.temperature if snapshot else AuditTemperature.NONE
        age_str = format_age_str(snapshot.audit_age_seconds) if snapshot else ""
        temp_colors = {
            AuditTemperature.HOT: (PALETTE["hotBg"], PALETTE["hotFg"]),
            AuditTemperature.WARM: (PALETTE["warmBg"], PALETTE["warmFg"]),
            AuditTemperature.COOL: (PALETTE["coolBg"], PALETTE["coolFg"]),
            AuditTemperature.COLD: (PALETTE["coldBg"], PALETTE["coldFg"]),
            AuditTemperature.STALE: (PALETTE["staleBg"], PALETTE["staleFg"]),
            AuditTemperature.NONE: (PALETTE["surfaceRaised"], PALETTE["textMuted"]),
        }
        t_bg, t_fg = temp_colors.get(temp, (PALETTE["surfaceRaised"], PALETTE["textMuted"]))
        if temp == AuditTemperature.HOT and age_str:
            temp_display = f"● {age_str}"
        elif temp == AuditTemperature.WARM and age_str:
            temp_display = f"● {age_str}"
        elif temp == AuditTemperature.COOL and age_str:
            temp_display = f"● {age_str}"
        elif temp == AuditTemperature.COLD and age_str:
            temp_display = f"● {age_str}"
        elif temp == AuditTemperature.STALE and age_str:
            temp_display = f"○ {age_str}"
        elif temp != AuditTemperature.NONE:
            temp_display = temp.value
        else:
            temp_display = "—"

        self.lbl_temp = tk.Label(
            self,
            text=temp_display,
            bg=t_bg,
            fg=t_fg,
            font=(FONT_FAMILY, 8, "bold"),
            relief="ridge",
            bd=1,
            width=10,
        )
        self.lbl_temp.grid(row=0, column=4, sticky="e", padx=(0, 4))

        # 5. COPY AUDIT Button (Column 5)
        self.btn_copy_audit = tk.Button(
            self,
            text=t("btn.copy_audit_done") if is_copied else t("btn.copy_audit"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["copied"] if is_copied else PALETTE["borderHighlight"] if all3_ready else PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold" if all3_ready else "normal"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            width=9,
            state="normal" if all3_ready else "disabled",
            command=lambda: on_copy_audit(project, snapshot, self.btn_copy_audit) if snapshot else None,
        )
        self.btn_copy_audit.grid(row=0, column=5, sticky="e", padx=(0, 4))
        self.btn_copy_audit.bind("<Button-3>", lambda e: self.on_reset_copied(self.project))

        # 6. PACK Button (Column 6)
        self.btn_pack = tk.Button(
            self,
            text=t("btn.pack"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if path_exists else PALETTE["textMuted"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            width=5,
            state="normal" if path_exists else "disabled",
            command=lambda: on_pack(project),
        )
        self.btn_pack.grid(row=0, column=6, sticky="e", padx=(0, 4))

        # 7. COPY ARCHIVE Button (Column 7) — with freshness indicator
        latest_archive = None
        archive_age_str = ""
        if self._output_dir is not None:
            try:
                latest_archive = find_archive_for_project(project, self._output_dir)
                if latest_archive and latest_archive.exists():
                    st = latest_archive.stat()
                    archive_mtime = datetime.fromtimestamp(st.st_mtime)
                    archive_age_seconds = max(0.0, (datetime.now() - archive_mtime).total_seconds())
                    archive_age_str = format_age_str(archive_age_seconds)
            except Exception:
                latest_archive = None

        archive_copied = bool(
            latest_archive
            and project.last_copied_archive_path
            and Path(project.last_copied_archive_path).resolve() == latest_archive.resolve()
        )

        if latest_archive:
            base_txt = t("btn.copy_archive_done") if archive_copied else t("btn.copy_archive")
            btn_text = f"{base_txt} ({archive_age_str})" if archive_age_str else base_txt
            btn_fg = PALETTE["copied"] if archive_copied else PALETTE["borderHighlight"]
            btn_state = "normal"
        else:
            btn_text = t("btn.copy_archive_no_archive")
            btn_fg = PALETTE["textMuted"]
            btn_state = "disabled"

        self.btn_copy_archive = tk.Button(
            self,
            text=btn_text,
            bg=PALETTE["surfaceRaised"],
            fg=btn_fg,
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold" if latest_archive else "normal"),
            relief="raised",
            bd=2,
            highlightthickness=0,
            width=13,
            state=btn_state,
            command=lambda: self.on_copy_archive(project, self.btn_copy_archive),
        )
        self.btn_copy_archive.grid(row=0, column=7, sticky="e", padx=(0, 4))

        # 8. Menu Button (Column 8)
        btn_menu = tk.Menubutton(
            self,
            text="···",
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textPrimary"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8, "bold"),
            relief="raised",
            bd=1,
            highlightthickness=0,
            width=3,
        )
        menu = tk.Menu(btn_menu, tearoff=0, bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"])
        menu.add_command(label=t("menu.move_up"), command=lambda: self.on_move_step(project, -1))
        menu.add_command(label=t("menu.move_down"), command=lambda: self.on_move_step(project, 1))

        menu_grp = tk.Menu(menu, tearoff=0, bg=PALETTE["surfaceRaised"], fg=PALETTE["textPrimary"], activebackground=PALETTE["surfaceAlt"], activeforeground=PALETTE["borderHighlight"])
        groups_list = active_groups or list(CANONICAL_GROUPS)
        for g in groups_list:
            menu_grp.add_command(label=t("menu.move_to_group_fmt", group=g), command=lambda target_g=g: self.on_move_to_group(project, target_g))
        menu.add_cascade(label=t("menu.move_to_group"), menu=menu_grp)

        menu.add_command(label=t("menu.move_dialog"), command=lambda: on_move(project))
        menu.add_separator()
        menu.add_command(label=t("menu.paste_audit"), command=lambda: self.on_paste_audit(project))
        menu.add_command(label=t("menu.copy_archive"), command=lambda: self.on_copy_archive(project, self.btn_copy_archive))
        menu.add_command(label=t("menu.unmute_project") if self.is_ignored else t("menu.mute_project"), command=self._on_toggle_ignored)
        menu.add_command(label=t("menu.edit"), command=lambda: on_edit(project))
        menu.add_command(label=t("menu.delete"), command=lambda: on_delete(project))
        btn_menu.configure(menu=menu)
        btn_menu.grid(row=0, column=8, sticky="e", padx=(0, 2))

        def _show_row_popup(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        # Bind Drag & Drop Events on Handles and Right-Click Context Menu
        for handle in [self, slot_frame, lbl_slot, lbl_name, title_box, info_frame, lbl_path]:
            handle.bind("<ButtonPress-1>", self._on_drag_start)
            handle.bind("<B1-Motion>", self._on_drag_motion)
            handle.bind("<ButtonRelease-1>", self._on_drag_release)
            handle.bind("<Button-3>", _show_row_popup)

    def _on_toggle(self):
        self.is_enabled = not self.is_enabled
        self.btn_chk.configure(
            text="✓" if self.is_enabled else "",
            bg=PALETTE["surfaceAlt"] if self.is_enabled else PALETTE["surfaceRaised"],
            fg=PALETTE["borderHighlight"] if self.is_enabled else PALETTE["textMuted"],
            relief="sunken" if self.is_enabled else "raised",
        )
        self.on_toggle_enabled(self.project, self.is_enabled)

    def _on_toggle_ignored(self):
        self.is_ignored = not self.is_ignored
        self.btn_ign.configure(
            text="⊘" if self.is_ignored else "",
            bg=PALETTE["surfaceAlt"] if self.is_ignored else PALETTE["surfaceRaised"],
            fg=PALETTE["warningFg"] if self.is_ignored else PALETTE["textMuted"],
            relief="sunken" if self.is_ignored else "raised",
        )
        self.on_toggle_ignored(self.project, self.is_ignored)

    def _on_drag_start(self, event):
        self._drag_data = {"x": event.x_root, "y": event.y_root, "active": False}

    def _on_drag_motion(self, event):
        if not self._drag_data:
            return
        dx = abs(event.x_root - self._drag_data["x"])
        dy = abs(event.y_root - self._drag_data["y"])
        if dx > 4 or dy > 4:
            self._drag_data["active"] = True
            try:
                self.winfo_toplevel().configure(cursor="fleur")
            except Exception:
                pass

    def _on_drag_release(self, event):
        try:
            self.winfo_toplevel().configure(cursor="")
        except Exception:
            pass
        if self._drag_data and self._drag_data.get("active"):
            try:
                target = self.winfo_toplevel().winfo_containing(event.x_root, event.y_root)
                cur = target
                while cur and cur != self.winfo_toplevel():
                    if hasattr(cur, "group") and hasattr(cur, "slot"):
                        self.on_drop_move(self.project, cur.group, cur.slot)
                        break
                    cur = cur.master
            except Exception:
                pass
        self._drag_data = None


class EmptySlotRow(tk.Frame):
    """Row widget representing an unassigned slot with matched grid column structure."""

    def __init__(
        self,
        parent: tk.Widget,
        group: str,
        slot: int,
        on_add: Callable[[str, int], None],
    ):
        super().__init__(parent, bg=PALETTE["backgroundSoft"], bd=1, relief="sunken", padx=3, pady=2)
        self.group = group
        self.slot = slot

        self.columnconfigure(0, weight=0, minsize=48)
        self.columnconfigure(1, weight=0, minsize=46)
        self.columnconfigure(2, weight=1)
        self.columnconfigure(3, weight=0, minsize=76)
        self.columnconfigure(4, weight=0, minsize=110)
        self.columnconfigure(5, weight=0, minsize=100)
        self.columnconfigure(6, weight=0, minsize=54)
        self.columnconfigure(7, weight=0, minsize=92)
        self.columnconfigure(8, weight=0, minsize=32)

        # 1. Slot Frame
        slot_frame = tk.Frame(self, bg=PALETTE["surfaceRaised"], bd=1, relief="sunken")
        slot_frame.grid(row=0, column=1, sticky="w", padx=(0, 6))

        lbl_slot = tk.Label(
            slot_frame,
            text=f"#{slot}",
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textMuted"],
            font=(FONT_FAMILY, 8),
            width=5,
        )
        lbl_slot.pack(side="left", padx=(1, 1))

        # 2. Empty Label
        lbl_empty = tk.Label(
            self,
            text=t("row.empty_slot_fmt", group=group, slot=slot),
            bg=PALETTE["backgroundSoft"],
            fg=PALETTE["textMuted"],
            font=(FONT_FAMILY, 8, "italic"),
            anchor="w",
        )
        lbl_empty.grid(row=0, column=2, sticky="w", padx=(0, 8))

        # 3. Add Project Button (Spans right columns)
        btn_add = tk.Button(
            self,
            text=t("row.add_project"),
            bg=PALETTE["surfaceRaised"],
            fg=PALETTE["textSecondary"],
            activebackground=PALETTE["surfaceAlt"],
            activeforeground=PALETTE["borderHighlight"],
            font=(FONT_FAMILY, 8),
            relief="raised",
            bd=1,
            highlightthickness=0,
            padx=8,
            command=lambda: on_add(group, slot),
        )
        btn_add.grid(row=0, column=5, columnspan=4, sticky="e", padx=(0, 2))