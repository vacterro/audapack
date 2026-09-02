"""End-to-end browser-dispatch protocol over real HTTP.

Everything a worker window does, minus the DOM: submit, claim, stream the
artifact, walk the lifecycle, deliver the wave, and land COMPLETE. It exists
because the failures this session were never in one function -- they were in
how the steps fit together, and only a full pass catches that.
"""

from __future__ import annotations

import json
from http.client import HTTPConnection
from pathlib import Path

CORE_WAVE = """PROJECT_NAME: E2EPROJ
DATE_TIME: 2026-09-01T12:00:00
WAVE: AUDIT CORE
TARGET: E2EPROJ repo
BASELINE: e2e-1
CAMPAIGN_RUN_ID: {run_id}
STATUS: AUDIT_CORE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [CORE-001] audapack/e2e.py
EVIDENCE: the leased artifact was streamed and attached
DEFECT: the dispatch could not reach COMPLETE from STARTED
REPAIR: allow the terminal transitions
VERIFY: this end-to-end pass

CORE_DONE_WHEN: the dispatch record is COMPLETE"""


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
        "content": CORE_WAVE.format(run_id=run_id),
    }, token)
    assert status == 200, payload
    assert payload.get("ok") is True

    transition("COMPLETE", campaign_run_id=run_id)

    status, payload = _post(conn, "/v1/browser/poll", _worker_payload(
        clean_for_audit=False, url_path="/c/e2e",
    ), token)
    assert status == 200, payload
    jobs = [job for job in _jobs(conn, token) if job["dispatch_id"] == dispatch_id]
    assert jobs and jobs[0]["state"] == "COMPLETE", jobs

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
CAMPAIGN_RUN_ID: {run_id}
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
        "content": COMPRESS_WAVE.format(run_id=run_id),
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
        "content": COMPRESS_WAVE.format(run_id=run_id).replace("CMPROJ", "CMREADY"),
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
        "content": COMPRESS_WAVE.format(run_id=run_id).replace("CMPROJ", "CMLANE"),
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
    """A window that cannot see its run end never rejoins the pool."""
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
        ("COMPLETE", {"campaign_run_id": run_id}),
    ):
        code, data = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id, "worker_id": worker["worker_id"],
            "lease_id": lease_id, "state": state, **extra,
        }, token)
        assert code == 200, (state, data)

    # The next poll has to carry the terminal state back, or the window keeps
    # its lease and `lease-still-owned` pins it out of the clean pool forever.
    status, payload = _post(conn, "/v1/browser/poll", worker, token)
    assert status == 200, payload
    owned = payload.get("owned_job") or {}
    assert owned.get("dispatch_id") == dispatch_id, payload
    assert owned.get("state") == "COMPLETE", owned
