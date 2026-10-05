"""Provider-neutral, non-consuming limit observations and probe cadence."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from audapack.account_registry import AccountIdentity
from audapack.config import get_state_dir


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOW = "LOW"
    EXHAUSTED = "EXHAUSTED"
    RESET_PENDING = "RESET_PENDING"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    ERROR = "ERROR"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("limit timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class LimitWindow:
    window_id: str
    kind: str
    label: str
    remaining_ratio: float | None = None
    used_ratio: float | None = None
    remaining_units: float | None = None
    capacity_units: float | None = None
    window_started_at: str | None = None
    reset_at: str | None = None
    duration_seconds: int | None = None
    source: str = ""
    confidence: str = "OBSERVED"
    is_hard_limit: bool = True
    quota_bucket: str = ""
    window_start_semantics: str = "UNKNOWN"

    def __post_init__(self) -> None:
        for ratio in (self.remaining_ratio, self.used_ratio):
            if ratio is not None and not 0 <= ratio <= 1:
                raise ValueError("limit ratio must be in [0, 1]")
        parse_time(self.reset_at)
        parse_time(self.window_started_at)


@dataclass(frozen=True)
class LimitSnapshot:
    account_id: str
    windows: tuple[LimitWindow, ...]
    observed_at: str
    source: str
    stale_after_seconds: int = 1800
    error: str = ""

    def __post_init__(self) -> None:
        parse_time(self.observed_at)
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")

    def relevant_windows(self, quota_bucket: str | None = None) -> tuple[LimitWindow, ...]:
        buckets = {w.quota_bucket for w in self.windows if w.quota_bucket}
        if quota_bucket is None and self.source == "codex_app_server" and "codex" in buckets:
            quota_bucket = "codex"
        if quota_bucket is None and len(buckets) > 1:
            return ()  # Model-to-bucket mapping is unknown; never guess.
        if quota_bucket is None:
            return self.windows
        return tuple(w for w in self.windows if not w.quota_bucket or w.quota_bucket == quota_bucket)

    def availability(self, now: datetime | None = None, *, quota_bucket: str | None = None) -> Availability:
        now = now or utc_now()
        if self.error:
            return Availability.ERROR
        if now - parse_time(self.observed_at) > timedelta(seconds=self.stale_after_seconds):
            return Availability.STALE
        hard = [window for window in self.relevant_windows(quota_bucket) if window.is_hard_limit]
        if not hard:
            return Availability.UNKNOWN
        exhausted = [window for window in hard if window.remaining_ratio == 0 or window.remaining_units == 0]
        if exhausted:
            if any(parse_time(window.reset_at) and parse_time(window.reset_at) <= now for window in exhausted):
                return Availability.RESET_PENDING
            return Availability.EXHAUSTED
        if any(window.remaining_ratio is None and window.remaining_units is None for window in hard):
            return Availability.UNKNOWN
        if any(window.remaining_ratio is not None and window.remaining_ratio <= 0.1 for window in hard):
            return Availability.LOW
        return Availability.AVAILABLE

    def bottleneck(self, *, quota_bucket: str | None = None) -> LimitWindow | None:
        def remaining(window: LimitWindow) -> float | None:
            if window.remaining_ratio is not None:
                return window.remaining_ratio
            if window.remaining_units is not None and window.capacity_units:
                return window.remaining_units / window.capacity_units
            return None

        known = [window for window in self.relevant_windows(quota_bucket)
                 if window.is_hard_limit and remaining(window) is not None]
        return min(known, key=remaining) if known else None


class LimitAdapter(Protocol):
    provider_id: str

    def probe_limits(self, account: AccountIdentity) -> LimitSnapshot:
        """Read account limits without causing a model request."""


class LimitStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_state_dir() / "resources.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS limit_snapshots (
                    account_id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    observed_at TEXT NOT NULL, next_probe_at TEXT NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS limit_history (
                    account_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(account_id, observed_at)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS limit_probe_leases (
                    account_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def get(self, account_id: str) -> tuple[LimitSnapshot, datetime, int] | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT body, next_probe_at, failures FROM limit_snapshots WHERE account_id=?", (account_id,)
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["windows"] = tuple(LimitWindow(**item) for item in data["windows"])
        return LimitSnapshot(**data), parse_time(row[1]), int(row[2])

    def put(self, snapshot: LimitSnapshot, next_probe_at: datetime, failures: int) -> None:
        body = json.dumps(asdict(snapshot), separators=(",", ":"))
        next_time = next_probe_at.astimezone(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO limit_snapshots VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET body=excluded.body,
                observed_at=excluded.observed_at, next_probe_at=excluded.next_probe_at,
                failures=excluded.failures""",
                (snapshot.account_id, body, snapshot.observed_at, next_time, failures),
            )
            db.execute(
                "INSERT OR REPLACE INTO limit_history VALUES (?, ?, ?)",
                (snapshot.account_id, snapshot.observed_at, body),
            )
            db.execute(
                """DELETE FROM limit_history WHERE account_id=? AND observed_at NOT IN (
                SELECT observed_at FROM limit_history WHERE account_id=?
                ORDER BY observed_at DESC LIMIT 128)""",
                (snapshot.account_id, snapshot.account_id),
            )

    def claim_probe(self, account_id: str, owner_id: str, now: datetime, ttl_seconds: int = 90) -> bool:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires_at FROM limit_probe_leases WHERE account_id=?", (account_id,)
            ).fetchone()
            if row and parse_time(row[0]) > now:
                db.rollback()
                return False
            db.execute(
                """INSERT INTO limit_probe_leases VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET owner_id=excluded.owner_id,
                expires_at=excluded.expires_at""",
                (account_id, owner_id, (now + timedelta(seconds=ttl_seconds)).isoformat()),
            )
            db.commit()
        return True

    def release_probe(self, account_id: str, owner_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM limit_probe_leases WHERE account_id=? AND owner_id=?",
                       (account_id, owner_id))


class LimitCoordinator:
    """One in-flight probe per account; normal cadence is fifteen minutes."""

    NORMAL_SECONDS = 900

    def __init__(
        self, adapters: dict[str, LimitAdapter], store: LimitStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.adapters = adapters
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self.owner_id = uuid.uuid4().hex

    def refresh(self, account: AccountIdentity, *, force: bool = False) -> LimitSnapshot | None:
        now = self.clock()
        previous = self.store.get(account.account_id)
        if previous and not force and now < previous[1]:
            return previous[0]
        adapter = self.adapters.get(account.provider_id)
        if adapter is None:
            return previous[0] if previous else None
        with self._lock:
            if account.account_id in self._inflight:
                return previous[0] if previous else None
            self._inflight.add(account.account_id)
        if not self.store.claim_probe(account.account_id, self.owner_id, now):
            with self._lock:
                self._inflight.discard(account.account_id)
            return previous[0] if previous else None
        try:
            try:
                snapshot = adapter.probe_limits(account)
                if snapshot.account_id != account.account_id:
                    raise ValueError("provider returned another account's snapshot")
            except Exception as exc:
                snapshot = LimitSnapshot(
                    account_id=account.account_id, windows=(), observed_at=now.isoformat(),
                    source=adapter.provider_id, error=type(exc).__name__,
                )
            failures = (previous[2] + 1) if snapshot.error and previous else int(bool(snapshot.error))
            delay = min(3600, self.NORMAL_SECONDS * (2 ** min(failures - 1, 3))) if failures else self.NORMAL_SECONDS
            self.store.put(snapshot, now + timedelta(seconds=delay), failures)
            return snapshot
        finally:
            self.store.release_probe(account.account_id, self.owner_id)
            with self._lock:
                self._inflight.discard(account.account_id)
