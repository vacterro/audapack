from datetime import datetime, timezone

import pytest

from audapack.account_registry import AccountIdentity
from audapack.config import AppConfig, LauncherConfig
from audapack.launch_authority import LaunchPolicyError, resolve_launch_target
from audapack.models import Project


def _setup(tmp_path):
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test",
                              datetime.now(timezone.utc).isoformat())
    return config, account


def test_manual_and_prepared_resolve_same_project_and_launcher(tmp_path):
    config, account = _setup(tmp_path)
    manual = resolve_launch_target(config, "project", "main_codex2")
    prepared = resolve_launch_target(config, "project", "main_codex2",
                                     account=account, prepared=True)
    assert manual.cwd == prepared.cwd == tmp_path.resolve()
    assert manual.project is prepared.project
    assert manual.launcher is prepared.launcher
    assert prepared.account is account


def test_prepared_keeps_operator_template_authoritative(tmp_path):
    config, account = _setup(tmp_path)
    config.launchers[0].command_template = "my-wrapper.cmd"
    assert resolve_launch_target(config, "project", "main_codex2").launcher.command_template
    with pytest.raises(LaunchPolicyError, match="prompt contract"):
        resolve_launch_target(config, "project", "main_codex2",
                              account=account, prepared=True)


def test_prepared_capacity_and_account_binding_fail_before_process(tmp_path):
    config, account = _setup(tmp_path)
    class FullMonitor:
        def block_reason(self, _launcher):
            return "capacity reached"
    with pytest.raises(LaunchPolicyError, match="capacity reached"):
        resolve_launch_target(config, "project", "main_codex2",
                              account=account, prepared=True, monitor=FullMonitor())
    with pytest.raises(LaunchPolicyError, match="identity mismatch"):
        resolve_launch_target(config, "project", "main_codex2",
                              account=AccountIdentity(
                                  account.account_id, "codex", "other", str(tmp_path),
                                  ("main_codex",), "test", account.last_seen_at),
                              prepared=True)
