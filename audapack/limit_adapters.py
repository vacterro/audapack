"""Local provider limit adapters. Probe methods never submit model prompts."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from audapack import sai_accounts
from audapack.account_registry import AccountIdentity
from audapack.limits import LimitSnapshot, LimitWindow


@dataclass(frozen=True)
class CodexModelCatalog:
    efforts_by_model: dict[str, frozenset[str]]
    default_model: str


def _utc(epoch: float | int | None) -> str | None:
    if not isinstance(epoch, (float, int)):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _ratio(used_percent: object) -> tuple[float | None, float | None]:
    if not isinstance(used_percent, (int, float)) or not 0 <= used_percent <= 100:
        return None, None
    used = float(used_percent) / 100
    return 1 - used, used


def parse_codex_rate_limits(account_id: str, result: dict, observed_at: str) -> LimitSnapshot:
    """Normalize every reported duration and quota pool; never assume 5h+week."""
    root = result.get("rateLimits") or {}
    pools = result.get("rateLimitsByLimitId") or {}
    sources: list[tuple[str, dict]] = []
    if isinstance(root, dict) and (not isinstance(pools, dict) or "codex" not in pools):
        sources.append(("", root))
    if isinstance(pools, dict):
        sources.extend((str(k), v) for k, v in pools.items() if isinstance(v, dict))
    windows: list[LimitWindow] = []
    seen: set[str] = set()
    for pool_id, source in sources:
        for slot in ("primary", "secondary"):
            entry = source.get(slot)
            if not isinstance(entry, dict):
                continue
            duration = entry.get("windowDurationMins")
            if not isinstance(duration, (int, float)) or duration <= 0:
                continue
            duration = int(duration)
            kind = {300: "five_hour", 10080: "weekly", 43200: "monthly"}.get(duration, f"window_{duration}m")
            window_id = f"{kind}@{pool_id}" if pool_id else kind
            if window_id in seen:
                continue
            seen.add(window_id)
            remaining, used = _ratio(entry.get("usedPercent"))
            windows.append(LimitWindow(
                window_id=window_id, kind=kind,
                label=kind.replace("_", " ").title() if not pool_id else f"{pool_id} {kind.replace('_', ' ')}",
                remaining_ratio=remaining, used_ratio=used,
                reset_at=_utc(entry.get("resetsAt")),
                duration_seconds=duration * 60, source="codex_app_server",
                confidence="PROVIDER", quota_bucket=pool_id,
            ))
    return LimitSnapshot(account_id, tuple(windows), observed_at, "codex_app_server")


class CodexLimitAdapter:
    provider_id = "codex"

    def __init__(self, executable: str | None = None, timeout_seconds: float = 30) -> None:
        self.executable = executable or shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
        self.timeout_seconds = timeout_seconds

    def probe_limits(self, account: AccountIdentity) -> LimitSnapshot:
        response = self._query(account, "account/rateLimits/read")
        return parse_codex_rate_limits(account.account_id, response,
                                       datetime.now(timezone.utc).isoformat())

    def discover_models(self, account: AccountIdentity) -> CodexModelCatalog:
        """Read model-specific reasoning choices from local app-server metadata."""
        result: dict[str, frozenset[str]] = {}
        default_model = ""
        cursor = None
        for _ in range(3):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = self._query(account, "model/list", params)
            for item in response.get("data", ()):
                if not isinstance(item, dict):
                    continue
                model = item.get("model")
                if not isinstance(model, str) or not model:
                    continue
                efforts = frozenset(option["reasoningEffort"] for option in
                                    item.get("supportedReasoningEfforts", ())
                                    if isinstance(option, dict) and
                                    isinstance(option.get("reasoningEffort"), str))
                result[model] = efforts
                if item.get("isDefault") is True:
                    default_model = model
            cursor = response.get("nextCursor")
            if not cursor:
                return CodexModelCatalog(result, default_model)
        raise RuntimeError("Codex model catalog exceeds bounded page limit")

    def _query(self, account: AccountIdentity, method: str,
               params: dict | None = None) -> dict:
        if account.provider_id != self.provider_id:
            raise ValueError("wrong provider account")
        if not account.profile_locator or not os.path.isdir(account.profile_locator):
            raise FileNotFoundError(f"Codex profile locator missing: {account.profile_locator!r}")
        if not self.executable:
            raise FileNotFoundError("codex CLI unavailable")
        expected_account_id = None
        auth_file = os.path.join(account.profile_locator, "auth.json")
        if os.path.isfile(auth_file):
            try:
                with open(auth_file, "r", encoding="utf-8") as f:
                    auth_doc = json.load(f)
                    if isinstance(auth_doc, dict):
                        tokens = auth_doc.get("tokens") or {}
                        if isinstance(tokens, dict):
                            expected_account_id = tokens.get("account_id")
            except Exception:
                pass
        env = dict(os.environ)
        env["CODEX_HOME"] = account.profile_locator
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(name, None)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child = subprocess.Popen(
            [self.executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            creationflags=flags, bufsize=0,
        )
        answers: queue.Queue[dict] = queue.Queue()

        def read_output() -> None:
            assert child.stdout is not None
            for raw in child.stdout:
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict) and "id" in value:
                    answers.put(value)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + self.timeout_seconds

        def send(method: str, request_id: int | None, params: dict | None = None) -> None:
            message: dict = {"jsonrpc": "2.0", "method": method}
            if request_id is not None:
                message["id"] = request_id
            message["params"] = params if params is not None else {}
            assert child.stdin is not None
            child.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            child.stdin.flush()

        def receive(request_id: int) -> dict:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex rate limit read timed out")
                try:
                    message = answers.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("Codex rate limit read timed out") from exc
                if message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError("Codex rate limit read failed")
                    return message.get("result") or {}

        try:
            send("initialize", 1, {"clientInfo": {"name": "audapack", "version": "1"}, "capabilities": None})
            receive(1)
            send("initialized", None)
            send(method, 2, params)
            res = receive(2)
            if method == "account/rateLimits/read":
                probed_id = res.get("accountId")
                if expected_account_id and probed_id:
                    if str(expected_account_id).strip().lower() != str(probed_id).strip().lower():
                        raise RuntimeError(f"Codex identity mismatch for {account.account_id}: expected {expected_account_id}, got {probed_id}")
            return res
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=2)
            except (subprocess.TimeoutExpired, ValueError):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            reader.join(timeout=2)


_CLAUDE_USAGE = re.compile(r"^(?P<label>[^:]+):\s*(?P<used>\d{1,3})%\s*used", re.I)
_CLAUDE_RESET = re.compile(r"\bresets\s+(?P<reset>[^\n(]+)", re.I)


def parse_claude_usage(account_id: str, answer: str, observed_at: str) -> LimitSnapshot:
    windows: list[LimitWindow] = []
    for line in answer.splitlines():
        match = _CLAUDE_USAGE.match(line.strip())
        if not match:
            continue
        label = " ".join(match.group("label").split())
        folded = label.casefold()
        kind = "five_hour" if folded == "current session" else "weekly" if folded.startswith("current week") else "other"
        remaining, used = _ratio(int(match.group("used")))
        reset_at = None
        reset_match = _CLAUDE_RESET.search(line)
        reset_text = (reset_match.group("reset") if reset_match else "").strip()
        if reset_text:
            for format_string in ("%b %d, %Y, %I:%M%p", "%b %d, %I:%M%p", "%b %d, %I%p"):
                try:
                    local = datetime.strptime(reset_text, format_string)
                except ValueError:
                    continue
                if "%Y" not in format_string:
                    local = local.replace(year=datetime.now().year)
                # Naive -> UTC uses the machine's local rules for that future
                # date, including DST, instead of today's fixed UTC offset.
                reset_at = local.astimezone(timezone.utc).isoformat()
                break
        windows.append(LimitWindow(
            window_id=re.sub(r"[^a-z0-9]+", "_", folded).strip("_"), kind=kind, label=label,
            remaining_ratio=remaining, used_ratio=used, reset_at=reset_at,
            source="claude_cli_usage", confidence="PROVIDER",
        ))
    return LimitSnapshot(account_id, tuple(windows), observed_at, "claude_cli_usage")


class ClaudeLimitAdapter:
    provider_id = "claude"

    def __init__(self, executable: str | None = None, timeout_seconds: float = 30) -> None:
        self.executable = executable or shutil.which("claude.exe") or shutil.which("claude")
        self.timeout_seconds = timeout_seconds

    def probe_limits(self, account: AccountIdentity) -> LimitSnapshot:
        if account.provider_id != self.provider_id:
            raise ValueError("wrong provider account")
        if not self.executable:
            raise FileNotFoundError("claude CLI unavailable")
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = account.profile_locator
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(name, None)
        session_id = str(uuid.uuid4())
        cwd = Path(tempfile.gettempdir()) / "audapack-limit-probe"
        cwd.mkdir(exist_ok=True)
        result = subprocess.run(
            [self.executable, "-p", "/usage", "--output-format", "json", "--session-id", session_id],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=self.timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        # Remove only the transcript created by this exact probe session.
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
        transcript = Path(account.profile_locator) / "projects" / slug / f"{session_id}.jsonl"
        transcript.unlink(missing_ok=True)
        if result.returncode:
            raise RuntimeError("Claude /usage failed")
        payload = json.loads(result.stdout)
        if payload.get("is_error"):
            raise RuntimeError("Claude /usage reported an error")
        answer = payload.get("result")
        if not isinstance(answer, str):
            raise ValueError("Claude /usage returned no text")
        return parse_claude_usage(account.account_id, answer, datetime.now(timezone.utc).isoformat())


def _antigravity_credential_present() -> bool:
    """Check Windows Credential Manager target presence without reading a blob."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        pointer = ctypes.c_void_p()
        found = ctypes.windll.advapi32.CredReadW("gemini:antigravity", 1, 0, ctypes.byref(pointer))
        if pointer.value:
            ctypes.windll.advapi32.CredFree(pointer)
        return bool(found)
    except (AttributeError, OSError):
        return False


@dataclass(frozen=True)
class AntigravityAccountContext:
    """Which execution context an Antigravity account's probe runs in.

    PHASE 0 of SRC-109 proved the installed CLI has no process-local account
    isolation: one logon-scoped ``gemini:antigravity`` credential, no
    auth-scope flag, and ``AGY_ACCOUNT`` honoured only by sandboxed child
    commands. A second account needs a different *context*, and this descriptor
    names one without ever naming a secret.
    """

    context_id: str
    label: str
    backend: str
    context_locator: str = ""
    data_dir: str = ""
    enabled: bool = True

# ponytail: only "default" is probeable here. A separate Windows user is a
# separate Credential Manager scope this process cannot read, and a Gemini API
# key is a billing context we refuse to store. Both raise until the operator
# provisions them; add a backend when one can actually be proven on this box.
LOCAL_ANTIGRAVITY_CONTEXT = AntigravityAccountContext(
    context_id="antigravity:local", label="This Windows sign-in", backend="default")


def probe_env(context: AntigravityAccountContext) -> dict[str, str]:
    """Environment for one Antigravity probe. Never carries a credential."""
    env = dict(os.environ)
    env["BROWSER"] = str(Path(env.get("SystemRoot") or r"C:\Windows") / "System32" / "where.exe")
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    if context.data_dir:
        env["ANTIGRAVITY_EXECUTABLE_DATA_DIR"] = context.data_dir
    return env


def parse_antigravity_usage(account_id: str, payload: dict, observed_at: str) -> LimitSnapshot:
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    windows: list[LimitWindow] = []
    for group in groups if isinstance(groups, list) else ():
        if not isinstance(group, dict):
            continue
        label = str(group.get("name") or "").strip()
        bucket_id = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
        if not bucket_id:
            continue
        for bucket in group.get("buckets") or ():
            if not isinstance(bucket, dict):
                continue
            raw_kind = str(bucket.get("window") or "").casefold()
            kind = {"5h": "five_hour", "weekly": "weekly", "7d": "weekly",
                    "monthly": "monthly", "30d": "monthly"}.get(raw_kind, raw_kind)
            fraction = bucket.get("remaining_fraction")
            if not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
                continue
            if bucket.get("disabled") is True:
                fraction = 0
            reset = bucket.get("reset_time")
            try:
                reset_at = datetime.fromisoformat(str(reset).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat() if reset else None
            except ValueError:
                reset_at = None
            windows.append(LimitWindow(
                window_id=f"{kind}@{bucket_id}", kind=kind, label=f"{label} {raw_kind}",
                remaining_ratio=float(fraction), used_ratio=1 - float(fraction),
                reset_at=reset_at, source="agy_cli_usage", confidence="PROVIDER",
                quota_bucket=bucket_id,
            ))
    return LimitSnapshot(account_id, tuple(windows), observed_at, "agy_cli_usage")


class AntigravityLimitAdapter:
    provider_id = "antigravity"

    def __init__(self, executable: str | None = None, timeout_seconds: float = 30,
                 context: AntigravityAccountContext | None = None) -> None:
        self.executable = executable or shutil.which("agy.exe") or shutil.which("agy")
        self.timeout_seconds = timeout_seconds
        self.context = context or LOCAL_ANTIGRAVITY_CONTEXT

    def probe_limits(self, account: AccountIdentity) -> LimitSnapshot:
        if account.provider_id != self.provider_id:
            raise ValueError("wrong provider account")
        if getattr(account, "discovery_source", "") == sai_accounts.SHARED_SOURCE:
            return self._probe_shared(account)
        if self.context.backend != "default":
            # Another context's credential is either unreadable from here (a
            # second Windows user) or one we refuse to hold (an API key).
            # Probing anyway would stamp THIS sign-in's numbers under that
            # account's name, which is the exact lie PHASE 0 forbids.
            raise RuntimeError(f"antigravity_context_unavailable:{self.context.context_id}")
        if not self.executable:
            raise FileNotFoundError("agy CLI unavailable")
        if not _antigravity_credential_present():
            raise RuntimeError("Antigravity credential target absent")
        result = subprocess.run(
            [self.executable, "-p", "/usage", "--output-format", "json"],
            cwd=Path(tempfile.gettempdir()), env=probe_env(self.context), capture_output=True, text=True,
            timeout=self.timeout_seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode:
            raise RuntimeError("agy /usage failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("agy /usage returned invalid JSON")
        return parse_antigravity_usage(account.account_id, payload, datetime.now(timezone.utc).isoformat())

    def _probe_shared(self, account: AccountIdentity) -> LimitSnapshot:
        """Read a shared account THROUGH the plane, never through this machine.

        ``agy -p /usage`` answers about the account of whoever is logged in
        here. A shared record names a *different* account, so falling through
        to the local read would report one account's numbers under another's
        name. The plane owns that identity and the context it lives in.
        """
        canonical = sai_accounts.canonical_id_for(account.provider_id, account.profile_locator)
        observed = datetime.now(timezone.utc).isoformat()
        if not canonical:
            # The plane is gone or no longer lists this account. Report the
            # shared source as unavailable rather than guessing locally.
            return LimitSnapshot(account_id=account.account_id, windows=(),
                                 observed_at=observed, source="sai_accounts",
                                 error="shared_source_offline")
        windows, state, detail = sai_accounts.probe(canonical, self.timeout_seconds)
        if state == "ok":
            return LimitSnapshot(account_id=account.account_id, windows=tuple(windows),
                                 observed_at=observed, source="sai_accounts")
        if state == "unsupported":
            # The plane has no broker for this provider and says so. That is an
            # honest no-opinion, and it is not an outage for this account.
            raise RuntimeError("sai_accounts_cannot_read_provider")
        return LimitSnapshot(account_id=account.account_id, windows=(), observed_at=observed,
                             source="sai_accounts", error=f"{state}:{detail}")
