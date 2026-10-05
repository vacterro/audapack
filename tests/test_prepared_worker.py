import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from audapack.account_registry import AccountIdentity, AccountRegistry
from audapack.auto_account import AutoCandidate, AutoSelection
from audapack.config import AppConfig, LauncherConfig
from audapack.limits import LimitSnapshot, LimitStore, LimitWindow
from audapack.models import Project
from audapack.prepared import (
    JobState,
    Payload,
    PreparedJob,
    PreparedStore,
    PreparedSyncMember,
    Trigger,
    evaluate_trigger,
)
from audapack.prepared_prime import PrimeStore, PrimeTarget
from audapack.prepared_sync import SyncStore
from audapack.prepared_worker import PreparedWorker
from audapack.provider_capabilities import PROVIDERS, WindowStartSemantics

NOW = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)


def test_worker_verifies_once_at_due_and_claims_once(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts", lambda *_args, **_kwargs: [account])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    limits = LimitStore(path)
    registry = AccountRegistry(path)
    at = NOW + timedelta(minutes=2)
    job = PreparedJob("job", "continue", "project", "main_codex2", account.account_id,
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": at.isoformat()}, enabled=True, state=JobState.ARMED)
    jobs.save(job)
    previous = LimitSnapshot(account.account_id, (), NOW.isoformat(), "test")
    limits.put(previous, at + timedelta(minutes=15), 0)
    current = [NOW]
    worker = PreparedWorker(config, clock=lambda: current[0], account_registry=registry,
                            limit_store=limits, prepared_store=jobs)
    verified = LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
        LimitWindow("weekly", "weekly", "Weekly", remaining_ratio=0.8),
    ), at.isoformat(), "test")
    probes = []
    def refresh(_account, *, force=False):
        probes.append(force)
        if force:
            limits.put(verified, at + timedelta(minutes=15), 0)
            return verified
        return limits.get(account.account_id)[0]
    worker.coordinator.refresh = refresh
    launched = []
    worker._deliver = lambda _job, _account, execution, _generation: launched.append(execution)
    worker._pool.submit = lambda fn, *args: fn(*args)
    assert worker.tick() == at
    assert not launched
    current[0] = at
    worker.tick()
    worker.tick()
    assert probes.count(True) == 1
    assert len(launched) == 1
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT state FROM prepared_executions WHERE prepared_id='job'").fetchone()[0] == "CLAIMED"
    worker.stop()


def test_missing_account_wait_is_visible_and_reappearance_rearms_job(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    present = [account]
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: list(present))
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    registry = AccountRegistry(path)
    jobs.save(PreparedJob("job", "continue", "project", "main_codex2",
                          account.account_id, Trigger.ON_TIME, Payload.USER_COMMAND,
                          {"text": "cc"}, {"at": (NOW + timedelta(minutes=2)).isoformat()},
                          enabled=True, state=JobState.ARMED))
    worker = PreparedWorker(config, clock=lambda: NOW, account_registry=registry,
                            limit_store=LimitStore(path), prepared_store=jobs)
    worker._probe_due = lambda _accounts: None
    worker.tick()
    present.clear()
    worker.tick()
    assert jobs.get("job").state == JobState.WAITING_LIMIT
    assert jobs.get("job").waiting_reason == "account not discovered"
    assert worker.status_snapshot["prepared"]["waiting_account"] == 1
    assert worker.status_snapshot["accounts"]["discovered"] == 0
    present.append(account)
    worker.tick()
    assert jobs.get("job").state == JobState.WAITING_TRIGGER
    assert jobs.get("job").waiting_reason == "time not reached"
    assert worker.status_snapshot["prepared"]["waiting_account"] == 0
    worker.stop()


def test_active_manual_test_keeps_parent_job_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [])
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    jobs.save(PreparedJob("job", "continue", "project", "main_codex2",
                          "codex:2", Trigger.ON_TIME, Payload.USER_COMMAND,
                          {"text": "cc"}, {"at": (NOW + timedelta(minutes=2)).isoformat()},
                          enabled=True, state=JobState.ARMED))
    jobs.claim_test("job", "tester", NOW)
    worker = PreparedWorker(AppConfig(), clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs)
    worker._probe_due = lambda _accounts: None
    worker.tick()
    assert jobs.get("job").state == JobState.ARMED
    assert jobs.get("job").waiting_reason == ""
    worker.stop()


def test_model_with_two_quota_pools_waits_for_mapping_then_uses_mapped_pool(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    limits = LimitStore(path)
    jobs.save(PreparedJob("job", "reserve model", "project", "main_codex2",
                          account.account_id, Trigger.ON_TIME, Payload.USER_COMMAND,
                          {"text": "cc"}, {"at": NOW.isoformat(),
                                             "verified_event": NOW.isoformat()},
                          model="synthetic-reserve-model", enabled=True, state=JobState.ARMED))
    snapshot = LimitSnapshot(account.account_id, (
        LimitWindow("five_hour@codex", "five_hour", "Codex", remaining_ratio=0,
                    quota_bucket="codex"),
        LimitWindow("five_hour@reserve", "five_hour", "Reserve", remaining_ratio=0.8,
                    quota_bucket="reserve"),
    ), NOW.isoformat(), "codex_app_server")
    limits.put(snapshot, NOW + timedelta(minutes=15), 0)
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=limits, prepared_store=jobs)
    worker._probe_due = lambda _accounts: None
    launches = []
    worker._deliver = lambda _job, _account, execution, _generation: launches.append(execution)
    worker._pool.submit = lambda fn, *args: fn(*args)
    worker.tick()
    assert jobs.get("job").waiting_reason == "model quota bucket unknown"
    assert jobs.receipt("job", "unused") is None
    assert not launches
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], quota_bucket_mapping={"synthetic-reserve-model": "reserve"}))
    worker.tick()
    assert len(launches) == 1
    assert jobs.get("job").state == JobState.CLAIMED
    worker.stop()


def test_thirty_second_worker_wake_does_not_probe_every_wake(tmp_path):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    path = tmp_path / "resources.sqlite3"
    current = [NOW]
    worker = PreparedWorker(AppConfig(), clock=lambda: current[0],
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=PreparedStore(path))
    worker._known_accounts = lambda: {account.account_id: account}
    calls = []

    class Adapter:
        provider_id = "codex"

        def probe_limits(self, identity):
            calls.append(current[0])
            return LimitSnapshot(identity.account_id, (), current[0].isoformat(), "fake")

    worker.coordinator.adapters["codex"] = Adapter()

    def inline(fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    worker._probe_pool.submit = inline
    for step in range(30):
        current[0] = NOW + timedelta(seconds=30 * step)
        worker.tick()
    assert calls == [NOW]
    current[0] = NOW + timedelta(minutes=15)
    worker.tick()
    assert calls == [NOW, current[0]]
    worker.stop()


def test_auto_on_time_claims_concrete_account_without_changing_parent(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    limits = LimitStore(path)
    jobs.save(PreparedJob("auto-job", "continue", "project", "AUTO", "AUTO",
                          Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                          {"at": NOW.isoformat()}, enabled=True, state=JobState.ARMED))
    snapshot = LimitSnapshot(account.account_id, (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.8),
    ), NOW.isoformat(), "test")
    limits.put(snapshot, NOW + timedelta(minutes=15), 0)
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=limits, prepared_store=jobs)
    worker._probe_due = lambda _accounts: None
    choice = AutoSelection(AutoCandidate(account, "main_codex2", snapshot, None, 0),
                           "AUTO test selection")
    worker._auto_candidates = lambda *_args: ([choice], "")
    worker.coordinator.refresh = lambda *_args, **_kwargs: snapshot
    delivered = []
    worker._deliver = lambda job, _account, execution, _generation: delivered.append(
        (job.account_id, job.launcher_id, execution))
    worker._pool.submit = lambda fn, *args: fn(*args)
    worker.tick()
    assert len(delivered) == 1
    assert delivered[0][:2] == (account.account_id, "main_codex2")
    assert jobs.get("auto-job").account_id == "AUTO"
    event_id = evaluate_trigger(jobs.get("auto-job"), snapshot, NOW).event_id
    receipt = jobs.receipt("auto-job", event_id)
    assert receipt["account_id"] == account.account_id
    assert receipt["selection_reason"] == "AUTO test selection"
    assert account.account_id in jobs.reservations(NOW)
    worker.stop()


def _gate_worker(tmp_path, monkeypatch, account, job, config=None):
    if account is not None:
        monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                            lambda *_args, **_kwargs: [account])
    else:
        monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                            lambda *_args, **_kwargs: [])
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    jobs.save(job)
    worker = PreparedWorker(config or AppConfig(), clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs)
    worker._probe_due = lambda _accounts: None
    return worker, jobs


def test_audit_job_waits_with_exact_adapter_reason(tmp_path, monkeypatch):
    job = PreparedJob("audit-job", "audit", "project", "main_codex2",
                      "codex:2", Trigger.ON_TIME, Payload.AUDIT,
                      {"profile_id": "quick3"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    worker, jobs = _gate_worker(tmp_path, monkeypatch, None, job)
    worker.tick()
    assert jobs.get("audit-job").state == JobState.WAITING_LIMIT
    assert jobs.get("audit-job").waiting_reason == (
        "Audit unavailable: AuditRunCoordinator runtime not connected")
    worker.stop()


def test_sync_job_wait_names_exact_provider_semantics(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    job = PreparedJob("sync-job", "sync", "project", "main_codex2",
                      account.account_id, Trigger.SYNC, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    worker, jobs = _gate_worker(tmp_path, monkeypatch, account, job, config)
    worker.tick()
    assert jobs.get("sync-job").state == JobState.WAITING_LIMIT
    assert jobs.get("sync-job").waiting_reason == (
        "SYNC unavailable: codex window start semantics UNKNOWN")
    # A proven-capable provider changes the reason to the missing runtime
    # instead of pretending the semantics are the blocker.
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_sync=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))
    worker.tick()
    assert jobs.get("sync-job").waiting_reason == (
        "SYNC unavailable: worker runtime not connected")
    # Undiscovered account keeps a truthful capability reason too.
    worker._known_accounts = lambda: {}
    worker.tick()
    assert jobs.get("sync-job").waiting_reason == (
        "SYNC unavailable: provider capability unknown")
    worker.stop()


def test_prime_job_wait_names_exact_provider_semantics(tmp_path, monkeypatch):
    account = AccountIdentity("claude:1", "claude", "Claude 1", str(tmp_path),
                              ("claude1",), "test", NOW.isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("claude1", "Claude 1", "C1")]
    job = PreparedJob("prime-job", "prime", "project", "claude1",
                      account.account_id, Trigger.PRIME, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    worker, jobs = _gate_worker(tmp_path, monkeypatch, account, job, config)
    worker.tick()
    assert jobs.get("prime-job").state == JobState.WAITING_LIMIT
    assert jobs.get("prime-job").waiting_reason == (
        "PRIME unavailable: claude window start semantics UNKNOWN")
    worker.stop()


def test_sync_job_waits_when_fewer_than_two_members(tmp_path, monkeypatch):
    account1 = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path),
                               ("main_codex1",), "test", NOW.isoformat())
    account2 = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account1, account2])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_sync=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex1", "Codex 1", "C1"),
                        LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("sync-job", "sync", "project", "main_codex1",
                      account1.account_id, Trigger.SYNC, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    jobs.save(job)
    sync_store = SyncStore(path)
    fake_coordinator = object()

    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            sync_store=sync_store, sync_coordinator=fake_coordinator)
    worker._probe_due = lambda _accounts: None
    worker.tick()
    assert jobs.get("sync-job").state == JobState.WAITING_LIMIT
    assert jobs.get("sync-job").waiting_reason == "SYNC requires at least two distinct accounts"
    worker.stop()


def test_worker_sync_successful_twophase_release(tmp_path, monkeypatch):
    account1 = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path),
                               ("main_codex1",), "test", NOW.isoformat())
    account2 = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account1, account2])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_sync=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex1", "Codex 1", "C1"),
                        LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("sync-job", "sync", "project", "main_codex1",
                      account1.account_id, Trigger.SYNC, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    jobs.save(job)
    m1 = PreparedSyncMember("sync-job", 0, "codex:1", "main_codex1", "m", "h", "", {}, True)
    m2 = PreparedSyncMember("sync-job", 1, "codex:2", "main_codex2", "m", "h", "", {}, True)
    jobs.save_sync_members("sync-job", [m1, m2])

    sync_store = SyncStore(path)

    class MockSyncCoordinator:
        def __init__(self):
            self.prepared_groups = []
            self.released_groups = []

        def prepare(self, group_id):
            self.prepared_groups.append(group_id)
            return True

        def release(self, group_id, now):
            self.released_groups.append((group_id, now))
            return {"state": "LAUNCHED", "process_skew_ms": 42}

    mock_coord = MockSyncCoordinator()
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            sync_store=sync_store, sync_coordinator=mock_coord)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)

    worker.tick()
    assert len(mock_coord.prepared_groups) == 1
    assert len(mock_coord.released_groups) == 1
    assert jobs.get("sync-job").state == JobState.DONE
    # Reservations should be released
    assert not jobs.reservations(NOW)

    # Subsequent tick does not re-launch
    worker.tick()
    assert len(mock_coord.prepared_groups) == 1
    worker.stop()


def test_worker_sync_preflight_failure_launches_zero(tmp_path, monkeypatch):
    account1 = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path),
                               ("main_codex1",), "test", NOW.isoformat())
    account2 = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account1, account2])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_sync=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex1", "Codex 1", "C1"),
                        LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("sync-job", "sync", "project", "main_codex1",
                      account1.account_id, Trigger.SYNC, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    jobs.save(job)
    m1 = PreparedSyncMember("sync-job", 0, "codex:1", "main_codex1", "m", "h", "", {}, True)
    m2 = PreparedSyncMember("sync-job", 1, "codex:2", "main_codex2", "m", "h", "", {}, True)
    jobs.save_sync_members("sync-job", [m1, m2])

    sync_store = SyncStore(path)

    class FailingSyncCoordinator:
        def __init__(self):
            self.released = False

        def prepare(self, group_id):
            return False

        def release(self, group_id, now):
            self.released = True
            return {"state": "LAUNCHED"}

    coord = FailingSyncCoordinator()
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            sync_store=sync_store, sync_coordinator=coord)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)

    worker.tick()
    assert not coord.released
    assert jobs.get("sync-job").state == JobState.FAILED_RETRYABLE
    worker.stop()


def test_worker_sync_post_barrier_failure_yields_recovery_required(tmp_path, monkeypatch):
    account1 = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path),
                               ("main_codex1",), "test", NOW.isoformat())
    account2 = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account1, account2])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_sync=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex1", "Codex 1", "C1"),
                        LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("sync-job", "sync", "project", "main_codex1",
                      account1.account_id, Trigger.SYNC, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    jobs.save(job)
    m1 = PreparedSyncMember("sync-job", 0, "codex:1", "main_codex1", "m", "h", "", {}, True)
    m2 = PreparedSyncMember("sync-job", 1, "codex:2", "main_codex2", "m", "h", "", {}, True)
    jobs.save_sync_members("sync-job", [m1, m2])

    sync_store = SyncStore(path)

    class PartialSyncCoordinator:
        def prepare(self, group_id):
            return True

        def release(self, group_id, now):
            return {"state": "PARTIAL", "process_skew_ms": 200, "members": [
                {"account_id": "codex:1", "state": "LAUNCHED"},
                {"account_id": "codex:2", "state": "UNCERTAIN"},
            ]}

    coord = PartialSyncCoordinator()
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            sync_store=sync_store, sync_coordinator=coord)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)

    worker.tick()
    assert jobs.get("sync-job").state == JobState.RECOVERY_REQUIRED
    assert "partial" in jobs.get("sync-job").waiting_reason
    worker.stop()


def test_worker_sync_reconcile_interrupted_release(tmp_path, monkeypatch):
    account1 = AccountIdentity("codex:1", "codex", "Codex 1", str(tmp_path),
                               ("main_codex1",), "test", NOW.isoformat())
    account2 = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account1, account2])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_sync=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex1", "Codex 1", "C1"),
                        LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("sync-job", "sync", "project", "main_codex1",
                      account1.account_id, Trigger.SYNC, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    jobs.save(job)
    m1 = PreparedSyncMember("sync-job", 0, "codex:1", "main_codex1", "m", "h", "", {}, True)
    m2 = PreparedSyncMember("sync-job", 1, "codex:2", "main_codex2", "m", "h", "", {}, True)
    jobs.save_sync_members("sync-job", [m1, m2])

    sync_store = SyncStore(path)
    from audapack.prepared_sync import SyncMemberSpec, SyncPreflight
    specs = [
        SyncMemberSpec("codex:1", "main_codex1", "codex", "m", "h", "0" * 64),
        SyncMemberSpec("codex:2", "main_codex2", "codex", "m", "h", "0" * 64),
    ]
    group_id = sync_store.create("sync-job", "ev-1", "five_hour", "ALL_READY", specs, NOW)
    sync_store.record_preflight(group_id, {
        "codex:1": SyncPreflight(SyncPreflight.REQUIRED),
        "codex:2": SyncPreflight(SyncPreflight.REQUIRED),
    })
    # Simulate crash during release: group is RELEASING
    sync_store.claim_release(group_id, NOW)

    # Claim execution in prepared_store with expired lease
    past = NOW - timedelta(minutes=5)
    execution_id = jobs.claim("sync-job", "ev-1", "dead-worker", past,
                              selected_account_id="codex:1", selected_launcher_id="main_codex1")
    assert execution_id is not None
    jobs.advance(execution_id, "dead-worker", 1, JobState.CLAIMED, JobState.LAUNCHING, now=past)

    # Set lease expired
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE prepared_executions SET lease_expires_at=? WHERE execution_id=?",
                   (past.isoformat(), execution_id))
        db.commit()

    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            sync_store=sync_store, sync_coordinator=object())
    worker._probe_due = lambda _accounts: None

    worker.tick()
    # Interrupted release recovered: group is PARTIAL, execution is RECOVERY_REQUIRED
    assert sync_store.get(group_id)["state"] == "PARTIAL"
    assert jobs.get("sync-job").state == JobState.RECOVERY_REQUIRED
    receipt = jobs.receipt("sync-job", "ev-1")
    assert receipt["state"] == "RECOVERY_REQUIRED"
    worker.stop()


def test_worker_prime_successful_delivery(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_prime=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("prime-job", "prime", "project", "main_codex2",
                      account.account_id, Trigger.PRIME, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat(), "window_id": "five_hour"},
                      model="model-1", enabled=True, state=JobState.ARMED)
    jobs.save(job)
    prime_store = PrimeStore(path)

    class MockPrimeCoordinator:
        def __init__(self):
            self.executed = []

        def execute(self, prime_id, acc, now):
            self.executed.append((prime_id, acc.account_id, now))
            return "DONE"

    coord = MockPrimeCoordinator()
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            prime_store=prime_store, prime_coordinator=coord)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)

    worker.tick()
    assert len(coord.executed) == 1
    assert jobs.get("prime-job").state == JobState.DONE
    receipt = jobs.receipt("prime-job", evaluate_trigger(job, None, NOW).event_id)
    assert receipt["state"] == "DONE"
    assert "prime target window verified" in receipt["result"]
    worker.stop()


def test_worker_prime_refusal_transitions_to_failed_retryable(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_prime=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("prime-job", "prime", "project", "main_codex2",
                      account.account_id, Trigger.PRIME, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      model="model-1", enabled=True, state=JobState.ARMED)
    jobs.save(job)
    prime_store = PrimeStore(path)

    class RefusingCoordinator:
        def execute(self, prime_id, acc, now):
            return "WRONG_BUCKET"

    coord = RefusingCoordinator()
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            prime_store=prime_store, prime_coordinator=coord)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)

    worker.tick()
    assert jobs.get("prime-job").state == JobState.FAILED_RETRYABLE
    assert "prime refused: WRONG_BUCKET" in jobs.get("prime-job").waiting_reason
    worker.stop()


def test_worker_prime_unverified_transitions_to_verifying_and_reconciles_done(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_prime=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("prime-job", "prime", "project", "main_codex2",
                      account.account_id, Trigger.PRIME, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      model="model-1", enabled=True, state=JobState.ARMED)
    jobs.save(job)
    prime_store = PrimeStore(path)

    class ObservationalCoordinator:
        def __init__(self):
            self.observed = False

        def execute(self, prime_id, acc, now):
            win = LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=1, quota_bucket="codex")
            prime_store.claim_action(prime_id, win, now)
            prime_store.record_submission(prime_id, accepted=True, result="accepted")
            prime_store.record_observation(prime_id, None)
            return "UNVERIFIED"

        def observe(self, prime_id, acc):
            self.observed = True
            return "DONE"

    coord = ObservationalCoordinator()
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            prime_store=prime_store, prime_coordinator=coord)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)

    worker.tick()
    event_id = evaluate_trigger(job, None, NOW).event_id
    receipt = jobs.receipt("prime-job", event_id)
    assert receipt["state"] == "VERIFYING"
    assert "awaiting window change" in receipt["result"]

    # Now simulate lease expiration
    past = NOW - timedelta(minutes=5)
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE prepared_executions SET lease_expires_at=? WHERE execution_id=?",
                   (past.isoformat(), receipt["execution_id"]))
        db.commit()

    # Next tick runs _reconcile_prime -> calls observe -> DONE
    worker.tick()
    assert coord.observed
    assert jobs.get("prime-job").state == JobState.DONE
    assert jobs.receipt("prime-job", event_id)["state"] == "DONE"
    worker.stop()


def test_worker_prime_reconcile_interrupted_submission(tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_args, **_kwargs: [account])
    monkeypatch.setitem(PROVIDERS, "codex", replace(
        PROVIDERS["codex"], supports_prime=True,
        window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE))

    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    job = PreparedJob("prime-job", "prime", "project", "main_codex2",
                      account.account_id, Trigger.PRIME, Payload.USER_COMMAND,
                      {"text": "cc"}, {"at": NOW.isoformat()},
                      model="model-1", enabled=True, state=JobState.ARMED)
    jobs.save(job)
    prime_store = PrimeStore(path)
    target = PrimeTarget("prime-job", "ev-1", account.account_id, "model-1", "codex", "five_hour")
    prime_id = prime_store.create(target, NOW)
    window = LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=1, quota_bucket="codex")
    prime_store.claim_action(prime_id, window, NOW)
    # prime receipt is in state SUBMITTING

    past = NOW - timedelta(minutes=5)
    execution_id = jobs.claim("prime-job", "ev-1", "dead-worker", past,
                              selected_account_id=account.account_id, selected_launcher_id="main_codex2")
    assert execution_id is not None
    jobs.advance(execution_id, "dead-worker", 1, JobState.CLAIMED, JobState.LAUNCHING, now=past)
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE prepared_executions SET lease_expires_at=? WHERE execution_id=?",
                   (past.isoformat(), execution_id))
        db.commit()

    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            prime_store=prime_store, prime_coordinator=object())
    worker._probe_due = lambda _accounts: None

    worker.tick()
    assert prime_store.get(prime_id)["state"] == "RECOVERY_REQUIRED"
    assert jobs.get("prime-job").state == JobState.RECOVERY_REQUIRED
    assert jobs.receipt("prime-job", "ev-1")["state"] == "RECOVERY_REQUIRED"
    worker.stop()




class TestStopIsATruthfulOwnershipBarrier:
    """W2-001 (audit/12.md): `stop()` used to set an event, join for 5s and
    return. A delivery already running in `_pool` kept going, and the scheduler
    thread could still reach `_pool.submit` after `shutdown(wait=False)`. The
    caller (`run_bridge_server`) then went on to `server_close()` and PID removal
    while the Bridge still owned side-effectful work.
    """

    @staticmethod
    def _worker(tmp_path):
        return PreparedWorker(AppConfig(), clock=lambda: NOW,
                              prepared_store=PreparedStore(tmp_path / "r.sqlite3"))

    def test_stop_reports_quiescent_while_a_running_delivery_can_still_act(self, tmp_path):
        worker = self._worker(tmp_path)
        started = threading.Event()
        irreversible = threading.Event()

        def slow_delivery():
            started.set()
            irreversible.wait(5)
            return "launched"

        future = worker._submit(worker._pool, slow_delivery)
        started.wait(5)

        assert worker.stop(timeout=0.3) is False, (
            "stop() claimed quiescence while an already-running delivery could "
            "still perform its irreversible step"
        )
        assert not irreversible.is_set()
        irreversible.set()
        assert future.result(timeout=5) == "launched"

    def test_queued_delivery_is_cancelled_and_never_begins_after_stop(self, tmp_path):
        worker = self._worker(tmp_path)
        # A one-worker pool guarantees the second delivery is still QUEUED.
        worker._pool = ThreadPoolExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        began_queued = []

        worker._submit(worker._pool, lambda: (started.set(), release.wait(5)))
        started.wait(5)
        queued = worker._submit(worker._pool, lambda: began_queued.append("began"))

        assert worker.stop(timeout=0.3) is False
        assert queued.cancelled(), "a delivery that had not begun must be cancelled"
        release.set()
        assert began_queued == [], "cancelled work must never execute after stop()"

    def test_no_submission_reaches_an_executor_after_shutdown_starts(self, tmp_path):
        worker = self._worker(tmp_path)
        tick_in = threading.Event()
        tick_release = threading.Event()
        submissions: list[str] = []
        real_submit = ThreadPoolExecutor.submit

        def recording_submit(pool_self, fn, *args, **kwargs):
            submissions.append(fn.__name__)
            return real_submit(pool_self, fn, *args, **kwargs)

        def slow_tick():
            # The scheduler thread parks INSIDE a tick, past the stop request.
            tick_in.set()
            tick_release.wait(5)
            return NOW

        worker.tick = slow_tick
        worker.start()
        tick_in.wait(5)

        worker._pool.submit = recording_submit.__get__(worker._pool, ThreadPoolExecutor)
        worker._probe_pool.submit = recording_submit.__get__(worker._probe_pool, ThreadPoolExecutor)
        assert worker.stop(timeout=0.3) is False
        before = list(submissions)
        tick_release.set()
        worker.stop(timeout=5)
        assert submissions == before, (
            f"executor submission after shutdown started: {submissions[len(before):]}"
        )

    def test_stop_refuses_new_submissions_before_quiescence(self, tmp_path):
        worker = self._worker(tmp_path)
        assert worker.stop() is True
        assert worker._submit(worker._pool, lambda: "late") is None
        assert worker.lifecycle == "CLOSED"

    def test_the_thread_handle_survives_a_stop_that_did_not_finish(self, tmp_path):
        worker = self._worker(tmp_path)
        release = threading.Event()
        worker.tick = lambda: (release.wait(5), NOW)[1]
        worker.start()
        assert worker.stop(timeout=0.2) is False
        assert worker._thread is not None and worker._thread.is_alive()
        release.set()
        assert worker.stop(timeout=5) is True
        assert worker._thread is None, "a dead thread handle must not be retained"


class TestTerminalExecutionsCompactToIdentityTombstones:
    """W2-003 (audit/12.md): a recurring EVERY_RESET job grew one terminal
    `prepared_executions` row per completed event forever -- 40 rows for 40
    events, with the job back at ARMED and nothing active or recoverable. The
    diagnostic payload is what grows; the identity is what must survive.
    """

    @staticmethod
    def _job(tmp_path, recurrence="EVERY_RESET"):
        jobs = PreparedStore(tmp_path / "r.sqlite3")
        jobs.save(PreparedJob(
            "recurring", "Recurring", "project", "acc", "acc:1",
            Trigger.ON_RESET, Payload.USER_COMMAND, {},
            {"armed_reset_at": "r0", "armed_window_id": "w0"},
            enabled=True, state=JobState.ARMED, recurrence=recurrence))
        return jobs

    def _run_event(self, jobs, index):
        execution = jobs.claim("recurring", f"ev-{index}", "owner",
                               NOW + timedelta(seconds=index),
                               selected_account_id="acc:1", selected_launcher_id="acc")
        assert execution, "the event was never claimed"
        jobs.advance(execution, "owner", 1, JobState.CLAIMED, JobState.DONE,
                     now=NOW + timedelta(seconds=index),
                     result=f"diagnostic payload number {index} " * 4)

    def _rows(self, jobs):
        with closing(sqlite3.connect(jobs.path)) as db:
            return db.execute(
                "SELECT trigger_event_id,state,result,delivery_hash,lease_expires_at "
                "FROM prepared_executions ORDER BY trigger_event_id").fetchall()

    def test_terminal_history_stops_growing_without_losing_a_replay_barrier(self, tmp_path):
        jobs = self._job(tmp_path)
        for index in range(40):
            self._run_event(jobs, index)

        assert jobs.compact_executions(keep=6) > 0, "nothing was compacted"
        rows = self._rows(jobs)
        assert len(rows) == 40, "compaction dropped identity a replay still needs"
        full = [row for row in rows if row[2]]
        assert len(full) == 6, f"{len(full)} rows kept their diagnostic payload"
        assert all(row[1] == "DONE" for row in rows), "a state was rewritten"
        # A replay of the oldest event is still refused, because the tombstone
        # keeps its (prepared_id, trigger_event_id) identity.
        assert jobs.claim("recurring", "ev-0", "owner", NOW) is None

    def test_an_active_or_ambiguous_execution_is_never_compacted(self, tmp_path):
        jobs = self._job(tmp_path)
        for index in range(10):
            self._run_event(jobs, index)
        live = jobs.claim("recurring", "ev-live", "owner", NOW,
                          selected_account_id="acc:1", selected_launcher_id="acc")
        jobs.advance(live, "owner", 1, JobState.CLAIMED, JobState.RECOVERY_REQUIRED,
                     now=NOW, result="ambiguous, needs a human")
        assert live

        jobs.compact_executions(keep=2)
        ambiguous = jobs.receipt("recurring", "ev-live")
        assert ambiguous["result"] == "ambiguous, needs a human"
        assert jobs.recover_expired is not None

    def test_compaction_is_a_no_op_when_there_is_nothing_to_do(self, tmp_path):
        jobs = self._job(tmp_path)
        for index in range(3):
            self._run_event(jobs, index)
        assert jobs.compact_executions(keep=6) == 0
        assert all(row[2] for row in self._rows(jobs))
