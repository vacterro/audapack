"""Project Add/Edit Dialog for Qt (Wave M parity)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from audapack.models import CANONICAL_GROUPS, Project


class ProjectEditDialog(QDialog):
    """Win95 dark golden Add/Edit Project modal dialog."""

    def __init__(
        self,
        parent=None,
        project: Optional[Project] = None,
        default_group: str = "MAIN0",
        default_slot: int = 1,
        active_groups: Optional[list[str]] = None,
    ):
        super().__init__(parent)
        self.project = project
        self.active_groups = active_groups or list(CANONICAL_GROUPS)

        self.setWindowTitle("Edit Project" if project else "Add Project")
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        # 1. Display Name
        self.ent_name = QLineEdit(self)
        if project:
            self.ent_name.setText(project.display_name)
        form.addRow("Display Name:", self.ent_name)

        # 2. Source Path + Browse
        path_row = QWidget(self)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)

        self.ent_path = QLineEdit(self)
        if project:
            self.ent_path.setText(str(project.source_path))
        self.btn_browse = QPushButton("Browse...", self)
        self.btn_browse.clicked.connect(self._on_browse)

        path_layout.addWidget(self.ent_path)
        path_layout.addWidget(self.btn_browse)
        form.addRow("Source Path:", path_row)

        # 3. Priority Group
        self.combo_group = QComboBox(self)
        for g in self.active_groups:
            self.combo_group.addItem(g.upper())
        cur_grp = (project.priority_group if project else default_group).upper()
        idx = self.combo_group.findText(cur_grp)
        if idx >= 0:
            self.combo_group.setCurrentIndex(idx)
        form.addRow("Priority Group:", self.combo_group)

        # 4. Slot
        self.spin_slot = QSpinBox(self)
        self.spin_slot.setRange(1, 10)
        self.spin_slot.setValue(project.slot if project else default_slot)
        form.addRow("Slot:", self.spin_slot)

        # 5. Archive Name (optional)
        self.ent_archive = QLineEdit(self)
        if project and project.archive_name:
            self.ent_archive.setText(project.archive_name)
        form.addRow("Archive Name:", self.ent_archive)

        # 6. Audit Project Name (optional)
        self.ent_audit_name = QLineEdit(self)
        if project and project.audit_project_name:
            self.ent_audit_name.setText(project.audit_project_name)
        form.addRow("Audit Name:", self.ent_audit_name)

        # 7. Deterministic INAUDIT aliases (optional, comma-separated)
        self.ent_inaudit_aliases = QLineEdit(self)
        if project and project.inaudit_aliases:
            self.ent_inaudit_aliases.setText(", ".join(project.inaudit_aliases))
        self.ent_inaudit_aliases.setToolTip("Comma-separated exact aliases used only for deterministic Inbox suggestions")
        form.addRow("INAUDIT Aliases:", self.ent_inaudit_aliases)

        # 8. Enabled
        self.chk_enabled = QCheckBox("Enabled for batch operations", self)
        self.chk_enabled.setChecked(project.enabled if project else True)
        form.addRow("", self.chk_enabled)

        layout.addLayout(form)

        # Button box
        btn_box = QWidget(self)
        btn_layout = QHBoxLayout(btn_box)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch()

        self.btn_save = QPushButton("Save", self)
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_cancel = QPushButton("Cancel", self)
        self.btn_cancel.clicked.connect(self.reject)

        btn_layout.addWidget(self.btn_save)
        btn_layout.addWidget(self.btn_cancel)
        layout.addWidget(btn_box)

    def _on_browse(self):
        cur_dir = self.ent_path.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Select Project Folder", cur_dir)
        if selected:
            self.ent_path.setText(selected)
            if not self.ent_name.text().strip():
                self.ent_name.setText(Path(selected).name)

    def _on_save(self):
        name = self.ent_name.text().strip()
        path_str = self.ent_path.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Display Name cannot be empty.")
            self.ent_name.setFocus()
            return
        if not path_str:
            QMessageBox.warning(self, "Validation Error", "Source Path cannot be empty.")
            self.ent_path.setFocus()
            return

        self.accept()

    def get_data(self) -> dict:
        return {
            "display_name": self.ent_name.text().strip(),
            "source_path": self.ent_path.text().strip(),
            "priority_group": self.combo_group.currentText().strip().upper(),
            "slot": self.spin_slot.value(),
            "archive_name": self.ent_archive.text().strip(),
            "audit_project_name": self.ent_audit_name.text().strip(),
            "inaudit_aliases": [
                value.strip()
                for value in self.ent_inaudit_aliases.text().split(",")
                if value.strip()
            ],
            "enabled": self.chk_enabled.isChecked(),
        }
