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
    """The delegate reserves exactly COMPACT_STATE_WIDTH, no separate literal.

    They used to be two numbers that had to agree; widening one cell clipped
    the last field on every row and nothing said so.
    """
    from audapack.ui_qt.models.project_delegate import COMPACT_STATE_WIDTH

    assert sum(width for _name, width in COMPACT_STATE_CELL_WIDTHS) == COMPACT_STATE_WIDTH


def test_the_archive_cell_shows_age_and_verdict_together():
    """A bare mark cannot tell one stale archive from another."""
    from audapack.ui_qt.models.project_delegate import compact_archive_cell

    assert compact_archive_cell(False, "", "none", None) == "—"
    assert compact_archive_cell(True, "16m", "fresh", False) == "16m ✓"
    assert compact_archive_cell(True, "2d 7h", "old", False) == "2d7h !"
    assert compact_archive_cell(True, "1d", "stale", False) == "1d ·"
    # A source that moved on outranks any freshness verdict: repack first.
    assert compact_archive_cell(True, "1d", "fresh", True) == "1d ▲"


def test_the_archive_cell_survives_packing_owning_the_zip_cell():
    """Packing borrows ZIP and a COMPLETE badge never gives it back.

    That is why archive age got its own cell instead of sharing ZIP: on a
    packed project -- which is most of them -- ZIP reads "[OK]" forever.
    """
    assert "arc" in dict(COMPACT_STATE_CELL_WIDTHS)


def test_no_column_is_reserved_for_the_pack_badge():
    """It was 32px wide, empty on almost every row, and too narrow to read.

    "PACK 42% 3f 1.2MB" was elided to nothing in it. Packing borrows the ZIP
    cell instead -- the archive size it shows is about to be replaced anyway --
    and the width goes to the project name, which was being truncated.
    """
    assert "pack" not in dict(COMPACT_STATE_CELL_WIDTHS)


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


def test_an_unanswered_bridge_poll_says_so():
    """Skipping the write is what let a startup verdict outlive the truth.

    The startup probe writes "Bridge OFFLINE" the moment it cannot reach a
    Bridge that is still booting. Nothing overwrote it, so that verdict stood
    for a whole poll interval next to a Settings tab reading BRIDGE CONNECTED.
    """
    from audapack.ui_qt.main_window import bridge_status_text

    assert bridge_status_text({}) == "BRIDGE ✗ | not answering"
    assert bridge_status_text({"active_workers": 2, "max_workers": 6}).startswith("BRIDGE ✓")
