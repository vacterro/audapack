from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from audapack.inaudit_capture import MAX_CAPTURE_BYTES, InauditCaptureError, InauditCaptureStore, body_sha256
from audapack.models import Project


def _payload(text: str = "# Useful audit\nExact body\n") -> dict:
    return {
        "capture_id": str(uuid.uuid4()),
        "text": text,
        "capture_kind": "response",
        "captured_at": "2026-08-31T10:00:00Z",
        "source": "ChatGPT",
        "source_url": "https://chatgpt.com/c/example",
        "source_title": "Conversation",
        "browser_name": "Brave",
        "conversation_fingerprint": "chat-one",
        "project_hints": [],
    }


def test_capture_persists_verified_body_and_metadata(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    payload = _payload()
    result = store.capture(payload, [])
    record = result["record"]
    body = store.inbox_dir / f"{payload['capture_id']}.md"
    meta = store.inbox_dir / f"{payload['capture_id']}.json"
    assert result["durable"] is True
    assert body.read_text(encoding="utf-8") == payload["text"]
    assert json.loads(meta.read_text(encoding="utf-8")) == record
    assert record["content_sha256"] == body_sha256(payload["text"])
    assert store.get(payload["capture_id"])["text"] == payload["text"]


def test_duplicate_capture_id_is_idempotent_but_conflict_is_rejected(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    payload = _payload()
    first = store.capture(payload, [])
    second = store.capture(payload, [])
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(store.list_records()) == 1
    with pytest.raises(InauditCaptureError, match="different content") as caught:
        store.capture({**payload, "text": "changed"}, [])
    assert caught.value.status == 409


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"text": "   \n"}, "empty_capture"),
        ({"capture_id": "../../evil"}, "invalid_capture_id"),
        ({"source_title": "bad\x00path"}, "invalid_metadata"),
        ({"schema_version": 999}, "unsupported_schema_version"),
    ],
)
def test_invalid_capture_never_writes(tmp_path: Path, change: dict, code: str):
    store = InauditCaptureStore(tmp_path)
    with pytest.raises(InauditCaptureError) as caught:
        store.capture({**_payload(), **change}, [])
    assert caught.value.code == code
    assert not list(store.inbox_dir.iterdir())


def test_oversized_capture_rejected(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    with pytest.raises(InauditCaptureError) as caught:
        store.capture(_payload("x" * (MAX_CAPTURE_BYTES + 1)), [])
    assert caught.value.code == "capture_too_large"
    assert caught.value.status == 413


def test_partial_record_moves_to_recovery_without_deletion(tmp_path: Path):
    inbox = tmp_path / "inaudit" / "inbox"
    inbox.mkdir(parents=True)
    capture_id = str(uuid.uuid4())
    (inbox / f"{capture_id}.md").write_text("survives crash", encoding="utf-8")
    store = InauditCaptureStore(tmp_path)
    recovered = store.get(capture_id)
    assert recovered["record"]["status"] == "RECOVERY"
    assert recovered["text"] == "survives crash"
    assert not (inbox / f"{capture_id}.md").exists()


def test_duplicate_body_is_marked_not_discarded(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    first = _payload("same body")
    second = _payload("same body")
    store.capture(first, [])
    result = store.capture(second, [])
    assert result["record"]["status"] == "DUPLICATE"
    assert result["record"]["duplicate_of"] == first["capture_id"]
    assert len(store.list_records()) == 2


def test_list_order_is_stable(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    older = _payload("old")
    older["captured_at"] = "2026-01-01T00:00:00Z"
    newer = _payload("new")
    newer["captured_at"] = "2026-02-01T00:00:00Z"
    store.capture(older, [])
    store.capture(newer, [])
    assert [item["capture_id"] for item in store.list_records()] == [newer["capture_id"], older["capture_id"]]


def test_archive_preserves_history_and_restore_recovers_item(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    payload = _payload()
    store.capture(payload, [])
    archived = store.archive(payload["capture_id"])
    assert archived["status"] == "ARCHIVED"
    assert store.list_records() == []
    assert store.list_records(include_archived=True)[0]["status"] == "ARCHIVED"
    restored = store.restore(payload["capture_id"])
    assert restored["status"] == "NEW"
    assert store.get(payload["capture_id"])["text"] == payload["text"]


def test_delete_is_explicit_and_narrow(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    first = _payload("delete me")
    second = _payload("keep me")
    store.capture(first, [])
    store.capture(second, [])
    store.delete(first["capture_id"])
    assert [item["capture_id"] for item in store.list_records()] == [second["capture_id"]]


def test_project_object_is_not_required_for_uncertain_capture(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    project = Project(id="p", display_name="P", source_path=str(tmp_path / "missing"))
    result = store.capture(_payload("generic notes"), [project])
    assert result["record"]["classification_state"] == "UNASSIGNED"
