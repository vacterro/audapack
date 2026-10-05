"""INAUDIT operator-path reliability (T-174).

The rule under test:

    AN ENABLED NORMAL-PATH CONTROL MUST NOT BE GUARANTEED TO FAIL BY DESIGN.

T-165 replaced the managed [+] popup with the embedded bottom editor but also
dropped the managed capture detour, so on a SAIPEN-managed project [+] now
hits ensure_next_layer()'s (correct) is_managed guard and reports
"Create failed". The guard stays; [+] must instead enter a managed DRAFT.

The same class of defect exists around DIRTY editor state: [+], Rename, row
selection and project switching could mutate filesystem state or silently
discard a draft, and a refused project bind could still let [+]/[edit] run
against the previously bound project.

Both an UNMANAGED and a SAIPEN-MANAGED project are exercised.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from audapack.config import AppConfig, AuditsConfig  # noqa: E402
from audapack.models import Project  # noqa: E402
from audapack.services.project_service import ProjectService  # noqa: E402
from audapack.ui_qt.main_window import MainWindow  # noqa: E402


class _FakeStore:
    """Minimal InauditCaptureStore stand-in recording the operator path.

    It mirrors the real contract that matters here: capture() is idempotent
    for (capture_id, content digest) and raises capture_id_conflict
    otherwise; assign() routes managed projects to a SAIPEN enqueue that
    returns the SAIPEN-chosen layer path.
    """

    def __init__(self, root: Path, enqueue=None):
        self.root = root
        self.captures: list[dict] = []
        self.assigns: list[dict] = []
        self._captured: dict[str, str] = {}
        self._enqueue = enqueue
        self.fail_enqueue = False
        self.generation_path = root / "generation.json"

    def capture(self, payload, projects):
        capture_id = str(payload["capture_id"])
        text = payload["text"]
        if capture_id in self._captured:
            if self._captured[capture_id] != text:
                from audapack.inaudit_capture import InauditCaptureError

                raise InauditCaptureError(
                    "capture_id_conflict", "capture_id already exists with different content"
                )
            return {"record": {"capture_id": capture_id}, "duplicate": True, "durable": True}
        self._captured[capture_id] = text
        self.captures.append({"capture_id": capture_id, "text": text, **payload})
        return {"record": {"capture_id": capture_id}, "duplicate": False, "durable": True}

    def assign(self, capture_id, project_id, projects, action="", after_assign=None):
        self.assigns.append({"capture_id": capture_id, "project_id": project_id, "action": action})
        if self.fail_enqueue:
            from audapack.inaudit_capture import InauditCaptureError

            raise InauditCaptureError("enqueue_failed", "saipen audit enqueue refused")
        project = next(p for p in projects if p.id == project_id)
        if self._enqueue is not None:
            rel = self._enqueue(project, self._captured.get(capture_id, ""))
        else:
            rel = "audit/1.md"
        target = Path(project.source_path) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._captured.get(capture_id, ""), encoding="utf-8")
        return {
            "record": {"capture_id": capture_id},
            "assigned_path": str(target),
            "command": "",
            "duplicate": False,
        }

    # -- unused surface of the real store -------------------------------
    def list_records(self, **_kwargs):
        return []

    def get(self, _capture_id):
        raise LookupError


def _managed_root(tmp_path: Path) -> Path:
    """A project root that is_managed() recognises as SAIPEN-owned."""
    root = tmp_path / "managed_src"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".saipen").mkdir(exist_ok=True)
    return root


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr("audapack.saipen_transport.is_managed", lambda path: bool(path) and Path(path).joinpath(".saipen").exists())
    import audapack.ui_qt.dialogs.inaudit_widget as widget_mod

    monkeypatch.setattr(widget_mod, "is_managed", lambda path: bool(path) and Path(path).joinpath(".saipen").exists())
    unmanaged = tmp_path / "plain_src"
    unmanaged.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="plain", display_name="_LIMISAW", source_path=str(unmanaged),
                    priority_group="MAIN0", slot=1),
            Project(id="managed", display_name="_AUDAPACK", source_path=str(_managed_root(tmp_path)),
                    priority_group="MAIN0", slot=2),
        ],
    )
    win = MainWindow(ProjectService(cfg, base_dir=tmp_path))
    store = _FakeStore(tmp_path / "capture_store")
    win.inaudit_widget._capture_store = store
    yield win
    win.close()


def _bind(win: MainWindow, project_id: str):
    project = next(p for p in win._service.config.projects if p.id == project_id)
    return win.inaudit_widget.set_project(project)


def _plain(win: MainWindow) -> Project:
    return next(p for p in win._service.config.projects if p.id == "plain")


def _managed(win: MainWindow) -> Project:
    return next(p for p in win._service.config.projects if p.id == "managed")


def _wait_runner(win: MainWindow, qapp):
    runner = win.inaudit_widget._task_runner
    for _ in range(400):
        qapp.processEvents()
        if not runner.is_running("inaudit:assign") and not runner.is_running("inaudit:draft"):
            break
        time.sleep(0.01)
    # The finished signal is queued; keep draining until the completion
    # callbacks (and their UI state changes) have actually run.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
        if not runner.is_running("inaudit:assign"):
            # one extra pass so a queued callback on the same loop runs
            qapp.processEvents()
            if not runner.is_running("inaudit:assign"):
                break


# ---------------------------------------------------------------- RED 1


def test_managed_plus_enters_a_draft_instead_of_failing(window):
    """The screenshot defect: managed [+] reported "Create failed"."""
    _bind(window, "managed")
    window.inaudit_widget.create_and_focus_layer()
    text = window.inaudit_widget.status.text()
    assert not text.startswith("Create failed"), text
    assert window.inaudit_widget.managed_draft_project_id == "managed"
    # no audit/N.md was invented locally
    audit_dir = Path(_managed(window).source_path) / "audit"
    assert not audit_dir.exists() or not list(audit_dir.glob("*.md"))


def test_unmanaged_plus_creates_one_exclusive_local_layer(window):
    _bind(window, "plain")
    window.inaudit_widget.create_and_focus_layer()
    assert not window.inaudit_widget.status.text().startswith("Create failed")
    assert window.inaudit_widget.managed_draft_project_id is None
    audit_dir = Path(_plain(window).source_path) / "audit"
    assert [p.name for p in audit_dir.glob("*.md")] == ["1.md"]


# ---------------------------------------------------------------- RED 2


def test_dirty_row_switch_cannot_replace_editor_text(window):
    """Switching rows with unsaved edits must not discard the draft."""
    _bind(window, "plain")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.refresh()
    widget.editor.setPlainText("draft body that must survive")
    QApplication.processEvents()
    assert widget._dirty
    # a second layer exists to switch to
    widget._dirty = True
    widget.list.blockSignals(False)
    second = Path(_plain(window).source_path) / "audit" / "2.md"
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text("second layer", encoding="utf-8")
    widget.refresh()
    before = widget.editor.toPlainText()
    assert before == "draft body that must survive"
    # now attempt the switch
    widget._on_row_changed(1)
    assert widget.editor.toPlainText() == "draft body that must survive"
    assert widget._dirty is True


# ---------------------------------------------------------------- RED 3


def test_dirty_plus_creates_no_filesystem_layer(window):
    """A dirty unmanaged [+] must refuse before touching the filesystem."""
    _bind(window, "plain")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.refresh()
    widget.editor.setPlainText("unsaved edit")
    QApplication.processEvents()
    before = sorted(p.name for p in (Path(_plain(window).source_path) / "audit").glob("*.md"))
    widget.create_and_focus_layer()
    after = sorted(p.name for p in (Path(_plain(window).source_path) / "audit").glob("*.md"))
    assert after == before, "dirty [+] must not create a layer"
    assert "Unsaved edits" in widget.status.text()


# ---------------------------------------------------------------- RED 4


def test_dirty_rename_cannot_move_the_backing_file(window, monkeypatch):
    _bind(window, "plain")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.refresh()
    widget.editor.setPlainText("unsaved edit")
    QApplication.processEvents()
    opened = []
    monkeypatch.setattr(
        "audapack.ui_qt.dialogs.inaudit_widget.QInputDialog.getInt",
        lambda *a, **k: (opened.append(True), 5, True)[1:],
    )
    widget._on_rename_layer()
    assert not opened, "Rename must refuse before any dialog while dirty"
    audit_dir = Path(_plain(window).source_path) / "audit"
    assert sorted(p.name for p in audit_dir.glob("*.md")) == ["1.md"]


# ---------------------------------------------------------------- RED 5


def test_refused_bind_cannot_act_on_the_previous_project(window):
    """[+] on B after a dirty-A refusal must not mutate A."""
    _bind(window, "plain")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.refresh()
    widget.editor.setPlainText("draft for plain")
    QApplication.processEvents()
    managed = _managed(window)
    accepted = window._show_project_inbox(managed)
    assert accepted is False, "set_project must report the refusal"
    assert widget._project.id == "plain", "widget stays bound to the draft's project"
    # the caller must not continue into [+] against the old project
    window._on_open_audit_layers(managed, focus_last=False)
    audit_dir = Path(_plain(window).source_path) / "audit"
    assert sorted(p.name for p in audit_dir.glob("*.md")) == ["1.md"], "no mutation on A"


def test_set_project_returns_binding_result(window):
    _bind(window, "plain")
    widget = window.inaudit_widget
    assert widget.set_project(_managed(window)) is True
    widget.editor.setPlainText("dirty managed text")
    QApplication.processEvents()
    widget._dirty = True
    assert widget.set_project(_plain(window)) is False


# ---------------------------------------------------------------- RED 6


def test_managed_layer_list_does_not_advertise_reorder(window):
    """Managed projects must not configure drag reorder the backend refuses."""
    _bind(window, "managed")
    from PySide6.QtWidgets import QAbstractItemView

    lst = window.inaudit_widget.list
    assert lst.dragDropMode() != QAbstractItemView.DragDropMode.InternalMove
    assert not lst.dragEnabled()
    _bind(window, "plain")
    assert lst.dragDropMode() == QAbstractItemView.DragDropMode.InternalMove


def test_plus_tooltip_is_context_aware(window):
    _bind(window, "managed")
    managed_tip = window.inaudit_widget.btn_plus.toolTip()
    _bind(window, "plain")
    plain_tip = window.inaudit_widget.btn_plus.toolTip()
    assert managed_tip != plain_tip
    assert "draft" in managed_tip.lower() or "SAIPEN" in managed_tip


# ------------------------------------------------------- managed delivery


def test_managed_save_uses_canonical_capture_then_enqueue(window):
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("manual audit smoke")
    QApplication.processEvents()
    widget._on_save()
    _wait_runner(window, QApplication.instance())
    store = widget._capture_store
    assert len(store.captures) == 1
    assert len(store.assigns) == 1
    assert store.captures[0]["capture_kind"] == "audit"
    assert store.assigns[0]["project_id"] == "managed"
    target = Path(store.assigns[0] and widget._editor_path)
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "manual audit smoke"
    assert widget.managed_draft_project_id is None


def test_managed_save_empty_draft_does_not_call_saipen(window):
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("   \n  ")
    QApplication.processEvents()
    widget._on_save()
    _wait_runner(window, QApplication.instance())
    assert widget._capture_store.captures == []
    assert widget._capture_store.assigns == []
    assert "empty" in widget.status.text().lower()


def test_managed_retry_reuses_the_same_operation_identity(window):
    """Save twice after a failed delivery must not duplicate the layer."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("retry body")
    QApplication.processEvents()
    first_id = widget.managed_draft_capture_id
    widget._capture_store.fail_enqueue = True
    widget._on_save()
    _wait_runner(window, QApplication.instance())
    assert "failed" in widget.status.text().lower()
    assert widget.managed_draft_capture_id == first_id, "draft survives a failed delivery"
    widget._capture_store.fail_enqueue = False
    widget._on_save()
    _wait_runner(window, QApplication.instance())
    ids = [c["capture_id"] for c in widget._capture_store.captures]
    assert ids == [first_id]
    assert len(widget._capture_store.assigns) == 2  # one failed + one retry
    # delivered layer exists on disk with the exact body (T-175 durability)
    assert (Path(_managed(window).source_path) / "audit" / "1.md").read_text(encoding="utf-8") == "retry body"

def test_two_rapid_saves_start_one_delivery(window):
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("double click body")
    QApplication.processEvents()

    class _SlowStore(widget._capture_store.__class__):
        def assign(self, capture_id, project_id, projects, action="", after_assign=None):
            if self.assigns:
                raise AssertionError("second delivery started")
            return super().assign(capture_id, project_id, projects, action=action,
                                  after_assign=after_assign)

    widget._capture_store.__class__ = _SlowStore
    widget._on_save()
    widget._on_save()
    _wait_runner(window, QApplication.instance())
    assert len(widget._capture_store.assigns) == 1


def test_managed_draft_layer_is_tracked_as_user_created(window):
    """[edit] must later find the SAIPEN-assigned managed layer."""
    from audapack.inaudit import last_user_layer

    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("tracked managed draft")
    QApplication.processEvents()
    widget._on_save()
    _wait_runner(window, QApplication.instance())
    number = last_user_layer(_managed(window))
    assert number is not None, "managed [+] layer must be recorded user-created"
    assert widget.focus_last_user_layer() is True


# ===========================================================================
# T-175: managed-draft in-flight isolation. Each test below reproduced a
# defect on the T-174 tree before the fix.
# ===========================================================================


def _start_managed_delivery(window, qapp, body="in-flight draft", *, fail=False):
    """Enter managed draft and start Save with a BLOCKED assign worker."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText(body)
    QApplication.processEvents()
    store = widget._capture_store
    gate = {"release": False}

    def _blocked_assign(capture_id, project_id, projects, action="", after_assign=None):
        while not gate["release"]:
            time.sleep(0.01)
        if fail:
            from audapack.inaudit_capture import InauditCaptureError

            raise InauditCaptureError("enqueue_failed", "saipen refused")
        store.assigns.append({"capture_id": capture_id, "project_id": project_id, "action": action})
        target = Path(_managed(window).source_path) / "audit" / "1.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(store._captured.get(capture_id, ""), encoding="utf-8")
        return {
            "record": {"capture_id": capture_id},
            "assigned_path": str(target),
            "command": "",
            "duplicate": False,
        }

    store.assign = _blocked_assign
    widget._on_save()
    for _ in range(200):
        qapp.processEvents()
        if widget.managed_draft_delivery_pending:
            break
        time.sleep(0.01)
    qapp.processEvents()
    return widget, store, gate


def test_editor_is_readonly_during_delivery_and_draft_survives_failure(window, qapp):
    """T-175 defect 1: typing during delivery must be impossible, and a failed
    delivery must restore the exact draft."""
    widget, store, gate = _start_managed_delivery(window, qapp, fail=True)
    try:
        assert widget.managed_draft_delivery_pending
        assert widget.editor.isReadOnly(), "editor must be read-only during delivery"
        # Qt readOnly blocks real user keyboard input (the operator path);
        # programmatic setPlainText bypasses it by design, so the assertion
        # is the read-only flag itself, held for the whole delivery.
        from PySide6.QtTest import QTest

        QTest.keyClicks(widget.editor, "typed-by-user")
        QApplication.processEvents()
        assert widget.editor.isReadOnly(), "read-only must hold through the delivery"
    finally:
        gate["release"] = True
        _wait_runner(window, qapp)
    assert "failed" in widget.status.text().lower()
    assert widget.editor.toPlainText() == "in-flight draft", (
        "the delivered draft text is restored on failure, not the post-Save noise"
    )
    assert not widget.editor.isReadOnly(), "failure must restore a writable editor"
    assert widget.managed_draft_capture_id is not None, "draft retained for retry"


def test_reload_refuses_while_delivery_is_pending(window, qapp):
    """T-175 defect 2: Reload must not destroy in-flight draft state."""
    widget, store, gate = _start_managed_delivery(window, qapp)
    try:
        capture_id = widget.managed_draft_capture_id
        project_id = widget.managed_draft_project_id
        widget._on_reload()
        assert widget.managed_draft_capture_id == capture_id
        assert widget.managed_draft_project_id == project_id
        assert widget._project.id == "managed", "no rebinding during delivery"
        assert "in progress" in widget.status.text().lower()
    finally:
        gate["release"] = True
        _wait_runner(window, qapp)


def test_layer_bound_actions_disabled_during_unassigned_draft(window, qapp):
    """T-175 defect 3: an unassigned draft must not let old layers masquerade."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    seed = Path(_managed(window).source_path) / "audit" / "1.md"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("old layer body", encoding="utf-8")
    widget.refresh()
    widget.list.setCurrentRow(0)
    QApplication.processEvents()
    widget.create_and_focus_layer()  # starts a new draft over the old selection
    widget.editor.setPlainText("new draft body")
    QApplication.processEvents()
    for button in (widget.btn_open, widget.btn_ia, widget.btn_gg, widget.btn_cc,
                   widget.btn_rename, widget.btn_delete):
        assert not button.isEnabled(), f"{button.text()} must be disabled during an unassigned draft"
    assert widget.btn_save.isEnabled()
    # the old layer must be untouched by any layer-bound action
    assert seed.read_text(encoding="utf-8") == "old layer body"


def test_actions_restore_after_successful_delivery(window, qapp):
    """T-175 defect 4: success binds the assigned layer and restores actions."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("delivered body")
    QApplication.processEvents()
    widget._on_save()
    _wait_runner(window, qapp)
    assert widget._editor_path is not None and widget._editor_path.is_file()
    for button in (widget.btn_open, widget.btn_ia, widget.btn_gg, widget.btn_cc, widget.btn_delete):
        assert button.isEnabled(), f"{button.text()} must return after delivery"
    assert widget._editor_path.read_text(encoding="utf-8") == "delivered body"


def test_inbox_assignment_blocks_managed_save(window, qapp):
    """T-175 defect 5 (direction 1): no managed Save may supersede an active
    Inbox inaudit:assign generation."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    store = widget._capture_store
    gate = {"release": False}
    inbox_started = []

    def _blocked_assign(capture_id, project_id, projects, action="", after_assign=None):
        inbox_started.append(capture_id)
        while not gate["release"]:
            time.sleep(0.01)
        return {"record": {"capture_id": capture_id}, "assigned_path": "", "command": "", "duplicate": False}

    store.assign = _blocked_assign
    widget.inbox_page.setEnabled(True)
    widget._submit_assignment("00000000-0000-0000-0000-000000000001", "managed",
                              window._service.config.projects, action="")
    # Wait for the worker's own observable, not for is_running(): that flag is
    # set when the task is DISPATCHED, so the thread had not necessarily reached
    # _blocked_assign yet and the assertion below raced it under load.
    for _ in range(200):
        qapp.processEvents()
        if inbox_started:
            break
        time.sleep(0.01)
    assert widget._task_runner.is_running("inaudit:assign")
    try:
        widget.create_and_focus_layer()
        widget.editor.setPlainText("managed save attempt")
        QApplication.processEvents()
        widget._on_save()
        qapp.processEvents()
        assert not widget.managed_draft_delivery_pending, "managed Save must not start"
        assert len(store.captures) == 0, "no capture while an assignment is active"
        assert inbox_started == ["00000000-0000-0000-0000-000000000001"]
        assert "already in progress" in widget.status.text().lower()
    finally:
        gate["release"] = True
        _wait_runner(window, qapp)


def test_managed_save_blocks_inbox_assignment(window, qapp):
    """T-175 defect 5 (direction 2): an active managed Save must make Inbox
    Assign refuse instead of superseding the generation."""
    widget, store, gate = _start_managed_delivery(window, qapp)
    try:
        before = len(store.assigns)
        widget.inbox_page.setEnabled(True)
        widget._submit_assignment("00000000-0000-0000-0000-000000000002", "managed",
                                  window._service.config.projects, action="")
        qapp.processEvents()
        assert len(store.assigns) == before, "no second concurrent assignment"
    finally:
        gate["release"] = True
        _wait_runner(window, qapp)


def test_row_plus_cannot_bypass_busy_assignment(window, qapp):
    """T-175 defect 5: the public row [+] entrypoint must respect busy state."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    store = widget._capture_store
    gate = {"release": False}

    def _blocked_assign(capture_id, project_id, projects, action="", after_assign=None):
        while not gate["release"]:
            time.sleep(0.01)
        return {"record": {"capture_id": capture_id}, "assigned_path": "", "command": "", "duplicate": False}

    store.assign = _blocked_assign
    widget._on_save  # noqa: B018
    widget.create_and_focus_layer()
    widget.editor.setPlainText("busy row plus body")
    QApplication.processEvents()
    widget._on_save()
    for _ in range(200):
        qapp.processEvents()
        if widget.managed_draft_delivery_pending:
            break
        time.sleep(0.01)
    try:
        before_draft_id = widget.managed_draft_capture_id
        before_audit = sorted(p.name for p in (Path(_managed(window).source_path) / "audit").glob("*.md"))
        window._on_open_audit_layers(_managed(window), focus_last=False)
        qapp.processEvents()
        assert widget.managed_draft_capture_id == before_draft_id, "no second draft while busy"
        after_audit = sorted(p.name for p in (Path(_managed(window).source_path) / "audit").glob("*.md"))
        assert after_audit == before_audit, "no local layer while busy"
        assert widget.managed_draft_delivery_pending, "the original delivery still owns the lane"
    finally:
        gate["release"] = True
        _wait_runner(window, qapp)


def test_project_removed_before_callback_completes_safely(window, qapp):
    """T-175 defect 6: registration vanishing during delivery must neither
    crash the callback nor retry a durable enqueue."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    store = widget._capture_store
    gate = {"release": False}
    delivered_to = []

    def _assign(capture_id, project_id, projects, action="", after_assign=None):
        target = Path(_managed(window).source_path) / "audit" / "1.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(store._captured.get(capture_id, ""), encoding="utf-8")
        delivered_to.append(target)
        gate["wait"] = True
        while gate.get("hold"):
            time.sleep(0.01)
        gate["wait"] = False
        return {"record": {"capture_id": capture_id}, "assigned_path": str(target),
                "command": "", "duplicate": False}

    store.assign = _assign
    gate["hold"] = True  # block the worker AFTER it writes the durable file
    widget.create_and_focus_layer()
    widget.editor.setPlainText("durable body")
    QApplication.processEvents()
    widget._on_save()
    for _ in range(200):
        qapp.processEvents()
        if gate.get("wait"):
            break
        time.sleep(0.01)
    assert gate.get("wait"), "worker must have reached the hold point"
    # the project registration disappears while the worker is held
    cfg = window._service.config
    cfg.projects = [p for p in cfg.projects if p.id != "managed"]
    gate["hold"] = False
    for _ in range(400):
        qapp.processEvents()
        if not widget._task_runner.is_running("inaudit:assign"):
            break
        time.sleep(0.01)
    time.sleep(0.05)
    for _ in range(50):
        qapp.processEvents()
        if "no longer registered" in widget.status.text().lower():
            break
        time.sleep(0.01)
    qapp.processEvents()
    assert delivered_to and delivered_to[0].is_file(), "durable assignment preserved"
    assert "no longer registered" in widget.status.text().lower()
    assert not widget.managed_draft_delivery_pending, "busy state cleared"
    assert widget._capture_store.captures, "assignment was NOT retried (single durable run)"


def test_failure_restores_controls_and_draft(window, qapp):
    """T-175 defect 9: failure restores exact controls + stable UUID + draft."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    store = widget._capture_store
    gate = {"release": False}

    def _failing_assign(capture_id, project_id, projects, action="", after_assign=None):
        while not gate["release"]:
            time.sleep(0.01)
        from audapack.inaudit_capture import InauditCaptureError

        raise InauditCaptureError("enqueue_failed", "saipen refused")

    store.assign = _failing_assign
    widget.create_and_focus_layer()
    widget.editor.setPlainText("surviving draft")
    QApplication.processEvents()
    stable_id = widget.managed_draft_capture_id
    widget._on_save()
    gate["release"] = True
    _wait_runner(window, qapp)
    assert widget.editor.toPlainText() == "surviving draft"
    assert not widget.editor.isReadOnly()
    assert widget.btn_save.isEnabled() and widget.btn_reload.isEnabled()
    assert widget.managed_draft_capture_id == stable_id
    for button in (widget.btn_open, widget.btn_ia, widget.btn_gg, widget.btn_cc, widget.btn_delete):
        assert not button.isEnabled(), "still an unassigned draft after failure"


def test_success_clears_pending_state(window, qapp):
    """T-175 defect 10: success lands in EXISTING_LAYER_CLEAN."""
    _bind(window, "managed")
    widget = window.inaudit_widget
    widget.create_and_focus_layer()
    widget.editor.setPlainText("final body")
    QApplication.processEvents()
    widget._on_save()
    _wait_runner(window, qapp)
    assert widget.managed_draft_delivery_pending is False
    assert widget.managed_draft_capture_id is None
    assert widget.managed_draft_project_id is None
    assert not widget.editor.isReadOnly()
    assert not widget._dirty
    assert widget._editor_path is not None and widget._editor_path.is_file()
