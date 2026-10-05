"""Durable two-phase SYNC transaction; provider launch remains capability gated."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from audapack.config import get_state_dir
from audapack.limits import parse_time
from audapack.provider_capabilities import ProviderCapabilities, WindowStartSemantics


@dataclass(frozen=True)
class PreparedSyncMember:
    prepared_id: str
    member_index: int
    account_id: str
    launcher_id: str
    model: str = ""
    effort: str = ""
    payload_policy: str = ""
    payload_config: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class SyncMemberSpec:
    account_id: str
    launcher_id: str
    provider_id: str
    model: str
    effort: str
    payload_sha256: str


@dataclass(frozen=True)
class SyncPreflight:
    proofs: frozenset[str] = frozenset()
    reason: str = ""

    REQUIRED = frozenset({
        "account_exists", "launcher_bound", "project_valid", "provider_available",
        "quota_bucket_correct", "model_supported", "effort_supported",
        "prepared_delivery_supported", "process_capacity_available", "payload_resolved",
    })

    @property
    def ready(self) -> bool:
        return self.REQUIRED <= self.proofs


@dataclass(frozen=True)
class SyncSpawn:
    process_id: int
    spawned_at: str
    process_token: int | None = None

    def __post_init__(self) -> None:
        if self.process_id <= 0 or parse_time(self.spawned_at) is None:
            raise ValueError("SYNC spawn requires a PID and timezone-aware timestamp")


class SyncStore:
    """One group per prepared event; release claims are never replayed."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_state_dir() / "resources.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS sync_groups (
                sync_group_id TEXT PRIMARY KEY, prepared_id TEXT NOT NULL,
                trigger_event_id TEXT NOT NULL, target_window TEXT NOT NULL,
                requested_at TEXT NOT NULL, state TEXT NOT NULL,
                release_policy TEXT NOT NULL, barrier_at TEXT NOT NULL DEFAULT '',
                process_skew_ms REAL, quota_skew_ms REAL, result TEXT NOT NULL DEFAULT '',
                UNIQUE(prepared_id,trigger_event_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS sync_members (
                sync_group_id TEXT NOT NULL, account_id TEXT NOT NULL,
                launcher_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                model TEXT NOT NULL, effort TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                preflight_state TEXT NOT NULL DEFAULT 'PENDING',
                state TEXT NOT NULL DEFAULT 'PENDING', release_at TEXT NOT NULL DEFAULT '',
                process_id INTEGER, observed_window_start TEXT NOT NULL DEFAULT '',
                observed_reset TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(sync_group_id,account_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(sync_members)")}
            if "process_token" not in columns:
                db.execute("ALTER TABLE sync_members ADD COLUMN process_token INTEGER")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, prepared_id: str, trigger_event_id: str, target_window: str,
               release_policy: str, members: list[SyncMemberSpec],
               requested_at: datetime) -> str:
        if not prepared_id or not trigger_event_id or not target_window or not release_policy:
            raise ValueError("SYNC group identity, window, and policy required")
        if len(members) < 2 or len({item.account_id for item in members}) != len(members):
            raise ValueError("SYNC requires at least two distinct accounts")
        if any(not all((m.account_id, m.launcher_id, m.provider_id, m.payload_sha256))
               for m in members):
            raise ValueError("SYNC member identity and payload required")
        if requested_at.tzinfo is None:
            raise ValueError("SYNC request time must include timezone")
        group_id = uuid.uuid4().hex
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT sync_group_id,target_window,release_policy
                FROM sync_groups WHERE prepared_id=? AND trigger_event_id=?""",
                (prepared_id, trigger_event_id)).fetchone()
            if existing:
                prior = self._members(db, existing[0])
                if (existing[1], existing[2], prior) != (
                    target_window, release_policy,
                    sorted((asdict(m) for m in members), key=lambda item: item["account_id"]),
                ):
                    db.rollback()
                    raise ValueError("SYNC event already has different members or policy")
                db.rollback()
                return existing[0]
            db.execute("""INSERT INTO sync_groups
                (sync_group_id,prepared_id,trigger_event_id,target_window,requested_at,state,release_policy)
                VALUES (?,?,?,?,?,'PENDING',?)""",
                (group_id, prepared_id, trigger_event_id, target_window,
                 requested_at.astimezone(timezone.utc).isoformat(), release_policy))
            db.executemany("""INSERT INTO sync_members
                (sync_group_id,account_id,launcher_id,provider_id,model,effort,payload_sha256)
                VALUES (?,?,?,?,?,?,?)""",
                [(group_id, m.account_id, m.launcher_id, m.provider_id, m.model,
                  m.effort, m.payload_sha256) for m in members])
            db.commit()
        return group_id

    @staticmethod
    def _members(db: sqlite3.Connection, group_id: str) -> list[dict]:
        rows = db.execute("""SELECT account_id,launcher_id,provider_id,model,effort,payload_sha256
            FROM sync_members WHERE sync_group_id=? ORDER BY account_id""", (group_id,)).fetchall()
        keys = ("account_id", "launcher_id", "provider_id", "model", "effort", "payload_sha256")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def get(self, group_id: str) -> dict | None:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            group = db.execute("SELECT * FROM sync_groups WHERE sync_group_id=?", (group_id,)).fetchone()
            if group is None:
                return None
            members = db.execute("""SELECT * FROM sync_members WHERE sync_group_id=?
                ORDER BY account_id""", (group_id,)).fetchall()
        return {**dict(group), "members": [dict(item) for item in members]}

    def get_by_event(self, prepared_id: str, trigger_event_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT sync_group_id FROM sync_groups WHERE prepared_id=? AND trigger_event_id=?",
                (prepared_id, trigger_event_id),
            ).fetchone()
            if row is None:
                return None
            group_id = row[0]
        return self.get(group_id)

    def record_preflight(self, group_id: str, outcomes: dict[str, SyncPreflight]) -> bool:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM sync_groups WHERE sync_group_id=?",
                             (group_id,)).fetchone()
            members = self._members(db, group_id)
            accounts = {item["account_id"] for item in members}
            if row is None or row[0] not in {"PENDING", "WAITING"} or set(outcomes) != accounts:
                db.rollback()
                return False
            ready = all(outcomes[account].ready for account in accounts)
            db.executemany("""UPDATE sync_members SET preflight_state=?,result=?
                WHERE sync_group_id=? AND account_id=?""",
                [("READY" if outcome.ready else "BLOCKED", outcome.reason[:240],
                  group_id, account) for account, outcome in outcomes.items()])
            db.execute("UPDATE sync_groups SET state=?,result=? WHERE sync_group_id=?",
                       ("READY" if ready else "WAITING",
                        "" if ready else "one or more members failed preflight", group_id))
            db.commit()
        return ready

    def claim_release(self, group_id: str, barrier_at: datetime) -> list[SyncMemberSpec] | None:
        if barrier_at.tzinfo is None:
            raise ValueError("SYNC barrier time must include timezone")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM sync_groups WHERE sync_group_id=?",
                             (group_id,)).fetchone()
            members = self._members(db, group_id)
            if row is None or row[0] != "READY" or len(members) < 2:
                db.rollback()
                return None
            db.execute("UPDATE sync_groups SET state='RELEASING',barrier_at=? WHERE sync_group_id=?",
                       (barrier_at.astimezone(timezone.utc).isoformat(), group_id))
            db.execute("""UPDATE sync_members SET state='RELEASING'
                WHERE sync_group_id=? AND preflight_state='READY'""", (group_id,))
            db.commit()
        return [SyncMemberSpec(**item) for item in members]

    def record_spawn(self, group_id: str, account_id: str,
                     spawn: SyncSpawn | None, result: str = "") -> bool:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE sync_members SET state=?,release_at=?,
                process_id=?,process_token=COALESCE(?,process_token),result=?
                WHERE sync_group_id=? AND account_id=?
                AND state='RELEASING'""",
                ("LAUNCHED" if spawn else "UNCERTAIN",
                 spawn.spawned_at if spawn else "", spawn.process_id if spawn else None,
                 spawn.process_token if spawn else None,
                 result[:240], group_id, account_id)).rowcount
            if changed:
                rows = db.execute("SELECT state,release_at FROM sync_members WHERE sync_group_id=?",
                                  (group_id,)).fetchall()
                if all(row[0] != "RELEASING" for row in rows):
                    state = "LAUNCHED" if all(row[0] == "LAUNCHED" for row in rows) else "PARTIAL"
                    times = [parse_time(row[1]) for row in rows if row[1]]
                    skew = ((max(times) - min(times)).total_seconds() * 1000
                            if len(times) >= 2 else None)
                    db.execute("""UPDATE sync_groups SET state=?,process_skew_ms=?
                        WHERE sync_group_id=?""", (state, skew, group_id))
            db.commit()
        return changed == 1

    def recover_interrupted_release(self, group_id: str) -> bool:
        """Fence a crashed release; an uncertain member is never launched again."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE sync_groups SET state='PARTIAL',
                result='release interrupted; operator recovery required'
                WHERE sync_group_id=? AND state='RELEASING'""", (group_id,)).rowcount
            if changed:
                db.execute("""UPDATE sync_members SET state='UNCERTAIN',
                    result='release outcome uncertain' WHERE sync_group_id=?
                    AND state='RELEASING'""", (group_id,))
            db.commit()
        return changed == 1

    def record_window(self, group_id: str, account_id: str,
                      started_at: str, reset_at: str = "") -> bool:
        if parse_time(started_at) is None:
            raise ValueError("provider window start required")
        parse_time(reset_at)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE sync_members SET observed_window_start=?,
                observed_reset=? WHERE sync_group_id=? AND account_id=? AND state='LAUNCHED'""",
                (started_at, reset_at, group_id, account_id)).rowcount
            if changed:
                rows = db.execute("""SELECT observed_window_start FROM sync_members
                    WHERE sync_group_id=? AND state='LAUNCHED'""", (group_id,)).fetchall()
                starts = [parse_time(row[0]) for row in rows if row[0]]
                skew = ((max(starts) - min(starts)).total_seconds() * 1000
                        if len(starts) >= 2 else None)
                db.execute("UPDATE sync_groups SET quota_skew_ms=? WHERE sync_group_id=?",
                           (skew, group_id))
            db.commit()
        return changed == 1


class SyncCoordinator:
    """No process begins until all members pass capability and local preflight."""

    def __init__(self, store: SyncStore,
                 capability: Callable[[SyncMemberSpec], ProviderCapabilities | None],
                 preflight: Callable[[SyncMemberSpec], SyncPreflight],
                 launch: Callable[[SyncMemberSpec], SyncSpawn]) -> None:
        self.store = store
        self.capability = capability
        self.preflight = preflight
        self.launch = launch

    def prepare(self, group_id: str) -> bool:
        group = self.store.get(group_id)
        if group is None or group["state"] not in {"PENDING", "WAITING"}:
            return False
        outcomes = {}
        for row in group["members"]:
            member = SyncMemberSpec(**{key: row[key] for key in SyncMemberSpec.__dataclass_fields__})
            caps = self.capability(member)
            if (caps is None or not caps.supports_sync or not caps.supports_prepared_prompt or
                    caps.window_start_semantics is not WindowStartSemantics.FIRST_CONSUMING_USE):
                outcomes[member.account_id] = SyncPreflight(
                    reason="provider window is not sync-capable")
                continue
            if (member.model and not caps.supports_model_selection or
                    member.effort and not caps.supports_effort_selection):
                outcomes[member.account_id] = SyncPreflight(
                    reason="model or effort selection unsupported")
                continue
            try:
                outcomes[member.account_id] = self.preflight(member)
            except Exception as exc:
                outcomes[member.account_id] = SyncPreflight(reason=type(exc).__name__)
        return self.store.record_preflight(group_id, outcomes)

    def release(self, group_id: str, barrier_at: datetime) -> dict | None:
        members = self.store.claim_release(group_id, barrier_at)
        if members is None:
            return None
        barrier = threading.Barrier(len(members))

        def one(member: SyncMemberSpec) -> None:
            try:
                barrier.wait(timeout=10)
                spawn = self.launch(member)
            except Exception as exc:
                self.store.record_spawn(group_id, member.account_id, None,
                                        f"launch outcome uncertain: {type(exc).__name__}")
            else:
                self.store.record_spawn(group_id, member.account_id, spawn)

        with ThreadPoolExecutor(max_workers=len(members), thread_name_prefix="prepared-sync") as pool:
            list(pool.map(one, members))
        return self.store.get(group_id)
