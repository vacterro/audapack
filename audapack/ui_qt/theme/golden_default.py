"""Golden Default theme tokens for Qt — one authoritative source matching UI.md."""

from __future__ import annotations

from typing import Dict


class GoldenDefault:
    # 21 exact Golden Default tokens from UI.md (Wintage goldendefault.json)
    background = "#1A1810"
    backgroundSoft = "#232018"
    surface = "#332E22"
    surfaceRaised = "#3D372A"
    surfaceAlt = "#453D30"

    borderDark = "#100E08"
    borderHighlight = "#F0D060"
    bevelLight = "#75663D"
    borderMuted = "#5A5040"

    textPrimary = "#D4C89A"
    textSecondary = "#9C9371"
    textMuted = "#6E674E"

    accentTeal = "#008080"
    accentTealDeep = "#004C4C"

    success = "#4A7A20"
    warning = "#7A7A20"
    danger = "#7A2020"
    dangerText = "#D66464"

    selection = "#3D372A"
    compareBack = "#14120C"
    link = "#F0D060"

    # Aliases
    borderGolden = borderHighlight
    accent = borderHighlight
    copied = success

    FONT_FAMILY = "Verdana"

    @classmethod
    def qss(cls) -> str:
        return f"""
QMainWindow, QDialog, QWidget {{
    background: {cls.background};
    color: {cls.textPrimary};
    font-family: "{cls.FONT_FAMILY}";
    font-size: 11px;
}}
QToolBar {{
    background: {cls.surfaceRaised};
    border-bottom: 2px solid {cls.borderDark};
    spacing: 2px;
    padding: 2px;
}}
QToolBarExtension {{
    background: {cls.surfaceRaised};
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
    padding: 1px;
}}
QPushButton, QToolButton {{
    background: {cls.surfaceRaised};
    color: {cls.textPrimary};
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
    padding: 2px 6px;
    font-family: "{cls.FONT_FAMILY}";
    font-size: 11px;
    font-weight: bold;
    outline: none;
}}
QPushButton:hover, QToolButton:hover {{
    background: {cls.surfaceAlt};
}}
QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked {{
    background: {cls.surface};
    border-top: 2px solid {cls.borderDark};
    border-left: 2px solid {cls.borderDark};
    border-right: 2px solid {cls.bevelLight};
    border-bottom: 2px solid {cls.bevelLight};
    padding: 3px 5px 1px 7px;
}}
QPushButton:disabled, QToolButton:disabled {{
    background: {cls.surfaceRaised};
    color: {cls.textMuted};
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
}}
QTreeView {{
    background: {cls.surface};
    color: {cls.textPrimary};
    border-top: 2px solid {cls.borderDark};
    border-left: 2px solid {cls.borderDark};
    border-right: 2px solid {cls.bevelLight};
    border-bottom: 2px solid {cls.bevelLight};
    selection-background-color: {cls.selection};
    selection-color: {cls.borderHighlight};
    outline: none;
    font-family: "{cls.FONT_FAMILY}";
}}
QTreeView::item {{
    height: 28px;
    border: none;
}}
QTreeView::item:selected {{
    background: {cls.selection};
    color: {cls.borderHighlight};
}}
QHeaderView::section {{
    background: {cls.surfaceRaised};
    color: {cls.textPrimary};
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
    padding: 3px 6px;
    font-family: "{cls.FONT_FAMILY}";
    font-size: 11px;
    font-weight: bold;
}}
QLineEdit, QSpinBox, QComboBox {{
    background: {cls.compareBack};
    color: {cls.textPrimary};
    border-top: 2px solid {cls.borderDark};
    border-left: 2px solid {cls.borderDark};
    border-right: 2px solid {cls.bevelLight};
    border-bottom: 2px solid {cls.bevelLight};
    padding: 2px 4px;
    font-family: "{cls.FONT_FAMILY}";
    font-size: 11px;
    selection-background-color: {cls.selection};
    selection-color: {cls.textPrimary};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 18px;
    background: {cls.surfaceRaised};
    border-left: 1px solid {cls.borderDark};
    border-top: 2px solid {cls.bevelLight};
    border-bottom: 2px solid {cls.borderDark};
    border-right: 2px solid {cls.borderDark};
}}
QComboBox QAbstractItemView {{
    background: {cls.surface};
    color: {cls.textPrimary};
    border: 2px solid {cls.borderDark};
    selection-background-color: {cls.selection};
    selection-color: {cls.borderHighlight};
}}
QTabWidget::pane {{
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
    background: {cls.surfaceRaised};
    top: -1px;
}}
QTabBar::tab {{
    background: {cls.surfaceRaised};
    color: {cls.textSecondary};
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: none;
    padding: 4px 10px;
    margin-right: 2px;
    font-family: "{cls.FONT_FAMILY}";
    font-size: 11px;
}}
QTabBar::tab:selected {{
    background: {cls.surface};
    color: {cls.textPrimary};
    font-weight: bold;
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.surface};
    margin-bottom: -2px;
}}
QTabBar::tab:hover:!selected {{
    background: {cls.surfaceAlt};
    color: {cls.textPrimary};
}}
QStatusBar {{
    background: {cls.surfaceRaised};
    color: {cls.textSecondary};
    border-top: 2px solid {cls.borderDark};
    font-family: "{cls.FONT_FAMILY}";
    font-size: 11px;
}}
QStatusBar::item {{
    border: none;
}}
QScrollBar:vertical {{
    background: {cls.backgroundSoft};
    width: 16px;
    border-left: 1px solid {cls.borderDark};
    margin: 16px 0 16px 0;
}}
QScrollBar::handle:vertical {{
    background: {cls.surfaceRaised};
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
    min-height: 20px;
}}
QScrollBar::add-line:vertical {{
    background: {cls.surfaceRaised};
    height: 16px;
    subcontrol-position: bottom;
    subcontrol-origin: margin;
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
}}
QScrollBar::sub-line:vertical {{
    background: {cls.surfaceRaised};
    height: 16px;
    subcontrol-position: top;
    subcontrol-origin: margin;
    border-top: 2px solid {cls.bevelLight};
    border-left: 2px solid {cls.bevelLight};
    border-right: 2px solid {cls.borderDark};
    border-bottom: 2px solid {cls.borderDark};
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: {cls.backgroundSoft};
}}
"""

    @classmethod
    def tokens(cls) -> Dict[str, str]:
        return PALETTE


PALETTE: dict[str, str] = {
    "background": GoldenDefault.background,
    "backgroundSoft": GoldenDefault.backgroundSoft,
    "surface": GoldenDefault.surface,
    "surfaceRaised": GoldenDefault.surfaceRaised,
    "surfaceAlt": GoldenDefault.surfaceAlt,
    "borderDark": GoldenDefault.borderDark,
    "borderGolden": GoldenDefault.borderHighlight,
    "borderHighlight": GoldenDefault.borderHighlight,
    "bevelLight": GoldenDefault.bevelLight,
    "borderMuted": GoldenDefault.borderMuted,
    "borderLight": GoldenDefault.bevelLight,
    "textPrimary": GoldenDefault.textPrimary,
    "textSecondary": GoldenDefault.textSecondary,
    "textMuted": GoldenDefault.textMuted,
    "accentTeal": GoldenDefault.accentTeal,
    "accentTealDeep": GoldenDefault.accentTealDeep,
    "success": GoldenDefault.success,
    "warning": GoldenDefault.warning,
    "danger": GoldenDefault.danger,
    "dangerText": GoldenDefault.dangerText,
    "error": GoldenDefault.danger,
    "accent": GoldenDefault.accent,
    "hot": GoldenDefault.dangerText,
    "info": GoldenDefault.borderGolden,
    "copied": GoldenDefault.copied,
    "selection": GoldenDefault.selection,
    "compareBack": GoldenDefault.compareBack,
    "link": GoldenDefault.link,
}
