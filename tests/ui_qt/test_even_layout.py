"""Toolbars and tab rows fill the width they are given.

Both used to be sized to their own text and nothing else, so a window wider
than the labels left a band of dead surface on the right of every row while the
labels stayed three-letter stubs.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QToolBar

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.even_layout import distribute_row

TIGHT = 640
ROOMY = 1400


def test_a_row_with_room_becomes_one_grid():
    """Equal cells are what makes a row scannable; the eye aims at a pitch."""
    assert distribute_row(100, [10, 10, 10, 10]) == [25, 25, 25, 25]


def test_the_leftover_pixels_go_somewhere_and_the_row_fills_exactly():
    widths = distribute_row(103, [10, 10, 10, 10])
    assert sum(widths) == 103
    assert widths == [26, 26, 26, 25]


def test_a_row_too_tight_for_a_grid_shares_the_slack_instead():
    """A grid there would clip whoever has the longest label."""
    widths = distribute_row(100, [10, 70, 10])
    assert sum(widths) == 100
    assert widths[1] >= 70, "the widest label must still fit"


def test_nothing_is_ever_squeezed_below_its_own_label():
    assert distribute_row(10, [40, 40]) == [40, 40]
    assert distribute_row(0, [40, 40]) == [40, 40]


@pytest.fixture
def window(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    qapp.setStyleSheet("")
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="p1", display_name="Project One",
                    source_path=str(tmp_path / "p1"), priority_group="MAIN0", slot=1),
        ],
    )
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    win.show()
    yield win
    win.close()


def _action_bar(win):
    bars = [b for b in win.findChildren(QToolBar) if any(a.text() for a in b.actions())]
    return bars[0]


def _row_width(bar):
    visible = [a for a in bar.actions() if a.text() and a.isVisible()]
    return sum(bar.widgetForAction(a).width() for a in visible)


def _visible_buttons(bar):
    return [bar.widgetForAction(a) for a in bar.actions() if a.text() and a.isVisible()]


def test_the_action_row_fills_the_window_at_any_width(window, qapp):
    bar = _action_bar(window)
    for width in (TIGHT, 900, ROOMY):
        window.resize(width, 600)
        qapp.processEvents()
        used = _row_width(bar)
        assert used <= width, f"the row overflows {width}px: {used}px"
        # Padding, spacing, one separator and the reserved chevron width are
        # the only slack allowed.
        assert used >= width - 48, f"{width - used}px of dead surface at {width}px"


def test_the_row_never_grows_a_second_line_for_one_button(window, qapp):
    """It did, and the arithmetic test above still passed while it happened.

    Filling the row to the last pixel SUMMONS QToolBar's overflow chevron: it
    has nowhere to sit, so it takes a button with it and the row becomes two
    lines for a single stray action. Only real geometry catches that, so this
    reads where the buttons actually landed.
    """
    bar = _action_bar(window)
    single_row_height = None
    for width in (TIGHT, 665, 700, 900, 1100, ROOMY, 1920):
        window.resize(width, 600)
        qapp.processEvents()
        buttons = _visible_buttons(bar)
        assert buttons, "the action row is empty"
        right = max(button.x() + button.width() for button in buttons)
        assert right <= bar.width(), (
            f"a button runs past the bar at {width}px: {right} > {bar.width()}"
        )
        tops = {button.y() for button in buttons}
        assert len(tops) == 1, f"the row wrapped at {width}px: button tops {sorted(tops)}"
        single_row_height = single_row_height or bar.height()
        assert bar.height() == single_row_height, (
            f"the toolbar grew from {single_row_height}px to {bar.height()}px at {width}px"
        )


def test_every_button_stays_in_the_row_and_none_is_hidden_in_the_chevron(window, qapp):
    """A button pushed into the overflow menu is a button nobody can find."""
    bar = _action_bar(window)
    window.resize(665, 600)
    qapp.processEvents()
    expected = {a.text() for a in bar.actions() if a.text() and a.isVisible()}
    placed = {
        bar.actionAt(button.geometry().center()).text()
        for button in _visible_buttons(bar)
        if bar.actionAt(button.geometry().center()) is not None
    }
    assert placed == expected, f"missing from the row: {expected - placed}"


def test_a_wide_row_spells_the_actions_out(window, qapp):
    """The width is already paid for; "GRP" does not have to stay "GRP"."""
    bar = _action_bar(window)
    window.resize(ROOMY, 600)
    qapp.processEvents()
    labels = {a.text() for a in bar.actions() if a.text()}
    assert "AUDIT GROUP" in labels
    assert "CLEAR MARKS" in labels

    window.resize(TIGHT, 600)
    qapp.processEvents()
    labels = {a.text() for a in bar.actions() if a.text()}
    assert "GRP" in labels, "a narrow row must fall back to the stubs"
    assert "AUDIT GROUP" not in labels


def test_the_short_form_survives_being_spelled_out(window, qapp):
    """It is the visibility key and the name the operator calls the button by."""
    window.resize(ROOMY, 600)
    qapp.processEvents()
    assert set(window.toolbar_actions) >= {"PACK", "START", "GRP", "WRK", "MRK"}
    window._apply_toolbar_visibility()
    hidden = {k for k, a in window.toolbar_actions.items() if not a.isVisible()}
    assert hidden == {"GG", "IA", "IA+"}


def test_the_tabs_fill_the_row_too(window, qapp):
    window.resize(ROOMY, 600)
    qapp.processEvents()
    bar = window.tabs.tabBar()
    widths = [bar.tabRect(i).width() for i in range(bar.count())]
    assert widths, "the window must have tabs"
    assert max(widths) - min(widths) <= 1, f"tabs are not one grid: {widths}"
    assert sum(widths) >= window.tabs.width() - 6 * bar.count()


def test_tabs_are_re_measured_when_the_window_widens(window, qapp):
    """updateGeometry alone leaves QTabBar's cached tab rects untouched.

    The tabs kept the widths from the previous size, overflowed the bar, and
    the scroll arrows appeared on a row with 500px to spare.
    """
    bar = window.tabs.tabBar()
    window.resize(900, 600)
    qapp.processEvents()
    narrow = bar.tabRect(0).width()
    window.resize(ROOMY, 600)
    qapp.processEvents()
    assert bar.tabRect(0).width() > narrow


# ------------------------------------------- closing the pool when it falls idle
#
# Edge-triggered off the status poll the window already makes. Enumerating every
# window and reading a command line per process is not free, and doing it every
# four seconds while nothing changed is the idle burn this app has too much of.


def _fire(window, **counts):
    window._maybe_close_idle_worker_windows({"active_workers": 6, **counts})


def _settle(window, qapp, timeout_s=4.0):
    """Pump Qt until the close task has run; it is submitted to a thread."""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if not window.task_runner.is_running("workers:close-idle"):
            qapp.processEvents()
            return
        time.sleep(0.02)


def test_the_close_fires_once_when_the_pool_falls_idle(window, qapp):
    from unittest.mock import patch

    with patch.object(window._comp_mgr, "close_idle_worker_windows",
                      return_value=(True, "Closed 3 idle worker window(s).")) as closer:
        _fire(window, queued_jobs=2, active_jobs=1)   # busy
        closer.assert_not_called()
        _fire(window, queued_jobs=0, active_jobs=0)   # busy -> idle
        _settle(window, qapp)
    assert closer.call_count == 1


def test_a_pool_that_was_never_busy_does_not_fire(window, qapp):
    """Opening the app onto an idle pool must not close windows behind you."""
    from unittest.mock import patch

    with patch.object(window._comp_mgr, "close_idle_worker_windows") as closer:
        _fire(window, queued_jobs=0, active_jobs=0)
        _fire(window, queued_jobs=0, active_jobs=0)
        _settle(window, qapp, 1.5)
    closer.assert_not_called()


def test_staying_idle_does_not_fire_again(window, qapp):
    from unittest.mock import patch

    with patch.object(window._comp_mgr, "close_idle_worker_windows",
                      return_value=(False, "No worker window to close.")) as closer:
        _fire(window, queued_jobs=1)
        _fire(window, queued_jobs=0)
        _settle(window, qapp)
        _fire(window, queued_jobs=0)
        _fire(window, queued_jobs=0)
        _settle(window, qapp, 1.5)
    assert closer.call_count == 1


def test_the_setting_off_stops_it(window, qapp):
    from unittest.mock import patch

    window._service.config.ui.close_idle_worker_windows = False
    with patch.object(window._comp_mgr, "close_idle_worker_windows") as closer:
        _fire(window, queued_jobs=1)
        _fire(window, queued_jobs=0)
        _settle(window, qapp, 1.5)
    closer.assert_not_called()


def test_a_blocked_run_counts_as_busy(window, qapp):
    """It is waiting for the operator IN that window."""
    from unittest.mock import patch

    with patch.object(window._comp_mgr, "close_idle_worker_windows") as closer:
        _fire(window, queued_jobs=1)
        _fire(window, queued_jobs=0, blocked_jobs=1)
        _settle(window, qapp, 1.5)
    closer.assert_not_called()
