"""T-290 (UI): ``Complete source archive`` is a faithful, project-local toggle.

The four T-290 defects are about membership policy; this file is about the one
control an operator actually uses. A checkable action that lies about its own
state, or whose toggle leaks into a neighbouring project, is the same class of
bug in the UI: the label says one thing and the registry holds another.

Pinned here, through the REAL handler:

* the label is exactly ``Complete source archive``;
* the action is checkable, and its checked state agrees with the persisted
  ``archive_scope`` the project was LOADED with, for both scopes;
* triggering persists ``complete_source`` and then ``auto`` through the
  registry transaction (an in-memory-only toggle would never reach the packer);
* toggling one project never mutates another.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint

from audapack.config import AppConfig, AuditsConfig, PackingConfig, save_config
from audapack.models import ARCHIVE_SCOPE_AUTO, ARCHIVE_SCOPE_COMPLETE_SOURCE, Project
from audapack.services.project_service import ProjectService

LABEL = "Complete source archive"


def _project(project_id: str, slot: int, scope: str = ARCHIVE_SCOPE_AUTO) -> Project:
    return Project(
        id=project_id,
        display_name=project_id.upper(),
        source_path=str(Path(f"V:\\code\\{project_id}")),
        priority_group="MAIN0",
        slot=slot,
        archive_scope=scope,
    )


def _window(tmp_path, projects):
    from audapack.ui_qt.main_window import MainWindow

    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        packing=PackingConfig(output_dir=str(tmp_path / "out")),
        projects=list(projects),
    )
    # The registry mutation path reloads the LATEST config from disk, so a
    # toggle only persists when the project is registered there first.
    assert save_config(config, base_dir=tmp_path), "fixture config could not be saved"
    return MainWindow(ProjectService(config, base_dir=tmp_path))


@pytest.fixture
def captured_menus(monkeypatch):
    """Collect every menu the window would have ``exec``ed, without showing it."""
    import audapack.ui_qt.main_window as main_window

    captured: list = []
    original = main_window.QMenu

    class _CapturedMenu(original):  # type: ignore[misc, valid-type]
        def exec(self, *args, **kwargs):  # noqa: A003 - shadowing QMenu.exec
            captured.append(self)
            return None

    monkeypatch.setattr(main_window, "QMenu", _CapturedMenu)
    return captured


class _Viewport:
    def mapToGlobal(self, pos):  # noqa: N802 - Qt naming
        return pos if pos is not None else QPoint(0, 0)


class _Tree:
    """Just enough of QTreeView for the menu builder, with no pixel geometry."""

    def __init__(self, index):
        self._index = index
        self._viewport = _Viewport()

    def indexAt(self, pos):  # noqa: N802 - Qt naming
        return self._index

    def setCurrentIndex(self, index):  # noqa: N802 - Qt naming
        self._index = index

    def viewport(self):  # noqa: N802 - Qt naming
        return self._viewport


def _menu_for(win, project_id: str, captured: list):
    """Build the project context menu and return the action with ``LABEL``."""
    index = win.model.index_for_project_id(project_id)
    assert index.isValid(), f"no tree row for {project_id}"
    real_tree = win.tree
    win.tree = _Tree(index)
    try:
        win._on_tree_context_menu(QPoint(4, 4))
    finally:
        win.tree = real_tree
    assert captured, "the context menu was never built"
    menu = captured.pop()
    matches = [a for a in menu.actions() if a.text() == LABEL]
    assert len(matches) == 1, (
        f"expected exactly one {LABEL!r} action, got {len(matches)}: "
        f"{[a.text() for a in menu.actions()]}"
    )
    return menu, matches[0]


def test_the_action_label_and_checked_state_mirror_the_persisted_scope(
    tmp_path, qapp, captured_menus
):
    win = _window(tmp_path, [
        _project("pa", 1, ARCHIVE_SCOPE_COMPLETE_SOURCE),
        _project("pb", 2, ARCHIVE_SCOPE_AUTO),
    ])
    try:
        # Both scopes pass through the real load path, so the check mark can
        # only agree with the persisted value -- not with a default.
        assert win._service.get_project("pa").archive_scope == ARCHIVE_SCOPE_COMPLETE_SOURCE
        assert win._service.get_project("pb").archive_scope == ARCHIVE_SCOPE_AUTO

        _, checked = _menu_for(win, "pa", captured_menus)
        assert checked.isCheckable()
        assert checked.isChecked() is True
        assert "larger" in checked.toolTip().lower(), checked.toolTip()

        _, unchecked = _menu_for(win, "pb", captured_menus)
        assert unchecked.isCheckable()
        assert unchecked.isChecked() is False
    finally:
        win.close()


def test_triggering_toggles_one_project_and_never_its_neighbour(
    tmp_path, qapp, captured_menus
):
    win = _window(tmp_path, [_project("pa", 1), _project("pb", 2)])
    try:
        # AUTO -> complete_source, persisted, and the neighbour is untouched.
        _, action = _menu_for(win, "pa", captured_menus)
        assert action.isChecked() is False
        action.trigger()
        pa = win._service.get_project("pa")
        pb = win._service.get_project("pb")
        assert pa is not None and pa.archive_scope == ARCHIVE_SCOPE_COMPLETE_SOURCE
        assert pb is not None and pb.archive_scope == ARCHIVE_SCOPE_AUTO

        # Reopening shows what the toggle actually persisted.
        _, action = _menu_for(win, "pa", captured_menus)
        assert action.isChecked() is True

        # ... and toggling back returns to AUTO rather than getting stuck.
        action.trigger()
        pa = win._service.get_project("pa")
        assert pa is not None and pa.archive_scope == ARCHIVE_SCOPE_AUTO

        # The neighbour never moved, in the registry or on screen.
        pb = win._service.get_project("pb")
        assert pb is not None and pb.archive_scope == ARCHIVE_SCOPE_AUTO
        _, other = _menu_for(win, "pb", captured_menus)
        assert other.isChecked() is False
    finally:
        win.close()
