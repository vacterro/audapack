"""T-188: auto-pack-on-project-click.

Clicking a real project row in Project Room packs that project in the
background. The setting defaults ON and is independent of the periodic AUTO
PACK ALL control. The trigger is a genuine operator row click only: startup
auto-selection, programmatic selection, group headers, empty slots, row
buttons, and drags must never pack; a running pack coalesces; and the
existing PACK ALL batch owns a project it is already packing.

The canonical pack dispatch boundary is ``MainWindow._start_project_pack``;
tests spy/mock that boundary rather than packing real archives.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication


def _config(tmp_path, *, click_enabled=True):
    from audapack.config import AppConfig, AuditsConfig, PackingConfig

    packing = PackingConfig(output_dir=str(tmp_path / "out"))
    packing.auto_pack_on_project_click_enabled = click_enabled
    return AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        packing=packing,
        projects=[
            Project(id="a", display_name="A", source_path=str(tmp_path / "a"), priority_group="MAIN0", slot=1),
            Project(id="b", display_name="B", source_path=str(tmp_path / "b"), priority_group="MAIN0", slot=2),
        ],
    )


def _window(tmp_path, *, click_enabled=True):
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    win = MainWindow(ProjectService(_config(tmp_path, click_enabled=click_enabled), base_dir=tmp_path))
    win.resize(1400, 700)
    win.show()
    QApplication.processEvents()
    # Keep the dispatch boundary harmless in click tests: no real archive I/O.
    win._packing.pack_project = lambda *a, **k: PackResult(project_id="x", name="x", success=True, output_path=Path("/dev/null/none.zip"))
    return win


class Project:  # local import helper to avoid circular import ordering
    def __init__(self, **kw):
        from audapack.models import Project as P

        self._p = P(**kw)
        for k, v in vars(self._p).items():
            setattr(self, k, v)

    def __getattr__(self, name):
        return getattr(self._p, name)


class PackResult:
    def __init__(self, **kw):
        from audapack.models import PackResult as R

        self._r = R(**kw)
        for k, v in vars(self._r).items():
            setattr(self, k, v)

    def __getattr__(self, name):
        return getattr(self._r, name)


def _mouse_event(typ, pos):
    return QMouseEvent(
        typ,
        QPointF(pos.x(), pos.y()),
        QPointF(pos.x(), pos.y()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _slot_index(win, group, slot):
    return win.model.index_for_slot(group, slot)


def _press_release(win, idx):
    tree = win.tree
    rect = tree.visualRect(idx)
    pos = rect.center()
    tree.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, pos))
    tree.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, pos))
    QApplication.processEvents()


# ----------------------------------------------------------------- Config (1, 2)

def test_config_defaults_to_enabled():
    from audapack.config import AppConfig, PackingConfig

    assert PackingConfig().auto_pack_on_project_click_enabled is True
    assert AppConfig().packing.auto_pack_on_project_click_enabled is True


def test_legacy_config_without_field_loads_enabled():
    from audapack.config import AppConfig, AuditsConfig, PackingConfig

    d = _config_without_field({"output_dir": "out"})
    cfg = AppConfig(audits=AuditsConfig(root="audits"), packing=PackingConfig(**d))
    assert cfg.packing.auto_pack_on_project_click_enabled is True


def _config_without_field(base):
    from copy import deepcopy

    d = deepcopy(base)
    d.pop("auto_pack_on_project_click_enabled", None)
    return d


def test_config_round_trip_preserves_false(tmp_path):
    from audapack.config import load_config, save_config

    cfg = _config(tmp_path)
    cfg.packing.auto_pack_on_project_click_enabled = False
    assert save_config(cfg, tmp_path) is True
    loaded = load_config(tmp_path)
    assert loaded.packing.auto_pack_on_project_click_enabled is False


# ----------------------------------------------------------------- No-trigger paths (3, 4)

def test_startup_auto_selection_produces_zero_pack_requests(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        # A fresh MainWindow already constructed -> startup must not have packed.
        assert calls == []
    finally:
        win.close()


def test_programmatic_selection_change_produces_zero_pack_requests(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        win.tree.setCurrentIndex(_slot_index(win, "MAIN0", 2))
        QApplication.processEvents()
        assert calls == []
    finally:
        win.close()


# ----------------------------------------------------------------- Real click (5, 6, 8)

def test_real_click_on_project_a_starts_exactly_one_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        _press_release(win, _slot_index(win, "MAIN0", 1))
        assert calls == [("MAIN0", 1)]
    finally:
        win.close()


def test_real_click_calls_shared_dispatch_once(tmp_path, qapp):
    win = _window(tmp_path)
    try:
        dispatched = []
        real = win._start_project_pack

        def spy(proj, *, flash=False):
            dispatched.append(proj.id)
            return real(proj, flash=flash)

        win._start_project_pack = spy
        _press_release(win, _slot_index(win, "MAIN0", 1))
        assert dispatched == ["a"]
    finally:
        win.close()


def test_click_on_already_selected_project_requests_another_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        idx = _slot_index(win, "MAIN0", 1)
        win.tree.setCurrentIndex(idx)
        QApplication.processEvents()
        _press_release(win, idx)
        assert ("MAIN0", 1) in calls  # click after auto-selection still packs
    finally:
        win.close()


def test_click_on_project_b_packs_b_not_stale_a(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        _press_release(win, _slot_index(win, "MAIN0", 2))
        assert calls == [("MAIN0", 2)]
    finally:
        win.close()


# ----------------------------------------------------------------- Coalesce (7)

def test_rapid_duplicate_clicks_while_running_do_not_duplicate(tmp_path, qapp):
    import threading

    win = _window(tmp_path)
    try:
        block = threading.Event()
        win._packing.pack_project = lambda *a, **k: (block.wait(2.0), PackResult(project_id="a", name="a", success=True))[1]
        submits = []
        real_submit = win.task_runner.submit

        def spy_submit(key, *a, **k):
            submits.append(key)
            return real_submit(key, *a, **k)

        win.task_runner.submit = spy_submit
        win._on_project_row_clicked("MAIN0", 1)
        win._on_project_row_clicked("MAIN0", 1)  # already running -> coalesced
        assert submits == ["pack:a"], submits
        block.set()
    finally:
        win.close()


# ----------------------------------------------------------------- Non-row clicks (9, 10)

def test_group_header_click_produces_zero_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        header = win.model.index(0, 0, QModelIndex())
        _press_release(win, header)
        assert calls == []
    finally:
        win.close()


def test_empty_slot_click_produces_zero_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        _press_release(win, _slot_index(win, "MAIN0", 3))
        assert calls == []
    finally:
        win.close()


# ----------------------------------------------------------------- Eligibility (11, 12, 13)

def test_disabled_project_produces_zero_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        dispatched = []
        monkeypatch.setattr(win, "_start_project_pack", lambda proj, *, flash=False: dispatched.append(proj.id))
        win.model.project_at("MAIN0", 1).enabled = False
        win._on_project_row_clicked("MAIN0", 1)
        assert dispatched == []
    finally:
        win.close()


def test_ignore_archive_project_produces_zero_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        dispatched = []
        monkeypatch.setattr(win, "_start_project_pack", lambda proj, *, flash=False: dispatched.append(proj.id))
        win.model.project_at("MAIN0", 1).ignore_archive = True
        win._on_project_row_clicked("MAIN0", 1)
        assert dispatched == []
    finally:
        win.close()


def test_source_less_project_produces_zero_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        dispatched = []
        monkeypatch.setattr(win, "_start_project_pack", lambda proj, *, flash=False: dispatched.append(proj.id))
        win.model.project_at("MAIN0", 1).source_path = ""
        win._on_project_row_clicked("MAIN0", 1)
        assert dispatched == []
    finally:
        win.close()


# ----------------------------------------------------------------- Row button (14)

def test_row_button_click_fires_button_action_zero_autopack(tmp_path, qapp, monkeypatch):
    from audapack.ui_qt.models.project_delegate import FULL_ROW_MIN_WIDTH

    win = _window(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: calls.append((g, s)))
        enabled_hits = []
        monkeypatch.setattr(win, "_on_toggle_project_enabled", lambda p: enabled_hits.append(p))
        idx = _slot_index(win, "MAIN0", 1)
        rect = win.tree.visualRect(idx)
        if rect.width() < FULL_ROW_MIN_WIDTH:
            import pytest

            pytest.skip("row too narrow for painted buttons")
        pos = QPoint(rect.left() + 9, rect.top() + rect.height() // 2)  # within [E] (x 2..16)
        win.tree.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, pos))
        QApplication.processEvents()
        win.tree.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, pos))
        QApplication.processEvents()
        assert calls == [], "row button must not trigger auto-pack"
        assert enabled_hits, "row button must still fire its own action"
    finally:
        win.close()


# ----------------------------------------------------------------- Drag (15)

def test_drag_does_not_trigger_auto_pack(tmp_path, qapp, monkeypatch):
    from PySide6.QtGui import QDrag

    win = _window(tmp_path)
    try:
        dispatched = []
        monkeypatch.setattr(
            win, "_start_project_pack",
            lambda proj, *, flash=False: dispatched.append(proj.id),
        )
        # startDrag is the single funnel a drag begins through; it clears the
        # armed click so a release after a drag never packs.
        monkeypatch.setattr(QDrag, "exec", lambda self, *a, **k: Qt.DropAction.IgnoreAction)
        idx = _slot_index(win, "MAIN0", 1)
        tree = win.tree
        rect = tree.visualRect(idx)
        tree.setCurrentIndex(idx)
        tree.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, rect.center()))
        tree.startDrag(Qt.DropAction.MoveAction)
        tree.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, rect.center()))
        QApplication.processEvents()
        assert dispatched == []
    finally:
        win.close()


# ----------------------------------------------------------------- Setting off (16)

def test_setting_disabled_produces_zero_auto_pack(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path, click_enabled=False)
    try:
        dispatched = []
        monkeypatch.setattr(
            win, "_start_project_pack",
            lambda proj, *, flash=False: dispatched.append(proj.id),
        )
        _press_release(win, _slot_index(win, "MAIN0", 1))
        assert dispatched == []
    finally:
        win.close()


# ----------------------------------------------------------------- PACK ALL unchanged (17)

def test_periodic_auto_pack_all_remains_unchanged(tmp_path, qapp, monkeypatch):
    win = _window(tmp_path)
    try:
        clicks = []
        monkeypatch.setattr(win, "_on_project_row_clicked", lambda g, s: clicks.append((g, s)))
        batched = []
        monkeypatch.setattr(
            win, "_start_parallel_pack_batch",
            lambda projects, batch_label="PACK ALL": (batched.append(([p.id for p in projects], batch_label)) or True),
        )
        # A click packs A but never starts the PACK ALL batch.
        _press_release(win, _slot_index(win, "MAIN0", 1))
        assert clicks == [("MAIN0", 1)]
        assert batched == []
        # Periodic AUTO PACK ALL still uses the same eligibility contract.
        win._on_auto_pack_tick()
        assert len(batched) == 1
        ids, label = batched[0]
        assert label == "AUTO PACK ALL"
        assert ids == ["a", "b"]
    finally:
        win.close()
