"""Rows that fill the width they are given: toolbars and tab bars.

Both were sized to their own text and nothing else, so a window wider than the
labels left a band of empty surface on the right of every row while the labels
themselves stayed cryptic three-letter stubs. Width the row already owns is
free: spend it on spelling the actions out, and on making the buttons a grid
the eye can aim at instead of a ragged run of different sizes.

The narrow case is the one that constrains everything: the action row still has
to fit a 640px window in ONE line, so a button's own label width is the floor
and nothing here may push a row past what it is given.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QSize
from PySide6.QtWidgets import QStyle, QStyleOptionToolButton, QTabBar

#: Spelled-out labels for the action row, used only when the row is wide enough
#: to hold every one of them. The keys stay the short forms: they are what the
#: visibility setting stores and what the operator names a button by.
#:
#: The audit profile switches (A3/A10/CM) are deliberately absent -- they are
#: names, not abbreviations, and "QUICK 3" is not clearer than "A3".
TOOLBAR_FULL_LABELS = {
    "PACK": "PACK",
    "START": "START AUDIT",
    "GRP": "AUDIT GROUP",
    "WRK": "WORKERS",
    "NEW": "NEW WINDOW",
    "ALL": "PACK ALL",
    "COPY": "COPY AUDIT",
    "GG": "COPY GG",
    "IA": "INAUDIT",
    "IA+": "INAUDIT +",
    "ZIP": "COPY ZIP",
    "MRK": "CLEAR MARKS",
}


def natural_button_width(button, text: str) -> int:
    """Width this button needs to render ``text`` without eliding.

    Taken from the style itself: a hand-picked padding was 5px short of the
    border and padding the stylesheet adds, and every label rendered "P...K".
    """
    # Without this the button is measured before the stylesheet's border and
    # padding are applied, and every label ends up two pixels short.
    button.ensurePolished()
    metrics = button.fontMetrics()
    option = QStyleOptionToolButton()
    option.initFrom(button)
    option.text = text
    return button.style().sizeFromContents(
        QStyle.ContentsType.CT_ToolButton,
        option,
        QSize(metrics.horizontalAdvance(text), metrics.height()),
        button,
    ).width()


def _toolbar_overhead(toolbar, item_count: int) -> int:
    """Everything in the row that is not a button: padding, spacing, separators."""
    layout = toolbar.layout()
    spacing = layout.spacing() if layout is not None else 0
    margins = layout.contentsMargins() if layout is not None else None
    padding = (margins.left() + margins.right()) if margins is not None else 4
    separators = 0
    for action in toolbar.actions():
        if not action.isSeparator():
            continue
        widget = toolbar.widgetForAction(action)
        if widget is not None:
            separators += max(widget.sizeHint().width(), 1)
    return padding + separators + spacing * max(0, item_count - 1)


def distribute_row(available: int, naturals: list[int]) -> list[int]:
    """Widths that fill ``available`` exactly and never elide a label.

    One grid when the widest label fits every cell -- that is what makes the
    row scannable. Otherwise the slack is shared out evenly on top of each
    button's own width, because forcing a grid there would clip somebody.
    """
    count = len(naturals)
    if count == 0:
        return []
    if available <= sum(naturals):
        return list(naturals)
    if available >= max(naturals) * count:
        base, remainder = divmod(available, count)
        return [base + (1 if i < remainder else 0) for i in range(count)]
    share, remainder = divmod(available - sum(naturals), count)
    return [nat + share + (1 if i < remainder else 0) for i, nat in enumerate(naturals)]


def fit_toolbar(toolbar, available: Optional[int] = None, full_labels: Optional[dict] = None) -> int:
    """Size every button in ``toolbar`` so the row fills its width.

    Returns the width the visible buttons occupy. Hidden buttons are sized too,
    so one the operator un-hides is already right instead of the ~59px style
    minimum -- but they are not counted, and they take none of the row.
    """
    labels = TOOLBAR_FULL_LABELS if full_labels is None else full_labels
    entries = []
    for action in toolbar.actions():
        if not action.text():
            continue
        button = toolbar.widgetForAction(action)
        if button is None:
            continue
        key = str(action.property("toolbar_key") or action.text())
        entries.append((action, button, key))
    if not entries:
        return 0

    visible = [entry for entry in entries if entry[0].isVisible()]
    if available is None:
        available = toolbar.width()
    available = max(0, int(available) - _toolbar_overhead(toolbar, len(visible)))

    # Spell the actions out only when every one of them fits spelled out. A row
    # of half-expanded labels reads worse than a row of consistent stubs.
    short = {key: key for _a, _b, key in entries}
    full = {key: str(labels.get(key, key)) for _a, _b, key in entries}
    wide_enough = sum(
        natural_button_width(button, full[key]) for _a, button, key in visible
    ) <= available
    chosen = full if wide_enough else short

    for action, _button, key in entries:
        if action.text() != chosen[key]:
            action.setText(chosen[key])

    naturals = [natural_button_width(button, chosen[key]) for _a, button, key in visible]
    widths = distribute_row(available, naturals)
    for (_action, button, _key), width in zip(visible, widths, strict=True):
        button.setMinimumWidth(0)
        button.setFixedWidth(width)
    for action, button, key in entries:
        if action.isVisible():
            continue
        button.setMinimumWidth(0)
        button.setFixedWidth(natural_button_width(button, chosen[key]))
    return sum(widths)


class EvenTabBar(QTabBar):
    """Tabs share the bar's full width instead of huddling on the left.

    The stylesheet sizes a tab from its own text, so five tabs ended a third of
    the way across the window and the rest of the row was dead surface.
    """

    #: Per-tab margin the stylesheet adds (``margin-right``), which is not part
    #: of the size hint and would otherwise push the last tab off the end.
    TAB_MARGIN = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        # The hints are a function of the PARENT's width, and nothing tells a
        # tab bar its parent got wider. Without this the tabs keep the widths
        # from the previous size: after a widen they overflow the bar and the
        # scroll arrows appear on a row with room to spare.
        if parent is not None:
            parent.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            # updateGeometry() alone marks the WIDGET dirty, not the tab rects
            # QTabBar caches internally, so the bar kept the widths from the
            # previous size. setIconSize is the one piece of public API that
            # drops that cache; the value is deliberately unchanged.
            self.setIconSize(self.iconSize())
            self.updateGeometry()
        return super().eventFilter(watched, event)

    def _row_width(self) -> int:
        parent = self.parentWidget()
        # The bar's own width is a result of these hints; reading it here would
        # feed the layout its own output and let the tabs creep on every pass.
        if parent is not None and parent.width() > 0:
            return parent.width()
        return self.width()

    def tabSizeHint(self, index: int) -> QSize:
        hint = super().tabSizeHint(index)
        count = self.count()
        if count <= 0:
            return hint
        available = self._row_width() - self.TAB_MARGIN * count
        if available <= 0:
            return hint
        base, remainder = divmod(available, count)
        width = base + (1 if index < remainder else 0)
        # A tab never shrinks below its own label: a narrow window keeps the
        # scroll arrows rather than eliding every tab into nothing.
        return QSize(max(hint.width(), width), hint.height())
