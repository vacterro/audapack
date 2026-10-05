from __future__ import annotations

import logging
import os
import uuid
import webbrowser
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from audapack.config import load_config
from audapack.inaudit import (
    TRACKING_REPAIR_UNPERSISTED,
    delete_inaudit_layer,
    ensure_next_layer,
    get_active_inaudit_path,
    get_inaudit_selected,
    inaudit_dir,
    last_user_layer,
    list_inaudit_layers,
    record_user_layer,
    recover_inaudit_state,
    rename_inaudit_layer,
    reorder_inaudit_layers,
    set_inaudit_selected,
    validate_inaudit_path,
)
from audapack.inaudit_capture import InauditCaptureError, InauditCaptureStore
from audapack.inaudit_commit import (
    CHANGED_EXTERNALLY,
    COMMITTED,
    MISSING_OR_CONSUMED,
    commit_existing_layer,
    recover_inaudit_dir_commits,
)
from audapack.models import Project
from audapack.saipen_transport import is_managed
from audapack.ui_qt.task_runner import TaskRunner
from audapack.ui_qt.theme.golden_default import GoldenDefault

logger = logging.getLogger(__name__)


class _InauditLayerList(QListWidget):
    """QListWidget that forwards the Delete key to the owning widget and
    lets layers be drag-reordered into audit priority order."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, event):
        # The canonical renumbering lives on disk (reorder_inaudit_layers), so
        # Qt's automatic InternalMove row shuffling is deliberately suppressed
        # here: the widget re-renders from disk after the reorder succeeds.
        owner = self._owner
        if owner is None or owner._project is None:
            event.ignore()
            return
        source_row = self.currentRow()
        if source_row < 0:
            event.ignore()
            return
        target_index = self.indexAt(event.position().toPoint())
        target_row = target_index.row() if target_index.isValid() else self.count() - 1
        position = self.dropIndicatorPosition()
        if position == QAbstractItemView.DropIndicatorPosition.BelowItem:
            target_row += 1
        elif position in (QAbstractItemView.DropIndicatorPosition.AboveItem,):
            target_row = target_row
        if target_row == source_row:
            event.ignore()
            return
        event.accept()
        owner._on_reorder(source_row, target_row)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete and self._owner is not None:
            self._owner._on_delete()
            event.accept()
            return
        super().keyPressEvent(event)


class InauditWidget(QWidget):
    def __init__(self, parent=None, on_changed=None, config_provider=None, capture_store=None):
        super().__init__(parent)
        self._on_changed_cb = on_changed
        self._config_provider = config_provider or load_config
        self._project: Project | None = None
        self._inbox_records: list[dict] = []
        self._capture_store = capture_store or InauditCaptureStore()
        self._task_runner = TaskRunner(max_threads=0, parent=self)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_fs_changed)
        self._watcher.fileChanged.connect(self._on_fs_changed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._on_debounced_fs)
        self._dirty = False
        self._editor_path: Path | None = None
        self._editor_bytes: bytes | None = None
        #: Why the last editor load could not read a real layer. A failure here
        #: is NOT the same fact as "this project has no layer yet": Save must
        #: refuse in the first case instead of inventing a new target.
        self._layer_read_error: str = ""
        # T-174: first-class managed-draft state. A managed draft is NOT a
        # fake audit/N.md: SAIPEN owns layer allocation, so the draft exists
        # only as editor text plus ONE stable capture UUID (the producer
        # operation id T-158 idempotency relies on) until Save delivers it.
        self.managed_draft_project_id: str | None = None
        self.managed_draft_capture_id: str | None = None
        self.managed_draft_delivery_pending = False

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        self.mode_tabs = QTabWidget(self)
        self.layers_page = QWidget(self.mode_tabs)
        layers = QVBoxLayout(self.layers_page)
        layers.setContentsMargins(2, 2, 2, 2)
        layers.setSpacing(4)
        self.inbox_page = QWidget(self.mode_tabs)
        self.mode_tabs.addTab(self.layers_page, "Layers")
        self.mode_tabs.addTab(self.inbox_page, "Inbox")
        root.addWidget(self.mode_tabs, 1)

        self.header = QLabel("INAUDIT — no project selected", self)
        hf = QFont("Verdana", 9, QFont.Weight.Bold)
        hf.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.header.setFont(hf)
        layers.addWidget(self.header)

        self.list = _InauditLayerList(owner=self, parent=self)
        self.list.setMinimumHeight(90)
        self.list.setMaximumHeight(140)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.itemDoubleClicked.connect(lambda _: self._on_open())
        layers.addWidget(self.list)

        btn_row = QWidget(self)
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        self.btn_open = QPushButton("Open", btn_row)
        self.btn_open.clicked.connect(self._on_open)
        self.btn_ia = QPushButton("IA Copy", btn_row)
        self.btn_ia.setToolTip("Copy selected INAUDIT path")
        self.btn_ia.clicked.connect(self._on_ia_copy)
        self.btn_gg = QPushButton("GG", btn_row)
        self.btn_gg.setToolTip("Legacy GG shortcut: copy saipen cc to process the Audit Inbox")
        self.btn_gg.clicked.connect(self._on_gg)
        self.btn_cc = QPushButton("CC", btn_row)
        self.btn_cc.setToolTip("Copy saipen cc to process the project's Audit Inbox")
        self.btn_cc.clicked.connect(self._on_cc)
        self.btn_plus = QPushButton("+", btn_row)
        self.btn_plus.setToolTip("Create next numbered layer")
        self.btn_plus.clicked.connect(self._on_plus)
        self.btn_rename = QPushButton("Rename", btn_row)
        self.btn_rename.setToolTip("Move this layer to a different free number (never overwrites)")
        self.btn_rename.clicked.connect(self._on_rename_layer)
        self.btn_delete = QPushButton("Delete", btn_row)
        self.btn_delete.setToolTip("Delete the selected layer (also: Del key on the list). No renumbering.")
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_refresh = QPushButton("Refresh", btn_row)
        self.btn_refresh.clicked.connect(self.refresh)
        for b in (self.btn_open, self.btn_ia, self.btn_gg, self.btn_cc, self.btn_plus, self.btn_rename, self.btn_delete, self.btn_refresh):
            bl.addWidget(b)
        bl.addStretch(1)
        layers.addWidget(btn_row)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlaceholderText("Select a layer to view/edit. Plain UTF-8 Markdown.")
        ef = QFont("Consolas", 9)
        ef.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.editor.setFont(ef)
        self.editor.textChanged.connect(self._on_editor_changed)
        layers.addWidget(self.editor, 1)

        edit_row = QWidget(self)
        el = QHBoxLayout(edit_row)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(4)
        self.lbl_dirty = QLabel("", edit_row)
        self.btn_save = QPushButton("Save", edit_row)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_reload = QPushButton("Reload", edit_row)
        self.btn_reload.clicked.connect(self._on_reload)
        el.addWidget(self.lbl_dirty, 1)
        el.addWidget(self.btn_reload)
        el.addWidget(self.btn_save)
        layers.addWidget(edit_row)

        self.status = QLabel("Select a layer, then IA / GG / CC.", self)
        self.status.setWordWrap(True)
        layers.addWidget(self.status)
        self._build_inbox_ui()
        self.setStyleSheet(GoldenDefault.qss())
        self._update_actions()
        self._save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self._save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._save_shortcut.activated.connect(self._on_save)
        self.refresh_inbox()

    def _build_inbox_ui(self):
        layout = QVBoxLayout(self.inbox_page)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        top = QWidget(self.inbox_page)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(2)
        self.inbox_header = QLabel("INAUDIT INBOX 0 · ?0", top)
        self.btn_clipboard_capture = QPushButton("IA+ Clipboard", top)
        self.btn_clipboard_capture.setToolTip("Capture clipboard text into the durable global INAUDIT Inbox")
        self.btn_clipboard_capture.clicked.connect(self._on_clipboard_capture)
        self.btn_inbox_refresh = QPushButton("Refresh Inbox", top)
        self.btn_inbox_refresh.clicked.connect(self.refresh_inbox)
        top_layout.addWidget(self.inbox_header, 1)
        top_layout.addWidget(self.btn_clipboard_capture)
        top_layout.addWidget(self.btn_inbox_refresh)
        layout.addWidget(top)

        self.inbox_list = QListWidget(self.inbox_page)
        self.inbox_list.setMinimumHeight(110)
        self.inbox_list.setMaximumHeight(180)
        self.inbox_list.currentRowChanged.connect(self._on_inbox_row_changed)
        layout.addWidget(self.inbox_list)

        target_row = QWidget(self.inbox_page)
        target_layout = QHBoxLayout(target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(4)
        target_layout.addWidget(QLabel("Assign to project:", target_row))
        self.inbox_project = QComboBox(target_row)
        self.inbox_project.currentIndexChanged.connect(self._on_inbox_project_changed)
        target_layout.addWidget(self.inbox_project, 1)
        layout.addWidget(target_row)

        self.inbox_detail = QPlainTextEdit(self.inbox_page)
        self.inbox_detail.setReadOnly(True)
        self.inbox_detail.setMinimumHeight(120)
        layout.addWidget(self.inbox_detail, 1)

        actions = QWidget(self.inbox_page)
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(2)
        self.btn_assign = QPushButton("Assign", actions)
        self.btn_assign.clicked.connect(lambda: self._on_assign_capture(""))
        self.btn_pin = QPushButton("Pin project", actions)
        self.btn_pin.setToolTip(
            "Record the project this capture belongs to, without creating its layer yet.\n"
            "Clears the pin when no project is selected."
        )
        self.btn_pin.clicked.connect(self._on_pin_project)
        self.btn_rename_capture = QPushButton("Rename", actions)
        self.btn_rename_capture.setToolTip("Give this capture a title you will recognise")
        self.btn_rename_capture.clicked.connect(self._on_rename_capture)
        self.btn_assign_gg = QPushButton("Assign + GG", actions)
        self.btn_assign_gg.clicked.connect(lambda: self._on_assign_capture("GG"))
        self.btn_assign_cc = QPushButton("Assign + CC", actions)
        self.btn_assign_cc.clicked.connect(lambda: self._on_assign_capture("CC"))
        self.btn_capture_copy = QPushButton("Copy text", actions)
        self.btn_capture_copy.clicked.connect(self._on_copy_capture)
        self.btn_open_source = QPushButton("Open Source", actions)
        self.btn_open_source.clicked.connect(self._on_open_capture_source)
        self.btn_archive_capture = QPushButton("Archive", actions)
        self.btn_archive_capture.clicked.connect(self._on_archive_capture)
        self.btn_delete_capture = QPushButton("Delete", actions)
        self.btn_delete_capture.clicked.connect(self._on_delete_capture)
        for button in (
            self.btn_assign,
            self.btn_assign_gg,
            self.btn_assign_cc,
            self.btn_pin,
            self.btn_rename_capture,
            self.btn_capture_copy,
            self.btn_open_source,
            self.btn_archive_capture,
            self.btn_delete_capture,
        ):
            action_layout.addWidget(button)
        action_layout.addStretch(1)
        layout.addWidget(actions)

        self.inbox_status = QLabel("Capture first. Classification is only a suggestion.", self.inbox_page)
        self.inbox_status.setWordWrap(True)
        layout.addWidget(self.inbox_status)

        self._inbox_watcher = QFileSystemWatcher(self)
        self._inbox_watcher.directoryChanged.connect(self._on_inbox_fs_changed)
        self._inbox_watcher.fileChanged.connect(self._on_inbox_fs_changed)
        self._inbox_debounce = QTimer(self)
        self._inbox_debounce.setSingleShot(True)
        self._inbox_debounce.setInterval(250)
        self._inbox_debounce.timeout.connect(self.refresh_inbox)
        self._rewatch_inbox()
        self._update_inbox_actions()

    def _config(self):
        try:
            return self._config_provider()
        except TypeError:
            return self._config_provider

    def _projects(self) -> list[Project]:
        cfg = self._config()
        return list(getattr(cfg, "projects", ()) or ())

    def _selected_capture(self) -> dict | None:
        row = self.inbox_list.currentRow()
        if 0 <= row < len(self._inbox_records):
            return self._inbox_records[row]
        return None

    @staticmethod
    def _copy_text(text: str) -> bool:
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        return clipboard.text() == text

    def _rewatch_inbox(self):
        try:
            for path in list(self._inbox_watcher.directories()):
                self._inbox_watcher.removePath(path)
            for path in list(self._inbox_watcher.files()):
                self._inbox_watcher.removePath(path)
            self._inbox_watcher.addPath(str(self._capture_store.root))
            if self._capture_store.generation_path.is_file():
                self._inbox_watcher.addPath(str(self._capture_store.generation_path))
        except OSError:
            pass

    def _on_inbox_fs_changed(self, _path: str = ""):
        self._inbox_debounce.start()

    def refresh_inbox(self):
        selected = self._selected_capture()
        selected_id = str(selected.get("capture_id") or "") if selected else ""
        try:
            self._inbox_records = self._capture_store.list_records()
        except OSError as exc:
            self._inbox_records = []
            self.inbox_status.setText(f"Inbox refresh failed: {exc}")
        pending = sum(1 for item in self._inbox_records if not item.get("assigned_project_id"))
        unassigned = sum(1 for item in self._inbox_records if not item.get("suggested_project_id") and not item.get("assigned_project_id"))
        self.inbox_header.setText(f"INAUDIT INBOX {pending} · ?{unassigned}")
        self.inbox_list.blockSignals(True)
        self.inbox_list.clear()
        selected_row = -1
        for row, record in enumerate(self._inbox_records):
            confidence = float(record.get("classification_confidence") or 0.0)
            confidence_text = f"{round(confidence * 100):02d}%" if confidence else " ? "
            # A pin is the operator's answer; mark it so the row does not read as
            # a classifier guess (T-145).
            pinned = str(record.get("target_project_name") or "")
            project = f"*{pinned}" if pinned else (record.get("suggested_project_name") or "UNASSIGNED")
            item = QListWidgetItem(f"{record.get('status', 'NEW'):<9} {confidence_text:>3}  {project:<16} {record.get('title', '')}")
            item.setData(Qt.ItemDataRole.UserRole, record.get("capture_id"))
            self.inbox_list.addItem(item)
            if record.get("capture_id") == selected_id:
                selected_row = row
        if selected_row < 0 and self._inbox_records:
            selected_row = 0
        if selected_row >= 0:
            self.inbox_list.setCurrentRow(selected_row)
        self.inbox_list.blockSignals(False)
        self._on_inbox_row_changed(selected_row)
        self._rewatch_inbox()

    def _on_inbox_row_changed(self, row: int):
        record = self._selected_capture() if row >= 0 else None
        self.inbox_project.blockSignals(True)
        self.inbox_project.clear()
        self.inbox_project.addItem("Select project…", "")
        # A pinned project is the operator's own answer and outranks the
        # classifier's guess (T-145).
        wanted_id = str(
            (record.get("target_project_id") or record.get("suggested_project_id") or "") if record else ""
        )
        selected_index = 0
        for index, project in enumerate(self._projects(), start=1):
            self.inbox_project.addItem(project.display_name, project.id)
            if project.id == wanted_id:
                selected_index = index
        self.inbox_project.setCurrentIndex(selected_index)
        self.inbox_project.blockSignals(False)
        if record is None:
            self.inbox_detail.clear()
            self._update_inbox_actions()
            return
        self._render_inbox_detail(record)

    def _render_inbox_detail(self, record: dict):
        try:
            detail = self._capture_store.get(str(record["capture_id"]))
            body = detail["text"]
        except (InauditCaptureError, OSError) as exc:
            body = f"Cannot read capture: {exc}"
        confidence = float(record.get("classification_confidence") or 0.0)
        evidence = "\n".join(f"- {value}" for value in record.get("classification_evidence") or []) or "- none"
        assigned = record.get("assigned_path") or self._proposed_path()
        preview = body[:12000]
        if len(body) > len(preview):
            preview += "\n\n[preview truncated]"
        self.inbox_detail.setPlainText(
            f"{record.get('title', '')}\n"
            f"Status: {record.get('status', '')}\n"
            f"Captured: {record.get('created_at', '')}\n"
            f"Source: {record.get('source', '')} · {record.get('browser_name', '')}\n"
            f"Suggested: {record.get('suggested_project_name') or '?'} {round(confidence * 100)}%\n"
            f"Pinned: {record.get('target_project_name') or '-'}\n"
            f"Destination: {assigned or '?'}\n"
            f"Evidence:\n{evidence}\n\n--- TEXT ---\n{preview}"
        )
        self._update_inbox_actions()

    def _on_inbox_project_changed(self, _index: int):
        record = self._selected_capture()
        if record is not None:
            self._render_inbox_detail(record)
        else:
            self._update_inbox_actions()

    def _proposed_path(self) -> str:
        project_id = self.inbox_project.currentData()
        project = next((item for item in self._projects() if item.id == project_id), None)
        if project is None or not project.source_path:
            return ""
        audit_dir = Path(project.source_path) / "audit"
        if is_managed(project.source_path):
            return f"{audit_dir} (number assigned on delivery)"
        try:
            numbers = [
                int(path.stem)
                for path in audit_dir.iterdir()
                if path.is_file()
                and path.suffix == ".md"
                and path.stem.isdigit()
                and int(path.stem) > 0
                and str(int(path.stem)) == path.stem
            ]
        except OSError:
            numbers = []
        return str(audit_dir / f"{max(numbers, default=0) + 1}.md")

    def _on_clipboard_capture(self):
        text = QApplication.clipboard().text()
        if not text.strip():
            self.inbox_status.setText("IA+ failed: clipboard has no text")
            return
        hints = [self._project.id] if self._project is not None else []
        try:
            result = self._capture_store.capture(
                {
                    "capture_id": str(uuid.uuid4()),
                    "text": text,
                    "capture_kind": "clipboard",
                    "source": "clipboard",
                    "source_title": "Desktop Clipboard",
                    "project_hints": hints,
                },
                self._projects(),
            )
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"IA+ failed: {exc}")
            return
        record = result["record"]
        self.inbox_status.setText(f"Captured {record['capture_id'][:8]} · durable Inbox write verified")
        self.refresh_inbox()
        try:
            if self._on_changed_cb and self._project is not None:
                self._on_changed_cb(self._project)
        except Exception:
            pass

    def capture_clipboard(self):
        """Public desktop IA+ action: show Inbox, then use its canonical capture path."""
        self.mode_tabs.setCurrentWidget(self.inbox_page)
        self._on_clipboard_capture()

    def _on_assign_capture(self, action: str):
        record = self._selected_capture()
        project_id = str(self.inbox_project.currentData() or "")
        if record is None or not project_id:
            self.inbox_status.setText("Select one capture and one registered project")
            return
        projects = self._projects()
        project = next((p for p in projects if p.id == project_id), None)
        if project and (is_managed(project.source_path) or record.get("delivery_project_root")):
            self._submit_assignment(str(record["capture_id"]), project_id, projects, action)
            return
        try:
            result = self._capture_store.assign(
                str(record["capture_id"]), project_id, projects, action=action
            )
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"Assign failed: {exc}")
            return
        self._assignment_finished(result, project_id, action)

    def _submit_assignment(self, capture_id, project_id, projects, action):
        if self._task_runner.is_running("inaudit:assign"):
            self.inbox_status.setText("Audit delivery in progress")
            return
        self.inbox_page.setEnabled(False)
        self.btn_plus.setEnabled(False)
        self.inbox_status.setText("Delivering audit to project...")

        def done(result):
            self.inbox_page.setEnabled(True)
            self.btn_plus.setEnabled(True)
            self._assignment_finished(result, project_id, action)

        def failed(error):
            self.inbox_page.setEnabled(True)
            self.btn_plus.setEnabled(True)
            self.inbox_status.setText(f"Assign failed: {error}")
            self.status.setText(f"Assign failed: {error}")

        self._task_runner.submit("inaudit:assign",
                                 lambda: self._capture_store.assign(capture_id, project_id, projects, action=action),
                                 on_success=done, on_error=failed)

    def _assignment_finished(self, result, project_id, action):
        copied = self._copy_text(str(result["command"])) if result.get("command") else True
        suffix = " · CC command copied" if action and copied else (" · CC copy failed" if action else "")
        self.inbox_status.setText(f"Assigned: {result['assigned_path']}{suffix}")
        self.refresh_inbox()
        self.refresh()
        try:
            if self._on_changed_cb:
                self._on_changed_cb(next((p for p in self._projects() if p.id == project_id), None))
        except Exception:
            pass

    def _on_pin_project(self):
        """Record the project by hand, without creating the layer yet (T-145)."""
        record = self._selected_capture()
        if record is None:
            self.inbox_status.setText("Select one capture first")
            return
        project_id = str(self.inbox_project.currentData() or "")
        try:
            updated = self._capture_store.set_target_project(
                str(record["capture_id"]), project_id, self._projects()
            )
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"Pin failed: {exc}")
            return
        name = str(updated.get("target_project_name") or "")
        self.inbox_status.setText(
            f"Pinned to {name}; Assign will use it" if name else "Pin cleared; the suggestion applies again"
        )
        self.refresh_inbox()

    def _on_rename_capture(self):
        record = self._selected_capture()
        if record is None:
            self.inbox_status.setText("Select one capture first")
            return
        title, ok = QInputDialog.getText(
            self, "Rename INAUDIT capture", "Title:", text=str(record.get("title") or "")
        )
        if not ok:
            return
        try:
            self._capture_store.rename(str(record["capture_id"]), title)
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"Rename failed: {exc}")
            return
        self.inbox_status.setText("Capture renamed")
        self.refresh_inbox()

    def _on_copy_capture(self):
        record = self._selected_capture()
        if record is None:
            return
        try:
            text = self._capture_store.get(str(record["capture_id"]))["text"]
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"Copy failed: {exc}")
            return
        if self._copy_text(text):
            self.inbox_status.setText("Exact capture text copied")
        else:
            self.inbox_status.setText("Copy failed: Windows clipboard is busy")

    def _on_open_capture_source(self):
        record = self._selected_capture()
        url = str(record.get("source_url") or "") if record else ""
        if not url:
            self.inbox_status.setText("This capture has no valid source URL")
            return
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error as exc:
            self.inbox_status.setText(f"Open Source failed: {exc}")
            return
        self.inbox_status.setText(
            "Source opened in the default browser" if opened else "Open Source failed: default browser rejected the URL"
        )

    def _on_archive_capture(self):
        record = self._selected_capture()
        if record is None:
            return
        try:
            self._capture_store.archive(str(record["capture_id"]))
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"Archive failed: {exc}")
            return
        self.inbox_status.setText(f"Archived: {record.get('title', '')}")
        self.refresh_inbox()

    def _on_delete_capture(self):
        record = self._selected_capture()
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete INAUDIT capture",
            f"Permanently delete capture {str(record.get('capture_id'))[:8]} and its Inbox body?\n"
            + ("Its assigned project layer will remain." if record.get("assigned_path") else "This cannot be undone."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._capture_store.delete(str(record["capture_id"]))
        except (InauditCaptureError, OSError) as exc:
            self.inbox_status.setText(f"Delete failed: {exc}")
            return
        self.inbox_status.setText("Capture permanently deleted")
        self.refresh_inbox()

    def _update_inbox_actions(self):
        record = self._selected_capture()
        has = record is not None
        assigned = bool(record and record.get("assigned_path"))
        archived = bool(record and record.get("status") == "ARCHIVED")
        has_project = bool(self.inbox_project.currentData())
        for button in (self.btn_assign, self.btn_assign_gg, self.btn_assign_cc):
            button.setEnabled(has and not assigned and not archived and has_project)
        self.btn_capture_copy.setEnabled(has)
        # Pinning is allowed while the capture is still unassigned, including
        # with no project selected -- that is how a pin is cleared.
        self.btn_pin.setEnabled(has and not assigned)
        self.btn_rename_capture.setEnabled(has)
        self.btn_open_source.setEnabled(bool(record and record.get("source_url")))
        self.btn_archive_capture.setEnabled(has and not archived)
        self.btn_delete_capture.setEnabled(has)

    def set_project(self, project: Project | None) -> bool:
        """Bind the widget to ``project``. False when the binding was refused
        (a dirty draft belongs to another project) -- callers MUST propagate
        this, or a refused bind lets [+]/[edit] run against the previously
        bound project (T-174 I5)."""
        if self._dirty and project != self._project:
            self.status.setText("Draft retained. Save or Reload before switching projects.")
            return False
        same = project == self._project
        if not same:
            # A real project switch drops the draft binding; re-binding the
            # SAME project (e.g. the Project Room row [++] path) must never
            # clear an in-flight managed draft (T-175 defect 5).
            self._clear_managed_draft()
        self._project = project
        if project is not None:
            # CORE-001/CORE-002/CORE-003 bounded recovery boundary: resolve
            # interrupted saves, pending reorder journals and tracker repair
            # intents once per project bind, before the layer is exposed.
            try:
                recover_inaudit_state(project)
            except Exception:
                logger.warning("inaudit recovery at bind failed", exc_info=True)
        self._rewatch()
        if not same or not self.managed_draft_project_id:
            self.refresh()
        self.refresh_inbox()
        self._update_actions()
        return True

    def _clear_managed_draft(self):
        self.managed_draft_project_id = None
        self.managed_draft_capture_id = None
        self.managed_draft_delivery_pending = False

    def _managed_active(self) -> bool:
        return self._project is not None and bool(
            getattr(self._project, "source_path", None)
        ) and is_managed(self._project.source_path)

    def _update_managed_action_state(self):
        """Advertise only operations the backend will actually perform (T-174 J).

        Managed projects: SAIPEN owns layer numbers, so drag reorder is
        disabled rather than advertised-then-refused. Unmanaged: unchanged.
        """
        managed = self._managed_active()
        self.btn_plus.setToolTip(
            "Create a manual audit draft; Save delivers it through SAIPEN"
            if managed
            else "Create next numbered layer"
        )
        if managed:
            self.list.setDragEnabled(False)
            self.list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
            self.list.setDefaultDropAction(Qt.DropAction.IgnoreAction)
        else:
            self.list.setDragEnabled(True)
            self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            self.list.setDefaultDropAction(Qt.DropAction.MoveAction)

    def _action_state(self) -> str:
        """The one deterministic derivation every layer control reads (T-175).

        Every state of the widget is named here; _update_actions maps them onto
        buttons instead of a dozen scattered setEnabled calls.
        """
        if self._project is None:
            return "NO_PROJECT"
        if self.managed_draft_delivery_pending or self._task_runner.is_running("inaudit:assign"):
            return "ASSIGNMENT_PENDING"
        if self.managed_draft_project_id:
            return "MANAGED_DRAFT_DIRTY" if self._dirty else "MANAGED_DRAFT_IDLE"
        if self._layer_read_error:
            return "LAYER_UNREADABLE"
        if self._active_path() is None:
            # The bottom textbox IS the manual audit editor, so this state is a
            # real one, not a dead end: Save here writes the FIRST canonical
            # audit. It only has to be distinguishable from "a layer is open".
            return "NO_LAYER"
        return "EXISTING_LAYER_DIRTY" if self._dirty else "EXISTING_LAYER_CLEAN"

    def _update_actions(self):
        """Apply the derived action state to every control (T-175).

        MANAGED_DRAFT_*: the draft has NO physical audit/N.md, so layer-bound
        actions (Open / IA Copy / GG / CC / Rename / Delete / reorder) must be
        disabled -- they would otherwise operate on a previously selected old
        layer behind the draft. Save + Reload(discard) + editor stay live.
        ASSIGNMENT_PENDING: the delivery snapshot is immutable -- editor
        read-only, Save/Reload/[+] frozen until the worker finishes.
        NO_LAYER: the editor is the first audit's text. Save stays live and
        writes it; only the layer-bound actions drop, because there is nothing
        to open, rename or delete yet.
        LAYER_UNREADABLE: Save is dead here on purpose. The editor is showing a
        layer we could NOT read, so committing would write to a target nobody
        has verified.
        """
        state = self._action_state()
        pending = state == "ASSIGNMENT_PENDING"
        layer_bound = state in ("EXISTING_LAYER_CLEAN", "EXISTING_LAYER_DIRTY")
        # editor
        self.editor.setEnabled(self._project is not None)
        self.editor.setReadOnly(pending)
        # save / reload
        self.btn_save.setEnabled(
            state in (
                "EXISTING_LAYER_CLEAN",
                "EXISTING_LAYER_DIRTY",
                "MANAGED_DRAFT_IDLE",
                "MANAGED_DRAFT_DIRTY",
                "NO_LAYER",
            )
        )
        self.btn_reload.setEnabled(
            state in ("EXISTING_LAYER_CLEAN", "EXISTING_LAYER_DIRTY", "MANAGED_DRAFT_IDLE", "MANAGED_DRAFT_DIRTY")
        )
        # layer-bound actions need a REAL physical active layer
        physical = layer_bound and self._active_path() is not None
        self.btn_open.setEnabled(physical)
        self.btn_ia.setEnabled(physical)
        self.btn_gg.setEnabled(physical)
        self.btn_cc.setEnabled(physical)
        self.btn_rename.setEnabled(physical and not self._managed_active())
        self.btn_delete.setEnabled(physical)
        # navigation / creation
        self.btn_plus.setEnabled(
            self._project is not None and not pending and not self.managed_draft_project_id
        )
        self.btn_refresh.setEnabled(True)
        self.btn_rename.setToolTip(
            "SAIPEN owns layer numbers; rename the capture title in Inbox"
            if self._managed_active()
            else "Move this layer to a different free number (never overwrites)"
        )
        self._update_managed_action_state()

    def _rewatch(self):
        try:
            for p in list(self._watcher.directories()):
                self._watcher.removePath(p)
            for p in list(self._watcher.files()):
                self._watcher.removePath(p)
        except Exception:
            pass
        if self._project is None:
            self.header.setText("INAUDIT — no project selected")
            return
        d = inaudit_dir(self._project)
        if d is None:
            return
        self.header.setText(f"INAUDIT — {self._project.display_name} · {d}")
        try:
            # CORE-001: resolve any interrupted Save journal once at the
            # project-layer boundary before the layer is exposed as healthy.
            recover_inaudit_dir_commits(d)
        except Exception:
            pass
        try:
            self._watcher.addPath(str(Path(self._project.source_path).resolve()))
            if d.exists():
                self._watcher.addPath(str(d.resolve()))
                for lay in list_inaudit_layers(self._project):
                    try:
                        self._watcher.addPath(str(lay.path))
                    except Exception:
                        pass
        except Exception:
            pass

    def refresh(self):
        if self._dirty:
            self.status.setText("Layers changed on disk. Draft retained; Save or Reload before Refresh.")
            return
        if self._project is None:
            self.list.clear()
            self.editor.clear()
            self._dirty = False
            self._update_actions()
            return
        if self.managed_draft_project_id:
            # A managed draft owns the editor; a filesystem refresh must not
            # silently clear it. Re-render the list only.
            layers = list_inaudit_layers(self._project)
            self.list.blockSignals(True)
            self.list.clear()
            for lay in layers:
                empty = "  EMPTY" if lay.size_bytes == 0 else ""
                item = QListWidgetItem(f"[{lay.number}]  {lay.number}.md    {lay.size_str}{empty}")
                item.setData(Qt.ItemDataRole.UserRole, lay.number)
                self.list.addItem(item)
            self.list.blockSignals(False)
            self._rewatch()
            self._update_actions()
            return
        layers = list_inaudit_layers(self._project)
        sel = get_inaudit_selected(self._project)
        self.list.blockSignals(True)
        self.list.clear()
        for lay in layers:
            empty = "  EMPTY" if lay.size_bytes == 0 else ""
            item = QListWidgetItem(f"[{lay.number}]  {lay.number}.md    {lay.size_str}{empty}")
            item.setData(Qt.ItemDataRole.UserRole, lay.number)
            self.list.addItem(item)
        # select current
        idx = -1
        if sel is not None:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == sel:
                    idx = i
                    break
        if idx < 0 and self.list.count() > 0:
            idx = 0
            sel = self.list.item(0).data(Qt.ItemDataRole.UserRole)
            set_inaudit_selected(self._project, sel)
        if idx >= 0:
            self.list.setCurrentRow(idx)
        self.list.blockSignals(False)
        self._load_editor()
        self._rewatch()
        self._update_actions()

    def _on_fs_changed(self, _path: str = ""):
        self._debounce.start()

    def _on_debounced_fs(self):
        if self._dirty:
            self.status.setText("Layers changed on disk. Draft retained; Save or Reload before Refresh.")
            self._rewatch()
        else:
            self.refresh()
        try:
            if self._on_changed_cb:
                self._on_changed_cb(self._project)
            else:
                w = self.window()
                if w and hasattr(w, "model") and self._project:
                    w.model.refresh_inaudit(self._project.id)
                    w.viewport().update() if hasattr(w, "viewport") else None
        except Exception:
            pass

    def _on_row_changed(self, row: int):
        if row < 0 or self._project is None:
            return
        item = self.list.item(row)
        if not item:
            return
        if self._dirty:
            # T-174 I3: a dirty editor must NEVER be silently replaced by
            # selecting another row. Keep the text, the bound layer/draft and
            # restore the visible selection to it.
            keep = None
            if self._editor_path is not None:
                keep = self._editor_path.stem
            if keep is None and self.managed_draft_project_id:
                keep = None  # draft: restore nothing on disk, just the row
            if keep is not None:
                for r in range(self.list.count()):
                    if str(self.list.item(r).data(Qt.ItemDataRole.UserRole)) == keep:
                        self.list.blockSignals(True)
                        self.list.setCurrentRow(r)
                        self.list.blockSignals(False)
                        break
            self.status.setText("Unsaved edits — Save or Reload before switching layers")
            return
        num = item.data(Qt.ItemDataRole.UserRole)
        set_inaudit_selected(self._project, int(num) if num else None)
        self._load_editor()
        self._update_actions()
        try:
            if self._on_changed_cb:
                self._on_changed_cb(self._project)
            else:
                w = self.window()
                if w and hasattr(w, "model") and self._project:
                    w.model.refresh_inaudit(self._project.id)
        except Exception:
            pass

    def _load_editor(self):
        self._editor_path = None
        self._editor_bytes = None
        self._layer_read_error = ""
        if self._project is None:
            self.editor.clear()
            self._dirty = False
            self.lbl_dirty.setText("")
            return
        p = get_active_inaudit_path(self._project)
        if p is None or not p.is_file():
            self.editor.blockSignals(True)
            self.editor.clear()
            self.editor.blockSignals(False)
            self._dirty = False
            self.lbl_dirty.setText("")
            return
        try:
            self._editor_bytes = p.read_bytes()
            text = self._editor_bytes.decode("utf-8")
            self._editor_path = p
        except Exception as exc:
            # A layer we could not read is not "no layer". Blanking the editor
            # and carrying on is how a save lands on the wrong file, so the
            # failure is kept and reported instead of swallowed.
            text = ""
            self._editor_bytes = None
            self._layer_read_error = f"{p.name} could not be read: {exc}"
            self.status.setText(f"{self._layer_read_error} — Reload to try again")
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._dirty = False
        self.lbl_dirty.setText(str(p.name))

    def _on_editor_changed(self):
        self._dirty = True
        self.lbl_dirty.setText("* dirty — Save to persist")

    def _on_save(self):
        if self._project is None:
            self.status.setText("Save refused: select a project first")
            return
        if self.managed_draft_project_id:
            self._save_managed_draft()
            return
        p = self._editor_path
        if p is None:
            # No layer is bound. That is a real state with a real meaning here:
            # the operator is writing the FIRST canonical audit for this
            # project. Returning silently is what made Save look broken.
            self._save_as_first_layer()
            return
        new_bytes = self.editor.toPlainText().encode("utf-8")
        # CORE-001: durability lives in the framework-neutral primitive, not
        # here. It guarantees exact old-or-new canonical bytes after any
        # failure and never recreates a consumed layer.
        res = commit_existing_layer(p, self._editor_bytes or b"", new_bytes)
        if res.outcome == COMMITTED:
            self._editor_bytes = res.committed_bytes if res.committed_bytes is not None else new_bytes
            self._dirty = False
            self.lbl_dirty.setText(f"Saved {p.name}")
            self.status.setText(f"Saved {p.name}")
            self.refresh()
            return
        if res.outcome == MISSING_OR_CONSUMED:
            self.status.setText("Save refused: layer changed or was consumed. Copy your draft, then Reload.")
            return
        if res.outcome == CHANGED_EXTERNALLY:
            self.status.setText("Save refused: layer changed or was consumed. Copy your draft, then Reload.")
            return
        # IO_FAILED / RECOVERY_REQUIRED: draft stays dirty and preserved.
        self.status.setText(f"Save failed: {res.detail} — draft retained, try again")

    def _save_as_first_layer(self):
        """Save with no layer bound means: create this project's first audit.

        Two paths, both canonical. A managed project gets a draft IDENTITY
        adopted around the text that is already typed -- never a fresh blank
        editor -- and then delivers through CaptureStore -> assign -> SAIPEN
        enqueue, so SAIPEN still owns layer numbering and the T-175 stable
        capture UUID survives every retry. An unmanaged project takes the next
        legal number from the allocator and commits the exact bytes through the
        same durable primitive an existing layer uses.

        Refusals are visible: an unreadable layer is not "no layer", and an
        empty editor is not an instruction to create an empty file.
        """
        if self._layer_read_error:
            self.status.setText(
                f"Save refused: {self._layer_read_error} — Reload to try again"
            )
            return
        text = self.editor.toPlainText()
        if not text.strip():
            self.status.setText("Nothing to save")
            return
        if self._managed_active():
            self._adopt_managed_draft()
            self._save_managed_draft()
            return
        try:
            created = ensure_next_layer(self._project)
        except Exception as exc:
            self.status.setText(f"Save refused: could not create the first layer — {exc}")
            return
        new_bytes = text.encode("utf-8")
        res = commit_existing_layer(created, b"", new_bytes)
        if res.outcome == COMMITTED:
            self._editor_path = created
            self._editor_bytes = res.committed_bytes if res.committed_bytes is not None else new_bytes
            self._dirty = False
            self.lbl_dirty.setText(f"Saved {created.name}")
            self.status.setText(f"Created and saved {created.name} as the first audit for this project")
            self.refresh()
            return
        self.status.setText(f"Save failed: {res.detail} — text retained, try again")

    # ------------------------------------------------- managed draft (T-174)

    def _adopt_managed_draft(self) -> None:
        """Give this editor a managed-draft identity, keeping the text in it.

        Split from ``_start_managed_draft`` because [+] starts a draft around an
        EMPTY editor, while Save-on-first-audit adopts one around text the
        operator already wrote. Both allocate ONE stable capture UUID, reused
        for every retry -- the producer operation id T-158 idempotency relies on.
        """
        self.managed_draft_project_id = self._project.id
        self.managed_draft_capture_id = str(uuid.uuid4())
        self.managed_draft_delivery_pending = False
        self._editor_path = None
        self._editor_bytes = None
        self._layer_read_error = ""
        self.mode_tabs.setCurrentWidget(self.layers_page)
        self._update_actions()

    def _start_managed_draft(self):
        """Enter the managed-draft state: the editor becomes the draft surface.

        No audit/N.md is created, no layer number chosen, no ensure_next_layer
        call, no enqueue yet. ONE stable capture UUID is allocated here and
        reused for every Save retry -- the producer operation id is part of
        T-158 idempotency.
        """
        self._adopt_managed_draft()
        self.editor.blockSignals(True)
        self.editor.setPlainText("")
        self.editor.blockSignals(False)
        self._dirty = False
        self.lbl_dirty.setText("New managed audit draft")
        self.status.setText("New managed audit draft — type the audit, then Save")
        self.mode_tabs.setCurrentWidget(self.layers_page)
        # Derive the whole control surface from the new state: layer-bound
        # actions must drop immediately so no old selected layer masquerades
        # behind the draft (T-175 defect 3).
        self._update_actions()
        self.editor.setFocus()

    def _save_managed_draft(self):
        """Deliver the managed draft through the canonical store path (T-174 D/E/F).

        Text is snapshotted on the GUI thread; capture + canonical SAIPEN
        enqueue run on the TaskRunner. The draft capture UUID is stable, so
        capture() stays idempotent and _assign_managed() resolves the SAME
        SAIPEN operation on retry -- a failed delivery can never produce a
        second layer. A failed delivery keeps the draft recoverable.

        T-175: the shared ``inaudit:assign`` lane is exclusive at the
        workflow boundary -- a managed Save refuses while ANY assignment is
        active (Inbox or draft) instead of superseding the running TaskRunner
        generation, and the editor becomes read-only for the delivery's
        lifetime so the snapshot cannot drift under the worker.
        """
        if self._task_runner.is_running("inaudit:assign"):
            self.status.setText("Audit delivery is already in progress")
            return
        if self.managed_draft_delivery_pending:
            self.status.setText("Managed audit delivery in progress")
            return
        text = self.editor.toPlainText()
        if not text.strip():
            self.status.setText("Audit text is empty")
            return
        capture_id = self.managed_draft_capture_id
        if not capture_id:
            self.status.setText("Managed draft lost its operation id — press [+] to start a new draft")
            return
        project_id = self.managed_draft_project_id
        project = next((p for p in self._projects() if p.id == project_id), None)
        if project is None:
            self.status.setText("Managed draft's project is no longer registered")
            return
        projects = self._projects()
        self.managed_draft_delivery_pending = True
        self.editor.setReadOnly(True)
        self.status.setText("Delivering managed audit through SAIPEN...")

        def _deliver():
            self._capture_store.capture(
                {
                    "capture_id": capture_id,
                    "text": text,
                    "capture_kind": "audit",
                    "source": "desktop",
                    "source_title": "Desktop manual audit",
                    "project_hints": [project_id],
                },
                projects,
            )
            return self._capture_store.assign(capture_id, project_id, projects)

        def done(result):
            self._managed_delivery_finished(result, project_id, text)

        def failed(error):
            self.managed_draft_delivery_pending = False
            self.editor.setReadOnly(False)
            self._update_actions()
            self.status.setText(f"Managed delivery failed: {error}. Draft retained — press Save to retry.")

        self._task_runner.submit("inaudit:assign", _deliver, on_success=done, on_error=failed)
        self._update_actions()

    def _managed_delivery_finished(self, result, project_id, text):
        """Managed delivery completion (CORE-002 C5).

        Once the durable SAIPEN assignment is proven (layer read back), the
        draft is complete: bind the assigned path, mark the editor committed,
        clear delivery pending, restore mutability and clear draft identity
        BEFORE the secondary tracker attempt. A tracker failure must never
        escape through TaskRunner on_success and leave the editor wedged
        read-only with delivery pending, and must never re-enqueue.
        """
        try:
            self._managed_delivery_finish_impl(result, project_id, text)
        except Exception as exc:  # noqa: BLE001 -- never wedge the draft state
            # Delivery itself already committed durably; only the callback
            # state is at risk. Restore it, never re-enqueue.
            self.managed_draft_delivery_pending = False
            self.editor.setReadOnly(False)
            self._clear_managed_draft()
            self._update_actions()
            self.status.setText(f"Delivery completed; callback error: {exc}")
            logger.warning("managed delivery callback failed", exc_info=True)

    def _managed_delivery_finish_impl(self, result, project_id, text):
        # T-175 defect 6: the project may have been removed from the registry
        # while the worker was running. Never dereference None, never retry a
        # durable enqueue, never bind the layer to a different project: report
        # completed-but-unregistered and clear the busy state safely.
        project = next((p for p in self._projects() if p.id == project_id), None)
        assigned = Path(str(result["assigned_path"]))
        if project is None or not project.source_path:
            self.managed_draft_delivery_pending = False
            self.editor.setReadOnly(False)
            self._clear_managed_draft()
            self._update_actions()
            self.status.setText(
                f"Delivery completed, but project is no longer registered; assigned path: {assigned}"
            )
            return
        # Validate the SAIPEN-returned path is a canonical numbered layer of
        # this project's audit directory before ever treating it as ours.
        audit_dir = Path(project.source_path) / "audit"
        resolved = assigned.resolve()
        number = None
        if resolved.parent == audit_dir.resolve() and resolved.suffix == ".md" and resolved.stem.isdigit():
            number = int(resolved.stem)
        if number is None:
            self.managed_draft_delivery_pending = False
            self.editor.setReadOnly(False)
            self._update_actions()
            self.status.setText(
                f"Managed delivery returned an unexpected path ({assigned.name}) — draft retained"
            )
            return
        # Bind the editor to the REAL assigned layer before any state clears,
        # so durability is proven before the draft state is dropped.
        try:
            self._editor_bytes = resolved.read_bytes()
            delivered = self._editor_bytes.decode("utf-8")
        except OSError as exc:
            self.managed_draft_delivery_pending = False
            self.editor.setReadOnly(False)
            self._update_actions()
            self.status.setText(f"Managed delivery could not be read back: {exc}. Draft retained")
            return
        self._editor_path = resolved
        self.editor.blockSignals(True)
        self.editor.setPlainText(delivered)
        self.editor.blockSignals(False)
        self._dirty = False
        self.lbl_dirty.setText(str(resolved.name))
        # Delivery is DONE: clear pending, restore mutability, drop the draft
        # identity BEFORE the secondary tracker attempt (CORE-002 C5).
        self._clear_managed_draft()
        self.managed_draft_delivery_pending = False
        self.editor.setReadOnly(False)
        self._update_actions()
        # Only the explicit desktop managed-draft path marks the layer
        # user-created; ordinary Inbox/widget assignments never do (T-174 G).
        # Secondary: a failure here is a warning + repair intent, never a
        # re-enqueue and never a wedge.
        track_warning = record_user_layer(project, number)
        if track_warning == TRACKING_REPAIR_UNPERSISTED:
            self.status.setText(f"Created {resolved.name} via SAIPEN — WARNING: {track_warning}")
        elif track_warning:
            self.status.setText(f"Created {resolved.name} via SAIPEN — {track_warning}")
        self.refresh()
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == number:
                self.list.setCurrentRow(row)
                break
        if not track_warning:
            self.status.setText(f"Created {resolved.name} via SAIPEN")
        self.editor.setFocus()
        try:
            if self._on_changed_cb:
                self._on_changed_cb(project)
        except Exception:
            pass

    def _on_reload(self):
        if self.managed_draft_delivery_pending:
            # T-175 defect 2: Reload during delivery would clear the draft
            # UUID/state the async callback still owns. No mutation while the
            # worker is live.
            self.status.setText("Managed audit delivery is still in progress")
            return
        if self.managed_draft_project_id:
            # Explicit draft reset: no filesystem mutation, the capture UUID
            # and draft state are dropped and the editor returns to the
            # project's (empty or existing) layer view.
            self._clear_managed_draft()
            self._load_editor()
            self.status.setText("Managed draft discarded")
            return
        self._load_editor()
        self.status.setText("Reloaded from disk")

    def _active_path(self) -> Path | None:
        if self._project is None:
            return None
        p = get_active_inaudit_path(self._project)
        if p is None or not validate_inaudit_path(self._project, p):
            return None
        return p

    def _on_open(self):
        p = self._active_path()
        if p is None:
            self.status.setText("No INAUDIT layer selected")
            return
        try:
            os.startfile(str(p))
            self.status.setText(f"Opened {p.name}")
        except Exception as exc:
            self.status.setText(f"Open failed: {exc}")

    def _on_ia_copy(self):
        p = self._active_path()
        if p is None:
            self.status.setText("No INAUDIT layer selected")
            return
        QApplication.clipboard().setText(str(p))
        self.status.setText(f"IA copied: audit\\{p.name}")
        try:
            w = self.window()
            if w and hasattr(w, "_flash_status"):
                w._flash_status(f"IA copied: audit\\{p.name}", "#D4A840")
        except Exception:
            pass

    def _on_gg(self):
        p = self._active_path()
        if p is None:
            self.status.setText("No INAUDIT layer selected")
            return
        cmd = "saipen cc"
        QApplication.clipboard().setText(cmd)
        self.status.setText(f"GG copied: {cmd[:80]}")

    def _on_cc(self):
        p = self._active_path()
        if p is None:
            self.status.setText("No INAUDIT layer selected")
            return
        cmd = "saipen cc"
        QApplication.clipboard().setText(cmd)
        self.status.setText(f"CC copied: {cmd[:80]}")

    def _on_plus(self):
        self.create_and_focus_layer()

    def create_and_focus_layer(self):
        """Create the next numbered layer and put the caret in the editor.

        The bottom textbox IS where a manual audit is written: one [+], one
        new layer, the caret already in the box. No popup asks for the text
        first -- the operator types straight into the editor and presses Save.

        T-174: on a SAIPEN-managed project the backend guard stays (SAIPEN
        owns layer allocation), so [+] starts a managed DRAFT instead: the
        editor becomes the draft surface and Save delivers through the
        canonical capture -> SAIPEN enqueue path.
        """
        if self._project is None:
            self.status.setText("Select a project first")
            return
        if self._task_runner.is_running("inaudit:assign"):
            # T-175 defect 5: a disabled local button is not an invariant --
            # the Project Room row [+] reaches this method directly, and a new
            # draft must never start while a canonical assignment owns the
            # shared inaudit:assign lane.
            self.status.setText("Audit delivery is already in progress")
            return
        if self._dirty:
            # T-174 I1: refuse BEFORE any filesystem/capture mutation. The old
            # behavior created an empty layer, then failed the switch on the
            # dirty editor -- a physical file the operator never asked for.
            self.status.setText("Unsaved edits — Save or Reload before creating another layer")
            return
        if self.managed_draft_delivery_pending:
            self.status.setText("Managed audit delivery in progress")
            return
        self.mode_tabs.setCurrentWidget(self.layers_page)
        if self.managed_draft_project_id:
            # A draft already exists: focus it rather than starting a second one.
            self.editor.setFocus()
            return
        if self._managed_active():
            self._start_managed_draft()
            return
        try:
            p = ensure_next_layer(self._project)
        except Exception as exc:
            self.status.setText(f"Create failed: {exc}")
            return
        # CORE-002: the layer physically exists; a tracker failure is only a
        # repair-pending note, never a false "Create failed".
        self.status.setText(f"Created {p.name} — type the audit below, then Save")
        self.refresh()
        # focus new row
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == int(p.stem):
                self.list.setCurrentRow(i)
                break
        self.editor.setFocus()

    def _on_rename_layer(self):
        """Move the selected layer to a different free number (T-144).

        The canonical name IS the number, so renaming here means renumbering, and
        a taken number is refused rather than overwritten -- another agent may be
        working that layer right now.
        """
        if self._project is None:
            self.status.setText("Select a project first")
            return
        if self._dirty:
            # T-174 I2: renaming moves the physical layer; with the editor
            # still pointing at the old path the next Save would refuse.
            self.status.setText("Unsaved edits — Save or Reload before renaming")
            return
        item = self.list.currentItem()
        if item is None:
            self.status.setText("Select a layer first")
            return
        current = int(item.data(Qt.ItemDataRole.UserRole))
        taken = sorted(layer.number for layer in list_inaudit_layers(self._project))
        proposal = next(n for n in range(1, (max(taken) if taken else 0) + 2) if n not in taken)
        number, ok = QInputDialog.getInt(
            self,
            "Rename INAUDIT layer",
            f"Move layer {current} to number (taken: {', '.join(str(n) for n in taken) or 'none'}):",
            proposal,
            1,
            9999,
        )
        if not ok or number == current:
            return
        reason = rename_inaudit_layer(self._project, current, number)
        if reason:
            self.status.setText(f"Rename failed: {reason}")
            return
        self.status.setText(f"Layer {current} is now {number}.md")
        self.refresh()
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == number:
                self.list.setCurrentRow(row)
                break
        try:
            if self._on_changed_cb:
                self._on_changed_cb(self._project)
        except Exception:
            pass

    def _on_reorder(self, source_row: int, target_row: int):
        """Drag-and-drop layer priority (T-162/T-163).

        The list shows layers in canonical number order, so "move row A to
        row B's position" translates directly into a new order of the CURRENT
        numbers; reorder_inaudit_layers renumbers the files to match and
        refresh() re-renders from disk, so the widget can never disagree with
        what actually happened.
        """
        if self._project is None:
            self.status.setText("Select a project first")
            return
        if self._dirty:
            self.status.setText("Unsaved edits — Save or Reload before reordering")
            return
        rows = [
            int(self.list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.list.count())
        ]
        if not 0 <= source_row < len(rows):
            return
        number = rows.pop(source_row)
        target_row = max(0, min(target_row, len(rows)))
        rows.insert(target_row, number)
        # CORE-003 D7: ONE stable operation identity per drop gesture. The
        # same id replays as already-committed success; a new drag gesture
        # generates a new id and is a real new operation.
        operation_id = str(uuid.uuid4())
        reason = reorder_inaudit_layers(self._project, rows, operation_id=operation_id)
        if reason:
            self.status.setText(f"Reorder failed: {reason}")
            self.refresh()
            return
        self.status.setText("Layer order updated (priority = top to bottom)")
        self.refresh()
        if 0 <= target_row < self.list.count():
            self.list.setCurrentRow(target_row)
        try:
            if self._on_changed_cb:
                self._on_changed_cb(self._project)
        except Exception:
            pass

    def focus_last_user_layer(self) -> bool:
        """Select the most recent operator-created layer. False when the
        operator has never made one, so the caller can say so instead of
        silently landing on a widget-delivered layer."""
        if self._project is None:
            return False
        if self._dirty:
            # T-174 I4: refresh() would refuse on the dirty editor while the
            # selection could still move; no layer switch while dirty.
            self.status.setText("Unsaved edits — Save or Reload before switching layers")
            return False
        self.refresh()
        number = last_user_layer(self._project)
        if number is None:
            self.status.setText("No custom (user-created) layer yet — use [+] first")
            return False
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == number:
                self.list.setCurrentRow(row)
                self.status.setText(f"Editing your last custom layer: {number}.md")
                return True
        self.status.setText(f"Custom layer {number}.md is gone — create a new one with [+]")
        return False

    def _on_delete(self):
        """Deletes the currently selected layer.

        Edge cases surfaced to the user instead of failing silently:
          - no project / no selection -> status hint.
          - last remaining layer -> directory stays, empty state shown.
          - file locked by another process -> exact reason, nothing deleted.
          - selection falls back to the next remaining layer after delete.
        """
        if self._project is None:
            self.status.setText("Select a project first")
            return
        row = self.list.currentRow()
        if row < 0:
            self.status.setText("No INAUDIT layer selected to delete")
            return
        item = self.list.item(row)
        if not item:
            return
        num = item.data(Qt.ItemDataRole.UserRole)
        try:
            number = int(num)
        except (TypeError, ValueError):
            self.status.setText("Invalid layer entry")
            return
        if self._dirty:
            self.status.setText("Unsaved edits in the editor — Save or Reload before deleting the layer")
            return
        reason = delete_inaudit_layer(self._project, number)
        if reason:
            self.status.setText(f"Delete failed: {reason}")
            return
        self.status.setText(f"Deleted {number}.md (no renumbering)")
        self.refresh()
        try:
            if self._on_changed_cb:
                self._on_changed_cb(self._project)
            else:
                w = self.window()
                if w and hasattr(w, "model"):
                    w.model.refresh_inaudit(self._project.id)
        except Exception:
            pass
