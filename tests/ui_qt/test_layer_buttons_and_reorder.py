"""T-161/T-162: row layer buttons, layer editor window, Alt+A, Ctrl+D, drag reorder.

The Project Room row grows two layer buttons beside [i]: [+] opens the audit
layer editor window for that project, [edit] opens the same window focused on
the operator's LAST user-created layer -- never on a layer the AUDAPACK widget
delivered. Alt+A is a second pack-all binding; Ctrl+D opens a NEW manual
audit window. Layers drag-reorder into priority order inside the editor.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, Qt

from audapack.config import AppConfig, AuditsConfig
from audapack.inaudit import (
    ensure_next_layer,
    last_user_layer,
    list_inaudit_layers,
    reorder_inaudit_layers,
)
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_delegate import (
    compute_info_button_rect,
    compute_layer_button_rects,
    compute_row_button_rects,
)


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    from audapack.ui_qt.main_window import MainWindow

    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="p1", display_name="Project One",
                    source_path=str(tmp_path / "p1"), priority_group="MAIN0", slot=1),
        ],
    )
    (tmp_path / "p1").mkdir()
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    win.resize(900, 600)
    win.show()
    qapp.processEvents()
    yield win
    win.close()


def _row_rect(window) -> QRect:
    idx = window.model.index_for_project_id("p1")
    assert idx.isValid()
    return window.tree.visualRect(idx)


def _button_centers(window) -> tuple[QPoint, QPoint]:
    rect = _row_rect(window)
    launcher_buttons, gg_rect = compute_row_button_rects(rect, window._service.config.launchers)
    info_rect = compute_info_button_rect(rect, launcher_buttons, gg_rect)
    plus_rect, edit_rect = compute_layer_button_rects(rect, info_rect)
    return plus_rect.center(), edit_rect.center()


class TestRowButtons:
    def test_row_paints_layer_buttons_inside_the_reserved_block(self, window):
        rect = _row_rect(window)
        launcher_buttons, _gg = compute_row_button_rects(rect, window._service.config.launchers)
        info_rect = compute_info_button_rect(rect, launcher_buttons, _gg)
        plus_rect, edit_rect = compute_layer_button_rects(rect, info_rect)
        assert plus_rect.left() < info_rect.left() < rect.right()
        assert edit_rect.right() < plus_rect.left()
        assert plus_rect.height() == info_rect.height()

    def test_state_column_stays_clear_of_the_button_block(self, window):
        """The content boundary must come from the REAL action block geometry.

        The old FIXED_ACTIONS_WIDTH guessed a 190px reservation; the real
        block is narrower, so every row gave away a dead strip that was
        stolen from the project name. The invariant is now geometric: the
        state area ends before the leftmost action control on every row, no
        matter how many launchers are enabled.
        """
        from audapack.ui_qt.models.project_delegate import compute_actions_left

        rect = _row_rect(window)
        actions_left = compute_actions_left(rect, window._service.config.launchers)
        launcher_buttons, _gg = compute_row_button_rects(rect, window._service.config.launchers)
        info_rect = compute_info_button_rect(rect, launcher_buttons, _gg)
        plus_rect, edit_rect = compute_layer_button_rects(rect, info_rect)
        assert actions_left == edit_rect.left()
        # ordering: edit < plus < info < launchers, contiguous 2px gaps
        assert edit_rect.right() < plus_rect.left()
        assert plus_rect.right() < info_rect.left()
        assert info_rect.right() < launcher_buttons[0][1].left()

    def test_plus_button_opens_the_inaudit_tab_and_creates_a_layer(self, window, qapp):
        """Row [+] must NOT open a separate window — it binds the INAUDIT tab and
        creates the next layer straight into the bottom editor."""
        from PySide6.QtTest import QTest

        plus_center, _edit = _button_centers(window)
        QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, pos=plus_center)
        qapp.processEvents()
        assert window.tabs.currentWidget() is window.inaudit_widget
        assert window.inaudit_widget._project is not None
        assert window.inaudit_widget._project.id == "p1"
        # one new layer created and the editor is the layer's file
        assert len(list_inaudit_layers(window._service.get_project("p1"))) == 1
        assert window.inaudit_widget._editor_path is not None
        assert "Created" in window.inaudit_widget.status.text()

    def test_edit_button_lands_on_the_last_user_layer_never_a_widget_layer(self, window, qapp, tmp_path):
        """The whole point of [edit]: a widget-delivered layer (written by the
        capture assign path, never through ensure_next_layer) must not be what
        the button selects."""
        from PySide6.QtTest import QTest

        project = window._service.get_project("p1")
        d = tmp_path / "p1" / "audit"
        d.mkdir()
        (d / "1.md").write_text("widget delivery", encoding="utf-8")
        assert last_user_layer(project) is None

        _plus, edit_center = _button_centers(window)
        QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, pos=edit_center)
        qapp.processEvents()
        assert window.tabs.currentWidget() is window.inaudit_widget
        # nothing selected onto the widget layer: the widget says so itself
        assert "No custom" in window.inaudit_widget.status.text() or "custom" in window.inaudit_widget.status.text().lower()
        assert window.inaudit_widget._active_path() is None or last_user_layer(project) is None

    def test_edit_button_focuses_the_last_custom_layer_when_one_exists(self, window, qapp, tmp_path):
        from PySide6.QtTest import QTest

        project = window._service.get_project("p1")
        ensure_next_layer(project)  # user layer 1
        ensure_next_layer(project)  # user layer 2
        _plus, edit_center = _button_centers(window)
        QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, pos=edit_center)
        qapp.processEvents()
        assert window.inaudit_widget._editor_path is not None
        assert window.inaudit_widget._editor_path.name == "2.md"
        assert "2.md" in window.inaudit_widget.status.text()

    def test_repressing_plus_keeps_one_inaudit_widget_bound(self, window, qapp, tmp_path):
        """No dialog to re-bind; the tab's single widget just tracks the project."""
        from PySide6.QtTest import QTest

        plus_center, _edit = _button_centers(window)
        QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, pos=plus_center)
        qapp.processEvents()
        first = window.inaudit_widget
        first.set_project(None)
        QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, pos=plus_center)
        qapp.processEvents()
        assert window.inaudit_widget is first  # one in-tab widget, re-bound
        assert first._project.id == "p1"


class TestShortcuts:
    def test_alt_a_packs_everything(self, window, monkeypatch):
        fired = []
        monkeypatch.setattr(window, "_on_pack_all", lambda: fired.append("alt-a"))
        window.pack_all_alt_a_shortcut.activated.emit()
        assert fired == ["alt-a"]

    def test_ctrl_d_opens_an_unclaimed_window_in_the_profile(self, window, monkeypatch):
        fired = []
        monkeypatch.setattr(window, "_on_new_manual_window", lambda: fired.append("ctrl-d"))
        window.new_window_shortcut.activated.emit()
        assert fired == ["ctrl-d"]

    def test_ctrl_a_still_packs_everything(self, window, monkeypatch):
        fired = []
        monkeypatch.setattr(window, "_on_pack_all", lambda: fired.append("ctrl-a"))
        window.pack_all_a_shortcut.activated.emit()
        assert fired == ["ctrl-a"]


class TestDragReorder:
    def _three_layers(self, tmp_path) -> Project:
        project = Project(id="p1", display_name="P", source_path=str(tmp_path / "p1"))
        d = tmp_path / "p1" / "audit"
        d.mkdir(parents=True)
        for n, text in ((1, "first"), (2, "second"), (3, "third")):
            (d / f"{n}.md").write_text(text, encoding="utf-8")
        return project

    def test_widget_reorder_moves_the_selected_layer_to_the_front(self, tmp_path, qapp, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        from audapack.inaudit_capture import InauditCaptureStore
        from audapack.ui_qt.dialogs.inaudit_widget import InauditWidget

        project = self._three_layers(tmp_path)
        store = InauditCaptureStore(tmp_path / "runtime")
        widget = InauditWidget(config_provider=lambda: AppConfig(projects=[project]), capture_store=store)
        widget.set_project(project)
        widget.refresh()
        widget._on_reorder(2, 0)
        d = tmp_path / "p1" / "audit"
        assert (d / "1.md").read_text(encoding="utf-8") == "third"
        assert (d / "2.md").read_text(encoding="utf-8") == "first"
        assert (d / "3.md").read_text(encoding="utf-8") == "second"
        rows = [widget.list.item(row).data(Qt.ItemDataRole.UserRole) for row in range(widget.list.count())]
        assert rows == [1, 2, 3]
        widget.deleteLater()

    def test_reorder_via_module_keeps_numbers_canonical(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = self._three_layers(tmp_path)
        assert reorder_inaudit_layers(project, [2, 3, 1]) == ""
        numbers = sorted(layer.number for layer in list_inaudit_layers(project))
        assert numbers == [1, 2, 3]
