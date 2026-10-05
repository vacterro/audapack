from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from audapack.models import AuditSnapshot, Project
from audapack.services.audit_run_service import (
    AuditRunCoordinator,
    AuditRunSnapshot,
    AuditStartIntentStore,
    ManagedWorkerSupervisor,
    _actions_for,
)


class FakeProjects:
    def __init__(self, projects):
        self.values = {project.id: project for project in projects}

    def get_project(self, project_id):
        return self.values.get(project_id)


class FakePacking:
    def __init__(self, root: Path, failures=()):
        self.root = root
        self.failures = set(failures)
        self.calls = []
        self.repacks = []

    def pack_project(self, project_id, **_kwargs):
        self.repacks.append(project_id)
        return self.ensure_fresh_archive(project_id)

    def ensure_fresh_archive(self, project_id):
        self.calls.append(project_id)
        if project_id in self.failures:
            return SimpleNamespace(success=False, output_path=None, error_message="pack denied")
        path = self.root / f"{project_id}.zip"
        path.write_bytes(project_id.encode())
        return SimpleNamespace(success=True, output_path=path, error_message="")


class FakeAudits:
    def __init__(self):
        self.snapshots = {}
        #: PERF-002 counting: the dashboard must stop invalidating a working
        #: audit cache on every tick.
        self.rescans = 0

    def get_snapshot(self, project_id, force_rescan=False):
        if force_rescan:
            self.rescans += 1
        return self.snapshots.get(project_id)

    def refresh_project(self, project_id):
        return self.get_snapshot(project_id, force_rescan=True)



class FakeBridge:
    def __init__(self):
        self.healthy = True
        self.jobs = []
        self.workers = []
        self.cancel_error = ""
        self.abandon_error = ""
        #: STOP raced the run to a real end: the bridge hands back the terminal
        #: job UNCHANGED (abandon_job is idempotent) instead of forcing FAILED.
        self.abandon_terminal_state = ""
        self.submits = 0
        #: PERF-002 counting: how many times this tick asked for /v1/browser/status.
        self.status_calls = 0


    def runtime_status(self):
        return {"healthy": self.healthy, "browser": self._status()}

    def start(self):
        self.healthy = True
        return True, "started"

    def _status(self):
        active = [job for job in self.jobs if job["state"] not in {"COMPLETE", "FAILED", "CANCELLED"}]
        return {
            "active_workers": len(self.workers),
            "active_jobs": len(active),
            "queued_jobs": sum(job["state"] == "QUEUED" for job in active),
            "workers": self.workers,
        }

    def browser_status(self):
        self.status_calls = getattr(self, "status_calls", 0) + 1
        return {"ok": True, "dispatch": self._status()}


    def browser_jobs(self, project_id=None):
        jobs = self.jobs
        if project_id:
            jobs = [job for job in jobs if job["project_id"] == project_id]
        return {"ok": True, "jobs": [dict(job) for job in jobs]}

    def active_browser_job(self, project_id):
        return next(
            (job for job in reversed(self.jobs) if job["project_id"] == project_id and job["state"] not in {"COMPLETE", "FAILED", "CANCELLED"}),
            None,
        )

    def submit_browser_audit(self, project, archive_path, profile):
        self.submits += 1
        job = {
            "dispatch_id": f"dsp-{self.submits:016d}",
            "project_id": project.id,
            "project_name": project.display_name,
            "state": "QUEUED",
            "assigned_worker_id": "",
            "campaign_run_id": "",
            "profile": profile,
            "created_at": float(self.submits),
            "updated_at": float(self.submits),
            "completed_at": 0.0,
            "error": "",
            "final_handoff_path": "",
            "final_handoff_sha256": "",
        }
        self.jobs.append(job)
        return {"ok": True, "dispatch": dict(job)}

    def cancel_browser_job(self, dispatch_id):
        if self.cancel_error:
            return {"ok": False, "error": {"message": self.cancel_error}}
        job = next(item for item in self.jobs if item["dispatch_id"] == dispatch_id)
        job["state"] = "CANCELLED"
        return {"ok": True, "dispatch": dict(job)}

    def abandon_browser_job(self, dispatch_id, reason=""):
        if self.abandon_error:
            return {"ok": False, "error": {"message": self.abandon_error}}
        job = next(item for item in self.jobs if item["dispatch_id"] == dispatch_id)
        if self.abandon_terminal_state:
            return {
                "ok": True,
                "dispatch_id": dispatch_id,
                "state": self.abandon_terminal_state,
                "error": job["error"],
            }
        if job["state"] in {"COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"}:
            return {"ok": True, "dispatch_id": dispatch_id, "state": job["state"], "error": job["error"]}
        if job["state"] not in {"STARTING", "STARTED", "AUDITING", "SAVING", "BLOCKED"}:
            return {"ok": False, "error": {"code": "invalid_transition", "message": "only a post-start dispatch can be abandoned; cancel pre-start work instead"}}
        job["state"] = "FAILED"
        job["error"] = reason or "operator abandoned a stuck blocked run"
        job["last_error_code"] = "operator_abandoned"
        return {"ok": True, "dispatch_id": dispatch_id, "state": "FAILED", "error": job["error"]}


def project(project_id="p1"):
    return Project(id=project_id, display_name=f"Project {project_id}", source_path=f"C:/{project_id}")


def coordinator(tmp_path, projects=None, failures=(), supervisor=None):
    projects = projects or [project()]
    bridge = FakeBridge()
    audits = FakeAudits()
    service = AuditRunCoordinator(
        FakeProjects(projects),
        FakePacking(tmp_path, failures),
        bridge,
        audits,
        intent_store=AuditStartIntentStore(tmp_path / "audit_start_intents.json"),
        worker_supervisor=supervisor,
    )
    # Real batches wait for freshly launched windows to register; these
    # tests drive a fake pool that never will.
    service.pool_settle_seconds = 0.0
    return service, bridge, audits


def test_one_click_start_is_exactly_once_and_durable(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    first = service.start("p1")
    second = service.start("p1")
    assert first.ok and first.state == "QUEUED"
    assert second.ok and second.duplicate
    assert second.dispatch_id == first.dispatch_id
    assert bridge.submits == 1
    saved = json.loads((tmp_path / "audit_start_intents.json").read_text(encoding="utf-8"))
    assert saved["intents"][0]["dispatch_id"] == first.dispatch_id


def test_restart_reconstructs_predispatch_intent_without_duplicate(tmp_path):
    store = AuditStartIntentStore(tmp_path / "audit_start_intents.json")
    intent, created = store.begin("p1", "Project p1", "quick3")
    assert created
    journal = json.loads((tmp_path / "audit_start_intents.json").read_text(encoding="utf-8"))
    journal["intents"][0]["owner_pid"] = 999999
    (tmp_path / "audit_start_intents.json").write_text(json.dumps(journal), encoding="utf-8")
    service, bridge, _audits = coordinator(tmp_path)
    service.intents = AuditStartIntentStore(tmp_path / "audit_start_intents.json")
    runs = service.refresh_runs()
    assert runs[0].intent_id == intent["intent_id"]
    assert runs[0].operator_state == "INTERRUPTED"
    assert runs[0].summary == "INTERRUPTED · Resume Start"
    resumed = service.start("p1")
    assert resumed.ok and not resumed.duplicate
    assert bridge.submits == 1


def test_the_whole_batch_is_queued_and_a_pack_failure_is_isolated(tmp_path):
    """Seven projects, seven answers -- the seventh is not dropped on the floor.

    The pool holds more jobs than there are windows and a freed window claims
    the next one, so a batch bigger than the lane count is a LINE, not an
    overflow. Only the windows are capped.
    """
    projects = [project(f"p{index}") for index in range(1, 8)]
    service, bridge, _audits = coordinator(tmp_path, projects, failures={"p3"})
    results = service.start_batch([item.id for item in projects])
    assert len(results) == 7
    assert [result.project_id for result in results if not result.ok] == ["p3"]
    assert len(bridge.jobs) == 6


def test_ready_requires_complete_matching_run_waves_file_and_hash(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    started = service.start("p1")
    handoff = tmp_path / "final.md"
    handoff.write_text("durable result", encoding="utf-8")
    digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    job = bridge.jobs[0]
    job.update({
        "state": "COMPLETE",
        "campaign_run_id": "run-1",
        "final_handoff_path": str(handoff),
        "final_handoff_sha256": digest,
        # A real completion instant: this test is about the proof chain, not
        # about how long a COMPLETE run may stay unproven.
        "completed_at": time.time(),
        "updated_at": time.time(),
    })
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="wrong",
        completed_waves=3, total_waves=3, campaign_complete=True,
        final_handoff_ready=True, final_handoff_path=handoff, final_handoff_sha256=digest,
    )
    assert service.refresh_runs()[0].operator_state == "SAVING"
    audits.snapshots["p1"].campaign_run_id = "run-1"
    ready = service.refresh_runs()[0]
    assert ready.ready and ready.operator_state == "READY"
    assert ready.ready_proof == (
        "dispatch_complete", "project_match", "campaign_match",
        "waves_complete", "handoff_durable", "handoff_hash_match",
    )
    assert started.dispatch_id == ready.dispatch_id


def test_complete_with_missing_handoff_remains_saving(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "COMPLETE", "campaign_run_id": "run-1"})
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="run-1",
        completed_waves=3, total_waves=3, campaign_complete=True,
        final_handoff_ready=True, final_handoff_path=tmp_path / "missing.md",
        final_handoff_sha256="0" * 64,
    )
    run = service.refresh_runs()[0]
    assert not run.ready and run.operator_state == "SAVING"


def test_state_mapping_keeps_wave_progress_visible(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "AUDITING", "campaign_run_id": "run-1"})
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="run-1",
        completed_waves=1, total_waves=3,
    )
    run = service.refresh_runs()[0]
    assert run.operator_state == "AUDITING"
    assert run.summary == "AUDIT 1/3"


def test_old_campaign_progress_never_leaks_into_new_queued_run(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="old-run",
        completed_waves=3, total_waves=3, campaign_complete=True,
    )
    service.start("p1")
    run = service.refresh_runs()[0]
    assert run.operator_state == "WAITING"
    assert run.completed_waves == 0, "new run must not inherit old 3/3"
    assert run.summary == "WAITING FOR WORKER · 0/3"


def test_old_campaign_progress_never_leaks_into_new_auditing_run(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="old-run",
        completed_waves=3, total_waves=3, campaign_complete=True,
    )
    service.start("p1")
    bridge.jobs[0].update({"state": "AUDITING", "campaign_run_id": "new-run"})
    run = service.refresh_runs()[0]
    assert run.operator_state == "AUDITING"
    assert run.completed_waves == 0, "new-run progress must be 0 until its own waves save"
    assert run.summary == "AUDIT 0/3"


def test_first_matching_new_wave_saved_advances_progress(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "AUDITING", "campaign_run_id": "new-run"})
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="new-run",
        completed_waves=1, total_waves=3,
    )
    run = service.refresh_runs()[0]
    assert run.completed_waves == 1
    assert run.summary == "AUDIT 1/3"


def test_ready_proof_still_requires_matching_campaign(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    handoff = tmp_path / "final.md"
    handoff.write_text("durable", encoding="utf-8")
    digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    service.start("p1")
    bridge.jobs[0].update({
        "state": "COMPLETE", "campaign_run_id": "new-run",
        "final_handoff_path": str(handoff), "final_handoff_sha256": digest,
    })
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="old-run",
        completed_waves=3, total_waves=3, campaign_complete=True,
        final_handoff_ready=True, final_handoff_path=handoff, final_handoff_sha256=digest,
    )
    run = service.refresh_runs()[0]
    assert not run.ready, "old-run result must never satisfy new-run READY proof"
    assert run.operator_state == "SAVING"


def test_prestart_cancel_and_poststart_refusal_are_honest(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = service.start("p1")
    cancelled = service.cancel(started.dispatch_id)
    assert cancelled.ok and cancelled.state == "CANCELLED"
    bridge.cancel_error = "START already committed; recover same worker"
    refused = service.cancel(started.dispatch_id)
    assert not refused.ok and refused.state == "BLOCKED"
    assert "recover same worker" in refused.message


def test_blocked_prestart_retries_but_poststart_never_duplicates_core(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "BLOCKED", "error": "attachment timed out"})
    retried = service.start("p1")
    assert retried.ok and not retried.duplicate
    assert bridge.submits == 2

    bridge.jobs[-1].update({
        "state": "BLOCKED", "campaign_run_id": "run-committed",
        "start_receipt": "receipt-1", "recovery_state": "AUDITING",
    })
    refused = service.start("p1")
    assert refused.ok and refused.duplicate
    assert bridge.submits == 2


def test_retrying_exposes_bounded_attempt_and_reason(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "RETRYABLE", "retry_count": 2, "error": "attachment timeout"})
    run = service.refresh_runs()[0]
    assert run.operator_state == "RETRYING"
    assert run.summary == "RETRYING 2/5 · attachment timeout"


def test_managed_worker_capacity_is_six_slot_bounded_and_cooldown_safe(tmp_path):
    launches = []

    def launch(slot, generation):
        launches.append((slot, generation))
        return True, "launched"

    supervisor = ManagedWorkerSupervisor(launch, tmp_path / "workers.json", cooldown_seconds=60)
    first = supervisor.ensure_capacity({"active_workers": 0, "workers": []}, 99)
    second = supervisor.ensure_capacity({"active_workers": 0, "workers": []}, 99)
    assert first["desired"] == 6
    assert [slot for slot, _generation in launches] == [1, 2, 3, 4, 5, 6]
    assert second["launched"] == []


def test_managed_worker_heartbeat_prevents_relaunch(tmp_path):
    launches = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launches.append((slot, generation)) is None, "ok"),
        tmp_path / "workers.json",
    )
    result = supervisor.ensure_capacity({
        "active_workers": 1,
        "workers": [{"managed_slot": 1, "managed_generation": 1, "last_seen_at": 10.0}],
    }, 1)
    assert result["registered"] == 1
    assert launches == []


def test_diagnostics_redacts_paths_tokens_and_content():
    snapshot = AuditRunSnapshot(
        project_id="p1", project_name="Project", operator_state="READY", summary="ready",
        handoff_path=r"C:\Users\Private\secret-result.md", handoff_sha256="abc", ready=True,
        bridge_healthy=True, worker_counts={"active": 6, "clean": 2}, retry_count=2,
    )
    payload = AuditRunCoordinator.diagnostics(snapshot)
    assert "secret-result.md" in payload
    assert "Users" not in payload and "Private" not in payload
    assert "token" not in payload.lower()
    assert '"audapack_version"' in payload
    assert '"active": 6' in payload


def test_intent_history_is_bounded(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json", history_bound=6)
    for index in range(9):
        intent, _created = store.begin(f"p{index}", f"P{index}", "quick3")
        store.update(intent["intent_id"], status="FAILED", error="x")
    entries = store.list()
    assert len(entries) == 6
    assert entries[0]["project_id"] == "p3"


def test_eight_projects_all_join_the_queue(tmp_path):
    projects = [project(f"p{index}") for index in range(1, 9)]
    service, bridge, _audits = coordinator(tmp_path, projects)
    results = service.start_batch([item.id for item in projects])
    assert len(results) == 8
    assert len(bridge.jobs) == 8
    assert all(result.ok for result in results)


def test_a_long_queue_never_asks_for_more_than_six_windows(tmp_path):
    """The multiplying this must not do: one window per queued job.

    This is the ASK side. The supervisor clamps the answer too -- see
    test_managed_worker_capacity_is_six_slot_bounded_and_cooldown_safe, which
    hands it 99 and gets six slots -- so a raised ask cannot open a seventh
    window either. Both halves, because the ask is what the operator sees in
    the settle wait.
    """
    from audapack.services.audit_run_service import MAX_AUDIT_LANES

    asked = []

    class CountingSupervisor:
        def ensure_capacity(self, _status, demand):
            asked.append(int(demand))
            return {"desired": 0, "launched": []}

    projects = [project(f"p{index}") for index in range(1, 13)]
    service, _bridge, _audits = coordinator(tmp_path, projects, supervisor=CountingSupervisor())
    service.start_batch([item.id for item in projects])
    assert asked, "capacity was never provisioned"
    assert max(asked) <= MAX_AUDIT_LANES, asked


def test_duplicate_double_start_returns_same_dispatch_and_intent(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    first = service.start("p1")
    assert first.ok and first.dispatch_id
    second = service.start("p1")
    assert second.ok and second.duplicate
    assert second.intent_id == first.intent_id
    assert second.dispatch_id == first.dispatch_id


def test_wave_progress_never_increments_on_bridge_persistence_failure(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "AUDITING", "campaign_run_id": "run-1"})
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="run-1",
        completed_waves=2, total_waves=3,
    )
    run = service.refresh_runs()[0]
    assert run.completed_waves == 2
    assert run.summary == "AUDIT 2/3"
    audits.snapshots["p1"].completed_waves = 1
    run = service.refresh_runs()[0]
    assert run.completed_waves == 1
    assert run.summary == "AUDIT 1/3"


def test_batch_reuses_active_project_without_duplicate(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = service.start("p1")
    assert started.ok
    results = service.start_batch(["p1", "p1"])
    assert len(results) == 1
    assert results[0].duplicate
    assert results[0].dispatch_id == started.dispatch_id
    assert bridge.submits == 1


def test_busy_workers_queue_without_failure(tmp_path):
    service, bridge, _audits = coordinator(tmp_path, [project("p1"), project("p2")])
    service.start("p1")
    service.start("p2")
    assert bridge.submits == 2
    assert all(job["state"] == "QUEUED" for job in bridge.jobs)


def blocked_post_start_run(service, bridge):
    started = service.start("p1")
    bridge.jobs[0].update({
        "state": "BLOCKED",
        "campaign_run_id": "run-committed",
        "start_receipt": "receipt-1",
        "recovery_state": "AUDITING",
        "error": "worker lost after START_PREPARED; recovery required",
    })
    return started


def test_post_start_blocked_lane_offers_abandon_not_blind_start(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    blocked_post_start_run(service, bridge)
    run = service.refresh_runs()[0]
    assert run.operator_state == "BLOCKED_POST_START"
    assert "ABANDON" in run.actions
    assert "RETRY" not in run.actions, "a post-start block must never offer a blind new START"
    assert "CANCEL" not in run.actions, "CANCELLED would falsely assert no Core was sent"


def test_abandon_frees_the_project_for_a_new_start(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = blocked_post_start_run(service, bridge)
    abandoned = service.abandon(started.dispatch_id, "operator forced unblock")
    assert abandoned.ok and abandoned.state == "FAILED"
    assert bridge.jobs[0]["last_error_code"] == "operator_abandoned"

    resumed = service.start("p1")
    assert resumed.ok and not resumed.duplicate
    assert bridge.submits == 2, "the freed project accepts exactly one fresh dispatch"


def test_abandon_refusal_is_reported_honestly(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = blocked_post_start_run(service, bridge)
    bridge.abandon_error = "bridge offline"
    refused = service.abandon(started.dispatch_id)
    assert not refused.ok and refused.state == "BLOCKED"
    assert "bridge offline" in refused.message
    assert bridge.jobs[0]["state"] == "BLOCKED", "a refused abandon must not mutate the run"


def test_pre_start_block_frees_the_project_lane_for_the_next_start(tmp_path):
    """A pre-start BLOCKED run must never jam the project forever.

    Live evidence: three intents sat at status=RUNNING with
    error=canonical-start-rejected while their dispatch was BLOCKED, so the
    Project Room showed `BLOCKED PRE 0/3 1d21h` and every later START was
    refused as a duplicate.
    """
    service, bridge, _audits = coordinator(tmp_path)
    first = service.start("p1")
    assert first.ok and first.state == "QUEUED"

    job = next(item for item in bridge.jobs if item["dispatch_id"] == first.dispatch_id)
    job["state"] = "BLOCKED"
    job["error"] = "canonical-start-rejected: START AUDITING is not ready"

    runs = service.refresh_runs(["p1"])
    assert runs[0].operator_state == "BLOCKED_PRE_START"
    saved = json.loads((tmp_path / "audit_start_intents.json").read_text(encoding="utf-8"))
    assert saved["intents"][0]["status"] == "BLOCKED"

    second = service.start("p1")
    assert second.ok and not second.duplicate
    assert second.state == "QUEUED"
    assert second.dispatch_id != first.dispatch_id
    assert bridge.submits == 2
    assert job["state"] == "CANCELLED"


def test_post_start_block_still_belongs_to_the_operator(tmp_path):
    """A block that already committed a START receipt is never auto-swept."""
    service, bridge, _audits = coordinator(tmp_path)
    first = service.start("p1")
    job = next(item for item in bridge.jobs if item["dispatch_id"] == first.dispatch_id)
    job["state"] = "BLOCKED"
    job["error"] = "clean-state-lost"
    job["start_receipt"] = "startcore-abc123"
    job["campaign_run_id"] = "run-abc123"

    runs = service.refresh_runs(["p1"])
    assert runs[0].operator_state == "BLOCKED_POST_START"
    saved = json.loads((tmp_path / "audit_start_intents.json").read_text(encoding="utf-8"))
    assert saved["intents"][0]["status"] == "RECOVERY_NEEDED"

    second = service.start("p1")
    assert second.duplicate
    assert bridge.submits == 1
    assert job["state"] == "BLOCKED"


def test_blocked_summary_always_carries_a_next_step(tmp_path):
    from audapack.services.audit_run_service import blocked_guidance

    service, bridge, _audits = coordinator(tmp_path)
    started = service.start("p1")
    job = next(item for item in bridge.jobs if item["dispatch_id"] == started.dispatch_id)
    job["state"] = "BLOCKED"
    job["error"] = "pre-start retries exhausted: artifact-http-400:missing_archive"

    run = service.refresh_runs(["p1"])[0]
    assert run.operator_state == "BLOCKED_PRE_START"
    assert "NEXT:" in run.summary
    assert "PACK the project again" in run.summary

    why, action = blocked_guidance("clean-state-lost", True)
    assert "stopped being clean" in why
    assert "FORCE UNBLOCK" in action

    unknown_why, unknown_action = blocked_guidance("", False)
    assert unknown_why
    assert "START AUDIT" in unknown_action


def test_repeated_starts_never_open_more_windows_than_lanes(tmp_path):
    """A launched window is invisible until it registers.

    Counting only registered workers is how repeated START presses opened a 7th
    and 8th Chromium window while the dispatcher still reported free capacity.
    """
    from audapack.services.audit_run_service import MAX_AUDIT_LANES

    launches = []

    def launch(slot, generation):
        launches.append((slot, generation))
        return True, f"started slot {slot}"

    supervisor = ManagedWorkerSupervisor(
        launch,
        path=tmp_path / "managed_browser_workers.json",
        cooldown_seconds=1.0,
    )

    # Nothing ever registers: every pass sees active_workers 0.
    dispatch = {"active_workers": 0, "workers": []}
    for _ in range(12):
        supervisor.ensure_capacity(dispatch, MAX_AUDIT_LANES)

    assert len(launches) == MAX_AUDIT_LANES, launches
    assert sorted(slot for slot, _ in launches) == list(range(1, MAX_AUDIT_LANES + 1))


def test_a_pending_window_is_not_relaunched_while_it_boots(tmp_path):
    launches = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launches.append(slot), (True, "started"))[1],
        path=tmp_path / "managed_browser_workers.json",
        cooldown_seconds=1.0,
    )
    dispatch = {"active_workers": 0, "workers": []}

    supervisor.ensure_capacity(dispatch, 1)
    assert launches == [1]

    # The cooldown is short, but the window is still booting.
    supervisor.ensure_capacity(dispatch, 1)
    supervisor.ensure_capacity(dispatch, 1)
    assert launches == [1]

    saved = json.loads((tmp_path / "managed_browser_workers.json").read_text(encoding="utf-8"))
    assert saved["slots"]["1"]["state"] == "LAUNCHING"


def test_reset_all_clears_a_jammed_board_in_one_action(tmp_path):
    """Resetting lane by lane is busywork, and Cancel refuses a BLOCKED run."""
    projects = [project("p1"), project("p2"), project("p3")]
    service, bridge, _audits = coordinator(tmp_path, projects=projects)

    queued = service.start("p1")
    pre_blocked = service.start("p2")
    post_blocked = service.start("p3")

    pre_job = next(j for j in bridge.jobs if j["dispatch_id"] == pre_blocked.dispatch_id)
    pre_job["state"] = "BLOCKED"
    pre_job["error"] = "canonical-start-rejected"

    post_job = next(j for j in bridge.jobs if j["dispatch_id"] == post_blocked.dispatch_id)
    post_job["state"] = "BLOCKED"
    post_job["error"] = "clean-state-lost"
    post_job["start_receipt"] = "startcore-live"
    post_job["campaign_run_id"] = "run-live"

    result = service.reset_all()

    assert result["failed"] == []
    assert result["total"] == 3
    assert sorted(result["cancelled"]) == ["Project p1", "Project p2"]
    assert result["unblocked"] == ["Project p3"]

    states = {job["dispatch_id"]: job["state"] for job in bridge.jobs}
    assert states[queued.dispatch_id] == "CANCELLED"
    assert states[pre_blocked.dispatch_id] == "CANCELLED"
    assert states[post_blocked.dispatch_id] == "FAILED"

    # Every project is free for a fresh START immediately afterwards.
    again = service.start("p3")
    assert again.ok and not again.duplicate


def test_reset_all_leaves_finished_runs_alone(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = service.start("p1")
    job = next(j for j in bridge.jobs if j["dispatch_id"] == started.dispatch_id)
    job["state"] = "CANCELLED"

    result = service.reset_all()
    assert result["total"] == 0
    assert job["state"] == "CANCELLED"


def test_a_slot_whose_window_never_registers_is_not_relaunched_forever(tmp_path):
    """A window that never registers is still a real window on screen.

    Relaunching its slot once the boot grace lapsed is how a 7th window appeared
    while six were already open.
    """
    from audapack.services.audit_run_service import (
        MAX_AUDIT_LANES,
        WORKER_LAUNCH_BOOT_GRACE_SECONDS,
        WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT,
    )

    launches = []
    path = tmp_path / "managed_browser_workers.json"
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launches.append(slot), (True, "started"))[1],
        path=path,
        cooldown_seconds=1.0,
    )
    dispatch = {"active_workers": 0, "workers": []}

    # Nothing ever registers. Age the slots past the boot grace between passes.
    for _ in range(6):
        supervisor.ensure_capacity(dispatch, MAX_AUDIT_LANES)
        doc = json.loads(path.read_text(encoding="utf-8"))
        for slot_state in doc["slots"].values():
            slot_state["launched_at"] = 0.0
            slot_state["cooldown_until"] = 0.0
        path.write_text(json.dumps(doc), encoding="utf-8")

    assert len(launches) <= MAX_AUDIT_LANES * WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT
    per_slot = {slot: launches.count(slot) for slot in set(launches)}
    assert max(per_slot.values()) <= WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT, per_slot

    # And once a slot registers, its budget is honestly restored.
    workers = [{"managed_slot": 1, "managed_generation": 1, "last_seen_at": 123.0}]
    supervisor.ensure_capacity({"active_workers": 1, "workers": workers}, 1)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["slots"]["1"]["state"] == "HEARTBEAT"
    assert doc["slots"]["1"]["launch_attempts"] == 0
    assert WORKER_LAUNCH_BOOT_GRACE_SECONDS > 0


def test_batch_provisions_every_lane_once_before_packing(tmp_path):
    """Six presses must ask for six windows, not "one more" six times.

    Demand was computed inside the per-project loop as queued+active+1, so a
    six-project batch trickled lanes open one at a time behind each pack, and
    the pool never reached six before the Bridge supervisor's own pacing took
    over.
    """
    projects = [project(f"p{index}") for index in range(1, 7)]
    demands: list[int] = []
    packs_at_first_provision: list[int] = []

    class RecordingSupervisor:
        def ensure_capacity(self, dispatch, demand):
            demands.append(int(demand))
            packs_at_first_provision.append(len(packing.calls))
            return {"desired": demand, "launched": [], "registered": 0, "generation": 1}

    service, bridge, _audits = coordinator(tmp_path, projects, supervisor=RecordingSupervisor())
    packing = service.packing
    results = service.start_batch([item.id for item in projects])

    assert all(result.ok for result in results)
    assert demands == [6], demands
    # Provisioning happens before the first archive is packed.
    assert packs_at_first_provision == [0]
    assert len(bridge.jobs) == 6


def test_start_retries_health_probe_before_starting_a_second_bridge(tmp_path):
    """One timed-out /health probe is not proof the Bridge is down."""
    service, bridge, _audits = coordinator(tmp_path)
    calls = {"count": 0}
    real_status = bridge.runtime_status

    def flaky_status():
        calls["count"] += 1
        if calls["count"] == 1:
            return {"healthy": False, "browser": {}}
        return real_status()

    bridge.runtime_status = flaky_status
    bridge.start = lambda: (_ for _ in ()).throw(AssertionError("must not start a second Bridge"))
    assert service.start("p1").ok


def test_a_legacy_banned_slot_is_amnestied_after_upgrade(tmp_path):
    """A slot banned by an older build must not stay banned forever."""
    import json as _json

    from audapack.services.audit_run_service import (
        MAX_AUDIT_LANES,
        WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT,
        ManagedWorkerSupervisor,
    )

    path = tmp_path / "managed_browser_workers.json"
    path.write_text(_json.dumps({
        "schema_version": 1,
        "generation": 1,
        "slots": {
            "1": {
                "state": "LAUNCHING",
                "launch_attempts": WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT,
                "launched_at": 0.0,
                "cooldown_until": 0.0,
            },
        },
    }), encoding="utf-8")

    launched: list[int] = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launched.append(slot), (True, "started"))[1],
        path=path,
    )
    supervisor.ensure_capacity({"active_workers": 0, "workers": []}, MAX_AUDIT_LANES)
    assert 1 in launched


def test_managed_lanes_are_not_starved_by_the_operators_own_tabs(tmp_path):
    """The operator's own ChatGPT tabs must not eat the managed window budget."""
    from audapack.services.audit_run_service import MAX_AUDIT_LANES, ManagedWorkerSupervisor

    launched: list[int] = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launched.append(slot), (True, "started"))[1],
        path=tmp_path / "managed_browser_workers.json",
    )
    # Four unmanaged workers are registered; none of them carries a slot.
    dispatch = {
        "active_workers": 4,
        "workers": [{"managed_slot": 0, "managed_generation": 0, "last_seen_at": 1.0}] * 4,
    }
    supervisor.ensure_capacity(dispatch, MAX_AUDIT_LANES)
    assert launched == [1, 2, 3, 4, 5, 6]


def test_a_lost_submit_response_adopts_the_dispatch_that_actually_exists(tmp_path):
    """A dropped response is not a failed audit.

    Observed live in a six-project batch: the submit POST returned "Remote end
    closed connection without response" while the Bridge had already enqueued
    the job and a worker was running it. Reporting FAILED there puts a lane on
    the board as failed while its audit is live, and hides the real dispatch
    from cancel and recovery.
    """
    service, bridge, _audits = coordinator(tmp_path)
    real_submit = bridge.submit_browser_audit

    def lossy_submit(project, archive_path, profile):
        real_submit(project, archive_path, profile)  # the Bridge does the work
        return {"ok": False, "error": "Remote end closed connection without response"}

    bridge.submit_browser_audit = lossy_submit
    result = service.start("p1")

    assert result.ok, result.message
    assert result.dispatch_id == bridge.jobs[-1]["dispatch_id"]
    assert len(bridge.jobs) == 1, "the lost response must not cause a second submission"
    intent = next(i for i in service.intents.list() if i["intent_id"] == result.intent_id)
    assert intent["status"] == "QUEUED"


def test_a_genuinely_rejected_submit_still_fails(tmp_path):
    """Adoption must not paper over a real rejection."""
    service, bridge, _audits = coordinator(tmp_path)
    bridge.submit_browser_audit = lambda project, archive_path, profile: {
        "ok": False, "error": "duplicate_dispatch",
    }
    result = service.start("p1")
    assert not result.ok
    assert "duplicate_dispatch" in result.message


def test_launch_slot_opens_the_named_slot_regardless_of_pool_size(tmp_path):
    """Reopening a closed window is a request for THAT window.

    ensure_capacity reads its argument as a COUNT of wanted lanes, so the
    operator relaunch path -- which passed the slot NUMBER -- opened nothing
    whenever that many lanes were already registered.
    """
    from audapack.services.audit_run_service import ManagedWorkerSupervisor

    launched: list[int] = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launched.append(slot), (True, "started"))[1],
        path=tmp_path / "managed_browser_workers.json",
    )
    dispatch = {
        "active_workers": 5,
        "workers": [
            {"managed_slot": slot, "managed_generation": 1, "last_seen_at": 1.0}
            for slot in (1, 3, 4, 5, 6)
        ],
    }
    outcome = supervisor.launch_slot(2, dispatch)
    assert outcome["launched"] is True
    assert launched == [2]


def test_launch_slot_leaves_a_slot_that_still_has_a_window(tmp_path):
    from audapack.services.audit_run_service import ManagedWorkerSupervisor

    launched: list[int] = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launched.append(slot), (True, "started"))[1],
        path=tmp_path / "managed_browser_workers.json",
    )
    dispatch = {"workers": [{"managed_slot": 2, "managed_generation": 1, "last_seen_at": 1.0}]}
    outcome = supervisor.launch_slot(2, dispatch)
    assert outcome["launched"] is False
    assert launched == []
    assert "already" in outcome["message"]


def test_a_batch_waits_for_the_lanes_it_just_asked_for(tmp_path):
    """Six queued projects must not become five concurrent audits.

    A launched Chromium window is invisible to the dispatcher until it has
    booted and registered -- tens of seconds -- and packing six archives is
    faster than that. The batch submitted against whatever happened to be
    clean, and the last job queued behind lanes that were still starting.
    """
    projects = [project(f"p{index}") for index in range(1, 7)]

    class Supervisor:
        def ensure_capacity(self, dispatch, demand):
            return {"desired": demand, "launched": [], "registered": 0, "generation": 1}

    service, bridge, _audits = coordinator(tmp_path, projects, supervisor=Supervisor())
    service.pool_settle_seconds = 5.0

    polls = {"count": 0}
    real_status = bridge.runtime_status

    def slow_pool():
        polls["count"] += 1
        status = real_status()
        # The sixth window registers only on the third look.
        status["browser"]["free_workers"] = 6 if polls["count"] >= 3 else 5
        return status

    bridge.runtime_status = slow_pool
    settled = service.provision_capacity(6)["settled"]

    assert settled == 6
    assert polls["count"] >= 3


def test_the_wait_gives_up_instead_of_blocking_the_batch(tmp_path):
    """A job with no window yet is a wait, not a loss."""
    service, bridge, _audits = coordinator(tmp_path)
    service.pool_settle_seconds = 0.2

    def never_ready():
        status = {"healthy": True, "browser": {"free_workers": 0, "queued_jobs": 0, "active_jobs": 0, "workers": []}}
        return status

    bridge.runtime_status = never_ready

    class Supervisor:
        def ensure_capacity(self, dispatch, demand):
            return {"desired": demand, "launched": [], "registered": 0, "generation": 1}

    service.workers = Supervisor()
    assert service.provision_capacity(6)["settled"] == 0
    assert service.start("p1").ok


def test_wave_progress_follows_the_same_lineage_across_a_re_derived_run_id(tmp_path):
    """A finished campaign reported 0/3 waves next to its own READY handoff."""
    service, _bridge, _audits = coordinator(tmp_path)
    audit = AuditSnapshot(
        project_id="p1", project_name="Project p1",
        campaign_run_id="acb-saved-under-this",
        completed_waves=3, total_waves=3,
    )
    job = {"project_id": "p1", "campaign_run_id": "acb-dispatch-saw-this"}

    assert service.audit_matches_dispatch(job, audit) is False
    job["meta_run_id_drift"] = "acb-saved-under-this"
    assert service.audit_matches_dispatch(job, audit) is True


def test_wave_progress_never_leaks_from_an_unrelated_campaign(tmp_path):
    service, _bridge, _audits = coordinator(tmp_path)
    audit = AuditSnapshot(
        project_id="p1", project_name="Project p1",
        campaign_run_id="acb-somebody-elses-run",
        completed_waves=3, total_waves=3,
    )
    job = {"project_id": "p1", "campaign_run_id": "acb-dispatch", "meta_run_id_drift": ""}
    assert service.audit_matches_dispatch(job, audit) is False


def _intent(status: str, **extra) -> dict:
    base = {
        "intent_id": f"int-{status.lower()}",
        "project_id": "p1",
        "project_name": "PROJ",
        "profile_id": "quick3",
        "status": status,
        "dispatch_id": "dsp-gone",
        "owner_pid": 0,
    }
    base.update(extra)
    return base


def test_a_finished_intent_is_not_counted_as_unfinished_work(tmp_path):
    """RESET ALL offered to clear 16 runs and Yes cleared nothing.

    An intent whose dispatch record is gone falls through to _intent_snapshot,
    and every settled status except FAILED collapsed into PREPARING. So 16
    finished runs read as live work; their dispatches are terminal, Cancel
    refuses and FORCE UNBLOCK refuses, and the same 16 came back on the next
    refresh. The dialog counts anything outside {READY, FAILED, CANCELLED}.
    """
    service, _bridge, _audits = coordinator(tmp_path)
    settled = {"COMPLETE": "READY", "READY": "READY", "CANCELLED": "CANCELLED", "FAILED": "FAILED"}
    for status, expected in settled.items():
        snapshot = service._intent_snapshot(_intent(status))
        assert snapshot.operator_state == expected, status
        assert snapshot.operator_state in {"READY", "FAILED", "CANCELLED"}, status


def test_live_intent_states_still_read_as_live(tmp_path):
    service, _bridge, _audits = coordinator(tmp_path)
    assert service._intent_snapshot(_intent("PREPARING")).operator_state == "PREPARING"
    assert service._intent_snapshot(_intent("QUEUED")).operator_state == "PREPARING"
    assert service._intent_snapshot(_intent("RECOVERY_NEEDED")).operator_state == "RECOVERY"
    stolen = _intent("PACKING", owner_pid=os.getpid() + 99999)
    assert service._intent_snapshot(stolen).operator_state == "INTERRUPTED"


def test_a_finished_intent_says_why_it_has_no_run_record(tmp_path):
    service, _bridge, _audits = coordinator(tmp_path)
    snapshot = service._intent_snapshot(_intent("COMPLETE"))
    assert "no dispatch record" in snapshot.summary
    assert snapshot.ready is False, "READY without proof must not inflate the ready count"


def test_a_run_whose_result_a_later_run_replaced_is_finished_not_saving(tmp_path):
    """13 of 32 COMPLETE dispatches read SAVING forever and RESET ALL could not
    clear them.

    Only a LATER run for the same project writes that canonical path, so once
    the bytes there hash to something else this run's recorded digest can never
    match and READY is unreachable. Calling it SAVING made it count as
    unfinished work; Yes then tried Cancel (refused: terminal) and FORCE
    UNBLOCK (refused: terminal) and the same rows came back on every press.
    """
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1")
    handoff = tmp_path / "PROJ__00_AUDIT_ALL_3.md"
    handoff.write_text("the run that overwrote it", encoding="utf-8")
    bridge.jobs[0].update({
        "state": "COMPLETE",
        "campaign_run_id": "run-1",
        "final_handoff_path": str(handoff),
        "final_handoff_sha256": hashlib.sha256(b"what this run actually wrote").hexdigest(),
    })

    run = service.refresh_runs()[0]
    assert run.ready is False
    assert run.operator_state == "SUPERSEDED"
    assert "later run" in run.summary
    assert run.operator_state in service.RESET_SETTLED_STATES, "must not count as unfinished"


def test_a_matching_digest_is_still_saving_until_the_proof_lands(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1")
    handoff = tmp_path / "PROJ__00_AUDIT_ALL_3.md"
    handoff.write_text("mine", encoding="utf-8")
    bridge.jobs[0].update({
        "state": "COMPLETE",
        "campaign_run_id": "run-1",
        "final_handoff_path": str(handoff),
        "final_handoff_sha256": hashlib.sha256(b"mine").hexdigest(),
    })
    assert service.refresh_runs()[0].operator_state == "SAVING"


def test_an_a10_lane_shows_ten_waves_not_three(tmp_path):
    """Six healthy A10 lanes were RESET ALL'd because the panel said 0/3.

    The dispatch carries its profile from the moment it is queued; the audit
    index only learns the wave count once a wave has been SAVED. Falling back to
    a hardcoded 3 meant a ten-wave run read "AUDIT 0/3" for the whole of wave 1
    -- 15-25 minutes on a real repo -- which is indistinguishable from a stalled
    three-wave run.
    """
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1", "super10")
    bridge.jobs[0].update({"state": "AUDITING", "profile": "super10"})

    run = service.refresh_runs()[0]
    assert run.total_waves == 10
    assert run.completed_waves == 0
    assert "0/10" in run.summary


def test_agent_state_is_read_once_per_project_per_refresh(tmp_path, monkeypatch):
    """PERF-005 (audit/4.md): a composite refresh fingerprints one project once.

    Two retained records of the same project used to each re-run the identical
    agent-inbox fingerprint; the request-local memo must collapse them to one
    read while still keeping distinct projects distinct.

    PERF-004 (audit/10.md): the default composite refresh is the PASSIVE
    dashboard layer (``read_inbox_passive``); the immediate-change reader is
    reserved for authoritative callers.
    """
    import audapack.agent_inbox as agent_inbox_module

    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    assert len(bridge.jobs) == 1
    # A second retained record for the same project (e.g. an older wave's job
    # still in history) plus one for another project.
    second_p1 = dict(bridge.jobs[0])
    second_p1["dispatch_id"] = "dsp-second"
    bridge.jobs.append(second_p1)
    p2 = dict(bridge.jobs[0])
    p2.update({"dispatch_id": "dsp-p2", "project_id": "p2", "project_name": "Project p2"})
    bridge.jobs.append(p2)
    service.projects.values["p2"] = project("p2")

    reads = []

    def counting(root, binding_rel=None, **_kwargs):
        reads.append(str(root))
        return SimpleNamespace(
            verdict="EMPTY", guidance="", residue=[], summary=lambda: "empty inbox",
        )

    monkeypatch.setattr(agent_inbox_module, "read_inbox_passive", counting)
    runs = service.refresh_runs()
    # Two records of p1, one of p2: p1 must be fingerprinted once, p2 once.
    p1_snaps = [r for r in runs if r.project_id == "p1"]
    p2_snaps = [r for r in runs if r.project_id == "p2"]
    assert len(p1_snaps) >= 2
    assert len(p2_snaps) >= 1
    assert len(reads) == 2, f"expected one read per project, got {len(reads)}"
    assert sorted(set(reads)) == ["C:/p1", "C:/p2"]
    assert all(r.agent_state == "EMPTY" for r in runs if r.project_id in ("p1", "p2"))
    assert agent_inbox_module.read_inbox_passive is counting, "monkeypatch must still be live"


def test_authoritative_refresh_uses_the_immediate_change_reader(tmp_path, monkeypatch):
    """PERF-004: a decision-critical refresh must select the authoritative reader."""
    import audapack.agent_inbox as agent_inbox_module

    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")

    passive_calls = []
    authoritative_calls = []

    def passive(root, binding_rel=None, **_kwargs):
        passive_calls.append(str(root))
        return SimpleNamespace(verdict="EMPTY", guidance="", residue=[], summary=lambda: "x")

    def authoritative(root, binding_rel=None, **_kwargs):
        authoritative_calls.append(str(root))
        return SimpleNamespace(verdict="EMPTY", guidance="", residue=[], summary=lambda: "x")

    monkeypatch.setattr(agent_inbox_module, "read_inbox_passive", passive)
    monkeypatch.setattr(agent_inbox_module, "read_inbox_cached", authoritative)
    service.refresh_runs()
    assert passive_calls and not authoritative_calls

    service.refresh_runs(authoritative_inbox=True)
    assert authoritative_calls, "authoritative_inbox must use read_inbox_cached"


def test_quick3_still_shows_three(tmp_path):
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1", "quick3")
    bridge.jobs[0].update({"state": "AUDITING", "profile": "quick3"})
    assert service.refresh_runs()[0].total_waves == 3


def test_a_saved_wave_count_still_wins(tmp_path):
    """Once the index knows, it is the authority -- this only fills the gap."""
    service, bridge, audits = coordinator(tmp_path)
    service.start("p1", "super10")
    bridge.jobs[0].update({"state": "AUDITING", "profile": "super10", "campaign_run_id": "run-1"})
    audits.snapshots["p1"] = AuditSnapshot(
        project_id="p1", project_name="Project p1", campaign_run_id="run-1",
        completed_waves=2, total_waves=10,
    )
    run = service.refresh_runs()[0]
    assert (run.completed_waves, run.total_waves) == (2, 10)


def test_autopack_repacks_instead_of_trusting_the_archive_mtime(tmp_path):
    """Freshness is an mtime compare, and mtime lies often enough to matter."""
    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    assert service.packing.repacks == ["p1"], "on by default"


def test_autopack_can_be_turned_off(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    service.projects.config = SimpleNamespace(audits=SimpleNamespace(autopack_before_audit=False))
    service.start("p1")
    assert service.packing.repacks == []
    assert service.packing.calls == ["p1"]


def test_a_complete_run_whose_proof_never_lands_stops_being_unclearable(tmp_path):
    """Three rows sat SAVING for over a day and nothing could move them.

    The digest matched, so the supersede check passed them, but the proof still
    failed for another reason -- the audit index belonging to a later run. A
    terminal dispatch refuses Cancel and refuses FORCE UNBLOCK, so RESET ALL
    offered to clear them and cleared nothing, every time.
    """
    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    handoff = tmp_path / "PROJ__00_AUDIT_ALL_3.md"
    handoff.write_text("mine", encoding="utf-8")
    bridge.jobs[0].update({
        "state": "COMPLETE",
        "campaign_run_id": "run-1",
        "final_handoff_path": str(handoff),
        "final_handoff_sha256": hashlib.sha256(b"mine").hexdigest(),
        "completed_at": time.time() - 86400,
    })

    run = service.refresh_runs()[0]
    assert run.operator_state == "SUPERSEDED"
    assert run.operator_state in service.RESET_SETTLED_STATES


def test_a_run_that_just_finished_is_still_saving(tmp_path):
    """Finalization writes in seconds; a fresh one is settling, not stuck."""
    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    handoff = tmp_path / "PROJ__00_AUDIT_ALL_3.md"
    handoff.write_text("mine", encoding="utf-8")
    bridge.jobs[0].update({
        "state": "COMPLETE",
        "campaign_run_id": "run-1",
        "final_handoff_path": str(handoff),
        "final_handoff_sha256": hashlib.sha256(b"mine").hexdigest(),
        "completed_at": time.time(),
    })
    assert service.refresh_runs()[0].operator_state == "SAVING"


def test_a_run_with_no_completion_time_is_left_alone(tmp_path):
    """Unknown is not old: never settle a run on a timestamp nobody wrote."""
    service, bridge, _audits = coordinator(tmp_path)
    service.start("p1")
    bridge.jobs[0].update({"state": "COMPLETE", "campaign_run_id": "run-1", "completed_at": 0.0})
    assert service.refresh_runs()[0].operator_state == "SAVING"


def test_a_quiet_slot_is_not_a_vacancy_to_launch_into(tmp_path):
    """Seven windows for six lanes, observed live on slot 3.

    The worker registry drops a worker 75s after its last heartbeat with no
    exemption for one mid-audit; the dispatcher remembers the slot has a window
    for 150s. In that gap the slot looked free, a second window was opened on
    it, and registration would not evict the first because it held a live run.
    """
    from audapack.services.audit_run_service import ManagedWorkerSupervisor

    launches = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launches.append(slot) is None, "ok"),
        tmp_path / "workers.json",
    )
    dispatch = {
        # Slot 3's worker fell out of the registry; only 1 and 2 are heard from.
        "workers": [
            {"managed_slot": 1, "managed_generation": 1, "last_seen_at": 10.0},
            {"managed_slot": 2, "managed_generation": 1, "last_seen_at": 10.0},
        ],
        "managed_slot_lanes": [1, 2, 3],
    }
    supervisor.ensure_capacity(dispatch, 3)
    assert 3 not in launches, f"opened a second window on a live slot: {launches}"


def test_an_older_bridge_without_the_slot_memory_still_works(tmp_path):
    """The key is absent when the running Bridge predates it: behave as before."""
    from audapack.services.audit_run_service import ManagedWorkerSupervisor

    launches = []
    supervisor = ManagedWorkerSupervisor(
        lambda slot, generation: (launches.append(slot) is None, "ok"),
        tmp_path / "workers.json",
    )
    supervisor.ensure_capacity(
        {"workers": [{"managed_slot": 1, "managed_generation": 1, "last_seen_at": 10.0}]}, 2
    )
    assert launches == [2]


def test_the_diagnostics_filename_is_the_filename_on_any_host():
    r"""CORE-003: the record promises a filename, so it must never leak a path.

    `Path(value).name` on POSIX reads a whole Windows path as ONE filename, so
    the Ubuntu half of the CI matrix emitted the operator's full
    C:\Users\<name>\... directory into a record documented as redacted. The
    redaction cannot depend on which host reads the value.
    """
    for raw in (r"C:\Users\Private\secret-result.md", "/home/private/secret-result.md"):
        snapshot = AuditRunSnapshot(
            project_id="p1", project_name="Project", operator_state="READY", summary="ready",
            handoff_path=raw, handoff_sha256="abc", ready=True,
        )
        payload = AuditRunCoordinator.diagnostics(snapshot)
        assert '"handoff_filename": "secret-result.md"' in payload, raw
        assert "Users" not in payload and "Private" not in payload and "home" not in payload


def test_a_repeat_refresh_never_invalidates_the_audit_cache(tmp_path):
    """PERF-002 (audit/1.md): settled history is not live state.

    refresh_runs called `refresh_project()` for every project with a retained
    job, which means force_rescan -- it INVALIDATES the AuditIndexer before it
    scans. So the periodic dashboard threw away a working directory-signature
    cache and re-read every wave file of every project on every tick. Measured
    over 100 settled projects: 44.18 ms and 300 file reads forced versus 2.87 ms
    and zero reads cached.
    """
    service, bridge, audits = coordinator(tmp_path)
    service.start_batch(["p1"])  # a retained job is what made the old path pay
    for _ in range(3):
        service.refresh_runs()
    assert audits.rescans == 0, "the periodic refresh invalidated the audit cache"



def test_one_composite_refresh_asks_for_browser_status_once(tmp_path):
    """The GUI asked for /v1/browser/status twice per repaint: once inside
    refresh_runs, once via runtime_status on the same tick."""
    service, bridge, _audits = coordinator(tmp_path)
    service.refresh_runs(status_response={"ok": True, "dispatch": bridge._status()})
    assert bridge.status_calls == 0, "the handed-in snapshot was ignored"


def test_a_handed_in_status_snapshot_still_fills_the_readout(tmp_path):
    """Passing the snapshot in is an optimization, not a weaker readout."""
    service, bridge, _audits = coordinator(tmp_path)
    service.start_batch(["p1"])
    bridge.workers = [{"worker_id": "w1", "browser_name": "Chrome", "state": "FREE"}]
    runs = service.refresh_runs(status_response={"ok": True, "dispatch": bridge._status()})
    assert runs and runs[0].worker_counts["active"] == 1, runs[0].worker_counts



def test_unchanged_intents_cost_one_journal_read_and_no_write(tmp_path):
    """PERF-002: `update()` per job is N lock acquisitions and N re-parses.

    Measured for 100 unchanged runs: 101 journal reads and ~3 MB of repeated
    JSON parsing, to discover nothing had moved.
    """
    reads = {"count": 0}

    service, bridge, _audits = coordinator(tmp_path)
    store = service.intents
    real_read = store._read_unlocked

    def counting_read():
        reads["count"] += 1
        return real_read()

    store._read_unlocked = counting_read
    intent, _created = store.begin("p1", "P1", "quick3")
    service.start_batch(["p1"])
    runs = [run for run in service.refresh_runs() if run.dispatch_id]
    assert runs, "no dispatch reached the intent reconciliation"

    before = (store.path.read_text(encoding="utf-8") if store.path.exists() else "")
    reads["count"] = 0
    service.refresh_runs()
    service.refresh_runs()

    assert reads["count"] <= 6, f"{reads['count']} journal reads for two unchanged passes"
    assert (store.path.read_text(encoding="utf-8") if store.path.exists() else "") == before, \
        "a no-op reconciliation wrote the journal anyway"


def test_a_changed_intent_status_is_still_persisted(tmp_path):
    """The batch merge must not become a silent no-op."""
    service, bridge, _audits = coordinator(tmp_path)
    intent, _created = service.intents.begin("p1", "P1", "quick3")
    service.start_batch(["p1"])
    service.refresh_runs()

    stored = next(item for item in service.intents.list() if item["intent_id"] == intent["intent_id"])
    assert stored["status"] != "PREPARING", "the intent never advanced"
    assert stored["dispatch_id"], "the dispatch was never bound to its intent"


def _diagnostic(**overrides):
    fields = {
        "project_id": "p1", "project_name": "Project", "operator_state": "BLOCKED_POST_START",
        "summary": "blocked", "handoff_sha256": "abc",
    }
    fields.update(overrides)
    return json.loads(AuditRunCoordinator.diagnostics(AuditRunSnapshot(**fields)))


def test_diagnostics_strip_credentials_from_worker_error_text():
    r"""CORE-006 (audit/3.md): the record promises "never tokens or content".

    `error` and `recovery` were copied verbatim, and browser_dispatch accepts
    worker-supplied payload["error"] straight into durable job state -- so that
    text is arbitrary. Reproduced: `Bearer TOPSECRET token=abc123
    /home/private/raw.txt` and a Windows path all survived into the JSON the UI
    offers the operator to copy as a safe diagnostic.
    """
    doc = _diagnostic(
        error="Bearer TOPSECRET token=abc123 while reading /home/private/raw.txt",
        recovery=r"authorization: Basic Zm9v at C:\Users\Private\notes.md",
    )
    blob = json.dumps(doc)
    for marker in ("TOPSECRET", "abc123", "Zm9v", "/home/private", "Users", "Private"):
        assert marker not in blob, f"{marker} survived into a redacted diagnostic"
    # The filename survives: it is the part that helps a support reader.
    assert "raw.txt" in doc["error"]
    assert "notes.md" in doc["recovery"]


def test_diagnostics_reduce_paths_on_either_platform_grammar():
    doc = _diagnostic(
        handoff_path=r"C:\Users\Private\secret-result.md",
        error=r"copy failed: \\FILESRV\share\audit\out.md -> /var/tmp/staging/out.md",
    )
    assert doc["handoff_filename"] == "secret-result.md"
    assert "FILESRV" not in json.dumps(doc)
    assert "/var/tmp" not in json.dumps(doc)
    assert "out.md" in doc["error"]


def test_diagnostics_keep_the_identifiers_support_actually_needs():
    doc = _diagnostic(
        dispatch_id="dsp-0123456789abcdef",
        campaign_run_id="acb-run-0001",
        intent_id="int-abc",
        handoff_sha256="deadbeef",
        worker_counts={"active": 6, "clean": 2},
        operator_state="READY",
        ready=True,
    )
    assert doc["dispatch_id"] == "dsp-0123456789abcdef"
    assert doc["campaign_run_id"] == "acb-run-0001"
    assert doc["intent_id"] == "int-abc"
    assert doc["handoff_sha256"] == "deadbeef"
    assert doc["worker_counts"] == {"active": 6, "clean": 2}
    assert doc["operator_state"] == "READY"
    assert doc["ready"] is True


def test_redaction_happens_before_truncation():
    """A limit must never cut a marker in half and leave the secret behind it."""
    from audapack.services.audit_run_service import _redact_diagnostic_text

    padding = "x" * 480
    text = f"{padding} token=SUPERSECRETVALUE trailing"
    out = _redact_diagnostic_text(text, 500)
    assert "SUPERSECRETVALUE" not in out
    assert "[redacted]" in out


def _supervisor(tmp_path, launches, ok=True):
    return ManagedWorkerSupervisor(
        lambda slot, generation: (launches.append((slot, generation)) or (ok, "started")),
        tmp_path / "workers.json",
    )


def test_a_launch_reservation_is_durable_before_the_spawn(tmp_path):
    """W2-005 (audit/3.md): the irreversible effect came before the record.

    `ensure_capacity()` spawned the browser and only then wrote the LAUNCHING
    row, so a failed ledger write lost all knowledge of an already-open window
    and the retry opened the same slot again. Measured: launches [(1,1)] with no
    journal on disk, then [(1,1),(1,1)] once persistence was restored.
    """
    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    seen: list[str] = []

    def record_then_fail_after_spawn(path, value):
        state = str(((value.get("slots") or {}).get("1") or {}).get("state") or "")
        seen.append(state)
        if state != "RESERVED":
            raise OSError("injected ledger write failure after the spawn")
        return _real_atomic_write(path, value)

    from audapack.services import audit_run_service as ars

    _real_atomic_write = ars._atomic_write_json
    ars._atomic_write_json = record_then_fail_after_spawn
    try:
        with pytest.raises(OSError):
            supervisor.ensure_capacity({"workers": []}, 1)
    finally:
        ars._atomic_write_json = _real_atomic_write

    assert launches == [(1, 1)], launches
    assert seen[0] == "RESERVED", f"the spawn happened before any record: {seen}"
    assert supervisor.path.exists(), "the reservation was not durable"

    # The retry sees the reservation and does NOT open a second window.
    supervisor.ensure_capacity({"workers": []}, 1)
    assert launches == [(1, 1)], f"the same slot was launched twice: {launches}"


def test_a_failure_before_the_spawn_opens_no_window(tmp_path):
    from audapack.services import audit_run_service as ars

    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    real = ars._atomic_write_json
    ars._atomic_write_json = lambda path, value: (_ for _ in ()).throw(OSError("injected"))
    try:
        with pytest.raises(OSError):
            supervisor.ensure_capacity({"workers": []}, 1)
    finally:
        ars._atomic_write_json = real
    assert launches == [], "a browser was opened with no durable reservation"


def test_a_corrupt_ledger_is_quarantined_not_treated_as_an_empty_pool(tmp_path):
    """Unreadable state is an UNKNOWN pool, not an empty one.

    Every slot read as vacant, so the next pass launched windows onto slots that
    already had one, and generation 1 made the era collide with the install's
    first pool.
    """
    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    supervisor.path.parent.mkdir(parents=True, exist_ok=True)
    supervisor.path.write_text("{ this is not json", encoding="utf-8")

    workers = [
        {"managed_slot": 1, "managed_generation": 4, "last_seen_at": time.time()},
        {"managed_slot": 2, "managed_generation": 4, "last_seen_at": time.time()},
    ]
    supervisor.ensure_capacity({"workers": workers}, 2)

    assert launches == [], f"duplicate windows for slots that already had one: {launches}"
    doc = json.loads(supervisor.path.read_text(encoding="utf-8"))
    assert doc["generation"] == 4, "the generation went backwards through corruption"
    assert set(doc["slots"]) == {"1", "2"}
    assert doc.get("recovered_from"), "the corrupt bytes were destroyed instead of quarantined"
    quarantined = list(supervisor.path.parent.glob("workers.json.corrupt.*"))
    assert quarantined, "the corrupt file was not kept for inspection"


def test_a_corrupt_ledger_with_no_live_worker_still_provisions(tmp_path):
    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    supervisor.path.parent.mkdir(parents=True, exist_ok=True)
    supervisor.path.write_text('{"schema_version": 99}', encoding="utf-8")

    supervisor.ensure_capacity({"workers": []}, 1)
    assert launches == [(1, 1)], launches


def _write_ledger(path: Path, slots: dict, generation: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "generation": generation,
        "slots": slots,
        "updated_at": time.time(),
    }), encoding="utf-8")


def test_direct_launch_slot_is_idempotent_before_registration(tmp_path):
    """W2-003 (SRC-041:R007): the ledger itself refuses the duplicate.

    launch_slot x2 used to spawn twice, [(2,1),(2,1)], because it only trusted
    the live dispatcher, which is blind until the first browser registers.
    """
    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    dispatch = {"workers": []}

    first = supervisor.launch_slot(2, dispatch)
    second = supervisor.launch_slot(2, dispatch)

    assert first["launched"] is True
    assert second["launched"] is False
    assert "already pending" in second["message"]
    assert launches == [(2, 1)], launches


def test_recent_reserved_or_launching_slot_is_not_relaunched(tmp_path):
    for state in ("RESERVED", "LAUNCHING"):
        launches: list[tuple[int, int]] = []
        supervisor = _supervisor(tmp_path, launches)
        _write_ledger(supervisor.path, {"2": {
            "state": state,
            "launch_attempts": 1,
            "launched_at": time.time(),
            "cooldown_until": time.time() + 60,
        }})
        outcome = supervisor.launch_slot(2, {"workers": []})
        assert outcome["launched"] is False, state
        assert launches == [], state


def test_expired_reservation_is_relaunched(tmp_path):
    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    _write_ledger(supervisor.path, {"2": {
        "state": "RESERVED",
        "launch_attempts": 1,
        "launched_at": 0.0,
        "cooldown_until": 0.0,
    }})
    outcome = supervisor.launch_slot(2, {"workers": []})
    assert outcome["launched"] is True
    assert launches == [(2, 1)]


def test_launch_failed_slot_is_relaunched(tmp_path):
    launches: list[tuple[int, int]] = []
    supervisor = _supervisor(tmp_path, launches)
    _write_ledger(supervisor.path, {"2": {
        "state": "LAUNCH_FAILED",
        "launch_attempts": 1,
        "launched_at": 0.0,
        "cooldown_until": 0.0,
    }})
    outcome = supervisor.launch_slot(2, {"workers": []})
    assert outcome["launched"] is True
    assert launches == [(2, 1)]


def test_reset_stale_slot_preserves_a_recent_reservation(tmp_path):
    supervisor = _supervisor(tmp_path, [])
    _write_ledger(supervisor.path, {"2": {
        "state": "RESERVED",
        "launch_attempts": 1,
        "launched_at": time.time(),
        "cooldown_until": time.time() + 60,
    }})
    assert supervisor.reset_stale_slot(2) is False
    doc = json.loads(supervisor.path.read_text(encoding="utf-8"))
    assert doc["slots"]["2"]["state"] == "RESERVED"


def test_reset_stale_slot_clears_a_failed_slot(tmp_path):
    supervisor = _supervisor(tmp_path, [])
    _write_ledger(supervisor.path, {"2": {
        "state": "LAUNCH_FAILED",
        "launch_attempts": 1,
        "launched_at": 0.0,
        "cooldown_until": 0.0,
    }})
    assert supervisor.reset_stale_slot(2) is True
    doc = json.loads(supervisor.path.read_text(encoding="utf-8"))
    assert "2" not in doc["slots"]


def test_concurrent_launch_slot_spawns_once(tmp_path):
    import threading

    launches: list[tuple[int, int]] = []
    barrier = threading.Barrier(2)

    def launch(slot, generation):
        launches.append((slot, generation))
        return True, "started"

    supervisor = ManagedWorkerSupervisor(launch, tmp_path / "workers.json")

    def _go():
        barrier.wait(timeout=10)
        supervisor.launch_slot(2, {"workers": []})

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(launches) == 1, launches


def test_perf_001_non_terminal_runs_cannot_be_hidden_behind_terminal_history(tmp_path):
    """PERF-001: active (non-terminal) dispatches must remain in the returned
    snapshot view even when 200+ terminal history entries would push them out
    of a naive truncation."""
    service, bridge, audits = coordinator(tmp_path)

    # Seed a large shape: 200 QUEUED (active) + 100 COMPLETE terminal jobs, all
    # for project p1, with increasing updated_at so terminal entries are newer.
    for i in range(200):
        bridge.jobs.append({
            "dispatch_id": f"dsp-active-{i:04d}",
            "project_id": "p1",
            "project_name": "Project p1",
            "state": "QUEUED",
            "assigned_worker_id": "",
            "campaign_run_id": "",
            "profile": "quick3",
            "created_at": float(i),
            "updated_at": float(i),
            "completed_at": 0.0,
            "error": "",
            "final_handoff_path": "",
            "final_handoff_sha256": "",
            "queue_position": i,
        })
    for i in range(100):
        bridge.jobs.append({
            "dispatch_id": f"dsp-term-{i:04d}",
            "project_id": "p1",
            "project_name": "Project p1",
            "state": "COMPLETE",
            "assigned_worker_id": "",
            "campaign_run_id": f"run-{i}",
            "profile": "quick3",
            "created_at": float(200 + i),
            "updated_at": float(200 + i),
            "completed_at": float(200 + i),
            "error": "",
            "final_handoff_path": "",
            "final_handoff_sha256": "",
        })

    runs = service.refresh_runs(["p1"])

    # Every non-terminal dispatch survives truncation (PERF-001 guardrail).
    active = [r for r in runs if r.dispatch_state != "COMPLETE" and r.operator_state != "COMPLETE"]
    assert len(active) == 200, f"expected 200 non-terminal runs, got {len(active)}"

    # Terminal history is bounded independently.
    terminal = [r for r in runs if r.operator_state == "COMPLETE"]
    assert len(terminal) <= 100, f"terminal history not bounded: {len(terminal)}"

    # The 4-second poll cadence must stay ACTIVE because non-terminal work exists.
    # (This is verified indirectly: all 200 active runs are present.)


# ------------------------------------------------------------------- STOP
#
# A selected LIVE audit had no operator stop at all: STARTING/AUDITING/SAVING
# returned ("DETAILS",), so every stop control was greyed out exactly where the
# operator most wants one. STOP is that control, and it routes through the
# honest retirement path rather than through CANCEL (which asserts no Core was
# ever sent and must stay pre-START only).


def live_run(service, bridge, state="AUDITING"):
    """A dispatch that has crossed START and is still working."""
    started = service.start("p1")
    bridge.jobs[0].update({
        "state": state,
        "campaign_run_id": "run-committed",
        "start_receipt": "receipt-1",
    })
    return started


def test_a_live_audit_offers_stop_instead_of_a_dead_panel(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    live_run(service, bridge)
    run = service.refresh_runs()[0]
    assert run.operator_state == "AUDITING"
    assert "STOP" in run.actions, "the operator must be able to stop a running audit"
    assert "CANCEL" not in run.actions, "CANCELLED would falsely assert no Core was sent"


def test_every_live_post_start_state_offers_stop(tmp_path):
    for state in ("STARTING", "AUDITING", "SAVING"):
        assert "STOP" in _actions_for(state), state
        assert "CANCEL" not in _actions_for(state), state


def test_pre_start_work_still_reads_cancel_and_never_stop(tmp_path):
    """STOP must not leak into the states where CANCEL is still honest."""
    for state in ("WAITING", "RETRYING", "PREPARING", "ATTACHING", "BLOCKED_PRE_START"):
        assert "CANCEL" in _actions_for(state), state
        assert "STOP" not in _actions_for(state), state


def test_a_blocked_post_start_run_keeps_force_unblock_not_stop(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    blocked_post_start_run(service, bridge)
    run = service.refresh_runs()[0]
    assert run.operator_state == "BLOCKED_POST_START"
    assert "ABANDON" in run.actions
    assert "STOP" not in run.actions, "the BLOCKED wording must not change"


def test_a_terminal_run_has_no_stop_action_at_all():
    for state in ("FAILED", "CANCELLED", "SUPERSEDED", "INTERRUPTED", "READY"):
        actions = _actions_for(state)
        assert "STOP" not in actions, state
        assert "CANCEL" not in actions, state
        assert "ABANDON" not in actions, state


def test_stop_retires_the_run_as_failed_operator_abandoned(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = live_run(service, bridge)
    stopped = service.abandon(
        started.dispatch_id, "operator stopped active audit from Audit Runs panel"
    )
    assert stopped.ok
    assert stopped.state == "FAILED", "a post-START stop is terminal FAILED, never CANCELLED"
    assert bridge.jobs[0]["state"] == "FAILED"
    assert bridge.jobs[0]["last_error_code"] == "operator_abandoned"


def test_stop_frees_the_lane_without_re_leasing_the_dispatch(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = live_run(service, bridge)
    service.abandon(started.dispatch_id, "operator stopped active audit from Audit Runs panel")

    resumed = service.start("p1")
    assert resumed.ok and not resumed.duplicate
    assert bridge.submits == 2, "the freed project accepts exactly one FRESH dispatch"
    assert resumed.dispatch_id != started.dispatch_id, "the stopped dispatch is never re-leased"
    assert len(bridge.jobs) == 2


def test_a_stop_that_loses_the_race_never_rewrites_a_finished_run(tmp_path):
    """The run reached COMPLETE between the click and the bridge call.

    abandon_job() is idempotent and returns the terminal job unchanged, so the
    coordinator must report THAT state rather than forcing FAILED over a
    finished audit whose waves were already saved.
    """
    service, bridge, _audits = coordinator(tmp_path)
    started = live_run(service, bridge)
    bridge.abandon_terminal_state = "COMPLETE"

    stopped = service.abandon(started.dispatch_id, "operator stopped active audit from Audit Runs panel")
    assert stopped.ok
    assert stopped.state == "COMPLETE"

    intent = service.intents.find_for_dispatch(started.dispatch_id)
    assert intent is not None and intent["status"] != "FAILED", (
        "a completed audit must not be relabelled FAILED by a late STOP"
    )


def test_a_stop_that_loses_the_race_to_failed_still_reports_failed(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = live_run(service, bridge)
    bridge.abandon_terminal_state = "FAILED"
    stopped = service.abandon(started.dispatch_id, "operator stopped active audit")
    assert stopped.ok and stopped.state == "FAILED"


def test_a_refused_stop_mutates_nothing(tmp_path):
    service, bridge, _audits = coordinator(tmp_path)
    started = live_run(service, bridge)
    bridge.abandon_error = "bridge offline"
    refused = service.abandon(started.dispatch_id, "operator stopped active audit")
    assert not refused.ok
    assert bridge.jobs[0]["state"] == "AUDITING", "a refused stop must not mutate the run"


class TestCloseTokenStopsAnIrreversibleStart:
    """W2-002 (audit/12.md): `closeEvent()` could drop a debounced START AUDIT,
    but once `_pump_audit_start_queue()` had moved the batch into
    `_audit_start_inflight` and submitted `_prepare`, `start_batch()` still
    provisioned browser windows and dispatched an audit for a window the operator
    had already closed. The token is checked at each irreversible boundary, and a
    race PAST a boundary is reported for what it is, never as "cancelled".
    """

    def test_a_closed_window_never_provisions_capacity_or_dispatches(self, tmp_path):
        provisioned = []

        class Supervisor:
            def ensure_capacity(self, _status, demand):
                provisioned.append(demand)
                return {"desired": 0, "launched": []}

        closed = []
        # Control: the token says the window is still open, so the batch runs.
        open_service, open_bridge, _ = coordinator(
            tmp_path, [project("p1"), project("p2")], supervisor=Supervisor())
        assert all(r.ok for r in open_service.start_batch(
            ["p1", "p2"], should_abort=lambda: bool(closed)))
        assert provisioned and open_bridge.submits == 2

        closed.append(True)
        service, bridge, _audits = coordinator(
            tmp_path / "closed", [project("p1"), project("p2")], supervisor=Supervisor())
        results = service.start_batch(["p1", "p2"], should_abort=lambda: bool(closed))
        assert results, "every project must be answered, not silently dropped"
        assert all(not result.ok for result in results)
        assert all(result.state == "CANCELLED" for result in results), [
            (r.project_id, r.state, r.message) for r in results]
        assert len(provisioned) == 1, "capacity was provisioned again after close"
        assert bridge.submits == 0, bridge.submits

    def test_closing_between_provisioning_and_submit_dispatches_nothing(self, tmp_path):
        class Supervisor:
            def __init__(self, token):
                self.token = token

            def ensure_capacity(self, _status, demand):
                self.token.append(True)  # the operator closes HERE
                return {"desired": 0, "launched": []}

        token = []
        service, bridge, _audits = coordinator(tmp_path, supervisor=Supervisor(token))
        results = service.start_batch(["p1"], should_abort=lambda: bool(token))
        assert len(results) == 1
        result = results[0]
        assert not result.ok
        assert result.state == "CANCELLED", result.state
        assert bridge.submits == 0, "a dispatch crossed the close boundary"

    def test_a_dispatch_that_raced_past_the_boundary_is_adopted_not_cancelled(self, tmp_path):
        """Past the durable submission there is nothing to revoke: the job runs.
        Reporting it as CANCELLED would hide a live dispatch from the board."""
        token = []
        service, bridge, _audits = coordinator(tmp_path)
        original = service.bridge.submit_browser_audit

        def submit_then_close(*args, **kwargs):
            token.append(True)  # closing wins the race AFTER the submission
            return original(*args, **kwargs)

        service.bridge.submit_browser_audit = submit_then_close
        result = service.start("p1", should_abort=lambda: bool(token))
        assert result.ok, result.message
        assert result.dispatch_id, "a submitted dispatch must not be reported cancelled"
        assert result.state != "CANCELLED", result.state
        assert len(bridge.jobs) == 1


class TestTerminalSourceIntentsCompactToTombstones:
    """W2-003 (audit/12.md): `_trim_intents()` bounded ordinary history to
    `history_bound` but retained EVERY source-keyed record forever, even at
    terminal status -- 40 terminally FAILED prepared sources with a bound of 6.
    A prepared source key is a durable idempotency record, so it must never be
    dropped; what can go is the diagnostic payload around its identity.
    """

    def _source_intents(self, tmp_path, count):
        store = AuditStartIntentStore(tmp_path / "intents.json", history_bound=6)
        # Each source gets its OWN project: begin() returns the existing
        # ACTIVE intent for a project rather than creating a second one,
        # so a shared "p1" would produce a single record, not 40.
        for index in range(count):
            store.begin(f"p{index}", f"P{index}", "quick3",
                        source_execution_id=f"exec-{index}")
        return store

    def test_terminal_source_records_shrink_to_identity_tombstones(self, tmp_path):
        store = self._source_intents(tmp_path, 40)
        for index in range(40):
            intent = store.find_for_source(f"exec-{index}")
            store.update(intent["intent_id"], status="FAILED",
                         error="a diagnostic string " * 20, completed_at=1.0 + index)
        assert len(store.list()) == 40, "a source key was dropped"

        compacted = store._trim_intents(store.list())
        assert len(compacted) == 40, "compaction lost an idempotency record"
        assert sum(1 for item in compacted if item.get("compacted")) >= 34
        for item in compacted:
            if item.get("compacted"):
                assert item["error"] == ""
                assert item["source_execution_id"]

    def test_a_replay_of_a_compacted_source_is_still_rejected_truthfully(self, tmp_path):
        store = self._source_intents(tmp_path, 12)
        for index in range(12):
            intent = store.find_for_source(f"exec-{index}")
            store.update(intent["intent_id"], status="FAILED", error="x", completed_at=1.0 + index)
        store._trim_intents(store.list())

        again, created = store.begin("p1", "P1", "quick3", source_execution_id="exec-0")
        assert created is False, "a compacted source key was re-issued as new work"
        assert again["status"] == "FAILED"
        assert store.find_for_source("exec-0")["intent_id"] == again["intent_id"]

    def test_an_active_or_recent_source_record_keeps_its_full_payload(self, tmp_path):
        store = self._source_intents(tmp_path, 19)
        store.begin("p-live", "P-live", "quick3", source_execution_id="exec-live")
        active = store.find_for_source("exec-live")
        store.update(active["intent_id"], status="QUEUED", error="still going",
                     dispatch_id="dsp-1", completed_at=0.0)
        for index in range(19):
            intent = store.find_for_source(f"exec-{index}")
            store.update(intent["intent_id"], status="FAILED", error="x",
                         completed_at=1.0 + index)
        compacted = store._trim_intents(store.list())
        live = next(item for item in compacted if item["intent_id"] == active["intent_id"])
        assert live.get("compacted") is None
        assert live["error"] == "still going"
