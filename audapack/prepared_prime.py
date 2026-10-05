"""Exactly-once consuming PRIME action, separate from normal limit probes."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from audapack.account_registry import AccountIdentity
from audapack.config import get_state_dir
from audapack.limits import Availability, LimitSnapshot, LimitWindow
from audapack.provider_capabilities import ProviderCapabilities, WindowStartSemantics

PRIME_PROMPT = "Return exactly OK. Do not read or write files."


@dataclass(frozen=True)
class PrimeTarget:
    prepared_id: str
    trigger_event_id: str
    account_id: str
    model: str
    quota_bucket: str
    window_id: str


class PrimeStore:
    """A claimed consuming action is never replayed after owner loss."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_state_dir() / "resources.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS prime_receipts (
                prime_id TEXT PRIMARY KEY, prepared_id TEXT NOT NULL,
                trigger_event_id TEXT NOT NULL, account_id TEXT NOT NULL,
                model TEXT NOT NULL, quota_bucket TEXT NOT NULL, window_id TEXT NOT NULL,
                state TEXT NOT NULL, requested_at TEXT NOT NULL,
                action_claimed_at TEXT NOT NULL DEFAULT '',
                previous_window_start TEXT NOT NULL DEFAULT '',
                previous_reset TEXT NOT NULL DEFAULT '',
                observed_window_start TEXT NOT NULL DEFAULT '',
                observed_reset TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                UNIQUE(prepared_id,trigger_event_id))""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, target: PrimeTarget, now: datetime) -> str:
        if now.tzinfo is None or not all(vars(target).values()):
            raise ValueError("PRIME requires complete target and timezone-aware request")
        prime_id = uuid.uuid4().hex
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT prime_id,account_id,model,quota_bucket,window_id
                FROM prime_receipts WHERE prepared_id=? AND trigger_event_id=?""",
                (target.prepared_id, target.trigger_event_id)).fetchone()
            if row:
                if row[1:] != (target.account_id, target.model, target.quota_bucket,
                               target.window_id):
                    db.rollback()
                    raise ValueError("PRIME event target changed")
                db.rollback()
                return row[0]
            db.execute("""INSERT INTO prime_receipts
                (prime_id,prepared_id,trigger_event_id,account_id,model,quota_bucket,
                 window_id,state,requested_at) VALUES (?,?,?,?,?,?,?,'PENDING',?)""",
                (prime_id, target.prepared_id, target.trigger_event_id,
                 target.account_id, target.model, target.quota_bucket,
                 target.window_id, now.astimezone(timezone.utc).isoformat()))
            db.commit()
        return prime_id

    def get(self, prime_id: str) -> dict | None:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM prime_receipts WHERE prime_id=?",
                             (prime_id,)).fetchone()
        return dict(row) if row else None

    def get_by_event(self, prepared_id: str, trigger_event_id: str) -> dict | None:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT * FROM prime_receipts WHERE prepared_id=? AND trigger_event_id=?""",
                (prepared_id, trigger_event_id),
            ).fetchone()
        return dict(row) if row else None

    def claim_action(self, prime_id: str, previous: LimitWindow, now: datetime) -> bool:
        """Durable line before the provider can consume usage."""
        if now.tzinfo is None:
            raise ValueError("PRIME action time must include timezone")
        with closing(self._connect()) as db:
            changed = db.execute("""UPDATE prime_receipts SET state='SUBMITTING',
                action_claimed_at=?,previous_window_start=?,previous_reset=?
                WHERE prime_id=? AND state='PENDING' AND quota_bucket=? AND window_id=?""",
                (now.astimezone(timezone.utc).isoformat(), previous.window_started_at or "",
                 previous.reset_at or "", prime_id, previous.quota_bucket,
                 previous.window_id)).rowcount
        return changed == 1

    def record_submission(self, prime_id: str, *, accepted: bool, result: str) -> bool:
        with closing(self._connect()) as db:
            changed = db.execute("""UPDATE prime_receipts SET state=?,result=?
                WHERE prime_id=? AND state='SUBMITTING'""",
                ("OBSERVING" if accepted else "RECOVERY_REQUIRED", result[:240],
                 prime_id)).rowcount
        return changed == 1

    def record_observation(self, prime_id: str, window: LimitWindow | None) -> bool:
        receipt = self.get(prime_id)
        if receipt is None or receipt["state"] != "OBSERVING":
            return False
        if window is not None and (window.quota_bucket, window.window_id) != (
            receipt["quota_bucket"], receipt["window_id"]
        ):
            raise ValueError("observed window does not match PRIME target")
        started = window.window_started_at if window else ""
        reset = window.reset_at if window else ""
        changed_window = bool(
            (started and started != receipt["previous_window_start"]) or
            (reset and reset != receipt["previous_reset"])
        )
        with closing(self._connect()) as db:
            changed = db.execute("""UPDATE prime_receipts SET state=?,
                observed_window_start=?,observed_reset=?,result=?
                WHERE prime_id=? AND state='OBSERVING'""",
                ("DONE" if changed_window else "UNVERIFIED", started or "", reset or "",
                 "target window changed" if changed_window else "target window change unproven",
                 prime_id)).rowcount
        return changed == 1

    def recover_interrupted(self, prime_id: str) -> bool:
        with closing(self._connect()) as db:
            changed = db.execute("""UPDATE prime_receipts SET state='RECOVERY_REQUIRED',
                result='provider side effect uncertain; operator recovery required'
                WHERE prime_id=? AND state='SUBMITTING'""",
                (prime_id,)).rowcount
        return changed == 1


class PrimeCoordinator:
    """Only a proven first-use model/bucket pair can cross the consuming line."""

    def __init__(self, store: PrimeStore,
                 capability: Callable[[AccountIdentity], ProviderCapabilities | None],
                 probe: Callable[[AccountIdentity], LimitSnapshot],
                 consume: Callable[[AccountIdentity, str, Path, str], bool]) -> None:
        self.store = store
        self.capability = capability
        self.probe = probe
        self.consume = consume

    @staticmethod
    def _window(snapshot: LimitSnapshot, target: PrimeTarget) -> LimitWindow | None:
        return next((item for item in snapshot.windows
                     if item.quota_bucket == target.quota_bucket and
                     item.window_id == target.window_id), None)

    def execute(self, prime_id: str, account: AccountIdentity,
                now: datetime | None = None) -> str:
        receipt = self.store.get(prime_id)
        if receipt is None or receipt["state"] != "PENDING":
            return "ALREADY_CLAIMED"
        target = PrimeTarget(receipt["prepared_id"], receipt["trigger_event_id"],
                             receipt["account_id"], receipt["model"],
                             receipt["quota_bucket"], receipt["window_id"])
        caps = self.capability(account)
        if (account.account_id != target.account_id or not account.enabled or caps is None or
                not caps.supports_prime or not caps.supports_prepared_prompt or
                not caps.supports_limit_probe or
                caps.evidence.confidence not in {"PROVIDER", "OPERATOR_VERIFIED"} or
                caps.window_start_semantics is not WindowStartSemantics.FIRST_CONSUMING_USE or
                caps.bucket_for_model(target.model) != target.quota_bucket):
            return "UNSUPPORTED"
        try:
            before = self.probe(account)
        except Exception:
            return "LIMIT_UNKNOWN"
        if before.account_id != account.account_id or before.error:
            return "LIMIT_UNKNOWN"
        window = self._window(before, target)
        if window is None or not window.is_hard_limit:
            return "WRONG_BUCKET"
        if before.availability(now or datetime.now(timezone.utc),
                               quota_bucket=target.quota_bucket) in {
            Availability.ERROR, Availability.STALE, Availability.UNKNOWN,
        }:
            return "LIMIT_UNKNOWN"
        if window.window_started_at:
            return "ALREADY_STARTED"
        if not self.store.claim_action(prime_id, window, now or datetime.now(timezone.utc)):
            return "ALREADY_CLAIMED"
        try:
            accepted = bool(self.consume(account, target.model,
                                         Path(tempfile.gettempdir()), PRIME_PROMPT))
        except Exception as exc:
            self.store.record_submission(prime_id, accepted=False, result=type(exc).__name__)
            return "RECOVERY_REQUIRED"
        if not accepted:
            self.store.record_submission(prime_id, accepted=False,
                                         result="provider response did not prove acceptance")
            return "RECOVERY_REQUIRED"
        self.store.record_submission(prime_id, accepted=True, result="provider accepted minimal request")
        return self.observe(prime_id, account)

    def observe(self, prime_id: str, account: AccountIdentity) -> str:
        receipt = self.store.get(prime_id)
        if receipt is None or receipt["state"] != "OBSERVING" or account.account_id != receipt["account_id"]:
            return "NOT_OBSERVABLE"
        target = PrimeTarget(receipt["prepared_id"], receipt["trigger_event_id"],
                             receipt["account_id"], receipt["model"],
                             receipt["quota_bucket"], receipt["window_id"])
        try:
            after = self.probe(account)
            observed = (self._window(after, target)
                        if after.account_id == account.account_id and not after.error else None)
        except Exception:
            observed = None
        self.store.record_observation(prime_id, observed)
        return self.store.get(prime_id)["state"]
