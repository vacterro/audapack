"""Lightweight Qt Background Task Runner (Wave M).

Uses QThreadPool + QRunnable + QObject signals for framework-clean async execution.
Supports task keys, deduplication/coalescing (dirty re-run), and stale-result protection.
All result callbacks are invoked on the Qt GUI thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)


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
    ):
        super().__init__()
        self.key = key
        self.generation = generation
        self.fn = fn
        self.signals = signals
        self.setAutoDelete(True)

    def run(self):
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
        # key -> generation
        self._running: dict[str, int] = {}
        # key -> (fn, on_success, on_error) for dirty re-runs
        self._dirty: dict[str, tuple[Callable[[], Any], Optional[Callable[[Any], None]], Optional[Callable[[Exception], None]]]] = {}
        # key -> callbacks for current running task
        self._callbacks: dict[str, tuple[Optional[Callable[[Any], None]], Optional[Callable[[Exception], None]]]] = {}
        self._generations: dict[str, int] = {}

        self._signals = _TaskSignals()
        self._signals.finished.connect(self._on_task_finished)

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
        """Submits a task, cancelling / superseding any existing generation for that key."""
        with self._lock:
            gen = self._generations.get(key, 0) + 1
            self._generations[key] = gen
            self._running[key] = gen
            self._callbacks[key] = (on_success, on_error)
            self._dirty.pop(key, None)

        worker = _WorkerRunnable(key, gen, fn, self._signals)
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
            if key in self._running:
                # Mark dirty for subsequent run
                self._dirty[key] = (fn, on_success, on_error)
                return self._running[key]
            gen = self._generations.get(key, 0) + 1
            self._generations[key] = gen
            self._running[key] = gen
            self._callbacks[key] = (on_success, on_error)

        worker = _WorkerRunnable(key, gen, fn, self._signals)
        self.pool.start(worker)
        return gen

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

    def cancel_all(self):
        with self._lock:
            self._running.clear()
            self._dirty.clear()
            self._callbacks.clear()
