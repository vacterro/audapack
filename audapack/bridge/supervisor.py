"""Bridge-side keeper of queued audit work.

Everything that kept a queued audit moving used to hang off an inbound HTTP
request from a browser worker: ``expire_leases()`` ran only inside
``/v1/browser/poll`` and ``ManagedWorkerSupervisor.ensure_capacity()`` only
inside a desktop START.  Close the worker browser -- or the desktop app -- and
the whole chain froze: a QUEUED job had nobody left to claim it, a post-START
job kept its dead lease forever, and no managed window was ever relaunched.

This module owns that work inside the Bridge daemon, the one process that
outlives both the browser and the GUI.  It deliberately grows the managed pool
by at most one lane per grace window and stops after a bounded number of
unproductive attempts, so a browser that never registers can never turn into an
endless stream of new windows.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_AUDIT_LANES = 6
DEFAULT_INTERVAL_SECONDS = 15.0
#: Pacing between provisioning passes. Duplicate windows are prevented by
#: ManagedWorkerSupervisor's own boot-grace accounting of slots that are still
#: starting, so this only has to stop the supervisor from thrashing -- it does
#: not need to be long enough to cover a full browser boot, and while it was
#: a queued audit could sit behind an empty lane for two minutes.
LAUNCH_GRACE_SECONDS = 45.0
#: Consecutive launches that produced no new registered worker before the
#: supervisor gives up and waits for the operator (or a worker) to change something.
MAX_UNPRODUCTIVE_LAUNCHES = 3


def _default_launch_worker(slot: int, generation: int) -> tuple[bool, str]:
    from audapack.components.widget import launch_dedicated_chromium_worker

    return launch_dedicated_chromium_worker(
        managed_slot=slot,
        managed_generation=generation,
    )


class DispatchSupervisor:
    """Ages dead leases out and re-provisions managed workers for queued work."""

    def __init__(
        self,
        dispatcher,
        worker_supervisor=None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        launch_worker: Optional[Callable[[int, int], tuple[bool, str]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.dispatcher = dispatcher
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.clock = clock
        if worker_supervisor is None:
            from audapack.services.audit_run_service import ManagedWorkerSupervisor

            worker_supervisor = ManagedWorkerSupervisor(launch_worker or _default_launch_worker)
        self.workers = worker_supervisor
        self._last_launch_at = 0.0
        self._unproductive_launches = 0
        self._last_active_workers = 0
        self._last_queued: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- pool provisioning ------------------------------------------------ #

    def _worker_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for worker in self.dispatcher.list_workers():
            rows.append({
                "worker_id": worker.worker_id,
                "state": worker.state,
                "managed_slot": worker.managed_slot,
                "managed_generation": worker.managed_generation,
                "last_seen_at": worker.last_seen_at,
            })
        return rows

    def _note_queue_progress(self, queued: int) -> None:
        """The launch budget resets on PROGRESS, never on a window appearing.

        Resetting when a worker registered meant every new window earned the
        right to open another one, so a queue that never drained kept spawning
        Chromium windows -- including with the desktop app closed. A shrinking
        queue is the only proof that opening windows is actually helping.
        """
        if self._last_queued is not None and queued < self._last_queued:
            self._unproductive_launches = 0
        self._last_queued = queued

    def _launch_allowed(self, active_workers: int) -> bool:
        self._last_active_workers = active_workers
        if self._unproductive_launches >= MAX_UNPRODUCTIVE_LAUNCHES:
            return False
        return self.clock() - self._last_launch_at >= LAUNCH_GRACE_SECONDS

    def tick(self) -> dict[str, Any]:
        """One supervision pass. Returns what it observed and did."""
        result: dict[str, Any] = {
            "requeued": 0,
            "freed": 0,
            "reconciled_complete": 0,
            "unrecoverable": 0,
            "queued_jobs": 0,
            "active_workers": 0,
            "clean_workers": 0,
            "free_workers": 0,
            "launched": [],
            "skipped": "",
        }
        try:
            result["requeued"] = int(self.dispatcher.expire_leases() or 0)
        except Exception as exc:  # never let one bad pass kill the loop
            logger.warning("dispatch supervisor could not expire leases: %s", exc)

        try:
            reconcile = getattr(self.dispatcher, "reconcile_abandoned_runs", None)
            if callable(reconcile):
                result["freed"] = int(reconcile() or 0)
        except Exception as exc:
            logger.warning("dispatch supervisor could not reconcile abandoned runs: %s", exc)

        try:
            reconcile_complete = getattr(self.dispatcher, "reconcile_completed_blocked_runs", None)
            if callable(reconcile_complete):
                result["reconciled_complete"] = int(reconcile_complete() or 0)
        except Exception as exc:
            logger.warning("dispatch supervisor could not reconcile completed blocked runs: %s", exc)

        try:
            # The finalization event closes a lane as it happens; this reaches
            # the same conclusion late, for a run whose campaign was finished
            # while the Bridge was down or the lane was blocked.
            reconcile_finished = getattr(self.dispatcher, "reconcile_finished_campaigns", None)
            if callable(reconcile_finished):
                result["reconciled_complete"] += int(reconcile_finished() or 0)
        except Exception as exc:
            logger.warning("dispatch supervisor could not reconcile finished campaigns: %s", exc)

        try:
            # Strictly after the completion reconcilers: a run that actually
            # finished must close as COMPLETE, never fail here.
            expire_unrecoverable = getattr(self.dispatcher, "expire_unrecoverable_runs", None)
            if callable(expire_unrecoverable):
                result["unrecoverable"] = int(expire_unrecoverable() or 0)
        except Exception as exc:
            logger.warning("dispatch supervisor could not expire unrecoverable runs: %s", exc)

        try:
            status = dict(self.dispatcher.status())
        except Exception as exc:
            logger.warning("dispatch supervisor could not read status: %s", exc)
            result["skipped"] = "status-unavailable"
            return result

        status["workers"] = self._worker_rows()
        queued = int(status.get("queued_jobs", 0) or 0)
        active_workers = int(status.get("active_workers", 0) or 0)
        clean = int(status.get("clean_workers", 0) or 0)
        free = int(status.get("free_workers", 0) or 0)
        result.update(queued_jobs=queued, active_workers=active_workers, clean_workers=clean, free_workers=free)
        self._note_queue_progress(queued)

        if queued <= 0:
            result["skipped"] = "no-queued-work"
            return result
        if free > 0:
            # Only a worker that can actually CLAIM counts as spare capacity. A
            # worker can report CLEAN while already holding a lease, and treating
            # that as availability left a queued audit waiting behind a worker
            # that was never going to take it.
            result["skipped"] = "free-worker-available"
            self._last_active_workers = active_workers
            return result
        if active_workers >= MAX_AUDIT_LANES:
            result["skipped"] = "lanes-full"
            self._last_active_workers = active_workers
            return result
        if not self._launch_allowed(active_workers):
            result["skipped"] = (
                "launch-budget-exhausted"
                if self._unproductive_launches >= MAX_UNPRODUCTIVE_LAUNCHES
                else "launch-grace"
            )
            return result

        # Provision for the work that actually exists: six queued audits should
        # end up in six windows, not one window per grace window. Over-launching
        # is prevented by ManagedWorkerSupervisor counting slots that are still
        # booting, so asking for the full figure here is safe.
        desired = min(MAX_AUDIT_LANES, max(1, active_workers + queued))
        try:
            outcome = self.workers.ensure_capacity(status, desired)
        except Exception as exc:
            logger.warning("dispatch supervisor could not provision a worker: %s", exc)
            result["skipped"] = "provisioning-failed"
            return result

        launched = list(outcome.get("launched", []))
        result["launched"] = launched
        if launched:
            self._last_launch_at = self.clock()
            self._unproductive_launches += 1
        else:
            result["skipped"] = "nothing-to-launch"
        return result

    def relaunch_managed_slot(self, slot: int) -> dict[str, Any]:
        """Reopen a specific managed worker slot whose window was closed.

        An explicit operator request: the slot's launch/cooldown accounting is
        cleared and the unproductive-launch budget is reset so past failures or
        grace pacing never block the request. A live worker already registered
        on the slot is left alone (no duplicate window is ever opened).
        """
        slot = max(1, min(MAX_AUDIT_LANES, int(slot)))
        self._unproductive_launches = 0
        self._last_launch_at = 0.0
        try:
            self.workers._reset_slot(slot)
            status = dict(self.dispatcher.status())
        except Exception as exc:
            logger.warning("could not prepare slot %s relaunch: %s", slot, exc)
            return {"slot": slot, "success": False, "message": f"relaunch preparation failed: {exc}"}
        status["workers"] = self._worker_rows()
        try:
            outcome = self.workers.launch_slot(slot, status)
        except Exception as exc:
            logger.warning("could not relaunch slot %s: %s", slot, exc)
            return {"slot": slot, "success": False, "message": f"relaunch failed: {exc}"}
        return {
            "slot": slot,
            "generation": outcome.get("generation", 1),
            "success": bool(outcome.get("launched")),
            "message": str(outcome.get("message") or ""),
            "launched": [outcome] if outcome.get("launched") else [],
        }

    # -- thread lifecycle ------------------------------------------------- #

    def start(self) -> bool:
        if self._thread is not None:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="audapack-dispatch-supervisor",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception as exc:
                logger.warning("dispatch supervisor pass failed: %s", exc)
