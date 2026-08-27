"""Qt settings widget and dialog (Wave L/M parity). No schema redesign."""

import os
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from audapack.components.manager import ComponentManager
from audapack.config import (
    OUTPUT_LAYOUT_ALONGSIDE_PROJECTS,
    OUTPUT_LAYOUT_CHOICES,
    OUTPUT_LAYOUT_GROUPED_BY_PRIORITY,
    OUTPUT_LAYOUT_SINGLE_FOLDER,
    normalize_output_layout,
    save_config,
)
from audapack.services.bridge_service import BridgeService

# Human-readable labels for the output-layout combo. Keep the data value as
# the canonical key (one of OUTPUT_LAYOUT_CHOICES) so the on-disk config is
# stable across UI translations.
_OUTPUT_LAYOUT_OPTIONS = (
    (
        OUTPUT_LAYOUT_SINGLE_FOLDER,
        "Single folder (all archives in Output dir)",
    ),
    (
        OUTPUT_LAYOUT_ALONGSIDE_PROJECTS,
        "Alongside projects (archive as sibling of each project folder)",
    ),
    (
        OUTPUT_LAYOUT_GROUPED_BY_PRIORITY,
        "Group subfolders (organized by MAIN0, SIDE0, ... in Output dir / _ARCHIVES)",
    ),
)


class SettingsWidget(QWidget):
    saved = Signal()

    def __init__(self, config, parent=None, on_saved=None):
        super().__init__(parent)
        self._config = config
        self._on_saved = on_saved

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.sub_tabs = QTabWidget(self)
        self.general_widget = self._build_general()
        self.packing_widget = self._build_packing()
        self.audit_widget = self._build_audit()
        self.bridge_widget = self._build_bridge()
        self.sub_tabs.addTab(self.general_widget, "General")
        self.sub_tabs.addTab(self.packing_widget, "Packing")
        self.sub_tabs.addTab(self.audit_widget, "Audit")
        self.sub_tabs.addTab(self.bridge_widget, "Bridge")
        layout.addWidget(self.sub_tabs)

        btn_row = QWidget(self)
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_save_status = QLabel("✓ Settings auto-save active", self)
        self.lbl_save_status.setStyleSheet("color: #9C9371; font-size: 10px;")
        btn_layout.addWidget(self.lbl_save_status)
        btn_layout.addStretch()

        self.save_btn = QPushButton("Save Settings", self)
        self.save_btn.clicked.connect(self._save)
        btn_layout.addWidget(self.save_btn)

        layout.addWidget(btn_row)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(200)
        self._autosave_timer.timeout.connect(self._save)

        self._wire_autosave()

    def _wire_autosave(self):
        # Text fields -> debounced auto-save
        self.ui_language.textChanged.connect(lambda: self._autosave_timer.start())
        self.reply_language.textChanged.connect(lambda: self._autosave_timer.start())
        self.output_dir.textChanged.connect(lambda: self._autosave_timer.start())
        self.audit_root.textChanged.connect(lambda: self._autosave_timer.start())
        self.host.textChanged.connect(lambda: self._autosave_timer.start())

        # Spinboxes -> debounced auto-save
        self.hot.valueChanged.connect(lambda: self._autosave_timer.start())
        self.warm.valueChanged.connect(lambda: self._autosave_timer.start())
        self.port.valueChanged.connect(lambda: self._autosave_timer.start())

        # Checkboxes and Dropdowns -> immediate auto-save
        self.output_layout.currentIndexChanged.connect(lambda: self._save())
        self.delete_old.toggled.connect(lambda: self._save())
        self.include_timestamp.toggled.connect(lambda: self._save())
        self.manifest.toggled.connect(lambda: self._save())
        self.autostart.toggled.connect(lambda: self._save())

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
        # CORE-009: archive output layout switch (T-26).
        self.output_layout = QComboBox()
        current_layout = normalize_output_layout(self._config.packing.output_layout)
        for value, label in _OUTPUT_LAYOUT_OPTIONS:
            self.output_layout.addItem(label, userData=value)
        idx = self.output_layout.findData(current_layout)
        if idx >= 0:
            self.output_layout.setCurrentIndex(idx)
        f.addRow("Archive layout", self.output_layout)
        self.delete_old = QCheckBox()
        self.delete_old.setChecked(self._config.packing.delete_old)
        f.addRow("Delete old archives", self.delete_old)
        self.include_timestamp = QCheckBox()
        self.include_timestamp.setChecked(getattr(self._config.packing, "include_timestamp", True))
        f.addRow("Timestamp in filename (DD.MM.YY-THH-MM-SS)", self.include_timestamp)
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
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self._bridge_service = BridgeService(self._config)
        self._comp_mgr = ComponentManager(self._config)

        # 1. Config Form Group
        grp_cfg = QGroupBox("Bridge Configuration", w)
        f = QFormLayout(grp_cfg)
        f.setContentsMargins(8, 8, 8, 8)
        self.host = QLineEdit(self._config.bridge.host)
        f.addRow("Host", self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self._config.bridge.port)
        f.addRow("Port", self.port)
        self.autostart = QCheckBox("Start with Windows")
        self.autostart.setChecked(self._config.bridge.autostart)
        f.addRow("Autostart", self.autostart)
        layout.addWidget(grp_cfg)

        # 2. Live Status Group
        grp_status = QGroupBox("Live Bridge Status & Actions", w)
        s_layout = QVBoxLayout(grp_status)
        s_layout.setContentsMargins(8, 8, 8, 8)
        s_layout.setSpacing(6)

        self.lbl_bridge_state = QLabel("CHECKING...", grp_status)
        self.lbl_bridge_state.setStyleSheet("font-weight: bold; font-size: 11px;")
        s_layout.addWidget(self.lbl_bridge_state)

        self.lbl_bridge_details = QLabel("", grp_status)
        self.lbl_bridge_details.setWordWrap(True)
        s_layout.addWidget(self.lbl_bridge_details)

        # Token Row
        tok_row = QWidget(grp_status)
        tok_layout = QHBoxLayout(tok_row)
        tok_layout.setContentsMargins(0, 0, 0, 0)
        tok_layout.setSpacing(6)

        self.ent_bridge_token = QLineEdit(grp_status)
        self.ent_bridge_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.ent_bridge_token.setReadOnly(True)
        tok_btn_show = QPushButton("Show", grp_status)
        tok_btn_show.clicked.connect(self._toggle_bridge_token_visibility)
        tok_btn_copy = QPushButton("Copy Token", grp_status)
        tok_btn_copy.clicked.connect(self._copy_bridge_token)

        tok_layout.addWidget(QLabel("Auth Token:", grp_status))
        tok_layout.addWidget(self.ent_bridge_token)
        tok_layout.addWidget(tok_btn_show)
        tok_layout.addWidget(tok_btn_copy)
        s_layout.addWidget(tok_row)

        # Actions Buttons Row
        act_row = QWidget(grp_status)
        act_layout = QHBoxLayout(act_row)
        act_layout.setContentsMargins(0, 4, 0, 0)
        act_layout.setSpacing(6)

        self.btn_bridge_start = QPushButton("Start Bridge", grp_status)
        self.btn_bridge_start.clicked.connect(self._on_bridge_start)
        self.btn_bridge_stop = QPushButton("Stop Bridge", grp_status)
        self.btn_bridge_stop.clicked.connect(self._on_bridge_stop)
        self.btn_bridge_restart = QPushButton("Restart Bridge", grp_status)
        self.btn_bridge_restart.clicked.connect(self._on_bridge_restart)

        act_layout.addWidget(self.btn_bridge_start)
        act_layout.addWidget(self.btn_bridge_stop)
        act_layout.addWidget(self.btn_bridge_restart)
        s_layout.addWidget(act_row)

        # Helper integration tools
        hlp_row = QWidget(grp_status)
        hlp_layout = QHBoxLayout(hlp_row)
        hlp_layout.setContentsMargins(0, 4, 0, 0)
        hlp_layout.setSpacing(6)

        btn_install_widget = QPushButton("Install/Update Widget in Browser", grp_status)
        btn_install_widget.clicked.connect(self._on_bridge_install_widget)

        btn_open_audits = QPushButton("Open Audits Folder", grp_status)
        btn_open_audits.clicked.connect(self._on_bridge_open_audits)

        hlp_layout.addWidget(btn_install_widget)
        hlp_layout.addWidget(btn_open_audits)
        s_layout.addWidget(hlp_row)

        layout.addWidget(grp_status)
        layout.addStretch()

        self._refresh_bridge_status()
        return w

    def _refresh_bridge_status(self):
        st = self._bridge_service.status()
        healthy = st.get("healthy", False)
        info = st.get("health_info", {})
        auto = st.get("autostart", {}).get("status_text", "?")

        token = self._comp_mgr.get_bridge_token()
        self.ent_bridge_token.setText(str(token) if token else "")

        if healthy:
            self.lbl_bridge_state.setText(f"✓ BRIDGE CONNECTED (Port {self._config.bridge.port})")
            self.lbl_bridge_state.setStyleSheet("color: #4A7A20; font-weight: bold; font-size: 11px;")
            ver = info.get("version", "?")
            api_ver = info.get("api_version", "?")
            self.lbl_bridge_details.setText(
                f"Service: AUDAPACK Bridge {ver} (API v{api_ver}) · Output Root: {self._config.audits.root}\n"
                f"Windows Autostart: {auto}"
            )
            self.btn_bridge_start.setEnabled(False)
            self.btn_bridge_stop.setEnabled(True)
            self.btn_bridge_restart.setEnabled(True)
        else:
            self.lbl_bridge_state.setText(f"✗ BRIDGE OFFLINE (Port {self._config.bridge.port})")
            self.lbl_bridge_state.setStyleSheet("color: #D66464; font-weight: bold; font-size: 11px;")
            self.lbl_bridge_details.setText(
                f"Bridge is not running on localhost:{self._config.bridge.port} · Output Root: {self._config.audits.root}\n"
                f"Windows Autostart: {auto}"
            )
            self.btn_bridge_start.setEnabled(True)
            self.btn_bridge_stop.setEnabled(False)
            self.btn_bridge_restart.setEnabled(False)

    def _toggle_bridge_token_visibility(self):
        if self.ent_bridge_token.echoMode() == QLineEdit.EchoMode.Password:
            self.ent_bridge_token.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.ent_bridge_token.setEchoMode(QLineEdit.EchoMode.Password)

    def _copy_bridge_token(self):
        tok = self.ent_bridge_token.text()
        QApplication.clipboard().setText(tok)
        self.lbl_bridge_state.setText("✓ Token copied to clipboard")
        self.lbl_bridge_state.setStyleSheet("color: #4A7A20; font-weight: bold; font-size: 11px;")

    def _on_bridge_start(self):
        self.lbl_bridge_state.setText("STARTING BRIDGE...")
        self.lbl_bridge_state.setStyleSheet("color: #C89A3C; font-weight: bold; font-size: 11px;")
        self.btn_bridge_start.setEnabled(False)
        QApplication.processEvents()
        ok, msg = self._bridge_service.start()
        self._refresh_bridge_status()
        if not ok:
            self.lbl_bridge_state.setText(f"✗ Start failed: {msg}")
            self.lbl_bridge_state.setStyleSheet("color: #D9534F; font-weight: bold; font-size: 11px;")

    def _on_bridge_stop(self):
        self.lbl_bridge_state.setText("STOPPING BRIDGE...")
        self.lbl_bridge_state.setStyleSheet("color: #C89A3C; font-weight: bold; font-size: 11px;")
        self.btn_bridge_stop.setEnabled(False)
        QApplication.processEvents()
        ok, msg = self._bridge_service.stop()
        self._refresh_bridge_status()
        if not ok:
            self.lbl_bridge_state.setText(f"✗ Stop failed: {msg}")
            self.lbl_bridge_state.setStyleSheet("color: #D9534F; font-weight: bold; font-size: 11px;")

    def _on_bridge_restart(self):
        self.lbl_bridge_state.setText("RESTARTING BRIDGE...")
        self.lbl_bridge_state.setStyleSheet("color: #C89A3C; font-weight: bold; font-size: 11px;")
        self.btn_bridge_restart.setEnabled(False)
        QApplication.processEvents()
        ok, msg = self._bridge_service.restart()
        self._refresh_bridge_status()
        if not ok:
            self.lbl_bridge_state.setText(f"✗ Restart failed: {msg}")
            self.lbl_bridge_state.setStyleSheet("color: #D9534F; font-weight: bold; font-size: 11px;")

    def _on_bridge_install_widget(self):
        ok, msg = self._comp_mgr.trigger_widget_install()
        self.lbl_bridge_state.setText(f"✓ {msg}" if ok else f"✗ {msg}")
        self.lbl_bridge_state.setStyleSheet("color: #4A7A20; font-weight: bold; font-size: 11px;" if ok else "color: #D9534F; font-weight: bold; font-size: 11px;")

    def _on_bridge_open_audits(self):
        root = Path(self._config.audits.root)
        if root.exists():
            os.startfile(str(root))
        else:
            self.lbl_bridge_state.setText(f"✗ Audits root does not exist: {root}")
            self.lbl_bridge_state.setStyleSheet("color: #D9534F; font-weight: bold; font-size: 11px;")

    def _save(self):
        c = self._config
        c.ui.ui_language = self.ui_language.text().strip() or c.ui.ui_language
        c.ui.reply_language = self.reply_language.text().strip() or c.ui.reply_language
        c.packing.output_dir = self.output_dir.text().strip()
        layout_value = self.output_layout.currentData()
        if layout_value not in OUTPUT_LAYOUT_CHOICES:
            layout_value = normalize_output_layout(layout_value)
        c.packing.output_layout = layout_value
        c.packing.delete_old = self.delete_old.isChecked()
        c.packing.include_timestamp = self.include_timestamp.isChecked()
        c.packing.manifest_enabled = self.manifest.isChecked()
        c.audits.root = self.audit_root.text().strip()
        c.audits.hot_seconds = self.hot.value()
        c.audits.warm_seconds = self.warm.value()
        c.bridge.host = self.host.text().strip()
        c.bridge.port = self.port.value()
        c.bridge.autostart = self.autostart.isChecked()
        save_config(c)
        self.saved.emit()
        if callable(self._on_saved):
            self._on_saved()


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("AUDAPACK Settings")
        self.setMinimumSize(300, 240)

        layout = QVBoxLayout(self)
        self.widget = SettingsWidget(config, self, on_saved=self.accept)
        layout.addWidget(self.widget)

        # Attribute aliases for backward-compatibility with existing tests
        self.ui_language = self.widget.ui_language
        self.reply_language = self.widget.reply_language
        self.output_dir = self.widget.output_dir
        self.output_layout = self.widget.output_layout
        self.delete_old = self.widget.delete_old
        self.include_timestamp = self.widget.include_timestamp
        self.manifest = self.widget.manifest
        self.audit_root = self.widget.audit_root
        self.hot = self.widget.hot
        self.warm = self.widget.warm
        self.host = self.widget.host
        self.port = self.widget.port
        self.autostart = self.widget.autostart

    def _save(self):
        self.widget._save()
        self.accept()
