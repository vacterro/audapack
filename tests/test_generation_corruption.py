"""CORE-001 / W2-003: generation corruption is not a fresh counter.

Two notification channels (the Bridge audit generation and the browser-dispatch
generation) plus INAUDIT's owed-signal must fail closed on an existing-but-
unreadable durable file: never infer generation 0, never republish lower than
known history, keep owed notifications owed. An explicit epoch reset is what
lets a consumer adopt a recovered baseline that is numerically lower.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from audapack.bridge.browser_dispatch import BrowserDispatcher
from audapack.bridge.state import (
    GenerationStateCorruptionError,
    get_audit_generation,
    get_generation_file_path,
    increment_audit_generation,
    recover_audit_generation,
)
from audapack.inaudit_capture import InauditCaptureStore


def _corrupt(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_ = "{broken generation "
    path.write_text(bytes_, encoding="utf-8")
    return bytes_


# --------------------------------------------------------------------------- #
# Bridge audit generation
# --------------------------------------------------------------------------- #

def test_missing_generation_file_bootstraps_at_one(tmp_path: Path):
    base = tmp_path / "state"
    base.mkdir()
    assert increment_audit_generation("P", "core", base) == 1
    doc = get_audit_generation(base)
    assert doc["generation"] == 1
    assert doc["epoch"]


def test_corrupt_generation_fails_closed_and_keeps_durable_bytes(tmp_path: Path):
    base = tmp_path / "state"
    base.mkdir()
    for _ in range(5):
        increment_audit_generation("P", "core", base)
    g_file = get_generation_file_path(base)
    assert get_audit_generation(base)["generation"] == 5

    corrupt_bytes = _corrupt(g_file)

    with pytest.raises(GenerationStateCorruptionError):
        get_audit_generation(base)
    with pytest.raises(GenerationStateCorruptionError):
        increment_audit_generation("P", "core", base)

    assert g_file.read_text(encoding="utf-8") == corrupt_bytes, "corrupt bytes were overwritten"


def test_recover_quarantines_corrupt_bytes_and_starts_new_epoch(tmp_path: Path):
    base = tmp_path / "state"
    base.mkdir()
    for _ in range(5):
        increment_audit_generation("P", "core", base)
    g_file = get_generation_file_path(base)
    _corrupt(g_file)

    recovered = recover_audit_generation(base)

    assert recovered == 1
    doc = get_audit_generation(base)
    assert doc["generation"] == 1 and doc["epoch"]
    quarantined = list(base.glob("audit_generation.json.corrupt-*.json"))
    assert quarantined, "corrupt generation bytes were destroyed instead of quarantined"

    # A healthy stream is a no-op.
    assert recover_audit_generation(base) == 1


def test_concurrent_increments_remain_monotonic(tmp_path: Path):
    import threading

    base = tmp_path / "state"
    base.mkdir()
    workers = 8
    barrier = threading.Barrier(workers)

    def worker():
        barrier.wait()
        increment_audit_generation("P", "core", base)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert get_audit_generation(base)["generation"] == workers


# --------------------------------------------------------------------------- #
# Browser dispatch generation
# --------------------------------------------------------------------------- #

def test_dispatch_corrupt_generation_resets_under_new_epoch(tmp_path: Path):
    state_dir = tmp_path / "dispatch"
    dispatcher = BrowserDispatcher(state_dir=state_dir)
    dispatcher._persist_jobs()
    assert dispatcher._generation == 1
    first_epoch = dispatcher._generation_epoch
    assert first_epoch

    corrupt_bytes = _corrupt(dispatcher.generation_file)

    reopened = BrowserDispatcher(state_dir=state_dir)
    reopened._persist_jobs()

    assert reopened._generation == 1, "a corrupt file must not continue the counter from zero"
    assert reopened._generation_epoch != first_epoch, "the recovery must change the epoch"
    assert list(state_dir.glob("browser_dispatch_generation.json.corrupt-*.json"))
    assert dispatcher.generation_file.read_text(encoding="utf-8") != corrupt_bytes

    # Normal monotonic advance resumes on the recovered stream.
    reopened._persist_jobs()
    assert reopened._generation == 2
    assert reopened._generation_epoch == (reopened._generation_epoch)


def test_dispatch_normal_reload_never_regresses(tmp_path: Path):
    state_dir = tmp_path / "dispatch"
    first = BrowserDispatcher(state_dir=state_dir)
    first._persist_jobs()
    first._persist_jobs()
    assert first._generation == 2
    epoch = first._generation_epoch

    second = BrowserDispatcher(state_dir=state_dir)
    assert second._generation == 2
    assert second._generation_epoch == epoch


# --------------------------------------------------------------------------- #
# INAUDIT owed notification
# --------------------------------------------------------------------------- #

def _inaudit_payload(text: str = "corrupt generation keeps me owed\n") -> dict:
    return {
        "capture_id": str(uuid.uuid4()),
        "text": text,
        "capture_kind": "response",
        "captured_at": "2026-09-06T10:00:00Z",
        "source": "ChatGPT",
        "conversation_fingerprint": "chat-core-001",
        "project_hints": [],
    }


def test_inaudit_corrupt_generation_keeps_notification_owed(tmp_path: Path):
    root = tmp_path / "runtime"
    store = InauditCaptureStore(root)
    corrupt_bytes = _corrupt(store.generation_path)

    payload = _inaudit_payload()
    result = store.capture(payload, [])

    assert result["durable"] is True
    assert store.notification_pending is True
    assert store._owed_signal is not None, "the owed notification was dropped on corruption"
    assert store.generation_path.read_text(encoding="utf-8") == corrupt_bytes
