"""Audit text ingestion, normalization, multi-wave splitting, and storage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from audapack.bridge.state import increment_audit_generation
from audapack.bridge.storage import (
    WAVES_CONFIG,
    atomic_write,
    generate_canonical_all3,
    parse_wave,
    resolve_project_audit_dir,
)
from audapack.config import AppConfig, load_config
from audapack.models import Project
from audapack.projects import ProjectRegistry


@dataclass
class IngestResult:
    ok: bool
    project_name: str = ""
    saved_waves: list[str] = field(default_factory=list)
    files_written: list[Path] = field(default_factory=list)
    all3_generated: bool = False
    all3_path: Optional[Path] = None
    message: str = ""
    error: str = ""


def clean_markdown_headers(text: str) -> str:
    """Strips markdown bolding and formatting decorators from audit headers."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Skip pure code block markers
        if line.startswith("```"):
            continue
        # Strip markdown bolding like **HEADER:** or **HEADER**:
        line = re.sub(r"^\*{1,2}([A-Za-z0-9_]+):\*{0,2}\s*", r"\1: ", line)
        line = re.sub(r"^\*{1,2}([A-Za-z0-9_]+)\*{1,2}:\s*", r"\1: ", line)
        lines.append(line)
    return "\n".join(lines)


def detect_wave_type(text: str) -> Optional[str]:
    """Detects wave type (core, second, performance) from audit text."""
    norm = clean_markdown_headers(text)
    for wave_name, cfg in WAVES_CONFIG.items():
        if cfg["status_line"] in norm or cfg["done_marker"] in norm:
            return wave_name
        if f"WAVE: {cfg['wave_header']}" in norm:
            return wave_name

    # Fallback heuristic
    if "CORE_DONE_WHEN:" in text or "AUDIT CORE" in text:
        return "core"
    if "SECOND_WAVE_DONE_WHEN:" in text or "AUDIT SECOND WAVE" in text:
        return "second"
    if "PERFORMANCE_DONE_WHEN:" in text or "AUDIT PERFORMANCE" in text:
        return "performance"
    return None


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


def split_multi_wave_text(text: str) -> dict[str, str]:
    """Splits text containing multiple wave audits into individual wave texts."""
    waves: dict[str, str] = {}
    cleaned = clean_markdown_headers(text)

    # Check if text contains multiple WAVE: markers
    markers = [
        ("core", "WAVE: AUDIT CORE"),
        ("second", "WAVE: AUDIT SECOND WAVE"),
        ("performance", "WAVE: AUDIT PERFORMANCE"),
    ]

    found_positions = []
    for wave_name, marker in markers:
        idx = cleaned.find(marker)
        if idx != -1:
            # Find the PROJECT_NAME line preceding this marker if any
            p_idx = cleaned.rfind("PROJECT_NAME:", 0, idx)
            start_pos = p_idx if p_idx != -1 else idx
            found_positions.append((start_pos, wave_name))

    if not found_positions:
        # Check by DONE_WHEN markers
        done_markers = [
            ("core", "CORE_DONE_WHEN:"),
            ("second", "SECOND_WAVE_DONE_WHEN:"),
            ("performance", "PERFORMANCE_DONE_WHEN:"),
        ]
        single_type = detect_wave_type(text)
        if single_type:
            waves[single_type] = text
        return waves

    found_positions.sort(key=lambda x: x[0])
    for i, (pos, wave_name) in enumerate(found_positions):
        end_pos = found_positions[i + 1][0] if i + 1 < len(found_positions) else len(cleaned)
        wave_content = cleaned[pos:end_pos].strip()
        if wave_content:
            waves[wave_name] = wave_content

    return waves


def ingest_audit_text(
    content: str,
    config: Optional[AppConfig] = None,
    project_hint: Optional[str] = None,
) -> IngestResult:
    """Ingests, validates, and stores one or more audit waves from text."""
    cfg = config or load_config()
    content = content.strip()
    if not content:
        return IngestResult(ok=False, error="Audit text is empty.")

    wave_chunks = split_multi_wave_text(content)
    if not wave_chunks:
        single_type = detect_wave_type(content)
        if single_type:
            wave_chunks = {single_type: content}
        else:
            return IngestResult(
                ok=False,
                error="Could not identify any valid audit wave markers (CORE, SECOND WAVE, or PERFORMANCE) in text.",
            )

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

    # CORE-010: reject a mixed-project paste atomically, before any physical
    # write. Every chunk must declare the same project as the selected target;
    # otherwise one project's wave could be cross-contaminated into another's
    # audit directory.
    for wave_name, chunk_text in wave_chunks.items():
        chunk_proj = extract_project_name_from_text(chunk_text) or project_hint
        if not _same_project_name(chunk_proj, resolved_name):
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                error=(
                    f"Wave {wave_name} declares project '{chunk_proj or 'unknown'}', "
                    f"which does not match the target project '{resolved_name}'. "
                    f"Mixed-project audit paste rejected; nothing was written."
                ),
            )

    saved_waves = []
    files_written = []

    for wave_name, chunk_text in wave_chunks.items():
        chunk_clean = clean_markdown_headers(chunk_text)

        # Ensure PROJECT_NAME is present if missing
        if "PROJECT_NAME:" not in chunk_clean:
            chunk_clean = f"PROJECT_NAME: {resolved_name}\n" + chunk_clean

        # Ensure WAVE header is present if missing
        w_info = WAVES_CONFIG[wave_name]
        if f"WAVE: {w_info['wave_header']}" not in chunk_clean and "WAVE:" not in chunk_clean:
            chunk_clean = re.sub(
                r"^(PROJECT_NAME:[^\n]+)",
                r"\1\nWAVE: " + w_info["wave_header"],
                chunk_clean,
            )

        # Validate with parse_wave
        valid, meta, err = parse_wave(chunk_clean, wave_name)
        if not valid:
            # Try mild repair of STATUS line if only status was missing
            if w_info["done_marker"] in chunk_clean and "STATUS:" not in chunk_clean:
                chunk_clean = re.sub(
                    r"^(WAVE:[^\n]+)",
                    r"\1\n" + w_info["status_line"],
                    chunk_clean,
                )
                valid, meta, err = parse_wave(chunk_clean, wave_name)

        if not valid:
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                error=f"Wave {wave_name} validation failed: {err}",
            )

        w_no = w_info["number"]
        w_slug = w_info["slug"]
        latest_filename = f"{resolved_name}__{w_no}_{w_slug}.md"
        latest_path = target_dir / latest_filename

        try:
            atomic_write(latest_path, chunk_clean)
            saved_waves.append(wave_name)
            files_written.append(latest_path)
            increment_audit_generation(resolved_name, wave_name)
        except Exception as exc:
            return IngestResult(
                ok=False,
                project_name=resolved_name,
                error=f"Failed to write {latest_filename}: {exc}",
            )

    # Check if all 3 waves now exist on disk
    all3_generated = False
    all3_path = None

    c_path = target_dir / f"{resolved_name}__01_AUDIT_CORE.md"
    s_path = target_dir / f"{resolved_name}__02_AUDIT_SECOND_WAVE.md"
    p_path = target_dir / f"{resolved_name}__03_AUDIT_PERFORMANCE.md"

    if c_path.exists() and s_path.exists() and p_path.exists():
        try:
            # CORE-011: never promote an invalid/stale wave into a canonical
            # ALL_3. Require every wave to parse as valid with real metadata;
            # otherwise leave ALL_3 absent so readiness cannot be falsely met.
            c_valid, c_m, c_err = parse_wave(c_path.read_text(encoding="utf-8"), "core")
            s_valid, s_m, s_err = parse_wave(s_path.read_text(encoding="utf-8"), "second")
            p_valid, p_m, p_err = parse_wave(p_path.read_text(encoding="utf-8"), "performance")
            if not (c_valid and s_valid and p_valid and c_m and s_m and p_m):
                all3_generated = False
            else:
                parsed_d = {
                    "core": c_m,
                    "second": s_m,
                    "performance": p_m,
                }
                all3_content = generate_canonical_all3(
                    resolved_name,
                    f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    parsed_d,
                )
                all3_file = target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
                atomic_write(all3_file, all3_content)
                all3_generated = True
                all3_path = all3_file
                files_written.append(all3_file)
                increment_audit_generation(resolved_name, "all3")
        except Exception:
            pass

    waves_str = ", ".join(w.upper() for w in saved_waves)
    msg = f"Project '{resolved_name}': successfully saved {waves_str}."
    if all3_generated:
        msg += " ALL 3 waves complete! __00_AUDIT_ALL_3.md generated."

    return IngestResult(
        ok=True,
        project_name=resolved_name,
        saved_waves=saved_waves,
        files_written=files_written,
        all3_generated=all3_generated,
        all3_path=all3_path,
        message=msg,
    )
