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
        self.font_bold = QFont("Verdana", 9, QFont.Weight.Bold)
        self.font_mono = QFont("Lucida Console", 8)
        self.font_small = QFont("Verdana", 8)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        node_type = index.data(Qt.ItemDataRole.UserRole + 7)  # node_type
        if node_type == "group":
            return QSize(option.rect.width(), 26)
        return QSize(option.rect.width(), 28)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = option.rect

        node_type = index.data(Qt.ItemDataRole.UserRole + 7)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if node_type == "group":
            # Group header bar
            grp_name = index.data(Qt.ItemDataRole.DisplayRole) or ""
            bg = QColor(PALETTE["surfaceRaised"])
            painter.fillRect(rect, bg)

            # Golden border
            painter.setPen(QPen(QColor(PALETTE["borderGolden"]), 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))

            painter.setFont(self.font_bold)
            painter.setPen(QColor(PALETTE["textPrimary"]))
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
            bg = QColor(PALETTE["surfaceAlt"])
        else:
            bg = QColor(PALETTE["surface"])
        painter.fillRect(rect, bg)

        # Bottom separator
        painter.setPen(QPen(QColor(PALETTE["borderLight"]), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        x = rect.left() + 6
        y = rect.top()
        h = rect.height()

        # 1. Slot badge [1..6]
        slot_badge = f"[{slot_num}]"
        painter.setFont(self.font_mono)
        painter.setPen(QColor(PALETTE["borderGolden"]))
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
        name_width = min(220, int(option.rect.width() * 0.35))
        painter.drawText(QRect(x, y, name_width, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, str(display_name))
        x += name_width + 8

        # 3. Wave badge (3/3 ALL, n/3, 0/3)
        if all_ready:
            wave_text = "✓ ALL 3/3"
            wave_color = QColor(PALETTE["success"])
        elif completed_waves > 0:
            wave_text = f"{completed_waves}/3"
            wave_color = QColor(PALETTE["warning"])
        else:
            wave_text = "0/3"
            wave_color = QColor(PALETTE["textMuted"])

        painter.setFont(self.font_bold if all_ready else self.font_small)
        painter.setPen(wave_color)
        painter.drawText(QRect(x, y, 70, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, wave_text)
        x += 76

        # 4. Temperature Badge
        temp_val = temperature.value if hasattr(temperature, "value") else str(temperature)
        if temp_val == "HOT":
            temp_color = QColor(PALETTE["hot"])
        elif temp_val == "WARM":
            temp_color = QColor(PALETTE["warning"])
        elif temp_val in ("COOL", "COLD"):
            temp_color = QColor(PALETTE["info"])
        else:
            temp_color = QColor(PALETTE["textMuted"])

        painter.setFont(self.font_small)
        painter.setPen(temp_color)
        painter.drawText(QRect(x, y, 55, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"[{temp_val}]")
        x += 60

        # 5. Pack state (if active or just completed)
        if pack_state and pack_state != "IDLE":
            if pack_state in ("PACKING", "QUEUED"):
                p_color = QColor(PALETTE["accent"])
            elif pack_state == "COMPLETE":
                p_color = QColor(PALETTE["success"])
            else:
                p_color = QColor(PALETTE["error"])
            painter.setFont(self.font_bold)
            painter.setPen(p_color)
            painter.drawText(QRect(x, y, 80, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"[{pack_state}]")
            x += 86

        # 6. Source path (truncated to right edge)
        rem_width = rect.right() - x - 8
        if rem_width > 40 and source_path:
            painter.setFont(self.font_small)
            painter.setPen(QColor(PALETTE["textMuted"]))
            painter.drawText(QRect(x, y, rem_width, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, str(source_path))

        painter.restore()
