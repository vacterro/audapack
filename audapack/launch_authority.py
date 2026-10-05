"""Shared identity, admission, process creation, and registration for agent launches."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from audapack.account_registry import AccountIdentity
from audapack.config import AppConfig, LauncherConfig
from audapack.instances import InstanceMonitor
from audapack.models import Project


class LaunchPolicyError(ValueError):
    """Launch identity or configured policy does not authorize this process."""


@dataclass(frozen=True)
class LaunchTarget:
    project: Project
    launcher: LauncherConfig
    cwd: Path
    account: AccountIdentity | None = None


def resolve_launch_target(config: AppConfig, project_id: str, launcher_id: str,
                          *, account: AccountIdentity | None = None,
                          prepared: bool = False,
                          monitor: InstanceMonitor | None = None) -> LaunchTarget:
    project = next((item for item in config.projects if item.id == project_id and item.enabled), None)
    if project is None or not project.source_path:
        raise LaunchPolicyError("project missing, disabled, or has no source path")
    cwd = Path(project.source_path).resolve()
    if not cwd.is_dir():
        raise LaunchPolicyError("project root missing")
    launcher = next((item for item in config.launchers if item.id == launcher_id and item.enabled), None)
    if launcher is None:
        raise LaunchPolicyError("launcher missing or disabled")
    if prepared:
        if account is None or launcher_id not in account.launcher_ids:
            raise LaunchPolicyError("launcher/account identity mismatch")
        if launcher.command_template:
            raise LaunchPolicyError("custom launcher template has no verified prompt contract")
    if monitor is not None:
        reason = monitor.block_reason(launcher)
        if reason:
            raise LaunchPolicyError(reason)
    return LaunchTarget(project, launcher, cwd, account)


def create_agent_process(argv: Sequence[str], target: LaunchTarget, *,
                         env: dict[str, str] | None = None,
                         stdin: Any = None, stdout: Any = None, stderr: Any = None,
                         creationflags: int = 0) -> subprocess.Popen:
    """Create one process in the validated project root without a DB transaction."""
    return subprocess.Popen(list(argv), cwd=target.cwd, env=env,
                            stdin=stdin, stdout=stdout, stderr=stderr,
                            creationflags=creationflags)


def register_agent_process(monitor: InstanceMonitor, process: Any,
                           target: LaunchTarget, *, correlation_token: str = "",
                           saipen_binding: dict[str, str] | None = None) -> bool:
    return monitor.track_launch(
        getattr(process, "pid", 0), target.launcher.id, target.project,
        correlation_token=correlation_token, saipen_binding=saipen_binding,
    )
