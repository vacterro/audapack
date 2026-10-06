"""Unit tests for TaskRunner (Wave M).

Verifies:
- Background execution without blocking caller thread.
- Result delivery via Qt signals / callbacks.
- Task deduplication and coalescing (dirty re-run pattern).
- Task keys and isolation.
"""

import threading
import time

from PySide6.QtCore import QTimer

from audapack.ui_qt.task_runner import TaskRunner


class _Ticker:
    """Fires on the GUI thread; its tick count is the responsiveness probe."""

    def __init__(self, app, sink):
        self._sink = sink
        self._timer = QTimer()
        self._timer.timeout.connect(lambda: self._sink.append(time.time()))
        self._timer.setInterval(10)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()


def test_task_runner_basic_execution(qapp):
    runner = TaskRunner(max_threads=2)

    results = []
    def _worker():
        return 42

    def _on_success(val):
        results.append(val)

    runner.submit("calc:simple", _worker, on_success=_on_success)

    # Process Qt event loop until finished
    start = time.time()
    while not results and time.time() - start < 3.0:
        qapp.processEvents()
        time.sleep(0.01)

    assert results == [42]


def test_task_runner_coalescing_event_storm(qapp):
    runner = TaskRunner(max_threads=2)

    execution_counter = [0]
    results = []

    def _heavy_task():
        time.sleep(0.05)
        execution_counter[0] += 1
        return execution_counter[0]

    def _on_success(val):
        results.append(val)

    # Fire 5 rapid coalesced requests for the same key
    for _ in range(5):
        runner.submit_coalesced("audit:fastprompter", _heavy_task, on_success=_on_success)

    # Wait for completion of running + trailing run
    start = time.time()
    while len(results) < 2 and time.time() - start < 3.0:
        qapp.processEvents()
        time.sleep(0.01)

    # Invariant: Instead of 5 runs, exactly 2 runs executed (the initial one + 1 dirty trailing run with latest state)
    assert execution_counter[0] == 2
    assert len(results) == 2


def test_task_runner_error_handling(qapp):
    runner = TaskRunner(max_threads=2)

    errors = []
    def _failing_task():
        raise ValueError("Simulated worker error")

    def _on_error(err):
        errors.append(err)

    runner.submit("task:fail", _failing_task, on_error=_on_error)

    start = time.time()
    while not errors and time.time() - start < 3.0:
        qapp.processEvents()
        time.sleep(0.01)

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "Simulated worker error" in str(errors[0])

def test_gui_thread_stays_responsive_during_a_long_task(qapp):
    """T-20: a long worker (ZIP packing / Bridge request) must not block the GUI
    thread. Proven by pumping real work through the event loop while it runs."""
    runner = TaskRunner(max_threads=2)
    ticks = []
    done = []

    def _slow_task():
        time.sleep(0.8)
        return "packed"

    def _on_success(_val):
        done.append(True)

    timer = _Ticker(qapp, ticks)
    timer.start()

    started = time.time()
    runner.submit("pack:slow", _slow_task, on_success=_on_success)
    submit_cost = time.time() - started

    start = time.time()
    while not done and time.time() - start < 5.0:
        qapp.processEvents()
        time.sleep(0.01)
    elapsed = time.time() - start
    timer.stop()

    assert done, "long task never completed"
    assert submit_cost < 0.2, f"submit() blocked for {submit_cost:.3f}s"
    assert len(ticks) >= 5, f"event loop serviced only {len(ticks)} ticks in {elapsed:.3f}s"


def test_stale_result_does_not_overwrite_a_newer_generation(qapp):
    """T-20: a superseded task's callback must never fire after a newer submit."""
    runner = TaskRunner(max_threads=2)
    stale_cb = []
    fresh_cb = []
    release = threading.Event()

    runner.submit("bridge:poll", lambda: (release.wait(2.0), "old")[1],
                  on_success=stale_cb.append)
    runner.submit("bridge:poll", lambda: "new", on_success=fresh_cb.append)
    release.set()

    start = time.time()
    while not fresh_cb and time.time() - start < 5.0:
        qapp.processEvents()
        time.sleep(0.01)
    time.sleep(0.3)
    qapp.processEvents()

    assert fresh_cb == ["new"], fresh_cb
    assert stale_cb == [], "superseded generation delivered its result"


class TestTerminalLifecycle:
    """W2-002 (audit/12.md): TaskRunner had no lifecycle at all. `closeEvent()`
    could drop a debounced START AUDIT, but once `_pump_audit_start_queue()`
    had moved a batch into `_audit_start_inflight` and submitted `_prepare`,
    nothing could revoke it -- `start_batch()` still packed, provisioned browser
    capacity and dispatched an audit against a window the operator had closed.
    """

    def _drain(self, qapp, predicate, budget=5.0):
        start = time.time()
        while not predicate() and time.time() - start < budget:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()

    def test_a_queued_task_never_executes_after_terminal_shutdown(self, qapp):
        runner = TaskRunner(max_threads=1)
        ran = []
        release = threading.Event()
        began = threading.Event()
        runner.submit("busy", lambda: (began.set(), release.wait(2.0))[1])
        assert began.wait(5.0), "the first task never started, so nothing was queued behind it"
        # The pool has one thread, so this one is still QUEUED.
        runner.submit("late", lambda: ran.append("late"))

        assert runner.state == "OPEN"
        runner.close(timeout=0.1)
        assert runner.state != "OPEN"
        release.set()
        self._drain(qapp, lambda: False, 1.0)

        assert ran == [], "a task queued before close() executed after it"

    def test_submissions_are_refused_after_closing(self, qapp):
        runner = TaskRunner(max_threads=2)
        ran = []
        assert runner.submit("first", lambda: 1) > 0
        runner.close(timeout=2.0)
        assert runner.state == "CLOSED"
        assert runner.submit("after", lambda: ran.append("x")) == 0
        assert runner.submit_coalesced("after", lambda: ran.append("y")) == 0
        self._drain(qapp, lambda: False, 0.5)
        assert ran == []

    def test_close_reports_whether_everything_finished(self, qapp):
        runner = TaskRunner(max_threads=2)
        release = threading.Event()
        began = threading.Event()
        runner.submit("slow", lambda: (began.set(), release.wait(2.0))[1])
        assert began.wait(5.0), "the task never started, so this proves nothing"
        assert runner.close(timeout=0.1) is False
        assert runner.state == "CLOSING"
        release.set()
        self._drain(qapp, lambda: False, 1.0)
        assert runner.close(timeout=2.0) is True
        assert runner.state == "CLOSED"

    def test_a_task_running_at_close_still_settles_its_own_bookkeeping(self, qapp):
        """A task that already began must not be reported as never-run, and its
        callback must be dropped rather than delivered into a destroyed window."""
        runner = TaskRunner(max_threads=2)
        callbacks = []
        release = threading.Event()
        began = threading.Event()
        runner.submit("inflight", lambda: (began.set(), release.wait(2.0))[1],
                      on_success=callbacks.append)
        assert began.wait(5.0), "the task never started, so this proves nothing"
        runner.close(timeout=0.1)
        release.set()
        self._drain(qapp, lambda: False, 1.0)
        assert callbacks == [], "a callback fired after close() into a dead window"
        assert not runner.is_running("inflight")
        assert runner.close(timeout=2.0) is True
