"""One effective launch inventory — the split-brain controls.

The defect class this file eliminates: AUDAPACK keeping a SECOND, static list of
Codex/Claude/Antigravity *account* identities beside the canonical SAI Accounts
registry, so the operator's account truth lives in two places and the second one
goes stale.

Every test drives production code. The plane is faked through the two seams
``sai_accounts`` already exposes, so nothing here needs a registry, a credential
or the operator's machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audapack import sai_accounts as sai
from audapack.account_registry import discover_accounts, identity_locator
from audapack.config import AppConfig, LauncherConfig
from audapack.launch_authority import resolve_launch_target
from audapack.launch_federation import (
    ACCOUNT_BACKED_LEGACY_IDS,
    account_id_of,
    is_account_target,
    sync_effective_launchers,
    target_id,
)

ENGINE = r"C:\fake\sai-accounts.exe"

# ── fakes ───────────────────────────────────────────────────────────────────


class Plane:
    """A fake control plane. ``engine == ""`` means the plane is NOT installed."""

    def __init__(self) -> None:
        self.engine = ENGINE
        self.list_ok = True
        self.list_stdout = ""
        self.launch_ok = True
        self.launch_stdout = ""
        self.launch_error = ""
        self.launch_pid = 4242
        self.launch_execution = "current_user"
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout: float) -> dict:
        self.calls.append(list(argv))
        if argv and argv[0] == "list":
            return {"ok": self.list_ok, "stdout": self.list_stdout, "error": ""}
        if argv and argv[0] == "launch":
            if self.launch_stdout or not self.launch_ok or self.launch_error:
                return {"ok": self.launch_ok, "stdout": self.launch_stdout,
                        "error": self.launch_error}
            target_account = argv[1] if len(argv) > 1 else ""
            target_workdir = argv[3] if len(argv) > 3 and argv[2] == "--workdir" else ""
            payload = {
                "schema": "sai.accounts/launch/1",
                "status": "launched",
                "account_id": target_account,
                "provider_id": target_account.split(":", 1)[0] if ":" in target_account else "antigravity",
                "execution": self.launch_execution,
                "broker_context": "S-1-5-21-111111111-222222222-333333333-1001",
                "workdir": target_workdir,
                "command": [r"C:\ProgramData\SAI\Accounts\bin\agy.exe", "--dangerously-skip-permissions"],
                "pid": self.launch_pid,
            }
            return {"ok": True, "stdout": json.dumps(payload), "error": ""}
        return {"ok": True, "stdout": "{}", "error": ""}

    def answer_list(self, *accounts: dict) -> None:
        self.list_stdout = json.dumps(
            {"schema": "sai.accounts/list/1", "generation": 1,
             "accounts": list(accounts), "status": "ok"})


@pytest.fixture(autouse=True)
def _isolate_plane():
    saved_cache = list(sai._LIST_CACHE)
    saved_engine, saved_run = sai.TestEngine, sai.TestRun
    sai.TestEngine, sai.TestRun = lambda: "", None
    sai.remember([])
    try:
        yield
    finally:
        sai._LIST_CACHE[:] = saved_cache
        sai.TestEngine, sai.TestRun = saved_engine, saved_run


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> Plane:
    fake = Plane()
    monkeypatch.setattr(sai, "TestEngine", lambda: fake.engine)
    monkeypatch.setattr(sai, "TestRun", fake.run)
    return fake


def home_with(home, *, claude2: bool = True, codex2: bool = True):
    """A fake HOME carrying only the presence markers discovery looks for."""
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "antigravity").mkdir(parents=True, exist_ok=True)
    for name in (".claude", ".claude-account2"):
        if name == ".claude-account2" and not claude2:
            continue
        (home / name).mkdir(parents=True, exist_ok=True)
        (home / name / ".credentials.json").write_text("{}", encoding="utf-8")
    for name in (".codex", ".codex-account2", ".codex-account3free"):
        if name != ".codex" and not codex2:
            continue
        (home / name).mkdir(parents=True, exist_ok=True)
        (home / name / "auth.json").write_text("{}", encoding="utf-8")
    return home


def account(account_id: str, provider: str, label: str, *, locator: str = "",
            compact: str = "", backend: str = "profile_directory",
            state: str = "ENABLED") -> dict:
    meta: dict = {}
    if backend == "profile_directory":
        meta["profile_locator"] = locator
    elif locator:
        meta["windows_user"] = locator
    return {"account_id": account_id, "provider_id": provider, "display_name": label,
            "compact_label": compact or label, "execution_backend": backend,
            "execution_context_id": "current-user", "operational_state": state,
            "hidden": False, "context_label": None, "provider_metadata": meta}


def profile(home, name: str) -> str:
    """The plane's locator for a profile directory, in AUDAPACK's own vocabulary."""
    return identity_locator("codex" if "codex" in name else "claude",
                            home / name, home)


def ids(config: AppConfig) -> list[str]:
    return [lc.id for lc in config.launchers]


def by_target(config: AppConfig, account_id: str) -> LauncherConfig | None:
    return next((lc for lc in config.launchers if lc.id == target_id(account_id)), None)


# ── RED CONTROLS (the split-brain the source receipt demonstrates) ───────────


class TestRedControls:
    def test_third_claude_account_appears_in_the_inventory(
            self, plane: Plane, tmp_path) -> None:
        """1. The plane reports a THIRD Claude account.

        The old list was fixed at Claude 1/Claude 2, so a third canonical
        account could not be represented at all.
        """
        home = home_with(tmp_path)
        plane.answer_list(
            account("claude:profile-dir:aaa", "claude", "Claude 1",
                    locator=profile(home, ".claude")),
            account("claude:profile-dir:bbb", "claude", "Claude 2",
                    locator=profile(home, ".claude-account2")),
            account("claude:profile-dir:ccc", "claude", "Claude 3",
                    locator=r"c:\somewhere\.claude-account3"),
        )
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        assert "sai:claude:profile-dir:ccc" in ids(config)
        assert len([i for i in ids(config) if "claude" in i]) == 3

    def test_removed_claude_account_stops_being_launchable(
            self, plane: Plane, tmp_path) -> None:
        """2. The plane removes Claude 2.

        The static Claude 2 stayed launchable forever because nothing tied it
        to the canonical account's existence.
        """
        home = home_with(tmp_path)
        claude1 = account("claude:profile-dir:aaa", "claude", "Claude 1",
                          locator=profile(home, ".claude"))
        claude2 = account("claude:profile-dir:bbb", "claude", "Claude 2",
                          locator=profile(home, ".claude-account2"))
        plane.answer_list(claude1, claude2)
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        assert by_target(config, "claude:profile-dir:bbb") is not None

        plane.answer_list(claude1)
        sync_effective_launchers(config, home=home)
        assert by_target(config, "claude:profile-dir:bbb") is None
        assert "claude2" not in ids(config)

    def test_four_codex_accounts_are_all_representable(
            self, plane: Plane, tmp_path) -> None:
        """3. The plane has FOUR Codex accounts.

        Three hard-coded Codex launchers could not name the fourth.
        """
        home = home_with(tmp_path)
        rows = [account("codex:profile-dir:0001", "codex", "Codex 1",
                        locator=profile(home, ".codex"))]
        for n in (2, 3, 4):
            rows.append(account(f"codex:profile-dir:{n:04d}", "codex", f"Codex {n}",
                                locator=rf"c:\somewhere\.codex-{n}"))
        plane.answer_list(*rows)
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        codex_targets = [i for i in ids(config) if account_id_of(i).startswith("codex:")]
        assert len(codex_targets) == 4

    def test_canonical_account_needs_no_manual_launcher_binding(
            self, plane: Plane, tmp_path) -> None:
        """4. A canonical account is executable but had ``launcher_ids=()``.

        Nothing could use it without the operator hand-binding it to one of the
        old static launchers.
        """
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig(projects=[])
        sync_effective_launchers(config, home=home)
        shared = [a for a in discover_accounts(config.launchers, home=home)
                  if a.discovery_source == sai.SHARED_SOURCE]
        assert shared, "the shared account is not in the registry at all"
        assert shared[0].launcher_ids == ("sai:claude:profile-dir:aaa",)
        assert shared[0].bound is True

    def test_a_canonical_account_resolves_as_a_prepared_target(
            self, plane: Plane, tmp_path) -> None:
        """The same account, through the launch authority Prepared uses."""
        from audapack.models import Project

        home = home_with(tmp_path)
        root = tmp_path / "proj"
        root.mkdir()
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig(projects=[Project(id="p1", display_name="P", source_path=str(root))])
        sync_effective_launchers(config, home=home)
        shared = [a for a in discover_accounts(config.launchers, home=home)
                  if a.discovery_source == sai.SHARED_SOURCE][0]
        target = resolve_launch_target(config, "p1", shared.launcher_ids[0],
                                       account=shared, prepared=True)
        assert target.launcher.id == "sai:claude:profile-dir:aaa"
        assert target.account is shared


# ── CONTRACT ────────────────────────────────────────────────────────────────


class TestAuthorityContract:
    def test_the_account_identity_table_has_exactly_one_home(self) -> None:
        """The defect is a SECOND table of account identities, so prove there is one.

        ``local_candidates`` is the only place that says "this legacy launcher id
        is this profile directory"; the reconciler derives its migration mapping
        from it instead of restating it. A copy appearing in a third module is
        the split brain coming back.
        """
        root = Path(__file__).resolve().parents[1] / "audapack"
        readers = sorted(
            str(p.relative_to(root)) for p in root.rglob("*.py")
            if "local_candidates" in p.read_text(encoding="utf-8"))
        assert readers == ["account_registry.py", "launch_federation.py"], readers
        reconciler = (root / "launch_federation.py").read_text(encoding="utf-8")
        for private in (".claude-account2", ".codex-account2", ".codex-account3free"):
            assert private not in reconciler

    def test_a_target_id_round_trips_and_never_collides_with_a_local_tool(self) -> None:
        assert target_id("claude:profile-dir:aaa") == "sai:claude:profile-dir:aaa"
        assert account_id_of("sai:claude:profile-dir:aaa") == "claude:profile-dir:aaa"
        assert is_account_target("sai:claude:profile-dir:aaa") is True
        for local in ("opencode", "freebuff", "cline", "zcode", "main_codex", ""):
            assert is_account_target(local) is False
            assert account_id_of(local) == ""

    def test_an_account_row_never_carries_a_command_template(
            self, plane: Plane, tmp_path) -> None:
        """The launch authority refuses a templated account target on purpose.

        The canonical account is selected by its own execution context, so an
        operator template here would be exactly the hand-typed identity mapping
        the registry exists to replace.
        """
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        row = by_target(config, "claude:profile-dir:aaa")
        assert row is not None and row.command_template == ""

    def test_local_tools_are_never_touched(self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        config.launchers[0].console_background_color = "Navy"
        config.launchers[1].max_instances = 7
        sync_effective_launchers(config, home=home)
        assert ids(config)[:3] == ["opencode", "freebuff", "cline"]
        assert "zcode" in ids(config)
        assert config.launchers[0].console_background_color == "Navy"
        assert config.launchers[1].max_instances == 7

    def test_no_secret_material_reaches_the_launcher_rows(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        blob = json.dumps([lc.to_dict() for lc in config.launchers]).casefold()
        for secret in ("oauth", "refresh_token", "private_key", "api_key", "password"):
            assert secret not in blob

    def test_the_provider_adapter_refuses_a_context_audapack_cannot_select(self) -> None:
        from audapack.cli_launchers import account_launch_resolution

        # An Antigravity account belongs to a Windows logon session. Launching
        # whichever profile is current would start the WRONG account, so the
        # honest answer is that there is no invocation at all.
        resolution = account_launch_resolution("antigravity", "windows_user", "someone-else")
        assert resolution.ok is False
        assert "SAI Accounts" in resolution.next_action

    def test_profile_directory_accounts_get_their_locator_as_the_context(self) -> None:
        from audapack.cli_launchers import account_launch_resolution

        resolution = account_launch_resolution(
            "codex", "profile_directory", r"c:\profiles\codex-two",
            exists=lambda _p: True, environ={"PATH": ""})
        assert resolution.ok is True
        assert "CODEX_HOME" in resolution.console_command
        assert r"c:\profiles\codex-two" in resolution.console_command

    def test_an_unknown_provider_has_no_invented_invocation(self) -> None:
        from audapack.cli_launchers import account_launch_resolution

        assert account_launch_resolution("gemini", "profile_directory", "/x").ok is False
        assert account_launch_resolution("claude", "", "/x").ok is False


# ── MIGRATION ───────────────────────────────────────────────────────────────


class TestLegacyMigration:
    def test_a_pristine_builtin_is_replaced_by_its_canonical_account(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            account("claude:profile-dir:aaa", "claude", "Claude 1",
                    locator=profile(home, ".claude")),
            account("claude:profile-dir:bbb", "claude", "Claude 2",
                    locator=profile(home, ".claude-account2")))
        config = AppConfig()
        result = sync_effective_launchers(config, home=home)
        # Every account-backed static row is retired, whether or not the plane
        # proves an account for it: a Codex built-in the registry does not know is
        # exactly the second account truth this change removes.
        assert set(result.migrated) == set(ACCOUNT_BACKED_LEGACY_IDS)
        assert "claude1" not in ids(config) and "claude2" not in ids(config)
        # The retired row's SLOT is where its account is materialised, so the
        # operator's order survives the migration.
        assert ids(config)[3] == target_id("claude:profile-dir:aaa")
        assert ids(config)[4] == target_id("claude:profile-dir:bbb")

    def test_local_presentation_travels_with_the_account(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        donor = next(lc for lc in config.launchers if lc.id == "claude1")
        donor.enabled = False
        donor.short_label = "Q1"
        donor.console_background_color = "Navy"
        donor.console_foreground_color = "Gold"
        sync_effective_launchers(config, home=home)
        row = by_target(config, "claude:profile-dir:aaa")
        assert row.enabled is False          # local visibility, migrated
        assert row.short_label == "Q1"       # local override, migrated
        assert (row.console_background_color, row.console_foreground_color) == ("Navy", "Gold")
        assert row.name == "Claude 1"        # registry-owned, projected

    def test_a_customised_legacy_launcher_survives_as_a_local_custom(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:bbb", "claude", "Claude 2",
                                  locator=profile(home, ".claude-account2")))
        config = AppConfig()
        custom = next(lc for lc in config.launchers if lc.id == "claude2")
        custom.command_template = "my-wrapper.exe {workdir}"
        sync_effective_launchers(config, home=home)
        kept = next(lc for lc in config.launchers if lc.id == "claude2")
        assert kept.command_template == "my-wrapper.exe {workdir}"
        assert "claude2" not in sync_effective_launchers(config, home=home).migrated

    def test_no_duplicate_row_for_one_physical_account(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        assert len(ids(config)) == len(set(ids(config)))
        assert "claude1" not in ids(config)
        # A second pass over the same snapshot must not materialise it twice.
        sync_effective_launchers(config, home=home)
        assert len(ids(config)) == len(set(ids(config)))
        assert ids(config).count(target_id("claude:profile-dir:aaa")) == 1

    def test_the_double_click_launcher_follows_its_migrated_account(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        config.ui.project_double_click_action = "launcher"
        config.ui.project_double_click_launcher_id = "claude1"
        sync_effective_launchers(config, home=home)
        assert config.ui.project_double_click_launcher_id == \
            target_id("claude:profile-dir:aaa")


# ── REGRESSION MATRIX ───────────────────────────────────────────────────────


class TestRegression:
    def test_standalone_is_untouched(self, tmp_path) -> None:
        """No plane: the inventory is exactly what it always was."""
        config = AppConfig()
        before = ids(config)
        result = sync_effective_launchers(config, home=home_with(tmp_path))
        assert result.state == "standalone" and result.fresh is False
        assert ids(config) == before
        assert config.federation.active is False

    def test_a_broken_reply_is_not_an_empty_account_list(
            self, plane: Plane, tmp_path) -> None:
        plane.list_stdout = "{ this is not json"
        config = AppConfig()
        before = ids(config)
        result = sync_effective_launchers(config, home=home_with(tmp_path))
        assert result.state == "standalone" and result.fresh is False
        assert ids(config) == before

    def test_an_outage_keeps_the_last_known_good_and_never_resurrects_defaults(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        after_sync = ids(config)
        assert "claude1" not in after_sync

        plane.list_ok = False
        result = sync_effective_launchers(config, home=home)
        assert result.state == "stale" and result.fresh is False
        assert ids(config) == after_sync          # last-known-good, kept
        assert config.federation.stale is True
        assert "claude1" not in ids(config)       # the retired alias stays retired

    def test_a_withdrawn_account_disappears_from_the_inventory(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            account("claude:profile-dir:aaa", "claude", "Claude 1",
                    locator=profile(home, ".claude")),
            account("claude:profile-dir:bbb", "claude", "Claude 2",
                    locator=profile(home, ".claude-account2")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        assert by_target(config, "claude:profile-dir:bbb") is not None

        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        result = sync_effective_launchers(config, home=home)
        assert target_id("claude:profile-dir:bbb") in result.removed
        assert by_target(config, "claude:profile-dir:bbb") is None

    def test_a_globally_hidden_account_is_not_materialised(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        hidden = account("claude:profile-dir:bbb", "claude", "Claude 2",
                         locator=profile(home, ".claude-account2"))
        hidden["hidden"] = True
        plane.answer_list(
            account("claude:profile-dir:aaa", "claude", "Claude 1",
                    locator=profile(home, ".claude")),
            hidden)
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        assert "claude2" not in ids(config)
        assert by_target(config, "claude:profile-dir:bbb") is None

    def test_more_accounts_than_the_digit_row_are_not_clipped(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        rows = [account("codex:profile-dir:0001", "codex", "Codex 1",
                        locator=profile(home, ".codex"))]
        for n in range(2, 13):
            rows.append(account(f"codex:profile-dir:{n:04d}", "codex", f"Codex {n}",
                                locator=rf"c:\elsewhere\.codex-{n}"))
        plane.answer_list(*rows)
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        targets = [i for i in ids(config) if account_id_of(i).startswith("codex:")]
        assert len(targets) == 12
        assert len(ids(config)) == len(set(ids(config)))

    def test_a_renamed_account_reaches_the_button_but_keeps_the_local_label(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        locator = profile(home, ".claude")
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=locator, compact="A1"))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        row = by_target(config, "claude:profile-dir:aaa")
        assert row.short_label == "A1"
        row.short_label = "MINE"

        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude Pro",
                                  locator=locator, compact="A1"))
        sync_effective_launchers(config, home=home)
        row = by_target(config, "claude:profile-dir:aaa")
        assert row.name == "Claude Pro"     # the registry owns the name
        assert row.short_label == "MINE"    # the operator owns the label

    def test_colliding_compact_labels_do_not_merge_identities(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            account("codex:profile-dir:0001", "codex", "Codex 1",
                    locator=profile(home, ".codex"), compact="C1"),
            account("codex:profile-dir:0002", "codex", "Codex Two",
                    locator=r"c:\elsewhere\.codex-2", compact="C1"))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        rows = [lc for lc in config.launchers if is_account_target(lc.id)]
        assert len(rows) == 2
        assert len({lc.short_label for lc in rows}) == 2
        assert {lc.id for lc in rows} == {
            target_id("codex:profile-dir:0001"), target_id("codex:profile-dir:0002")}

    def test_account_added_while_running_appears_on_the_next_refresh(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        assert by_target(config, "claude:profile-dir:ccc") is None

        plane.answer_list(
            account("claude:profile-dir:aaa", "claude", "Claude 1",
                    locator=profile(home, ".claude")),
            account("claude:profile-dir:ccc", "claude", "Claude 3",
                    locator=r"c:\elsewhere\.claude-3"))
        result = sync_effective_launchers(config, home=home)
        assert target_id("claude:profile-dir:ccc") in result.added
        assert by_target(config, "claude:profile-dir:ccc") is not None

    def test_the_same_display_name_with_two_identities_stays_two_rows(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            account("codex:profile-dir:0001", "codex", "Codex", locator=profile(home, ".codex")),
            account("codex:profile-dir:0002", "codex", "Codex",
                    locator=r"c:\elsewhere\.codex-2"))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        rows = [lc for lc in config.launchers if is_account_target(lc.id)]
        assert len(rows) == 2
        assert len({lc.id for lc in rows}) == 2

    def test_discovery_prefers_the_bound_projection_of_one_identity(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(account("claude:profile-dir:aaa", "claude", "Claude 1",
                                  locator=profile(home, ".claude")))
        config = AppConfig()
        sync_effective_launchers(config, home=home)
        found = [a for a in discover_accounts(config.launchers, home=home)
                 if a.provider_id == "claude"]
        # ``home_with`` carries a second Claude directory, so there are two real
        # Claude identities. The one the plane and the local marker BOTH prove
        # must be ONE record -- the bound canonical projection -- and never a
        # local residue beside it.
        here = [a for a in found
                if a.provider_id == "claude"
                and Path(a.profile_locator).name == ".claude"]
        assert len(here) == 1
        assert here[0].shared is True and here[0].launchable is True
        assert len(found) == 2

    def test_an_account_out_of_service_is_not_launchable(self) -> None:
        from audapack.account_registry import AccountIdentity

        row = AccountIdentity(
            account_id="claude:shared:x", provider_id="claude", display_name="C",
            profile_locator="x", launcher_ids=("sai:claude:profile-dir:x",),
            discovery_source=sai.SHARED_SOURCE, last_seen_at="", enabled=True,
            operational_state="ARCHIVED")
        assert row.bound is True and row.operational is False and row.launchable is False

    def test_the_federation_flag_round_trips_through_the_config(self) -> None:
        from audapack.config import SCHEMA_VERSION, migrate_legacy_data

        config = AppConfig()
        config.federation.active = True
        config.federation.generation = 12
        config.federation.last_sync = "2026-10-05T00:00:00+00:00"
        restored = migrate_legacy_data(config.to_dict())
        assert restored.federation.active is True
        assert restored.federation.generation == 12
        assert restored.federation.last_sync == "2026-10-05T00:00:00+00:00"
        assert SCHEMA_VERSION >= 8

    def test_an_old_config_gains_no_launcher_rows_from_the_new_schema(self) -> None:
        """Schema 8 is additive. Retiring the static account rows is a property
        of an authoritative snapshot having been read, not of the file being old
        -- so loading an old config must not silently delete anything either."""
        from audapack.config import create_default_launchers, migrate_legacy_data

        old = {
            "schema_version": 7,
            "launchers": [lc.to_dict() for lc in create_default_launchers()],
        }
        restored = migrate_legacy_data(old)
        assert [lc.id for lc in restored.launchers] == [lc.id for lc in create_default_launchers()]
        assert restored.federation.active is False


# ── T-279 CONTROL-PLANE LAUNCH CONTRACT ─────────────────────────────────────


class _StatusStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, text: str) -> None:
        self.messages.append(str(text))

    def currentMessage(self) -> str:
        return self.messages[-1] if self.messages else ""


class _WindowBackendStub:
    def __init__(self) -> None:
        self.windows: list = []
        self.alive: set[int] = set()
        self.focused: list[int] = []

    def list_windows(self) -> list:
        return list(self.windows)

    def focus_window(self, hwnd: int) -> bool:
        self.focused.append(int(hwnd))
        return True

    def process_alive(self, pid: int) -> bool:
        return int(pid) in self.alive

    def process_token(self, pid: int) -> int:
        return int(pid) * 1000 if int(pid) in self.alive else 0


class _InlineTaskRunner:
    """Deterministic stand-in for :class:`TaskRunner`.

    T-284 moved the blocking ``sai_accounts.launch`` call off the GUI thread and
    into the shared TaskRunner. This suite tests the control-plane *contract*
    (identity, envelope validation, registration, fail-closed), not the
    threading, which has its own suite. So it runs the submitted work inline on
    this thread and forwards the verdict straight to the completion seam -- the
    same order of operations the real runner produces on the GUI thread, with no
    event loop and no thread pool.
    """

    def __init__(self) -> None:
        self._running: set[str] = set()

    def is_running(self, key: str) -> bool:
        return key in self._running

    def submit(self, key, func, *, on_success=None, on_error=None) -> bool:
        self._running.add(key)
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001 - mirrors TaskRunner's contained error
            if on_error is not None:
                on_error(exc)
            return True
        finally:
            self._running.discard(key)
        if on_success is not None:
            on_success(result)
        return True


class _ProjectRoomHarness:
    """Binds MainWindow's real Project Room launch dispatch methods to an isolated config."""

    def __init__(self, config: AppConfig, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from audapack.instances import InstanceMonitor

        config.ui.auto_copy_gg_on_launch = False
        self._service = SimpleNamespace(
            config=config,
            list_projects=lambda: list(config.projects),
            get_project=lambda pid: next(
                (item for item in config.projects if item.id == pid), None
            ),
        )
        self.task_runner = _InlineTaskRunner()
        self._audit_service = SimpleNamespace(get_preferred_audit_file_path=lambda _id: None)
        self.backend = _WindowBackendStub()
        self._instance_monitor = InstanceMonitor(
            backend=self.backend, record_path=tmp_path / "launch_records.json"
        )
        self._title_guardian = SimpleNamespace(
            active_count=0,
            register=lambda *_a, **_k: None,
        )
        self._title_guardian_timer = None
        self._status = _StatusStub()
        self.instance_manager_shown: list[str] = []
        self._on_open_with_opencode = lambda *_a, **_k: None
        self._on_open_with_freebuff = lambda *_a, **_k: None
        self._on_open_with_cline = lambda *_a, **_k: None
        self._on_open_with_codex = lambda *_a, **_k: None

    def _sync_title_guardian_timer(self) -> None:
        return None

    def statusBar(self) -> _StatusStub:
        return self._status

    def request_instance_refresh(self, _reason: str = "") -> int:
        snap = self._instance_monitor.scan(
            self._service.list_projects(), self._service.config.launchers
        )
        self._instance_monitor.apply_snapshot(snap)
        return 1

    def _refresh_instances_after_launch(self) -> None:
        self.request_instance_refresh("post-launch")

    def _show_instance_manager(self, project=None) -> None:
        if project is not None:
            self.instance_manager_shown.append(project.id)

    def click(self, project, launcher_id: str, *, force_new: bool = False) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._continue_launcher_dispatch(self, project, launcher_id, force_new, None)

    def _focus_existing_launcher_instance(self, target, launcher_id: str, *, saipen_binding=None) -> bool:
        from audapack.ui_qt.main_window import MainWindow

        return MainWindow._focus_existing_launcher_instance(
            self, target, launcher_id, saipen_binding=saipen_binding
        )

    def _launcher_config(self, launcher_id: str):
        from audapack.ui_qt.main_window import MainWindow

        return MainWindow._launcher_config(self, launcher_id)

    def _launcher_block_reason(self, launcher_id: str) -> str:
        from audapack.ui_qt.main_window import MainWindow

        return MainWindow._launcher_block_reason(self, launcher_id)

    def _launch_cli_launcher(self, target, launcher_id: str) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._launch_cli_launcher(self, target, launcher_id)

    def _launch_plane_account(self, cfg, target, launch_target, resolution) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._launch_plane_account(self, cfg, target, launch_target, resolution)

    def _apply_plane_launch_verdict(self, verdict, cfg, target, resolution) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._apply_plane_launch_verdict(self, verdict, cfg, target, resolution)

    def _report_plane_launch_failure(self, cfg, target, resolution, reason, detail) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._report_plane_launch_failure(
            self, cfg, target, resolution, reason, detail
        )

    def _report_cli_launch_unavailable(self, cfg, target, resolution) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._report_cli_launch_unavailable(self, cfg, target, resolution)

    def _register_agent_launch(self, process, launcher_id: str, project, **kwargs) -> None:
        from audapack.ui_qt.main_window import MainWindow

        MainWindow._register_agent_launch(self, process, launcher_id, project, **kwargs)

    def _sanitize_diagnostic(self, text: str, limit: int = 160) -> str:
        from audapack.ui_qt.main_window import MainWindow

        return MainWindow._sanitize_diagnostic(text, limit)


class TestControlPlaneLaunchContract:
    """T-279: windows_user and managed_windows_user Antigravity accounts launch
    exclusively through ``sai-accounts launch <account_id> --workdir <project_root>``."""

    def test_a_windows_user_antigravity_account_delegates_through_control_plane(
            self, plane: Plane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A. windows_user Antigravity account delegates through the control plane."""
        import audapack.ui_qt.main_window as mw
        from audapack.models import Project

        home = home_with(tmp_path)
        proj_root = tmp_path / "proj_alpha"
        proj_root.mkdir()
        plane.answer_list(
            account("antigravity:windows-user:AAA", "antigravity", "Antigravity 1",
                    locator="vac34", compact="AG1", backend="windows_user"),
        )
        proj = Project(id="p1", display_name="Alpha", source_path=str(proj_root))
        config = AppConfig(projects=[proj])
        sync_effective_launchers(config, home=home)

        row = by_target(config, "antigravity:windows-user:AAA")
        assert row is not None
        assert row.resolved_stage == "plane_launch"
        assert row.resolved_command == ""

        spawned_direct: list = []
        monkeypatch.setattr(mw, "create_agent_process",
                            lambda *a, **k: spawned_direct.append((a, k)))
        monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)

        room = _ProjectRoomHarness(config, tmp_path)
        plane.launch_pid = 5101
        room.backend.alive.add(5101)
        room.click(proj, row.id)

        launch_calls = [c for c in plane.calls if c and c[0] == "launch"]
        assert launch_calls == [[
            "launch",
            "antigravity:windows-user:AAA",
            "--workdir",
            str(proj_root.resolve()),
        ]]
        assert spawned_direct == []
        assert "✓ Launched Antigravity 1 for Alpha" in room.statusBar().currentMessage()

    def test_b_managed_windows_user_antigravity_account_delegates_through_control_plane(
            self, plane: Plane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """B. managed_windows_user does the same."""
        import audapack.ui_qt.main_window as mw
        from audapack.models import Project

        home = home_with(tmp_path)
        proj_root = tmp_path / "proj_beta"
        proj_root.mkdir()
        plane.answer_list(
            account("antigravity:windows-user:BBB", "antigravity", "Antigravity 2",
                    locator="antigravity_b", compact="AG2", backend="managed_windows_user"),
        )
        proj = Project(id="p1", display_name="Beta", source_path=str(proj_root))
        config = AppConfig(projects=[proj])
        sync_effective_launchers(config, home=home)

        row = by_target(config, "antigravity:windows-user:BBB")
        assert row is not None
        assert row.resolved_stage == "plane_launch"
        assert row.resolved_command == ""

        spawned_direct: list = []
        monkeypatch.setattr(mw, "create_agent_process",
                            lambda *a, **k: spawned_direct.append((a, k)))
        monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)

        room = _ProjectRoomHarness(config, tmp_path)
        plane.launch_pid = 5202
        plane.launch_execution = "broker"
        room.backend.alive.add(5202)
        room.click(proj, row.id)

        launch_calls = [c for c in plane.calls if c and c[0] == "launch"]
        assert launch_calls == [[
            "launch",
            "antigravity:windows-user:BBB",
            "--workdir",
            str(proj_root.resolve()),
        ]]
        assert spawned_direct == []

    def test_c_windows_context_accounts_never_use_profile_directory_selectors(
            self, plane: Plane) -> None:
        """C. Never uses a profile-directory selector for windows_user / managed_windows_user."""
        from audapack.cli_launchers import account_launch_resolution

        for backend, acct in (
            ("windows_user", "antigravity:windows-user:AAA"),
            ("managed_windows_user", "antigravity:windows-user:BBB"),
        ):
            res = account_launch_resolution(
                "antigravity", backend, "some_user", account_id=acct,
            )
            assert res.ok is True
            assert res.is_plane_launch is True
            assert res.account_id == acct
            assert res.console_command == ""
            for selector in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTIGRAVITY_EXECUTABLE_DATA_DIR"):
                assert selector not in res.console_command
                assert selector not in res.profile

    def test_d_never_launches_generic_agy_or_antigravity_directly_for_canonical_account(
            self, plane: Plane, tmp_path: Path) -> None:
        """D. Never launches generic agy.exe / antigravity.exe directly for a canonical windows_user account."""
        from audapack.cli_launchers import resolve_cli_launcher

        fake_agy = tmp_path / "agy.exe"
        fake_agy.write_text("echo", encoding="utf-8")
        plane.answer_list(
            account("antigravity:windows-user:AAA", "antigravity", "Antigravity 1",
                    locator="vac34", compact="AG1", backend="windows_user"),
        )
        config = AppConfig()
        sync_effective_launchers(config, home=home_with(tmp_path))
        row = by_target(config, "antigravity:windows-user:AAA")
        assert row is not None

        res = resolve_cli_launcher(
            row,
            project_root=str(tmp_path),
            project_name="P",
            environ={"PATH": str(tmp_path)},
            exists=lambda _p: True,
        )
        assert res.ok is True
        assert res.is_plane_launch is True
        assert res.console_command == ""
        assert "agy.exe" not in res.console_command.casefold()
        assert "antigravity.exe" not in res.console_command.casefold()

        # Even if plane goes missing later, it fails closed instead of falling back to agy.exe.
        plane.engine = ""
        res_missing = resolve_cli_launcher(
            row,
            project_root=str(tmp_path),
            project_name="P",
            environ={"PATH": str(tmp_path)},
            exists=lambda _p: True,
        )
        assert res_missing.ok is False
        assert res_missing.console_command == ""

    def test_e_account_a_cannot_launch_account_b(
            self, plane: Plane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """E. Account A cannot launch Account B; mismatched plane reply is rejected."""
        import audapack.ui_qt.main_window as mw
        from audapack.models import Project

        home = home_with(tmp_path)
        proj_root = tmp_path / "proj"
        proj_root.mkdir()
        plane.answer_list(
            account("antigravity:windows-user:AAA", "antigravity", "Antigravity 1",
                    locator="vac34", compact="AG1", backend="windows_user"),
            account("antigravity:windows-user:BBB", "antigravity", "Antigravity 2",
                    locator="antigravity_b", compact="AG2", backend="managed_windows_user"),
            account("antigravity:windows-user:CCC", "antigravity", "AG3",
                    locator="SAIAG_9fabb0d5", compact="AG3", backend="managed_windows_user"),
        )
        proj = Project(id="p1", display_name="P", source_path=str(proj_root))
        config = AppConfig(projects=[proj])
        sync_effective_launchers(config, home=home)
        monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)

        room = _ProjectRoomHarness(config, tmp_path)
        plane.launch_pid = 6001
        room.backend.alive.add(6001)
        room.click(proj, target_id("antigravity:windows-user:AAA"))
        plane.launch_pid = 6002
        room.backend.alive.add(6002)
        room.click(proj, target_id("antigravity:windows-user:BBB"))
        plane.launch_pid = 6003
        room.backend.alive.add(6003)
        room.click(proj, target_id("antigravity:windows-user:CCC"))

        launch_calls = [c for c in plane.calls if c and c[0] == "launch"]
        assert [c[1] for c in launch_calls] == [
            "antigravity:windows-user:AAA",
            "antigravity:windows-user:BBB",
            "antigravity:windows-user:CCC",
        ]

        # If the plane ever claims to have launched a DIFFERENT account_id, fail closed.
        plane.launch_stdout = json.dumps({
            "schema": "sai.accounts/launch/1",
            "status": "launched",
            "account_id": "antigravity:windows-user:BBB",
            "provider_id": "antigravity",
            "execution": "current_user",
            "workdir": str(proj_root.resolve()),
            "pid": 6999,
        })
        verdict = sai.launch("antigravity:windows-user:AAA", proj_root)
        assert verdict.ok is False
        assert verdict.pid == 0

    def test_f_workdir_is_exact_resolved_project_root_with_spaces_and_unicode(
            self, plane: Plane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """F. Workdir is the exact resolved project root passed as argv list element."""
        import audapack.ui_qt.main_window as mw
        from audapack.models import Project

        home = home_with(tmp_path)
        spaced_unicode_root = tmp_path / "Töölaud & Projektid (Üäöõ)" / "alam kaust"
        spaced_unicode_root.mkdir(parents=True)
        plane.answer_list(
            account("antigravity:windows-user:AAA", "antigravity", "Antigravity 1",
                    locator="vac34", compact="AG1", backend="windows_user"),
        )
        proj = Project(id="p_uni", display_name="Üäöõ", source_path=str(spaced_unicode_root))
        config = AppConfig(projects=[proj])
        sync_effective_launchers(config, home=home)
        monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)

        room = _ProjectRoomHarness(config, tmp_path)
        plane.launch_pid = 6100
        room.backend.alive.add(6100)
        room.click(proj, target_id("antigravity:windows-user:AAA"))

        launch_calls = [c for c in plane.calls if c and c[0] == "launch"]
        assert len(launch_calls) == 1
        argv = launch_calls[0]
        assert isinstance(argv, list)
        assert argv == [
            "launch",
            "antigravity:windows-user:AAA",
            "--workdir",
            str(spaced_unicode_root.resolve()),
        ]

    def test_g_unsupported_provider_or_backend_remains_unavailable(
            self, plane: Plane) -> None:
        """G. A provider/backend with no supported control-plane launch contract remains UNAVAILABLE."""
        from audapack.cli_launchers import account_launch_resolution

        # claude on windows_user is not supported by the control plane
        res_claude_win = account_launch_resolution(
            "claude", "windows_user", "vac34", account_id="claude:windows-user:111",
        )
        assert res_claude_win.ok is False
        assert res_claude_win.stage == "unresolved"
        assert "SAI Accounts" in res_claude_win.next_action

        # unknown provider on windows_user is not supported
        res_gemini = account_launch_resolution(
            "gemini", "windows_user", "vac34", account_id="gemini:windows-user:111",
        )
        assert res_gemini.ok is False
        assert res_gemini.stage == "unresolved"

        # antigravity on unsupported backend is not supported
        res_cloud = account_launch_resolution(
            "antigravity", "cloud_token", "tok", account_id="antigravity:cloud:111",
        )
        assert res_cloud.ok is False
        assert res_cloud.stage == "unresolved"

        # plane absent -> antigravity windows_user is UNAVAILABLE
        plane.engine = ""
        res_no_plane = account_launch_resolution(
            "antigravity", "windows_user", "vac34", account_id="antigravity:windows-user:AAA",
        )
        assert res_no_plane.ok is False
        assert res_no_plane.stage == "unresolved"
        assert "SAI Accounts" in res_no_plane.next_action

    def test_h_failed_plane_launch_fails_closed_without_fallback_or_sid_leak(
            self, plane: Plane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """H. A failed plane launch does not fall back to another account or generic Antigravity."""
        import audapack.ui_qt.main_window as mw
        from audapack.models import Project

        home = home_with(tmp_path)
        proj_root = tmp_path / "proj"
        proj_root.mkdir()
        plane.answer_list(
            account("antigravity:windows-user:BBB", "antigravity", "Antigravity 2",
                    locator="antigravity_b", compact="AG2", backend="managed_windows_user"),
        )
        proj = Project(id="p1", display_name="P", source_path=str(proj_root))
        config = AppConfig(projects=[proj])
        sync_effective_launchers(config, home=home)

        spawned_direct: list = []
        monkeypatch.setattr(mw, "create_agent_process",
                            lambda *a, **k: spawned_direct.append((a, k)))
        monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)

        plane.launch_ok = False
        plane.launch_stdout = json.dumps({
            "schema": "sai.accounts/launch/1",
            "status": "error",
            "account_id": "antigravity:windows-user:BBB",
            "error": {
                "code": "CONTEXT_OFFLINE",
                "message": "Broker context S-1-5-21-3858504637-2055436529-1444068757-1002 is offline",
            },
            "broker_context": "S-1-5-21-3858504637-2055436529-1444068757-1002",
        })

        room = _ProjectRoomHarness(config, tmp_path)
        room.click(proj, target_id("antigravity:windows-user:BBB"))

        assert spawned_direct == []
        assert room._instance_monitor.instances == []
        msg = room.statusBar().currentMessage()
        assert "Antigravity 2 unavailable for P" in msg
        assert "CONTEXT_OFFLINE" in msg
        assert "S-1-5-21-" not in msg

        # Also test broker status/reason envelope (e.g. WORKDIR_UNAVAILABLE / ACCESS_DENIED)
        plane.launch_stdout = json.dumps({
            "schema": "sai.accounts/launch/1",
            "account_id": "antigravity:windows-user:BBB",
            "workdir": str(proj_root),
            "broker_context": "S-1-5-21-3858504637-2055436529-1444068757-1002",
            "status": "WORKDIR_UNAVAILABLE",
            "reason": "ACCESS_DENIED for S-1-5-21-3858504637-2055436529-1444068757-1002",
            "execution": "broker",
        })
        room.click(proj, target_id("antigravity:windows-user:BBB"))
        assert spawned_direct == []
        assert room._instance_monitor.instances == []
        msg2 = room.statusBar().currentMessage()
        assert "WORKDIR_UNAVAILABLE" in msg2
        assert "ACCESS_DENIED" in msg2
        assert "S-1-5-21-" not in msg2

    def test_i_instance_attribution_correlates_plane_pid_and_reuses_exact_account(
            self, plane: Plane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """I. Successful launch attribution uses sai:<account_id>, correlates agy.exe window,
        reuses only that exact account on second normal click, and spawns new on Shift+click."""
        import audapack.ui_qt.main_window as mw
        from audapack.instances import NativeWindow
        from audapack.models import Project

        home = home_with(tmp_path)
        proj_root = tmp_path / "proj"
        proj_root.mkdir()
        plane.answer_list(
            account("antigravity:windows-user:AAA", "antigravity", "Antigravity 1",
                    locator="vac34", compact="AG1", backend="windows_user"),
            account("antigravity:windows-user:BBB", "antigravity", "Antigravity 2",
                    locator="antigravity_b", compact="AG2", backend="managed_windows_user"),
        )
        proj = Project(id="p1", display_name="P", source_path=str(proj_root))
        config = AppConfig(projects=[proj])
        sync_effective_launchers(config, home=home)
        monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)

        room = _ProjectRoomHarness(config, tmp_path)
        ag1_id = target_id("antigravity:windows-user:AAA")
        ag2_id = target_id("antigravity:windows-user:BBB")

        # Launch AG1 -> returns PID 7101; native window appears with binary path in command_line
        plane.launch_pid = 7101
        room.backend.alive.add(7101)
        room.backend.windows = [
            NativeWindow(
                hwnd=9001,
                pid=7101,
                title=r"C:\ProgramData\SAI\Accounts\bin\agy.exe",
                process_name="agy.exe",
                command_line=r'"C:\ProgramData\SAI\Accounts\bin\agy.exe" --dangerously-skip-permissions',
            )
        ]
        room.click(proj, ag1_id)

        instances = room._instance_monitor.for_project_launcher("p1", ag1_id)
        assert len(instances) == 1
        assert instances[0].launcher_id == "sai:antigravity:windows-user:AAA"
        assert instances[0].pid == 7101
        assert instances[0].hwnd == 9001
        assert instances[0].state == "running"
        assert room._instance_monitor.for_project_launcher("p1", ag2_id) == []

        # Second normal click on AG1 focuses existing window 9001 without a second launch call
        calls_before = len([c for c in plane.calls if c and c[0] == "launch"])
        room.click(proj, ag1_id, force_new=False)
        assert len([c for c in plane.calls if c and c[0] == "launch"]) == calls_before
        assert room.backend.focused == [9001]
        assert "Focused Antigravity 1 for P (PID 7101)" in room.statusBar().currentMessage()

        # Normal click on AG2 does NOT focus AG1; it launches AG2 via the plane
        plane.launch_pid = 7202
        plane.launch_execution = "broker"
        room.backend.alive.add(7202)
        room.click(proj, ag2_id, force_new=False)
        launch_calls = [c for c in plane.calls if c and c[0] == "launch"]
        assert launch_calls[-1][1] == "antigravity:windows-user:BBB"

        # Second normal click on AG2 (brokered, hwnd=0, process alive) reuses/reports running without spawning duplicate
        calls_before_ag2 = len([c for c in plane.calls if c and c[0] == "launch"])
        room.click(proj, ag2_id, force_new=False)
        assert len([c for c in plane.calls if c and c[0] == "launch"]) == calls_before_ag2

        # Shift+click (force_new=True) on AG1 spawns a second AG1 instance
        plane.launch_pid = 7103
        room.backend.alive.add(7103)
        room.click(proj, ag1_id, force_new=True)
        launch_calls = [c for c in plane.calls if c and c[0] == "launch"]
        assert launch_calls[-1][1] == "antigravity:windows-user:AAA"


# ── T-278 SETTINGS LAUNCHERS FEDERATED INVENTORY SPLIT & ROUTING ────────────


class TestFederationSettingsView:
    def test_launchers_tab_splits_local_tools_and_sai_accounts(self, qapp, tmp_path) -> None:
        from PySide6.QtCore import Qt

        from audapack.config import AppConfig, LauncherConfig
        from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget

        config = AppConfig(launchers=[
            LauncherConfig(id="opencode", name="OpenCode", short_label="OC"),
            LauncherConfig(id="freebuff", name="FreeBuff", short_label="FB"),
            LauncherConfig(id="sai:claude:profile-dir:aaa", name="Claude 1", short_label="C1", enabled=True),
        ])
        w = SettingsWidget(config)
        w._base_dir = tmp_path

        # Distinct QGroupBox sections
        assert w.grp_local.title() == "LOCAL TOOLS"
        assert w.grp_sai.title() == "SAI ACCOUNTS"

        # Correct row split
        assert w.local_launcher_list.count() == 2
        assert w.sai_launcher_list.count() == 1

        local_texts = [w.local_launcher_list.item(i).text() for i in range(w.local_launcher_list.count())]
        assert any("OpenCode" in t for t in local_texts)
        assert any("FreeBuff" in t for t in local_texts)

        sai_item = w.sai_launcher_list.item(0)
        assert "Claude 1" in sai_item.text()
        assert "managed by SAI Accounts" in sai_item.text()
        assert sai_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert sai_item.checkState() == Qt.CheckState.Checked

        # Local enable toggle works on SAI accounts without mutating identity
        sai_item.setCheckState(Qt.CheckState.Unchecked)
        sai_row = next(lc for lc in config.launchers if lc.id == "sai:claude:profile-dir:aaa")
        assert sai_row.enabled is False
        assert sai_row.name == "Claude 1"
        assert sai_row.id == "sai:claude:profile-dir:aaa"

    def test_sai_manager_routing_and_path_resolution(self, qapp, monkeypatch, tmp_path) -> None:
        from audapack import sai_accounts
        from audapack.config import AppConfig, LauncherConfig
        from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget

        # 1. Path resolution
        monkeypatch.setattr(sai_accounts, "TestManager", lambda: "V:/mock/SAITULS.exe")
        assert sai_accounts.manager_path() == "V:/mock/SAITULS.exe"

        # 2. Open manager invokes canonical seam
        opened = []
        monkeypatch.setattr(sai_accounts, "TestOpenManager", lambda action="": (opened.append(action), {"ok": True, "error": ""})[1])

        config = AppConfig(launchers=[
            LauncherConfig(id="sai:codex:profile-dir:c1", name="Codex 1", short_label="C1"),
        ])
        w = SettingsWidget(config)
        w._base_dir = tmp_path

        w.btn_add_sai.click()
        assert len(opened) == 1

        w.btn_remove_sai.click()
        assert len(opened) == 2

        w.btn_manage_sai.click()
        assert len(opened) == 3

    def test_federation_status_display_fresh_stale_standalone(self, qapp, tmp_path) -> None:
        from audapack.config import AppConfig
        from audapack.launch_federation import FederationSync
        from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget

        # Standalone
        cfg_standalone = AppConfig()
        cfg_standalone.federation.active = False
        w_standalone = SettingsWidget(cfg_standalone)
        w_standalone._base_dir = tmp_path
        assert "STANDALONE" in w_standalone.lbl_federation_status.text()

        # Fresh
        cfg_fresh = AppConfig()
        cfg_fresh.federation.active = True
        cfg_fresh.federation.stale = False
        cfg_fresh.federation.last_sync = "2026-10-08T04:00:00+00:00"
        w_fresh = SettingsWidget(cfg_fresh)
        w_fresh._base_dir = tmp_path
        assert "FRESH" in w_fresh.lbl_federation_status.text()

        # Stale / Control Plane Offline
        cfg_stale = AppConfig()
        cfg_stale.federation.active = True
        cfg_stale.federation.stale = True
        cfg_stale.federation.last_sync = "2026-10-08T03:00:00+00:00"
        w_stale = SettingsWidget(cfg_stale)
        w_stale._base_dir = tmp_path
        status_text = w_stale.lbl_federation_status.text()
        assert "STALE" in status_text and "CONTROL PLANE OFFLINE" in status_text

        # update_federation_state with stale result does NOT mutate launcher projection
        initial_launchers = list(cfg_stale.launchers)
        w_stale.update_federation_state(FederationSync(state="stale", fresh=False, generation=1))
        assert [lc.id for lc in cfg_stale.launchers] == [lc.id for lc in initial_launchers]
        assert "STALE" in w_stale.lbl_federation_status.text()

    def test_single_refresh_owner_coalesces_on_task_runner(self) -> None:
        import time

        from audapack.ui_qt.task_runner import TaskRunner

        runner = TaskRunner(max_threads=2)
        runs = []

        def worker():
            runs.append(time.monotonic())
            time.sleep(0.05)
            return "ok"

        # Submit multiple concurrent refreshes under the same key "federation:refresh"
        runner.submit_coalesced("federation:refresh", worker)
        runner.submit_coalesced("federation:refresh", worker)
        runner.submit_coalesced("federation:refresh", worker)

        # Let the task settle
        runner.close()

        # Must have coalesced to at most 2 runs (1 in flight + at most 1 follow-up)
        assert len(runs) <= 2
