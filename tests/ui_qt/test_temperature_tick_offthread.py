"""The 60 s temperature tick must not touch the filesystem on the GUI thread.

PERF-001 moved the bounded source-tree walk off the paint path and into
``update_temperature_all()``, which the temperature timer calls directly on the
GUI thread. The freshness TTL is 10 s and the tick is 60 s, so every project was
stale at every tick: N projects x an archive resolve/stat plus up to 0.15 s of
``os.walk``, in one uninterruptible block. Observed as a ~1 s freeze at
intervals -- and dragging the window during it made the window jump to the
cursor once the thread came back.
"""

from __future__ import annotations

import pytest

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService


@pytest.fixture
def window(tmp_path, qapp):
    from audapack.ui_qt.main_window import MainWindow

    config = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(
                id=f"p{index}",
                display_name=f"Project {index}",
                source_path=str(tmp_path / f"p{index}"),
                priority_group="MAIN0",
                slot=index,
            )
            for index in range(1, 7)
        ],
    )
    win = MainWindow(ProjectService(config, base_dir=tmp_path))
    yield win
    win.close()


def _mark_every_project_stale(window) -> list[str]:
    ids = []
    for project in window._service.list_projects():
        window.model.invalidate_archive_fresh(project.id)
        ids.append(project.id)
    return ids


def _record_computes(window, monkeypatch) -> list[str]:
    computed: list[str] = []
    original = window.model._compute_archive_fresh

    def _spy(proj, probe_source=True):
        computed.append(proj.id)
        return original(proj, probe_source=probe_source)

    monkeypatch.setattr(window.model, "_compute_archive_fresh", _spy)
    return computed


def test_the_tick_hands_the_disk_work_to_a_worker(window, monkeypatch):
    ids = _mark_every_project_stale(window)
    computed = _record_computes(window, monkeypatch)
    submitted: list[tuple[str, object]] = []
    monkeypatch.setattr(
        window.task_runner,
        "submit_coalesced",
        lambda key, fn, on_success=None, on_error=None: submitted.append((key, fn)),
    )

    window._on_temperature_tick()

    assert computed == [], "the tick itself must do no filesystem work"
    assert [key for key, _fn in submitted] == ["archive-fresh:recompute"]

    # The submitted callable is what carries the walk -- and it recomputes every
    # project the tick claimed, not just the one that happened to be painted.
    submitted[0][1]()
    assert sorted(computed) == sorted(ids)


def test_the_worker_result_is_published_back_to_the_model(window, monkeypatch):
    ids = _mark_every_project_stale(window)
    monkeypatch.setattr(
        window.task_runner,
        "submit_coalesced",
        lambda key, fn, on_success=None, on_error=None: on_success and on_success(fn()),
    )

    window._on_temperature_tick()

    for project_id in ids:
        assert project_id in window.model._archive_fresh_cache, project_id
        assert window.model._archive_fresh_cache[project_id]["computed_at"] > 0


def test_a_tick_with_nothing_stale_submits_no_work(window, monkeypatch):
    window.model.take_stale_archive_projects()
    submitted: list[str] = []
    monkeypatch.setattr(
        window.task_runner,
        "submit_coalesced",
        lambda key, fn, on_success=None, on_error=None: submitted.append(key),
    )

    window._on_temperature_tick()

    assert submitted == []
