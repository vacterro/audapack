"""Bridge audit storage engine, canonical file generator, and registry router."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from audapack.config import AppConfig
from audapack.models import Project
from audapack.projects import ProjectRegistry

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
_RE_TICKET_PATTERNS = {
    wave: re.compile(rf"\[{w_info['prefix']}(\d+)\]")
    for wave, w_info in WAVES_CONFIG.items()
}


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
    except ValueError:
        raise InvalidProjectPathError(
            f"Resolved destination {resolved_path} escapes the configured audit root {resolved_root}"
        )
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

    Human identity (display_name) and filesystem identity are separate: the directory
    name and the canonical artifact filename base are derived deterministically through
    sanitize_project_name(), and the final physical destination is containment-checked
    against the audit root before returning (InvalidProjectPathError on escape; nothing
    is created or written by this function).
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


def parse_wave(text: str, wave: str) -> tuple[bool, Optional[dict[str, Any]], str]:
    """
    Strict canonical validation of an incoming audit wave markdown.

    Required exact contract (line-anchored header values, never substring hits):
      PROJECT_NAME: <non-empty>
      WAVE: <exact expected wave header for this wave type>
      STATUS: <exact terminal status line for this wave type>
      TICKETS: <integer >= 0>
      <DONE marker>: <non-empty remainder>
    When TICKETS > 0, the count must equal the number of unique [PREFIX-###]
    ticket ids of THIS wave's prefix (other waves' tickets are not evidence).
    """
    if wave not in WAVES_CONFIG:
        return False, None, f"Unknown wave: {wave}"

    w_info = WAVES_CONFIG[wave]
    prefix = w_info["prefix"]
    done_marker = w_info["done_marker"]

    raw_lines = [line.strip() for line in text.splitlines()]
    lines = []
    for rl in raw_lines:
        if rl.startswith("```"):
            continue
        cleaned = _RE_CLEAN_HEADER1.sub(r"\1: ", rl)
        cleaned = _RE_CLEAN_HEADER2.sub(r"\1: ", cleaned)
        lines.append(cleaned)

    def _header_value(key: str) -> Optional[str]:
        anchor = key + ":"
        for line_s in lines:
            if line_s.startswith(anchor):
                return line_s[len(anchor):].strip().strip("`*_ ")
        return None

    meta: dict[str, Any] = {}

    # Optional metadata headers
    for key, meta_key in (
        ("TARGET", "target"),
        ("BASELINE", "baseline"),
        ("CORE_BASELINE", "core_baseline"),
        ("PREVIOUS_BASELINE", "previous_baseline"),
        ("GIT_CONTEXT", "git_context"),
        ("SAIPEN_CONTEXT", "saipen_context"),
        ("AUDIT_SCOPE", "audit_scope"),
        ("DATE_TIME", "date_time"),
        ("TEST_STATUS", "test_status"),
        ("TEST_LIMITATION", "test_limitation"),
        ("VERIFIED_INSTEAD", "verified_instead"),
    ):
        value = _header_value(key)
        if value is not None:
            meta[meta_key] = value

    # PROJECT_NAME: mandatory and non-empty
    project_name = _header_value("PROJECT_NAME")
    if not project_name:
        return False, None, f"Missing or empty PROJECT_NAME in {wave}"
    meta["project_name"] = project_name

    # WAVE header: exact match against this wave's canonical header
    wave_header = _header_value("WAVE")
    if wave_header != w_info["wave_header"]:
        return False, None, (
            f"Wrong WAVE header in {wave}: got {wave_header!r}, "
            f"expected {w_info['wave_header']!r}"
        )

    # Terminal STATUS: exact line match
    status_raw = _header_value("STATUS")
    if status_raw is None:
        return False, None, f"Missing terminal STATUS in {wave}"
    if f"STATUS: {status_raw}" != w_info["status_line"]:
        return False, None, (
            f"Wrong terminal STATUS in {wave}: got 'STATUS: {status_raw}', "
            f"expected {w_info['status_line']!r}"
        )
    meta["status"] = status_raw

    # TICKETS: non-negative integer, present
    tickets_raw = _header_value("TICKETS")
    if tickets_raw is None:
        return False, None, f"Missing TICKETS in {wave}"
    try:
        n_tickets = int(tickets_raw)
    except Exception:
        return False, None, f"Invalid TICKETS count in {wave}"
    if n_tickets < 0:
        return False, None, f"Negative TICKETS count in {wave}"
    meta["tickets"] = n_tickets

    # DONE marker: must appear as a line with a non-empty remainder
    done_ok = any(
        line_s.startswith(done_marker) and len(line_s[len(done_marker):].strip()) > 0
        for line_s in lines
    )
    if not done_ok:
        return False, None, f"Missing {done_marker} in {wave}"

    # Ticket consistency: unique ids of THIS wave's prefix only
    if n_tickets > 0:
        ticket_pat = _RE_TICKET_PATTERNS.get(wave)
        found_tickets = set(int(m) for m in ticket_pat.findall(text)) if ticket_pat else set()
        if len(found_tickets) != n_tickets:
            return False, None, f"Expected {n_tickets} unique {prefix} tickets, found {len(found_tickets)}"

    meta["full_text"] = text.strip()
    return True, meta, ""


def generate_canonical_all3(
    project_name: str,
    run_id: str,
    parsed_waves: dict[str, dict[str, Any]],
) -> str:
    """Combines 3 validated waves into canonical __00_AUDIT_ALL_3.md."""
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
        f"STATUS: AUDIT_ALL_3: COMPLETE",
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
