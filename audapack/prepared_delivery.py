"""Prepared CLI preflight and native initial-prompt delivery."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from audapack.account_registry import AccountIdentity
from audapack.config import AppConfig
from audapack.handoff_drop import resolve_drop_dir
from audapack.instances import InstanceMonitor
from audapack.launch_authority import (
    LaunchPolicyError,
    LaunchTarget,
    create_agent_process,
    register_agent_process,
    resolve_launch_target,
)
from audapack.limit_adapters import CodexLimitAdapter, CodexModelCatalog
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore
from audapack.prepared_payloads import read_pinned, resolve_latest_handoff
from audapack.provider_capabilities import PROVIDERS


class PreflightError(ValueError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, str, str], tuple[float, CodexModelCatalog]] = {}


@lru_cache(maxsize=32)
def _cached_cli_help(binary: str, provider_id: str, version: str) -> str:
    command = [binary, "exec", "--help"] if provider_id == "codex" else [binary, "--help"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    if result.returncode:
            raise PreflightError(f"{provider_id} CLI help failed", retryable=True)
    return result.stdout


def _verify_local_cli_contract(binary: str, provider_id: str) -> str:
    """Check installed native prompt flags; cache help by executable/version."""
    try:
        version_result = subprocess.run([binary, "--version"], capture_output=True,
                                        text=True, timeout=8, check=False)
        if version_result.returncode:
            raise PreflightError(f"{provider_id} CLI version failed", retryable=True)
        version = version_result.stdout.strip()
        help_text = _cached_cli_help(binary, provider_id, version)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"{provider_id} CLI capability check failed", retryable=True) from exc
    required = ({"--json", "--approve-for-me", "--model", "--config", "--cd"}
                if provider_id == "codex" else
                {"--print", "--input-format", "--output-format", "--permission-mode",
                 "--model", "--effort"})
    missing = sorted(flag for flag in required if flag not in help_text)
    if missing:
        raise PreflightError(f"{provider_id} CLI lacks required prepared flags: {', '.join(missing)}")
    return version


def _codex_model_catalog(account: AccountIdentity, binary: str,
                         version: str) -> CodexModelCatalog:
    key = (account.account_id, binary, version)
    now = time.monotonic()
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached and now - cached[0] < 3600:
            return cached[1]
    catalog = CodexLimitAdapter(binary, timeout_seconds=8).discover_models(account)
    with _MODEL_CACHE_LOCK:
        if len(_MODEL_CACHE) >= 32:
            _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
        _MODEL_CACHE[key] = (now, catalog)
    return catalog


@dataclass(frozen=True)
class LaunchPlan:
    target: LaunchTarget
    provider_id: str
    account_id: str
    launcher_id: str
    project_id: str
    cwd: Path
    argv: tuple[str, ...]
    env: dict[str, str]
    payload: bytes
    payload_sha256: str
    payload_preview: str
    delivery_mode: str


def _payload(job: PreparedJob, config: AppConfig) -> tuple[bytes, str]:
    if job.payload in (Payload.USER_COMMAND, Payload.STATIC_PROMPT):
        text = job.payload_config.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PreflightError("empty prepared text")
        return text.encode("utf-8"), f"{job.payload.value}: {text[:80]}"
    if job.payload == Payload.PINNED_SAIHANDOFF:
        path = Path(str(job.payload_config.get("path") or ""))
        digest = str(job.payload_config.get("sha256") or "")
        if not digest:
            raise PreflightError("pinned handoff hash missing")
        body = read_pinned(path, digest)
        return body, f"Pinned SAIHANDOFF: {path.name} sha256={digest}"
    if job.payload == Payload.LATEST_SAIHANDOFF:
        latest = resolve_latest_handoff(
            job.project_id, config.projects, resolve_drop_dir(config.bridge.handoff_dir),
        )
        if latest is None:
            raise PreflightError("no unambiguous handoff for project")
        body = latest.path.read_bytes()
        if hashlib.sha256(body).hexdigest() != latest.sha256:
            raise PreflightError("handoff changed during resolution")
        return body, f"Latest SAIHANDOFF: {latest.path.name} sha256={latest.sha256}"
    if job.payload == Payload.AUDIT:
        raise PreflightError("audit delivery requires AuditRunCoordinator")
    raise PreflightError("unsupported payload")


def build_launch_plan(job: PreparedJob, account: AccountIdentity, config: AppConfig,
                      *, executable: str | None = None,
                      monitor: InstanceMonitor | None = None) -> LaunchPlan:
    if account.account_id != job.account_id:
        raise PreflightError("launcher/account identity mismatch")
    try:
        target = resolve_launch_target(config, job.project_id, job.launcher_id,
                                       account=account, prepared=True, monitor=monitor)
    except LaunchPolicyError as exc:
        raise PreflightError(str(exc)) from exc
    cwd = target.cwd
    capabilities = PROVIDERS.get(account.provider_id)
    if capabilities is None or not capabilities.supports_prepared_prompt:
        raise PreflightError("provider has no verified native prepared-prompt delivery")
    if job.model and not _MODEL_ID.fullmatch(job.model):
        raise PreflightError("invalid model id")
    if job.effort and job.effort.lower() not in capabilities.supported_efforts:
        raise PreflightError(f"unsupported {account.provider_id.capitalize()} effort")
    body, preview = _payload(job, config)
    if len(body) > 2_000_000:
        raise PreflightError("payload exceeds 2 MB")
    env = dict(os.environ)
    if account.provider_id == "codex":
        binary = executable or shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
        if not binary:
            raise PreflightError("Codex CLI unavailable", retryable=True)
        if executable is None:
            version = _verify_local_cli_contract(binary, "codex")
            if job.effort:
                try:
                    catalog = _codex_model_catalog(account, binary, version)
                except Exception as exc:
                    raise PreflightError("Codex model capabilities unavailable", retryable=True) from exc
                selected = job.model or catalog.default_model
                supported = catalog.efforts_by_model.get(selected)
                if supported is None:
                    raise PreflightError("Codex model effort support is unknown")
                if job.effort.lower() not in supported:
                    raise PreflightError("unsupported Codex effort for selected model")
        env["CODEX_HOME"] = account.profile_locator
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(key, None)
        args = [binary, "exec", "--json", "--skip-git-repo-check",
                "--approve-for-me", "-C", str(cwd)]
        if job.model:
            args += ["-m", job.model]
        if job.effort:
            args += ["-c", f'model_reasoning_effort="{job.effort.lower()}"']
        args.append("-")  # installed CLI: '-' reads initial instructions from stdin
    else:
        binary = executable or shutil.which("claude.exe") or shutil.which("claude")
        if not binary:
            raise PreflightError("Claude CLI unavailable", retryable=True)
        if executable is None:
            _verify_local_cli_contract(binary, "claude")
        env["CLAUDE_CONFIG_DIR"] = account.profile_locator
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        args = [binary, "-p", "--input-format", "text", "--output-format", "json",
                "--permission-mode", "auto"]
        if job.model:
            args += ["--model", job.model]
        if job.effort:
            args += ["--effort", job.effort.lower()]
        # Claude print mode accepts piped text as its initial user prompt.
    return LaunchPlan(
        target=target,
        provider_id=account.provider_id, account_id=account.account_id,
        launcher_id=job.launcher_id, project_id=target.project.id, cwd=cwd,
        argv=tuple(args), env=env, payload=body,
        payload_sha256=hashlib.sha256(body).hexdigest(), payload_preview=preview,
        delivery_mode="stdin_native",
    )


@dataclass(frozen=True)
class DeliveryResult:
    execution_id: str
    process_id: int
    exit_code: int
    state: JobState


def execute_claimed(plan: LaunchPlan, job: PreparedJob, store: PreparedStore,
                    execution_id: str, owner_id: str, generation: int = 1,
                    *, timeout_seconds: float | None = None,
                    monitor: InstanceMonitor | None = None) -> DeliveryResult:
    """Deliver once through native CLI stdin; record PID before waiting.

    An ambiguous crash after LAUNCHING is never auto-replayed. The unique
    execution receipt remains for explicit recovery, avoiding duplicate work.
    """
    stop_heartbeat = threading.Event()
    def heartbeat() -> None:
        while not stop_heartbeat.wait(30):
            if not store.heartbeat(execution_id, owner_id, generation):
                return
    thread = threading.Thread(target=heartbeat, name="prepared-lease", daemon=True)
    thread.start()
    try:
        return _execute_claimed(plan, job, store, execution_id, owner_id, generation,
                                timeout_seconds=timeout_seconds, monitor=monitor)
    finally:
        stop_heartbeat.set()
        thread.join(timeout=1)


def _execute_claimed(plan: LaunchPlan, job: PreparedJob, store: PreparedStore,
                     execution_id: str, owner_id: str, generation: int,
                     *, timeout_seconds: float | None,
                     monitor: InstanceMonitor | None) -> DeliveryResult:
    if not store.advance(execution_id, owner_id, generation, JobState.CLAIMED, JobState.PREPARING):
        raise RuntimeError("execution claim is not owned")
    if not store.advance(execution_id, owner_id, generation, JobState.PREPARING, JobState.LAUNCHING):
        raise RuntimeError("execution was superseded")
    process = create_agent_process(
        plan.argv, plan.target, env=plan.env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if monitor is not None and not register_agent_process(
        monitor, process, plan.target, correlation_token=execution_id,
    ):
        process.terminate()
        raise RuntimeError("process registration failed before delivery")
    try:
        token = monitor.backend.process_token(process.pid) if monitor is not None else None
    except Exception:
        token = None
    if not store.advance(execution_id, owner_id, generation, JobState.LAUNCHING, JobState.DELIVERING,
                         process_id=process.pid, process_token=token,
                         delivery_hash=plan.payload_sha256):
        process.terminate()
        if monitor is not None:
            monitor.untrack_launch(process.pid)
        raise RuntimeError("execution ownership lost before delivery")
    try:
        stdout, stderr = process.communicate(input=plan.payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # A running agent may be doing useful work. Leave it alive; the PID and
        # delivery hash remain in the durable receipt for later observation.
        store.advance(execution_id, owner_id, generation, JobState.DELIVERING, JobState.RUNNING,
                      result="process still running")
        return DeliveryResult(execution_id, process.pid, -1, JobState.RUNNING)
    if not store.advance(execution_id, owner_id, generation, JobState.DELIVERING, JobState.VERIFYING):
        raise RuntimeError("execution ownership lost during verification")
    if process.returncode == 0:
        # The CLI's exit status proves it consumed and finished the native
        # request. Output bodies are intentionally not persisted in logs.
        store.advance(execution_id, owner_id, generation, JobState.VERIFYING, JobState.DONE,
                      result=f"CLI completed; stdout_bytes={len(stdout)}")
        return DeliveryResult(execution_id, process.pid, 0, JobState.DONE)
    store.advance(execution_id, owner_id, generation, JobState.VERIFYING, JobState.FAILED_TERMINAL,
                  result=f"CLI exited {process.returncode}; stderr_bytes={len(stderr)}")
    return DeliveryResult(execution_id, process.pid, process.returncode, JobState.FAILED_TERMINAL)
