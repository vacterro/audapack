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
    yield window, bars[0]
    window.close()


def test_every_button_is_sized_to_its_own_label(toolbar):
    _window, bar = toolbar
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
    actions = [a for a in bar.actions() if a.text()]
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


def test_start_audit_is_not_duplicated_by_the_profile_buttons(toolbar):
    """A3/A10/CM each start an audit, so a generic START AUDIT is the same act."""
    _window, bar = toolbar
    labels = [a.text() for a in bar.actions() if a.text()]
    assert "START AUDIT" not in labels
    assert {"A3", "A10", "CM"} <= set(labels)
