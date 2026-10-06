"""Qt Delegate for Project Room Rows (Wave M).

Renders compact Golden Default Win95-style rows:
- Group headers with distinct raised bevel
- Slot badge [1..6]
- Project name + audit badges
- Compact wave/age/ZIP indicators
- Launcher buttons [1..N] + [GG]
- Empty slot representation
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from audapack.audits import format_age_str
from audapack.campaign import get_profile, profile_short_label
from audapack.freshness import ArchiveFreshness
from audapack.models import AuditTemperature
from audapack.ui_qt.theme.golden_default import PALETTE

# Temperature color scheme — muted, readable on dark bg, not acidic.
# Each key maps to a hex color for the temperature name.
TEMP_COLORS = {
    "HOT": "#E8A860",
    "WARM": "#D4B878",
    "COOL": "#9AA8C8",
    "COLD": "#6E8CB0",
    "STALE": "#8A8078",
    "NONE": "#7A7A7A",
}


def launcher_button_label(launcher: Any) -> str:
    """The ONE display label for a launcher button (SRC-081 TARGET I).

    The launcher's canonical ``short_label`` is the primary display source;
    the name's first two characters are only a last-resort fallback for
    entries without one. No per-surface id->label map exists anymore.
    """
    label = str(getattr(launcher, "short_label", "") or "").strip()
    if label:
        return label
    name = str(getattr(launcher, "name", "") or "").strip()
    return name[:2].upper() or "?"


def launcher_button_width(label: str) -> int:
    """Button width from the label string alone -- deterministic (TARGET I).

    Two-character labels keep the historical 18px cell (T-173 geometry);
    every extra character widens its own button instead of being clipped
    silently. String-based on purpose: paint and hit-testing compute identical
    pixels without sharing a font-metrics object.
    """
    chars = max(1, len(str(label or "").strip()))
    return 18 if chars <= 2 else 18 + 7 * (chars - 2)


def compute_row_button_rects(row_rect: QRect, launchers: Optional[list[Any]] = None) -> tuple[list[tuple[Any, QRect]], QRect]:
    """Computes layout of [1]..[N] launcher buttons (letters OC/FB/CL/C1/C2/CF) on right edge.

    GG button removed as obsolete. Info [ⓘ] placed left of launchers.
    Returns (launcher_buttons, _unused_gg_rect) for compat.

    Buttons always hug the FIXED right edge so the state column stays at the
    same x for every row -- a varying launcher count used to shift the column
    start and made the rows visually drift out of alignment.
    """
    # GG removed — keep dummy rect for compat but zero size
    gg_rect = QRect(row_rect.right() - 2, row_rect.top() + 2, 0, 20)
    if not launchers:
        return [], gg_rect

    enabled_launchers = [launcher for launcher in launchers if getattr(launcher, "enabled", True)]
    if not enabled_launchers:
        return [], gg_rect

    gap = 2
    # TARGET H/I: per-button width derives from the label actually painted
    # (the launcher's canonical short_label) instead of a fixed 18px cell for
    # an assumed six buttons. Paint and hit-testing share this helper, so the
    # geometry can never disagree between what is drawn and what is clickable.
    widths = [launcher_button_width(launcher_button_label(launcher)) for launcher in enabled_launchers]
    total_w = sum(widths) + gap * max(0, len(enabled_launchers) - 1)
    # hug right edge with 2px margin (was 36 with GG)
    start_x = row_rect.right() - total_w - 2

    cur_x = start_x
    result_buttons: list[tuple[Any, QRect]] = []
    for launcher, btn_w in zip(enabled_launchers, widths, strict=True):
        r = QRect(cur_x, row_rect.top() + 2, btn_w, 20)
        result_buttons.append((launcher, r))
        cur_x += btn_w + gap

    return result_buttons, gg_rect


#: Row narrower than this cannot fit checkboxes + name + state grid + the
#: right-edge button block without elements landing on top of each other.
#: Below it the row degrades gracefully: no state grid, no buttons — only
#: checkboxes, slot badge and an elided name. The click handler in
#: main_window guards on the same constant, so paint and hit-testing agree.
MIN_ROW_WIDTH = 340

#: SRC-081 TARGET H: there is deliberately NO ``MAX_LAUNCHERS`` ceiling
#: anymore. The action block derives from the actual configured enabled
#: launchers (below), so ten shipped launchers -- or more, or fewer -- all lay
#: out from one geometry authority. A cap existed only because the old UI
#: assumed six buttons and six shortcuts.


def actions_block_width(launchers: Optional[list[Any]] = None) -> int:
    """Exact width of the right-edge button block for ``launchers``.

    Derived from the SAME geometry helpers the painter and hit-tester use --
    never a guessed constant and never a fixed six-launcher ceiling (TARGET
    H). With no argument the shipped default set is measured, which is the
    worst case a window must survive with defaults enabled.
    """
    if launchers is None:
        from audapack.config import create_default_launchers

        launchers = create_default_launchers()
    probe = QRect(0, 0, 10000, 22)
    enabled = [launcher for launcher in launchers if getattr(launcher, "enabled", True)]
    launcher_buttons, gg_rect = compute_row_button_rects(probe, enabled)
    info_rect = compute_info_button_rect(probe, launcher_buttons, gg_rect)
    _plus_rect, edit_rect = compute_layer_button_rects(probe, info_rect)
    return probe.right() + 1 - edit_rect.left()


#: The exact width at which the full row layout (name + state grid + buttons)
#: stops fitting. ONE threshold shared by the delegate's paint and
#: main_window's hit-testing, so a control can never be clickable where it is
#: not painted (or painted where it is not clickable). Computed below, after
#: the geometry helpers it derives from.
FULL_ROW_MIN_WIDTH = 0

#: Legacy fixed-cell vocabulary for the compact rail -- kept ONLY as the
#: token order contract for compact_fit_tokens callers and the old
#: compact_state_columns offsets. No paint path reads these widths anymore.
COMPACT_STATE_CELL_WIDTHS = (("run", 50), ("waves", 30), ("age", 44), ("zip", 58), ("arc", 52))

#: Full-mode state grid: RUN | WAVES | AGE on line 0, ZIP | PACK on line 1.
#: Both lines must stay inside FULL_STATE_WIDTH -- a cell beyond it lands on
#: the fixed right-edge button block. That is exactly how the INAUDIT badge
#: ended up painted under the [edit] button and how a ZIP line fitted against
#: the whole column width ran into the PACK badge.
FULL_STATE_CELL_WIDTHS = (("run", 64), ("waves", 42), ("age", 62), ("zip", 100), ("pack", 60))
FULL_STATE_WIDTH = 175

#: Compact rows are a right-anchored TOKEN RAIL, not a fixed grid: every
#: visible token takes its measured text width plus one small gap, absent
#: values take zero width, and whatever the rail does not need flows back to
#: the project name. The old fixed five-cell grid (RUN 50 | WAVES 30 | AGE 44
#: | ZIP 58 | ARC 52 = 234px) reserved maximum-width cells on every row, so
#: sparse rows painted large holes and every row elided its name while pixels
#: sat unused.
COMPACT_TOKEN_GAP = 6
#: The gap between the last state token and the first action control.
COMPACT_RAIL_GAP = 8
#: The project name outranks low-value verbose state text (F6): if the rail
#: would squeeze the name below this, tail tokens (ARC, ZIP, AGE, WAVES) are
#: dropped one by one -- RUN always survives, and the cramped-row fallback
#: still owns the truly narrow case.
COMPACT_NAME_MIN = 44

#: The slot badge must hold the widest value the Add/Edit dialog can produce:
#: slots run 1..10, so "[10]" is the worst case. The width is derived from the
#: font at paint time (see slot_badge_width) -- a hardcoded pixel width lies the
#: moment the theme's NoAntialias strategy makes Verdana 9 render at 48px.
SLOT_BADGE_WORST = "[10]"
SLOT_BADGE_MIN = 24


def compact_state_tokens(
    wave_text: str,
    waves_label: str,
    age_cell: str,
    zip_text: str,
    packing: str,
    arc_cell: str,
) -> list[tuple[str, str]]:
    """Ordered semantic compact tokens: RUN WAVES AGE ZIP ARC (F3).

    Empty values are simply absent -- an absent field consumes zero width.
    Packing owns the ZIP token exactly as it used to own the ZIP cell.
    """
    tokens: list[tuple[str, str]] = [("run", wave_text)]
    if waves_label:
        tokens.append(("waves", waves_label))
    if age_cell:
        tokens.append(("age", age_cell))
    if (packing or zip_text).strip():
        tokens.append(("zip", packing or zip_text))
    if arc_cell:
        tokens.append(("arc", arc_cell))
    return tokens


def compute_actions_left(row_rect: QRect, launchers: Optional[list[Any]] = None) -> int:
    """Left edge of the REAL action block: [edit] [+] [i] [launchers...].

    Derived from the same three helpers the painter and the hit-tester share,
    so the content boundary can never disagree with the actual buttons. All
    rows share one global launcher configuration, so this edge is identical
    on every row and the columns still align.
    """
    launcher_buttons, gg_rect = compute_row_button_rects(row_rect, launchers)
    info_rect = compute_info_button_rect(row_rect, launcher_buttons, gg_rect)
    _plus_rect, edit_rect = compute_layer_button_rects(row_rect, info_rect)
    return edit_rect.left()


def compact_fit_tokens(
    metrics,
    tokens: list[tuple[str, str]],
    actions_left: int,
    name_left: int,
    name_text: str = "",
    name_extra: int = 0,
) -> tuple[list[tuple[str, QRect]], int]:
    """Fit the compact state rail right-anchored at the real action block.

    ``tokens`` is the ordered (key, text) rail content; empty texts are the
    caller's responsibility to omit. Every kept token takes its measured
    horizontalAdvance -- never a reserved cell -- and one COMPACT_TOKEN_GAP
    between neighbours. The rail sits COMPACT_RAIL_GAP left of ``actions_left``.

    F6: the project name outranks low-value state text. While the FULL name
    (plus ``name_extra``, e.g. the IA badge reservation) would not fit in the
    box the rail leaves it, tail tokens are dropped lowest-priority-first
    (ARC, ZIP, AGE, WAVES) until it fits; RUN always survives, and when even
    RUN leaves too little the name elides inside what is left -- operational
    status is never completely removed for an arbitrarily long name.
    Returns (token_rects, name_right): the name rectangle ends at
    ``name_right``.
    """
    kept = [(key, text) for key, text in tokens if text]
    widths: dict[str, int] = {}

    def _name_right() -> int:
        total = sum(widths[key] for key, _text in kept) + COMPACT_TOKEN_GAP * max(0, len(kept) - 1)
        return actions_left - COMPACT_RAIL_GAP - total - COMPACT_TOKEN_GAP

    def _name_fits() -> bool:
        if not name_text:
            return True
        need = metrics.horizontalAdvance(name_text) + name_extra
        return _name_right() - name_left >= need

    while True:
        for key, text in kept:
            widths[key] = metrics.horizontalAdvance(text)
        if _name_fits() or len(kept) <= 1:
            break
        kept.pop()  # lowest-priority token (rightmost of RUN,WAVES,AGE,ZIP,ARC)
    name_right = _name_right()
    rects: list[tuple[str, QRect]] = []
    x = actions_left - COMPACT_RAIL_GAP
    for key, _text in reversed(kept):
        x -= widths[key]
        rects.append((key, QRect(x, 0, widths[key], 0)))
        x -= COMPACT_TOKEN_GAP
    rects.reverse()
    return rects, name_right


def slot_badge_width(metrics) -> int:
    """Width the slot badge needs, from the font actually in use.

    Same value on every row, so the name column stays aligned. Derived from
    metrics instead of a constant: the NoAntialias strategy (crisp pixels) and
    per-machine DPI both change how wide "[10]" paints, and a fixed 24/30px
    badge clipped a two-digit slot straight onto the project name.
    """
    return max(metrics.horizontalAdvance(SLOT_BADGE_WORST), SLOT_BADGE_MIN)


def fit_zip_text(candidates: list[str], advance, limit: int) -> str:
    """First candidate that fits ``limit`` pixels, else the shortest one."""
    for candidate in candidates:
        if advance(candidate) <= limit:
            return candidate
    return candidates[-1] if candidates else ""


def compact_archive_cell(
    exists: bool,
    age_str: str,
    freshness_short: str,
    archive_freshness: Optional[str],
) -> str:
    """Text of the compact ARC cell: how old the archive is, and its verdict.

    A bare "2d" cannot be read without knowing the thresholds, and a bare mark
    cannot tell one stale archive from another. Both, or nothing.

    PERF-002 (audit/9.md): two different questions share this cell and must not
    be confused. ``age_str``/``freshness_short`` answer HOW OLD the archive is;
    ``archive_freshness`` is the canonical tri-state answer to WHETHER THE
    SOURCE MOVED ON since the pack. STALE and UNKNOWN own the mark because they
    are the actionable answers; only a proven-current archive falls through to
    its age glyph. The predecessor was a boolean named ``source_older`` whose
    True the producer set for "source is OLDER" and this consumer read as
    "source changed since the pack".
    """
    if not exists:
        return "—"
    state = str(archive_freshness or ArchiveFreshness.UNKNOWN.value).upper()
    if state == ArchiveFreshness.STALE.value:
        mark = "▲"  # source moved on since the pack -- repack before auditing
    elif state == ArchiveFreshness.UNKNOWN.value:
        mark = "?"  # freshness could not be proven; never rendered as current
    else:
        mark = {"fresh": "✓", "stale": "·", "old": "!"}.get(freshness_short, "")
    age = age_str.replace(" ", "")
    return f"{age} {mark}".strip() if age else mark


def compact_state_columns(col_x: int) -> dict[str, tuple[int, int]]:
    """Legacy fixed-cell offsets, retained only for full-mode callers.

    The compact rail no longer uses fixed cells -- it right-anchors measured
    tokens at the real action block (compact_fit_tokens). This helper stays
    for tests/importers that still speak the cell vocabulary.
    """
    cells: dict[str, tuple[int, int]] = {}
    cursor = int(col_x)
    for name, width in COMPACT_STATE_CELL_WIDTHS:
        cells[name] = (cursor, width)
        cursor += width
    return cells



def compute_info_button_rect(row_rect: QRect, launcher_buttons: list[tuple[Any, QRect]], gg_rect: QRect) -> QRect:
    """Info [ⓘ] button placed 4px left of the leftmost action button.

    The button block is anchored at the FIXED right edge so the state column
    does not shift when the launcher count differs between projects.
    """
    gap = 4
    info_w = 18
    if launcher_buttons:
        leftmost = launcher_buttons[0][1].left()
        x = leftmost - gap - info_w
    else:
        # no launchers — place near right edge where GG used to be
        x = row_rect.right() - info_w - 2
    return QRect(x, row_rect.top() + 2, info_w, 20)


def compute_layer_button_rects(row_rect: QRect, info_rect: QRect) -> tuple[QRect, QRect]:
    """[+] and [edit] layer buttons placed left of the info button.

    [+] opens the project's audit-layer editor window; [edit] opens it on the
    operator's LAST user-created layer (never on one the AUDAPACK widget
    delivered). Same 18x20 bevel geometry as the other row buttons, still
    hugging the fixed right-edge block.
    """
    btn_w = 18
    gap = 2
    plus_x = info_rect.left() - gap - btn_w
    plus_rect = QRect(plus_x, row_rect.top() + 2, btn_w, 20)
    edit_rect = QRect(plus_x - gap - btn_w, row_rect.top() + 2, btn_w, 20)
    return plus_rect, edit_rect


#: The exact width at which the full row layout (name + state grid + buttons)
#: stops fitting. ONE threshold shared by the delegate's paint and
#: main_window's hit-testing, so a control can never be clickable where it is
#: not painted (or painted where it is not clickable). Derived from the real
#: geometry helpers above, never a guessed constant.


def full_row_min_width(launchers: Optional[list[Any]] = None) -> int:
    """``MIN_ROW_WIDTH`` plus the real action block for ``launchers``.

    Paint and hit-testing BOTH ask this helper with the same configured
    launcher list (TARGET H), so the cramped-row threshold can never disagree
    between what is painted and what is clickable. The no-argument value is
    the shipped default set and stays the legacy ``FULL_ROW_MIN_WIDTH``.
    """
    return MIN_ROW_WIDTH + actions_block_width(launchers)


FULL_ROW_MIN_WIDTH = full_row_min_width()


class ProjectItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self._config = config
        self.font_main = QFont("Verdana", 9)
        self.font_main.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_bold = QFont("Verdana", 9, QFont.Weight.Bold)
        self.font_bold.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_mono = QFont("Verdana", 9)
        self.font_mono.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_small = QFont("Verdana", 8)
        self.font_small.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_tiny = QFont("Verdana", 7)
        self.font_tiny.setStyleStrategy(QFont.StyleStrategy.NoAntialias)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        node_type = index.data(Qt.ItemDataRole.UserRole + 7)  # node_type
        if node_type == "group":
            return QSize(option.rect.width(), 22)
        compact_rows = bool(getattr(getattr(self._config, "ui", None), "compact_rows", False))
        return QSize(option.rect.width(), 22 if compact_rows else 44)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        rect = option.rect

        node_type = index.data(Qt.ItemDataRole.UserRole + 7)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if node_type == "group":
            # Group header bar with Win95 Raised 2px Bevel
            grp_name = index.data(Qt.ItemDataRole.DisplayRole) or ""
            painter.fillRect(rect, QColor(PALETTE["surfaceRaised"]))

            # Top + Left highlight
            painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
            painter.drawLine(rect.left(), rect.top(), rect.right() - 1, rect.top())
            painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom() - 1)

            # Bottom + Right shadow
            painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
            painter.drawLine(rect.left(), rect.bottom() - 1, rect.right() - 1, rect.bottom() - 1)
            painter.drawLine(rect.right() - 1, rect.top(), rect.right() - 1, rect.bottom() - 1)

            painter.setFont(self.font_bold)
            painter.setPen(QColor(PALETTE["borderGolden"]))
            text_rect = rect.adjusted(4, 0, -8, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"▼  {grp_name}")
            painter.restore()
            return

        # ---- Slot row ----
        slot_num = index.data(Qt.ItemDataRole.UserRole + 4) or 1
        is_empty = index.data(Qt.ItemDataRole.UserRole + 6)
        display_name = index.data(Qt.ItemDataRole.UserRole + 2) or f"Slot {slot_num}"
        all_ready = bool(index.data(Qt.ItemDataRole.UserRole + 10))
        completed_waves = index.data(Qt.ItemDataRole.UserRole + 13) or 0
        temperature = index.data(Qt.ItemDataRole.UserRole + 8) or AuditTemperature.NONE
        pack_state = index.data(Qt.ItemDataRole.UserRole + 11) or "IDLE"
        is_ignored = bool(index.data(Qt.ItemDataRole.UserRole + 22))
        is_enabled = bool(index.data(Qt.ItemDataRole.UserRole + 5))
        if is_enabled is None:
            is_enabled = True

        # Row background — dimmed for ignored ("Done") or disabled projects
        if is_ignored or not is_enabled:
            bg = QColor(PALETTE["borderDark"]) if not is_selected else QColor(PALETTE["selection"])
        else:
            bg = QColor(PALETTE["selection"]) if is_selected else QColor(PALETTE["surface"])
        painter.fillRect(rect, bg)

        # Bottom separator
        painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
        painter.drawLine(rect.left(), rect.bottom() - 1, rect.right() - 1, rect.bottom() - 1)

        is_archive_ignored = bool(index.data(Qt.ItemDataRole.UserRole + 24))
        x = rect.left() + 2
        y = rect.top()
        h = rect.height()

        # 0. Enabled [E] — 14px, green when enabled, grey striked when disabled (ZIP packing gate)
        enabled_rect = QRect(x, y, 14, h)
        if is_enabled:
            painter.setFont(self.font_mono)
            painter.setPen(QColor(PALETTE["success"]))
            painter.drawText(enabled_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, "E")
        else:
            cb_e = QRect(x + 2, y + (h - 10) // 2, 10, 10)
            painter.setPen(QPen(QColor(PALETTE["dangerText"]), 1))
            painter.drawRect(cb_e)
            painter.setFont(self.font_tiny)
            painter.setPen(QColor(PALETTE["dangerText"]))
            painter.drawText(cb_e, Qt.AlignmentFlag.AlignCenter, "×")
        x += 14
        # 0a. Done checkbox [✓] — 14px wide, clickable
        done_rect = QRect(x, y, 14, h)
        if is_ignored:
            painter.setFont(self.font_mono)
            painter.setPen(QColor(PALETTE["success"]))
            painter.drawText(done_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, "\u2713")
        else:
            cb_rect = QRect(x + 2, y + (h - 10) // 2, 10, 10)
            painter.setPen(QPen(QColor(PALETTE["textMuted"]), 1))
            painter.drawRect(cb_rect)
        x += 14
        # 0b. Archive ignore [A] — 14px wide, clickable, tooltip via ⓘ popup
        arch_rect = QRect(x, y, 14, h)
        if is_archive_ignored:
            painter.setFont(self.font_mono)
            painter.setPen(QColor(PALETTE["warning"]))
            painter.drawText(arch_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, "A")
        else:
            cb2 = QRect(x + 2, y + (h - 10) // 2, 10, 10)
            painter.setPen(QPen(QColor(PALETTE["textMuted"]), 1))
            painter.drawRect(cb2)
            # small A hint inside empty box
            painter.setFont(self.font_tiny)
            painter.setPen(QColor(PALETTE["textMuted"]))
            painter.drawText(cb2, Qt.AlignmentFlag.AlignCenter, "A")
        x += 16

        # 1. Slot badge [1..10] — width from the font actually painting it, so
        # two-digit slots and DPI scaling never spill onto the project name.
        slot_badge = f"[{slot_num}]"
        painter.setFont(self.font_mono)
        name_color = QColor(PALETTE["textMuted"]) if is_ignored else QColor(PALETTE["textSecondary"])
        painter.setPen(name_color)
        slot_w = slot_badge_width(painter.fontMetrics())
        painter.drawText(QRect(x, y, slot_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, slot_badge)
        x += slot_w + 2

        if is_empty:
            painter.setFont(self.font_small)
            painter.setPen(QColor(PALETTE["textMuted"]))
            empty_text = "[ EMPTY — DROP ]"
            empty_w = rect.right() - x - 4
            painter.drawText(
                QRect(x, y, max(0, empty_w), h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                painter.fontMetrics().elidedText(empty_text, Qt.TextElideMode.ElideRight, empty_w),
            )
            painter.restore()
            return

        # 2. Compute right-side button area first
        launchers = getattr(self._config, "launchers", None) if self._config else None
        launcher_buttons, gg_rect = compute_row_button_rects(rect, launchers)
        info_rect = compute_info_button_rect(rect, launcher_buttons, gg_rect)
        plus_rect, edit_rect = compute_layer_button_rects(rect, info_rect)

        total_waves = index.data(Qt.ItemDataRole.UserRole + 15) or 3
        prof_label = index.data(Qt.ItemDataRole.UserRole + 19) or ("A10" if total_waves == 10 else "A3")
        dispatch_state = str(index.data(Qt.ItemDataRole.UserRole + 33) or "")
        inaudit_label = str(index.data(Qt.ItemDataRole.UserRole + 40) or "")
        audit_run_state = str(index.data(Qt.ItemDataRole.UserRole + 41) or "")

        # ── Vertical indicator column (fixed width, stacked top→bottom) ────────
        # Every project row uses the SAME column layout: consistent order and
        # alignment, so the eye scans down one column instead of hunting badges.
        # The column is anchored at a fixed distance from the right edge so the
        # left edge is identical on every row regardless of how many launcher
        # buttons a row actually has. This fixes the "rows crawling in different
        # directions" visual defect.
        compact_rows = bool(getattr(getattr(self._config, "ui", None), "compact_rows", False))
        # Compact rows right-anchor a measured token rail at the REAL action
        # block; full mode keeps its aligned fixed sub-column grid.
        actions_left = compute_actions_left(rect, launchers)
        col_w = FULL_STATE_WIDTH if not compact_rows else 0
        # A too-narrow row cannot hold name + state grid + button block side by
        # side: painting them anyway just stacks text on text. Skip the state
        # grid entirely and let the elided name own the whole middle.
        cramped = rect.width() < full_row_min_width(launchers)
        col_x = rect.right() - actions_block_width(launchers) - 4 - col_w

        # Data used by the column
        arc_data = index.data(Qt.ItemDataRole.UserRole + 18)
        sync_status = index.data(Qt.ItemDataRole.UserRole + 20) or "SYNCED"
        arc_exists, arc_size, arc_created, _arc_path = arc_data if arc_data else (False, "", "", None)
        audit_age_str = index.data(Qt.ItemDataRole.UserRole + 17) or ""
        temp_val = temperature.value if hasattr(temperature, "value") else str(temperature)
        arc_temp = index.data(Qt.ItemDataRole.UserRole + 23)  # archive_temperature
        arc_temp_val = arc_temp.value if hasattr(arc_temp, "value") else str(arc_temp) if arc_temp else ""
        pack_progress = index.data(Qt.ItemDataRole.UserRole + 26) or None
        pack_percent = index.data(Qt.ItemDataRole.UserRole + 27)
        archive_fresh_short = index.data(Qt.ItemDataRole.UserRole + 31) or "none"
        archive_freshness = str(index.data(Qt.ItemDataRole.UserRole + 30) or ArchiveFreshness.UNKNOWN.value)
        archive_age_str = index.data(Qt.ItemDataRole.UserRole + 44) or ""

        TC = TEMP_COLORS

        def _line_top(idx: int) -> int:
            return base_y + idx * line_h

        painter.setFont(self.font_small)
        line_h = 18
        base_y = y + 4

        # ── INAUDIT badge (kept tiny, only when layers exist)
        # ── Audit run/state label — SHORT, one column only (RUN).
        # Every state is a compact token so the column never overflows and the
        # rows stay aligned like a table. WAVES and AGE have their own columns.
        if audit_run_state:
            state_labels = {
                "PREPARING": "PREP",
                "INTERRUPTED": "INT",
                "WAITING": "WAIT",
                "RETRYING": "RETRY",
                "ATTACHING": "ATCH",
                "STARTING": "START",
                "AUDITING": "AUDIT",
                "SAVING": "SAVE",
                "READY": "READY",
                "BLOCKED_PRE_START": "!PRE",
                "BLOCKED_POST_START": "!POST",
                "RECOVERY": "RECOV",
                "FAILED": "FAIL",
                "CANCELLED": "CANC",
                "SUPERSEDED": "OLD",
            }
            wave_text = state_labels.get(audit_run_state, audit_run_state[:6])
            if audit_run_state == "READY":
                wave_color = QColor(PALETTE["success"])
            elif audit_run_state in {"FAILED", "BLOCKED_PRE_START", "BLOCKED_POST_START", "RECOVERY"}:
                wave_color = QColor(PALETTE["dangerText"])
            else:
                wave_color = QColor(PALETTE["warning"])
        elif dispatch_state and dispatch_state not in {"COMPLETE", "CANCELLED"}:
            state_labels = {
                "QUEUED": "WAIT",
                "LEASED": "ATCH",
                "ARTIFACT_FETCHED": "ATCH",
                "ATTACHED": "START",
                "START_PREPARED": "START",
                "STARTED": "AUDIT",
                "AUDITING": "AUDIT",
                "FINALIZING": "SAVE",
                "BLOCKED": "!BLOCK",
                "FAILED": "FAIL",
                "RETRYABLE": "WAIT",
            }
            wave_text = state_labels.get(dispatch_state, dispatch_state[:6])
            wave_color = QColor(PALETTE["warning"] if dispatch_state not in {"BLOCKED", "FAILED"} else PALETTE["dangerText"])
        else:
            # Idle: show profile readiness, still one short token.
            if all_ready:
                wave_text = "READY"
                wave_color = QColor(PALETTE["success"])
            else:
                wave_text = prof_label  # A3 / A10
                wave_color = QColor(PALETTE["textMuted"])
        if compact_rows:
            wave_text = wave_text.replace("✓ ", "✓", 1)
        # Pack badge after ZIP
        pack_display = ""
        pack_color = QColor(PALETTE["textMuted"])
        pack_progress_text = ""  # e.g. "  [PACK 42% 1.2MB]" only used in single-line branch
        if pack_state and pack_state != "IDLE":
            if pack_state in ("PACKING", "QUEUED"):
                if pack_progress and isinstance(pack_progress, dict):
                    fa = int(pack_progress.get("files_added") or 0)
                    bw = int(pack_progress.get("bytes_written") or 0)
                    pct = int(round(float(pack_percent) if isinstance(pack_percent, (int, float)) else 0))
                    pct = max(0, min(99, pct))
                    size_mb = bw / (1024 * 1024)
                    if size_mb >= 1:
                        size_disp = f"{size_mb:.1f}MB"
                    elif bw > 0:
                        size_disp = f"{max(1, bw // 1024)}KB"
                    else:
                        size_disp = ""
                    label = f"PACK {pct}% {fa}f" + (f" {size_disp}" if size_disp else "")
                else:
                    label = pack_state
                pack_display = f"  [{label}]"
                pack_progress_text = pack_display
                pack_color = QColor(PALETTE["borderGolden"])
            elif pack_state == "COMPLETE":
                # show truncated archive name like reference "[COMPLET v:\__...]"
                arc_name = ""
                try:
                    _arc_path = arc_data[3] if arc_data and len(arc_data) > 3 else None
                    if _arc_path:
                        arc_name = str(_arc_path).split("\\")[-1][:12]
                except Exception:
                    arc_name = ""
                pack_display = f"  [COMPLET {arc_name}]" if arc_name else "  [COMPLET]"
                pack_color = QColor(PALETTE["success"])
            else:
                pack_display = f"  [{pack_state}]"
                pack_color = QColor(PALETTE["danger"])

        # Audit age inline after the wave text
        if audit_age_str:
            audit_color = QColor(TC.get(temp_val, PALETTE["borderMuted"]))
            audit_display = f"  {audit_age_str}"
        else:
            audit_color = QColor(PALETTE["textMuted"])
            audit_display = ""
        if compact_rows:
            audit_display = f" {audit_age_str.replace(' ', '')}" if audit_age_str else ""

        # copy counter
        copy_cnt = int(index.data(Qt.ItemDataRole.UserRole + 25) or 0)
        copy_display = f"  ×{copy_cnt}" if copy_cnt > 0 else ""

        # ── ZIP text — "ZIP: 156,7 MB 28.08 01:12" — size + creation date
        freshness_tag = ""
        # The ZIP cell is 100px; the PACK badge owns what is left of the
        # column. Candidates used to be fitted against the WHOLE column (175px)
        # and then drawn into the 100px cell, so anything between 100 and 175px
        # wide ran straight into the PACK badge's pixels.
        zip_fit_width = dict(FULL_STATE_CELL_WIDTHS)["zip"]
        if arc_exists:
            size_str = str(arc_size).replace(".", ",")  # 156.7 MB → 156,7 MB like screenshot
            arc_color = QColor(TC.get(arc_temp_val, PALETTE["textSecondary"]))
            if sync_status == "OUTDATED" or archive_freshness == ArchiveFreshness.STALE.value:
                arc_color = QColor(PALETTE["dangerText"])
            # Coarse freshness tag at end of ZIP line so the user can see at a glance
            # whether the archive is fresh, stale, or old — and a small [NEW] if
            # the source tree has changed since the last pack.
            if archive_freshness == ArchiveFreshness.STALE.value:
                freshness_tag = "  [SRC\u25B2]"  # an included source file is newer
            elif archive_freshness == ArchiveFreshness.UNKNOWN.value:
                freshness_tag = "  [SRC?]"  # not proven current
            elif archive_fresh_short == "fresh":
                freshness_tag = "  [\u2713]"
            elif archive_fresh_short == "stale":
                freshness_tag = "  [\u00B7]"
            elif archive_fresh_short == "old":
                freshness_tag = "  [!]"
            # The creation stamp says WHEN; the age says how long ago, which is
            # what actually answers "is this archive still worth auditing".
            # Age wins the room when both cannot fit.
            age_part = f" {archive_age_str}" if archive_age_str else ""
            if arc_created:
                candidates = [
                    f"ZIP: {size_str}{age_part} {arc_created}{freshness_tag}",
                    f"ZIP: {size_str}{age_part}{freshness_tag}",
                    f"ZIP: {size_str}{age_part}",
                    f"ZIP: {size_str}",
                ]
            else:
                candidates = [
                    f"ZIP: {size_str}{age_part}{freshness_tag}",
                    f"ZIP: {size_str}{age_part}",
                    f"ZIP: {size_str}",
                ]
            zip_text = fit_zip_text(candidates, painter.fontMetrics().horizontalAdvance, zip_fit_width)
            if compact_rows:
                # Compact mode keeps the size in the ZIP cell and gives age and
                # freshness their own cell -- packing borrows the ZIP cell and a
                # COMPLETE badge never leaves it, so anything sharing that cell
                # is invisible on a packed project, which is most of them.
                zip_text = f"ZIP {size_str.replace(' ', '')}"
        else:
            arc_color = QColor(PALETTE["textMuted"])
            zip_text = "ZIP \u2014" if compact_rows else "\u2014"

        if compact_rows and pack_display:
            if pack_state in ("PACKING", "QUEUED"):
                pct = int(round(float(pack_percent))) if isinstance(pack_percent, (int, float)) else 0
                pack_display = f" [{pct}%]"
            elif pack_state == "COMPLETE":
                pack_display = " [OK]"
            else:
                pack_display = " [!]"

        # Compact mode keeps a single row (22px), but the fields are still
        # COLUMNS, not one concatenated string. Concatenation made every field
        # start wherever the previous one happened to end, so nothing lined up
        # down the list and the eye had to re-find each value on every row.
        if cramped:
            painter.setFont(self.font_bold)
            painter.setPen(QColor(PALETTE["textPrimary"] if not is_ignored else PALETTE["textMuted"]))
            available = rect.right() - x - 4
            painter.drawText(
                QRect(x, y, available, h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                painter.fontMetrics().elidedText(str(display_name), Qt.TextElideMode.ElideRight, available),
            )
            painter.restore()
            return
        if compact_rows:
            painter.setFont(self.font_small)
            single_y = y + (h - line_h) // 2
            fm = painter.fontMetrics()
            # The age token carries the audit age, or the copy counter when
            # there is no age. Packing owns the ZIP token.
            age_cell = (audit_display or "").strip() or (copy_display or "").strip()
            packing = (pack_display or "").strip()

            # ── Ordered semantic tokens (F3), reading order RUN WAVES AGE ZIP ARC.
            # Empty values are simply absent: zero width, no holes. Packing
            # owns the ZIP token and a COMPLETE badge never leaves it, so the
            # archive size yields to the pack badge exactly as before.
            tokens = compact_state_tokens(
                wave_text,
                f"{completed_waves}/{total_waves}" if total_waves else "",
                age_cell,
                zip_text,
                packing,
                compact_archive_cell(
                    arc_exists, archive_age_str, archive_fresh_short, archive_freshness,
                ),
            )

            token_rects, name_right = compact_fit_tokens(
                fm,
                tokens,
                actions_left,
                x,
                name_text=str(display_name),
                # The IA badge rides inside the name's right boundary (F5/F7).
                name_extra=painter.fontMetrics().horizontalAdvance(f" {inaudit_label}")
                if inaudit_label
                else 0,
            )

            def _draw_token(token_x: int, text: str, color) -> None:
                if not text:
                    return
                painter.setPen(color)
                painter.drawText(
                    QRect(token_x, single_y, fm.horizontalAdvance(text) + 2, line_h),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    text,
                )

            run_color = wave_color
            wav_color = QColor(PALETTE["success"]) if all_ready else QColor(
                PALETTE["warning"] if completed_waves > 0 else PALETTE["textMuted"]
            )
            colors = {
                "run": run_color,
                "waves": wav_color,
                "age": audit_color if (audit_display or "").strip() else QColor(PALETTE["textMuted"]),
                "zip": pack_color if packing else arc_color,
                "arc": arc_color,
            }
            token_map = dict(tokens)
            for key, token_rect in token_rects:
                _draw_token(token_rect.left(), token_map[key], colors[key])
            # The name rectangle ends at the rail's left edge; the IA badge
            # reservation happens inside the name's own box below (F5/F7).
            col_x = name_right
        else:
            # Full mode: each state field is a fixed-width column so every row
            # aligns vertically -- RUN | WAVES | AGE on line 0, ZIP | PACK on
            # line 1.  No concatenated text, no dynamic x offset.
            painter.setFont(self.font_small)
            # Column boundaries (offsets from col_x)
            RUN_W = 64
            WAV_W = 42
            AGE_W = 62
            ZIP_W = 100
            PK_W = 60
            run_x = col_x
            wav_x = run_x + RUN_W
            age_x = wav_x + WAV_W
            zip_x = col_x
            pk_x = zip_x + ZIP_W

            def _draw_col(x, w, text, color):
                painter.setPen(color)
                painter.drawText(QRect(x, _line_top(0), w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

            def _draw_col1(x, w, text, color):
                painter.setPen(color)
                painter.drawText(QRect(x, _line_top(1), w, line_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

            # Line 0: RUN state | WAVES | AGE
            _draw_col(run_x, RUN_W, wave_text, wave_color)
            wav_label = f"{completed_waves}/{total_waves}"
            wav_color = QColor(PALETTE["success"]) if all_ready else QColor(PALETTE["warning"] if completed_waves > 0 else PALETTE["textMuted"])
            _draw_col(wav_x, WAV_W, wav_label, wav_color)
            if audit_age_str:
                _draw_col(age_x, AGE_W, audit_age_str, audit_color)
            else:
                _draw_col(age_x, AGE_W, "", QColor(PALETTE["textMuted"]))
            # The INAUDIT count is NOT drawn here: the name line already carries
            # the golden IA badge, and an extra one at age_x + AGE_W extended
            # past the state column's 175px onto the fixed [edit] button.

            # Line 1: ZIP | PACK
            if pack_state in ("PACKING", "QUEUED") and isinstance(pack_progress, dict):
                pct = float(pack_percent) if isinstance(pack_percent, (int, float)) else 0.0
                pct = max(0.0, min(99.0, pct))
                bar_rect = QRect(zip_x, _line_top(1) + (line_h - 8) // 2, col_w, 8)
                painter.fillRect(bar_rect, QColor(PALETTE["borderDark"]))
                fill_w = int(round(bar_rect.width() * pct / 100.0))
                if fill_w > 0:
                    painter.fillRect(QRect(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height()), QColor(PALETTE["borderGolden"]))
                painter.setPen(QColor(PALETTE["textPrimary"]))
                painter.drawText(bar_rect, Qt.AlignmentFlag.AlignCenter, pack_progress_text.strip())
            else:
                _draw_col1(zip_x, ZIP_W, zip_text, arc_color)
                if pack_display:
                    _draw_col1(pk_x, PK_W, pack_display, pack_color)

        # 3. Project Name — fill the left area, vertically centered
        painter.setFont(self.font_bold)
        main_window = self.parent().window() if self.parent() is not None else None
        project_id = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")
        instance_count = 0
        if main_window is not None and hasattr(main_window, "_instance_monitor"):
            instance_count = len(main_window._instance_monitor.for_project(project_id))
        instance_prefix = f"▶{instance_count} " if instance_count else ""
        prefix_width = painter.fontMetrics().horizontalAdvance(instance_prefix)
        if instance_prefix:
            painter.setPen(QColor(PALETTE["success"]))
            painter.drawText(
                QRect(x, y, prefix_width, h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                instance_prefix,
            )
        if is_ignored:
            painter.setPen(QColor(PALETTE["textMuted"]))
        else:
            painter.setPen(QColor(PALETTE["textPrimary"]))
        name_x = x + prefix_width
        # The name owns every pixel between its left edge and the state area
        # (compact: the token rail's left edge; full: the fixed column), minus
        # the IA badge's real measured width so the name elides before the two
        # ever collide (F7). No artificial floor: a floor wider than the real
        # gap is what used to run the name over the state column.
        name_available = col_x - name_x - 8
        ia_suffix_w = painter.fontMetrics().horizontalAdvance(f" {inaudit_label}") if inaudit_label else 0
        name_width = max(0, name_available - ia_suffix_w)
        elided_name = painter.fontMetrics().elidedText(str(display_name), Qt.TextElideMode.ElideRight, name_width)
        painter.drawText(QRect(name_x, y, name_width, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)
        # IA badge right after the name — always visible, golden. Pinned to the
        # right edge of the name's own box, never past it: the old
        # name_x + width(elided_name) anchored slid a rounded advance past the
        # reserved width and onto the state column.
        if inaudit_label:
            painter.setFont(self.font_small)
            painter.setPen(QColor(PALETTE["borderGolden"]))
            painter.drawText(
                QRect(name_x + name_width, y, ia_suffix_w + 4, h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f" {inaudit_label}",
            )

        # 7. Draw launcher buttons — the launcher's canonical short_label
        # (SRC-081 TARGET I): one label source, zero per-surface id maps.
        # Numeric mode is the same source with Settings rewriting short_label
        # to the positional number.
        painter.setFont(self.font_tiny)
        # T-179: which launcher is already live for THIS project. Pure in-memory
        # read of the last instance scan (same source the row's instance prefix
        # already uses), so the paint path stays free of native/filesystem work.
        launcher_states = (
            main_window._instance_monitor.launcher_states(project_id)
            if main_window is not None and hasattr(main_window, "_instance_monitor")
            else {}
        )
        for launcher, b_rect in launcher_buttons:
            block_reason = (
                main_window._launcher_block_reason(launcher.id)
                if main_window is not None and hasattr(main_window, "_launcher_block_reason")
                else ""
            )
            run_state = launcher_states.get(launcher.id, "")
            lbl = launcher_button_label(launcher)
            # The RECT never changes -- a running agent must not move the
            # buttons or steal a pixel from the project name (T-173). Only the
            # surface, border and label colour carry the state.
            surface = {
                "running": PALETTE["accentTealDeep"],
                "starting": PALETTE["warning"],
            }.get(run_state, PALETTE["surfaceRaised"])
            painter.fillRect(b_rect, QColor(surface))
            painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
            painter.drawLine(b_rect.left(), b_rect.top(), b_rect.right() - 1, b_rect.top())
            painter.drawLine(b_rect.left(), b_rect.top(), b_rect.left(), b_rect.bottom() - 1)
            painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
            painter.drawLine(b_rect.left(), b_rect.bottom() - 1, b_rect.right() - 1, b_rect.bottom() - 1)
            painter.drawLine(b_rect.right() - 1, b_rect.top(), b_rect.right() - 1, b_rect.bottom() - 1)
            if run_state == "running":
                painter.setPen(QPen(QColor(PALETTE["success"]), 1))
                painter.drawLine(
                    b_rect.left() + 1, b_rect.bottom() - 2,
                    b_rect.right() - 2, b_rect.bottom() - 2,
                )

            if block_reason:
                label_color = PALETTE["textMuted"]
            elif run_state == "running":
                label_color = PALETTE["borderHighlight"]
            elif run_state == "starting":
                label_color = PALETTE["textPrimary"]
            else:
                label_color = PALETTE["borderGolden"]
            painter.setPen(QColor(label_color))
            painter.drawText(b_rect, Qt.AlignmentFlag.AlignCenter, lbl)
            limit_entry = (
                getattr(main_window, "_limit_snapshot_by_launcher", {}).get(launcher.id)
                if main_window is not None else None
            )
            if limit_entry is not None:
                snapshot = limit_entry[1]
                state = snapshot.availability().value if snapshot is not None else "UNKNOWN"
                windows = snapshot.relevant_windows() if snapshot is not None else ()
                for line, kind in enumerate(("five_hour", "weekly")):
                    meter = next((w for w in windows if w.kind == kind), None)
                    ratio = meter.remaining_ratio if meter else None
                    bar = QRect(b_rect.left() + 2, b_rect.bottom() - 4 + line,
                                max(1, b_rect.width() - 4), 1)
                    painter.fillRect(bar, QColor(PALETTE["borderMuted"]))
                    if ratio is not None and state not in ("STALE", "ERROR"):
                        color = ("danger" if ratio == 0 else "warning" if ratio <= 0.1
                                 else "success" if ratio > 0.3 else "borderGolden")
                        width = max(1, int(round(bar.width() * ratio))) if ratio > 0 else 0
                        if width:
                            painter.fillRect(QRect(bar.left(), bar.top(), width, 1), QColor(PALETTE[color]))
                    elif state == "STALE":
                        painter.setPen(QPen(QColor(PALETTE["textMuted"]), 1, Qt.PenStyle.DashLine))
                        painter.drawLine(bar.left(), bar.top(), bar.right(), bar.top())
                if state == "ERROR":
                    painter.setPen(QColor(PALETTE["dangerText"]))
                    painter.drawText(b_rect.adjusted(0, 0, -1, 0),
                                     Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, "!")
            if block_reason:
                painter.setFont(self.font_tiny)
                painter.setPen(QColor(PALETTE["dangerText"]))
                painter.drawText(
                    b_rect.adjusted(0, -2, -1, 0),
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                    "×",
                )

        # 7b. Draw [ⓘ] info button — replaces hover tooltip
        painter.fillRect(info_rect, QColor(PALETTE["surfaceRaised"]))
        painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
        painter.drawLine(info_rect.left(), info_rect.top(), info_rect.right() - 1, info_rect.top())
        painter.drawLine(info_rect.left(), info_rect.top(), info_rect.left(), info_rect.bottom() - 1)
        painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
        painter.drawLine(info_rect.left(), info_rect.bottom() - 1, info_rect.right() - 1, info_rect.bottom() - 1)
        painter.drawLine(info_rect.right() - 1, info_rect.top(), info_rect.right() - 1, info_rect.bottom() - 1)
        painter.setFont(self.font_bold)
        painter.setPen(QColor(PALETTE["borderGolden"]))
        painter.drawText(info_rect, Qt.AlignmentFlag.AlignCenter, "\u24D8")

        # 7c. Draw [+] / [edit] audit-layer buttons (T-161)
        for layer_rect, glyph in ((plus_rect, "+"), (edit_rect, "e")):
            painter.fillRect(layer_rect, QColor(PALETTE["surfaceRaised"]))
            painter.setPen(QPen(QColor(PALETTE["bevelLight"]), 1))
            painter.drawLine(layer_rect.left(), layer_rect.top(), layer_rect.right() - 1, layer_rect.top())
            painter.drawLine(layer_rect.left(), layer_rect.top(), layer_rect.left(), layer_rect.bottom() - 1)
            painter.setPen(QPen(QColor(PALETTE["borderDark"]), 1))
            painter.drawLine(layer_rect.left(), layer_rect.bottom() - 1, layer_rect.right() - 1, layer_rect.bottom() - 1)
            painter.drawLine(layer_rect.right() - 1, layer_rect.top(), layer_rect.right() - 1, layer_rect.bottom() - 1)
            painter.setFont(self.font_bold)
            painter.setPen(QColor(PALETTE["borderGolden"]))
            painter.drawText(layer_rect, Qt.AlignmentFlag.AlignCenter, glyph)

        painter.restore()

    @staticmethod
    def build_tooltip(hover_info: dict) -> str:
        """Compact-rich tooltip — all essential info, structured but not verbose."""
        proj = hover_info.get("project")
        snap = hover_info.get("snapshot")
        arc_data = hover_info.get("archive_info")
        pack_state = hover_info.get("pack_state", "IDLE")
        pack_msg = hover_info.get("pack_message", "")
        group = hover_info.get("group", "")
        slot = hover_info.get("slot", 0)
        group_count = hover_info.get("group_count", 0)

        if not proj:
            return f"<b>[{group} #{slot}]</b> — empty slot"

        # Temperature color helper
        TC = TEMP_COLORS

        lines = [f"<b>{proj.display_name}</b>  [{group} #{slot}]  ({group_count} in group)"]

        # Source (truncated)
        if proj.source_path:
            sp = proj.source_path
            if len(sp) > 50:
                sp = "..." + sp[-47:]
            lines.append(f"<font color='#999988'>{sp}</font>")

        # Status flags
        flags = []
        if not proj.enabled:
            flags.append("DISABLED")
        if getattr(proj, "ignored", False):
            flags.append("Done")
        if getattr(proj, "ignore_archive", False):
            flags.append("Ignore to archive")
        if flags:
            lines.append(f"<font color='#FF8866'>{' / '.join(flags)}</font>")

        # T-179: which agents are live for THIS project, and what a click does.
        # The Project column in the Instances tab still owns the full detail;
        # this is the at-a-glance answer next to the buttons themselves.
        agents = hover_info.get("launcher_instances") or []
        if agents:
            lines.append("")
            lines.append("<b>Agents</b>")
            for agent in agents:
                name = str(agent.get("launcher_name") or agent.get("launcher_id") or "?")
                state = str(agent.get("state") or "")
                pid = agent.get("pid")
                origin = "AUDAPACK" if agent.get("tracked") else "external"
                colour = "#55FF55" if state == "running" else "#FFD700"
                pid_txt = f" PID {pid}" if pid else ""
                lines.append(
                    f"  <font color='{colour}'>{name}: {state}</font>"
                    f"{pid_txt} <font color='#999988'>({origin})</font>"
                )
            lines.append(
                "  <font color='#999988'>Click a launcher button: focus this project's "
                "instance · Shift+click: another instance (max_instances still applies)</font>"
            )

        # Audit section — structured but compact
        if snap:
            prof = getattr(snap, "audit_profile_id", "quick3") or "quick3"
            prof_label = f"{profile_short_label(prof)} / {get_profile(prof).display_name}" if prof else "A3 / Quick 3 Waves"
            waves = getattr(snap, "completed_waves", 0)
            total = getattr(snap, "total_waves", 3)
            temp = getattr(snap, "temperature", AuditTemperature.NONE)
            temp_val = temp.value if hasattr(temp, "value") else str(temp)
            age_str = format_age_str(getattr(snap, "audit_age_seconds", None))
            tc = TC.get(temp_val, "#999999")

            # Status line with color
            if getattr(snap, "all3_ready", False) or getattr(snap, "final_handoff_ready", False):
                status_txt = "<font color='#55FF55'>✓ ALL WAVES COMPLETE</font>"
            elif waves > 0:
                status_txt = f"<font color='#FFD700'>In progress: {waves}/{total} waves</font>"
            else:
                status_txt = "<font color='#999999'>No audit data</font>"

            lines.append("")
            lines.append(f"<b>Audit</b> ({prof_label})")
            lines.append(f"  Status: {status_txt}")
            lines.append(f"  Waves: {waves}/{total}  |  <font color='{tc}'>Temp: {temp_val}</font>  |  Age: {age_str}")

            # Wave detail — compact inline
            wave_statuses = getattr(snap, "wave_statuses", None) or {}
            if wave_statuses:
                wave_names = {"core": "Core", "second": "Second", "performance": "Perf", "all": "ALL3"}
                detail_parts = []
                for wk, wv in wave_statuses.items():
                    label = wave_names.get(wk, wk)
                    if isinstance(wv, dict):
                        ws = wv.get("status", "?")
                    else:
                        ws = str(wv)
                    # Color status
                    if "COMPLETE" in ws.upper():
                        detail_parts.append(f"<font color='#55FF55'>{label}✓</font>")
                    elif "IDLE" in ws.upper():
                        detail_parts.append(f"<font color='#777766'>{label}</font>")
                    else:
                        detail_parts.append(f"<font color='#FFD700'>{label}</font>")
                if detail_parts:
                    lines.append(f"  {' | '.join(detail_parts)}")

            if getattr(snap, "campaign_run_id", None):
                run_id = snap.campaign_run_id[:16]
                lines.append(f"  <font color='#777766'>Run: {run_id}</font>")
        else:
            lines.append("")
            lines.append("<b>Audit</b>: <font color='#999999'>No data loaded</font>")

        # Archive section — compact
        arc_exists, arc_size, arc_created, arc_path = arc_data if arc_data else (False, "", "", None)
        lines.append("")
        if arc_exists:
            sync_status = hover_info.get("archive_sync_status", "SYNCED")
            sync_txt = '  <font color="#FF5555">⚠ OUTDATED</font>' if sync_status == "OUTDATED" else ""
            # Archive freshness color
            arc_temp_val = ""
            if snap:
                arc_temp = getattr(snap, "archive_temperature", None)
                if arc_temp:
                    arc_temp_val = arc_temp.value if hasattr(arc_temp, "value") else str(arc_temp)
            arc_tc = TC.get(arc_temp_val, "#999999")
            created_txt = f" created {arc_created}" if arc_created else ""
            fresh_short = hover_info.get("archive_freshness_short", "none")
            fresh_txt = {"fresh": "[✓ fresh]", "stale": "[· stale]", "old": "[! old]"}.get(fresh_short, "")
            src_state = str(hover_info.get("archive_freshness") or ArchiveFreshness.UNKNOWN.value)
            if src_state == ArchiveFreshness.STALE.value:
                src_txt = '  <font color="#FFAA55">⏫ SOURCE CHANGED AFTER PACK</font>'
            elif src_state == ArchiveFreshness.UNKNOWN.value:
                src_txt = '  <font color="#999999">[? freshness unknown]</font>'
            else:
                src_txt = ""
            lines.append(f"<b>Archive</b>: {arc_size}{created_txt} <font color='{arc_tc}'>[{arc_temp_val}]</font>{fresh_txt}{sync_txt}{src_txt}")
            if arc_path:
                ap = str(arc_path)
                if len(ap) > 60:
                    ap = "..." + ap[-57:]
                lines.append(f"  <font color='#777766'>{ap}</font>")
        else:
            lines.append("<b>Archive</b>: <font color='#999999'>Not packed yet</font>")

        dispatch = hover_info.get("dispatch") or {}
        dispatch_state = str(dispatch.get("state") or "")
        if dispatch_state and dispatch_state not in {"COMPLETE", "CANCELLED"}:
            dispatch_browser = str(dispatch.get("friendly_worker_label") or dispatch.get("browser_name") or dispatch.get("assigned_worker_id") or "")
            dispatch_error = str(dispatch.get("error") or dispatch.get("last_error_code") or "")
            dispatch_expected = str(dispatch.get("archive_filename") or "")
            dispatch_updated = str(dispatch.get("updated_at") or "")
            lines.append("")
            lines.append(f"<b>Dispatch</b>: {dispatch_state}" + (f"  <font color='#777766'>{dispatch_browser}</font>" if dispatch_browser and dispatch_state not in {"QUEUED", "RETRYABLE"} else ""))
            if dispatch_expected:
                lines.append(f"  Expected: <font color='#D4C89A'>{dispatch_expected}</font>")
            if dispatch_browser or dispatch.get("assigned_worker_id"):
                raw_wid = str(dispatch.get("assigned_worker_id") or dispatch_browser)[:32]
                lines.append(f"  Worker: {dispatch_browser or raw_wid}" + (f" <font color='#777766'>{raw_wid}</font>" if dispatch_browser and raw_wid != dispatch_browser else ""))
            if dispatch_state:
                lines.append(f"  State: {dispatch_state}")
            if dispatch_error:
                lines.append(f"  <font color='#FF8866'>Error: {dispatch_error[:160]}</font>")
            if dispatch_state == "BLOCKED":
                # A BLOCKED run with no readable next step is the whole
                # complaint: the operator sees a red badge and nothing else.
                from audapack.services.audit_run_service import blocked_guidance
                post_start = bool(
                    str(dispatch.get("recovery_state") or "") in {"START_PREPARED", "STARTED", "AUDITING", "FINALIZING"}
                    or dispatch.get("start_receipt")
                    or dispatch.get("campaign_run_id")
                )
                why, action = blocked_guidance(dispatch_error, post_start)
                lines.append(f"  <font color='#D4C89A'>Why: {why}</font>")
                lines.append(f"  <font color='#FFD700'>Next: {action}</font>")
            if dispatch_updated:
                try:
                    from datetime import datetime as _dt
                    upd = float(dispatch_updated)
                    when = _dt.fromtimestamp(upd).strftime("%H:%M:%S")
                except Exception:
                    when = dispatch_updated[:19]
                lines.append(f"  <font color='#777766'>Updated: {when}</font>")

        # Pack state
        if pack_state and pack_state != "IDLE":
            lines.append(f"<b>Pack</b>: <font color='#D4A840'>{pack_state}</font>")
            if pack_msg:
                lines.append(f"  <font color='#777766'>{pack_msg}</font>")

        layers = hover_info.get("inaudit_layers") or []
        sel = hover_info.get("inaudit_selected")
        if layers:
            lines.append("")
            lines.append(f"<b>INAUDIT</b>  {len(layers)} layer(s)" + (f" — selected {sel}.md" if sel else ""))
            for lay in layers[:8]:
                mark = " ◀" if lay.number == sel else ""
                empty = " — EMPTY" if lay.size_bytes == 0 else ""
                lines.append(f"  [{lay.number}] {lay.number}.md — {lay.size_str}{empty}{mark}")
            if len(layers) > 8:
                lines.append(f"  <font color='#777766'>+{len(layers)-8} more</font>")

        # Copy counter
        cc = int(getattr(proj, "audit_copy_count", 0) or 0)
        if cc > 0:
            last_at = getattr(proj, "last_copied_at", "") or ""
            try:
                from datetime import datetime

                # show short time
                dt = datetime.fromisoformat(last_at.replace("Z", "+00:00")) if last_at else None
                when = dt.strftime("%d.%m %H:%M") if dt else last_at[:16]
            except Exception:
                when = last_at[:16]
            lines.append("")
            lines.append(f"<b>Copied</b>: <font color='#D4A840'>×{cc}</font> <font color='#777766'>last {when}</font> — resets on fresh audit or manual reset")

        lines.append("")
        lines.append("<font color='#777766'>Enter: folder | Del: remove | Drag: reorder | ⓘ: info</font>")

        return "<br>".join(lines)
