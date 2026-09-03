"""The action row must fit a 640x480 window in ONE line.

Qt's own minimum for a QToolButton is ~59px whatever the label, so a
two-character button cost as much as a word and fourteen actions wrapped onto
a second row. Every button is now sized to its own text.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QToolBar

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService

TARGET_WIDTH = 640


@pytest.fixture
def toolbar(tmp_path, qapp):
    """The window at its NARROWEST supported width.

    The row fills whatever width it is given, so every "does it fit" assertion
    below has to be made at the width that actually constrains it.
    """
    from audapack.ui_qt.main_window import MainWindow

    # Deliberately NOT set on the app: production applies the theme from
    # inside MainWindow, after the toolbar exists. Pre-styling the app here
    # is what let the elision bug pass its own regression.
    qapp.setStyleSheet("")
    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="p1", display_name="Project One",
                    source_path=str(tmp_path / "p1"), priority_group="MAIN0", slot=1),
        ],
    )
    window = MainWindow(ProjectService(config, base_dir=tmp_path))
    bars = [bar for bar in window.findChildren(QToolBar) if any(a.text() for a in bar.actions())]
    assert bars, "the action toolbar must exist"
    window.resize(TARGET_WIDTH, 480)
    window.show()
    qapp.processEvents()
    from audapack.ui_qt.main_window import _fit_toolbar_to_text
    _fit_toolbar_to_text(bars[0], TARGET_WIDTH)
    yield window, bars[0]
    window.close()


def test_every_button_is_sized_to_its_own_label(toolbar):
    """No button carries the style's ~59px minimum whatever its label.

    That minimum is what made a two-character button cost as much as a word and
    fourteen actions wrap onto a second row. The row DOES pad buttons out to
    fill the width it is given -- that is the point of it -- so the question is
    asked of the natural widths, which is what a zero-width fit hands back.
    """
    from audapack.ui_qt.main_window import _fit_toolbar_to_text

    _window, bar = toolbar
    _fit_toolbar_to_text(bar, 0)
    actions = [a for a in bar.actions() if a.text()]
    for action in actions:
        button = bar.widgetForAction(action)
        text_width = button.fontMetrics().horizontalAdvance(action.text())
        assert button.width() < text_width + 30, (
            f"{action.text()} is padded far past its label: {button.width()}px for {text_width}px of text"
        )


def test_no_label_is_narrow_enough_to_elide(toolbar):
    """A hand-picked padding was 5px short and every label rendered "P...K"."""
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QStyle, QStyleOptionToolButton

    _window, bar = toolbar
    for action in [a for a in bar.actions() if a.text()]:
        button = bar.widgetForAction(action)
        metrics = button.fontMetrics()
        option = QStyleOptionToolButton()
        option.initFrom(button)
        option.text = action.text()
        needed = button.style().sizeFromContents(
            QStyle.ContentsType.CT_ToolButton,
            option,
            QSize(metrics.horizontalAdvance(action.text()), metrics.height()),
            button,
        ).width()
        assert button.width() >= needed, (
            f"{action.text()} would render elided: {button.width()}px given, {needed}px needed"
        )


def test_the_whole_action_row_fits_640(toolbar):
    _window, bar = toolbar
    actions = [a for a in bar.actions() if a.text() and a.isVisible()]
    used = sum(bar.widgetForAction(a).width() for a in actions)
    # Toolbar spacing is 2px per item plus its own 2px padding on both sides.
    used += bar.layout().spacing() * len(actions) + 8
    assert used <= TARGET_WIDTH, (
        f"{len(actions)} buttons need {used}px and would wrap at {TARGET_WIDTH}px: "
        + ", ".join(f"{a.text()}={bar.widgetForAction(a).width()}" for a in actions)
    )


def test_a_checked_button_is_not_clipped_by_its_pressed_padding(toolbar):
    """The profile button that marks the current default is CHECKED.

    The shared pressed rule widens horizontal padding from 4px to 12px to fake
    the bevel shift, which is 8px a width-fitted button does not have: the
    checked profile button rendered as "..." while every other label was fine.
    """
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QStyle, QStyleOptionToolButton

    _window, bar = toolbar
    checked = [a for a in bar.actions() if a.text() and a.isCheckable() and a.isChecked()]
    assert checked, "one profile button must mark the current default"

    for action in checked:
        button = bar.widgetForAction(action)
        button.ensurePolished()
        metrics = button.fontMetrics()
        option = QStyleOptionToolButton()
        option.initFrom(button)
        option.text = action.text()
        needed = button.style().sizeFromContents(
            QStyle.ContentsType.CT_ToolButton,
            option,
            QSize(metrics.horizontalAdvance(action.text()), metrics.height()),
            button,
        ).width()
        assert button.width() >= needed, (
            f"checked {action.text()} would render elided: {button.width()}px given, {needed}px needed"
        )


def test_ctrl_r_refreshes_everything(toolbar):
    """REFRESH lost its permanent slot, so the shortcut has to exist."""
    from PySide6.QtGui import QKeySequence, QShortcut

    window, bar = toolbar
    shortcuts = [
        s for s in window.findChildren(QShortcut)
        if s.key() == QKeySequence("Ctrl+R")
    ]
    assert shortcuts, "Ctrl+R must be bound"
    assert not [a for a in bar.actions() if a.text() in {"REF", "REFRESH"}]

    called = []
    window._on_refresh_all = lambda: called.append(True)
    shortcuts[0].activated.disconnect()
    shortcuts[0].activated.connect(window._on_refresh_all)
    shortcuts[0].activated.emit()
    assert called == [True]


def test_the_profile_buttons_are_switches_next_to_one_start(toolbar):
    """A3/A10/CM select; START runs.

    They looked like mode indicators, so firing an audit the moment one was
    pressed was a surprise -- and an audit is not an undoable thing to be
    surprised by.
    """
    _window, bar = toolbar
    labels = [a.text() for a in bar.actions() if a.text()]
    assert {"A3", "A10", "CM", "START"} <= set(labels)
    for name in ("A3", "A10", "CM"):
        action = next(a for a in bar.actions() if a.text() == name)
        assert action.isCheckable(), f"{name} must read as a switch"
    start = next(a for a in bar.actions() if a.text() == "START")
    assert not start.isCheckable()


def test_the_default_row_hides_the_buttons_that_have_another_route(toolbar):
    """Width is the scarce thing at 640px, so GG/IA/IA+ start hidden.

    GG has Ctrl+C and the two INAUDIT actions live on their own tab, so the
    width they used to spend now belongs to the buttons with no other way in.
    Hidden, never removed: one checkbox in Settings brings any of them back.
    """
    window, _bar = toolbar
    hidden = {key for key, action in window.toolbar_actions.items() if not action.isVisible()}
    assert hidden == {"GG", "IA", "IA+"}
    assert set(window.toolbar_actions) >= {"PACK", "START", "GRP", "A3", "A10", "CM", "MRK"}


def test_a_hidden_button_costs_the_row_no_width(toolbar):
    """Asked at zero available width, so the answer is the natural widths.

    Given room the row fills it either way -- that is what the widths mean now
    -- so the question "does a hidden button take width" is only meaningful
    where there is none to hand out.
    """
    from audapack.ui_qt.main_window import _fit_toolbar_to_text

    window, bar = toolbar
    narrow = _fit_toolbar_to_text(bar, 0)
    for action in window.toolbar_actions.values():
        action.setVisible(True)
    wide = _fit_toolbar_to_text(bar, 0)
    assert wide > narrow, "showing three more buttons must widen the row"


def test_showing_a_hidden_button_gives_it_a_correct_width(toolbar):
    """It is sized while hidden, so it is never the style minimum when shown."""
    window, bar = toolbar
    button = bar.widgetForAction(window.toolbar_actions["GG"])
    assert button is not None
    assert button.width() < 40, f"GG kept the style minimum: {button.width()}px"


def test_no_tooltip_is_a_paragraph(toolbar):
    """A tooltip that wraps the whole screen is not a tooltip.

    One of them ran to four sentences and rendered as a bar across the display.
    """
    window, _bar = toolbar
    for key, action in window.toolbar_actions.items():
        tip = action.toolTip()
        assert tip, f"{key} has no tooltip"
        assert chr(10) not in tip, f"{key} tooltip is multi-line: {tip!r}"
        assert len(tip) <= 90, f"{key} tooltip is {len(tip)} chars: {tip!r}"
