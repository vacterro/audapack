"""AUDAPACK Loopback HTTP Bridge server implementation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from audapack import __version__, archive_receipt, handoff_drop
from audapack.bridge.browser_dispatch import (
    SUPPORTED_BROWSER_WIDGET_VERSION,
    BrowserDispatcher,
    _get_required_widget_build,
)
from audapack.bridge.browser_dispatch import (
    DispatchError as BrowserDispatchError,
)
from audapack.bridge.lifecycle import (
    INSTANCE_NONCE,
    SUPPORTED_API_VERSIONS,
    remove_pid,
    write_pid,
)
from audapack.bridge.state import (
    GenerationPersistenceError,
    RunStateCorruptionError,
    RunStatePersistenceError,
    get_run_state,
    run_transaction,
    save_run_state,
)
from audapack.bridge.storage import (
    InvalidProjectPathError,
    atomic_write,
    canonical_audit_bytes,
    capture_file_snapshots,
    classify_canonical_file,
    classify_canonical_file_by_sha,
    expected_history_dir,
    expected_wave_representation_paths,
    generate_canonical_campaign,
    parse_wave,
    read_canonical_file,
    resolve_project_audit_dir,
    resolve_project_audit_dir_readonly,
    restore_file_snapshots,
)
from audapack.campaign import (
    ARTIFACT_KIND_DIRECT_HANDOFF,
    ARTIFACT_KIND_QUICK3_COMBINED,
    STATUS_CAMPAIGN_COMPLETE,
    STATUS_CAMPAIGN_READY_FOR_WAVE,
    campaign_transaction_lock,
    get_canonical_manifest_hash,
    get_profile,
    load_profiles,
    save_live_campaign_index,
)
from audapack.components.widget import get_bundled_widget_path
from audapack.config import (
    AppConfig,
    app_dir,
    get_token_file_path,
    get_user_runtime_dir,
    legacy_token_acceptance_revoked,
    load_config,
    normalize_bridge_host,
)
from audapack.inaudit_capture import InauditCaptureError, normalize_capture_text, store_for_config
from audapack.packing import find_archive_for_project, project_for_archive_filename, resolve_output_dir
from audapack.procutil import run_hidden
from audapack.projects import ProjectRegistry, RegistrySaveError

logger = logging.getLogger("audapack.bridge")

#: W2-001: bounded extra grace the Bridge gives the prepared worker to reach
#: quiescence before it refuses to close the socket or drop its PID file.
PREPARED_QUIESCENCE_WAIT_SECONDS = 30.0

# Canonical API contract version. Advertised in /health; supports v2 and v3.
# SUPPORTED_API_VERSIONS is the single client+server set (W2-004).
BRIDGE_API_VERSION = 3

# Canonical browser-worker protocol advertised in /health; sourced from browser_dispatch.
BROWSER_WORKER_PROTOCOL_VERSION = SUPPORTED_BROWSER_WIDGET_VERSION

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


_BUILD_IDENTITY: Optional[tuple[str, str]] = None
_BUILD_IDENTITY_LOCK = threading.Lock()


def _get_build_identity() -> tuple[str, str]:
    """Return (build_id, source_revision) from git or fallback.

    Computed once per process. It is the identity of the RUNNING build, which
    cannot change without restarting this daemon, and it was being recomputed
    on every /health and /v1/status request: two `git` spawns each, four per
    poll cycle, against a GUI that polls every 4s while an audit is unsettled.
    On Windows that is a console window flashing on the operator's screen
    roughly twice a second for the length of every audit -- the "endless
    windows" complaint -- plus a pointless process storm underneath it.
    """
    global _BUILD_IDENTITY
    if _BUILD_IDENTITY is not None:
        return _BUILD_IDENTITY
    with _BUILD_IDENTITY_LOCK:
        if _BUILD_IDENTITY is None:
            _BUILD_IDENTITY = _read_build_identity()
        return _BUILD_IDENTITY


def _read_build_identity() -> tuple[str, str]:
    try:
        rev = run_hidden(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=_REPO_ROOT,
        ).stdout.strip()
        dirty = run_hidden(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
            cwd=_REPO_ROOT,
        ).stdout.strip()
        if rev:
            source_revision = rev
            build_id = f"{rev}-dirty" if dirty else rev
        else:
            build_id = "dev"
            source_revision = ""
    except Exception:
        build_id = "dev"
        source_revision = ""
    return build_id, source_revision


_WIDGET_BUNDLE_CACHE: dict[tuple[str, int, int], tuple[str, str]] = {}


def _get_widget_bundle_info() -> tuple[str, str]:
    """Return (widget_bundle_version, widget_bundle_sha256_prefix16).

    Keyed on the bundled file's (path, mtime, size): re-hashing 800 KB on every
    /health and /v1/status request bought nothing, and those endpoints are
    polled every few seconds while an audit runs.
    """
    path = get_bundled_widget_path()
    widget_version = ""
    sha256 = ""
    if path and path.exists():
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        if key is not None and key in _WIDGET_BUNDLE_CACHE:
            return _WIDGET_BUNDLE_CACHE[key]
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()[:16]
        for line in data.decode("utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("// @version"):
                parts = stripped.split()
                if len(parts) >= 3:
                    widget_version = parts[2]
                break
        if key is not None:
            _WIDGET_BUNDLE_CACHE.clear()
            _WIDGET_BUNDLE_CACHE[key] = (widget_version, sha256)
    return widget_version, sha256

_WIDGET_UPDATE_DIRECTIVE_RE = re.compile(
    rb"^(//\s*@(?:updateURL|downloadURL)\s+)\S+", re.MULTILINE
)


def _widget_endpoint_authority(host_header: Optional[str], config: AppConfig) -> str:
    """Where this Bridge is actually reachable, for the update directives.

    The Host header is the authority the client used to get here, which is
    exactly the one its update check should keep using. Loopback binding is
    already enforced for every other route, so a Host is only trusted when it
    resolves to a loopback name; anything else falls back to the configured
    host/port rather than baking a foreign authority into the script.
    """
    host = str(host_header or "").strip()
    if host and ":" in host:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    if name.strip("[]").lower() in {"127.0.0.1", "localhost", "::1"} and host:
        return host
    configured_host = normalize_bridge_host(config.bridge.host) or "127.0.0.1"
    return f"{configured_host}:{int(config.bridge.port)}"


def _widget_source_for_endpoint(content: bytes, host_header: Optional[str], config: AppConfig) -> bytes:
    """Serve the userscript with its update endpoint pointing at THIS Bridge.

    CORE-001 (audit/2.md): the bundled `@updateURL`/`@downloadURL` hardcode
    127.0.0.1:17843, while `BridgeConfig.port` is operator-configurable and
    Settings exposes it as an editable 1..65535 field. An operator who moved the
    port could install the widget through the configured URL and then silently
    lose auto-update forever, because Tampermonkey persists the endpoint from the
    metadata block and kept checking 17843 -- reintroducing the manual-install
    outage those headers were added to remove.

    Rewritten at serve time rather than at build time: the file on disk stays one
    canonical artifact, and the same bundle is correct on every port.
    """
    authority = _widget_endpoint_authority(host_header, config)
    replacement = rb"\1http://" + authority.encode("ascii", "ignore") + b"/widget.user.js"
    return _WIDGET_UPDATE_DIRECTIVE_RE.sub(replacement, content)


def _live_bridge_token() -> str:
    """The current canonical Bridge token, read straight from its file.

    PERF-002 (audit/2.md): `check_auth()` called `load_config()` on EVERY
    authenticated request just to obtain a second token candidate -- and
    `load_config()` is not a credential read: it parses the whole config,
    migrates it, calls `ensure_token()`, then walks every registered project
    through source-path healing, which stats project paths on disk. Measured:
    0.327 ms/load at 12 projects, 0.972 ms at 60, 8.124 ms at 300 -- so the cost
    of comparing a local bearer token grew with registry size and inherited
    project-storage latency, on every worker heartbeat and every 4-second UI
    cycle.

    The token file is a few dozen bytes and is NOT cached: a first attempt keyed
    it on (mtime_ns, size) like the widget bundle, and a rotation to a
    same-length value inside one filesystem timestamp tick was then invisible --
    which is a stale credential, the one thing this must never be. Dropping
    `load_config()` is the whole win; re-reading 40 bytes is not a cost.
    """
    try:
        token_file = get_token_file_path()
    except Exception:
        return ""
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# Global callback for notifying UI of new audits or auto-registered projects
_ON_AUDIT_WRITTEN: Optional[Callable[[str, str], None]] = None

#: Retained INAUDIT stores, keyed on the custom base dir (PERF-002). One store
#: per Bridge lifetime per runtime root: crash recovery runs once at first use,
#: not once per endpoint call.
_INAUDIT_STORES: dict[str, Any] = {}
_INAUDIT_STORE_LOCK = threading.Lock()


def set_audit_written_callback(cb: Optional[Callable[[str, str], None]]):
    global _ON_AUDIT_WRITTEN
    _ON_AUDIT_WRITTEN = cb


def _rollback_error(snapshots, code: str, message: str) -> dict:
    """Roll back a failed commit and describe what the rollback left behind.

    W2-001 (audit/1.md): `restore_file_snapshots()` reports which paths it could
    NOT restore, and every caller here discarded that list. A failed campaign
    index therefore answered an ordinary retriable `campaign_index_failed` --
    "the transaction failed, nothing was published" -- while a canonical
    completion artifact it could not unlink was still sitting on disk for the
    next reader to treat as a finished audit. Same convention as
    `ingest.py::_ingest_failure`: name the residue, and stop calling an
    incomplete rollback a clean one.
    """
    rollback_errors = restore_file_snapshots(snapshots)
    if not rollback_errors:
        return {"ok": False, "error": {"code": code, "message": message, "retriable": True}}
    residue = "; ".join(rollback_errors)
    logger.error("rollback incomplete after %s: %s", code, residue)
    return {
        "ok": False,
        "error": {
            "code": "rollback_incomplete",
            "message": f"{message}; rollback incomplete, these paths may hold uncommitted state: {residue}",
            "retriable": False,
            "rollback_errors": rollback_errors,
        },
    }


def _write_final_artifacts(prof, synth_result, target_dir, history_dir, dt_str, state, resolved_name):
    """Synthesizes and durably writes the canonical campaign final artifacts.

    Raises on any write failure so the caller can roll back. History side is
    written before the canonical latest so a partial failure never leaves the
    authoritative file mutated without durable state agreeing.

    T-185 B3: the exact physical bytes of every canonical latest representation
    are recorded in ``state["final_artifact_digests"]`` so a future content-less
    verify probe can classify ALL_3 against exact bytes instead of guessing
    (the synthesizer embeds a generation timestamp, so re-synthesis can never
    reproduce the original bytes).
    """
    digests = dict(state.get("final_artifact_digests") or {})

    def _record(path, content):
        digests[str(path)] = hashlib.sha256(canonical_audit_bytes(content)).hexdigest()

    kind = prof.canonical_artifact_kind
    if kind == ARTIFACT_KIND_QUICK3_COMBINED:
        all3_content = synth_result.get("all3", "")
        all3_latest = target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
        all3_hist = history_dir / f"{resolved_name}__00_AUDIT_ALL_3__{dt_str}.md"
        atomic_write(all3_hist, all3_content)
        atomic_write(all3_latest, all3_content)
        _record(all3_latest, all3_content)
        state["all3_complete"] = True
        state["all3_path"] = str(all3_latest)
    elif kind == ARTIFACT_KIND_DIRECT_HANDOFF:
        # A single-wave profile has nothing to synthesise: the validated
        # terminal wave IS the handoff. Writing SUPER_AUDIT_ALL/FINAL/INDEX for
        # it would invent nine waves that never ran.
        handoff_content = synth_result.get("handoff", "")
        basename = prof.canonical_artifact_basename
        handoff_latest = target_dir / f"{resolved_name}{basename}.md"
        handoff_hist = history_dir / f"{resolved_name}{basename}__{dt_str}.md"
        atomic_write(handoff_hist, handoff_content)
        atomic_write(handoff_latest, handoff_content)
        _record(handoff_latest, handoff_content)
        state["campaign_complete"] = True
        state["final_handoff_path"] = str(handoff_latest)
        state["canonical_campaign_path"] = str(handoff_latest)
    else:
        super_all = synth_result.get("super_all", "")
        super_final = synth_result.get("super_final", "")
        super_index = synth_result.get("super_index", "")
        all_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md"
        final_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md"
        index_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json"
        all_hist = history_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL__{dt_str}.md"
        final_hist = history_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL__{dt_str}.md"
        index_hist = history_dir / "manifest.json"
        atomic_write(all_hist, super_all)
        atomic_write(all_latest, super_all)
        atomic_write(final_hist, super_final)
        atomic_write(final_latest, super_final)
        atomic_write(index_hist, super_index)
        atomic_write(index_latest, super_index)
        _record(all_latest, super_all)
        _record(final_latest, super_final)
        _record(index_latest, super_index)
        state["campaign_complete"] = True
        state["final_handoff_path"] = str(final_latest)
        state["canonical_campaign_path"] = str(all_latest)

    state["final_artifact_digests"] = digests


def _final_artifact_paths(prof, target_dir: Path, history_dir: Path, dt_str: str, resolved_name: str) -> list[Path]:
    """Every path `_write_final_artifacts` can write, for this profile.

    W2-001 (audit/1.md): the snapshot list was an `if quick3 else SUPER_AUDIT`
    branch written before the compress profile existed, so a compress campaign's
    canonical `__00_COMPRESS_AUDIT.md` was never snapshotted -- and therefore
    could not be rolled back. Proved by the failed-index regression: the dispatch
    stayed non-terminal and the uncommitted handoff sat on disk anyway. Derived
    from the same `canonical_artifact_kind` the writer switches on, so a new
    profile cannot add an artifact the rollback does not know about.
    """
    kind = prof.canonical_artifact_kind
    if kind == ARTIFACT_KIND_QUICK3_COMBINED:
        return [
            target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md",
            history_dir / f"{resolved_name}__00_AUDIT_ALL_3__{dt_str}.md",
        ]
    if kind == ARTIFACT_KIND_DIRECT_HANDOFF:
        basename = prof.canonical_artifact_basename
        return [
            target_dir / f"{resolved_name}{basename}.md",
            history_dir / f"{resolved_name}{basename}__{dt_str}.md",
        ]
    return [
        target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md",
        target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md",
        target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json",
        history_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL__{dt_str}.md",
        history_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL__{dt_str}.md",
        history_dir / "manifest.json",
    ]


def _get_final_handoff_path(prof, state) -> Optional[Path]:
    if prof.canonical_artifact_kind == ARTIFACT_KIND_QUICK3_COMBINED:
        return Path(state["all3_path"]) if state.get("all3_path") else None
    return Path(state["final_handoff_path"]) if state.get("final_handoff_path") else None


# T-185 P0-2: the required physical durability set. ONE derivation owner for
# what "this campaign is durably represented on disk" means, shared by
# verify_only, materialization (normal, partial and replay) and the duplicate
# acknowledgement -- four slightly different lists is exactly the defect class
# audit/10 CORE-001/W2-001 names.
def _campaign_requires_final_artifacts(prof, state) -> bool:
    """True when every profile-required wave is complete in the run state.

    B1: an incomplete 1/3 or 2/3 run has NO required final artifact -- Core
    still saves truthfully at 1/3 and Second at 2/3. B2: a campaign-complete
    run must also prove its final handoff.
    """
    return all(
        state.get("waves", {}).get(w.id, {}).get("complete")
        for w in prof.waves if w.required
    )


def _expected_final_artifacts(prof, state, target_dir, resolved_name):
    """The REQUIRED final-artifact durability set for a run (T-185 B2/B3).

    One derivation owner shared by verify_only, receipt-replay preflight,
    materialization postcondition and the duplicate acknowledgement. Each entry
    is {"path", "digest"}: the canonical latest representation this profile's
    finalization promises, and the exact physical-byte digest recorded when
    those bytes were written (``state["final_artifact_digests"]``). A missing
    digest means the bytes cannot be proven at all -- callers classify that
    fail-closed (UNREADABLE), never durable. Returns [] for an incomplete
    campaign (B1: no ALL_3 requirement before campaign readiness).
    """
    if not _campaign_requires_final_artifacts(prof, state):
        return []
    digests = state.get("final_artifact_digests") or {}
    kind = prof.canonical_artifact_kind
    entries: list[dict] = []
    if kind == ARTIFACT_KIND_QUICK3_COMBINED:
        latest = Path(state["all3_path"]) if state.get("all3_path") else (
            target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
        )
        entries.append({"path": latest, "digest": str(digests.get(str(latest), ""))})
    elif kind == ARTIFACT_KIND_DIRECT_HANDOFF:
        latest = Path(state["final_handoff_path"]) if state.get("final_handoff_path") else (
            target_dir / f"{resolved_name}{prof.canonical_artifact_basename}.md"
        )
        entries.append({"path": latest, "digest": str(digests.get(str(latest), ""))})
    else:
        for key, suffix in (
            ("canonical_campaign_path", "__00_SUPER_AUDIT_ALL.md"),
            ("final_handoff_path", "__00_SUPER_AUDIT_FINAL.md"),
            (None, "__00_SUPER_AUDIT_INDEX.json"),
        ):
            if key and state.get(key):
                latest = Path(state[key])
            else:
                latest = target_dir / f"{resolved_name}{suffix}"
            entries.append({"path": latest, "digest": str(digests.get(str(latest), ""))})
    return entries


def classify_required_artifacts(entries, target_dir) -> list[dict]:
    """Classify every required final artifact like wave representations (B3).

    Verdicts: INTACT / MISSING / CONTENT_MISMATCH / WRONG_TYPE / UNREADABLE.
    A recorded path outside the campaign directory is OUTSIDE_CANONICAL (A3:
    unsafe -- fail closed, never overwrite arbitrary filesystem objects). A
    file whose bytes cannot be proven against any recorded digest is
    UNREADABLE (the classify_canonical_file_by_sha convention: an unprovable
    file is never durable).
    """
    classified = []
    target_root = Path(target_dir).resolve()
    for entry in entries:
        path = Path(entry["path"])
        digest = str(entry.get("digest") or "")
        try:
            contained = path.resolve().parent == target_root or target_root in path.resolve().parents
        except OSError:
            contained = False
        if not contained:
            classified.append({"path": str(path), "verdict": "OUTSIDE_CANONICAL", "digest": digest})
            continue
        data, verdict = read_canonical_file(path)
        if verdict:
            classified.append({"path": str(path), "verdict": verdict, "digest": digest})
            continue
        if not digest:
            classified.append({"path": str(path), "verdict": "UNREADABLE", "digest": digest})
            continue
        intact = hashlib.sha256(data or b"").hexdigest() == digest.strip().lower()
        classified.append({
            "path": str(path),
            "verdict": "INTACT" if intact else "CONTENT_MISMATCH",
            "digest": digest,
        })
    return classified


def detect_project_placement_drift(state, target_dir) -> list[dict]:
    """W2-005: recorded canonical paths must belong to the CURRENT placement.

    A run records absolute canonical paths (per-wave latest/history, the
    history dir and the final artifacts) from the placement it was ingested
    in. If the project has since been moved to another group/display name, or
    the configured audit root changed, those recorded paths point at a STALE
    placement while materialize would create campaign.json under the NEW one
    -- one campaign physically split across two sources of truth.

    Returns one entry per recorded canonical path that no longer lives under
    ``target_dir`` (empty list = placement is coherent). The caller must fail
    closed BEFORE any mkdir/write; a bounded migration is deliberately out of
    scope. Entries carry placement-level context (the stale path's last two
    components), never full absolute paths: enough to diagnose the move,
    nothing that leaks unrelated filesystem layout.
    """
    target_root = Path(target_dir).resolve()
    stale: list[dict] = []

    def _drifts(kind: str, wave_id: str, raw) -> None:
        if not raw:
            return
        path = Path(str(raw))
        try:
            resolved = path.resolve()
            contained = resolved.parent == target_root or target_root in resolved.parents
        except OSError:
            contained = False
        if contained:
            return
        parts = resolved.parts
        stale.append({
            "kind": kind,
            "wave": wave_id,
            "file": resolved.name,
            "placement": "/".join(parts[-3:-1]) if len(parts) >= 3 else (parts[-2] if len(parts) >= 2 else ""),
        })

    _drifts("history_dir", "", state.get("history_dir"))
    for wave_id, wave_state in (state.get("waves") or {}).items():
        if not isinstance(wave_state, dict):
            continue
        _drifts("latest_path", str(wave_id), wave_state.get("latest_path"))
        _drifts("history_path", str(wave_id), wave_state.get("history_path"))
    for key in ("all3_path", "final_handoff_path", "canonical_campaign_path"):
        _drifts("final_artifact", "", state.get(key))
    return stale


def _get_canonical_path(prof, state) -> Optional[Path]:
    if prof.canonical_artifact_kind == ARTIFACT_KIND_QUICK3_COMBINED:
        return Path(state["all3_path"]) if state.get("all3_path") else None
    return Path(state["canonical_campaign_path"]) if state.get("canonical_campaign_path") else None


class AudapackBridgeHandler(BaseHTTPRequestHandler):
    config: AppConfig
    browser_dispatcher: Optional[BrowserDispatcher] = None
    dispatch_supervisor: Optional[Any] = None

    @classmethod
    def set_browser_dispatcher(cls, dispatcher: BrowserDispatcher) -> None:
        cls.browser_dispatcher = dispatcher

    @classmethod
    def set_dispatch_supervisor(cls, supervisor: Any) -> None:
        cls.dispatch_supervisor = supervisor

    def _dispatcher(self) -> BrowserDispatcher:
        dispatcher = getattr(self.__class__, "browser_dispatcher", None)
        if dispatcher is None:
            dispatcher = BrowserDispatcher(state_dir=Path(self.get_custom_base_dir()) / "browser_dispatch" if self.get_custom_base_dir() else None)
            self.__class__.browser_dispatcher = dispatcher
        return dispatcher

    def _dispatch_error(self, status: int, exc: BrowserDispatchError) -> None:
        self.send_json(status, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})

    def _browser_slots_status(self) -> dict[str, Any]:
        """Commissioning status for all managed worker slots (T07 data side)."""
        from audapack.bridge.supervisor import MAX_AUDIT_LANES

        managed = {}
        for worker in self._dispatcher().list_workers():
            slot = int(getattr(worker, "managed_slot", 0) or 0)
            if 1 <= slot <= MAX_AUDIT_LANES:
                managed.setdefault(slot, []).append(worker)

        supervisor = getattr(self.__class__, "dispatch_supervisor", None)
        doc = {}
        generation = 1
        if supervisor is not None:
            try:
                doc = supervisor.workers._load()
            except Exception:
                doc = {}
            generation = max(1, int(doc.get("generation", 1) or 1))

        slots = []
        for slot in range(1, MAX_AUDIT_LANES + 1):
            tracked = doc.get("slots", {}).get(str(slot), {}) if isinstance(doc, dict) else {}
            slot_workers = managed.get(slot, [])
            slots.append({
                "slot": slot,
                "generation": generation,
                "state": str(tracked.get("state", "")),
                "launch_attempts": int(tracked.get("launch_attempts", 0) or 0),
                "last_seen_at": float(tracked.get("last_seen_at", 0.0) or 0.0),
                "cooldown_until": float(tracked.get("cooldown_until", 0.0) or 0.0),
                "message": str(tracked.get("message", ""))[:300],
                "registered": len(slot_workers) > 0,
                "worker_ids": [item.worker_id for item in slot_workers],
            })
        return {"max_lanes": MAX_AUDIT_LANES, "generation": generation, "slots": slots}

    def _handle_relaunch_slot(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        try:
            slot = int(data.get("slot", 0))
        except (TypeError, ValueError):
            self.send_json(400, {
                "ok": False,
                "error": {"code": "invalid_slot", "message": "slot must be an integer from 1 to 6", "retriable": False},
            })
            return
        supervisor = getattr(self.__class__, "dispatch_supervisor", None)
        if supervisor is None:
            self.send_json(503, {
                "ok": False,
                "error": {"code": "supervisor_unavailable", "message": "dispatch supervisor is not running", "retriable": True},
            })
            return
        result = supervisor.relaunch_managed_slot(slot)
        self.send_json(200, {"ok": True, **result})

    def log_message(self, format: str, *args):
        # Override to prevent default console spam; use logger
        pass

    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin")
        if origin:
            return origin
        return "null"

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ACB-Token, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "false")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ACB-Token, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "false")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # W2-001: explicit base directory for test isolation. When set on the
    # handler (or its subclass), registry and run-state operations use this
    # directory instead of the canonical %LOCALAPPDATA% path. Production
    # callers must NEVER set this; the port number must NOT be used as a
    # proxy for test isolation.
    test_base_dir: Optional[Path] = None

    def get_custom_base_dir(self) -> Optional[Path]:
        return self.test_base_dir

    def get_live_config(self) -> AppConfig:
        if self.test_base_dir:
            return self.config
        try:
            cfg = load_config()
            if cfg and cfg.audits and cfg.audits.root:
                return cfg
        except Exception:
            pass
        return self.config

    def _legacy_token_candidates(self) -> list[Path]:
        if legacy_token_acceptance_revoked():
            return []
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            return []
        base = Path(local_app_data)
        return [
            base / "ACBBridge" / "token.txt",
            base / "AUDAPACK" / "migration_backup" / "ACBBridge" / "token.txt",
        ]

    def check_auth(self) -> bool:
        token = self.headers.get("X-ACB-Token")
        if not token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()

        if not token:
            self.send_json(403, {"ok": False, "error": {"code": "invalid_auth", "message": "Authentication failed: token missing", "retriable": False}})
            return False

        valid_tokens = set()
        if self.config and self.config.bridge and self.config.bridge.token:
            valid_tokens.add(self.config.bridge.token)
        # PERF-002: the canonical token file, not a full config reconstruction.
        # Rotation is still picked up on the next request -- the file's
        # (mtime_ns, size) is the cache key.
        if self.test_base_dir:
            try:
                live = load_config(self.test_base_dir)
                if live and live.bridge and live.bridge.token:
                    valid_tokens.add(live.bridge.token)
            except Exception:
                pass
        else:
            live_token = _live_bridge_token()
            if live_token:
                valid_tokens.add(live_token)

        for candidate_path in self._legacy_token_candidates():
            if candidate_path.exists():
                try:
                    c_tok = candidate_path.read_text(encoding="utf-8").strip()
                    if c_tok and len(c_tok) >= 16:
                        valid_tokens.add(c_tok)
                except Exception:
                    pass

        for exp in valid_tokens:
            if secrets.compare_digest(token, exp):
                return True

        self.send_json(403, {"ok": False, "error": {"code": "invalid_auth", "message": "Authentication failed: invalid token", "retriable": False}})
        return False

    def is_valid_loopback_host(self) -> bool:
        host_hdr = self.headers.get("Host", "")
        host = host_hdr.split(":")[0].strip().lower()
        return host in ["127.0.0.1", "localhost", "::1", "[::1]"]

    def _peer_is_loopback(self) -> bool:
        try:
            peer = self.client_address[0].split("%")[0].strip()
            return ipaddress.ip_address(peer).is_loopback
        except (ValueError, AttributeError, TypeError):
            return False

    def do_GET(self):
        if not self.is_valid_loopback_host() or not self._peer_is_loopback():
            self.send_response(400)
            self.end_headers()
            return

        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/v1/health"):
            live_cfg = self.get_live_config()
            profs = load_profiles()
            build_id, source_revision = _get_build_identity()
            widget_bundle_version, widget_bundle_sha256 = _get_widget_bundle_info()
            self.send_json(200, {
                "ok": True,
                "service": "AUDAPACK Bridge",
                "version": __version__,
                "app_version": __version__,
                "api_version": BRIDGE_API_VERSION,
                "supported_api_versions": list(SUPPORTED_API_VERSIONS),
                "profiles": list(profs.keys()),
                "manifest_hash": get_canonical_manifest_hash(),
                "instance_id": f"audapack_{os.getpid()}",
                "instance_nonce": INSTANCE_NONCE,
                # W2-004: stop_bridge binds cleanup to the identity it captured
                # from a LIVE /health, so this must carry the PID as well as the
                # nonce.
                "pid": os.getpid(),
                "registry_revision": len(live_cfg.projects),
                "build_id": build_id,
                "source_revision": source_revision,
                "widget_bundle_version": widget_bundle_version,
                "widget_bundle_sha256": widget_bundle_sha256,
                "browser_worker_protocol": BROWSER_WORKER_PROTOCOL_VERSION,
            })
        elif parsed.path == "/v1/probe/bytes":
            self._handle_transport_probe(parsed.query)
        elif parsed.path == "/widget.user.js":
            w_path = get_bundled_widget_path()
            if w_path.exists():
                content = _widget_source_for_endpoint(
                    w_path.read_bytes(), self.headers.get("Host"), self.config
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", self._cors_origin())
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_json(404, {"ok": False, "error": "Widget resource not found"})
        elif parsed.path == "/v1/status":
            if not self.check_auth():
                return
            live_cfg = self.get_live_config()
            out_root = Path(live_cfg.audits.root)
            out_exists = out_root.exists()
            out_writable = False
            if out_exists:
                try:
                    test_file = out_root / f".test_{os.getpid()}"
                    test_file.touch()
                    test_file.unlink()
                    out_writable = True
                except Exception:
                    pass

            build_id, source_revision = _get_build_identity()
            widget_bundle_version, widget_bundle_sha256 = _get_widget_bundle_info()
            self.send_json(200, {
                "ok": True,
                "version": __version__,
                "app_version": __version__,
                "pid": os.getpid(),
                "output_root": str(out_root),
                "output_exists": out_exists,
                "output_writable": out_writable,
                "build_id": build_id,
                "source_revision": source_revision,
                "widget_bundle_version": widget_bundle_version,
                "widget_bundle_sha256": widget_bundle_sha256,
                "browser_worker_protocol": BROWSER_WORKER_PROTOCOL_VERSION,
                "prepared_scheduler": getattr(getattr(self, "prepared_worker", None),
                                              "status_snapshot", {"state": "UNAVAILABLE"}),
            })
        elif parsed.path in ["/v1/projects", "/v1/registry"]:
            if not self.check_auth():
                return
            live_cfg = self.get_live_config()
            registry = ProjectRegistry(live_cfg)
            active_groups = registry.get_active_groups()
            self.send_json(200, {
                "ok": True,
                # P1 TARGET C: a CONTENT revision, not a wall clock. The old
                # `int(time.time())` changed every second, so it could never
                # support "the registry has not changed, keep the cached list".
                "revision": self._registry_revision(live_cfg),
                "groups": active_groups,
                "projects": [
                    {
                        "project_id": p.id,
                        "display_name": p.display_name,
                        "audit_name": p.audit_project_name or p.display_name,
                        "group": p.priority_group,
                        "slot": p.slot,
                        "enabled": p.enabled,
                    }
                    for p in live_cfg.projects
                ],
            })
        elif parsed.path == "/v1/inaudit/captures":
            if not self.check_auth():
                return
            query = parse_qs(parsed.query)
            include_archived = str((query.get("include_archived") or [""])[0]).lower() in {"1", "true", "yes"}
            try:
                records = self._inaudit_store().list_records(include_archived=include_archived)
            except (OSError, UnicodeError) as exc:
                self._send_inaudit_persistence_error("inbox_read_failed", exc)
                return
            self.send_json(200, {"ok": True, "captures": records})
        elif parsed.path.startswith("/v1/inaudit/captures/"):
            if not self.check_auth():
                return
            capture_id, action = self._inaudit_path_parts(parsed.path)
            if not capture_id or action:
                self.send_json(404, {"ok": False, "error": "Endpoint not found"})
                return
            try:
                result = self._inaudit_store().get(capture_id)
            except InauditCaptureError as exc:
                self._send_inaudit_error(exc)
                return
            except (OSError, UnicodeError) as exc:
                self._send_inaudit_persistence_error("capture_read_failed", exc)
                return
            self.send_json(200, {"ok": True, **result})
        elif parsed.path == "/v1/profiles":
            if not self.check_auth():
                return
            profs = load_profiles()
            self.send_json(200, {
                "ok": True,
                "manifest_hash": get_canonical_manifest_hash(),
                "profiles": {
                    pid: p.to_dict() for pid, p in profs.items()
                },
            })
        elif parsed.path == "/v1/browser/status":
            if not self.check_auth():
                return
            dispatch = self._dispatcher().status()
            # Windows the supervisor opened versus windows that actually came
            # back. A worker profile signed out of ChatGPT lands on the
            # marketing page, never registers, and used to be invisible: the
            # operator saw W 1/6 with nothing anywhere saying why.
            try:
                slots = self._browser_slots_status().get("slots", [])
                dispatch["managed_slots_launched"] = sum(
                    1 for slot in slots if str(slot.get("state") or "") in {"LAUNCHING", "HEARTBEAT"}
                )
                dispatch["managed_slots_registered"] = sum(1 for slot in slots if slot.get("registered"))
            except Exception:
                dispatch["managed_slots_launched"] = 0
                dispatch["managed_slots_registered"] = 0
            dispatch["workers"] = [{
                "worker_id": worker.worker_id,
                "state": worker.state,
                "widget_version": worker.widget_version,
                "browser_name": worker.meta.get("browser_name", ""),
                "is_brave": worker.is_brave,
                "is_chromium": worker.is_chromium,
                "page_eligible": worker.page_eligible,
                "url_path": worker.url_path,
                "project_name": worker.project_name,
                "last_seen_at": worker.last_seen_at,
                "has_conversation_turns": worker.has_conversation_turns,
                "clean_for_audit": worker.clean_for_audit,
                "managed_slot": worker.managed_slot,
                "managed_generation": worker.managed_generation,
                "widget_build_version": worker.widget_build_version,
                # Which campaign this window is currently set to run. Dispatch
                # holds a profiled job for a matching window, so an operator
                # staring at a queued CM audit needs to see where it can go.
                "profile": worker.profile,
                # A refused reconcile is why a post-restart run can sit BLOCKED
                # with its original worker present and heartbeating. Invisible,
                # it looks like the recovery path simply never runs.
                "last_reconcile_error": worker.meta.get("last_reconcile_error", ""),
                # T-248: the live ChatGPT composer shape this window can really
                # see, refreshed on every heartbeat. It answers "why did every
                # lane block pre-START" with the current build's own file-input
                # topology instead of a guess made from a screenshot.
                "upload_topology": worker.meta.get("upload_topology", ""),
                "managed_profile": worker.managed_profile,
                "profile_allowed": self._dispatcher().worker_in_allowed_profile(worker),
                "reports_lease": bool(worker.meta.get("reports_lease")),
                # A stale build can never claim, so reporting it as CLEAN is a
                # lie the operator cannot act on. Name it first.
                "worker_class": "STALE_WIDGET" if self._dispatcher().worker_widget_is_stale(worker) else (
                    "CLEAN" if worker.clean_for_audit else (
                    "OCCUPIED" if worker.has_conversation_turns else (
                        "DIRTY" if (worker.has_manual_draft or worker.has_attachments) else (
                            "BUSY" if (worker.generating or worker.audit_start_in_flight or worker.action_in_flight) else "OCCUPIED"
                        )
                    )
                    )
                ),
            } for worker in self._dispatcher().list_workers()]
            self.send_json(200, {"ok": True, "dispatch": dispatch})
        elif parsed.path == "/v1/browser/jobs":
            if not self.check_auth():
                return
            query = parse_qs(parsed.query)
            project_id = str((query.get("project_id") or [""])[0]).strip()
            dispatcher = self._dispatcher()
            jobs = dispatcher.list_jobs()
            # Computed over the WHOLE line, before any project filter: position
            # 3 has to mean third in the queue, not third among what was asked for.
            waiting_positions = {
                item.dispatch_id: index
                for index, item in enumerate(dispatcher.queued_jobs_in_order())
            }
            if project_id:
                jobs = [job for job in jobs if job.project_id == project_id]
            self.send_json(200, {
                "ok": True,
                "jobs": [{
                    "dispatch_id": job.dispatch_id,
                    "project_id": job.project_id,
                    "project_name": job.project_name,
                    "state": job.state,
                    "assigned_worker_id": job.assigned_worker_id,
                    "campaign_run_id": job.campaign_run_id,
                    "conversation_id": job.conversation_id,
                    "start_receipt": job.start_receipt,
                    "profile": job.requested_profile,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "error": job.error,
                    "final_handoff_path": job.final_handoff_path,
                    "final_handoff_sha256": job.final_handoff_sha256,
                    "completed_at": job.completed_at,
                    "retry_count": job.retry_count,
                    "next_retry_at": job.next_retry_at,
                    "last_error_code": job.last_error_code,
                    "recovery_state": job.recovery_state,
                    # Held, not failed: the worker stopped mid-answer and is
                    # waiting for a human. The Project Room shows this beside
                    # the lane so an interrupted wave is not invisible.
                    "attention": job.attention,
                    # The run id the saved campaign actually carries when the
                    # widget's runtime re-derived it. Without it in this payload
                    # the coordinator cannot prove campaign_match, and a
                    # finished audit never leaves SAVING.
                    "meta_run_id_drift": job.meta_run_id_drift,
                    # Where a WAITING job sits in the line for the next window
                    # to come free. Zero-based; -1 for a job that is not waiting.
                    "queue_position": waiting_positions.get(job.dispatch_id, -1),
                } for job in jobs],
            })
        elif parsed.path.startswith("/v1/browser/jobs/") and parsed.path.endswith("/artifact"):
            if not self.check_auth():
                return
            self._serve_artifact(parsed.path)
            return
        elif parsed.path == "/v1/browser/slots":
            if not self.check_auth():
                return
            self.send_json(200, {"ok": True, **self._browser_slots_status()})
        elif self._project_archive_route(parsed.path) is not None:
            project_id, action = self._project_archive_route(parsed.path)
            if action:
                self.send_json(404, {"ok": False, "error": "Endpoint not found"})
                return
            self.handle_project_archive_download(project_id)
        else:
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})

    def do_POST(self):
        if not self.is_valid_loopback_host() or not self._peer_is_loopback():
            self.send_response(400)
            self.end_headers()
            return

        if not self.check_auth():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/v1/widget/diagnostics":
            self._handle_widget_diagnostics()
            return
        if parsed.path == "/v1/shutdown":
            self.send_json(200, {"ok": True, "message": "Shutting down bridge"})
            threading.Thread(target=self.server.shutdown).start()
            return

        if parsed.path in ["/v1/projects/resolve", "/v1/projects"]:
            self.handle_project_resolve()
            return

        if parsed.path == "/v1/inaudit/captures":
            self._handle_inaudit_capture()
            return

        if parsed.path.startswith("/v1/inaudit/captures/"):
            capture_id, action = self._inaudit_path_parts(parsed.path)
            if action == "assign":
                self._handle_inaudit_assign(capture_id)
                return
            if action == "archive":
                self._handle_inaudit_archive(capture_id)
                return
            if action == "restore":
                self._handle_inaudit_restore(capture_id)
                return

        if parsed.path == "/v1/audits/materialize":
            self.handle_audit_materialize()
            return

        if parsed.path == "/v1/audits":
            self.handle_audit_submission()
            return

        if parsed.path == "/v1/browser/poll":
            self._handle_browser_poll()
            return

        if parsed.path.startswith("/v1/browser/jobs/") and parsed.path.endswith("/state"):
            self._handle_browser_state(parsed.path)
            return

        if parsed.path == "/v1/browser/jobs":
            self._handle_browser_submit()
            return

        if parsed.path.startswith("/v1/browser/jobs/") and parsed.path.endswith("/cancel"):
            self._handle_browser_cancel(parsed.path)
            return

        if parsed.path.startswith("/v1/browser/jobs/") and parsed.path.endswith("/abandon"):
            self._handle_browser_abandon(parsed.path)
            return

        if parsed.path.startswith("/v1/browser/jobs/") and parsed.path.endswith("/reorder"):
            self._handle_browser_reorder(parsed.path)
            return

        if parsed.path == "/v1/browser/relaunch-slot":
            self._handle_relaunch_slot()
            return

        if self._project_archive_route(parsed.path) is not None:
            project_id, action = self._project_archive_route(parsed.path)
            if action == "ensure":
                self.handle_project_archive_ensure(project_id)
                return
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return

        self.send_json(404, {"ok": False, "error": "Endpoint not found"})

    def do_DELETE(self):
        if not self.is_valid_loopback_host() or not self._peer_is_loopback():
            self.send_response(400)
            self.end_headers()
            return
        if not self.check_auth():
            return
        parsed = urlparse(self.path)
        capture_id, action = self._inaudit_path_parts(parsed.path)
        if not capture_id or action:
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return
        store = self._inaudit_store()
        try:
            store.delete(capture_id)
        except InauditCaptureError as exc:
            self._send_inaudit_error(exc)
            return
        except (OSError, UnicodeError) as exc:
            self._send_inaudit_persistence_error("capture_delete_failed", exc)
            return
        self._send_inaudit_committed(store, {"capture_id": capture_id})

    @staticmethod
    def _inaudit_path_parts(path: str) -> tuple[str, str]:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 4 or parts[:3] != ["v1", "inaudit", "captures"] or len(parts) > 5:
            return "", ""
        return parts[3], parts[4] if len(parts) == 5 else ""

    def _inaudit_store(self):
        """The retained server-owned INAUDIT store.

        PERF-002 (audit/4.md): this called `store_for_config(...)` per ENDPOINT
        CALL, and every `InauditCaptureStore.__init__` runs crash recovery --
        `_pair_fragments()` walks inbox/archive/recovery and `_pair_is_intact()`
        reads and SHA-256s every body. So even a read-only GET reconstructed and
        integrity-checked the whole lifecycle store first; measured ~40 ms per
        construction on a 400-record/25 MiB corpus, under the store lock, so
        growing history also serialized concurrent INAUDIT operations.

        One store per (base_dir, runtime root) is retained instead: recovery
        runs once per Bridge lifetime at that construction, exactly where the
        W2 recovery guarantees belong. Distinct base dirs still get distinct
        stores -- the isolation the old per-call construction provided for
        tests is preserved by keying on the same inputs.
        """
        base_dir = self.get_custom_base_dir()
        key = str(base_dir) if base_dir else "<runtime>"
        with _INAUDIT_STORE_LOCK:
            store = _INAUDIT_STORES.get(key)
            if store is None:
                store = store_for_config(self.get_live_config(), base_dir=base_dir)
                _INAUDIT_STORES[key] = store
            return store

    def _send_inaudit_error(self, exc: InauditCaptureError) -> None:
        self.send_json(
            exc.status,
            {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": False}},
        )

    def _send_inaudit_persistence_error(self, code: str, exc: BaseException) -> None:
        self.send_json(
            500,
            {"ok": False, "error": {"code": code, "message": str(exc), "retriable": True}},
        )

    def _send_inaudit_committed(self, store, payload: dict) -> None:
        """200 for a committed mutation, with the notification state stated.

        W2-004 (audit/2.md): notification publication used to share the mutation's
        success boundary, so a generation-write failure answered 500 retriable
        after the capture/move/delete had already committed -- and the retry then
        contradicted that with a 404. `committed=True` plus
        `notification_pending` says exactly what happened instead.
        """
        pending = bool(getattr(store, "notification_pending", False))
        body = {"ok": True, "committed": True, **payload}
        if pending:
            body["notification_pending"] = True
        self.send_json(200, body)

    def _handle_inaudit_capture(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        # SRC-083: a captured handoff is filed as a handoff and written as a
        # ready-to-hand-over file, so the operator pastes a path instead of
        # copying the block through a scratchpad first. The registry decides the
        # project-addressed shape, so only a REGISTERED name counts.
        live_cfg = self.get_live_config()
        text = data.get("text") if isinstance(data, dict) else None
        label = (
            handoff_drop.detect_handoff(text, handoff_drop.project_names(live_cfg.projects))
            if isinstance(text, str)
            else None
        )
        # `handoff_only`: the widget's automatic path offers a block that merely
        # LOOKS project-addressed. Anything the Bridge does not recognize as a
        # handoff is dropped here -- never filed as an inbox capture.
        if isinstance(data, dict) and data.get("handoff_only") is True and not label:
            self.send_json(200, {"ok": True, "committed": False, "durable": False, "handoff": None, "skipped": "not_a_handoff"})
            return
        if label and str(data.get("capture_kind") or "response").lower() in ("response", "block", "clipboard"):
            data = {**data, "capture_kind": "handoff"}
        store = self._inaudit_store()
        try:
            result = store.capture(data, live_cfg.projects)
        except InauditCaptureError as exc:
            self._send_inaudit_error(exc)
            return
        except OSError as exc:
            self.send_json(
                500,
                {"ok": False, "error": {"code": "capture_persistence_failed", "message": str(exc), "retriable": True}},
            )
            return
        if label:
            result = {**result, "handoff": self._materialize_handoff(normalize_capture_text(text), label, live_cfg)}
        else:
            result = self._pin_capture_to_archive_project(store, data, result, live_cfg.projects)
        self._send_inaudit_committed(store, result)

    def _pin_capture_to_archive_project(self, store, data: dict, result: dict, projects) -> dict:
        """Pin an audit reply to the project whose archive it answered.

        The widget sends ``archive_filename`` when it captures a reply to a user
        turn that carried a project archive. The archive name is an exact project
        identity, stronger than any text classification, so the capture lands in
        the Inbox already pinned. A name no single project owns, a capture that
        is already pinned or assigned, or any pin failure leaves the capture as
        it was -- the capture itself is already durable.
        """
        filename = data.get("archive_filename") if isinstance(data, dict) else None
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 260:
            return result
        record = result.get("record") or {}
        if record.get("target_project_id") or record.get("assigned_project_id"):
            return result
        project = project_for_archive_filename(filename, projects)
        if project is None:
            return result
        try:
            pinned = store.set_target_project(str(record.get("capture_id") or ""), project.id, projects)
        except (InauditCaptureError, OSError) as exc:
            logger.warning("archive reply capture not pinned: %s", exc)
            return result
        return {**result, "record": pinned, "pinned_project": project.display_name}

    def _materialize_handoff(self, text: str, label: str, live_cfg) -> dict:
        """Write the handoff file; a failure here never undoes the capture."""
        configured = getattr(getattr(live_cfg, "bridge", None), "handoff_dir", "") or ""
        folder = handoff_drop.resolve_drop_dir(configured)
        try:
            path, reused = handoff_drop.materialize(text, label, folder)
        except OSError as exc:
            logger.warning("handoff file not written: %s", exc)
            return {"ok": False, "label": label, "error": str(exc)[:240]}
        return {"ok": True, "label": label, "path": str(path), "filename": path.name, "reused": reused}

    def _handle_inaudit_assign(self, capture_id: str) -> None:
        data = self._read_json_body()
        if data is None:
            return
        if any(key in data for key in ("path", "filename", "destination", "assigned_path")):
            self._send_inaudit_error(
                InauditCaptureError("path_not_allowed", "assignment accepts project_id, never a destination path")
            )
            return
        store = self._inaudit_store()
        try:
            result = store.assign(
                capture_id,
                str(data.get("project_id") or ""),
                self.get_live_config().projects,
                action=str(data.get("action") or ""),
            )
        except InauditCaptureError as exc:
            self._send_inaudit_error(exc)
            return
        except OSError as exc:
            self.send_json(
                500,
                {"ok": False, "error": {"code": "assignment_persistence_failed", "message": str(exc), "retriable": True}},
            )
            return
        self._send_inaudit_committed(store, result)

    def _handle_inaudit_archive(self, capture_id: str) -> None:
        store = self._inaudit_store()
        try:
            record = store.archive(capture_id)
        except InauditCaptureError as exc:
            self._send_inaudit_error(exc)
            return
        except (OSError, UnicodeError) as exc:
            self._send_inaudit_persistence_error("archive_persistence_failed", exc)
            return
        self._send_inaudit_committed(store, {"record": record})

    def _handle_inaudit_restore(self, capture_id: str) -> None:
        store = self._inaudit_store()
        try:
            record = store.restore(capture_id)
        except InauditCaptureError as exc:
            self._send_inaudit_error(exc)
            return
        except (OSError, UnicodeError) as exc:
            self._send_inaudit_persistence_error("restore_persistence_failed", exc)
            return
        self._send_inaudit_committed(store, {"record": record})

    def _read_json_body(self) -> Optional[dict]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "Content-Length must be a non-negative integer", "retriable": False}})
            return None
        if length < 0:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "Content-Length must be non-negative", "retriable": False}})
            return None
        max_bytes = int(getattr(self.config.bridge, "max_request_bytes", 10 * 1024 * 1024))
        if length > max_bytes:
            self.send_json(413, {"ok": False, "error": {"code": "payload_too_large", "retriable": False}})
            return None
        try:
            self.connection.settimeout(5.0)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("request body ended before Content-Length")
            data = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data
        except Exception:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_json", "retriable": False}})
            return None

    def handle_project_resolve(self):
        data = self._read_json_body()
        if data is None:
            return

        raw_name = str(data.get("project_name") or data.get("name") or data.get("project_id") or "").strip()
        if not raw_name:
            self.send_json(400, {"ok": False, "error": {"code": "missing_project_name", "message": "project_name is required", "retriable": False}})
            return

        live_cfg = self.get_live_config()
        registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        try:
            proj, was_created = registry.resolve_or_register_project(raw_name)
        except RegistrySaveError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "configuration_error", "message": str(exc), "retriable": True}})
            return

        if was_created:
            from audapack.bridge.state import GenerationPersistenceError, publish_audit_generation
            try:
                publish_audit_generation(proj.display_name, "registered", project_id=proj.id)
            except GenerationPersistenceError as exc:
                logger.warning("registered generation publish deferred: %s", exc)
            if _ON_AUDIT_WRITTEN:
                try:
                    _ON_AUDIT_WRITTEN(proj.display_name, "registered")
                except Exception:
                    pass

        self.send_json(200, {
            "ok": True,
            "status": "registered" if was_created else "existing",
            "project_id": proj.id,
            "display_name": proj.display_name,
            "audit_name": proj.audit_project_name or proj.display_name,
            "group": proj.priority_group,
            "slot": proj.slot,
            "registry_revision": len(live_cfg.projects),
            "created": was_created,
        })

    @staticmethod
    def _project_archive_route(path: str) -> Optional[tuple[str, str]]:
        """Parse /v1/projects/<id>/archive[/ensure] with no other shape.

        Returns ``(project_id, action)`` where action is ``""`` for the download
        route or ``"ensure"``. Any other path (including a trailing arbitrary
        segment) is not an archive route.
        """
        parts = [part for part in str(path or "").split("/") if part]
        if len(parts) not in (4, 5):
            return None
        if parts[0] != "v1" or parts[1] != "projects" or parts[3] != "archive":
            return None
        action = parts[4] if len(parts) == 5 else ""
        if action not in ("", "ensure"):
            return None
        return unquote(parts[2]), action

    @staticmethod
    def _registry_revision(live_cfg) -> str:
        """A content digest of the registered project identities (TARGET C).

        The project picker caches its list; a cache may only be kept while the
        thing it caches is unchanged. A clock cannot state that (the previous
        `int(time.time())` revision changed every second regardless), so this is
        a digest of exactly the fields the picker renders and the ZIP path
        depends on: identity, names, group, slot, enabled. Two readbacks that
        differ here genuinely differ; two that agree may safely share a list.
        """
        try:
            payload = [
                [
                    str(getattr(p, "id", "")),
                    str(getattr(p, "display_name", "")),
                    str(getattr(p, "audit_project_name", "") or ""),
                    str(getattr(p, "priority_group", "") or ""),
                    int(getattr(p, "slot", 0) or 0),
                    bool(getattr(p, "enabled", False)),
                ]
                for p in list(getattr(live_cfg, "projects", None) or [])
            ]
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
        except Exception:  # noqa: BLE001 - an unreadable registry has no revision
            return ""

    def _registered_project(self, project_id: str):
        """Read-only registered lookup. Never registers and never guesses."""
        live_cfg = self.get_live_config()
        registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        return live_cfg, registry.get_project_by_id(project_id)

    def _canonical_archive_path(self, proj, live_cfg) -> Optional[Path]:
        """The archive the packer would call canonical for ``proj``.

        Server-side resolution only: the output directory comes from the live
        configuration and the project identity, never from the request.
        """
        if not getattr(proj, "source_path", ""):
            return None
        output_dir = resolve_output_dir(
            proj.source_path,
            live_cfg.packing,
            fallback=app_dir(),
            group=proj.priority_group,
            project=proj,
        )
        archive = find_archive_for_project(proj, output_dir)
        if archive is None or not archive.is_file():
            return None
        return archive

    def _archive_project_eligibility(self, project_id: str):
        """Shared archive eligibility for ensure AND download.

        Read-only, never registers. Returns ``(live_cfg, proj, error)`` where
        ``error`` is None or a ``(status, code, message)`` triple. Keeping one
        policy owner prevents ensure and download from drifting apart.
        """
        live_cfg, proj = self._registered_project(project_id)
        if proj is None:
            return live_cfg, None, (404, "unknown_project", "Project is not registered")
        if not proj.enabled:
            return live_cfg, None, (400, "project_disabled", "Project is disabled")
        if not proj.source_path:
            return live_cfg, None, (400, "project_source_missing", "Project has no source path")
        try:
            available = Path(proj.source_path).is_dir()
        except OSError:
            available = False
        if not available:
            return live_cfg, None, (400, "project_source_unavailable", "Project source directory is unavailable")
        return live_cfg, proj, None

    def handle_project_archive_ensure(self, project_id: str) -> None:
        if not self.check_auth():
            return
        live_cfg, proj, error = self._archive_project_eligibility(project_id)
        if error is not None:
            status, code, message = error
            self.send_json(status, {"ok": False, "error": {"code": code, "message": message, "retriable": False}})
            return

        from audapack.services.packing_service import PackingService

        packer = PackingService(live_cfg, base_dir=self.get_custom_base_dir())
        ensure_started = time.perf_counter()
        try:
            result = packer.ensure_fresh_archive(proj.id)
        except Exception as exc:  # noqa: BLE001 - surface an exact, non-fatal error
            self.send_json(503, {"ok": False, "error": {"code": "archive_pack_failed", "message": str(exc)[:240], "retriable": True}})
            return
        ensure_ms = (time.perf_counter() - ensure_started) * 1000.0
        if not result.success or not result.output_path:
            self.send_json(400, {"ok": False, "error": {"code": "archive_unavailable", "message": result.error_message or "Archive could not be produced", "retriable": False}})
            return
        archive = Path(result.output_path)
        try:
            stat = archive.stat()
        except OSError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "archive_unreadable", "message": str(exc), "retriable": True}})
            return
        # P1 TARGET D: ask the receipt store first. A reused archive whose
        # identity (path + size + mtime_ns + ctime_ns + policy fingerprint) is
        # unchanged returns its recorded SHA without rereading the whole ZIP;
        # anything else hashes once and refreshes the receipt.
        sha_started = time.perf_counter()
        try:
            digest, digest_from_receipt = archive_receipt.proven_digest(
                archive,
                policy_fingerprint=self._archive_policy_fingerprint(live_cfg),
                compute=self._sha256_path,
            )
        except OSError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "archive_unreadable", "message": str(exc), "retriable": True}})
            return
        sha_ms = (time.perf_counter() - sha_started) * 1000.0
        timings = dict(getattr(result, "timings", None) or {})
        timings.update({
            "ensure_total_ms": round(ensure_ms, 3),
            "server_archive_sha_ms": round(sha_ms, 3),
            "sha_receipt_reused": bool(digest_from_receipt),
        })
        # P1 TARGET E: the freshness probe is measured inside the ensure decision
        # and reported separately, so "is the walk or the pack the latency?" is
        # answered by evidence instead of by a guess.
        self.send_json(200, {
            "ok": True,
            "project_id": proj.id,
            "display_name": proj.display_name,
            "filename": archive.name,
            "size": int(stat.st_size),
            "mtime": int(stat.st_mtime),
            "sha256": digest,
            "sha_source": "receipt" if digest_from_receipt else "computed",
            "reused": bool(getattr(result, "reused", False)),
            "packed": bool(getattr(result, "packed", False)),
            "timings": timings,
            # T-190: compact terminal pack state + Git inventory summary.
            "status": str(getattr(result, "status", "") or ("PACKED" if result.success else "")),
            "git_summary": str(getattr(result, "git_summary", "") or ""),
        })

    @staticmethod
    def _archive_policy_fingerprint(live_cfg) -> str:
        """The packing policy identity the archive must prove it was built under.

        Empty means "this process cannot state the policy", and an empty
        fingerprint makes every receipt unusable by construction (see
        `audapack.archive_receipt`), so an inability to compute it degrades to
        "hash it" rather than to "trust a stale digest".
        """
        try:
            from audapack.fidelity import policy_fingerprint_from_config

            packing = live_cfg.packing
            return policy_fingerprint_from_config(packing, set(getattr(packing, "excludes", None) or []))
        except Exception:  # noqa: BLE001 - no policy identity means no receipt
            return ""

    def handle_project_archive_download(self, project_id: str) -> None:
        # SRC-083 TARGET C: the pre-stream work is timed per step so a slow GET
        # can be attributed to auth, project/archive resolution or the digest
        # proof instead of to "the download". Durations only -- never a path.
        entered = time.perf_counter()
        if not self.check_auth():
            return
        auth_done = time.perf_counter()
        live_cfg, proj, error = self._archive_project_eligibility(project_id)
        if error is not None:
            status, code, message = error
            self.send_json(status, {"ok": False, "error": {"code": code, "message": message, "retriable": False}})
            return
        archive = self._canonical_archive_path(proj, live_cfg)
        if archive is None:
            self.send_json(404, {"ok": False, "error": {"code": "archive_missing", "message": "No canonical archive for this project", "retriable": False}})
            return
        try:
            size = archive.stat().st_size
        except OSError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "archive_unreadable", "message": str(exc), "retriable": True}})
            return
        resolved = time.perf_counter()
        # TARGET D on the download path too: this used to hash the entire archive
        # BEFORE streaming it, so every GET paid two full reads of the same bytes.
        try:
            digest, _from_receipt = archive_receipt.proven_digest(
                archive,
                policy_fingerprint=self._archive_policy_fingerprint(live_cfg),
                compute=self._sha256_path,
            )
        except OSError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "archive_unreadable", "message": str(exc), "retriable": True}})
            return
        digested = time.perf_counter()
        auth_ms = (auth_done - entered) * 1000.0
        resolve_ms = (resolved - auth_done) * 1000.0
        digest_ms = (digested - resolved) * 1000.0
        prep_ms = (digested - entered) * 1000.0
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{archive.name}"')
            self.send_header("X-AUDAPACK-Archive-SHA256", digest)
            # Where the digest came from, so "did this GET reread the ZIP?" is
            # answerable from the wire instead of inferred.
            self.send_header("X-AUDAPACK-Archive-SHA256-Source", "receipt" if _from_receipt else "computed")
            self.send_header("X-AUDAPACK-Archive-Prep-Ms", f"{prep_ms:.3f}")
            self.send_header("X-AUDAPACK-Archive-Digest-Ms", f"{digest_ms:.3f}")
            self.send_header(
                "Server-Timing",
                f"auth;dur={auth_ms:.3f}, resolve;dur={resolve_ms:.3f}, digest;dur={digest_ms:.3f}, prep;dur={prep_ms:.3f}",
            )
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ACB-Token, Authorization")
            self.send_header(
                "Access-Control-Expose-Headers",
                "X-AUDAPACK-Archive-SHA256, X-AUDAPACK-Archive-SHA256-Source, X-AUDAPACK-Archive-Prep-Ms, "
                "X-AUDAPACK-Archive-Digest-Ms, Server-Timing, Content-Disposition, Content-Length",
            )
            self.end_headers()
            with archive.open("rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, BrokenPipeError) as exc:
            logger.warning("project archive stream interrupted: %s", exc)

    #: SRC-083 TARGET F: upper bound of one transport probe body.
    TRANSPORT_PROBE_MAX_BYTES = 8 * 1024 * 1024

    def _handle_transport_probe(self, query: str) -> None:
        """Stream N zero bytes so the widget can time a browser transport.

        The widget compares GM_xmlhttpRequest with the page's native fetch on a
        body the same size as the canonical archive. The body is content-free
        zeros, so the endpoint needs no token: a native fetch is never handed
        the Bridge credential just to be measured, and no project data can
        leave through it. Size is clamped; loopback checks already ran.
        """
        try:
            size = int((parse_qs(query).get("size") or ["0"])[0])
        except (TypeError, ValueError):
            size = 0
        size = max(1, min(self.TRANSPORT_PROBE_MAX_BYTES, size or (1 << 20)))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "Content-Length")
            self.end_headers()
            block = memoryview(bytes(1 << 20))
            remaining = size
            while remaining > 0:
                step = min(remaining, len(block))
                self.wfile.write(block[:step])
                remaining -= step
        except (OSError, BrokenPipeError) as exc:
            logger.warning("transport probe stream interrupted: %s", exc)

    def _completed_wave_duplicate_response(
        self,
        *,
        prof,
        run_id: str,
        project: str,
        project_id: str,
        live_cfg,
        state: dict,
        wave_def,
        wave_state: dict,
        dt_str: str,
        content: str = "",
    ) -> tuple[int, dict]:
        """Unified idempotent response for an already-committed wave (W2-002).

        SRC-041:R005: the same-receipt retry and the different-receipt retry were
        two copies of the same work -- only the receipt path repaired incomplete
        finalization and flushed `generation_pending`, so a retry that reused the
        wave content under a new receipt skipped both and could answer
        `campaign_ready=False` forever while the generation marker stayed pending.
        Both now run this one helper, so duplicate handling is identical.

        Secondary work (generation publication, marker clear, finalization) never
        drops the HTTP connection: a marker-clear `save_run_state` failure keeps
        `generation_pending` durable and still returns valid JSON (SRC-041:R005).

        T-185: a duplicate is durability proof only if every canonical expected
        file still holds the exact committed bytes. Both call sites gate on
        `wave_state["sha256"] == sha256(content)`, so the submitted bytes ARE the
        canonical bytes and a MISSING/CONTENT_MISMATCH file can be repaired with
        them transactionally. WRONG_TYPE / UNREADABLE fail closed -- a directory
        or an unreadable file is never silently replaced. Legacy state without
        recorded paths derives its expected paths instead of answering files=[].
        """
        from audapack.bridge.state import publish_audit_generation

        try:
            target_dir, resolved_name, _proj_dup, _created = resolve_project_audit_dir(
                live_cfg, project, project_id or None, base_dir=self.get_custom_base_dir()
            )
        except InvalidProjectPathError as exc:
            return 400, {
                "ok": False,
                "error": {"code": "invalid_project_path", "message": str(exc), "retriable": False},
            }
        except Exception as exc:
            return 503, {
                "ok": False,
                "error": {"code": "duplicate_resolve_failed", "message": str(exc), "retriable": True},
            }

        completed_at = str(wave_state.get("completed_at") or dt_str)
        history_dir = (
            Path(state["history_dir"])
            if state.get("history_dir")
            else expected_history_dir(target_dir, completed_at, run_id)
        )
        latest_path, history_path = expected_wave_representation_paths(
            wave_number=wave_def.number,
            wave_slug=wave_def.slug,
            resolved_name=resolved_name,
            target_dir=target_dir,
            history_dir=history_dir,
            completed_at=completed_at,
            latest_path=str(wave_state["latest_path"]) if wave_state.get("latest_path") else None,
            history_path=str(wave_state["history_path"]) if wave_state.get("history_path") else None,
        )

        classification: dict[Path, str] = {}
        for path in dict.fromkeys((latest_path, history_path)):
            classification[path] = classify_canonical_file(path, content) if content else "UNREADABLE"

        repairable = [p for p, verdict in classification.items() if verdict in ("MISSING", "CONTENT_MISMATCH")]
        blocking = [p for p, verdict in classification.items() if verdict in ("WRONG_TYPE", "UNREADABLE")]
        repaired_paths: list[str] = []
        if repairable and content:
            lock_root = target_dir
            try:
                with campaign_transaction_lock(lock_root):
                    snapshots, snap_err = capture_file_snapshots(repairable)
                    if snap_err:
                        return 503, {
                            "ok": False,
                            "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True},
                        }
                    try:
                        for path in repairable:
                            atomic_write(path, content)
                            repaired_paths.append(str(path))
                    except Exception as exc:
                        # Transactional: an unrepairable second write must return
                        # the first file to its exact pre-request bytes.
                        return 503, _rollback_error(snapshots, "atomic_write_failed", str(exc))
                    # Re-verify AFTER repair: only physical canonical bytes count.
                    # An injected no-op write leaves the file MISSING again.
                    still_bad = [
                        str(path) for path in repairable
                        if classify_canonical_file(path, content) != "INTACT"
                    ]
                    if still_bad:
                        return 503, {
                            "ok": False,
                            "error": {
                                "code": "duplicate_files_missing",
                                "message": (
                                    "Wave content matches the canonical run but these canonical files "
                                    "could not be restored and verified: " + "; ".join(still_bad)
                                ),
                                "retriable": True,
                                "files_missing": still_bad,
                            },
                        }
            except Exception as exc:
                return 503, {
                    "ok": False,
                    "error": {
                        "code": "atomic_write_failed",
                        "message": f"Duplicate-path repair could not take the campaign lock: {exc}",
                        "retriable": True,
                    },
                }

        if blocking:
            # Fail closed: no durability success while a canonical path is a
            # directory / special file / unreadable. Retriable: filesystem
            # conditions like this may be fixed by the operator.
            details = "; ".join(f"{path}={classification[path]}" for path in blocking)
            return 503, {
                "ok": False,
                "error": {
                    "code": "duplicate_files_unverified",
                    "message": (
                        "Wave content matches the canonical run but these canonical paths are not "
                        "verifiable regular files: " + details
                    ),
                    "retriable": True,
                    "files_unverified": [str(path) for path in blocking],
                },
            }

        completed_count = len([
            w for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")
        ])
        is_ready = state.get("campaign_complete", False) or state.get("all3_complete", False)

        if state.get("generation_pending", False):
            try:
                from audapack.bridge.state import publish_audit_generation
                publish_audit_generation(
                    project, wave_def.id, project_id=state.get("project_id") or None
                )
            except GenerationPersistenceError:
                pass
            else:
                state["generation_pending"] = False
                try:
                    save_run_state(run_id, state)
                except RunStatePersistenceError:
                    # Generation published, durable marker-clear failed: keep
                    # the marker pending (in memory and on disk) and answer
                    # normally; a later retry repairs it.
                    state["generation_pending"] = True

        # Repair incomplete finalization for a completed campaign (CORE-002).
        all_waves_complete = all(
            w.id in state.get("waves", {}) and state["waves"][w.id].get("complete")
            for w in prof.waves if w.required
        )
        if not is_ready and all_waves_complete:
            try:
                target_dir, resolved_name, proj, _created = resolve_project_audit_dir(
                    live_cfg, project, project_id, base_dir=self.get_custom_base_dir()
                )
                target_dir.mkdir(parents=True, exist_ok=True)
                parsed_dict = {
                    w.id: state["waves"][w.id].get("meta", {})
                    for w in prof.waves if w.id in state.get("waves", {})
                }
                # Reuse the finalizing wave's original completion stamp so a
                # repair overwrites its existing history artifact instead of
                # minting a second one (W2-002 G3: no new history artifact).
                final_wave = prof.waves[-1] if prof.waves else None
                final_wave_state = (
                    state.get("waves", {}).get(final_wave.id, {}) if final_wave else {}
                )
                final_dt = str(final_wave_state.get("completed_at") or dt_str)
                history_dir = Path(state["history_dir"]) if state.get("history_dir") else None
                if history_dir is None or not history_dir.exists():
                    history_dir = expected_history_dir(target_dir, final_dt, run_id)
                    history_dir.mkdir(parents=True, exist_ok=True)
                    state["history_dir"] = str(history_dir)
                # CORE-004: route duplicate finalization repair through the same
                # transactional commit gate as normal delivery. W2-001: same
                # campaign-root lock as ingest and delivery.
                with campaign_transaction_lock(target_dir):
                    snap_targets = [target_dir / "campaign.json"]
                    snap_targets.extend(
                        _final_artifact_paths(prof, target_dir, history_dir, final_dt, resolved_name)
                    )
                    snapshots, snap_err = capture_file_snapshots(snap_targets)
                    if snap_err:
                        return 503, {
                            "ok": False,
                            "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True},
                        }
                    try:
                        synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)
                        _write_final_artifacts(prof, synth_result, target_dir, history_dir,
                                               final_dt, state, resolved_name)
                        save_live_campaign_index(
                            campaign_root=target_dir, profile=prof, run_id=run_id,
                            project_name=resolved_name,
                            parsed_waves={
                                wid: {
                                    "wave_id": wid,
                                    "status": "COMPLETE",
                                    "tickets": int(w_info.get("meta", {}).get("tickets", 0)),
                                    "file": Path(w_info["latest_path"]) if w_info.get("latest_path") else None,
                                    "sha256": w_info.get("sha256", ""),
                                    "completed_at": w_info.get("completed_at", final_dt),
                                }
                                for wid, w_info in state.get("waves", {}).items()
                            },
                            completed_waves=[w.id for w in prof.waves],
                            active_wave_id=None,
                            status=STATUS_CAMPAIGN_COMPLETE,
                            final_handoff_path=_get_final_handoff_path(prof, state),
                        )
                    except Exception as exc:
                        return 503, _rollback_error(snapshots, "campaign_index_failed", str(exc))
                    try:
                        save_run_state(run_id, state)
                    except RunStatePersistenceError as exc:
                        return 503, _rollback_error(snapshots, "campaign_index_failed", str(exc))
                is_ready = True
                try:
                    from audapack.bridge.state import GenerationPersistenceError as _GPE2
                    from audapack.bridge.state import publish_audit_generation as _pub2
                    _pub2(resolved_name, wave_def.id, project_id=proj.id if proj else None)
                except _GPE2 as exc:
                    logger.warning("duplicate finalization generation deferred: %s", exc)
                    state["generation_pending"] = True
                    try:
                        save_run_state(run_id, state)
                    except Exception:
                        pass
            except Exception as exc:
                return 503, {
                    "ok": False,
                    "error": {"code": "finalization_failed", "message": str(exc), "retriable": True},
                }

        # T-185 P0-2 (B6): a duplicate of the final required wave must not
        # answer campaign-durable while the required final artifact set is
        # broken. The committed wave meta proves enough to rebuild finalization
        # safely, so repair it transactionally -- or fail closed.
        if is_ready and _campaign_requires_final_artifacts(prof, state):
            try:
                target_dir, resolved_name, _proj_b6, _created = resolve_project_audit_dir(
                    live_cfg, project, project_id, base_dir=self.get_custom_base_dir()
                )
            except Exception as exc:
                return 503, {
                    "ok": False,
                    "error": {"code": "duplicate_resolve_failed", "message": str(exc), "retriable": True},
                }
            final_bad = [
                entry for entry in classify_required_artifacts(
                    _expected_final_artifacts(prof, state, target_dir, resolved_name),
                    target_dir,
                )
                if entry["verdict"] != "INTACT"
            ]
            if final_bad:
                unsafe = [
                    entry["path"] for entry in final_bad
                    if entry["verdict"] in ("WRONG_TYPE", "UNREADABLE", "OUTSIDE_CANONICAL")
                ]
                if unsafe:
                    return 503, {
                        "ok": False,
                        "error": {
                            "code": "campaign_final_files_unverified",
                            "message": (
                                "Campaign final artifact(s) cannot be safely repaired: "
                                + "; ".join(f"{e['path']}={e['verdict']}" for e in final_bad)
                            ),
                            "retriable": True,
                            "files_unverified": unsafe,
                        },
                    }
                # Missing / mismatched final bytes: rebuild through the same
                # synthesizer, transactionally, exactly like a fresh
                # finalization would.
                final_wave = prof.waves[-1] if prof.waves else None
                final_wave_state = (
                    state.get("waves", {}).get(final_wave.id, {}) if final_wave else {}
                )
                final_dt = str(final_wave_state.get("completed_at") or dt_str)
                history_dir = Path(state["history_dir"]) if state.get("history_dir") else None
                if history_dir is None or not history_dir.exists():
                    history_dir = expected_history_dir(target_dir, final_dt, run_id)
                    history_dir.mkdir(parents=True, exist_ok=True)
                    state["history_dir"] = str(history_dir)
                parsed_dict = {
                    w.id: state["waves"][w.id].get("meta", {})
                    for w in prof.waves if w.id in state.get("waves", {})
                }
                with campaign_transaction_lock(target_dir):
                    snap_targets = [target_dir / "campaign.json"]
                    snap_targets.extend(
                        _final_artifact_paths(prof, target_dir, history_dir, final_dt, resolved_name)
                    )
                    snapshots, snap_err = capture_file_snapshots(snap_targets)
                    if snap_err:
                        return 503, {
                            "ok": False,
                            "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True},
                        }
                    try:
                        synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)
                        _write_final_artifacts(
                            prof, synth_result, target_dir, history_dir,
                            final_dt, state, resolved_name,
                        )
                        still_bad = [
                            entry["path"] for entry in classify_required_artifacts(
                                _expected_final_artifacts(prof, state, target_dir, resolved_name),
                                target_dir,
                            )
                            if entry["verdict"] != "INTACT"
                        ]
                        if still_bad:
                            return 503, _rollback_error(
                                snapshots, "campaign_final_files_unverified",
                                "campaign final artifact repair failed verification: "
                                + ", ".join(still_bad),
                            )
                    except Exception as exc:
                        return 503, _rollback_error(snapshots, "finalization_failed", str(exc))
                    try:
                        save_run_state(run_id, state)
                    except RunStatePersistenceError as exc:
                        return 503, _rollback_error(snapshots, "campaign_index_failed", str(exc))
                repaired_paths.extend(
                    str(entry["path"]) for entry in final_bad
                )

        return 200, {
            "ok": True,
            "duplicate": True,
            "run_id": run_id,
            "profile_id": prof.profile_id,
            "project": project,
            "wave": wave_def.id,
            "wave_index": wave_def.ordinal,
            "wave_count": prof.wave_count,
            "completed_waves": completed_count,
            "total_waves": prof.wave_count,
            "campaign_ready": is_ready,
            "all3_ready": is_ready if prof.profile_id == "quick3" else state.get("all3_complete", False),
            "files": [str(latest_path), str(history_path)],
            "repaired_files": repaired_paths,
            "integrity": {
                str(path): "INTACT" for path in dict.fromkeys((latest_path, history_path))
            },
        }

    # ------------------------------------------------------------------
    # Manual materialization (T-184)
    #
    # `/v1/audits` is INGEST: it commits a newly complete wave into the
    # canonical campaign run, and a completed wave is immutable there. Manual
    # SYNC/SAVE is a different operation -- it physically re-creates the files
    # that already-canonical content is supposed to occupy. Routing it through
    # ingest made an exact duplicate look like a failure and made a genuine
    # content conflict (`completed_wave_immutable`) look like success. This
    # endpoint owns representation recovery and nothing else: it never mints a
    # run, never mutates a wave's receipt/sha/completion stamp, and fails
    # closed the moment the requested bytes are not the canonical bytes.
    # ------------------------------------------------------------------

    MATERIALIZE_RECEIPTS_MAX = 50

    @staticmethod
    def _materialize_request_hash(
        *,
        run_id: str,
        project_id: str,
        project: str,
        profile_id: str,
        waves: list,
    ) -> str:
        canonical = json.dumps(
            {
                "run_id": run_id,
                "project_id": project_id,
                "project": project,
                "profile_id": profile_id,
                "waves": sorted(
                    [
                        {
                            "wave_id": str(w.get("wave_id") or w.get("wave") or "").strip().lower()
                            if isinstance(w, dict) else "",
                            "sha256": hashlib.sha256(
                                str(w.get("content", "") if isinstance(w, dict) else "").encode("utf-8")
                            ).hexdigest(),
                        }
                        for w in waves
                    ],
                    key=lambda item: (item["wave_id"], item["sha256"]),
                ),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _record_materialization(state: dict, receipt: str, request_sha: str, response: dict) -> None:
        ledger = state.get("materializations")
        if not isinstance(ledger, dict):
            ledger = {}
        ledger[receipt] = {
            "request_sha256": request_sha,
            "at": datetime.now(timezone.utc).isoformat(),
            "response": response,
        }
        limit = AudapackBridgeHandler.MATERIALIZE_RECEIPTS_MAX
        if len(ledger) > limit:
            ordered = sorted(ledger.items(), key=lambda kv: str(kv[1].get("at", "")))
            for stale_receipt, _stale in ordered[: len(ledger) - limit]:
                ledger.pop(stale_receipt, None)
        state["materializations"] = ledger

    @staticmethod
    def _record_replay_repair_failure(state: dict, receipt: str, failed_paths: list[str]) -> None:
        """A4: a failed replay repair never rewrites the stored success.

        The receipt stays the same logical operation with its original success
        history intact; the current repair failure is recorded separately under
        ``repair_failures`` so diagnostics keep both truths.
        """
        ledger = state.get("materializations")
        if not isinstance(ledger, dict) or receipt not in ledger:
            return
        entry = ledger[receipt]
        failures = entry.setdefault("repair_failures", [])
        failures.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "files": list(failed_paths),
        })
        state["materializations"] = ledger

    def handle_audit_materialize(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self.send_json(415, {"ok": False, "error": {"code": "unsupported_media_type", "retriable": False}})
            return

        data = self._read_json_body()
        if data is None:
            return

        try:
            client_api = int(data.get("api_version", 0))
        except Exception:
            client_api = 0
        if client_api not in SUPPORTED_API_VERSIONS:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_api_version",
                    "message": f"Bridge speaks API versions {list(SUPPORTED_API_VERSIONS)}; payload declared v{client_api}",
                    "retriable": False,
                }
            })
            return

        run_id = str(data.get("source_run_id") or data.get("run_id") or "").strip()
        project = str(data.get("project") or data.get("project_name") or "").strip()
        project_id = str(data.get("project_id", "")).strip()
        receipt = str(data.get("receipt", "")).strip()
        requested_profile = str(data.get("profile_id", "")).strip().lower()
        # T-185: a verification asks "do the canonical files still exist?" and
        # writes nothing. It needs no content and takes no receipt slot, so a
        # background check can never mutate a campaign or burn idempotency.
        verify_only = bool(data.get("verify_only"))
        waves_raw = data.get("waves")
        if not isinstance(waves_raw, list):
            waves_raw = []

        if not run_id or not waves_raw or (not receipt and not verify_only):
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "missing_fields",
                    "message": "source_run_id, receipt and a non-empty waves[] are required",
                    "retriable": False,
                }
            })
            return

        live_cfg = self.get_live_config()
        out_root = Path(live_cfg.audits.root).resolve()
        if not out_root.exists():
            self.send_json(503, {
                "ok": False,
                "error": {
                    "code": "output_unavailable",
                    "message": f"Audit root unavailable: {out_root}",
                    "retriable": True,
                }
            })
            return

        live_registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        if project_id and live_registry.get_project_by_id(project_id) is None:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "invalid_project_id",
                    "message": f"Unknown project_id: {project_id}",
                    "retriable": False,
                }
            })
            return

        with run_transaction(run_id):
            try:
                state = get_run_state(run_id)
            except RunStateCorruptionError as exc:
                self.send_json(503, {
                    "ok": False,
                    "error": {"code": "run_state_corrupt", "message": str(exc), "retriable": True}
                })
                return

            # A materialization never creates a campaign. get_run_state()
            # answers a blank scaffold for an unknown id, and a scaffold has no
            # waves -- exactly the "nothing canonical to re-materialize" case.
            if not isinstance(state.get("waves"), dict) or not state.get("waves"):
                self.send_json(404, {
                    "ok": False,
                    "error": {
                        "code": "unknown_run",
                        "message": f"No canonical campaign run state exists for run {run_id}",
                        "retriable": False,
                    }
                })
                return

            bound_profile = str(state.get("profile_id") or "").strip().lower()
            if requested_profile and bound_profile and requested_profile != bound_profile:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "campaign_profile_conflict",
                        "message": f"Run {run_id} is bound to profile '{bound_profile}', cannot materialize as '{requested_profile}'",
                        "retriable": False,
                    }
                })
                return
            try:
                prof = get_profile(bound_profile or requested_profile or "quick3")
            except KeyError:
                self.send_json(400, {
                    "ok": False,
                    "error": {
                        "code": "unsupported_profile",
                        "message": f"Unknown campaign profile: '{bound_profile or requested_profile}'",
                        "retriable": False,
                    }
                })
                return

            bound_pid = str(state.get("project_id") or "")
            bound_name = str(state.get("project") or "")
            if project_id and bound_pid and project_id != bound_pid:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": f"Run {run_id} is bound to project_id '{bound_pid}', cannot materialize for '{project_id}'",
                        "retriable": False,
                    }
                })
                return
            if project:
                named = live_registry.get_project_by_name(project)
                if bound_pid and named is not None and named.id != bound_pid:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "project_identity_conflict",
                            "message": f"Run {run_id} is bound to project_id '{bound_pid}' but '{project}' resolves to '{named.id}'",
                            "retriable": False,
                        }
                    })
                    return
                if not bound_pid and bound_name and named is None and bound_name.strip().lower() != project.strip().lower():
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "project_identity_conflict",
                            "message": f"Run {run_id} belongs to project '{bound_name}', cannot materialize for '{project}'",
                            "retriable": False,
                        }
                    })
                    return

            request_sha = self._materialize_request_hash(
                run_id=run_id,
                project_id=bound_pid or project_id,
                project=bound_name or project,
                profile_id=prof.profile_id,
                waves=waves_raw,
            )
            ledger = state.get("materializations")
            prior = None if verify_only else (ledger.get(receipt) if isinstance(ledger, dict) else None)
            replay_prior = None
            if isinstance(prior, dict):
                if str(prior.get("request_sha256", "")) != request_sha:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "receipt_conflict",
                            "message": "Materialize receipt already used with a different request body",
                            "retriable": False,
                        }
                    })
                    return
                # T-185 P0-1: a prior receipt is an operation IDENTITY, not a
                # licence to replay a stored JSON response. The physical effect
                # is re-proven below (A1 preflight) before any replay answer.
                replay_prior = prior

            # ---- validation pass: prove every requested wave BEFORE writing ----
            planned: list = []
            seen_waves: set = set()
            for entry in waves_raw:
                if not isinstance(entry, dict):
                    self.send_json(400, {
                        "ok": False,
                        "error": {"code": "missing_fields", "message": "waves[] entries must be objects", "retriable": False}
                    })
                    return
                wave_raw = str(entry.get("wave_id") or entry.get("wave") or "").strip().lower()
                content = str(entry.get("content", ""))
                declared_sha = str(entry.get("sha256", "")).strip().lower()
                wave_def = prof.get_wave_by_id(wave_raw) or prof.get_wave_by_number(wave_raw)
                if not wave_def:
                    self.send_json(400, {
                        "ok": False,
                        "error": {
                            "code": "unsupported_wave",
                            "message": f"Wave '{wave_raw}' is not valid for profile '{prof.profile_id}'",
                            "retriable": False,
                        }
                    })
                    return
                if wave_def.id in seen_waves:
                    self.send_json(400, {
                        "ok": False,
                        "error": {
                            "code": "duplicate_wave",
                            "message": f"Wave '{wave_def.id}' appears twice in one materialize request",
                            "retriable": False,
                        }
                    })
                    return
                seen_waves.add(wave_def.id)
                if not content and not verify_only:
                    self.send_json(400, {
                        "ok": False,
                        "error": {
                            "code": "missing_fields",
                            "message": f"Wave '{wave_def.id}' carries no content",
                            "retriable": False,
                        }
                    })
                    return

                wave_state = state["waves"].get(wave_def.id) or {}
                if not wave_state.get("complete"):
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "materialize_wave_not_complete",
                            "message": f"Wave '{wave_def.id}' is not COMPLETE in run {run_id}; only canonical complete content can be materialized",
                            "retriable": False,
                        }
                    })
                    return

                if verify_only and not content:
                    planned.append({
                        "wave_def": wave_def,
                        "wave_state": wave_state,
                        "content": "",
                        "sha256": str(wave_state.get("sha256", "")),
                        "meta": wave_state.get("meta", {}) or {},
                    })
                    continue

                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if declared_sha and declared_sha != content_hash:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "materialize_content_conflict",
                            "message": f"Declared sha256 for wave '{wave_def.id}' does not hash the submitted content",
                            "retriable": False,
                        }
                    })
                    return
                canonical_sha = str(wave_state.get("sha256", "")).lower()
                if not canonical_sha or canonical_sha != content_hash:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "materialize_content_conflict",
                            "message": (
                                f"Wave '{wave_def.id}' canonical sha256 '{canonical_sha[:12]}' does not match "
                                f"submitted content '{content_hash[:12]}'; nothing was written"
                            ),
                            "retriable": False,
                        }
                    })
                    return

                valid, wave_meta, parse_err = parse_wave(content, wave_def.id, prof)
                if not valid:
                    self.send_json(400, {
                        "ok": False,
                        "error": {"code": "invalid_wave_structure", "message": parse_err, "retriable": False}
                    })
                    return

                planned.append({
                    "wave_def": wave_def,
                    "wave_state": wave_state,
                    "content": content,
                    "sha256": content_hash,
                    "meta": wave_meta or {},
                })

            planned.sort(key=lambda item: item["wave_def"].ordinal)

            # T-185 D2: a verification is read-only, so it may never resolve
            # through registration. A legacy run that never stored project_id
            # must not mint a registry entry just because someone asked whether
            # its files still exist.
            if verify_only:
                resolved = resolve_project_audit_dir_readonly(
                    live_cfg,
                    bound_name or project,
                    bound_pid or project_id or None,
                    base_dir=self.get_custom_base_dir(),
                )
                if resolved is None:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "project_unresolvable_readonly",
                            "message": (
                                f"Cannot resolve project identity for run {run_id} without "
                                "registering; verification never registers projects"
                            ),
                            "retriable": False,
                        }
                    })
                    return
                target_dir, resolved_name, _proj_ro = resolved
            else:
                try:
                    target_dir, resolved_name, proj, _created = resolve_project_audit_dir(
                        live_cfg,
                        bound_name or project,
                        bound_pid or project_id or None,
                        base_dir=self.get_custom_base_dir(),
                    )
                except InvalidProjectPathError as exc:
                    self.send_json(400, {
                        "ok": False,
                        "error": {"code": "invalid_project_path", "message": str(exc), "retriable": False}
                    })
                    return

            # W2-005 (T-188): the placement gate. The current destination was
            # resolved once above; every canonical path the run RECORDS must
            # still belong to it before this operation touches anything. A
            # recorded path under a stale placement (project moved between
            # groups / display names, or the audit root itself changed) would
            # otherwise let materialize write campaign.json HERE while wave
            # writes still land in the OLD location -- split-brain campaign
            # state. Fail closed with one stable code; no mkdir, no repair,
            # no receipt, no run-state write. Receipt replay goes through this
            # gate too, so a stale success can never be resurrected. Alias
            # changes of the SAME project resolve to the same target_dir and
            # pass: identity is registry-owned, drift is physical.
            stale_placement = detect_project_placement_drift(state, target_dir)
            if stale_placement:
                current_placement = target_dir.resolve().relative_to(out_root).as_posix()
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_placement_changed",
                        "message": (
                            f"Run {run_id} records canonical artifacts under a stale project placement "
                            f"({len(stale_placement)} recorded path(s) outside the current placement "
                            f"'{current_placement}'). The campaign must be migrated to one placement; "
                            "nothing was written, created or repaired."
                        ),
                        "retriable": False,
                        "current_placement": current_placement,
                        "stale_paths": stale_placement,
                    }
                })
                return

            if not verify_only:
                # Only the WRITE path may create the target directory, and only
                # after the placement gate proved the run is not split-brained.
                target_dir.mkdir(parents=True, exist_ok=True)

            final_dt = str(
                planned[-1]["wave_state"].get("completed_at")
                or datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            )
            if state.get("history_dir"):
                history_dir = Path(state["history_dir"])
            else:
                history_dir = expected_history_dir(target_dir, final_dt, run_id)
            # T-185 D1: a verification changes nothing on disk -- not even a
            # directory. Only the materialization path creates the history dir.
            if not verify_only:
                history_dir.mkdir(parents=True, exist_ok=True)

            # A materialization re-uses the canonical paths the original commit
            # recorded. Only a legacy record that never stored them gets a
            # deterministic path derived from its own completion stamp -- never
            # a fresh "now" stamp, which would mint a second history artifact.
            # ONE derivation owner for materialize / duplicate / verify (T-185 C).
            for item in planned:
                wave_def = item["wave_def"]
                wave_state = item["wave_state"]
                item["latest_path"], item["history_path"] = expected_wave_representation_paths(
                    wave_number=wave_def.number,
                    wave_slug=wave_def.slug,
                    resolved_name=resolved_name,
                    target_dir=target_dir,
                    history_dir=history_dir,
                    completed_at=str(wave_state.get("completed_at") or final_dt),
                    latest_path=str(wave_state["latest_path"]) if wave_state.get("latest_path") else None,
                    history_path=str(wave_state["history_path"]) if wave_state.get("history_path") else None,
                )

            # T-185: ingest reconciles the transport project against the
            # handoff's own PROJECT_NAME (CORE-005). Materialization must not be
            # the weaker door: a body naming a different project never gets to
            # overwrite this project's canonical files. Identity is compared
            # through canonical registry resolution -- display_name and
            # audit_project_name aliases of the SAME project are legitimate
            # (ingest policy), a genuinely different project is a 409.
            for item in planned:
                handoff_project = str((item["meta"] or {}).get("project_name") or "").strip()
                if handoff_project:
                    handoff_proj = live_registry.get_project_by_name(handoff_project)
                    base_proj = live_registry.get_project_by_id(
                        str(state.get("project_id") or "") or (proj.id if not verify_only and proj else "")
                    ) or (
                        (not verify_only and proj)
                        or live_registry.get_project_by_name(resolved_name)
                        or None
                    )
                    if handoff_proj and base_proj and handoff_proj.id != base_proj.id:
                        self.send_json(409, {
                            "ok": False,
                            "error": {
                                "code": "project_identity_conflict",
                                "message": (
                                    f"Wave '{item['wave_def'].id}' handoff PROJECT_NAME "
                                    f"'{handoff_project}' resolves to project '{handoff_proj.id}' "
                                    f"but this campaign belongs to '{base_proj.id}'; nothing was written"
                                ),
                                "retriable": False,
                            }
                        })
                        return
                    if not handoff_proj and handoff_project.strip().lower() != resolved_name.strip().lower():
                        self.send_json(409, {
                            "ok": False,
                            "error": {
                                "code": "project_identity_conflict",
                                "message": (
                                    f"Wave '{item['wave_def'].id}' handoff PROJECT_NAME "
                                    f"'{handoff_project}' does not match canonical project "
                                    f"'{resolved_name}'; nothing was written"
                                ),
                                "retriable": False,
                            }
                        })
                        return

            # T-185 P0-2 (B): the required physical durability set. Every
            # consumer -- verify_only, receipt replay, materialization
            # postcondition, duplicate acknowledgement -- shares THIS
            # derivation; there are no four slightly different lists.
            campaign_ready = _campaign_requires_final_artifacts(prof, state)
            final_required = _expected_final_artifacts(prof, state, target_dir, resolved_name)
            final_classified = classify_required_artifacts(final_required, target_dir)

            # T-185 P0-1 (A1/A2/A3): receipt replay proves the physical effect
            # of the stored response BEFORE answering. A receipt is an
            # operation identity, never permission to skip durability.
            replay_repair_paths: list[str] = []
            if replay_prior is not None:
                replay_verdicts: dict[str, str] = {}
                for item in planned:
                    for path in (item["latest_path"], item["history_path"]):
                        replay_verdicts[str(path)] = classify_canonical_file(path, item["content"])
                for entry in final_classified:
                    replay_verdicts[entry["path"]] = entry["verdict"]
                unsafe = [path for path, verdict in replay_verdicts.items()
                          if verdict in ("WRONG_TYPE", "UNREADABLE", "OUTSIDE_CANONICAL")]
                if unsafe:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "materialize_replay_unsafe",
                            "message": (
                                "Receipt replay found canonical paths that cannot be safely "
                                f"verified or repaired: {', '.join(unsafe)}. Nothing was overwritten."
                            ),
                            "retriable": False,
                            "files_unsafe": unsafe,
                        }
                    })
                    return
                broken = [path for path, verdict in replay_verdicts.items()
                          if verdict in ("MISSING", "CONTENT_MISMATCH")]
                if not broken:
                    # A1: every required effect is still intact -- the stored
                    # response may be replayed safely.
                    replay = dict(replay_prior.get("response") or {})
                    replay["duplicate"] = True
                    replay["files_intact"] = True
                    self.send_json(200, replay)
                    return
                # A2: the effect was lost. Re-enter canonical materialization
                # under the SAME receipt -- no new receipt, no second logical
                # campaign, unchanged wave identity.
                replay_repair_paths = broken

            if verify_only:
                # Read-only: report what is on disk and change nothing.
                # T-185 E: existence is not integrity -- each expected path is
                # classified against the canonical committed bytes. A corrupted
                # file is a mismatch, not a silently durable file.
                # T-185 P0-2 (B4): the final handoff is part of the required
                # set for a campaign-complete run, never merely waved through.
                verify_waves = []
                missing_files = []
                mismatched_files = []
                unreadable_files = []
                for item in planned:
                    paths = [item["latest_path"], item["history_path"]]
                    absent = []
                    mismatched = []
                    unreadable = []
                    for path in paths:
                        if item["content"]:
                            verdict = classify_canonical_file(path, item["content"])
                        else:
                            verdict = classify_canonical_file_by_sha(
                                path,
                                physical_sha256=str(item["wave_state"].get("physical_sha256", "")),
                                logical_sha256=str(item["wave_state"].get("sha256", "")),
                            )
                        if verdict == "MISSING":
                            absent.append(str(path))
                        elif verdict == "CONTENT_MISMATCH":
                            mismatched.append(str(path))
                        elif verdict in ("UNREADABLE", "WRONG_TYPE"):
                            unreadable.append(str(path))
                    missing_files.extend(absent)
                    mismatched_files.extend(mismatched)
                    unreadable_files.extend(unreadable)
                    verify_waves.append({
                        "wave_id": item["wave_def"].id,
                        "sha256": str(item["wave_state"].get("sha256", "")),
                        "files": [str(path) for path in paths],
                        "missing": absent,
                        "mismatched": mismatched,
                        "unreadable": unreadable,
                        "intact": not (absent or mismatched or unreadable),
                    })
                final_artifacts = [
                    {"path": entry["path"], "digest": entry.get("digest", ""), "verdict": entry["verdict"]}
                    for entry in final_classified
                ]
                final_missing = [entry["path"] for entry in final_classified if entry["verdict"] == "MISSING"]
                final_mismatched = [
                    entry["path"] for entry in final_classified if entry["verdict"] == "CONTENT_MISMATCH"
                ]
                final_unreadable = [
                    entry["path"] for entry in final_classified
                    if entry["verdict"] in ("UNREADABLE", "WRONG_TYPE", "OUTSIDE_CANONICAL")
                ]
                missing_files.extend(final_missing)
                mismatched_files.extend(final_mismatched)
                unreadable_files.extend(final_unreadable)
                self.send_json(200, {
                    "ok": True,
                    "verify_only": True,
                    "duplicate": False,
                    "run_id": run_id,
                    "project": resolved_name,
                    "profile_id": prof.profile_id,
                    "waves": verify_waves,
                    "final_artifacts": final_artifacts,
                    "missing_files": missing_files,
                    "mismatched_files": mismatched_files,
                    "unreadable_files": unreadable_files,
                    "files_intact": not (missing_files or mismatched_files or unreadable_files),
                })
                return

            # T-185 P0-2 (B5): on a campaign-complete run the final handoff is
            # ALWAYS part of the required repair set -- a partial request that
            # leaves ALL_3 deleted is the same false durability the replay had.
            rebuild_final = campaign_ready
            # A materialization must not move the campaign pointer. campaign.json
            # falls back to waves[0] when it is handed active_wave_id=None on an
            # unfinished campaign, so materializing Core on a 1/3 run would
            # rewrite `current_wave_id` from 'second' back to 'core'. Re-derive
            # the first still-incomplete wave instead.
            next_incomplete = next(
                (w for w in prof.waves if not state["waves"].get(w.id, {}).get("complete")),
                None,
            )
            active_wid = None if campaign_ready else (
                next_incomplete.id if next_incomplete else None
            )

            with campaign_transaction_lock(target_dir):
                snapshot_paths = [target_dir / "campaign.json"]
                for item in planned:
                    snapshot_paths.append(item["latest_path"])
                    snapshot_paths.append(item["history_path"])
                if rebuild_final:
                    snapshot_paths.extend(
                        _final_artifact_paths(prof, target_dir, history_dir, final_dt, resolved_name)
                    )
                snapshots, snap_err = capture_file_snapshots(snapshot_paths)
                if snap_err:
                    self.send_json(503, {
                        "ok": False,
                        "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True}
                    })
                    return

                files_written = []
                wave_results = []
                try:
                    for item in planned:
                        item["history_path"].parent.mkdir(parents=True, exist_ok=True)
                        atomic_write(item["history_path"], item["content"])
                        atomic_write(item["latest_path"], item["content"])
                        files_written.append(str(item["latest_path"]))
                        files_written.append(str(item["history_path"]))
                        wave_results.append({
                            "wave_id": item["wave_def"].id,
                            "sha256": item["sha256"],
                            "files": [str(item["latest_path"]), str(item["history_path"])],
                        })
                except Exception as exc:
                    self.send_json(503, _rollback_error(snapshots, "atomic_write_failed", str(exc)))
                    return

                if rebuild_final:
                    parsed_dict = {
                        w.id: state["waves"][w.id].get("meta", {})
                        for w in prof.waves
                        if w.id in state["waves"]
                    }
                    try:
                        synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)
                        _write_final_artifacts(
                            prof, synth_result, target_dir, history_dir,
                            final_dt, state, resolved_name,
                        )
                    except Exception as exc:
                        # A4: a failed replay repair keeps the receipt's prior
                        # success history and records this failure separately.
                        if replay_prior is not None:
                            self._record_replay_repair_failure(
                                state, receipt, [str(p) for p in snapshot_paths]
                            )
                            try:
                                save_run_state(run_id, state)
                            except RunStatePersistenceError:
                                pass
                        self.send_json(503, _rollback_error(snapshots, "finalization_failed", str(exc)))
                        return
                    final_path = _get_final_handoff_path(prof, state)
                    if final_path:
                        files_written.append(str(final_path))

                # Canonical wave identity is untouched: only the recorded
                # physical paths are repaired for a legacy record that lacked
                # them. receipt / sha256 / completed_at / complete stay exactly
                # as the original ingest committed them. physical_sha256 is the
                # content-less verify anchor: it now describes the exact bytes
                # on disk even when they were recreated by materialization.
                for item in planned:
                    stored = state["waves"][item["wave_def"].id]
                    stored["latest_path"] = str(item["latest_path"])
                    stored["history_path"] = str(item["history_path"])
                    stored["physical_sha256"] = hashlib.sha256(
                        canonical_audit_bytes(item["content"])
                    ).hexdigest()
                state["history_dir"] = str(history_dir)

                # T-185 A2/A4: postcondition re-verification. Only success after
                # EVERY required artifact (waves + final) is INTACT.
                post_final = classify_required_artifacts(
                    _expected_final_artifacts(prof, state, target_dir, resolved_name),
                    target_dir,
                )
                post_final_bad = [e for e in post_final if e["verdict"] != "INTACT"]
                post_wave_bad = []
                for item in planned:
                    for path_key in ("latest_path", "history_path"):
                        path = item[path_key]
                        if classify_canonical_file(path, item["content"]) != "INTACT":
                            post_wave_bad.append(str(path))
                if post_final_bad or post_wave_bad:
                    bad = [e["path"] for e in post_final_bad] + post_wave_bad
                    if replay_prior is not None:
                        self._record_replay_repair_failure(state, receipt, bad)
                        try:
                            save_run_state(run_id, state)
                        except RunStatePersistenceError:
                            pass
                    self.send_json(503, _rollback_error(snapshots, "materialize_postcondition_failed",
                                                      f"postcondition re-verification failed for: {', '.join(bad)}"))
                    return

                try:
                    save_live_campaign_index(
                        campaign_root=target_dir,
                        profile=prof,
                        run_id=run_id,
                        project_name=resolved_name,
                        parsed_waves={
                            wid: {
                                "wave_id": wid,
                                "status": "COMPLETE" if w_info.get("complete") else "IDLE",
                                "tickets": int(w_info.get("meta", {}).get("tickets", 0)),
                                "file": Path(w_info["latest_path"]) if w_info.get("latest_path") else None,
                                "sha256": w_info.get("sha256", ""),
                                "completed_at": w_info.get("completed_at", final_dt),
                            }
                            for wid, w_info in state["waves"].items()
                        },
                        completed_waves=[
                            w.id for w in prof.waves if state["waves"].get(w.id, {}).get("complete")
                        ],
                        active_wave_id=active_wid,
                        status=STATUS_CAMPAIGN_COMPLETE if campaign_ready else STATUS_CAMPAIGN_READY_FOR_WAVE,
                        final_handoff_path=_get_final_handoff_path(prof, state),
                    )
                except Exception as exc:
                    self.send_json(503, _rollback_error(snapshots, "campaign_index_failed", str(exc)))
                    return

                response = {
                    "ok": True,
                    "duplicate": bool(replay_prior),
                    "materialized": True,
                    "run_id": run_id,
                    "receipt": receipt,
                    "project": resolved_name,
                    "profile_id": prof.profile_id,
                    "waves": wave_results,
                    "files": files_written,
                    "repaired_files": list(replay_repair_paths),
                    "files_intact": True,
                    "final_rebuilt": bool(rebuild_final),
                    "campaign_ready": bool(campaign_ready),
                    "all3_ready": (
                        bool(campaign_ready)
                        if prof.profile_id == "quick3"
                        else bool(state.get("all3_complete", False))
                    ),
                }
                self._record_materialization(state, receipt, request_sha, response)
                try:
                    save_run_state(run_id, state)
                except RunStatePersistenceError as exc:
                    self.send_json(503, _rollback_error(snapshots, "run_state_persistence_failed", str(exc)))
                    return

            terminal_wave_id = planned[-1]["wave_def"].id

            from audapack.bridge.state import publish_audit_generation
            try:
                publish_audit_generation(
                    resolved_name,
                    terminal_wave_id,
                    project_id=proj.id if proj else None,
                )
            except GenerationPersistenceError:
                pass

        if _ON_AUDIT_WRITTEN:
            try:
                _ON_AUDIT_WRITTEN(resolved_name, terminal_wave_id)
            except Exception:
                pass

        self.send_json(200, response)

    def handle_audit_submission(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self.send_json(415, {"ok": False, "error": {"code": "unsupported_media_type", "retriable": False}})
            return

        data = self._read_json_body()
        if data is None:
            return

        # API version contract: support 2 and 3
        try:
            client_api = int(data.get("api_version", 0))
        except Exception:
            client_api = 0
        if client_api not in SUPPORTED_API_VERSIONS:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_api_version",
                    "message": f"Bridge speaks API versions {list(SUPPORTED_API_VERSIONS)}; payload declared v{client_api}",
                    "retriable": False,
                }
            })
            return

        run_id = str(data.get("run_id", "")).strip()
        project = str(data.get("project") or data.get("project_name") or "").strip()
        project_id = str(data.get("project_id", "")).strip() or None
        wave_raw = str(data.get("wave_id") or data.get("wave") or "").strip().lower()
        status = str(data.get("status", "complete")).strip().lower()
        receipt = str(data.get("receipt", "")).strip()
        content = str(data.get("content", ""))
        predecessor_sha = str(data.get("predecessor_sha256", "")).strip()

        # Profile resolution: if profile_id omitted, default to quick3 for v2 compatibility
        profile_id_req = str(data.get("profile_id", "")).strip().lower()
        if not profile_id_req:
            # Try to detect from wave name or default to quick3
            if wave_raw in ["core", "second", "performance"]:
                profile_id_req = "quick3"
            else:
                try:
                    all_profs = load_profiles()
                    for pid, pobj in all_profs.items():
                        if pobj.get_wave_by_id(wave_raw):
                            profile_id_req = pid
                            break
                except Exception:
                    pass
            if not profile_id_req:
                profile_id_req = "quick3"

        try:
            prof = get_profile(profile_id_req)
        except KeyError:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_profile",
                    "message": f"Unknown campaign profile: '{profile_id_req}'",
                    "retriable": False,
                }
            })
            return

        if not all([run_id, (project or project_id), wave_raw, receipt, content]):
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "missing_fields",
                    "message": "run_id, project/project_id, wave/wave_id, receipt, and content are required",
                    "retriable": False,
                }
            })
            return

        wave_def = prof.get_wave_by_id(wave_raw) or prof.get_wave_by_number(wave_raw)
        if not wave_def:
            valid_waves = [w.id for w in prof.waves]
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_wave",
                    "message": f"Wave '{wave_raw}' is not valid for profile '{prof.profile_id}'. Valid waves: {valid_waves}",
                    "retriable": False,
                }
            })
            return

        if status != "complete":
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "invalid_status",
                    "message": "Only complete waves can be delivered",
                    "retriable": False,
                }
            })
            return

        live_cfg = self.get_live_config()
        out_root = Path(live_cfg.audits.root).resolve()
        if not out_root.exists():
            self.send_json(503, {
                "ok": False,
                "error": {
                    "code": "output_unavailable",
                    "message": f"Audit root unavailable: {out_root}",
                    "retriable": True,
                }
            })
            return

        # Validate wave content structure against wave_def and profile
        valid, wave_meta, parse_err = parse_wave(
            content, wave_def.id, prof, require_identity=(client_api >= 3)
        )
        if not valid:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "invalid_wave_structure",
                    "message": parse_err,
                    "retriable": False,
                }
            })
            return

        # CORE-006: enforce equality between transport run_id and content
        # CAMPAIGN_RUN_ID for v3 contracts. Placeholder/missing values are
        # rejected so the durable run identity is provably bound to the
        # delivered artifact. v2 is allowed the historical relaxation.
        if client_api >= 3:
            crid_raw = (wave_meta or {}).get("campaign_run_id", "")
            crid = (crid_raw or "").strip()
            is_placeholder = (not crid) or ("<" in crid and ">" in crid) or crid.lower() in {
                "<run-id>", "<run_id>", "<campaign_run_id>", "placeholder", "n/a", "tbd",
            }
            if is_placeholder:
                self.send_json(400, {
                    "ok": False,
                    "error": {
                        "code": "invalid_run_id",
                        "message": "v3 contract requires a non-placeholder CAMPAIGN_RUN_ID header",
                        "retriable": False,
                    }
                })
                return
            if crid != run_id:
                # The transport run_id is the canonical authority for this
                # delivery. A mismatch happens when the widget re-arms or
                # materializes a capture under a different run id and the
                # browser-side content still carries the legacy header.
                # Patch the content CAMPAIGN_RUN_ID in place and proceed --
                # the rest of the audit body is valid.
                import re as _re
                content, replaced = _re.subn(
                    r'^(\s*CAMPAIGN_RUN_ID\s*:\s*).*$',
                    rf'\1{run_id}',
                    content,
                    count=1,
                    flags=_re.MULTILINE | _re.IGNORECASE,
                )
                if not replaced:
                    # Append a header line if the content truly lacks one
                    content = f"CAMPAIGN_RUN_ID: {run_id}\n{content}"
                wave_meta_refreshed = parse_wave(content, wave_def.id, prof)
                if wave_meta_refreshed[0]:
                    _dummy, wave_meta, _ = wave_meta_refreshed

        # CORE-005: two explicit project identities (transport payload vs parsed
        # handoff PROJECT_NAME) must be reconciled before any registration, file,
        # or state mutation. Aliases that resolve to the same canonical project
        # are accepted; a genuine conflict is a hard, non-retriable 409.
        requested_project = project
        handoff_project = (wave_meta or {}).get("project_name") or ""
        if handoff_project and handoff_project.strip().lower() != requested_project.strip().lower():
            check_registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)

            def _resolve_canonical(pid: Optional[str], name: str):
                if pid:
                    p = check_registry.get_project_by_id(pid)
                    if p:
                        return p
                if name:
                    p = check_registry.get_project_by_name(name)
                    if p:
                        return p
                return None

            p_req = _resolve_canonical(project_id, requested_project)
            p_hand = _resolve_canonical(None, handoff_project)
            if p_req and p_hand and p_req.id != p_hand.id:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": (
                            f"Payload project '{requested_project}' resolves to '{p_req.id}' "
                            f"but handoff PROJECT_NAME '{handoff_project}' resolves to '{p_hand.id}'"
                        ),
                        "retriable": False,
                    }
                })
                return
            if p_hand and not p_req:
                # Payload names nothing known but the handoff resolves to an existing
                # project: refusing prevents handoff metadata from silently stealing
                # routing/registration for a project the transport did not address.
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": (
                            f"Payload project '{requested_project}' is unknown but handoff "
                            f"PROJECT_NAME '{handoff_project}' resolves to '{p_hand.id}'; refusing reroute"
                        ),
                        "retriable": False,
                    }
                })
                return
            if not p_req and not p_hand:
                # Two different unknown names: ambiguous auto-registration.
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": (
                            f"Payload project '{requested_project}' and handoff PROJECT_NAME "
                            f"'{handoff_project}' identify different unknown projects; refusing ambiguous registration"
                        ),
                        "retriable": False,
                    }
                })
                return
            # Both resolve to the same canonical project (or payload resolves and
            # handoff is a harmless formatting alias): accept the canonical name.
            project = handoff_project

        if wave_meta and wave_meta.get("project_name"):
            project = wave_meta["project_name"]

        # Canonical project resolution
        live_registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        target_proj = None
        if project_id:
            target_proj = live_registry.get_project_by_id(project_id)
            if target_proj is None:
                self.send_json(400, {
                    "ok": False,
                    "error": {
                        "code": "invalid_project_id",
                        "message": f"Unknown project_id: {project_id}",
                        "retriable": False,
                    }
                })
                return

        if project:
            name_proj = live_registry.get_project_by_name(project)
            if target_proj and name_proj and name_proj.id != target_proj.id:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": f"Payload project_id '{project_id}' resolves to '{target_proj.id}' but handoff name '{project}' resolves to '{name_proj.id}'",
                        "retriable": False,
                    }
                })
                return
            if target_proj is None and name_proj is not None:
                target_proj = name_proj
            if target_proj is not None:
                project = target_proj.audit_project_name or target_proj.display_name

        with run_transaction(run_id):
            try:
                state = get_run_state(run_id)
            except RunStateCorruptionError as exc:
                self.send_json(503, {
                    "ok": False,
                    "error": {"code": "run_state_corrupt", "message": str(exc), "retriable": True}
                })
                return
            existing_project = state.get("project")

            # Validate immutable run -> project_id binding
            bound_pid = state.get("project_id") or ""
            if not bound_pid and existing_project:
                legacy_bound = live_registry.get_project_by_name(existing_project)
                bound_pid = legacy_bound.id if legacy_bound else ""
                if not legacy_bound:
                    if project and existing_project and existing_project.lower() != project.lower():
                        self.send_json(409, {
                            "ok": False,
                            "error": {
                                "code": "project_identity_conflict",
                                "message": f"Run {run_id} belongs to project '{existing_project}', cannot accept '{project}'",
                                "retriable": False,
                            }
                        })
                        return

            if bound_pid:
                bound_project = live_registry.get_project_by_id(bound_pid)
                bound_names = {
                    str(getattr(bound_project, "display_name", "")).strip().lower(),
                    str(getattr(bound_project, "audit_project_name", "")).strip().lower(),
                }
                if project and project.strip().lower() not in bound_names:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "project_identity_conflict",
                            "message": f"Run {run_id} is bound to project_id '{bound_pid}', cannot accept '{project}'",
                            "retriable": False,
                        }
                    })
                    return
            if target_proj is not None and bound_pid and target_proj.id != bound_pid:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": f"Run {run_id} is bound to project_id '{bound_pid}', cannot accept '{target_proj.id}'",
                        "retriable": False,
                    }
                })
                return

            # Validate immutable run -> profile_id binding
            bound_profile = state.get("profile_id") or ""
            if bound_profile and bound_profile != prof.profile_id:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "campaign_profile_conflict",
                        "message": f"Run {run_id} is bound to profile '{bound_profile}', cannot accept '{prof.profile_id}'",
                        "retriable": False,
                    }
                })
                return

            if target_proj is not None:
                state["project_id"] = target_proj.id
                state["project_display_name"] = target_proj.display_name
            elif not bound_pid:
                state["project"] = project or existing_project or ""
            if project or existing_project:
                state["project"] = project or existing_project

            state["profile_id"] = wave_meta.get("profile_id", prof.profile_id)
            # CORE-003: persist the manifest identity that was actually
            # declared and validated by parse_wave. NEVER launder an absent or
            # untrusted declared hash into the current canonical hash.
            declared_manifest = (wave_meta or {}).get("campaign_manifest_sha256") or ""
            state["manifest_hash"] = declared_manifest or prof.manifest_hash or get_canonical_manifest_hash()
            state["profile_version"] = prof.profile_version
            state["updated_at"] = datetime.now(timezone.utc).isoformat()

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
            dt_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            wave_state = state.get("waves", {}).get(wave_def.id, {})

            # Receipt idempotency check
            if wave_state.get("receipt") == receipt:
                if wave_state.get("sha256") == content_hash:
                    status, payload = self._completed_wave_duplicate_response(
                        prof=prof,
                        run_id=run_id,
                        project=project,
                        project_id=project_id,
                        live_cfg=live_cfg,
                        state=state,
                        wave_def=wave_def,
                        wave_state=wave_state,
                        dt_str=dt_str,
                        content=content,
                    )
                    self.send_json(status, payload)
                    return
                else:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "receipt_conflict",
                            "message": "Receipt already used with different content",
                            "retriable": False,
                        }
                    })
                    return

            # T04: A completed wave is immutable, but a retry with identical
            # content is semantically a no-op. Treat it as success (idempotent
            # duplicate) so old clients don't show false "data loss".
            if wave_state.get("complete"):
                existing_sha = wave_state.get("sha256", "")
                if existing_sha and existing_sha == content_hash:
                    # W2-002: a different receipt over identical committed content
                    # is the same no-op as a same-receipt retry; route both
                    # through the one unified duplicate helper.
                    status, payload = self._completed_wave_duplicate_response(
                        prof=prof,
                        run_id=run_id,
                        project=project,
                        project_id=project_id,
                        live_cfg=live_cfg,
                        state=state,
                        wave_def=wave_def,
                        wave_state=wave_state,
                        dt_str=dt_str,
                        content=content,
                    )
                    self.send_json(status, payload)
                    return
                # Content differs -> true mutation; reject to preserve immutability.
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "completed_wave_immutable",
                        "message": f"Wave '{wave_def.id}' is already complete in run {run_id}; start a fresh run for replacement",
                        "retriable": False,
                    }
                })
                return

            # Order / dependency validation
            existing_waves = state.get("waves", {})
            for dep_id in wave_def.depends_on:
                if dep_id not in existing_waves or not existing_waves[dep_id].get("complete"):
                    self.send_json(400, {
                        "ok": False,
                        "error": {
                            "code": "out_of_order_wave",
                            "message": f"Wave '{wave_def.id}' depends on wave '{dep_id}' which has not been completed yet in run {run_id}",
                            "retriable": False,
                        }
                    })
                    return

            # Predecessor hash validation if provided
            if predecessor_sha and wave_def.ordinal > 1:
                prev_wave = prof.get_wave_by_ordinal(wave_def.ordinal - 1)
                if prev_wave and prev_wave.id in existing_waves:
                    prev_recorded_sha = existing_waves[prev_wave.id].get("sha256", "")
                    if prev_recorded_sha and predecessor_sha.lower() != prev_recorded_sha.lower():
                        self.send_json(409, {
                            "ok": False,
                            "error": {
                                "code": "predecessor_mismatch",
                                "message": f"Declared predecessor hash '{predecessor_sha[:12]}' does not match recorded hash of previous wave '{prev_recorded_sha[:12]}'",
                                "retriable": False,
                            }
                        })
                        return

            # Resolve project audit directory through canonical registry
            try:
                target_dir, resolved_name, proj, was_created = resolve_project_audit_dir(
                    live_cfg, project, project_id, base_dir=self.get_custom_base_dir()
                )
            except InvalidProjectPathError as exc:
                self.send_json(400, {
                    "ok": False,
                    "error": {"code": "invalid_project_path", "message": str(exc), "retriable": False}
                })
                return
            target_dir.mkdir(parents=True, exist_ok=True)

            w_no = wave_def.number
            w_slug = wave_def.slug

            latest_filename = f"{resolved_name}__{w_no}_{w_slug}.md"
            latest_path = target_dir / latest_filename
            run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
            dt_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

            if state.get("history_dir") and Path(state["history_dir"]).exists():
                history_dir = Path(state["history_dir"])
            else:
                history_dir = target_dir / "_history" / f"{dt_str}_{run_hash}"
                history_dir.mkdir(parents=True, exist_ok=True)
                state["history_dir"] = str(history_dir)

            history_filename = f"{resolved_name}__{w_no}_{w_slug}__{dt_str}.md"
            history_path = history_dir / history_filename

            # CORE-003: snapshot all canonical artifact paths before any write
            # so ANY failure below can restore the exact previous state.
            # W2-001: wave delivery is one transaction against this campaign root --
            # snapshot, history and canonical wave writes, finalization of the
            # canonical artifacts, campaign.json, and the snapshot restore that every
            # failure below performs. Serialized against the ingest path and the
            # duplicate-finalization repair above, so no rollback here can restore
            # pre-transaction bytes over another writer's committed campaign. Lock
            # order: resolve_project_audit_dir (registry lock) already returned above.
            with campaign_transaction_lock(target_dir):
                snapshot_paths = [latest_path, history_path, target_dir / "campaign.json"]
                snapshot_paths.extend(
                    _final_artifact_paths(prof, target_dir, history_dir, dt_str, resolved_name)
                )
                snapshots, snap_err = capture_file_snapshots(snapshot_paths)
                if snap_err:
                    self.send_json(500, {
                        "ok": False,
                        "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True}
                    })
                    return

                # Write history first, then canonical latest (CORE-003).
                try:
                    atomic_write(history_path, content)
                    atomic_write(latest_path, content)
                except Exception as exc:
                    self.send_json(500, _rollback_error(snapshots, "atomic_write_failed", str(exc)))
                    return

                if "waves" not in state:
                    state["waves"] = {}
                state["waves"][wave_def.id] = {
                    "complete": True,
                    "ordinal": wave_def.ordinal,
                    "sha256": content_hash,
                    "physical_sha256": hashlib.sha256(
                        canonical_audit_bytes(content)
                    ).hexdigest(),
                    "receipt": receipt,
                    "completed_at": dt_str,
                    "latest_path": str(latest_path),
                    "history_path": str(history_path),
                    "meta": wave_meta,
                }

                all_waves = state["waves"]
                required_waves = [w for w in prof.waves if w.required]
                campaign_ready = all(w.id in all_waves and all_waves[w.id].get("complete") for w in required_waves)

                final_handoff_path: Optional[Path] = None
                canonical_campaign_path: Optional[Path] = None
                finalization_ok = False

                if campaign_ready:
                    parsed_dict = {
                        w.id: all_waves[w.id].get("meta", {}) for w in prof.waves if w.id in all_waves
                    }
                    try:
                        synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)
                        _write_final_artifacts(prof, synth_result, target_dir, history_dir,
                                               dt_str, state, resolved_name)
                        final_handoff_path = _get_final_handoff_path(prof, state)
                        canonical_campaign_path = _get_canonical_path(prof, state)
                        finalization_ok = True
                    except Exception as exc:
                        self.send_json(503, _rollback_error(snapshots, "finalization_failed", str(exc)))
                        return

                # Live campaign index — only writes COMPLETE when finalization
                # succeeded (CORE-002). On failure roll back and return retriable.
                completed_waves_list = [w.id for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")]
                next_w = prof.get_next_wave(wave_def.id)
                active_wid = None if campaign_ready else (next_w.id if next_w else None)
                c_status = STATUS_CAMPAIGN_COMPLETE if (campaign_ready and finalization_ok) else STATUS_CAMPAIGN_READY_FOR_WAVE

                parsed_waves_dict = {
                    wid: {
                        "wave_id": wid,
                        "status": "COMPLETE" if w_info.get("complete") else "IDLE",
                        "tickets": int(w_info.get("meta", {}).get("tickets", 0)),
                        "file": Path(w_info.get("latest_path", "")) if w_info.get("latest_path") else None,
                        "sha256": w_info.get("sha256", ""),
                        "completed_at": w_info.get("completed_at", dt_str),
                    }
                    for wid, w_info in all_waves.items()
                }
                try:
                    save_live_campaign_index(
                        campaign_root=target_dir,
                        profile=prof,
                        run_id=run_id,
                        project_name=resolved_name,
                        parsed_waves=parsed_waves_dict,
                        completed_waves=completed_waves_list,
                        active_wave_id=active_wid,
                        status=c_status,
                        final_handoff_path=final_handoff_path,
                    )
                except Exception as ex:
                    # W2-001 (SRC-041:R005): campaign-index failure rollback is
                    # UNCONDITIONAL for every wave. Non-final waves used to skip
                    # the rollback (finalization_ok False) and answer a clean
                    # retriable campaign_index_failed while the canonical latest
                    # and history files stayed published -- a durable half-commit.
                    self.send_json(503, _rollback_error(snapshots, "campaign_index_failed", str(ex)))
                    return

                # W2-002: persist the pending-publication marker as part of the primary
                # durable state commit BEFORE publishing generation, so recovery intent
                # survives a crash and duplicate retries can repair a missed publication.
                state["generation_pending"] = True
                try:
                    save_run_state(run_id, state)
                except RunStatePersistenceError as exc:
                    self.send_json(500, _rollback_error(snapshots, "run_state_persistence_failed", str(exc)))
                    return

            from audapack.bridge.state import publish_audit_generation
            generation_pending = True
            try:
                # W2-003: pass the resolved canonical project id so consumers can
                # refresh the exact project instead of falling back to name lookup.
                publish_audit_generation(resolved_name, wave_def.id, project_id=proj.id if proj else None)
                generation_pending = False
                state["generation_pending"] = False
                try:
                    save_run_state(run_id, state)
                except RunStatePersistenceError:
                    # Publication succeeded but clearing the marker failed: keep
                    # the marker so a duplicate retry repairs the clear (W2-002).
                    state["generation_pending"] = True
                    generation_pending = True
            except GenerationPersistenceError:
                # Marker already durable; duplicate retries repair publication.
                generation_pending = True

            if _ON_AUDIT_WRITTEN:
                try:
                    _ON_AUDIT_WRITTEN(resolved_name, wave_def.id)
                except Exception:
                    pass

            # Transport COMPLETE is downstream of the durable final commit.
            # If this request belongs to a browser dispatch, record terminal
            # proof only after campaign.json/final handoff were written and
            # validated above. A transient dispatcher failure leaves the job
            # FINALIZING for reconciliation; it must never turn disk success
            # into a false transport failure or vice versa.
            if campaign_ready and finalization_ok and final_handoff_path and proj:
                try:
                    self._dispatcher().complete_for_run(
                        str(proj.id),
                        run_id,
                        final_handoff_path,
                        campaign_path=target_dir / "campaign.json",
                        expected_wave_count=prof.wave_count,
                    )
                except BrowserDispatchError as exc:
                    logger.warning("dispatch finalization pending for %s/%s: %s", proj.id, run_id, exc)

            # The lane learns a campaign finished only from the worker's terminal
            # ACK, and that ACK is one HTTP call that can fail to arrive. Writing
            # the durable handoff IS the finish, and here the project, path and
            # digest are all known exactly.
            #
            # W2-001 (audit/1.md): this used to run INSIDE the finalization
            # block, before campaign.json and the run state were committed. A
            # failed index write rolled the handoff back off disk and left the
            # dispatch COMPLETE pointing at a file that no longer existed, with
            # its worker freed for the next audit. It belongs here, past the
            # commit, beside the proof-checked completion above.
            if campaign_ready and finalization_ok and final_handoff_path:
                try:
                    # W2-001 (audit/7.md): the request is canonically resolved
                    # once into target_proj; downstream lifecycle operations
                    # used to revert to the raw optional request project_id,
                    # which is None for a legal name-only v2 submission. Passing
                    # "" let complete_runs_for_project fall back to the display
                    # NAME, so a project whose display name equals another
                    # project's canonical id closed that unrelated live lane.
                    # Propagate the resolved id, never the raw payload.
                    resolved_pid = str(target_proj.id) if target_proj is not None else str(project_id or "")
                    self._dispatcher().complete_runs_for_project(
                        resolved_pid,
                        str(resolved_name or ""),
                        str(final_handoff_path),
                        hashlib.sha256(Path(final_handoff_path).read_bytes()).hexdigest(),
                        str(run_id or ""),
                    )
                except Exception as exc:
                    logger.warning("could not close dispatch lanes for %s: %s", resolved_name, exc)

            # Put the finished audit where an agent working inside the repo will
            # find it, without it having to know the audit root. Best effort: a
            # failed copy must never fail a run whose artifacts are already
            # durable in the central root -- and it runs after the commit, so a
            # rolled-back handoff is never mirrored into a project.
            if campaign_ready and finalization_ok:
                try:
                    from audapack.bridge.storage import mirror_project_audits

                    mirror_project = live_registry.get_project_by_id(str(target_proj.id) if target_proj is not None else str(project_id or ""))
                    if mirror_project is not None:
                        mirror_project_audits(
                            live_cfg,
                            getattr(mirror_project, "source_path", ""),
                            target_dir,
                            final_handoff_path,
                        )
                except Exception as exc:
                    logger.warning("could not mirror audits into %s: %s", resolved_name, exc)

            files_written = [str(latest_path), str(history_path)]
            if campaign_ready and finalization_ok:
                if final_handoff_path:
                    files_written.append(str(final_handoff_path))
                if canonical_campaign_path and str(canonical_campaign_path) != str(final_handoff_path):
                    files_written.append(str(canonical_campaign_path))

            completed_count = len([w for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")])

            self.send_json(200, {
                "ok": True,
                "duplicate": False,
                "api_version": client_api,
                "run_id": run_id,
                "profile_id": prof.profile_id,
                "profile_version": prof.profile_version,
                "project_id": proj.id,
                "project": resolved_name,
                "group": proj.priority_group,
                "slot": proj.slot,
                "wave": wave_def.id,
                "wave_index": wave_def.ordinal,
                "wave_count": prof.wave_count,
                "completed_waves": completed_count,
                "total_waves": prof.wave_count,
                "campaign_ready": campaign_ready and finalization_ok,
                "all3_ready": campaign_ready and finalization_ok if prof.profile_id == "quick3" else state.get("all3_complete", False),
                "files": files_written,
                "history_dir": str(history_dir),
                "final_handoff_path": str(final_handoff_path) if final_handoff_path else "",
                "canonical_campaign_path": str(canonical_campaign_path) if canonical_campaign_path else "",
                "generation_pending": generation_pending,
            })

    # ------------------------------------------------------------------ #
    # Browser worker dispatcher (SRC-005)
    # ------------------------------------------------------------------ #

    def _handle_browser_poll(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        dispatcher = self._dispatcher()
        try:
            worker = dispatcher.register_worker(data)
            # Registration first, then renewal, then expiry: a worker that is
            # polling is alive, and its owned run must never be aged out by the
            # very request that proves it is still there.
            dispatcher.renew_owner_lease(worker.worker_id, str(data.get("dispatch_id") or ""))
            dispatcher.expire_leases()
            job = dispatcher.claim_job(worker.worker_id, data)
            wait_seconds = min(25.0, max(0.0, float(data.get("wait_seconds", 20))))
            wait_seconds = min(wait_seconds, dispatcher.max_poll_wait_seconds())
            if job is None and wait_seconds:
                with dispatcher._work_available:
                    deadline = time.monotonic() + wait_seconds
                    while job is None and time.monotonic() < deadline:
                        dispatcher._work_available.wait(timeout=max(0.0, deadline - time.monotonic()))
                        dispatcher.expire_leases()
                        job = dispatcher.claim_job(worker.worker_id, data)
        except (BrowserDispatchError, TypeError, ValueError) as exc:
            if isinstance(exc, BrowserDispatchError):
                self._dispatch_error(400, exc)
            else:
                self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "wait_seconds must be numeric", "retriable": False}})
            return
        if job is None:
            owned = dispatcher.get_owned_job(worker.worker_id)
            self.send_json(200, {
                "ok": True,
                "job": None,
                "owned_job": {
                    "dispatch_id": owned.dispatch_id,
                    "state": owned.state,
                    # A restart marks a live post-START run BLOCKED pending
                    # same-worker reconciliation. The worker must be able to
                    # tell that apart from a terminal block, or it drops the
                    # very lease identity reconciliation needs.
                    "recovery_state": owned.recovery_state,
                    # The worker's own "held, not failed" reason for this lane.
                    "attention": owned.attention,
                    "campaign_run_id": owned.campaign_run_id,
                    "lease_id": owned.lease_id,
                    "project_id": owned.project_id,
                    "project_name": owned.project_name,
                    "archive_filename": owned.archive_filename,
                    "archive_size": owned.archive_size,
                    "archive_sha256": owned.archive_sha256,
                    "profile": owned.requested_profile,
                } if owned else None,
                "worker_state": worker.state,
                # A window cannot see its own build verdict, and a stale build
                # can never claim. Told plainly, it reloads itself and picks the
                # new script up instead of idling in the pool forever -- but
                # only once per build: a manager with nothing newer to hand
                # back turns this into a reload every two minutes.
                "worker_widget_stale": dispatcher.should_ask_widget_reload(worker),
                "required_widget_build": _get_required_widget_build(),
                "status": dispatcher.status(),
            })
            return
        self.send_json(200, {
            "ok": True,
            "worker_state": worker.state,
            "worker_widget_stale": dispatcher.should_ask_widget_reload(worker),
            "required_widget_build": _get_required_widget_build(),
            "status": dispatcher.status(),
            "job": {
                "dispatch_id": job.dispatch_id,
                "project_id": job.project_id,
                "project_name": job.project_name,
                "archive_filename": job.archive_filename,
                "archive_size": job.archive_size,
                "archive_sha256": job.archive_sha256,
                "profile": job.requested_profile,
                "lease_id": job.lease_id,
                "lease_expires_at": job.lease_expires_at,
            },
        })

    def _handle_browser_state(self, path: str) -> None:
        data = self._read_json_body()
        if data is None:
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) != 5 or parts[2] != "jobs" or parts[4] != "state":
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return
        path_dispatch_id = parts[3]
        dispatch_id = str(data.get("dispatch_id") or "").strip()
        if dispatch_id != path_dispatch_id:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "dispatch_id must match URL", "retriable": False}})
            return
        worker_id = str(data.get("worker_id") or "").strip()
        lease_id = str(data.get("lease_id") or "").strip()
        to_state = str(data.get("state") or "").strip()
        if not (dispatch_id and worker_id and lease_id and to_state):
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "dispatch_id/worker_id/lease_id/state are required", "retriable": False}})
            return
        dispatcher = self._dispatcher()
        try:
            job = dispatcher.transition_job(dispatch_id, worker_id, lease_id, to_state, payload=data)
        except BrowserDispatchError as exc:
            self.send_json(400, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})
            return
        self.send_json(200, {
            "ok": True,
            "job": {
                "dispatch_id": job.dispatch_id,
                "state": job.state,
                "lease_expires_at": job.lease_expires_at,
                "campaign_run_id": job.campaign_run_id,
                "conversation_id": job.conversation_id,
                "final_handoff_path": job.final_handoff_path,
                "final_handoff_sha256": job.final_handoff_sha256,
                "completed_at": job.completed_at,
            },
        })

    def _handle_browser_cancel(self, path: str) -> None:
        """Operator-initiated cancel of a pre-start/queued/blocked dispatch.

        Desktop caller (no worker/lease identity) may cancel any job that has
        not crossed the START_PREPARED boundary, plus BLOCKED pre-start jobs.
        """
        parts = [p for p in path.split("/") if p]
        if len(parts) != 5 or parts[2] != "jobs" or parts[4] != "cancel":
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return
        dispatch_id = parts[3]
        dispatcher = self._dispatcher()
        try:
            dispatcher.cancel_job(dispatch_id)
        except BrowserDispatchError as exc:
            self.send_json(400, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})
            return
        self.send_json(200, {"ok": True, "dispatch_id": dispatch_id})

    def _handle_browser_abandon(self, path: str) -> None:
        """Operator escape hatch for a stuck BLOCKED dispatch.

        Cancel refuses post-start BLOCKED work because CANCELLED asserts no
        Core was sent. Abandon instead marks the run terminal FAILED with an
        honest operator_abandoned code, which frees the project lane without
        ever re-leasing the dispatch or issuing a second START.
        """
        parts = [p for p in path.split("/") if p]
        if len(parts) != 5 or parts[2] != "jobs" or parts[4] != "abandon":
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return
        data = self._read_json_body() if self.headers.get("Content-Length") else {}
        if data is None:
            return
        dispatch_id = parts[3]
        dispatcher = self._dispatcher()
        try:
            job = dispatcher.abandon_job(dispatch_id, str((data or {}).get("reason") or ""))
        except BrowserDispatchError as exc:
            self.send_json(400, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})
            return
        self.send_json(200, {"ok": True, "dispatch_id": dispatch_id, "state": job.state, "error": job.error})

    def _handle_browser_reorder(self, path: str) -> None:
        """Move a waiting job up or down the line for the next freed window.

        The pool already holds more jobs than there are windows and a window
        that frees up claims the next one; this is the say in WHICH one, which
        was otherwise strictly the order START happened to be pressed in.
        """
        parts = [p for p in path.split("/") if p]
        if len(parts) != 5 or parts[2] != "jobs" or parts[4] != "reorder":
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return
        data = self._read_json_body() if self.headers.get("Content-Length") else {}
        if data is None:
            return
        try:
            delta = int((data or {}).get("delta", 0))
        except (TypeError, ValueError):
            self.send_json(400, {"ok": False, "error": {"code": "invalid_delta", "message": "delta must be an integer"}})
            return
        dispatcher = self._dispatcher()
        try:
            order = dispatcher.reorder_job(parts[3], delta)
        except BrowserDispatchError as exc:
            self.send_json(400, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})
            return
        self.send_json(200, {"ok": True, "dispatch_id": parts[3], "order": order})

    def _handle_browser_submit(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        dispatcher = self._dispatcher()
        try:
            self._validate_browser_submission(data)
            job = dispatcher.enqueue_job(data)
        except BrowserDispatchError as exc:
            self.send_json(400, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})
            return
        self.send_json(200, {"ok": True, "dispatch": {"dispatch_id": job.dispatch_id, "state": job.state, "status": dispatcher.status()}})

    def _validate_browser_submission(self, data: dict) -> None:
        """Bind submitted artifacts to the registered project pack contract.

        PERF-001: this is where the queue-time archive digest is ESTABLISHED,
        exactly once, and written back into ``data`` so the queued job carries
        the Bridge's own proof. The GUI used to hash the same ZIP first merely
        to be told what the Bridge was about to work out; a normal delivery read
        the whole archive twice for that one fact. A client-supplied
        ``archive_sha256`` from an older caller is still compared -- it can only
        ever add a rejection, never substitute for the Bridge's own read.
        """
        project_id = str(data.get("project_id") or "").strip()
        archive_raw = str(data.get("archive_path") or "").strip()
        if not project_id or not archive_raw:
            return
        cfg = self.get_live_config()
        registry = ProjectRegistry(cfg)
        project = registry.get_project_by_id(project_id)
        # Test/minimal Bridge configurations may have no registered projects;
        # domain-level path and digest checks still apply in that mode.
        if project is None and getattr(cfg, "projects", None):
            raise BrowserDispatchError("unknown_project", "project_id is not registered", retriable=False)
        submitted = Path(archive_raw).resolve()
        if project is not None:
            if not project.enabled or not project.source_path:
                raise BrowserDispatchError("ineligible_project", "project is disabled or has no source path", retriable=False)
            output_dir = resolve_output_dir(
                project.source_path,
                cfg.packing,
                fallback=Path.cwd(),
                group=project.priority_group,
                project=project,
            )
            expected = find_archive_for_project(project, output_dir)
            if expected is None or submitted != expected.resolve():
                raise BrowserDispatchError("artifact_ownership", "archive is not the canonical project pack artifact", retriable=False)
        try:
            stat = submitted.stat()
        except OSError as exc:
            raise BrowserDispatchError("missing_archive", str(exc), retriable=False) from exc
        declared_size = int(data.get("archive_size") or 0)
        declared_hash = str(data.get("archive_sha256") or "").strip().lower()
        if declared_size and declared_size != stat.st_size:
            raise BrowserDispatchError("changed_archive", "archive size does not match submission", retriable=False)
        queue_sha = self._sha256_path(submitted)
        if declared_hash and declared_hash != queue_sha:
            raise BrowserDispatchError("changed_archive", "archive digest does not match submission", retriable=False)
        data["archive_sha256"] = queue_sha

    @staticmethod
    def _sha256_path(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _handle_widget_diagnostics(self) -> None:
        """Persist widget diagnostics so nobody has to copy/paste them by hand.

        The widget's own log lives in browser storage, which is unreadable from
        outside the browser. Mirroring it into the Bridge runtime directory is
        what turns "please send me the log" into a file anyone can read.
        """
        data = self._read_json_body()
        if data is None:
            return
        entries = data.get("entries")
        if not isinstance(entries, list):
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "entries must be a list", "retriable": False}})
            return
        try:
            log_dir = Path(self.get_custom_base_dir() or get_user_runtime_dir()) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / "widget_diagnostics.log"
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as stream:
                for entry in entries[:200]:
                    if not isinstance(entry, dict):
                        continue
                    stream.write(json.dumps(entry, ensure_ascii=False)[:4000])
                    stream.write(chr(10))
        except OSError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "log_unwritable", "message": str(exc), "retriable": True}})
            return
        self.send_json(200, {"ok": True, "written": min(len(entries), 200)})

    def _serve_artifact(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        if len(parts) != 5 or parts[2] != "jobs" or parts[4] != "artifact":
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})
            return
        dispatch_id = parts[3]
        worker_id = str(self.headers.get("X-Worker-Id") or "").strip()
        lease_id = str(self.headers.get("X-Lease-Id") or "").strip()
        if not (worker_id and lease_id):
            self.send_json(403, {"ok": False, "error": {"code": "invalid_auth", "message": "X-Worker-Id and X-Lease-Id headers are required", "retriable": False}})
            return
        dispatcher = self._dispatcher()
        try:
            archive = dispatcher.resolve_artifact(dispatch_id, worker_id, lease_id)
        except BrowserDispatchError as exc:
            self.send_json(400, {"ok": False, "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}})
            return
        if archive is None:
            self.send_json(404, {"ok": False, "error": {"code": "unknown_job", "message": "dispatch_id is unknown", "retriable": False}})
            return
        try:
            size = archive.stat().st_size
        except OSError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "archive_unreadable", "message": str(exc), "retriable": True}})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{archive.name}"')
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "X-ACB-Token, X-Worker-Id, X-Lease-Id")
            self.end_headers()
            with archive.open("rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, BrokenPipeError) as exc:
            logger.warning("artifact stream interrupted: %s", exc)


def run_bridge_server(config: AppConfig) -> int:
    """Runs the AUDAPACK Bridge HTTP daemon on configured loopback host/port."""
    host = normalize_bridge_host(config.bridge.host)
    port = config.bridge.port

    class HandlerWithConfig(AudapackBridgeHandler):
        pass

    HandlerWithConfig.config = config

    # Claim the port BEFORE touching dispatch state. BrowserDispatcher's
    # constructor rewrites jobs.json -- every live post-START run becomes
    # BLOCKED "Bridge restarted after START_PREPARED" -- so a second Bridge
    # process that was only ever going to lose the bind used to destroy the
    # running instance's in-flight audits on its way out. A loser now exits
    # having read and written nothing.
    try:
        server = ThreadingHTTPServer((host, port), HandlerWithConfig)
    except OSError as exc:
        print(f"Error starting AUDAPACK Bridge: Port {port} on {host} already in use or unavailable: {exc}", file=sys.stderr)
        return 1

    dispatcher = BrowserDispatcher(
        dedicated_profile_only=bool(getattr(config.audits, "dedicated_profile_only", False)),
    )
    HandlerWithConfig.set_browser_dispatcher(dispatcher)

    def _campaign_probe(project_id: str, project_name: str) -> dict[str, Any]:
        """Ask the audit index whether this project's campaign is finished.

        The dispatcher cannot answer it: campaign.json lives beside the audit
        artifacts, not under the dispatch state dir, and the saved run id may
        differ from the one the dispatch recorded.

        The answer carries WHEN that handoff was written. Every project audited
        even once has a complete campaign on disk forever, so "this project has
        a finished audit" closes any lane at all -- observed live: six fresh
        dispatches went COMPLETE within a minute of START against handoff files
        from the previous day, and not one of those audits ever ran.
        """
        from audapack.services.audit_service import AuditService

        snapshot = AuditService(config).refresh_project(str(project_id or ""))
        if snapshot is None or not snapshot.campaign_complete or not snapshot.final_handoff_ready:
            return {"complete": False}
        if snapshot.final_handoff_path is None or not Path(snapshot.final_handoff_path).is_file():
            return {"complete": False}
        try:
            written_at = float(Path(snapshot.final_handoff_path).stat().st_mtime)
        except OSError:
            return {"complete": False}
        return {
            "complete": True,
            "handoff_path": str(snapshot.final_handoff_path),
            "handoff_sha256": str(snapshot.final_handoff_sha256 or ""),
            "campaign_run_id": str(snapshot.campaign_run_id or ""),
            "handoff_written_at": written_at,
        }

    dispatcher.set_campaign_probe(_campaign_probe)

    write_pid()
    print(f"AUDAPACK Bridge listening on http://{host}:{port}")
    # Queued audit work used to move only while a browser was polling and
    # only while the desktop app was open. The Bridge outlives both, so it
    # owns lease expiry and managed-worker provisioning from here on.
    supervisor = None
    try:
        from audapack.bridge.supervisor import DispatchSupervisor

        supervisor = DispatchSupervisor(HandlerWithConfig.browser_dispatcher)
        supervisor.start()
    except Exception as exc:
        logger.warning("dispatch supervisor did not start: %s", exc)
    HandlerWithConfig.set_dispatch_supervisor(supervisor)
    prepared_worker = None
    try:
        from audapack.prepared_worker import PreparedWorker

        # The prepared AUDIT runtime is optional infrastructure: a failure to
        # build it must never take down limit probing, ON TIME, ON RESET or
        # ordinary CLI prepared jobs. Those keep working; AUDIT jobs then show
        # the exact "runtime not connected" wait reason instead.
        audit_runtime = None
        try:
            from audapack.prepared_audit import build_headless_audit_runtime

            audit_runtime = build_headless_audit_runtime(config)
        except Exception as exc:
            logger.warning("prepared audit runtime unavailable: %s", type(exc).__name__)
        prepared_worker = PreparedWorker(config, audit_runtime=audit_runtime)
        prepared_worker.start()
        HandlerWithConfig.prepared_worker = prepared_worker
    except Exception as exc:
        logger.warning("prepared scheduler did not start: %s", type(exc).__name__)
    # W2-011: prune expired history on startup (best-effort, non-blocking).
    try:
        from audapack.bridge.storage import prune_audit_history
        removed = prune_audit_history(config)
        if removed:
            logger.info(f"Pruned {removed} expired history run(s) from audit root")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # W2-001 (audit/12.md): a prepared delivery can LAUNCH a browser and
        # commit a receipt, so it is the last thing that may still be acting.
        # `stop()` now returns whether this worker is genuinely quiescent; if it
        # is not, the server stays up (and the PID file stays) so the still-owned
        # work has a live Bridge behind it, instead of orphaning a window against
        # a Bridge that no longer exists. The process then exits non-zero so the
        # caller can tell a clean stop from an unfinished one.
        prepared_quiescent = True
        if prepared_worker is not None:
            prepared_quiescent = bool(prepared_worker.stop())
            if not prepared_quiescent:
                logger.warning("prepared worker still owns work at shutdown; "
                               "keeping the Bridge up until it is quiescent")
        if supervisor is not None:
            # W2-006: the supervisor may be mid-pass, and a pass can LAUNCH a
            # browser window. Finish its shutdown before the server and the PID
            # file go, or a window opens against a Bridge that no longer exists.
            if not supervisor.stop():
                logger.warning("bridge supervisor was still running at shutdown")
        quiescent_deadline = time.monotonic() + PREPARED_QUIESCENCE_WAIT_SECONDS
        while not prepared_quiescent and time.monotonic() < quiescent_deadline:
            prepared_quiescent = bool(prepared_worker.stop())
        if prepared_quiescent:
            server.server_close()
            remove_pid(expected_pid=os.getpid(), expected_nonce=INSTANCE_NONCE)
        else:
            logger.error("prepared work never reached quiescence; leaving the "
                         "Bridge socket and PID file in place")
    return 0 if prepared_quiescent else 1
