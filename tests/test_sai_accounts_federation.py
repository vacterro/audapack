"""SAI Accounts federation — the OPTIONAL control plane.

Every case is deterministic: no real engine, no real vendor, no registry write,
no credential read. The plane is faked through the two seams the module exposes
(``sai_accounts.TestEngine`` / ``TestRun``), so the suite proves the CONTRACT
rather than the machine it happens to run on.

The contract in one sentence: SAI Accounts is federation, not captivity. AUDAPACK
is fully usable with the plane absent, adds shared accounts when it is present,
keeps local-only accounts either way, merges only on PROVEN identity, and
survives the plane going away mid-life.

Every assertion drives the production function it names. Nothing here
re-implements the merge: a test of a copy proves the copy.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import dataclass

import pytest

from audapack import sai_accounts as sai
from audapack.account_registry import AccountIdentity, discover_accounts, identity_locator
from audapack.limit_adapters import AntigravityLimitAdapter

ENGINE = r"C:\fake\sai-accounts.exe"


# ── fakes ───────────────────────────────────────────────────────────────────

class Plane:
    """A fake control plane. ``engine == ""`` means the plane is NOT installed."""

    def __init__(self) -> None:
        self.engine = ENGINE
        self.list_ok = True
        self.list_stdout = ""
        self.usage_ok = True
        self.usage_stdout = ""
        self.calls: list[str] = []

    def run(self, argv: list[str], timeout: float) -> dict:
        self.calls.append(" ".join(argv))
        if argv and argv[0] == "list":
            return {"ok": self.list_ok, "stdout": self.list_stdout, "error": ""}
        return {"ok": self.usage_ok, "stdout": self.usage_stdout, "error": ""}

    def answer_list(self, *accounts: dict) -> None:
        self.list_stdout = json.dumps(
            {"schema": "sai.accounts/list/1", "accounts": list(accounts), "status": "ok"})


class PopenSpy:
    """Records every child process any code under test tries to start."""

    def __init__(self) -> None:
        self.spawned: list[list[str]] = []

    def __call__(self, argv, *a, **k):
        self.spawned.append(list(argv) if isinstance(argv, (list, tuple)) else [str(argv)])
        raise AssertionError(f"a child process was started: {argv}")


@dataclass
class Launcher:
    id: str
    enabled: bool = True


@pytest.fixture(autouse=True)
def _isolate_plane():
    """Every test starts with the plane absent and the locator cache empty."""
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


def home_with(home, *, claude: bool = True, codex: bool = True, antigravity: bool = True):
    """A fake HOME carrying only the presence markers discovery looks for.

    No credential bytes are written: discovery checks that a marker FILE EXISTS
    and never reads it, and a test that wrote a token would be a test that could
    leak one.
    """
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    if antigravity:
        (home / ".gemini" / "antigravity").mkdir(parents=True, exist_ok=True)
    if claude:
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    if codex:
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    return home


def shared(account_id: str, provider: str, label: str, *, locator: str = "",
           backend: str = "windows_user", state: str = "ENABLED",
           hidden: bool = False) -> dict:
    meta: dict = {}
    if backend == "profile_directory":
        meta["profile_locator"] = locator
    elif locator:
        meta["windows_user"] = locator
    return {"account_id": account_id, "provider_id": provider, "display_name": label,
            "compact_label": label[-2:], "execution_backend": backend,
            "execution_context_id": "current-user", "operational_state": state,
            "hidden": hidden, "context_label": None, "provider_metadata": meta}


def records(accounts: list[AccountIdentity]) -> str:
    return ",".join(f"{a.provider_id}/{a.discovery_source}:{a.display_name}" for a in accounts)


def only_shared(accounts: list[AccountIdentity]) -> list[AccountIdentity]:
    return [a for a in accounts if a.discovery_source == sai.SHARED_SOURCE]


# ── STANDALONE ──────────────────────────────────────────────────────────────

class TestStandalone:
    """No plane. The application behaves exactly as it did before federation."""

    def test_plane_absent_resolves_to_nothing(self, plane: Plane) -> None:
        plane.engine = ""
        assert sai.engine_path() == ""

    def test_absent_plane_yields_no_shared_accounts(self, plane: Plane) -> None:
        plane.engine = ""
        assert sai.list_shared({"codex", "claude", "antigravity"}) == []

    def test_absent_plane_never_spawns_a_child(
            self, plane: Plane, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        plane.engine = ""
        spy = PopenSpy()
        monkeypatch.setattr(subprocess, "Popen", spy)
        discover_accounts([], home=home_with(tmp_path))
        assert spy.spawned == []

    def test_discovery_is_unchanged_without_the_plane(self, plane: Plane, tmp_path) -> None:
        plane.engine = ""
        got = discover_accounts([], home=home_with(tmp_path))
        assert records(got) == ("codex/known_profile_auth_marker:Codex 1,"
                                "claude/known_profile_auth_marker:Claude 1,"
                                "antigravity/known_provider_data_dir:Antigravity")

    def test_a_plain_home_discovers_nothing(self, plane: Plane, tmp_path) -> None:
        plane.engine = ""
        assert discover_accounts([], home=tmp_path) == []

    def test_local_accounts_keep_their_launcher_bindings(
            self, plane: Plane, tmp_path) -> None:
        plane.engine = ""
        got = discover_accounts([Launcher("main_codex")], home=home_with(tmp_path))
        codex = next(a for a in got if a.provider_id == "codex")
        assert codex.launcher_ids == ("main_codex",) and codex.bound is True
        claude = next(a for a in got if a.provider_id == "claude")
        assert claude.launcher_ids == () and claude.bound is False


class TestBrokenPlane:
    """A half-installed or failing plane is never a single point of failure."""

    def test_malformed_reply_is_absorbed(self, plane: Plane) -> None:
        plane.list_stdout = "{ this is not json"
        assert sai.list_shared({"codex", "claude", "antigravity"}) == []

    def test_wrongly_typed_reply_is_absorbed(self, plane: Plane) -> None:
        plane.list_stdout = '{"accounts": "not-an-array"}'
        assert sai.list_shared({"codex", "claude", "antigravity"}) == []

    def test_empty_reply_is_absorbed(self, plane: Plane) -> None:
        plane.list_stdout = ""
        assert sai.list_shared({"codex", "claude", "antigravity"}) == []

    def test_an_erroring_plane_leaves_local_discovery_intact(
            self, plane: Plane, tmp_path) -> None:
        plane.list_ok = False
        got = discover_accounts([], home=home_with(tmp_path))
        assert only_shared(got) == []
        assert len(got) == 3

    def test_a_plane_that_answers_nonsense_keeps_the_local_surface(
            self, plane: Plane, tmp_path) -> None:
        plane.list_stdout = json.dumps({"accounts": [None, 42, {"no": "id"}]})
        got = discover_accounts([], home=home_with(tmp_path))
        assert only_shared(got) == []


# ── FEDERATED ───────────────────────────────────────────────────────────────

class TestFederated:
    def test_shared_accounts_appear(self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        got = only_shared(discover_accounts([], home=home_with(tmp_path)))
        assert [(a.provider_id, a.display_name) for a in got] == \
            [("antigravity", "Antigravity B")]

    def test_origin_is_marked_so_the_ui_can_tell_them_apart(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        rows = discover_accounts([], home=home_with(tmp_path))
        assert {a.discovery_source for a in rows} == {
            "known_provider_data_dir", "known_profile_auth_marker", sai.SHARED_SOURCE}

    def test_a_shared_record_starts_unbound(self, plane: Plane, tmp_path) -> None:
        # Nothing here can decide which tool a remote account belongs to, and
        # inventing a binding would be worse than an honest UNBOUND account.
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        row = only_shared(discover_accounts([], home=home_with(tmp_path)))[0]
        assert row.launcher_ids == () and row.bound is False

    def test_a_shared_record_lives_in_its_own_id_namespace(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        row = only_shared(discover_accounts([], home=home_with(tmp_path)))[0]
        assert row.account_id.startswith("antigravity:shared:")
        assert row.account_id == sai.shared_account_id("antigravity", "someone-else")

    def test_hidden_and_non_enabled_accounts_leave_the_surface(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(
            shared("antigravity:windows-user:aaa", "antigravity", "Keep", locator="u1"),
            shared("antigravity:windows-user:bbb", "antigravity", "Hidden",
                   locator="u2", hidden=True),
            shared("antigravity:windows-user:ccc", "antigravity", "Frozen",
                   locator="u3", state="FROZEN"),
            shared("antigravity:windows-user:ddd", "antigravity", "Archived",
                   locator="u4", state="ARCHIVED"))
        got = [a.display_name
               for a in only_shared(discover_accounts([], home=home_with(tmp_path)))]
        assert got == ["Keep"]

    def test_a_globally_hidden_account_cannot_be_turned_on_locally(
            self, plane: Plane, tmp_path) -> None:
        # Local hide and global hide are two different concepts. AUDAPACK's own
        # per-account switch may disable a LOCAL record; it cannot resurrect one
        # the shared registry has taken out of service.
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Hidden", locator="someone-else", hidden=True))
        assert only_shared(discover_accounts([], home=home_with(tmp_path))) == []

    def test_an_account_for_an_unknown_provider_is_not_offered(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("gemini:windows-user:aaa", "gemini", "Gemini", locator="u1"))
        assert only_shared(discover_accounts([], home=home_with(tmp_path))) == []

    def test_the_plane_list_is_read_once_and_remembered(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        discover_accounts([], home=home_with(tmp_path))
        assert plane.calls == ["list --all"]
        assert sai.canonical_id_for("antigravity", "someone-else") == \
            "antigravity:windows-user:aaa"

    def test_two_reads_do_not_double_the_records(
            self, plane: Plane, tmp_path) -> None:
        entry = shared("antigravity:windows-user:aaa", "antigravity", "B", locator="u1")
        plane.answer_list(entry, entry)
        home = home_with(tmp_path)
        assert len(only_shared(discover_accounts([], home=home))) == 1
        assert len(only_shared(discover_accounts([], home=home))) == 1


# ── HYBRID ──────────────────────────────────────────────────────────────────

class TestHybrid:
    def test_a_proven_duplicate_is_one_record_not_two(self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            shared("claude:profile-dir:aaa", "claude", "Claude 1",
                   locator=str((home / ".claude").resolve()),
                   backend="profile_directory"))
        got = [a for a in discover_accounts([], home=home) if a.provider_id == "claude"]
        assert len(got) == 1
        assert got[0].discovery_source == "known_profile_auth_marker"

    def test_the_merged_record_keeps_the_local_account_id(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            shared("claude:profile-dir:aaa", "claude", "Claude 1",
                   locator=str((home / ".claude").resolve()),
                   backend="profile_directory"))
        row = next(a for a in discover_accounts([], home=home) if a.provider_id == "claude")
        assert not row.account_id.startswith("claude:shared:")

    def test_a_config_directory_locator_is_compared_case_insensitively(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            shared("claude:profile-dir:aaa", "claude", "Claude 1",
                   locator=str((home / ".claude").resolve()).upper(),
                   backend="profile_directory"))
        got = [a for a in discover_accounts([], home=home) if a.provider_id == "claude"]
        assert len(got) == 1

    def test_an_antigravity_account_is_located_by_windows_user(
            self, plane: Plane, tmp_path) -> None:
        # The Antigravity data root exists for every user on the machine, so the
        # profile path cannot identify the account. The Windows account name can,
        # and that is the locator the plane projects.
        home = home_with(tmp_path)
        plane.answer_list(
            shared("antigravity:windows-user:aaa", "antigravity", "Antigravity",
                   locator=home.name))
        got = [a for a in discover_accounts([], home=home) if a.provider_id == "antigravity"]
        assert len(got) == 1
        assert got[0].discovery_source == "known_provider_data_dir"

    def test_a_display_name_matching_a_local_one_does_not_merge(
            self, plane: Plane, tmp_path) -> None:
        # Same name, different proven identity -> two records, both honest.
        home = home_with(tmp_path)
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity", locator="someone-else"))
        got = [a for a in discover_accounts([], home=home) if a.provider_id == "antigravity"]
        assert len(got) == 2

    def test_local_only_accounts_survive(self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        assert records(discover_accounts([], home=home)) == (
            "codex/known_profile_auth_marker:Codex 1,"
            "claude/known_profile_auth_marker:Claude 1,"
            "antigravity/known_provider_data_dir:Antigravity,"
            "antigravity/sai_accounts_shared:Antigravity B")

    def test_no_local_account_at_all_still_sees_the_shared_one(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        got = only_shared(discover_accounts([], home=tmp_path))
        assert [a.display_name for a in got] == ["Antigravity B"]

    def test_the_shared_namespace_cannot_collide_with_a_local_id(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        ids = [a.account_id for a in discover_accounts([], home=home)]
        assert len(ids) == len(set(ids))
        assert any(i.startswith("antigravity:shared:") for i in ids)

    def test_a_shared_account_never_appears_twice(self, plane: Plane, tmp_path) -> None:
        entry = shared("antigravity:windows-user:aaa", "antigravity", "B", locator="u1")
        plane.answer_list(entry, entry)
        assert len(only_shared(discover_accounts([], home=home_with(tmp_path)))) == 1


# ── DUPLICATE DISCOVERY ─────────────────────────────────────────────────────

class TestDuplicateDiscovery:
    def test_unprovable_identity_is_shown_both_ways(self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(shared("antigravity:windows-user:zzz", "antigravity",
                                 "Antigravity", locator=""))
        got = [a for a in discover_accounts([], home=home) if a.provider_id == "antigravity"]
        assert len(got) == 2
        assert {a.discovery_source for a in got} == {
            "known_provider_data_dir", sai.SHARED_SOURCE}

    def test_an_empty_locator_never_claims_a_key(self) -> None:
        assert sai.identity_key("claude", "") == ""
        assert sai.identity_key("", "alice") == ""
        assert sai.identity_key("  ", "alice") == ""

    def test_different_providers_never_collide(self) -> None:
        assert sai.identity_key("claude", "alice") != sai.identity_key("codex", "alice")

    def test_different_locators_never_collide(self) -> None:
        assert sai.identity_key("antigravity", "alice") != \
            sai.identity_key("antigravity", "bob")

    def test_the_same_locator_is_the_same_key(self) -> None:
        assert sai.identity_key("antigravity", "Alice") == \
            sai.identity_key("antigravity", "alice")

    def test_a_shared_id_does_not_depend_on_the_working_directory(
            self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "deeper").mkdir()
        monkeypatch.chdir(tmp_path)
        first = sai.shared_account_id("antigravity", "alice")
        monkeypatch.chdir(tmp_path / "deeper")
        assert sai.shared_account_id("antigravity", "alice") == first

    def test_two_locator_less_accounts_get_two_rows(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(
            shared("antigravity:windows-user:aaa", "antigravity", "One", locator=""),
            shared("antigravity:windows-user:bbb", "antigravity", "Two", locator=""))
        rows = only_shared(discover_accounts([], home=home_with(tmp_path)))
        assert len(rows) == 2
        assert len({a.account_id for a in rows}) == 2

    def test_a_locator_less_account_is_not_read_through_the_plane(
            self, plane: Plane, tmp_path) -> None:
        # Nothing proves which account it is, so nothing may be reported about
        # it under that name. It is shown as shared and it is honestly unreadable.
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "One", locator=""))
        row = only_shared(discover_accounts([], home=home_with(tmp_path)))[0]
        snap = AntigravityLimitAdapter(executable="agy.exe").probe_limits(row)
        assert snap.windows == () and snap.error == "shared_source_offline"

    def test_canonical_lookup_refuses_to_guess(self, plane: Plane) -> None:
        assert sai.canonical_id_for("antigravity", "nobody") == ""
        assert sai.canonical_id_for("antigravity", "") == ""


# ── CENTRAL FAILURE ─────────────────────────────────────────────────────────

class TestCentralFailure:
    def _adapter(self) -> AntigravityLimitAdapter:
        # An executable is present on purpose: the assertion is that a shared
        # account is NEVER read locally, so the local path must be reachable.
        return AntigravityLimitAdapter(executable="agy.exe")

    def _shared_row(self, plane: Plane, tmp_path) -> AccountIdentity:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        return only_shared(discover_accounts([], home=home_with(tmp_path)))[0]

    def test_plane_removed_mid_life_leaves_local_accounts_working(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        assert len(discover_accounts([], home=home)) == 4
        plane.engine = ""
        got = discover_accounts([], home=home)
        assert records(got) == ("codex/known_profile_auth_marker:Codex 1,"
                                "claude/known_profile_auth_marker:Claude 1,"
                                "antigravity/known_provider_data_dir:Antigravity")

    def test_plane_windows_become_real_windows(self, plane: Plane, tmp_path) -> None:
        row = self._shared_row(plane, tmp_path)
        plane.usage_stdout = json.dumps({
            "account_id": "antigravity:windows-user:aaa",
            "context_state": "ONLINE", "auth_state": "AUTHENTICATED",
            "windows": [
                {"pool_index": 0, "window": "5h", "remaining_fraction": 0.25,
                 "reset_time": "2026-10-03T10:00:00Z"},
                {"pool_index": 0, "window": "weekly", "remaining_fraction": 0.5,
                 "reset_time": "2026-10-06T10:00:00Z"}]})
        snap = self._adapter().probe_limits(row)
        assert [w.kind for w in snap.windows] == ["five_hour", "weekly"]
        assert snap.windows[0].remaining_ratio == pytest.approx(0.25)
        assert snap.windows[0].used_ratio == pytest.approx(0.75)
        assert snap.error == ""

    def test_a_second_pool_gets_its_own_window_id(self, plane: Plane, tmp_path) -> None:
        row = self._shared_row(plane, tmp_path)
        plane.usage_stdout = json.dumps({
            "account_id": "antigravity:windows-user:aaa", "context_state": "ONLINE",
            "auth_state": "AUTHENTICATED",
            "windows": [
                {"pool_index": 0, "window": "5h", "remaining_fraction": 0.5},
                {"pool_index": 1, "window": "5h", "remaining_fraction": 0.1}]})
        snap = self._adapter().probe_limits(row)
        assert [w.window_id for w in snap.windows] == ["five_hour@0", "five_hour@1"]

    def test_offline_context_is_reported_not_faked(self, plane: Plane, tmp_path) -> None:
        row = self._shared_row(plane, tmp_path)
        plane.usage_stdout = json.dumps({
            "account_id": "antigravity:windows-user:aaa", "context_state": "OFFLINE",
            "auth_state": "UNKNOWN", "skipped_reason": "CONTEXT_OFFLINE"})
        snap = self._adapter().probe_limits(row)
        assert snap.windows == ()
        assert snap.error == "offline:CONTEXT_OFFLINE"

    def test_auth_refusal_is_typed_not_unknown(self, plane: Plane, tmp_path) -> None:
        row = self._shared_row(plane, tmp_path)
        plane.usage_ok = False      # the plane's own convention: typed body, nonzero exit
        plane.usage_stdout = json.dumps({
            "account_id": "antigravity:windows-user:aaa", "context_state": "ONLINE",
            "auth_state": "AUTH_REQUIRED", "usage_status": "unreadable_usage"})
        snap = self._adapter().probe_limits(row)
        assert snap.error == "auth_required:not authenticated"

    def test_a_provider_the_plane_cannot_read_keeps_no_opinion(
            self, plane: Plane, tmp_path) -> None:
        # An honest "I do not read this provider" is not an outage and must not
        # be reported as one.
        row = self._shared_row(plane, tmp_path)
        plane.usage_stdout = json.dumps({
            "account_id": "antigravity:windows-user:aaa",
            "skipped_reason": "provider_does_not_support_quota"})
        with pytest.raises(RuntimeError, match="cannot_read_provider"):
            self._adapter().probe_limits(row)

    def test_erroring_plane_is_not_the_local_read(
            self, plane: Plane, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The local read answers about THIS user's account. Reporting it under a
        # shared account's name is exactly the wrong-number bug the amendment
        # forbids, so it must not be reached at all.
        row = self._shared_row(plane, tmp_path)
        spy = PopenSpy()
        monkeypatch.setattr(subprocess, "Popen", spy)
        plane.usage_ok = False
        plane.usage_stdout = ""
        snap = self._adapter().probe_limits(row)
        assert snap.windows == ()
        assert snap.error == "unavailable:SAI Accounts did not answer"
        assert spy.spawned == []

    def test_a_vanished_account_is_reported_as_an_unavailable_shared_source(
            self, plane: Plane, tmp_path) -> None:
        row = self._shared_row(plane, tmp_path)
        sai.remember([])                      # the plane stopped listing it
        snap = self._adapter().probe_limits(row)
        assert snap.windows == () and snap.error == "shared_source_offline"

    def test_recovery_when_the_plane_answers_again(self, plane: Plane, tmp_path) -> None:
        row = self._shared_row(plane, tmp_path)
        plane.usage_ok = False
        assert self._adapter().probe_limits(row).error.startswith("unavailable:")
        plane.usage_ok = True
        plane.usage_stdout = json.dumps({
            "account_id": "antigravity:windows-user:aaa", "context_state": "ONLINE",
            "auth_state": "AUTHENTICATED",
            "windows": [{"pool_index": 0, "window": "5h", "remaining_fraction": 1.0}]})
        assert self._adapter().probe_limits(row).windows


class TestLocalAccountsStillReadLocally:
    def test_a_local_account_never_goes_through_the_plane(
            self, plane: Plane, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = home_with(tmp_path)
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        row = next(a for a in discover_accounts([], home=home)
                   if a.discovery_source == "known_provider_data_dir")
        spy = PopenSpy()
        monkeypatch.setattr(subprocess, "Popen", spy)
        with pytest.raises(AssertionError):
            AntigravityLimitAdapter(executable="agy.exe").probe_limits(row)
        assert plane.calls == ["list --all"]  # the plane was asked to list, nothing more


# ── origin is visible in the UI ─────────────────────────────────────────────

class TestOriginIsVisibleInTheUi:
    """A shared row must LOOK shared.

    Origin is not decoration here: a shared account is read through the control
    plane rather than this machine's own CLI, and it starts UNBOUND on purpose.
    A row that renders identically to a local one would invite an operator to
    treat a remote identity as a local one.
    """

    @staticmethod
    def _build(qapp, monkeypatch, accounts: list[AccountIdentity]):
        from audapack.ui_qt.dialogs import limits_prepared_widget as mod
        monkeypatch.setattr(mod, "discover_accounts", lambda launchers: list(accounts))

        class Config:
            launchers = ()

        class Runner:
            def submit_coalesced(self, key, load, on_success=None, **_kw):
                on_success(load())

        widget = mod.LimitsPreparedWidget(Config(), Runner())
        widget.timer.stop()
        return widget

    def test_a_shared_row_is_marked_and_a_local_row_is_not(
            self, qapp, monkeypatch, tmp_path) -> None:
        local = AccountIdentity(
            account_id="claude:local", provider_id="claude", display_name="Claude 1",
            profile_locator=str(tmp_path / ".claude"), launcher_ids=("claude1",),
            discovery_source="known_profile_auth_marker", last_seen_at="2026-10-02T00:00:00+00:00")
        remote = AccountIdentity(
            account_id="antigravity:shared:abc", provider_id="antigravity",
            display_name="Antigravity B", profile_locator="someone-else", launcher_ids=(),
            discovery_source=sai.SHARED_SOURCE, last_seen_at="2026-10-02T00:00:00+00:00")
        widget = self._build(qapp, monkeypatch, [local, remote])
        labels = [widget.account_table.item(r, 1).text()
                  for r in range(widget.account_table.rowCount())]
        assert "Antigravity B (shared)" in labels
        assert "Claude 1" in labels and "Claude 1 (shared)" not in labels

    def test_the_shared_tooltip_says_where_the_number_came_from(
            self, qapp, monkeypatch, tmp_path) -> None:
        remote = AccountIdentity(
            account_id="antigravity:shared:abc", provider_id="antigravity",
            display_name="Antigravity B", profile_locator="someone-else", launcher_ids=(),
            discovery_source=sai.SHARED_SOURCE, last_seen_at="2026-10-02T00:00:00+00:00")
        widget = self._build(qapp, monkeypatch, [remote])
        assert "SAI Accounts" in widget.account_table.item(0, 1).toolTip()

    def test_a_shared_row_still_reads_unbound(self, qapp, monkeypatch, tmp_path) -> None:
        remote = AccountIdentity(
            account_id="antigravity:shared:abc", provider_id="antigravity",
            display_name="Antigravity B", profile_locator="someone-else", launcher_ids=(),
            discovery_source=sai.SHARED_SOURCE, last_seen_at="2026-10-02T00:00:00+00:00")
        widget = self._build(qapp, monkeypatch, [remote])
        assert widget.account_table.item(0, 2).text() == "UNBOUND"


# ── GLOBAL hide / disable ────────────────────────────────────────────────────

class TestGlobalState:
    """The plane's global state reaches the record AUDAPACK already had.

    A global hide that only hides the SHARED half would let every account
    AUDAPACK already discovered survive the hide the operator asked for.
    Identity is still the only thing that can connect the two halves: a locator
    that cannot be proven suppresses nothing.
    """

    def _claude_locator(self, home) -> str:
        return str((home / ".claude").resolve())

    def test_a_globally_hidden_shared_account_is_not_drawn(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("claude:profile-dir:aaa", "claude", "Claude 9",
                                 locator=self._claude_locator(tmp_path),
                                 backend="profile_directory", hidden=True))
        assert sai.list_shared({"claude"}) == []

    def test_a_globally_hidden_account_drops_its_proven_local_record(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("claude:profile-dir:aaa", "claude", "Claude 1",
                                 locator=self._claude_locator(tmp_path),
                                 backend="profile_directory", hidden=True))
        got = discover_accounts([], home=home_with(tmp_path))
        assert [a for a in got if a.provider_id == "claude"] == []

    @pytest.mark.parametrize("state", ["DISABLED", "FROZEN", "ARCHIVED"])
    def test_an_account_out_of_service_drops_its_proven_local_record(
            self, plane: Plane, tmp_path, state: str) -> None:
        plane.answer_list(shared("claude:profile-dir:aaa", "claude", "Claude 1",
                                 locator=self._claude_locator(tmp_path),
                                 backend="profile_directory", state=state))
        got = discover_accounts([], home=home_with(tmp_path))
        assert [a for a in got if a.provider_id == "claude"] == []

    def test_an_enabled_shared_account_leaves_its_local_record_alone(
            self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("claude:profile-dir:aaa", "claude", "Claude 1",
                                 locator=self._claude_locator(tmp_path),
                                 backend="profile_directory"))
        got = discover_accounts([], home=home_with(tmp_path))
        assert [a for a in got if a.provider_id == "claude"] != []

    def test_a_withdrawal_suppresses_only_its_own_identity(
            self, plane: Plane, tmp_path) -> None:
        home = home_with(tmp_path)
        plane.answer_list(
            shared("claude:profile-dir:aaa", "claude", "Claude 1",
                   locator=self._claude_locator(tmp_path),
                   backend="profile_directory", hidden=True),
            shared("codex:profile-dir:bbb", "codex", "Codex 1",
                   locator=str((tmp_path / ".codex").resolve()),
                   backend="profile_directory"))
        assert "claude" not in records(discover_accounts([], home=home))
        assert "codex/known_profile_auth_marker" in records(discover_accounts([], home=home))

    def test_an_unprovable_withdrawal_suppresses_nothing(
            self, plane: Plane, tmp_path) -> None:
        # No locator means "cannot prove". Suppressing on that would hide every
        # record that also has no locator, which is a different one each time.
        plane.answer_list(shared("claude:profile-dir:aaa", "claude", "Claude 9",
                                 backend="profile_directory", hidden=True))
        got = discover_accounts([], home=home_with(tmp_path))
        assert "claude/known_profile_auth_marker" in records(got)

    def test_a_broken_plane_withdraws_nothing(self, plane: Plane, tmp_path) -> None:
        # An unreadable registry is not permission to delete local records.
        plane.list_ok = False
        got = discover_accounts([], home=home_with(tmp_path))
        assert "claude/known_profile_auth_marker" in records(got)

    def test_the_predicate_is_the_one_the_list_filter_uses(self) -> None:
        assert sai.withdrawn_by_plane({"hidden": True}) is True
        assert sai.withdrawn_by_plane({"operational_state": "ARCHIVED"}) is True
        assert sai.withdrawn_by_plane({"operational_state": ""}) is False
        assert sai.withdrawn_by_plane({}) is False


# ── read-only guarantee ─────────────────────────────────────────────────────

class TestReadOnly:
    def test_the_module_only_ever_reads(self) -> None:
        source = pathlib.Path(sai.__file__).read_text(encoding="utf-8")
        for forbidden in ("open(", "write_text", "write_bytes", "mkdir",
                          "unlink", "rmtree", "sqlite3", "urllib", "urlopen"):
            assert forbidden not in source, f"{forbidden!r} must not appear"

    def test_discovery_stays_credential_free(self, plane: Plane, tmp_path) -> None:
        plane.answer_list(shared("antigravity:windows-user:aaa", "antigravity",
                                 "Antigravity B", locator="someone-else"))
        rows = discover_accounts([], home=home_with(tmp_path))
        blob = " ".join(f"{a.account_id}{a.profile_locator}{a.display_name}" for a in rows)
        for secret in ("PRIVATE_TOKEN", "oauth", "refresh_token", "windows_sid"):
            assert secret.casefold() not in blob.casefold()

    def test_identity_locator_never_leaks_a_machine_path_for_antigravity(
            self, tmp_path) -> None:
        locator = identity_locator("antigravity", tmp_path / ".gemini" / "antigravity",
                                   tmp_path)
        assert locator == tmp_path.name

    def test_an_override_that_does_not_exist_falls_through(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setattr(sai, "TestEngine", None)
        monkeypatch.setattr(sai, "CANONICAL_INSTALL", tmp_path / "not-installed")
        monkeypatch.setenv(sai.ENV_OVERRIDE, str(tmp_path / "nope.exe"))
        monkeypatch.setattr(sai.shutil, "which", lambda name: "")
        assert sai.engine_path() == ""

    def test_an_override_that_exists_is_used(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        exe = tmp_path / "sai-accounts.exe"
        exe.write_bytes(b"")
        monkeypatch.setattr(sai, "TestEngine", None)
        monkeypatch.setenv(sai.ENV_OVERRIDE, str(exe))
        assert sai.engine_path() == str(exe)
