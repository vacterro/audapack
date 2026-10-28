"""Bounded, credential-free discovery of local provider account identities.

Launcher configuration describes how to start a tool. This registry describes
which local profile that launch will use. It reads filenames, never auth data.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from audapack import sai_accounts
from audapack.cli_launchers import CODEX_ACCOUNT_HOMES
from audapack.config import get_state_dir


@dataclass(frozen=True)
class AccountIdentity:
    account_id: str
    provider_id: str
    display_name: str
    profile_locator: str
    launcher_ids: tuple[str, ...]
    discovery_source: str
    last_seen_at: str
    enabled: bool = True
    #: The plane's GLOBAL lifecycle state for this account ("" for a purely local
    #: record). Kept apart from ``enabled`` on purpose: ``enabled`` is AUDAPACK's
    #: local visibility, this is the registry's own verdict, and conflating the
    #: two is what let a withdrawn account stay launchable.
    operational_state: str = ""

    @property
    def bound(self) -> bool:
        return bool(self.launcher_ids)

    @property
    def shared(self) -> bool:
        return self.discovery_source == sai_accounts.SHARED_SOURCE

    @property
    def operational(self) -> bool:
        """Globally usable according to the plane (a local record always is)."""
        return self.operational_state in ("", "ENABLED")

    @property
    def launchable(self) -> bool:
        """Has a local launch target AND is locally visible AND is operational."""
        return self.bound and self.enabled and self.operational


def _identity(provider: str, profile: Path) -> str:
    # Profile path is a local, non-secret discriminator. Do not hash credential
    # contents or use display names; both would make identity unstable/unsafe.
    canonical = os.path.normcase(os.path.normpath(str(profile.expanduser().resolve())))
    digest = hashlib.sha256(f"{provider}\0{canonical}".encode("utf-8")).hexdigest()[:20]
    return f"{provider}:{digest}"


def identity_locator(provider: str, profile: Path, home: Path) -> str:
    """Where this account's identity lives, in a vocabulary the shared plane uses.

    A config-directory account is located by its path, which is directly
    comparable to the plane's ``profile_locator``. An Antigravity account is
    bound to a *Windows user*, not to a directory — its data root exists for
    every user on the machine — so the comparable identity is the account name
    the profile sits under, which is the same locator the plane projects.
    """
    if provider == "antigravity":
        return home.name
    return os.path.normcase(os.path.normpath(str(profile.expanduser().resolve())))


def _auth_marker_exists(provider: str, profile: Path) -> bool:
    if provider == "antigravity":
        # The CLI's account is bound to this data root. Windows Credential
        # Manager owns auth; no credential bytes are read for discovery.
        return profile.is_dir()
    markers = {
        "codex": ("auth.json",),
        "claude": (".credentials.json", ".claude.json"),
    }[provider]
    return any((profile / name).is_file() for name in markers)


def local_candidates(home: Path) -> list[tuple[str, str, str, Path]]:
    """The local identities AUDAPACK can discover, as data.

    One table, two consumers: discovery builds records from it, and the launcher
    federation maps a retired static launcher id onto the canonical account that
    is provably the same identity. A second copy of this table is exactly how the
    two lists drift apart, so there is none.
    """
    return [
        ("codex", "main_codex", "Codex 1", home / CODEX_ACCOUNT_HOMES["main_codex"]),
        ("codex", "main_codex2", "Codex 2", home / CODEX_ACCOUNT_HOMES["main_codex2"]),
        ("codex", "main_codex3_free", "Codex Free", home / CODEX_ACCOUNT_HOMES["main_codex3_free"]),
        ("claude", "claude1", "Claude 1", home / ".claude"),
        ("claude", "claude2", "Claude 2", home / ".claude-account2"),
        ("antigravity", "antigravity", "Antigravity", home / ".gemini" / "antigravity"),
    ]


def discover_accounts(
    launchers: Iterable[object], *, home: Path | None = None, now: datetime | None = None,
    shared_accounts: list[sai_accounts.SharedAccount] | None = None,
) -> list[AccountIdentity]:
    """Inspect only known profile locations and configured launcher identities.

    A profile can be present without a launcher, in which case it is UNBOUND.
    No recursion, provider request, or reading of credential file contents.

    Accounts the SAI Accounts control plane shares are folded in last, and only
    the ones this installation does not already have. The plane is optional: it
    is consulted through one call that answers "nothing" when it is absent, and
    with no plane installed this function returns exactly the local records it
    always did.
    """
    home = Path(home) if home is not None else Path.home()
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    configured: Mapping[str, object] = {str(getattr(item, "id", "")): item for item in launchers}
    candidates = local_candidates(home)
    found: list[tuple[str, AccountIdentity]] = []
    local_by_key: dict[str, AccountIdentity] = {}
    for provider, launcher_id, label, profile in candidates:
        if not _auth_marker_exists(provider, profile):
            continue
        launcher = configured.get(launcher_id)
        key = sai_accounts.identity_key(provider, identity_locator(provider, profile, home))
        record = AccountIdentity(
            account_id=_identity(provider, profile),
            provider_id=provider,
            display_name=label,
            profile_locator=str(profile.resolve()),
            launcher_ids=(launcher_id,) if launcher is not None else (),
            discovery_source=("known_provider_data_dir" if provider == "antigravity"
                              else "known_profile_auth_marker"),
            last_seen_at=stamp,
            enabled=bool(getattr(launcher, "enabled", True)),
        )
        found.append((key, record))
        if key:
            local_by_key[key] = record
    fresh_shared, replaced = _shared_accounts(
        candidates, local_by_key, stamp, configured, shared_accounts=shared_accounts,
    )
    withdrawn = sai_accounts.suppressed_keys()
    out: list[AccountIdentity] = []
    for key, record in found:
        # A record found here is the same physical account the registry already
        # knows, so honouring a global hide only on the shared half would let
        # every account AUDAPACK already knew survive the very hide the operator
        # asked for. An unprovable key suppresses nothing, and a machine with no
        # plane read has an empty withdrawn set — a standalone install is
        # unchanged.
        if key and key in withdrawn:
            continue
        # One identity, one row: a proven duplicate is represented by whichever
        # side carries an actual launch target, so the projection follows the
        # binding instead of the order the two lists happen to be built in.
        out.append(replaced.get(key, record) if key else record)
    return out + fresh_shared


def _shared_accounts(
    candidates: list[tuple[str, str, str, Path]],
    local_by_key: Mapping[str, AccountIdentity],
    stamp: str,
    configured: Mapping[str, object],
    shared_accounts: list[sai_accounts.SharedAccount] | None = None,
) -> tuple[list[AccountIdentity], dict[str, AccountIdentity]]:
    """Shared accounts, split into new identities and proven duplicates.

    Returns the shared records that are NO local record's projection, plus the
    ones that replace an unbound local projection of the same identity.

    A shared record is bound to the effective launch target derived from its OWN
    canonical ``account_id`` -- never to a static launcher the operator had to
    pick by hand. That binding is derived on every read, so an account added,
    renamed or removed in the registry needs no AUDAPACK rebinding step at all;
    when no target row exists (a fresh install that has not synced yet) the
    account is honestly UNBOUND rather than pointed at a stranger's launcher.
    """
    from audapack.launch_federation import target_id

    if shared_accounts is not None:
        shared = list(shared_accounts)
    else:
        shared = sai_accounts.fresh_list({provider for provider, _l, _n, _p in candidates})
    fresh: list[AccountIdentity] = []
    replaced: dict[str, AccountIdentity] = {}
    seen: set[str] = set()
    for entry in shared:
        key = sai_accounts.identity_key(entry.provider_id, entry.locator)
        if key and key in seen:
            continue
        # A record with no locator is unprovable, not worthless. It is still the
        # operator's account, and dropping it would lose an account identity
        # without anyone being told. It is shown, marked shared, and merged with
        # nothing.
        account_id = sai_accounts.shared_account_id(
            entry.provider_id, entry.locator, fallback=entry.account_id)
        if not account_id:
            continue
        if key:
            seen.add(key)
        tid = target_id(entry.account_id)
        row = configured.get(tid)
        record = AccountIdentity(
            account_id=account_id,
            provider_id=entry.provider_id,
            display_name=entry.display_name,
            profile_locator=entry.locator,
            launcher_ids=(tid,) if row is not None else (),
            discovery_source=sai_accounts.SHARED_SOURCE,
            last_seen_at=stamp,
            enabled=bool(getattr(row, "enabled", True)),
            operational_state=entry.operational_state,
        )
        local = local_by_key.get(key) if key else None
        if local is None:
            fresh.append(record)
        elif record.bound and not local.bound:
            # The canonical account has a real target and the local residue has
            # none, so the registry's projection is the truthful one.
            replaced[key] = record
        # Otherwise the local record already IS the effective projection of this
        # identity (it has the binding, or neither side has one).
    return fresh, replaced


class AccountRegistry:
    """SQLite-backed account registry under AUDAPACK runtime state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_state_dir() / "resources.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    profile_locator TEXT NOT NULL,
                    launcher_ids TEXT NOT NULL,
                    discovery_source TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS account_bindings (
                    account_id TEXT PRIMARY KEY, launcher_id TEXT NOT NULL)"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def upsert(self, accounts: Iterable[AccountIdentity]) -> None:
        with closing(self._connect()) as db, db:
            for account in accounts:
                db.execute(
                    """INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        provider_id=excluded.provider_id,
                        display_name=excluded.display_name,
                        profile_locator=excluded.profile_locator,
                        launcher_ids=excluded.launcher_ids,
                        discovery_source=excluded.discovery_source,
                        last_seen_at=excluded.last_seen_at,
                        enabled=excluded.enabled""",
                    (
                        account.account_id, account.provider_id, account.display_name,
                        account.profile_locator, ",".join(account.launcher_ids),
                        account.discovery_source, account.last_seen_at, int(account.enabled),
                    ),
                )

    def list(self) -> list[AccountIdentity]:
        with closing(self._connect()) as db, db:
            rows = db.execute(
                """SELECT account_id, provider_id, display_name, profile_locator,
                   launcher_ids, discovery_source, last_seen_at, enabled
                   FROM accounts ORDER BY provider_id, display_name"""
            ).fetchall()
            bindings = dict(db.execute("SELECT account_id, launcher_id FROM account_bindings"))
        automatic = {launcher: row[0] for row in rows
                     for launcher in filter(None, row[4].split(","))}
        return [
            AccountIdentity(
                account_id=row[0], provider_id=row[1], display_name=row[2],
                profile_locator=row[3],
                launcher_ids=tuple(dict.fromkeys((
                    *filter(None, row[4].split(",")),
                    *((bindings[row[0]],) if row[0] in bindings and
                      automatic.get(bindings[row[0]], row[0]) == row[0] else ()),
                ))),
                discovery_source=row[5], last_seen_at=row[6], enabled=bool(row[7]),
            )
            for row in rows
        ]

    def bind(self, account_id: str, launcher_id: str) -> None:
        """Persist an explicit operator binding across rediscovery."""
        if not account_id or not launcher_id:
            raise ValueError("account and launcher required")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT account_id, launcher_ids FROM accounts").fetchall()
            if not any(row[0] == account_id for row in rows):
                db.rollback()
                raise ValueError("account not discovered")
            if any(row[0] != account_id and launcher_id in row[1].split(",") for row in rows):
                db.rollback()
                raise ValueError("launcher already identifies another account")
            owner = db.execute("SELECT account_id FROM account_bindings WHERE launcher_id=?",
                               (launcher_id,)).fetchone()
            if owner and owner[0] != account_id:
                db.rollback()
                raise ValueError("launcher already bound to another account")
            db.execute("""INSERT INTO account_bindings VALUES (?, ?)
                ON CONFLICT(account_id) DO UPDATE SET launcher_id=excluded.launcher_id""",
                (account_id, launcher_id))
            db.commit()
