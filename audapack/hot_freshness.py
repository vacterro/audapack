"""Hot freshness proofs guarded by a continuous source-change monitor (P1 TARGET F).

MEASURED PROBLEM. On the warm unchanged path `ensure_fresh_archive()` ->
`probe_archive_freshness()` -> `build_plan_from_config()` walks the whole source
tree on every Widget ZIP click. Measured on this repository's own tree (2,497
discovered files): ~2.1 s per ensure, against which the archive work is noise.
That walk, not the archive, is the button latency.

WHAT A HOT PROOF IS. Not a timeout. "The archive was fresh N seconds ago" is a
guess, and a guess is exactly what the operator's stale-archive failures were
made of. A proof is a statement with a mechanical warrant:

    a full, authoritative freshness proof or pack completed at generation G of a
    monitor that was armed BEFORE that proof started, stayed continuous, and has
    recorded NO filesystem mutation since.

If any of that is unavailable the proof does not exist and the caller runs the
current full walk. Uncertainty NEVER becomes FRESH:

  * the monitor could not be armed (non-Windows, unreadable root) -> no proof,
    ever, and the authoritative probe serves every request;
  * any mutation (DIRTY) -> generation advances -> the standing proof is
    invalid and the next ensure re-walks; the continuous watch itself SURVIVES,
    so a later authoritative full probe may establish a NEW proof at the new
    generation;
  * buffer overflow / wait or result failure / watch crash / lost continuity /
    a stopped, restarted or replaced monitor -> the monitor is permanently
    UNCERTAIN for its own lifetime -> no proof may ever come from that instance
    again;
  * the packing policy fingerprint changed -> no proof;
  * the archive's path/size/mtime/ctime identity changed -> no proof.

DIRTY vs UNCERTAIN. The two are deliberately different. DIRTY is recoverable:
it advances the generation, invalidates the standing proof and leaves the
continuous watch alive, so the next authoritative probe re-proves and records a
fresh generation. UNCERTAIN (overflow, wait/result failure, crash, stop) is
terminal for the instance: once continuity is lost, no later generation of that
monitor may back a proof, and recovery is a fresh full probe that arms a fresh
monitor.

WINDOWS MECHANISM. `ReadDirectoryChangesW` with `bWatchSubtree=TRUE` on the
source root, issued overlapped so the watch can be cancelled cleanly on stop.
One thread per watched project, bounded by `MAX_MONITORS`. Every notification
class the API offers is requested and EVERY notification marks the project
DIRTY -- we never try to decide whether a mutation touched an INCLUDED file,
because that decision needs the walk this module exists to avoid.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: One monitor per project source. Bridge serving is a handful of projects; the
#: bound keeps a pathological registry from leaving dozens of watcher threads
#: and 64 KiB buffers alive forever. Exceeding it stops the least recently used
#: monitor, which invalidates its proof -- correctness is unaffected, only the
#: hot path is, and one full probe brings it back.
MAX_MONITORS = 16

#: Notification buffer. Large enough that a normal build's event burst fits;
#: overflow is detected and treated as uncertainty rather than truncation.
NOTIFY_BUFFER_BYTES = 64 * 1024

_IS_WINDOWS = sys.platform == "win32"


def monitor_is_supported() -> bool:
    """Whether this platform can back a hot proof at all."""
    return _IS_WINDOWS


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _WindowsNotifyBackend:
    """Recursive `ReadDirectoryChangesW` watch with clean cancellation.

    Implemented with ctypes so the project keeps its empty dependency list. The
    call is issued OVERLAPPED and waited on together with a stop event, because a
    synchronous watch cannot be cancelled: stopping a monitor would leak the
    thread and its handle for the life of the Bridge.
    """

    #: Every class the API exposes. Missing one is a silent false-negative for
    #: DIRTY, which is the one direction we cannot afford to be wrong in.
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_SHARE_ALL = 0x00000007
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _WATCH_FILTER = (
        0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
        | 0x00000002  # FILE_NOTIFY_CHANGE_DIR_NAME
        | 0x00000004  # FILE_NOTIFY_CHANGE_ATTRIBUTES
        | 0x00000008  # FILE_NOTIFY_CHANGE_SIZE
        | 0x00000010  # FILE_NOTIFY_CHANGE_LAST_WRITE
        | 0x00000040  # FILE_NOTIFY_CHANGE_CREATION
        | 0x00000100  # FILE_NOTIFY_CHANGE_SECURITY
    )
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _INFINITE = 0xFFFFFFFF
    _INVALID_HANDLE_VALUE = -1

    def __init__(self, root: Path, monitor: "SourceChangeMonitor"):
        self.root = Path(root)
        self._monitor = monitor
        self._kernel32 = None
        self._handle = None
        self._event = None
        self._stop_event = None
        self._overlapped = None
        self._buffer = None
        #: Arm handshake: set once after the first ReadDirectoryChangesW succeeds
        #: or after it fails. The monitor waits on this before trusting continuity.
        self.armed_event = threading.Event()
        self._arm_succeeded = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> bool:
        """Open the directory handle and the overlapped machinery."""
        import ctypes
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_Overlapped),
            ctypes.c_void_p,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Overlapped),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]

        target = str(self.root)
        if not target:
            return False
        handle = kernel32.CreateFileW(
            target,
            self._FILE_LIST_DIRECTORY,
            self._FILE_SHARE_ALL,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle is None or handle == wintypes.HANDLE(self._INVALID_HANDLE_VALUE).value:
            return False
        # Manual-reset: the stop event stays signalled once set.
        stop_event = kernel32.CreateEventW(None, True, False, None)
        # Auto-reset: WaitForMultipleObjects consumes the completion signal.
        done_event = kernel32.CreateEventW(None, False, False, None)
        if not stop_event or not done_event:
            self._close_handle(kernel32, handle)
            return False
        self._kernel32 = kernel32
        self._handle = handle
        self._stop_event = stop_event
        self._event = done_event
        self._overlapped = _Overlapped()
        self._overlapped.hEvent = done_event
        self._buffer = ctypes.create_string_buffer(NOTIFY_BUFFER_BYTES)
        self._DWORD = wintypes.DWORD
        self._HANDLE = wintypes.HANDLE
        return True

    def run(self, stop_signal: threading.Event) -> None:
        """Block until stopped, reporting every notification as DIRTY."""
        kernel32 = self._kernel32
        if kernel32 is None:
            self._arm_succeeded = False
            self.armed_event.set()
            self._monitor.mark_uncertain("watch was not opened")
            return
        handles = (self._HANDLE * 2)(self._stop_event, self._event)
        first_arm = True
        while not stop_signal.is_set():
            ok = kernel32.ReadDirectoryChangesW(
                self._handle,
                self._buffer,
                NOTIFY_BUFFER_BYTES,
                True,
                self._WATCH_FILTER,
                None,
                self._overlapped,
                None,
            )
            if not ok:
                if first_arm:
                    self._arm_succeeded = False
                    self.armed_event.set()
                self._monitor.mark_uncertain("the source watch could not be armed")
                return
            if first_arm:
                # The OS notification request is now armed. Signal the monitor
                # so start() can trust that continuous observation has begun.
                self._arm_succeeded = True
                self.armed_event.set()
                first_arm = False
            rc = kernel32.WaitForMultipleObjects(
                2, handles, False, self._INFINITE
            )
            if rc == self._WAIT_OBJECT_0:
                kernel32.CancelIoEx(self._handle, self._overlapped)
                return
            if rc != self._WAIT_OBJECT_0 + 1:
                # WAIT_TIMEOUT cannot happen (INFINITE) and any other result is
                # a failed wait: the continuity of this watch is unproven.
                self._monitor.mark_uncertain(f"source watch wait failed ({rc})")
                return
            transferred = self._DWORD(0)
            got = kernel32.GetOverlappedResult(
                self._handle, self._overlapped, transferred, False
            )
            if not got:
                self._monitor.mark_uncertain("source watch result was unreadable")
                return
            if int(transferred.value) == 0:
                # The API's documented overflow signal (ERROR_NOTIFY_ENUM_DIR):
                # the buffer could not describe every change, so the tree's
                # state is unknown. Losing one event is losing the proof.
                self._monitor.mark_uncertain("the source watch buffer overflowed")
                return
            self._monitor.mark_dirty()

    def close(self) -> None:
        kernel32 = self._kernel32
        if kernel32 is None:
            return
        if self._stop_event:
            try:
                kernel32.SetEvent(self._stop_event)
            except Exception:
                pass
        self._close_handle(kernel32, self._handle)
        for handle in (self._stop_event, self._event):
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
        self._handle = None
        self._stop_event = None
        self._event = None

    @staticmethod
    def _close_handle(kernel32, handle) -> None:
        if not handle:
            return
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def _default_backend_factory(root: Path, monitor: "SourceChangeMonitor"):
    """The platform backend for *root*, or None when none is available."""
    if not _IS_WINDOWS:
        # Non-Windows keeps the authoritative full probe for every request. A
        # Linux inotify implementation is a separate, testable change; guessing
        # here would ship an unverified continuity mechanism on the one machine
        # class nobody runs this on.
        return None
    return _WindowsNotifyBackend(root, monitor)


#: Tests swap this to drive DIRTY/overflow/restart deterministically without a
#: real filesystem watcher.
_BACKEND_FACTORY = _default_backend_factory

#: Maximum seconds to wait for the backend to issue its first watch request.
#: Fail-closed: if the deadline expires, start() returns False and the caller
#: falls back to the authoritative full freshness probe.
ARM_DEADLINE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class SourceChangeMonitor:
    """A single continuous recursive watch over one source root.

    Continuity is the whole value. A DIRTY (mutation) event advances the
    generation and invalidates the standing proof but does NOT destroy
    continuity, so a later authoritative probe may re-prove at the new
    generation on this same instance. An UNCERTAIN event -- overflow,
    wait/result failure, watch crash or a stop -- destroys continuity
    permanently for this instance: it may never back a proof again and recovery
    is a fresh instance (see `_monitor_for`), never this one.

    `continuous` is True only while ALL of these hold: the watch armed through
    the explicit handshake, the watcher thread is still running, and no
    continuity-loss event has occurred. A dead watcher must never read as
    proof-eligible, so the raw `_continuous` bit is never trusted alone.
    """

    #: Monotonic ids so a proof can prove WHICH monitor instance it came from.
    _next_id = 0

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._generation = 0
        self._continuous = False
        self._running = False
        self._continuity_lost = False
        #: Monotonic count of continuity-loss events. `start()` snapshots this
        #: before the watcher runs and refuses to commit continuity if it moved,
        #: which is what makes the post-arm loss race impossible: a watcher that
        #: arms and then dies before `start()` commits can never be mistaken for
        #: a live one.
        self._uncertainty_epoch = 0
        self._reason = "not started"
        self._thread: Optional[threading.Thread] = None
        self._stop_signal = threading.Event()
        self._backend = None
        SourceChangeMonitor._next_id += 1
        self.monitor_id = SourceChangeMonitor._next_id
        self.dirty_events = 0
        self.uncertain_events = 0

    # -- state -------------------------------------------------------------

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def continuous(self) -> bool:
        """True only while this instance may back a proof.

        Requires the watch to have armed, the watcher thread to still be
        running, and no continuity-loss event to have occurred. The raw
        `_continuous` bit on a dead watcher is NOT proof-eligible state, so the
        raw bit is never trusted alone.
        """
        with self._lock:
            return self._proof_eligible_locked()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def _proof_eligible_locked(self) -> bool:
        """Whether a proof may be built here. Caller must hold `_lock`."""
        return self._continuous and self._running and not self._continuity_lost

    def snapshot(self) -> tuple[int, bool]:
        """(generation, proof-eligible) in ONE critical section.

        `proof-eligible` means the watch is armed, the watcher thread is still
        live and no continuity-loss event has occurred since startup. Reading
        generation and eligibility separately would let a DIRTY that lands
        between the two reads look like a clean generation.
        """
        with self._lock:
            return self._generation, self._proof_eligible_locked()

    # -- control -----------------------------------------------------------

    def start(self) -> bool:
        """Arm the watch. Returns False when no proof may ever be built here.

        The sequence is:
          1. backend.open() creates directory/event handles;
          2. watcher thread starts;
          3. watcher issues the FIRST successful ReadDirectoryChangesW;
          4. backend signals ARMED;
          5. this method waits for ARMED with a bounded deadline;
          6. only after ARMED: continuous becomes true, start() returns True.

        The commit in step 6 is ATOMIC with the continuity state: it happens
        under the monitor lock and only if the watcher is STILL running, stop
        was not requested, no continuity-loss event was recorded since startup
        began, and the backend's arm still reports success. A watcher that arms
        and then dies (wait failure, result failure, crash, overflow) before
        this commit can therefore never leave the instance claiming
        continuous=True.

        A backend used for hot freshness MUST implement the explicit arm
        handshake (`armed_event` + `_arm_succeeded`). A backend that does not is
        NOT trusted -- start() fails closed rather than inferring observation
        continuity from "the thread started".

        An instance that already lost continuity can never be restarted; the
        caller retires it and arms a fresh one.
        """
        with self._lock:
            if self._running:
                return self._proof_eligible_locked()
            if self._continuity_lost:
                # Terminal for this instance: recovery is a fresh monitor.
                self._reason = self._reason or "continuity was lost"
                return False
        backend = _BACKEND_FACTORY(self.root, self)
        if backend is None:
            with self._lock:
                self._continuous = False
                self._reason = "no source-change backend on this platform"
            return False
        # The arm handshake is mandatory. Without it there is no mechanical
        # warrant that observation began before the proof, so fail closed and
        # let the caller use the authoritative full freshness path.
        if getattr(backend, "armed_event", None) is None:
            with self._lock:
                self._continuous = False
                self._reason = "the source-change backend has no arm handshake"
            return False
        try:
            opened = backend.open()
        except Exception as exc:  # noqa: BLE001 - any failure means no proof
            with self._lock:
                self._continuous = False
                self._reason = f"source watch could not start: {exc}"
            return False
        if not opened:
            with self._lock:
                self._continuous = False
                self._reason = "source watch could not open the source root"
            return False
        self._backend = backend
        thread = threading.Thread(
            target=self._run,
            name=f"audapack-source-watch-{self.monitor_id}",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
            self._running = True
            # _continuous stays False until the backend confirms ARMED.
            self._continuous = False
            self._reason = "waiting for watch arm"
            # Snapshot the continuity epoch BEFORE the watcher runs. Any loss
            # recorded from here on must be visible at the commit below, so a
            # post-arm death cannot be overwritten by a stale commit.
            startup_epoch = self._uncertainty_epoch
        thread.start()
        # Wait for the backend to issue its first ReadDirectoryChangesW.
        # The backend sets armed_event on success or failure; we distinguish
        # via _arm_succeeded. A timeout is treated as failure (fail-closed).
        armed = backend.armed_event.wait(timeout=ARM_DEADLINE_SECONDS)
        # Commit continuity ATOMICALLY and only for a watch that is still
        # provably live. This is the post-arm race: arming can succeed and the
        # watcher can die BEFORE this method gets here, and that must never be
        # mistaken for continuous observation.
        with self._lock:
            # Read the backend's arm result under the monitor lock: it is set
            # once before ARMED is signalled, and this keeps every input to the
            # continuity commit in one synchronized section.
            backend_arm_ok = bool(getattr(backend, "_arm_succeeded", False))
            if (
                armed
                and backend_arm_ok
                and self._running
                and not self._stop_signal.is_set()
                and not self._continuity_lost
                and self._uncertainty_epoch == startup_epoch
            ):
                self._continuous = True
                self._reason = ""
                return True
            # Continuity was already lost, or the watch never became live.
            # Never allow this commit to overwrite an earlier mark_uncertain().
            self._continuous = False
            if not armed:
                reason = "source watch arm deadline expired"
            elif not backend_arm_ok:
                reason = "source watch arm failed"
            elif not self._running:
                reason = "the source watch died during arm"
            elif self._stop_signal.is_set():
                reason = "stopped during arm"
            else:
                reason = "the source watch lost continuity during arm"
            self._reason = reason
        self.stop()
        with self._lock:
            # stop() overwrites the reason; keep the diagnostic that explains
            # why this instance may never back a proof.
            self._continuity_lost = True
            self._reason = reason
        return False

    def _run(self) -> None:
        with self._lock:
            epoch_at_entry = self._uncertainty_epoch
        try:
            self._backend.run(self._stop_signal)
        except Exception as exc:  # noqa: BLE001 - a crashed watch is uncertainty
            self.mark_uncertain(f"source watch crashed: {exc}")
        finally:
            with self._lock:
                self._running = False
                # A watcher that ends without a stop request and without having
                # recorded why still ended: silent termination is lost
                # continuity, so this instance may never back a proof again.
                if (
                    not self._stop_signal.is_set()
                    and self._uncertainty_epoch == epoch_at_entry
                ):
                    self._continuous = False
                    self._continuity_lost = True
                    self._uncertainty_epoch += 1
                    self.uncertain_events += 1
                    self._reason = "the source watch ended without a stop or a reason"

    def stop(self) -> None:
        with self._lock:
            already_stopped = not self._running and not self._continuous
            self._stop_signal.set()
            # A stopped watch can never resume proving: recovery is a fresh
            # instance. Marking continuity lost here is what makes that true
            # even if the thread was already gone.
            self._continuity_lost = True
        if already_stopped:
            return
        backend = self._backend
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._continuous = False
            self._running = False
            self._reason = "stopped"

    # -- event intake ------------------------------------------------------

    def mark_dirty(self) -> None:
        """A filesystem mutation was observed: DIRTY, not UNCERTAIN.

        Advances the generation -- invalidating any standing proof -- but leaves
        continuity intact, so a later authoritative full probe may establish a
        new proof at the new generation on this same instance.
        """
        with self._lock:
            self._generation += 1
            self.dirty_events += 1

    def mark_uncertain(self, reason: str) -> None:
        """Continuity is lost. This instance can never back a proof again."""
        with self._lock:
            self._continuous = False
            self._continuity_lost = True
            self._uncertainty_epoch += 1
            self.uncertain_events += 1
            self._reason = str(reason or "continuity lost")


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HotProof:
    """A verified pack/probe plus the monitor generation that witnessed it."""

    source_root: str
    policy_fingerprint: str
    archive_path: str
    archive_size: int
    archive_mtime_ns: int
    archive_ctime_ns: int
    monitor_id: int
    monitor_generation: int
    fidelity_profile: str
    archive_semantics: str


@dataclass
class _ArmToken:
    """What `begin` hands back so `confirm` can prove nothing happened since."""

    source_root: str
    monitor: SourceChangeMonitor
    monitor_id: int
    generation: int
    continuous: bool


_LOCK = threading.RLock()
_MONITORS: dict[str, SourceChangeMonitor] = {}
_MONITOR_ORDER: list[str] = []
_PROOFS: dict[str, HotProof] = {}


def _key(source_root) -> str:
    try:
        resolved = Path(source_root).resolve()
    except OSError:
        resolved = Path(source_root)
    text = str(resolved)
    return text.lower() if os.name == "nt" else text


def _touch(key: str) -> None:
    if key in _MONITOR_ORDER:
        _MONITOR_ORDER.remove(key)
    _MONITOR_ORDER.append(key)


def _evict_over_capacity() -> None:
    while len(_MONITORS) > MAX_MONITORS:
        victim = _MONITOR_ORDER.pop(0)
        monitor = _MONITORS.pop(victim, None)
        _PROOFS.pop(victim, None)
        if monitor is not None:
            monitor.stop()


def _monitor_for(key: str, source_root) -> Optional[SourceChangeMonitor]:
    """The live continuous monitor for *key*, starting one when necessary.

    A proof-capable existing monitor must satisfy BOTH `continuous` AND
    `running`. `continuous=True, running=False` is invalid internal state -- a
    dead watcher whose stale continuous bit was never cleared -- and is treated
    as untrustworthy: its proof is dropped, it is retired, and a fresh monitor
    is started (falling back to authoritative probing when that fails). A dead
    monitor is NEVER reused solely because its continuous bit is stale.
    """
    monitor = _MONITORS.get(key)
    if monitor is not None and monitor.continuous and monitor.running:
        _touch(key)
        return monitor
    if monitor is not None:
        # Any existing monitor that is not provably live AND continuous can
        # never back a proof again; retire it and drop its proof.
        _MONITORS.pop(key, None)
        _PROOFS.pop(key, None)
        monitor.stop()
    monitor = SourceChangeMonitor(Path(source_root))
    if not monitor.start():
        _MONITORS.pop(key, None)
        _PROOFS.pop(key, None)
        return None
    _MONITORS[key] = monitor
    _touch(key)
    _evict_over_capacity()
    return monitor


def begin(source_root, policy_fingerprint: str) -> Optional[_ArmToken]:
    """Arm a watch BEFORE the authoritative proof, or say it cannot be done.

    Callers must hold the token across the full probe/pack and then call
    `confirm`. Arming first is what makes the proof sound: two consecutive
    clean generations mean no mutation happened DURING the proof, not merely
    that none happened after it.
    """
    if not source_root:
        return None
    key = _key(source_root)
    with _LOCK:
        monitor = _monitor_for(key, source_root)
        if monitor is None:
            return None
        generation, continuous = monitor.snapshot()
        if not continuous:
            # The watch stopped being proof-eligible between selection and here
            # (it died right after arming, for example). Fail closed: no token.
            _MONITORS.pop(key, None)
            _PROOFS.pop(key, None)
            monitor.stop()
            return None
        return _ArmToken(
            source_root=key,
            monitor=monitor,
            monitor_id=monitor.monitor_id,
            generation=generation,
            continuous=continuous,
        )


def confirm(
    token: Optional[_ArmToken],
    archive,
    policy_fingerprint: str,
    *,
    fidelity_profile: str = "",
    archive_semantics: str = "",
) -> bool:
    """Record a proof for *archive* iff the armed watch stayed clean throughout."""
    if token is None or archive is None:
        return False
    if not token.continuous:
        return False
    monitor = token.monitor
    with _LOCK:
        current = _MONITORS.get(token.source_root)
        # `snapshot` returns proof-eligibility, which requires a STILL-LIVE
        # watcher with no continuity loss -- a dead monitor can never confirm.
        generation, continuous = monitor.snapshot()
        if current is not monitor or monitor.monitor_id != token.monitor_id:
            return False
        if not continuous or generation != token.generation:
            return False
        identity = _archive_identity(archive)
        if identity is None:
            return False
        _PROOFS[token.source_root] = HotProof(
            source_root=token.source_root,
            policy_fingerprint=str(policy_fingerprint or ""),
            archive_path=identity["path"],
            archive_size=identity["size"],
            archive_mtime_ns=identity["mtime_ns"],
            archive_ctime_ns=identity["ctime_ns"],
            monitor_id=monitor.monitor_id,
            monitor_generation=generation,
            fidelity_profile=str(fidelity_profile or ""),
            archive_semantics=str(archive_semantics or ""),
        )
        return True


def _archive_identity(archive) -> Optional[dict]:
    try:
        stat = Path(archive).stat()
    except OSError:
        return None
    text = str(Path(archive))
    return {
        "path": text.lower() if os.name == "nt" else text,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
    }


def lookup(source_root, policy_fingerprint: str, archive) -> Optional[HotProof]:
    """The still-valid proof for this source + policy + archive, or None.

    Every clause is a way the proof could have been invalidated, and each one is
    checked here rather than trusted from an earlier decision.
    """
    if not source_root or archive is None:
        return None
    key = _key(source_root)
    with _LOCK:
        proof = _PROOFS.get(key)
        if proof is None:
            return None
        monitor = _MONITORS.get(key)
        if monitor is None or monitor.monitor_id != proof.monitor_id:
            return None
        # Proof-eligibility, not the raw bit: a dead watcher (or one that lost
        # continuity) invalidates the proof even when the generation is intact.
        generation, continuous = monitor.snapshot()
        if not continuous or generation != proof.monitor_generation:
            return None
        if proof.policy_fingerprint != str(policy_fingerprint or ""):
            return None
        identity = _archive_identity(archive)
        if identity is None:
            return None
        if (
            identity["path"] != proof.archive_path
            or identity["size"] != proof.archive_size
            or identity["mtime_ns"] != proof.archive_mtime_ns
            or identity["ctime_ns"] != proof.archive_ctime_ns
        ):
            return None
        return proof


def invalidate(source_root) -> None:
    """Drop the proof for one source (an explicit, external invalidation)."""
    if not source_root:
        return
    with _LOCK:
        _PROOFS.pop(_key(source_root), None)


def diagnostics() -> dict:
    """Bounded, token-free observability for the hot path."""
    with _LOCK:
        return {
            "supported": monitor_is_supported(),
            "monitors": [
                {
                    "source_root": key,
                    "monitor_id": monitor.monitor_id,
                    "generation": monitor.generation,
                    "continuous": monitor.continuous,
                    "running": monitor.running,
                    "reason": monitor.reason,
                    "dirty_events": monitor.dirty_events,
                    "uncertain_events": monitor.uncertain_events,
                    "proof": key in _PROOFS,
                }
                for key, monitor in _MONITORS.items()
            ],
        }


def reset() -> None:
    """Stop every monitor and drop every proof (tests, and explicit recovery)."""
    with _LOCK:
        monitors = list(_MONITORS.values())
        _MONITORS.clear()
        _MONITOR_ORDER.clear()
        _PROOFS.clear()
    for monitor in monitors:
        monitor.stop()
