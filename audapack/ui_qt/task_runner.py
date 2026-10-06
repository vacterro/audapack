"""Lightweight Qt Background Task Runner (Wave M).

Uses QThreadPool + QRunnable + QObject signals for framework-clean async execution.
Supports task keys, deduplication/coalescing (dirty re-run), and stale-result protection.
All result callbacks are invoked on the Qt GUI thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)

#: W2-002: how long `close()` waits for tasks that had already begun.
TASK_RUNNER_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass
class TaskResult:
    key: str
    generation: int
    success: bool
    data: Any = None
    error: Optional[Exception] = None


class _TaskSignals(QObject):
    finished = Signal(TaskResult)


class _WorkerRunnable(QRunnable):
    def __init__(
        self,
        key: str,
        generation: int,
        fn: Callable[[], Any],
        signals: _TaskSignals,
        gate: Callable[[], bool] | None = None,
        on_finished: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self.key = key
        self.generation = generation
        self.fn = fn
        self.signals = signals
        self.gate = gate
        self.on_finished = on_finished
        self.setAutoDelete(True)

    def run(self):
        # W2-002 (audit/12.md): the pool is global and cannot cancel one queued
        # QRunnable, so the runnable refuses ITSELF once the runner is closing.
        # Without this a task queued before closeEvent() still ran -- packing,
        # provisioning windows and dispatching an audit for a window the operator
        # had already closed.
        try:
            if self.gate is not None and not self.gate():
                logger.debug("Task %s dropped: runner is closing", self.key)
                return
            self._run_task()
        finally:
            # The barrier is signalled from HERE, on the worker thread. `close()`
            # runs on the GUI thread, and a result signal is queued TO that same
            # thread, so waiting for the signal would deadlock the event loop
            # that has to deliver it.
            if self.on_finished is not None:
                self.on_finished()

    def _run_task(self):
        try:
            res = self.fn()
            try:
                self.signals.finished.emit(
                    TaskResult(key=self.key, generation=self.generation, success=True, data=res)
                )
            except RuntimeError:
                pass
        except Exception as exc:
            logger.debug(f"Task {self.key} failed: {exc}", exc_info=True)
            try:
                self.signals.finished.emit(
                    TaskResult(key=self.key, generation=self.generation, success=False, error=exc)
                )
            except RuntimeError:
                pass


class TaskRunner(QObject):
    """Manages background workers with deduplication and main-thread signal delivery."""

    task_completed = Signal(TaskResult)

    def __init__(self, max_threads: int = 4, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        if max_threads > 0:
            self.pool.setMaxThreadCount(max_threads)

        self._lock = threading.Lock()
        # Counts task functions that have been admitted and not yet returned,
        # counted on the worker thread. This -- not `_running`, which only clears
        # when a queued Qt signal reaches the GUI thread -- is the barrier
        # `close()` waits on.
        self._work_settled = threading.Condition(self._lock)
        self._active_tasks = 0
        # key -> generation
        self._running: dict[str, int] = {}
        # key -> (fn, on_success, on_error) for dirty re-runs
        self._dirty: dict[str, tuple[Callable[[], Any], Optional[Callable[[Any], None]], Optional[Callable[[Exception], None]]]] = {}
        # key -> callbacks for current running task
        self._callbacks: dict[str, tuple[Optional[Callable[[Any], None]], Optional[Callable[[Exception], None]]]] = {}
        self._generation_counter = 0
        self._generations: dict[str, int] = {}
        # W2-002 (audit/12.md): an explicit terminal lifecycle. OPEN accepts work;
        # CLOSING refuses new work, drops queued work and withholds callbacks
        # from a window that is going away; CLOSED is the settled end state.
        self._state = "OPEN"

        self._signals = _TaskSignals()
        self._signals.finished.connect(self._on_task_finished)

    @property
    def state(self) -> str:
        """OPEN | CLOSING | CLOSED."""
        return self._state

    def close(self, timeout: float = TASK_RUNNER_CLOSE_TIMEOUT_SECONDS) -> bool:
        """Stop accepting work, then wait for what already began.

        Returns True once nothing this runner owns is still in flight. A task
        that has ALREADY started is never killed -- it may be mid-pack -- so the
        answer is "did it finish", not "did I ask it to stop".
        """
        with self._lock:
            if self._state == "OPEN":
                self._state = "CLOSING"
                # A dirty re-run scheduled by a task finishing during shutdown
                # would be a brand new submission after CLOSING.
                self._dirty.clear()
            deadline = time.monotonic() + max(0.0, float(timeout))
            while self._active_tasks and time.monotonic() < deadline:
                self._work_settled.wait(min(0.05, max(0.0, deadline - time.monotonic())))
            settled = not self._active_tasks
            if settled:
                self._state = "CLOSED"
            else:
                logger.warning("task runner still owns %d task(s) at close",
                               self._active_tasks)
        return settled

    def is_running(self, key: str) -> bool:
        with self._lock:
            return key in self._running

    def submit(
        self,
        key: str,
        fn: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> int:
        """Submits a task, cancelling / superseding any existing generation for that key.

        Returns 0 when the runner is closing: generation 0 is never issued, so a
        caller can tell a refused submission from a real one.
        """
        with self._lock:
            if self._state != "OPEN":
                logger.debug("refused task %s: runner is %s", key, self._state)
                return 0
            self._generation_counter += 1
            gen = self._generation_counter
            self._generations[key] = gen
            self._running[key] = gen
            self._callbacks[key] = (on_success, on_error)
            self._dirty.pop(key, None)

        self._active_tasks += 1
        worker = _WorkerRunnable(key, gen, fn, self._signals, self._accepts_work,
                                 self._task_returned)
        self.pool.start(worker)
        return gen

    def submit_coalesced(
        self,
        key: str,
        fn: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> int:
        """If a task for `key` is already running, marks it dirty to re-run once upon completion."""
        with self._lock:
            if self._state != "OPEN":
                logger.debug("refused coalesced task %s: runner is %s", key, self._state)
                return 0
            if key in self._running:
                # Mark dirty for subsequent run
                self._dirty[key] = (fn, on_success, on_error)
                return self._running[key]
            self._generation_counter += 1
            gen = self._generation_counter
            self._generations[key] = gen
            self._running[key] = gen
            self._callbacks[key] = (on_success, on_error)

        self._active_tasks += 1
        worker = _WorkerRunnable(key, gen, fn, self._signals, self._accepts_work,
                                 self._task_returned)
        self.pool.start(worker)
        return gen

    def _task_returned(self) -> None:
        with self._work_settled:
            self._active_tasks = max(0, self._active_tasks - 1)
            self._work_settled.notify_all()

    def _accepts_work(self) -> bool:
        with self._lock:
            return self._state == "OPEN"

    def _on_task_finished(self, result: TaskResult):
        re_run: Optional[tuple[Callable[[], Any], Optional[Callable[[Any], None]], Optional[Callable[[Exception], None]]]] = None
        cb_success: Optional[Callable[[Any], None]] = None
        cb_error: Optional[Callable[[Exception], None]] = None

        with self._lock:
            current_gen = self._generations.get(result.key)
            is_latest = current_gen == result.generation
            if is_latest:
                cb_success, cb_error = self._callbacks.pop(result.key, (None, None))
                self._running.pop(result.key, None)
                if result.key in self._dirty:
                    re_run = self._dirty.pop(result.key)
                else:
                    self._generations.pop(result.key, None)
            # W2-002 (audit/12.md): bookkeeping still settles after CLOSING -- the
            # task really did finish -- but its callback would touch a window the
            # operator already closed, and a dirty re-run is a fresh submission.
            if self._state != "OPEN":
                cb_success = cb_error = None
                re_run = None

        if is_latest:
            if result.success:
                if cb_success:
                    try:
                        cb_success(result.data)
                    except Exception as e:
                        logger.error(f"Callback on_success failed for {result.key}: {e}", exc_info=True)
            else:
                if cb_error:
                    try:
                        cb_error(result.error)
                    except Exception as e:
                        logger.error(f"Callback on_error failed for {result.key}: {e}", exc_info=True)
            self.task_completed.emit(result)

        if re_run:
            fn, s_cb, e_cb = re_run
            self.submit(result.key, fn, on_success=s_cb, on_error=e_cb)
