"""Audit campaign engine domain models, canonical profile loader, and validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


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
    if "quick3" in profiles:
        return profiles["quick3"]
    profile_keys = list(profiles.keys())
    raise KeyError(f"Profile '{profile_id}' not found in canonical profiles: {profile_keys}")


def get_default_profile() -> CampaignProfile:
    return get_profile("quick3")


def get_canonical_manifest_hash() -> str:
    global _MANIFEST_HASH_CACHE
    if not _MANIFEST_HASH_CACHE:
        load_profiles()
    return _MANIFEST_HASH_CACHE
