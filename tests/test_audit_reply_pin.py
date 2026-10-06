"""An audit reply captured by the widget is pinned to the project whose archive it answered.

The widget auto-captures a finished ChatGPT reply to a user turn that carried a
project ZIP and sends the archive's filename along. The filename is an exact
project identity, so the Bridge pins the capture to that project and the Inbox
row needs no classification guess.
"""

from __future__ import annotations

import json
import urllib.request
import uuid

from audapack.models import Project
from audapack.packing import project_for_archive_filename

AUDIT = "# Audit\n\n" + "The walker treats a vanished directory as unreadable.\n" * 30


def _projects(tmp_path):
    return [
        Project(id="zaicode", display_name="_ZAICODE", source_path=str(tmp_path / "z"), archive_name="_ZAICODE"),
        Project(id="saipen", display_name="_SAIPEN", source_path=str(tmp_path / "s"), archive_name="_SAIPEN"),
        Project(id="saipenview", display_name="_SAIPENVIEW", source_path=str(tmp_path / "v"), archive_name="_SAIPENVIEW"),
    ]


def test_archive_filename_resolves_to_exactly_one_project(tmp_path):
    projects = _projects(tmp_path)
    assert project_for_archive_filename("_ZAICODE.zip", projects).id == "zaicode"
    assert project_for_archive_filename("_SAIPEN_24.09.26-T09-20-40.zip", projects).id == "saipen"
    assert project_for_archive_filename("_SAIPENVIEW.zip", projects).id == "saipenview"
    # A sibling prefix never claims the shorter stem, and junk names own nothing.
    assert project_for_archive_filename("_SAIPEN_Bar.zip", projects) is None
    assert project_for_archive_filename("notes.txt", projects) is None
    assert project_for_archive_filename("", projects) is None


def _post(base_url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base_url + "/v1/inaudit/captures",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-ACB-Token": token},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def test_bridge_pins_an_archive_reply_capture(bridge_server, tmp_path):
    config, base_url = bridge_server
    config.projects = _projects(tmp_path)
    result = _post(base_url, config.bridge.token, {
        "capture_id": str(uuid.uuid4()),
        "text": AUDIT,
        "capture_kind": "response",
        "archive_filename": "_ZAICODE.zip",
    })
    assert result["ok"] and result["durable"]
    assert result["record"]["target_project_id"] == "zaicode"
    assert result["pinned_project"] == "_ZAICODE"


def test_bridge_leaves_an_unknown_archive_capture_unpinned(bridge_server, tmp_path):
    config, base_url = bridge_server
    config.projects = _projects(tmp_path)
    result = _post(base_url, config.bridge.token, {
        "capture_id": str(uuid.uuid4()),
        "text": AUDIT,
        "capture_kind": "response",
        "archive_filename": "SomethingElse.zip",
    })
    assert result["ok"] and result["durable"]
    assert not result["record"].get("target_project_id")
    assert "pinned_project" not in result
