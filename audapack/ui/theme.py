"""Golden Default theme tokens, typography, and styling helpers for AUDAPACK."""

from __future__ import annotations

PALETTE = {
    "background": "#1A1810",
    "backgroundSoft": "#232018",
    "surface": "#332E22",
    "surfaceRaised": "#3D372A",
    "surfaceAlt": "#453D30",
    "borderDark": "#100E08",
    "borderHighlight": "#D4B86A",
    "bevelLight": "#6B5E38",
    "borderMuted": "#4F4738",
    "textPrimary": "#D4C89A",
    "textSecondary": "#9C9371",
    "textMuted": "#6E674E",
    "accentTeal": "#2A5959",
    "accentTealDeep": "#1A3A3A",
    "success": "#2D4518",
    "successFg": "#A6C987",
    "warning": "#4A421A",
    "warningFg": "#D4C275",
    "danger": "#4D2222",
    "dangerText": "#BA6363",
    "selection": "#3D372A",
    "compareBack": "#14120C",
    "link": "#D4B86A",
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

