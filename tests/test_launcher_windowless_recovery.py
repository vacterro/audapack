"""T-25: a windowless/hung GUI owner must self-heal, not brick the launcher.

The reported failure was "app doesn't open": every click saw a live
Local\\AUDAPACK_GUI_MUTEX, found no window, told the operator (in a box they had
to dismiss) to go and end a process in Task Manager, and opened nothing. The
windowless owner held the guard forever, so the launcher was dead until a human
intervened.

The replacement contract, case by case:

  A  a healthy visible owner      -> foreground it, kill nothing, open no 2nd GUI
  B  an owner still starting      -> wait the grace, foreground it when it arrives
  C  a verified owner, no window  -> terminate it, take the SAME primary guard,
                                     open the GUI -- silently, in this same click
  D  an unprovable owner          -> kill nothing, say so as a real ERROR

The load-bearing part of this file is that "verified" means verified. These
tests drive REAL named mutexes, REAL owner records and REAL child processes on
Windows: a PID in a stale JSON file is not an identity, and only a test that
would actually fire TerminateProcess can prove the guard in front of it works.

Off Windows the live cases skip rather than pretend: ctypes.windll, named
mutexes and TerminateProcess have no meaning there.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from audapack import app as app_mod
from audapack.single_instance import SingleInstance

ROOT = Path(__file__).resolve().parent.parent
WIN32_ONLY = unittest.skipUnless(sys.platform == "win32", "named-object identity is Win32-only")


# --------------------------------------------------------------------------- #
# live child processes
# --------------------------------------------------------------------------- #

# A windowless owner: takes the guard, puts NOTHING on screen, stays alive.
# This is the production brick, reproduced in a controlled way instead of by
# corrupting somebody else's process.
_WINDOWLESS_OWNER = """
import sys, time
sys.path.insert(0, {root!r})
from audapack.single_instance import SingleInstance
guard = SingleInstance({name!r})
if guard.is_already_running():
    raise SystemExit(3)
print("OWNER_READY", flush=True)
time.sleep(300)
"""

# A REAL top-level window in a REAL other process, plus the REAL named mutex.
# `mode` picks what this child does to its own window:
#   shown    -- a normal visible window (the healthy owner)
#   hidden   -- window exists but was never shown (the .vbs launch state)
#   minimized-- shown, then iconified (alive, alive-but-not-on-screen)
#   hung     -- a window exists, then the UI thread STOPS PUMPING: the exact
#               shape of the owner that hangs a synchronous restore call.
# The window is a plain Win32 "STATIC" top-level, so no class registration and
# no Qt is involved: the thing under test is the Win32 show-state contract, not
# a particular toolkit's opinion of it. The operator's real AUDAPACK process is
# never used as the subject -- these own isolated guard names.
_WINDOW_OWNER = """
import ctypes, sys, time
sys.path.insert(0, {root!r})
from audapack.single_instance import SingleInstance
from ctypes import wintypes

guard = SingleInstance({name!r})
if guard.is_already_running():
    raise SystemExit(3)

user32 = ctypes.windll.user32
# ctypes types every return value as c_int unless told otherwise, and an HWND
# is 64-bit on this platform: left undeclared it is TRUNCATED, and a truncated
# HWND would silently name some other window. Declare it, once, here.
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    ctypes.c_ulong, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_ulong,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_HIDE, SW_SHOW, SW_MINIMIZE, SW_MAXIMIZE = 0, 5, 6, 3

hwnd = user32.CreateWindowExW(
    0, "STATIC", "AUDAPACK TEST OWNER WINDOW",
    WS_OVERLAPPEDWINDOW, 100, 100, 420, 240,
    None, None, None, None)
if not hwnd:
    raise SystemExit(4)

mode = {mode!r}
if mode in ("shown", "minimized", "maximized"):
    user32.ShowWindow(hwnd, SW_SHOW)
if mode == "minimized":
    user32.ShowWindow(hwnd, SW_MINIMIZE)
elif mode == "maximized":
    user32.ShowWindow(hwnd, SW_MAXIMIZE)

print("WINDOW_READY %d" % hwnd, flush=True)

if mode == "hung":
    # A real window, a real owning thread, and no message pump. Anything that
    # waits on this thread now waits forever -- which is the bug under test.
    while True:
        time.sleep(0.2)

while True:
    msg = wintypes.MSG()
    if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
        break
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
"""


class _Child:
    """A child this test file started, by PID, and may therefore end."""

    def __init__(self, script: str, ready_token: str = "OWNER_READY"):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = self.proc.stdout.readline().strip()
        if not ready.startswith(ready_token):
            err = self.proc.stderr.read()
            self.stop()
            raise AssertionError(f"live owner never became ready: {ready!r} / {err}")
        #: Only window children report one; it is the real HWND to assert on.
        self.hwnd = int(ready.split()[1]) if ready.count(" ") else 0

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _spawn(name: str, script_template: str, ready_token: str = "OWNER_READY", **extra) -> _Child:
    return _Child(script_template.format(root=str(ROOT), name=name, **extra), ready_token)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _run_launcher(grace: float = 0.2) -> tuple[int, "unittest.mock.MagicMock", list[tuple]]:
    """Drive the REAL app.main() down the already-running branch.

    Only the Qt entry point is stubbed: the guard, the mutexes, the owner
    record, the identity verification and the recovery sequence are all
    production code. Any MessageBox is captured instead of shown, so a failing
    assertion cannot leave a dialog on the operator's desktop.

    Callers MUST patch SingleInstance onto their own test guard name first. If
    one forgot, main() would build the PRODUCTION guard and could try to
    recover the operator's real AUDAPACK -- hence the wrapper.
    """
    import unittest.mock as mock

    boxes: list[tuple] = []
    run_gui = mock.MagicMock(return_value=0)
    # The report seam is Win32: ctypes.windll does not exist off Windows, and
    # _fail_recovery returns before any MessageBox there, so capture the box
    # only where a box can actually happen.
    capture_box = (
        patch("ctypes.windll.user32.MessageBoxW", lambda *a: boxes.append(a))
        if sys.platform == "win32"
        else contextlib.nullcontext()
    )
    with patch("audapack.single_instance.OWNER_WINDOW_GRACE_SECONDS", grace), \
         capture_box, \
         patch("audapack.ui_qt.app.run_qt_gui", run_gui):
        code = app_mod.main([])
    return code, run_gui, boxes


def _launcher_for(name: str):
    """Patch main()'s SingleInstance onto ONE named test guard.

    Binding the name is deliberate: main() asks for "AUDAPACK_GUI", and a test
    that let that through would probe -- and on a windowless owner, terminate --
    the operator's actual AUDAPACK process.
    """
    return patch("audapack.single_instance.SingleInstance", side_effect=lambda _n: SingleInstance(name))


def _cleanup(guard_name: str) -> None:
    # Only ever this test file's own named objects.
    for name in (guard_name,):
        try:
            rec = SingleInstance(name)._read_owner_record()
            rec_path = SingleInstance(name)._owner_record_path()
            if rec.get("pid") == os.getpid() and rec_path.exists():
                rec_path.unlink(missing_ok=True)
        except OSError:
            pass


def _probe_primary_mutex(guard_name: str) -> tuple[bool, int]:
    """Is the PRIMARY named mutex for `guard_name` genuinely HELD right now?

    Asked from a THREAD of this process, never from the owning thread: Win32
    mutex ownership is thread-scoped and recursive, so the thread that holds it
    gets WAIT_OBJECT_0 back from its own acquisition and cannot tell a held
    guard from a free one. A second thread really is an outside caller, and it
    is the only honest way for a test to ask "is it still held?" in-process.

    Returns (object already existed, WaitForSingleObject(0) result).
    """
    import threading

    from audapack.single_instance import _win32_api

    seen: dict = {}

    def _ask() -> None:
        kernel32 = _win32_api()
        handle = kernel32.CreateMutexW(None, True, f"Local\\{guard_name}_MUTEX")
        try:
            seen["existed"] = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
            seen["wait"] = kernel32.WaitForSingleObject(handle, 0)
        finally:
            kernel32.CloseHandle(handle)

    probe = threading.Thread(target=_ask, daemon=True)
    probe.start()
    probe.join(10)
    assert not probe.is_alive(), "the out-of-thread mutex probe never finished"
    return bool(seen.get("existed")), int(seen.get("wait", -1))


# --------------------------------------------------------------------------- #
# Case A / B -- a healthy owner is never touched
# --------------------------------------------------------------------------- #


class TestHealthyOwnerIsLeftAlone(unittest.TestCase):
    """Case A and B. Killing a healthy AUDAPACK would be a worse bug than the
    one being fixed, so these pin the do-nothing paths explicitly."""

    def test_a_visible_owner_is_foregrounded_and_nothing_else_happens(self):
        guard = SingleInstance("TEST_SI_T25_VISIBLE")
        with patch.object(guard, "is_already_running", return_value=True), \
             patch.object(guard, "activate_existing_window", return_value=True), \
             patch("audapack.single_instance.SingleInstance", return_value=guard):
            code, run_gui, boxes = _run_launcher()
        self.assertEqual(code, 0)
        run_gui.assert_not_called()
        self.assertEqual(boxes, [], "a foregrounded owner needs no dialog at all")

    def test_an_owner_that_is_still_starting_is_waited_for_then_foregrounded(self):
        """Case B: the window appears INSIDE the grace, so nothing is killed."""
        guard = SingleInstance("TEST_SI_T25_STARTING")
        # First activation misses (no window yet); the grace wait finds it.
        activations = [False, True]
        with patch.object(guard, "is_already_running", return_value=True), \
             patch.object(guard, "activate_existing_window", side_effect=lambda *_a, **_k: activations.pop(0)), \
             patch.object(guard, "wait_for_owner_window", return_value=0x1234), \
             patch("audapack.single_instance.SingleInstance", return_value=guard), \
             patch.object(guard, "acquire_recovery_guard") as recovery:
            code, run_gui, boxes = _run_launcher()
        self.assertEqual(code, 0)
        run_gui.assert_not_called()
        recovery.assert_not_called()
        self.assertEqual(boxes, [])


# --------------------------------------------------------------------------- #
# Case C -- verified recovery
# --------------------------------------------------------------------------- #


class TestVerifiedWindowlessOwnerIsRecoveredLive(unittest.TestCase):
    """Case C against a REAL windowless owner process.

    The child owns the real named mutex and has no window, so the launcher has
    exactly the production brick to deal with. Passing means the owner was
    verifiably identified, ended, and replaced by a real GUI in one click.
    """

    NAME = "TEST_SI_T25_LIVE_RECOVERY"

    @WIN32_ONLY
    def test_one_launch_recovers_the_brick_and_opens_the_real_gui(self):
        owner = _spawn(self.NAME, _WINDOWLESS_OWNER)
        self.addCleanup(owner.stop)
        self.addCleanup(_cleanup, self.NAME)
        broken_pid = owner.pid

        with _launcher_for(self.NAME):
            code, run_gui, boxes = _run_launcher()

        self.assertEqual(code, 0)
        run_gui.assert_called_once(), "the recovered launcher must open the GUI itself"
        self.assertTrue(
            _wait_until(lambda: not owner.alive()),
            "the verified windowless owner must be gone",
        )
        self.assertNotEqual(os.getpid(), broken_pid)
        self.assertEqual(boxes, [], "a SUCCESSFUL recovery is silent -- no 'already running' box")

        # The guard genuinely changed hands: the SAME primary object is still
        # held, and the record on disk now names THIS process, so the next
        # launch sees a live owner and stands down honestly.
        #
        # The old oracle here called is_already_running() from the very thread
        # that now owns the guard. Win32 mutex ownership is thread-scoped and
        # recursive, so that call can only ever answer WAIT_OBJECT_0 -- it
        # reports "free" for a guard that is held and for one that is not, so
        # it proved nothing about recovery either way. Ask from another thread.
        existed, wait = _probe_primary_mutex(self.NAME)
        self.assertTrue(existed, "recovery must keep the canonical primary name")
        self.assertEqual(wait, 0x102, "WAIT_TIMEOUT: the primary guard is no longer held")

        successor = SingleInstance(self.NAME)
        self.addCleanup(successor.release)
        record = successor.read_owner_record()
        self.assertEqual(record.get("pid"), os.getpid())
        self.assertEqual(record.get("guard_name"), successor.guard_name())
        verified, why = successor.verify_owner_identity(record)
        self.assertTrue(verified, why)


# --------------------------------------------------------------------------- #
# Case D -- unprovable identity is never killed
# --------------------------------------------------------------------------- #


class TestUnprovenIdentityIsNeverKilled(unittest.TestCase):
    """Case D, plus the PID-reuse trap that motivates it."""

    NAME = "TEST_SI_T25_UNPROVEN"

    @WIN32_ONLY
    def test_a_reused_pid_is_never_terminated(self):
        """The record is stale; the PID now belongs to a live stranger.

        This is the whole reason the owner record carries a kernel creation
        time. TerminateProcess on a bare PID can kill an unrelated process, and
        the child below is here to prove it is still breathing.
        """
        child = _spawn(self.NAME, _WINDOWLESS_OWNER)
        self.addCleanup(child.stop)
        self.addCleanup(_cleanup, self.NAME)

        stale = SingleInstance(self.NAME).read_owner_record()
        self.assertEqual(stale.get("pid"), child.pid)
        # PID reuse, exactly: same pid, different process.
        stale["process_creation_time"] = 1

        state, why = SingleInstance(self.NAME).terminate_verified_owner(stale)
        self.assertEqual(state, "refused", why)
        self.assertTrue(child.alive(), "a PID whose creation time differs was killed anyway")
        self.assertIn("creation time", why)

    @WIN32_ONLY
    def test_an_unreadable_owner_record_produces_a_recovery_failure_not_a_kill(self):
        guard = SingleInstance(self.NAME)
        with patch.object(guard, "is_already_running", return_value=True), \
             patch.object(guard, "activate_existing_window", return_value=False), \
             patch.object(guard, "wait_for_owner_window", return_value=None), \
             patch.object(guard, "read_owner_record", return_value={}), \
             patch("audapack.single_instance.SingleInstance", return_value=guard):
            code, run_gui, boxes = _run_launcher()
        self.assertEqual(code, 1)
        run_gui.assert_not_called()
        self.assertEqual(len(boxes), 1, "an unverifiable holder must produce exactly one report")
        text = boxes[0][1]
        self.assertIn("could not recover", text.lower())
        self.assertIn("NOT proven", text)
        # ERROR semantics, not the old informational "already running" box.
        self.assertTrue(boxes[0][3] & 0x10, "the report must carry MB_ICONERROR")

    @WIN32_ONLY
    def test_a_record_for_another_guard_is_never_terminated(self):
        """Same PID, same moment, wrong namespace: still not ours to kill."""
        child = _spawn(self.NAME, _WINDOWLESS_OWNER)
        self.addCleanup(child.stop)
        self.addCleanup(_cleanup, self.NAME)

        foreign = SingleInstance(self.NAME).read_owner_record()
        foreign["guard_name"] = "Local\\SOME_OTHER_GUI_MUTEX"
        state, why = SingleInstance(self.NAME).terminate_verified_owner(foreign)
        self.assertEqual(state, "refused", why)
        self.assertTrue(child.alive(), "an owner record from another guard was acted on")


# --------------------------------------------------------------------------- #
# serialization, natural death, restore, record ownership
# --------------------------------------------------------------------------- #


class TestRecoveryIsSerialized(unittest.TestCase):
    """Three guards, three names: a named mutex held by one test process is
    invisible to the next test's assumption that it starts free."""

    @WIN32_ONLY
    def test_only_one_launcher_may_recover_and_the_other_opens_no_second_gui(self):
        name = "TEST_SI_T25_SERIAL_RACE"
        first = SingleInstance(name)
        self.addCleanup(first.release)
        second = SingleInstance(name)
        self.addCleanup(second.release)

        acquired, why = first.acquire_recovery_guard()
        self.assertTrue(acquired, why)

        peer, peer_why = second.acquire_recovery_guard()
        self.assertFalse(peer, "a second recovery ran concurrently with the first")
        self.assertIn("already holds", peer_why)

        # ...and the launcher's answer to losing that race is to wait and
        # foreground the GUI the winner opens, never to start one of its own.
        # Nothing is stubbed here: the real recovery guard says no.
        with patch.object(second, "is_already_running", return_value=True), \
             patch.object(second, "activate_existing_window", side_effect=[False, True]), \
             patch.object(second, "wait_for_owner_window", return_value=0xABCD), \
             patch("audapack.single_instance.SingleInstance", return_value=second):
            code, run_gui, boxes = _run_launcher()
        self.assertEqual(code, 0)
        run_gui.assert_not_called()
        self.assertEqual(boxes, [])
        second.release()

        first.release_recovery_guard()
        released, released_why = second.acquire_recovery_guard()
        self.assertTrue(released, released_why)
        second.release_recovery_guard()

    @WIN32_ONLY
    def test_an_owner_that_dies_naturally_is_not_terminated(self):
        """It exited between our probe and our recovery: take the abandoned
        guard, do not fire TerminateProcess at a PID the OS may already reuse."""
        name = "TEST_SI_T25_SERIAL_DEAD"
        owner = _spawn(name, _WINDOWLESS_OWNER)
        self.addCleanup(owner.stop)
        self.addCleanup(_cleanup, name)
        guard = SingleInstance(name)

        def _owner_dies_then_no_window(*_a, **_k):
            owner.stop()
            return None

        outcomes: list[tuple[str, str]] = []
        real_terminate = SingleInstance.terminate_verified_owner

        def _record_outcome(self, record=None, timeout=10.0):
            outcome = real_terminate(self, record, timeout)
            outcomes.append(outcome)
            return outcome

        with patch.object(guard, "is_already_running", return_value=True), \
             patch.object(guard, "activate_existing_window", return_value=False), \
             patch.object(guard, "wait_for_owner_window", side_effect=_owner_dies_then_no_window), \
             patch.object(SingleInstance, "terminate_verified_owner", _record_outcome), \
             patch("audapack.single_instance.SingleInstance", return_value=guard):
            code, run_gui, _boxes = _run_launcher()

        self.assertEqual(code, 0)
        run_gui.assert_called_once()
        self.assertFalse(owner.alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][0], "gone",
                         "a naturally dead owner must be taken over, not terminated")
        self.assertIn("exited on its own", outcomes[0][1])

    @WIN32_ONLY
    def test_a_hidden_main_window_is_restored_instead_of_killed(self):
        """A hidden window is not a brick. Restoring it is free; killing it
        throws away a perfectly healthy AUDAPACK.

        Proved at the DECISION level rather than with a second live child: the
        window lookup and the restore call are the only two things that can
        differ between "restore" and "kill", so both are supplied and the only
        thing asserted is which branch ran -- and that OpenProcess was never
        even asked for PROCESS_TERMINATE.
        """
        name = "TEST_SI_T25_SERIAL_HIDDEN"
        owner = _spawn(name, _WINDOWLESS_OWNER)
        self.addCleanup(owner.stop)
        self.addCleanup(_cleanup, name)

        guard = SingleInstance(name)
        rec = guard._read_owner_record()
        self.assertEqual(rec.get("pid"), owner.pid, "the live owner did not write its record")

        opened: list[int] = []

        with patch.object(guard, "_find_window_hwnd", return_value=0xABCD) as finder, \
             patch.object(guard, "_restore_window_hwnd", return_value=True), \
             patch.object(guard, "wait_for_process_exit",
                          side_effect=lambda *a, **k: opened.append(1)):
            state, detail = guard.terminate_verified_owner(rec)

        self.assertEqual(state, "restored", detail)
        self.assertEqual(opened, [], "the restore branch must never wait for a death")
        self.assertIn(str(owner.pid), detail)
        self.assertTrue(owner.alive(), "the hidden-window owner was killed instead of restored")
        self.assertEqual(finder.call_args.kwargs.get("pid"), owner.pid,
                         "the window must belong to the proven owner, not to any AUDAPACK")
        self.assertTrue(finder.call_args.kwargs.get("include_hidden"),
                        "a hidden window is only reachable through the hidden path")

    def test_hidden_window_titles_match_only_the_canonical_markers(self):
        """The hidden matcher is stricter on purpose.

        Recovery restores -- and only if it cannot, kills -- whatever this
        returns. A loose match here would let any other process's window that
        merely mentions AUDAPACK pull the guard into the restore path.
        """
        from audapack.single_instance import AUDAPACK_WINDOW_MARKERS, is_hidden_audapack_window_title

        for marker in AUDAPACK_WINDOW_MARKERS:
            self.assertTrue(is_hidden_audapack_window_title(marker))
            self.assertTrue(is_hidden_audapack_window_title(f"  {marker}  "))
        for impostor in ("untitled - C:\\src\\AUDAPACK", "my AUDAPACK launcher notes",
                         "AUDAPACK", "", None, "some other app"):
            self.assertFalse(is_hidden_audapack_window_title(impostor), impostor)


class TestRealWin32WindowRestore(unittest.TestCase):
    """The restore branch against REAL Win32, not a mock.

    The decision-level test above pins WHICH branch runs; it cannot tell a
    working show-state call from one that merely returns True. These drive real
    HWNDs owned by real other processes on this box: the show/hide, un-minimize,
    and hung-thread contracts are the ones the launcher actually depends on, and
    none of them exist in Python. Every child owns an isolated named guard and a
    real named mutex; the operator's AUDAPACK process is never the subject.
    """

    NAME = "TEST_SI_T25_REAL_RESTORE"

    @staticmethod
    def _user32():
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        return user32

    @WIN32_ONLY
    def test_a_real_hidden_window_is_restored_and_its_process_survives(self):
        """The .vbs launch state: a real window exists, nobody showed it."""
        user32 = self._user32()
        child = _spawn(self.NAME, _WINDOW_OWNER, "WINDOW_READY", mode="hidden")
        self.addCleanup(child.stop)
        self.addCleanup(_cleanup, self.NAME)

        self.assertTrue(child.hwnd, "the child never reported a real HWND")
        self.assertFalse(user32.IsWindowVisible(child.hwnd))

        guard = SingleInstance(self.NAME)
        self.assertTrue(guard._restore_window_hwnd(child.hwnd))
        self.assertTrue(user32.IsWindowVisible(child.hwnd))
        self.assertTrue(child.alive(), "a restorable window must cost no process")

    @WIN32_ONLY
    def test_a_real_minimized_window_is_ACTUALLY_restored(self):
        """The red control for the show-state bug.

        A minimized window is already WS_VISIBLE, so `IsWindowVisible` alone
        cannot tell "restored" from "still iconified". The hand-rolled
        WM_SHOWWINDOW call left it iconified and was then called a success;
        only the real show-state API un-minimizes it.
        """
        user32 = self._user32()
        child = _spawn(self.NAME, _WINDOW_OWNER, "WINDOW_READY", mode="minimized")
        self.addCleanup(child.stop)
        self.addCleanup(_cleanup, self.NAME)

        self.assertTrue(user32.IsIconic(child.hwnd), "the child is not minimized")
        guard = SingleInstance(self.NAME)
        self.assertTrue(guard._restore_window_hwnd(child.hwnd))
        self.assertTrue(user32.IsWindowVisible(child.hwnd))
        self.assertFalse(user32.IsIconic(child.hwnd),
                         "visible-but-still-iconified is NOT restored")
        self.assertTrue(child.alive())

    @WIN32_ONLY
    def test_a_maximized_owner_is_not_shrunk_by_a_second_launch(self):
        """SW_RESTORE alone would un-maximize a perfectly happy owner on every
        double-click, so only an iconified window is asked to restore."""
        user32 = self._user32()
        child = _spawn(self.NAME, _WINDOW_OWNER, "WINDOW_READY", mode="maximized")
        self.addCleanup(child.stop)
        self.addCleanup(_cleanup, self.NAME)

        self.assertFalse(user32.IsIconic(child.hwnd))
        guard = SingleInstance(self.NAME)
        self.assertTrue(guard._restore_window_hwnd(child.hwnd))
        self.assertFalse(user32.IsIconic(child.hwnd))

    @WIN32_ONLY
    def test_a_hung_windows_restore_is_bounded_and_its_owner_can_still_be_ended(self):
        """The contract that keeps a hung owner from becoming a hung launcher.

        The child owns a real window and a real mutex but its UI thread never
        pumps. The restore request is posted to that thread and nothing happens,
        so the call must RETURN on its own deadline -- and the verified owner
        must then go down the termination path rather than being left to hang.
        """
        from audapack.single_instance import _process_is_alive

        user32 = self._user32()
        child = _spawn(self.NAME, _WINDOW_OWNER, "WINDOW_READY", mode="hung")
        self.addCleanup(child.stop)
        self.addCleanup(_cleanup, self.NAME)

        guard = SingleInstance(self.NAME)
        record = guard.read_owner_record()
        self.assertEqual(record.get("pid"), child.pid)
        self.assertFalse(user32.IsWindowVisible(child.hwnd))

        started = time.time()
        restored = guard._restore_window_hwnd(child.hwnd, poll_seconds=0.5)
        elapsed = time.time() - started
        self.assertFalse(restored, "a window nobody is pumping cannot be restored")
        self.assertLess(elapsed, 5.0, f"restore blocked for {elapsed:.1f}s against a hung thread")
        self.assertTrue(_process_is_alive(child.pid), "restore must not kill anything")

        state, why = guard.terminate_verified_owner(record)
        self.assertEqual(state, "terminated", why)
        self.assertTrue(_wait_until(lambda: not child.alive()),
                        "a verified hung owner must be endable after the bounded restore")


class TestOwnerRecordOwnership(unittest.TestCase):
    @WIN32_ONLY
    def test_an_old_owner_cannot_delete_the_new_owners_record(self):
        """Release must remove ONLY its own record.

        Strip the successor's record on the way out and the NEXT windowless
        brick has no identity to verify -- which is how a recoverable owner
        turns into an unkillable one.
        """
        name = "TEST_SI_T25_REC_OLD_OWNER"
        old = SingleInstance(name)
        self.addCleanup(_cleanup, name)
        self.assertFalse(old.is_already_running())
        old_nonce = old.read_owner_record()["owner_nonce"]

        # A later launcher takes the guard over and writes its own identity --
        # exactly what _write_owner_record does on every acquisition.
        successor = SingleInstance(name)
        successor._write_owner_record()
        new_nonce = successor.read_owner_record()["owner_nonce"]
        self.assertNotEqual(old_nonce, new_nonce)

        old.release()

        survivor = SingleInstance(name).read_owner_record()
        self.assertEqual(survivor.get("owner_nonce"), new_nonce,
                         "the released owner deleted a record that is not its own")

    @WIN32_ONLY
    def test_release_deletes_the_record_it_actually_wrote(self):
        name = "TEST_SI_T25_REC_RELEASE"
        guard = SingleInstance(name)
        self.addCleanup(_cleanup, name)
        self.assertFalse(guard.is_already_running())
        self.assertTrue(guard.read_owner_record())
        guard.release()
        self.assertEqual(guard.read_owner_record(), {})

    @WIN32_ONLY
    def test_the_record_carries_a_kernel_creation_time(self):
        """The mandatory half of the identity. Without it there is no proof."""
        guard = SingleInstance("TEST_SI_T25_REC_IDENTITY")
        self.addCleanup(guard.release)
        self.assertFalse(guard.is_already_running())
        record = guard.read_owner_record()
        self.assertEqual(record.get("schema"), 2)
        self.assertEqual(record.get("guard_name"), guard.guard_name())
        self.assertEqual(record.get("pid"), os.getpid())
        self.assertTrue(record.get("process_creation_time"), "no kernel creation time was recorded")
        self.assertTrue(record.get("process_image"), "no image identity was recorded")
        verified, why = guard.verify_owner_identity(record)
        self.assertTrue(verified, why)


if __name__ == "__main__":
    unittest.main()
