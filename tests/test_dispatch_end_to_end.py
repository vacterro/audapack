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
