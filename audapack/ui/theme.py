"""Golden Default theme tokens, typography, and styling helpers for AUDAPACK."""

from __future__ import annotations

PALETTE = {
    "background": "#1A1810",
    "backgroundSoft": "#232018",
    "surface": "#332E22",
    "surfaceRaised": "#3D372A",
    "surfaceAlt": "#453D30",
    "borderDark": "#100E08",
    "borderHighlight": "#F0D060",
    "bevelLight": "#75663D",
    "borderMuted": "#5A5040",
    "textPrimary": "#D4C89A",
    "textSecondary": "#9C9371",
    "textMuted": "#6E674E",
    "accentTeal": "#008080",
    "accentTealDeep": "#004C4C",
    "success": "#4A7A20",
    "successFg": "#A6C987",
    "warning": "#7A7A20",
    "warningFg": "#D4C275",
    "danger": "#7A2020",
    "dangerText": "#D66464",
    "selection": "#3D372A",
    "compareBack": "#14120C",
    "link": "#F0D060",
    # Muted semantic badges (non-acidic vintage dark golden tones)
    "copied": "#7A9E58",
    "newBadge": "#8C6D28",
    "hot": "#4A2424",
    "hotBg": "#4A2424",
    "hotFg": "#D49090",
    "warm": "#473A1D",
    "warmBg": "#473A1D",
    "warmFg": "#D4B875",
    "cool": "#253747",
    "coolBg": "#253747",
    "coolFg": "#8BB4D4",
    "cold": "#2C3036",
    "coldBg": "#2C3036",
    "coldFg": "#A0A8B0",
    "stale": "#29251E",
    "staleBg": "#29251E",
    "staleFg": "#7D7565",
}

SPACING = {
    "hair": 1,
    "control": 2,
    "group": 4,
    "section": 8,
    "outer": 12,
    "outerWide": 16,
}

FONT_FAMILY = "Verdana"
FONT_SIZE_TITLE = 11
FONT_SIZE_BODY = 9
FONT_SIZE_SMALL = 8
FONT_SIZE_TINY = 7


def make_vintage_btn(parent, text: str, command=None, bg=None, fg=None, font_size=8, bold=False, **kwargs):
    import tkinter as tk
    b_bg = bg or PALETTE["surfaceRaised"]
    b_fg = fg or PALETTE["textPrimary"]
    f = (FONT_FAMILY, font_size, "bold" if bold else "normal")
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=b_bg,
        fg=b_fg,
        activebackground=PALETTE["surfaceAlt"],
        activeforeground=PALETTE["borderHighlight"],
        font=f,
        relief="raised",
        bd=2,
        highlightthickness=0,
        **kwargs,
    )

