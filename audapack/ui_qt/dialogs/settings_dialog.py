"""Qt settings widget and dialog (Wave L/M parity). No schema redesign."""

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
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
    QListWidget,
    QListWidgetItem,
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
    LauncherConfig,
    normalize_output_layout,
    save_config,
)
from audapack.services.bridge_service import BridgeService
from audapack.ui_qt.dialogs.launcher_dialog import LauncherEditDialog

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
        self.launchers_widget = self._build_launchers()
        self.sub_tabs.addTab(self.general_widget, "General")
        self.sub_tabs.addTab(self.packing_widget, "Packing")
        self.sub_tabs.addTab(self.audit_widget, "Audit")
        self.sub_tabs.addTab(self.bridge_widget, "Bridge")
        self.sub_tabs.addTab(self.launchers_widget, "Launchers")
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
        self.gg_template.textChanged.connect(lambda: self._autosave_timer.start())
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
        self.autostart.toggled.connect(self._on_autostart_toggled)
        self.auto_copy_gg.toggled.connect(lambda: self._save())
        self.show_tooltips.toggled.connect(lambda: self._save())
        self.compact_tooltips.toggled.connect(lambda: self._save())
        self.compact_rows.toggled.connect(lambda: self._save())
        self.tooltip_delay.valueChanged.connect(lambda: self._autosave_timer.start())
        self.flash_duration.valueChanged.connect(lambda: self._autosave_timer.start())

    # ---------------------------------------------------------------- builders

    def _build_general(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.ui_language = QLineEdit(self._config.ui.ui_language)
        f.addRow("UI language", self.ui_language)
        self.reply_language = QLineEdit(self._config.ui.reply_language)
        f.addRow("Reply language", self.reply_language)
        # GG Template: user-configurable clipboard copy template with {path} placeholder
        self.gg_template = QLineEdit(getattr(self._config.ui, "gg_template", "/saipen gg {path}"))
        self.gg_template.setPlaceholderText("Use {path} as placeholder for the audit file path")
        self.gg_template.setMinimumWidth(400)
        f.addRow("GG Template (Ctrl+C)", self.gg_template)
        lbl_hint = QLabel("Copied to clipboard when pressing GG. Use {path} for the audit file path.", w)
        lbl_hint.setStyleSheet("color: #9C9371; font-size: 10px;")
        f.addRow("", lbl_hint)
        # Auto-copy GG on agent launch toggle
        self.auto_copy_gg = QCheckBox("Auto-copy GG command when launching agent")
        self.auto_copy_gg.setChecked(getattr(self._config.ui, "auto_copy_gg_on_launch", True))
        f.addRow("Agent Launch", self.auto_copy_gg)

        # --- UI Behavior ---
        sep1 = QLabel("— UI Behavior —")
        sep1.setStyleSheet("color: #9C9371; font-size: 10px; font-weight: bold; margin-top: 8px;")
        f.addRow("", sep1)

        self.show_tooltips = QCheckBox("Show tooltips on hover")
        self.show_tooltips.setChecked(getattr(self._config.ui, "show_tooltips", True))
        f.addRow("Tooltips", self.show_tooltips)

        self.compact_tooltips = QCheckBox("Compact tooltip mode (less verbose)")
        self.compact_tooltips.setChecked(getattr(self._config.ui, "compact_tooltips", True))
        f.addRow("", self.compact_tooltips)

        self.compact_rows = QCheckBox("Compact project rows (one line)")
        self.compact_rows.setChecked(getattr(self._config.ui, "compact_rows", False))
        f.addRow("Project rows", self.compact_rows)

        self.tooltip_delay = QSpinBox()
        self.tooltip_delay.setRange(0, 3000)
        self.tooltip_delay.setSuffix(" ms")
        self.tooltip_delay.setSingleStep(100)
        self.tooltip_delay.setValue(getattr(self._config.ui, "tooltip_delay_ms", 600))
        f.addRow("Tooltip delay", self.tooltip_delay)
        lbl_hint2 = QLabel("0 = instant, 600 = default. Delay prevents accidental popups.", w)
        lbl_hint2.setStyleSheet("color: #9C9371; font-size: 10px;")
        f.addRow("", lbl_hint2)

        self.tooltip_duration = QSpinBox()
        self.tooltip_duration.setRange(1000, 60000)
        self.tooltip_duration.setSuffix(" ms")
        self.tooltip_duration.setSingleStep(500)
        self.tooltip_duration.setValue(getattr(self._config.ui, "tooltip_duration_ms", 10000))
        f.addRow("Tooltip duration", self.tooltip_duration)
        lbl_hint3 = QLabel("How long the tooltip stays visible. 10000 = 10s default.", w)
        lbl_hint3.setStyleSheet("color: #9C9371; font-size: 10px;")
        f.addRow("", lbl_hint3)

        self.flash_duration = QSpinBox()
        self.flash_duration.setRange(200, 5000)
        self.flash_duration.setSuffix(" ms")
        self.flash_duration.setSingleStep(100)
        self.flash_duration.setValue(getattr(self._config.ui, "flash_duration_ms", 800))
        f.addRow("Flash feedback", self.flash_duration)
        lbl_hint3 = QLabel("Duration of status bar flash after button press.", w)
        lbl_hint3.setStyleSheet("color: #9C9371; font-size: 10px;")
        f.addRow("", lbl_hint3)

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

        btn_install_widget = QPushButton("Install Widget in AUDAPACK Chromium", grp_status)
        btn_install_widget.clicked.connect(self._on_bridge_install_widget)

        btn_launch_worker = QPushButton("Launch AUDAPACK Chromium", grp_status)
        btn_launch_worker.setToolTip(
            "Open the isolated AUDAPACK browser profile with background throttling disabled"
        )
        btn_launch_worker.clicked.connect(self._on_launch_browser_worker)

        btn_open_audits = QPushButton("Open Audits Folder", grp_status)
        btn_open_audits.clicked.connect(self._on_bridge_open_audits)

        hlp_layout.addWidget(btn_install_widget)
        hlp_layout.addWidget(btn_launch_worker)
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
        auto_installed = bool(st.get("autostart", {}).get("installed", False))

        # W2-006: checkbox reflects the actual OS Scheduled Task, not the
        # persisted config intent.
        self.autostart.blockSignals(True)
        self.autostart.setChecked(auto_installed)
        self.autostart.blockSignals(False)

        token = self._comp_mgr.get_bridge_token()
        self.ent_bridge_token.setText(str(token) if token else "")

        if healthy:
            self.lbl_bridge_state.setText(f"✓ BRIDGE CONNECTED (Port {self._config.bridge.port})")
            self.lbl_bridge_state.setStyleSheet("color: #4A7A20; font-weight: bold; font-size: 11px;")
            ver = info.get("version", "?")
            api_ver = info.get("api_version", "?")
            browser = st.get("browser", {}) or {}
            worker_line = (
                f"Workers: {browser.get('active_workers', 0)}/{browser.get('max_workers', 6)} · "
                f"Free: {browser.get('free_workers', 0)} · Busy: {browser.get('busy_workers', 0)} · "
                f"Queue: {browser.get('queued_jobs', 0)} · Active audits: {browser.get('active_jobs', 0)} · "
                f"Finalizing: {browser.get('finalizing_jobs', 0)} · Blocked: {browser.get('blocked_jobs', 0)} · "
                f"Failed: {browser.get('failed_jobs', 0)}"
            )
            workers = browser.get("workers", []) or []
            worker_rows = "\n".join(
                f"{item.get('worker_id', '?')}  {item.get('browser_name', '') or '-'}  {item.get('state', '?')}  {item.get('project_name', '') or '-'}"
                for item in workers[:6]
            )
            self.lbl_bridge_details.setText(
                f"Service: AUDAPACK Bridge {ver} (API v{api_ver}) · Output Root: {self._config.audits.root}\n"
                f"Windows Autostart: {auto}\n{worker_line}\n{worker_rows}"
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

    def _on_autostart_toggled(self, checked: bool):
        """W2-006: the checkbox controls the ACTUAL Windows Scheduled Task.

        On enable install/update the canonical task, on disable remove it, and
        persist the boolean only after a successful OS transition. On failure
        revert the checkbox so displayed intent never contradicts OS state.
        """
        from audapack.components.autostart import install_autostart, remove_autostart

        ok, msg = (install_autostart() if checked else remove_autostart())
        if ok:
            self._save()
            return
        self.autostart.blockSignals(True)
        self.autostart.setChecked(not checked)
        self.autostart.blockSignals(False)
        self.lbl_save_status.setText(f"✗ Autostart task transition failed: {msg}")
        self.lbl_save_status.setStyleSheet("color: #D9534F; font-size: 10px;")

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

    def _on_launch_browser_worker(self):
        ok, msg = self._comp_mgr.launch_browser_worker()
        self.lbl_bridge_state.setText(f"✓ {msg}" if ok else f"✗ {msg}")
        self.lbl_bridge_state.setStyleSheet("color: #4A7A20; font-weight: bold; font-size: 11px;" if ok else "color: #D9534F; font-weight: bold; font-size: 11px;")

    def _on_bridge_open_audits(self):
        root = Path(self._config.audits.root)
        if root.exists():
            os.startfile(str(root))
        else:
            self.lbl_bridge_state.setText(f"✗ Audits root does not exist: {root}")
            self.lbl_bridge_state.setStyleSheet("color: #D9534F; font-weight: bold; font-size: 11px;")

    # ---------------------------------------------------------------- Launchers

    def _build_launchers(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        lbl = QLabel("Agent launchers shown as [N] buttons on each project row. Drag to reorder.", w)
        lbl.setStyleSheet("color: #9C9371; font-size: 10px;")
        layout.addWidget(lbl)

        self.launcher_letters_chk = QCheckBox("Use letters OC / FB / CL / C1 / C2 / CF instead of 1 / 2 / 3 / 4 / 5 / 6", w)
        self.launcher_letters_chk.setChecked(bool(getattr(self._config.ui, "launcher_letters", True)))
        self.launcher_letters_chk.toggled.connect(self._on_launcher_letters_toggled)
        layout.addWidget(self.launcher_letters_chk)

        self.launcher_list = QListWidget(w)
        self.launcher_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.launcher_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.launcher_list.model().rowsMoved.connect(self._on_launcher_rows_moved)
        layout.addWidget(self.launcher_list)

        self._refresh_launcher_list()

        # Button row
        btn_row = QWidget(w)
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(4)

        btn_add = QPushButton("Add", w)
        btn_add.clicked.connect(self._on_add_launcher)
        btn_edit = QPushButton("Edit", w)
        btn_edit.clicked.connect(self._on_edit_launcher)
        btn_remove = QPushButton("Remove", w)
        btn_remove.clicked.connect(self._on_remove_launcher)
        btn_up = QPushButton("▲ Up", w)
        btn_up.clicked.connect(self._on_move_launcher_up)
        btn_down = QPushButton("▼ Down", w)
        btn_down.clicked.connect(self._on_move_launcher_down)

        btn_layout.addWidget(btn_add)
        btn_layout.addWidget(btn_edit)
        btn_layout.addWidget(btn_remove)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_up)
        btn_layout.addWidget(btn_down)

        layout.addWidget(btn_row)
        return w

    def _on_launcher_letters_toggled(self, checked: bool):
        self._config.ui.launcher_letters = bool(checked)
        # Migrate labels to match mode for next save
        letter_map = {"opencode": "OC", "freebuff": "FB", "cline": "CL", "main_codex": "C1", "main_codex2": "C2", "main_codex3_free": "CF"}
        num_map = {"opencode": "1", "freebuff": "2", "cline": "3", "main_codex": "4", "main_codex2": "5", "main_codex3_free": "6"}
        target = letter_map if checked else num_map
        for lc in self._config.launchers:
            if lc.id in target:
                lc.short_label = target[lc.id]
        self._refresh_launcher_list()

    def _refresh_launcher_list(self):
        self.launcher_list.clear()
        for lc in self._config.launchers:
            status = "✓" if lc.enabled else "✗"
            limit = int(getattr(lc, "max_instances", 0) or 0)
            limit_text = f" · max {limit}" if limit else " · unlimited"
            item = QListWidgetItem(f"{status}  [{lc.short_label}] {lc.name}  ({lc.id}){limit_text}")
            item.setData(Qt.ItemDataRole.UserRole, lc.id)
            self.launcher_list.addItem(item)

    def _on_launcher_rows_moved(self, *_args):
        """Sync config.launchers order after InternalMove drag-drop in QListWidget."""
        new_order: list = []
        for i in range(self.launcher_list.count()):
            item = self.launcher_list.item(i)
            lid = item.data(Qt.ItemDataRole.UserRole)
            lc = next((launcher for launcher in self._config.launchers if launcher.id == lid), None)
            if lc:
                new_order.append(lc)
        self._config.launchers = new_order
        self._refresh_launcher_list()
        self._save()

    def _on_add_launcher(self):
        dlg = LauncherEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            new_lc = LauncherConfig(
                id=data["id"],
                name=data["name"],
                short_label=data["short_label"],
                command_template=data["command_template"],
                agent_type=data.get("agent_type", "powershell"),
                enabled=data.get("enabled", True),
                max_instances=data.get("max_instances", 0),
            )
            self._config.launchers.append(new_lc)
            self._refresh_launcher_list()
            self._save()

    def _on_edit_launcher(self):
        item = self.launcher_list.currentItem()
        if not item:
            return
        lid = item.data(Qt.ItemDataRole.UserRole)
        lc = next((launcher for launcher in self._config.launchers if launcher.id == lid), None)
        if not lc:
            return
        dlg = LauncherEditDialog(launcher=lc, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            lc.id = data["id"]
            lc.name = data["name"]
            lc.short_label = data["short_label"]
            lc.command_template = data["command_template"]
            lc.agent_type = data.get("agent_type", "powershell")
            lc.enabled = data.get("enabled", True)
            lc.max_instances = data.get("max_instances", 0)
            self._refresh_launcher_list()
            self._save()

    def _on_remove_launcher(self):
        item = self.launcher_list.currentItem()
        if not item:
            return
        lid = item.data(Qt.ItemDataRole.UserRole)
        self._config.launchers = [launcher for launcher in self._config.launchers if launcher.id != lid]
        self._refresh_launcher_list()
        self._save()

    def _on_move_launcher_up(self):
        row = self.launcher_list.currentRow()
        if row <= 0:
            return
        self._config.launchers[row], self._config.launchers[row - 1] = (
            self._config.launchers[row - 1],
            self._config.launchers[row],
        )
        self._refresh_launcher_list()
        self.launcher_list.setCurrentRow(row - 1)
        self._save()

    def _on_move_launcher_down(self):
        row = self.launcher_list.currentRow()
        if row < 0 or row >= len(self._config.launchers) - 1:
            return
        self._config.launchers[row], self._config.launchers[row + 1] = (
            self._config.launchers[row + 1],
            self._config.launchers[row],
        )
        self._refresh_launcher_list()
        self.launcher_list.setCurrentRow(row + 1)
        self._save()

    def _save(self):
        c = self._config
        c.ui.ui_language = self.ui_language.text().strip() or c.ui.ui_language
        c.ui.reply_language = self.reply_language.text().strip() or c.ui.reply_language
        gg_val = self.gg_template.text().strip()
        if gg_val:
            c.ui.gg_template = gg_val
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
        c.ui.auto_copy_gg_on_launch = self.auto_copy_gg.isChecked()
        c.ui.show_tooltips = self.show_tooltips.isChecked()
        c.ui.compact_tooltips = self.compact_tooltips.isChecked()
        c.ui.compact_rows = self.compact_rows.isChecked()
        c.ui.tooltip_delay_ms = self.tooltip_delay.value()
        c.ui.tooltip_duration_ms = self.tooltip_duration.value()
        c.ui.flash_duration_ms = self.flash_duration.value()
        ok = self._persist_settings(c)
        self.lbl_save_status.setText("✓ Settings saved" if ok else "✗ Save FAILED — settings not persisted")
        self.lbl_save_status.setStyleSheet("color: #4A7A20; font-size: 10px;" if ok else "color: #D9534F; font-size: 10px;")
        if ok:
            self.saved.emit()
            if callable(self._on_saved):
                self._on_saved()

    def _persist_settings(self, c):
        """Transactional rebase of UI-owned fields onto the latest config.

        W2-002: never overwrite a newer project registry mutation with a stale
        whole-snapshot write. Reload the latest config under the registry lock,
        apply ONLY the non-project fields this dialog owns, and save that merged
        snapshot. ProjectRegistry remains the owner of project-list mutations.
        """
        from audapack.config import cross_process_lock, get_registry_lock_path, load_config

        base = getattr(self, "_base_dir", None)
        lock_path = get_registry_lock_path(base)
        try:
            with cross_process_lock(lock_path):
                try:
                    latest = load_config(base)
                except Exception:
                    latest = c
                latest.ui = c.ui
                latest.packing = c.packing
                latest.audits = c.audits
                latest.bridge = c.bridge
                latest.launchers = c.launchers
                self._config.projects = latest.projects
                return bool(save_config(latest, base))
        except Exception as exc:
            self.lbl_save_status.setText(f"✗ Save FAILED: {exc}")
            self.lbl_save_status.setStyleSheet("color: #D9534F; font-size: 10px;")
            return False


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
        self.compact_rows = self.widget.compact_rows

    def _save(self):
        self.widget._save()
        if self.widget.lbl_save_status.text().startswith("✓"):
            self.accept()
