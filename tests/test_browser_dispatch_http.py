"""SRC-005: integration tests for the Bridge HTTP dispatcher API.

The Bridge is exercised through the same `bridge_server` fixture used by
W4-003: an in-memory ThreadingHTTPServer with an OS-assigned port. Spec
coverage here focuses on the wire contract (request validation, auth,
loopback, content-length, ownership) so the Python domain plus the HTTP
adapter are both proven.
"""

from __future__ import annotations

import json
from http.client import HTTPConnection
from pathlib import Path


def _archive(tmp_path: Path, name: str = "PROJ.zip") -> Path:
    p = tmp_path / name
    p.write_bytes(b"PK\x03\x04fake-zip-bytes")
    return p


def _post(conn: HTTPConnection, path: str, body: dict, token: str) -> tuple[int, dict]:
    raw = json.dumps(body).encode("utf-8")
    conn.request(
        "POST",
        path,
        body=raw,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "X-ACB-Token": token,
        },
    )
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read().decode("utf-8") or "null")


def _live_dispatcher_owning(dispatch_id: str):
    """The in-memory dispatcher the running test server is actually using.

    ``bridge_server`` serves through a per-fixture handler subclass, and the
    dispatcher lives on that subclass -- not on the base handler.
    """
    from audapack.bridge.server import AudapackBridgeHandler

    for handler in AudapackBridgeHandler.__subclasses__():
        dispatcher = getattr(handler, "browser_dispatcher", None)
        if dispatcher is not None and dispatcher.get_job(dispatch_id) is not None:
            return dispatcher
    raise AssertionError(f"no live dispatcher owns {dispatch_id}")


def _get_with_headers(conn: HTTPConnection, path: str, headers: dict) -> tuple[int, dict, bytes]:
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    try:
        return resp.status, json.loads(body.decode("utf-8") or "null"), body
    except ValueError:
        return resp.status, {"_raw": body.decode("utf-8", errors="replace")}, body


def test_health_includes_dispatcher_status(bridge_server):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    _post(conn, "/v1/browser/poll", {"worker_id": "w_probe"}, config.bridge.token)
    status, payload, _ = _get_with_headers(
        conn,
        "/v1/browser/status",
        {"X-ACB-Token": config.bridge.token},
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["dispatch"]["max_workers"] == 6


def test_dispatch_jobs_submit_then_claim(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path)
    conn = HTTPConnection(base_url.replace("http://", ""))

    # Enqueue a job
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": "DISPATCH_A",
        "project_id": "p1",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
        "start_receipt": "receipt-start-1",
    }, config.bridge.token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]
    assert payload["dispatch"]["state"] == "QUEUED"

    # Worker polls
    status, payload = _post(conn, "/v1/browser/poll", {
        "worker_id": "w_alpha",
        "generating": False,
        "action_in_flight": False,
        "has_manual_draft": False,
        "has_attachments": False,
    }, config.bridge.token)
    assert status == 200, payload
    assert payload["job"]["dispatch_id"] == dispatch_id
    lease_id = payload["job"]["lease_id"]

    # Worker transitions through the lifecycle
    for to_state in ("ARTIFACT_FETCHED", "ATTACHED", "START_PREPARED", "STARTED", "AUDITING", "COMPLETE"):
        status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id,
            "worker_id": "w_alpha",
            "lease_id": lease_id,
            "state": to_state,
            "campaign_run_id": "runX",
            "conversation_id": "cX",
            "start_receipt": "receipt-start-1",
        }, config.bridge.token)
        assert status == 200, (to_state, payload)
    assert payload["job"]["state"] == "COMPLETE"


def test_dispatch_rejects_unauthenticated_request(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path)
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, _ = _post(conn, "/v1/browser/jobs", {
        "project_id": "x",
        "project_name": "X",
        "archive_path": str(archive),
        "archive_filename": archive.name,
    }, "wrong-token")
    assert status == 403


def test_dispatch_rejects_missing_archive(bridge_server):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_id": "x",
        "project_name": "X",
        "archive_path": "V:/does-not-exist.zip",
        "archive_filename": "does-not-exist.zip",
    }, config.bridge.token)
    assert status == 400
    assert payload["error"]["code"] == "missing_archive"


def test_artifact_ownership_requires_active_lease(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path)
    conn = HTTPConnection(base_url.replace("http://", ""))

    # Enqueue but never claim.
    _post(conn, "/v1/browser/jobs", {
        "project_id": "x",
        "project_name": "X",
        "archive_path": str(archive),
        "archive_filename": archive.name,
    }, config.bridge.token)

    # Random fetch attempt -- no lease.
    status, _payload, _body = _get_with_headers(conn, "/v1/browser/jobs/dsp-0000000000000000/artifact", {
        "X-ACB-Token": config.bridge.token,
        "X-Worker-Id": "w_thief",
        "X-Lease-Id": "lease-FAKE",
    })
    assert status == 400


def test_two_workers_never_get_the_same_job(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path)
    conn = HTTPConnection(base_url.replace("http://", ""))
    _post(conn, "/v1/browser/jobs", {
        "project_id": "only",
        "project_name": "ONLY",
        "archive_path": str(archive),
        "archive_filename": archive.name,
    }, config.bridge.token)

    # Both workers poll in turn. Only the first gets a job.
    _, first = _post(conn, "/v1/browser/poll", {
        "worker_id": "w_first",
    }, config.bridge.token)
    _, second = _post(conn, "/v1/browser/poll", {
        "worker_id": "w_second",
    }, config.bridge.token)
    assert first["job"] is not None
    assert second["job"] is None


def test_browser_jobs_state_stale_lease_rejected(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path)
    conn = HTTPConnection(base_url.replace("http://", ""))
    _post(conn, "/v1/browser/jobs", {
        "project_id": "x",
        "project_name": "X",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "start_receipt": "receipt-stale-1",
    }, config.bridge.token)
    _, polled = _post(conn, "/v1/browser/poll", {"worker_id": "w1"}, config.bridge.token)
    dispatch_id = polled["job"]["dispatch_id"]
    status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
        "dispatch_id": dispatch_id,
        "worker_id": "w1",
        "lease_id": "lease-WRONG",
        "state": "ARTIFACT_FETCHED",
    }, config.bridge.token)
    assert status == 400
    assert payload["error"]["code"] == "stale_lease"


def test_artifact_stream_carries_zip_bytes(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path, "STREAM.zip")
    conn = HTTPConnection(base_url.replace("http://", ""))
    _post(conn, "/v1/browser/jobs", {
        "project_id": "stream",
        "project_name": "STREAM",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
    }, config.bridge.token)
    _, polled = _post(conn, "/v1/browser/poll", {"worker_id": "w1"}, config.bridge.token)
    dispatch_id = polled["job"]["dispatch_id"]
    lease_id = polled["job"]["lease_id"]

    conn.request(
        "GET",
        f"/v1/browser/jobs/{dispatch_id}/artifact",
        headers={
            "X-ACB-Token": config.bridge.token,
            "X-Worker-Id": "w1",
            "X-Lease-Id": lease_id,
        },
    )
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/zip"
    assert resp.getheader("Content-Disposition", "").startswith("attachment;")
    assert body == b"PK\x03\x04fake-zip-bytes"


def test_browser_slots_reports_all_six_slots(bridge_server):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload, _ = _get_with_headers(conn, "/v1/browser/slots", {
        "X-ACB-Token": config.bridge.token,
    })
    assert status == 200
    assert payload["ok"] is True
    assert payload["max_lanes"] == 6
    assert [item["slot"] for item in payload["slots"]] == [1, 2, 3, 4, 5, 6]
    for item in payload["slots"]:
        assert "state" in item and "registered" in item and "generation" in item


def test_relaunch_slot_requires_an_integer_slot(bridge_server):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/relaunch-slot", {
        "slot": "not-a-number",
    }, config.bridge.token)
    assert status == 400
    assert payload["error"]["code"] == "invalid_slot"


def test_relaunch_slot_without_supervisor_is_503(bridge_server):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/relaunch-slot", {
        "slot": 3,
    }, config.bridge.token)
    assert status == 503
    assert payload["error"]["code"] == "supervisor_unavailable"


def test_relaunch_slot_rejects_unauthenticated_request(bridge_server):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, _ = _post(conn, "/v1/browser/relaunch-slot", {
        "slot": 3,
    }, "wrong-token")
    assert status == 403


def test_owner_poll_renews_the_lease_of_a_long_running_audit(bridge_server, tmp_path):
    """A worker that keeps polling keeps its run.

    The poll handler used to expire leases before registering the caller, and
    only a state transition extended one. An audit sits in AUDITING for minutes
    with no transition to make, so the owner's own poll aged its run out into
    "worker lost after START_PREPARED" while the audit was still running.
    """
    import time

    config, base_url = bridge_server
    archive = _archive(tmp_path, "RENEW.zip")
    conn = HTTPConnection(base_url.replace("http://", ""))

    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": "RENEW",
        "project_id": "renew",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
    }, config.bridge.token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]

    poll = {"worker_id": "w_renew", "generating": False, "action_in_flight": False,
            "has_manual_draft": False, "has_attachments": False}
    status, payload = _post(conn, "/v1/browser/poll", poll, config.bridge.token)
    assert status == 200, payload
    lease_id = payload["job"]["lease_id"]

    for to_state in ("ARTIFACT_FETCHED", "ATTACHED", "START_PREPARED", "STARTED", "AUDITING"):
        status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id,
            "worker_id": "w_renew",
            "lease_id": lease_id,
            "state": to_state,
            "campaign_run_id": "run-renew",
            "start_receipt": "receipt-renew",
        }, config.bridge.token)
        assert status == 200, (to_state, payload)

    # The audit has produced nothing to report for longer than one lease.
    dispatcher = _live_dispatcher_owning(dispatch_id)
    dispatcher.get_job(dispatch_id).lease_expires_at = time.time() - 1

    status, payload = _post(conn, "/v1/browser/poll", poll, config.bridge.token)
    assert status == 200, payload
    job = dispatcher.get_job(dispatch_id)
    assert job.state == "AUDITING"
    assert job.lease_expires_at > time.time()


def _enqueue(conn, config, tmp_path, name: str) -> str:
    archive = _archive(tmp_path, f"{name}.zip")
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": name,
        "project_id": name.lower(),
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
    }, config.bridge.token)
    assert status == 200, payload
    return payload["dispatch"]["dispatch_id"]


def _get_jobs(conn, config) -> list[dict]:
    conn.request("GET", "/v1/browser/jobs", headers={"X-ACB-Token": config.bridge.token})
    resp = conn.getresponse()
    return json.loads(resp.read().decode("utf-8"))["jobs"]


def test_the_waiting_line_can_be_reordered_over_the_wire(bridge_server, tmp_path):
    """Which project takes the next freed window is the operator's call."""
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    first = _enqueue(conn, config, tmp_path, "QA")
    second = _enqueue(conn, config, tmp_path, "QB")
    third = _enqueue(conn, config, tmp_path, "QC")

    # Move, not swap: two places up, leaving what it passed in its own order.
    status, payload = _post(conn, f"/v1/browser/jobs/{third}/reorder", {"delta": -2}, config.bridge.token)
    assert status == 200, payload
    assert payload["order"] == [third, first, second]

    positions = {job["dispatch_id"]: job["queue_position"] for job in _get_jobs(conn, config)}
    assert positions[third] == 0
    assert positions[first] == 1
    assert positions[second] == 2


def test_a_job_with_a_window_refuses_to_be_reordered_over_the_wire(bridge_server, tmp_path):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    dispatch_id = _enqueue(conn, config, tmp_path, "QD")
    status, payload = _post(conn, "/v1/browser/poll", {
        "worker_id": "w_reorder", "generating": False, "action_in_flight": False,
        "has_manual_draft": False, "has_attachments": False,
    }, config.bridge.token)
    assert status == 200 and payload["job"]["dispatch_id"] == dispatch_id

    status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/reorder", {"delta": -1}, config.bridge.token)
    assert status == 400
    assert payload["error"]["code"] == "not_waiting"

    # A job that is not waiting reports no place in the line, rather than 0 --
    # which would put it at the FRONT of a queue it is not even in.
    positions = {job["dispatch_id"]: job["queue_position"] for job in _get_jobs(conn, config)}
    assert positions[dispatch_id] == -1


def test_reorder_needs_the_token_like_every_other_dispatch_call(bridge_server, tmp_path):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    dispatch_id = _enqueue(conn, config, tmp_path, "QE")
    status, _payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/reorder", {"delta": -1}, "wrong-token")
    assert status == 403
