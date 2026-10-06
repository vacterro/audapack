"""Crash and takeover guarantees for durable prepared executions."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from audapack.account_registry import AccountIdentity, AccountRegistry
from audapack.config import AppConfig, LauncherConfig
from audapack.limits import LimitStore
from audapack.models import Project
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_delivery import PreflightError
from audapack.prepared_worker import PreparedWorker

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
EXPIRED = NOW + timedelta(seconds=121)


@pytest.fixture
def store(tmp_path):
    result = PreparedStore(tmp_path / "prepared.sqlite3")
    result.save(PreparedJob(
        "job", "continue", "project", "codex2", "codex:2", Trigger.ON_TIME,
        Payload.USER_COMMAND, {"text": "cc"}, {"at": NOW.isoformat()},
        enabled=True, state=JobState.ARMED,
    ))
    return result


@pytest.mark.parametrize("state", [JobState.CLAIMED, JobState.PREPARING])
def test_expired_pre_side_effect_lease_resumes_same_execution_and_fences_old_owner(store, state):
    execution = store.claim("job", "event", "old", NOW)
    if state == JobState.PREPARING:
        assert store.advance(execution, "old", 1, JobState.CLAIMED,
                             JobState.PREPARING, now=NOW)
    restarted = PreparedStore(store.path)
    assert restarted.recover_expired(execution, "new", EXPIRED) == ("resume", 2)
    receipt = restarted.receipt("job", "event")
    assert receipt["execution_id"] == execution
    assert receipt["state"] == "CLAIMED"
    assert not store.advance(execution, "old", 1, state, JobState.DONE, now=EXPIRED)
    assert restarted.advance(execution, "new", 2, JobState.CLAIMED,
                             JobState.PREPARING, now=EXPIRED)
    assert restarted.claim("job", "event", "third", EXPIRED) is None


def test_heartbeat_prevents_takeover_until_renewed_lease_expires(store):
    execution = store.claim("job", "event", "old", NOW)
    renewed = NOW + timedelta(seconds=100)
    assert store.heartbeat(execution, "old", 1, renewed)
    assert store.recover_expired(execution, "new", EXPIRED) is None
    assert store.recover_expired(execution, "new", renewed + timedelta(seconds=121)) == ("resume", 2)
    assert not store.heartbeat(execution, "old", 1, renewed + timedelta(seconds=122))


def test_generation_rejects_stale_worker_even_if_owner_text_is_reused(store):
    execution = store.claim("job", "event", "bridge", NOW)
    assert store.recover_expired(execution, "bridge", EXPIRED) == ("resume", 2)
    assert not store.advance(execution, "bridge", 1, JobState.CLAIMED,
                             JobState.PREPARING, now=EXPIRED)
    assert store.advance(execution, "bridge", 2, JobState.CLAIMED,
                         JobState.PREPARING, now=EXPIRED)


def test_launching_with_process_never_relaunches_and_live_process_is_observed(store):
    execution = store.claim("job", "event", "old", NOW)
    assert store.advance(execution, "old", 1, JobState.CLAIMED, JobState.PREPARING, now=NOW)
    assert store.advance(execution, "old", 1, JobState.PREPARING, JobState.LAUNCHING, now=NOW)
    assert store.advance(execution, "old", 1, JobState.LAUNCHING, JobState.DELIVERING,
                         process_id=1234, process_token=5678, now=NOW)
    assert store.recover_expired(execution, "new", EXPIRED, process_alive=True,
                                 process_attributable=True) == ("observe", 2)
    assert store.receipt("job", "event")["state"] == "RUNNING"
    assert store.claim("job", "event", "third", EXPIRED) is None
    assert store.recover_expired(execution, "third", EXPIRED + timedelta(seconds=121),
                                 process_alive=False) == ("ambiguous", 3)
    assert store.receipt("job", "event")["state"] == "RECOVERY_REQUIRED"
    assert store.claim("job", "event", "fourth", EXPIRED + timedelta(seconds=242)) is None


@pytest.mark.parametrize("state", [JobState.LAUNCHING, JobState.DELIVERING])
def test_ambiguous_spawn_or_delivery_never_automatically_retries(store, state):
    execution = store.claim("job", "event", "old", NOW)
    assert store.advance(execution, "old", 1, JobState.CLAIMED, JobState.PREPARING, now=NOW)
    assert store.advance(execution, "old", 1, JobState.PREPARING, JobState.LAUNCHING, now=NOW)
    if state == JobState.DELIVERING:
        assert store.advance(execution, "old", 1, JobState.LAUNCHING, state,
                             process_id=1234, delivery_hash="abc", now=NOW)
    assert store.recover_expired(execution, "new", EXPIRED) == ("ambiguous", 2)
    assert store.claim("job", "event", "another", EXPIRED) is None


def test_retryable_preflight_failure_reuses_event_and_increments_attempt(store):
    execution = store.claim("job", "event", "old", NOW)
    assert store.advance(execution, "old", 1, JobState.CLAIMED,
                         JobState.FAILED_RETRYABLE, result="CLI temporarily unavailable", now=NOW)
    assert store.claim("job", "event", "new", NOW + timedelta(seconds=119)) is None
    assert store.claim("job", "event", "new", EXPIRED) == execution
    receipt = store.receipt("job", "event")
    assert receipt["attempt_number"] == 2
    assert receipt["claim_generation"] == 2
    assert not store.advance(execution, "old", 1, JobState.CLAIMED, JobState.DONE, now=EXPIRED)
    assert store.advance(execution, "new", 2, JobState.CLAIMED, JobState.DONE, now=EXPIRED)
    assert store.claim("job", "event", "third", EXPIRED) is None
    assert store.claim("job", "different-event", "third", EXPIRED) is None


def test_every_reset_uses_new_event_after_success(tmp_path):
    store = PreparedStore(tmp_path / "reset.sqlite3")
    store.save(PreparedJob(
        "reset", "continue", "project", "codex2", "codex:2", Trigger.ON_RESET,
        Payload.USER_COMMAND, {"text": "cc"}, {"window_id": "five_hour"},
        enabled=True, state=JobState.ARMED, recurrence="EVERY_RESET",
    ))
    first = store.claim("reset", "reset-1", "old", NOW)
    assert store.advance(first, "old", 1, JobState.CLAIMED, JobState.DONE, now=NOW)
    assert store.claim("reset", "reset-1", "new", EXPIRED) is None
    second = store.claim("reset", "reset-2", "new", EXPIRED)
    assert second and second != first


def test_legacy_active_receipt_migrates_to_expired_lease(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("""CREATE TABLE prepared_executions (
            execution_id TEXT PRIMARY KEY, prepared_id TEXT NOT NULL,
            trigger_event_id TEXT NOT NULL, state TEXT NOT NULL,
            claimed_at TEXT NOT NULL, owner_id TEXT NOT NULL,
            process_id INTEGER, delivery_hash TEXT, result TEXT NOT NULL DEFAULT '',
            UNIQUE(prepared_id, trigger_event_id))""")
        db.execute("""INSERT INTO prepared_executions
            (execution_id,prepared_id,trigger_event_id,state,claimed_at,owner_id)
            VALUES ('old','job','event','LAUNCHING',?,'old-owner')""", (NOW.isoformat(),))
        db.commit()
    store = PreparedStore(path)
    receipt = store.receipt("job", "event")
    assert receipt["claim_generation"] == 1
    assert receipt["lease_expires_at"] == NOW.isoformat()
    assert store.recover_expired("old", "new", EXPIRED) == ("ambiguous", 2)


def test_retryable_preflight_error_is_explicit():
    assert PreflightError("CLI unavailable", retryable=True).retryable
    assert not PreflightError("invalid profile").retryable


def test_repeated_bridge_restarts_never_submit_second_launch_after_spawn(store, tmp_path):
    execution = store.claim("job", "event", "dead-worker", NOW)
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW.isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("codex2", "Codex 2", "C2")]
    launches = []

    def make_worker(at):
        worker = PreparedWorker(
            config, clock=lambda: at, account_registry=AccountRegistry(store.path),
            limit_store=LimitStore(store.path), prepared_store=PreparedStore(store.path),
        )
        worker._known_accounts = lambda: {account.account_id: account}
        worker._probe_due = lambda _accounts: None
        worker._pool.submit = lambda fn, *args: fn(*args)
        return worker

    first_restart = make_worker(EXPIRED)
    def fake_delivery(_job, _account, same_execution, generation):
        launches.append(same_execution)
        assert first_restart.jobs.advance(same_execution, first_restart.scheduler.owner_id,
                                          generation, JobState.CLAIMED, JobState.PREPARING,
                                          now=EXPIRED)
        assert first_restart.jobs.advance(same_execution, first_restart.scheduler.owner_id,
                                          generation, JobState.PREPARING, JobState.LAUNCHING,
                                          now=EXPIRED)
        assert first_restart.jobs.advance(same_execution, first_restart.scheduler.owner_id,
                                          generation, JobState.LAUNCHING, JobState.DELIVERING,
                                          process_id=1234, delivery_hash="payload", now=EXPIRED)
    first_restart._deliver = fake_delivery
    first_restart.tick()
    first_restart.stop()

    second_restart = make_worker(EXPIRED + timedelta(seconds=121))
    second_restart._deliver = lambda *_args: launches.append("duplicate")
    second_restart.tick()
    second_restart.stop()
    third_restart = make_worker(EXPIRED + timedelta(seconds=242))
    third_restart._deliver = lambda *_args: launches.append("duplicate")
    third_restart.tick()
    third_restart.stop()
    assert launches == [execution]
    assert store.receipt("job", "event")["state"] == "RECOVERY_REQUIRED"


def test_worker_classifies_safe_cli_resolution_failure_as_retryable(store, tmp_path, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("codex2",), "test", NOW.isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("codex2", "Codex 2", "C2")]
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(store.path),
                            limit_store=LimitStore(store.path), prepared_store=store)
    monkeypatch.setattr("audapack.prepared_worker.build_launch_plan",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            PreflightError("Codex CLI unavailable", retryable=True)))
    execution = store.claim("job", "event", worker.scheduler.owner_id, NOW)
    worker._deliver(store.get("job"), account, execution)
    assert store.receipt("job", "event")["state"] == "FAILED_RETRYABLE"
    assert store.get("job").enabled
    assert store.claim("job", "event", "replacement", EXPIRED) == execution
    worker.stop()


def test_test_now_receipt_does_not_consume_future_one_shot(store):
    claimed = store.claim_test("job", "tester", NOW)
    assert claimed is not None
    execution, test_event = claimed
    assert store.receipt("job", test_event)["is_test"] == 1
    assert store.claim("job", "scheduled", "scheduler", NOW) is None
    assert store.advance(execution, "tester", 1, JobState.CLAIMED,
                         JobState.DONE, now=NOW)
    assert store.get("job").enabled
    assert store.get("job").state == JobState.ARMED
    scheduled = store.claim("job", "scheduled", "scheduler", EXPIRED)
    assert scheduled and scheduled != execution


def test_active_test_cannot_settle_scheduled_event_as_missed(store):
    execution, _ = store.claim_test("job", "tester", NOW)
    assert not store.settle_without_launch("job", "scheduled", "scheduler",
                                           JobState.MISSED, "strict deadline passed", NOW)
    assert store.get("job").enabled
    assert store.advance(execution, "tester", 1, JobState.CLAIMED,
                         JobState.DONE, now=NOW)
    assert store.claim("job", "scheduled", "scheduler", EXPIRED)


def test_interrupted_test_now_blocks_duplicate_until_operator_recovery(store):
    execution, test_event = store.claim_test("job", "tester", NOW)
    assert store.advance(execution, "tester", 1, JobState.CLAIMED,
                         JobState.PREPARING, now=NOW)
    assert store.advance(execution, "tester", 1, JobState.PREPARING,
                         JobState.LAUNCHING, now=NOW)
    assert store.recover_expired(execution, "bridge", EXPIRED) == ("ambiguous", 2)
    assert store.receipt("job", test_event)["state"] == "RECOVERY_REQUIRED"
    assert store.claim("job", "scheduled", "scheduler", EXPIRED) is None
    assert store.get("job").enabled
    assert store.test_recovery("job")["execution_id"] == execution
    assert store.resolve_test_recovery(execution, "operator checked process")
    assert store.claim("job", "scheduled", "scheduler", EXPIRED)


def test_interrupted_pre_spawn_test_cancels_without_blocking_schedule(store):
    execution, test_event = store.claim_test("job", "tester", NOW)
    assert store.recover_expired(execution, "bridge", EXPIRED) == ("test-cancelled", 2)
    assert store.receipt("job", test_event)["state"] == "CANCELLED"
    assert store.claim("job", "scheduled", "scheduler", EXPIRED)
