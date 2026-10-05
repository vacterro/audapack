"""CP-11 prepared AUDIT runtime: capability gates, idempotent start, crash adoption.

These are the focused deterministic proofs for the Bridge-owned prepared audit
runtime. They use a fake AuditRunCoordinator (a fake ``coordinator`` with the
exact surface PreparedAuditRuntime touches) so no Qt, no browser and no real
Bridge is required -- the headless construction path is proven separately in
``test_prepared_audit_headless``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from audapack.account_registry import AccountIdentity, AccountRegistry
from audapack.config import AppConfig, LauncherConfig
from audapack.limits import LimitStore
from audapack.models import Project
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger, evaluate_trigger
from audapack.prepared_audit import (
    AccountBinding,
    AuditProgressState,
    CapabilityTruth,
    PreparedAuditRuntime,
)
from audapack.prepared_worker import PreparedWorker

NOW = datetime(2030, 6, 1, 10, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- fakes
@dataclass
class FakeResult:
    ok: bool
    project_id: str
    intent_id: str = ""
    dispatch_id: str = ""
    state: str = "QUEUED"
    message: str = ""
    duplicate: bool = False


@dataclass
class FakeSnapshot:
    project_id: str
    intent_id: str
    operator_state: str
    dispatch_id: str = ""
    error: str = ""
    campaign_run_id: str = ""
    recovery: str = ""


class FakeProjects:
    def __init__(self, projects):
        self.values = {p.id: p for p in projects}
        self.config = AppConfig()

    def get_project(self, project_id):
        return self.values.get(project_id)


class FakeCoordinator:
    """The exact AuditRunCoordinator surface PreparedAuditRuntime touches.

    ``starts`` records every start call so a test can prove one prepared event
    yields exactly one coordinator start. Adoption is modelled the way the real
    intent store behaves: a repeat start for the same source_execution_id
    returns the already-created intent/dispatch as a duplicate.
    """

    def __init__(self, projects):
        self.projects = FakeProjects(projects)
        self.bridge = self  # active_browser_job lives here for the test
        self.intents = self
        self.starts: list[tuple[str, str]] = []
        self._by_source: dict[str, dict] = {}
        self._dispatch_counter = 0
        self._runs: dict[str, FakeSnapshot] = {}
        self._active_jobs: dict[str, dict] = {}
        self.fail_start: str | None = None

    # -- coordinator.start
    def start(self, project_id, profile_id, provision=True, source_execution_id=""):
        self.starts.append((project_id, source_execution_id))
        prior = self._by_source.get(source_execution_id)
        if prior is not None:
            return FakeResult(True, project_id, prior["intent_id"],
                              prior["dispatch_id"], "QUEUED",
                              "already exists", duplicate=True)
        if self.fail_start == "pre_start":
            return FakeResult(False, project_id, "", "", "FAILED",
                              "packing failed", duplicate=False)
        if self.fail_start == "blocked":
            return FakeResult(False, project_id, "int-x", "", "BLOCKED",
                              "another audit owns this project", duplicate=False)
        self._dispatch_counter += 1
        intent_id = f"int-{source_execution_id or self._dispatch_counter}"
        dispatch_id = f"dsp-{self._dispatch_counter:04d}"
        self._by_source[source_execution_id] = {
            "intent_id": intent_id, "dispatch_id": dispatch_id,
            "project_id": project_id, "source_execution_id": source_execution_id,
        }
        self._runs[intent_id] = FakeSnapshot(project_id, intent_id, "WAITING",
                                             dispatch_id)
        return FakeResult(True, project_id, intent_id, dispatch_id, "QUEUED", "queued")

    # -- intents.find_for_source / find_for_dispatch
    def find_for_source(self, source_execution_id):
        return dict(self._by_source[source_execution_id]) if source_execution_id in self._by_source else None

    def find_for_dispatch(self, dispatch_id):
        for record in self._by_source.values():
            if record["dispatch_id"] == dispatch_id:
                return dict(record)
        return None

    # -- bridge.active_browser_job
    def active_browser_job(self, project_id):
        return self._active_jobs.get(project_id)

    # -- coordinator.refresh_runs
    def refresh_runs(self, project_ids=None):
        wanted = set(project_ids) if project_ids else None
        return [s for s in self._runs.values()
                if wanted is None or s.project_id in wanted]

    # -- test helpers
    def set_run_state(self, intent_id, operator_state, **kwargs):
        run = self._runs[intent_id]
        self._runs[intent_id] = replace(run, operator_state=operator_state, **kwargs)

    def add_unrelated_active(self, project_id, dispatch_id="manual-1"):
        self._active_jobs[project_id] = {"dispatch_id": dispatch_id, "state": "AUDITING"}


def _audit_job(*, account="codex:2", model="", effort="", policy=None,
               profile_id="quick3", at=None):
    payload = {"profile_id": profile_id}
    if policy:
        payload.update(policy)
    return PreparedJob(
        "audit-job", "audit", "project", "main_codex2", account,
        Trigger.ON_TIME, Payload.AUDIT, payload,
        {"at": (at or NOW).isoformat()}, model=model, effort=effort,
        enabled=True, state=JobState.ARMED,
    )


def _project():
    return Project(id="project", display_name="Project", source_path="C:/project")


def _runtime(coordinator=None, verifier=None):
    coordinator = coordinator or FakeCoordinator([_project()])
    return PreparedAuditRuntime(coordinator, AppConfig(), identity_verifier=verifier)


# --------------------------------------------------------------------------- gate
def test_exact_account_without_browser_binding_stays_unbound_and_waits():
    runtime = _runtime()
    gate = runtime.gate(_audit_job())
    assert gate.account_binding is AccountBinding.ACCOUNT_UNBOUND
    assert not gate.ok
    assert gate.reason == ("Audit waiting: browser account binding for "
                           "codex:2 is not verified")


def test_verified_browser_account_binding_passes_gate():
    runtime = _runtime(verifier=lambda account_id: account_id == "codex:2")
    gate = runtime.gate(_audit_job())
    assert gate.account_binding is AccountBinding.VERIFIED_BROWSER_ACCOUNT
    assert gate.ok and gate.reason == ""


def test_explicit_account_agnostic_policy_is_permitted():
    runtime = _runtime()
    gate = runtime.gate(_audit_job(policy={"account_policy": "ANY_ELIGIBLE"}))
    assert gate.account_binding is AccountBinding.ACCOUNT_AGNOSTIC_EXPLICIT
    assert gate.ok


def test_exact_account_is_never_silently_reinterpreted_as_agnostic():
    runtime = _runtime()
    # No policy declared: an exact-account job never drifts to agnostic mode.
    assert runtime.gate(_audit_job()).account_binding is AccountBinding.ACCOUNT_UNBOUND


def test_invalid_audit_profile_fails_before_dispatch():
    runtime = _runtime(verifier=lambda _a: True)
    gate = runtime.gate(_audit_job(profile_id="does-not-exist"))
    assert not gate.ok
    assert gate.reason == "audit profile does-not-exist no longer exists"


def test_missing_audit_profile_fails_before_dispatch():
    runtime = _runtime(verifier=lambda _a: True)
    gate = runtime.gate(_audit_job(profile_id=""))
    assert not gate.ok and "profile missing" in gate.reason


def test_exact_model_unverifiable_is_not_silently_downgraded():
    runtime = _runtime(verifier=lambda _a: True)
    gate = runtime.gate(_audit_job(model="gpt-6-pro"))
    assert gate.model_truth is CapabilityTruth.UNSUPPORTED
    assert not gate.ok
    assert gate.reason == ("Audit waiting: requested browser model gpt-6-pro "
                           "cannot be verified")


def test_exact_effort_unverifiable_is_not_silently_ignored():
    runtime = _runtime(verifier=lambda _a: True)
    gate = runtime.gate(_audit_job(effort="high"))
    assert gate.effort_truth is CapabilityTruth.UNSUPPORTED
    assert not gate.ok
    assert gate.reason == "Audit waiting: requested effort high cannot be verified"


def test_model_effort_declared_as_account_default_are_accepted():
    runtime = _runtime(verifier=lambda _a: True)
    gate = runtime.gate(_audit_job(
        model="gpt-6-pro", effort="high",
        policy={"model_policy": "ACCOUNT_DEFAULT", "effort_policy": "ACCOUNT_DEFAULT"}))
    assert gate.model_truth is CapabilityTruth.ACCOUNT_DEFAULT
    assert gate.effort_truth is CapabilityTruth.ACCOUNT_DEFAULT
    assert gate.ok


# --------------------------------------------------------------------- dry run
def test_dry_run_performs_no_side_effect():
    coordinator = FakeCoordinator([_project()])
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    report = runtime.dry_run(_audit_job())
    assert report["ok"] is True
    assert report["audit_profile"] == "quick3"
    assert report["account_binding"] == "VERIFIED_BROWSER_ACCOUNT"
    assert coordinator.starts == []  # no start, no packing, no dispatch


def test_dry_run_reports_missing_project():
    coordinator = FakeCoordinator([])  # project not registered
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    report = runtime.dry_run(_audit_job())
    assert not report["ok"] and report["project_reason"]


def test_dry_run_reports_unrelated_active_audit_conflict():
    coordinator = FakeCoordinator([_project()])
    coordinator.add_unrelated_active("project")
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    report = runtime.dry_run(_audit_job())
    assert not report["ok"]
    assert report["conflict_reason"] == "project has an unrelated active audit"


# ----------------------------------------------------------------- start / poll
def test_fresh_execution_starts_exactly_one_coordinator_start():
    coordinator = FakeCoordinator([_project()])
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    result = runtime.start(_audit_job(), "exec-1")
    assert result.ok and result.dispatch_id
    assert coordinator.starts == [("project", "exec-1")]


def test_source_execution_id_is_the_idempotency_anchor():
    coordinator = FakeCoordinator([_project()])
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    first = runtime.start(_audit_job(), "exec-1")
    second = runtime.start(_audit_job(), "exec-1")
    assert first.dispatch_id == second.dispatch_id
    assert second.duplicate
    # Two calls, one dispatch: the coordinator adopted rather than re-created.
    assert coordinator.starts == [("project", "exec-1"), ("project", "exec-1")]
    assert coordinator._dispatch_counter == 1


def test_poll_maps_ready_to_done():
    coordinator = FakeCoordinator([_project()])
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    result = runtime.start(_audit_job(), "exec-1")
    coordinator.set_run_state(result.intent_id, "READY")
    progress = runtime.poll("project", "exec-1")
    assert progress.state is AuditProgressState.DONE
    assert progress.dispatch_id == result.dispatch_id


def test_poll_absent_when_no_intent():
    runtime = _runtime()
    assert runtime.poll("project", "never").state is AuditProgressState.ABSENT


def test_poll_post_start_block_is_recovery_not_retry():
    coordinator = FakeCoordinator([_project()])
    runtime = _runtime(coordinator, verifier=lambda _a: True)
    result = runtime.start(_audit_job(), "exec-1")
    coordinator.set_run_state(result.intent_id, "BLOCKED_POST_START")
    progress = runtime.poll("project", "exec-1")
    assert progress.state is AuditProgressState.FAILED_POST_START


# ----------------------------------------------------- worker integration
def _worker(tmp_path, monkeypatch, coordinator, job, *, account=True):
    identity = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_a, **_k: [identity] if account else [])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    jobs.save(job)
    runtime = PreparedAuditRuntime(coordinator, config,
                                   identity_verifier=lambda _a: True)
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            audit_runtime=runtime)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)
    return worker, jobs


def test_worker_without_audit_runtime_still_gates_audit_and_runs_cli(tmp_path, monkeypatch):
    identity = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_a, **_k: [identity])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    jobs.save(_audit_job())
    cli = PreparedJob("cli-job", "cc", "project", "main_codex2", "codex:2",
                      Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                      {"at": (NOW + timedelta(hours=1)).isoformat()},
                      enabled=True, state=JobState.ARMED)
    jobs.save(cli)
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            audit_runtime=None)
    worker._probe_due = lambda _accounts: None
    worker.tick()
    assert jobs.get("audit-job").state == JobState.WAITING_LIMIT
    assert jobs.get("audit-job").waiting_reason == (
        "Audit unavailable: AuditRunCoordinator runtime not connected")
    # The CLI job is untouched by the audit gate; it simply waits for its time.
    assert jobs.get("cli-job").state == JobState.WAITING_TRIGGER
    worker.stop()


def test_worker_exact_account_no_binding_creates_no_dispatch(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    identity = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_a, **_k: [identity])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    jobs.save(_audit_job())
    # No verifier -> ACCOUNT_UNBOUND
    runtime = PreparedAuditRuntime(coordinator, config)
    worker = PreparedWorker(config, clock=lambda: NOW,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            audit_runtime=runtime)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)
    worker.tick()
    assert coordinator.starts == []
    assert jobs.get("audit-job").state == JobState.WAITING_LIMIT
    assert "not verified" in jobs.get("audit-job").waiting_reason
    worker.stop()


def test_worker_fresh_audit_claims_and_delivers_once(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    worker.tick()
    assert coordinator.starts == [("project", jobs.receipt("audit-job", _event_id(jobs))["execution_id"])]
    assert jobs.get("audit-job").state == JobState.DELIVERING
    worker.stop()


def test_worker_missing_project_fails_before_dispatch(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([])  # project unregistered in coordinator
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    # Coordinator.start returns not-ok/no dispatch for a missing project.
    coordinator.fail_start = "pre_start"
    worker.tick()
    assert coordinator.starts  # start was attempted
    assert jobs.get("audit-job").state == JobState.FAILED_RETRYABLE
    worker.stop()


def test_worker_blocked_start_is_terminal(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    coordinator.fail_start = "blocked"
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    worker.tick()
    assert jobs.get("audit-job").state == JobState.FAILED_TERMINAL
    worker.stop()


def test_worker_unrelated_active_audit_blocks_without_stealing(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    coordinator.add_unrelated_active("project")
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    worker.tick()
    assert coordinator.starts == []  # never stole the unrelated audit
    assert jobs.get("audit-job").state == JobState.WAITING_LIMIT
    assert "unrelated active audit" in jobs.get("audit-job").waiting_reason
    worker.stop()


def test_worker_restart_after_dispatch_adopts_same_dispatch(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    worker.tick()
    execution_id = jobs.receipt("audit-job", _event_id(jobs))["execution_id"]
    assert jobs.get("audit-job").state == JobState.DELIVERING
    # Expire the lease and reconcile on the "restarted" worker. Adoption is by
    # querying source_execution_id, not by a second start: no new dispatch.
    expired = NOW + timedelta(seconds=200)
    worker2, _ = _worker_reopen(tmp_path, monkeypatch, coordinator, clock=expired)
    worker2.tick()
    assert coordinator._dispatch_counter == 1  # one dispatch, adopted
    assert coordinator.starts == [("project", execution_id)]  # never re-started
    worker2.stop()
    worker.stop()


def test_worker_dispatch_terminal_success_converges_to_done(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    worker.tick()
    receipt = jobs.receipt("audit-job", _event_id(jobs))
    coordinator.set_run_state(receipt["result"].split()[1], "READY") if False else None
    # Mark the run READY through the source record.
    record = coordinator.find_for_source(receipt["execution_id"])
    coordinator.set_run_state(record["intent_id"], "READY")
    expired = NOW + timedelta(seconds=200)
    worker2, jobs2 = _worker_reopen(tmp_path, monkeypatch, coordinator, clock=expired)
    worker2.tick()
    assert jobs2.get("audit-job").state == JobState.DONE
    worker2.stop()
    worker.stop()


def test_worker_post_start_failure_is_recovery_not_auto_retry(tmp_path, monkeypatch):
    coordinator = FakeCoordinator([Project("project", "Project", str(tmp_path))])
    worker, jobs = _worker(tmp_path, monkeypatch, coordinator, _audit_job())
    worker.tick()
    receipt = jobs.receipt("audit-job", _event_id(jobs))
    record = coordinator.find_for_source(receipt["execution_id"])
    coordinator.set_run_state(record["intent_id"], "BLOCKED_POST_START")
    expired = NOW + timedelta(seconds=200)
    worker2, jobs2 = _worker_reopen(tmp_path, monkeypatch, coordinator, clock=expired)
    worker2.tick()
    assert jobs2.get("audit-job").state == JobState.RECOVERY_REQUIRED
    # No second dispatch created by recovery.
    assert coordinator._dispatch_counter == 1
    worker2.stop()
    worker.stop()


# --------------------------------------------------------------------- helpers
def _event_id(jobs):
    job = jobs.get("audit-job")
    return evaluate_trigger(job, None, NOW).event_id


def _worker_reopen(tmp_path, monkeypatch, coordinator, *, clock):
    identity = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                               ("main_codex2",), "test", NOW.isoformat())
    monkeypatch.setattr("audapack.prepared_worker.discover_accounts",
                        lambda *_a, **_k: [identity])
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    jobs = PreparedStore(path)
    runtime = PreparedAuditRuntime(coordinator, config,
                                   identity_verifier=lambda _a: True)
    worker = PreparedWorker(config, clock=lambda: clock,
                            account_registry=AccountRegistry(path),
                            limit_store=LimitStore(path), prepared_store=jobs,
                            audit_runtime=runtime)
    worker._probe_due = lambda _accounts: None
    worker._pool.submit = lambda fn, *args: fn(*args)
    return worker, jobs
