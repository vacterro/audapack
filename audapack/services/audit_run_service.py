"""Operator-facing audit run orchestration.

This module is deliberately independent from Qt.  It joins the durable browser
dispatch record with the audit index and exposes one conservative state machine
to every UI surface.  Transport COMPLETE is not presented as READY until the
final handoff exists and its identity and digest are proven.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from audapack import saipen_inbox
from audapack.config import cross_process_lock, get_state_dir
from audapack.models import AuditSnapshot

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
    #: What the agent did with what we delivered, read from SAIPEN's Audit
    #: Inbox binding. READY means the station is finished; it says nothing
    #: about whether anyone read the result, and that is the question the
    #: operator actually has before pressing START AUDIT again.
    agent_state: str = saipen_inbox.NO_INBOX
    agent_summary: str = ""
    agent_guidance: str = ""
    agent_residue: int = 0

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

    def begin(self, project_id: str, project_name: str, profile_id: str) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._read_unlocked()
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
                "status": "PREPARING",
                "dispatch_id": "",
                "campaign_run_id": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": 0.0,
                "owner_pid": os.getpid(),
            }
            intent["request_id"] = intent["intent_id"]
            intent["phase"] = intent["status"]
            intent["archive_path"] = ""
            doc["intents"].append(intent)
            doc["intents"] = doc["intents"][-self.history_bound:]
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return dict(intent), True

    def update(self, intent_id: str, **changes: Any) -> dict[str, Any]:
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._read_unlocked()
            target = next((item for item in doc["intents"] if item.get("intent_id") == intent_id), None)
            if target is None:
                raise KeyError(f"Unknown audit start intent: {intent_id}")
            normalized = {key: value for key, value in changes.items() if key not in {"intent_id", "project_id", "created_at"}}
            if "status" in normalized:
                normalized["phase"] = normalized["status"]
            if all(target.get(key) == value for key, value in normalized.items()):
                return dict(target)
            target.update(normalized)
            target["updated_at"] = now
            if str(target.get("status")) in {"READY", "FAILED", "CANCELLED"} and not target.get("completed_at"):
                target["completed_at"] = now
            doc["intents"] = doc["intents"][-self.history_bound:]
            doc["updated_at"] = now
            _atomic_write_json(self.path, doc)
            return dict(target)

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
        if not self.path.exists():
            return {"schema_version": 1, "generation": 1, "slots": {}}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"schema_version": 1, "generation": 1, "slots": {}}
        if not isinstance(doc, dict) or doc.get("schema_version") != 1:
            return {"schema_version": 1, "generation": 1, "slots": {}}
        doc.setdefault("generation", 1)
        doc.setdefault("slots", {})
        return doc

    def ensure_capacity(self, dispatch: dict[str, Any], demand: int) -> dict[str, Any]:
        desired = min(MAX_AUDIT_LANES, max(1, int(demand or 0)))
        workers = dispatch.get("workers", []) if isinstance(dispatch, dict) else []
        now = time.time()
        with cross_process_lock(self.lock_path):
            doc = self._load()
            generation = max(1, int(doc.get("generation", 1)))
            registered = {
                int(worker.get("managed_slot"))
                for worker in workers
                if int(worker.get("managed_generation", 0) or 0) == generation
                and str(worker.get("managed_slot", "")).isdigit()
                and 1 <= int(worker.get("managed_slot")) <= MAX_AUDIT_LANES
            }
            # A window that was launched but has not registered yet still
            # occupies its slot and one lane.
            pending = {
                int(slot_id)
                for slot_id, slot_state in doc["slots"].items()
                if str(slot_id).isdigit()
                and int(slot_id) not in registered
                and str(slot_state.get("state")) == "LAUNCHING"
                and now - float(slot_state.get("launched_at", 0.0) or 0.0) < WORKER_LAUNCH_BOOT_GRACE_SECONDS
            }
            launched: list[dict[str, Any]] = []
            for slot in range(1, desired + 1):
                if slot in registered:
                    doc["slots"][str(slot)] = {
                        "state": "HEARTBEAT",
                        "launch_attempts": 0,
                        "last_seen_at": max(
                            float(worker.get("last_seen_at", 0.0) or 0.0)
                            for worker in workers
                            if int(worker.get("managed_slot", 0) or 0) == slot
                            and int(worker.get("managed_generation", 0) or 0) == generation
                        ),
                        "cooldown_until": 0.0,
                    }
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
            doc = self._load()
            generation = max(1, int(doc.get("generation", 1)))
            live = any(
                int(worker.get("managed_slot", 0) or 0) == slot
                and int(worker.get("managed_generation", 0) or 0) == generation
                for worker in workers
            )
            if live:
                return {"slot": slot, "generation": generation, "launched": False, "message": "slot already has a live worker"}
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

    def _reset_slot(self, slot: int) -> None:
        """Forget a slot's launch/cooldown accounting so it can be relaunched.

        Called by the explicit operator relaunch path. Clearing the tracked
        entry resets launch_attempts and the cooldown clock, so a slot whose
        window was closed (and whose tracked state was stuck in LAUNCHING or
        LAUNCH_FAILED) is treated as fresh by the next ``ensure_capacity``.
        """
        slot = max(1, min(MAX_AUDIT_LANES, int(slot)))
        with cross_process_lock(self.lock_path):
            doc = self._load()
            doc["slots"].pop(str(slot), None)
            doc["updated_at"] = time.time()
            _atomic_write_json(self.path, doc)


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


def _actions_for(operator_state: str) -> tuple[str, ...]:
    if operator_state in {"PREPARING", "WAITING", "RETRYING", "ATTACHING"}:
        return ("CANCEL", "DETAILS")
    if operator_state in {"STARTING", "AUDITING", "SAVING"}:
        return ("DETAILS",)
    if operator_state in {"FAILED", "CANCELLED"}:
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
        wanted = max(1, int(lanes or 0))
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

    def start(self, project_id: str, profile_id: str = "quick3", provision: bool = True) -> AuditStartResult:
        project = self.projects.get_project(str(project_id))
        if project is None or not project.enabled or not project.source_path:
            return AuditStartResult(False, str(project_id), message="Project is missing, disabled, or has no source path")
        self._release_pre_start_block(project.id)
        intent, created = self.intents.begin(project.id, project.display_name, profile_id)
        if not created:
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
            self.intents.update(str(intent["intent_id"]), status="PREPARING", error="", owner_pid=os.getpid())

        intent_id = str(intent["intent_id"])
        try:
            health = self._healthy_bridge_status()
            if provision and self.workers is not None:
                dispatch_status = (health.get("browser") or {}) if isinstance(health, dict) else {}
                demand = (
                    int(dispatch_status.get("queued_jobs", 0) or 0)
                    + int(dispatch_status.get("active_jobs", 0) or 0)
                    + 1
                )
                self.workers.ensure_capacity(dispatch_status, demand)
            self.intents.update(intent_id, status="PACKING")
            packed = self.packing.ensure_fresh_archive(project.id)
            if not packed.success or not packed.output_path:
                raise RuntimeError(packed.error_message or "Packing failed")
            self.intents.update(intent_id, status="SUBMITTING", archive_path=str(Path(packed.output_path).resolve()))
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
                self.intents.update(intent_id, status="QUEUED", dispatch_id=dispatch_id)
                return AuditStartResult(
                    True, project.id, intent_id, dispatch_id,
                    str(adopted.get("state") or "QUEUED"),
                    "Audit queued (adopted after a lost submit response)",
                )
            self.intents.update(intent_id, status="QUEUED", dispatch_id=dispatch_id)
            return AuditStartResult(True, project.id, intent_id, dispatch_id, "QUEUED", "Audit queued")
        except Exception as exc:
            self.intents.update(intent_id, status="FAILED", error=str(exc)[:500])
            return AuditStartResult(False, project.id, intent_id, state="FAILED", message=str(exc))

    RESET_SETTLED_STATES = frozenset({"READY", "FAILED", "CANCELLED"})

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
            (unblocked if forced.ok else failed).append(label)

        return {
            "cancelled": cancelled,
            "unblocked": unblocked,
            "failed": failed,
            "total": len(cancelled) + len(unblocked) + len(failed),
        }

    def start_batch(self, project_ids: Iterable[str], profile_id: str = "quick3") -> list[AuditStartResult]:
        unique = list(dict.fromkeys(str(value) for value in project_ids if str(value)))[:MAX_AUDIT_LANES]
        if not unique:
            return []
        # Windows first, for the whole batch, before the first archive is
        # packed: browser boot and packing then overlap instead of queueing
        # behind each other. A provisioning failure is never fatal here -- the
        # Bridge supervisor keeps provisioning, and a queued job with no window
        # yet is a wait, not a loss.
        provisioned = False
        try:
            self.provision_capacity(len(unique))
            provisioned = True
        except Exception:
            provisioned = False
        return [self.start(project_id, profile_id, provision=not provisioned) for project_id in unique]

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
        """Force a stuck BLOCKED run terminal so START AUDIT works again.

        Cancel refuses a post-start BLOCKED dispatch because CANCELLED asserts
        no Core was sent. Abandon is the honest terminal: the lane frees up,
        the record stays FAILED with operator_abandoned, and no second START is
        issued automatically.
        """
        response = self.bridge.abandon_browser_job(str(dispatch_id), reason)
        intent = self.intents.find_for_dispatch(str(dispatch_id))
        project_id = str((intent or {}).get("project_id") or "")
        intent_id = str((intent or {}).get("intent_id") or "")
        if response.get("ok"):
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
        total = int(getattr(audit, "total_waves", 3) or 3) if matching and audit else 3
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
        else:
            operator = "READY" if ready else ("SAVING" if state == "COMPLETE" else mapping.get(state, "PREPARING"))
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
        )

    def _intent_snapshot(self, intent: dict[str, Any]) -> AuditRunSnapshot:
        raw = str(intent.get("status") or "PREPARING")
        interrupted = raw in {"PREPARING", "PACKING", "SUBMITTING"} and int(intent.get("owner_pid", 0) or 0) not in {0, os.getpid()}
        operator = "INTERRUPTED" if interrupted else (
            "FAILED" if raw == "FAILED" else ("RECOVERY" if raw == "RECOVERY_NEEDED" else "PREPARING")
        )
        error = str(intent.get("error") or "")
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

    def _stamp_agent_state(self, snapshot: AuditRunSnapshot) -> AuditRunSnapshot:
        """Answer 'has the agent read this yet' from the project's own inbox.

        Best effort by design: a project with no SAIPEN, no mirror or an
        unreadable tree simply reads NO_INBOX. A dashboard field must never be
        able to fail a refresh.
        """
        try:
            project = self.projects.get_project(str(snapshot.project_id))
            root = str(getattr(project, "source_path", "") or "") if project else ""
            if not root:
                return snapshot
            state = saipen_inbox.read_inbox_cached(root)
            snapshot.agent_state = state.verdict
            snapshot.agent_summary = state.summary()
            snapshot.agent_guidance = state.guidance
            snapshot.agent_residue = len(state.residue)
        except Exception:
            pass
        return snapshot

    def refresh_runs(self, project_ids: Optional[Iterable[str]] = None) -> list[AuditRunSnapshot]:
        selected = {str(value) for value in project_ids} if project_ids is not None else None
        jobs_response = self.bridge.browser_jobs()
        if not jobs_response.get("ok"):
            return [self._intent_snapshot(item) for item in reversed(self.intents.list()) if selected is None or str(item.get("project_id")) in selected]
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
        snapshots: list[AuditRunSnapshot] = []
        jobs = sorted(jobs_response.get("jobs", []), key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
        for job in jobs:
            project_id = str(job.get("project_id") or "")
            if selected is not None and project_id not in selected:
                continue
            if project_id not in audit_cache:
                audit_cache[project_id] = self.audits.refresh_project(project_id)
            intent = by_dispatch.get(str(job.get("dispatch_id") or ""))
            snapshot = self._snapshot(job, intent, audit_cache[project_id], labels, bridge_context)
            self._stamp_agent_state(snapshot)
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
                self.intents.update(
                    str(intent["intent_id"]), status=intent_state,
                    campaign_run_id=snapshot.campaign_run_id,
                    error=snapshot.error,
                )
        for intent in reversed(intents):
            if str(intent.get("intent_id")) in seen_intents:
                continue
            if selected is not None and str(intent.get("project_id")) not in selected:
                continue
            snapshots.append(self._stamp_agent_state(self._intent_snapshot(intent)))
        return snapshots[:RUN_HISTORY_BOUND]

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
            "handoff_filename": Path(snapshot.handoff_path).name if snapshot.handoff_path else "",
            "handoff_sha256": snapshot.handoff_sha256,
            "error": snapshot.error[:500],
            "recovery": snapshot.recovery[:200],
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "completed_at": snapshot.completed_at,
        }
        return json.dumps(doc, ensure_ascii=False, indent=2)
