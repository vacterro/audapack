from __future__ import annotations

import hashlib
import json
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


def test_batch_caps_at_six_and_isolates_pack_failure(tmp_path):
    projects = [project(f"p{index}") for index in range(1, 8)]
    service, bridge, _audits = coordinator(tmp_path, projects, failures={"p3"})
    results = service.start_batch([item.id for item in projects])
    assert len(results) == 6
    assert [result.project_id for result in results if not result.ok] == ["p3"]
    assert len(bridge.jobs) == 5


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
        "completed_at": 5.0,
        "updated_at": 5.0,
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


def test_seventh_project_is_capped_not_silently_dropped(tmp_path):
    projects = [project(f"p{index}") for index in range(1, 9)]
    service, bridge, _audits = coordinator(tmp_path, projects)
    results = service.start_batch([item.id for item in projects])
    assert len(results) == 6
    assert len(bridge.jobs) == 6
    assert all(result.ok for result in results)


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
    bridge.jobs[0].update({"state": "AUDITING"})
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
