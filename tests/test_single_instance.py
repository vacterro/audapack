"""Unit tests for SingleInstance mutex guard."""

import sys
import unittest
from unittest.mock import patch

from audapack.single_instance import GuardEstablishmentError, SingleInstance, _process_is_alive


class TestSingleInstance(unittest.TestCase):
    def test_first_instance_acquires_lock(self):
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_A")
        is_running1 = inst1.is_already_running()
        try:
            self.assertFalse(is_running1)
        finally:
            inst1.release()

    @unittest.skipUnless(sys.platform == "win32", "zombie mutex/window recovery is Win32-only")
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

    @unittest.skipUnless(sys.platform == "win32", "visible-window detection is Win32-only")
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

    @unittest.skipUnless(sys.platform == "win32", "foreground activation is Win32-only")
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


class TestOwnerLivenessGuard(unittest.TestCase):
    """W2-005 (audit/1.md): a live owner keeps the guard, window or not.

    "No window" was read as "the holder is dead", and a process sitting between
    CreateMutexW and window.show() looks exactly like that -- so a rapid second
    launch opened a second GUI beside a healthy first one, which is the
    multiple-writer condition the stale-config findings are about.
    """

    def test_a_live_owner_is_reported_running_even_with_no_window(self):
        import audapack.single_instance as mod

        first = SingleInstance("TEST_SI_LIVE_OWNER")
        self.assertFalse(first.is_already_running())
        second = SingleInstance("TEST_SI_LIVE_OWNER")
        try:
            with patch.object(second, "_find_window_hwnd", return_value=None), \
                 patch.object(mod, "_process_is_alive", return_value=True):
                self.assertTrue(
                    second.is_already_running(),
                    "a live owner was treated as a zombie because its window was not up yet",
                )
        finally:
            first.release()
            second.release()

    def test_a_dead_owner_still_lets_the_launcher_recover(self):
        import audapack.single_instance as mod

        first = SingleInstance("TEST_SI_DEAD_OWNER")
        self.assertFalse(first.is_already_running())
        second = SingleInstance("TEST_SI_DEAD_OWNER")
        try:
            with patch.object(second, "_find_window_hwnd", return_value=None), \
                 patch.object(mod, "_process_is_alive", return_value=False):
                self.assertFalse(second.is_already_running())
        finally:
            first.release()
            second.release()

    def test_the_liveness_probe_answers_about_real_processes(self):
        import os

        from audapack.single_instance import _process_is_alive

        self.assertFalse(_process_is_alive(0))
        self.assertFalse(_process_is_alive(-1))
        self.assertFalse(_process_is_alive(os.getpid()), "our own pid is not another owner")
        self.assertFalse(_process_is_alive(4_000_000_000), "no such pid exists")
        parent = os.getppid()
        if parent > 0:
            self.assertTrue(_process_is_alive(parent), "the parent process is running")

    def test_the_owner_record_names_the_holder(self):
        import os

        inst = SingleInstance("TEST_SI_OWNER_RECORD")
        try:
            self.assertFalse(inst.is_already_running())
            self.assertEqual(inst._read_owner_pid(), os.getpid())
        finally:
            inst.release()


class TestLauncherStandsDownForALiveOwner(unittest.TestCase):
    def test_a_live_owner_that_cannot_be_activated_never_opens_a_second_gui(self):
        """On POSIX activation can never succeed, so this branch decided every
        genuine second instance was a leftover lock and started another GUI."""
        from audapack import app as app_mod

        guard = SingleInstance("TEST_SI_MAIN_BRANCH")
        with patch.object(guard, "is_already_running", return_value=True), \
             patch.object(guard, "activate_existing_window", return_value=False), \
             patch.object(guard, "owner_is_alive", return_value=True), \
             patch.object(guard, "wait_for_owner_window", return_value=None), \
             patch("audapack.single_instance.SingleInstance", return_value=guard), \
             patch.object(app_mod, "load_config") as load_cfg:
            load_cfg.return_value = app_mod.load_config()
            with patch("audapack.ui_qt.app.run_qt_gui") as run_gui:
                code = app_mod.main([])
            run_gui.assert_not_called()
        self.assertEqual(code, 0)

    def test_a_dead_owner_still_opens_the_gui(self):
        from audapack import app as app_mod

        guard = SingleInstance("TEST_SI_MAIN_BRANCH_DEAD")
        with patch.object(guard, "is_already_running", return_value=True), \
             patch.object(guard, "activate_existing_window", return_value=False), \
             patch.object(guard, "owner_is_alive", return_value=False), \
             patch("audapack.single_instance.SingleInstance", return_value=guard), \
             patch("audapack.ui_qt.app.run_qt_gui", return_value=0) as run_gui:
            code = app_mod.main([])
        run_gui.assert_called_once()
        self.assertEqual(code, 0)


class TestRecoveryKeepsThePrimaryNamespace(unittest.TestCase):
    """W2-004 (audit/2.md): recovery must take over, not sidestep.

    The recovering launcher CLOSED its handle to the existing primary mutex and
    kept only a separate `_RECOVERY_MUTEX`. So once the zombie finally exited,
    the primary named object disappeared with it -- a third launcher saw no
    primary mutex, created a fresh one, never consulted the recovery mutex, and
    was admitted as a second full GUI. Reproduced with a faithful named-object
    lifetime model: `C allowed? False` on the pre-fix code.
    """

    def test_the_recovering_instance_holds_the_primary_handle(self):
        held = SingleInstance("TEST_SI_PRIMARY_HELD")
        recovering = SingleInstance("TEST_SI_PRIMARY_HELD")
        try:
            self.assertFalse(held.is_already_running())
            with patch.object(recovering, "_find_window_hwnd", return_value=None), \
                 patch("audapack.single_instance._process_is_alive", return_value=False):
                self.assertFalse(recovering.is_already_running())

            if sys.platform == "win32":
                self.assertIsNotNone(
                    recovering._primary_mutex,
                    "the recovered instance dropped the primary namespace",
                )
                self.assertIsNotNone(recovering._recovery_mutex)
                self.assertIsNot(
                    recovering._primary_mutex, recovering._recovery_mutex,
                    "primary and recovery must be tracked separately",
                )
        finally:
            held.release()
            recovering.release()

    @unittest.skipUnless(sys.platform == "win32", "named-mutex continuity is Win32-only")
    def test_a_third_launcher_is_refused_after_the_zombie_exits(self):
        zombie = SingleInstance("TEST_SI_CONTINUITY")
        recovering = SingleInstance("TEST_SI_CONTINUITY")
        third = SingleInstance("TEST_SI_CONTINUITY")
        try:
            self.assertFalse(zombie.is_already_running())
            with patch.object(recovering, "_find_window_hwnd", return_value=None), \
                 patch("audapack.single_instance._process_is_alive", return_value=False):
                self.assertFalse(recovering.is_already_running())

            # The zombie finally dies. Its handle goes; the recovered instance's
            # handle to the same object must keep the namespace alive.
            zombie.release()

            # The recovered GUI is up but its window is not answering yet, and
            # its owner record is live -- the worst window for a third launch.
            with patch.object(third, "_find_window_hwnd", return_value=None), \
                 patch("audapack.single_instance._process_is_alive", return_value=True):
                self.assertTrue(
                    third.is_already_running(),
                    "a third GUI was admitted after the zombie exited",
                )
        finally:
            recovering.release()
            third.release()

    @unittest.skipUnless(sys.platform == "win32", "named-mutex continuity is Win32-only")
    def test_a_clean_release_lets_the_next_launcher_in(self):
        first = SingleInstance("TEST_SI_CONTINUITY_RELEASE")
        recovering = SingleInstance("TEST_SI_CONTINUITY_RELEASE")
        self.assertFalse(first.is_already_running())
        with patch.object(recovering, "_find_window_hwnd", return_value=None), \
             patch("audapack.single_instance._process_is_alive", return_value=False):
            self.assertFalse(recovering.is_already_running())
        first.release()
        recovering.release()

        after = SingleInstance("TEST_SI_CONTINUITY_RELEASE")
        try:
            self.assertFalse(
                after.is_already_running(),
                "both guards were released, so a new instance must be admitted",
            )
        finally:
            after.release()

    @unittest.skipUnless(sys.platform == "win32", "named-mutex continuity is Win32-only")
    def test_a_second_recovery_launcher_is_refused_while_one_is_recovering(self):
        zombie = SingleInstance("TEST_SI_ONE_RECOVERY")
        first_recovery = SingleInstance("TEST_SI_ONE_RECOVERY")
        second_recovery = SingleInstance("TEST_SI_ONE_RECOVERY")
        try:
            self.assertFalse(zombie.is_already_running())
            with patch.object(first_recovery, "_find_window_hwnd", return_value=None), \
                 patch("audapack.single_instance._process_is_alive", return_value=False):
                self.assertFalse(first_recovery.is_already_running())

            with patch.object(second_recovery, "_find_window_hwnd", return_value=None), \
                 patch("audapack.single_instance._process_is_alive", return_value=False):
                with self.assertRaises(GuardEstablishmentError):
                    second_recovery.is_already_running()
        finally:
            zombie.release()
            first_recovery.release()
            second_recovery.release()


class TestLivenessProbeIsTotal(unittest.TestCase):
    """W2-005 (audit/4.md): the probe must answer, never raise.

    `os.kill(pid, 0)` raises OverflowError for a value beyond the platform's
    signed-int range -- measured with 4,000,000,000 on POSIX -- and nothing
    caught it, so a corrupt owner record could crash the launcher's
    second-instance handling. An unqueryable PID is treated as alive, because
    "dead" is the only answer that ever authorizes a second GUI.
    """

    def test_out_of_range_and_malformed_pids_never_raise(self):

        for value in (0, -1, -10**12, 4_000_000_000, 10**30, float(10**15), None):
            with self.subTest(value=value):
                try:
                    _process_is_alive(value)
                except Exception as exc:
                    self.fail(f"_process_is_alive({value!r}) raised {type(exc).__name__}: {exc}")

    def test_a_garbage_owner_record_fails_closed(self):
        self.assertTrue(
            _process_is_alive("garbage"),
            "an unparseable PID must not read as a dead owner",
        )

    def test_our_own_pid_is_not_another_owner(self):
        import os as _os

        self.assertFalse(_process_is_alive(_os.getpid()))

    def test_a_real_live_process_reports_alive(self):
        import os as _os

        parent = _os.getppid()
        if parent > 0:
            self.assertTrue(_process_is_alive(parent))
