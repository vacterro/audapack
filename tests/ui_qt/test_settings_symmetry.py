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


def test_settings_autosave_does_not_revert_a_change_made_elsewhere(qapp, tmp_path, monkeypatch):
    """The dialog owns fields, not whole sections.

    Swapping `latest.audits = c.audits` looked like a narrow merge and was not:
    every field in that section came from the snapshot taken when the tab was
    BUILT, so anything changed from outside -- another window, a script, the
    Bridge -- was reverted by the next autosave, and autosave fires on every
    checkbox toggle. Observed live: dedicated_profile_only was enabled outside
    the dialog and came back False on its own.
    """
    from audapack.config import AppConfig, load_config, save_config
    from audapack.models import Project
    from audapack.ui_qt.dialogs.settings_dialog import SettingsWidget

    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
    base = AppConfig()
    # save_config refuses to truncate a project list, so give it one to keep.
    base.projects = [Project(id="p1", display_name="P1", source_path=str(tmp_path / "p1"))]
    base.audits.dedicated_profile_only = False
    base.audits.hot_seconds = 111
    save_config(base)

    widget = SettingsWidget(load_config())

    # Something else changes the same SECTION while the tab is open.
    outside = load_config()
    outside.audits.dedicated_profile_only = True
    save_config(outside)

    # An unrelated toggle fires autosave.
    widget.compact_rows.setChecked(not widget.compact_rows.isChecked())
    widget._save()

    reloaded = load_config()
    assert reloaded.audits.dedicated_profile_only is True, "an outside change must survive autosave"
    assert reloaded.audits.hot_seconds == 111, "and the dialog's own fields still persist"


def test_a_launcher_is_turned_off_and_back_on_with_its_tick(tmp_path, qapp):
    """Remove used to be the only way off, and it left no way back."""
    from PySide6.QtCore import Qt

    w, cfg = widget(tmp_path)
    item = w.launcher_list.item(1)
    launcher_id = item.data(Qt.ItemDataRole.UserRole)
    assert item.checkState() == Qt.CheckState.Checked
    assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable

    def saved_state() -> bool:
        loaded = load_config(tmp_path)
        return next(lc.enabled for lc in loaded.launchers if lc.id == launcher_id)

    item.setCheckState(Qt.CheckState.Unchecked)
    assert saved_state() is False

    item.setCheckState(Qt.CheckState.Checked)
    assert saved_state() is True


def test_building_the_launcher_tab_does_not_write_anything(tmp_path, qapp):
    """Every setCheckState emits itemChanged; the load must not save back."""
    saves = []
    w, _cfg = widget(tmp_path)
    w._save = lambda: saves.append(1)
    w._refresh_launcher_list()
    assert saves == []


def test_a_disabled_launcher_has_no_row_button(tmp_path, qapp):
    from PySide6.QtCore import QRect

    from audapack.ui_qt.models.project_delegate import compute_row_button_rects

    _w, cfg = widget(tmp_path)
    row = QRect(0, 0, 640, 22)
    before, _ = compute_row_button_rects(row, cfg.launchers)
    cfg.launchers[0].enabled = False
    after, _ = compute_row_button_rects(row, cfg.launchers)
    assert len(after) == len(before) - 1
    assert all(lc.id != cfg.launchers[0].id for lc, _rect in after)


def test_all_three_worker_layouts_are_offered_and_round_trip(tmp_path, qapp):
    from audapack.window_layout import LAYOUT_SLOTS, LAYOUTS

    w, _cfg = widget(tmp_path)
    offered = {w.worker_layout.itemData(i) for i in range(w.worker_layout.count())}
    assert offered == set(LAYOUTS)

    w.worker_layout.setCurrentIndex(w.worker_layout.findData(LAYOUT_SLOTS))
    assert load_config(tmp_path).ui.worker_window_layout == LAYOUT_SLOTS


def test_closing_idle_worker_windows_is_on_by_default_and_round_trips(tmp_path, qapp):
    w, _cfg = widget(tmp_path)
    assert w.close_idle_workers.isChecked()
    w.close_idle_workers.setChecked(False)
    assert load_config(tmp_path).ui.close_idle_worker_windows is False
    w.close_idle_workers.setChecked(True)
    assert load_config(tmp_path).ui.close_idle_worker_windows is True


def test_a_setting_toggled_back_to_where_it_started_still_saves(tmp_path, qapp):
    """Tick a box, untick it, and the ticked value stayed on disk.

    The merge wrote only fields differing from the construction baseline, and
    the baseline is frozen on purpose. But "differs from the baseline" is only
    the same thing as "touched" until the first change: moving a field back
    matched the baseline again, the write was skipped, and the dialog sat there
    showing one value while disk held the other. Affects every field here.
    """
    w, _cfg = widget(tmp_path)
    started = w.compact_rows.isChecked()

    w.compact_rows.setChecked(not started)
    assert load_config(tmp_path).ui.compact_rows is (not started)

    w.compact_rows.setChecked(started)
    assert load_config(tmp_path).ui.compact_rows is started, "the revert was dropped"

    w.compact_rows.setChecked(not started)
    assert load_config(tmp_path).ui.compact_rows is (not started)


def test_a_field_nobody_touched_is_still_left_to_disk(tmp_path, qapp):
    """The property the frozen baseline exists to protect, kept intact."""
    from audapack.config import AppConfig, save_config

    on_disk = AppConfig()
    on_disk.ui.show_tooltips = False
    save_config(on_disk, tmp_path)

    w, _cfg = widget(tmp_path)          # built from a default config: True
    assert w.show_tooltips.isChecked()  # so the widget disagrees with disk
    w.compact_rows.setChecked(not w.compact_rows.isChecked())  # save something else
    assert load_config(tmp_path).ui.show_tooltips is False, "an untouched field was overwritten"


def test_an_unreadable_latest_config_fails_closed_and_writes_nothing(tmp_path, qapp, monkeypatch):
    """CORE-002 (audit/1.md): not knowing the current state is not permission to guess.

    The fallback was `latest = self._config` -- the snapshot this tab was built
    with, project registry included -- saved whole on the one path where the
    newest state could not be read. A settings autosave could therefore roll the
    project list back precisely when it had least idea what was on disk.
    """
    from audapack.models import Project

    cfg = AppConfig()
    cfg.projects = [Project(id="keep", display_name="Keep", source_path=str(tmp_path / "keep"))]
    assert save_config(cfg, tmp_path)
    before = (tmp_path / "config.json").read_bytes()

    w, _cfg = widget(tmp_path, AppConfig())
    monkeypatch.setattr(
        "audapack.config.load_config",
        lambda *a, **k: (_ for _ in ()).throw(OSError("simulated unreadable config")),
    )
    saves = []
    monkeypatch.setattr(
        "audapack.ui_qt.dialogs.settings_dialog.save_config",
        lambda *a, **k: saves.append(a) or True,
    )

    assert w._persist_settings([("ui", "compact_rows", True)]) is False
    assert saves == [], "a save was attempted with no knowledge of the current state"
    assert (tmp_path / "config.json").read_bytes() == before
    assert "FAILED" in w.lbl_save_status.text()
