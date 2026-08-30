from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from audapack.inaudit import (
    delete_inaudit_layer,
    ensure_next_layer,
    get_active_inaudit_path,
    get_inaudit_selected,
    inaudit_dir,
    list_inaudit_layers,
    set_inaudit_selected,
    validate_inaudit_path,
)
from audapack.models import Project
from audapack.ui_qt.theme.golden_default import GoldenDefault


class _InauditLayerList(QListWidget):
    """QListWidget that forwards the Delete key to the owning widget."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete and self._owner is not None:
            self._owner._on_delete()
            event.accept()
            return
        super().keyPressEvent(event)


class InauditWidget(QWidget):
    def __init__(self, parent=None, on_changed=None):
        super().__init__(parent)
        self._on_changed_cb = on_changed
        self._project: Project | None = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_fs_changed)
        self._watcher.fileChanged.connect(self._on_fs_changed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._on_debounced_fs)
        self._dirty = False

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        self.header = QLabel("INAUDIT — no project selected", self)
        hf = QFont("Verdana", 9, QFont.Weight.Bold)
        hf.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.header.setFont(hf)
        root.addWidget(self.header)

        self.list = _InauditLayerList(owner=self, parent=self)
        self.list.setMinimumHeight(90)
        self.list.setMaximumHeight(140)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.itemDoubleClicked.connect(lambda _: self._on_open())
        root.addWidget(self.list)

        btn_row = QWidget(self)
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        self.btn_open = QPushButton("Open", btn_row)
        self.btn_open.clicked.connect(self._on_open)
        self.btn_ia = QPushButton("IA Copy", btn_row)
        self.btn_ia.setToolTip("Copy selected INAUDIT path\nShift: saipen gg \"path\"\nCtrl: saipen cc \"path\"")
        self.btn_ia.clicked.connect(self._on_ia_copy)
        self.btn_gg = QPushButton("GG", btn_row)
        self.btn_gg.setToolTip('Copy saipen gg "path"')
        self.btn_gg.clicked.connect(self._on_gg)
        self.btn_cc = QPushButton("CC", btn_row)
        self.btn_cc.setToolTip('Copy saipen cc "path"')
        self.btn_cc.clicked.connect(self._on_cc)
        self.btn_plus = QPushButton("+", btn_row)
        self.btn_plus.setToolTip("Create next numbered layer")
        self.btn_plus.clicked.connect(self._on_plus)
        self.btn_delete = QPushButton("Delete", btn_row)
        self.btn_delete.setToolTip("Delete the selected layer (also: Del key on the list). No renumbering.")
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_refresh = QPushButton("Refresh", btn_row)
        self.btn_refresh.clicked.connect(self.refresh)
        for b in (self.btn_open, self.btn_ia, self.btn_gg, self.btn_cc, self.btn_plus, self.btn_delete, self.btn_refresh):
            bl.addWidget(b)
        bl.addStretch(1)
        root.addWidget(btn_row)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlaceholderText("Select a layer to view/edit. Plain UTF-8 Markdown.")
        ef = QFont("Consolas", 9)
        ef.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.editor.setFont(ef)
        self.editor.textChanged.connect(self._on_editor_changed)
        root.addWidget(self.editor, 1)

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
        root.addWidget(edit_row)

        self.status = QLabel("Select a layer, then IA / GG / CC.", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.setStyleSheet(GoldenDefault.qss())
        self._update_actions()

    def set_project(self, project: Project | None):
        self._project = project
        self._rewatch()
        self.refresh()

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
        if self._project is None:
            self.list.clear()
            self.editor.clear()
            self._dirty = False
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
            text = p.read_text(encoding="utf-8")
        except Exception:
            text = ""
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
            return
        p = get_active_inaudit_path(self._project)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.editor.toPlainText(), encoding="utf-8")
            self._dirty = False
            self.lbl_dirty.setText(f"Saved {p.name}")
            self.status.setText(f"Saved {p.name}")
            self.refresh()
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")

    def _on_reload(self):
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
        cmd = f'saipen gg "{p}"'
        QApplication.clipboard().setText(cmd)
        self.status.setText(f"GG copied: {cmd[:80]}")

    def _on_cc(self):
        p = self._active_path()
        if p is None:
            self.status.setText("No INAUDIT layer selected")
            return
        cmd = f'saipen cc "{p}"'
        QApplication.clipboard().setText(cmd)
        self.status.setText(f"CC copied: {cmd[:80]}")

    def _on_plus(self):
        if self._project is None:
            self.status.setText("Select a project first")
            return
        try:
            p = ensure_next_layer(self._project)
            self.status.setText(f"Created {p.name}")
            self.refresh()
            # focus new row
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == int(p.stem):
                    self.list.setCurrentRow(i)
                    break
        except Exception as exc:
            self.status.setText(f"Create failed: {exc}")

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

    def _update_actions(self):
        has = self._active_path() is not None
        for b in (self.btn_open, self.btn_ia, self.btn_gg, self.btn_cc, self.btn_save, self.btn_reload):
            b.setEnabled(has if b not in (self.btn_plus, self.btn_refresh) else True)
        self.btn_plus.setEnabled(self._project is not None)
        self.btn_delete.setEnabled(has)
        self.editor.setEnabled(has or self._project is not None)
