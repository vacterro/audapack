"""Qt model tests — ProjectRoomModel hierarchy, no filesystem access inside data()."""

import sys

from PySide6.QtCore import Qt

# Minimal QApplication: one per session, reuse across model tests.
from PySide6.QtWidgets import QApplication

_app = None


def _app():
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication(sys.argv)
    return _app


def _model_with(projects: list, groups: list[str] | None = None):
    from unittest.mock import MagicMock
    svc = MagicMock()
    svc.active_groups.return_value = groups or ["MAIN0", "MAIN1", "SIDE1"]
    svc.list_projects.return_value = projects
    svc.config.ui.window_size = [820, 600]
    from audapack.ui_qt.models.project_room_model import ProjectRoomModel
    model = ProjectRoomModel(svc)
    model._reload()
    return model, svc


def test_group_row_count():
    m, _ = _model_with([])
    assert m.rowCount() == 3  # MAIN0, MAIN1, SIDE1


def test_slot_row_count_per_group():
    m, _ = _model_with([])
    idx = m.index(0, 0)
    assert idx.isValid()
    assert m.rowCount(idx) == 6  # SLOTS_PER_GROUP


def test_slot_data_empty_vs_occupied():
    from audapack.models import Project
    p = Project(id="fp", display_name="FastPrompter", source_path="", priority_group="MAIN0", slot=1)
    m, _ = _model_with([p])
    main0 = m.index(0, 0)
    slot1 = m.index(0, 0, main0)
    assert m.data(slot1, Qt.ItemDataRole.DisplayRole) == "FastPrompter"
    slot2 = m.index(1, 0, main0)
    assert m.data(slot2, Qt.ItemDataRole.DisplayRole) == "Slot 2"


def test_project_id_role():
    from audapack.models import Project
    p = Project(id="fp", display_name="FastPrompter", source_path="", priority_group="MAIN0", slot=1)
    m, _ = _model_with([p])
    main0 = m.index(0, 0)
    slot1 = m.index(0, 0, main0)
    role = m.ROLES["project_id"]
    assert m.data(slot1, role) == "fp"


def test_is_empty_slot_role():
    from audapack.models import Project
    p = Project(id="fp", display_name="FastPrompter", source_path="", priority_group="MAIN0", slot=1)
    m, _ = _model_with([p])
    main0 = m.index(0, 0)
    empty_role = m.ROLES["is_empty_slot"]
    assert m.data(m.index(0, 0, main0), empty_role) is False
    assert m.data(m.index(1, 0, main0), empty_role) is True


def test_parent_index():
    m, _ = _model_with([])
    idx = m.index(0, 0)
    parent = m.parent(idx)
    assert not parent.isValid()  # group is root child
    slot = m.index(0, 0, idx)
    slot_parent = m.parent(slot)
    assert slot_parent.isValid()
    assert slot_parent == idx


def test_no_filesystem_access_in_data():
    import os

    from audapack.models import Project
    p = Project(id="fp", display_name="FastPrompter", source_path="", priority_group="MAIN0", slot=1)
    m, svc = _model_with([p])
    # Ensure data() never touches filesystem: mock would fail if called
    orig_access = os.access
    import builtins
    real_open = builtins.open
    try:
        builtins.open = None  # type:ignore; will crash if data() tries to open a file
        os.access = None  # type:ignore
        main0 = m.index(0, 0)
        slot1 = m.index(0, 0, main0)
        _ = m.data(slot1, Qt.ItemDataRole.DisplayRole)
        _ = m.data(slot1, m.ROLES["project_id"])
        _ = m.data(slot1, m.ROLES["is_empty_slot"])
        _ = m.data(slot1, m.ROLES["enabled"])
        _ = m.group_count("MAIN0")
        _ = m.rowCount()
        _ = m.columnCount()
    finally:
        builtins.open = real_open
        os.access = orig_access


def test_import_ui_qt_does_not_import_tkinter():
    import sys as _sys
    # Ensure tkinter is not pulled through ui_qt
    _sys.modules.pop("tkinter", None)
    from audapack.ui_qt import app  # noqa: F401
    assert "tkinter" not in _sys.modules
