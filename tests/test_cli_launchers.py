"""SRC-081 / APP-CLI-001: CLI-first multi-agent launcher semantics (TARGET N).

Deterministic by construction: every filesystem/PATH probe is injected, so
these prove the semantic contract -- resolution order (TARGET B), Claude
account separation (TARGET C), config migration safety (TARGET G), label truth
(TARGET I), launcher-specific diagnostics (TARGET M) and fail-open behaviour --
without touching the real machine.

"""

from __future__ import annotations

from pathlib import Path

from audapack.cli_launchers import (
    BUILTIN_CLI_LAUNCHERS,
    expand_command_template,
    managed_console_title,
    persist_resolution,
    ps_quote,
    resolve_cli_launcher,
)
from audapack.config import (
    DEFAULT_LAUNCHER_SPECS,
    DEFAULT_SHORT_LABELS,
    SCHEMA_VERSION,
    LauncherConfig,
    create_default_launchers,
    migrate_legacy_data,
)

HOME = Path(r"C:\Users\tester")
LOCAL = HOME / "AppData" / "Local"
CLAUDE_EXE = HOME / ".local" / "bin" / "claude.exe"
ACCOUNT2_DIR = HOME / ".claude-account2"
ACCOUNT2_CRED = ACCOUNT2_DIR / ".credentials.json"
AG_WRAPPER = LOCAL / "Programs" / "Antigravity IDE" / "bin" / "antigravity-ide.cmd"
AG_GUI = LOCAL / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe"
ZCODE_GUI = LOCAL / "Programs" / "ZCode" / "ZCode.exe"
ROOT_A = r"V:\code\a"
ROOT_B = r"V:\code\b"


class FakeFs:
    """Case-insensitive existence oracle over an explicit path set."""

    def __init__(self, paths=()):
        self.paths = {str(p).lower() for p in paths}

    def __call__(self, candidate: str) -> bool:
        return str(candidate).lower() in self.paths


def fake_env(path: str = r"C:\bin") -> dict:
    return {"USERPROFILE": str(HOME), "LOCALAPPDATA": str(LOCAL), "PATH": path}


def _launcher(launcher_id: str, **overrides) -> LauncherConfig:
    data = {
        "id": launcher_id,
        "name": launcher_id,
        "short_label": DEFAULT_SHORT_LABELS.get(launcher_id, "??"),
        "command_template": "",
        "agent_type": "powershell",
        "enabled": True,
        "max_instances": 0,
    }
    data.update(overrides)
    return LauncherConfig.from_dict(data)


def _old_six_config(**launchers_by_id) -> dict:
    """A schema-5 config: the historical six launchers, operator-modified."""
    specs = [
        ("opencode", "OpenCode", "OC"),
        ("freebuff", "FreeBuff", "FB"),
        ("cline", "Cline", "CL"),
        ("main_codex", "Codex 1", "C1"),
        ("main_codex2", "Codex 2", "C2"),
        ("main_codex3_free", "Codex Free", "CF"),
    ]
    launchers = []
    for launcher_id, name, label in specs:
        entry = {
            "id": launcher_id,
            "name": name,
            "short_label": label,
            "command_template": "",
            "agent_type": "powershell",
            "enabled": True,
            "max_instances": 0,
        }
        entry.update(launchers_by_id.get(launcher_id, {}))
        launchers.append(entry)
    return {"schema_version": 5, "projects": [], "launchers": launchers}


# ---------------------------------------------------------------------------
# TARGET N 1: fresh install ships all ten intended launchers
# ---------------------------------------------------------------------------


def test_default_config_contains_all_ten_intended_launchers():
    launchers = create_default_launchers()
    ids = [lc.id for lc in launchers]
    assert ids == [spec_id for spec_id, _name in DEFAULT_LAUNCHER_SPECS]
    assert len(ids) == 10
    for new_id in BUILTIN_CLI_LAUNCHERS:
        assert new_id in ids
    # Claude 1 and Claude 2 are distinct identities (TARGET A), never collapsed.
    assert "claude1" in ids and "claude2" in ids and "claude1" != "claude2"
    # Compact labels never reuse the Codex pair C1/C2 (TARGET I).
    assert DEFAULT_SHORT_LABELS["claude1"] == "A1"
    assert DEFAULT_SHORT_LABELS["claude2"] == "A2"
    assert len(set(DEFAULT_SHORT_LABELS.values())) == len(DEFAULT_SHORT_LABELS)


# ---------------------------------------------------------------------------
# TARGET N 2/3/4/5: schema migration
# ---------------------------------------------------------------------------


def test_schema_migration_adds_only_the_four_new_builtins_to_old_config():
    cfg = migrate_legacy_data(_old_six_config())
    ids = [lc.id for lc in cfg.launchers]
    assert ids[:6] == [
        "opencode",
        "freebuff",
        "cline",
        "main_codex",
        "main_codex2",
        "main_codex3_free",
    ]  # existing order preserved
    assert ids[6:] == list(BUILTIN_CLI_LAUNCHERS)  # exactly the four new ones
    assert len(ids) == len(set(ids))
    assert cfg.schema_version == SCHEMA_VERSION


def test_migration_preserves_modified_existing_launcher_settings():
    cfg = migrate_legacy_data(
        _old_six_config(
            opencode={"command_template": "custom.exe --x", "agent_type": "executable"},
            freebuff={"max_instances": 3},
            cline={"enabled": False},
            main_codex={"short_label": "CX"},
        )
    )
    by_id = {lc.id: lc for lc in cfg.launchers}
    assert by_id["opencode"].command_template == "custom.exe --x"
    assert by_id["opencode"].agent_type == "executable"
    assert by_id["freebuff"].max_instances == 3
    assert by_id["cline"].enabled is False
    assert by_id["main_codex"].short_label == "CX"  # custom label untouched


def test_migration_is_idempotent_across_load_save_reload():
    first = migrate_legacy_data(_old_six_config())
    # Loading the SAME old bytes twice appends nothing twice.
    second = migrate_legacy_data(_old_six_config())
    assert [lc.id for lc in first.launchers] == [lc.id for lc in second.launchers]
    # Save + reload (schema 6 now) never appends duplicates either.
    reloaded = migrate_legacy_data(
        {
            "schema_version": SCHEMA_VERSION,
            "projects": [],
            "launchers": [lc.to_dict() for lc in first.launchers],
        }
    )
    ids = [lc.id for lc in reloaded.launchers]
    assert ids == [lc.id for lc in first.launchers]
    assert len(ids) == len(set(ids))


def test_preexisting_custom_zcode_launcher_is_not_overwritten():
    custom = {
        "id": "zcode",
        "name": "ZCode Work",
        "short_label": "ZW",
        "command_template": "zwork.cmd {workdir}",
        "agent_type": "custom",
        "enabled": True,
        "max_instances": 2,
    }
    data = _old_six_config()
    data["launchers"].append(custom)
    cfg = migrate_legacy_data(data)
    zcodes = [lc for lc in cfg.launchers if lc.id == "zcode"]
    assert len(zcodes) == 1  # no duplicate appended over the custom entry
    assert zcodes[0].name == "ZCode Work"
    assert zcodes[0].command_template == "zwork.cmd {workdir}"
    assert zcodes[0].agent_type == "custom"
    assert zcodes[0].max_instances == 2


def test_migration_never_resurrects_deliberately_removed_launchers():
    """A later-schema config is the operator's curated state (TARGET G)."""
    data = _old_six_config()
    data["schema_version"] = SCHEMA_VERSION
    data["launchers"] = [lc for lc in data["launchers"] if lc["id"] != "cline"]
    cfg = migrate_legacy_data(data)
    ids = [lc.id for lc in cfg.launchers]
    assert "cline" not in ids
    assert "claude1" not in ids  # no re-fill of anything on a schema-6 config


# ---------------------------------------------------------------------------
# TARGET N 6/7: template override + distinct Claude identities
# ---------------------------------------------------------------------------


def test_command_template_overrides_builtin_claude1_behavior():
    cfg = _launcher(
        "claude1",
        name="Claude 1",
        command_template="my-claude-wrapper.cmd {project} {workdir}",
    )
    fs = FakeFs([CLAUDE_EXE, ACCOUNT2_CRED])
    res = resolve_cli_launcher(
        cfg, project_root=ROOT_A, project_name="Project A", environ=fake_env(), exists=fs
    )
    assert res.ok
    assert res.stage == "command_template"
    # The OPERATOR's command runs -- the built-in claude.exe never does.
    assert res.console_command == f"my-claude-wrapper.cmd Project A {ROOT_A}"
    assert str(CLAUDE_EXE) not in res.console_command
    # A template is operator intent already; it is never re-persisted.
    assert persist_resolution(cfg, res) is False
    assert cfg.resolved_command == ""


def test_claude1_and_claude2_are_distinct_and_separated_by_profile():
    fs = FakeFs([CLAUDE_EXE, ACCOUNT2_CRED])
    one = resolve_cli_launcher(
        _launcher("claude1", name="Claude 1"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    two = resolve_cli_launcher(
        _launcher("claude2", name="Claude 2"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    assert one.ok and two.ok
    assert one.launcher_id != two.launcher_id
    # Claude 2 selects the SECOND profile through the machine's real mechanism
    # (CLAUDE_CONFIG_DIR -> the existing .claude-account2 account directory).
    assert f"$env:CLAUDE_CONFIG_DIR = {ps_quote(str(ACCOUNT2_DIR))}" in two.console_command
    assert "CLAUDE_CONFIG_DIR" not in one.console_command
    assert "account2" in two.profile
    assert "default" in one.profile


def test_claude2_without_a_real_second_profile_never_launches_the_default_account():
    fs = FakeFs([CLAUDE_EXE])  # no .claude-account2 evidence at all
    two = resolve_cli_launcher(
        _launcher("claude2", name="Claude 2"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    assert not two.ok
    assert two.stage == "profile"
    # TARGET C: it must NOT silently launch the same account under Claude 2.
    assert two.console_command == ""
    assert "second Claude profile" in two.missing
    diag = two.diagnostic("Claude 2", "Project A")
    assert "Claude 2" in diag and "Project A" in diag and "next:" in diag


# ---------------------------------------------------------------------------
# TARGET N 11/12: project-root behaviour
# ---------------------------------------------------------------------------


def test_antigravity_launch_uses_the_selected_project_root():
    fs = FakeFs([AG_WRAPPER])
    res = resolve_cli_launcher(
        _launcher("antigravity", name="Antigravity"),
        project_root=ROOT_A,
        environ=fake_env(),
        exists=fs,
    )
    assert res.ok and res.stage == "wrapper" and res.is_cli
    assert ps_quote(ROOT_A) in res.console_command
    assert str(AG_WRAPPER) in res.console_command


def test_zcode_launch_uses_the_selected_project_root():
    fs = FakeFs([ZCODE_GUI])
    res = resolve_cli_launcher(
        _launcher("zcode", name="ZCode"), project_root=ROOT_B, environ=fake_env(), exists=fs
    )
    assert res.ok
    assert res.stage == "fallback"  # honest GUI fallback: ZCode has no CLI shim
    assert res.is_cli is False
    assert ps_quote(ROOT_B) in res.console_command
    assert str(ZCODE_GUI) in res.console_command


def test_resolution_prefers_wrapper_then_cli_then_documented_fallback():
    # Wrapper beats a PATH CLI for Antigravity (TARGET B order 2 before 3).
    fs = FakeFs([AG_WRAPPER, AG_GUI])
    res = resolve_cli_launcher(
        _launcher("antigravity"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    assert res.stage == "wrapper"
    # No wrapper/CLI -> the GUI executable, explicitly labelled non-CLI.
    fs_gui = FakeFs([AG_GUI])
    res_gui = resolve_cli_launcher(
        _launcher("antigravity"), project_root=ROOT_A, environ=fake_env(), exists=fs_gui
    )
    assert res_gui.stage == "fallback" and res_gui.is_cli is False
    # A PATH CLI beats the GUI fallback for ZCode (TARGET E).
    fs_cli = FakeFs([ZCODE_GUI, Path(r"C:\bin\zcode.cmd")])
    res_cli = resolve_cli_launcher(
        _launcher("zcode"), project_root=ROOT_A, environ=fake_env(), exists=fs_cli
    )
    assert res_cli.stage == "cli" and res_cli.is_cli is True


# ---------------------------------------------------------------------------
# TARGET N 13/14: diagnostics + fail-open
# ---------------------------------------------------------------------------


def test_missing_cli_produces_bounded_launcher_specific_diagnostic():
    res = resolve_cli_launcher(
        _launcher("zcode", name="ZCode"), project_root=ROOT_A, environ=fake_env(), exists=FakeFs()
    )
    assert not res.ok and res.stage == "unresolved"
    diag = res.diagnostic("ZCode", "Project A")
    for token in ("ZCode", "Project A", "stage=unresolved", "missing:", "next:"):
        assert token in diag
    assert len(diag) < 400  # bounded, one status line


def test_one_missing_launcher_never_disables_the_others():
    fs = FakeFs([CLAUDE_EXE])  # ZCode/Antigravity absent entirely
    one = resolve_cli_launcher(
        _launcher("claude1"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    zcode = resolve_cli_launcher(
        _launcher("zcode"), project_root=ROOT_A, environ=fake_env(), exists=FakeFs()
    )
    antigravity = resolve_cli_launcher(
        _launcher("antigravity"), project_root=ROOT_A, environ=fake_env(), exists=FakeFs()
    )
    assert one.ok  # the broken providers never poison the working one
    assert not zcode.ok and not antigravity.ok


# ---------------------------------------------------------------------------
# Persistence + identity helpers (TARGET B/K)
# ---------------------------------------------------------------------------


def test_persisted_resolution_is_reused_without_rescanning_the_machine():
    cfg = _launcher("antigravity", name="Antigravity")
    fs = FakeFs([AG_WRAPPER])
    first = resolve_cli_launcher(
        cfg, project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    assert first.ok and persist_resolution(cfg, first) is True
    assert cfg.resolved_command == first.raw_command  # placeholders intact
    # Second click for ANOTHER project: persisted answer, root re-expanded.
    second = resolve_cli_launcher(cfg, project_root=ROOT_B, environ=fake_env(), exists=fs)
    assert second.stage.startswith("persisted")
    assert ps_quote(ROOT_B) in second.console_command
    assert ROOT_A not in second.console_command  # no baked-in foreign root
    assert persist_resolution(cfg, second) is False  # nothing re-written
    # A dead probe invalidates the cache and discovery runs again.
    cfg.resolved_probe = str(AG_WRAPPER)
    dead = resolve_cli_launcher(
        cfg, project_root=ROOT_A, environ=fake_env(), exists=FakeFs()
    )
    assert not dead.ok  # the wrapper is gone; nothing stale is trusted


def test_template_placeholders_expand_deterministically():
    assert expand_command_template("x {workdir} {path} {project}", ROOT_A, "Proj") == (
        f"x {ROOT_A} {ROOT_A} Proj"
    )


def test_managed_console_title_carries_identity_and_no_secret():
    title = managed_console_title("Project A", "Claude 1", ROOT_A, "tok123")
    assert title == f"Project A | Claude 1 | {ROOT_A} | tok123"
    for piece in ("Project A", "Claude 1", ROOT_A, "tok123"):
        assert piece in title
    # Pipe characters can never forge extra title segments.
    forged = managed_console_title("A|B", "Claude 2", ROOT_A, "t")
    assert forged.count("|") == 3


def test_resolution_never_contains_credential_material():
    fs = FakeFs([CLAUDE_EXE, ACCOUNT2_CRED])
    two = resolve_cli_launcher(
        _launcher("claude2"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    blob = two.console_command + two.profile + two.missing + two.next_action
    assert ".credentials.json" not in blob  # evidence probed, never referenced
    assert "sk-" not in blob


# ---------------------------------------------------------------------------
# YOLO consoles: every built-in agent skips its per-tool permission prompts
# ---------------------------------------------------------------------------

AGY_EXE = LOCAL / "agy" / "bin" / "agy.exe"


def test_claude_consoles_start_in_yolo_mode():
    fs = FakeFs([CLAUDE_EXE, ACCOUNT2_CRED])
    for launcher_id in ("claude1", "claude2"):
        res = resolve_cli_launcher(
            _launcher(launcher_id), project_root=ROOT_A, environ=fake_env(), exists=fs
        )
        assert res.ok, res
        assert res.console_command.endswith("--dangerously-skip-permissions")


def test_antigravity_prefers_the_agy_terminal_agent_in_yolo_mode():
    fs = FakeFs([AGY_EXE, AG_WRAPPER, AG_GUI])
    res = resolve_cli_launcher(
        _launcher("antigravity"), project_root=ROOT_A, environ=fake_env(), exists=fs
    )
    assert res.ok and res.stage == "cli" and res.is_cli
    assert str(AGY_EXE) in res.console_command
    assert res.console_command.endswith("--dangerously-skip-permissions")


def test_codex_console_command_isolates_each_account():
    from audapack.cli_launchers import codex_console_command

    one = codex_console_command("main_codex", ROOT_A, "T")
    two = codex_console_command("main_codex2", ROOT_A)
    free = codex_console_command("main_codex3_free", ROOT_B)
    assert "'.codex'" in one and "Remove-Item" not in one
    assert "'.codex-account2'" in two and "Env:OPENAI_API_KEY" in two
    assert "'.codex-account3free'" in free and ps_quote(ROOT_B) in free
    for cmd in (one, two, free):
        assert "codex --dangerously-bypass-approvals-and-sandbox" in cmd
        assert "npm install -g @openai/codex" in cmd

    reserve = codex_console_command("main_codex", ROOT_A, model="gpt-6-luna")
    assert "codex --model 'gpt-6-luna' --dangerously-bypass-approvals-and-sandbox" in reserve


def test_schema_7_drops_the_pre_yolo_resolution_cache():
    data = {
        "schema_version": 6,
        "projects": [],
        "launchers": [
            {
                "id": "claude1",
                "name": "Claude 1",
                "resolved_command": "& 'C:/x/claude.exe'",
                "resolved_probe": "C:/x/claude.exe",
                "resolved_stage": "cli",
                "command_template": "",
            },
            {"id": "opencode", "name": "OpenCode", "command_template": "keep {workdir}"},
        ],
    }
    cfg = migrate_legacy_data(data)
    by_id = {lc.id: lc for lc in cfg.launchers}
    assert by_id["claude1"].resolved_command == ""
    assert by_id["claude1"].resolved_stage == ""
    assert by_id["opencode"].command_template == "keep {workdir}"


def test_zcode_prefers_a_full_cli_build_in_yolo_mode():
    node = Path(r"C:\bin\node.exe")
    script = r"D:\zc\dist\zcode.cjs"
    env = dict(fake_env(), ZCODE_CLI=script)
    res = resolve_cli_launcher(
        _launcher("zcode"), project_root=ROOT_A, environ=env,
        exists=FakeFs([node, script, ZCODE_GUI]),
    )
    assert res.ok and res.stage == "cli" and res.is_cli
    assert ps_quote(script) in res.console_command
    assert ps_quote(ROOT_A) in res.console_command
    assert res.console_command.endswith("--mode yolo")
