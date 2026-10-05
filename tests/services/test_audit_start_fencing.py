"""W2-001: the start-intent journal is an ownership primitive, not just an
idempotency record. Exactly one pre-dispatch writer may pack and submit; a
loser must not pack, submit, mutate state, or attach a stale FAILED after the
winner has progressed.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from audapack.models import Project
from audapack.services.audit_run_service import (
    AuditRunCoordinator,
    AuditStartIntentStore,
    StaleClaimError,
)


class FakeProjects:
    def __init__(self, projects):
        self.values = {project.id: project for project in projects}

    def get_project(self, project_id):
        return self.values.get(project_id)


class FakeAudits:
    def __init__(self):
        self.snapshots = {}

    def get_snapshot(self, project_id, force_rescan=False):
        return self.snapshots.get(project_id)

    def refresh_project(self, project_id):
        return self.get_snapshot(project_id, force_rescan=True)


class FakeBridge:
    def __init__(self):
        self.healthy = True
        self.jobs = []
        self.submits = 0

    def runtime_status(self):
        return {"healthy": self.healthy, "browser": {"active_workers": 0, "active_jobs": 0, "queued_jobs": 0, "workers": []}}

    def active_browser_job(self, project_id):
        return next(
            (job for job in reversed(self.jobs)
             if job["project_id"] == project_id and job["state"] not in {"COMPLETE", "FAILED", "CANCELLED"}),
            None,
        )

    def browser_jobs(self, project_id):
        return {"jobs": [dict(job) for job in self.jobs if job["project_id"] == project_id]}

    def submit_browser_audit(self, project, archive_path, profile):
        self.submits += 1
        job = {
            "dispatch_id": f"dsp-{self.submits:016d}",
            "project_id": project.id,
            "project_name": project.display_name,
            "state": "QUEUED",
        }
        self.jobs.append(job)
        return {"ok": True, "dispatch": dict(job)}


class GatedPacking:
    """Blocks the winner at the pre-submit pack boundary so the race is real."""

    def __init__(self, root: Path):
        self.root = root
        self.pack_calls = 0
        self.submit_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def pack_project(self, project_id, **_kwargs):
        self.pack_calls += 1
        self.entered.set()
        self.release.wait(5)
        return self.ensure_fresh_archive(project_id)

    def ensure_fresh_archive(self, project_id):
        path = self.root / f"{project_id}.zip"
        path.write_bytes(project_id.encode())
        return SimpleNamespace(success=True, output_path=path, error_message="")


def _project(project_id="p1"):
    return Project(id=project_id, display_name=f"Project {project_id}", source_path=f"C:/{project_id}")


def _coordinator(store, packing, bridge):
    service = AuditRunCoordinator(
        FakeProjects([_project()]),
        packing,
        bridge,
        FakeAudits(),
        intent_store=store,
    )
    service.pool_settle_seconds = 0.0
    return service


def test_claim_or_takeover_is_exclusive_and_fenced(tmp_path):
    path = tmp_path / "intents.json"
    store = AuditStartIntentStore(path)
    intent, created = store.begin("p1", "P1", "quick3")
    assert created
    assert intent["claim_token"] == ""

    granted, first = store.claim_or_takeover(intent["intent_id"])
    assert granted
    denied, _ = store.claim_or_takeover(intent["intent_id"])
    assert not denied, "a second caller must not hold the same claim"

    # A dead owner is taken over under a new fencing generation.
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["intents"][0]["owner_pid"] = 999999
    doc["intents"][0]["claim_holder_pid"] = 999999
    path.write_text(json.dumps(doc), encoding="utf-8")
    taken, second = store.claim_or_takeover(intent["intent_id"])
    assert taken
    assert second["fence"] > first["fence"]


def test_two_coordinators_release_concurrently_one_pack_one_submit(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json")
    bridge = FakeBridge()
    packing = GatedPacking(tmp_path)

    winner = _coordinator(store, packing, bridge)
    loser = _coordinator(store, packing, bridge)

    results = {}

    def run_winner():
        results["winner"] = winner.start("p1")

    thread = threading.Thread(target=run_winner)
    thread.start()
    assert packing.entered.wait(5), "the winner never reached the pack boundary"

    # The loser arrives while the winner holds the pre-dispatch claim.
    results["loser"] = loser.start("p1")

    packing.release.set()
    thread.join(10)

    assert packing.pack_calls == 1, "the loser packed a second archive"
    assert bridge.submits == 1, "the loser submitted a second dispatch"
    assert len(store.list()) == 1, "more than one durable intent was created"
    durable = store.list()[0]
    assert durable["dispatch_id"] == results["winner"].dispatch_id
    assert results["loser"].duplicate and not results["loser"].dispatch_id


def test_late_loser_cannot_overwrite_a_queued_intent(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json")
    bridge = FakeBridge()
    packing = GatedPacking(tmp_path)
    packing.release.set()

    winner = _coordinator(store, packing, bridge)
    started = winner.start("p1")
    assert started.ok and started.state == "QUEUED"

    durable = store.list()[0]
    assert durable["status"] == "QUEUED"
    old_fence = durable["fence"] - 1

    # A writer holding an earlier fencing generation is rejected.
    with pytest.raises(StaleClaimError):
        store.update(durable["intent_id"], status="FAILED", error="late loser", expect_fence=old_fence)

    # And a second start never even reaches the writer path.
    loser = _coordinator(store, packing, bridge)
    second = loser.start("p1")
    assert second.duplicate
    still = store.list()[0]
    assert still["status"] == "QUEUED" and still["error"] == ""
    assert bridge.submits == 1


def test_prepared_source_reuses_intent_after_restart_without_second_dispatch(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json")
    bridge = FakeBridge()
    packing = GatedPacking(tmp_path)
    packing.release.set()
    first = _coordinator(store, packing, bridge).start(
        "p1", source_execution_id="prepared-execution-1")
    second = _coordinator(AuditStartIntentStore(store.path), packing, bridge).start(
        "p1", source_execution_id="prepared-execution-1")
    assert first.ok and second.ok and second.duplicate
    assert first.intent_id == second.intent_id
    assert first.dispatch_id == second.dispatch_id
    assert packing.pack_calls == bridge.submits == 1
    assert store.find_for_source("prepared-execution-1")["dispatch_id"] == first.dispatch_id


def test_prepared_source_refuses_unrelated_active_audit(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json")
    bridge = FakeBridge()
    packing = GatedPacking(tmp_path)
    packing.release.set()
    manual = _coordinator(store, packing, bridge).start("p1")
    prepared = _coordinator(store, packing, bridge).start(
        "p1", source_execution_id="prepared-execution-1")
    assert manual.ok and not prepared.ok and prepared.state == "BLOCKED"
    assert not store.find_for_source("prepared-execution-1")
    assert packing.pack_calls == bridge.submits == 1


def test_prepared_source_does_not_resubmit_after_dispatch_is_terminal(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json")
    bridge = FakeBridge()
    packing = GatedPacking(tmp_path)
    packing.release.set()
    first = _coordinator(store, packing, bridge).start(
        "p1", source_execution_id="prepared-execution-1")
    bridge.jobs[0]["state"] = "COMPLETE"
    resumed = _coordinator(store, packing, bridge).start(
        "p1", source_execution_id="prepared-execution-1")
    assert first.ok and not resumed.ok and resumed.duplicate
    assert resumed.state == "COMPLETE" and resumed.dispatch_id == first.dispatch_id
    assert packing.pack_calls == bridge.submits == 1


def test_prepared_source_survives_bounded_manual_history(tmp_path):
    store = AuditStartIntentStore(tmp_path / "intents.json", history_bound=6)
    source, _ = store.begin("p1", "P1", "quick3", "prepared-execution-1")
    store.update(source["intent_id"], status="FAILED", error="interrupted")
    for index in range(12):
        intent, _ = store.begin(f"other-{index}", f"Other {index}", "quick3")
        store.update(intent["intent_id"], status="FAILED", error="done")
    assert store.find_for_source("prepared-execution-1")["intent_id"] == source["intent_id"]
