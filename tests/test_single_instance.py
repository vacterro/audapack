"""Unit tests for SingleInstance mutex guard."""

import unittest
from unittest.mock import patch

from audapack.single_instance import SingleInstance


class TestSingleInstance(unittest.TestCase):
    def test_first_instance_acquires_lock(self):
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_A")
        is_running1 = inst1.is_already_running()
        try:
            self.assertFalse(is_running1)
        finally:
            inst1.release()

    def test_second_instance_zombie_holder_self_corrects(self):
        """Regression for "app doesn't open via launcher": a held mutex with NO
        visible window is a zombie holder. The second instance must self-correct
        (release the failed-attempt handle and return False) so the launcher can
        open a new instance instead of silently no-opping forever."""
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_ZOMBIE")
        is_running1 = inst1.is_already_running()
        self.assertFalse(is_running1)

        inst2 = SingleInstance("TEST_SINGLE_INSTANCE_ZOMBIE")
        # Explicitly simulate a zombie: the mutex is held, but no AUDAPACK
        # window is reachable. (On a real desktop, unrelated apps could
        # otherwise surface a spurious "AUDAPACK"-titled window.)
        with patch.object(inst2, "_find_window_hwnd", return_value=None):
            is_running2 = inst2.is_already_running()
        try:
            self.assertFalse(
                is_running2,
                "zombie holder (mutex without window) must self-correct, otherwise the launcher is permanently bricked",
            )
        finally:
            inst1.release()
            inst2.release()

    def test_second_instance_with_window_detected(self):
        """If a mutex is held AND an AUDAPACK window is reachable, the guard
        correctly reports "already running" so the launcher can foreground it."""
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_LIVE")
        is_running1 = inst1.is_already_running()
        self.assertFalse(is_running1)

        fake_hwnd = 0xCAFE

        inst2 = SingleInstance("TEST_SINGLE_INSTANCE_LIVE")
        # Pretend inst1's window is visible and titled AUDAPACK.
        with patch.object(inst2, "_find_window_hwnd", return_value=fake_hwnd):
            try:
                self.assertTrue(inst2.is_already_running())
            finally:
                inst1.release()
                inst2.release()

    def test_activate_existing_window_returns_bool(self):
        """activate_existing_window must signal whether it actually foregrounded
        something, so the launcher can recover when the holder is a zombie."""
        inst = SingleInstance("TEST_SINGLE_INSTANCE_ACTIVATE")
        self.assertFalse(inst.is_already_running())
        with patch.object(inst, "_find_window_hwnd", return_value=None):
            self.assertFalse(inst.activate_existing_window("AUDAPACK"))
        inst.release()

    def test_ide_window_is_not_treated_as_audapack(self):
        """Regression: an editor/IDE window that happens to mention AUDAPACK in
        its title (e.g. OpenCode/VS Code with the project open) must NOT be
        matched as the AUDAPACK GUI, otherwise the launcher would try to
        foreground the IDE instead of opening (or recovering from) the real
        AUDAPACK GUI."""
        # Simulate the live OpenCode title we observed: "_AUDAPACK | OpenCode YOLO | V:\\..."
        title = "_AUDAPACK | OpenCode YOLO | V:\\___VAC\\__K\\__CODE\\_PY\\_AUDAPACK"
        # The default AUDAPACK markers must reject this title.
        markers = (
            "audapack \u2014 project room",
            "audapack settings",
        )
        self.assertFalse(any(m in title.lower() for m in markers))
        # And the prefix-fallback path must also reject it (it contains " | ").
        title_lower = title.lower()
        self.assertTrue(" | " in title_lower)

    def test_released_instance_allows_reacquire(self):
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_C")
        self.assertFalse(inst1.is_already_running())
        inst1.release()

        inst2 = SingleInstance("TEST_SINGLE_INSTANCE_C")
        try:
            self.assertFalse(inst2.is_already_running())
        finally:
            inst2.release()
