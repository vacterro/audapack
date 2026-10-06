"""Persisted Prepared/Scheduler bindings across the federation cut-over.

The defect class: a job saved before the account registry became the single
authority still names a retired static launcher id (``claude1``,
``main_codex2``, ...). Two wrong repairs exist and both are refused here --
silently running the row as-is (it now fails in a generic terminal state with no
explanation) and silently re-pointing it at "some other account of the same
provider". Account correctness outranks automatic continuation: the row either
re-points onto the ONE canonical account that provably shares its identity, or
it is BLOCKED with ``ACCOUNT_MAPPING_REQUIRED`` and keeps its history intact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from audapack.config import AppConfig, LauncherConfig
from audapack.launch_federation import (
    ACCOUNT_MAPPING_REQUIRED,
    MIGRATED,
    OK,
    migrate_prepared_bindings,
    resolve_prepared_binding,
    target_id,
)
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
CANON = "claude:profile-dir:aaaa"
DONORS = {"claude1": CANON, "claude2": "claude:profile-dir:bbbb"}

# ── the pure resolver ───────────────────────────────────────────────────────

def test_a_legacy_binding_migrates_onto_its_own_canonical_account():
    binding = resolve_prepared_binding("claude1", "claude1", donors=DONORS,
                                       proven={CANON})
    assert binding.state == MIGRATED
    assert binding.account_id == CANON
    assert binding.launcher_id == target_id(CANON)
    assert binding.runnable is True


def test_a_job_whose_account_is_already_canonical_only_moves_its_launcher():
    binding = resolve_prepared_binding(CANON, "claude1", donors=DONORS,
                                       proven={CANON})
    assert binding.state == MIGRATED
    assert binding.launcher_id == target_id(CANON)


def test_an_unprovable_identity_is_required_never_guessed():
    """A provider match is not an identity. With no proven account, nothing maps."""
    binding = resolve_prepared_binding("claude1", "claude1", donors={},
                                       proven={CANON})
    assert binding.state == ACCOUNT_MAPPING_REQUIRED
    assert binding.runnable is False
    assert binding.account_id == "" and binding.launcher_id == ""


def test_a_withdrawn_account_does_not_get_a_stranger():
    """The donor still knows the id, but the plane no longer proves the account."""
    binding = resolve_prepared_binding("claude1", "claude1", donors=DONORS,
                                       proven=set())
    assert binding.state == ACCOUNT_MAPPING_REQUIRED
    assert binding.runnable is False


def test_two_different_identities_in_one_row_are_never_merged():
    binding = resolve_prepared_binding("claude1", "claude2", donors=DONORS,
                                       proven={CANON, "claude:profile-dir:bbbb"})
    assert binding.state == ACCOUNT_MAPPING_REQUIRED
    assert binding.runnable is False


def test_a_half_provable_pair_is_never_half_migrated():
    """One resolvable alias does not license discarding the other one."""
    binding = resolve_prepared_binding("claude1", "claude2",
                                       donors={"claude1": CANON}, proven={CANON})
    assert binding.state == ACCOUNT_MAPPING_REQUIRED


def test_a_canonical_account_contradicted_by_its_launcher_is_required():
    binding = resolve_prepared_binding(CANON, "claude2", donors=DONORS,
                                       proven={CANON, "claude:profile-dir:bbbb"})
    assert binding.state == ACCOUNT_MAPPING_REQUIRED


def test_a_launcher_naming_a_foreign_account_is_required():
    other = "claude:profile-dir:bbbb"
    binding = resolve_prepared_binding(CANON, target_id(other), donors=DONORS,
                                       proven={CANON, other})
    assert binding.state == ACCOUNT_MAPPING_REQUIRED


def test_a_canonical_binding_already_in_place_is_ok():
    binding = resolve_prepared_binding(CANON, target_id(CANON), donors=DONORS,
                                       proven={CANON})
    assert binding.state == OK
    assert binding.account_id == CANON and binding.launcher_id == target_id(CANON)


def test_a_local_tool_binding_is_left_alone():
    for launcher_id in ("opencode", "freebuff", "cline", "zcode"):
        binding = resolve_prepared_binding("", launcher_id, donors=DONORS,
                                           proven={CANON})
        assert binding.state == OK, launcher_id
        assert binding.launcher_id == launcher_id


def test_auto_and_sync_sentinels_are_not_account_bindings():
    for account_id, launcher_id in (("AUTO", "AUTO"), ("SYNC", "SYNC")):
        binding = resolve_prepared_binding(account_id, launcher_id, donors=DONORS,
                                           proven={CANON})
        assert binding.state == OK


# ── the store pass ──────────────────────────────────────────────────────────

def _job(prepared_id: str, account_id: str, launcher_id: str,
         state: JobState = JobState.ARMED) -> PreparedJob:
    return PreparedJob(
        prepared_id, prepared_id, "project", launcher_id, account_id,
        Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
        {"at": (NOW.replace(hour=13)).isoformat()},
        enabled=True, state=state, created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(), next_due_at=NOW.replace(hour=13).isoformat(),
    )


def _store(tmp_path, *jobs: PreparedJob) -> PreparedStore:
    store = PreparedStore(tmp_path / "resources.sqlite3")
    for job in jobs:
        store.save(job)
    return store


def test_the_resolver_never_reads_the_plane_itself():
    """One refresh owner per process: the resolver is handed both tables.

    A resolver that could spawn its own ``sai-accounts list`` is how a scheduler
    thread ends up with a second, uncoordinated view of the registry.
    """
    import inspect

    source = inspect.getsource(resolve_prepared_binding) + inspect.getsource(
        migrate_prepared_bindings)
    for forbidden in ("subprocess", "snapshot(", "read_registry(", "sai_accounts"):
        assert forbidden not in source, forbidden


def test_the_pass_repoints_what_it_can_and_blocks_what_it_cannot(tmp_path):
    store = _store(tmp_path,
                   _job("good", "claude1", "claude1"),
                   _job("bad", "claude1", "claude2"),
                   _job("tool", "", "opencode"))
    report = migrate_prepared_bindings(store, donors=DONORS, proven={CANON})
    assert report.migrated == ("good",)
    assert report.blocked == ("bad",)

    good = store.get("good")
    assert good.account_id == CANON and good.launcher_id == target_id(CANON)
    # Only the binding was stale: the job keeps its trigger, payload and
    # enablement, so a migrated row is still the job the operator armed.
    assert good.trigger == Trigger.ON_TIME and good.enabled is True
    assert good.state == JobState.ARMED and good.payload_config == {"text": "cc"}

    unchanged = store.get("tool")
    assert unchanged.launcher_id == "opencode" and unchanged.account_id == ""


def test_a_blocked_row_keeps_its_history_and_says_why(tmp_path):
    store = _store(tmp_path, _job("bad", "claude1", "claude2"))
    migrate_prepared_bindings(store, donors=DONORS, proven={CANON})
    blocked = store.get("bad")
    assert blocked is not None, "a row is annotated, never deleted"
    assert blocked.state == JobState.BLOCKED
    assert blocked.waiting_reason.startswith(ACCOUNT_MAPPING_REQUIRED)
    # The original binding stays readable: that is the evidence an operator
    # needs in order to decide by hand.
    assert blocked.account_id == "claude1" and blocked.launcher_id == "claude2"
    assert blocked.enabled is True
    assert [j.prepared_id for j in store.list()] == ["bad"]


def test_the_pass_is_idempotent(tmp_path):
    store = _store(tmp_path, _job("good", "claude1", "claude1"),
                   _job("bad", "claude1", "claude2"))
    first = migrate_prepared_bindings(store, donors=DONORS, proven={CANON})
    second = migrate_prepared_bindings(store, donors=DONORS, proven={CANON})
    assert first.migrated == ("good",)
    assert second.migrated == () and second.blocked == ()


def test_an_outage_decides_nothing(tmp_path):
    """No authoritative snapshot (``proven=None``) changes nothing at all."""
    store = _store(tmp_path, _job("good", "claude1", "claude1"))
    report = migrate_prepared_bindings(store, donors=DONORS, proven=None)
    row = store.get("good")
    assert row.state == JobState.ARMED and row.launcher_id == "claude1"
    assert report.migrated == () and report.blocked == ()


def test_an_authoritative_snapshot_without_the_account_blocks(tmp_path):
    """A plane that answered "no such account" is a different answer from silence."""
    store = _store(tmp_path, _job("gone", "claude1", "claude1"))
    report = migrate_prepared_bindings(store, donors=DONORS, proven=set())
    assert report.blocked == ("gone",)
    assert store.get("gone").state == JobState.BLOCKED


def test_a_canonical_row_survives_a_migration_pass_untouched(tmp_path):
    store = _store(tmp_path, _job("now", CANON, target_id(CANON),
                                  state=JobState.WAITING_LIMIT))
    migrate_prepared_bindings(store, donors=DONORS, proven={CANON})
    row = store.get("now")
    assert row.state == JobState.WAITING_LIMIT and row.waiting_reason == ""


# ── the effective inventory the worker actually reads ───────────────────────

def test_the_federated_inventory_makes_the_migrated_target_real(tmp_path,
                                                                monkeypatch):
    """The re-pointed id must name a row the launch authority will accept."""
    from audapack import sai_accounts as sai
    from audapack.launch_authority import resolve_launch_target
    from audapack.launch_federation import sync_effective_launchers
    from audapack.models import Project

    (tmp_path / ".claude").mkdir()
    payload = json.dumps({
        "schema": "sai.accounts/list/1", "generation": 1, "status": "ok",
        "accounts": [{
            "account_id": CANON, "provider_id": "claude",
            "display_name": "Claude 1", "compact_label": "C1",
            "execution_backend": "profile_directory",
            "execution_context_id": "current-user",
            "operational_state": "ENABLED", "hidden": False,
            "context_label": None,
            "provider_metadata": {"profile_locator": str(tmp_path / ".claude")},
        }],
    })
    monkeypatch.setattr(sai, "TestEngine", lambda: r"C:\fake\sai-accounts.exe")
    monkeypatch.setattr(sai, "TestRun",
                        lambda argv, timeout: {"ok": True, "error": "", "stdout": payload})
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("opencode", "OpenCode", "OC"),
                        LauncherConfig("claude1", "Claude 1", "C1")]
    sync_effective_launchers(config, home=tmp_path)

    job = _job("good", "claude1", "claude1")
    store = _store(tmp_path, job)
    migrate_prepared_bindings(store, donors={"claude1": CANON}, proven={CANON})
    row = store.get("good")
    target = resolve_launch_target(config, "project", row.launcher_id,
                                   account=_account(CANON), prepared=True)
    assert target.launcher.id == target_id(CANON)
    assert target.account.account_id == CANON


def _account(account_id: str):
    from audapack.account_registry import AccountIdentity
    return AccountIdentity(account_id, "claude", "Claude 1", "C:/x",
                           (target_id(account_id),), "shared", NOW.isoformat())


# ── the live scheduler path ─────────────────────────────────────────────────

def _federated_config(tmp_path, *launchers: LauncherConfig) -> AppConfig:
    from audapack.models import Project

    config = AppConfig()
    config.federation.active = True
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = list(launchers)
    return config


def test_the_worker_blocks_a_row_it_cannot_resolve(tmp_path, monkeypatch):
    """A retired id the registry cannot prove never reaches a launch attempt."""
    from audapack.limits import LimitStore
    from audapack.prepared_worker import PreparedWorker

    other = "claude:profile-dir:bbbb"
    config = _federated_config(tmp_path,
                               LauncherConfig(target_id(other), "Claude 2", "C2"))
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts", lambda *a, **k: [])
    store = _store(tmp_path, _job("stale", "claude1", "claude1"))
    worker = PreparedWorker(config, clock=lambda: NOW,
                            limit_store=LimitStore(tmp_path / "resources.sqlite3"),
                            prepared_store=store)
    worker.tick()
    row = store.get("stale")
    assert row.state == JobState.BLOCKED
    assert ACCOUNT_MAPPING_REQUIRED in row.waiting_reason
    assert row.launcher_id == "claude1", "the old binding stays readable"


def test_the_worker_repoints_a_row_it_can_resolve(tmp_path, monkeypatch):
    """An unambiguous legacy row is re-pointed on the live scheduler path."""
    from audapack.account_registry import AccountIdentity
    from audapack.limits import LimitStore
    from audapack.prepared_worker import PreparedWorker

    config = _federated_config(tmp_path,
                               LauncherConfig(target_id(CANON), "Claude 1", "C1"))
    identity = AccountIdentity(CANON, "claude", "Claude 1", "C:/x",
                               (target_id(CANON),), "shared", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *a, **k: [identity])
    monkeypatch.setattr("audapack.launch_federation.legacy_donors",
                        lambda home: {"claude1": CANON})
    store = _store(tmp_path, _job("legacy", "claude1", "claude1"))
    worker = PreparedWorker(config, clock=lambda: NOW,
                            limit_store=LimitStore(tmp_path / "resources.sqlite3"),
                            prepared_store=store)
    worker.tick()
    row = store.get("legacy")
    assert row.launcher_id == target_id(CANON) and row.account_id == CANON
    assert row.state != JobState.BLOCKED


def test_a_standalone_installation_keeps_its_local_binding(tmp_path, monkeypatch):
    """Before a machine has ever federated, a legacy row is still its own tool."""
    from audapack.account_registry import AccountIdentity
    from audapack.limits import LimitStore
    from audapack.prepared_worker import PreparedWorker

    config = AppConfig()
    config.federation.active = False
    config.launchers = [LauncherConfig("claude1", "Claude 1", "C1")]
    identity = AccountIdentity("claude1", "claude", "Claude 1", "C:/x",
                               ("claude1",), "local", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *a, **k: [identity])
    store = _store(tmp_path, _job("local", "claude1", "claude1"))
    worker = PreparedWorker(config, clock=lambda: NOW,
                            limit_store=LimitStore(tmp_path / "resources.sqlite3"),
                            prepared_store=store)
    worker.tick()
    row = store.get("local")
    assert row.launcher_id == "claude1" and row.state != JobState.BLOCKED


def test_a_finished_row_is_history_not_a_candidate(tmp_path):
    """A DONE row keeps saying what it did; the pass never re-labels it."""
    store = _store(tmp_path, _job("done", "claude1", "claude1", state=JobState.DONE),
                   _job("missed", "claude1", "claude2", state=JobState.MISSED))
    report = migrate_prepared_bindings(store, donors=DONORS, proven={CANON})
    assert report.migrated == () and report.blocked == ()
    done = store.get("done")
    assert done.state == JobState.DONE and done.launcher_id == "claude1"
    assert done.waiting_reason == ""
    assert store.get("missed").state == JobState.MISSED


def test_the_worker_never_blocks_a_finished_row(tmp_path, monkeypatch):
    """A finished row is not a candidate: no re-point, no re-label, no re-arm.

    A terminal row is what happened. The tick loop has nothing to decide about
    it, so it may neither rewrite its binding nor downgrade its state to
    WAITING_LIMIT ("account not discovered") when its account later leaves the
    effective inventory.
    """
    from audapack.limits import LimitStore
    from audapack.prepared_worker import PreparedWorker

    config = _federated_config(tmp_path,
                               LauncherConfig(target_id(CANON), "Claude 1", "C1"))
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts", lambda *a, **k: [])
    store = _store(tmp_path, _job("done", "claude1", "claude1", state=JobState.DONE))
    worker = PreparedWorker(config, clock=lambda: NOW,
                            limit_store=LimitStore(tmp_path / "resources.sqlite3"),
                            prepared_store=store)
    worker.tick()
    row = store.get("done")
    assert row.launcher_id == "claude1" and row.account_id == "claude1"
    assert row.state == JobState.DONE
    assert row.waiting_reason == ""


@pytest.mark.parametrize("state", [JobState.DONE, JobState.MISSED,
                                   JobState.CANCELLED, JobState.FAILED_TERMINAL])
def test_the_worker_leaves_every_terminal_row_alone(tmp_path, monkeypatch, state):
    """Every terminal state survives a tick whose account is not discovered."""
    from audapack.limits import LimitStore
    from audapack.prepared_worker import PreparedWorker

    config = _federated_config(tmp_path,
                               LauncherConfig(target_id(CANON), "Claude 1", "C1"))
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts", lambda *a, **k: [])
    store = _store(tmp_path, _job("row", "claude1", "claude1", state=state))
    worker = PreparedWorker(config, clock=lambda: NOW,
                            limit_store=LimitStore(tmp_path / "resources.sqlite3"),
                            prepared_store=store)
    worker.tick()
    row = store.get("row")
    assert row.state == state and row.waiting_reason == ""
    assert row.state not in (JobState.ARMED, JobState.CLAIMED)
