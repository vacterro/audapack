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


def test_consumed_layer_disappears_and_foreign_notes_remain(qapp, tmp_path):
    widget, _store, project = _widget(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    layer = audit / "1.md"
    layer.write_text("audit", encoding="utf-8")
    (audit / "notes.md").write_text("keep", encoding="utf-8")
    widget._on_debounced_fs()
    assert widget.list.count() == 1
    layer.unlink()
    widget._on_debounced_fs()
    assert widget.list.count() == 0
    assert not widget.editor.toPlainText()
    assert (audit / "notes.md").read_text() == "keep"
    assert str(Path(project.source_path).resolve()) in widget._watcher.directories()
    widget.deleteLater()


def test_consumption_preserves_draft_and_save_does_not_recreate_layer(qapp, tmp_path):
    widget, _store, project = _widget(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    layer = audit / "1.md"
    layer.write_text("audit", encoding="utf-8")
    widget.refresh()
    widget.editor.setPlainText("unsaved draft")
    layer.unlink()
    widget._on_debounced_fs()
    widget._on_save()
    assert widget.editor.toPlainText() == "unsaved draft"
    assert widget._dirty
    assert not layer.exists()
    assert "Save refused" in widget.status.text()
    widget._on_reload()
    widget.refresh()
    assert widget.list.count() == 0
    widget.deleteLater()


def test_layer_commands_use_bare_cc(qapp, tmp_path):
    widget, _store, project = _widget(tmp_path)
    audit = Path(project.source_path) / "audit"
    audit.mkdir()
    (audit / "1.md").write_text("audit", encoding="utf-8")
    widget.refresh()
    for action in (widget._on_gg, widget._on_cc):
        action()
        assert QApplication.clipboard().text() == "saipen cc"
    widget.deleteLater()


def test_managed_assignment_runs_off_gui_thread(qapp, tmp_path, monkeypatch):
    import threading
    import time

    from PySide6.QtTest import QTest

    from tests.test_saipen_transport import _bind, _fake_cli

    widget, store, project = _widget(tmp_path)
    home, _calls = _fake_cli(tmp_path, monkeypatch)
    _bind(Path(project.source_path), home)
    capture_id = str(uuid.uuid4())
    store.capture({"capture_id": capture_id, "text": "Audit for AUDAPACK", "source": "desktop"}, [project])
    widget.refresh_inbox()
    widget.inbox_project.setCurrentIndex(widget.inbox_project.findData(project.id))
    original = store.assign
    release = threading.Event()
    entered = threading.Event()
    thread_ids = []

    def assign(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "GUI did not release the worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "assign", assign)
    try:
        widget._on_assign_capture("CC")
        assert entered.wait(2)
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != threading.get_ident()
        assert not widget.inbox_page.isEnabled()
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while widget._task_runner.is_running("inaudit:assign") and time.monotonic() < deadline:
        QTest.qWait(10)
    assert not widget._task_runner.is_running("inaudit:assign")
    assert widget.inbox_page.isEnabled()
    assert "CC command copied" in widget.inbox_status.text()
    assert QApplication.clipboard().text() == "saipen cc"
    assert widget.list.count() == 1
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
    assert copied == ["saipen cc"]
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

def _new_capture(store, text: str, projects: list[Project] | None = None) -> str:
    """One real durable capture; the status comes from the store's own rules."""
    capture_id = str(uuid.uuid4())
    store.capture(
        {
            "capture_id": capture_id,
            "text": text,
            "capture_kind": "response",
            "source": "t276",
            "project_hints": [],
        },
        projects or [],
    )
    return capture_id

def _rows(widget) -> list[str]:
    return [widget.inbox_list.item(row).text() for row in range(widget.inbox_list.count())]

def _assigned_capture(store, project) -> str:
    capture_id = _new_capture(store, "# AUDAPACK assignment", [project])
    store.assign(capture_id, project.id, [project])
    return capture_id

def test_inbox_counter_excludes_assigned_history(qapp, tmp_path: Path):
    """T-276 replaces the old contract: excluded from the count AND from the rows.

    The old assertion pinned the clutter as intended behaviour -- the counter
    said zero while the list showed the ASSIGNED record forever. The recovery
    guarantee the old test also carried (the durable record survives) is kept
    and asserted here, in the store, not only in the view.
    """
    widget, store, project = _widget(tmp_path)
    capture_id = _assigned_capture(store, project)
    widget.refresh_inbox()
    assert widget.inbox_header.text() == "INAUDIT INBOX 0 · ?0"
    assert _rows(widget) == ["No pending INAUDIT captures."]
    assert [record["capture_id"] for record in store.list_records()] == [capture_id]
    widget.deleteLater()

def test_history_shows_the_assigned_capture_and_keeps_it_durable(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    capture_id = _assigned_capture(store, project)
    widget.set_inbox_view("HISTORY")

    assert widget.inbox_header.text() == "INAUDIT HISTORY 1"
    assert len(_rows(widget)) == 1
    assert "ASSIGNED" in _rows(widget)[0]
    assert [record["capture_id"] for record in store.list_records()] == [capture_id]
    body = store.get(capture_id)
    assert body["record"]["status"] == "ASSIGNED"
    assert "AUDAPACK assignment" in body["text"]
    widget.deleteLater()

def test_a_duplicate_leaves_the_inbox_and_stays_in_history(qapp, tmp_path: Path):
    widget, _store, _project = _widget(tmp_path)
    store = widget._capture_store
    _new_capture(store, "# the same bytes twice")
    duplicate_id = _new_capture(store, "# the same bytes twice")

    assert store.get(duplicate_id)["record"]["status"] == "DUPLICATE"
    widget.refresh_inbox()
    assert widget.inbox_header.text() == "INAUDIT INBOX 1 · ?1"
    assert all("DUPLICATE" not in row for row in _rows(widget))

    widget.set_inbox_view("HISTORY")
    assert widget.inbox_header.text() == "INAUDIT HISTORY 1"
    assert "DUPLICATE" in _rows(widget)[0]
    assert store.get(duplicate_id)["record"]["status"] == "DUPLICATE"
    widget.deleteLater()

def test_actionable_captures_stay_in_the_inbox_and_in_its_count(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    _new_capture(store, "# unclassified new capture")
    _new_capture(store, "# AUDAPACK suggested capture", [project])
    _assigned_capture(store, project)
    widget.refresh_inbox()

    statuses = [record["status"] for record in store.list_records()]
    assert "NEW" in statuses and "ASSIGNED" in statuses
    assert widget.inbox_header.text() == "INAUDIT INBOX 2 · ?1"
    assert len(_rows(widget)) == 2
    assert all("ASSIGNED" not in row.split()[0] for row in _rows(widget))
    widget.deleteLater()

def test_both_counters_count_their_own_classification(qapp, tmp_path: Path):
    from audapack.inaudit_capture import capture_presentation_bucket

    widget, store, project = _widget(tmp_path)
    _new_capture(store, "# one actionable")
    _assigned_capture(store, project)
    _new_capture(store, "# duplicate me")
    _new_capture(store, "# duplicate me")
    records = store.list_records()
    active = [r for r in records if capture_presentation_bucket(r).value == "INBOX"]
    history = [r for r in records if capture_presentation_bucket(r).value == "HISTORY"]

    widget.refresh_inbox()
    assert widget.inbox_header.text() == f"INAUDIT INBOX {len(active)} · ?{len(active)}"
    assert widget.inbox_list.count() == len(active)
    widget.set_inbox_view("HISTORY")
    assert widget.inbox_header.text() == f"INAUDIT HISTORY {len(history)}"
    assert widget.inbox_list.count() == len(history)
    widget.deleteLater()

def test_switching_views_never_touches_a_record(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    _new_capture(store, "# stays")
    _assigned_capture(store, project)
    before = store.list_records()

    widget.set_inbox_view("HISTORY")
    widget.set_inbox_view("INBOX")
    widget.set_inbox_view("HISTORY")
    after = store.list_records()

    assert [(r["capture_id"], r["status"]) for r in before] == [
        (r["capture_id"], r["status"]) for r in after
    ]
    widget.deleteLater()

def test_a_refresh_keeps_the_view_the_operator_chose(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    _assigned_capture(store, project)
    widget.set_inbox_view("HISTORY")
    assert widget.inbox_view == "HISTORY"

    _new_capture(store, "# arrives while history is open")
    widget.refresh_inbox()

    assert widget.inbox_view == "HISTORY", "refresh jumped the operator back to the inbox"
    assert widget.inbox_header.text() == "INAUDIT HISTORY 1"
    widget.set_inbox_view("INBOX")
    assert len(_rows(widget)) == 1
    widget.deleteLater()

def test_assigning_the_selected_record_moves_it_out_without_stale_actions(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    capture_id = _new_capture(store, "# AUDAPACK assign me")
    widget.refresh_inbox()
    assert widget.inbox_list.count() == 1
    widget.inbox_list.setCurrentRow(0)
    widget.inbox_project.setCurrentIndex(1)
    assert widget.btn_assign.isEnabled()

    widget._on_assign_capture("")

    assert store.get(capture_id)["record"]["status"] == "ASSIGNED"
    assert _rows(widget) == ["No pending INAUDIT captures."]
    assert widget.inbox_header.text() == "INAUDIT INBOX 0 · ?0"
    assert widget._selected_capture() is None
    assert not widget.btn_assign.isEnabled(), "assign stayed bound to a record no longer in the inbox"
    assert not widget.btn_pin.isEnabled()
    assert widget.inbox_detail.toPlainText() == ""
    widget.set_inbox_view("HISTORY")
    assert len(_rows(widget)) == 1
    widget.deleteLater()

def test_a_history_record_cannot_be_assigned_again(qapp, tmp_path: Path):
    """Visibility in History must not enable an action that is invalid there."""
    widget, store, project = _widget(tmp_path)
    _assigned_capture(store, project)
    widget.set_inbox_view("HISTORY")
    widget.inbox_list.setCurrentRow(0)
    widget.inbox_project.setCurrentIndex(1)

    assert not widget.btn_assign.isEnabled()
    assert not widget.btn_assign_gg.isEnabled()
    assert not widget.btn_pin.isEnabled()
    assert widget.btn_capture_copy.isEnabled(), "reading a historical record stays legal"
    widget.deleteLater()

def test_the_inbox_empty_state_is_quiet_and_not_archaeology(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    _assigned_capture(store, project)
    widget.refresh_inbox()

    assert _rows(widget) == ["No pending INAUDIT captures."]
    # The placeholder is a message, not a selectable record.
    widget.inbox_list.setCurrentRow(0)
    assert widget._selected_capture() is None
    assert not widget.btn_assign.isEnabled()
    widget.set_inbox_view("HISTORY")
    assert "ASSIGNED" in _rows(widget)[0]
    widget.deleteLater()

def test_an_archived_capture_is_history_not_inbox_and_survives(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    capture_id = _assigned_capture(store, project)
    store.archive(capture_id)

    widget.refresh_inbox()
    assert widget.inbox_header.text() == "INAUDIT INBOX 0 · ?0"
    assert _rows(widget) == ["No pending INAUDIT captures."]

    widget.set_inbox_view("HISTORY")
    assert widget.inbox_header.text() == "INAUDIT HISTORY 1"
    assert "ARCHIVED" in _rows(widget)[0]
    assert store.get(capture_id)["record"]["status"] == "ARCHIVED"
    widget.deleteLater()

def test_an_empty_history_says_so(qapp, tmp_path: Path):
    widget, store, _project = _widget(tmp_path)
    _new_capture(store, "# actionable only")
    widget.set_inbox_view("HISTORY")

    assert _rows(widget) == ["No capture history."]
    assert widget.inbox_header.text() == "INAUDIT HISTORY 0"
    widget.deleteLater()

def test_every_lifecycle_status_has_a_presentation_bucket():
    """The one classification contract, checked against the real status set."""
    from audapack.inaudit_capture import (
        CAPTURE_STATUSES,
        CapturePresentationBucket,
        capture_presentation_bucket,
        is_inbox_actionable,
    )

    assert CAPTURE_STATUSES == {"NEW", "SUGGESTED", "ASSIGNED", "ARCHIVED", "DUPLICATE", "RECOVERY"}
    for status in CAPTURE_STATUSES:
        assert isinstance(capture_presentation_bucket({"status": status}), CapturePresentationBucket)
    history = {
        status for status in CAPTURE_STATUSES
        if capture_presentation_bucket({"status": status}) is CapturePresentationBucket.HISTORY
    }
    inbox = {
        status for status in CAPTURE_STATUSES
        if capture_presentation_bucket({"status": status}) is CapturePresentationBucket.INBOX
    }
    assert history | inbox == CAPTURE_STATUSES, "a status has no presentation bucket"
    assert not history & inbox, "a status is in both views"
    assert history == {"ASSIGNED", "DUPLICATE", "ARCHIVED"}, history
    assert inbox == {"NEW", "SUGGESTED", "RECOVERY"}, inbox
    assert all(is_inbox_actionable({"status": status}) == (status in inbox) for status in CAPTURE_STATUSES)

def test_an_unknown_or_missing_status_fails_safe_into_the_inbox():
    from audapack.inaudit_capture import CapturePresentationBucket, capture_presentation_bucket, is_inbox_actionable

    for record in (
        {"status": "COMPLETED_LATER"},
        {"status": ""},
        {},
        None,
        {"status": "recovery"},
    ):
        assert capture_presentation_bucket(record) == CapturePresentationBucket.INBOX
        assert is_inbox_actionable(record) is True

def test_recovery_records_are_actionable_not_history():
    from audapack.inaudit_capture import capture_presentation_bucket, is_inbox_actionable

    assert capture_presentation_bucket({"status": "RECOVERY"}).value == "INBOX"
    assert is_inbox_actionable({"status": "RECOVERY"}) is True

def _capture(widget, store, text="# pinned capture"):
    widget.inbox_project.setCurrentIndex(0)
    widget._capture_store.capture(
        {
            "capture_id": str(uuid.uuid4()),
            "text": text,
            "capture_kind": "response",
            "source": "widget-test",
            "project_hints": [],
        },
        [],
    )
    widget.refresh_inbox()
    assert store.list_records()
    return store.list_records()[0]["capture_id"]

def test_pin_records_the_hand_chosen_project_durably(qapp, tmp_path: Path):
    """T-145: the operator's answer, not the classifier's guess."""
    widget, store, project = _widget(tmp_path)
    capture_id = _capture(widget, store)

    index = widget.inbox_project.findData(project.id)
    assert index > 0
    widget.inbox_project.setCurrentIndex(index)
    widget._on_pin_project()

    reread = store.get(capture_id)["record"]
    assert reread["target_project_id"] == project.id
    assert reread["target_project_name"] == "AUDAPACK"
    assert "pinned" in widget.inbox_status.text().lower() or "Pinned" in widget.inbox_status.text()
    rows = [
        widget.inbox_list.item(row).text()
        for row in range(widget.inbox_list.count())
    ]
    assert any("*AUDAPACK" in text for text in rows), rows

    widget.deleteLater()

def test_pin_selects_the_project_like_a_suggestion_did(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    _capture(widget, store)

    # The classifier suggested nothing; the combo starts blank.
    assert widget.inbox_project.currentData() == ""


    widget.inbox_project.setCurrentIndex(widget.inbox_project.findData(project.id))
    widget._on_pin_project()
    widget.refresh_inbox()

    # And the row now opens with the pinned project preselected.
    assert widget.inbox_project.currentData() == project.id
    widget.deleteLater()

def test_a_pin_is_cleared_by_pinning_nothing(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    capture_id = _capture(widget, store)

    widget.inbox_project.setCurrentIndex(widget.inbox_project.findData(project.id))
    widget._on_pin_project()
    assert store.get(capture_id)["record"]["target_project_id"] == project.id

    widget.inbox_project.setCurrentIndex(0)
    widget._on_pin_project()
    assert store.get(capture_id)["record"]["target_project_id"] == ""
    widget.deleteLater()

def test_a_capture_title_is_renamed_through_the_ui(qapp, tmp_path: Path, monkeypatch):
    widget, store, _project = _widget(tmp_path)
    capture_id = _capture(widget, store)
    monkeypatch.setattr(
        "audapack.ui_qt.dialogs.inaudit_widget.QInputDialog.getText",
        lambda *args, **kwargs: ("A title I will recognise", True),
    )
    widget._on_rename_capture()

    assert store.get(capture_id)["record"]["title"] == "A title I will recognise"
    rows = [
        widget.inbox_list.item(row).text()
        for row in range(widget.inbox_list.count())
    ]
    assert any("A title I will recognise" in text for text in rows), rows
    widget.deleteLater()

def test_a_layer_is_renumbered_onto_a_free_number(qapp, tmp_path: Path, monkeypatch):
    from audapack.inaudit import list_inaudit_layers

    widget, _store, project = _widget(tmp_path)
    source = tmp_path / "AUDAPACK" / "audit"
    source.mkdir(parents=True)
    (source / "1.md").write_text("first", encoding="utf-8")
    (source / "2.md").write_text("second", encoding="utf-8")
    widget.refresh()

    widget.list.setCurrentRow(0)
    monkeypatch.setattr(
        "audapack.ui_qt.dialogs.inaudit_widget.QInputDialog.getInt",
        lambda *args, **kwargs: (5, True),
    )
    widget._on_rename_layer()

    assert sorted(item.number for item in list_inaudit_layers(project)) == [2, 5]
    assert (source / "5.md").read_text(encoding="utf-8") == "first"
    assert widget.status.text() == "Layer 1 is now 5.md"
    widget.deleteLater()

def test_a_layer_is_never_renumbered_onto_a_taken_number(qapp, tmp_path: Path, monkeypatch):
    from audapack.inaudit import list_inaudit_layers

    widget, _store, project = _widget(tmp_path)
    source = tmp_path / "AUDAPACK" / "audit"
    source.mkdir(parents=True)
    (source / "1.md").write_text("first", encoding="utf-8")
    (source / "2.md").write_text("second", encoding="utf-8")
    widget.refresh()

    widget.list.setCurrentRow(0)
    monkeypatch.setattr(
        "audapack.ui_qt.dialogs.inaudit_widget.QInputDialog.getInt",
        lambda *args, **kwargs: (2, True),
    )
    widget._on_rename_layer()

    assert sorted(item.number for item in list_inaudit_layers(project)) == [1, 2]
    assert (source / "1.md").read_text(encoding="utf-8") == "first"
    assert (source / "2.md").read_text(encoding="utf-8") == "second"
    assert "failed" in widget.status.text().lower()
    widget.deleteLater()

def test_assign_uses_the_pinned_project(qapp, tmp_path: Path):
    """The pin is what Assign acts on: no second decision to get wrong."""
    other_root = tmp_path / "OTHER"
    other_root.mkdir()
    widget, store, project = _widget(tmp_path)
    other = Project(id="other", display_name="OTHER", source_path=str(other_root))
    config = AppConfig(projects=[project, other])
    widget._config_provider = lambda: config

    capture_id = _capture(widget, store, "# goes to OTHER")
    widget.inbox_project.setCurrentIndex(widget.inbox_project.findData(other.id))
    widget._on_pin_project()
    widget.refresh_inbox()

    # Nothing re-chosen: the row opened with the pin already selected.
    assert widget.inbox_project.currentData() == other.id
    widget._on_assign_capture("")

    assigned = store.get(capture_id)["record"].get("assigned_path") or ""
    assert assigned, widget.inbox_status.text()
    assert str(other_root) in assigned, assigned
    assert (other_root / "audit" / "1.md").read_text(encoding="utf-8") == "# goes to OTHER"
    widget.deleteLater()

def test_a_pin_is_refused_once_the_capture_is_assigned(qapp, tmp_path: Path):
    widget, store, project = _widget(tmp_path)
    capture_id = _capture(widget, store, "# already placed")
    widget.inbox_project.setCurrentIndex(widget.inbox_project.findData(project.id))
    widget._on_assign_capture("")
    assert store.get(capture_id)["record"].get("assigned_path")

    # T-276: the assigned capture has left the Inbox, so the only place it can
    # even be selected is History -- and pinning it there must still be refused
    # by the store, not merely hidden behind a disabled button.
    widget.set_inbox_view("HISTORY")
    widget.inbox_list.setCurrentRow(0)
    assert widget._selected_capture()["capture_id"] == capture_id
    assert not widget.btn_pin.isEnabled(), "a history record still offered a pin"
    widget._on_pin_project()
    assert "failed" in widget.inbox_status.text().lower(), widget.inbox_status.text()
    assert not store.get(capture_id)["record"].get("target_project_id")
    widget.deleteLater()
