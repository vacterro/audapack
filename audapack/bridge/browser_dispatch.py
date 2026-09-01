"""Browser audit worker dispatcher -- broker / scheduler / lease authority.

SRC-005: AUDAPACK -> FREE CHROMIUM AUDIT WORKER DISPATCHER.

The desktop is the producer, the localhost Bridge is the broker, and every
AUDAPACK_WIDGET.user.js tab is a browser worker that PULLS work from the
Bridge. This module owns the pure dispatch domain:

  * ephemeral worker registry (heartbeat TTL expires stale workers)
  * durable job queue (survives AUDAPACK/Bridge restart)
  * atomic lease claim (two workers can never receive the same project)
  * validated job lifecycle transitions
  * deterministic scheduling (FIFO jobs + least-recently-assigned worker)

Design rules from the spec this module enforces:

  * dispatch_id is the delivery identity; it NEVER replaces CAMPAIGN_RUN_ID,
    which remains the authority of the existing audit engine.
  * exactly-once START: once a job reaches START_PREPARED the owning worker
    must recover that exact send; a dead lease may return a job to QUEUED only
    before that boundary.
  * hard upper bound of MAX_ACTIVE_WORKERS; the dispatcher never invents a
    seventh worker.
  * every state-changing request must carry dispatch_id + worker_id + lease_id;
    a stale owner is rejected.
  * the artifact is a server-owned path from the packing result; the browser
    never supplies a filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from audapack.config import cross_process_lock, get_state_dir

logger = logging.getLogger(__name__)

MAX_ACTIVE_WORKERS = 6
WORKER_TTL_SECONDS = 75
#: A worker id is per window session, so a reload or a hard navigation can
#: retire one id and register another. Requeuing the instant an id vanishes
#: handed the same job to a second window while the first still had the Core
#: prepared in its composer. Wait until the job itself has clearly stalled.
PRE_START_OWNER_GRACE_SECONDS = 45.0
#: How long a managed slot keeps its claim on a lane after its window's
#: last heartbeat. Longer than WORKER_TTL_SECONDS so a reload or a single
#: refused poll never hands the lane to an unmanaged tab.
MANAGED_SLOT_MEMORY_SECONDS = 150.0
LEASE_SECONDS = 180
QUEUE_BOUND = 200
HISTORY_BOUND = 100
PRE_START_MAX_RETRIES = 5
PRE_START_RETRY_BACKOFF_SECONDS = 5
PRE_START_RETRY_BACKOFF_MAX = 120
DISPATCH_ID_RE = re.compile(r"^dsp-[a-z0-9]{16}$")
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SUPPORTED_BROWSER_WIDGET_VERSION = "AUDAPACK_WIDGET/3"
INCOMPATIBLE_WIDGET_VERSIONS = {"AUDAPACK_WIDGET", "AUDAPACK_WIDGET/2"}


def _get_required_widget_build() -> str:
    """Read the @version of the bundled userscript on disk.

    T02: a stale widget build (0.0.22 vs current 0.0.24) used to look
    identical to the Bridge because the heartbeat reported only the
    protocol version. The required build is the @version of the
    userscript shipped with this Python package; stale tabs that still
    heartbeat a different @version are marked STALE_WIDGET and cannot
    claim audits.
    """
    try:
        from audapack.components.widget import read_bundled_widget_metadata
        meta = read_bundled_widget_metadata()
        ver = str(meta.get("version") or "").strip()
        if ver and ver != "0.0.01":
            return ver
    except Exception:
        pass
    return ""

# Worker lifecycle states (spec section 1).
WORKER_FREE = "FREE"
WORKER_RESERVED = "RESERVED"
WORKER_PREPARING = "PREPARING"
WORKER_UPLOADING = "UPLOADING"
WORKER_STARTING = "STARTING"
WORKER_AUDITING = "AUDITING"
WORKER_BLOCKED = "BLOCKED"
WORKER_OFFLINE = "OFFLINE"
WORKER_ACTIVE_STATES = {
    WORKER_FREE,
    WORKER_RESERVED,
    WORKER_PREPARING,
    WORKER_UPLOADING,
    WORKER_STARTING,
    WORKER_AUDITING,
    WORKER_BLOCKED,
}

# Job lifecycle (spec section 4).
JOB_QUEUED = "QUEUED"
JOB_LEASED = "LEASED"
JOB_ARTIFACT_FETCHED = "ARTIFACT_FETCHED"
JOB_ATTACHED = "ATTACHED"
JOB_START_PREPARED = "START_PREPARED"
JOB_STARTED = "STARTED"
JOB_AUDITING = "AUDITING"
JOB_FINALIZING = "FINALIZING"
JOB_COMPLETE = "COMPLETE"
JOB_RETRYABLE = "RETRYABLE"
JOB_BLOCKED = "BLOCKED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"

# Legal transitions, keyed by (from_state, to_state). Anything else refuses.
JOB_TRANSITIONS: set[tuple[str, str]] = {
    (JOB_QUEUED, JOB_LEASED),          # atomic claim
    (JOB_QUEUED, JOB_CANCELLED),       # operator cancel before claim
    (JOB_LEASED, JOB_ARTIFACT_FETCHED),
    (JOB_ARTIFACT_FETCHED, JOB_ATTACHED),
    (JOB_ATTACHED, JOB_RETRYABLE),
    (JOB_ATTACHED, JOB_START_PREPARED),
    (JOB_START_PREPARED, JOB_STARTED),
    (JOB_STARTED, JOB_AUDITING),
    # AUDITING is a progress marker, not a boundary: the irreversible Send
    # already happened at START_PREPARED. Its ACK is one HTTP call among six
    # windows sharing one serialized userscript request queue, and when it was
    # lost the run was pinned in STARTED -- from which FINALIZING and COMPLETE
    # were both illegal. The audit still ran and still landed on disk; its
    # dispatch simply could never close. A missing intermediate marker must
    # never invalidate a finished audit.
    (JOB_STARTED, JOB_FINALIZING),
    (JOB_STARTED, JOB_COMPLETE),
    (JOB_AUDITING, JOB_FINALIZING),
    (JOB_FINALIZING, JOB_COMPLETE),
    # Kept for wire compatibility with older workers. New workers must use
    # FINALIZING and the durable completion helper before terminal COMPLETE.
    (JOB_AUDITING, JOB_COMPLETE),
    (JOB_LEASED, JOB_RETRYABLE),       # lease expired pre-START_PREPARED
    (JOB_ARTIFACT_FETCHED, JOB_RETRYABLE),
    (JOB_RETRYABLE, JOB_QUEUED),       # safe redispatch only pre-START_PREPARED
    (JOB_LEASED, JOB_BLOCKED),         # worker lost post-claim, pre-START
    (JOB_ARTIFACT_FETCHED, JOB_BLOCKED),
    (JOB_ATTACHED, JOB_BLOCKED),
    (JOB_START_PREPARED, JOB_BLOCKED), # exactly-once: never re-leased
    (JOB_STARTED, JOB_BLOCKED),
    (JOB_AUDITING, JOB_BLOCKED),
    (JOB_BLOCKED, JOB_CANCELLED),
    (JOB_BLOCKED, JOB_FAILED),         # operator abandon: never re-leased, never re-STARTed
    (JOB_LEASED, JOB_FAILED),
    (JOB_ARTIFACT_FETCHED, JOB_FAILED),
    (JOB_ATTACHED, JOB_FAILED),
    (JOB_START_PREPARED, JOB_FAILED),
    (JOB_STARTED, JOB_FAILED),
    (JOB_AUDITING, JOB_FAILED),
    (JOB_RETRYABLE, JOB_BLOCKED),
}

# The exactly-once boundary: beyond this state a job is NEVER reassigned.
START_PREPARED_BOUNDARY = JOB_START_PREPARED

PRE_START_STATES = {JOB_LEASED, JOB_ARTIFACT_FETCHED, JOB_ATTACHED, JOB_RETRYABLE}
POST_START_STATES = {JOB_START_PREPARED, JOB_STARTED, JOB_AUDITING, JOB_FINALIZING}
TERMINAL_STATES = {JOB_COMPLETE, JOB_FAILED, JOB_CANCELLED}


class DispatchError(RuntimeError):
    """Raised for a rejected dispatch operation (carries a machine code)."""

    def __init__(self, code: str, message: str, retriable: bool = False):
        super().__init__(message)
        self.code = code
        self.retriable = retriable


@dataclass
class WorkerRecord:
    worker_id: str
    state: str = WORKER_FREE
    widget_version: str = ""
    widget_protocol: str = ""
    widget_build_version: str = ""
    bridge_api_version: str = ""
    site: str = "chatgpt"
    conversation_key: str = ""
    conversation_id: str = ""
    url_path: str = ""
    project_name: str = ""
    profile: str = ""
    campaign_run_id: str = ""
    last_seen_at: float = 0.0
    last_assigned_at: float = 0.0
    generating: bool = False
    has_manual_draft: bool = False
    has_attachments: bool = False
    audit_start_in_flight: bool = False
    action_in_flight: bool = False
    is_brave: bool = False
    is_chromium: bool = False
    page_eligible: bool = False
    has_conversation_turns: bool = False
    clean_for_audit: bool = False
    managed_slot: int = 0
    managed_generation: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatchJob:
    dispatch_id: str
    project_id: str = ""
    project_name: str = ""
    archive_filename: str = ""
    archive_path: str = ""
    archive_size: int = 0
    archive_sha256: str = ""
    requested_profile: str = "quick3"
    created_at: float = 0.0
    state: str = JOB_QUEUED
    assigned_worker_id: str = ""
    lease_id: str = ""
    lease_expires_at: float = 0.0
    attempts: int = 0
    campaign_run_id: str = ""
    conversation_id: str = ""
    start_receipt: str = ""
    error: str = ""
    result: str = ""
    final_handoff_path: str = ""
    final_handoff_sha256: str = ""
    completed_at: float = 0.0
    recovery_state: str = ""
    meta_run_id_drift: str = ""
    retry_count: int = 0
    next_retry_at: float = 0.0
    last_error_code: str = ""
    updated_at: float = 0.0
    cancel_owner_worker_id: str = ""
    cancel_owner_lease_id: str = ""


def new_dispatch_id() -> str:
    return f"dsp-{uuid.uuid4().hex[:16]}"


def new_lease_id() -> str:
    return f"lease-{uuid.uuid4().hex[:16]}"


def _now() -> float:
    return time.time()


def _atomic_write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class BrowserDispatcher:
    """Owns the worker registry, job queue, leases and scheduling.

    In-memory registries are the authority for worker liveness; jobs are
    mirrored to a small JSON state file so they survive a Bridge restart.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else (get_state_dir() / "browser_dispatch")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_file = self.state_dir / "jobs.json"
        self.generation_file = self.state_dir / "browser_dispatch_generation.json"
        self._generation = 0
        self._generation_context: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._work_available = threading.Condition(self._lock)
        self._workers: dict[str, WorkerRecord] = {}
        self._managed_slot_seen: dict[int, float] = {}
        self._jobs: dict[str, DispatchJob] = {}
        self._expired_worker_count = 0
        self._campaign_probe: Optional[Any] = None
        self._load_jobs()
        try:
            self._generation = int(json.loads(self.generation_file.read_text(encoding="utf-8")).get("generation", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._generation = 0

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    def _load_jobs(self) -> None:
        if not self.jobs_file.exists():
            return
        try:
            doc = json.loads(self.jobs_file.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("schema_version") != 1:
                raise ValueError("unsupported browser dispatch state schema")
            for raw in doc.get("jobs", []):
                if not isinstance(raw, dict):
                    raise ValueError("dispatch job entry must be an object")
                job = DispatchJob(**{k: raw[k] for k in DispatchJob.__dataclass_fields__ if k in raw})
                if not DISPATCH_ID_RE.fullmatch(job.dispatch_id):
                    raise ValueError(f"invalid persisted dispatch id: {job.dispatch_id!r}")
                self._jobs[job.dispatch_id] = job
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise DispatchError(
                "state_corrupt",
                f"browser dispatch state is unreadable: {exc}",
            ) from exc

        # Worker registrations are intentionally ephemeral. After a Bridge
        # restart pre-START work is safe to requeue; once a START receipt may
        # exist, fail closed into reconciliation instead of inventing a retry.
        changed = False
        now = _now()
        for job in self._jobs.values():
            if job.state in PRE_START_STATES:
                job.state = JOB_QUEUED
                job.assigned_worker_id = ""
                job.lease_id = ""
                job.lease_expires_at = 0.0
                job.updated_at = now
                changed = True
            elif job.state in POST_START_STATES:
                job.recovery_state = job.state
                job.state = JOB_BLOCKED
                job.error = "Bridge restarted after START_PREPARED; same-worker reconciliation required"
                job.updated_at = now
                changed = True
            elif job.state not in {JOB_QUEUED, JOB_BLOCKED, *TERMINAL_STATES}:
                raise DispatchError(
                    "state_corrupt",
                    f"persisted dispatch {job.dispatch_id} has unknown state {job.state!r}",
                )
        if changed:
            self._persist_jobs()

    def _persist_jobs(self) -> None:
        doc = {
            "schema_version": 1,
            "updated_at": _now(),
            "jobs": [
                {k: getattr(j, k) for k in DispatchJob.__dataclass_fields__}
                for j in self._jobs.values()
            ],
        }
        with cross_process_lock(self.jobs_file.with_suffix(".lock")):
            _atomic_write_json(self.jobs_file, doc)
            self._generation += 1
            _atomic_write_json(self.generation_file, {
                "generation": self._generation,
                **self._generation_context,
                "updated_at": _now(),
            })

    # ------------------------------------------------------------------ #
    # workers
    # ------------------------------------------------------------------ #

    def list_workers(self, expired_ttl: float = WORKER_TTL_SECONDS) -> list[WorkerRecord]:
        with self._lock:
            now = _now()
            live = []
            for w in self._workers.values():
                if now - w.last_seen_at <= expired_ttl:
                    live.append(w)
            return live

    def _expire_workers(self) -> None:
        now = _now()
        expired = [
            wid for wid, w in self._workers.items()
            if now - w.last_seen_at > WORKER_TTL_SECONDS
        ]
        for wid in expired:
            self._workers.pop(wid, None)
        self._expired_worker_count += len(expired)

    @staticmethod
    def _incoming_managed_slot(payload: dict[str, Any]) -> int:
        """The managed slot a registering window claims, or 0 for a plain tab."""
        try:
            slot = int(payload.get("managed_slot", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return slot if 1 <= slot <= MAX_ACTIVE_WORKERS else 0

    def _note_managed_slot(self, slot: int) -> None:
        self._managed_slot_seen[int(slot)] = _now()

    def _live_managed_slots(self) -> set[int]:
        """Managed slots that have a window behind them right now.

        Remembered slightly beyond one heartbeat TTL on purpose: a managed
        window that is reloading, or that lost one poll to a refusal, has not
        stopped being a lane, and letting an unmanaged tab take its place for
        those few seconds is exactly the rotation this is here to stop.
        """
        now = _now()
        registered = {
            worker.managed_slot for worker in self._workers.values()
            if worker.managed_slot and now - worker.last_seen_at <= WORKER_TTL_SECONDS
        }
        remembered = {
            slot for slot, seen in self._managed_slot_seen.items()
            if now - seen <= MANAGED_SLOT_MEMORY_SECONDS
        }
        return registered | remembered

    def register_worker(self, payload: dict[str, Any]) -> WorkerRecord:
        with self._lock:
            self._expire_workers()
            wid = str(payload.get("worker_id") or "").strip()
            if not WORKER_ID_RE.fullmatch(wid):
                raise DispatchError("invalid_worker_id", "worker_id must be 1-128 safe identifier characters")
            url_path = str(payload.get("url_path") or "")
            if (
                str(payload.get("widget_version") or "").startswith("AUDAPACK_WIDGET")
                and url_path.startswith("/backend-api/sentinel/")
            ):
                # Old Widget builds ran inside ChatGPT's matching sentinel
                # iframe. Purge any prior record before rejecting the poll so
                # embedded documents can never consume a real tab slot.
                self._workers.pop(wid, None)
                raise DispatchError(
                    "ineligible_worker_context",
                    "embedded ChatGPT frames cannot register as browser workers",
                )
            incoming_slot = self._incoming_managed_slot(payload)
            if incoming_slot:
                self._note_managed_slot(incoming_slot)
                # A reloaded window keeps its slot but takes a fresh worker_id,
                # so its predecessor sat in the registry until TTL holding a
                # second lane for one physical window. One slot, one lane.
                for stale in [
                    other for other in self._workers.values()
                    if other.worker_id != wid
                    and other.managed_slot == incoming_slot
                    and not other.campaign_run_id
                    and not self._worker_owns_live_job(other)
                ]:
                    self._workers.pop(stale.worker_id, None)
                if len(self._live_managed_slots()) >= MAX_ACTIVE_WORKERS:
                    # Every lane is spoken for by a managed window. An idle
                    # personal tab already holding one is evicted here, not
                    # merely refused on arrival: refusing newcomers alone left
                    # whichever tab got in first sitting on a lane forever
                    # while the sixth managed window rotated in and out.
                    for tab in [
                        other for other in self._workers.values()
                        if not other.managed_slot
                        and self.worker_consumes_lane(other)
                        and not other.campaign_run_id
                        and not self._worker_owns_live_job(other)
                        and other.state in {WORKER_FREE, WORKER_RESERVED}
                    ]:
                        self._workers.pop(tab.worker_id, None)
            elif (
                wid not in self._workers
                and len(self._live_managed_slots()) >= MAX_ACTIVE_WORKERS
            ):
                # Every lane belongs to a managed window the operator asked
                # for. A ChatGPT tab in their own browser is a fallback worker,
                # not a seventh lane: admitting it here evicted a managed
                # window, which came back and evicted the tab, and the pool
                # rotated between five and six forever instead of settling.
                raise DispatchError(
                    "worker_limit",
                    f"all {MAX_ACTIVE_WORKERS} audit lanes belong to managed AUDAPACK windows",
                )
            lane_workers = [w for w in self._workers.values() if self.worker_consumes_lane(w)]
            if (
                wid not in self._workers
                and len(lane_workers) < MAX_ACTIVE_WORKERS
                and len(self._workers) >= MAX_ACTIVE_WORKERS
            ):
                # Lanes are free but the registry is full of tabs that can
                # never do audit work. Drop the stalest of those instead of
                # refusing a worker that could actually run the queue.
                spare = [w for w in self._workers.values() if not self.worker_consumes_lane(w)]
                if spare:
                    stalest = min(spare, key=lambda item: (item.last_seen_at, item.worker_id))
                    self._workers.pop(stalest.worker_id, None)
            if wid not in self._workers and len(lane_workers) >= MAX_ACTIVE_WORKERS:
                incoming_supported = (
                    str(payload.get("widget_version") or "") == SUPPORTED_BROWSER_WIDGET_VERSION
                    and bool(payload.get("is_chromium", payload.get("is_brave", False)))
                    and bool(payload.get("page_eligible", False))
                    and str(payload.get("site") or "chatgpt") == "chatgpt"
                    and str(payload.get("url_path") or "") == "/"
                )
                if incoming_supported and self._incoming_managed_slot(payload):
                    # A managed window is a lane the operator asked for; a
                    # ChatGPT tab in their own browser is not. Six managed
                    # windows plus one personal tab is seven candidates for six
                    # lanes, and the loser rotated on every heartbeat -- slots
                    # appearing and vanishing while the pool never settled.
                    # An idle unmanaged tab yields its lane instead.
                    yielding = [
                        worker for worker in lane_workers
                        if not worker.managed_slot
                        and not worker.campaign_run_id
                        and not self._worker_owns_live_job(worker)
                        and worker.state in {WORKER_FREE, WORKER_RESERVED}
                    ]
                    if yielding:
                        stalest = min(yielding, key=lambda item: (item.last_seen_at, item.worker_id))
                        self._workers.pop(stalest.worker_id, None)
                        lane_workers = [w for w in self._workers.values() if self.worker_consumes_lane(w)]
            if wid not in self._workers and len(lane_workers) >= MAX_ACTIVE_WORKERS:
                replaceable = [
                    worker for worker in lane_workers
                    if worker.widget_version.startswith("AUDAPACK_WIDGET")
                    and not self.worker_free_for_claim(worker)
                    and not worker.campaign_run_id
                    and not self._worker_owns_live_job(worker)
                    and worker.state in {WORKER_FREE, WORKER_RESERVED}
                ]
                if incoming_supported and replaceable:
                    oldest = min(replaceable, key=lambda item: (item.last_seen_at, item.worker_id))
                    self._workers.pop(oldest.worker_id, None)
                else:
                    raise DispatchError(
                        "worker_limit",
                        f"dispatcher accepts at most {MAX_ACTIVE_WORKERS} active workers",
                    )
            record = self._workers.get(wid) or WorkerRecord(worker_id=wid)
            reported_state = str(payload.get("state") or WORKER_FREE).strip().upper()
            if reported_state not in WORKER_ACTIVE_STATES | {WORKER_OFFLINE}:
                raise DispatchError("invalid_worker_state", f"unsupported worker state {reported_state!r}")
            record.state = reported_state
            record.widget_version = str(payload.get("widget_version") or record.widget_version)
            record.widget_protocol = str(
                payload.get("widget_protocol")
                or payload.get("widget_version")
                or record.widget_protocol
            )
            record.widget_build_version = str(
                payload.get("widget_build_version") or record.widget_build_version
            )
            record.bridge_api_version = str(payload.get("bridge_api_version") or record.bridge_api_version)
            record.site = str(payload.get("site") or "chatgpt")
            record.conversation_key = str(payload.get("conversation_key") or record.conversation_key)
            record.conversation_id = str(payload.get("conversation_id") or record.conversation_id)
            record.url_path = url_path
            record.project_name = str(payload.get("project_name") or "")
            record.profile = str(payload.get("profile") or "")
            record.campaign_run_id = str(payload.get("campaign_run_id") or "")
            record.generating = bool(payload.get("generating", False))
            record.has_manual_draft = bool(payload.get("has_manual_draft", False))
            record.has_attachments = bool(payload.get("has_attachments", False))
            record.audit_start_in_flight = bool(payload.get("audit_start_in_flight", False))
            record.action_in_flight = bool(payload.get("action_in_flight", False))
            record.is_brave = bool(payload.get("is_brave", False))
            record.is_chromium = bool(payload.get("is_chromium", record.is_brave))
            record.page_eligible = bool(payload.get("page_eligible", False))
            record.has_conversation_turns = bool(payload.get("has_conversation_turns", False))
            record.clean_for_audit = bool(payload.get("clean_for_audit", False))
            try:
                record.managed_slot = max(0, min(MAX_ACTIVE_WORKERS, int(payload.get("managed_slot", 0) or 0)))
                record.managed_generation = max(0, int(payload.get("managed_generation", 0) or 0))
            except (TypeError, ValueError):
                record.managed_slot = 0
                record.managed_generation = 0
            if payload.get("browser_name"):
                record.meta["browser_name"] = str(payload.get("browser_name"))[:80]
            record.last_seen_at = _now()
            if reported_state == WORKER_OFFLINE:
                self._workers.pop(wid, None)
                return record
            self._workers[wid] = record

            dispatch_id = str(payload.get("dispatch_id") or "").strip()
            lease_id = str(payload.get("lease_id") or "").strip()
            record.meta["reports_lease"] = bool(dispatch_id and lease_id)
            if dispatch_id or lease_id:
                if not (dispatch_id and lease_id):
                    raise DispatchError("invalid_lease", "dispatch_id and lease_id must be reported together")
                job = self._jobs.get(dispatch_id)
                try:
                    if job and self._is_recovery_block(job):
                        self.reconcile_job(dispatch_id, wid, lease_id, payload)
                    else:
                        self.renew_lease(dispatch_id, wid, lease_id)
                    record.meta.pop("last_reconcile_error", None)
                except DispatchError as exc:
                    # Registration must never depend on one job's recovery
                    # outcome. A refused reconcile (run id or receipt conflict,
                    # an expired lease) used to propagate out of register_worker,
                    # so the window stopped registering entirely: it vanished
                    # from the pool, could not recycle, and its lane was lost
                    # for good over a single stuck dispatch. The job stays
                    # BLOCKED for the operator; the worker stays alive.
                    record.meta["last_reconcile_error"] = f"{exc.code}: {exc}"[:200]
            return record

    def _worker_owns_live_job(self, worker: Optional[WorkerRecord]) -> bool:
        # A worker that is not registered owns nothing. Reading .worker_id off
        # None raised AttributeError instead -- and only once a job existed, so
        # every test with an empty queue passed while the live Bridge closed
        # the connection on any unmanaged registration.
        if worker is None:
            return False
        return any(
            job.assigned_worker_id == worker.worker_id
            and job.state not in TERMINAL_STATES | {JOB_BLOCKED}
            for job in self._jobs.values()
        )

    def worker_consumes_lane(self, worker: WorkerRecord) -> bool:
        """True when a worker occupies one of the MAX_ACTIVE_WORKERS audit lanes.

        Every registered ChatGPT tab running the Widget used to consume a lane,
        including tabs that can never claim anything: legacy-widget builds and
        the operator's own parked conversations. Six such tabs filled the whole
        dispatcher while real queued audits starved and no managed window could
        be launched -- the Bridge reported `W 6/6 CLEAN 0` with nothing running.

        A worker occupies a lane only when it could actually do audit work: it
        already owns a run or a job, or it is a live claim candidate. Everything
        else stays visible in status but stops blocking capacity.
        """
        if worker.widget_version in INCOMPATIBLE_WIDGET_VERSIONS:
            return False
        if not worker.widget_version.startswith("AUDAPACK_WIDGET"):
            # Legacy/test registrations keep the historical behaviour.
            return True
        if worker.widget_version != SUPPORTED_BROWSER_WIDGET_VERSION:
            return False
        # A build difference is reported, never enforced. The protocol is the
        # real compatibility boundary; the build number is a release marker,
        # and refusing on it meant every widget release took the whole pool
        # offline until a human clicked Install in Tampermonkey -- an
        # unattended machine simply stopped auditing. STALE_WIDGET still says
        # so, loudly, in status and in the Project Room.
        if not worker.is_chromium or worker.site != "chatgpt":
            return False
        if worker.campaign_run_id or self._worker_owns_live_job(worker):
            return True
        if not worker.page_eligible:
            return False
        if not worker.managed_slot and not worker.clean_for_audit:
            # A tab in the operator's own browser that is eligible but not
            # clean can never claim anything, yet it sat on one of the six
            # lanes forever: observed as act 6 / free 5 with a personal tab
            # RESERVED on a project it no longer owned, while a managed window
            # had nowhere to register. A managed slot still holds its lane
            # while it settles -- that one is the pool.
            return False
        return True

    @staticmethod
    def worker_widget_is_stale(worker: WorkerRecord) -> bool:
        """True when this window runs a widget build that cannot claim audits.

        The build gate is silent by design in the claim path, which made it the
        worst possible failure: six windows sat there reporting CLEAN, every
        audit stayed QUEUED, and nothing anywhere said the widget needed
        updating. Status has to be able to name this.
        """
        if worker.widget_version in INCOMPATIBLE_WIDGET_VERSIONS:
            return True
        if not worker.widget_version.startswith("AUDAPACK_WIDGET"):
            return False
        if worker.widget_version != SUPPORTED_BROWSER_WIDGET_VERSION:
            return True
        if not worker.widget_protocol.startswith("AUDAPACK_WIDGET"):
            return False
        required = _get_required_widget_build()
        return bool(required and worker.widget_build_version and worker.widget_build_version != required)

    def stale_widget_workers(self) -> int:
        with self._lock:
            now = _now()
            return sum(
                1 for worker in self._workers.values()
                if now - worker.last_seen_at <= WORKER_TTL_SECONDS
                and self.worker_widget_is_stale(worker)
            )

    def worker_free_for_claim(self, worker: WorkerRecord) -> bool:
        """FREE + CLEAN must both be true to claim a new audit.

        P0-3: a worker with has_conversation_turns is OCCUPIED regardless of
        composer emptiness -- random old chats must NEVER receive an audit job.
        clean_for_audit is the Widget's own positive proof of a clean ChatGPT
        conversation with zero turns, no draft, no attachments, no generation.
        The clean gate applies to the supported AUDAPACK_WIDGET/3 contract;
        legacy/test registrations (widget_version not AUDAPACK_WIDGET-prefixed)
        are treated as clean by default to preserve the historical behaviour.
        """
        if worker.widget_version in INCOMPATIBLE_WIDGET_VERSIONS:
            return False
        if worker.widget_version.startswith("AUDAPACK_WIDGET"):
            if worker.widget_version != SUPPORTED_BROWSER_WIDGET_VERSION:
                return False
            # See worker_consumes_lane: the build number warns, the protocol
            # decides. A pool that refuses to work until someone clicks Install
            # is a worse failure than running one release behind.
            if not worker.is_chromium or not worker.page_eligible:
                return False
            if worker.site != "chatgpt" or worker.url_path != "/":
                return False
            if worker.has_conversation_turns or not worker.clean_for_audit:
                return False
        if worker.generating or worker.audit_start_in_flight or worker.action_in_flight:
            return False
        if worker.has_manual_draft or worker.has_attachments:
            return False
        if worker.state not in (WORKER_FREE, WORKER_RESERVED):
            return False
        if worker.campaign_run_id:
            return False
        if self._worker_owns_live_job(worker):
            return False
        return True

    def renew_lease(self, dispatch_id: str, worker_id: str, lease_id: str) -> DispatchJob:
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                raise DispatchError("unknown_job", "dispatch_id is unknown")
            self._require_owner(job, worker_id, lease_id, check_expiry=True)
            if job.state in TERMINAL_STATES | {JOB_BLOCKED}:
                return job
            job.lease_expires_at = _now() + LEASE_SECONDS
            job.updated_at = _now()
            return job

    @staticmethod
    def _is_recovery_block(job: DispatchJob) -> bool:
        """True for any explicit post-start recovery block (restart or expiry)."""
        if job.state != JOB_BLOCKED:
            return False
        if job.recovery_state in POST_START_STATES:
            return True
        return job.error.startswith("Bridge restarted after START_PREPARED") or job.error.startswith("worker lost after START_PREPARED")

    def reconcile_job(self, dispatch_id: str, worker_id: str, lease_id: str, payload: dict[str, Any]) -> DispatchJob:
        """Restore a restart-blocked post-START job for its same owner only."""
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                raise DispatchError("unknown_job", "dispatch_id is unknown")
            if not self._is_recovery_block(job):
                return self.renew_lease(dispatch_id, worker_id, lease_id)
            if job.assigned_worker_id != worker_id or job.lease_id != lease_id:
                raise DispatchError("stale_owner", "only the original worker may reconcile this dispatch")
            run_id = str(payload.get("campaign_run_id") or "").strip()
            receipt = str(payload.get("start_receipt") or "").strip()
            # worker_id + lease_id is already proof of the original owner: the
            # lease is a server-minted secret bound to this dispatch and cannot
            # be fabricated. Demanding that the worker ALSO echo the campaign
            # run id back permanently stranded live audits, because ChatGPT
            # route hydration re-arms the widget's runtime and re-derives that
            # id -- observed on five concurrent runs at once, every one of them
            # refused with run_id_conflict while the audit kept going in the
            # browser. The job's own id stays authoritative and is never
            # overwritten here; a mismatch is recorded, not fatal.
            if run_id and job.campaign_run_id and run_id != job.campaign_run_id:
                job.meta_run_id_drift = run_id
            if job.start_receipt and receipt and receipt != job.start_receipt:
                raise DispatchError("start_receipt_conflict", "recovery START receipt does not match dispatch")
            job.state = job.recovery_state or JOB_AUDITING
            job.error = ""
            job.lease_expires_at = _now() + LEASE_SECONDS
            job.updated_at = _now()
            worker = self._workers.get(worker_id)
            if worker:
                worker.state = WORKER_AUDITING
                worker.campaign_run_id = job.campaign_run_id
            self._generation_context = {"dispatch_id": job.dispatch_id, "project_id": job.project_id, "state": job.state}
            self._persist_jobs()
            return job

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #

    def enqueue_job(self, payload: dict[str, Any]) -> DispatchJob:
        with self._lock:
            self._prune_history_locked()
            active_count = sum(1 for job in self._jobs.values() if job.state not in TERMINAL_STATES)
            if active_count >= QUEUE_BOUND:
                raise DispatchError("queue_full", f"dispatch queue bound is {QUEUE_BOUND}")
            now = _now()
            project_id = str(payload.get("project_id") or "")
            if project_id and any(
                existing.project_id == project_id and existing.state not in TERMINAL_STATES
                for existing in self._jobs.values()
            ):
                raise DispatchError("duplicate_dispatch", "project already has an active browser audit dispatch")
            job = DispatchJob(
                dispatch_id=new_dispatch_id(),
                project_id=project_id,
                project_name=str(payload.get("project_name") or ""),
                archive_filename=str(payload.get("archive_filename") or ""),
                archive_path=str(payload.get("archive_path") or ""),
                archive_size=int(payload.get("archive_size") or 0),
                archive_sha256=str(payload.get("archive_sha256") or ""),
                requested_profile=str(payload.get("profile") or payload.get("requested_profile") or "quick3"),
                created_at=now,
                state=JOB_QUEUED,
                updated_at=now,
            )
            if not job.project_name:
                raise DispatchError("invalid_job", "project_name is required")
            if not job.project_id:
                raise DispatchError("invalid_job", "project_id is required")
            if not job.archive_path or not Path(job.archive_path).is_file():
                raise DispatchError("missing_archive", "a real archive path is required")
            if not job.archive_filename or Path(job.archive_filename).name != job.archive_filename:
                raise DispatchError("invalid_job", "archive_filename must be a basename")
            self._jobs[job.dispatch_id] = job
            self._generation_context = {"dispatch_id": job.dispatch_id, "project_id": job.project_id, "state": job.state}
            self._persist_jobs()
            self._work_available.notify_all()
            return job

    def _prune_history_locked(self) -> None:
        terminal = sorted(
            (job for job in self._jobs.values() if job.state in TERMINAL_STATES),
            key=lambda job: (job.updated_at, job.created_at),
            reverse=True,
        )
        for job in terminal[HISTORY_BOUND:]:
            self._jobs.pop(job.dispatch_id, None)

    def get_job(self, dispatch_id: str) -> Optional[DispatchJob]:
        with self._lock:
            return self._jobs.get(dispatch_id)

    def get_owned_job(self, worker_id: str) -> Optional[DispatchJob]:
        """Return the worker's current dispatch for heartbeat reconciliation.

        W5.2: a CANCELLED dispatch still belongs to its original worker (via
        cancel_owner_* identity) so the browser can receive the terminal
        CANCELLED ACK and clear its local lease -- otherwise it would loop on
        stale_owner forever."""
        with self._lock:
            jobs = [
                job for job in self._jobs.values()
                if (job.assigned_worker_id == str(worker_id)
                    or job.cancel_owner_worker_id == str(worker_id))
                and job.state not in {JOB_COMPLETE, JOB_FAILED}
            ]
            return min(jobs, key=lambda item: item.created_at) if jobs else None

    def max_poll_wait_seconds(self) -> float:
        """Longest a poll may block without starving the other workers.

        Every AUDAPACK window drives its polls through one shared userscript
        manager, so the browser serializes them: six windows each holding a
        20s long poll means any one window heartbeats once every two minutes,
        well past WORKER_TTL_SECONDS. The registry could then never hold more
        than five of six lanes, and which lane was missing rotated forever.
        Shrink the block as the pool grows so a full round of polls stays
        inside half the TTL. A poll with work waiting still returns at once.
        """
        with self._lock:
            pool = max(1, len(self._workers), len(self._live_managed_slots()))
        return max(2.0, WORKER_TTL_SECONDS / (2.0 * pool))

    def renew_owner_lease(self, worker_id: str, dispatch_id: str = "") -> Optional[DispatchJob]:
        """Extend the lease of the job this worker owns, because it just polled.

        A lease exists to notice a worker that died. Only a state transition
        used to extend one, and a worker in AUDITING has no transition to make
        for as long as the audit takes -- minutes per wave. Its own lease
        therefore expired underneath it and ``expire_leases`` blocked a run
        that was healthy and still producing waves, which is why finished
        audits kept landing on disk while their dispatch record said
        ``worker lost after START_PREPARED``. A poll is positive proof the
        window is alive, so a poll renews.

        Not persisted on purpose: ``lease_expires_at`` is meaningless across a
        Bridge restart (``_load_jobs`` requeues or blocks every live job
        anyway), and writing jobs.json on every poll of every worker would be
        pure disk churn.
        """
        with self._lock:
            renewable = TERMINAL_STATES | {JOB_QUEUED, JOB_BLOCKED}
            job = next(
                (
                    item for item in self._jobs.values()
                    if item.assigned_worker_id == str(worker_id)
                    and item.state not in renewable
                ),
                None,
            )
            if job is None:
                return None
            # Renew only the dispatch the worker still says it holds. A widget
            # that claims a job and then drops it locally reports no lease at
            # all, and renewing anyway kept a pre-START job LEASED forever:
            # expire_leases could never requeue it, so the project sat with a
            # dead lease while clean workers idled beside it. A post-start run
            # always reports its lease, so recovery is unaffected.
            declared = str(dispatch_id or "").strip()
            if job.state in PRE_START_STATES and declared != job.dispatch_id:
                return None
            job.lease_expires_at = _now() + LEASE_SECONDS
            return job

    def list_jobs(self) -> list[DispatchJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def _eligible_job(self, worker: WorkerRecord) -> Optional[DispatchJob]:
        """FIFO over QUEUED jobs and backoff-elapsed RETRYABLE jobs,
        least-recently-assigned worker is chosen by the caller comparing
        last_assigned_at. A RETRYABLE job becomes eligible only after its
        next_retry_at backoff has elapsed, which prevents worker pinball."""
        now = _now()
        candidates = [
            j for j in self._jobs.values()
            if (j.state == JOB_QUEUED)
            or (j.state == JOB_RETRYABLE and now >= j.next_retry_at)
        ]
        return min(candidates, key=lambda j: j.created_at) if candidates else None

    def _next_worker_for_assignment(self) -> Optional[str]:
        eligible = [worker for worker in self._workers.values() if self.worker_free_for_claim(worker)]
        if not eligible:
            return None
        return min(eligible, key=lambda worker: (worker.last_assigned_at, worker.worker_id)).worker_id

    def claim_job(self, worker_id: str, poll_payload: Optional[dict[str, Any]] = None) -> Optional[DispatchJob]:
        """Atomic claim: pick oldest eligible queued job, bind a lease.

        Returns the leased job (already moved to LEASED) or None when nothing
        is available. Raises DispatchError for a stale/busy worker.
        """
        with self._lock:
            self._expire_workers()
            worker = self._workers.get(worker_id)
            if worker is None:
                raise DispatchError("unknown_worker", "worker_id is not registered")
            now = _now()
            worker.last_seen_at = now
            if not self.worker_free_for_claim(worker):
                return None
            job = self._eligible_job(worker)
            if job is None:
                worker.state = WORKER_FREE
                return None
            job.state = JOB_LEASED
            job.assigned_worker_id = worker_id
            job.lease_id = new_lease_id()
            job.lease_expires_at = now + LEASE_SECONDS
            job.attempts += 1
            job.updated_at = now
            worker.state = WORKER_RESERVED
            worker.last_assigned_at = now
            self._generation_context = {"dispatch_id": job.dispatch_id, "project_id": job.project_id, "state": job.state}
            self._persist_jobs()
            return job

    def _require_owner(
        self,
        job: DispatchJob,
        worker_id: str,
        lease_id: str,
        *,
        check_expiry: bool = True,
    ) -> None:
        if job.assigned_worker_id != worker_id:
            raise DispatchError("stale_owner", "job is leased to a different worker")
        if job.lease_id != lease_id:
            raise DispatchError("stale_lease", "lease token does not match the job")
        if check_expiry and job.state not in TERMINAL_STATES and _now() > job.lease_expires_at:
            raise DispatchError("lease_expired", "dispatch lease has expired", retriable=job.state in PRE_START_STATES)

    def _apply_transition_metadata(self, job: DispatchJob, payload: dict[str, Any]) -> None:
        campaign_run_id = str(payload.get("campaign_run_id") or "").strip()
        if campaign_run_id:
            if job.campaign_run_id and job.campaign_run_id != campaign_run_id:
                raise DispatchError("run_id_conflict", "campaign_run_id cannot change within a dispatch")
            job.campaign_run_id = campaign_run_id
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if conversation_id:
            job.conversation_id = conversation_id
        start_receipt = str(payload.get("start_receipt") or "").strip()
        if start_receipt:
            if job.start_receipt and job.start_receipt != start_receipt:
                raise DispatchError("start_receipt_conflict", "START receipt cannot change within a dispatch")
            job.start_receipt = start_receipt

    def transition_job(
        self,
        dispatch_id: str,
        worker_id: str,
        lease_id: str,
        to_state: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> DispatchJob:
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                raise DispatchError("unknown_job", "dispatch_id is unknown")
            retryable_requeue = job.state == JOB_RETRYABLE and to_state == JOB_QUEUED
            if not retryable_requeue:
                self._require_owner(job, worker_id, lease_id)
            now = _now()

            # ACK retries are deliberately idempotent. A lost HTTP response
            # must never turn the same receipt/state report into a false
            # transition failure that tempts another worker to start Core.
            if job.state == to_state:
                self._apply_transition_metadata(job, payload or {})
                if job.state not in TERMINAL_STATES | {JOB_BLOCKED}:
                    job.lease_expires_at = now + LEASE_SECONDS
                job.updated_at = now
                self._persist_jobs()
                return job

            # Lease expiry before START_PREPARED may return to QUEUED (spec
            # section 11). After the boundary exactly-once forbids reassignment.
            if to_state == JOB_RETRYABLE and now > job.lease_expires_at:
                pass  # allowed
            if to_state == JOB_QUEUED and job.state == JOB_RETRYABLE:
                job.state = JOB_QUEUED
                job.assigned_worker_id = ""
                job.lease_id = ""
                job.lease_expires_at = 0
                job.updated_at = now
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_FREE
                self._persist_jobs()
                return job

            if (job.state, to_state) not in JOB_TRANSITIONS:
                raise DispatchError(
                    "invalid_transition",
                    f"dispatch {dispatch_id} cannot move {job.state} -> {to_state}",
                )

            self._apply_transition_metadata(job, payload or {})
            if to_state == JOB_START_PREPARED and not job.start_receipt:
                raise DispatchError("missing_start_receipt", "START_PREPARED requires the canonical START receipt")

            job.state = to_state
            self._generation_context = {"dispatch_id": job.dispatch_id, "project_id": job.project_id, "state": to_state}
            job.updated_at = now
            if to_state not in TERMINAL_STATES | {JOB_BLOCKED}:
                job.lease_expires_at = now + LEASE_SECONDS
            if to_state == JOB_ARTIFACT_FETCHED:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_UPLOADING
            elif to_state == JOB_ATTACHED:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_UPLOADING
            elif to_state == JOB_START_PREPARED:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_STARTING
            elif to_state == JOB_STARTED:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_AUDITING
                    worker.campaign_run_id = job.campaign_run_id
            elif to_state == JOB_AUDITING:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_AUDITING
                    worker.campaign_run_id = job.campaign_run_id
            elif to_state == JOB_FINALIZING:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_AUDITING
                    worker.campaign_run_id = job.campaign_run_id
            elif to_state == JOB_COMPLETE:
                # Durable completion is normally performed by
                # complete_for_run() after Bridge persistence. Direct legacy
                # COMPLETE remains accepted for old clients, but cannot carry
                # terminal proof unless supplied.
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_FREE
                    worker.campaign_run_id = ""
                    worker.conversation_key = ""
                job.result = str(payload.get("result") or job.result)
                if payload.get("final_handoff_path"):
                    job.final_handoff_path = str(payload["final_handoff_path"])
                if payload.get("final_handoff_sha256"):
                    job.final_handoff_sha256 = str(payload["final_handoff_sha256"])
                job.completed_at = now
            elif to_state in (JOB_BLOCKED, JOB_FAILED):
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_BLOCKED
                job.error = str(payload.get("error") or job.error)
            elif to_state == JOB_RETRYABLE:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.state = WORKER_FREE
                job.error = str(payload.get("error") or job.error)
                job.last_error_code = str(payload.get("error") or job.error)
                if job.retry_count >= PRE_START_MAX_RETRIES:
                    job.state = JOB_BLOCKED
                    job.retry_count += 1
                    job.error = f"pre-start retries exhausted: {job.last_error_code}"
                    job.assigned_worker_id = ""
                    job.lease_id = ""
                    job.lease_expires_at = 0.0
                else:
                    job.retry_count += 1
                    job.next_retry_at = now + min(PRE_START_RETRY_BACKOFF_SECONDS * (2 ** (job.retry_count - 1)), PRE_START_RETRY_BACKOFF_MAX)
                    job.assigned_worker_id = ""
                    job.lease_id = ""
                    job.lease_expires_at = 0.0
            self._persist_jobs()
            if to_state in (JOB_RETRYABLE, JOB_COMPLETE):
                self._work_available.notify_all()
            return job

    def expire_leases(self) -> int:
        """Return expired LEASED/ARTIFACT_FETCHED jobs to QUEUED (safe only
        before START_PREPARED). START_PREPARED and beyond become BLOCKED --
        never re-leased."""
        with self._lock:
            now = _now()
            requeued = 0
            for job in self._jobs.values():
                pre_start = job.state in (JOB_LEASED, JOB_ARTIFACT_FETCHED, JOB_ATTACHED)
                # A worker expires after WORKER_TTL_SECONDS but its lease runs for
                # LEASE_SECONDS, so a job leased to a window that vanished used to
                # sit untouchable for the difference -- pure dead time with clean
                # workers idle beside it. Nothing before START_PREPARED is
                # irreversible, so a job whose owner is no longer registered goes
                # straight back into the queue.
                owner_gone = (
                    bool(job.assigned_worker_id)
                    and job.assigned_worker_id not in self._workers
                    and now - job.updated_at > PRE_START_OWNER_GRACE_SECONDS
                )
                if pre_start and (now > job.lease_expires_at or owner_gone):
                    job.state = JOB_QUEUED
                    job.assigned_worker_id = ""
                    job.lease_id = ""
                    job.lease_expires_at = 0
                    job.updated_at = now
                    requeued += 1
                elif job.state in POST_START_STATES and now > job.lease_expires_at:
                    owner = self._workers.get(job.assigned_worker_id)
                    if owner is not None and now - owner.last_seen_at <= WORKER_TTL_SECONDS:
                        # The owning window is still registered and heartbeating.
                        # "worker lost" must mean the worker is actually lost --
                        # a live window mid-audit has no transition to make and
                        # must never be blocked for staying quiet. Its heartbeat
                        # is the renewal, so treat it as one.
                        job.lease_expires_at = now + LEASE_SECONDS
                        continue
                    job.recovery_state = job.state
                    job.state = JOB_BLOCKED
                    job.error = "worker lost after START_PREPARED; recovery required"
                    job.updated_at = now
            changed = requeued > 0 or any(
                job.state == JOB_BLOCKED and job.error == "worker lost after START_PREPARED; recovery required"
                and job.updated_at == now
                for job in self._jobs.values()
            )
            if changed:
                self._generation_context = {"dispatch_id": "", "project_id": "", "state": ""}
                self._persist_jobs()
            if requeued:
                self._work_available.notify_all()
            return requeued

    def reconcile_abandoned_runs(self, grace_seconds: float = 90.0) -> int:
        """Free a lane whose assigned worker demonstrably no longer owns the run.

        A post-START job pins its worker, and the worker pins the lane. When the
        window that actually ran the audit is gone -- a duplicate window that
        shared its worker id, a closed tab that came back clean, a hard reload
        that lost the lease -- the Bridge kept believing the run was live while
        the worker reported itself CLEAN and idle on the root page. The result
        was `clean_workers > 0` and `free_workers 0` at the same time, with
        every queued audit starving behind a run nobody was running.

        Only a positively contradictory report counts: the worker is alive,
        reports the clean root state, carries no campaign, and has been seen
        well after the job last progressed.
        """
        grace = max(1.0, float(grace_seconds))
        with self._lock:
            now = _now()
            freed = 0
            for job in self._jobs.values():
                if job.state not in POST_START_STATES:
                    continue
                worker = self._workers.get(job.assigned_worker_id or "")
                if worker is None:
                    continue
                if now - worker.last_seen_at > WORKER_TTL_SECONDS:
                    continue
                if worker.last_seen_at <= job.updated_at + grace:
                    continue
                contradicts = (
                    worker.clean_for_audit
                    and worker.url_path == "/"
                    and not worker.campaign_run_id
                    and not worker.has_conversation_turns
                    and not worker.generating
                )
                if not contradicts:
                    continue
                job.recovery_state = job.state
                job.state = JOB_BLOCKED
                job.error = "assigned worker no longer owns this run; recovery required"
                job.assigned_worker_id = ""
                job.lease_id = ""
                job.lease_expires_at = 0.0
                job.updated_at = now
                freed += 1
            if freed:
                self._generation_context = {"dispatch_id": "", "project_id": "", "state": ""}
                self._persist_jobs()
                self._work_available.notify_all()
            return freed

    def set_campaign_probe(self, probe: Optional[Any]) -> None:
        """Install the callable that asks the audit index if a project finished.

        The dispatcher cannot resolve campaign.json on its own: it looks under
        its own state dir while the real one lives beside the audit artifacts,
        and it keys on a run id the widget may have re-derived. The Bridge can
        answer both questions, so it lends the answer instead.
        """
        self._campaign_probe = probe

    def reconcile_finished_campaigns(self) -> int:
        """Close post-start lanes whose project has a durable finished campaign.

        The finalization event closes a lane as it happens. This is the same
        conclusion reached late -- after a Bridge restart, or for a run blocked
        while its campaign was being written -- so a completed audit never
        stays on the board as a live lane.
        """
        probe = getattr(self, "_campaign_probe", None)
        if probe is None:
            return 0
        with self._lock:
            candidates = [
                job for job in self._jobs.values()
                if job.state in POST_START_STATES
                or (job.state == JOB_BLOCKED and job.recovery_state in POST_START_STATES)
            ]
        closed = 0
        for job in candidates:
            try:
                verdict = probe(job.project_id, job.project_name)
            except Exception as exc:
                logger.debug("campaign probe failed for %s: %s", job.project_name, exc)
                continue
            if not verdict or not verdict.get("complete"):
                continue
            closed += self.complete_runs_for_project(
                job.project_id,
                job.project_name,
                str(verdict.get("handoff_path") or ""),
                str(verdict.get("handoff_sha256") or ""),
                str(verdict.get("campaign_run_id") or ""),
            )
        return closed

    def complete_runs_for_project(self, project_id: str, project_name: str, handoff_path: str, handoff_sha256: str, campaign_run_id: str = "") -> int:
        """Close the lane the moment the durable final handoff is written.

        The dispatch only learns a campaign finished from the worker's terminal
        ACK, and that ACK is one HTTP call that can simply not arrive. Two
        campaigns were observed complete on disk -- 3/3 waves and a valid
        canonical handoff -- with their lanes still reporting AUDITING and
        never becoming READY. reconcile_completed_blocked_runs could not help:
        it looks for campaign.json under the dispatch state dir, while the real
        one lives beside the audit artifacts, and it keys on a run id the
        widget may have re-derived.

        The Bridge writing that handoff is the authoritative event, and here it
        knows the project, the exact path and the digest. Only post-start jobs
        are closed: nothing before START_PREPARED has an audit to be finished.
        """
        wanted = {str(project_id or "").strip().lower(), str(project_name or "").strip().lower()} - {""}
        if not wanted:
            return 0
        with self._lock:
            now = _now()
            closed = 0
            for job in self._jobs.values():
                if job.state not in POST_START_STATES and not (
                    job.state == JOB_BLOCKED and job.recovery_state in POST_START_STATES
                ):
                    continue
                names = {str(job.project_id or "").strip().lower(), str(job.project_name or "").strip().lower()}
                if not (names & wanted):
                    continue
                job.state = JOB_COMPLETE
                job.result = "audit-complete"
                job.error = ""
                job.final_handoff_path = str(handoff_path or job.final_handoff_path)
                job.final_handoff_sha256 = str(handoff_sha256 or job.final_handoff_sha256)
                # The saved campaign can carry a run id this dispatch never saw:
                # ChatGPT route hydration re-arms the widget's runtime and
                # re-derives it. The Bridge just wrote that campaign for this
                # project, so it is the one witness that can bind the two ids
                # together -- without it a finished audit stays SAVING forever,
                # one failed `campaign_match` away from READY.
                saved_run = str(campaign_run_id or "").strip()
                if saved_run and job.campaign_run_id and saved_run != job.campaign_run_id:
                    job.meta_run_id_drift = saved_run
                job.completed_at = now
                job.updated_at = now
                worker = self._workers.get(job.assigned_worker_id or "")
                if worker:
                    worker.campaign_run_id = ""
                    worker.state = WORKER_FREE
                closed += 1
            if closed:
                self._generation_context = {"dispatch_id": "", "project_id": "", "state": JOB_COMPLETE}
                self._persist_jobs()
                self._work_available.notify_all()
            return closed

    def reconcile_completed_blocked_runs(self) -> int:
        """Complete any live post-start run with durable COMPLETE proof.

        A post-start job may finish its audit without the terminal ACK ever
        landing: the browser writes every wave and the canonical handoff, and
        the lane keeps saying AUDITING. Observed after a Bridge restart -- two
        campaigns complete on disk, 3/3 waves and a valid __00_AUDIT_ALL_3.md,
        with their dispatches still reporting 0/3 and never becoming READY.

        Durable campaign evidence outranks a missing ACK: campaign.json exists,
        is COMPLETE, and all waves are done. This used to cover only BLOCKED
        jobs, which left exactly the runs that recovered successfully stuck.
        """
        with self._lock:
            now = _now()
            reconciled = 0
            for job in self._jobs.values():
                if job.state == JOB_BLOCKED:
                    if job.recovery_state not in POST_START_STATES:
                        continue
                elif job.state not in POST_START_STATES:
                    continue
                if not job.campaign_run_id:
                    continue
                campaign_path = self._resolved_campaign_path(job)
                if campaign_path is None or not campaign_path.is_file():
                    continue
                try:
                    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(campaign, dict):
                    continue
                if campaign.get("campaign_status") != "COMPLETE":
                    continue
                if str(campaign.get("campaign_run_id") or "") != str(job.campaign_run_id):
                    continue
                wave_count = int(campaign.get("wave_count") or 0)
                completed_count = int(campaign.get("completed_count") or 0)
                if wave_count and completed_count < wave_count:
                    continue
                job.state = JOB_COMPLETE
                job.result = "audit-complete"
                job.completed_at = now
                job.updated_at = now
                reconciled += 1
            if reconciled:
                self._generation_context = {"dispatch_id": "", "project_id": "", "state": ""}
                self._persist_jobs()
                self._work_available.notify_all()
            return reconciled

    def _resolved_campaign_path(self, job: DispatchJob) -> Optional[Path]:
        """Resolve the durable campaign.json path for a job, if any."""
        base = None
        if job.final_handoff_path:
            try:
                final = Path(job.final_handoff_path).resolve()
            except OSError:
                final = Path(job.final_handoff_path)
            if final.parent.name == "runs":
                base = final.parent
        if base is None:
            base = self.state_dir / "campaigns"
        return base / f"{job.campaign_run_id}.json"

    def complete_for_run(
        self,
        project_id: str,
        campaign_run_id: str,
        final_handoff_path: str | Path,
        *,
        final_handoff_sha256: str | None = None,
        campaign_path: str | Path | None = None,
        expected_wave_count: int | None = None,
        result: str = "audit-complete",
    ) -> Optional[DispatchJob]:
        """Atomically mark the matching FINALIZING dispatch COMPLETE.

        Bridge disk persistence is authoritative: the handoff must exist and
        its digest is recorded before the transport job can become terminal.
        No worker/lease identity is invented during this operation.
        """
        path = Path(final_handoff_path)
        if not path.is_file():
            raise DispatchError("missing_final_handoff", "final handoff is not durable", retriable=True)
        try:
            digest = sha256_of(path)
        except OSError as exc:
            raise DispatchError("final_handoff_unreadable", str(exc), retriable=True) from exc
        expected = str(final_handoff_sha256 or digest).strip().lower()
        if expected != digest:
            raise DispatchError("final_handoff_changed", "final handoff digest does not match", retriable=True)
        if campaign_path is not None:
            try:
                campaign = json.loads(Path(campaign_path).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise DispatchError("campaign_unreadable", str(exc), retriable=True) from exc
            if not isinstance(campaign, dict) or campaign.get("campaign_status") != "COMPLETE":
                raise DispatchError("campaign_incomplete", "campaign.json is not COMPLETE", retriable=True)
            if str(campaign.get("campaign_run_id") or "") != str(campaign_run_id):
                raise DispatchError("run_id_conflict", "campaign.json run id does not match dispatch", retriable=False)
            wave_count = int(expected_wave_count or campaign.get("wave_count") or 0)
            if wave_count and int(campaign.get("completed_count") or 0) < wave_count:
                raise DispatchError("waves_incomplete", "campaign.json has incomplete waves", retriable=True)
        with self._lock:
            candidates = [
                job for job in self._jobs.values()
                if job.project_id == str(project_id)
                and job.campaign_run_id == str(campaign_run_id)
                and (
                    job.state in (JOB_AUDITING, JOB_FINALIZING)
                    or (job.state == JOB_BLOCKED and job.recovery_state in POST_START_STATES)
                )
            ]
            if not candidates:
                return None
            job = min(candidates, key=lambda item: item.created_at)
            now = _now()
            job.state = JOB_COMPLETE
            job.final_handoff_path = str(path.resolve())
            job.final_handoff_sha256 = digest
            job.completed_at = now
            job.result = result
            job.updated_at = now
            worker = self._workers.get(job.assigned_worker_id)
            if worker:
                worker.state = WORKER_FREE
                worker.campaign_run_id = ""
                worker.conversation_key = ""
            self._generation_context = {"dispatch_id": job.dispatch_id, "project_id": job.project_id, "state": job.state}
            self._persist_jobs()
            self._work_available.notify_all()
            return job

    def abandon_job(self, dispatch_id: str, reason: str = "") -> DispatchJob:
        """Operator escape hatch for a stuck post-start dispatch.

        Any live post-start state qualifies, not only BLOCKED. A run whose
        worker window was closed can sit in AUDITING with nobody behind it, and
        Cancel refuses it on principle -- so the operator had NO way to clear
        the lane and START AUDIT kept answering "already active".

        Cancel deliberately refuses a BLOCKED job that may already own an
        irreversible START, because CANCELLED means "no Core was sent" and
        cancelling would free the project for a second Core. Abandon is the
        honest alternative: the run becomes terminal FAILED, so the lane stops
        occupying capacity and START AUDIT works again, while the record keeps
        its post-start lineage (campaign_run_id / start_receipt) and states
        plainly that a Core may have been sent. The dispatch is never re-leased
        and no automatic new START is issued.
        """
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                raise DispatchError("unknown_job", "dispatch_id is unknown")
            if job.state in TERMINAL_STATES:
                return job
            if job.state not in POST_START_STATES | {JOB_BLOCKED}:
                raise DispatchError(
                    "invalid_transition",
                    "only a post-start dispatch can be abandoned; cancel pre-start work instead",
                )
            if self.safe_prestart_cancel(job):
                raise DispatchError(
                    "invalid_transition",
                    "pre-start BLOCKED dispatch can be cancelled safely; abandon is for post-start recovery",
                )
            note = str(reason or "operator abandoned a stuck blocked run").strip()[:300]
            job.state = JOB_FAILED
            job.assigned_worker_id = ""
            job.lease_id = ""
            job.lease_expires_at = 0.0
            job.last_error_code = "operator_abandoned"
            job.error = (
                f"{note} (was BLOCKED: {job.error})" if job.error else note
            )[:500]
            job.completed_at = _now()
            job.updated_at = job.completed_at
            self._generation_context = {"dispatch_id": job.dispatch_id, "project_id": job.project_id, "state": job.state}
            self._persist_jobs()
            self._work_available.notify_all()
            return job

    @staticmethod
    def safe_prestart_cancel(job: DispatchJob) -> bool:
        """True only when positive evidence exists that no irreversible START occurred."""
        if job.state in POST_START_STATES:
            # Past the boundary by definition. Only BLOCKED used to be checked,
            # so a live AUDITING run read as safely cancellable and abandon
            # refused it -- leaving the operator with no way to clear the lane.
            return False
        if job.state == JOB_BLOCKED:
            if job.recovery_state in POST_START_STATES:
                return False
            if job.start_receipt:
                return False
            if job.campaign_run_id:
                return False
        return True

    def cancel_job(self, dispatch_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                return False
            if job.state not in (JOB_QUEUED, JOB_BLOCKED, JOB_RETRYABLE, *PRE_START_STATES):
                raise DispatchError("invalid_transition", "only pre-start/queued/blocked jobs can be cancelled")
            # W6: a BLOCKED job that holds post-start lineage (start_receipt,
            # campaign_run_id or a post-start recovery_state) is NOT disposable.
            # Cancelling it would break active-project dedupe and allow a second
            # Core. Ordinary cancel refuses; RECONCILE is the correct path.
            if job.state == JOB_BLOCKED and not self.safe_prestart_cancel(job):
                raise DispatchError(
                    "post_start_blocked",
                    "dispatch is BLOCKED after an irreversible start; RECONCILE, do not cancel",
                )
            # W5.1: preserve cancel owner identity so the original worker can
            # prove ownership and receive a terminal CANCELLED ACK. Only the
            # holder of the matching cancel_owner_lease_id may finalize.
            job.cancel_owner_worker_id = job.assigned_worker_id
            job.cancel_owner_lease_id = job.lease_id
            job.state = JOB_CANCELLED
            job.updated_at = _now()
            self._generation_context = {"dispatch_id": dispatch_id, "project_id": job.project_id, "state": job.state}
            self._persist_jobs()
            return True

    def finalize_cancel(self, dispatch_id: str, worker_id: str, lease_id: str) -> DispatchJob:
        """Clear cancel-owner identity once the original worker observes CANCELLED.

        A worker that polls with the recorded cancel_owner_* tokens may call
        this to drop assigned_worker_id/lease_id; any other identity gets a
        stale_owner refusal so the worker's local lease is provably terminal
        before it touches local state.
        """
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                raise DispatchError("unknown_job", "dispatch_id is unknown")
            if job.state != JOB_CANCELLED:
                raise DispatchError("invalid_transition", "dispatch is not in CANCELLED state")
            if (job.cancel_owner_worker_id and job.cancel_owner_worker_id != worker_id) or (
                job.cancel_owner_lease_id and job.cancel_owner_lease_id != lease_id
            ):
                raise DispatchError("stale_owner", "only the original cancel owner may finalize this cancellation")
            job.assigned_worker_id = ""
            job.lease_id = ""
            job.lease_expires_at = 0.0
            job.cancel_owner_worker_id = ""
            job.cancel_owner_lease_id = ""
            job.updated_at = _now()
            self._persist_jobs()
            return job

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._expire_workers()
            now = _now()
            workers = list(self._workers.values())
            live = [w for w in workers if now - w.last_seen_at <= WORKER_TTL_SECONDS]
            eligible = [w for w in live if w.state in WORKER_ACTIVE_STATES]
            # Capacity decisions must see audit lanes, not every ChatGPT tab
            # that happens to run the Widget.
            active = [w for w in eligible if self.worker_consumes_lane(w)]
            foreign = [w for w in eligible if w not in active]
            free = [w for w in active if w.state == WORKER_FREE and self.worker_free_for_claim(w)]
            busy = [w for w in active if w not in free]
            jobs = list(self._jobs.values())
            return {
                "max_workers": MAX_ACTIVE_WORKERS,
                "active_workers": len(active),
                "free_workers": len(free),
                "clean_workers": sum(1 for w in active if w.clean_for_audit),
                "busy_workers": len(busy),
                "offline_workers": self._expired_worker_count + len(workers) - len(live),
                "foreign_workers": len(foreign),
                # Named so the operator is told to update the widget instead
                # of watching six "clean" windows claim nothing.
                "stale_widget_workers": sum(1 for w in live if self.worker_widget_is_stale(w)),
                "required_widget_build": _get_required_widget_build(),
                "queued_jobs": sum(1 for j in jobs if j.state in (JOB_QUEUED, JOB_RETRYABLE)),
                "active_jobs": sum(1 for j in jobs if j.state in POST_START_STATES | {JOB_LEASED, JOB_ARTIFACT_FETCHED, JOB_ATTACHED}),
                "finalizing_jobs": sum(1 for j in jobs if j.state == JOB_FINALIZING),
                "blocked_jobs": sum(1 for j in jobs if j.state == JOB_BLOCKED),
                "failed_jobs": sum(1 for j in jobs if j.state == JOB_FAILED),
                "total_jobs": len(jobs),
            }

    # ------------------------------------------------------------------ #
    # artifact ownership
    # ------------------------------------------------------------------ #

    def resolve_artifact(self, dispatch_id: str, worker_id: str, lease_id: str) -> Optional[Path]:
        """Return the server-owned archive path for a leased job, or None.

        Ownership is verified: only the leased worker may fetch the artifact,
        and the path is the one recorded from the packing result -- never an
        arbitrary path supplied by the browser."""
        with self._lock:
            job = self._jobs.get(dispatch_id)
            if job is None:
                raise DispatchError("unknown_job", "dispatch_id is unknown")
            if job.state not in (JOB_LEASED, JOB_ARTIFACT_FETCHED, JOB_ATTACHED):
                raise DispatchError("invalid_transition", "artifact is available only before START_PREPARED")
            self._require_owner(job, worker_id, lease_id)
            path = Path(job.archive_path)
            if not path.is_file():
                raise DispatchError("missing_archive", "the recorded archive no longer exists")
            try:
                if job.archive_size and path.stat().st_size != job.archive_size:
                    raise DispatchError("changed_archive", "the recorded archive size changed")
                if job.archive_sha256 and sha256_of(path) != job.archive_sha256:
                    raise DispatchError("changed_archive", "the recorded archive digest changed")
            except DispatchError:
                raise
            except OSError as exc:
                raise DispatchError("archive_unreadable", str(exc), retriable=True) from exc
            return path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
