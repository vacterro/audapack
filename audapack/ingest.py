"""Audit text ingestion, normalization, multi-wave splitting, and storage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from audapack.bridge.state import increment_audit_generation
from audapack.bridge.storage import (
    atomic_write,
    generate_canonical_campaign,
    parse_wave,
    resolve_project_audit_dir,
)
from audapack.campaign import (
    get_profile,
    load_profiles,
)
from audapack.config import AppConfig, load_config


@dataclass
class IngestResult:
    ok: bool
    project_name: str = ""
    profile_id: str = "quick3"
    saved_waves: list[str] = field(default_factory=list)
    files_written: list[Path] = field(default_factory=list)
    campaign_generated: bool = False
    all3_generated: bool = False
    final_handoff_path: Optional[Path] = None
    all3_path: Optional[Path] = None
    message: str = ""
    error: str = ""


def clean_markdown_headers(text: str) -> str:
    """Strips markdown bolding and formatting decorators from audit headers."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            continue
        line = re.sub(r"^\*{1,2}([A-Za-z0-9_]+):\*{0,2}\s*", r"\1: ", line)
        line = re.sub(r"^\*{1,2}([A-Za-z0-9_]+)\*{1,2}:\s*", r"\1: ", line)
        lines.append(line)
    return "\n".join(lines)


def detect_wave_and_profile(text: str, profile_hint: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Detects wave id and profile id from audit text."""
    norm = clean_markdown_headers(text)

    # Check explicit CAMPAIGN_PROFILE header
    m_prof = re.search(r"^CAMPAIGN_PROFILE:\s*([A-Za-z0-9_-]+)", norm, re.MULTILINE)
    detected_prof = m_prof.group(1).strip().lower() if m_prof else (profile_hint or "").strip().lower()

    # Check explicit WAVE_ID header
    m_wave_id = re.search(r"^WAVE_ID:\s*([A-Za-z0-9_-]+)", norm, re.MULTILINE)
    if m_wave_id:
        w_id = m_wave_id.group(1).strip().lower()
        return w_id, detected_prof or "super10"

    profs = load_profiles()
    active_prof_keys = [detected_prof] if detected_prof in profs else list(profs.keys())

    for pid in active_prof_keys:
        p_obj = profs[pid]
        for w in p_obj.waves:
            if w.status_line in norm or w.done_marker in norm or f"WAVE: {w.wave_header}" in norm:
                return w.id, pid

    # Heuristic fallbacks
    if "ARCH_DONE_WHEN:" in text or "AUDIT ARCHITECTURE" in text:
        return "architecture", "super10"
    if "CORR_DONE_WHEN:" in text or "AUDIT CORRECTNESS" in text:
        return "correctness", "super10"
    if "STATE_DONE_WHEN:" in text or "AUDIT STATE" in text:
        return "state", "super10"
    if "REC_DONE_WHEN:" in text or "AUDIT FAILURE" in text or "AUDIT RECOVERY" in text:
        return "recovery", "super10"
    if "SEC_DONE_WHEN:" in text or "AUDIT SECURITY" in text:
        return "security", "super10"
    if "INT_DONE_WHEN:" in text or "AUDIT INTEGRATION" in text:
        return "integration", "super10"
    if "TEST_DONE_WHEN:" in text or "AUDIT TESTS" in text or "AUDIT VERIFICATION" in text:
        return "verification", "super10"
    if "UX_DONE_WHEN:" in text or "AUDIT OPERATOR" in text or "AUDIT UX" in text:
        return "operator", "super10"
    if "RED_DONE_WHEN:" in text or "AUDIT REDTEAM" in text:
        return "redteam", "super10"

    if "CORE_DONE_WHEN:" in text or "AUDIT CORE" in text:
        return "core", "quick3"
    if "SECOND_WAVE_DONE_WHEN:" in text or "AUDIT SECOND WAVE" in text:
        return "second", "quick3"
    if "PERFORMANCE_DONE_WHEN:" in text or "AUDIT PERFORMANCE" in text:
        return "performance", "quick3"

    return None, None


def detect_wave_type(text: str, profile_hint: Optional[str] = None) -> Optional[str]:
    """Detects wave id from audit text (backwards compatible)."""
    wid, _pid = detect_wave_and_profile(text, profile_hint)
    return wid


def _same_project_name(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def extract_project_name_from_text(text: str) -> Optional[str]:
    """Extracts PROJECT_NAME from audit text."""
    norm = clean_markdown_headers(text)
    m = re.search(r"^PROJECT_NAME:\s*(.+)$", norm, re.MULTILINE)
    if m:
        name = m.group(1).strip()
        name = name.strip("`*_# ")
        if name:
            return name
    return None


def split_multi_wave_text(text: str, profile_hint: Optional[str] = None) -> tuple[dict[str, str], str]:
    """Splits text containing multiple wave audits into individual wave texts."""
    cleaned = clean_markdown_headers(text)
    profs = load_profiles()

    detected_wave, detected_pid = detect_wave_and_profile(text, profile_hint)
    pid = detected_pid or profile_hint or "quick3"
    if pid not in profs:
        pid = "quick3"
    profile = profs[pid]

    found_positions: list[tuple[int, str]] = []
    for w in profile.waves:
        marker = f"WAVE: {w.wave_header}"
        idx = cleaned.find(marker)
        if idx != -1:
            p_idx = cleaned.rfind("PROJECT_NAME:", 0, idx)
            start_pos = p_idx if p_idx != -1 else idx
            found_positions.append((start_pos, w.id))
        else:
            d_idx = cleaned.find(w.done_marker)
            if d_idx != -1:
                p_idx = cleaned.rfind("PROJECT_NAME:", 0, d_idx)
                start_pos = p_idx if p_idx != -1 else d_idx
                found_positions.append((start_pos, w.id))

    waves: dict[str, str] = {}
    if not found_positions:
        if detected_wave:
            waves[detected_wave] = text
        return waves, pid

    found_positions.sort(key=lambda x: x[0])
    # Deduplicate positions by wave_id
    seen_waves = set()
    unique_positions = []
    for pos, wid in found_positions:
        if wid not in seen_waves:
            seen_waves.add(wid)
            unique_positions.append((pos, wid))

    for i, (pos, wid) in enumerate(unique_positions):
        end_pos = unique_positions[i + 1][0] if i + 1 < len(unique_positions) else len(cleaned)
        chunk = cleaned[pos:end_pos].strip()
        if chunk:
            waves[wid] = chunk

    return waves, pid


def ingest_audit_text(
    content: str,
    config: Optional[AppConfig] = None,
    project_hint: Optional[str] = None,
    profile_hint: Optional[str] = None,
) -> IngestResult:
    """Ingests, validates, and stores one or more audit waves from text."""
    cfg = config or load_config()
    content = content.strip()
    if not content:
        return IngestResult(ok=False, error="Audit text is empty.")

    wave_chunks, resolved_pid = split_multi_wave_text(content, profile_hint)
    if not wave_chunks:
        single_type, single_pid = detect_wave_and_profile(content, profile_hint)
        if single_type:
            wave_chunks = {single_type: content}
            resolved_pid = single_pid or "quick3"
        else:
            return IngestResult(
                ok=False,
                error="Could not identify any valid audit wave markers in text.",
            )

    profile = get_profile(resolved_pid)

    extracted_proj = extract_project_name_from_text(content)
    target_project_name = extracted_proj or project_hint
    if not target_project_name:
        return IngestResult(
            ok=False,
            error="PROJECT_NAME header is missing and no target project was selected.",
        )

    try:
        target_dir, resolved_name, proj, was_created = resolve_project_audit_dir(
            cfg, target_project_name
        )
    except Exception as exc:
        return IngestResult(ok=False, error=f"Failed to resolve project audit directory: {exc}")

    target_dir.mkdir(parents=True, exist_ok=True)

    for wid, chunk_text in wave_chunks.items():
        chunk_proj = extract_project_name_from_text(chunk_text) or project_hint
        if not _same_project_name(chunk_proj, resolved_name):
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                error=(
                    f"Wave '{wid}' declares project '{chunk_proj or 'unknown'}', "
                    f"which does not match the target project '{resolved_name}'. "
                    f"Mixed-project audit paste rejected; nothing was written."
                ),
            )

    saved_waves = []
    files_written = []

    for wid, chunk_text in wave_chunks.items():
        w_def = profile.get_wave_by_id(wid)
        if not w_def:
            continue

        chunk_clean = clean_markdown_headers(chunk_text)

        if "PROJECT_NAME:" not in chunk_clean:
            chunk_clean = f"PROJECT_NAME: {resolved_name}\n" + chunk_clean

        if f"WAVE: {w_def.wave_header}" not in chunk_clean and "WAVE:" not in chunk_clean:
            chunk_clean = re.sub(
                r"^(PROJECT_NAME:[^\n]+)",
                r"\1\nWAVE: " + w_def.wave_header,
                chunk_clean,
            )

        valid, meta, err = parse_wave(chunk_clean, wid, profile)
        if not valid:
            if w_def.done_marker in chunk_clean and "STATUS:" not in chunk_clean:
                chunk_clean = re.sub(
                    r"^(WAVE:[^\n]+)",
                    r"\1\n" + w_def.status_line,
                    chunk_clean,
                )
                valid, meta, err = parse_wave(chunk_clean, wid, profile)

        if not valid:
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                profile_id=profile.profile_id,
                error=f"Wave '{wid}' validation failed: {err}",
            )

        latest_filename = f"{resolved_name}__{w_def.number}_{w_def.slug}.md"
        latest_path = target_dir / latest_filename

        try:
            atomic_write(latest_path, chunk_clean)
            saved_waves.append(wid)
            files_written.append(latest_path)
            increment_audit_generation(resolved_name, wid)
        except Exception as exc:
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                profile_id=profile.profile_id,
                error=f"Failed to write {latest_filename}: {exc}",
            )

    # Check if all required waves of active profile now exist on disk
    all_wave_paths = [
        target_dir / f"{resolved_name}__{w.number}_{w.slug}.md"
        for w in profile.waves
    ]
    all_exist = all(p.exists() for p in all_wave_paths)

    campaign_generated = False
    all3_generated = False
    final_path = None
    all3_path = None

    if all_exist:
        try:
            parsed_d = {}
            valid_all = True
            for w, p in zip(profile.waves, all_wave_paths, strict=False):
                text = p.read_text(encoding="utf-8")
                v, m, _ = parse_wave(text, w.id, profile)
                if not (v and m):
                    valid_all = False
                    break
                parsed_d[w.id] = m

            if valid_all:
                synth_map = generate_canonical_campaign(
                    profile,
                    f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    parsed_d,
                    resolved_name,
                )

                if profile.profile_id == "quick3":
                    all3_file = target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
                    atomic_write(all3_file, synth_map.get("all3", ""))
                    all3_generated = True
                    campaign_generated = True
                    all3_path = all3_file
                    final_path = all3_file
                    files_written.append(all3_file)
                    increment_audit_generation(resolved_name, "all3")
                else:
                    all_file = target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md"
                    final_file = target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md"
                    idx_file = target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json"

                    atomic_write(all_file, synth_map.get("super_all", ""))
                    atomic_write(final_file, synth_map.get("super_final", ""))
                    atomic_write(idx_file, synth_map.get("super_index", ""))

                    campaign_generated = True
                    final_path = final_file
                    files_written.extend([all_file, final_file, idx_file])
                    increment_audit_generation(resolved_name, "super_audit")
        except Exception:
            pass

    waves_str = ", ".join(w.upper() for w in saved_waves)
    msg = f"Project '{resolved_name}': successfully saved {waves_str} ({profile.display_name})."
    if campaign_generated:
        msg += f" All {profile.wave_count}/{profile.wave_count} waves complete! Canonical campaign generated."

    return IngestResult(
        ok=True,
        project_name=resolved_name,
        profile_id=profile.profile_id,
        saved_waves=saved_waves,
        files_written=files_written,
        campaign_generated=campaign_generated,
        all3_generated=all3_generated,
        final_handoff_path=final_path,
        all3_path=all3_path,
        message=msg,
    )
