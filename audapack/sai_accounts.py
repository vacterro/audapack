"""SAI Accounts — the OPTIONAL shared account control plane.

SAI Accounts is federation, not captivity. This module is the whole integration
and it is allowed to answer *nothing*: with the plane absent, stopped, broken or
uninstalled, every function here returns an empty result and AUDAPACK discovers
and probes accounts exactly as it did before this file existed. Nothing here
writes, publishes or exports — an account only reaches the shared registry
through an explicit action the operator takes elsewhere.

The rules, once each:

* **STANDALONE** — no plane. ``list_shared`` is empty and nothing is merged.
  Every account AUDAPACK discovers on its own keeps its own identity, its own
  reader and its own launcher bindings.
* **FEDERATED** — plane present. Shared accounts this installation does not
  already know appear in the registry as ``discovery_source="sai_accounts_shared"``.
* **HYBRID** — both. A shared account and a local account merge into ONE record
  only when their provider identity locator proves they are the same identity.
  A display name is never evidence.

The plane owns the canonical ``account_id``, the provider, the execution context
and the global lifecycle state. AUDAPACK keeps owning what is local to it: which
launchers an account is bound to, whether it is enabled here, and its own
``account_id`` scheme, which is a different namespace on purpose so a shared
record can never collide with a local one.

ponytail: the plane is a registry first and a broker second. For a provider it
does not yet read it answers ``provider_does_not_support_quota``, which is an
honest "no opinion" rather than an outage. Upgrade path: when the plane gains a
broker for that provider the same call returns windows and nothing here changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Where the control plane installs itself. Absent everywhere else, which is the
# point: a machine that never installed it takes the STANDALONE path.
CANONICAL_INSTALL = Path("C:/ProgramData/SAI/Accounts/bin")
ENV_OVERRIDE = "SAI_ACCOUNTS_EXE"

# Bound, like every other child this application spawns. The plane is a local
# CLI: a read that has not answered by now is a plane that is not there.
LIST_TIMEOUT_S = 4.0
USAGE_TIMEOUT_CAP_S = 30.0

# discovery_source marker. AUDAPACK already stores this column, so origin is
# visible without a schema migration and without inventing a parallel table.
SHARED_SOURCE = "sai_accounts_shared"

# One vocabulary for "this is where the account's identity lives", strongest
# evidence first. profile_locator is a real path and is directly comparable to
# AUDAPACK's own discovery; windows_user is a Windows account name. The public
# list deliberately withholds the SID and no version may reintroduce it here.
_LOCATOR_FIELDS = ("profile_locator", "windows_user", "context_label")

# Test seams. Both are monkeypatched by the federation suite and neutralized
# for the whole test session by conftest; nothing else may set them.
TestEngine = None
TestRun = None


@dataclass(frozen=True)
class SharedAccount:
    """One account the shared registry owns, as the public list projection."""

    account_id: str          # canonical, plane-owned identity
    provider_id: str
    display_name: str
    compact_label: str
    backend: str             # windows_user / profile_directory
    locator: str             # stable provider identity locator, may be ""
    operational_state: str


def engine_path() -> str:
    """Resolve the plane's CLI, or return "" when it is not installed.

    Resolution order: the explicit override, then the canonical install, then
    PATH. Every branch may legitimately produce "".
    """
    if TestEngine is not None:
        return TestEngine() or ""
    override = os.environ.get(ENV_OVERRIDE, "")
    if override and os.path.isfile(override):
        return override
    installed = CANONICAL_INSTALL / "sai-accounts.exe"
    if installed.is_file():
        return str(installed)
    return shutil.which("sai-accounts") or ""


def _run(argv: list[str], timeout: float) -> dict:
    """Spawn the plane once, bounded. Never raises.

    Matches the adapter-local policy in ``limit_adapters``: argv is always a
    list so no argument can be reinterpreted by a shell, the child never opens a
    console window, and a timeout kills it rather than hanging the caller.
    """
    if TestRun is not None:
        return TestRun(argv, timeout)
    exe = engine_path()
    if not exe:
        return {"ok": False, "stdout": "", "error": "plane_absent"}
    try:
        done = subprocess.run(
            [exe, *argv], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max(0.1, timeout), check=False,
            cwd=tempfile.gettempdir(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "error": "timeout"}
    except OSError as exc:
        return {"ok": False, "stdout": "", "error": type(exc).__name__}
    return {"ok": done.returncode == 0, "stdout": done.stdout or "",
            "error": "" if done.returncode == 0 else f"exit {done.returncode}"}


def _json_object(text: str) -> dict:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def locator_of(entry: dict) -> str:
    """The account's stable identity locator, strongest evidence available."""
    meta = entry.get("provider_metadata")
    if isinstance(meta, dict):
        for field in _LOCATOR_FIELDS:
            value = meta.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for field in _LOCATOR_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def identity_key(provider_id: str, locator: str) -> str:
    """Case-insensitive merge key. Empty means "unprovable".

    An empty locator must never produce a key: accounts with no comparable
    identity are not the same account, and a key built from "" would collapse
    every locator-less account into one.
    """
    provider = (provider_id or "").strip().lower()
    value = (locator or "").strip().lower()
    if not provider or not value:
        return ""
    return f"{provider}|{value}"


def shared_account_id(provider: str, locator: str, *, fallback: str = "") -> str:
    """A stable account id for a shared record, in its own namespace.

    Deliberately NOT ``account_registry._identity``: that one hashes a resolved
    *path*, so feeding it an opaque locator like a Windows user name would make
    the answer depend on the current working directory. This namespace says
    ``shared`` out loud, so a shared record can never be mistaken for — or
    silently overwrite — a locally discovered one.

    ``fallback`` is used only when there is no locator at all, in which case the
    plane's own canonical id is the only stable thing left to key on. Hashing ""
    instead would collapse every such account onto a single row.
    """
    value = (locator or fallback or "").strip().lower()
    if not (provider or "").strip() or not value:
        return ""
    digest = hashlib.sha256(f"{provider.strip().lower()}\0shared\0{value}"
                            .encode("utf-8")).hexdigest()[:20]
    return f"{provider.strip().lower()}:shared:{digest}"


@dataclass(frozen=True)
class SharedListing:
    """One read of the plane, split by what this consumer must do with it.

    ``accounts`` is what may be drawn; ``suppressed`` is the identity keys of
    the accounts the plane has taken off the board. Two answers to the SAME
    question, so they come from one pass over one list call.
    """

    accounts: list[SharedAccount]   # enabled + unhidden, for display
    suppressed: frozenset[str]      # identity keys the plane withdrew


def withdrawn_by_plane(entry: dict) -> bool:
    """True when the plane's own global state keeps this account off the board.

    One predicate, two uses. An account it answers true for is never drawn as a
    shared record, AND a local record proven to be the same identity is
    suppressed — the same account must not come back through AUDAPACK's own
    discovery just because AUDAPACK already had it. ``enabled`` here is a LOCAL
    setting and is never read from or written to the registry.
    """
    if entry.get("hidden") is True:
        return True
    state = entry.get("operational_state")
    state = state if isinstance(state, str) and state else "ENABLED"
    return state != "ENABLED"


def read_registry(provider_ids, timeout: float | None = None) -> SharedListing:
    """Read the plane once and split its accounts into drawn and suppressed."""
    exe = engine_path()
    if not exe:
        return SharedListing([], frozenset())
    # --all so an ARCHIVED account is still projected. Without it the plane
    # drops archived accounts from the payload entirely, and a withdrawal this
    # consumer cannot see is a withdrawal it cannot honour.
    result = _run(["list", "--all"], LIST_TIMEOUT_S if timeout is None else timeout)
    payload = _json_object(result.get("stdout", ""))
    entries = payload.get("accounts")
    if not isinstance(entries, list):
        return SharedListing([], frozenset())
    wanted = {str(p).lower() for p in provider_ids}
    found: list[SharedAccount] = []
    suppressed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("account_id")
        provider = entry.get("provider_id")
        if not isinstance(account_id, str) or not account_id.strip():
            continue
        if not isinstance(provider, str) or provider.strip().lower() not in wanted:
            continue
        locator = locator_of(entry)
        state = entry.get("operational_state")
        state = state if isinstance(state, str) and state else "ENABLED"
        if withdrawn_by_plane(entry):
            # An unprovable identity suppresses nothing: it would suppress
            # every record that also has no locator, which is a different one.
            key = identity_key(provider, locator)
            if key:
                suppressed.add(key)
            continue
        found.append(SharedAccount(
            account_id=account_id.strip(),
            provider_id=provider.strip(),
            display_name=str(entry.get("display_name") or account_id),
            compact_label=str(entry.get("compact_label") or ""),
            backend=str(entry.get("execution_backend") or ""),
            locator=locator,
            operational_state=state,
        ))
    return SharedListing(found, frozenset(suppressed))


def list_shared(provider_ids, timeout: float | None = None) -> list[SharedAccount]:
    """Every enabled, unhidden shared account for these providers."""
    return read_registry(provider_ids, timeout).accounts


# The last list this process read, so probing a shared account can recover its
# canonical id from the locator it was discovered by. The plane accepts a
# display name as a selector too, and using one here would reintroduce exactly
# the merge-on-a-label ambiguity the whole design refuses.
_LIST_CACHE: list[SharedAccount] = []

# The identity keys the SAME read withdrew. Kept beside the cache rather than
# re-read, so honouring a global hide never costs a second child process.
_SUPPRESSED: frozenset[str] = frozenset()


def remember(shared: list[SharedAccount]) -> list[SharedAccount]:
    global _SUPPRESSED
    _LIST_CACHE[:] = list(shared)
    _SUPPRESSED = frozenset()
    return list(_LIST_CACHE)


def suppressed_keys() -> frozenset[str]:
    """Identity keys the plane withdrew on the last read. Empty when absent."""
    return _SUPPRESSED


def canonical_id_for(provider_id: str, locator: str) -> str:
    """The plane's own id for this account, from the last list this process read."""
    key = identity_key(provider_id, locator)
    if not key:
        return ""
    for entry in _LIST_CACHE:
        if identity_key(entry.provider_id, entry.locator) == key:
            return entry.account_id
    return ""


def parse_windows(payload: dict) -> list:
    """Translate the plane's window list into ``LimitWindow`` records.

    The two vocabularies are kept apart deliberately — the plane speaks in
    ``remaining_fraction`` and names a window ``5h``, this module in ratios and
    ``five_hour`` — so the conversion lives in exactly one function and the two
    dialects cannot drift into two parsers.
    """
    from audapack.limits import LimitWindow
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return []
    kinds = {"5h": "five_hour", "weekly": "weekly", "7d": "weekly",
             "monthly": "monthly", "30d": "monthly"}
    out: list[LimitWindow] = []
    for item in windows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("window") or "").strip().casefold()
        kind = kinds.get(name, name)
        if not kind:
            continue
        fraction = item.get("remaining_fraction")
        if not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
            continue
        bucket = str(item.get("pool_index") or "0")
        window_id = f"{kind}@{bucket}"
        label = {"five_hour": "5h", "weekly": "weekly"}.get(kind, kind)
        try:
            reset = item.get("reset_time")
            reset_at = reset if isinstance(reset, str) and reset else None
            out.append(LimitWindow(
                window_id=window_id, kind=kind, label=f"{label}",
                remaining_ratio=float(fraction),
                used_ratio=round(1.0 - float(fraction), 6),
                reset_at=reset_at, duration_seconds=None,
                source="sai_accounts_usage", confidence="PROVIDER",
                quota_bucket=bucket,
            ))
        except (TypeError, ValueError):
            continue
    return out


def probe(account_id: str, timeout: float | None = None) -> tuple:
    """Read one shared account through the plane.

    Returns ``(windows, state, detail)`` where state is one of ``ok``,
    ``unsupported``, ``offline``, ``auth_required`` or ``unavailable``.

    The payload is read BEFORE the exit code. A plane that cannot read one
    account answers with a typed envelope AND a nonzero exit — that is a known
    state about one account, not an outage, and reporting it as an outage would
    be a lie about the whole control plane.
    """
    result = _run(["usage", account_id],
                  USAGE_TIMEOUT_CAP_S if timeout is None else timeout)
    payload = _json_object(result.get("stdout", ""))
    if not payload:
        return [], "unavailable", "SAI Accounts did not answer"
    windows = parse_windows(payload)
    if windows:
        return windows, "ok", ""
    if str(payload.get("auth_state") or "") == "AUTH_REQUIRED":
        return [], "auth_required", "not authenticated"
    skipped = str(payload.get("skipped_reason") or "")
    if skipped == "provider_does_not_support_quota":
        return [], "unsupported", skipped
    if skipped or str(payload.get("context_state") or "") == "OFFLINE":
        return [], "offline", skipped or "account context is offline"
    return [], "unavailable", "SAI Accounts returned no usable reading"


def fresh_list(providers, timeout: float | None = None) -> list[SharedAccount]:
    """Read the plane and remember it, in one call."""
    global _SUPPRESSED
    listing = read_registry(providers, timeout)
    # remember() clears the withdrawn set (a hand-fed list has no provenance),
    # so the real read's keys are recorded after it, not before.
    cached = remember(listing.accounts)
    _SUPPRESSED = listing.suppressed
    return cached
