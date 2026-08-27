"""Qt Delegate for Project Room Rows (Wave M).

Renders compact Golden Default Win95-style rows:
- Group headers with distinct raised bevel
- Slot badge [1..6]
- Project name + muted path
- Audit wave badge (ALL 3/3, 2/3, 0/3)
- Temperature badge (HOT, WARM, COOL, COLD, NONE)
- Pack state badge (PACKING, COMPLETE, FAILED)
- Empty slot representation
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from audapack.models import AuditTemperature
from audapack.ui_qt.theme.golden_default import PALETTE


class ProjectItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.font_main = QFont("Verdana", 9)
        self.font_main.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_bold = QFont("Verdana", 9, QFont.Weight.Bold)
        self.font_bold.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_mono = QFont("Verdana", 9)
        self.font_mono.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_small = QFont("Verdana", 8)
        self.font_small.setStyleStrategy(QFont.StyleStrategy.NoAntialias)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        node_type = index.data(Qt.ItemDataRole.UserRole + 7)  # node_type
        if node_type == "group":
            return QSize(option.rect.width(), 24)
        return QSize(option.rect.width(), 26)

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
            text_rect = rect.adjusted(8, 0, -8, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"▼  {grp_name}")
            painter.restore()
            return

        # Slot row
        slot_num = index.data(Qt.ItemDataRole.UserRole + 4) or 1
        is_empty = index.data(Qt.ItemDataRole.UserRole + 6)
        display_name = index.data(Qt.ItemDataRole.UserRole + 2) or f"Slot {slot_num}"
        source_path = index.data(Qt.ItemDataRole.UserRole + 14) or ""
        all_ready = bool(index.data(Qt.ItemDataRole.UserRole + 10))
        completed_waves = index.data(Qt.ItemDataRole.UserRole + 13) or 0
        temperature = index.data(Qt.ItemDataRole.UserRole + 8) or AuditTemperature.NONE
        pack_state = index.data(Qt.ItemDataRole.UserRole + 11) or "IDLE"

        # Row background
        if is_selected:
            bg = QColor(PALETTE["selection"])
        else:
            bg = QColor(PALETTE["surface"])
        painter.fillRect(rect, bg)

        # Bottom separator
        painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
        painter.drawLine(rect.left(), rect.bottom() - 1, rect.right() - 1, rect.bottom() - 1)

        x = rect.left() + 6
        y = rect.top()
        h = rect.height()

        # 1. Slot badge [1..6]
        slot_badge = f"[{slot_num}]"
        painter.setFont(self.font_mono)
        painter.setPen(QColor(PALETTE["textSecondary"]))
        painter.drawText(QRect(x, y, 28, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, slot_badge)
        x += 32

        if is_empty:
            painter.setFont(self.font_small)
            painter.setPen(QColor(PALETTE["textMuted"]))
            painter.drawText(QRect(x, y, 200, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "[ EMPTY SLOT — DROP HERE ]")
            painter.restore()
            return

        # 2. Project Name
        painter.setFont(self.font_bold)
        painter.setPen(QColor(PALETTE["textPrimary"]))
        avail_width = rect.width() - 40
        if avail_width < 180:
            name_width = max(60, avail_width - 80)
        else:
            name_width = min(220, max(80, int(avail_width * 0.35)))
        elided_name = painter.fontMetrics().elidedText(str(display_name), Qt.TextElideMode.ElideRight, name_width)
        painter.drawText(QRect(x, y, name_width, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)
        x += name_width + 6

        total_waves = index.data(Qt.ItemDataRole.UserRole + 15) or 3
        prof_label = index.data(Qt.ItemDataRole.UserRole + 19) or ("A10" if total_waves == 10 else "A3")

        # 3. Dual Profile Wave badge (✓ A3 3/3, A10 7/10, etc.)
        if all_ready:
            wave_text = f"✓ {prof_label} {total_waves}/{total_waves}"
            wave_color = QColor(PALETTE["success"])
        elif completed_waves > 0:
            wave_text = f"{prof_label} {completed_waves}/{total_waves}"
            wave_color = QColor(PALETTE["warning"])
        else:
            wave_text = f"{prof_label} 0/{total_waves}"
            wave_color = QColor(PALETTE["textMuted"])

        painter.setFont(self.font_bold if all_ready else self.font_small)
        painter.setPen(wave_color)
        wave_w = max(64, len(wave_text) * 7 + 4)
        if x + wave_w <= rect.right() - 4:
            painter.drawText(QRect(x, y, wave_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, wave_text)
            x += wave_w + 4

        # 4. Audit Freshness Badge (Graduated temperature coloring in Golden Default tones)
        audit_age_str = index.data(Qt.ItemDataRole.UserRole + 17) or ""
        temp_val = temperature.value if hasattr(temperature, "value") else str(temperature)
        if audit_age_str:
            temp_badge = f"[{audit_age_str}]"
            if temp_val == "HOT":
                temp_color = QColor(PALETTE["borderHighlight"])
            elif temp_val == "WARM":
                temp_color = QColor(PALETTE["borderGolden"])
            elif temp_val == "COOL":
                temp_color = QColor(PALETTE["textSecondary"])
            elif temp_val == "COLD":
                temp_color = QColor(PALETTE["textMuted"])
            else:
                temp_color = QColor(PALETTE["borderMuted"])
        else:
            temp_badge = "[-]"
            temp_color = QColor(PALETTE["borderMuted"])

        painter.setFont(self.font_small)
        painter.setPen(temp_color)
        temp_w = max(34, len(temp_badge) * 7 + 4)
        if x + temp_w <= rect.right() - 4:
            painter.drawText(QRect(x, y, temp_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, temp_badge)
            x += temp_w + 4

        # 5. Archive Freshness, Size & Sync Badge (ZIP: size age [NEEDS PACK])
        arc_data = index.data(Qt.ItemDataRole.UserRole + 18)
        sync_status = index.data(Qt.ItemDataRole.UserRole + 20) or "SYNCED"
        arc_exists, arc_size, arc_age, _arc_path = arc_data if arc_data else (False, "", "", None)
        if arc_exists:
            if sync_status == "OUTDATED":
                arc_badge = f"ZIP: {arc_size} ({arc_age}) [NEEDS PACK]"
                arc_color = QColor(PALETTE["dangerText"])
            else:
                arc_badge = f"ZIP: {arc_size} ({arc_age})"
                arc_color = QColor(PALETTE["textSecondary"])
        else:
            arc_badge = "ZIP: NONE"
            arc_color = QColor(PALETTE["textMuted"])

        painter.setFont(self.font_small)
        painter.setPen(arc_color)
        arc_w = max(70, len(arc_badge) * 7 + 6)
        if x + arc_w <= rect.right() - 4:
            painter.drawText(QRect(x, y, arc_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, arc_badge)
            x += arc_w + 4

        # 6. Pack state (if active or just completed)
        if pack_state and pack_state != "IDLE":
            if pack_state in ("PACKING", "QUEUED"):
                p_color = QColor(PALETTE["borderGolden"])
            elif pack_state == "COMPLETE":
                p_color = QColor(PALETTE["success"])
            else:
                p_color = QColor(PALETTE["danger"])
            painter.setFont(self.font_bold)
            painter.setPen(p_color)
            p_w = 70
            if x + p_w <= rect.right() - 4:
                painter.drawText(QRect(x, y, p_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"[{pack_state}]")
                x += p_w + 4

        # 7. Inline [GG] copy button on right edge
        btn_rect = QRect(rect.right() - 36, rect.top() + 3, 32, 20)
        painter.fillRect(btn_rect, QColor(PALETTE["surfaceRaised"]))
        painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
        painter.drawLine(btn_rect.left(), btn_rect.top(), btn_rect.right() - 1, btn_rect.top())
        painter.drawLine(btn_rect.left(), btn_rect.top(), btn_rect.left(), btn_rect.bottom() - 1)
        painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
        painter.drawLine(btn_rect.left(), btn_rect.bottom() - 1, btn_rect.right() - 1, btn_rect.bottom() - 1)
        painter.drawLine(btn_rect.right() - 1, btn_rect.top(), btn_rect.right() - 1, btn_rect.bottom() - 1)

        painter.setFont(self.font_bold)
        painter.setPen(QColor(PALETTE["borderGolden"]))
        painter.drawText(btn_rect, Qt.AlignmentFlag.AlignCenter, "GG")

        # 8. Source path (truncated between pack state and [GG] button)
        rem_width = rect.right() - x - 42
        if rem_width > 40 and source_path:
            painter.setFont(self.font_small)
            painter.setPen(QColor(PALETTE["textMuted"]))
            elided_path = painter.fontMetrics().elidedText(str(source_path), Qt.TextElideMode.ElideMiddle, rem_width)
            painter.drawText(QRect(x, y, rem_width, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_path)

        painter.restore()
