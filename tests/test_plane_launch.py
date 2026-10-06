"""windows_user accounts launch through the control plane's own contract.

The defect class: an Antigravity account the registry reports as
``execution_backend = windows_user`` / ``managed_windows_user`` belongs to a
Windows logon session AUDAPACK cannot select. Two wrong repairs exist and both
are refused here -- switching whichever profile or session happens to be current
(a wrong-account launch, which the source calls worse than no launch), and
leaving the account permanently UNAVAILABLE on a machine whose control plane
exposes a documented launch contract that resolves the context authoritatively.

So the row is driven through ``sai-accounts launch <canonical account_id>`` or it
stays UNAVAILABLE and names the contract it would need. Nothing here reads a
credential, a token or another account's profile: the plane owns the session.
"""

from __future__ import annotations

import inspect

import pytest

from audapack import sai_accounts as sai
from audapack.config import AppConfig, LauncherConfig
from audapack.launch_authority import resolve_launch_target
from audapack.launch_federation import (
    account_id_of,
    sync_effective_launchers,
    target_id,
)
from tests.test_launch_federation import Plane, account, by_target, home_with

AG = "antigravity:windows-user:1f17624fa8051f44"
AG2 = "antigravity:managed-windows-user:7381b83affffffffff"


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


def _resolution(backend: str, locator: str = "vac34", account_id: str = AG):
    from audapack.cli_launchers import account_launch_resolution

    return account_launch_resolution("antigravity", backend, locator,
                                     account_id=account_id)


# ── the contract ────────────────────────────────────────────────────────────


def test_a_windows_user_account_is_launched_by_the_plane(installed):
    """The plane resolves the session; AUDAPACK only names the account."""
    resolution = _resolution("windows_user")
    assert resolution.ok is True
    assert resolution.is_plane_launch is True
    assert resolution.account_id == AG
    assert resolution.probe == installed.engine
    assert resolution.console_command == ""
    assert resolution.stage == "plane_launch"


def test_a_managed_windows_user_account_uses_the_same_contract(installed):
    resolution = _resolution("managed_windows_user", account_id=AG2)
    assert resolution.ok is True
    assert resolution.is_plane_launch is True
    assert resolution.account_id == AG2


def test_the_launch_never_switches_a_session_or_a_profile_directory(installed):
    """No env-profile switch, no profile argument: only the canonical id."""
    resolution = _resolution("windows_user")
    assert resolution.console_command == ""
    verdict = sai.launch(resolution.account_id, r"C:\work\one")
    assert verdict.ok is True
    argv = installed.calls[-1]
    assert argv == ["launch", AG, "--workdir", r"C:\work\one"]
    joined = " ".join(argv)
    assert "$env:" not in joined
    assert "--profile" not in joined
    assert "CODEX_HOME" not in joined and "CLAUDE_CONFIG_DIR" not in joined


def test_the_workdir_is_passed_as_exact_argv_to_the_plane(installed):
    """The plane is told WHICH project as an explicit --workdir argv element."""
    verdict = sai.launch(AG, r"C:\work\one")
    assert verdict.ok is True
    assert installed.calls[-1] == ["launch", AG, "--workdir", r"C:\work\one"]


def test_the_probe_is_the_plane_itself(installed):
    """A persisted resolution is revalidated against the plane, not a session."""
    resolution = _resolution("windows_user")
    assert resolution.probe == installed.engine
    assert resolution.profile == f"SAI Accounts ({AG})"


def test_no_credential_is_read_to_launch_via_plane(installed):
    """The plane launch adapter delegates by account_id and touches no secret material."""
    body = "\n".join(
        line for line in inspect.getsource(sai.launch).splitlines()
        if not line.lstrip().startswith("#"))
    for forbidden in ("read_text", "open(", "environ", "credentials", "oauth", "password"):
        assert forbidden not in body, forbidden


# ── the honest fallbacks ────────────────────────────────────────────────────


def test_without_the_plane_the_row_is_unavailable_with_the_named_contract():
    resolution = _resolution("windows_user")
    assert resolution.ok is False
    assert "launch" in resolution.next_action
    assert "SAI Accounts" in resolution.next_action


def test_a_session_account_without_its_canonical_id_is_unavailable(installed):
    """No id, no launch: a session we cannot name is never guessed."""
    from audapack.cli_launchers import account_launch_resolution

    resolution = account_launch_resolution("antigravity", "windows_user", "vac34")
    assert resolution.ok is False


def test_an_unknown_backend_is_still_unavailable(installed):
    from audapack.cli_launchers import account_launch_resolution

    resolution = account_launch_resolution("gemini", "windows_service", "x",
                                           account_id="gemini:windows-service:x")
    assert resolution.ok is False
    assert "launch" in resolution.next_action


# ── the inventory projection ────────────────────────────────────────────────


def test_the_inventory_materialises_the_antigravity_row_as_launchable(
        installed, tmp_path):
    home = home_with(tmp_path)
    installed.answer_list(
        account(AG, "antigravity", "Antigravity 1", locator="vac34",
                compact="AG1", backend="windows_user"),
        account(AG2, "antigravity", "Antigravity 2", locator="other-user",
                compact="AG2", backend="managed_windows_user"),
    )
    config = AppConfig()
    sync_effective_launchers(config, home=home)

    row = by_target(config, AG)
    assert row is not None
    assert row.command_template == "", "the plane owns the invocation"
    assert row.resolved_stage == "plane_launch"
    assert row.resolved_probe == installed.engine
    assert row.resolved_command == ""
    # One identity: the launcher row IS the canonical account's projection, so
    # whatever instance tracking records against it names that account back.
    assert account_id_of(row.id) == AG


def test_the_click_path_uses_the_plane_resolution(installed, tmp_path):
    """The row's own resolution is authoritative; no profile discovery runs."""
    from audapack.cli_launchers import resolve_cli_launcher

    config = AppConfig()
    config.launchers = [LauncherConfig(target_id(AG), "Antigravity 1", "AG1",
                                       command_template="",
                                       resolved_command="",
                                       resolved_probe=installed.engine,
                                       resolved_stage="plane_launch")]
    row = config.launchers[0]
    resolution = resolve_cli_launcher(row, project_root=r"C:\work\one",
                                      project_name="One", exists=lambda p: True)
    assert resolution.ok is True
    assert resolution.is_plane_launch is True
    assert "plane_launch" in resolution.stage
    assert resolution.account_id == AG
    assert resolution.probe == installed.engine
    assert resolution.console_command == ""


def test_the_launch_target_keeps_the_canonical_identity(installed, tmp_path):
    """Instance attribution flows through the row id, not a display name."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    config = AppConfig()
    config.launchers = [LauncherConfig(target_id(AG), "Antigravity 1", "AG1",
                                       command_template="")]
    from audapack.models import Project

    config.projects = [Project("p1", "P1", str(project_root))]
    target = resolve_launch_target(config, "p1", target_id(AG))
    assert target.launcher.id == target_id(AG)
    assert account_id_of(target.launcher.id) == AG


def test_a_backend_the_plane_does_not_expose_stays_unavailable(installed, tmp_path):
    """No invocation at all: an empty resolution cannot start the wrong thing."""
    unknown = "antigravity:windows-service:deadbeef"
    home = home_with(tmp_path)
    installed.answer_list(
        account(unknown, "antigravity", "Antigravity 9", locator="vac34",
                backend="windows_service"),
    )
    config = AppConfig()
    sync_effective_launchers(config, home=home)

    row = by_target(config, unknown)
    assert row is not None
    assert row.resolved_command == "" and row.resolved_probe == ""
    assert row.resolved_stage == ""
    assert row.command_template == "", "nothing is invented for an unknown backend"


# ── T-279 closure: the launched envelope must prove the requested launch ────

import json  # noqa: E402

SCHEMA = "sai.accounts/launch/1"


def _say(plane, **payload):
    plane.launch_stdout = json.dumps(payload)


REJECTED = [
    ("A_missing_account", {"schema": SCHEMA, "status": "launched", "pid": 123},
     "LAUNCH_ACCOUNT_MISSING"),
    ("A_empty_account", {"schema": SCHEMA, "status": "launched", "account_id": " ", "pid": 123},
     "LAUNCH_ACCOUNT_MISSING"),
    ("A_nonstring_account", {"schema": SCHEMA, "status": "launched", "account_id": 7, "pid": 123},
     "LAUNCH_ACCOUNT_MISSING"),
    ("B_wrong_schema", {"schema": "wrong/schema", "status": "launched", "account_id": AG, "pid": 123},
     "LAUNCH_SCHEMA_MISMATCH"),
    ("B_archive_repro", {"schema": "totally.wrong/99", "status": "launched", "pid": 12345},
     "LAUNCH_SCHEMA_MISMATCH"),
    ("C_missing_schema", {"status": "launched", "account_id": AG, "pid": 123},
     "LAUNCH_SCHEMA_MISMATCH"),
    ("E_other_account", {"schema": SCHEMA, "status": "launched", "account_id": AG2, "pid": 123},
     "ACCOUNT_MISMATCH"),
    ("F_pid_missing", {"schema": SCHEMA, "status": "launched", "account_id": AG},
     "LAUNCH_PID_MISSING"),
    ("F_pid_zero", {"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": 0},
     "LAUNCH_PID_MISSING"),
    ("F_pid_bool", {"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": True},
     "LAUNCH_PID_MISSING"),
    ("F_pid_string", {"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": "123"},
     "LAUNCH_PID_MISSING"),
]


@pytest.mark.parametrize("name,payload,code", REJECTED, ids=[r[0] for r in REJECTED])
def test_an_unproven_launched_envelope_fails_closed(installed, name, payload, code):
    _say(installed, **payload)
    verdict = sai.launch(AG, r"C:\work\one")
    assert verdict.ok is False
    assert verdict.pid == 0
    assert verdict.error_code == code
    assert "S-1-5-" not in verdict.detail


def test_d_the_exact_proven_envelope_succeeds(installed):
    _say(installed, schema=SCHEMA, status="launched", account_id=AG, pid=123,
         provider_id="antigravity", execution="current_user", workdir=r"C:\work\one")
    verdict = sai.launch(AG, r"C:\work\one")
    assert verdict.ok is True
    assert verdict.account_id == AG
    assert verdict.pid == 123


@pytest.mark.parametrize("name,payload,code", REJECTED, ids=[r[0] for r in REJECTED])
def test_g_no_rejected_envelope_is_registered(installed, name, payload, code):
    _assert_not_registered(installed, payload, code)


# ── T-285: an OPTIONAL field that contradicts the request fails closed ──────
#
# ``provider_id`` and ``workdir`` are optional in the installed generation-36
# envelope: absent or empty is accepted. But when either IS present it is a
# claim about THIS launch, and a claim that contradicts the requested launch is
# a malformed success. Neither field is required; neither may lie.

ROOT = r"C:\work\one"

PROVIDER_MISMATCH = [
    ("other_provider", "claude"),
    ("upper_other_provider", "CLAUDE"),
    ("third_provider", "gemini"),
    ("nonstring_provider", 7),
]

WORKDIR_MISMATCH = [
    ("other_root", r"C:\work\two"),
    ("parent_root", r"C:\work"),
    ("other_drive", r"D:\work\one"),
    ("deeper_root", r"C:\work\one\sub"),
]

WORKDIR_SAME = [
    r"C:\work\one",
    r"c:\work\one",
    r"C:/work/one",
    r"C:\work\one\\",
    r"c:/work/one/",
]


def _launched(**payload):
    return {"schema": SCHEMA, "status": "launched", "account_id": AG, "pid": 123, **payload}


def _assert_not_registered(plane, payload, code):
    from types import SimpleNamespace

    from audapack.ui_qt.main_window import MainWindow

    _say(plane, **payload)
    registered: list = []
    messages: list = []
    host = SimpleNamespace(
        _register_agent_launch=lambda *a, **k: registered.append(a),
        statusBar=lambda: SimpleNamespace(showMessage=messages.append),
        _sanitize_diagnostic=lambda text, limit=160: MainWindow._sanitize_diagnostic(text, limit),
    )
    cfg = SimpleNamespace(id=target_id(AG), name="AG1")
    MainWindow._launch_plane_account(
        host, cfg, SimpleNamespace(display_name="P"),
        SimpleNamespace(cwd=ROOT),
        SimpleNamespace(account_id=AG, stage="plane_launch"),
    )
    assert registered == []
    assert messages and code in messages[-1]


def test_a_a_missing_provider_id_is_optional(installed):
    """A) The plane need not state a provider at all."""
    _say(installed, **_launched())
    verdict = sai.launch(AG, ROOT)
    assert verdict.ok is True
    assert verdict.pid == 123
    assert verdict.provider_id == ""


def test_a_an_empty_provider_id_is_optional(installed):
    _say(installed, **_launched(provider_id="  "))
    assert sai.launch(AG, ROOT).ok is True


def test_b_a_matching_provider_id_is_accepted(installed):
    """B) The canonical account's own provider, echoed back."""
    _say(installed, **_launched(provider_id="antigravity"))
    verdict = sai.launch(AG, ROOT)
    assert verdict.ok is True
    assert verdict.provider_id == "antigravity"


def test_b_a_matching_provider_id_is_case_insensitive(installed):
    _say(installed, **_launched(provider_id="Antigravity"))
    assert sai.launch(AG, ROOT).ok is True


@pytest.mark.parametrize("name,provider", PROVIDER_MISMATCH,
                         ids=[c[0] for c in PROVIDER_MISMATCH])
def test_c_a_contradicting_provider_id_fails_closed(installed, name, provider):
    """C) A provider the canonical account_id does not name is never accepted."""
    _say(installed, **_launched(provider_id=provider))
    verdict = sai.launch(AG, ROOT)
    assert verdict.ok is False
    assert verdict.pid == 0
    assert verdict.error_code == "LAUNCH_PROVIDER_MISMATCH"


def test_d_a_missing_workdir_uses_the_requested_root(installed):
    """D) Absent workdir keeps the current behaviour: the request is the root."""
    _say(installed, **_launched())
    verdict = sai.launch(AG, ROOT)
    assert verdict.ok is True
    assert verdict.workdir == ROOT


def test_d_an_empty_workdir_uses_the_requested_root(installed):
    _say(installed, **_launched(workdir="  "))
    verdict = sai.launch(AG, ROOT)
    assert verdict.ok is True
    assert verdict.workdir == ROOT


def test_e_an_equal_returned_workdir_is_accepted(installed):
    """E) The root the plane names is the root that was requested."""
    _say(installed, **_launched(workdir=ROOT))
    assert sai.launch(AG, ROOT).ok is True


@pytest.mark.parametrize("same", WORKDIR_SAME,
                         ids=["plain", "lower", "fwd", "trailing", "lower_fwd"])
def test_f_a_safely_normalised_workdir_is_accepted(installed, same):
    """F) Case and separator differences are representation, not a new root."""
    _say(installed, **_launched(workdir=same))
    assert sai.launch(AG, ROOT).ok is True


@pytest.mark.parametrize("name,other", WORKDIR_MISMATCH,
                         ids=[c[0] for c in WORKDIR_MISMATCH])
def test_g_a_different_returned_root_fails_closed(installed, name, other):
    """G) A genuinely different execution root contradicts Project Room."""
    _say(installed, **_launched(workdir=other))
    verdict = sai.launch(AG, ROOT)
    assert verdict.ok is False
    assert verdict.pid == 0
    assert verdict.error_code == "LAUNCH_WORKDIR_MISMATCH"


@pytest.mark.parametrize("name,provider", PROVIDER_MISMATCH,
                         ids=[c[0] for c in PROVIDER_MISMATCH])
def test_i_a_provider_mismatch_is_never_registered(installed, name, provider):
    """I) A provider contradiction cannot reach ``_register_agent_launch``."""
    _assert_not_registered(installed, _launched(provider_id=provider),
                           "LAUNCH_PROVIDER_MISMATCH")


@pytest.mark.parametrize("name,other", WORKDIR_MISMATCH,
                         ids=[c[0] for c in WORKDIR_MISMATCH])
def test_i_a_workdir_mismatch_is_never_registered(installed, name, other):
    """I) A root contradiction cannot reach ``_register_agent_launch``."""
    _assert_not_registered(installed, _launched(workdir=other),
                           "LAUNCH_WORKDIR_MISMATCH")
