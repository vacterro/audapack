"""T-32: every GUI setting must be consumed and must survive a save/load round trip."""

from __future__ import annotations

from audapack.config import AppConfig, load_config, save_config
from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget


def widget(tmp_path, config=None):
    cfg = config or AppConfig()
    w = SettingsWidget(cfg)
    w._base_dir = tmp_path
    return w, cfg


def test_every_exposed_setting_round_trips_through_disk(tmp_path, qapp):
    w, _cfg = widget(tmp_path)
    w.include_timestamp.setChecked(False)
    w.show_tooltips.setChecked(False)
    w.compact_rows.setChecked(True)
    w.tooltip_duration.setValue(4500)
    w.flash_duration.setValue(1200)
    w.cool.setValue(11111)
    w.cold.setValue(222222)
    w.history_retention.setValue(7)
    w._save()

    loaded = load_config(tmp_path)
    assert loaded.packing.include_timestamp is False
    assert loaded.ui.show_tooltips is False
    assert loaded.ui.compact_rows is True
    assert loaded.ui.tooltip_duration_ms == 4500
    assert loaded.ui.flash_duration_ms == 1200
    assert loaded.audits.cool_seconds == 11111
    assert loaded.audits.cold_seconds == 222222
    assert loaded.bridge.history_retention_days == 7


def test_temperature_and_retention_widgets_load_persisted_values(tmp_path, qapp):
    cfg = AppConfig()
    cfg.audits.cool_seconds = 98765
    cfg.audits.cold_seconds = 876543
    cfg.bridge.history_retention_days = 90
    assert save_config(cfg, tmp_path)

    w, _cfg = widget(tmp_path, load_config(tmp_path))
    assert w.cool.value() == 98765
    assert w.cold.value() == 876543
    assert w.history_retention.value() == 90


def test_removed_dead_tooltip_widgets_are_gone(tmp_path, qapp):
    w, _cfg = widget(tmp_path)
    for dead in ("tooltip_delay", "compact_tooltips"):
        assert not hasattr(w, dead), f"dead widget {dead} must not exist"


def test_autostart_toggle_drives_real_scheduled_task(tmp_path, qapp, monkeypatch):
    w, _cfg = widget(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "audapack.components.autostart.install_autostart",
        lambda: (calls.append("install") is None, "installed"),
    )
    monkeypatch.setattr(
        "audapack.components.autostart.remove_autostart",
        lambda: (calls.append("remove") is None, "removed"),
    )
    w._on_autostart_toggled(True)
    w._on_autostart_toggled(False)
    assert calls == ["install", "remove"]


def test_autostart_failure_reverts_checkbox_so_ui_never_lies(tmp_path, qapp, monkeypatch):
    w, _cfg = widget(tmp_path)
    monkeypatch.setattr(
        "audapack.components.autostart.install_autostart",
        lambda: (False, "schtasks denied"),
    )
    w.autostart.blockSignals(True)
    w.autostart.setChecked(True)
    w.autostart.blockSignals(False)
    w._on_autostart_toggled(True)
    assert w.autostart.isChecked() is False
    assert "schtasks denied" in w.lbl_save_status.text()
