"""PRIME tests use fake capabilities and never consume real provider quota."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from audapack.account_registry import AccountIdentity
from audapack.limits import LimitSnapshot, LimitWindow
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_prime import PrimeCoordinator, PrimeStore, PrimeTarget
from audapack.provider_capabilities import (
    PROVIDERS,
    CapabilityEvidence,
    WindowStartSemantics,
)

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
TARGET = PrimeTarget("prepared", "event", "codex:2", "synthetic-cheap-model",
                     "codex", "five_hour@codex")


def account(tmp_path):
    return AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                           ("main_codex2",), "test", NOW.isoformat())


def capability(_account):
    return replace(PROVIDERS["codex"], supports_prime=True,
                   window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE,
                   quota_bucket_mapping={TARGET.model: TARGET.quota_bucket},
                   evidence=CapabilityEvidence("controlled fake", "OPERATOR_VERIFIED"))


def snapshot(*, started="", reset="", bucket="codex"):
    return LimitSnapshot(TARGET.account_id, (
        LimitWindow(f"five_hour@{bucket}", "five_hour", "5h", remaining_ratio=1,
                    quota_bucket=bucket, window_started_at=started or None,
                    reset_at=reset or None),
    ), NOW.isoformat(), "fake")


def test_unsupported_or_unknown_semantics_never_call_consuming_adapter(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    used = []
    coordinator = PrimeCoordinator(store, lambda _account: PROVIDERS["codex"],
                                   lambda _account: snapshot(),
                                   lambda *args: used.append(args))
    assert coordinator.execute(prime_id, account(tmp_path), NOW) == "UNSUPPORTED"
    assert store.get(prime_id)["state"] == "PENDING"
    assert not used


def test_wrong_bucket_or_already_started_window_refuses_prime(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    used = []
    def consume(*args):
        used.append(args)
    wrong = PrimeCoordinator(store, capability, lambda _account: snapshot(bucket="reserve"), consume)
    assert wrong.execute(prime_id, account(tmp_path), NOW) == "WRONG_BUCKET"
    active = PrimeCoordinator(store, capability,
                              lambda _account: snapshot(started=NOW.isoformat()), consume)
    assert active.execute(prime_id, account(tmp_path), NOW) == "ALREADY_STARTED"
    assert not used and store.get(prime_id)["state"] == "PENDING"


def test_minimal_prime_records_selected_window_and_neutral_directory(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    observed = [snapshot(), snapshot(started=NOW.isoformat(),
                                     reset=(NOW + timedelta(hours=5)).isoformat())]
    calls = []

    def probe(_account):
        calls.append("non-consuming probe")
        return observed.pop(0)

    def consume(_account, model, cwd, prompt):
        calls.append((model, cwd, prompt))
        assert cwd != tmp_path
        assert "Do not read or write files" in prompt
        return True

    coordinator = PrimeCoordinator(store, capability, probe, consume)
    assert coordinator.execute(prime_id, account(tmp_path), NOW) == "DONE"
    receipt = store.get(prime_id)
    assert receipt["quota_bucket"] == "codex"
    assert receipt["observed_window_start"] == NOW.isoformat()
    assert receipt["observed_reset"] == (NOW + timedelta(hours=5)).isoformat()
    assert calls[0] == calls[2] == "non-consuming probe"
    assert len([call for call in calls if isinstance(call, tuple)]) == 1
    assert coordinator.execute(prime_id, account(tmp_path), NOW) == "ALREADY_CLAIMED"


def test_restart_after_action_claim_never_consumes_again(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    assert store.claim_action(prime_id, snapshot().windows[0], NOW)
    restarted = PrimeStore(store.path)
    assert restarted.recover_interrupted(prime_id)
    assert not store.record_submission(prime_id, accepted=True, result="late stale worker")
    used = []
    coordinator = PrimeCoordinator(restarted, capability, lambda _account: snapshot(),
                                   lambda *args: used.append(args))
    assert coordinator.execute(prime_id, account(tmp_path), NOW) == "ALREADY_CLAIMED"
    assert restarted.get(prime_id)["state"] == "RECOVERY_REQUIRED"
    assert not used


def test_restart_after_accepted_action_can_observe_without_consuming_again(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    assert store.claim_action(prime_id, snapshot().windows[0], NOW)
    assert store.record_submission(prime_id, accepted=True, result="accepted")
    restarted = PrimeStore(store.path)
    assert not restarted.recover_interrupted(prime_id)
    used = []
    coordinator = PrimeCoordinator(
        restarted, capability,
        lambda _account: snapshot(reset=(NOW + timedelta(hours=5)).isoformat()),
        lambda *args: used.append(args),
    )
    assert coordinator.observe(prime_id, account(tmp_path)) == "DONE"
    assert not used
    assert coordinator.execute(prime_id, account(tmp_path), NOW) == "ALREADY_CLAIMED"


def test_unchanged_observation_does_not_claim_prime_success(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    coordinator = PrimeCoordinator(store, capability, lambda _account: snapshot(),
                                   lambda *_args: True)
    assert coordinator.execute(prime_id, account(tmp_path), NOW) == "UNVERIFIED"
    assert store.get(prime_id)["state"] == "UNVERIFIED"


def test_prime_store_get_by_event(tmp_path):
    store = PrimeStore(tmp_path / "prime.db")
    prime_id = store.create(TARGET, NOW)
    record = store.get_by_event(TARGET.prepared_id, TARGET.trigger_event_id)
    assert record is not None
    assert record["prime_id"] == prime_id
    assert record["account_id"] == TARGET.account_id
    assert record["model"] == TARGET.model
    assert store.get_by_event("nonexistent", "event") is None


def test_prepared_store_reown_prime(tmp_path):
    store = PreparedStore(tmp_path / "resources.sqlite3")
    job = PreparedJob("prime-job", "prime", "project", "launcher-0", "account-0",
                      Trigger.PRIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": NOW.isoformat()}, enabled=True, state=JobState.ARMED)
    store.save(job)
    execution_id = store.claim("prime-job", "ev-1", "owner-1", NOW, selected_account_id="account-0")
    assert execution_id is not None

    res = store.reown_prime(execution_id, "owner-2", NOW)
    assert res == ("CLAIMED", 2)

    res_same = store.reown_prime(execution_id, "owner-2", NOW + timedelta(seconds=10))
    assert res_same == ("CLAIMED", 2)

    store.advance(execution_id, "owner-2", 2, JobState.CLAIMED, JobState.DONE, now=NOW)
    assert store.reown_prime(execution_id, "owner-3", NOW) is None
    assert store.reown_prime("nonexistent", "owner-3", NOW) is None

