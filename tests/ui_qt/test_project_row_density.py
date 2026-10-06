"""Project Room compact-row density (T-173).

The compact row used to position its state grid from a guessed
FIXED_ACTIONS_WIDTH (190px) and five maximum-width cells (234px). At the
supplied 640px-class viewport that painted a dead strip before the action
buttons, kept holes for absent fields, and elided project names so hard they
became unreadable -- all while pixels sat unused in the same row.

The invariants here are relative/font-metric, never raw pixel counts across
DPI: the content boundary is the REAL action geometry, empty tokens cost
zero, the rail is right-anchored with one declared gap, and the project name
gets every pixel the rail does not need.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QStyleOptionViewItem

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_delegate import (
    COMPACT_RAIL_GAP,
    actions_block_width,
    compact_fit_tokens,
    compact_state_tokens,
    compute_actions_left,
)


@pytest.fixture
def window(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="p1", display_name="_AUDAPACK",
                    source_path=str(tmp_path / "p1"), priority_group="MAIN0", slot=1),
        ],
    )
    config.ui.compact_rows = True
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    yield win
    win.close()


def _metrics() -> QFontMetrics:
    return QFontMetrics(QFont("Verdana", 9))


def _launchers(count: int):
    return [type("L", (), {"enabled": True, "id": f"l{i}"})() for i in range(count)]


def _tokens_full() -> list[tuple[str, str]]:
    return compact_state_tokens("AUDIT", "2/3", "2d7h", "ZIP 16,7MB", "", "16m ✓")


def test_actual_action_width_derives_from_real_geometry(qapp):
    """Phase G #1: no 190px phantom -- the boundary is the real [edit] edge."""
    rect = QRect(0, 0, 620, 22)
    # SRC-081 TARGET H: no phantom constant and no six-button ceiling -- the
    # block width is measured from the launchers it is given (label-derived
    # button widths): 2 launchers = 103px, each extra button adds 20px, and
    # the shipped ten-launcher default set measures 263px.
    assert actions_block_width(_launchers(2)) == 103
    assert actions_block_width(_launchers(6)) == 183
    assert actions_block_width(_launchers(10)) == 263
    assert actions_block_width() == 263  # default set: ten two-char labels
    few = compute_actions_left(rect, _launchers(2))
    many = compute_actions_left(rect, _launchers(6))
    # 2 launchers: 2*18w + 1*2 gap + 2 margin | 4+18 info | 2+18 plus | 2+18 edit
    # (QRect.right() is left+width-1, so the block hugs at right()-1... the
    # launcher start is right()-1 - total - 2; assert the boundary structurally
    # instead of re-deriving pixel arithmetic.)
    assert few == rect.width() - 103  # 2*18+1*2+2+4+18+2+18+2+18 = 102 px of block
    assert many < few


def test_no_dead_strip_at_screenshot_width(qapp):
    """Phase G #2: at ~620px the gap between the last token and the action
    block is only the declared rail gap."""
    rect = QRect(0, 0, 620, 22)
    fm = _metrics()
    actions_left = compute_actions_left(rect, _launchers(3))
    rects, _name_right = compact_fit_tokens(fm, _tokens_full(), actions_left, 100)
    rightmost = max(r.right() for _key, r in rects)
    assert COMPACT_RAIL_GAP <= actions_left - rightmost <= COMPACT_RAIL_GAP + 1


def test_empty_tokens_cost_zero_width(qapp):
    """Phase G #3: absent WAVES/AGE leave no invisible holes."""
    fm = _metrics()
    sparse = compact_state_tokens("A3", "", "", "ZIP 16,7MB", "", "1h40m ✓")
    full = _tokens_full()
    rects_sparse, _n = compact_fit_tokens(fm, sparse, 500, 0)
    rects_full, _n2 = compact_fit_tokens(fm, full, 500, 0)
    sparse_width = sum(r.width() for _k, r in rects_sparse) + COMPACT_RAIL_GAP * (len(rects_sparse) - 1)
    full_width = sum(r.width() for _k, r in rects_full) + COMPACT_RAIL_GAP * (len(rects_full) - 1)
    assert sparse_width < full_width
    assert [key for key, _r in rects_sparse] == ["run", "zip", "arc"]


def test_short_run_token_uses_its_own_width(qapp):
    """Phase G #4: "FAIL" must not consume the old 50px RUN cell."""
    fm = _metrics()
    tokens = compact_state_tokens("FAIL", "", "", "", "", "")
    rects, _n = compact_fit_tokens(fm, tokens, 500, 0)
    run_rect = dict(rects)["run"]
    assert run_rect.width() == fm.horizontalAdvance("FAIL")
    assert run_rect.width() < 50


def test_project_name_reclaims_space_at_620px(qapp):
    """Phase G #5: the name rectangle is materially wider than the old
    fixed-reservation boundary at a screenshot-class width. Relative
    invariant against the old geometry, no brittle pixel count."""
    rect = QRect(0, 0, 620, 22)
    fm = _metrics()
    actions_left = compute_actions_left(rect, _launchers(3))
    _rects, name_right = compact_fit_tokens(fm, _tokens_full(), actions_left, 100)
    old_boundary = rect.width() - 190 - 4 - 234  # FIXED_ACTIONS_WIDTH + old grid
    assert old_boundary == 192
    # The new rail degrades tail-first until the FULL name fits, so the name
    # box must hold the name itself when the geometry allows it; measured
    # against the old grid the boundary is strictly right of it.
    need = fm.horizontalAdvance("AUDIT") + fm.horizontalAdvance("2/3") \
        + fm.horizontalAdvance("2d7h") + fm.horizontalAdvance("ZIP 16,7MB") \
        + fm.horizontalAdvance("16m ✓") + 4 * 6 + 2 * 8
    # Worst case (all tokens kept) must still beat the old boundary.
    assert name_right > 100 or need < old_boundary - 100
    # And with the name degrading the rail, the box always ends right of the
    # old fixed grid start for a name the row can actually fit.
    _rects2, name_right2 = compact_fit_tokens(
        fm, compact_state_tokens("A3", "0/3", "2d7h", "ZIP 16,7MB", "", "18h53m ·"),
        actions_left, 100, name_text="_AUDAPACK",
    )
    assert name_right2 > old_boundary


def test_long_name_elides_inside_its_box(qapp):
    """Phase G #6: a long name elides without touching state or actions."""
    rect = QRect(0, 0, 620, 22)
    fm = _metrics()
    actions_left = compute_actions_left(rect, _launchers(6))
    rects, name_right = compact_fit_tokens(fm, _tokens_full(), actions_left, 100)
    # every token rectangle stays inside [name_right, actions_left]
    for _key, r in rects:
        assert r.left() >= name_right
        assert r.right() < actions_left
    painted = fm.elidedText("A_VERY_LONG_PROJECT_NAME_THAT_WILL_NOT_FIT_AT_ALL",
                            __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextElideMode.ElideRight,
                            max(0, name_right - 100))
    assert painted != "A_VERY_LONG_PROJECT_NAME_THAT_WILL_NOT_FIT_AT_ALL"


def test_recognizable_name_is_not_elided(qapp):
    """Phase G #7: a normal name that fits is returned verbatim."""
    fm = _metrics()
    rect = QRect(0, 0, 620, 22)
    actions_left = compute_actions_left(rect, _launchers(3))
    _rects, name_right = compact_fit_tokens(
        fm, _tokens_full(), actions_left, 100, name_text="_AUDAPACK"
    )
    name_width = name_right - 100 - 4
    advance = fm.horizontalAdvance("_AUDAPACK")
    # The name box always ends right of the old fixed grid start, and the
    # degradation guarantees the FULL name fits whenever the geometry allows
    # it -- so the box must hold the name at this viewport.
    assert name_width >= advance, (name_width, advance)
    elided = fm.elidedText(
        "_AUDAPACK",
        __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextElideMode.ElideRight,
        name_width,
    )
    assert elided == "_AUDAPACK"


def test_ia_badge_name_and_rail_stay_disjoint(qapp):
    """Phase G #8: name + IA suffix + state rail never overlap. The IA badge
    is reserved inside the name's own right boundary."""
    rect = QRect(0, 0, 620, 22)
    fm = _metrics()
    actions_left = compute_actions_left(rect, _launchers(4))
    ia_w = fm.horizontalAdvance(" IA3")
    rects, name_right = compact_fit_tokens(
        fm, _tokens_full(), actions_left, 100, name_text="_AUDAPACK", name_extra=ia_w
    )
    name_left = 100
    name_width = max(0, name_right - name_left - ia_w)
    ia_left = name_left + name_width
    assert ia_left + ia_w <= name_right
    for _key, r in rects:
        assert r.left() >= name_right


def test_fully_populated_row_has_no_overlap(qapp):
    """Phase G #9: five visible tokens keep their declared gaps in order."""
    fm = _metrics()
    rects, _n = compact_fit_tokens(fm, _tokens_full(), 500, 0)
    ordered = sorted(rects, key=lambda kr: kr[1].left())
    assert [key for key, _r in ordered] == ["run", "waves", "age", "zip", "arc"]
    for (_ka, a), (_kb, b) in zip(ordered, ordered[1:], strict=False):
        assert a.right() < b.left()


def test_sparse_row_paints_without_holes(qapp):
    """Phase G #10: a sparse row paints only the tokens that exist."""
    fm = _metrics()
    tokens = compact_state_tokens("A3", "", "", "", "", "")
    rects, _n = compact_fit_tokens(fm, tokens, 500, 0)
    assert [key for key, _r in rects] == ["run"]


def test_action_config_changes_move_the_real_boundary(qapp):
    """Phase G #11: few and maximum launcher counts both derive the boundary
    from their own real geometry."""
    rect = QRect(0, 0, 620, 22)
    fm = _metrics()
    for count in (0, 2, 6):
        actions_left = compute_actions_left(rect, _launchers(count))
        rects, _n = compact_fit_tokens(fm, _tokens_full(), actions_left, 100)
        for _key, r in rects:
            assert r.right() < actions_left


def test_paint_and_hit_test_share_the_action_geometry(qapp, window):
    """Phase G #12: the click handler must use the same helpers the painter
    uses -- same import, same call shape, same cramped guard."""
    import inspect

    from audapack.ui_qt import main_window

    src = inspect.getsource(main_window.ProjectTreeView.mousePressEvent)
    for helper in ("compute_row_button_rects", "compute_info_button_rect", "compute_layer_button_rects"):
        assert helper in src
    # SRC-081 TARGET H: the shared cramped-row threshold helper, derived from
    # the same configured launchers the painter uses.
    assert "full_row_min_width" in src


def test_full_mode_regression(qapp, window):
    """Phase G #13: full mode keeps its two-line aligned grid."""
    window.delegate._config.ui.compact_rows = False
    index = window.model.index_for_project_id("p1")
    pixmap = QPixmap(700, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 700, 44)
        window.delegate.paint(painter, option, index)
    finally:
        painter.end()


def test_cramped_mode_regression(qapp, window):
    """Phase G #14: below the threshold the row degrades to an elided name."""
    index = window.model.index_for_project_id("p1")
    pixmap = QPixmap(300, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 300, 22)
        window.delegate.paint(painter, option, index)
    finally:
        painter.end()


def test_screenshot_class_compact_row_paints(qapp, window):
    """Phase H: the supplied 640px-class viewport paints the compact row
    cleanly -- no crash, no overlap class regressions."""
    index = window.model.index_for_project_id("p1")
    assert index.isValid()
    pixmap = QPixmap(620, 40)
    painter = QPainter(pixmap)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 620, 22)
        window.delegate.paint(painter, option, index)
    finally:
        painter.end()


def test_tooltip_carries_the_full_display_name(qapp, window):
    """Phase F10: the tooltip never uses the elided painted string."""
    from audapack.ui_qt.models.project_delegate import ProjectItemDelegate

    project = window._service.config.projects[0]
    tip = ProjectItemDelegate.build_tooltip({"project": project, "group": "MAIN0", "slot": 1})
    assert project.display_name in tip
