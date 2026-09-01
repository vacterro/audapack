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


def lose_worker(d: BrowserDispatcher, wid: str) -> None:
    """Age a worker past its heartbeat TTL: the window is gone, not just quiet.

    A post-START lease expires only when the worker is actually lost. Setting
    lease_expires_at into the past is not enough on its own -- a registered,
    heartbeating window mid-audit has no transition to make and keeps its run.
    """
    d._workers[wid].last_seen_at = time.time() - (WORKER_TTL_SECONDS + 5)


def test_worker_registration_is_idempotent(tmp_path):
    d = dispatcher(tmp_path)
    d.register_worker(worker("w1"))
    d.register_worker(worker("w1"))
    assert len(d.list_workers()) == 1


def test_worker_registration_preserves_managed_slot_identity(tmp_path):
    d = dispatcher(tmp_path)
    record = d.register_worker(worker("w-managed", managed_slot=4, managed_generation=11))
    assert record.managed_slot == 4
    assert record.managed_generation == 11


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
    lose_worker(d, "w1")
    d.expire_leases()
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED
    # A replacement window on the same slot must not be handed the blocked run.
    d.register_worker(worker("w1"))
    assert d.claim_job("w1") is None


def test_post_start_lease_does_not_expire_under_a_live_worker(tmp_path):
    """A window mid-audit keeps its run: silence is not death.

    AUDITING makes no transitions for as long as the audit takes, and only a
    transition used to extend the lease, so a healthy run was blocked with
    "worker lost after START_PREPARED" minutes into a wave -- the audit kept
    running in the browser and finished onto disk while its dispatch record
    said it had failed.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    leased = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", leased.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    d.get_job(item.dispatch_id).lease_expires_at = time.time() - 1
    d.expire_leases()
    assert d.get_job(item.dispatch_id).state == JOB_AUDITING


def test_owner_poll_renews_its_lease(tmp_path):
    """Polling is proof of life, so it renews the owned lease."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    leased = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", leased.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    d.get_job(item.dispatch_id).lease_expires_at = time.time() - 1
    renewed = d.renew_owner_lease("w1")
    assert renewed is not None and renewed.dispatch_id == item.dispatch_id
    assert d.get_job(item.dispatch_id).lease_expires_at > time.time()
    assert d.renew_owner_lease("nobody") is None


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
    lose_worker(d, "w1")
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
    lose_worker(d, "w1")
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


def blocked_post_start(d, path, name="PROJECT"):
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path, name))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_BLOCKED, {"error": "worker lost after START_PREPARED; recovery required"})
    return item


def test_abandon_frees_a_stuck_post_start_blocked_project(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    item = blocked_post_start(d, path)
    # Cancel must keep refusing: CANCELLED would assert no Core was sent.
    with pytest.raises(DispatchError) as refused:
        d.cancel_job(item.dispatch_id)
    assert refused.value.code == "post_start_blocked"

    job = d.abandon_job(item.dispatch_id, "operator forced unblock")
    assert job.state == "FAILED"
    assert job.last_error_code == "operator_abandoned"
    assert "operator forced unblock" in job.error
    assert job.campaign_run_id == "run" and job.start_receipt == "receipt", "post-start lineage must survive"
    assert job.assigned_worker_id == "" and job.lease_id == ""
    # Project lane is free again: a fresh dispatch is accepted.
    fresh = d.enqueue_job(job_payload(path))
    assert fresh.state == JOB_QUEUED


def test_abandon_is_idempotent_and_refuses_non_blocked_runs(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    item = blocked_post_start(d, path)
    first = d.abandon_job(item.dispatch_id)
    assert d.abandon_job(item.dispatch_id).state == first.state

    d.register_worker(worker("w2"))
    live = d.enqueue_job(job_payload(path, "OTHER"))
    with pytest.raises(DispatchError) as exc:
        d.abandon_job(live.dispatch_id)
    assert exc.value.code == "invalid_transition"
    with pytest.raises(DispatchError) as unknown:
        d.abandon_job("dsp-0000000000000000")
    assert unknown.value.code == "unknown_job"


def test_abandoned_run_is_never_reclaimed_by_a_worker(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    item = blocked_post_start(d, path)
    d.abandon_job(item.dispatch_id)
    d.register_worker(worker("w-fresh"))
    assert d.claim_job("w-fresh") is None, "an abandoned run must never produce a second Core"


def test_parked_operator_tabs_do_not_consume_audit_lanes(tmp_path):
    """Six ordinary ChatGPT tabs must never starve a real queued audit.

    Live evidence: the Bridge reported `W 6/6 CLEAN 0` with two queued audits
    and nothing running, because every lane was held by a legacy-widget build or
    a parked conversation that could never claim anything.
    """
    d = dispatcher(tmp_path)
    for i in range(3):
        d.register_worker(worker(
            f"legacy{i}",
            widget_version="AUDAPACK_WIDGET",
            is_chromium=False,
            page_eligible=False,
            url_path=f"/c/old-{i}",
        ))
    for i in range(3):
        d.register_worker(worker(
            f"parked{i}",
            widget_version="AUDAPACK_WIDGET/3",
            is_chromium=True,
            page_eligible=False,
            url_path=f"/c/human-{i}",
            has_conversation_turns=True,
        ))

    status = d.status()
    assert status["active_workers"] == 0, status
    assert status["foreign_workers"] == 6

    # A real managed window can still register even though the registry is full.
    d.register_worker(worker(
        "audapack-managed-1-1",
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=True,
        managed_slot=1,
        managed_generation=1,
    ))
    live = {item.worker_id for item in d.list_workers()}
    assert "audapack-managed-1-1" in live
    assert len(live) <= MAX_ACTIVE_WORKERS

    status = d.status()
    assert status["active_workers"] == 1
    assert status["clean_workers"] == 1


def test_a_worker_running_a_job_still_holds_its_lane(tmp_path):
    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    d = dispatcher(tmp_path)
    d.register_worker(worker(
        "audapack-managed-1-1",
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=True,
    ))
    job = d.enqueue_job(job_payload(archive))
    claimed = d.claim_job("audapack-managed-1-1", worker(
        "audapack-managed-1-1",
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=True,
    ))
    assert claimed is not None and claimed.dispatch_id == job.dispatch_id

    # Mid-run the window leaves the root path; it must keep its lane.
    d.register_worker(worker(
        "audapack-managed-1-1",
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=False,
        url_path="/c/live-run",
        clean_for_audit=False,
    ))
    status = d.status()
    assert status["active_workers"] == 1
    assert status["clean_workers"] == 0


def _clean_root_worker(wid: str) -> dict:
    return worker(
        wid,
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=True,
        url_path="/",
        clean_for_audit=True,
        has_conversation_turns=False,
    )


def test_a_worker_that_came_back_clean_releases_its_abandoned_run(tmp_path, monkeypatch):
    """clean_workers > 0 with free_workers 0 is a deadlock, not a busy pool.

    Live evidence: __SAITULS sat in STARTED and _AUDAPACK in AUDITING, both
    assigned to managed workers that reported themselves CLEAN on the root page,
    so three queued audits never got claimed.
    """
    import audapack.bridge.browser_dispatch as bd

    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    d = dispatcher(tmp_path)
    d.register_worker(_clean_root_worker("audapack-managed-2-1-aaaa"))
    job = d.enqueue_job(job_payload(archive))
    d.claim_job("audapack-managed-2-1-aaaa", _clean_root_worker("audapack-managed-2-1-aaaa"))
    d.transition_job(job.dispatch_id, "audapack-managed-2-1-aaaa", job.lease_id, "ARTIFACT_FETCHED", {})
    d.transition_job(job.dispatch_id, "audapack-managed-2-1-aaaa", job.lease_id, "ATTACHED", {})
    d.transition_job(job.dispatch_id, "audapack-managed-2-1-aaaa", job.lease_id, "START_PREPARED", {"campaign_run_id": "run-1", "start_receipt": "startcore-1"})
    d.transition_job(job.dispatch_id, "audapack-managed-2-1-aaaa", job.lease_id, "STARTED", {})
    assert d._jobs[job.dispatch_id].state == "STARTED"

    # Inside the grace window nothing is touched.
    assert d.reconcile_abandoned_runs(grace_seconds=90.0) == 0

    # The window comes back clean on the root page well after the run stalled.
    now = bd._now()
    d._jobs[job.dispatch_id].updated_at = now - 600
    d.register_worker(_clean_root_worker("audapack-managed-2-1-aaaa"))

    assert d.reconcile_abandoned_runs(grace_seconds=90.0) == 1
    stuck = d._jobs[job.dispatch_id]
    assert stuck.state == "BLOCKED"
    assert stuck.recovery_state == "STARTED"
    assert stuck.assigned_worker_id == ""
    assert "no longer owns this run" in stuck.error

    # The lane is free again, so the next queued audit can actually be claimed.
    assert d.worker_free_for_claim(d._workers["audapack-managed-2-1-aaaa"]) is True


def test_a_worker_still_running_its_audit_is_never_reconciled_away(tmp_path):
    import audapack.bridge.browser_dispatch as bd

    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    d = dispatcher(tmp_path)
    d.register_worker(_clean_root_worker("audapack-managed-5-1-bbbb"))
    job = d.enqueue_job(job_payload(archive))
    d.claim_job("audapack-managed-5-1-bbbb", _clean_root_worker("audapack-managed-5-1-bbbb"))
    d.transition_job(job.dispatch_id, "audapack-managed-5-1-bbbb", job.lease_id, "ARTIFACT_FETCHED", {})
    d.transition_job(job.dispatch_id, "audapack-managed-5-1-bbbb", job.lease_id, "ATTACHED", {})
    d.transition_job(job.dispatch_id, "audapack-managed-5-1-bbbb", job.lease_id, "START_PREPARED", {"campaign_run_id": "run-2", "start_receipt": "startcore-2"})
    d.transition_job(job.dispatch_id, "audapack-managed-5-1-bbbb", job.lease_id, "STARTED", {})
    d.transition_job(job.dispatch_id, "audapack-managed-5-1-bbbb", job.lease_id, "AUDITING", {})

    # The window is where it should be: inside the run, on the conversation.
    d.register_worker(worker(
        "audapack-managed-5-1-bbbb",
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        page_eligible=False,
        url_path="/c/live-run",
        clean_for_audit=False,
        has_conversation_turns=True,
        campaign_run_id="run-2",
    ))
    d._jobs[job.dispatch_id].updated_at = bd._now() - 600

    assert d.reconcile_abandoned_runs(grace_seconds=90.0) == 0
    assert d._jobs[job.dispatch_id].state == "AUDITING"


def test_a_pre_start_job_is_requeued_once_its_worker_is_gone_for_good(tmp_path):
    """A worker expires at 75 s but its lease runs 180 s.

    A job leased to a window that vanished sat untouchable for the difference
    while clean workers idled beside it, which is most of the "it picks up a
    minute later" feeling. Nothing before START_PREPARED is irreversible.
    """
    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    d = dispatcher(tmp_path)
    d.register_worker(_clean_root_worker("gone-worker"))
    job = d.enqueue_job(job_payload(archive))
    leased = d.claim_job("gone-worker", _clean_root_worker("gone-worker"))
    assert leased is not None and leased.state == "LEASED"
    assert leased.lease_expires_at > 0

    # The lease is still valid; only the worker is gone.
    d._workers.pop("gone-worker")
    assert d.expire_leases() == 0

    # A worker id is per window session, so a brief disappearance is not proof
    # of abandonment. Once the job itself has clearly stalled it goes back to
    # the queue without waiting out the remaining lease.
    import audapack.bridge.browser_dispatch as bd

    d._jobs[job.dispatch_id].updated_at = bd._now() - (bd.PRE_START_OWNER_GRACE_SECONDS + 5)
    assert d.expire_leases() == 1
    assert d._jobs[job.dispatch_id].state == "QUEUED"
    assert d._jobs[job.dispatch_id].assigned_worker_id == ""


def test_a_live_worker_keeps_its_pre_start_lease(tmp_path):
    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    d = dispatcher(tmp_path)
    d.register_worker(_clean_root_worker("live-worker"))
    job = d.enqueue_job(job_payload(archive))
    d.claim_job("live-worker", _clean_root_worker("live-worker"))

    assert d.expire_leases() == 0
    assert d._jobs[job.dispatch_id].state == "LEASED"
    assert d._jobs[job.dispatch_id].assigned_worker_id == "live-worker"


def test_re_acking_the_same_start_receipt_is_a_no_op_not_an_error(tmp_path):
    """A first Send that is not positively verified retries through recovery.

    The retry re-acks START_PREPARED with the identical receipt. Rejecting that
    made the worker stand down and abandon a perfectly prepared Core, leaving
    the job START_PREPARED forever with nothing able to re-lease it.
    """
    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    d = dispatcher(tmp_path)
    d.register_worker(_clean_root_worker("audapack-managed-1-1-aaaa"))
    job = d.enqueue_job(job_payload(archive))
    d.claim_job("audapack-managed-1-1-aaaa", _clean_root_worker("audapack-managed-1-1-aaaa"))
    lease = d._jobs[job.dispatch_id].lease_id
    d.transition_job(job.dispatch_id, "audapack-managed-1-1-aaaa", lease, "ARTIFACT_FETCHED", {})
    d.transition_job(job.dispatch_id, "audapack-managed-1-1-aaaa", lease, "ATTACHED", {})
    d.transition_job(
        job.dispatch_id, "audapack-managed-1-1-aaaa", lease, "START_PREPARED",
        {"campaign_run_id": "run-1", "start_receipt": "startcore-1"},
    )

    again = d.transition_job(
        job.dispatch_id, "audapack-managed-1-1-aaaa", lease, "START_PREPARED",
        {"campaign_run_id": "run-1", "start_receipt": "startcore-1"},
    )
    assert again.state == "START_PREPARED"
    assert again.start_receipt == "startcore-1"

    # Exactly-once is untouched: a different receipt is still refused.
    try:
        d.transition_job(
            job.dispatch_id, "audapack-managed-1-1-aaaa", lease, "START_PREPARED",
            {"campaign_run_id": "run-1", "start_receipt": "startcore-SECOND"},
        )
    except DispatchError as exc:
        assert exc.code == "start_receipt_conflict"
    else:
        raise AssertionError("a second distinct START receipt must never be accepted")


def test_a_bridge_restart_does_not_kill_a_live_audit(tmp_path):
    """A restart blocks a post-START run pending same-worker reconciliation.

    The audit keeps running in the browser, so the lane must be recoverable by
    the exact worker that owns it -- not stranded BLOCKED forever.
    """
    archive = tmp_path / "PROJECT.zip"
    archive.write_bytes(b"zip")
    state_dir = tmp_path / "dispatch"
    d = BrowserDispatcher(state_dir=state_dir)
    d.register_worker(_clean_root_worker("audapack-managed-1-1-live"))
    job = d.enqueue_job(job_payload(archive))
    d.claim_job("audapack-managed-1-1-live", _clean_root_worker("audapack-managed-1-1-live"))
    lease = d._jobs[job.dispatch_id].lease_id
    for state, payload in (
        ("ARTIFACT_FETCHED", {}),
        ("ATTACHED", {}),
        ("START_PREPARED", {"campaign_run_id": "run-live", "start_receipt": "startcore-live"}),
        ("STARTED", {}),
        ("AUDITING", {}),
    ):
        d.transition_job(job.dispatch_id, "audapack-managed-1-1-live", lease, state, payload)

    # Bridge restarts: same state directory, fresh dispatcher.
    restarted = BrowserDispatcher(state_dir=state_dir)
    blocked = restarted._jobs[job.dispatch_id]
    assert blocked.state == "BLOCKED"
    assert blocked.recovery_state == "AUDITING"
    assert blocked.assigned_worker_id == "audapack-managed-1-1-live"
    assert blocked.lease_id == lease

    # The owning worker comes back with its lease and the run resumes.
    payload = _clean_root_worker("audapack-managed-1-1-live")
    payload.update({
        "dispatch_id": job.dispatch_id,
        "lease_id": lease,
        "campaign_run_id": "run-live",
        "start_receipt": "startcore-live",
    })
    restarted.register_worker(payload)
    assert restarted._jobs[job.dispatch_id].state == "AUDITING"
    assert restarted._jobs[job.dispatch_id].error == ""


def test_complete_for_run_reconciles_blocked_post_start(tmp_path):
    """A BLOCKED post-start run with durable COMPLETE campaign proof reconciles."""
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
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_AUDITING
    job.error = "restart recovery"
    done = d.complete_for_run(item.project_id, "run", final, campaign_path=campaign, expected_wave_count=3)
    assert done.state == JOB_COMPLETE
    assert done.final_handoff_sha256


def test_complete_for_run_rejects_blocked_without_recovery_state(tmp_path):
    """A BLOCKED job without a post-start recovery state stays blocked."""
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
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state, {"campaign_run_id": "run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.error = "restart recovery"
    assert d.complete_for_run(item.project_id, "run", final, campaign_path=campaign, expected_wave_count=3) is None


def supported_worker(wid: str, **overrides) -> dict:
    """A registration payload the dispatcher accepts as a real audit lane."""
    return worker(
        wid,
        widget_version="AUDAPACK_WIDGET/3",
        is_chromium=True,
        is_brave=False,
        page_eligible=True,
        clean_for_audit=True,
        has_conversation_turns=False,
        url_path="/",
        **overrides,
    )


def test_a_managed_window_takes_its_lane_from_an_idle_personal_tab(tmp_path):
    """Six managed windows plus a personal ChatGPT tab is seven for six lanes.

    The operator asked for six audit windows; a ChatGPT tab they happen to have
    open in their own browser must not be the reason one of them is refused.
    Without this the seventh registration lost, then won on the next heartbeat,
    and the pool thrashed instead of settling at six.
    """
    d = dispatcher(tmp_path)
    d.register_worker(supported_worker("personal"))
    for slot in range(1, MAX_ACTIVE_WORKERS):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))
    assert len(d.list_workers()) == MAX_ACTIVE_WORKERS

    d.register_worker(supported_worker("managed-6", managed_slot=6, managed_generation=1))
    live = {w.worker_id for w in d.list_workers()}
    assert "managed-6" in live
    assert "personal" not in live
    assert len(live) == MAX_ACTIVE_WORKERS


def test_a_personal_tab_running_an_audit_keeps_its_lane(tmp_path):
    """Yielding is for idle tabs only -- never for one mid-run."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("personal"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("personal")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "personal", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})
    for slot in range(1, MAX_ACTIVE_WORKERS):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))

    with pytest.raises(DispatchError) as refused:
        d.register_worker(supported_worker("managed-6", managed_slot=6, managed_generation=1))
    assert refused.value.code == "worker_limit"
    assert "personal" in {w.worker_id for w in d.list_workers()}


def test_six_managed_slots_own_every_lane(tmp_path):
    """With six managed windows live, a personal tab gets no lane at all.

    Admitting it evicted a managed window, which came back and evicted the tab,
    and the pool oscillated between five and six lanes indefinitely instead of
    settling -- observed live as managed slots rotating in and out on every
    heartbeat while offline_workers climbed.
    """
    d = dispatcher(tmp_path)
    for slot in range(1, MAX_ACTIVE_WORKERS + 1):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))

    with pytest.raises(DispatchError) as refused:
        d.register_worker(supported_worker("personal"))
    assert refused.value.code == "worker_limit"

    live = {w.worker_id for w in d.list_workers()}
    assert live == {f"managed-{slot}" for slot in range(1, MAX_ACTIVE_WORKERS + 1)}


def test_a_reloaded_managed_window_replaces_its_own_slot(tmp_path):
    """One slot is one lane: a reload must not leave two records behind."""
    d = dispatcher(tmp_path)
    d.register_worker(supported_worker("audapack-managed-3-1-old", managed_slot=3, managed_generation=1))
    d.register_worker(supported_worker("audapack-managed-3-1-new", managed_slot=3, managed_generation=1))
    live = {w.worker_id for w in d.list_workers()}
    assert live == {"audapack-managed-3-1-new"}


def test_a_personal_tab_is_admitted_when_managed_windows_are_gone(tmp_path):
    """The lane reservation is for live managed windows, not a permanent ban."""
    import audapack.bridge.browser_dispatch as module

    d = dispatcher(tmp_path)
    for slot in range(1, MAX_ACTIVE_WORKERS + 1):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))
    # Every managed window is closed and its memory of the slot has lapsed.
    d._workers.clear()
    d._managed_slot_seen = {
        slot: seen - (module.MANAGED_SLOT_MEMORY_SECONDS + 1)
        for slot, seen in d._managed_slot_seen.items()
    }
    record = d.register_worker(supported_worker("personal"))
    assert record.worker_id == "personal"


def test_an_already_seated_personal_tab_gives_its_lane_back(tmp_path):
    """The sixth managed window evicts a tab that got in first.

    Refusing newcomers is not enough: a personal tab admitted while only five
    managed windows had registered kept its lane forever, and the sixth managed
    window rotated in and out against it heartbeat after heartbeat.
    """
    d = dispatcher(tmp_path)
    for slot in range(1, MAX_ACTIVE_WORKERS):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))
    d.register_worker(supported_worker("personal"))
    assert "personal" in {w.worker_id for w in d.list_workers()}

    d.register_worker(supported_worker("managed-6", managed_slot=6, managed_generation=1))
    live = {w.worker_id for w in d.list_workers()}
    assert live == {f"managed-{slot}" for slot in range(1, MAX_ACTIVE_WORKERS + 1)}


def test_poll_block_shrinks_as_the_worker_pool_grows(tmp_path):
    """Six windows sharing one serialized poll queue must all stay registered.

    A 20s block per poll times six windows is a two-minute round trip, and a
    worker that heartbeats every two minutes is dead by WORKER_TTL_SECONDS.
    """
    d = dispatcher(tmp_path)
    d.register_worker(supported_worker("managed-1", managed_slot=1, managed_generation=1))
    assert d.max_poll_wait_seconds() > 20.0  # one worker: no need to hurry

    for slot in range(2, MAX_ACTIVE_WORKERS + 1):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))
    wait = d.max_poll_wait_seconds()
    assert wait * MAX_ACTIVE_WORKERS < WORKER_TTL_SECONDS
    assert wait >= 2.0


def test_a_stale_widget_build_is_named_not_reported_clean(tmp_path):
    """A window that cannot claim must say why.

    The build gate is silent in the claim path, so six windows running an
    outdated widget sat there reporting CLEAN while every audit stayed QUEUED
    and nothing anywhere told the operator to update the widget.
    """
    import audapack.bridge.browser_dispatch as module

    d = dispatcher(tmp_path)
    d.register_worker(supported_worker(
        "fresh", widget_protocol="AUDAPACK_WIDGET/3", widget_build_version="9.9.9",
    ))
    d.register_worker(supported_worker(
        "stale", widget_protocol="AUDAPACK_WIDGET/3", widget_build_version="0.0.1",
    ))

    original = module._get_required_widget_build
    module._get_required_widget_build = lambda: "9.9.9"
    try:
        workers = {w.worker_id: w for w in d.list_workers()}
        assert d.worker_widget_is_stale(workers["stale"]) is True
        assert d.worker_widget_is_stale(workers["fresh"]) is False
        assert d.stale_widget_workers() == 1
        status = d.status()
        assert status["stale_widget_workers"] == 1
        assert status["required_widget_build"] == "9.9.9"
        # And it must not be counted as capacity that will ever do work.
        assert d.worker_free_for_claim(workers["stale"]) is False
    finally:
        module._get_required_widget_build = original
