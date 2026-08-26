"""Audit indexing, validation, freshness/temperature calculation, and copy state tracking."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from audapack.bridge.storage import parse_wave
from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditSnapshot, AuditTemperature, Project

_RE_GEN_AT = re.compile(r"^GENERATED_AT:\s*(.+)$", re.MULTILINE)
_RE_DATE_TIME = re.compile(r"^DATE_TIME:\s*(.+)$", re.MULTILINE)
_RE_TOTAL_TICKETS = re.compile(r"^TOTAL_TICKETS:\s*(\d+)", re.MULTILINE)


def parse_iso_or_custom_datetime(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    if not raw:
        return None
    # Try ISO format
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        pass

    # Try common formats
    formats = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H-%M-%S",
        "%d-%m-%Y-T%H-%M-%S",
        "%d-%m-%Y %H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw[:19], fmt)
        except Exception:
            pass
    return None


def extract_audit_metadata_timestamp(content: str) -> Optional[datetime]:
    # Check GENERATED_AT
    m_gen = _RE_GEN_AT.search(content)
    if m_gen:
        dt = parse_iso_or_custom_datetime(m_gen.group(1))
        if dt:
            return dt

    # Check DATE_TIME
    m_dt = _RE_DATE_TIME.search(content)
    if m_dt:
        dt = parse_iso_or_custom_datetime(m_dt.group(1))
        if dt:
            return dt

    return None


def is_wave_complete(content: str, wave_type: str) -> bool:
    """
    Readiness gate for wave markdown on disk.

    Single canonical authority: delegates to audapack.bridge.storage.parse_wave,
    the exact same strict contract the Bridge enforces at delivery time, so
    physical persistence and AuditIndexer readiness can never disagree.
    """
    if wave_type not in ("core", "second", "performance"):
        return False
    if not content or len(content) < 50:
        return False

    ok, _meta, _err = parse_wave(content, wave_type)
    return ok


def is_all3_ready(content: str) -> tuple[bool, int]:
    """Validates if __00_AUDIT_ALL_3.md is fully complete and ready for handoff."""
    if not content or len(content) < 100:
        return False, 0

    has_header = "# " in content and "Audit Handoff" in content
    has_generated = "GENERATED_AT:" in content

    m_tot = _RE_TOTAL_TICKETS.search(content)
    total_tickets = int(m_tot.group(1)) if m_tot else 0

    has_core = "01 — AUDIT CORE" in content or "01 - AUDIT CORE" in content or "## 01" in content
    has_second = "02 — AUDIT SECOND WAVE" in content or "02 - AUDIT SECOND WAVE" in content or "## 02" in content
    has_perf = "03 — AUDIT PERFORMANCE" in content or "03 - AUDIT PERFORMANCE" in content or "## 03" in content

    ready = bool(has_header and has_generated and has_core and has_second and has_perf)
    return ready, total_tickets


def format_age_str(age_seconds: Optional[float]) -> str:
    if age_seconds is None or age_seconds < 0:
        return ""
    total_min = int(age_seconds // 60)
    if total_min < 60:
        return f"{max(1, total_min)}m"
    hours = total_min // 60
    rem_min = total_min % 60
    if hours < 24:
        return f"{hours}h {rem_min}m" if rem_min > 0 else f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    if days < 7:
        return f"{days}d {rem_hours}h" if rem_hours > 0 else f"{days}d"
    return f"{days}d"


def calculate_temperature(age_seconds: Optional[float], cfg: AuditsConfig) -> AuditTemperature:
    if age_seconds is None:
        return AuditTemperature.NONE
    if age_seconds <= cfg.hot_seconds:
        return AuditTemperature.HOT
    if age_seconds <= cfg.warm_seconds:
        return AuditTemperature.WARM
    if age_seconds <= cfg.cool_seconds:
        return AuditTemperature.COOL
    if age_seconds <= cfg.cold_seconds:
        return AuditTemperature.COLD
    return AuditTemperature.STALE


# Performance counters for testing and diagnostics
AUDIT_COUNTERS = {
    "files_read": 0,
    "directory_scans": 0,
}


def reset_audit_counters():
    AUDIT_COUNTERS["files_read"] = 0
    AUDIT_COUNTERS["directory_scans"] = 0


class AuditIndexer:
    def __init__(self, config: AppConfig):
        self.config = config
        # Cache: project_id -> (AuditSnapshot, dict[str, tuple[int, int]])
        self._cache: dict[str, tuple[AuditSnapshot, dict[str, tuple[int, int]]]] = {}

    def invalidate(self, project_id: Optional[str] = None):
        """Invalidates snapshot cache for a single project or all projects."""
        if project_id:
            self._cache.pop(project_id, None)
        else:
            self._cache.clear()

    def find_project_audit_dir(self, project: Project) -> Optional[Path]:
        root = Path(self.config.audits.root)
        if not root.exists() or not root.is_dir():
            return None

        raw_names = [
            project.audit_project_name,
            project.display_name,
            project.archive_name,
            project.id,
        ]
        base_names = [n.strip() for n in raw_names if n and n.strip()]
        names_to_try = list(base_names)
        for n in base_names:
            stripped = n.lstrip("_")
            if stripped and stripped not in names_to_try:
                names_to_try.append(stripped)
            with_us = f"_{stripped}"
            if with_us not in names_to_try:
                names_to_try.append(with_us)

        # 1. Canonical path check FIRST: root / <GROUP> / <Name>
        grp = project.priority_group.upper()
        grp_dir = root / grp
        if grp_dir.exists() and grp_dir.is_dir():
            for name in names_to_try:
                candidate = grp_dir / name
                if candidate.exists() and candidate.is_dir():
                    return candidate

        # 2. Dynamic group scan fallback (supports MAIN0..MAINN, SIDE0..SIDEN, etc.)
        AUDIT_COUNTERS["directory_scans"] += 1
        if grp_dir.exists() and grp_dir.is_dir():
            for child in grp_dir.iterdir():
                if child.is_dir() and any(child.name.lower() == n.lower() for n in names_to_try):
                    return child

        # 3. Look in any dynamically discovered group folders
        try:
            for g_child in root.iterdir():
                if g_child.is_dir() and g_child.name != grp:
                    for name in names_to_try:
                        candidate = g_child / name
                        if candidate.exists() and candidate.is_dir():
                            return candidate
                    for child in g_child.iterdir():
                        if child.is_dir() and any(child.name.lower() == n.lower() for n in names_to_try):
                            return child
        except Exception:
            pass

        # 4. Look directly in root
        for name in names_to_try:
            candidate = root / name
            if candidate.exists() and candidate.is_dir():
                return candidate
        try:
            for child in root.iterdir():
                if child.is_dir() and any(child.name.lower() == n.lower() for n in names_to_try):
                    return child
        except Exception:
            pass

        return None

    def _get_dir_signatures(self, audit_dir: Optional[Path]) -> dict[str, tuple[int, int]]:
        """Returns map of filename -> (size, mtime_ns) for fast cache validation."""
        sigs: dict[str, tuple[int, int]] = {}
        if not audit_dir or not audit_dir.exists() or not audit_dir.is_dir():
            return sigs
        try:
            with os.scandir(audit_dir) as it:
                for entry in it:
                    if entry.is_file() and entry.name.lower().endswith(".md"):
                        st = entry.stat()
                        sigs[entry.name] = (st.st_size, st.st_mtime_ns)
        except Exception:
            pass
        return sigs

    def scan_project(self, project: Project, now: Optional[datetime] = None) -> AuditSnapshot:
        current_time = now or datetime.now()
        audit_dir = self.find_project_audit_dir(project)

        if not audit_dir:
            return AuditSnapshot(
                project_id=project.id,
                project_name=project.display_name,
                temperature=AuditTemperature.NONE,
            )

        # Check lightweight cache
        current_sigs = self._get_dir_signatures(audit_dir)
        cached = self._cache.get(project.id)
        if cached:
            cached_snap, cached_sigs = cached
            if current_sigs == cached_sigs:
                # Signatures match exactly -> reuse cached snapshot, recalculating age & temperature in memory
                latest_timestamp = cached_snap.audit_timestamp
                age_seconds: Optional[float] = None
                if latest_timestamp:
                    age_seconds = max(0.0, (current_time - latest_timestamp).total_seconds())
                temp = (
                    calculate_temperature(age_seconds, self.config.audits)
                    if (cached_snap.completed_waves > 0 or cached_snap.all3_ready)
                    else AuditTemperature.NONE
                )
                return AuditSnapshot(
                    project_id=cached_snap.project_id,
                    project_name=cached_snap.project_name,
                    core_path=cached_snap.core_path,
                    core_complete=cached_snap.core_complete,
                    second_path=cached_snap.second_path,
                    second_complete=cached_snap.second_complete,
                    performance_path=cached_snap.performance_path,
                    performance_complete=cached_snap.performance_complete,
                    all3_path=cached_snap.all3_path,
                    all3_ready=cached_snap.all3_ready,
                    all3_sha256=cached_snap.all3_sha256,
                    audit_timestamp=cached_snap.audit_timestamp,
                    audit_age_seconds=age_seconds,
                    temperature=temp,
                    completed_waves=cached_snap.completed_waves,
                    total_tickets=cached_snap.total_tickets,
                )

        core_file: Optional[Path] = None
        second_file: Optional[Path] = None
        perf_file: Optional[Path] = None
        all3_file: Optional[Path] = None

        # Search for canonical files
        for f_name in current_sigs.keys():
            if f_name.startswith("."):
                continue
            f = audit_dir / f_name
            if "01_AUDIT_CORE" in f_name or "01__AUDIT_CORE" in f_name:
                core_file = f
            elif "02_AUDIT_SECOND_WAVE" in f_name or "02__AUDIT_SECOND_WAVE" in f_name:
                second_file = f
            elif "03_AUDIT_PERFORMANCE" in f_name or "03__AUDIT_PERFORMANCE" in f_name:
                perf_file = f
            elif "00_AUDIT_ALL_3" in f_name or "00__AUDIT_ALL_3" in f_name:
                all3_file = f

        # Validate waves with single-read
        core_complete = False
        second_complete = False
        perf_complete = False
        all3_ready = False
        all3_sha256 = ""
        total_tickets = 0
        timestamps: list[datetime] = []

        c_text: str = ""
        s_text: str = ""
        p_text: str = ""
        a_text: str = ""

        if core_file and core_file.exists():
            try:
                c_text = core_file.read_text(encoding="utf-8")
                AUDIT_COUNTERS["files_read"] += 1
                core_complete = is_wave_complete(c_text, "core")
                ts = extract_audit_metadata_timestamp(c_text) or datetime.fromtimestamp(core_file.stat().st_mtime)
                if ts:
                    timestamps.append(ts)
            except Exception:
                pass

        if second_file and second_file.exists():
            try:
                s_text = second_file.read_text(encoding="utf-8")
                AUDIT_COUNTERS["files_read"] += 1
                second_complete = is_wave_complete(s_text, "second")
                ts = extract_audit_metadata_timestamp(s_text) or datetime.fromtimestamp(second_file.stat().st_mtime)
                if ts:
                    timestamps.append(ts)
            except Exception:
                pass

        if perf_file and perf_file.exists():
            try:
                p_text = perf_file.read_text(encoding="utf-8")
                AUDIT_COUNTERS["files_read"] += 1
                perf_complete = is_wave_complete(p_text, "performance")
                ts = extract_audit_metadata_timestamp(p_text) or datetime.fromtimestamp(perf_file.stat().st_mtime)
                if ts:
                    timestamps.append(ts)
            except Exception:
                pass

        if all3_file and all3_file.exists():
            try:
                a_text = all3_file.read_text(encoding="utf-8")
                AUDIT_COUNTERS["files_read"] += 1
                all3_ready, total_tickets = is_all3_ready(a_text)
                all3_sha256 = hashlib.sha256(a_text.encode("utf-8")).hexdigest()
                ts = extract_audit_metadata_timestamp(a_text)
                if ts:
                    timestamps.insert(0, ts)  # Prioritize ALL_3 GENERATED_AT
                else:
                    timestamps.append(datetime.fromtimestamp(all3_file.stat().st_mtime))
            except Exception:
                pass

        if core_complete and second_complete and perf_complete and (not all3_file or not all3_ready):
            try:
                from audapack.bridge.storage import atomic_write, generate_canonical_all3, parse_wave
                # Reuse already-read strings instead of re-reading from disk
                _, c_m, _ = parse_wave(c_text, "core")
                _, s_m, _ = parse_wave(s_text, "second")
                _, p_m, _ = parse_wave(p_text, "performance")
                parsed_d = {
                    "core": c_m or {},
                    "second": s_m or {},
                    "performance": p_m or {},
                }
                all3_target = audit_dir / f"{audit_dir.name}__00_AUDIT_ALL_3.md"
                all3_c = generate_canonical_all3(audit_dir.name, f"synthesized_{int(current_time.timestamp())}", parsed_d)
                atomic_write(all3_target, all3_c)
                all3_file = all3_target
                all3_ready, total_tickets = is_all3_ready(all3_c)
                all3_sha256 = hashlib.sha256(all3_c.encode("utf-8")).hexdigest()
                timestamps.insert(0, current_time)
                # Update signature map with newly synthesized file
                current_sigs = self._get_dir_signatures(audit_dir)
            except Exception:
                pass

        completed_waves = sum([core_complete, second_complete, perf_complete])
        latest_timestamp = max(timestamps) if timestamps else None

        age_seconds: Optional[float] = None
        if latest_timestamp:
            age_seconds = max(0.0, (current_time - latest_timestamp).total_seconds())

        temperature = (
            calculate_temperature(age_seconds, self.config.audits)
            if (completed_waves > 0 or all3_ready)
            else AuditTemperature.NONE
        )

        snap = AuditSnapshot(
            project_id=project.id,
            project_name=project.display_name,
            core_path=core_file,
            core_complete=core_complete,
            second_path=second_file,
            second_complete=second_complete,
            performance_path=perf_file,
            performance_complete=perf_complete,
            all3_path=all3_file,
            all3_ready=all3_ready,
            all3_sha256=all3_sha256,
            audit_timestamp=latest_timestamp,
            audit_age_seconds=age_seconds,
            temperature=temperature,
            completed_waves=completed_waves,
            total_tickets=total_tickets,
        )

        # Store in cache
        self._cache[project.id] = (snap, current_sigs)
        return snap

    def scan_all_projects(self, now: Optional[datetime] = None) -> dict[str, AuditSnapshot]:
        results: dict[str, AuditSnapshot] = {}
        for p in self.config.projects:
            results[p.id] = self.scan_project(p, now=now)
        return results

    def read_exact_all3(self, snapshot: AuditSnapshot) -> tuple[bool, str, str]:
        """Reads exact ALL_3 text from disk and computes its SHA-256 hash."""
        if not snapshot.all3_path or not snapshot.all3_path.exists() or not snapshot.all3_ready:
            return False, "", ""
        try:
            content = snapshot.all3_path.read_text(encoding="utf-8")
            AUDIT_COUNTERS["files_read"] += 1
            sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return True, content, sha256
        except Exception:
            return False, "", ""

