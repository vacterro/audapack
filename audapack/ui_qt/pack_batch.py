"""T-191: parallel pack batch execution for the Qt Project Room.

The pre-T-191 batch paths (PACK ALL, AUTO PACK ALL, PACK [group]) dispatched
one project, waited for its completion callback, then chained the next one
through ``QTimer.singleShot``. That completion-chain made a 20-project batch
take 20 sequential pack times and left 19 rows visually idle.

This module owns the replacement contract:

- ONE canonical entry point, :meth:`PackBatchRunner.start_batch`, used by
  manual PACK ALL, periodic AUTO PACK ALL and PACK [group].
- All eligible projects are SUBMITTED immediately; no completion is ever
  required to start the next job.
- A dedicated pack-only :class:`QThreadPool` sized to the batch (bounded by
  :data:`MAX_PACK_WORKERS`). The global Qt thread pool and the shared
  TaskRunner that own unrelated background work are never resized or
  starved.
- Explicit batch state (batch_id, label, pending/running/succeeded/failed
  id sets, started_at) replaces the sequential-queue flags.
- Ownership: a project cannot be packed twice concurrently by the same
  window; a second PACK ALL while a batch is active starts zero duplicate
  jobs.
- Independent terminal states: one failure never cancels siblings; the
  batch finalizes only when every owned project is terminal.
- Every UI/model update happens on the GUI thread through the runner's
  queued Qt signal (worker threads never touch Qt widgets or models).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

#: Hard safety ceiling for one pack batch. The normal supported Project Room
#: batch size (4 groups x 6 slots) fits well under it, so every eligible
#: project can begin concurrently; a hypothetically larger batch would be
#: dispatched QUEUED first rather than silently serialized behind a four-slot
#: ceiling inherited from the general TaskRunner.
MAX_PACK_WORKERS = 32


@dataclass(frozen=True)
class PackJobResult:
    """Terminal outcome of one pack job, delivered on the GUI thread."""

    project_id: str
    success: bool
    error_message: str = ""
    payload: Any = None


@dataclass
class PackBatch:
    """Explicit parallel batch state (replaces the sequential queue)."""

    batch_id: int
    label: str
    owned_project_ids: frozenset
    started_at: float = field(default_factory=time.monotonic)
    #: Projects submitted but not yet picked up by a bounded pool worker.
    pending_ids: set = field(default_factory=set)
    running_ids: set = field(default_factory=set)
    succeeded_ids: set = field(default_factory=set)
    failed_ids: set = field(default_factory=set)

    def is_terminal(self, project_id: str) -> bool:
        return (
            project_id in self.succeeded_ids
            or project_id in self.failed_ids
            or project_id not in self.owned_project_ids
        )

    @property
    def complete(self) -> bool:
        return not self.pending_ids and not self.running_ids

    def summary(self) -> str:
        packed = len(self.succeeded_ids)
        failed = len(self.failed_ids)
        return f"{packed}/{len(self.owned_project_ids)} packed" + (
            f", {failed} failed" if failed else ""
        )


class _PackJobSignals(QObject):
    # Emitted from the worker thread; Qt delivers these queued to the GUI
    # thread because the receiver lives there.
    started = Signal(str)
    finished = Signal(PackJobResult)


class _PackJobRunnable(QRunnable):
    def __init__(self, project_id: str, fn: Callable[[], Any], signals: _PackJobSignals):
        super().__init__()
        self._project_id = project_id
        self._fn = fn
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self._signals.started.emit(self._project_id)
        except RuntimeError:
            # Receiver (window) already destroyed: stop touching Qt.
            return
        try:
            payload = self._fn()
            # The real packing service reports failures by RETURNING a
            # ``PackResult`` with ``success=False`` (it raises only on
            # unexpected internal errors). Honor both conventions: a falsy
            # payload success is a failed job, never a batch abort.
            success = bool(getattr(payload, "success", True))
            error = "" if success else str(getattr(payload, "error_message", "") or "pack failed")
            result = PackJobResult(
                project_id=self._project_id, success=success, error_message=error, payload=payload
            )
        except Exception as exc:  # noqa: BLE001 - one job failing must never take the batch down
            result = PackJobResult(
                project_id=self._project_id, success=False, error_message=str(exc)
            )
        try:
            self._signals.finished.emit(result)
        except RuntimeError:
            pass


class PackBatchRunner(QObject):
    """Bounded pack-only executor plus explicit parallel batch state.

    Lifetime belongs to the MainWindow (``parent=window``), so pool threads
    and queued callbacks never outlive the Qt objects they would touch.
    """

    #: Emitted on the GUI thread when a job actually enters a worker.
    job_started = Signal(str)
    #: Emitted on the GUI thread when a job reaches a terminal state.
    job_finished = Signal(PackJobResult)
    #: Emitted on the GUI thread when every owned project is terminal.
    batch_finished = Signal(int, str, str)  # batch_id, label, summary

    def __init__(self, max_workers: int = MAX_PACK_WORKERS, parent: Optional[QObject] = None):
        super().__init__(parent)
        # Dedicated pack-only pool: NEVER QThreadPool.globalInstance(), which
        # carries unrelated runtime work (bridge polling, audit refresh, UI
        # enrichment through the shared TaskRunner).
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_workers)))
        self._signals = _PackJobSignals()
        self._signals.started.connect(self._on_job_started)
        self._signals.finished.connect(self._on_job_finished)

        self._lock = threading.Lock()
        self._batch: Optional[PackBatch] = None
        self._next_batch_id = 1
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Ownership / state queries (GUI thread)
    # ------------------------------------------------------------------

    def active_batch(self) -> Optional[PackBatch]:
        with self._lock:
            return self._batch

    def owns(self, project_id: str) -> bool:
        """True while an active batch owns ``project_id`` (pending or running)."""
        with self._lock:
            batch = self._batch
            return bool(batch and project_id in batch.owned_project_ids and not batch.is_terminal(project_id))

    def busy(self) -> bool:
        with self._lock:
            return self._batch is not None

    def max_workers(self) -> int:
        return self._pool.maxThreadCount()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def start_batch(
        self,
        projects: list,
        label: str,
        *,
        job_fn: Callable[[str], Any],
        on_batch_done: Optional[Callable[[PackBatch], None]] = None,
        on_job_done: Optional[Callable[[PackJobResult], None]] = None,
    ) -> Optional[PackBatch]:
        """Submit every project immediately and return the new batch state.

        Returns ``None`` (and starts nothing) while a batch is already
        active -- a second PACK ALL must never create an overlapping batch.
        ``job_fn(project_id)`` runs on a pool worker; its exceptions become
        failed jobs, never batch aborts.
        """
        with self._lock:
            if self._batch is not None or self._shutting_down:
                return None
            owned = frozenset(p.id for p in projects)
            batch = PackBatch(
                batch_id=self._next_batch_id,
                label=label,
                owned_project_ids=owned,
            )
            batch.pending_ids = set(owned)
            self._batch = batch
            self._next_batch_id += 1
            self._on_batch_done_cb = on_batch_done
            self._on_job_done_cb = on_job_done
            self._job_fn = job_fn

        for project in projects:
            with self._lock:
                if self._shutting_down:
                    return batch
            runnable = _PackJobRunnable(project.id, lambda pid=project.id: job_fn(pid), self._signals)
            # start() merely enqueues; every job is submitted NOW and pool
            # workers pick them up concurrently (all of them, when the batch
            # fits under the worker ceiling).
            self._pool.start(runnable)
        return batch

    def shutdown(self) -> None:
        """Terminal, callback-safe close; called from closeEvent on the GUI thread.

        After shutdown the runner is inert: new dispatch is refused, the active
        batch ownership is retired (``busy()`` is False, ``active_batch()``
        None), queued runnables are dropped by ``QThreadPool.clear()`` and
        never run, the completion callbacks are detached, and the late signal
        slots of an already-running worker return early -- so no result can
        mutate window/model state after close. In-flight pack workers are never
        waited on: GUI shutdown must not block on filesystem compression.
        """
        with self._lock:
            self._shutting_down = True
            self._batch = None
            self._job_fn = None
            self._on_batch_done_cb = None
            self._on_job_done_cb = None
        self._pool.clear()

    # ------------------------------------------------------------------
    # Signal slots (GUI thread, queued delivery)
    # ------------------------------------------------------------------

    def _on_job_started(self, project_id: str) -> None:
        with self._lock:
            if self._shutting_down:
                # Post-close late start from a worker launched before shutdown:
                # never touch model/UI state again.
                return
            batch = self._batch
            if batch is None or project_id not in batch.owned_project_ids:
                return
            batch.pending_ids.discard(project_id)
            batch.running_ids.add(project_id)
        self.job_started.emit(project_id)

    def _on_job_finished(self, result: PackJobResult) -> None:
        with self._lock:
            if self._shutting_down:
                # A worker that outlived shutdown is generation-fenced: its
                # result is dropped, callbacks stay detached, no batch can be
                # resurrected.
                return
            batch = self._batch
            if batch is None or result.project_id not in batch.owned_project_ids:
                return
            batch.running_ids.discard(result.project_id)
            batch.pending_ids.discard(result.project_id)
            if result.success:
                batch.succeeded_ids.add(result.project_id)
            else:
                batch.failed_ids.add(result.project_id)
            done = batch.complete
            summary = batch.summary()
            batch_id, label = batch.batch_id, batch.label
            if done:
                self._batch = None
        self.job_finished.emit(result)
        cb = getattr(self, "_on_job_done_cb", None)
        if cb is not None:
            cb(result)
        if done:
            self.batch_finished.emit(batch_id, label, summary)
            done_cb = getattr(self, "_on_batch_done_cb", None)
            if done_cb is not None:
                done_cb(batch)
