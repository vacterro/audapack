"""Bridge-owned local scheduler worker. Widgets never own timers or probes."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta

from audapack.account_registry import AccountIdentity, AccountRegistry, discover_accounts
from audapack.auto_account import AutoCandidate, AutoSelection, rank_auto_accounts
from audapack.config import AppConfig, load_config
from audapack.instances import InstanceMonitor
from audapack.limit_adapters import AntigravityLimitAdapter, ClaudeLimitAdapter, CodexLimitAdapter
from audapack.limits import Availability, LimitCoordinator, LimitSnapshot, LimitStore, parse_time, utc_now
from audapack.prepared import (
    JobState,
    Payload,
    PreparedJob,
    PreparedScheduler,
    PreparedStore,
    Trigger,
    evaluate_trigger,
    reconcile_reset,
)
from audapack.prepared_audit import AuditProgressState, audit_outcome
from audapack.prepared_delivery import PreflightError, _payload, build_launch_plan, execute_claimed
from audapack.prepared_prime import PrimeCoordinator, PrimeStore, PrimeTarget
from audapack.prepared_sync import PreparedSyncMember, SyncCoordinator, SyncMemberSpec, SyncStore
from audapack.provider_capabilities import PROVIDERS

logger = logging.getLogger(__name__)

#: W2-001: how long `stop()` waits for the scheduler thread and for deliveries
#: this worker already owns. Bounded, because a delivery can block on a browser;
#: the bound is what makes `stop()` reportable rather than silent.
PREPARED_STOP_TIMEOUT_SECONDS = 10.0

#: W2-003: how often terminal execution history is compacted. Slow on
#: purpose -- it is housekeeping, never a correctness step.
PREPARED_COMPACTION_INTERVAL_SECONDS = 300.0


class PreparedWorker:
    """One worker in the sole Bridge process, with SQLite event claims as fence."""

    def __init__(self, config: AppConfig | None = None, *, clock=utc_now,
                 account_registry: AccountRegistry | None = None,
                 limit_store: LimitStore | None = None,
                 prepared_store: PreparedStore | None = None,
                 audit_runtime=None,
                 sync_store: SyncStore | None = None,
                 sync_coordinator: SyncCoordinator | None = None,
                 prime_store: PrimeStore | None = None,
                 prime_coordinator: PrimeCoordinator | None = None) -> None:
        self.config = config or load_config()
        self.clock = clock
        self.accounts = account_registry or AccountRegistry()
        self.limits = limit_store or LimitStore()
        self.jobs = prepared_store or PreparedStore()
        self.coordinator = LimitCoordinator(
            {"codex": CodexLimitAdapter(), "claude": ClaudeLimitAdapter(),
             "antigravity": AntigravityLimitAdapter()}, self.limits, clock,
        )
        self.scheduler = PreparedScheduler(self.jobs, clock)
        self.instance_monitor = InstanceMonitor()
        # One prepared audit runtime for the whole Bridge scheduler process --
        # never constructed lazily per tick, never one coordinator per job.
        # Its absence is a truthful gate reason, never a crash.
        self.audit_runtime = audit_runtime
        self.sync_store = sync_store or SyncStore(self.jobs.path)
        self.sync_coordinator = sync_coordinator
        self.prime_store = prime_store or PrimeStore(self.jobs.path)
        self.prime_coordinator = prime_coordinator
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="prepared-launch")
        self._probe_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="limit-probe")
        self._probe_futures: dict[str, Future] = {}
        # W2-001 (audit/12.md): the ownership barrier. `_lifecycle` is the single
        # admission gate for every executor submission, `_inflight` is the exact
        # set of work this worker still owns, and `lifecycle` is what a caller
        # reads instead of assuming that `stop()` returning means quiescence.
        self._lifecycle = "OPEN"
        self._admission = threading.Lock()
        self._inflight: set[Future] = set()
        self._last_compaction = 0.0
        self.status_snapshot: dict = {"state": "STARTING"}

    @property
    def lifecycle(self) -> str:
        """OPEN | CLOSING | CLOSED. CLOSING means not yet quiescent."""
        return self._lifecycle

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._lifecycle = "OPEN"
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="audapack-prepared", daemon=True)
        self._thread.start()

    def _submit(self, pool: ThreadPoolExecutor, fn, *args, **kwargs):
        """The ONLY path to an executor from this worker.

        W2-001: a tick could reach `_pool.submit` after `stop()` had already
        called `shutdown()`, raising `RuntimeError` inside the scheduler thread
        and stranding the job. Admission is taken under the same lock that
        `stop()` uses to flip the lifecycle, so a submission and an executor
        shutdown can never interleave. Returns None once shutdown begins, which
        makes a late tick a no-op instead of a crash.
        """
        with self._admission:
            if self._lifecycle != "OPEN":
                return None
            future = pool.submit(fn, *args, **kwargs)
            # A test may stub `pool.submit` with a direct call; only a real
            # Future carries ownership we can wait on.
            if isinstance(future, Future):
                self._inflight.add(future)
        return future

    def stop(self, timeout: float = PREPARED_STOP_TIMEOUT_SECONDS) -> bool:
        """Close admission, then WAIT for this worker's own work to be done.

        W2-001 (audit/12.md): the old `stop()` set an event, joined the
        scheduler for 5s and shut both pools down with `wait=False`, then
        returned. It told the caller nothing, so `run_bridge_server()` went on
        to `server_close()` and PID removal while a delivery could still launch a
        browser -- and the scheduler thread could still submit to a closed
        executor. It also left `_thread` set forever, so `start()` could never
        run again on the same object.

        Returns True only when the scheduler thread has exited AND every future
        this worker still owns has finished. Never joins itself: a stop
        originating on the scheduler thread would deadlock, and that is reported
        as not-quiescent rather than as success.
        """
        with self._admission:
            self._lifecycle = "CLOSING"
        self._stop.set()
        self._wake.set()
        thread = self._thread
        joined = True
        if thread is not None and thread.is_alive():
            if thread is threading.current_thread():
                joined = False
            else:
                thread.join(max(0.0, float(timeout)))
                if thread.is_alive():
                    logger.warning(
                        "prepared scheduler did not finish its tick within %.1fs", timeout)
                    joined = False
                else:
                    # Only a thread that is provably dead may release its handle.
                    self._thread = None
        # cancel_futures drops everything that has not begun; running deliveries
        # are still owned and are waited for below.
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._probe_pool.shutdown(wait=False, cancel_futures=True)
        owned = [f for f in self._inflight if not f.done()]
        deadline = time.monotonic() + max(0.0, float(timeout))
        for future in owned:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                future.exception(timeout=remaining)
            except (TimeoutError, CancelledError):
                continue
            except Exception:
                continue
        self._inflight = {f for f in self._inflight if not f.done()}
        quiescent = joined and not self._inflight
        if quiescent:
            self._lifecycle = "CLOSED"
            self.status_snapshot = {"state": "STOPPED", **self.status_snapshot,
                                    "state_note": "quiescent"}
        else:
            self._lifecycle = "CLOSING"
            logger.warning("prepared worker is still owning %d future(s) at stop",
                           len(self._inflight))
        return quiescent

    def wake(self) -> None:
        self._wake.set()

    def _known_accounts(self) -> dict[str, AccountIdentity]:
        found = discover_accounts(self.config.launchers, now=self.clock())
        self.accounts.upsert(found)
        present = {account.account_id for account in found}
        return {account.account_id: account for account in self.accounts.list()
                if account.enabled and account.account_id in present}

    def _probe_due(self, accounts: dict[str, AccountIdentity]) -> None:
        for identity, future in list(self._probe_futures.items()):
            if future.done():
                self._probe_futures.pop(identity, None)
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("limit probe failed for %s: %s", identity,
                                   type(exc).__name__)
        for account in accounts.values():
            if self._stop.is_set():
                return
            if account.account_id in self._probe_futures:
                continue
            stored = self.limits.get(account.account_id)
            if stored and self.clock() < stored[1]:
                continue
            self._probe_futures[account.account_id] = self._submit(
                self._probe_pool, self.coordinator.refresh, account,
            )

    def _snapshot(self, account_id: str) -> LimitSnapshot | None:
        result = self.limits.get(account_id)
        return result[0] if result else None

    @staticmethod
    def _quota_bucket(job: PreparedJob, account: AccountIdentity,
                      snapshot: LimitSnapshot | None) -> tuple[str | None, str]:
        selected = str(job.trigger_config.get("quota_bucket") or "")
        if snapshot is None:
            return None, ""
        pools = {window.quota_bucket for window in snapshot.windows if window.quota_bucket}
        if selected and pools and selected not in pools:
            return None, "selected quota bucket is not reported"
        if len(pools) <= 1:
            return selected or None, ""
        capabilities = PROVIDERS.get(account.provider_id)
        mapped = capabilities.bucket_for_model(job.model) if capabilities else None
        if job.model:
            if not mapped:
                return None, "model quota bucket unknown"
            if mapped not in pools:
                return None, "model quota bucket is not reported"
            if selected and selected != mapped:
                return None, "selected quota bucket disagrees with model"
            return mapped, ""
        if selected:
            return None, "default model quota bucket unknown"
        return None, ""

    def _verify_due(self, job: PreparedJob, account: AccountIdentity,
                    snapshot: LimitSnapshot | None,
                    quota_bucket: str | None = None) -> tuple[PreparedJob, LimitSnapshot | None]:
        now = self.clock()
        if job.trigger == Trigger.ON_RESET:
            armed = reconcile_reset(job, snapshot, now, quota_bucket=quota_bucket)
            if armed != job:
                self.jobs.save(armed)
                job = armed
            due = parse_time(job.next_due_at) if job.next_due_at else None
        elif job.trigger == Trigger.ON_TIME:
            due = parse_time(str(job.trigger_config.get("at")))
        else:
            return job, snapshot
        if due is None or now < due:
            return job, snapshot
        if snapshot and snapshot.availability(now, quota_bucket=quota_bucket) in (Availability.AVAILABLE, Availability.LOW) and (
            job.trigger_config.get("verified_event") == job.next_due_at or
            job.trigger_config.get("verified_event") == job.trigger_config.get("at")
        ):
            return job, snapshot
        count = int(job.trigger_config.get("verification_attempts", 0))
        if count >= 3:
            return job, snapshot
        next_at = job.trigger_config.get("next_verification_at")
        next_dt = parse_time(next_at)
        if next_dt and now < next_dt:
            return job, snapshot
        verified = self.coordinator.refresh(account, force=True)
        updated = self.jobs.get(job.prepared_id) or job
        config = dict(updated.trigger_config)
        config["verification_attempts"] = count + 1
        delays = (60, 60, 300)
        config["next_verification_at"] = (now + timedelta(seconds=delays[count])).isoformat()
        if verified and verified.availability(now, quota_bucket=quota_bucket) in (Availability.AVAILABLE, Availability.LOW):
            config["verified_event"] = job.next_due_at if job.trigger == Trigger.ON_RESET else job.trigger_config.get("at")
        persisted = replace(updated, trigger_config=config,
                            waiting_reason=f"verification {count + 1}/3")
        self.jobs.save(persisted)
        return replace(persisted, account_id=job.account_id,
                       launcher_id=job.launcher_id), verified

    def _deliver(self, job: PreparedJob, account: AccountIdentity, execution_id: str,
                 generation: int = 1) -> None:
        try:
            snapshot = self.instance_monitor.scan(self.config.projects, self.config.launchers)
            if snapshot.last_error:
                raise PreflightError("instance capacity could not be verified", retryable=True)
            self.instance_monitor.apply_snapshot(snapshot)
            plan = build_launch_plan(job, account, self.config,
                                     monitor=self.instance_monitor)
        except PreflightError as exc:
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation, JobState.CLAIMED,
                              JobState.FAILED_RETRYABLE if exc.retryable else JobState.FAILED_TERMINAL,
                              result=str(exc), now=self.clock())
            return
        try:
            execute_claimed(plan, job, self.jobs, execution_id, self.scheduler.owner_id,
                            generation,
                            monitor=self.instance_monitor)
            # Actual provider use can move a predicted reset. Observe once
            # immediately through the cheap status adapter, independent of the
            # ordinary fifteen-minute cadence.
            try:
                self.coordinator.refresh(account, force=True)
            except Exception as exc:
                logger.warning("post-use limit observation failed for %s: %s",
                               account.account_id, type(exc).__name__)
        except Exception as exc:
            # A crash after LAUNCHING might already have created a process.
            # Keep its unique receipt and refuse an automatic second launch.
            logger.error("prepared execution %s stopped in an uncertain stage: %s",
                         execution_id, type(exc).__name__)

    def _auto_candidates(self, job: PreparedJob,
                         accounts: dict[str, AccountIdentity], now: datetime,
                         ) -> tuple[list[AutoSelection], str]:
        try:
            scan = self.instance_monitor.scan(self.config.projects, self.config.launchers)
            if scan.last_error:
                return [], "instance capacity could not be verified"
            self.instance_monitor.apply_snapshot(scan)
        except Exception:
            return [], "instance capacity could not be verified"
        reservations = self.jobs.reservations(now)
        preferred_provider = str(job.trigger_config.get("auto_provider_id") or "")
        candidates: list[AutoCandidate] = []
        reasons: list[str] = []
        for account in sorted(accounts.values(), key=lambda item: item.account_id):
            if preferred_provider and account.provider_id != preferred_provider:
                continue
            if len(account.launcher_ids) != 1 or account.account_id in reservations:
                continue
            snapshot = self._snapshot(account.account_id)
            bucket, error = self._quota_bucket(job, account, snapshot)
            if error or snapshot is None:
                reasons.append(error or "limit state unavailable")
                continue
            if snapshot.availability(now, quota_bucket=bucket) not in (Availability.AVAILABLE,
                                                                       Availability.LOW):
                reasons.append("account limit unavailable")
                continue
            launcher_id = account.launcher_ids[0]
            resolved = replace(job, account_id=account.account_id, launcher_id=launcher_id)
            try:
                build_launch_plan(resolved, account, self.config,
                                  monitor=self.instance_monitor)
            except PreflightError as exc:
                reasons.append(str(exc))
                continue
            candidates.append(AutoCandidate(
                account, launcher_id, snapshot, bucket,
                self.instance_monitor.count_for_launcher(launcher_id),
            ))
        ranked = rank_auto_accounts(job, candidates, now)
        return ranked, "AUTO no eligible account: " + (reasons[0] if reasons else "none discovered")

    def _tick_auto(self, job: PreparedJob, accounts: dict[str, AccountIdentity],
                   now: datetime) -> datetime | None:
        if job.trigger != Trigger.ON_TIME:
            reason = "AUTO requires an account-specific reset policy for this trigger"
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                       waiting_reason=reason))
            return None
        at = parse_time(job.trigger_config.get("at"))
        if at is None:
            return None
        if now < at:
            decision = evaluate_trigger(job, None, now)
            if (job.state != decision.state or job.waiting_reason != decision.reason or
                    job.next_due_at != decision.due_at):
                self.jobs.save(replace(job, state=decision.state,
                                       waiting_reason=decision.reason,
                                       next_due_at=decision.due_at))
            return at
        if now > at + timedelta(seconds=job.catch_up_seconds):
            decision = evaluate_trigger(job, None, now)
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            decision.reason, now)
            return None
        selections, wait_reason = self._auto_candidates(job, accounts, now)
        for choice in selections:
            candidate = choice.candidate
            resolved = replace(job, account_id=candidate.account.account_id,
                               launcher_id=candidate.launcher_id)
            verified_job, snapshot = self._verify_due(
                resolved, candidate.account, candidate.snapshot, candidate.quota_bucket)
            resolved = replace(verified_job, account_id=candidate.account.account_id,
                               launcher_id=candidate.launcher_id)
            bucket, bucket_error = self._quota_bucket(resolved, candidate.account, snapshot)
            if bucket_error:
                wait_reason = bucket_error
                continue
            decision, execution_id = self.scheduler.due(
                resolved, snapshot, quota_bucket=bucket,
                selected_account_id=candidate.account.account_id,
                selected_launcher_id=candidate.launcher_id,
                selection_reason=choice.reason,
            )
            if execution_id:
                receipt = self.jobs.receipt(job.prepared_id, decision.event_id)
                if receipt:
                    self._submit(self._pool, self._deliver, resolved, candidate.account,
                                      execution_id, receipt["claim_generation"])
                return None
            if decision.state == JobState.MISSED:
                self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                                self.scheduler.owner_id, JobState.MISSED,
                                                decision.reason, now)
                return None
            if decision.state == JobState.WAITING_LIMIT:
                wait_reason = decision.reason
        if job.trigger_config.get("availability_policy") == "STRICT_TIME":
            decision = evaluate_trigger(job, None, now)
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            wait_reason, now)
            return None
        current = self.jobs.get(job.prepared_id) or job
        if current.state not in (JobState.CLAIMED, JobState.PREPARING, JobState.LAUNCHING,
                                 JobState.DELIVERING, JobState.VERIFYING, JobState.RUNNING,
                                 JobState.RECOVERY_REQUIRED):
            if current.state != JobState.WAITING_LIMIT or current.waiting_reason != wait_reason:
                self.jobs.save(replace(current, state=JobState.WAITING_LIMIT,
                                       waiting_reason=wait_reason, next_due_at=at.isoformat()))
        return None

    def _tick_audit(self, job: PreparedJob, accounts: dict[str, AccountIdentity],
                    now: datetime) -> None:
        """Gate, claim once, and enter the canonical audit pipeline.

        AUDIT jobs use ON_TIME/ON_RESET triggers like any other; the audit
        distinction is the payload, not the trigger. When the runtime is
        absent or a capability gate is closed, the job waits with the exact
        reason. When every gate passes and the trigger is due, the execution
        is CLAIMED and delivery runs the coordinator start under
        source_execution_id.
        """
        if self.audit_runtime is None:
            reason = "Audit unavailable: AuditRunCoordinator runtime not connected"
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                       waiting_reason=reason))
            return
        gate = self.audit_runtime.gate(job)
        if not gate.ok:
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != gate.reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                       waiting_reason=gate.reason))
            return
        if self.audit_runtime.unrelated_active_audit(job.project_id):
            reason = "Audit waiting: project has an unrelated active audit"
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                       waiting_reason=reason))
            return
        # AUDIT does not consume the CLI limit bucket; evaluate_trigger claims
        # on time. A missing account only matters for VERIFIED binding, which
        # the gate already checked; pass no snapshot.
        decision, execution_id = self.scheduler.due(job, None)
        if decision.state == JobState.CLAIMED and execution_id:
            receipt = self.jobs.receipt(job.prepared_id, decision.event_id)
            if receipt:
                self._submit(self._pool, self._deliver_audit, job, execution_id,
                                  receipt["claim_generation"])
        elif decision.state == JobState.MISSED:
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            decision.reason, now)
        elif decision.state in (JobState.WAITING_LIMIT, JobState.WAITING_TRIGGER):
            current = self.jobs.get(job.prepared_id) or job
            if current.state != decision.state or current.waiting_reason != decision.reason:
                self.jobs.save(replace(current, state=decision.state,
                                       waiting_reason=decision.reason,
                                       next_due_at=decision.due_at))

    def _resolve_member_payload_sha(self, job: PreparedJob, member: PreparedSyncMember) -> str:
        try:
            if member.payload_policy:
                synthetic_job = replace(job, payload=Payload(member.payload_policy),
                                        payload_config=member.payload_config)
            else:
                synthetic_job = job
            body, _ = _payload(synthetic_job, self.config)
            return hashlib.sha256(body).hexdigest()
        except Exception:
            return ""

    def _workflow_wait_reason(self, job: PreparedJob,
                              accounts: dict[str, AccountIdentity]) -> str:
        """Exact per-workflow gate reason; a truthful wait, never a fake ship."""
        if job.payload == Payload.AUDIT:
            if self.audit_runtime is None:
                return "Audit unavailable: AuditRunCoordinator runtime not connected"
            return self.audit_runtime.gate(job).reason
        if job.trigger == Trigger.SYNC:
            sync_members = self.jobs.get_sync_members(job.prepared_id)
            if len(sync_members) >= 2:
                for m in sync_members:
                    acc = accounts.get(m.account_id)
                    if acc is None:
                        return "SYNC unavailable: provider capability unknown"
                    caps = PROVIDERS.get(acc.provider_id)
                    if caps is None:
                        return "SYNC unavailable: provider capability unknown"
                    if not caps.supports_sync:
                        return f"SYNC unavailable: {acc.provider_id} window start semantics {caps.window_start_semantics.value}"
                if self.sync_coordinator is None:
                    return "SYNC unavailable: worker runtime not connected"
                return ""
        if job.trigger == Trigger.PRIME:
            account = accounts.get(job.account_id)
            if account is None:
                return "PRIME unavailable: provider capability unknown"
            caps = PROVIDERS.get(account.provider_id)
            if caps is None:
                return "PRIME unavailable: provider capability unknown"
            if not caps.supports_prime:
                return f"PRIME unavailable: {account.provider_id} window start semantics {caps.window_start_semantics.value}"
            if self.prime_coordinator is None:
                return "PRIME unavailable: worker runtime not connected"
            return ""
        account = accounts.get(job.account_id)
        capabilities = PROVIDERS.get(account.provider_id) if account else None
        semantics = (capabilities.window_start_semantics.value
                     if capabilities else "UNKNOWN")
        label = job.trigger.value
        if capabilities is None:
            return f"{label} unavailable: provider capability unknown"
        supported = capabilities.supports_sync if job.trigger == Trigger.SYNC else capabilities.supports_prime
        if not supported:
            return f"{label} unavailable: {account.provider_id} window start semantics {semantics}"
        return f"{label} unavailable: worker runtime not connected"

    def _tick_sync(self, job: PreparedJob, accounts: dict[str, AccountIdentity],
                   now: datetime) -> datetime | None:
        sync_members = self.jobs.get_sync_members(job.prepared_id)
        if len(sync_members) < 2:
            reason = self._workflow_wait_reason(job, accounts)
            if self.sync_coordinator is not None and "worker runtime not connected" in reason:
                reason = "SYNC requires at least two distinct accounts"
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT, waiting_reason=reason))
            return None

        # Check all member accounts and capabilities
        for m in sync_members:
            account = accounts.get(m.account_id)
            if account is None:
                reason = "SYNC unavailable: provider capability unknown"
                if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                    self.jobs.save(replace(job, state=JobState.WAITING_LIMIT, waiting_reason=reason))
                return None
            caps = PROVIDERS.get(account.provider_id)
            if caps is None:
                reason = "SYNC unavailable: provider capability unknown"
                if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                    self.jobs.save(replace(job, state=JobState.WAITING_LIMIT, waiting_reason=reason))
                return None
            if not caps.supports_sync:
                reason = f"SYNC unavailable: {account.provider_id} window start semantics {caps.window_start_semantics.value}"
                if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                    self.jobs.save(replace(job, state=JobState.WAITING_LIMIT, waiting_reason=reason))
                return None

        if self.sync_coordinator is None:
            reason = "SYNC unavailable: worker runtime not connected"
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT, waiting_reason=reason))
            return None

        at = parse_time(job.trigger_config.get("at"))
        if at is None:
            return None
        if now < at:
            decision = evaluate_trigger(job, None, now)
            if (job.state != decision.state or job.waiting_reason != decision.reason or
                    job.next_due_at != decision.due_at):
                self.jobs.save(replace(job, state=decision.state,
                                       waiting_reason=decision.reason,
                                       next_due_at=decision.due_at))
            return at
        if now > at + timedelta(seconds=job.catch_up_seconds):
            decision = evaluate_trigger(job, None, now)
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            decision.reason, now)
            return None

        decision, execution_id = self.scheduler.due(job, None)
        if decision.state == JobState.CLAIMED and execution_id:
            receipt = self.jobs.receipt(job.prepared_id, decision.event_id)
            if receipt:
                specs = []
                for m in sync_members:
                    account = accounts[m.account_id]
                    sha256 = self._resolve_member_payload_sha(job, m)
                    specs.append(SyncMemberSpec(
                        account_id=m.account_id,
                        launcher_id=m.launcher_id,
                        provider_id=account.provider_id,
                        model=m.model or job.model,
                        effort=m.effort or job.effort,
                        payload_sha256=sha256 or ("0" * 64),
                    ))
                target_window = job.trigger_config.get("target_window", "five_hour")
                release_policy = job.trigger_config.get("release_policy", "ALL_READY")
                group_id = self.sync_store.create(
                    job.prepared_id, decision.event_id, target_window, release_policy, specs, now
                )
                self._submit(self._pool, self._deliver_sync, job, execution_id, group_id,
                                  receipt["claim_generation"])
        elif decision.state == JobState.MISSED:
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            decision.reason, now)
        elif decision.due_at:
            due = parse_time(decision.due_at)
            if due and now < due:
                return due
        if decision.state in (JobState.WAITING_LIMIT, JobState.WAITING_TRIGGER):
            current = self.jobs.get(job.prepared_id) or job
            if current.state != decision.state or current.waiting_reason != decision.reason:
                self.jobs.save(replace(current, state=decision.state,
                                       waiting_reason=decision.reason,
                                       next_due_at=decision.due_at))
        return None

    def _deliver_sync(self, job: PreparedJob, execution_id: str,
                      group_id: str, generation: int) -> None:
        if self.sync_coordinator is None:
            return
        now = self.clock()
        if not self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                 JobState.CLAIMED, JobState.PREPARING, now=now):
            return
        try:
            ready = self.sync_coordinator.prepare(group_id)
        except Exception as exc:
            logger.error("sync prepare failed for %s: %s", group_id, type(exc).__name__)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.PREPARING, JobState.FAILED_RETRYABLE,
                              result=f"sync prepare error: {type(exc).__name__}", now=now)
            return

        if not ready:
            group = self.sync_store.get(group_id)
            reasons = []
            if group:
                for m in group.get("members", []):
                    if m.get("preflight_state") != "READY" and m.get("result"):
                        reasons.append(f"{m['account_id']}: {m['result']}")
            wait_reason = "; ".join(reasons) or "one or more members failed preflight"
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.PREPARING, JobState.FAILED_RETRYABLE,
                              result=wait_reason, now=now)
            return

        if not self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                 JobState.PREPARING, JobState.LAUNCHING, now=now):
            return

        try:
            result = self.sync_coordinator.release(group_id, self.clock())
        except Exception as exc:
            logger.error("sync release failed for %s: %s", group_id, type(exc).__name__)
            self.sync_store.recover_interrupted_release(group_id)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.RECOVERY_REQUIRED,
                              result=f"sync release error: {type(exc).__name__}", now=now)
            return

        if result is None:
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.RECOVERY_REQUIRED,
                              result="sync release returned no result", now=now)
            return

        skew_str = f" skew={result.get('process_skew_ms')}ms" if result.get('process_skew_ms') is not None else ""
        if result.get("state") == "LAUNCHED":
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.DELIVERING,
                              delivery_hash=group_id, result="sync members released", now=now)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.DELIVERING, JobState.DONE,
                              result=f"sync group {group_id} launched{skew_str}", now=now)
        else:
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.RECOVERY_REQUIRED,
                              result=f"sync group {group_id} partial{skew_str}", now=now)

    def _reconcile_sync(self, receipt: dict, now: datetime) -> None:
        execution_id = receipt["execution_id"]
        job = self.jobs.get(receipt["prepared_id"])
        if job is None:
            return
        reown = self.jobs.reown_sync(execution_id, self.scheduler.owner_id, now)
        if reown is None:
            return
        _state, generation = reown
        group = self.sync_store.get_by_event(receipt["prepared_id"], receipt["trigger_event_id"])
        current_state = JobState(str(receipt["state"]))
        if group is None:
            if current_state in (JobState.CLAIMED, JobState.PREPARING):
                sync_members = self.jobs.get_sync_members(job.prepared_id)
                accounts = self._known_accounts()
                if len(sync_members) >= 2 and all(m.account_id in accounts for m in sync_members):
                    specs = [
                        SyncMemberSpec(
                            m.account_id, m.launcher_id, accounts[m.account_id].provider_id,
                            m.model or job.model, m.effort or job.effort,
                            self._resolve_member_payload_sha(job, m) or ("0" * 64),
                        )
                        for m in sync_members
                    ]
                    target_window = job.trigger_config.get("target_window", "five_hour")
                    release_policy = job.trigger_config.get("release_policy", "ALL_READY")
                    group_id = self.sync_store.create(
                        job.prepared_id, receipt["trigger_event_id"], target_window, release_policy, specs, now
                    )
                    self._deliver_sync(job, execution_id, group_id, generation)
                else:
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      current_state, JobState.RECOVERY_REQUIRED,
                                      result="sync membership unavailable during recovery", now=now)
            else:
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.RECOVERY_REQUIRED,
                                  result="sync group missing after launch attempt", now=now)
            return

        group_id = group["sync_group_id"]
        group_state = group["state"]
        skew_str = f" skew={group.get('process_skew_ms')}ms" if group.get('process_skew_ms') is not None else ""

        if group_state in ("PENDING", "WAITING"):
            if self.sync_coordinator is not None:
                self._deliver_sync(job, execution_id, group_id, generation)
            else:
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.WAITING_LIMIT,
                                  result="sync runtime not connected", now=now)
        elif group_state == "READY":
            if self.sync_coordinator is not None:
                if current_state != JobState.LAUNCHING:
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      current_state, JobState.LAUNCHING, now=now)
                result = self.sync_coordinator.release(group_id, now)
                if result and result.get("state") == "LAUNCHED":
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      JobState.LAUNCHING, JobState.DELIVERING,
                                      delivery_hash=group_id, result="sync members released", now=now)
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      JobState.DELIVERING, JobState.DONE,
                                      result=f"sync group {group_id} launched{skew_str}", now=now)
                else:
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      JobState.LAUNCHING, JobState.RECOVERY_REQUIRED,
                                      result=f"sync group {group_id} partial{skew_str}", now=now)
            else:
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.RECOVERY_REQUIRED,
                                  result="sync coordinator unavailable to release ready group", now=now)
        elif group_state == "RELEASING":
            self.sync_store.recover_interrupted_release(group_id)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              current_state, JobState.RECOVERY_REQUIRED,
                              result="release interrupted; operator recovery required", now=now)
        elif group_state == "LAUNCHED":
            if current_state not in (JobState.DONE, JobState.FAILED_TERMINAL):
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.DONE,
                                  result=f"sync group {group_id} launched{skew_str}", now=now)
        elif group_state in ("PARTIAL", "UNCERTAIN"):
            if current_state not in (JobState.DONE, JobState.FAILED_TERMINAL):
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.RECOVERY_REQUIRED,
                                  result=group.get("result") or f"sync group {group_id} partial{skew_str}", now=now)

    def _tick_prime(self, job: PreparedJob, accounts: dict[str, AccountIdentity],
                    now: datetime) -> datetime | None:
        reason = self._workflow_wait_reason(job, accounts)
        if reason:
            if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                self.jobs.save(replace(job, state=JobState.WAITING_LIMIT, waiting_reason=reason))
            return None

        at = parse_time(job.trigger_config.get("at"))
        if at is None:
            return None
        if now < at:
            decision = evaluate_trigger(job, None, now)
            if (job.state != decision.state or job.waiting_reason != decision.reason or
                    job.next_due_at != decision.due_at):
                self.jobs.save(replace(job, state=decision.state,
                                       waiting_reason=decision.reason,
                                       next_due_at=decision.due_at))
            return at
        if now > at + timedelta(seconds=job.catch_up_seconds):
            decision = evaluate_trigger(job, None, now)
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            decision.reason, now)
            return None

        decision, execution_id = self.scheduler.due(job, None)
        if decision.state == JobState.CLAIMED and execution_id:
            receipt = self.jobs.receipt(job.prepared_id, decision.event_id)
            if receipt:
                account = accounts[job.account_id]
                model = job.model or str(job.trigger_config.get("model") or "")
                quota_bucket = str(job.trigger_config.get("quota_bucket") or "")
                if not quota_bucket:
                    caps = PROVIDERS.get(account.provider_id)
                    quota_bucket = (caps.bucket_for_model(model) if caps else "") or "codex"
                target_window = str(job.trigger_config.get("target_window") or job.trigger_config.get("window_id") or "five_hour")
                target = PrimeTarget(
                    prepared_id=job.prepared_id,
                    trigger_event_id=decision.event_id,
                    account_id=job.account_id,
                    model=model,
                    quota_bucket=quota_bucket,
                    window_id=target_window,
                )
                prime_id = self.prime_store.create(target, now)
                self._submit(self._pool, self._deliver_prime, job, account, execution_id, prime_id,
                                  receipt["claim_generation"])
        elif decision.state == JobState.MISSED:
            self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                            self.scheduler.owner_id, JobState.MISSED,
                                            decision.reason, now)
        elif decision.due_at:
            due = parse_time(decision.due_at)
            if due and now < due:
                return due
        if decision.state in (JobState.WAITING_LIMIT, JobState.WAITING_TRIGGER):
            current = self.jobs.get(job.prepared_id) or job
            if current.state != decision.state or current.waiting_reason != decision.reason:
                self.jobs.save(replace(current, state=decision.state,
                                       waiting_reason=decision.reason,
                                       next_due_at=decision.due_at))
        return None

    def _deliver_prime(self, job: PreparedJob, account: AccountIdentity,
                       execution_id: str, prime_id: str, generation: int) -> None:
        if self.prime_coordinator is None:
            return
        now = self.clock()
        if not self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                 JobState.CLAIMED, JobState.PREPARING, now=now):
            return
        if not self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                 JobState.PREPARING, JobState.LAUNCHING, now=now):
            return
        try:
            verdict = self.prime_coordinator.execute(prime_id, account, now)
        except Exception as exc:
            logger.error("prime execute failed for %s: %s", prime_id, type(exc).__name__)
            self.prime_store.recover_interrupted(prime_id)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.RECOVERY_REQUIRED,
                              result=f"prime error: {type(exc).__name__}", now=now)
            return

        if verdict == "DONE":
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.DELIVERING,
                              delivery_hash=prime_id, result="prime request accepted", now=now)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.DELIVERING, JobState.DONE,
                              result=f"prime target window verified {prime_id}", now=now)
        elif verdict in ("UNSUPPORTED", "WRONG_BUCKET", "ALREADY_STARTED", "LIMIT_UNKNOWN"):
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.FAILED_RETRYABLE,
                              result=f"prime refused: {verdict}", now=now)
        elif verdict == "UNVERIFIED":
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.VERIFYING,
                              delivery_hash=prime_id,
                              result="prime submitted; awaiting window change", now=now)
        else:
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.LAUNCHING, JobState.RECOVERY_REQUIRED,
                              result=f"prime {verdict}; operator recovery required", now=now)

    def _reconcile_prime(self, receipt: dict, now: datetime) -> None:
        execution_id = receipt["execution_id"]
        job = self.jobs.get(receipt["prepared_id"])
        if job is None:
            return
        reown = self.jobs.reown_prime(execution_id, self.scheduler.owner_id, now)
        if reown is None:
            return
        _state, generation = reown
        prime_record = self.prime_store.get_by_event(receipt["prepared_id"], receipt["trigger_event_id"])
        current_state = JobState(str(receipt["state"]))
        if prime_record is None:
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              current_state, JobState.RECOVERY_REQUIRED,
                              result="prime record missing", now=now)
            return

        prime_id = prime_record["prime_id"]
        prime_state = prime_record["state"]

        if prime_state == "PENDING":
            if self.prime_coordinator is not None:
                accounts = self._known_accounts()
                account = accounts.get(job.account_id)
                if account:
                    self._deliver_prime(job, account, execution_id, prime_id, generation)
                else:
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      current_state, JobState.WAITING_LIMIT,
                                      result="account not discovered during prime recovery", now=now)
            else:
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.WAITING_LIMIT,
                                  result="prime runtime not connected", now=now)
        elif prime_state == "SUBMITTING":
            self.prime_store.recover_interrupted(prime_id)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              current_state, JobState.RECOVERY_REQUIRED,
                              result="submission interrupted; operator recovery required", now=now)
        elif prime_state in ("OBSERVING", "UNVERIFIED"):
            if self.prime_coordinator is not None:
                accounts = self._known_accounts()
                account = accounts.get(job.account_id)
                if account:
                    verdict = self.prime_coordinator.observe(prime_id, account)
                    if verdict == "DONE":
                        self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                          current_state, JobState.DONE,
                                          result=f"prime target window verified {prime_id}", now=now)
                    else:
                        self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                          current_state, JobState.RECOVERY_REQUIRED,
                                          result=f"prime window observation unproven {prime_id}", now=now)
                else:
                    self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                      current_state, JobState.RECOVERY_REQUIRED,
                                      result="account missing for prime observation", now=now)
            else:
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.RECOVERY_REQUIRED,
                                  result="prime coordinator unavailable to observe prime", now=now)
        elif prime_state == "DONE":
            if current_state not in (JobState.DONE, JobState.FAILED_TERMINAL):
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.DONE,
                                  result=f"prime target window verified {prime_id}", now=now)
        elif prime_state in ("RECOVERY_REQUIRED", "FAILED"):
            if current_state not in (JobState.DONE, JobState.FAILED_TERMINAL):
                self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                  current_state, JobState.RECOVERY_REQUIRED,
                                  result=prime_record.get("result") or f"prime {prime_id} failed", now=now)

    def _deliver_audit(self, job: PreparedJob, execution_id: str,
                       generation: int) -> None:
        """Enter the canonical audit pipeline once for this execution.

        The prepared execution is already CLAIMED. Move it to PREPARING, then
        DELIVERING once AuditRunCoordinator has accepted an intent/dispatch for
        source_execution_id=execution_id. Terminal convergence happens in the
        reconciler, which derives the audit's real state from canonical stores.
        """
        if self.audit_runtime is None:
            return
        if not self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                                 JobState.CLAIMED, JobState.PREPARING, now=self.clock()):
            return
        try:
            result = self.audit_runtime.start(job, execution_id)
        except Exception as exc:
            logger.error("prepared audit start failed for %s: %s",
                         execution_id, type(exc).__name__)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.PREPARING, JobState.FAILED_RETRYABLE,
                              result=f"audit start error: {type(exc).__name__}",
                              now=self.clock())
            return
        if not result.ok and not result.duplicate:
            # A refusal that created no intent/dispatch has no side effect;
            # a pre-start failure is retryable, a terminal one is not.
            state = (JobState.FAILED_TERMINAL if result.state in ("BLOCKED",)
                     else JobState.FAILED_RETRYABLE)
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.PREPARING, state,
                              result=str(result.message or "audit refused")[:240],
                              now=self.clock())
            return
        self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                          JobState.PREPARING, JobState.DELIVERING,
                          result=f"audit intent {result.intent_id} dispatch {result.dispatch_id}",
                          now=self.clock())

    def _reconcile_audit(self, receipt: dict, now: datetime) -> None:
        """Adopt an in-flight prepared audit and converge on canonical state.

        Called for an audit execution whose lease expired. There is no local
        process to observe: the durable audit start intent (keyed by
        source_execution_id) is the side effect, and the coordinator adopts a
        repeated start rather than creating a second dispatch. So re-own the
        lease, poll canonical stores, and map the result onto the prepared
        execution -- never invent an unsafe restart.
        """
        if self.audit_runtime is None:
            return
        execution_id = receipt["execution_id"]
        job = self.jobs.get(receipt["prepared_id"])
        if job is None:
            return
        reown = self.jobs.reown_audit(execution_id, self.scheduler.owner_id, now)
        if reown is None:
            return
        _state, generation = reown
        progress = self.audit_runtime.poll(job.project_id, execution_id)
        current = str(receipt["state"])
        if progress.state is AuditProgressState.DONE:
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState(current), JobState.VERIFYING, now=now,
                              result=f"dispatch {progress.dispatch_id}")
            self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                              JobState.VERIFYING, JobState.DONE, now=now,
                              result=progress.detail[:240])
            return
        classification, detail = audit_outcome(progress)
        if classification == "ACTIVE":
            # Still in flight; the re-owned lease keeps it durable until the
            # next reconcile pass observes a terminal canonical state.
            return
        target = JobState(classification)
        # Only pre-start/no-side-effect failures are retryable; a post-start
        # failure becomes RECOVERY_REQUIRED and is never auto-resubmitted.
        self.jobs.advance(execution_id, self.scheduler.owner_id, generation,
                          JobState(current), target, now=now, result=detail[:240])

    def tick(self) -> datetime:
        """One bounded pass; return next local wake time for the monotonic wait."""
        now = self.clock()
        next_wake = now + timedelta(minutes=15)
        accounts = self._known_accounts()
        for receipt in self.jobs.active_receipts():
            expiry = parse_time(receipt["lease_expires_at"]) if receipt.get("lease_expires_at") else now
            if expiry and expiry > now:
                if expiry < next_wake:
                    next_wake = expiry
                continue
            job = self.jobs.get(receipt["prepared_id"])
            # A prepared AUDIT has no local process; its side effect is the
            # durable audit start intent. Adopt it and converge on canonical
            # state rather than running the CLI pid-based recovery fence.
            if job is not None and job.payload == Payload.AUDIT:
                self._reconcile_audit(receipt, now)
                continue
            if job is not None and job.trigger == Trigger.SYNC:
                self._reconcile_sync(receipt, now)
                continue
            if job is not None and job.trigger == Trigger.PRIME:
                self._reconcile_prime(receipt, now)
                continue
            pid = receipt["process_id"]
            record = self.instance_monitor.records.get(pid) if pid else None
            backend = self.instance_monitor.backend
            resolved_job = (replace(job, account_id=receipt["account_id"],
                                    launcher_id=receipt["launcher_id"])
                            if job and job.account_id == "AUTO" else job)
            try:
                alive = bool(pid and backend.process_alive(pid))
                attributable = bool(
                    alive and record and resolved_job and receipt["process_token"] and
                    backend.process_token(pid) == receipt["process_token"] and
                    record.project_id == resolved_job.project_id and
                    record.launcher_id == resolved_job.launcher_id and
                    record.correlation_token == receipt["execution_id"] and
                    receipt["account_id"] == resolved_job.account_id and
                    receipt["project_id"] == resolved_job.project_id and
                    receipt["launcher_id"] == resolved_job.launcher_id
                )
            except Exception:
                alive, attributable = False, False
            recovery = self.jobs.recover_expired(
                receipt["execution_id"], self.scheduler.owner_id, now,
                process_alive=alive, process_attributable=attributable,
            )
            if recovery and recovery[0] == "resume":
                job = self.jobs.get(receipt["prepared_id"])
                if job and job.account_id == "AUTO":
                    job = replace(job, account_id=receipt["account_id"],
                                  launcher_id=receipt["launcher_id"])
                account = accounts.get(job.account_id) if job else None
                if job and account:
                    self._submit(self._pool, self._deliver, job, account,
                                      receipt["execution_id"], recovery[1])
        for account in accounts.values():
            stored = self.limits.get(account.account_id)
            if stored and stored[1] < next_wake:
                next_wake = stored[1]
        active_tests = self.jobs.jobs_with_active_tests()
        for job in self.jobs.list():
            if self._stop.is_set() or not job.enabled:
                continue
            if job.prepared_id in active_tests:
                continue
            if job.state in (JobState.CLAIMED, JobState.PREPARING, JobState.LAUNCHING,
                             JobState.DELIVERING, JobState.VERIFYING, JobState.RUNNING,
                             JobState.RECOVERY_REQUIRED):
                # The execution receipt owns the job until a terminal result.
                # A second pass must neither edit it nor schedule it again.
                continue
            if job.payload == Payload.AUDIT:
                self._tick_audit(job, accounts, now)
                continue
            if job.trigger == Trigger.SYNC:
                due = self._tick_sync(job, accounts, now)
                if due and now < due < next_wake:
                    next_wake = due
                continue
            if job.trigger == Trigger.PRIME:
                due = self._tick_prime(job, accounts, now)
                if due and now < due < next_wake:
                    next_wake = due
                continue
            if job.account_id == "AUTO":
                due = self._tick_auto(job, accounts, now)
                if due and now < due < next_wake:
                    next_wake = due
                continue
            account = accounts.get(job.account_id)
            if account is None:
                reason = "account not discovered"
                if job.state != JobState.WAITING_LIMIT or job.waiting_reason != reason:
                    self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                           waiting_reason=reason))
                continue
            snapshot = self._snapshot(account.account_id)
            quota_bucket, bucket_error = self._quota_bucket(job, account, snapshot)
            if bucket_error:
                if job.state != JobState.WAITING_LIMIT or job.waiting_reason != bucket_error:
                    self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                           waiting_reason=bucket_error))
                continue
            if job.trigger in (Trigger.ON_RESET, Trigger.ON_TIME):
                job, snapshot = self._verify_due(job, account, snapshot, quota_bucket)
                quota_bucket, bucket_error = self._quota_bucket(job, account, snapshot)
                if bucket_error:
                    self.jobs.save(replace(job, state=JobState.WAITING_LIMIT,
                                           waiting_reason=bucket_error))
                    continue
            decision, execution_id = self.scheduler.due(job, snapshot,
                                                        quota_bucket=quota_bucket)
            if decision.state == JobState.CLAIMED and execution_id:
                receipt = self.jobs.receipt(job.prepared_id, decision.event_id)
                if receipt:
                    self._submit(self._pool, self._deliver, job, account, execution_id,
                                      receipt["claim_generation"])
            elif decision.state == JobState.MISSED:
                self.jobs.settle_without_launch(job.prepared_id, decision.event_id,
                                                self.scheduler.owner_id, JobState.MISSED,
                                                decision.reason, now)
            elif decision.due_at:
                due = parse_time(decision.due_at)
                if due and now < due < next_wake:
                    next_wake = due
            if decision.state in (JobState.WAITING_LIMIT, JobState.WAITING_TRIGGER):
                current = self.jobs.get(job.prepared_id) or job
                if current.state != decision.state or current.waiting_reason != decision.reason:
                    self.jobs.save(replace(current, state=decision.state,
                                           waiting_reason=decision.reason,
                                           next_due_at=decision.due_at))
        # Due jobs receive their own immediate account verification above.
        # Ordinary probes run behind them and never stall the trigger loop.
        self._probe_due(accounts)
        latest_jobs = self.jobs.list()
        enabled = [job for job in latest_jobs if job.enabled]
        due_jobs = [job for job in enabled if job.next_due_at]
        next_job = min(due_jobs, key=lambda item: item.next_due_at) if due_jobs else None
        self.status_snapshot = {
            "state": "RUNNING",
            "accounts": {
                "discovered": len(accounts),
                "limit_capable": sum(account.provider_id in self.coordinator.adapters
                                     for account in accounts.values()),
                "unknown": sum(self._snapshot(account.account_id) is None
                               for account in accounts.values()),
            },
            "prepared": {
                "armed": len(enabled),
                "waiting_reset": sum(job.trigger == Trigger.ON_RESET and job.enabled
                                     for job in latest_jobs),
                "waiting_account": sum(job.enabled and job.waiting_reason == "account not discovered"
                                       for job in latest_jobs),
                "failed": sum(job.state in (JobState.FAILED_RETRYABLE,
                                             JobState.FAILED_TERMINAL) for job in latest_jobs),
                "recovery_required": sum(job.state == JobState.RECOVERY_REQUIRED
                                         for job in latest_jobs),
            },
            "next": ({"prepared_id": next_job.prepared_id, "account_id": next_job.account_id,
                      "project_id": next_job.project_id, "trigger": next_job.trigger.value,
                      "due_at": next_job.next_due_at} if next_job else None),
        }
        self._compact_history()
        return next_wake

    def _compact_history(self) -> None:
        """W2-003: terminal executions keep their identity, not their payload."""
        now = time.monotonic()
        if now - self._last_compaction < PREPARED_COMPACTION_INTERVAL_SECONDS:
            return
        self._last_compaction = now
        try:
            self.jobs.compact_executions()
        except Exception as exc:
            logger.warning("prepared execution compaction failed: %s", type(exc).__name__)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                next_wake = self.tick()
            except Exception as exc:
                logger.error("prepared scheduler pass failed: %s", type(exc).__name__)
                next_wake = self.clock() + timedelta(minutes=1)
            # Monotonic Event.wait handles normal waiting. The local 30-second
            # ceiling reconciles Windows sleep/clock changes without a provider
            # probe storm; LimitCoordinator enforces each account's cadence.
            delay = min(30.0, max(0.2, (next_wake - self.clock()).total_seconds()))
            self._wake.wait(delay)
            self._wake.clear()
