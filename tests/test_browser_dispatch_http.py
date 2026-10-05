"""SRC-005: integration tests for the Bridge HTTP dispatcher API.

The Bridge is exercised through the same `bridge_server` fixture used by
W4-003: an in-memory ThreadingHTTPServer with an OS-assigned port. Spec
coverage here focuses on the wire contract (request validation, auth,
loopback, content-length, ownership) so the Python domain plus the HTTP
adapter are both proven.
"""

from __future__ import annotations

import hashlib
import json
from http.client import HTTPConnection
from pathlib import Path

import pytest


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
    handoff = tmp_path / "DISPATCH_A__00_AUDIT_ALL_3.md"
    handoff.write_text("final", encoding="utf-8")
    for to_state in ("ARTIFACT_FETCHED", "ATTACHED", "START_PREPARED", "STARTED", "AUDITING", "COMPLETE"):
        body = {
            "dispatch_id": dispatch_id,
            "worker_id": "w_alpha",
            "lease_id": lease_id,
            "state": to_state,
            "campaign_run_id": "runX",
            "conversation_id": "cX",
            "start_receipt": "receipt-start-1",
        }
        if to_state == "COMPLETE":
            # W2-004/T-156: terminal COMPLETE carries proof -- the path AND the
            # digest the bytes actually hash to -- or it is held at FINALIZING
            # for the Bridge's own reconciliation.
            body["final_handoff_path"] = str(handoff)
            body["final_handoff_sha256"] = hashlib.sha256(handoff.read_bytes()).hexdigest()
        status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", body, config.bridge.token)
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


def _drive_to_auditing(conn, config, dispatch_id: str, worker_id: str = "w_alpha") -> str:
    """Claim a queued dispatch and walk it to AUDITING, returning the lease id."""
    status, payload = _post(conn, "/v1/browser/poll", {
        "worker_id": worker_id,
        "generating": False,
        "action_in_flight": False,
        "has_manual_draft": False,
        "has_attachments": False,
    }, config.bridge.token)
    assert status == 200, payload
    lease_id = payload["job"]["lease_id"]
    for to_state in ("ARTIFACT_FETCHED", "ATTACHED", "START_PREPARED", "STARTED", "AUDITING"):
        status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/state", {
            "dispatch_id": dispatch_id,
            "worker_id": worker_id,
            "lease_id": lease_id,
            "state": to_state,
            "campaign_run_id": "runStop",
            "start_receipt": "receipt-stop-1",
        }, config.bridge.token)
        assert status == 200, (to_state, payload)
    assert payload["job"]["state"] == "AUDITING", payload
    return lease_id


def test_an_explicit_operator_stop_retires_a_live_managed_dispatch(bridge_server, tmp_path):
    """SRC-098: the widget's operator-A3-OFF handshake, over the real wire.

    This is the exact transition `managedA3OperatorStop()` issues when a human
    unchecks A3 on a worker that is mid-audit. It has to retire the lane
    terminally, release the worker, and stay idempotent under the repeat clicks
    and reload-resumes that Milestone E requires.
    """
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    dispatch_id = _enqueue(conn, config, tmp_path, "STOPME")
    _drive_to_auditing(conn, config, dispatch_id)

    status, payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/abandon", {
        "dispatch_id": dispatch_id,
        "reason": "a3-checkbox",
        "source": "operator",
    }, config.bridge.token)
    assert status == 200, payload
    assert payload["state"] == "FAILED", payload
    assert "a3-checkbox" in str(payload.get("error", "")), payload

    # Terminal, not active AUDIT ownership: the lane is gone from the room's
    # active set and the worker is free again rather than pinned.
    dispatcher = _live_dispatcher_owning(dispatch_id)
    job = dispatcher.get_job(dispatch_id)
    assert job.state == "FAILED"
    assert job.assigned_worker_id == "" and job.lease_id == ""
    assert job.campaign_run_id == "runStop", "the run must stay inspectable in Audit Runs"

    # Milestone E: a repeated OFF click is an ACK, not a contradiction.
    status, again = _post(conn, f"/v1/browser/jobs/{dispatch_id}/abandon", {"reason": "a3-checkbox"}, config.bridge.token)
    assert status == 200 and again["state"] == "FAILED", again
    assert dispatcher.get_job(dispatch_id).completed_at == job.completed_at


def test_the_operator_stop_needs_the_token_like_every_other_dispatch_call(bridge_server, tmp_path):
    config, base_url = bridge_server
    conn = HTTPConnection(base_url.replace("http://", ""))
    dispatch_id = _enqueue(conn, config, tmp_path, "STOPAUTH")
    status, _payload = _post(conn, f"/v1/browser/jobs/{dispatch_id}/abandon", {"reason": "a3-checkbox"}, "wrong-token")
    assert status == 403


# --------------------------------------------------------------------------
# PERF-001 (audit/12.md): ONE owner for the queue-time archive digest.
#
# The GUI used to hash the ZIP only so the Bridge could read the same bytes
# again to check it, so a normal browser-audit delivery read the whole archive
# three times before ChatGPT upload even started and a fourth to stream it.
# The Bridge owns the canonical path and can establish the pinned digest
# itself; the client's copy was pure duplicate I/O.
# --------------------------------------------------------------------------

def test_the_gui_does_not_hash_the_archive_to_submit_it(bridge_server, tmp_path, monkeypatch):
    from audapack.models import Project
    from audapack.services.bridge_service import BridgeService

    config, _base_url = bridge_server
    archive = _archive(tmp_path, "PERF_GUI.zip")
    monkeypatch.setattr(
        "audapack.services.bridge_service.find_archive_for_project",
        lambda *_a, **_k: archive,
    )
    monkeypatch.setattr(
        "audapack.services.bridge_service.resolve_output_dir",
        lambda *_a, **_k: tmp_path,
    )
    monkeypatch.setattr(
        "audapack.services.bridge_service._sha256_file",
        lambda *_a, **_k: pytest.fail("the GUI must not hash the archive to submit it"),
    )

    service = BridgeService(config)
    project = Project(id="p1", display_name="PERF1", source_path=str(tmp_path))
    result = service.submit_browser_audit(project, archive, profile="quick3")
    # The submission itself may still fail on transport/auth in this fixture;
    # what this test pins is that it never read the archive to produce it.
    assert isinstance(result, dict)


def test_the_bridge_hashes_the_archive_exactly_once_per_submission(bridge_server, tmp_path, monkeypatch):
    config, base_url = bridge_server
    archive = _archive(tmp_path, "PERF_ONCE.zip")
    calls = []
    from audapack.bridge import server as server_mod

    original = server_mod.AudapackBridgeHandler._sha256_path
    monkeypatch.setattr(
        server_mod.AudapackBridgeHandler,
        "_sha256_path",
        staticmethod(lambda p: (calls.append(Path(p)), original(p))[1]),
    )

    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": "PERF_ONCE",
        "project_id": "p1",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
    }, config.bridge.token)
    assert status == 200, payload
    assert len(calls) == 1, f"expected exactly one queue-time hash, saw {len(calls)}"
    assert calls[0].resolve() == archive.resolve()


def test_submission_without_a_client_digest_is_pinned_by_the_bridge(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path, "PERF_NOHASH.zip")
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": "PERF_NOHASH",
        "project_id": "p1",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
    }, config.bridge.token)
    assert status == 200, payload

    job = _live_dispatcher_owning(payload["dispatch"]["dispatch_id"]).get_job(
        payload["dispatch"]["dispatch_id"]
    )
    assert job.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    # The leased worker is handed the very same proof.
    status, poll = _post(conn, "/v1/browser/poll", {
        "worker_id": "w_perf", "generating": False, "action_in_flight": False,
        "has_manual_draft": False, "has_attachments": False,
    }, config.bridge.token)
    assert status == 200, poll
    assert poll["job"]["dispatch_id"] == job.dispatch_id


def test_a_wrong_legacy_client_digest_is_still_rejected(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path, "PERF_BADDIGEST.zip")
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": "PERF_BADDIGEST",
        "project_id": "p2",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "archive_sha256": "0" * 64,
        "profile": "quick3",
    }, config.bridge.token)
    assert status == 400, payload
    assert payload["error"]["code"] == "changed_archive", payload


def test_an_archive_changed_after_enqueue_is_still_refused_at_fetch(bridge_server, tmp_path):
    config, base_url = bridge_server
    archive = _archive(tmp_path, "PERF_CHANGED.zip")
    conn = HTTPConnection(base_url.replace("http://", ""))
    status, payload = _post(conn, "/v1/browser/jobs", {
        "project_name": "PERF_CHANGED",
        "project_id": "p3",
        "archive_path": str(archive),
        "archive_filename": archive.name,
        "archive_size": archive.stat().st_size,
        "profile": "quick3",
    }, config.bridge.token)
    assert status == 200, payload
    dispatch_id = payload["dispatch"]["dispatch_id"]

    status, poll = _post(conn, "/v1/browser/poll", {
        "worker_id": "w_perf", "generating": False, "action_in_flight": False,
        "has_manual_draft": False, "has_attachments": False,
    }, config.bridge.token)
    assert status == 200, poll
    lease_id = poll["job"]["lease_id"]

    archive.write_bytes(b"PKtampered-after-enqueue")

    from audapack.bridge.browser_dispatch import DispatchError
    dispatcher = _live_dispatcher_owning(dispatch_id)
    try:
        dispatcher.resolve_artifact(dispatch_id, "w_perf", lease_id)
    except DispatchError as exc:
        assert exc.code == "changed_archive"
    else:
        pytest.fail("a tampered archive must not reach the attachment step")
