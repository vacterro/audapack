"""Operator-facing audit run orchestration.

This module is deliberately independent from Qt.  It joins the durable browser
dispatch record with the audit index and exposes one conservative state machine
to every UI surface.  Transport COMPLETE is not presented as READY until the
final handoff exists and its identity and digest are proven.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from audapack import agent_inbox
from audapack.config import cross_process_lock, get_state_dir
from audapack.models import AuditSnapshot

logger = logging.getLogger(__name__)

MAX_AUDIT_LANES = 6
RUN_HISTORY_BOUND = 100
WORKER_LAUNCH_COOLDOWN_SECONDS = 20.0
#: A launched Chromium window needs this long to boot, load ChatGPT and
#: register. Until it does it is invisible to the dispatcher, and counting
#: only registered workers is how repeated START presses opened a 7th and 8th
#: window: every call saw the same "free" capacity and launched again.
WORKER_LAUNCH_BOOT_GRACE_SECONDS = 120.0
#: A window that never registers is still a real window on the operator's
#: screen. Relaunching its slot once the boot grace lapsed is how a 7th
#: window appeared while six were already open, so a slot that has been
#: launched this many times without ever registering is left alone.
WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT = 2
#: ...but the ban expires. A slot whose last launch is this old starts its
#: attempt count over, so two bad launches cost one cooldown instead of
#: retiring the slot permanently from a six-lane pool.
WORKER_LAUNCH_ATTEMPT_DECAY_SECONDS = 600.0
#: How long a batch waits for the windows it just asked for to become
#: claimable before submitting anyway. Chromium boot plus a ChatGPT load is
#: tens of seconds and packing is faster, which is how six queued projects
#: ended up as five concurrent audits and one job waiting for a lane.
WORKER_POOL_SETTLE_SECONDS = 90.0
WORKER_POOL_SETTLE_POLL_SECONDS = 3.0

#: How long a transport-COMPLETE dispatch may stay SAVING before its
#: readiness proof is treated as never arriving. Finalization writes its
#: artifacts in seconds; past this the proof is not late, it is not coming --
#: the index belongs to a later run, or the campaign identity drifted. Three
#: such rows sat SAVING for over a day with no action able to move them:
#: a terminal dispatch refuses Cancel and refuses FORCE UNBLOCK, so RESET ALL
#: offered to clear them and could not.
COMPLETE_SETTLE_AFTER_SECONDS = 1800.0

ACTIVE_DISPATCH_STATES = {
    "QUEUED", "RETRYABLE", "LEASED", "ARTIFACT_FETCHED", "ATTACHED",
    "START_PREPARED", "STARTED", "AUDITING", "FINALIZING", "BLOCKED",
}
TERMINAL_DISPATCH_STATES = {"COMPLETE", "FAILED", "CANCELLED"}
ACTIVE_INTENT_STATES = {"PREPARING", "PACKING", "SUBMITTING", "QUEUED", "RUNNING", "RECOVERY_NEEDED"}


@dataclass(frozen=True)
class AuditStartResult:
    ok: bool
    project_id: str
    intent_id: str = ""
    dispatch_id: str = ""
    state: str = "FAILED"
    message: str = ""
    duplicate: bool = False


@dataclass(frozen=True)
class AuditRunSnapshot:
    """A restart-reconstructable, UI-facing view of one audit run."""

    project_id: str
    project_name: str
    operator_state: str
    summary: str
    intent_id: str = ""
    dispatch_id: str = ""
    dispatch_state: str = ""
    worker_id: str = ""
    worker_label: str = ""
    profile_id: str = "quick3"
    campaign_run_id: str = ""
    audit_campaign_run_id: str = ""
    completed_waves: int = 0
    total_waves: int = 3
    ready: bool = False
    ready_proof: tuple[str, ...] = ()
    handoff_path: str = ""
    handoff_sha256: str = ""
    error: str = ""
    recovery: str = ""
    retry_count: int = 0
    last_error_code: str = ""
    conversation_locator: str = ""
    bridge_healthy: bool = False
    worker_counts: dict[str, int] = field(default_factory=dict)
    handoff_present: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    actions: tuple[str, ...] = ()
    #: What the agent did with what we delivered, read from the agent's own
    #: journal at the configured probe path. READY means the station is
    #: finished; it says nothing about whether anyone read the result, and that
    #: is the question the operator actually has before pressing START AUDIT.
    agent_state: str = agent_inbox.NO_INBOX
    agent_summary: str = ""
    agent_guidance: str = ""
    agent_residue: int = 0
    #: Place in the line for the next window to come free. Zero-based; -1 for a
    #: run that is not waiting -- one that already has a window, or is finished.
    queue_position: int = -1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: PERF-001 (audit/11.md): a bounded physical-digest proof cache. The same final
#: handoff was re-hashed from raw bytes on every periodic dashboard poll, and a
#: mismatched COMPLETE run paid the hash TWICE in one snapshot. The key carries
#: a file-change signature (mtime_ns + size) so a same-path mutation invalidates
#: the entry; a missing/unreadable file never produces a cached success.
_DIGEST_CACHE: "OrderedDict[tuple[str, int, int], str]" = OrderedDict()
_DIGEST_CACHE_MAX = 256


def _sha256_file_cached(path: Path) -> str:
    """Return the physical SHA-256 of ``path``, reusing a proof only while the
    file's (path, mtime_ns, size) identity is unchanged. Raises OSError exactly
    as ``_sha256_file`` when the file cannot be statted/read, so callers keep
    their "missing/unreadable is a visible failure" contract."""

    stat = path.stat()
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    cached = _DIGEST_CACHE.get(key)
    if cached is not None:
        _DIGEST_CACHE.move_to_end(key)
        return cached
    digest = _sha256_file(path)
    # Re-stat after hashing: a concurrent write during the read invalidates the
    # key we are about to store.
    after = path.stat()
    if int(after.st_mtime_ns) == key[1] and int(after.st_size) == key[2]:
        _DIGEST_CACHE[key] = digest
        _DIGEST_CACHE.move_to_end(key)
        while len(_DIGEST_CACHE) > _DIGEST_CACHE_MAX:
            _DIGEST_CACHE.popitem(last=False)
    return digest


def _clear_digest_cache() -> None:
    _DIGEST_CACHE.clear()


class StaleClaimError(RuntimeError):
    """A start-pipeline writer no longer owns the intent it is mutating (W2-001)."""


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness probe for a claim owner.

    W2-001: PID alone cannot distinguish concurrent calls in one process, so it
    is only one input -- liveness here, plus a fencing token on every mutation.
    An uncertain probe returns True (conservative: never steal a claim we cannot
    prove dead).
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True



class AuditStartIntentStore:
    """Small atomic journal written before packing or dispatch submission."""

    def __init__(self, path: Optional[Path] = None, history_bound: int = RUN_HISTORY_BOUND):
        self.path = Path(path) if path else get_state_dir() / "audit_start_intents.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.history_bound = max(MAX_AUDIT_LANES, int(history_bound))

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "updated_at": 0.0, "intents": []}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Audit start intent journal is unreadable: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("intents"), list):
            raise RuntimeError("Audit start intent journal has an unsupported schema")
        return doc

    def list(self) -> list[dict[str, Any]]:
        with cross_process_lock(self.lock_path):
            return [dict(item) for item in self._read_unlocked()["intents"] if isinstance(item, dict)]

    def _trim_intents(self, intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Prepared source keys are durable idempotency records. Ordinary
        # history remains bounded while these keys await reconciliation.
        ordinary = [item for item in intents if not item.get("source_execution_id")]
        retained = {item.get("intent_id") for item in ordinary[-self.history_bound:]}
        kept = [item for item in intents
                if item.get("source_execution_id") or item.get("intent_id") in retained]
        stale = self._compactable_ids(kept)
        return [self._compact_source_intent(item, stale) for item in kept]

    def _compactable_ids(self, population: list[dict[str, Any]]) -> set[int]:
        """Ids of the source records old enough to be reduced to identities.

        A record is stale when it is source-keyed, not already compacted, not
        still active, and past the most recent ``history_bound`` records that
        share its status. The newest slice of each status keeps every field, so
        a run an operator is still reading is never rewritten underneath them.
        """
        by_status: dict[str, list[dict[str, Any]]] = {}
        for entry in population:
            if entry.get("compacted") or not entry.get("source_execution_id"):
                continue
            by_status.setdefault(str(entry.get("status") or ""), []).append(entry)
        stale: set[int] = set()
        for status, group in by_status.items():
            if status in ACTIVE_INTENT_STATES or len(group) <= self.history_bound:
                continue
            group.sort(key=lambda entry: float(entry.get("completed_at")
                                              or entry.get("updated_at") or 0.0),
                       reverse=True)
            stale.update(id(entry) for entry in group[self.history_bound:])
        return stale

    def _compact_source_intent(self, item: dict[str, Any],
                               stale: set[int]) -> dict[str, Any]:
        """Reduce a settled source record to the identity a replay needs.

        W2-003 (audit/12.md): every source-keyed record was retained in full
        forever -- 40 terminally FAILED prepared sources with a bound of 6 --
        because a prepared source key is the durable idempotency record and
        dropping it would re-issue a finished audit. The key must survive; the
        error text, the owning PID, the single-writer claim and its fence do
        not, once the record is terminal and settled.

        ponytail: the record COUNT still grows with distinct prepared sources.
        Move old identities into an age-bounded tombstone table only once a
        replay older than that window can be refused by the prepared execution
        tombstone alone.
        """
        if id(item) not in stale:
            return item
        return {**item, "error": "", "owner_pid": 0, "claim_token": "",
                "claim_holder_pid": 0, "fence": 0, "campaign_run_id": "",
                "compacted": True}


    def begin(self, project_id: str, project_name: str, profile_id: str,
              source_execution_id: str = "") -> tuple[dict[str, Any], bool]:
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._read_unlocked()
            if source_execution_id:
                prior = next((item for item in reversed(doc["intents"])
                              if item.get("source_execution_id") == source_execution_id), None)
                if prior is not None:
                    return dict(prior), False
            active = next(
                (
                    item for item in reversed(doc["intents"])
                    if str(item.get("project_id")) == str(project_id)
                    and str(item.get("status")) in ACTIVE_INTENT_STATES
                ),
                None,
            )
            if active is not None:
                return dict(active), False
            intent = {
                "intent_id": f"int-{uuid.uuid4().hex[:16]}",
                "project_id": str(project_id),
                "project_name": str(project_name),
                "profile_id": str(profile_id or "quick3"),
                "source_execution_id": str(source_execution_id),
                "status": "PREPARING",
                "dispatch_id": "",
                "campaign_run_id": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": 0.0,
                "owner_pid": os.getpid(),
                # W2-001: begin() records ownership but grants NO claim. The
                # single writer claim is issued only by claim_or_takeover(),
                # held for the PREPARING -> PACKING -> SUBMITTING section and
                # fenced on every mutation, so a stored intent is never confused
                # with a live critical section.
                "claim_token": "",
                "claim_holder_pid": 0,
                "fence": 0,
            }
            intent["request_id"] = intent["intent_id"]
            intent["phase"] = intent["status"]
            intent["archive_path"] = ""
            doc["intents"].append(intent)
            doc["intents"] = self._trim_intents(doc["intents"])
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return dict(intent), True

    def find_for_source(self, source_execution_id: str) -> Optional[dict[str, Any]]:
        if not source_execution_id:
            return None
        with cross_process_lock(self.lock_path):
            return next((dict(item) for item in reversed(self._read_unlocked()["intents"])
                         if item.get("source_execution_id") == source_execution_id), None)


    def claim_or_takeover(self, intent_id: str, take_over: bool = False) -> tuple[bool, dict[str, Any]]:
        """W2-001: atomically grant the one writer authorized to advance an intent.

        Returns ``(granted, intent)``. A caller that loses the claim MUST stand
        down: it may not pack, submit, mutate PACKING/SUBMITTING/QUEUED, or
        attach FAILED/error to the durable intent. Exactly one claim lives at a
        time: an in-process holder marks the journal so a second caller refuses
        itself without any owner-PID comparison, while a second process refuses
        on the holder's live PID. Recovery is explicit: ``take_over=True``
        issues a fresh claim token and bumps the fencing generation.
        """
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._read_unlocked()
            target = next((item for item in doc["intents"] if item.get("intent_id") == intent_id), None)
            if target is None:
                raise KeyError(f"Unknown audit start intent: {intent_id}")
            status = str(target.get("status") or "")
            pre_dispatch = status in {"PREPARING", "PACKING", "SUBMITTING"}
            # An in-process holder (same process, possibly a second thread) writes
            # its marker into the journal under the same lock, so no PID
            # comparison is trusted and a process can be proven dead or not.
            marker_pid = target.get("claim_holder_pid")
            holder_marker = (
                pre_dispatch
                and isinstance(marker_pid, int)
                and marker_pid == os.getpid()
                and bool(target.get("claim_token"))
            )
            owner_pid = int(target.get("owner_pid", 0) or 0)
            owner_alive = (
                pre_dispatch
                and _pid_is_alive(owner_pid)
                and bool(target.get("claim_token"))
            )
            if not take_over and (holder_marker or owner_alive):
                return False, dict(target)
            token = uuid.uuid4().hex
            target["claim_token"] = token
            target["claim_holder_pid"] = os.getpid()
            target["fence"] = int(target.get("fence", 0) or 0) + 1
            target["owner_pid"] = os.getpid()
            target["status"] = "PREPARING"
            target["phase"] = "PREPARING"
            target["error"] = ""
            target["updated_at"] = now
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return True, dict(target)

    def update(self, intent_id: str, expect_fence: Optional[int] = None, **changes: Any) -> dict[str, Any]:
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._read_unlocked()
            target = next((item for item in doc["intents"] if item.get("intent_id") == intent_id), None)
            if target is None:
                raise KeyError(f"Unknown audit start intent: {intent_id}")
            if expect_fence is not None and int(target.get("fence", 0) or 0) != int(expect_fence):
                raise StaleClaimError(
                    f"Intent {intent_id} is owned by fencing generation "
                    f"{target.get('fence')}, not {expect_fence}"
                )
            normalized = {key: value for key, value in changes.items() if key not in {"intent_id", "project_id", "created_at"}}
            if "status" in normalized:
                normalized["phase"] = normalized["status"]
            if all(target.get(key) == value for key, value in normalized.items()):
                return dict(target)
            target.update(normalized)
            target["updated_at"] = now
            if str(target.get("status")) in {"READY", "FAILED", "CANCELLED"} and not target.get("completed_at"):
                target["completed_at"] = now
            doc["intents"] = self._trim_intents(doc["intents"])
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return dict(target)

    def reconcile(self, updates: dict[str, dict[str, Any]]) -> int:
        """Apply many intent updates under ONE lock, writing at most once.

        PERF-002 (audit/1.md): the dashboard called `update()` once per matched
        job, and every call reacquired the cross-process lock and reparsed the
        WHOLE journal before it could discover the values were already correct.
        Measured: 101 journal reads and ~3 MB of repeated JSON parsing for one
        refresh of 100 runs that had not changed at all. Same normalization and
        the same completed_at semantics as `update()`; the write happens only if
        something actually moved.
        """
        if not updates:
            return 0
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._read_unlocked()
            changed = 0
            for target in doc["intents"]:
                changes = updates.get(str(target.get("intent_id") or ""))
                if not changes:
                    continue
                normalized = {
                    key: value for key, value in changes.items()
                    if key not in {"intent_id", "project_id", "created_at"}
                }
                if "status" in normalized:
                    normalized["phase"] = normalized["status"]
                if all(target.get(key) == value for key, value in normalized.items()):
                    continue
                target.update(normalized)
                target["updated_at"] = now
                if str(target.get("status")) in {"READY", "FAILED", "CANCELLED"} and not target.get("completed_at"):
                    target["completed_at"] = now
                changed += 1
            if not changed:
                return 0
            doc["intents"] = self._trim_intents(doc["intents"])
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return changed

    def find_for_dispatch(self, dispatch_id: str) -> Optional[dict[str, Any]]:
        return next((item for item in reversed(self.list()) if item.get("dispatch_id") == dispatch_id), None)


class ManagedWorkerSupervisor:
    """Launches only marked AUDAPACK-dedicated windows; never closes browser tabs."""

    def __init__(
        self,
        launch_worker: Callable[[int, int], tuple[bool, str]],
        path: Optional[Path] = None,
        cooldown_seconds: float = WORKER_LAUNCH_COOLDOWN_SECONDS,
    ):
        self.launch_worker = launch_worker
        self.path = Path(path) if path else get_state_dir() / "managed_browser_workers.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))

    def _load(self) -> dict[str, Any]:
        doc, _corrupt = self._load_checked()
        return doc

    def _load_checked(self) -> tuple[dict[str, Any], bool]:
        """The worker ledger, and whether the file on disk was unusable.

        W2-005 (audit/3.md): unreadable JSON, a wrong schema or a non-dict all
        became a fresh `{generation: 1, slots: {}}`. That is not an empty pool,
        it is an UNKNOWN pool: every slot reads as vacant, so the next pass
        launches windows on slots that already have one, and generation 1 makes
        the era collide with the very first pool the install ever had. Corruption
        is reported to the caller, which quarantines the bytes and rebuilds from
        the workers that are actually registered.
        """
        empty = {"schema_version": 1, "generation": 1, "slots": {}}
        if not self.path.exists():
            return empty, False
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return empty, True
        if not isinstance(doc, dict) or doc.get("schema_version") != 1:
            return empty, True
        if not isinstance(doc.get("slots", {}), dict):
            return empty, True
        doc.setdefault("generation", 1)
        doc.setdefault("slots", {})
        return doc, False

    def _rebuild_from_workers(self, workers: list[dict[str, Any]], now: float) -> dict[str, Any]:
        """Reconstruct the ledger from the pool that demonstrably exists.

        A registered worker proves its slot AND its generation, which is exactly
        what the lost file held. Generation never goes backwards: it becomes the
        highest era any live window reports.
        """
        quarantine = self.path.with_name(f"{self.path.name}.corrupt.{uuid.uuid4().hex[:8]}")
        try:
            if self.path.exists():
                self.path.replace(quarantine)
        except OSError:
            quarantine = self.path
        generation = 1
        slots: dict[str, Any] = {}
        for worker in workers:
            try:
                slot = int(worker.get("managed_slot", 0) or 0)
                worker_generation = int(worker.get("managed_generation", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not (1 <= slot <= MAX_AUDIT_LANES):
                continue
            generation = max(generation, worker_generation)
            slots[str(slot)] = {
                "state": "HEARTBEAT",
                "launch_attempts": 0,
                "last_seen_at": float(worker.get("last_seen_at", now) or now),
                "cooldown_until": 0.0,
            }
        doc = {
            "schema_version": 1,
            "generation": max(1, generation),
            "slots": slots,
            "recovered_from": quarantine.name,
            "recovered_at": now,
            "updated_at": now,
        }
        _atomic_write_json(self.path, doc)
        return doc

    def ensure_capacity(self, dispatch: dict[str, Any], demand: int) -> dict[str, Any]:
        desired = min(MAX_AUDIT_LANES, max(1, int(demand or 0)))
        workers = dispatch.get("workers", []) if isinstance(dispatch, dict) else []
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc, corrupt = self._load_checked()
            if corrupt:
                logger.warning("managed worker ledger was unreadable; rebuilding from live workers")
                doc = self._rebuild_from_workers(list(workers), now)
            generation = max(1, int(doc.get("generation", 1)))
            registered = {
                int(worker.get("managed_slot"))
                for worker in workers
                if int(worker.get("managed_generation", 0) or 0) == generation
                and str(worker.get("managed_slot", "")).isdigit()
                and 1 <= int(worker.get("managed_slot")) <= MAX_AUDIT_LANES
            }
            # A slot whose window went quiet is NOT a vacancy. The worker
            # registry drops a worker after 75s without a heartbeat, with no
            # exemption for one that is mid-audit, while the dispatcher itself
            # remembers the slot has a window for 150s. Launching into that gap
            # opened a second window on a slot that already had one, and
            # registration refuses to evict a predecessor holding a live run --
            # so the duplicate never went away. Trust the longer memory.
            registered |= {
                int(slot)
                for slot in (dispatch.get("managed_slot_lanes") or [])
                if str(slot).isdigit() and 1 <= int(slot) <= MAX_AUDIT_LANES
            }
            # A window that was launched but has not registered yet still
            # occupies its slot and one lane. RESERVED counts too (W2-005): a
            # crash between the reservation and the spawn must not let the next
            # pass open a second window on that slot, and the boot grace expires
            # the reservation if the launch never happened.
            pending = {
                int(slot_id)
                for slot_id, slot_state in doc["slots"].items()
                if str(slot_id).isdigit()
                and int(slot_id) not in registered
                and str(slot_state.get("state")) in {"LAUNCHING", "RESERVED"}
                and now - float(slot_state.get("launched_at", 0.0) or 0.0) < WORKER_LAUNCH_BOOT_GRACE_SECONDS
            }
            launched: list[dict[str, Any]] = []
            for slot in range(1, desired + 1):
                if slot in registered:
                    heard = [
                        float(worker.get("last_seen_at", 0.0) or 0.0)
                        for worker in workers
                        if int(worker.get("managed_slot", 0) or 0) == slot
                        and int(worker.get("managed_generation", 0) or 0) == generation
                    ]
                    if heard:
                        doc["slots"][str(slot)] = {
                            "state": "HEARTBEAT",
                            "launch_attempts": 0,
                            "last_seen_at": max(heard),
                            "cooldown_until": 0.0,
                        }
                    # No row for a slot the dispatcher still counts as occupied
                    # means its window has gone quiet, not that it is gone. The
                    # ledger entry is left exactly as it stands -- there is no
                    # heartbeat to record and nothing to reset. All that matters
                    # here is that nothing is launched into it.
                    continue
                if slot in pending:
                    continue
                slot_state = doc["slots"].get(str(slot), {})
                attempts = int(slot_state.get("launch_attempts", 0) or 0)
                attempts_expire_at = float(slot_state.get("attempts_expire_at", 0.0) or 0.0)
                if attempts and "attempts_expire_at" not in slot_state:
                    # Written before the ban had an expiry: a slot recorded by
                    # an older build could be banned forever. One-time amnesty
                    # so an upgraded install starts from a six-lane pool.
                    attempts = 0
                elif attempts and attempts_expire_at and now >= attempts_expire_at:
                    # The attempt counter stops a burst of windows for a slot
                    # that will not register; it must not be a life sentence.
                    # Without this expiry a slot that failed twice was banned
                    # for the life of the state file, and a pool that lost two
                    # slots that way could never reach six lanes again -- which
                    # is exactly the state a jammed pool was found in.
                    attempts = 0
                if attempts >= WORKER_LAUNCH_MAX_ATTEMPTS_PER_SLOT and str(slot_state.get("state")) != "HEARTBEAT":
                    # Its window is presumably still open and simply not
                    # registering; opening another one only adds clutter.
                    continue
                if now < float(slot_state.get("cooldown_until", 0.0) or 0.0):
                    continue
                if len(registered) + len(pending) + len(launched) >= desired:
                    break
                # W2-005 (audit/3.md): the reservation is durable BEFORE the
                # irreversible spawn. It used to launch first and record after,
                # so a failed ledger write lost all knowledge of an already-open
                # browser and the retry opened the same slot again -- measured:
                # launches [(1,1)] with no journal, then [(1,1),(1,1)] on retry.
                # A reservation that outlives a crash is what makes the retry
                # safe: `RESERVED` is treated as occupied by `pending` below, and
                # its cooldown expires it if the launch never happened.
                doc["slots"][str(slot)] = {
                    "state": "RESERVED",
                    "launch_attempts": attempts + 1,
                    "launched_at": now,
                    "attempts_expire_at": now + WORKER_LAUNCH_ATTEMPT_DECAY_SECONDS,
                    "cooldown_until": now + WORKER_LAUNCH_BOOT_GRACE_SECONDS,
                    "message": "reserved before launch",
                }
                doc["desired"] = desired
                doc["updated_at"] = now
                _atomic_write_json(self.path, doc)

                ok, message = self.launch_worker(slot, generation)
                doc["slots"][str(slot)] = {
                    "state": "LAUNCHING" if ok else "LAUNCH_FAILED",
                    "launch_attempts": attempts + 1,
                    "launched_at": now,
                    "attempts_expire_at": now + WORKER_LAUNCH_ATTEMPT_DECAY_SECONDS,
                    "cooldown_until": now + (WORKER_LAUNCH_BOOT_GRACE_SECONDS if ok else self.cooldown_seconds),
                    "message": str(message)[:300],
                }
                launched.append({"slot": slot, "ok": bool(ok), "message": str(message)})
            doc["desired"] = desired
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
        return {"desired": desired, "registered": len(registered), "launched": launched, "generation": generation}

    def launch_slot(self, slot: int, dispatch: dict[str, Any]) -> dict[str, Any]:
        """Open exactly one named slot, whatever the rest of the pool looks like.

        The operator relaunch path went through ensure_capacity(demand=slot),
        which reads `slot` as a COUNT of wanted lanes: reopening slot 2 while
        five other slots were registered satisfied the demand instantly and
        launched nothing. Reopening a window the operator closed is a request
        for that window, not for a lane budget.
        """
        slot = max(1, min(MAX_AUDIT_LANES, int(slot)))
        workers = dispatch.get("workers", []) if isinstance(dispatch, dict) else []
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc, corrupt = self._load_checked()
            if corrupt:
                logger.warning("managed worker ledger was unreadable; rebuilding from live workers")
                doc = self._rebuild_from_workers(list(workers), now)
            generation = max(1, int(doc.get("generation", 1)))
            live = any(
                int(worker.get("managed_slot", 0) or 0) == slot
                and int(worker.get("managed_generation", 0) or 0) == generation
                for worker in workers
            )
            if live:
                return {"slot": slot, "generation": generation, "launched": False, "message": "slot already has a live worker"}
            # W2-003 (SRC-041:R007): enforce the ledger here too. A recent
            # RESERVED/LAUNCHING entry means a browser may already be opening for
            # this slot/generation; the second launch_slot call used to spawn a
            # duplicate before the first ever registered (observed spawns
            # [(2,1),(2,1)]). An expired reservation is dead and may be retried.
            pending_entry = doc["slots"].get(str(slot)) or {}
            pending_state = str(pending_entry.get("state"))
            pending_at = float(pending_entry.get("launched_at", 0.0) or 0.0)
            if pending_state in {"RESERVED", "LAUNCHING"} and (
                now - pending_at < WORKER_LAUNCH_BOOT_GRACE_SECONDS
            ):
                return {
                    "slot": slot,
                    "generation": generation,
                    "launched": False,
                    "message": "slot launch already pending",
                }
            # W2-005: durable reservation before the spawn, here too.
            doc["slots"][str(slot)] = {
                "state": "RESERVED",
                "launch_attempts": 1,
                "launched_at": now,
                "attempts_expire_at": now + WORKER_LAUNCH_ATTEMPT_DECAY_SECONDS,
                "cooldown_until": now + WORKER_LAUNCH_BOOT_GRACE_SECONDS,
                "message": "reserved before launch",
            }
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)

            ok, message = self.launch_worker(slot, generation)
            doc["slots"][str(slot)] = {
                "state": "LAUNCHING" if ok else "LAUNCH_FAILED",
                "launch_attempts": 1,
                "launched_at": now,
                "attempts_expire_at": now + WORKER_LAUNCH_ATTEMPT_DECAY_SECONDS,
                "cooldown_until": now + (WORKER_LAUNCH_BOOT_GRACE_SECONDS if ok else self.cooldown_seconds),
                "message": str(message)[:300],
            }
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
        return {"slot": slot, "generation": generation, "launched": bool(ok), "message": str(message)}

    def reset_stale_slot(self, slot: int) -> bool:
        """Forget a slot's ledger entry ONLY when it is demonstrably dead/stale.

        W2-003 (SRC-041:R007): the explicit operator relaunch path used to reset
        the slot unconditionally before every relaunch, deleting a recent
        RESERVED/LAUNCHING reservation written before the irreversible spawn. A
        retry or double-click during boot then destroyed the only durable
        evidence a browser might already be opening and opened a second one.
        A recent reservation now survives; only LAUNCH_FAILED, an expired boot
        grace, or any other provably dead entry is cleared.
        """
        slot = max(1, min(MAX_AUDIT_LANES, int(slot)))
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._load()
            entry = doc["slots"].get(str(slot))
            if entry is None:
                return False
            state = str(entry.get("state"))
            launched_at = float(entry.get("launched_at", 0.0) or 0.0)
            if state in {"RESERVED", "LAUNCHING"} and (
                now - launched_at < WORKER_LAUNCH_BOOT_GRACE_SECONDS
            ):
                return False
            doc["slots"].pop(str(slot), None)
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return True


BLOCKED_REASONS: dict[str, tuple[str, str]] = {
    "missing_archive": (
        "The packed project ZIP is gone from disk.",
        "PACK the project again, then press START AUDIT.",
    ),
    "changed_archive": (
        "The packed ZIP changed after the run was queued, so its pinned size/SHA-256 no longer match.",
        "Press START AUDIT again to bind a fresh run to the current archive.",
    ),
    "invalid_transition": (
        "The worker asked for the ZIP after START was already prepared.",
        "Press START AUDIT again; the stale run is released automatically.",
    ),
    "artifact-request-timeout": (
        "The worker could not download the ZIP from the Bridge in time.",
        "Check that the Bridge is running, then press START AUDIT again.",
    ),
    "file-injection-rejected": (
        "ChatGPT refused the injected ZIP in the composer.",
        "Open a fresh root ChatGPT tab in the AUDAPACK worker window, then press START AUDIT again.",
    ),
    "attachment-not-ready": (
        "ChatGPT never finished uploading the ZIP. Very large archives are the usual cause.",
        "Reduce the packed size or retry on a faster connection, then press START AUDIT again.",
    ),
    "canonical-start-rejected": (
        "The widget attached the ZIP but its start engine refused to commit the START receipt.",
        "Press START AUDIT again; the exact engine reason is appended to this error.",
    ),
    "clean-state-lost": (
        "The ChatGPT tab stopped being clean right before Send, so the widget refused to overwrite it.",
        "Leave the AUDAPACK worker window alone while it works, then press START AUDIT again.",
    ),
    "bridge-marked-blocked": (
        "The Bridge marked this dispatch blocked during reconciliation.",
        "Press START AUDIT again; use FORCE UNBLOCK if the run already sent its START.",
    ),
}


def blocked_guidance(error: str, post_start: bool) -> tuple[str, str]:
    """Turn a raw dispatch error into (what happened, what to do next).

    Worker errors carry their detail inline (`artifact-http-400:missing_archive`,
    `canonical-start-rejected: <engine reason>`), so both halves of the string
    are tried before falling back to a generic answer. A BLOCKED run with no
    readable next step is the exact complaint this exists to remove.
    """
    code = str(error or "").strip()
    # Codes nest: `pre-start retries exhausted: artifact-http-400:missing_archive`
    # carries the real cause in its last segment. Try every segment, most
    # specific first, so wrapping never costs the operator the explanation.
    segments = [part.strip() for part in code.split(":") if part.strip()]
    aliases = [code, *reversed(segments)]
    for alias in aliases:
        if alias in BLOCKED_REASONS:
            why, action = BLOCKED_REASONS[alias]
            break
    else:
        why = code or "The Bridge returned BLOCKED without a reason code."
        action = (
            "Use FORCE UNBLOCK, then press START AUDIT again."
            if post_start
            else "Press START AUDIT again; the stale pre-start run is released automatically."
        )
    if post_start:
        action = "This run may already own a real audit chat. " + (
            "Use RECOVER to adopt it, or FORCE UNBLOCK to abandon it."
        )
    return why, action


def _profile_wave_count(profile_id: str, default: int = 3) -> int:
    """How many waves this profile actually has, before any wave is saved."""
    try:
        from audapack.campaign import get_profile

        waves = getattr(get_profile(str(profile_id or "")), "waves", None)
        if waves:
            return len(waves)
    except Exception:
        pass
    return default


def _as_queue_position(value: Any) -> int:
    """Place in the waiting line, or -1 when the run is not waiting."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _basename_any_platform(value: str) -> str:
    """The filename inside a path, whichever OS wrote it.

    CORE-003 (audit/1.md): the diagnostics record promises a filename and never
    a directory, but it used `Path(...).name`, which on POSIX reads the whole
    `C:\\Users\\Private\\secret-result.md` as ONE filename -- so the Ubuntu half
    of the CI matrix emitted the operator's full Windows path into a record
    documented as redacted. The redaction cannot depend on which host is
    reading the value; both separators are cut here.
    """
    text = str(value or "")
    if not text:
        return ""
    return text.replace("\\", "/").rsplit("/", 1)[-1]


#: Anything shaped like a filesystem path, on either platform, including UNC.
_DIAGNOSTIC_PATH_RE = re.compile(
    r"""(?:[A-Za-z]:[\\/]|\\\\[^\s\\]+\\|/)(?:[^\s'"<>|]*)""",
)
#: Authorization material an operator would not expect to hand out. Values only:
#: the KEY stays so the reader can see that something was removed. The value
#: alternation takes a scheme prefix too, or `authorization: Basic Zm9v` loses
#: only the word "Basic" and leaves the credential standing next to it.
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"""(?ix)
    (?:
        \b(?:bearer|basic)\s+\S+
      | \b(?:token|secret|password|passwd|pwd|api[_-]?key|authorization|auth)\b
        \s*[:=]\s*
        (?:"[^"]*"|'[^']*'|(?:bearer|basic)\s+\S+|\S+)
    )
    """,
)


def _redact_diagnostic_text(value: str, limit: int) -> str:
    """Free text from a worker, with paths and credentials removed.

    CORE-006 (audit/3.md): `diagnostics()` promises "identities and state only,
    never tokens or content", then copied `snapshot.error` and
    `snapshot.recovery` verbatim -- and `browser_dispatch` accepts
    worker-supplied `payload["error"]` straight into durable job state, so that
    text is arbitrary. Reproduced: `Bearer TOPSECRET token=abc123
    /home/private/raw.txt` in error and a Windows path plus SECRET_CONTENT in
    recovery all survived into the purportedly redacted JSON, which the UI
    offers the operator to copy.

    Paths become their basename rather than vanishing: the filename is the part
    that helps a support reader, and the directory is the part that identifies
    the operator. Redaction happens BEFORE truncation, so a limit can never cut
    a marker in half and leave the secret behind it.
    """
    text = str(value or "")
    if not text:
        return ""
    text = _DIAGNOSTIC_SECRET_RE.sub("[redacted]", text)
    text = _DIAGNOSTIC_PATH_RE.sub(
        lambda match: _basename_any_platform(match.group(0)) or "[path]", text
    )
    return text[:limit]


def _actions_for(operator_state: str) -> tuple[str, ...]:
    if operator_state in {"WAITING", "RETRYING"}:
        # Still in the line, so it can still be moved in it. ATTACHING is not:
        # by then a window is already holding the job.
        return ("UP", "DOWN", "CANCEL", "DETAILS")
    if operator_state in {"PREPARING", "ATTACHING"}:
        return ("CANCEL", "DETAILS")
    if operator_state in {"STARTING", "AUDITING", "SAVING"}:
        # Live post-START work gets STOP, never CANCEL: CANCELLED asserts that
        # no Core was ever sent, and from here a prompt is already in the
        # browser. STOP retires the run honestly through abandon().
        return ("STOP", "DETAILS")
    if operator_state in {"FAILED", "CANCELLED", "SUPERSEDED"}:
        return ("RETRY", "DETAILS")
    if operator_state == "INTERRUPTED":
        return ("RETRY", "DETAILS")
    if operator_state == "BLOCKED_PRE_START":
        return ("RETRY", "CANCEL", "DETAILS")
    if operator_state in {"BLOCKED_POST_START", "RECOVERY"}:
        return ("RECOVER", "ABANDON", "DETAILS")
    if operator_state == "READY":
        return ("OPEN", "COPY", "DETAILS")
    return ("DETAILS",)


class AuditRunCoordinator:
    """Single owner for start, reconstruction, cancellation and support data."""

    def __init__(
        self,
        project_service,
        packing_service,
        bridge_service,
        audit_service,
        component_manager=None,
        intent_store: Optional[AuditStartIntentStore] = None,
        worker_supervisor: Optional[ManagedWorkerSupervisor] = None,
    ):
        self.projects = project_service
        self.packing = packing_service
        self.bridge = bridge_service
        self.audits = audit_service
        self.intents = intent_store or AuditStartIntentStore()
        self.pool_settle_seconds = WORKER_POOL_SETTLE_SECONDS
        if worker_supervisor is not None:
            self.workers = worker_supervisor
        elif component_manager is not None:
            self.workers = ManagedWorkerSupervisor(
                lambda slot, generation: component_manager.launch_browser_worker(
                    managed_slot=slot,
                    managed_generation=generation,
                )
            )
        else:
            self.workers = None

    @staticmethod
    def _is_post_start_block(job: dict[str, Any]) -> bool:
        """True when a dispatch already committed something irreversible."""
        return bool(
            str(job.get("recovery_state") or "") in {"START_PREPARED", "STARTED", "AUDITING", "FINALIZING"}
            or job.get("start_receipt")
            or job.get("campaign_run_id")
        )

    def _release_pre_start_block(self, project_id: str) -> str:
        """Free a project lane jammed by a pre-start BLOCKED dispatch.

        A pre-start block committed nothing: no START receipt, no campaign id,
        no audit turn in any chat. Leaving it non-terminal jams the project in
        two places at once -- enqueue_job() answers `duplicate_dispatch` and
        intents.begin() keeps the intent in RUNNING -- which is how a project
        sits at `BLOCKED PRE-START` for days with no way forward but manual
        Cancel. Sweep it to CANCELLED so the next START simply works.

        A post-start block is never swept: that one may own a real audit in a
        real chat and stays with the operator (RECOVER / ABANDON).
        """
        active = self.bridge.active_browser_job(str(project_id))
        if not active or str(active.get("state")) != "BLOCKED":
            return ""
        if self._is_post_start_block(active):
            return ""
        dispatch_id = str(active.get("dispatch_id") or "")
        if not dispatch_id:
            return ""
        if not self.bridge.cancel_browser_job(dispatch_id).get("ok"):
            return ""
        intent = self.intents.find_for_dispatch(dispatch_id)
        if intent:
            self.intents.update(str(intent["intent_id"]), status="CANCELLED")
        return dispatch_id

    def _healthy_bridge_status(self) -> dict[str, Any]:
        """Return a healthy Bridge status, starting the Bridge only if needed.

        One failed probe is not proof the Bridge is down: /health runs on a
        1.2s timeout against a server that is simultaneously hashing archives
        for six dispatches, and a timeout there used to spawn a second Bridge
        process. Probe again before concluding anything.
        """
        health = self.bridge.runtime_status()
        if health.get("healthy"):
            return health
        health = self.bridge.runtime_status()
        if health.get("healthy"):
            return health
        started, message = self.bridge.start()
        health = self.bridge.runtime_status()
        if not started or not health.get("healthy"):
            raise RuntimeError(message or "Bridge is not healthy")
        return health

    def provision_capacity(self, lanes: int) -> dict[str, Any]:
        """Open managed worker windows for *lanes* audits, once, up front.

        Called once per operator batch instead of once per project: demand
        computed inside the per-project loop only ever asked for `+1`, so six
        queued audits trickled into one or two windows and the rest waited on
        the supervisor's 45s pacing.
        """
        health = self._healthy_bridge_status()
        dispatch_status = (health.get("browser") or {}) if isinstance(health, dict) else {}
        if self.workers is None:
            return {"desired": 0, "launched": []}
        # More queued projects than lanes is the normal case now; opening a
        # window per queued job is exactly the multiplying this must not do.
        wanted = max(1, min(int(lanes or 0), MAX_AUDIT_LANES))
        demand = (
            int(dispatch_status.get("queued_jobs", 0) or 0)
            + int(dispatch_status.get("active_jobs", 0) or 0)
            + wanted
        )
        outcome = self.workers.ensure_capacity(dispatch_status, demand)
        outcome["settled"] = self._await_free_lanes(wanted)
        return outcome

    def _await_free_lanes(self, wanted: int, timeout_seconds: Optional[float] = None) -> int:
        """Wait, briefly, for freshly launched windows to become claimable.

        A launched Chromium window is invisible to the dispatcher until it has
        booted, loaded ChatGPT and registered -- tens of seconds. Packing six
        archives is faster than that, so a six-project batch submitted against
        whatever happened to be clean and the last job simply queued behind
        lanes that were still starting: six windows on screen, five audits
        running. Waiting here is what makes "six at once" actually six.

        Never fatal: a job with no window yet is a wait, not a loss, and the
        Bridge supervisor keeps provisioning either way.
        """
        wanted = max(1, min(MAX_AUDIT_LANES, int(wanted)))
        budget = self.pool_settle_seconds if timeout_seconds is None else timeout_seconds
        deadline = time.time() + max(0.0, float(budget))
        free = 0
        while True:
            try:
                status = (self.bridge.runtime_status() or {}).get("browser") or {}
            except Exception:
                return free
            free = int(status.get("free_workers", 0) or 0)
            if free >= wanted:
                return free
            if time.time() >= deadline:
                return free
            time.sleep(WORKER_POOL_SETTLE_POLL_SECONDS)

    def _settled_by_close(self, project_id: str, intent_id: str, fence,
                          where: str) -> AuditStartResult:
        """The truthful terminal for a start that never crossed a boundary.

        CANCELLED is accurate here and only here: no window was provisioned for
        it and no dispatch was submitted, so the project is genuinely free again.
        """
        try:
            self.intents.update(intent_id, status="CANCELLED",
                                error=f"AUDAPACK is closing ({where})",
                                expect_fence=fence, claim_token="", claim_holder_pid=None)
        except StaleClaimError:
            pass
        return AuditStartResult(False, project_id, intent_id, state="CANCELLED",
                                message=f"Not dispatched: AUDAPACK closed {where}")

    def start(self, project_id: str, profile_id: str = "quick3", provision: bool = True,
              source_execution_id: str = "",
              should_abort: Optional[Callable[[], bool]] = None) -> AuditStartResult:
        """`should_abort` is the cooperative close token (W2-002, audit/12.md).

        It is checked immediately BEFORE each irreversible boundary -- worker
        provisioning and the durable dispatch submission -- and never after one.
        A race that wins past a boundary has already provisioned a window or
        enqueued a job: that work is reported for what it is, because calling it
        CANCELLED would hide a live dispatch from the board.
        """
        project = self.projects.get_project(str(project_id))
        if project is None or not project.enabled or not project.source_path:
            return AuditStartResult(False, str(project_id), message="Project is missing, disabled, or has no source path")
        if not source_execution_id:
            self._release_pre_start_block(project.id)
        intent, created = self.intents.begin(project.id, project.display_name, profile_id,
                                             source_execution_id=source_execution_id)
        if source_execution_id and not created:
            if intent.get("source_execution_id") != source_execution_id:
                return AuditStartResult(False, project.id, str(intent["intent_id"]),
                                        str(intent.get("dispatch_id") or ""), "BLOCKED",
                                        "Another audit owns this project")
            if (str(intent.get("project_id")) != project.id or
                    str(intent.get("profile_id")) != profile_id):
                return AuditStartResult(False, project.id, str(intent["intent_id"]),
                                        str(intent.get("dispatch_id") or ""), "BLOCKED",
                                        "Prepared audit source identity changed")
            dispatch_id = str(intent.get("dispatch_id") or "")
            if dispatch_id:
                try:
                    response = self.bridge.browser_jobs(project.id)
                    prior = next((job for job in response.get("jobs", [])
                                  if job.get("dispatch_id") == dispatch_id), None)
                except Exception:
                    prior = None
                state = str((prior or {}).get("state") or "RECOVERY_NEEDED")
                return AuditStartResult(state not in TERMINAL_DISPATCH_STATES,
                                        project.id, str(intent["intent_id"]), dispatch_id,
                                        state, "Prepared audit dispatch already exists", True)
            if str(intent.get("status")) not in ACTIVE_INTENT_STATES:
                return AuditStartResult(False, project.id, str(intent["intent_id"]),
                                        str(intent.get("dispatch_id") or ""),
                                        str(intent.get("status") or "FAILED"),
                                        "Prepared audit intent is already terminal", True)
        if not created and not source_execution_id:
            active = self.bridge.active_browser_job(project.id)
            if active:
                if str(active.get("state")) == "BLOCKED" and not self._is_post_start_block(active):
                    cancelled = self.bridge.cancel_browser_job(str(active.get("dispatch_id") or ""))
                    if cancelled.get("ok"):
                        self.intents.update(str(intent["intent_id"]), status="CANCELLED")
                        intent, created = self.intents.begin(project.id, project.display_name, profile_id)
                        active = None
                if active is None:
                    pass
                else:
                    dispatch_id = str(active.get("dispatch_id") or intent.get("dispatch_id") or "")
                    return AuditStartResult(
                        True, project.id, str(intent["intent_id"]), dispatch_id,
                        str(active.get("state") or intent.get("status") or "PREPARING"),
                        "Audit run is already active", True,
                    )
            if intent.get("dispatch_id"):
                response = self.bridge.browser_jobs(project.id)
                prior = next(
                    (
                        job for job in response.get("jobs", [])
                        if job.get("dispatch_id") == intent.get("dispatch_id")
                    ),
                    None,
                )
                prior_state = str((prior or {}).get("state") or "")
                if prior_state not in TERMINAL_DISPATCH_STATES:
                    return AuditStartResult(
                        True, project.id, str(intent["intent_id"]), str(intent["dispatch_id"]),
                        prior_state or str(intent.get("status") or "PREPARING"),
                        "Audit run is already active", True,
                    )
                self.intents.update(str(intent["intent_id"]), status=prior_state)
                intent, created = self.intents.begin(project.id, project.display_name, profile_id)

        intent_id = str(intent["intent_id"])
        # W2-001: exactly one writer may hold the pre-dispatch claim. A fresh
        # intent, a resumed interrupted intent, and a concurrent duplicate all
        # pass through here; only the first is granted and the rest stand down
        # without packing or submitting.
        claimed, intent = self.intents.claim_or_takeover(intent_id)
        if not claimed:
            return AuditStartResult(
                True, project.id, intent_id,
                str(intent.get("dispatch_id") or ""),
                str(intent.get("status") or "PREPARING"),
                "Audit run is already in progress", True,
            )
        my_fence = intent.get("fence")
        self.intents.update(intent_id, status="PREPARING", error="", owner_pid=os.getpid(), expect_fence=my_fence)
        try:
            health = self._healthy_bridge_status()
            if provision and self.workers is not None:
                if should_abort and should_abort():
                    return self._settled_by_close(project.id, intent_id, my_fence,
                                                  "before worker provisioning")
                dispatch_status = (health.get("browser") or {}) if isinstance(health, dict) else {}
                demand = (
                    int(dispatch_status.get("queued_jobs", 0) or 0)
                    + int(dispatch_status.get("active_jobs", 0) or 0)
                    + 1
                )
                self.workers.ensure_capacity(dispatch_status, demand)
            self.intents.update(intent_id, status="PACKING", expect_fence=my_fence)
            # Freshness is an mtime comparison, and mtime lies often enough to
            # matter: a restored file, a clock skew or a changed exclude list
            # all leave a stale archive looking current, and the operator then
            # waits out a full audit of code they have already moved past.
            audits_cfg = getattr(getattr(self.projects, "config", None), "audits", None)
            if bool(getattr(audits_cfg, "autopack_before_audit", True)):
                packed = self.packing.pack_project(project.id)
            else:
                packed = self.packing.ensure_fresh_archive(project.id)
            if not packed.success or not packed.output_path:
                raise RuntimeError(packed.error_message or "Packing failed")
            if should_abort and should_abort():
                return self._settled_by_close(project.id, intent_id, my_fence,
                                              "before the dispatch was submitted")
            self.intents.update(intent_id, status="SUBMITTING", archive_path=str(Path(packed.output_path).resolve()), expect_fence=my_fence)
            # Past this line the dispatch exists and will run; nothing below may
            # re-interpret it as cancelled.
            response = self.bridge.submit_browser_audit(project, packed.output_path, profile_id)
            dispatch = response.get("dispatch", {}) if response.get("ok") else {}
            dispatch_id = str(dispatch.get("dispatch_id") or "")
            if not dispatch_id:
                # A submission whose RESPONSE was lost is not a failed audit.
                # Observed live during a six-project batch: the POST came back
                # "Remote end closed connection without response" while the
                # Bridge had already enqueued the job and a worker was running
                # it. Reporting FAILED there puts a lane on the board as failed
                # while its audit is live, and hides the real dispatch from
                # cancel/recover. Ask the Bridge what actually exists.
                adopted = self.bridge.active_browser_job(project.id) or {}
                dispatch_id = str(adopted.get("dispatch_id") or "")
                if not dispatch_id:
                    raise RuntimeError(
                        str(response.get("error") or "Bridge rejected audit dispatch")
                        if not response.get("ok")
                        else "Bridge returned no dispatch identity"
                    )
                self.intents.update(intent_id, status="QUEUED", dispatch_id=dispatch_id, expect_fence=my_fence,
                                    claim_token="", claim_holder_pid=None)
                return AuditStartResult(
                    True, project.id, intent_id, dispatch_id,
                    str(adopted.get("state") or "QUEUED"),
                    "Audit queued (adopted after a lost submit response)",
                )
            self.intents.update(intent_id, status="QUEUED", dispatch_id=dispatch_id, expect_fence=my_fence,
                                claim_token="", claim_holder_pid=None)
            return AuditStartResult(True, project.id, intent_id, dispatch_id, "QUEUED", "Audit queued")
        except Exception as exc:
            try:
                self.intents.update(intent_id, status="FAILED", error=str(exc)[:500], expect_fence=my_fence,
                                    claim_token="", claim_holder_pid=None)
            except StaleClaimError:
                # A takeover happened while this writer was failing; the new
                # owner's durable state must not be overwritten by a stale loser.
                pass
            return AuditStartResult(False, project.id, intent_id, state="FAILED", message=str(exc))

    RESET_SETTLED_STATES = frozenset({"READY", "FAILED", "CANCELLED", "SUPERSEDED"})

    def reset_all(self, project_ids: Optional[Iterable[str]] = None) -> dict[str, Any]:
        """Clear every non-terminal run in one operator action.

        Resetting a jammed board one lane at a time is busywork, and a pre-start
        BLOCKED run refuses Cancel, so the operator had to know which button each
        lane needed. This picks per lane: Cancel where cancellation is legal,
        FORCE UNBLOCK where it is not.
        """
        cancelled: list[str] = []
        unblocked: list[str] = []
        failed: list[str] = []

        for snapshot in self.refresh_runs(project_ids):
            if snapshot.operator_state in self.RESET_SETTLED_STATES:
                continue
            label = snapshot.project_name or snapshot.project_id

            if not snapshot.dispatch_id:
                # An intent that never reached the Bridge is cleared directly.
                if snapshot.intent_id:
                    self.intents.update(snapshot.intent_id, status="CANCELLED")
                    cancelled.append(label)
                continue

            post_start = snapshot.operator_state in {"BLOCKED_POST_START", "RECOVERY"}
            if post_start:
                result = self.abandon(snapshot.dispatch_id, "operator reset all audit runs")
                (unblocked if result.ok else failed).append(label)
                continue

            result = self.cancel(snapshot.dispatch_id)
            if result.ok:
                cancelled.append(label)
                continue
            # Cancel refuses a BLOCKED dispatch; force it terminal instead.
            forced = self.abandon(snapshot.dispatch_id, "operator reset all audit runs")
            if forced.ok:
                unblocked.append(label)
                continue
            # Neither would move it, which for a dispatch that is already
            # terminal or already pruned is the correct refusal -- the run is
            # over. Settling the intent is what stops it reappearing in the
            # next "unfinished" count forever; without it RESET ALL reports the
            # same lanes on every press and clears nothing.
            if snapshot.intent_id:
                self.intents.update(snapshot.intent_id, status="CANCELLED")
                cancelled.append(label)
                continue
            failed.append(label)

        return {
            "cancelled": cancelled,
            "unblocked": unblocked,
            "failed": failed,
            "total": len(cancelled) + len(unblocked) + len(failed),
        }

    def start_batch(self, project_ids: Iterable[str], profile_id: str = "quick3",
                    should_abort: Optional[Callable[[], bool]] = None) -> list[AuditStartResult]:
        # No lane cap on the QUEUE. The pool holds the waiting jobs and a window
        # that frees up claims the next one, so asking for more projects than
        # there are windows is a line, not an overflow. Only the WINDOWS are
        # capped, inside provision_capacity.
        unique = list(dict.fromkeys(str(value) for value in project_ids if str(value)))
        if not unique:
            return []
        # Windows first, for the whole batch, before the first archive is
        # packed: browser boot and packing then overlap instead of queueing
        # behind each other. A provisioning failure is never fatal here -- the
        # Bridge supervisor keeps provisioning, and a queued job with no window
        # yet is a wait, not a loss.
        aborted = bool(should_abort and should_abort())
        provisioned = False
        if not aborted:
            try:
                self.provision_capacity(len(unique))
                provisioned = True
            except Exception:
                provisioned = False
        return [self.start(project_id, profile_id, provision=not provisioned,
                           should_abort=should_abort) for project_id in unique]

    def reorder(self, dispatch_id: str, delta: int) -> AuditStartResult:
        """Move a waiting run up (-1) or down (+1) the line for the next window.

        The pool already holds more jobs than there are windows and a freed
        window claims the next one; this decides which one that is.
        """
        response = self.bridge.reorder_browser_job(str(dispatch_id), int(delta))
        intent = self.intents.find_for_dispatch(str(dispatch_id))
        project_id = str((intent or {}).get("project_id") or "")
        intent_id = str((intent or {}).get("intent_id") or "")
        if response.get("ok"):
            place = list(response.get("order") or []).index(str(dispatch_id)) + 1                 if str(dispatch_id) in list(response.get("order") or []) else 0
            return AuditStartResult(
                True, project_id, intent_id, str(dispatch_id), "WAITING",
                f"Now {place} in the queue" if place else "Queue reordered",
            )
        error = response.get("error") or "Bridge refused the reorder"
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "Bridge refused the reorder"
        return AuditStartResult(False, project_id, intent_id, str(dispatch_id), "WAITING", str(error))

    def cancel(self, dispatch_id: str) -> AuditStartResult:
        response = self.bridge.cancel_browser_job(str(dispatch_id))
        intent = self.intents.find_for_dispatch(str(dispatch_id))
        project_id = str((intent or {}).get("project_id") or "")
        if response.get("ok"):
            if intent:
                self.intents.update(str(intent["intent_id"]), status="CANCELLED")
            return AuditStartResult(True, project_id, str((intent or {}).get("intent_id") or ""), str(dispatch_id), "CANCELLED", "Audit cancelled")
        error = response.get("error") or "Bridge rejected cancellation"
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "Bridge rejected cancellation"
        return AuditStartResult(False, project_id, str((intent or {}).get("intent_id") or ""), str(dispatch_id), "BLOCKED", str(error))

    def abandon(self, dispatch_id: str, reason: str = "") -> AuditStartResult:
        """Stop a live post-START run, or force a stuck BLOCKED one terminal.

        Cancel refuses a post-start dispatch because CANCELLED asserts no Core
        was sent. Abandon is the honest terminal: the lane frees up, the record
        stays FAILED with operator_abandoned, and no second START is issued
        automatically.

        The bridge stays authoritative about the terminal state. abandon_job()
        is idempotent and returns an already-terminal job UNCHANGED, so an
        audit that reached COMPLETE between the click and this call must NOT be
        rewritten to FAILED here: that would relabel a finished run whose waves
        are already saved, and hide a real result behind a stop pressed too late.
        """
        response = self.bridge.abandon_browser_job(str(dispatch_id), reason)
        intent = self.intents.find_for_dispatch(str(dispatch_id))
        project_id = str((intent or {}).get("project_id") or "")
        intent_id = str((intent or {}).get("intent_id") or "")
        if response.get("ok"):
            terminal = str(response.get("state") or "FAILED").strip().upper() or "FAILED"
            if terminal != "FAILED":
                return AuditStartResult(
                    True, project_id, intent_id, str(dispatch_id), terminal,
                    f"Run already reached {terminal}; nothing was stopped",
                )
            if intent:
                self.intents.update(intent_id, status="FAILED", error="operator abandoned a stuck blocked run")
            return AuditStartResult(True, project_id, intent_id, str(dispatch_id), "FAILED", "Run abandoned; project is free again")
        error = response.get("error") or "Bridge refused abandon"
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "Bridge refused abandon"
        return AuditStartResult(False, project_id, intent_id, str(dispatch_id), "BLOCKED", str(error))

    @staticmethod
    def audit_matches_dispatch(job: dict[str, Any], audit: Optional[AuditSnapshot]) -> bool:
        """True only when the durable audit snapshot belongs to this dispatch lineage.

        A new run must never display wave progress inherited from a previous
        campaign_run_id. Before the dispatch establishes its own campaign
        identity, no historical snapshot counts as current progress.
        """
        if audit is None:
            return False
        if str(audit.project_id) != str(job.get("project_id")):
            return False
        dispatch_run = str(job.get("campaign_run_id") or "")
        if not dispatch_run:
            return False
        # Same lineage, two ids: ChatGPT route hydration re-derives the widget's
        # run id, and the Bridge records the id the campaign was actually saved
        # under when it closes the lane. Without accepting it a finished
        # campaign reported 0/3 waves next to its own READY handoff.
        drift_run = str(job.get("meta_run_id_drift") or "")
        return str(audit.campaign_run_id or "") in {dispatch_run, drift_run} - {""}

    @staticmethod
    def _ready_proof(job: dict[str, Any], audit: Optional[AuditSnapshot]) -> tuple[bool, tuple[str, ...], str, str]:
        proof: list[str] = []
        if str(job.get("state")) != "COMPLETE":
            return False, (), "", ""
        proof.append("dispatch_complete")
        if audit is None or str(audit.project_id) != str(job.get("project_id")):
            return False, tuple(proof), "", ""
        proof.append("project_match")
        dispatch_run = str(job.get("campaign_run_id") or "")
        audit_run = str(audit.campaign_run_id or "")
        # ChatGPT route hydration re-arms the widget's runtime and re-derives
        # the campaign run id, so a finished campaign can be saved under an id
        # the dispatch never saw. The Bridge records that drift when it
        # reconciles, and it is the same run: without this a completed audit --
        # 3/3 waves and a valid canonical handoff on disk -- could never become
        # READY, which is the whole point of the lane.
        drift_run = str(job.get("meta_run_id_drift") or "")
        if not dispatch_run or audit_run not in {dispatch_run, drift_run} or not audit_run:
            return False, tuple(proof), "", ""
        proof.append("campaign_match" if audit_run == dispatch_run else "campaign_match_via_drift")
        if not audit.campaign_complete or audit.completed_waves != audit.total_waves or audit.total_waves <= 0:
            return False, tuple(proof), "", ""
        proof.append("waves_complete")
        if not audit.final_handoff_ready or audit.final_handoff_path is None:
            return False, tuple(proof), "", ""
        audit_path = Path(audit.final_handoff_path)
        recorded_path = str(job.get("final_handoff_path") or "")
        try:
            if not audit_path.is_file():
                return False, tuple(proof), "", ""
            # The dispatch only learns the handoff path and digest from the
            # terminal ACK. When that ACK never landed the record is blank, and
            # demanding it made a durable, verified artifact unreachable. Where
            # the dispatch DOES carry them they must still agree exactly.
            if recorded_path and Path(recorded_path).resolve() != audit_path.resolve():
                return False, tuple(proof), "", ""
            digest = _sha256_file(audit_path)
        except OSError:
            return False, tuple(proof), "", ""
        expected = str(audit.final_handoff_sha256 or "").lower()
        dispatch_digest = str(job.get("final_handoff_sha256") or "").lower()
        if not expected or digest != expected:
            return False, tuple(proof), "", ""
        if dispatch_digest and digest != dispatch_digest:
            return False, tuple(proof), "", ""
        proof.append("handoff_durable")
        proof.append("handoff_hash_match" if dispatch_digest else "handoff_hash_index_only")
        return True, tuple(proof), str(audit_path), digest

    @staticmethod
    def _complete_may_still_settle(job: dict[str, Any]) -> bool:
        """Could this COMPLETE dispatch still become READY, or is it over?

        One precise signal, not a timeout: the artifact this run names still
        exists, but hashes to something else. Only a LATER run for the same
        project writes that canonical path, so this run's recorded digest can
        never match again and no amount of waiting will make it READY.

        A handoff that is simply absent is a different failure -- the result
        was never written, which is worth an operator's attention rather than
        being quietly settled -- and it deliberately stays SAVING.
        """
        path = str(job.get("final_handoff_path") or "")
        digest = str(job.get("final_handoff_sha256") or "")
        if path and digest:
            artifact = Path(path)
            try:
                if artifact.is_file() and _sha256_file(artifact) != digest:
                    return False
            except OSError:
                pass
        # The digest can still match while the proof fails for another reason --
        # the audit index holding a later run, or a drifted campaign identity --
        # and those never resolve either. Age is the only thing that separates
        # "finalization is still writing" from "this is not coming". A run with
        # no completion time recorded is left alone: unknown is not old.
        completed_at = float(job.get("completed_at") or 0.0)
        if completed_at and (time.time() - completed_at) > COMPLETE_SETTLE_AFTER_SECONDS:
            return False
        return True

    def _snapshot(
        self,
        job: dict[str, Any],
        intent: Optional[dict[str, Any]],
        audit: Optional[AuditSnapshot],
        worker_labels: dict[str, str],
        bridge_context: dict[str, Any],
    ) -> AuditRunSnapshot:
        state = str(job.get("state") or "")
        ready, proof, handoff_path, handoff_hash = self._ready_proof(job, audit)
        matching = self.audit_matches_dispatch(job, audit)
        completed = int(getattr(audit, "completed_waves", 0) or 0) if matching else 0
        # The DISPATCH knows its profile from the moment it is queued; the audit
        # index only learns the wave count once a wave has been saved. Falling
        # back to a hardcoded 3 made every A10 lane read "AUDIT 0/3" for the
        # whole of wave 1 -- which on a real repo is 15-25 minutes of a ten-wave
        # run looking like a stalled three-wave one. Six healthy lanes were
        # RESET ALL'd over exactly that.
        total = int(getattr(audit, "total_waves", 0) or 0) if matching and audit else 0
        if not total:
            total = _profile_wave_count(
                str(job.get("profile") or (intent or {}).get("profile_id") or "")
            )
        mapping = {
            "QUEUED": "WAITING", "RETRYABLE": "RETRYING",
            "LEASED": "ATTACHING", "ARTIFACT_FETCHED": "ATTACHING", "ATTACHED": "ATTACHING",
            "START_PREPARED": "STARTING", "STARTED": "AUDITING", "AUDITING": "AUDITING",
            "FINALIZING": "SAVING", "FAILED": "FAILED", "CANCELLED": "CANCELLED",
        }
        recovery_state = str(job.get("recovery_state") or "")
        post_start_block = bool(
            recovery_state in {"START_PREPARED", "STARTED", "AUDITING", "FINALIZING"}
            or job.get("start_receipt")
            or job.get("campaign_run_id")
        )
        if state == "BLOCKED":
            operator = "BLOCKED_POST_START" if post_start_block else "BLOCKED_PRE_START"
        elif state == "COMPLETE" and not ready:
            # Transport finished and the proof did not pass. SAVING is the
            # honest transient right after completion -- finalization is still
            # writing. It is NOT honest forever: 13 of 32 COMPLETE dispatches
            # here point at a canonical artifact a LATER run for the same
            # project has since overwritten, so their recorded digest can never
            # match again. Those read SAVING permanently, RESET ALL counted
            # them as unfinished, and Yes could not clear them because a
            # terminal dispatch refuses both Cancel and FORCE UNBLOCK -- the
            # same 16 came back on every press.
            operator = "SAVING" if self._complete_may_still_settle(job) else "SUPERSEDED"
        else:
            operator = "READY" if ready else mapping.get(state, "PREPARING")
        error = str(job.get("error") or job.get("last_error_code") or "")
        worker_id = str(job.get("assigned_worker_id") or "")
        if operator == "READY":
            summary = f"AUDIT READY ✓ · {completed}/{total}"
        elif operator == "AUDITING":
            summary = f"AUDIT {completed}/{total}"
        elif operator == "WAITING":
            summary = f"WAITING FOR WORKER · {completed}/{total}"
        elif operator == "RETRYING":
            summary = f"RETRYING {int(job.get('retry_count') or 0)}/5 · {error or 'pre-start retry'}"
        elif operator == "SAVING":
            summary = f"SAVING · {completed}/{total}"
        elif operator == "SUPERSEDED":
            summary = "SUPERSEDED · a later run for this project replaced the result"
        elif operator in {"FAILED", "BLOCKED_PRE_START", "BLOCKED_POST_START"}:
            label = "BLOCKED PRE-START" if operator == "BLOCKED_PRE_START" else ("BLOCKED POST-START" if operator == "BLOCKED_POST_START" else "FAILED")
            _why, _action = blocked_guidance(error, operator == "BLOCKED_POST_START")
            summary = f"{label}: {error or 'no reason code'} · NEXT: {_action}"
        else:
            summary = f"{operator} · {completed}/{total}"
        intent_id = str((intent or {}).get("intent_id") or "")
        return AuditRunSnapshot(
            project_id=str(job.get("project_id") or (intent or {}).get("project_id") or ""),
            project_name=str(job.get("project_name") or (intent or {}).get("project_name") or ""),
            operator_state=operator,
            summary=summary,
            intent_id=intent_id,
            dispatch_id=str(job.get("dispatch_id") or ""),
            dispatch_state=state,
            worker_id=worker_id,
            worker_label=worker_labels.get(worker_id, ""),
            profile_id=str((intent or {}).get("profile_id") or getattr(audit, "audit_profile_id", "quick3") or "quick3"),
            campaign_run_id=str(job.get("campaign_run_id") or ""),
            audit_campaign_run_id=str(getattr(audit, "campaign_run_id", "") or ""),
            completed_waves=completed,
            total_waves=total,
            ready=ready,
            ready_proof=proof,
            handoff_path=handoff_path,
            handoff_sha256=handoff_hash,
            error=error,
            recovery=recovery_state,
            retry_count=int(job.get("retry_count") or 0),
            last_error_code=str(job.get("last_error_code") or ""),
            conversation_locator=str(job.get("conversation_id") or ""),
            bridge_healthy=bool(bridge_context.get("healthy")),
            worker_counts=dict(bridge_context.get("worker_counts") or {}),
            handoff_present=bool(handoff_path and Path(handoff_path).is_file()),
            created_at=float(job.get("created_at") or (intent or {}).get("created_at") or 0.0),
            updated_at=float(job.get("updated_at") or (intent or {}).get("updated_at") or 0.0),
            completed_at=float(job.get("completed_at") or (intent or {}).get("completed_at") or 0.0),
            actions=_actions_for(operator),
            # Zero is the FRONT of the line, not a missing value: `or -1` here
            # hid the one row the operator cares most about.
            queue_position=_as_queue_position(job.get("queue_position")),
        )

    #: A finished intent's true operator state. Everything not in
    #: ACTIVE_INTENT_STATES is finished, so this map is exhaustive by
    #: construction and an unknown settled status falls to FAILED rather than
    #: pretending to be live work.
    _SETTLED_INTENT_STATES = {"READY": "READY", "COMPLETE": "READY", "CANCELLED": "CANCELLED"}

    def _intent_snapshot(self, intent: dict[str, Any]) -> AuditRunSnapshot:
        raw = str(intent.get("status") or "PREPARING")
        error = str(intent.get("error") or "")
        # An intent whose dispatch record is gone falls through to here, and
        # every settled status except FAILED used to collapse into PREPARING --
        # so 16 finished runs read as live work, RESET ALL offered to "clear 16
        # unfinished audit run(s)", and Yes cleared nothing: their dispatches
        # are terminal, so Cancel refuses, FORCE UNBLOCK refuses, and the same
        # 16 came back on the next refresh. Finished is finished.
        if raw not in ACTIVE_INTENT_STATES:
            operator = self._SETTLED_INTENT_STATES.get(raw, "FAILED")
            summary = (
                f"{operator}: {error}" if error
                else "FINISHED · no dispatch record on the Bridge"
                if operator == "READY" else operator
            )
            return AuditRunSnapshot(
                project_id=str(intent.get("project_id") or ""),
                project_name=str(intent.get("project_name") or ""),
                operator_state=operator,
                summary=summary,
                intent_id=str(intent.get("intent_id") or ""),
                profile_id=str(intent.get("profile_id") or "quick3"),
                error=error,
                created_at=float(intent.get("created_at") or 0.0),
                updated_at=float(intent.get("updated_at") or 0.0),
                completed_at=float(intent.get("completed_at") or 0.0),
                actions=_actions_for(operator),
            )
        interrupted = raw in {"PREPARING", "PACKING", "SUBMITTING"} and int(intent.get("owner_pid", 0) or 0) not in {0, os.getpid()}
        operator = "INTERRUPTED" if interrupted else (
            "RECOVERY" if raw == "RECOVERY_NEEDED" else "PREPARING"
        )
        if operator == "INTERRUPTED":
            summary = "INTERRUPTED · Resume Start"
        else:
            summary = f"{operator}: {error}" if error else f"{operator} · 0/3"
        return AuditRunSnapshot(
            project_id=str(intent.get("project_id") or ""),
            project_name=str(intent.get("project_name") or ""),
            operator_state=operator,
            summary=summary,
            intent_id=str(intent.get("intent_id") or ""),
            profile_id=str(intent.get("profile_id") or "quick3"),
            error=error,
            created_at=float(intent.get("created_at") or 0.0),
            updated_at=float(intent.get("updated_at") or 0.0),
            completed_at=float(intent.get("completed_at") or 0.0),
            actions=_actions_for(operator),
        )

    def _stamp_agent_state(
        self,
        snapshot: AuditRunSnapshot,
        memo: Optional[dict[tuple[str, str], Any]] = None,
        authoritative: bool = False,
    ) -> AuditRunSnapshot:
        """Answer 'has the agent read this yet' from the project's own inbox.

        Best effort by design: a project with no SAIPEN, no mirror or an
        unreadable tree simply reads NO_INBOX. A dashboard field must never be
        able to fail a refresh.

        PERF-005 (audit/4.md): ``memo`` is a request-local cache keyed by
        (root, binding). A composite refresh can hold several retained records
        of the same project, and stamping each one used to re-run the identical
        filesystem fingerprint. The caller passes one memo for the whole
        refresh so a project's inbox is read once; the key includes the root
        and binding so one project's verdict is never reused for another.

        PERF-004 (audit/10.md): the default path is the PASSIVE dashboard layer
        (``read_inbox_passive``), so a 4-second repaint reuses a settled verdict
        inside its freshness budget instead of re-enumerating every inbox.
        ``authoritative=True`` selects the immediate-change layer
        (``read_inbox_cached``), which fingerprints on every call and must be
        used by any caller whose correctness depends on current inbox truth.

        AuditRunSnapshot is frozen by design, so the verdict is applied with
        ``dataclasses.replace`` and the enriched copy is returned -- callers
        must use the return value, never assume the input was mutated.
        """
        try:
            project = self.projects.get_project(str(snapshot.project_id))
            root = str(getattr(project, "source_path", "") or "") if project else ""
            if not root:
                return snapshot
            audits = getattr(getattr(self.projects, "config", None), "audits", None)
            binding = str(getattr(audits, "agent_receipt_path", "") or "")
            key = (root, binding)
            state = memo.get(key) if memo is not None else None
            if state is None:
                reader = (
                    agent_inbox.read_inbox_cached if authoritative
                    else agent_inbox.read_inbox_passive
                )
                state = reader(root, binding_rel=binding)
                if memo is not None:
                    memo[key] = state
            return replace(
                snapshot,
                agent_state=state.verdict,
                agent_summary=state.summary(),
                agent_guidance=state.guidance,
                agent_residue=len(state.residue),
            )
        except Exception:
            return snapshot

    def refresh_runs(
        self,
        project_ids: Optional[Iterable[str]] = None,
        status_response: Optional[dict[str, Any]] = None,
        authoritative_inbox: bool = False,
    ) -> list[AuditRunSnapshot]:
        """The dashboard's composite view of every run.

        ``status_response`` lets a caller that already asked the Bridge for
        `/v1/browser/status` hand that snapshot in instead of causing a second
        identical request for the same repaint (PERF-002).

        ``authoritative_inbox`` (PERF-004) selects the immediate-change inbox
        reader for callers whose correctness depends on current truth (for
        example an operator action that consumes the verdict). The default
        passive path reuses a settled verdict inside its freshness budget, so
        the periodic 4-second repaint does not re-enumerate every project's
        inbox.
        """
        selected = {str(value) for value in project_ids} if project_ids is not None else None
        jobs_response = self.bridge.browser_jobs()
        if not jobs_response.get("ok"):
            return [self._intent_snapshot(item) for item in reversed(self.intents.list()) if selected is None or str(item.get("project_id")) in selected]
        if status_response is None:
            status_response = self.bridge.browser_status()
        workers = (status_response.get("dispatch") or {}).get("workers", []) if status_response.get("ok") else []
        labels: dict[str, str] = {}
        counts: dict[str, int] = {}
        for worker in workers:
            name = str(worker.get("browser_name") or "Browser")
            counts[name] = counts.get(name, 0) + 1
            labels[str(worker.get("worker_id") or "")] = f"{name} #{counts[name]}"
        dispatch_status = status_response.get("dispatch") or {}
        bridge_context = {
            "healthy": bool(status_response.get("ok")),
            "worker_counts": {
                "active": int(dispatch_status.get("active_workers", 0) or 0),
                "clean": int(dispatch_status.get("clean_workers", 0) or 0),
                "busy": int(dispatch_status.get("busy_workers", 0) or 0),
                "offline": int(dispatch_status.get("offline_workers", 0) or 0),
            },
        }
        intents = self.intents.list()
        by_dispatch = {str(item.get("dispatch_id")): item for item in intents if item.get("dispatch_id")}
        seen_intents: set[str] = set()
        audit_cache: dict[str, Optional[AuditSnapshot]] = {}
        intent_updates: dict[str, dict[str, Any]] = {}
        snapshots: list[AuditRunSnapshot] = []
        # PERF-005: one agent-inbox read per (root, binding) per refresh -- not
        # one per retained record. Multiple rows of the same project all want
        # the same current verdict.
        agent_memo: dict[tuple[str, str], Any] = {}
        jobs = sorted(jobs_response.get("jobs", []), key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
        # PERF-001: separate CURRENT (non-terminal) work from TERMINAL history
        # BEFORE enrichment. Every non-terminal dispatch must remain represented
        # to the state consumer and queue controls. RUN_HISTORY_BOUND applies
        # only to terminal history, not to the composite current-state surface.
        current_jobs = [j for j in jobs if selected is None or str(j.get("project_id") or "") in selected]
        # Partition into current and terminal before enrichment
        terminal_jobs: list[dict[str, Any]] = []
        for job in current_jobs:
            dispatch_state = str(job.get("state") or "")
            if dispatch_state in TERMINAL_DISPATCH_STATES:
                terminal_jobs.append(job)
        # PERF-001: enrich only terminal jobs up to RUN_HISTORY_BOUND (terminal
        # history is bounded independently from current work).
        terminal_jobs.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
        terminal_jobs = terminal_jobs[:RUN_HISTORY_BOUND]

        audit_cache: dict[str, Optional[AuditSnapshot]] = {}
        intent_updates: dict[str, dict[str, Any]] = {}
        snapshots: list[AuditRunSnapshot] = []
        # PERF-005: one agent-inbox read per (root, binding) per refresh -- not
        # one per retained record. Multiple rows of the same project all want
        # the same current verdict.
        # Enrich current (non-terminal) jobs first -- they have priority in the
        # returned view and must never be hidden behind terminal history.
        for job in current_jobs:
            if str(job.get("state") or "") in TERMINAL_DISPATCH_STATES:
                continue
            project_id = str(job.get("project_id") or "")
            if project_id not in audit_cache:
                # PERF-002: `refresh_project()` means force_rescan, which
                # INVALIDATES the AuditIndexer before scanning -- so the periodic
                # dashboard threw away a working directory-signature cache on
                # every tick and re-read every wave file of every project with a
                # retained job, changed or not. Measured over 100 settled
                # projects: 44.18 ms and 300 file reads forced, versus 2.87 ms
                # and zero reads cached. Real audit writes still arrive: the
                # indexer's signature check sees them, and generation/watcher
                # events invalidate the exact project that changed.
                audit_cache[project_id] = self.audits.get_snapshot(project_id)
            intent = by_dispatch.get(str(job.get("dispatch_id") or ""))
            snapshot = self._snapshot(job, intent, audit_cache[project_id], labels, bridge_context)
            snapshot = self._stamp_agent_state(snapshot, agent_memo, authoritative=authoritative_inbox)
            snapshots.append(snapshot)
            if intent:
                seen_intents.add(str(intent.get("intent_id")))
                intent_state = "READY" if snapshot.ready else (
                    snapshot.operator_state if snapshot.operator_state in {"FAILED", "CANCELLED", "RECOVERY"} else (
                        "RUNNING" if snapshot.dispatch_state in ACTIVE_DISPATCH_STATES else snapshot.dispatch_state
                    )
                )
                if intent_state == "RECOVERY":
                    intent_state = "RECOVERY_NEEDED"
                # A BLOCKED dispatch used to land here as "RUNNING" because
                # BLOCKED is an ACTIVE_DISPATCH_STATE. The intent then stayed in
                # ACTIVE_INTENT_STATES forever, so intents.begin() refused every
                # later START for that project and the lane read as a live run
                # that would never finish. Split the two real cases instead.
                if snapshot.operator_state == "BLOCKED_PRE_START":
                    intent_state = "BLOCKED"
                elif snapshot.operator_state == "BLOCKED_POST_START":
                    intent_state = "RECOVERY_NEEDED"
                intent_updates[str(intent["intent_id"])] = {
                    "status": intent_state,
                    "campaign_run_id": snapshot.campaign_run_id,
                    "error": snapshot.error,
                }
        # Enrich terminal jobs up to the terminal-history bound only.
        for job in terminal_jobs:
            project_id = str(job.get("project_id") or "")
            if selected is not None and project_id not in selected:
                continue
            if project_id not in audit_cache:
                audit_cache[project_id] = self.audits.get_snapshot(project_id)
            intent = by_dispatch.get(str(job.get("dispatch_id") or ""))
            snapshot = self._snapshot(job, intent, audit_cache[project_id], labels, bridge_context)
            snapshot = self._stamp_agent_state(snapshot, agent_memo, authoritative=authoritative_inbox)
            snapshots.append(snapshot)
            if intent:
                seen_intents.add(str(intent.get("intent_id")))
                intent_state = "READY" if snapshot.ready else (
                    snapshot.operator_state if snapshot.operator_state in {"FAILED", "CANCELLED", "RECOVERY"} else (
                        "RUNNING" if snapshot.dispatch_state in ACTIVE_DISPATCH_STATES else snapshot.dispatch_state
                    )
                )
                if intent_state == "RECOVERY":
                    intent_state = "RECOVERY_NEEDED"
                # A BLOCKED dispatch used to land here as "RUNNING" because
                # BLOCKED is an ACTIVE_DISPATCH_STATE. The intent then stayed in
                # ACTIVE_INTENT_STATES forever, so intents.begin() refused every
                # later START for that project and the lane read as a live run
                # that would never finish. Split the two real cases instead.
                if snapshot.operator_state == "BLOCKED_PRE_START":
                    intent_state = "BLOCKED"
                elif snapshot.operator_state == "BLOCKED_POST_START":
                    intent_state = "RECOVERY_NEEDED"
                intent_updates[str(intent["intent_id"])] = {
                    "status": intent_state,
                    "campaign_run_id": snapshot.campaign_run_id,
                    "error": snapshot.error,
                }
        # One lock, one parse, and a write only if a status actually moved.
        self.intents.reconcile(intent_updates)
        for intent in reversed(intents):
            if str(intent.get("intent_id")) in seen_intents:
                continue
            if selected is not None and str(intent.get("project_id")) not in selected:
                continue
            snapshots.append(self._stamp_agent_state(self._intent_snapshot(intent), agent_memo, authoritative=authoritative_inbox))
        return snapshots

    def latest_by_project(self, project_ids: Optional[Iterable[str]] = None) -> dict[str, AuditRunSnapshot]:
        result: dict[str, AuditRunSnapshot] = {}
        for snapshot in self.refresh_runs(project_ids):
            result.setdefault(snapshot.project_id, snapshot)
        return result

    @staticmethod
    def diagnostics(snapshot: AuditRunSnapshot) -> str:
        """Redacted support record: identities and state only, never tokens or content."""
        from audapack import __version__

        doc = {
            "schema_version": 1,
            "audapack_version": __version__,
            "project_id": snapshot.project_id,
            "project_name": snapshot.project_name,
            "intent_id": snapshot.intent_id,
            "dispatch_id": snapshot.dispatch_id,
            "dispatch_state": snapshot.dispatch_state,
            "operator_state": snapshot.operator_state,
            "worker_label": snapshot.worker_label,
            "profile_id": snapshot.profile_id,
            "campaign_run_id": snapshot.campaign_run_id,
            "audit_campaign_run_id": snapshot.audit_campaign_run_id,
            "waves": [snapshot.completed_waves, snapshot.total_waves],
            "ready": snapshot.ready,
            "ready_proof": list(snapshot.ready_proof),
            "retry_count": snapshot.retry_count,
            "last_error_code": snapshot.last_error_code,
            "bridge_healthy": snapshot.bridge_healthy,
            "worker_counts": snapshot.worker_counts,
            "conversation_locator": snapshot.conversation_locator,
            "final_handoff_present": snapshot.handoff_present,
            "handoff_filename": _basename_any_platform(snapshot.handoff_path),
            "handoff_sha256": snapshot.handoff_sha256,
            "error": _redact_diagnostic_text(snapshot.error, 500),
            "recovery": _redact_diagnostic_text(snapshot.recovery, 200),
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "completed_at": snapshot.completed_at,
        }
        return json.dumps(doc, ensure_ascii=False, indent=2)
