"""Launcher Edit Dialog for adding and editing agent launchers."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class LauncherEditDialog(QDialog):
    """Win95 dark golden Add/Edit Launcher modal dialog."""

    def __init__(
        self,
        parent=None,
        launcher=None,
    ):
        super().__init__(parent)
        self.launcher = launcher

        self.setWindowTitle("Edit Launcher" if launcher else "Add Launcher")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        # 1. ID (unique key)
        self.ent_id = QLineEdit(self)
        if launcher:
            self.ent_id.setText(launcher.id)
        self.ent_id.setPlaceholderText("e.g. opencode, freebuff, cline, custom_agent")
        form.addRow("ID:", self.ent_id)

        # 2. Display Name
        self.ent_name = QLineEdit(self)
        if launcher:
            self.ent_name.setText(launcher.name)
        self.ent_name.setPlaceholderText("e.g. OpenCode, FreeBuff, Cline")
        form.addRow("Name:", self.ent_name)

        # 3. Short Label (button text)
        self.ent_short_label = QLineEdit(self)
        if launcher:
            self.ent_short_label.setText(launcher.short_label)
        self.ent_short_label.setPlaceholderText("e.g. 1, 2, 3, OC, FB")
        self.ent_short_label.setMaximumWidth(80)
        form.addRow("Button Label:", self.ent_short_label)

        # 4. Command Template (optional, for custom launchers)
        self.ent_command = QLineEdit(self)
        if launcher:
            self.ent_command.setText(launcher.command_template)
        self.ent_command.setPlaceholderText("e.g. Set-Location '{workdir}'; my-agent --auto")
        form.addRow("Command Template:", self.ent_command)

        # 5. Agent Type
        self.combo_agent_type = QComboBox(self)
        for atype in ["powershell", "cmd", "executable", "custom"]:
            self.combo_agent_type.addItem(atype)
        if launcher:
            idx = self.combo_agent_type.findText(launcher.agent_type)
            if idx >= 0:
                self.combo_agent_type.setCurrentIndex(idx)
        form.addRow("Agent Type:", self.combo_agent_type)

        # 6. Enabled
        self.chk_enabled = QCheckBox("Enabled", self)
        self.chk_enabled.setChecked(launcher.enabled if launcher else True)
        form.addRow("", self.chk_enabled)

        # 7. Global capacity. Zero keeps launchers such as OpenCode unlimited;
        # FreeBuff defaults to one because its client supports one active tab.
        self.spin_max_instances = QSpinBox(self)
        self.spin_max_instances.setRange(0, 99)
        self.spin_max_instances.setSpecialValueText("Unlimited")
        self.spin_max_instances.setValue(int(getattr(launcher, "max_instances", 0) or 0) if launcher else 0)
        form.addRow("Max Running:", self.spin_max_instances)

        layout.addLayout(form)

        # Help text
        help_lbl = QLabel(
            "Use {workdir} and {name} as placeholders in command template.\n"
            "Leave command empty to use built-in handler for known agents.",
            self,
        )
        help_lbl.setStyleSheet("color: #9C9371; font-size: 9px;")
        help_lbl.setWordWrap(True)
        layout.addWidget(help_lbl)

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

    def _on_save(self):
        lid = self.ent_id.text().strip()
        name = self.ent_name.text().strip()
        if not lid:
            self.ent_id.setFocus()
            return
        if not name:
            self.ent_name.setFocus()
            return
        self.accept()

    def get_data(self) -> dict:
        return {
            "id": self.ent_id.text().strip(),
            "name": self.ent_name.text().strip(),
            "short_label": self.ent_short_label.text().strip(),
            "command_template": self.ent_command.text().strip(),
            "agent_type": self.combo_agent_type.currentText().strip(),
            "enabled": self.chk_enabled.isChecked(),
            "max_instances": self.spin_max_instances.value(),
        }
