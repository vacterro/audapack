"""SRC-083: a captured SAIHANDOFF becomes a ready-to-hand-over file.

The operator's manual chain was: copy the block, paste it into a scratchpad,
export it as a file, paste the file's path into an agent. These tests pin the
automatic replacement: detection is strict, the file is byte-exact and named
like the scratchpad export, and one block always resolves to one file.
"""

from __future__ import annotations

import datetime
import json
import urllib.request
import uuid
from pathlib import Path

from audapack import handoff_drop

REAL_APPEND = "AUDAPACK\n\nSAIHANDOFF APPEND — LIVE ESCAPED LATENCY: WIDGET 0.0.64 STILL STALLS AT GET\n\nEXECUTOR\n\nClaude\n"
REAL_RESUME = "ProTrail\n\nSAIHANDOFF — RESUME T-59 FROM E-577 PARTIAL COMPILE RECOVERY\n\nMISSION\n\nResume.\n"
WHEN = datetime.datetime(2026, 9, 23, 4, 8)


def test_detects_the_two_real_handoff_shapes():
    assert handoff_drop.detect_handoff(REAL_APPEND) == "AUDAPACK"
    assert handoff_drop.detect_handoff(REAL_RESUME) == "ProTrail"
    assert handoff_drop.detect_handoff("Wintage — SAIHANDOFF — T-286 continuation\n\nbody") == "Wintage"
    assert handoff_drop.detect_handoff("SAIHANDOFF_V1\nHANDOFF_ID: x\n") == "SAIHANDOFF"


def test_ordinary_replies_are_not_handoffs():
    assert handoff_drop.detect_handoff("") is None
    assert handoff_drop.detect_handoff("print('hello')\n") is None
    prose = "\n".join(["line"] * 20 + ["SAIHANDOFF appears far too late"])
    assert handoff_drop.detect_handoff(prose) is None
    assert handoff_drop.detect_handoff("We discussed the SAIHANDOFF format yesterday.\n") is None


def test_a_sentence_first_line_is_not_taken_as_the_project():
    text = "Here is the next brief for you.\n\nSAIHANDOFF — DO THE THING\n"
    assert handoff_drop.detect_handoff(text) == "SAIHANDOFF"


def test_code_block_chrome_above_the_label_is_not_the_project():
    text = "text\nCopy code\nProTrail\n\nSAIHANDOFF — RESUME T-59\n"
    assert handoff_drop.detect_handoff(text) == "ProTrail"


def test_filename_matches_the_scratchpad_export_and_is_path_safe():
    assert handoff_drop.drop_filename("AUDAPACK", WHEN) == "AUDAPACK_20260923_0408.md"
    assert handoff_drop.drop_filename('..\\evil/"name"', WHEN) == "evil__name_20260923_0408.md"
    assert handoff_drop.drop_filename("", WHEN) == "SAIHANDOFF_20260923_0408.md"


def test_materialize_is_byte_exact_and_content_addressed(tmp_path):
    first, reused = handoff_drop.materialize(REAL_APPEND, "AUDAPACK", tmp_path, WHEN)
    assert not reused
    assert first.name == "AUDAPACK_20260923_0408.md"
    assert first.read_bytes() == REAL_APPEND.encode("utf-8")

    again, reused = handoff_drop.materialize(REAL_APPEND, "AUDAPACK", tmp_path, WHEN)
    assert reused and again == first, "the same block never becomes a second file"

    other, reused = handoff_drop.materialize(REAL_APPEND + "extra\n", "AUDAPACK", tmp_path, WHEN)
    assert not reused
    assert other.name == "AUDAPACK_20260923_0408_2.md", "a same-minute collision never overwrites"
    assert first.read_bytes() == REAL_APPEND.encode("utf-8")


def test_an_edited_or_deleted_file_is_written_again(tmp_path):
    first, _ = handoff_drop.materialize(REAL_RESUME, "ProTrail", tmp_path, WHEN)
    first.write_text("operator edited this", encoding="utf-8")
    second, reused = handoff_drop.materialize(REAL_RESUME, "ProTrail", tmp_path, WHEN)
    assert not reused and second != first
    assert second.read_text(encoding="utf-8") == REAL_RESUME

    second.unlink()
    third, reused = handoff_drop.materialize(REAL_RESUME, "ProTrail", tmp_path, WHEN)
    assert not reused and third.read_text(encoding="utf-8") == REAL_RESUME


def test_a_broken_index_is_disposable(tmp_path):
    (tmp_path / handoff_drop.INDEX_NAME).write_text("{not json", encoding="utf-8")
    path, reused = handoff_drop.materialize(REAL_APPEND, "AUDAPACK", tmp_path, WHEN)
    assert not reused and path.is_file()
    assert json.loads((tmp_path / handoff_drop.INDEX_NAME).read_text(encoding="utf-8"))


def _post_capture(base_url: str, token: str, text: str, kind: str = "block") -> dict:
    payload = {
        "capture_id": str(uuid.uuid4()),
        "text": text,
        "capture_kind": kind,
        "captured_at": "2026-09-23T01:00:00Z",
        "source": "ChatGPT",
        "conversation_fingerprint": "handoff-chat",
        "project_hints": [],
    }
    request = urllib.request.Request(
        base_url + "/v1/inaudit/captures",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-ACB-Token": token},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def test_the_bridge_files_a_captured_handoff_and_returns_its_path(bridge_server, tmp_path):
    config, base_url = bridge_server
    drop = tmp_path / "drop"
    config.bridge.handoff_dir = str(drop)

    first = _post_capture(base_url, config.bridge.token, REAL_RESUME)
    assert first["ok"] and first["durable"]
    assert first["record"]["capture_kind"] == "handoff"
    handoff = first["handoff"]
    assert handoff["ok"] and handoff["label"] == "ProTrail" and handoff["reused"] is False
    path = Path(handoff["path"])
    assert path.parent == drop
    assert path.name.startswith("ProTrail_") and path.suffix == ".md"
    assert path.read_text(encoding="utf-8") == REAL_RESUME

    # A second capture of the same block (retry, re-render, second click) is the
    # same file, not a second copy.
    second = _post_capture(base_url, config.bridge.token, REAL_RESUME)
    assert second["handoff"]["reused"] is True
    assert Path(second["handoff"]["path"]) == path
    assert len([p for p in drop.iterdir() if p.suffix == ".md"]) == 1


def test_an_ordinary_capture_writes_no_handoff_file(bridge_server, tmp_path):
    config, base_url = bridge_server
    drop = tmp_path / "drop"
    config.bridge.handoff_dir = str(drop)
    result = _post_capture(base_url, config.bridge.token, "# Just notes\nnothing to hand over\n", kind="response")
    assert result["ok"]
    assert "handoff" not in result
    assert result["record"]["capture_kind"] == "response"
    assert not drop.exists()


BRIEF = "LIMISAW\n\nMISSION\n\nContinue the authoritative LIMISAW repository under SAIPEN control.\n\nDo not restart.\n\nVERIFY\n\nAll gates green.\n"


def test_a_brief_addressed_to_a_registered_project_is_a_handoff():
    known = {handoff_drop.project_key("_LIMISAW"), handoff_drop.project_key("__SAIMAIL__")}
    assert handoff_drop.detect_handoff(BRIEF, known) == "LIMISAW"
    assert handoff_drop.detect_handoff("SAIMAIL\nCREATE ONE TICKET.\nDo not reopen.\nNo wrapper.\nVerify.\n", known) == "SAIMAIL"


def test_a_project_line_alone_is_never_guessed():
    known = {handoff_drop.project_key("_LIMISAW")}
    assert handoff_drop.detect_handoff(BRIEF) is None, "no registry, no project-addressed guess"
    assert handoff_drop.detect_handoff("LIMISAW\nshort\n", known) is None, "a name needs a real body"
    assert handoff_drop.detect_handoff("import os\nimport sys\n\nx = 1\ny = 2\nz = 3\n", known) is None


def test_project_names_cover_every_registered_identity():
    from audapack.models import Project

    names = handoff_drop.project_names([
        Project(id="saimail", display_name="__SAIMAIL__", source_path="", audit_project_name="SAIMAIL", inaudit_aliases=["Mailer"]),
    ])
    assert {"saimail", "mailer"} <= names


def _post(base_url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base_url + "/v1/inaudit/captures",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-ACB-Token": token},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def _listing(base_url: str, token: str) -> list:
    request = urllib.request.Request(base_url + "/v1/inaudit/captures", headers={"X-ACB-Token": token})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))["captures"]


def test_the_bridge_files_a_brief_addressed_to_a_registered_project(bridge_server, tmp_path):
    from audapack.models import Project

    config, base_url = bridge_server
    config.bridge.handoff_dir = str(tmp_path / "drop")
    config.projects = [Project(id="limisaw", display_name="_LIMISAW", source_path=str(tmp_path))]
    result = _post(base_url, config.bridge.token, {
        "capture_id": str(uuid.uuid4()), "text": BRIEF, "capture_kind": "block", "handoff_only": True,
    })
    assert result["handoff"]["ok"] and result["handoff"]["label"] == "LIMISAW"
    assert Path(result["handoff"]["path"]).read_text(encoding="utf-8") == BRIEF
    assert result["record"]["capture_kind"] == "handoff"


def test_handoff_only_never_files_an_ordinary_block(bridge_server, tmp_path):
    config, base_url = bridge_server
    config.bridge.handoff_dir = str(tmp_path / "drop")
    before = len(_listing(base_url, config.bridge.token))
    result = _post(base_url, config.bridge.token, {
        "capture_id": str(uuid.uuid4()), "text": "import os\nimport sys\n\nx = 1\ny = 2\n", "capture_kind": "block", "handoff_only": True,
    })
    assert result == {"ok": True, "committed": False, "durable": False, "handoff": None, "skipped": "not_a_handoff"}
    assert len(_listing(base_url, config.bridge.token)) == before, "no inbox noise"
    assert not (tmp_path / "drop").exists()
