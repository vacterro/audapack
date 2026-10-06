"""PERF-005 (SRC-044): pack progress timer lifecycle must match pack lifecycle.

audit/10.md residual defect: MainWindow started ``pack_progress_timer`` (250 ms)
unconditionally during initialization and never stopped it, so an idle Project
Room scheduled four empty progress wakeups per second for the whole window
lifetime -- roughly 14,400 per hour, all of them returning immediately because
``_pack_progress_buffer`` was empty.

The contract these tests lock in:

* an idle window has NO active pack-progress timer;
* the first active pack (individual or batch) starts exactly one timer;
* overlapping packs keep one timer alive until every one settles;
* the final buffered snapshot is flushed to the model BEFORE the terminal
  state is applied, so every emitted progress value was genuinely rendered;
* a later pack restarts it;
* worker-thread callbacks never touch the QTimer -- they only write the
  lock-guarded buffer.
"""

from __future__ import annotations

import threading

from audapack.config import AppConfig, AuditsConfig, PackingConfig
from audapack.models import PackResult, Project
from audapack.services.project_service import ProjectService


def _config(tmp_path, projects=None) -> AppConfig:
    packing = PackingConfig(output_dir=str(tmp_path / "out"))
    packing.auto_pack_all_enabled = True
    packing.auto_pack_all_interval_minutes = 60
    return AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        packing=packing,
        projects=projects or [],
    )


def _project(tmp_path, pid: str, slot: int, group: str = "MAIN0") -> Project:
    return Project(
        id=pid,
        display_name=pid.upper(),
        source_path=str(tmp_path / pid),
        priority_group=group,
        slot=slot,
    )


def _window(tmp_path, projects):
    from audapack.ui_qt.main_window import MainWindow

    service = ProjectService(_config(tmp_path, projects), base_dir=tmp_path)
    return MainWindow(service)


def _ok(project_id: str) -> PackResult:
    return PackResult(project_id=project_id, name=project_id, source_path="", success=True)


def _pump(qapp, seconds=10.0, cond=None):
    import time

    start = time.monotonic()
    while time.monotonic() - start < seconds:
        qapp.processEvents()
        if cond is not None and cond():
            return True
        time.sleep(0.01)
    return cond is not None and cond()


def test_idle_window_has_no_active_pack_progress_timer(tmp_path, qapp):
    w = _window(tmp_path, [_project(tmp_path, "idle", 1)])
    try:
        assert not w.pack_progress_timer.isActive(), (
            "an idle Project Room must not own a permanent 4 Hz wakeup"
        )
        assert w._active_pack_runs == set()
    finally:
        w.close()


def test_first_pack_starts_one_timer_and_last_settle_stops_it(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, "one", 1)]
    w = _window(tmp_path, projects)
    try:
        release = threading.Event()
        rendered: list[dict] = []
        orig_flush = w._flush_pack_progress

        def observed_flush():
            orig_flush()
            # A non-empty drain is a progress update that genuinely rendered.
            rendered.append(dict(w.model._pack_progress))

        monkeypatch.setattr(w, "_flush_pack_progress", observed_flush)

        def fake_pack(project_id, progress_callback=None):
            progress_callback(4, 400, "src/a.txt")
            # The final snapshot is buffered after the last periodic tick and
            # must be flushed on completion, before the terminal state.
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)

        assert not w.pack_progress_timer.isActive()
        assert w._start_project_pack(projects[0])

        assert _pump(qapp, cond=lambda: len(w._active_pack_runs) == 1)
        assert w.pack_progress_timer.isActive(), "first active pack arms the single timer"
        assert w.pack_progress_timer.interval() == 250

        release.set()
        assert _pump(qapp, cond=lambda: not w._active_pack_runs)
        assert not w.pack_progress_timer.isActive(), "last settled pack stops the timer"
        # The final buffered snapshot rendered while the run was live.
        assert any(entry.get("one", {}).get("bytes_written") == 400 for entry in rendered)
        assert w.model._pack_progress.get("one") is None, "terminal state retires progress"
        assert w._pack_progress_buffer == {}
    finally:
        release.set()
        w.close()


def test_overlapping_packs_keep_one_timer_until_both_settle(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, "a", 1), _project(tmp_path, "b", 2)]
    w = _window(tmp_path, projects)
    release = threading.Event()
    try:
        entered: set[str] = set()

        def fake_pack(project_id, progress_callback=None):
            progress_callback(1, 10, f"src/{project_id}.txt")
            entered.add(project_id)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)

        assert w._start_project_pack(projects[0])
        assert w._start_project_pack(projects[1])
        assert _pump(qapp, cond=lambda: entered == {"a", "b"})
        assert w._active_pack_runs == {"pack:a", "pack:b"}
        assert w.pack_progress_timer.isActive()

        release.set()
        assert _pump(qapp, cond=lambda: not w._active_pack_runs)
        assert not w.pack_progress_timer.isActive()
    finally:
        release.set()
        w.close()


def test_batch_pack_arms_timer_and_stops_after_last_job(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, "b1", 1), _project(tmp_path, "b2", 2)]
    w = _window(tmp_path, projects)
    release = threading.Event()
    try:
        entered: set[str] = set()

        def fake_pack(project_id, progress_callback=None):
            progress_callback(2, 20, f"src/{project_id}.txt")
            entered.add(project_id)
            release.wait(timeout=10)
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)
        assert w._start_parallel_pack_batch(projects, "PACK ALL")
        assert _pump(qapp, cond=lambda: entered == {"b1", "b2"})
        assert w._active_pack_runs == {"pack:b1", "pack:b2"}
        assert w.pack_progress_timer.isActive()

        release.set()
        assert _pump(qapp, cond=lambda: w._pack_batch_runner.active_batch() is None)
        assert _pump(qapp, cond=lambda: not w._active_pack_runs)
        assert not w.pack_progress_timer.isActive()
        assert w.model._pack_states["b1"][0] == "COMPLETE"
        assert w.model._pack_states["b2"][0] == "COMPLETE"
    finally:
        release.set()
        w.close()


def test_later_pack_restarts_the_timer(tmp_path, qapp, monkeypatch):
    projects = [_project(tmp_path, "again", 1)]
    w = _window(tmp_path, projects)
    try:
        def fake_pack(project_id, progress_callback=None):
            progress_callback(1, 100, "src/one.txt")
            return _ok(project_id)

        monkeypatch.setattr(w._packing, "pack_project", fake_pack)

        assert w._start_project_pack(projects[0])
        assert _pump(qapp, cond=lambda: not w._active_pack_runs)
        assert not w.pack_progress_timer.isActive()

        assert w._start_project_pack(projects[0])
        assert w.pack_progress_timer.isActive(), "a later pack restarts the timer"
        assert _pump(qapp, cond=lambda: not w._active_pack_runs)
        assert not w.pack_progress_timer.isActive()
    finally:
        w.close()


def test_worker_progress_callback_never_touches_the_qtimer(tmp_path, qapp):
    """The worker-thread callback is buffer-only; every QTimer mutation is GUI-owned."""
    w = _window(tmp_path, [_project(tmp_path, "t", 1)])
    try:
        from PySide6.QtCore import QThread

        callback = w._make_pack_progress_callback("t", 1)
        observed: dict[str, object] = {}

        def worker():
            observed["thread"] = QThread.currentThread()
            callback(5, 500, "src/x.txt")
            observed["armed_from_worker"] = w.pack_progress_timer.isActive()

        out_of_gui = []
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        out_of_gui.append(observed["armed_from_worker"])

        # The callback only wrote the lock-guarded buffer.
        assert w._pack_progress_buffer["t"] == (5, 500, 1, "src/x.txt")
        assert out_of_gui == [False], (
            "a worker thread must never start the QTimer itself"
        )
    finally:
        w.close()
