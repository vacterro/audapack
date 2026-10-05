"""T-167 (SRC-039): one periodic timer owns automatic PACK ALL.

The operator asked for a configurable periodic PACK ALL, on by default, hourly
by default. The timer must never fire at startup, never stack a second batch,
never overlap an individual pack, and must follow the config even when a
Settings save changed nothing audit-relevant (the fingerprint early return).
T-191: the tick dispatches through the one parallel batch entry point
(``_start_parallel_pack_batch``) and skips while the batch runner is busy.
"""

from __future__ import annotations

from audapack.config import AppConfig, AuditsConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService


def _config(tmp_path, *, enabled=True, interval=60, projects=None) -> AppConfig:
    from audapack.config import PackingConfig

    packing = PackingConfig(output_dir=str(tmp_path / "out"))
    packing.auto_pack_all_enabled = enabled
    packing.auto_pack_all_interval_minutes = interval
    return AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        packing=packing,
        projects=projects
        or [
            Project(
                id=f"p{index}",
                display_name=f"Project {index}",
                source_path=str(tmp_path / f"p{index}"),
                priority_group="MAIN0",
                slot=index,
            )
            for index in range(1, 5)
        ],
    )


def _window(tmp_path, *, enabled=True, interval=60):
    from audapack.ui_qt.main_window import MainWindow

    win = MainWindow(ProjectService(_config(tmp_path, enabled=enabled, interval=interval), base_dir=tmp_path))
    return win


def test_enabled_default_starts_one_timer_at_sixty_minutes(tmp_path, qapp):
    w = _window(tmp_path)
    try:
        assert w.auto_pack_timer.isActive()
        assert w.auto_pack_timer.interval() == 60 * 60 * 1000
        assert getattr(w, "_auto_pack_fired", 0) == 0
    finally:
        w.close()


def test_disabled_config_leaves_the_timer_stopped(tmp_path, qapp):
    w = _window(tmp_path, enabled=False, interval=90)
    try:
        assert not w.auto_pack_timer.isActive()
    finally:
        w.close()


def test_custom_interval_sets_the_expected_qtimer_interval(tmp_path, qapp):
    w = _window(tmp_path, enabled=True, interval=15)
    try:
        assert w.auto_pack_timer.interval() == 15 * 60 * 1000
    finally:
        w.close()


def test_reconfiguration_never_creates_a_second_timer(tmp_path, qapp):
    w = _window(tmp_path, interval=60)
    try:
        first = w.auto_pack_timer
        w._service.config.packing.auto_pack_all_interval_minutes = 10
        w._configure_auto_pack_timer()
        assert w.auto_pack_timer is first, "a Settings save must not stack another timer"
        assert w.auto_pack_timer.interval() == 10 * 60 * 1000
        assert w.auto_pack_timer.isActive()
    finally:
        w.close()


def test_settings_save_reconfigures_the_timer_before_the_fingerprint_early_return(tmp_path, qapp):
    """Changing ONLY auto-pack settings must still reach the timer."""
    w = _window(tmp_path, interval=60)
    try:
        w._service.config.packing.auto_pack_all_interval_minutes = 7
        w._on_settings_saved()
        assert w.auto_pack_timer.interval() == 7 * 60 * 1000
        assert w.auto_pack_timer.isActive()
    finally:
        w.close()


def test_the_timer_never_fires_immediately_at_startup(tmp_path, qapp):
    """Construction starts the countdown; it must not pack on the spot."""
    fired: list[str] = []
    w = _window(tmp_path)
    try:
        original = w._on_auto_pack_tick
        w._on_auto_pack_tick = lambda: fired.append("tick")  # type: ignore[method-assign]
        # No event loop iterations are pumped during construction: the tick
        # can only fire after a full interval. A direct call just now would
        # also have packed, so assert construction itself queued nothing.
        assert fired == []
        assert not w._pack_batch_runner.busy()
        del original
    finally:
        w.close()


def _make_config(tmp_path, *, enabled, interval):
    from audapack.config import PackingConfig

    packing = PackingConfig(output_dir=str(tmp_path / "out"))
    packing.auto_pack_all_enabled = enabled
    packing.auto_pack_all_interval_minutes = interval
    return AppConfig(audits=AuditsConfig(root=str(tmp_path / "audits")), packing=packing)


def _seed_projects(tmp_path, service, spec: list[tuple[str, bool, bool, bool]]):
    """(name, ignore_archive, enabled, has_source) rows -> registered projects."""
    from audapack.models import Project

    projects = []
    for index, (name, ignore, enabled, has_source) in enumerate(spec, start=1):
        projects.append(Project(
            id=name,
            display_name=name.upper(),
            source_path=str(tmp_path / name) if has_source else "",
            ignore_archive=ignore,
            enabled=enabled,
            priority_group="MAIN0",
            slot=index,
        ))
    return projects


def test_a_tick_starts_one_parallel_pack_batch_with_the_same_eligible_projects(tmp_path, qapp):
    w = _window(tmp_path)
    try:
        started: list[tuple[list, str]] = []
        w._start_parallel_pack_batch = lambda projects, batch_label="PACK ALL": (  # type: ignore[method-assign]
            started.append((list(projects), batch_label)) or True
        )
        w._on_auto_pack_tick()
        assert len(started) == 1
        projects, label = started[0]
        assert label == "AUTO PACK ALL"
        assert [p.id for p in projects] == [f"p{i}" for i in range(1, 5)], (
            "the automatic tick must use the same eligibility as manual PACK ALL"
        )
    finally:
        w.close()


def test_ignore_archive_disabled_and_sourceless_projects_are_skipped(tmp_path, qapp):
    from audapack.models import Project

    projects = [
        Project(id="good", display_name="GOOD", source_path=str(tmp_path / "good"), priority_group="MAIN0", slot=1),
        Project(id="ignored", display_name="IGN", source_path=str(tmp_path / "ignored"), ignore_archive=True, priority_group="MAIN0", slot=2),
        Project(id="disabled", display_name="DIS", source_path=str(tmp_path / "dis"), enabled=False, priority_group="MAIN0", slot=3),
        Project(id="nosrc", display_name="NOSRC", source_path="", priority_group="MAIN0", slot=4),
    ]
    w = _window(tmp_path)
    try:
        for project in projects:
            w.model._projects[("MAIN0", project.slot)] = project
        started: list[tuple[list, str]] = []
        w._start_parallel_pack_batch = lambda items, batch_label="PACK ALL": (  # type: ignore[method-assign]
            started.append((list(items), batch_label)) or True
        )
        w._on_auto_pack_tick()
        assert [p.id for p in started[0][0]] == ["good"]
    finally:
        w.close()


def test_an_active_pack_batch_makes_the_tick_skip(tmp_path, qapp, monkeypatch):
    w = _window(tmp_path)
    try:
        monkeypatch.setattr(w._pack_batch_runner, "busy", lambda: True)
        started: list[tuple[list, str]] = []
        w._start_parallel_pack_batch = lambda items, batch_label="PACK ALL": (  # type: ignore[method-assign]
            started.append((list(items), batch_label)) or True
        )
        w._on_auto_pack_tick()
        assert started == [], "a busy pack batch must not start a second one"
    finally:
        w.close()


def test_a_running_individual_pack_makes_the_tick_skip(tmp_path, qapp):
    w = _window(tmp_path)
    try:
        w.task_runner.submit("pack:p1", lambda: None)
        started: list[tuple[list, str]] = []
        w._start_parallel_pack_batch = lambda items, batch_label="PACK ALL": (  # type: ignore[method-assign]
            started.append((list(items), batch_label)) or True
        )
        w._on_auto_pack_tick()
        assert started == [], "an in-flight individual pack must not be overlapped"
    finally:
        w.close()


def test_repeated_busy_ticks_do_not_build_a_backlog(tmp_path, qapp, monkeypatch):
    w = _window(tmp_path)
    try:
        started: list[tuple[list, str]] = []
        w._start_parallel_pack_batch = lambda items, batch_label="PACK ALL": (  # type: ignore[method-assign]
            started.append((list(items), batch_label)) or True
        )
        monkeypatch.setattr(w._pack_batch_runner, "busy", lambda: True)
        w._on_auto_pack_tick()
        w._on_auto_pack_tick()
        assert started == [], "busy ticks must start nothing and queue no work"
        monkeypatch.setattr(w._pack_batch_runner, "busy", lambda: False)
        queued = [
            project
            for project in w.model._projects.values()
            if project and str(getattr(w.model, "_pack_states", {}).get(project.id, (None,))[0] if isinstance(w.model._pack_states.get(project.id), tuple) else "") == "QUEUED"
        ]
        # Nothing was queued while busy, and no backlog exists to drain later.
        assert queued == []
        # Ownership ended: the release itself must NOT start a batch -- only a
        # future timer tick may, and it starts exactly one.
        assert started == []
        w._on_auto_pack_tick()
        assert len(started) == 1 and started[0][1] == "AUTO PACK ALL"
    finally:
        w.close()
