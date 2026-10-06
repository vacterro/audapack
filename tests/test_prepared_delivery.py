import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from audapack.account_registry import AccountIdentity
from audapack.config import AppConfig, LauncherConfig
from audapack.limit_adapters import CodexModelCatalog
from audapack.models import Project
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_delivery import PreflightError, build_launch_plan, execute_claimed


def _config(tmp_path: Path):
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2"),
                        LauncherConfig("claude2", "Claude 2", "A2")]
    return config


def _job(launcher="main_codex2", account="codex:2", effort="high"):
    return PreparedJob(
        "p-1", "continue", "project", launcher, account, Trigger.ON_TIME,
        Payload.USER_COMMAND, {"text": "cc"},
        {"at": datetime(2026, 9, 23, tzinfo=timezone.utc).isoformat()},
        model="example-model", effort=effort, enabled=True, state=JobState.ARMED,
    )


def _account(tmp_path, provider="codex", launcher="main_codex2"):
    return AccountIdentity(f"{provider}:2", provider, "Account 2", str(tmp_path),
                           (launcher,), "test", "2026-09-23T00:00:00+00:00")


def test_codex_preflight_preserves_exact_account_project_and_payload(tmp_path):
    plan = build_launch_plan(_job(), _account(tmp_path), _config(tmp_path), executable="codex.cmd")
    assert plan.env["CODEX_HOME"] == str(tmp_path)
    assert plan.cwd == tmp_path.resolve()
    assert plan.payload == b"cc"
    assert "--approve-for-me" in plan.argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in plan.argv
    assert "model_reasoning_effort=\"high\"" in plan.argv
    assert plan.argv[-1] == "-"


def test_wrong_account_and_unsupported_effort_fail_before_process(tmp_path):
    with pytest.raises(PreflightError, match="mismatch"):
        build_launch_plan(_job(), _account(tmp_path, launcher="main_codex"),
                          _config(tmp_path), executable="codex.cmd")
    with pytest.raises(PreflightError, match="effort"):
        build_launch_plan(_job(effort="extreme"), _account(tmp_path),
                          _config(tmp_path), executable="codex.cmd")


def test_custom_override_rejected_until_native_prompt_contract_exists(tmp_path):
    config = _config(tmp_path)
    config.launchers[0].command_template = "my-wrapper.cmd"
    with pytest.raises(PreflightError, match="custom launcher"):
        build_launch_plan(_job(), _account(tmp_path), config, executable="codex.cmd")


def test_claude_effort_maps_only_supported_values(tmp_path):
    job = _job("claude2", "claude:2", "max")
    plan = build_launch_plan(job, _account(tmp_path, "claude", "claude2"),
                             _config(tmp_path), executable="claude.exe")
    assert plan.argv[-2:] == ("--effort", "max")
    assert plan.env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert plan.argv[plan.argv.index("--permission-mode") + 1] == "auto"


def test_codex_effort_uses_discovered_model_capability(tmp_path, monkeypatch):
    monkeypatch.setattr("audapack.prepared_delivery.shutil.which", lambda _name: "codex.cmd")
    monkeypatch.setattr("audapack.prepared_delivery._verify_local_cli_contract", lambda *_args: None)
    monkeypatch.setattr("audapack.prepared_delivery.CodexLimitAdapter.discover_models",
                        lambda *_args: CodexModelCatalog({"example-model": frozenset({"high"})},
                                                           "example-model"))
    valid = build_launch_plan(_job(), _account(tmp_path), _config(tmp_path))
    assert 'model_reasoning_effort="high"' in valid.argv
    with pytest.raises(PreflightError, match="selected model"):
        build_launch_plan(replace(_job(), effort="max"), _account(tmp_path), _config(tmp_path))


def test_native_stdin_delivery_receipt_and_windows_db_cleanup(tmp_path):
    job = _job()
    store_path = tmp_path / "execution.sqlite3"
    store = PreparedStore(store_path)
    store.save(job)
    execution = store.claim(job.prepared_id, "test-now", "owner")
    plan = build_launch_plan(job, _account(tmp_path), _config(tmp_path), executable="codex.cmd")
    plan = replace(plan, argv=(sys.executable, "-c", "import sys; assert sys.stdin.read() == 'cc'"))
    result = execute_claimed(plan, job, store, execution, "owner", timeout_seconds=5)
    assert result.state == JobState.DONE
    assert store.receipt(job.prepared_id, "test-now")["delivery_hash"] == plan.payload_sha256
    assert store.get(job.prepared_id).enabled is False
    store_path.unlink()  # SQLite handles must be closed on Windows too.
