"""Headless prepared AUDIT runtime binding the scheduler to the canonical pipeline.

The Bridge process owns prepared scheduling; MainWindow owns nothing here. This
module constructs the exact service set AuditRunCoordinator already needs --
ProjectService, PackingService, BridgeService, AuditService and an optional
ComponentManager -- without importing Qt, and enforces the honest capability
gates before a prepared audit is ever allowed to start (mission CP-11):

- a browser audit cannot prove which logical provider account is signed in,
  so an exact-account audit stays ACCOUNT_UNBOUND and waits, while an
  explicitly account-agnostic audit may proceed;
- model/effort selections the browser transport cannot apply or verify are
  never silently downgraded: the job waits unless the operator explicitly
  declared them as the account's default choice;
- ``source_execution_id`` is the one idempotency anchor between a prepared
  execution and the audit start intent, so every crash boundary adopts the
  existing intent/dispatch instead of creating a second one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from audapack.config import AppConfig
from audapack.prepared import PreparedJob

logger = logging.getLogger(__name__)


class AccountBinding(Enum):
    """What the browser worker can prove about the account it would use."""

    VERIFIED_BROWSER_ACCOUNT = "VERIFIED_BROWSER_ACCOUNT"
    ACCOUNT_AGNOSTIC_EXPLICIT = "ACCOUNT_AGNOSTIC_EXPLICIT"
    ACCOUNT_UNBOUND = "ACCOUNT_UNBOUND"


class CapabilityTruth(Enum):
    """Whether a requested model/effort can be applied and verified."""

    VERIFIED = "VERIFIED"
    ACCOUNT_DEFAULT = "ACCOUNT_DEFAULT"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class AuditProgressState(Enum):
    """Canonical audit state for a prepared source execution id."""

    ABSENT = "ABSENT"
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED_PRE_START = "FAILED_PRE_START"
    FAILED_POST_START = "FAILED_POST_START"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class AuditGate:
    ok: bool
    reason: str
    profile_id: str
    account_binding: AccountBinding
    model_truth: CapabilityTruth
    effort_truth: CapabilityTruth


@dataclass(frozen=True)
class AuditProgress:
    state: AuditProgressState
    intent_id: str = ""
    dispatch_id: str = ""
    detail: str = ""


#: Mission V: no reliable browser-identity signal exists yet, so a verifier is
#: never fabricated and exact-account audits remain truthfully gated.
BrowserIdentityVerifier = Callable[[str], bool]


def _profile_exists(profile_id: str) -> bool:
    try:
        from audapack.campaign import get_profile

        get_profile(str(profile_id or ""))
        return True
    except Exception:
        return False


class PreparedAuditRuntime:
    """Coordinator-backed audit runtime; idempotent through source_execution_id."""

    def __init__(self, coordinator, config: AppConfig | None = None,
                 identity_verifier: BrowserIdentityVerifier | None = None) -> None:
        self.coordinator = coordinator
        self.config = config
        # No verifier is installed by default: window titles and slot numbers
        # prove nothing about the signed-in account, so pretending they do
        # would silently weaken an exact-account audit.
        self._identity_verifier = identity_verifier

    # ---------------------------------------------------------------- gates
    def account_binding(self, job: PreparedJob) -> AccountBinding:
        if str(job.payload_config.get("account_policy") or "").upper() in (
            "ANY_ELIGIBLE", "ACCOUNT_AGNOSTIC", "ANY"
        ):
            return AccountBinding.ACCOUNT_AGNOSTIC_EXPLICIT
        if self._identity_verifier is not None and self._identity_verifier(job.account_id):
            return AccountBinding.VERIFIED_BROWSER_ACCOUNT
        return AccountBinding.ACCOUNT_UNBOUND

    def _capability_truth(self, requested: str, policy: str) -> CapabilityTruth:
        if not requested:
            return CapabilityTruth.VERIFIED
        if str(policy or "").upper() in ("BROWSER_DEFAULT", "ACCOUNT_DEFAULT"):
            return CapabilityTruth.ACCOUNT_DEFAULT
        return CapabilityTruth.UNSUPPORTED

    def gate(self, job: PreparedJob) -> AuditGate:
        profile_id = str(job.payload_config.get("profile_id") or "")
        reason = ""
        if not profile_id:
            reason = "audit profile missing from prepared payload"
        elif not _profile_exists(profile_id):
            reason = f"audit profile {profile_id} no longer exists"
        binding = self.account_binding(job)
        if not reason and binding is AccountBinding.ACCOUNT_UNBOUND:
            reason = (f"Audit waiting: browser account binding for "
                      f"{job.account_id} is not verified")
        model_truth = CapabilityTruth.VERIFIED
        effort_truth = CapabilityTruth.VERIFIED
        if not reason:
            model_truth = self._capability_truth(
                job.model, str(job.payload_config.get("model_policy") or ""))
            if model_truth is CapabilityTruth.UNSUPPORTED:
                reason = (f"Audit waiting: requested browser model {job.model} "
                          "cannot be verified")
            else:
                effort_truth = self._capability_truth(
                    job.effort, str(job.payload_config.get("effort_policy") or ""))
                if effort_truth is CapabilityTruth.UNSUPPORTED:
                    reason = (f"Audit waiting: requested effort {job.effort} "
                              "cannot be verified")
        return AuditGate(
            ok=not reason, reason=reason, profile_id=profile_id,
            account_binding=binding, model_truth=model_truth,
            effort_truth=effort_truth,
        )

    def dry_run(self, job: PreparedJob) -> dict:
        """Validate without any side effect: no packing, no dispatch, no worker."""
        gate = self.gate(job)
        project = None
        project_reason = ""
        try:
            project = self.coordinator.projects.get_project(job.project_id)
        except Exception:
            project = None
        if project is None:
            project_reason = "project is missing or unregistered"
        conflict_reason = ""
        if not project_reason and self.unrelated_active_audit(job.project_id):
            conflict_reason = "project has an unrelated active audit"
        return {
            "ok": bool(gate.ok and not project_reason and not conflict_reason),
            "project_id": job.project_id,
            "project_present": project is not None,
            "project_reason": project_reason,
            "audit_profile": gate.profile_id,
            "account_id": job.account_id,
            "account_binding": gate.account_binding.value,
            "model": job.model,
            "model_truth": gate.model_truth.value,
            "effort": job.effort,
            "effort_truth": gate.effort_truth.value,
            "archive_freshness": "canonical pipeline ensures current archive at execution",
            "conflict_reason": conflict_reason,
            "reason": gate.reason or project_reason or conflict_reason,
            "execution_policy": "one audit per prepared execution via source_execution_id",
        }

    # ------------------------------------------------------------ conflicts
    def unrelated_active_audit(self, project_id: str) -> bool:
        """True when a source-less (manual) audit already owns the project."""
        try:
            active = self.coordinator.bridge.active_browser_job(str(project_id))
        except Exception:
            return False
        if not active or str(active.get("dispatch_id") or "") == "":
            return False
        intent = self.coordinator.intents.find_for_dispatch(
            str(active.get("dispatch_id") or ""))
        # A source-keyed intent belongs to a prepared execution; the reconciler
        # adopts it. Anything else is an operator/manual run this job must not
        # steal or cancel merely because its timer fired.
        return not (intent and intent.get("source_execution_id"))

    # ---------------------------------------------------------------- start
    def start(self, job: PreparedJob, execution_id: str):
        """Enter the canonical pipeline exactly once for this execution."""
        return self.coordinator.start(
            job.project_id,
            str(job.payload_config.get("profile_id") or ""),
            provision=True,
            source_execution_id=execution_id,
        )

    # ----------------------------------------------------------------- poll
    def poll(self, project_id: str, execution_id: str) -> AuditProgress:
        """Derive the audit's state from canonical stores; never a mirror."""
        intent = self.coordinator.intents.find_for_source(execution_id)
        if intent is None:
            return AuditProgress(AuditProgressState.ABSENT)
        intent_id = str(intent.get("intent_id") or "")
        dispatch_id = str(intent.get("dispatch_id") or "")
        snapshot = None
        try:
            for run in self.coordinator.refresh_runs([str(project_id)]):
                if str(run.intent_id) == intent_id:
                    snapshot = run
                    break
        except Exception as exc:
            logger.warning("prepared audit reconcile failed: %s", type(exc).__name__)
        if snapshot is None:
            snapshot = self._from_intent(intent, dispatch_id)
        return self._classify(snapshot, dispatch_id)

    @staticmethod
    def _from_intent(intent: dict, dispatch_id: str):
        """Fallback view when the Bridge is unreachable: durable intent only."""
        from audapack.services.audit_run_service import AuditRunSnapshot

        raw = str(intent.get("status") or "PREPARING")
        state = {
            "READY": "READY", "CANCELLED": "CANCELLED", "FAILED": "FAILED",
            "BLOCKED": "BLOCKED_PRE_START", "RECOVERY_NEEDED": "RECOVERY",
        }.get(raw, "PREPARING")
        return AuditRunSnapshot(
            project_id=str(intent.get("project_id") or ""),
            project_name=str(intent.get("project_name") or ""),
            operator_state=state,
            summary=raw, intent_id=str(intent.get("intent_id") or ""),
            dispatch_id=dispatch_id, error=str(intent.get("error") or ""),
        )

    @staticmethod
    def _classify(snapshot, dispatch_id: str) -> AuditProgress:
        state = str(snapshot.operator_state or "")
        if state == "READY":
            return AuditProgress(AuditProgressState.DONE, str(snapshot.intent_id),
                                 dispatch_id, "audit accepted terminal success")
        if state in {"WAITING", "ATTACHING", "STARTING", "RETRYING",
                     "PREPARING", "INTERRUPTED"}:
            target = AuditProgressState.DISPATCHED if dispatch_id else AuditProgressState.PENDING
            return AuditProgress(target, str(snapshot.intent_id), dispatch_id, state)
        if state in {"AUDITING", "SAVING"}:
            return AuditProgress(AuditProgressState.RUNNING, str(snapshot.intent_id),
                                 dispatch_id, state)
        if state == "BLOCKED_POST_START" or state == "RECOVERY":
            return AuditProgress(AuditProgressState.FAILED_POST_START,
                                 str(snapshot.intent_id), dispatch_id, state)
        if state == "BLOCKED_PRE_START":
            return AuditProgress(AuditProgressState.FAILED_PRE_START,
                                 str(snapshot.intent_id), dispatch_id,
                                 str(snapshot.error or "blocked pre-start"))
        if state in {"FAILED", "CANCELLED", "SUPERSEDED"}:
            post_start = bool(
                getattr(snapshot, "campaign_run_id", "")
                or str(getattr(snapshot, "recovery", "") or "") in {
                    "START_PREPARED", "STARTED", "AUDITING", "FINALIZING"}
            )
            kind = (AuditProgressState.FAILED_POST_START if post_start
                    else AuditProgressState.FAILED_PRE_START)
            return AuditProgress(kind, str(snapshot.intent_id), dispatch_id, state)
        return AuditProgress(AuditProgressState.PENDING, str(snapshot.intent_id),
                             dispatch_id, state or "unknown")


def build_headless_audit_runtime(config: AppConfig | None = None,
                                 component_manager=None) -> PreparedAuditRuntime:
    """Non-Qt construction path for the full AuditRunCoordinator service set.

    MainWindow builds the same coordinator from the same services; the
    invariant is that GUI and scheduler call one canonical behavior, not two
    copies of the audit logic.
    """
    from audapack.components.manager import ComponentManager
    from audapack.services.audit_run_service import AuditRunCoordinator
    from audapack.services.audit_service import AuditService
    from audapack.services.bridge_service import BridgeService
    from audapack.services.packing_service import PackingService
    from audapack.services.project_service import ProjectService

    effective_config = config or getattr(component_manager, "config", None)
    projects = ProjectService(effective_config)
    coordinator = AuditRunCoordinator(
        projects,
        PackingService(effective_config),
        BridgeService(effective_config),
        AuditService(effective_config),
        component_manager=component_manager if component_manager is not None
        else ComponentManager(effective_config),
    )
    return PreparedAuditRuntime(coordinator, effective_config)


#: Mapping from a polled audit state to the prepared execution's terminal or
#: transitional classification. Kept here so the worker stays a thin caller.
def audit_outcome(progress: AuditProgress) -> tuple[str, str]:
    """(prepared classification, human detail) for a polled progress."""
    if progress.state is AuditProgressState.DONE:
        return "DONE", progress.detail
    if progress.state is AuditProgressState.FAILED_PRE_START:
        return "FAILED_RETRYABLE", progress.detail or "audit failed before start"
    if progress.state is AuditProgressState.FAILED_POST_START:
        return "RECOVERY_REQUIRED", progress.detail or "audit failed after start"
    if progress.state is AuditProgressState.CANCELLED:
        return "CANCELLED", progress.detail
    return "ACTIVE", progress.detail
