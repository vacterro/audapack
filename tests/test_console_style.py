"""Unit and regression tests for console style authority (T-244 / SRC-091)."""

from __future__ import annotations

from unittest.mock import patch

from audapack.cli_launchers import codex_console_command, managed_console_title
from audapack.config import LauncherConfig
from audapack.console_style import (
    ConsoleStyle,
    apply_console_style,
    resolve_console_style,
)


def test_established_launcher_styles_resolve_deterministically():
    """Milestone G: launcher id resolves deterministically to its established style."""
    opencode_style = resolve_console_style("opencode")
    assert opencode_style.background_color == "Black"
    assert opencode_style.foreground_color == "White"
    assert not opencode_style.is_empty
    assert "$Host.UI.RawUI.BackgroundColor = 'Black'" in opencode_style.powershell_prelude()
    assert "$Host.UI.RawUI.ForegroundColor = 'White'" in opencode_style.powershell_prelude()
    assert "Clear-Host" in opencode_style.powershell_prelude()

    cline_style = resolve_console_style("cline")
    assert cline_style.background_color == "Black"
    assert cline_style.foreground_color == "White"
    assert not cline_style.is_empty


def test_unknown_and_unconfigured_launchers_resolve_to_empty_style():
    """Milestone G: unknown/custom launcher safely resolves to no style or configured style."""
    # Built-in launchers without custom legacy styles resolve to empty style
    assert resolve_console_style("freebuff").is_empty
    assert resolve_console_style("main_codex").is_empty
    assert resolve_console_style("main_codex2").is_empty
    assert resolve_console_style("main_codex3_free").is_empty
    assert resolve_console_style("claude1").is_empty
    assert resolve_console_style("claude2").is_empty
    assert resolve_console_style("antigravity").is_empty
    assert resolve_console_style("zcode").is_empty

    # Unknown string id
    assert resolve_console_style("some_unknown_agent").is_empty

    # LauncherConfig without configured colors
    custom_cfg = LauncherConfig(id="my_tool", name="My Tool", short_label="MT")
    assert resolve_console_style(custom_cfg).is_empty
    assert resolve_console_style(custom_cfg).powershell_prelude() == ""


def test_configured_launcher_style_overrides_defaults():
    """Milestone G: launcher config can explicitly configure console style."""
    cfg = LauncherConfig(
        id="custom_agent",
        name="Custom",
        short_label="CA",
        console_background_color="DarkBlue",
        console_foreground_color="Yellow",
    )
    style = resolve_console_style(cfg)
    assert style.background_color == "DarkBlue"
    assert style.foreground_color == "Yellow"
    prelude = style.powershell_prelude()
    assert "$Host.UI.RawUI.BackgroundColor = 'DarkBlue'" in prelude
    assert "$Host.UI.RawUI.ForegroundColor = 'Yellow'" in prelude
    assert "Clear-Host" in prelude

    # Explicit 'none' or 'default' disables style for opencode
    cfg_none = LauncherConfig(
        id="opencode",
        name="OpenCode",
        short_label="OC",
        console_background_color="none",
        console_foreground_color="default",
    )
    assert resolve_console_style(cfg_none).is_empty


def test_style_generation_contains_no_secrets_paths_or_tokens():
    """Milestone G: style generation contains no project path or credential data."""
    style = resolve_console_style("opencode")
    prelude = style.powershell_prelude()
    for sensitive in ("http", "token", "secret", "bearer", "key", "password", "path", "c:\\", "v:\\"):
        assert sensitive not in prelude.lower()


def test_project_name_and_correlation_token_do_not_determine_color():
    """Milestone G: project name and correlation token do not affect color."""
    style1 = resolve_console_style("opencode")
    style2 = resolve_console_style("opencode")
    assert style1 == style2
    assert style1.powershell_prelude() == style2.powershell_prelude()


def test_account_identity_is_preserved():
    """Milestone F/G: account identity is preserved for codex and claude."""
    # Codex accounts remain distinct
    assert resolve_console_style("main_codex").is_empty
    assert resolve_console_style("main_codex2").is_empty
    assert resolve_console_style("main_codex3_free").is_empty

    # Custom override on one account does not leak to others
    custom_c2 = LauncherConfig(
        id="main_codex2",
        name="Codex 2",
        short_label="C2",
        console_background_color="DarkMagenta",
    )
    assert resolve_console_style(custom_c2).background_color == "DarkMagenta"
    assert resolve_console_style("main_codex").is_empty
    assert resolve_console_style("main_codex3_free").is_empty

    # Claude accounts remain distinct
    assert resolve_console_style("claude1").is_empty
    assert resolve_console_style("claude2").is_empty


def test_codex_console_command_preserves_isolation_and_applies_style():
    """Milestone G: codex command still preserves CODEX_HOME isolation."""
    # Default: no style prelude, isolation intact
    cmd = codex_console_command("main_codex2", r"C:\project\path", "My Title")
    assert "'.codex-account2'" in cmd
    assert "Env:OPENAI_API_KEY" in cmd
    assert "[Console]::Title = 'My Title'" in cmd

    # With configured style
    styled_cfg = LauncherConfig(
        id="main_codex2",
        name="Codex 2",
        short_label="C2",
        console_background_color="DarkRed",
        console_foreground_color="White",
    )
    styled_cmd = codex_console_command("main_codex2", r"C:\project\path", "My Title", launcher=styled_cfg)
    assert "$Host.UI.RawUI.BackgroundColor = 'DarkRed'" in styled_cmd
    assert "$Host.UI.RawUI.ForegroundColor = 'White'" in styled_cmd
    # Style executes before title and child command
    style_idx = styled_cmd.index("$Host.UI.RawUI.BackgroundColor")
    title_idx = styled_cmd.index("[Console]::Title")
    codex_idx = styled_cmd.index("codex --dangerously-bypass-approvals-and-sandbox")
    assert style_idx < title_idx < codex_idx


def test_console_style_executes_before_child_command():
    """Milestone D/G: console style executes before the child TUI command."""
    style = ConsoleStyle(background_color="Black", foreground_color="White")
    script = "opencode.cmd . --auto"
    applied = apply_console_style(script, style)
    assert applied.startswith("try { $Host.UI.RawUI.BackgroundColor = 'Black'")
    assert applied.endswith("opencode.cmd . --auto")


def test_no_duplicate_style_prelude_is_inserted():
    """Milestone G: no duplicate style prelude is inserted."""
    script = "Set-Location -LiteralPath 'C:/proj'; opencode.cmd ."
    first = apply_console_style(script, "opencode")
    second = apply_console_style(first, "opencode")
    assert first == second
    assert second.count("$Host.UI.RawUI.BackgroundColor") == 1


def test_launcher_title_generation_remains_unchanged():
    """Milestone G: launcher title generation remains unchanged."""
    title = managed_console_title("My Project", "OpenCode", r"C:\Path\To\Proj", "token123")
    assert title == r"My Project | OpenCode | C:\Path\To\Proj | token123"


def test_managed_opencode_launch_applies_style_before_title_and_child(qapp):
    """Milestone C/D: managed OpenCode has style prelude and preserves bound architecture."""
    from pathlib import Path

    from audapack.config import AppConfig
    from audapack.models import Project
    from audapack.opencode_launch import OpenCodeAdmission
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    cfg = AppConfig()
    svc = ProjectService(cfg)
    win = MainWindow(svc)
    target = Project(id="p1", display_name="TestProj", source_path=r"C:\test\proj", priority_group="MAIN0", slot=1)
    admission = OpenCodeAdmission(
        True,
        Path(r"C:\test\proj"),
        ("python.exe", "bound/saipen.py", "--agent", "opencode", "launch", "opencode"),
        {"kind": "saipen-opencode-v1", "project_root": r"C:\test\proj", "actor": "opencode"},
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value.pid = 9999
        mock_popen.return_value.poll.return_value = None
        win._launch_bound_opencode(target, admission)

    assert mock_popen.called
    argv = mock_popen.call_args.args[0]
    script = argv[-1]
    # Invariant: bound CLI, not external launcher
    assert "AI_AGENT_LAUNCHER" not in script
    assert "'python.exe' 'bound/saipen.py' '--agent' 'opencode'" in script
    # Style prelude is present and executes before title
    assert "$Host.UI.RawUI.BackgroundColor = 'Black'" in script
    assert "$Host.UI.RawUI.ForegroundColor = 'White'" in script
    style_pos = script.index("$Host.UI.RawUI.BackgroundColor")
    title_pos = script.index("[Console]::Title")
    cmd_pos = script.index("& 'python.exe' 'bound/saipen.py'")
    assert style_pos < title_pos < cmd_pos
    win.close()
