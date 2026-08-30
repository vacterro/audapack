"""Browser audit worker dispatcher -- broker / scheduler / lease authority.

SRC-005: AUDAPACK -> FREE BRAVE AUDIT WORKER DISPATCHER.

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
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from audapack.config import cross_process_lock, get_state_dir

MAX_ACTIVE_WORKERS = 6
WORKER_TTL_SECONDS = 75
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
    page_eligible: bool = False
    has_conversation_turns: bool = False
    clean_for_audit: bool = False
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
        self._jobs: dict[str, DispatchJob] = {}
        self._expired_worker_count = 0
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

    def register_worker(self, payload: dict[str, Any]) -> WorkerRecord:
        with self._lock:
            self._expire_workers()
            wid = str(payload.get("worker_id") or "").strip()
            if not WORKER_ID_RE.fullmatch(wid):
                raise DispatchError("invalid_worker_id", "worker_id must be 1-128 safe identifier characters")
            if wid not in self._workers and len(self._workers) >= MAX_ACTIVE_WORKERS:
                incoming_supported = (
                    str(payload.get("widget_version") or "") == SUPPORTED_BROWSER_WIDGET_VERSION
                    and bool(payload.get("is_brave", False))
                    and bool(payload.get("page_eligible", False))
                    and str(payload.get("site") or "chatgpt") == "chatgpt"
                    and str(payload.get("url_path") or "") == "/"
                )
                replaceable = [
                    worker for worker in self._workers.values()
                    if worker.widget_version.startswith("AUDAPACK_WIDGET")
                    and not self.worker_free_for_claim(worker)
                    and not worker.campaign_run_id
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
            record.bridge_api_version = str(payload.get("bridge_api_version") or record.bridge_api_version)
            record.site = str(payload.get("site") or "chatgpt")
            record.conversation_key = str(payload.get("conversation_key") or record.conversation_key)
            record.conversation_id = str(payload.get("conversation_id") or record.conversation_id)
            record.url_path = str(payload.get("url_path") or "")
            record.project_name = str(payload.get("project_name") or "")
            record.profile = str(payload.get("profile") or "")
            record.campaign_run_id = str(payload.get("campaign_run_id") or "")
            record.generating = bool(payload.get("generating", False))
            record.has_manual_draft = bool(payload.get("has_manual_draft", False))
            record.has_attachments = bool(payload.get("has_attachments", False))
            record.audit_start_in_flight = bool(payload.get("audit_start_in_flight", False))
            record.action_in_flight = bool(payload.get("action_in_flight", False))
            record.is_brave = bool(payload.get("is_brave", False))
            record.page_eligible = bool(payload.get("page_eligible", False))
            record.has_conversation_turns = bool(payload.get("has_conversation_turns", False))
            record.clean_for_audit = bool(payload.get("clean_for_audit", False))
            if payload.get("browser_name"):
                record.meta["browser_name"] = str(payload.get("browser_name"))[:80]
            record.last_seen_at = _now()
            if reported_state == WORKER_OFFLINE:
                self._workers.pop(wid, None)
                return record
            self._workers[wid] = record

            dispatch_id = str(payload.get("dispatch_id") or "").strip()
            lease_id = str(payload.get("lease_id") or "").strip()
            if dispatch_id or lease_id:
                if not (dispatch_id and lease_id):
                    raise DispatchError("invalid_lease", "dispatch_id and lease_id must be reported together")
                job = self._jobs.get(dispatch_id)
                if job and self._is_recovery_block(job):
                    self.reconcile_job(dispatch_id, wid, lease_id, payload)
                else:
                    self.renew_lease(dispatch_id, wid, lease_id)
            return record

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
            if not worker.is_brave or not worker.page_eligible:
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
        if any(
            job.assigned_worker_id == worker.worker_id
            and job.state not in TERMINAL_STATES | {JOB_BLOCKED}
            for job in self._jobs.values()
        ):
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
            if not run_id or run_id != job.campaign_run_id:
                raise DispatchError("run_id_conflict", "recovery campaign_run_id does not match dispatch")
            if job.start_receipt and receipt != job.start_receipt:
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
                if job.state in (JOB_LEASED, JOB_ARTIFACT_FETCHED, JOB_ATTACHED) and now > job.lease_expires_at:
                    job.state = JOB_QUEUED
                    job.assigned_worker_id = ""
                    job.lease_id = ""
                    job.lease_expires_at = 0
                    job.updated_at = now
                    requeued += 1
                elif job.state in POST_START_STATES and now > job.lease_expires_at:
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
                and job.state in (JOB_AUDITING, JOB_FINALIZING)
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

    @staticmethod
    def safe_prestart_cancel(job: DispatchJob) -> bool:
        """True only when positive evidence exists that no irreversible START occurred."""
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
            active = [w for w in live if w.state in WORKER_ACTIVE_STATES]
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
