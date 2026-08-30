"""Qt Delegate for Project Room Rows (Wave M).

Renders compact Golden Default Win95-style rows:
- Group headers with distinct raised bevel
- Slot badge [1..6]
- Project name + audit badges
- Compact wave/age/ZIP indicators
- Launcher buttons [1..N] + [GG]
- Empty slot representation
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from audapack.audits import format_age_str
from audapack.models import AuditTemperature
from audapack.ui_qt.theme.golden_default import PALETTE

# Temperature color scheme — muted, readable on dark bg, not acidic.
# Each key maps to a hex color for the temperature name.
TEMP_COLORS = {
    "HOT": "#E8A860",
    "WARM": "#D4B878",
    "COOL": "#9AA8C8",
    "COLD": "#6E8CB0",
    "STALE": "#8A8078",
    "NONE": "#7A7A7A",
}


def compute_row_button_rects(row_rect: QRect, launchers: Optional[list[Any]] = None) -> tuple[list[tuple[Any, QRect]], QRect]:
    """Computes layout of [1]..[N] launcher buttons (letters OC/FB/CL/C1/C2/CF) on right edge.

    GG button removed as obsolete. Info [ⓘ] placed left of launchers.
    Returns (launcher_buttons, _unused_gg_rect) for compat.
    """
    # GG removed — keep dummy rect for compat but zero size
    gg_rect = QRect(row_rect.right() - 2, row_rect.top() + 2, 0, 20)
    if not launchers:
        return [], gg_rect

    enabled_launchers = [launcher for launcher in launchers if getattr(launcher, "enabled", True)]
    if not enabled_launchers:
        return [], gg_rect

    btn_w = 18  # widened for 2-char labels OC/FB/CL/C1
    gap = 2
    total_w = len(enabled_launchers) * btn_w + (len(enabled_launchers) - 1) * gap
    # hug right edge with 2px margin (was 36 with GG)
    start_x = row_rect.right() - total_w - 2

    cur_x = start_x
    result_buttons: list[tuple[Any, QRect]] = []
    for launcher in enabled_launchers:
        r = QRect(cur_x, row_rect.top() + 2, btn_w, 20)
        result_buttons.append((launcher, r))
        cur_x += btn_w + gap

    return result_buttons, gg_rect


def compute_info_button_rect(row_rect: QRect, launcher_buttons: list[tuple[Any, QRect]], gg_rect: QRect) -> QRect:
    """Info [ⓘ] button placed 4px left of the leftmost action button."""
    gap = 4
    info_w = 18
    if launcher_buttons:
        leftmost = launcher_buttons[0][1].left()
        x = leftmost - gap - info_w
    else:
        # no launchers — place near right edge where GG used to be
        x = row_rect.right() - info_w - 2
    return QRect(x, row_rect.top() + 2, info_w, 20)


class ProjectItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self._config = config
        self.font_main = QFont("Verdana", 9)
        self.font_main.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_bold = QFont("Verdana", 9, QFont.Weight.Bold)
        self.font_bold.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_mono = QFont("Verdana", 9)
        self.font_mono.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_small = QFont("Verdana", 8)
        self.font_small.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_tiny = QFont("Verdana", 7)
        self.font_tiny.setStyleStrategy(QFont.StyleStrategy.NoAntialias)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        node_type = index.data(Qt.ItemDataRole.UserRole + 7)  # node_type
        if node_type == "group":
            return QSize(option.rect.width(), 22)
        compact_rows = bool(getattr(getattr(self._config, "ui", None), "compact_rows", False))
        return QSize(option.rect.width(), 22 if compact_rows else 44)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        rect = option.rect

        node_type = index.data(Qt.ItemDataRole.UserRole + 7)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if node_type == "group":
            # Group header bar with Win95 Raised 2px Bevel
            grp_name = index.data(Qt.ItemDataRole.DisplayRole) or ""
            painter.fillRect(rect, QColor(PALETTE["surfaceRaised"]))

            # Top + Left highlight
            painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
            painter.drawLine(rect.left(), rect.top(), rect.right() - 1, rect.top())
            painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom() - 1)

            # Bottom + Right shadow
            painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
            painter.drawLine(rect.left(), rect.bottom() - 1, rect.right() - 1, rect.bottom() - 1)
            painter.drawLine(rect.right() - 1, rect.top(), rect.right() - 1, rect.bottom() - 1)

            painter.setFont(self.font_bold)
            painter.setPen(QColor(PALETTE["borderGolden"]))
            text_rect = rect.adjusted(4, 0, -8, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"▼  {grp_name}")
            painter.restore()
            return

        # ---- Slot row ----
        slot_num = index.data(Qt.ItemDataRole.UserRole + 4) or 1
        is_empty = index.data(Qt.ItemDataRole.UserRole + 6)
        display_name = index.data(Qt.ItemDataRole.UserRole + 2) or f"Slot {slot_num}"
        all_ready = bool(index.data(Qt.ItemDataRole.UserRole + 10))
        completed_waves = index.data(Qt.ItemDataRole.UserRole + 13) or 0
        temperature = index.data(Qt.ItemDataRole.UserRole + 8) or AuditTemperature.NONE
        pack_state = index.data(Qt.ItemDataRole.UserRole + 11) or "IDLE"
        is_ignored = bool(index.data(Qt.ItemDataRole.UserRole + 22))
        is_enabled = bool(index.data(Qt.ItemDataRole.UserRole + 5))
        if is_enabled is None:
            is_enabled = True

        # Row background — dimmed for ignored ("Done") or disabled projects
        if is_ignored or not is_enabled:
            bg = QColor(PALETTE["borderDark"]) if not is_selected else QColor(PALETTE["selection"])
        else:
            bg = QColor(PALETTE["selection"]) if is_selected else QColor(PALETTE["surface"])
        painter.fillRect(rect, bg)

        # Bottom separator
        painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
        painter.drawLine(rect.left(), rect.bottom() - 1, rect.right() - 1, rect.bottom() - 1)

        is_archive_ignored = bool(index.data(Qt.ItemDataRole.UserRole + 24))
        x = rect.left() + 2
        y = rect.top()
        h = rect.height()

        # 0. Enabled [E] — 14px, green when enabled, grey striked when disabled (ZIP packing gate)
        enabled_rect = QRect(x, y, 14, h)
        if is_enabled:
            painter.setFont(self.font_mono)
            painter.setPen(QColor(PALETTE["success"]))
            painter.drawText(enabled_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, "E")
        else:
            cb_e = QRect(x + 2, y + (h - 10) // 2, 10, 10)
            painter.setPen(QPen(QColor(PALETTE["dangerText"]), 1))
            painter.drawRect(cb_e)
            painter.setFont(self.font_tiny)
            painter.setPen(QColor(PALETTE["dangerText"]))
            painter.drawText(cb_e, Qt.AlignmentFlag.AlignCenter, "×")
        x += 14
        # 0a. Done checkbox [✓] — 14px wide, clickable
        done_rect = QRect(x, y, 14, h)
        if is_ignored:
            painter.setFont(self.font_mono)
            painter.setPen(QColor(PALETTE["success"]))
            painter.drawText(done_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, "\u2713")
        else:
            cb_rect = QRect(x + 2, y + (h - 10) // 2, 10, 10)
            painter.setPen(QPen(QColor(PALETTE["textMuted"]), 1))
            painter.drawRect(cb_rect)
        x += 14
        # 0b. Archive ignore [A] — 14px wide, clickable, tooltip via ⓘ popup
        arch_rect = QRect(x, y, 14, h)
        if is_archive_ignored:
            painter.setFont(self.font_mono)
            painter.setPen(QColor(PALETTE["warning"]))
            painter.drawText(arch_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, "A")
        else:
            cb2 = QRect(x + 2, y + (h - 10) // 2, 10, 10)
            painter.setPen(QPen(QColor(PALETTE["textMuted"]), 1))
            painter.drawRect(cb2)
            # small A hint inside empty box
            painter.setFont(self.font_tiny)
            painter.setPen(QColor(PALETTE["textMuted"]))
            painter.drawText(cb2, Qt.AlignmentFlag.AlignCenter, "A")
        x += 16

        # 1. Slot badge [1..6] — compact 24px
        slot_badge = f"[{slot_num}]"
        painter.setFont(self.font_mono)
        name_color = QColor(PALETTE["textMuted"]) if is_ignored else QColor(PALETTE["textSecondary"])
        painter.setPen(name_color)
        slot_w = 24
        painter.drawText(QRect(x, y, slot_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, slot_badge)
        x += slot_w + 2

        if is_empty:
            painter.setFont(self.font_small)
            painter.setPen(QColor(PALETTE["textMuted"]))
            painter.drawText(QRect(x, y, 200, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "[ EMPTY — DROP ]")
            painter.restore()
            return

        # 2. Compute right-side button area first
        launchers = getattr(self._config, "launchers", None) if self._config else None
        launcher_buttons, gg_rect = compute_row_button_rects(rect, launchers)
        info_rect = compute_info_button_rect(rect, launcher_buttons, gg_rect)
        buttons_start_x = info_rect.left()

        total_waves = index.data(Qt.ItemDataRole.UserRole + 15) or 3
        prof_label = index.data(Qt.ItemDataRole.UserRole + 19) or ("A10" if total_waves == 10 else "A3")
        dispatch_state = str(index.data(Qt.ItemDataRole.UserRole + 33) or "")
        dispatch_browser = str(index.data(Qt.ItemDataRole.UserRole + 35) or "")
        inaudit_label = str(index.data(Qt.ItemDataRole.UserRole + 40) or "")

        # ── Vertical indicator column (fixed width, stacked top→bottom) ────────
        # Every project row uses the SAME column layout: consistent order and
        # alignment, so the eye scans down one column instead of hunting badges.
        compact_rows = bool(getattr(getattr(self._config, "ui", None), "compact_rows", False))
        col_w = 230 if compact_rows else 110
        col_x = buttons_start_x - 4 - col_w

        # Data used by the column
        arc_data = index.data(Qt.ItemDataRole.UserRole + 18)
        sync_status = index.data(Qt.ItemDataRole.UserRole + 20) or "SYNCED"
        arc_exists, arc_size, arc_created, _arc_path = arc_data if arc_data else (False, "", "", None)
        audit_age_str = index.data(Qt.ItemDataRole.UserRole + 17) or ""
        temp_val = temperature.value if hasattr(temperature, "value") else str(temperature)
        arc_temp = index.data(Qt.ItemDataRole.UserRole + 23)  # archive_temperature
        arc_temp_val = arc_temp.value if hasattr(arc_temp, "value") else str(arc_temp) if arc_temp else ""
        pack_progress = index.data(Qt.ItemDataRole.UserRole + 26) or None
        pack_percent = index.data(Qt.ItemDataRole.UserRole + 27)
        archive_fresh_short = index.data(Qt.ItemDataRole.UserRole + 31) or "none"
        source_older = index.data(Qt.ItemDataRole.UserRole + 30)

        TC = TEMP_COLORS

        def _line_top(idx: int) -> int:
            return base_y + idx * line_h

        painter.setFont(self.font_small)
        line_h = 18
        base_y = y + 4

        # ── INAUDIT badge (kept tiny, only when layers exist)
        # ── Audit wave text (always audit, pack shown separately)
        if all_ready:
            wave_text = f"\u2713 {prof_label} {completed_waves}/{total_waves}"
            wave_color = QColor(PALETTE["success"])
        elif completed_waves > 0:
            wave_text = f"{prof_label} {completed_waves}/{total_waves}"
            wave_color = QColor(PALETTE["warning"])
        else:
            wave_text = f"{prof_label} 0/{total_waves}"
            wave_color = QColor(PALETTE["textMuted"])
        if dispatch_state and dispatch_state not in {"COMPLETE", "CANCELLED"}:
            state_labels = {
                "QUEUED": "WAIT",
                "LEASED": "ATTACH",
                "ARTIFACT_FETCHED": "ATTACH",
                "ATTACHED": "START",
                "START_PREPARED": "START",
                "STARTED": "AUDIT",
                "AUDITING": "AUDIT",
                "FINALIZING": "SAVE",
                "BLOCKED": "! BLOCKED",
                "FAILED": "FAILED",
                "RETRYABLE": "WAIT",
            }
            label = state_labels.get(dispatch_state, dispatch_state)
            suffix = f" {dispatch_browser}" if dispatch_browser and dispatch_state not in {"QUEUED", "RETRYABLE"} else ""
            wave_text = f"{label}{suffix}"
            wave_color = QColor(PALETTE["warning"] if dispatch_state not in {"BLOCKED", "FAILED"} else PALETTE["dangerText"])
        if compact_rows:
            wave_text = wave_text.replace("✓ ", "✓", 1)
        # Pack badge after ZIP
        pack_display = ""
        pack_color = QColor(PALETTE["textMuted"])
        pack_progress_text = ""  # e.g. "  [PACK 42% 1.2MB]" only used in single-line branch
        if pack_state and pack_state != "IDLE":
            if pack_state in ("PACKING", "QUEUED"):
                if pack_progress and isinstance(pack_progress, dict):
                    fa = int(pack_progress.get("files_added") or 0)
                    bw = int(pack_progress.get("bytes_written") or 0)
                    pct = int(round(float(pack_percent) if isinstance(pack_percent, (int, float)) else 0))
                    pct = max(0, min(99, pct))
                    size_mb = bw / (1024 * 1024)
                    if size_mb >= 1:
                        size_disp = f"{size_mb:.1f}MB"
                    elif bw > 0:
                        size_disp = f"{max(1, bw // 1024)}KB"
                    else:
                        size_disp = ""
                    label = f"PACK {pct}% {fa}f" + (f" {size_disp}" if size_disp else "")
                else:
                    label = pack_state
                pack_display = f"  [{label}]"
                pack_progress_text = pack_display
                pack_color = QColor(PALETTE["borderGolden"])
            elif pack_state == "COMPLETE":
                # show truncated archive name like reference "[COMPLET v:\__...]"
                arc_name = ""
                try:
                    _arc_path = arc_data[3] if arc_data and len(arc_data) > 3 else None
                    if _arc_path:
                        arc_name = str(_arc_path).split("\\")[-1][:12]
                except Exception:
                    arc_name = ""
                pack_display = f"  [COMPLET {arc_name}]" if arc_name else "  [COMPLET]"
                pack_color = QColor(PALETTE["success"])
            else:
                pack_display = f"  [{pack_state}]"
                pack_color = QColor(PALETTE["danger"])

        # Audit age inline after the wave text
        if audit_age_str:
            audit_color = QColor(TC.get(temp_val, PALETTE["borderMuted"]))
            audit_display = f"  {audit_age_str}"
        else:
            audit_color = QColor(PALETTE["textMuted"])
            audit_display = ""
        if compact_rows:
            audit_display = f" {audit_age_str.replace(' ', '')}" if audit_age_str else ""

        # copy counter
        copy_cnt = int(index.data(Qt.ItemDataRole.UserRole + 25) or 0)
        copy_display = f"  ×{copy_cnt}" if copy_cnt > 0 else ""
        inaudit_display = f"  {inaudit_label}" if inaudit_label else ""
        inaudit_color = QColor(PALETTE["borderGolden"]) if inaudit_label else QColor(PALETTE["textMuted"])

        # ── ZIP text — "ZIP: 156,7 MB 28.08 01:12" — size + creation date
        freshness_tag = ""
        if arc_exists:
            size_str = str(arc_size).replace(".", ",")  # 156.7 MB → 156,7 MB like screenshot
            arc_color = QColor(TC.get(arc_temp_val, PALETTE["textSecondary"]))
            if sync_status == "OUTDATED" or source_older is True:
                arc_color = QColor(PALETTE["dangerText"])
            # Coarse freshness tag at end of ZIP line so the user can see at a glance
            # whether the archive is fresh, stale, or old — and a small [NEW] if
            # the source tree has changed since the last pack.
            if source_older is True:
                freshness_tag = "  [SRC\u25B2]"  # source is newer than archive
            elif archive_fresh_short == "fresh":
                freshness_tag = "  [\u2713]"
            elif archive_fresh_short == "stale":
                freshness_tag = "  [\u00B7]"
            elif archive_fresh_short == "old":
                freshness_tag = "  [!]"
            if arc_created:
                zip_full = f"ZIP: {size_str} {arc_created}{freshness_tag}"
                zip_full_nofresh = f"ZIP: {size_str} {arc_created}"
                if painter.fontMetrics().horizontalAdvance(zip_full) <= col_w:
                    zip_text = zip_full
                elif painter.fontMetrics().horizontalAdvance(zip_full_nofresh) <= col_w:
                    zip_text = zip_full_nofresh
                else:
                    zip_text = f"ZIP: {size_str}"
            else:
                zip_text = f"ZIP: {size_str}{freshness_tag}"
            if compact_rows:
                # Full mode keeps creation/freshness details; compact mode
                # keeps the essential archive size inline on the one row.
                zip_text = f"ZIP {size_str.replace(' ', '')}"
                freshness_tag = ""
        else:
            arc_color = QColor(PALETTE["textMuted"])
            zip_text = "ZIP \u2014" if compact_rows else "\u2014"

        if compact_rows and pack_display:
            if pack_state in ("PACKING", "QUEUED"):
                pct = int(round(float(pack_percent))) if isinstance(pack_percent, (int, float)) else 0
                pack_display = f" [{pct}%]"
            elif pack_state == "COMPLETE":
                pack_display = " [OK]"
            else:
                pack_display = " [!]"

        # Try single-line layout if wave+age+copy+IA+ZIP+pack fits in col_w
        painter.setFont(self.font_small)
        single_gap = "  " if zip_text != "\u2014" else ""
        single_line = wave_text + audit_display + copy_display + inaudit_display + (single_gap + zip_text if zip_text != "\u2014" else "") + pack_display
        single_w = painter.fontMetrics().horizontalAdvance(single_line)
        if compact_rows or (zip_text != "\u2014" and single_w <= col_w):
            single_y = y + (h - line_h) // 2
            painter.setPen(wave_color)
            painter.drawText(QRect(col_x, single_y, col_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, wave_text)
            cur_x1 = painter.fontMetrics().horizontalAdvance(wave_text)
            if audit_display:
                painter.setPen(audit_color)
                painter.drawText(QRect(col_x + cur_x1, single_y, col_w - cur_x1, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, audit_display)
                cur_x1 += painter.fontMetrics().horizontalAdvance(audit_display)
            if copy_display:
                painter.setPen(QColor(PALETTE["borderGolden"]))
                painter.drawText(QRect(col_x + cur_x1, single_y, col_w - cur_x1, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, copy_display)
                cur_x1 += painter.fontMetrics().horizontalAdvance(copy_display)
            if inaudit_display:
                painter.setPen(inaudit_color)
                painter.drawText(QRect(col_x + cur_x1, single_y, col_w - cur_x1, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, inaudit_display)
                cur_x1 += painter.fontMetrics().horizontalAdvance(inaudit_display)
            if single_gap:
                gap_w = painter.fontMetrics().horizontalAdvance(single_gap)
                painter.setPen(arc_color)
                painter.drawText(QRect(col_x + cur_x1 + gap_w, single_y, col_w - cur_x1 - gap_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, zip_text)
                cur_x1 += gap_w + painter.fontMetrics().horizontalAdvance(zip_text)
            if pack_display:
                painter.setPen(pack_color)
                painter.drawText(QRect(col_x + cur_x1, single_y, col_w - cur_x1, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, pack_display)
        else:
            full_line1 = wave_text + audit_display + copy_display + inaudit_display
            painter.setFont(self.font_small)
            painter.setPen(wave_color)
            painter.drawText(QRect(col_x, _line_top(0), col_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, full_line1)
            if audit_display:
                w_w = painter.fontMetrics().horizontalAdvance(wave_text)
                painter.setPen(audit_color)
                painter.drawText(QRect(col_x + w_w, _line_top(0), col_w - w_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, audit_display)
            if copy_display:
                base_w = painter.fontMetrics().horizontalAdvance(wave_text + audit_display)
                painter.setPen(QColor(PALETTE["borderGolden"]))
                painter.drawText(QRect(col_x + base_w, _line_top(0), col_w - base_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, copy_display)
            if inaudit_display:
                base2 = painter.fontMetrics().horizontalAdvance(wave_text + audit_display + copy_display)
                painter.setPen(inaudit_color)
                painter.drawText(QRect(col_x + base2, _line_top(0), col_w - base2, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, inaudit_display)
            # line1 ZIP + pack together (or live progress bar while packing)
            painter.setFont(self.font_small)
            if pack_state in ("PACKING", "QUEUED") and isinstance(pack_progress, dict):
                pct = float(pack_percent) if isinstance(pack_percent, (int, float)) else 0.0
                pct = max(0.0, min(99.0, pct))
                bar_rect = QRect(col_x, _line_top(1) + (line_h - 8) // 2, col_w, 8)
                painter.fillRect(bar_rect, QColor(PALETTE["borderDark"]))
                fill_w = int(round(bar_rect.width() * pct / 100.0))
                if fill_w > 0:
                    painter.fillRect(QRect(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height()), QColor(PALETTE["borderGolden"]))
                painter.setPen(QColor(PALETTE["textPrimary"]))
                painter.drawText(bar_rect, Qt.AlignmentFlag.AlignCenter, pack_progress_text.strip())
            else:
                line1_text = zip_text + pack_display
                painter.setPen(arc_color)
                painter.drawText(QRect(col_x, _line_top(1), col_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, line1_text)
                if pack_display and zip_text != "\u2014":
                    zip_w = painter.fontMetrics().horizontalAdvance(zip_text)
                    painter.setPen(pack_color)
                    painter.drawText(QRect(col_x + zip_w, _line_top(1), col_w - zip_w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, pack_display)

        # 3. Project Name — fill the left area, vertically centered
        painter.setFont(self.font_bold)
        main_window = self.parent().window() if self.parent() is not None else None
        project_id = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")
        instance_count = 0
        if main_window is not None and hasattr(main_window, "_instance_monitor"):
            instance_count = len(main_window._instance_monitor.for_project(project_id))
        instance_prefix = f"▶{instance_count} " if instance_count else ""
        prefix_width = painter.fontMetrics().horizontalAdvance(instance_prefix)
        if instance_prefix:
            painter.setPen(QColor(PALETTE["success"]))
            painter.drawText(
                QRect(x, y, prefix_width, h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                instance_prefix,
            )
        if is_ignored:
            painter.setPen(QColor(PALETTE["textMuted"]))
        else:
            painter.setPen(QColor(PALETTE["textPrimary"]))
        name_x = x + prefix_width
        name_available = col_x - name_x - 8
        name_width = max(40, name_available)
        elided_name = painter.fontMetrics().elidedText(str(display_name), Qt.TextElideMode.ElideRight, name_width)
        painter.drawText(QRect(name_x, y, name_width, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)

        # 7. Draw launcher buttons — letters OC/FB/CL/C1/C2/CF or numbers 1-6
        LETTER_MAP = {"opencode": "OC", "freebuff": "FB", "cline": "CL", "main_codex": "C1", "main_codex2": "C2", "main_codex3_free": "CF"}
        use_letters = bool(getattr(getattr(self._config, "ui", None), "launcher_letters", True))
        painter.setFont(self.font_tiny)
        for idx, (launcher, b_rect) in enumerate(launcher_buttons):
            block_reason = (
                main_window._launcher_block_reason(launcher.id)
                if main_window is not None and hasattr(main_window, "_launcher_block_reason")
                else ""
            )
            if use_letters:
                lbl = LETTER_MAP.get(launcher.id, str(getattr(launcher, "short_label", "") or getattr(launcher, "name", "")[:2] or "?").upper()[:2])
            else:
                lbl = str(idx + 1)
            painter.fillRect(b_rect, QColor(PALETTE["surfaceRaised"]))
            painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
            painter.drawLine(b_rect.left(), b_rect.top(), b_rect.right() - 1, b_rect.top())
            painter.drawLine(b_rect.left(), b_rect.top(), b_rect.left(), b_rect.bottom() - 1)
            painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
            painter.drawLine(b_rect.left(), b_rect.bottom() - 1, b_rect.right() - 1, b_rect.bottom() - 1)
            painter.drawLine(b_rect.right() - 1, b_rect.top(), b_rect.right() - 1, b_rect.bottom() - 1)

            painter.setPen(QColor(PALETTE["textMuted"] if block_reason else PALETTE["borderGolden"]))
            painter.drawText(b_rect, Qt.AlignmentFlag.AlignCenter, lbl)
            if block_reason:
                painter.setFont(self.font_tiny)
                painter.setPen(QColor(PALETTE["dangerText"]))
                painter.drawText(
                    b_rect.adjusted(0, -2, -1, 0),
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                    "×",
                )

        # 7b. Draw [ⓘ] info button — replaces hover tooltip
        painter.fillRect(info_rect, QColor(PALETTE["surfaceRaised"]))
        painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
        painter.drawLine(info_rect.left(), info_rect.top(), info_rect.right() - 1, info_rect.top())
        painter.drawLine(info_rect.left(), info_rect.top(), info_rect.left(), info_rect.bottom() - 1)
        painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
        painter.drawLine(info_rect.left(), info_rect.bottom() - 1, info_rect.right() - 1, info_rect.bottom() - 1)
        painter.drawLine(info_rect.right() - 1, info_rect.top(), info_rect.right() - 1, info_rect.bottom() - 1)
        painter.setFont(self.font_bold)
        painter.setPen(QColor(PALETTE["borderGolden"]))
        painter.drawText(info_rect, Qt.AlignmentFlag.AlignCenter, "\u24D8")

        painter.restore()

    @staticmethod
    def build_tooltip(hover_info: dict) -> str:
        """Compact-rich tooltip — all essential info, structured but not verbose."""
        proj = hover_info.get("project")
        snap = hover_info.get("snapshot")
        arc_data = hover_info.get("archive_info")
        pack_state = hover_info.get("pack_state", "IDLE")
        pack_msg = hover_info.get("pack_message", "")
        group = hover_info.get("group", "")
        slot = hover_info.get("slot", 0)
        group_count = hover_info.get("group_count", 0)

        if not proj:
            return f"<b>[{group} #{slot}]</b> — empty slot"

        # Temperature color helper
        TC = TEMP_COLORS

        lines = [f"<b>{proj.display_name}</b>  [{group} #{slot}]  ({group_count} in group)"]

        # Source (truncated)
        if proj.source_path:
            sp = proj.source_path
            if len(sp) > 50:
                sp = "..." + sp[-47:]
            lines.append(f"<font color='#999988'>{sp}</font>")

        # Status flags
        flags = []
        if not proj.enabled:
            flags.append("DISABLED")
        if getattr(proj, "ignored", False):
            flags.append("Done")
        if getattr(proj, "ignore_archive", False):
            flags.append("Ignore to archive")
        if flags:
            lines.append(f"<font color='#FF8866'>{' / '.join(flags)}</font>")

        # Audit section — structured but compact
        if snap:
            prof = getattr(snap, "audit_profile_id", "quick3") or "quick3"
            prof_label = "A10 / Super10" if prof == "super10" else "A3 / Quick3"
            waves = getattr(snap, "completed_waves", 0)
            total = getattr(snap, "total_waves", 3)
            temp = getattr(snap, "temperature", AuditTemperature.NONE)
            temp_val = temp.value if hasattr(temp, "value") else str(temp)
            age_str = format_age_str(getattr(snap, "audit_age_seconds", None))
            tc = TC.get(temp_val, "#999999")

            # Status line with color
            if getattr(snap, "all3_ready", False) or getattr(snap, "final_handoff_ready", False):
                status_txt = "<font color='#55FF55'>✓ ALL WAVES COMPLETE</font>"
            elif waves > 0:
                status_txt = f"<font color='#FFD700'>In progress: {waves}/{total} waves</font>"
            else:
                status_txt = "<font color='#999999'>No audit data</font>"

            lines.append("")
            lines.append(f"<b>Audit</b> ({prof_label})")
            lines.append(f"  Status: {status_txt}")
            lines.append(f"  Waves: {waves}/{total}  |  <font color='{tc}'>Temp: {temp_val}</font>  |  Age: {age_str}")

            # Wave detail — compact inline
            wave_statuses = getattr(snap, "wave_statuses", None) or {}
            if wave_statuses:
                wave_names = {"core": "Core", "second": "Second", "performance": "Perf", "all": "ALL3"}
                detail_parts = []
                for wk, wv in wave_statuses.items():
                    label = wave_names.get(wk, wk)
                    if isinstance(wv, dict):
                        ws = wv.get("status", "?")
                    else:
                        ws = str(wv)
                    # Color status
                    if "COMPLETE" in ws.upper():
                        detail_parts.append(f"<font color='#55FF55'>{label}✓</font>")
                    elif "IDLE" in ws.upper():
                        detail_parts.append(f"<font color='#777766'>{label}</font>")
                    else:
                        detail_parts.append(f"<font color='#FFD700'>{label}</font>")
                if detail_parts:
                    lines.append(f"  {' | '.join(detail_parts)}")

            if getattr(snap, "campaign_run_id", None):
                run_id = snap.campaign_run_id[:16]
                lines.append(f"  <font color='#777766'>Run: {run_id}</font>")
        else:
            lines.append("")
            lines.append("<b>Audit</b>: <font color='#999999'>No data loaded</font>")

        # Archive section — compact
        arc_exists, arc_size, arc_created, arc_path = arc_data if arc_data else (False, "", "", None)
        lines.append("")
        if arc_exists:
            sync_status = hover_info.get("archive_sync_status", "SYNCED")
            sync_txt = '  <font color="#FF5555">⚠ OUTDATED</font>' if sync_status == "OUTDATED" else ""
            # Archive freshness color
            arc_temp_val = ""
            if snap:
                arc_temp = getattr(snap, "archive_temperature", None)
                if arc_temp:
                    arc_temp_val = arc_temp.value if hasattr(arc_temp, "value") else str(arc_temp)
            arc_tc = TC.get(arc_temp_val, "#999999")
            created_txt = f" created {arc_created}" if arc_created else ""
            fresh_short = hover_info.get("archive_freshness_short", "none")
            fresh_txt = {"fresh": "[✓ fresh]", "stale": "[· stale]", "old": "[! old]"}.get(fresh_short, "")
            src_newer = hover_info.get("source_older_than_archive")
            src_txt = '  <font color="#FFAA55">⏫ SOURCE CHANGED AFTER PACK</font>' if src_newer is True else ""
            lines.append(f"<b>Archive</b>: {arc_size}{created_txt} <font color='{arc_tc}'>[{arc_temp_val}]</font>{fresh_txt}{sync_txt}{src_txt}")
            if arc_path:
                ap = str(arc_path)
                if len(ap) > 60:
                    ap = "..." + ap[-57:]
                lines.append(f"  <font color='#777766'>{ap}</font>")
        else:
            lines.append("<b>Archive</b>: <font color='#999999'>Not packed yet</font>")

        dispatch = hover_info.get("dispatch") or {}
        dispatch_state = str(dispatch.get("state") or "")
        if dispatch_state and dispatch_state not in {"COMPLETE", "CANCELLED"}:
            dispatch_browser = str(dispatch.get("friendly_worker_label") or dispatch.get("browser_name") or dispatch.get("assigned_worker_id") or "")
            dispatch_error = str(dispatch.get("error") or dispatch.get("last_error_code") or "")
            dispatch_expected = str(dispatch.get("archive_filename") or "")
            dispatch_updated = str(dispatch.get("updated_at") or "")
            lines.append("")
            lines.append(f"<b>Dispatch</b>: {dispatch_state}" + (f"  <font color='#777766'>{dispatch_browser}</font>" if dispatch_browser and dispatch_state not in {"QUEUED", "RETRYABLE"} else ""))
            if dispatch_expected:
                lines.append(f"  Expected: <font color='#D4C89A'>{dispatch_expected}</font>")
            if dispatch_browser or dispatch.get("assigned_worker_id"):
                raw_wid = str(dispatch.get("assigned_worker_id") or dispatch_browser)[:32]
                lines.append(f"  Worker: {dispatch_browser or raw_wid}" + (f" <font color='#777766'>{raw_wid}</font>" if dispatch_browser and raw_wid != dispatch_browser else ""))
            if dispatch_state:
                lines.append(f"  State: {dispatch_state}")
            if dispatch_error:
                lines.append(f"  <font color='#FF8866'>Error: {dispatch_error[:120]}</font>")
            if dispatch_updated:
                try:
                    from datetime import datetime as _dt
                    upd = float(dispatch_updated)
                    when = _dt.fromtimestamp(upd).strftime("%H:%M:%S")
                except Exception:
                    when = dispatch_updated[:19]
                lines.append(f"  <font color='#777766'>Updated: {when}</font>")

        # Pack state
        if pack_state and pack_state != "IDLE":
            lines.append(f"<b>Pack</b>: <font color='#D4A840'>{pack_state}</font>")
            if pack_msg:
                lines.append(f"  <font color='#777766'>{pack_msg}</font>")

        layers = hover_info.get("inaudit_layers") or []
        sel = hover_info.get("inaudit_selected")
        if layers:
            lines.append("")
            lines.append(f"<b>INAUDIT</b>  {len(layers)} layer(s)" + (f" — selected {sel}.md" if sel else ""))
            for lay in layers[:8]:
                mark = " ◀" if lay.number == sel else ""
                empty = " — EMPTY" if lay.size_bytes == 0 else ""
                lines.append(f"  [{lay.number}] {lay.number}.md — {lay.size_str}{empty}{mark}")
            if len(layers) > 8:
                lines.append(f"  <font color='#777766'>+{len(layers)-8} more</font>")

        # Copy counter
        cc = int(getattr(proj, "audit_copy_count", 0) or 0)
        if cc > 0:
            last_at = getattr(proj, "last_copied_at", "") or ""
            try:
                from datetime import datetime

                # show short time
                dt = datetime.fromisoformat(last_at.replace("Z", "+00:00")) if last_at else None
                when = dt.strftime("%d.%m %H:%M") if dt else last_at[:16]
            except Exception:
                when = last_at[:16]
            lines.append("")
            lines.append(f"<b>Copied</b>: <font color='#D4A840'>×{cc}</font> <font color='#777766'>last {when}</font> — resets on fresh audit or manual reset")

        lines.append("")
        lines.append("<font color='#777766'>Enter: folder | Del: remove | Drag: reorder | ⓘ: info</font>")

        return "<br>".join(lines)
