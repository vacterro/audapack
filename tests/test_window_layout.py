"""Six worker windows land where the operator said, not where Windows felt like.

The geometry is pure and tested without a screen; only the two calls that talk
to Win32 (enumerate displays, move a window) are platform-bound, and both are
no-ops that say so anywhere else.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from audapack.window_layout import (
    LAYOUT_CASCADE,
    LAYOUT_GRID,
    Monitor,
    arrange_windows,
    cascade_geometry,
    find_profile_windows,
    grid_shape,
    layout_geometry,
    resolve_monitor,
    tile_geometry,
)

SCREEN = Monitor(index=0, name="d1", x=0, y=0, width=1920, height=1040, primary=True)
SECOND = Monitor(index=1, name="d2", x=1920, y=0, width=2560, height=1400)


class TestGrid(unittest.TestCase):
    def test_six_windows_become_three_by_two(self):
        """Which is the arrangement in the screenshot the ask came with."""
        self.assertEqual(grid_shape(6), (3, 2))

    def test_the_shape_stays_wide_rather_than_leaving_holes(self):
        self.assertEqual(grid_shape(1), (1, 1))
        self.assertEqual(grid_shape(2), (2, 1))
        self.assertEqual(grid_shape(3), (3, 1))
        self.assertEqual(grid_shape(4), (2, 2))
        self.assertEqual(grid_shape(0), (0, 0))

    def test_the_tiling_covers_the_monitor_with_no_gaps_or_overlap(self):
        places = tile_geometry(6, SCREEN)
        self.assertEqual(len(places), 6)
        covered = sum(w * h for _x, _y, w, h in places)
        self.assertEqual(covered, SCREEN.width * SCREEN.height)
        right = max(x + w for x, _y, w, _h in places)
        bottom = max(y + h for _x, y, _w, h in places)
        self.assertEqual(right, SCREEN.x + SCREEN.width)
        self.assertEqual(bottom, SCREEN.y + SCREEN.height)

    def test_a_tiling_is_offset_onto_the_monitor_it_was_asked_for(self):
        places = tile_geometry(6, SECOND)
        self.assertTrue(all(x >= SECOND.x for x, _y, _w, _h in places))

    def test_an_odd_count_still_fills_the_row_it_reaches(self):
        """Five windows in a 3x2 leave one hole, not five ragged sizes."""
        places = tile_geometry(5, SCREEN)
        self.assertEqual(len(places), 5)
        self.assertEqual(len({(w, h) for _x, _y, w, h in places[:3]}), 1)


class TestCascade(unittest.TestCase):
    def test_every_window_lands_on_the_monitor(self):
        places = cascade_geometry(6, SCREEN)
        for x, y, w, h in places:
            self.assertGreaterEqual(x, SCREEN.x)
            self.assertGreaterEqual(y, SCREEN.y)
            self.assertLessEqual(x + w, SCREEN.x + SCREEN.width)
            self.assertLessEqual(y + h, SCREEN.y + SCREEN.height)

    def test_the_step_shrinks_before_the_stack_walks_off_the_corner(self):
        many = cascade_geometry(30, SCREEN)
        self.assertLessEqual(many[-1][0] + many[-1][2], SCREEN.x + SCREEN.width)

    def test_each_window_is_offset_from_the_one_before_it(self):
        places = cascade_geometry(4, SCREEN)
        for earlier, later in zip(places, places[1:], strict=False):
            self.assertGreater(later[0], earlier[0])
            self.assertGreater(later[1], earlier[1])

    def test_the_layout_name_picks_the_geometry(self):
        self.assertEqual(layout_geometry(LAYOUT_GRID, 6, SCREEN), tile_geometry(6, SCREEN))
        self.assertEqual(layout_geometry(LAYOUT_CASCADE, 6, SCREEN), cascade_geometry(6, SCREEN))
        # An unknown name is the default, not an exception in a GUI callback.
        self.assertEqual(layout_geometry("nonsense", 6, SCREEN), tile_geometry(6, SCREEN))


class TestMonitorChoice(unittest.TestCase):
    def test_the_configured_display_wins(self):
        self.assertIs(resolve_monitor([SCREEN, SECOND], 1), SECOND)

    def test_an_unplugged_display_falls_back_to_the_primary(self):
        """Or six windows go to coordinates nobody can see."""
        self.assertIs(resolve_monitor([SCREEN, SECOND], 7), SCREEN)

    def test_minus_one_means_whichever_is_primary_now(self):
        self.assertIs(resolve_monitor([SECOND, SCREEN], -1), SCREEN)

    def test_no_displays_means_arrange_nothing(self):
        self.assertIsNone(resolve_monitor([], 0))


class FakeWindow:
    def __init__(self, hwnd, command_line):
        self.hwnd = hwnd
        self.command_line = command_line


class FakeBackend:
    def __init__(self, windows):
        self._windows = windows

    def list_windows(self):
        return self._windows


class TestFindingTheWorkerWindows(unittest.TestCase):
    PROFILE = Path(r"C:\Users\x\AppData\Local\AUDAPACK\browser_worker\chromium_profile")

    def test_only_windows_of_the_dedicated_profile_are_touched(self):
        """The operator's own browsing is not ours to move."""
        backend = FakeBackend([
            FakeWindow(1, f'chrome.exe --user-data-dir={self.PROFILE} --new-window'),
            FakeWindow(2, "chrome.exe"),
            FakeWindow(3, r"chrome.exe --user-data-dir=C:\Users\x\AppData\Local\Google\Chrome"),
            FakeWindow(4, f'chrome.exe --user-data-dir={self.PROFILE}'),
        ])
        self.assertEqual(find_profile_windows(self.PROFILE, backend), [1, 4])

    def test_the_match_survives_a_different_case(self):
        backend = FakeBackend([FakeWindow(9, f'CHROME.EXE --user-data-dir={str(self.PROFILE).upper()}')])
        self.assertEqual(find_profile_windows(self.PROFILE, backend), [9])

    def test_an_unreadable_command_line_is_skipped_not_guessed(self):
        backend = FakeBackend([FakeWindow(5, None), FakeWindow(6, "")])
        self.assertEqual(find_profile_windows(self.PROFILE, backend), [])


class TestArrangeIsSafeOffWindows(unittest.TestCase):
    def test_it_moves_nothing_and_says_so(self):
        with patch("audapack.window_layout.sys") as fake_sys:
            fake_sys.platform = "linux"
            self.assertEqual(arrange_windows([1, 2], tile_geometry(2, SCREEN)), 0)


if __name__ == "__main__":
    unittest.main()


class TestArrangeThroughTheManager(unittest.TestCase):
    """The manager is where the setting, the display and the windows meet."""

    def _manager(self):
        from audapack.components.manager import ComponentManager
        from audapack.config import AppConfig

        return ComponentManager(AppConfig())

    def test_the_setting_off_means_nothing_moves(self):
        manager = self._manager()
        manager.config.ui.arrange_worker_windows = False
        with patch("audapack.window_layout.arrange_windows") as mover:
            ok, message = manager.arrange_worker_windows()
        self.assertFalse(ok)
        mover.assert_not_called()
        self.assertIn("switched off", message)

    def test_pressing_arrange_now_overrides_the_setting(self):
        """Pressing it IS the intent; the checkbox is about the automatic runs."""
        manager = self._manager()
        manager.config.ui.arrange_worker_windows = False
        with patch("audapack.window_layout.list_monitors", return_value=[SCREEN]), \
             patch("audapack.window_layout.find_profile_windows", return_value=[11, 22]), \
             patch("audapack.window_layout.arrange_windows", return_value=2) as mover:
            ok, message = manager.arrange_worker_windows(force=True)
        self.assertTrue(ok)
        mover.assert_called_once()
        self.assertIn("2 worker window", message)

    def test_no_readable_display_moves_nothing(self):
        manager = self._manager()
        with patch("audapack.window_layout.list_monitors", return_value=[]), \
             patch("audapack.window_layout.arrange_windows") as mover:
            ok, message = manager.arrange_worker_windows(force=True)
        self.assertFalse(ok)
        mover.assert_not_called()
        self.assertIn("display", message.lower())

    def test_no_open_window_is_an_answer_not_a_failure_to_hide(self):
        manager = self._manager()
        with patch("audapack.window_layout.list_monitors", return_value=[SCREEN]), \
             patch("audapack.window_layout.find_profile_windows", return_value=[]):
            ok, message = manager.arrange_worker_windows(force=True)
        self.assertFalse(ok)
        self.assertIn("nothing to arrange", message)

    def test_the_configured_layout_and_display_are_the_ones_used(self):
        manager = self._manager()
        manager.config.ui.worker_window_layout = LAYOUT_CASCADE
        manager.config.ui.worker_window_monitor = 1
        manager.config.ui.worker_windows_minimized = False
        with patch("audapack.window_layout.list_monitors", return_value=[SCREEN, SECOND]), \
             patch("audapack.window_layout.find_profile_windows", return_value=[1, 2, 3]), \
             patch("audapack.window_layout.arrange_windows", return_value=3) as mover:
            ok, message = manager.arrange_worker_windows()
        self.assertTrue(ok)
        places, minimize = mover.call_args.args[1], mover.call_args.args[2]
        self.assertEqual(places, cascade_geometry(3, SECOND))
        self.assertFalse(minimize)
        self.assertIn("display 2", message)
        self.assertNotIn("minimized", message)


class TestSlots(unittest.TestCase):
    """A fixed lattice, filled one window at a time from the bottom-left.

    The grid divides the display by however many windows there are, so opening
    a second one halves the first. These cells do not move with the count.
    """

    def test_the_fill_order_is_bottom_row_left_to_right_then_top(self):
        from audapack.window_layout import slot_geometry

        places = slot_geometry(6, SCREEN)
        bottom_y = SCREEN.y + SCREEN.height // 2
        self.assertEqual([x for x, _y, _w, _h in places[:3]], [0, 640, 1280])
        self.assertTrue(all(y == bottom_y for _x, y, _w, _h in places[:3]))
        self.assertEqual([x for x, _y, _w, _h in places[3:]], [0, 640, 1280])
        self.assertTrue(all(y == SCREEN.y for _x, y, _w, _h in places[3:]))

    def test_the_first_window_is_the_bottom_left_cell(self):
        from audapack.window_layout import slot_geometry

        x, y, _w, _h = slot_geometry(1, SCREEN)[0]
        self.assertEqual(x, SCREEN.x)
        self.assertEqual(y, SCREEN.y + SCREEN.height // 2)

    def test_a_cell_is_the_same_size_whatever_the_count(self):
        """This is the whole difference from the grid."""
        from audapack.window_layout import slot_geometry, tile_geometry

        self.assertEqual(slot_geometry(2, SCREEN)[0], slot_geometry(6, SCREEN)[0])
        # The grid does the opposite, on purpose -- two windows take half each.
        self.assertNotEqual(tile_geometry(2, SCREEN)[0], tile_geometry(6, SCREEN)[0])

    def test_the_lattice_reaches_the_right_and_bottom_edges(self):
        from audapack.window_layout import slot_geometry

        places = slot_geometry(6, SCREEN)
        self.assertEqual(max(x + w for x, _y, w, _h in places), SCREEN.x + SCREEN.width)
        self.assertEqual(max(y + h for _x, y, _w, h in places), SCREEN.y + SCREEN.height)

    def test_a_seventh_window_wraps_instead_of_walking_off_the_display(self):
        """A seventh window is a bug to SEE, not one to lose off the edge."""
        from audapack.window_layout import slot_geometry

        places = slot_geometry(7, SCREEN)
        self.assertEqual(places[6], places[0])
        for x, y, w, h in places:
            self.assertLessEqual(x + w, SCREEN.x + SCREEN.width)
            self.assertLessEqual(y + h, SCREEN.y + SCREEN.height)

    def test_the_layout_name_picks_it(self):
        from audapack.window_layout import LAYOUT_SLOTS, slot_geometry

        self.assertEqual(layout_geometry(LAYOUT_SLOTS, 6, SCREEN), slot_geometry(6, SCREEN))
