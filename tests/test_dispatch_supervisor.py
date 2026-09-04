from __future__ import annotations

from types import SimpleNamespace

from audapack.bridge.supervisor import (
    LAUNCH_GRACE_SECONDS,
    MAX_UNPRODUCTIVE_LAUNCHES,
    DispatchSupervisor,
)


class FakeDispatcher:
    def __init__(self, **status):
        self.expired = 0
        self._status = {
            "active_workers": 0,
            "clean_workers": 0,
            "free_workers": 0,
            "queued_jobs": 0,
            "active_jobs": 0,
        }
        self._status.update(status)
        self.workers = []

    def expire_leases(self):
        self.expired += 1
        return 0

    def status(self):
        return dict(self._status)

    def list_workers(self):
        return list(self.workers)


class FakeWorkerSupervisor:
    def __init__(self, launched=True):
        self.calls = []
        self.launched = launched
        self.resets = []
        self.slot_launches = []

    def ensure_capacity(self, dispatch, desired):
        self.calls.append((dict(dispatch), desired))
        return {
            "desired": desired,
            "registered": 0,
            "launched": [{"slot": desired, "ok": True, "message": "started"}] if self.launched else [],
            "generation": 1,
        }

    def _reset_slot(self, slot):
        self.resets.append(int(slot))

    def launch_slot(self, slot, dispatch):
        # The operator relaunch path targets ONE named slot. Routing it through
        # ensure_capacity read `slot` as a lane COUNT, so reopening slot 2 with
        # five other slots registered satisfied the demand and launched nothing.
        self.slot_launches.append((int(slot), dict(dispatch)))
        return {
            "slot": int(slot),
            "generation": 1,
            "launched": bool(self.launched),
            "message": "started" if self.launched else "slot already has a live worker",
        }


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def supervisor(dispatcher, workers=None, clock=None):
    return DispatchSupervisor(
        dispatcher,
        worker_supervisor=workers or FakeWorkerSupervisor(),
        clock=clock or Clock(),
    )


def test_leases_are_aged_out_even_with_nobody_polling():
    """Closing the worker browser must not freeze the queue."""
    dispatcher = FakeDispatcher(queued_jobs=0)
    sup = supervisor(dispatcher)
    result = sup.tick()
    assert dispatcher.expired == 1
    assert result["skipped"] == "no-queued-work"


def test_queued_work_is_provisioned_for_every_queued_audit():
    """Six queued audits belong in six windows, not one per grace window."""
    dispatcher = FakeDispatcher(queued_jobs=5, active_workers=1, clean_workers=0, free_workers=0)
    workers = FakeWorkerSupervisor()
    sup = supervisor(dispatcher, workers)
    result = sup.tick()
    assert result["launched"], result
    assert workers.calls[0][1] == 6

    # Never past the six-lane ceiling, however deep the queue is.
    deep = FakeDispatcher(queued_jobs=40, active_workers=2, clean_workers=0, free_workers=0)
    deep_workers = FakeWorkerSupervisor()
    supervisor(deep, deep_workers).tick()
    assert deep_workers.calls[0][1] == 6


def test_a_free_worker_is_never_shadowed_by_a_new_window():
    dispatcher = FakeDispatcher(queued_jobs=3, active_workers=2, clean_workers=1, free_workers=1)
    workers = FakeWorkerSupervisor()
    sup = supervisor(dispatcher, workers)
    assert sup.tick()["skipped"] == "free-worker-available"
    assert workers.calls == []


def test_launches_are_rate_limited_and_give_up_when_unproductive():
    dispatcher = FakeDispatcher(queued_jobs=1, active_workers=0, clean_workers=0)
    workers = FakeWorkerSupervisor()
    clock = Clock()
    sup = supervisor(dispatcher, workers, clock)

    assert sup.tick()["launched"]
    clock.now += 5
    assert sup.tick()["skipped"] == "launch-grace"
    assert len(workers.calls) == 1

    for _ in range(MAX_UNPRODUCTIVE_LAUNCHES):
        clock.now += LAUNCH_GRACE_SECONDS + 1
        sup.tick()
    assert len(workers.calls) == MAX_UNPRODUCTIVE_LAUNCHES

    clock.now += LAUNCH_GRACE_SECONDS + 1
    assert sup.tick()["skipped"] == "launch-budget-exhausted"
    assert len(workers.calls) == MAX_UNPRODUCTIVE_LAUNCHES


def test_only_a_draining_queue_restores_the_launch_budget():
    dispatcher = FakeDispatcher(queued_jobs=1, active_workers=0, clean_workers=0)
    workers = FakeWorkerSupervisor()
    clock = Clock()
    sup = supervisor(dispatcher, workers, clock)

    for _ in range(MAX_UNPRODUCTIVE_LAUNCHES):
        clock.now += LAUNCH_GRACE_SECONDS + 1
        sup.tick()
    clock.now += LAUNCH_GRACE_SECONDS + 1
    assert sup.tick()["skipped"] == "launch-budget-exhausted"

    # A window merely appearing is not progress and must not buy another launch.
    dispatcher._status["active_workers"] = 1
    clock.now += LAUNCH_GRACE_SECONDS + 1
    assert sup.tick()["skipped"] == "launch-budget-exhausted"

    # The queue actually draining is the only proof that launching helped.
    dispatcher._status["queued_jobs"] = 0
    clock.now += LAUNCH_GRACE_SECONDS + 1
    sup.tick()
    dispatcher._status["queued_jobs"] = 2
    clock.now += LAUNCH_GRACE_SECONDS + 1
    assert sup.tick()["launched"]


def test_a_broken_dispatcher_never_kills_the_loop():
    class Broken(FakeDispatcher):
        def status(self):
            raise RuntimeError("bridge state unreadable")

    sup = supervisor(Broken(queued_jobs=1))
    assert sup.tick()["skipped"] == "status-unavailable"


def test_worker_rows_carry_managed_identity():
    dispatcher = FakeDispatcher(queued_jobs=1, active_workers=0)
    dispatcher.workers = [SimpleNamespace(
        worker_id="audapack-managed-2-1",
        state="FREE",
        managed_slot=2,
        managed_generation=1,
        last_seen_at=123.0,
    )]
    workers = FakeWorkerSupervisor()
    sup = supervisor(dispatcher, workers)
    sup.tick()
    passed = workers.calls[0][0]
    assert passed["workers"][0]["managed_slot"] == 2
    assert passed["workers"][0]["managed_generation"] == 1


def test_relaunch_resets_unproductive_budget_and_clears_slot():
    dispatcher = FakeDispatcher(queued_jobs=1, active_workers=0, clean_workers=0)
    workers = FakeWorkerSupervisor()
    clock = Clock()
    sup = supervisor(dispatcher, workers, clock)
    for _ in range(MAX_UNPRODUCTIVE_LAUNCHES):
        clock.now += LAUNCH_GRACE_SECONDS + 1
        sup.tick()
    clock.now += LAUNCH_GRACE_SECONDS + 1
    assert sup.tick()["skipped"] == "launch-budget-exhausted"

    result = sup.relaunch_managed_slot(3)
    assert result["slot"] == 3
    assert result["success"] is True
    assert workers.resets == [3]
    # The unproductivity budget is gone, so the next tick can launch again.
    clock.now += LAUNCH_GRACE_SECONDS + 1
    assert sup.tick()["launched"]


def test_relaunch_targets_the_named_slot_not_a_lane_budget():
    """Reopening a closed window is a request for THAT window."""
    workers = FakeWorkerSupervisor()
    sup = supervisor(FakeDispatcher(active_workers=5, clean_workers=5), workers)
    result = sup.relaunch_managed_slot(2)
    assert result["success"] is True
    assert [slot for slot, _dispatch in workers.slot_launches] == [2]
    assert workers.calls == [], "the relaunch path must not go through ensure_capacity"


def test_relaunch_refuses_out_of_range_slots():
    sup = supervisor(FakeDispatcher(), FakeWorkerSupervisor())
    for bad in (0, 7, -1, 99):
        result = sup.relaunch_managed_slot(bad)
        assert result["slot"] in (1, 6)
        assert result["success"] is True
        assert result["slot"] == max(1, min(6, bad))


def test_relaunch_succeeds_even_when_dispatcher_status_is_broken():
    class Broken(FakeDispatcher):
        def status(self):
            raise RuntimeError("bridge state unreadable")

    workers = FakeWorkerSupervisor()
    sup = supervisor(Broken(), workers)
    result = sup.relaunch_managed_slot(2)
    assert result["success"] is False
    assert "relaunch preparation failed" in result["message"]
    assert workers.resets == [2]


def test_stop_waits_for_a_running_pass():
    """W2-006 (audit/2.md): stop() must be a barrier, not a hint.

    It only set the event and returned, so run_bridge_server went straight on to
    server_close() and PID removal while a tick was still inside
    ensure_capacity(). Measured: the launch completed AFTER shutdown was
    requested -- which recreates the orphan-browser lifecycle this supervisor
    exists to control.
    """
    import threading
    import time as _time

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingWorkers(FakeWorkerSupervisor):
        def ensure_capacity(self, dispatch, desired):
            entered.set()
            release.wait(timeout=10)
            finished.set()
            return super().ensure_capacity(dispatch, desired)

    dispatcher = FakeDispatcher(queued_jobs=1, active_workers=0, free_workers=0)
    sup = DispatchSupervisor(
        dispatcher,
        worker_supervisor=BlockingWorkers(),
        interval_seconds=1.0,
    )
    assert sup.start() is True
    assert entered.wait(timeout=15), "the pass never reached provisioning"

    stopper = {"result": None}
    thread = threading.Thread(target=lambda: stopper.__setitem__("result", sup.stop(timeout=15)))
    thread.start()
    _time.sleep(0.3)
    assert stopper["result"] is None, "stop() returned while a launch-capable tick was active"

    release.set()
    thread.join(timeout=20)
    assert stopper["result"] is True
    assert finished.is_set()
    assert sup._thread is None, "a dead thread handle must not block a restart"


def test_a_stopped_supervisor_can_start_again():
    """`_thread` stayed non-None forever, so start() returned a silent False."""
    sup = DispatchSupervisor(
        FakeDispatcher(queued_jobs=0),
        worker_supervisor=FakeWorkerSupervisor(),
        interval_seconds=1.0,
    )
    assert sup.start() is True
    assert sup.stop(timeout=15) is True
    assert sup.start() is True, "the object refused to run again after a clean stop"
    assert sup.stop(timeout=15) is True


def test_a_pass_that_sees_stop_never_launches():
    """The one irreversible side effect is gated on the stop event."""
    workers = FakeWorkerSupervisor()
    sup = supervisor(FakeDispatcher(queued_jobs=3, active_workers=0, free_workers=0), workers)
    sup.stop(timeout=1)
    result = sup.tick()

    assert workers.calls == [], "a browser was launched after shutdown was requested"
    assert result["skipped"] == "stopping"
    # Reconciliation still ran: it only touches state that already exists.
    assert result["requeued"] == 0
