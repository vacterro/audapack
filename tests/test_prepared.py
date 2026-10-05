from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from audapack.limits import LimitSnapshot, LimitWindow
from audapack.prepared import (
    JobState,
    Payload,
    PreparedJob,
    PreparedScheduler,
    PreparedStore,
    Trigger,
)

NOW = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)


def _job(trigger=Trigger.ON_TIME, at=None):
    return PreparedJob(
        prepared_id="P-1", name="continue", project_id="project", launcher_id="main_codex2",
        account_id="codex:2", trigger=trigger, payload=Payload.USER_COMMAND,
        payload_config={"text": "cc"}, trigger_config={"at": (at or NOW).isoformat()},
        enabled=True, state=JobState.ARMED,
    )


def _available(observed=NOW):
    return LimitSnapshot("codex:2", (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.5),
        LimitWindow("weekly", "weekly", "Weekly", remaining_ratio=0.8),
    ), observed.isoformat(), "test", stale_after_seconds=7200)


def test_due_time_claim_survives_restart_and_two_workers(tmp_path):
    path = tmp_path / "scheduler.db"
    store = PreparedStore(path)
    job = _job(at=NOW + timedelta(minutes=2))
    store.save(job)
    now = [NOW]
    first = PreparedScheduler(PreparedStore(path), lambda: now[0], "gui")
    assert first.due(job, _available())[0].state == JobState.WAITING_TRIGGER
    now[0] += timedelta(minutes=2)
    def compete(owner):
        return PreparedScheduler(PreparedStore(path), lambda: now[0], owner).due(job, _available())[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(compete, ("bridge", "gui")))
    assert sum(bool(value) for value in ids) == 1
    assert PreparedScheduler(PreparedStore(path), lambda: now[0], "restart").due(job, _available())[1] is None
    owner = "bridge" if ids[0] else "gui"
    execution = next(value for value in ids if value)
    assert store.advance(execution, owner, 1, JobState.CLAIMED, JobState.PREPARING, now=now[0])
    assert not store.advance(execution, "stale", 1, JobState.PREPARING, JobState.DONE, now=now[0])
    assert store.advance(execution, owner, 1, JobState.PREPARING, JobState.DONE, now=now[0])
    assert store.get(job.prepared_id).enabled is False


def test_reset_waits_full_safety_delay_then_requires_fresh_verification(tmp_path):
    store = PreparedStore(tmp_path / "scheduler.db")
    job = _job(Trigger.ON_RESET)
    job = PreparedJob(**{**job.__dict__, "trigger_config": {"window_id": "five_hour"}})
    store.save(job)
    reset = NOW + timedelta(minutes=30)
    before = LimitSnapshot("codex:2", (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0,
                    reset_at=reset.isoformat()),
    ), NOW.isoformat(), "test", stale_after_seconds=7200)
    current = [NOW]
    scheduler = PreparedScheduler(store, lambda: current[0], "bridge")
    assert scheduler.due(job, before)[0].state == JobState.WAITING_TRIGGER
    armed = store.get(job.prepared_id)
    current[0] = reset + timedelta(seconds=59)
    assert scheduler.due(armed, before)[1] is None
    current[0] += timedelta(seconds=1)
    assert scheduler.due(armed, before)[0].state == JobState.WAITING_LIMIT
    verified = LimitSnapshot("codex:2", (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=1,
                    reset_at=(reset + timedelta(hours=5)).isoformat()),
    ), current[0].isoformat(), "test")
    decision, execution = scheduler.due(armed, verified)
    assert decision.state == JobState.CLAIMED
    assert execution
    assert scheduler.due(armed, verified)[1] is None


def test_strict_time_misses_instead_of_late_launch(tmp_path):
    store = PreparedStore(tmp_path / "scheduler.db")
    original = _job()
    job = PreparedJob(**{**original.__dict__, "trigger_config": {
        "at": NOW.isoformat(), "availability_policy": "STRICT_TIME"}})
    store.save(job)
    empty = LimitSnapshot("codex:2", (), NOW.isoformat(), "test")
    decision, execution = PreparedScheduler(store, lambda: NOW).due(job, empty)
    assert decision.state == JobState.MISSED
    assert execution is None
    assert store.settle_without_launch(job.prepared_id, decision.event_id, "bridge",
                                       JobState.MISSED, decision.reason, NOW)
    assert store.get(job.prepared_id).state == JobState.MISSED
    assert store.get(job.prepared_id).enabled is False
    assert not store.settle_without_launch(job.prepared_id, decision.event_id, "restart",
                                           JobState.MISSED, decision.reason, NOW)
    assert store.receipt(job.prepared_id, decision.event_id)["state"] == "MISSED"


def test_timezone_offset_sleep_and_backward_clock_keep_one_event(tmp_path):
    store = PreparedStore(tmp_path / "scheduler.db")
    local_due = (NOW + timedelta(minutes=2)).astimezone(timezone(timedelta(hours=3)))
    job = _job(at=local_due)
    store.save(job)
    current = [NOW]
    first = PreparedScheduler(store, lambda: current[0], "first")
    assert first.due(job, _available())[0].state == JobState.WAITING_TRIGGER
    current[0] = NOW + timedelta(minutes=3)
    restarted = PreparedScheduler(PreparedStore(store.path), lambda: current[0], "restart")
    decision, execution = restarted.due(store.get(job.prepared_id), _available(current[0]))
    assert decision.state == JobState.CLAIMED and execution
    assert store.advance(execution, "restart", 1, JobState.CLAIMED, JobState.DONE,
                         now=current[0])
    current[0] = NOW - timedelta(hours=1)
    assert first.due(store.get(job.prepared_id), _available())[1] is None
    current[0] = NOW + timedelta(minutes=4)
    assert first.due(store.get(job.prepared_id), _available(current[0]))[1] is None
    assert store.receipt(job.prepared_id, decision.event_id)["state"] == "DONE"


def test_next_usable_reset_waits_for_weekly_hard_bottleneck(tmp_path):
    store = PreparedStore(tmp_path / "scheduler.db")
    job = _job(Trigger.ON_RESET)
    job = PreparedJob(**{**job.__dict__, "trigger_config": {"window_id": "NEXT_USABLE"}})
    store.save(job)
    five_hour = NOW + timedelta(hours=5)
    weekly = NOW + timedelta(days=2)
    before = LimitSnapshot("codex:2", (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.7,
                    reset_at=five_hour.isoformat()),
        LimitWindow("weekly", "weekly", "Weekly", remaining_ratio=0,
                    reset_at=weekly.isoformat()),
    ), NOW.isoformat(), "test", stale_after_seconds=7 * 86400)
    current = [NOW]
    scheduler = PreparedScheduler(store, lambda: current[0], "bridge")
    assert scheduler.due(job, before)[0].state == JobState.WAITING_TRIGGER
    armed = store.get(job.prepared_id)
    assert armed.next_due_at == (weekly + timedelta(seconds=60)).isoformat()
    current[0] = weekly + timedelta(seconds=60)
    still_exhausted = LimitSnapshot("codex:2", before.windows, current[0].isoformat(),
                                    "test", stale_after_seconds=7200)
    assert scheduler.due(armed, still_exhausted)[0].state == JobState.WAITING_LIMIT
    available = LimitSnapshot("codex:2", (
        LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=0.7,
                    reset_at=(five_hour + timedelta(hours=5)).isoformat()),
        LimitWindow("weekly", "weekly", "Weekly", remaining_ratio=1,
                    reset_at=(weekly + timedelta(days=7)).isoformat()),
    ), current[0].isoformat(), "test")
    decision, execution = scheduler.due(armed, available)
    assert decision.state == JobState.CLAIMED and execution
