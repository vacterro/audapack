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


def test_the_full_mode_grid_fits_its_declared_width():
    """A cell past FULL_STATE_WIDTH lands on the right-edge button block.

    That is how the INAUDIT badge got painted under the [edit] button, and how
    a ZIP line fitted against the whole column width ran into the PACK badge.
    """
    from audapack.ui_qt.models.project_delegate import FULL_STATE_CELL_WIDTHS, FULL_STATE_WIDTH

    widths = [width for _name, width in FULL_STATE_CELL_WIDTHS]
    assert widths[0] + widths[1] + widths[2] <= FULL_STATE_WIDTH, "line 0 (RUN|WAVES|AGE) must fit"
    assert widths[3] + widths[4] <= FULL_STATE_WIDTH, "line 1 (ZIP|PACK) must fit"


def test_zip_text_picks_the_widest_candidate_that_fits_the_zip_cell():
    """Fitting against the whole column and drawing into the ZIP cell overlapped PACK."""
    from audapack.ui_qt.models.project_delegate import fit_zip_text

    advance = {"a" * 10: 100, "a" * 5: 50, "a" * 2: 20}.get
    candidates = ["a" * 10, "a" * 5, "a" * 2]
    assert fit_zip_text(candidates, advance, 60) == "a" * 5
    assert fit_zip_text(candidates, advance, 10) == "a" * 2
    assert fit_zip_text([], advance, 100) == ""


def test_the_slot_badge_holds_a_two_digit_slot(compact_window):
    """The Add/Edit dialog allows slots 1..10; "[10]" must not spill onto the name.

    The width is derived from the font metrics (NoAntialias/DPI change how wide
    the same point size paints), never a hardcoded pixel count.
    """
    from PySide6.QtGui import QFontMetrics

    from audapack.ui_qt.models.project_delegate import slot_badge_width

    fm = QFontMetrics(compact_window.delegate.font_mono)
    assert fm.horizontalAdvance("[10]") <= slot_badge_width(fm)


def test_a_full_mode_row_paints_without_error(compact_window):
    """Guards the full-mode rewrite (grid constants, no IA in the grid) against a crash."""
    compact_window.delegate._config.ui.compact_rows = False
    index = compact_window.model.index_for_project_id("p1")
    assert index.isValid()

    pixmap = QPixmap(700, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 700, 44)
        compact_window.delegate.paint(painter, option, index)
    finally:
        painter.end()


def test_a_cramped_row_paints_only_the_elided_name(compact_window):
    """Narrow window used to stack the name on top of the state grid.

    The state grid is anchored to the right edge and the name used to keep a
    hard 40px floor, so on a narrow window both painted into the same pixels.
    Below FULL_ROW_MIN_WIDTH the row degrades: checkboxes, slot badge, one
    elided name -- no grid, no buttons.
    """
    from audapack.ui_qt.models.project_delegate import FULL_ROW_MIN_WIDTH

    index = compact_window.model.index_for_project_id("p1")
    assert index.isValid()

    pixmap = QPixmap(300, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 300, 22)
        compact_window.delegate.paint(painter, option, index)
    finally:
        painter.end()

    # Paint and hit-testing must agree on where the button block lives.
    rect = QRect(0, 0, FULL_ROW_MIN_WIDTH - 1, 22)
    assert rect.width() < FULL_ROW_MIN_WIDTH


def test_cramped_threshold_matches_main_window_hit_testing():
    """One threshold: a control may never be clickable where it is not painted."""
    import inspect

    from audapack.ui_qt import main_window

    src = inspect.getsource(main_window.ProjectTreeView.mousePressEvent)
    assert "FULL_ROW_MIN_WIDTH" in src


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
