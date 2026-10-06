"""P1 TARGET F: hot freshness proofs must never be able to serve a stale archive.

The measured problem: on the warm unchanged path `ensure_fresh_archive()` walked
the entire source tree on every Widget click (~2.1 s on this repository's own
2,497-file tree). The accelerator that removes that walk is only acceptable if
EVERY way it could be wrong invalidates it. That is what this file pins:

    continuity, cleanliness, policy identity, archive identity, and an
    authoritative fallback when no monitor is available at all.

The forbidden shortcut this suite exists to prevent is "the archive was fresh N
seconds ago, therefore it is still fresh". There is no clock in the hot decision.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from audapack import hot_freshness
from audapack.config import AppConfig, PackingConfig
from audapack.models import Project


class FakeBackend:
    """A deterministic stand-in for the Windows directory watch.

    It never observes the filesystem: the test drives DIRTY and OVERFLOW
    explicitly, which is the only way to assert those transitions exactly.

    Arms immediately on ``run()`` entry so existing tests are unaffected by the
    arm-handshake protocol added to close the pre-arm race defect.
    """

    instances: list["FakeBackend"] = []

    def __init__(self, root: Path, monitor: hot_freshness.SourceChangeMonitor):
        self.root = Path(root)
        self.monitor = monitor
        self.opened = False
        self.closed = False
        self._stop = threading.Event()
        #: Arm handshake protocol — arms immediately.
        self.armed_event = threading.Event()
        self._arm_succeeded = False
        FakeBackend.instances.append(self)

    def open(self) -> bool:
        self.opened = True
        return True

    def run(self, stop_signal: threading.Event) -> None:
        # Arm immediately so the monitor's start() proceeds.
        self._arm_succeeded = True
        self.armed_event.set()
        while not stop_signal.is_set():
            time.sleep(0.005)

    def close(self) -> None:
        self.closed = True
        self._stop.set()


@pytest.fixture
def monitors(monkeypatch):
    FakeBackend.instances = []
    monkeypatch.setattr(hot_freshness, "_BACKEND_FACTORY", FakeBackend)
    yield FakeBackend
    hot_freshness.reset()


@pytest.fixture
def workspace():
    root = Path(tempfile.mkdtemp(prefix="audapack_hotfresh_"))
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")
    (root / "out").mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _archive(root: Path, name: str = "proj.zip", payload: bytes = b"PK\x03\x04 latest") -> Path:
    path = root / "out" / name
    path.write_bytes(payload)
    return path


def _confirm(source: Path, fingerprint: str, archive: Path, **kwargs) -> bool:
    token = hot_freshness.begin(str(source), fingerprint)
    assert token is not None
    return hot_freshness.confirm(token, archive, fingerprint, **kwargs)


# --------------------------------------------------------------------------- #
# TARGET M.7 / M.18: proof exists only with continuity; no proof without it
# --------------------------------------------------------------------------- #


def test_a_proof_requires_a_clean_continuous_monitor(workspace, monitors):
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True

    proof = hot_freshness.lookup(str(source), "policy-a", archive)
    assert proof is not None
    assert proof.archive_size == archive.stat().st_size
    assert len(monitors.instances) == 1
    assert monitors.instances[0].opened is True


def test_no_monitor_means_no_proof_and_the_authoritative_path_stays_in_charge(
    workspace, monkeypatch
):
    """TARGET M.18: with acceleration unavailable behaviour is unchanged."""
    monkeypatch.setattr(hot_freshness, "_BACKEND_FACTORY", lambda root, monitor: None)
    source = workspace / "src"
    archive = _archive(workspace)

    assert hot_freshness.begin(str(source), "policy-a") is None
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    assert hot_freshness.diagnostics()["monitors"] == []


def test_a_monitor_that_loses_continuity_can_never_back_a_proof_again(workspace, monitors):
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True
    monitor = monitors.instances[0].monitor

    monitor.mark_uncertain("buffer overflowed")
    assert monitor.continuous is False
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None

    # Recovery is a FRESH instance that has to prove itself again, never the
    # uncertain one: the old proof is not resurrected by a new generation.
    monitor.mark_dirty()
    probe = hot_freshness.begin(str(source), "policy-a")
    assert probe is not None
    assert probe.monitor is not monitor
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    assert hot_freshness.confirm(probe, archive, "policy-a") is True
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None


# --------------------------------------------------------------------------- #
# TARGET M.8 / M.9 / M.10: every invalidation path
# --------------------------------------------------------------------------- #


def test_a_dirty_event_invalidates_the_hot_proof(workspace, monitors):
    """TARGET M.8."""
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None

    monitors.instances[0].monitor.mark_dirty()
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_a_watcher_overflow_invalidates_the_hot_proof(workspace, monitors):
    """TARGET M.9: an overflow means the buffer could not describe every change,
    so the tree's state is unknown -- not 'probably fine'."""
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True

    monitors.instances[0].monitor.mark_uncertain("the source watch buffer overflowed")
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    assert hot_freshness.diagnostics()["monitors"][0]["continuous"] is False


def test_a_watcher_restart_invalidates_the_hot_proof(workspace, monitors):
    """TARGET M.10: a restarted watch proves nothing about the gap it missed."""
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True
    first = monitors.instances[0].monitor

    first.stop()
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None

    second_token = hot_freshness.begin(str(source), "policy-a")
    assert second_token is not None
    assert second_token.monitor.monitor_id != first.monitor_id
    # A new instance does not inherit the old proof: confirmation is required.
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    assert hot_freshness.confirm(second_token, archive, "policy-a") is True
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None


def test_a_policy_change_invalidates_the_hot_proof(workspace, monitors):
    """TARGET M.11."""
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True
    assert hot_freshness.lookup(str(source), "policy-b", archive) is None
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None


def test_a_changed_archive_identity_invalidates_the_hot_proof(workspace, monitors):
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True

    archive.write_bytes(b"PK\x03\x04 a repacked generation")
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_a_disappeared_archive_invalidates_the_hot_proof(workspace, monitors):
    source = workspace / "src"
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True
    archive.unlink()
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_a_mutation_during_the_probe_cannot_be_confirmed(workspace, monitors):
    """The whole soundness argument: the watch is armed BEFORE the probe, so a
    change the probe never saw is not a clean generation."""
    source = workspace / "src"
    archive = _archive(workspace)
    token = hot_freshness.begin(str(source), "policy-a")
    assert token is not None

    monitors.instances[0].monitor.mark_dirty()
    assert hot_freshness.confirm(token, archive, "policy-a") is False
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_a_proof_for_one_source_never_serves_another(workspace, monitors):
    source = workspace / "src"
    other = workspace / "other"
    other.mkdir()
    archive = _archive(workspace)
    assert _confirm(source, "policy-a", archive) is True
    assert _confirm(other, "policy-a", archive) is True
    assert hot_freshness.lookup(str(other), "policy-a", archive) is not None

    monitors.instances[-1].monitor.mark_dirty()
    assert hot_freshness.lookup(str(other), "policy-a", archive) is None
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None


def test_monitor_count_is_bounded(workspace, monkeypatch, monitors):
    """A burst of distinct sources must not leave an unbounded set of watcher
    threads alive. Eviction costs the hot path only; correctness is the walk."""
    from audapack import hot_freshness as module

    monkeypatch.setattr(module, "MAX_MONITORS", 3)
    archive = _archive(workspace)
    keys = []
    for index in range(6):
        directory = workspace / f"src{index}"
        directory.mkdir()
        keys.append(str(directory))
        assert _confirm(directory, "policy-a", archive) is True

    monitors_now = hot_freshness.diagnostics()["monitors"]
    assert len(monitors_now) <= 3
    # Every evicted monitor was stopped, so its proof is gone too.
    stopped = [item for item in monitors.instances if item.closed]
    assert len(stopped) >= 3
    assert hot_freshness.lookup(keys[-1], "policy-a", archive) is not None


# --------------------------------------------------------------------------- #
# Integration: the reused-archive path used by the Bridge's ensure endpoint
# --------------------------------------------------------------------------- #


def _service(root: Path, source: Path, output: Path):
    from audapack.services.packing_service import PackingService

    config = AppConfig(packing=PackingConfig(output_dir=str(output), delete_old=True))
    config.projects = [Project(id="proj", display_name="proj", source_path=str(source), archive_name="proj")]
    return PackingService(config, base_dir=root), config


def test_the_first_cold_pack_makes_the_next_unchanged_ensure_hot(workspace, monitors, monkeypatch):
    """TARGET L: after a cold pack the NEXT unchanged click is the hot path, and
    the hot path performs NO source walk at all."""
    from audapack.services import packing_service as service_module

    source = workspace / "src"
    service, _config = _service(workspace, source, workspace / "out")

    probes = {"count": 0}
    real_probe = service_module.probe_archive_freshness

    def counting_probe(*args, **kwargs):
        probes["count"] += 1
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(service_module, "probe_archive_freshness", counting_probe)

    packed = service.ensure_fresh_archive("proj")
    assert packed.success is True
    assert packed.packed is True
    assert probes["count"] == 1, "the cold path is allowed exactly one walk"

    warm = service.ensure_fresh_archive("proj")
    assert warm.success is True
    assert warm.reused is True
    assert warm.timings.get("hot_proof") is True
    assert warm.timings.get("source_walk_skipped") is True
    assert warm.timings.get("freshness_probe_ms") == 0.0
    assert probes["count"] == 1, "the hot path must not walk the source tree"


def test_a_mutation_returns_the_warm_path_to_the_authoritative_walk(workspace, monitors, monkeypatch):
    """TARGET M.17: no fast path may serve a stale changed project archive."""
    from audapack.services import packing_service as service_module

    source = workspace / "src"
    service, _config = _service(workspace, source, workspace / "out")
    probes = {"count": 0}
    real_probe = service_module.probe_archive_freshness

    def counting_probe(*args, **kwargs):
        probes["count"] += 1
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(service_module, "probe_archive_freshness", counting_probe)

    first = service.ensure_fresh_archive("proj")
    assert first.packed is True
    assert probes["count"] == 1

    # A DIRTY event is the monitor's whole job: it buys back the walk only while
    # it can prove nothing changed.
    monitors.instances[0].monitor.mark_dirty()
    after_dirty = service.ensure_fresh_archive("proj")
    assert after_dirty.success is True
    assert after_dirty.timings.get("source_walk_skipped") is False
    assert probes["count"] == 2
    assert after_dirty.reused is True, "an unchanged tree is still a reuse, just a proven one"

    # Now change the source for real. The monitor reports the write, exactly as
    # the Windows watch does; the next ensure must re-walk, see the change and
    # repack -- a fast path may never hand back the previous generation.
    old_archive = Path(after_dirty.output_path)
    old_mtime = old_archive.stat().st_mtime
    (source / "main.py").write_text("print('changed')\n", encoding="utf-8")
    os.utime(source / "main.py", (time.time() + 5, time.time() + 5))
    monitors.instances[0].monitor.mark_dirty()
    changed = service.ensure_fresh_archive("proj")
    assert changed.success is True
    assert changed.packed is True
    assert changed.reused is False
    assert probes["count"] == 3
    assert Path(changed.output_path).stat().st_mtime >= old_mtime

    # And the pack that just completed is itself the next hot proof.
    again = service.ensure_fresh_archive("proj")
    assert again.reused is True
    assert again.timings.get("source_walk_skipped") is True
    assert probes["count"] == 3


def test_an_unavailable_monitor_leaves_the_authoritative_path_identical(workspace, monkeypatch):
    """TARGET M.18, at the service boundary: with no accelerator every ensure is
    the full probe, and it still answers FRESH/reuse correctly."""
    monkeypatch.setattr(hot_freshness, "_BACKEND_FACTORY", lambda root, monitor: None)
    source = workspace / "src"
    service, _config = _service(workspace, source, workspace / "out")

    packed = service.ensure_fresh_archive("proj")
    assert packed.packed is True
    warm = service.ensure_fresh_archive("proj")
    assert warm.reused is True
    assert warm.timings.get("hot_proof") is False
    assert warm.timings.get("source_walk_skipped") is False
    assert warm.timings.get("freshness_probe_ms") > 0


# --------------------------------------------------------------------------- #
# TARGET D: deterministic pre-arm regression tests (watch-arm handshake)
# --------------------------------------------------------------------------- #


class DelayedArmBackend:
    """A backend whose arming is gated by an explicit threading.Event.

    The test controls WHEN and WHETHER the arm succeeds, which is the only way
    to prove that the monitor does not report proof-eligible continuity before
    the backend has actually issued a watch request.
    """

    instances: list["DelayedArmBackend"] = []

    def __init__(self, root: Path, monitor: hot_freshness.SourceChangeMonitor):
        self.root = Path(root)
        self.monitor = monitor
        self.opened = False
        self.closed = False
        self._stop = threading.Event()
        #: Arm handshake protocol.
        self.armed_event = threading.Event()
        self._arm_succeeded = False
        #: Test controls: arm_gate blocks run() until released; fail_arm forces failure.
        self.arm_gate = threading.Event()
        self.fail_arm = False
        self.crash_before_arm = False
        DelayedArmBackend.instances.append(self)

    def open(self) -> bool:
        self.opened = True
        return True

    def run(self, stop_signal: threading.Event) -> None:
        # Wait for the test to release the arm gate OR for stop.
        while not self.arm_gate.is_set() and not stop_signal.is_set():
            self.arm_gate.wait(timeout=0.01)
        if self.crash_before_arm:
            raise RuntimeError("simulated crash before arm")
        if stop_signal.is_set() and not self.arm_gate.is_set():
            # Stopped before arm gate released.
            self._arm_succeeded = False
            self.armed_event.set()
            return
        if self.fail_arm:
            self._arm_succeeded = False
            self.armed_event.set()
            self.monitor.mark_uncertain("simulated arm failure")
            return
        # Arm succeeds.
        self._arm_succeeded = True
        self.armed_event.set()
        while not stop_signal.is_set():
            time.sleep(0.005)

    def close(self) -> None:
        self.closed = True
        self.arm_gate.set()  # unblock run() if waiting
        self._stop.set()


@pytest.fixture
def delayed_monitors(monkeypatch):
    DelayedArmBackend.instances = []
    monkeypatch.setattr(hot_freshness, "_BACKEND_FACTORY", DelayedArmBackend)
    monkeypatch.setattr(hot_freshness, "ARM_DEADLINE_SECONDS", 1.0)
    yield DelayedArmBackend
    hot_freshness.reset()


def test_d1_blocked_arm_means_monitor_not_continuous(workspace, delayed_monitors):
    """D.1: open succeeds but first arm is deliberately blocked;
    SourceChangeMonitor must not report continuous yet."""
    source = workspace / "src"
    monitor = hot_freshness.SourceChangeMonitor(source)
    # Don't release the arm gate -- start() must time out.
    result = monitor.start()
    assert result is False
    assert monitor.continuous is False
    assert "deadline" in monitor.reason or "arm" in monitor.reason or "stopped" in monitor.reason
    monitor.stop()


def test_d2_start_not_proof_eligible_before_armed(workspace, delayed_monitors):
    """D.2: start() does not return proof-eligible True before ARMED."""
    source = workspace / "src"
    monitor = hot_freshness.SourceChangeMonitor(source)
    # Arm will time out.
    started = monitor.start()
    assert started is False
    assert monitor.continuous is False
    monitor.stop()


def test_d3_begin_cannot_obtain_token_before_armed(workspace, delayed_monitors):
    """D.3: begin() cannot obtain an eligible token before ARMED."""
    source = workspace / "src"
    # start() will fail because arm times out.
    with hot_freshness._LOCK:
        hot_freshness._MONITORS.pop(hot_freshness._key(source), None)
    token = hot_freshness.begin(str(source), "policy-a")
    # begin calls _monitor_for which calls start(); start times out -> None
    assert token is None
    hot_freshness.reset()


def test_d4_releasing_arm_permits_startup(workspace, delayed_monitors):
    """D.4: releasing the arm permits startup."""
    source = workspace / "src"
    backend_holder = []

    original_factory = DelayedArmBackend

    def capturing_factory(root, monitor):
        b = original_factory(root, monitor)
        backend_holder.append(b)
        # Release arm gate immediately to simulate fast arm.
        b.arm_gate.set()
        return b

    delayed_monitors.instances = []
    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = capturing_factory
    try:
        monitor = hot_freshness.SourceChangeMonitor(source)
        result = monitor.start()
        assert result is True
        assert monitor.continuous is True
        monitor.stop()
    finally:
        mod._BACKEND_FACTORY = old


def test_d5_first_arm_failure_returns_false(workspace, delayed_monitors):
    """D.5: first-arm failure -> start() False."""
    source = workspace / "src"

    def failing_factory(root, monitor):
        b = DelayedArmBackend(root, monitor)
        b.fail_arm = True
        b.arm_gate.set()  # let run() proceed immediately
        return b

    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = failing_factory
    try:
        monitor = hot_freshness.SourceChangeMonitor(source)
        result = monitor.start()
        assert result is False
        assert monitor.continuous is False
    finally:
        mod._BACKEND_FACTORY = old


def test_d6_watcher_crash_before_arm_returns_false(workspace, delayed_monitors):
    """D.6: watcher crash before arm -> start() False."""
    source = workspace / "src"

    def crashing_factory(root, monitor):
        b = DelayedArmBackend(root, monitor)
        b.crash_before_arm = True
        b.arm_gate.set()  # let run() proceed
        return b

    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = crashing_factory
    try:
        monitor = hot_freshness.SourceChangeMonitor(source)
        result = monitor.start()
        assert result is False
        assert monitor.continuous is False
    finally:
        mod._BACKEND_FACTORY = old


def test_d7_arm_timeout_returns_false(workspace, delayed_monitors):
    """D.7: arm timeout -> start() False."""
    source = workspace / "src"
    monitor = hot_freshness.SourceChangeMonitor(source)
    # arm_gate never released -> times out
    result = monitor.start()
    assert result is False
    assert monitor.continuous is False
    monitor.stop()


def test_d8_stop_before_arm_returns_false(workspace, delayed_monitors):
    """D.8: stop before arm -> start() False."""
    source = workspace / "src"

    stop_called = threading.Event()

    def stopping_factory(root, monitor):
        b = DelayedArmBackend(root, monitor)
        # Don't release arm_gate. The test will stop the monitor from another
        # thread while start() is waiting.
        return b

    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = stopping_factory
    try:
        monitor = hot_freshness.SourceChangeMonitor(source)

        def stop_after_delay():
            time.sleep(0.05)
            monitor.stop()
            stop_called.set()

        t = threading.Thread(target=stop_after_delay, daemon=True)
        t.start()
        result = monitor.start()
        # Either returns False from timeout or from stop-before-arm.
        assert result is False
        assert monitor.continuous is False
        t.join(timeout=2)
    finally:
        mod._BACKEND_FACTORY = old


def test_d9_successful_arm_retains_generation_proof_behavior(workspace, delayed_monitors):
    """D.9: successful arm retains current generation/proof behavior."""
    source = workspace / "src"
    archive = _archive(workspace)

    def fast_factory(root, monitor):
        b = DelayedArmBackend(root, monitor)
        b.arm_gate.set()
        return b

    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = fast_factory
    try:
        assert _confirm(source, "policy-a", archive) is True
        proof = hot_freshness.lookup(str(source), "policy-a", archive)
        assert proof is not None
        assert proof.archive_size == archive.stat().st_size
    finally:
        mod._BACKEND_FACTORY = old


def test_d10_mutation_after_arm_invalidates_proof(workspace, delayed_monitors):
    """D.10: mutation after arm invalidates proof."""
    source = workspace / "src"
    archive = _archive(workspace)

    def fast_factory(root, monitor):
        b = DelayedArmBackend(root, monitor)
        b.arm_gate.set()
        return b

    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = fast_factory
    try:
        assert _confirm(source, "policy-a", archive) is True
        assert hot_freshness.lookup(str(source), "policy-a", archive) is not None
        # Simulate mutation.
        delayed_monitors.instances[0].monitor.mark_dirty()
        assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    finally:
        mod._BACKEND_FACTORY = old


def test_d11_overflow_lost_continuity_remains_fail_closed(workspace, delayed_monitors):
    """D.11: overflow/lost continuity remains fail-closed."""
    source = workspace / "src"
    archive = _archive(workspace)

    def fast_factory(root, monitor):
        b = DelayedArmBackend(root, monitor)
        b.arm_gate.set()
        return b

    import audapack.hot_freshness as mod
    old = mod._BACKEND_FACTORY
    mod._BACKEND_FACTORY = fast_factory
    try:
        assert _confirm(source, "policy-a", archive) is True
        delayed_monitors.instances[0].monitor.mark_uncertain("buffer overflowed")
        assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    finally:
        mod._BACKEND_FACTORY = old


def test_red_control_old_code_was_continuous_before_armed():
    """RED CONTROL: demonstrates that the pre-fix implementation (setting
    _continuous = True at thread.start()) would report continuous before the
    backend has actually armed. The current implementation correctly waits.

    This test constructs a backend that never arms and proves that the monitor
    does NOT become continuous -- which would have been wrong before the fix.
    """
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="audapack_red_ctrl_"))

    class NeverArmBackend:
        """A backend that opens but never arms."""

        def __init__(self, r, monitor):
            self.armed_event = threading.Event()
            self._arm_succeeded = False

        def open(self):
            return True

        def run(self, stop_signal):
            # Never arm, never signal armed_event.
            while not stop_signal.is_set():
                time.sleep(0.01)

        def close(self):
            pass

    old_factory = hot_freshness._BACKEND_FACTORY
    old_deadline = hot_freshness.ARM_DEADLINE_SECONDS
    hot_freshness._BACKEND_FACTORY = NeverArmBackend
    hot_freshness.ARM_DEADLINE_SECONDS = 0.1  # fast timeout for test
    try:
        monitor = hot_freshness.SourceChangeMonitor(root)
        result = monitor.start()
        # The fix: start() returns False because the backend never armed.
        # Pre-fix: start() returned True and _continuous was True immediately.
        assert result is False, (
            "RED CONTROL FAILED: monitor reported continuous before backend armed. "
            "This is the exact pre-arm race condition this fix closes."
        )
        assert monitor.continuous is False
        monitor.stop()
    finally:
        hot_freshness._BACKEND_FACTORY = old_factory
        hot_freshness.ARM_DEADLINE_SECONDS = old_deadline
        hot_freshness.reset()
        import shutil
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# TARGET A/B/C: the post-arm continuity race, dead-monitor reuse, and the
# mandatory arm handshake.
# --------------------------------------------------------------------------- #


class _LossyArmEvent(threading.Event):
    """An ``armed_event`` whose ``wait()`` runs a loss callback the instant the
    monitor observes ARMED.

    This removes scheduler nondeterminism from the post-arm race replay: the
    continuity loss is guaranteed to be recorded BEFORE ``start()`` can commit,
    which is exactly the interleaving the defect describes (the watch arms, then
    a wait/result failure or crash destroys continuity before the commit).
    """

    def __init__(self, loss):
        super().__init__()
        self._loss = loss

    def wait(self, timeout=None):
        fired = super().wait(timeout)
        if fired:
            self._loss()
        return fired


class PostArmBackend:
    """Deterministic post-arm lifecycle replay for ``SourceChangeMonitor``.

    ``mode`` selects what the watcher does right after the first watch arms:

      "live"           -> arm and stay alive (normal Windows-style success);
      "uncertain"      -> continuity lost immediately after arm;
      "wait_failure"   -> the real backend's wait-failure path;
      "result_failure" -> the real backend's result-failure path;
      "overflow"       -> buffer overflow;
      "return"         -> arm then return with NO recorded reason (silent exit);
      "crash"          -> arm then raise (caught by the monitor thread).
    """

    instances: list["PostArmBackend"] = []

    def __init__(self, root: Path, monitor: hot_freshness.SourceChangeMonitor):
        self.root = Path(root)
        self.monitor = monitor
        self.mode = "live"
        self.opened = False
        self.closed = False
        self.finished = threading.Event()
        self._stop = threading.Event()
        self._arm_succeeded = False
        self.armed_event = _LossyArmEvent(self._on_arm_observed)
        PostArmBackend.instances.append(self)

    # -- backend protocol --------------------------------------------------

    def open(self) -> bool:
        self.opened = True
        return True

    def run(self, stop_signal: threading.Event) -> None:
        try:
            self._arm_succeeded = True
            self.armed_event.set()
            if self.mode == "crash":
                raise RuntimeError("post-arm crash")
            if self.mode != "live":
                return
            while not stop_signal.is_set():
                time.sleep(0.005)
        finally:
            self.finished.set()

    def close(self) -> None:
        self.closed = True
        self._stop.set()

    # -- loss injection ----------------------------------------------------

    def _on_arm_observed(self) -> None:
        if self.mode == "uncertain":
            self.monitor.mark_uncertain("post-arm wait failure")
        elif self.mode == "wait_failure":
            self.monitor.mark_uncertain("source watch wait failed (0)")
        elif self.mode == "result_failure":
            self.monitor.mark_uncertain("source watch result was unreadable")
        elif self.mode == "overflow":
            self.monitor.mark_uncertain("the source watch buffer overflowed")
        elif self.mode == "crash":
            self.monitor.mark_uncertain("post-arm crash")


@pytest.fixture
def post_arm(monkeypatch):
    PostArmBackend.instances = []
    mode_holder = {"mode": "live"}

    def factory(root, monitor):
        backend = PostArmBackend(root, monitor)
        backend.mode = mode_holder["mode"]
        return backend

    monkeypatch.setattr(hot_freshness, "_BACKEND_FACTORY", factory)
    monkeypatch.setattr(hot_freshness, "ARM_DEADLINE_SECONDS", 1.0)
    yield mode_holder
    hot_freshness.reset()


def _wait_not_running(monitor, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while monitor.running and time.time() < deadline:
        time.sleep(0.005)


def test_red_post_arm_death_cannot_be_proof_eligible(workspace, post_arm):
    """RED REPRODUCTION of the exact P0 race, reproduced deterministically.

    Pre-fix, the same sequence produced:

        start()          -> True
        continuous       -> True
        running          -> False
        uncertain_events -> 1
        begin(...)       -> non-None proof-eligible token

    The watcher arms, then a wait failure destroys continuity before start()
    commits. start() must fail closed and no proof-eligible token may exist.
    """
    post_arm["mode"] = "uncertain"
    source = workspace / "src"
    archive = _archive(workspace)

    monitor = hot_freshness.SourceChangeMonitor(source)
    assert monitor.start() is False
    assert monitor.continuous is False
    assert monitor.running is False
    assert monitor.uncertain_events >= 1

    token = hot_freshness.begin(str(source), "policy-a")
    assert token is None or token.continuous is False
    if token is not None:
        assert hot_freshness.confirm(token, archive, "policy-a") is False
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_d2_arm_then_watcher_exits_is_never_proof_eligible(workspace, post_arm):
    """D.2: the watcher arms and then returns with no recorded reason. A dead
    watcher must never be proof-eligible, even if start() committed in time."""
    post_arm["mode"] = "return"
    source = workspace / "src"
    monitor = hot_freshness.SourceChangeMonitor(source)
    monitor.start()
    _wait_not_running(monitor)

    assert monitor.running is False
    assert monitor.continuous is False
    # The silent exit is recorded as lost continuity, so the instance is terminal.
    assert monitor.start() is False


def test_d3_arm_then_crash_is_never_proof_eligible(workspace, post_arm):
    """D.3: backend arms then crashes -> no proof eligibility."""
    post_arm["mode"] = "crash"
    source = workspace / "src"
    archive = _archive(workspace)
    monitor = hot_freshness.SourceChangeMonitor(source)

    assert monitor.start() is False
    _wait_not_running(monitor)
    assert monitor.running is False
    assert monitor.continuous is False
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_d4_arm_then_wait_failure_is_never_proof_eligible(workspace, post_arm):
    """D.4: backend arms then reports a wait failure -> no proof eligibility."""
    post_arm["mode"] = "wait_failure"
    source = workspace / "src"
    archive = _archive(workspace)
    monitor = hot_freshness.SourceChangeMonitor(source)

    assert monitor.start() is False
    assert monitor.continuous is False
    assert monitor.uncertain_events >= 1
    assert hot_freshness.begin(str(source), "policy-a") is None
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_d5_arm_then_result_failure_is_never_proof_eligible(workspace, post_arm):
    """D.5: backend arms then reports a result failure -> no proof eligibility."""
    post_arm["mode"] = "result_failure"
    source = workspace / "src"
    archive = _archive(workspace)
    monitor = hot_freshness.SourceChangeMonitor(source)

    assert monitor.start() is False
    assert monitor.continuous is False
    assert monitor.uncertain_events >= 1
    assert hot_freshness.begin(str(source), "policy-a") is None
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_d6_backend_without_arm_handshake_fails_closed(workspace, monkeypatch):
    """D.6: a backend with no explicit arm protocol is NOT trusted. Production
    optimization may never infer observation continuity from "the thread
    started"; start() fails closed and the authoritative path takes over."""

    class NoHandshakeBackend:
        def __init__(self, root, monitor):
            self.opened = False
            self.closed = False

        def open(self):
            self.opened = True
            return True

        def run(self, stop_signal):
            while not stop_signal.is_set():
                time.sleep(0.005)

        def close(self):
            self.closed = True

    monkeypatch.setattr(hot_freshness, "_BACKEND_FACTORY", NoHandshakeBackend)
    source = workspace / "src"
    archive = _archive(workspace)

    monitor = hot_freshness.SourceChangeMonitor(source)
    assert monitor.start() is False
    assert monitor.continuous is False
    assert monitor.reason, "a fail-closed arm must explain itself"
    assert hot_freshness.begin(str(source), "policy-a") is None
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    hot_freshness.reset()


def test_d7_continuous_but_dead_monitor_is_not_reused(workspace, post_arm):
    """D.7: ``continuous=True, running=False`` is invalid internal state. The
    dead monitor may not be reused: its proof is dropped, it is retired, and a
    fresh monitor is armed in its place."""
    post_arm["mode"] = "live"
    source = workspace / "src"
    archive = _archive(workspace)
    key = hot_freshness._key(source)

    token = hot_freshness.begin(str(source), "policy-a")
    assert token is not None
    assert hot_freshness.confirm(token, archive, "policy-a") is True
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None

    dead = token.monitor
    # Simulate the watcher dying without clearing its continuous bit.
    with dead._lock:
        dead._running = False
    assert dead.running is False
    assert dead.continuous is False, "a dead watcher must never read as eligible"

    with hot_freshness._LOCK:
        fresh = hot_freshness._monitor_for(key, source)
    assert fresh is not None
    assert fresh is not dead
    assert fresh.continuous is True
    assert dead.reason == "stopped", "the dead monitor must have been retired"
    # The dead monitor's proof died with it and was not inherited by the fresh one.
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None


def test_d7b_stub_continuous_dead_monitor_is_never_returned(workspace, post_arm):
    """D.7 (direct): a monitor object reporting continuous=True with
    running=False must be retired, never returned for reuse."""
    post_arm["mode"] = "live"
    source = workspace / "src"
    key = hot_freshness._key(source)

    class DeadStub:
        monitor_id = 987654
        continuous = True  # stale bit, exactly the impossible state
        running = False
        reason = "stale"
        generation = 0
        dirty_events = 0
        uncertain_events = 0
        stopped = False

        def stop(self):
            self.stopped = True

    stub = DeadStub()
    with hot_freshness._LOCK:
        hot_freshness._MONITORS[key] = stub
        hot_freshness._PROOFS[key] = object()
        fresh = hot_freshness._monitor_for(key, source)

    assert stub.stopped is True, "a dead monitor must be retired, not reused"
    assert fresh is not stub
    assert key not in hot_freshness._PROOFS, "the stale proof was not dropped"


def test_d8_successful_arm_with_a_live_thread_is_proof_capable(workspace, post_arm):
    """D.8: normal Windows-style successful arm + live thread -> start() True."""
    post_arm["mode"] = "live"
    source = workspace / "src"
    archive = _archive(workspace)
    monitor = hot_freshness.SourceChangeMonitor(source)

    assert monitor.start() is True
    assert monitor.continuous is True
    assert monitor.running is True
    assert monitor.reason == ""

    token = hot_freshness.begin(str(source), "policy-a")
    assert token is not None and token.continuous is True
    assert hot_freshness.confirm(token, archive, "policy-a") is True
    assert hot_freshness.lookup(str(source), "policy-a", archive) is not None
    monitor.stop()


def test_d9_dirty_after_arm_advances_generation_and_allows_reproof(workspace, post_arm):
    """D.9: a DIRTY event after a successful arm advances the generation and
    invalidates the old proof, but does NOT destroy continuity; a later
    authoritative re-proof establishes a new generation."""
    post_arm["mode"] = "live"
    source = workspace / "src"
    archive = _archive(workspace)

    first = hot_freshness.begin(str(source), "policy-a")
    assert first is not None
    assert hot_freshness.confirm(first, archive, "policy-a") is True
    proof = hot_freshness.lookup(str(source), "policy-a", archive)
    assert proof is not None
    assert proof.monitor_generation == first.generation

    first.monitor.mark_dirty()
    assert first.monitor.continuous is True, "DIRTY must not destroy continuity"
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None

    second = hot_freshness.begin(str(source), "policy-a")
    assert second is not None
    assert second.monitor is first.monitor, "DIRTY keeps the same live instance"
    assert second.generation == first.generation + 1
    assert hot_freshness.confirm(second, archive, "policy-a") is True
    reparsed = hot_freshness.lookup(str(source), "policy-a", archive)
    assert reparsed is not None
    assert reparsed.monitor_generation == first.generation + 1


def test_d10_uncertainty_is_permanent_for_the_instance(workspace, post_arm):
    """D.10: overflow / lost continuity is permanent for that monitor instance:
    it can never be restarted and no generation of it may back a proof again."""
    post_arm["mode"] = "live"
    source = workspace / "src"
    archive = _archive(workspace)

    token = hot_freshness.begin(str(source), "policy-a")
    assert token is not None
    monitor = token.monitor
    assert monitor.continuous is True

    monitor.mark_uncertain("the source watch buffer overflowed")
    assert monitor.continuous is False
    # Terminal for this instance: it cannot be restarted...
    assert monitor.start() is False
    # ...and no generation of it may ever back a proof again.
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None
    # Recovery arms a fresh instance that must prove itself from scratch.
    fresh = hot_freshness.begin(str(source), "policy-a")
    assert fresh is not None
    assert fresh.monitor is not monitor
    assert hot_freshness.lookup(str(source), "policy-a", archive) is None



# --------------------------------------------------------------------------- #
# T-237: a direct pack seeds the next ensure's proof -- only when it is sound
# --------------------------------------------------------------------------- #


def _counting_probe(monkeypatch):
    from audapack.services import packing_service as service_module

    probes = {"count": 0}
    real_probe = service_module.probe_archive_freshness

    def counting_probe(*args, **kwargs):
        probes["count"] += 1
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(service_module, "probe_archive_freshness", counting_probe)
    return probes


def test_t237_direct_pack_makes_the_next_unchanged_ensure_hot(workspace, monitors, monkeypatch):
    service, _config = _service(workspace, workspace / "src", workspace / "out")
    probes = _counting_probe(monkeypatch)

    packed = service.pack_project("proj")
    assert packed.success is True
    warm = service.ensure_fresh_archive("proj")
    assert warm.success is True
    assert warm.reused is True
    assert warm.packed is False
    assert warm.timings.get("hot_proof") is True
    assert warm.timings.get("source_walk_skipped") is True
    assert probes["count"] == 0, "the ensure right after a pack must not walk"
    assert Path(warm.output_path) == Path(packed.output_path)


def test_t237_mutation_during_the_pack_records_no_proof(workspace, monitors, monkeypatch):
    from audapack.services import packing_service as service_module

    service, _config = _service(workspace, workspace / "src", workspace / "out")
    real_pack_single = service_module.pack_single

    def pack_while_source_changes(*args, **kwargs):
        monitors.instances[0].monitor.mark_dirty()
        return real_pack_single(*args, **kwargs)

    monkeypatch.setattr(service_module, "pack_single", pack_while_source_changes)
    assert service.pack_project("proj").success is True
    monkeypatch.setattr(service_module, "pack_single", real_pack_single)
    probes = _counting_probe(monkeypatch)
    after = service.ensure_fresh_archive("proj")
    assert after.timings.get("hot_proof") is False
    assert after.timings.get("source_walk_skipped") is False
    assert probes["count"] == 1


def test_t237_failed_or_cancelled_pack_records_no_proof(workspace, monitors, monkeypatch):
    from audapack.models import PackResult
    from audapack.services import packing_service as service_module

    service, _config = _service(workspace, workspace / "src", workspace / "out")
    first = service.pack_project("proj")
    assert first.success is True
    hot_freshness.invalidate(str(workspace / "src"))

    monkeypatch.setattr(
        service_module, "pack_single",
        lambda **_kw: PackResult(project_id="proj", name="proj", source_path="", success=False, error_message="boom"),
    )
    assert service.pack_project("proj").success is False
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(
        service_module, "pack_single",
        lambda **kw: PackResult(
            project_id="proj", name="proj", source_path="", success=True,
            output_path=first.output_path,
        ),
    )
    service.pack_project("proj", cancel_event=cancel)
    assert all(not m["proof"] for m in hot_freshness.diagnostics()["monitors"])


def test_t237_policy_change_after_pack_forces_the_walk(workspace, monitors, monkeypatch):
    service, config = _service(workspace, workspace / "src", workspace / "out")
    assert service.pack_project("proj").success is True
    config.packing.excludes = list(config.packing.excludes or []) + ["*.never"]
    probes = _counting_probe(monkeypatch)
    after = service.ensure_fresh_archive("proj")
    assert after.timings.get("source_walk_skipped") is False
    assert probes["count"] == 1


def test_t237_ensure_repack_does_not_double_arm(workspace, monitors, monkeypatch):
    """The ensure transaction owns its arm; the pack it triggers must not re-arm."""
    service, _config = _service(workspace, workspace / "src", workspace / "out")
    arms = {"count": 0}
    real_begin = hot_freshness.begin

    def counting_begin(*args, **kwargs):
        arms["count"] += 1
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(hot_freshness, "begin", counting_begin)
    first = service.ensure_fresh_archive("proj")
    assert first.packed is True
    assert arms["count"] == 1
