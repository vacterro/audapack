"""Durable prepared jobs, trigger identity, and cross-process execution claims."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable

from audapack.config import get_state_dir
from audapack.limits import Availability, LimitSnapshot, parse_time, utc_now
from audapack.prepared_sync import PreparedSyncMember


class Trigger(str, Enum):
    ON_TIME = "ON_TIME"
    ON_RESET = "ON_RESET"
    SYNC = "SYNC"
    PRIME = "PRIME"


class Payload(str, Enum):
    LATEST_SAIHANDOFF = "LATEST_SAIHANDOFF"
    PINNED_SAIHANDOFF = "PINNED_SAIHANDOFF"
    USER_COMMAND = "USER_COMMAND"
    AUDIT = "AUDIT"
    STATIC_PROMPT = "STATIC_PROMPT"


class JobState(str, Enum):
    DRAFT = "DRAFT"
    ARMED = "ARMED"
    WAITING_TRIGGER = "WAITING_TRIGGER"
    WAITING_LIMIT = "WAITING_LIMIT"
    CLAIMED = "CLAIMED"
    PREPARING = "PREPARING"
    LAUNCHING = "LAUNCHING"
    DELIVERING = "DELIVERING"
    VERIFYING = "VERIFYING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class PreparedJob:
    prepared_id: str
    name: str
    project_id: str
    launcher_id: str
    account_id: str
    trigger: Trigger
    payload: Payload
    payload_config: dict
    trigger_config: dict
    model: str = ""
    effort: str = ""
    enabled: bool = False
    state: JobState = JobState.DRAFT
    recurrence: str = "ONE_SHOT"
    safety_delay_seconds: int = 60
    catch_up_seconds: int = 900
    created_at: str = ""
    updated_at: str = ""
    next_due_at: str = ""
    waiting_reason: str = ""

    def __post_init__(self) -> None:
        if self.safety_delay_seconds < 0 or self.catch_up_seconds < 0:
            raise ValueError("negative scheduling delay")
        if self.recurrence not in ("ONE_SHOT", "EVERY_RESET"):
            raise ValueError("unsupported recurrence")
        if self.recurrence == "EVERY_RESET" and self.trigger != Trigger.ON_RESET:
            raise ValueError("EVERY_RESET requires ON_RESET")
        if self.trigger in (Trigger.ON_TIME, Trigger.PRIME, Trigger.SYNC):
            parse_time(self.trigger_config.get("at"))
        parse_time(self.next_due_at)


@dataclass(frozen=True)
class TriggerDecision:
    state: JobState
    event_id: str = ""
    due_at: str = ""
    reason: str = ""


def _event_id(job: PreparedJob, discriminator: str) -> str:
    body = f"{job.prepared_id}\0{discriminator}".encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:24]


def evaluate_trigger(job: PreparedJob, snapshot: LimitSnapshot | None, now: datetime,
                     *, quota_bucket: str | None = None) -> TriggerDecision:
    """Pure scheduling decision; safe to repeat after clock jumps and restarts."""
    bucket = quota_bucket or job.trigger_config.get("quota_bucket")
    if not job.enabled:
        return TriggerDecision(JobState.DISABLED, reason="job disarmed")
    if job.trigger in (Trigger.ON_TIME, Trigger.PRIME, Trigger.SYNC):
        at = parse_time(job.trigger_config.get("at"))
        if at is None:
            return TriggerDecision(JobState.FAILED_TERMINAL, reason="missing time")
        event = _event_id(job, f"{job.trigger.value}:{at.isoformat()}")
        if now < at:
            return TriggerDecision(JobState.WAITING_TRIGGER, event, at.isoformat(), "time not reached")
        if now > at + timedelta(seconds=job.catch_up_seconds):
            return TriggerDecision(JobState.MISSED, event, at.isoformat(), "catch-up deadline passed")
        # A browser AUDIT does not consume the measured CLI 5h/weekly bucket,
        # so gating it on CLI limit availability would be a false gate: the
        # audit's own account/browser binding is proven separately by the
        # PreparedAuditRuntime. Claim on time, like PRIME/SYNC.
        if job.trigger in (Trigger.PRIME, Trigger.SYNC) or job.payload == Payload.AUDIT:
            return TriggerDecision(JobState.CLAIMED, event, at.isoformat())
        availability = snapshot.availability(now, quota_bucket=bucket) if snapshot else Availability.UNKNOWN
        if availability in (Availability.AVAILABLE, Availability.LOW):
            return TriggerDecision(JobState.CLAIMED, event, at.isoformat())
        if job.trigger_config.get("availability_policy") == "STRICT_TIME":
            return TriggerDecision(JobState.MISSED, event, at.isoformat(), f"account {availability.value}")
        return TriggerDecision(JobState.WAITING_LIMIT, event, at.isoformat(), f"account {availability.value}")

    if job.trigger != Trigger.ON_RESET:
        return TriggerDecision(JobState.FAILED_TERMINAL, reason="unknown trigger")
    if snapshot is None or snapshot.availability(now, quota_bucket=bucket) in (Availability.ERROR, Availability.STALE):
        return TriggerDecision(JobState.WAITING_LIMIT, reason="limit state unavailable")
    armed_reset = job.trigger_config.get("armed_reset_at")
    armed_window = job.trigger_config.get("armed_window_id")
    if not armed_reset or not armed_window:
        return TriggerDecision(JobState.WAITING_LIMIT, reason="reset event not armed")
    reset = parse_time(armed_reset)
    due = reset + timedelta(seconds=job.safety_delay_seconds)
    event = _event_id(job, f"{job.account_id}:{armed_window}:{armed_reset}")
    if now < due:
        return TriggerDecision(JobState.WAITING_TRIGGER, event, due.isoformat(), "reset safety delay")
    if now > due + timedelta(seconds=job.catch_up_seconds):
        return TriggerDecision(JobState.MISSED, event, due.isoformat(), "reset catch-up deadline passed")
    availability = snapshot.availability(now, quota_bucket=bucket)
    if availability in (Availability.AVAILABLE, Availability.LOW):
        return TriggerDecision(JobState.CLAIMED, event, due.isoformat())
    # A pre-reset observation becomes RESET_PENDING when the clock passes its
    # reset. One cheap verification must replace it before launch.
    return TriggerDecision(JobState.WAITING_LIMIT, event, due.isoformat(), f"verification required: {availability.value}")


def reconcile_reset(job: PreparedJob, snapshot: LimitSnapshot | None, now: datetime,
                    *, quota_bucket: str | None = None) -> PreparedJob:
    """Arm or move a reset timer from authoritative observed windows."""
    if job.trigger != Trigger.ON_RESET or snapshot is None or snapshot.error:
        return job
    selected = job.trigger_config.get("window_id", "NEXT_USABLE")
    bucket = quota_bucket or job.trigger_config.get("quota_bucket")
    windows = [w for w in snapshot.relevant_windows(bucket)
               if w.is_hard_limit and w.reset_at]
    if selected != "NEXT_USABLE":
        windows = [w for w in windows if w.window_id == selected]
    if not windows:
        return job
    exhausted = [w for w in windows if w.remaining_ratio == 0 or w.remaining_units == 0]
    target = (max(exhausted, key=lambda w: parse_time(w.reset_at)) if exhausted
              else min(windows, key=lambda w: parse_time(w.reset_at)))
    old_reset = parse_time(job.trigger_config.get("armed_reset_at"))
    # Once an old reset passed and fresh provider state is available, keep the
    # old event until it gets a receipt. The provider naturally reports the
    # *next* reset now; replacing it here would silently lose the due event.
    if old_reset and now >= old_reset and snapshot.availability(now, quota_bucket=bucket) in (Availability.AVAILABLE, Availability.LOW):
        return job
    if job.trigger_config.get("armed_reset_at") == target.reset_at and job.trigger_config.get("armed_window_id") == target.window_id:
        return job
    config = dict(job.trigger_config)
    config["armed_reset_at"] = target.reset_at
    config["armed_window_id"] = target.window_id
    config.pop("verification_attempts", None)
    config.pop("next_verification_at", None)
    config.pop("verified_event", None)
    due = parse_time(target.reset_at) + timedelta(seconds=job.safety_delay_seconds)
    return replace(job, trigger_config=config, next_due_at=due.isoformat(),
                   waiting_reason=f"reset observed: {target.window_id} at {target.reset_at}")


#: W2-003: executions in these states are provably finished and reconciled, so
#: their diagnostic payload may be compacted. FAILED_RETRYABLE and
#: RECOVERY_REQUIRED are deliberately absent.
PREPARED_TERMINAL_STATES = ("DONE", "FAILED_TERMINAL", "MISSED", "CANCELLED")

#: Terminal executions kept in full PER JOB for diagnostics; older ones become
#: identity tombstones.
PREPARED_DIAGNOSTIC_RETENTION = 12


class PreparedStore:
    LEASE_SECONDS = 120

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_state_dir() / "resources.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS prepared_jobs (
                prepared_id TEXT PRIMARY KEY, body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""CREATE TABLE IF NOT EXISTS prepared_executions (
                execution_id TEXT PRIMARY KEY, prepared_id TEXT NOT NULL,
                trigger_event_id TEXT NOT NULL, state TEXT NOT NULL,
                claimed_at TEXT NOT NULL, owner_id TEXT NOT NULL,
                process_id INTEGER, delivery_hash TEXT, result TEXT NOT NULL DEFAULT '',
                UNIQUE(prepared_id, trigger_event_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(prepared_executions)")}
            for name, definition in (
                ("claim_generation", "INTEGER NOT NULL DEFAULT 1"),
                ("attempt_number", "INTEGER NOT NULL DEFAULT 1"),
                ("lease_expires_at", "TEXT"),
                ("heartbeat_at", "TEXT"),
                ("process_token", "INTEGER"),
                ("account_id", "TEXT"),
                ("launcher_id", "TEXT"),
                ("project_id", "TEXT"),
                ("is_test", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE prepared_executions ADD COLUMN {name} {definition}")
            db.execute("""UPDATE prepared_executions SET lease_expires_at=claimed_at
                WHERE lease_expires_at IS NULL AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING')""")
            if "selection_reason" not in columns:
                db.execute("ALTER TABLE prepared_executions ADD COLUMN selection_reason TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS prepared_account_reservations (
                account_id TEXT PRIMARY KEY, owner_execution TEXT NOT NULL,
                expires_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS prepared_sync_members (
                prepared_id TEXT NOT NULL, member_index INTEGER NOT NULL,
                account_id TEXT NOT NULL, launcher_id TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', effort TEXT NOT NULL DEFAULT '',
                payload_policy TEXT NOT NULL DEFAULT '', payload_config TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(prepared_id, member_index),
                UNIQUE(prepared_id, account_id))""")
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _encode(job: PreparedJob) -> str:
        return json.dumps(asdict(job), separators=(",", ":"))

    @staticmethod
    def _decode(body: str) -> PreparedJob:
        data = json.loads(body)
        data["trigger"] = Trigger(data["trigger"])
        data["payload"] = Payload(data["payload"])
        data["state"] = JobState(data["state"])
        return PreparedJob(**data)

    def save(self, job: PreparedJob) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                """SELECT 1 FROM prepared_executions WHERE prepared_id=? AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING',
                 'RECOVERY_REQUIRED')""",
                (job.prepared_id,),
            ).fetchone()
            if active:
                db.rollback()
                raise RuntimeError("cannot edit job during active execution")
            db.execute("""INSERT INTO prepared_jobs VALUES (?, ?, 1)
                ON CONFLICT(prepared_id) DO UPDATE SET body=excluded.body,
                revision=prepared_jobs.revision+1""", (job.prepared_id, self._encode(job)))
            db.commit()

    def get(self, prepared_id: str) -> PreparedJob | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT body FROM prepared_jobs WHERE prepared_id=?", (prepared_id,)).fetchone()
        return self._decode(row[0]) if row else None

    def list(self) -> list[PreparedJob]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT body FROM prepared_jobs ORDER BY prepared_id").fetchall()
        return [self._decode(row[0]) for row in rows]

    def _reserve_account(self, db: sqlite3.Connection, account_id: str,
                         execution_id: str, now: datetime, reason: str) -> bool:
        current = db.execute("""SELECT owner_execution,expires_at
            FROM prepared_account_reservations WHERE account_id=?""",
            (account_id,)).fetchone()
        if current and current[0] != execution_id and current[1] > now.isoformat():
            return False
        db.execute("""INSERT INTO prepared_account_reservations
            (account_id,owner_execution,expires_at,reason) VALUES (?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET owner_execution=excluded.owner_execution,
            expires_at=excluded.expires_at,reason=excluded.reason""",
            (account_id, execution_id,
             (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(), reason[:240]))
        return True

    def reservations(self, now: datetime | None = None) -> dict[str, dict[str, str]]:
        now = now or utc_now()
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT account_id,owner_execution,expires_at,reason
                FROM prepared_account_reservations WHERE expires_at>?""",
                (now.isoformat(),)).fetchall()
        return {row[0]: {"owner_execution": row[1], "expires_at": row[2],
                         "reason": row[3]} for row in rows}

    @staticmethod
    def _get_sync_members_db(db: sqlite3.Connection, prepared_id: str) -> list[PreparedSyncMember]:
        rows = db.execute(
            """SELECT prepared_id, member_index, account_id, launcher_id, model, effort,
                      payload_policy, payload_config, enabled
            FROM prepared_sync_members WHERE prepared_id=? ORDER BY member_index""",
            (prepared_id,),
        ).fetchall()
        result = []
        for r in rows:
            cfg = json.loads(r[7]) if r[7] else {}
            result.append(PreparedSyncMember(
                prepared_id=r[0],
                member_index=r[1],
                account_id=r[2],
                launcher_id=r[3],
                model=r[4],
                effort=r[5],
                payload_policy=r[6],
                payload_config=cfg,
                enabled=bool(r[8]),
            ))
        return result

    def get_sync_members(self, prepared_id: str) -> list[PreparedSyncMember]:
        with closing(self._connect()) as db:
            return self._get_sync_members_db(db, prepared_id)

    def save_sync_members(self, prepared_id: str, members: list[PreparedSyncMember]) -> None:
        if not prepared_id:
            raise ValueError("prepared_id required")
        if len(members) < 2:
            raise ValueError("SYNC requires at least two distinct accounts")
        account_ids = [m.account_id for m in members]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("SYNC requires distinct accounts")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM prepared_sync_members WHERE prepared_id=?", (prepared_id,))
            for idx, m in enumerate(members):
                payload_cfg = json.dumps(m.payload_config) if isinstance(m.payload_config, dict) else "{}"
                db.execute(
                    """INSERT INTO prepared_sync_members
                    (prepared_id, member_index, account_id, launcher_id, model, effort,
                     payload_policy, payload_config, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (prepared_id, idx, m.account_id, m.launcher_id, m.model or "",
                     m.effort or "", m.payload_policy or "", payload_cfg, 1 if m.enabled else 0),
                )
            db.commit()

    def delete_sync_members(self, prepared_id: str) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM prepared_sync_members WHERE prepared_id=?", (prepared_id,))
            db.commit()

    def claim(self, prepared_id: str, event_id: str, owner_id: str,
              now: datetime | None = None, *, selected_account_id: str = "",
              selected_launcher_id: str = "", selection_reason: str = "") -> str | None:
        """Atomically claim one event. No expired lease can duplicate a side effect."""
        if not event_id:
            raise ValueError("empty trigger event")
        now = now or utc_now()
        execution_id = uuid.uuid4().hex
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT body FROM prepared_jobs WHERE prepared_id=?", (prepared_id,)).fetchone()
            if not job or not self._decode(job[0]).enabled:
                db.rollback()
                return None
            parsed_job = self._decode(job[0])
            if parsed_job.account_id == "AUTO":
                if not selected_account_id or not selected_launcher_id or selected_account_id == "AUTO":
                    db.rollback()
                    return None
                account_id, launcher_id = selected_account_id, selected_launcher_id
            else:
                account_id, launcher_id = parsed_job.account_id, parsed_job.launcher_id
                if ((selected_account_id and selected_account_id != account_id) or
                        (selected_launcher_id and selected_launcher_id != launcher_id)):
                    db.rollback()
                    return None
            active_test = db.execute("""SELECT 1 FROM prepared_executions
                WHERE prepared_id=? AND is_test=1 AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING',
                 'RECOVERY_REQUIRED')""", (prepared_id,)).fetchone()
            if active_test:
                db.rollback()
                return None
            prior = db.execute("""SELECT execution_id,trigger_event_id,state,claim_generation,
                attempt_number,lease_expires_at,process_id,delivery_hash,
                account_id,launcher_id,project_id
                FROM prepared_executions WHERE prepared_id=? AND is_test=0""",
                (prepared_id,)).fetchall()
            active_states = {"CLAIMED", "PREPARING", "LAUNCHING", "DELIVERING",
                             "VERIFYING", "RUNNING", "RECOVERY_REQUIRED"}
            if any(row[2] in active_states for row in prior):
                db.rollback()
                return None
            retry = next((row for row in prior if row[1] == event_id and
                          row[2] == JobState.FAILED_RETRYABLE.value and
                          row[6] is None and row[7] is None), None)
            if (parsed_job.recurrence == "ONE_SHOT" and prior and
                    (retry is None or any(row[0] != retry[0] for row in prior))):
                db.rollback()
                return None
            if retry:
                if parsed_job.account_id != "AUTO" and (retry[8], retry[9], retry[10]) != (
                    account_id, launcher_id, parsed_job.project_id):
                    db.rollback()
                    return None
                if retry[5] and retry[5] > now.isoformat():
                    db.rollback()
                    return None
                if not self._reserve_account(db, account_id, retry[0], now,
                                             selection_reason or "prepared retry"):
                    db.rollback()
                    return None
                if parsed_job.trigger == Trigger.SYNC:
                    for sm in self._get_sync_members_db(db, prepared_id):
                        if sm.enabled and not self._reserve_account(
                            db, sm.account_id, retry[0], now,
                            selection_reason or "prepared sync retry"
                        ):
                            db.rollback()
                            return None
                if retry[8] != account_id:
                    db.execute("""DELETE FROM prepared_account_reservations
                        WHERE account_id=? AND owner_execution=?""", (retry[8], retry[0]))
                db.execute("""UPDATE prepared_executions SET state=?,owner_id=?,
                    claim_generation=?,attempt_number=?,claimed_at=?,heartbeat_at=?,
                    lease_expires_at=?,account_id=?,launcher_id=?,selection_reason=?,
                    result='' WHERE execution_id=?""",
                    (JobState.CLAIMED.value, owner_id, retry[3] + 1, retry[4] + 1,
                     now.isoformat(), now.isoformat(),
                     (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                     account_id, launcher_id, selection_reason[:240], retry[0]))
                db.execute("UPDATE prepared_jobs SET body=?,revision=revision+1 WHERE prepared_id=?",
                           (self._encode(replace(parsed_job, state=JobState.CLAIMED,
                                                 waiting_reason="")), prepared_id))
                db.commit()
                return retry[0]
            if any(row[1] == event_id for row in prior):
                db.rollback()
                return None
            if not self._reserve_account(db, account_id, execution_id, now,
                                         selection_reason or "prepared launch"):
                db.rollback()
                return None
            if parsed_job.trigger == Trigger.SYNC:
                for sm in self._get_sync_members_db(db, prepared_id):
                    if sm.enabled and not self._reserve_account(
                        db, sm.account_id, execution_id, now,
                        selection_reason or "prepared sync launch"
                    ):
                        db.rollback()
                        return None
            try:
                db.execute(
                    """INSERT INTO prepared_executions
                    (execution_id,prepared_id,trigger_event_id,state,claimed_at,owner_id,
                     lease_expires_at,heartbeat_at,account_id,launcher_id,project_id,
                     selection_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (execution_id, prepared_id, event_id, JobState.CLAIMED.value,
                     now.isoformat(), owner_id,
                     (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(), now.isoformat(),
                     account_id, launcher_id, parsed_job.project_id,
                     selection_reason[:240]),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                return None
            db.execute(
                "UPDATE prepared_jobs SET body=?, revision=revision+1 WHERE prepared_id=?",
                (self._encode(replace(self._decode(job[0]), state=JobState.CLAIMED)), prepared_id),
            )
            db.commit()
        return execution_id

    def claim_test(self, prepared_id: str, owner_id: str,
                   now: datetime | None = None) -> tuple[str, str] | None:
        """Claim an isolated manual test without spending the scheduled event."""
        now = now or utc_now()
        event_id = f"manual-test:{uuid.uuid4().hex}"
        execution_id = uuid.uuid4().hex
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM prepared_jobs WHERE prepared_id=?",
                             (prepared_id,)).fetchone()
            if row is None:
                db.rollback()
                return None
            job = self._decode(row[0])
            if not job.enabled:
                db.rollback()
                return None
            active = db.execute("""SELECT 1 FROM prepared_executions
                WHERE prepared_id=? AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING',
                 'RECOVERY_REQUIRED')""", (prepared_id,)).fetchone()
            if active:
                db.rollback()
                return None
            if job.account_id == "AUTO" or not self._reserve_account(
                db, job.account_id, execution_id, now, "manual Test Now"
            ):
                db.rollback()
                return None
            db.execute("""INSERT INTO prepared_executions
                (execution_id,prepared_id,trigger_event_id,state,claimed_at,owner_id,
                 lease_expires_at,heartbeat_at,account_id,launcher_id,project_id,is_test)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                (execution_id, prepared_id, event_id, JobState.CLAIMED.value,
                 now.isoformat(), owner_id,
                 (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(), now.isoformat(),
                 job.account_id, job.launcher_id, job.project_id))
            db.commit()
        return execution_id, event_id

    def settle_without_launch(self, prepared_id: str, event_id: str, owner_id: str,
                              state: JobState, reason: str, now: datetime | None = None) -> bool:
        """Persist a missed trigger once, without ever claiming a CLI launch."""
        if state != JobState.MISSED or not event_id:
            raise ValueError("only a missed trigger can settle without launch")
        now = now or utc_now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM prepared_jobs WHERE prepared_id=?",
                             (prepared_id,)).fetchone()
            if row is None:
                db.rollback()
                return False
            job = self._decode(row[0])
            if not job.enabled:
                db.rollback()
                return False
            active_test = db.execute("""SELECT 1 FROM prepared_executions
                WHERE prepared_id=? AND is_test=1 AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING',
                 'RECOVERY_REQUIRED')""", (prepared_id,)).fetchone()
            if active_test:
                db.rollback()
                return False
            try:
                db.execute(
                    """INSERT INTO prepared_executions
                    (execution_id,prepared_id,trigger_event_id,state,claimed_at,owner_id,result)
                    VALUES (?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, prepared_id, event_id, state.value,
                     now.isoformat(), owner_id, reason[:240]),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                return False
            if job.trigger == Trigger.ON_RESET and job.recurrence == "EVERY_RESET":
                config = dict(job.trigger_config)
                config.pop("armed_reset_at", None)
                config.pop("armed_window_id", None)
                job = replace(job, state=JobState.ARMED, trigger_config=config,
                              next_due_at="", waiting_reason="next reset not observed")
            else:
                job = replace(job, enabled=False, state=state, waiting_reason=reason)
            db.execute("UPDATE prepared_jobs SET body=?, revision=revision+1 WHERE prepared_id=?",
                       (self._encode(job), prepared_id))
            db.commit()
        return True

    def advance(
        self, execution_id: str, owner_id: str, generation: int,
        expected: JobState, state: JobState,
        *, process_id: int | None = None, process_token: int | None = None,
        delivery_hash: str | None = None, result: str = "", now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT prepared_id,is_test,account_id FROM prepared_executions WHERE execution_id=?
                AND owner_id=? AND claim_generation=? AND state=? AND lease_expires_at>?""",
                (execution_id, owner_id, generation, expected.value, now.isoformat()),
            ).fetchone()
            if current is None:
                db.rollback()
                return False
            changed = db.execute(
                """UPDATE prepared_executions SET state=?, process_id=COALESCE(?,process_id),
                process_token=COALESCE(?,process_token), delivery_hash=COALESCE(?,delivery_hash),
                result=?, heartbeat_at=?, lease_expires_at=?
                WHERE execution_id=? AND owner_id=? AND claim_generation=? AND state=?""",
                (state.value, process_id, process_token, delivery_hash, result[:240],
                 now.isoformat(), (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                 execution_id, owner_id, generation, expected.value),
            ).rowcount
            if changed:
                if state in (JobState.DELIVERING, JobState.VERIFYING, JobState.RUNNING,
                             JobState.DONE, JobState.FAILED_RETRYABLE,
                             JobState.FAILED_TERMINAL, JobState.MISSED,
                             JobState.CANCELLED, JobState.RECOVERY_REQUIRED):
                    db.execute("""DELETE FROM prepared_account_reservations
                        WHERE owner_execution=?""",
                        (execution_id,))
                else:
                    db.execute("""UPDATE prepared_account_reservations SET expires_at=?
                        WHERE owner_execution=?""",
                        ((now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                         execution_id))
            if changed and not current[1]:
                row = db.execute("SELECT body FROM prepared_jobs WHERE prepared_id=?", (current[0],)).fetchone()
                if row:
                    job = self._decode(row[0])
                    terminal = state in (JobState.DONE, JobState.FAILED_TERMINAL, JobState.MISSED, JobState.CANCELLED)
                    if terminal and job.recurrence == "EVERY_RESET" and state == JobState.DONE:
                        config = dict(job.trigger_config)
                        config.pop("armed_reset_at", None)
                        config.pop("armed_window_id", None)
                        job = replace(job, state=JobState.ARMED, trigger_config=config,
                                      next_due_at="", waiting_reason="next reset not observed")
                    else:
                        job = replace(job, state=state, enabled=job.enabled and not terminal,
                                      waiting_reason=result[:240] if state in (
                                          JobState.FAILED_RETRYABLE, JobState.FAILED_TERMINAL,
                                          JobState.RECOVERY_REQUIRED,
                                      ) else "")
                    db.execute("UPDATE prepared_jobs SET body=?, revision=revision+1 WHERE prepared_id=?",
                               (self._encode(job), current[0]))
            db.commit()
        return changed == 1

    def heartbeat(self, execution_id: str, owner_id: str, generation: int,
                  now: datetime | None = None) -> bool:
        now = now or utc_now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE prepared_executions
                SET heartbeat_at=?, lease_expires_at=?
                WHERE execution_id=? AND owner_id=? AND claim_generation=?
                AND lease_expires_at>? AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING')""",
                (now.isoformat(), (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                 execution_id, owner_id, generation, now.isoformat())).rowcount
            if changed:
                db.execute("""UPDATE prepared_account_reservations SET expires_at=?
                    WHERE owner_execution=?""",
                    ((now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(), execution_id))
            db.commit()
        return changed == 1

    def compact_executions(self, keep: int = PREPARED_DIAGNOSTIC_RETENTION) -> int:
        """Strip diagnostic payload from terminal executions outside the window.

        W2-003 (audit/12.md): a recurring EVERY_RESET job left one terminal row
        per completed event forever -- 40 rows for 40 events, with the job back
        at ARMED and nothing active or recoverable. The identity is what a replay
        needs (`claim()` refuses a repeated `trigger_event_id`, and a ONE_SHOT
        job refuses any second claim at all), while the delivery hash, the lease
        and the result text are what actually grow. Those are dropped; the row
        stays, so a replay is still rejected or adopted.

        Active, retryable and RECOVERY_REQUIRED (ambiguous) rows are never
        touched: nothing here can tell an ambiguous execution from a finished
        one, and guessing would lose a run that needs an operator.

        ponytail: the ROW COUNT still grows with the number of events. Move the
        identities into an age-bounded tombstone table only once a replay older
        than that window can be refused by this table alone.
        """
        keep = max(0, int(keep))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in PREPARED_TERMINAL_STATES)
            ordered = db.execute(
                f"""SELECT prepared_id, execution_id FROM prepared_executions
                    WHERE state IN ({marks})
                    ORDER BY prepared_id, claimed_at DESC, execution_id DESC""",
                PREPARED_TERMINAL_STATES).fetchall()
            # The rows arrive grouped per job and newest-first; keep the first
            # `keep` of each group and compact the rest.
            seen: dict[str, int] = {}
            stale = []
            for prepared_id, execution_id in ordered:
                index = seen.get(prepared_id, 0)
                seen[prepared_id] = index + 1
                if index >= keep:
                    stale.append(execution_id)
            if not stale:
                db.rollback()
                return 0
            ids = ",".join("?" for _ in stale)
            changed = db.execute(
                f"""UPDATE prepared_executions SET result='', delivery_hash=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, process_token=NULL,
                    selection_reason=''
                    WHERE execution_id IN ({ids})""", stale).rowcount
            db.commit()
        return changed

    def active_receipts(self) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT execution_id,prepared_id,trigger_event_id,state,
                owner_id,claim_generation,attempt_number,lease_expires_at,process_id,
                process_token,delivery_hash,account_id,launcher_id,project_id,is_test
                FROM prepared_executions WHERE state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING')""").fetchall()
        keys = ("execution_id", "prepared_id", "trigger_event_id", "state", "owner_id",
                "claim_generation", "attempt_number", "lease_expires_at", "process_id",
                "process_token", "delivery_hash", "account_id", "launcher_id",
                "project_id", "is_test")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def jobs_with_active_tests(self) -> set[str]:
        """Return jobs whose isolated manual test still owns execution safety."""
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT DISTINCT prepared_id FROM prepared_executions
                WHERE is_test=1 AND state IN
                ('CLAIMED','PREPARING','LAUNCHING','DELIVERING','VERIFYING','RUNNING',
                 'RECOVERY_REQUIRED')""").fetchall()
        return {row[0] for row in rows}

    def recover_expired(self, execution_id: str, new_owner: str, now: datetime,
                        *, process_alive: bool = False, process_attributable: bool = False) -> tuple[str, int] | None:
        """Fence an expired owner; only pre-side-effect states may be relaunched."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT prepared_id,state,claim_generation,lease_expires_at,
                process_id,delivery_hash,is_test,account_id FROM prepared_executions WHERE execution_id=?""",
                (execution_id,)).fetchone()
            if not row or not row[3] or row[3] > now.isoformat():
                db.rollback()
                return None
            prepared_id, state, generation, _, process_id, delivery_hash, is_test, account_id = row
            if is_test and state in (JobState.CLAIMED.value, JobState.PREPARING.value) and not process_id and not delivery_hash:
                new_state = JobState.CANCELLED
                action = "test-cancelled"
            elif is_test:
                new_state = JobState.RECOVERY_REQUIRED
                action = "ambiguous"
            elif state in (JobState.CLAIMED.value, JobState.PREPARING.value) and not process_id and not delivery_hash:
                new_state = JobState.CLAIMED
                action = "resume"
            elif process_id and process_alive and process_attributable:
                new_state = JobState.RUNNING
                action = "observe"
            else:
                new_state = JobState.RECOVERY_REQUIRED
                action = "ambiguous"
            if action == "resume" and not self._reserve_account(
                db, account_id, execution_id, now, "prepared recovery"
            ):
                db.rollback()
                return None
            if action != "resume":
                db.execute("""DELETE FROM prepared_account_reservations
                    WHERE owner_execution=?""", (execution_id,))
            next_generation = generation + 1
            db.execute("""UPDATE prepared_executions SET owner_id=?,claim_generation=?,state=?,
                heartbeat_at=?,lease_expires_at=?,result=? WHERE execution_id=?""",
                (new_owner, next_generation, new_state.value, now.isoformat(),
                 (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                 "reclaimed before side effect" if action == "resume" else
                 "process observed after owner loss" if action == "observe" else
                 "side effect uncertain; operator recovery required", execution_id))
            job_row = db.execute("SELECT body FROM prepared_jobs WHERE prepared_id=?", (prepared_id,)).fetchone()
            if job_row and not is_test:
                job = self._decode(job_row[0])
                job = replace(job, state=new_state,
                              waiting_reason="" if action == "resume" else
                              "observing existing process" if action == "observe" else
                              "delivery outcome uncertain; inspect execution receipt")
                db.execute("UPDATE prepared_jobs SET body=?,revision=revision+1 WHERE prepared_id=?",
                           (self._encode(job), prepared_id))
            db.commit()
        return action, next_generation

    def test_recovery(self, prepared_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("""SELECT execution_id,trigger_event_id,process_id,result
                FROM prepared_executions WHERE prepared_id=? AND is_test=1
                AND state='RECOVERY_REQUIRED' ORDER BY claimed_at DESC LIMIT 1""",
                (prepared_id,)).fetchone()
        keys = ("execution_id", "trigger_event_id", "process_id", "result")
        return dict(zip(keys, row, strict=True)) if row else None

    def resolve_test_recovery(self, execution_id: str, reason: str) -> bool:
        """Operator closes an uncertain manual test after checking its process."""
        if not reason.strip():
            raise ValueError("operator recovery reason required")
        with closing(self._connect()) as db:
            changed = db.execute("""UPDATE prepared_executions SET state='CANCELLED',result=?
                WHERE execution_id=? AND is_test=1 AND state='RECOVERY_REQUIRED'""",
                (reason[:240], execution_id)).rowcount
        return changed == 1

    def receipt(self, prepared_id: str, event_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT execution_id,state,claimed_at,owner_id,process_id,delivery_hash,result,
                claim_generation,attempt_number,lease_expires_at,heartbeat_at,process_token,
                account_id,launcher_id,project_id,is_test,selection_reason
                FROM prepared_executions WHERE prepared_id=? AND trigger_event_id=?""",
                (prepared_id, event_id),
            ).fetchone()
        keys = ("execution_id", "state", "claimed_at", "owner_id", "process_id",
                "delivery_hash", "result", "claim_generation", "attempt_number",
                "lease_expires_at", "heartbeat_at", "process_token", "account_id",
                "launcher_id", "project_id", "is_test", "selection_reason")
        return dict(zip(keys, row, strict=True)) if row else None

    def reown_audit(self, execution_id: str, new_owner: str, now: datetime,
                    keep_state: JobState = JobState.DELIVERING) -> tuple[str, int] | None:
        """Adopt a live audit execution under a fresh lease without losing it.

        An audit execution has no local process identity: its side effect is a
        durable audit start intent keyed by source_execution_id, and the
        coordinator already adopts a repeated start instead of creating a
        second dispatch. So takeover here never fences the execution into
        RECOVERY_REQUIRED the way the CLI pid-based path must -- it re-arms
        the lease under the reconciling scheduler, which then derives the
        outcome from the canonical audit stores.
        """
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT state,claim_generation,owner_id,lease_expires_at
                FROM prepared_executions WHERE execution_id=?""",
                (execution_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            state, generation, owner_id, lease_expires_at = row
            active = {"CLAIMED", "PREPARING", "LAUNCHING", "DELIVERING",
                      "VERIFYING", "RUNNING", "RECOVERY_REQUIRED"}
            if state not in active:
                db.rollback()
                return None
            same_owner_fresh = (
                owner_id == new_owner and lease_expires_at
                and lease_expires_at > now.isoformat()
            )
            next_generation = generation if same_owner_fresh else generation + 1
            db.execute(
                """UPDATE prepared_executions SET owner_id=?,claim_generation=?,
                heartbeat_at=?,lease_expires_at=? WHERE execution_id=?""",
                (new_owner, next_generation, now.isoformat(),
                 (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                 execution_id),
            )
            db.commit()
        return state, next_generation

    def reown_sync(self, execution_id: str, new_owner: str, now: datetime) -> tuple[str, int] | None:
        """Adopt a live sync execution under a fresh lease without losing it."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT state,claim_generation,owner_id,lease_expires_at
                FROM prepared_executions WHERE execution_id=?""",
                (execution_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            state, generation, owner_id, lease_expires_at = row
            active = {"CLAIMED", "PREPARING", "LAUNCHING", "DELIVERING",
                      "VERIFYING", "RUNNING", "RECOVERY_REQUIRED"}
            if state not in active:
                db.rollback()
                return None
            same_owner_fresh = (
                owner_id == new_owner and lease_expires_at
                and lease_expires_at > now.isoformat()
            )
            next_generation = generation if same_owner_fresh else generation + 1
            db.execute(
                """UPDATE prepared_executions SET owner_id=?,claim_generation=?,
                heartbeat_at=?,lease_expires_at=? WHERE execution_id=?""",
                (new_owner, next_generation, now.isoformat(),
                 (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                 execution_id),
            )
            db.commit()
        return state, next_generation

    def reown_prime(self, execution_id: str, new_owner: str, now: datetime) -> tuple[str, int] | None:
        """Adopt a live prime execution under a fresh lease without losing it."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT state,claim_generation,owner_id,lease_expires_at
                FROM prepared_executions WHERE execution_id=?""",
                (execution_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            state, generation, owner_id, lease_expires_at = row
            active = {"CLAIMED", "PREPARING", "LAUNCHING", "DELIVERING",
                      "VERIFYING", "RUNNING", "RECOVERY_REQUIRED"}
            if state not in active:
                db.rollback()
                return None
            same_owner_fresh = (
                owner_id == new_owner and lease_expires_at
                and lease_expires_at > now.isoformat()
            )
            next_generation = generation if same_owner_fresh else generation + 1
            db.execute(
                """UPDATE prepared_executions SET owner_id=?,claim_generation=?,
                heartbeat_at=?,lease_expires_at=? WHERE execution_id=?""",
                (new_owner, next_generation, now.isoformat(),
                 (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat(),
                 execution_id),
            )
            db.commit()
        return state, next_generation



class PreparedScheduler:
    """Pure evaluation plus durable claim; caller owns verified delivery."""

    def __init__(self, store: PreparedStore, clock: Callable[[], datetime] = utc_now, owner_id: str | None = None) -> None:
        self.store = store
        self.clock = clock
        self.owner_id = owner_id or uuid.uuid4().hex

    def due(self, job: PreparedJob, snapshot: LimitSnapshot | None,
            *, quota_bucket: str | None = None, selected_account_id: str = "",
            selected_launcher_id: str = "", selection_reason: str = "") -> tuple[TriggerDecision, str | None]:
        now = self.clock()
        reconciled = reconcile_reset(job, snapshot, now, quota_bucket=quota_bucket)
        if reconciled != job:
            self.store.save(reconciled)
            job = reconciled
        decision = evaluate_trigger(job, snapshot, now, quota_bucket=quota_bucket)
        if decision.state != JobState.CLAIMED:
            return decision, None
        return decision, self.store.claim(
            job.prepared_id, decision.event_id, self.owner_id, self.clock(),
            selected_account_id=selected_account_id,
            selected_launcher_id=selected_launcher_id,
            selection_reason=selection_reason,
        )
