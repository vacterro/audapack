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
    TOOLBAR_BUTTON_KEYS,
    LauncherConfig,
    normalize_output_layout,
    save_config,
)
from audapack.fidelity import PROFILES, normalize_fidelity_profile
from audapack.services.bridge_service import BridgeService
from audapack.ui_qt.dialogs.launcher_dialog import LauncherEditDialog
from audapack.ui_qt.even_layout import EvenTabBar
from audapack.window_layout import LAYOUT_CASCADE, LAYOUT_GRID, LAYOUT_SLOTS, list_monitors


def short_worker_label(worker: dict) -> str:
    """Name a worker window in the width a 640px dialog actually has.

    A managed window is worth naming by its slot -- that is what the operator
    presses WRK to reopen. An operator's own tab has nothing but a UUID, and
    the whole 36 characters wrapped to a second line for no information at all.
    """
    slot = int(worker.get("managed_slot") or 0)
    if slot:
        return f"slot {slot}"
    return str(worker.get("worker_id", "?"))[:8]


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
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.sub_tabs = QTabWidget(self)
        self.sub_tabs.setTabBar(EvenTabBar(self.sub_tabs))
        self.general_widget = self._build_general()
        self.packing_widget = self._build_packing()
        self.audit_widget = self._build_audit()
        self.bridge_widget = self._build_bridge()
        self.launchers_widget = self._build_launchers()
        # Bridge is the tab an operator opens Settings for: it carries the
        # health, worker pool and autostart controls that a stuck START AUDIT
        # sends them here to check. It leads, and it is selected on open.
        self.sub_tabs.addTab(self.bridge_widget, "Bridge")
        self.sub_tabs.addTab(self.general_widget, "General")
        self.sub_tabs.addTab(self.packing_widget, "Packing")
        self.sub_tabs.addTab(self.audit_widget, "Audit")
        self.sub_tabs.addTab(self.launchers_widget, "Launchers")
        self.sub_tabs.setCurrentWidget(self.bridge_widget)
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
        # What the widgets held when this tab was built. A save writes only
        # what MOVED since then, so a field nobody touched here is left to
        # whatever is on disk -- see _persist_settings.
        self._baseline = {(section, field): value for section, field, value in self._owned_values()}
        #: Fields the operator has moved in THIS dialog. Once moved, a field is
        #: written on every later save even if it is moved back to where it
        #: started -- see _persist_settings.
        self._touched: set[tuple[str, str]] = set()

    def _wire_autosave(self):
        # Text fields -> debounced auto-save
        self.ui_language.textChanged.connect(lambda: self._autosave_timer.start())
        self.reply_language.textChanged.connect(lambda: self._autosave_timer.start())
        self.gg_template.textChanged.connect(lambda: self._autosave_timer.start())
        self.output_dir.textChanged.connect(lambda: self._autosave_timer.start())
        self.audit_root.textChanged.connect(lambda: self._autosave_timer.start())
        self.mirror_into_project.toggled.connect(lambda: self._autosave_timer.start())
        self.mirror_dir_name.textChanged.connect(lambda: self._autosave_timer.start())
        self.mirror_include_waves.toggled.connect(lambda: self._autosave_timer.start())
        self.autopack_before_audit.toggled.connect(lambda: self._autosave_timer.start())
        self.dedicated_profile_only.toggled.connect(lambda: self._autosave_timer.start())
        self.host.textChanged.connect(lambda: self._autosave_timer.start())

        # Spinboxes -> debounced auto-save
        self.hot.valueChanged.connect(lambda: self._autosave_timer.start())
        self.warm.valueChanged.connect(lambda: self._autosave_timer.start())
        self.cool.valueChanged.connect(lambda: self._autosave_timer.start())
        self.cold.valueChanged.connect(lambda: self._autosave_timer.start())
        self.port.valueChanged.connect(lambda: self._autosave_timer.start())
        self.history_retention.valueChanged.connect(lambda: self._autosave_timer.start())

        # Checkboxes and Dropdowns -> immediate auto-save
        self.output_layout.currentIndexChanged.connect(lambda: self._save())
        self.delete_old.toggled.connect(lambda: self._save())
        self.include_timestamp.toggled.connect(lambda: self._save())
        self.manifest.toggled.connect(lambda: self._save())
        self.fidelity_profile.currentIndexChanged.connect(lambda: self._save())
        self.fidelity_max_mb.valueChanged.connect(lambda: self._autosave_timer.start())
        self.fidelity_media_samples.valueChanged.connect(lambda: self._autosave_timer.start())
        self.fidelity_media_bytes.valueChanged.connect(lambda: self._autosave_timer.start())
        self.always_include.textChanged.connect(lambda: self._autosave_timer.start())
        self.always_exclude.textChanged.connect(lambda: self._autosave_timer.start())
        self.autostart.toggled.connect(self._on_autostart_toggled)
        self.auto_copy_gg.toggled.connect(lambda: self._save())
        self.show_tooltips.toggled.connect(lambda: self._save())
        self.compact_rows.toggled.connect(lambda: self._save())
        self.arrange_workers.toggled.connect(lambda: self._save())
        self.worker_minimized.toggled.connect(lambda: self._save())
        self.close_idle_workers.toggled.connect(lambda: self._save())
        self.worker_layout.currentIndexChanged.connect(lambda: self._save())
        self.worker_monitor.currentIndexChanged.connect(lambda: self._save())
        for _box in self.toolbar_button_checks.values():
            _box.toggled.connect(lambda: self._save())
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

        self.compact_rows = QCheckBox("Compact project rows (one line)")
        self.compact_rows.setChecked(getattr(self._config.ui, "compact_rows", False))
        f.addRow("Project rows", self.compact_rows)

        # Toolbar buttons, one checkbox each. The row has to fit 640px, so a
        # button nobody presses is width taken from one they do.
        hidden = {
            str(key).strip().upper()
            for key in getattr(self._config.ui, "hidden_toolbar_buttons", ()) or ()
        }
        self.toolbar_button_checks: dict[str, QCheckBox] = {}
        row = QWidget(w)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)
        for index, key in enumerate(TOOLBAR_BUTTON_KEYS):
            box = QCheckBox(key, row)
            box.setChecked(key.upper() not in hidden)
            box.setToolTip(f"Show the {key} button on the toolbar")
            self.toolbar_button_checks[key] = box
            row_layout.addWidget(box)
            # Fourteen checkboxes do not fit one 640px line.
            if index == 6:
                f.addRow("Toolbar buttons", row)
                row = QWidget(w)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(2)
        row_layout.addStretch()
        f.addRow("", row)

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
        # T-147: audit-fidelity profiles. STANDARD is the default; FULL is the
        # only profile that claims a complete snapshot.
        self.fidelity_profile = QComboBox()
        current_profile = normalize_fidelity_profile(getattr(self._config.packing, "fidelity_profile", "standard"))
        for _p in PROFILES:
            self.fidelity_profile.addItem(_p.upper(), userData=_p)
        _idx = self.fidelity_profile.findData(current_profile)
        if _idx >= 0:
            self.fidelity_profile.setCurrentIndex(_idx)
        f.addRow("Fidelity profile", self.fidelity_profile)
        self.fidelity_max_mb = QSpinBox()
        self.fidelity_max_mb.setRange(0, 10000)
        self.fidelity_max_mb.setValue(int(getattr(self._config.packing, "fidelity_max_mb", 0) or 0))
        self.fidelity_max_mb.setSpecialValueText("profile default")
        self.fidelity_max_mb.setToolTip("Soft budget override in MB. 0 = profile default. Never trims source/tests/configs/docs.")
        f.addRow("Max archive MB (0=default)", self.fidelity_max_mb)
        self.fidelity_media_samples = QSpinBox()
        self.fidelity_media_samples.setRange(0, 1000)
        self.fidelity_media_samples.setValue(int(getattr(self._config.packing, "fidelity_media_samples", 0) or 0))
        self.fidelity_media_samples.setSpecialValueText("profile default")
        self.fidelity_media_samples.setToolTip("Representative media files kept per media-heavy directory. 0 = profile default.")
        f.addRow("Media samples per dir (0=default)", self.fidelity_media_samples)
        self.fidelity_media_bytes = QSpinBox()
        self.fidelity_media_bytes.setRange(0, 100000)
        self.fidelity_media_bytes.setValue(int(getattr(self._config.packing, "fidelity_media_bytes", 0) or 0))
        self.fidelity_media_bytes.setSpecialValueText("profile default")
        self.fidelity_media_bytes.setToolTip("Byte cap per media-heavy directory. 0 = profile default.")
        f.addRow("Media bytes per dir (0=default)", self.fidelity_media_bytes)
        self.always_include = QLineEdit(", ".join(getattr(self._config.packing, "always_include", None) or []))
        self.always_include.setPlaceholderText("fixtures/**, Sounds/success.wav")
        f.addRow("Always include", self.always_include)
        self.always_exclude = QLineEdit(", ".join(getattr(self._config.packing, "always_exclude", None) or []))
        self.always_exclude.setPlaceholderText("References/raw/**")
        f.addRow("Always exclude", self.always_exclude)
        return w

    def _build_audit(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(6, 4, 6, 4)
        f.setVerticalSpacing(3)
        self.audit_root = QLineEdit(self._config.audits.root)
        f.addRow("Audit root", self.audit_root)
        self.mirror_into_project = QCheckBox("Also copy each finished audit into the project")
        self.mirror_into_project.setChecked(bool(getattr(self._config.audits, "mirror_into_project", False)))
        self.mirror_into_project.setToolTip(
            "A copy, not a move: the audit root stays the index this app reads.\n"
            "An agent working inside the repo finds the audit without being told where it lives."
        )
        f.addRow("Copy to project", self.mirror_into_project)
        self.mirror_dir_name = QLineEdit(str(getattr(self._config.audits, "mirror_dir_name", "audit")))
        self.mirror_dir_name.setPlaceholderText("audit")
        self.mirror_dir_name.setToolTip(
            "Folder created inside the project source path.\n"
            "Agent inboxes read 'audit' specifically: rename it and `cc` finds nothing."
        )
        f.addRow("Project folder", self.mirror_dir_name)
        self.mirror_include_waves = QCheckBox("Also copy the per-wave files beside it")
        self.mirror_include_waves.setChecked(bool(getattr(self._config.audits, "mirror_include_waves", False)))
        self.mirror_include_waves.setToolTip(
            "The agent inbox reads only 1.md, 2.md ... Everything else is residue:\n"
            "never read, never cleaned up, so a settled inbox still reports dirty."
        )
        f.addRow("Per-wave copies", self.mirror_include_waves)
        self.autopack_before_audit = QCheckBox("Autopack zip before audit")
        self.autopack_before_audit.setChecked(bool(getattr(self._config.audits, "autopack_before_audit", True)))
        self.autopack_before_audit.setToolTip(
            "Always repack, instead of trusting the archive's mtime.\n"
            "A stale zip means auditing code you have already moved past."
        )
        f.addRow("Fresh archive", self.autopack_before_audit)
        self.dedicated_profile_only = QCheckBox("Work only in specialized Chromium instances with widget")
        self.dedicated_profile_only.setChecked(
            bool(getattr(self._config.audits, "dedicated_profile_only", False))
        )
        self.dedicated_profile_only.setToolTip(
            "Off, any Chromium browser holding the widget and the token joins the pool\n"
            "and can claim an audit -- including your own Brave or Chrome tab."
        )
        f.addRow("Worker pool", self.dedicated_profile_only)

        # Where the six worker windows go. Windows opens them wherever it
        # likes -- stacked, half of them on the wrong display -- and they were
        # dragged into place by hand after every restart.
        self.arrange_workers = QCheckBox("Put worker windows on one display when they open")
        self.arrange_workers.setChecked(bool(getattr(self._config.ui, "arrange_worker_windows", True)))
        f.addRow("Window layout", self.arrange_workers)

        self.worker_layout = QComboBox()
        self.worker_layout.addItem("Grid — tiled edge to edge (six become 3x2)", LAYOUT_GRID)
        self.worker_layout.addItem("Cascade — overlapped, each title bar reachable", LAYOUT_CASCADE)
        self.worker_layout.addItem(
            "Slots — fixed 3x2 cells, filled from the bottom-left", LAYOUT_SLOTS
        )
        current_layout = str(getattr(self._config.ui, "worker_window_layout", LAYOUT_GRID))
        self.worker_layout.setCurrentIndex(max(0, self.worker_layout.findData(current_layout)))
        f.addRow("Arrangement", self.worker_layout)

        self.worker_monitor = QComboBox()
        self.worker_monitor.addItem("Primary display", -1)
        for monitor in list_monitors():
            self.worker_monitor.addItem(f"Display {monitor.label}", monitor.index)
        wanted = int(getattr(self._config.ui, "worker_window_monitor", -1))
        # A display unplugged since the setting was saved must not vanish from
        # the list silently: it is added back so the choice is still visible.
        if self.worker_monitor.findData(wanted) < 0:
            self.worker_monitor.addItem(f"Display {wanted + 1} (not connected)", wanted)
        self.worker_monitor.setCurrentIndex(max(0, self.worker_monitor.findData(wanted)))
        f.addRow("Display", self.worker_monitor)

        self.worker_minimized = QCheckBox("Minimize them after arranging")
        self.worker_minimized.setChecked(bool(getattr(self._config.ui, "worker_windows_minimized", True)))
        self.worker_minimized.setToolTip(
            "Safe for a worker: the dedicated profile runs with occlusion detection\n"
            "and every backgrounding throttle off, so a minimized audit keeps running."
        )
        f.addRow("Minimized", self.worker_minimized)

        self.close_idle_workers = QCheckBox("Close them when the queue is empty and nothing is running")
        self.close_idle_workers.setChecked(bool(getattr(self._config.ui, "close_idle_worker_windows", True)))
        self.close_idle_workers.setToolTip(
            "Six idle windows are six windows in the way; the next audit reopens what it needs.\n"
            "A window you opened yourself with NEW is never closed, and neither is one holding a run."
        )
        f.addRow("When idle", self.close_idle_workers)

        self.arrange_now_btn = QPushButton("Arrange worker windows now")
        self.arrange_now_btn.clicked.connect(self._on_arrange_worker_windows)
        f.addRow("", self.arrange_now_btn)
        self.lbl_arrange_state = QLabel("")
        self.lbl_arrange_state.setStyleSheet("color: #9C9371; font-size: 10px;")
        self.lbl_arrange_state.setWordWrap(True)
        f.addRow("", self.lbl_arrange_state)

        self.hot = QSpinBox()
        self.hot.setRange(0, 400 * 24 * 3600)
        self.hot.setValue(self._config.audits.hot_seconds)
        f.addRow("Hot seconds", self.hot)
        self.warm = QSpinBox()
        self.warm.setRange(0, 400 * 24 * 3600)
        self.warm.setValue(self._config.audits.warm_seconds)
        f.addRow("Warm seconds", self.warm)
        self.cool = QSpinBox()
        self.cool.setRange(0, 400 * 24 * 3600)
        self.cool.setValue(self._config.audits.cool_seconds)
        f.addRow("Cool seconds", self.cool)
        self.cold = QSpinBox()
        self.cold.setRange(0, 4000 * 24 * 3600)
        self.cold.setValue(self._config.audits.cold_seconds)
        f.addRow("Cold seconds", self.cold)
        return w

    def _build_bridge(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(4)

        self._bridge_service = BridgeService(self._config)
        self._comp_mgr = ComponentManager(self._config)

        # 1. Config Form Group
        grp_cfg = QGroupBox("Bridge Configuration", w)
        f = QFormLayout(grp_cfg)
        f.setContentsMargins(6, 4, 6, 4)
        f.setVerticalSpacing(3)
        self.host = QLineEdit(self._config.bridge.host)
        f.addRow("Host", self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self._config.bridge.port)
        f.addRow("Port", self.port)
        self.autostart = QCheckBox("Start with Windows")
        self.autostart.setChecked(self._config.bridge.autostart)
        f.addRow("Autostart", self.autostart)
        self.history_retention = QSpinBox()
        self.history_retention.setRange(1, 3650)
        self.history_retention.setSuffix(" days")
        self.history_retention.setValue(getattr(self._config.bridge, "history_retention_days", 30))
        f.addRow("History retention", self.history_retention)
        layout.addWidget(grp_cfg)

        # 2. Live Status Group
        grp_status = QGroupBox("Live Bridge Status", w)
        s_layout = QVBoxLayout(grp_status)
        s_layout.setContentsMargins(6, 4, 6, 4)
        s_layout.setSpacing(3)

        self.lbl_bridge_state = QLabel("CHECKING...", grp_status)
        self.lbl_bridge_state.setStyleSheet("font-weight: bold; font-size: 11px;")
        self.lbl_bridge_state.setWordWrap(True)
        s_layout.addWidget(self.lbl_bridge_state)

        self.lbl_bridge_details = QLabel("", grp_status)
        self.lbl_bridge_details.setWordWrap(True)
        s_layout.addWidget(self.lbl_bridge_details)

        # Token Row
        tok_row = QWidget(grp_status)
        tok_layout = QHBoxLayout(tok_row)
        tok_layout.setContentsMargins(0, 0, 0, 0)
        tok_layout.setSpacing(3)

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
        act_layout.setContentsMargins(0, 2, 0, 0)
        act_layout.setSpacing(3)

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
        hlp_layout.setContentsMargins(0, 2, 0, 0)
        hlp_layout.setSpacing(3)

        btn_install_widget = QPushButton("Install Widget", grp_status)
        btn_install_widget.setToolTip("Install or update AUDAPACK_WIDGET in the isolated AUDAPACK browser profile")
        btn_install_widget.clicked.connect(self._on_bridge_install_widget)

        btn_launch_worker = QPushButton("Launch Chromium", grp_status)
        btn_launch_worker.setToolTip("Open the isolated AUDAPACK browser profile")
        btn_launch_worker.clicked.connect(self._on_launch_browser_worker)

        btn_open_audits = QPushButton("Open Audits", grp_status)
        btn_open_audits.setToolTip("Open the audit output root in Explorer")
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
            # Short labels and a trimmed worker id keep every line inside 640px.
            # Spelled out, one wrapped counter line plus six wrapped 36-char
            # UUIDs pushed the action buttons off the bottom of the dialog.
            worker_line = (
                f"W {browser.get('active_workers', 0)}/{browser.get('max_workers', 6)} · "
                f"free {browser.get('free_workers', 0)} · busy {browser.get('busy_workers', 0)} · "
                f"queue {browser.get('queued_jobs', 0)} · audits {browser.get('active_jobs', 0)} · "
                f"fin {browser.get('finalizing_jobs', 0)} · blocked {browser.get('blocked_jobs', 0)} · "
                f"failed {browser.get('failed_jobs', 0)}"
            )
            workers = browser.get("workers", []) or []
            worker_rows = "\n".join(
                f"{short_worker_label(item)}  {item.get('browser_name', '') or '-'}  "
                f"{item.get('state', '?')}  {item.get('project_name', '') or '-'}"
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

        lbl = QLabel("Agent launchers shown as [N] buttons on each project row. Tick to show, untick to hide. Drag to reorder.", w)
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
        self.launcher_list.itemChanged.connect(self._on_launcher_item_changed)
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

    def _on_arrange_worker_windows(self):
        """Arrange now, whatever the checkbox says -- pressing it IS the intent."""
        # Save first: the arrangement reads the config, not the widgets.
        self._save()
        ok, message = self._comp_mgr.arrange_worker_windows(force=True)
        self.lbl_arrange_state.setText(message)
        self.lbl_arrange_state.setStyleSheet(
            f"color: {'#4A7A20' if ok else '#D9534F'}; font-size: 10px;"
        )

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
        # Repopulating sets check states, and every one of those emits
        # itemChanged. Without the guard the first refresh would write the
        # config back over itself launcher by launcher.
        self._launcher_list_loading = True
        try:
            self.launcher_list.clear()
            for lc in self._config.launchers:
                limit = int(getattr(lc, "max_instances", 0) or 0)
                limit_text = f" · max {limit}" if limit else " · unlimited"
                item = QListWidgetItem(f"[{lc.short_label}] {lc.name}  ({lc.id}){limit_text}")
                item.setData(Qt.ItemDataRole.UserRole, lc.id)
                # A tick is how you turn a launcher off and back on. Remove used
                # to be the only way, and it left no way back to the button.
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if lc.enabled else Qt.CheckState.Unchecked
                )
                self.launcher_list.addItem(item)
        finally:
            self._launcher_list_loading = False

    def _on_launcher_item_changed(self, item):
        """Tick / untick a launcher -- shows or hides its row button."""
        if getattr(self, "_launcher_list_loading", False):
            return
        lid = item.data(Qt.ItemDataRole.UserRole)
        lc = next((launcher for launcher in self._config.launchers if launcher.id == lid), None)
        if lc is None:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        if bool(lc.enabled) == enabled:
            return
        lc.enabled = enabled
        self._save()

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

    def _owned_values(self) -> list[tuple[str, str, object]]:
        """Exactly the (section, field, value) triples this dialog owns.

        Field-level, not section-level. Swapping whole sections looked like a
        narrow merge but was not: every field in a section the dialog happens
        to touch got overwritten from the snapshot taken when the tab was
        BUILT, so any change made to that section from anywhere else -- another
        window, the Bridge, a script -- was silently reverted by the next
        autosave, and autosave fires on every checkbox toggle. Observed live:
        dedicated_profile_only was enabled outside the dialog and came back
        False on its own.
        """
        c = self._config
        owned: list[tuple[str, str, object]] = []
        owned.append(("ui", "ui_language", self.ui_language.text().strip() or c.ui.ui_language))
        owned.append(("ui", "reply_language", self.reply_language.text().strip() or c.ui.reply_language))
        gg_val = self.gg_template.text().strip()
        if gg_val:
            owned.append(("ui", "gg_template", gg_val))
        owned.append(("packing", "output_dir", self.output_dir.text().strip()))
        layout_value = self.output_layout.currentData()
        if layout_value not in OUTPUT_LAYOUT_CHOICES:
            layout_value = normalize_output_layout(layout_value)
        owned.append(("packing", "output_layout", layout_value))
        owned.append(("packing", "delete_old", self.delete_old.isChecked()))
        owned.append(("packing", "include_timestamp", self.include_timestamp.isChecked()))
        owned.append(("packing", "manifest_enabled", self.manifest.isChecked()))
        owned.append(("packing", "fidelity_profile", str(self.fidelity_profile.currentData() or "standard")))
        owned.append(("packing", "fidelity_max_mb", int(self.fidelity_max_mb.value())))
        owned.append(("packing", "fidelity_media_samples", int(self.fidelity_media_samples.value())))
        owned.append(("packing", "fidelity_media_bytes", int(self.fidelity_media_bytes.value())))
        owned.append(("packing", "always_include", [p.strip() for p in self.always_include.text().split(",") if p.strip()]))
        owned.append(("packing", "always_exclude", [p.strip() for p in self.always_exclude.text().split(",") if p.strip()]))
        owned.append(("ui", "hidden_toolbar_buttons", [
            key for key, box in self.toolbar_button_checks.items() if not box.isChecked()
        ]))
        owned.append(("audits", "root", self.audit_root.text().strip()))
        owned.append(("audits", "mirror_into_project", bool(self.mirror_into_project.isChecked())))
        owned.append(("audits", "mirror_dir_name", self.mirror_dir_name.text().strip() or "audit"))
        owned.append(("audits", "mirror_include_waves", bool(self.mirror_include_waves.isChecked())))
        owned.append(("audits", "autopack_before_audit", bool(self.autopack_before_audit.isChecked())))
        owned.append(("audits", "dedicated_profile_only", bool(self.dedicated_profile_only.isChecked())))
        owned.append(("ui", "arrange_worker_windows", bool(self.arrange_workers.isChecked())))
        owned.append(("ui", "worker_window_layout", str(self.worker_layout.currentData() or LAYOUT_GRID)))
        owned.append(("ui", "worker_window_monitor", int(self.worker_monitor.currentData() if self.worker_monitor.currentData() is not None else -1)))
        owned.append(("ui", "worker_windows_minimized", bool(self.worker_minimized.isChecked())))
        owned.append(("ui", "close_idle_worker_windows", bool(self.close_idle_workers.isChecked())))
        owned.append(("audits", "hot_seconds", self.hot.value()))
        owned.append(("audits", "warm_seconds", self.warm.value()))
        owned.append(("audits", "cool_seconds", self.cool.value()))
        owned.append(("audits", "cold_seconds", self.cold.value()))
        owned.append(("bridge", "host", self.host.text().strip()))
        owned.append(("bridge", "port", self.port.value()))
        owned.append(("bridge", "autostart", self.autostart.isChecked()))
        owned.append(("bridge", "history_retention_days", self.history_retention.value()))
        owned.append(("ui", "auto_copy_gg_on_launch", self.auto_copy_gg.isChecked()))
        owned.append(("ui", "show_tooltips", self.show_tooltips.isChecked()))
        owned.append(("ui", "compact_rows", self.compact_rows.isChecked()))
        owned.append(("ui", "tooltip_duration_ms", self.tooltip_duration.value()))
        owned.append(("ui", "flash_duration_ms", self.flash_duration.value()))
        return owned

    def _save(self):
        ok = self._persist_settings(self._owned_values())
        self.lbl_save_status.setText("✓ Settings saved" if ok else "✗ Save FAILED — settings not persisted")
        self.lbl_save_status.setStyleSheet("color: #4A7A20; font-size: 10px;" if ok else "color: #D9534F; font-size: 10px;")
        if ok:
            self.saved.emit()
            if callable(self._on_saved):
                self._on_saved()

    def _persist_settings(self, owned) -> bool:
        """Apply only the fields this dialog owns onto the LATEST config.

        Reload under the registry lock, set each owned field by name, save.
        Nothing else in the file is touched -- not the project registry, and
        not a neighbouring field in a section this dialog happens to write.
        """
        from audapack.config import cross_process_lock, get_registry_lock_path, load_config

        base = getattr(self, "_base_dir", None)
        lock_path = get_registry_lock_path(base)
        try:
            with cross_process_lock(lock_path):
                try:
                    latest = load_config(base)
                except Exception as exc:
                    # CORE-002 (audit/1.md): this used to fall back to
                    # `self._config` -- the snapshot this tab was built with --
                    # and save it whole, INCLUDING its project registry, on the
                    # exact path where the latest state could not be read. Not
                    # knowing what is on disk is the one moment a merge must not
                    # happen: fail closed, write nothing.
                    self.lbl_save_status.setText(f"✗ Save FAILED: cannot read current settings ({exc})")
                    self.lbl_save_status.setStyleSheet("color: #D9534F; font-size: 10px;")
                    return False
                for section, field, value in owned:
                    # Only what the operator actually changed HERE. A field the
                    # dialog renders but nobody touched is not evidence of
                    # intent, and writing it back is how an enabled setting
                    # silently reverted itself: the checkbox still held the
                    # value from when the tab was built, and every autosave --
                    # one per toggle, anywhere in the dialog -- rewrote it.
                    #
                    # "Differs from the baseline" is only the same thing as
                    # "touched" until the first change. Tick a box and untick
                    # it and the value matches the baseline again -- so the
                    # write was skipped, the ticked value stayed on disk, and
                    # the dialog sat there showing unticked. A field stays
                    # touched once it has moved, and is written from then on.
                    if value != self._baseline.get((section, field), object()):
                        self._touched.add((section, field))
                    elif (section, field) not in self._touched:
                        continue
                    target = getattr(latest, section, None)
                    if target is not None and hasattr(target, field):
                        setattr(target, field, value)
                latest.launchers = self._config.launchers
                ok = bool(save_config(latest, base))
                if ok:
                    # Keep the in-memory snapshot honest, or the next autosave
                    # would write back what we just merged away.
                    self._config.projects = latest.projects
                    for section in ("ui", "packing", "audits", "bridge"):
                        setattr(self._config, section, getattr(latest, section))
                    # The baseline is deliberately NOT advanced. It records what
                    # this tab was BUILT with, so a field the operator changed
                    # here stays changed and is re-asserted on every later save;
                    # only fields nobody touched are left to disk. Advancing it
                    # made a second save forget the first one's choice.
                return ok
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
