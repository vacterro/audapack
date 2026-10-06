"""Deterministic prepared AUTO account ranking; reservations live in PreparedStore."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from audapack.account_registry import AccountIdentity
from audapack.limits import Availability, LimitSnapshot, parse_time
from audapack.prepared import PreparedJob


@dataclass(frozen=True)
class AutoCandidate:
    account: AccountIdentity
    launcher_id: str
    snapshot: LimitSnapshot
    quota_bucket: str | None
    workload: int


@dataclass(frozen=True)
class AutoSelection:
    candidate: AutoCandidate
    reason: str


def rank_auto_accounts(job: PreparedJob,
                       candidates: list[AutoCandidate], now: datetime) -> list[AutoSelection]:
    """Operator priority, actual bottleneck, workload and reset break ties."""
    preferred = job.trigger_config.get("auto_priority") or []
    if not isinstance(preferred, list):
        preferred = []
    priority = {str(account_id): index for index, account_id in enumerate(preferred)}
    ranked = []
    for candidate in candidates:
        account = candidate.account
        availability = candidate.snapshot.availability(now, quota_bucket=candidate.quota_bucket)
        if availability not in (Availability.AVAILABLE, Availability.LOW):
            continue
        bottleneck = candidate.snapshot.bottleneck(quota_bucket=candidate.quota_bucket)
        remaining = (bottleneck.remaining_ratio if bottleneck and bottleneck.remaining_ratio is not None
                     else bottleneck.remaining_units / bottleneck.capacity_units
                     if bottleneck and bottleneck.remaining_units is not None and
                     bottleneck.capacity_units else 0.0)
        reset = parse_time(bottleneck.reset_at) if bottleneck else None
        reset_key = reset.timestamp() if reset else float("inf")
        rank = priority.get(account.account_id, len(priority))
        score = (rank, -remaining, candidate.workload, reset_key, account.account_id)
        reason = (f"AUTO priority={rank} remaining={remaining:.3f} "
                  f"workload={candidate.workload} bucket={candidate.quota_bucket or 'default'} "
                  f"reset={reset.isoformat() if reset else 'unknown'}")
        ranked.append((score, AutoSelection(candidate, reason)))
    ranked.sort(key=lambda item: item[0])
    return [item[1] for item in ranked]
