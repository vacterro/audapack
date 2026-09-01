"""The compact state row is a grid, not a concatenated string.

Every field used to be appended to the previous one, so a short RUN token or a
missing age shifted everything after it. Down a list of projects nothing lined
up and the same value sat in a different place on every row.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QModelIndex, QRect
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStyleOptionViewItem

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_delegate import (
    COMPACT_STATE_CELL_WIDTHS,
    compact_state_columns,
)


def test_cells_are_contiguous_and_never_overlap():
    cells = compact_state_columns(100)
    order = [name for name, _width in COMPACT_STATE_CELL_WIDTHS]
    cursor = 100
    for name in order:
        x, width = cells[name]
        assert x == cursor, f"{name} must start where the previous cell ended"
        assert width > 0
        cursor += width


def test_the_grid_fits_the_compact_state_column_exactly():
    # col_w for compact rows is 230 in the delegate; a grid wider than the
    # column would silently clip the last field on every row.
    assert sum(width for _name, width in COMPACT_STATE_CELL_WIDTHS) == 230


def test_cell_offsets_do_not_depend_on_any_field_value():
    """The whole point: the same field is at the same x on every row."""
    assert compact_state_columns(0)["zip"][0] - 0 == compact_state_columns(500)["zip"][0] - 500


@pytest.fixture
def compact_window(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="p1", display_name="Project One",
                    source_path=str(tmp_path / "p1"), priority_group="MAIN0", slot=1),
        ],
    )
    config.ui.compact_rows = True
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    yield win
    win.close()


def test_a_compact_row_paints_without_error(compact_window):
    """Guards the grid rewrite against a crash in the real paint path."""
    index = compact_window.model.index_for_project_id("p1")
    assert index.isValid()

    pixmap = QPixmap(900, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 900, 22)
        compact_window.delegate.paint(painter, option, index)
    finally:
        painter.end()

    assert compact_window.delegate.sizeHint(QStyleOptionViewItem(), index).height() == 22
    assert isinstance(index, QModelIndex)
