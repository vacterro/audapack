"""Regressions for audit/13 CORE findings: CORE-001, CORE-002, CORE-003."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from audapack import launch_federation as fed
from audapack import sai_accounts as sai
from audapack.account_registry import AccountIdentity
from audapack.config import AppConfig, LauncherConfig
from audapack.limit_adapters import AntigravityLimitAdapter
from audapack.ui_qt.app import _sync_launcher_inventory

ENGINE = r"C:\fake\sai-accounts.exe"


class FakePlane:
    def __init__(self) -> None:
        self.list_calls = 0
        self.usage_calls = 0
        self.list_responses: list[dict[str, Any]] = []
        self.usage_response: dict[str, Any] = {"ok": True, "stdout": "", "error": ""}

    def run(self, argv: list[str], timeout: float) -> dict[str, Any]:
        if argv and argv[0] == "list":
            self.list_calls += 1
            if self.list_responses:
                return self.list_responses.pop(0)
            return {"ok": True, "stdout": json.dumps({"schema": "sai.accounts/list/1", "status": "ok", "accounts": []}), "error": ""}
        self.usage_calls += 1
        return self.usage_response


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


# ── CORE-001: LIST Snapshot Authority ───────────────────────────────────────

class TestCore001ListAuthority:
    def test_unsupported_list_schema_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "future.incompatible/list/99",
                "status": "ok",
                "generation": 9,
                "accounts": [{
                    "account_id": "claude:profile-dir:abc",
                    "provider_id": "claude",
                    "display_name": "Claude 1",
                    "profile_locator": "C:\\loc1",
                }],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["claude"])
        assert not listing.ok, "Unsupported schema must make entire snapshot non-authoritative"
        assert listing.accounts == []

    def test_non_ok_list_status_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "error",
                "accounts": [{
                    "account_id": "claude:profile-dir:abc",
                    "provider_id": "claude",
                    "display_name": "Claude 1",
                    "profile_locator": "C:\\loc1",
                }],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["claude"])
        assert not listing.ok, "Non-ok status must make entire snapshot non-authoritative"
        assert listing.accounts == []

    def test_canonical_account_and_provider_mismatch_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "accounts": [{
                    "account_id": "claude:windows_user:abc123",
                    "provider_id": "antigravity",
                    "display_name": "Claude or Antigravity?",
                    "windows_user": "abc123",
                }],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["antigravity", "claude"])
        assert not listing.ok, "Contradictory account_id provider and provider_id must fail closed"
        assert listing.accounts == []

    def test_duplicate_canonical_account_id_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "accounts": [
                    {
                        "account_id": "claude:profile-dir:DUP",
                        "provider_id": "claude",
                        "display_name": "Claude First",
                        "profile_locator": "C:\\loc1",
                    },
                    {
                        "account_id": "claude:profile-dir:DUP",
                        "provider_id": "claude",
                        "display_name": "Claude Second",
                        "profile_locator": "C:\\loc2",
                    },
                ],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["claude"])
        assert not listing.ok, "Duplicate canonical account_id must fail closed"
        assert listing.accounts == []

    def test_identical_duplicate_canonical_account_id_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "generation": 4,
                "accounts": [
                    {
                        "account_id": "claude:profile-dir:DUP",
                        "provider_id": "claude",
                        "display_name": "Claude Same",
                        "profile_locator": "C:\\loc1",
                    },
                    {
                        "account_id": "claude:profile-dir:DUP",
                        "provider_id": "claude",
                        "display_name": "Claude Same",
                        "profile_locator": "C:\\loc1",
                    },
                ],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["claude"])
        assert listing.ok is False, "Identical duplicate canonical account_id must fail closed"
        assert listing.accounts == []

        # Also prove invalid authority cannot mutate last-known-good federation projection
        cfg = AppConfig()
        cfg.launchers = [
            LauncherConfig(id="sai:claude:profile-dir:valid", name="Valid Claude", short_label="VC"),
        ]
        cfg.federation.active = True
        cfg.federation.generation = 2

        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "generation": 4,
                "accounts": [
                    {
                        "account_id": "claude:profile-dir:DUP",
                        "provider_id": "claude",
                        "display_name": "Claude Same",
                        "profile_locator": "C:\\loc1",
                    },
                    {
                        "account_id": "claude:profile-dir:DUP",
                        "provider_id": "claude",
                        "display_name": "Claude Same",
                        "profile_locator": "C:\\loc1",
                    },
                ],
            }),
            "error": "",
        }]
        sync = fed.sync_effective_launchers(cfg)
        assert not sync.fresh
        assert sync.state == "stale"
        assert len(cfg.launchers) == 1
        assert cfg.launchers[0].id == "sai:claude:profile-dir:valid"

    def test_malformed_wanted_provider_record_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "accounts": [
                    {
                        "account_id": "",
                        "provider_id": "claude",
                    },
                ],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["claude"])
        assert not listing.ok, "Malformed wanted-provider entry must make snapshot non-authoritative"

    def test_invalid_snapshot_cannot_alter_last_known_good_launchers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        cfg = AppConfig()
        cfg.launchers = [
            LauncherConfig(id="sai:claude:profile-dir:valid", name="Valid Claude", short_label="VC"),
        ]
        cfg.federation.active = True
        cfg.federation.generation = 5

        # Now feed an invalid snapshot (duplicate IDs)
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "generation": 6,
                "accounts": [
                    {"account_id": "claude:profile-dir:DUP", "provider_id": "claude", "profile_locator": "C:\\a"},
                    {"account_id": "claude:profile-dir:DUP", "provider_id": "claude", "profile_locator": "C:\\b"},
                ],
            }),
            "error": "",
        }]

        sync = fed.sync_effective_launchers(cfg)
        assert not sync.fresh
        assert sync.state == "stale"
        assert len(cfg.launchers) == 1
        assert cfg.launchers[0].id == "sai:claude:profile-dir:valid"

    def test_valid_deployed_schema_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.list_responses = [{
            "ok": True,
            "stdout": json.dumps({
                "schema": "sai.accounts/list/1",
                "status": "ok",
                "generation": 3,
                "accounts": [
                    {
                        "account_id": "claude:profile-dir:abc",
                        "provider_id": "claude",
                        "display_name": "Claude 1",
                        "profile_locator": "C:\\loc1",
                    },
                ],
            }),
            "error": "",
        }]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        listing = sai.read_registry(["claude"])
        assert listing.ok
        assert listing.generation == 3
        assert len(listing.accounts) == 1
        assert listing.accounts[0].account_id == "claude:profile-dir:abc"


# ── CORE-002: Usage Response Account Identity ───────────────────────────────

class TestCore002UsageIdentity:
    def test_requested_a_payload_names_b_discards_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.usage_response = {
            "ok": True,
            "stdout": json.dumps({
                "account_id": "claude:profile-dir:OTHER",
                "context_state": "ONLINE",
                "auth_state": "AUTHENTICATED",
                "windows": [{"window": "5h", "remaining_fraction": 0.85}],
            }),
            "error": "",
        }
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        windows, state, detail = sai.probe("claude:profile-dir:REQUESTED")
        assert windows == [], "Foreign quota windows must be discarded"
        assert state != "ok", "Foreign payload cannot return ok"
        assert state == "unavailable"

    def test_missing_account_id_in_usage_response_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.usage_response = {
            "ok": True,
            "stdout": json.dumps({
                "context_state": "ONLINE",
                "auth_state": "AUTHENTICATED",
                "windows": [{"window": "5h", "remaining_fraction": 0.5}],
            }),
            "error": "",
        }
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        windows, state, detail = sai.probe("claude:profile-dir:AAA")
        assert windows == []
        assert state == "unavailable"

    def test_non_string_account_id_in_usage_response_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.usage_response = {
            "ok": True,
            "stdout": json.dumps({
                "account_id": 12345,
                "context_state": "ONLINE",
                "windows": [{"window": "5h", "remaining_fraction": 0.5}],
            }),
            "error": "",
        }
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        windows, state, detail = sai.probe("claude:profile-dir:AAA")
        assert windows == []
        assert state == "unavailable"

    def test_matching_account_happy_path_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.usage_response = {
            "ok": True,
            "stdout": json.dumps({
                "account_id": "claude:profile-dir:AAA",
                "context_state": "ONLINE",
                "auth_state": "AUTHENTICATED",
                "windows": [{"window": "5h", "remaining_fraction": 0.9}],
            }),
            "error": "",
        }
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        windows, state, detail = sai.probe("claude:profile-dir:AAA")
        assert len(windows) == 1
        assert state == "ok"
        assert windows[0].remaining_ratio == pytest.approx(0.9)

    def test_limit_adapter_cannot_attribute_foreign_windows_to_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plane = FakePlane()
        plane.usage_response = {
            "ok": True,
            "stdout": json.dumps({
                "account_id": "antigravity:windows-user:B",
                "context_state": "ONLINE",
                "auth_state": "AUTHENTICATED",
                "windows": [{"window": "5h", "remaining_fraction": 0.95}],
            }),
            "error": "",
        }
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        # Populate cache so canonical id resolves
        sai.remember([
            sai.SharedAccount(
                account_id="antigravity:windows-user:A",
                provider_id="antigravity",
                display_name="User A",
                compact_label="",
                backend="windows_user",
                locator="A_locator",
                operational_state="ENABLED",
            )
        ])

        adapter = AntigravityLimitAdapter()
        identity = AccountIdentity(
            account_id="local_a",
            provider_id="antigravity",
            display_name="User A",
            profile_locator="A_locator",
            launcher_ids=("sai:antigravity:windows-user:A",),
            discovery_source=sai.SHARED_SOURCE,
            last_seen_at="",
        )
        snap = adapter.probe_limits(identity)
        assert snap.windows == (), "Foreign windows must never reach AccountIdentity A"
        assert snap.error.startswith("unavailable:")


# ── CORE-003: Single Authority Read on Startup Refresh ───────────────────────

class TestCore003SingleAuthorityRead:
    def test_sync_launcher_inventory_reads_authority_exactly_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        plane = FakePlane()
        # Call 1: valid generation 1
        # Call 2 (if made): would timeout or return error
        plane.list_responses = [
            {
                "ok": True,
                "stdout": json.dumps({
                    "schema": "sai.accounts/list/1",
                    "status": "ok",
                    "generation": 1,
                    "accounts": [{
                        "account_id": "claude:profile-dir:gen1",
                        "provider_id": "claude",
                        "display_name": "Claude G1",
                        "profile_locator": str(tmp_path / "g1"),
                    }],
                }),
                "error": "",
            },
            {
                "ok": False,
                "stdout": "",
                "error": "timeout",
            },
        ]
        monkeypatch.setattr(sai, "TestEngine", lambda: ENGINE)
        monkeypatch.setattr(sai, "TestRun", plane.run)

        # Mock service and config persistence
        from audapack.config import load_config, save_config
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("audapack.config.config_path", lambda base_dir=None: cfg_path)

        initial_cfg = AppConfig()
        save_config(initial_cfg)

        @dataclass
        class FakeService:
            config: AppConfig

        svc = FakeService(config=load_config())

        _sync_launcher_inventory(svc)

        assert plane.list_calls == 1, f"Expected exactly 1 LIST call, but saw {plane.list_calls}"

        persisted = load_config()
        # Runtime and persisted projection must agree
        assert svc.config.federation.generation == 1
        assert persisted.federation.generation == 1
        assert [item.id for item in svc.config.launchers if item.id.startswith("sai:")] == ["sai:claude:profile-dir:gen1"]
        assert [item.id for item in persisted.launchers if item.id.startswith("sai:")] == ["sai:claude:profile-dir:gen1"]
