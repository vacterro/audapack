"""Minimal Qt settings dialog (Wave L parity). No schema redesign."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from audapack.config import save_config


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("AUDAPACK Settings")

        tabs = QTabWidget()
        general = self._build_general()
        packing = self._build_packing()
        audit = self._build_audit()
        bridge = self._build_bridge()
        tabs.addTab(general, "General")
        tabs.addTab(packing, "Packing")
        tabs.addTab(audit, "Audit")
        tabs.addTab(bridge, "Bridge")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ---------------------------------------------------------------- builders

    def _build_general(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.ui_language = QLineEdit(self._config.ui.ui_language)
        f.addRow("UI language", self.ui_language)
        self.reply_language = QLineEdit(self._config.ui.reply_language)
        f.addRow("Reply language", self.reply_language)
        return w

    def _build_packing(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.output_dir = QLineEdit(self._config.packing.output_dir)
        f.addRow("Output dir", self.output_dir)
        self.delete_old = QCheckBox()
        self.delete_old.setChecked(self._config.packing.delete_old)
        f.addRow("Delete old archives", self.delete_old)
        self.manifest = QCheckBox()
        self.manifest.setChecked(self._config.packing.manifest_enabled)
        f.addRow("Include manifest", self.manifest)
        return w

    def _build_audit(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.audit_root = QLineEdit(self._config.audits.root)
        f.addRow("Audit root", self.audit_root)
        self.hot = QSpinBox()
        self.hot.setValue(self._config.audits.hot_seconds)
        f.addRow("Hot seconds", self.hot)
        self.warm = QSpinBox()
        self.warm.setValue(self._config.audits.warm_seconds)
        f.addRow("Warm seconds", self.warm)
        return w

    def _build_bridge(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.host = QLineEdit(self._config.bridge.host)
        f.addRow("Host", self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self._config.bridge.port)
        f.addRow("Port", self.port)
        self.autostart = QCheckBox()
        self.autostart.setChecked(self._config.bridge.autostart)
        f.addRow("Autostart", self.autostart)
        note = QLabel("Bridge token is stored under %LOCALAPPDATA%\\AUDAPACK\\secrets and is never edited here.")
        note.setWordWrap(True)
        f.addRow("", note)
        return w

    def _save(self):
        c = self._config
        c.ui.ui_language = self.ui_language.text().strip() or c.ui.ui_language
        c.ui.reply_language = self.reply_language.text().strip() or c.ui.reply_language
        c.packing.output_dir = self.output_dir.text().strip()
        c.packing.delete_old = self.delete_old.isChecked()
        c.packing.manifest_enabled = self.manifest.isChecked()
        c.audits.root = self.audit_root.text().strip()
        c.audits.hot_seconds = self.hot.value()
        c.audits.warm_seconds = self.warm.value()
        c.bridge.host = self.host.text().strip()
        c.bridge.port = self.port.value()
        c.bridge.autostart = self.autostart.isChecked()
        save_config(c)
        self.accept()
