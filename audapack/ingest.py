"""Audit text ingestion, normalization, multi-wave splitting, and storage."""

from __future__ import annotations

import hashlib
import json
import re
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from audapack.bridge.state import increment_audit_generation
from audapack.bridge.storage import (
    atomic_write,
    capture_file_snapshots,
    generate_canonical_campaign,
    parse_wave,
    resolve_project_audit_dir,
    restore_file_snapshots,
)
from audapack.campaign import (
    STATUS_CAMPAIGN_COMPLETE,
    STATUS_CAMPAIGN_READY_FOR_WAVE,
    get_profile,
    load_profiles,
    save_live_campaign_index,
)
from audapack.config import AppConfig, load_config
from audapack.projects import ProjectRegistry


def _ingest_failure(
    project_name: str,
    profile_id: str,
    error: str,
    snapshots: dict[Path, Optional[bytes]],
) -> "IngestResult":
    rollback_errors = restore_file_snapshots(snapshots)
    if rollback_errors:
        error += f"; rollback incomplete: {'; '.join(rollback_errors)}"
    else:
        error += "; all ingest writes rolled back"
    return IngestResult(
        ok=False,
        project_name=project_name,
        profile_id=profile_id,
        error=error,
    )


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


def _extract_campaign_run_id(text: str) -> Optional[str]:
    """Return the CAMPAIGN_RUN_ID header value from raw audit text, or None."""
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`*_# ")
        if line.upper().startswith("CAMPAIGN_RUN_ID:"):
            val = line[len("CAMPAIGN_RUN_ID:"):].strip().strip("`*_ ")
            if val:
                return val
    return None


def clean_markdown_headers(text: str) -> str:
    """Normalizes ONLY recognized audit header syntax; preserves body lines
    byte-for-byte apart from an allowed newline convention.

    A line is normalized only when it is a recognizable bolded audit header
    (``**KEY:** value`` / ``**KEY**: value`` / ``*KEY*: value``). Every other
    line — evidence, code snippets, nested fences, indentation, lists,
    blockquotes, plain ``KEY: value`` — is preserved exactly. An outer
    code fence enclosing the entire artifact (```markdown ... ```) is stripped
    once, but embedded fences inside the body are never removed (CORE-007).
    """
    raw_lines = text.splitlines()
    # Strip a single outer fence pair if the whole artifact is wrapped: first
    # non-empty line is ```... and last non-empty line is ```. Embedded fences
    # in the middle are preserved — they are evidence, not container.
    first_idx, last_idx = -1, -1
    for i, ln in enumerate(raw_lines):
        if ln.strip() != "":
            if first_idx == -1:
                first_idx = i
            last_idx = i
    if first_idx != -1 and last_idx != -1 and first_idx != last_idx:
        if raw_lines[first_idx].strip().startswith("```") and raw_lines[last_idx].strip() == "```":
            raw_lines = raw_lines[first_idx + 1 : last_idx]
    lines = raw_lines
    out: list[str] = []
    for raw_line in lines:
        if re.match(r"^\*{1,2}[A-Za-z0-9_ ]+:{0,1}\*{0,2}\s", raw_line):
            cleaned = re.sub(r"^\*{1,2}([A-Za-z0-9_]+):\*{0,2}\s*", r"\1: ", raw_line)
            cleaned = re.sub(r"^\*{1,2}([A-Za-z0-9_]+)\*{1,2}:\s*", r"\1: ", cleaned)
            out.append(cleaned.rstrip())
        else:
            out.append(raw_line)
    return "\n".join(out)


def detect_wave_and_profile(text: str, profile_hint: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Detects wave id and profile id from audit text."""
    norm = clean_markdown_headers(text)

    # Check explicit CAMPAIGN_PROFILE header
    m_prof = re.search(r"^CAMPAIGN_PROFILE:\s*([A-Za-z0-9_-]+)", norm, re.MULTILINE)
    detected_prof = m_prof.group(1).strip().lower() if m_prof else (profile_hint or "").strip().lower()

    profs = load_profiles()

    # Check explicit WAVE_ID header. Resolve the wave against the DECLARED
    # profile first; when no profile is declared, find the unique profile that
    # owns that wave instead of defaulting to super10. A legacy handoff with
    # WAVE_ID: core (a quick3 wave) must resolve to quick3, never super10.
    m_wave_id = re.search(r"^WAVE_ID:\s*([A-Za-z0-9_-]+)", norm, re.MULTILINE)
    if m_wave_id:
        w_id = m_wave_id.group(1).strip().lower()
        if detected_prof in profs:
            return w_id, detected_prof
        for pid, p_obj in profs.items():
            if p_obj.get_wave_by_id(w_id) or p_obj.get_wave_by_number(w_id):
                return w_id, pid
        return w_id, None

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
    base_dir: Optional[Path] = None,
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

    # Resolve identity without mutation. Unknown projects are registered only
    # after every wave has passed validation.
    registry = ProjectRegistry(cfg, base_dir=base_dir, transactional=bool(base_dir))
    existing_project = registry.get_project_by_name(target_project_name)
    resolved_name = (
        existing_project.audit_project_name or existing_project.display_name
        if existing_project else target_project_name
    )
    proj = existing_project

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

    # CORE-008: prepare/commit. Validate and normalize EVERY chunk before any
    # disk write, so a failed multi-wave ingest leaves the audit directory
    # byte-for-byte unchanged (no undeclared partial campaign).
    prepared: list[tuple[str, Path, str]] = []
    for wid, chunk_text in wave_chunks.items():
        w_def = profile.get_wave_by_id(wid)
        if not w_def:
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                profile_id=profile.profile_id,
                error=f"Wave '{wid}' is not defined in profile '{profile.profile_id}'. "
                      f"Check for a profile/handoff mismatch.",
            )

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
        prepared.append((wid, latest_filename, chunk_clean))

    if proj is None:
        try:
            proj, _created = registry.resolve_or_register_project(target_project_name)
        except Exception as exc:
            return IngestResult(ok=False, profile_id=profile.profile_id, error=f"Failed to register project: {exc}")
        resolved_name = proj.audit_project_name or proj.display_name
    target_dir, resolved_name, proj, _created = resolve_project_audit_dir(
        cfg, resolved_name, project_id=proj.id, base_dir=base_dir
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    prepared = [(wid, target_dir / f"{resolved_name}__{profile.get_wave_by_id(wid).number}_{profile.get_wave_by_id(wid).slug}.md", chunk)
                for wid, _filename, chunk in prepared]

    wave_paths = [path for _wid, path, _chunk in prepared]
    canonical_paths = [
        target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md",
        target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md",
        target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md",
        target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json",
        target_dir / "campaign.json",
    ]
    snapshots, snapshot_error = capture_file_snapshots(wave_paths + canonical_paths)
    if snapshot_error:
        return IngestResult(
            ok=False,
            project_name=resolved_name,
            profile_id=profile.profile_id,
            error=snapshot_error,
        )

    try:
        for wid, latest_path, chunk_clean in prepared:
            atomic_write(latest_path, chunk_clean)
            saved_waves.append(wid)
            files_written.append(latest_path)
    except Exception as exc:
        return _ingest_failure(
            resolved_name,
            profile.profile_id,
            f"Failed to write {latest_path.name}: {exc}",
            snapshots,
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

    try:
        # W4-003: derive the canonical campaign run id from the wave content
        # itself, so the same id flows through the wave files, the synthesized
        # __00_AUDIT_ALL_3.md, the campaign.json index, and the Bridge payload
        # that will carry them. Two patches above this line already extracted
        # the `campaign_run_id` value via parse_wave; reading the raw header
        # from each chunk is the most robust way to enforce cross-wave
        # equality and to detect a third-party paste that swapped the id
        # mid-campaign. The mint-only legacy path is preserved for ingest
        # calls that supply no CAMPAIGN_RUN_ID header at all.
        canonical_run_ids: set[str] = set()
        for _wid, _path, chunk_clean in prepared:
            rid = _extract_campaign_run_id(chunk_clean)
            if rid:
                canonical_run_ids.add(rid)
        if len(canonical_run_ids) > 1:
            return _ingest_failure(
                resolved_name,
                profile.profile_id,
                "Multiple campaign run IDs detected across waves: "
                + ", ".join(sorted(canonical_run_ids))
                + ". All waves in a single ingest must share one CAMPAIGN_RUN_ID.",
                snapshots,
            )
        if len(canonical_run_ids) == 1:
            ingest_run_id = next(iter(canonical_run_ids))
        else:
            # Legacy path: wave content carries no CAMPAIGN_RUN_ID. Preserve
            # the original W2-006 mint so existing callers that fabricate the
            # id server-side keep working.
            ingest_run_id = (
                f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                f"{_uuid.uuid4().hex[:6]}"
            )

        # W4-003: also reject a drift against the on-disk campaign.json if the
        # same project already has a finalized run. A new ingest that brings
        # a different run id for an existing campaign means a transport /
        # content split-brain, and the only honest answer is to refuse before
        # any file write instead of silently rewriting the index.
        existing_index = target_dir / "campaign.json"
        if existing_index.exists() and canonical_run_ids:
            try:
                idx_doc = json.loads(existing_index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                idx_doc = None
            if isinstance(idx_doc, dict):
                existing_run = (idx_doc.get("campaign_run_id") or "").strip()
                if existing_run and existing_run != ingest_run_id:
                    return _ingest_failure(
                        resolved_name,
                        profile.profile_id,
                        f"Multiple campaign run IDs: existing campaign run id {existing_run!r} "
                        f"disagrees with the new content CAMPAIGN_RUN_ID {ingest_run_id!r}. "
                        f"A campaign run id is immutable; start a fresh run instead of "
                        f"re-pasting under a different id.",
                        snapshots,
                    )

        if all_exist:
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
                    ingest_run_id,
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

        if all_exist and not campaign_generated:
            return _ingest_failure(
                resolved_name,
                profile.profile_id,
                "Failed to finalize complete campaign: canonical final artifact was not generated",
                snapshots,
            )

        # Save / update live campaign.json after ingestion. This is part of the
        # same transaction: a stale index can make the next-wave gate lie.
        completed_waves = []
        parsed_waves_dict = {}
        for w in profile.waves:
            w_path = target_dir / f"{resolved_name}__{w.number}_{w.slug}.md"
            if w_path.exists():
                txt = w_path.read_text(encoding="utf-8", errors="replace")
                v, m, _ = parse_wave(txt, w.id, profile)
                if v and m:
                    completed_waves.append(w.id)
                    parsed_waves_dict[w.id] = {
                        "wave_id": w.id,
                        "status": "COMPLETE",
                        "tickets": int(m.get("tickets", 0)),
                        "file": w_path,
                        "sha256": hashlib.sha256(txt.encode("utf-8")).hexdigest(),
                    }
        next_w = None
        for w in profile.waves:
            if w.id not in completed_waves:
                next_w = w
                break
        c_status = (
            STATUS_CAMPAIGN_COMPLETE
            if len(completed_waves) == profile.wave_count and campaign_generated and final_path
            else STATUS_CAMPAIGN_READY_FOR_WAVE
        )
        save_live_campaign_index(
            campaign_root=target_dir,
            profile=profile,
            run_id=ingest_run_id,
            project_name=resolved_name,
            parsed_waves=parsed_waves_dict,
            completed_waves=completed_waves,
            active_wave_id=next_w.id if next_w else None,
            status=c_status,
            final_handoff_path=final_path,
        )
    except Exception as exc:
        return _ingest_failure(
            resolved_name,
            profile.profile_id,
            f"Failed to commit canonical campaign artifacts: {exc}",
            snapshots,
        )

    # W2-005: generation publication is a separate POST-COMMIT phase. The
    # transaction block above already durably committed wave files, canonical
    # finals, and campaign.json; a failure here must NEVER roll back those
    # authoritative files (notifications cannot be un-published). Publish each
    # logical event once -- for a terminal ingest, a single consolidated
    # generation for the campaign is enough; consumers refresh the project.
    try:
        if campaign_generated:
            increment_audit_generation(
                resolved_name,
                "all3" if profile.profile_id == "quick3" else "super_audit",
                project_id=proj.id if proj else None,
            )
        elif saved_waves:
            increment_audit_generation(
                resolved_name,
                saved_waves[-1],
                project_id=proj.id if proj else None,
            )
    except Exception as exc:
        # The ingest itself committed; surface the notification failure without
        # rolling back files. A stale generation is a refresh problem, not data
        # loss, and the next successful generation or explicit refresh recovers.
        return IngestResult(
            ok=False,
            project_name=resolved_name,
            profile_id=profile.profile_id,
            saved_waves=saved_waves,
            files_written=files_written,
            campaign_generated=campaign_generated,
            all3_generated=all3_generated,
            final_handoff_path=final_path,
            all3_path=all3_path,
            error=f"Committed, but audit generation publication failed: {exc}",
            message="Committed; generation notification not delivered.",
        )

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
