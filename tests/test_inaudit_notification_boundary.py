"""W2-004 (audit/6.md R010): notification publication is a POST-COMMIT effect.

`_signal` persisted `inaudit_generation.json` INSIDE the mutation's success
boundary, so a filesystem failure there escaped as
`capture_persistence_failed` / `archive_persistence_failed` / 500 retriable
after the body, the moved pair or the deletion had already landed durably. The
caller was told the mutation failed, and the retry then contradicted that
answer: the duplicate branch returned without ever re-signalling (so the
generation stayed permanently unannounced), and archive/restore/delete reported
404 because the source pair no longer existed.

The fault is injected where it actually lives -- the generation file -- by
turning that path into a directory, so `atomic_write`'s final `os.replace`
fails for real instead of a patched method pretending to.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from audapack.inaudit_capture import InauditCaptureError, InauditCaptureStore
from audapack.models import Project


def _payload(text: str = "# Notification boundary\nExact body\n") -> dict:
    return {
        "capture_id": str(uuid.uuid4()),
        "text": text,
        "capture_kind": "response",
        "captured_at": "2026-09-06T10:00:00Z",
        "source": "ChatGPT",
        "conversation_fingerprint": "chat-w2-004",
        "project_hints": [],
    }


def _block_generation(root: Path) -> Path:
    """Make the generation publication fail without touching the mutation."""
    path = root / "inaudit" / "inaudit_generation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        path.unlink()
    path.mkdir(exist_ok=True)
    return path


def _unblock_generation(path: Path) -> None:
    if path.is_dir():
        path.rmdir()


def _generation(store: InauditCaptureStore) -> dict:
    if not store.generation_path.is_file():
        return {}
    return json.loads(store.generation_path.read_text(encoding="utf-8"))


def test_a_capture_is_committed_even_when_its_notification_cannot_be_published(tmp_path: Path):
    _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload()

    result = store.capture(payload, [])

    assert result["durable"] is True, "a notification failure was reported as a persistence failure"
    assert result["duplicate"] is False
    assert store.notification_pending is True
    assert store.get(payload["capture_id"])["text"] == payload["text"]
    assert store.pending_signal_path.is_file(), "the owed notification was not recorded for replay"
    owed = json.loads(store.pending_signal_path.read_text(encoding="utf-8"))
    assert owed["capture_id"] == payload["capture_id"]
    assert owed["event"] == "capture"


def test_the_owed_notification_survives_the_process_and_is_replayed_on_restart(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("restart replays me")
    store.capture(payload, [])
    assert store.notification_pending is True

    _unblock_generation(blocked)
    reopened = InauditCaptureStore(tmp_path)

    assert reopened.notification_pending is False
    published = _generation(reopened)
    assert published["capture_id"] == payload["capture_id"]
    assert published["generation"] == 1
    assert not reopened.pending_signal_path.exists(), "a published notification stayed owed"


def test_a_duplicate_retry_repairs_the_unannounced_generation(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("duplicate repairs the signal")
    store.capture(payload, [])
    assert _generation(store) == {}

    _unblock_generation(blocked)
    retry = store.capture(payload, [])

    assert retry["duplicate"] is True and retry["durable"] is True
    assert store.notification_pending is False
    assert _generation(store)["capture_id"] == payload["capture_id"]


def test_the_generation_advances_once_per_publication_not_once_per_retry(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("counted once")
    store.capture(payload, [])
    _unblock_generation(blocked)

    store.capture(payload, [])
    after_first_retry = _generation(store)["generation"]
    store.capture(payload, [])
    store.capture(payload, [])

    assert after_first_retry == 1
    assert _generation(store)["generation"] == 1, "a settled notification was published again"


def test_archive_retry_reports_the_terminal_state_instead_of_404(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("archived once")
    store.capture(payload, [])

    archived = store.archive(payload["capture_id"])
    assert archived["status"] == "ARCHIVED"
    assert store.notification_pending is True

    retry = store.archive(payload["capture_id"])
    assert retry["status"] == "ARCHIVED", "a committed archive answered its own retry with not-found"

    _unblock_generation(blocked)
    store.archive(payload["capture_id"])
    assert store.notification_pending is False
    assert _generation(store)["capture_id"] == payload["capture_id"]


def test_restore_retry_reports_the_terminal_state_instead_of_404(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("restored once")
    store.capture(payload, [])
    store.archive(payload["capture_id"])

    restored = store.restore(payload["capture_id"])
    assert restored["status"] == "NEW"

    retry = store.restore(payload["capture_id"])
    assert retry["status"] == "NEW", "a committed restore answered its own retry with not-found"
    _unblock_generation(blocked)


def test_delete_retry_reports_the_terminal_state_instead_of_404(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("deleted once")
    store.capture(payload, [])

    store.delete(payload["capture_id"])
    assert store.notification_pending is True
    store.delete(payload["capture_id"])  # must not raise

    _unblock_generation(blocked)
    store.delete(payload["capture_id"])
    assert store.notification_pending is False
    assert _generation(store)["event"] == "delete"


def test_a_committed_mutation_is_recognised_after_a_restart_between_failure_and_retry(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("survives the restart")
    store.capture(payload, [])
    store.archive(payload["capture_id"])

    _unblock_generation(blocked)
    reopened = InauditCaptureStore(tmp_path)
    assert reopened.notification_pending is False, "the recovered notification was not published"

    retry = reopened.archive(payload["capture_id"])
    assert retry["status"] == "ARCHIVED"


def test_an_unknown_capture_is_still_not_found_while_another_notification_is_owed(tmp_path: Path):
    blocked = _block_generation(tmp_path)
    store = InauditCaptureStore(tmp_path)
    payload = _payload("the only real capture")
    store.capture(payload, [])
    assert store.notification_pending is True

    stranger = str(uuid.uuid4())
    with pytest.raises(InauditCaptureError) as caught:
        store.archive(stranger)
    assert caught.value.status == 404
    with pytest.raises(InauditCaptureError):
        store.delete(stranger)
    _unblock_generation(blocked)


def test_a_cleanly_settled_mutation_is_still_not_found_on_a_later_retry(tmp_path: Path):
    """No owed notification means no proof, so the old 404 semantics stand."""
    store = InauditCaptureStore(tmp_path)
    payload = _payload("settled cleanly")
    store.capture(payload, [])
    store.archive(payload["capture_id"])
    assert store.notification_pending is False

    with pytest.raises(InauditCaptureError) as caught:
        store.archive(payload["capture_id"])
    assert caught.value.status == 404
    store.delete(payload["capture_id"])
    with pytest.raises(InauditCaptureError) as deleted:
        store.delete(payload["capture_id"])
    assert deleted.value.status == 404


def test_assign_is_committed_and_its_retry_replays_the_owed_notification(tmp_path: Path):
    blocked = _block_generation(tmp_path / "runtime")
    store = InauditCaptureStore(tmp_path / "runtime")
    root = tmp_path / "Project"
    root.mkdir()
    project = Project(id="project", display_name="Project", source_path=str(root))
    payload = _payload("assigned body")
    store.capture(payload, [project])

    assigned = store.assign(payload["capture_id"], project.id, [project])
    target = Path(assigned["assigned_path"])
    assert target.read_text(encoding="utf-8") == payload["text"]
    assert store.notification_pending is True

    _unblock_generation(blocked)
    retry = store.assign(payload["capture_id"], project.id, [project])

    assert retry["duplicate"] is True
    assert Path(retry["assigned_path"]) == target
    assert store.notification_pending is False
    assert _generation(store)["capture_id"] == payload["capture_id"]


def _request(base_url: str, token: str, path: str, *, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-ACB-Token": token},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_no_api_sequence_turns_a_committed_mutation_into_500_then_404(bridge_server):
    """The exact contradiction the clause reports, over the HTTP boundary."""
    config, base_url = bridge_server
    runtime = Path(config.audits.root).parent
    blocked = _block_generation(runtime)
    payload = _payload("# Committed over HTTP\n")
    token = config.bridge.token
    captures = "/v1/inaudit/captures"

    status, created = _request(base_url, token, captures, payload=payload, method="POST")
    assert status == 200, created
    assert created["committed"] is True and created["durable"] is True
    assert created["notification_pending"] is True

    capture_id = payload["capture_id"]
    first = _request(base_url, token, f"{captures}/{capture_id}/archive", payload={}, method="POST")
    retry = _request(base_url, token, f"{captures}/{capture_id}/archive", payload={}, method="POST")
    assert first[0] == 200 and first[1]["record"]["status"] == "ARCHIVED"
    assert retry[0] == 200, "500 retriable then 404 on retry: the sequence the clause forbids"
    assert retry[1]["record"]["status"] == "ARCHIVED"

    deleted = _request(base_url, token, f"{captures}/{capture_id}", method="DELETE")
    deleted_again = _request(base_url, token, f"{captures}/{capture_id}", method="DELETE")
    assert deleted[0] == 200 and deleted[1]["capture_id"] == capture_id
    assert deleted_again[0] == 200

    _unblock_generation(blocked)
    settled = _payload("# Notification recovers\n")
    status, published = _request(base_url, token, captures, payload=settled, method="POST")
    assert status == 200
    assert "notification_pending" not in published, "a settled mutation still claimed a pending notification"
