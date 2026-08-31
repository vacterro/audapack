from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from audapack.config import save_config
from audapack.models import Project


def _request(base_url: str, token: str, path: str, *, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-ACB-Token": token},
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _capture_payload(text: str = "# Browser handoff\nAUDAPACK\n") -> dict:
    return {
        "capture_id": str(uuid.uuid4()),
        "text": text,
        "capture_kind": "response",
        "captured_at": "2026-08-31T10:00:00Z",
        "source": "ChatGPT",
        "browser_name": "Brave",
        "conversation_fingerprint": "api-chat",
        "project_hints": [],
    }


def test_capture_ack_is_durable_and_duplicate_safe(bridge_server):
    config, base_url = bridge_server
    payload = _capture_payload()
    status, first = _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
    _, second = _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
    assert status == 200
    assert first["ok"] and first["durable"] and not first["duplicate"]
    assert second["ok"] and second["durable"] and second["duplicate"]
    runtime = Path(config.audits.root).parent
    body = runtime / "inaudit" / "inbox" / f"{payload['capture_id']}.md"
    assert body.read_text(encoding="utf-8") == payload["text"]


def test_list_and_detail_return_stable_records(bridge_server):
    config, base_url = bridge_server
    payload = _capture_payload("# Exact API body\n")
    _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
    _, listing = _request(base_url, config.bridge.token, "/v1/inaudit/captures")
    _, detail = _request(base_url, config.bridge.token, f"/v1/inaudit/captures/{payload['capture_id']}")
    assert [item["capture_id"] for item in listing["captures"]] == [payload["capture_id"]]
    assert detail["text"] == payload["text"]
    assert detail["record"]["capture_id"] == payload["capture_id"]


def test_capture_api_rejects_empty_and_oversized_payload(bridge_server):
    config, base_url = bridge_server
    for payload, expected in [(_capture_payload(" "), 400), (_capture_payload("x" * (5 * 1024 * 1024 + 1)), 413)]:
        try:
            _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
            raise AssertionError("request should fail")
        except urllib.error.HTTPError as exc:
            assert exc.code == expected


def test_assignment_accepts_project_id_only_and_returns_canonical_layer(bridge_server):
    config, base_url = bridge_server
    project_root = Path(config.audits.root).parent / "RegisteredProject"
    project_root.mkdir()
    config.projects = [Project(id="registered", display_name="Registered", source_path=str(project_root))]
    save_config(config, base_dir=Path(config.audits.root).parent)
    payload = _capture_payload("# Assign through API\n")
    _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
    _, assigned = _request(
        base_url,
        config.bridge.token,
        f"/v1/inaudit/captures/{payload['capture_id']}/assign",
        payload={"project_id": "registered", "action": "CC"},
        method="POST",
    )
    path = Path(assigned["assigned_path"])
    assert path == project_root / "audit" / "1.md"
    assert path.read_text(encoding="utf-8") == payload["text"]
    assert assigned["command"] == f'saipen cc "{path}"'


def test_assignment_rejects_browser_destination_path(bridge_server):
    config, base_url = bridge_server
    payload = _capture_payload()
    _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
    request = urllib.request.Request(
        base_url + f"/v1/inaudit/captures/{payload['capture_id']}/assign",
        data=json.dumps({"project_id": "x", "path": "C:\\evil.md"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-ACB-Token": config.bridge.token},
    )
    try:
        urllib.request.urlopen(request)
        raise AssertionError("request should fail")
    except urllib.error.HTTPError as exc:
        error = json.loads(exc.read().decode("utf-8"))["error"]
        assert exc.code == 400
        assert error["code"] == "path_not_allowed"


def test_archive_and_delete_are_independent_from_browser_audit_state(bridge_server):
    config, base_url = bridge_server
    payload = _capture_payload()
    _request(base_url, config.bridge.token, "/v1/inaudit/captures", payload=payload, method="POST")
    _, archived = _request(
        base_url,
        config.bridge.token,
        f"/v1/inaudit/captures/{payload['capture_id']}/archive",
        payload={},
        method="POST",
    )
    assert archived["record"]["status"] == "ARCHIVED"
    status, deleted = _request(
        base_url,
        config.bridge.token,
        f"/v1/inaudit/captures/{payload['capture_id']}",
        method="DELETE",
    )
    assert status == 200 and deleted["capture_id"] == payload["capture_id"]

