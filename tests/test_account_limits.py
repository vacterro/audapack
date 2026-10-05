from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audapack.account_registry import AccountIdentity, AccountRegistry, discover_accounts
from audapack.config import create_default_launchers
from audapack.limit_adapters import parse_antigravity_usage, parse_codex_rate_limits
from audapack.limits import Availability, LimitCoordinator, LimitSnapshot, LimitStore, LimitWindow

NOW = datetime(2026, 9, 23, 9, tzinfo=timezone.utc)


def test_discovery_stable_bound_unbound_and_credential_free(tmp_path: Path):
    for dirname, marker in ((".codex", "auth.json"), (".codex-account2", "auth.json"),
                            (".claude", ".credentials.json")):
        folder = tmp_path / dirname
        folder.mkdir()
        (folder / marker).write_text("PRIVATE_TOKEN=do-not-copy", encoding="utf-8")
    launchers = [launcher for launcher in create_default_launchers() if launcher.id != "main_codex2"]
    first = discover_accounts(launchers, home=tmp_path, now=NOW)
    second = discover_accounts(launchers, home=tmp_path, now=NOW + timedelta(hours=1))
    assert len(first) == 3
    assert [a.account_id for a in first] == [a.account_id for a in second]
    assert next(a for a in first if a.display_name == "Codex 2").bound is False
    db = AccountRegistry(tmp_path / "resources.sqlite3")
    db.upsert(first)
    db.upsert(second)
    assert len(db.list()) == 3
    assert b"PRIVATE_TOKEN" not in (tmp_path / "resources.sqlite3").read_bytes()
    codex2 = next(a for a in first if a.display_name == "Codex 2")
    db.bind(codex2.account_id, "custom_codex")
    db.upsert(second)
    assert next(a for a in db.list() if a.account_id == codex2.account_id).launcher_ids == ("custom_codex",)
    with pytest.raises(ValueError, match="another account"):
        db.bind(first[0].account_id, "custom_codex")


def test_all_hard_windows_constrain_availability_and_staleness():
    windows = (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
        LimitWindow("weekly", "weekly", "Weekly", remaining_ratio=0, reset_at=(NOW + timedelta(hours=2)).isoformat()),
    )
    snapshot = LimitSnapshot("codex:1", windows, NOW.isoformat(), "local", stale_after_seconds=10800)
    assert snapshot.availability(NOW) == Availability.EXHAUSTED
    assert snapshot.bottleneck().window_id == "weekly"
    assert snapshot.availability(NOW + timedelta(hours=2)) == Availability.RESET_PENDING
    assert snapshot.availability(NOW + timedelta(hours=4)) == Availability.STALE


def test_independent_quota_bucket_does_not_exhaust_default_model():
    snapshot = LimitSnapshot(
        "codex:1",
        (
            LimitWindow("five_hour@codex", "five_hour", "Codex 5h", remaining_ratio=0.7,
                        quota_bucket="codex"),
            LimitWindow("weekly@codex", "weekly", "Codex weekly", remaining_ratio=0.6,
                        quota_bucket="codex"),
            LimitWindow("weekly@reserve", "weekly", "Reserve weekly", remaining_ratio=0,
                        quota_bucket="reserve"),
        ), NOW.isoformat(), "codex_app_server",
    )
    assert snapshot.availability(NOW) == Availability.AVAILABLE
    assert snapshot.availability(NOW, quota_bucket="reserve") == Availability.EXHAUSTED


def test_fifteen_minute_probe_and_manual_refresh(tmp_path: Path):
    class Adapter:
        provider_id = "codex"
        calls = 0

        def probe_limits(self, account):
            self.calls += 1
            return LimitSnapshot(account.account_id, (), current[0].isoformat(), "test")

    account = discover_accounts([], home=tmp_path)
    assert not account
    account = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path), (), "test", NOW.isoformat())
    current = [NOW]
    adapter = Adapter()
    coordinator = LimitCoordinator({"codex": adapter}, LimitStore(tmp_path / "limits.db"), lambda: current[0])
    coordinator.refresh(account)
    current[0] += timedelta(minutes=3)
    for _ in range(5):
        coordinator.refresh(account)
    assert adapter.calls == 1
    current[0] += timedelta(minutes=12)
    coordinator.refresh(account)
    assert adapter.calls == 2
    coordinator.refresh(account, force=True)
    assert adapter.calls == 3


def test_probe_errors_back_off_and_success_restores_normal_cadence(tmp_path: Path):
    class Adapter:
        provider_id = "codex"
        calls = 0

        def probe_limits(self, account):
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError("temporary provider failure")
            return LimitSnapshot(account.account_id, (), current[0].isoformat(), "fake")

    account = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path),
                              (), "test", NOW.isoformat())
    current = [NOW]
    store = LimitStore(tmp_path / "limits.db")
    adapter = Adapter()
    coordinator = LimitCoordinator({"codex": adapter}, store, lambda: current[0])
    assert coordinator.refresh(account).error == "RuntimeError"
    assert store.get(account.account_id)[1] == NOW + timedelta(minutes=15)
    current[0] += timedelta(minutes=15)
    assert coordinator.refresh(account).error == "RuntimeError"
    assert store.get(account.account_id)[1] == NOW + timedelta(minutes=45)
    current[0] += timedelta(minutes=30)
    assert not coordinator.refresh(account).error
    assert store.get(account.account_id)[1] == NOW + timedelta(minutes=60)
    assert store.get(account.account_id)[2] == 0


def test_codex_normalizes_reported_durations_and_separate_reserve_bucket():
    raw = {"rateLimitsByLimitId": {
        "codex": {"primary": {"windowDurationMins": 300, "usedPercent": 20,
                               "resetsAt": 1780000000},
                  "secondary": {"windowDurationMins": 10080, "usedPercent": 10}},
        "reserve": {"primary": {"windowDurationMins": 43200, "usedPercent": 100}},
    }}
    snapshot = parse_codex_rate_limits("codex:2", raw, NOW.isoformat())
    assert {(w.kind, w.quota_bucket) for w in snapshot.windows} == {
        ("five_hour", "codex"), ("weekly", "codex"), ("monthly", "reserve")}
    assert snapshot.availability(NOW) == Availability.AVAILABLE
    assert snapshot.availability(NOW, quota_bucket="reserve") == Availability.EXHAUSTED
    assert next(w for w in snapshot.windows if w.kind == "weekly").reset_at is None


def test_probe_lease_deduplicates_across_coordinators(tmp_path: Path):
    path = tmp_path / "shared.db"
    first = LimitStore(path)
    second = LimitStore(path)
    assert first.claim_probe("a", "owner-a", NOW)
    assert not second.claim_probe("a", "owner-b", NOW)
    first.release_probe("a", "owner-a")
    assert second.claim_probe("a", "owner-b", NOW)


def test_antigravity_disabled_pool_is_not_misreported_free():
    raw = {"command": {"data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"window": "weekly", "remaining_fraction": 0.5,
             "reset_time": "2026-09-29T10:00:00Z"},
            {"window": "5h", "remaining_fraction": 0.8}]},
        {"name": "Claude and GPT models", "buckets": [
            {"window": "weekly", "remaining_fraction": 0},
            {"window": "5h", "remaining_fraction": 1, "disabled": True}]},
    ]}}}
    snapshot = parse_antigravity_usage("ag:1", raw, NOW.isoformat())
    assert snapshot.availability(NOW, quota_bucket="gemini_models") == Availability.AVAILABLE
    assert snapshot.availability(NOW, quota_bucket="claude_and_gpt_models") == Availability.EXHAUSTED
    assert len(snapshot.windows) == 4
