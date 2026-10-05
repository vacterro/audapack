"""Authoritative console styling for agent launcher terminals (T-244 / SRC-091).

Provides one shared console-identity style authority for all direct AUDAPACK
console launches. Restores established per-launcher console colors (e.g.
OpenCode / Cline Black/White from AI_AGENT_LAUNCHER.PS1) without duplicating
snippets across launch paths or breaking managed OpenCode architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class ConsoleStyle:
    """Console color identity for a launcher terminal."""

    background_color: str = ""
    foreground_color: str = ""

    @property
    def is_empty(self) -> bool:
        return not bool(self.background_color) and not bool(self.foreground_color)

    def powershell_prelude(self) -> str:
        """Returns the PowerShell statement to establish this console style.

        Applies before the agent TUI starts, changing RawUI default colors
        and clearing the screen buffer so the full-screen terminal inherits
        the intended identity.
        """
        if self.is_empty:
            return ""
        parts: list[str] = []
        if self.background_color:
            bg = str(self.background_color).replace("'", "''")
            parts.append(f"$Host.UI.RawUI.BackgroundColor = '{bg}'")
        if self.foreground_color:
            fg = str(self.foreground_color).replace("'", "''")
            parts.append(f"$Host.UI.RawUI.ForegroundColor = '{fg}'")
        if self.background_color:
            parts.append("Clear-Host")
        body = "; ".join(parts)
        return f"try {{ {body} }} catch {{}};"


#: Established legacy launcher color identities recovered from local sources of truth
#: (specifically AI_AGENT_LAUNCHER.PS1 lines 120-121).
#: Launchers without an established custom style intentionally have no default entry.
ESTABLISHED_LAUNCHER_STYLES: dict[str, ConsoleStyle] = {
    "opencode": ConsoleStyle(background_color="Black", foreground_color="White"),
    "cline": ConsoleStyle(background_color="Black", foreground_color="White"),
}


def resolve_console_style(
    launcher: Any,
    *,
    custom_styles: Optional[Mapping[str, ConsoleStyle]] = None,
) -> ConsoleStyle:
    """Resolves the authoritative console style for a launcher or launcher id.

    Resolution order:
    1. Explicit configured colors on launcher object (if any)
    2. Optional custom_styles mapping override
    3. Established legacy style for known launcher_id
    4. Safe fallback: no style (empty ConsoleStyle)
    """
    if isinstance(launcher, ConsoleStyle):
        return launcher

    launcher_id = getattr(launcher, "id", None)
    if launcher_id is None:
        launcher_id = str(launcher or "").strip()
    else:
        launcher_id = str(launcher_id or "").strip()

    # 1. Configured colors on launcher config object
    bg = str(getattr(launcher, "console_background_color", "") or "").strip()
    fg = str(getattr(launcher, "console_foreground_color", "") or "").strip()
    if bg or fg:
        bg_val = "" if bg.lower() in ("none", "default") else bg
        fg_val = "" if fg.lower() in ("none", "default") else fg
        return ConsoleStyle(background_color=bg_val, foreground_color=fg_val)

    # 2. Custom styles override mapping
    if custom_styles and launcher_id in custom_styles:
        return custom_styles[launcher_id]

    # 3. Established legacy style
    if launcher_id in ESTABLISHED_LAUNCHER_STYLES:
        return ESTABLISHED_LAUNCHER_STYLES[launcher_id]

    # 4. Fallback: no custom style
    return ConsoleStyle()


def apply_console_style(command: str, launcher: Any) -> str:
    """Prepend console style prelude to a PowerShell command string if applicable and not already present."""
    style = launcher if isinstance(launcher, ConsoleStyle) else resolve_console_style(launcher)
    prelude = style.powershell_prelude()
    if not prelude:
        return command

    # Avoid duplicate injection
    normalized_cmd = command or ""
    if "$Host.UI.RawUI.BackgroundColor" in normalized_cmd or "$Host.UI.RawUI.ForegroundColor" in normalized_cmd:
        return normalized_cmd

    if not normalized_cmd:
        return prelude
    return f"{prelude} {normalized_cmd}".strip()
