from datetime import datetime, timedelta, timezone

from audapack.account_registry import AccountIdentity
from audapack.auto_account import AutoCandidate, rank_auto_accounts
from audapack.limits import LimitSnapshot, LimitWindow
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _candidate(acc_id, provider="codex", remaining=0.8, workload=0, reset_in_hours=5, availability=None):
    acc = AccountIdentity(acc_id, provider, f"Account {acc_id}", "/path", (f"launch_{acc_id}",), "test", NOW.isoformat())
    reset_at = (NOW + timedelta(hours=reset_in_hours)).isoformat() if reset_in_hours else None
    windows = [LimitWindow("five_hour", "five_hour", "5h", remaining_ratio=remaining, reset_at=reset_at)]
    snapshot = LimitSnapshot(acc_id, windows, NOW.isoformat(), "test")
    return AutoCandidate(acc, f"launch_{acc_id}", snapshot, None, workload)


def test_auto_ranking_respects_operator_priority():
    job = PreparedJob("job", "test", "proj", "AUTO", "AUTO", Trigger.ON_TIME, Payload.USER_COMMAND,
                      {}, {"auto_priority": ["acc-2", "acc-1"]})
    c1 = _candidate("acc-1", remaining=0.9, workload=0)
    c2 = _candidate("acc-2", remaining=0.3, workload=2)
    ranked = rank_auto_accounts(job, [c1, c2], NOW)
    # acc-2 is first because it's listed first in auto_priority despite lower remaining quota
    assert [s.candidate.account.account_id for s in ranked] == ["acc-2", "acc-1"]


def test_auto_ranking_tie_breaks_on_remaining_quota_then_workload():
    job = PreparedJob("job", "test", "proj", "AUTO", "AUTO", Trigger.ON_TIME, Payload.USER_COMMAND, {}, {})
    # Same priority (both unlisted): c1 has remaining 0.5, c2 has 0.8
    c1 = _candidate("acc-1", remaining=0.5, workload=0)
    c2 = _candidate("acc-2", remaining=0.8, workload=0)
    ranked = rank_auto_accounts(job, [c1, c2], NOW)
    assert ranked[0].candidate.account.account_id == "acc-2"

    # Same remaining quota: c3 has workload 1, c4 has workload 0
    c3 = _candidate("acc-3", remaining=0.8, workload=1)
    c4 = _candidate("acc-4", remaining=0.8, workload=0)
    ranked_workload = rank_auto_accounts(job, [c3, c4], NOW)
    assert ranked_workload[0].candidate.account.account_id == "acc-4"


def test_auto_ranking_excludes_exhausted_accounts():
    job = PreparedJob("job", "test", "proj", "AUTO", "AUTO", Trigger.ON_TIME, Payload.USER_COMMAND, {}, {})
    c_avail = _candidate("acc-1", remaining=0.5)
    c_exhausted = _candidate("acc-2", remaining=0.0)
    ranked = rank_auto_accounts(job, [c_avail, c_exhausted], NOW)
    assert len(ranked) == 1
    assert ranked[0].candidate.account.account_id == "acc-1"


def test_auto_reservation_lifecycle_in_store(tmp_path):
    store = PreparedStore(tmp_path / "resources.sqlite3")
    job = PreparedJob("auto-job", "test", "proj", "AUTO", "AUTO", Trigger.ON_TIME,
                      Payload.USER_COMMAND, {"text": "hi"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    store.save(job)

    # Claim concrete account for AUTO job
    exec_id = store.claim("auto-job", "ev-1", "worker-1", NOW,
                          selected_account_id="acc-1", selected_launcher_id="launch-1",
                          selection_reason="AUTO rank 1")
    assert exec_id is not None
    assert store.get("auto-job").account_id == "AUTO"
    assert "acc-1" in store.reservations(NOW)
    assert store.reservations(NOW)["acc-1"]["owner_execution"] == exec_id

    # Another claim cannot take acc-1 while reserved
    job2 = PreparedJob("job-2", "test", "proj", "launch-1", "acc-1", Trigger.ON_TIME,
                       Payload.USER_COMMAND, {"text": "hi"}, {"at": NOW.isoformat()},
                       enabled=True, state=JobState.ARMED)
    store.save(job2)
    assert store.claim("job-2", "ev-2", "worker-2", NOW, selected_account_id="acc-1") is None

    # Advance execution to terminal releases reservation
    assert store.advance(exec_id, "worker-1", 1, JobState.CLAIMED, JobState.DONE, now=NOW)
    assert "acc-1" not in store.reservations(NOW)


def test_auto_reservation_reclaims_on_recovery(tmp_path):
    store = PreparedStore(tmp_path / "resources.sqlite3")
    job = PreparedJob("auto-job", "test", "proj", "AUTO", "AUTO", Trigger.ON_TIME,
                      Payload.USER_COMMAND, {"text": "hi"}, {"at": NOW.isoformat()},
                      enabled=True, state=JobState.ARMED)
    store.save(job)

    past = NOW - timedelta(minutes=5)
    exec_id = store.claim("auto-job", "ev-1", "dead-worker", past,
                          selected_account_id="acc-1", selected_launcher_id="launch-1")
    assert exec_id is not None

    # Expire reservation and lease
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE prepared_executions SET lease_expires_at=? WHERE execution_id=?",
                   (past.isoformat(), exec_id))
        db.execute("DELETE FROM prepared_account_reservations")
        db.commit()

    assert not store.reservations(NOW)

    # Recover expired execution before side effect (resume)
    action, gen = store.recover_expired(exec_id, "new-worker", NOW)
    assert action == "resume"
    assert gen == 2

    # Reservation was restored for acc-1 under exec_id
    assert "acc-1" in store.reservations(NOW)
    assert store.reservations(NOW)["acc-1"]["owner_execution"] == exec_id
