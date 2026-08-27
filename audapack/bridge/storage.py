"""Bridge audit storage engine, canonical file generator, and registry router."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from audapack.campaign import (
    CampaignProfile,
    get_canonical_manifest_hash,
    get_default_profile,
    get_profile,
    load_profiles,
)
from audapack.config import AppConfig
from audapack.models import Project
from audapack.projects import ProjectRegistry

# Legacy dictionary compatibility for existing imports
WAVES_CONFIG = {
    "core": {
        "number": "01",
        "slug": "AUDIT_CORE",
        "prefix": "CORE-",
        "done_marker": "CORE_DONE_WHEN:",
        "wave_header": "AUDIT CORE",
        "status_line": "STATUS: AUDIT_CORE: COMPLETE",
    },
    "second": {
        "number": "02",
        "slug": "AUDIT_SECOND_WAVE",
        "prefix": "W2-",
        "done_marker": "SECOND_WAVE_DONE_WHEN:",
        "wave_header": "AUDIT SECOND WAVE",
        "status_line": "STATUS: SECOND_WAVE: COMPLETE",
    },
    "performance": {
        "number": "03",
        "slug": "AUDIT_PERFORMANCE",
        "prefix": "PERF-",
        "done_marker": "PERFORMANCE_DONE_WHEN:",
        "wave_header": "AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS",
        "status_line": "STATUS: PERFORMANCE: COMPLETE",
    },
}

_RE_CLEAN_HEADER1 = re.compile(r"^\*{1,2}([A-Za-z0-9_]+):\*{0,2}\s*")
_RE_CLEAN_HEADER2 = re.compile(r"^\*{1,2}([A-Za-z0-9_]+)\*{1,2}:\s*")
_RE_SANITIZE_PROJ = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def sanitize_project_name(name: str, max_length: int = 80) -> str:
    if not name or not isinstance(name, str):
        return "UNKNOWN_PROJECT"
    name = _RE_SANITIZE_PROJ.sub("_", name.strip())
    name = name.strip(" .")
    if not name:
        return "UNKNOWN_PROJECT"
    reserved = ("CON", "PRN", "AUX", "NUL") + tuple(f"COM{i}" for i in range(1, 10)) + tuple(f"LPT{i}" for i in range(1, 10))
    if name.upper() in reserved:
        name = name + "_"
    return name[:max_length].strip(" .") or "UNKNOWN_PROJECT"


class InvalidProjectPathError(ValueError):
    """Permanent logical error: resolved audit destination escapes the configured audit root."""


def ensure_contained(path: Path, root: Path) -> Path:
    """Final filesystem-boundary defense: resolve ``path`` and prove it stays inside ``root``.

    Sanitization alone is not trusted; every physical audit destination must pass this
    gate before any directory is created or any file is written. Raises
    InvalidProjectPathError when containment fails.
    """
    resolved_root = Path(root).resolve()
    resolved_path = Path(path).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as err:
        raise InvalidProjectPathError(
            f"Resolved destination {resolved_path} escapes the configured audit root {resolved_root}"
        ) from err
    return resolved_path


def atomic_write(filepath: Path, content: str):
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = filepath.parent / f".{filepath.name}.tmp.{os.getpid()}.{hashlib.sha256(content.encode('utf-8')).hexdigest()[:6]}"
    try:
        with open(tmp_path, "wb") as f:
            norm_content = content.replace("\r\n", "\n")
            f.write(norm_content.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(filepath)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def resolve_project_audit_dir(
    config: AppConfig,
    raw_project_name: str,
    project_id: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> tuple[Path, str, Project, bool]:
    """
    Resolves destination audit folder using canonical ProjectRegistry:
    1. If project_id is provided, lookup in current registry.
    2. If not found or not provided, resolves or auto-registers the project into SIDE1+.
    3. Re-resolves current placement from disk to guarantee live placement ownership.
    Returns: (target_dir, filesystem_safe_artifact_name, project, was_created)
    """
    out_root = Path(config.audits.root).resolve()
    registry = ProjectRegistry(config, base_dir=base_dir)

    proj: Optional[Project] = None
    was_created = False

    if project_id:
        proj = registry.get_project_by_id(project_id)

    if not proj:
        proj, was_created = registry.resolve_or_register_project(raw_project_name)

    grp = sanitize_project_name(proj.priority_group.upper(), max_length=40)
    fs_name = sanitize_project_name(proj.display_name)
    target_dir = out_root / grp / fs_name
    ensure_contained(target_dir, out_root)
    return target_dir, fs_name, proj, was_created


def _extract_header_map(lines: list[str]) -> dict[str, str]:
    headers = {}
    for line_s in lines:
        if ":" in line_s:
            parts = line_s.split(":", 1)
            key = parts[0].strip().strip("`*_ ").upper()
            val = parts[1].strip().strip("`*_ ")
            if key and key not in headers:
                headers[key] = val
    return headers


def parse_wave(
    text: str,
    wave_or_id: str,
    profile_or_id: Optional[Union[str, CampaignProfile]] = None,
) -> tuple[bool, Optional[dict[str, Any]], str]:
    """
    Strict canonical validation of an incoming audit wave markdown.

    Supports both legacy quick3 waves and any generic CampaignProfile wave.
    """
    raw_lines = [line.strip() for line in text.splitlines()]
    lines = []
    for rl in raw_lines:
        if rl.startswith("```"):
            continue
        cleaned = _RE_CLEAN_HEADER1.sub(r"\1: ", rl)
        cleaned = _RE_CLEAN_HEADER2.sub(r"\1: ", cleaned)
        lines.append(cleaned)

    def _header_value(key: str) -> Optional[str]:
        anchor = key.upper() + ":"
        for line_s in lines:
            if line_s.upper().startswith(anchor):
                return line_s[len(anchor):].strip().strip("`*_ ")
        return None

    # Resolve profile
    prof: Optional[CampaignProfile] = None
    if isinstance(profile_or_id, CampaignProfile):
        prof = profile_or_id
    elif isinstance(profile_or_id, str) and profile_or_id.strip():
        try:
            prof = get_profile(profile_or_id)
        except Exception:
            prof = None

    if prof is None:
        declared_profile = _header_value("CAMPAIGN_PROFILE")
        if declared_profile:
            try:
                prof = get_profile(declared_profile)
            except Exception:
                prof = None

    if prof is None:
        # Check if wave matches a known profile
        try:
            all_profs = load_profiles()
            for p_candidate in all_profs.values():
                if p_candidate.get_wave_by_id(wave_or_id):
                    prof = p_candidate
                    break
        except Exception:
            pass

    if prof is None:
        prof = get_default_profile()

    wave_def = prof.get_wave_by_id(wave_or_id)
    if not wave_def:
        # Try matching by ordinal/number
        wave_def = prof.get_wave_by_number(wave_or_id)

    if not wave_def:
        return False, None, f"Unknown wave '{wave_or_id}' in profile '{prof.profile_id}'"

    prefix = wave_def.ticket_prefix.rstrip("-")
    done_marker = wave_def.done_marker

    meta: dict[str, Any] = {
        "profile_id": prof.profile_id,
        "profile_version": prof.profile_version,
        "wave_id": wave_def.id,
        "wave_index": wave_def.ordinal,
        "wave_count": prof.wave_count,
        "ticket_prefix": wave_def.ticket_prefix,
    }

    # Optional metadata headers
    metadata_keys = (
        ("TARGET", "target"),
        ("BASELINE", "baseline"),
        ("CORE_BASELINE", "core_baseline"),
        ("PREVIOUS_BASELINE", "previous_baseline"),
        ("PREVIOUS_WAVE_SHA256", "previous_wave_sha256"),
        ("CAMPAIGN_RUN_ID", "campaign_run_id"),
        ("CAMPAIGN_MANIFEST_SHA256", "campaign_manifest_sha256"),
        ("GIT_CONTEXT", "git_context"),
        ("SAIPEN_CONTEXT", "saipen_context"),
        ("AUDIT_SCOPE", "audit_scope"),
        ("DATE_TIME", "date_time"),
        ("TEST_STATUS", "test_status"),
        ("TEST_LIMITATION", "test_limitation"),
        ("VERIFIED_INSTEAD", "verified_instead"),
        ("COVERAGE_INSPECTED", "coverage_inspected"),
        ("COVERAGE_DEFERRED", "coverage_deferred"),
        ("CROSS_WAVE_REFERENCES", "cross_wave_references"),
        ("RESIDUAL_UNCERTAINTY", "residual_uncertainty"),
    )
    for key, meta_key in metadata_keys:
        value = _header_value(key)
        if value is not None:
            meta[meta_key] = value

    # PROJECT_NAME: mandatory and non-empty
    project_name = _header_value("PROJECT_NAME")
    if not project_name:
        return False, None, f"Missing or empty PROJECT_NAME in {wave_def.id}"
    meta["project_name"] = project_name

    # WAVE header: exact match against wave_def.wave_header
    wave_header = _header_value("WAVE")
    if wave_header != wave_def.wave_header:
        return False, None, (
            f"Wrong WAVE header in {wave_def.id}: got {wave_header!r}, "
            f"expected {wave_def.wave_header!r}"
        )

    # Terminal STATUS: exact match against wave_def.status_line or STATUS: <terminal_status_key>: COMPLETE
    status_raw = _header_value("STATUS")
    if status_raw is None:
        return False, None, f"Missing terminal STATUS in {wave_def.id}"

    expected_status_full = wave_def.status_line.replace("STATUS: ", "").strip()
    status_line_candidate = f"STATUS: {status_raw}"
    if status_line_candidate != wave_def.status_line and status_raw != expected_status_full:
        # Fallback check for prefix: COMPLETE
        expected_short = f"{wave_def.terminal_status_key}: COMPLETE"
        if status_raw != expected_short and status_raw != f"{prefix}: COMPLETE":
            return False, None, (
                f"Wrong terminal STATUS in {wave_def.id}: got 'STATUS: {status_raw}', "
                f"expected {wave_def.status_line!r}"
            )
    meta["status"] = status_raw

    # TICKETS: non-negative integer, present
    tickets_raw = _header_value("TICKETS")
    if tickets_raw is None:
        return False, None, f"Missing TICKETS in {wave_def.id}"
    try:
        n_tickets = int(tickets_raw)
    except Exception:
        return False, None, f"Invalid TICKETS count in {wave_def.id}"
    if n_tickets < 0:
        return False, None, f"Negative TICKETS count in {wave_def.id}"
    meta["tickets"] = n_tickets

    # DONE marker: must appear as a line with a non-empty remainder
    done_ok = any(
        line_s.startswith(done_marker) and len(line_s[len(done_marker):].strip()) > 0
        for line_s in lines
    )
    if not done_ok:
        return False, None, f"Missing {done_marker} in {wave_def.id}"

    # Ticket consistency: unique ids of THIS wave's prefix only
    if n_tickets > 0:
        ticket_pat = re.compile(rf"\[{re.escape(wave_def.ticket_prefix)}(\d+)\]")
        found_tickets = set(int(m) for m in ticket_pat.findall(text))
        if len(found_tickets) != n_tickets:
            return False, None, (
                f"Expected {n_tickets} unique {wave_def.ticket_prefix} tickets, "
                f"found {len(found_tickets)}"
            )

    meta["full_text"] = text.strip()
    return True, meta, ""


def generate_canonical_all3(
    project_name: str,
    run_id: str,
    parsed_waves: dict[str, dict[str, Any]],
) -> str:
    """Combines 3 validated waves into canonical __00_AUDIT_ALL_3.md (Quick3 profile)."""
    c_meta = parsed_waves.get("core", {})
    s_meta = parsed_waves.get("second", {})
    p_meta = parsed_waves.get("performance", {})

    total_tickets = c_meta.get("tickets", 0) + s_meta.get("tickets", 0) + p_meta.get("tickets", 0)
    gen_time = datetime.now().isoformat()

    lines = [
        f"# {project_name} — Audit Handoff",
        "",
        f"RUN_ID: {run_id}",
        f"GENERATED_AT: {gen_time}",
        f"PROJECT_NAME: {c_meta.get('project_name', project_name)}",
        f"TARGET: {c_meta.get('target', '')}",
        f"BASELINE: {c_meta.get('baseline', '')}",
        f"GIT_CONTEXT: {c_meta.get('git_context', 'ABSENT')}",
        f"SAIPEN_CONTEXT: {c_meta.get('saipen_context', 'ABSENT')}",
        f"TOTAL_TICKETS: {total_tickets}",
        "STATUS: AUDIT_ALL_3: COMPLETE",
        "",
        "## Summary of Wave Audits",
        f"- Wave 1 (Core): {c_meta.get('tickets', 0)} tickets (baseline: {c_meta.get('baseline', 'NONE')})",
        f"- Wave 2 (Second Wave): {s_meta.get('tickets', 0)} tickets (baseline: {s_meta.get('baseline', 'NONE')})",
        f"- Wave 3 (Performance): {p_meta.get('tickets', 0)} tickets (baseline: {p_meta.get('baseline', 'NONE')})",
        "",
        "---",
        "## 01 — AUDIT CORE",
        c_meta.get("full_text", "No content"),
        "",
        "---",
        "## 02 — AUDIT SECOND WAVE",
        s_meta.get("full_text", "No content"),
        "",
        "---",
        "## 03 — AUDIT PERFORMANCE",
        p_meta.get("full_text", "No content"),
        "",
        "---",
        "ALL_3_DONE_WHEN: 3/3 waves validated and combined into canonical handoff package.",
    ]
    return "\n".join(lines)


def generate_canonical_campaign(
    profile: CampaignProfile,
    run_id: str,
    parsed_waves: dict[str, dict[str, Any]],
    project_name: str,
) -> dict[str, str]:
    """
    Generic synthesis generator for any CampaignProfile.
    Returns mapping of artifact type -> generated file text:
    - quick3: {'all3': '...'}
    - super10 / N-wave: {'super_all': '...', 'super_final': '...', 'super_index': '...'}
    """
    if profile.profile_id == "quick3":
        all3_text = generate_canonical_all3(project_name, run_id, parsed_waves)
        return {"all3": all3_text}

    # N-wave / SUPER10 synthesis
    first_wave = profile.waves[0] if profile.waves else None
    first_meta = parsed_waves.get(first_wave.id, {}) if first_wave else {}

    total_tickets = sum(
        int(parsed_waves.get(w.id, {}).get("tickets", 0))
        for w in profile.waves
    )
    gen_time = datetime.now().isoformat()
    manifest_hash = profile.manifest_hash or get_canonical_manifest_hash()

    # 1. Combined raw artifact (__00_SUPER_AUDIT_ALL.md)
    all_lines = [
        f"# {project_name} — {profile.display_name} Raw Evidence",
        "",
        f"CAMPAIGN_PROFILE: {profile.profile_id}",
        f"CAMPAIGN_PROFILE_VERSION: {profile.profile_version}",
        f"CAMPAIGN_MANIFEST_SHA256: {manifest_hash}",
        f"CAMPAIGN_RUN_ID: {run_id}",
        f"GENERATED_AT: {gen_time}",
        f"PROJECT_NAME: {first_meta.get('project_name', project_name)}",
        f"TARGET: {first_meta.get('target', '')}",
        f"BASELINE: {first_meta.get('baseline', '')}",
        f"GIT_CONTEXT: {first_meta.get('git_context', 'ABSENT')}",
        f"SAIPEN_CONTEXT: {first_meta.get('saipen_context', 'ABSENT')}",
        f"TOTAL_TICKETS: {total_tickets}",
        "STATUS: SUPER_AUDIT_ALL: COMPLETE",
        f"COMPLETED_WAVES: {len(profile.waves)}/{profile.wave_count}",
        "",
        "## Wave Summary",
    ]

    for w in profile.waves:
        w_meta = parsed_waves.get(w.id, {})
        w_tix = w_meta.get("tickets", 0)
        w_sha = hashlib.sha256(w_meta.get("full_text", "").encode("utf-8")).hexdigest()[:12] if w_meta.get("full_text") else "NONE"
        all_lines.append(f"- Wave {w.ordinal:02d} ({w.short_label}): {w_tix} tickets | sha256:{w_sha} | {w.title}")

    all_lines.append("")

    for w in profile.waves:
        w_meta = parsed_waves.get(w.id, {})
        all_lines.extend([
            "---",
            f"## {w.number} — {w.wave_header}",
            w_meta.get("full_text", f"No content for {w.id}"),
            "",
        ])

    all_lines.extend([
        "---",
        f"SUPER_AUDIT_ALL_DONE_WHEN: All {profile.wave_count}/{profile.wave_count} waves validated and combined into canonical evidence.",
    ])
    super_all_content = "\n".join(all_lines)

    # 2. Final deduplicated handoff artifact (__00_SUPER_AUDIT_FINAL.md)
    finalizer_def = profile.get_wave_by_id(profile.finalizer_wave_id) or (profile.waves[-1] if profile.waves else None)
    finalizer_meta = parsed_waves.get(finalizer_def.id, {}) if finalizer_def else {}
    finalizer_text = finalizer_meta.get("full_text", "")

    # If wave 10 already produced a dedicated synthesis / final handoff section, extract or wrap it
    final_lines = [
        f"# {project_name} — Implementation Handoff (Final Deduplicated Audit)",
        "",
        f"CAMPAIGN_PROFILE: {profile.profile_id}",
        f"CAMPAIGN_RUN_ID: {run_id}",
        f"GENERATED_AT: {gen_time}",
        f"PROJECT_NAME: {first_meta.get('project_name', project_name)}",
        f"TARGET: {first_meta.get('target', '')}",
        f"BASELINE: {first_meta.get('baseline', '')}",
        f"TOTAL_RAW_TICKETS: {total_tickets}",
        "SUPER_AUDIT_STATUS: COMPLETE",
        f"SOURCE_WAVES: {len(profile.waves)}/{profile.wave_count}",
        "",
        "---",
        "## FINAL IMPLEMENTATION HANDOFF",
        "",
    ]

    # Look for dedicated synthesis block in finalizer wave text
    if "FINAL DEDUPLICATED IMPLEMENTATION HANDOFF" in finalizer_text or "SUPER_AUDIT_STATUS:" in finalizer_text:
        final_lines.append(finalizer_text)
    elif finalizer_text:
        final_lines.append(finalizer_text)
    else:
        final_lines.append(f"Finalizer wave ({profile.finalizer_wave_id}) handoff text.")

    final_lines.extend([
        "",
        "---",
        f"SUPER_AUDIT_DONE_WHEN: Final implementation handoff verified and deduplicated across {profile.wave_count} waves.",
    ])
    super_final_content = "\n".join(final_lines)

    # 3. Machine index artifact (__00_SUPER_AUDIT_INDEX.json)
    index_data = {
        "schema_version": 1,
        "campaign_profile": profile.profile_id,
        "campaign_profile_version": profile.profile_version,
        "campaign_manifest_sha256": manifest_hash,
        "campaign_run_id": run_id,
        "project_name": project_name,
        "generated_at": gen_time,
        "total_tickets": total_tickets,
        "status": "COMPLETE",
        "waves": [
            {
                "wave_id": w.id,
                "ordinal": w.ordinal,
                "number": w.number,
                "slug": w.slug,
                "title": w.title,
                "ticket_prefix": w.ticket_prefix,
                "tickets": parsed_waves.get(w.id, {}).get("tickets", 0),
                "sha256": hashlib.sha256(parsed_waves.get(w.id, {}).get("full_text", "").encode("utf-8")).hexdigest() if parsed_waves.get(w.id, {}).get("full_text") else "",
            }
            for w in profile.waves
        ],
    }
    super_index_json = json.dumps(index_data, indent=2, ensure_ascii=False)

    return {
        "super_all": super_all_content,
        "super_final": super_final_content,
        "super_index": super_index_json,
    }
