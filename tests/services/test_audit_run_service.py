from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from audapack.models import AuditSnapshot, Project
from audapack.services.audit_run_service import (
    AuditRunCoordinator,
    AuditRunSnapshot,
    AuditStartIntentStore,
    ManagedWorkerSupervisor,
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

    def refresh_project(self, project_id):
        return self.snapshots.get(project_id)


class FakeBridge:
    def __init__(self):
        self.healthy = True
        self.jobs = []
        self.workers = []
        self.cancel_error = ""
        self.abandon_error = ""
        self.submits = 0

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
        if job["state"] != "BLOCKED":
            return {"ok": False, "error": {"code": "invalid_transition", "message": "only a BLOCKED dispatch can be abandoned"}}
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
