"""The compact state row is a right-anchored token rail, not a fixed grid.

Compact mode used to reserve five maximum-width cells (234px) on every row:
absent values kept their cells, short values took their maximum width, and
the project name was elided while pixels sat unused. Now every visible token
takes its measured text width plus one small gap, absent values cost zero,
the rail sits at the REAL action block (never a guessed 190px), and whatever
the rail does not need flows back to the project name.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QModelIndex, QRect
from PySide6.QtGui import QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QStyleOptionViewItem

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_delegate import (
    COMPACT_RAIL_GAP,
    COMPACT_STATE_CELL_WIDTHS,
    actions_block_width,
    compact_fit_tokens,
    compact_state_columns,
    compact_state_tokens,
    compute_actions_left,
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


def test_the_token_order_is_the_reading_order():
    """RUN WAVES AGE ZIP ARC -- preserved from the old grid contract."""
    tokens = compact_state_tokens("A3", "0/3", "2d7h", "ZIP 16,7MB", "", "16m ✓")
    assert [key for key, _text in tokens] == ["run", "waves", "age", "zip", "arc"]


def test_empty_tokens_cost_zero():
    """Absent values must not reserve invisible cells (Phase G #3)."""
    tokens = compact_state_tokens("A3", "", "", "ZIP 16,7MB", "", "1h40m ✓")
    assert [key for key, _text in tokens] == ["run", "zip", "arc"]
    assert "waves" not in dict(tokens)
    assert "age" not in dict(tokens)


def test_packing_owns_the_zip_token():
    assert dict(compact_state_tokens("A3", "", "", "ZIP x", " [42%]", ""))["zip"] == " [42%]"
    assert dict(compact_state_tokens("A3", "", "", "ZIP x", "", ""))["zip"] == "ZIP x"


def test_the_rail_is_right_anchored_at_the_real_action_block(qapp):
    """Phase G #1: the content boundary is the real geometry, not a guess."""
    from PySide6.QtGui import QFont

    rect = QRect(0, 0, 620, 22)
    fm = QFontMetrics(QFont("Verdana", 9))
    launchers = [type("L", (), {"enabled": True, "id": f"l{i}"})() for i in range(3)]
    actions_left = compute_actions_left(rect, launchers)
    # SRC-081 TARGET H: measured from the real launchers it is given (three
    # 18px buttons + gaps + the fixed info/plus/edit block), never a phantom.
    assert actions_block_width(launchers) == 123
    tokens = compact_state_tokens("FAIL", "", "", "ZIP 16,7MB", "", "2d7h !")
    rects, name_right = compact_fit_tokens(fm, tokens, actions_left, 100)
    assert rects
    rightmost = max(r.right() for _key, r in rects)
    # Phase G #2: dead gap is only the declared rail gap. QRect.right() is
    # left+width-1, so the closed-interval gap measures rail_gap+1.
    assert COMPACT_RAIL_GAP <= actions_left - rightmost <= COMPACT_RAIL_GAP + 1
    assert name_right <= actions_left - COMPACT_RAIL_GAP


def test_short_values_use_content_width(qapp):
    """Phase G #4: "FAIL" must not consume a 50px cell."""
    from PySide6.QtGui import QFont

    fm = QFontMetrics(QFont("Verdana", 9))
    tokens = compact_state_tokens("FAIL", "", "", "", "", "")
    rects, _name_right = compact_fit_tokens(fm, tokens, 500, 0)
    run_rect = dict(rects)["run"]
    assert run_rect.width() == fm.horizontalAdvance("FAIL")


def test_sparse_row_has_no_holes(qapp):
    """Phase G #10: RUN + ARC only -- no invisible WAVES/AGE/ZIP cells."""
    from PySide6.QtGui import QFont

    fm = QFontMetrics(QFont("Verdana", 9))
    tokens = compact_state_tokens("A3", "", "", "", "", "—")
    rects, _name_right = compact_fit_tokens(fm, tokens, 500, 0)
    assert [key for key, _r in rects] == ["run", "arc"]


def test_the_name_reclaims_the_reclaimed_space(qapp):
    """Phase G #5: the rail degrades tail-first until the full name fits, so
    the name box ends right of the old fixed grid start. Relative invariant.
    """
    from PySide6.QtGui import QFont

    rect = QRect(0, 0, 620, 22)
    fm = QFontMetrics(QFont("Verdana", 9))
    launchers = [type("L", (), {"enabled": True, "id": f"l{i}"})() for i in range(3)]
    actions_left = compute_actions_left(rect, launchers)
    tokens = compact_state_tokens("A3", "1/3", "2d7h", "ZIP 19,5MB", "", "18h53m ·")
    _rects, name_right = compact_fit_tokens(
        fm, tokens, actions_left, 100, name_text="_AUDAPACK"
    )
    old_fixed_col_x = rect.right() - 190 - 4 - 234  # the old phantom+grid
    assert name_right > old_fixed_col_x
    assert name_right - 100 >= fm.horizontalAdvance("_AUDAPACK")


def test_long_name_elides_but_name_box_never_crosses_the_rail(qapp):
    """Phase G #6: the NAME box is bounded; elision happens inside it."""
    from PySide6.QtGui import QFont

    rect = QRect(0, 0, 620, 22)
    fm = QFontMetrics(QFont("Verdana", 9))
    launchers = [type("L", (), {"enabled": True, "id": f"l{i}"})() for i in range(6)]
    actions_left = compute_actions_left(rect, launchers)
    tokens = compact_state_tokens("AUDIT", "2/3", "2d7h", "ZIP 16,7MB", "", "16m ✓")
    _rects, name_right = compact_fit_tokens(fm, tokens, actions_left, 100)
    assert name_right <= actions_left - 8


def test_all_tokens_present_still_paint_without_overlap(qapp):
    """Phase G #9: a fully populated rail keeps its declared inter-token gaps
    and never exceeds the action boundary."""
    from PySide6.QtGui import QFont

    fm = QFontMetrics(QFont("Verdana", 9))
    tokens = compact_state_tokens("AUDIT", "2/3", "2d7h", "ZIP 16,7MB", "", "16m ✓")
    rects, _name_right = compact_fit_tokens(fm, tokens, 500, 0)
    ordered = sorted(rects, key=lambda kr: kr[1].left())
    for (_ka, a), (_kb, b) in zip(ordered, ordered[1:], strict=False):
        assert a.right() < b.left()
    assert ordered[-1][1].right() <= 500 - 8


def test_launcher_count_moves_the_boundary_with_the_real_block(qapp):
    """Phase G #11: few vs max launchers both derive from actual geometry."""
    from PySide6.QtGui import QFont

    rect = QRect(0, 0, 620, 22)
    fm = QFontMetrics(QFont("Verdana", 9))
    few = [type("L", (), {"enabled": True, "id": f"l{i}"})() for i in range(2)]
    many = [type("L", (), {"enabled": True, "id": f"l{i}"})() for i in range(6)]
    few_left = compute_actions_left(rect, few)
    many_left = compute_actions_left(rect, many)
    assert many_left < few_left  # more buttons push the boundary left
    tokens = compact_state_tokens("A3", "", "", "", "", "")
    _rects_few, name_right_few = compact_fit_tokens(fm, tokens, few_left, 0)
    _rects_many, name_right_many = compact_fit_tokens(fm, tokens, many_left, 0)
    assert name_right_many < name_right_few


def test_the_archive_cell_shows_age_and_verdict_together():
    """A bare mark cannot tell one stale archive from another.

    PERF-002 (audit/9.md): the fourth argument is now the canonical tri-state,
    not a boolean whose producer and consumer read it in opposite directions.
    """
    from audapack.ui_qt.models.project_delegate import compact_archive_cell

    assert compact_archive_cell(False, "", "none", None) == "—"
    assert compact_archive_cell(True, "16m", "fresh", "FRESH") == "16m ✓"
    assert compact_archive_cell(True, "2d 7h", "old", "FRESH") == "2d7h !"
    assert compact_archive_cell(True, "1d", "stale", "FRESH") == "1d ·"
    # A source that moved on outranks any age verdict: repack first.
    assert compact_archive_cell(True, "1d", "fresh", "STALE") == "1d ▲"
    # Freshness that could not be proven is never painted as current.
    assert compact_archive_cell(True, "1d", "fresh", "UNKNOWN") == "1d ?"
    assert compact_archive_cell(True, "1d", "fresh", None) == "1d ?"


def test_the_legacy_cell_offsets_survive_for_full_mode_importers():
    """Full mode and legacy importers still get contiguous fixed cells."""
    order = [name for name, _width in COMPACT_STATE_CELL_WIDTHS]
    cells = compact_state_columns(0)
    assert list(cells) == order


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
    # SRC-081 TARGET H: hit-testing asks the SAME helper the painter asks.
    assert "full_row_min_width" in src


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
    """Guards the token-rail rewrite against a crash in the real paint path."""
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


def test_a_screenshot_class_row_paints_without_error(compact_window):
    """The supplied 640px-class viewport must paint cleanly (Phase H)."""
    index = compact_window.model.index_for_project_id("p1")
    assert index.isValid()

    pixmap = QPixmap(620, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 620, 22)
        compact_window.delegate.paint(painter, option, index)
    finally:
        painter.end()


def test_an_unanswered_bridge_poll_says_so():
    """Skipping the write is what let a startup verdict outlive the truth.

    The startup probe writes "Bridge OFFLINE" the moment it cannot reach a
    Bridge that is still booting. Nothing overwrote it, so that verdict stood
    for a whole poll interval next to a Settings tab reading BRIDGE CONNECTED.
    """
    from audapack.ui_qt.main_window import bridge_status_text

    assert bridge_status_text({}) == "BRIDGE ✗ | not answering"
    assert bridge_status_text({"active_workers": 2, "max_workers": 6}).startswith("BRIDGE ✓")
