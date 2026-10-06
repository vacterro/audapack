"""T-191: parallel pack batch contract tests.

The old contract was a sequential completion-chain: dispatch A, wait for its
callback, QTimer-chain B. These tests lock in the parallel replacement:

1. PACK ALL submits every eligible project without waiting for completions.
2. PACK [group] submits every eligible member concurrently.
3. AUTO PACK ALL uses the same parallel batch path.
4. A worker-side barrier proves six blocking jobs are ALL inside their bodies
   before any is allowed to finish, on more than one distinct worker thread.
5. A 24-project supported room is submitted to the dedicated pack pool with
   the same all-entered proof -- no sequential completion chaining fallback.
6. A slow project does not prevent another from reaching COMPLETE, and the
   row timeline stays truthful (IDLE -> QUEUED -> PACKING -> terminal).
7. A RETURNED PackResult(success=False) is a FAILED job.
8. A RAISED exception is a FAILED job.
9. One failed project never cancels siblings; aggregate counts are honest.
10. A second PACK ALL while a batch is active starts zero duplicate jobs.
11. Row-click PACK for a batch-owned project starts zero duplicate jobs;
    a manual PACK ALL coalesces a project that already has an individual
    pack running instead of starting a second one; unrelated individual
    packs still work.
12. Periodic busy ticks create no backlog.
13. ignore_archive / disabled / sourceless projects remain excluded.
14. Pack run generations: allocated once per project on the GUI thread before
    submission, preserved across QUEUED -> PACKING, live progress accepted,
    stale-generation and post-terminal callbacks rejected.
15. Closing the window shuts the batch down terminally: queued jobs dropped,
    in-flight results fenced away from the model, busy() False, no new
    dispatch.
16. The general TaskRunner still executes work while the batch is busy.
"""

from __future__ import annotations

import threading
import time

from audapack.config import AppConfig, AuditsConfig, PackingConfig
from audapack.models import PackResult, Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.pack_batch import MAX_PACK_WORKERS, PackBatchRunner, PackJobResult

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(tmp_path, projects=None) -> AppConfig:
    packing = PackingConfig(output_dir=str(tmp_path / "out"))
    packing.auto_pack_all_enabled = True
    packing.auto_pack_all_interval_minutes = 60
    return AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        packing=packing,
        projects=projects or [],
    )


def _project(tmp_path, pid: str, slot: int, group: str = "MAIN0", **kw) -> Project:
    kwargs = dict(
        id=pid,
        display_name=pid.upper(),
        source_path=str(tmp_path / pid),
        priority_group=group,
        slot=slot,
    )
    kwargs.update(kw)
    return Project(**kwargs)


def _window(tmp_path, projects):
    from audapack.ui_qt.main_window import MainWindow

    service = ProjectService(_config(tmp_path, projects), base_dir=tmp_path)
    return MainWindow(service)


def _fake_pack_job(fn):
    """Adapt a plain ``fake_job(project_id)`` to the real pack signature.

    The batch job calls ``pack_project(project_id, progress_callback=...)``;
    fakes that only want the project id swallow the extra keyword here.
    """

    def wrapper(project_id, progress_callback=None):
        return fn(project_id)

    return wrapper


def _pump(qapp, seconds=10.0, cond=None):
    """Pump the Qt event loop until ``cond`` is true or the timeout expires."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        qapp.processEvents()
        if cond is not None and cond():
            return True
        time.sleep(0.01)
    return cond is not None and cond()


def _ok(project_id: str) -> PackResult:
    return PackResult(project_id=project_id, name=project_id, source_path="", success=True)


# ---------------------------------------------------------------------------
# 1-3. One canonical parallel dispatch
# ---------------------------------------------------------------------------


def test_pack_all_submits_every_project_without_waiting(tmp_path, qapp, monkeypatch):
    w = _window(tmp_path, [_project(tmp_path, f"p{i}", i) for i in range(1, 6)])
    try:
        submitted: list[str] = []
        release = threading.Event()

        def fake_job(project_id):
            submitted.append(project_id)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(list(w.model._projects.values())[:5], "PACK ALL")
        # All five submitted immediately, none blocked on a completion.
        assert _pump(qapp, cond=lambda: len(set(submitted)) == 5), submitted
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


def test_pack_group_submits_members_concurrently(tmp_path, qapp, monkeypatch):
    members = [_project(tmp_path, "ga", 1), _project(tmp_path, "gb", 2)]
    w = _window(tmp_path, members)
    try:
        for p in members:
            w.model._projects[("MAIN1", p.slot)] = p
        entered = {p.id: threading.Event() for p in members}
        release = threading.Event()

        def fake_job(project_id):
            entered[project_id].set()
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        w._on_pack_group("MAIN1")
        assert all(ev.wait(timeout=10) for ev in entered.values()), (
            "both group members must be running concurrently"
        )
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


def test_auto_pack_all_uses_the_same_parallel_batch_path(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, f"p{i}", i) for i in range(1, 5)]
    w = _window(tmp_path, projects)
    try:
        for project in projects:
            w.model._projects[("MAIN0", project.slot)] = project
        submitted: list[str] = []
        release = threading.Event()

        def fake_job(project_id):
            submitted.append(project_id)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        w._on_auto_pack_tick()
        assert _pump(qapp, cond=lambda: set(submitted) == {f"p{i}" for i in range(1, 5)}), (
            f"AUTO PACK ALL must submit every eligible project immediately: {submitted}"
        )
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


# ---------------------------------------------------------------------------
# 4-5. The strongest regression proof: worker-side barriers
# ---------------------------------------------------------------------------


def _barrier_batch(tmp_path, qapp, monkeypatch, pids):
    """All len(pids) jobs must enter before any may leave the barrier.

    The barrier lives ONLY among the workers: the main test thread never joins
    it (Barrier.wait returns a party index, and a main-thread party would both
    falsify the index-0 return and let the batch start before every worker
    entered). The workers set ``all_entered`` only after the barrier released,
    which by construction means every body was entered first.
    """
    n = len(pids)
    # The supported Project Room grid: canonical groups x 6 slots each.
    canonical_groups = ["MAIN0", "MAIN1", "SIDE0", "SIDE1"]
    projects = [
        _project(tmp_path, pid, (i % 6) + 1, group=canonical_groups[(i // 6) % len(canonical_groups)])
        for i, pid in enumerate(pids)
    ]
    w = _window(tmp_path, projects)
    for project in projects:
        w.model._projects[(project.priority_group, project.slot)] = project
    barrier = threading.Barrier(n)
    all_entered = threading.Event()
    release = threading.Event()
    entered: list[str] = []
    thread_ids: dict[str, int] = {}

    def fake_job(project_id):
        entered.append(project_id)
        thread_ids[project_id] = threading.get_ident()
        try:
            barrier.wait(timeout=20)
        except threading.BrokenBarrierError:  # pragma: no cover - diagnosis only
            pass
        all_entered.set()
        release.wait(timeout=20)
        return _ok(project_id)

    monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
    return w, all_entered, release, entered, thread_ids


def test_six_jobs_all_enter_before_any_finishes_on_multiple_worker_threads(
    tmp_path, qapp, monkeypatch
):
    pids = [f"p{i}" for i in range(1, 7)]
    w, all_entered, release, entered, thread_ids = _barrier_batch(
        tmp_path, qapp, monkeypatch, pids
    )
    try:
        ordered, _skipped = w._pack_all_eligible_projects()
        assert [p.id for p in ordered] == pids
        assert w._start_parallel_pack_batch(ordered, "PACK ALL")
        assert all_entered.wait(timeout=20), (
            f"all six worker bodies must enter before any is released; entered={entered}"
        )
        # No completion was required to launch jobs 2..6: the barrier could not
        # have released otherwise, and the release happens inside the workers.
        assert set(entered) == set(pids)
        assert len(set(thread_ids.values())) > 1, (
            f"more than one distinct worker thread must be active: {thread_ids}"
        )
        # Independent completion order: release everything, expect honest
        # success for every project and an empty (finalized) batch.
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
        for pid in pids:
            assert w.model._pack_states[pid][0] == "COMPLETE"
    finally:
        release.set()
        w.close()


def test_24_project_room_is_submitted_to_the_pack_pool_without_sequential_chaining(
    tmp_path, qapp, monkeypatch
):
    pids = [f"p{i}" for i in range(1, 25)]
    w, all_entered, release, entered, thread_ids = _barrier_batch(
        tmp_path, qapp, monkeypatch, pids
    )
    try:
        ordered, _skipped = w._pack_all_eligible_projects()
        assert [p.id for p in ordered] == pids, "the supported room is 4 groups x 6 slots"
        assert w._start_parallel_pack_batch(ordered, "PACK ALL")
        # If any completion chained the next job, the 24-party barrier could
        # never be satisfied by the bounded pool: all 24 must be inside their
        # bodies at once (MAX_PACK_WORKERS >= 24).
        assert MAX_PACK_WORKERS >= 24
        assert all_entered.wait(timeout=30), (
            f"all 24 worker bodies must enter before any finishes; entered={len(entered)}"
        )
        assert set(entered) == set(pids)
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
        for pid in pids:
            assert w.model._pack_states[pid][0] == "COMPLETE"
    finally:
        release.set()
        w.close()


# ---------------------------------------------------------------------------
# 6. Truthful row timeline
# ---------------------------------------------------------------------------


def test_row_timeline_idle_queued_packing_complete_without_keeping_siblings_queued(
    tmp_path, qapp, monkeypatch
):
    projects = [_project(tmp_path, "slow", 1), _project(tmp_path, "fast", 2)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            if project_id == "slow":
                release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        for p in projects:
            assert w.model._pack_states.get(p.id, ("IDLE", ""))[0] == "IDLE"
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        # Ownership taken: every owned project is QUEUED immediately.
        for p in projects:
            assert w.model._pack_states[p.id][0] == "QUEUED"
        # fast reaches COMPLETE while slow is still packing -- one slow project
        # must not keep its siblings queued behind it.
        assert _pump(qapp, cond=lambda: w.model._pack_states["fast"][0] == "COMPLETE")
        assert w.model._pack_states["slow"][0] in ("PACKING", "QUEUED")
        release.set()
        assert _pump(qapp, cond=lambda: w.model._pack_states["slow"][0] == "COMPLETE")
    finally:
        w.close()


# ---------------------------------------------------------------------------
# 7-9. Failure forms, isolation and honest aggregation
# ---------------------------------------------------------------------------


def test_returned_failed_packresult_is_failed_and_siblings_continue(
    tmp_path, qapp, monkeypatch
):
    """Failure form A: the packing service REPORTS failure by returning a
    PackResult with success=False, without raising."""
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["ok1", "bad", "ok2"], start=1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            release.wait(timeout=10)
            if project_id == "bad":
                return PackResult(
                    project_id=project_id,
                    name=project_id,
                    source_path="",
                    success=False,
                    error_message="source vanished",
                )
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
        assert w.model._pack_states["bad"][0] == "FAILED"
        assert "source vanished" in w.model._pack_states["bad"][1]
        assert w.model._pack_states["ok1"][0] == "COMPLETE"
        assert w.model._pack_states["ok2"][0] == "COMPLETE"
    finally:
        w.close()


def test_returned_failure_keeps_exact_reason_in_model_and_status_bar(
    tmp_path, qapp
):
    project = _project(tmp_path, "bad", 1)
    w = _window(tmp_path, [project])
    failure = (
        "SAIPEN audit-manifest precondition failed "
        "(AUDIT_MANIFEST_STALE_REGENERATION_FAILED): launcher discovery failed"
    )
    try:
        result = PackJobResult(
            project_id=project.id,
            success=False,
            error_message=failure,
            payload=PackResult(
                project_id=project.id,
                name=project.display_name,
                source_path=project.source_path,
                success=False,
                status="FAILED_INVENTORY",
                error_message=failure,
            ),
        )

        w._apply_batch_job_result(result)

        assert w.model._pack_states[project.id][0] == "FAILED"
        assert failure in w.model._pack_states[project.id][1]
        assert failure in w.statusBar().currentMessage()
        assert "AUDIT_MANIFEST_STALE_REGENERATION_FAILED" in w.statusBar().currentMessage()
    finally:
        w.close()


def test_reserved_archive_collision_is_classified_by_name_in_status_and_hover(
    tmp_path, qapp
):
    """A reserved archive-control collision must not read as a secret refusal.

    The failed _ZAICODE pack showed the operator
    ``[FAILED_INVENTORY][TRACKED_HARD_DENY_CONFLICT]`` for a Git-tracked
    ``_AUDAPACK_MANIFEST.json`` -- a naming collision reported as hard safety.
    Project Room must expose all four facts: the project, the conflicting
    relative path, the exact classification, and the repair.
    """
    from audapack.source_inventory import (
        CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT,
        reserved_archive_conflict_message,
    )
    from audapack.ui_qt.models.project_delegate import ProjectItemDelegate

    rel = "_AUDAPACK_MANIFEST.json"
    project = _project(tmp_path, "zaicode", 1)
    w = _window(tmp_path, [project])
    expected = (
        f"[FAILED_INVENTORY][{CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT}] "
        f"{reserved_archive_conflict_message(rel)} ({rel})"
    )
    try:
        w._apply_batch_job_result(
            PackJobResult(
                project_id=project.id,
                success=False,
                error_message=reserved_archive_conflict_message(rel),
                payload=PackResult(
                    project_id=project.id,
                    name=project.display_name,
                    source_path=project.source_path,
                    success=False,
                    status="FAILED_INVENTORY",
                    error_code=CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT,
                    error_message=f"[{CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT}] "
                    f"{reserved_archive_conflict_message(rel)} ({rel})",
                    first_error_path=rel,
                ),
            )
        )

        bar = w.statusBar().currentMessage()
        # Project name, classification, path and remediation, in one line.
        assert project.display_name in bar
        assert "[FAILED_INVENTORY][TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT]" in bar
        assert rel in bar
        assert "Remove it from source control" in bar
        assert "hard-safety" not in bar

        # The row's Pack state carries the same line, so the ⓘ detail popup
        # shows it too instead of a bare message with no classification.
        state, message = w.model._pack_states[project.id]
        assert state == "FAILED"
        assert message == expected
        hover = w.model.data(
            w.model.index_for_project_id(project.id), w.model.ROLES["hover_info"]
        )
        html = ProjectItemDelegate.build_tooltip(hover)
        assert project.display_name in html
        assert rel in html
        assert CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT in html
        assert "Remove it from source control" in html
    finally:
        w.close()


def test_pack_failure_detail_is_backwards_compatible_without_a_code():
    from audapack.ui_qt.main_window import pack_failure_detail

    # A failure whose stage publishes no code keeps the old shape exactly:
    # one status bracket, the message, then the path when it is not in it.
    assert (
        pack_failure_detail(
            PackResult(
                project_id="p",
                name="P",
                source_path="s",
                success=False,
                status="FAILED_INVENTORY",
                error_message="source vanished",
                first_error_path="gone.py",
            )
        )
        == "[FAILED_INVENTORY] source vanished (gone.py)"
    )
    assert pack_failure_detail(PackResult(project_id="p", name="P", source_path="s")) == ""


def test_raised_exception_is_failed_and_siblings_continue(tmp_path, qapp, monkeypatch):
    """Failure form B: an unexpected internal error raised by a job."""
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["ok1", "bad", "ok2"], start=1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            release.wait(timeout=10)
            if project_id == "bad":
                raise RuntimeError("disk exploded")
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
        assert w.model._pack_states["bad"][0] == "FAILED"
        assert "disk exploded" in w.model._pack_states["bad"][1]
        assert w.model._pack_states["ok1"][0] == "COMPLETE"
        assert w.model._pack_states["ok2"][0] == "COMPLETE"
    finally:
        w.close()


def test_one_failure_does_not_cancel_siblings(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["ok1", "bad", "ok2"], start=1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()
        started_ok: list[str] = []
        started_ok_lock = threading.Lock()

        def fake_job(project_id):
            with started_ok_lock:
                started_ok.append(project_id)
            release.wait(timeout=10)
            if project_id == "bad":
                return PackResult(
                    project_id=project_id, name=project_id, source_path="",
                    success=False, error_message="source vanished",
                )
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
        assert w.model._pack_states["ok1"][0] == "COMPLETE"
        assert w.model._pack_states["ok2"][0] == "COMPLETE"
        assert w.model._pack_states["bad"][0] == "FAILED"
    finally:
        w.close()


def test_aggregate_never_claims_all_packed_when_one_returned_failure(
    tmp_path, qapp, monkeypatch
):
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["ok1", "bad"], start=1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            release.wait(timeout=10)
            if project_id == "bad":
                return PackResult(
                    project_id=project_id, name=project_id, source_path="",
                    success=False, error_message="boom",
                )
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        aggregates: list[tuple[int, str, str]] = []
        w._pack_batch_runner.batch_finished.connect(
            lambda b, lbl, s: aggregates.append((b, lbl, s))
        )
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        release.set()
        assert _pump(qapp, cond=lambda: bool(aggregates))
        _batch_id, label, summary = aggregates[0]
        assert label == "PACK ALL"
        # Honest counts: one real failure can never read as 2/2 packed.
        assert "1/2 packed" in summary and "1 failed" in summary
        assert "2/2" not in summary
    finally:
        w.close()


def test_runner_classifies_returned_failure_and_exception_alike(qapp):
    """Unit level: both failure forms produce success=False jobs that keep
    their actionable message; a returned failure is never converted into an
    exception just to satisfy the runner."""
    runner = PackBatchRunner(max_workers=2)
    try:
        results: list = []
        done = threading.Event()

        def job_a(project_id):
            return PackResult(
                project_id=project_id, name=project_id, source_path="",
                success=False, error_message="returned failure",
            )

        def job_b(project_id):
            raise ValueError("raised failure")

        class _P:
            def __init__(self, pid):
                self.id = pid

        batch = runner.start_batch(
            [_P("a"), _P("b")],
            "unit",
            job_fn=lambda pid: job_a(pid) if pid == "a" else job_b(pid),
            on_job_done=results.append,
            on_batch_done=lambda _b: done.set(),
        )
        assert batch is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not done.is_set():
            qapp.processEvents()
            time.sleep(0.01)
        assert done.is_set()
        by_id = {r.project_id: r for r in results}
        assert by_id["a"].success is False
        assert "returned failure" in by_id["a"].error_message
        assert by_id["a"].payload is not None, "the failed payload is preserved"
        assert by_id["b"].success is False
        assert "raised failure" in by_id["b"].error_message
        assert runner.active_batch() is None
    finally:
        runner.shutdown()


# ---------------------------------------------------------------------------
# 10-11. Ownership / coalescing, both directions
# ---------------------------------------------------------------------------


def test_second_pack_all_starts_zero_duplicate_jobs(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, f"p{i}", i) for i in range(1, 4)]
    w = _window(tmp_path, projects)
    try:
        calls: list[str] = []
        release = threading.Event()

        def fake_job(project_id):
            calls.append(project_id)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        _pump(qapp, cond=lambda: len(calls) == 3)
        in_flight = len(calls)
        assert not w._start_parallel_pack_batch(projects, "PACK ALL"), (
            "a second PACK ALL must be refused while a batch is active"
        )
        assert len(calls) == in_flight, "no duplicate jobs may start"
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


def test_row_click_pack_for_batch_owned_project_starts_zero_duplicate_jobs(
    tmp_path, qapp, monkeypatch
):
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["owned", "other"], start=1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch([projects[0]], "PACK ALL")
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.owns("owned"))
        assert not w._pack_batch_runner.owns("other")
        # Manual PACK on a batch-owned project is refused (no duplicate).
        assert not w._start_project_pack(projects[0], flash=False)
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


def test_manual_pack_all_coalesces_project_with_live_individual_pack(
    tmp_path, qapp, monkeypatch
):
    """Direction 2: an individual PACK for A is already running when PACK ALL
    fires. The batch must NOT start a second concurrent pack for A; it
    submits the other projects and reports A as skipped."""
    projects = [_project(tmp_path, "busy", 1), _project(tmp_path, "free", 2)]
    w = _window(tmp_path, projects)
    try:
        individual_release = threading.Event()
        batch_release = threading.Event()
        pack_calls: list[str] = []
        calls_lock = threading.Lock()

        def fake_pack(project_id, progress_callback=None):
            with calls_lock:
                pack_calls.append(project_id)
            if project_id == "busy":
                individual_release.wait(timeout=10)
            else:
                batch_release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)
        # Start the individual pack for "busy" through the real dispatcher.
        assert w._start_project_pack(projects[0], flash=False)
        assert _pump(qapp, cond=lambda: w.task_runner.is_running("pack:busy"))
        assert _pump(qapp, cond=lambda: "busy" in pack_calls)
        # Now the manual batch over BOTH projects.
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        assert _pump(qapp, cond=lambda: "free" in pack_calls)
        with calls_lock:
            assert pack_calls.count("busy") == 1, (
                f"no duplicate archive transaction for the individually packed project: {pack_calls}"
            )
        # Truthful report: the busy project is named as skipped.
        assert "already packing" in w.statusBar().currentMessage()
        # The batch does not own the coalesced project; the individual pack
        # still completes normally through its own path.
        assert not w._pack_batch_runner.owns("busy")
        assert w._pack_batch_runner.owns("free")
        individual_release.set()
        assert _pump(qapp, cond=lambda: w.model._pack_states["busy"][0] == "COMPLETE")
        batch_release.set()
        assert _pump(qapp, cond=lambda: w.model._pack_states["free"][0] == "COMPLETE")
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        individual_release.set()
        batch_release.set()
        w.close()


def test_manual_pack_all_with_every_project_individually_busy_starts_nothing(
    tmp_path, qapp, monkeypatch
):
    projects = [_project(tmp_path, "busy1", 1), _project(tmp_path, "busy2", 2)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()
        pack_calls: list[str] = []
        calls_lock = threading.Lock()

        def fake_pack(project_id, progress_callback=None):
            with calls_lock:
                pack_calls.append(project_id)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)
        for p in projects:
            assert w._start_project_pack(p, flash=False)
        assert _pump(qapp, cond=lambda: len(pack_calls) == 2)
        assert not w._start_parallel_pack_batch(projects, "PACK ALL"), (
            "a batch over only individually-busy projects must start nothing"
        )
        assert _pump(qapp, cond=lambda: len(pack_calls) == 2)
        release.set()
    finally:
        release.set()
        w.close()


def test_individual_pack_outside_batch_ownership_still_works(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["batched", "solo"], start=1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_pack(project_id, progress_callback=None):
            if project_id == "batched":
                release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)
        # Batch owns only "batched"; "solo" is a manual TaskRunner pack that
        # must not be globally disabled by the active batch.
        assert w._start_parallel_pack_batch([projects[0]], "PACK ALL")
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.owns("batched"))
        assert w._start_project_pack(projects[1], flash=False)
        assert w.task_runner.is_running("pack:solo")
        assert _pump(qapp, cond=lambda: w.model._pack_states["solo"][0] == "COMPLETE"), (
            "an individual pack for a non-batch-owned project must still complete"
        )
        assert not w._pack_batch_runner.owns("solo")
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        release.set()
        w.close()


# ---------------------------------------------------------------------------
# 12-13. Timer behavior + eligibility
# ---------------------------------------------------------------------------


def test_periodic_busy_ticks_create_no_backlog(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, f"p{i}", i) for i in range(1, 4)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        for _ in range(5):
            w._on_auto_pack_tick()
            assert w._pack_batch_runner.active_batch() is not None
            # A busy tick leaves no residual state on any project.
            assert all(
                w.model._pack_states.get(p.id, ("", ""))[0] in ("QUEUED", "PACKING")
                for p in projects
            )
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


def test_ignore_archive_disabled_and_sourceless_remain_excluded(tmp_path, qapp):
    projects = [
        _project(tmp_path, "good", 1),
        _project(tmp_path, "ignored", 2, ignore_archive=True),
        _project(tmp_path, "disabled", 3, enabled=False),
        _project(tmp_path, "nosrc", 4, source_path=""),
    ]
    w = _window(tmp_path, projects)
    try:
        for project in projects:
            w.model._projects[("MAIN0", project.slot)] = project
        ordered, skipped = w._pack_all_eligible_projects()
        assert [p.id for p in ordered] == ["good"]
        assert skipped == 1
    finally:
        w.close()


# ---------------------------------------------------------------------------
# 14. Pack run generations / progress ownership
# ---------------------------------------------------------------------------


def test_queued_to_packing_preserves_the_generation_terminal_invalidates(tmp_path, qapp):
    """Model invariant: NEW PACK RUN allocates once; QUEUED -> PACKING keeps
    the same run id; COMPLETE/FAILED invalidates it."""
    w = _window(tmp_path, [_project(tmp_path, "gen", 1)])
    try:
        pid = "gen"
        assert w.model.get_current_pack_run_id(pid) == 0
        w.model.update_pack_state(pid, "QUEUED")
        queued_run = w.model.get_current_pack_run_id(pid)
        assert queued_run == 1
        w.model.update_pack_state(pid, "PACKING")
        assert w.model.get_current_pack_run_id(pid) == queued_run, (
            "QUEUED -> PACKING of one logical pack run must not create a second generation"
        )
        w.model.update_pack_state(pid, "COMPLETE", "archive.zip")
        assert w.model.get_current_pack_run_id(pid) == queued_run + 1
        # A new run allocates the next generation again.
        w.model.update_pack_state(pid, "QUEUED")
        assert w.model.get_current_pack_run_id(pid) == queued_run + 2
    finally:
        w.close()


def test_batch_allocates_one_generation_before_submission_and_progress_lands(
    tmp_path, qapp, monkeypatch
):
    """The worker never reads mutable model lifecycle state: the run id is
    allocated on the GUI thread before submission and carried into the job,
    so live progress reaches the correct project while it packs."""
    projects = [_project(tmp_path, p, i) for i, p in enumerate(["prog1", "prog2"], start=1)]
    w = _window(tmp_path, projects)
    try:
        got_progress: dict[str, tuple] = {}
        release = threading.Event()

        def fake_pack(project_id, progress_callback=None):
            # Generations were allocated BEFORE any worker exists.
            run_inside = w.model.get_current_pack_run_id(project_id)
            assert run_inside > 0
            if project_id == "prog1":
                progress_callback(3, 300, "src/a.txt")
            got_progress[project_id] = (run_inside,)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        assert _pump(qapp, cond=lambda: len(got_progress) == 2)
        # The generation observed inside the worker is still the current one
        # after QUEUED -> PACKING, and the live progress callback was accepted.
        for p in projects:
            assert w.model.get_current_pack_run_id(p.id) == got_progress[p.id][0]
        w._flush_pack_progress()
        progress = w.model._pack_progress.get("prog1")
        assert progress is not None, "live progress must reach the model while packing"
        assert progress["files_added"] == 3 and progress["bytes_written"] == 300
        assert progress["current_path"] == "src/a.txt"
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        release.set()
        w.close()


def test_stale_generation_and_post_terminal_progress_are_rejected(tmp_path, qapp):
    w = _window(tmp_path, [_project(tmp_path, "stale", 1)])
    try:
        pid = "stale"
        # Run 1 packing.
        w.model.update_pack_state(pid, "PACKING")
        run1 = w.model.get_current_pack_run_id(pid)
        w.model.update_pack_progress(pid, 1, 10, "old.txt", run_id=run1)
        assert w.model._pack_progress[pid]["bytes_written"] == 10
        # A callback from an OLDER (or unregistered) generation is a no-op.
        w.model.update_pack_progress(pid, 9, 999, "stale.txt", run_id=run1 - 1)
        assert w.model._pack_progress[pid]["bytes_written"] == 10
        # Terminal state: the run id is invalidated and buffered progress dropped.
        w.model.update_pack_state(pid, "COMPLETE", "archive.zip")
        assert pid not in w.model._pack_progress
        # A late callback carrying the pre-terminal generation is rejected...
        w.model.update_pack_progress(pid, 5, 500, "late.txt", run_id=run1)
        assert pid not in w.model._pack_progress
        # ...and so is one carrying ANY id once the state is terminal.
        w.model.update_pack_progress(pid, 5, 500, "late.txt", run_id=run1 + 1)
        assert pid not in w.model._pack_progress
    finally:
        w.close()


# ---------------------------------------------------------------------------
# 15. CloseEvent / shutdown ownership
# ---------------------------------------------------------------------------


def test_runner_shutdown_is_terminal_and_fences_running_and_queued_jobs(qapp):
    """Runner level: >=1 running job, >=1 queued job, shutdown, worker release
    after shutdown, zero post-close callback invocations."""
    runner = PackBatchRunner(max_workers=1)
    started_first = threading.Event()
    release = threading.Event()
    second_ran = threading.Event()
    callbacks: list = []
    batch_done: list = []

    def job(project_id):
        if project_id == "first":
            started_first.set()
            release.wait(timeout=10)
        else:
            second_ran.set()
        return _ok(project_id)

    class _P:
        def __init__(self, pid):
            self.id = pid

    batch = runner.start_batch(
        [_P("first"), _P("second")],
        "shutdown",
        job_fn=job,
        on_job_done=callbacks.append,
        on_batch_done=batch_done.append,
    )
    assert batch is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not started_first.is_set():
        qapp.processEvents()
        time.sleep(0.01)
    assert started_first.is_set()
    assert runner.busy() and runner.active_batch() is not None
    # "second" is queued behind the single worker.

    runner.shutdown()
    # Terminal immediately: ownership retired, dispatch refused.
    assert runner.busy() is False
    assert runner.active_batch() is None
    assert runner.start_batch([_P("x")], "after", job_fn=job) is None

    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    # The in-flight worker's result was fenced away; the queued job was
    # dropped and never ran.
    assert callbacks == []
    assert batch_done == []
    assert not second_ran.is_set()


def test_close_during_batch_cannot_mutate_model_after_close(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, f"p{i}", i) for i in range(1, 4)]
    w = _window(tmp_path, projects)
    release = threading.Event()
    try:
        def fake_job(project_id):
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.busy())
        states_before_close = dict(w.model._pack_states)
        # Close the window while every job is still blocked inside its worker.
        w.close()
        # Shutdown is terminal on the runner.
        assert w._pack_batch_runner.busy() is False
        assert w._pack_batch_runner.active_batch() is None
        assert not w._pack_batch_runner.start_batch(
            projects, "PACK ALL", job_fn=lambda pid: _ok(pid)
        )
        # Release the workers AFTER close: their late results must not touch
        # the model (no COMPLETE/FAILED flip post-close).
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert w.model._pack_states == states_before_close, (
            "no post-close model update may leak from an in-flight pack worker"
        )
    finally:
        release.set()
        try:
            w.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 16. Pool independence
# ---------------------------------------------------------------------------


def test_task_runner_still_executes_while_pack_batch_is_busy(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, f"p{i}", i) for i in range(1, 4)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()

        def fake_job(project_id):
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", _fake_pack_job(fake_job))
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        # Unrelated TaskRunner work must complete promptly.
        unrelated: list[int] = []
        w.task_runner.submit("unrelated:probe", lambda: 42, on_success=unrelated.append)
        assert _pump(qapp, cond=lambda: unrelated == [42]), (
            "the shared TaskRunner must never be starved by a pack batch"
        )
        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
    finally:
        w.close()


def test_pack_pool_is_dedicated_and_never_resizes_the_global_pool(qapp):
    from PySide6.QtCore import QThreadPool

    before = QThreadPool.globalInstance().maxThreadCount()
    runner = PackBatchRunner(max_workers=8)
    try:
        assert runner.max_workers() == 8
        assert runner._pool is not QThreadPool.globalInstance()
        assert QThreadPool.globalInstance().maxThreadCount() == before
    finally:
        runner.shutdown()
    # Safety ceiling exists and covers the supported batch size (24 slots).
    assert MAX_PACK_WORKERS >= 24
