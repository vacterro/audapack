"""Audit campaign engine domain models, canonical profile loader, and validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union


@dataclass
class WaveDefinition:
    id: str
    ordinal: int
    number: str
    slug: str
    title: str
    short_label: str
    description: str
    ticket_prefix: str
    wave_header: str
    terminal_status_key: str
    status_line: str
    done_marker: str
    prompt_focus: str
    prompt_output_contract: str
    depends_on: list[str] = field(default_factory=list)
    required: bool = True
    max_partial_continuations: int = 5
    max_stall_recoveries: int = 3
    max_retry_clicks: int = 2
    max_continue_generating_clicks: int = 3
    synthesis_role: str = "standard"
    finalizer: bool = False
    ticket_fields: list[str] = field(default_factory=lambda: ["EVIDENCE", "DEFECT", "REPAIR", "VERIFY"])
    no_findings_marker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ordinal": self.ordinal,
            "number": self.number,
            "slug": self.slug,
            "title": self.title,
            "short_label": self.short_label,
            "description": self.description,
            "ticket_prefix": self.ticket_prefix,
            "wave_header": self.wave_header,
            "terminal_status_key": self.terminal_status_key,
            "status_line": self.status_line,
            "done_marker": self.done_marker,
            "prompt_focus": self.prompt_focus,
            "prompt_output_contract": self.prompt_output_contract,
            "depends_on": self.depends_on,
            "required": self.required,
            "max_partial_continuations": self.max_partial_continuations,
            "max_stall_recoveries": self.max_stall_recoveries,
            "max_retry_clicks": self.max_retry_clicks,
            "max_continue_generating_clicks": self.max_continue_generating_clicks,
            "synthesis_role": self.synthesis_role,
            "finalizer": self.finalizer,
            "ticket_fields": self.ticket_fields,
            "no_findings_marker": self.no_findings_marker,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaveDefinition:
        return cls(
            id=str(data.get("id", "")).strip().lower(),
            ordinal=int(data.get("ordinal", 1)),
            number=str(data.get("number", "")).strip(),
            slug=str(data.get("slug", "")).strip(),
            title=str(data.get("title", "")).strip(),
            short_label=str(data.get("short_label", "")).strip(),
            description=str(data.get("description", "")).strip(),
            ticket_prefix=str(data.get("ticket_prefix", "")).strip(),
            wave_header=str(data.get("wave_header", "")).strip(),
            terminal_status_key=str(data.get("terminal_status_key", "")).strip(),
            status_line=str(data.get("status_line", "")).strip(),
            done_marker=str(data.get("done_marker", "")).strip(),
            prompt_focus=str(data.get("prompt_focus", "")).strip(),
            prompt_output_contract=str(data.get("prompt_output_contract", "")).strip(),
            depends_on=[str(x).strip().lower() for x in data.get("depends_on", [])],
            required=bool(data.get("required", True)),
            max_partial_continuations=int(data.get("max_partial_continuations", 5)),
            max_stall_recoveries=int(data.get("max_stall_recoveries", 3)),
            max_retry_clicks=int(data.get("max_retry_clicks", 2)),
            max_continue_generating_clicks=int(data.get("max_continue_generating_clicks", 3)),
            synthesis_role=str(data.get("synthesis_role", "standard")).strip(),
            finalizer=bool(data.get("finalizer", False)),
            ticket_fields=[str(x).strip() for x in data.get("ticket_fields", ["EVIDENCE", "DEFECT", "REPAIR", "VERIFY"])],
            no_findings_marker=str(data.get("no_findings_marker", "")).strip(),
        )


@dataclass
class CampaignProfile:
    profile_id: str
    profile_version: str
    display_name: str
    description: str
    waves: list[WaveDefinition]
    finalizer_wave_id: str
    manifest_hash: str = ""

    def get_wave_by_id(self, wave_id: str) -> Optional[WaveDefinition]:
        norm = str(wave_id or "").strip().lower()
        for w in self.waves:
            if w.id == norm:
                return w
        return None

    def get_wave_by_ordinal(self, ordinal: int) -> Optional[WaveDefinition]:
        for w in self.waves:
            if w.ordinal == ordinal:
                return w
        return None

    def get_wave_by_number(self, num_str: str) -> Optional[WaveDefinition]:
        clean = str(num_str).zfill(2)
        for w in self.waves:
            if w.number == clean:
                return w
        return None

    def get_next_wave(self, current_wave_id: str) -> Optional[WaveDefinition]:
        current = self.get_wave_by_id(current_wave_id)
        if not current:
            return self.waves[0] if self.waves else None
        next_ord = current.ordinal + 1
        return self.get_wave_by_ordinal(next_ord)

    def get_prerequisites(self, wave_id: str) -> list[WaveDefinition]:
        w = self.get_wave_by_id(wave_id)
        if not w:
            return []
        if w.finalizer or w.synthesis_role == "finalizer":
            return [other for other in self.waves if other.id != w.id]
        prereqs = []
        for dep in w.depends_on:
            dep_def = self.get_wave_by_id(dep)
            if dep_def and dep_def not in prereqs:
                prereqs.append(dep_def)
        return prereqs

    @property
    def wave_count(self) -> int:
        return len(self.waves)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "display_name": self.display_name,
            "description": self.description,
            "finalizer_wave_id": self.finalizer_wave_id,
            "manifest_hash": self.manifest_hash,
            "waves": [w.to_dict() for w in self.waves],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], manifest_hash: str = "") -> CampaignProfile:
        waves_raw = data.get("waves", [])
        waves = [WaveDefinition.from_dict(w) for w in waves_raw]
        return cls(
            profile_id=str(data.get("profile_id", "")).strip().lower(),
            profile_version=str(data.get("profile_version", "1.0.0")).strip(),
            display_name=str(data.get("display_name", "")).strip(),
            description=str(data.get("description", "")).strip(),
            waves=waves,
            finalizer_wave_id=str(data.get("finalizer_wave_id", "")).strip().lower(),
            manifest_hash=manifest_hash,
        )


@dataclass
class WaveRunState:
    wave_id: str
    ordinal: int
    user_turn_id: str = ""
    receipt: str = ""
    status: str = "idle"
    continuation_count: int = 0
    stall_recovery_count: int = 0
    retry_count: int = 0
    continue_generating_count: int = 0
    result_sha256: str = ""
    bridge_saved_at: str = ""
    latest_path: str = ""
    history_path: str = ""
    predecessor_sha256: str = ""
    completed_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave_id": self.wave_id,
            "ordinal": self.ordinal,
            "user_turn_id": self.user_turn_id,
            "receipt": self.receipt,
            "status": self.status,
            "continuation_count": self.continuation_count,
            "stall_recovery_count": self.stall_recovery_count,
            "retry_count": self.retry_count,
            "continue_generating_count": self.continue_generating_count,
            "result_sha256": self.result_sha256,
            "bridge_saved_at": self.bridge_saved_at,
            "latest_path": self.latest_path,
            "history_path": self.history_path,
            "predecessor_sha256": self.predecessor_sha256,
            "completed_at": self.completed_at,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaveRunState:
        return cls(
            wave_id=str(data.get("wave_id", "")).strip().lower(),
            ordinal=int(data.get("ordinal", 1)),
            user_turn_id=str(data.get("user_turn_id", "")).strip(),
            receipt=str(data.get("receipt", "")).strip(),
            status=str(data.get("status", "idle")).strip(),
            continuation_count=int(data.get("continuation_count", 0)),
            stall_recovery_count=int(data.get("stall_recovery_count", 0)),
            retry_count=int(data.get("retry_count", 0)),
            continue_generating_count=int(data.get("continue_generating_count", 0)),
            result_sha256=str(data.get("result_sha256", "")).strip(),
            bridge_saved_at=str(data.get("bridge_saved_at", "")).strip(),
            latest_path=str(data.get("latest_path", "")).strip(),
            history_path=str(data.get("history_path", "")).strip(),
            predecessor_sha256=str(data.get("predecessor_sha256", "")).strip(),
            completed_at=str(data.get("completed_at", "")).strip(),
            meta=dict(data.get("meta", {})),
        )


@dataclass
class CampaignRunState:
    run_id: str
    profile_id: str
    profile_version: str
    manifest_hash: str
    project_id: str = ""
    project_name: str = ""
    target_identity: str = ""
    baseline_identity: str = ""
    current_wave_index: int = 0
    current_wave_id: str = ""
    state: str = "idle"
    wave_states: dict[str, WaveRunState] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    complete_at: str = ""
    history_dir: str = ""
    final_handoff_path: str = ""
    canonical_campaign_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "manifest_hash": self.manifest_hash,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "target_identity": self.target_identity,
            "baseline_identity": self.baseline_identity,
            "current_wave_index": self.current_wave_index,
            "current_wave_id": self.current_wave_id,
            "state": self.state,
            "wave_states": {k: v.to_dict() for k, v in self.wave_states.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "complete_at": self.complete_at,
            "history_dir": self.history_dir,
            "final_handoff_path": self.final_handoff_path,
            "canonical_campaign_path": self.canonical_campaign_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CampaignRunState:
        raw_waves = data.get("wave_states", {})
        wave_states = {k: WaveRunState.from_dict(v) for k, v in raw_waves.items()}
        return cls(
            run_id=str(data.get("run_id", "")).strip(),
            profile_id=str(data.get("profile_id", "quick3")).strip().lower(),
            profile_version=str(data.get("profile_version", "1.0.0")).strip(),
            manifest_hash=str(data.get("manifest_hash", "")).strip(),
            project_id=str(data.get("project_id", "")).strip(),
            project_name=str(data.get("project_name", "")).strip(),
            target_identity=str(data.get("target_identity", "")).strip(),
            baseline_identity=str(data.get("baseline_identity", "")).strip(),
            current_wave_index=int(data.get("current_wave_index", 0)),
            current_wave_id=str(data.get("current_wave_id", "")).strip(),
            state=str(data.get("state", "idle")).strip(),
            wave_states=wave_states,
            created_at=str(data.get("created_at", "")).strip(),
            updated_at=str(data.get("updated_at", "")).strip(),
            complete_at=str(data.get("complete_at", "")).strip(),
            history_dir=str(data.get("history_dir", "")).strip(),
            final_handoff_path=str(data.get("final_handoff_path", "")).strip(),
            canonical_campaign_path=str(data.get("canonical_campaign_path", "")).strip(),
        )


def get_canonical_manifest_path() -> Path:
    base = Path(__file__).resolve().parent
    path = base / "data" / "audit_profiles.json"
    if path.exists():
        return path
    root_path = base.parent / "audapack" / "data" / "audit_profiles.json"
    if root_path.exists():
        return root_path
    return path


def compute_manifest_hash(raw_text_or_dict: Any) -> str:
    if isinstance(raw_text_or_dict, str):
        try:
            parsed = json.loads(raw_text_or_dict)
            norm_json = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        except Exception:
            norm_json = raw_text_or_dict.strip()
    elif isinstance(raw_text_or_dict, dict):
        norm_json = json.dumps(raw_text_or_dict, sort_keys=True, separators=(",", ":"))
    else:
        norm_json = str(raw_text_or_dict)
    return hashlib.sha256(norm_json.encode("utf-8")).hexdigest()


def validate_profile_manifest(data: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Manifest must be a JSON object"
    schema_version = data.get("schema_version")
    if schema_version != 1:
        return False, f"Unsupported schema_version: {schema_version}, expected 1"
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return False, "Manifest must define a non-empty 'profiles' map"

    for pid, pdata in profiles.items():
        if not isinstance(pdata, dict):
            return False, f"Profile '{pid}' must be an object"
        p_id = str(pdata.get("profile_id", "")).strip().lower()
        if p_id != pid.lower():
            return False, f"Profile key '{pid}' does not match profile_id '{p_id}'"
        waves = pdata.get("waves")
        if not isinstance(waves, list) or not waves:
            return False, f"Profile '{pid}' must define at least one wave"

        wave_ids = set()
        ordinals = set()
        prefixes = set()

        for w in waves:
            if not isinstance(w, dict):
                return False, f"Profile '{pid}' has invalid wave entry"
            wid = str(w.get("id", "")).strip().lower()
            ord_num = w.get("ordinal")
            pfx = str(w.get("ticket_prefix", "")).strip()

            if not wid:
                return False, f"Profile '{pid}' wave missing 'id'"
            if wid in wave_ids:
                return False, f"Profile '{pid}' duplicate wave id: '{wid}'"
            wave_ids.add(wid)

            if not isinstance(ord_num, int) or ord_num <= 0:
                return False, f"Profile '{pid}' wave '{wid}' invalid ordinal: {ord_num}"
            if ord_num in ordinals:
                return False, f"Profile '{pid}' duplicate ordinal: {ord_num}"
            ordinals.add(ord_num)

            if not pfx:
                return False, f"Profile '{pid}' wave '{wid}' missing ticket_prefix"
            if pfx in prefixes:
                return False, f"Profile '{pid}' duplicate ticket_prefix: '{pfx}'"
            prefixes.add(pfx)

            for req in ["wave_header", "done_marker", "terminal_status_key", "status_line", "number", "slug"]:
                if not str(w.get(req, "")).strip():
                    return False, f"Profile '{pid}' wave '{wid}' missing required field: '{req}'"

        for w in waves:
            wid = w["id"].strip().lower()
            deps = w.get("depends_on", [])
            for d in deps:
                d_clean = str(d).strip().lower()
                if d_clean not in wave_ids:
                    return False, f"Profile '{pid}' wave '{wid}' depends on unknown wave: '{d_clean}'"

        finalizer_id = str(pdata.get("finalizer_wave_id", "")).strip().lower()
        if finalizer_id and finalizer_id not in wave_ids:
            return False, f"Profile '{pid}' finalizer_wave_id '{finalizer_id}' does not exist in waves"

    return True, ""


_PROFILES_CACHE: Optional[dict[str, CampaignProfile]] = None
_MANIFEST_HASH_CACHE: str = ""


def load_profiles(manifest_path: Optional[Path] = None, force_reload: bool = False) -> dict[str, CampaignProfile]:
    global _PROFILES_CACHE, _MANIFEST_HASH_CACHE
    if _PROFILES_CACHE is not None and not force_reload and manifest_path is None:
        return _PROFILES_CACHE

    target = manifest_path or get_canonical_manifest_path()
    if not target.exists():
        raise FileNotFoundError(f"Canonical audit profiles manifest not found at: {target}")

    raw_text = target.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    valid, err = validate_profile_manifest(data)
    if not valid:
        raise ValueError(f"Invalid audit profiles manifest: {err}")

    m_hash = compute_manifest_hash(data)
    profiles: dict[str, CampaignProfile] = {}
    for pid, pdata in data["profiles"].items():
        profiles[pid.lower()] = CampaignProfile.from_dict(pdata, manifest_hash=m_hash)

    if manifest_path is None:
        _PROFILES_CACHE = profiles
        _MANIFEST_HASH_CACHE = m_hash

    return profiles


def get_profile(profile_id: str, manifest_path: Optional[Path] = None) -> CampaignProfile:
    profiles = load_profiles(manifest_path=manifest_path)
    norm = str(profile_id or "").strip().lower()
    if norm in profiles:
        return profiles[norm]
    profile_keys = list(profiles.keys())
    raise KeyError(f"Profile '{profile_id}' not found in canonical profiles: {profile_keys}")


def get_default_profile() -> CampaignProfile:
    return get_profile("quick3")


def get_canonical_manifest_hash() -> str:
    global _MANIFEST_HASH_CACHE
    if not _MANIFEST_HASH_CACHE:
        load_profiles()
    return _MANIFEST_HASH_CACHE


STATUS_CAMPAIGN_NOT_FOUND = "CAMPAIGN_NOT_FOUND"
STATUS_CAMPAIGN_PROFILE_UNKNOWN = "CAMPAIGN_PROFILE_UNKNOWN"
STATUS_CAMPAIGN_MANIFEST_MISMATCH = "CAMPAIGN_MANIFEST_MISMATCH"
STATUS_CAMPAIGN_RUN_CONFLICT = "CAMPAIGN_RUN_CONFLICT"
STATUS_CAMPAIGN_PROJECT_CONFLICT = "CAMPAIGN_PROJECT_CONFLICT"
STATUS_CAMPAIGN_DEPENDENCY_GAP = "CAMPAIGN_DEPENDENCY_GAP"
STATUS_CAMPAIGN_ARTIFACT_INVALID = "CAMPAIGN_ARTIFACT_INVALID"
STATUS_CAMPAIGN_BLOCKED = "CAMPAIGN_BLOCKED"
STATUS_CAMPAIGN_PARTIAL = "CAMPAIGN_PARTIAL"
STATUS_CAMPAIGN_READY_FOR_WAVE = "CAMPAIGN_READY_FOR_WAVE"
STATUS_CAMPAIGN_COMPLETE = "CAMPAIGN_COMPLETE"

POLICY_CAMPAIGN_DIRECTORY_READ_ALLOWED = "CAMPAIGN_DIRECTORY_READ_ALLOWED"

CAMPAIGN_CONTEXT_TEMPLATE = """CAMPAIGN CONTEXT

This audit belongs to a multi-wave {profile_display} campaign.

AUDIT_CAMPAIGN_CONTEXT_POLICY: CAMPAIGN_DIRECTORY_READ_ALLOWED
CAMPAIGN_PROFILE: {profile_id}
WAVE_COUNT: {wave_count}

The path supplied by the operator is an ENTRYPOINT to the campaign, not an
instruction to limit inspection to that single audit artifact.

You are explicitly authorized to READ all relevant files inside the owning
A10 campaign directory in order to reconstruct campaign state, consume prior
wave handoffs, recover interrupted continuations, validate dependencies,
avoid duplicate tickets, and perform final synthesis.

Do not ask the operator to paste prior A10 wave outputs when they already
exist inside the campaign directory.

Treat campaign files as read-only evidence except through the explicit
campaign result/update protocol.

Do not read sibling project audit directories merely because they share the
same parent audit root.

Determine the active wave from validated campaign state, not from the
ordinal/name of the entrypoint file."""


def build_campaign_context_header(profile: Optional[Union[str, CampaignProfile]] = None) -> str:
    if isinstance(profile, str):
        try:
            prof = get_profile(profile)
        except Exception:
            prof = get_profile("super10")
    elif isinstance(profile, CampaignProfile):
        prof = profile
    else:
        prof = get_profile("super10")
    return CAMPAIGN_CONTEXT_TEMPLATE.format(
        profile_display=prof.display_name or "A10 / Super10",
        profile_id=prof.profile_id,
        wave_count=prof.wave_count,
    )


def _extract_header_line(text: str, key: str) -> Optional[str]:
    anchor = key.upper() + ":"
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`*_# ")
        if line.upper().startswith(anchor):
            val = line[len(anchor):].strip().strip("`*_ ")
            if val:
                return val
    return None


def save_live_campaign_index(
    campaign_root: Path,
    profile: CampaignProfile,
    run_id: str,
    project_name: str,
    parsed_waves: dict[str, dict[str, Any]],
    completed_waves: list[str],
    active_wave_id: Optional[str],
    status: str,
    final_handoff_path: Optional[Path] = None,
) -> Path:
    """Writes / updates live campaign.json atomically inside campaign_root."""
    import os
    from datetime import datetime

    campaign_root = Path(campaign_root)
    campaign_root.mkdir(parents=True, exist_ok=True)
    manifest_hash = profile.manifest_hash or get_canonical_manifest_hash()
    now_iso = datetime.now().isoformat()

    active_wave_def = profile.get_wave_by_id(active_wave_id) if active_wave_id else None
    active_ordinal = active_wave_def.ordinal if active_wave_def else (len(completed_waves) + 1 if len(completed_waves) < profile.wave_count else profile.wave_count)

    waves_data = []
    total_tickets = 0
    for w in profile.waves:
        w_parsed = parsed_waves.get(w.id, {})
        w_status = w_parsed.get("status", "IDLE" if w.id not in completed_waves else "COMPLETE")
        w_tickets = int(w_parsed.get("tickets", 0))
        total_tickets += w_tickets
        rel_art = w_parsed.get("relative_path", "")
        if not rel_art and w_parsed.get("file"):
            try:
                rel_art = str(Path(w_parsed["file"]).relative_to(campaign_root)).replace("\\", "/")
            except Exception:
                rel_art = Path(w_parsed["file"]).name

        waves_data.append({
            "wave_id": w.id,
            "ordinal": w.ordinal,
            "number": w.number,
            "slug": w.slug,
            "title": w.title,
            "ticket_prefix": w.ticket_prefix,
            "terminal_status_key": w.terminal_status_key,
            "done_marker": w.done_marker,
            "required": w.required,
            "depends_on": w.depends_on,
            "artifact": rel_art,
            "status": w_status,
            "tickets": w_tickets,
            "result_sha256": w_parsed.get("sha256", ""),
            "predecessor_sha256": w_parsed.get("predecessor_sha256", ""),
            "completed_at": w_parsed.get("completed_at", now_iso if w_status == "COMPLETE" else ""),
        })

    final_rel = ""
    if final_handoff_path:
        try:
            final_rel = str(Path(final_handoff_path).relative_to(campaign_root)).replace("\\", "/")
        except Exception:
            final_rel = Path(final_handoff_path).name

    payload = {
        "schema_version": 1,
        "campaign_profile": profile.profile_id,
        "campaign_profile_version": profile.profile_version,
        "campaign_manifest_sha256": manifest_hash,
        "campaign_run_id": run_id,
        "project_name": project_name,
        "wave_count": profile.wave_count,
        "finalizer_wave_id": profile.finalizer_wave_id,
        "campaign_status": status,
        "current_wave_id": active_wave_id or (profile.waves[-1].id if status == STATUS_CAMPAIGN_COMPLETE else profile.waves[0].id),
        "current_wave_index": active_ordinal,
        "completed_count": len(completed_waves),
        "completed_waves": completed_waves,
        "total_tickets": total_tickets,
        "final_handoff": final_rel,
        "updated_at": now_iso,
        "waves": waves_data,
    }

    target_file = campaign_root / "campaign.json"
    content_str = json.dumps(payload, indent=2, ensure_ascii=False)

    # Safe atomic write
    tmp_path = campaign_root / f".campaign.json.tmp.{os.getpid()}.{hashlib.sha256(content_str.encode('utf-8')).hexdigest()[:6]}"
    with open(tmp_path, "wb") as f:
        f.write(content_str.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(target_file)
    return target_file


def resolve_audit_campaign_entrypoint(
    entry_path: Union[str, Path],
    manifest_path: Optional[Path] = None,
    auto_repair_index: bool = True,
) -> dict[str, Any]:
    """
    Authoritative discovery & reconstruction of an A10 / Super10 / Quick3 audit campaign
    from any arbitrary file or folder entrypoint.
    """
    if not entry_path:
        return {
            "status": STATUS_CAMPAIGN_NOT_FOUND,
            "ok": False,
            "message": "Empty entrypoint path provided.",
            "entry_artifact": None,
        }

    raw_path = Path(entry_path)
    try:
        resolved_entry = raw_path.resolve()
    except Exception as ex:
        return {
            "status": STATUS_CAMPAIGN_NOT_FOUND,
            "ok": False,
            "message": f"Could not resolve entrypoint path: {ex}",
            "entry_artifact": raw_path,
        }

    if not resolved_entry.exists():
        return {
            "status": STATUS_CAMPAIGN_NOT_FOUND,
            "ok": False,
            "message": f"Entrypoint path does not exist: {resolved_entry}",
            "entry_artifact": resolved_entry,
        }

    # Determine campaign root
    if resolved_entry.is_dir():
        campaign_root = resolved_entry
    else:
        parent = resolved_entry.parent
        if parent.name.startswith("_history") or parent.name == "_history":
            campaign_root = parent.parent
        elif parent.parent.name == "_history":
            campaign_root = parent.parent.parent
        else:
            campaign_root = parent

    # Boundary containment verification: Entrypoint must stay inside campaign root
    try:
        resolved_entry.relative_to(campaign_root)
    except ValueError:
        return {
            "status": STATUS_CAMPAIGN_NOT_FOUND,
            "ok": False,
            "message": f"Entrypoint {resolved_entry} escapes campaign root {campaign_root}",
            "entry_artifact": resolved_entry,
            "campaign_root": campaign_root,
        }

    # 1. Look for existing machine-readable index
    index_file = campaign_root / "campaign.json"
    legacy_index_file = None
    if not index_file.exists():
        for f in campaign_root.glob("*__00_SUPER_AUDIT_INDEX.json"):
            legacy_index_file = f
            break
        if not legacy_index_file:
            for f in campaign_root.glob("*_INDEX.json"):
                legacy_index_file = f
                break

    index_data: dict[str, Any] = {}
    if index_file.exists():
        try:
            index_data = json.loads(index_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            index_data = {}
    elif legacy_index_file and legacy_index_file.exists():
        try:
            index_data = json.loads(legacy_index_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            index_data = {}

    # 2. Discover all Markdown files directly in campaign root
    md_files = [f for f in campaign_root.glob("*.md") if f.is_file() and not f.name.startswith(".")]

    # 3. Detect profile
    profile_id = (
        index_data.get("campaign_profile")
        or index_data.get("profile_id")
        or ""
    ).strip().lower()

    if not profile_id:
        for f in md_files:
            text_snippet = f.read_text(encoding="utf-8", errors="replace")[:2048]
            m_prof = _extract_header_line(text_snippet, "CAMPAIGN_PROFILE")
            if m_prof:
                profile_id = m_prof.strip().lower()
                break

    if not profile_id:
        # Check markers
        all_md_names = " ".join(f.name.upper() for f in md_files)
        if any(f"_{num}_" in all_md_names for num in ["04", "05", "06", "07", "08", "09", "10"]) or "ARCHITECTURE" in all_md_names:
            profile_id = "super10"
        elif "CORE" in all_md_names or "ALL_3" in all_md_names:
            profile_id = "quick3"
        else:
            profile_id = "super10"

    try:
        profile = get_profile(profile_id, manifest_path=manifest_path)
    except KeyError:
        return {
            "status": STATUS_CAMPAIGN_PROFILE_UNKNOWN,
            "ok": False,
            "message": f"Unknown campaign profile '{profile_id}' for campaign at {campaign_root}",
            "campaign_root": campaign_root,
            "entry_artifact": resolved_entry,
        }

    expected_manifest_hash = profile.manifest_hash or get_canonical_manifest_hash()
    detected_runs: set[str] = set()
    detected_projects: set[str] = set()
    parsed_wave_artifacts: dict[str, dict[str, Any]] = {}

    # 4. Inspect wave files
    for f in md_files:
        if "__00_" in f.name or f.name.startswith("__00_"):
            continue

        content = f.read_text(encoding="utf-8", errors="replace")
        h_proj = _extract_header_line(content, "PROJECT_NAME")
        h_run = _extract_header_line(content, "CAMPAIGN_RUN_ID")
        h_manifest = _extract_header_line(content, "CAMPAIGN_MANIFEST_SHA256")
        h_wave_id = _extract_header_line(content, "WAVE_ID")
        h_status = _extract_header_line(content, "STATUS")
        h_tickets = _extract_header_line(content, "TICKETS")

        if h_proj:
            detected_projects.add(h_proj)
        if h_run:
            detected_runs.add(h_run)
        if h_manifest and h_manifest.lower() != expected_manifest_hash.lower():
            return {
                "status": STATUS_CAMPAIGN_MANIFEST_MISMATCH,
                "ok": False,
                "message": f"Artifact {f.name} declares manifest hash '{h_manifest[:12]}', expected '{expected_manifest_hash[:12]}'",
                "campaign_root": campaign_root,
                "entry_artifact": resolved_entry,
            }

        # Resolve wave definition
        wave_def = None
        if h_wave_id:
            wave_def = profile.get_wave_by_id(h_wave_id)
        if not wave_def:
            for w in profile.waves:
                if f"__{w.number}_{w.slug}" in f.name or f"__{w.id}" in f.name or f"_{w.number}_" in f.name:
                    wave_def = w
                    break
        if not wave_def:
            for w in profile.waves:
                if f"WAVE: {w.wave_header}" in content or w.status_line in content:
                    wave_def = w
                    break

        if wave_def:
            done_label = wave_def.done_marker.split(":")[0].strip().replace("_", " ")
            is_done = False
            for line in content.splitlines():
                l_norm = line.strip().rstrip("`").replace("_", " ")
                if l_norm.startswith(done_label) or done_label in l_norm:
                    is_done = True
                    break

            status_val = "UNKNOWN"
            if h_status:
                h_upper = h_status.upper()
                if "COMPLETE" in h_upper and is_done:
                    status_val = "COMPLETE"
                elif "PARTIAL" in h_upper:
                    status_val = "PARTIAL"
                elif "BLOCKED" in h_upper:
                    status_val = "BLOCKED"
            else:
                if "COMPLETE" in content and is_done:
                    status_val = "COMPLETE"

            parsed_wave_artifacts[wave_def.id] = {
                "wave_id": wave_def.id,
                "wave_def": wave_def,
                "file": f,
                "relative_path": str(f.relative_to(campaign_root)).replace("\\", "/"),
                "status": status_val,
                "tickets": int(h_tickets) if (h_tickets and h_tickets.isdigit()) else 0,
                "run_id": h_run or "",
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "full_text": content,
            }

    # 5. Check run conflicts
    if len(detected_runs) > 1:
        err_text = f"Multiple campaign run IDs mixed in {campaign_root}: {sorted(detected_runs)}"
        return {
            "status": STATUS_CAMPAIGN_RUN_CONFLICT,
            "ok": False,
            "message": err_text,
            "error": err_text,
            "campaign_root": campaign_root,
            "entry_artifact": resolved_entry,
            "runs": sorted(detected_runs),
        }

    if len(detected_projects) > 1:
        project_names = sorted(detected_projects)
        err_text = f"Multiple project names mixed in {campaign_root}: {project_names}"
        return {
            "status": STATUS_CAMPAIGN_PROJECT_CONFLICT,
            "ok": False,
            "message": err_text,
            "error": err_text,
            "campaign_root": campaign_root,
            "entry_artifact": resolved_entry,
            "project_names": project_names,
        }

    primary_run_id = list(detected_runs)[0] if detected_runs else (index_data.get("campaign_run_id") or "run_default")
    artifact_project_name = next(iter(detected_projects), "")
    indexed_project_name = str(index_data.get("project_name") or "").strip()
    if artifact_project_name and indexed_project_name and artifact_project_name != indexed_project_name:
        err_text = f"campaign.json project name '{indexed_project_name}' disagrees with artifact project name '{artifact_project_name}'"
        return {
            "status": STATUS_CAMPAIGN_PROJECT_CONFLICT,
            "ok": False,
            "message": err_text,
            "error": err_text,
            "campaign_root": campaign_root,
            "entry_artifact": resolved_entry,
            "project_name": artifact_project_name,
            "campaign_json_project_name": indexed_project_name,
            "project_names": [artifact_project_name, indexed_project_name],
        }
    project_name = artifact_project_name or indexed_project_name or campaign_root.name

    # 6. Pre-validate dependency DAG for all completed artifacts on disk
    for w in profile.waves:
        w_data = parsed_wave_artifacts.get(w.id)
        if w_data and w_data["status"] == "COMPLETE":
            for dep in w.depends_on:
                dep_data = parsed_wave_artifacts.get(dep)
                if not dep_data or dep_data.get("status") != "COMPLETE":
                    # Gap: Wave on disk is COMPLETE, but its prerequisite is missing or not COMPLETE!
                    dep_def = profile.get_wave_by_id(dep)
                    return {
                        "status": STATUS_CAMPAIGN_DEPENDENCY_GAP,
                        "ok": False,
                        "message": f"Wave '{w.id}' is COMPLETE on disk, but required dependency '{dep}' is not COMPLETE.",
                        "error": f"Wave '{w.id}' is COMPLETE on disk, but required dependency '{dep}' is not COMPLETE.",
                        "campaign_root": campaign_root,
                        "entry_artifact": resolved_entry,
                        "profile_id": profile.profile_id,
                        "run_id": primary_run_id,
                        "project_name": project_name,
                        "active_wave_id": dep,
                        "active_wave_index": dep_def.ordinal if dep_def else None,
                        "gap_wave_id": w.id,
                        "completed_waves": [wid for wid, d in parsed_wave_artifacts.items() if d.get("status") == "COMPLETE" and wid != w.id],
                        "completed_count": len([wid for wid, d in parsed_wave_artifacts.items() if d.get("status") == "COMPLETE" and wid != w.id]),
                        "next_action": "start_wave",
                    }

    # 7. Evaluate progression in canonical profile order
    completed_wave_ids: list[str] = []
    active_wave_id: Optional[str] = None
    campaign_status = STATUS_CAMPAIGN_READY_FOR_WAVE
    next_action = "start_wave"

    for w in profile.waves:
        w_data = parsed_wave_artifacts.get(w.id)
        missing_deps = [dep for dep in w.depends_on if dep not in completed_wave_ids]
        if missing_deps:
            active_wave_id = missing_deps[0]
            campaign_status = STATUS_CAMPAIGN_READY_FOR_WAVE
            next_action = "start_wave"
            break

        # Dependencies are satisfied:
        if w_data and w_data["status"] == "COMPLETE":
            completed_wave_ids.append(w.id)
            continue
        elif w_data and w_data["status"] == "PARTIAL":
            active_wave_id = w.id
            campaign_status = STATUS_CAMPAIGN_PARTIAL
            next_action = "resume_wave"
            break
        elif w_data and w_data["status"] == "BLOCKED":
            active_wave_id = w.id
            campaign_status = STATUS_CAMPAIGN_BLOCKED
            next_action = "blocked"
            break
        else:
            active_wave_id = w.id
            campaign_status = STATUS_CAMPAIGN_READY_FOR_WAVE
            next_action = "start_wave"
            break

    # 7. Check if all required waves are completed
    all_required_done = all(w.id in completed_wave_ids for w in profile.waves if w.required)
    final_handoff_file: Optional[Path] = None

    if all_required_done:
        if profile.profile_id == "quick3":
            final_candidate = campaign_root / f"{project_name}__00_AUDIT_ALL_3.md"
            if not final_candidate.exists():
                final_candidate = campaign_root / "__00_AUDIT_ALL_3.md"
        else:
            final_candidate = campaign_root / f"{project_name}__00_SUPER_AUDIT_FINAL.md"
            if not final_candidate.exists():
                final_candidate = campaign_root / "__00_SUPER_AUDIT_FINAL.md"
        final_handoff_file = final_candidate if final_candidate.exists() else None
        if final_handoff_file:
            campaign_status = STATUS_CAMPAIGN_COMPLETE
            next_action = "campaign_complete"
            active_wave_id = None
        else:
            campaign_status = STATUS_CAMPAIGN_ARTIFACT_INVALID
            next_action = "regenerate_final_artifact"
            active_wave_id = profile.finalizer_wave_id

    # 8. Collect prerequisite artifacts for active wave
    prereq_files: list[Path] = []
    if active_wave_id:
        prereq_defs = profile.get_prerequisites(active_wave_id)
        for pdef in prereq_defs:
            if pdef.id in parsed_wave_artifacts and parsed_wave_artifacts[pdef.id]["status"] == "COMPLETE":
                prereq_files.append(parsed_wave_artifacts[pdef.id]["file"])

    # 9. Update live campaign.json
    if auto_repair_index:
        try:
            save_live_campaign_index(
                campaign_root=campaign_root,
                profile=profile,
                run_id=primary_run_id,
                project_name=project_name,
                parsed_waves=parsed_wave_artifacts,
                completed_waves=completed_wave_ids,
                active_wave_id=active_wave_id,
                status=campaign_status,
                final_handoff_path=final_handoff_file,
            )
        except Exception as exc:
            return {
                "status": STATUS_CAMPAIGN_ARTIFACT_INVALID,
                "ok": False,
                "message": f"Failed to repair live campaign index: {exc}",
                "error": f"Failed to repair live campaign index: {exc}",
                "campaign_root": campaign_root,
                "entry_artifact": resolved_entry,
                "profile_id": profile.profile_id,
                "run_id": primary_run_id,
                "project_name": project_name,
                "active_wave_id": active_wave_id,
                "completed_waves": completed_wave_ids,
                "completed_count": len(completed_wave_ids),
                "next_action": "repair_campaign_index",
            }

    active_wave_def = profile.get_wave_by_id(active_wave_id) if active_wave_id else None

    return {
        "status": campaign_status,
        "ok": campaign_status in [STATUS_CAMPAIGN_READY_FOR_WAVE, STATUS_CAMPAIGN_PARTIAL, STATUS_CAMPAIGN_COMPLETE],
        "campaign_root": campaign_root,
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "manifest_hash": expected_manifest_hash,
        "run_id": primary_run_id,
        "project_name": project_name,
        "entry_artifact": resolved_entry,
        "active_wave_id": active_wave_id,
        "active_wave_ordinal": active_wave_def.ordinal if active_wave_def else None,
        "active_wave_index": active_wave_def.ordinal if active_wave_def else (len(profile.waves) if campaign_status == STATUS_CAMPAIGN_COMPLETE else None),
        "active_wave_def": active_wave_def,
        "prerequisite_artifacts": prereq_files,
        "completed_waves": completed_wave_ids,
        "completed_count": len(completed_wave_ids),
        "final_handoff_path": final_handoff_file,
        "next_action": next_action,
        "context_policy": POLICY_CAMPAIGN_DIRECTORY_READ_ALLOWED,
        "error": "",
        "message": f"Campaign '{project_name}' ({profile.profile_id}) state: {campaign_status}, active wave: {active_wave_id or 'none'}",
    }
