"""End-to-end browser-dispatch protocol over real HTTP.

Everything a worker window does, minus the DOM: submit, claim, stream the
artifact, walk the lifecycle, deliver the wave, and land COMPLETE. It exists
because the failures this session were never in one function -- they were in
how the steps fit together, and only a full pass catches that.
"""

from __future__ import annotations

import hashlib
import json
from http.client import HTTPConnection
from pathlib import Path

from audapack.campaign import get_canonical_manifest_hash, get_profile

CORE_WAVE = """PROJECT_NAME: E2EPROJ
DATE_TIME: 2026-09-01T12:00:00
CAMPAIGN_PROFILE: quick3
CAMPAIGN_PROFILE_VERSION: {profile_version}
CAMPAIGN_RUN_ID: {run_id}
CAMPAIGN_MANIFEST_SHA256: {manifest_hash}
WAVE: AUDIT CORE
TARGET: E2EPROJ repo
BASELINE: e2e-1
STATUS: AUDIT_CORE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [CORE-001] audapack/e2e.py
EVIDENCE: the leased artifact was streamed and attached
DEFECT: the dispatch could not reach COMPLETE from STARTED
REPAIR: allow the terminal transitions
VERIFY: this end-to-end pass

CORE_DONE_WHEN: the dispatch record is COMPLETE"""


def core_wave(run_id: str) -> str:
    profile = get_profile("quick3")
    return CORE_WAVE.format(
        run_id=run_id,
        profile_version=profile.profile_version,
        manifest_hash=profile.manifest_hash or get_canonical_manifest_hash(),
    )


def _post(conn: HTTPConnection, path: str, body: dict, token: str) -> tuple[int, dict]:
    raw = json.dumps(body).encode("utf-8")
    conn.request("POST", path, body=raw, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "X-ACB-Token": token,
    })
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read().decode("utf-8") or "null")


def _worker_payload(**extra) -> dict:
    return {
        "worker_id": "audapack-managed-1-1-e2e",
        "widget_version": "AUDAPACK_WIDGET/3",
        "is_chromium": True,
        "page_eligible": True,
        "clean_for_audit": True,
        "has_conversation_turns": False,
        "url_path": "/",
        "site": "chatgpt",
        "managed_slot": 1,
        "managed_generation": 1,
        "wait_seconds": 0,
        **extra,
    }


def test_a_dispatch_reaches_complete_through_the_real_protocol(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = tmp_path / "E2EPROJ.zip"
    archive.write_bytes(b"PK\x03\x04end-to-end-archive-bytes")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token

    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_id": "e2eproj",
        "project_name": "E2EPROJ",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
    }, token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]

    # The worker polls and is handed the job atomically.
    status, payload = _post(conn, "/v1/browser/poll", _worker_payload(), token)
    assert status == 200, payload
    assert payload["job"]["dispatch_id"] == dispatch_id
    lease_id = payload["job"]["lease_id"]

    # It streams the server-owned artifact through its lease.
    conn.request("GET", f"/v1/browser/jobs/{dispatch_id}/artifact", headers={
        "X-ACB-Token": token,
        "X-Worker-Id": _worker_payload()["worker_id"],
        "X-Lease-Id": lease_id,
    })
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200, body[:200]
    assert body == archive.read_bytes()

    def transition(state: str, **extra) -> dict:
        code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id,
            "worker_id": _worker_payload()["worker_id"],
            "lease_id": lease_id,
            "state": state,
            **extra,
        }, token)
        assert code == 200, (state, data)
        return data

    run_id = "acb-e2e-0001"
    transition("ARTIFACT_FETCHED")
    transition("ATTACHED")
    transition("START_PREPARED", campaign_run_id=run_id, start_receipt="startcore-e2e")
    transition("STARTED", campaign_run_id=run_id)

    # The AUDITING ack is deliberately dropped: that is the failure observed
    # live, where the audit ran to completion under a dispatch stuck in STARTED.
    transition("FINALIZING", campaign_run_id=run_id)

    status, payload = _post(conn, "/v1/audits", {
        "run_id": run_id,
        "project": "E2EPROJ",
        "wave": "core",
        "status": "complete",
        "api_version": 3,
        "receipt": "rcpt-e2e-001",
        "content": core_wave(run_id),
    }, token)
    assert status == 200, payload
    assert payload.get("ok") is True

    # One of three quick3 waves is delivered, so the campaign is NOT finished --
    # and W2-004 (audit/3.md) makes that the deciding fact rather than the
    # worker's word. An unproven COMPLETE is honoured as "I finished sending" and
    # holds at FINALIZING, keeping the lane owned for reconciliation instead of
    # letting the board claim an audit that never completed.
    transition("COMPLETE", campaign_run_id=run_id)
    status, payload = _post(conn, "/v1/browser/poll", _worker_payload(
        clean_for_audit=False, url_path="/c/e2e",
    ), token)
    assert status == 200, payload
    jobs = [job for job in _jobs(conn, token) if job["dispatch_id"] == dispatch_id]
    assert jobs and jobs[0]["state"] == "FINALIZING", jobs

    # With the handoff on disk the same ACK carries terminal proof -- path AND
    # the digest the bytes actually hash to (T-156) -- and the lane closes.
    handoff = Path(config.audits.root) / "E2EPROJ__00_AUDIT_ALL_3.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_bytes(b"final handoff bytes")
    transition("COMPLETE", campaign_run_id=run_id, final_handoff_path=str(handoff),
               final_handoff_sha256=hashlib.sha256(handoff.read_bytes()).hexdigest())
    jobs = [job for job in _jobs(conn, token) if job["dispatch_id"] == dispatch_id]
    assert jobs and jobs[0]["state"] == "COMPLETE", jobs
    assert jobs[0]["final_handoff_path"] == str(handoff)

    written = list(Path(config.audits.root).rglob("*AUDIT_CORE.md"))
    assert written, f"the audit was never written under {config.audits.root}"
    assert "CORE-001" in written[0].read_text(encoding="utf-8")


def _jobs(conn: HTTPConnection, token: str) -> list[dict]:
    conn.request("GET", "/v1/browser/jobs", headers={"X-ACB-Token": token})
    resp = conn.getresponse()
    return json.loads(resp.read().decode("utf-8")).get("jobs", [])


def test_a_polling_worker_keeps_its_run_past_the_lease(bridge_server, tmp_path):
    """The whole reason no audit had ever completed."""
    import time

    from audapack.bridge.server import AudapackBridgeHandler

    config, base_url = bridge_server
    archive = tmp_path / "LEASE.zip"
    archive.write_bytes(b"PK\x03\x04lease")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token

    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_id": "leaseproj", "project_name": "LEASEPROJ",
        "archive_path": str(archive), "archive_filename": archive.name,
        "archive_size": archive.stat().st_size, "profile": "quick3",
    }, token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]

    worker = _worker_payload(worker_id="audapack-managed-2-1-lease", managed_slot=2)
    status, payload = _post(conn, "/v1/browser/poll", worker, token)
    lease_id = payload["job"]["lease_id"]
    for state, extra in (
        ("ARTIFACT_FETCHED", {}), ("ATTACHED", {}),
        ("START_PREPARED", {"campaign_run_id": "acb-lease", "start_receipt": "r"}),
        ("STARTED", {"campaign_run_id": "acb-lease"}),
        ("AUDITING", {"campaign_run_id": "acb-lease"}),
    ):
        code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id, "worker_id": worker["worker_id"],
            "lease_id": lease_id, "state": state, **extra,
        }, token)
        assert code == 200, (state, data)

    dispatcher = next(
        d for d in (getattr(h, "browser_dispatcher", None) for h in AudapackBridgeHandler.__subclasses__())
        if d is not None and d.get_job(dispatch_id) is not None
    )
    dispatcher.get_job(dispatch_id).lease_expires_at = time.time() - 1

    # A wave takes minutes and makes no transitions. The poll must keep it.
    status, _ = _post(conn, "/v1/browser/poll", worker, token)
    assert status == 200
    job = dispatcher.get_job(dispatch_id)
    assert job.state == "AUDITING"
    assert job.lease_expires_at > time.time()


COMPRESS_WAVE = """PROJECT_NAME: CMPROJ
DATE_TIME: 2026-09-01T18:30:00
CAMPAIGN_PROFILE: compress
CAMPAIGN_PROFILE_VERSION: {profile_version}
CAMPAIGN_RUN_ID: {run_id}
CAMPAIGN_MANIFEST_SHA256: {manifest_hash}
WAVE_ID: compress
WAVE: COMPRESS AUDIT
TARGET: CMPROJ repo
BASELINE: cm-1
STATUS: COMPRESS: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [CMP-001] DELETE audapack/legacy_shim.py
EVIDENCE: no importer, no entry point, no test references it
WASTE: 240 lines and one dependency kept alive for a migration that finished
ACTION: DELETE
BEHAVIOR_GUARD: the public run() signature and its exit codes stay identical
IMPACT: delete 1 file, remove ~240 source LOC, drop one dependency
VERIFY: full suite green and the CLI smoke test still exits 0

COMPRESS_DONE_WHEN: the file is gone and the suite is green."""


def compress_wave(run_id: str, project: str = "CMPROJ") -> str:
    profile = get_profile("compress")
    return COMPRESS_WAVE.format(
        run_id=run_id,
        profile_version=profile.profile_version,
        manifest_hash=profile.manifest_hash or get_canonical_manifest_hash(),
    ).replace("CMPROJ", project)


def test_a_compress_campaign_writes_its_own_canonical_handoff(bridge_server, tmp_path):
    """CM end to end: one wave in, __00_COMPRESS_AUDIT.md out.

    A one-wave profile must not be pushed through the Super10 synthesis, and
    the file it produces is what GG resolves.
    """
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    run_id = "acb-cm-e2e-0001"

    status, payload = _post(conn, "/v1/audits", {
        "run_id": run_id,
        "project": "CMPROJ",
        "wave": "compress",
        "profile_id": "compress",
        "status": "complete",
        "api_version": 3,
        "receipt": "rcpt-cm-001",
        "content": compress_wave(run_id),
    }, config.bridge.token)
    assert status == 200, payload
    assert payload.get("ok") is True

    root = Path(config.audits.root)
    handoff = list(root.rglob("CMPROJ__00_COMPRESS_AUDIT.md"))
    assert handoff, f"canonical compress handoff missing under {root}"
    text = handoff[0].read_text(encoding="utf-8")
    assert "CAMPAIGN_PROFILE: compress" in text
    assert "[CMP-001]" in text
    assert "COMPRESS_DONE_WHEN:" in text

    assert list(root.rglob("CMPROJ__01_AUDIT_COMPRESS.md")), "the per-wave artifact must exist"
    assert not list(root.rglob("*SUPER_AUDIT*")), "compress must never generate SUPER_AUDIT_* artifacts"
    assert not list(root.rglob("*AUDIT_ALL_3*")), "compress must not borrow the quick3 name"

    history = list(root.rglob("_history/**/CMPROJ__00_COMPRESS_AUDIT__*.md"))
    assert history, "the canonical handoff needs its history twin"


def test_the_compress_handoff_is_what_the_audit_index_calls_ready(bridge_server, tmp_path):
    """GG resolves final_handoff_path; for CM that is __00_COMPRESS_AUDIT.md."""
    from audapack.audits import AuditIndexer
    from audapack.models import Project

    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    run_id = "acb-cm-e2e-0002"

    status, payload = _post(conn, "/v1/audits", {
        "run_id": run_id, "project": "CMREADY", "wave": "compress",
        "profile_id": "compress", "status": "complete", "api_version": 3,
        "receipt": "rcpt-cm-002",
        "content": compress_wave(run_id, "CMREADY"),
    }, config.bridge.token)
    assert status == 200, payload

    handoff = list(Path(config.audits.root).rglob("CMREADY__00_COMPRESS_AUDIT.md"))[0]
    project = Project(id="cmready", display_name="CMREADY",
                      source_path=str(tmp_path / "src"), priority_group="MAIN0", slot=1)
    snapshot = AuditIndexer(config).scan_project(project)

    assert snapshot.audit_profile_id == "compress"
    assert snapshot.total_waves == 1
    assert snapshot.campaign_complete is True
    assert snapshot.final_handoff_ready is True
    assert Path(snapshot.final_handoff_path).resolve() == handoff.resolve()


def test_a_compress_dispatch_reaches_complete_through_the_real_protocol(bridge_server, tmp_path):
    """The CM lane, not just the CM artifact.

    The one-wave profile was proven from /v1/audits onward, which skips the
    half where dispatch actually lives: the profile riding the lease, the
    archive streaming under it, and the state machine landing COMPLETE without
    a quick3 assumption anywhere. That is the segment every CM failure this
    session happened in.
    """
    config, base_url = bridge_server
    archive = tmp_path / "CMLANE.zip"
    archive.write_bytes(b"PK\x03\x04compress-lane-archive")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    worker = _worker_payload(worker_id="audapack-managed-4-1-cm", managed_slot=4)

    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_id": "cmlane",
        "project_name": "CMLANE",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "compress",
    }, token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]

    status, payload = _post(conn, "/v1/browser/poll", worker, token)
    assert status == 200, payload
    job = payload["job"]
    assert job["dispatch_id"] == dispatch_id
    # The window must be told WHICH campaign to run: a CM press that arrives as
    # a bare project is how a compress dispatch ended up running A3.
    assert job.get("profile") == "compress", job
    lease_id = job["lease_id"]

    conn.request("GET", f"/v1/browser/jobs/{dispatch_id}/artifact", headers={
        "X-ACB-Token": token,
        "X-Worker-Id": worker["worker_id"],
        "X-Lease-Id": lease_id,
    })
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200, body[:200]
    assert body == archive.read_bytes()

    def transition(state: str, **extra) -> dict:
        code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id,
            "worker_id": worker["worker_id"],
            "lease_id": lease_id,
            "state": state,
            **extra,
        }, token)
        assert code == 200, (state, data)
        return data

    run_id = "acb-cm-lane-0001"
    transition("ARTIFACT_FETCHED")
    transition("ATTACHED")
    transition("START_PREPARED", campaign_run_id=run_id, start_receipt="startcm-e2e")
    transition("STARTED", campaign_run_id=run_id)
    transition("AUDITING", campaign_run_id=run_id)
    transition("FINALIZING", campaign_run_id=run_id)

    status, payload = _post(conn, "/v1/audits", {
        "run_id": run_id,
        "project": "CMLANE",
        "wave": "compress",
        "profile_id": "compress",
        "status": "complete",
        "api_version": 3,
        "receipt": "rcpt-cm-lane-001",
        "content": compress_wave(run_id, "CMLANE"),
    }, token)
    assert status == 200, payload

    transition("COMPLETE", campaign_run_id=run_id)

    jobs = [item for item in _jobs(conn, token) if item["dispatch_id"] == dispatch_id]
    assert jobs and jobs[0]["state"] == "COMPLETE", jobs

    root = Path(config.audits.root)
    handoff = list(root.rglob("CMLANE__00_COMPRESS_AUDIT.md"))
    assert handoff, f"the compress lane produced no canonical handoff under {root}"
    text = handoff[0].read_text(encoding="utf-8")
    assert "CAMPAIGN_PROFILE: compress" in text
    assert "[CMP-001]" in text
    assert not list(root.rglob("CMLANE*AUDIT_ALL_3*")), "the CM lane must not borrow the quick3 name"


def test_a_finished_compress_lane_releases_its_window(bridge_server, tmp_path):
    """A window that cannot see its run end never rejoins the pool.

    The compress campaign is really delivered here, so the Bridge's own probe
    confirms it -- which is what W2-004 requires before a lane may close. The
    worker's ACK is the trigger; the proof is the Bridge's.
    """
    config, base_url = bridge_server
    archive = tmp_path / "CMFREE.zip"
    archive.write_bytes(b"PK\x03\x04compress-release")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    worker = _worker_payload(worker_id="audapack-managed-5-1-cmfree", managed_slot=5)

    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_id": "cmfree", "project_name": "CMFREE",
        "archive_path": str(archive), "archive_filename": archive.name,
        "archive_size": archive.stat().st_size, "profile": "compress",
    }, token)
    dispatch_id = payload["dispatch"]["dispatch_id"]
    status, payload = _post(conn, "/v1/browser/poll", worker, token)
    lease_id = payload["job"]["lease_id"]
    run_id = "acb-cm-free-0001"
    for state, extra in (
        ("ARTIFACT_FETCHED", {}), ("ATTACHED", {}),
        ("START_PREPARED", {"campaign_run_id": run_id, "start_receipt": "r"}),
        ("STARTED", {"campaign_run_id": run_id}),
        ("AUDITING", {"campaign_run_id": run_id}),
    ):
        code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id, "worker_id": worker["worker_id"],
            "lease_id": lease_id, "state": state, **extra,
        }, token)
        assert code == 200, (state, data)

    # The single compress wave IS the campaign, so delivering it finishes the run.
    status, payload = _deliver_compress_wave(conn, token, "CMFREE", run_id)
    assert status == 200, payload

    code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
        "dispatch_id": dispatch_id, "worker_id": worker["worker_id"],
        "lease_id": lease_id, "state": "COMPLETE", "campaign_run_id": run_id,
    }, token)
    assert code == 200, data

    # The next poll has to carry the terminal state back, or the window keeps
    # its lease and `lease-still-owned` pins it out of the clean pool forever.
    status, payload = _post(conn, "/v1/browser/poll", worker, token)
    assert status == 200, payload
    owned = payload.get("owned_job") or {}
    assert owned.get("dispatch_id") == dispatch_id, payload
    assert owned.get("state") == "COMPLETE", owned



def _run_compress_to_finalizing(conn, token, project: str, archive: Path, worker: dict, run_id: str) -> str:
    """Submit, claim, stream and walk one COMPRESS dispatch up to FINALIZING.

    The one-wave profile is used deliberately: delivering its single wave makes
    the campaign complete, so one /v1/audits call reaches the finalization
    commit these tests are about.
    """
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_id": project.lower(), "project_name": project,
        "archive_path": str(archive), "archive_filename": archive.name,
        "archive_size": archive.stat().st_size, "profile": "compress",
    }, token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]

    status, payload = _post(conn, "/v1/browser/poll", worker, token)
    assert status == 200, payload
    lease_id = payload["job"]["lease_id"]

    for state, extra in (
        ("ARTIFACT_FETCHED", {}), ("ATTACHED", {}),
        ("START_PREPARED", {"campaign_run_id": run_id, "start_receipt": f"startcm-{run_id}"}),
        ("STARTED", {"campaign_run_id": run_id}),
        ("AUDITING", {"campaign_run_id": run_id}),
        ("FINALIZING", {"campaign_run_id": run_id}),
    ):
        code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id, "worker_id": worker["worker_id"],
            "lease_id": lease_id, "state": state, **extra,
        }, token)
        assert code == 200, (state, data)
    return dispatch_id


def _deliver_compress_wave(conn, token, project: str, run_id: str) -> tuple[int, dict]:
    return _post(conn, "/v1/audits", {
        "run_id": run_id, "project": project, "wave": "compress",
        "profile_id": "compress", "status": "complete", "api_version": 3,
        "receipt": f"rcpt-{run_id}",
        "content": compress_wave(run_id, project),
    }, token)


def test_a_failed_index_commit_never_leaves_a_complete_dispatch(bridge_server, tmp_path, monkeypatch):
    """W2-001 (audit/1.md): transport COMPLETE is downstream of the commit.

    The lane was closed inside the finalization block, BEFORE campaign.json and
    the run state were written. A failed index write rolled the canonical
    handoff back off disk and left the dispatch COMPLETE pointing at a file that
    no longer existed, with its worker freed for the next audit.
    """
    from audapack.bridge import server as server_mod

    config, base_url = bridge_server
    archive = tmp_path / "W2ONE.zip"
    archive.write_bytes(b"PK\x03\x04rollback-order")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    worker = _worker_payload(worker_id="audapack-managed-2-1-w2one", managed_slot=2)
    run_id = "acb-w2-001-order"
    dispatch_id = _run_compress_to_finalizing(conn, token, "W2ONE", archive, worker, run_id)

    monkeypatch.setattr(server_mod, "save_live_campaign_index", lambda **kw: (_ for _ in ()).throw(
        OSError("injected index write failure")))

    status, payload = _deliver_compress_wave(conn, token, "W2ONE", run_id)

    assert status == 503, payload
    assert payload["error"]["code"] == "campaign_index_failed", payload

    jobs = [item for item in _jobs(conn, token) if item["dispatch_id"] == dispatch_id]
    assert jobs, "the dispatch disappeared"
    assert jobs[0]["state"] != "COMPLETE", "a failed commit published transport COMPLETE"
    assert jobs[0]["final_handoff_path"] == "", jobs[0]

    root = Path(config.audits.root)
    assert not list(root.rglob("W2ONE__00_COMPRESS_AUDIT.md")), \
        "an uncommitted canonical handoff survived the rollback"


def test_an_incomplete_rollback_is_reported_and_never_called_clean(bridge_server, tmp_path, monkeypatch):
    """A file the rollback could not remove is still published state.

    Every caller discarded `restore_file_snapshots()`'s error list, so a failed
    index answered an ordinary retriable `campaign_index_failed` -- "nothing was
    published" -- while a canonical completion artifact it could not unlink sat
    on disk for the next reader to treat as a finished audit.
    """
    from audapack.bridge import server as server_mod

    config, base_url = bridge_server
    archive = tmp_path / "W2RES.zip"
    archive.write_bytes(b"PK\x03\x04rollback-residue")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    worker = _worker_payload(worker_id="audapack-managed-3-1-w2res", managed_slot=3)
    run_id = "acb-w2-001-residue"
    _run_compress_to_finalizing(conn, token, "W2RES", archive, worker, run_id)

    monkeypatch.setattr(server_mod, "save_live_campaign_index", lambda **kw: (_ for _ in ()).throw(
        OSError("injected index write failure")))
    monkeypatch.setattr(
        server_mod, "restore_file_snapshots",
        lambda snapshots: ["W2RES__00_COMPRESS_AUDIT.md: injected unlink failure"],
    )

    status, payload = _deliver_compress_wave(conn, token, "W2RES", run_id)

    assert status == 503, payload
    assert payload["error"]["code"] == "rollback_incomplete", payload
    assert payload["error"]["retriable"] is False
    assert "W2RES__00_COMPRESS_AUDIT.md" in payload["error"]["message"]
    assert payload["error"]["rollback_errors"]


def test_a_committed_campaign_still_closes_its_lane_and_mirrors(bridge_server, tmp_path):
    """The safety net moved past the commit -- it must still be there.

    The lane learns a campaign finished from the worker's terminal ACK, and that
    ACK is one HTTP call that can fail to arrive; writing the durable handoff is
    the backstop. Moving it after the commit must not have removed it.
    """
    config, base_url = bridge_server
    archive = tmp_path / "W2OK.zip"
    archive.write_bytes(b"PK\x03\x04commit-then-close")
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    worker = _worker_payload(worker_id="audapack-managed-4-1-w2ok", managed_slot=4)
    run_id = "acb-w2-001-committed"
    dispatch_id = _run_compress_to_finalizing(conn, token, "W2OK", archive, worker, run_id)

    status, payload = _deliver_compress_wave(conn, token, "W2OK", run_id)
    assert status == 200, payload

    # No terminal ACK is ever sent: the lane must close on the durable write.
    jobs = [item for item in _jobs(conn, token) if item["dispatch_id"] == dispatch_id]
    assert jobs and jobs[0]["state"] == "COMPLETE", jobs
    assert jobs[0]["final_handoff_path"].endswith("W2OK__00_COMPRESS_AUDIT.md"), jobs[0]
    assert Path(jobs[0]["final_handoff_path"]).is_file()


def _build_quick3_wave_text(project: str, run_id: str, wave_id: str) -> str:
    from audapack.campaign import get_profile

    profile = get_profile("quick3")
    wave_def = profile.get_wave_by_id(wave_id)
    assert wave_def is not None, wave_id
    return (
        f"PROJECT_NAME: {project}\n"
        f"CAMPAIGN_PROFILE: quick3\n"
        f"CAMPAIGN_PROFILE_VERSION: {profile.profile_version}\n"
        f"CAMPAIGN_RUN_ID: {run_id}\n"
        f"CAMPAIGN_MANIFEST_SHA256: {profile.manifest_hash}\n"
        f"WAVE_ID: {wave_def.id}\n"
        f"WAVE_INDEX: {wave_def.ordinal}\n"
        f"WAVE_COUNT: {profile.wave_count}\n"
        f"WAVE: {wave_def.wave_header}\n"
        f"TARGET: repo\n"
        f"BASELINE: main\n"
        f"{wave_def.status_line}\n"
        f"TICKETS: 0\n"
        f"HANDOFF: IMPLEMENTATION_AGENT\n"
        f"{wave_def.no_findings_marker or 'NO VERIFIED DEFECTS.'}\n"
        f"{wave_def.done_marker.rstrip(':')}: verified\n"
    )


def _deliver_quick3_wave(conn, token, project, run_id, wave_id, receipt) -> tuple[int, dict]:
    return _post(conn, "/v1/audits", {
        "run_id": run_id,
        "project": project,
        "wave": wave_id,
        "profile_id": "quick3",
        "status": "complete",
        "api_version": 3,
        "receipt": receipt,
        "content": _build_quick3_wave_text(project, run_id, wave_id),
    }, token)


def _complete_quick3(conn, token, project, run_id, tag) -> None:
    for wave_id in ("core", "second", "performance"):
        status, payload = _deliver_quick3_wave(
            conn, token, project, run_id, wave_id, f"{tag}-{wave_id}"
        )
        assert status == 200, (wave_id, payload)


def test_non_final_index_failure_rolls_back_every_byte(bridge_server, tmp_path, monkeypatch):
    """W2-001 (SRC-041:R005): the rollback used to be final-wave-only.

    A failed index write on a NON-final wave answered a clean retriable
    `campaign_index_failed` while the canonical latest and history files stayed
    published on disk -- a durable half-commit no run state ever recorded.
    """
    from audapack.bridge import server as server_mod
    from audapack.bridge import state as state_mod

    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    run_id = "acb-w2-001-nonfinal"
    project = "W2NF"
    root = Path(config.audits.root)
    state_file = state_mod.get_run_state_file(run_id)
    before_state = state_file.read_bytes() if state_file.exists() else None

    monkeypatch.setattr(server_mod, "save_live_campaign_index", lambda **kw: (_ for _ in ()).throw(
        OSError("injected index write failure")))

    status, payload = _deliver_quick3_wave(conn, token, project, run_id, "core", "nf-core")
    assert status == 503, payload
    assert payload["error"]["code"] == "campaign_index_failed", payload
    assert not list(root.rglob("*AUDIT_CORE*.md")), "the non-final wave survived the rollback"
    assert not list(root.rglob("campaign.json")), "campaign.json survived the rollback"
    after_state = state_file.read_bytes() if state_file.exists() else None
    assert after_state == before_state, "run state changed across a rolled-back non-final wave"

    # Retry after the clean rollback: exactly one eventual history artifact.
    monkeypatch.undo()
    status, payload = _deliver_quick3_wave(conn, token, project, run_id, "core", "nf-core")
    assert status == 200, payload
    history = [p for p in root.rglob("*AUDIT_CORE*.md") if "_history" in p.parts]
    assert len(history) == 1, history


def test_duplicate_retry_with_new_receipt_flushes_generation_pending(bridge_server, tmp_path, monkeypatch):
    """W2-002 G2: different-receipt duplicates used to skip generation_pending.

    The old fast path returned a duplicate for identical completed content
    without touching the pending marker, so the generation count never advanced
    no matter how many retries arrived.
    """
    from audapack.bridge import state as state_mod
    from audapack.bridge.state import GenerationPersistenceError

    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    run_id = "acb-w2-002-gen"
    project = "W2GEN"
    _complete_quick3(conn, token, project, run_id, "gen")

    state = state_mod.get_run_state(run_id)
    state["generation_pending"] = True
    state_mod.save_run_state(run_id, state)

    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise GenerationPersistenceError("injected generation failure")

    monkeypatch.setattr(state_mod, "increment_audit_generation", _boom)

    status, payload = _deliver_quick3_wave(
        conn, token, project, run_id, "performance", "gen-different-receipt")

    assert status == 200, payload
    assert payload.get("duplicate") is True
    assert calls["n"] == 1, calls
    assert state_mod.get_run_state(run_id).get("generation_pending") is True


def test_duplicate_retry_marker_clear_failure_never_drops_the_socket(bridge_server, tmp_path, monkeypatch):
    """W2-002 G2: publishing then failing the marker-clear dropped the HTTP conn.

    The old same-receipt path let RunStatePersistenceError escape from the
    duplicate branch, so the retry saw a transport error even though the
    generation had been published. It must answer valid JSON and keep the
    durable marker pending for a later repair.
    """
    from audapack.bridge import server as server_mod
    from audapack.bridge import state as state_mod
    from audapack.bridge.state import RunStatePersistenceError

    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    run_id = "acb-w2-002-clear"
    project = "W2CLR"
    _complete_quick3(conn, token, project, run_id, "clr")

    state = state_mod.get_run_state(run_id)
    state["generation_pending"] = True
    state_mod.save_run_state(run_id, state)

    monkeypatch.setattr(state_mod, "increment_audit_generation", lambda *a, **k: None)
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        raise RunStatePersistenceError("injected marker-clear failure")

    monkeypatch.setattr(server_mod, "save_run_state", _flaky)

    # ORIGINAL receipt: the exact payload that once dropped the connection.
    status, payload = _deliver_quick3_wave(
        conn, token, project, run_id, "performance", "clr-performance")

    assert status == 200, payload
    assert payload.get("duplicate") is True
    assert calls["n"] >= 1, "the marker-clear save was never attempted"
    assert state_mod.get_run_state(run_id).get("generation_pending") is True


def test_duplicate_retry_repairs_incomplete_finalization_without_extra_history(bridge_server, tmp_path, monkeypatch):
    """W2-002 G3: both receipt variants converge on the same finalization repair.

    The canonical artifacts and index were lost after the wave records were
    committed. A retry under a NEW receipt used to return campaign_ready=False
    forever; it must now rebuild the final artifacts, reuse the existing
    history stamp, and not mint an extra history artifact.
    """
    from audapack.bridge import state as state_mod

    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    run_id = "acb-w2-002-finalize"
    project = "W2FIN"
    _complete_quick3(conn, token, project, run_id, "fin")

    root = Path(config.audits.root)
    all3 = list(root.rglob("*__00_AUDIT_ALL_3.md"))
    campaign = list(root.rglob("campaign.json"))
    assert all3 and campaign, "campaign did not finalize"
    history_before = sorted(
        p.name for p in root.rglob("*__00_AUDIT_ALL_3__*.md") if "_history" in p.parts
    )
    assert history_before

    # Crash residue: canonical artifacts and index gone, waves still complete.
    all3[0].unlink()
    campaign[0].unlink()
    state = state_mod.get_run_state(run_id)
    state["all3_complete"] = False
    state.pop("all3_path", None)
    state_mod.save_run_state(run_id, state)

    status, payload = _deliver_quick3_wave(
        conn, token, project, run_id, "performance", "fin-different-receipt")

    assert status == 200, payload
    assert payload.get("campaign_ready") is True, payload
    assert list(root.rglob("*__00_AUDIT_ALL_3.md")), "finalization was not repaired"
    assert list(root.rglob("campaign.json")), "campaign.json was not repaired"
    history_after = sorted(
        p.name for p in root.rglob("*__00_AUDIT_ALL_3__*.md") if "_history" in p.parts
    )
    assert history_after == history_before, "a repair minted an extra history artifact"


def test_same_receipt_with_different_content_still_conflicts(bridge_server, tmp_path):
    """W2-002 G1: unifying duplicate repair must not weaken receipt identity."""
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    token = config.bridge.token
    run_id = "acb-w2-002-conflict"
    project = "W2CON"

    status, payload = _deliver_quick3_wave(conn, token, project, run_id, "core", "conflict-r")
    assert status == 200, payload

    # Same receipt, different content for the SAME wave -> conflict.
    altered = dict(
        run_id=run_id,
        project=project,
        wave="core",
        profile_id="quick3",
        status="complete",
        api_version=3,
        receipt="conflict-r",
        content=_build_quick3_wave_text(project, run_id, "core") + "\nextra line",
    )
    status, payload = _post(conn, "/v1/audits", altered, token)
    assert status == 409, payload
    assert payload["error"]["code"] == "receipt_conflict", payload
