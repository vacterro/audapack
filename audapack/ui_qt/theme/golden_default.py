"""Golden Default theme tokens for Qt — one authoritative source, no QSS sprinkling."""

from __future__ import annotations

from typing import Dict


class GoldenDefault:
    background = "#0F0E0C"
    backgroundSoft = "#1A1815"
    surface = "#221F1A"
    surfaceRaised = "#2B2822"
    surfaceAlt = "#332F27"
    borderDark = "#000000"
    borderHighlight = "#B8A34A"
    textPrimary = "#E8E4DA"
    textSecondary = "#B8B2A4"
    textMuted = "#7A7468"
    success = "#4CAF50"
    warning = "#E0B44E"
    danger = "#C0504D"
    accent = "#B8A34A"
    copied = "#6FC36F"

    FONT_FAMILY = "Verdana"

    @classmethod
    def qss(cls) -> str:
        return f"""
QMainWindow, QDialog {{ background: {cls.background}; color: {cls.textPrimary}; font-family: "{cls.FONT_FAMILY}"; }}
QTreeView {{
    background: {cls.surface}; color: {cls.textPrimary}; border: 1px solid {cls.borderDark};
    alternate-background-color: {cls.surfaceAlt}; selection-background-color: {cls.surfaceRaised};
    selection-color: {cls.borderHighlight}; outline: none;
}}
QTreeView::item {{ height: 30px; border: 0; }}
QHeaderView::section {{
    background: {cls.surfaceAlt}; color: {cls.textMuted}; border: 1px solid {cls.borderDark};
    padding: 4px; font-family: "{cls.FONT_FAMILY}"; font-size: 8pt;
}}
QPushButton {{
    background: {cls.surface}; color: {cls.textPrimary}; border: 2px solid {cls.borderDark};
    padding: 4px 8px; font-family: "{cls.FONT_FAMILY}";
}}
QPushButton:hover {{ background: {cls.surfaceRaised}; }}
QPushButton:pressed {{ background: {cls.surfaceAlt}; }}
QStatusBar {{ background: {cls.surfaceRaised}; color: {cls.textSecondary}; border-top: 1px solid {cls.borderDark}; }}
QToolBar {{ background: {cls.surfaceRaised}; border-bottom: 2px solid {cls.borderDark}; spacing: 4px; padding: 4px; }}
QScrollBar:vertical {{ background: {cls.backgroundSoft}; width: 16px; }}
QScrollBar::handle:vertical {{ background: {cls.surfaceAlt}; border: 1px solid {cls.borderDark}; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
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
    "borderLight": GoldenDefault.surfaceAlt,
    "textPrimary": GoldenDefault.textPrimary,
    "textSecondary": GoldenDefault.textSecondary,
    "textMuted": GoldenDefault.textMuted,
    "success": GoldenDefault.success,
    "warning": GoldenDefault.warning,
    "error": GoldenDefault.danger,
    "danger": GoldenDefault.danger,
    "accent": GoldenDefault.accent,
    "hot": "#E06666",
    "info": "#6D9EEB",
    "copied": GoldenDefault.copied,
}
