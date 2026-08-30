"""SRC-004 mandatory scenario regressions not covered by the base dispatch suite.

Scenarios from the one-click audit roadmap:
  08  a long-running audit (>10 min) must never expire while heartbeats renew
  16  a stale-but-not-yet-TTL-expired ghost worker must not stall a live worker
  25  a failed final disk write must never produce a green COMPLETE
  30  a meaningful dispatch state change must bump the generation file
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from audapack.bridge import browser_dispatch as bd
from audapack.bridge.browser_dispatch import (
    JOB_ARTIFACT_FETCHED,
    JOB_ATTACHED,
    JOB_AUDITING,
    JOB_COMPLETE,
    JOB_FINALIZING,
    JOB_START_PREPARED,
    JOB_STARTED,
    BrowserDispatcher,
    DispatchError,
)


def archive(tmp_path: Path, name="project.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(b"PK\x03\x04archive")
    return path


def dispatcher(tmp_path: Path) -> BrowserDispatcher:
    return BrowserDispatcher(state_dir=tmp_path / "dispatch")


def worker(wid: str, **overrides) -> dict:
    return {
        "worker_id": wid,
        "widget_version": "test",
        "bridge_api_version": "3",
        "site": "chatgpt",
        "generating": False,
        "action_in_flight": False,
        "has_manual_draft": False,
        "has_attachments": False,
        **overrides,
    }


def job_payload(path: Path, name="PROJECT") -> dict:
    return {"project_id": name.lower(), "project_name": name, "archive_path": str(path), "archive_filename": path.name}


def run_to_auditing(d: BrowserDispatcher, tmp_path: Path, wid="w1"):
    path = archive(tmp_path)
    d.register_worker(worker(wid))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job(wid)
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, wid, lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    return item, lease


def test_08_long_running_audit_lease_never_expires(monkeypatch, tmp_path):
    d = dispatcher(tmp_path)
    item, lease = run_to_auditing(d, tmp_path)
    start = 1_000_000.0
    now = {"t": start}
    monkeypatch.setattr(bd, "_now", lambda: now["t"])

    # Simulate >10 minutes of active audit with heartbeats every 30s.
    heartbeat = d.register_worker(
        worker("w1", state="AUDITING", dispatch_id=item.dispatch_id, lease_id=lease.lease_id)
    )
    for elapsed in range(30, 660, 30):
        now["t"] = start + elapsed
        heartbeat.last_seen_at = now["t"]
        d.renew_lease(item.dispatch_id, "w1", lease.lease_id)
        assert d.get_job(item.dispatch_id).state == JOB_AUDITING

    # Lease must have been renewed well past the original 180s boundary.
    assert d.get_job(item.dispatch_id).lease_expires_at >= start + 600


def test_16_ghost_worker_does_not_stall_live_worker(monkeypatch, tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(bd, "_now", lambda: now["t"])

    # Ghost worker A: registered FREE, stops heartbeating, still inside TTL.
    ghost = d.register_worker(worker("A"))
    now["t"] += bd.WORKER_TTL_SECONDS - 1
    ghost.last_seen_at = now["t"] - (bd.WORKER_TTL_SECONDS - 10)
    d._workers["A"] = ghost  # keep it present and inside TTL

    # Live worker B: actively polling, FREE.
    d.register_worker(worker("B"))
    item = d.enqueue_job(job_payload(path, "G"))

    # B must atomically claim the job; A's dormant presence must not win.
    claimed = d.claim_job("B")
    assert claimed is not None
    assert claimed.dispatch_id == item.dispatch_id
    assert claimed.assigned_worker_id == "B"


def test_25_failed_final_disk_write_never_completes(tmp_path):
    d = dispatcher(tmp_path)
    item, lease = run_to_auditing(d, tmp_path)
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_FINALIZING)

    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(DispatchError) as exc:
        d.complete_for_run(item.project_id, "run", missing)
    assert exc.value.code == "missing_final_handoff"
    assert d.get_job(item.dispatch_id).state == JOB_FINALIZING

    # Final write "succeeds" but the digest comes back wrong -> still no COMPLETE.
    final = tmp_path / "final.md"
    final.write_text("final", encoding="utf-8")
    with pytest.raises(DispatchError) as exc:
        d.complete_for_run(item.project_id, "run", final, final_handoff_sha256="0" * 64)
    assert exc.value.code == "final_handoff_changed"
    assert d.get_job(item.dispatch_id).state == JOB_FINALIZING

    # A correctly durable handoff finally permits COMPLETE.
    done = d.complete_for_run(item.project_id, "run", final)
    assert done.state == JOB_COMPLETE


def test_30_generation_file_bumps_on_meaningful_state_change(tmp_path):
    d = dispatcher(tmp_path)
    item, lease = run_to_auditing(d, tmp_path)
    gen = int(json.loads(d.generation_file.read_text(encoding="utf-8")).get("generation", 0))

    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_FINALIZING)
    gen2 = int(json.loads(d.generation_file.read_text(encoding="utf-8")).get("generation", 0))
    assert gen2 > gen


def test_17_six_concurrent_workers_claim_unique_jobs(tmp_path):
    import threading

    d = dispatcher(tmp_path)
    paths = [archive(tmp_path, f"p{i}.zip") for i in range(6)]
    for i, path in enumerate(paths):
        d.enqueue_job(job_payload(path, f"P{i}"))
    for i in range(6):
        d.register_worker(worker(f"w{i}"))

    results: list = []
    threads = [threading.Thread(target=lambda wid=f"w{i}": results.append(d.claim_job(wid))) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [item for item in results if item]
    assert len(claimed) == 6
    assert len({item.dispatch_id for item in claimed}) == 6
    assert len({item.project_id for item in claimed}) == 6
    assert d.status()["queued_jobs"] == 0


def test_17b_seventh_active_worker_refused(tmp_path):
    d = dispatcher(tmp_path)
    for i in range(6):
        d.register_worker(worker(f"w{i}"))
    with pytest.raises(DispatchError) as exc:
        d.register_worker(worker("w6"))
    assert exc.value.code == "worker_limit"
    assert d.status()["active_workers"] == 6
