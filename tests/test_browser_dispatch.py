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
    JOB_FAILED,
    JOB_FINALIZING,
    JOB_LEASED,
    JOB_QUEUED,
    JOB_RETRYABLE,
    JOB_START_PREPARED,
    JOB_STARTED,
    MAX_ACTIVE_WORKERS,
    POST_START_RECOVERY_GRACE_SECONDS,
    TERMINAL_ACK_WINDOW_SECONDS,
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
    # Compare against the SHORTENED deadline, not the original one: two
    # `now + LEASE_SECONDS` values computed inside one clock tick are equal on
    # Windows, and a strict `>` against the original made this test flaky.
    shortened = time.time() + 1
    lease.lease_expires_at = shortened
    d.register_worker(worker("w1", state="AUDITING", dispatch_id=item.dispatch_id, lease_id=lease.lease_id))
    assert d.get_job(item.dispatch_id).lease_expires_at > shortened + 60


def test_a_silently_dropped_prestart_claim_is_not_renewed_forever(tmp_path):
    """A widget that claims a job and then drops it reports no lease at all.

    Renewing on worker identity alone kept such a job LEASED forever: the owner
    stayed registered so `expire_leases` never saw `owner_gone`, and the
    renewal kept pushing the deadline out. The project sat on a dead lease
    while clean workers idled beside it.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    d.claim_job("w1")

    # The worker keeps polling, but no longer reports the dispatch.
    assert d.renew_owner_lease("w1") is None
    assert d.renew_owner_lease("w1", "dsp-someone-else") is None

    d.get_job(item.dispatch_id).lease_expires_at = time.time() - 1
    d.expire_leases()
    assert d.get_job(item.dispatch_id).state == JOB_QUEUED
    assert d.claim_job("w1") is not None


def test_a_worker_still_holding_its_prestart_claim_keeps_the_lease(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    d.claim_job("w1")

    shortened = time.time() + 1
    d.get_job(item.dispatch_id).lease_expires_at = shortened
    assert d.renew_owner_lease("w1", item.dispatch_id) is not None
    assert d.get_job(item.dispatch_id).lease_expires_at > shortened + 60
    d.expire_leases()
    assert d.get_job(item.dispatch_id).state == JOB_LEASED


def test_a_post_start_run_is_renewed_even_without_an_echoed_dispatch(tmp_path):
    """Recovery must not depend on the widget echoing its lease after START."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(
            item.dispatch_id, "w1", lease.lease_id, state,
            {"campaign_run_id": "run", "start_receipt": "receipt"},
        )

    shortened = time.time() + 1
    d.get_job(item.dispatch_id).lease_expires_at = shortened
    assert d.renew_owner_lease("w1") is not None
    assert d.get_job(item.dispatch_id).lease_expires_at > shortened + 60


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
    # An occupied tab in the operator's own browser can never claim, so it
    # stays visible as foreign but no longer holds one of the six audit lanes.
    assert st["active_workers"] == 1
    assert st["foreign_workers"] == 1
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
    defaults = {
        "widget_version": "AUDAPACK_WIDGET/3",
        "is_chromium": True,
        "is_brave": False,
        "page_eligible": True,
        "clean_for_audit": True,
        "has_conversation_turns": False,
        "url_path": "/",
    }
    defaults.update(overrides)
    return worker(wid, **defaults)


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
        # Reported, not refused: the protocol is the compatibility boundary, and
        # a pool that stops working until a human clicks Install in Tampermonkey
        # is a worse failure than running one release behind.
        assert d.worker_free_for_claim(workers["stale"]) is True
        assert d.worker_consumes_lane(workers["stale"]) is True
    finally:
        module._get_required_widget_build = original


def test_a_refused_reconcile_never_unregisters_the_window(tmp_path):
    """One stuck dispatch must not cost the pool a lane.

    A refused reconcile propagated out of register_worker, so the window
    stopped registering at all: invisible to the pool, unable to recycle, its
    lane gone for good over a single blocked job.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("managed-1", managed_slot=1, managed_generation=1))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("managed-1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "managed-1", lease.lease_id, state,
                         {"campaign_run_id": "run-real", "start_receipt": "receipt-real"})
    # A Bridge restart blocks the live post-START run pending reconciliation.
    lose_worker(d, "managed-1")
    d.expire_leases()
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_STARTED
    job.error = "Bridge restarted after START_PREPARED; same-worker reconciliation required"

    # The window comes back carrying a START receipt that is not this run's:
    # a genuine irreversibility conflict, and the one thing reconcile refuses.
    record = d.register_worker(supported_worker(
        "managed-1", managed_slot=1, managed_generation=1,
        dispatch_id=item.dispatch_id, lease_id=lease.lease_id,
        campaign_run_id="run-real", start_receipt="receipt-from-another-run",
    ))
    assert record.worker_id == "managed-1"
    assert "managed-1" in {w.worker_id for w in d.list_workers()}
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED
    assert "start_receipt_conflict" in record.meta.get("last_reconcile_error", "")


def test_lane_reservation_refuses_cleanly_while_jobs_exist(tmp_path):
    """The refusal must be a DispatchError, not a crash.

    The admission check called _worker_owns_live_job on a worker that is by
    definition not registered yet. With an empty queue the generator never
    touched the None, so every test passed while the live Bridge raised
    AttributeError and closed the connection on any unmanaged registration.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    for slot in range(1, MAX_ACTIVE_WORKERS + 1):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))
    item = d.enqueue_job(job_payload(path))
    d.claim_job("managed-1")
    assert d.get_job(item.dispatch_id).state == JOB_LEASED

    with pytest.raises(DispatchError) as refused:
        d.register_worker(supported_worker("personal"))
    assert refused.value.code == "worker_limit"


def test_a_lost_auditing_ack_never_blocks_a_finished_audit(tmp_path):
    """STARTED must be able to finish.

    AUDITING is a progress marker, not a boundary -- the irreversible Send
    already happened at START_PREPARED. Its ACK is one HTTP call among six
    windows sharing one serialized userscript request queue, and when it was
    lost the run was pinned in STARTED, from which FINALIZING and COMPLETE were
    both illegal. Observed live: four of six dispatches sat STARTED while every
    worker reported AUDITING in a real conversation.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})
    assert d.get_job(item.dispatch_id).state == JOB_STARTED

    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_FINALIZING, {"campaign_run_id": "run"})
    assert d.get_job(item.dispatch_id).state == JOB_FINALIZING


def test_a_started_run_can_complete_directly(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})
    d.transition_job(item.dispatch_id, "w1", lease.lease_id, JOB_COMPLETE, {"campaign_run_id": "run"})
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE


def test_recovery_survives_a_runtime_that_re_derived_its_run_id(tmp_path):
    """The lease is the ownership proof, not an echoed campaign run id.

    ChatGPT route hydration re-arms the widget's runtime and re-derives the
    run id. Demanding the worker echo the original back refused recovery
    permanently: observed on five concurrent runs at once, every one refused
    with run_id_conflict while the audit kept going in the browser.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("managed-1", managed_slot=1, managed_generation=1))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("managed-1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "managed-1", lease.lease_id, state,
                         {"campaign_run_id": "acb-original", "start_receipt": "receipt-1"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_AUDITING
    job.error = "Bridge restarted after START_PREPARED; same-worker reconciliation required"

    record = d.register_worker(supported_worker(
        "managed-1", managed_slot=1, managed_generation=1,
        dispatch_id=item.dispatch_id, lease_id=lease.lease_id,
        campaign_run_id="acb-rederived-by-hydration", start_receipt="receipt-1",
    ))
    restored = d.get_job(item.dispatch_id)
    assert restored.state == JOB_AUDITING
    assert restored.campaign_run_id == "acb-original", "the job's own run id stays authoritative"
    assert restored.meta_run_id_drift == "acb-rederived-by-hydration"
    assert record.meta.get("last_reconcile_error", "") == ""


def test_a_finished_campaign_completes_a_run_whose_ack_never_landed(tmp_path):
    """Durable campaign evidence outranks a missing terminal ACK.

    Observed live: two campaigns complete on disk -- 3/3 waves and a valid
    canonical handoff -- with their dispatches still reporting AUDITING 0/3
    and never becoming READY, because reconciliation only ever looked at
    BLOCKED jobs and these had recovered successfully.
    """
    import json as _json

    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-done", "start_receipt": "receipt"})
    assert d.get_job(item.dispatch_id).state == JOB_AUDITING

    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    (campaign_dir / "campaign.json").write_text(_json.dumps({
        "campaign_run_id": "acb-done",
        "campaign_status": "COMPLETE",
        "profile_id": "quick3",
        "wave_count": 3,
        "completed_count": 3,
    }), encoding="utf-8")
    d._resolved_campaign_path = lambda job: campaign_dir / "campaign.json"

    assert d.reconcile_completed_blocked_runs() == 1
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE


def test_writing_the_final_handoff_closes_the_lane(tmp_path):
    """The durable handoff IS the finish, ACK or no ACK.

    Two campaigns were observed complete on disk -- 3/3 waves and a valid
    canonical handoff -- with their lanes still reporting AUDITING and never
    becoming READY, because the terminal ACK never arrived.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "TERMISAI"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})

    handoff = tmp_path / "TERMISAI__00_AUDIT_ALL_3.md"
    handoff.write_text("final", encoding="utf-8")
    closed = d.complete_runs_for_project("termisai", "TERMISAI", str(handoff), "deadbeef")

    assert closed == 1
    job = d.get_job(item.dispatch_id)
    assert job.state == JOB_COMPLETE
    assert job.final_handoff_path == str(handoff)
    assert job.final_handoff_sha256 == "deadbeef"


def test_completing_a_project_never_touches_a_pre_start_job(tmp_path):
    """Nothing before START_PREPARED has an audit to be finished."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "TERMISAI"))
    d.claim_job("w1")
    assert d.get_job(item.dispatch_id).state == JOB_LEASED

    assert d.complete_runs_for_project("termisai", "TERMISAI", "/tmp/x.md", "abc") == 0
    assert d.get_job(item.dispatch_id).state == JOB_LEASED


def test_completing_a_project_never_touches_another_project(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "TERMISAI"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})

    assert d.complete_runs_for_project("wintage", "Wintage", "/tmp/x.md", "abc") == 0
    assert d.get_job(item.dispatch_id).state == JOB_STARTED


def test_completion_binds_a_campaign_run_id_the_dispatch_never_saw(tmp_path):
    """The Bridge is the one witness that can bind the two run ids.

    ChatGPT route hydration re-derives the widget's run id, so the saved
    campaign can carry an id the dispatch never saw. Observed live: SAIWORK2
    finished with a matching handoff digest and stayed SAVING, one failed
    campaign_match away from READY.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "SAIWORK2"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-dispatch-saw-this", "start_receipt": "receipt"})

    handoff = tmp_path / "SAIWORK2__00_AUDIT_ALL_3.md"
    handoff.write_text("final", encoding="utf-8")
    d.complete_runs_for_project(
        "saiwork2", "SAIWORK2", str(handoff), "abc123", "acb-campaign-was-saved-as",
    )

    job = d.get_job(item.dispatch_id)
    assert job.state == JOB_COMPLETE
    assert job.campaign_run_id == "acb-dispatch-saw-this", "the dispatch's own id is never overwritten"
    assert job.meta_run_id_drift == "acb-campaign-was-saved-as"


def test_a_campaign_finished_while_the_lane_was_blocked_still_closes(tmp_path):
    """The same conclusion, reached late.

    Wintage and TERMISAI finished 3/3 waves with valid canonical handoffs on
    disk while their lanes were blocked by a Bridge restart. The finalization
    event had already passed, so nothing closed them: the dispatcher cannot
    resolve campaign.json itself (wrong directory, and a run id the widget may
    have re-derived), so the Bridge lends it the answer.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-dispatch", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_AUDITING
    job.error = "Bridge restarted after START_PREPARED; same-worker reconciliation required"

    handoff = tmp_path / "WINTAGE__00_AUDIT_ALL_3.md"
    handoff.write_text("final", encoding="utf-8")
    d.set_campaign_probe(lambda pid, name: {
        "complete": True,
        "handoff_path": str(handoff),
        "handoff_sha256": "cafebabe",
        "campaign_run_id": "acb-saved-under-another-id",
    })

    assert d.reconcile_finished_campaigns() == 1
    closed = d.get_job(item.dispatch_id)
    assert closed.state == JOB_COMPLETE
    assert closed.final_handoff_path == str(handoff)
    assert closed.meta_run_id_drift == "acb-saved-under-another-id"


def test_a_run_whose_window_never_returns_stops_claiming_to_be_recoverable(tmp_path):
    """A managed worker id lives in its window's sessionStorage.

    A closed window takes its identity with it, so a relaunched slot registers
    as somebody else and no recovery is possible. Observed live: SAIPET blocked
    on "worker lost after START_PREPARED; recovery required" with its slot
    empty, and nothing in the system ever ended that wait.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "SAIPET"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_STARTED
    job.error = "worker lost after START_PREPARED; recovery required"

    # The window is gone from the registry, but not yet for long enough.
    d._workers.pop("w1", None)
    assert d.expire_unrecoverable_runs() == 0
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED

    job.updated_at = time.time() - (POST_START_RECOVERY_GRACE_SECONDS + 1)
    assert d.expire_unrecoverable_runs() == 1
    closed = d.get_job(item.dispatch_id)
    assert closed.state == JOB_FAILED
    assert closed.last_error_code == "worker_window_gone"
    assert closed.recovery_state == ""


def test_an_absent_worker_is_not_proof_the_run_is_dead(tmp_path):
    """The browser keeps auditing while the Bridge cannot see its worker.

    Observed live: SAIPET blocked at 02:06 with an empty managed slot and wrote
    its finished 3-wave handoff at 02:40. A grace shorter than an audit would
    have stamped FAILED on a run that succeeded -- the same lie as closing a
    lane against yesterday's artifact, pointed the other way.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "SAIPET"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_STARTED
    d._workers.pop("w1", None)

    # 34 minutes with no worker in sight -- exactly the live SAIPET window.
    job.updated_at = time.time() - 34 * 60
    assert d.expire_unrecoverable_runs() == 0
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED
    # An hour: still shorter than a super10 campaign.
    job.updated_at = time.time() - 3600
    assert d.expire_unrecoverable_runs() == 0
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED


def test_a_blocked_run_whose_window_is_still_registered_keeps_waiting(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "SAIPET"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})
    job = d.get_job(item.dispatch_id)
    job.state = JOB_BLOCKED
    job.recovery_state = JOB_STARTED
    job.updated_at = time.time() - 3600

    assert d.expire_unrecoverable_runs() == 0
    assert d.get_job(item.dispatch_id).state == JOB_BLOCKED


def test_a_pre_start_job_is_never_failed_as_unrecoverable(tmp_path):
    """Nothing before START_PREPARED has an audit to lose: it requeues."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "SAIPET"))
    d.claim_job("w1")
    d._workers.pop("w1", None)
    d.get_job(item.dispatch_id).updated_at = time.time() - 3600

    assert d.expire_unrecoverable_runs() == 0
    assert d.get_job(item.dispatch_id).state == JOB_LEASED


def test_a_lane_closed_server_side_still_reaches_its_own_worker(tmp_path):
    """The window releases its lease only on a terminal ACK from the Bridge.

    Completion once happened only through the worker's own ACK, so it cleared
    the lease as it sent it. Lanes are now also closed server-side, and the
    window was never told: its lease stayed, browserWorkerRecycleBlockReason()
    answered `lease-still-owned` forever, and the window never returned to the
    clean pool. Observed live: five managed windows all reporting AUDITING
    with not one live job in the dispatcher, and a queued audit starving in
    front of them.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})

    # The Bridge writes the handoff and closes the lane; no worker ACK at all.
    assert d.complete_runs_for_project("wintage", "WINTAGE", "/final.md", "abc") == 1

    owned = d.get_owned_job("w1")
    assert owned is not None, "the window must be able to see that its run is over"
    assert owned.state == JOB_COMPLETE
    # And it is never mistaken for live work.
    assert d._worker_owns_live_job(d._workers["w1"]) is False


def test_a_long_finished_dispatch_stops_being_reported_as_owned(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-run", "start_receipt": "receipt"})
    d.complete_runs_for_project("wintage", "WINTAGE", "/final.md", "abc")

    job = d.get_job(item.dispatch_id)
    job.completed_at = time.time() - (TERMINAL_ACK_WINDOW_SECONDS + 1)
    assert d.get_owned_job("w1") is None


def test_a_profiled_job_waits_briefly_for_a_window_already_on_that_profile(tmp_path):
    """Switching a window's profile resets its audit runtime.

    That reset is what sent a compress Core with no engine armed behind it and
    saved nothing, twice. When another free window is already on the requested
    profile, handing it there costs nothing and skips the reset entirely.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w-quick", profile="quick3"))
    d.register_worker(worker("w-compress", profile="compress"))
    item = d.enqueue_job(dict(job_payload(path, "CMPROJ"), profile="compress"))

    assert d.claim_job("w-quick") is None, "the quick3 window must not grab it first"
    leased = d.claim_job("w-compress")
    assert leased is not None and leased.dispatch_id == item.dispatch_id


def test_a_profiled_job_is_never_starved_by_affinity(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w-quick", profile="quick3"))
    d.register_worker(worker("w-compress", profile="compress"))
    item = d.enqueue_job(dict(job_payload(path, "CMPROJ"), profile="compress"))

    assert d.claim_job("w-quick") is None
    # The matching window went away without ever polling.
    d.get_job(item.dispatch_id).created_at = time.time() - 61
    leased = d.claim_job("w-quick")
    assert leased is not None and leased.dispatch_id == item.dispatch_id


def test_affinity_does_not_hold_a_job_for_a_busy_window(tmp_path):
    """Only a window that could actually claim counts as the better home."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w-quick", profile="quick3"))
    d.register_worker(worker("w-compress", profile="compress", has_conversation_turns=True, generating=True))
    item = d.enqueue_job(dict(job_payload(path, "CMPROJ"), profile="compress"))

    leased = d.claim_job("w-quick")
    assert leased is not None and leased.dispatch_id == item.dispatch_id


def test_a_job_with_no_profile_is_taken_by_anyone(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(worker("w-quick", profile="quick3"))
    d.register_worker(worker("w-compress", profile="compress"))
    item = d.enqueue_job(job_payload(path, "ANYPROJ"))

    leased = d.claim_job("w-quick")
    assert leased is not None and leased.dispatch_id == item.dispatch_id


def _stale_widget_worker(wid: str, slot: int = 1) -> dict:
    return worker(
        wid,
        widget_version="AUDAPACK_WIDGET/3",
        widget_protocol="AUDAPACK_WIDGET/3",
        widget_build_version="0.0.1",
        managed_slot=slot,
        managed_generation=1,
    )


def test_a_stale_window_is_asked_to_reload_once_per_build(tmp_path, monkeypatch):
    """A reload only re-runs the script the manager already holds.

    A window whose userscript manager has nothing newer comes back on the same
    build and is asked again on the next poll. Observed live: free windows
    reloading every two minutes forever, dropping out of the registry each
    time, and a run stranded when one of those reloads landed on the window
    that had just taken a job. The client stopped asking twice in 0.0.37,
    which is no help to the builds that need telling -- the Bridge has to.
    """
    import audapack.bridge.browser_dispatch as bd

    monkeypatch.setattr(bd, "_get_required_widget_build", lambda: "0.0.9")
    d = dispatcher(tmp_path)
    record = d.register_worker(_stale_widget_worker("w1", slot=1))

    assert d.worker_widget_is_stale(record) is True
    assert d.should_ask_widget_reload(record) is True
    assert d.should_ask_widget_reload(record) is False

    # autoTabId lives in sessionStorage and survives the reload, so the window
    # that came back IS this worker: asking it again is the identical futile
    # loop. Status still tells the operator the truth about the build.
    reloaded = d.register_worker(_stale_widget_worker("w1", slot=1))
    assert d.should_ask_widget_reload(reloaded) is False
    assert d.worker_widget_is_stale(reloaded) is True


def test_a_new_window_in_the_same_slot_gets_its_own_ask(tmp_path, monkeypatch):
    """A refusal must not outlive the window that made it.

    Keying the ask by managed slot made one refusal permanent: a window that
    declined because it was mid-recycle was never asked again, and a brand new
    window opened into that slot inherited the refusal. Observed live: four
    free windows stuck on 0.0.37 for forty minutes with the Bridge requiring
    0.0.38 and not one reload request sent.
    """
    import audapack.bridge.browser_dispatch as bd

    monkeypatch.setattr(bd, "_get_required_widget_build", lambda: "0.0.9")
    d = dispatcher(tmp_path)
    first = d.register_worker(_stale_widget_worker("w1-session-a", slot=1))
    assert d.should_ask_widget_reload(first) is True
    assert d.should_ask_widget_reload(first) is False

    # That window was closed and the slot relaunched: a new session, a new id.
    fresh = d.register_worker(_stale_widget_worker("w1-session-b", slot=1))
    assert d.should_ask_widget_reload(fresh) is True


def test_a_different_slot_still_gets_its_one_ask(tmp_path, monkeypatch):
    import audapack.bridge.browser_dispatch as bd

    monkeypatch.setattr(bd, "_get_required_widget_build", lambda: "0.0.9")
    d = dispatcher(tmp_path)
    first = d.register_worker(_stale_widget_worker("w1", slot=1))
    second = d.register_worker(_stale_widget_worker("w2", slot=2))

    assert d.should_ask_widget_reload(first) is True
    assert d.should_ask_widget_reload(second) is True


def test_a_newer_required_build_earns_a_fresh_ask(tmp_path, monkeypatch):
    import audapack.bridge.browser_dispatch as bd

    build = {"value": "0.0.9"}
    monkeypatch.setattr(bd, "_get_required_widget_build", lambda: build["value"])
    d = dispatcher(tmp_path)
    record = d.register_worker(_stale_widget_worker("w1", slot=1))

    assert d.should_ask_widget_reload(record) is True
    assert d.should_ask_widget_reload(record) is False
    build["value"] = "0.1.0"
    assert d.should_ask_widget_reload(record) is True


def test_a_current_window_is_never_asked_to_reload(tmp_path, monkeypatch):
    import audapack.bridge.browser_dispatch as bd

    monkeypatch.setattr(bd, "_get_required_widget_build", lambda: "0.0.9")
    d = dispatcher(tmp_path)
    record = d.register_worker(worker(
        "w1", widget_version="AUDAPACK_WIDGET/3", widget_protocol="AUDAPACK_WIDGET/3",
        widget_build_version="0.0.9", managed_slot=1, managed_generation=1,
    ))
    assert d.worker_widget_is_stale(record) is False
    assert d.should_ask_widget_reload(record) is False


def test_a_six_worker_status_reads_the_userscript_once(tmp_path, monkeypatch):
    """The required build is a release marker, not something to re-derive.

    `_get_required_widget_build()` read and regex-scanned the whole 841 KB
    bundled userscript on every call, and status() calls it once per live
    worker through `worker_widget_is_stale()` plus once for its own field: a
    six-worker status did seven full reads, a poll response nine, every four
    seconds, to learn a version that changes when the operator upgrades the
    widget and at no other time.
    """
    from audapack.components import widget as widget_mod
    from audapack.components.widget import WIDGET_FILE_NAME

    # Defensive, not the assertion: this test must fail on the READ COUNT, so
    # it still runs against a module that has no cache at all.
    getattr(widget_mod, "_WIDGET_METADATA_CACHE", {}).clear()
    reads = {"count": 0}
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == WIDGET_FILE_NAME:
            reads["count"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    d = dispatcher(tmp_path)
    for slot in range(1, 7):
        d.register_worker(supported_worker(
            f"w{slot}", managed_slot=slot, managed_generation=1,
            widget_protocol="AUDAPACK_WIDGET/3", widget_build_version="9.9.9",
        ))

    first = d.status()
    d.status()
    d.status()

    assert first["active_workers"] == 6
    assert first["required_widget_build"]
    assert reads["count"] == 1


def test_a_campaign_finished_before_this_dispatch_started_closes_nothing(tmp_path):
    """Every project audited even once keeps a complete campaign on disk.

    "This project has a finished audit" is therefore true forever, and closing
    a lane on it closes any lane at all. Observed live: six fresh dispatches
    went COMPLETE within a minute of START against handoff files written the
    previous day. The audits never ran, and the board said they had.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-dispatch", "start_receipt": "receipt"})

    stale = tmp_path / "WINTAGE__00_AUDIT_ALL_3.md"
    stale.write_text("yesterday's audit", encoding="utf-8")
    d.set_campaign_probe(lambda pid, name: {
        "complete": True,
        "handoff_path": str(stale),
        "handoff_sha256": "cafebabe",
        "campaign_run_id": "acb-yesterday",
        "handoff_written_at": time.time() - 9 * 3600,
    })

    assert d.reconcile_finished_campaigns() == 0
    assert d.get_job(item.dispatch_id).state == JOB_AUDITING


def test_a_campaign_finished_after_this_dispatch_started_closes_its_lane(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-dispatch", "start_receipt": "receipt"})

    fresh = tmp_path / "WINTAGE__00_AUDIT_ALL_3.md"
    fresh.write_text("this run's audit", encoding="utf-8")
    d.set_campaign_probe(lambda pid, name: {
        "complete": True,
        "handoff_path": str(fresh),
        "handoff_sha256": "cafebabe",
        "campaign_run_id": "acb-saved-under-another-id",
        "handoff_written_at": time.time() + 1,
    })

    assert d.reconcile_finished_campaigns() == 1
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE


def test_the_finalization_path_closes_without_a_timestamp(tmp_path):
    """The Bridge writing the handoff IS the freshness proof."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-dispatch", "start_receipt": "receipt"})

    assert d.complete_runs_for_project("wintage", "WINTAGE", "/final.md", "abc") == 1
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE


def test_an_unfinished_campaign_leaves_its_lane_alone(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "WINTAGE"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "acb-dispatch", "start_receipt": "receipt"})

    d.set_campaign_probe(lambda pid, name: {"complete": False})
    assert d.reconcile_finished_campaigns() == 0
    assert d.get_job(item.dispatch_id).state == JOB_AUDITING


def test_a_dirty_personal_tab_does_not_hold_an_audit_lane(tmp_path):
    """Observed live: act 6 / free 5, with the sixth lane unusable.

    A tab in the operator's own browser, eligible but not clean and owning no
    run, can never claim anything -- yet it sat on one of the six lanes while a
    managed window had nowhere to register.
    """
    d = dispatcher(tmp_path)
    for slot in range(1, MAX_ACTIVE_WORKERS):
        d.register_worker(supported_worker(f"managed-{slot}", managed_slot=slot, managed_generation=1))
    d.register_worker(supported_worker("personal", clean_for_audit=False))

    status = d.status()
    assert status["active_workers"] == MAX_ACTIVE_WORKERS - 1
    assert status["foreign_workers"] == 1

    # ...so the sixth managed window still has a lane to register into.
    d.register_worker(supported_worker("managed-6", managed_slot=6, managed_generation=1))
    assert d.status()["active_workers"] == MAX_ACTIVE_WORKERS


def test_a_managed_slot_keeps_its_lane_while_it_settles(tmp_path):
    """A managed window is the pool; it holds its lane even while dirty."""
    d = dispatcher(tmp_path)
    d.register_worker(supported_worker("managed-1", managed_slot=1, managed_generation=1, clean_for_audit=False))
    assert d.status()["active_workers"] == 1


def test_the_operator_can_abandon_a_live_post_start_run(tmp_path):
    """A run whose worker window was closed had no escape hatch.

    Cancel refuses a post-start dispatch on principle, and abandon accepted
    only BLOCKED, so an AUDITING job with nobody behind it could not be
    cleared and START AUDIT kept answering "already active".
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})

    abandoned = d.abandon_job(item.dispatch_id, "worker window was closed")
    assert abandoned.state == JOB_FAILED
    assert abandoned.campaign_run_id == "run", "post-start lineage is kept, not erased"
    assert d.claim_job("w1") is None, "an abandoned run is never re-leased"


def test_a_pre_start_job_is_still_cancelled_not_abandoned(tmp_path):
    """Abandon asserts a Core may have been sent; before START that is a lie."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path))
    d.claim_job("w1")

    with pytest.raises(DispatchError) as refused:
        d.abandon_job(item.dispatch_id, "too early")
    assert refused.value.code == "invalid_transition"
    assert d.cancel_job(item.dispatch_id)


def test_an_incompatible_widget_protocol_is_still_refused(tmp_path):
    """The build warns; the protocol decides."""
    d = dispatcher(tmp_path)
    for index, version in enumerate(("AUDAPACK_WIDGET", "AUDAPACK_WIDGET/2")):
        d.register_worker(supported_worker(f"legacy-{index}", widget_version=version))
    for worker_record in d.list_workers():
        assert d.worker_free_for_claim(worker_record) is False
        assert d.worker_consumes_lane(worker_record) is False


def test_a_widget_release_does_not_take_the_pool_offline(tmp_path):
    """Every build bump used to stop all auditing until someone clicked Install."""
    import audapack.bridge.browser_dispatch as module

    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker(
        "managed-1", managed_slot=1, managed_generation=1,
        widget_protocol="AUDAPACK_WIDGET/3", widget_build_version="0.0.34",
    ))
    item = d.enqueue_job(job_payload(path))

    original = module._get_required_widget_build
    module._get_required_widget_build = lambda: "0.0.35"
    try:
        assert d.status()["stale_widget_workers"] == 1, "the operator must still be told"
        claimed = d.claim_job("managed-1")
        assert claimed is not None and claimed.dispatch_id == item.dispatch_id
    finally:
        module._get_required_widget_build = original


def test_a_lease_for_a_purged_dispatch_does_not_hold_a_lane(tmp_path):
    """A phantom lease ate one of the six lanes, so six windows ran five audits.

    get_owned_job() answers None for a job the Bridge no longer has, so the
    window has nothing to ACK against and keeps reporting RESERVED with its
    dead lease forever. Live: a Brave worker stuck on
    ``unknown_job: dispatch_id is unknown`` since the morning, counted in
    active_workers, and every six-project press dispatching only five.
    """
    d = BrowserDispatcher(state_dir=tmp_path)
    record = d.register_worker(worker(
        "ghost",
        state="RESERVED",
        dispatch_id="dsp-does-not-exist",
        lease_id="lease-does-not-exist",
    ))

    assert record.state == "FREE"
    assert record.meta.get("reports_lease") is False
    assert "unknown_job" in record.meta.get("last_reconcile_error", "")
    assert d._worker_owns_live_job(record) is False


def test_any_chromium_browser_can_claim_by_default(tmp_path):
    """The historical pool is deliberately wide; narrowing it silently would
    strand a working setup."""
    d = BrowserDispatcher(state_dir=tmp_path)
    record = d.register_worker(worker(
        "brave-tab", is_chromium=True, is_brave=True, page_eligible=True,
        url_path="/", browser_name="Brave", clean_for_audit=True,
        widget_version="AUDAPACK_WIDGET/3", managed_profile=False,
    ))
    assert d.worker_in_allowed_profile(record) is True
    assert d.worker_free_for_claim(record) is True


def test_confining_the_pool_keeps_a_personal_browser_out(tmp_path):
    """A Brave tab registered, counted toward the six lanes and claimed a real
    audit. With the pool confined it may still register and never claim."""
    d = BrowserDispatcher(state_dir=tmp_path, dedicated_profile_only=True)
    record = d.register_worker(worker(
        "brave-tab", is_chromium=True, is_brave=True, page_eligible=True,
        url_path="/", browser_name="Brave", clean_for_audit=True,
        widget_version="AUDAPACK_WIDGET/3", managed_profile=False,
    ))
    assert d.worker_in_allowed_profile(record) is False
    assert d.worker_free_for_claim(record) is False


def test_a_dedicated_window_with_no_slot_still_claims_when_confined(tmp_path):
    """Launch Chromium opens the dedicated profile with no slot params at all,
    so slot 0 there is normal and must not be mistaken for a foreign browser."""
    d = BrowserDispatcher(state_dir=tmp_path, dedicated_profile_only=True)
    record = d.register_worker(worker(
        "dedicated", is_chromium=True, page_eligible=True, url_path="/",
        browser_name="Chrome", clean_for_audit=True,
        widget_version="AUDAPACK_WIDGET/3", managed_profile=True, managed_slot=0,
    ))
    assert d.worker_free_for_claim(record) is True


def test_a_widget_too_old_to_answer_is_never_locked_out(tmp_path):
    """Unknown is allowed. Locking out a pool over a missing field is a worse
    failure than one stray browser claiming a job."""
    d = BrowserDispatcher(state_dir=tmp_path, dedicated_profile_only=True)
    record = d.register_worker(worker(
        "old-widget", is_chromium=True, page_eligible=True, url_path="/",
        browser_name="Chrome", clean_for_audit=True,
        widget_version="AUDAPACK_WIDGET/3",
    ))
    assert record.managed_profile is None
    assert d.worker_free_for_claim(record) is True


def test_a_numbered_slot_is_proof_of_the_dedicated_profile(tmp_path):
    """Slots are only ever handed to a launched worker window."""
    d = BrowserDispatcher(state_dir=tmp_path, dedicated_profile_only=True)
    record = d.register_worker(worker(
        "slot-3", is_chromium=True, page_eligible=True, url_path="/",
        browser_name="Chrome", clean_for_audit=True,
        widget_version="AUDAPACK_WIDGET/3", managed_slot=3, managed_generation=1,
    ))
    assert record.managed_profile is True
    assert d.worker_free_for_claim(record) is True


# --------------------------------------------------------------- queue order
#
# The pool already holds more jobs than there are windows, and a window that
# frees up claims the next one instead of a new window being opened. What was
# missing was any say in WHICH one: the line was strictly the order START was
# pressed in.


def free_worker(wid: str, **overrides) -> dict:
    return worker(
        wid, widget_version="AUDAPACK_WIDGET/3", is_chromium=True, page_eligible=True,
        url_path="/", browser_name="Chrome", clean_for_audit=True,
        has_conversation_turns=False, **overrides,
    )


def queued_line(d: BrowserDispatcher) -> list[str]:
    return [job.project_name for job in d.queued_jobs_in_order()]


def three_queued(tmp_path) -> tuple[BrowserDispatcher, dict]:
    d = dispatcher(tmp_path)
    jobs = {}
    for name in ("A", "B", "C"):
        path = archive(tmp_path, f"{name}.zip")
        jobs[name] = d.enqueue_job(job_payload(path, name))
        # Distinct creation stamps: the default line is FIFO by age.
        time.sleep(0.01)
    return d, jobs


def test_the_line_is_fifo_until_somebody_reorders_it(tmp_path):
    d, _jobs = three_queued(tmp_path)
    assert queued_line(d) == ["A", "B", "C"]


def test_a_waiting_job_can_be_moved_up_the_line(tmp_path):
    d, jobs = three_queued(tmp_path)
    assert d.reorder_job(jobs["C"].dispatch_id, -1) == [
        jobs["A"].dispatch_id, jobs["C"].dispatch_id, jobs["B"].dispatch_id
    ]
    assert queued_line(d) == ["A", "C", "B"]


def test_the_freed_window_takes_whatever_is_now_first(tmp_path):
    """The whole point: reordering decides who gets the next window."""
    d, jobs = three_queued(tmp_path)
    d.reorder_job(jobs["C"].dispatch_id, -1)
    d.reorder_job(jobs["C"].dispatch_id, -1)
    d.register_worker(free_worker("w1"))
    assert d.claim_job("w1").dispatch_id == jobs["C"].dispatch_id


def test_a_move_off_either_end_is_a_no_op_not_an_error(tmp_path):
    """Or the button at the top row throws instead of doing nothing."""
    d, jobs = three_queued(tmp_path)
    assert queued_line(d) == ["A", "B", "C"]
    d.reorder_job(jobs["A"].dispatch_id, -1)
    d.reorder_job(jobs["C"].dispatch_id, +1)
    assert queued_line(d) == ["A", "B", "C"]


def test_a_bigger_step_moves_rather_than_swaps(tmp_path):
    """"Up two" means two places up, leaving what it passed in order.

    A swap would send the job it jumped over to the BACK of the line, which is
    not what an arrow means and not what a drag would do either.
    """
    d, jobs = three_queued(tmp_path)
    assert d.reorder_job(jobs["C"].dispatch_id, -2) == [
        jobs["C"].dispatch_id, jobs["A"].dispatch_id, jobs["B"].dispatch_id
    ]
    assert queued_line(d) == ["C", "A", "B"]


def test_a_step_past_the_end_is_clamped_not_refused(tmp_path):
    """Holding the arrow at the end settles instead of erroring."""
    d, jobs = three_queued(tmp_path)
    d.reorder_job(jobs["C"].dispatch_id, -99)
    assert queued_line(d) == ["C", "A", "B"]
    d.reorder_job(jobs["C"].dispatch_id, +99)
    assert queued_line(d) == ["A", "B", "C"]


def test_a_job_that_already_has_a_window_cannot_be_reordered(tmp_path):
    """It has a window. Moving it in the line would mean taking that away."""
    d, jobs = three_queued(tmp_path)
    d.register_worker(free_worker("w1"))
    leased = d.claim_job("w1")
    with pytest.raises(DispatchError) as excinfo:
        d.reorder_job(leased.dispatch_id, +1)
    assert excinfo.value.code == "not_waiting"


def test_reordering_an_unknown_dispatch_says_so(tmp_path):
    d, _jobs = three_queued(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        d.reorder_job("dsp-nope", -1)
    assert excinfo.value.code == "unknown_dispatch"


def test_a_new_job_joins_the_back_even_after_a_reorder(tmp_path):
    """Reordering swaps two places; it must not make the line unstable."""
    d, jobs = three_queued(tmp_path)
    d.reorder_job(jobs["C"].dispatch_id, -1)
    time.sleep(0.01)
    d.enqueue_job(job_payload(archive(tmp_path, "D.zip"), "D"))
    assert queued_line(d) == ["A", "C", "B", "D"]


def test_the_order_survives_a_restart(tmp_path):
    d, jobs = three_queued(tmp_path)
    d.reorder_job(jobs["C"].dispatch_id, -1)
    reopened = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    assert queued_line(reopened) == ["A", "C", "B"]


def test_a_job_written_before_the_field_existed_keeps_its_place(tmp_path):
    """queue_order 0 means "never reordered", not "first in the line"."""
    d, _jobs = three_queued(tmp_path)
    for job in d._jobs.values():
        job.queue_order = 0.0
    assert queued_line(d) == ["A", "B", "C"]


# ----------------------------------------------------- one slot, one window
#
# Seven windows for six lanes, observed live: slot 3 held two workers, one
# AUDITING and one FREE. The launcher decides vacancy from the worker registry
# (75s TTL, no exemption for a worker mid-audit) while the dispatcher remembers
# a managed slot for 150s -- so a window that went quiet for 76s looked vacant,
# got a second window opened on it, and registration would not evict the first
# because it was holding a live run. Nothing ever looked again.


def managed(wid: str, slot: int, **overrides) -> dict:
    return free_worker(wid, managed_slot=slot, managed_generation=1, **overrides)


def test_the_slot_memory_outlives_the_worker_registry(tmp_path):
    d = dispatcher(tmp_path)
    d.register_worker(managed("w_slot3", 3))
    assert 3 in d.status()["managed_slot_lanes"]

    # Quiet for longer than the worker TTL but inside the slot memory.
    d._workers["w_slot3"].last_seen_at = time.time() - (WORKER_TTL_SECONDS + 10)
    status = d.status()
    assert status["active_workers"] == 0, "the worker itself is gone"
    assert 3 in status["managed_slot_lanes"], "but the slot still has a window"


def test_a_duplicate_that_owns_nothing_is_retired(tmp_path):
    d = dispatcher(tmp_path)
    d.register_worker(managed("w_first", 3))
    d._workers["w_first"].campaign_run_id = "run-live"
    # A second window on the same slot: registration will not evict the first
    # while it holds a run, so both are live for a moment.
    d.register_worker(managed("w_second", 3))
    assert {w.worker_id for w in d.list_workers()} == {"w_first", "w_second"}

    # The run ends. The duplicate owns nothing now and must not outlive it.
    d._workers["w_first"].campaign_run_id = ""
    d.status()
    slots = [w.managed_slot for w in d.list_workers()]
    assert slots.count(3) == 1, f"slot 3 still doubled: {slots}"


def test_the_window_holding_the_run_is_the_one_kept(tmp_path):
    """Never drop a window with a run behind it -- that is the whole caution."""
    d = dispatcher(tmp_path)
    d.register_worker(managed("w_idle", 3))
    d.register_worker(managed("w_busy", 3))
    d._workers["w_busy"].campaign_run_id = "run-live"
    d.status()
    assert [w.worker_id for w in d.list_workers()] == ["w_busy"]


def test_two_windows_both_holding_runs_are_both_kept(tmp_path):
    """A bad state, but not one to resolve by killing somebody's audit."""
    d = dispatcher(tmp_path)
    d.register_worker(managed("w_a", 3))
    d._workers["w_a"].campaign_run_id = "run-a"
    d.register_worker(managed("w_b", 3))
    d._workers["w_b"].campaign_run_id = "run-b"
    d.status()
    assert len(d.list_workers()) == 2


# ------------------------------------------------- atomic writes leave nothing
#
# 11 orphaned temp files were found beside a live state dir, every one for the
# generation file and none for jobs.json -- the generation file being the one
# the GUI both watches and polls, so a reader holding it open is exactly when
# the replace fails on Windows. Nothing swept them.


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path):
    from unittest.mock import patch

    from audapack.bridge.browser_dispatch import _atomic_write_json

    target = tmp_path / "state.json"
    with patch.object(Path, "replace", side_effect=PermissionError("held open")):
        with pytest.raises(PermissionError):
            _atomic_write_json(target, {"a": 1})
    assert list(tmp_path.glob("state.json.tmp.*")) == []


def test_the_caller_still_sees_the_failure(tmp_path):
    """Cleaning up must not turn a failed write into a silent success."""
    from unittest.mock import patch

    from audapack.bridge.browser_dispatch import _atomic_write_json

    with patch.object(Path, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            _atomic_write_json(tmp_path / "state.json", {"a": 1})


def test_a_later_write_sweeps_an_older_orphan(tmp_path):
    import os

    from audapack.bridge.browser_dispatch import _TEMP_SWEEP_AGE_SECONDS, _atomic_write_json

    target = tmp_path / "state.json"
    orphan = tmp_path / "state.json.tmp.deadbe"
    orphan.write_text("{}", encoding="utf-8")
    old = time.time() - (_TEMP_SWEEP_AGE_SECONDS + 60)
    os.utime(orphan, (old, old))

    _atomic_write_json(target, {"a": 1})
    assert not orphan.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_the_sweep_never_touches_a_write_in_flight(tmp_path):
    """A fresh temp file may belong to another process writing right now."""
    from audapack.bridge.browser_dispatch import _atomic_write_json

    fresh = tmp_path / "state.json.tmp.abc123"
    fresh.write_text("{}", encoding="utf-8")
    _atomic_write_json(tmp_path / "state.json", {"a": 1})
    assert fresh.exists()


def test_a_display_name_never_closes_another_project_by_its_id(tmp_path):
    """W2-003 (audit/1.md): id and name are different identity domains.

    Both went into ONE set compared against a set holding each job's id AND
    name, so a project whose NAME equals another project's ID terminalized that
    other project's live audit, inherited its handoff as proof of completion and
    had drift written into its lineage. Measured on the pre-fix code: closing
    project_id=alpha/name=beta returned 2.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w-alpha"))
    d.register_worker(supported_worker("w-beta"))

    alpha = d.enqueue_job(dict(job_payload(path), project_id="alpha", project_name="beta"))
    beta = d.enqueue_job(dict(job_payload(path), project_id="beta", project_name="gamma"))
    for worker_id, item, run in (("w-alpha", alpha, "run-a"), ("w-beta", beta, "run-b")):
        lease = d.claim_job(worker_id)
        assert lease is not None and lease.dispatch_id == item.dispatch_id
        for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
            d.transition_job(item.dispatch_id, worker_id, lease.lease_id, state,
                             {"campaign_run_id": run, "start_receipt": f"receipt-{run}"})

    handoff = tmp_path / "alpha__00_AUDIT_ALL_3.md"
    handoff.write_text("final", encoding="utf-8")
    closed = d.complete_runs_for_project("alpha", "beta", str(handoff), "deadbeef", "run-a")

    assert closed == 1
    assert d.get_job(alpha.dispatch_id).state == JOB_COMPLETE
    survivor = d.get_job(beta.dispatch_id)
    assert survivor.state == JOB_AUDITING, "a live audit was closed by a name/id collision"
    assert survivor.final_handoff_path == ""
    assert survivor.meta_run_id_drift == ""


def test_the_reverse_collision_is_equally_refused(tmp_path):
    """Closing `beta` must not reach the job whose display NAME is beta."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w-alpha"))
    d.register_worker(supported_worker("w-beta"))

    alpha = d.enqueue_job(dict(job_payload(path), project_id="alpha", project_name="beta"))
    beta = d.enqueue_job(dict(job_payload(path), project_id="beta", project_name="gamma"))
    for worker_id, item in (("w-alpha", alpha), ("w-beta", beta)):
        lease = d.claim_job(worker_id)
        for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
            d.transition_job(item.dispatch_id, worker_id, lease.lease_id, state,
                             {"campaign_run_id": f"run-{worker_id}", "start_receipt": "receipt"})

    assert d.complete_runs_for_project("beta", "gamma", "/final.md", "abc") == 1
    assert d.get_job(beta.dispatch_id).state == JOB_COMPLETE
    assert d.get_job(alpha.dispatch_id).state == JOB_AUDITING


def test_a_legacy_job_with_no_id_is_still_reachable_by_name(tmp_path):
    """Old records predate the canonical id; the name is all they carry."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "LEGACY"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})
    d.get_job(item.dispatch_id).project_id = ""

    assert d.complete_runs_for_project("legacy", "LEGACY", "/final.md", "abc") == 1
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE


def test_the_id_match_is_case_folded_like_before(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(dict(job_payload(path), project_id="MixedCase", project_name="Mixed Case"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})

    assert d.complete_runs_for_project("MIXEDCASE", "Mixed Case", "/final.md", "abc") == 1
    assert d.get_job(item.dispatch_id).state == JOB_COMPLETE


def _queued(d: BrowserDispatcher, tmp_path: Path, name="PERSIST"):
    return d.enqueue_job(job_payload(archive(tmp_path, f"{name}.zip"), name))


def test_a_failed_jobs_write_leaves_memory_where_disk_is(tmp_path, monkeypatch):
    """W2-004 (audit/1.md): one failed write must not create two authorities.

    Mutators changed the live job objects and only then persisted, with nothing
    to undo. Measured on the pre-fix code: cancel_job raised OSError while the
    running process held CANCELLED and a dispatcher rebuilt from disk said
    QUEUED -- a state no restart could ever reproduce.
    """
    import audapack.bridge.browser_dispatch as bd

    d = dispatcher(tmp_path)
    item = _queued(d, tmp_path)
    assert d.get_job(item.dispatch_id).state == JOB_QUEUED

    real_write = bd._atomic_write_json

    def refuse_jobs(path, value):
        if Path(path).name == "jobs.json":
            raise OSError("injected jobs.json replace failure")
        return real_write(path, value)

    monkeypatch.setattr(bd, "_atomic_write_json", refuse_jobs)
    with pytest.raises(OSError):
        d.cancel_job(item.dispatch_id)
    monkeypatch.undo()

    assert d.get_job(item.dispatch_id).state == JOB_QUEUED, "memory kept an uncommitted state"
    reloaded = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    assert reloaded.get_job(item.dispatch_id).state == JOB_QUEUED


def test_a_committed_transition_is_never_reported_as_failed(tmp_path, monkeypatch):
    """The generation file is a notification, not the authority.

    jobs.json committed and only the generation write failed, yet the call
    raised: the caller was told its cancel failed while the dispatch really was
    CANCELLED, and the retry then answered invalid_transition.
    """
    import audapack.bridge.browser_dispatch as bd

    d = dispatcher(tmp_path)
    item = _queued(d, tmp_path, "GENFAIL")

    real_write = bd._atomic_write_json

    def refuse_generation(path, value):
        if "generation" in Path(path).name:
            raise OSError("injected generation replace failure")
        return real_write(path, value)

    monkeypatch.setattr(bd, "_atomic_write_json", refuse_generation)
    assert d.cancel_job(item.dispatch_id) is True
    monkeypatch.undo()

    assert d.get_job(item.dispatch_id).state == JOB_CANCELLED
    reloaded = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    assert reloaded.get_job(item.dispatch_id).state == JOB_CANCELLED, "the committed jobs document was lost"


def test_a_deferred_generation_publish_is_repaired_by_the_next_write(tmp_path, monkeypatch):
    import audapack.bridge.browser_dispatch as bd

    d = dispatcher(tmp_path)
    first = _queued(d, tmp_path, "REPAIR1")
    real_write = bd._atomic_write_json

    def refuse_generation(path, value):
        if "generation" in Path(path).name:
            raise OSError("injected generation replace failure")
        return real_write(path, value)

    monkeypatch.setattr(bd, "_atomic_write_json", refuse_generation)
    d.cancel_job(first.dispatch_id)
    monkeypatch.undo()

    before = json.loads(d.generation_file.read_text(encoding="utf-8"))["generation"]
    _queued(d, tmp_path, "REPAIR2")
    after = json.loads(d.generation_file.read_text(encoding="utf-8"))["generation"]
    assert after > before, "the deferred generation was never published"


def test_cancelling_an_already_cancelled_dispatch_is_idempotent(tmp_path):
    """A retry of a terminal operation asks for a state that already holds."""
    d = dispatcher(tmp_path)
    item = _queued(d, tmp_path, "IDEMP")
    assert d.cancel_job(item.dispatch_id) is True
    assert d.cancel_job(item.dispatch_id) is True
    assert d.get_job(item.dispatch_id).state == JOB_CANCELLED


def test_restart_recovery_never_regresses_the_generation(tmp_path):
    """W2-005 (audit/2.md): the UI only refreshes on a HIGHER generation.

    `_load_jobs()` is exactly the code that rewrites recovered jobs and calls
    `_persist_jobs()`, and the persisted generation was read AFTER it -- so the
    in-memory value was still 0 and a restart doing its designed recovery
    republished at generation 1. Measured: a LEASED job persisted at generation 2
    came back QUEUED at generation 1, and MainWindow, which refreshes only when
    `dispatch_gen > _last_dispatch_generation`, ignored the recovery and every
    state change after it.
    """
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "GENPROJ"))
    d.claim_job("w1")
    assert d.get_job(item.dispatch_id).state == JOB_LEASED

    before = json.loads(d.generation_file.read_text(encoding="utf-8"))["generation"]
    assert before >= 2, f"the fixture needs a generation above 1, got {before}"

    reloaded = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    after = json.loads(reloaded.generation_file.read_text(encoding="utf-8"))["generation"]

    assert reloaded.get_job(item.dispatch_id).state == JOB_QUEUED, "the pre-START job was not requeued"
    assert after > before, f"generation regressed across restart recovery: {before} -> {after}"


def test_a_post_start_recovery_also_publishes_forward(tmp_path):
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    item = d.enqueue_job(job_payload(path, "GENPOST"))
    lease = d.claim_job("w1")
    for state in (JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_START_PREPARED, JOB_STARTED):
        d.transition_job(item.dispatch_id, "w1", lease.lease_id, state,
                         {"campaign_run_id": "run", "start_receipt": "receipt"})
    before = json.loads(d.generation_file.read_text(encoding="utf-8"))["generation"]

    reloaded = BrowserDispatcher(state_dir=tmp_path / "dispatch")
    after = json.loads(reloaded.generation_file.read_text(encoding="utf-8"))["generation"]

    assert reloaded.get_job(item.dispatch_id).state == JOB_BLOCKED
    assert after > before, f"generation regressed: {before} -> {after}"


def test_the_generation_never_decreases_across_repeated_reloads(tmp_path):
    """An invariant, not one scenario: monotonic across every reload cycle."""
    d = dispatcher(tmp_path)
    path = archive(tmp_path)
    d.register_worker(supported_worker("w1"))
    d.enqueue_job(job_payload(path, "MONO"))

    seen = [json.loads(d.generation_file.read_text(encoding="utf-8"))["generation"]]
    for index in range(4):
        reloaded = BrowserDispatcher(state_dir=tmp_path / "dispatch")
        reloaded.enqueue_job(job_payload(path, f"MONO{index}"))
        seen.append(json.loads(reloaded.generation_file.read_text(encoding="utf-8"))["generation"])

    assert seen == sorted(seen), f"generation went backwards somewhere: {seen}"
    assert len(set(seen)) == len(seen), f"generation repeated a value: {seen}"
