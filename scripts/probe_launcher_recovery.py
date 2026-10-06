"""Operator-run Windows acceptance for T-25. One case per handoff step.

    python scripts/probe_launcher_recovery.py --case all   # A..E, self-contained
    python scripts/probe_launcher_recovery.py --case E     # one case

EVERY case is SELF-CONTAINED: it establishes its own precondition, launches the
real AUDAPACK itself, proves what it came to prove, and cleans up the processes
it started. There is no manual "start AUDAPACK first" / "close AUDAPACK first"
step anywhere, so `--case all` can run in one uninterrupted invocation. The only
thing the operator must do is CLOSE AUDAPACK once, before starting the run.

  A  a healthy VISIBLE owner    -> foreground it, kill nothing, open no 2nd GUI
  B  an owner still STARTING    -> wait the grace, foreground it, kill nothing
  C  a windowless owner         -> verified, ended, SAME guard, GUI opens in the
                                   same click, with NO "already running" box
  D  mutex held but unprovable  -> kill NOTHING, exit 1, one recovery ERROR
  E  a healthy HIDDEN owner     -> restored in place, same PID, no kill

The probe's window discovery is deliberately its OWN EnumWindows pass rather
than the production `_find_window_hwnd`: an acceptance gate that asks the code
under test whether it succeeded cannot fail when the code under test is wrong.
It shares only the pure title classifier, which has its own unit tests.

Pytest cannot judge this: it needs the operator's own interactive window
station, with the operator watching, and several cases are about what the
DESKTOP shows. Run it from a normal session, not from a service.

Safety, per case:
  * Only processes this script started are ever ended, and only by PID.
  * Case C ends its own deliberately windowless owner -- never anything else.
  * Case D ends nothing but its own child; production refuses to kill an
    identity it cannot prove, and that refusal is what the case proves.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audapack import __app_name__  # noqa: E402
from audapack.single_instance import (  # noqa: E402
    WAIT_ABANDONED,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    SingleInstance,
    _process_is_alive,
    is_audapack_window_title,
)

# The real production guard. A probe on a private name would prove nothing.
GUARD = "AUDAPACK_GUI"
WINDOW_TIMEOUT_SECONDS = 120.0
LAUNCHER_EXIT_TIMEOUT_SECONDS = 180.0
POLL_SECONDS = 0.25

#: The one modal AUDAPACK can raise, and the only one a case must ever see.
RECOVERY_ERROR_TITLE = f"{__app_name__} - launcher recovery failed"

#: Stands in for the .vbs launcher state: the owner exists, nobody showed it.
SW_HIDE = 0

_WINDOWLESS_OWNER = """
import sys, time
sys.path.insert(0, {root!r})
from audapack.single_instance import SingleInstance
guard = SingleInstance({name!r})
if guard.is_already_running():
    print("GUARD_BUSY", flush=True)
    raise SystemExit(3)
print("OWNER_READY", flush=True)
time.sleep(900)
"""


# --------------------------------------------------------------------------- #
# Win32, declared once
# --------------------------------------------------------------------------- #

def _user32():
    """Declared prototypes only. ctypes types returns as c_int unless told
    otherwise, and an HWND is 64-bit here: undeclared it is TRUNCATED, and a
    truncated HWND names some other window -- which would turn every window
    assertion in this file into a coin toss."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


def _top_level_windows() -> list[tuple[int, int, str]]:
    """Every top-level window on the desktop as (hwnd, owning pid, title)."""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: list[tuple[int, int, str]] = []

    def foreach(hwnd, _lparam):
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value
        found.append((int(hwnd), int(owner.value), title))
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(foreach)
    user32.EnumWindows(callback, 0)
    return found


def _audapack_windows(pid: int | None = None, include_hidden: bool = False) -> list[tuple[int, int, str]]:
    user32 = _user32()
    out = []
    for hwnd, owner, title in _top_level_windows():
        if pid is not None and owner != pid:
            continue
        if not include_hidden and not user32.IsWindowVisible(hwnd):
            continue
        if is_audapack_window_title(title):
            out.append((hwnd, owner, title))
    return out


def _audapack_window_pids() -> set[int]:
    """Every PID currently showing an AUDAPACK window. More than one means two
    GUIs, which is the bug a second launch would open."""
    return {owner for _hwnd, owner, _title in _audapack_windows()}


def _visible_window_for_pid(pid: int) -> int | None:
    matches = _audapack_windows(pid=pid)
    return matches[0][0] if matches else None


def _recovery_error_windows(pid: int) -> list[int]:
    return [hwnd for hwnd, owner, title in _top_level_windows()
            if owner == pid and title == RECOVERY_ERROR_TITLE]


def _dismiss(hwnd: int) -> None:
    """Close a probe-owned modal so an unattended case can still read its exit
    code. Only ever called on a window belonging to a PID this script started."""
    try:
        _user32().PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# guard, launcher, lifecycle
# --------------------------------------------------------------------------- #

#: Wait-only access. Deliberately not MUTEX_ALL_ACCESS: this probe must never
#: be able to take ownership of the guard it is measuring.
SYNCHRONIZE = 0x00100000


def _mutex_held(guard: SingleInstance) -> bool:
    """Is the REAL primary named mutex genuinely occupied right now?

    Opened, never created. `CreateMutexW` is the wrong tool twice over: it makes
    this probe the very owner it is measuring, and worse, a probe that
    transiently creates a free guard would race a real launcher into finding it
    held and standing down -- the case would then "pass" for the wrong reason.

    WAIT_TIMEOUT can only mean a live holder. If the object turns out to be free
    or abandoned we momentarily own it, so it is handed straight back.
    """
    from ctypes import wintypes

    from audapack.single_instance import _win32_api

    kernel32 = _win32_api()
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, guard.guard_name())
    if not handle:
        return False  # no such object exists, so nobody holds a guard
    try:
        wait = kernel32.WaitForSingleObject(handle, 0)
        if wait == WAIT_TIMEOUT:
            return True
        if wait in (WAIT_OBJECT_0, WAIT_ABANDONED):
            kernel32.ReleaseMutex(handle)
        return False
    finally:
        kernel32.CloseHandle(handle)


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _sweep_stale_record(guard: SingleInstance) -> None:
    """A record naming a process that no longer exists is not an owner, and it
    would otherwise be the first thing Case D has to overwrite."""
    record = guard.read_owner_record()
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError, OverflowError):
        pid = 0
    if pid and not _process_is_alive(pid) and not _mutex_held(guard):
        guard._owner_record_path().unlink(missing_ok=True)


def _precondition(guard: SingleInstance) -> bool:
    """Nothing may hold the guard before a case plants its own world.

    The probe never kills a process it did not start, so a real AUDAPACK left
    open by the operator is a stop, not something to clean up automatically."""
    if _mutex_held(guard):
        record = guard.read_owner_record()
        print(f"PRECONDITION FAILED: something already holds {guard.guard_name()} "
              f"(owner record names PID {record.get('pid')}).")
        print("Close AUDAPACK completely, then run this again.")
        return False
    _sweep_stale_record(guard)
    return True


def _launcher(capture_stderr: bool = False) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "audapack.app"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
        text=True,
    )


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()


def _shutdown(child: subprocess.Popen) -> None:
    """Ask the app to close like a person would, then insist. Leaves no orphan
    window on the operator's desktop between cases."""
    if child.poll() is not None:
        return
    for hwnd, _owner, _title in _top_level_windows():
        if _owner == child.pid:
            _dismiss(hwnd)
    try:
        child.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    _stop(child)


def _wait_for_window(pid: int, timeout: float, watch: SingleInstance | None = None,
                     watch_pid: int | None = None) -> tuple[int | None, list[int]]:
    """A VISIBLE Project Room window belonging to `pid`.

    PID-scoped, so a pre-existing AUDAPACK window on the operator's desktop
    cannot fake a PASS. When `watch` is given, any modal recovery error that
    appears during the wait is collected -- Case C must prove none ever did."""
    errors: list[int] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        if watch is not None and watch_pid is not None:
            for hwnd in _recovery_error_windows(watch_pid):
                if hwnd not in errors:
                    errors.append(hwnd)
        hwnd = _visible_window_for_pid(pid)
        if hwnd:
            return hwnd, errors
        time.sleep(POLL_SECONDS)
    return None, errors


def _wait_for_exit(child: subprocess.Popen, timeout: float,
                   on_poll=None) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if on_poll is not None:
            on_poll()
        if child.poll() is not None:
            return child.returncode
        time.sleep(POLL_SECONDS)
    return None


def _spawn_windowless_owner(guard: SingleInstance) -> subprocess.Popen:
    """A child that takes the REAL production mutex and puts NOTHING on screen.

    It calls the production `SingleInstance`, so the guard it holds is the same
    object a launcher probes -- not a lookalike, and not a JSON file."""
    child = subprocess.Popen(
        [sys.executable, "-c", _WINDOWLESS_OWNER.format(root=str(ROOT), name=GUARD)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if child.stdout.readline().strip() != "OWNER_READY":
        raise RuntimeError(f"windowless owner never took {guard.guard_name()}: {child.stderr.read()}")
    return child


def _verdict(ok: bool, text: str) -> int:
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} -- {text}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# cases
# --------------------------------------------------------------------------- #

def case_a() -> int:
    """A healthy visible owner: foreground it, kill nothing, open no second GUI."""
    guard = SingleInstance(GUARD)
    if not _precondition(guard):
        return 1
    print("planted nothing; this case launches a real healthy AUDAPACK itself.")

    first = _launcher()
    second = None
    try:
        hwnd, _errors = _wait_for_window(first.pid, WINDOW_TIMEOUT_SECONDS)
        if not hwnd:
            return _verdict(False, f"the launched AUDAPACK never showed a window on PID {first.pid}")
        print(f"real AUDAPACK running as PID {first.pid}, Project Room HWND {hwnd}")

        second = _launcher()
        code = _wait_for_exit(second, LAUNCHER_EXIT_TIMEOUT_SECONDS)
        alive = first.poll() is None
        still = _visible_window_for_pid(first.pid)
        owners = _audapack_window_pids()
        ok = code == 0 and alive and still == hwnd and owners == {first.pid}
        return _verdict(ok, f"second launcher exit={code}; owner PID {first.pid} alive={alive}; "
                            f"window HWND {hwnd} -> {still}; GUI owners on the desktop={sorted(owners)} "
                            f"(exactly one, nothing was terminated)")
    finally:
        if second is not None:
            _shutdown(second)
        _shutdown(first)


def case_b() -> int:
    """An owner still starting: wait the grace, foreground it, kill nothing."""
    guard = SingleInstance(GUARD)
    if not _precondition(guard):
        return 1

    first = _launcher()
    second = None
    try:
        # Barrier on OWNERSHIP, not on the window. The whole point of B is to
        # fire the second launch inside the gap between "the guard is taken" and
        # "the Project Room exists". Racing two blind launches and then ASSUMING
        # the first one won the guard is not a test: when the second wins, the
        # first legitimately owns nothing, never grows a window, and the case
        # silently burns its whole timeout before failing on a lost race.
        if not _wait_until(lambda: _mutex_held(guard), WINDOW_TIMEOUT_SECONDS):
            return _verdict(False, "the first launcher never took the primary guard")
        already_up = _visible_window_for_pid(first.pid)
        owner_pid = guard.read_owner_record().get("pid")
        print(f"first launcher PID {first.pid} holds {guard.guard_name()} "
              f"(record says {owner_pid}); its window was already up: {bool(already_up)}")

        second = _launcher()
        code = _wait_for_exit(second, LAUNCHER_EXIT_TIMEOUT_SECONDS)
        hwnd, _errors = _wait_for_window(first.pid, WINDOW_TIMEOUT_SECONDS)
        alive = first.poll() is None
        owners = _audapack_window_pids()
        ok = (code == 0 and bool(hwnd) and alive
              and owner_pid == first.pid and owners == {first.pid})
        return _verdict(ok, f"racing launcher exit={code}; first owner PID {first.pid} alive={alive}; "
                            f"Project Room HWND {hwnd}; GUI owners={sorted(owners)} "
                            f"(a slow owner is waited for, never killed)")
    finally:
        _shutdown(second)
        _shutdown(first)


def case_c() -> int:
    """The brick itself: a windowless owner self-heals in the SAME click."""
    guard = SingleInstance(GUARD)
    if not _precondition(guard):
        return 1

    brick = _spawn_windowless_owner(guard)
    launcher = None
    try:
        if not _mutex_held(guard):
            return _verdict(False, f"the planted owner did NOT hold {guard.guard_name()}")
        record = guard.read_owner_record()
        verified, why = guard.verify_owner_identity(record)
        if not verified:
            return _verdict(False, f"the planted owner is not even provable: {why}")
        print(f"planted a windowless owner holding {guard.guard_name()} as PID {brick.pid}")
        print("EXPECT ON SCREEN:   NO dialog at all. AUDAPACK opens by itself.")
        print("EXPECT IN TASK MANAGER: the windowless owner is gone.")

        launcher = _launcher()
        hwnd, errors = _wait_for_window(launcher.pid, WINDOW_TIMEOUT_SECONDS,
                                        watch=guard, watch_pid=launcher.pid)
        brick_dead = brick.poll() is not None
        rec = guard.read_owner_record()
        rec_pid = rec.get("pid")
        ok = bool(hwnd) and brick_dead and launcher.poll() is None
        ok = ok and rec_pid == launcher.pid and not errors
        return _verdict(ok, f"fresh GUI HWND {hwnd} on PID {launcher.pid}; "
                            f"windowless owner PID {brick.pid} terminated={brick_dead}; "
                            f"owner record now points at {rec_pid}; "
                            f"'already running'/recovery boxes seen={len(errors)} (must be 0)")
    finally:
        if launcher is not None:
            _shutdown(launcher)
        _stop(brick)


def case_d() -> int:
    """Mutex genuinely HELD, identity genuinely unprovable: kill nothing."""
    guard = SingleInstance(GUARD)
    if not _precondition(guard):
        return 1

    holder = _spawn_windowless_owner(guard)
    launcher = None
    try:
        if not _mutex_held(guard):
            return _verdict(False, f"the child never really held {guard.guard_name()}")
        real = guard.read_owner_record()
        ok_real, _why_real = guard.verify_owner_identity(real)
        if not ok_real:
            return _verdict(False, "the child owner is not provable before tampering; the case is invalid")

        # Break EXACTLY ONE thing: the kernel creation time. Same real PID,
        # same real guard, same live process -- and the record can no longer be
        # tied to it. This is the shape a PID-reuse or a tampered record takes.
        path = guard._owner_record_path()
        broken = dict(real)
        broken["process_creation_time"] = 0
        broken["owner_nonce"] = "probe-unprovable"
        path.write_text(json.dumps(broken, indent=2), encoding="utf-8")
        provable, why = guard.verify_owner_identity()
        if provable:
            return _verdict(False, "the tampered record is still provable; the case proves nothing")
        print(f"the real holder PID {holder.pid} still holds {guard.guard_name()}, "
              f"but its record is now unprovable ({why})")
        print("EXPECT ON SCREEN: one ERROR box saying the holder could not be proven.")
        print("EXPECT: that PID still running, and no AUDAPACK window anywhere.")

        launcher = _launcher(capture_stderr=True)
        seen: list[int] = []

        def _watch() -> None:
            for hwnd in _recovery_error_windows(launcher.pid):
                if hwnd not in seen:
                    seen.append(hwnd)
                    print(f"  recovery ERROR box appeared on screen (HWND {hwnd}); closing it")
                    _dismiss(hwnd)

        code = _wait_for_exit(launcher, LAUNCHER_EXIT_TIMEOUT_SECONDS, on_poll=_watch)
        report = launcher.stderr.read() if launcher.stderr else ""
        if report.strip():
            print(f"  launcher reported: {report.strip()}")
        alive = holder.poll() is None
        window = _visible_window_for_pid(launcher.pid) if launcher.poll() is None else None
        # The ERROR is judged from the launcher's own report, not from catching a
        # modal on camera: an automated desktop may dismiss the box before a
        # 0.25s poll ever sees it, and a gate that loses to that race is not a
        # gate. The box sighting is still printed as on-screen evidence.
        reported = "could not recover" in report.lower() and "NOT proven" in report
        ok = code == 1 and alive and not window and reported
        return _verdict(ok, f"launcher exit={code} (expected 1); the innocent PID {holder.pid} "
                            f"survived={alive}; no GUI opened={not window}; "
                            f"recovery ERROR reported={reported}; "
                            f"ERROR boxes seen on screen={len(seen)}")
    finally:
        if launcher is not None:
            _shutdown(launcher)
        _stop(holder)
        try:
            if str(guard.read_owner_record().get("owner_nonce") or "") == "probe-unprovable":
                guard._owner_record_path().unlink(missing_ok=True)
        except OSError:
            pass


def case_e() -> int:
    """A healthy but HIDDEN owner: restore it in place, same PID, no kill."""
    guard = SingleInstance(GUARD)
    if not _precondition(guard):
        return 1

    first = _launcher()
    second = None
    try:
        hwnd, _errors = _wait_for_window(first.pid, WINDOW_TIMEOUT_SECONDS)
        if not hwnd:
            return _verdict(False, f"the launched AUDAPACK never showed a window on PID {first.pid}")
        record = guard.read_owner_record()
        pid, created = record.get("pid"), record.get("process_creation_time")
        user32 = _user32()
        user32.ShowWindow(hwnd, SW_HIDE)
        hidden_alive = first.poll() is None
        hidden_mutex = _mutex_held(guard)
        hidden_visible = bool(user32.IsWindowVisible(hwnd))
        if not (hidden_alive and hidden_mutex and not hidden_visible):
            return _verdict(False, f"the SW_HIDE precondition did not hold: "
                                   f"alive={hidden_alive}, mutex held={hidden_mutex}, "
                                   f"IsWindowVisible={hidden_visible} (expected False)")
        print(f"hid the Project Room of PID {pid} (created {created}, HWND {hwnd}) with SW_HIDE")
        print("EXPECT ON SCREEN: nothing -- AUDAPACK looks gone, but it is alive and holds the guard.")

        second = _launcher()
        code = _wait_for_exit(second, LAUNCHER_EXIT_TIMEOUT_SECONDS)
        visible = bool(user32.IsWindowVisible(hwnd))
        alive = first.poll() is None
        owners = _audapack_window_pids()
        same_pid = alive and _process_is_alive(pid)
        ok = (code == 0 and visible and same_pid and owners == {pid}
              and hidden_alive and hidden_mutex and not hidden_visible)
        return _verdict(ok, f"second launcher exit={code}; original HWND {hwnd} visible again={visible}; "
                            f"original PID {pid} still alive={same_pid} (nothing was terminated); "
                            f"GUI owners={sorted(owners)}")
    finally:
        if second is not None:
            _shutdown(second)
        _shutdown(first)


CASES = {"A": case_a, "B": case_b, "C": case_c, "D": case_d, "E": case_e}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="all", choices=["A", "B", "C", "D", "E", "all"])
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Windows-only acceptance. Do NOT claim a launcher PASS from Linux.")
        return 1

    order = list(CASES) if args.case == "all" else [args.case]
    results: list[tuple[str, int]] = []
    for key in order:
        print(f"\n=== CASE {key} ===")
        try:
            results.append((key, CASES[key]()))
        except Exception as exc:  # a harness fault is a FAIL, never an abort
            import traceback

            traceback.print_exc()
            print(f"VERDICT: FAIL -- case {key} raised {type(exc).__name__}: {exc}")
            results.append((key, 1))

    print("\n=== SUMMARY ===")
    for key, code in results:
        print(f"  CASE {key}: {'PASS' if code == 0 else 'FAIL'}")
    failed = [key for key, code in results if code != 0]
    print(f"  {len(results) - len(failed)} of {len(results)} case(s) passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
