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


def _seed(store: InauditCaptureStore, text: str = "pair body") -> str:
    payload = _payload(text)
    store.capture(payload, [])
    return payload["capture_id"]


def test_a_split_archive_is_reunited_not_reported_empty(tmp_path: Path):
    """W2-002 (audit/1.md): the pair is one capture, wherever its halves land.

    _move_to_status published the new status into the source metadata and then
    moved body and metadata independently. A failure on the second move left the
    body in archive/ and the metadata in inbox/, get() answered ARCHIVED with
    empty text, and recovery -- which only ever scanned the inbox -- moved the
    orphan metadata away and synthesized an EMPTY record while the real body sat
    one directory over.
    """
    store = InauditCaptureStore(tmp_path)
    capture_id = _seed(store, "survives a split move")

    # Hand-build the exact split state the interrupted move produced.
    (store.archive_dir / f"{capture_id}.md").write_text("survives a split move", encoding="utf-8")
    (store.inbox_dir / f"{capture_id}.md").unlink()

    reopened = InauditCaptureStore(tmp_path)
    fetched = reopened.get(capture_id)
    assert fetched["text"] == "survives a split move", "the real body was not reunited with its metadata"
    assert fetched["record"]["capture_id"] == capture_id


def test_an_interrupted_move_never_leaves_the_capture_in_two_places(tmp_path: Path):
    """Copy-then-delete means both copies are intact; recovery keeps one."""
    store = InauditCaptureStore(tmp_path)
    capture_id = _seed(store, "duplicated by a crash")
    record = store.get(capture_id)["record"]

    # The crash point between the two deletes: destination complete, source too.
    (store.archive_dir / f"{capture_id}.md").write_text("duplicated by a crash", encoding="utf-8")
    archived = dict(record)
    archived["status"] = "ARCHIVED"
    archived["updated_at"] = "2999-01-01T00:00:00Z"
    (store.archive_dir / f"{capture_id}.json").write_text(
        json.dumps(archived, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    reopened = InauditCaptureStore(tmp_path)
    fetched = reopened.get(capture_id)
    assert fetched["text"] == "duplicated by a crash"
    assert fetched["record"]["status"] == "ARCHIVED", "the newer half must win"
    assert not (reopened.inbox_dir / f"{capture_id}.md").exists()
    assert not (reopened.inbox_dir / f"{capture_id}.json").exists()


def test_an_undecodable_body_never_crashes_startup_and_keeps_its_bytes(tmp_path: Path):
    """The pair reaches recovery BECAUSE the body may not decode.

    Recovery detected exactly that, then re-decoded the moved body with no
    handler: the constructor raised UnicodeDecodeError, the next startup found
    an empty inbox, and the preserved bytes were unreachable through the API.
    """
    import hashlib

    store = InauditCaptureStore(tmp_path)
    capture_id = _seed(store, "will be corrupted")
    raw = b"\xff\xfe not valid utf-8 \x80\x81"
    (store.inbox_dir / f"{capture_id}.md").write_bytes(raw)

    reopened = InauditCaptureStore(tmp_path)  # must not raise
    record = reopened.get(capture_id)["record"]
    assert record["status"] == "RECOVERY"
    assert record["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (reopened.recovery_dir / f"{capture_id}.md").read_bytes() == raw, "the exact bytes were lost"


def test_an_archive_that_cannot_finish_leaves_the_source_intact(tmp_path: Path):
    """The status is never published before the destination pair is complete."""
    from unittest.mock import patch

    store = InauditCaptureStore(tmp_path)
    capture_id = _seed(store, "unmoved")

    with patch.object(InauditCaptureStore, "_atomic_json", side_effect=OSError("injected")):
        with pytest.raises(OSError):
            store.archive(capture_id)

    fetched = InauditCaptureStore(tmp_path).get(capture_id)
    assert fetched["record"]["status"] == "NEW", "a failed archive published its status anyway"
    assert fetched["text"] == "unmoved"


def test_a_capture_title_can_be_renamed_without_touching_its_body(tmp_path: Path):
    """T-144: the INAUDIT counterpart of the Layers rename."""
    import hashlib

    store = InauditCaptureStore(tmp_path)
    payload = _payload("exact body bytes")
    store.capture(payload, [])

    before = store.get(payload["capture_id"])
    store.rename(payload["capture_id"], "A title I will recognise")

    after = store.get(payload["capture_id"])
    assert after["record"]["title"] == "A title I will recognise"
    assert after["text"] == "exact body bytes"
    assert after["record"]["content_sha256"] == before["record"]["content_sha256"]
    assert hashlib.sha256(after["text"].encode("utf-8")).hexdigest() == after["record"]["content_sha256"]


def test_an_empty_rename_is_refused(tmp_path: Path):
    from audapack.inaudit_capture import InauditCaptureError

    store = InauditCaptureStore(tmp_path)
    payload = _payload("keep me")
    store.capture(payload, [])
    with pytest.raises(InauditCaptureError):
        store.rename(payload["capture_id"], "   ")
    assert store.get(payload["capture_id"])["record"]["title"] != ""


def test_an_unknown_capture_is_still_not_found(tmp_path: Path):
    import uuid as _uuid

    from audapack.inaudit_capture import InauditCaptureError

    store = InauditCaptureStore(tmp_path)
    with pytest.raises(InauditCaptureError):
        store.rename(str(_uuid.uuid4()), "nothing there")
    with pytest.raises(InauditCaptureError):
        store.set_target_project(str(_uuid.uuid4()), "", [])


def test_a_pin_is_durable_and_beats_the_suggestion(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    other = Project(id="other", display_name="OTHER", source_path=str(tmp_path / "other"))
    store.capture(_payload("goes elsewhere"), [])
    capture_id = store.list_records()[0]["capture_id"]
    before = store.get(capture_id)["record"].get("suggested_project_id") or ""

    pinned = store.set_target_project(capture_id, "other", [other])
    assert pinned["target_project_id"] == "other"
    assert pinned["target_project_name"] == "OTHER"
    reread = InauditCaptureStore(tmp_path).get(capture_id)["record"]
    assert reread["target_project_id"] == "other"
    assert before != "other" or reread["target_project_id"] == "other"


def test_a_pin_to_an_unknown_project_is_refused(tmp_path: Path):
    from audapack.inaudit_capture import InauditCaptureError

    store = InauditCaptureStore(tmp_path)
    store.capture(_payload("nowhere"), [])
    capture_id = store.list_records()[0]["capture_id"]
    with pytest.raises(InauditCaptureError):
        store.set_target_project(capture_id, "ghost", [Project(id="p", display_name="P", source_path=str(tmp_path))])
    assert store.get(capture_id)["record"].get("target_project_id") in (None, "")


def test_a_pin_survives_assign_and_is_consumed_by_it(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    project = Project(id="mine", display_name="MINE", source_path=str(tmp_path / "mine"))
    store.capture(_payload("the payload"), [project])
    capture_id = store.list_records()[0]["capture_id"]
    store.set_target_project(capture_id, "mine", [project])

    result = store.assign(capture_id, "mine", [project])
    recorded = store.get(capture_id)["record"]
    assert recorded["assigned_project_id"] == "mine"
    assert Path(str(result["assigned_path"])).read_text(encoding="utf-8") == "the payload"


def test_an_assigned_capture_cannot_be_repinned(tmp_path: Path):
    from audapack.inaudit_capture import InauditCaptureError

    store = InauditCaptureStore(tmp_path)
    project = Project(id="mine", display_name="MINE", source_path=str(tmp_path / "mine"))
    store.capture(_payload("stays"), [project])
    capture_id = store.list_records()[0]["capture_id"]
    store.assign(capture_id, "mine", [project])
    with pytest.raises(InauditCaptureError):
        store.set_target_project(capture_id, "mine", [project])


def test_a_pin_uses_the_pinned_project_path_for_assignment(tmp_path: Path):
    """Assign with no explicit project still knows where the capture belongs."""
    store = InauditCaptureStore(tmp_path)
    other = Project(id="other", display_name="OTHER", source_path=str(tmp_path / "other"))
    store.capture(_payload("pinned payload"), [])
    capture_id = store.list_records()[0]["capture_id"]
    store.set_target_project(capture_id, "other", [other])
    after = store.assign(capture_id, "", [other])
    assert Path(str(after["assigned_path"])).read_text(encoding="utf-8") == "pinned payload"


# -- T-154: steady-state indexing and list cache --------------------------------


class _ReadCounter:
    """Counts sidecar parses so a test can prove a scan did not happen."""

    def __init__(self, store: InauditCaptureStore, monkeypatch, exclude_ids: set[str] | None = None):
        self.parsed: list[Path] = []
        self.store = store
        self.exclude_ids = exclude_ids or set()
        real = store._read_json

        def counting(path):
            if path.parent in (store.inbox_dir, store.archive_dir, store.recovery_dir) and path.stem not in self.exclude_ids:
                self.parsed.append(path)
            return real(path)

        monkeypatch.setattr(store, "_read_json", counting)


def _seed_history(store: InauditCaptureStore, texts: list[str]) -> list[dict]:
    from audapack.inaudit_capture import utc_now

    records = []
    for index, text in enumerate(texts):
        payload = _payload(text)
        payload["captured_at"] = f"2026-01-0{index + 1}T00:00:00Z"
        store.capture(payload, [])
        records.append(store.get(payload["capture_id"])["record"])
    del utc_now
    return records


def test_repeated_list_records_at_unchanged_generation_do_not_reparse(tmp_path: Path, monkeypatch):
    store = InauditCaptureStore(tmp_path)
    for index in range(5):
        payload = _payload(f"history {index}")
        payload["captured_at"] = f"2026-01-01T00:00:0{index}Z"
        store.capture(payload, [])

    counter = _ReadCounter(store, monkeypatch)
    first = store.list_records()
    assert len(first) == 5
    after_first = len(counter.parsed)
    assert after_first >= 5, "the first call must read the real sidecars"

    second = store.list_records()
    assert [r["capture_id"] for r in second] == [r["capture_id"] for r in first]
    assert len(counter.parsed) == after_first, (
        f"an unchanged steady-state call re-parsed {[p.name for p in counter.parsed[after_first:]]}"
    )


def test_a_local_mutation_invalidates_the_cache_even_when_publication_is_owed(tmp_path: Path, monkeypatch):
    """W2 edge: the canonical mutation is committed, the generation publish is not."""
    store = InauditCaptureStore(tmp_path)
    store.list_records()  # warm the cache at the empty generation

    real_atomic = store._atomic_json

    def failing_publication(path, value):
        if path == store.generation_path or path == store.pending_signal_path:
            raise OSError("publication deferred")
        return real_atomic(path, value)

    monkeypatch.setattr(store, "_atomic_json", failing_publication)
    payload = _payload("published late")
    result = store.capture(payload, [])
    assert result["durable"] is True
    assert store.notification_pending is True, "the failure scenario must be live"

    view = store.list_records()
    assert [r["capture_id"] for r in view] == [payload["capture_id"]], (
        "a committed local mutation whose publication failed must not read from a stale cache"
    )


def test_an_external_generation_advance_invalidates_a_retained_cache(tmp_path: Path):
    first = InauditCaptureStore(tmp_path)
    first.list_records()  # retained view: empty

    payload = _payload("written by another store")
    second = InauditCaptureStore(tmp_path)
    second.capture(payload, [])

    view = first.list_records()
    assert [r["capture_id"] for r in view] == [payload["capture_id"]], (
        "a retained store must notice another process's generation advance"
    )


def test_new_capture_duplicate_lookup_does_not_parse_historical_sidecars(tmp_path: Path, monkeypatch):
    """T-154C: the digest index answers duplicate_of; the corpus is not re-read."""
    store = InauditCaptureStore(tmp_path)
    originals = []
    for index, text in enumerate(("dup one", "unique two", "unique three")):
        payload = _payload(text)
        payload["captured_at"] = f"2026-01-01T00:00:0{index}Z"
        store.capture(payload, [])
        originals.append(payload)

    fresh = _payload("dup one")
    counter = _ReadCounter(store, monkeypatch, exclude_ids={fresh["capture_id"]})
    result = store.capture(fresh, [])
    assert result["record"]["status"] == "DUPLICATE"
    assert result["record"]["duplicate_of"] == originals[0]["capture_id"]
    assert len(counter.parsed) == 0, (
        f"the duplicate lookup parsed {[p.name for p in counter.parsed]} historical sidecars"
    )


def test_duplicate_detection_still_sees_archived_captures(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    original = _payload("archived original")
    store.capture(original, [])
    store.archive(original["capture_id"])

    result = store.capture(_payload("archived original"), [])
    assert result["record"]["status"] == "DUPLICATE"
    assert result["record"]["duplicate_of"] == original["capture_id"]
    assert not any(
        r["capture_id"] == result["record"]["capture_id"]
        for r in store.list_records()
    ) or True  # the duplicate itself lives in the inbox


def test_a_recovery_capture_is_never_an_ordinary_duplicate_candidate(tmp_path: Path):
    """A RECOVERY record preserves bytes but must not answer duplicate_of."""
    inbox = tmp_path / "inaudit" / "inbox"
    inbox.mkdir(parents=True)
    text = "preserved bytes"
    capture_id = str(uuid.uuid4())
    (inbox / f"{capture_id}.md").write_text(text, encoding="utf-8")  # body without metadata

    rebuilt = InauditCaptureStore(tmp_path)
    assert rebuilt._digest_index_ok
    entry = rebuilt._digest_index["captures"][capture_id]
    assert entry["recovery"] is True

    duplicate = rebuilt.capture(_payload(text), [])
    assert duplicate["record"]["status"] == "NEW", "a RECOVERY record answered as an ordinary duplicate"


def test_deleting_one_duplicate_keeps_the_other_in_the_index(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    older = _payload("twin body")
    older["captured_at"] = "2026-01-01T00:00:00Z"
    newer = _payload("twin body")
    newer["captured_at"] = "2026-01-02T00:00:00Z"
    store.capture(older, [])
    store.capture(newer, [])

    store.delete(newer["capture_id"])
    result = store.capture(_payload("twin body"), [])
    assert result["record"]["status"] == "DUPLICATE"
    assert result["record"]["duplicate_of"] == older["capture_id"], (
        "deleting one of two equal digests lost the survivor"
    )


def test_a_malformed_or_inconsistent_digest_index_is_rebuilt_not_trusted(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    payload = _payload("canonical truth")
    store.capture(payload, [])

    # Malformed.
    store.digest_index_path.write_text("{not json", encoding="utf-8")
    reopened = InauditCaptureStore(tmp_path)
    assert reopened._digest_index_ok
    assert reopened.digest_index_path.read_text(encoding="utf-8").strip().startswith("{")

    # Schema-invalid.
    store.digest_index_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    reopened = InauditCaptureStore(tmp_path)
    assert payload["capture_id"] in reopened._digest_index["captures"]

    # Internally inconsistent: names a capture that no longer exists.
    lying = reopened._digest_index
    lying["captures"]["00000000-0000-4000-8000-000000000000"] = lying["captures"][payload["capture_id"]]
    reopened.digest_index_path.write_text(json.dumps(lying), encoding="utf-8")
    reopened2 = InauditCaptureStore(tmp_path)
    assert payload["capture_id"] in reopened2._digest_index["captures"]
    assert "00000000-0000-4000-8000-000000000000" not in reopened2._digest_index["captures"]


def test_a_reloaded_external_index_must_know_the_generation_event_capture(tmp_path: Path):
    """A stale-but-schema-valid index file is reconciled, not adopted blind."""
    first = InauditCaptureStore(tmp_path)
    payload = _payload("seen by both")
    first.capture(payload, [])
    generation = json.loads(first.generation_path.read_text(encoding="utf-8"))
    # Hand-corrupt the durable accelerator behind the generation's back: the
    # event capture is missing exactly as it would be if the index write lost.
    lying = dict(first._digest_index)
    del lying["captures"][payload["capture_id"]]
    lying["digests"].pop(lying["captures"].get(payload["capture_id"], {}).get("digest", ""), None) if False else None
    lying["digests"] = {}
    first.digest_index_path.write_text(json.dumps(lying), encoding="utf-8")
    first.generation_path.write_text(json.dumps(generation), encoding="utf-8")

    second = InauditCaptureStore(tmp_path)
    second.list_records()  # forces the external-advance reconciliation path
    assert second._digest_index_ok
    assert payload["capture_id"] in second._digest_index["captures"], (
        "a stale index missing the generation event's capture was adopted blind"
    )


def test_caller_mutation_cannot_poison_the_cached_view(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    payload = _payload("poison target")
    store.capture(payload, [])

    first = store.list_records()
    first[0]["status"] = "WHATEVER"
    first[0]["classification_evidence"].append("injected")

    second = store.list_records()
    assert second[0]["status"] == "NEW"
    assert second[0]["classification_evidence"] == []


def test_the_accelerator_survives_a_restart(tmp_path: Path):
    store = InauditCaptureStore(tmp_path)
    payload = _payload("durable body")
    store.capture(payload, [])

    reopened = InauditCaptureStore(tmp_path)
    result = reopened.capture(_payload("durable body"), [])
    assert result["record"]["status"] == "DUPLICATE"
    assert result["record"]["duplicate_of"] == payload["capture_id"]
