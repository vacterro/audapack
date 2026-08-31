from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtWidgets import QApplication

from audapack.config import AppConfig
from audapack.inaudit_capture import InauditCaptureStore
from audapack.models import Project
from audapack.ui_qt.dialogs.inaudit_widget import InauditWidget


def _widget(tmp_path: Path) -> tuple[InauditWidget, InauditCaptureStore, Project]:
    project_root = tmp_path / "AUDAPACK"
    project_root.mkdir()
    project = Project(id="audapack", display_name="AUDAPACK", source_path=str(project_root))
    config = AppConfig(projects=[project])
    store = InauditCaptureStore(tmp_path / "runtime")
    widget = InauditWidget(config_provider=lambda: config, capture_store=store)
    widget.set_project(project)
    return widget, store, project


def test_inaudit_has_layers_and_inbox_tabs(qapp, tmp_path: Path):
    widget, _store, _project = _widget(tmp_path)
    assert [widget.mode_tabs.tabText(index) for index in range(widget.mode_tabs.count())] == ["Layers", "Inbox"]
    assert widget.inbox_header.text() == "INAUDIT INBOX 0 · ?0"
    widget.deleteLater()


def test_ia_plus_captures_clipboard_through_canonical_store(qapp, tmp_path: Path):
    widget, store, _project = _widget(tmp_path)
    QApplication.clipboard().setText("# Clipboard audit\nExact clipboard body")
    widget._on_clipboard_capture()
    records = store.list_records()
    assert len(records) == 1
    assert records[0]["source"] == "clipboard"
    assert store.get(records[0]["capture_id"])["text"] == "# Clipboard audit\nExact clipboard body"
    assert "durable Inbox write verified" in widget.inbox_status.text()
    widget.deleteLater()


def test_assign_plus_gg_copies_only_canonical_assigned_path(qapp, tmp_path: Path, monkeypatch):
    widget, store, project = _widget(tmp_path)
    copied: list[str] = []
    monkeypatch.setattr(widget, "_copy_text", lambda text: copied.append(text) or True)
    payload = {
        "capture_id": str(uuid.uuid4()),
        "text": "# AUDAPACK task\nBody",
        "capture_kind": "handoff",
        "source": "ChatGPT",
    }
    store.capture(payload, [project])
    widget.refresh_inbox()
    widget._on_assign_capture("GG")
    assigned = Path(project.source_path) / "audit" / "1.md"
    assert assigned.read_text(encoding="utf-8") == payload["text"]
    assert copied == [f'saipen gg "{assigned}"']
    assert store.get(payload["capture_id"])["record"]["assigned_path"] == str(assigned)
    widget.deleteLater()


def test_inbox_detail_shows_suggestion_evidence_and_destination(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    payload = {
        "capture_id": str(uuid.uuid4()),
        "text": f"# Fix AUDAPACK\nPath: {project.source_path}",
        "capture_kind": "response",
        "source": "ChatGPT",
        "browser_name": "Brave",
    }
    store.capture(payload, [project])
    widget.refresh_inbox()
    detail = widget.inbox_detail.toPlainText()
    assert "Suggested: AUDAPACK 100%" in detail
    assert "exact path" in detail
    assert str(Path(project.source_path) / "audit" / "1.md") in detail
    widget.deleteLater()


def test_unassigned_capture_requires_explicit_project_choice(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    payload = {
        "capture_id": str(uuid.uuid4()),
        "text": "Generic implementation notes without an owner",
        "capture_kind": "response",
        "source": "ChatGPT",
    }
    store.capture(payload, [project])
    widget.refresh_inbox()
    assert widget.inbox_project.currentData() == ""
    assert not widget.btn_assign.isEnabled()

    widget.inbox_project.setCurrentIndex(1)

    assert widget.inbox_project.currentData() == project.id
    assert widget.btn_assign.isEnabled()
    assert str(Path(project.source_path) / "audit" / "1.md") in widget.inbox_detail.toPlainText()
    widget.deleteLater()


def test_inbox_counter_excludes_assigned_history(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    payload = {
        "capture_id": str(uuid.uuid4()),
        "text": "# AUDAPACK assignment",
        "capture_kind": "response",
        "source": "ChatGPT",
    }
    store.capture(payload, [project])
    store.assign(payload["capture_id"], project.id, [project])
    widget.refresh_inbox()
    assert widget.inbox_header.text() == "INAUDIT INBOX 0 · ?0"
    assert "ASSIGNED" in widget.inbox_list.item(0).text()
    widget.deleteLater()
