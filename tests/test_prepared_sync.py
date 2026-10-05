"""SYNC barriers use fake providers; no model requests or real processes."""

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from audapack.prepared import (
    JobState,
    Payload,
    PreparedJob,
    PreparedStore,
    PreparedSyncMember,
    Trigger,
)
from audapack.prepared_sync import (
    SyncCoordinator,
    SyncMemberSpec,
    SyncPreflight,
    SyncSpawn,
    SyncStore,
)
from audapack.provider_capabilities import PROVIDERS, WindowStartSemantics

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
PROOFS = SyncPreflight.REQUIRED


def members(count=2, *, same_payload=True):
    return [SyncMemberSpec(f"account-{n}", f"launcher-{n}", "codex", "model", "high",
                           "a" * 64 if same_payload else str(n) * 64)
            for n in range(count)]


def capable(_member):
    return replace(PROVIDERS["codex"], supports_sync=True,
                   window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE)


def group(store, count=2, *, same_payload=True):
    specs = members(count, same_payload=same_payload)
    identity = store.create("prepared", "event", "five_hour", "ALL_READY",
                            specs, NOW)
    return identity, specs


def test_sync_group_is_durable_idempotent_and_has_member_rows(tmp_path):
    store = SyncStore(tmp_path / "sync.db")
    group_id, specs = group(store, 3, same_payload=False)
    restarted = SyncStore(store.path)
    assert restarted.create("prepared", "event", "five_hour", "ALL_READY",
                            specs, NOW) == group_id
    saved = restarted.get(group_id)
    assert saved["state"] == "PENDING" and len(saved["members"]) == 3
    assert len({item["payload_sha256"] for item in saved["members"]}) == 3
    with pytest.raises(ValueError, match="different members"):
        restarted.create("prepared", "event", "five_hour", "ALL_READY",
                         members(2), NOW)
    with pytest.raises(ValueError, match="distinct accounts"):
        store.create("p2", "event", "five_hour", "ALL_READY", [specs[0], specs[0]], NOW)


@pytest.mark.parametrize("semantics", [WindowStartSemantics.FIXED,
                                       WindowStartSemantics.UNKNOWN])
def test_fixed_or_unknown_window_cannot_reach_release(tmp_path, semantics):
    store = SyncStore(tmp_path / "sync.db")
    group_id, _ = group(store)
    launches = []
    coordinator = SyncCoordinator(
        store, lambda _member: replace(capable(None), window_start_semantics=semantics),
        lambda _member: SyncPreflight(PROOFS), lambda member: launches.append(member),
    )
    assert not coordinator.prepare(group_id)
    assert store.get(group_id)["state"] == "WAITING"
    assert coordinator.release(group_id, NOW) is None
    assert not launches


def test_one_failed_preflight_keeps_all_members_before_barrier(tmp_path):
    store = SyncStore(tmp_path / "sync.db")
    group_id, _ = group(store, 3)
    launches = []
    coordinator = SyncCoordinator(
        store, capable,
        lambda member: SyncPreflight(PROOFS if member.account_id != "account-2"
                                     else PROOFS - {"process_capacity_available"},
                                     "existing process"),
        lambda member: launches.append(member),
    )
    assert not coordinator.prepare(group_id)
    assert [m["preflight_state"] for m in store.get(group_id)["members"]] == [
        "READY", "READY", "BLOCKED"]
    assert coordinator.release(group_id, NOW) is None
    assert not launches


def test_three_member_release_is_concurrent_and_records_separate_drifts(tmp_path):
    store = SyncStore(tmp_path / "sync.db")
    group_id, _ = group(store, 3)
    seen = []
    lock = threading.Lock()

    def launch(member):
        with lock:
            seen.append((member.account_id, threading.get_ident()))
        number = int(member.account_id[-1])
        return SyncSpawn(100 + number, (NOW + timedelta(milliseconds=number * 110)).isoformat())

    coordinator = SyncCoordinator(store, capable, lambda _member: SyncPreflight(PROOFS), launch)
    assert coordinator.prepare(group_id)
    result = SyncCoordinator(SyncStore(store.path), capable,
                             lambda _member: SyncPreflight(PROOFS), launch).release(group_id, NOW)
    assert result["state"] == "LAUNCHED"
    assert result["process_skew_ms"] == pytest.approx(220)
    assert result["quota_skew_ms"] is None
    assert len(seen) == len({item[0] for item in seen}) == 3
    assert len({item[1] for item in seen}) >= 2
    assert coordinator.release(group_id, NOW) is None
    assert store.record_window(group_id, "account-0", NOW.isoformat())
    assert store.record_window(group_id, "account-1", (NOW + timedelta(seconds=2)).isoformat())
    assert store.record_window(group_id, "account-2", (NOW + timedelta(seconds=3)).isoformat())
    assert store.get(group_id)["quota_skew_ms"] == pytest.approx(3000)


def test_post_barrier_failure_is_partial_and_never_retries_started_member(tmp_path):
    store = SyncStore(tmp_path / "sync.db")
    group_id, _ = group(store)
    launched = []

    def launch(member):
        launched.append(member.account_id)
        if member.account_id == "account-1":
            raise RuntimeError("response lost after possible spawn")
        return SyncSpawn(101, NOW.isoformat())

    coordinator = SyncCoordinator(store, capable, lambda _member: SyncPreflight(PROOFS), launch)
    assert coordinator.prepare(group_id)
    result = coordinator.release(group_id, NOW)
    assert result["state"] == "PARTIAL"
    assert [member["state"] for member in result["members"]] == ["LAUNCHED", "UNCERTAIN"]
    assert SyncCoordinator(SyncStore(store.path), capable,
                           lambda _member: SyncPreflight(PROOFS), launch).release(group_id, NOW) is None
    assert sorted(launched) == ["account-0", "account-1"]


def test_interrupted_release_fences_all_unrecorded_members(tmp_path):
    store = SyncStore(tmp_path / "sync.db")
    group_id, _ = group(store)
    assert store.record_preflight(group_id, {m.account_id: SyncPreflight(PROOFS)
                                             for m in members()})
    assert store.claim_release(group_id, NOW)
    assert store.record_spawn(group_id, "account-0", SyncSpawn(101, NOW.isoformat()))
    restarted = SyncStore(store.path)
    assert restarted.recover_interrupted_release(group_id)
    saved = restarted.get(group_id)
    assert saved["state"] == "PARTIAL"
    assert [member["state"] for member in saved["members"]] == ["LAUNCHED", "UNCERTAIN"]
    assert restarted.claim_release(group_id, NOW) is None


def test_prepared_store_sync_members_crud_and_validation(tmp_path):
    store = PreparedStore(tmp_path / "resources.sqlite3")
    job = PreparedJob("sync-job", "sync job", "project", "launcher-0", "account-0",
                      Trigger.SYNC, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)

    m0 = PreparedSyncMember("sync-job", 0, "account-0", "launcher-0", "model-a", "high",
                            "user_command", {"text": "ping"}, True)
    m1 = PreparedSyncMember("sync-job", 1, "account-1", "launcher-1", "model-b", "low",
                            "user_command", {"text": "pong"}, True)
    m2 = PreparedSyncMember("sync-job", 2, "account-2", "launcher-2", "model-c", "medium",
                            "user_command", {"text": "pang"}, True)

    with pytest.raises(ValueError, match="at least two distinct accounts"):
        store.save_sync_members("sync-job", [m0])

    m_dup = replace(m1, account_id="account-0")
    with pytest.raises(ValueError, match="distinct accounts"):
        store.save_sync_members("sync-job", [m0, m_dup])

    # Save 2 members
    store.save_sync_members("sync-job", [m0, m1])
    saved = store.get_sync_members("sync-job")
    assert len(saved) == 2
    assert saved[0].account_id == "account-0" and saved[0].model == "model-a"
    assert saved[1].account_id == "account-1" and saved[1].effort == "low"

    # Save 3 members (updates existing)
    store.save_sync_members("sync-job", [m0, m1, m2])
    assert len(store.get_sync_members("sync-job")) == 3

    # Restart retains membership
    restarted = PreparedStore(store.path)
    reloaded = restarted.get_sync_members("sync-job")
    assert len(reloaded) == 3
    assert [m.account_id for m in reloaded] == ["account-0", "account-1", "account-2"]

    # Delete members
    restarted.delete_sync_members("sync-job")
    assert restarted.get_sync_members("sync-job") == []


def test_prepared_store_sync_claim_reserves_all_members_and_releases(tmp_path):
    store = PreparedStore(tmp_path / "resources.sqlite3")
    job = PreparedJob("sync-job", "sync job", "project", "launcher-0", "account-0",
                      Trigger.SYNC, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)
    m0 = PreparedSyncMember("sync-job", 0, "account-0", "launcher-0", "model-a", "high", "", {}, True)
    m1 = PreparedSyncMember("sync-job", 1, "account-1", "launcher-1", "model-b", "low", "", {}, True)
    store.save_sync_members("sync-job", [m0, m1])

    # Claim SYNC job
    execution_id = store.claim("sync-job", "ev-1", "owner-1", NOW,
                               selected_account_id="account-0", selected_launcher_id="launcher-0")
    assert execution_id is not None

    # Check reservations for all members
    res = store.reservations(NOW)
    assert "account-0" in res and res["account-0"]["owner_execution"] == execution_id
    assert "account-1" in res and res["account-1"]["owner_execution"] == execution_id

    # Another job attempting to claim account-1 fails due to active reservation
    job2 = PreparedJob("other-job", "other", "project", "launcher-1", "account-1",
                       Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                       {"at": NOW.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job2)
    assert store.claim("other-job", "ev-2", "owner-2", NOW,
                       selected_account_id="account-1") is None

    # Advancing execution to DONE releases all reservations
    assert store.advance(execution_id, "owner-1", 1, JobState.CLAIMED, JobState.DONE, now=NOW)
    res_after = store.reservations(NOW)
    assert "account-0" not in res_after
    assert "account-1" not in res_after


def test_prepared_store_reown_sync(tmp_path):
    store = PreparedStore(tmp_path / "resources.sqlite3")
    job = PreparedJob("sync-job", "sync job", "project", "launcher-0", "account-0",
                      Trigger.SYNC, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)
    m0 = PreparedSyncMember("sync-job", 0, "account-0", "launcher-0", "m", "h", "", {}, True)
    m1 = PreparedSyncMember("sync-job", 1, "account-1", "launcher-1", "m", "h", "", {}, True)
    store.save_sync_members("sync-job", [m0, m1])

    execution_id = store.claim("sync-job", "ev-1", "owner-1", NOW, selected_account_id="account-0")
    assert execution_id is not None

    # Re-owning active execution by new owner increments generation
    res = store.reown_sync(execution_id, "owner-2", NOW)
    assert res == ("CLAIMED", 2)

    # Re-owning by same owner with fresh lease keeps generation
    res_same = store.reown_sync(execution_id, "owner-2", NOW + timedelta(seconds=10))
    assert res_same == ("CLAIMED", 2)

    # Advance to terminal state (DONE)
    store.advance(execution_id, "owner-2", 2, JobState.CLAIMED, JobState.DONE, now=NOW)
    # Re-owning inactive execution returns None
    assert store.reown_sync(execution_id, "owner-3", NOW) is None
    # Re-owning non-existent execution returns None
    assert store.reown_sync("non-existent", "owner-3", NOW) is None

