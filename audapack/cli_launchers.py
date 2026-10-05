"""CLI-first launch resolution for the multi-agent CLI launchers.

SRC-081 / APP-CLI-001 (TARGET B..F, TARGET M): one authoritative resolver for
the built-in CLI launchers ``claude1``, ``claude2``, ``antigravity`` and
``zcode``. Provider-specific launch behaviour lives here as *data* (candidate
commands, profile rules); every semantic -- template precedence, persistence,
diagnostics, console ownership -- is shared.

Resolution order (TARGET B)::

    1. explicit operator ``command_template``
    2. an existing local account/profile-specific launcher wrapper
    3. a directly discoverable CLI executable/command
    4. a bounded, documented fallback (explicitly labelled non-CLI)

Discovery is LOCAL and READ-ONLY: fixed candidate paths plus one PATH scan.
No network installation, no credential handling, no provider-config mutation.
The first stable answer is persisted on the launcher config (``resolved_*``
fields) so a click never rescans the machine.

Claude 1 / Claude 2 (TARGET C) are distinct launch targets. The machine's
existing account isolation is two independent profile directories
(``~/.claude`` and ``~/.claude-account2``, each with its own credentials)
selected through the ``CLAUDE_CONFIG_DIR`` environment variable the installed
claude binary honours. When the second profile cannot be proven present,
Claude 2 reports an unresolved-profile diagnostic and does NOT launch the
default account under the Claude 2 label.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

#: The four built-in CLI launcher identities (TARGET A). Durable ids -- never
#: display names. Claude 1 and Claude 2 are deliberately separate entries.
BUILTIN_CLI_LAUNCHERS: tuple[str, ...] = ("claude1", "claude2", "antigravity", "zcode")

#: TARGET J: the ordered keyboard model. Ctrl+1..9 address launcher positions
#: 1..9 and Ctrl+0 position 10 -- the digit row is the only ceiling, so the
#: tenth shipped launcher is reachable from the same model as the first. The
#: same order with Shift is force-new. Launcher reordering redefines positions.
LAUNCHER_SHORTCUT_KEYS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")

#: Stage names are part of the operator diagnostic (TARGET M) -- the
#: "resolution stage" a failure report must name.
STAGE_TEMPLATE = "command_template"
STAGE_PERSISTED = "persisted"
STAGE_WRAPPER = "wrapper"
STAGE_CLI = "cli"
STAGE_FALLBACK = "fallback"
STAGE_PROFILE = "profile"
STAGE_UNRESOLVED = "unresolved"

#: Claude profile directories already established on this machine. Ordered:
#: the first one with its own credential/config evidence wins.
_CLAUDE_ACCOUNT2_DIRS: tuple[str, ...] = (".claude-account2", ".claude-2")
_CLAUDE_DEFAULT_DIR: str = ".claude"
#: The installed claude binary honours this variable for profile selection
#: (verified as a supported selector of the shipped claude.exe).
_CLAUDE_CONFIG_ENV: str = "CLAUDE_CONFIG_DIR"

#: Every built-in agent console starts in its own "YOLO" mode: the CLI's
#: documented flag that skips per-tool permission prompts. AUDAPACK consoles
#: are operator-launched work windows; a prompt per tool call defeats them.
CLAUDE_YOLO_ARGS: str = "--dangerously-skip-permissions"
ANTIGRAVITY_YOLO_ARGS: str = "--dangerously-skip-permissions"
CODEX_YOLO_ARGS: str = "--dangerously-bypass-approvals-and-sandbox"
ZCODE_YOLO_ARGS: str = "--mode yolo"
#: Local full ZCode CLI build (terminal UI included); ZCODE_CLI overrides it.
ZCODE_CHECKOUT_CLI: str = (
    r"V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\_ZAICODE\zcode\apps\zcode-cli\packages\cli\dist\zcode.cjs"
)

#: Codex accounts: launcher id -> isolated CODEX_HOME directory name. The
#: secondary accounts also drop inherited API keys so an environment-level
#: key can never silently replace the account's own login.
CODEX_ACCOUNT_HOMES: dict[str, str] = {
    "main_codex": ".codex",
    "main_codex2": ".codex-account2",
    "main_codex3_free": ".codex-account3free",
}
_CODEX_SCRUBBED_ENV: tuple[str, ...] = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


@dataclass(frozen=True)
class CliResolution:
    """How one launcher is actually invoked -- or why it cannot be."""

    launcher_id: str
    ok: bool
    #: Which resolution stage produced this answer (TARGET M).
    stage: str
    #: PowerShell snippet executed inside the project-root console. Empty when
    #: ``ok`` is False. Never contains credentials.
    console_command: str = ""
    #: True for a real CLI/TUI path; False only for an honest GUI fallback.
    is_cli: bool = True
    #: Non-secret profile identity (e.g. ``default (~/.claude)``). Never a
    #: token, never credential material.
    profile: str = ""
    #: Absolute path whose existence revalidates a persisted resolution.
    probe: str = ""
    #: Unexpanded command (placeholders intact) -- what persistence stores, so
    #: one project's root never gets baked into another project's launch.
    raw_command: str = ""
    #: The missing command/profile/wrapper (TARGET M).
    missing: str = ""
    #: The next useful operator action (TARGET M).
    next_action: str = ""

    def diagnostic(self, launcher_name: str, project_name: str) -> str:
        """One bounded launcher-specific status line (TARGET M)."""
        parts = [
            f"{launcher_name or self.launcher_id} unavailable for {project_name or 'project'}",
            f"stage={self.stage}",
        ]
        if self.missing:
            parts.append(f"missing: {self.missing}")
        if self.next_action:
            parts.append(f"next: {self.next_action}")
        return " | ".join(parts)


def ps_quote(value: str) -> str:
    """PowerShell single-quote literal (same escaping the bound path uses)."""
    return "'" + str(value).replace("'", "''") + "'"


def expand_command_template(template: str, project_root: str, project_name: str = "") -> str:
    """The operator's template with the documented placeholders resolved.

    Shared by the custom-launcher path and the CLI-launcher path so there is
    exactly one placeholder vocabulary: ``{workdir}``/``{path}`` = the project
    root, ``{project}`` = the display name.
    """
    text = str(template or "")
    text = text.replace("{workdir}", project_root).replace("{path}", project_root)
    text = text.replace("{project}", project_name)
    return text


def managed_console_title(project_name: str, launcher_name: str, project_root: str, token: str) -> str:
    """Canonical managed console title (TARGET K).

    Carries project, launcher and the canonical project discriminator plus the
    correlation token -- and nothing secret: never a credential, token value
    other than the random correlation nonce, or environment content.
    """
    def _clean(value: str) -> str:
        return str(value or "").replace("|", "/").replace('"', "").strip()

    return f"{_clean(project_name)} | {_clean(launcher_name)} | {_clean(project_root)} | {token}"


def _default_home(environ: Mapping[str, str]) -> Path:
    override = environ.get("AUDAPACK_FAKE_HOME")  # test seam only
    if override:
        return Path(override)
    return Path(environ.get("USERPROFILE") or Path.home())


def _default_local_appdata(environ: Mapping[str, str]) -> Path:
    override = environ.get("AUDAPACK_FAKE_LOCALAPPDATA")  # test seam only
    if override:
        return Path(override)
    return Path(environ.get("LOCALAPPDATA") or (_default_home(environ) / "AppData" / "Local"))


def _path_lookup(
    names: tuple[str, ...],
    environ: Mapping[str, str],
    exists: Callable[[str], bool],
) -> Optional[Path]:
    """First existing PATH hit for ``names`` -- one bounded scan, no recursion."""
    path_var = environ.get("PATH") or environ.get("Path") or ""
    separator = ";" if ";" in path_var else os.pathsep
    for entry in path_var.split(separator):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        for name in names:
            candidate = str(Path(entry) / name)
            if exists(candidate):
                return Path(candidate)
    return None


def _first_existing(
    candidates: tuple[str, ...],
    exists: Callable[[str], bool],
) -> Optional[Path]:
    for candidate in candidates:
        if exists(candidate):
            return Path(candidate)
    return None


def _claude_exe(home: Path, environ: Mapping[str, str], exists: Callable[[str], bool]) -> Optional[Path]:
    """CLI-first Claude lookup: local install, then PATH. No install, ever."""
    local = _first_existing(
        (
            str(home / ".local" / "bin" / "claude.exe"),
            str(home / ".local" / "bin" / "claude.cmd"),
            str(home / ".local" / "bin" / "claude"),
        ),
        exists,
    )
    if local is not None:
        return local
    return _path_lookup(("claude.exe", "claude.cmd", "claude"), environ, exists)


def _claude_profile_evidence(profile_dir: Path, exists: Callable[[str], bool]) -> bool:
    """A profile is real when it carries its own config/credential evidence.

    Existence of the directory alone is not proof of an account; the machine's
    two established profiles each carry their own ``.claude.json`` /
    ``.credentials.json``. This probes filenames only -- it never reads,
    copies or exposes credential contents.
    """
    return bool(
        exists(str(profile_dir / ".credentials.json"))
        or exists(str(profile_dir / ".claude.json"))
    )


def _resolve_claude(
    launcher_id: str,
    home: Path,
    environ: Mapping[str, str],
    exists: Callable[[str], bool],
) -> CliResolution:
    exe = _claude_exe(home, environ, exists)
    if exe is None:
        return CliResolution(
            launcher_id=launcher_id,
            ok=False,
            stage=STAGE_UNRESOLVED,
            missing="claude CLI (claude.exe)",
            next_action=(
                "install the Claude Code CLI on PATH or under %USERPROFILE%\\.local\\bin, "
                "or set this launcher's command_template in Settings -> Launchers"
            ),
        )

    if launcher_id == "claude1":
        profile_dir = home / _CLAUDE_DEFAULT_DIR
        profile = f"default (~/{_CLAUDE_DEFAULT_DIR})"
        command = f"& {ps_quote(str(exe))} {CLAUDE_YOLO_ARGS}"
        return CliResolution(
            launcher_id=launcher_id,
            ok=True,
            stage=STAGE_CLI,
            console_command=command,
            is_cli=True,
            profile=profile,
            probe=str(exe),
        )

    # claude2: a genuinely distinct local account/profile or nothing (TARGET C).
    profile_dir: Optional[Path] = None
    for name in _CLAUDE_ACCOUNT2_DIRS:
        candidate = home / name
        if _claude_profile_evidence(candidate, exists):
            profile_dir = candidate
            break
    if profile_dir is None:
        tried = ", ".join(f"~{name}" for name in _CLAUDE_ACCOUNT2_DIRS)
        return CliResolution(
            launcher_id=launcher_id,
            ok=False,
            stage=STAGE_PROFILE,
            missing=f"second Claude profile ({tried} with its own .credentials.json/.claude.json)",
            next_action=(
                "point CLAUDE_CONFIG_DIR at a second, already-authenticated Claude profile "
                "or set a command_template wrapper for this account; Claude 1 stays usable"
            ),
        )
    command = (
        f"$env:{_CLAUDE_CONFIG_ENV} = {ps_quote(str(profile_dir))}; "
        f"& {ps_quote(str(exe))} {CLAUDE_YOLO_ARGS}"
    )
    return CliResolution(
        launcher_id=launcher_id,
        ok=True,
        stage=STAGE_CLI,
        console_command=command,
        is_cli=True,
        profile=f"account2 (~/{profile_dir.name})",
        probe=str(exe),
    )


def _resolve_antigravity(
    home: Path,
    local_appdata: Path,
    environ: Mapping[str, str],
    exists: Callable[[str], bool],
) -> CliResolution:
    missing_hint = (
        "Antigravity CLI wrapper (bin\\antigravity-ide.cmd) or Antigravity.exe; "
        "set a command_template if it lives elsewhere"
    )
    # The real terminal agent (agy) wins over everything else: it is the only
    # Antigravity entrypoint that runs in the console, and it runs in YOLO
    # mode. The caller has already moved the console into the project root.
    agy = _first_existing(
        (str(local_appdata / "agy" / "bin" / "agy.exe"),),
        exists,
    ) or _path_lookup(("agy.exe",), environ, exists)
    if agy is not None:
        return CliResolution(
            launcher_id="antigravity",
            ok=True,
            stage=STAGE_CLI,
            console_command=f"& {ps_quote(str(agy))} {ANTIGRAVITY_YOLO_ARGS}",
            is_cli=True,
            probe=str(agy),
        )
    # Tier 2: the installed CLI-compatible wrapper (verified on this machine:
    # Antigravity IDE 1.107 ships bin\antigravity-ide.cmd, a real folder CLI).
    wrapper = _first_existing(
        (
            str(local_appdata / "Programs" / "Antigravity IDE" / "bin" / "antigravity-ide.cmd"),
        ),
        exists,
    ) or _path_lookup(("antigravity-ide.cmd", "antigravity-ide"), environ, exists)
    if wrapper is not None:
        return CliResolution(
            launcher_id="antigravity",
            ok=True,
            stage=STAGE_WRAPPER,
            console_command=f"& {ps_quote(str(wrapper))} {ps_quote('{workdir}')}",
            is_cli=True,
            probe=str(wrapper),
        )
    # Tier 3: a directly discoverable CLI command.
    cli = _path_lookup(("antigravity.cmd", "antigravity"), environ, exists)
    if cli is not None:
        return CliResolution(
            launcher_id="antigravity",
            ok=True,
            stage=STAGE_CLI,
            console_command=f"& {ps_quote(str(cli))} {ps_quote('{workdir}')}",
            is_cli=True,
            probe=str(cli),
        )
    # Tier 4: bounded documented fallback -- GUI executable, honestly labelled.
    gui = _first_existing(
        (
            str(local_appdata / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe"),
            str(local_appdata / "Programs" / "antigravity" / "Antigravity.exe"),
        ),
        exists,
    )
    if gui is not None:
        return CliResolution(
            launcher_id="antigravity",
            ok=True,
            stage=STAGE_FALLBACK,
            console_command=(
                f"Start-Process -FilePath {ps_quote(str(gui))} "
                f"-ArgumentList {ps_quote('{workdir}')}"
            ),
            is_cli=False,
            probe=str(gui),
            missing="no Antigravity CLI entrypoint; using the installed GUI executable",
        )
    return CliResolution(
        launcher_id="antigravity",
        ok=False,
        stage=STAGE_UNRESOLVED,
        missing=missing_hint,
        next_action=(
            "install Antigravity or set this launcher's command_template in "
            "Settings -> Launchers; other launchers stay usable"
        ),
    )


def _resolve_zcode(
    home: Path,
    local_appdata: Path,
    environ: Mapping[str, str],
    exists: Callable[[str], bool],
) -> CliResolution:
    # A full ZCode CLI build (with its terminal UI) wins: the desktop app only
    # bundles the headless part. ZCODE_CLI names its dist/zcode.cjs; the known
    # local checkout is the second candidate. Runs in yolo permission mode.
    node = _path_lookup(("node.exe",), environ, exists)
    for script in (environ.get("ZCODE_CLI", ""), ZCODE_CHECKOUT_CLI):
        if node is not None and script and exists(script):
            return CliResolution(
                launcher_id="zcode",
                ok=True,
                stage=STAGE_CLI,
                console_command=(
                    f"& {ps_quote(str(node))} {ps_quote(script)} "
                    f"--cwd {ps_quote('{workdir}')} {ZCODE_YOLO_ARGS}"
                ),
                is_cli=True,
                probe=script,
            )
    # Tier 3 first in practice: no ZCode CLI shim is known on this machine, but
    # a PATH-visible one must win when present.
    cli = _first_existing(
        (
            str(local_appdata / "Programs" / "ZCode" / "bin" / "zcode.cmd"),
            str(home / ".zcode" / "bin" / "zcode.cmd"),
        ),
        exists,
    ) or _path_lookup(("zcode.cmd", "zcode.exe", "zcode"), environ, exists)
    if cli is not None and str(cli).lower().endswith((".cmd", ".bat")):
        return CliResolution(
            launcher_id="zcode",
            ok=True,
            stage=STAGE_CLI,
            console_command=f"& {ps_quote(str(cli))} {ps_quote('{workdir}')}",
            is_cli=True,
            probe=str(cli),
        )
    if cli is not None:
        return CliResolution(
            launcher_id="zcode",
            ok=True,
            stage=STAGE_CLI,
            console_command=f"& {ps_quote(str(cli))} {ps_quote('{workdir}')}",
            is_cli=True,
            probe=str(cli),
        )
    # Tier 4: bounded documented fallback -- the installed ZCode desktop
    # (Electron, no CLI shim). Labelled non-CLI; never pretended to be a TUI.
    gui = _first_existing(
        (str(local_appdata / "Programs" / "ZCode" / "ZCode.exe"),),
        exists,
    )
    if gui is not None:
        return CliResolution(
            launcher_id="zcode",
            ok=True,
            stage=STAGE_FALLBACK,
            console_command=(
                f"Start-Process -FilePath {ps_quote(str(gui))} "
                f"-ArgumentList {ps_quote('{workdir}')}"
            ),
            is_cli=False,
            probe=str(gui),
            missing="no ZCode CLI entrypoint; using the installed ZCode desktop executable",
        )
    return CliResolution(
        launcher_id="zcode",
        ok=False,
        stage=STAGE_UNRESOLVED,
        missing="ZCode executable (ZCode.exe) or a zcode CLI command",
        next_action=(
            "install ZCode or set this launcher's command_template in "
            "Settings -> Launchers; other launchers stay usable"
        ),
    )


def codex_console_command(account: str, project_root: str, title: str = "",
                          model: str = "", launcher: Any = None) -> str:
    """PowerShell for one Codex account console in ``project_root``, YOLO mode.

    Self-contained: no external per-account launcher script. The account is
    selected by CODEX_HOME inside this console only; secondary accounts also
    drop inherited API keys. A missing ``codex`` command prints the install
    hint in the console instead of a bare CommandNotFoundException.
    """
    from audapack.console_style import resolve_console_style

    style = resolve_console_style(launcher if launcher is not None else account)
    style_prelude = style.powershell_prelude().strip()

    home_name = CODEX_ACCOUNT_HOMES.get(account, CODEX_ACCOUNT_HOMES["main_codex"])
    parts: list[str] = []
    if style_prelude:
        parts.append(style_prelude.rstrip(";"))
    if title:
        parts.append(f"[Console]::Title = {ps_quote(title)}")
    parts.append(f"$env:CODEX_HOME = Join-Path $env:USERPROFILE {ps_quote(home_name)}")
    if account != "main_codex":
        scrub = ",".join(f"Env:{name}" for name in _CODEX_SCRUBBED_ENV)
        parts.append(f"Remove-Item {scrub} -ErrorAction SilentlyContinue")
    parts.append("New-Item -ItemType Directory -Force -Path $env:CODEX_HOME | Out-Null")
    parts.append(f"Set-Location -LiteralPath {ps_quote(project_root)}")
    model_arg = f" --model {ps_quote(model)}" if str(model or "").strip() else ""
    parts.append(
        "if (Get-Command codex -ErrorAction SilentlyContinue) { "
        f"codex{model_arg} {CODEX_YOLO_ARGS} "
        "} else { Write-Host 'codex not found on PATH. Install: npm install -g @openai/codex' "
        "-ForegroundColor Yellow }"
    )
    return "; ".join(parts)


def _discover(
    launcher_id: str,
    home: Path,
    local_appdata: Path,
    environ: Mapping[str, str],
    exists: Callable[[str], bool],
) -> CliResolution:
    if launcher_id in ("claude1", "claude2"):
        return _resolve_claude(launcher_id, home, environ, exists)
    if launcher_id == "antigravity":
        return _resolve_antigravity(home, local_appdata, environ, exists)
    if launcher_id == "zcode":
        return _resolve_zcode(home, local_appdata, environ, exists)
    return CliResolution(
        launcher_id=launcher_id,
        ok=False,
        stage=STAGE_UNRESOLVED,
        missing=f"no built-in CLI profile for launcher id {launcher_id!r}",
        next_action="set this launcher's command_template in Settings -> Launchers",
    )


def resolve_cli_launcher(
    launcher: Any,
    *,
    project_root: str = "",
    project_name: str = "",
    environ: Optional[Mapping[str, str]] = None,
    exists: Optional[Callable[[str], bool]] = None,
) -> CliResolution:
    """Resolve how ``launcher`` is invoked, honouring TARGET B order.

    ``launcher`` is any object with ``id``/``command_template`` (a
    :class:`audapack.config.LauncherConfig` in production). The result's
    ``console_command`` is ready for the project-root console: the caller owns
    ``Set-Location`` into the canonical project root and the managed title.
    ``{workdir}`` placeholders inside a resolved command are expanded here.
    """
    env: Mapping[str, str] = environ if environ is not None else os.environ
    probe: Callable[[str], bool] = exists if exists is not None else os.path.exists
    launcher_id = str(getattr(launcher, "id", "") or "").strip()
    root = str(project_root or "")

    # 1. explicit operator command_template is authoritative (TARGET F).
    template = str(getattr(launcher, "command_template", "") or "").strip()
    if template:
        return CliResolution(
            launcher_id=launcher_id,
            ok=True,
            stage=STAGE_TEMPLATE,
            console_command=expand_command_template(template, root, project_name),
            is_cli=True,
            profile="operator command_template",
            raw_command=template,
        )

    # 2. a previously persisted stable resolution, revalidated by probe.
    persisted = str(getattr(launcher, "resolved_command", "") or "").strip()
    persisted_probe = str(getattr(launcher, "resolved_probe", "") or "").strip()
    if persisted and (not persisted_probe or probe(persisted_probe)):
        stage = STAGE_PERSISTED
        if str(getattr(launcher, "resolved_stage", "") or "").strip():
            stage = f"{STAGE_PERSISTED}({str(launcher.resolved_stage).strip()})"
        return CliResolution(
            launcher_id=launcher_id,
            ok=True,
            stage=stage,
            console_command=expand_command_template(persisted, root, project_name),
            is_cli=bool(getattr(launcher, "resolved_is_cli", True)),
            profile=str(getattr(launcher, "resolved_profile", "") or ""),
            probe=persisted_probe,
        )

    # 3./4. bounded local discovery (wrapper > CLI > documented fallback).
    resolution = _discover(
        launcher_id,
        _default_home(env),
        _default_local_appdata(env),
        env,
        probe,
    )
    if resolution.ok:
        resolution = replace(
            resolution,
            raw_command=resolution.console_command,
            console_command=expand_command_template(resolution.console_command, root, project_name),
        )
    return resolution


def persist_resolution(launcher: Any, resolution: CliResolution) -> bool:
    """Write a stable resolution back onto the launcher config.

    Returns True when anything changed. A template-stage answer is never
    persisted here: ``command_template`` is already the durable operator
    intent, and duplicating it would create a second source of truth.
    """
    if not resolution.ok or resolution.stage in (STAGE_TEMPLATE, STAGE_PERSISTED) or resolution.stage.startswith(STAGE_PERSISTED):
        return False
    raw = resolution.raw_command or resolution.console_command
    changed = False
    if str(getattr(launcher, "resolved_command", "") or "") != raw:
        launcher.resolved_command = raw
        changed = True
    if str(getattr(launcher, "resolved_probe", "") or "") != resolution.probe:
        launcher.resolved_probe = resolution.probe
        changed = True
    if str(getattr(launcher, "resolved_profile", "") or "") != resolution.profile:
        launcher.resolved_profile = resolution.profile
        changed = True
    base_stage = resolution.stage.split("(", 1)[0]
    if str(getattr(launcher, "resolved_stage", "") or "") != base_stage:
        launcher.resolved_stage = base_stage
        changed = True
    is_cli = bool(resolution.is_cli)
    if bool(getattr(launcher, "resolved_is_cli", True)) != is_cli:
        launcher.resolved_is_cli = is_cli
        changed = True
    return changed
