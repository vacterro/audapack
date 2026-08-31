from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from audapack.inaudit_capture import InauditCaptureError, InauditCaptureStore, body_sha256
from audapack.models import Project


def _seed(tmp_path: Path) -> tuple[InauditCaptureStore, dict, Project]:
    store = InauditCaptureStore(tmp_path / "runtime")
    root = tmp_path / "Project"
    root.mkdir()
    project = Project(id="project", display_name="Project", source_path=str(root))
    payload = {
        "capture_id": str(uuid.uuid4()),
        "text": "# Assignment body\nExact content\n",
        "capture_kind": "handoff",
        "source": "ChatGPT",
        "conversation_fingerprint": "chat-assignment",
    }
    store.capture(payload, [project])
    return store, payload, project


def test_existing_1_2_4_allocates_5_and_never_fills_holes(tmp_path: Path):
    store, payload, project = _seed(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    for number in (1, 2, 4):
        (audit / f"{number}.md").write_text(str(number), encoding="utf-8")
    result = store.assign(payload["capture_id"], project.id, [project])
    assigned = Path(result["assigned_path"])
    assert assigned.name == "5.md"
    assert assigned.read_text(encoding="utf-8") == payload["text"]
    assert store.get(payload["capture_id"])["record"]["status"] == "ASSIGNED"


def test_concurrent_destination_creation_recomputes_without_overwrite(tmp_path: Path, monkeypatch):
    store, payload, project = _seed(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    original_link = __import__("os").link
    raced = False

    def racing_link(source, path):
        nonlocal raced
        candidate = Path(path)
        if candidate.name == "1.md" and not raced:
            raced = True
            candidate.write_text("external writer", encoding="utf-8")
            raise FileExistsError(str(candidate))
        return original_link(source, path)

    monkeypatch.setattr("audapack.inaudit_capture.os.link", racing_link)
    result = store.assign(payload["capture_id"], project.id, [project])
    assert Path(result["assigned_path"]).name == "2.md"
    assert (audit / "1.md").read_text(encoding="utf-8") == "external writer"


def test_metadata_failure_rolls_back_owned_layer_and_keeps_inbox(tmp_path: Path, monkeypatch):
    store, payload, project = _seed(tmp_path)
    original = store._atomic_json

    def fail_assignment(path: Path, value: dict):
        if path.name == f"{payload['capture_id']}.json" and value.get("status") == "ASSIGNED":
            raise OSError("disk full")
        return original(path, value)

    monkeypatch.setattr(store, "_atomic_json", fail_assignment)
    with pytest.raises(OSError, match="disk full"):
        store.assign(payload["capture_id"], project.id, [project])
    assert not list((Path(project.source_path) / "audit").glob("*.md"))
    record = json.loads((store.inbox_dir / f"{payload['capture_id']}.json").read_text(encoding="utf-8"))
    assert record["status"] != "ASSIGNED"


def test_assign_action_runs_only_after_verified_write(tmp_path: Path):
    store, payload, project = _seed(tmp_path)
    calls: list[tuple[str, Path]] = []
    result = store.assign(
        payload["capture_id"], project.id, [project], action="GG", after_assign=lambda action, path: calls.append((action, path))
    )
    assigned = Path(result["assigned_path"])
    assert calls == [("GG", assigned)]
    assert body_sha256(assigned.read_text(encoding="utf-8")) == store.get(payload["capture_id"])["record"]["content_sha256"]
    assert result["command"] == f'saipen gg "{assigned}"'


def test_assignment_requires_registered_project_and_rejects_external_filename(tmp_path: Path):
    store, payload, project = _seed(tmp_path)
    with pytest.raises(InauditCaptureError) as caught:
        store.assign(payload["capture_id"], "../../evil", [project])
    assert caught.value.code == "unknown_project"


def test_explicit_assignment_updates_affinity_once(tmp_path: Path):
    store, payload, project = _seed(tmp_path)
    store.assign(payload["capture_id"], project.id, [project])
    affinity = store._load_affinity()["chat-assignment"]
    assert affinity["last_confirmed_project_id"] == project.id
    assert affinity["confirmed_count"] == 1


def test_published_assignment_journal_is_recovered_after_crash(tmp_path: Path):
    store, payload, project = _seed(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    target = audit / "1.md"
    target.write_text(payload["text"], encoding="utf-8")
    journal = {
        "schema_version": 1,
        "capture_id": payload["capture_id"],
        "project_id": project.id,
        "content_sha256": body_sha256(payload["text"]),
        "temp": str(audit / ".inaudit-crashed.tmp"),
        "target": str(target),
        "stage": "published",
        "assigned_at": "2026-08-31T10:00:00Z",
    }
    store._atomic_json(store.transactions_dir / f"assign-{payload['capture_id']}.json", journal)
    recovered = InauditCaptureStore(tmp_path / "runtime")
    record = recovered.get(payload["capture_id"])["record"]
    assert record["status"] == "ASSIGNED"
    assert record["assigned_path"] == str(target.resolve())
    assert not list(recovered.transactions_dir.iterdir())


def test_prepared_hardlink_is_distinguished_and_recovered_after_crash(tmp_path: Path):
    store, payload, project = _seed(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    temp = audit / ".inaudit-crashed.tmp"
    target = audit / "1.md"
    temp.write_text(payload["text"], encoding="utf-8")
    os.link(temp, target)
    journal = {
        "schema_version": 1,
        "capture_id": payload["capture_id"],
        "project_id": project.id,
        "content_sha256": body_sha256(payload["text"]),
        "temp": str(temp),
        "target": str(target),
        "stage": "prepared",
        "assigned_at": "2026-08-31T10:00:00Z",
    }
    store._atomic_json(store.transactions_dir / f"assign-{payload['capture_id']}.json", journal)

    recovered = InauditCaptureStore(tmp_path / "runtime")

    record = recovered.get(payload["capture_id"])["record"]
    assert record["status"] == "ASSIGNED"
    assert record["assigned_path"] == str(target.resolve())
    assert not temp.exists()


def test_assignment_falls_back_to_copy_when_hardlink_unsupported(tmp_path: Path, monkeypatch):
    store, payload, project = _seed(tmp_path)
    cross_volume = OSError(18, "cross-volume link not supported")

    def forbid_link(*_args, **_kwargs):
        raise cross_volume

    monkeypatch.setattr("audapack.inaudit_capture.os.link", forbid_link)
    result = store.assign(payload["capture_id"], project.id, [project])
    assigned = Path(result["assigned_path"])
    assert assigned.is_file()
    assert assigned.read_text(encoding="utf-8") == payload["text"]
    assert body_sha256(assigned.read_text(encoding="utf-8")) == store.get(payload["capture_id"])["record"]["content_sha256"]
    assert not (Path(project.source_path) / "audit" / ".inaudit-*").exists() or not any(
        (Path(project.source_path) / "audit").glob(".inaudit-*.tmp")
    )
