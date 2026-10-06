"""Real Windows console style verification probe for T-244 / SRC-091."""

import subprocess
import sys
from pathlib import Path

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from audapack.cli_launchers import codex_console_command
from audapack.config import LauncherConfig
from audapack.console_style import ConsoleStyle, apply_console_style, resolve_console_style


def test_real_powershell_execution():
    """Verify that generated PowerShell preludes genuinely set RawUI colors in powershell.exe."""
    print("Testing real PowerShell process execution of console styles...")

    test_cases = [
        ("opencode", "opencode", "Black", "White"),
        ("cline", "cline", "Black", "White"),
        ("custom_blue_yellow", LauncherConfig(id="custom", name="Custom", short_label="CU", command_template="", console_background_color="DarkBlue", console_foreground_color="Yellow"), "DarkBlue", "Yellow"),
        ("main_codex", "main_codex", "", ""),
        ("freebuff", "freebuff", "", ""),
    ]

    for name, target, expected_bg, expected_fg in test_cases:
        style = resolve_console_style(target)
        if expected_bg and expected_fg:
            assert not style.is_empty, f"{name} should have style"
            assert style.background_color == expected_bg
            assert style.foreground_color == expected_fg

            # Form PowerShell script that executes prelude, then queries RawUI
            prelude = style.powershell_prelude()
            ps_script = (
                f"{prelude}; "
                f"$bg = [string]$Host.UI.RawUI.BackgroundColor; "
                f"$fg = [string]$Host.UI.RawUI.ForegroundColor; "
                f"Write-Output \"RAWUI_RESULT:$bg/$fg\""
            )

            res = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = res.stdout
            expected_out = f"RAWUI_RESULT:{expected_bg}/{expected_fg}"
            assert expected_out in output, f"{name}: expected '{expected_out}' in output, got: '{output}'"
            print(f"  [PASS] {name} -> RawUI Background={expected_bg}, Foreground={expected_fg}")
        else:
            assert style.is_empty, f"{name} should not have style"
            prelude = style.powershell_prelude()
            assert prelude == ""
            print(f"  [PASS] {name} -> Default unstyled (no prelude inserted)")


def test_codex_console_command_integration():
    """Verify codex_console_command preserves CODEX_HOME and injects prelude when styled."""
    styled_cfg = LauncherConfig(
        id="main_codex",
        name="Codex",
        short_label="C1",
        command_template="codex",
        console_background_color="DarkMagenta",
        console_foreground_color="Cyan",
    )
    cmd = codex_console_command(
        account="main",
        project_root=r"V:\test\proj",
        title="Proj | Codex 1",
        launcher=styled_cfg,
    )
    assert "$Host.UI.RawUI.BackgroundColor = 'DarkMagenta'" in cmd
    assert "$Host.UI.RawUI.ForegroundColor = 'Cyan'" in cmd
    assert "$env:CODEX_HOME" in cmd
    assert "[Console]::Title = 'Proj | Codex 1'" in cmd
    # Assert order: prelude -> title -> CODEX_HOME -> command
    idx_style = cmd.index("BackgroundColor = 'DarkMagenta'")
    idx_title = cmd.index("[Console]::Title")
    idx_home = cmd.index("$env:CODEX_HOME")
    assert idx_style < idx_title < idx_home
    print("  [PASS] codex_console_command preserves order and CODEX_HOME isolation")


def test_apply_console_style_idempotency():
    """Verify applying console style twice does not duplicate the prelude."""
    style = ConsoleStyle(background_color="Black", foreground_color="White")
    original = "[Console]::Title = 'Hello'; node app.js"
    once = apply_console_style(original, style)
    twice = apply_console_style(once, style)
    assert once == twice
    assert once.count("RawUI.BackgroundColor") == 1
    print("  [PASS] apply_console_style is idempotent")


def main():
    print("=== T-244 Real Windows Acceptance Probe ===")
    test_real_powershell_execution()
    test_codex_console_command_integration()
    test_apply_console_style_idempotency()
    print("=== All Acceptance Checks Passed Successfully! ===")


if __name__ == "__main__":
    main()
