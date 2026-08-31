"""SRC-005 browser dispatcher domain regressions."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from audapack.bridge.browser_dispatch import (
    JOB_ARTIFACT_FETCHED,
    JOB_ATTACHED,
    JOB_AUDITING,
    JOB_BLOCKED,
    JOB_CANCELLED,
    JOB_COMPLETE,
    JOB_FINALIZING,
    JOB_QUEUED,
    JOB_RETRYABLE,
    JOB_START_PREPARED,
    JOB_STARTED,
    MAX_ACTIVE_WORKERS,
    WORKER_AUDITING,
    WORKER_TTL_SECONDS,
    BrowserDispatcher,
    DispatchError,
)


def archive(tmp_path: Path, name="project.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(b"PK\\x03\\x04archive")
    return path


def dispatcher(tmp_path: Path) -> BrowserDispatcher:
    return BrowserDispatcher(state_dir=tmp_path / "dispatch")


def worker(wid: str, **overrides) -> dict:
    return {
        "worker_id": wid,
        "widget_version": "test",
        "bridge_api_version": "3",
        "site": "chatgpt",
        "conversation_key": "c:test",
        "generating": False,
        "action_in_flight": False,
        "has_manual_draft": False,
        "has_attachments": False,
        **overrides,
    }


def job_payload(path: Path, name="PROJECT") -> dict:
    return {"project_id": name.lower(), "project_name": name, "archive_path": str(path), "archive_filename": path.name}


def test_worker_registration_is_idempotent(tmp_path):
    d = dispatcher(tmp_path)
    d.register_worker(worker("w1"))
    d.register_worker(worker("w1"))
    assert len(d.list_workers()) == 1


def test_worker_ttl_expires(tmp_path):
    d = dispatcher(tmp_path)
    record = d.register_worker(worker("w1"))
    record.last_seen_at -= WORKER_TTL_SECONDS + 1
    d._expire_workers()
    assert d.list_workers() == []


def test_auditing_worker_ttl_expires_without_heartbeat(tmp_path):
    d = dispatcher(tmp_path)
    record = d.register_worker(worker("w1"))
    record.last_seen_at -= WORKER_TTL_SECONDS + 1
    record.state = WORKER_AUDITING
    d._expire_workers()
    assert d.list_workers() == []


def test_seventh_worker_is_refused(tmp_path):
    d = dispatcher(tmp_path)
    for i in range(MAX_ACTIVE_WORKERS):
        d.register_worker(worker(f"w{i}"))
    with pytest.raises(DispatchError, match="at most") as exc:
        d.register_worker(worker("w7"))
    assert exc.value.code == "worker_limit"


def test_supported_chromium_root_displaces_stale_incompatible_widget(tmp_path):
    d = dispatcher(tmp_path)
    for i in range(MAX_ACTIVE_WORKERS):
        d.register_worker(worker(f"legacy{i}", widget_version="AUDAPACK_WIDGET/2"))

    d.register_worker(worker(
        "chrome-root",
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=True,
        url_path="/",
        browser_name="Chrome",
    ))

    live_ids = {item.worker_id for item in d.list_workers()}
    assert "chrome-root" in live_ids
    assert len(live_ids) == MAX_ACTIVE_WORKERS


def test_embedded_sentinel_frame_never_consumes_worker_slot(tmp_path):
    d = dispatcher(tmp_path)
    embedded = worker(
        "sentinel-frame",
        widget_version="AUDAPACK_WIDGET/2",
        url_path="/backend-api/sentinel/frame.html",
    )

    with pytest.raises(DispatchError) as exc:
        d.register_worker(embedded)

    assert exc.value.code == "ineligible_worker_context"
    assert d.list_workers() == []

    # A legacy frame already present in the registry is purged on its next
    # heartbeat instead of occupying a slot until TTL expiry.
    record = d.register_worker(worker("sentinel-frame", widget_version="test"))
    assert record.worker_id == "sentinel-frame"
    with pytest.raises(DispatchError):
        d.register_worker(embedded)
    assert d.list_workers() == []


def test_busy_worker_cannot_claim(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1", generating=True))
    d.enqueue_job(job_payload(path))
    assert d.claim_job("w1") is None


def test_only_v3_chromium_root_widget_can_claim(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("legacy", widget_version="AUDAPACK_WIDGET"))
    d.register_worker(worker("v2", widget_version="AUDAPACK_WIDGET/2"))
    d.register_worker(worker("v3_unsupported", widget_version="AUDAPACK_WIDGET/3", page_eligible=True, url_path="/", clean_for_audit=True, has_conversation_turns=False))
    d.register_worker(worker("v3_chat", widget_version="AUDAPACK_WIDGET/3", is_brave=True, page_eligible=False, url_path="/c/old", clean_for_audit=False, has_conversation_turns=False))
    d.register_worker(worker("v3_root", widget_version="AUDAPACK_WIDGET/3", is_chromium=True, page_eligible=True, url_path="/", browser_name="Chrome", clean_for_audit=True, has_conversation_turns=False))
    item = d.enqueue_job(job_payload(path))

    assert d.claim_job("legacy") is None
    assert d.claim_job("v2") is None
    assert d.claim_job("v3_unsupported") is None
    assert d.claim_job("v3_chat") is None
    assert d.claim_job("v3_root").dispatch_id == item.dispatch_id


def test_claim_is_fifo_and_unique_under_threads(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.enqueue_job(job_payload(path, "A"))
    d.register_worker(worker("w1"))
    d.register_worker(worker("w2"))
    results = []
    threads = [threading.Thread(target=lambda wid=wid: results.append(d.claim_job(wid))) for wid in ("w1", "w2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    claimed = [item for item in results if item]
    assert len(claimed) == 1
    assert claimed[0].project_name == "A"


def test_all_busy_job_remains_queued(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1", generating=True))
    item = d.enqueue_job(job_payload(path))
    assert d.claim_job("w1") is None
    assert d.get_job(item.dispatch_id).state == JOB_QUEUED


def test_freed_worker_claims_queued_job(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    record = d.register_worker(worker("w1", generating=True))
    item = d.enqueue_job(job_payload(path))
    assert d.claim_job("w1") is None
    record.generating = False
    assert d.claim_job("w1").dispatch_id == item.dispatch_id


def test_retryable_requeues_without_stale_lease(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_RETRYABLE, {"error": "temporary"})
    assert d.transition_job(item.dispatch_id, "w1", "", JOB_QUEUED).state == JOB_QUEUED


def test_pre_start_lease_expiry_requeues(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    leased = d.claim_job("w1")
    leased.lease_expires_at = time.time() - 1
    assert d.expire_leases() == 1
    assert d.get_job(item.dispatch_id).state == JOB_QUEUED


def test_post_start_lease_expiry_blocks_without_redispatch(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    leased = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED):
        d.transition_job(item.dispatch_id, "w1", leased.lease_id, state)
    d.transition_job(item.dispatch_id, "w1", leased.lease_id, JOB_START_PREPARED, {"campaign_run_id": "run", "start_receipt": "receipt-start"})
    leased.lease_expires_at = time.time() - 1
    d.expire_leases()
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED
    assert d.claim_job("w1") is None


def test_stale_lease_and_owner_rejected(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    d.register_worker(worker("w2"))
    item = d.enqueue_job(job_payload(path))
    leased = d.claim_job("w1")
    with pytest.raises(DispatchError) as wrong_lease:
        d.transition_job(item.dispatch_id, "w1", "fake", JOB_ARTIFACT_FETCHED)
    assert wrong_lease.value.code == "stale_lease"
    with pytest.raises(DispatchError) as wrong_owner:
        d.transition_job(item.dispatch_id, "w2", leased.lease_id, JOB_ARTIFACT_FETCHED)
    assert wrong_owner.value.code == "stale_owner"


def test_artifact_requires_lease_owner(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    with pytest.raises(DispatchError) as not_leased:
        d.resolve_artifact(item.dispatch_id, "w1", "fake")
    assert not_leased.value.code == "invalid_transition"
    leased = d.claim_job("w1")
    assert d.resolve_artifact(item.dispatch_id, "w1", leased.lease_id) == path


def test_missing_and_changed_artifacts_rejected(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    missing = d.enqueue_job(job_payload(path))
    leased = d.claim_job("w1")
    path.unlink()
    with pytest.raises(DispatchError) as gone:
        d.resolve_artifact(missing.dispatch_id, "w1", leased.lease_id)
    assert gone.value.code == "missing_archive"

    path = archive(tmp_path, "changed.zip")
    d2 = dispatcher(tmp_path / "second")
    d2.register_worker(worker("w1"))
    item = d2.enqueue_job({**job_payload(path), "archive_size": path.stat().st_size, "archive_sha256": "0" * 64})
    lease = d2.claim_job("w1")
    with pytest.raises(DispatchError) as changed:
        d2.resolve_artifact(item.dispatch_id, "w1", lease.lease_id)
    assert changed.value.code == "changed_archive"


def test_full_lifecycle_frees_worker(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING, JOB_COMPLETE):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt-start"})
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE
    assert d.status()["free_workers"] == 1


def test_illegal_transition_rejected(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    with pytest.raises(DispatchError) as exc:
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_COMPLETE)
    assert exc.value.code == "invalid_transition"


def test_jobs_survive_restart(tmp_path):
    path = archive(tmp_path)
    d1 = dispatcher(tmp_path)
    item = d1.enqueue_job(job_payload(path))
    d2 = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    assert d2.get_job(item.dispatch_id).state == JOB_QUEUED
    assert d2.status()["queued_jobs"] == 1


def test_active_heartbeat_renews_exact_lease(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    before = lease.lease_expires_at
    lease.lease_expires_at = time.time() + 1
    d.register_worker(worker("w1", state="AUDITING", dispatch_id=item.dispatch_id, lease_id=lease.lease_id))
    assert d.get_job(item.dispatch_id).lease_expires_at > before


def test_finalizing_requires_durable_campaign_proof(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    final = tmp_path / "final.md"
    final.write_text("final", encoding="utf-8")
    campaign = tmp_path / "campaign.json"
    campaign.write_text(json.dumps({
        "campaign_status": "COMPLETE",
        "campaign_run_id": "run",
        "wave_count": 3,
        "completed_count": 3,
    }), encoding="utf-8")
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING, JOB_FINALIZING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    done = d.complete_for_run(item.project_id, "run", final, campaign_path=campaign, expected_wave_count=3)
    assert done.state == JOB_COMPLETE
    assert done.final_handoff_sha256


def test_post_start_restart_reconciles_same_owner(tmp_path):
    path = archive(tmp_path)
    d1 = dispatcher(tmp_path)
    d1.register_worker(worker("w1"))
    item = d1.enqueue_job(job_payload(path))
    lease = d1.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d1.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    d2 = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    assert d2.get_job(item.dispatch_id).state == JOB_BLOCKED
    d2.register_worker(worker("w1", state="AUDITING", dispatch_id=item.dispatch_id, lease_id=lease.lease_id, campaign_run_id="run", start_receipt="receipt"))
    assert d2.get_job(item.dispatch_id).state == JOB_AUDITING


def test_duplicate_active_project_dispatch_is_rejected(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.enqueue_job(job_payload(path, "SAME"))
    with pytest.raises(DispatchError) as exc:
        d.enqueue_job(job_payload(path, "SAME"))
    assert exc.value.code == "duplicate_dispatch"


# P0-3: a ChatGPT worker with an existing conversation (has_conversation_turns)
# or no positive clean_for_audit proof must NEVER claim an audit job, even if
# the composer is empty. Only the supported AUDAPACK_WIDGET/3 contract enforces
# the clean gate; legacy registrations fall back to historical behaviour.
def test_v3_worker_with_occupied_conversation_is_rejected(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker(
        "v3_occupied",
        widget_version="AUDAPACK_WIDGET/3",
        is_brave=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=False,
        has_conversation_turns=True,
    ))
    d.enqueue_job(job_payload(path))
    assert d.claim_job("v3_occupied") is None


def test_v3_worker_with_legacy_clean_default_can_claim(tmp_path):
    """Unprefixed / test registrations without clean flag remain claimable (historical contract)."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("legacy", widget_version="test"))
    item = d.enqueue_job(job_payload(path))
    claimed = d.claim_job("legacy")
    assert claimed is not None and claimed.dispatch_id == item.dispatch_id


def test_v3_dirty_worker_with_draft_is_rejected(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker(
        "v3_dirty",
        widget_version="AUDAPACK_WIDGET/3",
        is_brave=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=False,
        has_conversation_turns=False,
        has_manual_draft=True,
    ))
    d.enqueue_job(job_payload(path))
    assert d.claim_job("v3_dirty") is None


# Status must expose clean count + CLEAN/BUSY/OCCUPIED classification for
# Project Room display and dispatcher feedback (P0-15 / 3.15).
def test_status_exposes_clean_worker_count_and_classification(tmp_path):
    d = dispatcher(tmp_path)
    d.register_worker(worker(
        "v3_clean",
        widget_version="AUDAPACK_WIDGET/3",
        is_brave=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=True,
        has_conversation_turns=False,
    ))
    d.register_worker(worker(
        "v3_occ",
        widget_version="AUDAPACK_WIDGET/3",
        is_brave=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=False,
        has_conversation_turns=True,
    ))
    st = d.status()
    assert st["clean_workers"] == 1
    assert st["active_workers"] == 2
    occ = [w for w in d.list_workers() if w.worker_id == "v3_occ"][0]
    assert occ.has_conversation_turns is True
    assert occ.clean_for_audit is False


# W4.1: lease expiry after START must record recovery_state + preserve lineage.
def test_post_start_expiry_records_recovery_state(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.lease_expires_at = time.time() - 1
    d.expire_leases()
    job = d.get_job(item.dispatch_id)
    assert job.state == JOB_BLOCKED
    assert job.recovery_state == JOB_AUDITING
    assert job.campaign_run_id == "run"
    assert job.start_receipt == "receipt"
    assert job.assigned_worker_id == "w1"


# W4.2: same-owner reconciliation works for expiry blocks, not just restart.
def test_expiry_recovery_reconciles_same_owner(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.lease_expires_at = time.time() - 1
    d.expire_leases()
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED
    d.register_worker(worker("w1", state="AUDITING", dispatch_id=item.dispatch_id, lease_id=lease.lease_id, campaign_run_id="run", start_receipt="receipt"))
    assert d.get_job(item.dispatch_id).state == JOB_AUDITING


# W5.1/W5.2: cancel preserves owner identity; poll returns owned CANCELLED.
def test_cancel_preserves_owner_identity_for_ack(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_ARTIFACT_FETCHED)
    assert d.cancel_job(item.dispatch_id)
    job = d.get_job(item.dispatch_id)
    assert job.state == JOB_CANCELLED
    assert job.cancel_owner_worker_id == "w1"
    assert job.cancel_owner_lease_id == lease.lease_id
    # original worker still "owns" the cancelled job for ACK purposes
    owned = d.get_owned_job("w1")
    assert owned is not None and owned.dispatch_id == item.dispatch_id and owned.state == JOB_CANCELLED
    # wrong worker cannot finalize
    with pytest.raises(DispatchError) as wrong:
        d.finalize_cancel(item.dispatch_id, "w2", "nope")
    assert wrong.value.code == "stale_owner"
    # correct owner finalizes
    d.finalize_cancel(item.dispatch_id, "w1", lease.lease_id)
    assert d.get_owned_job("w1") is None


# W6: post-start BLOCKED cannot be ordinary-cancelled.
def test_post_start_blocked_rejects_ordinary_cancel(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_BLOCKED, {"error": "recovery"})
    with pytest.raises(DispatchError) as exc:
        d.cancel_job(item.dispatch_id)
    assert exc.value.code == "post_start_blocked"


# W6: pre-start BLOCKED (no start_receipt) is still cancellable.
def test_pre_start_blocked_can_cancel(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_ARTIFACT_FETCHED)
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_ATTACHED)
    assert d.cancel_job(item.dispatch_id)
