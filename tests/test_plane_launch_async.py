"""T-284: the plane launch must never run on the Qt GUI thread.

``MainWindow._launch_plane_account`` used to call ``sai_accounts.launch()``
inline. That call is a bounded subprocess (``LAUNCH_TIMEOUT_S = 15.0``), so a
stalled, offline or hung SAI Accounts control plane froze the whole AUDAPACK
window for up to fifteen seconds -- on the Project Room click path.

These tests pin the repair without weakening one line of the T-279/T-285
contract: the click path does bounded preparation and submits the blocking call
to the shared :class:`TaskRunner`, the verdict is applied back on the GUI
thread, and a rejected/failed/timed-out verdict is still never registered and
still never falls back to a generic Antigravity process.

Every test here fails against the synchronous implementation: A/B/F by
observing that the caller returned while the plane call was still blocked, G by
observing registration after the window closed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from audapack import sai_accounts as sai
from audapack.config import AppConfig, LauncherConfig
from audapack.launch_federation import account_id_of, target_id
from audapack.models import Project
from audapack.ui_qt.task_runner import TaskRunner
from tests.test_launch_federation import Plane

AG = "antigravity:windows-user:1f17624fa8051f44"
AG2 = "antigravity:managed-windows-user:7381b83affffffffff"
SCHEMA = "sai.accounts/launch/1"


@pytest.fixture(autouse=True)
def _isolate_plane():
    saved_cache = list(sai._LIST_CACHE)
    saved_engine, saved_run = sai.TestEngine, sai.TestRun
    sai.TestEngine, sai.TestRun = lambda: "", None
    sai.remember([])
    try:
        yield
    finally:
        sai._LIST_CACHE[:] = saved_cache
        sai.TestEngine, sai.TestRun = saved_engine, saved_run


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A plane that IS installed: a real file the probe can see."""
    exe = tmp_path / "sai-accounts.exe"
    exe.write_bytes(b"")
    fake = Plane()
    fake.engine = str(exe)
    monkeypatch.setattr(sai, "TestEngine", lambda: fake.engine)
    monkeypatch.setattr(sai, "TestRun", fake.run)
    return fake


@pytest.fixture
def launch_env(qapp, tmp_path):
    """A real MainWindow carrying a real TaskRunner, plus one canonical click."""
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    project_dir = tmp_path / "Proj"
    project_dir.mkdir()
    config = AppConfig(projects=[
        Project(id="p1", display_name="Proj", source_path=str(project_dir),
                priority_group="MAIN0", slot=1),
    ])
    config.launchers = [LauncherConfig(target_id(AG), "Antigravity 1", "AG1",
                                       command_template="",
                                       resolved_stage="plane_launch")]
    window = MainWindow(ProjectService(config, base_dir=tmp_path))
    cfg = config.launchers[0]
    target = config.projects[0]
    launch_target = SimpleNamespace(cwd=Path(project_dir))
    resolution = SimpleNamespace(account_id=AG, stage="plane_launch")
    try:
        yield SimpleNamespace(
            window=window, cfg=cfg, target=target,
            launch_target=launch_target, resolution=resolution,
            project_dir=project_dir, key=f"plane-launch:{target.id}:{cfg.id}",
        )
    finally:
        window.task_runner.close(timeout=0.2)
        window.close()


def spin(app, predicate, timeout=10.0):
    """Pump the GUI event loop until ``predicate`` holds (never a bare sleep)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


def drain(app, seconds=0.3):
    """Pump the GUI event loop for a bounded window (to catch a spurious second
    application of a verdict, not as a synchronization primitive)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def ok_verdict(account_id=AG, pid=4242):
    return sai.LaunchResult(ok=True, account_id=account_id, pid=pid,
                            provider_id="antigravity", execution="current_user",
                            workdir=r"C:\work\one")


class _Spy:
    """A recording stand-in for ``_register_agent_launch`` (never a real launch)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.threads: list[int] = []

    def __call__(self, process, launcher_id, project, **kwargs):
        self.calls.append((process, launcher_id, project))
        self.threads.append(threading.current_thread().ident)

    @property
    def count(self) -> int:
        return len(self.calls)


class _Gate:
    """A deterministic blocking fake plane call: no sleeps as synchronization."""

    def __init__(self, verdict):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple] = []
        self.threads: list[int] = []
        self._verdict = verdict

    def __call__(self, account_id, workdir, timeout=None):
        self.calls.append((account_id, str(workdir)))
        self.threads.append(threading.current_thread().ident)
        self.entered.set()
        # Blocks until the test releases it. A synchronous caller therefore
        # cannot return: the GUI-thread assertion below is deterministic, not a
        # wall-clock guess.
        self.release.wait(10.0)
        return self._verdict


# ── A. the GUI/caller path does not wait for the plane ─────────────────────


def test_a_a_blocking_plane_call_does_not_block_the_caller(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    monkeypatch.setattr(env.window, "_register_agent_launch", _Spy())
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    started = time.monotonic()
    # Would block for ten seconds inline; must return immediately.
    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"click path waited {elapsed:.2f}s on the plane"
    assert env.window.task_runner.is_running(env.key), "no in-flight plane request"
    assert gate.entered.wait(5.0), "the plane call never reached a worker"
    assert not gate.release.is_set()
    assert env.window._register_agent_launch.count == 0, "applied before the verdict"

    gate.release.set()
    assert spin(qapp, lambda: env.window._register_agent_launch.count == 1)
    assert env.window._register_agent_launch.count == 1


def test_a_the_plane_call_runs_off_the_gui_thread(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    monkeypatch.setattr(env.window, "_register_agent_launch", _Spy())
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert gate.entered.wait(5.0)
    gate.release.set()
    assert spin(qapp, lambda: env.window._register_agent_launch.count == 1)
    assert gate.threads and gate.threads[0] != threading.main_thread().ident


# ── B. the verdict is applied ON the GUI thread ───────────────────────────


def test_b_the_verdict_is_applied_on_the_gui_thread(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert gate.entered.wait(5.0)
    gate.release.set()
    assert spin(qapp, lambda: spy.count == 1)
    assert spy.threads == [threading.main_thread().ident]
    assert gate.threads[0] != threading.main_thread().ident


# ── C. success: exactly once, exact canonical id, authoritative pid ───────


def test_c_a_success_registers_exactly_once_under_the_canonical_id(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    assert account_id_of(env.cfg.id) == AG
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    installed.launch_pid = 4242

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert spin(qapp, lambda: spy.count == 1)
    # Generously long: a second, spurious application would land here.
    drain(qapp, 0.3)

    assert spy.calls == [(4242, env.cfg.id, env.target)]
    assert spy.count == 1
    # The plane was invoked with the exact canonical id and the project root.
    assert installed.calls[-1] == ["launch", AG, "--workdir", str(env.project_dir)]


def test_c_registration_uses_the_planes_pid_not_the_brokers(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    installed.launch_pid = 987654

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert spin(qapp, lambda: spy.count == 1)
    assert spy.calls[0][0] == 987654


# ── D. a rejected verdict is never registered and never falls back ────────


@pytest.mark.parametrize("payload,code", [
    ({"schema": "wrong/schema", "status": "launched", "account_id": AG, "pid": 1},
     "LAUNCH_SCHEMA_MISMATCH"),
    ({"schema": SCHEMA, "status": "launched", "pid": 1}, "LAUNCH_ACCOUNT_MISSING"),
    ({"schema": SCHEMA, "status": "launched", "account_id": AG2, "pid": 1},
     "ACCOUNT_MISMATCH"),
    ({"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": 0},
     "LAUNCH_PID_MISSING"),
    ({"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": 1,
      "provider_id": "claude"}, "LAUNCH_PROVIDER_MISMATCH"),
    ({"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": 1,
      "workdir": r"D:\elsewhere"}, "LAUNCH_WORKDIR_MISMATCH"),
    ({"schema": SCHEMA, "status": "rejected", "reason": "account offline"},
     "rejected"),
], ids=["schema", "account_missing", "account_mismatch", "pid",
        "provider", "workdir", "rejected"])
def test_d_a_rejected_verdict_never_registers(
        installed, monkeypatch, launch_env, qapp, payload, code):
    import json

    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    installed.launch_stdout = json.dumps(payload)
    messages: list[str] = []
    monkeypatch.setattr(env.window, "statusBar",
                        lambda: SimpleNamespace(showMessage=messages.append))

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert spin(qapp, lambda: any(code in m for m in messages))
    drain(qapp, 0.2)

    assert spy.count == 0, "a rejected plane verdict was registered"
    assert any(code in m and env.target.display_name in m for m in messages)
    # No generic Antigravity fallback: the plane is the ONLY invocation.
    launches = [argv for argv in installed.calls if argv[0] == "launch"]
    assert launches == [["launch", AG, "--workdir", str(env.project_dir)]]


# ── E. a worker exception is contained, registers nothing, reports ───────


def test_e_a_worker_exception_is_contained_and_never_registers(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)

    def _boom(account_id, workdir, timeout=None):
        raise RuntimeError("control plane exploded")

    monkeypatch.setattr(sai, "launch", _boom)
    messages: list[str] = []
    monkeypatch.setattr(env.window, "statusBar",
                        lambda: SimpleNamespace(showMessage=messages.append))

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    # Wait for the failure diagnostic specifically: the in-flight status also
    # names the launcher, so waiting on that would race the error itself.
    assert spin(qapp, lambda: any("error=" in m for m in messages))
    drain(qapp, 0.2)

    assert spy.count == 0
    plane = [m for m in messages if env.cfg.name in m]
    assert plane, "a worker exception produced no operator diagnostic"
    assert env.target.display_name in plane[-1]
    assert "error=" in plane[-1]
    assert env.window.task_runner.is_running(env.key) is False


# ── F. in-flight double click: one plane call, one registration ──────────


def test_f_a_second_click_while_in_flight_does_not_launch_twice(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)
    messages: list[str] = []
    monkeypatch.setattr(env.window, "statusBar",
                        lambda: SimpleNamespace(showMessage=messages.append))

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert gate.entered.wait(5.0)
    assert env.window.task_runner.is_running(env.key)

    # The same canonical account/project clicked again, still unresolved.
    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)

    assert len(gate.calls) == 1, "a second uncontrolled plane call was started"
    gate.release.set()
    assert spin(qapp, lambda: spy.count == 1)
    drain(qapp, 0.3)
    assert len(gate.calls) == 1
    assert spy.count == 1
    assert any("already launching" in m for m in messages)


def test_f_a_click_for_another_project_is_not_coalesced_away(
        installed, monkeypatch, launch_env, qapp, tmp_path):
    """Single-flight is per canonical account/project, not a global lock."""
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    other_dir = tmp_path / "Other"
    other_dir.mkdir()
    other = env.window._service.add_project(
        "Other", source_path=str(other_dir), priority_group="MAIN0", slot=2)
    assert env.window._service.get_project(other.id) is not None

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert gate.entered.wait(5.0)
    env.window._launch_plane_account(
        env.cfg, other, SimpleNamespace(cwd=other_dir), env.resolution)

    assert spin(qapp, lambda: len(gate.calls) >= 2), "a second project was blocked"
    gate.release.set()
    assert spin(qapp, lambda: spy.count == 2)
    assert {call[2].id for call in spy.calls} == {env.target.id, other.id}


# ── G. window close while the worker is running ──────────────────────────


def test_g_closing_the_window_while_the_plane_call_runs_is_safe(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    real_close = TaskRunner.close
    monkeypatch.setattr(TaskRunner, "close",
                        lambda self, timeout=0.2: real_close(self, timeout))

    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert gate.entered.wait(5.0)
    assert env.window.task_runner.is_running(env.key)

    env.window.close()  # real closeEvent -> TaskRunner.close()

    gate.release.set()
    drain(qapp, 0.5)

    assert spy.count == 0, "a closed window still registered the plane pid"
    assert env.window.task_runner.state in ("CLOSING", "CLOSED")


def test_g_a_result_arriving_after_close_never_touches_the_window(
        installed, monkeypatch, launch_env, qapp):
    """The withheld callback is the whole guarantee -- no destroyed-widget call."""
    env = launch_env
    seen: list[int] = []
    monkeypatch.setattr(env.window, "_register_agent_launch",
                        lambda *a, **k: seen.append(1))
    real_close = TaskRunner.close
    monkeypatch.setattr(TaskRunner, "close",
                        lambda self, timeout=0.2: real_close(self, timeout))
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert gate.entered.wait(5.0)
    env.window.close()
    assert gate.release.wait(0.1) is False
    gate.release.set()
    drain(qapp, 0.5)
    assert seen == []


# ── H. a refused submission is never a silent success ────────────────────


def test_h_a_submission_refused_by_a_closing_runner_never_registers(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    env.window.task_runner.close(timeout=0.01)
    assert env.window.task_runner.is_running(env.key) is False
    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    drain(qapp, 0.2)

    assert gate.entered.is_set() is False, "work submitted to a closing runner"
    assert spy.count == 0


# ── I. WORKDIR_UNAVAILABLE: ACCESS_DENIED auto-heals via grant_workdir ───


def test_i_workdir_access_denied_auto_heals_via_grant_workdir(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)

    denied = sai.LaunchResult(
        ok=False, account_id=AG, pid=0, provider_id="antigravity", execution="",
        workdir=str(env.project_dir), error_code="WORKDIR_UNAVAILABLE",
        detail="ACCESS_DENIED",
    )
    calls = []

    def _mock_launch(account_id, workdir, timeout=None):
        calls.append(("launch", account_id, workdir))
        if len(calls) == 1:
            return denied
        return ok_verdict(account_id=account_id)

    grant_calls = []

    def _mock_grant(account_id, workdir, timeout=None):
        grant_calls.append((account_id, workdir))
        return sai.GrantWorkdirResult(ok=True, account_id=account_id, workdir=str(workdir), status="granted")

    monkeypatch.setattr(sai, "launch", _mock_launch)
    monkeypatch.setattr(sai, "grant_workdir", _mock_grant)

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert spin(qapp, lambda: spy.count == 1)
    drain(qapp, 0.2)

    assert len(grant_calls) == 1
    assert len(calls) == 2
    assert spy.count == 1


def test_j_workdir_access_denied_permanent_failure_reports_filesystem_next_step(
        installed, monkeypatch, launch_env, qapp):
    env = launch_env
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)

    denied = sai.LaunchResult(
        ok=False, account_id=AG, pid=0, provider_id="antigravity", execution="",
        workdir=str(env.project_dir), error_code="WORKDIR_UNAVAILABLE",
        detail="ACCESS_DENIED",
    )

    def _mock_launch(account_id, workdir, timeout=None):
        return denied

    def _mock_grant(account_id, workdir, timeout=None):
        return sai.GrantWorkdirResult(ok=False, account_id=account_id, workdir=str(workdir), status="grant_failed")

    monkeypatch.setattr(sai, "launch", _mock_launch)
    monkeypatch.setattr(sai, "grant_workdir", _mock_grant)

    messages = []
    monkeypatch.setattr(env.window, "statusBar",
                        lambda: SimpleNamespace(showMessage=messages.append))

    env.window._launch_plane_account(
        env.cfg, env.target, env.launch_target, env.resolution)
    assert spin(qapp, lambda: any("WORKDIR_UNAVAILABLE" in m for m in messages))
    drain(qapp, 0.2)

    assert spy.count == 0
    fail_msg = [m for m in messages if "WORKDIR_UNAVAILABLE" in m][-1]
    assert "stage=plane_launch" in fail_msg or "stage=" in fail_msg
    assert "ACCESS_DENIED" in fail_msg
    assert "check folder permissions for Proj in Windows Explorer" in fail_msg
    assert "verify the account context in SAI Accounts" not in fail_msg



# ── Acceptance: the real Project Room click path with a stuck plane ──────


def test_acceptance_click_path_stays_live_while_the_plane_is_stuck(
        installed, monkeypatch, launch_env, qapp):
    """Live acceptance at app level: the actual button dispatch, a real
    :class:`TaskRunner`, a real event loop, and a plane that never answers
    until released.

    While the plane is stuck the click has returned, a timer armed before the
    click still fires (the event loop is alive), and nothing has been
    registered -- then on release the launch registers exactly once under the
    exact canonical launcher id with the plane's own PID.
    """
    from PySide6.QtCore import QTimer

    import audapack.ui_qt.main_window as mw

    env = launch_env
    monkeypatch.setattr(mw, "scoped_config_write", lambda _fn: True)
    spy = _Spy()
    monkeypatch.setattr(env.window, "_register_agent_launch", spy)
    gate = _Gate(ok_verdict())
    monkeypatch.setattr(sai, "launch", gate)

    tick: list = []
    QTimer.singleShot(0, lambda: tick.append(1))

    started = time.monotonic()
    env.window._continue_launcher_dispatch(env.target, env.cfg.id, False, None)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"click path waited {elapsed:.2f}s on the plane"
    assert gate.entered.wait(5.0), "the plane call never started"
    assert gate.release.is_set() is False, "the plane was supposed to be stuck"

    # The GUI thread kept running while the plane was stuck: the timer armed
    # before the click gets serviced, and no verdict has been applied yet.
    assert spin(qapp, lambda: bool(tick)), "the event loop stalled while the plane blocked"
    assert spy.count == 0, "a verdict was applied before the plane answered"

    gate.release.set()
    assert spin(qapp, lambda: spy.count == 1)
    assert spy.count == 1, "one user action must register exactly one process"
    assert len(gate.calls) == 1, "one user action must reach the plane once"

    pid, launcher_id, project = spy.calls[0]
    assert pid == 4242, "the plane's own PID must be the registered one"
    assert launcher_id == target_id(AG), f"registered under {launcher_id}, not the canonical id"
    assert project.id == env.target.id
