import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audapack.account_registry import AccountIdentity, AccountRegistry
from audapack.config import AppConfig, LauncherConfig
from audapack.limits import LimitSnapshot, LimitStore, LimitWindow
from audapack.models import Project
from audapack.prepared import (
    JobState,
    Payload,
    PreparedJob,
    PreparedStore,
    Trigger,
)
from audapack.prepared_prime import PrimeStore
from audapack.prepared_sync import PreparedSyncMember, SyncStore
from audapack.prepared_worker import PreparedWorker

NOW_UTC = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


def _make_worker(tmp_path: Path, store: PreparedStore, current_time: list[datetime],
                 accounts: list[AccountIdentity] | None = None) -> PreparedWorker:
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [
        LauncherConfig("codex2", "Codex 2", "C2"),
        LauncherConfig("claude2", "Claude 2", "CL2"),
    ]
    registry = AccountRegistry(store.path)
    if accounts:
        registry.upsert(accounts)
    limits = LimitStore(store.path)
    worker = PreparedWorker(
        config,
        clock=lambda: current_time[0],
        account_registry=registry,
        limit_store=limits,
        prepared_store=store,
        sync_store=SyncStore(store.path),
        prime_store=PrimeStore(store.path),
    )
    worker._known_accounts = lambda: {a.account_id: a for a in (accounts or [])}
    worker._probe_due = lambda _accs: None
    worker._pool.submit = lambda fn, *args: fn(*args)
    worker.coordinator.refresh = lambda acc, force=False: (
        worker.limits.get(acc.account_id)[0] if worker.limits.get(acc.account_id) else None
    )
    return worker


def test_timezone_offset_equivalence(tmp_path):
    path = tmp_path / "clock.sqlite3"
    store = PreparedStore(path)
    # 15:30 at UTC+05:30 == 10:00 UTC
    tz_ist = timezone(timedelta(hours=5, minutes=30))
    at_ist = datetime(2026, 9, 23, 15, 30, 0, tzinfo=tz_ist)

    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-tz", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": at_ist.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)

    current = [NOW_UTC - timedelta(seconds=1)]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    snapshot = LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), NOW_UTC.isoformat(), "test")
    limits.put(snapshot, NOW_UTC + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda _j, _a, ex, _g: launches.append(ex)

    # 1 second before: not due
    assert worker.tick() == NOW_UTC
    assert len(launches) == 0

    # At exact UTC equivalent: claims and launches
    current[0] = NOW_UTC
    worker.tick()
    assert len(launches) == 1
    receipt = store.receipt(job.prepared_id, store.list()[0].next_due_at)
    assert receipt is not None or len(store.active_receipts()) == 1
    worker.stop()


def test_backward_wall_clock_never_duplicates_execution(tmp_path):
    path = tmp_path / "backward.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-bw", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)

    current = [NOW_UTC]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), NOW_UTC.isoformat(), "test"), NOW_UTC + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: (
        launches.append(ex),
        store.advance(ex, worker.scheduler.owner_id, g, JobState.CLAIMED, JobState.DONE, now=current[0]),
    )

    worker.tick()
    assert len(launches) == 1

    # Wall clock jumps backward by 2 hours
    current[0] = NOW_UTC - timedelta(hours=2)
    worker.tick()
    # No second launch
    assert len(launches) == 1

    # Wall clock moves forward through due time again
    current[0] = NOW_UTC
    worker.tick()
    assert len(launches) == 1

    # Check total executions in DB
    with closing(sqlite3.connect(path)) as db:
        count = db.execute("SELECT COUNT(*) FROM prepared_executions WHERE prepared_id='job-bw'").fetchone()[0]
        assert count == 1
    worker.stop()


def test_every_reset_backward_clock_refuses_duplicate_for_same_reset(tmp_path):
    path = tmp_path / "reset_bw.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    reset_time = NOW_UTC + timedelta(hours=1)
    job = PreparedJob("job-reset-bw", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_RESET, Payload.USER_COMMAND, {"text": "cc"},
                      {"window_id": "five_hour", "armed_reset_at": reset_time.isoformat(),
                       "armed_window_id": "five_hour"},
                      enabled=True, state=JobState.ARMED, recurrence="EVERY_RESET",
                      safety_delay_seconds=60)
    store.save(job)

    due_time = reset_time + timedelta(seconds=60)
    current = [due_time]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    snapshot = LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=1.0,
                    reset_at=(reset_time + timedelta(hours=5)).isoformat()),
    ), due_time.isoformat(), "test")
    limits.put(snapshot, due_time + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: (
        launches.append(ex),
        store.advance(ex, worker.scheduler.owner_id, g, JobState.CLAIMED, JobState.DONE, now=current[0]),
    )

    worker.tick()
    assert len(launches) == 1

    # Clock moves backward before the reset
    current[0] = reset_time - timedelta(minutes=10)
    # Fake snapshot still reporting original reset_time
    stale_snapshot = LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.0,
                    reset_at=reset_time.isoformat()),
    ), current[0].isoformat(), "test")
    limits.put(stale_snapshot, current[0] + timedelta(hours=1), 0)

    worker.tick()
    assert len(launches) == 1

    # Clock reaches due time again
    current[0] = due_time
    limits.put(snapshot, current[0] + timedelta(hours=1), 0)
    worker.tick()
    # Must NOT launch again for the same reset event!
    assert len(launches) == 1

    with closing(sqlite3.connect(path)) as db:
        count = db.execute("SELECT COUNT(*) FROM prepared_executions WHERE prepared_id='job-reset-bw'").fetchone()[0]
        assert count == 1
    worker.stop()


def test_sleep_within_catchup_window_claims_and_launches(tmp_path):
    path = tmp_path / "sleep_catchup.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-sleep-ok", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, catch_up_seconds=900,
                      enabled=True, state=JobState.ARMED)
    store.save(job)

    # System slept 09:59 -> 10:05 (due was 10:00, catchup is 900s = 15m)
    current = [NOW_UTC + timedelta(minutes=5)]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), current[0].isoformat(), "test"), current[0] + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: launches.append(ex)
    worker.tick()

    assert len(launches) == 1
    worker.stop()


def test_sleep_past_catchup_deadline_settles_missed_without_launch(tmp_path):
    path = tmp_path / "sleep_missed.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-sleep-missed", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, catch_up_seconds=900,
                      enabled=True, state=JobState.ARMED)
    store.save(job)

    # System slept 09:59 -> 10:30 (due was 10:00, deadline was 10:15)
    current = [NOW_UTC + timedelta(minutes=30)]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), current[0].isoformat(), "test"), current[0] + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: launches.append(ex)
    worker.tick()

    assert len(launches) == 0
    updated = store.get("job-sleep-missed")
    assert updated.state == JobState.MISSED
    assert updated.enabled is False
    worker.stop()


def test_strict_time_policy_misses_immediately_when_exhausted(tmp_path):
    path = tmp_path / "strict_time.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-strict", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat(), "availability_policy": "STRICT_TIME"},
                      catch_up_seconds=900, enabled=True, state=JobState.ARMED)
    store.save(job)

    current = [NOW_UTC]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.0),
    ), current[0].isoformat(), "test"), current[0] + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: launches.append(ex)
    worker.tick()

    assert len(launches) == 0
    updated = store.get("job-strict")
    assert updated.state == JobState.MISSED
    assert updated.enabled is False
    worker.stop()


def test_availability_wait_catches_up_when_limit_clears(tmp_path):
    path = tmp_path / "avail_wait.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-avail-wait", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()},  # default availability policy = wait
                      catch_up_seconds=900, enabled=True, state=JobState.ARMED)
    store.save(job)

    current = [NOW_UTC]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    # Initially exhausted
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.0),
    ), current[0].isoformat(), "test"), current[0] + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: launches.append(ex)
    worker.tick()

    # At 10:00: waiting limit, not missed, not launched
    assert len(launches) == 0
    assert store.get("job-avail-wait").state == JobState.WAITING_LIMIT

    # At 10:05: account limit clears (becomes AVAILABLE)
    current[0] = NOW_UTC + timedelta(minutes=5)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.5),
    ), current[0].isoformat(), "test"), current[0] + timedelta(hours=1), 0)

    worker.tick()
    assert len(launches) == 1
    worker.stop()


def test_editing_trigger_time_recalculates_due_and_preserves_safety(tmp_path):
    path = tmp_path / "edit_trigger.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-edit", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)

    # Edit trigger time from 10:00 to 10:30
    new_at = NOW_UTC + timedelta(minutes=30)
    edited = replace(job, trigger_config={"at": new_at.isoformat()}, next_due_at=new_at.isoformat())
    store.save(edited)

    current = [NOW_UTC]
    worker = _make_worker(tmp_path, store, current, [account])
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), current[0].isoformat(), "test"), current[0] + timedelta(hours=1), 0)

    launches = []
    worker._deliver = lambda j, a, ex, g: launches.append(ex)

    # At 10:00: should NOT launch
    worker.tick()
    assert len(launches) == 0

    # At 10:30: claims and launches
    current[0] = new_at
    worker.tick()
    assert len(launches) == 1
    worker.stop()


def test_editing_forbidden_during_active_execution(tmp_path):
    path = tmp_path / "edit_active.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-active", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)

    # Claim an execution
    claim_id = store.claim("job-active", "event-1", "worker-1", NOW_UTC)
    assert claim_id is not None

    # Attempting to save edits to active job must raise RuntimeError
    with pytest.raises(RuntimeError, match="cannot edit job during active execution"):
        store.save(replace(job, name="renamed"))


def test_sync_and_prime_sleep_through_catchup_deadline_misses(tmp_path, monkeypatch):
    from audapack.provider_capabilities import PROVIDERS, WindowStartSemantics
    fake_cap = replace(PROVIDERS["codex"], supports_sync=True, supports_prime=True,
                       window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE)
    monkeypatch.setattr("audapack.prepared_worker.PROVIDERS", {"codex": fake_cap})

    path = tmp_path / "sync_prime_missed.sqlite3"
    store = PreparedStore(path)
    a1 = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path), ("codex1",), "test", NOW_UTC.isoformat())
    a2 = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path), ("codex2",), "test", NOW_UTC.isoformat())

    sync_job = PreparedJob(
        "job-sync-missed", "sync", "project", "codex1", "codex:1",
        Trigger.SYNC, Payload.USER_COMMAND, {"text": "cc"},
        {"at": NOW_UTC.isoformat()}, catch_up_seconds=900,
        enabled=True, state=JobState.ARMED,
    )
    store.save(sync_job)
    store.save_sync_members("job-sync-missed", [
        PreparedSyncMember("job-sync-missed", 0, a1.account_id, a1.launcher_ids[0]),
        PreparedSyncMember("job-sync-missed", 1, a2.account_id, a2.launcher_ids[0]),
    ])

    prime_job = PreparedJob(
        "job-prime-missed", "prime", "project", "codex1", "codex:1",
        Trigger.PRIME, Payload.USER_COMMAND, {"text": "cc"},
        {"at": NOW_UTC.isoformat()}, catch_up_seconds=900,
        enabled=True, state=JobState.ARMED,
    )
    store.save(prime_job)

    # Slept past 15m catchup deadline -> 10:30
    current = [NOW_UTC + timedelta(minutes=30)]
    worker = _make_worker(tmp_path, store, current, [a1, a2])
    worker.sync_coordinator = object()
    worker.prime_coordinator = object()

    worker.tick()

    assert store.get("job-sync-missed").state == JobState.MISSED
    assert store.get("job-sync-missed").enabled is False
    assert store.get("job-prime-missed").state == JobState.MISSED
    assert store.get("job-prime-missed").enabled is False
    worker.stop()


def test_bridge_restart_before_and_during_due(tmp_path):
    path = tmp_path / "restart.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-restart", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), NOW_UTC.isoformat(), "test"), NOW_UTC + timedelta(hours=1), 0)

    # Worker 1: runs before due time (09:50)
    current = [NOW_UTC - timedelta(minutes=10)]
    w1 = _make_worker(tmp_path, store, current, [account])
    launches_w1 = []
    w1._deliver = lambda j, a, ex, g: launches_w1.append(ex)
    assert w1.tick() == NOW_UTC
    assert len(launches_w1) == 0
    w1.stop()

    # Worker 2: restarts at due time (10:00)
    current[0] = NOW_UTC
    w2 = _make_worker(tmp_path, store, current, [account])
    launches_w2 = []
    w2._deliver = lambda j, a, ex, g: (
        launches_w2.append(ex),
        store.advance(ex, w2.scheduler.owner_id, g, JobState.CLAIMED, JobState.DONE, now=current[0]),
    )
    w2.tick()
    assert len(launches_w2) == 1
    w2.stop()

    # Worker 3: restarts after due time (10:05)
    current[0] = NOW_UTC + timedelta(minutes=5)
    w3 = _make_worker(tmp_path, store, current, [account])
    launches_w3 = []
    w3._deliver = lambda j, a, ex, g: launches_w3.append(ex)
    w3.tick()
    assert len(launches_w3) == 0
    w3.stop()


def test_test_now_does_not_consume_or_alter_scheduled_event(tmp_path):
    path = tmp_path / "test_now_sched.sqlite3"
    store = PreparedStore(path)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW_UTC.isoformat())
    job = PreparedJob("job-tn-sched", "continue", "project", "codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW_UTC.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)
    limits = LimitStore(path)
    limits.put(LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), NOW_UTC.isoformat(), "test"), NOW_UTC + timedelta(hours=1), 0)

    # At 09:50: Test Now runs
    claim_res = store.claim_test("job-tn-sched", "operator-ui", NOW_UTC - timedelta(minutes=10))
    assert claim_res is not None
    test_exec_id, test_event_id = claim_res
    assert store.advance(test_exec_id, "operator-ui", 1, JobState.CLAIMED, JobState.DONE,
                         now=NOW_UTC - timedelta(minutes=10))

    # Scheduled job must still be ARMED and enabled
    assert store.get("job-tn-sched").enabled is True
    assert store.get("job-tn-sched").state == JobState.ARMED

    # At 10:00: Worker ticks and claims scheduled event
    current = [NOW_UTC]
    worker = _make_worker(tmp_path, store, current, [account])
    launches = []
    worker._deliver = lambda j, a, ex, g: launches.append(ex)
    worker.tick()

    assert len(launches) == 1
    assert launches[0] != test_exec_id
    worker.stop()

